# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the vault health check (`memory_manager.doctor`, #31)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import yaml

from memory_manager.doctor import run_doctor
from memory_manager.vault.note import Note, serialize
from memory_manager.vault.ulid import new_ulid

_EXAMPLES_VAULT = Path(__file__).parent.parent / "examples" / "vault"
_GOLDEN = Path(__file__).parent.parent / "eval" / "golden.yaml"

_CREATED = datetime(2026, 1, 1, tzinfo=UTC)


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


class TestCleanVault:
    def test_clean_vault_has_no_errors_or_warnings(self, tmp_path: Path) -> None:
        _write(tmp_path, "personal/fact/one.md", serialize(_note(title="One")))
        _write(
            tmp_path,
            "personal/fact/two.md",
            serialize(_note(id=new_ulid(_CREATED), title="Two")),
        )

        report = run_doctor(tmp_path)

        assert report.errors == []
        assert report.warnings == []
        assert report.ok


class TestErrors:
    def test_duplicate_id_is_an_error(self, tmp_path: Path) -> None:
        shared_id = new_ulid(_CREATED)
        _write(tmp_path, "personal/fact/one.md", serialize(_note(id=shared_id, title="One")))
        _write(tmp_path, "personal/fact/two.md", serialize(_note(id=shared_id, title="Two")))

        report = run_doctor(tmp_path)

        assert any("used by more than one file" in error for error in report.errors)
        assert not report.ok

    def test_invalid_note_is_an_error(self, tmp_path: Path) -> None:
        # Title is too long (>120 chars) - a semantic violation of ADR-0005.
        _write(tmp_path, "personal/fact/too-long.md", serialize(_note(title="x" * 200)))

        report = run_doctor(tmp_path)

        assert any("title" in error for error in report.errors)
        assert not report.ok

    def test_bad_path_is_an_error(self, tmp_path: Path) -> None:
        # "not-a-type" is not one of the allowed type directories.
        _write(tmp_path, "personal/not-a-type/note.md", serialize(_note(type="fact")))

        report = run_doctor(tmp_path)

        assert any("invalid path" in error for error in report.errors)
        assert not report.ok

    def test_secret_is_an_error(self, tmp_path: Path) -> None:
        note = _note(body="AWS key: AKIAABCDEFGHIJKLMNOP\n")
        _write(tmp_path, "personal/fact/leaky.md", serialize(note))

        report = run_doctor(tmp_path)

        assert any("looks like" in error for error in report.errors)
        assert not report.ok


class TestWarnings:
    def test_dangling_link_is_a_warning(self, tmp_path: Path) -> None:
        note = _note(body="See [[does-not-exist]] for details.\n")
        _write(tmp_path, "personal/fact/linker.md", serialize(note))

        report = run_doctor(tmp_path)

        assert report.ok
        assert any("dangling link" in warning for warning in report.warnings)

    def test_non_canonical_note_is_a_warning(self, tmp_path: Path) -> None:
        canonical = serialize(_note(title="One"))
        # A human-written variant with non-canonical YAML (quoted scalar,
        # block list) that still parses to the same semantic content.
        non_canonical = canonical.replace(b"title: One", b'title: "One"')
        _write(tmp_path, "personal/fact/human-edited.md", non_canonical)

        report = run_doctor(tmp_path)

        assert report.ok
        assert any("not canonical" in warning for warning in report.warnings)

    def test_conflict_file_is_a_warning_not_an_error(self, tmp_path: Path) -> None:
        _write(tmp_path, "personal/fact/one.md", serialize(_note(title="One")))
        _write(tmp_path, "personal/fact/one.conflict.md", serialize(_note(title="One (conflict)")))

        report = run_doctor(tmp_path)

        assert report.ok
        assert any("conflict awaits resolution" in warning for warning in report.warnings)

    def test_unknown_supersedes_target_is_a_warning(self, tmp_path: Path) -> None:
        unknown_id = new_ulid(_CREATED)
        note = _note(supersedes=(unknown_id,))
        _write(tmp_path, "personal/fact/one.md", serialize(note))

        report = run_doctor(tmp_path)

        assert report.ok
        assert any("supersedes unknown id" in warning for warning in report.warnings)


class TestExampleVault:
    def test_example_vault_has_no_errors(self) -> None:
        report = run_doctor(_EXAMPLES_VAULT)

        assert report.errors == []

    def test_every_golden_expected_id_exists_in_the_vault(self) -> None:
        golden = yaml.safe_load(_GOLDEN.read_text(encoding="utf-8"))

        known_ids: set[str] = set()
        for path in _EXAMPLES_VAULT.rglob("*.md"):
            text = path.read_text(encoding="utf-8")
            for line in text.splitlines():
                if line.startswith("id: "):
                    known_ids.add(line.removeprefix("id: ").strip())
                    break

        missing = {
            note_id for entry in golden for note_id in entry["expected"] if note_id not in known_ids
        }
        assert missing == set()
