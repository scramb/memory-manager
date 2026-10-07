# SPDX-License-Identifier: AGPL-3.0-only
"""The pluggable login seam for `/authorize` (ADR-0004 L3, #36/#37).

`auth.provider.MemoryManagerOAuthProvider.authorize` never shows a login
form itself: it parks the request (`auth.store.save_pending`) and redirects
the browser to `{LOGIN_PATH}?pending=<id>`. What happens there is this
module's job: the `Authenticator` protocol plus the routing/lookup plumbing
around it, wired together by `login_routes`. The real login methods -
`auth.login_password.PasswordAuthenticator` (a single admin password) and
`auth.login_oidc.OidcAuthenticator` (upstream OIDC), ADR-0004's L1/L2 - live
in their own modules, not here, and both import this module rather than the
other way around: `tests/auth/test_oauth_flow.py`'s `FakeAuthenticator` is
still a valid third `Authenticator`, so none of the OAuth-authorization-server
plumbing may depend on either real implementation. Production without
`LOGIN_MODE` set never reaches this module at all (`http.py`: no
authenticator configured means no OAuth authorization server, no `/login`
route, and thus no attacker-visible change of behaviour from before #36).

`PendingAuthorizationLookup`/`AuthorizationCompleter` are plain callables
rather than the provider class itself, on purpose: this module has no
import of `auth.provider` at all, so a different authorization-server
implementation could reuse the same `Authenticator` protocol and
`login_routes` without inheriting anything OAuth-specific from this one.
`http.py` is what binds the two together, passing
`provider.pending_authorization`/`provider.complete_authorization` in.

`parse_namespaces`/`parse_namespace_map`/`resolve_namespaces` are the one
piece of logic both real `Authenticator`s need identically (ADR-0004's
"Token subject -> allowed namespaces via config"): `LOGIN_NAMESPACES` (a
comma list, default `"*"` - `auth.tokens.ALL_NAMESPACES`) is the fallback
every subject gets; `LOGIN_NAMESPACE_MAP` (a JSON object keyed by subject or
email, case-insensitively) overrides it per subject. Kept here rather than
duplicated in `login_password`/`login_oidc` - neither imports `auth.provider`
either, so sharing them here does not weaken the layering above.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from memory_manager.config import ServerConfigError

__all__ = [
    "LOGIN_PATH",
    "PENDING_PARAM",
    "Authenticator",
    "AuthorizationCompleter",
    "BoundCompleter",
    "PendingAuthorization",
    "PendingAuthorizationLookup",
    "login_routes",
    "parse_namespace_map",
    "parse_namespaces",
    "resolve_namespaces",
]

LOGIN_PATH = "/login"

#: The query/form parameter `/login` carries the pending authorization's id under -
#: public so `auth.login_oidc`'s own interstitial page can build a same-shaped
#: `{LOGIN_PATH}?{PENDING_PARAM}=<id>&...` continue link without duplicating the name.
PENDING_PARAM = "pending"

#: `LOGIN_NAMESPACES`'s default when unset (ADR-0004: "Token subject -> allowed
#: namespaces via config") - every namespace, the same literal `auth.tokens.
#: ALL_NAMESPACES` uses for a static token created without `--namespace`.
_DEFAULT_LOGIN_NAMESPACES = ("*",)


@dataclass(frozen=True)
class PendingAuthorization:
    """What `/login` has to show: a parked `/authorize` call, not yet completed.

    `client_name` is best-effort (`None` for a DCR client that never sent one);
    an `Authenticator` must still render *something* usable in that case.
    `redirect_uri` is always set (`auth.provider`'s `authorize` never parks a request
    without one) - the spec's consent-screen requirement to show the redirect hostname
    (`auth.templates`) is why this field exists here at all.
    """

    id: str
    client_id: str
    client_name: str | None
    scopes: tuple[str, ...]
    resource: str | None
    redirect_uri: str


#: `pending.id -> PendingAuthorization | None` (`None` if unknown or expired).
PendingAuthorizationLookup = Callable[[str], Awaitable[PendingAuthorization | None]]

#: `(pending_id, subject, namespaces) -> redirect URL | None` (`None` if the pending
#: authorization vanished - expired, or already completed by a concurrent request -
#: between the lookup above and this call).
AuthorizationCompleter = Callable[[str, str, Sequence[str]], Awaitable[str | None]]

#: What an `Authenticator.handle` call is given to finish a pending authorization:
#: already bound to `pending.id`, so an implementation only ever supplies `subject`
#: and the namespaces that subject may use.
BoundCompleter = Callable[[str, Sequence[str]], Awaitable[str | None]]


class Authenticator(Protocol):
    """How a human proves who they are during `/authorize` (ADR-0004 L1/L2/L3).

    `handle` is called for both `GET` (render the login UI) and `POST` (a submitted
    login attempt) on `/login`; which one `request.method` is is the implementation's
    own business; `login_routes` does not distinguish them.

    On a successful login, call `complete(subject, namespaces)` and redirect the
    browser to the URL it returns (`None` means the pending authorization expired
    or was already used - show an error instead of redirecting to `None`).
    """

    async def handle(
        self, request: Request, pending: PendingAuthorization, complete: BoundCompleter
    ) -> Response: ...


def login_routes(
    *,
    lookup: PendingAuthorizationLookup,
    complete: AuthorizationCompleter,
    authenticator: Authenticator,
) -> list[Route]:
    """The `/login` route (GET + POST), dispatching to `authenticator` for everything else.

    Only mounted by `http.py` when an `Authenticator` is actually configured - see this
    module's docstring for why that is never true in production yet.
    """

    async def handle(request: Request) -> Response:
        pending_id = request.query_params.get(PENDING_PARAM, "")
        if not pending_id and request.method == "POST":
            form = await request.form()
            pending_id = str(form.get(PENDING_PARAM, ""))

        pending = await lookup(pending_id)
        if pending is None:
            return PlainTextResponse(
                "This login link has expired or was already used. "
                "Start the connection again from your client.",
                status_code=400,
            )

        async def bound_complete(subject: str, namespaces: Sequence[str]) -> str | None:
            return await complete(pending.id, subject, namespaces)

        return await authenticator.handle(request, pending, bound_complete)

    return [Route(LOGIN_PATH, endpoint=handle, methods=["GET", "POST"])]


def parse_namespaces(raw: str | None) -> list[str]:
    """`LOGIN_NAMESPACES` (comma-separated), or `_DEFAULT_LOGIN_NAMESPACES` if `raw` is
    unset or empty."""
    if not raw:
        return list(_DEFAULT_LOGIN_NAMESPACES)
    namespaces = [namespace.strip() for namespace in raw.split(",") if namespace.strip()]
    return namespaces or list(_DEFAULT_LOGIN_NAMESPACES)


def parse_namespace_map(raw: str | None) -> dict[str, list[str]]:
    """`LOGIN_NAMESPACE_MAP` (a JSON object, `"<sub or email>": ["ns", ...]`), with every
    key lower-cased so a later lookup by email never has to case-fold itself.

    Raises `ServerConfigError` if `raw` is set but is not a JSON object of string lists -
    a startup failure, the same way a malformed `PORT` is (`config.py`).
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ServerConfigError(f"LOGIN_NAMESPACE_MAP is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ServerConfigError("LOGIN_NAMESPACE_MAP must be a JSON object")
    namespace_map: dict[str, list[str]] = {}
    for key, value in parsed.items():
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ServerConfigError(
                f"LOGIN_NAMESPACE_MAP[{key!r}] must be a list of namespace strings"
            )
        namespace_map[str(key).lower()] = list(value)
    return namespace_map


def resolve_namespaces(
    keys: Sequence[str], *, namespace_map: Mapping[str, Sequence[str]], default: Sequence[str]
) -> list[str]:
    """The namespaces a logged-in subject may use: the first of `keys` (tried in order,
    lower-cased) found in `namespace_map` wins; `default` (`parse_namespaces`'s result)
    otherwise. `keys` is typically `[subject]` (password mode, always `"admin"`) or
    `[subject, email]` (OIDC mode - the subject is tried first as the more stable
    identifier, falling back to email only if the map has no entry for it)."""
    for key in keys:
        match = namespace_map.get(key.lower())
        if match is not None:
            return list(match)
    return list(default)
