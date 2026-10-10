# SPDX-License-Identifier: AGPL-3.0-only
"""Plan regression test for #296/#271: an unfiltered `chunks_user` vector leg
narrows to the caller's own readable namespaces instead of scanning the whole
partition.

Seeds through the real app role (`db.rls.request_identity`, not the
migration-owner connection `tests/search/test_vector_per_kind.py`'s own
`postgres_conn`/`postgres_pool` fixtures use) - `_VECTOR_SQL_KIND_TEMPLATE`'s
new predicate is conditioned on `app.oid` (`search.py`'s own module comment
on it), so a test exercising it needs a real RLS identity, not an
RLS-exempt owner connection. The fixture below mirrors `tests/db/
test_frequent_lexemes_partitions.py`'s `partitioned_db` (a non-superuser
owner + a `NOLOGIN` app role, the same CNPG-provisioned shape `db.rls.
grant_app_role` expects), trimmed to this file's own needs.

**Why the assertion is on the planner's own row estimate, not "no Seq Scan"/
"chunks_namespace_idx used" by name (the issue's own suggested check), and
not `explain (analyze)`'s *actual* row count either.** Empirically probed
against this exact migrated schema, from 1,500 up to 200,000 seeded
`chunks_user` rows across hundreds of namespaces, with every combination of
`enable_seqscan`/`enable_bitmapscan`/`enable_nestloop`/`random_page_cost`
this file's own author tried: Postgres never once chose `chunks_namespace_idx`
over `chunks_user`'s own primary key or its `(note_id, ord, namespace_kind)`
unique index - both of which happen to be at least as cheap to scan in full
at these row counts, the same "found only at spike scale" limitation
`test_vector_per_kind.py`'s own module docstring already documents for the
sibling HNSW index. Forcing `enable_seqscan = off` does make *some* index win
over a `Seq Scan` node, but it does so for the pre-fix query exactly as
readily as the fixed one, so that check alone cannot tell a fixed query from
a broken one here. `explain (analyze)`'s *actual* row count fares no better,
for the opposite reason: `chunks_select`'s own RLS `exists`, even without
this fix's predicate, gets planned as a *hashed* SubPlan at this fixture's
row counts (Postgres building the small set of the caller's own readable
namespaces once and probing it per row) - cheap enough in practice that the
pre-fix query's *actual* rows past the filter are already small, masking the
difference an `analyze`-free `explain` still shows plainly.

What the planner's own (pre-`analyze`-of-the-query, cost-estimate-only)
`Plan Rows` at whichever node touches `chunks_user` *does* reliably show,
at every scale tried: pre-fix, that estimate is the hashed SubPlan's own
generic, pessimistic guess - roughly half the partition, regardless of its
real selectivity, because an opaque correlated subquery gives the planner
nothing to estimate from; post-fix, `c.namespace = any(...)` is an ordinary
`text[]` containment test the planner estimates properly, collapsing the
same number to a small multiple of the caller's own row count. That estimate
is exactly what would let a real-scale planner prefer `chunks_namespace_idx`
over scanning the whole partition - this test cannot force that *choice* at
fixture scale (the paragraph above), but it can show the fix gives the
planner the *information* that choice would be based on. `enable_nestloop =
off` is needed to see any of this at a test-sized fixture: left on, the
planner's own join reordering routes through `notes` first (restricted by
its own, already-indexable RLS predicate, `notes_namespace_type_idx`) and
reaches `chunks_user` only by an already-narrow `note_id`, which is cheap
regardless of whether `chunks_user` itself carries a namespace predicate -
masking the exact difference this test exists to catch.

`TestNamespaceFilterPlan` is the plan check above; `TestReadableRowsMatchRls`
is the issue's own second checklist item - the fix's result set equals a
plain RLS-only query's, not just its row count.
"""

from __future__ import annotations

import json
import random
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest_asyncio

from memory_manager.db.migrate import migrate
from memory_manager.db.rls import grant_app_role, request_identity
from memory_manager.search import (
    _HNSW_KIND_SETTINGS,
    _VECTOR_SQL_KIND_TEMPLATE,
    _fetch_vector_kind_rows,
)

__all__: list[str] = []

_MODEL = "test-model"
_DIMENSION = 1024
_CALLER_OID = "oid-me"
_CALLER_NAMESPACE = "me"
_OTHER_NAMESPACE_COUNT = 150
_ROWS_PER_OTHER_NAMESPACE = 8
_CALLER_ROW_COUNT = 3


def _unit_vector(seed: int, dimension: int = _DIMENSION) -> list[float]:
    """A deterministic, unit-norm vector - content never matters here (this
    file only checks which rows a query reaches, not ranking).
    """
    rng = random.Random(seed)  # noqa: S311 - deterministic fixture data, not crypto
    vector = [rng.gauss(0, 1) for _ in range(dimension)]
    norm = sum(value * value for value in vector) ** 0.5
    return [value / norm for value in vector]


def _vector_literal(seed: int, dimension: int = _DIMENSION) -> str:
    """`_unit_vector`'s own `halfvec` literal text, for the raw-SQL seeding
    inserts below (`_fetch_vector_kind_rows` takes the plain float sequence
    instead, and builds its own literal internally).
    """
    return "[" + ",".join(str(value) for value in _unit_vector(seed, dimension)) + "]"


@dataclass(frozen=True)
class _NamespaceFilterDb:
    owner_conn: asyncpg.Connection
    app_role: str


@pytest_asyncio.fixture
async def namespace_filter_db(admin_database_url: str) -> AsyncIterator[_NamespaceFilterDb]:
    """A `backend="postgres"`-migrated database owned by a non-superuser role,
    with a `NOLOGIN` app role granted exactly `db.rls.grant_app_role`'s own
    privileges (same shape as `tests/db/test_frequent_lexemes_partitions.py`'s
    `partitioned_db`), seeded with one caller (`_CALLER_OID`, namespace
    `_CALLER_NAMESPACE`) plus `_OTHER_NAMESPACE_COUNT` unrelated `user`
    namespaces the caller must never see.
    """
    db_name = f"mm_test_nsfilter_{secrets.token_hex(8)}"
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

        await owner_conn.execute(
            "insert into users (oid, tid, display_name) values ($1, 't1', 'Me')", _CALLER_OID
        )
        await owner_conn.execute(
            "insert into namespaces (kind, external_key, alias) values ('user', $1, $2)",
            _CALLER_OID,
            _CALLER_NAMESPACE,
        )

        note_rows: list[tuple[str, str, str]] = []
        chunk_rows: list[tuple[str, str, str, str, int]] = []

        def _seed(namespace: str, note_id: str, seed: int) -> None:
            note_rows.append((note_id, f"{namespace}/fact/{note_id}.md", namespace))
            chunk_rows.append((note_id, namespace, _vector_literal(seed), _MODEL, _DIMENSION))

        for row in range(_CALLER_ROW_COUNT):
            _seed(_CALLER_NAMESPACE, f"{_CALLER_NAMESPACE}-note-{row}", row)

        for ns_index in range(_OTHER_NAMESPACE_COUNT):
            namespace = f"other-ns-{ns_index}"
            for row in range(_ROWS_PER_OTHER_NAMESPACE):
                _seed(namespace, f"{namespace}-note-{row}", 1000 + ns_index * 100 + row)

        await owner_conn.executemany(
            "insert into notes "
            "(id, path, namespace, type, slug, title, description, created, updated, file_hash) "
            "values ($1, $2, $3, 'fact', $1, 'title', 'description', now(), now(), md5($1))",
            note_rows,
        )
        await owner_conn.executemany(
            "insert into chunks "
            "(note_id, namespace, namespace_kind, ord, text, embedding, model, dimension) "
            "values ($1, $2, 'user', 0, 'chunk text', $3::halfvec, $4, $5)",
            chunk_rows,
        )

        yield _NamespaceFilterDb(owner_conn=owner_conn, app_role=app_role)
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


def _nodes_touching_relation(
    node: object, relation: str, acc: list[dict[str, object]]
) -> list[dict[str, object]]:
    """Every plan node (`explain (format json)`'s own shape) whose `Relation
    Name` is `relation`, anywhere in the tree.
    """
    if isinstance(node, dict):
        if node.get("Relation Name") == relation:
            acc.append(node)
        for value in node.values():
            _nodes_touching_relation(value, relation, acc)
    elif isinstance(node, list):
        for item in node:
            _nodes_touching_relation(item, relation, acc)
    return acc


def _plan_rows(node: dict[str, object]) -> int:
    """`node["Plan Rows"]` (a JSON number, parsed as `int`/`float`), as `int`."""
    value = node["Plan Rows"]
    assert isinstance(value, int | float), f"unexpected Plan Rows type: {value!r}"
    return int(value)


class TestNamespaceFilterPlan:
    """An unfiltered `user`-kind vector search gives the planner a properly
    estimable restriction to the caller's own namespace, instead of only the
    hashed-but-opaque RLS `exists` whose selectivity the planner can only
    guess at (module docstring above).
    """

    async def test_unfiltered_search_estimate_reflects_the_namespace_filter(
        self, namespace_filter_db: _NamespaceFilterDb
    ) -> None:
        conn = namespace_filter_db.owner_conn
        # Per #296's own checklist: `ANALYZE` the partition the per-kind leg
        # actually reads (`chunks_user`, not the partitioned parent `chunks`
        # - it carries no rows of its own, `tests/db/
        # test_frequent_lexemes_partitions.py`'s own module docstring).
        await conn.execute("analyze chunks_user")

        total_other_rows = _OTHER_NAMESPACE_COUNT * _ROWS_PER_OTHER_NAMESPACE
        sql = "explain (format json) " + _VECTOR_SQL_KIND_TEMPLATE.format(dim=_DIMENSION)
        query_vector = _vector_literal(seed=999999)

        async with request_identity(
            conn, role=namespace_filter_db.app_role, oid=_CALLER_OID, roles=()
        ) as app_conn:
            for statement in _HNSW_KIND_SETTINGS["user"]:
                await app_conn.execute(statement)
            # `enable_nestloop = off`: without it, the planner's own join
            # reordering reaches `chunks_user` through `notes` (already
            # narrowed there by `notes_select`'s own indexable RLS predicate)
            # by the small, already-matched set of `note_id`s - cheap
            # regardless of whether `chunks_user` itself carries a namespace
            # predicate, which would hide exactly the regression this test
            # exists to catch (module docstring above).
            await app_conn.execute("set local enable_nestloop = off")
            plan = await app_conn.fetch(
                sql,
                query_vector,
                "user",
                _MODEL,
                _DIMENSION,
                False,
                None,
                [],
                None,
                None,
                10,
            )

        parsed_plan = json.loads(plan[0]["QUERY PLAN"])
        chunks_user_nodes = _nodes_touching_relation(parsed_plan[0]["Plan"], "chunks_user", [])
        assert chunks_user_nodes, "expected the plan to touch chunks_user at all"
        estimated_rows = max(_plan_rows(node) for node in chunks_user_nodes)

        # Pre-fix, this estimate sits at roughly half of every row outside
        # the caller's own (the hashed RLS `exists`'s own generic guess,
        # module docstring); post-fix, a plain `text[]` containment test
        # against `c.namespace` lets the planner estimate properly instead,
        # collapsing it to a small multiple of the caller's own row count.
        # The bound below sits well below that pre-fix estimate and well
        # above the post-fix one, with margin on both sides for exact
        # selectivity-estimator differences across Postgres versions.
        bound = total_other_rows // 10
        assert estimated_rows <= bound, (
            f"expected the planner's own row estimate for chunks_user to reflect "
            f"the caller's {_CALLER_ROW_COUNT} own rows, not the {total_other_rows} "
            f"rows belonging to other namespaces - got {estimated_rows}, over the "
            f"{bound}-row bound"
        )


class TestReadableRowsMatchRls:
    """The fix's own result set for an unfiltered search is exactly what a
    plain RLS-only query (no `c.namespace` predicate of its own) returns for
    the same caller - a pure narrowing, not a different set of rows.
    """

    async def test_unfiltered_search_matches_rls_only_query(
        self, namespace_filter_db: _NamespaceFilterDb
    ) -> None:
        conn = namespace_filter_db.owner_conn
        query_vector = _unit_vector(seed=424242)

        async with request_identity(
            conn, role=namespace_filter_db.app_role, oid=_CALLER_OID, roles=()
        ) as app_conn:
            rows = await _fetch_vector_kind_rows(
                app_conn,
                "user",
                query_vector,
                _MODEL,
                _DIMENSION,
                limit=50,
                include_archived=False,
                types=None,
                tags=None,
                namespaces=None,
                valid_at=None,
            )
            rls_only_rows = await app_conn.fetch(
                "select c.id as chunk_id, n.namespace as namespace "
                "from chunks c join notes n on n.id = c.note_id "
                "where c.namespace_kind = 'user' and c.model = $1 and c.dimension = $2",
                _MODEL,
                _DIMENSION,
            )

        fixed_chunk_ids = {row["chunk_id"] for row in rows}
        rls_only_chunk_ids = {row["chunk_id"] for row in rls_only_rows}

        assert fixed_chunk_ids, "expected the caller's own rows back"
        assert fixed_chunk_ids == rls_only_chunk_ids
        assert {row["namespace"] for row in rls_only_rows} == {_CALLER_NAMESPACE}
