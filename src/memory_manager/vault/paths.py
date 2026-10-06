# SPDX-License-Identifier: AGPL-3.0-only
"""Vault-relative path parsing and safe resolution to disk (ADR-0005, #9).

A note's path is always `<namespace>/<type>/<slug>.md`, relative to the
vault root, with `/` as separator; an archived note lives at the mirrored
`_archive/<namespace>/<type>/<slug>.md` (ADR-0005 "Path"). This module is
the one place that turns a client-supplied path string into a path on disk,
so every check that keeps a client inside the vault root lives here: the
allowlist from the ADR, rejection of traversal and encoding tricks, and a
symlink-free walk down to the resolved file.

`parse_note_path` only looks at the string; it never touches disk.
`resolve` additionally walks the filesystem and is what the read/write
paths must call before opening a file - never build a path from client
input any other way.
"""

from __future__ import annotations

import re
import stat
from dataclasses import dataclass, replace
from pathlib import Path

from memory_manager.vault.validate import NOTE_TYPES

__all__ = ["NotePath", "PathRejected", "parse_note_path", "resolve"]

_MAX_PATH_CHARS = 200
_MAX_SLUG_CHARS = 80
_ARCHIVE_SEGMENT = "_archive"
_FILE_SUFFIX = ".md"

_NAMESPACE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

_SHAPE_HINT = (
    "a valid path looks like '<namespace>/<type>/<slug>.md', e.g. 'personal/fact/favorite-color.md'"
)


class PathRejected(ValueError):
    """`rel` is not a safe, valid vault-relative note path."""


@dataclass(frozen=True)
class NotePath:
    """A parsed, valid note path, independent of where the vault root is."""

    namespace: str
    type: str
    slug: str
    archived: bool = False

    @property
    def relative(self) -> str:
        """The vault-relative path, `/`-separated, e.g. `personal/fact/x.md`."""
        base = f"{self.namespace}/{self.type}/{self.slug}{_FILE_SUFFIX}"
        return f"{_ARCHIVE_SEGMENT}/{base}" if self.archived else base

    def archive_path(self) -> NotePath:
        """The archived counterpart of this path (`_archive/...`)."""
        return replace(self, archived=True)

    def live_path(self) -> NotePath:
        """The live counterpart of this path (without the `_archive/` prefix)."""
        return replace(self, archived=False)


def parse_note_path(rel: str, *, allow_archive: bool = False) -> NotePath:
    """Parse `rel` into a `NotePath`, without touching the filesystem.

    Raises `PathRejected` for anything that is not exactly
    `<namespace>/<type>/<slug>.md` (or, when `allow_archive` is set, its
    `_archive/`-prefixed counterpart) per ADR-0005: traversal, absolute
    paths, backslashes, percent signs, control characters, non-ASCII
    characters (which rules out Unicode lookalike slashes and dots), empty
    segments, and any charset/length/enum violation of the namespace, type
    or slug.
    """
    if not rel:
        raise PathRejected(f"path is empty - {_SHAPE_HINT}")
    if len(rel) > _MAX_PATH_CHARS:
        raise PathRejected(f"path is {len(rel)} chars, max {_MAX_PATH_CHARS} - {_SHAPE_HINT}")
    if not rel.isascii():
        raise PathRejected(
            f"path {rel!r} contains a non-ASCII character - "
            f"only 'a'-'z', '0'-'9', '-', '.' and '/' are allowed - {_SHAPE_HINT}"
        )
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in rel):
        raise PathRejected(f"path {rel!r} contains a control character - {_SHAPE_HINT}")
    if "\\" in rel:
        raise PathRejected(f"path {rel!r} contains a backslash, use '/' instead - {_SHAPE_HINT}")
    if "%" in rel:
        raise PathRejected(
            f"path {rel!r} contains '%', percent-encoding is never decoded - {_SHAPE_HINT}"
        )
    if rel.startswith("/"):
        raise PathRejected(
            f"path {rel!r} is absolute, it must be relative to the vault root - {_SHAPE_HINT}"
        )
    if rel.endswith("/"):
        raise PathRejected(f"path {rel!r} has a trailing slash - {_SHAPE_HINT}")

    segments = rel.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise PathRejected(f"path {rel!r} contains an empty, '.' or '..' segment - {_SHAPE_HINT}")

    archived = False
    if segments[0] == _ARCHIVE_SEGMENT:
        if not allow_archive:
            raise PathRejected(
                f"path {rel!r} is an archive path, not accepted here - {_SHAPE_HINT}"
            )
        archived = True
        segments = segments[1:]

    if len(segments) != 3:
        raise PathRejected(
            f"path {rel!r} does not have the shape <namespace>/<type>/<slug>.md - {_SHAPE_HINT}"
        )

    namespace, note_type, filename = segments

    if namespace.startswith("_"):
        raise PathRejected(
            f"namespace {namespace!r} starts with '_', which is reserved - "
            "use a namespace starting with a letter or digit"
        )
    if not _NAMESPACE_RE.match(namespace):
        raise PathRejected(
            f"namespace {namespace!r} does not match ^[a-z0-9][a-z0-9-]{{0,39}}$ - "
            "use lower-case letters, digits and hyphens only, max 40 chars"
        )

    if note_type not in NOTE_TYPES:
        allowed = ", ".join(NOTE_TYPES)
        raise PathRejected(
            f"type directory {note_type!r} is not one of the allowed types ({allowed}) - "
            "use one of the allowed type directories"
        )

    if not filename.endswith(_FILE_SUFFIX):
        raise PathRejected(f"path {rel!r} does not end with '.md' - {_SHAPE_HINT}")
    slug = filename[: -len(_FILE_SUFFIX)]

    if len(slug) > _MAX_SLUG_CHARS:
        raise PathRejected(
            f"slug {slug!r} is {len(slug)} chars, max {_MAX_SLUG_CHARS} - shorten it"
        )
    if not _SLUG_RE.match(slug):
        raise PathRejected(
            f"slug {slug!r} does not match ^[a-z0-9]+(-[a-z0-9]+)*$ - "
            "use lower-case letters, digits and hyphens only, no leading, trailing "
            "or doubled hyphens"
        )

    return NotePath(namespace=namespace, type=note_type, slug=slug, archived=archived)


def resolve(
    vault_root: Path,
    rel: str,
    *,
    allow_archive: bool = False,
    must_exist: bool = False,
) -> Path:
    """Resolve `rel` to an absolute file path inside `vault_root`.

    Raises `PathRejected` if `rel` is not a valid note path (see
    `parse_note_path`), if any existing path component on disk is a
    symlink, if the resolved path escapes `vault_root`, or if the
    resulting path exists but is not a regular file. `must_exist`
    additionally requires every path component to already exist.
    """
    note_path = parse_note_path(rel, allow_archive=allow_archive)
    root = vault_root.resolve()

    candidate = root
    for segment in note_path.relative.split("/"):
        candidate = candidate / segment
        try:
            entry_stat = candidate.lstat()
        except FileNotFoundError:
            if must_exist:
                raise PathRejected(f"'{note_path.relative}' does not exist in the vault") from None
            continue
        except NotADirectoryError as exc:
            raise PathRejected(
                f"'{note_path.relative}' is invalid, a parent component is not a directory"
            ) from exc
        if stat.S_ISLNK(entry_stat.st_mode):
            raise PathRejected(
                f"'{note_path.relative}' contains a symlink at {segment!r}, "
                "symlinks are not allowed in the vault"
            )

    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise PathRejected(f"'{note_path.relative}' resolves outside the vault root") from None

    if candidate.exists() and not candidate.is_file():
        raise PathRejected(f"'{note_path.relative}' exists but is not a regular file")

    return candidate
