# SPDX-License-Identifier: AGPL-3.0-only
"""Assembles the services a running memory-manager process needs (#17).

`open_services` is the one place that turns the process environment into a
working `Repo` + `WriteQueue`, synced to the remote, with the derived
Postgres index wired in when `DATABASE_URL` is set: a startup reindex brings
the index in step with whatever the vault currently holds, a write-queue
hook keeps it in step with every write this process makes, a sync hook
keeps it in step with every change a sync - any sync - picks up, and a
`vault.sync.poll_loop` is what keeps triggering syncs on a timer. Without
`DATABASE_URL` the vault and write queue still come up - full-text/vector
search and `memory_search` degrade, note read/write do not (`CLAUDE.md`:
Postgres is a derived index, never the only place a client's data lives).

`Services` is what the MCP tool layer (`mcp/server.py`) and future write
tools (#18/#19) are built against; nothing outside this module touches
`asyncpg`/`Indexer` construction directly.

Every `repo.sync()`/commit/push/rebase/reset in this process goes through
`WriteQueue`'s one consumer (#33): the poll loop's timer tick, the HTTP
transport's vault webhook (`Services.trigger_sync`, `http.py`), and the
pre-write sync inside every `queue.submit()` all end up calling
`queue.sync()`/being served by the same consumer task, so none of them
ever runs a working-copy operation concurrently with another - the
single-writer rule `docs/PLAN.md` describes, covering every operation on
the clone, not just writes. `Services.trigger_sync` is `None` only on a
`Services` built by hand rather than through `open_services` (stdio-era
test fixtures); only the webhook calls it, and only on a `Services`
`open_services` built.
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
from memory_manager.config import EmbeddingConfig, VaultConfig, storage_backend_from_env
from memory_manager.db.migrate import migrate
from memory_manager.index.embeddings import EmbeddingProvider, provider_from_config
from memory_manager.index.indexer import Indexer
from memory_manager.queue import (
    AuditHook,
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
from memory_manager.vault.repo import Repo
from memory_manager.vault.sync import ChangeSet, poll_loop

__all__ = ["Services", "open_services", "open_storage"]

_logger = logging.getLogger(__name__)


@dataclass
class Services:
    """Everything a running memory-manager process (MCP server, CLI) acts through."""

    repo: Repo
    queue: WriteQueue
    vault_root: Path
    pool: asyncpg.Pool | None
    indexer: Indexer | None
    provider: EmbeddingProvider | None
    storage: StorageBackend
    trigger_sync: Callable[[], Awaitable[ChangeSet]] | None = None


@asynccontextmanager
async def _open_backend(
    backend_name: str, vault_config: VaultConfig
) -> AsyncIterator[tuple[StorageBackend, Repo, WriteQueue]]:
    """Build the `STORAGE_BACKEND` named by `backend_name`, torn down on exit.

    Only `"git"` exists today - `storage_backend_from_env` already rejected
    anything else before this is ever called. `repo`/`queue` are `GitBackend`'s
    own innards, handed back alongside it so `open_services`'s hooks, indexer,
    poll loop and webhook can keep reaching the working copy directly
    (ADR-0007 §1: the Git backend is unchanged, single-writer, derived index).
    A future `"postgres"` branch (WP-18) would yield its own pool-backed
    handle here instead; `open_storage`'s callers, which only ever use the
    `StorageBackend` itself, would not change at all.
    """
    if backend_name != "git":  # pragma: no cover - storage_backend_from_env already rejected this
        raise AssertionError(f"unknown storage backend {backend_name!r}")

    repo = Repo(vault_config)
    await asyncio.to_thread(repo.ensure_clone)
    await asyncio.to_thread(repo.sync)

    queue = WriteQueue(repo)
    await queue.start()
    try:
        yield GitBackend(queue, repo, vault_config.dir), repo, queue
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
    cloned.
    """
    vault_config = VaultConfig.from_env(dict(environ))
    storage_backend_name = storage_backend_from_env(dict(environ))
    async with _open_backend(storage_backend_name, vault_config) as (storage, _repo, _queue):
        yield storage


@asynccontextmanager
async def open_services(environ: Mapping[str, str]) -> AsyncIterator[Services]:
    """Build `Services` from `environ` and tear everything down on exit.

    Raises `VaultConfigError`/`EmbeddingConfigError`/`StorageConfigError` if the
    matching `VAULT_*`/`EMBEDDING_*`/`STORAGE_BACKEND` variables are missing or
    malformed; these surface before anything is started. With
    `environ["DATABASE_URL"]` set, also migrates and reindexes the Postgres
    index before yielding, so the index is never stale behind the vault for
    the first request.
    """
    vault_config = VaultConfig.from_env(dict(environ))
    embedding_config = EmbeddingConfig.from_env(dict(environ))
    storage_backend_name = storage_backend_from_env(dict(environ))
    database_url = environ.get("DATABASE_URL")

    async with _open_backend(storage_backend_name, vault_config) as (storage, repo, queue):
        pool: asyncpg.Pool | None = None
        indexer: Indexer | None = None
        provider: EmbeddingProvider | None = None

        try:
            if database_url:
                pool, indexer, provider = await _open_index(
                    database_url, vault_config.dir, embedding_config
                )
                queue.add_hook(_index_write_hook(indexer))
                queue.add_sync_hook(_index_sync_hook(indexer))
                queue.add_audit_hook(_audit_write_hook(AuditWriter(pool)))

            poll_stop = asyncio.Event()
            poll_task = asyncio.create_task(
                poll_loop(queue.sync, vault_config.poll_seconds, stop=poll_stop)
            )
            try:
                yield Services(
                    repo=repo,
                    queue=queue,
                    vault_root=vault_config.dir,
                    pool=pool,
                    indexer=indexer,
                    provider=provider,
                    storage=storage,
                    trigger_sync=queue.sync,
                )
            finally:
                poll_stop.set()
                poll_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await poll_task
        finally:
            if pool is not None:
                await pool.close()


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
    text, only a version, an error class name, or a conflict file path.
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
