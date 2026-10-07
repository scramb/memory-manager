# SPDX-License-Identifier: AGPL-3.0-only
"""Unit tests for the Streamable HTTP ASGI app (#33): `/healthz`, `/readyz`,
Origin validation and the vault webhook.

Drives `create_app`'s own `lifespan` directly (`app.router.lifespan_context(app)`)
rather than simulating the ASGI lifespan protocol's "lifespan.startup"/
"lifespan.shutdown" messages by hand - the same async context manager
`http.py` itself nests `mcp_app`'s lifespan into. `httpx.ASGITransport` then
drives ordinary "http" scope requests against the already-started app, the
way `tests/conformance/test_http.py` drives a real subprocess instead.

The full MCP wire protocol (tools/list, tools/call, ...) over this
transport is `tests/conformance/test_http.py`'s job; these tests stop at
"a request reaches the mounted MCP sub-app" (`test_unknown_path_reaches_the_mcp_mount`)
and otherwise cover what sits around it.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import httpx
import pytest_asyncio
from git_fixtures import human_commit
from starlette.applications import Starlette

from memory_manager import __commit__, __version__
from memory_manager.app import open_services
from memory_manager.config import ServerConfig
from memory_manager.http import HEALTH_PATH, READY_PATH, WEBHOOK_PATH, create_app

_SOURCE_URL = "https://github.com/scramb/memory-manager"
_WEBHOOK_SECRET = "s3cr3t"  # noqa: S105 - test fixture value, not a real secret
_NOTE_PATH = "personal/fact/webhook-added.md"
_NOTE_CONTENT = b"""---
id: 01JAZZZZZZZZZZZZZZZZZZZZZZ
title: Added by webhook
description: Pushed to the remote directly, picked up by the vault webhook.
type: fact
created: 2025-06-01T00:00:00Z
updated: 2025-06-01T00:00:00Z
---

Added out of band.
"""


def _environ(bare_remote: Path, tmp_path: Path) -> dict[str, str]:
    return {"VAULT_REMOTE": str(bare_remote), "VAULT_DIR": str(tmp_path / "vault")}


@pytest_asyncio.fixture
async def app_role(admin_database_url: str, test_database_url: str) -> AsyncIterator[str]:
    """A disposable, non-owner, non-superuser role for the RLS request path
    (ADR-0008 addendum, #116) - `open_services` requires `DATABASE_APP_ROLE`
    for `STORAGE_BACKEND=postgres` and grants it the content-table
    privileges it needs at startup (`db.rls.grant_app_role`). Neither test
    using this fixture touches a content table, so no `namespaces`/`users`
    registry row or principal is seeded here - only the role needs to exist.
    """
    role = f"mm_test_app_{secrets.token_hex(8)}"
    admin_conn = await asyncpg.connect(admin_database_url)
    try:
        await admin_conn.execute(f'create role "{role}" nologin nosuperuser nobypassrls')
    finally:
        await admin_conn.close()
    try:
        yield role
    finally:
        # See `tests/test_app.py`'s identical fixture for why `drop owned by`
        # against `test_database_url` has to run before the cluster-wide
        # `DROP ROLE` below.
        owned_conn: asyncpg.Connection | None
        try:
            owned_conn = await asyncpg.connect(test_database_url)
        except asyncpg.PostgresError:
            owned_conn = None
        if owned_conn is not None:
            try:
                await owned_conn.execute(f'drop owned by "{role}"')
            finally:
                await owned_conn.close()
        admin_conn = await asyncpg.connect(admin_database_url)
        try:
            await admin_conn.execute(f'drop role if exists "{role}"')
        finally:
            await admin_conn.close()


@asynccontextmanager
async def _running_app(
    environ: dict[str, str], config: ServerConfig
) -> AsyncIterator[tuple[Starlette, httpx.AsyncClient]]:
    app = create_app(lambda: open_services(environ), config)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield app, client


# --- /healthz -------------------------------------------------------------


async def test_healthz_reports_build_metadata(bare_remote: Path, tmp_path: Path) -> None:
    config = ServerConfig()
    async with _running_app(_environ(bare_remote, tmp_path), config) as (_app, client):
        response = await client.get(HEALTH_PATH)

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "version": __version__,
        "commit": __commit__,
        "source": _SOURCE_URL,
    }


# --- /readyz ----------------------------------------------------------------


async def test_readyz_is_ready_once_the_vault_is_cloned(bare_remote: Path, tmp_path: Path) -> None:
    config = ServerConfig()
    async with _running_app(_environ(bare_remote, tmp_path), config) as (_app, client):
        response = await client.get(READY_PATH)

    assert response.status_code == 200
    assert response.json() == {"ready": True, "vault": True, "database": True, "draining": False}


async def test_readyz_is_503_when_the_vault_clone_is_gone(
    bare_remote: Path, tmp_path: Path
) -> None:
    config = ServerConfig()
    async with _running_app(_environ(bare_remote, tmp_path), config) as (app, client):
        app.state.services.vault_root = tmp_path / "no-such-vault"
        response = await client.get(READY_PATH)

    assert response.status_code == 503
    body = response.json()
    assert body["ready"] is False
    assert body["vault"] is False


async def test_readyz_is_503_when_the_database_is_unreachable(
    bare_remote: Path, tmp_path: Path, test_database_url: str
) -> None:
    environ = {**_environ(bare_remote, tmp_path), "DATABASE_URL": test_database_url}
    # DATABASE_URL set turns bearer-token auth on (#34), which requires
    # PUBLIC_URL (#35, ADR-0004) - unrelated to what this test checks, but
    # needed for the app to start at all.
    config = ServerConfig(public_url="https://mm.example.test")
    async with _running_app(environ, config) as (app, client):
        await app.state.services.pool.close()
        response = await client.get(READY_PATH)

    assert response.status_code == 503
    body = response.json()
    assert body["ready"] is False
    assert body["database"] is False


async def test_readyz_is_ready_with_the_postgres_backend_and_no_vault(
    test_database_url: str, app_role: str
) -> None:
    """The `postgres` backend (ADR-0007 §2, WP-18) has no `vault_root` to check at
    all - `vault` collapses to the same database reachability `database` reports.
    """
    environ = {
        "STORAGE_BACKEND": "postgres",
        "DATABASE_URL": test_database_url,
        "DATABASE_APP_ROLE": app_role,
    }
    config = ServerConfig(public_url="https://mm.example.test")
    async with _running_app(environ, config) as (_app, client):
        response = await client.get(READY_PATH)

    assert response.status_code == 200
    assert response.json() == {"ready": True, "vault": True, "database": True, "draining": False}


# --- Origin validation -------------------------------------------------------


async def test_origin_header_absent_is_allowed(bare_remote: Path, tmp_path: Path) -> None:
    config = ServerConfig(allowed_origins=())
    async with _running_app(_environ(bare_remote, tmp_path), config) as (_app, client):
        response = await client.get(HEALTH_PATH)

    assert response.status_code == 200


async def test_origin_header_in_allowlist_is_allowed(bare_remote: Path, tmp_path: Path) -> None:
    config = ServerConfig(allowed_origins=("https://claude.ai",))
    async with _running_app(_environ(bare_remote, tmp_path), config) as (_app, client):
        response = await client.get(HEALTH_PATH, headers={"Origin": "https://claude.ai"})

    assert response.status_code == 200


async def test_origin_header_not_in_allowlist_is_rejected(
    bare_remote: Path, tmp_path: Path
) -> None:
    config = ServerConfig(allowed_origins=("https://claude.ai",))
    async with _running_app(_environ(bare_remote, tmp_path), config) as (_app, client):
        response = await client.get(HEALTH_PATH, headers={"Origin": "https://evil.example"})

    assert response.status_code == 403


# --- /hooks/vault ------------------------------------------------------------


def _github_signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _gitea_signature(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def test_webhook_is_404_without_a_configured_secret(
    bare_remote: Path, tmp_path: Path
) -> None:
    config = ServerConfig(webhook_secret=None)
    async with _running_app(_environ(bare_remote, tmp_path), config) as (_app, client):
        response = await client.post(WEBHOOK_PATH, content=b"{}")

    assert response.status_code == 404


async def test_webhook_is_404_with_the_postgres_backend_even_with_a_secret_configured(
    test_database_url: str, app_role: str
) -> None:
    """The `postgres` backend (ADR-0007 §2, WP-18) has no vault and nothing a webhook
    could ever resync (`Services.trigger_sync` is `None`) - 404 regardless of
    whether `VAULT_WEBHOOK_SECRET` happens to be set, same as the endpoint not
    existing at all.
    """
    environ = {
        "STORAGE_BACKEND": "postgres",
        "DATABASE_URL": test_database_url,
        "DATABASE_APP_ROLE": app_role,
    }
    config = ServerConfig(public_url="https://mm.example.test", webhook_secret=_WEBHOOK_SECRET)
    async with _running_app(environ, config) as (_app, client):
        response = await client.post(WEBHOOK_PATH, content=b"{}")

    assert response.status_code == 404


async def test_webhook_is_401_with_no_signature_header(bare_remote: Path, tmp_path: Path) -> None:
    config = ServerConfig(webhook_secret=_WEBHOOK_SECRET)
    async with _running_app(_environ(bare_remote, tmp_path), config) as (_app, client):
        response = await client.post(WEBHOOK_PATH, content=b"{}")

    assert response.status_code == 401


async def test_webhook_is_401_with_a_wrong_signature(bare_remote: Path, tmp_path: Path) -> None:
    config = ServerConfig(webhook_secret=_WEBHOOK_SECRET)
    async with _running_app(_environ(bare_remote, tmp_path), config) as (_app, client):
        response = await client.post(
            WEBHOOK_PATH,
            content=b"{}",
            headers={"X-Hub-Signature-256": "sha256=" + "0" * 64},
        )

    assert response.status_code == 401


async def test_webhook_is_413_over_the_body_size_cap(bare_remote: Path, tmp_path: Path) -> None:
    config = ServerConfig(webhook_secret=_WEBHOOK_SECRET)
    oversize_body = b"x" * (1024 * 1024 + 1)
    async with _running_app(_environ(bare_remote, tmp_path), config) as (_app, client):
        response = await client.post(WEBHOOK_PATH, content=oversize_body)

    assert response.status_code == 413


async def test_webhook_with_a_valid_github_signature_triggers_a_sync(
    bare_remote: Path, tmp_path: Path
) -> None:
    config = ServerConfig(webhook_secret=_WEBHOOK_SECRET)
    body = b'{"ref": "refs/heads/main"}'
    signature = _github_signature(_WEBHOOK_SECRET, body)

    async with _running_app(_environ(bare_remote, tmp_path), config) as (_app, client):
        human_commit(bare_remote, _NOTE_PATH, _NOTE_CONTENT)

        response = await client.post(
            WEBHOOK_PATH, content=body, headers={"X-Hub-Signature-256": signature}
        )
        assert response.status_code == 202

        synced_path = tmp_path / "vault" / _NOTE_PATH
        assert synced_path.read_bytes() == _NOTE_CONTENT


async def test_webhook_with_a_valid_gitea_signature_triggers_a_sync(
    bare_remote: Path, tmp_path: Path
) -> None:
    config = ServerConfig(webhook_secret=_WEBHOOK_SECRET)
    body = b'{"ref": "refs/heads/main"}'
    signature = _gitea_signature(_WEBHOOK_SECRET, body)

    async with _running_app(_environ(bare_remote, tmp_path), config) as (_app, client):
        human_commit(bare_remote, _NOTE_PATH, _NOTE_CONTENT)

        response = await client.post(
            WEBHOOK_PATH, content=body, headers={"X-Gitea-Signature": signature}
        )
        assert response.status_code == 202

        synced_path = tmp_path / "vault" / _NOTE_PATH
        assert synced_path.read_bytes() == _NOTE_CONTENT


# --- The MCP mount ------------------------------------------------------------


async def test_mcp_path_reaches_the_mounted_mcp_app(bare_remote: Path, tmp_path: Path) -> None:
    """Not a full MCP handshake (`tests/conformance/test_http.py`'s job) - just confirms
    a request at `config.mcp_path` reaches the mounted sub-app instead of 404ing against
    this module's own routes, the same way a stray `GET /mcp` would if `_McpMount` were
    wired up wrong.
    """
    config = ServerConfig()
    async with _running_app(_environ(bare_remote, tmp_path), config) as (_app, client):
        response = await client.post(
            config.mcp_path,
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Accept": "application/json, text/event-stream"},
        )

    # However the MCP transport reacts to an un-initialized session, it must not be the
    # 404 this module's own router would produce for an unmounted path.
    assert response.status_code != 404
