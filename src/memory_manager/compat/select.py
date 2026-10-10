# SPDX-License-Identifier: AGPL-3.0-only
"""Picks the `compat.profiles.Profile` an incoming request runs under (#131, ADR-0010).

Resolution order, per ADR-0010: an explicit override (`?profile=` on the MCP URL, the
`MM-Client-Profile` header, or `serve --stdio --profile` - whichever of those a
transport has) wins outright; otherwise the connecting client's `clientInfo.name`, when
the transport could read one for this request, is looked up in `_CLIENT_INFO_PROFILE`;
neither present falls back to `profiles.DEFAULT_PROFILE`. An override naming an unknown
profile is rejected (`profiles.UnknownProfile`) - never silently mapped to `default`; an
*unrecognized* `clientInfo.name` is not an error the same way - it just is not known yet,
so it falls through to `default` exactly like no `clientInfo.name` at all.

Two `contextvars.ContextVar`s carry state across the module boundaries a request
actually crosses, the same `contextvars` + reset-token idiom
`observability/logging.py`'s `request_id_var`/`RequestIdMiddleware` already uses for the
request id:

- the *override*: `http.py`'s ASGI middleware validates and sets it for the lifetime of
  one HTTP request (`set_profile_override`/`reset_profile_override`); it propagates
  unchanged into `mcp/server.py`'s `ServerMiddleware`, which runs inside the same ASGI
  call stack and therefore the same `contextvars.Context`. stdio has no ASGI layer at
  all, so there `build_server`'s `stdio_profile` keyword plays the same role instead -
  `mcp/server.py`'s middleware combines the two itself; this module only resolves
  whichever one it is handed.
- the *resolved profile*: `mcp/server.py`'s `ServerMiddleware` sets it for the lifetime
  of one MCP request, after resolution; `observability/metrics.py`'s `track_tool_call`
  reads it back (`current_resolved_profile()`) to label `mm_tool_calls_total`. Defaults
  to `profiles.DEFAULT_PROFILE` outside of any request - at import time, in a unit test
  that calls a tool function directly, or in any of the roughly ten existing test modules
  that build an in-process `mcp/server.py` server with no HTTP layer and no
  `ServerMiddleware` run at all - so every one of those keeps behaving exactly as before
  this module existed.

Deliberately does not import from `memory_manager.mcp`/`memory_manager.app`, for the same
reason `compat/__init__.py`'s docstring gives: `mcp/server.py` imports this module, and a
reverse import here would make that a cycle. Only talks to `compat.profiles`'s public
registry (`get_profile`, `DEFAULT_PROFILE`) and its own two `ContextVar`s.
"""

from __future__ import annotations

from contextvars import ContextVar, Token

from memory_manager.compat.profiles import DEFAULT_PROFILE, Profile, get_profile

__all__ = [
    "current_profile_override",
    "current_resolved_profile",
    "reset_profile_override",
    "reset_resolved_profile",
    "resolve_profile",
    "set_profile_override",
    "set_resolved_profile",
]

# docs/research/clients/client-info-names.md, retrieved 2026-10-10: the Claude Code CLI's
# own MCP `Client` is constructed with the literal `clientInfo.name = "claude-code"` (read
# from the shipped binary, `mcp` 2.3.0's `Client` class) for every configured server,
# stdio or HTTP alike. claude.ai's own `clientInfo.name` is not sourced (same note - the
# live site returns a bot challenge to this environment, no bundle to read) and therefore
# stays out of this mapping: an unmapped name falls back to `DEFAULT_PROFILE` below, which
# is exactly what claude.ai gets until a sourced name can be added here.
_CLIENT_INFO_PROFILE: dict[str, str] = {
    "claude-code": "claude-code",
}

#: Set by `http.py`'s ASGI middleware (`?profile=`/`MM-Client-Profile`, already validated
#: against the registry) for the lifetime of one HTTP request; `None` outside of one,
#: including for the whole lifetime of a stdio process (`mcp/server.py`'s middleware
#: combines this with `build_server`'s `stdio_profile` keyword instead of relying on this
#: var for stdio).
_override_var: ContextVar[str | None] = ContextVar("mm_profile_override", default=None)

#: Set by `mcp/server.py`'s `ServerMiddleware` for the lifetime of one MCP request, to the
#: `Profile.name` `resolve_profile` picked for it. Defaults to `DEFAULT_PROFILE`, not
#: `None` - a reader outside of any request (module docstring) gets the same answer a
#: request that explicitly resolved to `default` would, never a third "no profile at all"
#: state a caller would have to special-case.
_resolved_var: ContextVar[str] = ContextVar("mm_resolved_profile", default=DEFAULT_PROFILE)


def set_profile_override(name: str) -> Token[str | None]:
    """Validate `name` against the registry and set it as this context's override.

    Raises `profiles.UnknownProfile` - naming `name` and every valid name - rather than
    setting anything, if `name` is not registered (ADR-0010: an override is rejected, not
    silently mapped). Returns the `contextvars.Token` `reset_profile_override` needs to
    undo exactly this `set` later, never a later one - the same token-per-set contract
    `observability/logging.py`'s `RequestIdMiddleware` relies on for `request_id_var`.
    """
    get_profile(name)
    return _override_var.set(name)


def reset_profile_override(token: Token[str | None]) -> None:
    """Undo exactly the `set_profile_override` call that returned `token`."""
    _override_var.reset(token)


def current_profile_override() -> str | None:
    """The override set for the request currently in flight, or `None` outside of one."""
    return _override_var.get()


def set_resolved_profile(name: str) -> Token[str]:
    """Record `name` as the profile resolved for the request currently in flight.

    `name` is trusted as already a registered profile name - every caller
    (`mcp/server.py`'s middleware) only ever passes `resolve_profile(...).name` here, never
    an unvalidated string.
    """
    return _resolved_var.set(name)


def reset_resolved_profile(token: Token[str]) -> None:
    """Undo exactly the `set_resolved_profile` call that returned `token`."""
    _resolved_var.reset(token)


def current_resolved_profile() -> str:
    """The profile resolved for the request currently in flight.

    `DEFAULT_PROFILE` outside of any request - see this module's docstring for exactly
    which situations that covers.
    """
    return _resolved_var.get()


def resolve_profile(client_info_name: str | None, *, override: str | None = None) -> Profile:
    """The `Profile` for one request, per ADR-0010's resolution order.

    `override`, when given, wins outright - it is trusted to be whatever
    `set_profile_override` (HTTP) or `build_server`'s `stdio_profile` keyword (stdio)
    already validated; this function re-validates it anyway through `get_profile` (the
    same defensive re-check every `get_profile` caller gets, not a new failure mode) and
    raises `profiles.UnknownProfile` if it somehow does not name a registered profile.

    Without an override, `client_info_name` - the connecting client's `clientInfo.name`
    for this request, or `None` when the transport could not read one (no handshake yet,
    or a client that sent none at all) - is looked up in `_CLIENT_INFO_PROFILE`. An
    unmapped or absent name resolves to `DEFAULT_PROFILE`, never guessed and never an
    error: ADR-0010's "unknown names are rejected" is about an *override* naming a profile
    directly, not about a client this registry simply does not recognize yet.

    Every `DeliveryMode` is deliverable (`"full"`, `"descriptions"` and `"short"` alike,
    #306) - `mcp/server.py`'s `_ProfileMiddleware` is the one place that branches on
    `delivery_mode`, so this function never has to.
    """
    if override is not None:
        name = override
    elif client_info_name is not None:
        name = _CLIENT_INFO_PROFILE.get(client_info_name, DEFAULT_PROFILE)
    else:
        name = DEFAULT_PROFILE
    return get_profile(name)
