# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for Open WebUI as a confidential OAuth 2.1 client of the embedded
authorization server (#143, ADR-0011 option A).

`_OPENWEBUI_DCR_BODY` is Open WebUI's own DCR request, recorded live against a pinned
0.11.4 stack in the WP-42 spike (2026-10-10, `docs/research/clients/openwebui.md`'s
"Spike results") and cross-checked against `open_webui/utils/oauth.py` at that version
(`client_name`/`redirect_uris`/`grant_types`/`response_types`/`token_endpoint_auth_method`
L517-538; the refresh request's `client_id`/`client_secret`/`resource`/`scope` shape,
`scope` only with `OAUTH_REFRESH_TOKEN_INCLUDE_SCOPE`, L1146-1164). The recorded host
(`localhost:3000`) is substituted for this suite's `*.example.test` convention; the field
set and the `/oauth/clients/<id>/callback` path shape are unchanged.

Helpers below are copied from `tests/auth/test_oauth_flow.py:46-228` (modules copy, don't
import, per this task's contract) and adapted to drive Open WebUI's own request shapes -
a `client_secret_post` confidential client with `scope`/`resource` on `/authorize` and a
real `client_secret` on every `/token`/`/revoke` call - instead of the generic public
client the other file registers.
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
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

from memory_manager.app import open_services
from memory_manager.auth.login import BoundCompleter, PendingAuthorization
from memory_manager.auth.verifier import OAUTH_ACCESS_TOKEN_PREFIX
from memory_manager.config import ServerConfig
from memory_manager.http import create_app
from memory_manager.vault.git import Git

_PUBLIC_URL = "https://mm.example.test"
_MCP_PATH = "/mcp"
_RESOURCE = f"{_PUBLIC_URL}{_MCP_PATH}"

#: Open WebUI's own frontend base URL and connection id (`mcp:<server-id>`, Open WebUI's
#: own lookup key, unrelated to our AS-issued `client_id` - confirmed by the spike).
_WEBUI_URL = "https://openwebui.example.test"
_CONNECTION_ID = "mcp:memmgr"
_OPENWEBUI_REDIRECT_URI = f"{_WEBUI_URL}/oauth/clients/{_CONNECTION_ID}/callback"
_OPENWEBUI_SCOPE = "memory:read memory:write"

#: Open WebUI's DCR request body: `OAuthClientMetadata().model_dump(exclude_none=True)`
#: (`utils/oauth.py#L517-538`, v0.11.4) - no other field is sent.
_OPENWEBUI_DCR_BODY: dict[str, Any] = {
    "client_name": "Open WebUI",
    "redirect_uris": [_OPENWEBUI_REDIRECT_URI],
    "grant_types": ["authorization_code", "refresh_token"],
    "response_types": ["code"],
    "token_endpoint_auth_method": "client_secret_post",
    "scope": _OPENWEBUI_SCOPE,
}

#: Raised to comfortably clear the combined `/register`+`/authorize`+`/token` bucket
#: (default `oauth_burst=10`, shared by client IP) that a full flow plus its negative
#: cases can otherwise exhaust inside a single test - see this task's "Risiken".
_OAUTH_BURST = 40.0

_SUBJECT = "alice"
_NAMESPACES = ["personal"]


class FakeAuthenticator:
    """Completes every pending authorization immediately as `subject`/`namespaces` -
    same stand-in `tests/auth/test_oauth_flow.py` uses; #37 replaces this with a real
    login method. `subject`/`namespaces` are mutable instance attributes so one instance
    can stand in for two different Open WebUI users across two flows."""

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
_CLIENT_SECRET_KEY = "jqu3ADLWDQGwO7qUDQAt3eYkIBNcwn32Flv0fwYObgU="  # noqa: S105 - a Fernet test key, not a credential protecting anything real


def _config() -> ServerConfig:
    return ServerConfig(
        public_url=_PUBLIC_URL,
        mcp_path=_MCP_PATH,
        oauth_client_secret_key=_CLIENT_SECRET_KEY,
        oauth_burst=_OAUTH_BURST,
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


def _location(response: httpx.Response) -> str:
    location = response.headers.get("location")
    assert location is not None, f"expected a redirect, got {response.status_code} {response.text}"
    return str(location)


def _commit_author(remote: Path, commit: str) -> str:
    """The `<name> <email>` author of `commit` in `remote` - same helper
    `tests/mcp/test_write_tools.py:59-61` uses, copied rather than imported."""
    result = Git(cwd=remote).run("log", "-1", "--format=%an <%ae>", commit)
    return result.stdout.decode("utf-8").strip()


async def _register_openwebui_client(client: httpx.AsyncClient) -> dict[str, Any]:
    """`/register` with Open WebUI's own recorded DCR body."""
    response = await client.post("/register", json=_OPENWEBUI_DCR_BODY)
    assert response.status_code == 201, response.text
    registered = response.json()
    assert isinstance(registered, dict)
    return registered


async def _authorize_openwebui(
    client: httpx.AsyncClient,
    *,
    client_id: str,
    code_challenge: str,
    redirect_uri: str = _OPENWEBUI_REDIRECT_URI,
    resource: str | None = _RESOURCE,
    scope: str | None = _OPENWEBUI_SCOPE,
    state: str = "xyz",
) -> httpx.Response:
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
    if scope is not None:
        params["scope"] = scope
    return await client.get("/authorize", params=params)


async def _authorize_and_login(
    client: httpx.AsyncClient,
    *,
    client_id: str,
    code_challenge: str,
    redirect_uri: str = _OPENWEBUI_REDIRECT_URI,
    resource: str | None = _RESOURCE,
    scope: str | None = _OPENWEBUI_SCOPE,
    state: str = "xyz",
) -> httpx.Response:
    """Drive `/authorize` through to the login redirect; returns the `/login` response
    (a 302 back to `redirect_uri` on success)."""
    authorize_response = await _authorize_openwebui(
        client,
        client_id=client_id,
        code_challenge=code_challenge,
        redirect_uri=redirect_uri,
        resource=resource,
        scope=scope,
        state=state,
    )
    assert authorize_response.status_code == 302, authorize_response.text
    login_url = _location(authorize_response)
    assert urlsplit(login_url).path == "/login"
    return await client.get(login_url)


async def _complete_openwebui_login(
    client: httpx.AsyncClient,
    *,
    client_id: str,
    client_secret: str,
    code_verifier: str,
    code_challenge: str,
    redirect_uri: str = _OPENWEBUI_REDIRECT_URI,
    resource: str | None = _RESOURCE,
    scope: str | None = _OPENWEBUI_SCOPE,
    state: str = "xyz",
) -> dict[str, Any]:
    """`/register` is assumed done; drives `/authorize` -> `/login` -> `/token` with
    `client_secret_post`, returns the token response body."""
    login_response = await _authorize_and_login(
        client,
        client_id=client_id,
        code_challenge=code_challenge,
        redirect_uri=redirect_uri,
        resource=resource,
        scope=scope,
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
            "client_secret": client_secret,
            "code_verifier": code_verifier,
        },
    )
    assert token_response.status_code == 200, token_response.text
    body = token_response.json()
    assert isinstance(body, dict)
    return body


# --- DCR: the recorded request, registered and echoed byte-exact ----------------


async def test_openwebui_dcr_registers_a_confidential_client_matching_the_recorded_request(
    bare_remote: Path, tmp_path: Path, test_database_url: str, pool: asyncpg.Pool
) -> None:
    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    async with _running_app(environ, config, authenticator=FakeAuthenticator()) as (_app, client):
        registered = await _register_openwebui_client(client)
        client_id = registered["client_id"]
        client_secret = registered["client_secret"]
        assert client_secret
        assert registered["token_endpoint_auth_method"] == "client_secret_post"  # noqa: S105
        assert registered["redirect_uris"] == [_OPENWEBUI_REDIRECT_URI]
        assert registered["scope"] == _OPENWEBUI_SCOPE

        metadata_response = await client.get("/.well-known/oauth-authorization-server")
    assert metadata_response.status_code == 200
    metadata = metadata_response.json()
    assert "client_secret_post" in metadata["token_endpoint_auth_methods_supported"]

    row = await pool.fetchrow(
        "select client_info from oauth_clients where client_id = $1", client_id
    )
    assert row is not None
    assert client_secret not in row["client_info"]


# --- Full code flow: label and commit author ------------------------------------


async def test_openwebui_completes_the_code_flow_and_commits_as_openwebui(
    bare_remote: Path, tmp_path: Path, test_database_url: str, pool: asyncpg.Pool
) -> None:
    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    async with _running_app(environ, config, authenticator=FakeAuthenticator()) as (app, client):
        registered = await _register_openwebui_client(client)
        client_id = registered["client_id"]
        client_secret = registered["client_secret"]
        code_verifier, code_challenge = _pkce_pair()

        tokens = await _complete_openwebui_login(
            client,
            client_id=client_id,
            client_secret=client_secret,
            code_verifier=code_verifier,
            code_challenge=code_challenge,
        )
        assert tokens["access_token"].startswith(OAUTH_ACCESS_TOKEN_PREFIX)
        assert tokens["refresh_token"].startswith("mmr_")

        label = await pool.fetchval(
            "select client_label from oauth_tokens where client_id = $1 and kind = 'access'",
            client_id,
        )
        assert label == "openwebui"

        content = "---\ntitle: A note\ndescription: A description.\ntype: fact\n---\nBody.\n"
        async with _authed_mcp_client(app, config, tokens["access_token"]) as mcp_client:
            result = await mcp_client.call_tool(
                "memory_write",
                {"path": "personal/fact/a.md", "content": content, "if_version": "new"},
            )
        assert result.is_error is False

    assert _commit_author(bare_remote, "HEAD") == "openwebui <openwebui@memory-manager.invalid>"


# --- client_secret_post is mandatory at /token -----------------------------------


async def test_openwebui_token_exchange_requires_the_registered_secret(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    async with _running_app(environ, config, authenticator=FakeAuthenticator()) as (_app, client):
        registered = await _register_openwebui_client(client)
        client_id = registered["client_id"]
        client_secret = registered["client_secret"]
        code_verifier, code_challenge = _pkce_pair()
        login_response = await _authorize_and_login(
            client, client_id=client_id, code_challenge=code_challenge
        )
        code = parse_qs(urlsplit(_location(login_response)).query)["code"][0]
        token_data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _OPENWEBUI_REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": code_verifier,
        }

        missing_secret_response = await client.post("/token", data=token_data)
        assert missing_secret_response.status_code == 401
        assert missing_secret_response.json()["error"] == "invalid_client"

        wrong_secret_response = await client.post(
            "/token", data={**token_data, "client_secret": "not-the-right-secret"}
        )
        assert wrong_secret_response.status_code == 401
        assert wrong_secret_response.json()["error"] == "invalid_client"

        right_secret_response = await client.post(
            "/token", data={**token_data, "client_secret": client_secret}
        )
        assert right_secret_response.status_code == 200, right_secret_response.text


# --- Exact-match redirect URI, both negative shapes ------------------------------


async def test_openwebui_authorize_rejects_a_redirect_uri_not_exactly_registered(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    async with _running_app(environ, config, authenticator=FakeAuthenticator()) as (_app, client):
        registered = await _register_openwebui_client(client)
        client_id = registered["client_id"]
        _verifier, challenge = _pkce_pair()

        different_connection = await _authorize_openwebui(
            client,
            client_id=client_id,
            code_challenge=challenge,
            redirect_uri=f"{_WEBUI_URL}/oauth/clients/mcp:other/callback",
        )
        assert different_connection.status_code == 400
        assert different_connection.json()["error"] == "invalid_request"

        appended_segment = await _authorize_openwebui(
            client,
            client_id=client_id,
            code_challenge=challenge,
            redirect_uri=f"{_OPENWEBUI_REDIRECT_URI}/extra",
        )
        assert appended_segment.status_code == 400
        assert appended_segment.json()["error"] == "invalid_request"


# --- Refresh: rotates, keeps the label, requires the secret ---------------------


async def test_openwebui_refresh_rotates_keeps_the_label_and_requires_the_secret(
    bare_remote: Path, tmp_path: Path, test_database_url: str, pool: asyncpg.Pool
) -> None:
    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    async with _running_app(environ, config, authenticator=FakeAuthenticator()) as (app, client):
        registered = await _register_openwebui_client(client)
        client_id = registered["client_id"]
        client_secret = registered["client_secret"]
        code_verifier, code_challenge = _pkce_pair()
        tokens = await _complete_openwebui_login(
            client,
            client_id=client_id,
            client_secret=client_secret,
            code_verifier=code_verifier,
            code_challenge=code_challenge,
        )
        old_refresh = tokens["refresh_token"]
        # Open WebUI's own refresh request shape (`utils/oauth.py#L1146-1164`):
        # `client_id`, `client_secret`, `resource` - `scope` only with
        # `OAUTH_REFRESH_TOKEN_INCLUDE_SCOPE`, omitted here as it is by default.
        refresh_data = {
            "grant_type": "refresh_token",
            "refresh_token": old_refresh,
            "client_id": client_id,
            "resource": _RESOURCE,
        }

        wrong_secret_response = await client.post(
            "/token", data={**refresh_data, "client_secret": "not-the-right-secret"}
        )
        assert wrong_secret_response.status_code == 401
        assert wrong_secret_response.json()["error"] == "invalid_client"

        # The failed attempt above must not have consumed the refresh token - the
        # correct secret still rotates it successfully right after.
        refreshed_response = await client.post(
            "/token", data={**refresh_data, "client_secret": client_secret}
        )
        assert refreshed_response.status_code == 200, refreshed_response.text
        refreshed = refreshed_response.json()
        assert refreshed["access_token"] != tokens["access_token"]
        assert refreshed["refresh_token"] != old_refresh

        label = await pool.fetchval(
            "select client_label from oauth_tokens where client_id = $1 and kind = 'access' "
            "order by created_at desc limit 1",
            client_id,
        )
        assert label == "openwebui"

        async with _authed_mcp_client(app, config, refreshed["access_token"]) as mcp_client:
            result = await mcp_client.call_tool("memory_index", {})
            assert result.is_error is False


# --- Refresh-token replay: observed, not changed (Nicht dabei) ------------------


async def test_openwebui_refresh_token_replay_revokes_the_family(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    """Observation only: the sequential-replay-revokes-the-family behaviour (ADR-0004)
    is unchanged by #143 - only Open WebUI's own client shape is new in this file. The
    concurrent-refresh TOCTOU and Open WebUI's lock-free early refresh race are separate,
    already-tracked issues (this task's "Nicht dabei"), not exercised here."""
    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    async with _running_app(environ, config, authenticator=FakeAuthenticator()) as (app, client):
        registered = await _register_openwebui_client(client)
        client_id = registered["client_id"]
        client_secret = registered["client_secret"]
        code_verifier, code_challenge = _pkce_pair()
        tokens = await _complete_openwebui_login(
            client,
            client_id=client_id,
            client_secret=client_secret,
            code_verifier=code_verifier,
            code_challenge=code_challenge,
        )
        old_refresh = tokens["refresh_token"]
        refresh_data = {
            "grant_type": "refresh_token",
            "refresh_token": old_refresh,
            "client_id": client_id,
            "client_secret": client_secret,
        }

        first_response = await client.post("/token", data=refresh_data)
        assert first_response.status_code == 200, first_response.text
        new_access = first_response.json()["access_token"]

        replay_response = await client.post("/token", data=refresh_data)
        assert replay_response.status_code == 400
        assert replay_response.json()["error"] == "invalid_grant"

        with pytest.raises(Exception):  # noqa: B017 - raised by the SDK client's own session
            async with _authed_mcp_client(app, config, new_access) as mcp_client:
                await mcp_client.call_tool("memory_index", {})


# --- Revocation: client_secret_post revokes the whole family --------------------


async def test_openwebui_revoke_with_client_secret_revokes_the_family(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    async with _running_app(environ, config, authenticator=FakeAuthenticator()) as (app, client):
        registered = await _register_openwebui_client(client)
        client_id = registered["client_id"]
        client_secret = registered["client_secret"]
        code_verifier, code_challenge = _pkce_pair()
        tokens = await _complete_openwebui_login(
            client,
            client_id=client_id,
            client_secret=client_secret,
            code_verifier=code_verifier,
            code_challenge=code_challenge,
        )

        revoke_response = await client.post(
            "/revoke",
            data={
                "token": tokens["refresh_token"],
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )
        assert revoke_response.status_code == 200

        with pytest.raises(Exception):  # noqa: B017 - raised by the SDK client's own session
            async with _authed_mcp_client(app, config, tokens["access_token"]) as mcp_client:
                await mcp_client.call_tool("memory_index", {})


# --- Two users, one DCR client: independent families -----------------------------


async def test_openwebui_two_users_on_one_client_have_independent_families(
    bare_remote: Path, tmp_path: Path, test_database_url: str, pool: asyncpg.Pool
) -> None:
    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    authenticator = FakeAuthenticator(subject="alice", namespaces=["personal"])
    async with _running_app(environ, config, authenticator=authenticator) as (app, client):
        registered = await _register_openwebui_client(client)
        client_id = registered["client_id"]
        client_secret = registered["client_secret"]

        verifier_a, challenge_a = _pkce_pair()
        tokens_a = await _complete_openwebui_login(
            client,
            client_id=client_id,
            client_secret=client_secret,
            code_verifier=verifier_a,
            code_challenge=challenge_a,
            state="state-alice",
        )

        authenticator.subject = "bob"
        authenticator.namespaces = ["bob"]
        verifier_b, challenge_b = _pkce_pair()
        tokens_b = await _complete_openwebui_login(
            client,
            client_id=client_id,
            client_secret=client_secret,
            code_verifier=verifier_b,
            code_challenge=challenge_b,
            state="state-bob",
        )

        rows = await pool.fetch(
            "select subject, namespaces, family_id from oauth_tokens "
            "where client_id = $1 and kind = 'access'",
            client_id,
        )
        by_subject = {row["subject"]: row for row in rows}
        assert by_subject["alice"]["namespaces"] == ["personal"]
        assert by_subject["bob"]["namespaces"] == ["bob"]
        assert by_subject["alice"]["family_id"] != by_subject["bob"]["family_id"]

        revoke_response = await client.post(
            "/revoke",
            data={
                "token": tokens_a["access_token"],
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )
        assert revoke_response.status_code == 200

        async with _authed_mcp_client(app, config, tokens_b["access_token"]) as mcp_client:
            result = await mcp_client.call_tool("memory_index", {})
            assert result.is_error is False

        with pytest.raises(Exception):  # noqa: B017 - raised by the SDK client's own session
            async with _authed_mcp_client(app, config, tokens_a["access_token"]) as mcp_client:
                await mcp_client.call_tool("memory_index", {})
