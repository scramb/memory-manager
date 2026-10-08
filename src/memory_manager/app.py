# SPDX-License-Identifier: AGPL-3.0-only
"""Assembles the services a running memory-manager process needs (#17).

`open_services` is the one place that turns the process environment into
the configured `STORAGE_BACKEND` (ADR-0007), ready to serve. For the
default `"git"` backend, that means a working `Repo` + `WriteQueue`, synced
to the remote, with the derived Postgres index wired in when `DATABASE_URL`
is set: a startup reindex brings the index in step with whatever the vault
currently holds, a write-queue hook keeps it in step with every write this
process makes, a sync hook keeps it in step with every change a sync - any
sync - picks up, and a `vault.sync.poll_loop` is what keeps triggering syncs
on a timer. Without `DATABASE_URL` the vault and write queue still come up -
full-text/vector search and `memory_search` degrade, note read/write do not
(`CLAUDE.md`: Postgres is a derived index, never the only place a client's
data lives, for this backend).

For `"postgres"` (enterprise mode, ADR-0007 §2/§4/§7, WP-18), `DATABASE_URL`
is required (`storage_backend_from_env` enforces this before anything else
runs) and there is no vault at all: no clone, no `Repo`/`WriteQueue`, no
poll loop, no vault webhook (ADR-0009) - `Services.repo`/`queue`/
`vault_root` are all `None`, and `Services.pool` is the
`storage.postgres.PostgresBackend`'s own connection pool rather than one
`_open_index` builds separately. Indexing (`Services.indexer`) is wired in
too (#98), but differently from `"git"`: no startup reindex (there is no
"first request" lag to cover - every write indexes itself, in the same
transaction, before `PostgresBackend` ever returns from it) and no
sync/write-queue hooks (there is no queue) - instead, an `index.indexer.Indexer`
built over `vault_notes` is handed to `PostgresBackend` itself as its
`index_hook`/`index_commit_hook` (ADR-0007 §4), so `memory_search` can use
`search.hybrid_search` exactly like `"git"` with `DATABASE_URL` does.

`Services` is what the MCP tool layer (`mcp/server.py`) and the write tools
(#18/#19) are built against; nothing outside this module touches
`asyncpg`/`Indexer` construction directly.

Every `repo.sync()`/commit/push/rebase/reset in a `"git"`-backend process
goes through `WriteQueue`'s one consumer (#33): the poll loop's timer tick,
the HTTP transport's vault webhook (`Services.trigger_sync`, `http.py`),
and the pre-write sync inside every `queue.submit()` all end up calling
`queue.sync()`/being served by the same consumer task, so none of them
ever runs a working-copy operation concurrently with another - the
single-writer rule `docs/PLAN.md` describes, covering every operation on
the clone, not just writes. `Services.trigger_sync` is `None` on a
`"postgres"`-backend `Services` (there is nothing to sync) and on a
`Services` built by hand rather than through `open_services` (stdio-era
test fixtures); only the webhook calls it, and only on a `Services`
`open_services` built for the `"git"` backend.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import asyncpg

from memory_manager.audit import AuditWriter
from memory_manager.config import (
    EmbeddingConfig,
    VaultConfig,
    blocklist_file_from_env,
    database_app_role_from_env,
    storage_backend_from_env,
)
from memory_manager.db import rls
from memory_manager.db.migrate import migrate
from memory_manager.index.embeddings import EmbeddingProvider, provider_from_config
from memory_manager.index.indexer import Indexer, VaultNotesSource
from memory_manager.queue import (
    AuditHook,
    BlocklistRejected,
    EditMismatch,
    InvalidNote,
    NotFound,
    SecretRejected,
    SyncHook,
    VersionConflict,
    WriteConflict,
    WriteError,
    WriteFailed,
    WriteHook,
    WriteQueue,
    WriteRequest,
    WriteResult,
)
from memory_manager.storage.base import StorageBackend
from memory_manager.storage.git import GitBackend
from memory_manager.storage.postgres import PostgresBackend
from memory_manager.vault import blocklist
from memory_manager.vault.repo import Repo
from memory_manager.vault.sync import ChangeSet, poll_loop

__all__ = ["Services", "open_services", "open_storage"]

_logger = logging.getLogger(__name__)


@dataclass
class Services:
    """Everything a running memory-manager process (MCP server, CLI) acts through.

    `repo`/`queue`/`vault_root` are `None` on a `"postgres"`-backend
    `Services` (ADR-0007 §2, WP-18): there is no vault, no clone, no
    working copy to point at. Every caller that reaches for one of them
    (`mcp/server.py`'s scan fallback, `http.py`'s `/readyz`) is a `"git"`
    backend-specific path and narrows accordingly.

    `app_role` (ADR-0008 addendum, #116) is the non-owner role every request
    transaction must switch to before touching a row-level-security-protected
    content table - set only for `"postgres"` (`database_app_role_from_env`
    requires it there); `None` for `"git"`, which has no RLS at all.
    `mcp/server.py`'s `memory_search` reads it to decide whether to run on a
    `db.rls.request_connection` (set) or the plain owner pool (`None`) - the
    same switch `storage.postgres.PostgresBackend` itself already made via its
    own `app_role` constructor argument, built with the identical value.
    """

    repo: Repo | None
    queue: WriteQueue | None
    vault_root: Path | None
    pool: asyncpg.Pool | None
    indexer: Indexer | None
    provider: EmbeddingProvider | None
    storage: StorageBackend
    trigger_sync: Callable[[], Awaitable[ChangeSet]] | None = None
    app_role: str | None = None


@dataclass
class _BackendHandle:
    """What `_open_backend` hands back: the backend, plus its backend-specific innards.

    `repo`/`queue` are `None` for `"postgres"`. `pool` is `None` for
    `"git"` (that backend's own index pool, if any, is `_open_index`'s
    separate concern) and the backend's own connection pool for
    `"postgres"` - `open_services` reuses it for the audit writer and
    `Services.pool` instead of opening a second one. `indexer`/`provider`
    are `"postgres"`-only too (ADR-0007 §4, WP-18): built here, over
    `vault_notes`, only when `_open_backend` was given an `embedding_config`
    (`open_services` always passes one; `open_storage`, which needs no
    index at all, does not) - `None`/`None` otherwise, same as `"git"`'s are
    until `open_services`'s own `_open_index` call sets them.
    """

    storage: StorageBackend
    repo: Repo | None
    queue: WriteQueue | None
    pool: asyncpg.Pool | None
    indexer: Indexer | None = None
    provider: EmbeddingProvider | None = None


@asynccontextmanager
async def _open_backend(
    backend_name: str,
    *,
    vault_config: VaultConfig | None,
    database_url: str | None,
    embedding_config: EmbeddingConfig | None = None,
    app_role: str | None = None,
) -> AsyncIterator[_BackendHandle]:
    """Build the `STORAGE_BACKEND` named by `backend_name`, torn down on exit.

    `"git"` needs `vault_config` (never `None` for it - `open_services`/
    `open_storage` only build one for this backend) and clones/syncs/starts
    a `WriteQueue` exactly as before. `"postgres"` needs `database_url`
    instead (already guaranteed non-`None` by `storage_backend_from_env`,
    ADR-0007 §2) and builds its own connection-pool-backed
    `storage.postgres.PostgresBackend` with no clone, no working copy and no
    `Repo`/`WriteQueue` at all (ADR-0007 §2/§7, ADR-0009: no clone, no poll,
    no vault webhook in this mode) - `repo`/`queue` come back `None`.

    `embedding_config`, given only by `open_services` (ADR-0007 §4, WP-18/#98),
    additionally builds an `index.indexer.Indexer` over `vault_notes` and wires
    it into the `PostgresBackend` as its `index_hook`/`index_commit_hook` -
    every write indexes itself in the same transaction, and embeddings for it
    are scheduled right after. `open_storage` (the import CLI's entry point)
    never passes one, so a `"postgres"` call from there stays index-free, same
    as it always was. Any `self._background_tasks` the indexer still has
    pending are drained (`Indexer.aclose`) before the pool closes.

    `app_role` (ADR-0008 addendum, #116) is `"postgres"`-only too: given only
    by `open_services` (`database_app_role_from_env` requires it there before
    this is ever called) and never by `open_storage`, which is a system job
    that keeps connecting as the owner (same ADR addendum: "Git mode and
    system jobs keep connecting as the owner"). When given, this runs
    `db.rls.check_app_role` then `db.rls.grant_app_role` against it on
    `migration_conn` - the owner connection that just ran `migrate` - before
    building `PostgresBackend` with it, so a misconfigured role is refused at
    startup, never on a request's first write. A migration owner that turns
    out to itself be a superuser only gets a warning (FORCE RLS has no effect
    on it, but every content access below still goes through the switched
    role regardless) - this process keeps starting, the risk is operational,
    not something to refuse on.
    """
    if backend_name == "postgres":
        if (
            database_url is None
        ):  # pragma: no cover - storage_backend_from_env already required this
            raise AssertionError("_open_backend('postgres', ...) called without a database_url")
        migration_conn = await asyncpg.connect(database_url)
        try:
            await migrate(migration_conn)
            if app_role is not None:
                owner_is_superuser = await migration_conn.fetchval(
                    "select rolsuper from pg_roles where rolname = current_user"
                )
                if owner_is_superuser:
                    _logger.warning(
                        "STORAGE_BACKEND=postgres: the migrating/owner role is a "
                        "superuser - FORCE ROW LEVEL SECURITY has no effect on it, "
                        "relying entirely on every request transaction switching to "
                        "DATABASE_APP_ROLE=%r before touching content (ADR-0008 addendum)",
                        app_role,
                    )
                await rls.check_app_role(migration_conn, app_role)
                await rls.grant_app_role(migration_conn, app_role)
        finally:
            await migration_conn.close()

        pool = await asyncpg.create_pool(database_url)
        indexer: Indexer | None = None
        provider: EmbeddingProvider | None = None
        if embedding_config is not None:
            provider = provider_from_config(embedding_config)
            indexer = Indexer(pool, VaultNotesSource(), provider)

        storage = PostgresBackend(
            pool,
            index_hook=indexer.index_on_connection if indexer is not None else None,
            index_commit_hook=indexer.schedule_embeddings if indexer is not None else None,
            app_role=app_role,
        )
        try:
            yield _BackendHandle(
                storage=storage,
                repo=None,
                queue=None,
                pool=pool,
                indexer=indexer,
                provider=provider,
            )
        finally:
            if indexer is not None:
                await indexer.aclose()
            await pool.close()
        return

    if backend_name != "git":  # pragma: no cover - storage_backend_from_env already rejected this
        raise AssertionError(f"unknown storage backend {backend_name!r}")
    if vault_config is None:  # pragma: no cover - open_services/open_storage always build one
        raise AssertionError("_open_backend('git', ...) called without a vault_config")

    repo = Repo(vault_config)
    await asyncio.to_thread(repo.ensure_clone)
    await asyncio.to_thread(repo.sync)

    queue = WriteQueue(repo)
    await queue.start()
    try:
        yield _BackendHandle(
            storage=GitBackend(queue, repo, vault_config.dir), repo=repo, queue=queue, pool=None
        )
    finally:
        await queue.stop()


@asynccontextmanager
async def open_storage(environ: Mapping[str, str]) -> AsyncIterator[StorageBackend]:
    """Build the configured `StorageBackend` from `environ`, torn down on exit.

    The import CLI's (`memory-manager import ...`) entry point into the
    vault: just read/write access to `STORAGE_BACKEND`, synced once on entry
    - no Postgres index, audit log or poll loop, unlike `open_services`.
    Raises `VaultConfigError`/`StorageConfigError` if the matching
    environment variables are missing or malformed, before anything is
    cloned. `STORAGE_BACKEND` is read first, and `VaultConfig` is only ever
    built for the `"git"` backend - a `"postgres"` call needs no `VAULT_*`
    variable at all (ADR-0007 §2). Also loads `BLOCKLIST_FILE` eagerly
    (`vault.blocklist.load_rules`), so a malformed file raises
    `vault.blocklist.BlocklistConfigError` here too, before anything is
    cloned, rather than surfacing as a confusing rejection on the first
    write that happens to hit it (#244).
    """
    storage_backend_name = storage_backend_from_env(dict(environ))
    vault_config = VaultConfig.from_env(dict(environ)) if storage_backend_name == "git" else None
    blocklist.load_rules(blocklist_file_from_env(dict(environ)))
    database_url = environ.get("DATABASE_URL")
    async with _open_backend(
        storage_backend_name, vault_config=vault_config, database_url=database_url
    ) as handle:
        yield handle.storage


@asynccontextmanager
async def open_services(environ: Mapping[str, str]) -> AsyncIterator[Services]:
    """Build `Services` from `environ` and tear everything down on exit.

    Raises `VaultConfigError`/`EmbeddingConfigError`/`StorageConfigError` if the
    matching `VAULT_*`/`EMBEDDING_*`/`STORAGE_BACKEND` variables are missing or
    malformed; these surface before anything is started - `STORAGE_BACKEND`
    is read first, and `VaultConfig` is only ever built for the `"git"`
    backend (ADR-0007 §2: a `"postgres"` call needs no `VAULT_*` variable at
    all). For `"git"` with `environ["DATABASE_URL"]` set, also migrates and
    reindexes the Postgres index before yielding, so the index is never
    stale behind the vault for the first request. For `"postgres"`, there is
    no comparable startup reindex (ADR-0007 §4, WP-18/#98: every write already
    indexes itself, so there is nothing to catch up on at startup) - only the
    audit hook is wired onto the backend in addition to what `_open_backend`
    already wired in as `index_hook`/`index_commit_hook`, and `Services.pool`
    is the backend's own pool. `app_role` (ADR-0008 addendum, #116) is read
    here too (`database_app_role_from_env`, `None` for `"git"`, required and
    validated before anything else starts for `"postgres"`) and threaded
    through `_open_backend` into `PostgresBackend` and `Services.app_role`
    alike. `BLOCKLIST_FILE` is loaded eagerly here too (`vault.blocklist.
    load_rules`), same "fails before anything is started" contract as the
    rest of this list (#244).
    """
    storage_backend_name = storage_backend_from_env(dict(environ))
    database_url = environ.get("DATABASE_URL")
    vault_config = VaultConfig.from_env(dict(environ)) if storage_backend_name == "git" else None
    embedding_config = EmbeddingConfig.from_env(dict(environ))
    app_role = database_app_role_from_env(dict(environ))
    blocklist.load_rules(blocklist_file_from_env(dict(environ)))

    async with _open_backend(
        storage_backend_name,
        vault_config=vault_config,
        database_url=database_url,
        embedding_config=embedding_config,
        app_role=app_role,
    ) as handle:
        pool: asyncpg.Pool | None = handle.pool
        index_pool: asyncpg.Pool | None = None
        indexer: Indexer | None = None
        provider: EmbeddingProvider | None = None
        trigger_sync: Callable[[], Awaitable[ChangeSet]] | None = None
        poll_task: asyncio.Task[None] | None = None
        poll_stop: asyncio.Event | None = None

        try:
            if storage_backend_name == "git":
                queue = handle.queue
                if queue is None or vault_config is None:
                    raise AssertionError(  # pragma: no cover - _open_backend's own invariant
                        "_open_backend('git', ...) returned no queue/vault_config"
                    )
                if database_url:
                    pool, indexer, provider = await _open_index(
                        database_url, vault_config.dir, embedding_config
                    )
                    index_pool = pool
                    queue.add_hook(_index_write_hook(indexer))
                    queue.add_sync_hook(_index_sync_hook(indexer))
                    queue.add_audit_hook(_audit_write_hook(AuditWriter(pool)))

                poll_stop = asyncio.Event()
                poll_task = asyncio.create_task(
                    poll_loop(queue.sync, vault_config.poll_seconds, stop=poll_stop)
                )
                trigger_sync = queue.sync
            elif pool is None or not isinstance(handle.storage, PostgresBackend):
                raise AssertionError(  # pragma: no cover - _open_backend's own invariant
                    "_open_backend('postgres', ...) returned no pool/PostgresBackend"
                )
            else:
                indexer = handle.indexer
                provider = handle.provider
                handle.storage.add_audit_hook(_audit_write_hook(AuditWriter(pool)))

            try:
                yield Services(
                    repo=handle.repo,
                    queue=handle.queue,
                    vault_root=vault_config.dir if vault_config is not None else None,
                    pool=pool,
                    indexer=indexer,
                    provider=provider,
                    storage=handle.storage,
                    trigger_sync=trigger_sync,
                    app_role=app_role,
                )
            finally:
                if poll_task is not None:
                    if poll_stop is None:
                        raise AssertionError(  # pragma: no cover - set together, just above
                            "poll_task is set but poll_stop is None"
                        )
                    poll_stop.set()
                    poll_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await poll_task
        finally:
            if index_pool is not None:
                await index_pool.close()


def _index_sync_hook(indexer: Indexer) -> SyncHook:
    """`WriteQueue.add_sync_hook`'s hook: keep the index in step with every sync (#33).

    Registered on the queue, not passed to `poll_loop`: every sync the
    consumer ever runs - the poll loop's timer tick, the webhook's
    `trigger_sync()` (`queue.sync`), and the pre-write sync inside every
    `submit()` - ends up here exactly once for a given human change,
    whichever of those three happens to pick it up first.
    """

    async def hook(change_set: ChangeSet) -> None:
        await indexer.apply_changeset(change_set)

    return hook


async def _open_index(
    database_url: str, vault_root: Path, embedding_config: EmbeddingConfig
) -> tuple[asyncpg.Pool, Indexer, EmbeddingProvider | None]:
    """Migrate, build the `Indexer` and bring it in step with the vault at startup."""
    migration_conn = await asyncpg.connect(database_url)
    try:
        await migrate(migration_conn)
    finally:
        await migration_conn.close()

    provider = provider_from_config(embedding_config)
    pool = await asyncpg.create_pool(database_url)
    indexer = Indexer(pool, vault_root, provider)
    stats = await indexer.reindex()
    _logger.info(
        "startup reindex: indexed=%d unchanged=%d deleted=%d failed=%d",
        stats.indexed,
        stats.unchanged,
        stats.deleted,
        stats.failed,
    )
    return pool, indexer, provider


def _index_write_hook(indexer: Indexer) -> WriteHook:
    async def hook(result: object, request: object, changed_paths: tuple[str, ...]) -> None:
        await indexer.index_paths(changed_paths)

    return hook


def _audit_write_hook(audit: AuditWriter) -> AuditHook:
    """`WriteQueue.add_audit_hook`'s hook: one `audit_log` row per processed write (#39).

    `detail` is built here, from op-level metadata only - never from
    `request.content`/`old_str`/`new_str` or an error's `current_content`
    (`AuditWriter`'s docstring: that is the one place a note's text could
    leak into the audit log).
    """

    async def hook(
        request: WriteRequest, result: WriteResult | None, error: Exception | None
    ) -> None:
        outcome, detail = _audit_outcome(result, error)
        await audit.record(
            actor=request.actor,
            client=request.client,
            op=request.op,
            path=result.path if result is not None else request.path,
            commit_sha=result.commit if result is not None else None,
            outcome=outcome,
            detail=detail,
        )

    return hook


def _audit_outcome(
    result: WriteResult | None, error: Exception | None
) -> tuple[str, dict[str, object]]:
    """The `(outcome, detail)` pair `_audit_write_hook` records for one write.

    `outcome` is one of `"ok"`/`"conflict"`/`"rejected"`/`"failed"`.
    `detail` never carries `current_content`/`current_version`-adjacent note
    text, only a version, an error class name, a conflict file path, or -
    for `BlocklistRejected` - the matched category's name, never the text
    that matched it (#244).
    """
    if error is None:
        if result is None:  # pragma: no cover - defensive, a write-queue invariant
            return "failed", {"error": "unknown"}
        return "ok", {"version": result.version}
    if isinstance(error, VersionConflict):
        return "conflict", {"error": "VersionConflict", "current_version": error.current_version}
    if isinstance(error, WriteConflict):
        return "conflict", {
            "error": "WriteConflict",
            "conflict_path": error.conflict_path,
            "current_version": error.current_version,
        }
    if isinstance(error, BlocklistRejected):
        return "rejected", {"error": "BlocklistRejected", "category": error.category}
    if isinstance(error, (InvalidNote, EditMismatch, NotFound, SecretRejected)):
        return "rejected", {"error": type(error).__name__}
    if isinstance(error, WriteFailed):
        return "failed", {"error": "WriteFailed"}
    if isinstance(error, WriteError):  # pragma: no cover - defensive, no other WriteError today
        return "failed", {"error": type(error).__name__}
    # Not a `WriteError` at all - a bug elsewhere in the write path, not a
    # client-facing rejection; still audited, as "failed" with no further
    # detail guessed about an exception type this was never written for.
    return "failed", {"error": type(error).__name__}  # pragma: no cover - defensive
