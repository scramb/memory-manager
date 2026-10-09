# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the break-glass notification (#239, ADR-0008 addendum
2026-10-08 "/account session and break-glass notification").

Login is driven exactly the way `tests/account/test_break_glass.py` drives
Entra login - that module's own docstring explains the shape; the helpers
below are a trimmed copy rather than a cross-module import (`tests/account/
conftest.py`'s own docstring: every package under `tests/` that needs a
given shape defines its own).

`BREAK_GLASS_APPROVERS=1` throughout: these tests are about the notice, not
the four-eyes rule (`tests/account/test_break_glass.py` already covers
that), so one admin requests and approves its own grant.
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

from memory_manager.account import sessions
from memory_manager.account.break_glass import APPROVE_PATH, REQUEST_PATH
from memory_manager.account.break_glass_notice import ACKNOWLEDGE_PATH, CSRF_FORM_ACKNOWLEDGE
from memory_manager.account.routes import LOGIN_PATH_START, PATH, SESSION_COOKIE
from memory_manager.account.templates import CSRF_FIELD_NAME
from memory_manager.app import open_services
from memory_manager.auth.login_entra import CALLBACK_PATH as ENTRA_CALLBACK_PATH
from memory_manager.auth.login_entra import EntraAuthenticator
from memory_manager.config import ServerConfig
from memory_manager.http import create_app
from memory_manager.vault.note import parse as parse_note

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


def _one_approver_environ(environ: dict[str, str]) -> dict[str, str]:
    merged = dict(environ)
    merged["BREAK_GLASS_APPROVERS"] = "1"
    return merged


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
    extraction shape `tests/account/test_break_glass.py`'s own `_csrf_token_for`
    uses. `form_name` is unused beyond documenting *which* form a caller means."""
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
    having signed in or written a note (same reasoning `tests/account/
    test_break_glass.py`'s own helper of the same name gives)."""
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


async def _fetch_vault_row(database_url: str, *, path: str) -> asyncpg.Record | None:
    conn = await asyncpg.connect(database_url)
    try:
        return await conn.fetchrow(
            "select vn.path, vr.content, vr.author_oid, vr.author, vr.client "
            "from vault_notes vn join vault_revisions vr "
            "on vr.note_id = vn.id and vr.revision = vn.current_revision "
            "where vn.path = $1",
            path,
        )
    finally:
        await conn.close()


async def _fetch_audit_rows(database_url: str, *, op: str) -> list[asyncpg.Record]:
    conn = await asyncpg.connect(database_url)
    try:
        return await conn.fetch("select actor, op, detail from audit_log where op = $1", op)
    finally:
        await conn.close()


# -- approval writes exactly one valid reference note with who/when/reason/expiry ----


async def test_approval_writes_exactly_one_reference_note_with_the_facts(
    admin_database_url: str,
    test_database_url: str,
    postgres_app_role_environ: dict[str, str],
    mock_idp_client: httpx.AsyncClient,
) -> None:
    target_oid = "oid-target-notice-note"
    admin_oid = "oid-admin-notice-note"
    alias = "u-target-notice-note"
    reason = "reviewing a confidential incident"

    authenticator = _entra_authenticator(mock_idp_client)
    config = _config()
    environ = _one_approver_environ(postgres_app_role_environ)
    async with _running_app(environ, config, authenticator=authenticator) as (_app, client):
        await _seed_personal_namespace(test_database_url, oid=target_oid, alias=alias)
        await _login_as(client, mock_idp_client, oid=admin_oid, roles=[_MEMORY_ADMIN])
        page_response = await client.get(PATH)
        request_token = _csrf_token_for(
            page_response.text, action=REQUEST_PATH, form_name="request"
        )
        request_response = await client.post(
            REQUEST_PATH,
            data={CSRF_FIELD_NAME: request_token, "oid": target_oid, "reason": reason},
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

    path = f"{alias}/reference/break-glass-{grant_id}.md"
    row = await _fetch_vault_row(test_database_url, path=path)
    assert row is not None

    note = parse_note(bytes(row["content"]))
    assert note.type == "reference"
    assert admin_oid in note.body
    assert reason in note.body

    # Written by the system identity, not the approving admin (ADR-0008
    # addendum: "written by the system identity ... author_oid NULL").
    assert row["author_oid"] is None
    assert row["author"] == "system"
    assert row["client"] == "account"

    # Exactly one note for this grant - never more than one `vault_notes` row
    # at this path.
    count = await _path_count(test_database_url, path=path)
    assert count == 1


async def _path_count(database_url: str, *, path: str) -> int:
    conn = await asyncpg.connect(database_url)
    try:
        value = await conn.fetchval("select count(*) from vault_notes where path = $1", path)
    finally:
        await conn.close()
    return int(value)


# -- the banner is shown only to the affected user, until acknowledged ---------------


async def test_banner_shown_only_to_the_affected_user_until_acknowledged(
    admin_database_url: str,
    test_database_url: str,
    postgres_app_role_environ: dict[str, str],
    mock_idp_client: httpx.AsyncClient,
) -> None:
    target_oid = "oid-target-banner"
    admin_oid = "oid-admin-banner"
    bystander_oid = "oid-bystander-banner"
    alias = "u-target-banner"
    reason = "incident review"

    authenticator = _entra_authenticator(mock_idp_client)
    config = _config()
    environ = _one_approver_environ(postgres_app_role_environ)
    async with _running_app(environ, config, authenticator=authenticator) as (_app, client):
        await _seed_personal_namespace(test_database_url, oid=target_oid, alias=alias)
        await _login_as(client, mock_idp_client, oid=admin_oid, roles=[_MEMORY_ADMIN])
        page_response = await client.get(PATH)
        request_token = _csrf_token_for(
            page_response.text, action=REQUEST_PATH, form_name="request"
        )
        request_response = await client.post(
            REQUEST_PATH,
            data={CSRF_FIELD_NAME: request_token, "oid": target_oid, "reason": reason},
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

        # An unrelated user, with no grant on their own namespace, sees no
        # banner at all.
        await _login_as(client, mock_idp_client, oid=bystander_oid, roles=[_MEMORY_USER])
        bystander_page = await client.get(PATH)
        assert bystander_page.status_code == 200, bystander_page.text
        assert "<h2>Break-glass notice</h2>" not in bystander_page.text

        # The affected user sees the banner with the grant's own facts.
        await _login_as(client, mock_idp_client, oid=target_oid, roles=[_MEMORY_USER])
        target_page = await client.get(PATH)
        assert target_page.status_code == 200, target_page.text
        assert "<h2>Break-glass notice</h2>" in target_page.text
        assert admin_oid in target_page.text
        assert reason in target_page.text

        acknowledge_token = _csrf_token_for(
            target_page.text, action=ACKNOWLEDGE_PATH, form_name="acknowledge"
        )
        acknowledge_response = await client.post(
            ACKNOWLEDGE_PATH,
            data={CSRF_FIELD_NAME: acknowledge_token, "grant_id": str(grant_id)},
        )
        assert acknowledge_response.status_code == 302, acknowledge_response.text

        # Acknowledged: the banner is gone, even for the same user.
        after_ack_page = await client.get(PATH)
        assert "<h2>Break-glass notice</h2>" not in after_ack_page.text

        # Acknowledging again finds nothing left to acknowledge.
        second_ack_response = await client.post(
            ACKNOWLEDGE_PATH,
            data={CSRF_FIELD_NAME: acknowledge_token, "grant_id": str(grant_id)},
        )
        assert second_ack_response.status_code == 404, second_ack_response.text

    acknowledge_rows = await _fetch_audit_rows(
        test_database_url, op="account.break_glass_notice.acknowledge"
    )
    matching = [row for row in acknowledge_rows if row["actor"] == target_oid]
    assert matching, [dict(row) for row in acknowledge_rows]
    assert str(grant_id) in matching[0]["detail"]


# -- acknowledging someone else's grant is refused ------------------------------------


async def test_acknowledge_refuses_a_grant_that_is_not_the_caller_s_own(
    admin_database_url: str,
    test_database_url: str,
    postgres_app_role_environ: dict[str, str],
    mock_idp_client: httpx.AsyncClient,
) -> None:
    target_oid = "oid-target-notmine"
    admin_oid = "oid-admin-notmine"
    other_oid = "oid-other-notmine"
    alias = "u-target-notmine"

    authenticator = _entra_authenticator(mock_idp_client)
    config = _config()
    environ = _one_approver_environ(postgres_app_role_environ)
    async with _running_app(environ, config, authenticator=authenticator) as (_app, client):
        await _seed_personal_namespace(test_database_url, oid=target_oid, alias=alias)
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

        # Someone else altogether - not the target - signs in and tries to
        # acknowledge the target's own grant id directly. The acknowledge
        # form never renders for them at all (they have no notice of their
        # own), so the token is minted straight from their own session id
        # instead of scraped off a page - `account.sessions.csrf_token` is a
        # deterministic HMAC(session_id, form), not a server-side nonce
        # (that function's own docstring), so this is exactly the same
        # token the page would have rendered for them had it rendered one.
        await _login_as(client, mock_idp_client, oid=other_oid, roles=[_MEMORY_USER])
        other_page = await client.get(PATH)
        assert "<h2>Break-glass notice</h2>" not in other_page.text
        session_id = client.cookies.get(SESSION_COOKIE)
        assert session_id is not None
        other_token = sessions.csrf_token(session_id, CSRF_FORM_ACKNOWLEDGE)

        response = await client.post(
            ACKNOWLEDGE_PATH,
            data={CSRF_FIELD_NAME: other_token, "grant_id": str(grant_id)},
        )
        assert response.status_code == 404, response.text
