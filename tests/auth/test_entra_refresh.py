# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for #216: the ADR-0006 §5 Graph re-check on every refresh of an
entra-bound token family, `ENTRA_MAX_SESSION`, and 15-minute entra access
tokens.

Driven the same way `tests/auth/test_login_entra.py` drives a full
`LOGIN_MODE=entra` login - `mock_idp_client` (`tests/mock_idp_fixtures.py`)
plugged in as `EntraAuthenticator`'s own `http_client`, a real Postgres
(`entra_environ`, duplicated from that module rather than imported, since
neither module re-exports its private helpers) - then a direct
`grant_type=refresh_token` call against `/token` for the refresh-specific
behaviour itself.

There is no injectable clock here: `family_started_at`/`groups_fetched_at`
are real Postgres `now()` timestamps (`db/migrations/0005_rls.sql`,
`0010_oauth_token_principal.sql`), and `auth.provider`/`auth.login_entra`
compare them against a real `datetime.now(UTC)` - deliberately, to keep this
seam-free (see those two modules' own docstrings). A test instead moves the
*stored* timestamp into the past, directly through `pool`, exactly the way a
real family that has been refreshed for `ENTRA_MAX_SESSION` or a groups cache
older than `ENTRA_GROUPS_TTL_SECONDS` would look on disk.
"""

from __future__ import annotations

import base64
import hashlib
import html
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

import asyncpg
import httpx
import pytest_asyncio
from mock_idp_fixtures import mock_idp_client
from starlette.applications import Starlette

from memory_manager.app import open_services
from memory_manager.auth import store
from memory_manager.auth.login import LOGIN_PATH
from memory_manager.auth.login_entra import CALLBACK_PATH, EntraAuthenticator
from memory_manager.config import ServerConfig
from memory_manager.http import create_app

__all__ = ["mock_idp_client"]

_PUBLIC_URL = "https://mm.example.test"
_MCP_PATH = "/mcp"
_REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"
_CLIENT_SECRET_KEY = "f2F7cFcySJJZnWoa6ZFYlsU-MQSxl8_Fw6bT_DLoLJ0="  # noqa: S105 - fake test key

_AUTHORITY = "https://mock-idp.test"
_GRAPH_URL = "https://mock-idp.test/graph/v1.0"
_TID = "33333333-3333-3333-3333-333333333333"
_ENTRA_CLIENT_ID = "44444444-4444-4444-4444-444444444444"
_ENTRA_CLIENT_SECRET = "mock-entra-client-secret"  # noqa: S105 - fake test credential

_OID = "user-refresh-1"


def _config() -> ServerConfig:
    return ServerConfig(
        public_url=_PUBLIC_URL, mcp_path=_MCP_PATH, oauth_client_secret_key=_CLIENT_SECRET_KEY
    )


@pytest_asyncio.fixture
async def entra_environ(
    admin_database_url: str, test_database_url: str
) -> AsyncIterator[dict[str, str]]:
    """`STORAGE_BACKEND=postgres` plus a disposable `DATABASE_APP_ROLE` - duplicated
    from `tests/auth/test_login_entra.py`'s own fixture of the same name/shape."""
    role = f"mm_test_entra_refresh_{secrets.token_hex(8)}"
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


@asynccontextmanager
async def _running_app(
    environ: dict[str, str], config: ServerConfig, *, authenticator: EntraAuthenticator
) -> AsyncIterator[tuple[Starlette, httpx.AsyncClient]]:
    app = create_app(lambda: open_services(environ), config, authenticator=authenticator)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", follow_redirects=False
        ) as client:
            yield app, client


def _entra_authenticator(mock_idp_client: httpx.AsyncClient) -> EntraAuthenticator:
    return EntraAuthenticator(
        tenant_id=_TID,
        client_id=_ENTRA_CLIENT_ID,
        client_secret=_ENTRA_CLIENT_SECRET,
        redirect_uri=f"{_PUBLIC_URL}{CALLBACK_PATH}",
        authority=_AUTHORITY,
        graph_url=_GRAPH_URL,
        http_client=mock_idp_client,
    )


async def _register_entra_client(mock_idp_client: httpx.AsyncClient) -> None:
    response = await mock_idp_client.post(
        "/_mock/clients",
        json={
            "tid": _TID,
            "client_id": _ENTRA_CLIENT_ID,
            "client_secret": _ENTRA_CLIENT_SECRET,
            "redirect_uris": [f"{_PUBLIC_URL}{CALLBACK_PATH}"],
            "graph_roles": ["User.Read.All", "GroupMember.Read.All"],
        },
    )
    assert response.status_code == 201, response.text


async def _create_entra_user(mock_idp_client: httpx.AsyncClient, oid: str = _OID) -> None:
    response = await mock_idp_client.post(
        "/_mock/users",
        json={
            "tid": _TID,
            "oid": oid,
            "display_name": "Ada Lovelace",
            "roles": ["Memory.User"],
            "groups": ["group-a"],
        },
    )
    assert response.status_code == 201, response.text


async def _set_account_enabled(
    mock_idp_client: httpx.AsyncClient, oid: str, *, enabled: bool
) -> None:
    response = await mock_idp_client.patch(
        f"/_mock/users/{_TID}/{oid}", json={"account_enabled": enabled}
    )
    assert response.status_code == 200, response.text


async def _delete_entra_user(mock_idp_client: httpx.AsyncClient, oid: str) -> None:
    response = await mock_idp_client.delete(f"/_mock/users/{_TID}/{oid}")
    assert response.status_code == 204, response.text


async def _inject_graph_fault(
    mock_idp_client: httpx.AsyncClient, *, endpoint: str, status: int, count: int
) -> None:
    response = await mock_idp_client.post(
        "/_mock/graph/fault",
        json={"endpoint": endpoint, "status": status, "retry_after": 0, "count": count},
    )
    assert response.status_code == 201, response.text


async def _select_signin(mock_idp_client: httpx.AsyncClient, oid: str = _OID) -> None:
    response = await mock_idp_client.post("/_mock/select-signin", json={"tid": _TID, "oid": oid})
    assert response.status_code == 200, response.text


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


async def _register_mcp_client(client: httpx.AsyncClient) -> str:
    response = await client.post(
        "/register",
        json={"redirect_uris": [_REDIRECT_URI], "token_endpoint_auth_method": "none"},
    )
    assert response.status_code == 201, response.text
    client_id = response.json()["client_id"]
    assert isinstance(client_id, str)
    return client_id


def _location(response: httpx.Response) -> str:
    location = response.headers.get("location")
    assert location is not None, f"expected a redirect, got {response.status_code} {response.text}"
    return str(location)


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


async def _confirm_interstitial(
    client: httpx.AsyncClient, interstitial_response: httpx.Response
) -> httpx.Response:
    assert interstitial_response.status_code == 200, interstitial_response.text
    marker = 'class="button" href="'
    start = interstitial_response.text.index(marker) + len(marker)
    end = interstitial_response.text.index('"', start)
    continue_url = html.unescape(interstitial_response.text[start:end])
    return await client.get(continue_url)


async def _login_and_get_tokens(
    client: httpx.AsyncClient, mock_idp_client: httpx.AsyncClient, *, client_id: str
) -> dict[str, object]:
    """Drives a full entra login (#215 shape, duplicated from
    `test_login_entra.py`'s own `_drive_entra_login`) and exchanges the resulting code
    for an access/refresh token pair."""
    _verifier, code_challenge = _pkce_pair()
    authorize_response = await client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": _REDIRECT_URI,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": "xyz",
            "resource": f"{_PUBLIC_URL}{_MCP_PATH}",
        },
    )
    assert authorize_response.status_code == 302, authorize_response.text
    login_url = _location(authorize_response)
    assert urlsplit(login_url).path == LOGIN_PATH
    interstitial_response = await client.get(login_url)
    redirect_response = await _confirm_interstitial(client, interstitial_response)
    assert redirect_response.status_code == 302, redirect_response.text
    mock_authorize_url = _location(redirect_response)

    mock_redirect = await mock_idp_client.get(mock_authorize_url)
    assert mock_redirect.status_code == 302, mock_redirect.text
    callback_params = {key: values[0] for key, values in _query(_location(mock_redirect)).items()}
    callback_response = await client.get(CALLBACK_PATH, params=callback_params)
    assert callback_response.status_code == 302, callback_response.text
    code = _query(_location(callback_response))["code"][0]

    token_response = await client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": _verifier,
        },
    )
    assert token_response.status_code == 200, token_response.text
    result: dict[str, object] = token_response.json()
    return result


async def _refresh(
    client: httpx.AsyncClient, *, refresh_token: str, client_id: str
) -> httpx.Response:
    return await client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        },
    )


async def test_disabled_user_is_denied_a_refresh(
    entra_environ: dict[str, str], pool: asyncpg.Pool, mock_idp_client: httpx.AsyncClient
) -> None:
    config = _config()
    authenticator = _entra_authenticator(mock_idp_client)
    await _register_entra_client(mock_idp_client)
    await _create_entra_user(mock_idp_client, "user-disabled")
    await _select_signin(mock_idp_client, "user-disabled")

    async with _running_app(entra_environ, config, authenticator=authenticator) as (_app, client):
        client_id = await _register_mcp_client(client)
        tokens = await _login_and_get_tokens(client, mock_idp_client, client_id=client_id)

        await _set_account_enabled(mock_idp_client, "user-disabled", enabled=False)

        response = await _refresh(
            client, refresh_token=str(tokens["refresh_token"]), client_id=client_id
        )
        assert response.status_code == 400, response.text
        assert response.json()["error"] == "invalid_grant"

    user = await pool.fetchrow("select disabled_at from users where oid = $1", "user-disabled")
    assert user is not None
    assert user["disabled_at"] is not None


async def test_deleted_user_is_denied_a_refresh(
    entra_environ: dict[str, str], mock_idp_client: httpx.AsyncClient
) -> None:
    config = _config()
    authenticator = _entra_authenticator(mock_idp_client)
    await _register_entra_client(mock_idp_client)
    await _create_entra_user(mock_idp_client, "user-deleted")
    await _select_signin(mock_idp_client, "user-deleted")

    async with _running_app(entra_environ, config, authenticator=authenticator) as (_app, client):
        client_id = await _register_mcp_client(client)
        tokens = await _login_and_get_tokens(client, mock_idp_client, client_id=client_id)

        await _delete_entra_user(mock_idp_client, "user-deleted")

        response = await _refresh(
            client, refresh_token=str(tokens["refresh_token"]), client_id=client_id
        )
        assert response.status_code == 400, response.text
        assert response.json()["error"] == "invalid_grant"


async def test_refresh_past_max_session_is_denied_without_asking_graph(
    entra_environ: dict[str, str], pool: asyncpg.Pool, mock_idp_client: httpx.AsyncClient
) -> None:
    config = _config()
    authenticator = _entra_authenticator(mock_idp_client)
    await _register_entra_client(mock_idp_client)
    await _create_entra_user(mock_idp_client, "user-max-session")
    await _select_signin(mock_idp_client, "user-max-session")

    async with _running_app(entra_environ, config, authenticator=authenticator) as (_app, client):
        client_id = await _register_mcp_client(client)
        tokens = await _login_and_get_tokens(client, mock_idp_client, client_id=client_id)

        access_token = str(tokens["access_token"])
        stored = await store.get_token(pool, access_token, "access")
        assert stored is not None
        await pool.execute(
            "update oauth_tokens set family_started_at = now() - interval '13 hours' "
            "where family_id = $1",
            stored.family_id,
        )

        response = await _refresh(
            client, refresh_token=str(tokens["refresh_token"]), client_id=client_id
        )
        assert response.status_code == 400, response.text
        assert response.json()["error"] == "invalid_grant"

    calls = await mock_idp_client.get("/_mock/calls")
    # The session-length cutoff is checked before ever asking Graph (#216).
    assert calls.json().get("users.get", 0) == 0


async def test_fresh_refresh_keeps_default_entra_access_token_ttl_and_skips_group_refetch(
    entra_environ: dict[str, str], mock_idp_client: httpx.AsyncClient
) -> None:
    config = _config()
    authenticator = _entra_authenticator(mock_idp_client)
    await _register_entra_client(mock_idp_client)
    await _create_entra_user(mock_idp_client, "user-fresh")
    await _select_signin(mock_idp_client, "user-fresh")

    async with _running_app(entra_environ, config, authenticator=authenticator) as (_app, client):
        client_id = await _register_mcp_client(client)
        tokens = await _login_and_get_tokens(client, mock_idp_client, client_id=client_id)
        assert tokens["expires_in"] == 900

        response = await _refresh(
            client, refresh_token=str(tokens["refresh_token"]), client_id=client_id
        )
        assert response.status_code == 200, response.text
        refreshed = response.json()
        assert refreshed["expires_in"] == 900
        assert refreshed["access_token"] != tokens["access_token"]
        assert refreshed["refresh_token"] != tokens["refresh_token"]

    calls = await mock_idp_client.get("/_mock/calls")
    # Groups were never stale (just fetched at login), so a refresh must not re-fetch.
    assert calls.json().get("getMemberGroups", 0) == 0


async def test_refresh_with_stale_groups_refetches_them(
    entra_environ: dict[str, str], pool: asyncpg.Pool, mock_idp_client: httpx.AsyncClient
) -> None:
    config = _config()
    authenticator = _entra_authenticator(mock_idp_client)
    await _register_entra_client(mock_idp_client)
    await _create_entra_user(mock_idp_client, "user-stale-groups")
    await _select_signin(mock_idp_client, "user-stale-groups")

    async with _running_app(entra_environ, config, authenticator=authenticator) as (_app, client):
        client_id = await _register_mcp_client(client)
        tokens = await _login_and_get_tokens(client, mock_idp_client, client_id=client_id)

        await pool.execute(
            "update users set groups_fetched_at = now() - interval '2 hours' where oid = $1",
            "user-stale-groups",
        )

        response = await _refresh(
            client, refresh_token=str(tokens["refresh_token"]), client_id=client_id
        )
        assert response.status_code == 200, response.text

    calls = await mock_idp_client.get("/_mock/calls")
    assert calls.json().get("getMemberGroups", 0) == 1


async def test_graph_unavailable_during_refresh_is_retryable_and_the_token_still_works(
    entra_environ: dict[str, str], mock_idp_client: httpx.AsyncClient
) -> None:
    config = _config()
    authenticator = _entra_authenticator(mock_idp_client)
    await _register_entra_client(mock_idp_client)
    await _create_entra_user(mock_idp_client, "user-graph-down")
    await _select_signin(mock_idp_client, "user-graph-down")

    async with _running_app(entra_environ, config, authenticator=authenticator) as (_app, client):
        client_id = await _register_mcp_client(client)
        tokens = await _login_and_get_tokens(client, mock_idp_client, client_id=client_id)
        refresh_token = str(tokens["refresh_token"])

        # Exhausts auth.graph.GraphClient's own bounded retry budget (3 attempts) in
        # one call, exactly `tests/auth/test_graph.py`'s own "exhausted retries" shape.
        await _inject_graph_fault(mock_idp_client, endpoint="users.get", status=503, count=3)

        failed_response = await _refresh(client, refresh_token=refresh_token, client_id=client_id)
        assert failed_response.status_code == 503, failed_response.text
        body = failed_response.json()
        assert body["error"] == "temporarily_unavailable"

        # Neither rotated nor consumed: the exact same refresh token works once the
        # injected fault queue is empty (ADR-0006 addendum 2026-10-08).
        recovered_response = await _refresh(
            client, refresh_token=refresh_token, client_id=client_id
        )
        assert recovered_response.status_code == 200, recovered_response.text
