# SPDX-License-Identifier: AGPL-3.0-only
"""The Postgres implementation of `StorageBackend` (ADR-0007 §2, enterprise mode).

Unlike `storage.git.GitBackend`, which delegates to the serialized
`WriteQueue`, `PostgresBackend` talks straight to `vault_notes`/
`vault_revisions` (migration `0004_vault.sql`): Postgres writes are already
serialized per row by the database, through the same `UPDATE ... WHERE
current_revision = $n` idiom `queue.py`'s Git path uses at the file level.
`write`/`edit` build a `WriteRequest` exactly like `GitBackend` does and
hand it to one private flow, `_write_or_edit`, that both call.

Indexing (ADR-0007 §4, WP-18/#98): `__init__`'s optional `index_hook` is
awaited on the write's own connection, inside its own transaction, right
after the revision row is inserted - `index.indexer.Indexer.index_on_connection`
is the one implementation, wired in by `app.py`. An exception from it rolls
back the whole write exactly like any other failure in that transaction
(the existing `asyncpg.PostgresError` -> `WriteFailed` mapping applies
unchanged); `index_commit_hook` runs only once that transaction has already
committed, with the ids of every note the write just touched, and is never
awaited by anything that could make a write wait on it (`Indexer.schedule_embeddings`
only ever starts a background task). Without either hook (every other
caller, most tests), indexing is simply skipped - the two parameters default
to `None` and `PostgresBackend(pool)` keeps working unchanged.

Row-level security (ADR-0008 + both addenda, #116): `__init__`'s optional
`app_role` turns on the request path's role switch. Every content-table
access below - `read`/`list`/`namespace_usage` and the inner flow of
`write`/`edit`/`archive`/`supersede` - runs exclusively through
`_content_connection`, this class's
one seam onto a connection: without `app_role` (every existing
`PostgresBackend(pool)` call, including every test in this package) it is a
plain pool connection in its own transaction, exactly as before `app_role`
existed; with it, it is `db.rls.request_connection`, which switches the
connection to `app_role` and the calling request's principal before yielding
it - and raises `db.rls.NoPrincipal` *before* acquiring one at all if the
request carries none, so a missing principal can never silently fall
through to running as the owner. The index hook above runs on that very
same connection, so `notes`/`chunks`/`links` are written under the app role
too, automatically. `changes_since` is the one method that stays on the
owner pool unconditionally, `app_role` or not - it is a system path
(`app.py`'s `_reindex`/CLI scope, never a per-request caller), not part of
the request path this module's docstring is otherwise about.

Concurrency: a `write`/`edit` runs in one `READ COMMITTED` transaction (the
pool's default - no `REPEATABLE READ`/`SERIALIZABLE`). An `UPDATE ... WHERE
id = $id AND current_revision = $n` is safe under `READ COMMITTED` without
an explicit row lock: Postgres blocks a second, concurrent `UPDATE` on the
same row behind the first's lock, then re-evaluates its `WHERE` clause
against the now-committed row once unblocked - so the loser always sees
the winner's new `current_revision` and legitimately updates zero rows,
never a stale one (this is what makes the plain conditional `UPDATE` safe
here, not a construct that happens to work by accident). The same holds for
two concurrent `INSERT ... ON CONFLICT (path) DO NOTHING` on one `path`.

`id` is never part of an `UPDATE`'s `SET`: a conditional `UPDATE` only ever
touches the row it matched by `id`, so the note's identity cannot drift
from one write to the next. On `write(if_version="new")`, `ON CONFLICT
(path) DO NOTHING` (not `(id)`) means a client-chosen `id` that already
exists under a *different* path surfaces as a plain Postgres
`UniqueViolationError` on the primary key, mapped here to `InvalidNote` -
`path` is the only conflict target this flow resolves by re-reading and
raising `VersionConflict`.

`archive` reuses the same conditional-`UPDATE` idiom, just with `path`
itself in the `SET` list: the row's identity (`id`) is unchanged, only
where it lives moves, under the same `current_revision` guard. A
concurrent `edit` racing an `archive` on the same note is reported as
`VersionConflict`, never `WriteFailed`: whichever of the two loses its
conditional `UPDATE` re-reads by the *original* `path` - if `archive` won,
that re-read now finds nothing there at all (`(None, None)`, the same
shape `VersionConflict` already uses for "does not exist"), not an
unreachable error. `supersede` runs the old note's `UPDATE` through the
same path (its `path` never changes, only `content`/`valid_to`) and the
new note's insert through the same `ON CONFLICT (path) DO NOTHING` `write`
uses for `if_version="new"` - but reports a lost race there as
`InvalidNote`, not `VersionConflict`: `new_path` carries no `if_version`
to compare against, only the existence check `rules.prepare_supersede_content`
already made moments earlier, so losing the race afterwards is "the path
turned out to be taken after all", not a version mismatch.

`changes_since` is the one method that never takes a row lock or competes
with a write: it opens its own read-only `REPEATABLE READ` transaction
and uses `pg_snapshot_xmin(pg_current_snapshot())` as the cursor, not
`now()`, `max(xid)` or `current_revision`. `xid8` order, not `created_at`
or `revision`, is what makes this correct under concurrent writers: a
transaction that is still in flight when one call takes its snapshot
keeps the snapshot's `xmin` from moving past it even if that transaction
commits *after* this call already returned - the next call's window
starts exactly there, so a lower `xid` committing late is picked up
instead of skipped. A long-running transaction anywhere in the cluster -
not only one that itself writes to this vault - holds `xmin` back the
same way, so it delays how soon EVERY later-committed change is
reported, never only its own, and never drops any of them.
Within one call's window `[cursor, new_xmin)`, a revision whose `path`
differs from its own note's immediately preceding revision (an `archive`)
contributes *both* paths to the touched set - the vacated path otherwise
never appears in this window, since nothing was written there - and every
touched path is then classified by one more lookup, in the same snapshot:
present in `vault_notes` means `changed` (covers a path archived and then
reoccupied within this very window), absent means `deleted`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

import asyncpg

from memory_manager.db import rls
from memory_manager.storage import rules
from memory_manager.storage.base import (
    AuditHook,
    IndexCommitHook,
    IndexHook,
    InvalidNote,
    NotFound,
    StorageChanges,
    StoredNote,
    VersionConflict,
    WriteFailed,
    WriteRequest,
    WriteResult,
)
from memory_manager.vault.note import parse, version
from memory_manager.vault.paths import PathRejected, parse_note_path

__all__ = ["NamespaceUsage", "PostgresBackend"]

_logger = logging.getLogger(__name__)

# A connection acquired from `self._pool` directly (no `app_role` configured) or
# one `db.rls.request_connection` has already switched role/identity on - both
# expose the same `fetch`/`fetchrow`/`execute`/`transaction` surface this module
# uses, so every inner flow below is written against either shape
# interchangeably (same idea as `db/rls.py`'s own `_Connectable`).
_Connectable = asyncpg.pool.PoolConnectionProxy | asyncpg.Connection

_SELECT_CURRENT = """
select id, content, version, current_revision
from vault_notes
where path = $1
"""

_SELECT_LIST = """
select path, content, version
from vault_notes
order by path
"""

_INSERT_NEW = """
insert into vault_notes (id, namespace, path, content, version, current_revision)
values ($1, $2, $3, $4, $5, 1)
on conflict (path) do nothing
returning id, current_revision
"""

_UPDATE_EXISTING = """
update vault_notes
set content = $1, version = $2, current_revision = current_revision + 1, updated_at = now()
where id = $3 and current_revision = $4
returning id, current_revision
"""

_UPDATE_ARCHIVE = """
update vault_notes
set path = $1, content = $2, version = $3,
    current_revision = current_revision + 1, updated_at = now()
where id = $4 and current_revision = $5
returning id, current_revision
"""

_INSERT_REVISION = """
insert into vault_revisions (note_id, revision, path, content, version, author, client, message)
values ($1, $2, $3, $4, $5, $6, $7, $8)
"""

#: Not cast to `::text`: asyncpg already decodes `xid8` straight to a plain
#: Python `int` (unlike some other Postgres-specific types), which is also
#: what `_SELECT_CHANGED_REVISIONS` below needs to bind back in as `$1`/`$2`
#: - the cursor string callers see is `str()` of this value, round-tripped
#: back to `int()` on the next call.
_SELECT_SNAPSHOT_XMIN = "select pg_snapshot_xmin(pg_current_snapshot()) as xmin"

_SELECT_CHANGED_REVISIONS = """
select r.path as path, prev.path as prev_path
from vault_revisions r
left join vault_revisions prev
    on prev.note_id = r.note_id and prev.revision = r.revision - 1
where r.xid >= $1::xid8 and r.xid < $2::xid8
order by r.xid, r.revision
"""

_SELECT_EXISTING_PATHS = """
select path from vault_notes where path = any($1::text[])
"""

#: `namespace_usage`'s one query (#243): `note_count` excludes an archived
#: path (`left(path, 9) = '_archive/'`, `vault.paths.NotePath`'s own
#: `_ARCHIVE_SEGMENT` prefix - CLAUDE.md/#243's own issue text: "archived
#: notes count toward size, not toward count"), `total_bytes` does not -
#: it sums every row for `namespace`, archived or not. `path_bytes` is the
#: byte size already stored at the one `path` a write/edit/supersede is
#: about to touch (`0` if it does not exist yet, via the `coalesce` below),
#: read in the same round trip so a caller can compute "total after this
#: write" as `total_bytes - path_bytes + <the write's own predicted size>`
#: without a second query. Uses `vault_notes_namespace_idx` (`0013_vault_
#: notes_namespace_idx.sql` - 0010-0012 are taken by WP-22/WP-23, applied
#: in sorted-filename order regardless of the gap) for the `namespace` match.
_SELECT_NAMESPACE_USAGE = """
with ns as (
    select
        count(*) filter (where left(path, 9) <> '_archive/') as note_count,
        coalesce(sum(octet_length(content)), 0) as total_bytes
    from vault_notes
    where namespace = $1
)
select
    ns.note_count,
    ns.total_bytes,
    coalesce((select octet_length(content) from vault_notes where path = $2), 0) as path_bytes
from ns
"""


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class NamespaceUsage:
    """What `namespace` currently holds, for `quotas.StorageQuotaChecker.check_write` (#243)
    to predict a write's/edit's/supersede's resulting note count and byte size against,
    before it runs. See `PostgresBackend.namespace_usage`'s own docstring for exactly what
    each field counts."""

    note_count: int
    total_bytes: int
    path_bytes: int


class PostgresBackend:
    """`StorageBackend` backed by `vault_notes`/`vault_revisions` (ADR-0007 §2).

    Implements the full `StorageBackend` protocol: `read`/`write`/`edit`
    (#96) plus `list`/`supersede`/`archive`/`changes_since` (#97). `clock`
    is injectable for tests, the same pattern `WriteQueue.__init__` uses -
    it is only ever consulted for `archive`'s and `supersede`'s `updated`/
    `valid_to` stamps, truncated to whole seconds like the Git backend's.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        clock: Callable[[], datetime] = _utc_now,
        index_hook: IndexHook | None = None,
        index_commit_hook: IndexCommitHook | None = None,
        app_role: str | None = None,
    ) -> None:
        self._pool = pool
        self._clock = clock
        self._audit_hooks: list[AuditHook] = []
        # `index_hook`/`index_commit_hook` (ADR-0007 §4, WP-18/#98): optional so
        # every existing `PostgresBackend(pool)` call (most tests) keeps running
        # with no index wired in at all - `app.py` is the only caller that passes
        # either, built from an `index.indexer.Indexer` over `vault_notes`.
        self._index_hook = index_hook
        self._index_commit_hook = index_commit_hook
        # `app_role` (ADR-0008 addendum, #116): optional so every existing
        # `PostgresBackend(pool)` call keeps running exactly as before - only
        # `app.py`'s `open_services` passes one, for the request-serving process.
        self._app_role = app_role

    @asynccontextmanager
    async def _content_connection(self) -> AsyncIterator[_Connectable]:
        """One connection, inside one transaction, for a single content-table access.

        Every `read`/`list`/write/archive/supersede flow below runs
        exclusively through this - the structural guarantee #116 asks for:
        once `app_role` is configured, this backend never touches
        `vault_notes`/`vault_revisions`/`notes`/`chunks`/`links` on a
        connection that has not switched to `app_role` under the current
        request's principal (`db.rls.request_connection`, which itself
        raises `db.rls.NoPrincipal` before ever acquiring one if the request
        carries none). Without `app_role` (every existing
        `PostgresBackend(pool)` call, no request path, #96/#97's own tests),
        this is a plain pool connection in its own transaction, exactly as
        every flow below acquired one before this method existed.
        """
        if self._app_role is None:
            async with self._pool.acquire() as plain_conn, plain_conn.transaction():
                yield plain_conn
            return
        async with rls.request_connection(self._pool, role=self._app_role) as switched_conn:
            yield switched_conn

    def add_audit_hook(self, hook: AuditHook) -> None:
        """Register `hook`, awaited after every `write`/`edit`/`supersede`/`archive`.

        Same contract as `queue.WriteQueue.add_audit_hook` (`storage.base.AuditHook`'s
        docstring, ADR-0007 §2: both backends enforce "audit log for every write"
        identically) - called exactly once per op, success or rejection alike, after
        the transaction has already committed or failed but before the result is
        returned or the error raised to this method's own caller. A raising hook is
        logged, never propagated.
        """
        self._audit_hooks.append(hook)

    async def _run_audit_hooks(
        self, request: WriteRequest, result: WriteResult | None, error: Exception | None
    ) -> None:
        for hook in self._audit_hooks:
            try:
                await hook(request, result, error)
            except Exception:
                _logger.exception("postgres backend audit hook failed for %s", request.path)

    async def read(self, path: str) -> StoredNote | None:
        """The note at `path`, or `None` if it does not exist.

        Raises `PathRejected` for a `path` that is not a safe, valid note
        path, same as `GitBackend.read` - callers rely on this, not on it
        being converted to an `InvalidNote` here.
        """
        parse_note_path(path, allow_archive=True)
        async with self._content_connection() as conn:
            row = await conn.fetchrow(_SELECT_CURRENT, path)
        if row is None:
            return None
        return StoredNote(path=path, content=bytes(row["content"]), version=row["version"])

    async def list(self, *, include_archived: bool = False) -> list[StoredNote]:
        """Every note in `vault_notes`, in path order.

        Mirrors `GitBackend.list`'s `_archive/`-prefix filter
        (`storage/git.py`): a path that fails `parse_note_path` is skipped
        rather than raised on - nothing in this backend's write path can
        ever produce one, but a row is not reparsed against the write-time
        rules just to read it back. Archived notes are included only when
        `include_archived` is set.
        """
        async with self._content_connection() as conn:
            rows = await conn.fetch(_SELECT_LIST)
        entries: list[StoredNote] = []
        for row in rows:
            path = row["path"]
            try:
                note_path = parse_note_path(path, allow_archive=True)
            except PathRejected:
                continue
            if note_path.archived and not include_archived:
                continue
            entries.append(
                StoredNote(path=path, content=bytes(row["content"]), version=row["version"])
            )
        return entries

    async def namespace_usage(self, namespace: str, path: str) -> NamespaceUsage:
        """`namespace`'s current note count/byte size, plus `path`'s own current byte size.

        Read-only, through the same RLS-respecting `_content_connection` every
        other read in this backend uses - under `app_role` once one is
        configured, a plain pool connection otherwise, exactly like `read`/
        `list` above. `path` need not exist yet (`NamespaceUsage.path_bytes`
        is `0` then, the `_SELECT_NAMESPACE_USAGE` module constant's own
        `coalesce`) - a caller computes "this write's predicted total" from
        the three fields together, see that constant's own comment.
        """
        async with self._content_connection() as conn:
            row = await conn.fetchrow(_SELECT_NAMESPACE_USAGE, namespace, path)
        if row is None:  # pragma: no cover - the `ns` CTE above always returns exactly one row
            raise AssertionError("_SELECT_NAMESPACE_USAGE returned no row")
        return NamespaceUsage(
            note_count=int(row["note_count"]),
            total_bytes=int(row["total_bytes"]),
            path_bytes=int(row["path_bytes"]),
        )

    async def write(
        self,
        path: str,
        content: bytes,
        *,
        if_version: str,
        client: str,
        actor: str = "stdio",
        message: str | None = None,
    ) -> WriteResult:
        return await self._write_or_edit(
            WriteRequest(
                op="write",
                path=path,
                client=client,
                if_version=if_version,
                content=content,
                message=message,
                actor=actor,
            )
        )

    async def edit(
        self,
        path: str,
        old_str: str,
        new_str: str,
        *,
        if_version: str,
        client: str,
        actor: str = "stdio",
        message: str | None = None,
    ) -> WriteResult:
        return await self._write_or_edit(
            WriteRequest(
                op="edit",
                path=path,
                client=client,
                if_version=if_version,
                old_str=old_str,
                new_str=new_str,
                message=message,
                actor=actor,
            )
        )

    async def _write_or_edit(self, request: WriteRequest) -> WriteResult:
        """`write`/`edit`'s audited entry point: run `_write_or_edit_inner`, then audit.

        The audit hook fires exactly once, after the transaction has
        committed or failed, before the result is returned or the error
        raised - same ordering `queue.py`'s own audit hook uses for the
        `"git"` backend.
        """
        try:
            result = await self._write_or_edit_inner(request)
        except Exception as exc:
            await self._run_audit_hooks(request, None, exc)
            raise
        await self._run_audit_hooks(request, result, None)
        return result

    async def _write_or_edit_inner(self, request: WriteRequest) -> WriteResult:
        """`write`/`edit`'s shared flow: validate, then one `READ COMMITTED` transaction.

        `WriteResult.commit` is `"<note id>@<revision>"` - a stable, unique
        string per revision of a note (not a Git sha; there is no commit
        here), good enough for a caller that only needs "what changed,
        identifiably" out of it.
        """
        note_path = rules.parse_note_path_or_raise(request.path, allow_archive=True)

        try:
            async with self._content_connection() as conn:
                current_row = await conn.fetchrow(_SELECT_CURRENT, request.path)
                current = bytes(current_row["content"]) if current_row is not None else None
                current_version = current_row["version"] if current_row is not None else None

                rules.check_version(request, current_version, current)

                final_bytes = rules.prepare_write_or_edit(
                    request.op,
                    request.path,
                    request.content,
                    request.old_str,
                    request.new_str,
                    current,
                )

                if current is not None and final_bytes == current:
                    raise WriteFailed("no changes")

                new_version = version(final_bytes)
                message = request.message or f"{request.op} {request.path}"

                if current_row is None:
                    note_id, revision = await self._insert_new(
                        conn, note_path.namespace, request.path, final_bytes, new_version
                    )
                else:
                    note_id, revision = await self._update_existing(
                        conn,
                        request.path,
                        final_bytes,
                        new_version,
                        current_row["id"],
                        current_row["current_revision"],
                    )

                await conn.execute(
                    _INSERT_REVISION,
                    note_id,
                    revision,
                    request.path,
                    final_bytes,
                    new_version,
                    request.actor,
                    request.client,
                    message,
                )

                if self._index_hook is not None:
                    await self._index_hook(conn, request.path, final_bytes)
        except asyncpg.UniqueViolationError as exc:
            raise InvalidNote(request.path, str(exc)) from exc
        except asyncpg.PostgresError as exc:
            raise WriteFailed(str(exc)) from exc

        if self._index_commit_hook is not None:
            await self._index_commit_hook((note_id,))

        return WriteResult(path=request.path, version=new_version, commit=f"{note_id}@{revision}")

    async def _insert_new(
        self,
        conn: _Connectable,
        namespace: str,
        path: str,
        final_bytes: bytes,
        new_version: str,
    ) -> tuple[str, int]:
        """Create the note's current row and return `(id, revision=1)`.

        A concurrent `write(if_version="new")` on the same `path` loses the
        `INSERT ... ON CONFLICT (path) DO NOTHING` race, sees no returned
        row, and raises `VersionConflict` against what the winner just
        committed - re-read inside this same transaction, so it is never
        stale by the time it is reported.
        """
        note_id = parse(final_bytes).id
        inserted = await conn.fetchrow(
            _INSERT_NEW, note_id, namespace, path, final_bytes, new_version
        )
        if inserted is None:
            raise VersionConflict(path, *await self._reread_for_conflict(conn, path))
        return str(inserted["id"]), int(inserted["current_revision"])

    async def _update_existing(
        self,
        conn: _Connectable,
        path: str,
        final_bytes: bytes,
        new_version: str,
        note_id: str,
        current_revision: int,
    ) -> tuple[str, int]:
        """Advance the note's current row and return `(id, revision=n+1)`.

        A concurrent writer that got there first loses the `WHERE id = $id
        AND current_revision = $n` race, sees no returned row, and raises
        `VersionConflict` against what the winner just committed - re-read
        inside this same transaction, built directly from that row rather
        than through `rules.check_version` (which would compare against
        this call's now-stale `if_version` again, not against what
        actually won).
        """
        updated = await conn.fetchrow(
            _UPDATE_EXISTING, final_bytes, new_version, note_id, current_revision
        )
        if updated is None:
            raise VersionConflict(path, *await self._reread_for_conflict(conn, path))
        return str(updated["id"]), int(updated["current_revision"])

    async def _reread_for_conflict(
        self, conn: _Connectable, path: str
    ) -> tuple[str | None, str | None]:
        """`(version, content)` of the row a losing writer's conflict is reported against.

        Re-read inside the same transaction right after losing an `INSERT
        ... ON CONFLICT`/conditional `UPDATE` race. Usually reflects the
        winner, which just committed in this same transaction's view - but
        a racing `archive` can have moved the row to a different path in
        that same instant, so `path` now has no row there at all: reported
        as `(None, None)`, the same shape `VersionConflict` already uses
        for "does not exist", never `WriteFailed`.
        """
        winner = await conn.fetchrow(_SELECT_CURRENT, path)
        if winner is None:
            return None, None
        content = bytes(winner["content"]).decode("utf-8", errors="replace")
        return winner["version"], content

    async def archive(
        self,
        path: str,
        *,
        if_version: str,
        client: str,
        actor: str = "stdio",
        message: str | None = None,
    ) -> WriteResult:
        """Move the note at `path` to its `_archive/` counterpart, in one transaction.

        `if_version` is checked first (`rules.check_version`), before
        `path`'s existence is: the same order `queue.py`'s `_process`
        uses, so `if_version="new"` against a missing `path` passes that
        check (there is nothing to conflict with) and only then surfaces
        as `NotFound` from `rules.prepare_archive` - never masked by a
        spurious `VersionConflict`. The move itself reuses the conditional
        `UPDATE` `write`/`edit` use, just with `path` in the `SET` list
        too: a concurrent `edit` racing this on the same note loses (or
        wins) the same `WHERE id = $id AND current_revision = $n` guard
        and is reported through `_reread_for_conflict` exactly like
        `write`/`edit` would. Audited exactly once, success or rejection
        alike, same as `_write_or_edit` above.
        """
        request = WriteRequest(
            op="archive",
            path=path,
            client=client,
            if_version=if_version,
            actor=actor,
            message=message,
        )
        try:
            result = await self._archive_inner(request)
        except Exception as exc:
            await self._run_audit_hooks(request, None, exc)
            raise
        await self._run_audit_hooks(request, result, None)
        return result

    async def _archive_inner(self, request: WriteRequest) -> WriteResult:
        path = request.path
        try:
            async with self._content_connection() as conn:
                current_row = await conn.fetchrow(_SELECT_CURRENT, path)
                current = bytes(current_row["content"]) if current_row is not None else None
                current_version = current_row["version"] if current_row is not None else None

                rules.check_version(request, current_version, current)

                if current is None or current_row is None:
                    raise NotFound(path)
                note_path = rules.parse_note_path_or_raise(path)
                archive_rel = note_path.archive_path().relative
                archive_row = await conn.fetchrow(_SELECT_CURRENT, archive_rel)

                now = self._clock().astimezone(UTC).replace(microsecond=0)
                archived_bytes = rules.prepare_archive(
                    path, current, archive_exists=archive_row is not None, now=now
                )
                new_version = version(archived_bytes)
                final_message = request.message or f"archive {path}"

                updated = await conn.fetchrow(
                    _UPDATE_ARCHIVE,
                    archive_rel,
                    archived_bytes,
                    new_version,
                    current_row["id"],
                    current_row["current_revision"],
                )
                if updated is None:
                    raise VersionConflict(path, *await self._reread_for_conflict(conn, path))
                note_id = str(updated["id"])
                revision = int(updated["current_revision"])

                await conn.execute(
                    _INSERT_REVISION,
                    note_id,
                    revision,
                    archive_rel,
                    archived_bytes,
                    new_version,
                    request.actor,
                    request.client,
                    final_message,
                )

                if self._index_hook is not None:
                    await self._index_hook(conn, archive_rel, archived_bytes)
        except asyncpg.UniqueViolationError as exc:
            raise InvalidNote(path, str(exc)) from exc
        except asyncpg.PostgresError as exc:
            raise WriteFailed(str(exc)) from exc

        if self._index_commit_hook is not None:
            await self._index_commit_hook((note_id,))

        return WriteResult(path=archive_rel, version=new_version, commit=f"{note_id}@{revision}")

    async def supersede(
        self,
        path: str,
        new_path: str,
        content: bytes,
        *,
        if_version: str,
        client: str,
        actor: str = "stdio",
        message: str | None = None,
    ) -> WriteResult:
        """Replace the note at `path` with a new note at `new_path`, in one transaction.

        The old note's row keeps its `path`, just like an `edit` would -
        only `content`/`version` change, through the same `_update_existing`
        conditional `UPDATE`, so a concurrent `edit` of the old note races
        and is reported exactly the same way. The new note's row is
        inserted through the same `ON CONFLICT (path) DO NOTHING` `write`
        uses for `if_version="new"`, but losing that race is reported as
        `InvalidNote`, not `VersionConflict`: `new_path` carries no
        `if_version` to compare against, only the existence check
        `rules.prepare_supersede_content` already made moments earlier -
        losing afterwards is "the path turned out to be taken after all",
        the same wording a non-racing caller gets from that check. Audited
        exactly once, success or rejection alike, same as `_write_or_edit`
        above.
        """
        request = WriteRequest(
            op="supersede",
            path=path,
            client=client,
            if_version=if_version,
            new_path=new_path,
            content=content,
            message=message,
            actor=actor,
        )
        try:
            result = await self._supersede_inner(request)
        except Exception as exc:
            await self._run_audit_hooks(request, None, exc)
            raise
        await self._run_audit_hooks(request, result, None)
        return result

    async def _supersede_inner(self, request: WriteRequest) -> WriteResult:
        path = request.path
        new_path = request.new_path
        if new_path is None or request.content is None:
            raise AssertionError(  # pragma: no cover - supersede() above always sets both
                "PostgresBackend._supersede_inner called without new_path/content"
            )
        content = request.content
        client = request.client
        actor = request.actor
        message = request.message
        try:
            async with self._content_connection() as conn:
                old_row = await conn.fetchrow(_SELECT_CURRENT, path)
                old_current = bytes(old_row["content"]) if old_row is not None else None
                old_version = old_row["version"] if old_row is not None else None

                rules.check_version(request, old_version, old_current)

                old_note_path, new_note_path, checked_new_path, new_content, old_bytes = (
                    rules.prepare_supersede_paths(path, new_path, content, old_current)
                )
                if old_row is None:  # pragma: no cover - prepare_supersede_paths already raised
                    raise NotFound(path)

                new_row = await conn.fetchrow(_SELECT_CURRENT, checked_new_path)

                now = self._clock().astimezone(UTC).replace(microsecond=0)
                new_final_bytes, old_final_bytes = rules.prepare_supersede_content(
                    path,
                    checked_new_path,
                    new_content,
                    old_bytes,
                    old_note_path=old_note_path,
                    new_note_path=new_note_path,
                    new_target_exists=new_row is not None,
                    now=now,
                )

                old_new_version = version(old_final_bytes)
                new_new_version = version(new_final_bytes)
                final_message = message or f"supersede {path} with {checked_new_path}"

                old_note_id, old_revision = await self._update_existing(
                    conn,
                    path,
                    old_final_bytes,
                    old_new_version,
                    old_row["id"],
                    old_row["current_revision"],
                )
                await conn.execute(
                    _INSERT_REVISION,
                    old_note_id,
                    old_revision,
                    path,
                    old_final_bytes,
                    old_new_version,
                    actor,
                    client,
                    final_message,
                )
                if self._index_hook is not None:
                    await self._index_hook(conn, path, old_final_bytes)

                new_note_id = parse(new_final_bytes).id
                inserted = await conn.fetchrow(
                    _INSERT_NEW,
                    new_note_id,
                    new_note_path.namespace,
                    checked_new_path,
                    new_final_bytes,
                    new_new_version,
                )
                if inserted is None:
                    raise InvalidNote(
                        checked_new_path, "already exists, supersede needs an unused path"
                    )
                new_revision = int(inserted["current_revision"])
                await conn.execute(
                    _INSERT_REVISION,
                    str(inserted["id"]),
                    new_revision,
                    checked_new_path,
                    new_final_bytes,
                    new_new_version,
                    actor,
                    client,
                    final_message,
                )
                if self._index_hook is not None:
                    await self._index_hook(conn, checked_new_path, new_final_bytes)
        except asyncpg.UniqueViolationError as exc:
            raise InvalidNote(new_path, str(exc)) from exc
        except asyncpg.PostgresError as exc:
            raise WriteFailed(str(exc)) from exc

        if self._index_commit_hook is not None:
            await self._index_commit_hook((old_note_id, str(inserted["id"])))

        return WriteResult(
            path=checked_new_path,
            version=new_new_version,
            commit=f"{inserted['id']}@{new_revision}",
            related={path: old_new_version},
        )

    async def changes_since(self, cursor: str | None) -> StorageChanges:
        """Notes added, modified or deleted since `cursor` (ADR-0007 §2).

        Runs in its own read-only `REPEATABLE READ` transaction, never
        competing with a write for a row lock. `cursor` round-trips
        `pg_snapshot_xmin(pg_current_snapshot())` - see the module
        docstring for why `xid8` order, not `created_at`/`revision`, is
        what makes a late-committing lower `xid` never get skipped, and
        why a long-running transaction anywhere in the cluster - not only
        one touching this vault - delays how soon every later-committed
        change is reported, never drops any of them.
        """
        lower = int(cursor) if cursor is not None else 0

        async with (
            self._pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            new_xmin: int = await conn.fetchval(_SELECT_SNAPSHOT_XMIN)
            rows = await conn.fetch(_SELECT_CHANGED_REVISIONS, lower, new_xmin)

            touched: set[str] = set()
            for row in rows:
                touched.add(row["path"])
                prev_path = row["prev_path"]
                if prev_path is not None and prev_path != row["path"]:
                    touched.add(prev_path)

            existing: set[str] = set()
            if touched:
                existing_rows = await conn.fetch(_SELECT_EXISTING_PATHS, list(touched))
                existing = {row["path"] for row in existing_rows}

        changed = tuple(sorted(path for path in touched if path in existing))
        deleted = tuple(sorted(path for path in touched if path not in existing))
        return StorageChanges(cursor=str(new_xmin), changed=changed, deleted=deleted)
