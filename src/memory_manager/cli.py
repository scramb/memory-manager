# SPDX-License-Identifier: AGPL-3.0-only
"""The `memory-manager` command-line entry point (#26).

`reindex`, `doctor`, `eval` and `export` exist so far. Other subcommands
(vault sync, search, ...) are added as their own tasks wire the server
together (M2/M4).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path

import asyncpg

from memory_manager.config import EmbeddingConfig, EmbeddingConfigError, VaultConfigError
from memory_manager.db.migrate import migrate
from memory_manager.doctor import DoctorReport, run_doctor
from memory_manager.eval import EvalReport, compare, load_golden, run_eval
from memory_manager.exporter import ExportError, Manifest, export_vault
from memory_manager.importers import ImportReport, dedupe_against_vault, open_queue, run_import
from memory_manager.importers.chatgpt import ChatGPTFormatError
from memory_manager.importers.chatgpt import collect as collect_chatgpt
from memory_manager.importers.claude import ClaudeFormatError
from memory_manager.importers.claude import collect as collect_claude
from memory_manager.importers.markdown import collect as collect_markdown
from memory_manager.index.embeddings import provider_from_config
from memory_manager.index.indexer import Indexer, IndexStats
from memory_manager.vault.validate import NOTE_TYPES

__all__ = ["main"]

_DEFAULT_GOLDEN = Path("eval/golden.yaml")
_DEFAULT_EVAL_VAULT = Path("examples/vault")
_DEFAULT_BASELINE = Path("eval/baseline.json")
_DEFAULT_EVAL_K = 5


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

    if args.command != "reindex":
        parser.print_help()
        return 1

    try:
        database_url = _require_env("DATABASE_URL")
        vault_dir = Path(_require_env("VAULT_DIR"))
        embedding_config = EmbeddingConfig.from_env(dict(os.environ))
    except (_MissingEnvironment, EmbeddingConfigError) as exc:
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

    return parser


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

    try:
        repo, queue, _vault_dir = await open_queue(os.environ)
    except VaultConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        items, pre_rejected = collect_markdown(
            directory, namespace=namespace, default_type=default_type
        )
        report = await run_import(items, queue, repo, apply=apply)
        report.rejected = pre_rejected + report.rejected
    finally:
        await queue.stop()

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
        repo, queue, vault_dir = await open_queue(os.environ)
    except VaultConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        await asyncio.to_thread(repo.sync)
        kept_items, duplicates = dedupe_against_vault(items, vault_dir)
        report = await run_import(kept_items, queue, repo, apply=apply)
        report.rejected = pre_rejected + report.rejected
        report.duplicates = duplicates + report.duplicates
    finally:
        await queue.stop()

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
        repo, queue, vault_dir = await open_queue(os.environ)
    except VaultConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        await asyncio.to_thread(repo.sync)
        kept_items, duplicates = dedupe_against_vault(items, vault_dir)
        report = await run_import(kept_items, queue, repo, apply=apply)
        report.rejected = pre_rejected + report.rejected
        report.duplicates = duplicates + report.duplicates
    finally:
        await queue.stop()

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


async def _reindex(
    database_url: str, vault_dir: Path, embedding_config: EmbeddingConfig, *, full: bool
) -> int:
    # A plain connection for the migration, not one from the pool below:
    # `migrate` takes an `asyncpg.Connection`, not a pool's connection proxy.
    migration_conn = await asyncpg.connect(database_url)
    try:
        await migrate(migration_conn)
    finally:
        await migration_conn.close()

    provider = provider_from_config(embedding_config)
    pool = await asyncpg.create_pool(database_url)
    try:
        stats = await Indexer(pool, vault_dir, provider).reindex(full=full)
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


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise _MissingEnvironment(f"{name} is required but not set")
    return value


if __name__ == "__main__":
    sys.exit(main())
