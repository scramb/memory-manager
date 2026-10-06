# SPDX-License-Identifier: AGPL-3.0-only
"""The change set produced by a sync, and a loop that triggers one on a timer (#12, #33).

`ChangeSet` is what `Repo.sync()` returns: the note paths that changed
between the previous and the new local `HEAD`, classified as added,
modified or deleted, plus everything that changed but is not a note
(`ignored`) so a stray non-note file in the vault never breaks a caller.

`poll_loop` only decides *when* to sync, not what runs the sync or what
happens with its result: it calls the `sync` callable it is given on a
timer and keeps going even if a single call raises `GitError` (e.g. a
transient network failure), so a transient remote problem does not kill
the process. `sync` is `memory_manager.queue.WriteQueue.sync` in
production (#33) - the write queue's consumer is the one place every
working-copy operation runs, and its sync hooks are what deliver a
non-empty `ChangeSet` to a subscriber (the indexer) before `sync()` even
returns, so this loop needs no `on_change` callback of its own to pass
such a result anywhere.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from memory_manager.vault.git import GitError

__all__ = ["ChangeSet", "poll_loop"]

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChangeSet:
    """Note paths that changed between `old_head` and `new_head`.

    `old_head`/`new_head` are `None` only while the local clone has no
    commit yet (unborn `HEAD`). `ignored` holds paths that changed but are
    not a note per `vault.paths.parse_note_path` (e.g. a `README.md` in the
    vault) - they never reach `added`/`modified`/`deleted`.
    """

    old_head: str | None
    new_head: str | None
    added: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()
    ignored: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        """True if no note was added, modified or deleted.

        A change set with only `ignored` entries (e.g. a human editing a
        non-note file) counts as empty: nothing a caller needs to act on.
        """
        return not (self.added or self.modified or self.deleted)


async def poll_loop(
    sync: Callable[[], Awaitable[ChangeSet]],
    interval_seconds: float,
    *,
    jitter: float = 0.1,
    stop: asyncio.Event,
) -> None:
    """Call `sync()` on a timer until `stop` is set.

    `sync()`'s own caller-side effects (if any - `WriteQueue.sync()`'s
    sync hooks in production) have already run by the time it returns;
    this loop only triggers the call and otherwise ignores the result.
    Runs until `stop` is set, which it checks promptly: the wait between
    iterations is interruptible, not a plain `sleep`. A `GitError` from
    `sync()` (e.g. a transient network failure) is logged and the loop
    continues with the next interval rather than propagating.
    """
    while not stop.is_set():
        try:
            await sync()
        except GitError:
            _logger.exception("vault sync failed, retrying after the next interval")

        if stop.is_set():
            return

        delay = _jittered_delay(interval_seconds, jitter)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=delay)


def _jittered_delay(interval_seconds: float, jitter: float) -> float:
    spread = interval_seconds * jitter
    return max(0.0, interval_seconds + random.uniform(-spread, spread))  # noqa: S311
