# SPDX-License-Identifier: AGPL-3.0-only
"""Split a note body into retrieval chunks along its ATX headings (#25).

`chunk_note` walks the Markdown body top to bottom, tracking a stack of ATX
headings (`#` .. `######`) to build a `heading_path` such as
"Setup > Database" for every section. Text inside fenced code blocks
(``` ``` `` or `~~~`) is never scanned for headings, so a heading-like line
inside a code sample stays plain text.

A section that is too long for one chunk is split again, first at
blank-line paragraph boundaries, then — if a single paragraph alone still
does not fit — hard-split at the last whitespace before the limit. Fenced
code blocks are kept whole unless the block by itself exceeds the limit.

`detect_lang` is a stopword-count heuristic, not a real language detector;
it only has to be good enough to tag a chunk as "de", "en" or unknown.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["Chunk", "chunk_note", "detect_lang"]

_HEADING_RE = re.compile(r"^ {0,3}(#{1,6}) +(.*?) *$")
_FENCE_CHARS = ("`", "~")
_MIN_FENCE_LEN = 3
_WHITESPACE_RE = re.compile(r"\s")


@dataclass(frozen=True)
class Chunk:
    """One retrieval chunk of a note, ready for embedding and indexing."""

    ord: int
    heading_path: str
    text: str
    lang: str | None


def chunk_note(title: str, body: str, *, max_chars: int = 1500) -> list[Chunk]:
    """Split `body` into `Chunk`s, prefixed with `title` and heading path.

    Text before the first heading becomes chunk 0 with an empty
    `heading_path`. A heading with no content (and no non-empty
    descendants) produces no chunk, but its title still appears in the
    `heading_path` of any content nested under it.
    """
    chunks: list[Chunk] = []
    for heading_path, lines in _split_sections(body):
        content = _join_trimmed(lines)
        if not content:
            continue
        for piece in _split_section(content, max_chars):
            text = _build_text(title, heading_path, piece)
            chunks.append(
                Chunk(
                    ord=len(chunks), heading_path=heading_path, text=text, lang=detect_lang(piece)
                )
            )
    return chunks


def _build_text(title: str, heading_path: str, content: str) -> str:
    if heading_path:
        return f"{title}\n{heading_path}\n\n{content}"
    return f"{title}\n\n{content}"


def _split_sections(body: str) -> list[tuple[str, list[str]]]:
    """Group `body`'s lines into (heading_path, content_lines) sections."""
    sections: list[tuple[str, list[str]]] = []
    stack: list[tuple[int, str]] = []
    current: list[str] = []
    in_fence = False
    fence_char = ""
    fence_len = 0

    for line in body.split("\n"):
        if in_fence:
            current.append(line)
            if _is_fence_close(line, fence_char, fence_len):
                in_fence = False
            continue

        opened = _fence_open(line)
        if opened is not None:
            fence_char, fence_len = opened
            in_fence = True
            current.append(line)
            continue

        match = _HEADING_RE.match(line)
        if match is not None:
            sections.append((_heading_path(stack), current))
            current = []
            level = len(match.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, match.group(2)))
            continue

        current.append(line)

    sections.append((_heading_path(stack), current))
    return sections


def _heading_path(stack: list[tuple[int, str]]) -> str:
    return " > ".join(title for _, title in stack)


def _fence_open(line: str) -> tuple[str, int] | None:
    stripped = line.lstrip()
    for char in _FENCE_CHARS:
        if stripped.startswith(char * _MIN_FENCE_LEN):
            run_len = len(stripped) - len(stripped.lstrip(char))
            return char, run_len
    return None


def _is_fence_close(line: str, fence_char: str, open_len: int) -> bool:
    stripped = line.strip()
    if len(stripped) < _MIN_FENCE_LEN or len(stripped) < open_len:
        return False
    return stripped == fence_char * len(stripped)


def _join_trimmed(lines: list[str]) -> str:
    start = 0
    end = len(lines)
    while start < end and lines[start].strip() == "":
        start += 1
    while end > start and lines[end - 1].strip() == "":
        end -= 1
    return "\n".join(lines[start:end])


def _split_section(content: str, max_chars: int) -> list[str]:
    if len(content) <= max_chars:
        return [content]

    pieces: list[str] = []
    group: list[list[str]] = []
    group_len = 0

    def flush() -> None:
        nonlocal group, group_len
        if group:
            pieces.append("\n\n".join("\n".join(para) for para in group))
        group = []
        group_len = 0

    for para in _split_paragraphs(content.split("\n")):
        para_text = "\n".join(para)
        if len(para_text) > max_chars:
            flush()
            pieces.extend(_hard_split(para_text, max_chars))
            continue

        added_len = len(para_text) if not group else group_len + 2 + len(para_text)
        if group and added_len > max_chars:
            flush()
            group = [para]
            group_len = len(para_text)
        else:
            group.append(para)
            group_len = added_len

    flush()
    return pieces


def _split_paragraphs(lines: list[str]) -> list[list[str]]:
    """Split `lines` on blank-line boundaries; a fenced block stays one piece."""
    paragraphs: list[list[str]] = []
    buf: list[str] = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        if line.strip() == "":
            if buf:
                paragraphs.append(buf)
                buf = []
            i += 1
            continue

        opened = _fence_open(line)
        if opened is not None:
            if buf:
                paragraphs.append(buf)
                buf = []
            fence_char, fence_len = opened
            fence_lines = [line]
            i += 1
            while i < n:
                fence_lines.append(lines[i])
                closed = _is_fence_close(lines[i], fence_char, fence_len)
                i += 1
                if closed:
                    break
            paragraphs.append(fence_lines)
            continue

        buf.append(line)
        i += 1

    if buf:
        paragraphs.append(buf)
    return paragraphs


def _hard_split(text: str, max_chars: int) -> list[str]:
    pieces: list[str] = []
    while len(text) > max_chars:
        split_at = None
        for m in _WHITESPACE_RE.finditer(text, 0, max_chars):
            split_at = m.start()
        if split_at is None:
            pieces.append(text[:max_chars])
            text = text[max_chars:]
        else:
            pieces.append(text[:split_at])
            text = text[split_at + 1 :]
    if text:
        pieces.append(text)
    return pieces


_DE_STOPWORDS = frozenset({"der", "die", "das", "und", "nicht", "ist", "ich", "mit", "für", "auf"})
_EN_STOPWORDS = frozenset({"the", "and", "is", "not", "with", "for", "of", "to", "in"})
_WORD_RE = re.compile(r"[a-zäöüß]+")
_MIN_LANG_HITS = 3


def detect_lang(text: str) -> str | None:
    """Guess "de" or "en" from stopword counts; `None` if the signal is weak."""
    lowered = text.lower()
    words = _WORD_RE.findall(lowered)
    de_hits = sum(1 for word in words if word in _DE_STOPWORDS)
    en_hits = sum(1 for word in words if word in _EN_STOPWORDS)
    if any(char in "äöüß" for char in lowered):
        de_hits += 1

    if de_hits + en_hits < _MIN_LANG_HITS:
        return None
    if de_hits > en_hits:
        return "de"
    if en_hits > de_hits:
        return "en"
    return None
