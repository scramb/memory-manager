# SPDX-License-Identifier: AGPL-3.0-only
"""The upstream-OIDC `Authenticator` (ADR-0004 L2, addendum 2026-10-07, #37).

No new dependency: the client is built on `httpx` alone (already a direct
dependency), per the addendum's decision against `authlib`/`joserfc` - this
module is the "~200 lines on httpx" the addendum describes. It deliberately
never parses or verifies an ID token's signature: the identity comes from
the upstream `userinfo_endpoint`, called with the access token the token
endpoint handed back directly over TLS (confidential `client_secret_basic`),
which is why no JOSE library is needed here at all.

Two round trips through this module, with the browser in between:

1. `handle` (`GET {LOGIN_PATH}?pending=...`, reached the same way
   `PasswordAuthenticator.handle` is - `auth.login.login_routes`): generates
   a PKCE pair, a `nonce` and a `state`, parks all three keyed by `state` on
   a `SharedState` (`_shared_state`, `bind_shared_state` - defaults to an
   `InMemorySharedState`, today's single-replica behaviour, until `http.py`'s
   `create_app`/`lifespan` binds a shared one once a database is configured,
   ADR-0009 §2, #103), and redirects the browser to the upstream
   `authorization_endpoint`.
2. `handle_callback` (`GET {CALLBACK_PATH}`, its own route - `oidc_routes`,
   mounted by `http.py` only when the configured `Authenticator` actually is
   one of these): looks `state` up (single-use - popped, not just read),
   exchanges `code` at the upstream `token_endpoint`, calls `userinfo_endpoint`
   with the returned access token, checks the mandatory allowlist
   (`OIDC_ALLOWED_EMAILS`/`OIDC_ALLOWED_SUBJECTS` - empty means nobody can log
   in, ADR-0004's addendum is explicit that an upstream IdP may otherwise
   issue tokens to any of its own accounts), and only then calls `complete`.

Discovery (`{issuer}/.well-known/openid-configuration`) is cached for
`_DISCOVERY_CACHE_TTL` and its own `issuer` field is checked against the
configured one *exactly* - a mismatch is a startup-adjacent configuration
problem (wrong `OIDC_ISSUER`, or a compromised/misrouted discovery document),
never silently accepted. "Exactly" means exactly: OIDC Discovery §4.3
requires the document's `issuer` to equal the configured one byte for byte,
trailing slash included - `self._issuer` is kept verbatim as configured for
that comparison; only the well-known URL itself is built by stripping
*one* trailing slash before joining `/.well-known/openid-configuration`
(an IdP like Hydra that advertises its issuer with a trailing slash, e.g.
`https://auth.example.test/`, must still compare equal - stripping the
slash before comparing would make this check pass for an issuer the
operator never configured).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlencode

import httpx
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from memory_manager.auth.login import (
    LOGIN_PATH,
    PENDING_PARAM,
    AuthorizationCompleter,
    BoundCompleter,
    PendingAuthorization,
    parse_namespace_map,
    parse_namespaces,
    resolve_namespaces,
)
from memory_manager.auth.oidc_client import (
    CallbackCapable,
    DiscoveryDocument,
    OidcDiscoveryClient,
    OidcError,
    begin_pending_login,
    take_pending_login,
)
from memory_manager.auth.shared_state import InMemorySharedState, SharedState
from memory_manager.auth.templates import (
    html_response,
    login_denied_page,
    login_error_page,
    oidc_interstitial_page,
)
from memory_manager.config import ServerConfigError, canonical_resource_url

__all__ = ["CALLBACK_PATH", "OidcAuthenticator", "oidc_routes"]

CALLBACK_PATH = "/oidc/callback"

_logger = logging.getLogger(__name__)

_SCOPE = "openid email profile"
_DISCOVERY_CACHE_TTL = 3600.0
_HTTP_TIMEOUT = 10.0
#: `{LOGIN_PATH}` query parameter `oidc_interstitial_page`'s "Continue to sign in" link
#: sets to `_CONFIRMED` - `handle`'s cue that the interstitial has already been shown
#: and it is time to actually redirect to the upstream `authorization_endpoint`.
_CONFIRM_PARAM = "confirm"
_CONFIRMED = "1"
#: How long a parked `state` stays valid - matches `auth.provider`'s own pending-
#: authorization TTL, since a stale `state` can never outlive the pending
#: authorization it is bound to being useful at all.
_STATE_TTL = 600.0


class OidcAuthenticator:
    """`LOGIN_MODE=oidc`: upstream OIDC login with a mandatory allowlist."""

    def __init__(
        self,
        *,
        issuer: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        allowed_emails: frozenset[str],
        allowed_subjects: frozenset[str],
        namespaces: Sequence[str],
        namespace_map: Mapping[str, Sequence[str]],
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        # Kept verbatim, trailing slash and all - see the module docstring on why
        # this must never be normalized before the discovery-document comparison.
        self._issuer = issuer
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._allowed_emails = allowed_emails
        self._allowed_subjects = allowed_subjects
        self._namespaces = list(namespaces)
        self._namespace_map = {key: list(value) for key, value in namespace_map.items()}
        # Only a self-created client is this instance's to close (`aclose`) - a
        # caller-supplied one (every test) stays the caller's own responsibility.
        self._owns_http_client = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=_HTTP_TIMEOUT)
        self._client = OidcDiscoveryClient(
            issuer=issuer, http_client=self._http, cache_ttl_seconds=_DISCOVERY_CACHE_TTL
        )
        self._shared_state: SharedState = InMemorySharedState()

    def bind_shared_state(self, state: SharedState) -> None:
        """Swap in a shared `SharedState` (a `PostgresSharedState`, once a database is
        configured) - called once by `http.py`'s `create_app` lifespan. Until then, or
        without a database at all, this authenticator's own `InMemorySharedState` keeps
        today's single-replica behaviour unchanged."""
        self._shared_state = state

    async def aclose(self) -> None:
        """Close the internally created `httpx.AsyncClient`, if `__init__` made one
        itself - called by `http.py`'s `create_app` lifespan on shutdown, so the
        connection pool does not outlive the app."""
        if self._owns_http_client:
            await self._http.aclose()

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> OidcAuthenticator:
        """Build an `OidcAuthenticator` from `OIDC_*`/`PUBLIC_URL`/`LOGIN_NAMESPACES*`.

        Raises `ServerConfigError` if a required variable is missing or the allowlist
        (`OIDC_ALLOWED_EMAILS`/`OIDC_ALLOWED_SUBJECTS`) is entirely empty - ADR-0004's
        addendum: an upstream IdP may issue tokens to any of its own accounts, so "deny
        by default" is enforced here, not left to an operator remembering to set one.
        """
        required = {
            "OIDC_ISSUER": environ.get("OIDC_ISSUER"),
            "OIDC_CLIENT_ID": environ.get("OIDC_CLIENT_ID"),
            "OIDC_CLIENT_SECRET": environ.get("OIDC_CLIENT_SECRET"),
            "PUBLIC_URL": environ.get("PUBLIC_URL"),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ServerConfigError(f"LOGIN_MODE=oidc requires {', '.join(missing)} to be set")
        issuer = required["OIDC_ISSUER"]
        client_id = required["OIDC_CLIENT_ID"]
        client_secret = required["OIDC_CLIENT_SECRET"]
        public_url = required["PUBLIC_URL"]
        if issuer is None or client_id is None or client_secret is None or public_url is None:
            raise AssertionError("unreachable: 'missing' above already covers every None case")

        allowed_emails = _parse_csv_lower(environ.get("OIDC_ALLOWED_EMAILS"))
        allowed_subjects = _parse_csv(environ.get("OIDC_ALLOWED_SUBJECTS"))
        if not allowed_emails and not allowed_subjects:
            raise ServerConfigError(
                "LOGIN_MODE=oidc requires at least one of OIDC_ALLOWED_EMAILS/"
                "OIDC_ALLOWED_SUBJECTS - an empty allowlist would mean nobody can log in"
            )

        redirect_uri = canonical_resource_url(public_url, "") + CALLBACK_PATH
        namespace_map = parse_namespace_map(environ.get("LOGIN_NAMESPACE_MAP"))
        default_namespaces = parse_namespaces(environ.get("LOGIN_NAMESPACES"))

        return cls(
            issuer=issuer,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            allowed_emails=frozenset(allowed_emails),
            allowed_subjects=frozenset(allowed_subjects),
            namespaces=default_namespaces,
            namespace_map=namespace_map,
        )

    # ---- step 1: GET /login -------------------------------------------------------

    async def handle(
        self, request: Request, pending: PendingAuthorization, _complete: BoundCompleter
    ) -> Response:
        """`oidc_interstitial_page` first (the spec's consent-screen redirect-hostname
        notice - the upstream's own login page is not this server's to show it on), then
        - once `_CONFIRM_PARAM` shows the user clicked through - a 302 to the upstream
        `authorization_endpoint`.

        `_complete` is unused here: this mode never completes a pending authorization
        from `/login` itself, only from `handle_callback` once the upstream round trip
        comes back - `pending.id` is what gets parked under `state` for that to find
        again.
        """
        if request.query_params.get(_CONFIRM_PARAM) != _CONFIRMED:
            continue_url = (
                f"{LOGIN_PATH}?{urlencode({PENDING_PARAM: pending.id, _CONFIRM_PARAM: _CONFIRMED})}"
            )
            return html_response(
                oidc_interstitial_page(
                    client_name=pending.client_name,
                    redirect_uri=pending.redirect_uri,
                    continue_url=continue_url,
                )
            )

        try:
            discovery = await self._client.discovery_document()
        except OidcError as exc:
            _logger.warning("oidc discovery failed: %s", exc)
            return html_response(
                login_error_page("Sign-in is temporarily unavailable. Please try again shortly."),
                status_code=503,
            )

        state, nonce, code_challenge = await begin_pending_login(
            self._shared_state, pending_id=pending.id, ttl_seconds=_STATE_TTL
        )

        params = {
            "response_type": "code",
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "scope": _SCOPE,
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return RedirectResponse(
            f"{discovery.authorization_endpoint}?{urlencode(params)}", status_code=302
        )

    # ---- step 2: GET /oidc/callback -----------------------------------------------

    async def handle_callback(self, request: Request, complete: AuthorizationCompleter) -> Response:
        """Finish the upstream flow: validate `state`, exchange `code`, check the
        allowlist, then `complete` the pending authorization `state` was bound to."""
        params = request.query_params
        upstream_error = params.get("error")
        if upstream_error:
            _logger.info("oidc callback: upstream returned error=%s", upstream_error)
            return html_response(
                login_error_page("Sign-in was cancelled or failed upstream."), status_code=400
            )

        code = params.get("code")
        pending_state = await take_pending_login(self._shared_state, params.get("state", ""))
        if pending_state is None or not code:
            return html_response(
                login_error_page(
                    "This sign-in attempt is invalid or has expired. Start again from your client."
                ),
                status_code=400,
            )

        try:
            discovery = await self._client.discovery_document()
            token_response = await self._client.exchange_code(
                discovery,
                code=code,
                code_verifier=pending_state.code_verifier,
                redirect_uri=self._redirect_uri,
                client_id=self._client_id,
                client_secret=self._client_secret,
            )
            access_token = token_response.get("access_token")
            if not isinstance(access_token, str) or not access_token:
                raise OidcError("token response carried no access_token")
            claims = await self._userinfo(discovery, access_token=access_token)
        except OidcError as exc:
            _logger.warning("oidc login failed: %s", exc)
            return html_response(
                login_error_page("Sign-in failed. Please try again."), status_code=400
            )

        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            _logger.warning("oidc userinfo response carried no 'sub'")
            return html_response(
                login_error_page("The identity provider did not return a subject."),
                status_code=400,
            )

        raw_email = claims.get("email")
        email = raw_email.lower() if isinstance(raw_email, str) and raw_email else None
        email_verified = bool(claims.get("email_verified", False))

        if not self._is_allowed(subject=subject, email=email, email_verified=email_verified):
            return html_response(
                login_denied_page("Your account is not allowed to sign in to this server."),
                status_code=403,
            )

        keys = [subject] + ([email] if email else [])
        namespaces = resolve_namespaces(
            keys, namespace_map=self._namespace_map, default=self._namespaces
        )

        redirect_url = await complete(pending_state.pending_id, subject, namespaces)
        if redirect_url is None:
            return html_response(
                login_error_page("This login link expired or was already used."), status_code=400
            )
        return RedirectResponse(redirect_url, status_code=302)

    # ---- allowlist ------------------------------------------------------------------

    def _is_allowed(self, *, subject: str, email: str | None, email_verified: bool) -> bool:
        if subject in self._allowed_subjects:
            return True
        return email is not None and email_verified and email in self._allowed_emails

    # ---- userinfo (the one part of the discovery document only `oidc` mode needs) ---

    async def _userinfo(self, discovery: DiscoveryDocument, *, access_token: str) -> dict[str, Any]:
        if discovery.userinfo_endpoint is None:
            raise OidcError("discovery document is missing 'userinfo_endpoint'")
        try:
            response = await self._http.get(
                discovery.userinfo_endpoint, headers={"Authorization": f"Bearer {access_token}"}
            )
            response.raise_for_status()
            result: dict[str, Any] = response.json()
            return result
        except httpx.HTTPError as exc:
            raise OidcError(f"userinfo request failed: {exc}") from exc


def oidc_routes(*, complete: AuthorizationCompleter, authenticator: CallbackCapable) -> list[Route]:
    """`{CALLBACK_PATH}` (`GET`) - mounted by `http.py` when the configured
    `Authenticator` is an `OidcAuthenticator` (`LOGIN_MODE=oidc`) or an
    `EntraAuthenticator` (`LOGIN_MODE=entra`, #215 - both reuse this callback path,
    ADR-0006 §9)."""

    async def callback(request: Request) -> Response:
        return await authenticator.handle_callback(request, complete)

    return [Route(CALLBACK_PATH, endpoint=callback, methods=["GET"])]


def _parse_csv(raw: str | None) -> list[str]:
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


def _parse_csv_lower(raw: str | None) -> list[str]:
    return [item.strip().lower() for item in (raw or "").split(",") if item.strip()]
