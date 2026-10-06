# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for `Repo.sync()` and `poll_loop` (#12)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from conftest import human_commit, human_delete, human_rename

from memory_manager.config import VaultConfig
from memory_manager.vault.git import GitError
from memory_manager.vault.repo import Repo, SyncDiverged, author_for
from memory_manager.vault.sync import ChangeSet, poll_loop


class TestSync:
    def test_reports_added_files_on_the_first_sync_from_unborn_head(
        self, vault_config: VaultConfig, bare_remote: Path
    ) -> None:
        repo = Repo(vault_config)
        repo.ensure_clone()
        human_commit(bare_remote, "personal/fact/a.md", b"first\n")

        change = repo.sync()

        assert change.old_head is None
        assert change.new_head == repo.head()
        assert change.added == ("personal/fact/a.md",)
        assert change.modified == ()
        assert change.deleted == ()
        assert change.ignored == ()
        assert not change.empty

    def test_reports_a_modification_on_a_later_sync(
        self, vault_config: VaultConfig, bare_remote: Path
    ) -> None:
        repo = Repo(vault_config)
        repo.ensure_clone()
        human_commit(bare_remote, "personal/fact/a.md", b"first\n")
        repo.sync()
        old_head = repo.head()

        human_commit(bare_remote, "personal/fact/a.md", b"first, edited\n")
        change = repo.sync()

        assert change.old_head == old_head
        assert change.new_head == repo.head()
        assert change.added == ()
        assert change.modified == ("personal/fact/a.md",)
        assert change.deleted == ()

    def test_reports_a_deletion_on_a_later_sync(
        self, vault_config: VaultConfig, bare_remote: Path
    ) -> None:
        repo = Repo(vault_config)
        repo.ensure_clone()
        human_commit(bare_remote, "personal/fact/a.md", b"first\n")
        repo.sync()

        human_delete(bare_remote, "personal/fact/a.md")
        change = repo.sync()

        assert change.added == ()
        assert change.modified == ()
        assert change.deleted == ("personal/fact/a.md",)

    def test_reports_a_rename_as_a_delete_and_an_add(
        self, vault_config: VaultConfig, bare_remote: Path
    ) -> None:
        repo = Repo(vault_config)
        repo.ensure_clone()
        human_commit(bare_remote, "personal/fact/a.md", b"first\n")
        repo.sync()

        human_rename(bare_remote, "personal/fact/a.md", "personal/fact/a2.md")
        change = repo.sync()

        assert change.deleted == ("personal/fact/a.md",)
        assert change.added == ("personal/fact/a2.md",)
        assert change.modified == ()

    def test_non_note_file_goes_to_ignored(
        self, vault_config: VaultConfig, bare_remote: Path
    ) -> None:
        repo = Repo(vault_config)
        repo.ensure_clone()
        human_commit(bare_remote, "README.md", b"not a note\n")

        change = repo.sync()

        assert change.added == ()
        assert change.modified == ()
        assert change.deleted == ()
        assert change.ignored == ("README.md",)
        assert change.empty

    def test_sync_on_an_empty_remote_is_a_noop(self, vault_config: VaultConfig) -> None:
        repo = Repo(vault_config)
        repo.ensure_clone()

        change = repo.sync()

        assert change.empty
        assert change.old_head is None
        assert change.new_head is None

    def test_local_ahead_of_remote_is_a_noop(
        self, vault_config: VaultConfig, bare_remote: Path
    ) -> None:
        repo = Repo(vault_config)
        repo.ensure_clone()
        repo.commit_file("personal/fact/a.md", b"first\n", author_for("claude-code"), "add a")
        repo.push()
        repo.commit_file("personal/fact/b.md", b"second\n", author_for("claude-code"), "add b")
        head = repo.head()

        change = repo.sync()

        assert change.empty
        assert change.old_head == head
        assert change.new_head == head
        assert repo.head() == head

    def test_diverged_local_and_remote_raises_sync_diverged(
        self, vault_config: VaultConfig, bare_remote: Path
    ) -> None:
        repo = Repo(vault_config)
        repo.ensure_clone()
        repo.commit_file("personal/fact/a.md", b"first\n", author_for("claude-code"), "add a")
        repo.push()
        repo.commit_file("personal/fact/c.md", b"third\n", author_for("claude-code"), "add c")

        human_commit(bare_remote, "personal/fact/b.md", b"human change\n")

        with pytest.raises(SyncDiverged):
            repo.sync()


class TestPollLoop:
    async def test_calls_sync_and_stops_once_a_human_commit_is_detected(
        self, vault_config: VaultConfig, bare_remote: Path
    ) -> None:
        """`poll_loop` only triggers `sync`; a caller observes the result through `sync`
        itself (`WriteQueue.sync`'s sync hooks in production, #33) - simulated here by
        having the `sync` callable itself record what it saw and stop the loop.
        """
        repo = Repo(vault_config)
        repo.ensure_clone()
        human_commit(bare_remote, "personal/fact/a.md", b"first\n")

        received: list[ChangeSet] = []
        stop = asyncio.Event()

        async def sync() -> ChangeSet:
            change_set = await asyncio.to_thread(repo.sync)
            received.append(change_set)
            if not change_set.empty:
                stop.set()
            return change_set

        await asyncio.wait_for(
            poll_loop(sync, 0.05, stop=stop),
            timeout=5,
        )

        assert len(received) == 1
        assert received[0].added == ("personal/fact/a.md",)

    async def test_survives_a_git_error_from_sync_and_keeps_polling(self) -> None:
        stop = asyncio.Event()
        calls = {"count": 0}

        async def flaky_sync() -> ChangeSet:
            calls["count"] += 1
            if calls["count"] == 1:
                raise GitError(("fetch",), 1, "simulated network failure")
            stop.set()
            return ChangeSet(old_head=None, new_head=None)

        await asyncio.wait_for(
            poll_loop(flaky_sync, 0.01, stop=stop),
            timeout=5,
        )

        assert calls["count"] == 2

    async def test_stops_promptly_when_stop_is_already_set(self) -> None:
        stop = asyncio.Event()
        stop.set()
        calls = {"count": 0}

        async def counting_sync() -> ChangeSet:
            calls["count"] += 1
            return ChangeSet(old_head=None, new_head=None)

        await asyncio.wait_for(
            poll_loop(counting_sync, 10.0, stop=stop),
            timeout=5,
        )

        assert calls["count"] == 0
