# SPDX-License-Identifier: AGPL-3.0-only
"""`fulltext_search` stays bounded when a query term is extremely common (#117).

Seeds `chunks` with the word "note" in every chunk (so it is the most
common lexeme in both `tsv_simple` and `tsv_lang`) plus a unique marker per
chunk, then `analyze`s - the same shape `test_index_plans.py` uses for its
own planner-facing fixture, just over `chunks` instead of `notes`/`links`.
"""

from __future__ import annotations

import json

import asyncpg
import pytest_asyncio

from memory_manager.db.migrate import migrate
from memory_manager.search import _CANDIDATE_CAP, _build_fulltext_query, fulltext_search

__all__: list[str] = []

# Large enough that scoring/sorting every match of a frequent term is
# unmistakably slower than the capped candidate stage, small enough to seed
# via `generate_series` well within a test's own timeout.
_CHUNK_COUNT = 50_000


def _marker(i: int) -> str:
    return f"marker{i:08d}"


async def _seed(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        insert into notes (
            id, path, namespace, type, slug, title, description, created, updated, file_hash
        )
        select
            'n' || lpad(i::text, 8, '0'),
            'personal/fact/note-' || i || '.md',
            'personal',
            'fact',
            'note-' || i,
            'Note ' || i,
            'a description',
            now(),
            now(),
            md5(i::text)
        from generate_series(1, $1) as i
        """,
        _CHUNK_COUNT,
    )
    await conn.execute(
        """
        insert into chunks (note_id, ord, text, lang)
        select
            'n' || lpad(i::text, 8, '0'),
            0,
            'note marker' || lpad(i::text, 8, '0'),
            case when i % 2 = 0 then 'en' else null end
        from generate_series(1, $1) as i
        """,
        _CHUNK_COUNT,
    )
    # `analyze`, not `vacuum analyze` - the container's shared memory limits
    # make a full `vacuum` of a 50k-row fixture table expensive per test.
    await conn.execute("analyze chunks")


@pytest_asyncio.fixture
async def seeded_conn(conn: asyncpg.Connection) -> asyncpg.Connection:
    await migrate(conn)
    await _seed(conn)
    return conn


def _cte_scan_rows(node: object, cte_name: str) -> list[int]:
    """`Actual Rows` of every `CTE Scan` node for `cte_name`, anywhere in an `EXPLAIN` tree."""
    found: list[int] = []
    if isinstance(node, dict):
        if node.get("Node Type") == "CTE Scan" and node.get("CTE Name") == cte_name:
            found.append(int(node["Actual Rows"]))
        for value in node.values():
            found.extend(_cte_scan_rows(value, cte_name))
    elif isinstance(node, list):
        for item in node:
            found.extend(_cte_scan_rows(item, cte_name))
    return found


async def _explain_candidates(conn: asyncpg.Connection, query: str, *, limit: int = 50) -> int:
    """Run `EXPLAIN (ANALYZE, FORMAT JSON)` for `query` and return the `candidates`
    CTE's actual row count - the number of rows that ever reach scoring/sorting.
    """
    sql, args = _build_fulltext_query(
        query,
        include_archived=False,
        limit=limit,
        types=None,
        tags=None,
        namespaces=None,
        valid_at=None,
        use_selective=True,
    )
    rows = await conn.fetch(f"explain (analyze, format json) {sql}", *args)
    raw_plan = rows[0]["QUERY PLAN"]
    plan = json.loads(raw_plan) if isinstance(raw_plan, str) else raw_plan
    candidate_rows = _cte_scan_rows(plan, "candidates")
    assert candidate_rows, f"no 'candidates' CTE scan node in plan for {query!r}: {plan}"
    return candidate_rows[0]


class TestBoundedCandidates:
    async def test_marker_query_ranks_its_own_chunk_first_within_the_cap(
        self, seeded_conn: asyncpg.Connection
    ) -> None:
        marker = _marker(1234)
        hits = await fulltext_search(seeded_conn, marker)

        assert hits, f"no hits for {marker!r}"
        assert hits[0].path == "personal/fact/note-1234.md"

        candidate_rows = await _explain_candidates(seeded_conn, marker)
        assert candidate_rows <= _CANDIDATE_CAP

    async def test_frequent_only_query_stays_within_the_cap(
        self, seeded_conn: asyncpg.Connection
    ) -> None:
        # "note" is in every one of the 50k chunks - the extreme case where
        # a branch's only lexeme is frequent and there is nothing selective
        # left to filter the candidate stage on.
        candidate_rows = await _explain_candidates(seeded_conn, "note")
        assert candidate_rows <= _CANDIDATE_CAP

        hits = await fulltext_search(seeded_conn, "note", limit=5)
        assert len(hits) == 5

    async def test_frequent_plus_nonexistent_lexeme_still_returns_hits(
        self, seeded_conn: asyncpg.Connection
    ) -> None:
        # "note" is frequent and left out of the selective match; the
        # nonexistent word matches nothing at all. Without the retry in
        # `fulltext_search`, the selective-filtered stage would come back
        # empty even though "note or zzznonexistentzzz" should hit on "note".
        hits = await fulltext_search(seeded_conn, "note or zzznonexistentzzz", limit=5)
        assert hits


class TestFrequentLexemesFunctionUnderANonOwnerRole:
    async def test_security_definer_sees_pg_stats_without_table_privileges(
        self, seeded_conn: asyncpg.Connection
    ) -> None:
        role_name = "mm_test_no_privileges_role"
        await seeded_conn.execute(f'create role "{role_name}" login')
        try:
            # `pg_stats` itself already hides a table's rows from a role
            # that cannot `select` from that table (standard Postgres
            # behaviour, not something this migration changes) - revoking
            # `chunks` is the actual boundary `security definer` has to
            # cross below.
            await seeded_conn.execute(f'revoke all on chunks from "{role_name}"')

            # `set local role` inside a transaction is enough to make the
            # grant above apply to the statements that follow - no need
            # for a second connection to prove `security definer` bridges
            # the missing table privilege.
            async with seeded_conn.transaction():
                await seeded_conn.execute(f'set local role "{role_name}"')

                direct_rows = await seeded_conn.fetch(
                    "select 1 from pg_stats where tablename = 'chunks' and attname = 'tsv_simple'"
                )
                assert direct_rows == [], "role should not see pg_stats rows for chunks directly"

                result = await seeded_conn.fetchval(
                    "select mm_frequent_lexemes('tsv_simple', $1::text[], 100)",
                    ["note"],
                )
                assert result == ["note"]
        finally:
            await seeded_conn.execute("reset role")
            await seeded_conn.execute(f'drop role if exists "{role_name}"')
