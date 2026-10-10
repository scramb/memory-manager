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

A push rejected because the remote moved (a human or another writer pushed
in between `sync()` and `push()`) is retried through a rebase (#15), but
only when the remote's new commits left every path this write touches
alone: a clean rebase does a real 3-way merge, so if the remote touched the
exact note being written - on different lines, so there is no textual
conflict - the rebase would otherwise report "clean" even though
`if_version` no longer matches the remote (#16). That case, and an actual
rebase conflict, are handled the same way: the rebase (if one happened) is
aborted, the local commit discarded (`reset_to_remote()`), and both
versions are written to `<path>.conflict.md` for a human to resolve - the
write comes back as `WriteConflict`, never silently lost. A push that keeps
getting rejected even after a clean, unrelated rebase (the remote keeps
moving) gives up after 3 attempts as `WriteFailed`. `add_hook()` is the
seam the indexer subscribes through, on success only; `add_audit_hook()`
(#39) is the separate seam for the audit log in Postgres, which needs
every outcome, not just success - see `AuditHook`'s docstring.

`sync()` (#33) is the other way a caller reaches the working copy: a
sync-only job, queued through the same consumer as every write, so the
poll loop's timer tick, the vault webhook's on-demand sync, and the
pre-write sync inside every `submit()` never run `repo.sync()`/commit/
push/rebase/reset concurrently on the same clone - the single-writer rule
`docs/PLAN.md` describes covers every working-copy operation, not just
writes. `add_sync_hook()` is called with every non-empty `ChangeSet` the
consumer produces this way, including the pre-write sync inside
`submit()` - a human's change reaches a subscriber (the indexer) the
first time any of those three paths happens to pick it up, not only when
the poll loop's own timer does.

`submit()` also snapshots the caller's `contextvars.Context` (`_WriteJob.context`)
and replays it for exactly one thing: the audit hooks run by `_run_write_job` (#305).
The consumer is one long-lived background task with no connection to any particular
request otherwise, so without this, `AuditHook`s would see whatever happens to be
ambient on *that* task (nothing, usually) instead of the submitting request's own
`compat.select` resolved profile and `observability.logging` request id. Every other
hook - the indexer's `WriteHook`, every `SyncHook`, `_process` itself - keeps running
in the consumer's own ambient context: a pre-write sync picking up a human's change
must never get attributed to the request that happened to trigger it.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import NoReturn

from memory_manager.observability.metrics import QUEUE_DEPTH, record_queue_write
from memory_manager.storage import rules
from memory_manager.storage.base import (
    AuditHook,
    BlocklistRejected,
    EditMismatch,
    InvalidNote,
    NotFound,
    Op,
    SecretRejected,
    VersionConflict,
    WriteConflict,
    WriteError,
    WriteFailed,
    WriteRequest,
    WriteResult,
)
from memory_manager.vault.git import GitError, PushRejected
from memory_manager.vault.note import version
from memory_manager.vault.paths import PathRejected, conflict_path, parse_note_path
from memory_manager.vault.repo import Repo, author_for
from memory_manager.vault.sync import ChangeSet

__all__ = [
    "AuditHook",
    "BlocklistRejected",
    "EditMismatch",
    "InvalidNote",
    "NotFound",
    "Op",
    "SecretRejected",
    "SyncHook",
    "VersionConflict",
    "WriteConflict",
    "WriteError",
    "WriteFailed",
    "WriteHook",
    "WriteQueue",
    "WriteRequest",
    "WriteResult",
]

_logger = logging.getLogger(__name__)

_MAX_PUSH_ATTEMPTS = 3


WriteHook = Callable[["WriteResult", "WriteRequest", tuple[str, ...]], Awaitable[None]]
SyncHook = Callable[[ChangeSet], Awaitable[None]]
#: `AuditHook` moved to `storage.base` (WP-18) so `storage.postgres.PostgresBackend`
#: can share it without importing this module - re-exported here unchanged, see
#: that module's docstring for the full contract.


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class _SyncJob:
    """A queued sync-only job: no write, just `repo.sync()` plus the sync hooks (#33).

    Shares the consumer's single `asyncio.Queue` with every `(WriteRequest,
    Future)` write job (`WriteQueue._queue`'s other item shape) - the thing
    that makes `sync()` and `submit()` never race each other onto the same
    working copy.
    """

    future: asyncio.Future[ChangeSet]


@dataclass(frozen=True)
class _WriteJob:
    """A queued write: the request, the submitter's future, and its context snapshot.

    `context` is `contextvars.copy_context()`, taken inside `submit()` before the
    request is queued - the submitting request's own `contextvars` (`compat.select`'s
    resolved profile, `observability.logging`'s request id) at the moment of
    submission, frozen so the consumer's own long-lived task (with none of that
    ambient otherwise) can replay it later. Used for exactly one thing:
    `_run_write_job` runs the audit hooks under it (`_run_audit_hooks_in_context`,
    module docstring) - never `_process` itself, the indexer's `WriteHook`, or any
    `SyncHook`, which all keep running in the consumer's own ambient context.
    """

    request: WriteRequest
    future: asyncio.Future[WriteResult]
    context: contextvars.Context


_QueueItem = _WriteJob | _SyncJob


class WriteQueue:
    """Serializes every vault write - and every sync - through one background consumer.

    `start()` clones the vault (if needed) and starts the consumer task;
    `submit()` and `sync()` are the only two ways in - both are safe to call
    from any number of concurrent callers, who all get served one at a
    time, in submission order, on the same consumer task. No `repo`
    operation (`sync`/commit/push/rebase/reset) ever runs anywhere else.
    """

    def __init__(self, repo: Repo, *, clock: Callable[[], datetime] = _utc_now) -> None:
        self._repo = repo
        self._clock = clock
        self._queue: asyncio.Queue[_QueueItem] = asyncio.Queue()
        self._consumer_task: asyncio.Task[None] | None = None
        self._hooks: list[WriteHook] = []
        self._audit_hooks: list[AuditHook] = []
        self._sync_hooks: list[SyncHook] = []
        self._pending_sync: asyncio.Future[ChangeSet] | None = None

    def add_hook(self, hook: WriteHook) -> None:
        """Register `hook` to be awaited after every successful write.

        A hook that raises is logged and otherwise ignored - it never fails
        the write it was notified about.
        """
        self._hooks.append(hook)

    def add_audit_hook(self, hook: AuditHook) -> None:
        """Register `hook` to be awaited after every processed write, ok or not (#39).

        Unlike `add_hook`, this fires for a rejected/conflicting/failed
        write too - see `AuditHook`'s docstring. Same failure contract: a
        raising hook is logged, never propagated to the write's own caller.
        """
        self._audit_hooks.append(hook)

    def add_sync_hook(self, hook: SyncHook) -> None:
        """Register `hook` to be awaited after every sync that found a change (#33).

        Covers every sync the consumer ever runs: a standalone `sync()`
        job (the poll loop, the vault webhook's `trigger_sync()`) and the
        pre-write sync inside every `submit()` alike - a human's change
        reaches `hook` exactly once, through whichever of those happens to
        pick it up first, never zero times and never twice. Same
        failure contract as `add_hook`: a raising hook is logged, not
        propagated.
        """
        self._sync_hooks.append(hook)

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

        Snapshots this call's `contextvars.Context` into the queued `_WriteJob`
        (module docstring, #305) - taken here, in the caller's own task, since the
        consumer that eventually processes this job runs on its own long-lived task
        with no access to it otherwise.
        """
        future: asyncio.Future[WriteResult] = asyncio.get_running_loop().create_future()
        await self._queue.put(
            _WriteJob(request=request, future=future, context=contextvars.copy_context())
        )
        QUEUE_DEPTH.set(self._queue.qsize())
        return await future

    async def sync(self) -> ChangeSet:
        """Queue a sync-only job and wait for the consumer to run it (#33).

        Goes through the same consumer every write does, so this never
        races a write's own pre-write `repo.sync()`/commit/push/rebase/reset
        - the single-writer rule the write queue exists for
        (`docs/PLAN.md`) covers every `repo` operation, not just writes.
        Any non-empty result is already handed to every `add_sync_hook`
        subscriber by the time this returns.

        Concurrent calls while one sync job is still waiting in the queue
        (not yet picked up by the consumer) share its result instead of
        each enqueuing a redundant sync.
        """
        pending = self._pending_sync
        if pending is not None and not pending.done():
            return await pending

        future: asyncio.Future[ChangeSet] = asyncio.get_running_loop().create_future()
        self._pending_sync = future
        await self._queue.put(_SyncJob(future=future))
        QUEUE_DEPTH.set(self._queue.qsize())
        return await future

    async def _consume(self) -> None:
        while True:
            item = await self._queue.get()
            QUEUE_DEPTH.set(self._queue.qsize())
            if isinstance(item, _SyncJob):
                await self._run_sync_job(item)
            else:
                await self._run_write_job(item)

    async def _run_write_job(self, job: _WriteJob) -> None:
        request, future = job.request, job.future
        try:
            result = await self._process(request)
        except Exception as exc:  # routed to the submitter, not raised here
            # Audit hooks run *before* the future resolves - same ordering
            # `_process`'s own `_run_hooks` (the indexer) already uses for a
            # success. Otherwise `submit()`'s caller could resume and query
            # the audit log before this write's own row exists in it (seen
            # as a real, flaky race once this ran with a real Postgres: a
            # `fetchrow` landing on the *previous* write's row, or none at
            # all, because this one's `INSERT` was still in flight).
            await self._run_audit_hooks_in_context(job.context, request, None, exc)
            if not future.done():
                future.set_exception(exc)
            record_queue_write(request.op, "error")
        else:
            await self._run_audit_hooks_in_context(job.context, request, result, None)
            if not future.done():
                future.set_result(result)
            record_queue_write(request.op, "ok")

    async def _run_sync_job(self, job: _SyncJob) -> None:
        # A later concurrent `sync()` call must enqueue its own fresh job
        # once this one has actually been picked up - only clear the slot
        # if nobody else already replaced it (which `sync()` itself never
        # does while this job is still pending, but a future instance
        # check is cheap insurance against ever reading a reused slot).
        if self._pending_sync is job.future:
            self._pending_sync = None
        try:
            change_set = await self._sync_and_notify()
        except Exception as exc:  # routed to the caller, not raised here
            if not job.future.done():
                job.future.set_exception(exc)
        else:
            if not job.future.done():
                job.future.set_result(change_set)

    async def _sync_and_notify(self) -> ChangeSet:
        """`repo.sync()`, then hand any non-empty result to every sync hook.

        The one place every working-copy sync goes through: the standalone
        `sync()` job and the pre-write sync inside `_process` both call
        this, so a sync hook (the indexer) sees a given human change
        exactly once no matter which path picked it up.
        """
        change_set = await asyncio.to_thread(self._repo.sync)
        if not change_set.empty:
            await self._run_sync_hooks(change_set)
        return change_set

    async def _run_sync_hooks(self, change_set: ChangeSet) -> None:
        for hook in self._sync_hooks:
            try:
                await hook(change_set)
            except Exception:
                _logger.exception("write queue sync hook failed")

    async def _process(self, request: WriteRequest) -> WriteResult:
        try:
            await self._sync_and_notify()
        except GitError as exc:
            raise WriteFailed(str(exc)) from exc

        # The remote tip our commit is about to be built on - `_push` needs
        # this to tell "the remote moved, but not on our path" from "the
        # remote moved our exact note" after a rejected push (#16).
        base = await asyncio.to_thread(self._repo.head_or_none)

        try:
            current = await asyncio.to_thread(self._repo.read_file, request.path)
        except PathRejected as exc:
            raise InvalidNote(request.path, str(exc)) from exc

        current_version = version(current) if current is not None else None
        rules.check_version(request, current_version, current)

        if request.op == "archive":
            result, changed_paths = await self._do_archive(request, current)
        elif request.op == "supersede":
            result, changed_paths = await self._do_supersede(request, current)
        elif request.op == "promote":
            result, changed_paths = await self._do_promote(request, current)
        else:
            result, changed_paths = await self._do_write_or_edit(request, current)

        final_commit = await self._push(request, changed_paths, base)
        if final_commit != result.commit:
            # A rebase retry rewrote our commit onto the remote's new tip,
            # so the sha captured at commit time no longer exists on the
            # pushed history - report the one that does.
            result = replace(result, commit=final_commit)
        await self._run_hooks(result, request, changed_paths)
        return result

    async def _do_write_or_edit(
        self, request: WriteRequest, current: bytes | None
    ) -> tuple[WriteResult, tuple[str, ...]]:
        final_bytes = rules.prepare_write_or_edit(
            request.op, request.path, request.content, request.old_str, request.new_str, current
        )

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

        note_path = rules.parse_note_path_or_raise(request.path)
        archive_rel = note_path.archive_path().relative

        try:
            already_archived = await asyncio.to_thread(self._repo.read_file, archive_rel)
        except PathRejected as exc:
            raise InvalidNote(request.path, str(exc)) from exc

        now = self._clock().astimezone(UTC).replace(microsecond=0)
        archived_bytes = rules.prepare_archive(
            request.path, current, archive_exists=already_archived is not None, now=now
        )

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

    async def _do_supersede(
        self, request: WriteRequest, current: bytes | None
    ) -> tuple[WriteResult, tuple[str, ...]]:
        old_note_path, new_note_path, new_path, content, current_bytes = (
            rules.prepare_supersede_paths(request.path, request.new_path, request.content, current)
        )

        try:
            new_target = await asyncio.to_thread(self._repo.read_file, new_path)
        except PathRejected as exc:
            raise InvalidNote(new_path, str(exc)) from exc

        now = self._clock().astimezone(UTC).replace(microsecond=0)
        new_final_bytes, old_final_bytes = rules.prepare_supersede_content(
            request.path,
            new_path,
            content,
            current_bytes,
            old_note_path=old_note_path,
            new_note_path=new_note_path,
            new_target_exists=new_target is not None,
            now=now,
        )

        message = request.message or f"supersede {request.path} with {new_path}"
        author = author_for(request.client)
        try:
            commit_sha = await asyncio.to_thread(
                self._repo.commit_files,
                {request.path: old_final_bytes, new_path: new_final_bytes},
                author,
                message,
            )
        except GitError as exc:
            raise WriteFailed(str(exc)) from exc

        result = WriteResult(
            path=new_path,
            version=version(new_final_bytes),
            commit=commit_sha,
            related={request.path: version(old_final_bytes)},
        )
        return result, (request.path, new_path)

    async def _do_promote(
        self, request: WriteRequest, current: bytes | None
    ) -> tuple[WriteResult, tuple[str, ...]]:
        source_note_path, target_note_path, target_path, current_bytes = (
            rules.prepare_promote_paths(request.path, request.target_namespace, current)
        )

        try:
            target_current = await asyncio.to_thread(self._repo.read_file, target_path)
        except PathRejected as exc:
            raise InvalidNote(target_path, str(exc)) from exc

        archive_rel = source_note_path.archive_path().relative
        archive_exists = False
        if not request.keep_original:
            try:
                already_archived = await asyncio.to_thread(self._repo.read_file, archive_rel)
            except PathRejected as exc:
                raise InvalidNote(request.path, str(exc)) from exc
            archive_exists = already_archived is not None

        now = self._clock().astimezone(UTC).replace(microsecond=0)
        new_bytes, archived_bytes = rules.prepare_promote_content(
            request.path,
            target_path,
            current_bytes,
            target_note_path=target_note_path,
            target_exists=target_current is not None,
            archive_exists=archive_exists,
            keep_original=request.keep_original,
            now=now,
        )

        message = request.message or f"promote {request.path} to {target_path}"
        author = author_for(request.client)
        archive_move = (
            (request.path, archive_rel, archived_bytes) if archived_bytes is not None else None
        )
        try:
            commit_sha = await asyncio.to_thread(
                self._repo.promote_files, target_path, new_bytes, archive_move, author, message
            )
        except GitError as exc:
            raise WriteFailed(str(exc)) from exc

        if archived_bytes is None:
            related = {request.path: version(current_bytes)}
            changed_paths: tuple[str, ...] = (request.path, target_path)
        else:
            related = {archive_rel: version(archived_bytes)}
            changed_paths = (request.path, archive_rel, target_path)

        result = WriteResult(
            path=target_path, version=version(new_bytes), commit=commit_sha, related=related
        )
        return result, changed_paths

    async def _push(
        self, request: WriteRequest, changed_paths: tuple[str, ...], base: str | None
    ) -> str:
        """Push the queued commit(s), retrying a rejection through a rebase.

        A rejected push's rebase is only trusted when the remote's new
        commits left every path in `changed_paths` alone. A clean
        `rebase_onto_remote()` is not enough on its own: git's rebase does
        a real 3-way merge, so if the remote's rejecting commit touched the
        exact note this write is committing - on different lines, so there
        is no textual conflict - the rebase still reports "clean" even
        though the write's `if_version` no longer matches what is on the
        remote (#16). That case is treated exactly like a rebase conflict:
        `_raise_conflict` discards our commit and writes `<path>.conflict.md`
        instead of silently reporting success for content nobody actually
        checked. Only when the remote's new commits changed other paths do
        we rebase onto them and retry.

        Returns the commit sha that ended up on the remote - identical to
        the sha the caller committed with unless a rebase retry rewrote it
        onto the remote's new tip, in which case the caller's `WriteResult`
        must report this sha instead of the now-dangling original one.
        """
        try:
            for _ in range(_MAX_PUSH_ATTEMPTS):
                try:
                    await asyncio.to_thread(self._repo.push)
                    return await asyncio.to_thread(self._repo.head)
                except PushRejected:
                    new_remote_head = await asyncio.to_thread(self._repo.fetch)
                    touched = await self._touched_path(base, new_remote_head, changed_paths)
                    if touched is not None:
                        await self._raise_conflict(request, touched)
                    # Rebase onto exactly the remote tip just checked above -
                    # not `rebase_onto_remote()`, which would fetch again and
                    # could silently rebase onto a newer tip nobody checked
                    # `changed_paths` against.
                    rebased = await asyncio.to_thread(self._repo.rebase_onto, new_remote_head)
                    if not rebased:
                        await self._raise_conflict(request, changed_paths[-1])
                    base = new_remote_head
        except GitError as exc:
            await asyncio.to_thread(self._repo.reset_to_remote)
            raise WriteFailed(str(exc)) from exc

        await asyncio.to_thread(self._repo.reset_to_remote)
        raise WriteFailed("remote keeps moving, retry")

    async def _touched_path(
        self, old_rev: str | None, new_rev: str, changed_paths: tuple[str, ...]
    ) -> str | None:
        """The first of `changed_paths` the remote touched between the two revs, if any.

        For `write`/`edit`/`archive`, `changed_paths` names one note (archive's two
        paths both map to the same `conflict_path`), so which one is returned does
        not matter. `supersede`'s two paths are two different notes: returning the
        specific one that actually moved on the remote is what makes
        `_raise_conflict` write the conflict file next to the right note instead of
        always the new one.
        """
        for path in changed_paths:
            if await asyncio.to_thread(self._repo.changed_between, old_rev, new_rev, path):
                return path
        return None

    async def _raise_conflict(self, request: WriteRequest, path: str) -> NoReturn:
        """The rebase onto the remote conflicted: give up on this write.

        Captures what we tried to commit (`ours`, still on the local HEAD
        that is about to be discarded), resets the clone to the remote,
        then writes `<path>.conflict.md` with both versions and raises
        `WriteConflict`. Reads `theirs`/`theirs_sha` only after the reset,
        so they describe the remote exactly as the conflict file reports it.
        """
        ours = await asyncio.to_thread(self._repo.read_file, path)
        await asyncio.to_thread(self._repo.reset_to_remote)
        theirs = await asyncio.to_thread(self._repo.read_remote_file, path)
        theirs_sha = await asyncio.to_thread(self._repo.remote_head)

        note_path = parse_note_path(path, allow_archive=True)
        conflict_rel = conflict_path(note_path)
        content = _render_conflict_file(
            path, request.client, self._clock(), ours, theirs, theirs_sha
        )
        message = f"conflict {conflict_rel}"

        try:
            await asyncio.to_thread(self._repo.commit_internal_file, conflict_rel, content, message)
            await asyncio.to_thread(self._repo.push)
        except PushRejected:
            rebased = await asyncio.to_thread(self._repo.rebase_onto_remote)
            pushed = False
            if rebased:
                with contextlib.suppress(GitError):
                    await asyncio.to_thread(self._repo.push)
                    pushed = True
            if not pushed:
                # Either the rebase itself conflicted, or it was clean but
                # the retried push was rejected again (the remote moved once
                # more while we were writing the conflict file) - either
                # way the conflict-file commit never made it, so the clone
                # must not be left carrying it (`reset_to_remote` is the
                # only path back to "no unpushed commit").
                await asyncio.to_thread(self._repo.reset_to_remote)
        except GitError:
            await asyncio.to_thread(self._repo.reset_to_remote)

        raise WriteConflict(
            path=path,
            conflict_path=conflict_rel,
            current_version=version(theirs) if theirs is not None else None,
            current_content=rules.decode_for_conflict(theirs),
        )

    async def _run_hooks(
        self, result: WriteResult, request: WriteRequest, changed_paths: tuple[str, ...]
    ) -> None:
        for hook in self._hooks:
            try:
                await hook(result, request, changed_paths)
            except Exception:
                _logger.exception("write queue hook failed for %s", request.path)

    async def _run_audit_hooks_in_context(
        self,
        context: contextvars.Context,
        request: WriteRequest,
        result: WriteResult | None,
        error: Exception | None,
    ) -> None:
        """`_run_audit_hooks`, replayed under the submitter's own `context` (#305).

        Runs as a child task of the consumer's own task, built with
        `context=context` so it (and everything it awaits) sees exactly the
        `contextvars` that were ambient in `submit()`'s caller, not the consumer's
        own - then awaited in place, so ordering stays what it already was (the
        future still only resolves after this returns) and cancelling the
        consumer task (`stop()`) still cancels this too, the same as it already
        cancelled whatever `_run_audit_hooks` itself was awaiting.
        """
        task = asyncio.create_task(self._run_audit_hooks(request, result, error), context=context)
        await task

    async def _run_audit_hooks(
        self, request: WriteRequest, result: WriteResult | None, error: Exception | None
    ) -> None:
        for hook in self._audit_hooks:
            try:
                await hook(request, result, error)
            except Exception:
                _logger.exception("write queue audit hook failed for %s", request.path)


def _render_conflict_file(
    path: str,
    client: str,
    at: datetime,
    ours: bytes | None,
    theirs: bytes | None,
    theirs_sha: str,
) -> bytes:
    """The content of `<path>.conflict.md` (ADR-0005 "Conflict files").

    Plain Markdown, not a note: no frontmatter, holds both versions in
    fenced code blocks. The fence is chosen longer than any backtick run
    already inside either version, so it can never be closed early by the
    content it wraps.
    """
    ours_decoded = rules.decode_for_conflict(ours)
    theirs_decoded = rules.decode_for_conflict(theirs)
    ours_text = ours_decoded if ours_decoded is not None else "(deleted locally)"
    theirs_text = theirs_decoded if theirs_decoded is not None else "(deleted on the remote)"
    fence = _conflict_fence(ours_text, theirs_text)
    timestamp = at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    return (
        f"# Write conflict: {path}\n"
        "\n"
        f"A write by {client} at {timestamp} could not be applied because the note "
        "was changed on the remote at the same time. Nothing was overwritten. "
        "Resolve by editing the note, then delete this file.\n"
        "\n"
        f"## Remote version ({theirs_sha[:7]})\n"
        "\n"
        f"{fence}markdown\n"
        f"{theirs_text}\n"
        f"{fence}\n"
        "\n"
        "## Rejected write\n"
        "\n"
        f"{fence}markdown\n"
        f"{ours_text}\n"
        f"{fence}\n"
    ).encode()


def _conflict_fence(*texts: str) -> str:
    longest_run = 0
    for text in texts:
        for run in re.findall(r"`+", text):
            longest_run = max(longest_run, len(run))
    return "`" * max(3, longest_run + 1)
