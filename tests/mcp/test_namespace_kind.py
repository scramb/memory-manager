# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the additive `namespace_kind` result field (ADR-0008, #102).

`memory_search`/`memory_index` each get a new, additive `namespace_kind`
field on every result item: 'personal'/'group'/'project'/'org' in
`"postgres"` mode (`namespaces.Resolution.kind_of`, derived from the same
per-call `Resolution` #101 already computes - no new SQL column, no touch
of `search.py`), `None` on the Git backend (`resolved is None`, the same
condition every other `namespaces`-aware rewrite in `mcp/server.py` already
branches on).

The Git-mode case uses the plain `services` fixture (`tests/mcp/conftest.py`)
- no namespace registry exists there at all. The `"postgres"` case builds
its own group/project/org namespaces directly against `test_database_url`
(the same technique `tests/mcp/conftest.py`'s own `_seed_personal_namespace`
and `tests/mcp/test_permission_matrix.py`'s `_seed` use: connect as the
database owner, which carries no RLS on the registry tables at all) for a
principal injected the same way `tests/mcp/conftest.py`'s
`postgres_backend_principal` is - monkeypatching `db.rls.get_access_token`,
since the in-memory `mcp.Client` these tests drive `build_server(services)`
through never runs the real HTTP transport's `AuthContextMiddleware`. The
principal carries `Memory.User` (needed for org read) and `Memory.Curator`
(needed for org write) plus membership in one group and one project, so a
single `memory_write` through the real tool reaches all four namespace
kinds - exercising the whole path (write, index, search, `me`/alias
rewriting) rather than inserting note rows directly, which would bypass
the Postgres backend's own search-index hook.
"""

from __future__ import annotations

from typing import Any, cast

import asyncpg
import pytest
from mcp import Client
from mcp.server.auth.provider import AccessToken

from memory_manager.app import Services
from memory_manager.db import rls
from memory_manager.mcp.server import build_server

pytestmark = pytest.mark.asyncio

_OID = "oid-namespace-kind"
_GROUP_KEY = "grp-namespace-kind"
_GROUP_ALIAS = "team-namespace-kind"
_PROJECT_KEY = "proj-namespace-kind-key"
_PROJECT_ALIAS = "proj-namespace-kind"
_ORG_ALIAS = "org-namespace-kind"

_PERSONAL_PATH = "me/fact/namespace-kind-personal.md"
_GROUP_PATH = f"{_GROUP_ALIAS}/fact/namespace-kind-group.md"
_PROJECT_PATH = f"{_PROJECT_ALIAS}/fact/namespace-kind-project.md"
_ORG_PATH = f"{_ORG_ALIAS}/fact/namespace-kind-org.md"

# path (as addressed/displayed) -> expected `namespace_kind`
_EXPECTED_KIND: dict[str, str] = {
    _PERSONAL_PATH: "personal",
    _GROUP_PATH: "group",
    _PROJECT_PATH: "project",
    _ORG_PATH: "org",
}


async def _seed_shared_namespaces(database_url: str) -> None:
    """The group/project/org namespaces and memberships `_OID` needs to read and
    write all four namespace kinds - connects as the test database's owner
    (`mm`), which carries no RLS on these registry tables at all (same
    reasoning as `tests/mcp/conftest.py`'s `_seed_personal_namespace`). The
    personal namespace itself is left to `mm_ensure_personal_ns()`'s lazy
    creation (ADR-0008 addendum) - never seeded by hand here.
    """
    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute(
            "insert into users (oid, tid, display_name) values ($1, 'tenant-namespace-kind', $1)",
            _OID,
        )

        await conn.execute(
            "insert into namespaces (kind, external_key, alias) values ('group', $1, $2)",
            _GROUP_KEY,
            _GROUP_ALIAS,
        )
        await conn.execute(
            "insert into user_groups (oid, group_id) values ($1, $2)", _OID, _GROUP_KEY
        )

        project_id = await conn.fetchval(
            "insert into namespaces (kind, external_key, alias) values ('project', $1, $2) "
            "returning id",
            _PROJECT_KEY,
            _PROJECT_ALIAS,
        )
        await conn.execute(
            "insert into project_members (namespace_id, principal_kind, principal_id, role) "
            "values ($1, 'user', $2, 'writer')",
            project_id,
            _OID,
        )

        await conn.execute(
            "insert into namespaces (kind, external_key, alias) values ('org', 'org', $1)",
            _ORG_ALIAS,
        )
    finally:
        await conn.close()


def _set_principal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `db.rls.current_principal()` resolve to `_OID`, a member of
    `_GROUP_KEY` holding both `Memory.User` and `Memory.Curator` - the same
    technique `tests/mcp/conftest.py`'s `postgres_backend_principal` uses,
    with this module's own identity instead of that fixture's fixed one.
    """
    token = AccessToken(
        token="mm_x",  # noqa: S106 - a fake test token, not a credential
        client_id="static:test",
        scopes=[],
        claims={"oid": _OID, "roles": ["Memory.User", "Memory.Curator"], "groups": [_GROUP_KEY]},
    )
    monkeypatch.setattr(rls, "get_access_token", lambda: token)


async def _write(client: Client, path: str, *, title: str) -> None:
    content = (
        "---\n"
        f"title: {title}\n"
        f"description: {title} description.\n"
        "type: fact\n"
        "---\n"
        f"Body for {title}.\n"
    )
    result = await client.call_tool(
        "memory_write", {"path": path, "content": content, "if_version": "new"}
    )
    assert result.is_error is False, result.content


# -- Git backend: the field is present and null ---------------------------------


async def test_memory_index_namespace_kind_is_null_on_git_backend(services: Services) -> None:
    async with Client(build_server(services)) as client:
        result = await client.call_tool("memory_index", {})
    entries = cast(list[dict[str, Any]], result.structured_content["result"])
    assert entries
    for entry in entries:
        assert entry["namespace_kind"] is None, entry


async def test_memory_search_namespace_kind_is_null_on_git_backend(services: Services) -> None:
    async with Client(build_server(services)) as client:
        result = await client.call_tool("memory_search", {"query": "favorite"})
    payload = cast(dict[str, Any], result.structured_content)
    assert payload["results"]
    for item in payload["results"]:
        assert item["namespace_kind"] is None, item


# -- postgres backend: personal/group/project/org, each with its own kind -------


async def test_memory_index_namespace_kind_matches_each_namespace(
    services_with_postgres_backend: Services,
    test_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_shared_namespaces(test_database_url)
    _set_principal(monkeypatch)

    async with Client(build_server(services_with_postgres_backend)) as client:
        for path in _EXPECTED_KIND:
            await _write(client, path, title=path)

        result = await client.call_tool("memory_index", {})
    entries = cast(list[dict[str, Any]], result.structured_content["result"])
    by_path = {entry["path"]: entry for entry in entries}

    for path, expected_kind in _EXPECTED_KIND.items():
        assert path in by_path, (path, by_path.keys())
        assert by_path[path]["namespace_kind"] == expected_kind


async def test_memory_search_namespace_kind_matches_each_namespace(
    services_with_postgres_backend: Services,
    test_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_shared_namespaces(test_database_url)
    _set_principal(monkeypatch)

    async with Client(build_server(services_with_postgres_backend)) as client:
        for path in _EXPECTED_KIND:
            await _write(client, path, title=path)

        for path, expected_kind in _EXPECTED_KIND.items():
            result = await client.call_tool("memory_search", {"query": f"Body for {path}"})
            payload = cast(dict[str, Any], result.structured_content)
            matches = [item for item in payload["results"] if item["path"] == path]
            assert matches, (path, payload["results"])
            assert matches[0]["namespace_kind"] == expected_kind


# -- tool descriptions stay within the protocol size target ---------------------


async def test_memory_index_and_memory_search_descriptions_mention_namespace_kind_and_fit(
    services: Services,
) -> None:
    async with Client(build_server(services)) as client:
        listing = await client.list_tools()
    by_name = {tool.name: tool for tool in listing.tools}
    for name in ("memory_index", "memory_search"):
        description = by_name[name].description or ""
        assert "namespace_kind" in description, name
        assert len(description) <= 2048, (name, len(description))
