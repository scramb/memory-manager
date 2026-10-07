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
upserted, via the optional `provider` constructor argument. A missing
provider or a failed embedding call both leave `chunks.embedding` `NULL` -
indexing itself never blocks on it. `embed_pending`, called at the end of
`reindex`, is the catch-up pass: it re-embeds every chunk still missing an
embedding or stamped with a model other than the provider's current one,
so a provider outage or a model change both self-heal on the next reindex.

`index_on_connection`/`schedule_embeddings` are the connection-path entry
points `storage.postgres.PostgresBackend` wires in as its `IndexHook`/
`IndexCommitHook` (ADR-0007 §4, #98): a write indexes the note it just
wrote on the *same* connection, inside the *same* transaction as the write
itself - no separate pool acquisition, no second transaction - and schedules
its embeddings as a background task only after that transaction has
committed, never awaited by the write. `aclose` drains those background
tasks with a time limit on process shutdown, so none of them outlives the
pool they were handed.
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

from memory_manager.index.chunker import chunk_note
from memory_manager.index.embeddings import EmbeddingError, EmbeddingProvider
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
        note indexed later in the same run still resolves.
        """
        discovered = await self._source.discover_paths()
        stats = await self.index_paths(discovered)
        if not full:
            await self.embed_pending()
            return stats

        extra_deleted = await self._delete_stale(discovered)
        await self._recompute_all_links()
        await self.embed_pending()
        return IndexStats(
            indexed=stats.indexed,
            unchanged=stats.unchanged,
            deleted=stats.deleted + extra_deleted,
            failed=stats.failed,
        )

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
        """
        note = parse(content)
        file_hash = version(content)
        note_path = await self._upsert_note_rows(conn, path, note, file_hash)
        await self._heal_dangling_for_note(
            conn, note.id, note_path.namespace, note_path.slug, note.aliases
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

    async def _apply_embeddings(
        self, rows: Sequence[asyncpg.Record], vectors: Sequence[Sequence[float]], model: str
    ) -> None:
        if not vectors:
            return
        dimension = len(vectors[0])

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
    to resolve a link (#98) - `notes` has no index on `slug`/`aliases` today, so
    this still costs a sequential scan per call, just over a result set bounded
    by `len(targets)` rather than every row read back into Python.
    """
    if not targets:
        return []
    rows = await conn.fetch(
        """
        select path, namespace, slug, aliases
        from notes
        where lower(slug) = any($1::text[])
           or exists (
                select 1 from unnest(aliases) as alias where lower(alias) = any($1::text[])
           )
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
