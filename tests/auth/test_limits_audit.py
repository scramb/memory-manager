# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for rate limiting, the request body cap and the audit log (#39, #103).

Four layers, bottom to top:

- `memory_manager.auth.ratelimit.RateLimiter` against an `InMemorySharedState`
  with a fake clock - window limits, rollover and per-key isolation, none of
  which need real time or an ASGI app at all. `InMemorySharedState`'s own
  bounded-LRU eviction is `tests/auth/test_shared_state.py`'s to test, not
  this file's (#103) - it has no `PostgresSharedState` equivalent.
- `memory_manager.http`'s `_LimitsMiddleware`, driven through a real
  `create_app`/`open_services` pair the same way `tests/auth/
  test_static_tokens.py` drives bearer auth: a plain `httpx.AsyncClient`
  over an ASGI transport for the burst/isolation/oauth/webhook cases (no
  database needed - the middleware keys on the raw `Authorization` header
  and the client IP, independent of whether a token actually verifies),
  and a hand-built ASGI `receive` for the one case that needs a body with
  no `Content-Length` at all (oversized, chunked).
- `memory_manager.queue.WriteQueue`'s `add_audit_hook`: a raising hook
  must never fail the write it was notified about.
- `memory_manager.mcp.server.current_actor` and the audit rows a real
  `memory_write`/`memory_archive`/`memory_supersede` through the HTTP
  transport leaves in `audit_log` - `ok`, a version conflict and a
  secret-rejected write, checked for exactly the safe fields CLAUDE.md
  allows (never a note's content).
- `memory_manager.cli._serve_http`: `uvicorn.Config`/`.Server` faked out
  (`.serve()` never actually binds a socket) to check the one argument
  that matters here, `forwarded_allow_ips` - `ServerConfig.forwarded_
  allow_ips` end to end from `FORWARDED_ALLOW_IPS` into `uvicorn.Config`,
  not the hardcoded `"*"` this replaced.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import httpx
import httpx2
import pytest
import uvicorn
from git_fixtures import seed_notes
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.provider import AccessToken
from starlette.applications import Starlette
from starlette.types import Message, Scope

from memory_manager import cli
from memory_manager.app import open_services
from memory_manager.auth.ratelimit import RateLimiter
from memory_manager.auth.shared_state import InMemorySharedState
from memory_manager.auth.tokens import ALL_NAMESPACES, create_token
from memory_manager.config import ServerConfig, ServerConfigError, VaultConfig
from memory_manager.http import create_app
from memory_manager.mcp import server as mcp_server
from memory_manager.mcp.authz import READ_SCOPE, WRITE_SCOPE
from memory_manager.queue import WriteQueue, WriteRequest, WriteResult
from memory_manager.vault.note import Note, serialize
from memory_manager.vault.repo import Repo
from memory_manager.vault.ulid import new_ulid

_CREATED = datetime(2026, 1, 1, tzinfo=UTC)
_FAKE_AWS_ACCESS_KEY_ID = "AKIA" + "IOSFODNN7EXAMPLE"
_PUBLIC_URL = "https://mm.example.test"
_PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
_MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


def _note_bytes(path_id: str | None = None, **overrides: object) -> bytes:
    defaults: dict[str, object] = {
        "id": path_id or new_ulid(_CREATED),
        "title": "A note",
        "description": "A description.",
        "type": "fact",
        "created": _CREATED,
        "updated": _CREATED,
        "body": "Body.\n",
    }
    defaults.update(overrides)
    return serialize(Note(**defaults))  # type: ignore[arg-type]


def _new_note_content(path: str) -> str:
    return (
        "---\ntitle: New note\ndescription: Written by a test.\ntype: fact\n"
        f"---\n\nWritten to {path}.\n"
    )


# --- `auth.ratelimit.RateLimiter` against an `InMemorySharedState`, fake clock --


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def test_rate_limiter_allows_up_to_the_window_limit_then_rejects_with_retry_after() -> None:
    clock = _FakeClock()
    state = InMemorySharedState(clock=clock)
    limiter = RateLimiter(state=state, per_minute=60, burst=3)  # limit=3, window=180s

    assert await limiter.allow("k") == (True, 0.0)
    assert await limiter.allow("k") == (True, 0.0)
    assert await limiter.allow("k") == (True, 0.0)
    allowed, retry_after = await limiter.allow("k")

    assert allowed is False
    assert retry_after > 0


async def test_rate_limiter_rolls_over_once_the_window_passes_on_the_injected_clock() -> None:
    clock = _FakeClock()
    state = InMemorySharedState(clock=clock)
    limiter = RateLimiter(state=state, per_minute=60, burst=1)  # limit=1, window=60s
    assert (await limiter.allow("k"))[0] is True
    assert (await limiter.allow("k"))[0] is False

    clock.now += 60.0

    assert (await limiter.allow("k"))[0] is True


async def test_rate_limiter_isolates_two_keys_from_each_other() -> None:
    clock = _FakeClock()
    state = InMemorySharedState(clock=clock)
    limiter = RateLimiter(state=state, per_minute=60, burst=1)

    assert (await limiter.allow("a"))[0] is True
    assert (await limiter.allow("a"))[0] is False
    assert (await limiter.allow("b"))[0] is True


# --- `ServerConfig`'s limits knobs ------------------------------------------


def test_server_config_limits_default_to_the_documented_values() -> None:
    config = ServerConfig.from_env({})

    assert config.max_request_bytes == 1024 * 1024
    assert config.mcp_per_minute == 120.0
    assert config.mcp_burst == 30.0
    assert config.write_per_minute == 30.0
    assert config.oauth_per_minute == 30.0
    assert config.webhook_per_minute == 30.0
    assert config.forwarded_allow_ips == "127.0.0.1"


def test_server_config_limits_are_read_from_the_environment() -> None:
    config = ServerConfig.from_env(
        {
            "MAX_REQUEST_BYTES": "2048",
            "RATE_LIMIT_MCP_BURST": "5",
            "FORWARDED_ALLOW_IPS": "10.0.0.0/8",
        }
    )

    assert config.max_request_bytes == 2048
    assert config.mcp_burst == 5.0
    assert config.forwarded_allow_ips == "10.0.0.0/8"


def test_server_config_rejects_a_non_positive_rate_limit() -> None:
    with pytest.raises(ServerConfigError):
        ServerConfig.from_env({"RATE_LIMIT_MCP_BURST": "0"})


# --- `cli._serve_http` wires `forwarded_allow_ips` into uvicorn -------------


#: `uvicorn.Config`'s real constructor validates `app`/`host`/... in ways this
#: test has no interest in - this only records the keyword arguments
#: `cli._serve_http` actually passed, the one thing this test checks.
class _CapturedUvicornConfig:
    def __init__(self, app: object, **kwargs: object) -> None:
        self.app = app
        self.kwargs = kwargs


class _FakeUvicornServer:
    """Stands in for `uvicorn.Server`: `.serve()` returns immediately, never binds a
    socket - `cli._serve_http` never gets far enough to call `create_app`'s `lifespan`
    (that only happens inside the real `Server.serve()`), so no vault/database is
    needed for this test either."""

    def __init__(self, config: _CapturedUvicornConfig) -> None:
        self.config = config

    async def serve(self) -> None:
        return None


def _patch_uvicorn(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Replaces `uvicorn.Config`/`.Server` - the same module object `cli.py`'s own
    `import uvicorn` bound, so this reaches `_serve_http`'s calls without reading
    `cli`'s (unexported) `uvicorn` attribute back out. Returns the dict
    `_serve_http`'s `uvicorn.Config(...)` call's keyword arguments land in."""
    captured: dict[str, object] = {}

    def fake_config(app: object, **kwargs: object) -> _CapturedUvicornConfig:
        captured.update(kwargs)
        return _CapturedUvicornConfig(app, **kwargs)

    monkeypatch.setattr(uvicorn, "Config", fake_config)
    monkeypatch.setattr(uvicorn, "Server", _FakeUvicornServer)
    return captured


async def test_serve_http_passes_forwarded_allow_ips_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("HOST", "127.0.0.1")  # loopback, independent of the ambient $HOST
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "10.0.0.0/8")
    captured = _patch_uvicorn(monkeypatch)

    exit_code = await cli._serve_http()

    assert exit_code == 0
    assert captured["forwarded_allow_ips"] == "10.0.0.0/8"
    assert captured["proxy_headers"] is True


async def test_serve_http_defaults_forwarded_allow_ips_to_loopback_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("HOST", "127.0.0.1")  # loopback, independent of the ambient $HOST
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    captured = _patch_uvicorn(monkeypatch)

    exit_code = await cli._serve_http()

    assert exit_code == 0
    assert captured["forwarded_allow_ips"] == "127.0.0.1"
    assert captured["proxy_headers"] is True


# --- The HTTP transport: rate limiting and the body cap ---------------------


def _environ(bare_remote: Path, tmp_path: Path, database_url: str | None = None) -> dict[str, str]:
    environ = {"VAULT_REMOTE": str(bare_remote), "VAULT_DIR": str(tmp_path / "vault")}
    if database_url:
        environ["DATABASE_URL"] = database_url
    return environ


@asynccontextmanager
async def _running_app(
    environ: Mapping[str, str], config: ServerConfig
) -> AsyncIterator[Starlette]:
    app = create_app(lambda: open_services(environ), config)
    async with app.router.lifespan_context(app):
        yield app


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


async def test_mcp_requests_beyond_the_burst_get_429_with_retry_after(
    bare_remote: Path, tmp_path: Path
) -> None:
    config = ServerConfig(
        # per_minute == burst keeps the fixed window at 60s (burst * 60 / per_minute) -
        # long enough that this test's handful of requests can never roll over mid-run.
        public_url=_PUBLIC_URL,
        mcp_per_minute=2,
        mcp_burst=2,
        write_per_minute=600,
        write_burst=600,
    )
    async with _running_app(_environ(bare_remote, tmp_path), config) as app:
        transport = httpx.ASGITransport(app=app)
        headers = {**_MCP_HEADERS, "Authorization": "Bearer same-token"}
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            first = await client.post(config.mcp_path, json=_PING, headers=headers)
            second = await client.post(config.mcp_path, json=_PING, headers=headers)
            third = await client.post(config.mcp_path, json=_PING, headers=headers)

    assert first.status_code != 429
    assert second.status_code != 429
    assert third.status_code == 429
    assert "Retry-After" in third.headers


async def test_mcp_rate_limit_is_isolated_per_token(bare_remote: Path, tmp_path: Path) -> None:
    config = ServerConfig(
        public_url=_PUBLIC_URL,
        mcp_per_minute=1,
        mcp_burst=1,
        write_per_minute=600,
        write_burst=600,
    )
    async with _running_app(_environ(bare_remote, tmp_path), config) as app:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            headers_a = {**_MCP_HEADERS, "Authorization": "Bearer token-a"}
            headers_b = {**_MCP_HEADERS, "Authorization": "Bearer token-b"}

            first_a = await client.post(config.mcp_path, json=_PING, headers=headers_a)
            second_a = await client.post(config.mcp_path, json=_PING, headers=headers_a)
            first_b = await client.post(config.mcp_path, json=_PING, headers=headers_b)

    assert first_a.status_code != 429
    assert second_a.status_code == 429  # token-a's own burst is spent
    assert first_b.status_code != 429  # token-b has never been charged


async def test_oauth_endpoints_are_rate_limited_by_client_ip(
    bare_remote: Path, tmp_path: Path
) -> None:
    config = ServerConfig(public_url=_PUBLIC_URL, oauth_per_minute=1, oauth_burst=1)
    async with _running_app(_environ(bare_remote, tmp_path), config) as app:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            first = await client.post("/register", json={})
            second = await client.post("/register", json={})

    assert first.status_code != 429
    assert second.status_code == 429


async def test_vault_webhook_is_rate_limited_by_client_ip(
    bare_remote: Path, tmp_path: Path
) -> None:
    config = ServerConfig(public_url=_PUBLIC_URL, webhook_per_minute=1, webhook_burst=1)
    async with _running_app(_environ(bare_remote, tmp_path), config) as app:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            first = await client.post("/hooks/vault", content=b"{}")
            second = await client.post("/hooks/vault", content=b"{}")

    assert first.status_code != 429
    assert second.status_code == 429


async def test_mcp_rate_limiting_writes_to_the_shared_postgres_state(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    """Once a database is configured, `_LimitsMiddleware`'s limiter is backed by
    `auth.shared_state.PostgresSharedState` (#103) - a request against `mcp_path`
    must leave a row in `rate_limits`, not just a local, in-process counter."""
    config = ServerConfig(public_url=_PUBLIC_URL, mcp_per_minute=60, mcp_burst=60)
    environ = _environ(bare_remote, tmp_path, test_database_url)
    async with _running_app(environ, config) as app:
        pool: asyncpg.Pool = app.state.services.pool
        transport = httpx.ASGITransport(app=app)
        headers = {**_MCP_HEADERS, "Authorization": "Bearer same-token"}
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(config.mcp_path, json=_PING, headers=headers)
        assert response.status_code != 429

        row = await pool.fetchrow("select count from rate_limits")

    assert row is not None
    assert row["count"] == 1


async def test_oversized_chunked_mcp_body_with_no_content_length_is_413(
    bare_remote: Path, tmp_path: Path
) -> None:
    config = ServerConfig(public_url=_PUBLIC_URL, max_request_bytes=16)
    async with _running_app(_environ(bare_remote, tmp_path), config) as app:
        sent: list[Message] = []

        async def send(message: Message) -> None:
            sent.append(message)

        remaining = [b"x" * 10, b"x" * 10, b"x" * 10]  # 30 bytes total, no Content-Length header

        async def receive() -> Message:
            if remaining:
                chunk = remaining.pop(0)
                return {"type": "http.request", "body": chunk, "more_body": bool(remaining)}
            return {"type": "http.disconnect"}  # pragma: no cover - not reached once capped

        scope: Scope = {
            "type": "http",
            "method": "POST",
            "path": config.mcp_path,
            "raw_path": config.mcp_path.encode(),
            "headers": [(b"accept", b"application/json, text/event-stream")],
            "query_string": b"",
            "client": ("203.0.113.5", 12345),
            "server": ("testserver", 80),
            "scheme": "http",
            "http_version": "1.1",
        }

        await app(scope, receive, send)

    statuses = [message["status"] for message in sent if message["type"] == "http.response.start"]
    assert statuses == [413]


# --- `WriteQueue.add_audit_hook`: a raising hook never fails the write -----


async def test_a_raising_audit_hook_does_not_fail_the_write(
    vault_config: VaultConfig, caplog: pytest.LogCaptureFixture
) -> None:
    repo = Repo(vault_config)
    queue = WriteQueue(repo)
    await queue.start()
    try:

        async def raising_hook(
            request: WriteRequest, result: WriteResult | None, error: Exception | None
        ) -> None:
            raise RuntimeError("audit backend unreachable")

        queue.add_audit_hook(raising_hook)

        with caplog.at_level(logging.ERROR):
            result = await queue.submit(
                WriteRequest(
                    op="write",
                    path="personal/fact/a.md",
                    client="claude-code",
                    if_version="new",
                    content=_note_bytes(),
                )
            )

        assert isinstance(result, WriteResult)
        assert any("audit hook failed" in record.message for record in caplog.records)
    finally:
        await queue.stop()


# --- `mcp.server.current_actor` -------------------------------------------


def test_current_actor_is_stdio_without_an_access_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server, "current_access_token", lambda: None)

    assert mcp_server.current_actor() == "stdio"


def test_current_actor_is_the_oauth_tokens_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    token = AccessToken(
        token="mma_x",  # noqa: S106 - a fake test token, not a credential protecting anything real
        client_id="dyn-client",
        scopes=[],
        subject="alice",
    )
    monkeypatch.setattr(mcp_server, "current_access_token", lambda: token)

    assert mcp_server.current_actor() == "alice"


def test_current_actor_is_the_static_tokens_name(monkeypatch: pytest.MonkeyPatch) -> None:
    token = AccessToken(
        token="mm_x",  # noqa: S106 - a fake test token, not a credential protecting anything real
        client_id="static:ci",
        scopes=[],
    )
    monkeypatch.setattr(mcp_server, "current_access_token", lambda: token)

    assert mcp_server.current_actor() == "ci"


# --- Audit rows through the real HTTP transport -----------------------------


async def test_audit_row_recorded_for_a_successful_write(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    config = ServerConfig(public_url=_PUBLIC_URL)
    async with _running_app(_environ(bare_remote, tmp_path, test_database_url), config) as app:
        pool: asyncpg.Pool = app.state.services.pool
        plaintext, _info = await create_token(
            pool, "ci", scopes=[READ_SCOPE, WRITE_SCOPE], namespaces=[ALL_NAMESPACES]
        )
        path = "personal/fact/new.md"

        async with _authed_mcp_client(app, config, plaintext) as client:
            result = await client.call_tool(
                "memory_write",
                {"path": path, "content": _new_note_content(path), "if_version": "new"},
            )
            assert result.is_error is False

        row = await pool.fetchrow("select * from audit_log order by id desc limit 1")

    assert row is not None
    assert row["actor"] == "ci"
    assert row["client"] == "claude-code"
    assert row["op"] == "write"
    assert row["path"] == path
    assert row["commit_sha"] is not None
    assert row["outcome"] == "ok"
    detail = json.loads(row["detail"])
    assert set(detail) == {"version"}


async def test_audit_row_recorded_for_a_version_conflict_without_note_content(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    config = ServerConfig(public_url=_PUBLIC_URL)
    async with _running_app(_environ(bare_remote, tmp_path, test_database_url), config) as app:
        pool: asyncpg.Pool = app.state.services.pool
        plaintext, _info = await create_token(
            pool, "ci", scopes=[READ_SCOPE, WRITE_SCOPE], namespaces=[ALL_NAMESPACES]
        )
        path = "personal/fact/new.md"
        # Not a real secret - a marker string whose absence from the audit row is
        # what this test actually checks, standing in for a note's body text.
        secret_body = "this body text must never reach the audit log"  # noqa: S105

        async with _authed_mcp_client(app, config, plaintext) as client:
            first = await client.call_tool(
                "memory_write",
                {"path": path, "content": _new_note_content(path), "if_version": "new"},
            )
            assert first.is_error is False

            conflicting_content = (
                "---\ntitle: New note\ndescription: Written by a test.\ntype: fact\n"
                f"---\n\n{secret_body}\n"
            )
            second = await client.call_tool(
                "memory_write",
                {"path": path, "content": conflicting_content, "if_version": "new"},
            )
            assert second.is_error is True

        row = await pool.fetchrow("select * from audit_log order by id desc limit 1")

    assert row is not None
    assert row["outcome"] == "conflict"
    assert row["op"] == "write"
    detail = json.loads(row["detail"])
    assert detail["error"] == "VersionConflict"
    assert "current_content" not in detail
    assert secret_body not in str(dict(row))


async def test_audit_row_recorded_for_a_secret_rejected_write_without_the_secret(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    config = ServerConfig(public_url=_PUBLIC_URL)
    async with _running_app(_environ(bare_remote, tmp_path, test_database_url), config) as app:
        pool: asyncpg.Pool = app.state.services.pool
        plaintext, _info = await create_token(
            pool, "ci", scopes=[READ_SCOPE, WRITE_SCOPE], namespaces=[ALL_NAMESPACES]
        )
        path = "personal/fact/leaky.md"
        content = (
            "---\ntitle: New note\ndescription: Written by a test.\ntype: fact\n"
            f"---\n\nAWS_ACCESS_KEY_ID={_FAKE_AWS_ACCESS_KEY_ID}\n"
        )

        async with _authed_mcp_client(app, config, plaintext) as client:
            result = await client.call_tool(
                "memory_write", {"path": path, "content": content, "if_version": "new"}
            )
            assert result.is_error is True

        row = await pool.fetchrow("select * from audit_log order by id desc limit 1")

    assert row is not None
    assert row["outcome"] == "rejected"
    detail = json.loads(row["detail"])
    assert detail == {"error": "SecretRejected"}
    assert _FAKE_AWS_ACCESS_KEY_ID not in str(dict(row))


async def test_audit_row_recorded_for_an_archive(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    path = "personal/fact/to-archive.md"
    seed_notes(bare_remote, {path: _note_bytes(new_ulid(_CREATED))})
    config = ServerConfig(public_url=_PUBLIC_URL)
    async with _running_app(_environ(bare_remote, tmp_path, test_database_url), config) as app:
        pool: asyncpg.Pool = app.state.services.pool
        plaintext, _info = await create_token(
            pool, "ci", scopes=[READ_SCOPE, WRITE_SCOPE], namespaces=[ALL_NAMESPACES]
        )

        async with _authed_mcp_client(app, config, plaintext) as client:
            read_result = await client.call_tool("memory_read", {"items": [path]})
            version_str = read_result.structured_content["result"][0]["version"]

            archive_result = await client.call_tool(
                "memory_archive", {"path": path, "if_version": version_str}
            )
            assert archive_result.is_error is False

        row = await pool.fetchrow("select * from audit_log order by id desc limit 1")

    assert row is not None
    assert row["actor"] == "ci"
    assert row["op"] == "archive"
    assert row["outcome"] == "ok"
    assert row["path"] == "_archive/" + path


async def test_audit_row_recorded_for_a_supersede(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    old_path = "personal/fact/old.md"
    seed_notes(bare_remote, {old_path: _note_bytes(new_ulid(_CREATED))})
    new_path = "personal/fact/new-replacement.md"
    config = ServerConfig(public_url=_PUBLIC_URL)
    async with _running_app(_environ(bare_remote, tmp_path, test_database_url), config) as app:
        pool: asyncpg.Pool = app.state.services.pool
        plaintext, _info = await create_token(
            pool, "ci", scopes=[READ_SCOPE, WRITE_SCOPE], namespaces=[ALL_NAMESPACES]
        )

        async with _authed_mcp_client(app, config, plaintext) as client:
            read_result = await client.call_tool("memory_read", {"items": [old_path]})
            version_str = read_result.structured_content["result"][0]["version"]

            supersede_result = await client.call_tool(
                "memory_supersede",
                {
                    "old": old_path,
                    "new_path": new_path,
                    "new_content": _new_note_content(new_path),
                    "if_version": version_str,
                },
            )
            assert supersede_result.is_error is False

        row = await pool.fetchrow("select * from audit_log order by id desc limit 1")

    assert row is not None
    assert row["actor"] == "ci"
    assert row["op"] == "supersede"
    assert row["outcome"] == "ok"
    assert row["path"] == new_path
