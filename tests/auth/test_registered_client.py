# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for operator-registered confidential OAuth clients (ADR-0015, #197).

`FakeAuthenticator`/`_running_app`/`_pkce_pair`/`_authorize_and_login`/`_authed_mcp_client`
are copied from `tests/auth/test_oauth_flow.py` (that file's own docstring explains each) -
this file drives the same real HTTP surface (`memory_manager.http.create_app`), just for a
client `auth.registered_clients.create` registered instead of one `/register` (DCR) or a
CIMD document produced.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
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
from memory_manager.auth import store
from memory_manager.auth.login import BoundCompleter, PendingAuthorization
from memory_manager.auth.registered_clients import AUTH_METHOD_BASIC, AUTH_METHOD_POST
from memory_manager.auth.registered_clients import create as create_registered_client
from memory_manager.auth.registered_clients import list_clients as list_registered_clients
from memory_manager.auth.registered_clients import revoke as revoke_registered_client
from memory_manager.auth.store import ClientSecretCipher
from memory_manager.auth.verifier import OAUTH_ACCESS_TOKEN_PREFIX, verify_bearer_token
from memory_manager.cli import main
from memory_manager.config import ServerConfig
from memory_manager.db.migrate import migrate
from memory_manager.http import create_app

_PUBLIC_URL = "https://mm.example.test"
_MCP_PATH = "/mcp"
_RESOURCE = f"{_PUBLIC_URL}{_MCP_PATH}"
_REDIRECT_URI = "https://gemini.example.test/callback"
_OTHER_REDIRECT_URI = "https://gemini.example.test/callback-other"
_SUBJECT = "alice"
_NAMESPACES = ["personal"]

#: A Fernet key, generated once for this test module only (`Fernet.generate_key()`) - not a
#: credential protecting anything real, just what `OAUTH_CLIENT_SECRET_KEY` requires.
_CLIENT_SECRET_KEY = "Wsm3C2WXurw049qRl3jAMMLNSLwGZt7A9e_c5FgksFg="  # noqa: S105


class FakeAuthenticator:
    """Completes every pending authorization immediately as `subject`/`namespaces` - the
    test-only stand-in `create_app`'s docstring describes."""

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


async def _code_for(
    client: httpx.AsyncClient, *, client_id: str, code_challenge: str, redirect_uri: str
) -> str:
    login_response = await _authorize_and_login(
        client, client_id=client_id, code_challenge=code_challenge, redirect_uri=redirect_uri
    )
    assert login_response.status_code == 302, login_response.text
    query = parse_qs(urlsplit(_location(login_response)).query)
    return query["code"][0]


def _token_request(
    *, code: str, redirect_uri: str, client_id: str, code_verifier: str
) -> dict[str, str]:
    return {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }


def _basic_auth_header(client_id: str, secret: str) -> dict[str, str]:
    encoded = base64.b64encode(f"{client_id}:{secret}".encode()).decode("ascii")
    return {"Authorization": f"Basic {encoded}"}


@pytest.mark.parametrize("auth_method", [AUTH_METHOD_POST, AUTH_METHOD_BASIC])
async def test_registered_client_code_flow_reaches_a_tool_call(
    bare_remote: Path,
    tmp_path: Path,
    test_database_url: str,
    pool: asyncpg.Pool,
    auth_method: str,
) -> None:
    cipher = ClientSecretCipher(_CLIENT_SECRET_KEY)
    client_id, secret = await create_registered_client(
        pool, "gemini", redirect_uris=[_REDIRECT_URI], cipher=cipher, auth_method=auth_method
    )

    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    async with _running_app(environ, config, authenticator=FakeAuthenticator()) as (app, client):
        code_verifier, code_challenge = _pkce_pair()
        code = await _code_for(
            client, client_id=client_id, code_challenge=code_challenge, redirect_uri=_REDIRECT_URI
        )
        token_data = _token_request(
            code=code, redirect_uri=_REDIRECT_URI, client_id=client_id, code_verifier=code_verifier
        )
        if auth_method == AUTH_METHOD_BASIC:
            token_response = await client.post(
                "/token", data=token_data, headers=_basic_auth_header(client_id, secret)
            )
        else:
            token_response = await client.post(
                "/token", data={**token_data, "client_secret": secret}
            )
        assert token_response.status_code == 200, token_response.text
        body: dict[str, Any] = token_response.json()
        access_token = body["access_token"]
        assert access_token.startswith(OAUTH_ACCESS_TOKEN_PREFIX)

        async with _authed_mcp_client(app, config, access_token) as mcp_client:
            tools = await mcp_client.list_tools()
            assert tools.tools


async def test_registered_client_token_rejects_a_wrong_or_missing_secret(
    bare_remote: Path, tmp_path: Path, test_database_url: str, pool: asyncpg.Pool
) -> None:
    cipher = ClientSecretCipher(_CLIENT_SECRET_KEY)
    client_id, secret = await create_registered_client(
        pool, "gemini", redirect_uris=[_REDIRECT_URI], cipher=cipher
    )

    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    async with _running_app(environ, config, authenticator=FakeAuthenticator()) as (_app, client):
        code_verifier, code_challenge = _pkce_pair()
        code = await _code_for(
            client, client_id=client_id, code_challenge=code_challenge, redirect_uri=_REDIRECT_URI
        )
        token_data = _token_request(
            code=code, redirect_uri=_REDIRECT_URI, client_id=client_id, code_verifier=code_verifier
        )

        wrong = await client.post("/token", data={**token_data, "client_secret": "not-it"})
        assert wrong.status_code == 401
        assert wrong.json()["error"] == "invalid_client"

        missing = await client.post("/token", data=token_data)
        assert missing.status_code == 401
        assert missing.json()["error"] == "invalid_client"

        right = await client.post("/token", data={**token_data, "client_secret": secret})
        assert right.status_code == 200, right.text


async def test_registered_client_authorize_rejects_an_unregistered_or_different_redirect(
    bare_remote: Path, tmp_path: Path, test_database_url: str, pool: asyncpg.Pool
) -> None:
    cipher = ClientSecretCipher(_CLIENT_SECRET_KEY)
    client_id, _secret = await create_registered_client(
        pool, "gemini", redirect_uris=[_REDIRECT_URI], cipher=cipher
    )

    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    async with _running_app(environ, config, authenticator=FakeAuthenticator()) as (_app, client):
        _verifier, code_challenge = _pkce_pair()

        different = await client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": _OTHER_REDIRECT_URI,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "resource": _RESOURCE,
            },
        )
        assert different.status_code == 400, different.text
        assert different.json()["error"] == "invalid_request"

        unregistered_client = await client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": "mmc_" + secrets.token_hex(8),
                "redirect_uri": _REDIRECT_URI,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "resource": _RESOURCE,
            },
        )
        assert unregistered_client.status_code == 400, unregistered_client.text
        assert unregistered_client.json()["error"] == "invalid_request"


async def test_registered_client_authorize_without_code_challenge_is_rejected(
    bare_remote: Path, tmp_path: Path, test_database_url: str, pool: asyncpg.Pool
) -> None:
    """PKCE S256 stays mandatory (ADR-0004): omitting `code_challenge` never produces a
    usable authorization code, whether the SDK answers with a direct error or a redirect
    carrying the error (both outcomes are valid shapes for the same rejection, depending on
    whether `redirect_uri` could itself be re-validated for the error response)."""
    cipher = ClientSecretCipher(_CLIENT_SECRET_KEY)
    client_id, _secret = await create_registered_client(
        pool, "gemini", redirect_uris=[_REDIRECT_URI], cipher=cipher
    )

    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    async with _running_app(environ, config, authenticator=FakeAuthenticator()) as (_app, client):
        response = await client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": _REDIRECT_URI,
                "resource": _RESOURCE,
            },
        )
        if response.status_code == 302:
            query = parse_qs(urlsplit(_location(response)).query)
            assert query["error"] == ["invalid_request"]
        else:
            assert response.status_code == 400, response.text
            assert response.json()["error"] == "invalid_request"


async def test_registered_client_revoked_rejects_authorize_refresh_and_access_token(
    bare_remote: Path, tmp_path: Path, test_database_url: str, pool: asyncpg.Pool
) -> None:
    cipher = ClientSecretCipher(_CLIENT_SECRET_KEY)
    client_id, secret = await create_registered_client(
        pool, "gemini", redirect_uris=[_REDIRECT_URI], cipher=cipher
    )

    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    async with _running_app(environ, config, authenticator=FakeAuthenticator()) as (_app, client):
        code_verifier, code_challenge = _pkce_pair()
        code = await _code_for(
            client, client_id=client_id, code_challenge=code_challenge, redirect_uri=_REDIRECT_URI
        )
        token_data = _token_request(
            code=code, redirect_uri=_REDIRECT_URI, client_id=client_id, code_verifier=code_verifier
        )
        token_response = await client.post("/token", data={**token_data, "client_secret": secret})
        assert token_response.status_code == 200, token_response.text
        body: dict[str, Any] = token_response.json()
        access_token = body["access_token"]
        refresh_token = body["refresh_token"]

        revoked_tokens = await revoke_registered_client(pool, client_id)
        assert revoked_tokens == 2

        assert await verify_bearer_token(pool, access_token, oauth_resource=_RESOURCE) is None

        authorize_response = await client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": _REDIRECT_URI,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "resource": _RESOURCE,
            },
        )
        assert authorize_response.status_code == 400, authorize_response.text

        refresh_response = await client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": secret,
            },
        )
        assert refresh_response.status_code == 401, refresh_response.text
        assert refresh_response.json()["error"] == "invalid_client"


async def test_registered_client_cleanup_spares_it_but_removes_an_equally_old_dcr_client(
    pool: asyncpg.Pool,
) -> None:
    cipher = ClientSecretCipher(_CLIENT_SECRET_KEY)
    registered_id, _secret = await create_registered_client(
        pool, "gemini", redirect_uris=[_REDIRECT_URI], cipher=cipher
    )
    dcr_id = str(secrets.token_hex(16))
    await store.save_client(
        pool,
        dcr_id,
        {
            "client_id": dcr_id,
            "redirect_uris": [_REDIRECT_URI],
            "token_endpoint_auth_method": "none",
        },
        cipher=cipher,
    )

    aged = datetime.now(UTC) - timedelta(days=31)
    await pool.execute(
        "update oauth_clients set created_at = $1 where client_id = any($2::text[])",
        aged,
        [registered_id, dcr_id],
    )

    stats = await store.cleanup(pool)
    assert stats.clients == 1

    remaining_rows = await pool.fetch("select client_id from oauth_clients")
    remaining = {row["client_id"] for row in remaining_rows}
    assert registered_id in remaining
    assert dcr_id not in remaining


async def test_registered_client_secret_is_encrypted_at_rest(pool: asyncpg.Pool) -> None:
    cipher = ClientSecretCipher(_CLIENT_SECRET_KEY)
    client_id, secret = await create_registered_client(
        pool, "gemini", redirect_uris=[_REDIRECT_URI], cipher=cipher
    )

    row = await pool.fetchrow(
        "select client_info from oauth_clients where client_id = $1", client_id
    )
    assert row is not None
    assert secret not in row["client_info"]

    info = await store.get_client(pool, client_id, cipher=cipher)
    assert info is not None
    assert info["client_secret"] == secret


async def test_registered_client_audit_entries_are_one_per_action_and_never_carry_the_secret(
    pool: asyncpg.Pool,
) -> None:
    cipher = ClientSecretCipher(_CLIENT_SECRET_KEY)
    client_id, secret = await create_registered_client(
        pool, "gemini", redirect_uris=[_REDIRECT_URI], cipher=cipher
    )
    await revoke_registered_client(pool, client_id)

    rows = await pool.fetch(
        "select op, detail from audit_log "
        "where op in ('oauth_client_create', 'oauth_client_revoke') order by at"
    )
    assert [row["op"] for row in rows] == ["oauth_client_create", "oauth_client_revoke"]
    for row in rows:
        assert secret not in row["detail"]


def test_registered_client_cli_round_trip(
    monkeypatch: pytest.MonkeyPatch, test_database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", test_database_url)
    monkeypatch.setenv("OAUTH_CLIENT_SECRET_KEY", _CLIENT_SECRET_KEY)

    create_exit = main(["oauth-client", "create", "gemini", "--redirect-uri", _REDIRECT_URI])
    assert create_exit == 0
    created = capsys.readouterr()
    client_id, secret = created.out.strip().split("\t")
    assert client_id.startswith("mmc_")
    assert "will not be shown again" in created.err

    list_exit = main(["oauth-client", "list"])
    assert list_exit == 0
    listed_out = capsys.readouterr().out
    assert client_id in listed_out
    assert secret not in listed_out

    assert main(["oauth-client", "revoke", client_id]) == 0
    assert main(["oauth-client", "revoke", client_id]) == 2


def test_registered_client_cli_requires_oauth_client_secret_key(
    monkeypatch: pytest.MonkeyPatch, test_database_url: str
) -> None:
    monkeypatch.setenv("DATABASE_URL", test_database_url)
    monkeypatch.delenv("OAUTH_CLIENT_SECRET_KEY", raising=False)

    assert main(["oauth-client", "create", "gemini", "--redirect-uri", _REDIRECT_URI]) == 2

    monkeypatch.setenv("OAUTH_CLIENT_SECRET_KEY", "not-a-fernet-key")
    assert main(["oauth-client", "create", "gemini", "--redirect-uri", _REDIRECT_URI]) == 2


async def test_registered_client_store_works_against_backend_postgres(
    test_database_url: str,
) -> None:
    """`migrate(conn, backend="postgres")` applies `0026_preregistered_clients.sql` exactly
    like `backend="git"` does - `oauth_clients` is shared by both backends, unlike the
    `postgres`-only `chunks` layout (ADR-0016)."""
    conn = await asyncpg.connect(test_database_url)
    try:
        await migrate(conn, backend="postgres")
    finally:
        await conn.close()

    created_pool = await asyncpg.create_pool(test_database_url)
    try:
        cipher = ClientSecretCipher(_CLIENT_SECRET_KEY)
        client_id, secret = await create_registered_client(
            created_pool, "gemini", redirect_uris=[_REDIRECT_URI], cipher=cipher
        )
        info = await store.get_client(created_pool, client_id, cipher=cipher)
        assert info is not None
        assert info["client_secret"] == secret

        clients = await list_registered_clients(created_pool)
        assert [c.client_id for c in clients] == [client_id]
    finally:
        await created_pool.close()
