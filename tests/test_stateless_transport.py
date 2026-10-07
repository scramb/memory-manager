# SPDX-License-Identifier: AGPL-3.0-only
"""Pins the four stateless Streamable HTTP behaviours ADR-0009 §1 relies on for
replica-agnostic routing (`stateless_http=True`, `json_response=True` in
`src/memory_manager/http.py`'s `create_app`), against a real
`memory-manager serve --http` subprocess with no database configured:

- neither `initialize` nor `tools/call` ever sets an `Mcp-Session-Id` response
  header - any replica can answer any request, there is no session to route to
- a legacy GET (`Accept: text/event-stream`) is answered `200 text/event-stream`,
  not `405` - the SDK's "born-ready" stateless path holds an empty stream open
  rather than refusing it, which is also what sidesteps a known Claude Code
  failure mode on `405` to this endpoint (anthropics/claude-code#39790)
- DELETE is `405` ("session termination not supported" - there is no session)
- `tools/call` works without a prior `initialize` - the stateless transport
  seeds a "born-ready" connection from the (absent, here) `MCP-Protocol-Version`
  header, so a client that skips the handshake still gets served

See ADR-0009 §1 and `docs/research/enterprise.md` §2.1 for the SDK-source
evidence behind each of the four. `tests/conformance/test_http.py` already
drives the full MCP wire protocol (`mcp.Client`, both protocol eras) - this
module stops at exactly these four transport-shape assertions, raw over
`httpx`, since `Client` has no seam to inspect response headers on
`initialize`/`tools/call` or to send a bare GET/DELETE.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest_asyncio
from git_fixtures import seed_notes
from http_fixtures import Server, run_http_server

from memory_manager.vault.note import Note, serialize
from memory_manager.vault.ulid import new_ulid

_SEEDED_PATH = "personal/fact/favorite-color.md"
_SEEDED_BODY = "Blue.\n"
_MCP_HEADERS = {"Accept": "application/json, text/event-stream"}
_SESSION_ID_HEADER = "mcp-session-id"


@pytest_asyncio.fixture
async def stateless_server(tmp_path: Path, bare_remote: Path) -> AsyncIterator[Server]:
    """A real `serve --http` subprocess, no `DATABASE_URL` - same shape as
    `tests/conformance/test_http.py`'s `http_server`, seeded with one note so a
    real `memory_index` call has something to return."""
    now = datetime(2025, 6, 1, tzinfo=UTC)
    note = Note(
        id=new_ulid(now),
        title="Favorite color",
        description="The user's favorite color, seeded for stateless-transport checks.",
        type="fact",
        created=now,
        updated=now,
        body=_SEEDED_BODY,
        tags=("color",),
    )
    seed_notes(bare_remote, {_SEEDED_PATH: serialize(note)})
    env = {"VAULT_REMOTE": str(bare_remote), "VAULT_DIR": str(tmp_path / "vault")}
    async with run_http_server(env) as server:
        yield server


def _tools_call_body(tool: str, arguments: dict[str, object]) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }


async def test_initialize_issues_no_session_id_header(stateless_server: Server) -> None:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "stateless-transport-test", "version": "0"},
        },
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(stateless_server.mcp_url, json=body, headers=_MCP_HEADERS)

    assert response.status_code == 200
    assert _SESSION_ID_HEADER not in response.headers


async def test_tools_call_issues_no_session_id_header(stateless_server: Server) -> None:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            stateless_server.mcp_url,
            json=_tools_call_body("memory_index", {}),
            headers=_MCP_HEADERS,
        )

    assert response.status_code == 200
    assert _SESSION_ID_HEADER not in response.headers


async def test_legacy_get_is_200_text_event_stream_not_405(stateless_server: Server) -> None:
    """Reads only the response's status line and headers, via a bounded-timeout
    stream, then closes it: nothing is ever written to this stream (no producer
    exists in the per-request stateless transport, `docs/research/enterprise.md`
    §2.1), so reading a body would simply hang until the subprocess is killed.
    """
    async with (
        httpx.AsyncClient() as client,
        client.stream(
            "GET", stateless_server.mcp_url, headers={"Accept": "text/event-stream"}, timeout=5.0
        ) as response,
    ):
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")


async def test_delete_is_405(stateless_server: Server) -> None:
    async with httpx.AsyncClient() as client:
        response = await client.delete(stateless_server.mcp_url, headers=_MCP_HEADERS)

    assert response.status_code == 405


async def test_tools_call_works_without_a_prior_initialize(stateless_server: Server) -> None:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            stateless_server.mcp_url,
            json=_tools_call_body("memory_index", {}),
            headers=_MCP_HEADERS,
        )

    assert response.status_code == 200
    payload = response.json()
    assert "error" not in payload
    assert payload["result"]["isError"] is False
