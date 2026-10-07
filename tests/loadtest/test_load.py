# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the synthetic-vault loader (#108): the pure row-building half
against a generated vault on disk, the async half against a real Postgres
(`test_database_url`, see `tests/conftest.py`)."""

from __future__ import annotations

from pathlib import Path

import asyncpg

from loadtest.generate import generate
from loadtest.load import build_rows, create_principal_tokens, load_vault
from memory_manager.auth.tokens import verify
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
        tokens = await create_principal_tokens(pool, ["user-00001"], namespaces)
        assert len(tokens) == 1
        entry = tokens[0]
        assert entry["alias"] == "user-00001"
        assert entry["namespaces"] == ["group-001", "org", "user-00001"]

        info = await verify(pool, entry["token"])
        assert info is not None
        assert set(info.scopes) == {READ_SCOPE, WRITE_SCOPE}
        assert info.namespaces == ("*",)
    finally:
        await pool.close()
