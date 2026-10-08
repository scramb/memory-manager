# SPDX-License-Identifier: AGPL-3.0-only
"""Fixtures that put `tests/mock_idp`'s mock in front of a test, two ways.

`mock_idp_client` is an in-process `httpx.AsyncClient` over
`httpx.ASGITransport(app=create_app())` - no subprocess, no port. Every
scenario in `tests/mock_idp/test_mock_idp.py` itself uses this one:
exercising the mock's own behaviour needs no real network, the same reason
`tests/auth/conftest.py`'s `FakeOidcProvider` is an in-process
`httpx.MockTransport` rather than a server.

`mock_idp_server` instead starts the real subprocess
(`uv run python -m tests.mock_idp --host ... --port ...`) on a free port,
reusing `tests/http_fixtures.py`'s `free_port` and following the same
startup/teardown shape as that module's `run_http_server`. This is the shape
a future WP-24 facade test needs - a real base URL to point `LOGIN_MODE=entra`
at - not something `test_mock_idp.py` itself needs, so it is exercised here
only by `test_subprocess_launcher_serves_the_same_app`.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest_asyncio
from http_fixtures import free_port
from mock_idp.app import create_app

__all__ = ["MockIdpServer", "mock_idp_client", "mock_idp_server"]

_STARTUP_TIMEOUT = 10.0
_SHUTDOWN_TIMEOUT = 5.0
_POLL_INTERVAL = 0.1
_REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class MockIdpServer:
    process: asyncio.subprocess.Process
    base_url: str


@pytest_asyncio.fixture
async def mock_idp_client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="https://mock-idp.test") as client:
        yield client


async def _wait_until_ready(process: asyncio.subprocess.Process, base_url: str) -> None:
    deadline = asyncio.get_running_loop().time() + _STARTUP_TIMEOUT
    async with httpx.AsyncClient() as client:
        while True:
            if process.returncode is not None:
                stderr = await process.stderr.read() if process.stderr else b""
                raise AssertionError(
                    f"mock idp exited early (code {process.returncode}): "
                    f"{stderr.decode('utf-8', errors='replace')}"
                )
            try:
                response = await client.get(f"{base_url}/_mock/health", timeout=1.0)
                if response.status_code == 200:
                    return
            except httpx.TransportError:
                pass
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError("mock idp did not become ready in time")
            await asyncio.sleep(_POLL_INTERVAL)


@pytest_asyncio.fixture
async def mock_idp_server() -> AsyncIterator[MockIdpServer]:
    port = free_port()
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tests.mock_idp",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        cwd=_REPO_ROOT,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        await _wait_until_ready(process, base_url)
        yield MockIdpServer(process=process, base_url=base_url)
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=_SHUTDOWN_TIMEOUT)
            except TimeoutError:
                process.kill()
                await process.wait()
