# SPDX-License-Identifier: AGPL-3.0-only
"""Fixtures shared by `tests/auth/test_login.py` and `tests/auth/test_oauth_flow.py`.

`FakeOidcProvider` is not a browser-driven upstream login page: the actual
"a human logged in at the IdP and consented" step it stands in for happens
entirely inside the test (`issue_code`) - what matters for
`memory_manager.auth.login_oidc.OidcAuthenticator` is that its own httpx
calls (discovery, the token exchange, userinfo) see a real, spec-shaped OIDC
provider on the other end, not that a consent page exists anywhere. It is
wired into an `OidcAuthenticator` through that class's `http_client`
constructor parameter (`httpx.AsyncClient(transport=httpx.MockTransport(
provider.handler))`) - the same transport-injection idiom
`tests/index/test_embeddings.py` already uses for an HTTP-calling provider,
chosen over monkeypatching `httpx.AsyncClient` globally because
`OidcAuthenticator` already takes the client as a parameter.

`bare_remote`/`human_commit`/`human_delete`/`human_rename`/`vault_config` are
re-exported from `tests/git_fixtures.py`, same as `tests/vault/conftest.py`
does and for the same reason (see that file's docstring): pytest's bare
`from conftest import ...` imports resolve to *some* `conftest.py` module
named `conftest` depending on collection order, not necessarily this
directory's own one - re-exporting the same names here keeps any such
import elsewhere in the suite working regardless of which one wins.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

import asyncpg
import httpx
import pytest
import pytest_asyncio
from git_fixtures import bare_remote, human_commit, human_delete, human_rename, vault_config

from memory_manager.db.migrate import migrate

__all__ = [
    "FakeOidcProvider",
    "bare_remote",
    "fake_oidc_provider",
    "human_commit",
    "human_delete",
    "human_rename",
    "pool",
    "vault_config",
]

DEFAULT_ISSUER = "https://idp.example.test"
DEFAULT_CLIENT_ID = "mm-oidc-client"
DEFAULT_CLIENT_SECRET = "s3cr3t-not-real-fake-oidc-client-secret"  # noqa: S105 - a fake test credential


@dataclass
class _IssuedCode:
    claims: dict[str, Any]
    used: bool = False


@dataclass
class FakeOidcProvider:
    """A minimal, spec-shaped OIDC provider: discovery + an authorization-code token
    endpoint + userinfo - nothing else `OidcAuthenticator` ever calls.

    `fail_discovery`/`fail_token`/`discovery_issuer_override` let a test provoke the
    specific upstream failures `OidcAuthenticator` has to turn into a generic error page
    rather than ever leaking: a discovery request that fails outright, one whose `issuer`
    field does not match the configured `OIDC_ISSUER`, or a token endpoint that is down.
    """

    issuer: str = DEFAULT_ISSUER
    client_id: str = DEFAULT_CLIENT_ID
    client_secret: str = DEFAULT_CLIENT_SECRET
    fail_discovery: bool = False
    fail_token: bool = False
    discovery_issuer_override: str | None = None
    _codes: dict[str, _IssuedCode] = field(default_factory=dict)
    _tokens: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def authorization_endpoint(self) -> str:
        return f"{self.issuer}/oauth2/authorize"

    @property
    def token_endpoint(self) -> str:
        return f"{self.issuer}/oauth2/token"

    @property
    def userinfo_endpoint(self) -> str:
        return f"{self.issuer}/userinfo"

    def issue_code(self, claims: dict[str, Any]) -> str:
        """Register `claims` under a fresh opaque code - this fake's stand-in for "a
        human logged in at the IdP"; the test appends the result to the callback URL
        exactly where a real browser redirect would carry it."""
        code = secrets.token_urlsafe(16)
        self._codes[code] = _IssuedCode(claims=dict(claims))
        return code

    def handler(self, request: httpx.Request) -> httpx.Response:
        """The single `httpx.MockTransport` handler covering every endpoint above."""
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            return self._discovery_response()
        if path == "/oauth2/token":
            return self._token_response(request)
        if path == "/userinfo":
            return self._userinfo_response(request)
        return httpx.Response(404, json={"error": "not_found"})  # pragma: no cover - defensive

    def _discovery_response(self) -> httpx.Response:
        if self.fail_discovery:
            return httpx.Response(503, json={"error": "discovery_unavailable"})
        issuer = (
            self.discovery_issuer_override
            if self.discovery_issuer_override is not None
            else self.issuer
        )
        return httpx.Response(
            200,
            json={
                "issuer": issuer,
                "authorization_endpoint": self.authorization_endpoint,
                "token_endpoint": self.token_endpoint,
                "userinfo_endpoint": self.userinfo_endpoint,
            },
        )

    def _token_response(self, request: httpx.Request) -> httpx.Response:
        if self.fail_token:
            return httpx.Response(503, json={"error": "token_endpoint_unavailable"})
        form = parse_qs(request.content.decode("utf-8"))
        code = form.get("code", [""])[0]
        issued = self._codes.get(code)
        if issued is None or issued.used:
            return httpx.Response(400, json={"error": "invalid_grant"})
        issued.used = True
        access_token = secrets.token_urlsafe(16)
        self._tokens[access_token] = issued.claims
        return httpx.Response(
            200, json={"access_token": access_token, "token_type": "Bearer", "expires_in": 3600}
        )

    def _userinfo_response(self, request: httpx.Request) -> httpx.Response:
        bearer = request.headers.get("authorization", "")
        token = bearer.removeprefix("Bearer ")
        claims = self._tokens.get(token)
        if claims is None:
            return httpx.Response(401, json={"error": "invalid_token"})
        return httpx.Response(200, json=claims)


@pytest.fixture
def fake_oidc_provider() -> FakeOidcProvider:
    return FakeOidcProvider()


@pytest_asyncio.fixture
async def pool(test_database_url: str) -> AsyncIterator[asyncpg.Pool]:
    conn = await asyncpg.connect(test_database_url)
    try:
        await migrate(conn)
    finally:
        await conn.close()
    created_pool = await asyncpg.create_pool(test_database_url)
    try:
        yield created_pool
    finally:
        await created_pool.close()
