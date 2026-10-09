# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the admin "revoke access" action on `/account` (#235): a
`Memory.Admin` ends a target user's OAuth token families, static tokens and
`/account` sessions at once, without disabling the account or touching its
notes.

Admin login is driven exactly the way `tests/account/test_admin.py` drives
Entra login - that module's own docstring explains the `/account/login` ->
`/login` -> `{CALLBACK_PATH}` shape; the helpers below are a trimmed copy
rather than a cross-module import (`tests/account/conftest.py`'s own
docstring: every package under `tests/` that needs a given shape defines its
own).

The *target* user never logs in through the browser here - its OAuth token
family, static token and `/account` session are minted directly against a
second pool connected to the same `test_database_url`, the same shape
`tests/auth/test_disable_user.py` already uses to set up credentials for
`auth.users.disable_user`/`revoke_all_credentials` without a real Entra
login for the *target*. This action calls that same `revoke_all_credentials`
(`account.admin`'s own module docstring explains why, not `disable_user`:
the target stays enabled and can sign in again right away) plus
`account.sessions.revoke_all_for_oid` for the session half.
"""

from __future__ import annotations

import html
import json
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import asyncpg
import httpx
from mcp.shared.auth import OAuthClientInformationFull
from mock_idp_fixtures import mock_idp_client
from pydantic import AnyUrl
from starlette.applications import Starlette

from memory_manager.account.admin import REVOKE_USER_PATH
from memory_manager.account.routes import LOGIN_PATH_START, PATH
from memory_manager.account.sessions import create as create_account_session
from memory_manager.account.sessions import lookup as lookup_account_session
from memory_manager.account.templates import CSRF_FIELD_NAME
from memory_manager.app import open_services
from memory_manager.auth import store, tokens, users
from memory_manager.auth.login import LoginPrincipal
from memory_manager.auth.login_entra import CALLBACK_PATH as ENTRA_CALLBACK_PATH
from memory_manager.auth.login_entra import EntraAuthenticator
from memory_manager.auth.provider import MemoryManagerOAuthProvider
from memory_manager.auth.verifier import verify_bearer_token
from memory_manager.config import ServerConfig
from memory_manager.http import create_app

__all__ = ["mock_idp_client"]

_PUBLIC_URL = "https://mm.example.test"
_CLIENT_SECRET_KEY = "f2F7cFcySJJZnWoa6ZFYlsU-MQSxl8_Fw6bT_DLoLJ0="  # noqa: S105 - fake test key

_TID = "33333333-3333-3333-3333-333333333333"
_ENTRA_CLIENT_ID = "44444444-4444-4444-4444-444444444444"
_ENTRA_CLIENT_SECRET = "mock-entra-client-secret"  # noqa: S105 - a fake test credential
_ENTRA_AUTHORITY = "https://mock-idp.test"
_ENTRA_GRAPH_URL = "https://mock-idp.test/graph/v1.0"

_MEMORY_USER = "Memory.User"
_MEMORY_ADMIN = "Memory.Admin"

#: Resource/issuer/key for the standalone `MemoryManagerOAuthProvider` this module
#: uses to mint the *target* user's own token family - never reachable through the
#: running app's own routes, so it does not need to match `_config()`'s own
#: `oauth_client_secret_key` (same reasoning `tests/auth/test_disable_user.py`'s own
#: module docstring gives for its own, independent Fernet key).
_OAUTH_RESOURCE = "https://mm.example.test/mcp"
_OAUTH_ISSUER = "https://mm.example.test"
_OAUTH_CLIENT_SECRET_KEY = "r8rGp30uQA9cx9egMfZk4ez3xkfaFzL0tCst-kNzrcI="  # noqa: S105
_OAUTH_REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"
_OAUTH_CLIENT_ID = "test-client"
_OAUTH_NAMESPACES = ["personal"]


def _config() -> ServerConfig:
    return ServerConfig(public_url=_PUBLIC_URL, oauth_client_secret_key=_CLIENT_SECRET_KEY)


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


def _location(response: httpx.Response) -> str:
    location = response.headers.get("location")
    assert location is not None, f"expected a redirect, got {response.status_code} {response.text}"
    return str(location)


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


def _continue_url_from_interstitial(response: httpx.Response) -> str:
    marker = 'class="button" href="'
    start = response.text.index(marker) + len(marker)
    end = response.text.index('"', start)
    return html.unescape(response.text[start:end])


async def _start_account_login(client: httpx.AsyncClient) -> str:
    start_response = await client.get(LOGIN_PATH_START)
    assert start_response.status_code == 302, start_response.text
    return _location(start_response)


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


async def _register_entra_identity(
    mock_idp_client: httpx.AsyncClient, oid: str, *, roles: list[str]
) -> None:
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
        "/_mock/users", json={"tid": _TID, "oid": oid, "roles": roles}
    )
    assert response.status_code == 201, response.text
    response = await mock_idp_client.post("/_mock/select-signin", json={"tid": _TID, "oid": oid})
    assert response.status_code == 200, response.text


async def _login_as(
    client: httpx.AsyncClient,
    mock_idp_client: httpx.AsyncClient,
    *,
    oid: str,
    roles: list[str],
) -> None:
    await _register_entra_identity(mock_idp_client, oid, roles=roles)
    response = await _login_with_entra(client, mock_idp_client)
    assert response.status_code == 302, response.text
    assert _location(response) == PATH


def _revoke_csrf_token(page_html: str) -> str:
    form_marker = f'action="{html.escape(REVOKE_USER_PATH)}"'
    form_start = page_html.index(form_marker)
    marker = f'name="{CSRF_FIELD_NAME}" value="'
    start = page_html.index(marker, form_start) + len(marker)
    end = page_html.index('"', start)
    return html.unescape(page_html[start:end])


def _oauth_provider(pool: asyncpg.Pool) -> MemoryManagerOAuthProvider:
    return MemoryManagerOAuthProvider(
        pool,
        resource=_OAUTH_RESOURCE,
        issuer=_OAUTH_ISSUER,
        client_secret_key=_OAUTH_CLIENT_SECRET_KEY,
    )


def _oauth_client() -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=_OAUTH_CLIENT_ID,
        redirect_uris=[AnyUrl(_OAUTH_REDIRECT_URI)],
        token_endpoint_auth_method="none",  # noqa: S106 - an auth method, not a credential
    )


async def _park_pending(pool: asyncpg.Pool) -> str:
    pending_id = secrets.token_urlsafe(16)
    await store.save_pending(
        pool,
        pending_id,
        client_id=_OAUTH_CLIENT_ID,
        redirect_uri=_OAUTH_REDIRECT_URI,
        redirect_uri_provided_explicitly=True,
        code_challenge="challenge",
        state=None,
        resource=_OAUTH_RESOURCE,
        scopes=["memory:read", "memory:write"],
        ttl=timedelta(minutes=10),
    )
    return pending_id


def _code_from_redirect(redirect_url: str) -> str:
    query = parse_qs(urlsplit(redirect_url).query)
    return query["code"][0]


async def _issue_target_tokens(pool: asyncpg.Pool, *, target_oid: str) -> tuple[str, str]:
    """Issue one OAuth token family (`access_token`, `refresh_token`) for
    `target_oid`, directly against `pool` - the same shape `tests/auth/
    test_disable_user.py`'s own `_issue_tokens` already uses, trimmed to the one
    family this module's tests need."""
    provider = _oauth_provider(pool)
    principal = LoginPrincipal(oid=target_oid, roles=(_MEMORY_USER,))
    await provider.register_client(_oauth_client())
    pending_id = await _park_pending(pool)
    redirect_url = await provider.complete_authorization(
        pending_id, f"{target_oid}-session", _OAUTH_NAMESPACES, principal
    )
    assert redirect_url is not None
    code = _code_from_redirect(redirect_url)

    client = _oauth_client()
    loaded_code = await provider.load_authorization_code(client, code)
    assert loaded_code is not None
    oauth_token = await provider.exchange_authorization_code(client, loaded_code)
    assert oauth_token.refresh_token is not None
    return oauth_token.access_token, oauth_token.refresh_token


async def _create_target_static_token(pool: asyncpg.Pool, *, target_oid: str) -> str:
    plaintext, _info = await tokens.create_token(
        pool,
        "target-static",
        scopes=["memory:read", "memory:write"],
        namespaces=["*"],
        owner_oid=target_oid,
        roles=[_MEMORY_USER],
    )
    return plaintext


async def _seed_note(pool: asyncpg.Pool, *, namespace: str, slug: str) -> str:
    """A minimal `vault_notes` row, direct against the owner pool (bypasses RLS,
    same assumption `tests/account/test_admin.py`'s own `_seed_vault_note` and
    `tests/account/test_delete.py`'s own `_seed_note` make) - enough to prove the
    revoke action never touches note content."""
    note_id = f"note-{namespace}-{slug}"
    content = f"---\ntitle: {slug}\ndescription: {slug} description.\ntype: fact\n---\nBody.\n"
    await pool.execute(
        "insert into vault_notes (id, namespace, path, content, version, current_revision) "
        "values ($1, $2, $3, $4, 'v1', 1)",
        note_id,
        namespace,
        f"{namespace}/fact/{slug}.md",
        content.encode(),
    )
    return note_id


async def test_revoke_access_ends_tokens_and_session_but_leaves_notes_and_the_account(
    admin_database_url: str,
    test_database_url: str,
    postgres_app_role_environ: dict[str, str],
    mock_idp_client: httpx.AsyncClient,
) -> None:
    admin_oid = "oid-admin-revoke"
    target_oid = "oid-target-revoke"
    target_namespace = "u-target-revoke"
    reason = "admin: Entra role removed, Graph sync pending"

    side_pool = await asyncpg.create_pool(test_database_url)
    try:
        authenticator = _entra_authenticator(mock_idp_client)
        config = _config()
        async with _running_app(postgres_app_role_environ, config, authenticator=authenticator) as (
            _app,
            client,
        ):
            await _login_as(client, mock_idp_client, oid=admin_oid, roles=[_MEMORY_ADMIN])

            await users.upsert_user(side_pool, target_oid, tid="tenant-1", display_name="Target")
            access_token, refresh_token = await _issue_target_tokens(
                side_pool, target_oid=target_oid
            )
            static_token = await _create_target_static_token(side_pool, target_oid=target_oid)
            session_id = await create_account_session(
                side_pool,
                subject="target@example.test",
                login_mode="entra",
                oid=target_oid,
                roles=[_MEMORY_USER],
            )
            note_id = await _seed_note(side_pool, namespace=target_namespace, slug="untouched")
            note_content_before = await side_pool.fetchval(
                "select content from vault_notes where id = $1", note_id
            )

            page_response = await client.get(PATH)
            assert "<h2>Admin</h2>" in page_response.text
            token = _revoke_csrf_token(page_response.text)

            response = await client.post(
                REVOKE_USER_PATH,
                data={CSRF_FIELD_NAME: token, "oid": target_oid, "reason": reason},
            )
            assert response.status_code == 302, response.text
            assert _location(response) == PATH

        assert (
            await verify_bearer_token(side_pool, access_token, oauth_resource=_OAUTH_RESOURCE)
            is None
        )
        refresh_stored = await store.get_token(
            side_pool, refresh_token, "refresh", include_revoked=True
        )
        assert refresh_stored is not None
        assert refresh_stored.revoked_at is not None
        assert await tokens.verify(side_pool, static_token) is None
        assert await lookup_account_session(side_pool, session_id) is None

        target_user = await users.get_user(side_pool, target_oid)
        assert target_user is not None
        assert target_user.disabled_at is None

        note_content_after = await side_pool.fetchval(
            "select content from vault_notes where id = $1", note_id
        )
        assert note_content_after == note_content_before

        audit_row = await side_pool.fetchrow(
            "select actor, op, outcome, detail from audit_log where actor = $1 and op = $2",
            admin_oid,
            "admin.user.revoke",
        )
        assert audit_row is not None
        assert audit_row["outcome"] == "ok"
        detail = json.loads(audit_row["detail"])
        assert detail["target_oid"] == target_oid
        assert detail["reason"] == reason
        assert detail["oauth_tokens_revoked"] == 2
        assert detail["static_tokens_revoked"] == 1
        assert detail["account_sessions_revoked"] == 1
        assert "untouched" not in audit_row["detail"]
    finally:
        await side_pool.close()


async def test_revoke_access_rejects_a_non_admin_session_with_403(
    admin_database_url: str,
    test_database_url: str,
    postgres_app_role_environ: dict[str, str],
    mock_idp_client: httpx.AsyncClient,
) -> None:
    authenticator = _entra_authenticator(mock_idp_client)
    config = _config()
    async with _running_app(postgres_app_role_environ, config, authenticator=authenticator) as (
        _app,
        client,
    ):
        await _login_as(client, mock_idp_client, oid="oid-not-admin-revoke", roles=[_MEMORY_USER])

        response = await client.post(
            REVOKE_USER_PATH, data={"oid": "oid-irrelevant", "reason": "irrelevant"}
        )
        assert response.status_code == 403, response.text


async def test_revoke_access_without_a_reason_is_rejected(
    admin_database_url: str,
    test_database_url: str,
    postgres_app_role_environ: dict[str, str],
    mock_idp_client: httpx.AsyncClient,
) -> None:
    target_oid = "oid-target-no-reason"

    side_pool = await asyncpg.create_pool(test_database_url)
    try:
        authenticator = _entra_authenticator(mock_idp_client)
        config = _config()
        async with _running_app(postgres_app_role_environ, config, authenticator=authenticator) as (
            _app,
            client,
        ):
            await _login_as(
                client, mock_idp_client, oid="oid-admin-no-reason", roles=[_MEMORY_ADMIN]
            )
            await users.upsert_user(side_pool, target_oid, tid="tenant-1", display_name="Target")
            static_token = await _create_target_static_token(side_pool, target_oid=target_oid)

            page_response = await client.get(PATH)
            token = _revoke_csrf_token(page_response.text)

            response = await client.post(
                REVOKE_USER_PATH,
                data={CSRF_FIELD_NAME: token, "oid": target_oid, "reason": ""},
            )
            assert response.status_code == 400, response.text

        assert await tokens.verify(side_pool, static_token) is not None
    finally:
        await side_pool.close()
