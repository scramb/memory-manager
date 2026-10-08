# SPDX-License-Identifier: AGPL-3.0-only
"""`memory_promote` goes through the same governance checks every other write
tool already does: the operator blocklist (#244), the per-scope write-rate
quota (`quotas.QuotaChecker`, #242) and the Postgres-only storage quota
(`quotas.StorageQuotaChecker`, #243).

`tests/storage/contract.py`'s own `PromoteContract.test_blocklist_hit_is_
rejected_without_writing` already covers the blocklist check at the backend
layer (both backends); the case here is the same check surfaced through the
real MCP tool, the shape `tests/mcp/test_promote_tool.py`'s own module
docstring draws for "wiring" tests. The two quota checkers are never wired by
`services`/`services_with_postgres_backend` themselves (`build_server`'s
`quota_checker`/`storage_quota_checker` default to `None`, same "off unless
configured" default `http.py` gives them) - every test below builds its own
checker and passes it to `build_server` directly, mirroring `tests/quotas/
test_rate_quotas.py`/`test_storage_quotas.py`'s own checker construction
rather than going through a real `DATABASE_URL`-configured `ServerConfig`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import asyncpg
import pytest
from mcp import Client
from mcp.server.auth.provider import AccessToken

from memory_manager.app import Services
from memory_manager.auth.shared_state import InMemorySharedState
from memory_manager.db import rls
from memory_manager.mcp.server import build_server
from memory_manager.quotas import QuotaChecker, StorageQuotaChecker
from memory_manager.storage.postgres import PostgresBackend

pytestmark = pytest.mark.asyncio

_TARGET_NAMESPACE = "team"


def _content(title: str, body: str = "Worth sharing.\n") -> str:
    return f"---\ntitle: {title}\ndescription: {title} description.\ntype: fact\n---\n{body}"


async def _write(client: Client, path: str, *, body: str = "Worth sharing.\n") -> dict[str, Any]:
    result = await client.call_tool(
        "memory_write",
        {"path": path, "content": _content(title=path, body=body), "if_version": "new"},
    )
    assert result.is_error is False, result.content
    return cast(dict[str, Any], result.structured_content)


def _message(result: Any) -> str:
    """The plain-text message of an `is_error` `CallToolResult` - a bare
    `ToolError` (quota rejections) never carries structured content, only the
    `str(exc)` the SDK's own `_handle_call_tool` wraps into one `TextContent`
    block (`mcp.server.mcpserver.server`)."""
    block = result.content[0]
    assert isinstance(block.text, str)
    return block.text


# === Blocklist (#244) ================================================================


class TestBlocklist:
    async def test_blocklisted_content_is_rejected_without_writing(
        self,
        services: Services,
        vault_root: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        source_path = "personal/fact/promote-gov-blocklist.md"
        target_path = f"{_TARGET_NAMESPACE}/fact/promote-gov-blocklist.md"
        archive_path = f"_archive/{source_path}"

        async with Client(build_server(services)) as client:
            source = await _write(client, source_path, body="the plan is topsecret for now\n")

            blocklist_file = tmp_path / "blocklist.toml"
            blocklist_file.write_text(
                '[[category]]\nname = "example-confidential"\nkeywords = ["topsecret"]\n',
                encoding="utf-8",
            )
            monkeypatch.setenv("BLOCKLIST_FILE", str(blocklist_file))

            result = await client.call_tool(
                "memory_promote",
                {
                    "path": source_path,
                    "target_namespace": _TARGET_NAMESPACE,
                    "if_version": source["version"],
                },
            )

        assert result.is_error is True
        error = cast(dict[str, Any], result.structured_content)
        assert error["error"] == "BlocklistRejected"
        assert error["category"] == "example-confidential"

        # Nothing was overwritten: the original is still live, unarchived,
        # and no copy landed in the target namespace (CLAUDE.md: never
        # overwrite silently).
        assert (vault_root / source_path).exists()
        assert not (vault_root / archive_path).exists()
        assert not (vault_root / target_path).exists()


# === Write-rate quota (#242) ==========================================================


class TestRateQuota:
    async def test_second_promote_into_the_same_target_namespace_is_quota_blocked(
        self, services: Services, vault_root: Path
    ) -> None:
        """`namespace_per_minute=1`, keyed by the *target* namespace - the first
        promote (into an unused target path) goes through, a second one (into a
        different, also-unused target path) is blocked before it ever reaches
        `services.storage`, even though nothing about its own target path would
        otherwise have rejected it.
        """
        source_a = "personal/fact/promote-gov-rate-a.md"
        source_b = "personal/fact/promote-gov-rate-b.md"
        target_b = f"{_TARGET_NAMESPACE}/fact/promote-gov-rate-b.md"

        # Both source notes are written through a plain server first - the
        # quota under test is scoped to the *target* namespace a promote
        # writes into ("team"), not "personal", which `memory_write` would
        # otherwise also spend this same checker's one-per-minute budget on.
        async with Client(build_server(services)) as plain_client:
            written_a = await _write(plain_client, source_a)
            written_b = await _write(plain_client, source_b)

        checker = QuotaChecker(state=InMemorySharedState(), namespace_per_minute=1)
        server = build_server(services, quota_checker=checker)
        async with Client(server) as client:
            first = await client.call_tool(
                "memory_promote",
                {
                    "path": source_a,
                    "target_namespace": _TARGET_NAMESPACE,
                    "if_version": written_a["version"],
                },
            )
            assert first.is_error is False, first.content

            second = await client.call_tool(
                "memory_promote",
                {
                    "path": source_b,
                    "target_namespace": _TARGET_NAMESPACE,
                    "if_version": written_b["version"],
                },
            )

        assert second.is_error is True
        assert "write quota exceeded" in _message(second)
        assert "scope='namespace'" in _message(second)
        assert not (vault_root / target_b).exists()

    async def test_a_generous_quota_does_not_block_promote(
        self, services: Services, vault_root: Path
    ) -> None:
        checker = QuotaChecker(state=InMemorySharedState(), namespace_per_minute=10)
        server = build_server(services, quota_checker=checker)
        source_path = "personal/fact/promote-gov-rate-ok.md"
        target_path = f"{_TARGET_NAMESPACE}/fact/promote-gov-rate-ok.md"

        async with Client(server) as client:
            written = await _write(client, source_path)
            result = await client.call_tool(
                "memory_promote",
                {
                    "path": source_path,
                    "target_namespace": _TARGET_NAMESPACE,
                    "if_version": written["version"],
                },
            )

        assert result.is_error is False, result.content
        assert (vault_root / target_path).exists()


# === Storage quota (#243, Postgres backend only) ======================================

_OID = "oid-promote-governance"
_GROUP_KEY = "grp-promote-governance"
_GROUP_ALIAS = "team-promote-governance"
_MEMORY_USER = "Memory.User"


async def _seed_group(database_url: str) -> None:
    """One group namespace this module's postgres cases can write to - trimmed down
    from `tests/mcp/test_promote_tool.py`'s own `_seed_shared_namespaces` to just
    what the storage-quota case needs.
    """
    conn = await asyncpg.connect(database_url)
    try:
        await conn.execute(
            "insert into users (oid, tid, display_name) values ($1, 'tenant-promote-gov', $1)",
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
    finally:
        await conn.close()


def _set_principal(monkeypatch: pytest.MonkeyPatch) -> None:
    token = AccessToken(
        token="mm_x",  # noqa: S106 - a fake test token, not a credential
        client_id="static:test",
        scopes=[],
        claims={"oid": _OID, "roles": [_MEMORY_USER], "groups": [_GROUP_KEY]},
    )
    monkeypatch.setattr(rls, "get_access_token", lambda: token)


async def _row_exists(pool: asyncpg.Pool, path: str) -> bool:
    row = await pool.fetchval("select 1 from vault_notes where path = $1", path)
    return bool(row)


def _pool(services: Services) -> asyncpg.Pool:
    assert services.pool is not None
    return services.pool


class TestStorageQuota:
    async def test_promote_blocked_by_the_target_namespaces_byte_quota(
        self,
        services_with_postgres_backend: Services,
        test_database_url: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await _seed_group(test_database_url)
        _set_principal(monkeypatch)
        storage = cast(PostgresBackend, services_with_postgres_backend.storage)
        storage_quota_checker = StorageQuotaChecker(storage=storage, max_bytes_shared=1)
        server = build_server(
            services_with_postgres_backend, storage_quota_checker=storage_quota_checker
        )

        source_path = "me/fact/promote-gov-storage-quota.md"
        target_path = f"{_GROUP_ALIAS}/fact/promote-gov-storage-quota.md"

        async with Client(server) as client:
            source = await _write(client, source_path)
            result = await client.call_tool(
                "memory_promote",
                {
                    "path": source_path,
                    "target_namespace": _GROUP_ALIAS,
                    "if_version": source["version"],
                },
            )

        assert result.is_error is True
        assert "shared namespace storage quota exceeded" in _message(result)
        assert "bytes" in _message(result)
        assert not await _row_exists(_pool(services_with_postgres_backend), target_path)

    async def test_a_generous_storage_quota_does_not_block_promote(
        self,
        services_with_postgres_backend: Services,
        test_database_url: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await _seed_group(test_database_url)
        _set_principal(monkeypatch)
        storage = cast(PostgresBackend, services_with_postgres_backend.storage)
        storage_quota_checker = StorageQuotaChecker(
            storage=storage, max_notes_shared=10, max_bytes_shared=10_000
        )
        server = build_server(
            services_with_postgres_backend, storage_quota_checker=storage_quota_checker
        )

        source_path = "me/fact/promote-gov-storage-quota-ok.md"
        target_path = f"{_GROUP_ALIAS}/fact/promote-gov-storage-quota-ok.md"

        async with Client(server) as client:
            source = await _write(client, source_path)
            result = await client.call_tool(
                "memory_promote",
                {
                    "path": source_path,
                    "target_namespace": _GROUP_ALIAS,
                    "if_version": source["version"],
                },
            )

        assert result.is_error is False, result.content
        assert await _row_exists(_pool(services_with_postgres_backend), target_path)
