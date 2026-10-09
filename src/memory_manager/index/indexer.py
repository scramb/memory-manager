# SPDX-License-Identifier: AGPL-3.0-only
"""Keep the Postgres index in step with the vault (#26) or `vault_notes` (#98).

`Indexer.index_paths` is the one place that turns a batch of vault-relative
paths into the matching `notes`/`chunks`/`links` rows: it compares each
note's current `file_hash` against the one on record, skips unchanged
files, and upserts the rest in one transaction per note so a crash never
leaves a note half-written. `apply_changeset` feeds it a `vault.sync`
result; `reindex_full` feeds it every note path currently in the vault and
then removes rows for paths that no longer exist - the only way the index
is guaranteed rebuildable from the backend's source of truth alone
(`CLAUDE.md`). Where that source of truth lives is `self._source`: a
`Path` constructor argument builds a `_FileTreeSource` (the `"git"`
backend's working copy); `VaultNotesSource()` builds a `_VaultNotesReader`
instead (the `"postgres"` backend's `vault_notes`, ADR-0007 §2) - every
other method reads through `self._source` and never cares which one it got.

Link resolution only ever needs the handful of notes a reference's slug or
alias could possibly match, not the whole `notes` table: `_entries_for`
fetches exactly those, so neither `_refresh_links` (one note) nor
`_heal_dangling_links` (every currently-dangling link) ever reads a row
that could not have mattered. `_heal_dangling_links` runs after every
`index_paths` call and resolves anything that was dangling but now has a
target; `reindex_full` additionally recomputes links for every note once
all of them are in the index, so a forward reference is correct the first
time a vault is rebuilt from scratch.

Embedding a note's chunks (#27) happens best-effort right after it is
upserted, via the optional `provider` constructor argument - for the
`"git"` backend's batch paths (`index_paths`/`_index_one`), unchanged
(#219's "not included"). A missing provider or a failed embedding call
both leave `chunks.embedding` `NULL` - indexing itself never blocks on it.
`embed_pending`, called at the end of `reindex` for a `_FileTreeSource`
(the `"git"` backend), is the catch-up pass: it re-embeds every chunk
still missing an embedding or stamped with a model other than the
provider's current one, so a provider outage or a model change both
self-heal on the next reindex. For a `_VaultNotesReader` source (the
`"postgres"` backend), `reindex` skips that call: #219's worker-side
catch-up (`enqueue_stale_embeddings`) takes over that job instead.

`index_on_connection` is the connection-path entry point
`storage.postgres.PostgresBackend` wires in as its `IndexHook` (ADR-0007
§4, #98): a write indexes the note it just wrote on the *same* connection,
inside the *same* transaction as the write itself - no separate pool
acquisition, no second transaction - leaving the chunks it just wrote with
`embedding` `NULL`, same as every other upsert. Embedding them is not this
module's job to run inline any more (#219): on that same connection, still
inside that same transaction, `index_on_connection` enqueues one
`"embed_note"` job (`jobs.enqueue`, note id + `file_hash`, plus
`observability.tracing.current_traceparent()` so a worker span can continue
this request's own trace, #263 WP-31) for the worker process to pick up
once the write has committed - `jobs.enqueue`'s own `pg_notify` only ever
fires on commit, so a rolled-back write never wakes a worker for a job
that no longer exists either. `embed_note_job` is that job's handler
(`worker.py`'s `build_job_handlers`): skips if the note's `file_hash` has
already moved on (a newer write's own job will embed the current chunks
instead), otherwise embeds whatever is still stale and lets
`EmbeddingError` propagate so the worker retries with backoff, rather than
swallowing it the way the batch path's `_embed_note` does.

`schedule_embeddings`/`aclose` are this module's older, still-functional
background-task mechanism for the same `IndexCommitHook` seam
(`storage.base.IndexCommitHook`) - no longer wired by `app.py` (#219
replaced it with the job above), kept only because other callers
(`PostgresBackend(index_commit_hook=...)`) may still wire it in by hand.
"""

from __future__ import annotations

import asyncio
import enum
import hashlib
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import asyncpg
import asyncpg.pool

from memory_manager.config import EmbeddingDimensionPinError
from memory_manager.db.migrate import POSTGRES_VECTOR_LAYOUT_VERSION
from memory_manager.index.chunker import chunk_note
from memory_manager.index.embeddings import EmbeddingError, EmbeddingProvider
from memory_manager.jobs import enqueue as enqueue_job
from memory_manager.observability.tracing import current_traceparent
from memory_manager.vault.links import (
    LinkRef,
    ResolvedLink,
    VaultEntry,
    extract_links,
    resolve_links,
)
from memory_manager.vault.note import Note, NoteFormatError, parse, version
from memory_manager.vault.paths import NotePath, PathRejected, parse_note_path, resolve
from memory_manager.vault.sync import ChangeSet

__all__ = ["IndexStats", "Indexer", "VaultNotesSource"]

_logger = logging.getLogger(__name__)

# Every connection this module touches - whether acquired from the pool
# itself or handed in by a caller such as `PostgresBackend` - is an
# `asyncpg.pool.PoolConnectionProxy`, not a plain `Connection`.
_Conn = asyncpg.pool.PoolConnectionProxy

# How many stale chunks `embed_pending` re-embeds per round trip to the
# provider; keeps a single call's memory and request size bounded no matter
# how many chunks are waiting.
_EMBED_PENDING_BATCH_SIZE = 200

# How long `aclose` waits for background embedding tasks (`schedule_embeddings`)
# to finish on their own before cancelling whatever is still running.
_EMBED_DRAIN_TIMEOUT = 5.0


class VaultNotesSource:
    """Pass instead of a `Path` to `Indexer` to index from `vault_notes` (ADR-0007
    §2, WP-18) instead of a vault working copy - the `"postgres"` backend's source
    of truth. Carries no state of its own: `Indexer.__init__` builds the actual
    reader from its own `pool`.
    """


class _Source(Protocol):
    """Where `Indexer` reads note paths and bytes from - a vault or `vault_notes`."""

    async def discover_paths(self) -> list[str]:
        """Every note-shaped path the source currently has, in path order."""
        ...

    async def read(self, rel: str) -> bytes | None:
        """`rel`'s current bytes, or `None` if it does not exist.

        Raises `PathRejected` if `rel` is not a path this source could ever
        have - never for "exists, but did not resolve to anything",
        which is `None`, not an error.
        """
        ...


class _FileTreeSource:
    """Reads notes straight off a vault's working copy (the `"git"` backend)."""

    def __init__(self, vault_root: Path) -> None:
        self._vault_root = vault_root

    async def discover_paths(self) -> list[str]:
        paths: list[str] = []
        for file in sorted(self._vault_root.rglob("*.md")):
            rel_parts = file.relative_to(self._vault_root).parts
            if ".git" in rel_parts:
                continue
            rel = "/".join(rel_parts)
            if _is_note_path(rel):
                paths.append(rel)
        return paths

    async def read(self, rel: str) -> bytes | None:
        disk_path = resolve(self._vault_root, rel, allow_archive=True)
        if not disk_path.exists():
            return None
        return disk_path.read_bytes()


class _VaultNotesReader:
    """Reads notes from `vault_notes` (the `"postgres"` backend, ADR-0007 §2).

    `discover_paths` reads only `path`, never `content` - a full `reindex_full`
    never has to hold every note's bytes in memory at once, only the current one
    while it is being indexed.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def discover_paths(self) -> list[str]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("select path from vault_notes order by path")
        return [row["path"] for row in rows]

    async def read(self, rel: str) -> bytes | None:
        parse_note_path(rel, allow_archive=True)
        async with self._pool.acquire() as conn:
            content = await conn.fetchval("select content from vault_notes where path = $1", rel)
        return bytes(content) if content is not None else None


@dataclass(frozen=True)
class IndexStats:
    """Counts of what an `index_paths`/`reindex_full` call did."""

    indexed: int = 0
    unchanged: int = 0
    deleted: int = 0
    failed: int = 0


class _Outcome(enum.Enum):
    """What happened to one path inside `Indexer._index_one`."""

    INDEXED = "indexed"
    UNCHANGED = "unchanged"
    DELETED = "deleted"
    FAILED = "failed"
    NOOP = "noop"


class Indexer:
    """Builds and maintains the derived Postgres index from a vault or `vault_notes`."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        vault_root: Path | VaultNotesSource,
        provider: EmbeddingProvider | None = None,
    ) -> None:
        self._pool = pool
        self._source: _Source = (
            _VaultNotesReader(pool)
            if isinstance(vault_root, VaultNotesSource)
            else _FileTreeSource(vault_root)
        )
        self._provider = provider
        # Background `schedule_embeddings` tasks, held onto strongly so nothing
        # garbage-collects one mid-flight - `asyncio`'s own fire-and-forget
        # pitfall - and drained with a time limit by `aclose`.
        self._background_tasks: set[asyncio.Task[None]] = set()
        # Whether `self._pool`'s database has the ADR-0016 `chunks` layout
        # (`migrations/postgres/0012_vector_layout.sql`) - lazily resolved by
        # `_uses_vector_layout` (a schema never changes shape again once a
        # process is running) and cached here so every call after the first
        # costs nothing. `None` means "not checked yet", not "no".
        self._vector_layout: bool | None = None

    async def index_paths(self, paths: Iterable[str]) -> IndexStats:
        """Index or remove each of `paths`, skipping unchanged files.

        Non-note paths (per `vault.paths.parse_note_path`) are silently
        skipped. A note that fails to parse is logged and counted as
        `failed`; its previous row, if any, is left untouched. Every other
        note is indexed or removed in its own transaction, so one bad note
        never blocks the rest of the batch.
        """
        indexed = unchanged = deleted = failed = 0
        for rel in paths:
            if not _is_note_path(rel):
                continue
            outcome = await self._index_one(rel)
            if outcome is _Outcome.INDEXED:
                indexed += 1
            elif outcome is _Outcome.UNCHANGED:
                unchanged += 1
            elif outcome is _Outcome.DELETED:
                deleted += 1
            elif outcome is _Outcome.FAILED:
                failed += 1

        await self._heal_dangling_links()
        return IndexStats(indexed=indexed, unchanged=unchanged, deleted=deleted, failed=failed)

    async def apply_changeset(self, cs: ChangeSet) -> IndexStats:
        """Index every note path a vault sync added, modified or deleted."""
        return await self.index_paths((*cs.added, *cs.modified, *cs.deleted))

    async def reindex_full(self) -> IndexStats:
        """Rebuild the index from every note the source currently has."""
        return await self.reindex(full=True)

    async def reindex(self, *, full: bool = False) -> IndexStats:
        """Index every path the source currently has, skipping unchanged ones.

        With `full=True`, additionally removes rows for paths the source no
        longer has and recomputes links for every note, so a reference to a
        note indexed later in the same run still resolves. For a
        `_VaultNotesReader` source (the `"postgres"` backend), `full=True`
        also runs `ANALYZE chunks` once the rebuild is done (ADR-0007
        addendum #117, #220's own checklist): a bulk `reindex --full` is
        exactly the kind of mass rewrite that leaves `mm_frequent_lexemes`
        (0008_frequent_lexemes.sql) without statistics until something
        analyzes the table, and this is the one place in this module that
        already knows the rebuild just finished - `search.py`'s
        `hybrid_search` never has to wait for an unrelated autovacuum run
        before its candidate-stage cap works for a freshly rebuilt index.
        `ANALYZE chunks` on the partitioned parent (ADR-0016, #220)
        recurses into every partition on its own.

        The trailing `embed_pending` catch-up only runs for a
        `_FileTreeSource` (the `"git"` backend, #219's "not included"). For
        a `_VaultNotesReader` source, the worker process's own startup
        catch-up (`enqueue_stale_embeddings`) is what re-embeds a chunk left
        stale by a lost job, a provider outage or a model change - this
        method leaves embedding alone entirely.
        """
        discovered = await self._source.discover_paths()
        stats = await self.index_paths(discovered)
        if not full:
            if not isinstance(self._source, _VaultNotesReader):
                await self.embed_pending()
            return stats

        extra_deleted = await self._delete_stale(discovered)
        await self._recompute_all_links()
        if isinstance(self._source, _VaultNotesReader):
            await self._analyze_chunks()
        else:
            await self.embed_pending()
        return IndexStats(
            indexed=stats.indexed,
            unchanged=stats.unchanged,
            deleted=stats.deleted + extra_deleted,
            failed=stats.failed,
        )

    async def _analyze_chunks(self) -> None:
        """`ANALYZE chunks` (ADR-0007 addendum #117, #220) - `reindex(full=True)`'s
        own trailing step for a `_VaultNotesReader` source. Runs as the owner
        (`self._pool.acquire()`, never a caller-supplied, possibly
        role-switched connection): `ANALYZE` needs no row-level grant beyond
        `SELECT`, but is a system-maintenance statement system jobs run, not
        something a request-serving role should ever trigger.
        """
        async with self._pool.acquire() as conn:
            await conn.execute("analyze chunks")

    async def _delete_stale(self, keep_paths: Sequence[str]) -> int:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "delete from notes where not (path = any($1::text[]))", list(keep_paths)
            )
        return _parse_affected(result)

    async def _index_one(self, rel: str) -> _Outcome:
        try:
            data = await self._source.read(rel)
        except PathRejected as exc:
            _logger.warning("skipping %r, path is not safe to resolve: %s", rel, exc)
            return _Outcome.FAILED

        if data is None:
            return _Outcome.DELETED if await self._delete_by_path(rel) else _Outcome.NOOP

        file_hash = version(data)

        async with self._pool.acquire() as conn:
            existing_hash = await conn.fetchval("select file_hash from notes where path = $1", rel)
        if existing_hash == file_hash:
            return _Outcome.UNCHANGED

        try:
            note = parse(data)
        except NoteFormatError as exc:
            _logger.warning("skipping %r, failed to parse: %s", rel, exc)
            return _Outcome.FAILED

        async with self._pool.acquire() as conn, conn.transaction():
            await self._upsert_note_rows(conn, rel, note, file_hash)
        await self._embed_note(note.id)
        return _Outcome.INDEXED

    async def _delete_by_path(self, rel: str) -> bool:
        async with self._pool.acquire() as conn:
            result = await conn.execute("delete from notes where path = $1", rel)
        return _parse_affected(result) > 0

    async def index_on_connection(self, conn: _Conn, path: str, content: bytes) -> None:
        """Index `path`'s `content` on the caller's own `conn`, in its own transaction.

        The `storage.base.IndexHook` `storage.postgres.PostgresBackend` calls right
        after inserting a write's revision (ADR-0007 §4, #98): `conn` is already
        inside that write's transaction, so `notes`/`chunks`/`links` land in the
        exact same commit as the write itself, never a separate connection or
        transaction - a rolled-back write takes its index rows with it for free.
        Also heals dangling links elsewhere in the index that this note's slug,
        aliases or path can now resolve, scoped to just this note - never a scan
        of every dangling link in the index (`_heal_dangling_links`'s job, for the
        batch paths that actually need it).

        The chunks `_upsert_note_rows` just wrote are left with `embedding`
        `NULL`, same as always - this method does not embed them itself any
        more (#219). Instead, still on `conn` and still inside the caller's
        own transaction, it enqueues one `"embed_note"` job (`jobs.enqueue`)
        naming this note's id and the `file_hash` it was just written at, so
        a worker picks the job up once - and only once - this transaction has
        actually committed. A no-op without a configured provider: nothing
        would ever claim the job.
        """
        note = parse(content)
        file_hash = version(content)
        note_path = await self._upsert_note_rows(conn, path, note, file_hash)
        await self._heal_dangling_for_note(
            conn, note.id, note_path.namespace, note_path.slug, note.aliases
        )
        if self._provider is not None:
            await enqueue_job(
                conn,
                "embed_note",
                {"note_id": note.id, "version": file_hash},
                traceparent=current_traceparent(),
            )

    async def schedule_embeddings(self, note_ids: Sequence[str]) -> None:
        """Embed `note_ids`' chunks in the background, never awaited by the caller.

        The `storage.base.IndexCommitHook` `PostgresBackend` calls once its write's
        transaction has already committed (ADR-0007 §4): embedding must never slow
        down or fail a write, so this only ever starts a task - held in
        `self._background_tasks` so nothing garbage-collects it mid-flight - and
        returns immediately. Every exception the task could raise, not only
        `EmbeddingError`, is caught and logged inside it: a bug in the embedding
        path must never surface as an unhandled exception with nothing awaiting
        the task it came from. A no-op without a configured provider.
        """
        if self._provider is None:
            return
        for note_id in note_ids:
            task = asyncio.create_task(self._embed_note_safe(note_id))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

    async def _embed_note_safe(self, note_id: str) -> None:
        try:
            await self._embed_note(note_id)
        except Exception:
            _logger.exception("background embedding failed for note %r", note_id)

    async def aclose(self, *, timeout: float = _EMBED_DRAIN_TIMEOUT) -> None:
        """Await pending `schedule_embeddings` tasks, then cancel whatever remains.

        Called before the pool those tasks' connections came from is closed
        (`app.py`'s `"postgres"` branch): bounded by `timeout` instead of waiting
        forever for a slow or stuck embedding call to finish.
        """
        tasks = list(self._background_tasks)
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _upsert_note_rows(
        self, conn: _Conn, rel: str, note: Note, file_hash: str
    ) -> NotePath:
        """Write `note`'s `notes`/`chunks`/`links` rows on `conn`, no transaction of its own.

        Shared by the batch path (`_index_one`, inside its own `conn.transaction()`)
        and the connection path (`index_on_connection`, inside the caller's write
        transaction) - this never acquires a connection or opens a transaction
        itself, so it is safe to call from either.
        """
        note_path = parse_note_path(rel, allow_archive=True)

        # If another note currently occupies `rel` (e.g. it was deleted and a
        # different note created at the same path), free the path before
        # upserting on `id` - `path` is unique.
        await conn.execute("delete from notes where path = $1 and id <> $2", rel, note.id)

        await conn.execute(
            """
            insert into notes (
                id, path, namespace, type, slug, title, description, tags, aliases,
                created, updated, valid_from, valid_to, supersedes, source, archived,
                file_hash, indexed_at
            ) values (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17,
                now()
            )
            on conflict (id) do update set
                path = excluded.path,
                namespace = excluded.namespace,
                type = excluded.type,
                slug = excluded.slug,
                title = excluded.title,
                description = excluded.description,
                tags = excluded.tags,
                aliases = excluded.aliases,
                created = excluded.created,
                updated = excluded.updated,
                valid_from = excluded.valid_from,
                valid_to = excluded.valid_to,
                supersedes = excluded.supersedes,
                source = excluded.source,
                archived = excluded.archived,
                file_hash = excluded.file_hash,
                indexed_at = excluded.indexed_at
            """,
            note.id,
            rel,
            note_path.namespace,
            note_path.type,
            note_path.slug,
            note.title,
            note.description,
            list(note.tags),
            list(note.aliases),
            note.created,
            note.updated,
            note.valid_from,
            note.valid_to,
            list(note.supersedes),
            note.source,
            note_path.archived,
            file_hash,
        )

        await conn.execute("delete from chunks where note_id = $1", note.id)
        chunks = chunk_note(
            note.title,
            note.body,
            description=note.description,
            aliases=note.aliases,
            tags=note.tags,
        )
        if chunks:
            if await self._uses_vector_layout(conn):
                # ADR-0016's `chunks` (`migrations/postgres/0012_vector_layout.sql`):
                # `namespace`/`namespace_kind` are denormalised onto every row, the
                # latter via `mm_namespace_kind` rather than a direct `select` on
                # `namespaces` - `conn` may be running as the app role here
                # (`index_on_connection`'s own connection-path writes), which holds
                # no grant on that table (0005_rls.sql). `'org'` is the fallback for
                # an alias with no registry row yet - never a failed write over it.
                await conn.executemany(
                    "insert into chunks "
                    "(note_id, ord, heading_path, text, lang, namespace, namespace_kind) "
                    "values ($1, $2, $3, $4, $5, $6, coalesce(mm_namespace_kind($6), 'org'))",
                    [
                        (note.id, c.ord, c.heading_path, c.text, c.lang, note_path.namespace)
                        for c in chunks
                    ],
                )
            else:
                await conn.executemany(
                    "insert into chunks (note_id, ord, heading_path, text, lang) "
                    "values ($1, $2, $3, $4, $5)",
                    [(note.id, c.ord, c.heading_path, c.text, c.lang) for c in chunks],
                )

        await self._refresh_links(conn, note.id, note_path.namespace, note.body)
        return note_path

    async def _refresh_links(self, conn: _Conn, note_id: str, namespace: str, body: str) -> None:
        await conn.execute("delete from links where source_id = $1", note_id)

        refs = extract_links(body)
        if not refs:
            return

        targets = sorted({_target_slug(ref.target) for ref in refs})
        entries = await _entries_for(conn, targets)
        resolved = resolve_links(
            refs,
            source_namespace=namespace,
            entries=entries,
            readable_namespaces=_namespaces(entries),
        )
        rows = _dedup_links(resolved)
        if rows:
            await conn.executemany(
                "insert into links (source_id, target_raw, target_path) values ($1, $2, $3)",
                [(note_id, target_raw, target_path) for target_raw, target_path in rows],
            )

    async def _embed_note(self, note_id: str) -> None:
        """Embed `note_id`'s chunks right after they were (re-)written.

        A missing provider or an `EmbeddingError` both leave the chunks'
        `embedding` column `NULL` - either is picked up later by
        `embed_pending`, so an embedding outage never blocks indexing
        (`CLAUDE.md`/PLAN: embedding APIs are optional and pluggable).
        """
        if self._provider is None:
            return

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "select id, text from chunks where note_id = $1 order by ord", note_id
            )
        if not rows:
            return

        try:
            vectors = await self._provider.embed([row["text"] for row in rows])
        except EmbeddingError as exc:
            _logger.warning("skipping embeddings for note %r: %s", note_id, exc)
            return

        await self._apply_embeddings(rows, vectors, self._provider.model)

    async def embed_pending(self, limit: int | None = None) -> int:
        """Re-embed every chunk with no embedding, or one from a stale model.

        A no-op without a configured provider. Processes chunks in batches
        of `_EMBED_PENDING_BATCH_SIZE` (bounded further by `limit`, if
        given) and stops at the first `EmbeddingError`, leaving the
        remaining stale chunks for the next call. Returns the number of
        chunks re-embedded.
        """
        if self._provider is None:
            return 0

        model = self._provider.model
        total = 0
        while limit is None or total < limit:
            batch_size = _EMBED_PENDING_BATCH_SIZE
            if limit is not None:
                batch_size = min(batch_size, limit - total)

            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "select id, text from chunks "
                    "where embedding is null or model is distinct from $1 "
                    "order by id limit $2",
                    model,
                    batch_size,
                )
            if not rows:
                break

            try:
                vectors = await self._provider.embed([row["text"] for row in rows])
            except EmbeddingError as exc:
                _logger.warning("embed_pending: stopping after a failed batch: %s", exc)
                break

            await self._apply_embeddings(rows, vectors, model)
            total += len(rows)

        return total

    async def embed_note_job(self, note_id: str, expected_version: str) -> None:
        """Embed `note_id`'s still-stale chunks for one worker-claimed `"embed_note"`
        job (#219, `worker.py`'s `build_job_handlers`).

        A no-op without a configured provider - nothing to embed with. Also
        a no-op if `note_id`'s current `notes.file_hash` no longer matches
        `expected_version`: a newer write already replaced this note's
        chunks (and `index_on_connection` already enqueued that write's own
        job for them), so this stale job has nothing left to do, not even a
        model-change catch-up - `enqueue_stale_embeddings` owns that case.
        Unlike `_embed_note`'s own background path, an `EmbeddingError` from
        `self._provider.embed` is not caught here: it propagates to the
        caller (`worker._dispatch_job`), which retries the job with backoff
        (`jobs.fail_or_retry`) rather than leaving it silently `NULL` forever.
        """
        if self._provider is None:
            return

        async with self._pool.acquire() as conn:
            current_hash = await conn.fetchval("select file_hash from notes where id = $1", note_id)
            if current_hash != expected_version:
                return

            rows = await conn.fetch(
                "select id, text from chunks where note_id = $1 "
                "and (embedding is null or model is distinct from $2) "
                "order by ord",
                note_id,
                self._provider.model,
            )
        if not rows:
            return

        vectors = await self._provider.embed([row["text"] for row in rows])
        await self._apply_embeddings(rows, vectors, self._provider.model)

    async def enqueue_stale_embeddings(self) -> int:
        """Enqueue one `"embed_note"` job per note with a chunk still missing an
        embedding or stamped with a model other than the provider's current one.

        The worker process's own startup catch-up (#219): a lost job, a
        provider outage that left a chunk `NULL`, or a model change are all
        caught up here, the same cases `embed_pending` used to catch at the
        end of a `"postgres"`-mode `reindex` (`reindex`'s own docstring). A
        no-op without a configured provider. Returns how many notes were
        enqueued.
        """
        if self._provider is None:
            return 0

        async with self._pool.acquire() as conn, conn.transaction():
            rows = await conn.fetch(
                "select distinct n.id as note_id, n.file_hash as file_hash "
                "from notes n join chunks c on c.note_id = n.id "
                "where c.embedding is null or c.model is distinct from $1",
                self._provider.model,
            )
            for row in rows:
                await enqueue_job(
                    conn,
                    "embed_note",
                    {"note_id": row["note_id"], "version": row["file_hash"]},
                    traceparent=current_traceparent(),
                )
        return len(rows)

    async def _apply_embeddings(
        self, rows: Sequence[asyncpg.Record], vectors: Sequence[Sequence[float]], model: str
    ) -> None:
        if not vectors:
            return
        dimension = len(vectors[0])

        if await self._uses_vector_layout():
            await self._apply_embeddings_vector_layout(rows, vectors, model, dimension)
            return

        async with self._pool.acquire() as conn:
            await conn.executemany(
                "update chunks set embedding = $1::vector, model = $2, dimension = $3 "
                "where id = $4",
                [
                    (_vector_literal(vector), model, dimension, row["id"])
                    for row, vector in zip(rows, vectors, strict=True)
                ],
            )

        await self.ensure_vector_index(model, dimension)

    async def _apply_embeddings_vector_layout(
        self,
        rows: Sequence[asyncpg.Record],
        vectors: Sequence[Sequence[float]],
        model: str,
        dimension: int,
    ) -> None:
        """`_apply_embeddings`'s ADR-0016 counterpart (`migrations/postgres/
        0012_vector_layout.sql`): `chunks.embedding` is a fixed `halfvec(1024)`,
        with every `(namespace_kind)` partition's own index already created by
        that migration - there is no runtime `ensure_vector_index` equivalent to
        run here, unlike the per-`(model, dimension)` scheme the branch above
        still uses for the Git backend.

        Refuses, with `EmbeddingDimensionPinError`, a provider response whose
        dimension disagrees with `embedding_dimension`'s pinned value (PLAN O23) -
        before the mismatch ever reaches the strictly-typed `halfvec(1024)` column,
        where it would otherwise surface as an opaque `asyncpg` data error instead
        of a clear one naming the reindex path.
        """
        async with self._pool.acquire() as conn:
            pinned = await conn.fetchval("select dimension from embedding_dimension")
            if pinned is not None and dimension != pinned:
                raise EmbeddingDimensionPinError(
                    f"provider model {model!r} returned {dimension}-dim vectors, but this "
                    f"Postgres backend is pinned to {pinned} (EMBEDDING_DIMENSIONS, set at "
                    "first migrate, PLAN O23) - reindex into a new column/table to change it "
                    "(ADR-0016; not done by this process)"
                )

            await conn.executemany(
                "update chunks set embedding = $1::halfvec, model = $2, dimension = $3 "
                "where id = $4",
                [
                    (_vector_literal(vector), model, dimension, row["id"])
                    for row, vector in zip(rows, vectors, strict=True)
                ],
            )

    async def _uses_vector_layout(self, conn: _Conn | None = None) -> bool:
        """Whether the database has the ADR-0016 `chunks` layout.

        Detected from `schema_migrations` rather than from which `_Source`
        this `Indexer` was built with: a `VaultNotesSource()` instance
        pointed at a `backend="git"`-migrated database (every existing test
        that never asked for `backend="postgres"`) must keep writing the
        flat, `vector`-typed `chunks` migration `0001_index_schema.sql`
        gives it - ADR-0016 Consequences' "Git backend unchanged" holds
        regardless of which `_Source` happens to be in play, only of which
        migrations actually ran.

        `conn`, when given (`_upsert_note_rows`'s own call, both from the
        batch path's and `index_on_connection`'s already-open transaction),
        is queried directly rather than through a second `self._pool.acquire()`
        - acquiring a second connection while the caller's transaction still
        holds the only one a `min_size=1, max_size=1` pool has (several
        tests use exactly that shape for `index_on_connection`) would
        deadlock the pool against itself. Querying `conn` instead needs
        `schema_migrations` readable under whatever role that connection is
        running as - `db.rls.grant_app_role` grants the app role plain
        `select` on it for exactly this (#220). `self._pool.acquire()` is
        used only when no `conn` is given (`_apply_embeddings`'s own call,
        never inside an open caller transaction).
        """
        if self._vector_layout is None:
            query = "select exists(select 1 from schema_migrations where version = $1)"
            if conn is not None:
                self._vector_layout = bool(
                    await conn.fetchval(query, POSTGRES_VECTOR_LAYOUT_VERSION)
                )
            else:
                async with self._pool.acquire() as pool_conn:
                    self._vector_layout = bool(
                        await pool_conn.fetchval(query, POSTGRES_VECTOR_LAYOUT_VERSION)
                    )
        return self._vector_layout

    async def ensure_vector_index(self, model: str, dimension: int) -> None:
        """Create the HNSW index for `(model, dimension)` if it doesn't exist yet.

        One partial index per `(model, dimension)` pair, named after a hash
        of `model` so switching models never collides with an index left
        behind by the previous one. `model` is config-controlled, not user
        input, but is still quoted through `_sql_string_literal` rather
        than interpolated raw.
        """
        index_name = _hnsw_index_name(model, dimension)
        model_literal = _sql_string_literal(model)
        async with self._pool.acquire() as conn:
            await conn.execute(
                f"create index if not exists {index_name} on chunks "
                f"using hnsw ((embedding::vector({dimension})) vector_cosine_ops) "
                f"where model = {model_literal} and dimension = {dimension}"
            )

    async def _heal_dangling_links(self) -> None:
        """Resolve links that were dangling but now have a matching note.

        An `UPDATE` pass over `target_path is null` rows, not a full
        recompute: a link that *was* resolved and no longer is (its target
        got deleted) is only fixed by `_recompute_all_links` (`reindex_full`).
        Used by the batch paths (`index_paths`, so every `apply_changeset`/
        `reindex`), where more than one note in the same batch can be the
        reason a given link is no longer dangling - `index_on_connection`'s
        single-note equivalent is `_heal_dangling_for_note` below.
        """
        async with self._pool.acquire() as conn:
            dangling = await conn.fetch(
                """
                select links.source_id, links.target_raw, notes.namespace as source_namespace
                from links
                join notes on notes.id = links.source_id
                where links.target_path is null
                """
            )
            if not dangling:
                return

            targets = sorted({_target_slug(row["target_raw"]) for row in dangling})
            entries = await _entries_for(conn, targets)
            namespaces = _namespaces(entries)
            for row in dangling:
                ref = LinkRef(target=row["target_raw"], label=None, line=0)
                resolved = resolve_links(
                    [ref],
                    source_namespace=row["source_namespace"],
                    entries=entries,
                    readable_namespaces=namespaces,
                )[0]
                if resolved.target_path is not None:
                    await conn.execute(
                        "update links set target_path = $1 "
                        "where source_id = $2 and target_raw = $3",
                        resolved.target_path,
                        row["source_id"],
                        row["target_raw"],
                    )

    async def _heal_dangling_for_note(
        self, conn: _Conn, note_id: str, namespace: str, slug: str, aliases: Sequence[str]
    ) -> None:
        """Resolve dangling links that could point at the note just written on `conn`.

        `index_on_connection`'s counterpart to `_heal_dangling_links` above, scoped
        to the handful of raw forms (`slug`, each alias, and their
        `namespace/`-qualified forms, case-folded) that could ever resolve to
        *this* note - never a scan of every dangling link in the index, let alone
        every note (#98's connection-path performance requirement: one write must
        never read unrelated notes or links).
        """
        forms = {slug.lower(), *(alias.lower() for alias in aliases)}
        forms |= {f"{namespace.lower()}/{form}" for form in list(forms)}

        dangling = await conn.fetch(
            """
            select links.source_id, links.target_raw, notes.namespace as source_namespace
            from links
            join notes on notes.id = links.source_id
            where links.target_path is null
              and lower(trim(links.target_raw)) = any($1::text[])
            """,
            sorted(forms),
        )
        if not dangling:
            return

        targets = sorted({_target_slug(row["target_raw"]) for row in dangling})
        entries = await _entries_for(conn, targets)
        namespaces = _namespaces(entries)
        for row in dangling:
            if row["source_id"] == note_id:
                continue  # a note's own links are already current, just written
            ref = LinkRef(target=row["target_raw"], label=None, line=0)
            resolved = resolve_links(
                [ref],
                source_namespace=row["source_namespace"],
                entries=entries,
                readable_namespaces=namespaces,
            )[0]
            if resolved.target_path is not None:
                await conn.execute(
                    "update links set target_path = $1 where source_id = $2 and target_raw = $3",
                    resolved.target_path,
                    row["source_id"],
                    row["target_raw"],
                )

    async def _recompute_all_links(self) -> None:
        """Recompute links for every note currently in the index.

        Used by `reindex_full` once every note is in, so a link to a note
        indexed later in the same run resolves, and a link whose target
        disappeared becomes dangling again.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("select id, path, namespace from notes")

        for row in rows:
            try:
                data = await self._source.read(row["path"])
            except PathRejected as exc:
                _logger.warning("skipping link recompute for %r: %s", row["path"], exc)
                continue
            if data is None:
                _logger.warning("skipping link recompute for %r: no longer found", row["path"])
                continue
            try:
                body = parse(data).body
            except NoteFormatError as exc:
                _logger.warning("skipping link recompute for %r: %s", row["path"], exc)
                continue

            async with self._pool.acquire() as conn_tx, conn_tx.transaction():
                await self._refresh_links(conn_tx, row["id"], row["namespace"], body)


def _is_note_path(rel: str) -> bool:
    try:
        parse_note_path(rel, allow_archive=True)
    except PathRejected:
        return False
    return True


def _target_slug(target: str) -> str:
    """The slug part of a link target, namespace prefix stripped, case-folded.

    Mirrors `vault.links._normalize_target`'s own split (trim, lower-case, take
    whatever follows the first `/`, or the whole thing if there is none) - just
    the part that can ever match a `notes.slug`/`notes.aliases` entry, so
    `_entries_for` can look candidates up by it directly instead of re-deriving
    `resolve_links`' own namespace handling here too.
    """
    normalized = target.strip().lower()
    _, sep, slug = normalized.partition("/")
    return slug if sep else normalized


async def _entries_for(conn: _Conn, targets: Sequence[str]) -> list[VaultEntry]:
    """`VaultEntry` for every note whose slug or alias matches one of `targets`.

    `targets` are already normalized (`_target_slug`): trimmed, lower-cased,
    namespace prefix stripped. These are the only notes `resolve_links` could
    ever match a reference to, so neither `_refresh_links` (one note's own
    outgoing links) nor `_heal_dangling_links`/`_heal_dangling_for_note` (the
    index's currently-dangling links) ever has to read the whole `notes` table
    to resolve a link (#98). Both branches are index-backed (`notes_slug_lower_idx`,
    `notes_aliases_lower_gin_idx`, migration 0004, see #98): `lower_array` folds
    `aliases` to lower case the same way the dropped `unnest`/`exists` form did,
    just as a `&&` the GIN index can be probed with instead of a per-row subplan.
    """
    if not targets:
        return []
    rows = await conn.fetch(
        """
        select path, namespace, slug, aliases
        from notes
        where lower(slug) = any($1::text[])
           or lower_array(aliases) && $1::text[]
        """,
        list(targets),
    )
    return [
        VaultEntry(
            path=row["path"],
            namespace=row["namespace"],
            slug=row["slug"],
            aliases=tuple(row["aliases"]),
        )
        for row in rows
    ]


def _namespaces(entries: Sequence[VaultEntry]) -> list[str]:
    return sorted({entry.namespace for entry in entries})


def _dedup_links(resolved: Sequence[ResolvedLink]) -> list[tuple[str, str | None]]:
    """Collapse links whose target only differs by case to a single row.

    `resolve_links` folds case before matching, so `[[Foo]]` and `[[foo]]`
    in one note are the same reference even though `extract_links` keeps
    them as two distinct, case-sensitive refs (#10, #24 comment). The first
    occurrence's raw text is kept as `target_raw`.
    """
    seen: set[str] = set()
    rows: list[tuple[str, str | None]] = []
    for link in resolved:
        key = link.ref.target.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        rows.append((link.ref.target, link.target_path))
    return rows


def _parse_affected(result: str) -> int:
    """Parse the row count out of an asyncpg command tag like `'DELETE 3'`."""
    return int(result.rsplit(" ", 1)[-1])


def _vector_literal(vector: Sequence[float]) -> str:
    """Render `vector` as the `'[1.0,2.0,...]'` text pgvector parses via `::vector`."""
    return "[" + ",".join(str(float(value)) for value in vector) + "]"


def _hnsw_index_name(model: str, dimension: int) -> str:
    # Not a cryptographic use - just a short, stable, identifier-safe tag
    # that is deterministic per model name.
    digest = hashlib.sha1(model.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]
    return f"chunks_hnsw_{digest}_{dimension}"


def _sql_string_literal(value: str) -> str:
    """Quote `value` as a SQL string literal; DDL has no parameter placeholders."""
    return "'" + value.replace("'", "''") + "'"
