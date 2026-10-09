# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the read-only, audited break-glass viewer on `/account`'s admin
area (#238, ADR-0008 addendum 2026-10-08: "break-glass reads happen only in
this viewer").

Login/request/approve is driven exactly the way `tests/account/
test_break_glass.py` drives it - that module's own docstring explains the
shape; the helpers below are a trimmed copy rather than a cross-module
import (`tests/account/conftest.py`'s own docstring: every package under
`tests/` that needs a given shape defines its own).
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

from memory_manager.account.break_glass import APPROVE_PATH, REQUEST_PATH, REVOKE_PATH
from memory_manager.account.break_glass_viewer import NOTE_PATH, VIEW_PATH
from memory_manager.account.routes import LOGIN_PATH_START, PATH
from memory_manager.account.templates import CSRF_FIELD_NAME
from memory_manager.app import open_services
from memory_manager.auth.login_entra import CALLBACK_PATH as ENTRA_CALLBACK_PATH
from memory_manager.auth.login_entra import EntraAuthenticator
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

_NOTE_TEXT = (
    "---\n"
    "id: 01ARZ3NDEKTSV4RRFFQ69G5FAV\n"
    "title: Confidential incident\n"
    "description: desc\n"
    "type: fact\n"
    "created: 2026-01-01T00:00:00+00:00\n"
    "updated: 2026-01-01T00:00:00+00:00\n"
    "---\n"
    "Secret body text.\n"
)


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


def _csrf_token_for(page_html: str, *, action: str) -> str:
    """The per-form CSRF token for the form whose `action` is `action` - same
    extraction shape `tests/account/test_break_glass.py`'s own
    `_csrf_token_for` uses."""
    form_marker = f'action="{html.escape(action)}"'
    form_start = page_html.index(form_marker)
    marker = f'name="{CSRF_FIELD_NAME}" value="'
    start = page_html.index(marker, form_start) + len(marker)
    end = page_html.index('"', start)
    return html.unescape(page_html[start:end])


async def _seed_personal_namespace(database_url: str, *, oid: str, alias: str) -> None:
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


async def _seed_vault_note(database_url: str, *, namespace: str, path: str, content: str) -> None:
    """A minimal `vault_notes` row, direct against the owner connection
    (bypasses RLS, same assumption `tests/account/test_admin.py`'s own
    `_seed_vault_note` states)."""
    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute(
            "insert into vault_notes (id, namespace, path, content, version, current_revision) "
            "values ($1, $2, $3, $4, 'v1', 1)",
            f"note-{namespace}-{path.replace('/', '-')}",
            namespace,
            path,
            content.encode(),
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
    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute(
            "update break_glass_grants set expires_at = now() - interval '1 second' where id = $1",
            grant_id,
        )
    finally:
        await conn.close()


async def _fetch_audit_rows(database_url: str, *, op: str) -> list[asyncpg.Record]:
    conn = await asyncpg.connect(database_url)
    try:
        return await conn.fetch("select actor, op, path, detail from audit_log where op = $1", op)
    finally:
        await conn.close()


async def _request_and_approve(
    client: httpx.AsyncClient,
    mock_idp_client: httpx.AsyncClient,
    test_database_url: str,
    *,
    target_oid: str,
    alias: str,
    requester_oid: str,
    approver_oid: str,
) -> int:
    """Seeds `target_oid`'s personal namespace, has `requester_oid` request
    break-glass access to it and `approver_oid` approve it - returns the
    grant id, now approved and unexpired. Leaves the client logged in as
    `requester_oid` (the one and only identity `_active_grant` ever
    accepts for the grant this returns)."""
    await _seed_personal_namespace(test_database_url, oid=target_oid, alias=alias)
    await _login_as(client, mock_idp_client, oid=requester_oid, roles=[_MEMORY_ADMIN])
    page_response = await client.get(PATH)
    request_token = _csrf_token_for(page_response.text, action=REQUEST_PATH)
    request_response = await client.post(
        REQUEST_PATH,
        data={CSRF_FIELD_NAME: request_token, "oid": target_oid, "reason": "incident review"},
    )
    assert request_response.status_code == 302, request_response.text
    grant_id = await _latest_grant_id(test_database_url, requester=requester_oid)

    await _login_as(client, mock_idp_client, oid=approver_oid, roles=[_MEMORY_ADMIN])
    page_response = await client.get(PATH)
    approve_token = _csrf_token_for(page_response.text, action=APPROVE_PATH)
    approve_response = await client.post(
        APPROVE_PATH,
        data={CSRF_FIELD_NAME: approve_token, "grant_id": str(grant_id)},
    )
    assert approve_response.status_code == 302, approve_response.text

    await _login_as(client, mock_idp_client, oid=requester_oid, roles=[_MEMORY_ADMIN])
    return grant_id


# -- no access before approval --------------------------------------------------------


async def test_no_access_before_approval(
    admin_database_url: str,
    test_database_url: str,
    postgres_app_role_environ: dict[str, str],
    mock_idp_client: httpx.AsyncClient,
) -> None:
    target_oid = "oid-target-pending"
    requester_oid = "oid-admin-pending-requester"
    alias = "u-target-pending"

    authenticator = _entra_authenticator(mock_idp_client)
    config = _config()
    async with _running_app(postgres_app_role_environ, config, authenticator=authenticator) as (
        _app,
        client,
    ):
        await _seed_personal_namespace(test_database_url, oid=target_oid, alias=alias)
        await _login_as(client, mock_idp_client, oid=requester_oid, roles=[_MEMORY_ADMIN])
        page_response = await client.get(PATH)
        request_token = _csrf_token_for(page_response.text, action=REQUEST_PATH)
        request_response = await client.post(
            REQUEST_PATH,
            data={CSRF_FIELD_NAME: request_token, "oid": target_oid, "reason": "incident review"},
        )
        assert request_response.status_code == 302, request_response.text
        grant_id = await _latest_grant_id(test_database_url, requester=requester_oid)

        response = await client.get(VIEW_PATH, params={"grant_id": str(grant_id)})
        assert response.status_code == 403, response.text


# -- list and note view succeed and are audited after approval ------------------------


async def test_list_and_note_view_succeed_and_are_audited_after_approval(
    admin_database_url: str,
    test_database_url: str,
    postgres_app_role_environ: dict[str, str],
    mock_idp_client: httpx.AsyncClient,
) -> None:
    target_oid = "oid-target-approved"
    requester_oid = "oid-admin-approved-requester"
    approver_oid = "oid-admin-approved-approver"
    alias = "u-target-approved"
    note_path = f"{alias}/fact/confidential.md"

    authenticator = _entra_authenticator(mock_idp_client)
    config = _config()
    async with _running_app(postgres_app_role_environ, config, authenticator=authenticator) as (
        _app,
        client,
    ):
        grant_id = await _request_and_approve(
            client,
            mock_idp_client,
            test_database_url,
            target_oid=target_oid,
            alias=alias,
            requester_oid=requester_oid,
            approver_oid=approver_oid,
        )
        await _seed_vault_note(
            test_database_url, namespace=alias, path=note_path, content=_NOTE_TEXT
        )

        list_response = await client.get(VIEW_PATH, params={"grant_id": str(grant_id)})
        assert list_response.status_code == 200, list_response.text
        assert note_path in list_response.text
        assert "Confidential incident" in list_response.text
        assert "Secret body text." not in list_response.text

        note_response = await client.get(
            NOTE_PATH, params={"grant_id": str(grant_id), "path": note_path}
        )
        assert note_response.status_code == 200, note_response.text
        assert "Secret body text." in note_response.text

    audit_rows = await _fetch_audit_rows(test_database_url, op="break_glass.read")
    list_rows = [row for row in audit_rows if row["path"] is None]
    note_rows = [row for row in audit_rows if row["path"] == note_path]
    assert list_rows, [dict(row) for row in audit_rows]
    assert note_rows, [dict(row) for row in audit_rows]
    assert all(row["actor"] == requester_oid for row in list_rows + note_rows)
    assert str(grant_id) in list_rows[0]["detail"]
    assert str(grant_id) in note_rows[0]["detail"]


# -- note content is escaped, never interpreted as HTML --------------------------------


async def test_note_html_is_escaped(
    admin_database_url: str,
    test_database_url: str,
    postgres_app_role_environ: dict[str, str],
    mock_idp_client: httpx.AsyncClient,
) -> None:
    target_oid = "oid-target-escaped"
    requester_oid = "oid-admin-escaped-requester"
    approver_oid = "oid-admin-escaped-approver"
    alias = "u-target-escaped"
    # A path crafted to break out of both an HTML text node (`<script>`)
    # and an HTML attribute (`">`/`"onmouseover=`) - #238 rework: the
    # viewer's own note page used to interpolate `path` unescaped into its
    # subtitle whenever the note's `type` was non-empty (the common case),
    # reflecting this payload verbatim.
    note_path = f'{alias}/fact/esc" onmouseover="alert(2)"><script>alert(1)</script>.md'
    note_text = (
        "---\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FAW\n"
        "title: <script>evil</script>\n"
        "description: desc\n"
        "type: fact\n"
        "created: 2026-01-01T00:00:00+00:00\n"
        "updated: 2026-01-01T00:00:00+00:00\n"
        "---\n"
        "<img src=x onerror=alert(1)>\n"
    )

    authenticator = _entra_authenticator(mock_idp_client)
    config = _config()
    async with _running_app(postgres_app_role_environ, config, authenticator=authenticator) as (
        _app,
        client,
    ):
        grant_id = await _request_and_approve(
            client,
            mock_idp_client,
            test_database_url,
            target_oid=target_oid,
            alias=alias,
            requester_oid=requester_oid,
            approver_oid=approver_oid,
        )
        await _seed_vault_note(
            test_database_url, namespace=alias, path=note_path, content=note_text
        )

        note_response = await client.get(
            NOTE_PATH, params={"grant_id": str(grant_id), "path": note_path}
        )
        assert note_response.status_code == 200, note_response.text
        assert "<script>evil</script>" not in note_response.text
        assert "<img src=x onerror=alert(1)>" not in note_response.text
        assert html.escape("<img src=x onerror=alert(1)>") in note_response.text
        # Text context: the note page's own subtitle used to interpolate
        # `path` unescaped (the bug this test now pins down) - `note_type`
        # ("fact") is non-empty here, the exact branch that was affected.
        assert "<script" not in note_response.text
        assert 'onmouseover="alert(2)"' not in note_response.text
        assert html.escape(note_path) in note_response.text

        # Attribute context: the list page links to this note by building
        # its `href` from the same `path` - `urlencode` plus a final
        # `html.escape` of the whole attribute value must leave no way for
        # the payload to close the `href="..."` attribute or inject a new
        # tag, on either page.
        list_response = await client.get(VIEW_PATH, params={"grant_id": str(grant_id)})
        assert list_response.status_code == 200, list_response.text
        assert "<script>alert(1)</script>" not in list_response.text
        assert "<script" not in list_response.text
        assert 'onmouseover="alert(2)"' not in list_response.text
        assert html.escape(note_path) in list_response.text


# -- denied after expiry ----------------------------------------------------------------


async def test_denied_after_expiry(
    admin_database_url: str,
    test_database_url: str,
    postgres_app_role_environ: dict[str, str],
    mock_idp_client: httpx.AsyncClient,
) -> None:
    target_oid = "oid-target-expired"
    requester_oid = "oid-admin-expired-requester"
    approver_oid = "oid-admin-expired-approver"
    alias = "u-target-expired"

    authenticator = _entra_authenticator(mock_idp_client)
    config = _config()
    async with _running_app(postgres_app_role_environ, config, authenticator=authenticator) as (
        _app,
        client,
    ):
        grant_id = await _request_and_approve(
            client,
            mock_idp_client,
            test_database_url,
            target_oid=target_oid,
            alias=alias,
            requester_oid=requester_oid,
            approver_oid=approver_oid,
        )
        await _expire_grant(test_database_url, grant_id=grant_id)

        response = await client.get(VIEW_PATH, params={"grant_id": str(grant_id)})
        assert response.status_code == 403, response.text


# -- denied after revoke -----------------------------------------------------------------


async def test_denied_after_revoke(
    admin_database_url: str,
    test_database_url: str,
    postgres_app_role_environ: dict[str, str],
    mock_idp_client: httpx.AsyncClient,
) -> None:
    target_oid = "oid-target-revoked"
    requester_oid = "oid-admin-revoked-requester"
    approver_oid = "oid-admin-revoked-approver"
    alias = "u-target-revoked"

    authenticator = _entra_authenticator(mock_idp_client)
    config = _config()
    async with _running_app(postgres_app_role_environ, config, authenticator=authenticator) as (
        _app,
        client,
    ):
        grant_id = await _request_and_approve(
            client,
            mock_idp_client,
            test_database_url,
            target_oid=target_oid,
            alias=alias,
            requester_oid=requester_oid,
            approver_oid=approver_oid,
        )

        page_response = await client.get(PATH)
        revoke_token = _csrf_token_for(page_response.text, action=REVOKE_PATH)
        revoke_response = await client.post(
            REVOKE_PATH,
            data={CSRF_FIELD_NAME: revoke_token, "grant_id": str(grant_id)},
        )
        assert revoke_response.status_code == 302, revoke_response.text

        response = await client.get(VIEW_PATH, params={"grant_id": str(grant_id)})
        assert response.status_code == 403, response.text


# -- another admin cannot use the grant --------------------------------------------------


async def test_another_admin_cannot_use_the_grant(
    admin_database_url: str,
    test_database_url: str,
    postgres_app_role_environ: dict[str, str],
    mock_idp_client: httpx.AsyncClient,
) -> None:
    target_oid = "oid-target-other-admin"
    requester_oid = "oid-admin-other-requester"
    approver_oid = "oid-admin-other-approver"
    alias = "u-target-other-admin"

    authenticator = _entra_authenticator(mock_idp_client)
    config = _config()
    async with _running_app(postgres_app_role_environ, config, authenticator=authenticator) as (
        _app,
        client,
    ):
        grant_id = await _request_and_approve(
            client,
            mock_idp_client,
            test_database_url,
            target_oid=target_oid,
            alias=alias,
            requester_oid=requester_oid,
            approver_oid=approver_oid,
        )

        # `_request_and_approve` leaves the client logged back in as the
        # requester - switch to the approver, who never requested this
        # grant and must not be able to view the namespace through it.
        await _login_as(client, mock_idp_client, oid=approver_oid, roles=[_MEMORY_ADMIN])
        response = await client.get(VIEW_PATH, params={"grant_id": str(grant_id)})
        assert response.status_code == 403, response.text


# -- no write route exists ---------------------------------------------------------------


async def test_no_write_route_exists(
    admin_database_url: str,
    test_database_url: str,
    postgres_app_role_environ: dict[str, str],
    mock_idp_client: httpx.AsyncClient,
) -> None:
    target_oid = "oid-target-nowrite"
    requester_oid = "oid-admin-nowrite-requester"
    approver_oid = "oid-admin-nowrite-approver"
    alias = "u-target-nowrite"

    authenticator = _entra_authenticator(mock_idp_client)
    config = _config()
    async with _running_app(postgres_app_role_environ, config, authenticator=authenticator) as (
        _app,
        client,
    ):
        grant_id = await _request_and_approve(
            client,
            mock_idp_client,
            test_database_url,
            target_oid=target_oid,
            alias=alias,
            requester_oid=requester_oid,
            approver_oid=approver_oid,
        )

        for path in (VIEW_PATH, NOTE_PATH):
            response = await client.post(path, data={"grant_id": str(grant_id)})
            # No `POST` route exists at either path at all - the Starlette
            # router's own `Mount("/", app=_McpMount())` catch-all
            # (`http.py`) ends up handling it instead of a `405`, since a
            # `Route`'s own method mismatch is only a partial match and a
            # later full-path `Mount` match overrides it; the MCP app
            # itself then reports `404` for a path it does not know either.
            assert response.status_code == 404, f"{path}: {response.status_code} {response.text}"


# -- MCP tools still deny the namespace (no break_glass in play there at all) ------------


async def test_mcp_read_path_never_passes_break_glass(
    admin_database_url: str,
    test_database_url: str,
    postgres_app_role_environ: dict[str, str],
    mock_idp_client: httpx.AsyncClient,
) -> None:
    """`db.rls.request_connection` (the MCP read path's own seam) calls
    `request_identity` with no `break_glass=` argument at all, regardless
    of any grant an admin holds in a completely separate browser session -
    `mm_readable_ns()`'s own `break_glass` CTE then has nothing to match
    (`app.break_glass` empty), so the namespace stays unreadable there even
    while this viewer's own grant is active."""
    target_oid = "oid-target-mcp"
    requester_oid = "oid-admin-mcp-requester"
    approver_oid = "oid-admin-mcp-approver"
    alias = "u-target-mcp"

    authenticator = _entra_authenticator(mock_idp_client)
    config = _config()
    async with _running_app(postgres_app_role_environ, config, authenticator=authenticator) as (
        _app,
        client,
    ):
        grant_id = await _request_and_approve(
            client,
            mock_idp_client,
            test_database_url,
            target_oid=target_oid,
            alias=alias,
            requester_oid=requester_oid,
            approver_oid=approver_oid,
        )

        # The viewer itself can read it, under the grant.
        response = await client.get(VIEW_PATH, params={"grant_id": str(grant_id)})
        assert response.status_code == 200, response.text

    from memory_manager.db import rls

    app_role = postgres_app_role_environ["DATABASE_APP_ROLE"]
    conn = await asyncpg.connect(test_database_url)
    try:
        async with rls.request_identity(
            conn, role=app_role, oid=requester_oid, roles=[_MEMORY_ADMIN]
        ) as identified:
            readable = await identified.fetchval("select mm_readable_ns()")
    finally:
        await conn.close()
    assert alias not in list(readable)
