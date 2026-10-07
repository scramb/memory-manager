# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the Markdown folder importer (#48)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from conftest import human_commit

from memory_manager.config import VaultConfig
from memory_manager.importers.core import build_source, run_import
from memory_manager.importers.markdown import collect
from memory_manager.queue import WriteQueue
from memory_manager.storage.base import StorageBackend
from memory_manager.storage.git import GitBackend
from memory_manager.vault.git import Git
from memory_manager.vault.note import parse
from memory_manager.vault.repo import Repo

# Fake secret, assembled at runtime so no secret-shaped literal sits in the
# source (same convention as tests/vault/test_secrets.py).
_FAKE_AWS_ACCESS_KEY_ID = "AKIA" + "IOSFODNN7EXAMPLE"


def _log(remote: Path) -> list[str]:
    result = Git(cwd=remote).run("log", "--format=%H", check=False)
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.decode("utf-8").strip("\n").split("\n") if line]


def _remote_file(remote: Path, rel: str) -> bytes | None:
    result = Git(cwd=remote).run("show", f"main:{rel}", check=False)
    if result.returncode != 0:
        return None
    return result.stdout


@pytest.fixture
async def backend(vault_config: VaultConfig) -> AsyncIterator[StorageBackend]:
    """A `GitBackend` over a fresh clone, following `tests/storage/test_git_backend.py`."""
    repo = Repo(vault_config)
    queue = WriteQueue(repo)
    await queue.start()
    try:
        yield GitBackend(queue, repo, vault_config.dir)
    finally:
        await queue.stop()


@pytest.fixture
def repo(vault_config: VaultConfig) -> Repo:
    return Repo(vault_config)


class TestCollect:
    def test_plain_markdown_file_without_frontmatter(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        source.mkdir()
        (source / "todo.md").write_text("# My Todo\n\nBuy milk.\n", encoding="utf-8")

        items, rejected = collect(source, namespace="personal")

        assert rejected == []
        assert len(items) == 1
        item = items[0]
        assert item.title is None
        assert item.slug_hint == "todo"
        assert item.type == "reference"
        assert item.namespace == "personal"
        assert item.source == "import:markdown:todo.md"
        assert "Buy milk." in item.body

    def test_obsidian_frontmatter_is_parsed_and_foreign_keys_dropped(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        source.mkdir()
        (source / "note.md").write_text(
            "---\n"
            "title: Project Kickoff\n"
            "description: Notes from the kickoff meeting.\n"
            "type: project\n"
            "tags: [work, kickoff]\n"
            "aliases: [kickoff-notes]\n"
            "created: 2026-01-15\n"
            "cssclass: fancy\n"
            "publish: true\n"
            "---\n"
            "Body content here.\n",
            encoding="utf-8",
        )

        items, rejected = collect(source, namespace="work")

        assert rejected == []
        item = items[0]
        assert item.title == "Project Kickoff"
        assert item.description == "Notes from the kickoff meeting."
        assert item.type == "project"
        assert item.tags == ("work", "kickoff")
        assert item.aliases == ("kickoff-notes",)
        assert item.created is not None
        assert item.created.isoformat() == "2026-01-15T00:00:00+00:00"
        assert item.body == "Body content here.\n"

    def test_frontmatter_type_invalid_falls_back_to_default(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        source.mkdir()
        (source / "note.md").write_text(
            "---\ntype: not-a-real-type\n---\nBody.\n", encoding="utf-8"
        )

        items, _ = collect(source, namespace="personal", default_type="fact")

        assert items[0].type == "fact"

    def test_german_umlaut_filename_produces_ascii_slug_hint_source(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        source.mkdir()
        (source / "Käsekuchen Rezept.md").write_text("Lecker.\n", encoding="utf-8")

        items, _ = collect(source, namespace="personal")

        assert items[0].slug_hint == "Käsekuchen Rezept"
        assert items[0].source == "import:markdown:Käsekuchen Rezept.md"

    def test_walks_nested_directories(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        (source / "sub" / "deeper").mkdir(parents=True)
        (source / "top.md").write_text("Top.\n", encoding="utf-8")
        (source / "sub" / "mid.md").write_text("Mid.\n", encoding="utf-8")
        (source / "sub" / "deeper" / "low.md").write_text("Low.\n", encoding="utf-8")

        items, _ = collect(source, namespace="personal")

        sources = sorted(item.source for item in items)
        assert sources == [
            "import:markdown:sub/deeper/low.md",
            "import:markdown:sub/mid.md",
            "import:markdown:top.md",
        ]

    def test_dotfiles_and_dotdirs_are_skipped(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        (source / ".hidden-dir").mkdir(parents=True)
        (source / ".hidden-dir" / "note.md").write_text("Hidden.\n", encoding="utf-8")
        (source / ".dotfile.md").write_text("Hidden.\n", encoding="utf-8")
        (source / "visible.md").write_text("Visible.\n", encoding="utf-8")

        items, _ = collect(source, namespace="personal")

        assert [item.source for item in items] == ["import:markdown:visible.md"]

    def test_symlinked_file_is_ignored(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        source.mkdir()
        target = tmp_path / "outside.md"
        target.write_text("Outside content.\n", encoding="utf-8")
        (source / "link.md").symlink_to(target)
        (source / "real.md").write_text("Real content.\n", encoding="utf-8")

        items, _ = collect(source, namespace="personal")

        assert [item.source for item in items] == ["import:markdown:real.md"]

    def test_symlinked_directory_is_not_followed(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        source.mkdir()
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        (outside_dir / "secret.md").write_text("Should not appear.\n", encoding="utf-8")
        (source / "linked-dir").symlink_to(outside_dir)

        items, _ = collect(source, namespace="personal")

        assert items == []


class TestRunImport:
    async def test_dry_run_writes_nothing(
        self, tmp_path: Path, backend: StorageBackend, repo: Repo, bare_remote: Path
    ) -> None:
        source = tmp_path / "source"
        source.mkdir()
        (source / "note.md").write_text("# A Note\n\nSome content.\n", encoding="utf-8")

        before = _log(bare_remote)
        items, rejected = collect(source, namespace="personal")
        report = await run_import(items, backend, apply=False)

        assert rejected == []
        assert report.created == ["personal/reference/note.md"]
        assert _log(bare_remote) == before

    async def test_apply_creates_one_commit_per_file_authored_import(
        self, tmp_path: Path, backend: StorageBackend, repo: Repo, bare_remote: Path
    ) -> None:
        source = tmp_path / "source"
        source.mkdir()
        (source / "first.md").write_text("# First\n\nFirst body.\n", encoding="utf-8")
        (source / "second.md").write_text("# Second\n\nSecond body.\n", encoding="utf-8")

        items, rejected = collect(source, namespace="personal")
        report = await run_import(items, backend, apply=True)

        assert rejected == []
        assert sorted(report.created) == [
            "personal/reference/first.md",
            "personal/reference/second.md",
        ]

        log_result = Git(cwd=bare_remote).run("log", "--format=%an")
        authors = {
            line for line in log_result.stdout.decode("utf-8").strip("\n").split("\n") if line
        }
        assert authors == {"import"}
        assert len(_log(bare_remote)) == 2

        note = parse(_remote_file(bare_remote, "personal/reference/first.md") or b"")
        assert note.title == "First"
        assert note.source == "import:markdown:first.md"

    async def test_fake_secret_is_rejected_and_reported_not_fatal(
        self, tmp_path: Path, backend: StorageBackend, repo: Repo, bare_remote: Path
    ) -> None:
        source = tmp_path / "source"
        source.mkdir()
        (source / "leaky.md").write_text(
            f"# Leaky\n\nKey: {_FAKE_AWS_ACCESS_KEY_ID}\n", encoding="utf-8"
        )
        (source / "clean.md").write_text("# Clean\n\nNothing secret here.\n", encoding="utf-8")

        items, _ = collect(source, namespace="personal")
        report = await run_import(items, backend, apply=True)

        assert report.created == ["personal/reference/clean.md"]
        assert len(report.rejected) == 1
        source_ref, reason = report.rejected[0]
        assert source_ref == "import:markdown:leaky.md"
        assert "secret" in reason.lower() or "aws" in reason.lower()
        assert _remote_file(bare_remote, "personal/reference/leaky.md") is None

    async def test_oversized_file_is_rejected_not_truncated(
        self, tmp_path: Path, backend: StorageBackend, repo: Repo
    ) -> None:
        source = tmp_path / "source"
        source.mkdir()
        huge_body = "x" * 20_000
        (source / "huge.md").write_text(f"# Huge\n\n{huge_body}\n", encoding="utf-8")

        items, _ = collect(source, namespace="personal")
        report = await run_import(items, backend, apply=True)

        assert report.created == []
        assert len(report.rejected) == 1
        source_ref, reason = report.rejected[0]
        assert source_ref == "import:markdown:huge.md"
        assert "too large" in reason

    async def test_duplicate_content_is_deduped(
        self, tmp_path: Path, backend: StorageBackend, repo: Repo
    ) -> None:
        source = tmp_path / "source"
        source.mkdir()
        (source / "one.md").write_text("# Shared\n\nIdentical content.\n", encoding="utf-8")
        (source / "two.md").write_text("# Shared\n\nIdentical content.\n", encoding="utf-8")

        items, _ = collect(source, namespace="personal")
        report = await run_import(items, backend, apply=True)

        assert len(report.created) == 1
        assert report.duplicates == ["import:markdown:two.md"]

    async def test_existing_target_path_is_skipped_not_overwritten(
        self, tmp_path: Path, backend: StorageBackend, repo: Repo, bare_remote: Path
    ) -> None:
        existing_note = (
            b"---\n"
            b"id: 01ARZ3NDEKTSV4RRFFQ69G5FAV\n"
            b"title: Existing\n"
            b"description: Already here.\n"
            b"type: reference\n"
            b"created: 2026-01-01T00:00:00Z\n"
            b"updated: 2026-01-01T00:00:00Z\n"
            b"---\n"
            b"Original content.\n"
        )
        human_commit(bare_remote, "personal/reference/clash.md", existing_note)
        await asyncio.to_thread(repo.sync)

        source = tmp_path / "source"
        source.mkdir()
        (source / "clash.md").write_text("# Clash\n\nDifferent content.\n", encoding="utf-8")

        items, _ = collect(source, namespace="personal")
        report = await run_import(items, backend, apply=True)

        assert report.created == []
        assert report.skipped_existing == ["personal/reference/clash.md"]
        assert _remote_file(bare_remote, "personal/reference/clash.md") == existing_note

    async def test_derived_description_is_flagged_and_within_limit(
        self, tmp_path: Path, backend: StorageBackend, repo: Repo, bare_remote: Path
    ) -> None:
        source = tmp_path / "source"
        source.mkdir()
        long_sentence = "This sentence just keeps going and going " * 6
        (source / "nodesc.md").write_text(
            f"# No Description\n\n{long_sentence}\n", encoding="utf-8"
        )

        items, _ = collect(source, namespace="personal")
        report = await run_import(items, backend, apply=True)

        assert report.created == ["personal/reference/nodesc.md"]
        assert len(report.flagged) == 1
        path, flags = report.flagged[0]
        assert path == "personal/reference/nodesc.md"
        assert "description-derived" in flags

        note = parse(_remote_file(bare_remote, "personal/reference/nodesc.md") or b"")
        assert len(note.description) <= 150
        assert "needs-review" in note.tags


class TestEndToEnd:
    async def test_collect_and_import_full_batch(
        self, tmp_path: Path, backend: StorageBackend, repo: Repo, bare_remote: Path
    ) -> None:
        source = tmp_path / "source"
        (source / "nested").mkdir(parents=True)
        (source / "a.md").write_text("# A\n\nBody A.\n", encoding="utf-8")
        (source / "nested" / "b.md").write_text("---\ntitle: B\n---\nBody B.\n", encoding="utf-8")

        items, rejected = collect(source, namespace="team", default_type="project")
        report = await run_import(items, backend, apply=True)

        assert rejected == []
        assert sorted(report.created) == [
            "team/project/a.md",
            "team/project/b.md",
        ]

    async def test_very_deep_path_imports_successfully_with_shortened_source(
        self, tmp_path: Path, backend: StorageBackend, repo: Repo
    ) -> None:
        # A relative path long enough that "import:markdown:" + rel alone
        # would blow past the ADR-0005 200-char `source` cap.
        source = tmp_path / "source"
        nested = source
        for i in range(12):
            nested = nested / f"deeply-nested-segment-{i:02d}"
        nested.mkdir(parents=True)
        deep_file = nested / "a-rather-long-and-descriptive-note-filename.md"
        deep_file.write_text("# Deep Note\n\nBody.\n", encoding="utf-8")

        rel = deep_file.relative_to(source).as_posix()
        assert len(f"import:markdown:{rel}") > 200, "fixture must exceed the source cap"

        items, rejected = collect(source, namespace="personal")
        assert rejected == []
        assert len(items[0].source) <= 200

        report = await run_import(items, backend, apply=True)

        assert rejected == []
        assert report.rejected == []
        assert len(report.created) == 1
        assert len(items[0].source) <= 200


class TestBuildSource:
    def test_leaves_short_source_untouched(self) -> None:
        assert build_source("import:markdown:", "notes/todo.md") == "import:markdown:notes/todo.md"

    def test_shortens_a_too_long_identifier_keeping_the_tail(self) -> None:
        prefix = "import:markdown:"
        identifier = "a/" * 150 + "final-note.md"
        full = f"{prefix}{identifier}"
        assert len(full) > 200

        result = build_source(prefix, identifier)

        assert len(result) <= 200
        assert result.startswith(prefix + "…")
        assert result.endswith("final-note.md")

    def test_result_never_exceeds_max_len_even_for_a_long_prefix(self) -> None:
        prefix = "import:some-very-long-importer-scheme-name:" * 5
        result = build_source(prefix, "identifier", max_len=200)
        assert len(result) <= 200
