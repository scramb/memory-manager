# SPDX-License-Identifier: AGPL-3.0-only
"""Protected Resource Metadata (RFC 9728) and the discoverable 401 for `/mcp` (#35).

Three layers, bottom to top:

- `memory_manager.config.canonical_resource_url` - pure string normalization,
  tested as a table, no app involved.
- `memory_manager.auth.prm.build_protected_resource_metadata` /
  `serve_protected_resource_metadata` against a real `create_app`/`open_services`
  pair (`_running_app`, the same pattern `tests/auth/test_static_tokens.py` and
  `tests/test_http_app.py` use), with `DATABASE_URL` set - the condition
  `http.py` turns bearer-token auth on for (#34).
- The 401 challenge on `/mcp` itself: the `WWW-Authenticate` header's
  `resource_metadata` and `scope` parameters, checked both for a missing and an
  invalid token.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response

from memory_manager.app import open_services
from memory_manager.auth.login import Authenticator, BoundCompleter, PendingAuthorization
from memory_manager.auth.prm import (
    RESOURCE_NAME,
    SCOPE_CHALLENGE,
    WELL_KNOWN_ROOT_PATH,
    path_suffixed_well_known_path,
)
from memory_manager.config import ServerConfig, ServerConfigError, canonical_resource_url
from memory_manager.http import create_app
from memory_manager.mcp.authz import READ_SCOPE, WRITE_SCOPE

_PUBLIC_URL = "https://mm.example.test"
_MCP_PATH = "/mcp"
_EXPECTED_RESOURCE = f"{_PUBLIC_URL}{_MCP_PATH}"
_EXPECTED_PATH_SUFFIXED_URL = path_suffixed_well_known_path(_MCP_PATH)

# --- canonical_resource_url --------------------------------------------------


@pytest.mark.parametrize(
    ("public_url", "path", "expected"),
    [
        # Uppercase scheme/host are lowercased.
        ("HTTPS://MM.EXAMPLE.TEST", "/mcp", "https://mm.example.test/mcp"),
        # The scheme's default port is dropped.
        ("https://mm.example.test:443", "/mcp", "https://mm.example.test/mcp"),
        ("http://mm.example.test:80", "/mcp", "http://mm.example.test/mcp"),
        # Any other port is kept.
        ("https://mm.example.test:8443", "/mcp", "https://mm.example.test:8443/mcp"),
        # A trailing slash on `public_url` does not leak into the result.
        ("https://mm.example.test/", "/mcp", "https://mm.example.test/mcp"),
        # `path` is normalized regardless of how it is spelled.
        ("https://mm.example.test", "mcp", "https://mm.example.test/mcp"),
        ("https://mm.example.test", "/mcp/", "https://mm.example.test/mcp"),
        ("https://mm.example.test", "//mcp//", "https://mm.example.test/mcp"),
        # An empty path is the bare origin - no trailing slash either.
        ("https://mm.example.test", "", "https://mm.example.test"),
        # Any path already on `public_url` itself is dropped: `path` is the
        # single source of truth for what comes after the origin (ADR-0004).
        ("https://mm.example.test/ignored", "/mcp", "https://mm.example.test/mcp"),
    ],
)
def test_canonical_resource_url_normalizes(public_url: str, path: str, expected: str) -> None:
    assert canonical_resource_url(public_url, path) == expected


# --- PRM served at both well-known URLs -------------------------------------


def _environ(bare_remote: Path, tmp_path: Path, database_url: str | None = None) -> dict[str, str]:
    environ = {"VAULT_REMOTE": str(bare_remote), "VAULT_DIR": str(tmp_path / "vault")}
    if database_url is not None:
        environ["DATABASE_URL"] = database_url
    return environ


class _UnusedAuthenticator:
    """An `Authenticator` that only exists to turn the OAuth authorization server on
    (`create_app`'s `authenticator` parameter) - none of this module's tests drive an
    actual `/login` round trip, so `handle` being called at all would be a test bug."""

    async def handle(
        self, request: Request, pending: PendingAuthorization, complete: BoundCompleter
    ) -> Response:
        raise AssertionError("not expected to be called by these PRM-focused tests")


@asynccontextmanager
async def _running_app(
    environ: dict[str, str],
    config: ServerConfig,
    *,
    authenticator: Authenticator | None = None,
) -> AsyncIterator[tuple[Starlette, httpx.AsyncClient]]:
    app = create_app(lambda: open_services(environ), config, authenticator=authenticator)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield app, client


#: A Fernet key (`Fernet.generate_key()`), required by `ServerConfig.oauth_client_secret_key`
#: whenever a test here enables the OAuth authorization server (`_UnusedAuthenticator`) -
#: harmless to set for the AS-disabled tests too, since it is then simply never read.
_CLIENT_SECRET_KEY = "r8rGp30uQA9cx9egMfZk4ez3xkfaFzL0tCst-kNzrcI="  # noqa: S105 - a Fernet test key, not a credential protecting anything real


def _authenticated_config() -> ServerConfig:
    return ServerConfig(
        public_url=_PUBLIC_URL, mcp_path=_MCP_PATH, oauth_client_secret_key=_CLIENT_SECRET_KEY
    )


async def test_prm_startup_fails_without_public_url_when_database_url_is_set(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    """ADR-0004/#35: the canonical URL must come from `PUBLIC_URL`, never a guess."""
    config = ServerConfig(mcp_path=_MCP_PATH)
    assert config.public_url is None

    with pytest.raises(ServerConfigError, match="PUBLIC_URL"):
        async with _running_app(_environ(bare_remote, tmp_path, test_database_url), config):
            pass


@pytest.mark.parametrize("well_known_path", [WELL_KNOWN_ROOT_PATH, _EXPECTED_PATH_SUFFIXED_URL])
async def test_prm_is_identical_at_both_well_known_urls(
    bare_remote: Path, tmp_path: Path, test_database_url: str, well_known_path: str
) -> None:
    config = _authenticated_config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    async with _running_app(environ, config, authenticator=_UnusedAuthenticator()) as (
        _app,
        client,
    ):
        response = await client.get(well_known_path)

    assert response.status_code == 200
    body = response.json()
    assert body["resource"] == _EXPECTED_RESOURCE
    assert body["authorization_servers"] == [_PUBLIC_URL]
    assert body["scopes_supported"] == [READ_SCOPE, WRITE_SCOPE]
    assert body["bearer_methods_supported"] == ["header"]
    assert body["resource_name"] == RESOURCE_NAME
    assert response.headers["access-control-allow-origin"] == "*"


async def test_prm_both_urls_serve_byte_identical_json(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    config = _authenticated_config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    async with _running_app(environ, config, authenticator=_UnusedAuthenticator()) as (
        _app,
        client,
    ):
        root_response = await client.get(WELL_KNOWN_ROOT_PATH)
        suffixed_response = await client.get(_EXPECTED_PATH_SUFFIXED_URL)

    assert root_response.content == suffixed_response.content


async def test_prm_requires_no_token(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    config = _authenticated_config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    async with _running_app(environ, config, authenticator=_UnusedAuthenticator()) as (
        _app,
        client,
    ):
        response = await client.get(WELL_KNOWN_ROOT_PATH)

    assert response.status_code == 200


async def test_prm_is_not_found_when_auth_is_disabled(bare_remote: Path, tmp_path: Path) -> None:
    """No `DATABASE_URL` means no `static_tokens` table to ever verify a token
    against (#34) - advertising how to get one would be misleading."""
    config = _authenticated_config()
    async with _running_app(_environ(bare_remote, tmp_path), config) as (_app, client):
        root_response = await client.get(WELL_KNOWN_ROOT_PATH)
        suffixed_response = await client.get(_EXPECTED_PATH_SUFFIXED_URL)

    assert root_response.status_code == 404
    assert suffixed_response.status_code == 404


async def test_prm_is_not_found_when_the_oauth_authorization_server_is_disabled(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    """#36: a database (so static tokens work) but no `authenticator` configured means
    no OAuth authorization server either - `authorization_servers` is RFC 9728's one
    *required* field, and this server has no honest value to put there without a
    running AS behind it (no `/authorize`/`/token` at that issuer), so the whole
    document is omitted rather than advertising one that would 404 if ever fetched."""
    config = _authenticated_config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    async with _running_app(environ, config) as (_app, client):
        root_response = await client.get(WELL_KNOWN_ROOT_PATH)
        suffixed_response = await client.get(_EXPECTED_PATH_SUFFIXED_URL)

    assert root_response.status_code == 404
    assert suffixed_response.status_code == 404


# --- The 401 challenge on /mcp -----------------------------------------------


async def test_mcp_without_a_token_challenges_with_resource_metadata_and_scope(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    config = _authenticated_config()
    environ = _environ(bare_remote, tmp_path, test_database_url)
    async with _running_app(environ, config, authenticator=_UnusedAuthenticator()) as (
        _app,
        client,
    ):
        response = await client.post(
            config.mcp_path,
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Accept": "application/json, text/event-stream"},
        )
        assert response.status_code == 401

        challenge = response.headers["www-authenticate"]
        resource_metadata_url = _extract_challenge_param(challenge, "resource_metadata")
        scope = _extract_challenge_param(challenge, "scope")
        assert scope == SCOPE_CHALLENGE

        parsed = urlsplit(resource_metadata_url)
        assert f"{parsed.scheme}://{parsed.netloc}" == _PUBLIC_URL
        assert parsed.path == _EXPECTED_PATH_SUFFIXED_URL

        # The advertised URL actually resolves to the PRM document.
        metadata_response = await client.get(parsed.path)
        assert metadata_response.status_code == 200
        assert metadata_response.json()["resource"] == _EXPECTED_RESOURCE


async def test_mcp_with_an_invalid_token_also_challenges_with_scope(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    config = _authenticated_config()
    async with _running_app(_environ(bare_remote, tmp_path, test_database_url), config) as (
        _app,
        client,
    ):
        response = await client.post(
            config.mcp_path,
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={
                "Accept": "application/json, text/event-stream",
                "Authorization": "Bearer mm_not-a-real-token",
            },
        )

    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert _extract_challenge_param(challenge, "scope") == SCOPE_CHALLENGE


def _extract_challenge_param(challenge: str, param: str) -> str:
    """Pull `param="value"` out of a `WWW-Authenticate: Bearer ...` header value."""
    marker = f'{param}="'
    start = challenge.index(marker) + len(marker)
    end = challenge.index('"', start)
    return challenge[start:end]
