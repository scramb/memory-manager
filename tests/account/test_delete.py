# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for `POST /account/delete` (#232, ADR-0008 "Self-service").

Login is driven exactly the way `tests/account/test_export.py` drives Entra
login - that module's own docstring explains the `/account/login` ->
`/login` -> `{CALLBACK_PATH}` shape and why its helpers are a trimmed copy
rather than a cross-module import (`tests/account/conftest.py`'s own
docstring: every package under `tests/` that needs a given shape defines
its own).

Notes are seeded across every table a real write touches
(`vault_notes`/`vault_revisions`/`notes`/`chunks`/`links`), the same shape
`tests/storage/test_erasure.py`'s own `_seed_note` uses - directly against
the owner connection, bypassing RLS entirely (that module's own assumption).
"""

from __future__ import annotations

import html
import json
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

from memory_manager.account.delete import (
    CONFIRM_FIELD_NAME,
    CONFIRM_PHRASE,
    CSRF_FORM_DELETE,
    DELETE_PATH,
)
from memory_manager.account.routes import LOGIN_PATH_START, PATH
from memory_manager.account.sessions import csrf_token
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
    function carries (see `tests/account/test_export.py`'s own copy)."""
    conn = await asyncpg.connect(database_url)
    try:
        async with conn.transaction():
            await conn.execute("select set_config('app.oid', $1, true)", oid)
            alias = await conn.fetchval("select mm_ensure_personal_ns()")
    finally:
        await conn.close()
    assert alias is not None
    return str(alias)


async def _seed_note(database_url: str, *, namespace: str, slug: str) -> str:
    """One note across `vault_notes`/`vault_revisions`/`notes`/`chunks`/`links` -
    the same tables `tests/storage/test_erasure.py`'s own `_seed_note` touches,
    checked here to be empty for `namespace` after a successful delete."""
    note_id = f"note-{namespace}-{slug}"
    path = f"{namespace}/fact/{slug}.md"
    content = f"Body for {slug}.\n".encode()
    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute(
            "insert into vault_notes (id, namespace, path, content, version, current_revision) "
            "values ($1, $2, $3, $4, 'v1', 1)",
            note_id,
            namespace,
            path,
            content,
        )
        await conn.execute(
            "insert into vault_revisions "
            "(note_id, revision, path, content, version, author, client, author_oid) "
            "values ($1, 1, $2, $3, 'v1', 'tester', 'pytest', null)",
            note_id,
            path,
            content,
        )
        await conn.execute(
            "insert into notes "
            "(id, path, namespace, type, slug, title, description, tags, created, updated, "
            "file_hash) "
            "values ($1, $2, $3, 'fact', $4, $5, 'description', $6, $7, $7, 'deadbeef')",
            note_id,
            path,
            namespace,
            slug,
            slug.replace("-", " ").title(),
            [],
            _SEED_NOW,
        )
        await conn.execute(
            "insert into chunks (note_id, namespace, namespace_kind, ord, text) "
            "values ($1, $2, 'user', 0, $3)",
            note_id,
            namespace,
            f"chunk for {slug}",
        )
        await conn.execute(
            "insert into links (source_id, target_raw) values ($1, 'nowhere')", note_id
        )
    finally:
        await conn.close()
    return note_id


async def _row_counts(database_url: str, note_id: str) -> dict[str, int]:
    conn = await asyncpg.connect(database_url)
    try:
        counts = await conn.fetchrow(
            """
            select
                (select count(*) from vault_notes where id = $1) as vault_notes,
                (select count(*) from vault_revisions where note_id = $1) as vault_revisions,
                (select count(*) from notes where id = $1) as notes,
                (select count(*) from chunks where note_id = $1) as chunks,
                (select count(*) from links where source_id = $1) as links
            """,
            note_id,
        )
    finally:
        await conn.close()
    assert counts is not None
    return dict(counts)


def _delete_csrf_token(page_html: str) -> str:
    form_marker = f'action="{html.escape(DELETE_PATH)}"'
    form_start = page_html.index(form_marker)
    marker = f'name="{CSRF_FIELD_NAME}" value="'
    start = page_html.index(marker, form_start) + len(marker)
    end = page_html.index('"', start)
    return html.unescape(page_html[start:end])


async def test_delete_with_wrong_phrase_deletes_nothing(
    admin_database_url: str,
    test_database_url: str,
    postgres_app_role_environ: dict[str, str],
    mock_idp_client: httpx.AsyncClient,
) -> None:
    oid = "oid-delete-wrong-phrase"
    await _register_entra_identity(mock_idp_client, oid)
    authenticator = _entra_authenticator(mock_idp_client)

    config = _config()
    async with _running_app(postgres_app_role_environ, config, authenticator=authenticator) as (
        _app,
        client,
    ):
        own_alias = await _personal_alias(test_database_url, oid)
        note_id = await _seed_note(test_database_url, namespace=own_alias, slug="keep-me")

        await _login_with_entra(client, mock_idp_client)
        page_response = await client.get(PATH)
        token = _delete_csrf_token(page_response.text)

        response = await client.post(
            DELETE_PATH,
            data={CSRF_FIELD_NAME: token, CONFIRM_FIELD_NAME: "not the phrase"},
        )
        assert response.status_code == 400, response.text

    counts = await _row_counts(test_database_url, note_id)
    assert all(count == 1 for count in counts.values()), counts


async def test_delete_with_correct_phrase_erases_only_the_callers_own_namespace(
    admin_database_url: str,
    test_database_url: str,
    postgres_app_role_environ: dict[str, str],
    mock_idp_client: httpx.AsyncClient,
) -> None:
    oid = "oid-delete-own"
    other_oid = "oid-delete-other"
    await _register_entra_identity(mock_idp_client, oid)
    authenticator = _entra_authenticator(mock_idp_client)

    config = _config()
    async with _running_app(postgres_app_role_environ, config, authenticator=authenticator) as (
        _app,
        client,
    ):
        own_alias = await _personal_alias(test_database_url, oid)
        other_alias = await _personal_alias(test_database_url, other_oid)
        own_note_id = await _seed_note(test_database_url, namespace=own_alias, slug="erase-me")
        other_note_id = await _seed_note(test_database_url, namespace=other_alias, slug="leave-me")

        await _login_with_entra(client, mock_idp_client)
        page_response = await client.get(PATH)
        token = _delete_csrf_token(page_response.text)

        response = await client.post(
            DELETE_PATH,
            data={CSRF_FIELD_NAME: token, CONFIRM_FIELD_NAME: CONFIRM_PHRASE},
        )
        assert response.status_code == 302, response.text
        assert _location(response) == PATH

        page_after = await client.get(PATH)
        assert page_after.status_code == 200, page_after.text
        assert "Notes in your personal namespace</dt><dd>0</dd>" in page_after.text

    own_counts = await _row_counts(test_database_url, own_note_id)
    assert own_counts == {
        "vault_notes": 0,
        "vault_revisions": 0,
        "notes": 0,
        "chunks": 0,
        "links": 0,
    }

    other_counts = await _row_counts(test_database_url, other_note_id)
    assert other_counts == {
        "vault_notes": 1,
        "vault_revisions": 1,
        "notes": 1,
        "chunks": 1,
        "links": 1,
    }


async def test_delete_without_csrf_token_is_rejected(
    admin_database_url: str,
    test_database_url: str,
    postgres_app_role_environ: dict[str, str],
    mock_idp_client: httpx.AsyncClient,
) -> None:
    oid = "oid-delete-no-csrf"
    await _register_entra_identity(mock_idp_client, oid)
    authenticator = _entra_authenticator(mock_idp_client)
    config = _config()
    async with _running_app(postgres_app_role_environ, config, authenticator=authenticator) as (
        _app,
        client,
    ):
        await _login_with_entra(client, mock_idp_client)

        response = await client.post(DELETE_PATH, data={CONFIRM_FIELD_NAME: CONFIRM_PHRASE})
        assert response.status_code == 403, response.text


async def test_delete_section_absent_with_git_backend(
    bare_remote: Path, tmp_path: Path, test_database_url: str, mock_idp_client: httpx.AsyncClient
) -> None:
    oid = "oid-delete-git-backend"
    await _register_entra_identity(mock_idp_client, oid)
    authenticator = _entra_authenticator(mock_idp_client)
    config = _config()
    environ = _git_environ(bare_remote, tmp_path, test_database_url)
    async with _running_app(environ, config, authenticator=authenticator) as (_app, client):
        await _login_with_entra(client, mock_idp_client)

        page_response = await client.get(PATH)
        assert page_response.status_code == 200, page_response.text
        assert "Delete my memory" not in page_response.text

        session_id = client.cookies.get("mm_session")
        assert session_id is not None
        token = csrf_token(session_id, CSRF_FORM_DELETE)
        response = await client.post(
            DELETE_PATH, data={CSRF_FIELD_NAME: token, CONFIRM_FIELD_NAME: CONFIRM_PHRASE}
        )
        assert response.status_code == 403, response.text


async def test_delete_writes_an_erasure_log_row_without_note_content(
    admin_database_url: str,
    test_database_url: str,
    postgres_app_role_environ: dict[str, str],
    mock_idp_client: httpx.AsyncClient,
) -> None:
    oid = "oid-delete-audit"
    await _register_entra_identity(mock_idp_client, oid)
    authenticator = _entra_authenticator(mock_idp_client)

    config = _config()
    async with _running_app(postgres_app_role_environ, config, authenticator=authenticator) as (
        _app,
        client,
    ):
        own_alias = await _personal_alias(test_database_url, oid)
        await _seed_note(test_database_url, namespace=own_alias, slug="audited")

        await _login_with_entra(client, mock_idp_client)
        page_response = await client.get(PATH)
        token = _delete_csrf_token(page_response.text)

        response = await client.post(
            DELETE_PATH,
            data={CSRF_FIELD_NAME: token, CONFIRM_FIELD_NAME: CONFIRM_PHRASE},
        )
        assert response.status_code == 302, response.text

    conn = await asyncpg.connect(test_database_url)
    try:
        log_row = await conn.fetchrow(
            "select actor, reason, target_kind, target_ids, row_counts from erasure_log "
            "where actor = $1",
            oid,
        )
        audit_row = await conn.fetchrow(
            "select path, detail from audit_log where client = 'erasure' and actor = $1", oid
        )
    finally:
        await conn.close()

    assert log_row is not None
    assert log_row["reason"] == "self-service"
    assert log_row["target_kind"] == "namespace"
    assert log_row["target_ids"] == [own_alias]
    row_counts = json.loads(log_row["row_counts"])
    assert row_counts["vault_notes"] == 1
    assert "audited" not in json.dumps(row_counts)

    assert audit_row is not None
    assert audit_row["path"] is None
    assert "audited" not in audit_row["detail"]
