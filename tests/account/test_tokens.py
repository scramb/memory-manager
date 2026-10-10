# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for `/account`'s personal-token section (ADR-0012, #135).

Login is driven exactly the way `tests/account/test_page.py`/`test_export.py` drive
it - see those modules' own docstrings for why the helpers below are a trimmed copy
rather than a cross-module import (`tests/account/conftest.py`'s own docstring: every
package under `tests/` that needs a given shape defines its own).

`_authed_mcp_client` is `tests/auth/test_oauth_flow.py`'s own helper for driving a
real `mcp.Client` session against an issued bearer token - used here to prove a
created personal token actually works on `/mcp`, and stops working once revoked.
"""

from __future__ import annotations

import html
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import asyncpg
import httpx
import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mock_idp_fixtures import mock_idp_client
from starlette.applications import Starlette

from memory_manager.account.routes import LOGIN_PATH_START, PATH, SESSION_COOKIE
from memory_manager.account.sessions import csrf_token
from memory_manager.account.templates import CSRF_FIELD_NAME
from memory_manager.account.tokens import CREATE_PATH, CSRF_FORM_CREATE, CSRF_FORM_REVOKE
from memory_manager.account.tokens import REVOKE_PATH as TOKEN_REVOKE_PATH
from memory_manager.app import open_services
from memory_manager.auth.login import LOGIN_PATH
from memory_manager.auth.login_entra import CALLBACK_PATH as ENTRA_CALLBACK_PATH
from memory_manager.auth.login_entra import EntraAuthenticator
from memory_manager.auth.login_password import PasswordAuthenticator, hash_password
from memory_manager.auth.tokens import KIND_PERSONAL, create_token
from memory_manager.config import ServerConfig
from memory_manager.http import create_app

__all__ = ["mock_idp_client"]

_PUBLIC_URL = "https://mm.example.test"
_ADMIN_PASSWORD = "correct horse battery staple"  # noqa: S105 - a fake test credential
_CLIENT_SECRET_KEY = "f2F7cFcySJJZnWoa6ZFYlsU-MQSxl8_Fw6bT_DLoLJ0="  # noqa: S105 - fake test key

_TID = "33333333-3333-3333-3333-333333333333"
_ENTRA_CLIENT_ID = "44444444-4444-4444-4444-444444444444"
_ENTRA_CLIENT_SECRET = "mock-entra-client-secret"  # noqa: S105 - a fake test credential
_ENTRA_AUTHORITY = "https://mock-idp.test"
_ENTRA_GRAPH_URL = "https://mock-idp.test/graph/v1.0"


def _config() -> ServerConfig:
    return ServerConfig(public_url=_PUBLIC_URL, oauth_client_secret_key=_CLIENT_SECRET_KEY)


def _git_environ(bare_remote: Path, tmp_path: Path, test_database_url: str) -> dict[str, str]:
    return {
        "VAULT_REMOTE": str(bare_remote),
        "VAULT_DIR": str(tmp_path / "vault"),
        "DATABASE_URL": test_database_url,
    }


@asynccontextmanager
async def _running_app(
    environ: dict[str, str], config: ServerConfig, *, authenticator: Any
) -> AsyncIterator[tuple[Starlette, httpx.AsyncClient]]:
    app = create_app(lambda: open_services(environ), config, authenticator=authenticator)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="https://testserver", follow_redirects=False
        ) as client:
            yield app, client


def _authed_mcp_client(app: Starlette, config: ServerConfig, token: str) -> Client:
    http_client = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url="https://testserver",
        headers={"Authorization": f"Bearer {token}"},
    )
    transport = streamable_http_client(
        f"https://testserver{config.mcp_path}", http_client=http_client
    )
    return Client(transport, mode="legacy")


def _location(response: httpx.Response) -> str:
    location = response.headers.get("location")
    assert location is not None, f"expected a redirect, got {response.status_code} {response.text}"
    return str(location)


def _continue_url_from_interstitial(response: httpx.Response) -> str:
    marker = 'class="button" href="'
    start = response.text.index(marker) + len(marker)
    end = response.text.index('"', start)
    return html.unescape(response.text[start:end])


def _pending_id_from_password_page(response: httpx.Response) -> str:
    marker = 'name="pending" value="'
    start = response.text.index(marker) + len(marker)
    end = response.text.index('"', start)
    return response.text[start:end]


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


async def _start_account_login(client: httpx.AsyncClient) -> str:
    start_response = await client.get(LOGIN_PATH_START)
    assert start_response.status_code == 302, start_response.text
    return _location(start_response)


async def _login_with_password(client: httpx.AsyncClient) -> httpx.Response:
    login_url = await _start_account_login(client)
    login_response = await client.get(login_url)
    assert login_response.status_code == 200, login_response.text
    pending_id = _pending_id_from_password_page(login_response)
    return await client.post(LOGIN_PATH, data={"pending": pending_id, "password": _ADMIN_PASSWORD})


async def _login_with_entra(
    client: httpx.AsyncClient, mock_idp_client: httpx.AsyncClient
) -> httpx.Response:
    login_url = await _start_account_login(client)
    interstitial_response = await client.get(login_url)
    assert interstitial_response.status_code == 200, interstitial_response.text
    continue_response = await client.get(_continue_url_from_interstitial(interstitial_response))
    assert continue_response.status_code == 302, continue_response.text
    mock_authorize_url = _location(continue_response)

    mock_redirect = await mock_idp_client.get(mock_authorize_url)
    assert mock_redirect.status_code == 302, mock_redirect.text
    callback_params = {key: values[0] for key, values in _query(_location(mock_redirect)).items()}
    return await client.get(ENTRA_CALLBACK_PATH, params=callback_params)


def _entra_authenticator(mock_idp_client: httpx.AsyncClient) -> EntraAuthenticator:
    return EntraAuthenticator(
        tenant_id=_TID,
        client_id=_ENTRA_CLIENT_ID,
        client_secret=_ENTRA_CLIENT_SECRET,
        redirect_uri=f"{_PUBLIC_URL}{ENTRA_CALLBACK_PATH}",
        authority=_ENTRA_AUTHORITY,
        graph_url=_ENTRA_GRAPH_URL,
        http_client=mock_idp_client,
    )


async def _register_entra_identity(mock_idp_client: httpx.AsyncClient, oid: str) -> None:
    response = await mock_idp_client.post(
        "/_mock/clients",
        json={
            "tid": _TID,
            "client_id": _ENTRA_CLIENT_ID,
            "client_secret": _ENTRA_CLIENT_SECRET,
            "redirect_uris": [f"{_PUBLIC_URL}{ENTRA_CALLBACK_PATH}"],
            "graph_roles": [],
        },
    )
    assert response.status_code == 201, response.text
    response = await mock_idp_client.post(
        "/_mock/users", json={"tid": _TID, "oid": oid, "roles": ["Memory.User"]}
    )
    assert response.status_code == 201, response.text
    response = await mock_idp_client.post("/_mock/select-signin", json={"tid": _TID, "oid": oid})
    assert response.status_code == 200, response.text


def _extract_plaintext(page_html: str) -> str:
    marker = 'class="token-value" value="'
    start = page_html.index(marker) + len(marker)
    end = page_html.index('"', start)
    return html.unescape(page_html[start:end])


def _extract_name(page_html: str) -> str:
    marker = "Name: <code>"
    start = page_html.index(marker) + len(marker)
    end = page_html.index("</code>", start)
    return html.unescape(page_html[start:end])


async def _create_personal_token(
    client: httpx.AsyncClient,
    *,
    namespaces: str,
    expires_days: str = "10",
    scopes: tuple[str, ...] = ("memory:read", "memory:write"),
    description: str = "a test token",
) -> httpx.Response:
    session_id = client.cookies.get(SESSION_COOKIE)
    assert session_id is not None
    data: dict[str, Any] = {
        CSRF_FIELD_NAME: csrf_token(session_id, CSRF_FORM_CREATE),
        "namespaces": namespaces,
        "expires_days": expires_days,
        "description": description,
        "scopes": list(scopes),
    }
    return await client.post(CREATE_PATH, data=data)


async def _revoke_personal_token(client: httpx.AsyncClient, *, name: str) -> httpx.Response:
    session_id = client.cookies.get(SESSION_COOKIE)
    assert session_id is not None
    return await client.post(
        TOKEN_REVOKE_PATH,
        data={CSRF_FIELD_NAME: csrf_token(session_id, CSRF_FORM_REVOKE), "name": name},
    )


# === "git" backend, password login ==================================================


async def test_git_password_create_lists_only_the_owners_allowed_namespaces(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    config = _config()
    environ = _git_environ(bare_remote, tmp_path, test_database_url)
    environ["LOGIN_NAMESPACE_MAP"] = json.dumps({"admin": ["team-a", "team-b"]})
    authenticator = PasswordAuthenticator(
        password_hash=hash_password(_ADMIN_PASSWORD), namespaces=["*"]
    )
    async with _running_app(environ, config, authenticator=authenticator) as (_app, client):
        await _login_with_password(client)

        page_response = await client.get(PATH)
        assert page_response.status_code == 200, page_response.text
        assert "Personal tokens" in page_response.text
        assert "team-a, team-b" in page_response.text
        assert 'value="memory:read"' in page_response.text
        assert 'value="memory:write"' in page_response.text


async def test_git_password_create_shows_plaintext_once_then_never_again(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    config = _config()
    environ = _git_environ(bare_remote, tmp_path, test_database_url)
    authenticator = PasswordAuthenticator(
        password_hash=hash_password(_ADMIN_PASSWORD), namespaces=["*"]
    )
    async with _running_app(environ, config, authenticator=authenticator) as (_app, client):
        await _login_with_password(client)

        create_response = await _create_personal_token(client, namespaces="*")
        assert create_response.status_code == 200, create_response.text
        plaintext = _extract_plaintext(create_response.text)
        assert plaintext.startswith("mm_")
        assert create_response.headers["cache-control"] == "no-store"

        page_response = await client.get(PATH)
        assert page_response.status_code == 200, page_response.text
        assert plaintext not in page_response.text


async def test_git_password_created_token_works_on_mcp_and_not_after_revoke(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    config = _config()
    environ = _git_environ(bare_remote, tmp_path, test_database_url)
    authenticator = PasswordAuthenticator(
        password_hash=hash_password(_ADMIN_PASSWORD), namespaces=["*"]
    )
    async with _running_app(environ, config, authenticator=authenticator) as (app, client):
        await _login_with_password(client)

        create_response = await _create_personal_token(client, namespaces="*")
        assert create_response.status_code == 200, create_response.text
        plaintext = _extract_plaintext(create_response.text)
        name = _extract_name(create_response.text)

        async with _authed_mcp_client(app, config, plaintext) as mcp_client:
            index_result = await mcp_client.call_tool("memory_index", {})
            assert index_result.is_error is False

        revoke_response = await _revoke_personal_token(client, name=name)
        assert revoke_response.status_code == 302, revoke_response.text

        response = await client.post(
            config.mcp_path,
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {plaintext}",
            },
        )
        assert response.status_code == 401, response.text


async def test_git_password_namespace_outside_allowed_is_400(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    config = _config()
    environ = _git_environ(bare_remote, tmp_path, test_database_url)
    environ["LOGIN_NAMESPACE_MAP"] = json.dumps({"admin": ["team-a"]})
    authenticator = PasswordAuthenticator(
        password_hash=hash_password(_ADMIN_PASSWORD), namespaces=["*"]
    )
    async with _running_app(environ, config, authenticator=authenticator) as (_app, client):
        await _login_with_password(client)

        response = await _create_personal_token(client, namespaces="team-z")
        assert response.status_code == 400, response.text


@pytest.mark.parametrize("expires_days", ["0", "91", "not-a-number", ""])
async def test_git_password_bad_expiry_is_400(
    bare_remote: Path, tmp_path: Path, test_database_url: str, expires_days: str
) -> None:
    config = _config()
    environ = _git_environ(bare_remote, tmp_path, test_database_url)
    authenticator = PasswordAuthenticator(
        password_hash=hash_password(_ADMIN_PASSWORD), namespaces=["*"]
    )
    async with _running_app(environ, config, authenticator=authenticator) as (_app, client):
        await _login_with_password(client)

        response = await _create_personal_token(client, namespaces="*", expires_days=expires_days)
        assert response.status_code == 400, response.text


async def test_git_password_create_and_revoke_without_csrf_is_403(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    config = _config()
    environ = _git_environ(bare_remote, tmp_path, test_database_url)
    authenticator = PasswordAuthenticator(
        password_hash=hash_password(_ADMIN_PASSWORD), namespaces=["*"]
    )
    async with _running_app(environ, config, authenticator=authenticator) as (_app, client):
        await _login_with_password(client)

        create_response = await client.post(
            CREATE_PATH,
            data={
                "namespaces": "*",
                "expires_days": "10",
                "scopes": "memory:read",
            },
        )
        assert create_response.status_code == 403, create_response.text

        revoke_response = await client.post(TOKEN_REVOKE_PATH, data={"name": "whatever"})
        assert revoke_response.status_code == 403, revoke_response.text


async def test_git_password_create_and_revoke_with_wrong_csrf_is_403(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    config = _config()
    environ = _git_environ(bare_remote, tmp_path, test_database_url)
    authenticator = PasswordAuthenticator(
        password_hash=hash_password(_ADMIN_PASSWORD), namespaces=["*"]
    )
    async with _running_app(environ, config, authenticator=authenticator) as (_app, client):
        await _login_with_password(client)

        create_response = await client.post(
            CREATE_PATH,
            data={
                CSRF_FIELD_NAME: "not-the-right-token",
                "namespaces": "*",
                "expires_days": "10",
                "scopes": "memory:read",
            },
        )
        assert create_response.status_code == 403, create_response.text

        revoke_response = await client.post(
            TOKEN_REVOKE_PATH,
            data={CSRF_FIELD_NAME: "not-the-right-token", "name": "whatever"},
        )
        assert revoke_response.status_code == 403, revoke_response.text


async def test_git_password_create_writes_an_audit_row_with_the_owner_as_actor(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    config = _config()
    environ = _git_environ(bare_remote, tmp_path, test_database_url)
    authenticator = PasswordAuthenticator(
        password_hash=hash_password(_ADMIN_PASSWORD), namespaces=["*"]
    )
    async with _running_app(environ, config, authenticator=authenticator) as (_app, client):
        await _login_with_password(client)

        create_response = await _create_personal_token(client, namespaces="*")
        assert create_response.status_code == 200, create_response.text
        name = _extract_name(create_response.text)

        revoke_response = await _revoke_personal_token(client, name=name)
        assert revoke_response.status_code == 302, revoke_response.text

    conn = await asyncpg.connect(test_database_url)
    try:
        created_row = await conn.fetchrow(
            "select created_by, owner_oid, kind from static_tokens where name = $1", name
        )
        assert created_row is not None
        assert created_row["created_by"] == "admin"
        assert created_row["owner_oid"] == "admin"
        assert created_row["kind"] == KIND_PERSONAL

        create_audit = await conn.fetchrow(
            "select actor, detail from audit_log where op = 'token_create' "
            "and detail->>'name' = $1",
            name,
        )
        assert create_audit is not None
        assert create_audit["actor"] == "admin"

        revoke_audit = await conn.fetchrow(
            "select actor, detail from audit_log where op = 'token_revoke' "
            "and detail->>'name' = $1",
            name,
        )
        assert revoke_audit is not None
        assert revoke_audit["actor"] == "admin"
    finally:
        await conn.close()


# === "postgres" backend, Entra login =================================================


async def test_postgres_entra_create_lists_only_all_namespaces(
    admin_database_url: str,
    test_database_url: str,
    postgres_app_role_environ: dict[str, str],
    mock_idp_client: httpx.AsyncClient,
) -> None:
    oid = "oid-tokens-postgres"
    await _register_entra_identity(mock_idp_client, oid)
    authenticator = _entra_authenticator(mock_idp_client)
    config = _config()
    async with _running_app(postgres_app_role_environ, config, authenticator=authenticator) as (
        _app,
        client,
    ):
        await _login_with_entra(client, mock_idp_client)

        page_response = await client.get(PATH)
        assert page_response.status_code == 200, page_response.text
        assert "Personal tokens" in page_response.text
        assert 'value="*" readonly' in page_response.text


async def test_postgres_entra_created_token_works_on_mcp_and_not_after_revoke(
    admin_database_url: str,
    test_database_url: str,
    postgres_app_role_environ: dict[str, str],
    mock_idp_client: httpx.AsyncClient,
) -> None:
    oid = "oid-tokens-postgres-mcp"
    await _register_entra_identity(mock_idp_client, oid)
    authenticator = _entra_authenticator(mock_idp_client)
    config = _config()
    async with _running_app(postgres_app_role_environ, config, authenticator=authenticator) as (
        app,
        client,
    ):
        await _login_with_entra(client, mock_idp_client)

        create_response = await _create_personal_token(client, namespaces="*")
        assert create_response.status_code == 200, create_response.text
        plaintext = _extract_plaintext(create_response.text)
        name = _extract_name(create_response.text)

        async with _authed_mcp_client(app, config, plaintext) as mcp_client:
            index_result = await mcp_client.call_tool("memory_index", {})
            assert index_result.is_error is False

        revoke_response = await _revoke_personal_token(client, name=name)
        assert revoke_response.status_code == 302, revoke_response.text

        response = await client.post(
            config.mcp_path,
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {plaintext}",
            },
        )
        assert response.status_code == 401, response.text


async def test_postgres_entra_cannot_see_or_revoke_another_users_token(
    admin_database_url: str,
    test_database_url: str,
    postgres_app_role_environ: dict[str, str],
    mock_idp_client: httpx.AsyncClient,
) -> None:
    oid_a = "oid-tokens-user-a"
    oid_b = "oid-tokens-user-b"
    await _register_entra_identity(mock_idp_client, oid_a)
    authenticator = _entra_authenticator(mock_idp_client)
    config = _config()
    async with _running_app(postgres_app_role_environ, config, authenticator=authenticator) as (
        _app,
        client,
    ):
        pool = await asyncpg.create_pool(test_database_url)
        try:
            _plaintext, info = await create_token(
                pool,
                "other-users-token",
                scopes=["memory:read"],
                namespaces=["*"],
                owner_oid=oid_b,
                roles=["Memory.User"],
                kind=KIND_PERSONAL,
                created_by=oid_b,
                description="belongs to user B",
                personal_max_days=90,
                expires_at=datetime.now(UTC) + timedelta(days=10),
            )
        finally:
            await pool.close()

        await _login_with_entra(client, mock_idp_client)

        page_response = await client.get(PATH)
        assert page_response.status_code == 200, page_response.text
        assert "other-users-token" not in page_response.text
        assert "belongs to user B" not in page_response.text

        revoke_response = await _revoke_personal_token(client, name=info.name)
        assert revoke_response.status_code == 404, revoke_response.text

    conn = await asyncpg.connect(test_database_url)
    try:
        row = await conn.fetchrow("select revoked_at from static_tokens where name = $1", info.name)
    finally:
        await conn.close()
    assert row is not None
    assert row["revoked_at"] is None


async def test_postgres_session_without_oid_hides_the_section_and_rejects_posts(
    admin_database_url: str,
    test_database_url: str,
    postgres_app_role_environ: dict[str, str],
) -> None:
    config = _config()
    authenticator = PasswordAuthenticator(
        password_hash=hash_password(_ADMIN_PASSWORD), namespaces=["*"]
    )
    async with _running_app(postgres_app_role_environ, config, authenticator=authenticator) as (
        _app,
        client,
    ):
        await _login_with_password(client)

        page_response = await client.get(PATH)
        assert page_response.status_code == 200, page_response.text
        assert "Personal tokens" not in page_response.text

        create_response = await client.post(CREATE_PATH, data={})
        assert create_response.status_code == 403, create_response.text

        revoke_response = await client.post(TOKEN_REVOKE_PATH, data={})
        assert revoke_response.status_code == 403, revoke_response.text
