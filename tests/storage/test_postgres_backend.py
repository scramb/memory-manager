# SPDX-License-Identifier: AGPL-3.0-only
"""`PostgresBackend` against the backend-agnostic contract suite, plus the
Postgres-specific guarantees ADR-0007 §2 adds on top of it (#96, #97):
`vault_revisions` stays append-only and exactly one row per actual change,
and a stale `if_version` - including the two writers racing each other
directly, not just one arriving after the other - never loses a write
silently (CLAUDE.md).

`backend` is typed `AsyncIterator[StorageBackend]`: this backend now
implements the full protocol (`read`/`write`/`edit`/`list`/`supersede`/
`archive`/`changes_since`), so it runs every capability mixin in
`storage.contract`, the same as `GitBackend` does in
`test_git_backend.py`. Assertions on `vault_notes`/`vault_revisions` below
go through a short-lived connection of their own rather than reaching into
`backend`'s internals, same as any other caller could.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import asyncpg
import pytest
import pytest_asyncio
from storage.contract import (
    AuditEntry,
    ChangesSinceContract,
    ListContract,
    PromoteContract,
    ReadWriteEditContract,
    SupersedeArchiveContract,
    audit_recorder,
    note_bytes,
    poll_changes_since,
)

from memory_manager.db.migrate import migrate
from memory_manager.storage.base import (
    InvalidNote,
    SecretRejected,
    StorageBackend,
    VersionConflict,
    WriteResult,
)
from memory_manager.storage.postgres import PostgresBackend
from memory_manager.vault.note import parse, version
from memory_manager.vault.ulid import new_ulid

_CREATED = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def audit_log() -> list[AuditEntry]:
    """Every `AuditHook` call the `backend` fixture below fires, in order."""
    return []


@pytest_asyncio.fixture
async def backend(
    test_database_url: str, audit_log: list[AuditEntry]
) -> AsyncIterator[StorageBackend]:
    """A `PostgresBackend` over a freshly migrated, empty test database."""
    conn = await asyncpg.connect(test_database_url)
    try:
        await migrate(conn)
    finally:
        await conn.close()
    pool = await asyncpg.create_pool(test_database_url)
    pg_backend = PostgresBackend(pool)
    pg_backend.add_audit_hook(audit_recorder(audit_log))
    try:
        yield pg_backend
    finally:
        await pool.close()


async def _revision_rows(test_database_url: str, path: str) -> list[asyncpg.Record]:
    conn = await asyncpg.connect(test_database_url)
    try:
        return await conn.fetch(
            "select * from vault_revisions where path = $1 order by revision", path
        )
    finally:
        await conn.close()


async def _current_revision(test_database_url: str, path: str) -> int:
    conn = await asyncpg.connect(test_database_url)
    try:
        value: int = await conn.fetchval(
            "select current_revision from vault_notes where path = $1", path
        )
        return value
    finally:
        await conn.close()


async def _notes_with_id(test_database_url: str, note_id: str) -> int:
    conn = await asyncpg.connect(test_database_url)
    try:
        value: int = await conn.fetchval("select count(*) from vault_notes where id = $1", note_id)
        return value
    finally:
        await conn.close()


class TestReadWriteEdit(ReadWriteEditContract):
    pass


class TestSupersedeArchive(SupersedeArchiveContract):
    pass


class TestPromote(PromoteContract):
    pass


class TestList(ListContract):
    pass


class TestChangesSince(ChangesSinceContract):
    pass


class TestRevisions:
    async def test_write_then_edit_appends_exactly_two_matching_revisions(
        self, backend: PostgresBackend, test_database_url: str
    ) -> None:
        written = await backend.write(
            "personal/fact/a.md",
            note_bytes(body="Original body.\n"),
            if_version="new",
            client="claude-code",
            actor="user-1",
        )
        edited = await backend.edit(
            "personal/fact/a.md",
            "Original",
            "Edited",
            if_version=written.version,
            client="claude-desktop",
            actor="user-2",
        )

        rows = await _revision_rows(test_database_url, "personal/fact/a.md")
        assert [row["revision"] for row in rows] == [1, 2]
        assert rows[0]["version"] == written.version
        assert rows[0]["client"] == "claude-code"
        assert rows[0]["author"] == "user-1"
        assert rows[0]["path"] == "personal/fact/a.md"
        assert rows[1]["version"] == edited.version
        assert rows[1]["client"] == "claude-desktop"
        assert rows[1]["author"] == "user-2"
        assert b"Edited body." in bytes(rows[1]["content"])

        assert await _current_revision(test_database_url, "personal/fact/a.md") == 2

    async def test_rejected_write_leaves_no_revision_row(
        self, backend: PostgresBackend, test_database_url: str
    ) -> None:
        written = await backend.write(
            "personal/fact/a.md", note_bytes(), if_version="new", client="claude-code"
        )

        with pytest.raises(VersionConflict):
            await backend.write(
                "personal/fact/a.md",
                note_bytes(title="Second"),
                if_version="0" * 64,
                client="claude-code",
            )

        aws_key = "AKIA" + "IOSFODNN7EXAMPLE"
        with pytest.raises(SecretRejected):
            await backend.edit(
                "personal/fact/a.md",
                "Body.",
                f"Body with a key: {aws_key}",
                if_version=written.version,
                client="claude-code",
            )

        rows = await _revision_rows(test_database_url, "personal/fact/a.md")
        assert len(rows) == 1

    async def test_concurrent_updates_with_the_same_if_version_leave_one_winner(
        self, backend: PostgresBackend, test_database_url: str
    ) -> None:
        written = await backend.write(
            "personal/fact/a.md", note_bytes(body="Original body.\n"), if_version="new", client="c"
        )

        results = await asyncio.gather(
            backend.edit(
                "personal/fact/a.md", "Original", "First", if_version=written.version, client="c"
            ),
            backend.edit(
                "personal/fact/a.md", "Original", "Second", if_version=written.version, client="c"
            ),
            return_exceptions=True,
        )

        successes = [r for r in results if isinstance(r, WriteResult)]
        conflicts = [r for r in results if isinstance(r, VersionConflict)]
        assert len(successes) == 1
        assert len(conflicts) == 1
        assert conflicts[0].current_version == successes[0].version

        rows = await _revision_rows(test_database_url, "personal/fact/a.md")
        assert len(rows) == 2  # the initial write plus exactly one winning edit

    async def test_concurrent_new_writes_on_one_path_leave_one_winner(
        self, backend: PostgresBackend, test_database_url: str
    ) -> None:
        results = await asyncio.gather(
            backend.write(
                "personal/fact/a.md", note_bytes(title="First"), if_version="new", client="c"
            ),
            backend.write(
                "personal/fact/a.md", note_bytes(title="Second"), if_version="new", client="c"
            ),
            return_exceptions=True,
        )

        successes = [r for r in results if isinstance(r, WriteResult)]
        conflicts = [r for r in results if isinstance(r, VersionConflict)]
        assert len(successes) == 1
        assert len(conflicts) == 1
        assert conflicts[0].current_version == successes[0].version

        rows = await _revision_rows(test_database_url, "personal/fact/a.md")
        assert len(rows) == 1

    async def test_new_note_with_an_id_used_elsewhere_is_rejected_without_writing(
        self, backend: PostgresBackend, test_database_url: str
    ) -> None:
        shared_id = new_ulid(_CREATED)
        await backend.write(
            "personal/fact/a.md",
            note_bytes(id=shared_id),
            if_version="new",
            client="claude-code",
        )

        with pytest.raises(InvalidNote):
            await backend.write(
                "personal/fact/b.md",
                note_bytes(id=shared_id, title="Second"),
                if_version="new",
                client="claude-code",
            )

        assert await backend.read("personal/fact/b.md") is None
        assert await _notes_with_id(test_database_url, shared_id) == 1


class TestArchiveAndSupersedeConcurrency:
    """Postgres-specific guarantees on top of `SupersedeArchiveContract` (#97)."""

    async def test_archive_appends_a_revision_with_the_new_path_and_bumps_current_revision(
        self, backend: PostgresBackend, test_database_url: str
    ) -> None:
        written = await backend.write(
            "personal/fact/a.md", note_bytes(), if_version="new", client="claude-code"
        )

        await backend.archive(
            "personal/fact/a.md", if_version=written.version, client="claude-code"
        )

        rows = await _revision_rows(test_database_url, "_archive/personal/fact/a.md")
        assert len(rows) == 1
        assert rows[0]["revision"] == 2
        assert await _current_revision(test_database_url, "_archive/personal/fact/a.md") == 2

    async def test_concurrent_edit_racing_archive_yields_version_conflict_never_write_failed(
        self, backend: PostgresBackend, test_database_url: str
    ) -> None:
        written = await backend.write(
            "personal/fact/a.md",
            note_bytes(body="Original body.\n"),
            if_version="new",
            client="c",
        )

        results = await asyncio.gather(
            backend.archive("personal/fact/a.md", if_version=written.version, client="c"),
            backend.edit(
                "personal/fact/a.md",
                "Original",
                "Edited",
                if_version=written.version,
                client="c",
            ),
            return_exceptions=True,
        )

        successes = [r for r in results if isinstance(r, WriteResult)]
        conflicts = [r for r in results if isinstance(r, VersionConflict)]
        others = [
            r for r in results if isinstance(r, Exception) and not isinstance(r, VersionConflict)
        ]
        assert len(successes) == 1
        assert len(conflicts) == 1
        assert others == []

    async def test_supersede_onto_occupied_target_leaves_nothing_behind(
        self, backend: PostgresBackend, test_database_url: str
    ) -> None:
        old = await backend.write(
            "personal/fact/old.md", note_bytes(), if_version="new", client="c"
        )
        await backend.write(
            "personal/fact/new.md",
            note_bytes(title="Already there"),
            if_version="new",
            client="c",
        )

        with pytest.raises(InvalidNote):
            await backend.supersede(
                "personal/fact/old.md",
                "personal/fact/new.md",
                note_bytes(title="New note"),
                if_version=old.version,
                client="c",
            )

        old_rows = await _revision_rows(test_database_url, "personal/fact/old.md")
        assert len(old_rows) == 1  # just the initial write, the failed supersede appended nothing

        stored_old = await backend.read("personal/fact/old.md")
        assert stored_old is not None
        assert parse(stored_old.content).valid_to is None


class TestChangesSinceConcurrency:
    """Postgres-specific guarantees `changes_since`'s `xid8` cursor adds (#97)."""

    async def test_does_not_skip_a_lower_xid_committing_later(
        self, backend: PostgresBackend, test_database_url: str
    ) -> None:
        conn_a = await asyncpg.connect(test_database_url)
        tr_a = conn_a.transaction()
        await tr_a.start()
        try:
            note_a = note_bytes(title="A")
            note_a_id = parse(note_a).id
            await conn_a.execute(
                "insert into vault_notes "
                "(id, namespace, path, content, version, current_revision) "
                "values ($1, 'personal', 'personal/fact/a.md', $2, $3, 1)",
                note_a_id,
                note_a,
                version(note_a),
            )
            await conn_a.execute(
                "insert into vault_revisions "
                "(note_id, revision, path, content, version, author, client) "
                "values ($1, 1, 'personal/fact/a.md', $2, $3, 'tester', 'test')",
                note_a_id,
                note_a,
                version(note_a),
            )

            # B writes and commits on an unrelated connection while A is still
            # open - A's lower, still in-flight xid holds the snapshot xmin
            # back, so B's already-committed write must not be reported yet
            # either: the window can never advance past A.
            await backend.write(
                "personal/fact/b.md", note_bytes(title="B"), if_version="new", client="c"
            )

            baseline = await backend.changes_since(None)
            assert "personal/fact/a.md" not in baseline.changed
            assert "personal/fact/b.md" not in baseline.changed

            await tr_a.commit()
        finally:
            await conn_a.close()

        # A backend that skipped xid(A) would never see both paths show up
        # here and hit the poll's deadline instead.
        after = await poll_changes_since(
            backend,
            baseline.cursor,
            lambda changes: (
                "personal/fact/a.md" in changes.changed and "personal/fact/b.md" in changes.changed
            ),
        )
        assert "personal/fact/a.md" in after.changed
        assert "personal/fact/b.md" in after.changed

    async def test_archived_then_reoccupied_path_is_changed_not_deleted(
        self, backend: PostgresBackend, test_database_url: str
    ) -> None:
        written = await backend.write(
            "personal/fact/a.md", note_bytes(), if_version="new", client="c"
        )
        baseline = await backend.changes_since(None)

        await backend.archive("personal/fact/a.md", if_version=written.version, client="c")
        await backend.write(
            "personal/fact/a.md", note_bytes(title="Reoccupied"), if_version="new", client="c"
        )

        # Poll until the archive itself is in the window - "a.md in changed"
        # alone could already be true before the archive ever showed up, from
        # the reoccupying write landing in an earlier call's snapshot.
        changes = await poll_changes_since(
            backend,
            baseline.cursor,
            lambda changes: "_archive/personal/fact/a.md" in changes.changed,
        )
        assert "personal/fact/a.md" in changes.changed
        assert "personal/fact/a.md" not in changes.deleted
