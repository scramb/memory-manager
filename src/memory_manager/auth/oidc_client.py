# SPDX-License-Identifier: AGPL-3.0-only
"""The OIDC wire plumbing shared by `auth/login_oidc.py` (`LOGIN_MODE=oidc`) and
`auth/login_entra.py` (`LOGIN_MODE=entra`, #215): discovery-document fetch/cache,
the authorization-code token exchange, and the `state`-keyed pending-login
bookkeeping on a `SharedState`.

Extracted from `login_oidc.py` without behaviour change (#215's first
Implementation step) - `OidcAuthenticator` is still the only caller `tests/
auth/test_login.py` exercises, so every wire shape and error message below is
the one that module already had. `EntraAuthenticator` is the second caller:
both need "fetch and cache `{issuer}/.well-known/openid-configuration`,
exact-match its own `issuer` field (OIDC Discovery §4.3)" and "exchange a
`code` for a token response with `client_secret_basic`" identically - the
difference between the two modes (how the result is used: `userinfo_endpoint`
for `oidc`, the ID token's own claims with no signature check for `entra`,
ADR-0006 §2) stays in each authenticator's own module.

No new dependency: built on `httpx` alone, the same choice `login_oidc.py`
already made (ADR-0004 addendum).
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from starlette.requests import Request
from starlette.responses import Response

from memory_manager.auth.login import AuthorizationCompleter
from memory_manager.auth.shared_state import SharedState

__all__ = [
    "CallbackCapable",
    "DiscoveryDocument",
    "OidcDiscoveryClient",
    "OidcError",
    "PendingOidcState",
    "begin_pending_login",
    "pkce_challenge",
    "take_pending_login",
]


class OidcError(Exception):
    """Something about discovery or the token exchange failed - caught by each
    authenticator's own `handle`/`handle_callback` and turned into a generic,
    detail-free error page, never leaked to the browser."""


@dataclass(frozen=True)
class DiscoveryDocument:
    """The subset of an OIDC discovery document either authenticator reads.

    `userinfo_endpoint` is `None` when the document carries none at all - true of
    every real Entra discovery document (`docs/research/entra-contract.md` §1:
    not part of the documented response), so `OidcDiscoveryClient` never requires
    it; `login_oidc.OidcAuthenticator` (the only caller that needs it) checks its
    own presence itself.
    """

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    userinfo_endpoint: str | None = None


@dataclass(frozen=True)
class PendingOidcState:
    """What `begin_pending_login` parks under `state`, for `take_pending_login` to
    pick back up - identical shape for `oidc` and `entra` (#215): both need
    exactly the pending authorization id, the PKCE verifier and the `nonce` they
    sent upstream, across the same browser round trip."""

    pending_id: str
    code_verifier: str
    nonce: str


class OidcDiscoveryClient:
    """Discovery-document fetch/cache plus the authorization-code token exchange,
    for one configured `issuer` - owns neither the `httpx.AsyncClient` it is given
    nor any caching beyond `cache_ttl_seconds`'s in-memory one.

    "Exactly" below means exactly: OIDC Discovery §4.3 requires the document's
    own `issuer` field to equal the configured one byte for byte, trailing slash
    included - `issuer` is kept verbatim as given for that comparison; only the
    well-known URL itself is built by stripping *one* trailing slash before
    joining `/.well-known/openid-configuration` (an IdP that advertises its
    issuer with a trailing slash must still compare equal - stripping the slash
    before comparing would make this check pass for an issuer that was never
    actually configured).
    """

    def __init__(
        self, *, issuer: str, http_client: httpx.AsyncClient, cache_ttl_seconds: float = 3600.0
    ) -> None:
        self._issuer = issuer
        self._http = http_client
        self._cache_ttl_seconds = cache_ttl_seconds
        self._discovery: DiscoveryDocument | None = None
        self._discovery_at = 0.0

    async def discovery_document(self) -> DiscoveryDocument:
        now = time.monotonic()
        if self._discovery is not None and now - self._discovery_at < self._cache_ttl_seconds:
            return self._discovery

        url = f"{self._issuer.rstrip('/')}/.well-known/openid-configuration"
        try:
            response = await self._http.get(url)
            response.raise_for_status()
            data: dict[str, Any] = response.json()
        except httpx.HTTPError as exc:
            raise OidcError(f"discovery request to {url!r} failed: {exc}") from exc

        issuer = data.get("issuer")
        if issuer != self._issuer:
            raise OidcError(
                f"discovery document issuer {issuer!r} does not match configured "
                f"issuer {self._issuer!r}"
            )
        try:
            discovery = DiscoveryDocument(
                issuer=issuer,
                authorization_endpoint=data["authorization_endpoint"],
                token_endpoint=data["token_endpoint"],
                userinfo_endpoint=data.get("userinfo_endpoint"),
            )
        except KeyError as exc:
            raise OidcError(f"discovery document is missing {exc}") from exc

        self._discovery = discovery
        self._discovery_at = now
        return discovery

    async def discovery_reachable(self) -> bool:
        """`True` once `discovery_document` has succeeded at least once (then served
        from cache, `cache_ttl_seconds`) - `/readyz`'s own live check
        (`EntraAuthenticator.discovery_reachable`, ADR-0009 §5: "Entra discovery is
        reachable"), never raising."""
        try:
            await self.discovery_document()
        except OidcError:
            return False
        return True

    async def exchange_code(
        self,
        discovery: DiscoveryDocument,
        *,
        code: str,
        code_verifier: str,
        redirect_uri: str,
        client_id: str,
        client_secret: str,
    ) -> dict[str, Any]:
        try:
            response = await self._http.post(
                discovery.token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "code_verifier": code_verifier,
                },
                auth=(client_id, client_secret),
            )
            response.raise_for_status()
            result: dict[str, Any] = response.json()
            return result
        except httpx.HTTPError as exc:
            raise OidcError(f"token exchange failed: {exc}") from exc


def pkce_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


async def begin_pending_login(
    shared_state: SharedState, *, pending_id: str, ttl_seconds: float
) -> tuple[str, str, str]:
    """Generates a PKCE pair, a `nonce` and a `state`, parks `(pending_id,
    code_verifier, nonce)` under `state` on `shared_state`, and returns
    `(state, nonce, code_challenge)` - everything `handle`'s redirect to the
    upstream `authorization_endpoint` needs, for either mode."""
    code_verifier = secrets.token_urlsafe(48)
    nonce = secrets.token_urlsafe(24)
    state = secrets.token_urlsafe(32)
    payload = json.dumps({"pending_id": pending_id, "code_verifier": code_verifier, "nonce": nonce})
    await shared_state.put_pending(state, payload, ttl_seconds=ttl_seconds)
    return state, nonce, pkce_challenge(code_verifier)


async def take_pending_login(shared_state: SharedState, state: str) -> PendingOidcState | None:
    """`PendingOidcState` parked under `state` (single-use - `SharedState.
    take_pending` deletes it in the same round trip), or `None` if `state` is
    empty, unknown, already used, or past its TTL."""
    if not state:
        return None
    payload = await shared_state.take_pending(state)
    if payload is None:
        return None
    try:
        data = json.loads(payload)
        return PendingOidcState(
            pending_id=data["pending_id"],
            code_verifier=data["code_verifier"],
            nonce=data["nonce"],
        )
    except (ValueError, KeyError, TypeError):  # pragma: no cover - defensive
        return None


class CallbackCapable(Protocol):
    """What `login_oidc.oidc_routes` needs from an authenticator to mount
    `{CALLBACK_PATH}` - both `OidcAuthenticator` and `EntraAuthenticator` satisfy
    this structurally, without `login_oidc` ever importing `login_entra`
    (avoiding a circular import between the two sibling modules)."""

    async def handle_callback(
        self, request: Request, complete: AuthorizationCompleter
    ) -> Response: ...
