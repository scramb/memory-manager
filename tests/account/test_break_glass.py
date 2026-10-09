# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for break-glass read grants on `/account`'s admin area (#237,
ADR-0008 "Break-glass" + its addendum 2026-10-08).

Login is driven exactly the way `tests/account/test_admin.py` drives Entra
login - that module's own docstring explains the shape; the helpers below
are a trimmed copy rather than a cross-module import (`tests/account/
conftest.py`'s own docstring: every package under `tests/` that needs a
given shape defines its own).

The window `mm_readable_ns()` honours a grant in (approved, not yet
expired, not revoked - ADR-0008: "the grant expires after 1 h", counted
from approval) is exercised directly against `db.rls.request_identity`/
`mm_readable_ns()` on the owner connection, the same technique `tests/db/
test_rls.py`'s own `rls_db` fixture already uses for the same function -
there is no need to wait a real hour: the expiry test manipulates
`expires_at` directly through the owner connection instead, the same
"seed/mutate rows directly, bypassing RLS" assumption `tests/account/
test_admin.py`'s own `_seed_vault_note`/`_insert_user_group` helpers
already make.
"""

from __future__ import annotations

import html
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import parse_qs, urlsplit

import asyncpg
import httpx
from mock_idp_fixtures import mock_idp_client
from starlette.applications import Starlette

from memory_manager.account.break_glass import (
    APPROVE_PATH,
    DENY_PATH,
    REQUEST_PATH,
    REVOKE_PATH,
)
from memory_manager.account.routes import LOGIN_PATH_START, PATH
from memory_manager.account.templates import CSRF_FIELD_NAME
from memory_manager.app import open_services
from memory_manager.auth.login_entra import CALLBACK_PATH as ENTRA_CALLBACK_PATH
from memory_manager.auth.login_entra import EntraAuthenticator
from memory_manager.config import ServerConfig
from memory_manager.db import rls
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


def _csrf_token_for(page_html: str, *, action: str, form_name: str) -> str:
    """The per-form CSRF token for the form whose `action` is `action` - same
    extraction shape `tests/account/test_admin.py`'s own `_csrf_token_for` uses.
    `form_name` is unused beyond documenting *which* form a caller means."""
    _ = form_name
    form_marker = f'action="{html.escape(action)}"'
    form_start = page_html.index(form_marker)
    marker = f'name="{CSRF_FIELD_NAME}" value="'
    start = page_html.index(marker, form_start) + len(marker)
    end = page_html.index('"', start)
    return html.unescape(page_html[start:end])


async def _seed_personal_namespace(database_url: str, *, oid: str, alias: str) -> None:
    """A minimal `users`/`namespaces` row for `oid` - enough for
    `mm_break_glass_request` to resolve a target, without that user ever
    having signed in or written a note (`mm_ensure_personal_ns()` only ever
    creates a namespace for the *calling* identity, never an admin's
    target - this module's own docstring)."""
    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute(
            "insert into users (oid, tid, display_name) values ($1, 'tid', 'tester') "
            "on conflict (oid) do nothing",
            oid,
        )
        await conn.execute(
            "insert into namespaces (kind, external_key, alias) values ('user', $1, $2)",
            oid,
            alias,
        )
    finally:
        await conn.close()


async def _latest_grant_id(database_url: str, *, requester: str) -> int:
    conn = await asyncpg.connect(database_url)
    try:
        grant_id = await conn.fetchval(
            "select id from break_glass_grants where requester = $1 "
            "order by requested_at desc limit 1",
            requester,
        )
    finally:
        await conn.close()
    assert grant_id is not None
    return int(grant_id)


async def _expire_grant(database_url: str, *, grant_id: int) -> None:
    """Simulates "1 hour after approval has passed" without waiting for it -
    this module's own docstring."""
    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute(
            "update break_glass_grants set expires_at = now() - interval '1 second' where id = $1",
            grant_id,
        )
    finally:
        await conn.close()


async def _readable_namespaces(
    database_url: str, *, app_role: str, oid: str, roles: list[str], break_glass: int | None
) -> list[str]:
    conn = await asyncpg.connect(database_url)
    try:
        async with rls.request_identity(
            conn, role=app_role, oid=oid, roles=roles, break_glass=break_glass
        ) as identified:
            readable = await identified.fetchval("select mm_readable_ns()")
    finally:
        await conn.close()
    return list(readable)


async def _fetch_audit_rows(database_url: str, *, op: str) -> list[asyncpg.Record]:
    conn = await asyncpg.connect(database_url)
    try:
        return await conn.fetch("select actor, op, detail from audit_log where op = $1", op)
    finally:
        await conn.close()


# -- non-admin is refused everywhere --------------------------------------------------


async def test_every_break_glass_route_rejects_a_non_admin_session_with_403(
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
        await _login_as(client, mock_idp_client, oid="oid-not-admin", roles=[_MEMORY_USER])

        page_response = await client.get(PATH)
        assert page_response.status_code == 200, page_response.text
        assert "<h2>Break-glass</h2>" not in page_response.text

        for path in (REQUEST_PATH, APPROVE_PATH, DENY_PATH, REVOKE_PATH):
            response = await client.post(path, data={})
            assert response.status_code == 403, f"{path}: {response.status_code} {response.text}"


# -- request: refuses a target with no personal namespace ----------------------------


async def test_request_refuses_a_target_with_no_personal_namespace(
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
        await _login_as(client, mock_idp_client, oid="oid-admin-norequest", roles=[_MEMORY_ADMIN])
        page_response = await client.get(PATH)
        token = _csrf_token_for(page_response.text, action=REQUEST_PATH, form_name="request")

        response = await client.post(
            REQUEST_PATH,
            data={
                CSRF_FIELD_NAME: token,
                "oid": "oid-nobody-home",
                "reason": "incident review",
            },
        )
        assert response.status_code == 404, response.text


# -- self-approval: refused with two approvers (default), allowed with one -----------


async def test_self_approval_refused_with_two_approvers_default(
    admin_database_url: str,
    test_database_url: str,
    postgres_app_role_environ: dict[str, str],
    mock_idp_client: httpx.AsyncClient,
) -> None:
    target_oid = "oid-target-refuse"
    admin_oid = "oid-admin-self-refuse"

    authenticator = _entra_authenticator(mock_idp_client)
    config = _config()
    async with _running_app(postgres_app_role_environ, config, authenticator=authenticator) as (
        _app,
        client,
    ):
        await _seed_personal_namespace(test_database_url, oid=target_oid, alias="u-target-refuse")
        await _login_as(client, mock_idp_client, oid=admin_oid, roles=[_MEMORY_ADMIN])
        page_response = await client.get(PATH)
        request_token = _csrf_token_for(
            page_response.text, action=REQUEST_PATH, form_name="request"
        )

        request_response = await client.post(
            REQUEST_PATH,
            data={CSRF_FIELD_NAME: request_token, "oid": target_oid, "reason": "incident review"},
        )
        assert request_response.status_code == 302, request_response.text
        grant_id = await _latest_grant_id(test_database_url, requester=admin_oid)

        page_response = await client.get(PATH)
        approve_token = _csrf_token_for(
            page_response.text, action=APPROVE_PATH, form_name="approve"
        )
        approve_response = await client.post(
            APPROVE_PATH,
            data={CSRF_FIELD_NAME: approve_token, "grant_id": str(grant_id)},
        )
        assert approve_response.status_code == 403, approve_response.text

    conn = await asyncpg.connect(test_database_url)
    try:
        approved = await conn.fetchval(
            "select approved from break_glass_grants where id = $1", grant_id
        )
    finally:
        await conn.close()
    assert approved is False


async def test_self_approval_allowed_with_one_approver(
    admin_database_url: str,
    test_database_url: str,
    postgres_app_role_environ: dict[str, str],
    mock_idp_client: httpx.AsyncClient,
) -> None:
    target_oid = "oid-target-allow"
    admin_oid = "oid-admin-self-allow"

    environ = dict(postgres_app_role_environ)
    environ["BREAK_GLASS_APPROVERS"] = "1"

    authenticator = _entra_authenticator(mock_idp_client)
    config = _config()
    async with _running_app(environ, config, authenticator=authenticator) as (_app, client):
        await _seed_personal_namespace(test_database_url, oid=target_oid, alias="u-target-allow")
        await _login_as(client, mock_idp_client, oid=admin_oid, roles=[_MEMORY_ADMIN])
        page_response = await client.get(PATH)
        request_token = _csrf_token_for(
            page_response.text, action=REQUEST_PATH, form_name="request"
        )

        request_response = await client.post(
            REQUEST_PATH,
            data={CSRF_FIELD_NAME: request_token, "oid": target_oid, "reason": "incident review"},
        )
        assert request_response.status_code == 302, request_response.text
        grant_id = await _latest_grant_id(test_database_url, requester=admin_oid)

        page_response = await client.get(PATH)
        approve_token = _csrf_token_for(
            page_response.text, action=APPROVE_PATH, form_name="approve"
        )
        approve_response = await client.post(
            APPROVE_PATH,
            data={CSRF_FIELD_NAME: approve_token, "grant_id": str(grant_id)},
        )
        assert approve_response.status_code == 302, approve_response.text

        app_role = environ["DATABASE_APP_ROLE"]
        readable = await _readable_namespaces(
            test_database_url,
            app_role=app_role,
            oid=admin_oid,
            roles=[_MEMORY_ADMIN],
            break_glass=grant_id,
        )
        assert "u-target-allow" in readable


# -- a grant is usable only by its own requester --------------------------------------


async def test_grant_usable_only_by_the_requester(
    admin_database_url: str,
    test_database_url: str,
    postgres_app_role_environ: dict[str, str],
    mock_idp_client: httpx.AsyncClient,
) -> None:
    target_oid = "oid-target-requester-only"
    requester_oid = "oid-admin-requester"
    approver_oid = "oid-admin-approver"

    authenticator = _entra_authenticator(mock_idp_client)
    config = _config()
    async with _running_app(postgres_app_role_environ, config, authenticator=authenticator) as (
        _app,
        client,
    ):
        await _seed_personal_namespace(
            test_database_url, oid=target_oid, alias="u-target-requester-only"
        )
        await _login_as(client, mock_idp_client, oid=requester_oid, roles=[_MEMORY_ADMIN])
        page_response = await client.get(PATH)
        request_token = _csrf_token_for(
            page_response.text, action=REQUEST_PATH, form_name="request"
        )
        request_response = await client.post(
            REQUEST_PATH,
            data={CSRF_FIELD_NAME: request_token, "oid": target_oid, "reason": "incident review"},
        )
        assert request_response.status_code == 302, request_response.text
        grant_id = await _latest_grant_id(test_database_url, requester=requester_oid)

        await _login_as(client, mock_idp_client, oid=approver_oid, roles=[_MEMORY_ADMIN])
        page_response = await client.get(PATH)
        approve_token = _csrf_token_for(
            page_response.text, action=APPROVE_PATH, form_name="approve"
        )
        approve_response = await client.post(
            APPROVE_PATH,
            data={CSRF_FIELD_NAME: approve_token, "grant_id": str(grant_id)},
        )
        assert approve_response.status_code == 302, approve_response.text

        app_role = postgres_app_role_environ["DATABASE_APP_ROLE"]
        requester_readable = await _readable_namespaces(
            test_database_url,
            app_role=app_role,
            oid=requester_oid,
            roles=[_MEMORY_ADMIN],
            break_glass=grant_id,
        )
        approver_readable = await _readable_namespaces(
            test_database_url,
            app_role=app_role,
            oid=approver_oid,
            roles=[_MEMORY_ADMIN],
            break_glass=grant_id,
        )
        assert "u-target-requester-only" in requester_readable
        assert "u-target-requester-only" not in approver_readable


# -- mm_readable_ns() includes the namespace only between approval and +1h -----------


async def test_readable_only_between_approval_and_expiry(
    admin_database_url: str,
    test_database_url: str,
    postgres_app_role_environ: dict[str, str],
    mock_idp_client: httpx.AsyncClient,
) -> None:
    target_oid = "oid-target-window"
    requester_oid = "oid-admin-window-requester"
    approver_oid = "oid-admin-window-approver"
    alias = "u-target-window"

    authenticator = _entra_authenticator(mock_idp_client)
    config = _config()
    async with _running_app(postgres_app_role_environ, config, authenticator=authenticator) as (
        _app,
        client,
    ):
        await _seed_personal_namespace(test_database_url, oid=target_oid, alias=alias)
        await _login_as(client, mock_idp_client, oid=requester_oid, roles=[_MEMORY_ADMIN])
        page_response = await client.get(PATH)
        request_token = _csrf_token_for(
            page_response.text, action=REQUEST_PATH, form_name="request"
        )
        request_response = await client.post(
            REQUEST_PATH,
            data={CSRF_FIELD_NAME: request_token, "oid": target_oid, "reason": "incident review"},
        )
        assert request_response.status_code == 302, request_response.text
        grant_id = await _latest_grant_id(test_database_url, requester=requester_oid)

        app_role = postgres_app_role_environ["DATABASE_APP_ROLE"]
        before_approval = await _readable_namespaces(
            test_database_url,
            app_role=app_role,
            oid=requester_oid,
            roles=[_MEMORY_ADMIN],
            break_glass=grant_id,
        )
        assert alias not in before_approval

        await _login_as(client, mock_idp_client, oid=approver_oid, roles=[_MEMORY_ADMIN])
        page_response = await client.get(PATH)
        approve_token = _csrf_token_for(
            page_response.text, action=APPROVE_PATH, form_name="approve"
        )
        approve_response = await client.post(
            APPROVE_PATH,
            data={CSRF_FIELD_NAME: approve_token, "grant_id": str(grant_id)},
        )
        assert approve_response.status_code == 302, approve_response.text

        after_approval = await _readable_namespaces(
            test_database_url,
            app_role=app_role,
            oid=requester_oid,
            roles=[_MEMORY_ADMIN],
            break_glass=grant_id,
        )
        assert alias in after_approval

        await _expire_grant(test_database_url, grant_id=grant_id)
        after_expiry = await _readable_namespaces(
            test_database_url,
            app_role=app_role,
            oid=requester_oid,
            roles=[_MEMORY_ADMIN],
            break_glass=grant_id,
        )
        assert alias not in after_expiry


# -- revoke ends access at once --------------------------------------------------------


async def test_revoke_ends_access_at_once(
    admin_database_url: str,
    test_database_url: str,
    postgres_app_role_environ: dict[str, str],
    mock_idp_client: httpx.AsyncClient,
) -> None:
    target_oid = "oid-target-revoke"
    requester_oid = "oid-admin-revoke-requester"
    approver_oid = "oid-admin-revoke-approver"
    alias = "u-target-revoke"

    authenticator = _entra_authenticator(mock_idp_client)
    config = _config()
    async with _running_app(postgres_app_role_environ, config, authenticator=authenticator) as (
        _app,
        client,
    ):
        await _seed_personal_namespace(test_database_url, oid=target_oid, alias=alias)
        await _login_as(client, mock_idp_client, oid=requester_oid, roles=[_MEMORY_ADMIN])
        page_response = await client.get(PATH)
        request_token = _csrf_token_for(
            page_response.text, action=REQUEST_PATH, form_name="request"
        )
        request_response = await client.post(
            REQUEST_PATH,
            data={CSRF_FIELD_NAME: request_token, "oid": target_oid, "reason": "incident review"},
        )
        assert request_response.status_code == 302, request_response.text
        grant_id = await _latest_grant_id(test_database_url, requester=requester_oid)

        await _login_as(client, mock_idp_client, oid=approver_oid, roles=[_MEMORY_ADMIN])
        page_response = await client.get(PATH)
        approve_token = _csrf_token_for(
            page_response.text, action=APPROVE_PATH, form_name="approve"
        )
        approve_response = await client.post(
            APPROVE_PATH,
            data={CSRF_FIELD_NAME: approve_token, "grant_id": str(grant_id)},
        )
        assert approve_response.status_code == 302, approve_response.text

        app_role = postgres_app_role_environ["DATABASE_APP_ROLE"]
        before_revoke = await _readable_namespaces(
            test_database_url,
            app_role=app_role,
            oid=requester_oid,
            roles=[_MEMORY_ADMIN],
            break_glass=grant_id,
        )
        assert alias in before_revoke

        page_response = await client.get(PATH)
        revoke_token = _csrf_token_for(page_response.text, action=REVOKE_PATH, form_name="revoke")
        revoke_response = await client.post(
            REVOKE_PATH,
            data={CSRF_FIELD_NAME: revoke_token, "grant_id": str(grant_id)},
        )
        assert revoke_response.status_code == 302, revoke_response.text

        after_revoke = await _readable_namespaces(
            test_database_url,
            app_role=app_role,
            oid=requester_oid,
            roles=[_MEMORY_ADMIN],
            break_glass=grant_id,
        )
        assert alias not in after_revoke

        # Revoking an already-revoked grant has nothing left to revoke. The
        # token itself is a deterministic HMAC(session_id, form) - not a
        # nonce (`account.sessions.csrf_token`'s own docstring) - so the one
        # already minted above is still valid, even though the row no
        # longer renders a revoke form at all now that it is revoked.
        second_revoke_response = await client.post(
            REVOKE_PATH,
            data={CSRF_FIELD_NAME: revoke_token, "grant_id": str(grant_id)},
        )
        assert second_revoke_response.status_code == 404, second_revoke_response.text


# -- deny ends a pending request without ever granting access -------------------------


async def test_deny_ends_a_pending_request(
    admin_database_url: str,
    test_database_url: str,
    postgres_app_role_environ: dict[str, str],
    mock_idp_client: httpx.AsyncClient,
) -> None:
    target_oid = "oid-target-deny"
    requester_oid = "oid-admin-deny-requester"
    approver_oid = "oid-admin-deny-approver"

    authenticator = _entra_authenticator(mock_idp_client)
    config = _config()
    async with _running_app(postgres_app_role_environ, config, authenticator=authenticator) as (
        _app,
        client,
    ):
        await _seed_personal_namespace(test_database_url, oid=target_oid, alias="u-target-deny")
        await _login_as(client, mock_idp_client, oid=requester_oid, roles=[_MEMORY_ADMIN])
        page_response = await client.get(PATH)
        request_token = _csrf_token_for(
            page_response.text, action=REQUEST_PATH, form_name="request"
        )
        request_response = await client.post(
            REQUEST_PATH,
            data={CSRF_FIELD_NAME: request_token, "oid": target_oid, "reason": "incident review"},
        )
        assert request_response.status_code == 302, request_response.text
        grant_id = await _latest_grant_id(test_database_url, requester=requester_oid)

        await _login_as(client, mock_idp_client, oid=approver_oid, roles=[_MEMORY_ADMIN])
        page_response = await client.get(PATH)
        deny_token = _csrf_token_for(page_response.text, action=DENY_PATH, form_name="deny")
        # Minted from the same (still-pending) page load as `deny_token` -
        # the row stops rendering an approve form at all once it is denied,
        # but the token itself is a deterministic HMAC(session_id, form),
        # not a nonce (`account.sessions.csrf_token`'s own docstring), so
        # the one minted here is still valid afterwards.
        approve_token = _csrf_token_for(
            page_response.text, action=APPROVE_PATH, form_name="approve"
        )
        deny_response = await client.post(
            DENY_PATH,
            data={CSRF_FIELD_NAME: deny_token, "grant_id": str(grant_id)},
        )
        assert deny_response.status_code == 302, deny_response.text

        approve_response = await client.post(
            APPROVE_PATH,
            data={CSRF_FIELD_NAME: approve_token, "grant_id": str(grant_id)},
        )
        # `mm_break_glass_approve` itself refuses a denied grant with
        # `22023` (invalid_parameter_value), mapped to 400 - distinct from
        # the 404 a *nonexistent* grant id gets.
        assert approve_response.status_code == 400, approve_response.text

    conn = await asyncpg.connect(test_database_url)
    try:
        row = await conn.fetchrow(
            "select approved, revoked_at from break_glass_grants where id = $1", grant_id
        )
    finally:
        await conn.close()
    assert row is not None
    assert row["approved"] is False
    assert row["revoked_at"] is not None


# -- audit: request, approve, deny, revoke each leave a metadata-only row ------------


async def test_request_approve_and_revoke_are_audited_with_metadata_only(
    admin_database_url: str,
    test_database_url: str,
    postgres_app_role_environ: dict[str, str],
    mock_idp_client: httpx.AsyncClient,
) -> None:
    target_oid = "oid-target-audit"
    requester_oid = "oid-admin-audit-requester"
    approver_oid = "oid-admin-audit-approver"
    reason = "reviewing a confidential incident"

    authenticator = _entra_authenticator(mock_idp_client)
    config = _config()
    async with _running_app(postgres_app_role_environ, config, authenticator=authenticator) as (
        _app,
        client,
    ):
        await _seed_personal_namespace(test_database_url, oid=target_oid, alias="u-target-audit")
        await _login_as(client, mock_idp_client, oid=requester_oid, roles=[_MEMORY_ADMIN])
        page_response = await client.get(PATH)
        request_token = _csrf_token_for(
            page_response.text, action=REQUEST_PATH, form_name="request"
        )
        request_response = await client.post(
            REQUEST_PATH,
            data={CSRF_FIELD_NAME: request_token, "oid": target_oid, "reason": reason},
        )
        assert request_response.status_code == 302, request_response.text
        grant_id = await _latest_grant_id(test_database_url, requester=requester_oid)

        await _login_as(client, mock_idp_client, oid=approver_oid, roles=[_MEMORY_ADMIN])
        page_response = await client.get(PATH)
        approve_token = _csrf_token_for(
            page_response.text, action=APPROVE_PATH, form_name="approve"
        )
        approve_response = await client.post(
            APPROVE_PATH,
            data={CSRF_FIELD_NAME: approve_token, "grant_id": str(grant_id)},
        )
        assert approve_response.status_code == 302, approve_response.text

        page_response = await client.get(PATH)
        revoke_token = _csrf_token_for(page_response.text, action=REVOKE_PATH, form_name="revoke")
        revoke_response = await client.post(
            REVOKE_PATH,
            data={CSRF_FIELD_NAME: revoke_token, "grant_id": str(grant_id)},
        )
        assert revoke_response.status_code == 302, revoke_response.text

    request_rows = await _fetch_audit_rows(test_database_url, op="admin.break_glass.request")
    approve_rows = await _fetch_audit_rows(test_database_url, op="admin.break_glass.approve")
    revoke_rows = await _fetch_audit_rows(test_database_url, op="admin.break_glass.revoke")

    matching_request = [row for row in request_rows if row["actor"] == requester_oid]
    matching_approve = [row for row in approve_rows if row["actor"] == approver_oid]
    # The same session that just approved it also revokes it here - any
    # Memory.Admin may revoke, not only the original requester.
    matching_revoke = [row for row in revoke_rows if row["actor"] == approver_oid]
    assert matching_request, [dict(row) for row in request_rows]
    assert matching_approve, [dict(row) for row in approve_rows]
    assert matching_revoke, [dict(row) for row in revoke_rows]

    # The reason is metadata the admin themself typed, not note content -
    # storing it in the audit row is intentional (CLAUDE.md "reason" is on
    # `audit.DETAIL_ALLOWLIST`).
    request_detail = matching_request[0]["detail"]
    assert target_oid in request_detail
    assert reason in request_detail
    assert str(grant_id) in matching_approve[0]["detail"]
    assert str(grant_id) in matching_revoke[0]["detail"]
