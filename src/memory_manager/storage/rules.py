# SPDX-License-Identifier: AGPL-3.0-only
"""Pure, I/O-free write rules shared by every `StorageBackend` (ADR-0007 §1).

Everything here is validation and content computation only - no disk, no
git, no network - so both the Git backend (`storage/git.py`, through
`queue.py`'s `_process`/`_do_*`) and a future Postgres backend enforce
`if_version`, ADR-0005, the secret scan and the operator blocklist
(`BLOCKLIST_FILE`, #244) identically. Each function that
used to also do a filesystem lookup (does the archive target already
exist? does `new_path` already exist?) now takes that lookup's result as a
plain argument instead - `queue.py` performs the actual `repo.read_file`
call and passes the boolean/bytes in, in the same order the check used to
run, so error precedence and message wording are unchanged from before
this was split out of `queue.py`.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from memory_manager.storage.base import (
    BlocklistRejected,
    EditMismatch,
    InvalidNote,
    NotFound,
    Op,
    SecretRejected,
    VersionConflict,
    WriteRequest,
)
from memory_manager.vault import blocklist
from memory_manager.vault.note import NoteFormatError, parse, serialize
from memory_manager.vault.paths import NotePath, PathRejected, parse_note_path
from memory_manager.vault.secrets import SecretFound, check
from memory_manager.vault.ulid import new_ulid
from memory_manager.vault.validate import NoteInvalid, validate, validate_bytes

__all__ = [
    "check_version",
    "decode_for_conflict",
    "new_content_for",
    "parse_note_path_or_raise",
    "prepare_archive",
    "prepare_promote_content",
    "prepare_promote_paths",
    "prepare_supersede_content",
    "prepare_supersede_paths",
    "prepare_write_or_edit",
]


def check_version(
    request: WriteRequest, current_version: str | None, current: bytes | None
) -> None:
    """Raise `VersionConflict` unless `request.if_version` matches `current_version`.

    `"new"` only matches when `current` does not exist yet - never silently
    overwrite (CLAUDE.md).
    """
    if request.if_version == "new":
        if current is not None:
            raise VersionConflict(request.path, current_version, decode_for_conflict(current))
    elif current_version != request.if_version:
        raise VersionConflict(request.path, current_version, decode_for_conflict(current))


def decode_for_conflict(current: bytes | None) -> str | None:
    """`current` decoded for an error payload, or `None` if it does not exist."""
    if current is None:
        return None
    return current.decode("utf-8", errors="replace")


def new_content_for(
    op: Op,
    path: str,
    content: bytes | None,
    old_str: str | None,
    new_str: str | None,
    current: bytes | None,
) -> bytes:
    """The raw bytes a `write` or `edit` would produce, before validation.

    `write` just returns `content` (raising `InvalidNote` if it is missing).
    `edit` replaces the one occurrence of `old_str` in `current`'s text with
    `new_str`, raising `EditMismatch` unless it occurs exactly once.
    """
    if op == "write":
        if content is None:
            raise InvalidNote(path, "write requires content")
        return content

    if old_str is None or new_str is None:
        raise InvalidNote(path, "edit requires old_str and new_str")
    current_text = current.decode("utf-8") if current is not None else ""
    count = current_text.count(old_str)
    if count != 1:
        raise EditMismatch(path, count)
    new_text = current_text.replace(old_str, new_str, 1)
    return new_text.encode("utf-8")


def parse_note_path_or_raise(path: str, *, allow_archive: bool = False) -> NotePath:
    """`vault.paths.parse_note_path`, mapping `PathRejected` to `InvalidNote`."""
    try:
        return parse_note_path(path, allow_archive=allow_archive)
    except PathRejected as exc:
        raise InvalidNote(path, str(exc)) from exc


def prepare_write_or_edit(
    op: Op,
    path: str,
    content: bytes | None,
    old_str: str | None,
    new_str: str | None,
    current: bytes | None,
) -> bytes:
    """The canonical bytes to commit for a `write`/`edit`.

    Raises `InvalidNote` (bad path, structurally or semantically invalid
    note, or the note's `id` changed), `EditMismatch`, `SecretRejected`, or
    `BlocklistRejected` (an operator blocklist category, checked right
    after the secret scan, #244).
    """
    new_bytes = new_content_for(op, path, content, old_str, new_str, current)

    note_path = parse_note_path_or_raise(path)

    try:
        parsed = validate_bytes(new_bytes, expected_type=note_path.type)
    except (NoteFormatError, NoteInvalid) as exc:
        raise InvalidNote(path, str(exc)) from exc

    if current is not None:
        try:
            current_note = parse(current)
        except NoteFormatError as exc:
            raise InvalidNote(path, str(exc)) from exc
        if current_note.id != parsed.id:
            raise InvalidNote(path, "id must not change")

    decoded = new_bytes.decode("utf-8")
    try:
        check(decoded)
    except SecretFound as exc:
        raise SecretRejected(path, str(exc)) from exc
    try:
        blocklist.check(decoded)
    except blocklist.BlocklistFound as exc:
        raise BlocklistRejected(path, exc.category) from exc

    canonical = serialize(parsed)
    return new_bytes if new_bytes == canonical else canonical


def prepare_archive(
    path: str, current: bytes | None, *, archive_exists: bool, now: datetime
) -> bytes:
    """The canonical bytes to commit at the archive path for `archive`.

    Caller order: `current is None` and the path parse happen before this
    is called (the latter is needed to compute the archive path for the
    existence lookup `archive_exists` carries in); this covers the rest -
    the existence check, parsing the current note and stamping `updated`.
    Raises `NotFound` if `current` is `None`, `InvalidNote` if the archive
    target already exists or the current note fails to parse.
    """
    if current is None:
        raise NotFound(path)
    if archive_exists:
        raise InvalidNote(path, "archive target exists")
    try:
        parsed = parse(current)
    except NoteFormatError as exc:
        raise InvalidNote(path, str(exc)) from exc
    archived_note = replace(parsed, updated=now)
    return serialize(archived_note)


def prepare_supersede_paths(
    path: str, new_path: str | None, content: bytes | None, current: bytes | None
) -> tuple[NotePath, NotePath, str, bytes, bytes]:
    """The parsed old/new note paths for `supersede`, or the errors `queue.py`
    must raise before it is safe to do the `new_path` existence lookup.

    Returns `(old_note_path, new_note_path, new_path, content, current)` -
    `new_path`/`content`/`current` narrowed to non-`None`, now that the
    checks below have passed, so the caller never needs an `assert`.
    Raises `NotFound` if `path`'s current note is missing, `InvalidNote` if
    `new_path`/`content` are missing or either path is not a valid note path.
    """
    if current is None:
        raise NotFound(path)
    if not new_path:
        raise InvalidNote(path, "supersede requires new_path")
    if content is None:
        raise InvalidNote(new_path, "supersede requires content for the new note")

    old_note_path = parse_note_path_or_raise(path)
    new_note_path = parse_note_path_or_raise(new_path)
    return old_note_path, new_note_path, new_path, content, current


def prepare_supersede_content(
    path: str,
    new_path: str,
    content: bytes,
    current: bytes,
    *,
    old_note_path: NotePath,
    new_note_path: NotePath,
    new_target_exists: bool,
    now: datetime,
) -> tuple[bytes, bytes]:
    """The canonical `(new_final_bytes, old_final_bytes)` to commit for `supersede`.

    Called after `prepare_supersede_paths` and the `new_path` existence
    lookup it enables. Raises `InvalidNote` if `new_path` already exists,
    either note fails to parse, or either fails ADR-0005 validation;
    `SecretRejected` if the new note's text looks like it contains a secret;
    `BlocklistRejected` if it matches an operator blocklist category (#244).
    """
    if new_target_exists:
        raise InvalidNote(new_path, "already exists, supersede needs an unused path")

    try:
        old_note = parse(current)
    except NoteFormatError as exc:
        raise InvalidNote(path, str(exc)) from exc
    try:
        new_note = parse(content)
    except NoteFormatError as exc:
        raise InvalidNote(new_path, str(exc)) from exc

    supersedes = new_note.supersedes
    if old_note.id not in supersedes:
        supersedes = (*supersedes, old_note.id)
    new_note = replace(new_note, supersedes=supersedes)

    today = now.date()
    if old_note.valid_to is not None and old_note.valid_to < today:
        new_valid_to = old_note.valid_to
    else:
        new_valid_to = today
    old_note = replace(old_note, valid_to=new_valid_to, updated=now)

    try:
        validate(new_note, expected_type=new_note_path.type)
    except NoteInvalid as exc:
        raise InvalidNote(new_path, str(exc)) from exc
    try:
        validate(old_note, expected_type=old_note_path.type)
    except NoteInvalid as exc:
        raise InvalidNote(path, str(exc)) from exc

    new_final_bytes = serialize(new_note)
    old_final_bytes = serialize(old_note)

    new_decoded = new_final_bytes.decode("utf-8")
    try:
        check(new_decoded)
    except SecretFound as exc:
        raise SecretRejected(new_path, str(exc)) from exc
    try:
        blocklist.check(new_decoded)
    except blocklist.BlocklistFound as exc:
        raise BlocklistRejected(new_path, exc.category) from exc

    return new_final_bytes, old_final_bytes


def prepare_promote_paths(
    path: str, target_namespace: str | None, current: bytes | None
) -> tuple[NotePath, NotePath, str, bytes]:
    """The parsed source/target note paths for `promote`, or the errors `queue.py`
    must raise before it is safe to do the two existence lookups it enables (the
    target path, and - unless `keep_original` - the source's archive path).

    Returns `(source_note_path, target_note_path, target_path, current)` -
    `current` narrowed to non-`None`, now that the check below has passed, so
    the caller never needs an `assert`. The target path is always
    `<target_namespace>/<type>/<slug>.md` with `path`'s own `type`/`slug`
    carried over - never supplied by the caller. Raises `NotFound` if `path`'s
    current note is missing, `InvalidNote` if `target_namespace` is empty,
    `path` is not a valid, non-archived note path (an archived source is
    rejected here, the same way `allow_archive=False` rejects any other
    `_archive/...` path), or the constructed target path is invalid (e.g. a
    malformed `target_namespace`).
    """
    if current is None:
        raise NotFound(path)
    if not target_namespace:
        raise InvalidNote(path, "promote requires target_namespace")

    source_note_path = parse_note_path_or_raise(path)
    candidate = NotePath(
        namespace=target_namespace, type=source_note_path.type, slug=source_note_path.slug
    )
    target_note_path = parse_note_path_or_raise(candidate.relative)
    return source_note_path, target_note_path, target_note_path.relative, current


def prepare_promote_content(
    path: str,
    target_path: str,
    current: bytes,
    *,
    target_note_path: NotePath,
    target_exists: bool,
    archive_exists: bool,
    keep_original: bool,
    now: datetime,
) -> tuple[bytes, bytes | None]:
    """The canonical `(new_bytes, archived_original_bytes)` to commit for `promote`.

    `archived_original_bytes` is `None` exactly when `keep_original` is set -
    the original at `path` is left untouched, only the copy is written.
    Otherwise it reuses `prepare_archive` for the original, the same
    archive-target-exists check and `updated` stamp a plain `archive` uses.
    Called after `prepare_promote_paths` and the two existence lookups it
    enables. Raises `InvalidNote` if `target_path` already exists, the
    original fails to parse, the copy fails ADR-0005 validation, or (when
    archiving) the original's archive target already exists;
    `SecretRejected` if the copy's text looks like it contains a secret.
    """
    if target_exists:
        raise InvalidNote(target_path, "already exists, promote needs an unused path")

    try:
        original = parse(current)
    except NoteFormatError as exc:
        raise InvalidNote(path, str(exc)) from exc

    supersedes = original.supersedes
    if original.id not in supersedes:
        supersedes = (*supersedes, original.id)
    new_note = replace(original, id=new_ulid(now), supersedes=supersedes)

    try:
        validate(new_note, expected_type=target_note_path.type)
    except NoteInvalid as exc:
        raise InvalidNote(target_path, str(exc)) from exc

    new_bytes = serialize(new_note)

    try:
        check(new_bytes.decode("utf-8"))
    except SecretFound as exc:
        raise SecretRejected(target_path, str(exc)) from exc

    if keep_original:
        return new_bytes, None

    archived_bytes = prepare_archive(path, current, archive_exists=archive_exists, now=now)
    return new_bytes, archived_bytes
