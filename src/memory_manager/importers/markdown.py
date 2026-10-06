# SPDX-License-Identifier: AGPL-3.0-only
"""Markdown folder importer (#48): turn a directory tree into `ImportItem`s.

Walks a directory recursively for `*.md` files, skipping dotfiles/dirs and
never following symlinks (a symlinked file or directory is ignored outright
- CLAUDE.md's path-safety rules apply here too, even though this reads from
the local filesystem rather than the vault). Existing YAML frontmatter -
Obsidian's included - is parsed permissively: recognized keys (`title`,
`description`, `type`, `tags`, `aliases`, `created`/`date`) are picked up,
everything else is dropped rather than rejected, since a human's frontmatter
was never written against ADR-0005 in the first place.

A file that cannot even be decoded as UTF-8 is reported as a pre-existing
rejection (`collect`'s second return value) rather than turned into an
`ImportItem` - everything else about turning an item into note bytes lives
in `importers.core`.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, date, datetime
from pathlib import Path

import yaml

from memory_manager.importers.core import ImportItem, build_source
from memory_manager.vault.validate import NOTE_TYPES

__all__ = ["collect"]

_DELIMITER = "---"
_SOURCE_PREFIX = "import:markdown:"


def collect(
    root: Path, *, namespace: str, default_type: str = "reference"
) -> tuple[list[ImportItem], list[tuple[str, str]]]:
    """Walk `root` for `*.md` files and parse each into an `ImportItem`.

    Returns `(items, pre_rejected)`: `pre_rejected` holds `(source_ref,
    reason)` for files that could not even be decoded, in the same shape
    `ImportReport.rejected` uses, so a caller can merge it straight in.
    """
    items: list[ImportItem] = []
    pre_rejected: list[tuple[str, str]] = []

    for file_path in _walk_markdown_files(root):
        rel = file_path.relative_to(root).as_posix()
        source_ref = build_source(_SOURCE_PREFIX, rel)
        try:
            text = file_path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            pre_rejected.append((source_ref, f"not valid UTF-8: {exc}"))
            continue

        items.append(
            _parse_item(
                text,
                rel=rel,
                source_ref=source_ref,
                namespace=namespace,
                default_type=default_type,
            )
        )

    return items, pre_rejected


def _walk_markdown_files(root: Path) -> list[Path]:
    """Every `*.md` file under `root`, depth-first, never through a symlink."""
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        dirnames[:] = sorted(
            name
            for name in dirnames
            if not name.startswith(".") and not (current / name).is_symlink()
        )
        for filename in sorted(filenames):
            if filename.startswith(".") or not filename.endswith(".md"):
                continue
            file_path = current / filename
            if file_path.is_symlink():
                continue
            files.append(file_path)
    return files


def _parse_item(
    text: str, *, rel: str, source_ref: str, namespace: str, default_type: str
) -> ImportItem:
    frontmatter, body = _split_frontmatter(text)

    note_type = frontmatter.get("type")
    if not isinstance(note_type, str) or note_type not in NOTE_TYPES:
        note_type = default_type

    title = _clean_str(frontmatter.get("title"))
    description = _clean_str(frontmatter.get("description"))
    tags = _str_tuple(frontmatter.get("tags"))
    aliases = _str_tuple(frontmatter.get("aliases"))
    created = _parse_created(frontmatter.get("created", frontmatter.get("date")))

    return ImportItem(
        title=title,
        body=body,
        description=description,
        type=note_type,
        tags=tags,
        aliases=aliases,
        created=created,
        source=source_ref,
        slug_hint=Path(rel).stem,
        namespace=namespace,
    )


def _split_frontmatter(text: str) -> tuple[dict[str, object], str]:
    """Split a human Markdown file into `(frontmatter, body)`.

    Returns an empty `dict` and the whole text as the body when there is no
    frontmatter block, it is not valid YAML, or it is not a mapping - a
    foreign or malformed frontmatter block is not a reason to reject the
    file, just to treat it as plain Markdown.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != _DELIMITER:
        return {}, text

    closing = next((i for i in range(1, len(lines)) if lines[i].strip() == _DELIMITER), None)
    if closing is None:
        return {}, text

    frontmatter_text = "\n".join(lines[1:closing])
    body = "\n".join(lines[closing + 1 :])

    try:
        loaded = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError:
        return {}, text
    if not isinstance(loaded, dict) or not all(isinstance(key, str) for key in loaded):
        return {}, text
    return loaded, body


def _clean_str(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _str_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_created(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    if isinstance(value, str):
        candidate = value.strip()
        if _DATE_ONLY_RE.match(candidate):
            try:
                parsed_date = date.fromisoformat(candidate)
            except ValueError:
                return None
            return datetime(parsed_date.year, parsed_date.month, parsed_date.day, tzinfo=UTC)
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None
