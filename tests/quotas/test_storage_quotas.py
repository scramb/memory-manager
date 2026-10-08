# SPDX-License-Identifier: AGPL-3.0-only
"""`quotas.StorageQuotaChecker` (#243): the note-count and byte-size budget per
namespace, Postgres mode only.

`backend` is a plain `PostgresBackend(pool)` - no `app_role` - the same shape
`tests/storage/test_postgres_backend.py` already runs its own contract suite
against: `StorageQuotaChecker.check_write` only ever calls `PostgresBackend.
namespace_usage`, which goes through `_content_connection` regardless of
whether `app_role` is configured, so these tests exercise exactly the query
`namespace_usage` runs without needing the full RLS/principal plumbing
`tests/mcp/test_permission_matrix.py` sets up for the MCP-tool-level wiring.

Each test writes real rows through `backend.write`/`backend.archive` first,
then asks `StorageQuotaChecker.check_write` whether a *further* write would
be allowed - the checker itself never performs the write it is asked about.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg
import pytest
import pytest_asyncio
from storage.contract import note_bytes

from memory_manager.quotas import StorageQuotaChecker, StorageQuotaExceeded
from memory_manager.storage.postgres import PostgresBackend


@pytest_asyncio.fixture
async def backend(pool: asyncpg.Pool) -> AsyncIterator[PostgresBackend]:
    """A `PostgresBackend` over `tests/quotas/conftest.py`'s migrated `pool` - no
    `app_role`, same as `tests/storage/test_postgres_backend.py`'s own fixture."""
    yield PostgresBackend(pool)


async def _write(backend: PostgresBackend, path: str, **overrides: object) -> bytes:
    """Write a fresh note at `path` and return the bytes actually committed."""
    content = note_bytes(**overrides)
    await backend.write(path, content, if_version="new", client="tester", actor="tester")
    return content


class TestNoteCountQuota:
    async def test_write_at_the_limit_is_rejected_below_is_accepted(
        self, backend: PostgresBackend
    ) -> None:
        checker = StorageQuotaChecker(storage=backend, max_notes_personal=1)

        # No notes yet in "alice" - below the limit of 1, the check allows it.
        await checker.check_write(
            op="write",
            path="alice/fact/a.md",
            namespace="alice",
            namespace_kind="personal",
            is_new_note=True,
            final_size=len(note_bytes()),
            actor="tester",
            client="tester-client",
        )
        await _write(backend, "alice/fact/a.md")

        # One note already there, at the limit of 1: a second new note is rejected.
        with pytest.raises(StorageQuotaExceeded) as exc_info:
            await checker.check_write(
                op="write",
                path="alice/fact/b.md",
                namespace="alice",
                namespace_kind="personal",
                is_new_note=True,
                final_size=len(note_bytes()),
                actor="tester",
                client="tester-client",
            )
        assert exc_info.value.resource == "notes"
        assert exc_info.value.namespace_kind == "personal"
        assert exc_info.value.limit == 1
        assert exc_info.value.predicted == 2

    async def test_editing_an_existing_note_never_counts_against_the_note_limit(
        self, backend: PostgresBackend
    ) -> None:
        checker = StorageQuotaChecker(storage=backend, max_notes_personal=1)
        await _write(backend, "alice/fact/a.md")

        # The namespace is already at its note-count limit, but this is an edit
        # of the existing note, not a new one - `is_new_note=False` skips the
        # note-count check entirely.
        await checker.check_write(
            op="edit",
            path="alice/fact/a.md",
            namespace="alice",
            namespace_kind="personal",
            is_new_note=False,
            final_size=len(note_bytes()),
            actor="tester",
            client="tester-client",
        )

    async def test_archive_frees_a_count_slot(self, backend: PostgresBackend) -> None:
        checker = StorageQuotaChecker(storage=backend, max_notes_personal=1)
        await _write(backend, "alice/fact/a.md")

        written = await backend.read("alice/fact/a.md")
        assert written is not None
        await backend.archive(
            "alice/fact/a.md", if_version=written.version, client="tester", actor="tester"
        )

        # The one note in "alice" is now archived - it still exists (and still
        # counts toward the byte-size budget, see TestByteSizeQuota below), but
        # no longer toward the note count, so a new note is accepted again.
        await checker.check_write(
            op="write",
            path="alice/fact/b.md",
            namespace="alice",
            namespace_kind="personal",
            is_new_note=True,
            final_size=len(note_bytes()),
            actor="tester",
            client="tester-client",
        )


class TestByteSizeQuota:
    async def test_edits_that_grow_a_note_respect_the_size_limit(
        self, backend: PostgresBackend
    ) -> None:
        content = await _write(backend, "alice/fact/a.md", body="Short body.\n")
        checker = StorageQuotaChecker(storage=backend, max_bytes_personal=len(content) + 5)

        # Growing by a little stays within the budget.
        await checker.check_write(
            op="edit",
            path="alice/fact/a.md",
            namespace="alice",
            namespace_kind="personal",
            is_new_note=False,
            final_size=len(content) + 3,
            actor="tester",
            client="tester-client",
        )

        # Growing past it is rejected.
        with pytest.raises(StorageQuotaExceeded) as exc_info:
            await checker.check_write(
                op="edit",
                path="alice/fact/a.md",
                namespace="alice",
                namespace_kind="personal",
                is_new_note=False,
                final_size=len(content) + 20,
                actor="tester",
                client="tester-client",
            )
        assert exc_info.value.resource == "bytes"

    async def test_archived_notes_still_count_toward_the_byte_budget(
        self, backend: PostgresBackend
    ) -> None:
        content = await _write(backend, "alice/fact/a.md", body="Short body.\n")
        written = await backend.read("alice/fact/a.md")
        assert written is not None
        await backend.archive(
            "alice/fact/a.md", if_version=written.version, client="tester", actor="tester"
        )
        checker = StorageQuotaChecker(storage=backend, max_bytes_personal=len(content))

        # The archived note alone already fills the byte budget - a brand new
        # note of any size is rejected, unlike the note-count budget above.
        with pytest.raises(StorageQuotaExceeded) as exc_info:
            await checker.check_write(
                op="write",
                path="alice/fact/b.md",
                namespace="alice",
                namespace_kind="personal",
                is_new_note=True,
                final_size=1,
                actor="tester",
                client="tester-client",
            )
        assert exc_info.value.resource == "bytes"


class TestScopesAreIndependent:
    async def test_personal_and_shared_limits_are_independent(
        self, backend: PostgresBackend
    ) -> None:
        checker = StorageQuotaChecker(storage=backend, max_notes_personal=1, max_notes_shared=2)
        await _write(backend, "alice/fact/a.md")
        await _write(backend, "team-x/fact/a.md")

        # "alice" (personal) is already at its own limit of 1.
        with pytest.raises(StorageQuotaExceeded):
            await checker.check_write(
                op="write",
                path="alice/fact/b.md",
                namespace="alice",
                namespace_kind="personal",
                is_new_note=True,
                final_size=1,
                actor="tester",
                client="tester-client",
            )

        # "team-x" (shared) has its own, higher limit of 2, and is only at 1 -
        # unaffected by "alice" having just been rejected.
        await checker.check_write(
            op="write",
            path="team-x/fact/b.md",
            namespace="team-x",
            namespace_kind="shared",
            is_new_note=True,
            final_size=1,
            actor="tester",
            client="tester-client",
        )

    async def test_notes_and_bytes_limits_are_independent(self, backend: PostgresBackend) -> None:
        content = await _write(backend, "alice/fact/a.md", body="Short body.\n")
        checker = StorageQuotaChecker(
            storage=backend, max_notes_personal=5, max_bytes_personal=len(content)
        )

        # Well under the note-count limit, but a new note of any size would
        # push the namespace over its byte-size limit.
        with pytest.raises(StorageQuotaExceeded) as exc_info:
            await checker.check_write(
                op="write",
                path="alice/fact/b.md",
                namespace="alice",
                namespace_kind="personal",
                is_new_note=True,
                final_size=10,
                actor="tester",
                client="tester-client",
            )
        assert exc_info.value.resource == "bytes"


class TestOffByDefault:
    async def test_no_limits_configured_allows_any_write(self, backend: PostgresBackend) -> None:
        checker = StorageQuotaChecker(storage=backend)

        # Every limit defaults to 0 (off) - allowed outright, regardless of size.
        await checker.check_write(
            op="write",
            path="alice/fact/a.md",
            namespace="alice",
            namespace_kind="personal",
            is_new_note=True,
            final_size=10_000_000,
            actor="tester",
            client="tester-client",
        )
