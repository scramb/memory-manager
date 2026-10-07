# SPDX-License-Identifier: AGPL-3.0-only
"""Namespace- and scope-authorization seam for the MCP tools (#30, #34).

`readable_namespaces`/`writable_namespaces` are the functions `mcp/server.py`'s
tools call before applying a `namespaces` filter or a write of their own:
`None` means "every namespace", any other set is the exact, final set to
narrow to. Both are derived from the calling request's access token -
`current_access_token()` wraps the SDK's `get_access_token()`, which reads a
contextvar the HTTP transport's `AuthContextMiddleware` sets per request
(`http.py`); stdio mode never runs that middleware, so there `ctx` is
unused and every call reads/writes as unrestricted, exactly as before #34.
A token's namespaces live in `AccessToken.claims["namespaces"]`, for a
static token (`memory_manager.auth.verifier.StaticTokenVerifier`) and an
OAuth access token (`memory_manager.auth.provider.
MemoryManagerOAuthProvider.load_access_token`, #36) alike - this module
never needs to tell the two apart. `("*",)` there means "every namespace",
the same literal `memory_manager.auth.tokens` uses; an OAuth token's
namespaces are whatever `auth.login`'s `Authenticator` passed to
`complete_authorization`, so `("*",)` never occurs for one in practice, but
is read the same way if it ever does.

`restrict_namespaces` folds a tool's own `namespaces` argument together
with what `readable_namespaces` allows, so `mcp/server.py` only has to call
both and hand the result to `search.SearchFilters`/`memory_index`'s own
namespace filter. Its return value is deliberately unambiguous: `None`
means "no restriction, every namespace", an empty list means "nothing is
readable" - a caller that collapses the empty list back into "no filter"
(the way an empty `search.SearchFilters.namespaces` means "no filter")
reopens exactly the deny-all-becomes-allow-all hole `memory_search` was
fixed for (#30); every call site must check for the empty list and return
no results *before* building a filter or running a query.

`require_scope`/`require_writable_namespace` are the write-tool side of the
same token: a stdio call (no access token at all) always passes both, a
token missing the needed scope or writing outside its namespaces raises
`ToolError` - a client-facing, retryable mistake, not a crash.
"""

from __future__ import annotations

from collections.abc import Sequence

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError

from memory_manager.auth.scopes import READ_SCOPE, WRITE_SCOPE
from memory_manager.auth.tokens import ALL_NAMESPACES
from memory_manager.vault.paths import PathRejected, parse_note_path

__all__ = [
    "READ_SCOPE",
    "WRITE_SCOPE",
    "readable_namespaces",
    "require_scope",
    "require_writable_namespace",
    "restrict_namespaces",
    "writable_namespaces",
]


def current_access_token() -> AccessToken | None:
    """The access token backing the request this call runs in, or `None`.

    `None` both for stdio (no token concept at all) and for an HTTP request
    whose `Authorization` header the transport never saw (unauthenticated
    HTTP, #33's loopback-only mode) - both read the same as "unrestricted".
    """
    return get_access_token()


def _token_namespaces(token: AccessToken) -> set[str] | None:
    namespaces = (token.claims or {}).get("namespaces")
    if not namespaces or ALL_NAMESPACES in namespaces:
        return None
    return set(namespaces)


def readable_namespaces(ctx: Context | None) -> set[str] | None:
    """The namespaces the current request's token may read, or `None` for "every namespace".

    `ctx` is accepted but unused: the token comes from `current_access_token()`'s
    contextvar, not from `ctx` - kept so every existing call site
    (`readable_namespaces(ctx)`) stays unchanged.
    """
    token = current_access_token()
    if token is None:
        return None
    return _token_namespaces(token)


def writable_namespaces() -> set[str] | None:
    """The namespaces the current request's token may write to, or `None` for "every namespace".

    The same set `readable_namespaces` reports for the same token (ADR-0004: a
    token's namespaces gate both read and write, only the scope differs).
    """
    token = current_access_token()
    if token is None:
        return None
    return _token_namespaces(token)


def require_scope(scope: str) -> None:
    """Raise `ToolError` if the current request's token lacks `scope`.

    A stdio call (no token) always passes. An HTTP call without a token
    never reaches a tool at all (the transport answers 401 first, #34); this
    only ever fires for a token that *has* a scope list missing the one a
    write tool needs.
    """
    token = current_access_token()
    if token is None:
        return
    if scope not in token.scopes:
        raise ToolError(f"token lacks {scope}")


def require_writable_namespace(path: str) -> None:
    """Raise `ToolError` if `path`'s namespace is outside the current token's namespaces.

    Best-effort on `path`: a `path` that does not even parse is left alone -
    the write/edit/supersede/archive call that is about to run its own
    `parse_note_path`/`resolve` raises the sharper `PathRejected` for that,
    not this namespace check.
    """
    writable = writable_namespaces()
    if writable is None:
        return
    try:
        note_path = parse_note_path(path, allow_archive=True)
    except PathRejected:
        return
    if note_path.namespace not in writable:
        raise ToolError(f"token may not write to namespace {note_path.namespace!r}")


def restrict_namespaces(
    requested: Sequence[str] | None, readable: set[str] | None
) -> list[str] | None:
    """`requested` narrowed to what `readable` allows.

    `None` on either side means "no restriction from that side"; `None` is
    returned only when neither side restricts anything, and must be read as
    "query every namespace". Any other return value - including `[]` - is
    the exact, final set of namespaces to search/list/read, and `[]` means
    the caller may read none of them: fail closed on it (return no results
    without touching the index/vault), never treat it as "no filter".
    """
    if readable is None:
        return list(requested) if requested else None
    if requested is None:
        return sorted(readable)
    return [namespace for namespace in requested if namespace in readable]
