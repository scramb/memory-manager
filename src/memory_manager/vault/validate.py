# SPDX-License-Identifier: AGPL-3.0-only
"""Semantic validation of notes against ADR-0005.

`vault/note.py` only rejects structurally broken input (bad YAML, unknown
keys, wrong types). This module checks the semantic rules from the ADR-0005
"Frontmatter fields" table: length limits, enum membership, charset, date
ordering, ULID shape and the overall file size cap.

Validation collects every violation instead of stopping at the first one, so
a single error report can tell Claude everything that needs fixing. Messages
are returned to Claude by the MCP tools later, so each one says what is wrong
and what to do about it.
"""

import re
from dataclasses import dataclass

from memory_manager.vault.note import Note, parse, serialize
from memory_manager.vault.ulid import is_ulid

__all__ = [
    "MAX_FILE_BYTES",
    "NOTE_TYPES",
    "NoteInvalid",
    "ValidationIssue",
    "validate",
    "validate_bytes",
]

MAX_FILE_BYTES = 16384

NOTE_TYPES = ("user", "feedback", "project", "reference", "fact")

_MAX_TITLE_CHARS = 120
_MAX_DESCRIPTION_CHARS = 150
_MAX_TAGS = 20
_MAX_ALIASES = 20
_MAX_ALIAS_CHARS = 80
_MAX_SOURCE_CHARS = 200

_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


@dataclass(frozen=True)
class ValidationIssue:
    """A single rule violation found on a note.

    `field` names the offending frontmatter key, or `None` for a file-level
    issue (e.g. the size cap). `message` is written for Claude: what is
    wrong and what to change.
    """

    field: str | None
    message: str


class NoteInvalid(ValueError):
    """Raised by `validate`/`validate_bytes` when a note violates ADR-0005.

    `issues` holds every violation found, not just the first one.
    """

    def __init__(self, issues: tuple[ValidationIssue, ...]) -> None:
        self.issues = issues
        super().__init__("; ".join(issue.message for issue in issues))


def _is_single_line(value: str) -> bool:
    return "\n" not in value


def validate(
    note: Note,
    *,
    raw_size: int | None = None,
    expected_type: str | None = None,
) -> None:
    """Validate `note` against the ADR-0005 semantic rules.

    `raw_size` is the size in bytes of the note's file on disk; when omitted
    it is computed by serializing the note. `expected_type` is the note type
    implied by the note's path (`<namespace>/<type>/<slug>.md`, see #9); when
    given, it must match the frontmatter `type`.

    Raises `NoteInvalid` with every violation found, not just the first one.
    """
    issues: list[ValidationIssue] = []

    issues.extend(_check_title(note.title))
    issues.extend(_check_description(note.description))
    issues.extend(_check_type(note.type, expected_type))
    issues.extend(_check_tags(note.tags))
    issues.extend(_check_aliases(note.aliases))
    issues.extend(_check_timestamps(note))
    issues.extend(_check_valid_range(note))
    issues.extend(_check_supersedes(note.supersedes))
    issues.extend(_check_source(note.source))
    issues.extend(_check_id(note.id))

    size = raw_size if raw_size is not None else len(serialize(note))
    issues.extend(_check_size(size))

    if issues:
        raise NoteInvalid(tuple(issues))


def validate_bytes(data: bytes, *, expected_type: str | None = None) -> Note:
    """Parse `data` and validate the result against ADR-0005.

    Convenience for the write path: parse structural errors surface as
    `NoteFormatError` (from `vault.note`), semantic ones as `NoteInvalid`.
    """
    note = parse(data)
    validate(note, raw_size=len(data), expected_type=expected_type)
    return note


def _check_title(title: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if not _is_single_line(title):
        issues.append(
            ValidationIssue(
                "title",
                "title contains a newline, must be a single line - "
                "remove the line break and keep it on one line",
            )
        )
    length = len(title)
    if length < 1:
        issues.append(
            ValidationIssue(
                "title",
                f"title is {length} chars, must be 1-{_MAX_TITLE_CHARS} chars - "
                "set a non-empty title",
            )
        )
    elif length > _MAX_TITLE_CHARS:
        issues.append(
            ValidationIssue(
                "title",
                f"title is {length} chars, max {_MAX_TITLE_CHARS} - shorten it",
            )
        )
    return issues


def _check_description(description: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if not _is_single_line(description):
        issues.append(
            ValidationIssue(
                "description",
                "description contains a newline, must be a single line - "
                "remove the line break and keep it on one line",
            )
        )
    length = len(description)
    if length < 1:
        issues.append(
            ValidationIssue(
                "description",
                f"description is {length} chars, must be 1-{_MAX_DESCRIPTION_CHARS} chars - "
                "set a non-empty description",
            )
        )
    elif length > _MAX_DESCRIPTION_CHARS:
        issues.append(
            ValidationIssue(
                "description",
                f"description is {length} chars, max {_MAX_DESCRIPTION_CHARS} - shorten it",
            )
        )
    return issues


def _check_type(note_type: str, expected_type: str | None) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if note_type not in NOTE_TYPES:
        allowed = ", ".join(NOTE_TYPES)
        issues.append(
            ValidationIssue(
                "type",
                f"type '{note_type}' is not one of the allowed types ({allowed}) - "
                "use one of the allowed types",
            )
        )
    if expected_type is not None and note_type != expected_type:
        issues.append(
            ValidationIssue(
                "type",
                f"type '{note_type}' does not match the directory type '{expected_type}' - "
                "set type to match the directory, or move the note to the matching directory",
            )
        )
    return issues


def _check_tags(tags: tuple[str, ...]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if len(tags) > _MAX_TAGS:
        issues.append(
            ValidationIssue(
                "tags",
                f"tags has {len(tags)} entries, max {_MAX_TAGS} - remove some tags",
            )
        )
    seen: set[str] = set()
    duplicates: set[str] = set()
    for tag in tags:
        if tag in seen:
            duplicates.add(tag)
        seen.add(tag)
        if not _TAG_RE.match(tag):
            issues.append(
                ValidationIssue(
                    "tags",
                    f"tag '{tag}' does not match ^[a-z0-9][a-z0-9-]{{0,39}}$ - "
                    "use lower-case letters, digits and hyphens only",
                )
            )
    for duplicate in sorted(duplicates):
        issues.append(
            ValidationIssue(
                "tags",
                f"tag '{duplicate}' appears more than once - remove the duplicate",
            )
        )
    return issues


def _check_aliases(aliases: tuple[str, ...]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if len(aliases) > _MAX_ALIASES:
        issues.append(
            ValidationIssue(
                "aliases",
                f"aliases has {len(aliases)} entries, max {_MAX_ALIASES} - remove some aliases",
            )
        )
    for alias in aliases:
        if not _is_single_line(alias):
            issues.append(
                ValidationIssue(
                    "aliases",
                    f"alias '{alias}' contains a newline, must be a single line "
                    f"(1-{_MAX_ALIAS_CHARS} chars) - remove the line break",
                )
            )
            continue
        length = len(alias)
        if length < 1:
            issues.append(
                ValidationIssue(
                    "aliases",
                    f"alias is {length} chars, must be 1-{_MAX_ALIAS_CHARS} chars - "
                    "set a non-empty alias or remove it",
                )
            )
        elif length > _MAX_ALIAS_CHARS:
            issues.append(
                ValidationIssue(
                    "aliases",
                    f"alias '{alias}' is {length} chars, max {_MAX_ALIAS_CHARS} - shorten it",
                )
            )
    return issues


def _check_timestamps(note: Note) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if note.updated < note.created:
        issues.append(
            ValidationIssue(
                "updated",
                f"updated ({note.updated.isoformat()}) is before created "
                f"({note.created.isoformat()}); updated must be >= created - "
                "set updated to the created time or later",
            )
        )
    return issues


def _check_valid_range(note: Note) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if (
        note.valid_from is not None
        and note.valid_to is not None
        and note.valid_to < note.valid_from
    ):
        issues.append(
            ValidationIssue(
                "valid_to",
                f"valid_to ({note.valid_to.isoformat()}) is before valid_from "
                f"({note.valid_from.isoformat()}); valid_to must be >= valid_from - "
                "set valid_to to valid_from or later",
            )
        )
    return issues


def _check_supersedes(supersedes: tuple[str, ...]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for entry in supersedes:
        if not is_ulid(entry):
            issues.append(
                ValidationIssue(
                    "supersedes",
                    f"supersedes entry '{entry}' is not a valid ULID (expected 26 "
                    "Crockford-base32 chars, e.g. '01ARZ3NDEKTSV4RRFFQ69G5FAV') - "
                    "use the id of the note it supersedes",
                )
            )
    return issues


def _check_source(source: str | None) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if source is None:
        return issues
    if not _is_single_line(source):
        issues.append(
            ValidationIssue(
                "source",
                f"source '{source}' contains a newline, must be a single line "
                f"(1-{_MAX_SOURCE_CHARS} chars) - remove the line break",
            )
        )
    length = len(source)
    if length < 1:
        issues.append(
            ValidationIssue(
                "source",
                f"source is {length} chars, must be 1-{_MAX_SOURCE_CHARS} chars - "
                "set a non-empty source or remove the field",
            )
        )
    elif length > _MAX_SOURCE_CHARS:
        issues.append(
            ValidationIssue(
                "source",
                f"source is {length} chars, max {_MAX_SOURCE_CHARS} - shorten it",
            )
        )
    return issues


def _check_id(note_id: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if not is_ulid(note_id):
        issues.append(
            ValidationIssue(
                "id",
                f"id '{note_id}' is not a valid ULID (expected 26 Crockford-base32 "
                "chars, e.g. '01ARZ3NDEKTSV4RRFFQ69G5FAV') - set id to a valid ULID",
            )
        )
    return issues


def _check_size(size: int) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if size > MAX_FILE_BYTES:
        issues.append(
            ValidationIssue(
                None,
                f"note is {size} bytes, max {MAX_FILE_BYTES} - consolidate or split "
                "it into a new, linked note instead of appending",
            )
        )
    return issues
