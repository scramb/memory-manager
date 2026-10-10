# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for `/account` (#229, ADR-0008 addendum 2026-10-08): the page shell, login
and logout in every embedded-AS login mode, on both storage backends.

Login itself is driven exactly the way `tests/auth/test_login.py`/`tests/auth/
test_login_entra.py` already drive `/authorize` -> `/login` -> `{CALLBACK_PATH}`,
just starting from `GET /account/login` instead of `GET /authorize` - see
`memory_manager.account.routes`'s own module docstring for why the three real
`Authenticator`s need no change at all to make that work.
"""

from __future__ import annotations

import html
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from mock_idp_fixtures import mock_idp_client
from starlette.applications import Starlette

from memory_manager.account.routes import LOGIN_PATH_START, LOGOUT_PATH, PATH, SESSION_COOKIE
from memory_manager.account.sessions import LOGIN_MODES
from memory_manager.account.templates import CSRF_FIELD_NAME
from memory_manager.app import open_services
from memory_manager.auth.login import LOGIN_PATH
from memory_manager.auth.login_entra import CALLBACK_PATH as ENTRA_CALLBACK_PATH
from memory_manager.auth.login_entra import EntraAuthenticator
from memory_manager.auth.login_oidc import CALLBACK_PATH as OIDC_CALLBACK_PATH
from memory_manager.auth.login_oidc import OidcAuthenticator
from memory_manager.auth.login_password import PasswordAuthenticator, hash_password
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


def _location(response: httpx.Response) -> str:
    location = response.headers.get("location")
    assert location is not None, f"expected a redirect, got {response.status_code} {response.text}"
    return str(location)


def _pending_id_from_password_page(response: httpx.Response) -> str:
    marker = 'name="pending" value="'
    start = response.text.index(marker) + len(marker)
    end = response.text.index('"', start)
    return response.text[start:end]


def _continue_url_from_interstitial(response: httpx.Response) -> str:
    marker = 'class="button" href="'
    start = response.text.index(marker) + len(marker)
    end = response.text.index('"', start)
    return html.unescape(response.text[start:end])


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


async def _start_account_login(client: httpx.AsyncClient) -> str:
    """`GET /account/login` -> 302 to `{LOGIN_PATH}?pending=...`; returns that URL."""
    start_response = await client.get(LOGIN_PATH_START)
    assert start_response.status_code == 302, start_response.text
    login_url = _location(start_response)
    assert urlsplit(login_url).path == LOGIN_PATH
    return login_url


async def _login_with_password(client: httpx.AsyncClient) -> httpx.Response:
    login_url = await _start_account_login(client)
    login_response = await client.get(login_url)
    assert login_response.status_code == 200, login_response.text
    pending_id = _pending_id_from_password_page(login_response)
    return await client.post(LOGIN_PATH, data={"pending": pending_id, "password": _ADMIN_PASSWORD})


async def _login_with_oidc(
    client: httpx.AsyncClient, fake_oidc_provider: Any, *, claims: dict[str, Any]
) -> httpx.Response:
    login_url = await _start_account_login(client)
    interstitial_response = await client.get(login_url)
    assert interstitial_response.status_code == 200, interstitial_response.text
    continue_response = await client.get(_continue_url_from_interstitial(interstitial_response))
    assert continue_response.status_code == 302, continue_response.text
    upstream_url = _location(continue_response)
    state = _query(upstream_url)["state"][0]
    code = fake_oidc_provider.issue_code(claims)
    return await client.get(OIDC_CALLBACK_PATH, params={"state": state, "code": code})


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


def _assert_session_cookie(response: httpx.Response) -> str:
    """The `Set-Cookie` header's raw value, asserted to carry every attribute ADR-0008's
    addendum 2026-10-08 specifies - returns it so a caller can also check the cookie
    name/value."""
    maybe_raw = response.headers.get("set-cookie")
    assert maybe_raw is not None, f"expected a Set-Cookie header, got {response.headers}"
    raw = str(maybe_raw)
    assert raw.startswith(f"{SESSION_COOKIE}=")
    lowered = raw.lower()
    assert "httponly" in lowered
    assert "secure" in lowered
    assert "samesite=strict" in lowered
    assert f"path={PATH.lower()}" in lowered
    return raw


# === Password mode ==================================================================


async def test_password_login_sets_a_session_cookie_and_renders_the_page(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    config = _config()
    environ = _git_environ(bare_remote, tmp_path, test_database_url)
    authenticator = PasswordAuthenticator(
        password_hash=hash_password(_ADMIN_PASSWORD), namespaces=["*"]
    )
    async with _running_app(environ, config, authenticator=authenticator) as (_app, client):
        login_response = await _login_with_password(client)
        assert login_response.status_code == 302, login_response.text
        assert _location(login_response) == PATH
        _assert_session_cookie(login_response)

        page_response = await client.get(PATH)
        assert page_response.status_code == 200, page_response.text
        assert "Your account" in page_response.text
        assert "admin" in page_response.text
        assert "password" in page_response.text


async def test_account_without_a_session_redirects_to_login(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    config = _config()
    environ = _git_environ(bare_remote, tmp_path, test_database_url)
    authenticator = PasswordAuthenticator(
        password_hash=hash_password(_ADMIN_PASSWORD), namespaces=["*"]
    )
    async with _running_app(environ, config, authenticator=authenticator) as (_app, client):
        response = await client.get(PATH)
        assert response.status_code == 302
        assert _location(response) == LOGIN_PATH_START


# === OIDC mode =======================================================================


async def test_oidc_login_sets_a_session_cookie_and_renders_the_page(
    bare_remote: Path, tmp_path: Path, test_database_url: str, fake_oidc_provider: Any
) -> None:
    config = _config()
    environ = _git_environ(bare_remote, tmp_path, test_database_url)
    authenticator = OidcAuthenticator(
        issuer=fake_oidc_provider.issuer,
        client_id=fake_oidc_provider.client_id,
        client_secret=fake_oidc_provider.client_secret,
        redirect_uri=f"{_PUBLIC_URL}{OIDC_CALLBACK_PATH}",
        allowed_emails=frozenset(),
        allowed_subjects=frozenset({"alice-sub"}),
        namespaces=["*"],
        namespace_map={},
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(fake_oidc_provider.handler)),
    )
    async with _running_app(environ, config, authenticator=authenticator) as (_app, client):
        callback_response = await _login_with_oidc(
            client,
            fake_oidc_provider,
            claims={"sub": "alice-sub", "email": "alice@example.test", "email_verified": True},
        )
        assert callback_response.status_code == 302, callback_response.text
        assert _location(callback_response) == PATH
        _assert_session_cookie(callback_response)

        page_response = await client.get(PATH)
        assert page_response.status_code == 200, page_response.text
        assert "alice-sub" in page_response.text
        assert "oidc" in page_response.text


# === Entra mode ======================================================================


async def test_entra_login_on_git_backend_shows_no_postgres_only_section(
    bare_remote: Path, tmp_path: Path, test_database_url: str, mock_idp_client: httpx.AsyncClient
) -> None:
    """`STORAGE_BACKEND=git` (the default) plus a real Entra `oid`: the overview
    section's note-count row - the one piece of this page that needs
    `STORAGE_BACKEND=postgres` - must not appear, even though a real principal with a
    real `oid` signed in (ADR-0008 addendum: "sections that need the Postgres backend
    ... appear only with STORAGE_BACKEND=postgres")."""
    config = _config()
    environ = _git_environ(bare_remote, tmp_path, test_database_url)
    oid = "oid-git-backend"
    await _register_entra_identity(mock_idp_client, oid)
    authenticator = _entra_authenticator(mock_idp_client)
    async with _running_app(environ, config, authenticator=authenticator) as (_app, client):
        callback_response = await _login_with_entra(client, mock_idp_client)
        assert callback_response.status_code == 302, callback_response.text
        assert _location(callback_response) == PATH
        _assert_session_cookie(callback_response)

        page_response = await client.get(PATH)
        assert page_response.status_code == 200, page_response.text
        assert "entra" in page_response.text
        assert "Notes in your personal namespace" not in page_response.text


async def test_entra_login_on_postgres_backend_shows_the_note_count(
    admin_database_url: str,
    test_database_url: str,
    postgres_app_role_environ: dict[str, str],
    mock_idp_client: httpx.AsyncClient,
) -> None:
    """The same login, on `STORAGE_BACKEND=postgres`: the note-count row now appears
    (zero notes so far, but present at all is the point)."""
    config = _config()
    oid = "oid-postgres-backend"
    await _register_entra_identity(mock_idp_client, oid)
    authenticator = _entra_authenticator(mock_idp_client)
    async with _running_app(postgres_app_role_environ, config, authenticator=authenticator) as (
        _app,
        client,
    ):
        callback_response = await _login_with_entra(client, mock_idp_client)
        assert callback_response.status_code == 302, callback_response.text

        page_response = await client.get(PATH)
        assert page_response.status_code == 200, page_response.text
        assert "Notes in your personal namespace" in page_response.text


# === Logout ==========================================================================


async def test_logout_without_csrf_token_is_rejected(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    config = _config()
    environ = _git_environ(bare_remote, tmp_path, test_database_url)
    authenticator = PasswordAuthenticator(
        password_hash=hash_password(_ADMIN_PASSWORD), namespaces=["*"]
    )
    async with _running_app(environ, config, authenticator=authenticator) as (_app, client):
        await _login_with_password(client)

        response = await client.post(LOGOUT_PATH, data={})
        assert response.status_code == 403, response.text

        # The session must still be usable - a rejected logout never revokes it.
        page_response = await client.get(PATH)
        assert page_response.status_code == 200


async def test_logout_with_a_valid_csrf_token_revokes_the_session(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    config = _config()
    environ = _git_environ(bare_remote, tmp_path, test_database_url)
    authenticator = PasswordAuthenticator(
        password_hash=hash_password(_ADMIN_PASSWORD), namespaces=["*"]
    )
    async with _running_app(environ, config, authenticator=authenticator) as (_app, client):
        await _login_with_password(client)
        page_response = await client.get(PATH)
        # Scoped to the logout form's own action (#135 added a second form - the
        # token section's create form - ahead of it on the page, so a bare, unscoped
        # search for the first `CSRF_FIELD_NAME` field would grab that one's token
        # instead; `tests/account/test_export.py`'s own `_export_csrf_token` scopes
        # the identical way, for the identical reason).
        form_marker = f'action="{html.escape(LOGOUT_PATH)}"'
        form_start = page_response.text.index(form_marker)
        marker = f'name="{CSRF_FIELD_NAME}" value="'
        start = page_response.text.index(marker, form_start) + len(marker)
        end = page_response.text.index('"', start)
        csrf_token = html.unescape(page_response.text[start:end])

        logout_response = await client.post(LOGOUT_PATH, data={CSRF_FIELD_NAME: csrf_token})
        assert logout_response.status_code == 302, logout_response.text
        assert _location(logout_response) == LOGIN_PATH_START

        after_logout = await client.get(PATH)
        assert after_logout.status_code == 302
        assert _location(after_logout) == LOGIN_PATH_START


# === Cross-instance session =========================================================


async def test_a_session_created_on_one_instance_works_on_another(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    """Two separate `create_app` instances sharing the same database: a session
    minted on the first is usable on the second - `account.sessions` is pure Postgres
    state, nothing process-local (ADR-0008 addendum: revocable, shared state)."""
    config = _config()
    environ = _git_environ(bare_remote, tmp_path, test_database_url)

    def authenticator() -> PasswordAuthenticator:
        return PasswordAuthenticator(password_hash=hash_password(_ADMIN_PASSWORD), namespaces=["*"])

    async with (
        _running_app(environ, config, authenticator=authenticator()) as (_app1, client1),
        _running_app(environ, config, authenticator=authenticator()) as (_app2, client2),
    ):
        await _login_with_password(client1)
        session_id = client1.cookies.get(SESSION_COOKIE)
        assert session_id is not None

        client2.cookies.set(SESSION_COOKIE, session_id)
        response = await client2.get(PATH)
        assert response.status_code == 200, response.text
        assert "Your account" in response.text


def test_login_modes_cover_every_real_authenticator() -> None:
    """A guard against `account.sessions.LOGIN_MODES` drifting from the three real
    `Authenticator`s this file drives - not itself an HTTP test."""
    assert set(LOGIN_MODES) == {"password", "oidc", "entra"}
