# SPDX-License-Identifier: AGPL-3.0-only
"""Stdio transport conformance: a real `memory-manager serve --stdio` subprocess,
exercised against both protocol revisions the SDK can actually reach over stdio (#21).

Which revisions are in scope, and why: `mcp_types.version` (installed SDK, `mcp` 2.3.0)
splits `KNOWN_PROTOCOL_VERSIONS` into `HANDSHAKE_PROTOCOL_VERSIONS` (2024-11-05 through
2025-11-25, reachable via the `initialize` handshake) and `MODERN_PROTOCOL_VERSIONS`
(2026-07-28 only, the stateless per-request envelope, reachable via `server/discover`).
`docs/PLAN.md` "Protocol targets" names 2025-11-25 (what Claude speaks today) and
2026-07-28 (the newest spec) as the two targets; those are exactly
`LATEST_HANDSHAKE_VERSION` and `LATEST_MODERN_VERSION`. `mcp.server.lowlevel.server`'s
`Server.run` drives `serve_dual_era_loop` regardless of transport - confirmed here (not
assumed) by actually sending a handshake-era `initialize` and a modern-era
`server/discover` down the same stdio subprocess and observing both answered correctly.

Two complementary harnesses are used:

- `mcp.Client` (`mcp.client.client`) with `mode="legacy"` (forces the 2025-11-25
  handshake) or `mode="auto"` (probes `server/discover` for real and adopts
  whatever comes back - this server only advertises 2026-07-28, so `auto` lands
  there) against a `StdioServerParameters` subprocess - for the rich, typed checks
  (tool/prompt listings, `call_tool`, `get_prompt`).
- `_RawServer` below, a thin hand-rolled JSON-RPC-over-stdio client - for checks the
  typed `Client` does not expose a seam for: an unregistered top-level method, a
  malformed line on the wire, and the raw `server/discover` probe. Every line it reads
  is parsed as JSON before anything else happens, which is this module's running check
  that the server's stdout carries nothing but JSON-RPC messages.

`stdio_env` seeds a `"git"`-backend vault cloned from a throwaway local bare
remote (`tests/git_fixtures.bare_remote`, no network), with no `DATABASE_URL` -
`memory_index`/`memory_read` do not need Postgres (`app.open_services`).
`STORAGE_BACKEND=postgres` has no stdio variant at all (ADR-0008 addendum
"identity sources and curate", #115/#116): `cli.py`'s `serve --stdio` refuses
it outright, so there is nothing left for this module to exercise over stdio -
`tests/conformance/test_http.py`'s `http_env` is where the `"postgres"`
backend is exercised instead.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import warnings
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from git_fixtures import seed_notes
from mcp import Client, MCPDeprecationWarning
from mcp.client.stdio import StdioServerParameters
from mcp_types.version import LATEST_HANDSHAKE_VERSION, LATEST_MODERN_VERSION

from memory_manager.vault.note import Note, serialize
from memory_manager.vault.ulid import new_ulid

__all__: list[str] = []

_CLI_ARGS = ("-m", "memory_manager.cli", "serve", "--stdio")

# `mcp_types.version.LATEST_HANDSHAKE_VERSION` / `LATEST_MODERN_VERSION` in the
# installed SDK (`mcp` 2.3.0) - spelled out as literals so a module-load-time
# assumption never silently drifts from what the SDK actually negotiates; see
# `test_negotiated_versions_match_the_sdks_registry` below.
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
        "memory_promote",
    }
)

_SEEDED_PATH = "personal/fact/favorite-color.md"
_SEEDED_BODY = "Blue.\n"

_READLINE_TIMEOUT = 5.0
_SHUTDOWN_TIMEOUT = 5.0

_PARAMETRIZE_VERSIONS = pytest.mark.parametrize(
    "version", [_HANDSHAKE_VERSION, _MODERN_VERSION], ids=["2025-11-25", "2026-07-28"]
)


@pytest_asyncio.fixture
async def stdio_env(tmp_path: Path, bare_remote: Path) -> dict[str, str]:
    """A stdio subprocess's env, a `"git"`-backend vault seeded with one note.

    `VAULT_REMOTE`/`VAULT_DIR`, no `DATABASE_URL` - `memory_index`/
    `memory_read` work without Postgres (`memory_manager.app.open_services`),
    and conformance here is about the wire protocol, not the search index.
    No `"postgres"` variant (module docstring): `cli.py`'s `serve --stdio`
    refuses that backend outright.
    """
    now = datetime(2025, 6, 1, tzinfo=UTC)
    note = Note(
        id=new_ulid(now),
        title="Favorite color",
        description="The user's favorite color, seeded for stdio conformance checks.",
        type="fact",
        created=now,
        updated=now,
        body=_SEEDED_BODY,
        tags=("color",),
    )
    content = serialize(note)

    seed_notes(bare_remote, {_SEEDED_PATH: content})
    return {"VAULT_REMOTE": str(bare_remote), "VAULT_DIR": str(tmp_path / "vault")}


def _stdio_params(env: Mapping[str, str]) -> StdioServerParameters:
    """`StdioServerParameters` to launch the real CLI (`memory-manager serve --stdio`).

    `sys.executable -m memory_manager.cli`, not the `memory-manager` console script:
    the running interpreter is already the one with this project installed, so this
    needs no `PATH` lookup for the entry point.
    """
    return StdioServerParameters(command=sys.executable, args=list(_CLI_ARGS), env=dict(env))


class _RawServer:
    """A `memory-manager serve --stdio` subprocess, driven by hand-written JSON-RPC lines.

    Used where `mcp.Client` has no seam: an unregistered method, a malformed line, and
    the bare `server/discover` probe. `read_message` is the one place every line this
    module reads from the subprocess's stdout passes through `json.loads` - a non-JSON
    line fails the test there, which is how every test using this class also checks
    that stdout carries nothing but JSON-RPC.
    """

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process
        self._next_id = 1

    @classmethod
    async def start(cls, env: Mapping[str, str]) -> _RawServer:
        full_env = {**os.environ, **env}
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            *_CLI_ARGS,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=full_env,
        )
        return cls(process)

    async def send(self, method: str, params: dict[str, Any] | None = None) -> int:
        """Send a request for `method`, returning its id."""
        request_id = self._next_id
        self._next_id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        await self._write_line(json.dumps(payload))
        return request_id

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a one-way notification (no `id`, no response expected)."""
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        await self._write_line(json.dumps(payload))

    async def send_malformed(self, text: str) -> None:
        """Write `text` verbatim as one line - not JSON-RPC, on purpose."""
        await self._write_line(text)

    async def _write_line(self, text: str) -> None:
        assert self._process.stdin is not None
        self._process.stdin.write((text + "\n").encode("utf-8"))
        await self._process.stdin.drain()

    async def read_message(self) -> dict[str, Any]:
        """Read one line of the subprocess's stdout and parse it as JSON-RPC.

        A line that fails to parse fails the calling test immediately - the stdio
        transport's stdout contract (JSON-RPC messages, nothing else) is exactly what
        this enforces.
        """
        assert self._process.stdout is not None
        raw = await asyncio.wait_for(self._process.stdout.readline(), timeout=_READLINE_TIMEOUT)
        assert raw, "server closed stdout before answering"
        message = json.loads(raw.decode("utf-8"))
        assert isinstance(message, dict)
        return message

    def is_alive(self) -> bool:
        return self._process.returncode is None

    async def close(self) -> None:
        if self._process.stdin is not None:
            self._process.stdin.close()
        try:
            await asyncio.wait_for(self._process.wait(), timeout=_SHUTDOWN_TIMEOUT)
        except TimeoutError:
            self._process.kill()
            await self._process.wait()


def _modern_meta() -> dict[str, Any]:
    """The `_meta` envelope every 2026-07-28+ request carries (no persistent handshake)."""
    return {
        "io.modelcontextprotocol/protocolVersion": _MODERN_VERSION,
        "io.modelcontextprotocol/clientInfo": {"name": "conformance-test", "version": "0"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }


def _params_for(version: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """`params` for a request at `version`: modern requests get `_meta` added, legacy don't."""
    payload = dict(params or {})
    if version == _MODERN_VERSION:
        payload["_meta"] = _modern_meta()
    return payload


async def _handshake(server: _RawServer, version: str) -> None:
    """Bring `server` to a ready state at `version`.

    The handshake era needs an `initialize`/`notifications/initialized` exchange first;
    the modern era needs nothing upfront - every request already carries its own
    `_meta` envelope via `_params_for`.
    """
    if version != _HANDSHAKE_VERSION:
        return
    request_id = await server.send(
        "initialize",
        {
            "protocolVersion": version,
            "capabilities": {},
            "clientInfo": {"name": "conformance-test", "version": "0"},
        },
    )
    message = await server.read_message()
    assert message["id"] == request_id
    await server.notify("notifications/initialized")


def test_negotiated_versions_match_the_sdks_registry() -> None:
    """The literals this module tests against are exactly the installed SDK's.

    Guards against this module quietly drifting from the SDK's own registry
    (`mcp_types.version`) if a dependency bump changes which revision is newest in
    either era - the module docstring's claim, checked rather than just stated.
    """
    assert _HANDSHAKE_VERSION == LATEST_HANDSHAKE_VERSION
    assert _MODERN_VERSION == LATEST_MODERN_VERSION


# --- mcp.Client-driven checks: handshake, tool/prompt surface, call_tool shapes -----


@pytest.mark.parametrize(
    ("mode", "expected_version"),
    [("legacy", _HANDSHAKE_VERSION), ("auto", _MODERN_VERSION)],
    ids=["2025-11-25", "2026-07-28"],
)
async def test_handshake_and_tool_surface(
    stdio_env: dict[str, str], mode: str, expected_version: str
) -> None:
    # `mode="auto"` (not the modern version string pinned directly) for the
    # 2026-07-28 case: a pinned version string skips the wire probe entirely and
    # `adopt()`s a client-side-synthesized `DiscoverResult` (`Client.__aenter__`,
    # `_synthesize_discover`) - `instructions`/`server_capabilities`/`server_info`
    # would then read back this test's own guess, not the server's real answer.
    # `mode="auto"` sends a real `server/discover` and adopts what comes back; this
    # server has no handshake-era fallback reason, so `auto` lands on 2026-07-28 here
    # the same way a real client would.
    async with Client(_stdio_params(stdio_env), mode=mode) as client:
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

        # Invalid arguments (missing required fields): the SDK reports this through
        # `isError`, not a JSON-RPC protocol error - the same path a tool's own raised
        # `ToolError` takes (`mcp.server.mcpserver.server._handle_call_tool`).
        invalid_args_result = await client.call_tool("memory_write", {})
        assert invalid_args_result.is_error is True

        # An unknown tool name takes the same `isError` path, not a raised MCPError.
        unknown_tool_result = await client.call_tool("no_such_tool", {})
        assert unknown_tool_result.is_error is True


async def test_ping_at_handshake_era_returns_empty_result(stdio_env: dict[str, str]) -> None:
    """`ping` only exists in the handshake era (`Client.send_ping` is removed at 2026-07-28+)."""
    async with Client(_stdio_params(stdio_env), mode="legacy") as client:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", MCPDeprecationWarning)
            result = await client.send_ping()
    assert result.model_dump(exclude_none=True) == {}


async def test_serve_stdio_refuses_the_postgres_backend() -> None:
    """`cli.py`'s `serve --stdio` refuses `STORAGE_BACKEND=postgres` outright
    (ADR-0008 addendum "identity sources and curate", #115/#116): exit code 2,
    before `open_services` ever runs - `DATABASE_URL` here is never actually
    connected to (`storage_backend_from_env` only checks it is non-empty).
    """
    env = {
        **os.environ,
        "STORAGE_BACKEND": "postgres",
        "DATABASE_URL": "postgresql://unused/unused",
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        *_CLI_ARGS,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    _stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=_SHUTDOWN_TIMEOUT)

    assert process.returncode == 2
    assert b"STORAGE_BACKEND=postgres" in stderr


# --- Raw JSON-RPC checks: protocol-layer errors, malformed input, discover ----------


@_PARAMETRIZE_VERSIONS
async def test_unknown_method_is_a_json_rpc_method_not_found_error(
    stdio_env: dict[str, str], version: str
) -> None:
    server = await _RawServer.start(stdio_env)
    try:
        await _handshake(server, version)
        request_id = await server.send("not/a/real/method", _params_for(version))
        message = await server.read_message()
        assert message["id"] == request_id
        assert message["error"]["code"] == -32601
    finally:
        await server.close()


@_PARAMETRIZE_VERSIONS
async def test_malformed_json_line_is_ignored_and_the_server_keeps_running(
    stdio_env: dict[str, str], version: str
) -> None:
    server = await _RawServer.start(stdio_env)
    try:
        await _handshake(server, version)
        await server.send_malformed("this line is not JSON at all")

        # The malformed line produces no stdout output of its own: the very next
        # line read is the answer to the next well-formed request, not an echo of
        # the bad input and not a stray error frame for it.
        request_id = await server.send("tools/list", _params_for(version))
        message = await server.read_message()
        assert message["id"] == request_id
        assert {tool["name"] for tool in message["result"]["tools"]} == _EXPECTED_TOOL_NAMES
        assert server.is_alive()
    finally:
        await server.close()


async def test_discover_advertises_the_modern_protocol_revision(stdio_env: dict[str, str]) -> None:
    server = await _RawServer.start(stdio_env)
    try:
        request_id = await server.send("server/discover", _params_for(_MODERN_VERSION))
        message = await server.read_message()
        assert message["id"] == request_id
        assert _MODERN_VERSION in message["result"]["supportedVersions"]
    finally:
        await server.close()
