# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for `migrate git-to-postgres --dry-run` (#246).

Every test here stays entirely off the database: `migrate_git.dry_run` only
ever reads the vault's working tree and its Git history, so "the database
stays untouched" holds by construction - none of these tests open a
database connection at all, nor does `migrate_git.py` import `asyncpg`.

`_build_vault` grows a small Git history (via `tests/git_fixtures.py`'s
`seed_notes`/`human_commit`/`human_rename`, reused rather than duplicated)
and clones it into a working tree, the shape `dry_run` expects for `--vault`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from git_fixtures import human_commit, human_rename, seed_notes

from memory_manager import cli
from memory_manager.migrate_git import (
    MapEntry,
    MapError,
    MigrationError,
    discover_namespaces,
    dry_run,
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
    """A Git vault with two namespaces across three commits.

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


class TestParseMapEntries:
    def test_rejects_missing_equals(self) -> None:
        with pytest.raises(MapError, match="missing '='"):
            parse_map_entries(["personaluser:oid-1"])

    def test_rejects_missing_colon(self) -> None:
        with pytest.raises(MapError, match="missing ':'"):
            parse_map_entries(["personal=user"])

    def test_rejects_too_many_colons(self) -> None:
        with pytest.raises(MapError, match="too many ':'"):
            parse_map_entries(["team=group:team-1:payments:extra"])

    def test_rejects_unknown_kind(self) -> None:
        with pytest.raises(MapError, match="not one of"):
            parse_map_entries(["team=agent:a-1"])

    def test_rejects_alias_on_user(self) -> None:
        with pytest.raises(MapError, match="takes no alias"):
            parse_map_entries(["personal=user:oid-1:me"])

    def test_rejects_org_with_wrong_key(self) -> None:
        with pytest.raises(MapError, match="kind 'org' takes key"):
            parse_map_entries(["shared=org:company"])

    def test_rejects_reserved_alias(self) -> None:
        with pytest.raises(MapError, match="reserved"):
            parse_map_entries(["team=group:team-1:org"])

    def test_rejects_internal_alias_prefix(self) -> None:
        with pytest.raises(MapError, match="reserved"):
            parse_map_entries(["team=group:team-1:u-9"])

    def test_rejects_bad_alias_charset(self) -> None:
        with pytest.raises(MapError, match="does not match"):
            parse_map_entries(["team=group:team-1:Team_1"])

    def test_rejects_duplicate_namespace(self) -> None:
        with pytest.raises(MapError, match="more than one entry"):
            parse_map_entries(["team=group:team-1", "team=group:team-2"])

    def test_rejects_same_target_different_alias(self) -> None:
        with pytest.raises(MapError, match="more than one alias"):
            parse_map_entries(["team=group:team-1:a", "squad=group:team-1:b"])

    def test_rejects_alias_collision_across_targets(self) -> None:
        with pytest.raises(MapError, match="aliases must be unique"):
            parse_map_entries(["team=group:team-1:shared", "proj=project:proj-1:shared"])

    def test_group_alias_defaults_to_git_namespace(self) -> None:
        entries = parse_map_entries(["payments=group:grp-1"])
        assert entries["payments"] == MapEntry(
            git_namespace="payments", kind="group", key="grp-1", alias="payments"
        )

    def test_user_entry_has_no_alias(self) -> None:
        entries = parse_map_entries(["personal=user:oid-1"])
        assert entries["personal"].alias is None

    def test_org_entry_is_fixed(self) -> None:
        entries = parse_map_entries(["shared=org:org"])
        assert entries["shared"] == MapEntry(
            git_namespace="shared", kind="org", key="org", alias="org"
        )


class TestDiscoverNamespaces:
    def test_finds_live_and_archive_only_namespaces(self, tmp_path: Path) -> None:
        (tmp_path / "personal" / "fact").mkdir(parents=True)
        (tmp_path / "personal" / "fact" / "x.md").write_bytes(_note_bytes())
        (tmp_path / "_archive" / "team" / "fact").mkdir(parents=True)
        (tmp_path / "_archive" / "team" / "fact" / "y.md").write_bytes(_note_bytes())
        (tmp_path / ".git").mkdir()

        assert discover_namespaces(tmp_path) == {"personal", "team"}


class TestDryRun:
    def test_raises_for_non_git_directory(self, tmp_path: Path) -> None:
        (tmp_path / "personal").mkdir()
        with pytest.raises(MigrationError, match="not a git working copy"):
            dry_run(tmp_path, {})

    def test_reports_unmapped_namespace(self, bare_remote: Path, tmp_path: Path) -> None:
        vault_dir = _build_vault(
            bare_remote,
            tmp_path / "vault",
            note_a_id=new_ulid(_CREATED),
            note_b_id=new_ulid(_CREATED),
        )
        map_entries = parse_map_entries(["personal=user:oid-1"])

        report = dry_run(vault_dir, map_entries)

        assert report.ok is False
        assert report.unmapped == ("team",)
        assert report.unknown_mappings == ()

    def test_reports_stale_mapping(self, bare_remote: Path, tmp_path: Path) -> None:
        vault_dir = _build_vault(
            bare_remote,
            tmp_path / "vault",
            note_a_id=new_ulid(_CREATED),
            note_b_id=new_ulid(_CREATED),
        )
        map_entries = parse_map_entries(
            ["personal=user:oid-1", "team=group:team-1", "ghost=org:org"]
        )

        report = dry_run(vault_dir, map_entries)

        assert report.ok is False
        assert report.unknown_mappings == ("ghost",)

    def test_counts_live_archived_and_revisions_per_namespace(
        self, bare_remote: Path, tmp_path: Path
    ) -> None:
        note_a_id = new_ulid(_CREATED)
        note_b_id = new_ulid(_CREATED)
        vault_dir = _build_vault(
            bare_remote, tmp_path / "vault", note_a_id=note_a_id, note_b_id=note_b_id
        )
        map_entries = parse_map_entries(["personal=user:oid-1", "team=group:team-1"])

        report = dry_run(vault_dir, map_entries)

        assert report.ok is True
        by_namespace = {ns.git_namespace: ns for ns in report.namespaces}
        assert set(by_namespace) == {"personal", "team"}

        personal = by_namespace["personal"]
        assert personal.target == MapEntry(
            git_namespace="personal", kind="user", key="oid-1", alias=None
        )
        assert personal.live_notes == 1
        assert personal.archived_notes == 0
        assert personal.revisions == 2
        assert personal.problems == ()

        team = by_namespace["team"]
        assert team.target == MapEntry(
            git_namespace="team", kind="group", key="team-1", alias="team"
        )
        assert team.live_notes == 0
        assert team.archived_notes == 1
        assert team.revisions == 2
        assert team.problems == ()

    def test_reports_a_note_that_fails_validation(self, bare_remote: Path, tmp_path: Path) -> None:
        seed_notes(bare_remote, {"personal/fact/bad.md": b"not a note"})
        vault_dir = _clone(bare_remote, tmp_path / "vault")
        map_entries = parse_map_entries(["personal=user:oid-1"])

        report = dry_run(vault_dir, map_entries)

        assert report.ok is False
        personal = next(ns for ns in report.namespaces if ns.git_namespace == "personal")
        assert personal.live_notes == 0
        assert len(personal.problems) == 1
        assert "personal/fact/bad.md" in personal.problems[0]

    def test_reports_a_secret_in_a_note(self, bare_remote: Path, tmp_path: Path) -> None:
        secret_body = "aws key AKIA" + "IOSFODNN7EXAMPLE" + "\n"
        seed_notes(
            bare_remote,
            {"personal/fact/secret.md": _note_bytes(title="Secret", body=secret_body)},
        )
        vault_dir = _clone(bare_remote, tmp_path / "vault")
        map_entries = parse_map_entries(["personal=user:oid-1"])

        report = dry_run(vault_dir, map_entries)

        assert report.ok is False
        personal = next(ns for ns in report.namespaces if ns.git_namespace == "personal")
        assert len(personal.problems) == 1


class TestCli:
    def test_dry_run_requires_vault(self) -> None:
        assert cli.main(["migrate", "git-to-postgres", "--dry-run"]) == 2

    def test_apply_requires_vault(self) -> None:
        # No `--dry-run` (#247 lifted the requirement to pass it): this must
        # still fail on the missing `--vault`, before ever touching a database.
        assert cli.main(["migrate", "git-to-postgres"]) == 2

    def test_rejects_malformed_map(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exit_code = cli.main(
            ["migrate", "git-to-postgres", "--vault", str(tmp_path), "--dry-run", "--map", "oops"]
        )
        assert exit_code == 2
        assert "missing '='" in capsys.readouterr().err

    def test_runs_clean_mapped_vault(
        self, bare_remote: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        vault_dir = _build_vault(
            bare_remote,
            tmp_path / "vault",
            note_a_id=new_ulid(_CREATED),
            note_b_id=new_ulid(_CREATED),
        )

        exit_code = cli.main(
            [
                "migrate",
                "git-to-postgres",
                "--vault",
                str(vault_dir),
                "--dry-run",
                "--map",
                "personal=user:oid-1",
                "--map",
                "team=group:team-1",
            ]
        )

        assert exit_code == 0
        out = capsys.readouterr().out
        assert "personal -> user:oid-1" in out
        assert "team -> group:team-1:team" in out

    def test_runs_unmapped_vault_nonzero(self, bare_remote: Path, tmp_path: Path) -> None:
        vault_dir = _build_vault(
            bare_remote,
            tmp_path / "vault",
            note_a_id=new_ulid(_CREATED),
            note_b_id=new_ulid(_CREATED),
        )

        exit_code = cli.main(
            [
                "migrate",
                "git-to-postgres",
                "--vault",
                str(vault_dir),
                "--dry-run",
                "--map",
                "personal=user:oid-1",
            ]
        )

        assert exit_code == 1
