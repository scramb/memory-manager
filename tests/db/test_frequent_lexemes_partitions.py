# SPDX-License-Identifier: AGPL-3.0-only
"""`mm_frequent_lexemes` under the ADR-0016 partitioned `chunks` (#293).

`migrations/postgres/0024_frequent_lexemes_partitions.sql` replaces
`0008_frequent_lexemes.sql`'s original body for `backend="postgres"`: the
original read `pg_stats`/`reltuples` for `tablename = 'chunks'` - the
partitioned *parent*, which autovacuum never `ANALYZE`s on its own (a
partitioned table carries no rows of its own) and whose own inherited
array-element statistics proved unreliable against a real load-test vault
(#293's own root cause). The fix sums each lexeme's estimated row count
from every leaf partition's own, independently `ANALYZE`d stats instead
(found via `pg_inherits`, not hard-coded partition names).

This seeds directly into `chunks_user`/`chunks_group` (mirrors
`tests/search/test_vector_per_kind.py`'s own raw-insert style), analyzes
*only* those two partitions - never the parent `chunks` itself, the exact
gap this migration closes - and calls the function as the non-owner,
non-superuser app role (`db.rls.request_identity`), the same role/grant
shape `db/rls.py`'s `grant_app_role` gives a real request connection
(ADR-0008 addendum).
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest_asyncio

from memory_manager.db.migrate import migrate
from memory_manager.db.rls import grant_app_role, request_identity

__all__: list[str] = []

_MODEL = "test-model"
_DIMENSION = 1024


@dataclass(frozen=True)
class _PartitionedDb:
    owner_conn: asyncpg.Connection
    app_role: str


@pytest_asyncio.fixture
async def partitioned_db(admin_database_url: str) -> AsyncIterator[_PartitionedDb]:
    """A `backend="postgres"`-migrated database owned by a non-superuser role,
    with a `NOLOGIN` app role granted exactly `db.rls.grant_app_role`'s own
    privileges - the same CNPG-provisioned shape `tests/db/test_rls.py`'s own
    `rls_db` fixture builds, trimmed to just what this file needs.
    """
    db_name = f"mm_test_freqlex_{secrets.token_hex(8)}"
    owner_role = f"mm_test_owner_{secrets.token_hex(8)}"
    owner_password = secrets.token_urlsafe(16)
    app_role = f"mm_test_app_{secrets.token_hex(8)}"

    admin_conn = await asyncpg.connect(admin_database_url)
    owner_conn: asyncpg.Connection | None = None
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
        await migrate(owner_conn, backend="postgres")
        await grant_app_role(owner_conn, app_role)

        yield _PartitionedDb(owner_conn=owner_conn, app_role=app_role)
    finally:
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


async def _seed_chunk(
    conn: asyncpg.Connection, *, note_id: str, namespace: str, namespace_kind: str, text: str
) -> None:
    await conn.execute(
        "insert into notes "
        "(id, path, namespace, type, slug, title, description, created, updated, file_hash) "
        "values ($1, $2, $3, 'fact', $1, 'title', 'description', now(), now(), md5($1))",
        note_id,
        f"{namespace}/fact/{note_id}.md",
        namespace,
    )
    await conn.execute(
        "insert into chunks (note_id, namespace, namespace_kind, ord, text, model, dimension) "
        "values ($1, $2, $3, 0, $4, $5, $6)",
        note_id,
        namespace,
        namespace_kind,
        text,
        _MODEL,
        _DIMENSION,
    )


class TestFrequentLexemesAcrossPartitions:
    """A lexeme spread across `chunks_user` and `chunks_group` is recognised as
    frequent from those two partitions' own stats alone - the partitioned
    parent `chunks` is never analyzed in this test at all, the exact gap
    #293's fix closes.
    """

    async def test_lexeme_in_most_rows_across_two_partitions_is_frequent(
        self, partitioned_db: _PartitionedDb
    ) -> None:
        conn = partitioned_db.owner_conn

        # ~99 of 100 rows in each of two partitions carry "widespread" - a
        # lexeme no single partition's own row count alone would need
        # inflating to look frequent; this is testing the *sum* across
        # partitions, not one partition's own share.
        for i in range(100):
            await _seed_chunk(
                conn,
                note_id=f"user-note-{i}",
                namespace=f"user-ns-{i}",
                namespace_kind="user",
                text="widespread filler text" if i != 0 else "something else entirely",
            )
        for i in range(100):
            await _seed_chunk(
                conn,
                note_id=f"group-note-{i}",
                namespace=f"group-ns-{i}",
                namespace_kind="group",
                text="widespread filler text" if i != 0 else "something else entirely",
            )
        # Also plant a genuinely rare lexeme, once, in only one partition.
        await _seed_chunk(
            conn,
            note_id="rare-note",
            namespace="user-ns-rare",
            namespace_kind="user",
            text="raremarker appears exactly once",
        )

        # Analyze only the leaf partitions - never the partitioned parent
        # `chunks` itself (autovacuum never would either, #293's own root
        # cause: a partitioned table carries no rows of its own for
        # `ANALYZE` to sample).
        await conn.execute("analyze chunks_user")
        await conn.execute("analyze chunks_group")

        parent_stats = await conn.fetchval(
            "select array_length(most_common_elems, 1) from pg_stats "
            "where tablename = 'chunks' and attname = 'tsv_simple'"
        )
        assert parent_stats is None, (
            "the partitioned parent must carry no stats of its own for this test to "
            "exercise #293's fix - got stats anyway, the fixture's own premise is wrong"
        )

        async with request_identity(
            conn, role=partitioned_db.app_role, oid="tester", roles=()
        ) as app_conn:
            frequent = await app_conn.fetchval(
                "select mm_frequent_lexemes('tsv_simple', $1::text[], $2)",
                ["widespread", "raremarker"],
                100.0,
            )

        assert frequent == ["widespread"], (
            f"expected only the cross-partition-common lexeme, got {frequent}"
        )
