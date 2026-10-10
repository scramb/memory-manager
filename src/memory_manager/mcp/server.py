# SPDX-License-Identifier: AGPL-3.0-only
"""The MCP tools exposed over stdio (and, later, HTTP) (#17, #18, #19).

`build_server` assembles an `mcp.server.mcpserver.MCPServer` around a
`memory_manager.app.Services`: `memory_index` is the "table of contents" a
client reads first, `memory_read` fetches the notes it picked by path or id,
`memory_write`/`memory_edit` write through `Services.storage`
(`memory_manager.storage.StorageBackend`, ADR-0007 §1 - every tool below
calls only the interface, never a backend's own internals), and
`memory_supersede`/`memory_archive` retire a note without ever deleting it
(CLAUDE.md "Never hard-delete notes"), and `memory_promote` (#227) copies a
note from the caller's own namespace into a shared one, archiving the
original the same way `memory_archive` does unless `keep_original` is set
- every one of these write tools surfaces a conflict as an error result
carrying the current content and version instead of ever overwriting
silently (CLAUDE.md "Never overwrite silently"). `memory_search` (#30) ranks notes with
`memory_manager.search.hybrid_search` when `Services.indexer` is set - both
the `git` backend with `DATABASE_URL` configured and the `postgres` backend
(ADR-0007 §2/§4, WP-18: every write there indexes itself, so `Services.indexer`
is set from the first request on, never `None`) - and falls back to
`memory_manager.search_fallback.scan_search` over the plain working copy
only for `git` without a database at all.
`memory_index`/`memory_read`/`memory_search` all
narrow their namespace handling through `mcp/authz.py`'s
`readable_namespaces` hook, and every tool calls `mcp/authz.py`'s
`require_scope` (write tools also `require_writable_namespace`) before
doing anything else - both read a static token's scopes/namespaces off the
current request (#34), and both are a no-op in stdio mode, which has no
token at all. `INSTRUCTIONS` (the server's `instructions`, sent on
every connection) and the `memory_guide` prompt registered below both come
from `mcp/instructions.py` (#20); the error mapping every tool here uses
lives in `mcp/errors.py`.

When `Services.app_role` is set (`"postgres"`, ADR-0008 addendum, #101/#116),
every tool additionally goes through `mcp/namespaces.py`'s own, independent
enforcement of ADR-0008's permission matrix, on top of - never instead of -
RLS: `_resolve_namespaces` makes the one `namespaces.resolve` round trip a
tool call needs, every path/namespace argument is rewritten from `me` to the
caller's real personal-namespace alias before use and back to `me` in every
success and error result (`_to_stored_path`/`_to_display_path`,
`_rewrite_error_result`), and `_require_writable`/`_require_archive_access`/
`_require_promote_access` replace `mcp/authz.py`'s plain, token-namespace-only
`require_writable_namespace` with the full matrix, further narrowed by
whatever a static/OAuth token's own `namespaces` claim (ADR-0004) still
restricts (`_effective_readable`/`_effective_writable`). `"git"` mode
(`Services.app_role is None`) never runs any of this - every tool call below
is then exactly what it always was.

`quota_checker` (`memory_manager.quotas.QuotaChecker`, #242), given only by
`http.py`, additionally caps how many writes the calling user, namespace and
token may make per minute/day, held across replicas on the same shared state
`mcp/authz.py`'s rate limiters use - every write tool calls it, right after
its own namespace-permission check and before it ever reaches
`Services.storage`; `None` (stdio, or an HTTP server with no quota
configured) skips this entirely, same as always before #242.

`storage_quota_checker` (`memory_manager.quotas.StorageQuotaChecker`, #243),
also given only by `http.py` and only once `Services.storage` is a
`storage.postgres.PostgresBackend`, additionally caps how many notes and how
many bytes a namespace may hold in total - `memory_write`/`memory_edit`/
`memory_supersede` each call it with the namespace and byte size the write
they are about to submit would actually produce; `memory_archive` never does
(it only ever frees a note-count slot). `None` skips this entirely, same
"off unless configured" default as `quota_checker`.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import Annotated, Any, NotRequired, TypedDict

from mcp.server.auth.provider import OAuthAuthorizationServerProvider, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult, TextContent, ToolAnnotations

from memory_manager.app import Services
from memory_manager.compat.profiles import get_profile
from memory_manager.compat.select import (
    current_profile_override,
    reset_resolved_profile,
    resolve_profile,
    set_resolved_profile,
)
from memory_manager.db import rls
from memory_manager.mcp import namespaces
from memory_manager.mcp.authz import (
    READ_SCOPE,
    WRITE_SCOPE,
    current_access_token,
    readable_namespaces,
    require_scope,
    require_writable_namespace,
    restrict_namespaces,
    writable_namespaces,
)
from memory_manager.mcp.errors import error_to_dict
from memory_manager.mcp.instructions import GUIDE, INSTRUCTIONS, TOOL_DATA_SENTENCE
from memory_manager.observability import instrument_tool
from memory_manager.quotas import NamespaceKind, QuotaChecker, StorageQuotaChecker
from memory_manager.search import NoteHit, SearchFilters, hybrid_search
from memory_manager.search_fallback import ScanHit, scan_search
from memory_manager.storage import InvalidNote, NotFound, StorageBackend, WriteError
from memory_manager.vault.note import Note, NoteFormatError, parse, serialize
from memory_manager.vault.paths import NotePath, PathRejected, parse_note_path
from memory_manager.vault.ulid import is_ulid, new_ulid
from memory_manager.vault.validate import NOTE_TYPES

__all__ = ["INSTRUCTIONS", "build_server", "current_actor", "current_client"]

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

# Known to the Git backend's `Repo.author_for` as a committer name; what a
# stdio session commits as, and what any static-token-authenticated HTTP
# request commits as too (`current_client`).
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
    # Additive (ADR-0008, #102): the ADR-0008 kind of `path`'s namespace
    # ('personal'/'group'/'project'/'org'), or `None` on the Git backend
    # (`services.app_role is None` - no namespace registry at all).
    namespace_kind: str | None


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
    # Additive (ADR-0008, #102): the ADR-0008 kind of `path`'s namespace
    # ('personal'/'group'/'project'/'org'), or `None` on the Git backend
    # (`services.app_role is None` - no namespace registry at all).
    namespace_kind: str | None


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


class _PromotedCopy(TypedDict):
    """The new, shared-namespace side of a successful `memory_promote`."""

    path: str
    id: str
    version: str
    # Additive (ADR-0008, #102): the ADR-0008 kind of the target namespace
    # ('personal'/'group'/'project'/'org'), or `None` on the Git backend
    # (`services.app_role is None` - no namespace registry at all).
    namespace_kind: str | None


class _PromotedOriginal(TypedDict):
    """The original note's side of a successful `memory_promote`: its resulting path
    (the archive path, or the unchanged original `path` when `keep_original=True`)
    and version - never deleted, same as `memory_archive`/`memory_supersede`.
    """

    path: str
    version: str


class MemoryPromoteResult(TypedDict):
    """The result of a successful `memory_promote`."""

    new: _PromotedCopy
    original: _PromotedOriginal
    commit: str


def current_client() -> str:
    """The client identity a write through this process commits as.

    An OAuth access token (#36) carries its own committer identity in
    `claims["client_label"]` - `"claude-ai"` when the token's client
    registered a claude.ai/claude.com redirect URI, `"claude-code"`
    otherwise (`auth.provider`'s `_client_label_for`, set once at issuance,
    not re-derived here). A static token (#34) carries no such claim and
    always commits as `"claude-code"`, regardless of the token's own name or
    `MEMORY_CLIENT` - static tokens authenticate human-driven clients
    (Claude Code, CI), not a separate committer identity. Stdio mode has no
    token at all, so it commits as `MEMORY_CLIENT` (default `"claude-code"`)
    - one of the Git backend's `Repo.author_for`'s known clients.
    """
    token = current_access_token()
    if token is None:
        return os.environ.get(_CLIENT_ENV_VAR, _DEFAULT_CLIENT)
    if token.claims is not None and "client_label" in token.claims:
        return str(token.claims["client_label"])
    return _DEFAULT_CLIENT


class _ProfileMiddleware:
    """Resolves the `compat.select`/ADR-0010 client profile for every request (#131).

    `ServerMiddleware` (provisional in `mcp` 2.3.0, `mcp/server/context.py:146`, pinned -
    see this work package's own risk note) runs for every inbound message, including
    `initialize`, before any params validation (`mcp/server/runner.py:225`'s
    `_compose_server_middleware`): exactly the seam ADR-0010's resolution order needs, so
    it never has to live inside a tool handler.

    `clientInfo.name` is read two different ways depending on where in the handshake this
    request is, both read-only, neither going anywhere near the result:

    - `ctx.method == "initialize"` (the 2025-11-25-and-earlier handshake, still in
      flight): `ctx.connection.client_params` is **not** set yet - `runner.py` only
      commits it after this middleware chain returns - so the only place `clientInfo` is
      readable at all is the raw wire params, `ctx.params["clientInfo"]["name"]`.
    - every other request - the 2026-07-28 per-request envelope (`Connection.
      from_envelope` synthesizes `client_params` before any middleware ever runs, from
      that request's own `_meta`), or a post-handshake request on a session-based
      connection (stdio, or legacy HTTP, both committed by their own earlier
      `initialize`) - `ctx.session.client_params.client_info.name` already has it.

    The override side of ADR-0010's order combines `compat.select.
    current_profile_override()` (set by `http.py`'s ASGI middleware for one HTTP
    request - absent for stdio, which has no ASGI layer) with `stdio_profile` (given to
    `build_server`, for `serve --stdio --profile`) - whichever of the two a given process
    could ever have set.

    For `"full"` (`compat.select.require_deliverable`'s other deliverable mode, and the
    only one any profile registered in `compat/profiles.py` actually uses today), this
    middleware changes nothing about the request or the result it produces - `ctx` goes
    into `call_next` unchanged and whatever it returns comes straight back - so
    `default`'s behaviour stays byte-for-byte what it always was (conformance's own
    guarantee, not just this docstring's claim). It only ever sets `compat.select`'s
    *resolved-profile* contextvar, for `observability/metrics.py`'s `track_tool_call` to
    read, for the duration of `call_next`.

    For `"descriptions"` (#132: the usage rules folded into tool descriptions instead,
    for a client that drops `instructions` - no profile registered today uses it, the
    same "no behaviour change yet" as every other profile-shaped seam in this work
    package until one is), this middleware drops the `instructions` key from the result
    of exactly two methods: `initialize` (2025-11-25) and `server/discover` (2026-07-28)
    - the only two that carry one at all. By the time `call_next` returns, `runner.py`
    has already serialized either into a plain `dict` (`runner.py:202,223`), never the
    typed `InitializeResult`/`DiscoverResult` model, so `_without_instructions` below
    only ever has to drop a dict key, with nothing to re-validate afterwards. Every other
    method's result is untouched, the same as `"full"`.

    !!! warning
        Per `ServerMiddleware`'s own docstring: `initialize` is handled inline, with the
        transport's read loop parked until this chain returns - awaiting a
        server-initiated request from inside it would deadlock the connection. This
        middleware never sends anything to the client at all, so that risk never
        actually arises here, but any future addition to this class must keep it that
        way.
    """

    #: The only two methods whose result ever carries `instructions` (module
    #: docstring's `"descriptions"` paragraph) - `server/discover`'s own 2026-07-28 RPC
    #: name, not a method this server defines itself.
    _INSTRUCTIONS_METHODS = frozenset({"initialize", "server/discover"})

    def __init__(self, *, stdio_profile: str | None = None) -> None:
        if stdio_profile is not None:
            # Fail fast at server construction, not on the first request - mirrors
            # `compat.select.set_profile_override`'s own eager validation.
            get_profile(stdio_profile)
        self._stdio_profile = stdio_profile

    async def __call__(
        self, ctx: ServerRequestContext[Any, Any], call_next: CallNext
    ) -> HandlerResult:
        client_info_name = self._client_info_name(ctx)
        override = current_profile_override()
        if override is None:
            override = self._stdio_profile
        profile = resolve_profile(client_info_name, override=override)
        token = set_resolved_profile(profile.name)
        try:
            result = await call_next(ctx)
        finally:
            reset_resolved_profile(token)
        if profile.delivery_mode == "descriptions" and ctx.method in self._INSTRUCTIONS_METHODS:
            return self._without_instructions(result)
        return result

    @staticmethod
    def _without_instructions(result: HandlerResult) -> HandlerResult:
        """`result` with its `instructions` key removed, for the `"descriptions"`
        delivery mode (#132) - a no-op for anything but the plain `dict` `_inner` already
        produced (module docstring's `"descriptions"` paragraph) and for a dict that
        carries no `instructions` key at all (never reached today: both `initialize` and
        `server/discover` always set one, `build_server`'s own `instructions=INSTRUCTIONS`
        and `mcp`'s own `DiscoverResult.instructions` - kept anyway so this never raises on
        a `KeyError` if that ever changes).
        """
        if not isinstance(result, dict) or "instructions" not in result:
            return result
        return {key: value for key, value in result.items() if key != "instructions"}

    @staticmethod
    def _client_info_name(ctx: ServerRequestContext[Any, Any]) -> str | None:
        if ctx.method == "initialize":
            params = ctx.params or {}
            client_info = params.get("clientInfo")
            if isinstance(client_info, Mapping):
                name = client_info.get("name")
                if isinstance(name, str):
                    return name
            return None
        client_params = ctx.session.client_params
        if client_params is None:
            return None
        return client_params.client_info.name


_STDIO_ACTOR = "stdio"
# Mirrors `auth.verifier`'s own private `_CLIENT_ID_PREFIX` - a static
# token's `AccessToken.client_id` (not re-exported, so duplicated as a
# literal here rather than importing a private name).
_STATIC_CLIENT_ID_PREFIX = "static:"


def current_actor() -> str:
    """Who asked for the write this process is about to make, for the audit log (#39).

    An OAuth access token (#36) carries its own subject in
    `AccessToken.subject` - the human who logged in and authorized the
    client, independent of `current_client()`'s committer label. A static
    token (#34) carries no subject of its own; its `client_id` is
    `"static:<name>"` (`auth.verifier._verify_static_token`), so the token's
    name is what identifies it here instead. Stdio mode has no token at all,
    so it is always `"stdio"` - never guessed from `MEMORY_CLIENT`, which
    names the *committer* `current_client()` returns, not who is actually
    running the local session.
    """
    token = current_access_token()
    if token is None:
        return _STDIO_ACTOR
    if token.subject is not None:
        return token.subject
    return token.client_id.removeprefix(_STATIC_CLIENT_ID_PREFIX)


# Every tool's `ToolAnnotations` (#132, ADR-0010): the spec's four hints are identical
# across both protocol revisions this server speaks (schema/2025-11-25/schema.ts and
# schema/2026-07-28/schema.ts, docs/research/mcp-auth-and-connectors.md §1, retrieved
# 2026-10-10), so one pair of constants covers every tool below - never a code path that
# changes behaviour (ADR-0010 "profiles are data, not code that reaches into tool
# handlers" applies the same way to annotations: they describe the contract, they do not
# branch on it).
#
# Read tools (`memory_index`/`memory_read`/`memory_search`) never modify the vault at
# all: `readOnlyHint=True` makes `destructiveHint`/`idempotentHint` not meaningful per the
# spec's own note, set here anyway so every tool below carries all four explicitly rather
# than leaving two to the spec's own (different) defaults. `openWorldHint=False`: this
# tool's domain is the vault these notes live in, never an open world of external
# entities (the spec's own "a memory tool is not" example).
_READ_TOOL_ANNOTATIONS = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)

# Write tools (`memory_write`/`memory_edit`/`memory_supersede`/`memory_archive`/
# `memory_promote`) do modify the vault - `readOnlyHint=False`, explicit rather than left
# to the spec's own default of `False` - but never destructively: `memory_archive` moves
# a note to `_archive/`, never deletes it (CLAUDE.md "Never hard-delete notes"), and every
# other write tool here only ever adds a revision, so `destructiveHint=False`.
# `idempotentHint=True`: every one of these five tools requires `if_version`
# (`storage/rules.py:53-65`'s `check_version`) - an identical repeat of a call that
# already succeeded sees a version/path that no longer matches ("new" no longer means "it
# does not exist yet" once it does) and raises `VersionConflict`/`NotFound` instead,
# with no additional effect on the vault, exactly what `idempotentHint` promises.
# `openWorldHint=False`, same reasoning as the read tools above.
_WRITE_TOOL_ANNOTATIONS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)

# Each tool's description is built from `TOOL_DATA_SENTENCE` rather than repeating the
# sentence as a second hardcoded copy, so the one rule every tool carries can never drift
# from `INSTRUCTIONS`/`GUIDE`'s wording of it. Passed explicitly as `@mcp.tool(description=...)`
# (an f-string cannot be a function's docstring - only a literal string/bytes constant is);
# each function keeps a short plain docstring of its own for readers of this module.
_MEMORY_INDEX_DESCRIPTION = f"""List every note in the vault: the table of contents to read first.

{TOOL_DATA_SENTENCE}
Returns one entry per note (id, path, title, description, type, tags, updated,
namespace_kind), sorted by path. `namespace` and `type` filter to an exact match;
archived notes are excluded unless `include_archived` is set. A note that fails to
parse is reported as a `warning` entry instead of being silently dropped.
`namespace_kind` is 'personal', 'group', 'project' or 'org' in enterprise mode, and
`null` otherwise. Pass the `path` or `id` of the entries you need to `memory_read`."""

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

Returns `{{results: [{{id, path, title, description, type, tags, snippet, score,
namespace_kind}}], mode}}`. `mode` is 'hybrid' when a vector index is configured,
'fulltext' when only full-text search is available, and 'scan' when no database is
configured at all - a slower, best-effort fallback over the plain working copy that
keeps this tool usable without Postgres. `namespace_kind` is 'personal', 'group',
'project' or 'org' in enterprise mode, and `null` otherwise."""

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

_MEMORY_PROMOTE_DESCRIPTION = f"""\
Copy the note at `path` into `target_namespace`, a shared namespace you can write to.

{TOOL_DATA_SENTENCE}
The default write target stays your own namespace (`me`) - use this only when a note
should actually become shared, team-visible knowledge, and only into a namespace you
already have write access to. The copy gets a new `id` at `<target_namespace>/<type>/
<slug>.md` (`path`'s own `type`/`slug`); the original's id is added to its `supersedes`
list; every other field is carried over unchanged. `if_version` is the `version` a
previous `memory_read` returned for `path`.

Unless `keep_original` is set, the original at `path` is archived in the same write
(moved to `_archive/`, exactly like `memory_archive` - never deleted); `keep_original=true`
leaves it untouched in place instead.

On success returns `{{new: {{path, id, version, namespace_kind}}, original: {{path,
version}}, commit}}`. `namespace_kind` is 'personal', 'group', 'project' or 'org' in
enterprise mode, and `null` otherwise. On a conflict - `if_version` is stale, `path` does
not exist or is archived, or the target path already exists - returns an error result
(`isError: true`) whose structured content carries enough to retry, the same way
`memory_write` does."""

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


async def _resolve_namespaces(services: Services) -> namespaces.Resolution | None:
    """The calling principal's ADR-0008 matrix access, or `None` for `"git"` mode.

    `None` exactly when `Services.app_role` is `None` ("git" mode has no RLS
    and no namespace registry at all - the module docstring's "exactly what
    it always was") - every helper below treats `None` as "no rewriting, no
    matrix check, fall back to `mcp/authz.py`'s plain token-namespace check".
    The one `namespaces.resolve` round trip a tool call needs; raises
    `db.rls.NoPrincipal` before acquiring a connection at all if the current
    request carries none (`namespaces.resolve`'s own docstring).
    """
    if services.app_role is None:
        return None
    if services.pool is None:  # pragma: no cover - open_services always pairs these
        raise AssertionError("Services.app_role is set but Services.pool is None")
    return await namespaces.resolve(services.pool, role=services.app_role)


def _effective_readable(
    resolved: namespaces.Resolution | None, ctx: Context | None
) -> set[str] | None:
    """The namespaces the caller may read: the ADR-0008 matrix, further narrowed by
    whatever a token's own `namespaces` claim (ADR-0004) still restricts - `None` only
    when neither side restricts anything (`"git"` mode with an unrestricted/no token).
    """
    token_readable = readable_namespaces(ctx)
    if resolved is None:
        return token_readable
    matrix = resolved.readable()
    return matrix if token_readable is None else matrix & token_readable


def _effective_writable(resolved: namespaces.Resolution | None) -> set[str] | None:
    """The namespaces the caller may write to - `_effective_readable`'s write-side twin."""
    token_writable = writable_namespaces()
    if resolved is None:
        return token_writable
    matrix = resolved.writable()
    return matrix if token_writable is None else matrix & token_writable


def _to_stored_path(path: str, resolved: namespaces.Resolution | None) -> str:
    """`path` translated from `me`/alias to the real stored alias, or unchanged in `"git"` mode."""
    if resolved is None:
        return path
    return namespaces.rewrite_path_to_stored(path, resolved)


def _to_display_path(path: str, resolved: namespaces.Resolution | None) -> str:
    """`path` translated from the real stored alias to `me`/alias, or unchanged in `"git"` mode."""
    if resolved is None:
        return path
    return namespaces.rewrite_path_to_display(path, resolved)


def _to_stored_path_or_result(
    path: str, resolved: namespaces.Resolution | None
) -> str | CallToolResult:
    """`_to_stored_path`, with a rejected `u-*` namespace (`Resolution.to_stored`) turned
    into the same `InvalidNote`-shaped `_error_result` every other invalid path already
    gets - never an uncaught `PathRejected` escaping a write tool call the way a bare
    `parse_note_path` failure deeper in `storage.write`/`edit`/`supersede`/`archive`
    never does either (`storage.rules.parse_note_path_or_raise` maps that one the same
    way). Every write tool below checks `isinstance(result, CallToolResult)` immediately
    and returns it as-is before doing anything else.
    """
    try:
        return _to_stored_path(path, resolved)
    except PathRejected as exc:
        return _error_result(InvalidNote(path, str(exc)))


def _to_stored_namespace(value: str, resolved: namespaces.Resolution | None) -> str:
    """A bare namespace filter value (not a full path), translated the same way `_to_stored_path`
    translates one - `memory_index`'s `namespace`/`memory_search`'s `namespaces` arguments."""
    if resolved is None:
        return value
    return resolved.to_stored(value)


def _namespace_of(path: str) -> str | None:
    """`path`'s namespace segment, for `quotas.QuotaChecker.check_write` - `None` if
    `path` does not even parse, the same best-effort fallback `_require_writable`'s own
    `PathRejected` catch uses: a `path` this malformed is about to fail its own
    `parse_note_path`/`storage.write` call anyway, with a sharper error than a
    skipped quota check would ever report."""
    try:
        return parse_note_path(path, allow_archive=True).namespace
    except PathRejected:
        return None


def _promote_target_path(path: str, target_namespace: str) -> str | None:
    """The target path `memory_promote` is about to write, for the quota checks below -
    same construction `storage.rules.prepare_promote_paths` does, computed here only
    for the audit-only `path` argument `QuotaChecker.check_write`/`StorageQuotaChecker.
    check_write` take (neither keys its quota by it, see their own docstrings). `None`
    if `path` does not even parse as a non-archived note path - the same best-effort
    fallback `_namespace_of` uses, left to `storage.promote`'s own sharper error.
    """
    try:
        source_note_path = parse_note_path(path, allow_archive=False)
    except PathRejected:
        return None
    return NotePath(
        namespace=target_namespace, type=source_note_path.type, slug=source_note_path.slug
    ).relative


def _storage_namespace_kind(
    resolved: namespaces.Resolution | None, namespace: str | None
) -> NamespaceKind | None:
    """`namespace`'s kind for `quotas.StorageQuotaChecker.check_write` (#243): `"personal"`
    for the caller's own namespace, `"shared"` for every other kind (group/project/org).

    `None` in `"git"` mode (`resolved is None` - `storage_quota_checker` is
    never given one then either, see `build_server`'s own docstring) or for
    a `namespace` this resolution holds no row for at all - not reachable
    for a path that already passed `_require_writable`, but the same
    best-effort fallback `_namespace_of` uses for a path that does not even
    parse.
    """
    if resolved is None or namespace is None:
        return None
    kind = resolved.kind_of(namespace)
    if kind is None:
        return None
    return "personal" if kind == "personal" else "shared"


def _require_writable(path: str, resolved: namespaces.Resolution | None) -> None:
    """Raise `ToolError` if `path`'s namespace is not writable for the calling principal.

    Falls back to `mcp/authz.py`'s plain `require_writable_namespace` in `"git"`
    mode (`resolved is None`); otherwise checks the ADR-0008 matrix
    (`Resolution.writable`) instead - `path` must already be in stored-alias
    form (`_to_stored_path`), not `me`. Best-effort on a `path` that does not
    even parse, the same way `require_writable_namespace` is: left to the
    write call's own, sharper `PathRejected`.
    """
    if resolved is None:
        require_writable_namespace(path)
        return
    try:
        note_path = parse_note_path(path, allow_archive=True)
    except PathRejected:
        return
    if note_path.namespace not in resolved.writable():
        raise ToolError(
            f"caller may not write to namespace {resolved.to_display(note_path.namespace)!r}"
        )


def _require_promote_access(
    path: str, target_namespace: str, resolved: namespaces.Resolution | None
) -> None:
    """Raise `ToolError` unless the calling principal may promote `path` into
    `target_namespace` (ADR-0008 "`memory_promote`"). `path`/`target_namespace` are
    already in stored-alias form (`_to_stored_path`/`_to_stored_namespace`), not `me`.

    `"postgres"` mode (`resolved` set): the source must be the caller's own `me` -
    promote moves a *personal* note into a shared namespace, never someone else's
    personal namespace nor one that is already shared (ADR-0008 "it copies a note
    from `me`"); the target must be in the ADR-0008 matrix's `writable()` set, the
    same check `_require_writable` makes for every other write tool. `"git"` mode
    (`resolved is None`, no `me`, no registry - ADR-0008 "Git backend: unchanged"):
    both source and target are checked against the token's own writable namespaces
    instead (`mcp/authz.py`'s `require_writable_namespace`), the same way every
    other write tool does. Best-effort on a `path` that does not even parse as a
    note path - left to `storage.promote`'s own, sharper `InvalidNote`/`PathRejected`,
    the same way `_require_writable` is.
    """
    try:
        source_note_path = parse_note_path(path, allow_archive=False)
    except PathRejected:
        return

    if resolved is None:
        require_writable_namespace(path)
        target_candidate = NotePath(
            namespace=target_namespace,
            type=source_note_path.type,
            slug=source_note_path.slug,
        ).relative
        require_writable_namespace(target_candidate)
        return

    if resolved.own_alias is None or source_note_path.namespace != resolved.own_alias:
        raise ToolError(
            f"memory_promote only promotes from the caller's own namespace (me), "
            f"not {resolved.to_display(source_note_path.namespace)!r}"
        )
    if target_namespace not in resolved.writable():
        raise ToolError(
            f"caller may not write to namespace {resolved.to_display(target_namespace)!r}"
        )


async def _require_archive_access(
    path: str, resolved: namespaces.Resolution | None, services: Services
) -> None:
    """Raise `ToolError` if the calling principal may not archive `path`.

    Falls back to the plain writable check in `"git"` mode. Otherwise: write
    access first (ADR-0008 addendum 2026-10-07, #119, "curate requires
    write"), then - only for a note whose revision-1 `author_oid`
    (`namespaces.author_oid_of`) is not the caller's own `oid` - the matrix's
    stricter curate check (`Resolution.can_curate`). Archiving your own note
    in a shared namespace is a plain write (ADR-0008 addendum "curate is
    author-based"); only `memory_archive` makes this distinction -
    `memory_write`/`memory_edit`/`memory_supersede` stay write-only,
    regardless of who authored what they touch.
    """
    if resolved is None:
        require_writable_namespace(path)
        return
    try:
        note_path = parse_note_path(path, allow_archive=True)
    except PathRejected:
        return
    alias = note_path.namespace
    if alias not in resolved.writable():
        raise ToolError(f"caller may not write to namespace {resolved.to_display(alias)!r}")

    if (
        services.pool is None or services.app_role is None
    ):  # pragma: no cover - paired by open_services
        raise AssertionError("_require_archive_access called without pool/app_role")
    author_oid = await namespaces.author_oid_of(services.pool, role=services.app_role, path=path)
    is_own_note = author_oid is not None and author_oid == resolved.oid
    if is_own_note:
        return
    if not resolved.can_curate(alias):
        raise ToolError(
            f"caller may not curate namespace {resolved.to_display(alias)!r}: "
            "not the note's author, and lacks curator/owner/admin rights there"
        )


def _rewrite_error_result(
    result: CallToolResult, resolved: namespaces.Resolution | None
) -> CallToolResult:
    """`result`'s `path` (`VersionConflict`/`NotFound`/`InvalidNote`/`EditMismatch`'s
    shared `to_dict()` field) translated back to `me`/alias, in both the structured
    content and the plain-text content block - a no-op in `"git"` mode or for any
    error shape that carries no `path` at all.
    """
    if resolved is None:
        return result
    structured = result.structured_content
    if not isinstance(structured, Mapping) or "path" not in structured:
        return result
    original_path = structured["path"]
    if not isinstance(original_path, str):
        return result
    rewritten_path = namespaces.rewrite_path_to_display(original_path, resolved)
    if rewritten_path == original_path:
        return result

    new_structured = dict(structured)
    new_structured["path"] = rewritten_path
    message = new_structured.get("message")
    if isinstance(message, str):
        new_structured["message"] = message.replace(f"'{original_path}'", f"'{rewritten_path}'")

    new_content = [
        TextContent(
            type="text", text=block.text.replace(f"'{original_path}'", f"'{rewritten_path}'")
        )
        if isinstance(block, TextContent)
        else block
        for block in result.content
    ]
    return CallToolResult(content=new_content, is_error=True, structured_content=new_structured)


def build_server(
    services: Services,
    *,
    auth: AuthSettings | None = None,
    token_verifier: TokenVerifier | None = None,
    auth_server_provider: OAuthAuthorizationServerProvider[Any, Any, Any] | None = None,
    quota_checker: QuotaChecker | None = None,
    storage_quota_checker: StorageQuotaChecker | None = None,
    stdio_profile: str | None = None,
) -> MCPServer:
    """Build the MCP server for `services`, with all memory tools and `memory_guide` registered.

    `auth` plus exactly one of `token_verifier`/`auth_server_provider` - or
    neither - are `None` for stdio (no bearer auth at all) and for an HTTP
    server with no `DATABASE_URL` configured (#33's loopback-only mode);
    `http.py` passes a `memory_manager.auth.verifier.StaticTokenVerifier`
    whenever a database is configured but no OAuth authorization server is
    (#34), or a `memory_manager.auth.provider.MemoryManagerOAuthProvider`
    once one is (#36 - `MCPServer` itself then derives the bearer-token
    verifier from the provider, merging OAuth and static tokens; see that
    provider's module docstring). Passed straight to `MCPServer`, which is
    what actually wires the SDK's bearer-auth middleware (and, for a
    provider, the `/authorize`/`/token`/... routes) into
    `streamable_http_app()` - nothing in this module reads any of the three
    directly; every tool below gets the per-request token through
    `mcp/authz.py`'s `get_access_token()` instead.

    `quota_checker` (`quotas.QuotaChecker`, #242) is `None` for stdio and for
    an HTTP server built without one - no write quota is enforced then, the
    same "off unless configured" default `ServerConfig`'s `quota_*` fields
    have. Given, every write tool below calls `quota_checker.check_write`
    with the namespace it is about to write to, right after its own
    `_require_writable`/`_require_archive_access`/`_require_promote_access`
    check and before it ever reaches `services.storage` - a `QuotaExceeded`
    (a `ToolError`) then stops the call exactly like a scope or
    namespace-permission failure would. `memory_promote` checks the *target*
    namespace, the same way its own `storage_quota_checker` call below does.

    `storage_quota_checker` (`quotas.StorageQuotaChecker`, #243) is `None`
    for stdio, for `"git"` mode and for an HTTP server built without one -
    `None` is also the only possibility unless `services.storage` is a
    `storage.postgres.PostgresBackend` (`http.py` only ever builds one
    then). Given, `memory_write`/`memory_edit`/`memory_supersede`/
    `memory_promote` each call it with the namespace and byte size the write
    they are about to submit would actually produce, right after that
    content is computed and before it reaches `services.storage` - never
    `memory_archive`, which only ever frees a note-count slot (module
    docstring, `quotas.StorageQuotaChecker`'s own). A `StorageQuotaExceeded`
    (also a `ToolError`) stops the call the
    same way `QuotaExceeded` does.

    `stdio_profile` (#131, ADR-0010) is `cli.py`'s `serve --stdio --profile`: the one
    client profile a stdio connection runs under when given, same precedence as `http.py`'s
    `?profile=`/`MM-Client-Profile` override (`_ProfileMiddleware` combines whichever of
    the two a given process could have). `None` (stdio with no `--profile`, and every HTTP
    server - `http.py` never passes this) leaves the per-request `clientInfo.name`
    mapping/`default` fallback as the only source, exactly as before this parameter
    existed.
    """
    mcp = MCPServer(
        name="memory-manager",
        instructions=INSTRUCTIONS,
        auth=auth,
        token_verifier=token_verifier,
        auth_server_provider=auth_server_provider,
        middleware=[_ProfileMiddleware(stdio_profile=stdio_profile)],
    )

    @mcp.prompt(name="memory_guide")
    def memory_guide() -> str:
        """The long-form usage guide: workflow, examples, and what never to store."""
        return GUIDE

    @mcp.tool(description=_MEMORY_INDEX_DESCRIPTION, annotations=_READ_TOOL_ANNOTATIONS)
    @instrument_tool("memory_index")
    async def memory_index(
        namespace: str | None = None,
        type: str | None = None,
        include_archived: bool = False,
        ctx: Context | None = None,
    ) -> list[MemoryIndexEntry]:
        """List every note in the vault: the table of contents to read first."""
        require_scope(READ_SCOPE)
        resolved = await _resolve_namespaces(services)
        stored_namespace = None
        if namespace is not None:
            try:
                stored_namespace = _to_stored_namespace(namespace, resolved)
            except PathRejected as exc:
                raise ToolError(
                    f"memory_index got an invalid namespace {namespace!r}: {exc}"
                ) from exc
        readable = _effective_readable(resolved, ctx)
        entries = [
            entry
            for entry in await _index_entries(services.storage)
            if _matches(
                entry,
                namespace=stored_namespace,
                type=type,
                include_archived=include_archived,
                readable=readable,
            )
        ]
        entries = _cap_index(entries)
        if resolved is not None:
            entries = [_rewrite_index_entry(entry, resolved) for entry in entries]
        return entries

    @mcp.tool(description=_MEMORY_READ_DESCRIPTION, annotations=_READ_TOOL_ANNOTATIONS)
    @instrument_tool("memory_read")
    async def memory_read(items: list[str], ctx: Context | None = None) -> list[MemoryReadItem]:
        """Read one or more notes by vault path or id."""
        require_scope(READ_SCOPE)
        if len(items) > _MAX_READ_ITEMS:
            raise ToolError(
                f"memory_read accepts at most {_MAX_READ_ITEMS} items, got {len(items)}"
            )
        resolved = await _resolve_namespaces(services)
        readable = _effective_readable(resolved, ctx)
        return await _read_items(services.storage, items, readable=readable, resolved=resolved)

    @mcp.tool(description=_MEMORY_SEARCH_DESCRIPTION, annotations=_READ_TOOL_ANNOTATIONS)
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

        resolved = await _resolve_namespaces(services)
        stored_namespaces: list[str] | None = None
        if namespaces is not None:
            try:
                stored_namespaces = [_to_stored_namespace(ns, resolved) for ns in namespaces]
            except PathRejected as exc:
                raise ToolError(f"memory_search got an invalid namespace: {exc}") from exc

        parsed_valid_at = _parse_valid_at(valid_at)
        clamped_limit = max(_MIN_SEARCH_LIMIT, min(limit, _MAX_SEARCH_LIMIT))
        readable = _effective_readable(resolved, ctx)
        effective_namespaces = restrict_namespaces(stored_namespaces, readable)
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

        if services.indexer is not None:
            if services.pool is None:
                raise AssertionError(  # pragma: no cover - open_services always pairs these
                    "Services.indexer is set but Services.pool is None"
                )
            if services.app_role is not None:
                # `"postgres"` (ADR-0008 addendum, #116): run the whole search on
                # one connection switched to the app role and the caller's own
                # identity, not the owner pool - `rls.request_connection` raises
                # `NoPrincipal` before acquiring one at all if the request
                # carries none.
                async with rls.request_connection(services.pool, role=services.app_role) as conn:
                    note_hits = await hybrid_search(
                        conn,
                        query,
                        provider=services.provider,
                        filters=filters,
                        limit=clamped_limit,
                    )
            else:
                # `"git"` with `DATABASE_URL` configured: no RLS at all, the
                # plain owner pool is exactly what `services.pool` already is.
                note_hits = await hybrid_search(
                    services.pool,
                    query,
                    provider=services.provider,
                    filters=filters,
                    limit=clamped_limit,
                )
            results = [_note_hit_result(hit) for hit in note_hits]
        elif services.vault_root is not None:
            scan_hits = scan_search(
                services.vault_root, query, filters=filters, limit=clamped_limit
            )
            results = [_scan_hit_result(hit) for hit in scan_hits]
        else:
            # Unreachable through `app.open_services`: the `postgres` backend
            # always has an indexer (ADR-0007 §4, WP-18/#98), and `"git"`
            # without one always has `vault_root`. Only a hand-assembled
            # `Services` with neither could ever get here.
            raise AssertionError(  # pragma: no cover - open_services always sets one of these
                "memory_search: services.indexer and services.vault_root are both None"
            )

        if resolved is not None:
            results = [_rewrite_search_result(result, resolved) for result in results]
        return {"results": results, "mode": mode}

    @mcp.tool(description=_MEMORY_WRITE_DESCRIPTION, annotations=_WRITE_TOOL_ANNOTATIONS)
    @instrument_tool("memory_write")
    async def memory_write(
        path: str,
        content: str,
        if_version: str,
        message: str | None = None,
    ) -> Annotated[CallToolResult, MemoryWriteResult]:
        """Create or replace the note at `path`."""
        require_scope(WRITE_SCOPE)
        resolved = await _resolve_namespaces(services)
        stored_path_or_error = _to_stored_path_or_result(path, resolved)
        if isinstance(stored_path_or_error, CallToolResult):
            return stored_path_or_error
        stored_path = stored_path_or_error
        _require_writable(stored_path, resolved)
        if quota_checker is not None:
            await quota_checker.check_write(
                op="write",
                path=stored_path,
                namespace=_namespace_of(stored_path),
                actor=current_actor(),
                client=current_client(),
            )
        try:
            prepared = await _prepare_write_content(
                services.storage, stored_path, content, if_version
            )
        except NoteFormatError as exc:
            return _error_result(exc)

        write_namespace = _namespace_of(stored_path)
        write_namespace_kind = _storage_namespace_kind(resolved, write_namespace)
        if (
            storage_quota_checker is not None
            and write_namespace is not None
            and write_namespace_kind is not None
        ):
            await storage_quota_checker.check_write(
                op="write",
                path=stored_path,
                namespace=write_namespace,
                namespace_kind=write_namespace_kind,
                is_new_note=(if_version == _NEW_VERSION),
                final_size=len(prepared.content),
                actor=current_actor(),
                client=current_client(),
            )

        try:
            result = await services.storage.write(
                stored_path,
                prepared.content,
                if_version=if_version,
                client=current_client(),
                actor=current_actor(),
                message=message,
            )
        except WriteError as exc:
            return _rewrite_error_result(_error_result(exc), resolved)

        return _ok_result(
            {
                "path": _to_display_path(result.path, resolved),
                "id": prepared.id,
                "version": result.version,
                "commit": result.commit,
            }
        )

    @mcp.tool(description=_MEMORY_EDIT_DESCRIPTION, annotations=_WRITE_TOOL_ANNOTATIONS)
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
        resolved = await _resolve_namespaces(services)
        stored_path_or_error = _to_stored_path_or_result(path, resolved)
        if isinstance(stored_path_or_error, CallToolResult):
            return stored_path_or_error
        stored_path = stored_path_or_error
        _require_writable(stored_path, resolved)
        if quota_checker is not None:
            await quota_checker.check_write(
                op="edit",
                path=stored_path,
                namespace=_namespace_of(stored_path),
                actor=current_actor(),
                client=current_client(),
            )
        edit_namespace = _namespace_of(stored_path)
        edit_namespace_kind = _storage_namespace_kind(resolved, edit_namespace)
        if (
            storage_quota_checker is not None
            and edit_namespace is not None
            and edit_namespace_kind is not None
        ):
            current_for_quota = await services.storage.read(stored_path)
            if current_for_quota is not None:
                # An estimate, not the exact canonical bytes `services.storage.edit`
                # will end up committing (module docstring: #243 is a soft limit) -
                # good enough to decide whether this edit would push the namespace
                # over its byte budget, without duplicating `storage.rules`' own
                # validation here just to measure a length.
                predicted_size = max(
                    0,
                    len(current_for_quota.content)
                    - len(old_str.encode("utf-8"))
                    + len(new_str.encode("utf-8")),
                )
                await storage_quota_checker.check_write(
                    op="edit",
                    path=stored_path,
                    namespace=edit_namespace,
                    namespace_kind=edit_namespace_kind,
                    is_new_note=False,
                    final_size=predicted_size,
                    actor=current_actor(),
                    client=current_client(),
                )
        try:
            result = await services.storage.edit(
                stored_path,
                old_str,
                new_str,
                if_version=if_version,
                client=current_client(),
                actor=current_actor(),
                message=message,
            )
        except WriteError as exc:
            return _rewrite_error_result(_error_result(exc), resolved)

        stored = await services.storage.read(result.path)
        if stored is None:  # pragma: no cover - defensive, a write that just succeeded disappeared
            raise RuntimeError(f"'{result.path}' was just written but is now missing")
        note_id = parse(stored.content).id
        return _ok_result(
            {
                "path": _to_display_path(result.path, resolved),
                "id": note_id,
                "version": result.version,
                "commit": result.commit,
            }
        )

    @mcp.tool(description=_MEMORY_SUPERSEDE_DESCRIPTION, annotations=_WRITE_TOOL_ANNOTATIONS)
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
        resolved = await _resolve_namespaces(services)
        stored_old_input_or_error = _to_stored_path_or_result(old, resolved)
        if isinstance(stored_old_input_or_error, CallToolResult):
            return stored_old_input_or_error
        resolved_old = await _resolve_path_or_id(services.storage, stored_old_input_or_error)
        if resolved_old is None:
            return _error_result(NotFound(old))
        stored_new_path_or_error = _to_stored_path_or_result(new_path, resolved)
        if isinstance(stored_new_path_or_error, CallToolResult):
            return stored_new_path_or_error
        stored_new_path = stored_new_path_or_error
        _require_writable(resolved_old, resolved)
        _require_writable(stored_new_path, resolved)
        if quota_checker is not None:
            # Quotas against the namespace actually receiving new content
            # (`new_path`) - `old` only ever has its `valid_to` set in place,
            # never a new revision of its own content.
            await quota_checker.check_write(
                op="supersede",
                path=stored_new_path,
                namespace=_namespace_of(stored_new_path),
                actor=current_actor(),
                client=current_client(),
            )

        try:
            prepared = await _prepare_write_content(
                services.storage, stored_new_path, new_content, _NEW_VERSION
            )
        except NoteFormatError as exc:
            return _error_result(exc)

        supersede_namespace = _namespace_of(stored_new_path)
        supersede_namespace_kind = _storage_namespace_kind(resolved, supersede_namespace)
        if (
            storage_quota_checker is not None
            and supersede_namespace is not None
            and supersede_namespace_kind is not None
        ):
            # Same reasoning as the rate-quota check above: only `new_path`'s
            # namespace is checked - `supersede` always inserts exactly one new
            # note there, never changes how many notes `old`'s own namespace holds.
            await storage_quota_checker.check_write(
                op="supersede",
                path=stored_new_path,
                namespace=supersede_namespace,
                namespace_kind=supersede_namespace_kind,
                is_new_note=True,
                final_size=len(prepared.content),
                actor=current_actor(),
                client=current_client(),
            )

        try:
            result = await services.storage.supersede(
                resolved_old,
                stored_new_path,
                prepared.content,
                if_version=if_version,
                client=current_client(),
                actor=current_actor(),
                message=message,
            )
        except WriteError as exc:
            return _rewrite_error_result(_error_result(exc), resolved)

        old_version = (result.related or {}).get(resolved_old, "")
        old_stored = await services.storage.read(resolved_old)
        if old_stored is None:  # pragma: no cover - defensive, see memory_edit above
            raise RuntimeError(f"'{resolved_old}' was just superseded but is now missing")
        old_note = parse(old_stored.content)
        valid_to = old_note.valid_to.isoformat() if old_note.valid_to is not None else ""
        return _ok_result(
            {
                "new": {
                    "path": _to_display_path(result.path, resolved),
                    "id": prepared.id,
                    "version": result.version,
                },
                "old": {
                    "path": _to_display_path(resolved_old, resolved),
                    "version": old_version,
                    "valid_to": valid_to,
                },
                "commit": result.commit,
            }
        )

    @mcp.tool(description=_MEMORY_PROMOTE_DESCRIPTION, annotations=_WRITE_TOOL_ANNOTATIONS)
    @instrument_tool("memory_promote")
    async def memory_promote(
        path: str,
        target_namespace: str,
        if_version: str,
        keep_original: bool = False,
    ) -> Annotated[CallToolResult, MemoryPromoteResult]:
        """Copy the note at `path` into `target_namespace`, a shared namespace you can write to."""
        require_scope(WRITE_SCOPE)
        resolved = await _resolve_namespaces(services)
        stored_path_or_error = _to_stored_path_or_result(path, resolved)
        if isinstance(stored_path_or_error, CallToolResult):
            return stored_path_or_error
        stored_path = stored_path_or_error

        try:
            stored_target_namespace = _to_stored_namespace(target_namespace, resolved)
        except PathRejected as exc:
            raise ToolError(
                f"memory_promote got an invalid target_namespace {target_namespace!r}: {exc}"
            ) from exc

        _require_promote_access(stored_path, stored_target_namespace, resolved)

        promote_target_path = _promote_target_path(stored_path, stored_target_namespace)
        if quota_checker is not None:
            await quota_checker.check_write(
                op="promote",
                path=promote_target_path if promote_target_path is not None else stored_path,
                namespace=stored_target_namespace,
                actor=current_actor(),
                client=current_client(),
            )

        promote_namespace_kind = _storage_namespace_kind(resolved, stored_target_namespace)
        if (
            storage_quota_checker is not None
            and promote_namespace_kind is not None
            and promote_target_path is not None
        ):
            # An estimate, not the exact canonical bytes `services.storage.promote`
            # will end up committing (module docstring: #243 is a soft limit) - the
            # source note's own size is close enough: the copy only ever differs
            # by a new `id` (same fixed length) and, when not already present, one
            # more entry in `supersedes`.
            current_for_quota = await services.storage.read(stored_path)
            if current_for_quota is not None:
                await storage_quota_checker.check_write(
                    op="promote",
                    path=promote_target_path,
                    namespace=stored_target_namespace,
                    namespace_kind=promote_namespace_kind,
                    is_new_note=True,
                    final_size=len(current_for_quota.content),
                    actor=current_actor(),
                    client=current_client(),
                )

        try:
            result = await services.storage.promote(
                stored_path,
                stored_target_namespace,
                if_version=if_version,
                keep_original=keep_original,
                client=current_client(),
                actor=current_actor(),
            )
        except WriteError as exc:
            return _rewrite_error_result(_error_result(exc), resolved)

        original_path, original_version = next(iter((result.related or {}).items()), ("", ""))

        new_stored = await services.storage.read(result.path)
        if new_stored is None:  # pragma: no cover - defensive, see memory_edit above
            raise RuntimeError(f"'{result.path}' was just written but is now missing")
        new_id = parse(new_stored.content).id

        namespace_kind = (
            _namespace_kind_of_path(result.path, resolved) if resolved is not None else None
        )

        return _ok_result(
            {
                "new": {
                    "path": _to_display_path(result.path, resolved),
                    "id": new_id,
                    "version": result.version,
                    "namespace_kind": namespace_kind,
                },
                "original": {
                    "path": _to_display_path(original_path, resolved),
                    "version": original_version,
                },
                "commit": result.commit,
            }
        )

    @mcp.tool(description=_MEMORY_ARCHIVE_DESCRIPTION, annotations=_WRITE_TOOL_ANNOTATIONS)
    @instrument_tool("memory_archive")
    async def memory_archive(
        path: str,
        if_version: str,
        message: str | None = None,
    ) -> Annotated[CallToolResult, MemoryArchiveResult]:
        """Archive the note at `path`: move it to `_archive/`, never delete it."""
        require_scope(WRITE_SCOPE)
        resolved_ns = await _resolve_namespaces(services)
        stored_path_input_or_error = _to_stored_path_or_result(path, resolved_ns)
        if isinstance(stored_path_input_or_error, CallToolResult):
            return stored_path_input_or_error
        resolved_path = await _resolve_path_or_id(services.storage, stored_path_input_or_error)
        if resolved_path is None:
            return _error_result(NotFound(path))
        await _require_archive_access(resolved_path, resolved_ns, services)
        if quota_checker is not None:
            await quota_checker.check_write(
                op="archive",
                path=resolved_path,
                namespace=_namespace_of(resolved_path),
                actor=current_actor(),
                client=current_client(),
            )

        try:
            result = await services.storage.archive(
                resolved_path,
                if_version=if_version,
                client=current_client(),
                actor=current_actor(),
                message=message,
            )
        except WriteError as exc:
            return _rewrite_error_result(_error_result(exc), resolved_ns)

        return _ok_result(
            {
                "archived_path": _to_display_path(result.path, resolved_ns),
                "version": result.version,
                "commit": result.commit,
            }
        )

    return mcp


@dataclass(frozen=True)
class _VaultNote:
    """One note `StorageBackend.list` reported, parsed best-effort."""

    rel: str
    note: Note | None
    error: NoteFormatError | None


async def _iter_vault_notes(storage: StorageBackend) -> list[_VaultNote]:
    """Every note in the vault, in path order, parsed best-effort.

    A note that fails to parse is still included, with `note=None` and
    `error` set, so callers can report it instead of dropping it -
    `storage.list` already did the note-shaped-path filtering (a stray
    `README.md` never reaches here at all).
    """
    notes: list[_VaultNote] = []
    for stored in await storage.list(include_archived=True):
        try:
            note = parse(stored.content)
        except NoteFormatError as exc:
            notes.append(_VaultNote(rel=stored.path, note=None, error=exc))
            continue
        notes.append(_VaultNote(rel=stored.path, note=note, error=None))
    return notes


async def _index_entries(storage: StorageBackend) -> list[MemoryIndexEntry]:
    entries: list[MemoryIndexEntry] = []
    for vault_note in await _iter_vault_notes(storage):
        if vault_note.note is None:
            entries.append(
                {
                    "path": vault_note.rel,
                    "warning": f"failed to parse: {vault_note.error}",
                    "namespace_kind": None,
                }
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
                "namespace_kind": None,
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
            "namespace_kind": entry["namespace_kind"],
        }
        if "id" in entry:
            slim["id"] = entry["id"]
        if "description" in entry:
            slim["description"] = entry["description"]
        slimmed.append(slim)
    return slimmed


def _namespace_kind_of_path(path: str, resolved: namespaces.Resolution) -> str | None:
    """The ADR-0008 `namespace_kind` (#102) of `path`'s namespace - `path` is the real
    *stored* alias (before `rewrite_path_to_display`), since `Resolution.kind_of`
    looks rows up by stored alias, not by `me`/display alias. Best-effort the same
    way `rewrite_path_to_stored`/`rewrite_path_to_display` are: `None` for anything
    that does not even parse as a note path.
    """
    try:
        note_path = parse_note_path(path, allow_archive=True)
    except PathRejected:
        return None
    return resolved.kind_of(note_path.namespace)


def _rewrite_index_entry(
    entry: MemoryIndexEntry, resolved: namespaces.Resolution
) -> MemoryIndexEntry:
    """`entry`'s `path`, translated back to `me`/alias (`_resolve_namespaces`'s own
    docstring), and its `namespace_kind` (#102) filled in from the matrix.
    """
    path = entry.get("path")
    if path is None:
        return entry
    kind = _namespace_kind_of_path(path, resolved)
    rewritten = namespaces.rewrite_path_to_display(path, resolved)
    if rewritten == path and kind == entry.get("namespace_kind"):
        return entry
    return {**entry, "path": rewritten, "namespace_kind": kind}


def _search_mode(services: Services) -> str:
    """Which ranking `memory_search` would use for `services`, without running a query.

    Computed up front so the deny-all short-circuit (`effective_namespaces == []`) can
    still report the `mode` a caller would otherwise have gotten, instead of skipping it
    along with the query.
    """
    if services.indexer is None:
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
        "namespace_kind": None,
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
        "namespace_kind": None,
    }


def _rewrite_search_result(
    result: MemorySearchResult, resolved: namespaces.Resolution
) -> MemorySearchResult:
    """`result`'s `path`, translated back to `me`/alias (`_resolve_namespaces`'s own
    docstring), and its `namespace_kind` (#102) filled in from the matrix.
    """
    kind = _namespace_kind_of_path(result["path"], resolved)
    rewritten = namespaces.rewrite_path_to_display(result["path"], resolved)
    if rewritten == result["path"] and kind == result.get("namespace_kind"):
        return result
    return {**result, "path": rewritten, "namespace_kind": kind}


async def _read_items(
    storage: StorageBackend,
    items: list[str],
    *,
    readable: set[str] | None = None,
    resolved: namespaces.Resolution | None = None,
) -> list[MemoryReadItem]:
    """`memory_read`'s own loop. `item` (echoed back on every error) is always the
    caller's own, unrewritten input; `path` - the *stored* path - is what the
    lookup/readability check and `storage.read` itself use, translated back to
    `me`/alias (`resolved`, `None` in `"git"` mode) only in the one success case's
    `path` field.
    """
    id_map: dict[str, str] | None = None
    results: list[MemoryReadItem] = []

    for item in items:
        path = item
        if "/" not in item and is_ulid(item):
            if id_map is None:
                id_map = await _build_id_map(storage)
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
        else:
            try:
                path = _to_stored_path(path, resolved)
            except PathRejected as exc:
                results.append({"item": item, "error": error_to_dict(exc)})
                continue

        if readable is not None and not _namespace_readable(path, readable):
            results.append(
                {
                    "item": item,
                    "error": {"error": "NotFound", "message": f"'{item}' does not exist"},
                }
            )
            continue

        try:
            stored = await storage.read(path)
        except PathRejected as exc:
            results.append({"item": item, "error": error_to_dict(exc)})
            continue

        if stored is None:
            results.append(
                {
                    "item": item,
                    "error": {"error": "NotFound", "message": f"'{item}' does not exist"},
                }
            )
            continue

        try:
            note = parse(stored.content)
        except NoteFormatError as exc:
            results.append({"item": item, "error": error_to_dict(exc)})
            continue

        results.append(
            {
                "path": _to_display_path(path, resolved),
                "id": note.id,
                "version": stored.version,
                "content": stored.content.decode("utf-8"),
            }
        )

    return results


def _namespace_readable(path: str, readable: set[str]) -> bool:
    """Whether `path`'s namespace is in `readable`, best-effort.

    A `path` that does not even parse is left to `storage.read`'s own, more
    specific `PathRejected` - this only ever turns a readable check into a
    `NotFound` (never exposing whether the namespace exists), not into a
    `PathRejected` of its own.
    """
    try:
        note_path = parse_note_path(path, allow_archive=True)
    except PathRejected:
        return True
    return note_path.namespace in readable


async def _build_id_map(storage: StorageBackend) -> dict[str, str]:
    """Every note's `id -> path`, scanned once for a `memory_read` call that needs it."""
    return {
        vault_note.note.id: vault_note.rel
        for vault_note in await _iter_vault_notes(storage)
        if vault_note.note is not None
    }


async def _resolve_path_or_id(storage: StorageBackend, item: str) -> str | None:
    """`item` as a vault path: itself if it already looks like one, else looked up by id.

    Used by `memory_supersede`/`memory_archive`, which accept either like
    `memory_read` does. Returns `None` if `item` is a ULID with no matching
    note, so the caller can report its own `NotFound` instead of handing the
    storage backend a path that was never a real lookup.
    """
    if "/" not in item and is_ulid(item):
        return (await _build_id_map(storage)).get(item)
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


async def _prepare_write_content(
    storage: StorageBackend, path: str, content: str, if_version: str
) -> _PreparedWrite:
    """Normalize `content`'s `id`/`created`/`updated` before it is submitted to `storage`.

    On create (`if_version == "new"`): a missing `id`, or the literal `id: new`, is replaced
    with a freshly generated ULID; `created`/`updated` are always set to now, never taken
    from the client. On update: `updated` is always set to now; `created` is carried forward
    from the note currently at `path` when that can be read, left alone otherwise (an unsafe
    path or a missing/unparsable note is `storage.write`'s error to raise, not this
    function's). `id` is never touched on update - a changed `id` is `storage.write`'s
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
        current = await _current_note(storage, path)
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


async def _current_note(storage: StorageBackend, path: str) -> Note | None:
    """Best-effort read of the note currently at `path`, or `None` if it cannot be read.

    Used only so `_prepare_write_content` can carry `created` forward on an update; an
    unsafe path, an archived path (an update never targets `_archive/...` - the same
    restriction `resolve`'s pre-storage callers relied on `allow_archive=False` for), a
    missing note or content that fails to parse all return `None` here and are left to
    `storage.write`, which re-reads `path` itself as the one authoritative current state
    and raises the real error (`VersionConflict`, `InvalidNote`, ...).
    """
    try:
        note_path = parse_note_path(path, allow_archive=True)
    except PathRejected:
        return None
    if note_path.archived:
        return None
    try:
        stored = await storage.read(path)
    except PathRejected:
        return None
    if stored is None:
        return None
    try:
        return parse(stored.content)
    except NoteFormatError:
        return None
