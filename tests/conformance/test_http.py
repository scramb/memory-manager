# SPDX-License-Identifier: AGPL-3.0-only
"""Streamable HTTP conformance: a real `memory-manager serve --http` subprocess,
exercised against both protocol revisions the server mounts on one endpoint (#33) -
the HTTP counterpart of `tests/conformance/test_stdio.py` (read there first for the
two-revision background: `mcp_types.version`'s `HANDSHAKE_PROTOCOL_VERSIONS` vs.
`MODERN_PROTOCOL_VERSIONS`, and why 2025-11-25/2026-07-28 are the two targets here).

Over HTTP, which era a request lands in is decided by one header
(`mcp.server.streamable_http_manager.StreamableHTTPSessionManager._handle_request`,
confirmed by reading it rather than assumed): no `MCP-Protocol-Version` header, or one
of `HANDSHAKE_PROTOCOL_VERSIONS`, goes through the legacy (session/`initialize`) path;
any other value - including an unsupported one - goes to `handle_modern_request`.
`mcp.Client` drives both eras correctly on its own (`mode="legacy"`/`"auto"`); the raw
`httpx` checks below exist for what `Client` has no seam for: an unregistered method and
an unsupported `MCP-Protocol-Version` header, both only reachable by sending the header
by hand.

The subprocess talks to a vault cloned from a throwaway local bare remote
(`tests/git_fixtures.bare_remote`, no network), seeded with one note, and no
`DATABASE_URL` - same as `test_stdio.py`. `MM_ALLOW_UNAUTHENTICATED` is not set: the
subprocess binds to `127.0.0.1`, which `cli._is_loopback` always allows.

`authenticated_http_server` (bottom of the module, #34) is the one exception: it sets
`DATABASE_URL` to a fresh `test_database_url`, so the subprocess's own startup turns
bearer-token auth on for `/mcp` (`http.py`) - a static token is then created directly
against that same database once the subprocess is ready, and a real `mcp.Client`
carries it as `Authorization: Bearer ...` the same way Claude Code or CI would, via
`httpx2.AsyncClient(headers=...)` passed to `streamable_http_client` (the SDK's own
seam for a client-supplied `httpx2.AsyncClient`, since `mcp.Client("http://...")`
itself has no `headers=` parameter).
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import httpx2
import pytest
import pytest_asyncio
from git_fixtures import seed_notes
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp_types import METHOD_NOT_FOUND, UNSUPPORTED_PROTOCOL_VERSION
from mcp_types.version import LATEST_HANDSHAKE_VERSION, LATEST_MODERN_VERSION

from memory_manager.auth.tokens import ALL_NAMESPACES, create_token
from memory_manager.mcp.authz import READ_SCOPE, WRITE_SCOPE
from memory_manager.vault.note import Note, serialize
from memory_manager.vault.ulid import new_ulid

__all__: list[str] = []

_CLI_ARGS = ("-m", "memory_manager.cli", "serve", "--http")

# Spelled out as literals, checked against the installed SDK's own registry
# below - see `tests/conformance/test_stdio.py`'s identical comment.
_HANDSHAKE_VERSION = "2025-11-25"
_MODERN_VERSION = "2026-07-28"

_EXPECTED_TOOL_NAMES = frozenset(
    {
        "memory_index",
        "memory_read",
        "memory_search",
        "memory_write",
        "memory_edit",
        "memory_supersede",
        "memory_archive",
    }
)

_SEEDED_PATH = "personal/fact/favorite-color.md"
_SEEDED_BODY = "Blue.\n"

_STARTUP_TIMEOUT = 10.0
_SHUTDOWN_TIMEOUT = 5.0
_POLL_INTERVAL = 0.1

_PARAMETRIZE_VERSIONS = pytest.mark.parametrize(
    "version", [_HANDSHAKE_VERSION, _MODERN_VERSION], ids=["2025-11-25", "2026-07-28"]
)


def test_negotiated_versions_match_the_sdks_registry() -> None:
    """The literals this module tests against are exactly the installed SDK's.

    Same guard as `test_stdio.py`'s - kept here too since this module pins
    the same two literals independently (a header value, not a handshake
    parameter).
    """
    assert _HANDSHAKE_VERSION == LATEST_HANDSHAKE_VERSION
    assert _MODERN_VERSION == LATEST_MODERN_VERSION


@dataclass
class _Server:
    process: asyncio.subprocess.Process
    base_url: str
    mcp_url: str


def _free_port() -> int:
    """An ephemeral TCP port, free at the instant of the call.

    Closed again immediately: `serve --http` binds it itself a moment
    later. Vulnerable in theory to another process grabbing the same port
    first - the same race every "find a free port for a test server"
    helper accepts.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@pytest.fixture
def http_env(tmp_path: Path, bare_remote: Path) -> dict[str, str]:
    """`VAULT_REMOTE`/`VAULT_DIR` for an HTTP subprocess, vault seeded with one note.

    No `DATABASE_URL`, same reasoning as `test_stdio.py`'s `stdio_env`.
    """
    now = datetime(2025, 6, 1, tzinfo=UTC)
    note = Note(
        id=new_ulid(now),
        title="Favorite color",
        description="The user's favorite color, seeded for HTTP conformance checks.",
        type="fact",
        created=now,
        updated=now,
        body=_SEEDED_BODY,
        tags=("color",),
    )
    seed_notes(bare_remote, {_SEEDED_PATH: serialize(note)})
    return {"VAULT_REMOTE": str(bare_remote), "VAULT_DIR": str(tmp_path / "vault")}


@pytest_asyncio.fixture
async def http_server(http_env: Mapping[str, str]) -> AsyncIterator[_Server]:
    """A real `memory-manager serve --http` subprocess, up and answering `/healthz`.

    `MM_ALLOW_UNAUTHENTICATED` is deliberately left unset: `HOST` defaults to
    `127.0.0.1`, which `cli._is_loopback` always allows, so the no-auth
    refusal never fires here. No `DATABASE_URL` either, so `/mcp` itself has
    no bearer-token auth turned on (#34) - see `authenticated_http_server`
    below for that case.
    """
    port = _free_port()
    full_env = {**os.environ, **http_env, "HOST": "127.0.0.1", "PORT": str(port)}
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        *_CLI_ARGS,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=full_env,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        await _wait_until_ready(process, base_url)
        yield _Server(process=process, base_url=base_url, mcp_url=f"{base_url}/mcp")
    finally:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=_SHUTDOWN_TIMEOUT)
        except TimeoutError:
            process.kill()
            await process.wait()


async def _wait_until_ready(process: asyncio.subprocess.Process, base_url: str) -> None:
    deadline = asyncio.get_running_loop().time() + _STARTUP_TIMEOUT
    async with httpx.AsyncClient() as client:
        while True:
            if process.returncode is not None:
                stderr = await process.stderr.read() if process.stderr else b""
                raise AssertionError(
                    f"memory-manager serve --http exited early (code {process.returncode}): "
                    f"{stderr.decode('utf-8', errors='replace')}"
                )
            try:
                response = await client.get(f"{base_url}/healthz", timeout=1.0)
                if response.status_code == 200:
                    return
            except httpx.TransportError:
                pass
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError("memory-manager serve --http did not become ready in time")
            await asyncio.sleep(_POLL_INTERVAL)


# --- Bearer-token auth (#34): a real subprocess with `DATABASE_URL` set -----


@dataclass
class _AuthenticatedServer:
    server: _Server
    token: str


@pytest_asyncio.fixture
async def authenticated_http_server(
    http_env: Mapping[str, str], test_database_url: str
) -> AsyncIterator[_AuthenticatedServer]:
    """Same subprocess as `http_server`, with `DATABASE_URL` set and one token created.

    The subprocess's own startup (`open_services`) migrates `test_database_url`
    before this fixture ever touches it, so by the time `_wait_until_ready`
    returns, `static_tokens` already exists - the token below is created
    against that same database, not a separate one.
    """
    port = _free_port()
    full_env = {
        **os.environ,
        **http_env,
        "HOST": "127.0.0.1",
        "PORT": str(port),
        "DATABASE_URL": test_database_url,
        # DATABASE_URL set above turns bearer-token auth on (#34), which
        # requires PUBLIC_URL (#35, ADR-0004) - the subprocess refuses to
        # start otherwise. Its value is unrelated to `base_url` below: the
        # bearer tokens this fixture creates carry no RFC 8707 resource of
        # their own (`validate_token_resource=False`, `http.py`), so this
        # never has to match the loopback address the test client actually
        # talks to.
        "PUBLIC_URL": "https://mm.example.test",
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        *_CLI_ARGS,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=full_env,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        await _wait_until_ready(process, base_url)

        pool = await asyncpg.create_pool(test_database_url)
        try:
            plaintext, _info = await create_token(
                pool,
                "conformance",
                scopes=[READ_SCOPE, WRITE_SCOPE],
                namespaces=[ALL_NAMESPACES],
            )
        finally:
            await pool.close()

        server = _Server(process=process, base_url=base_url, mcp_url=f"{base_url}/mcp")
        yield _AuthenticatedServer(server=server, token=plaintext)
    finally:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=_SHUTDOWN_TIMEOUT)
        except TimeoutError:
            process.kill()
            await process.wait()


async def test_mcp_without_a_token_is_401_when_database_url_is_set(
    authenticated_http_server: _AuthenticatedServer,
) -> None:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            authenticated_http_server.server.mcp_url,
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Accept": "application/json, text/event-stream"},
        )

    assert response.status_code == 401


async def test_a_bearer_token_authenticates_a_real_client_over_the_wire(
    authenticated_http_server: _AuthenticatedServer,
) -> None:
    http_client = httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {authenticated_http_server.token}"}
    )
    transport = streamable_http_client(
        authenticated_http_server.server.mcp_url, http_client=http_client
    )

    async with Client(transport, mode="legacy") as client:
        listing = await client.list_tools()
        assert {tool.name for tool in listing.tools} == _EXPECTED_TOOL_NAMES

        index_result = await client.call_tool("memory_index", {})
        assert index_result.is_error is False
        assert index_result.structured_content is not None
        paths = {entry["path"] for entry in index_result.structured_content["result"]}
        assert _SEEDED_PATH in paths


# --- mcp.Client-driven checks: handshake, tool/prompt surface, call_tool shapes -----


@pytest.mark.parametrize(
    ("mode", "expected_version"),
    [("legacy", _HANDSHAKE_VERSION), ("auto", _MODERN_VERSION)],
    ids=["2025-11-25", "2026-07-28"],
)
async def test_handshake_and_tool_surface(
    http_server: _Server, mode: str, expected_version: str
) -> None:
    # Same reasoning as `test_stdio.py`'s identically named test for `mode="auto"`
    # landing on the modern version: this server has no handshake-era fallback
    # reason, so a real `server/discover` probe lands on 2026-07-28.
    async with Client(http_server.mcp_url, mode=mode) as client:
        assert client.protocol_version == expected_version
        assert client.instructions
        assert client.server_info is not None
        assert client.server_info.name == "memory-manager"
        capabilities = client.server_capabilities
        assert capabilities.tools is not None
        assert capabilities.prompts is not None

        listing = await client.list_tools()
        names = {tool.name for tool in listing.tools}
        assert names == _EXPECTED_TOOL_NAMES
        for tool in listing.tools:
            assert tool.description
            assert tool.input_schema["type"] == "object"

        prompts = await client.list_prompts()
        assert any(prompt.name == "memory_guide" for prompt in prompts.prompts)
        guide = await client.get_prompt("memory_guide")
        assert guide.messages

        index_result = await client.call_tool("memory_index", {})
        assert index_result.is_error is False
        assert index_result.structured_content is not None
        indexed_paths = {entry["path"] for entry in index_result.structured_content["result"]}
        assert _SEEDED_PATH in indexed_paths

        read_result = await client.call_tool("memory_read", {"items": [_SEEDED_PATH]})
        assert read_result.is_error is False
        assert read_result.structured_content is not None
        read_items = read_result.structured_content["result"]
        assert read_items[0]["path"] == _SEEDED_PATH
        assert _SEEDED_BODY in read_items[0]["content"]

        invalid_args_result = await client.call_tool("memory_write", {})
        assert invalid_args_result.is_error is True

        unknown_tool_result = await client.call_tool("no_such_tool", {})
        assert unknown_tool_result.is_error is True


# --- Raw HTTP checks: protocol-layer errors reachable only by hand-set headers -----


def _modern_meta() -> dict[str, Any]:
    """The `_meta` envelope every 2026-07-28+ request carries - see `test_stdio.py`'s twin."""
    return {
        "io.modelcontextprotocol/protocolVersion": _MODERN_VERSION,
        "io.modelcontextprotocol/clientInfo": {"name": "conformance-test", "version": "0"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }


async def test_unknown_method_is_a_json_rpc_method_not_found_error(http_server: _Server) -> None:
    method = "not/a/real/method"
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": {"_meta": _modern_meta()}}
    async with httpx.AsyncClient() as client:
        response = await client.post(
            http_server.mcp_url,
            json=body,
            headers={
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": _MODERN_VERSION,
                # The envelope ladder's rung 2 (`inbound.classify_inbound_request`):
                # these two routing headers must mirror the body before the ladder
                # even gets to "is this version supported" - method existence
                # itself is checked later, by kernel dispatch.
                "MCP-Method": method,
            },
        )

    assert response.status_code == 404
    payload = response.json()
    assert payload["error"]["code"] == METHOD_NOT_FOUND


async def test_unsupported_protocol_version_header_is_rejected(http_server: _Server) -> None:
    bogus_version = "1999-01-01"
    method = "server/discover"
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": bogus_version,
                "io.modelcontextprotocol/clientInfo": {"name": "conformance-test", "version": "0"},
                "io.modelcontextprotocol/clientCapabilities": {},
            }
        },
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(
            http_server.mcp_url,
            json=body,
            headers={
                "Accept": "application/json, text/event-stream",
                # Not in `HANDSHAKE_PROTOCOL_VERSIONS`, so the session manager
                # routes this to the modern handler, which then rejects the
                # envelope's own unsupported version - the header is what
                # decides the *era*, the body is what gets validated.
                "MCP-Protocol-Version": bogus_version,
                "MCP-Method": method,
            },
        )

    assert response.status_code == 400
    payload = response.json()
    assert payload["error"]["code"] == UNSUPPORTED_PROTOCOL_VERSION
