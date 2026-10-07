# SPDX-License-Identifier: AGPL-3.0-only
"""`memory_search`'s no-database fallback: term overlap over the vault (#30).

`scan_search` is what `mcp/server.py`'s `memory_search` falls back to when
`services.pool` is `None` - no `DATABASE_URL` configured, see `app.py`'s
module docstring ("full-text/vector search and `memory_search` degrade,
note read/write do not"). It walks the working copy directly instead of
querying Postgres: no stemming, no chunking, no fusion - just a
case-insensitive term-overlap score over each note's own fields, title and
aliases weighted higher than tags, description and body. Good enough to
keep stdio usable without Postgres; not a substitute for `search.hybrid_search`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from memory_manager.search import SearchFilters
from memory_manager.vault.note import Note, NoteFormatError, parse
from memory_manager.vault.paths import NotePath, PathRejected, iter_md_files, parse_note_path

__all__ = ["ScanHit", "scan_search"]

_WORD_RE = re.compile(r"[a-z0-9]+")
_TITLE_ALIAS_WEIGHT = 3.0
_TAG_WEIGHT = 2.0
_FIELD_WEIGHT = 1.0
_SNIPPET_CHARS = 240


@dataclass(frozen=True)
class ScanHit:
    """One note `scan_search` scored above zero."""

    note: Note
    path: str
    snippet: str
    score: float


def scan_search(
    vault_root: Path, query: str, *, filters: SearchFilters, limit: int
) -> list[ScanHit]:
    """Score every note under `vault_root` by term overlap with `query`.

    A note-shaped file that fails to parse, or a file that is not
    note-shaped at all, is skipped silently - there is no `warning` channel
    in a search result the way `memory_index` has one. `filters` is applied
    identically to `search.hybrid_search`'s. Ranked best score first, ties
    broken by path; only notes scoring above zero are returned, at most
    `limit` of them. Never follows a symlink (`vault.paths.iter_md_files`):
    a symlink's target must never have its body read and surfaced in a
    snippet as if it were a note in the vault.
    """
    terms = _terms(query)
    if not terms:
        return []

    hits: list[ScanHit] = []
    for file in iter_md_files(vault_root):
        rel = "/".join(file.relative_to(vault_root).parts)
        try:
            note_path = parse_note_path(rel, allow_archive=True)
        except PathRejected:
            continue
        try:
            note = parse(file.read_bytes())
        except NoteFormatError:
            continue
        if not _passes_filters(note, note_path, filters):
            continue
        score, snippet = _score(note, terms)
        if score > 0:
            hits.append(ScanHit(note=note, path=rel, snippet=snippet, score=score))

    hits.sort(key=lambda hit: (-hit.score, hit.path))
    return hits[:limit]


def _terms(query: str) -> list[str]:
    return _WORD_RE.findall(query.lower())


def _passes_filters(note: Note, note_path: NotePath, filters: SearchFilters) -> bool:
    if note_path.archived and not filters.include_archived:
        return False
    if filters.types and note.type not in filters.types:
        return False
    if filters.tags and not set(filters.tags) <= set(note.tags):
        return False
    if filters.namespaces and note_path.namespace not in filters.namespaces:
        return False
    if filters.valid_at is not None:
        if note.valid_from is not None and note.valid_from > filters.valid_at:
            return False
        if note.valid_to is not None and note.valid_to < filters.valid_at:
            return False
    return True


def _count(terms: Sequence[str], haystack: str) -> int:
    lowered = haystack.lower()
    return sum(lowered.count(term) for term in terms)


def _score(note: Note, terms: Sequence[str]) -> tuple[float, str]:
    score = 0.0
    score += _TITLE_ALIAS_WEIGHT * _count(terms, note.title)
    score += _TITLE_ALIAS_WEIGHT * _count(terms, " ".join(note.aliases))
    score += _TAG_WEIGHT * _count(terms, " ".join(note.tags))
    score += _FIELD_WEIGHT * _count(terms, note.description)
    score += _FIELD_WEIGHT * _count(terms, note.body)

    snippet = note.description
    for line in note.body.splitlines():
        if line.strip() and any(term in line.lower() for term in terms):
            snippet = line.strip()
            break
    return score, snippet[:_SNIPPET_CHARS]
