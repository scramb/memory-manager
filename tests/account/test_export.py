# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for `POST /account/export` (#230, ADR-0008 "Self-service").

Login is driven exactly the way `tests/account/test_page.py` drives Entra login -
that module's own docstring explains the `/account/login` -> `/login` ->
`{CALLBACK_PATH}` shape; the helpers below are a trimmed copy rather than a
cross-module import (`tests/account/conftest.py`'s own docstring: every
package under `tests/` that needs a given shape defines its own).

Notes are seeded directly against `vault_notes`, as the owner connection
(superuser locally, same assumption `tests/mcp/test_permission_matrix.py`'s
own module docstring states) - bypassing RLS entirely, the same shape that
module's `_seed_note` uses, rather than going through a write path this
package has no fixture for.
"""

from __future__ import annotations

import html
import io
import json
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import asyncpg
import httpx
from mock_idp_fixtures import mock_idp_client
from starlette.applications import Starlette

from memory_manager.account.export import CSRF_FORM_EXPORT, EXPORT_PATH
from memory_manager.account.routes import LOGIN_PATH_START, PATH, SESSION_COOKIE
from memory_manager.account.sessions import csrf_token
from memory_manager.account.templates import CSRF_FIELD_NAME
from memory_manager.app import open_services
from memory_manager.auth.login_entra import CALLBACK_PATH as ENTRA_CALLBACK_PATH
from memory_manager.auth.login_entra import EntraAuthenticator
from memory_manager.config import ServerConfig
from memory_manager.http import create_app
from memory_manager.vault.note import Note, serialize
from memory_manager.vault.note import version as note_version
from memory_manager.vault.ulid import new_ulid

__all__ = ["mock_idp_client"]

_PUBLIC_URL = "https://mm.example.test"
_CLIENT_SECRET_KEY = "f2F7cFcySJJZnWoa6ZFYlsU-MQSxl8_Fw6bT_DLoLJ0="  # noqa: S105 - fake test key

_TID = "33333333-3333-3333-3333-333333333333"
_ENTRA_CLIENT_ID = "44444444-4444-4444-4444-444444444444"
_ENTRA_CLIENT_SECRET = "mock-entra-client-secret"  # noqa: S105 - a fake test credential
_ENTRA_AUTHORITY = "https://mock-idp.test"
_ENTRA_GRAPH_URL = "https://mock-idp.test/graph/v1.0"

_SEED_NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


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


async def _personal_alias(database_url: str, oid: str) -> str:
    """`oid`'s personal namespace alias, lazily created the same way
    `mm_ensure_personal_ns()` always is - run directly on the owner connection
    (superuser locally), which bypasses the `revoke execute ... from public` that
    function carries (see this module's own docstring)."""
    conn = await asyncpg.connect(database_url)
    try:
        async with conn.transaction():
            await conn.execute("select set_config('app.oid', $1, true)", oid)
            alias = await conn.fetchval("select mm_ensure_personal_ns()")
    finally:
        await conn.close()
    assert alias is not None
    return str(alias)


def _note_bytes(slug: str) -> bytes:
    note = Note(
        id=new_ulid(_SEED_NOW),
        title=slug.replace("-", " ").title(),
        description=f"{slug} description.",
        type="fact",
        created=_SEED_NOW,
        updated=_SEED_NOW,
        body="Body.\n",
    )
    return serialize(note)


async def _seed_note(
    database_url: str, *, namespace: str, type_: str, slug: str, archived: bool = False
) -> bytes:
    content = _note_bytes(slug)
    prefix = f"_archive/{namespace}" if archived else namespace
    path = f"{prefix}/{type_}/{slug}.md"
    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute(
            "insert into vault_notes (id, namespace, path, content, version, current_revision) "
            "values ($1, $2, $3, $4, $5, 1)",
            new_ulid(_SEED_NOW),
            namespace,
            path,
            content,
            note_version(content),
        )
    finally:
        await conn.close()
    return content


def _export_csrf_token(page_html: str) -> str:
    form_marker = f'action="{html.escape(EXPORT_PATH)}"'
    form_start = page_html.index(form_marker)
    marker = f'name="{CSRF_FIELD_NAME}" value="'
    start = page_html.index(marker, form_start) + len(marker)
    end = page_html.index('"', start)
    return html.unescape(page_html[start:end])


def _open_zip(content: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(content))


async def test_export_zip_holds_only_the_callers_own_notes_including_archive(
    admin_database_url: str,
    test_database_url: str,
    postgres_app_role_environ: dict[str, str],
    mock_idp_client: httpx.AsyncClient,
) -> None:
    oid = "oid-export-own"
    other_oid = "oid-export-other"
    await _register_entra_identity(mock_idp_client, oid)
    authenticator = _entra_authenticator(mock_idp_client)

    config = _config()
    async with _running_app(postgres_app_role_environ, config, authenticator=authenticator) as (
        _app,
        client,
    ):
        # The schema only exists once `open_services` has migrated it, which
        # happens inside `_running_app`'s own lifespan - seeding has to wait
        # until this point is reached, not run before entering it.
        own_alias = await _personal_alias(test_database_url, oid)
        other_alias = await _personal_alias(test_database_url, other_oid)
        live_content = await _seed_note(
            test_database_url, namespace=own_alias, type_="fact", slug="own-live"
        )
        archived_content = await _seed_note(
            test_database_url, namespace=own_alias, type_="fact", slug="own-archived", archived=True
        )
        await _seed_note(test_database_url, namespace=other_alias, type_="fact", slug="other-live")

        login_response = await _login_with_entra(client, mock_idp_client)
        assert login_response.status_code == 302, login_response.text

        page_response = await client.get(PATH)
        assert page_response.status_code == 200, page_response.text
        token = _export_csrf_token(page_response.text)

        export_response = await client.post(EXPORT_PATH, data={CSRF_FIELD_NAME: token})
        assert export_response.status_code == 200, export_response.text
        assert export_response.headers["content-type"] == "application/zip"

    with _open_zip(export_response.content) as zf:
        names = set(zf.namelist())
        assert "manifest.json" in names
        assert "vault/me/fact/own-live.md" in names
        assert "vault/_archive/me/fact/own-archived.md" in names
        assert not any("other-live" in name for name in names)
        assert not any(own_alias in name or other_alias in name for name in names)

        assert zf.read("vault/me/fact/own-live.md") == live_content
        assert zf.read("vault/_archive/me/fact/own-archived.md") == archived_content

        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["note_count"] == 2
        paths = {entry["path"] for entry in manifest["notes"]}
        assert paths == {"me/fact/own-live.md", "_archive/me/fact/own-archived.md"}
        for entry in manifest["notes"]:
            assert entry["namespace"] == "me"
            content = live_content if entry["path"] == "me/fact/own-live.md" else archived_content
            assert entry["sha256"] == note_version(content)
            assert entry["bytes"] == len(content)


async def test_export_without_csrf_token_is_rejected(
    admin_database_url: str,
    test_database_url: str,
    postgres_app_role_environ: dict[str, str],
    mock_idp_client: httpx.AsyncClient,
) -> None:
    oid = "oid-export-no-csrf"
    await _register_entra_identity(mock_idp_client, oid)
    authenticator = _entra_authenticator(mock_idp_client)
    config = _config()
    async with _running_app(postgres_app_role_environ, config, authenticator=authenticator) as (
        _app,
        client,
    ):
        await _login_with_entra(client, mock_idp_client)

        response = await client.post(EXPORT_PATH, data={})
        assert response.status_code == 403, response.text


async def test_export_section_absent_with_git_backend(
    bare_remote: Path, tmp_path: Path, test_database_url: str, mock_idp_client: httpx.AsyncClient
) -> None:
    oid = "oid-export-git-backend"
    await _register_entra_identity(mock_idp_client, oid)
    authenticator = _entra_authenticator(mock_idp_client)
    config = _config()
    environ = _git_environ(bare_remote, tmp_path, test_database_url)
    async with _running_app(environ, config, authenticator=authenticator) as (_app, client):
        await _login_with_entra(client, mock_idp_client)

        page_response = await client.get(PATH)
        assert page_response.status_code == 200, page_response.text
        assert "Download my memory" not in page_response.text

        session_id = client.cookies.get(SESSION_COOKIE)
        assert session_id is not None
        token = csrf_token(session_id, CSRF_FORM_EXPORT)
        export_response = await client.post(EXPORT_PATH, data={CSRF_FIELD_NAME: token})
        assert export_response.status_code == 403, export_response.text


async def test_export_writes_an_audit_row(
    admin_database_url: str,
    test_database_url: str,
    postgres_app_role_environ: dict[str, str],
    mock_idp_client: httpx.AsyncClient,
) -> None:
    oid = "oid-export-audit"
    await _register_entra_identity(mock_idp_client, oid)
    authenticator = _entra_authenticator(mock_idp_client)

    config = _config()
    async with _running_app(postgres_app_role_environ, config, authenticator=authenticator) as (
        _app,
        client,
    ):
        own_alias = await _personal_alias(test_database_url, oid)
        await _seed_note(test_database_url, namespace=own_alias, type_="fact", slug="audited")

        await _login_with_entra(client, mock_idp_client)
        page_response = await client.get(PATH)
        token = _export_csrf_token(page_response.text)

        export_response = await client.post(EXPORT_PATH, data={CSRF_FIELD_NAME: token})
        assert export_response.status_code == 200, export_response.text

    conn = await asyncpg.connect(test_database_url)
    try:
        row = await conn.fetchrow(
            "select actor, client, op, detail from audit_log where op = 'account.export'"
        )
    finally:
        await conn.close()
    assert row is not None
    assert row["actor"] == oid
    assert row["client"] == "account"
    detail = json.loads(row["detail"])
    assert detail == {"note_count": 1}
