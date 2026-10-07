# SPDX-License-Identifier: AGPL-3.0-only
"""Source-independent core of the importers: `ImportItem` -> note bytes -> write.

Every importer (markdown today, `#49`'s Claude/ChatGPT export parsers later)
only has to produce a stream of `ImportItem`; everything from there on -
slug generation, title/description derivation, the 16 KB cap, within-run
deduplication, the existing-path/unchanged rule (CLAUDE.md "never overwrite
silently") and the actual write - lives here, once.

`to_note_bytes` turns a single item into note bytes; `run_import` drives a
whole batch through a `StorageBackend` (client `import`, ADR-0007 §1: every
write goes through the interface, never a backend's own internals directly),
reporting what happened without ever raising on a single bad item - a
rejected file is reported, not fatal.

`dedupe_against_vault` is the cross-run/cross-vault half of deduplication
(`#49`): since ADR-0005 stays frozen and the Claude/ChatGPT importers add no
`import_key`/`import_origin` frontmatter field, "has this memory already
been imported" is answered by scanning the backend's notes once for ones
whose `source` already starts with `import:`, not by a dedicated key.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from memory_manager.storage import StorageBackend, WriteError
from memory_manager.vault.note import Note, NoteFormatError, parse, serialize
from memory_manager.vault.paths import PathRejected, parse_note_path
from memory_manager.vault.secrets import SecretFound, check
from memory_manager.vault.ulid import new_ulid
from memory_manager.vault.validate import MAX_FILE_BYTES, NoteInvalid, validate_bytes

__all__ = [
    "ImportItem",
    "ImportItemRejected",
    "ImportReport",
    "build_source",
    "dedup_hash",
    "dedupe_against_vault",
    "normalize_for_dedup",
    "run_import",
    "scan_vault_import_hashes",
    "slugify",
    "to_note_bytes",
]

_MAX_DESCRIPTION_CHARS = 150
_MAX_SLUG_CHARS = 80
_IMPORT_CLIENT = "import"
# ADR-0005 "Frontmatter fields": `source` is 1-200 chars, single line.
_MAX_SOURCE_CHARS = 200
_SOURCE_ELLIPSIS = "…"

_UMLAUT_MAP = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}
_SLUG_SEPARATOR_RE = re.compile(r"[^a-z0-9]+")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.*\S)\s*$")
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s")

_IMPORT_SOURCE_PREFIX = "import:"
_DEDUP_PUNCTUATION_RE = re.compile(r"[^\w\s]", re.UNICODE)
_DEDUP_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class ImportItem:
    """One note-to-be, independent of where it came from.

    `title`/`description`/`created` are `None` when the source did not give
    one - `to_note_bytes` derives them. `slug_hint` is typically the source
    filename stem; `source` is the note's future `source` frontmatter field
    (ADR-0005) and doubles as the per-item identifier in `ImportReport`
    (e.g. `import:markdown:notes/todo.md`), so it must be unique within a
    run.
    """

    title: str | None
    body: str
    description: str | None
    type: str
    tags: tuple[str, ...]
    aliases: tuple[str, ...]
    created: datetime | None
    source: str
    slug_hint: str | None
    namespace: str


class ImportItemRejected(Exception):
    """`to_note_bytes` could not turn an `ImportItem` into note bytes."""


def build_source(prefix: str, identifier: str, *, max_len: int = _MAX_SOURCE_CHARS) -> str:
    """Join `prefix` and `identifier` into an ADR-0005 `source` value, shortened to fit.

    `prefix` names the importer and scheme, with its own trailing separator
    (e.g. `"import:markdown:"`); `identifier` is whatever distinguishes one
    imported item from another within that scheme - a relative file path
    for the Markdown importer, an export-internal id for `#49`'s later
    ones. `source` is capped at `max_len` chars (ADR-0005); a too-long
    result is not truncated from the end (the most specific, most useful
    part of a path or id is usually its tail) but marked with an ellipsis
    and cut from the front instead: `f"{prefix}…{identifier[-keep:]}"`.

    A `prefix` alone already at or beyond `max_len` falls back to a plain
    truncation - there is no room left for the ellipsis or any of
    `identifier`, so nothing clever is left to do.
    """
    full = f"{prefix}{identifier}"
    if len(full) <= max_len:
        return full
    keep = max_len - len(prefix) - len(_SOURCE_ELLIPSIS)
    if keep <= 0:
        return full[:max_len]
    return f"{prefix}{_SOURCE_ELLIPSIS}{identifier[-keep:]}"


@dataclass
class ImportReport:
    """What a `run_import` batch did, one entry per note path (or source ref)."""

    created: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    skipped_existing: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    flagged: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)


def slugify(text: str, *, max_len: int = _MAX_SLUG_CHARS) -> str:
    """Fold `text` into a valid ADR-0005 slug (`^[a-z0-9]+(-[a-z0-9]+)*$`).

    German umlauts are folded to their usual ASCII transliteration
    (ä -> ae, ö -> oe, ü -> ue, ß -> ss) before anything else is dropped, so
    they do not just lose their diaeresis via Unicode normalization.
    """
    folded = text.lower()
    for umlaut, replacement in _UMLAUT_MAP.items():
        folded = folded.replace(umlaut, replacement)
    ascii_only = unicodedata.normalize("NFKD", folded).encode("ascii", "ignore").decode("ascii")
    collapsed = _SLUG_SEPARATOR_RE.sub("-", ascii_only).strip("-")
    if len(collapsed) > max_len:
        collapsed = collapsed[:max_len].rstrip("-")
    return collapsed or "note"


def _first_heading(body: str) -> str | None:
    for line in body.splitlines():
        match = _HEADING_RE.match(line.strip())
        if match:
            return match.group(1).strip()
    return None


def _first_non_heading_line(body: str) -> str | None:
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or _HEADING_RE.match(stripped):
            continue
        return stripped
    return None


def _humanize_slug_hint(slug_hint: str) -> str:
    words = [word for word in re.split(r"[-_]+", slug_hint) if word]
    return " ".join(word.capitalize() for word in words) or "Untitled"


def _resolve_title(item: ImportItem) -> str:
    if item.title:
        return item.title
    heading = _first_heading(item.body)
    if heading:
        return heading
    if item.slug_hint:
        return _humanize_slug_hint(item.slug_hint)
    return "Untitled"


def _truncate_at_word_boundary(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    truncated = text[:max_len]
    boundary = truncated.rfind(" ")
    if boundary > 0:
        truncated = truncated[:boundary]
    return truncated.rstrip(" .,;:-")


def _derive_description(body: str, *, fallback: str) -> str:
    line = _first_non_heading_line(body)
    sentence = _SENTENCE_END_RE.split(line, maxsplit=1)[0].strip() if line else ""
    source_text = sentence or fallback
    return _truncate_at_word_boundary(source_text, _MAX_DESCRIPTION_CHARS)


def to_note_bytes(item: ImportItem, *, now: datetime | None = None) -> tuple[str, bytes, list[str]]:
    """Turn `item` into `(slug, note_bytes, flags)`.

    `slug` is the bare, not-yet-deduplicated slug derived from
    `item.slug_hint` (preferred - usually the source filename) or the
    resolved title. Raises `ImportItemRejected` if the resulting note would
    exceed the ADR-0005 16 KB file cap - the body is never silently
    truncated, the whole item is rejected instead so nothing is lost.
    """
    flags: list[str] = []
    title = _resolve_title(item)

    description = item.description
    if not description:
        description = _derive_description(item.body, fallback=title)
        flags.append("description-derived")

    tags = list(item.tags)
    if "description-derived" in flags and "needs-review" not in tags:
        tags.append("needs-review")

    created = item.created or (now or datetime.now(UTC))
    created = created.astimezone(UTC).replace(microsecond=0)

    note = Note(
        id=new_ulid(created),
        title=title,
        description=description,
        type=item.type,
        created=created,
        updated=created,
        body=item.body,
        tags=tuple(tags),
        aliases=item.aliases,
        source=item.source,
    )
    data = serialize(note)
    if len(data) > MAX_FILE_BYTES:
        raise ImportItemRejected(
            f"note would be {len(data)} bytes, max {MAX_FILE_BYTES} - too large, split it"
        )

    slug_source = item.slug_hint or title
    return slugify(slug_source), data, flags


def _body_hash(body: str) -> str:
    normalized = body.replace("\r\n", "\n").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def normalize_for_dedup(text: str) -> str:
    """Fold `text` for cross-run/cross-vault duplicate detection.

    Unicode-normalizes (NFKC), casefolds, drops every character that is
    neither a "word" character nor whitespace (bullets, brackets, trailing
    punctuation, inline markers such as `[stated]`), then collapses
    whitespace runs to a single space. Two items that only differ in how
    they happen to be decorated end up identical here, even though each
    still keeps its own, undisturbed text in the note body.
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    stripped = _DEDUP_PUNCTUATION_RE.sub(" ", folded)
    return _DEDUP_WHITESPACE_RE.sub(" ", stripped).strip()


def _first_paragraph(text: str) -> str:
    return text.strip().partition("\n\n")[0]


def dedup_hash(text: str) -> str:
    """The cross-run/cross-vault duplicate key for an item or note body.

    Taken from the first paragraph only: every Claude/ChatGPT import item's
    body is the memory text plus a blank line plus an "Imported from ... on
    ..." footer (`#49`), and that footer's date must never be the reason two
    runs of the same import look different.
    """
    return hashlib.sha256(normalize_for_dedup(_first_paragraph(text)).encode("utf-8")).hexdigest()


async def scan_vault_import_hashes(storage: StorageBackend) -> set[str]:
    """The `dedup_hash` of every note `storage` holds whose `source` is an import's.

    Lists the backend's notes once - never the Postgres index, ADR-0007 §1's
    "the storage backend is the source of truth" - so `dedupe_against_vault`
    can recognize a memory an earlier run (of this importer or a different
    one) already turned into a note, without a dedicated frontmatter key.
    """
    hashes: set[str] = set()
    for stored in await storage.list(include_archived=True):
        try:
            note = parse(stored.content)
        except NoteFormatError:
            continue
        if note.source is not None and note.source.startswith(_IMPORT_SOURCE_PREFIX):
            hashes.add(dedup_hash(note.body))
    return hashes


async def dedupe_against_vault(
    items: Iterable[ImportItem], storage: StorageBackend
) -> tuple[list[ImportItem], list[str]]:
    """Drop items whose normalized text already exists, in this run or in the vault.

    Returns `(kept, duplicate_sources)`. Within one run, the first item with
    a given normalized text wins and keeps its own (usually earliest) date -
    later occurrences are reported as duplicates, not written. Running the
    very same import again drops every item, because the vault scan already
    carries last run's hashes.
    """
    seen = await scan_vault_import_hashes(storage)
    kept: list[ImportItem] = []
    duplicates: list[str] = []
    for item in items:
        key = dedup_hash(item.body)
        if key in seen:
            duplicates.append(item.source)
            continue
        seen.add(key)
        kept.append(item)
    return kept, duplicates


def _dedupe_slug(base: str, used: set[str]) -> str:
    if base not in used:
        used.add(base)
        return base
    suffix = 2
    while True:
        tag = f"-{suffix}"
        candidate = f"{base[: _MAX_SLUG_CHARS - len(tag)].rstrip('-')}{tag}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        suffix += 1


async def run_import(
    items: Iterable[ImportItem], storage: StorageBackend, *, apply: bool
) -> ImportReport:
    """Drive every `item` through validation, dedup and `storage`.

    Dry-run (`apply=False`, the default everywhere above this function) does
    every check - slug/path validation, the secret scan, the existing-path
    comparison - but never calls `storage.write`, so nothing is written;
    `--apply` is the only thing that turns a "would create" into a commit.
    A single item failing any check is reported in `ImportReport.rejected`
    and does not stop the rest of the batch. `storage` is assumed already
    synced to the remote (the factory that built it did so on entry) -
    this function never syncs on its own.
    """
    report = ImportReport()
    seen_bodies: set[str] = set()
    seen_slugs: dict[tuple[str, str], set[str]] = defaultdict(set)

    for item in items:
        body_hash = _body_hash(item.body)
        if body_hash in seen_bodies:
            report.duplicates.append(item.source)
            continue
        seen_bodies.add(body_hash)

        try:
            bare_slug, note_bytes, flags = to_note_bytes(item)
        except ImportItemRejected as exc:
            report.rejected.append((item.source, str(exc)))
            continue

        slug = _dedupe_slug(bare_slug, seen_slugs[(item.namespace, item.type)])

        try:
            note_path = parse_note_path(f"{item.namespace}/{item.type}/{slug}.md")
        except PathRejected as exc:
            report.rejected.append((item.source, str(exc)))
            continue
        path = note_path.relative

        try:
            validate_bytes(note_bytes, expected_type=item.type)
        except (NoteFormatError, NoteInvalid) as exc:
            report.rejected.append((item.source, str(exc)))
            continue

        try:
            check(note_bytes.decode("utf-8"))
        except SecretFound as exc:
            report.rejected.append((item.source, str(exc)))
            continue

        current = await storage.read(path)
        if current is not None:
            if current.content == note_bytes:
                report.unchanged.append(path)
            else:
                report.skipped_existing.append(path)
            if flags:
                report.flagged.append((path, tuple(flags)))
            continue

        if apply:
            try:
                await storage.write(path, note_bytes, if_version="new", client=_IMPORT_CLIENT)
            except WriteError as exc:
                report.rejected.append((item.source, str(exc)))
                continue

        report.created.append(path)
        if flags:
            report.flagged.append((path, tuple(flags)))

    return report
