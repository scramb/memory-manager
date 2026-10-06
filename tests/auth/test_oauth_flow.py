# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the embedded OAuth 2.1 authorization server (ADR-0004, #36).

`FakeAuthenticator` is this file's only `auth.login.Authenticator`: it
completes a pending authorization immediately, with a fixed `subject`/
`namespaces`, standing in for the real login methods #37 adds. Everything
else drives the real HTTP surface `memory_manager.http.create_app` builds -
DCR (`/register`), `/authorize`, `/login`, `/token`, `/revoke`, the AS
metadata document - with a plain `httpx.AsyncClient` over an ASGI
transport, `follow_redirects=False` throughout so each hop can be asserted
on its own. `tools/list`/`tools/call` against the issued access token go
through a real `mcp.Client` session instead, the same seam
`tests/auth/test_static_tokens.py` already uses to drive the MCP protocol
over an in-process ASGI app.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import asyncpg
import httpx
import httpx2
import pytest
import pytest_asyncio
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

from memory_manager.app import open_services
from memory_manager.auth.login import BoundCompleter, PendingAuthorization
from memory_manager.auth.verifier import OAUTH_ACCESS_TOKEN_PREFIX, verify_bearer_token
from memory_manager.config import ServerConfig, ServerConfigError, canonical_resource_url
from memory_manager.db.migrate import migrate
from memory_manager.http import create_app

_PUBLIC_URL = "https://mm.example.test"
_MCP_PATH = "/mcp"
_RESOURCE = f"{_PUBLIC_URL}{_MCP_PATH}"
_REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"
_SUBJECT = "alice"
_NAMESPACES = ["personal"]


class FakeAuthenticator:
    """Completes every pending authorization immediately as `subject`/`namespaces` -
    the test-only stand-in `create_app`'s docstring describes; #37 replaces this with
    a real login method."""

    def __init__(self, *, subject: str = _SUBJECT, namespaces: list[str] | None = None) -> None:
        self.subject = subject
        self.namespaces = namespaces if namespaces is not None else list(_NAMESPACES)

    async def handle(
        self, request: Request, pending: PendingAuthorization, complete: BoundCompleter
    ) -> Response:
        redirect_url = await complete(self.subject, self.namespaces)
        if redirect_url is None:
            return PlainTextResponse("expired", status_code=400)
        return RedirectResponse(redirect_url, status_code=302)


#: A Fernet key, generated once for this test module only (`Fernet.generate_key()`) -
#: not a credential protecting anything real, just what `OAUTH_CLIENT_SECRET_KEY` requires.
_CLIENT_SECRET_KEY = "r8rGp30uQA9cx9egMfZk4ez3xkfaFzL0tCst-kNzrcI="  # noqa: S105 - a Fernet test key, not a credential protecting anything real


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
    environ: dict[str, str],
    config: ServerConfig,
    *,
    authenticator: FakeAuthenticator | None,
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
    """A PKCE `(code_verifier, code_challenge)` pair, S256."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


async def _register_client(client: httpx.AsyncClient, *, redirect_uri: str = _REDIRECT_URI) -> str:
    response = await client.post(
        "/register",
        json={"redirect_uris": [redirect_uri], "token_endpoint_auth_method": "none"},
    )
    assert response.status_code == 201, response.text
    client_id = response.json()["client_id"]
    assert isinstance(client_id, str)
    return client_id


def _location(response: httpx.Response) -> str:
    location = response.headers.get("location")
    assert location is not None, f"expected a redirect, got {response.status_code} {response.text}"
    return str(location)


async def _authorize_and_login(
    client: httpx.AsyncClient,
    *,
    client_id: str,
    code_challenge: str,
    redirect_uri: str = _REDIRECT_URI,
    resource: str | None = _RESOURCE,
    state: str = "xyz",
) -> httpx.Response:
    """Drive `/authorize` through to the login redirect; returns the `/login` response
    (a 302 back to `redirect_uri` on success, whatever `/login` answered on failure)."""
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    if resource is not None:
        params["resource"] = resource
    authorize_response = await client.get("/authorize", params=params)
    assert authorize_response.status_code == 302, authorize_response.text
    login_url = _location(authorize_response)
    assert urlsplit(login_url).path == "/login"
    return await client.get(login_url)


async def _complete_full_login(
    client: httpx.AsyncClient,
    *,
    client_id: str,
    code_verifier: str,
    code_challenge: str,
    redirect_uri: str = _REDIRECT_URI,
    resource: str | None = _RESOURCE,
    state: str = "xyz",
) -> dict[str, Any]:
    """`/register` is assumed done; drives `/authorize` -> `/login` -> `/token`, returns the
    token response body. Asserts `state`/`iss` on the way, same as a real OAuth client would."""
    login_response = await _authorize_and_login(
        client,
        client_id=client_id,
        code_challenge=code_challenge,
        redirect_uri=redirect_uri,
        resource=resource,
        state=state,
    )
    assert login_response.status_code == 302, login_response.text
    callback_url = _location(login_response)
    parsed = urlsplit(callback_url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == redirect_uri
    query = parse_qs(parsed.query)
    assert query["state"] == [state]
    assert query["iss"] == [_PUBLIC_URL]
    code = query["code"][0]

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


@pytest_asyncio.fixture
async def pool(test_database_url: str) -> AsyncIterator[asyncpg.Pool]:
    conn = await asyncpg.connect(test_database_url)
    try:
        await migrate(conn)
    finally:
        await conn.close()
    created_pool = await asyncpg.create_pool(test_database_url)
    try:
        yield created_pool
    finally:
        await created_pool.close()


# --- The full happy path: DCR -> authorize -> login -> token -> tools/call ---


async def test_full_authorization_code_flow_then_tools_call(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    async with _running_app(environ, config, authenticator=FakeAuthenticator()) as (app, client):
        client_id = await _register_client(client)
        code_verifier, code_challenge = _pkce_pair()

        tokens = await _complete_full_login(
            client, client_id=client_id, code_verifier=code_verifier, code_challenge=code_challenge
        )
        assert tokens["access_token"].startswith(OAUTH_ACCESS_TOKEN_PREFIX)
        assert tokens["refresh_token"].startswith("mmr_")
        assert tokens["expires_in"] == 3600

        async with _authed_mcp_client(app, config, tokens["access_token"]) as mcp_client:
            index_result = await mcp_client.call_tool("memory_index", {})
            assert index_result.is_error is False


# --- Refresh rotation and replay -> family revocation -------------------------


async def test_refresh_rotates_and_replay_revokes_the_whole_family(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    async with _running_app(environ, config, authenticator=FakeAuthenticator()) as (app, client):
        client_id = await _register_client(client)
        code_verifier, code_challenge = _pkce_pair()
        tokens = await _complete_full_login(
            client, client_id=client_id, code_verifier=code_verifier, code_challenge=code_challenge
        )
        old_access = tokens["access_token"]
        old_refresh = tokens["refresh_token"]

        refreshed_response = await client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": old_refresh,
                "client_id": client_id,
            },
        )
        assert refreshed_response.status_code == 200, refreshed_response.text
        refreshed = refreshed_response.json()
        new_access = refreshed["access_token"]
        new_refresh = refreshed["refresh_token"]
        assert new_access != old_access
        assert new_refresh != old_refresh

        # The new access token works.
        async with _authed_mcp_client(app, config, new_access) as mcp_client:
            result = await mcp_client.call_tool("memory_index", {})
            assert result.is_error is False

        # Reusing the old (already-rotated) refresh token is a replay: rejected, and
        # it revokes the whole family - including the pair that replaced it.
        replay_response = await client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": old_refresh,
                "client_id": client_id,
            },
        )
        assert replay_response.status_code == 400
        assert replay_response.json()["error"] == "invalid_grant"

        with pytest.raises(Exception):  # noqa: B017 - raised by the SDK client's own session
            async with _authed_mcp_client(app, config, new_access) as mcp_client:
                await mcp_client.call_tool("memory_index", {})


# --- Revocation -----------------------------------------------------------------


async def test_revoke_invalidates_the_access_token(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    async with _running_app(environ, config, authenticator=FakeAuthenticator()) as (app, client):
        client_id = await _register_client(client)
        code_verifier, code_challenge = _pkce_pair()
        tokens = await _complete_full_login(
            client, client_id=client_id, code_verifier=code_verifier, code_challenge=code_challenge
        )

        revoke_response = await client.post(
            "/revoke",
            data={"token": tokens["access_token"], "client_id": client_id, "client_secret": ""},
        )
        assert revoke_response.status_code == 200

        with pytest.raises(Exception):  # noqa: B017 - raised by the SDK client's own session
            async with _authed_mcp_client(app, config, tokens["access_token"]) as mcp_client:
                await mcp_client.call_tool("memory_index", {})


# --- Negative paths --------------------------------------------------------------


async def test_authorize_rejects_plain_pkce(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    async with _running_app(environ, config, authenticator=FakeAuthenticator()) as (_app, client):
        client_id = await _register_client(client)
        response = await client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": _REDIRECT_URI,
                "code_challenge": "plain-challenge",
                "code_challenge_method": "plain",
                "state": "xyz",
            },
        )
        if response.status_code == 302:
            assert "error=" in _location(response)
            assert "code=" not in _location(response)
        else:
            assert response.status_code == 400


async def test_authorize_rejects_the_wrong_resource(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    async with _running_app(environ, config, authenticator=FakeAuthenticator()) as (_app, client):
        client_id = await _register_client(client)
        _verifier, challenge = _pkce_pair()
        response = await client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": _REDIRECT_URI,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "xyz",
                "resource": "https://wrong.example.test/mcp",
            },
        )
        assert response.status_code == 302
        location = _location(response)
        assert location.startswith(_REDIRECT_URI)
        assert "error=invalid_target" in location


async def test_authorize_rejects_a_missing_resource(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    """RFC 8707's `resource` is REQUIRED by this server, not merely validated when a
    client bothers to send one - a client that omits it entirely is rejected exactly
    like one that sends the wrong value, at `/authorize` itself."""
    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    async with _running_app(environ, config, authenticator=FakeAuthenticator()) as (_app, client):
        client_id = await _register_client(client)
        _verifier, challenge = _pkce_pair()
        response = await client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": _REDIRECT_URI,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "xyz",
                # `resource` deliberately omitted.
            },
        )
        assert response.status_code == 302
        location = _location(response)
        assert location.startswith(_REDIRECT_URI)
        assert "error=invalid_target" in location


async def test_verifier_rejects_a_token_issued_for_a_different_resource(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    """ADR-0004: audience enforced. A token issued for this server's own canonical
    resource must still be rejected by `auth.verifier.verify_bearer_token` when checked
    against a *different* resource - the scenario a second, differently configured
    deployment sharing the same database would otherwise be exposed to."""
    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    async with _running_app(environ, config, authenticator=FakeAuthenticator()) as (_app, client):
        client_id = await _register_client(client)
        code_verifier, code_challenge = _pkce_pair()
        tokens = await _complete_full_login(
            client, client_id=client_id, code_verifier=code_verifier, code_challenge=code_challenge
        )

    async with asyncpg.create_pool(test_database_url) as pool:
        verified_for_own_resource = await verify_bearer_token(
            pool, tokens["access_token"], oauth_resource=_RESOURCE
        )
        verified_for_other_resource = await verify_bearer_token(
            pool, tokens["access_token"], oauth_resource="https://other.example.test/mcp"
        )
    assert verified_for_own_resource is not None
    assert verified_for_other_resource is None


async def test_authorization_code_is_single_use(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    async with _running_app(environ, config, authenticator=FakeAuthenticator()) as (_app, client):
        client_id = await _register_client(client)
        code_verifier, code_challenge = _pkce_pair()
        login_response = await _authorize_and_login(
            client, client_id=client_id, code_challenge=code_challenge
        )
        code = parse_qs(urlsplit(_location(login_response)).query)["code"][0]

        token_data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": code_verifier,
        }
        first = await client.post("/token", data=token_data)
        assert first.status_code == 200

        second = await client.post("/token", data=token_data)
        assert second.status_code == 400
        assert second.json()["error"] == "invalid_grant"


async def test_expired_authorization_code_is_rejected(
    bare_remote: Path, tmp_path: Path, test_database_url: str, pool: asyncpg.Pool
) -> None:
    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    async with _running_app(environ, config, authenticator=FakeAuthenticator()) as (_app, client):
        client_id = await _register_client(client)
        code_verifier, code_challenge = _pkce_pair()
        login_response = await _authorize_and_login(
            client, client_id=client_id, code_challenge=code_challenge
        )
        code = parse_qs(urlsplit(_location(login_response)).query)["code"][0]

        await pool.execute(
            "update oauth_auth_codes set expires_at = now() - interval '1 second' "
            "where code_hash = $1",
            hashlib.sha256(code.encode("utf-8")).hexdigest(),
        )

        response = await client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _REDIRECT_URI,
                "client_id": client_id,
                "code_verifier": code_verifier,
            },
        )
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_grant"


async def test_confidential_client_secret_is_encrypted_at_rest(
    bare_remote: Path, tmp_path: Path, test_database_url: str, pool: asyncpg.Pool
) -> None:
    """RFC 7591 §3.2.1: the SDK mints a `client_secret` by default for any DCR client that
    does not explicitly request `token_endpoint_auth_method: "none"` - `auth.store` must
    not keep that plaintext around, but the SDK's own `ClientAuthenticator` (`mcp/server/
    auth/middleware/client_auth.py`) still has to see the real secret to authenticate
    `/token`/`/revoke`, so encryption at rest (`ClientSecretCipher`), not hashing, is what
    this checks end to end: the secret still works for a real request, a wrong one is
    still rejected, and the stored row never contains the plaintext."""
    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    async with _running_app(environ, config, authenticator=FakeAuthenticator()) as (_app, client):
        # No `token_endpoint_auth_method` -> the SDK defaults to `client_secret_post` and
        # mints a secret (`register.py`), unlike every other registration in this file.
        register_response = await client.post("/register", json={"redirect_uris": [_REDIRECT_URI]})
        assert register_response.status_code == 201, register_response.text
        registered = register_response.json()
        client_id = registered["client_id"]
        client_secret = registered["client_secret"]
        assert registered["token_endpoint_auth_method"] == "client_secret_post"  # noqa: S105
        assert client_secret

        code_verifier, code_challenge = _pkce_pair()
        login_response = await _authorize_and_login(
            client, client_id=client_id, code_challenge=code_challenge
        )
        code = parse_qs(urlsplit(_location(login_response)).query)["code"][0]
        token_data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": code_verifier,
        }

        wrong_secret_response = await client.post(
            "/token", data={**token_data, "client_secret": "not-the-right-secret"}
        )
        assert wrong_secret_response.status_code == 401
        assert wrong_secret_response.json()["error"] == "invalid_client"

        right_secret_response = await client.post(
            "/token", data={**token_data, "client_secret": client_secret}
        )
        assert right_secret_response.status_code == 200, right_secret_response.text

    row = await pool.fetchrow(
        "select client_info from oauth_clients where client_id = $1", client_id
    )
    assert row is not None
    assert client_secret not in row["client_info"]


async def test_access_token_is_never_stored_in_plaintext(
    bare_remote: Path, tmp_path: Path, test_database_url: str, pool: asyncpg.Pool
) -> None:
    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    async with _running_app(environ, config, authenticator=FakeAuthenticator()) as (_app, client):
        client_id = await _register_client(client)
        code_verifier, code_challenge = _pkce_pair()
        tokens = await _complete_full_login(
            client, client_id=client_id, code_verifier=code_verifier, code_challenge=code_challenge
        )

    plaintext = tokens["access_token"]
    rows = await pool.fetch("select token_hash from oauth_tokens")
    assert rows, "expected at least one issued token"
    expected_hash = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    assert any(row["token_hash"] == expected_hash for row in rows)
    assert all(row["token_hash"] != plaintext for row in rows)


async def test_oauth_authorization_server_disabled_without_an_authenticator(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    """No `authenticator` (the production default today, #37) means no OAuth routes at
    all - static tokens are unaffected."""
    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    async with _running_app(environ, config, authenticator=None) as (app, client):
        authorize_response = await client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": "whatever",
                "redirect_uri": _REDIRECT_URI,
                "code_challenge": "x",
                "code_challenge_method": "S256",
            },
        )
        assert authorize_response.status_code == 404

        metadata_response = await client.get("/.well-known/oauth-authorization-server")
        assert metadata_response.status_code == 404

        pool = app.state.services.pool
        from memory_manager.auth.tokens import create_token
        from memory_manager.mcp.authz import READ_SCOPE

        plaintext, _info = await create_token(
            pool, "ci", scopes=[READ_SCOPE], namespaces=["personal"]
        )
        async with _authed_mcp_client(app, config, plaintext) as mcp_client:
            result = await mcp_client.call_tool("memory_index", {})
            assert result.is_error is False


async def test_login_mode_without_an_authenticator_is_a_startup_error(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    """ADR-0004/#37: a deployment that asks for OAuth login but has no login method
    wired in yet must refuse to start, not silently fall back to static tokens."""
    config = ServerConfig(public_url=_PUBLIC_URL, mcp_path=_MCP_PATH, login_mode="password")
    environ = _environ(bare_remote, tmp_path, test_database_url)

    with pytest.raises(ServerConfigError, match="LOGIN_MODE"):
        async with _running_app(environ, config, authenticator=None):
            pass


# --- AS metadata -----------------------------------------------------------------


async def test_authorization_server_metadata(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    async with _running_app(environ, config, authenticator=FakeAuthenticator()) as (_app, client):
        response = await client.get("/.well-known/oauth-authorization-server")

    assert response.status_code == 200
    metadata = response.json()
    assert metadata["issuer"] == canonical_resource_url(_PUBLIC_URL, "")
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert set(metadata["grant_types_supported"]) >= {"authorization_code", "refresh_token"}
    assert metadata["registration_endpoint"].endswith("/register")
    assert metadata["revocation_endpoint"].endswith("/revoke")
    assert metadata["scopes_supported"] == ["memory:read", "memory:write"]
