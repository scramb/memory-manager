# SPDX-License-Identifier: AGPL-3.0-only
"""Shared plain-text/Markdown list parser (#49): one memory per line or bullet.

This is the main input path for the ChatGPT importer (a model-printed or
hand-pasted memory list, the one both vendors' own export FAQs point users
at when nothing else works) and the plain-text fallback both `import claude`
and `import chatgpt` fall back to when their input is not a zip or JSON
shape they recognize. `importers.claude` also reuses `parse_items` to split
Claude's prose-style `conversations_memory`/`project_memories` blocks into
one item per bullet or paragraph - same splitting and date-prefix rules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime

from memory_manager.importers.core import ImportItem, build_source

__all__ = [
    "ParsedItem",
    "build_body",
    "build_footer",
    "collect_from_text",
    "derive_title",
    "parse_items",
]

_FENCE_RE = re.compile(r"^\s*```")
_BULLET_RE = re.compile(r"^\s*(?:[-*]|\d+\.)\s+(.*\S)\s*$")
_HEADING_RE = re.compile(r"^\s*#{1,6}\s+\S")
_BRACKET_DATE_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2})\]\s*-?\s*")
_PLAIN_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s*-\s*")

_MAX_TITLE_WORDS = 8
_MAX_TITLE_CHARS = 120


@dataclass(frozen=True)
class ParsedItem:
    """One memory extracted from a text/Markdown list, before it becomes a note."""

    text: str
    created: datetime | None


def parse_items(text: str, *, join_lines: bool = False) -> list[ParsedItem]:
    """Split `text` into memories, one per line, bullet, or paragraph.

    Lines are grouped into blocks by blank lines; code fence marker lines
    (` ``` `) are dropped, keeping their content as plain text. Within a
    block, heading lines (`#`...) are dropped - they are structure, not a
    memory. If what remains has at least one bullet line (`-`, `*`, `1.`),
    each bullet starts a new item and any following non-bullet line in the
    same block is folded into it as a continuation, regardless of
    `join_lines`. A block with no bullet is one item per line by default
    (`join_lines=False` - ChatGPT's and the generic list's "one memory per
    line", no blank line required between two different memories); pass
    `join_lines=True` for Claude's prose `conversations_memory`/
    `project_memories` blocks, where an unbulleted block is one continuous
    paragraph and becomes a single item instead. Each resulting item's
    leading date tag - `[YYYY-MM-DD]`, `[YYYY-MM-DD] -` or `YYYY-MM-DD -`,
    the formats Anthropic's own prompt and ChatGPT's "Model Set Context"
    block use - is parsed off into `ParsedItem.created`.
    """
    lines = [line for line in text.replace("\r\n", "\n").split("\n") if not _FENCE_RE.match(line)]

    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line.strip() == "":
            if current:
                blocks.append(current)
                current = []
            continue
        current.append(line)
    if current:
        blocks.append(current)

    items: list[ParsedItem] = []
    for block in blocks:
        items.extend(_items_from_block(block, join_lines=join_lines))
    return items


def _items_from_block(block: list[str], *, join_lines: bool) -> list[ParsedItem]:
    content_lines = [line for line in block if not _HEADING_RE.match(line)]
    if not content_lines:
        return []

    bullet_matches = [_BULLET_RE.match(line) for line in content_lines]
    if not any(bullet_matches):
        if join_lines:
            joined = " ".join(line.strip() for line in content_lines)
            return _finalize_item(joined)
        result: list[ParsedItem] = []
        for line in content_lines:
            result.extend(_finalize_item(line.strip()))
        return result

    raw_items: list[str] = []
    buffer: str | None = None
    for line, bullet_match in zip(content_lines, bullet_matches, strict=True):
        if bullet_match:
            if buffer is not None:
                raw_items.append(buffer)
            buffer = bullet_match.group(1)
        elif buffer is not None:
            buffer = f"{buffer} {line.strip()}"
        # A non-bullet line before the first bullet (an intro sentence mixed
        # into a bulleted block) has nowhere to attach to and is dropped.
    if buffer is not None:
        raw_items.append(buffer)

    bullet_items: list[ParsedItem] = []
    for raw in raw_items:
        bullet_items.extend(_finalize_item(raw))
    return bullet_items


def _finalize_item(raw: str) -> list[ParsedItem]:
    stripped = raw.strip()
    if not stripped or _HEADING_RE.match(stripped):
        return []
    text, created = _split_date_prefix(stripped)
    if not text:
        return []
    return [ParsedItem(text=text, created=created)]


def _split_date_prefix(text: str) -> tuple[str, datetime | None]:
    for pattern in (_BRACKET_DATE_RE, _PLAIN_DATE_RE):
        match = pattern.match(text)
        if not match:
            continue
        try:
            parsed_date = date.fromisoformat(match.group(1))
        except ValueError:
            continue
        remainder = text[match.end() :].strip()
        if not remainder:
            continue
        created = datetime(parsed_date.year, parsed_date.month, parsed_date.day, tzinfo=UTC)
        return remainder, created
    return text, None


def derive_title(
    text: str, *, max_words: int = _MAX_TITLE_WORDS, max_len: int = _MAX_TITLE_CHARS
) -> str:
    """The first `max_words` words of `text`, cut to `max_len` chars if still too long."""
    words = text.split()
    title = " ".join(words[:max_words])
    if len(title) > max_len:
        title = title[:max_len].rstrip()
    return title or "Untitled"


def build_footer(provider_label: str, when: date) -> str:
    """The provenance line appended to every imported item's body."""
    return f"Imported from {provider_label} on {when.isoformat()}."


def build_body(text: str, *, provider_label: str, when: date) -> str:
    """`text` plus a blank line plus `build_footer` - the note body for one item."""
    return f"{text.strip()}\n\n{build_footer(provider_label, when)}"


def collect_from_text(
    text: str,
    *,
    namespace: str,
    type_: str,
    source_prefix: str,
    provider_label: str,
    today: date | None = None,
) -> list[ImportItem]:
    """Turn a plain text/Markdown list into one `ImportItem` per `parse_items` entry."""
    when = today or date.today()
    items: list[ImportItem] = []
    for position, parsed in enumerate(parse_items(text)):
        items.append(
            ImportItem(
                title=derive_title(parsed.text),
                body=build_body(parsed.text, provider_label=provider_label, when=when),
                description=None,
                type=type_,
                tags=(),
                aliases=(),
                created=parsed.created,
                source=build_source(source_prefix, f"line:{position}"),
                slug_hint=None,
                namespace=namespace,
            )
        )
    return items
