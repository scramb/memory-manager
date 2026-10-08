# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the synthetic-vault loader (#108, #124): the pure row-building
half against a generated vault on disk, the async half against a real
Postgres (`test_database_url`, see `tests/conftest.py`)."""

from __future__ import annotations

import json
import random
import secrets
from pathlib import Path

import asyncpg

from loadtest.generate import generate
from loadtest.load import build_rows, create_principal_tokens, load_vault, populate_registry
from memory_manager.auth.tokens import verify
from memory_manager.db.rls import grant_app_role, request_identity
from memory_manager.mcp.authz import READ_SCOPE, WRITE_SCOPE
from memory_manager.storage import rules
from memory_manager.storage.postgres import PostgresBackend
from memory_manager.vault.note import parse, version

_NOTES = 50
_USERS = 5
_GROUPS = 2
_SEED = 1


def _generate_vault(out: Path) -> Path:
    generate(notes=_NOTES, users=_USERS, groups=_GROUPS, seed=_SEED, out=out)
    return out / "vault"


async def test_load_vault_stores_the_canonical_bytes_and_version(
    tmp_path: Path, test_database_url: str
) -> None:
    vault_dir = _generate_vault(tmp_path / "vault-out")
    rows = build_rows(vault_dir)
    assert len(rows) == _NOTES

    await load_vault(test_database_url, rows)

    sample = rows[0]
    original_bytes = (vault_dir / sample.path).read_bytes()
    expected_content = rules.prepare_write_or_edit(
        "write", sample.path, original_bytes, None, None, None
    )

    pool = await asyncpg.create_pool(test_database_url)
    try:
        row = await pool.fetchrow(
            "select content, version, current_revision from vault_notes where id = $1",
            sample.id,
        )
        assert row is not None
        stored_content = bytes(row["content"])
        assert stored_content == expected_content
        assert row["version"] == version(stored_content)
        assert row["version"] == sample.version
        assert row["current_revision"] == 1

        revision_count = await pool.fetchval(
            "select count(*) from vault_revisions where note_id = $1", sample.id
        )
        assert revision_count == 1
    finally:
        await pool.close()


async def test_a_loaded_row_accepts_a_normal_edit_through_postgres_backend(
    tmp_path: Path, test_database_url: str
) -> None:
    vault_dir = _generate_vault(tmp_path / "vault-out")
    rows = build_rows(vault_dir)
    await load_vault(test_database_url, rows)

    sample = rows[0]
    note = parse((vault_dir / sample.path).read_bytes())
    old_str = f"title: {note.title}"
    new_str = f"{old_str} edited"

    pool = await asyncpg.create_pool(test_database_url)
    try:
        backend = PostgresBackend(pool)
        result = await backend.edit(
            sample.path, old_str, new_str, if_version=sample.version, client="test", actor="test"
        )
        assert result.path == sample.path

        revision_count = await pool.fetchval(
            "select count(*) from vault_revisions where note_id = $1", sample.id
        )
        assert revision_count == 2

        current_revision = await pool.fetchval(
            "select current_revision from vault_notes where id = $1", sample.id
        )
        assert current_revision == 2
    finally:
        await pool.close()


async def test_create_principal_tokens_are_verifiable_read_write_all_namespace_tokens(
    tmp_path: Path, test_database_url: str
) -> None:
    vault_dir = _generate_vault(tmp_path / "vault-out")
    rows = build_rows(vault_dir)
    await load_vault(test_database_url, rows)

    namespaces = {
        "namespaces": {
            "user-00001": {"kind": "personal", "members": ["user-00001"]},
            "group-001": {"kind": "group", "members": ["user-00001"]},
            "org": {"kind": "org", "members": ["user-00001"]},
        }
    }

    pool = await asyncpg.create_pool(test_database_url)
    try:
        tokens = await create_principal_tokens(
            pool,
            ["user-00001"],
            namespaces,
            rows,
            rng=random.Random(_SEED),  # noqa: S311 - deterministic sampling, not a secret
        )
        assert len(tokens) == 1
        entry = tokens[0]
        assert entry["alias"] == "user-00001"
        assert entry["namespaces"] == ["group-001", "org", "user-00001"]

        info = await verify(pool, entry["token"])
        assert info is not None
        assert set(info.scopes) == {READ_SCOPE, WRITE_SCOPE}
        assert info.namespaces == ("*",)
        assert info.owner_oid == "oid-user-00001"
        assert info.roles == ("Memory.User",)
    finally:
        await pool.close()


async def test_populated_registry_resolves_rls_and_personal_namespace(
    tmp_path: Path, test_database_url: str
) -> None:
    """`populate_registry` must make `loadtest.load`'s own synthetic
    principals resolve exactly the way `db.rls`/`mcp.namespaces` resolve a
    real one (#124): the RLS functions see the same `users`/`namespaces`/
    `user_groups` rows a real identity would, `mm_ensure_personal_ns()`
    confirms the generator's own alias rather than inventing a `u-<id>` one,
    and the sampled read paths a static token gets never point outside what
    that identity can actually read.
    """
    out = tmp_path / "vault-out"
    generate(notes=200, users=_USERS, groups=_GROUPS, seed=_SEED, out=out)
    vault_dir = out / "vault"
    rows = build_rows(vault_dir)
    await load_vault(test_database_url, rows)

    namespaces = json.loads((out / "namespaces.json").read_text(encoding="utf-8"))
    alias = "user-00001"
    entries = namespaces["namespaces"]
    own_groups = sorted(
        group_alias
        for group_alias, info in entries.items()
        if info["kind"] == "group" and alias in info["members"]
    )
    org_alias = next(a for a, info in entries.items() if info["kind"] == "org")

    pool = await asyncpg.create_pool(test_database_url)
    try:
        await populate_registry(pool, namespaces)

        tokens = await create_principal_tokens(
            pool,
            [alias],
            namespaces,
            rows,
            rng=random.Random(_SEED),  # noqa: S311 - deterministic sampling, not a secret
        )
        read_paths = tokens[0]["read_paths"]
        assert read_paths
        assert all(path.startswith("me/") or path.startswith("org/") for path in read_paths)

        present_rows = await pool.fetch("select distinct namespace from vault_notes")
        present = {row["namespace"] for row in present_rows}
        expected = present & {alias, org_alias, *own_groups}

        app_role = f"mm_test_loadtest_app_{secrets.token_hex(8)}"
        conn = await asyncpg.connect(test_database_url)
        try:
            await conn.execute(f'create role "{app_role}" nologin nosuperuser nobypassrls')
            await grant_app_role(conn, app_role)

            async with request_identity(
                conn, role=app_role, oid=f"oid-{alias}", roles=["Memory.User"]
            ):
                own_alias = await conn.fetchval("select mm_ensure_personal_ns()")
                observed = {
                    row["namespace"]
                    for row in await conn.fetch("select distinct namespace from vault_notes")
                }
        finally:
            await conn.close()
    finally:
        await pool.close()

    assert own_alias == alias
    assert observed == expected
