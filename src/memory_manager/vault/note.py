# SPDX-License-Identifier: AGPL-3.0-only
"""Parsing and canonical serialization of notes (ADR-0005).

A note is a Markdown file with a YAML frontmatter block:

    ---
    id: ...
    ...
    ---
    <body>

`parse` accepts any frontmatter a human can write with valid, structurally
correct YAML (PyYAML's safe loader). `serialize` always produces the one
canonical byte representation described in the ADR, so that `version` is
meaningful and diffs on server writes stay minimal.

Semantic validation (field length limits, enum membership, date ordering,
ULID shape of `id`/`supersedes`, path/slug rules, link extraction) is out of
scope here; it lives in the modules that own those rules (#8, #9, #10). This
module only rejects structurally broken input: missing delimiters, YAML that
is not a mapping, YAML syntax errors, unknown keys and values of the wrong
type.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime

import yaml

__all__ = ["Note", "NoteFormatError", "parse", "serialize", "version"]

_DELIMITER = "---"

_REQUIRED_FIELDS = ("id", "title", "description", "type", "created", "updated")
_STRING_FIELDS = ("id", "title", "description", "type", "source")
_LIST_FIELDS = ("tags", "aliases", "supersedes")
_TIMESTAMP_FIELDS = ("created", "updated")
_DATE_FIELDS = ("valid_from", "valid_to")
_KNOWN_FIELDS = frozenset(_STRING_FIELDS + _LIST_FIELDS + _TIMESTAMP_FIELDS + _DATE_FIELDS)


class NoteFormatError(ValueError):
    """A note's bytes could not be parsed into a `Note`.

    `field` names the offending frontmatter key, if the problem is tied to
    one. `line` is the 1-indexed line within the file, if known.
    """

    def __init__(self, message: str, *, field: str | None = None, line: int | None = None) -> None:
        super().__init__(message)
        self.field = field
        self.line = line


@dataclass(frozen=True)
class Note:
    """A single parsed note, independent of its path in the vault."""

    id: str
    title: str
    description: str
    type: str
    created: datetime
    updated: datetime
    body: str
    tags: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    valid_from: date | None = None
    valid_to: date | None = None
    supersedes: tuple[str, ...] = ()
    source: str | None = None


def parse(data: bytes) -> Note:
    """Parse note file bytes into a `Note`.

    Raises `NoteFormatError` for anything structurally wrong: a BOM, CR
    characters, missing frontmatter delimiters, non-mapping or syntactically
    invalid YAML, unknown frontmatter keys, or values of the wrong type.
    """
    if data.startswith(b"\xef\xbb\xbf"):
        raise NoteFormatError("file must not start with a UTF-8 BOM")
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise NoteFormatError(f"file is not valid UTF-8: {exc}") from exc

    if "\r" in text:
        raise NoteFormatError("file must use LF line endings, found a CR character")

    lines = text.split("\n")
    if lines[0] != _DELIMITER:
        raise NoteFormatError("file must start with a '---' frontmatter delimiter", line=1)

    closing_line_index = next((i for i in range(1, len(lines)) if lines[i] == _DELIMITER), None)
    if closing_line_index is None:
        raise NoteFormatError("missing closing '---' frontmatter delimiter")

    frontmatter_text = "\n".join(lines[1:closing_line_index])
    body = "\n".join(lines[closing_line_index + 1 :])

    loaded = _load_frontmatter(frontmatter_text)
    if not isinstance(loaded, dict):
        raise NoteFormatError(f"frontmatter must be a YAML mapping, got {type(loaded).__name__}")

    for key in loaded:
        if not isinstance(key, str):
            raise NoteFormatError(f"frontmatter keys must be strings, got {key!r}")
        if key not in _KNOWN_FIELDS:
            raise NoteFormatError(f"unknown frontmatter field '{key}'", field=key)

    for required in _REQUIRED_FIELDS:
        if required not in loaded:
            raise NoteFormatError(f"missing required field '{required}'", field=required)

    tags = _expect_str_list(loaded["tags"], "tags") if "tags" in loaded else ()
    aliases = _expect_str_list(loaded["aliases"], "aliases") if "aliases" in loaded else ()
    valid_from = (
        _expect_date(loaded["valid_from"], "valid_from") if "valid_from" in loaded else None
    )
    valid_to = _expect_date(loaded["valid_to"], "valid_to") if "valid_to" in loaded else None
    supersedes = (
        _expect_str_list(loaded["supersedes"], "supersedes") if "supersedes" in loaded else ()
    )
    source = _expect_str(loaded["source"], "source") if "source" in loaded else None

    return Note(
        id=_expect_str(loaded["id"], "id"),
        title=_expect_str(loaded["title"], "title"),
        description=_expect_str(loaded["description"], "description"),
        type=_expect_str(loaded["type"], "type"),
        created=_expect_timestamp(loaded["created"], "created"),
        updated=_expect_timestamp(loaded["updated"], "updated"),
        body=body,
        tags=tags,
        aliases=aliases,
        valid_from=valid_from,
        valid_to=valid_to,
        supersedes=supersedes,
        source=source,
    )


def _load_frontmatter(frontmatter_text: str) -> object:
    try:
        return yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as exc:
        line = None
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            # `mark.line` is 0-indexed within the frontmatter block. +1 for
            # 1-indexing, +1 for the opening '---' delimiter line.
            line = mark.line + 2
        raise NoteFormatError(f"frontmatter is not valid YAML: {exc}", line=line) from exc


def _expect_str(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise NoteFormatError(
            f"field '{field_name}' must be a string, got {type(value).__name__}",
            field=field_name,
        )
    return value


def _expect_str_list(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise NoteFormatError(
            f"field '{field_name}' must be a list of strings, got {type(value).__name__}",
            field=field_name,
        )
    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise NoteFormatError(
                f"field '{field_name}' must be a list of strings, got a {type(item).__name__} item",
                field=field_name,
            )
        items.append(item)
    return tuple(items)


def _expect_timestamp(value: object, field_name: str) -> datetime:
    parsed: datetime
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise NoteFormatError(
                f"field '{field_name}' is not a valid RFC 3339 timestamp: {value!r}",
                field=field_name,
            ) from exc
    else:
        raise NoteFormatError(
            f"field '{field_name}' must be a timestamp, got {type(value).__name__}",
            field=field_name,
        )
    if parsed.tzinfo is None:
        raise NoteFormatError(
            f"field '{field_name}' must be timezone-aware "
            "(RFC 3339 with a 'Z' suffix or an offset)",
            field=field_name,
        )
    return parsed.astimezone(UTC)


def _expect_date(value: object, field_name: str) -> date:
    if isinstance(value, datetime):
        raise NoteFormatError(
            f"field '{field_name}' must be a date (YYYY-MM-DD), not a timestamp",
            field=field_name,
        )
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise NoteFormatError(
                f"field '{field_name}' is not a valid date (YYYY-MM-DD): {value!r}",
                field=field_name,
            ) from exc
    raise NoteFormatError(
        f"field '{field_name}' must be a date, got {type(value).__name__}",
        field=field_name,
    )


_PLAIN_FORBIDDEN_START = set("-?:,[]{}#&*!|>'\"%@`")
_FLOW_FORBIDDEN_CHARS = ",[]{}"


def _is_plain_safe(s: str, *, in_flow: bool) -> bool:
    if not s or "\n" in s or s != s.strip():
        return False
    if ": " in s or " #" in s:
        return False
    if s[0] in _PLAIN_FORBIDDEN_START:
        return False
    if in_flow and any(c in s for c in _FLOW_FORBIDDEN_CHARS):
        return False
    try:
        return bool(yaml.safe_load(s) == s)
    except yaml.YAMLError:
        return False


def _scalar(s: str, *, in_flow: bool = False) -> str:
    """Render `s` as a plain YAML scalar when that round-trips, else quoted."""
    if _is_plain_safe(s, in_flow=in_flow):
        return s
    return json.dumps(s, ensure_ascii=False)


def _flow_list(items: tuple[str, ...]) -> str:
    return "[" + ", ".join(_scalar(item, in_flow=True) for item in items) + "]"


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def serialize(note: Note) -> bytes:
    """Serialize `note` into the one canonical byte representation (ADR-0005)."""
    lines = [
        _DELIMITER,
        f"id: {_scalar(note.id)}",
        f"title: {_scalar(note.title)}",
        f"description: {_scalar(note.description)}",
        f"type: {_scalar(note.type)}",
    ]
    if note.tags:
        lines.append(f"tags: {_flow_list(note.tags)}")
    if note.aliases:
        lines.append(f"aliases: {_flow_list(note.aliases)}")
    lines.append(f"created: {_format_timestamp(note.created)}")
    lines.append(f"updated: {_format_timestamp(note.updated)}")
    if note.valid_from is not None:
        lines.append(f"valid_from: {note.valid_from.isoformat()}")
    if note.valid_to is not None:
        lines.append(f"valid_to: {note.valid_to.isoformat()}")
    if note.supersedes:
        lines.append(f"supersedes: {_flow_list(note.supersedes)}")
    if note.source is not None:
        lines.append(f"source: {_scalar(note.source)}")
    lines.append(_DELIMITER)

    body = note.body
    if not body.endswith("\n"):
        body += "\n"

    header = "\n".join(lines) + "\n"
    return (header + body).encode("utf-8")


def version(data: bytes) -> str:
    """Compute the `if_version` token for note bytes: lower-case hex SHA-256."""
    return hashlib.sha256(data).hexdigest()
