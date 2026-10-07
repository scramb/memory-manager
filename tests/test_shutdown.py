# SPDX-License-Identifier: AGPL-3.0-only
"""Graceful shutdown (ADR-0009 §1/§5, #105): `SIGTERM` drains an in-flight tool
call within a bounded time, and `/readyz` fails fast as soon as the signal
arrives - before the grace period even starts, so a load balancer stops
routing new requests here while the one already in flight still gets to
finish.

Three layers, cheapest first:

- `ServerConfig.shutdown_grace_seconds` (`SHUTDOWN_GRACE_SECONDS`) - plain
  `from_env` parsing, same shape as every other `_parse_positive_int` knob.
- `cli._serve_http` wires it into `uvicorn.Config(timeout_graceful_shutdown=...)`
  and serves with `http.GracefulShutdownServer`, not plain `uvicorn.Server` -
  checked the same way `tests/auth/test_limits_audit.py` checks
  `forwarded_allow_ips`, with `uvicorn.Config`/`.Server` faked out so this
  never binds a socket.
- `GracefulShutdownServer.handle_exit` flips `app.state.draining` - checked
  in-process (`_readyz` 503 the instant `handle_exit` runs, no real signal
  needed) and, for the two end-to-end claims that need a real process (an
  in-flight write surviving `SIGTERM`, an open legacy GET stream not blocking
  it), against a real `memory-manager serve --http` subprocess
  (`tests/http_fixtures.py`).
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import uvicorn
from git_fixtures import seed_notes
from http_fixtures import CLI_ARGS, free_port, wait_until_ready
from starlette.applications import Starlette

from memory_manager import cli
from memory_manager.app import open_services
from memory_manager.config import ServerConfig, ServerConfigError
from memory_manager.http import GracefulShutdownServer, create_app
from memory_manager.vault.git import Git
from memory_manager.vault.note import Note, serialize
from memory_manager.vault.ulid import new_ulid

_MCP_HEADERS = {"Accept": "application/json, text/event-stream"}
_SEEDED_PATH = "personal/fact/favorite-color.md"
_SEEDED_BODY = "Blue.\n"
_WRITE_PATH = "personal/fact/shutdown-write.md"
_HOOK_HARD_CAP_SECONDS = 30


def _tools_call_body(tool: str, arguments: dict[str, object]) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }


def _write_note_content(path: str) -> str:
    return (
        "---\ntitle: Written during shutdown\n"
        "description: Written by tests/test_shutdown.py.\ntype: fact\n"
        f"---\n\nWritten to {path} while a SIGTERM was in flight.\n"
    )


# --- `ServerConfig.shutdown_grace_seconds` ----------------------------------


def test_shutdown_grace_seconds_defaults_to_twenty() -> None:
    config = ServerConfig.from_env({})
    assert config.shutdown_grace_seconds == 20


def test_shutdown_grace_seconds_is_read_from_the_environment() -> None:
    config = ServerConfig.from_env({"SHUTDOWN_GRACE_SECONDS": "45"})
    assert config.shutdown_grace_seconds == 45


def test_shutdown_grace_seconds_rejects_a_non_positive_value() -> None:
    with pytest.raises(ServerConfigError):
        ServerConfig.from_env({"SHUTDOWN_GRACE_SECONDS": "0"})


def test_shutdown_grace_seconds_rejects_a_non_integer_value() -> None:
    with pytest.raises(ServerConfigError):
        ServerConfig.from_env({"SHUTDOWN_GRACE_SECONDS": "abc"})


# --- `cli._serve_http` wires it into `uvicorn.Config`/`GracefulShutdownServer` --


class _CapturedUvicornConfig:
    def __init__(self, app: object, **kwargs: object) -> None:
        self.app = app
        self.kwargs = kwargs


class _FakeUvicornServer:
    """`.serve()` returns immediately, never binds a socket - same reasoning as
    `tests/auth/test_limits_audit.py`'s own `_FakeUvicornServer`."""

    def __init__(self, config: _CapturedUvicornConfig, *args: object, **kwargs: object) -> None:
        self.config = config

    async def serve(self) -> None:
        return None


async def test_serve_http_passes_shutdown_grace_seconds_to_uvicorn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("HOST", "127.0.0.1")
    monkeypatch.setenv("SHUTDOWN_GRACE_SECONDS", "45")
    captured: dict[str, object] = {}

    def fake_config(app: object, **kwargs: object) -> _CapturedUvicornConfig:
        captured.update(kwargs)
        return _CapturedUvicornConfig(app, **kwargs)

    monkeypatch.setattr(uvicorn, "Config", fake_config)
    monkeypatch.setattr(cli, "GracefulShutdownServer", _FakeUvicornServer)

    exit_code = await cli._serve_http()

    assert exit_code == 0
    assert captured["timeout_graceful_shutdown"] == 45


async def test_serve_http_defaults_shutdown_grace_seconds_to_twenty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("HOST", "127.0.0.1")
    monkeypatch.delenv("SHUTDOWN_GRACE_SECONDS", raising=False)
    captured: dict[str, object] = {}

    def fake_config(app: object, **kwargs: object) -> _CapturedUvicornConfig:
        captured.update(kwargs)
        return _CapturedUvicornConfig(app, **kwargs)

    monkeypatch.setattr(uvicorn, "Config", fake_config)
    monkeypatch.setattr(cli, "GracefulShutdownServer", _FakeUvicornServer)

    exit_code = await cli._serve_http()

    assert exit_code == 0
    assert captured["timeout_graceful_shutdown"] == 20


# --- `GracefulShutdownServer.handle_exit` flips `/readyz`, in-process ------


def _environ(bare_remote: Path, tmp_path: Path) -> dict[str, str]:
    return {"VAULT_REMOTE": str(bare_remote), "VAULT_DIR": str(tmp_path / "vault")}


@asynccontextmanager
async def _running_app(
    environ: dict[str, str], config: ServerConfig
) -> AsyncIterator[tuple[Starlette, httpx.AsyncClient]]:
    app = create_app(lambda: open_services(environ), config)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield app, client


async def test_handle_exit_makes_readyz_503_before_the_grace_period_starts(
    bare_remote: Path, tmp_path: Path
) -> None:
    config = ServerConfig()
    async with _running_app(_environ(bare_remote, tmp_path), config) as (app, client):
        before = await client.get("/readyz")
        assert before.status_code == 200
        assert before.json()["draining"] is False

        uvicorn_config = uvicorn.Config(app, host="127.0.0.1", port=0)
        server = GracefulShutdownServer(uvicorn_config)
        server.handle_exit(signal.SIGTERM, None)

        after = await client.get("/readyz")

    assert after.status_code == 503
    assert after.json() == {"ready": False, "draining": True}


# --- End to end, a real subprocess (ADR-0009 §5) ----------------------------


def _install_blocking_pre_receive_hook(remote: Path, marker: Path, release: Path) -> None:
    """A `pre-receive` hook that marks `marker` the instant a push reaches the
    remote, then blocks until `release` exists (or `_HOOK_HARD_CAP_SECONDS`
    pass) - the only reliable way to hold a `memory_write` tool call's commit
    +push in flight for exactly as long as a test needs, confirmed against a
    throwaway bare repo (`git push` to a local path still runs the target's
    `hooks/pre-receive`, the same as a real network push would).
    """
    hook_path = remote / "hooks" / "pre-receive"
    hook_path.write_text(
        "#!/bin/sh\n"
        f"touch '{marker}'\n"
        "i=0\n"
        f"while [ ! -f '{release}' ] && [ \"$i\" -lt {int(_HOOK_HARD_CAP_SECONDS / 0.2)} ]; do\n"
        "  sleep 0.2\n"
        "  i=$((i+1))\n"
        "done\n"
        "exit 0\n"
    )
    hook_path.chmod(0o755)


async def _wait_for(
    predicate: Callable[[], bool], *, timeout: float, interval: float = 0.1
) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError("condition was never met")
        await asyncio.sleep(interval)


async def test_sigterm_drains_an_in_flight_write_before_the_process_exits(
    bare_remote: Path, tmp_path: Path
) -> None:
    now = datetime(2025, 6, 1, tzinfo=UTC)
    seeded = Note(
        id=new_ulid(now),
        title="Favorite color",
        description="Seeded so the subprocess has something to clone (#105).",
        type="fact",
        created=now,
        updated=now,
        body=_SEEDED_BODY,
        tags=("color",),
    )
    seed_notes(bare_remote, {_SEEDED_PATH: serialize(seeded)})

    port = free_port()
    full_env = {
        **os.environ,
        "VAULT_REMOTE": str(bare_remote),
        "VAULT_DIR": str(tmp_path / "vault"),
        "HOST": "127.0.0.1",
        "PORT": str(port),
        "SHUTDOWN_GRACE_SECONDS": "10",
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        *CLI_ARGS,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=full_env,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        await wait_until_ready(process, base_url)

        # Only now - after the subprocess's own startup clone/sync has already
        # run - does a push into `bare_remote` block, so the hook never delays
        # anything but the one write this test triggers itself.
        marker = tmp_path / "pre-receive-marker"
        release = tmp_path / "pre-receive-release"
        _install_blocking_pre_receive_hook(bare_remote, marker, release)

        async with httpx.AsyncClient(timeout=20.0) as client:
            write_task = asyncio.create_task(
                client.post(
                    f"{base_url}/mcp",
                    json=_tools_call_body(
                        "memory_write",
                        {
                            "path": _WRITE_PATH,
                            "content": _write_note_content(_WRITE_PATH),
                            "if_version": "new",
                        },
                    ),
                    headers=_MCP_HEADERS,
                )
            )

            await _wait_for(marker.exists, timeout=10.0)

            process.send_signal(signal.SIGTERM)

            # /readyz may answer 503, or the connection may already be
            # refused - both mean the same thing here: this replica is
            # draining and must not be routed new requests.
            try:
                readyz = await client.get(f"{base_url}/readyz", timeout=2.0)
            except httpx.TransportError:
                pass
            else:
                assert readyz.status_code == 503

            await asyncio.sleep(0.5)
            assert process.returncode is None, "process exited before the in-flight write finished"

            release.touch()

            write_response = await write_task

        assert write_response.status_code == 200
        write_payload = write_response.json()
        assert write_payload["result"]["isError"] is False

        # `memory_write` normalizes the frontmatter server-side (adds `id`,
        # `created`/`updated`, ...) - the body text survives untouched, which
        # is enough to confirm this exact write reached the remote.
        shown = Git(cwd=bare_remote).run("show", f"main:{_WRITE_PATH}")
        assert f"Written to {_WRITE_PATH} while a SIGTERM was in flight." in shown.stdout.decode(
            "utf-8"
        )

        returncode = await asyncio.wait_for(process.wait(), timeout=5.0)
        assert returncode == -signal.SIGTERM

        stderr = (await process.stderr.read()) if process.stderr else b""
        assert "Application shutdown complete." in stderr.decode("utf-8", errors="replace")
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def test_an_open_legacy_get_stream_does_not_hold_up_shutdown(
    bare_remote: Path, tmp_path: Path
) -> None:
    """ADR-0009 §5 / `docs/research/enterprise.md` §2.4: `sse_starlette` drains the
    open, empty legacy GET stream automatically once `GracefulShutdownServer.
    handle_exit` has run - the process exits well under the 10 s grace period
    configured here, not waiting it out.
    """
    port = free_port()
    full_env = {
        **os.environ,
        "VAULT_REMOTE": str(bare_remote),
        "VAULT_DIR": str(tmp_path / "vault"),
        "HOST": "127.0.0.1",
        "PORT": str(port),
        "SHUTDOWN_GRACE_SECONDS": "10",
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        *CLI_ARGS,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=full_env,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        await wait_until_ready(process, base_url)

        async with (
            httpx.AsyncClient() as client,
            client.stream(
                "GET", f"{base_url}/mcp", headers={"Accept": "text/event-stream"}, timeout=15.0
            ) as get_response,
        ):
            assert get_response.status_code == 200

            started = time.monotonic()
            process.send_signal(signal.SIGTERM)

            returncode = await asyncio.wait_for(process.wait(), timeout=3.0)
            elapsed = time.monotonic() - started

        assert returncode == -signal.SIGTERM
        assert elapsed < 3.0
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
