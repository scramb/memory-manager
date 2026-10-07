# SPDX-License-Identifier: AGPL-3.0-only
"""Two `memory-manager serve --http` replicas on one Postgres (#106, ADR-0009).

Everything here runs the `"postgres"` backend with a disposable, non-superuser
owner and a granted, `NOLOGIN` app role (`_replica_db`, the same shape as
`tests/db/test_request_path.py:55-110`'s `request_path_db`, not the plain
`admin_database_url` connection `MM_TEST_DATABASE_URL` names locally: that
connection is a superuser (`mm`) there, and a superuser's `SET ROLE` would
succeed regardless of whether the request-path role switch ADR-0008's
addendum (#100/#116) relies on is wired correctly at all - the one thing this
module's "enforced namespaces" scenario has to actually exercise).

Three scenarios, each its own test function so each gets exactly the server
configuration it needs, sharing a process pair across sub-scenarios wherever
that configuration allows it (generous, default rate limits serve both
"consistency/namespaces" and "kill" below; "shared rate limit" needs its own,
deliberately tight one):

- `test_two_replicas_serve_one_dataset_consistently_and_survive_a_kill`:
  consistency + enforced namespaces (ADR-0008), then killing one replica
  mid-request-loop while the other keeps answering (ADR-0009 §5's "any
  replica can serve any request", not graceful shutdown - that is
  `tests/test_shutdown.py`'s own scenario).
- `test_rate_limit_is_shared_across_replicas`: one bearer token's requests
  count against the same Postgres-backed window regardless of which replica
  answers them (`auth.shared_state.PostgresSharedState`, ADR-0009 §2). Only
  read-only tools (`memory_index`/`memory_read`/`memory_search`) are ever
  called here - #122 (the MCP and write limiters still collide in the same
  key space) would otherwise make a write call's accounting unpredictable.
- `test_cleanup_sweep_is_a_singleton_across_replicas`: the periodic cleanup
  sweep's advisory lock (`http._run_cleanup_iteration`, #106) - exercised
  directly against `_replica_db.pool`, not through a subprocess pair: the
  sweep interval is a fixed hour (deliberately not configurable, #106's own
  scope), far too long to wait out in a test.

Every request goes through raw `httpx`/JSON-RPC `tools/call`, the pattern
`tests/test_stateless_transport.py` uses (no SDK `mcp.Client` session to
manage, matching the stateless transport itself) - `_call_tool` is this
module's version of that pattern's own `_tools_call_body` helper, extended to
report a 429's status code too (`mcp.Client` has no seam for that - a 429
never reaches the JSON-RPC layer at all, `http.py`'s own `_LimitsMiddleware`
answers it before routing).
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import httpx
import pytest_asyncio
from http_fixtures import Server, run_http_server

from memory_manager.auth.shared_state import PostgresSharedState
from memory_manager.auth.tokens import ALL_NAMESPACES, MEMORY_ROLES, create_token
from memory_manager.db import rls
from memory_manager.db.migrate import migrate
from memory_manager.http import _CLEANUP_LOCK_KEY, _run_cleanup_iteration
from memory_manager.mcp.authz import READ_SCOPE, WRITE_SCOPE

__all__: list[str] = []

_PUBLIC_URL = "https://mm-e2e.example.test"
_OWNER_ROLE = MEMORY_ROLES[0]  # "Memory.User"
_OID_A = "oid-e2e-replica-a"
_OID_B = "oid-e2e-replica-b"

_MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


@dataclass(frozen=True)
class _ReplicaDb:
    owner_url: str
    app_role: str
    pool: asyncpg.Pool


@pytest_asyncio.fixture
async def _replica_db(admin_database_url: str) -> AsyncIterator[_ReplicaDb]:
    """A non-superuser owner with its own database and a granted app role -
    `tests/db/test_request_path.py:55-110`'s `request_path_db`, copied rather than
    imported (that module's own fixture is not exported for reuse, and this one's
    teardown order is identical for the same reason given there).
    """
    db_name = f"mm_test_e2e_{secrets.token_hex(8)}"
    owner_role = f"mm_test_owner_{secrets.token_hex(8)}"
    owner_password = secrets.token_urlsafe(16)
    app_role = f"mm_test_app_{secrets.token_hex(8)}"

    admin_conn = await asyncpg.connect(admin_database_url)
    owner_conn: asyncpg.Connection | None = None
    pool: asyncpg.Pool | None = None
    try:
        await admin_conn.execute(
            f"create role \"{owner_role}\" login password '{owner_password}' nosuperuser"
        )
        await admin_conn.execute(f'create database "{db_name}" owner "{owner_role}"')
        await admin_conn.execute(f'create role "{app_role}" nologin nosuperuser nobypassrls')
        await admin_conn.execute(f'grant "{app_role}" to "{owner_role}"')

        parsed = urlsplit(admin_database_url)
        base, _, _ = admin_database_url.rpartition("/")
        bootstrap_conn = await asyncpg.connect(f"{base}/{db_name}")
        try:
            await bootstrap_conn.execute("create extension if not exists vector")
        finally:
            await bootstrap_conn.close()

        owner_url = urlunsplit(
            (
                parsed.scheme,
                f"{owner_role}:{owner_password}@{parsed.hostname}:{parsed.port}",
                f"/{db_name}",
                "",
                "",
            )
        )
        owner_conn = await asyncpg.connect(owner_url)
        await migrate(owner_conn)
        await rls.grant_app_role(owner_conn, app_role)

        pool = await asyncpg.create_pool(owner_url)
        yield _ReplicaDb(owner_url=owner_url, app_role=app_role, pool=pool)
    finally:
        if pool is not None:
            await pool.close()
        if owner_conn is not None:
            await owner_conn.close()
        await admin_conn.execute(
            "select pg_terminate_backend(pid) from pg_stat_activity "
            "where datname = $1 and pid <> pg_backend_pid()",
            db_name,
        )
        await admin_conn.execute(f'drop database if exists "{db_name}"')
        await admin_conn.execute(f'drop role if exists "{app_role}"')
        await admin_conn.execute(f'drop role if exists "{owner_role}"')
        await admin_conn.close()


#: Generous enough that neither limiter this module's non-rate-limit scenario
#: exercises (`RATE_LIMIT_MCP_*`/`RATE_LIMIT_WRITE_*`) ever trips on the handful
#: of calls that scenario makes - deliberately set here, on both, rather than
#: left at `ServerConfig`'s own defaults: #122 (the MCP and write limiters still
#: share one key space) means a run of reads *and* writes against the same
#: token - exactly what "consistency + kill" below does - would otherwise be
#: able to trip the tighter default write limit well before this module's own
#: `limit` requests are anywhere close to it, for a reason that has nothing to
#: do with what that scenario tests. The rate-limit scenario below overrides
#: `RATE_LIMIT_MCP_*` back down to its own deliberately tight pair; it never
#: calls a write tool, so `RATE_LIMIT_WRITE_*` staying generous there too is
#: harmless.
_GENEROUS_RATE_LIMIT = "1000"


def _replica_env(db: _ReplicaDb, **overrides: str) -> dict[str, str]:
    """The `"postgres"`-backend env every replica in this module starts with
    (`STORAGE_BACKEND`/`DATABASE_URL`/`DATABASE_APP_ROLE`/`PUBLIC_URL`, the shape
    `tests/conformance/test_http.py:256-266`'s `http_env` builds), generous
    rate limits by default (`_GENEROUS_RATE_LIMIT`'s own docstring), plus
    whatever `overrides` a scenario needs on top instead (the rate-limit
    scenario's own, deliberately tight `RATE_LIMIT_MCP_*` pair).
    """
    env = {
        "STORAGE_BACKEND": "postgres",
        "DATABASE_URL": db.owner_url,
        "DATABASE_APP_ROLE": db.app_role,
        "PUBLIC_URL": _PUBLIC_URL,
        "RATE_LIMIT_MCP_PER_MINUTE": _GENEROUS_RATE_LIMIT,
        "RATE_LIMIT_MCP_BURST": _GENEROUS_RATE_LIMIT,
        "RATE_LIMIT_WRITE_PER_MINUTE": _GENEROUS_RATE_LIMIT,
        "RATE_LIMIT_WRITE_BURST": _GENEROUS_RATE_LIMIT,
    }
    env.update(overrides)
    return env


async def _create_user_token(pool: asyncpg.Pool, name: str, *, oid: str) -> str:
    """A bearer token for `oid` (`Memory.User`, every namespace it may otherwise
    reach) - the pattern `tests/conformance/test_http.py:311-318`'s `http_headers`
    uses, called directly here instead of through a fixture since this module
    needs two distinct principals, not one.
    """
    plaintext, _info = await create_token(
        pool,
        name,
        scopes=[READ_SCOPE, WRITE_SCOPE],
        namespaces=[ALL_NAMESPACES],
        owner_oid=oid,
        roles=[_OWNER_ROLE],
    )
    return plaintext


def _note_content(unique_term: str) -> str:
    """A minimal, valid note body (`tests/mcp/test_write_tools.py`'s own
    `_content` helper's shape) mentioning `unique_term` exactly once, so a
    search for it is a positive control with no risk of matching anything
    else `_replica_db`'s fresh database could ever hold."""
    return (
        "---\n"
        "title: Replica fixture note\n"
        f"description: Mentions {unique_term} exactly once.\n"
        "type: fact\n"
        "---\n"
        f"The unique term is {unique_term}.\n"
    )


@dataclass(frozen=True)
class _ToolCallOutcome:
    """One `tools/call` response: either the JSON-RPC `result` (always at HTTP
    200 - this server never reports a tool failure as a transport error, only
    as `isError: true` inside `result`), or the raw status code for anything
    that is not 200 (429, the one other status this module's scenarios ever
    see, is a plain-text response - `http.py`'s `_send_rate_limited` - with no
    JSON-RPC envelope at all)."""

    status_code: int
    result: dict[str, Any] | None


async def _call_tool(
    client: httpx.AsyncClient, server: Server, token: str, tool: str, arguments: dict[str, object]
) -> _ToolCallOutcome:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    headers = {**_MCP_HEADERS, "Authorization": f"Bearer {token}"}
    response = await client.post(server.mcp_url, json=body, headers=headers)
    if response.status_code != 200:
        return _ToolCallOutcome(status_code=response.status_code, result=None)
    payload = response.json()
    assert "error" not in payload, payload
    result = payload["result"]
    assert isinstance(result, dict)
    return _ToolCallOutcome(status_code=200, result=result)


async def test_two_replicas_serve_one_dataset_consistently_and_survive_a_kill(
    _replica_db: _ReplicaDb,
) -> None:
    token_a = await _create_user_token(_replica_db.pool, "replica-a", oid=_OID_A)
    token_b = await _create_user_token(_replica_db.pool, "replica-b", oid=_OID_B)
    unique_term = f"zyxreplica{secrets.token_hex(4)}"
    env = _replica_env(_replica_db)

    async with (
        run_http_server(env) as server1,
        run_http_server(env) as server2,
        httpx.AsyncClient(timeout=10.0) as client,
    ):
        # --- (a) consistency + enforced namespaces ------------------------------

        written = await _call_tool(
            client,
            server1,
            token_a,
            "memory_write",
            {
                "path": "me/fact/sync-check.md",
                "content": _note_content(unique_term),
                "if_version": "new",
            },
        )
        assert written.result is not None and written.result["isError"] is False
        written_path = written.result["structuredContent"]["path"]
        assert written_path == "me/fact/sync-check.md"

        read_back = await _call_tool(
            client, server2, token_a, "memory_read", {"items": [written_path]}
        )
        assert read_back.result is not None and read_back.result["isError"] is False
        read_items = read_back.result["structuredContent"]["result"]
        assert unique_term in read_items[0]["content"]

        own_search = await _call_tool(
            client, server2, token_a, "memory_search", {"query": unique_term}
        )
        assert own_search.result is not None and own_search.result["isError"] is False
        own_hits = own_search.result["structuredContent"]["results"]
        assert any(hit["path"] == written_path for hit in own_hits)

        foreign_search = await _call_tool(
            client, server2, token_b, "memory_search", {"query": unique_term}
        )
        assert foreign_search.result is not None and foreign_search.result["isError"] is False
        assert foreign_search.result["structuredContent"]["results"] == []

        owner_conn = await asyncpg.connect(_replica_db.owner_url)
        try:
            a_alias = await owner_conn.fetchval(
                "select alias from namespaces where kind = 'user' and external_key = $1", _OID_A
            )
        finally:
            await owner_conn.close()
        assert a_alias is not None and a_alias.startswith("u-")

        rejected = await _call_tool(
            client,
            server2,
            token_b,
            "memory_search",
            {"query": unique_term, "namespaces": [a_alias]},
        )
        assert rejected.result is not None and rejected.result["isError"] is True

        # --- (c) kill one replica mid-loop, the other keeps answering -----------

        for i in range(10):
            target = server1 if i % 2 == 0 else server2
            outcome = await _call_tool(client, target, token_a, "memory_index", {})
            assert outcome.result is not None and outcome.result["isError"] is False

        server1.process.kill()

        after_kill_write = await _call_tool(
            client,
            server2,
            token_a,
            "memory_write",
            {
                "path": "me/fact/after-kill.md",
                "content": _note_content(f"{unique_term}-after-kill"),
                "if_version": "new",
            },
        )
        assert after_kill_write.result is not None and after_kill_write.result["isError"] is False
        after_kill_path = after_kill_write.result["structuredContent"]["path"]

        after_kill_read = await _call_tool(
            client, server2, token_a, "memory_read", {"items": [after_kill_path]}
        )
        assert after_kill_read.result is not None and after_kill_read.result["isError"] is False

        await server1.process.wait()
        assert server1.process.returncode == -9


async def test_rate_limit_is_shared_across_replicas(_replica_db: _ReplicaDb) -> None:
    """One token's calls count against the *same* `RATE_LIMIT_MCP_*` window
    (`PostgresSharedState`, ADR-0009 §2) regardless of which of the two replicas
    answers them - not two independent, per-process counters that happen to
    produce a 429 each on their own. `RATE_LIMIT_MCP_BURST == RATE_LIMIT_MCP_PER_MINUTE
    == limit` makes `window_seconds` (`burst * 60 / per_minute`,
    `auth.ratelimit.RateLimiter`'s own formula) exactly 60 seconds, comfortably
    longer than this test takes to run.

    The assertion that actually distinguishes a shared counter from two
    independent ones: `total_requests` is `4 * limit`, so each replica gets `2 *
    limit` requests of its own - enough that an *independent* per-process limiter
    on each would let `limit` of its own through, `2 * limit` successes combined.
    A *shared* counter must cap the combined total at exactly `limit`, no matter
    how the requests are split across the two replicas - asserting "both
    replicas saw a 429" alone cannot tell the two apart (either arrangement
    produces that), so this checks the combined success count instead, with
    `limit` chosen (and the first `limit` requests alternating) so each replica
    still gets at least one of those successes - proof this is actually a
    cross-replica property, not one replica racing through the whole budget
    before the other is ever asked.

    Read-only tool only (`memory_index`): #122 (the MCP and write limiters still
    share one key space) would otherwise make a write call's accounting here
    unpredictable.
    """
    limit = 5
    total_requests = 4 * limit
    token = await _create_user_token(_replica_db.pool, "rate-limit", oid="oid-e2e-ratelimit")
    env = _replica_env(
        _replica_db, RATE_LIMIT_MCP_BURST=str(limit), RATE_LIMIT_MCP_PER_MINUTE=str(limit)
    )

    async with (
        run_http_server(env) as server1,
        run_http_server(env) as server2,
        httpx.AsyncClient(timeout=10.0) as client,
    ):
        servers = (server1, server2)
        successes_by_server = {server1.base_url: 0, server2.base_url: 0}
        for i in range(total_requests):
            target = servers[i % 2]
            outcome = await _call_tool(client, target, token, "memory_index", {})
            if outcome.status_code == 429:
                continue
            assert outcome.result is not None and outcome.result["isError"] is False
            successes_by_server[target.base_url] += 1

        total_successes = sum(successes_by_server.values())
        assert total_successes == limit, successes_by_server
        assert all(count > 0 for count in successes_by_server.values()), successes_by_server


async def test_cleanup_sweep_is_a_singleton_across_replicas(_replica_db: _ReplicaDb) -> None:
    """`http._run_cleanup_iteration`'s advisory lock (#106, ADR-0009 §4: "singleton
    jobs take a Postgres advisory lock"): while a foreign connection holds
    `_CLEANUP_LOCK_KEY`, an iteration skips its sweep entirely and an expired
    `rate_limits` row survives; once that connection releases the lock, the next
    iteration removes it.

    Exercised directly against `_replica_db.pool`, not through a subprocess pair -
    the real interval (`_CLEANUP_INTERVAL_SECONDS`, a fixed hour, deliberately not
    configurable - out of scope for #106) is far too long to wait out here, and
    the lock itself is what this test is about, not the loop that schedules it.
    """
    state = PostgresSharedState(_replica_db.pool)
    await _replica_db.pool.execute(
        "insert into rate_limits (key, window_start, count) "
        "values ('e2e-stale-singleton', now() - interval '2 hours', 1)"
    )

    foreign_conn = await asyncpg.connect(_replica_db.owner_url)
    foreign_tx = foreign_conn.transaction()
    await foreign_tx.start()
    try:
        await foreign_conn.fetchval("select pg_advisory_xact_lock($1)", _CLEANUP_LOCK_KEY)

        await _run_cleanup_iteration(
            _replica_db.pool,
            oauth_provider=None,
            shared_state_backend=state,
            rate_limit_window_floor_seconds=0.0,
        )
        still_there = await _replica_db.pool.fetchval(
            "select count(*) from rate_limits where key = 'e2e-stale-singleton'"
        )
        assert still_there == 1
    finally:
        await foreign_tx.rollback()
        await foreign_conn.close()

    await _run_cleanup_iteration(
        _replica_db.pool,
        oauth_provider=None,
        shared_state_backend=state,
        rate_limit_window_floor_seconds=0.0,
    )
    gone = await _replica_db.pool.fetchval(
        "select count(*) from rate_limits where key = 'e2e-stale-singleton'"
    )
    assert gone == 0
