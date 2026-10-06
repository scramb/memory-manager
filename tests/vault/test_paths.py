# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for vault path parsing and safe resolution (ADR-0005, #9)."""

import os
from pathlib import Path

import pytest

from memory_manager.vault.paths import (
    NotePath,
    PathRejected,
    conflict_path,
    parse_note_path,
    resolve,
    resolve_internal,
)


class TestParseNotePathValid:
    def test_live_path_is_accepted(self) -> None:
        note_path = parse_note_path("personal/fact/favorite-color.md")
        assert note_path == NotePath(
            namespace="personal", type="fact", slug="favorite-color", archived=False
        )
        assert note_path.relative == "personal/fact/favorite-color.md"

    def test_archive_path_is_accepted_when_allowed(self) -> None:
        note_path = parse_note_path("_archive/personal/fact/favorite-color.md", allow_archive=True)
        assert note_path == NotePath(
            namespace="personal", type="fact", slug="favorite-color", archived=True
        )
        assert note_path.relative == "_archive/personal/fact/favorite-color.md"

    def test_single_char_namespace_and_slug_are_accepted(self) -> None:
        note_path = parse_note_path("a/fact/b.md")
        assert note_path.namespace == "a"
        assert note_path.slug == "b"

    def test_namespace_at_max_length_is_accepted(self) -> None:
        namespace = "a" + "b" * 39
        assert len(namespace) == 40
        parse_note_path(f"{namespace}/fact/x.md")

    def test_slug_at_max_length_is_accepted(self) -> None:
        slug = "a" + "b" * 79
        assert len(slug) == 80
        parse_note_path(f"personal/fact/{slug}.md")

    @pytest.mark.parametrize("note_type", ["user", "feedback", "project", "reference", "fact"])
    def test_each_enum_type_is_accepted(self, note_type: str) -> None:
        note_path = parse_note_path(f"personal/{note_type}/x.md")
        assert note_path.type == note_type


class TestParseNotePathRejections:
    def test_empty_path_is_rejected(self) -> None:
        with pytest.raises(PathRejected):
            parse_note_path("")

    def test_too_long_path_is_rejected(self) -> None:
        rel = f"personal/fact/{'x' * 190}.md"
        assert len(rel) > 200
        with pytest.raises(PathRejected) as excinfo:
            parse_note_path(rel)
        assert "max 200" in str(excinfo.value)

    def test_nul_byte_is_rejected(self) -> None:
        with pytest.raises(PathRejected):
            parse_note_path("personal/fact/\x00.md")

    def test_control_character_is_rejected(self) -> None:
        with pytest.raises(PathRejected):
            parse_note_path("personal/fact/x\x01y.md")

    def test_backslash_is_rejected(self) -> None:
        with pytest.raises(PathRejected):
            parse_note_path("personal\\fact\\x.md")

    def test_percent_encoded_traversal_is_rejected(self) -> None:
        with pytest.raises(PathRejected):
            parse_note_path("personal/fact/%2e%2e.md")

    def test_bare_percent_is_rejected(self) -> None:
        with pytest.raises(PathRejected):
            parse_note_path("personal/fact/100%done.md")

    def test_leading_slash_is_rejected(self) -> None:
        with pytest.raises(PathRejected):
            parse_note_path("/personal/fact/x.md")

    def test_trailing_slash_is_rejected(self) -> None:
        with pytest.raises(PathRejected):
            parse_note_path("personal/fact/x.md/")

    def test_double_slash_is_rejected(self) -> None:
        with pytest.raises(PathRejected):
            parse_note_path("personal//fact/x.md")

    def test_dot_segment_is_rejected(self) -> None:
        with pytest.raises(PathRejected):
            parse_note_path("personal/./x.md")

    def test_dotdot_traversal_is_rejected(self) -> None:
        with pytest.raises(PathRejected):
            parse_note_path("personal/../../etc/passwd")

    def test_fullwidth_solidus_is_rejected(self) -> None:
        # U+FF0F FULLWIDTH SOLIDUS, built via chr() to keep this source file ASCII-only.
        solidus = chr(0xFF0F)
        with pytest.raises(PathRejected):
            parse_note_path(f"personal{solidus}fact{solidus}x.md")

    def test_division_slash_is_rejected(self) -> None:
        # U+2215 DIVISION SLASH, built via chr() to keep this source file ASCII-only.
        solidus = chr(0x2215)
        with pytest.raises(PathRejected):
            parse_note_path(f"personal{solidus}fact{solidus}x.md")

    def test_cyrillic_lookalike_is_rejected(self) -> None:
        # U+0430 CYRILLIC SMALL LETTER A, built via chr() to keep this source file ASCII-only.
        lookalike_a = chr(0x0430)
        with pytest.raises(PathRejected):
            parse_note_path(f"{lookalike_a}/fact/x.md")

    def test_uppercase_extension_is_rejected(self) -> None:
        with pytest.raises(PathRejected):
            parse_note_path("personal/fact/x.MD")

    def test_wrong_extension_is_rejected(self) -> None:
        with pytest.raises(PathRejected):
            parse_note_path("personal/fact/x.txt")

    def test_uppercase_namespace_is_rejected(self) -> None:
        with pytest.raises(PathRejected):
            parse_note_path("Personal/fact/x.md")

    def test_uppercase_slug_is_rejected(self) -> None:
        with pytest.raises(PathRejected):
            parse_note_path("personal/fact/X.md")

    def test_slug_over_max_length_is_rejected(self) -> None:
        slug = "a" * 81
        with pytest.raises(PathRejected) as excinfo:
            parse_note_path(f"personal/fact/{slug}.md")
        assert "max 80" in str(excinfo.value)

    def test_reserved_namespace_is_rejected(self) -> None:
        with pytest.raises(PathRejected) as excinfo:
            parse_note_path("_foo/fact/x.md")
        assert "reserved" in str(excinfo.value)

    def test_wrong_type_directory_is_rejected(self) -> None:
        with pytest.raises(PathRejected) as excinfo:
            parse_note_path("personal/note/x.md")
        assert "not one of the allowed types" in str(excinfo.value)

    def test_too_few_segments_is_rejected(self) -> None:
        with pytest.raises(PathRejected):
            parse_note_path("personal/x.md")

    def test_too_many_segments_is_rejected(self) -> None:
        with pytest.raises(PathRejected):
            parse_note_path("personal/fact/sub/x.md")

    def test_archive_path_rejected_when_not_allowed(self) -> None:
        with pytest.raises(PathRejected) as excinfo:
            parse_note_path("_archive/personal/fact/x.md")
        assert "archive path" in str(excinfo.value)

    def test_double_hyphen_in_slug_is_rejected(self) -> None:
        with pytest.raises(PathRejected):
            parse_note_path("personal/fact/a--b.md")

    def test_leading_hyphen_in_slug_is_rejected(self) -> None:
        with pytest.raises(PathRejected):
            parse_note_path("personal/fact/-ab.md")

    def test_conflict_file_path_is_rejected(self) -> None:
        with pytest.raises(PathRejected):
            parse_note_path("personal/fact/x.conflict.md")


class TestNotePathHelpers:
    def test_archive_path_adds_prefix(self) -> None:
        live = NotePath(namespace="personal", type="fact", slug="x")
        archived = live.archive_path()
        assert archived.archived is True
        assert archived.relative == "_archive/personal/fact/x.md"

    def test_live_path_removes_prefix(self) -> None:
        archived = NotePath(namespace="personal", type="fact", slug="x", archived=True)
        live = archived.live_path()
        assert live.archived is False
        assert live.relative == "personal/fact/x.md"

    def test_archive_and_live_round_trip(self) -> None:
        live = NotePath(namespace="personal", type="fact", slug="x")
        assert live.archive_path().live_path() == live


class TestResolveValid:
    def test_resolves_live_path_under_root(self, tmp_path: Path) -> None:
        resolved = resolve(tmp_path, "personal/fact/x.md")
        assert resolved == tmp_path / "personal" / "fact" / "x.md"

    def test_resolves_archive_path_when_allowed(self, tmp_path: Path) -> None:
        resolved = resolve(tmp_path, "_archive/personal/fact/x.md", allow_archive=True)
        assert resolved == tmp_path / "_archive" / "personal" / "fact" / "x.md"

    def test_missing_components_are_fine_without_must_exist(self, tmp_path: Path) -> None:
        resolved = resolve(tmp_path, "personal/fact/x.md")
        assert not resolved.exists()

    def test_must_exist_accepts_an_existing_file(self, tmp_path: Path) -> None:
        note_dir = tmp_path / "personal" / "fact"
        note_dir.mkdir(parents=True)
        (note_dir / "x.md").write_text("content\n")
        resolved = resolve(tmp_path, "personal/fact/x.md", must_exist=True)
        assert resolved.read_text() == "content\n"


class TestResolveRejections:
    def test_invalid_rel_is_rejected_before_touching_disk(self, tmp_path: Path) -> None:
        with pytest.raises(PathRejected):
            resolve(tmp_path, "personal/../../etc/passwd")

    def test_must_exist_rejects_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(PathRejected):
            resolve(tmp_path, "personal/fact/x.md", must_exist=True)

    def test_existing_directory_instead_of_file_is_rejected(self, tmp_path: Path) -> None:
        note_dir = tmp_path / "personal" / "fact" / "x.md"
        note_dir.mkdir(parents=True)
        with pytest.raises(PathRejected) as excinfo:
            resolve(tmp_path, "personal/fact/x.md")
        assert "not a regular file" in str(excinfo.value)

    def test_symlinked_namespace_directory_is_rejected(self, tmp_path: Path) -> None:
        real_dir = tmp_path / "real-namespace"
        real_dir.mkdir()
        os.symlink(real_dir, tmp_path / "personal")
        with pytest.raises(PathRejected) as excinfo:
            resolve(tmp_path, "personal/fact/x.md")
        assert "symlink" in str(excinfo.value)

    def test_symlinked_type_directory_is_rejected(self, tmp_path: Path) -> None:
        namespace_dir = tmp_path / "personal"
        namespace_dir.mkdir()
        real_dir = tmp_path / "real-type"
        real_dir.mkdir()
        os.symlink(real_dir, namespace_dir / "fact")
        with pytest.raises(PathRejected) as excinfo:
            resolve(tmp_path, "personal/fact/x.md")
        assert "symlink" in str(excinfo.value)

    def test_symlinked_file_is_rejected(self, tmp_path: Path) -> None:
        type_dir = tmp_path / "personal" / "fact"
        type_dir.mkdir(parents=True)
        real_file = tmp_path / "real-x.md"
        real_file.write_text("content\n")
        os.symlink(real_file, type_dir / "x.md")
        with pytest.raises(PathRejected) as excinfo:
            resolve(tmp_path, "personal/fact/x.md")
        assert "symlink" in str(excinfo.value)

    def test_symlink_pointing_outside_root_is_rejected(self, tmp_path: Path) -> None:
        outside = tmp_path.parent / f"outside-{tmp_path.name}"
        outside.mkdir()
        try:
            type_dir = tmp_path / "personal" / "fact"
            type_dir.mkdir(parents=True)
            os.symlink(outside, type_dir / "x.md")
            with pytest.raises(PathRejected):
                resolve(tmp_path, "personal/fact/x.md")
        finally:
            outside.rmdir()


class TestConflictPath:
    def test_live_note_path_has_conflict_suffix_beside_it(self) -> None:
        note_path = parse_note_path("personal/fact/x.md")
        assert conflict_path(note_path) == "personal/fact/x.conflict.md"

    def test_archived_note_path_uses_the_live_shape(self) -> None:
        note_path = parse_note_path("_archive/personal/fact/x.md", allow_archive=True)
        assert conflict_path(note_path) == "personal/fact/x.conflict.md"


class TestResolveInternal:
    def test_resolves_a_note_path(self, tmp_path: Path) -> None:
        resolved = resolve_internal(tmp_path, "personal/fact/x.md")
        assert resolved == tmp_path / "personal" / "fact" / "x.md"

    def test_resolves_a_conflict_file_path(self, tmp_path: Path) -> None:
        resolved = resolve_internal(tmp_path, "personal/fact/x.conflict.md")
        assert resolved == tmp_path / "personal" / "fact" / "x.conflict.md"

    def test_resolves_an_archive_path(self, tmp_path: Path) -> None:
        resolved = resolve_internal(tmp_path, "_archive/personal/fact/x.md")
        assert resolved == tmp_path / "_archive" / "personal" / "fact" / "x.md"

    def test_rejects_anything_that_is_neither(self, tmp_path: Path) -> None:
        with pytest.raises(PathRejected):
            resolve_internal(tmp_path, "personal/fact/x.txt")

    def test_rejects_traversal_in_a_conflict_path(self, tmp_path: Path) -> None:
        with pytest.raises(PathRejected):
            resolve_internal(tmp_path, "../escape.conflict.md")

    def test_symlinked_type_directory_is_rejected_for_a_conflict_path(self, tmp_path: Path) -> None:
        namespace_dir = tmp_path / "personal"
        namespace_dir.mkdir()
        real_dir = tmp_path / "real-type"
        real_dir.mkdir()
        os.symlink(real_dir, namespace_dir / "fact")
        with pytest.raises(PathRejected) as excinfo:
            resolve_internal(tmp_path, "personal/fact/x.conflict.md")
        assert "symlink" in str(excinfo.value)
