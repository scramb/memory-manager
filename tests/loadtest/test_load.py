# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the synthetic-vault loader (#108, #124, #267): the pure row-
building half against a generated vault on disk, the async half against a
real Postgres (`test_database_url`, see `tests/conftest.py`)."""

from __future__ import annotations

import json
import random
import secrets
from pathlib import Path

import asyncpg
import pytest

from loadtest.generate import generate
from loadtest.load import (
    build_chunk_rows,
    build_chunk_rows_parallel,
    build_rows,
    create_principal_tokens,
    hnsw_indexes_from_migration,
    load_chunks_with_index,
    load_vault,
    namespace_kinds,
    populate_registry,
)
from loadtest.vectors import synthetic_vector
from memory_manager.auth.tokens import verify
from memory_manager.db.migrate import migrate
from memory_manager.db.rls import grant_app_role, request_identity
from memory_manager.index.chunker import chunk_note
from memory_manager.index.indexer import Indexer
from memory_manager.mcp.authz import READ_SCOPE, WRITE_SCOPE
from memory_manager.storage import rules
from memory_manager.storage.postgres import PostgresBackend
from memory_manager.vault.note import parse, version

_NOTES = 50
_USERS = 5
_GROUPS = 2
_SEED = 1

# #267's own DoD ("on a 500-note vault") - `users`/`groups`/`seed` match
# `loadtest.generate`'s own `test_every_namespace_kind_gets_notes_with_the_
# default_kinds_mix` fixture shape, so every ADR-0016 partition
# (`user`/`group`/`project`/`org`) is known to receive at least one note.
_CHUNK_NOTES = 500
_CHUNK_USERS = 10
_CHUNK_GROUPS = 3
_CHUNK_SEED = 1


def _generate_vault(out: Path) -> Path:
    generate(notes=_NOTES, users=_USERS, groups=_GROUPS, seed=_SEED, out=out)
    return out / "vault"


def _generate_chunk_vault(out: Path) -> tuple[Path, dict[str, object]]:
    """A larger, every-kind-covered vault for the `--with-chunks` tests."""
    generate(
        notes=_CHUNK_NOTES, users=_CHUNK_USERS, groups=_CHUNK_GROUPS, seed=_CHUNK_SEED, out=out
    )
    namespaces = json.loads((out / "namespaces.json").read_text(encoding="utf-8"))
    return out / "vault", namespaces


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
    `user_groups`/`project_members` rows a real identity would,
    `mm_ensure_personal_ns()` confirms the generator's own alias rather
    than inventing a `u-<id>` one, and the sampled read paths a static
    token gets never point outside what that identity can actually read.
    `generate`'s own default `--kinds` also produces `project` namespaces
    (ADR-0008) - `own_projects` below accounts for those too (#267
    follow-up), the same way `own_groups` already did for `group` ones.
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
    # `generate`'s own default `--kinds` also produces `project` namespaces
    # (ADR-0008) - `populate_registry` registers their membership in
    # `project_members` too (#267 follow-up), so `mm_readable_ns()` grants
    # read on these exactly like it does for `own_groups`.
    own_projects = sorted(
        project_alias
        for project_alias, info in entries.items()
        if info["kind"] == "project" and alias in info["members"]
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
        expected = present & {alias, org_alias, *own_groups, *own_projects}

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


def test_namespace_kinds_maps_the_generator_vocabulary_onto_adr_0016() -> None:
    """#267: `namespaces.json`'s own `kind` ("personal"/"group"/"project"/
    "org") must land on the exact values `chunks.namespace_kind`'s check
    constraint accepts ("user"/"group"/"project"/"org") - only "personal"
    actually changes spelling."""
    namespaces = {
        "namespaces": {
            "user-00001": {"kind": "personal", "members": []},
            "group-001": {"kind": "group", "members": []},
            "proj-001": {"kind": "project", "members": []},
            "org": {"kind": "org", "members": []},
        }
    }

    assert namespace_kinds(namespaces) == {
        "user-00001": "user",
        "group-001": "group",
        "proj-001": "project",
        "org": "org",
    }


def test_build_chunk_rows_uses_the_production_chunker_with_the_indexer_s_own_arguments(
    tmp_path: Path,
) -> None:
    """#267: `build_chunk_rows` must never reimplement chunking differently
    from `index/indexer.py`'s own `_upsert_note_rows` - same chunker, same
    `title`/`body`/`description`/`aliases`/`tags` arguments, for every note."""
    out = tmp_path / "vault-out"
    vault_dir, namespaces = _generate_chunk_vault(out)
    rows = build_rows(vault_dir)
    kinds = namespace_kinds(namespaces)

    chunk_rows = build_chunk_rows(rows, kinds)

    expected: set[tuple[str, int, str, str]] = set()
    for row in rows:
        note = parse(row.content)
        for chunk in chunk_note(
            note.title,
            note.body,
            description=note.description,
            aliases=note.aliases,
            tags=note.tags,
        ):
            expected.add((note.id, chunk.ord, chunk.heading_path, chunk.text))

    actual = {(r.note_id, r.ord, r.heading_path, r.text) for r in chunk_rows}
    assert actual == expected
    assert actual  # the generated vault actually has chunks to compare

    namespace_by_note = {row.id: row.namespace for row in rows}
    assert all(r.namespace_kind == kinds[namespace_by_note[r.note_id]] for r in chunk_rows)


async def test_build_chunk_rows_matches_what_a_real_indexer_run_writes(
    tmp_path: Path, admin_database_url: str
) -> None:
    """#267's own DoD: "the chunk rows equal what `index.indexer` would write
    for the same notes (text, positions, note id)" - checked against an
    actual `Indexer(FileTreeSource).reindex_full()` run, in its own fresh
    database (never the one `--with-chunks` loads into - the two would
    otherwise collide on `chunks`' own unique `(note_id, ord,
    namespace_kind)` constraint)."""
    out = tmp_path / "vault-out"
    vault_dir, namespaces = _generate_chunk_vault(out)
    rows = build_rows(vault_dir)
    kinds = namespace_kinds(namespaces)
    loader_chunks = {
        (r.note_id, r.ord, r.heading_path, r.text) for r in build_chunk_rows(rows, kinds)
    }

    ref_db_name = f"mm_test_loadtest_ref_{secrets.token_hex(8)}"
    admin_conn = await asyncpg.connect(admin_database_url)
    try:
        await admin_conn.execute(f'create database "{ref_db_name}"')
    finally:
        await admin_conn.close()
    base, _, _ = admin_database_url.rpartition("/")
    ref_db_url = f"{base}/{ref_db_name}"

    try:
        migration_conn = await asyncpg.connect(ref_db_url)
        try:
            await migrate(migration_conn, backend="postgres")
        finally:
            await migration_conn.close()

        ref_pool = await asyncpg.create_pool(ref_db_url)
        try:
            await Indexer(ref_pool, vault_dir, provider=None).reindex_full()
            indexer_rows = await ref_pool.fetch(
                "select note_id, ord, heading_path, text from chunks"
            )
        finally:
            await ref_pool.close()
    finally:
        admin_conn = await asyncpg.connect(admin_database_url)
        try:
            await admin_conn.execute(
                "select pg_terminate_backend(pid) from pg_stat_activity "
                "where datname = $1 and pid <> pg_backend_pid()",
                ref_db_name,
            )
            await admin_conn.execute(f'drop database if exists "{ref_db_name}"')
        finally:
            await admin_conn.close()

    indexer_chunks = {
        (row["note_id"], row["ord"], row["heading_path"], row["text"]) for row in indexer_rows
    }
    assert loader_chunks == indexer_chunks
    assert loader_chunks  # the generated vault actually has chunks to compare


async def test_with_chunks_fills_every_partition_with_full_dimension_vectors_and_the_hnsw_index(
    tmp_path: Path, test_database_url: str
) -> None:
    """#267: `--with-chunks`'s own DoD - every ADR-0016 partition holds rows,
    every chunk carries a full `EMBEDDING_DIMENSIONS`-wide vector, and the
    HNSW index exists with the migration's own `m`/`ef_construction`
    options (`pg_class.reloptions`)."""
    out = tmp_path / "vault-out"
    vault_dir, namespaces = _generate_chunk_vault(out)
    rows = build_rows(vault_dir)

    await load_vault(test_database_url, rows, backend="postgres")
    results = await load_chunks_with_index(test_database_url, rows, namespaces)

    total_chunks = results["chunks"]
    assert isinstance(total_chunks, int)
    assert total_chunks > 0

    partition_counts = results["chunks_per_partition"]
    assert isinstance(partition_counts, dict)
    assert set(partition_counts) == {
        "chunks_user",
        "chunks_group",
        "chunks_project",
        "chunks_org",
    }
    assert all(count > 0 for count in partition_counts.values())

    indexes = hnsw_indexes_from_migration()
    assert {index.table for index in indexes} == {"chunks_group", "chunks_project", "chunks_org"}

    conn = await asyncpg.connect(test_database_url)
    try:
        for table in partition_counts:
            dims = await conn.fetch(
                f"select vector_dims(embedding::vector) as dims from {table}"  # noqa: S608
            )
            assert dims, f"{table} has no rows"
            assert all(row["dims"] == 1024 for row in dims)

        for index in indexes:
            reloptions = await conn.fetchval(
                "select reloptions from pg_class where relname = $1", index.name
            )
            assert reloptions is not None
            options = {opt.split("=")[0]: opt.split("=")[1] for opt in reloptions}
            assert options["m"] == "16"
            assert options["ef_construction"] == "64"
    finally:
        await conn.close()

    # One chunk's own vector must be reproducible: the exact same key
    # (`"{note_id}:{ord}"`) always yields the same `synthetic_vector`, in a
    # separate process too (#267, shared with the generator's own
    # `vector_key` and the embedding stub, #268).
    sample_note_id = next(iter(rows)).id
    conn = await asyncpg.connect(test_database_url)
    try:
        sample = await conn.fetchrow(
            "select ord, embedding::text as embedding from chunks where note_id = $1 order by ord",
            sample_note_id,
        )
    finally:
        await conn.close()
    assert sample is not None
    expected_vector = synthetic_vector(f"{sample_note_id}:{sample['ord']}", 1024)
    stored_vector = [float(v) for v in sample["embedding"].strip("[]").split(",")]
    # `halfvec` stores fp16 (~3 decimal digits of precision), so the
    # round-tripped value is never bit-identical to the fp64 Python float -
    # this only checks it is still recognisably the same vector.
    assert stored_vector == pytest.approx(expected_vector, rel=1e-2, abs=1e-3)


async def test_populate_registry_registers_project_namespaces_for_mm_namespace_kind(
    tmp_path: Path, test_database_url: str
) -> None:
    """#267 follow-up: a `proj-*` namespace must resolve through the
    registry exactly like a personal/group/org one - `populate_registry`
    used to only ever insert `user`/`group`/`org` rows into `namespaces`,
    so ADR-0016's `mm_namespace_kind()` resolved every `proj-*` alias to
    `null` and a real `index/indexer.py` write into it fell back to the
    `'org'` partition instead of `chunks_project`. Also checks the
    generator's own project membership lands in `project_members`, so
    `mm_writable_ns()`'s `project_write` CTE actually grants a member write.
    """
    out = tmp_path / "vault-out"
    generate(notes=50, users=3, groups=1, projects=2, seed=_SEED, out=out)
    namespaces = json.loads((out / "namespaces.json").read_text(encoding="utf-8"))

    migration_conn = await asyncpg.connect(test_database_url)
    try:
        await migrate(migration_conn, backend="postgres")
    finally:
        await migration_conn.close()

    pool = await asyncpg.create_pool(test_database_url)
    try:
        await populate_registry(pool, namespaces)

        project_alias, project_info = next(
            (alias, info)
            for alias, info in namespaces["namespaces"].items()
            if info["kind"] == "project"
        )
        kind = await pool.fetchval("select mm_namespace_kind($1)", project_alias)
        assert kind == "project"

        member_alias = project_info["members"][0]
        member_role = await pool.fetchval(
            "select pm.role from project_members pm "
            "join namespaces n on n.id = pm.namespace_id "
            "where n.alias = $1 and pm.principal_kind = 'user' and pm.principal_id = $2",
            project_alias,
            f"oid-{member_alias}",
        )
        assert member_role == "writer"
    finally:
        await pool.close()


async def test_build_chunk_rows_parallel_matches_the_serial_version(tmp_path: Path) -> None:
    """#267 follow-up: parallelising chunk generation across processes
    (`build_chunk_rows_parallel`) must never change the result - same
    rows, same order - compared to the single-process `build_chunk_rows`.
    `_CHUNK_NOTES`/`_CHUNK_SHARD_SIZE` (200) guarantee more than one shard
    here, so this actually exercises the multi-shard, multi-process path,
    not just a single-shard no-op.
    """
    out = tmp_path / "vault-out"
    vault_dir, namespaces = _generate_chunk_vault(out)
    rows = build_rows(vault_dir)
    kinds = namespace_kinds(namespaces)

    serial = build_chunk_rows(rows, kinds)
    parallel = [row for shard in build_chunk_rows_parallel(rows, kinds) for row in shard]

    assert parallel == serial
    assert serial  # the generated vault actually has chunks to compare
