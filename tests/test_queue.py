# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the write queue: serialization and `if_version` conflicts (#14, #33)."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from conftest import human_commit

from memory_manager.compat.select import (
    current_resolved_profile,
    reset_resolved_profile,
    set_resolved_profile,
)
from memory_manager.config import VaultConfig
from memory_manager.observability.logging import current_request_id, request_id_var
from memory_manager.queue import (
    EditMismatch,
    InvalidNote,
    NotFound,
    SecretRejected,
    VersionConflict,
    WriteQueue,
    WriteRequest,
    WriteResult,
)
from memory_manager.vault.note import Note, parse, serialize, version
from memory_manager.vault.repo import Repo
from memory_manager.vault.sync import ChangeSet
from memory_manager.vault.ulid import new_ulid

_CREATED = datetime(2026, 1, 1, tzinfo=UTC)
_FAKE_AWS_ACCESS_KEY_ID = "AKIA" + "IOSFODNN7EXAMPLE"


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
    from memory_manager.vault.git import Git

    result = Git(cwd=remote).run("log", "--format=%H", check=False)
    if result.returncode != 0:
        return []
    lines = result.stdout.decode("utf-8").strip("\n").split("\n")
    return [line for line in lines if line]


@pytest.fixture
async def queue(vault_config: VaultConfig) -> AsyncIterator[WriteQueue]:
    repo = Repo(vault_config)
    write_queue = WriteQueue(repo)
    await write_queue.start()
    try:
        yield write_queue
    finally:
        await write_queue.stop()


class TestWrite:
    async def test_happy_path_write_new_commits_with_correct_author(
        self, queue: WriteQueue, bare_remote: Path
    ) -> None:
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

        assert isinstance(result, WriteResult)
        assert result.version == version(content)
        log = _log(bare_remote)
        assert result.commit in log

    async def test_stale_version_is_rejected_and_nothing_committed(
        self, queue: WriteQueue, bare_remote: Path
    ) -> None:
        first = _note_bytes()
        await queue.submit(
            WriteRequest(
                op="write",
                path="personal/fact/a.md",
                client="human",
                if_version="new",
                content=first,
            )
        )
        before = _log(bare_remote)

        second = _note_bytes(title="Changed title")
        with pytest.raises(VersionConflict) as excinfo:
            await queue.submit(
                WriteRequest(
                    op="write",
                    path="personal/fact/a.md",
                    client="human",
                    if_version="0" * 64,
                    content=second,
                )
            )

        assert excinfo.value.current_version == version(first)
        assert excinfo.value.current_content == first.decode("utf-8")
        assert _log(bare_remote) == before

    async def test_new_on_existing_file_is_rejected(
        self, queue: WriteQueue, bare_remote: Path
    ) -> None:
        content = _note_bytes()
        await queue.submit(
            WriteRequest(
                op="write",
                path="personal/fact/a.md",
                client="human",
                if_version="new",
                content=content,
            )
        )
        before = _log(bare_remote)

        with pytest.raises(VersionConflict) as excinfo:
            await queue.submit(
                WriteRequest(
                    op="write",
                    path="personal/fact/a.md",
                    client="human",
                    if_version="new",
                    content=_note_bytes(title="Other"),
                )
            )
        assert excinfo.value.current_version == version(content)
        assert _log(bare_remote) == before

    async def test_write_with_changed_id_is_rejected(
        self, queue: WriteQueue, bare_remote: Path
    ) -> None:
        first = _note_bytes()
        result = await queue.submit(
            WriteRequest(
                op="write",
                path="personal/fact/a.md",
                client="human",
                if_version="new",
                content=first,
            )
        )
        before = _log(bare_remote)

        changed_id = _note_bytes(id=new_ulid(_CREATED))
        with pytest.raises(InvalidNote) as excinfo:
            await queue.submit(
                WriteRequest(
                    op="write",
                    path="personal/fact/a.md",
                    client="human",
                    if_version=result.version,
                    content=changed_id,
                )
            )
        assert "id must not change" in str(excinfo.value)
        assert _log(bare_remote) == before

    async def test_invalid_note_is_rejected_and_nothing_committed(
        self, queue: WriteQueue, bare_remote: Path
    ) -> None:
        before = _log(bare_remote)
        invalid = _note_bytes(description="x" * 151)
        with pytest.raises(InvalidNote):
            await queue.submit(
                WriteRequest(
                    op="write",
                    path="personal/fact/a.md",
                    client="human",
                    if_version="new",
                    content=invalid,
                )
            )
        assert _log(bare_remote) == before

    async def test_secret_in_body_is_rejected_and_nothing_committed(
        self, queue: WriteQueue, bare_remote: Path
    ) -> None:
        before = _log(bare_remote)
        with_secret = _note_bytes(body=f"AWS_ACCESS_KEY_ID={_FAKE_AWS_ACCESS_KEY_ID}\n")
        with pytest.raises(SecretRejected):
            await queue.submit(
                WriteRequest(
                    op="write",
                    path="personal/fact/a.md",
                    client="human",
                    if_version="new",
                    content=with_secret,
                )
            )
        assert _log(bare_remote) == before

    async def test_non_canonical_input_is_committed_canonically(
        self, queue: WriteQueue, vault_config: VaultConfig
    ) -> None:
        canonical = _note_bytes()
        non_canonical = canonical.replace(b"title: A note", b'title: "A note"')
        assert non_canonical != canonical

        result = await queue.submit(
            WriteRequest(
                op="write",
                path="personal/fact/a.md",
                client="human",
                if_version="new",
                content=non_canonical,
            )
        )

        on_disk = (vault_config.dir / "personal/fact/a.md").read_bytes()
        assert on_disk == canonical
        assert result.version == version(canonical)

    async def test_human_commit_between_two_writes_is_picked_up_before_version_check(
        self, queue: WriteQueue, bare_remote: Path
    ) -> None:
        first = _note_bytes()
        result = await queue.submit(
            WriteRequest(
                op="write",
                path="personal/fact/a.md",
                client="human",
                if_version="new",
                content=first,
            )
        )

        human_version_bytes = _note_bytes(title="Edited by a human")
        human_commit(bare_remote, "personal/fact/a.md", human_version_bytes)

        with pytest.raises(VersionConflict) as excinfo:
            await queue.submit(
                WriteRequest(
                    op="write",
                    path="personal/fact/a.md",
                    client="claude-code",
                    if_version=result.version,
                    content=_note_bytes(title="Overwritten by the client"),
                )
            )
        assert excinfo.value.current_version == version(human_version_bytes)
        assert excinfo.value.current_content == human_version_bytes.decode("utf-8")


class TestEdit:
    async def test_happy_path_edit(self, queue: WriteQueue, bare_remote: Path) -> None:
        note_id = new_ulid(_CREATED)
        content = _note_bytes(id=note_id, body="Old body.\n")
        result = await queue.submit(
            WriteRequest(
                op="write",
                path="personal/fact/a.md",
                client="human",
                if_version="new",
                content=content,
            )
        )

        edited = await queue.submit(
            WriteRequest(
                op="edit",
                path="personal/fact/a.md",
                client="human",
                if_version=result.version,
                old_str="Old body.",
                new_str="New body.",
            )
        )
        expected = _note_bytes(id=note_id, body="New body.\n")
        assert edited.version == version(expected)
        log = _log(bare_remote)
        assert edited.commit in log

    async def test_edit_with_zero_matches_is_rejected(self, queue: WriteQueue) -> None:
        content = _note_bytes(body="Old body.\n")
        result = await queue.submit(
            WriteRequest(
                op="write",
                path="personal/fact/a.md",
                client="human",
                if_version="new",
                content=content,
            )
        )

        with pytest.raises(EditMismatch) as excinfo:
            await queue.submit(
                WriteRequest(
                    op="edit",
                    path="personal/fact/a.md",
                    client="human",
                    if_version=result.version,
                    old_str="does not occur",
                    new_str="x",
                )
            )
        assert excinfo.value.count == 0

    async def test_edit_with_two_matches_is_rejected(self, queue: WriteQueue) -> None:
        content = _note_bytes(body="dup dup\n")
        result = await queue.submit(
            WriteRequest(
                op="write",
                path="personal/fact/a.md",
                client="human",
                if_version="new",
                content=content,
            )
        )

        with pytest.raises(EditMismatch) as excinfo:
            await queue.submit(
                WriteRequest(
                    op="edit",
                    path="personal/fact/a.md",
                    client="human",
                    if_version=result.version,
                    old_str="dup",
                    new_str="x",
                )
            )
        assert excinfo.value.count == 2


class TestArchive:
    async def test_archive_moves_file_and_sets_updated(
        self, queue: WriteQueue, vault_config: VaultConfig
    ) -> None:
        content = _note_bytes()
        result = await queue.submit(
            WriteRequest(
                op="write",
                path="personal/fact/a.md",
                client="human",
                if_version="new",
                content=content,
            )
        )

        fixed_now = datetime(2026, 5, 1, 12, 0, 0, 123456, tzinfo=UTC)
        repo = Repo(vault_config)
        archive_queue = WriteQueue(repo, clock=lambda: fixed_now)
        await archive_queue.start()
        try:
            archived = await archive_queue.submit(
                WriteRequest(
                    op="archive",
                    path="personal/fact/a.md",
                    client="human",
                    if_version=result.version,
                )
            )
        finally:
            await archive_queue.stop()

        assert archived.path == "_archive/personal/fact/a.md"
        assert not (vault_config.dir / "personal/fact/a.md").exists()
        archived_disk = vault_config.dir / "_archive/personal/fact/a.md"
        assert archived_disk.exists()
        assert b"updated: 2026-05-01T12:00:00Z" in archived_disk.read_bytes()

    async def test_archive_of_missing_file_is_not_found(self, queue: WriteQueue) -> None:
        with pytest.raises(NotFound):
            await queue.submit(
                WriteRequest(
                    op="archive",
                    path="personal/fact/missing.md",
                    client="human",
                    if_version="new",
                )
            )


class TestSupersede:
    async def _write(self, queue: WriteQueue, path: str, **overrides: object) -> WriteResult:
        return await queue.submit(
            WriteRequest(
                op="write",
                path=path,
                client="human",
                if_version="new",
                content=_note_bytes(**overrides),
            )
        )

    async def test_happy_path_commits_both_notes_together(
        self, queue: WriteQueue, vault_config: VaultConfig, bare_remote: Path
    ) -> None:
        old_id = new_ulid(_CREATED)
        old_result = await self._write(queue, "personal/fact/old.md", id=old_id, body="Old.\n")

        new_id = new_ulid(_CREATED)
        fixed_now = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
        supersede_queue = WriteQueue(Repo(vault_config), clock=lambda: fixed_now)
        await supersede_queue.start()
        try:
            result = await supersede_queue.submit(
                WriteRequest(
                    op="supersede",
                    path="personal/fact/old.md",
                    new_path="personal/fact/new.md",
                    client="human",
                    if_version=old_result.version,
                    content=_note_bytes(id=new_id, body="New.\n"),
                )
            )
        finally:
            await supersede_queue.stop()

        assert result.path == "personal/fact/new.md"
        new_disk = parse((vault_config.dir / "personal/fact/new.md").read_bytes())
        assert new_disk.supersedes == (old_id,)

        old_disk = parse((vault_config.dir / "personal/fact/old.md").read_bytes())
        assert old_disk.valid_to == fixed_now.date()
        assert old_disk.updated == fixed_now
        assert result.related == {"personal/fact/old.md": version(serialize(old_disk))}

        log_with_commit = _log(bare_remote)
        assert result.commit in log_with_commit

        from memory_manager.vault.git import Git

        changed = (
            Git(cwd=bare_remote)
            .run("diff-tree", "--no-commit-id", "--name-only", "-r", result.commit)
            .stdout.decode()
            .split()
        )
        assert set(changed) == {"personal/fact/old.md", "personal/fact/new.md"}

    async def test_keeps_an_earlier_valid_to_already_set_on_the_old_note(
        self, vault_config: VaultConfig
    ) -> None:
        write_queue = WriteQueue(Repo(vault_config))
        await write_queue.start()
        try:
            old_result = await self._write(
                write_queue,
                "personal/fact/old.md",
                valid_to=date(2025, 1, 1),
            )
        finally:
            await write_queue.stop()

        fixed_now = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
        supersede_queue = WriteQueue(Repo(vault_config), clock=lambda: fixed_now)
        await supersede_queue.start()
        try:
            await supersede_queue.submit(
                WriteRequest(
                    op="supersede",
                    path="personal/fact/old.md",
                    new_path="personal/fact/new.md",
                    client="human",
                    if_version=old_result.version,
                    content=_note_bytes(id=new_ulid(_CREATED)),
                )
            )
        finally:
            await supersede_queue.stop()

        old_disk = parse((vault_config.dir / "personal/fact/old.md").read_bytes())
        assert old_disk.valid_to == date(2025, 1, 1)

    async def test_stale_old_version_is_a_conflict_and_nothing_committed(
        self, queue: WriteQueue, bare_remote: Path
    ) -> None:
        await self._write(queue, "personal/fact/old.md")
        before = _log(bare_remote)

        with pytest.raises(VersionConflict) as excinfo:
            await queue.submit(
                WriteRequest(
                    op="supersede",
                    path="personal/fact/old.md",
                    new_path="personal/fact/new.md",
                    client="human",
                    if_version="0" * 64,
                    content=_note_bytes(id=new_ulid(_CREATED)),
                )
            )
        assert excinfo.value.current_content is not None
        assert _log(bare_remote) == before

    async def test_new_path_already_existing_is_rejected(
        self, queue: WriteQueue, bare_remote: Path
    ) -> None:
        old_result = await self._write(queue, "personal/fact/old.md")
        await self._write(queue, "personal/fact/new.md", id=new_ulid(_CREATED))
        before = _log(bare_remote)

        with pytest.raises(InvalidNote) as excinfo:
            await queue.submit(
                WriteRequest(
                    op="supersede",
                    path="personal/fact/old.md",
                    new_path="personal/fact/new.md",
                    client="human",
                    if_version=old_result.version,
                    content=_note_bytes(id=new_ulid(_CREATED)),
                )
            )
        assert "already exists" in str(excinfo.value)
        assert _log(bare_remote) == before

    async def test_supersede_of_missing_old_note_is_not_found(self, queue: WriteQueue) -> None:
        with pytest.raises(NotFound):
            await queue.submit(
                WriteRequest(
                    op="supersede",
                    path="personal/fact/missing.md",
                    new_path="personal/fact/new.md",
                    client="human",
                    if_version="new",
                    content=_note_bytes(id=new_ulid(_CREATED)),
                )
            )


class TestConcurrency:
    async def test_concurrent_submits_for_different_files_all_commit_serialized(
        self, queue: WriteQueue, bare_remote: Path
    ) -> None:
        requests = [
            WriteRequest(
                op="write",
                path=f"personal/fact/note-{i}.md",
                client="human",
                if_version="new",
                content=_note_bytes(id=new_ulid(_CREATED), title=f"Note {i}"),
            )
            for i in range(10)
        ]

        results = await asyncio.gather(*(queue.submit(request) for request in requests))

        assert len({result.commit for result in results}) == 10
        log = _log(bare_remote)
        assert len(log) == 10
        for result in results:
            assert result.commit in log


class TestHooks:
    async def test_hook_is_called_once_per_write(self, queue: WriteQueue) -> None:
        calls: list[tuple[WriteResult, str, tuple[str, ...]]] = []

        async def hook(
            result: WriteResult, request: WriteRequest, changed_paths: tuple[str, ...]
        ) -> None:
            calls.append((result, request.path, changed_paths))

        queue.add_hook(hook)

        result = await queue.submit(
            WriteRequest(
                op="write",
                path="personal/fact/a.md",
                client="human",
                if_version="new",
                content=_note_bytes(),
            )
        )

        assert len(calls) == 1
        assert calls[0] == (result, "personal/fact/a.md", ("personal/fact/a.md",))

    async def test_failing_hook_does_not_fail_the_write(self, queue: WriteQueue) -> None:
        async def failing_hook(
            result: WriteResult, request: WriteRequest, changed_paths: tuple[str, ...]
        ) -> None:
            raise RuntimeError("boom")

        queue.add_hook(failing_hook)

        result = await queue.submit(
            WriteRequest(
                op="write",
                path="personal/fact/a.md",
                client="human",
                if_version="new",
                content=_note_bytes(),
            )
        )
        assert isinstance(result, WriteResult)


class TestAuditHookContext:
    """`WriteQueue.submit()`'s `contextvars.Context` snapshot (#305, `queue.py`'s
    module docstring): the audit hooks it runs must see the *submitting* request's
    own `compat.select` resolved profile and `observability.logging` request id,
    not whatever happens to be ambient on the consumer's own long-lived task.
    """

    async def test_audit_hook_sees_the_submitters_profile_and_request_id(
        self, queue: WriteQueue
    ) -> None:
        seen: list[tuple[str, str | None]] = []

        async def audit_hook(
            request: WriteRequest, result: WriteResult | None, error: Exception | None
        ) -> None:
            seen.append((current_resolved_profile(), current_request_id()))

        queue.add_audit_hook(audit_hook)

        profile_token = set_resolved_profile("claude-code")
        request_id_token = request_id_var.set("req-1")
        try:
            await queue.submit(
                WriteRequest(
                    op="write",
                    path="personal/fact/a.md",
                    client="human",
                    if_version="new",
                    content=_note_bytes(),
                )
            )
        finally:
            reset_resolved_profile(profile_token)
            request_id_var.reset(request_id_token)

        assert seen == [("claude-code", "req-1")]

    async def test_submit_with_no_resolved_profile_in_scope_audits_as_default(
        self, queue: WriteQueue
    ) -> None:
        seen: list[str] = []

        async def audit_hook(
            request: WriteRequest, result: WriteResult | None, error: Exception | None
        ) -> None:
            seen.append(current_resolved_profile())

        queue.add_audit_hook(audit_hook)

        await queue.submit(
            WriteRequest(
                op="write",
                path="personal/fact/a.md",
                client="human",
                if_version="new",
                content=_note_bytes(),
            )
        )

        assert seen == ["default"]

    async def test_concurrent_submits_with_different_profiles_keep_their_own(
        self, queue: WriteQueue
    ) -> None:
        seen: dict[str, str] = {}

        async def audit_hook(
            request: WriteRequest, result: WriteResult | None, error: Exception | None
        ) -> None:
            seen[request.path] = current_resolved_profile()

        queue.add_audit_hook(audit_hook)

        async def submit_under(profile: str, path: str, title: str) -> None:
            token = set_resolved_profile(profile)
            try:
                await queue.submit(
                    WriteRequest(
                        op="write",
                        path=path,
                        client="human",
                        if_version="new",
                        content=_note_bytes(id=new_ulid(_CREATED), title=title),
                    )
                )
            finally:
                reset_resolved_profile(token)

        await asyncio.gather(
            submit_under("claude-code", "personal/fact/a.md", "A"),
            submit_under("default", "personal/fact/b.md", "B"),
        )

        assert seen == {
            "personal/fact/a.md": "claude-code",
            "personal/fact/b.md": "default",
        }

    async def test_a_profile_set_inside_a_hook_does_not_leak_to_the_next_write(
        self, queue: WriteQueue
    ) -> None:
        seen: list[str] = []

        async def leaky_hook(
            request: WriteRequest, result: WriteResult | None, error: Exception | None
        ) -> None:
            seen.append(current_resolved_profile())
            # Deliberately never reset - proves the queue's own context snapshot,
            # not hook discipline, is what keeps this from leaking into the next
            # write's audit hook run.
            set_resolved_profile("claude-code")

        queue.add_audit_hook(leaky_hook)

        await queue.submit(
            WriteRequest(
                op="write",
                path="personal/fact/a.md",
                client="human",
                if_version="new",
                content=_note_bytes(),
            )
        )
        await queue.submit(
            WriteRequest(
                op="write",
                path="personal/fact/b.md",
                client="human",
                if_version="new",
                content=_note_bytes(id=new_ulid(_CREATED), title="B"),
            )
        )

        assert seen == ["default", "default"]


class TestSync:
    """`WriteQueue.sync()` and `add_sync_hook` (#33): every working-copy sync - a
    standalone `sync()` job or the pre-write sync inside `submit()` - goes through
    the one consumer, and hands a non-empty result to every sync hook exactly once.
    """

    async def test_sync_returns_the_change_set_and_notifies_the_hook(
        self, queue: WriteQueue, bare_remote: Path
    ) -> None:
        human_commit(bare_remote, "personal/fact/a.md", b"first\n")

        received: list[ChangeSet] = []

        async def sync_hook(change_set: ChangeSet) -> None:
            received.append(change_set)

        queue.add_sync_hook(sync_hook)

        change_set = await queue.sync()

        assert change_set.added == ("personal/fact/a.md",)
        assert received == [change_set]

    async def test_an_empty_sync_does_not_call_the_hook(self, queue: WriteQueue) -> None:
        received: list[ChangeSet] = []

        async def sync_hook(change_set: ChangeSet) -> None:
            received.append(change_set)

        queue.add_sync_hook(sync_hook)

        change_set = await queue.sync()

        assert change_set.empty
        assert received == []

    async def test_a_human_commit_ahead_of_a_write_reaches_the_sync_hook_too(
        self, queue: WriteQueue, bare_remote: Path
    ) -> None:
        """The bug this guards against (#33): a write's own pre-write sync fast-forwards
        whatever a human pushed in the meantime, even on a path the write never touches -
        that change set used to go nowhere. The sync hook must see it, not just the
        write's own commit hook (`add_hook`, which only ever hears about this write's
        own `changed_paths`).
        """
        human_commit(bare_remote, "personal/fact/human.md", b"from a human\n")

        received: list[ChangeSet] = []

        async def sync_hook(change_set: ChangeSet) -> None:
            received.append(change_set)

        queue.add_sync_hook(sync_hook)

        await queue.submit(
            WriteRequest(
                op="write",
                path="personal/fact/a.md",
                client="human",
                if_version="new",
                content=_note_bytes(),
            )
        )

        assert len(received) == 1
        assert received[0].added == ("personal/fact/human.md",)

    async def test_concurrent_sync_calls_coalesce(self, queue: WriteQueue) -> None:
        """Calling `sync()` several times at once while the first is still queued
        (not yet picked up by the consumer) must result in exactly one real
        `repo.sync()` - everyone else shares its result rather than each enqueuing a
        redundant sync.
        """
        calls = {"count": 0}
        original_sync = queue._repo.sync

        def counting_sync() -> ChangeSet:
            calls["count"] += 1
            return original_sync()

        queue._repo.sync = counting_sync  # type: ignore[method-assign]

        first, second, third = await asyncio.gather(queue.sync(), queue.sync(), queue.sync())

        assert calls["count"] == 1
        assert first is second is third

    async def test_concurrent_sync_during_a_slow_write_is_serialized(
        self, vault_config: VaultConfig, bare_remote: Path
    ) -> None:
        """A `sync()` call concurrent with an in-flight write must wait for that
        write's own commit/push to finish before it runs its own `repo.sync()` -
        before the fix, the two could run concurrently on the same working copy,
        reproducibly hitting a `merge --ff-only` TOCTOU git error.
        """
        repo = Repo(vault_config)
        write_queue = WriteQueue(repo)
        await write_queue.start()
        try:
            commit_started = threading.Event()
            release_commit = threading.Event()
            order: list[str] = []
            original_commit_file = repo.commit_file

            def slow_commit_file(*args: object, **kwargs: object) -> str:
                order.append("write:commit_start")
                commit_started.set()
                if not release_commit.wait(timeout=5):
                    raise AssertionError("release_commit was never set")
                order.append("write:commit_end")
                return original_commit_file(*args, **kwargs)  # type: ignore[arg-type]

            repo.commit_file = slow_commit_file  # type: ignore[method-assign]

            async def do_write() -> None:
                await write_queue.submit(
                    WriteRequest(
                        op="write",
                        path="personal/fact/a.md",
                        client="human",
                        if_version="new",
                        content=_note_bytes(),
                    )
                )
                order.append("write:done")

            async def do_sync() -> None:
                order.append("sync:call")
                await write_queue.sync()
                order.append("sync:done")

            write_task = asyncio.create_task(do_write())
            while not commit_started.is_set():
                await asyncio.sleep(0.01)

            sync_task = asyncio.create_task(do_sync())
            # Give `sync()` a chance to actually enqueue its job (the `await
            # self._queue.put(...)` inside it) before the write's commit is
            # released - otherwise this would not prove the sync job was still
            # queued behind the in-flight write when the write finished.
            await asyncio.sleep(0.05)
            release_commit.set()

            await asyncio.wait_for(asyncio.gather(write_task, sync_task), timeout=5)

            assert order.index("write:commit_start") < order.index("sync:call")
            assert order.index("write:commit_end") < order.index("sync:done")
            assert order.index("write:done") < order.index("sync:done")

            # Neither task raised (no GitError/SyncDiverged from a racing
            # `merge --ff-only`), and the clone is left clean: a fresh sync
            # afterwards is a true no-op, not a sign of a divergence nobody
            # reported.
            final = await write_queue.sync()
            assert final.empty
        finally:
            await write_queue.stop()
