# SPDX-License-Identifier: AGPL-3.0-only
"""The change set produced by a sync, and a loop that polls for them (#12).

`ChangeSet` is what `Repo.sync()` returns: the note paths that changed
between the previous and the new local `HEAD`, classified as added,
modified or deleted, plus everything that changed but is not a note
(`ignored`) so a stray non-note file in the vault never breaks a caller.

`poll_loop` is the scheduler around `Repo.sync()`: it runs on a timer,
hands non-empty change sets to `on_change`, and keeps going even if a
single sync fails (`GitError`) so a transient remote problem does not kill
the process. Pulling human changes is in scope here; what happens with a
change set (indexing, #26) is the caller's problem via `on_change`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from memory_manager.vault.git import GitError

if TYPE_CHECKING:
    from memory_manager.vault.repo import Repo

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
    repo: Repo,
    interval_seconds: float,
    on_change: Callable[[ChangeSet], Awaitable[None]],
    *,
    jitter: float = 0.1,
    stop: asyncio.Event,
) -> None:
    """Call `repo.sync()` on a timer and hand non-empty results to `on_change`.

    Runs until `stop` is set, which it checks promptly: the wait between
    iterations is interruptible, not a plain `sleep`. A `GitError` from
    `sync()` (e.g. a transient network failure) is logged and the loop
    continues with the next interval rather than propagating.
    """
    while not stop.is_set():
        try:
            change_set = await asyncio.to_thread(repo.sync)
        except GitError:
            _logger.exception("vault sync failed, retrying after the next interval")
        else:
            if not change_set.empty:
                await on_change(change_set)

        if stop.is_set():
            return

        delay = _jittered_delay(interval_seconds, jitter)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=delay)


def _jittered_delay(interval_seconds: float, jitter: float) -> float:
    spread = interval_seconds * jitter
    return max(0.0, interval_seconds + random.uniform(-spread, spread))  # noqa: S311
