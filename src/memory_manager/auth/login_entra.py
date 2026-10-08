# SPDX-License-Identifier: AGPL-3.0-only
"""The Entra ID `Authenticator` (ADR-0006, #215): the embedded OAuth authorization
server's authorization-code login delegated to Microsoft Entra ID.

`LOGIN_MODE=entra` next to `password` (ADR-0004 L1) and `oidc` (L2): this is
ADR-0006's option B, "the authorization-server facade" - MCP clients keep
talking to this server's own AS (DCR, CIMD, PKCE, `resource`, unchanged);
`/authorize` delegates the human login step to Entra. The wire plumbing
(discovery fetch/cache, the authorization-code token exchange, the
`state`-keyed pending-login bookkeeping on a `SharedState`) is the same
`auth.oidc_client` module `auth.login_oidc.OidcAuthenticator` already uses -
see that module's docstring for why both modes need it identically.

What is different from `oidc` mode, all per ADR-0006:

- The authority is tenant-specific (`{authority}/{tenant_id}/v2.0`, never
  `common`), and the scope requested is `openid profile offline_access` - no
  `email` (§1).
- No `userinfo_endpoint` call at all: identity comes from the ID token's own
  claims (`oid`, `tid`, `roles`, `groups`), read with **no signature
  check** - OIDC Core §3.1.3.7 lets TLS server authentication on the
  token-endpoint response stand in for it, the same addendum pattern
  `login_oidc.py` already uses for its own `userinfo_endpoint` call (§2).
  `_decode_unverified_claims` only base64-decodes the payload segment; it
  never touches the signature segment at all.
- The identity key is `oid`, not `sub` (§3). A user with none of
  `auth.tokens.MEMORY_ROLES` in the `roles` claim is denied outright.
- Groups come from the `groups` claim, or - on overage (`_claim_names`/
  `_claim_sources`, `docs/research/entra-contract.md` §4) - from
  `auth.graph.GraphClient.member_groups`, called with this authenticator's own
  app-only Graph credentials (§4).
- A successful login always calls `auth.users.upsert_user`/`replace_groups`
  (ADR-0008 addendum: "filled at login, never in the request path") and
  completes with a `LoginPrincipal` (`oid`, `roles`) and namespaces `["*"]` -
  per-namespace permission resolution from roles/groups is `mcp.namespaces`'s
  own job at request time (ADR-0008), not this module's.
- `ENTRA_MAX_SESSION`, the refresh-time re-check against Graph and 15-minute
  access tokens (§5) are #216's job, not this one's (issue #215 "Not
  included").
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode

import asyncpg
import httpx
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from memory_manager.auth.graph import GraphClient, GraphError
from memory_manager.auth.login import (
    LOGIN_PATH,
    PENDING_PARAM,
    AuthorizationCompleter,
    BoundCompleter,
    LoginPrincipal,
    PendingAuthorization,
)

# Both modes mount the same callback path (ADR-0006 §9) - imported, not
# redefined, so the two can never drift apart.
from memory_manager.auth.login_oidc import CALLBACK_PATH
from memory_manager.auth.oidc_client import (
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
from memory_manager.auth.tokens import ALL_NAMESPACES, MEMORY_ROLES
from memory_manager.auth.users import replace_groups, upsert_user
from memory_manager.config import ServerConfigError, canonical_resource_url

__all__ = ["CALLBACK_PATH", "EntraAuthenticator"]

_logger = logging.getLogger(__name__)

#: ADR-0006 §1: "plain v2 scopes `openid profile offline_access`, no `resource`
#: towards Entra" - no `email`, unlike `login_oidc.OidcAuthenticator`'s own `_SCOPE`.
_SCOPE = "openid profile offline_access"
_DISCOVERY_CACHE_TTL = 3600.0  # ADR-0009 §3: "OIDC/Entra discovery: 1 h"
_HTTP_TIMEOUT = 10.0
_CONFIRM_PARAM = "confirm"
_CONFIRMED = "1"
#: Matches `login_oidc.py`'s own `_STATE_TTL` - the same reasoning (a stale `state`
#: can never outlive the pending authorization it is bound to being useful at all).
_STATE_TTL = 600.0

_DEFAULT_AUTHORITY = "https://login.microsoftonline.com"
#: Matches `auth.graph.GraphClient`'s own default - duplicated here (not imported,
#: which is private to that module) so `from_env`'s own default is self-contained.
_DEFAULT_GRAPH_URL = "https://graph.microsoft.com/v1.0"
_DEFAULT_GROUPS_TTL_SECONDS = 3600.0


class EntraAuthenticator:
    """`LOGIN_MODE=entra`: ADR-0006's authorization-server facade in front of
    Microsoft Entra ID."""

    def __init__(
        self,
        *,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        authority: str = _DEFAULT_AUTHORITY,
        graph_url: str = _DEFAULT_GRAPH_URL,
        allowed_tenants: frozenset[str] | None = None,
        allow_insecure_authority: bool = False,
        groups_ttl_seconds: float = _DEFAULT_GROUPS_TTL_SECONDS,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._allowed_tenants = allowed_tenants if allowed_tenants else frozenset({tenant_id})
        # Not read anywhere yet - #216's refresh-time re-check is what decides
        # whether a cached group membership is stale enough to refetch; a login
        # always refetches regardless (see the module docstring).
        self._groups_ttl_seconds = groups_ttl_seconds
        self._issuer = f"{authority.rstrip('/')}/{tenant_id}/v2.0"

        # Only a self-created client is this instance's to close (`aclose`) - a
        # caller-supplied one (every test) stays the caller's own responsibility,
        # the same split `OidcAuthenticator` uses.
        self._owns_http_client = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=_HTTP_TIMEOUT)
        self._client = OidcDiscoveryClient(
            issuer=self._issuer, http_client=self._http, cache_ttl_seconds=_DISCOVERY_CACHE_TTL
        )
        # Raises `ServerConfigError` itself when `authority`/`graph_url` is not
        # `https://` and `allow_insecure_authority` was not set - the one check
        # this constructor needs for both of its own non-https inputs, already
        # built for #214.
        self._graph = GraphClient(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
            authority_base_url=authority,
            graph_base_url=graph_url,
            allow_insecure_authority=allow_insecure_authority,
            http_client=self._http,
        )
        self._shared_state: SharedState = InMemorySharedState()
        self._pool: asyncpg.Pool | None = None

    def bind_shared_state(self, state: SharedState) -> None:
        """Swap in a shared `SharedState` (a `PostgresSharedState`/`ValkeySharedState`) -
        called once by `http.py`'s `create_app`, the same `bind_shared_state` seam
        `OidcAuthenticator`/`PasswordAuthenticator` already use."""
        self._shared_state = state

    def bind_pool(self, pool: asyncpg.Pool) -> None:
        """Called once by `http.py`'s `create_app` lifespan once `services.pool`
        exists - guaranteed for `LOGIN_MODE=entra` (`build_authenticator` refuses to
        start without `STORAGE_BACKEND=postgres`, ADR-0006 addendum 2026-10-08)."""
        self._pool = pool

    async def aclose(self) -> None:
        """Close the internally created `httpx.AsyncClient`, if `__init__` made one
        itself - called by `http.py`'s `create_app` lifespan on shutdown. `self._graph`
        shares this same client (never created its own), so there is nothing else to
        close here."""
        if self._owns_http_client:
            await self._http.aclose()

    async def discovery_reachable(self) -> bool:
        """`True` once Entra discovery has succeeded at least once (then served from
        the 1 h cache) - `http.py`'s `/readyz` (ADR-0009 §5: "true only after ...
        Entra discovery ... is reachable"), never raising."""
        return await self._client.discovery_reachable()

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> EntraAuthenticator:
        """Build an `EntraAuthenticator` from `ENTRA_*`/`PUBLIC_URL`.

        Raises `ServerConfigError` if a required variable is missing, or if
        `ENTRA_AUTHORITY`/`ENTRA_GRAPH_URL` is not `https://` and
        `ENTRA_ALLOW_INSECURE_AUTHORITY` is not set (`auth.graph.GraphClient`'s own
        check, `__init__` above).
        """
        required = {
            "ENTRA_TENANT_ID": environ.get("ENTRA_TENANT_ID"),
            "ENTRA_CLIENT_ID": environ.get("ENTRA_CLIENT_ID"),
            "ENTRA_CLIENT_SECRET": environ.get("ENTRA_CLIENT_SECRET"),
            "PUBLIC_URL": environ.get("PUBLIC_URL"),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ServerConfigError(f"LOGIN_MODE=entra requires {', '.join(missing)} to be set")
        tenant_id = required["ENTRA_TENANT_ID"]
        client_id = required["ENTRA_CLIENT_ID"]
        client_secret = required["ENTRA_CLIENT_SECRET"]
        public_url = required["PUBLIC_URL"]
        if tenant_id is None or client_id is None or client_secret is None or public_url is None:
            raise AssertionError("unreachable: 'missing' above already covers every None case")

        allowed_tenants = frozenset(_parse_csv(environ.get("ENTRA_ALLOWED_TENANTS"))) or frozenset(
            {tenant_id}
        )
        authority = environ.get("ENTRA_AUTHORITY") or _DEFAULT_AUTHORITY
        graph_url = environ.get("ENTRA_GRAPH_URL") or _DEFAULT_GRAPH_URL
        allow_insecure_authority = _parse_bool(environ.get("ENTRA_ALLOW_INSECURE_AUTHORITY"))
        groups_ttl_seconds = _parse_positive_float(
            environ.get("ENTRA_GROUPS_TTL_SECONDS"), _DEFAULT_GROUPS_TTL_SECONDS
        )
        redirect_uri = canonical_resource_url(public_url, "") + CALLBACK_PATH

        return cls(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            authority=authority,
            graph_url=graph_url,
            allowed_tenants=allowed_tenants,
            allow_insecure_authority=allow_insecure_authority,
            groups_ttl_seconds=groups_ttl_seconds,
        )

    # ---- step 1: GET /login -------------------------------------------------------

    async def handle(
        self, request: Request, pending: PendingAuthorization, _complete: BoundCompleter
    ) -> Response:
        """Same interstitial-then-redirect shape as `OidcAuthenticator.handle` - see
        that method's docstring; `_complete` is unused for the same reason."""
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
            _logger.warning("entra discovery failed: %s", exc)
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
        """Finish the Entra round trip: validate `state`, exchange `code`, validate
        the ID token's claims (ADR-0006 §1), resolve roles/groups, record the user
        (ADR-0008 addendum), then `complete` with a `LoginPrincipal`."""
        params = request.query_params
        upstream_error = params.get("error")
        if upstream_error:
            _logger.info("entra callback: upstream returned error=%s", upstream_error)
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
        except OidcError as exc:
            _logger.warning("entra login failed: %s", exc)
            return html_response(
                login_error_page("Sign-in failed. Please try again."), status_code=400
            )

        id_token = token_response.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            _logger.warning("entra token response carried no id_token")
            return html_response(
                login_error_page("The identity provider did not return an ID token."),
                status_code=400,
            )

        claims = _decode_unverified_claims(id_token)
        if claims is None:
            _logger.warning("entra id_token could not be decoded")
            return html_response(
                login_error_page("The identity provider returned a malformed ID token."),
                status_code=400,
            )

        failed_claim = _validate_id_token(
            claims,
            issuer=discovery.issuer,
            client_id=self._client_id,
            allowed_tenants=self._allowed_tenants,
            expected_nonce=pending_state.nonce,
            now=time.time(),
        )
        if failed_claim is not None:
            _logger.warning("entra id_token failed validation: claim=%s", failed_claim)
            return html_response(
                login_error_page("Sign-in failed. Please try again."), status_code=400
            )

        oid = claims.get("oid")
        if not isinstance(oid, str) or not oid:
            _logger.warning("entra id_token carried no 'oid'")
            return html_response(
                login_error_page("The identity provider did not return a user id."),
                status_code=400,
            )

        roles = tuple(role for role in claims.get("roles", []) if role in MEMORY_ROLES)
        if not roles:
            return html_response(
                login_denied_page("Your account has no memory role assigned."), status_code=403
            )

        if _is_groups_overage(claims):
            try:
                groups: tuple[str, ...] = tuple(await self._graph.member_groups(oid))
            except GraphError as exc:
                _logger.warning("entra group lookup failed: %s", exc)
                return html_response(
                    login_error_page(
                        "Sign-in is temporarily unavailable. Please try again shortly."
                    ),
                    status_code=503,
                )
        else:
            raw_groups = claims.get("groups")
            groups = (
                tuple(str(group) for group in raw_groups) if isinstance(raw_groups, list) else ()
            )

        tid = str(claims["tid"])  # already validated present and allowed by _validate_id_token
        raw_name = claims.get("name")
        display_name = raw_name if isinstance(raw_name, str) and raw_name else oid

        pool = self._require_pool()
        await upsert_user(pool, oid, tid=tid, display_name=display_name)
        await replace_groups(pool, oid, groups)

        principal = LoginPrincipal(oid=oid, roles=roles)
        redirect_url = await complete(pending_state.pending_id, oid, [ALL_NAMESPACES], principal)
        if redirect_url is None:
            return html_response(
                login_error_page("This login link expired or was already used."), status_code=400
            )
        return RedirectResponse(redirect_url, status_code=302)

    def _require_pool(self) -> asyncpg.Pool:
        if self._pool is None:  # pragma: no cover - defensive, see bind_pool's docstring
            raise RuntimeError(
                "EntraAuthenticator.handle_callback was called before bind_pool - "
                "http.py's create_app lifespan is expected to call it once services.pool exists"
            )
        return self._pool


def entra_routes(
    *, complete: AuthorizationCompleter, authenticator: EntraAuthenticator
) -> list[Route]:
    """`{CALLBACK_PATH}` (`GET`) - mounted by `http.py` when the configured
    `Authenticator` is an `EntraAuthenticator` (`LOGIN_MODE=entra`). Kept as its own
    function (rather than reusing `login_oidc.oidc_routes` at the call site) only so
    `http.py` never has to import `EntraAuthenticator` through `login_oidc`; the route
    table it builds is identical."""

    async def callback(request: Request) -> Response:
        return await authenticator.handle_callback(request, complete)

    return [Route(CALLBACK_PATH, endpoint=callback, methods=["GET"])]


# ---- ID token claims (no signature check, ADR-0006 §2) ---------------------------


def _decode_unverified_claims(id_token: str) -> dict[str, Any] | None:
    """The ID token's payload claims, with no signature check at all - the token came
    TLS-direct from the token endpoint (`OidcDiscoveryClient.exchange_code`), the same
    trust basis `login_oidc.py`'s own `userinfo_endpoint` call relies on (module
    docstring). `None` if `id_token` is not a three-segment JWT or its payload segment
    is not a JSON object - never raises."""
    parts = id_token.split(".")
    if len(parts) != 3:
        return None
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, binascii.Error):
        return None
    return payload if isinstance(payload, dict) else None


def _is_groups_overage(claims: Mapping[str, Any]) -> bool:
    """`docs/research/entra-contract.md` §4: groups overage replaces the `groups`
    claim with `_claim_names`/`_claim_sources`."""
    claim_names = claims.get("_claim_names")
    return isinstance(claim_names, dict) and "groups" in claim_names


def _validate_id_token(
    claims: Mapping[str, Any],
    *,
    issuer: str,
    client_id: str,
    allowed_tenants: frozenset[str],
    expected_nonce: str,
    now: float,
) -> str | None:
    """`None` if `claims` passes every ADR-0006 item 1 check (`iss`/`tid`/`aud`/`exp`/
    `nonce`); otherwise the name of the first claim that failed - logged by
    `handle_callback`, never shown to the browser, which only ever sees a generic
    "sign-in failed"."""
    if claims.get("iss") != issuer:
        return "iss"
    tid = claims.get("tid")
    if not isinstance(tid, str) or tid not in allowed_tenants:
        return "tid"
    if claims.get("aud") != client_id:
        return "aud"
    exp = claims.get("exp")
    if not isinstance(exp, int | float) or exp <= now:
        return "exp"
    if claims.get("nonce") != expected_nonce:
        return "nonce"
    return None


def _parse_csv(raw: str | None) -> list[str]:
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


def _parse_bool(raw: str | None) -> bool:
    if raw is None:
        return False
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def _parse_positive_float(raw: str | None, default: float) -> float:
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ServerConfigError(f"ENTRA_GROUPS_TTL_SECONDS must be a number, got {raw!r}") from exc
    if value <= 0:
        raise ServerConfigError(f"ENTRA_GROUPS_TTL_SECONDS must be positive, got {value}")
    return value
