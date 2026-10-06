# SPDX-License-Identifier: AGPL-3.0-only
"""Namespace-authorization seam for the MCP tools (M4 placeholder, #30).

`readable_namespaces` is the one function `mcp/server.py`'s tools call
before applying a `namespaces` filter of their own: in stdio mode, with no
per-call caller identity yet, every call is unrestricted (`None`). M4
replaces this function's body with the real set derived from the caller's
token, without changing its signature or any of its call sites.

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
"""

from __future__ import annotations

from collections.abc import Sequence

from mcp.server.mcpserver import Context

__all__ = ["readable_namespaces", "restrict_namespaces"]


def readable_namespaces(ctx: Context | None) -> set[str] | None:
    """The namespaces `ctx`'s caller may read, or `None` for "every namespace".

    Stdio mode (`mcp/server.py`'s `current_client`) has no per-call caller
    identity yet, so every call reads as unrestricted regardless of `ctx`.
    M4 derives the real set from the caller's token here, once there is one
    to derive it from.
    """
    return None


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
