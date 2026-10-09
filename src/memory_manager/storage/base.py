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

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

__all__ = [
    "AuditHook",
    "BlocklistRejected",
    "EditMismatch",
    "ErasureResult",
    "ErasureTargetKind",
    "ErasureUnsupported",
    "IndexCommitHook",
    "IndexHook",
    "InvalidNote",
    "NotFound",
    "Op",
    "SecretRejected",
    "StorageBackend",
    "StorageChanges",
    "StoredNote",
    "SystemWriteUnsupported",
    "VersionConflict",
    "WriteConflict",
    "WriteError",
    "WriteFailed",
    "WriteRequest",
    "WriteResult",
]

Op = Literal["write", "edit", "archive", "supersede", "promote"]


@dataclass(frozen=True)
class WriteRequest:
    """One write submitted to the queue.

    `if_version` is either the sha256 `vault.note.version` of the content
    the caller last saw, or the literal `"new"` meaning `path` must not
    exist yet. `content` is used by `write`; `old_str`/`new_str` by `edit`;
    `archive` needs neither. `supersede` keeps `path` pointing at the old
    note (`if_version` is its current version) and uses `new_path`/`content`
    for the new note that replaces it - the two end up in one commit (#19).
    `promote` (#226) also keeps `path` pointing at the original (`if_version`
    is its current version) and uses `target_namespace`/`keep_original`
    instead of `new_path`/`content` - the copy's path and bytes are derived
    from the original, not supplied by the caller. `message` overrides the
    default commit message (`"<op> <path>"`).
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
    #: `promote` only (#226): the namespace the copy at `path` is written
    #: into, as `<target_namespace>/<type>/<slug>.md` with `path`'s own
    #: `type`/`slug` carried over unchanged.
    target_namespace: str | None = None
    #: `promote` only (#226): leave the original at `path` untouched instead
    #: of archiving it once the copy exists. Defaults to archiving
    #: (ADR-0008 "`memory_promote`"), the same default the MCP tool (#227)
    #: exposes.
    keep_original: bool = False
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

    `related` is set only by `supersede` and `promote`: the old note's
    resulting path mapped to its new version, for a caller that needs to
    report both notes' state from one result. For `supersede` that path is
    always the original `path` itself (its content/`valid_to` changed, it
    never moves); for `promote` it is the original's archive path
    (`keep_original=False`, the default) or the original `path` unchanged
    (`keep_original=True`).
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

#: `storage.postgres.PostgresBackend`'s in-transaction indexing seam (ADR-0007
#: §4, WP-18/#98): called with `(conn, path, content)` for every note a write
#: touches, right after that write's revision is inserted but *before* its
#: transaction commits - `conn` is the backend's own connection, already inside
#: that transaction, so the derived index (`notes`/`chunks`/`links`) lands in the
#: exact same commit as the write, and a rolled-back write takes its index rows
#: with it for free. Typed `Any` for `conn` rather than `asyncpg.Connection`:
#: this module stays independent of `asyncpg` (see the module docstring), the
#: same reason `AuditHook` above never had to import anything from `queue.py`
#: either - `index.indexer.Indexer.index_on_connection` is the one implementation
#: today and is fully typed on its own side. Index errors propagate like any
#: other exception raised inside the write's transaction: it rolls back, and
#: the existing `PostgresError` -> `WriteFailed` mapping applies unchanged.
IndexHook = Callable[[Any, str, bytes], Awaitable[None]]

#: `PostgresBackend`'s post-commit indexing seam (ADR-0007 §4, WP-18/#98):
#: called with the ids of every note a write just committed, once its
#: transaction has already committed - never inside it, and never awaited by
#: the write itself. `index.indexer.Indexer.schedule_embeddings` is the one
#: implementation today: it starts background embedding tasks and returns
#: immediately, so a slow or failing embedding call can never slow down or
#: fail the write that triggered it.
IndexCommitHook = Callable[[Sequence[str]], Awaitable[None]]


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


class BlocklistRejected(WriteError):
    """The note text a write would produce matches an operator blocklist category.

    `category` is the name of the matched `[[category]]` from `BLOCKLIST_FILE`
    (`vault.blocklist`) - never the text that matched it (CLAUDE.md: note
    content is never echoed back into an error or the audit log, #244).
    """

    def __init__(self, path: str, category: str) -> None:
        self.path = path
        self.category = category
        super().__init__(f"'{path}' rejected: blocklist category {category!r}")

    def to_dict(self) -> dict[str, object]:
        result = super().to_dict()
        result["path"] = self.path
        result["category"] = self.category
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


class ErasureUnsupported(Exception):
    """Raised by `GitBackend.erase` (ADR-0007 §3): erasure only exists for the
    Postgres backend (CLAUDE.md "erasure exists only with the postgres
    backend, outside MCP"). Git is designed to keep history forever - there
    is no provable way to hard-delete from every clone and remote - so the
    Git backend refuses the call outright rather than pretending to comply.
    Not a `WriteError`: `erase` is a separate, non-MCP operation (#231), not
    part of the write contract every `WriteError` subclass above describes.
    """


class SystemWriteUnsupported(Exception):
    """Raised by `GitBackend.write_system` (#239, ADR-0008 addendum "break-glass
    notification"): a system-authored write only exists for the Postgres backend.

    Git has no owner role that bypasses row security and no `author_oid` column a
    write could ever leave `NULL` on - there is no connection shape on the Git side
    that means "the system wrote this, not a person", so the Git backend refuses the
    call outright rather than pretending to comply (same reasoning as `ErasureUnsupported`
    above). Not a `WriteError`: `write_system` is a separate, non-MCP, server-internal
    operation, never reachable from a client write.
    """


#: The three things `storage.erasure` can hard-delete (ADR-0007 §3 addendum
#: 2026-10-08, #231): a single note (by its ULID `id`), every note in a
#: namespace (by its alias), or a user and their personal namespace (by
#: `users.oid`) - `PostgresBackend.erase`'s own dispatch key.
ErasureTargetKind = Literal["note", "namespace", "user"]


@dataclass(frozen=True)
class ErasureResult:
    """What one `storage.erasure.erase_note`/`erase_namespace`/`erase_user` call did.

    `target_ids` is the one id `erase` was called with, except for
    `erase_user` with no personal namespace of its own yet - still exactly
    one id (`target_kind="user"`'s `users.oid`), never more: this is *not*
    the set of notes touched, only what identified the erasure itself.
    `row_counts` is a plain count per table touched (e.g. `{"vault_notes":
    3, "vault_revisions": 7, ...}`), the same shape persisted in
    `erasure_log.row_counts` and mirrored into the `audit_log` row's
    `detail` - counts only, never a path, a slug or any other content.
    `erasure_log_id` is that row's own id, for a caller that wants to look
    it up again.
    """

    target_kind: ErasureTargetKind
    target_ids: tuple[str, ...]
    row_counts: dict[str, int]
    erasure_log_id: int


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
    - `BlocklistRejected` - the resulting text matches an operator blocklist
      category (`BLOCKLIST_FILE`, #244); never raised when none is configured.
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
        """Copy the note at `path` into `target_namespace` as a new note that supersedes it.

        The copy gets a new `id` at `<target_namespace>/<type>/<slug>.md`
        (`path`'s own `type`/`slug`); its `supersedes` gains the original's
        `id`; every other field is byte-identical to the original
        (ADR-0008 "`memory_promote`"). By default (`keep_original=False`)
        the original is archived in the same write; `keep_original=True`
        leaves it untouched. `related` in the result carries the
        original's resulting path (its archive path, or `path` itself when
        `keep_original=True`) mapped to its version. Raises `NotFound` if
        `path` does not exist, `InvalidNote` if `path` is archived, the
        target path already exists, or (when archiving) the original's
        archive target already exists.
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

    async def erase(
        self,
        target_kind: ErasureTargetKind,
        target_id: str,
        *,
        actor: str,
        reason: str,
    ) -> ErasureResult:
        """Hard-delete `target_kind`'s `target_id`, in one transaction (ADR-0007 §3, #231).

        A separate, non-MCP, admin-only operation - never reachable from a
        write (`CLAUDE.md`: "no MCP tool hard-deletes notes"). `target_id`
        is a note's ULID `id` for `"note"`, a namespace alias for
        `"namespace"`, or a `users.oid` for `"user"`. `actor`/`reason` are
        recorded on the `erasure_log` row this call writes, in the same
        transaction as the deletes themselves - a failure partway through
        rolls back the deletes and leaves no `erasure_log`/`audit_log` row
        behind either.

        `GitBackend.erase` always raises `ErasureUnsupported`: Git is the
        source of truth there, and nothing about it can be provably
        erased from every clone and remote (ADR-0007 §3).
        """
        ...

    async def write_system(self, request: WriteRequest, *, reason: str) -> WriteResult:
        """Create or overwrite `request.path` as the system identity, not a real
        principal's own (#239, ADR-0008 addendum "break-glass notification").

        A separate, non-MCP, server-internal operation - never reachable from a
        client write, the same "no MCP tool ever selects this op" shape `erase` has.
        Runs the identical validation, secret scan, blocklist, size cap and audit
        every `write`/`edit` enforces (this Protocol's own docstring) - a system
        write is rejected by the same rules a user's own write would be, never a
        quieter shortcut around them. Only the identity differs: the resulting
        revision carries no author at all (`author_oid is NULL`), not the caller's
        own, so a later reader can tell a system-written note apart from one a
        person wrote. `reason` becomes `request.message` when the caller did not set
        one of its own.

        `GitBackend.write_system` always raises `SystemWriteUnsupported`: a
        system-authored note only exists with `STORAGE_BACKEND=postgres`
        (ADR-0008 addendum, "Postgres mode only, like every enterprise `/account`
        section").
        """
        ...

    async def changes_since(self, cursor: str | None) -> StorageChanges:
        """Notes added, modified or deleted since `cursor` (`None` for "everything").

        Returns a fresh cursor to pass on the next call alongside the
        changes. Read-only and independent of any write in progress: a
        concurrent `read()` of a path this reports may briefly lag or lead
        what `changes_since` itself just saw, by design (ADR-0007 §1). May
        report an already-committed change with a delay (a concurrent
        transaction elsewhere can hold a backend's visibility window
        back) but never drops one - a caller polls if it needs a change
        to have shown up, rather than assuming the very next call will see it.
        """
        ...
