# SPDX-License-Identifier: AGPL-3.0-only
"""The backend-agnostic storage contract (ADR-0007 §1).

`StorageBackend` is what every MCP tool is meant to call instead of the
Git-specific `WriteQueue`/`Repo` pair directly (#95 wires that up; this
module only introduces the interface and its Git implementation keeps
working exactly as `queue.py` does today). Validation, the secret scan,
path safety and the size cap stay above the interface (`storage/rules.py`),
so a future Postgres backend (WP-18) enforces the identical rules.

`Op`, `WriteRequest`, `WriteResult` and every `WriteError` subclass moved
here verbatim from `queue.py` (no renames, no behaviour change) - they are
the vocabulary both `WriteQueue` and `StorageBackend.write`/`edit`/
`supersede`/`archive` speak. `StoredNote` and `StorageChanges` are new:
the shapes `read`/`list` and `changes_since` return.

This module imports only from `memory_manager.vault.*` (and the stdlib) -
never from `memory_manager.storage.git` or `memory_manager.queue` - so
`queue.py` can import the names below without a cycle back through
`memory_manager.storage` (which re-exports `storage.git`, the Git
implementation that itself imports `WriteQueue`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol

__all__ = [
    "AuditHook",
    "EditMismatch",
    "InvalidNote",
    "NotFound",
    "Op",
    "SecretRejected",
    "StorageBackend",
    "StorageChanges",
    "StoredNote",
    "VersionConflict",
    "WriteConflict",
    "WriteError",
    "WriteFailed",
    "WriteRequest",
    "WriteResult",
]

Op = Literal["write", "edit", "archive", "supersede"]


@dataclass(frozen=True)
class WriteRequest:
    """One write submitted to the queue.

    `if_version` is either the sha256 `vault.note.version` of the content
    the caller last saw, or the literal `"new"` meaning `path` must not
    exist yet. `content` is used by `write`; `old_str`/`new_str` by `edit`;
    `archive` needs neither. `supersede` keeps `path` pointing at the old
    note (`if_version` is its current version) and uses `new_path`/`content`
    for the new note that replaces it - the two end up in one commit (#19).
    `message` overrides the default commit message (`"<op> <path>"`).
    """

    op: Op
    path: str
    client: str
    if_version: str
    content: bytes | None = None
    old_str: str | None = None
    new_str: str | None = None
    new_path: str | None = None
    message: str | None = None
    #: The caller's identity for the audit log (#39): an OAuth access token's
    #: subject, a static token's name, or `"stdio"` for a local session with
    #: no token at all (`memory_manager.mcp.server.current_actor`'s default,
    #: and this field's). Distinct from `client`, which is the *committer*
    #: label `vault.repo.author_for` commits as - `actor` is who asked,
    #: `client` is who the commit says did it.
    actor: str = "stdio"


@dataclass(frozen=True)
class WriteResult:
    """What a successful write produced: where, at what version, in which commit.

    `related` is set only by `supersede`: the old note's new path mapped to
    its new version, for a caller that needs to report both notes' state
    from one result.
    """

    path: str
    version: str
    commit: str
    related: dict[str, str] | None = None


#: Called exactly once for every processed write (`write`/`edit`/`supersede`/
#: `archive`), success and rejection alike - the seam `memory_manager.app`
#: wires an `memory_manager.audit.AuditWriter` through (#39), shared
#: verbatim by `queue.WriteQueue.add_audit_hook` (the `git` backend) and
#: `storage.postgres.PostgresBackend.add_audit_hook` (ADR-0007 §2, WP-18) -
#: both backends enforce "audit log for every write" (`CLAUDE.md`)
#: identically. `result` and `error` are mutually exclusive: exactly one is
#: `None`. `error` is typed `Exception`, not `WriteError`, only because the
#: call site that raises it catches broadly in case of a bug elsewhere, not
#: because either backend ever raises anything but a `WriteError` subclass
#: on purpose. Same failure contract everywhere this is called: a raising
#: hook is logged, never allowed to affect the write it was notified about.
AuditHook = Callable[["WriteRequest", "WriteResult | None", Exception | None], Awaitable[None]]


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


class WriteConflict(WriteError):
    """A push was rejected and rebasing it onto the remote conflicted.

    Nothing was overwritten: the local commit was discarded and the remote
    note is unchanged. Both the rejected write and the remote's current
    version are preserved at `conflict_path` for a human to resolve.
    `current_version`/`current_content` describe the remote's current state
    the same way `VersionConflict` does - `None` exactly when the remote
    side no longer has the note (it was deleted there).
    """

    def __init__(
        self,
        path: str,
        conflict_path: str,
        current_version: str | None,
        current_content: str | None,
    ) -> None:
        self.path = path
        self.conflict_path = conflict_path
        self.current_version = current_version
        self.current_content = current_content
        super().__init__(
            f"'{path}' could not be written: it changed on the remote at the same "
            f"time, see '{conflict_path}'"
        )

    def to_dict(self) -> dict[str, object]:
        result = super().to_dict()
        result["path"] = self.path
        result["conflict_path"] = self.conflict_path
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
    """A git operation failed, or a push the remote kept rejecting.

    A single rejected push is retried through a rebase (see `WriteConflict`
    for the case where that rebase conflicts); this is raised when the
    rebase itself fails for a reason other than a conflict, when the remote
    keeps moving faster than the queue's retry budget can catch up, or for
    any other git error. Either way the local clone is reset to the remote
    before this is raised, so it never carries an unpushed commit.
    """


@dataclass(frozen=True)
class StoredNote:
    """One note as a backend reads or lists it: where, what, at what version."""

    path: str
    content: bytes
    version: str


@dataclass(frozen=True)
class StorageChanges:
    """What changed in the vault since some earlier point, per `changes_since`.

    `cursor` is an opaque string a caller stores and passes back on its next
    call - never parsed or compared by value, only round-tripped. `changed`
    covers both additions and modifications (a caller re-reads either the
    same way); `deleted` is reported separately since there is nothing left
    to read.
    """

    cursor: str
    changed: tuple[str, ...]
    deleted: tuple[str, ...]


class StorageBackend(Protocol):
    """A backend that can read, write and enumerate the vault's notes.

    Every write method mirrors the corresponding op of `WriteRequest`:
    `if_version` is required (the sha256 `vault.note.version` of the
    content the caller last saw, or `"new"` for a path that must not exist
    yet - never overwrite silently, CLAUDE.md), `client` picks the commit
    author, `actor` is the caller's identity for the audit log, and
    `message` optionally overrides the default commit message. Validation,
    the secret scan, path safety and the size cap are enforced identically
    by every implementation (`storage/rules.py`), above this interface.

    Error contract, shared by every write method below:
    - `VersionConflict` - `if_version` did not match the current content.
    - `InvalidNote` - the resulting bytes violate ADR-0005 or the path rules.
    - `SecretRejected` - the resulting text looks like it contains a secret.
    A backend may raise additional, implementation-specific subclasses of
    `WriteError` (the Git backend's `WriteConflict`/`WriteFailed` for a
    remote push race) that are not part of this shared contract.
    """

    async def read(self, path: str) -> StoredNote | None:
        """The note at `path`, or `None` if it does not exist."""
        ...

    async def list(self, *, include_archived: bool = False) -> list[StoredNote]:
        """Every note in the vault, in path order.

        Archived notes (`_archive/...`) are included only when
        `include_archived` is set.
        """
        ...

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
        """Create or overwrite the note at `path` with `content`."""
        ...

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
        """Replace the one occurrence of `old_str` in `path`'s body with `new_str`.

        Raises `EditMismatch` if `old_str` occurs zero or more than once.
        """
        ...

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
        """Replace the note at `path` with a new note at `new_path`.

        The old note gets `valid_to` set and stays in place; the new note's
        `supersedes` gains the old note's `id`. Raises `NotFound` if `path`
        does not exist.
        """
        ...

    async def archive(
        self,
        path: str,
        *,
        if_version: str,
        client: str,
        actor: str = "stdio",
        message: str | None = None,
    ) -> WriteResult:
        """Move the note at `path` to its `_archive/` counterpart.

        Raises `NotFound` if `path` does not exist, `InvalidNote` if the
        archive target already exists. Never hard-deletes (CLAUDE.md).
        """
        ...

    async def changes_since(self, cursor: str | None) -> StorageChanges:
        """Notes added, modified or deleted since `cursor` (`None` for "everything").

        Returns a fresh cursor to pass on the next call alongside the
        changes. Read-only and independent of any write in progress: a
        concurrent `read()` of a path this reports may briefly lag or lead
        what `changes_since` itself just saw, by design (ADR-0007 §1).
        """
        ...
