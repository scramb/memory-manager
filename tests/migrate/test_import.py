# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for `migrate git-to-postgres` (#247, ADR-0007 §6): the real import.

`_build_vault` grows the same small Git history `test_dry_run.py`'s own
helper does (two namespaces, three commits: `personal` edited once,
`team` seeded then archived) - `import_vault` is exercised against exactly
what `dry_run` already proved clean in `test_dry_run.py`, so these tests
focus on what only a real database can show: revision rows matching
`git log` one for one, the archived note staying archived, a second run
being refused rather than merged, and a failure mid-import leaving the
namespace exactly as empty as before it started.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import pytest
from git_fixtures import human_commit, human_rename, seed_notes

from memory_manager import cli
from memory_manager.db.migrate import migrate
from memory_manager.migrate_git import (
    ImportReport,
    import_vault,
    parse_map_entries,
)
from memory_manager.vault.git import Git
from memory_manager.vault.note import Note, serialize
from memory_manager.vault.ulid import new_ulid

_CREATED = datetime(2026, 1, 1, tzinfo=UTC)


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


def _build_vault(remote: Path, vault_dir: Path, *, note_a_id: str, note_b_id: str) -> Path:
    """A Git vault with two namespaces across three commits (`test_dry_run.py`'s own shape).

    `personal/fact/one.md` (id `note_a_id`) is seeded, then edited once (two
    revisions total). `team/fact/two.md` (id `note_b_id`) is seeded, then
    archived by a rename to `_archive/team/fact/two.md` (two revisions
    total: the seed and the move) - `team` ends up a namespace that only
    has an archived note.
    """
    seed_notes(
        remote,
        {
            "personal/fact/one.md": _note_bytes(id=note_a_id, title="One"),
            "team/fact/two.md": _note_bytes(id=note_b_id, title="Two"),
        },
    )
    human_commit(
        remote, "personal/fact/one.md", _note_bytes(id=note_a_id, title="One", body="Edited.\n")
    )
    human_rename(remote, "team/fact/two.md", "_archive/team/fact/two.md")
    return _clone(remote, vault_dir)


async def _pool(test_database_url: str) -> asyncpg.Pool:
    conn = await asyncpg.connect(test_database_url)
    try:
        await migrate(conn)
    finally:
        await conn.close()
    return await asyncpg.create_pool(test_database_url)


async def _git_log(vault_dir: Path, rel: str) -> list[tuple[str, str, str]]:
    """`(author, author_iso_time, subject)` for every commit touching `rel`, oldest first."""
    git = Git(cwd=vault_dir)
    result = git.run("log", "--reverse", "--follow", "--format=%an\x01%aI\x01%s", "--", rel)
    lines = result.stdout.decode("utf-8").splitlines()
    return [tuple(line.split("\x01")) for line in lines]  # type: ignore[misc]


class TestImportVault:
    async def test_imports_notes_and_history_matching_git_log(
        self, bare_remote: Path, tmp_path: Path, test_database_url: str
    ) -> None:
        note_a_id = new_ulid(_CREATED)
        note_b_id = new_ulid(_CREATED)
        vault_dir = _build_vault(
            bare_remote, tmp_path / "vault", note_a_id=note_a_id, note_b_id=note_b_id
        )
        map_entries = parse_map_entries(["personal=user:oid-1", "team=group:team-1"])

        pool = await _pool(test_database_url)
        try:
            report = await import_vault(pool, vault_dir, map_entries)
            assert report.ok is True
            by_namespace = {ns.git_namespace: ns for ns in report.namespaces}

            personal = by_namespace["personal"]
            assert personal.refused is False
            assert personal.imported_notes == 1
            assert personal.imported_revisions == 2

            team = by_namespace["team"]
            assert team.refused is False
            assert team.imported_notes == 1
            assert team.imported_revisions == 2

            async with pool.acquire() as conn:
                current = await conn.fetchrow("select * from vault_notes where id = $1", note_a_id)
                assert current is not None
                assert current["namespace"] == personal.stored_alias
                assert current["path"] == f"{personal.stored_alias}/fact/one.md"
                assert current["current_revision"] == 2
                with (vault_dir / "personal/fact/one.md").open("rb") as f:
                    head_bytes = f.read()
                from memory_manager.vault.note import version as note_version

                assert current["version"] == note_version(head_bytes)
                assert bytes(current["content"]) == head_bytes

                rows = await conn.fetch(
                    "select * from vault_revisions where note_id = $1 order by revision",
                    note_a_id,
                )
                assert [row["revision"] for row in rows] == [1, 2]
                git_log = await _git_log(vault_dir, "personal/fact/one.md")
                assert len(git_log) == 2
                for row, (author, iso_time, subject) in zip(rows, git_log, strict=True):
                    assert row["author"] == author
                    assert row["message"] == subject
                    assert row["created_at"].astimezone(UTC) == datetime.fromisoformat(
                        iso_time
                    ).astimezone(UTC)
                    # ADR-0008 addendum "curate is author-based": a 'user'
                    # namespace's revisions carry the mapped oid.
                    assert row["author_oid"] == "oid-1"

                team_row = await conn.fetchrow("select * from vault_notes where id = $1", note_b_id)
                assert team_row is not None
                assert team_row["namespace"] == "team"
                assert team_row["path"] == "_archive/team/fact/two.md"

                team_revisions = await conn.fetch(
                    "select * from vault_revisions where note_id = $1 order by revision",
                    note_b_id,
                )
                assert len(team_revisions) == 2
                assert all(row["author_oid"] is None for row in team_revisions)
        finally:
            await pool.close()

    async def test_audits_one_row_per_imported_note_without_content(
        self, bare_remote: Path, tmp_path: Path, test_database_url: str
    ) -> None:
        vault_dir = _build_vault(
            bare_remote,
            tmp_path / "vault",
            note_a_id=new_ulid(_CREATED),
            note_b_id=new_ulid(_CREATED),
        )
        map_entries = parse_map_entries(["personal=user:oid-1", "team=group:team-1"])

        pool = await _pool(test_database_url)
        try:
            report = await import_vault(pool, vault_dir, map_entries)
            assert report.ok is True

            async with pool.acquire() as conn:
                expected_paths = {
                    row["path"] for row in await conn.fetch("select path from vault_notes")
                }
                rows = await conn.fetch(
                    "select * from audit_log where op = 'migrate_import' order by id"
                )

            # Exactly one audit row per imported note (2 here: one per
            # namespace's single note), never per revision.
            assert len(rows) == 2
            for row in rows:
                assert row["op"] == "migrate_import"
                assert row["actor"] == "migrate"
                assert row["client"] == "migrate-git"
                assert row["outcome"] == "ok"
                assert row["path"] in expected_paths
                detail = json.loads(row["detail"])
                # CLAUDE.md "note content is data, not instructions"/
                # `AuditWriter`'s own rule: never a note's body or title.
                assert set(detail) == {"version", "revisions"}
                assert "Edited" not in json.dumps(detail)
                assert "Two" not in json.dumps(detail)
        finally:
            await pool.close()

    async def test_rerun_is_refused_and_writes_nothing_more(
        self, bare_remote: Path, tmp_path: Path, test_database_url: str
    ) -> None:
        vault_dir = _build_vault(
            bare_remote,
            tmp_path / "vault",
            note_a_id=new_ulid(_CREATED),
            note_b_id=new_ulid(_CREATED),
        )
        map_entries = parse_map_entries(["personal=user:oid-1", "team=group:team-1"])

        pool = await _pool(test_database_url)
        try:
            first = await import_vault(pool, vault_dir, map_entries)
            assert first.ok is True

            async with pool.acquire() as conn:
                before = await conn.fetchval("select count(*) from vault_notes")
                before_revisions = await conn.fetchval("select count(*) from vault_revisions")

            second: ImportReport = await import_vault(pool, vault_dir, map_entries)
            assert second.ok is False
            assert all(ns.refused for ns in second.namespaces)
            assert all(ns.imported_notes == 0 for ns in second.namespaces)

            async with pool.acquire() as conn:
                after = await conn.fetchval("select count(*) from vault_notes")
                after_revisions = await conn.fetchval("select count(*) from vault_revisions")
                rejected_audits = await conn.fetch(
                    "select * from audit_log where op = 'migrate_import' and outcome = 'rejected'"
                )
            assert after == before
            assert after_revisions == before_revisions
            # The refused re-run is itself audited once per namespace, even
            # though it wrote nothing (CLAUDE.md "audit log for every write").
            assert len(rejected_audits) == len(second.namespaces)
            for row in rejected_audits:
                assert row["path"] is None
                assert set(json.loads(row["detail"])) == {"namespace", "reason"}
        finally:
            await pool.close()

    async def test_injected_failure_leaves_the_namespace_empty(
        self,
        bare_remote: Path,
        tmp_path: Path,
        test_database_url: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        note_a_id = new_ulid(_CREATED)
        note_b_id = new_ulid(_CREATED)
        vault_dir = _build_vault(
            bare_remote, tmp_path / "vault", note_a_id=note_a_id, note_b_id=note_b_id
        )
        map_entries = parse_map_entries(["personal=user:oid-1", "team=group:team-1"])

        import memory_manager.migrate_git as migrate_git

        real_version = migrate_git.note_version  # type: ignore[attr-defined]
        calls = 0

        def _failing_version(data: bytes) -> str:
            nonlocal calls
            calls += 1
            # Lets `personal`'s own namespace import run to completion first
            # (its single note's current row plus both revisions), then fails
            # partway through `team`'s namespace - a different transaction,
            # so `personal` must stay imported while `team` stays empty.
            if calls > 3:
                raise RuntimeError("injected failure")
            return real_version(data)

        monkeypatch.setattr(migrate_git, "note_version", _failing_version)

        pool = await _pool(test_database_url)
        try:
            with pytest.raises(RuntimeError, match="injected failure"):
                await import_vault(pool, vault_dir, map_entries)

            async with pool.acquire() as conn:
                team_notes = await conn.fetchval(
                    "select count(*) from vault_notes where namespace = 'team'"
                )
                team_revisions = await conn.fetchval(
                    "select count(*) from vault_revisions vr "
                    "join vault_notes vn on vn.id = vr.note_id "
                    "where vn.namespace = 'team'"
                )
                personal_notes = await conn.fetchval(
                    "select count(*) from vault_notes where id = $1", note_a_id
                )
            assert team_notes == 0
            assert team_revisions == 0
            # `personal`'s own transaction already committed, in a separate
            # connection, before `team`'s own failed - only `team` rolled back.
            assert personal_notes == 1
        finally:
            await pool.close()


@pytest.fixture
def cli_database_url(admin_database_url: str) -> Iterator[str]:
    """A fresh database created/dropped with its own event loop.

    `cli.main` runs its own `asyncio.run` (#247's apply path included), so
    this fixture must not depend on the async `test_database_url` fixture -
    nesting event loops fails (`tests/index/test_indexer.py`'s own fixture
    of the same name and shape).
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


class TestCliApply:
    def test_apply_imports_and_reindexes(
        self,
        bare_remote: Path,
        tmp_path: Path,
        cli_database_url: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        vault_dir = _build_vault(
            bare_remote,
            tmp_path / "vault",
            note_a_id=new_ulid(_CREATED),
            note_b_id=new_ulid(_CREATED),
        )
        monkeypatch.setenv("DATABASE_URL", cli_database_url)

        exit_code = cli.main(
            [
                "migrate",
                "git-to-postgres",
                "--vault",
                str(vault_dir),
                "--map",
                "personal=user:oid-1",
                "--map",
                "team=group:team-1",
            ]
        )

        out = capsys.readouterr().out
        assert exit_code == 0
        assert "imported_notes=1 imported_revisions=2" in out
        assert "indexed=" in out

    def test_apply_refuses_when_dry_run_has_problems(
        self,
        bare_remote: Path,
        tmp_path: Path,
        cli_database_url: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        vault_dir = _build_vault(
            bare_remote,
            tmp_path / "vault",
            note_a_id=new_ulid(_CREATED),
            note_b_id=new_ulid(_CREATED),
        )
        monkeypatch.setenv("DATABASE_URL", cli_database_url)

        exit_code = cli.main(
            [
                "migrate",
                "git-to-postgres",
                "--vault",
                str(vault_dir),
                "--map",
                "personal=user:oid-1",
                # 'team' deliberately left unmapped.
            ]
        )

        assert exit_code == 1
        assert "nothing was imported" in capsys.readouterr().err
