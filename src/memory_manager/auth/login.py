# SPDX-License-Identifier: AGPL-3.0-only
"""The pluggable login seam for `/authorize` (ADR-0004 L3, #36/#37).

`auth.provider.MemoryManagerOAuthProvider.authorize` never shows a login
form itself: it parks the request (`auth.store.save_pending`) and redirects
the browser to `{LOGIN_PATH}?pending=<id>`. What happens there is this
module's job, and deliberately **not** this task's: the real login methods
(upstream OIDC / a single admin password, ADR-0004's L1/L2) are #37. All
this module provides for now is the seam they will plug into - the
`Authenticator` protocol - plus the routing/lookup plumbing around it,
wired together by `login_routes`. The only `Authenticator` that exists
today is `tests/auth/test_oauth_flow.py`'s `FakeAuthenticator`; production
without `LOGIN_MODE` set never reaches this module at all (`http.py`: no
authenticator configured means no OAuth authorization server, no `/login`
route, and thus no attacker-visible change of behaviour from before #36).

`PendingAuthorizationLookup`/`AuthorizationCompleter` are plain callables
rather than the provider class itself, on purpose: this module has no
import of `auth.provider` at all, so a different authorization-server
implementation could reuse the same `Authenticator` protocol and
`login_routes` without inheriting anything OAuth-specific from this one.
`http.py` is what binds the two together, passing
`provider.pending_authorization`/`provider.complete_authorization` in.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

__all__ = [
    "LOGIN_PATH",
    "Authenticator",
    "AuthorizationCompleter",
    "BoundCompleter",
    "PendingAuthorization",
    "PendingAuthorizationLookup",
    "login_routes",
]

LOGIN_PATH = "/login"

_PENDING_PARAM = "pending"


@dataclass(frozen=True)
class PendingAuthorization:
    """What `/login` has to show: a parked `/authorize` call, not yet completed.

    `client_name` is best-effort (`None` for a DCR client that never sent one);
    an `Authenticator` must still render *something* usable in that case.
    """

    id: str
    client_id: str
    client_name: str | None
    scopes: tuple[str, ...]
    resource: str | None


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
        pending_id = request.query_params.get(_PENDING_PARAM, "")
        if not pending_id and request.method == "POST":
            form = await request.form()
            pending_id = str(form.get(_PENDING_PARAM, ""))

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
