# SPDX-License-Identifier: AGPL-3.0-only
"""The write queue: one consumer, every vault write serialized through it.

`WriteQueue` is the single writer onto the vault's working copy
(`docs/PLAN.md#data-flow-write`, steps 3-6): callers `submit()` a
`WriteRequest` and await the matching `WriteResult`; a single background
task takes requests off an `asyncio.Queue` one at a time, so two writes
never race on the same clone.

Every request carries `if_version` (the sha256 from `vault.note.version`,
or the literal `"new"` for a file that must not exist yet) and is checked
against the *current* content after a fresh `repo.sync()` - never against a
value the caller computed earlier. A stale version never overwrites
silently (CLAUDE.md): it comes back as `VersionConflict`, carrying the
current content and version so the caller can retry.

Push-conflict rebase and `*.conflict.md` files are #15; for now a rejected
push (a human pushed in between `sync()` and `push()`) surfaces as
`WriteFailed` and leaves cleaning up the local clone to the next `sync()`.
Audit log persistence lives in Postgres from M3; `add_hook()` is the seam
an indexer/audit log subscribes through until then.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Literal

from memory_manager.vault.git import GitError, PushRejected
from memory_manager.vault.note import NoteFormatError, parse, serialize, version
from memory_manager.vault.paths import PathRejected, parse_note_path
from memory_manager.vault.repo import Repo, author_for
from memory_manager.vault.secrets import SecretFound, check
from memory_manager.vault.validate import NoteInvalid, validate_bytes

__all__ = [
    "EditMismatch",
    "InvalidNote",
    "NotFound",
    "Op",
    "SecretRejected",
    "VersionConflict",
    "WriteError",
    "WriteFailed",
    "WriteHook",
    "WriteQueue",
    "WriteRequest",
    "WriteResult",
]

_logger = logging.getLogger(__name__)

_NEW = "new"

Op = Literal["write", "edit", "archive"]


@dataclass(frozen=True)
class WriteRequest:
    """One write submitted to the queue.

    `if_version` is either the sha256 `vault.note.version` of the content
    the caller last saw, or the literal `"new"` meaning `path` must not
    exist yet. `content` is used by `write`; `old_str`/`new_str` by `edit`;
    `archive` needs neither. `message` overrides the default commit
    message (`"<op> <path>"`).
    """

    op: Op
    path: str
    client: str
    if_version: str
    content: bytes | None = None
    old_str: str | None = None
    new_str: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class WriteResult:
    """What a successful write produced: where, at what version, in which commit."""

    path: str
    version: str
    commit: str


class WriteError(Exception):
    """Base class for every error `WriteQueue.submit` can raise.

    `to_dict()` is the shape an MCP tool result reports back to a client
    (#18/#19 wire this up); every subclass extends it with whatever extra
    fields its error carries.
    """

    def to_dict(self) -> dict[str, object]:
        return {"error": type(self).__name__, "message": str(self)}


class VersionConflict(WriteError):
    """`if_version` did not match the current content at `path`.

    `current_version`/`current_content` are `None` exactly when the file
    does not exist at all - the caller asked for a version check against a
    path that was never (or no longer) there.
    """

    def __init__(self, path: str, current_version: str | None, current_content: str | None) -> None:
        self.path = path
        self.current_version = current_version
        self.current_content = current_content
        if current_version is None:
            message = f"'{path}' does not exist, use if_version 'new' to create it"
        else:
            message = f"'{path}' has moved to version {current_version}, if_version is stale"
        super().__init__(message)

    def to_dict(self) -> dict[str, object]:
        result = super().to_dict()
        result["path"] = self.path
        result["current_version"] = self.current_version
        result["current_content"] = self.current_content
        return result


class NotFound(WriteError):
    """`path` does not exist, for an operation that requires it to (archive)."""

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(f"'{path}' does not exist")

    def to_dict(self) -> dict[str, object]:
        result = super().to_dict()
        result["path"] = self.path
        return result


class EditMismatch(WriteError):
    """An `edit`'s `old_str` occurred zero or more than one time."""

    def __init__(self, path: str, count: int) -> None:
        self.path = path
        self.count = count
        super().__init__(f"old_str occurs {count} times in '{path}', must occur exactly once")

    def to_dict(self) -> dict[str, object]:
        result = super().to_dict()
        result["path"] = self.path
        result["count"] = self.count
        return result


class InvalidNote(WriteError):
    """The note bytes a write would produce violate ADR-0005 or the path rules.

    Wraps the message of a `NoteFormatError`, `NoteInvalid` or `PathRejected`
    raised while validating a write, or a queue-level rule such as "id must
    not change" or "archive target exists".
    """

    def __init__(self, path: str, message: str) -> None:
        self.path = path
        super().__init__(f"'{path}' is invalid: {message}")

    def to_dict(self) -> dict[str, object]:
        result = super().to_dict()
        result["path"] = self.path
        return result


class SecretRejected(WriteError):
    """The note text a write would produce looks like it contains a secret."""

    def __init__(self, path: str, message: str) -> None:
        self.path = path
        super().__init__(f"'{path}' rejected: {message}")

    def to_dict(self) -> dict[str, object]:
        result = super().to_dict()
        result["path"] = self.path
        return result


class WriteFailed(WriteError):
    """A git operation failed (commit, or a push the remote rejected).

    #15 replaces the push-rejection case with a rebase; for now the caller
    just has to retry.
    """


WriteHook = Callable[["WriteResult", "WriteRequest", tuple[str, ...]], Awaitable[None]]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class WriteQueue:
    """Serializes every vault write through one background consumer.

    `start()` clones the vault (if needed) and starts the consumer task;
    `submit()` is the only way in - it is safe to call from any number of
    concurrent callers, who all get served one at a time, in submission
    order.
    """

    def __init__(self, repo: Repo, *, clock: Callable[[], datetime] = _utc_now) -> None:
        self._repo = repo
        self._clock = clock
        self._queue: asyncio.Queue[tuple[WriteRequest, asyncio.Future[WriteResult]]] = (
            asyncio.Queue()
        )
        self._consumer_task: asyncio.Task[None] | None = None
        self._hooks: list[WriteHook] = []

    def add_hook(self, hook: WriteHook) -> None:
        """Register `hook` to be awaited after every successful write.

        A hook that raises is logged and otherwise ignored - it never fails
        the write it was notified about.
        """
        self._hooks.append(hook)

    async def start(self) -> None:
        """Make sure the vault is cloned and start the consumer task."""
        await asyncio.to_thread(self._repo.ensure_clone)
        self._consumer_task = asyncio.create_task(self._consume())

    async def stop(self) -> None:
        """Cancel the consumer task and wait for it to finish."""
        task = self._consumer_task
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self._consumer_task = None

    async def submit(self, request: WriteRequest) -> WriteResult:
        """Queue `request` and wait for the consumer to process it.

        Raises whatever `WriteError` the write failed with; returns the
        `WriteResult` on success.
        """
        future: asyncio.Future[WriteResult] = asyncio.get_running_loop().create_future()
        await self._queue.put((request, future))
        return await future

    async def _consume(self) -> None:
        while True:
            request, future = await self._queue.get()
            try:
                result = await self._process(request)
            except Exception as exc:  # routed to the submitter, not raised here
                if not future.done():
                    future.set_exception(exc)
            else:
                if not future.done():
                    future.set_result(result)

    async def _process(self, request: WriteRequest) -> WriteResult:
        try:
            await asyncio.to_thread(self._repo.sync)
        except GitError as exc:
            raise WriteFailed(str(exc)) from exc

        try:
            current = await asyncio.to_thread(self._repo.read_file, request.path)
        except PathRejected as exc:
            raise InvalidNote(request.path, str(exc)) from exc

        current_version = version(current) if current is not None else None
        _check_version(request, current_version, current)

        if request.op == "archive":
            result, changed_paths = await self._do_archive(request, current)
        else:
            result, changed_paths = await self._do_write_or_edit(request, current)

        await self._push()
        await self._run_hooks(result, request, changed_paths)
        return result

    async def _do_write_or_edit(
        self, request: WriteRequest, current: bytes | None
    ) -> tuple[WriteResult, tuple[str, ...]]:
        new_bytes = _new_content(request, current)

        try:
            note_path = parse_note_path(request.path)
        except PathRejected as exc:
            raise InvalidNote(request.path, str(exc)) from exc

        try:
            parsed = validate_bytes(new_bytes, expected_type=note_path.type)
        except (NoteFormatError, NoteInvalid) as exc:
            raise InvalidNote(request.path, str(exc)) from exc

        if current is not None:
            try:
                current_note = parse(current)
            except NoteFormatError as exc:
                raise InvalidNote(request.path, str(exc)) from exc
            if current_note.id != parsed.id:
                raise InvalidNote(request.path, "id must not change")

        try:
            check(new_bytes.decode("utf-8"))
        except SecretFound as exc:
            raise SecretRejected(request.path, str(exc)) from exc

        canonical = serialize(parsed)
        final_bytes = new_bytes if new_bytes == canonical else canonical

        message = request.message or f"{request.op} {request.path}"
        author = author_for(request.client)
        try:
            commit_sha = await asyncio.to_thread(
                self._repo.commit_file, request.path, final_bytes, author, message
            )
        except GitError as exc:
            raise WriteFailed(str(exc)) from exc

        result = WriteResult(path=request.path, version=version(final_bytes), commit=commit_sha)
        return result, (request.path,)

    async def _do_archive(
        self, request: WriteRequest, current: bytes | None
    ) -> tuple[WriteResult, tuple[str, ...]]:
        if current is None:
            raise NotFound(request.path)

        try:
            note_path = parse_note_path(request.path)
        except PathRejected as exc:
            raise InvalidNote(request.path, str(exc)) from exc

        archive_rel = note_path.archive_path().relative

        try:
            already_archived = await asyncio.to_thread(self._repo.read_file, archive_rel)
        except PathRejected as exc:
            raise InvalidNote(request.path, str(exc)) from exc
        if already_archived is not None:
            raise InvalidNote(request.path, "archive target exists")

        try:
            parsed = parse(current)
        except NoteFormatError as exc:
            raise InvalidNote(request.path, str(exc)) from exc

        now = self._clock().astimezone(UTC).replace(microsecond=0)
        archived_note = replace(parsed, updated=now)
        archived_bytes = serialize(archived_note)

        message = request.message or f"archive {request.path}"
        author = author_for(request.client)
        try:
            commit_sha = await asyncio.to_thread(
                self._repo.move_file, request.path, archive_rel, archived_bytes, author, message
            )
        except GitError as exc:
            raise WriteFailed(str(exc)) from exc

        result = WriteResult(path=archive_rel, version=version(archived_bytes), commit=commit_sha)
        return result, (request.path, archive_rel)

    async def _push(self) -> None:
        try:
            await asyncio.to_thread(self._repo.push)
        except PushRejected as exc:
            raise WriteFailed("remote moved, retry") from exc
        except GitError as exc:
            raise WriteFailed(str(exc)) from exc

    async def _run_hooks(
        self, result: WriteResult, request: WriteRequest, changed_paths: tuple[str, ...]
    ) -> None:
        for hook in self._hooks:
            try:
                await hook(result, request, changed_paths)
            except Exception:
                _logger.exception("write queue hook failed for %s", request.path)


def _check_version(
    request: WriteRequest, current_version: str | None, current: bytes | None
) -> None:
    if request.if_version == _NEW:
        if current is not None:
            raise VersionConflict(request.path, current_version, _decode_for_conflict(current))
    elif current_version != request.if_version:
        raise VersionConflict(request.path, current_version, _decode_for_conflict(current))


def _decode_for_conflict(current: bytes | None) -> str | None:
    if current is None:
        return None
    return current.decode("utf-8", errors="replace")


def _new_content(request: WriteRequest, current: bytes | None) -> bytes:
    if request.op == "write":
        if request.content is None:
            raise InvalidNote(request.path, "write requires content")
        return request.content

    if request.old_str is None or request.new_str is None:
        raise InvalidNote(request.path, "edit requires old_str and new_str")
    current_text = current.decode("utf-8") if current is not None else ""
    count = current_text.count(request.old_str)
    if count != 1:
        raise EditMismatch(request.path, count)
    new_text = current_text.replace(request.old_str, request.new_str, 1)
    return new_text.encode("utf-8")
