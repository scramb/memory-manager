# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the no-database search fallback (`memory_manager.search_fallback`, #30)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from memory_manager.search import SearchFilters
from memory_manager.search_fallback import scan_search
from memory_manager.vault.note import Note, serialize
from memory_manager.vault.ulid import new_ulid

_CREATED = datetime(2026, 1, 1, tzinfo=UTC)

_NO_FILTERS = SearchFilters()


def _note(**overrides: object) -> Note:
    defaults: dict[str, object] = {
        "id": new_ulid(_CREATED),
        "title": "A valid note",
        "description": "A valid description.",
        "type": "fact",
        "created": _CREATED,
        "updated": _CREATED,
        "body": "Body text.\n",
        "tags": (),
        "aliases": (),
        "valid_from": None,
        "valid_to": None,
        "supersedes": (),
        "source": None,
    }
    defaults.update(overrides)
    return Note(**defaults)  # type: ignore[arg-type]


def _write(vault_root: Path, rel: str, data: bytes) -> Path:
    path = vault_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class TestScanSearch:
    def test_finds_a_note_by_title_term(self, tmp_path: Path) -> None:
        _write(tmp_path, "personal/fact/color.md", serialize(_note(title="Favorite color")))

        hits = scan_search(tmp_path, "color", filters=_NO_FILTERS, limit=10)

        assert [hit.path for hit in hits] == ["personal/fact/color.md"]


class TestSymlinkSafety:
    def test_symlinked_note_file_is_never_scanned_or_leaked_in_a_snippet(
        self, tmp_path: Path
    ) -> None:
        # Reproduces #51: a symlink inside the vault pointing at a file outside it
        # must never have its body read, scored or returned in a `memory_search`
        # snippet - a secret-bearing query term must not surface it either.
        # `tmp_path.parent` is pytest's shared session base temp dir, not unique
        # to this test - the name below is scoped to this module so it can never
        # collide with another test file's own "outside the vault" fixture file.
        outside = tmp_path.parent / "outside-the-vault-search-fallback.md"
        outside.write_bytes(serialize(_note(title="Leaked secret", body="top-secret-body-term\n")))
        link = tmp_path / "personal" / "fact" / "evil.md"
        link.parent.mkdir(parents=True)
        link.symlink_to(outside)

        hits = scan_search(tmp_path, "top-secret-body-term", filters=_NO_FILTERS, limit=10)

        assert hits == []

    def test_note_reached_through_a_symlinked_directory_is_never_scanned(
        self, tmp_path: Path
    ) -> None:
        # Same shared-base-temp-dir caveat as above - scoped name, and
        # `exist_ok=True` as defence in depth against a leftover directory from
        # an earlier, interrupted run.
        outside = tmp_path.parent / "outside-dir-search-fallback"
        outside.mkdir(exist_ok=True)
        _write(outside, "fact/leaky.md", serialize(_note(body="top-secret-body-term\n")))
        (tmp_path / "personal").symlink_to(outside)

        hits = scan_search(tmp_path, "top-secret-body-term", filters=_NO_FILTERS, limit=10)

        assert hits == []
