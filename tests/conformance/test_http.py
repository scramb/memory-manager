# SPDX-License-Identifier: AGPL-3.0-only
"""Streamable HTTP conformance: a real `memory-manager serve --http` subprocess,
exercised against both protocol revisions the server mounts on one endpoint (#33) -
the HTTP counterpart of `tests/conformance/test_stdio.py` (read there first for the
two-revision background: `mcp_types.version`'s `HANDSHAKE_PROTOCOL_VERSIONS` vs.
`MODERN_PROTOCOL_VERSIONS`, and why 2025-11-25/2026-07-28 are the two targets here).

Over HTTP, which era a request lands in is decided by one header
(`mcp.server.streamable_http_manager.StreamableHTTPSessionManager._handle_request`,
confirmed by reading it rather than assumed): no `MCP-Protocol-Version` header, or one
of `HANDSHAKE_PROTOCOL_VERSIONS`, goes through the legacy (session/`initialize`) path;
any other value - including an unsupported one - goes to `handle_modern_request`.
`mcp.Client` drives both eras correctly on its own (`mode="legacy"`/`"auto"`); the raw
`httpx` checks below exist for what `Client` has no seam for: an unregistered method and
an unsupported `MCP-Protocol-Version` header, both only reachable by sending the header
by hand.

`http_env` is parametrised over `STORAGE_BACKEND` (ADR-0007 §2, WP-18), so every
test built on `http_server` runs against both: `"git"` talks to a vault cloned
from a throwaway local bare remote (`tests/git_fixtures.bare_remote`, no
network), seeded with one note, and no `DATABASE_URL` - same as `test_stdio.py`.
`"postgres"` carries no `VAULT_*` variable at all (proving this mode never
clones anything) and seeds its note straight through
`storage.postgres.PostgresBackend.write`; `DATABASE_URL` always turns
bearer-token auth on for `/mcp` in this mode (#34, ADR-0007 §2), so it also
needs `PUBLIC_URL` and the `http_headers` fixture's bearer token - every test
built on `http_server` sends `http_headers` with it, empty for `"git"` without
a database. `MM_ALLOW_UNAUTHENTICATED` is not set: the subprocess binds to
`127.0.0.1`, which `cli._is_loopback` always allows.

`authenticated_http_server` (bottom of the module, #34) is a separate,
non-parametrised fixture: it specifically exercises the `"git"` backend with
`DATABASE_URL` set (bearer-token auth turning on for a backend that is
otherwise unauthenticated), not backend conformance - a fresh
`test_database_url`, so the subprocess's own startup turns bearer-token auth on
for `/mcp` (`http.py`) - a static token is then created directly against that
same database once the subprocess is ready, and a real `mcp.Client` carries it
as `Authorization: Bearer ...` the same way Claude Code or CI would, via
`httpx2.AsyncClient(headers=...)` passed to `streamable_http_client` (the SDK's
own seam for a client-supplied `httpx2.AsyncClient`, since `mcp.Client("http://
...")` itself has no `headers=` parameter) - the same seam `http_headers`
reuses for the parametrised `"postgres"` case.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import httpx2
import pytest
import pytest_asyncio
from git_fixtures import seed_notes
from http_fixtures import Server as _Server
from http_fixtures import run_http_server
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp_types import METHOD_NOT_FOUND, UNSUPPORTED_PROTOCOL_VERSION
from mcp_types.version import LATEST_HANDSHAKE_VERSION, LATEST_MODERN_VERSION

from memory_manager.auth.tokens import ALL_NAMESPACES, MEMORY_ROLES, create_token
from memory_manager.db.migrate import migrate
from memory_manager.mcp.authz import READ_SCOPE, WRITE_SCOPE
from memory_manager.storage.postgres import PostgresBackend
from memory_manager.vault.note import Note, serialize
from memory_manager.vault.ulid import new_ulid

#: The static token's owner principal for the `"postgres"`-parametrised cases
#: of `http_env`/`http_headers` below (ADR-0008 addendum, #115/#116) - this
#: oid's own personal namespace is seeded, aliased `"me"`, matching
#: `_SEEDED_PATH` - `me` rewriting (#101) would show it as `me` regardless of
#: its real alias, but using `"me"` as the stored alias too means this
#: module's single `_SEEDED_PATH` constant works unchanged for both the
#: `"git"` backend (no rewriting at all, a plain literal namespace) and the
#: `"postgres"` one (where it is, coincidentally, already the display form).
_OWNER_OID = "oid-http-conformance"
_OWNER_ROLE = MEMORY_ROLES[0]  # "Memory.User"

__all__: list[str] = []

# Spelled out as literals, checked against the installed SDK's own registry
# below - see `tests/conformance/test_stdio.py`'s identical comment.
_HANDSHAKE_VERSION = "2025-11-25"
_MODERN_VERSION = "2026-07-28"

_EXPECTED_TOOL_NAMES = frozenset(
    {
        "memory_index",
        "memory_read",
        "memory_search",
        "memory_write",
        "memory_edit",
        "memory_supersede",
        "memory_archive",
    }
)

_SEEDED_PATH = "me/fact/favorite-color.md"
_SEEDED_BODY = "Blue.\n"

# DATABASE_URL set (always, for the `"postgres"` backend; on request, for
# `authenticated_http_server`'s `"git"` + DATABASE_URL case) turns bearer-token
# auth on (#34), which requires PUBLIC_URL (#35, ADR-0004) - unrelated to what
# either case actually checks, but needed for the subprocess to start at all.
_PUBLIC_URL = "https://mm.example.test"

_PARAMETRIZE_VERSIONS = pytest.mark.parametrize(
    "version", [_HANDSHAKE_VERSION, _MODERN_VERSION], ids=["2025-11-25", "2026-07-28"]
)


def test_negotiated_versions_match_the_sdks_registry() -> None:
    """The literals this module tests against are exactly the installed SDK's.

    Same guard as `test_stdio.py`'s - kept here too since this module pins
    the same two literals independently (a header value, not a handshake
    parameter).
    """
    assert _HANDSHAKE_VERSION == LATEST_HANDSHAKE_VERSION
    assert _MODERN_VERSION == LATEST_MODERN_VERSION


def _seeded_note(now: datetime, body: str) -> Note:
    return Note(
        id=new_ulid(now),
        title="Favorite color",
        description="The user's favorite color, seeded for HTTP conformance checks.",
        type="fact",
        created=now,
        updated=now,
        body=body,
        tags=("color",),
    )


async def _seed_postgres_note(database_url: str, content: bytes) -> None:
    """Migrate `database_url`, then write `content` at `_SEEDED_PATH` through
    `PostgresBackend` directly - the `"postgres"` backend's counterpart to
    `git_fixtures.seed_notes`'s commit onto the bare remote (see
    `tests/conformance/test_stdio.py`'s identical twin).

    No `app_role` (ADR-0008 addendum, #116): this connects, and writes, as
    the migrating owner - the one identity every content table's
    owner-only policy always lets through regardless of namespace, exactly
    like Git-mode indexing or `reindex --full` would.
    """
    migration_conn = await asyncpg.connect(database_url)
    try:
        await migrate(migration_conn)
    finally:
        await migration_conn.close()

    pool = await asyncpg.create_pool(database_url)
    try:
        await PostgresBackend(pool).write(
            _SEEDED_PATH, content, if_version="new", client="conformance-seed"
        )
    finally:
        await pool.close()


async def _seed_personal_namespace(database_url: str, *, oid: str, alias: str) -> None:
    """Seed `namespaces`/`users` rows so `oid`'s own namespace `alias` is
    readable/writable under RLS (`mm_readable_ns`/`mm_writable_ns`,
    `migrations/0005_rls.sql`) - connects as the owner, which carries no RLS
    on these two membership tables at all.
    """
    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute(
            "insert into users (oid, tid, display_name) values ($1, 'tenant-conformance', $1)",
            oid,
        )
        await conn.execute(
            "insert into namespaces (kind, external_key, alias) values ('user', $1, $2)",
            oid,
            alias,
        )
    finally:
        await conn.close()


async def _create_app_role(admin_database_url: str) -> str:
    """A disposable, non-owner, non-superuser role for the RLS request path
    (ADR-0008 addendum, #116). Roles are cluster-wide - created against
    `admin_database_url`, not the per-test database - and never granted here:
    the subprocess's own `open_services` does that at startup
    (`db.rls.grant_app_role`), once `DATABASE_APP_ROLE` names it.
    """
    role = f"mm_test_app_{secrets.token_hex(8)}"
    conn = await asyncpg.connect(admin_database_url)
    try:
        await conn.execute(f'create role "{role}" nologin nosuperuser nobypassrls')
    finally:
        await conn.close()
    return role


async def _drop_app_role(admin_database_url: str, database_url: str, role: str) -> None:
    """Undo `_create_app_role`, in the order that avoids `DependentObjectsStillExistError`
    (see `tests/test_app.py`'s identical `app_role` fixture for why)."""
    owned_conn: asyncpg.Connection | None
    try:
        owned_conn = await asyncpg.connect(database_url)
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


@pytest_asyncio.fixture(params=["git", "postgres"], ids=["git", "postgres"])
async def http_env(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    bare_remote: Path,
    test_database_url: str,
    admin_database_url: str,
) -> AsyncIterator[dict[str, str]]:
    """An HTTP subprocess's env, backend seeded with one note (ADR-0007 §2, WP-18).

    `"git"`: `VAULT_REMOTE`/`VAULT_DIR`, no `DATABASE_URL` - same reasoning as
    `test_stdio.py`'s `stdio_env`. `"postgres"`: `STORAGE_BACKEND`/`DATABASE_URL`
    only, no `VAULT_*` variable at all (proving this mode never clones
    anything), plus `PUBLIC_URL` - `DATABASE_URL` always turns bearer-token
    auth on for `/mcp` in this mode (#34, ADR-0007 §2), which requires it
    (#35, ADR-0004). `http_headers` below is this fixture's companion: the
    bearer token every test built on `http_server` needs to actually reach
    anything in the `"postgres"` case.

    Request path (ADR-0008 addendum, #116): the `"postgres"` branch also
    creates a disposable app role (`DATABASE_APP_ROLE` - the subprocess's own
    `open_services` grants it at startup) and seeds `_OWNER_OID`'s personal
    namespace, aliased `"me"` (`_SEEDED_PATH`'s own comment explains why this
    alias is itself `"me"`, not just shown as it) - `http_headers` creates a
    token carrying that same oid as its owner principal, so every request
    this module makes against the `"postgres"` case runs under the app role
    and that identity.
    """
    note = _seeded_note(datetime(2025, 6, 1, tzinfo=UTC), _SEEDED_BODY)
    content = serialize(note)

    if request.param == "postgres":
        await _seed_postgres_note(test_database_url, content)
        await _seed_personal_namespace(test_database_url, oid=_OWNER_OID, alias="me")
        role = await _create_app_role(admin_database_url)
        try:
            yield {
                "STORAGE_BACKEND": "postgres",
                "DATABASE_URL": test_database_url,
                "PUBLIC_URL": _PUBLIC_URL,
                "DATABASE_APP_ROLE": role,
            }
        finally:
            await _drop_app_role(admin_database_url, test_database_url, role)
        return

    seed_notes(bare_remote, {_SEEDED_PATH: content})
    yield {"VAULT_REMOTE": str(bare_remote), "VAULT_DIR": str(tmp_path / "vault")}


@pytest_asyncio.fixture
async def http_server(http_env: Mapping[str, str]) -> AsyncIterator[_Server]:
    """A real `memory-manager serve --http` subprocess, up and answering `/healthz`.

    `MM_ALLOW_UNAUTHENTICATED` is deliberately left unset: `HOST` defaults to
    `127.0.0.1`, which `cli._is_loopback` always allows, so the no-auth
    refusal never fires here. The `"git"` case of `http_env` sets no
    `DATABASE_URL`, so `/mcp` itself has no bearer-token auth turned on there
    (#34) - see `authenticated_http_server` below for that case on its own
    terms, and `http_headers` for the `"postgres"` case's bearer token.
    """
    async with run_http_server(http_env) as server:
        yield server


@pytest_asyncio.fixture
async def http_headers(http_env: Mapping[str, str]) -> dict[str, str]:
    """The `Authorization` header every request against `http_server` needs - empty
    for `"git"` without a database, a freshly created bearer token for `"postgres"`
    (`DATABASE_URL` always turns bearer-token auth on there, #34/ADR-0007 §2).
    Used by both `mcp.Client` (via `streamable_http_client(http_client=...)`) and the
    raw `httpx`/`httpx2` checks below - the same seam `authenticated_http_server`
    uses for its own, separately-created token.

    `http_env`'s `"postgres"` branch has already migrated `database_url` by the
    time this runs (through `_seed_postgres_note`), so `static_tokens` already
    exists here too. The token carries `_OWNER_OID`/`_OWNER_ROLE` as its owner
    principal (ADR-0008 addendum, #115) - `http_env`'s own personal-namespace
    row for that oid is what makes the request path's RLS checks (#116) let
    every request in this module's `"postgres"` case through.
    """
    database_url = http_env.get("DATABASE_URL")
    if not database_url:
        return {}
    pool = await asyncpg.create_pool(database_url)
    try:
        plaintext, _info = await create_token(
            pool,
            "conformance",
            scopes=[READ_SCOPE, WRITE_SCOPE],
            namespaces=[ALL_NAMESPACES],
            owner_oid=_OWNER_OID,
            roles=[_OWNER_ROLE],
        )
    finally:
        await pool.close()
    return {"Authorization": f"Bearer {plaintext}"}


# --- Bearer-token auth (#34): a real subprocess with `DATABASE_URL` set -----


@pytest.fixture
def _git_http_env(tmp_path: Path, bare_remote: Path) -> dict[str, str]:
    """`VAULT_REMOTE`/`VAULT_DIR` for a `"git"`-backend-only HTTP subprocess, vault
    seeded with one note - `authenticated_http_server`'s own env, deliberately not
    the parametrised `http_env` above: that fixture's `"postgres"` case would
    otherwise leak `STORAGE_BACKEND=postgres` into this git-plus-`DATABASE_URL`
    scenario and needlessly double every test built on it, which is about `"git"`
    gaining auth when a database is configured (#34), not backend conformance.
    """
    note = _seeded_note(datetime(2025, 6, 1, tzinfo=UTC), _SEEDED_BODY)
    seed_notes(bare_remote, {_SEEDED_PATH: serialize(note)})
    return {"VAULT_REMOTE": str(bare_remote), "VAULT_DIR": str(tmp_path / "vault")}


@dataclass
class _AuthenticatedServer:
    server: _Server
    token: str


@pytest_asyncio.fixture
async def authenticated_http_server(
    _git_http_env: Mapping[str, str], test_database_url: str
) -> AsyncIterator[_AuthenticatedServer]:
    """Same kind of subprocess as `http_server`'s `"git"` case, with `DATABASE_URL`
    set and one token created.

    The subprocess's own startup (`open_services`) migrates `test_database_url`
    before this fixture ever touches it, so by the time `run_http_server`'s
    `wait_until_ready` returns, `static_tokens` already exists - the token below is
    created against that same database, not a separate one.
    """
    full_env = {
        **_git_http_env,
        "DATABASE_URL": test_database_url,
        # DATABASE_URL set above turns bearer-token auth on (#34), which
        # requires PUBLIC_URL (#35, ADR-0004) - the subprocess refuses to
        # start otherwise. Its value is unrelated to `base_url` below: the
        # bearer tokens this fixture creates carry no RFC 8707 resource of
        # their own (`validate_token_resource=False`, `http.py`), so this
        # never has to match the loopback address the test client actually
        # talks to.
        "PUBLIC_URL": _PUBLIC_URL,
    }
    async with run_http_server(full_env) as server:
        pool = await asyncpg.create_pool(test_database_url)
        try:
            plaintext, _info = await create_token(
                pool,
                "conformance",
                scopes=[READ_SCOPE, WRITE_SCOPE],
                namespaces=[ALL_NAMESPACES],
            )
        finally:
            await pool.close()

        yield _AuthenticatedServer(server=server, token=plaintext)


async def test_mcp_without_a_token_is_401_when_database_url_is_set(
    authenticated_http_server: _AuthenticatedServer,
) -> None:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            authenticated_http_server.server.mcp_url,
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Accept": "application/json, text/event-stream"},
        )

    assert response.status_code == 401


async def test_a_bearer_token_authenticates_a_real_client_over_the_wire(
    authenticated_http_server: _AuthenticatedServer,
) -> None:
    http_client = httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {authenticated_http_server.token}"}
    )
    transport = streamable_http_client(
        authenticated_http_server.server.mcp_url, http_client=http_client
    )

    async with Client(transport, mode="legacy") as client:
        listing = await client.list_tools()
        assert {tool.name for tool in listing.tools} == _EXPECTED_TOOL_NAMES

        index_result = await client.call_tool("memory_index", {})
        assert index_result.is_error is False
        assert index_result.structured_content is not None
        paths = {entry["path"] for entry in index_result.structured_content["result"]}
        assert _SEEDED_PATH in paths


# --- mcp.Client-driven checks: handshake, tool/prompt surface, call_tool shapes -----


@pytest.mark.parametrize(
    ("mode", "expected_version"),
    [("legacy", _HANDSHAKE_VERSION), ("auto", _MODERN_VERSION)],
    ids=["2025-11-25", "2026-07-28"],
)
async def test_handshake_and_tool_surface(
    http_server: _Server, http_headers: dict[str, str], mode: str, expected_version: str
) -> None:
    # Same reasoning as `test_stdio.py`'s identically named test for `mode="auto"`
    # landing on the modern version: this server has no handshake-era fallback
    # reason, so a real `server/discover` probe lands on 2026-07-28. Built through
    # `streamable_http_client` rather than a bare URL string so `http_headers`'
    # bearer token (empty for `"git"` without a database) reaches the `"postgres"`
    # case, the same seam `authenticated_http_server`'s own test uses below.
    transport = streamable_http_client(
        http_server.mcp_url, http_client=httpx2.AsyncClient(headers=http_headers)
    )
    async with Client(transport, mode=mode) as client:
        assert client.protocol_version == expected_version
        assert client.instructions
        assert client.server_info is not None
        assert client.server_info.name == "memory-manager"
        capabilities = client.server_capabilities
        assert capabilities.tools is not None
        assert capabilities.prompts is not None

        listing = await client.list_tools()
        names = {tool.name for tool in listing.tools}
        assert names == _EXPECTED_TOOL_NAMES
        for tool in listing.tools:
            assert tool.description
            assert tool.input_schema["type"] == "object"

        prompts = await client.list_prompts()
        assert any(prompt.name == "memory_guide" for prompt in prompts.prompts)
        guide = await client.get_prompt("memory_guide")
        assert guide.messages

        index_result = await client.call_tool("memory_index", {})
        assert index_result.is_error is False
        assert index_result.structured_content is not None
        indexed_paths = {entry["path"] for entry in index_result.structured_content["result"]}
        assert _SEEDED_PATH in indexed_paths

        read_result = await client.call_tool("memory_read", {"items": [_SEEDED_PATH]})
        assert read_result.is_error is False
        assert read_result.structured_content is not None
        read_items = read_result.structured_content["result"]
        assert read_items[0]["path"] == _SEEDED_PATH
        assert _SEEDED_BODY in read_items[0]["content"]

        invalid_args_result = await client.call_tool("memory_write", {})
        assert invalid_args_result.is_error is True

        unknown_tool_result = await client.call_tool("no_such_tool", {})
        assert unknown_tool_result.is_error is True


# --- Raw HTTP checks: protocol-layer errors reachable only by hand-set headers -----


def _modern_meta() -> dict[str, Any]:
    """The `_meta` envelope every 2026-07-28+ request carries - see `test_stdio.py`'s twin."""
    return {
        "io.modelcontextprotocol/protocolVersion": _MODERN_VERSION,
        "io.modelcontextprotocol/clientInfo": {"name": "conformance-test", "version": "0"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }


async def test_unknown_method_is_a_json_rpc_method_not_found_error(
    http_server: _Server, http_headers: dict[str, str]
) -> None:
    method = "not/a/real/method"
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": {"_meta": _modern_meta()}}
    async with httpx.AsyncClient() as client:
        response = await client.post(
            http_server.mcp_url,
            json=body,
            headers={
                **http_headers,
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": _MODERN_VERSION,
                # The envelope ladder's rung 2 (`inbound.classify_inbound_request`):
                # these two routing headers must mirror the body before the ladder
                # even gets to "is this version supported" - method existence
                # itself is checked later, by kernel dispatch.
                "MCP-Method": method,
            },
        )

    assert response.status_code == 404
    payload = response.json()
    assert payload["error"]["code"] == METHOD_NOT_FOUND


async def test_unsupported_protocol_version_header_is_rejected(
    http_server: _Server, http_headers: dict[str, str]
) -> None:
    bogus_version = "1999-01-01"
    method = "server/discover"
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": bogus_version,
                "io.modelcontextprotocol/clientInfo": {"name": "conformance-test", "version": "0"},
                "io.modelcontextprotocol/clientCapabilities": {},
            }
        },
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(
            http_server.mcp_url,
            json=body,
            headers={
                **http_headers,
                "Accept": "application/json, text/event-stream",
                # Not in `HANDSHAKE_PROTOCOL_VERSIONS`, so the session manager
                # routes this to the modern handler, which then rejects the
                # envelope's own unsupported version - the header is what
                # decides the *era*, the body is what gets validated.
                "MCP-Protocol-Version": bogus_version,
                "MCP-Method": method,
            },
        )

    assert response.status_code == 400
    payload = response.json()
    assert payload["error"]["code"] == UNSUPPORTED_PROTOCOL_VERSION
