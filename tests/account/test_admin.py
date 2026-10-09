# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the `/account` admin area (#234, ADR-0008 "`Memory.Admin` manages
namespaces and ACLs. It grants no content access").

Login is driven exactly the way `tests/account/test_delete.py` drives Entra
login - that module's own docstring explains the `/account/login` ->
`/login` -> `{CALLBACK_PATH}` shape; the helpers below are a trimmed copy
rather than a cross-module import (`tests/account/conftest.py`'s own
docstring: every package under `tests/` that needs a given shape defines its
own). Only the *admin* ever needs a real Entra login here - a `project`/
`group` member is injected directly as a principal
(`tests/mcp/test_permission_matrix.py`'s own `_set_principal`, monkeypatching
`db.rls.get_access_token`) and driven through the real MCP tools
(`build_server`, an in-memory `mcp.Client`), never through the browser - the
admin area and the MCP surface are two independent paths into the same
database, and this module's own end-to-end tests exercise both at once,
the same way `tests/mcp/test_permission_matrix.py`'s "end-to-end" section
does for the permission matrix itself.
"""

from __future__ import annotations

import html
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import asyncpg
import httpx
import pytest
from mcp import Client
from mcp.server.auth.provider import AccessToken
from mock_idp_fixtures import mock_idp_client
from starlette.applications import Starlette

from memory_manager.account.admin import (
    ADD_MEMBER_PATH,
    CREATE_NAMESPACE_PATH,
    REMOVE_MEMBER_PATH,
    RENAME_NAMESPACE_PATH,
    UPDATE_SETTINGS_PATH,
)
from memory_manager.account.routes import LOGIN_PATH_START, PATH
from memory_manager.account.templates import CSRF_FIELD_NAME
from memory_manager.app import Services, open_services
from memory_manager.auth.login_entra import CALLBACK_PATH as ENTRA_CALLBACK_PATH
from memory_manager.auth.login_entra import EntraAuthenticator
from memory_manager.config import ServerConfig
from memory_manager.db import rls
from memory_manager.http import create_app
from memory_manager.mcp.server import build_server

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
    extraction shape `tests/account/test_delete.py`'s own `_delete_csrf_token`
    uses, generalized over several forms with the same hidden field name on one
    page. `form_name` is unused beyond documenting *which* form a caller means -
    `action` alone is already unique among the admin section's five forms
    (`account.admin.render_admin_section`).
    """
    _ = form_name
    form_marker = f'action="{html.escape(action)}"'
    form_start = page_html.index(form_marker)
    marker = f'name="{CSRF_FIELD_NAME}" value="'
    start = page_html.index(marker, form_start) + len(marker)
    end = page_html.index('"', start)
    return html.unescape(page_html[start:end])


def _set_principal(
    monkeypatch: pytest.MonkeyPatch, *, oid: str, roles: list[str], groups: list[str]
) -> None:
    """Make `db.rls.current_principal()` resolve to `oid`/`roles`/`groups` - same
    shape `tests/mcp/test_permission_matrix.py`'s own `_set_principal`."""
    token = AccessToken(
        token="mm_x",  # noqa: S106 - a fake test token, not a credential
        client_id="static:test",
        scopes=[],
        claims={"oid": oid, "roles": roles, "groups": groups},
    )
    monkeypatch.setattr(rls, "get_access_token", lambda: token)


def _note_text(*, title: str) -> str:
    return f"---\ntitle: {title}\ndescription: {title} description.\ntype: fact\n---\nBody.\n"


async def _insert_user_group(database_url: str, *, oid: str, group_id: str) -> None:
    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute(
            "insert into users (oid, tid, display_name) values ($1, 'tid', 'tester') "
            "on conflict (oid) do nothing",
            oid,
        )
        await conn.execute("insert into user_groups (oid, group_id) values ($1, $2)", oid, group_id)
    finally:
        await conn.close()


async def _seed_vault_note(database_url: str, *, namespace: str, slug: str) -> None:
    """A minimal `vault_notes` row, direct against the owner connection (bypasses
    RLS, same assumption `tests/account/test_export.py`'s own module docstring
    states) - enough for `memory_read`'s own `StorageBackend.read`, which never
    touches `notes`/`chunks`/`links` at all (`storage/postgres.py`'s own `read`)."""
    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute(
            "insert into vault_notes (id, namespace, path, content, version, current_revision) "
            "values ($1, $2, $3, $4, 'v1', 1)",
            f"note-{namespace}-{slug}",
            namespace,
            f"{namespace}/fact/{slug}.md",
            _note_text(title=slug).encode(),
        )
    finally:
        await conn.close()


async def _fetch_audit_rows(database_url: str, *, op: str) -> list[asyncpg.Record]:
    conn = await asyncpg.connect(database_url)
    try:
        return await conn.fetch("select actor, op, detail from audit_log where op = $1", op)
    finally:
        await conn.close()


# -- non-admin is refused everywhere --------------------------------------------------


async def test_every_admin_route_rejects_a_non_admin_session_with_403(
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
        assert "<h2>Admin</h2>" not in page_response.text

        for path in (
            CREATE_NAMESPACE_PATH,
            RENAME_NAMESPACE_PATH,
            ADD_MEMBER_PATH,
            REMOVE_MEMBER_PATH,
            UPDATE_SETTINGS_PATH,
        ):
            response = await client.post(path, data={})
            assert response.status_code == 403, f"{path}: {response.status_code} {response.text}"


# -- create: collisions and reserved aliases ------------------------------------------


async def test_create_namespace_rejects_collision_and_reserved_aliases(
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
        await _login_as(client, mock_idp_client, oid="oid-admin-collisions", roles=[_MEMORY_ADMIN])
        page_response = await client.get(PATH)
        assert "<h2>Admin</h2>" in page_response.text
        token = _csrf_token_for(
            page_response.text, action=CREATE_NAMESPACE_PATH, form_name="create"
        )

        first = await client.post(
            CREATE_NAMESPACE_PATH,
            data={
                CSRF_FIELD_NAME: token,
                "kind": "group",
                "external_key": "grp-collide-key",
                "alias": "grp-collide",
            },
        )
        assert first.status_code == 302, first.text

        # A fresh page load mints a fresh (but still valid) CSRF token for the
        # same session/form - reused below for every further POST in this test.
        page_response = await client.get(PATH)
        token = _csrf_token_for(
            page_response.text, action=CREATE_NAMESPACE_PATH, form_name="create"
        )

        collision = await client.post(
            CREATE_NAMESPACE_PATH,
            data={
                CSRF_FIELD_NAME: token,
                "kind": "project",
                "external_key": "proj-collide-key",
                "alias": "grp-collide",
            },
        )
        assert collision.status_code == 400, collision.text

        for reserved_alias in ("org", "me", "u-5", "agent-x"):
            response = await client.post(
                CREATE_NAMESPACE_PATH,
                data={
                    CSRF_FIELD_NAME: token,
                    "kind": "group",
                    "external_key": f"grp-reserved-{reserved_alias}",
                    "alias": reserved_alias,
                },
            )
            assert response.status_code == 400, f"{reserved_alias}: {response.text}"

    conn = await asyncpg.connect(test_database_url)
    try:
        count = await conn.fetchval("select count(*) from namespaces where alias = 'grp-collide'")
    finally:
        await conn.close()
    assert count == 1


# -- end-to-end: admin creates a group namespace, a group member writes via MCP -------


async def test_admin_creates_group_namespace_and_a_member_writes_via_mcp(
    admin_database_url: str,
    test_database_url: str,
    postgres_app_role_environ: dict[str, str],
    mock_idp_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    group_key = "grp-eng-key"
    alias = "grp-eng"
    member_oid = "oid-member-eng"

    authenticator = _entra_authenticator(mock_idp_client)
    config = _config()
    async with _running_app(postgres_app_role_environ, config, authenticator=authenticator) as (
        app,
        client,
    ):
        await _login_as(client, mock_idp_client, oid="oid-admin-eng", roles=[_MEMORY_ADMIN])
        page_response = await client.get(PATH)
        token = _csrf_token_for(
            page_response.text, action=CREATE_NAMESPACE_PATH, form_name="create"
        )

        create_response = await client.post(
            CREATE_NAMESPACE_PATH,
            data={
                CSRF_FIELD_NAME: token,
                "kind": "group",
                "external_key": group_key,
                "alias": alias,
            },
        )
        assert create_response.status_code == 302, create_response.text

        # The namespace now shows up in the admin listing, with a note count of 0.
        page_after_create = await client.get(PATH)
        assert f"<td>{alias}</td>" in page_after_create.text

        await _insert_user_group(test_database_url, oid=member_oid, group_id=group_key)

        services: Services = app.state.services
        _set_principal(monkeypatch, oid=member_oid, roles=[_MEMORY_USER], groups=[group_key])
        async with Client(build_server(services)) as mcp_client:
            write_result = await mcp_client.call_tool(
                "memory_write",
                {
                    "path": f"{alias}/fact/hello.md",
                    "content": _note_text(title="Hello"),
                    "if_version": "new",
                },
            )
            assert write_result.is_error is False, write_result.structured_content

    conn = await asyncpg.connect(test_database_url)
    try:
        stored = await conn.fetchval(
            "select namespace from vault_notes where path = $1", f"{alias}/fact/hello.md"
        )
    finally:
        await conn.close()
    assert stored == alias


# -- admin alone never gains content access --------------------------------------------


async def test_admin_without_membership_still_cannot_read_the_namespace_via_mcp(
    admin_database_url: str,
    test_database_url: str,
    postgres_app_role_environ: dict[str, str],
    mock_idp_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alias = "proj-secret"
    admin_oid = "oid-admin-secret"

    authenticator = _entra_authenticator(mock_idp_client)
    config = _config()
    async with _running_app(postgres_app_role_environ, config, authenticator=authenticator) as (
        app,
        client,
    ):
        await _login_as(client, mock_idp_client, oid=admin_oid, roles=[_MEMORY_ADMIN])
        page_response = await client.get(PATH)
        token = _csrf_token_for(
            page_response.text, action=CREATE_NAMESPACE_PATH, form_name="create"
        )
        create_response = await client.post(
            CREATE_NAMESPACE_PATH,
            data={
                CSRF_FIELD_NAME: token,
                "kind": "project",
                "external_key": "proj-secret-key",
                "alias": alias,
            },
        )
        assert create_response.status_code == 302, create_response.text

        await _seed_vault_note(test_database_url, namespace=alias, slug="confidential")

        services: Services = app.state.services
        _set_principal(monkeypatch, oid=admin_oid, roles=[_MEMORY_ADMIN], groups=[])
        async with Client(build_server(services)) as mcp_client:
            read_result = await mcp_client.call_tool(
                "memory_read", {"items": [f"{alias}/fact/confidential.md"]}
            )
            assert read_result.is_error is False
            items = cast(list[dict[str, Any]], read_result.structured_content["result"])
            assert len(items) == 1
            assert "content" not in items[0]
            assert items[0]["error"]["error"] == "NotFound"


# -- project member add/remove ----------------------------------------------------------


async def test_admin_adds_and_removes_a_project_member(
    admin_database_url: str,
    test_database_url: str,
    postgres_app_role_environ: dict[str, str],
    mock_idp_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alias = "proj-atlas"
    member_oid = "oid-member-atlas"

    authenticator = _entra_authenticator(mock_idp_client)
    config = _config()
    async with _running_app(postgres_app_role_environ, config, authenticator=authenticator) as (
        app,
        client,
    ):
        await _login_as(client, mock_idp_client, oid="oid-admin-atlas", roles=[_MEMORY_ADMIN])
        page_response = await client.get(PATH)
        create_token = _csrf_token_for(
            page_response.text, action=CREATE_NAMESPACE_PATH, form_name="create"
        )
        create_response = await client.post(
            CREATE_NAMESPACE_PATH,
            data={
                CSRF_FIELD_NAME: create_token,
                "kind": "project",
                "external_key": "proj-atlas-key",
                "alias": alias,
            },
        )
        assert create_response.status_code == 302, create_response.text

        page_response = await client.get(PATH)
        add_token = _csrf_token_for(page_response.text, action=ADD_MEMBER_PATH, form_name="add")
        add_response = await client.post(
            ADD_MEMBER_PATH,
            data={
                CSRF_FIELD_NAME: add_token,
                "alias": alias,
                "principal_kind": "user",
                "principal_id": member_oid,
                "role": "writer",
            },
        )
        assert add_response.status_code == 302, add_response.text

        services: Services = app.state.services
        _set_principal(monkeypatch, oid=member_oid, roles=[_MEMORY_USER], groups=[])
        async with Client(build_server(services)) as mcp_client:
            write_result = await mcp_client.call_tool(
                "memory_write",
                {
                    "path": f"{alias}/fact/first.md",
                    "content": _note_text(title="First"),
                    "if_version": "new",
                },
            )
            assert write_result.is_error is False, write_result.structured_content

        page_response = await client.get(PATH)
        remove_token = _csrf_token_for(
            page_response.text, action=REMOVE_MEMBER_PATH, form_name="remove"
        )
        remove_response = await client.post(
            REMOVE_MEMBER_PATH,
            data={
                CSRF_FIELD_NAME: remove_token,
                "alias": alias,
                "principal_kind": "user",
                "principal_id": member_oid,
            },
        )
        assert remove_response.status_code == 302, remove_response.text

        _set_principal(monkeypatch, oid=member_oid, roles=[_MEMORY_USER], groups=[])
        async with Client(build_server(services)) as mcp_client:
            second_write = await mcp_client.call_tool(
                "memory_write",
                {
                    "path": f"{alias}/fact/second.md",
                    "content": _note_text(title="Second"),
                    "if_version": "new",
                },
            )
            assert second_write.is_error is True


# -- rename + settings -------------------------------------------------------------------


async def test_admin_renames_alias_and_updates_settings(
    admin_database_url: str,
    test_database_url: str,
    postgres_app_role_environ: dict[str, str],
    mock_idp_client: httpx.AsyncClient,
) -> None:
    old_alias = "proj-before"
    new_alias = "proj-after"

    authenticator = _entra_authenticator(mock_idp_client)
    config = _config()
    async with _running_app(postgres_app_role_environ, config, authenticator=authenticator) as (
        _app,
        client,
    ):
        await _login_as(client, mock_idp_client, oid="oid-admin-rename", roles=[_MEMORY_ADMIN])
        page_response = await client.get(PATH)
        create_token = _csrf_token_for(
            page_response.text, action=CREATE_NAMESPACE_PATH, form_name="create"
        )
        create_response = await client.post(
            CREATE_NAMESPACE_PATH,
            data={
                CSRF_FIELD_NAME: create_token,
                "kind": "project",
                "external_key": "proj-rename-key",
                "alias": old_alias,
            },
        )
        assert create_response.status_code == 302, create_response.text

        page_response = await client.get(PATH)
        rename_token = _csrf_token_for(
            page_response.text, action=RENAME_NAMESPACE_PATH, form_name="rename"
        )
        rename_response = await client.post(
            RENAME_NAMESPACE_PATH,
            data={
                CSRF_FIELD_NAME: rename_token,
                "old_alias": old_alias,
                "new_alias": new_alias,
            },
        )
        assert rename_response.status_code == 302, rename_response.text

        page_response = await client.get(PATH)
        assert f"<td>{new_alias}</td>" in page_response.text
        assert f"<td>{old_alias}</td>" not in page_response.text

        settings_token = _csrf_token_for(
            page_response.text, action=UPDATE_SETTINGS_PATH, form_name="settings"
        )
        settings_response = await client.post(
            UPDATE_SETTINGS_PATH,
            data={
                CSRF_FIELD_NAME: settings_token,
                "alias": new_alias,
                "project_write": "readers",
            },
        )
        assert settings_response.status_code == 302, settings_response.text

    conn = await asyncpg.connect(test_database_url)
    try:
        project_write = await conn.fetchval(
            "select s.project_write from namespace_settings s "
            "join namespaces n on n.id = s.namespace_id where n.alias = $1",
            new_alias,
        )
    finally:
        await conn.close()
    assert project_write == "readers"


# -- audit ----------------------------------------------------------------------------


async def test_admin_actions_are_audited_with_metadata_only(
    admin_database_url: str,
    test_database_url: str,
    postgres_app_role_environ: dict[str, str],
    mock_idp_client: httpx.AsyncClient,
) -> None:
    admin_oid = "oid-admin-audit"
    alias = "grp-audited"

    authenticator = _entra_authenticator(mock_idp_client)
    config = _config()
    async with _running_app(postgres_app_role_environ, config, authenticator=authenticator) as (
        _app,
        client,
    ):
        await _login_as(client, mock_idp_client, oid=admin_oid, roles=[_MEMORY_ADMIN])
        page_response = await client.get(PATH)
        token = _csrf_token_for(
            page_response.text, action=CREATE_NAMESPACE_PATH, form_name="create"
        )
        create_response = await client.post(
            CREATE_NAMESPACE_PATH,
            data={
                CSRF_FIELD_NAME: token,
                "kind": "group",
                "external_key": "grp-audited-key",
                "alias": alias,
            },
        )
        assert create_response.status_code == 302, create_response.text

    rows = await _fetch_audit_rows(test_database_url, op="admin.namespace.create")
    assert rows, "expected at least one admin.namespace.create audit row"
    matching = [row for row in rows if row["actor"] == admin_oid]
    assert matching, [dict(row) for row in rows]
    detail = matching[0]["detail"]
    assert "confidential" not in detail
    assert alias in detail
