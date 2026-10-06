# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the write queue's push-rejection handling: rebase and conflict files (#15)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import human_commit, human_delete
from git_fixtures import wrap_push_with_side_effect

from memory_manager.config import VaultConfig
from memory_manager.queue import WriteConflict, WriteFailed, WriteQueue, WriteRequest
from memory_manager.vault.git import Git
from memory_manager.vault.note import Note, serialize, version
from memory_manager.vault.paths import PathRejected, parse_note_path
from memory_manager.vault.repo import Repo
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


def _log(remote: Path) -> list[str]:
    result = Git(cwd=remote).run("log", "--format=%H", check=False)
    if result.returncode != 0:
        return []
    lines = result.stdout.decode("utf-8").strip("\n").split("\n")
    return [line for line in lines if line]


def _remote_show(remote: Path, rel: str, branch: str = "main") -> bytes | None:
    result = Git(cwd=remote).run("show", f"{branch}:{rel}", check=False)
    if result.returncode != 0:
        return None
    return result.stdout


def _remote_head(remote: Path, branch: str = "main") -> str:
    return Git(cwd=remote).run("rev-parse", branch).stdout.decode("utf-8").strip()


def _is_clean_and_pushed(vault_dir: Path, remote: Path, branch: str = "main") -> bool:
    status = Git(cwd=vault_dir).run("status", "--porcelain").stdout
    head = Git(cwd=vault_dir).run("rev-parse", "HEAD").stdout.decode("utf-8").strip()
    return status == b"" and head == _remote_head(remote, branch)


@pytest.fixture
def repo(vault_config: VaultConfig) -> Repo:
    return Repo(vault_config)


@pytest.fixture
async def queue(repo: Repo) -> AsyncIterator[WriteQueue]:
    write_queue = WriteQueue(repo)
    await write_queue.start()
    try:
        yield write_queue
    finally:
        await write_queue.stop()


class TestRebaseSucceeds:
    async def test_unrelated_remote_change_rebases_cleanly_and_the_write_lands(
        self, repo: Repo, queue: WriteQueue, bare_remote: Path
    ) -> None:
        wrap_push_with_side_effect(
            repo,
            lambda n: human_commit(
                bare_remote, "personal/fact/unrelated.md", _note_bytes(title="Unrelated")
            ),
        )

        content = _note_bytes()
        result = await queue.submit(
            WriteRequest(
                op="write",
                path="personal/fact/a.md",
                client="claude-code",
                if_version="new",
                content=content,
            )
        )

        log = _log(bare_remote)
        assert result.commit in log
        assert len(log) == 2  # our write + the injected human commit, both on the remote
        assert _remote_show(bare_remote, "personal/fact/a.md") == content
        assert _remote_show(bare_remote, "personal/fact/a.conflict.md") is None


class TestRebaseConflicts:
    async def test_same_note_changed_on_remote_raises_write_conflict(
        self, repo: Repo, queue: WriteQueue, bare_remote: Path, vault_config: VaultConfig
    ) -> None:
        note_id = new_ulid(_CREATED)
        first = _note_bytes(id=note_id)
        result = await queue.submit(
            WriteRequest(
                op="write",
                path="personal/fact/a.md",
                client="claude-code",
                if_version="new",
                content=first,
            )
        )

        human_version = _note_bytes(id=note_id, title="Edited by a human")
        wrap_push_with_side_effect(
            repo, lambda n: human_commit(bare_remote, "personal/fact/a.md", human_version)
        )

        ours = _note_bytes(id=note_id, title="Overwritten by claude-code")
        with pytest.raises(WriteConflict) as excinfo:
            await queue.submit(
                WriteRequest(
                    op="write",
                    path="personal/fact/a.md",
                    client="claude-code",
                    if_version=result.version,
                    content=ours,
                )
            )

        exc = excinfo.value
        assert exc.path == "personal/fact/a.md"
        assert exc.conflict_path == "personal/fact/a.conflict.md"
        assert exc.current_version == version(human_version)
        assert exc.current_content == human_version.decode("utf-8")

        # Nothing was overwritten: the remote note is exactly the human version.
        assert _remote_show(bare_remote, "personal/fact/a.md") == human_version

        # Remote history: first write, human edit, our conflict-file commit (newest).
        log = _log(bare_remote)
        assert len(log) == 3
        human_commit_sha = log[1]

        conflict_bytes = _remote_show(bare_remote, "personal/fact/a.conflict.md")
        assert conflict_bytes is not None
        conflict_text = conflict_bytes.decode("utf-8")
        assert "personal/fact/a.md" in conflict_text
        assert "claude-code" in conflict_text
        assert human_version.decode("utf-8") in conflict_text
        assert ours.decode("utf-8") in conflict_text
        assert human_commit_sha[:7] in conflict_text

        assert _is_clean_and_pushed(vault_config.dir, bare_remote)

    async def test_note_deleted_on_remote_reports_no_current_content(
        self, repo: Repo, queue: WriteQueue, bare_remote: Path, vault_config: VaultConfig
    ) -> None:
        note_id = new_ulid(_CREATED)
        first = _note_bytes(id=note_id)
        result = await queue.submit(
            WriteRequest(
                op="write",
                path="personal/fact/a.md",
                client="claude-code",
                if_version="new",
                content=first,
            )
        )

        wrap_push_with_side_effect(repo, lambda n: human_delete(bare_remote, "personal/fact/a.md"))

        ours = _note_bytes(id=note_id, title="Overwritten by claude-code")
        with pytest.raises(WriteConflict) as excinfo:
            await queue.submit(
                WriteRequest(
                    op="write",
                    path="personal/fact/a.md",
                    client="claude-code",
                    if_version=result.version,
                    content=ours,
                )
            )

        exc = excinfo.value
        assert exc.current_version is None
        assert exc.current_content is None
        assert _remote_show(bare_remote, "personal/fact/a.md") is None

        conflict_text = _remote_show(bare_remote, "personal/fact/a.conflict.md")
        assert conflict_text is not None
        assert "(deleted on the remote)" in conflict_text.decode("utf-8")
        assert ours.decode("utf-8") in conflict_text.decode("utf-8")

        assert _is_clean_and_pushed(vault_config.dir, bare_remote)

    async def test_overwriting_an_existing_conflict_file_replaces_it(
        self, repo: Repo, queue: WriteQueue, bare_remote: Path
    ) -> None:
        note_id = new_ulid(_CREATED)
        first = _note_bytes(id=note_id)
        result = await queue.submit(
            WriteRequest(
                op="write",
                path="personal/fact/a.md",
                client="claude-code",
                if_version="new",
                content=first,
            )
        )

        wrap_push_with_side_effect(
            repo,
            lambda n: human_commit(
                bare_remote, "personal/fact/a.md", _note_bytes(id=note_id, title="Human one")
            ),
        )
        with pytest.raises(WriteConflict):
            await queue.submit(
                WriteRequest(
                    op="write",
                    path="personal/fact/a.md",
                    client="claude-code",
                    if_version=result.version,
                    content=_note_bytes(id=note_id, title="Claude attempt one"),
                )
            )
        first_conflict = _remote_show(bare_remote, "personal/fact/a.conflict.md")
        assert first_conflict is not None

        current = _remote_show(bare_remote, "personal/fact/a.md")
        assert current is not None
        wrap_push_with_side_effect(
            repo,
            lambda n: human_commit(
                bare_remote, "personal/fact/a.md", _note_bytes(id=note_id, title="Human two")
            ),
        )
        with pytest.raises(WriteConflict):
            await queue.submit(
                WriteRequest(
                    op="write",
                    path="personal/fact/a.md",
                    client="claude-code",
                    if_version=version(current),
                    content=_note_bytes(id=note_id, title="Claude attempt two"),
                )
            )
        second_conflict = _remote_show(bare_remote, "personal/fact/a.conflict.md")
        assert second_conflict is not None
        assert second_conflict != first_conflict
        assert "Claude attempt two" in second_conflict.decode("utf-8")


class TestPersistentRejection:
    async def test_remote_keeps_moving_gives_up_as_write_failed(
        self, repo: Repo, queue: WriteQueue, bare_remote: Path, vault_config: VaultConfig
    ) -> None:
        def keep_moving(n: int) -> None:
            human_commit(bare_remote, f"personal/fact/filler-{n}.md", f"filler {n}\n".encode())

        wrap_push_with_side_effect(repo, keep_moving, times=None)

        with pytest.raises(WriteFailed):
            await queue.submit(
                WriteRequest(
                    op="write",
                    path="personal/fact/a.md",
                    client="claude-code",
                    if_version="new",
                    content=_note_bytes(),
                )
            )

        assert _remote_show(bare_remote, "personal/fact/a.md") is None
        assert _is_clean_and_pushed(vault_config.dir, bare_remote)


class TestQueueRecoversAfterConflict:
    async def test_next_write_with_the_fresh_version_succeeds(
        self, repo: Repo, queue: WriteQueue, bare_remote: Path
    ) -> None:
        note_id = new_ulid(_CREATED)
        first = _note_bytes(id=note_id)
        result = await queue.submit(
            WriteRequest(
                op="write",
                path="personal/fact/a.md",
                client="claude-code",
                if_version="new",
                content=first,
            )
        )

        human_version = _note_bytes(id=note_id, title="Edited by a human")
        wrap_push_with_side_effect(
            repo, lambda n: human_commit(bare_remote, "personal/fact/a.md", human_version)
        )
        with pytest.raises(WriteConflict) as excinfo:
            await queue.submit(
                WriteRequest(
                    op="write",
                    path="personal/fact/a.md",
                    client="claude-code",
                    if_version=result.version,
                    content=_note_bytes(id=note_id, title="Overwritten"),
                )
            )
        fresh_version = excinfo.value.current_version
        assert fresh_version is not None

        retried = await queue.submit(
            WriteRequest(
                op="write",
                path="personal/fact/a.md",
                client="claude-code",
                if_version=fresh_version,
                content=_note_bytes(id=note_id, title="Retried after conflict"),
            )
        )
        assert retried.version == version(_note_bytes(id=note_id, title="Retried after conflict"))
        assert _remote_show(bare_remote, "personal/fact/a.md") == _note_bytes(
            id=note_id, title="Retried after conflict"
        )


class TestConflictPathIsNotClientWritable:
    def test_parse_note_path_rejects_a_conflict_file_path(self) -> None:
        with pytest.raises(PathRejected):
            parse_note_path("personal/fact/a.conflict.md")
