# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the real `/login` methods (ADR-0004 L1/L2, #37): a single admin
password (`auth.login_password.PasswordAuthenticator`) and upstream OIDC
(`auth.login_oidc.OidcAuthenticator`), plus the shared namespace-resolution
helpers in `auth.login` both of them use.

`tests/auth/test_oauth_flow.py` already exercises the embedded OAuth
authorization server generically through `FakeAuthenticator` - this file
drives the same real HTTP surface (DCR, `/authorize`, `/login`,
`{CALLBACK_PATH}`, `/token`, `tools/call`) with the two real `Authenticator`s
instead, plus the construction-time (`ServerConfigError`) checks neither one
lets a misconfigured deployment past.
"""

from __future__ import annotations

import base64
import hashlib
import html
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlsplit

import asyncpg
import httpx
import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette

from memory_manager.app import open_services
from memory_manager.auth import store
from memory_manager.auth.login import (
    LOGIN_PATH,
    parse_namespace_map,
    parse_namespaces,
    resolve_namespaces,
)
from memory_manager.auth.login_oidc import CALLBACK_PATH, OidcAuthenticator
from memory_manager.auth.login_password import PasswordAuthenticator, hash_password
from memory_manager.cli import main as cli_main
from memory_manager.config import ServerConfig, ServerConfigError
from memory_manager.http import create_app

_PUBLIC_URL = "https://mm.example.test"
_MCP_PATH = "/mcp"
_RESOURCE = f"{_PUBLIC_URL}{_MCP_PATH}"
_REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"
_ADMIN_PASSWORD = "correct horse battery staple"  # noqa: S105 - a fake test credential

#: A Fernet key generated once for this test module only - not a credential
#: protecting anything real, just what `OAUTH_CLIENT_SECRET_KEY` requires.
_CLIENT_SECRET_KEY = "f2F7cFcySJJZnWoa6ZFYlsU-MQSxl8_Fw6bT_DLoLJ0="  # noqa: S105


def _config() -> ServerConfig:
    return ServerConfig(
        public_url=_PUBLIC_URL, mcp_path=_MCP_PATH, oauth_client_secret_key=_CLIENT_SECRET_KEY
    )


def _environ(bare_remote: Path, tmp_path: Path, database_url: str) -> dict[str, str]:
    return {
        "VAULT_REMOTE": str(bare_remote),
        "VAULT_DIR": str(tmp_path / "vault"),
        "DATABASE_URL": database_url,
    }


@asynccontextmanager
async def _running_app(
    environ: dict[str, str], config: ServerConfig, *, authenticator: Any
) -> AsyncIterator[tuple[Starlette, httpx.AsyncClient]]:
    app = create_app(lambda: open_services(environ), config, authenticator=authenticator)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", follow_redirects=False
        ) as client:
            yield app, client


def _authed_mcp_client(app: Starlette, config: ServerConfig, token: str) -> Client:
    http_client = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {token}"},
    )
    transport = streamable_http_client(
        f"http://testserver{config.mcp_path}", http_client=http_client
    )
    return Client(transport, mode="legacy")


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


async def _register_client(
    client: httpx.AsyncClient, *, redirect_uri: str = _REDIRECT_URI, client_name: str | None = None
) -> str:
    payload: dict[str, Any] = {
        "redirect_uris": [redirect_uri],
        "token_endpoint_auth_method": "none",
    }
    if client_name is not None:
        payload["client_name"] = client_name
    response = await client.post("/register", json=payload)
    assert response.status_code == 201, response.text
    client_id = response.json()["client_id"]
    assert isinstance(client_id, str)
    return client_id


def _continue_url_from(response: httpx.Response) -> str:
    """The `oidc_interstitial_page`'s "Continue to sign in" link target."""
    marker = 'class="button" href="'
    start = response.text.index(marker) + len(marker)
    end = response.text.index('"', start)
    return html.unescape(response.text[start:end])


def _location(response: httpx.Response) -> str:
    location = response.headers.get("location")
    assert location is not None, f"expected a redirect, got {response.status_code} {response.text}"
    return str(location)


async def _start_authorize(
    client: httpx.AsyncClient,
    *,
    client_id: str,
    code_challenge: str,
    redirect_uri: str = _REDIRECT_URI,
    state: str = "xyz",
) -> httpx.Response:
    """`GET /authorize` -> 302 to `/login`; returns the `/login` response."""
    authorize_response = await client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
            "resource": _RESOURCE,
        },
    )
    assert authorize_response.status_code == 302, authorize_response.text
    login_url = _location(authorize_response)
    assert urlsplit(login_url).path == LOGIN_PATH
    return await client.get(login_url)


async def _exchange_code(
    client: httpx.AsyncClient,
    *,
    code: str,
    client_id: str,
    code_verifier: str,
    redirect_uri: str = _REDIRECT_URI,
) -> dict[str, Any]:
    token_response = await client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": code_verifier,
        },
    )
    assert token_response.status_code == 200, token_response.text
    body = token_response.json()
    assert isinstance(body, dict)
    return body


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


# === Shared namespace helpers (auth.login) ========================================


class TestNamespaceHelpers:
    def test_parse_namespaces_defaults_to_every_namespace(self) -> None:
        assert parse_namespaces(None) == ["*"]
        assert parse_namespaces("") == ["*"]

    def test_parse_namespaces_splits_and_strips_a_csv_list(self) -> None:
        assert parse_namespaces(" personal , work ") == ["personal", "work"]

    def test_parse_namespace_map_lowercases_keys(self) -> None:
        namespace_map = parse_namespace_map('{"Alice@Example.Test": ["work"]}')
        assert namespace_map == {"alice@example.test": ["work"]}

    def test_parse_namespace_map_rejects_invalid_json(self) -> None:
        with pytest.raises(ServerConfigError, match="LOGIN_NAMESPACE_MAP"):
            parse_namespace_map("{not json")

    def test_parse_namespace_map_rejects_a_non_object(self) -> None:
        with pytest.raises(ServerConfigError, match="JSON object"):
            parse_namespace_map("[1, 2, 3]")

    def test_resolve_namespaces_prefers_the_map_over_the_default(self) -> None:
        namespace_map = {"alice": ["work"]}
        assert resolve_namespaces(["alice"], namespace_map=namespace_map, default=["*"]) == ["work"]
        assert resolve_namespaces(["bob"], namespace_map=namespace_map, default=["*"]) == ["*"]

    def test_resolve_namespaces_tries_keys_in_order(self) -> None:
        namespace_map = {"bob@example.test": ["work"]}
        namespaces = resolve_namespaces(
            ["alice", "bob@example.test"], namespace_map=namespace_map, default=["*"]
        )
        assert namespaces == ["work"]


# === PasswordAuthenticator.from_env ================================================


class TestPasswordAuthenticatorFromEnv:
    def test_requires_admin_password_hash(self) -> None:
        with pytest.raises(ServerConfigError, match="ADMIN_PASSWORD_HASH"):
            PasswordAuthenticator.from_env({})

    def test_builds_with_a_configured_hash(self) -> None:
        authenticator = PasswordAuthenticator.from_env(
            {"ADMIN_PASSWORD_HASH": hash_password(_ADMIN_PASSWORD)}
        )
        assert isinstance(authenticator, PasswordAuthenticator)


# === OidcAuthenticator.from_env =====================================================


class TestOidcAuthenticatorFromEnv:
    _BASE_ENVIRON: ClassVar[dict[str, str]] = {
        "OIDC_ISSUER": "https://idp.example.test",
        "OIDC_CLIENT_ID": "mm-oidc-client",
        "OIDC_CLIENT_SECRET": "s3cr3t",
        "PUBLIC_URL": _PUBLIC_URL,
        "OIDC_ALLOWED_SUBJECTS": "alice-sub",
    }

    def test_requires_every_core_variable(self) -> None:
        with pytest.raises(ServerConfigError, match="OIDC_ISSUER"):
            OidcAuthenticator.from_env(
                {k: v for k, v in self._BASE_ENVIRON.items() if k != "OIDC_ISSUER"}
            )

    def test_requires_a_non_empty_allowlist(self) -> None:
        environ = {k: v for k, v in self._BASE_ENVIRON.items() if k != "OIDC_ALLOWED_SUBJECTS"}
        with pytest.raises(ServerConfigError, match="allowlist"):
            OidcAuthenticator.from_env(environ)

    def test_builds_with_a_configured_allowlist(self) -> None:
        authenticator = OidcAuthenticator.from_env(dict(self._BASE_ENVIRON))
        assert isinstance(authenticator, OidcAuthenticator)


# === hash-password CLI ==============================================================


class TestHashPasswordCli:
    def test_produces_a_hash_the_password_verifies_against(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO(f"{_ADMIN_PASSWORD}\n"))
        exit_code = cli_main(["hash-password"])
        assert exit_code == 0

        printed = capsys.readouterr().out.strip()
        authenticator = PasswordAuthenticator(password_hash=printed, namespaces=["*"])
        # White-box: the hash must verify the real password and reject a wrong one -
        # exercised through the module function `_verify_password` indirectly via a
        # real login below would need a full app; checking the hash format directly
        # here is the fast, DB-free way to assert the CLI's output is actually usable.
        from argon2 import PasswordHasher

        PasswordHasher().verify(printed, _ADMIN_PASSWORD)
        with pytest.raises(Exception):  # noqa: B017 - argon2's own mismatch exception
            PasswordHasher().verify(printed, "wrong password")
        assert authenticator is not None

    def test_empty_stdin_is_an_error(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO(""))
        exit_code = cli_main(["hash-password"])
        assert exit_code == 2


# === Password mode: full HTTP flow ===================================================


async def test_password_login_succeeds_and_the_token_works(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    authenticator = PasswordAuthenticator(
        password_hash=hash_password(_ADMIN_PASSWORD), namespaces=["personal"]
    )
    async with _running_app(environ, config, authenticator=authenticator) as (app, client):
        client_id = await _register_client(client)
        code_verifier, code_challenge = _pkce_pair()

        login_response = await _start_authorize(
            client, client_id=client_id, code_challenge=code_challenge
        )
        assert login_response.status_code == 200
        assert "SIGN IN" in login_response.text.upper()

        submit_response = await client.post(
            LOGIN_PATH,
            data={"pending": _pending_id_from(login_response), "password": _ADMIN_PASSWORD},
        )
        assert submit_response.status_code == 302, submit_response.text
        callback_url = _location(submit_response)
        code = _query(callback_url)["code"][0]

        tokens = await _exchange_code(
            client, code=code, client_id=client_id, code_verifier=code_verifier
        )
        async with _authed_mcp_client(app, config, tokens["access_token"]) as mcp_client:
            result = await mcp_client.call_tool("memory_index", {})
            assert result.is_error is False


def _pending_id_from(response: httpx.Response) -> str:
    """The `pending` id embedded in a rendered `login_password_page`'s hidden form field."""
    marker = 'name="pending" value="'
    start = response.text.index(marker) + len(marker)
    end = response.text.index('"', start)
    return response.text[start:end]


async def test_password_login_rejects_the_wrong_password(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    authenticator = PasswordAuthenticator(
        password_hash=hash_password(_ADMIN_PASSWORD), namespaces=["*"]
    )
    async with _running_app(environ, config, authenticator=authenticator) as (_app, client):
        client_id = await _register_client(client)
        _verifier, code_challenge = _pkce_pair()
        login_response = await _start_authorize(
            client, client_id=client_id, code_challenge=code_challenge
        )
        pending_id = _pending_id_from(login_response)

        response = await client.post(LOGIN_PATH, data={"pending": pending_id, "password": "wrong"})
        assert response.status_code == 401
        assert "code=" not in response.headers.get("location", "")


async def test_password_login_blocks_the_sixth_attempt_even_with_the_right_password(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    authenticator = PasswordAuthenticator(
        password_hash=hash_password(_ADMIN_PASSWORD), namespaces=["*"]
    )
    async with _running_app(environ, config, authenticator=authenticator) as (_app, client):
        client_id = await _register_client(client)

        for _ in range(5):
            _verifier, code_challenge = _pkce_pair()
            login_response = await _start_authorize(
                client, client_id=client_id, code_challenge=code_challenge
            )
            pending_id = _pending_id_from(login_response)
            failed = await client.post(
                LOGIN_PATH, data={"pending": pending_id, "password": "wrong"}
            )
            assert failed.status_code == 401

        _verifier, code_challenge = _pkce_pair()
        login_response = await _start_authorize(
            client, client_id=client_id, code_challenge=code_challenge
        )
        pending_id = _pending_id_from(login_response)
        blocked = await client.post(
            LOGIN_PATH, data={"pending": pending_id, "password": _ADMIN_PASSWORD}
        )
        assert blocked.status_code == 429


async def test_login_page_carries_the_security_headers(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    authenticator = PasswordAuthenticator(
        password_hash=hash_password(_ADMIN_PASSWORD), namespaces=["*"]
    )
    async with _running_app(environ, config, authenticator=authenticator) as (_app, client):
        client_id = await _register_client(client)
        _verifier, code_challenge = _pkce_pair()
        login_response = await _start_authorize(
            client, client_id=client_id, code_challenge=code_challenge
        )

        assert login_response.headers["cache-control"] == "no-store"
        assert login_response.headers["x-frame-options"] == "DENY"
        assert "default-src 'none'" in login_response.headers["content-security-policy"]


_XSS_CLIENT_NAME = "<script>alert(1)</script>"


async def test_password_login_page_shows_redirect_host_and_escapes_the_client_name(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    authenticator = PasswordAuthenticator(
        password_hash=hash_password(_ADMIN_PASSWORD), namespaces=["*"]
    )
    async with _running_app(environ, config, authenticator=authenticator) as (_app, client):
        client_id = await _register_client(client, client_name=_XSS_CLIENT_NAME)
        _verifier, code_challenge = _pkce_pair()
        login_response = await _start_authorize(
            client, client_id=client_id, code_challenge=code_challenge
        )

        assert login_response.status_code == 200
        host = urlsplit(_REDIRECT_URI).hostname
        assert host is not None and host in login_response.text
        assert _XSS_CLIENT_NAME not in login_response.text
        assert html.escape(_XSS_CLIENT_NAME) in login_response.text


async def test_password_login_page_notes_a_localhost_redirect(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    authenticator = PasswordAuthenticator(
        password_hash=hash_password(_ADMIN_PASSWORD), namespaces=["*"]
    )
    localhost_redirect = "http://127.0.0.1:51000/callback"
    async with _running_app(environ, config, authenticator=authenticator) as (_app, client):
        client_id = await _register_client(client, redirect_uri=localhost_redirect)
        _verifier, code_challenge = _pkce_pair()
        login_response = await _start_authorize(
            client,
            client_id=client_id,
            code_challenge=code_challenge,
            redirect_uri=localhost_redirect,
        )
        assert "your own computer" in login_response.text


async def test_a_completed_pending_authorization_cannot_be_reused(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    authenticator = PasswordAuthenticator(
        password_hash=hash_password(_ADMIN_PASSWORD), namespaces=["*"]
    )
    async with _running_app(environ, config, authenticator=authenticator) as (_app, client):
        client_id = await _register_client(client)
        _verifier, code_challenge = _pkce_pair()
        login_response = await _start_authorize(
            client, client_id=client_id, code_challenge=code_challenge
        )
        pending_id = _pending_id_from(login_response)

        first = await client.post(
            LOGIN_PATH, data={"pending": pending_id, "password": _ADMIN_PASSWORD}
        )
        assert first.status_code == 302

        second = await client.post(
            LOGIN_PATH, data={"pending": pending_id, "password": _ADMIN_PASSWORD}
        )
        assert second.status_code == 400


# === OIDC mode: full HTTP flow =======================================================


def _oidc_authenticator(
    fake_oidc_provider: Any,
    *,
    allowed_subjects: frozenset[str] = frozenset(),
    allowed_emails: frozenset[str] = frozenset(),
    namespaces: list[str] | None = None,
    namespace_map: dict[str, list[str]] | None = None,
) -> OidcAuthenticator:
    return OidcAuthenticator(
        issuer=fake_oidc_provider.issuer,
        client_id=fake_oidc_provider.client_id,
        client_secret=fake_oidc_provider.client_secret,
        redirect_uri=f"{_PUBLIC_URL}{CALLBACK_PATH}",
        allowed_emails=allowed_emails,
        allowed_subjects=allowed_subjects,
        namespaces=namespaces if namespaces is not None else ["*"],
        namespace_map=namespace_map or {},
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(fake_oidc_provider.handler)),
    )


async def _confirm_interstitial(
    client: httpx.AsyncClient, interstitial_response: httpx.Response
) -> httpx.Response:
    """The "Continue to sign in" click: `GET` the link `oidc_interstitial_page` rendered."""
    assert interstitial_response.status_code == 200, interstitial_response.text
    return await client.get(_continue_url_from(interstitial_response))


async def _drive_to_callback(
    client: httpx.AsyncClient,
    fake_oidc_provider: Any,
    *,
    client_id: str,
    code_challenge: str,
    claims: dict[str, Any] | None,
    state_override: str | None = None,
) -> httpx.Response:
    """`/authorize` -> `/login` (the interstitial page) -> "Continue to sign in" (a
    redirect to the fake upstream) -> the fake upstream "login" (`issue_code`, skipped
    entirely if `claims` is `None`) -> `{CALLBACK_PATH}`.
    """
    interstitial_response = await _start_authorize(
        client, client_id=client_id, code_challenge=code_challenge
    )
    redirect_response = await _confirm_interstitial(client, interstitial_response)
    assert redirect_response.status_code == 302, redirect_response.text
    upstream_url = _location(redirect_response)
    assert upstream_url.startswith(fake_oidc_provider.authorization_endpoint)
    state = state_override if state_override is not None else _query(upstream_url)["state"][0]

    params: dict[str, str] = {"state": state}
    if claims is not None:
        params["code"] = fake_oidc_provider.issue_code(claims)
    return await client.get(CALLBACK_PATH, params=params)


async def test_oidc_full_flow_then_tools_call(
    bare_remote: Path, tmp_path: Path, test_database_url: str, fake_oidc_provider: Any
) -> None:
    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    authenticator = _oidc_authenticator(
        fake_oidc_provider, allowed_subjects=frozenset({"alice-sub"})
    )
    async with _running_app(environ, config, authenticator=authenticator) as (app, client):
        client_id = await _register_client(client)
        code_verifier, code_challenge = _pkce_pair()

        callback_response = await _drive_to_callback(
            client,
            fake_oidc_provider,
            client_id=client_id,
            code_challenge=code_challenge,
            claims={"sub": "alice-sub", "email": "alice@example.test", "email_verified": True},
        )
        assert callback_response.status_code == 302, callback_response.text
        code = _query(_location(callback_response))["code"][0]

        tokens = await _exchange_code(
            client, code=code, client_id=client_id, code_verifier=code_verifier
        )
        async with _authed_mcp_client(app, config, tokens["access_token"]) as mcp_client:
            result = await mcp_client.call_tool("memory_index", {})
            assert result.is_error is False


async def test_oidc_callback_rejects_an_unknown_state(
    bare_remote: Path, tmp_path: Path, test_database_url: str, fake_oidc_provider: Any
) -> None:
    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    authenticator = _oidc_authenticator(
        fake_oidc_provider, allowed_subjects=frozenset({"alice-sub"})
    )
    async with _running_app(environ, config, authenticator=authenticator) as (_app, client):
        client_id = await _register_client(client)
        _verifier, code_challenge = _pkce_pair()

        callback_response = await _drive_to_callback(
            client,
            fake_oidc_provider,
            client_id=client_id,
            code_challenge=code_challenge,
            claims={"sub": "alice-sub"},
            state_override="not-the-real-state",
        )
        assert callback_response.status_code == 400


async def test_login_rejects_an_oidc_pending_state_value_as_its_own_pending_id(
    bare_remote: Path, tmp_path: Path, test_database_url: str, fake_oidc_provider: Any
) -> None:
    """A `state` value `OidcAuthenticator` parks on `SharedState` (`kind=
    'login_pending'` in `oauth_pending`, #103) must never be mistaken for one of
    `auth.store.save_pending`'s own `kind='authorize'` rows, even though both now live
    in the same table: `GET /login?pending=<that state>` has to return 400 like any
    other unknown pending id, not crash trying to parse a `login_pending` row's params
    as a `PendingRow`."""
    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    authenticator = _oidc_authenticator(
        fake_oidc_provider, allowed_subjects=frozenset({"alice-sub"})
    )
    async with _running_app(environ, config, authenticator=authenticator) as (_app, client):
        client_id = await _register_client(client)
        _verifier, code_challenge = _pkce_pair()

        interstitial_response = await _start_authorize(
            client, client_id=client_id, code_challenge=code_challenge
        )
        redirect_response = await _confirm_interstitial(client, interstitial_response)
        assert redirect_response.status_code == 302, redirect_response.text
        state = _query(_location(redirect_response))["state"][0]

        response = await client.get(LOGIN_PATH, params={"pending": state})
        assert response.status_code == 400


async def test_oidc_discovery_issuer_mismatch_is_rejected(
    bare_remote: Path, tmp_path: Path, test_database_url: str, fake_oidc_provider: Any
) -> None:
    fake_oidc_provider.discovery_issuer_override = "https://a-different-issuer.example.test"
    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    authenticator = _oidc_authenticator(
        fake_oidc_provider, allowed_subjects=frozenset({"alice-sub"})
    )
    async with _running_app(environ, config, authenticator=authenticator) as (_app, client):
        client_id = await _register_client(client)
        _verifier, code_challenge = _pkce_pair()

        interstitial_response = await _start_authorize(
            client, client_id=client_id, code_challenge=code_challenge
        )
        confirm_response = await _confirm_interstitial(client, interstitial_response)
        assert confirm_response.status_code == 503


async def test_oidc_discovery_accepts_a_trailing_slash_issuer_matched_exactly(
    bare_remote: Path, tmp_path: Path, test_database_url: str, fake_oidc_provider: Any
) -> None:
    # Hydra (and some other IdPs) advertise their issuer with a trailing slash; OIDC
    # Discovery §4.3 requires an exact, byte-for-byte match against the configured
    # OIDC_ISSUER, trailing slash included - this must still succeed, not be rejected
    # the way `test_oidc_discovery_issuer_mismatch_is_rejected` above expects a real
    # mismatch to be.
    fake_oidc_provider.issuer = "https://idp.example.test/"
    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    authenticator = _oidc_authenticator(
        fake_oidc_provider, allowed_subjects=frozenset({"alice-sub"})
    )
    async with _running_app(environ, config, authenticator=authenticator) as (_app, client):
        client_id = await _register_client(client)
        _verifier, code_challenge = _pkce_pair()

        interstitial_response = await _start_authorize(
            client, client_id=client_id, code_challenge=code_challenge
        )
        confirm_response = await _confirm_interstitial(client, interstitial_response)
        assert confirm_response.status_code == 302, confirm_response.text


async def test_oidc_discovery_rejects_a_trailing_slash_mismatch(
    bare_remote: Path, tmp_path: Path, test_database_url: str, fake_oidc_provider: Any
) -> None:
    # The configured issuer has no trailing slash; the discovery document's issuer
    # does - still a mismatch per OIDC Discovery §4.3, not normalized away.
    fake_oidc_provider.discovery_issuer_override = f"{fake_oidc_provider.issuer}/"
    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    authenticator = _oidc_authenticator(
        fake_oidc_provider, allowed_subjects=frozenset({"alice-sub"})
    )
    async with _running_app(environ, config, authenticator=authenticator) as (_app, client):
        client_id = await _register_client(client)
        _verifier, code_challenge = _pkce_pair()

        interstitial_response = await _start_authorize(
            client, client_id=client_id, code_challenge=code_challenge
        )
        confirm_response = await _confirm_interstitial(client, interstitial_response)
        assert confirm_response.status_code == 503


async def test_oidc_denies_an_email_not_on_the_allowlist(
    bare_remote: Path, tmp_path: Path, test_database_url: str, fake_oidc_provider: Any
) -> None:
    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    authenticator = _oidc_authenticator(
        fake_oidc_provider, allowed_emails=frozenset({"carsten@example.test"})
    )
    async with _running_app(environ, config, authenticator=authenticator) as (_app, client):
        client_id = await _register_client(client)
        _verifier, code_challenge = _pkce_pair()

        callback_response = await _drive_to_callback(
            client,
            fake_oidc_provider,
            client_id=client_id,
            code_challenge=code_challenge,
            claims={"sub": "mallory-sub", "email": "mallory@example.test", "email_verified": True},
        )
        assert callback_response.status_code == 403
        assert "code=" not in callback_response.headers.get("location", "")


async def test_oidc_denies_an_unverified_email_even_if_listed(
    bare_remote: Path, tmp_path: Path, test_database_url: str, fake_oidc_provider: Any
) -> None:
    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    authenticator = _oidc_authenticator(
        fake_oidc_provider, allowed_emails=frozenset({"carsten@example.test"})
    )
    async with _running_app(environ, config, authenticator=authenticator) as (_app, client):
        client_id = await _register_client(client)
        _verifier, code_challenge = _pkce_pair()

        callback_response = await _drive_to_callback(
            client,
            fake_oidc_provider,
            client_id=client_id,
            code_challenge=code_challenge,
            claims={
                "sub": "carsten-sub",
                "email": "carsten@example.test",
                "email_verified": False,
            },
        )
        assert callback_response.status_code == 403


async def test_oidc_callback_rejects_userinfo_without_a_subject(
    bare_remote: Path, tmp_path: Path, test_database_url: str, fake_oidc_provider: Any
) -> None:
    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    authenticator = _oidc_authenticator(
        fake_oidc_provider, allowed_subjects=frozenset({"alice-sub"})
    )
    async with _running_app(environ, config, authenticator=authenticator) as (_app, client):
        client_id = await _register_client(client)
        _verifier, code_challenge = _pkce_pair()

        callback_response = await _drive_to_callback(
            client,
            fake_oidc_provider,
            client_id=client_id,
            code_challenge=code_challenge,
            claims={"email": "alice@example.test", "email_verified": True},
        )
        assert callback_response.status_code == 400


async def test_oidc_interstitial_shows_redirect_host_and_escapes_the_client_name(
    bare_remote: Path, tmp_path: Path, test_database_url: str, fake_oidc_provider: Any
) -> None:
    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    authenticator = _oidc_authenticator(
        fake_oidc_provider, allowed_subjects=frozenset({"alice-sub"})
    )
    async with _running_app(environ, config, authenticator=authenticator) as (_app, client):
        client_id = await _register_client(client, client_name=_XSS_CLIENT_NAME)
        _verifier, code_challenge = _pkce_pair()
        interstitial_response = await _start_authorize(
            client, client_id=client_id, code_challenge=code_challenge
        )

        assert interstitial_response.status_code == 200
        host = urlsplit(_REDIRECT_URI).hostname
        assert host is not None and host in interstitial_response.text
        assert _XSS_CLIENT_NAME not in interstitial_response.text
        assert html.escape(_XSS_CLIENT_NAME) in interstitial_response.text


async def test_oidc_interstitial_notes_a_localhost_redirect(
    bare_remote: Path, tmp_path: Path, test_database_url: str, fake_oidc_provider: Any
) -> None:
    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    authenticator = _oidc_authenticator(
        fake_oidc_provider, allowed_subjects=frozenset({"alice-sub"})
    )
    localhost_redirect = "http://127.0.0.1:51000/callback"
    async with _running_app(environ, config, authenticator=authenticator) as (_app, client):
        client_id = await _register_client(client, redirect_uri=localhost_redirect)
        _verifier, code_challenge = _pkce_pair()
        interstitial_response = await _start_authorize(
            client,
            client_id=client_id,
            code_challenge=code_challenge,
            redirect_uri=localhost_redirect,
        )
        assert "your own computer" in interstitial_response.text


async def test_oidc_authenticator_aclose_closes_only_a_self_created_client() -> None:
    created = OidcAuthenticator(
        issuer="https://idp.example.test",
        client_id="c",
        client_secret="s",  # noqa: S106 - a fake test credential
        redirect_uri="https://mm.example.test/oidc/callback",
        allowed_emails=frozenset(),
        allowed_subjects=frozenset({"alice"}),
        namespaces=["*"],
        namespace_map={},
    )
    await created.aclose()
    # White-box: there is no public getter for this, and this test only exists to pin
    # down "a self-created client is actually closed".
    assert created._http.is_closed

    external_client = httpx.AsyncClient()
    supplied = OidcAuthenticator(
        issuer="https://idp.example.test",
        client_id="c",
        client_secret="s",  # noqa: S106 - a fake test credential
        redirect_uri="https://mm.example.test/oidc/callback",
        allowed_emails=frozenset(),
        allowed_subjects=frozenset({"alice"}),
        namespaces=["*"],
        namespace_map={},
        http_client=external_client,
    )
    await supplied.aclose()
    assert not external_client.is_closed
    await external_client.aclose()


async def test_oidc_namespace_map_restricts_the_issued_token(
    bare_remote: Path,
    tmp_path: Path,
    test_database_url: str,
    fake_oidc_provider: Any,
    pool: asyncpg.Pool,
) -> None:
    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    authenticator = _oidc_authenticator(
        fake_oidc_provider,
        allowed_subjects=frozenset({"alice-sub"}),
        namespaces=["*"],
        namespace_map={"alice-sub": ["work"]},
    )
    async with _running_app(environ, config, authenticator=authenticator) as (_app, client):
        client_id = await _register_client(client)
        code_verifier, code_challenge = _pkce_pair()

        callback_response = await _drive_to_callback(
            client,
            fake_oidc_provider,
            client_id=client_id,
            code_challenge=code_challenge,
            claims={"sub": "alice-sub", "email": "alice@example.test", "email_verified": True},
        )
        assert callback_response.status_code == 302, callback_response.text
        code = _query(_location(callback_response))["code"][0]
        tokens = await _exchange_code(
            client, code=code, client_id=client_id, code_verifier=code_verifier
        )

    stored = await store.get_token(pool, tokens["access_token"], "access")
    assert stored is not None
    assert stored.namespaces == ("work",)
