# SPDX-License-Identifier: AGPL-3.0-only
"""The MCP tools exposed over stdio (and, later, HTTP) (#17, #18, #19).

`build_server` assembles an `mcp.server.mcpserver.MCPServer` around a
`memory_manager.app.Services`: `memory_index` is the "table of contents" a
client reads first, `memory_read` fetches the notes it picked by path or id,
`memory_write`/`memory_edit` write through `Services.queue`
(`memory_manager.queue.WriteQueue`), and `memory_supersede`/`memory_archive`
retire a note without ever deleting it (CLAUDE.md "Never hard-delete notes")
- all five surface a conflict as an error result carrying the current
content and version instead of ever overwriting silently (CLAUDE.md "Never
overwrite silently"). `memory_search` (#30) ranks notes with
`memory_manager.search.hybrid_search` when a database is configured,
falling back to `memory_manager.search_fallback.scan_search` over the plain
working copy otherwise. `memory_index`/`memory_read`/`memory_search` all
narrow their namespace handling through `mcp/authz.py`'s
`readable_namespaces` hook, and every tool calls `mcp/authz.py`'s
`require_scope` (write tools also `require_writable_namespace`) before
doing anything else - both read a static token's scopes/namespaces off the
current request (#34), and both are a no-op in stdio mode, which has no
token at all. `INSTRUCTIONS` (the server's `instructions`, sent on
every connection) and the `memory_guide` prompt registered below both come
from `mcp/instructions.py` (#20); the error mapping every tool here uses
lives in `mcp/errors.py`.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, NotRequired, TypedDict

from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult, TextContent

from memory_manager.app import Services
from memory_manager.mcp.authz import (
    READ_SCOPE,
    WRITE_SCOPE,
    current_access_token,
    readable_namespaces,
    require_scope,
    require_writable_namespace,
    restrict_namespaces,
)
from memory_manager.mcp.errors import error_to_dict
from memory_manager.mcp.instructions import GUIDE, INSTRUCTIONS, TOOL_DATA_SENTENCE
from memory_manager.observability import instrument_tool
from memory_manager.queue import NotFound, WriteError, WriteRequest
from memory_manager.search import NoteHit, SearchFilters, hybrid_search
from memory_manager.search_fallback import ScanHit, scan_search
from memory_manager.vault.note import Note, NoteFormatError, parse, serialize, version
from memory_manager.vault.paths import NotePath, PathRejected, parse_note_path, resolve
from memory_manager.vault.ulid import is_ulid, new_ulid
from memory_manager.vault.validate import NOTE_TYPES

__all__ = ["INSTRUCTIONS", "build_server", "current_client"]

_logger = logging.getLogger(__name__)

_MAX_READ_ITEMS = 20
_INDEX_SOFT_CAP_CHARS = 100_000

_MIN_SEARCH_LIMIT = 1
_MAX_SEARCH_LIMIT = 25
_DEFAULT_SEARCH_LIMIT = 8
_VALID_AT_TODAY = "today"

_NEW_VERSION = "new"
_PLACEHOLDER_ID = "new"
_PLACEHOLDER_TIMESTAMP = "1970-01-01T00:00:00Z"

# Known to `vault.repo.author_for`; what a stdio session commits as, and what
# any static-token-authenticated HTTP request commits as too (`current_client`).
_DEFAULT_CLIENT = "claude-code"
_CLIENT_ENV_VAR = "MEMORY_CLIENT"


class MemoryIndexEntry(TypedDict):
    """One row of the `memory_index` table of contents."""

    path: str
    id: NotRequired[str]
    title: NotRequired[str]
    description: NotRequired[str]
    type: NotRequired[str]
    tags: NotRequired[list[str]]
    updated: NotRequired[str]
    warning: NotRequired[str]


class MemoryReadItem(TypedDict):
    """One result of `memory_read`: either a note's content, or `item`+`error`."""

    path: NotRequired[str]
    id: NotRequired[str]
    version: NotRequired[str]
    content: NotRequired[str]
    item: NotRequired[str]
    error: NotRequired[dict[str, object]]


class MemorySearchResult(TypedDict):
    """One ranked note from `memory_search`."""

    id: str
    path: str
    title: str
    description: str
    type: str
    tags: list[str]
    snippet: str
    score: float


class MemorySearchResponse(TypedDict):
    """The result of `memory_search`: ranked notes plus how they were ranked."""

    results: list[MemorySearchResult]
    mode: str


class MemoryWriteResult(TypedDict):
    """The result of a successful `memory_write` or `memory_edit`."""

    path: str
    id: str
    version: str
    commit: str


class _SupersedingNote(TypedDict):
    """The new note side of a successful `memory_supersede`."""

    path: str
    id: str
    version: str


class _SupersededNote(TypedDict):
    """The old note side of a successful `memory_supersede`: unchanged in place."""

    path: str
    version: str
    valid_to: str


class MemorySupersedeResult(TypedDict):
    """The result of a successful `memory_supersede`."""

    new: _SupersedingNote
    old: _SupersededNote
    commit: str


class MemoryArchiveResult(TypedDict):
    """The result of a successful `memory_archive`."""

    archived_path: str
    version: str
    commit: str


def current_client() -> str:
    """The client identity a write through this process commits as.

    An HTTP request carrying a static token always commits as
    `"claude-code"`, regardless of the token's own name or `MEMORY_CLIENT` -
    static tokens authenticate human-driven clients (Claude Code, CI), not a
    separate committer identity (#34). Stdio mode has no token at all, so it
    commits as `MEMORY_CLIENT` (default `"claude-code"`) - one of
    `vault.repo.author_for`'s known clients.
    """
    if current_access_token() is not None:
        return _DEFAULT_CLIENT
    return os.environ.get(_CLIENT_ENV_VAR, _DEFAULT_CLIENT)


# Each tool's description is built from `TOOL_DATA_SENTENCE` rather than repeating the
# sentence as a second hardcoded copy, so the one rule every tool carries can never drift
# from `INSTRUCTIONS`/`GUIDE`'s wording of it. Passed explicitly as `@mcp.tool(description=...)`
# (an f-string cannot be a function's docstring - only a literal string/bytes constant is);
# each function keeps a short plain docstring of its own for readers of this module.
_MEMORY_INDEX_DESCRIPTION = f"""List every note in the vault: the table of contents to read first.

{TOOL_DATA_SENTENCE}
Returns one entry per note (id, path, title, description, type, tags, updated),
sorted by path. `namespace` and `type` filter to an exact match; archived notes are
excluded unless `include_archived` is set. A note that fails to parse is reported as
a `warning` entry instead of being silently dropped. Pass the `path` or `id` of the
entries you need to `memory_read`."""

_MEMORY_READ_DESCRIPTION = f"""Read one or more notes by vault path or id.

{TOOL_DATA_SENTENCE}
Each entry in `items` is either a note's vault path (e.g.
'personal/fact/favorite-color.md') or its ULID `id`. Returns one result per item, in
the same order: `{{path, id, version, content}}` on success, `{{item, error}}` if that one
item failed - a bad path or an unknown id never fails the whole call. At most 20 items
per call."""

_MEMORY_SEARCH_DESCRIPTION = f"""Search notes by `query`, ranked best match first.

{TOOL_DATA_SENTENCE}
Search before asserting facts about the user - results are pointers, not full
content: read a note with `memory_read` before relying on its details.

`types` is any-of against the five note types ('user', 'feedback', 'project',
'reference', 'fact'); `tags` is all-of (a note must carry every tag listed);
`namespaces` is any-of. `valid_at` is a date ('YYYY-MM-DD', or the word 'today')
that excludes notes outside their `valid_from`/`valid_to` range. Archived notes
are excluded unless `include_archived` is set. `limit` is clamped to 1-25.

Returns `{{results: [{{id, path, title, description, type, tags, snippet, score}}],
mode}}`. `mode` is 'hybrid' when a vector index is configured, 'fulltext' when only
full-text search is available, and 'scan' when no database is configured at all -
a slower, best-effort fallback over the plain working copy that keeps this tool
usable without Postgres."""

_MEMORY_WRITE_DESCRIPTION = f"""Create or replace the note at `path`.

{TOOL_DATA_SENTENCE}
`content` must be a complete note file: a `---`-delimited YAML frontmatter block
followed by the Markdown body. Required frontmatter fields are `title` (1-120
chars), `description` (1-150 chars, shown in `memory_index`) and `type` (one of
'user', 'feedback', 'project', 'reference', 'fact' - must match the `<type>`
directory in `path`, which has the shape `<namespace>/<type>/<slug>.md`). Optional
fields: `tags`, `aliases`, `valid_from`, `valid_to`, `supersedes`, `source`.

`if_version` is the `version` a previous `memory_read` returned for this path, or
the literal 'new' to create a note that must not already exist. To create a note,
omit `id` or set it to 'new' - the server generates a ULID and sets `created`/
`updated` to now. To replace an existing note, echo back its `id`; `created` is
kept from the existing note and `updated` is always set to now, regardless of what
is sent - a client cannot forge either timestamp.

On success returns `{{path, id, version, commit}}`. On a conflict - `if_version` is
stale, or 'new' was used for a path that already exists - returns an error result
(`isError: true`) whose structured content carries `current_version` and
`current_content` to merge from and retry; the same shape is returned for an
invalid note or a secret found in the content. Never raises on a write failure a
client could act on."""

_MEMORY_EDIT_DESCRIPTION = f"""\
Replace one exact occurrence of `old_str` with `new_str` in the note at `path`.

{TOOL_DATA_SENTENCE}
`old_str` is matched against the note's raw file text (frontmatter and body) and
must occur exactly once; if it occurs zero or more than once, this errors with the
match count instead of guessing - include more surrounding context to make
`old_str` unique and retry. `if_version` is the `version` a previous `memory_read`
returned for this path.

On success returns `{{path, id, version, commit}}`. On a version conflict returns an
error result (`isError: true`) whose structured content carries `current_version`
and `current_content` to merge from and retry. Never raises on a write failure a
client could act on."""

_MEMORY_SUPERSEDE_DESCRIPTION = f"""\
Replace the note `old` with a new note at `new_path`, keeping both.

{TOOL_DATA_SENTENCE}
Use this when a fact changed and the old note's history should stay readable -
never `memory_write`/`memory_edit` a note into saying something different, since
that erases what it used to say. `old` is the old note's vault path or ULID `id`;
`if_version` is the `version` a previous `memory_read` returned for it.
`new_content` is a complete note file for the replacement (same shape
`memory_write` expects for a create); its `id`/`created`/`updated` are
generated/forced the same way, and the old note's id is added to its
`supersedes` list if not already there.

Both notes are committed together: the new note is written at `new_path`, and
the old note gets `valid_to` set to today (UTC, unless it already has an earlier
one) and `updated` set to now - it stays exactly where it was, in full, never
deleted.

On success returns `{{new: {{path, id, version}}, old: {{path, version, valid_to}},
commit}}`. On a conflict - `if_version` is stale, `old` does not exist, or
`new_path` already exists - returns an error result (`isError: true`) whose
structured content carries enough to retry, the same way `memory_write` does."""

_MEMORY_ARCHIVE_DESCRIPTION = f"""\
Archive the note at `path`: move it to `_archive/`, never delete it.

{TOOL_DATA_SENTENCE}
Use this for a note that is obsolete or simply wrong, and nothing should replace
it - if a corrected version should take its place, use `memory_supersede` instead,
so the old content stays linked to what replaced it. `path` is the note's vault
path or ULID `id`; `if_version` is the `version` a previous `memory_read`
returned for it.

The archived note keeps its content, `id` and `created` exactly as they were;
only `updated` is set to now. It stays readable through `memory_read` at its new
`_archive/<namespace>/<type>/<slug>.md` path, but `memory_index` and search leave
it out unless archived notes are explicitly asked for.

On success returns `{{archived_path, version, commit}}`. On a conflict - `if_version`
is stale, or `path` does not exist (including: it is already archived) - returns
an error result (`isError: true`) whose structured content carries enough to
retry, the same way `memory_write` does."""


def build_server(
    services: Services,
    *,
    auth: AuthSettings | None = None,
    token_verifier: TokenVerifier | None = None,
) -> MCPServer:
    """Build the MCP server for `services`, with all memory tools and `memory_guide` registered.

    `auth`/`token_verifier` are `None` for stdio (no bearer auth at all) and
    for an HTTP server with no `DATABASE_URL` configured (#33's
    loopback-only mode); `http.py` builds both from
    `memory_manager.auth.verifier.StaticTokenVerifier` whenever a database is
    configured (#34). Passed straight to `MCPServer`, which is what actually
    wires the SDK's bearer-auth middleware into `streamable_http_app()` -
    nothing in this module reads either one directly; every tool below gets
    the per-request token through `mcp/authz.py`'s `get_access_token()`
    instead.
    """
    mcp = MCPServer(
        name="memory-manager", instructions=INSTRUCTIONS, auth=auth, token_verifier=token_verifier
    )

    @mcp.prompt(name="memory_guide")
    def memory_guide() -> str:
        """The long-form usage guide: workflow, examples, and what never to store."""
        return GUIDE

    @mcp.tool(description=_MEMORY_INDEX_DESCRIPTION)
    @instrument_tool("memory_index")
    async def memory_index(
        namespace: str | None = None,
        type: str | None = None,
        include_archived: bool = False,
        ctx: Context | None = None,
    ) -> list[MemoryIndexEntry]:
        """List every note in the vault: the table of contents to read first."""
        require_scope(READ_SCOPE)
        readable = readable_namespaces(ctx)
        entries = [
            entry
            for entry in _index_entries(services.vault_root)
            if _matches(
                entry,
                namespace=namespace,
                type=type,
                include_archived=include_archived,
                readable=readable,
            )
        ]
        return _cap_index(entries)

    @mcp.tool(description=_MEMORY_READ_DESCRIPTION)
    @instrument_tool("memory_read")
    async def memory_read(items: list[str], ctx: Context | None = None) -> list[MemoryReadItem]:
        """Read one or more notes by vault path or id."""
        require_scope(READ_SCOPE)
        if len(items) > _MAX_READ_ITEMS:
            raise ToolError(
                f"memory_read accepts at most {_MAX_READ_ITEMS} items, got {len(items)}"
            )
        readable = readable_namespaces(ctx)
        return _read_items(services.vault_root, items, readable=readable)

    @mcp.tool(description=_MEMORY_SEARCH_DESCRIPTION)
    @instrument_tool("memory_search")
    async def memory_search(
        query: str,
        types: list[str] | None = None,
        tags: list[str] | None = None,
        namespaces: list[str] | None = None,
        valid_at: str | None = None,
        include_archived: bool = False,
        limit: int = _DEFAULT_SEARCH_LIMIT,
        ctx: Context | None = None,
    ) -> MemorySearchResponse:
        """Search notes by `query`, ranked best match first."""
        require_scope(READ_SCOPE)
        if types is not None:
            unknown = sorted(set(types) - set(NOTE_TYPES))
            if unknown:
                raise ToolError(
                    f"memory_search got unknown type(s) {unknown}, "
                    f"expected one of: {', '.join(NOTE_TYPES)}"
                )

        parsed_valid_at = _parse_valid_at(valid_at)
        clamped_limit = max(_MIN_SEARCH_LIMIT, min(limit, _MAX_SEARCH_LIMIT))
        readable = readable_namespaces(ctx)
        effective_namespaces = restrict_namespaces(namespaces, readable)
        mode = _search_mode(services)

        # `effective_namespaces == []` (as opposed to `None`) means the caller may read
        # none of the namespaces it asked for (or none at all, once `readable_namespaces`
        # can narrow things) - fail closed and never run a query, in any mode: an empty
        # `SearchFilters.namespaces` means "no filter", so building one from `[]` here
        # would silently turn deny-all into allow-all (#30).
        if effective_namespaces is not None and not effective_namespaces:
            return {"results": [], "mode": mode}

        filters = SearchFilters(
            types=tuple(types) if types else (),
            tags=tuple(tags) if tags else (),
            namespaces=tuple(effective_namespaces) if effective_namespaces is not None else (),
            valid_at=parsed_valid_at,
            include_archived=include_archived,
        )

        if services.pool is not None:
            note_hits = await hybrid_search(
                services.pool,
                query,
                provider=services.provider,
                filters=filters,
                limit=clamped_limit,
            )
            results = [_note_hit_result(hit) for hit in note_hits]
        else:
            scan_hits = scan_search(
                services.vault_root, query, filters=filters, limit=clamped_limit
            )
            results = [_scan_hit_result(hit) for hit in scan_hits]

        return {"results": results, "mode": mode}

    @mcp.tool(description=_MEMORY_WRITE_DESCRIPTION)
    @instrument_tool("memory_write")
    async def memory_write(
        path: str,
        content: str,
        if_version: str,
        message: str | None = None,
    ) -> Annotated[CallToolResult, MemoryWriteResult]:
        """Create or replace the note at `path`."""
        require_scope(WRITE_SCOPE)
        require_writable_namespace(path)
        try:
            prepared = _prepare_write_content(services.vault_root, path, content, if_version)
        except NoteFormatError as exc:
            return _error_result(exc)

        request = WriteRequest(
            op="write",
            path=path,
            client=current_client(),
            if_version=if_version,
            content=prepared.content,
            message=message,
        )
        try:
            result = await services.queue.submit(request)
        except WriteError as exc:
            return _error_result(exc)

        return _ok_result(
            {
                "path": result.path,
                "id": prepared.id,
                "version": result.version,
                "commit": result.commit,
            }
        )

    @mcp.tool(description=_MEMORY_EDIT_DESCRIPTION)
    @instrument_tool("memory_edit")
    async def memory_edit(
        path: str,
        old_str: str,
        new_str: str,
        if_version: str,
        message: str | None = None,
    ) -> Annotated[CallToolResult, MemoryWriteResult]:
        """Replace one exact occurrence of `old_str` with `new_str` in the note at `path`."""
        require_scope(WRITE_SCOPE)
        require_writable_namespace(path)
        request = WriteRequest(
            op="edit",
            path=path,
            client=current_client(),
            if_version=if_version,
            old_str=old_str,
            new_str=new_str,
            message=message,
        )
        try:
            result = await services.queue.submit(request)
        except WriteError as exc:
            return _error_result(exc)

        note_id = parse(resolve(services.vault_root, result.path).read_bytes()).id
        return _ok_result(
            {"path": result.path, "id": note_id, "version": result.version, "commit": result.commit}
        )

    @mcp.tool(description=_MEMORY_SUPERSEDE_DESCRIPTION)
    @instrument_tool("memory_supersede")
    async def memory_supersede(
        old: str,
        new_path: str,
        new_content: str,
        if_version: str,
        message: str | None = None,
    ) -> Annotated[CallToolResult, MemorySupersedeResult]:
        """Replace the note `old` with a new note at `new_path`, keeping both."""
        require_scope(WRITE_SCOPE)
        resolved_old = _resolve_path_or_id(services.vault_root, old)
        if resolved_old is None:
            return _error_result(NotFound(old))
        require_writable_namespace(resolved_old)
        require_writable_namespace(new_path)

        try:
            prepared = _prepare_write_content(
                services.vault_root, new_path, new_content, _NEW_VERSION
            )
        except NoteFormatError as exc:
            return _error_result(exc)

        request = WriteRequest(
            op="supersede",
            path=resolved_old,
            new_path=new_path,
            client=current_client(),
            if_version=if_version,
            content=prepared.content,
            message=message,
        )
        try:
            result = await services.queue.submit(request)
        except WriteError as exc:
            return _error_result(exc)

        old_version = (result.related or {}).get(resolved_old, "")
        old_note = parse(
            resolve(services.vault_root, resolved_old, allow_archive=True).read_bytes()
        )
        valid_to = old_note.valid_to.isoformat() if old_note.valid_to is not None else ""
        return _ok_result(
            {
                "new": {"path": result.path, "id": prepared.id, "version": result.version},
                "old": {"path": resolved_old, "version": old_version, "valid_to": valid_to},
                "commit": result.commit,
            }
        )

    @mcp.tool(description=_MEMORY_ARCHIVE_DESCRIPTION)
    @instrument_tool("memory_archive")
    async def memory_archive(
        path: str,
        if_version: str,
        message: str | None = None,
    ) -> Annotated[CallToolResult, MemoryArchiveResult]:
        """Archive the note at `path`: move it to `_archive/`, never delete it."""
        require_scope(WRITE_SCOPE)
        resolved = _resolve_path_or_id(services.vault_root, path)
        if resolved is None:
            return _error_result(NotFound(path))
        require_writable_namespace(resolved)

        request = WriteRequest(
            op="archive",
            path=resolved,
            client=current_client(),
            if_version=if_version,
            message=message,
        )
        try:
            result = await services.queue.submit(request)
        except WriteError as exc:
            return _error_result(exc)

        return _ok_result(
            {"archived_path": result.path, "version": result.version, "commit": result.commit}
        )

    return mcp


@dataclass(frozen=True)
class _VaultNote:
    """One `*.md` file under the vault root that is shaped like a note path."""

    rel: str
    note_path: NotePath
    note: Note | None
    error: NoteFormatError | None


def _iter_vault_notes(vault_root: Path) -> Iterator[_VaultNote]:
    """Every note-shaped file in the vault, in path order, parsed best-effort.

    A path that is not note-shaped at all (e.g. a stray `README.md`) is
    skipped silently, exactly like `Indexer._discover_paths`; a note-shaped
    path that fails to parse is still yielded, with `note=None` and `error`
    set, so callers can report it instead of dropping it.
    """
    for file in sorted(vault_root.rglob("*.md")):
        rel_parts = file.relative_to(vault_root).parts
        if ".git" in rel_parts:
            continue
        rel = "/".join(rel_parts)
        try:
            note_path = parse_note_path(rel, allow_archive=True)
        except PathRejected:
            continue
        try:
            note = parse(file.read_bytes())
        except NoteFormatError as exc:
            yield _VaultNote(rel=rel, note_path=note_path, note=None, error=exc)
            continue
        yield _VaultNote(rel=rel, note_path=note_path, note=note, error=None)


def _index_entries(vault_root: Path) -> list[MemoryIndexEntry]:
    entries: list[MemoryIndexEntry] = []
    for vault_note in _iter_vault_notes(vault_root):
        if vault_note.note is None:
            entries.append(
                {"path": vault_note.rel, "warning": f"failed to parse: {vault_note.error}"}
            )
            continue
        note = vault_note.note
        entries.append(
            {
                "id": note.id,
                "path": vault_note.rel,
                "title": note.title,
                "description": note.description,
                "type": note.type,
                "tags": list(note.tags),
                "updated": note.updated.isoformat(),
            }
        )
    return entries


def _matches(
    entry: MemoryIndexEntry,
    *,
    namespace: str | None,
    type: str | None,
    include_archived: bool,
    readable: set[str] | None = None,
) -> bool:
    try:
        note_path = parse_note_path(entry["path"], allow_archive=True)
    except PathRejected:  # pragma: no cover - entries always come from a parsed path
        return False
    if note_path.archived and not include_archived:
        return False
    if namespace is not None and note_path.namespace != namespace:
        return False
    if readable is not None and note_path.namespace not in readable:
        return False
    return not (type is not None and note_path.type != type)


def _cap_index(entries: list[MemoryIndexEntry]) -> list[MemoryIndexEntry]:
    """Drop every field but `id`/`path`/`description` once the listing is too large.

    A single note's title/tags/updated rarely tip the balance; the vault as
    a whole can - this keeps `memory_index` usable as a table of contents
    even for a large vault instead of failing outright.
    """
    if len(json.dumps(entries, ensure_ascii=False)) <= _INDEX_SOFT_CAP_CHARS:
        return entries

    slimmed: list[MemoryIndexEntry] = []
    for entry in entries:
        if "warning" in entry:
            slimmed.append(entry)
            continue
        slim: MemoryIndexEntry = {
            "path": entry["path"],
            "warning": "full index exceeded the size cap, extra fields dropped",
        }
        if "id" in entry:
            slim["id"] = entry["id"]
        if "description" in entry:
            slim["description"] = entry["description"]
        slimmed.append(slim)
    return slimmed


def _search_mode(services: Services) -> str:
    """Which ranking `memory_search` would use for `services`, without running a query.

    Computed up front so the deny-all short-circuit (`effective_namespaces == []`) can
    still report the `mode` a caller would otherwise have gotten, instead of skipping it
    along with the query.
    """
    if services.pool is None:
        return "scan"
    return "hybrid" if services.provider is not None else "fulltext"


def _parse_valid_at(value: str | None) -> date | None:
    """`memory_search`'s `valid_at` argument, parsed into a `date`.

    Accepts `None` (no filter), the literal word 'today', or an ISO
    'YYYY-MM-DD' date. Raises `ToolError` for anything else - a client
    input mistake, not a vault/queue conflict a retry could carry extra
    data for.
    """
    if value is None:
        return None
    if value == _VALID_AT_TODAY:
        return datetime.now(UTC).date()
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ToolError(
            f"memory_search got an invalid valid_at {value!r}, expected 'YYYY-MM-DD' or 'today'"
        ) from exc


def _note_hit_result(hit: NoteHit) -> MemorySearchResult:
    return {
        "id": hit.note_id,
        "path": hit.path,
        "title": hit.title,
        "description": hit.description,
        "type": hit.type,
        "tags": list(hit.tags),
        "snippet": hit.snippet,
        "score": hit.score,
    }


def _scan_hit_result(hit: ScanHit) -> MemorySearchResult:
    return {
        "id": hit.note.id,
        "path": hit.path,
        "title": hit.note.title,
        "description": hit.note.description,
        "type": hit.note.type,
        "tags": list(hit.note.tags),
        "snippet": hit.snippet,
        "score": hit.score,
    }


def _read_items(
    vault_root: Path, items: list[str], *, readable: set[str] | None = None
) -> list[MemoryReadItem]:
    id_map: dict[str, str] | None = None
    results: list[MemoryReadItem] = []

    for item in items:
        path = item
        if "/" not in item and is_ulid(item):
            if id_map is None:
                id_map = _build_id_map(vault_root)
            found = id_map.get(item)
            if found is None:
                results.append(
                    {
                        "item": item,
                        "error": {"error": "NotFound", "message": f"no note with id {item!r}"},
                    }
                )
                continue
            path = found

        if readable is not None and not _namespace_readable(path, readable):
            results.append(
                {
                    "item": item,
                    "error": {"error": "NotFound", "message": f"'{path}' does not exist"},
                }
            )
            continue

        try:
            disk_path = resolve(vault_root, path, allow_archive=True)
        except PathRejected as exc:
            results.append({"item": item, "error": error_to_dict(exc)})
            continue

        if not disk_path.exists():
            results.append(
                {
                    "item": item,
                    "error": {"error": "NotFound", "message": f"'{path}' does not exist"},
                }
            )
            continue

        data = disk_path.read_bytes()
        try:
            note = parse(data)
        except NoteFormatError as exc:
            results.append({"item": item, "error": error_to_dict(exc)})
            continue

        results.append(
            {
                "path": path,
                "id": note.id,
                "version": version(data),
                "content": data.decode("utf-8"),
            }
        )

    return results


def _namespace_readable(path: str, readable: set[str]) -> bool:
    """Whether `path`'s namespace is in `readable`, best-effort.

    A `path` that does not even parse is left to `resolve`'s own, more
    specific `PathRejected` - this only ever turns a readable check into a
    `NotFound` (never exposing whether the namespace exists), not into a
    `PathRejected` of its own.
    """
    try:
        note_path = parse_note_path(path, allow_archive=True)
    except PathRejected:
        return True
    return note_path.namespace in readable


def _build_id_map(vault_root: Path) -> dict[str, str]:
    """Every note's `id -> path`, scanned once for a `memory_read` call that needs it."""
    return {
        vault_note.note.id: vault_note.rel
        for vault_note in _iter_vault_notes(vault_root)
        if vault_note.note is not None
    }


def _resolve_path_or_id(vault_root: Path, item: str) -> str | None:
    """`item` as a vault path: itself if it already looks like one, else looked up by id.

    Used by `memory_supersede`/`memory_archive`, which accept either like
    `memory_read` does. Returns `None` if `item` is a ULID with no matching
    note, so the caller can report its own `NotFound` instead of handing the
    write queue a path that was never a real lookup.
    """
    if "/" not in item and is_ulid(item):
        return _build_id_map(vault_root).get(item)
    return item


def _ok_result(payload: Mapping[str, object]) -> CallToolResult:
    """A successful write-tool result, with `payload` as structured content."""
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload))], structured_content=payload
    )


def _error_result(exc: Exception) -> CallToolResult:
    """An `isError: true` tool result for `exc`, with `error_to_dict(exc)` as structured content.

    Never raised as a `ToolError`: a client that wants to retry a conflict needs
    `current_version`/`current_content` from the structured content, not just the text a
    crashed-looking exception would carry.
    """
    mapped = error_to_dict(exc)
    return CallToolResult(
        content=[TextContent(type="text", text=str(exc))], is_error=True, structured_content=mapped
    )


@dataclass(frozen=True)
class _PreparedWrite:
    """The canonical bytes `memory_write` submits, plus the `id` they carry."""

    content: bytes
    id: str


def _prepare_write_content(
    vault_root: Path, path: str, content: str, if_version: str
) -> _PreparedWrite:
    """Normalize `content`'s `id`/`created`/`updated` before it is submitted to the queue.

    On create (`if_version == "new"`): a missing `id`, or the literal `id: new`, is replaced
    with a freshly generated ULID; `created`/`updated` are always set to now, never taken
    from the client. On update: `updated` is always set to now; `created` is carried forward
    from the note currently at `path` when that can be read, left alone otherwise (an unsafe
    path or a missing/unparsable note is `WriteQueue.submit`'s error to raise, not this
    function's). `id` is never touched on update - a changed `id` is `WriteQueue.submit`'s
    "id must not change" `InvalidNote`, not a silent overwrite.

    Raises `NoteFormatError` if `content` does not parse structurally even after the create
    placeholders are inserted (e.g. a missing `title`/`description`/`type`) - the same
    exception `memory_write` maps through `error_to_dict`.
    """
    creating = if_version == _NEW_VERSION
    text = _patch_missing_create_fields(content) if creating else content
    note = parse(text.encode("utf-8"))

    now = datetime.now(UTC).replace(microsecond=0)
    if creating:
        note_id = new_ulid(now) if note.id == _PLACEHOLDER_ID else note.id
        note = replace(note, id=note_id, created=now, updated=now)
    else:
        current = _current_note(vault_root, path)
        if current is not None:
            note = replace(note, created=current.created)
        note = replace(note, updated=now)

    return _PreparedWrite(content=serialize(note), id=note.id)


def _patch_missing_create_fields(text: str) -> str:
    """Insert placeholder `id`/`created`/`updated` lines into `text`'s frontmatter if absent.

    Lets a client creating a note omit these entirely: `_prepare_write_content` overrides
    them with server-generated values right after parsing, so the placeholders inserted here
    only need to be syntactically valid YAML, never semantically correct. `text` is returned
    unchanged if it is not even shaped like `---\\n...\\n---\\n...` - `note.parse` reports that
    structural problem on its own terms.
    """
    lines = text.split("\n")
    if not lines or lines[0] != "---":
        return text
    closing = next((i for i in range(1, len(lines)) if lines[i] == "---"), None)
    if closing is None:
        return text

    present = {line.split(":", 1)[0].strip() for line in lines[1:closing] if ":" in line}
    missing = [
        f"{field}: {placeholder}"
        for field, placeholder in (
            ("id", _PLACEHOLDER_ID),
            ("created", _PLACEHOLDER_TIMESTAMP),
            ("updated", _PLACEHOLDER_TIMESTAMP),
        )
        if field not in present
    ]
    if not missing:
        return text
    return "\n".join([lines[0], *missing, *lines[1:]])


def _current_note(vault_root: Path, path: str) -> Note | None:
    """Best-effort read of the note currently at `path`, or `None` if it cannot be read.

    Used only so `_prepare_write_content` can carry `created` forward on an update; an
    unsafe path, a missing file or content that fails to parse all return `None` here and
    are left to `WriteQueue.submit`, which re-reads `path` itself as the one authoritative
    current state and raises the real error (`VersionConflict`, `InvalidNote`, ...).
    """
    try:
        disk_path = resolve(vault_root, path)
    except PathRejected:
        return None
    if not disk_path.exists():
        return None
    try:
        return parse(disk_path.read_bytes())
    except NoteFormatError:
        return None
