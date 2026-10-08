# SPDX-License-Identifier: AGPL-3.0-only
"""The Git implementation of `StorageBackend` (ADR-0007 §1, default backend).

`GitBackend` wraps an already-started `WriteQueue` and the `Repo`/vault
root it was built from. Every write method builds the matching
`WriteRequest` and delegates to `queue.submit` - behaviour, including
every `WriteError` and its message, is unchanged from calling the queue
directly (#95 moves the MCP tools onto this interface without touching
`queue.py` itself). Starting and stopping the queue stays the caller's
job: this class never calls `queue.start()`/`queue.stop()`, so one running
queue could in principle back more than one `GitBackend` (not done today -
`mcp/server.py`'s `Services` builds exactly one of each).

`read`/`list` go straight to the working copy via `repo.read_file`, not
through the queue: they are informational reads, safe to run concurrently
with a write in flight. `changes_since` goes through `repo.diff_since`
instead, which only reads the fetched remote-tracking ref - see that
method's docstring for the same "briefly lag or lead a concurrent read"
caveat ADR-0007 §1 accepts for this.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from memory_manager.queue import WriteQueue
from memory_manager.storage.base import (
    ErasureResult,
    ErasureTargetKind,
    ErasureUnsupported,
    StorageChanges,
    StoredNote,
    WriteRequest,
    WriteResult,
)
from memory_manager.vault.note import version
from memory_manager.vault.paths import PathRejected, iter_md_files, parse_note_path
from memory_manager.vault.repo import Repo

__all__ = ["GitBackend"]

#: The cursor `changes_since` reports back when the vault's remote branch
#: does not exist yet (nothing has ever been pushed there) - a 40-char
#: all-zero string, shaped like a git sha but never one any repository can
#: produce, the same convention git's own wire protocol uses for "no ref".
_NO_REMOTE_CURSOR = "0" * 40


class GitBackend:
    """`StorageBackend` backed by a Git vault clone (ADR-0007 §1, the default)."""

    def __init__(self, queue: WriteQueue, repo: Repo, vault_root: Path) -> None:
        self._queue = queue
        self._repo = repo
        self._vault_root = vault_root

    async def read(self, path: str) -> StoredNote | None:
        """The note at `path`, or `None` if it does not exist.

        Raises `PathRejected` for a `path` that is not a safe, valid note
        path - the same check every write goes through.
        """
        data = await asyncio.to_thread(self._repo.read_file, path)
        if data is None:
            return None
        return StoredNote(path=path, content=data, version=version(data))

    async def list(self, *, include_archived: bool = False) -> list[StoredNote]:
        """Every note in the vault, in path order (`vault.paths.iter_md_files`).

        Filters to every `*.md` file shaped like `<namespace>/<type>/<slug>.md`
        (optionally `_archive/`-prefixed), skipping anything else silently -
        the one place that filter lives now that callers such as
        `mcp.server` read the vault through this interface instead of
        walking the working copy themselves. Archived notes are included
        only when `include_archived` is set.
        """
        entries: list[StoredNote] = []
        for file in iter_md_files(self._vault_root):
            rel = "/".join(file.relative_to(self._vault_root).parts)
            try:
                note_path = parse_note_path(rel, allow_archive=True)
            except PathRejected:
                continue
            if note_path.archived and not include_archived:
                continue
            data = await asyncio.to_thread(self._repo.read_file, rel)
            if data is None:
                continue
            entries.append(StoredNote(path=rel, content=data, version=version(data)))
        return entries

    async def write(
        self,
        path: str,
        content: bytes,
        *,
        if_version: str,
        client: str,
        actor: str = "stdio",
        message: str | None = None,
    ) -> WriteResult:
        return await self._queue.submit(
            WriteRequest(
                op="write",
                path=path,
                client=client,
                if_version=if_version,
                content=content,
                message=message,
                actor=actor,
            )
        )

    async def edit(
        self,
        path: str,
        old_str: str,
        new_str: str,
        *,
        if_version: str,
        client: str,
        actor: str = "stdio",
        message: str | None = None,
    ) -> WriteResult:
        return await self._queue.submit(
            WriteRequest(
                op="edit",
                path=path,
                client=client,
                if_version=if_version,
                old_str=old_str,
                new_str=new_str,
                message=message,
                actor=actor,
            )
        )

    async def supersede(
        self,
        path: str,
        new_path: str,
        content: bytes,
        *,
        if_version: str,
        client: str,
        actor: str = "stdio",
        message: str | None = None,
    ) -> WriteResult:
        return await self._queue.submit(
            WriteRequest(
                op="supersede",
                path=path,
                client=client,
                if_version=if_version,
                new_path=new_path,
                content=content,
                message=message,
                actor=actor,
            )
        )

    async def promote(
        self,
        path: str,
        target_namespace: str,
        *,
        if_version: str,
        keep_original: bool = False,
        client: str,
        actor: str = "stdio",
        message: str | None = None,
    ) -> WriteResult:
        return await self._queue.submit(
            WriteRequest(
                op="promote",
                path=path,
                client=client,
                if_version=if_version,
                target_namespace=target_namespace,
                keep_original=keep_original,
                message=message,
                actor=actor,
            )
        )

    async def archive(
        self,
        path: str,
        *,
        if_version: str,
        client: str,
        actor: str = "stdio",
        message: str | None = None,
    ) -> WriteResult:
        return await self._queue.submit(
            WriteRequest(
                op="archive",
                path=path,
                client=client,
                if_version=if_version,
                message=message,
                actor=actor,
            )
        )

    async def erase(
        self,
        target_kind: ErasureTargetKind,
        target_id: str,
        *,
        actor: str,
        reason: str,
    ) -> ErasureResult:
        """Always raises `ErasureUnsupported` (ADR-0007 §3, CLAUDE.md:
        "erasure exists only with the postgres backend")."""
        raise ErasureUnsupported(
            "erasure is not supported by the git backend - git is the source of truth there "
            "and nothing can be provably hard-deleted from every clone and remote (ADR-0007 §3)"
        )

    async def changes_since(self, cursor: str | None) -> StorageChanges:
        rev = None if cursor in (None, _NO_REMOTE_CURSOR) else cursor
        change_set = await asyncio.to_thread(self._repo.diff_since, rev)
        new_cursor = change_set.new_head if change_set.new_head is not None else _NO_REMOTE_CURSOR
        changed = change_set.added + change_set.modified
        return StorageChanges(cursor=new_cursor, changed=changed, deleted=change_set.deleted)
