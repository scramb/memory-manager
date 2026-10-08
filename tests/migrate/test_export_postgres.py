# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for `export_postgres` (#283, ADR-0007 §1/§Reversibility).

`_build_vault` seeds a small Git vault with two namespaces - `personal`
(one live note, one later archived) and `team` (one live note) - and maps
both through `--map <ns>=group:<key>:<ns>` so the stored Postgres alias is
the same string as the Git namespace: the point of these tests is whether
`export_postgres` reproduces the *content* `migrate git-to-postgres` just
imported byte for byte, not whether a namespace-renaming `--map` (already
covered by `tests/migrate/test_import.py`) round-trips - keeping the alias
unchanged lets every test compare the extracted archive directly against
`vault_dir`'s own tree with no path translation of its own.

`TestCliDispatch` exercises the one new decision in `cli.py`: `export`
reads `STORAGE_BACKEND` and calls `export_postgres` instead of
`export_vault` when it is `"postgres"` - using the same `cli_database_url`
fixture shape `tests/migrate/test_import.py` already uses for a CLI call
that runs its own `asyncio.run`.
"""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import secrets
import tarfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import pytest
from git_fixtures import human_rename, seed_notes

from memory_manager import cli
from memory_manager.db.migrate import migrate
from memory_manager.exporter import export_postgres, export_vault
from memory_manager.migrate_git import import_vault, parse_map_entries
from memory_manager.vault.git import Git
from memory_manager.vault.note import Note, serialize
from memory_manager.vault.paths import iter_md_files
from memory_manager.vault.ulid import new_ulid

_CREATED = datetime(2026, 1, 1, tzinfo=UTC)

_MAP_ARGS = ["personal=group:grp-1:personal", "team=group:grp-2:team"]


def _note_bytes(**overrides: object) -> bytes:
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


def _clone(remote: Path, dest: Path) -> Path:
    Git(cwd=dest.parent).run("clone", "--origin", "origin", str(remote), str(dest))
    return dest


def _build_vault(remote: Path, vault_dir: Path) -> Path:
    """Three notes across two namespaces; one of `personal`'s two is archived.

    `personal/fact/one.md` stays live, `personal/fact/two.md` is renamed to
    `_archive/personal/fact/two.md`, `team/fact/three.md` stays live - the
    same small shape `tests/migrate/test_import.py`'s own `_build_vault`
    uses, minus the second commit on `one.md` (its revision history is not
    this module's concern).
    """
    seed_notes(
        remote,
        {
            "personal/fact/one.md": _note_bytes(title="One"),
            "personal/fact/two.md": _note_bytes(title="Two"),
            "team/fact/three.md": _note_bytes(title="Three"),
        },
    )
    human_rename(remote, "personal/fact/two.md", "_archive/personal/fact/two.md")
    return _clone(remote, vault_dir)


async def _pool(test_database_url: str) -> asyncpg.Pool:
    conn = await asyncpg.connect(test_database_url)
    try:
        await migrate(conn)
    finally:
        await conn.close()
    return await asyncpg.create_pool(test_database_url)


def _source_notes(vault_dir: Path) -> dict[str, bytes]:
    """Every note under `vault_dir`, keyed by its vault-relative path."""
    return {
        file_path.relative_to(vault_dir).as_posix(): file_path.read_bytes()
        for file_path in iter_md_files(vault_dir)
    }


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


class TestExportPostgres:
    async def test_extracted_files_are_byte_identical_to_the_source_vault(
        self, bare_remote: Path, tmp_path: Path, test_database_url: str
    ) -> None:
        vault_dir = _build_vault(bare_remote, tmp_path / "vault")
        source_notes = _source_notes(vault_dir)
        assert len(source_notes) == 3

        pool = await _pool(test_database_url)
        try:
            map_entries = parse_map_entries(_MAP_ARGS)
            report = await import_vault(pool, vault_dir, map_entries)
            assert report.ok

            out_path = tmp_path / "export.tar.gz"
            manifest = await export_postgres(pool, out_path)

            assert manifest.note_count == 3
            assert manifest.vault_head is None
            _, members = _read_archive(out_path)
            assert members == source_notes
        finally:
            await pool.close()

    async def test_no_include_archive_excludes_the_archived_note(
        self, bare_remote: Path, tmp_path: Path, test_database_url: str
    ) -> None:
        vault_dir = _build_vault(bare_remote, tmp_path / "vault")

        pool = await _pool(test_database_url)
        try:
            map_entries = parse_map_entries(_MAP_ARGS)
            await import_vault(pool, vault_dir, map_entries)

            out_path = tmp_path / "export.tar.gz"
            manifest = await export_postgres(pool, out_path, include_archive=False)

            assert manifest.note_count == 2
            assert not any(entry.archived for entry in manifest.notes)
            _, members = _read_archive(out_path)
            assert not any(path.startswith("_archive/") for path in members)
        finally:
            await pool.close()

    async def test_namespace_filter_keeps_only_the_requested_namespace(
        self, bare_remote: Path, tmp_path: Path, test_database_url: str
    ) -> None:
        vault_dir = _build_vault(bare_remote, tmp_path / "vault")

        pool = await _pool(test_database_url)
        try:
            map_entries = parse_map_entries(_MAP_ARGS)
            await import_vault(pool, vault_dir, map_entries)

            out_path = tmp_path / "export.tar.gz"
            manifest = await export_postgres(pool, out_path, namespaces=["team"])

            assert manifest.note_count == 1
            assert {entry.namespace for entry in manifest.notes} == {"team"}
            _, members = _read_archive(out_path)
            assert set(members) == {"team/fact/three.md"}
        finally:
            await pool.close()

    async def test_writes_exactly_one_audit_record_without_note_content(
        self, bare_remote: Path, tmp_path: Path, test_database_url: str
    ) -> None:
        vault_dir = _build_vault(bare_remote, tmp_path / "vault")

        pool = await _pool(test_database_url)
        try:
            map_entries = parse_map_entries(_MAP_ARGS)
            await import_vault(pool, vault_dir, map_entries)

            out_path = tmp_path / "export.tar.gz"
            await export_postgres(pool, out_path)

            rows = await pool.fetch("select * from audit_log where op = 'export'")
            assert len(rows) == 1
            row = rows[0]
            assert row["actor"] == "export"
            assert row["outcome"] == "ok"
            assert row["path"] is None
            detail = json.loads(row["detail"])
            assert detail["note_count"] == 3
            assert sorted(detail["namespaces"]) == ["personal", "team"]
            assert "Body." not in json.dumps(detail)
        finally:
            await pool.close()

    async def test_git_mode_export_is_unaffected_by_the_shared_entry_builder(
        self, bare_remote: Path, tmp_path: Path
    ) -> None:
        """`export_vault` (Git mode) still works after sharing `_collect_entries`."""
        vault_dir = _build_vault(bare_remote, tmp_path / "vault")

        out_path = tmp_path / "export.tar.gz"
        manifest = export_vault(vault_dir, out_path)

        assert manifest.note_count == 3
        assert manifest.vault_head is not None
        _, members = _read_archive(out_path)
        assert members == _source_notes(vault_dir)


@pytest.fixture
def cli_database_url(admin_database_url: str) -> Iterator[str]:
    """A fresh database created/dropped with its own event loop.

    `cli.main` runs its own `asyncio.run` (the export command's Postgres
    path included), so this fixture must not depend on the async
    `test_database_url` fixture - nesting event loops fails
    (`tests/migrate/test_import.py`'s own fixture of the same name/shape).
    """
    db_name = f"mm_test_{secrets.token_hex(8)}"

    async def _create() -> None:
        connection = await asyncpg.connect(admin_database_url)
        try:
            await connection.execute(f'create database "{db_name}"')
        finally:
            await connection.close()

    asyncio.run(_create())
    base, _, _ = admin_database_url.rpartition("/")
    url = f"{base}/{db_name}"
    try:
        yield url
    finally:

        async def _drop() -> None:
            connection = await asyncpg.connect(admin_database_url)
            try:
                await connection.execute(
                    "select pg_terminate_backend(pid) from pg_stat_activity "
                    "where datname = $1 and pid <> pg_backend_pid()",
                    db_name,
                )
                await connection.execute(f'drop database if exists "{db_name}"')
            finally:
                await connection.close()

        asyncio.run(_drop())


class TestCliDispatch:
    def test_export_uses_the_postgres_path_when_storage_backend_is_postgres(
        self, tmp_path: Path, cli_database_url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("STORAGE_BACKEND", "postgres")
        monkeypatch.setenv("DATABASE_URL", cli_database_url)
        out_path = tmp_path / "export.tar.gz"

        exit_code = cli.main(["export", "--out", str(out_path)])

        assert exit_code == 0
        assert out_path.exists()
        manifest, _ = _read_archive(out_path)
        assert manifest["note_count"] == 0
        assert manifest["vault_head"] is None

    def test_export_still_uses_the_git_path_without_storage_backend_set(
        self, bare_remote: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("STORAGE_BACKEND", raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        vault_dir = _build_vault(bare_remote, tmp_path / "vault")
        out_path = tmp_path / "export.tar.gz"

        exit_code = cli.main(["export", "--vault", str(vault_dir), "--out", str(out_path)])

        assert exit_code == 0
        manifest, _ = _read_archive(out_path)
        assert manifest["note_count"] == 3
        assert manifest["vault_head"] is not None
