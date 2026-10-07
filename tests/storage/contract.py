# SPDX-License-Identifier: AGPL-3.0-only
"""Backend-agnostic contract for `StorageBackend` (ADR-0007 §1, #94).

Every class here is a mixin of test methods against a `backend` fixture a
concrete test module provides (see `tests/storage/test_git_backend.py`);
none of these classes is named `Test...` itself, so pytest never tries to
collect one directly (it has no `backend` fixture of its own to resolve).
Split by capability so a backend that only supports a subset can run just
that subset (#96, the Postgres backend, only exercises
`ReadWriteEditContract`):

- `ReadWriteEditContract`: `read`/`write`/`edit`, `if_version` enforcement.
- `SupersedeArchiveContract`: `supersede`/`archive`.
- `ListContract`: `list`, with and without archived notes.
- `ChangesSinceContract`: `changes_since`.

Deliberately out of scope: `WriteConflict`/`WriteFailed` (a Git-specific
remote push race, not part of what every backend must satisfy) and exact
timestamps (no assertion here depends on wall-clock precision).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from memory_manager.storage.base import (
    EditMismatch,
    InvalidNote,
    NotFound,
    SecretRejected,
    StorageBackend,
    VersionConflict,
)
from memory_manager.vault.note import Note, parse, serialize, version
from memory_manager.vault.ulid import new_ulid

_CREATED = datetime(2026, 1, 1, tzinfo=UTC)
#: A well-known AWS *example* access key id (never a real credential, see
#: AWS's own docs), split across a concatenation so the repo's own secret
#: scan never flags this literal.
_FAKE_AWS_ACCESS_KEY_ID = "AKIA" + "IOSFODNN7EXAMPLE"


def note_bytes(**overrides: object) -> bytes:
    """A minimal, valid note's canonical bytes, `title`/`body`/... overridable."""
    defaults: dict[str, object] = {
        "id": new_ulid(_CREATED),
        "title": "A note",
        "description": "A description.",
        "type": "fact",
        "created": _CREATED,
        "updated": _CREATED,
        "body": "Body.\n",
    }
    defaults.update(overrides)
    return serialize(Note(**defaults))  # type: ignore[arg-type]


class ReadWriteEditContract:
    """`read`/`write`/`edit` and `if_version` enforcement."""

    async def test_read_missing_returns_none(self, backend: StorageBackend) -> None:
        assert await backend.read("personal/fact/missing.md") is None

    async def test_write_then_read_round_trips(self, backend: StorageBackend) -> None:
        content = note_bytes()
        result = await backend.write(
            "personal/fact/a.md", content, if_version="new", client="claude-code"
        )
        assert result.version == version(content)

        stored = await backend.read("personal/fact/a.md")
        assert stored is not None
        assert stored.content == content
        assert stored.version == result.version

    async def test_write_new_on_existing_raises_version_conflict_with_current_content(
        self, backend: StorageBackend
    ) -> None:
        first = note_bytes()
        await backend.write("personal/fact/a.md", first, if_version="new", client="claude-code")

        with pytest.raises(VersionConflict) as excinfo:
            await backend.write(
                "personal/fact/a.md",
                note_bytes(title="Second"),
                if_version="new",
                client="claude-code",
            )
        assert excinfo.value.current_version == version(first)
        assert excinfo.value.current_content == first.decode("utf-8")

    async def test_stale_if_version_on_write_is_rejected_and_nothing_changes(
        self, backend: StorageBackend
    ) -> None:
        first = note_bytes()
        await backend.write("personal/fact/a.md", first, if_version="new", client="claude-code")

        with pytest.raises(VersionConflict):
            await backend.write(
                "personal/fact/a.md",
                note_bytes(title="Changed"),
                if_version="0" * 64,
                client="claude-code",
            )

        stored = await backend.read("personal/fact/a.md")
        assert stored is not None
        assert stored.content == first

    async def test_stale_if_version_on_edit_is_rejected_and_nothing_changes(
        self, backend: StorageBackend
    ) -> None:
        first = note_bytes(body="Original body.\n")
        await backend.write("personal/fact/a.md", first, if_version="new", client="claude-code")

        with pytest.raises(VersionConflict):
            await backend.edit(
                "personal/fact/a.md",
                "Original body.",
                "Edited body.",
                if_version="0" * 64,
                client="claude-code",
            )

        stored = await backend.read("personal/fact/a.md")
        assert stored is not None
        assert stored.content == first

    async def test_edit_replaces_the_one_occurrence(self, backend: StorageBackend) -> None:
        first = note_bytes(body="Original body.\n")
        written = await backend.write(
            "personal/fact/a.md", first, if_version="new", client="claude-code"
        )

        result = await backend.edit(
            "personal/fact/a.md",
            "Original",
            "Edited",
            if_version=written.version,
            client="claude-code",
        )

        stored = await backend.read("personal/fact/a.md")
        assert stored is not None
        assert b"Edited body." in stored.content
        assert stored.version == result.version

    async def test_edit_mismatch_is_rejected_without_writing(self, backend: StorageBackend) -> None:
        first = note_bytes(body="Original body.\n")
        written = await backend.write(
            "personal/fact/a.md", first, if_version="new", client="claude-code"
        )

        with pytest.raises(EditMismatch):
            await backend.edit(
                "personal/fact/a.md",
                "nonexistent text",
                "replacement",
                if_version=written.version,
                client="claude-code",
            )

        stored = await backend.read("personal/fact/a.md")
        assert stored is not None
        assert stored.content == first

    async def test_invalid_note_is_rejected_without_writing(self, backend: StorageBackend) -> None:
        with pytest.raises(InvalidNote):
            await backend.write(
                "personal/fact/a.md", b"not a note at all", if_version="new", client="claude-code"
            )

        assert await backend.read("personal/fact/a.md") is None

    async def test_secret_is_rejected_without_writing(self, backend: StorageBackend) -> None:
        content = note_bytes(body=f"AWS key: {_FAKE_AWS_ACCESS_KEY_ID}\n")

        with pytest.raises(SecretRejected):
            await backend.write(
                "personal/fact/a.md", content, if_version="new", client="claude-code"
            )

        assert await backend.read("personal/fact/a.md") is None


class SupersedeArchiveContract:
    """`supersede`/`archive`."""

    async def test_supersede_sets_supersedes_and_valid_to(self, backend: StorageBackend) -> None:
        old_content = note_bytes()
        old_id = parse(old_content).id
        old_written = await backend.write(
            "personal/fact/old.md", old_content, if_version="new", client="claude-code"
        )

        result = await backend.supersede(
            "personal/fact/old.md",
            "personal/fact/new.md",
            note_bytes(title="New note"),
            if_version=old_written.version,
            client="claude-code",
        )

        new_stored = await backend.read("personal/fact/new.md")
        assert new_stored is not None
        assert old_id in parse(new_stored.content).supersedes

        old_stored = await backend.read("personal/fact/old.md")
        assert old_stored is not None
        assert parse(old_stored.content).valid_to is not None
        assert result.related == {"personal/fact/old.md": old_stored.version}

    async def test_archive_missing_raises_not_found(self, backend: StorageBackend) -> None:
        with pytest.raises(NotFound):
            await backend.archive(
                "personal/fact/missing.md", if_version="new", client="claude-code"
            )

    async def test_archive_moves_the_note(self, backend: StorageBackend) -> None:
        written = await backend.write(
            "personal/fact/a.md", note_bytes(), if_version="new", client="claude-code"
        )

        await backend.archive(
            "personal/fact/a.md", if_version=written.version, client="claude-code"
        )

        assert await backend.read("personal/fact/a.md") is None
        archived = await backend.read("_archive/personal/fact/a.md")
        assert archived is not None

    async def test_archive_occupied_target_raises_invalid_note(
        self, backend: StorageBackend
    ) -> None:
        first = await backend.write(
            "personal/fact/a.md", note_bytes(), if_version="new", client="claude-code"
        )
        await backend.archive("personal/fact/a.md", if_version=first.version, client="claude-code")

        second = await backend.write(
            "personal/fact/a.md", note_bytes(title="Second"), if_version="new", client="claude-code"
        )

        with pytest.raises(InvalidNote):
            await backend.archive(
                "personal/fact/a.md", if_version=second.version, client="claude-code"
            )

        # Nothing was overwritten: the second note is still live, the first
        # archive is still intact (CLAUDE.md: never overwrite silently).
        assert await backend.read("personal/fact/a.md") is not None
        assert await backend.read("_archive/personal/fact/a.md") is not None


class ListContract:
    """`list`, with and without archived notes."""

    async def test_list_excludes_archived_by_default(self, backend: StorageBackend) -> None:
        await backend.write(
            "personal/fact/a.md", note_bytes(), if_version="new", client="claude-code"
        )
        to_archive = await backend.write(
            "personal/fact/b.md", note_bytes(title="B"), if_version="new", client="claude-code"
        )
        await backend.archive(
            "personal/fact/b.md", if_version=to_archive.version, client="claude-code"
        )

        paths = {entry.path for entry in await backend.list()}
        assert "personal/fact/a.md" in paths
        assert "personal/fact/b.md" not in paths
        assert "_archive/personal/fact/b.md" not in paths

    async def test_list_include_archived(self, backend: StorageBackend) -> None:
        to_archive = await backend.write(
            "personal/fact/b.md", note_bytes(), if_version="new", client="claude-code"
        )
        await backend.archive(
            "personal/fact/b.md", if_version=to_archive.version, client="claude-code"
        )

        paths = {entry.path for entry in await backend.list(include_archived=True)}
        assert "_archive/personal/fact/b.md" in paths


class ChangesSinceContract:
    """`changes_since`."""

    async def test_changes_since_none_reports_a_write(self, backend: StorageBackend) -> None:
        await backend.write(
            "personal/fact/a.md", note_bytes(), if_version="new", client="claude-code"
        )

        changes = await backend.changes_since(None)

        assert "personal/fact/a.md" in changes.changed
        assert changes.cursor

    async def test_changes_since_after_an_archive_reports_delete_and_add(
        self, backend: StorageBackend
    ) -> None:
        written = await backend.write(
            "personal/fact/a.md", note_bytes(), if_version="new", client="claude-code"
        )
        baseline = await backend.changes_since(None)

        await backend.archive(
            "personal/fact/a.md", if_version=written.version, client="claude-code"
        )

        changes = await backend.changes_since(baseline.cursor)
        assert "personal/fact/a.md" in changes.deleted
        assert "_archive/personal/fact/a.md" in changes.changed
