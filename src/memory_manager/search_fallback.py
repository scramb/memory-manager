# SPDX-License-Identifier: AGPL-3.0-only
"""`memory_search`'s no-index fallback: term overlap, scored per note (#30, WP-18).

`scan_search` is what `mcp/server.py`'s `memory_search` falls back to when
`services.indexer` is `None` - no `DATABASE_URL` configured for the `git`
backend, see `app.py`'s module docstring ("full-text/vector search and
`memory_search` degrade, note read/write do not"). It walks the working
copy directly instead of querying Postgres: no stemming, no chunking, no
fusion - just a case-insensitive term-overlap score over each note's own
fields, title and aliases weighted higher than tags, description and body.
Good enough to keep stdio usable without Postgres; not a substitute for
`search.hybrid_search`.

`scan_notes` is the same scoring applied to notes already read into memory
instead of a vault working copy - `mcp/server.py`'s interim `memory_search`
path for the `postgres` backend (ADR-0007 §2) until #98 adds real indexing
there. Both share `_score_note`, the per-note filter/scoring core that
takes a bare `(path, bytes)` pair rather than a filesystem path, so neither
walking a vault nor reading `StorageBackend.list()`'s results has to know
how scoring itself works.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from memory_manager.search import SearchFilters
from memory_manager.storage.base import StoredNote
from memory_manager.vault.note import Note, NoteFormatError, parse
from memory_manager.vault.paths import NotePath, PathRejected, iter_md_files, parse_note_path

__all__ = ["ScanHit", "scan_notes", "scan_search"]

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

    `filters` is applied identically to `search.hybrid_search`'s. Ranked
    best score first, ties broken by path; only notes scoring above zero
    are returned, at most `limit` of them. Never follows a symlink
    (`vault.paths.iter_md_files`): a symlink's target must never have its
    body read and surfaced in a snippet as if it were a note in the vault.
    """
    terms = _terms(query)
    if not terms:
        return []

    hits: list[ScanHit] = []
    for file in iter_md_files(vault_root):
        rel = "/".join(file.relative_to(vault_root).parts)
        hit = _score_note(rel, file.read_bytes(), terms, filters)
        if hit is not None:
            hits.append(hit)
    return _ranked(hits, limit)


def scan_notes(
    notes: Iterable[StoredNote], query: str, *, filters: SearchFilters, limit: int
) -> list[ScanHit]:
    """`scan_search`'s scoring, applied to notes already read into memory.

    The interim `memory_search` fallback for the `postgres` backend
    (ADR-0007 §2, WP-18) until #98 adds real indexing there:
    `mcp/server.py` passes `await services.storage.list(include_archived=True)`
    (filtering to `filters.include_archived` happens below, same as
    `scan_search`'s own walk never skips an archived file upfront either) -
    O(n) in the number of notes the backend holds, since every one of them
    is parsed and scored on every call, same as `scan_search` does for
    every file under the vault.
    """
    terms = _terms(query)
    if not terms:
        return []

    hits: list[ScanHit] = []
    for note in notes:
        hit = _score_note(note.path, note.content, terms, filters)
        if hit is not None:
            hits.append(hit)
    return _ranked(hits, limit)


def _score_note(
    path: str, data: bytes, terms: Sequence[str], filters: SearchFilters
) -> ScanHit | None:
    """`scan_search`/`scan_notes`'s shared per-note core: filter, then score.

    A note-shaped file that fails to parse, or a path that is not
    note-shaped at all, is skipped silently (returns `None`) - there is no
    `warning` channel in a search result the way `memory_index` has one.
    """
    try:
        note_path = parse_note_path(path, allow_archive=True)
    except PathRejected:
        return None
    try:
        note = parse(data)
    except NoteFormatError:
        return None
    if not _passes_filters(note, note_path, filters):
        return None
    score, snippet = _score(note, terms)
    if score <= 0:
        return None
    return ScanHit(note=note, path=path, snippet=snippet, score=score)


def _ranked(hits: list[ScanHit], limit: int) -> list[ScanHit]:
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
