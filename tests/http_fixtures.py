# SPDX-License-Identifier: AGPL-3.0-only
"""Shared subprocess helpers for tests that drive a real `memory-manager serve
--http` process: a free port, starting the subprocess with extra environment
on top of the ambient one, waiting for `/healthz`, and tearing it down again
(`terminate()`, `kill()` after a timeout).

Used by `tests/conformance/test_http.py` (protocol conformance over the real
transport), `tests/test_stateless_transport.py` (the four stateless-transport
behaviours ADR-0009 §1 pins) and `tests/test_shutdown.py` (graceful shutdown,
ADR-0009 §5) - three different sets of assertions against the same kind of
process, so starting and stopping it lives here once.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx

__all__ = ["CLI_ARGS", "Server", "free_port", "run_http_server", "wait_until_ready"]

CLI_ARGS = ("-m", "memory_manager.cli", "serve", "--http")

_STARTUP_TIMEOUT = 10.0
_SHUTDOWN_TIMEOUT = 5.0
_POLL_INTERVAL = 0.1


@dataclass
class Server:
    process: asyncio.subprocess.Process
    base_url: str
    mcp_url: str


def free_port() -> int:
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


async def wait_until_ready(process: asyncio.subprocess.Process, base_url: str) -> None:
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


@asynccontextmanager
async def run_http_server(env: Mapping[str, str]) -> AsyncIterator[Server]:
    """Start `memory-manager serve --http` on a fresh free port with `env` merged
    onto the ambient environment (`HOST`/`PORT` are always overridden, last), wait
    for `/healthz`, yield a `Server`, then terminate it - `kill()` if it has not
    exited within `_SHUTDOWN_TIMEOUT` seconds.
    """
    port = free_port()
    full_env = {**os.environ, **env, "HOST": "127.0.0.1", "PORT": str(port)}
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
        yield Server(process=process, base_url=base_url, mcp_url=f"{base_url}/mcp")
    finally:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=_SHUTDOWN_TIMEOUT)
        except TimeoutError:
            process.kill()
            await process.wait()
