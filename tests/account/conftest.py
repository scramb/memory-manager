# SPDX-License-Identifier: AGPL-3.0-only
"""Fixtures for `tests/account/test_sessions.py` and `tests/account/test_page.py`.

`pool` is the same migrated-pool fixture `tests/auth/conftest.py` defines, repeated
here rather than imported - pytest's bare `from conftest import ...` imports resolve
to *some* `conftest.py` module named `conftest`, not necessarily this directory's own
one, so every package under `tests/` that needs it defines its own. The
`bare_remote`/`human_commit`/`human_delete`/`human_rename`/`vault_config` re-exports
are for the same reason (see `tests/auth/conftest.py`'s own docstring): this package
does not use them today, but re-exporting keeps any such import elsewhere in the
suite working regardless of which `conftest.py` collection order picks.

`FakeOidcProvider`/`fake_oidc_provider` is a trimmed copy of `tests/auth/conftest.
py`'s own fixture of the same name (that module's own docstring explains why a
`conftest.py`-named module cannot be imported from by name across packages) - only
the happy-path discovery/token/userinfo endpoints `test_page.py`'s one `oidc`-mode
test needs.

`postgres_app_role_environ` is the same disposable-`DATABASE_APP_ROLE` shape
`tests/auth/test_login_entra.py`'s own `entra_environ` builds, generalized to a
plain `STORAGE_BACKEND` (`git` or `postgres`) rather than always `postgres` -
`test_page.py`'s "note count only with STORAGE_BACKEND=postgres" test needs an
`EntraAuthenticator` running against the `git` backend, which `auth.login_entra`'s
own production `from_env` refuses but `create_app` (given an authenticator built
directly, not through it) does not.
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
    "postgres_app_role_environ",
    "vault_config",
]

_ISSUER = "https://idp.example.test"
_CLIENT_ID = "mm-oidc-client"
_CLIENT_SECRET = "s3cr3t-not-real-fake-oidc-client-secret"  # noqa: S105 - a fake test credential


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


@dataclass
class _IssuedCode:
    claims: dict[str, Any]
    used: bool = False


@dataclass
class FakeOidcProvider:
    """A minimal, spec-shaped OIDC provider: discovery + an authorization-code token
    endpoint + userinfo - nothing else `OidcAuthenticator` ever calls (see `tests/
    auth/conftest.py`'s own, fuller copy for the failure-injection knobs this one
    skips)."""

    issuer: str = _ISSUER
    client_id: str = _CLIENT_ID
    client_secret: str = _CLIENT_SECRET
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
        code = secrets.token_urlsafe(16)
        self._codes[code] = _IssuedCode(claims=dict(claims))
        return code

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": self.issuer,
                    "authorization_endpoint": self.authorization_endpoint,
                    "token_endpoint": self.token_endpoint,
                    "userinfo_endpoint": self.userinfo_endpoint,
                },
            )
        if path == "/oauth2/token":
            form = parse_qs(request.content.decode("utf-8"))
            code = form.get("code", [""])[0]
            issued = self._codes.get(code)
            if issued is None or issued.used:
                return httpx.Response(400, json={"error": "invalid_grant"})
            issued.used = True
            access_token = secrets.token_urlsafe(16)
            self._tokens[access_token] = issued.claims
            return httpx.Response(
                200,
                json={"access_token": access_token, "token_type": "Bearer", "expires_in": 3600},
            )
        if path == "/userinfo":
            bearer = request.headers.get("authorization", "")
            token = bearer.removeprefix("Bearer ")
            claims = self._tokens.get(token)
            if claims is None:
                return httpx.Response(401, json={"error": "invalid_token"})
            return httpx.Response(200, json=claims)
        return httpx.Response(404, json={"error": "not_found"})  # pragma: no cover - defensive


@pytest.fixture
def fake_oidc_provider() -> FakeOidcProvider:
    return FakeOidcProvider()


@pytest_asyncio.fixture
async def postgres_app_role_environ(
    admin_database_url: str, test_database_url: str
) -> AsyncIterator[dict[str, str]]:
    """`STORAGE_BACKEND=postgres` plus a disposable `DATABASE_APP_ROLE` - the same
    shape `tests/auth/test_login_entra.py`'s own `entra_environ` builds, needed
    whenever a test logs in through `EntraAuthenticator` (`account.sections`'s own
    `mm_ensure_personal_ns()` call needs a granted app role, ADR-0008 addendum,
    #116)."""
    role = f"mm_test_account_{secrets.token_hex(8)}"
    admin_conn = await asyncpg.connect(admin_database_url)
    try:
        await admin_conn.execute(f'create role "{role}" nologin nosuperuser nobypassrls')
    finally:
        await admin_conn.close()
    try:
        yield {
            "STORAGE_BACKEND": "postgres",
            "DATABASE_URL": test_database_url,
            "DATABASE_APP_ROLE": role,
        }
    finally:
        owned_conn: asyncpg.Connection | None
        try:
            owned_conn = await asyncpg.connect(test_database_url)
        except asyncpg.PostgresError:
            owned_conn = None
        if owned_conn is not None:
            try:
                await owned_conn.execute(f'drop owned by "{role}"')
            finally:
                await owned_conn.close()
        admin_conn = await asyncpg.connect(admin_database_url)
        try:
            await admin_conn.execute(f'drop role if exists "{role}"')
        finally:
            await admin_conn.close()
