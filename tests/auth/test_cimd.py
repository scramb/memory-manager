# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for Client ID Metadata Document registration (CIMD, SEP-991, #38).

Two layers: `memory_manager.auth.cimd.ClientMetadataFetcher` on its own - the SSRF
guards, validation and caching, driven with an `httpx.MockTransport` and a fake
`Resolver` so no real network or DNS is ever touched - and
`memory_manager.auth.provider.MemoryManagerOAuthProvider.get_client`/the full HTTP
surface (`memory_manager.http.create_app`), the same `httpx.AsyncClient`-over-ASGI
seam `tests/auth/test_oauth_flow.py` already uses, with `create_app`'s `cimd_fetcher`
parameter standing in for the production `ClientMetadataFetcher()`.
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
import pytest
import pytest_asyncio
from mcp.shared.auth import InvalidRedirectUriError
from pydantic import AnyUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

from memory_manager.app import open_services
from memory_manager.auth.cimd import CimdError, ClientMetadataFetcher
from memory_manager.auth.login import BoundCompleter, PendingAuthorization
from memory_manager.auth.provider import MemoryManagerOAuthProvider
from memory_manager.config import ServerConfig
from memory_manager.db.migrate import migrate
from memory_manager.http import create_app

# --- Shared fixtures for a fake CIMD document server -------------------------------

_CLIENT_URL = "https://client.example.test/metadata.json"
_LOOPBACK_REDIRECT = "http://127.0.0.1/callback"
_PUBLIC_ADDRESS = "8.8.8.8"
_PRIVATE_ADDRESS = "10.1.2.3"


def _document_json(
    *,
    client_id: str = _CLIENT_URL,
    client_name: str | None = "Test CIMD Client",
    redirect_uris: tuple[str, ...] = (_LOOPBACK_REDIRECT,),
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "client_id": client_id,
        "client_name": client_name,
        "redirect_uris": list(redirect_uris),
    }
    if extra:
        body.update(extra)
    return body


def _ok_handler(**document_kwargs: Any) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json=_document_json(**document_kwargs),
        )

    return httpx.MockTransport(handler)


def _counting_handler() -> tuple[httpx.MockTransport, list[httpx.Request]]:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200, headers={"content-type": "application/json"}, json=_document_json()
        )

    return httpx.MockTransport(handler), calls


def _public_resolver(_host: str) -> list[str]:
    return [_PUBLIC_ADDRESS]


def _private_resolver(_host: str) -> list[str]:
    return [_PRIVATE_ADDRESS]


# --- Unit tests: ClientMetadataFetcher's SSRF guards and validation ----------------


async def test_fetch_happy_path_pins_to_the_resolved_address_and_sets_host_and_sni() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, headers={"content-type": "application/json"}, json=_document_json()
        )

    fetcher = ClientMetadataFetcher(
        transport=httpx.MockTransport(handler), resolver=_public_resolver
    )
    document = await fetcher.fetch_client_metadata(_CLIENT_URL)

    assert document.client_id == _CLIENT_URL
    assert document.client_name == "Test CIMD Client"
    assert document.redirect_uris == (_LOOPBACK_REDIRECT,)

    assert len(seen) == 1
    request = seen[0]
    assert request.url.host == _PUBLIC_ADDRESS
    assert request.headers["host"] == "client.example.test"
    assert request.extensions.get("sni_hostname") == "client.example.test"


async def test_fetch_rejects_a_client_id_mismatch() -> None:
    fetcher = ClientMetadataFetcher(
        transport=_ok_handler(client_id="https://other.example.test/metadata.json"),
        resolver=_public_resolver,
    )
    with pytest.raises(CimdError):
        await fetcher.fetch_client_metadata(_CLIENT_URL)


async def test_fetch_rejects_http_scheme() -> None:
    fetcher = ClientMetadataFetcher(transport=_ok_handler(), resolver=_public_resolver)
    with pytest.raises(CimdError, match="https"):
        await fetcher.fetch_client_metadata("http://client.example.test/metadata.json")


async def test_fetch_rejects_a_url_without_a_path() -> None:
    fetcher = ClientMetadataFetcher(transport=_ok_handler(), resolver=_public_resolver)
    with pytest.raises(CimdError, match="path"):
        await fetcher.fetch_client_metadata("https://client.example.test")


@pytest.mark.parametrize(
    "address",
    [
        "10.1.2.3",  # RFC 1918 private
        "127.0.0.1",  # loopback
        "169.254.169.254",  # link-local (cloud metadata)
        "224.0.0.1",  # multicast
        "::1",  # IPv6 loopback
        "fd00::1",  # IPv6 unique local
        "100.64.0.5",  # CGNAT (RFC 6598) - not `is_private`/`is_reserved`, only `is_global`
        "100.127.255.254",  # CGNAT, top of the /10
        "198.18.0.1",  # benchmarking (RFC 2544)
        "192.0.0.8",  # IETF protocol assignments (RFC 6890)
        "64:ff9b::7f00:1",  # NAT64 (RFC 6052) embedding 127.0.0.1
        "2001:db8::1",  # documentation (RFC 3849)
    ],
)
async def test_fetch_rejects_disallowed_resolved_addresses(address: str) -> None:
    fetcher = ClientMetadataFetcher(transport=_ok_handler(), resolver=lambda _host: [address])
    with pytest.raises(CimdError):
        await fetcher.fetch_client_metadata(_CLIENT_URL)


async def test_fetch_rejects_when_the_document_exceeds_the_size_cap() -> None:
    oversized_name = "x" * (70 * 1024)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json=_document_json(client_name=oversized_name),
        )

    fetcher = ClientMetadataFetcher(
        transport=httpx.MockTransport(handler), resolver=_public_resolver
    )
    with pytest.raises(CimdError, match="byte cap"):
        await fetcher.fetch_client_metadata(_CLIENT_URL)


async def test_fetch_rejects_on_timeout() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated slow server")

    fetcher = ClientMetadataFetcher(
        transport=httpx.MockTransport(handler), resolver=_public_resolver
    )
    with pytest.raises(CimdError, match="timed out"):
        await fetcher.fetch_client_metadata(_CLIENT_URL)


async def test_fetch_rejects_a_redirect_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302, headers={"location": "https://attacker.example.test/metadata.json"}
        )

    fetcher = ClientMetadataFetcher(
        transport=httpx.MockTransport(handler), resolver=_public_resolver
    )
    with pytest.raises(CimdError, match="HTTP 302"):
        await fetcher.fetch_client_metadata(_CLIENT_URL)


async def test_fetch_rejects_a_non_json_content_type() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<html></html>")

    fetcher = ClientMetadataFetcher(
        transport=httpx.MockTransport(handler), resolver=_public_resolver
    )
    with pytest.raises(CimdError, match="JSON"):
        await fetcher.fetch_client_metadata(_CLIENT_URL)


async def test_fetch_rejects_a_document_requesting_a_secret_based_auth_method() -> None:
    fetcher = ClientMetadataFetcher(
        transport=_ok_handler(extra={"token_endpoint_auth_method": "client_secret_post"}),
        resolver=_public_resolver,
    )
    with pytest.raises(CimdError, match="public"):
        await fetcher.fetch_client_metadata(_CLIENT_URL)


async def test_fetch_caches_a_successful_fetch_by_url() -> None:
    transport, calls = _counting_handler()
    fetcher = ClientMetadataFetcher(transport=transport, resolver=_public_resolver)

    first = await fetcher.fetch_client_metadata(_CLIENT_URL)
    second = await fetcher.fetch_client_metadata(_CLIENT_URL)

    assert first == second
    assert len(calls) == 1


async def test_fetch_caches_a_failure_negatively() -> None:
    transport, calls = _counting_handler()
    # The resolver is wrong for this document's host the first and second time alike -
    # the second call must still not touch `transport` at all (served from the negative
    # cache), confirming the failure itself was cached, not merely not retried by luck.
    fetcher = ClientMetadataFetcher(transport=transport, resolver=_private_resolver)

    with pytest.raises(CimdError):
        await fetcher.fetch_client_metadata(_CLIENT_URL)
    with pytest.raises(CimdError):
        await fetcher.fetch_client_metadata(_CLIENT_URL)

    assert len(calls) == 0


# --- MemoryManagerOAuthProvider.get_client: CIMD vs. DCR routing, loopback matching -


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


#: A Fernet key generated once for this test module only - not a credential protecting
#: anything real, just what `MemoryManagerOAuthProvider` requires to construct.
_CLIENT_SECRET_KEY = "r8rGp30uQA9cx9egMfZk4ez3xkfaFzL0tCst-kNzrcI="  # noqa: S105
_RESOURCE = "https://mm.example.test/mcp"
_ISSUER = "https://mm.example.test"


def _provider(
    pool: asyncpg.Pool, *, cimd_fetcher: ClientMetadataFetcher | None
) -> MemoryManagerOAuthProvider:
    return MemoryManagerOAuthProvider(
        pool,
        resource=_RESOURCE,
        issuer=_ISSUER,
        client_secret_key=_CLIENT_SECRET_KEY,
        cimd_fetcher=cimd_fetcher,
    )


async def test_get_client_fetches_and_upserts_a_cimd_client(pool: asyncpg.Pool) -> None:
    fetcher = ClientMetadataFetcher(transport=_ok_handler(), resolver=_public_resolver)
    provider = _provider(pool, cimd_fetcher=fetcher)

    client = await provider.get_client(_CLIENT_URL)

    assert client is not None
    assert client.client_id == _CLIENT_URL
    assert client.token_endpoint_auth_method == "none"  # noqa: S105 - an auth method, not a credential
    assert client.redirect_uris is not None
    assert str(client.redirect_uris[0]) == _LOOPBACK_REDIRECT

    row = await pool.fetchrow(
        "select client_info from oauth_clients where client_id = $1", _CLIENT_URL
    )
    assert row is not None
    assert '"cimd": true' in row["client_info"]


async def test_get_client_returns_none_for_an_unfetchable_cimd_url(pool: asyncpg.Pool) -> None:
    fetcher = ClientMetadataFetcher(transport=_ok_handler(), resolver=_private_resolver)
    provider = _provider(pool, cimd_fetcher=fetcher)

    assert await provider.get_client(_CLIENT_URL) is None


async def test_get_client_with_cimd_disabled_does_not_resolve_url_client_ids(
    pool: asyncpg.Pool,
) -> None:
    provider = _provider(pool, cimd_fetcher=None)

    assert await provider.get_client(_CLIENT_URL) is None


async def test_cimd_client_loopback_redirect_matches_any_port(pool: asyncpg.Pool) -> None:
    fetcher = ClientMetadataFetcher(transport=_ok_handler(), resolver=_public_resolver)
    provider = _provider(pool, cimd_fetcher=fetcher)
    client = await provider.get_client(_CLIENT_URL)
    assert client is not None

    matched = client.validate_redirect_uri(AnyUrl("http://127.0.0.1:54321/callback"))
    assert str(matched) == "http://127.0.0.1:54321/callback"


async def test_cimd_client_rejects_a_non_loopback_redirect_not_listed(pool: asyncpg.Pool) -> None:
    fetcher = ClientMetadataFetcher(transport=_ok_handler(), resolver=_public_resolver)
    provider = _provider(pool, cimd_fetcher=fetcher)
    client = await provider.get_client(_CLIENT_URL)
    assert client is not None

    with pytest.raises(InvalidRedirectUriError):
        client.validate_redirect_uri(AnyUrl("https://attacker.example.test/callback"))


async def test_cimd_client_exact_match_redirect_still_works(pool: asyncpg.Pool) -> None:
    fetcher = ClientMetadataFetcher(
        transport=_ok_handler(redirect_uris=("https://app.example.test/callback",)),
        resolver=_public_resolver,
    )
    provider = _provider(pool, cimd_fetcher=fetcher)
    client = await provider.get_client(_CLIENT_URL)
    assert client is not None

    matched = client.validate_redirect_uri(AnyUrl("https://app.example.test/callback"))
    assert str(matched) == "https://app.example.test/callback"


# --- Full HTTP surface: authorize -> login -> token, and AS metadata ---------------


class _FakeAuthenticator:
    """Completes every pending authorization immediately - the same test-only stand-in
    `tests/auth/test_oauth_flow.py`'s own `FakeAuthenticator` is, kept local here so this
    file does not depend on another test module's internals."""

    def __init__(self, *, subject: str = "alice", namespaces: list[str] | None = None) -> None:
        self.subject = subject
        self.namespaces = namespaces if namespaces is not None else ["personal"]

    async def handle(
        self, request: Request, pending: PendingAuthorization, complete: BoundCompleter
    ) -> Response:
        redirect_url = await complete(self.subject, self.namespaces)
        if redirect_url is None:
            return PlainTextResponse("expired", status_code=400)
        return RedirectResponse(redirect_url, status_code=302)


_PUBLIC_URL = "https://mm.example.test"
_MCP_PATH = "/mcp"


def _config(*, cimd_enabled: bool = True) -> ServerConfig:
    return ServerConfig(
        public_url=_PUBLIC_URL,
        mcp_path=_MCP_PATH,
        oauth_client_secret_key=_CLIENT_SECRET_KEY,
        cimd_enabled=cimd_enabled,
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
    cimd_fetcher: ClientMetadataFetcher | None,
) -> AsyncIterator[tuple[Starlette, httpx.AsyncClient]]:
    app = create_app(
        lambda: open_services(environ),
        config,
        authenticator=_FakeAuthenticator(),
        cimd_fetcher=cimd_fetcher,
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver", follow_redirects=False
        ) as client:
            yield app, client


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def _location(response: httpx.Response) -> str:
    location = response.headers.get("location")
    assert location is not None, f"expected a redirect, got {response.status_code} {response.text}"
    return str(location)


async def test_cimd_client_authorize_login_token_with_loopback_any_port(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    config = _config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    fetcher = ClientMetadataFetcher(transport=_ok_handler(), resolver=_public_resolver)
    redirect_uri = "http://127.0.0.1:54321/callback"
    code_verifier, code_challenge = _pkce_pair()

    async with _running_app(environ, config, cimd_fetcher=fetcher) as (_app, client):
        authorize_response = await client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": _CLIENT_URL,
                "redirect_uri": redirect_uri,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "state": "xyz",
                "resource": f"{_PUBLIC_URL}{_MCP_PATH}",
            },
        )
        assert authorize_response.status_code == 302, authorize_response.text
        login_response = await client.get(_location(authorize_response))
        assert login_response.status_code == 302, login_response.text

        callback_url = _location(login_response)
        parsed = urlsplit(callback_url)
        assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == redirect_uri
        code = parse_qs(parsed.query)["code"][0]

        token_response = await client.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": _CLIENT_URL,
                "code_verifier": code_verifier,
            },
        )
        assert token_response.status_code == 200, token_response.text
        body = token_response.json()
        assert body["access_token"]


async def test_cimd_disabled_rejects_a_url_client_id_as_unknown(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    config = _config(cimd_enabled=False)
    environ = _environ(bare_remote, tmp_path, test_database_url)
    fetcher = ClientMetadataFetcher(transport=_ok_handler(), resolver=_public_resolver)
    _verifier, challenge = _pkce_pair()

    async with _running_app(environ, config, cimd_fetcher=fetcher) as (_app, client):
        response = await client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": _CLIENT_URL,
                "redirect_uri": "http://127.0.0.1:1/callback",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "xyz",
                "resource": f"{_PUBLIC_URL}{_MCP_PATH}",
            },
        )
        assert response.status_code == 400
        assert "not found" in response.text.lower()


async def test_authorization_server_metadata_advertises_cimd_when_enabled(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    config = _config(cimd_enabled=True)
    environ = _environ(bare_remote, tmp_path, test_database_url)
    async with _running_app(environ, config, cimd_fetcher=None) as (_app, client):
        response = await client.get("/.well-known/oauth-authorization-server")
    assert response.status_code == 200
    metadata = response.json()
    assert metadata["client_id_metadata_document_supported"] is True
    assert "none" in metadata["token_endpoint_auth_methods_supported"]
    assert "client_secret_post" in metadata["token_endpoint_auth_methods_supported"]


async def test_authorization_server_metadata_omits_cimd_when_disabled(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    config = _config(cimd_enabled=False)
    environ = _environ(bare_remote, tmp_path, test_database_url)
    async with _running_app(environ, config, cimd_fetcher=None) as (_app, client):
        response = await client.get("/.well-known/oauth-authorization-server")
    assert response.status_code == 200
    metadata = response.json()
    assert metadata.get("client_id_metadata_document_supported") is None
    assert "none" not in (metadata.get("token_endpoint_auth_methods_supported") or [])
