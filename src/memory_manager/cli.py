# SPDX-License-Identifier: AGPL-3.0-only
"""The `memory-manager` command-line entry point (#17, #26, #33, #34).

`reindex`, `doctor`, `eval`, `export` and `import` work on the vault and
index; `serve --stdio` runs the MCP server for a local Claude Code
connection, `serve --http` runs it over Streamable HTTP (`http.py`);
`token create|list|revoke` manage the static bearer tokens `/mcp` accepts
once `DATABASE_URL` is set (ADR-0004, #34); `token create --owner --role`
gives a token an owner principal (ADR-0008 addendum 2026-10-07, #115);
`hash-password` is the operator helper for `LOGIN_MODE=password` (ADR-0004
L1, #37) - it never takes the password as an argument (it would then show
up in shell history and `ps`), only ever reading it from stdin.

`serve --http` with `DATABASE_URL` set turns bearer-token auth on for
`/mcp` (`http.py`); without it (no token to ever verify a request against)
it instead refuses to bind to a non-loopback host unless
`MM_ALLOW_UNAUTHENTICATED=1` is set - every MCP tool and the vault webhook
would otherwise be reachable by anyone who can reach the port, with nothing
in front of them. `/healthz`/`/readyz`/the vault webhook stay unauthenticated
either way - the webhook has its own HMAC-signature check, and the health
endpoints carry nothing sensitive.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import uvicorn

from memory_manager.app import open_services, open_storage
from memory_manager.auth.login_password import hash_password
from memory_manager.auth.tokens import (
    ALL_NAMESPACES,
    MEMORY_ROLES,
    TokenInfo,
    create_token,
    list_tokens,
    revoke_token,
)
from memory_manager.config import (
    EmbeddingConfig,
    EmbeddingConfigError,
    ServerConfig,
    ServerConfigError,
    StorageConfigError,
    VaultConfigError,
    storage_backend_from_env,
)
from memory_manager.db.migrate import migrate
from memory_manager.doctor import DoctorReport, run_doctor
from memory_manager.eval import EvalReport, compare, load_golden, run_eval
from memory_manager.exporter import ExportError, Manifest, export_vault
from memory_manager.http import GracefulShutdownServer, build_authenticator, create_app
from memory_manager.importers import ImportReport, dedupe_against_vault, run_import
from memory_manager.importers.chatgpt import ChatGPTFormatError
from memory_manager.importers.chatgpt import collect as collect_chatgpt
from memory_manager.importers.claude import ClaudeFormatError
from memory_manager.importers.claude import collect as collect_claude
from memory_manager.importers.markdown import collect as collect_markdown
from memory_manager.index.embeddings import provider_from_config
from memory_manager.index.indexer import Indexer, IndexStats, VaultNotesSource
from memory_manager.mcp.authz import READ_SCOPE, WRITE_SCOPE
from memory_manager.mcp.server import build_server
from memory_manager.observability.logging import configure_logging_from_env
from memory_manager.vault.validate import NOTE_TYPES

__all__ = ["main"]

_logger = logging.getLogger(__name__)

_DEFAULT_GOLDEN = Path("eval/golden.yaml")
_DEFAULT_EVAL_VAULT = Path("examples/vault")
_DEFAULT_BASELINE = Path("eval/baseline.json")
_DEFAULT_EVAL_K = 5

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_ALLOW_UNAUTHENTICATED_ENV = "MM_ALLOW_UNAUTHENTICATED"


class _MissingEnvironment(RuntimeError):
    """A required environment variable is not set."""


def main(argv: list[str] | None = None) -> int:
    """Parse `argv` (`sys.argv[1:]` if omitted) and run the requested command."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "doctor":
        return _run_doctor_command(args.vault)

    if args.command == "eval":
        return _run_eval_command(
            golden=args.golden,
            vault_dir=args.vault,
            baseline=args.baseline,
            update_baseline=args.update_baseline,
            k=args.k,
        )

    if args.command == "export":
        return _run_export_command(
            args.vault, out=args.out, include_archive=args.include_archive, force=args.force
        )

    if args.command == "import":
        if args.import_source == "markdown":
            return asyncio.run(
                _run_import_markdown(
                    args.dir,
                    namespace=args.namespace,
                    default_type=args.type,
                    apply=args.apply,
                )
            )
        if args.import_source == "claude":
            return asyncio.run(
                _run_import_claude(
                    args.file,
                    namespace=args.namespace,
                    type_=args.type,
                    apply=args.apply,
                )
            )
        if args.import_source == "chatgpt":
            return asyncio.run(
                _run_import_chatgpt(
                    args.file,
                    namespace=args.namespace,
                    type_=args.type,
                    from_conversations=args.from_conversations,
                    apply=args.apply,
                )
            )
        parser.print_help()
        return 1
    if args.command == "serve":
        return _serve(stdio=args.stdio, http=args.http)

    if args.command == "hash-password":
        return _run_hash_password()

    if args.command == "token":
        if args.subcommand == "create":
            return asyncio.run(
                _run_token_create(
                    args.name,
                    scopes=args.scopes,
                    namespaces=args.namespaces or [ALL_NAMESPACES],
                    expires_days=args.expires_days,
                    owner_oid=args.owner,
                    roles=args.roles or [],
                )
            )
        if args.subcommand == "list":
            return asyncio.run(_run_token_list())
        if args.subcommand == "revoke":
            return asyncio.run(_run_token_revoke(args.name))
        parser.print_help()
        return 1

    if args.command != "reindex":
        parser.print_help()
        return 1

    try:
        storage_backend = storage_backend_from_env(dict(os.environ))
        database_url = _require_env("DATABASE_URL")
        vault_dir = Path(_require_env("VAULT_DIR")) if storage_backend == "git" else None
        embedding_config = EmbeddingConfig.from_env(dict(os.environ))
    except (_MissingEnvironment, EmbeddingConfigError, StorageConfigError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    return asyncio.run(_reindex(database_url, vault_dir, embedding_config, full=args.full))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memory-manager")
    subparsers = parser.add_subparsers(dest="command")
    reindex_parser = subparsers.add_parser(
        "reindex", help="bring the Postgres index in step with the vault"
    )
    reindex_parser.add_argument(
        "--full",
        action="store_true",
        help="also drop stale rows and recompute every link (full rebuild)",
    )
    doctor_parser = subparsers.add_parser(
        "doctor", help="check every note in the vault against ADR-0005"
    )
    doctor_parser.add_argument(
        "--vault",
        default=os.environ.get("VAULT_DIR"),
        help="path to the vault root (defaults to $VAULT_DIR)",
    )
    eval_parser = subparsers.add_parser(
        "eval", help="score retrieval quality against the golden query set"
    )
    eval_parser.add_argument(
        "--golden", type=Path, default=_DEFAULT_GOLDEN, help="path to the golden query set"
    )
    eval_parser.add_argument(
        "--vault", type=Path, default=_DEFAULT_EVAL_VAULT, help="vault to index and query against"
    )
    eval_parser.add_argument(
        "--baseline", type=Path, default=_DEFAULT_BASELINE, help="path to the committed baseline"
    )
    eval_parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="overwrite --baseline with this run's metrics instead of comparing against it",
    )
    eval_parser.add_argument(
        "--k", type=int, default=_DEFAULT_EVAL_K, help="cutoff for recall@k and the search limit"
    )

    export_parser = subparsers.add_parser(
        "export", help="export the vault to a tar.gz archive plus a manifest.json"
    )
    export_parser.add_argument(
        "--vault",
        default=os.environ.get("VAULT_DIR"),
        help="path to the vault root (defaults to $VAULT_DIR)",
    )
    export_parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="output archive path (default 'memory-export-<date>.tar.gz')",
    )
    export_parser.add_argument(
        "--include-archive",
        dest="include_archive",
        action="store_true",
        default=True,
        help="include archived notes (_archive/) in the export (default)",
    )
    export_parser.add_argument(
        "--no-include-archive",
        dest="include_archive",
        action="store_false",
        help="exclude archived notes (_archive/) from the export",
    )
    export_parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite --out if it already exists",
    )

    import_parser = subparsers.add_parser("import", help="import notes from an external source")
    import_subparsers = import_parser.add_subparsers(dest="import_source")
    markdown_parser = import_subparsers.add_parser(
        "markdown", help="import a folder of Markdown files"
    )
    markdown_parser.add_argument("dir", type=Path, help="folder to walk recursively for *.md files")
    markdown_parser.add_argument(
        "--namespace", required=True, help="namespace every imported note is filed under"
    )
    markdown_parser.add_argument(
        "--type",
        default="reference",
        choices=NOTE_TYPES,
        help="note type used when a file's frontmatter does not set a valid one",
    )
    markdown_parser.add_argument(
        "--apply",
        action="store_true",
        help="actually write notes (default is a dry run that writes nothing)",
    )

    claude_parser = import_subparsers.add_parser(
        "claude", help="import a Claude memory export (zip, memories JSON, or a plain text list)"
    )
    claude_parser.add_argument(
        "file", type=Path, help="export zip, a memories JSON file, or a plain text/Markdown list"
    )
    claude_parser.add_argument(
        "--namespace", required=True, help="namespace every imported note is filed under"
    )
    claude_parser.add_argument(
        "--type",
        default="user",
        choices=NOTE_TYPES,
        help="note type for items the export does not map to a fixed type itself "
        "(memory_files entries and a plain-text fallback; conversations_memory and "
        "project_memories always become 'user'/'project')",
    )
    claude_parser.add_argument(
        "--apply",
        action="store_true",
        help="actually write notes (default is a dry run that writes nothing)",
    )

    chatgpt_parser = import_subparsers.add_parser(
        "chatgpt", help="import a ChatGPT memory list, or 'bio' calls from a conversations export"
    )
    chatgpt_parser.add_argument(
        "file",
        type=Path,
        help="a plain text/Markdown memory list, or (with --from-conversations) a "
        "conversations.json export",
    )
    chatgpt_parser.add_argument(
        "--namespace", required=True, help="namespace every imported note is filed under"
    )
    chatgpt_parser.add_argument(
        "--type", default="user", choices=NOTE_TYPES, help="note type for every imported item"
    )
    chatgpt_parser.add_argument(
        "--from-conversations",
        action="store_true",
        help="treat 'file' as a ChatGPT conversations.json export and extract 'bio' memory "
        "calls from it, instead of a plain text memory list",
    )
    chatgpt_parser.add_argument(
        "--apply",
        action="store_true",
        help="actually write notes (default is a dry run that writes nothing)",
    )

    serve_parser = subparsers.add_parser("serve", help="run the MCP server")
    serve_parser.add_argument(
        "--stdio",
        action="store_true",
        help="serve over stdio, for a local Claude Code connection",
    )
    serve_parser.add_argument(
        "--http",
        action="store_true",
        help="serve over Streamable HTTP, binding to $HOST:$PORT (default 127.0.0.1:8080)",
    )

    token_parser = subparsers.add_parser(
        "token", help="manage static bearer tokens for the HTTP transport (ADR-0004)"
    )
    token_subparsers = token_parser.add_subparsers(dest="subcommand")

    token_create_parser = token_subparsers.add_parser(
        "create", help="create a token and print it once - it is never shown again"
    )
    token_create_parser.add_argument("name", help="a unique name identifying the token")
    token_create_parser.add_argument(
        "--scope",
        dest="scopes",
        action="append",
        choices=(READ_SCOPE, WRITE_SCOPE),
        required=True,
        help=f"repeatable; one of {READ_SCOPE!r}, {WRITE_SCOPE!r}",
    )
    token_create_parser.add_argument(
        "--namespace",
        dest="namespaces",
        action="append",
        default=None,
        help=f"repeatable; namespace the token may read/write, or omit for every namespace "
        f"({ALL_NAMESPACES!r})",
    )
    token_create_parser.add_argument(
        "--expires-days",
        type=int,
        default=None,
        help="the token stops verifying this many days from now (default: never expires)",
    )
    token_create_parser.add_argument(
        "--owner",
        dest="owner",
        default=None,
        help="the token's owner principal (an oid); required together with --role (#115)",
    )
    token_create_parser.add_argument(
        "--role",
        dest="roles",
        action="append",
        choices=MEMORY_ROLES,
        help=f"repeatable; one of {MEMORY_ROLES!r}; required together with --owner",
    )

    token_subparsers.add_parser("list", help="list every token's metadata (never the token itself)")

    token_revoke_parser = token_subparsers.add_parser("revoke", help="revoke a token by name")
    token_revoke_parser.add_argument("name", help="the token's name, as passed to 'token create'")

    subparsers.add_parser(
        "hash-password",
        help="hash a password from stdin into an ADMIN_PASSWORD_HASH value (ADR-0004 L1)",
    )

    return parser


def _run_hash_password() -> int:
    password = sys.stdin.readline().rstrip("\n")
    if not password:
        print("hash-password: no password read from stdin", file=sys.stderr)
        return 2
    print(hash_password(password))
    return 0


def _run_doctor_command(vault: str | None) -> int:
    if not vault:
        print("--vault is required (or set VAULT_DIR)", file=sys.stderr)
        return 2
    report = run_doctor(Path(vault))
    _print_doctor_report(report)
    return 1 if report.errors else 0


def _print_doctor_report(report: DoctorReport) -> None:
    for error in report.errors:
        print(f"ERROR: {error}")
    for warning in report.warnings:
        print(f"WARNING: {warning}")
    print(f"{len(report.errors)} error(s), {len(report.warnings)} warning(s)")


def _run_export_command(
    vault: str | None, *, out: Path | None, include_archive: bool, force: bool
) -> int:
    if not vault:
        print("--vault is required (or set VAULT_DIR)", file=sys.stderr)
        return 2
    out_path = out or Path(f"memory-export-{datetime.now(UTC).date().isoformat()}.tar.gz")
    if out_path.exists() and not force:
        print(f"'{out_path}' already exists, pass --force to overwrite", file=sys.stderr)
        return 2

    try:
        manifest = export_vault(Path(vault), out_path, include_archive=include_archive)
    except ExportError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    _print_export_manifest(manifest, out_path)
    return 0


def _print_export_manifest(manifest: Manifest, out_path: Path) -> None:
    print(f"exported {manifest.note_count} note(s) to {out_path}")


async def _run_import_markdown(
    directory: Path, *, namespace: str, default_type: str, apply: bool
) -> int:
    if not directory.is_dir():
        print(f"'{directory}' is not a directory", file=sys.stderr)
        return 2

    items, pre_rejected = collect_markdown(
        directory, namespace=namespace, default_type=default_type
    )

    try:
        async with open_storage(os.environ) as storage:
            report = await run_import(items, storage, apply=apply)
    except VaultConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    report.rejected = pre_rejected + report.rejected
    _print_import_report(report, apply=apply)
    return 1 if (apply and report.rejected) else 0


async def _run_import_claude(file: Path, *, namespace: str, type_: str, apply: bool) -> int:
    if not file.is_file():
        print(f"'{file}' is not a file", file=sys.stderr)
        return 2

    try:
        items, pre_rejected = collect_claude(file, namespace=namespace, type_=type_)
    except ClaudeFormatError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        async with open_storage(os.environ) as storage:
            kept_items, duplicates = await dedupe_against_vault(items, storage)
            report = await run_import(kept_items, storage, apply=apply)
    except VaultConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    report.rejected = pre_rejected + report.rejected
    report.duplicates = duplicates + report.duplicates
    _print_import_report(report, apply=apply)
    return 1 if (apply and report.rejected) else 0


async def _run_import_chatgpt(
    file: Path, *, namespace: str, type_: str, from_conversations: bool, apply: bool
) -> int:
    if not file.is_file():
        print(f"'{file}' is not a file", file=sys.stderr)
        return 2

    try:
        items, pre_rejected = collect_chatgpt(
            file, namespace=namespace, type_=type_, from_conversations=from_conversations
        )
    except ChatGPTFormatError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        async with open_storage(os.environ) as storage:
            kept_items, duplicates = await dedupe_against_vault(items, storage)
            report = await run_import(kept_items, storage, apply=apply)
    except VaultConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    report.rejected = pre_rejected + report.rejected
    report.duplicates = duplicates + report.duplicates
    _print_import_report(report, apply=apply)
    return 1 if (apply and report.rejected) else 0


def _print_import_report(report: ImportReport, *, apply: bool) -> None:
    verb = "created" if apply else "would_create"
    print(
        f"{verb}={len(report.created)} unchanged={len(report.unchanged)} "
        f"skipped_existing={len(report.skipped_existing)} duplicates={len(report.duplicates)} "
        f"flagged={len(report.flagged)} rejected={len(report.rejected)}"
    )
    for path, flags in report.flagged:
        print(f"FLAGGED: {path}: {', '.join(flags)}")
    for source_ref, reason in report.rejected:
        print(f"REJECTED: {source_ref}: {reason}")
    if not apply:
        print("dry run - nothing was written, pass --apply to write")


async def _open_migrated_pool() -> asyncpg.Pool | None:
    """A connection pool to `DATABASE_URL`, migrated first. `None` if it is unset."""
    try:
        database_url = _require_env("DATABASE_URL")
    except _MissingEnvironment as exc:
        print(str(exc), file=sys.stderr)
        return None

    migration_conn = await asyncpg.connect(database_url)
    try:
        await migrate(migration_conn)
    finally:
        await migration_conn.close()

    return await asyncpg.create_pool(database_url)


async def _run_token_create(
    name: str,
    *,
    scopes: list[str],
    namespaces: list[str],
    expires_days: int | None,
    owner_oid: str | None,
    roles: list[str],
) -> int:
    pool = await _open_migrated_pool()
    if pool is None:
        return 2

    try:
        expires_at = (
            datetime.now(UTC) + timedelta(days=expires_days) if expires_days is not None else None
        )
        try:
            plaintext, info = await create_token(
                pool,
                name,
                scopes=scopes,
                namespaces=namespaces,
                expires_at=expires_at,
                owner_oid=owner_oid,
                roles=roles,
            )
        except asyncpg.UniqueViolationError:
            print(f"a token named {name!r} already exists", file=sys.stderr)
            return 2
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    finally:
        await pool.close()

    print(plaintext)
    print(
        f"^ token {info.name!r} created with scopes={list(info.scopes)} "
        f"namespaces={list(info.namespaces)} owner_oid={info.owner_oid!r} "
        f"roles={list(info.roles)} - store it now, it will not be shown again",
        file=sys.stderr,
    )
    return 0


async def _run_token_list() -> int:
    pool = await _open_migrated_pool()
    if pool is None:
        return 2

    try:
        tokens = await list_tokens(pool)
    finally:
        await pool.close()

    for info in tokens:
        _print_token_info(info)
    return 0


def _print_token_info(info: TokenInfo) -> None:
    status = "revoked" if info.revoked_at is not None else "active"
    print(
        f"{info.name}\tstatus={status}\tscopes={','.join(info.scopes)}\t"
        f"namespaces={','.join(info.namespaces)}\towner_oid={info.owner_oid or '-'}\t"
        f"roles={','.join(info.roles) or '-'}\tcreated_at={info.created_at.isoformat()}\t"
        f"expires_at={info.expires_at.isoformat() if info.expires_at else '-'}\t"
        f"last_used_at={info.last_used_at.isoformat() if info.last_used_at else '-'}"
    )


async def _run_token_revoke(name: str) -> int:
    pool = await _open_migrated_pool()
    if pool is None:
        return 2

    try:
        revoked = await revoke_token(pool, name)
    finally:
        await pool.close()

    if not revoked:
        print(f"no active token named {name!r}", file=sys.stderr)
        return 2
    print(f"revoked {name!r}")
    return 0


async def _reindex(
    database_url: str, vault_dir: Path | None, embedding_config: EmbeddingConfig, *, full: bool
) -> int:
    """Reindex `database_url` from `vault_dir` (`"git"`) or `vault_notes` (`"postgres"`).

    `vault_dir` is `None` for the `postgres` backend (ADR-0007 §2, WP-18):
    there is no vault to walk, `vault_notes` is `Indexer`'s source instead
    (`VaultNotesSource`).
    """
    # A plain connection for the migration, not one from the pool below:
    # `migrate` takes an `asyncpg.Connection`, not a pool's connection proxy.
    migration_conn = await asyncpg.connect(database_url)
    try:
        await migrate(migration_conn)
    finally:
        await migration_conn.close()

    provider = provider_from_config(embedding_config)
    pool = await asyncpg.create_pool(database_url)
    source = vault_dir if vault_dir is not None else VaultNotesSource()
    try:
        stats = await Indexer(pool, source, provider).reindex(full=full)
    finally:
        await pool.close()

    _print_stats(stats)
    return 1 if stats.failed else 0


def _print_stats(stats: IndexStats) -> None:
    print(
        f"indexed={stats.indexed} unchanged={stats.unchanged} "
        f"deleted={stats.deleted} failed={stats.failed}"
    )


def _run_eval_command(
    golden: Path, vault_dir: Path, baseline: Path, *, update_baseline: bool, k: int
) -> int:
    admin_url = os.environ.get("MM_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not admin_url:
        print("MM_TEST_DATABASE_URL or DATABASE_URL is required", file=sys.stderr)
        return 2
    if not update_baseline and not baseline.exists():
        print(f"{baseline} does not exist; run with --update-baseline first", file=sys.stderr)
        return 2

    try:
        embedding_config = EmbeddingConfig.from_env(dict(os.environ))
    except EmbeddingConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    return asyncio.run(
        _eval(
            admin_url,
            vault_dir,
            golden,
            baseline,
            embedding_config,
            update_baseline=update_baseline,
            k=k,
        )
    )


async def _eval(
    admin_url: str,
    vault_dir: Path,
    golden_path: Path,
    baseline_path: Path,
    embedding_config: EmbeddingConfig,
    *,
    update_baseline: bool,
    k: int,
) -> int:
    golden = load_golden(golden_path)
    provider = provider_from_config(embedding_config)

    eval_db_url = await _create_eval_database(admin_url)
    try:
        migration_conn = await asyncpg.connect(eval_db_url)
        try:
            await migrate(migration_conn)
        finally:
            await migration_conn.close()

        pool = await asyncpg.create_pool(eval_db_url)
        try:
            await Indexer(pool, vault_dir, provider).reindex(full=True)
            report = await run_eval(pool, golden, provider=provider, k=k)
        finally:
            await pool.close()
    finally:
        await _drop_eval_database(admin_url, eval_db_url)

    _print_eval_report(report)

    if update_baseline:
        _write_baseline(baseline_path, report, embedding_config.provider)
        print(f"baseline written to {baseline_path}")
        return 0

    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    regressions = compare(report, baseline)
    for message in regressions:
        print(f"REGRESSION: {message}")
    return 1 if regressions else 0


async def _create_eval_database(admin_url: str) -> str:
    """Create a freshly named `mm_eval_<random>` database and return its URL."""
    db_name = f"mm_eval_{secrets.token_hex(8)}"
    admin_conn = await asyncpg.connect(admin_url)
    try:
        await admin_conn.execute(f'create database "{db_name}"')
    finally:
        await admin_conn.close()

    base, _, _ = admin_url.rpartition("/")
    return f"{base}/{db_name}"


async def _drop_eval_database(admin_url: str, eval_db_url: str) -> None:
    db_name = eval_db_url.rpartition("/")[-1]
    admin_conn = await asyncpg.connect(admin_url)
    try:
        await admin_conn.execute(
            "select pg_terminate_backend(pid) from pg_stat_activity "
            "where datname = $1 and pid <> pg_backend_pid()",
            db_name,
        )
        await admin_conn.execute(f'drop database if exists "{db_name}"')
    finally:
        await admin_conn.close()


def _print_eval_report(report: EvalReport) -> None:
    print(f"overall  recall@{report.k}={report.recall_at_k:.4f}  mrr={report.mrr:.4f}")
    for kind, metrics in report.per_kind.items():
        print(
            f"  {kind:<14} n={metrics.count:<3} "
            f"recall@{report.k}={metrics.recall_at_k:.4f}  mrr={metrics.mrr:.4f}"
        )

    misses = [result for result in report.per_query if result.recall < 1.0]
    if misses:
        print(f"misses ({len(misses)}):")
        for result in misses:
            print(
                f"  {result.golden.id} [{result.golden.kind}] {result.golden.query!r} "
                f"expected={list(result.golden.expected)} hits={list(result.hits)}"
            )


def _write_baseline(path: Path, report: EvalReport, provider: str) -> None:
    data = {
        "k": report.k,
        "recall_at_k": round(report.recall_at_k, 4),
        "mrr": round(report.mrr, 4),
        "provider": provider,
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _serve(*, stdio: bool, http: bool) -> int:
    if stdio == http:
        print("serve: pass exactly one of --stdio or --http", file=sys.stderr)
        return 2

    # stdout is the stdio transport's protocol channel - every log line must
    # go to stderr, never stdout (a stray `print()` would corrupt the wire).
    # Harmless but kept the same for --http: nothing here relies on stdout
    # staying clean, logs just belong together regardless of transport.
    # `configure_logging_from_env` (#43) always logs to stderr too; it also
    # reads `LOG_LEVEL`/`LOG_FORMAT` (default `INFO`/`json`, the container
    # default) instead of hardcoding both the way `logging.basicConfig` did.
    configure_logging_from_env(os.environ)

    try:
        if stdio:
            return asyncio.run(_serve_stdio())
        return asyncio.run(_serve_http())
    except (VaultConfigError, EmbeddingConfigError, ServerConfigError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


async def _serve_stdio() -> int:
    async with open_services(os.environ) as services:
        server = build_server(services)
        await server.run_stdio_async()
    return 0


async def _serve_http() -> int:
    config = ServerConfig.from_env(dict(os.environ))
    # Mirrors `http.py`'s own condition for turning bearer-token auth on:
    # `DATABASE_URL` is what `Services.pool` ends up set from, and `/mcp` is
    # authenticated exactly when that is set (#34). Without it, there is no
    # `static_tokens` table to verify a token against at all.
    database_configured = bool(os.environ.get("DATABASE_URL"))

    if (
        not database_configured
        and not _is_loopback(config.host)
        and os.environ.get(_ALLOW_UNAUTHENTICATED_ENV) != "1"
    ):
        print(
            f"serve --http: refusing to bind to non-loopback host {config.host!r} - no "
            f"DATABASE_URL is set, so the HTTP transport has no authentication at all "
            f"(#34); set DATABASE_URL to turn bearer-token auth on for /mcp, or "
            f"{_ALLOW_UNAUTHENTICATED_ENV}=1 to bind anyway (every MCP tool and the vault "
            "webhook is then reachable by anyone who can reach the port)",
            file=sys.stderr,
        )
        return 2
    if not database_configured and not _is_loopback(config.host):
        _logger.warning(
            "serve --http: binding to non-loopback host %r with %s=1 and no DATABASE_URL - "
            "no authentication is in effect, every request reaches the MCP tools and the "
            "vault webhook",
            config.host,
            _ALLOW_UNAUTHENTICATED_ENV,
        )

    authenticator = build_authenticator(config, os.environ)
    app = create_app(lambda: open_services(os.environ), config, authenticator=authenticator)
    uvicorn_config = uvicorn.Config(
        app,
        host=config.host,
        port=config.port,
        log_config=None,
        proxy_headers=True,
        # `ServerConfig.forwarded_allow_ips` (`FORWARDED_ALLOW_IPS`, default
        # `127.0.0.1`, #39) - which proxy hop uvicorn's own
        # `ProxyHeadersMiddleware` trusts `X-Forwarded-For` from before
        # rewriting `scope["client"]`. That value is what `http.py`'s
        # `_LimitsMiddleware` keys its IP-based rate limits on (and what
        # `_vault_webhook`'s signature check logs as the caller); trusting
        # every hop (the old hardcoded `"*"`) would let a request spoof
        # `X-Forwarded-For` to pick its own rate-limit bucket, or collapse
        # every client behind a real proxy onto that proxy's one bucket.
        forwarded_allow_ips=config.forwarded_allow_ips,
        # `ServerConfig.shutdown_grace_seconds` (`SHUTDOWN_GRACE_SECONDS`,
        # default 20, ADR-0009 §1/§5) - uvicorn's own default is `None`
        # (wait forever), which would leave only an orchestrator's SIGKILL
        # to bound a draining shutdown.
        timeout_graceful_shutdown=config.shutdown_grace_seconds,
    )
    server = GracefulShutdownServer(uvicorn_config)
    await server.serve()
    return 0


def _is_loopback(host: str) -> bool:
    return host in _LOOPBACK_HOSTS


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise _MissingEnvironment(f"{name} is required but not set")
    return value


if __name__ == "__main__":
    sys.exit(main())
