# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for `LOGIN_MODE=entra` (ADR-0006, #215): `auth.login_entra.
EntraAuthenticator`, driven against `tests/mock_idp`'s in-process ASGI app
(`mock_idp_client`, `tests/mock_idp_fixtures.py`) - the same mock
`tests/auth/test_graph.py` already proves behaves like `docs/research/
entra-contract.md`, now wired in as this authenticator's own `http_client`
(both the Entra OIDC endpoints and the Graph endpoints live on that one app,
the same way a real deployment's `ENTRA_AUTHORITY`/`ENTRA_GRAPH_URL` are two
paths under one tenant).

Every full-flow test needs a real Postgres (`STORAGE_BACKEND=postgres`,
ADR-0006 addendum): `entra_environ` builds a disposable app role the same
way `tests/mcp/conftest.py`'s `services_with_postgres_backend` does, since
`EntraAuthenticator.handle_callback` writes `users`/`user_groups`
(ADR-0008 addendum) through `services.pool`.

"Wrong `aud`" cannot be produced through a real round trip at all: the
mock's own token endpoint ties a code's resulting ID token's `aud` to
whichever client authenticated the exchange, and the facade always
authenticates as its own configured client - so `TestValidateIdToken` below
is a direct, white-box test of `_validate_id_token` instead. Every other
ADR-0006 item 1 check ("foreign tenant" via `tid`, "replayed/mismatched
nonce") is still driven end to end.
"""

from __future__ import annotations

import base64
import hashlib
import html
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import asyncpg
import httpx
import pytest
import pytest_asyncio
from mock_idp_fixtures import mock_idp_client
from starlette.applications import Starlette

from memory_manager.app import open_services
from memory_manager.auth import store
from memory_manager.auth.login import LOGIN_PATH
from memory_manager.auth.login_entra import CALLBACK_PATH, EntraAuthenticator, _validate_id_token
from memory_manager.auth.users import get_user, group_ids
from memory_manager.config import ServerConfig, ServerConfigError
from memory_manager.http import build_authenticator, create_app

__all__ = ["mock_idp_client"]

_PUBLIC_URL = "https://mm.example.test"
_MCP_PATH = "/mcp"
_REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"
_CLIENT_SECRET_KEY = "f2F7cFcySJJZnWoa6ZFYlsU-MQSxl8_Fw6bT_DLoLJ0="  # noqa: S105 - fake test key

_AUTHORITY = "https://mock-idp.test"
_GRAPH_URL = "https://mock-idp.test/graph/v1.0"
_TID = "33333333-3333-3333-3333-333333333333"
_ENTRA_CLIENT_ID = "44444444-4444-4444-4444-444444444444"
_ENTRA_CLIENT_SECRET = "mock-entra-client-secret"  # noqa: S105 - fake test credential


def _config() -> ServerConfig:
    return ServerConfig(
        public_url=_PUBLIC_URL, mcp_path=_MCP_PATH, oauth_client_secret_key=_CLIENT_SECRET_KEY
    )


@pytest_asyncio.fixture
async def entra_environ(
    admin_database_url: str, test_database_url: str
) -> AsyncIterator[dict[str, str]]:
    """`STORAGE_BACKEND=postgres` plus a disposable `DATABASE_APP_ROLE` - the same
    shape `tests/mcp/conftest.py`'s `services_with_postgres_backend` builds, since
    `open_services` requires one for this backend (ADR-0008 addendum, #116)."""
    role = f"mm_test_entra_{secrets.token_hex(8)}"
    admin_conn = await asyncpg.connect(admin_database_url)
    try:
        await admin_conn.execute(f'create role "{role}" nologin nosuperuser nobypassrls')
    finally:
        await admin_conn.close()
    try:
        yield {
            "STORAGE_BACKEND": "postgres",
            "DATABASE_URL": test_database_url,
            "DATABASE_APP_ROLE": role,
        }
    finally:
        owned_conn: asyncpg.Connection | None
        try:
            owned_conn = await asyncpg.connect(test_database_url)
        except asyncpg.PostgresError:
            owned_conn = None
        if owned_conn is not None:
            try:
                await owned_conn.execute(f'drop owned by "{role}"')
            finally:
                await owned_conn.close()
        admin_conn = await asyncpg.connect(admin_database_url)
        try:
            await admin_conn.execute(f'drop role if exists "{role}"')
        finally:
            await admin_conn.close()


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


def _entra_authenticator(
    mock_idp_client: httpx.AsyncClient,
    *,
    tenant_id: str = _TID,
    client_id: str = _ENTRA_CLIENT_ID,
    client_secret: str = _ENTRA_CLIENT_SECRET,
    allowed_tenants: frozenset[str] | None = None,
) -> EntraAuthenticator:
    return EntraAuthenticator(
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=f"{_PUBLIC_URL}{CALLBACK_PATH}",
        authority=_AUTHORITY,
        graph_url=_GRAPH_URL,
        allowed_tenants=allowed_tenants,
        http_client=mock_idp_client,
    )


async def _register_entra_client(
    mock_idp_client: httpx.AsyncClient,
    *,
    tenant_id: str = _TID,
    client_id: str = _ENTRA_CLIENT_ID,
    client_secret: str = _ENTRA_CLIENT_SECRET,
    redirect_uri: str = f"{_PUBLIC_URL}{CALLBACK_PATH}",
    graph_roles: list[str] | None = None,
) -> None:
    response = await mock_idp_client.post(
        "/_mock/clients",
        json={
            "tid": tenant_id,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uris": [redirect_uri],
            "graph_roles": graph_roles or [],
        },
    )
    assert response.status_code == 201, response.text


async def _create_entra_user(
    mock_idp_client: httpx.AsyncClient, oid: str, *, tenant_id: str = _TID, **fields: Any
) -> None:
    response = await mock_idp_client.post(
        "/_mock/users", json={"tid": tenant_id, "oid": oid, **fields}
    )
    assert response.status_code == 201, response.text


async def _select_signin(
    mock_idp_client: httpx.AsyncClient, oid: str, *, tenant_id: str = _TID
) -> None:
    response = await mock_idp_client.post(
        "/_mock/select-signin", json={"tid": tenant_id, "oid": oid}
    )
    assert response.status_code == 200, response.text


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


async def _register_mcp_client(
    client: httpx.AsyncClient, *, redirect_uri: str = _REDIRECT_URI
) -> str:
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


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


async def _start_authorize(
    client: httpx.AsyncClient,
    *,
    client_id: str,
    code_challenge: str,
    redirect_uri: str = _REDIRECT_URI,
    state: str = "xyz",
) -> httpx.Response:
    authorize_response = await client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
            "resource": f"{_PUBLIC_URL}{_MCP_PATH}",
        },
    )
    assert authorize_response.status_code == 302, authorize_response.text
    login_url = _location(authorize_response)
    assert urlsplit(login_url).path == LOGIN_PATH
    return await client.get(login_url)


async def _confirm_interstitial(
    client: httpx.AsyncClient, interstitial_response: httpx.Response
) -> httpx.Response:
    """The "Continue to sign in" click: `GET` the link `oidc_interstitial_page`
    rendered (same marker `test_login.py`'s own `_continue_url_from` looks for)."""
    assert interstitial_response.status_code == 200, interstitial_response.text
    marker = 'class="button" href="'
    start = interstitial_response.text.index(marker) + len(marker)
    end = interstitial_response.text.index('"', start)
    continue_url = html.unescape(interstitial_response.text[start:end])
    return await client.get(continue_url)


async def _drive_entra_login(
    client: httpx.AsyncClient,
    mock_idp_client: httpx.AsyncClient,
    *,
    client_id: str,
    code_challenge: str,
    redirect_uri: str = _REDIRECT_URI,
    nonce_override: str | None = None,
) -> httpx.Response:
    """`/authorize` -> `/login` (interstitial) -> "Continue to sign in" (a redirect to
    the mock's own `/authorize`, requires `_select_signin` to already have run) -> the
    mock's own redirect back to `{CALLBACK_PATH}`, replayed as a direct `GET` against
    `client` (the same shape `test_login.py`'s own `_drive_to_callback` uses for its
    fake OIDC provider) -> the facade's own callback response.

    `nonce_override`, when given, replaces the `nonce` `EntraAuthenticator.handle`
    generated before the mock ever sees the request - proving `handle_callback`'s own
    nonce check independently of what the facade itself sent.
    """
    interstitial_response = await _start_authorize(
        client, client_id=client_id, code_challenge=code_challenge, redirect_uri=redirect_uri
    )
    redirect_response = await _confirm_interstitial(client, interstitial_response)
    assert redirect_response.status_code == 302, redirect_response.text
    mock_authorize_url = _location(redirect_response)

    if nonce_override is not None:
        parsed = urlsplit(mock_authorize_url)
        params = parse_qs(parsed.query)
        params["nonce"] = [nonce_override]
        mock_authorize_url = urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, urlencode(params, doseq=True), "")
        )

    mock_redirect = await mock_idp_client.get(mock_authorize_url)
    assert mock_redirect.status_code == 302, mock_redirect.text
    callback_params = {key: values[0] for key, values in _query(_location(mock_redirect)).items()}

    return await client.get(CALLBACK_PATH, params=callback_params)


# === EntraAuthenticator construction ================================================


class TestEntraAuthenticatorFromEnv:
    _BASE_ENVIRON: ClassVar[dict[str, str]] = {
        "ENTRA_TENANT_ID": _TID,
        "ENTRA_CLIENT_ID": _ENTRA_CLIENT_ID,
        "ENTRA_CLIENT_SECRET": _ENTRA_CLIENT_SECRET,
        "PUBLIC_URL": _PUBLIC_URL,
    }

    def test_requires_every_core_variable(self) -> None:
        with pytest.raises(ServerConfigError, match="ENTRA_TENANT_ID"):
            EntraAuthenticator.from_env(
                {k: v for k, v in self._BASE_ENVIRON.items() if k != "ENTRA_TENANT_ID"}
            )

    def test_builds_with_the_core_variables(self) -> None:
        authenticator = EntraAuthenticator.from_env(dict(self._BASE_ENVIRON))
        assert isinstance(authenticator, EntraAuthenticator)


def test_constructor_rejects_non_https_authority_without_opt_in() -> None:
    with pytest.raises(ServerConfigError):
        EntraAuthenticator(
            tenant_id=_TID,
            client_id=_ENTRA_CLIENT_ID,
            client_secret=_ENTRA_CLIENT_SECRET,
            redirect_uri=f"{_PUBLIC_URL}{CALLBACK_PATH}",
            authority="http://insecure.example.test",
        )


def test_constructor_allows_non_https_authority_with_opt_in() -> None:
    authenticator = EntraAuthenticator(
        tenant_id=_TID,
        client_id=_ENTRA_CLIENT_ID,
        client_secret=_ENTRA_CLIENT_SECRET,
        redirect_uri=f"{_PUBLIC_URL}{CALLBACK_PATH}",
        authority="http://insecure.example.test",
        allow_insecure_authority=True,
    )
    assert authenticator is not None


async def test_build_authenticator_refuses_entra_without_postgres_backend() -> None:
    config = ServerConfig(public_url=_PUBLIC_URL, login_mode="entra")
    environ = {
        "ENTRA_TENANT_ID": _TID,
        "ENTRA_CLIENT_ID": _ENTRA_CLIENT_ID,
        "ENTRA_CLIENT_SECRET": _ENTRA_CLIENT_SECRET,
        "PUBLIC_URL": _PUBLIC_URL,
        "STORAGE_BACKEND": "git",
    }
    with pytest.raises(ServerConfigError, match="STORAGE_BACKEND=postgres"):
        build_authenticator(config, environ)


# === ID token claim validation (white-box, see module docstring on "wrong aud") =====


class TestValidateIdToken:
    _ISSUER = f"{_AUTHORITY}/{_TID}/v2.0"
    _NOW = 1_700_000_000.0

    def _claims(self, **overrides: Any) -> dict[str, Any]:
        claims: dict[str, Any] = {
            "iss": self._ISSUER,
            "tid": _TID,
            "aud": _ENTRA_CLIENT_ID,
            "exp": self._NOW + 300,
            "nonce": "expected-nonce",
        }
        claims.update(overrides)
        return claims

    def _validate(self, claims: dict[str, Any]) -> str | None:
        return _validate_id_token(
            claims,
            issuer=self._ISSUER,
            client_id=_ENTRA_CLIENT_ID,
            allowed_tenants=frozenset({_TID}),
            expected_nonce="expected-nonce",
            now=self._NOW,
        )

    def test_accepts_valid_claims(self) -> None:
        assert self._validate(self._claims()) is None

    def test_rejects_wrong_audience(self) -> None:
        assert self._validate(self._claims(aud="someone-elses-client")) == "aud"

    def test_rejects_an_expired_token(self) -> None:
        assert self._validate(self._claims(exp=self._NOW - 1)) == "exp"

    def test_rejects_an_issuer_mismatch(self) -> None:
        assert self._validate(self._claims(iss="https://a-different-issuer.test")) == "iss"


# === Full HTTP flow ==================================================================


async def test_entra_happy_path_with_claim_groups_binds_the_token(
    entra_environ: dict[str, str],
    test_database_url: str,
    pool: asyncpg.Pool,
    mock_idp_client: httpx.AsyncClient,
) -> None:
    config = _config()
    authenticator = _entra_authenticator(mock_idp_client)
    await _register_entra_client(mock_idp_client)
    await _create_entra_user(
        mock_idp_client,
        "user-1",
        display_name="Ada Lovelace",
        roles=["Memory.User"],
        groups=["group-a", "group-b"],
    )
    await _select_signin(mock_idp_client, "user-1")

    async with _running_app(entra_environ, config, authenticator=authenticator) as (_app, client):
        client_id = await _register_mcp_client(client)
        code_verifier, code_challenge = _pkce_pair()

        callback_response = await _drive_entra_login(
            client, mock_idp_client, client_id=client_id, code_challenge=code_challenge
        )
        assert callback_response.status_code == 302, callback_response.text
        code = _query(_location(callback_response))["code"][0]

        token_response = await client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _REDIRECT_URI,
                "client_id": client_id,
                "code_verifier": code_verifier,
            },
        )
        assert token_response.status_code == 200, token_response.text
        access_token = token_response.json()["access_token"]

    stored = await store.get_token(pool, access_token, "access")
    assert stored is not None
    assert stored.subject == "user-1"
    assert stored.user_oid == "user-1"
    assert stored.roles == ("Memory.User",)
    assert stored.namespaces == ("*",)

    user = await get_user(pool, "user-1")
    assert user is not None
    assert user.tid == _TID
    assert user.display_name == "Ada Lovelace"
    assert sorted(await group_ids(pool, "user-1")) == ["group-a", "group-b"]


async def test_entra_overage_fetches_groups_from_graph_exactly_once(
    entra_environ: dict[str, str], pool: asyncpg.Pool, mock_idp_client: httpx.AsyncClient
) -> None:
    config = _config()
    authenticator = _entra_authenticator(mock_idp_client)
    await _register_entra_client(
        mock_idp_client, graph_roles=["User.Read.All", "GroupMember.Read.All"]
    )
    await _create_entra_user(
        mock_idp_client, "user-2", roles=["Memory.User"], groups=["group-a"], overage=True
    )
    await _select_signin(mock_idp_client, "user-2")

    async with _running_app(entra_environ, config, authenticator=authenticator) as (_app, client):
        client_id = await _register_mcp_client(client)
        _verifier, code_challenge = _pkce_pair()

        callback_response = await _drive_entra_login(
            client, mock_idp_client, client_id=client_id, code_challenge=code_challenge
        )
        assert callback_response.status_code == 302, callback_response.text

    calls = await mock_idp_client.get("/_mock/calls")
    assert calls.json()["getMemberGroups"] == 1
    assert sorted(await group_ids(pool, "user-2")) == ["group-a"]


async def test_entra_denies_a_foreign_tenant(
    entra_environ: dict[str, str], mock_idp_client: httpx.AsyncClient
) -> None:
    config = _config()
    # Deliberately excludes the mock's own `_TID` - the id token's `tid` can then
    # never be in the allowlist, exercising that check through a real round trip.
    authenticator = _entra_authenticator(
        mock_idp_client, allowed_tenants=frozenset({"99999999-9999-9999-9999-999999999999"})
    )
    await _register_entra_client(mock_idp_client)
    await _create_entra_user(mock_idp_client, "user-3", roles=["Memory.User"])
    await _select_signin(mock_idp_client, "user-3")

    async with _running_app(entra_environ, config, authenticator=authenticator) as (_app, client):
        client_id = await _register_mcp_client(client)
        _verifier, code_challenge = _pkce_pair()

        callback_response = await _drive_entra_login(
            client, mock_idp_client, client_id=client_id, code_challenge=code_challenge
        )
        assert callback_response.status_code == 400, callback_response.text


async def test_entra_denies_a_replayed_or_mismatched_nonce(
    entra_environ: dict[str, str], mock_idp_client: httpx.AsyncClient
) -> None:
    config = _config()
    authenticator = _entra_authenticator(mock_idp_client)
    await _register_entra_client(mock_idp_client)
    await _create_entra_user(mock_idp_client, "user-4", roles=["Memory.User"])
    await _select_signin(mock_idp_client, "user-4")

    async with _running_app(entra_environ, config, authenticator=authenticator) as (_app, client):
        client_id = await _register_mcp_client(client)
        _verifier, code_challenge = _pkce_pair()

        callback_response = await _drive_entra_login(
            client,
            mock_idp_client,
            client_id=client_id,
            code_challenge=code_challenge,
            nonce_override="a-nonce-the-facade-never-generated",
        )
        assert callback_response.status_code == 400, callback_response.text


async def test_entra_denies_a_user_with_no_memory_role(
    entra_environ: dict[str, str], mock_idp_client: httpx.AsyncClient
) -> None:
    config = _config()
    authenticator = _entra_authenticator(mock_idp_client)
    await _register_entra_client(mock_idp_client)
    await _create_entra_user(mock_idp_client, "user-5", roles=[])
    await _select_signin(mock_idp_client, "user-5")

    async with _running_app(entra_environ, config, authenticator=authenticator) as (_app, client):
        client_id = await _register_mcp_client(client)
        _verifier, code_challenge = _pkce_pair()

        callback_response = await _drive_entra_login(
            client, mock_idp_client, client_id=client_id, code_challenge=code_challenge
        )
        assert callback_response.status_code == 403, callback_response.text


# === Readiness gate (ADR-0009 §5) ====================================================


async def test_readyz_is_503_until_entra_discovery_succeeds(
    entra_environ: dict[str, str],
) -> None:
    config = _config()
    unreachable = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(503))
    )
    authenticator = EntraAuthenticator(
        tenant_id=_TID,
        client_id=_ENTRA_CLIENT_ID,
        client_secret=_ENTRA_CLIENT_SECRET,
        redirect_uri=f"{_PUBLIC_URL}{CALLBACK_PATH}",
        authority=_AUTHORITY,
        graph_url=_GRAPH_URL,
        http_client=unreachable,
    )
    try:
        async with _running_app(entra_environ, config, authenticator=authenticator) as (
            _app,
            client,
        ):
            response = await client.get("/readyz")
            assert response.status_code == 503, response.text
            body = response.json()
            assert body["entra_discovery"] is False
            assert body["ready"] is False
    finally:
        await unreachable.aclose()


async def test_readyz_is_ready_once_entra_discovery_succeeds(
    entra_environ: dict[str, str], mock_idp_client: httpx.AsyncClient
) -> None:
    config = _config()
    authenticator = _entra_authenticator(mock_idp_client)
    await _register_entra_client(mock_idp_client)

    async with _running_app(entra_environ, config, authenticator=authenticator) as (_app, client):
        response = await client.get("/readyz")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["entra_discovery"] is True
        assert body["ready"] is True
