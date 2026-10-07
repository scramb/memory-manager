# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the Claude/ChatGPT export importer core (#49)."""

from __future__ import annotations

import asyncio
import json
import struct
import zipfile
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path

import pytest
from conftest import human_commit

from memory_manager.config import VaultConfig
from memory_manager.importers.chatgpt import ChatGPTFormatError
from memory_manager.importers.chatgpt import collect as collect_chatgpt
from memory_manager.importers.claude import ClaudeFormatError
from memory_manager.importers.claude import collect as collect_claude
from memory_manager.importers.core import dedupe_against_vault, run_import
from memory_manager.importers.textlist import parse_items
from memory_manager.queue import WriteQueue
from memory_manager.storage.base import StorageBackend
from memory_manager.storage.git import GitBackend
from memory_manager.vault.git import Git
from memory_manager.vault.note import parse as parse_note
from memory_manager.vault.repo import Repo

# Fake secret, assembled at runtime so no secret-shaped literal sits in the
# source (same convention as tests/vault/test_secrets.py and
# tests/importers/test_markdown.py).
_FAKE_AWS_ACCESS_KEY_ID = "AKIA" + "IOSFODNN7EXAMPLE"

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "exports"
_TODAY = date(2026, 10, 6)


def _read_fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def _build_nested_claude_zip(tmp_path: Path, memories_json: str) -> Path:
    """A manifest-style export zip: a top-level zip holding `memories-000.zip`,
    which in turn holds `memories/<uuid>.json` - the current (2026) Claude
    export layout (`docs/research/memory-exports.md` 1.2)."""
    inner_path = tmp_path / "memories-000.zip"
    with zipfile.ZipFile(inner_path, "w") as inner:
        inner.writestr("memories/0198aaaa-0000-7000-8000-00000000beef.json", memories_json)

    outer_path = tmp_path / "export.zip"
    with zipfile.ZipFile(outer_path, "w") as outer:
        outer.writestr("memories-000.zip", inner_path.read_bytes())
        # A sibling category zip that must be ignored (research 1.3 caveat).
        outer.writestr("conversations-000.zip", b"not read by this importer")

    return outer_path


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


class TestParseItems:
    def test_one_item_per_line_by_default(self) -> None:
        text = "The user likes tea.\nThe user works remotely.\n"
        items = parse_items(text)
        assert [item.text for item in items] == ["The user likes tea.", "The user works remotely."]

    def test_bracketed_date_prefix_is_parsed_off(self) -> None:
        items = parse_items("[2026-01-02] The user likes tea.\n")
        assert items[0].text == "The user likes tea."
        assert items[0].created is not None
        assert items[0].created.isoformat() == "2026-01-02T00:00:00+00:00"

    def test_bracketed_date_with_dash_is_parsed_off(self) -> None:
        items = parse_items("[2026-01-02] - The user likes tea.\n")
        assert items[0].text == "The user likes tea."
        assert items[0].created is not None

    def test_plain_date_dash_prefix_is_parsed_off(self) -> None:
        items = parse_items("2026-01-02 - The user likes tea.\n")
        assert items[0].text == "The user likes tea."
        assert items[0].created is not None

    def test_line_without_date_prefix_has_no_created(self) -> None:
        items = parse_items("The user likes tea.\n")
        assert items[0].created is None

    def test_bullets_become_one_item_each_regardless_of_join_lines(self) -> None:
        text = "- The user likes tea.\n- The user works remotely.\n"
        assert [item.text for item in parse_items(text)] == [
            "The user likes tea.",
            "The user works remotely.",
        ]
        assert [item.text for item in parse_items(text, join_lines=True)] == [
            "The user likes tea.",
            "The user works remotely.",
        ]

    def test_join_lines_true_folds_an_unbulleted_block_into_one_item(self) -> None:
        text = "The user is thinking about\nrewriting the service in Rust.\n"
        assert len(parse_items(text, join_lines=False)) == 2
        joined = parse_items(text, join_lines=True)
        assert len(joined) == 1
        assert joined[0].text == "The user is thinking about rewriting the service in Rust."

    def test_heading_only_block_is_skipped(self) -> None:
        text = "## Work context\n\nThe user is a backend engineer.\n"
        items = parse_items(text, join_lines=True)
        assert [item.text for item in items] == ["The user is a backend engineer."]

    def test_heading_inside_a_bulleted_block_is_dropped_not_an_item(self) -> None:
        text = "## Work context\n- The user is a backend engineer.\n- Prefers Postgres.\n"
        items = parse_items(text, join_lines=True)
        assert [item.text for item in items] == [
            "The user is a backend engineer.",
            "Prefers Postgres.",
        ]

    def test_code_fence_markers_are_stripped_content_kept(self) -> None:
        text = "```\nThe user likes tea.\n```\n"
        items = parse_items(text)
        assert [item.text for item in items] == ["The user likes tea."]

    def test_blank_text_produces_no_items(self) -> None:
        assert parse_items("\n\n   \n") == []


class TestClaudeCollect:
    def test_zip_with_nested_memories_zip_memory_files_mapping(self, tmp_path: Path) -> None:
        memories_json = _read_fixture("claude_memories.json")
        zip_path = _build_nested_claude_zip(tmp_path, memories_json)

        items, rejected = collect_claude(zip_path, namespace="personal", today=_TODAY)

        assert rejected == []
        memory_file_items = [item for item in items if "memory_files" in item.source]
        assert len(memory_file_items) == 2

        profile_item = next(item for item in memory_file_items if "profile" in item.source)
        assert profile_item.title == "Profile"  # from the file's own first heading
        assert "Alex prefers metric units" in profile_item.body
        assert profile_item.created is not None
        assert profile_item.created.isoformat() == "2026-01-02T03:04:05+00:00"
        assert profile_item.slug_hint == "profile"

        preferences_item = next(item for item in memory_file_items if "preferences" in item.source)
        assert preferences_item.title == "Preferences"  # no heading -> humanized path stem

    def test_conversations_memory_is_split_into_bullets_and_paragraphs(
        self, tmp_path: Path
    ) -> None:
        memories_json = _read_fixture("claude_memories.json")
        zip_path = _build_nested_claude_zip(tmp_path, memories_json)

        items, _ = collect_claude(zip_path, namespace="personal", today=_TODAY)

        conv_items = [item for item in items if "conversations_memory" in item.source]
        assert len(conv_items) == 3
        assert all(item.type == "user" for item in conv_items)
        texts = {item.body.splitlines()[0] for item in conv_items}
        assert "Alex is a backend engineer at a fictional startup." in texts
        assert "Prefers Postgres over MySQL for new projects." in texts
        assert "Alex is thinking about rewriting the billing service in Rust." in texts

    def test_project_memories_become_project_type_tagged_with_project_slug(
        self, tmp_path: Path
    ) -> None:
        memories_json = _read_fixture("claude_memories.json")
        zip_path = _build_nested_claude_zip(tmp_path, memories_json)

        items, _ = collect_claude(zip_path, namespace="personal", today=_TODAY)

        project_items = [item for item in items if "project_memories" in item.source]
        assert len(project_items) == 2
        assert all(item.type == "project" for item in project_items)
        assert all(item.tags == ("0198aaaa-0000-7000-8000-000000000001",) for item in project_items)

    def test_legacy_memories_json_file(self, tmp_path: Path) -> None:
        path = tmp_path / "memories.json"
        path.write_text(_read_fixture("claude_memories_legacy.json"), encoding="utf-8")

        items, rejected = collect_claude(path, namespace="personal", today=_TODAY)

        assert rejected == []
        texts = {item.body.splitlines()[0] for item in items}
        assert "Alex grew up in a fictional town called Rivermouth." in texts
        assert "Alex has a fictional cat named Noodle." in texts

    def test_unknown_json_shape_raises_clear_error(self, tmp_path: Path) -> None:
        path = tmp_path / "export.json"
        path.write_text(_read_fixture("claude_unknown.json"), encoding="utf-8")

        with pytest.raises(ClaudeFormatError) as excinfo:
            collect_claude(path, namespace="personal", today=_TODAY)

        message = str(excinfo.value)
        assert "conversations_memory" in message
        assert "memory_files" in message

    def test_zip_with_no_memories_file_raises_clear_error(self, tmp_path: Path) -> None:
        zip_path = tmp_path / "export.zip"
        with zipfile.ZipFile(zip_path, "w") as archive:
            archive.writestr("conversations-000.zip", b"irrelevant")

        with pytest.raises(ClaudeFormatError):
            collect_claude(zip_path, namespace="personal", today=_TODAY)

    def test_plain_text_fallback(self, tmp_path: Path) -> None:
        path = tmp_path / "pasted.txt"
        path.write_text("The user likes tea.\nThe user works remotely.\n", encoding="utf-8")

        items, rejected = collect_claude(path, namespace="personal", today=_TODAY)

        assert rejected == []
        assert len(items) == 2
        assert all(item.source.startswith("import:claude:") for item in items)


class TestClaudeZipBombGuards:
    """#49 fix round: a zip bomb must never reach `json.loads`/memory exhaustion."""

    def test_zip_nested_deeper_than_two_levels_raises_clear_error(self, tmp_path: Path) -> None:
        memories_json = _read_fixture("claude_memories.json")

        # depth 3: outer export.zip -> memories-000.zip -> memories-001.zip -> memories/<uuid>.json
        innermost = tmp_path / "memories-001.zip"
        with zipfile.ZipFile(innermost, "w") as archive:
            archive.writestr("memories/0198aaaa-0000-7000-8000-00000000beef.json", memories_json)

        middle = tmp_path / "memories-000.zip"
        with zipfile.ZipFile(middle, "w") as archive:
            archive.writestr("memories-001.zip", innermost.read_bytes())

        outer = tmp_path / "export.zip"
        with zipfile.ZipFile(outer, "w") as archive:
            archive.writestr("memories-000.zip", middle.read_bytes())

        with pytest.raises(ClaudeFormatError, match="depth"):
            collect_claude(outer, namespace="personal", today=_TODAY)

    def test_highly_compressed_entry_is_rejected_without_decompressing(
        self, tmp_path: Path
    ) -> None:
        # 15 MB of zeros - under the per-entry size cap on its own, so this
        # specifically exercises the ratio check, not the size cap - but
        # compresses down to a few KB: an implausible ratio that must be
        # caught from the zip header alone, never by reading it all.
        bomb_path = tmp_path / "export.zip"
        with zipfile.ZipFile(bomb_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("memories.json", b"0" * (15 * 1024 * 1024))

        with pytest.raises(ClaudeFormatError, match="compression ratio"):
            collect_claude(bomb_path, namespace="personal", today=_TODAY)

    def test_50mb_of_zeros_is_rejected_cleanly_no_memory_error(self, tmp_path: Path) -> None:
        # The coordinator's own repro: 50 MB of zeros compresses to
        # kilobytes. Whichever guard catches it first (the per-entry size
        # cap here, since 50 MB alone exceeds it), the outcome must be a
        # clear `ClaudeFormatError`, never a `MemoryError` or a hang.
        bomb_path = tmp_path / "export.zip"
        with zipfile.ZipFile(bomb_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("memories.json", b"0" * (50 * 1024 * 1024))

        with pytest.raises(ClaudeFormatError):
            collect_claude(bomb_path, namespace="personal", today=_TODAY)

    def test_entry_with_a_forged_declared_size_fails_cleanly_not_a_crash(
        self, tmp_path: Path
    ) -> None:
        # Patch the on-disk central directory and local header `file_size`
        # fields to lie (10 bytes) about an entry that really decompresses
        # to 1 MB - a hand-crafted mismatch, not something `zipfile`'s own
        # writer ever produces. `zipfile` itself notices the CRC-32 no
        # longer matches once it believes it has read all 10 (lied) bytes
        # and raises `BadZipFile`; this importer must turn that into a
        # clear `ClaudeFormatError`, never a crash or a runaway read.
        true_content = b"0" * (1024 * 1024)
        zip_path = tmp_path / "export.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("memories.json", true_content)

        data = bytearray(zip_path.read_bytes())
        local_offset = data.index(b"PK\x03\x04")
        central_offset = data.index(b"PK\x01\x02")
        struct.pack_into("<I", data, local_offset + 22, 10)
        struct.pack_into("<I", data, central_offset + 24, 10)
        zip_path.write_bytes(bytes(data))

        with pytest.raises(ClaudeFormatError):
            collect_claude(zip_path, namespace="personal", today=_TODAY)

    def test_entry_over_the_per_entry_limit_is_skipped_not_imported(self, tmp_path: Path) -> None:
        zip_path = tmp_path / "export.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            # Declared size alone exceeds the per-entry cap; highly
            # compressible so it does not also trip the ratio check at a
            # size the entry-limit check should already have skipped.
            archive.writestr("memories.json", b"{" + b" " * (21 * 1024 * 1024) + b"}")

        with pytest.raises(ClaudeFormatError):
            collect_claude(zip_path, namespace="personal", today=_TODAY)

    def test_outer_file_over_the_total_budget_is_rejected_before_opening(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "huge.json"
        with path.open("wb") as handle:
            handle.seek(101 * 1024 * 1024 - 1)
            handle.write(b"\0")

        with pytest.raises(ClaudeFormatError, match="byte import limit"):
            collect_claude(path, namespace="personal", today=_TODAY)

    def test_a_normal_export_still_imports_successfully(self, tmp_path: Path) -> None:
        memories_json = _read_fixture("claude_memories.json")
        zip_path = _build_nested_claude_zip(tmp_path, memories_json)

        items, rejected = collect_claude(zip_path, namespace="personal", today=_TODAY)

        assert rejected == []
        assert len(items) > 0


class TestChatgptCollect:
    def test_plain_list_with_dates(self, tmp_path: Path) -> None:
        path = tmp_path / "list.txt"
        path.write_text(_read_fixture("chatgpt_list.txt"), encoding="utf-8")

        items, rejected = collect_chatgpt(path, namespace="personal", today=_TODAY)

        assert rejected == []
        assert len(items) == 3
        assert items[0].created is not None
        assert items[0].created.isoformat() == "2026-01-02T00:00:00+00:00"
        assert all(item.source.startswith("import:chatgpt:") for item in items)

    def test_from_conversations_extracts_bio_calls_only(self, tmp_path: Path) -> None:
        path = tmp_path / "conversations.json"
        path.write_text(_read_fixture("chatgpt_conversations.json"), encoding="utf-8")

        items, rejected = collect_chatgpt(
            path, namespace="personal", from_conversations=True, today=_TODAY
        )

        assert rejected == []
        assert len(items) == 2
        texts = {item.body.splitlines()[0] for item in items}
        assert "The user prefers Python for scripting tasks." in texts
        assert "The user lives in a fictional city called Rivermouth." in texts
        # The user-authored message in the fixture is not a 'bio' call.
        assert not any("Remember that" in text for text in texts)

    def test_from_conversations_requires_json_array(self, tmp_path: Path) -> None:
        path = tmp_path / "conversations.json"
        path.write_text(json.dumps({"not": "an array"}), encoding="utf-8")

        with pytest.raises(ChatGPTFormatError):
            collect_chatgpt(path, namespace="personal", from_conversations=True, today=_TODAY)

    def test_from_conversations_requires_valid_json(self, tmp_path: Path) -> None:
        path = tmp_path / "conversations.json"
        path.write_text("not json at all", encoding="utf-8")

        with pytest.raises(ChatGPTFormatError):
            collect_chatgpt(path, namespace="personal", from_conversations=True, today=_TODAY)


class TestRunImportIntegration:
    async def test_dry_run_writes_nothing(
        self,
        tmp_path: Path,
        backend: StorageBackend,
        repo: Repo,
        vault_config: VaultConfig,
        bare_remote: Path,
    ) -> None:
        path = tmp_path / "list.txt"
        path.write_text(_read_fixture("chatgpt_list.txt"), encoding="utf-8")

        before = _log(bare_remote)
        items, rejected = collect_chatgpt(path, namespace="personal", today=_TODAY)
        kept, duplicates = await dedupe_against_vault(items, backend)
        report = await run_import(kept, backend, apply=False)

        assert rejected == []
        assert duplicates == []
        assert len(report.created) == 3
        assert _log(bare_remote) == before

    async def test_apply_creates_one_commit_per_note_authored_import(
        self,
        tmp_path: Path,
        backend: StorageBackend,
        repo: Repo,
        vault_config: VaultConfig,
        bare_remote: Path,
    ) -> None:
        path = tmp_path / "list.txt"
        path.write_text(_read_fixture("chatgpt_list.txt"), encoding="utf-8")

        items, _ = collect_chatgpt(path, namespace="personal", today=_TODAY)
        kept, duplicates = await dedupe_against_vault(items, backend)
        report = await run_import(kept, backend, apply=True)

        assert duplicates == []
        assert len(report.created) == 3

        log_result = Git(cwd=bare_remote).run("log", "--format=%an")
        authors = {
            line for line in log_result.stdout.decode("utf-8").strip("\n").split("\n") if line
        }
        assert authors == {"import"}
        assert len(_log(bare_remote)) == 3

    async def test_rerunning_the_same_import_creates_nothing_new(
        self, tmp_path: Path, backend: StorageBackend, repo: Repo, vault_config: VaultConfig
    ) -> None:
        path = tmp_path / "list.txt"
        path.write_text(_read_fixture("chatgpt_list.txt"), encoding="utf-8")

        first_items, _ = collect_chatgpt(path, namespace="personal", today=_TODAY)
        first_kept, _ = await dedupe_against_vault(first_items, backend)
        first_report = await run_import(first_kept, backend, apply=True)
        assert len(first_report.created) == 3

        await asyncio.to_thread(repo.sync)
        second_items, _ = collect_chatgpt(path, namespace="personal", today=_TODAY)
        second_kept, second_duplicates = await dedupe_against_vault(second_items, backend)

        assert second_kept == []
        assert len(second_duplicates) == 3

        second_report = await run_import(second_kept, backend, apply=True)
        assert second_report.created == []

    async def test_fake_secret_item_is_rejected_others_are_imported(
        self,
        tmp_path: Path,
        backend: StorageBackend,
        repo: Repo,
        vault_config: VaultConfig,
        bare_remote: Path,
    ) -> None:
        path = tmp_path / "list.txt"
        path.write_text(
            f"The user's AWS access key is {_FAKE_AWS_ACCESS_KEY_ID}, do not lose it.\n"
            "The user prefers dark mode in every app.\n",
            encoding="utf-8",
        )

        items, _ = collect_chatgpt(path, namespace="personal", today=_TODAY)
        kept, duplicates = await dedupe_against_vault(items, backend)
        report = await run_import(kept, backend, apply=True)

        assert duplicates == []
        assert len(report.created) == 1
        assert len(report.rejected) == 1
        source_ref, reason = report.rejected[0]
        assert source_ref.startswith("import:chatgpt:")
        assert "secret" in reason.lower() or "aws" in reason.lower()

    async def test_dedupe_against_vault_skips_a_memory_already_imported_by_claude(
        self,
        tmp_path: Path,
        backend: StorageBackend,
        repo: Repo,
        vault_config: VaultConfig,
        bare_remote: Path,
    ) -> None:
        existing_note = (
            b"---\n"
            b"id: 01ARZ3NDEKTSV4RRFFQ69G5FAV\n"
            b"title: Prefers Python\n"
            b"description: The user prefers Python for scripting tasks.\n"
            b"type: user\n"
            b"created: 2026-01-01T00:00:00Z\n"
            b"updated: 2026-01-01T00:00:00Z\n"
            b"source: import:claude:conversations_memory:0\n"
            b"---\n"
            b"The user prefers Python for scripting tasks.\n\n"
            b"Imported from claude on 2026-01-01.\n"
        )
        human_commit(bare_remote, "personal/user/already-there.md", existing_note)

        path = tmp_path / "list.txt"
        path.write_text(_read_fixture("chatgpt_list.txt"), encoding="utf-8")

        items, _ = collect_chatgpt(path, namespace="personal", today=_TODAY)
        await asyncio.to_thread(repo.sync)
        kept, duplicates = await dedupe_against_vault(items, backend)

        assert len(duplicates) == 1
        assert len(kept) == 2
        assert all("prefers Python" not in item.body for item in kept)


class TestEndToEndCommittedNote:
    async def test_imported_note_parses_and_has_expected_source(
        self,
        tmp_path: Path,
        backend: StorageBackend,
        repo: Repo,
        vault_config: VaultConfig,
        bare_remote: Path,
    ) -> None:
        path = tmp_path / "list.txt"
        path.write_text("The user prefers dark mode in every app.\n", encoding="utf-8")

        items, _ = collect_chatgpt(path, namespace="personal", today=_TODAY)
        kept, _ = await dedupe_against_vault(items, backend)
        report = await run_import(kept, backend, apply=True)

        assert len(report.created) == 1
        note_path = report.created[0]
        note_bytes = _remote_file(bare_remote, note_path)
        assert note_bytes is not None
        note = parse_note(note_bytes)
        assert note.source == "import:chatgpt:line:0"
        assert "Imported from chatgpt on 2026-10-06." in note.body
