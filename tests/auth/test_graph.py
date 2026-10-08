# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for `memory_manager.auth.graph.GraphClient` (#214, ADR-0006 §4-§5).

Driven against `tests/mock_idp` over its in-process `mock_idp_client` transport
(`tests/mock_idp_fixtures.py`) - the same seam `tests/mock_idp/test_mock_idp.py`
already proved behaves like `docs/research/entra-contract.md`. `mock_idp_client`
is not re-exported by `tests/auth/conftest.py`, so it is imported directly here,
the same way `test_static_tokens.py`/`test_limits_audit.py` already import
`git_fixtures.seed_notes` without going through that conftest either.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from mock_idp_fixtures import mock_idp_client

from memory_manager.auth.graph import GraphClient, GraphError, UserState
from memory_manager.config import ServerConfigError

__all__ = ["mock_idp_client"]

_TID = "33333333-3333-3333-3333-333333333333"
_CLIENT_ID = "44444444-4444-4444-4444-444444444444"
_CLIENT_SECRET = "mock-graph-client-secret"  # noqa: S105 - fake test credential
_AUTHORITY_BASE_URL = "https://mock-idp.test"
_GRAPH_BASE_URL = "https://mock-idp.test/graph/v1.0"
_TOKEN_PATH = f"/{_TID}/oauth2/v2.0/token"


def _graph_client(http_client: httpx.AsyncClient, **overrides: Any) -> GraphClient:
    kwargs: dict[str, Any] = {
        "tenant_id": _TID,
        "client_id": _CLIENT_ID,
        "client_secret": _CLIENT_SECRET,
        "authority_base_url": _AUTHORITY_BASE_URL,
        "graph_base_url": _GRAPH_BASE_URL,
        "http_client": http_client,
    }
    kwargs.update(overrides)
    return GraphClient(**kwargs)


async def _register_client(client: httpx.AsyncClient, *, graph_roles: list[str]) -> None:
    response = await client.post(
        "/_mock/clients",
        json={
            "tid": _TID,
            "client_id": _CLIENT_ID,
            "client_secret": _CLIENT_SECRET,
            "redirect_uris": [],
            "graph_roles": graph_roles,
        },
    )
    assert response.status_code == 201


async def _create_user(client: httpx.AsyncClient, oid: str, **fields: Any) -> None:
    response = await client.post("/_mock/users", json={"tid": _TID, "oid": oid, **fields})
    assert response.status_code == 201


async def _inject_fault(
    client: httpx.AsyncClient,
    *,
    endpoint: str,
    status: int,
    count: int = 1,
    retry_after: int | None = None,
) -> None:
    body: dict[str, Any] = {"endpoint": endpoint, "status": status, "count": count}
    if retry_after is not None:
        body["retry_after"] = retry_after
    response = await client.post("/_mock/graph/fault", json=body)
    assert response.status_code == 201


_GRAPH_ROLES = ["User.Read.All", "GroupMember.Read.All"]


async def test_user_state_reports_enabled(mock_idp_client: httpx.AsyncClient) -> None:
    await _register_client(mock_idp_client, graph_roles=_GRAPH_ROLES)
    await _create_user(mock_idp_client, "user-enabled", account_enabled=True)

    state = await _graph_client(mock_idp_client).user_state("user-enabled")

    assert state is UserState.ENABLED


async def test_user_state_reports_disabled(mock_idp_client: httpx.AsyncClient) -> None:
    await _register_client(mock_idp_client, graph_roles=_GRAPH_ROLES)
    await _create_user(mock_idp_client, "user-disabled", account_enabled=False)

    state = await _graph_client(mock_idp_client).user_state("user-disabled")

    assert state is UserState.DISABLED


async def test_user_state_reports_missing(mock_idp_client: httpx.AsyncClient) -> None:
    await _register_client(mock_idp_client, graph_roles=_GRAPH_ROLES)

    state = await _graph_client(mock_idp_client).user_state("no-such-user")

    assert state is UserState.MISSING


async def test_member_groups_returns_group_ids(mock_idp_client: httpx.AsyncClient) -> None:
    await _register_client(mock_idp_client, graph_roles=_GRAPH_ROLES)
    await _create_user(mock_idp_client, "user-groups", groups=["group-a", "group-b"])

    groups = await _graph_client(mock_idp_client).member_groups("user-groups")

    assert sorted(groups) == ["group-a", "group-b"]


async def test_app_token_is_reused_until_expiry(mock_idp_client: httpx.AsyncClient) -> None:
    await _register_client(mock_idp_client, graph_roles=_GRAPH_ROLES)
    await _create_user(mock_idp_client, "user-reuse", account_enabled=True)

    token_requests: list[str] = []

    async def _count_token_requests(request: httpx.Request) -> None:
        if request.url.path == _TOKEN_PATH:
            token_requests.append(request.url.path)

    mock_idp_client.event_hooks["request"].append(_count_token_requests)
    try:
        client = _graph_client(mock_idp_client)
        first = await client.user_state("user-reuse")
        second = await client.user_state("user-reuse")
    finally:
        mock_idp_client.event_hooks["request"].remove(_count_token_requests)

    assert first is UserState.ENABLED
    assert second is UserState.ENABLED
    # Two Graph calls, but the cached app token (ADR-0009 §3) is reused -
    # exactly one `/token` request, not two.
    assert len(token_requests) == 1


async def test_retries_on_injected_429_then_succeeds(mock_idp_client: httpx.AsyncClient) -> None:
    await _register_client(mock_idp_client, graph_roles=_GRAPH_ROLES)
    await _create_user(mock_idp_client, "user-retry", account_enabled=True)
    await _inject_fault(mock_idp_client, endpoint="users.get", status=429, retry_after=0, count=1)

    state = await _graph_client(mock_idp_client).user_state("user-retry")

    assert state is UserState.ENABLED
    calls = await mock_idp_client.get("/_mock/calls")
    assert calls.json()["users.get"] == 2


async def test_persistent_429_raises_graph_error_after_bounded_retries(
    mock_idp_client: httpx.AsyncClient,
) -> None:
    await _register_client(mock_idp_client, graph_roles=_GRAPH_ROLES)
    await _create_user(mock_idp_client, "user-exhausted", account_enabled=True)
    # More faults queued than the retry budget allows - every attempt sees a 429.
    await _inject_fault(mock_idp_client, endpoint="users.get", status=429, retry_after=0, count=5)

    with pytest.raises(GraphError):
        await _graph_client(mock_idp_client).user_state("user-exhausted")

    calls = await mock_idp_client.get("/_mock/calls")
    assert calls.json()["users.get"] == 3  # the bounded retry budget, not unbounded


async def test_member_groups_raises_graph_error_on_403(mock_idp_client: httpx.AsyncClient) -> None:
    # Registered without GroupMember.Read.All (ADR-0006 addendum 2026-10-08).
    await _register_client(mock_idp_client, graph_roles=["User.Read.All"])
    await _create_user(mock_idp_client, "user-forbidden", groups=["group-a"])

    with pytest.raises(GraphError) as excinfo:
        await _graph_client(mock_idp_client).member_groups("user-forbidden")

    # The message names the failure, never the app token or a response body.
    assert _CLIENT_SECRET not in str(excinfo.value)


async def test_constructor_rejects_non_https_authority_without_opt_in(
    mock_idp_client: httpx.AsyncClient,
) -> None:
    with pytest.raises(ServerConfigError):
        _graph_client(mock_idp_client, authority_base_url="http://insecure.example.test")


async def test_constructor_allows_non_https_authority_with_opt_in(
    mock_idp_client: httpx.AsyncClient,
) -> None:
    client = _graph_client(
        mock_idp_client,
        authority_base_url="http://insecure.example.test",
        allow_insecure_authority=True,
    )
    assert client is not None
