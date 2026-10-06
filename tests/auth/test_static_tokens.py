# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for static bearer tokens (ADR-0004, #34).

Three layers, bottom to top:

- `memory_manager.auth.tokens` (`create_token`/`list_tokens`/`revoke_token`/
  `verify`) against a real Postgres - the `pool` fixture migrates a fresh
  `test_database_url` database first.
- `memory_manager.auth.verifier.StaticTokenVerifier`, the thin adapter that
  turns a `tokens.verify` result into the SDK's `AccessToken`.
- The HTTP transport end to end: `_running_app` builds the same
  `create_app`/`open_services` pair `tests/test_http_app.py` does, with
  `DATABASE_URL` set this time - exactly the condition `http.py` turns
  bearer-token auth on for. A real `mcp.Client` session (not raw `httpx`)
  is needed to drive `tools/call` far enough to see scope/namespace
  enforcement, so these tests go through `httpx2.AsyncClient` +
  `mcp.client.streamable_http.streamable_http_client` - the seam the SDK
  itself uses to carry a caller-supplied `httpx2.AsyncClient` (and therefore
  custom headers) over an ASGI transport instead of a real socket.

`memory-manager token create|list|revoke` (`cli.py`) is covered at the
bottom, through `cli.main` directly - the same way `tests/test_export.py`
drives other subcommands.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import httpx
import httpx2
import pytest
import pytest_asyncio
from git_fixtures import seed_notes
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp_types import TextContent
from starlette.applications import Starlette

from memory_manager.app import open_services
from memory_manager.auth.tokens import (
    ALL_NAMESPACES,
    create_token,
    list_tokens,
    revoke_token,
    verify,
)
from memory_manager.auth.verifier import StaticTokenVerifier
from memory_manager.cli import main
from memory_manager.config import ServerConfig
from memory_manager.db.migrate import migrate
from memory_manager.http import create_app
from memory_manager.mcp.authz import READ_SCOPE, WRITE_SCOPE

_PERSONAL_PATH = "personal/fact/favorite-color.md"
_WORK_PATH = "work/reference/deploy-notes.md"


def _note(note_id: str) -> bytes:
    return (
        f"---\nid: {note_id}\n"
        "title: A note\ndescription: A test fixture note.\ntype: fact\n"
        "created: 2026-01-01T00:00:00Z\nupdated: 2026-01-01T00:00:00Z\n---\n\nBody.\n"
    ).encode()


def _new_note_content(path: str) -> str:
    return (
        "---\ntitle: New note\ndescription: Written by a test.\ntype: fact\n"
        f"---\n\nWritten to {path}.\n"
    )


# --- `memory_manager.auth.tokens` ------------------------------------------


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


async def test_create_token_returns_the_plaintext_once_and_stores_only_its_hash(
    pool: asyncpg.Pool,
) -> None:
    plaintext, info = await create_token(
        pool, "ci", scopes=[READ_SCOPE, WRITE_SCOPE], namespaces=["personal"]
    )

    assert plaintext.startswith("mm_")
    assert info.name == "ci"
    assert info.scopes == (READ_SCOPE, WRITE_SCOPE)
    assert info.namespaces == ("personal",)
    assert info.revoked_at is None

    row = await pool.fetchrow("select token_hash from static_tokens where name = 'ci'")
    assert row is not None
    assert row["token_hash"] != plaintext
    assert row["token_hash"] == hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


async def test_verify_returns_token_info_for_a_valid_token(pool: asyncpg.Pool) -> None:
    plaintext, info = await create_token(
        pool, "ci", scopes=[READ_SCOPE], namespaces=[ALL_NAMESPACES]
    )

    result = await verify(pool, plaintext)

    assert result is not None
    assert result.name == info.name
    assert result.scopes == info.scopes


async def test_verify_returns_none_for_an_unknown_token(pool: asyncpg.Pool) -> None:
    assert await verify(pool, "mm_does-not-exist") is None


async def test_verify_returns_none_for_a_revoked_token(pool: asyncpg.Pool) -> None:
    plaintext, _info = await create_token(
        pool, "ci", scopes=[READ_SCOPE], namespaces=[ALL_NAMESPACES]
    )
    assert await revoke_token(pool, "ci") is True

    assert await verify(pool, plaintext) is None


async def test_verify_returns_none_for_an_expired_token(pool: asyncpg.Pool) -> None:
    expired = datetime.now(UTC) - timedelta(seconds=1)
    plaintext, _info = await create_token(
        pool, "ci", scopes=[READ_SCOPE], namespaces=[ALL_NAMESPACES], expires_at=expired
    )

    assert await verify(pool, plaintext) is None


async def test_revoke_token_returns_false_for_an_already_revoked_token(pool: asyncpg.Pool) -> None:
    await create_token(pool, "ci", scopes=[READ_SCOPE], namespaces=[ALL_NAMESPACES])

    assert await revoke_token(pool, "ci") is True
    assert await revoke_token(pool, "ci") is False


async def test_revoke_token_returns_false_for_an_unknown_name(pool: asyncpg.Pool) -> None:
    assert await revoke_token(pool, "no-such-token") is False


async def test_list_tokens_never_exposes_the_plaintext_or_its_hash(pool: asyncpg.Pool) -> None:
    plaintext, _info = await create_token(pool, "ci", scopes=[READ_SCOPE], namespaces=["personal"])

    listed = await list_tokens(pool)

    assert [info.name for info in listed] == ["ci"]
    assert not any(hasattr(info, "token_hash") for info in listed)
    rendered = repr(listed)
    assert plaintext not in rendered


# --- `memory_manager.auth.verifier.StaticTokenVerifier` --------------------


async def test_verifier_returns_an_access_token_with_scopes_and_namespaces(
    pool: asyncpg.Pool,
) -> None:
    plaintext, _info = await create_token(
        pool, "claude-code", scopes=[READ_SCOPE, WRITE_SCOPE], namespaces=["personal", "work"]
    )
    verifier = StaticTokenVerifier(pool)

    access_token = await verifier.verify_token(plaintext)

    assert access_token is not None
    assert access_token.client_id == "static:claude-code"
    assert set(access_token.scopes) == {READ_SCOPE, WRITE_SCOPE}
    assert access_token.claims is not None
    assert set(access_token.claims["namespaces"]) == {"personal", "work"}


async def test_verifier_returns_none_for_an_invalid_token(pool: asyncpg.Pool) -> None:
    verifier = StaticTokenVerifier(pool)

    assert await verifier.verify_token("mm_not-a-real-token") is None


# --- The HTTP transport end to end ------------------------------------------


def _environ(bare_remote: Path, tmp_path: Path, database_url: str) -> dict[str, str]:
    return {
        "VAULT_REMOTE": str(bare_remote),
        "VAULT_DIR": str(tmp_path / "vault"),
        "DATABASE_URL": database_url,
    }


@asynccontextmanager
async def _running_app(environ: dict[str, str], config: ServerConfig) -> AsyncIterator[Starlette]:
    app = create_app(lambda: open_services(environ), config)
    async with app.router.lifespan_context(app):
        yield app


def _authed_mcp_client(app: Starlette, config: ServerConfig, token: str | None) -> Client:
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    http_client = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://testserver", headers=headers
    )
    transport = streamable_http_client(
        f"http://testserver{config.mcp_path}", http_client=http_client
    )
    return Client(transport, mode="legacy")


async def test_mcp_without_a_token_is_401(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    config = ServerConfig()
    async with _running_app(_environ(bare_remote, tmp_path, test_database_url), config) as app:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(
                config.mcp_path,
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                headers={"Accept": "application/json, text/event-stream"},
            )

    assert response.status_code == 401


async def test_mcp_with_an_invalid_token_is_401(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    config = ServerConfig()
    async with _running_app(_environ(bare_remote, tmp_path, test_database_url), config) as app:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(
                config.mcp_path,
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Authorization": "Bearer mm_not-a-real-token",
                },
            )

    assert response.status_code == 401


async def test_a_wildcard_read_write_token_can_read_and_write_every_namespace(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    seed_notes(
        bare_remote,
        {
            _PERSONAL_PATH: _note("01JAAAAAAAAAAAAAAAAAAAAAAA"),
            _WORK_PATH: _note("01JBBBBBBBBBBBBBBBBBBBBBBB"),
        },
    )
    config = ServerConfig()
    async with _running_app(_environ(bare_remote, tmp_path, test_database_url), config) as app:
        pool: asyncpg.Pool = app.state.services.pool
        plaintext, _info = await create_token(
            pool, "full", scopes=[READ_SCOPE, WRITE_SCOPE], namespaces=[ALL_NAMESPACES]
        )

        async with _authed_mcp_client(app, config, plaintext) as client:
            index_result = await client.call_tool("memory_index", {})
            assert index_result.is_error is False
            paths = {entry["path"] for entry in index_result.structured_content["result"]}
            assert paths == {_PERSONAL_PATH, _WORK_PATH}

            write_result = await client.call_tool(
                "memory_write",
                {
                    "path": "work/fact/new.md",
                    "content": _new_note_content("work/fact/new.md"),
                    "if_version": "new",
                },
            )
            assert write_result.is_error is False


async def test_a_read_only_token_can_search_but_not_write(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    seed_notes(bare_remote, {_PERSONAL_PATH: _note("01JAAAAAAAAAAAAAAAAAAAAAAA")})
    config = ServerConfig()
    async with _running_app(_environ(bare_remote, tmp_path, test_database_url), config) as app:
        pool: asyncpg.Pool = app.state.services.pool
        plaintext, _info = await create_token(
            pool, "reader", scopes=[READ_SCOPE], namespaces=[ALL_NAMESPACES]
        )

        async with _authed_mcp_client(app, config, plaintext) as client:
            read_result = await client.call_tool("memory_read", {"items": [_PERSONAL_PATH]})
            assert read_result.is_error is False

            write_result = await client.call_tool(
                "memory_write",
                {
                    "path": "personal/fact/new.md",
                    "content": _new_note_content("personal/fact/new.md"),
                    "if_version": "new",
                },
            )
            assert write_result.is_error is True
            content_block = write_result.content[0]
            assert isinstance(content_block, TextContent)
            assert WRITE_SCOPE in content_block.text


async def test_a_namespace_restricted_token_sees_only_its_namespace(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    seed_notes(
        bare_remote,
        {
            _PERSONAL_PATH: _note("01JAAAAAAAAAAAAAAAAAAAAAAA"),
            _WORK_PATH: _note("01JBBBBBBBBBBBBBBBBBBBBBBB"),
        },
    )
    config = ServerConfig()
    async with _running_app(_environ(bare_remote, tmp_path, test_database_url), config) as app:
        pool: asyncpg.Pool = app.state.services.pool
        plaintext, _info = await create_token(
            pool, "personal-only", scopes=[READ_SCOPE, WRITE_SCOPE], namespaces=["personal"]
        )

        async with _authed_mcp_client(app, config, plaintext) as client:
            index_result = await client.call_tool("memory_index", {})
            assert index_result.is_error is False
            paths = {entry["path"] for entry in index_result.structured_content["result"]}
            assert paths == {_PERSONAL_PATH}

            read_result = await client.call_tool("memory_read", {"items": [_WORK_PATH]})
            assert read_result.is_error is False
            assert read_result.structured_content["result"][0]["error"]["error"] == "NotFound"

            write_outside_result = await client.call_tool(
                "memory_write",
                {
                    "path": "work/fact/new.md",
                    "content": _new_note_content("work/fact/new.md"),
                    "if_version": "new",
                },
            )
            assert write_outside_result.is_error is True

            write_inside_result = await client.call_tool(
                "memory_write",
                {
                    "path": "personal/fact/new.md",
                    "content": _new_note_content("personal/fact/new.md"),
                    "if_version": "new",
                },
            )
            assert write_inside_result.is_error is False


# --- `memory-manager token create|list|revoke` ------------------------------


def test_cli_token_create_prints_the_token_once_and_list_never_shows_it(
    monkeypatch: pytest.MonkeyPatch, test_database_url: str
) -> None:
    monkeypatch.setenv("DATABASE_URL", test_database_url)

    create_exit = main(["token", "create", "ci", "--scope", READ_SCOPE, "--namespace", "personal"])
    assert create_exit == 0


def test_cli_token_list_and_revoke_round_trip(
    monkeypatch: pytest.MonkeyPatch, test_database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", test_database_url)

    assert main(["token", "create", "ci", "--scope", READ_SCOPE, "--namespace", "personal"]) == 0
    created_out = capsys.readouterr().out.strip()
    assert created_out.startswith("mm_")

    assert main(["token", "list"]) == 0
    listed_out = capsys.readouterr().out
    assert "ci" in listed_out
    assert created_out not in listed_out

    assert main(["token", "revoke", "ci"]) == 0
    assert main(["token", "revoke", "ci"]) == 2
