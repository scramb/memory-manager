# SPDX-License-Identifier: AGPL-3.0-only
"""`fulltext_search` stays bounded when a query term is extremely common (#117).

Seeds `chunks` with the word "note" in every chunk (so it is the most
common lexeme in both `tsv_simple` and `tsv_lang`) plus a unique marker per
chunk, then `analyze`s - the same shape `test_index_plans.py` uses for its
own planner-facing fixture, just over `chunks` instead of `notes`/`links`.
"""

from __future__ import annotations

import json
import time

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


def _frequent_lexemes_call_loops(node: object) -> list[int]:
    """`Actual Loops` of every plan node that calls `mm_frequent_lexemes`.

    Looks at a node's `Output`/`Filter` text (needs `verbose`, not just
    `analyze`) rather than its `Node Type`, because where Postgres ends up
    evaluating the call depends on the plan: materialized, it shows up in
    a `CTE Scan`'s `Output`; inlined into `candidates`' per-row filter
    (the regression this guards against), it shows up in a `Function
    Scan`'s `Filter` instead - evaluated once per candidate row, not once
    overall.
    """
    found: list[int] = []
    if isinstance(node, dict):
        for field in ("Output", "Filter"):
            value = node.get(field)
            if value is not None and "mm_frequent_lexemes" in str(value):
                found.append(int(node["Actual Loops"]))
        for value in node.values():
            found.extend(_frequent_lexemes_call_loops(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_frequent_lexemes_call_loops(item))
    return found


async def _explain_verbose(
    conn: asyncpg.Connection, query: str, *, limit: int = 50, use_selective: bool = True
) -> object:
    """Run `EXPLAIN (ANALYZE, FORMAT JSON, VERBOSE)` for `query`'s plan, as a dict."""
    sql, args = _build_fulltext_query(
        query,
        include_archived=False,
        limit=limit,
        types=None,
        tags=None,
        namespaces=None,
        valid_at=None,
        use_selective=use_selective,
    )
    rows = await conn.fetch(f"explain (analyze, format json, verbose) {sql}", *args)
    raw_plan = rows[0]["QUERY PLAN"]
    plan = json.loads(raw_plan) if isinstance(raw_plan, str) else raw_plan
    return plan[0]["Plan"]


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


class TestFrequentLexemesLookupIsBounded:
    """`lexemes`/`frequent`/`selective` must run once per search, not once per
    candidate row (#117 follow-up).

    Left unmaterialized, Postgres is free to inline these CTEs into
    `candidates`' own per-row join filter - `mm_frequent_lexemes` (and the
    `selective` computation built from its result) then re-runs once for
    every one of the up to 50k rows `candidates`' join considers before its
    own `limit` kicks in, rather than the single time its result actually
    needs computing. That is the slow path this fixture is built to expose:
    "note" is the most common lexeme in the fixture, so the un-fixed query
    evaluates `mm_frequent_lexemes` tens of thousands of times before
    returning.
    """

    async def test_frequent_lexemes_lookup_runs_once_not_per_candidate_row(
        self, seeded_conn: asyncpg.Connection
    ) -> None:
        plan = await _explain_verbose(seeded_conn, "note")
        loops = _frequent_lexemes_call_loops(plan)

        assert loops, "no plan node calls mm_frequent_lexemes - fixture or query changed?"
        # Two config branches (`tsv_simple`, `tsv_lang`) share the one call
        # site each; `<= 2` stays far under "once per candidate row" while
        # still catching the regression (25,000 loops against the
        # unmaterialized CTEs, on this fixture).
        assert max(loops) <= 2, f"mm_frequent_lexemes ran per-row, not once: loops={loops}"

    async def test_frequent_term_search_is_fast_on_a_50k_chunk_vault(
        self, seeded_conn: asyncpg.Connection
    ) -> None:
        # A marker query (rare lexeme) stays comparatively fast even with
        # the unmaterialized CTEs, since the candidate stage's `tsv_simple
        # @@ q.simple` filter alone already narrows the scan to ~1 row
        # before `mm_frequent_lexemes` gets re-evaluated per surviving row.
        # "note" - every chunk's lexeme - does not narrow anything, so it
        # is what actually exposes the per-row cost (tens of seconds
        # un-fixed on this fixture, measured while diagnosing #117's
        # follow-up) without flaking on an otherwise-fast query.
        start = time.monotonic()
        hits = await fulltext_search(seeded_conn, "note", limit=5)
        elapsed = time.monotonic() - start

        assert len(hits) == 5
        # Generous enough not to flake on a loaded machine (fixed: ~150ms
        # measured locally), tight enough that re-running
        # `mm_frequent_lexemes` per candidate row unmistakably fails it.
        assert elapsed < 1.0, f"fulltext_search('note') took {elapsed:.3f}s"


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
