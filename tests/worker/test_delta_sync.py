# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for #223: the worker's own Graph delta-sync job disables deprovisioned
users within one interval (ADR-0006 §6).

Drives `worker._entra_delta_sync_job` directly against a real Postgres pool
and `tests/mock_idp`'s `users/delta` over its in-process `mock_idp_client`
transport (`tests/mock_idp_fixtures.py`) - the same seam `tests/auth/
test_graph.py` already drives `GraphClient` through, and the same
"exercise the job function directly, not through a subprocess" shape
`tests/worker/test_singleton.py`'s own first two test groups use for
`_run_singleton`/`_job_loop`. `build_jobs`'s own wiring of this job
(`graph_client`/`worker_config` parameters, scheduling, the advisory lock)
is not re-tested here - that is generic across every `Job`, already covered
by `test_singleton.py`.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio
from mock_idp_fixtures import mock_idp_client

from memory_manager.auth.graph import GraphClient, GraphError
from memory_manager.auth.tokens import create_token, list_tokens
from memory_manager.auth.users import get_user, upsert_user
from memory_manager.db.migrate import migrate
from memory_manager.worker import _entra_delta_cursor_link, _entra_delta_sync_job

__all__ = ["mock_idp_client"]

_TID = "77777777-7777-7777-7777-777777777777"
_CLIENT_ID = "88888888-8888-8888-8888-888888888888"
_CLIENT_SECRET = "mock-delta-sync-client-secret"  # noqa: S105 - fake test credential
_AUTHORITY_BASE_URL = "https://mock-idp.test"
_GRAPH_BASE_URL = "https://mock-idp.test/graph/v1.0"
_GRAPH_ROLES = ["User.Read.All"]


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


def _graph_client(http_client: httpx.AsyncClient) -> GraphClient:
    return GraphClient(
        tenant_id=_TID,
        client_id=_CLIENT_ID,
        client_secret=_CLIENT_SECRET,
        authority_base_url=_AUTHORITY_BASE_URL,
        graph_base_url=_GRAPH_BASE_URL,
        http_client=http_client,
    )


async def _register_client(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/_mock/clients",
        json={
            "tid": _TID,
            "client_id": _CLIENT_ID,
            "client_secret": _CLIENT_SECRET,
            "redirect_uris": [],
            "graph_roles": _GRAPH_ROLES,
        },
    )
    assert response.status_code == 201


async def _create_mock_user(client: httpx.AsyncClient, oid: str, **fields: Any) -> None:
    response = await client.post("/_mock/users", json={"tid": _TID, "oid": oid, **fields})
    assert response.status_code == 201


async def _update_mock_user(client: httpx.AsyncClient, oid: str, **fields: Any) -> None:
    response = await client.patch(f"/_mock/users/{_TID}/{oid}", json=fields)
    assert response.status_code == 200


async def _delete_mock_user(client: httpx.AsyncClient, oid: str) -> None:
    response = await client.delete(f"/_mock/users/{_TID}/{oid}")
    assert response.status_code == 204


async def _set_page_size(client: httpx.AsyncClient, page_size: int) -> None:
    response = await client.post(
        "/_mock/graph/delta-page-size", json={"tid": _TID, "page_size": page_size}
    )
    assert response.status_code == 200


async def _inject_fault(client: httpx.AsyncClient, *, status: int, count: int = 1) -> None:
    response = await client.post(
        "/_mock/graph/fault", json={"endpoint": "users.delta", "status": status, "count": count}
    )
    assert response.status_code == 201


async def _audit_detail(pool: asyncpg.Pool, *, actor: str, op: str) -> dict[str, Any]:
    """The `detail` of the one `audit_log` row `actor`/`op` must have - CLAUDE.md
    "audit log for every write", `auth.users.disable_user`/`enable_user`'s own
    `AuditWriter(pool).record(...)` call, same assertion shape `tests/auth/
    test_disable_user.py` already uses directly against `audit_log`."""
    row = await pool.fetchrow(
        "select outcome, detail from audit_log where actor = $1 and op = $2", actor, op
    )
    assert row is not None, f"no audit_log row for actor={actor!r} op={op!r}"
    assert row["outcome"] == "ok"
    detail: dict[str, Any] = json.loads(row["detail"])
    return detail


async def test_disabled_and_deleted_known_users_lose_access(
    pool: asyncpg.Pool, mock_idp_client: httpx.AsyncClient
) -> None:
    await _register_client(mock_idp_client)
    await _create_mock_user(mock_idp_client, "user-disable", account_enabled=True)
    await _create_mock_user(mock_idp_client, "user-delete", account_enabled=True)
    await upsert_user(pool, "user-disable", tid=_TID, display_name="User Disable")
    await upsert_user(pool, "user-delete", tid=_TID, display_name="User Delete")
    _, token_info = await create_token(
        pool,
        "token-user-disable",
        scopes=["memory:read"],
        namespaces=["personal"],
        owner_oid="user-disable",
        roles=["Memory.User"],
    )

    # Both users change in Entra before the worker ever ran a round.
    await _update_mock_user(mock_idp_client, "user-disable", account_enabled=False)
    await _delete_mock_user(mock_idp_client, "user-delete")

    graph = _graph_client(mock_idp_client)
    await _entra_delta_sync_job(pool, graph)

    disabled = await get_user(pool, "user-disable")
    deleted = await get_user(pool, "user-delete")
    assert disabled is not None and disabled.disabled_at is not None
    assert deleted is not None and deleted.disabled_at is not None

    tokens = {info.name: info for info in await list_tokens(pool)}
    assert tokens[token_info.name].revoked_at is not None

    for oid in ("user-disable", "user-delete"):
        detail = await _audit_detail(pool, actor=oid, op="disable_user")
        assert detail["reason"] == "entra delta sync: disabled or removed in Graph"
    disable_detail = await _audit_detail(pool, actor="user-disable", op="disable_user")
    assert disable_detail["static_tokens_revoked"] == 1


async def test_unknown_oid_is_ignored_and_never_inserted(
    pool: asyncpg.Pool, mock_idp_client: httpx.AsyncClient
) -> None:
    await _register_client(mock_idp_client)
    # Known to Graph, never signed in here - #223's own checklist: "unknown
    # oids ignored", the sync never imports the tenant.
    await _create_mock_user(mock_idp_client, "user-unknown", account_enabled=False)

    graph = _graph_client(mock_idp_client)
    await _entra_delta_sync_job(pool, graph)

    assert await get_user(pool, "user-unknown") is None


async def test_re_enabled_known_user_is_enabled_again(
    pool: asyncpg.Pool, mock_idp_client: httpx.AsyncClient
) -> None:
    await _register_client(mock_idp_client)
    await _create_mock_user(mock_idp_client, "user-re-enable", account_enabled=True)
    await upsert_user(pool, "user-re-enable", tid=_TID, display_name="User Re-enable")
    graph = _graph_client(mock_idp_client)

    await _update_mock_user(mock_idp_client, "user-re-enable", account_enabled=False)
    await _entra_delta_sync_job(pool, graph)
    disabled = await get_user(pool, "user-re-enable")
    assert disabled is not None and disabled.disabled_at is not None
    disable_detail = await _audit_detail(pool, actor="user-re-enable", op="disable_user")
    assert disable_detail["reason"] == "entra delta sync: disabled or removed in Graph"

    await _update_mock_user(mock_idp_client, "user-re-enable", account_enabled=True)
    await _entra_delta_sync_job(pool, graph)
    enabled = await get_user(pool, "user-re-enable")
    assert enabled is not None and enabled.disabled_at is None
    enable_detail = await _audit_detail(pool, actor="user-re-enable", op="enable_user")
    assert enable_detail["reason"] == "entra delta sync: re-enabled in Graph"


async def test_pages_across_three_pages_and_applies_every_change(
    pool: asyncpg.Pool, mock_idp_client: httpx.AsyncClient
) -> None:
    await _register_client(mock_idp_client)
    for oid in ("user-page-1", "user-page-2", "user-page-3"):
        await _create_mock_user(mock_idp_client, oid, account_enabled=True)
        await upsert_user(pool, oid, tid=_TID, display_name=oid)
    await _update_mock_user(mock_idp_client, "user-page-2", account_enabled=False)
    await _set_page_size(mock_idp_client, 1)

    graph = _graph_client(mock_idp_client)
    await _entra_delta_sync_job(pool, graph)

    calls = await mock_idp_client.get("/_mock/calls")
    assert calls.json()["users.delta"] == 3

    page_1 = await get_user(pool, "user-page-1")
    page_2 = await get_user(pool, "user-page-2")
    page_3 = await get_user(pool, "user-page-3")
    assert page_1 is not None and page_1.disabled_at is None
    assert page_2 is not None and page_2.disabled_at is not None
    assert page_3 is not None and page_3.disabled_at is None
    assert await _entra_delta_cursor_link(pool) is not None


async def test_injected_503_raises_and_leaves_the_cursor_unchanged(
    pool: asyncpg.Pool, mock_idp_client: httpx.AsyncClient
) -> None:
    await _register_client(mock_idp_client)
    await _create_mock_user(mock_idp_client, "user-stable", account_enabled=True)
    await upsert_user(pool, "user-stable", tid=_TID, display_name="User Stable")
    graph = _graph_client(mock_idp_client)

    # Establish a real cursor first, exactly as a prior successful interval would.
    await _entra_delta_sync_job(pool, graph)
    cursor_before = await _entra_delta_cursor_link(pool)
    assert cursor_before is not None

    await _update_mock_user(mock_idp_client, "user-stable", account_enabled=False)
    # More faults than `GraphClient`'s own retry budget (3 attempts).
    await _inject_fault(mock_idp_client, status=503, count=5)

    with pytest.raises(GraphError):
        await _entra_delta_sync_job(pool, graph)

    assert await _entra_delta_cursor_link(pool) == cursor_before
    # The failed round never applied its change either.
    stable = await get_user(pool, "user-stable")
    assert stable is not None and stable.disabled_at is None


async def test_expired_delta_link_triggers_a_full_resync(
    pool: asyncpg.Pool, mock_idp_client: httpx.AsyncClient
) -> None:
    await _register_client(mock_idp_client)
    await _create_mock_user(mock_idp_client, "user-after-reset", account_enabled=True)
    await upsert_user(pool, "user-after-reset", tid=_TID, display_name="User After Reset")
    graph = _graph_client(mock_idp_client)

    # A first round establishes a real, stored delta link.
    await _entra_delta_sync_job(pool, graph)
    cursor_before = await _entra_delta_cursor_link(pool)
    assert cursor_before is not None

    # Something changes, then the *next* round's own first request (against the
    # stored delta link) is answered with `410` - Entra's "Synchronization reset".
    await _update_mock_user(mock_idp_client, "user-after-reset", account_enabled=False)
    await _inject_fault(mock_idp_client, status=410, count=1)

    await _entra_delta_sync_job(pool, graph)

    # The retried full sync still saw (and applied) the change.
    after_reset = await get_user(pool, "user-after-reset")
    assert after_reset is not None and after_reset.disabled_at is not None
    cursor_after = await _entra_delta_cursor_link(pool)
    assert cursor_after is not None
    assert cursor_after != cursor_before
