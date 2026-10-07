# SPDX-License-Identifier: AGPL-3.0-only
"""The Postgres implementation of `StorageBackend` (ADR-0007 §2, enterprise mode).

Unlike `storage.git.GitBackend`, which delegates to the serialized
`WriteQueue`, `PostgresBackend` talks straight to `vault_notes`/
`vault_revisions` (migration `0004_vault.sql`): Postgres writes are already
serialized per row by the database, through the same `UPDATE ... WHERE
current_revision = $n` idiom `queue.py`'s Git path uses at the file level.
`write`/`edit` build a `WriteRequest` exactly like `GitBackend` does and
hand it to one private flow, `_write_or_edit`, that both call.

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
"""

from __future__ import annotations

import asyncpg

from memory_manager.storage import rules
from memory_manager.storage.base import (
    InvalidNote,
    StoredNote,
    VersionConflict,
    WriteFailed,
    WriteRequest,
    WriteResult,
)
from memory_manager.vault.note import parse, version
from memory_manager.vault.paths import parse_note_path

__all__ = ["PostgresBackend"]

_SELECT_CURRENT = """
select id, content, version, current_revision
from vault_notes
where path = $1
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

_INSERT_REVISION = """
insert into vault_revisions (note_id, revision, path, content, version, author, client, message)
values ($1, $2, $3, $4, $5, $6, $7, $8)
"""


class PostgresBackend:
    """`StorageBackend` backed by `vault_notes`/`vault_revisions` (ADR-0007 §2).

    Only `read`/`write`/`edit` are implemented (#96): `list`, `supersede`,
    `archive` and `changes_since` are #97's job, so this class does not
    satisfy the full `StorageBackend` protocol yet.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def read(self, path: str) -> StoredNote | None:
        """The note at `path`, or `None` if it does not exist.

        Raises `PathRejected` for a `path` that is not a safe, valid note
        path, same as `GitBackend.read` - callers rely on this, not on it
        being converted to an `InvalidNote` here.
        """
        parse_note_path(path, allow_archive=True)
        row = await self._pool.fetchrow(_SELECT_CURRENT, path)
        if row is None:
            return None
        return StoredNote(path=path, content=bytes(row["content"]), version=row["version"])

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
        """`write`/`edit`'s shared flow: validate, then one `READ COMMITTED` transaction.

        `WriteResult.commit` is `"<note id>@<revision>"` - a stable, unique
        string per revision of a note (not a Git sha; there is no commit
        here), good enough for a caller that only needs "what changed,
        identifiably" out of it.
        """
        note_path = rules.parse_note_path_or_raise(request.path, allow_archive=True)

        try:
            async with self._pool.acquire() as conn, conn.transaction():
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
        except asyncpg.UniqueViolationError as exc:
            raise InvalidNote(request.path, str(exc)) from exc
        except asyncpg.PostgresError as exc:
            raise WriteFailed(str(exc)) from exc

        return WriteResult(path=request.path, version=new_version, commit=f"{note_id}@{revision}")

    async def _insert_new(
        self,
        conn: asyncpg.pool.PoolConnectionProxy,
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
        conn: asyncpg.pool.PoolConnectionProxy,
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
        self, conn: asyncpg.pool.PoolConnectionProxy, path: str
    ) -> tuple[str, str]:
        """`(version, content)` of the row a losing writer's conflict is reported against.

        Re-read inside the same transaction right after losing an `INSERT
        ... ON CONFLICT`/conditional `UPDATE` race, so this always reflects
        the winner, never a stale snapshot. The row is guaranteed to exist:
        something else just committed it, in the same transaction's view.
        """
        winner = await conn.fetchrow(_SELECT_CURRENT, path)
        if winner is None:  # pragma: no cover - defensive, should be unreachable
            raise WriteFailed(f"'{path}' vanished mid-transaction")
        content = bytes(winner["content"]).decode("utf-8", errors="replace")
        return winner["version"], content
