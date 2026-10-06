# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the vault export archive (`memory_manager.exporter`, #50)."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from memory_manager.cli import main
from memory_manager.doctor import run_doctor
from memory_manager.exporter import export_vault
from memory_manager.vault.git import Git
from memory_manager.vault.note import Note, serialize
from memory_manager.vault.ulid import new_ulid

_CREATED = datetime(2026, 1, 1, tzinfo=UTC)

_HUMAN_AUTHOR_ENV = {
    "GIT_AUTHOR_NAME": "human",
    "GIT_AUTHOR_EMAIL": "human@memory-manager.invalid",
    "GIT_COMMITTER_NAME": "human",
    "GIT_COMMITTER_EMAIL": "human@memory-manager.invalid",
}


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


def _small_vault(vault_root: Path) -> None:
    _write(vault_root, "personal/fact/one.md", serialize(_note(title="One")))
    _write(
        vault_root,
        "personal/fact/two.md",
        serialize(_note(id=new_ulid(_CREATED), title="Two")),
    )


def _git_init_and_commit(vault_root: Path, message: str) -> str:
    """Turn `vault_root` into a one-commit git working copy, return `HEAD`."""
    git = Git(cwd=vault_root, env_extra=_HUMAN_AUTHOR_ENV)
    git.run("init", "-q", "-b", "main")
    git.run("add", "-A")
    git.run("commit", "-q", "-m", message)
    return git.run("rev-parse", "HEAD").stdout.decode("utf-8").strip()


def _read_archive(out_path: Path) -> tuple[dict[str, object], dict[str, bytes]]:
    """Extract `manifest.json` and every `vault/...` member from `out_path`."""
    with gzip.open(out_path, "rb") as gz:
        raw = gz.read()
    manifest: dict[str, object] | None = None
    members: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r") as tar:
        for info in tar.getmembers():
            extracted = tar.extractfile(info)
            assert extracted is not None
            content = extracted.read()
            if info.name == "manifest.json":
                manifest = json.loads(content)
            elif info.name.startswith("vault/"):
                members[info.name.removeprefix("vault/")] = content
    assert manifest is not None
    return manifest, members


class TestExportVault:
    def test_archive_contains_manifest_and_notes_with_matching_sha256(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "vault"
        vault_root.mkdir()
        _small_vault(vault_root)

        manifest = export_vault(vault_root, tmp_path / "export.tar.gz")

        assert manifest.note_count == 2
        archived_manifest, members = _read_archive(tmp_path / "export.tar.gz")
        assert archived_manifest["note_count"] == 2
        notes = archived_manifest["notes"]
        assert isinstance(notes, list)
        assert {entry["path"] for entry in notes} == set(members)
        for entry in notes:
            data = members[entry["path"]]
            assert entry["bytes"] == len(data)
            assert entry["sha256"] == hashlib.sha256(data).hexdigest()

    def test_git_dir_and_non_note_files_are_excluded(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "vault"
        vault_root.mkdir()
        _small_vault(vault_root)
        _git_init_and_commit(vault_root, "seed")
        _write(vault_root, "README.md", b"not a note\n")

        manifest = export_vault(vault_root, tmp_path / "export.tar.gz")

        assert manifest.note_count == 2
        with gzip.open(tmp_path / "export.tar.gz", "rb") as gz:
            raw = gz.read()
        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
            names = tar.getnames()
        assert "vault/README.md" not in names
        assert not any(".git" in name.split("/") for name in names)

    def test_symlink_in_vault_is_not_included(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "vault"
        vault_root.mkdir()
        _small_vault(vault_root)
        outside = tmp_path / "outside.md"
        outside.write_bytes(serialize(_note(id=new_ulid(_CREATED), title="Outside")))
        link = vault_root / "personal" / "fact" / "linked.md"
        link.symlink_to(outside)

        manifest = export_vault(vault_root, tmp_path / "export.tar.gz")

        assert manifest.note_count == 2
        _, members = _read_archive(tmp_path / "export.tar.gz")
        assert "personal/fact/linked.md" not in members

    def test_archive_namespace_excluded_with_flag(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "vault"
        vault_root.mkdir()
        _small_vault(vault_root)
        _write(
            vault_root,
            "_archive/personal/fact/old.md",
            serialize(_note(id=new_ulid(_CREATED), title="Old")),
        )

        excluded = export_vault(vault_root, tmp_path / "no-archive.tar.gz", include_archive=False)
        assert excluded.note_count == 2
        _, members = _read_archive(tmp_path / "no-archive.tar.gz")
        assert not any(path.startswith("_archive/") for path in members)

        included = export_vault(vault_root, tmp_path / "with-archive.tar.gz")
        assert included.note_count == 3

    def test_two_exports_of_the_same_vault_are_byte_identical(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "vault"
        vault_root.mkdir()
        _small_vault(vault_root)
        _git_init_and_commit(vault_root, "seed")

        first = tmp_path / "first.tar.gz"
        second = tmp_path / "second.tar.gz"
        export_vault(vault_root, first)
        export_vault(vault_root, second)

        assert first.read_bytes() == second.read_bytes()

    def test_round_trip_through_doctor(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "vault"
        vault_root.mkdir()
        _small_vault(vault_root)
        _git_init_and_commit(vault_root, "seed")

        out_path = tmp_path / "export.tar.gz"
        export_vault(vault_root, out_path)

        extract_dir = tmp_path / "extracted"
        extract_dir.mkdir()
        with gzip.open(out_path, "rb") as gz:
            raw = gz.read()
        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
            tar.extractall(extract_dir, filter="data")

        report = run_doctor(extract_dir / "vault")
        assert report.errors == []


class TestExportCli:
    def test_cli_refuses_to_overwrite_without_force(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "vault"
        vault_root.mkdir()
        _small_vault(vault_root)
        out_path = tmp_path / "export.tar.gz"
        out_path.write_bytes(b"existing")

        exit_code = main(["export", "--vault", str(vault_root), "--out", str(out_path)])

        assert exit_code == 2
        assert out_path.read_bytes() == b"existing"

    def test_cli_force_overwrites_existing_out_file(self, tmp_path: Path) -> None:
        vault_root = tmp_path / "vault"
        vault_root.mkdir()
        _small_vault(vault_root)
        out_path = tmp_path / "export.tar.gz"
        out_path.write_bytes(b"existing")

        exit_code = main(["export", "--vault", str(vault_root), "--out", str(out_path), "--force"])

        assert exit_code == 0
        assert out_path.read_bytes() != b"existing"

    def test_cli_default_out_path_and_vault_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vault_root = tmp_path / "vault"
        vault_root.mkdir()
        _small_vault(vault_root)

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("VAULT_DIR", str(vault_root))

        exit_code = main(["export"])

        assert exit_code == 0
        assert any(tmp_path.glob("memory-export-*.tar.gz"))
