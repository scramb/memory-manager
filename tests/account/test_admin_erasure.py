# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the admin "erase a note, a namespace or a user" action on
`/account` (#236): a `Memory.Admin` hard-deletes a note by path, a namespace
by alias or a user by Entra object id, with a mandatory reason and a typed
confirmation of the target itself.

Admin login is driven exactly the way `tests/account/test_admin_revoke.py`
drives Entra login - that module's own docstring explains the shape; the
helpers below are a trimmed copy rather than a cross-module import (`tests/
account/conftest.py`'s own docstring: every package under `tests/` that
needs a given shape defines its own).

Notes are seeded directly against a second pool connected to the same
`test_database_url` (`tests/account/test_admin_revoke.py`'s own `_seed_note`
shape) - bypassing RLS entirely, the same assumption every fixture in this
package already makes.
"""

from __future__ import annotations

import html
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import parse_qs, urlsplit

import asyncpg
import httpx
from mock_idp_fixtures import mock_idp_client
from starlette.applications import Starlette

from memory_manager.account.admin import CONFIRM_FIELD_NAME, ERASE_PATH
from memory_manager.account.routes import LOGIN_PATH_START, PATH
from memory_manager.account.templates import CSRF_FIELD_NAME
from memory_manager.app import open_services
from memory_manager.auth import users
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


def _erase_csrf_token(page_html: str) -> str:
    form_marker = f'action="{html.escape(ERASE_PATH)}"'
    form_start = page_html.index(form_marker)
    marker = f'name="{CSRF_FIELD_NAME}" value="'
    start = page_html.index(marker, form_start) + len(marker)
    end = page_html.index('"', start)
    return html.unescape(page_html[start:end])


#: A word that only ever appears in a note's own *content* (its body), never in
#: an id, a path or a slug - `test_erase_note_removes_the_row_and_logs_actor_
#: and_reason_but_no_content` asserts this word never reaches `audit_log.detail`,
#: which only ever carries ids/paths/counts (CLAUDE.md), structurally never a
#: note's body.
_CONTENT_SENTINEL = "classified-body-content"


async def _seed_note(pool: asyncpg.Pool, *, namespace: str, slug: str) -> str:
    """A minimal `vault_notes` row, direct against the owner pool (bypasses RLS,
    same assumption `tests/account/test_admin_revoke.py`'s own `_seed_note`
    makes) - enough to prove path resolution and the erase itself."""
    note_id = f"note-{namespace}-{slug}"
    path = f"{namespace}/fact/{slug}.md"
    content = (
        f"---\ntitle: {slug}\ndescription: {slug} description.\ntype: fact\n---\n"
        f"Body mentions {_CONTENT_SENTINEL}.\n"
    )
    await pool.execute(
        "insert into vault_notes (id, namespace, path, content, version, current_revision) "
        "values ($1, $2, $3, $4, 'v1', 1)",
        note_id,
        namespace,
        path,
        content.encode(),
    )
    return path


async def _seed_namespace_row(
    pool: asyncpg.Pool, *, alias: str, kind: str, external_key: str
) -> None:
    await pool.execute(
        "insert into namespaces (kind, external_key, alias) values ($1, $2, $3)",
        kind,
        external_key,
        alias,
    )


async def test_erase_note_removes_the_row_and_logs_actor_and_reason_but_no_content(
    admin_database_url: str,
    test_database_url: str,
    postgres_app_role_environ: dict[str, str],
    mock_idp_client: httpx.AsyncClient,
) -> None:
    admin_oid = "oid-admin-erase-note"
    reason = "gdpr: data subject request"

    side_pool = await asyncpg.create_pool(test_database_url)
    try:
        authenticator = _entra_authenticator(mock_idp_client)
        config = _config()
        async with _running_app(postgres_app_role_environ, config, authenticator=authenticator) as (
            _app,
            client,
        ):
            await _login_as(client, mock_idp_client, oid=admin_oid, roles=[_MEMORY_ADMIN])

            path = await _seed_note(side_pool, namespace="project-x", slug="report")

            page_response = await client.get(PATH)
            assert "<h2>Admin</h2>" in page_response.text
            token = _erase_csrf_token(page_response.text)

            response = await client.post(
                ERASE_PATH,
                data={
                    CSRF_FIELD_NAME: token,
                    "target_kind": "note",
                    "target": path,
                    CONFIRM_FIELD_NAME: path,
                    "reason": reason,
                },
            )
            assert response.status_code == 302, response.text
            assert _location(response) == PATH

        remaining = await side_pool.fetchval(
            "select count(*) from vault_notes where path = $1", path
        )
        assert remaining == 0

        audit_row = await side_pool.fetchrow(
            "select actor, op, outcome, path, detail from audit_log where client = 'erasure'"
        )
        assert audit_row is not None
        assert audit_row["actor"] == admin_oid
        assert audit_row["outcome"] == "ok"
        assert audit_row["path"] is None
        detail = json.loads(audit_row["detail"])
        assert detail["target_kind"] == "note"
        assert _CONTENT_SENTINEL not in audit_row["detail"]

        erasure_log_row = await side_pool.fetchrow(
            "select actor, reason, target_kind from erasure_log"
        )
        assert erasure_log_row is not None
        assert erasure_log_row["actor"] == admin_oid
        assert erasure_log_row["reason"] == reason
        assert erasure_log_row["target_kind"] == "note"
    finally:
        await side_pool.close()


async def test_erase_namespace_removes_every_note_in_it(
    admin_database_url: str,
    test_database_url: str,
    postgres_app_role_environ: dict[str, str],
    mock_idp_client: httpx.AsyncClient,
) -> None:
    admin_oid = "oid-admin-erase-namespace"
    namespace = "group-doomed"

    side_pool = await asyncpg.create_pool(test_database_url)
    try:
        authenticator = _entra_authenticator(mock_idp_client)
        config = _config()
        async with _running_app(postgres_app_role_environ, config, authenticator=authenticator) as (
            _app,
            client,
        ):
            await _login_as(client, mock_idp_client, oid=admin_oid, roles=[_MEMORY_ADMIN])

            await _seed_note(side_pool, namespace=namespace, slug="one")
            await _seed_note(side_pool, namespace=namespace, slug="two")
            await _seed_namespace_row(
                side_pool, alias=namespace, kind="group", external_key=namespace
            )

            page_response = await client.get(PATH)
            token = _erase_csrf_token(page_response.text)

            response = await client.post(
                ERASE_PATH,
                data={
                    CSRF_FIELD_NAME: token,
                    "target_kind": "namespace",
                    "target": namespace,
                    CONFIRM_FIELD_NAME: namespace,
                    "reason": "project shut down",
                },
            )
            assert response.status_code == 302, response.text

        remaining_notes = await side_pool.fetchval(
            "select count(*) from vault_notes where namespace = $1", namespace
        )
        assert remaining_notes == 0
        remaining_namespace = await side_pool.fetchval(
            "select count(*) from namespaces where alias = $1", namespace
        )
        assert remaining_namespace == 0
    finally:
        await side_pool.close()


async def test_erase_user_removes_identity_and_personal_namespace(
    admin_database_url: str,
    test_database_url: str,
    postgres_app_role_environ: dict[str, str],
    mock_idp_client: httpx.AsyncClient,
) -> None:
    admin_oid = "oid-admin-erase-user"
    target_oid = "oid-target-erase-user"
    target_namespace = "u-target-erase"

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
            await _seed_note(side_pool, namespace=target_namespace, slug="personal")
            await _seed_namespace_row(
                side_pool, alias=target_namespace, kind="user", external_key=target_oid
            )

            page_response = await client.get(PATH)
            token = _erase_csrf_token(page_response.text)

            response = await client.post(
                ERASE_PATH,
                data={
                    CSRF_FIELD_NAME: token,
                    "target_kind": "user",
                    "target": target_oid,
                    CONFIRM_FIELD_NAME: target_oid,
                    "reason": "retention: deprovisioned 30 days ago",
                },
            )
            assert response.status_code == 302, response.text

        target_user = await users.get_user(side_pool, target_oid)
        assert target_user is None
        remaining_namespace = await side_pool.fetchval(
            "select count(*) from namespaces where alias = $1", target_namespace
        )
        assert remaining_namespace == 0
    finally:
        await side_pool.close()


async def test_erase_rejects_a_non_admin_session_with_403(
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
        await _login_as(client, mock_idp_client, oid="oid-not-admin-erase", roles=[_MEMORY_USER])

        response = await client.post(
            ERASE_PATH,
            data={
                "target_kind": "namespace",
                "target": "irrelevant",
                CONFIRM_FIELD_NAME: "irrelevant",
                "reason": "irrelevant",
            },
        )
        assert response.status_code == 403, response.text


async def test_erase_with_a_wrong_confirmation_deletes_nothing(
    admin_database_url: str,
    test_database_url: str,
    postgres_app_role_environ: dict[str, str],
    mock_idp_client: httpx.AsyncClient,
) -> None:
    admin_oid = "oid-admin-erase-wrong-confirm"
    namespace = "group-survives"

    side_pool = await asyncpg.create_pool(test_database_url)
    try:
        authenticator = _entra_authenticator(mock_idp_client)
        config = _config()
        async with _running_app(postgres_app_role_environ, config, authenticator=authenticator) as (
            _app,
            client,
        ):
            await _login_as(client, mock_idp_client, oid=admin_oid, roles=[_MEMORY_ADMIN])

            await _seed_note(side_pool, namespace=namespace, slug="untouched")

            page_response = await client.get(PATH)
            token = _erase_csrf_token(page_response.text)

            response = await client.post(
                ERASE_PATH,
                data={
                    CSRF_FIELD_NAME: token,
                    "target_kind": "namespace",
                    "target": namespace,
                    CONFIRM_FIELD_NAME: "not-" + namespace,
                    "reason": "should not apply",
                },
            )
            assert response.status_code == 400, response.text

        remaining = await side_pool.fetchval(
            "select count(*) from vault_notes where namespace = $1", namespace
        )
        assert remaining == 1
    finally:
        await side_pool.close()


async def test_erase_without_a_reason_is_rejected(
    admin_database_url: str,
    test_database_url: str,
    postgres_app_role_environ: dict[str, str],
    mock_idp_client: httpx.AsyncClient,
) -> None:
    admin_oid = "oid-admin-erase-no-reason"
    namespace = "group-also-survives"

    side_pool = await asyncpg.create_pool(test_database_url)
    try:
        authenticator = _entra_authenticator(mock_idp_client)
        config = _config()
        async with _running_app(postgres_app_role_environ, config, authenticator=authenticator) as (
            _app,
            client,
        ):
            await _login_as(client, mock_idp_client, oid=admin_oid, roles=[_MEMORY_ADMIN])

            await _seed_note(side_pool, namespace=namespace, slug="untouched")

            page_response = await client.get(PATH)
            token = _erase_csrf_token(page_response.text)

            response = await client.post(
                ERASE_PATH,
                data={
                    CSRF_FIELD_NAME: token,
                    "target_kind": "namespace",
                    "target": namespace,
                    CONFIRM_FIELD_NAME: namespace,
                    "reason": "",
                },
            )
            assert response.status_code == 400, response.text

        remaining = await side_pool.fetchval(
            "select count(*) from vault_notes where namespace = $1", namespace
        )
        assert remaining == 1
    finally:
        await side_pool.close()


async def test_erase_note_at_an_unknown_path_is_a_404(
    admin_database_url: str,
    test_database_url: str,
    postgres_app_role_environ: dict[str, str],
    mock_idp_client: httpx.AsyncClient,
) -> None:
    admin_oid = "oid-admin-erase-unknown-note"

    authenticator = _entra_authenticator(mock_idp_client)
    config = _config()
    async with _running_app(postgres_app_role_environ, config, authenticator=authenticator) as (
        _app,
        client,
    ):
        await _login_as(client, mock_idp_client, oid=admin_oid, roles=[_MEMORY_ADMIN])

        page_response = await client.get(PATH)
        token = _erase_csrf_token(page_response.text)

        response = await client.post(
            ERASE_PATH,
            data={
                CSRF_FIELD_NAME: token,
                "target_kind": "note",
                "target": "does-not/exist/anywhere.md",
                CONFIRM_FIELD_NAME: "does-not/exist/anywhere.md",
                "reason": "cleanup",
            },
        )
        assert response.status_code == 404, response.text
