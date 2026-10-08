# SPDX-License-Identifier: AGPL-3.0-only
"""Synthetic vectors and measurement driver for the ADR-0016 spike (#125, WP-23).

This is the "synthetic-vector generator and measurement scripts" the spike's
Definition of Done asks for, reusable by WP-32's target-size load test: a
deterministic generator of `chunks`-shaped rows (namespace, namespace kind,
embedding) at arbitrary scale, plus a small CLI driver that loads them into a
scratch schema, builds an HNSW index over them and benchmarks it - all
*outside* `src/`, so it never touches the real schema or search code (Blast
Radius: `loadtest/` and `docs/research/vector-index.md` only).

Namespace mix mirrors `loadtest.generate`'s shape (personal/group/org, Zipf-
by-rank weights over the namespace list - see `_build_namespaces` there),
extended with the fourth kind ADR-0008 added since: `project`. The `agent`
kind (ADR-0013) is deliberately *not* generated here: its rows would be
indistinguishable from `personal` ones for every measurement in this module
(same one-owner cardinality), so partitioning support for it is demonstrated
structurally instead (`PARTITION_DDL` below, and `docs/research/vector-index.md`
shows the one extra `CREATE TABLE ... PARTITION OF ... FOR VALUES IN ('agent')`
statement that attaches it later, additively, with no change to the
partitions already in place).

Embeddings are synthetic: `iter_rows` draws each vector from one of
`--clusters` random unit centroids plus Gaussian noise, then re-normalises -
"clustered, unit-normalised synthetic vectors (Gaussian mixture)", the same
shape `docs/research/enterprise.md` §3 "Synthetic 1M notes" describes. No
real embedding model and no real content is involved; recall against *real*
semantic content is measured separately, against the golden set
(`docs/research/vector-index.md` §2).

Usage (see `docs/research/vector-index.md` §1 for the exact invocations used
for the spike's own measurements)::

    python -m loadtest.vector_index load   --admin-url postgresql://... \\
        --table chunks_vec_1024 --vector-type vector --dim 1024 --rows 1000000
    python -m loadtest.vector_index index  --admin-url postgresql://... \\
        --table chunks_vec_1024 --vector-type vector --dim 1024 \\
        --m 16 --ef-construction 64
    python -m loadtest.vector_index bench  --admin-url postgresql://... \\
        --table chunks_vec_1024 --dim 1024 --scenario personal --ef-search 100

Every subcommand prints one JSON object to stdout - this module has no
opinion on how results are aggregated, that is `docs/research/vector-index.md`'s
job, written by hand from these numbers.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import math
import random
import sys
import time
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass

import asyncpg

__all__ = [
    "Namespace",
    "build_namespaces",
    "format_vector",
    "iter_rows",
    "namespace_weights",
]

_KINDS = ("personal", "group", "project", "org")
_ORG_ALIAS = "org"

# The one statement that attaches a fifth, `agent` partition to a table
# built with PARTITION_DDL - never executed here, just documented: it shows
# that namespace kind `agent` (ADR-0013) is additive to the partitioning
# scheme this module measures, not a reason to redesign it.
AGENT_PARTITION_DDL = "create table {table}_agent partition of {table} for values in ('agent')"


@dataclass(frozen=True)
class Namespace:
    """One synthetic namespace: its alias and kind (personal/group/project/org)."""

    alias: str
    kind: str


def build_namespaces(users: int, groups: int, projects: int) -> list[Namespace]:
    """One personal namespace per user, `groups` groups, `projects` projects, one org.

    Shape mirrors `loadtest.generate._build_namespaces`, extended with the
    `project` kind ADR-0008 added. Membership lists are not needed here (this
    module never writes notes or checks ACLs, only namespace + kind), so
    unlike `loadtest.generate` this returns bare `Namespace` rows.
    """
    namespaces = [Namespace(f"user-{u:05d}", "personal") for u in range(1, users + 1)]
    namespaces += [Namespace(f"group-{g:03d}", "group") for g in range(1, groups + 1)]
    namespaces += [Namespace(f"proj-{p:03d}", "project") for p in range(1, projects + 1)]
    namespaces.append(Namespace(_ORG_ALIAS, "org"))
    return namespaces


def namespace_weights(count: int) -> list[float]:
    """Zipf-by-rank weights for `count` namespaces: rank 1 is `1/1`, rank 2 `1/2`, ...

    Identical scheme to `loadtest.generate._namespace_weights` - reused, not
    reimplemented differently, so both generators spread notes/chunks over
    namespaces the same way.
    """
    return [1.0 / rank for rank in range(1, count + 1)]


def _unit_vector(rng: random.Random, dim: int) -> list[float]:
    values = [rng.gauss(0.0, 1.0) for _ in range(dim)]
    return _normalize(values)


def _normalize(values: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return [v / norm for v in values]


def iter_rows(
    total: int,
    *,
    dim: int,
    namespaces: Sequence[Namespace],
    seed: int,
    clusters: int = 24,
    noise: float = 0.15,
    start_id: int = 1,
) -> Iterator[tuple[int, str, str, list[float]]]:
    """Deterministically yield `total` synthetic `(id, namespace, kind, embedding)` rows.

    The same `seed`, `dim`, `namespaces` and `clusters` always produce the
    same rows. Each row's namespace is drawn by Zipf rank (`namespace_weights`)
    over `namespaces` in list order; its embedding is one of `clusters` random
    unit centroids (drawn once, up front) perturbed by Gaussian `noise` and
    re-normalised, i.e. a Gaussian-mixture-on-the-sphere model - the point is
    realistic-enough clustering for HNSW to behave like it does on real
    embeddings (uniform random vectors are an *easier* case for recall, not a
    harder one - docs/research/enterprise.md §3's 64-dim lab run used uniform
    random and flagged it as worst-case; this module's default is clustered
    instead, closer to the common case).
    """
    rng = random.Random(seed)  # noqa: S311 - reproducible synthetic data, not a secret
    centroids = [_unit_vector(rng, dim) for _ in range(clusters)]

    weights = namespace_weights(len(namespaces))
    cumulative = list(itertools.accumulate(weights))
    total_weight = cumulative[-1]

    for i in range(total):
        draw = rng.random() * total_weight
        index = _bisect_left(cumulative, draw)
        if index >= len(namespaces):
            index = len(namespaces) - 1
        namespace = namespaces[index]

        centroid = centroids[rng.randrange(clusters)]
        vector = _normalize([c + rng.gauss(0.0, noise) for c in centroid])
        yield (start_id + i, namespace.alias, namespace.kind, vector)


def _bisect_left(values: Sequence[float], target: float) -> int:
    low, high = 0, len(values)
    while low < high:
        mid = (low + high) // 2
        if values[mid] < target:
            low = mid + 1
        else:
            high = mid
    return low


def format_vector(values: Sequence[float], precision: int = 5) -> str:
    """Render `values` as the `'[1.00000,...]'` text pgvector's input function parses.

    `precision` trades row size for fidelity; 5 decimal digits is far more
    than an HNSW cosine search can tell apart (`docs/research/vector-index.md`
    notes this - the synthetic rows are already lossy before `halfvec` ever
    rounds them to fp16, so this module's own halfvec-vs-vector *size and
    build-time* numbers are unaffected, and its recall numbers are read as
    relative, exactly like the existing 100k/200k lab runs in
    `docs/research/enterprise.md` §1/§3).
    """
    return "[" + ",".join(f"{v:.{precision}f}" for v in values) + "]"


async def copy_source(
    rows: Iterator[tuple[int, str, str, list[float]]], *, batch_rows: int = 2000
) -> AsyncIterator[bytes]:
    """Turn `rows` into `COPY ... FROM STDIN` text-format chunks, batched for throughput."""
    buf: list[str] = []
    for row_id, namespace, kind, vector in rows:
        buf.append(f"{row_id}\t{namespace}\t{kind}\t{format_vector(vector)}\n")
        if len(buf) >= batch_rows:
            yield "".join(buf).encode("utf-8")
            buf.clear()
    if buf:
        yield "".join(buf).encode("utf-8")


# --- Scratch schema DDL -----------------------------------------------------
#
# Deliberately not the real `chunks` table (Blast Radius excludes `src/`):
# just enough columns for the measurements this spike needs (id, namespace,
# kind, embedding). `UNLOGGED` - this data is disposable and never needs to
# survive a crash, and skipping WAL roughly doubles COPY throughput on the
# lab hardware.

_FLAT_DDL = """
create unlogged table if not exists {table} (
    id bigint primary key,
    namespace text not null,
    kind text not null,
    embedding {vector_type}({dim}) not null
)
"""

_PARTITIONED_DDL = """
create unlogged table if not exists {table} (
    id bigint,
    namespace text not null,
    kind text not null,
    embedding {vector_type}({dim}) not null
) partition by list (kind)
"""

_PARTITION_DDL = (
    "create unlogged table if not exists {table}_{kind} "
    "partition of {table} for values in ('{kind}')"
)

_INDEX_DDL = (
    "create index if not exists {index} on {table} "
    "using hnsw (embedding {ops}) with (m = {m}, ef_construction = {ef_construction})"
)

_RLS_POLICY_DDL = """
alter table {table} enable row level security;
alter table {table} force row level security;
drop policy if exists {table}_ns_policy on {table};
create policy {table}_ns_policy on {table}
    using (namespace = any(coalesce(nullif(current_setting('app.ns', true), ''), '{{}}')::text[]))
"""


def _vector_type_ops(vector_type: str) -> str:
    if vector_type == "vector":
        return "vector_cosine_ops"
    if vector_type == "halfvec":
        return "halfvec_cosine_ops"
    raise ValueError(f"unknown vector type {vector_type!r}")


async def cmd_load(args: argparse.Namespace) -> dict[str, object]:
    conn = await asyncpg.connect(args.admin_url)
    try:
        await conn.execute("create extension if not exists vector")
        if args.partitioned:
            await conn.execute(
                _PARTITIONED_DDL.format(
                    table=args.table, vector_type=args.vector_type, dim=args.dim
                )
            )
            for kind in _KINDS:
                await conn.execute(_PARTITION_DDL.format(table=args.table, kind=kind))
        else:
            await conn.execute(
                _FLAT_DDL.format(table=args.table, vector_type=args.vector_type, dim=args.dim)
            )
        await conn.execute(f"truncate {args.table}")

        namespaces = build_namespaces(args.users, args.groups, args.projects)
        rows = iter_rows(
            args.rows,
            dim=args.dim,
            namespaces=namespaces,
            seed=args.seed,
            clusters=args.clusters,
        )
        start = time.monotonic()
        status = await conn.copy_to_table(
            args.table,
            source=copy_source(rows),
            columns=["id", "namespace", "kind", "embedding"],
            format="text",
        )
        elapsed = time.monotonic() - start
        copied = int(status.rsplit(" ", 1)[-1]) if status else args.rows
        await conn.execute(f"analyze {args.table}")
        return {
            "cmd": "load",
            "table": args.table,
            "vector_type": args.vector_type,
            "dim": args.dim,
            "partitioned": args.partitioned,
            "rows": copied,
            "seconds": round(elapsed, 2),
            "rows_per_second": round(copied / elapsed, 1) if elapsed else None,
        }
    finally:
        await conn.close()


async def cmd_index(args: argparse.Namespace) -> dict[str, object]:
    conn = await asyncpg.connect(args.admin_url)
    try:
        if args.maintenance_work_mem:
            await conn.execute(f"set maintenance_work_mem = '{args.maintenance_work_mem}'")
        if args.max_parallel_maintenance_workers is not None:
            await conn.execute(
                f"set max_parallel_maintenance_workers = {args.max_parallel_maintenance_workers}"
            )
        ops = _vector_type_ops(args.vector_type)
        index_name = f"{args.table}_hnsw"
        await conn.execute(f"drop index if exists {index_name}")

        start = time.monotonic()
        await conn.execute(
            _INDEX_DDL.format(
                index=index_name,
                table=args.table,
                ops=ops,
                m=args.m,
                ef_construction=args.ef_construction,
            )
        )
        elapsed = time.monotonic() - start

        if args.partitioned:
            # A partitioned index's own catalog row carries no pages; its
            # children (one real HNSW index per partition) are linked via
            # `pg_inherits` the same way partition tables are.
            index_size = await conn.fetchval(
                "select coalesce(sum(pg_relation_size(inhrelid)), 0) "
                "from pg_inherits where inhparent = $1::regclass",
                index_name,
            )
            table_size = await conn.fetchval(
                "select coalesce(sum(pg_total_relation_size(inhrelid)), 0) "
                "from pg_inherits where inhparent = $1::regclass",
                args.table,
            )
        else:
            index_size = await conn.fetchval("select pg_relation_size($1::regclass)", index_name)
            table_size = await conn.fetchval(
                "select pg_total_relation_size($1::regclass)", args.table
            )
        # `args.table`/`table` below is this spike's own scratch identifier
        # (argparse, never request input) - DDL/identifier interpolation,
        # not a value that could carry injected SQL.
        row_count = await conn.fetchval(f"select count(*) from {args.table}")  # noqa: S608

        return {
            "cmd": "index",
            "table": args.table,
            "vector_type": args.vector_type,
            "dim": args.dim,
            "m": args.m,
            "ef_construction": args.ef_construction,
            "maintenance_work_mem": args.maintenance_work_mem,
            "rows": row_count,
            "build_seconds": round(elapsed, 2),
            "index_bytes": int(index_size or 0),
            "table_total_bytes": int(table_size or 0),
        }
    finally:
        await conn.close()


_SCENARIOS: dict[str, str] = {
    "personal": "one personal namespace (narrowest selectivity)",
    "group": "20 group namespaces (moderate selectivity)",
    "org": "every namespace (unfiltered)",
}


async def _visible_namespaces(
    conn: asyncpg.Connection, table: str, scenario: str, rng: random.Random
) -> list[str]:
    if scenario == "org":
        rows = await conn.fetch(f"select distinct namespace from {table}")  # noqa: S608
        return [r["namespace"] for r in rows]
    if scenario == "personal":
        rows = await conn.fetch(
            f"select distinct namespace from {table} where kind = 'personal' limit 1000"  # noqa: S608
        )
        return [rng.choice([r["namespace"] for r in rows])] if rows else []
    if scenario == "group":
        rows = await conn.fetch(
            f"select distinct namespace from {table} where kind = 'group'"  # noqa: S608
        )
        aliases = [r["namespace"] for r in rows]
        rng.shuffle(aliases)
        return aliases[:20]
    raise ValueError(f"unknown scenario {scenario!r}")


async def cmd_bench(args: argparse.Namespace) -> dict[str, object]:
    conn = await asyncpg.connect(args.admin_url)
    try:
        await conn.execute(_RLS_POLICY_DDL.format(table=args.table))
        # `args.app_role` is this spike's own CLI argument, never request input.
        await conn.execute(
            f"do $$ begin "  # noqa: S608
            f"if not exists (select 1 from pg_roles where rolname = '{args.app_role}') then "
            f"create role {args.app_role} nologin; end if; end $$"
        )
        await conn.execute(f"grant select on {args.table} to {args.app_role}")

        rng = random.Random(args.seed)  # noqa: S311 - reproducible synthetic data, not a secret
        namespaces = build_namespaces(args.users, args.groups, args.projects)
        query_rows = list(
            iter_rows(
                args.queries,
                dim=args.dim,
                namespaces=namespaces,
                seed=args.seed + 1,
                clusters=args.clusters,
                start_id=0,
            )
        )

        visible = await _visible_namespaces(conn, args.table, args.scenario, rng)

        latencies: list[float] = []
        recalls: list[float] = []
        for _, _, _, vector in query_rows:
            vec_literal = format_vector(vector)
            async with conn.transaction():
                await conn.execute("select set_config('role', $1, true)", args.app_role)
                ns_literal = "{" + ",".join(visible) + "}"
                await conn.execute("select set_config('app.ns', $1, true)", ns_literal)
                if args.force_index_scan:
                    # The planner's own cost-based choice is part of what this
                    # spike measures (a selective namespace filter on a *flat*,
                    # unpartitioned table routes around the HNSW index entirely -
                    # see docs/research/vector-index.md §1) - this flag is only
                    # for isolating "how would a filtered *approximate* search
                    # behave" as a separate question from "what does the planner
                    # actually choose today".
                    await conn.execute("set local enable_seqscan = off")
                    await conn.execute("set local enable_bitmapscan = off")
                if args.ef_search is not None:
                    await conn.execute(f"set local hnsw.ef_search = {args.ef_search}")
                if args.iterative_scan is not None:
                    await conn.execute(f"set local hnsw.iterative_scan = '{args.iterative_scan}'")
                if args.max_scan_tuples is not None:
                    await conn.execute(f"set local hnsw.max_scan_tuples = {args.max_scan_tuples}")

                start = time.monotonic()
                approx = await conn.fetch(
                    f"select id from {args.table} where namespace = any($1::text[]) "  # noqa: S608
                    f"order by embedding <=> $2::{args.vector_type}({args.dim}) limit {args.k}",
                    visible,
                    vec_literal,
                )
                latencies.append((time.monotonic() - start) * 1000.0)

                if args.measure_recall:
                    await conn.execute("set local enable_indexscan = off")
                    await conn.execute("set local enable_bitmapscan = off")
                    exact = await conn.fetch(
                        f"select id from {args.table} where namespace = any($1::text[]) "  # noqa: S608
                        f"order by embedding <=> $2::{args.vector_type}({args.dim}) limit {args.k}",
                        visible,
                        vec_literal,
                    )
                    exact_ids = {r["id"] for r in exact}
                    approx_ids = {r["id"] for r in approx}
                    recalls.append(
                        len(exact_ids & approx_ids) / len(exact_ids) if exact_ids else 0.0
                    )
                await conn.execute("select set_config('role', 'none', true)")

        latencies.sort()
        return {
            "cmd": "bench",
            "table": args.table,
            "scenario": args.scenario,
            "visible_namespaces": len(visible),
            "ef_search": args.ef_search,
            "iterative_scan": args.iterative_scan,
            "max_scan_tuples": args.max_scan_tuples,
            "queries": len(latencies),
            "p50_ms": round(_percentile(latencies, 0.50), 2) if latencies else None,
            "p95_ms": round(_percentile(latencies, 0.95), 2) if latencies else None,
            "recall_at_k": round(sum(recalls) / len(recalls), 4) if recalls else None,
        }
    finally:
        await conn.close()


def _percentile(sorted_values: Sequence[float], fraction: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, int(len(sorted_values) * fraction))
    return sorted_values[index]


async def cmd_drop(args: argparse.Namespace) -> dict[str, object]:
    conn = await asyncpg.connect(args.admin_url)
    try:
        await conn.execute(f"drop table if exists {args.table} cascade")
        return {"cmd": "drop", "table": args.table}
    finally:
        await conn.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loadtest.vector_index",
        description="synthetic vectors and measurement driver for the ADR-0016 spike (#125)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--admin-url", required=True, help="postgres admin connection URL")
    common.add_argument("--table", required=True, help="scratch table name")
    common.add_argument("--vector-type", choices=["vector", "halfvec"], default="vector")
    common.add_argument("--dim", type=int, required=True)

    load_p = sub.add_parser("load", parents=[common])
    load_p.add_argument("--rows", type=int, default=1_000_000)
    load_p.add_argument("--users", type=int, default=2000)
    load_p.add_argument("--groups", type=int, default=500)
    load_p.add_argument("--projects", type=int, default=50)
    load_p.add_argument("--seed", type=int, default=1)
    load_p.add_argument("--clusters", type=int, default=24)
    load_p.add_argument("--partitioned", action="store_true")

    index_p = sub.add_parser("index", parents=[common])
    index_p.add_argument("--m", type=int, default=16)
    index_p.add_argument("--ef-construction", type=int, default=64)
    index_p.add_argument("--maintenance-work-mem", default=None)
    index_p.add_argument("--max-parallel-maintenance-workers", type=int, default=None)
    index_p.add_argument("--partitioned", action="store_true")

    bench_p = sub.add_parser("bench", parents=[common])
    bench_p.add_argument("--scenario", choices=list(_SCENARIOS), required=True)
    bench_p.add_argument("--queries", type=int, default=30)
    bench_p.add_argument("--k", type=int, default=10)
    bench_p.add_argument("--ef-search", type=int, default=None)
    bench_p.add_argument("--iterative-scan", choices=["off", "strict_order", "relaxed_order"])
    bench_p.add_argument("--max-scan-tuples", type=int, default=None)
    bench_p.add_argument("--app-role", default="mm_vector_spike_app")
    bench_p.add_argument("--users", type=int, default=2000)
    bench_p.add_argument("--groups", type=int, default=500)
    bench_p.add_argument("--projects", type=int, default=50)
    bench_p.add_argument("--seed", type=int, default=1)
    bench_p.add_argument("--clusters", type=int, default=24)
    bench_p.add_argument("--measure-recall", action="store_true")
    bench_p.add_argument(
        "--force-index-scan",
        action="store_true",
        help="disable seqscan/bitmapscan so a selective filter still uses the HNSW index",
    )

    sub.add_parser("drop", parents=[common])

    return parser


async def _dispatch(args: argparse.Namespace) -> dict[str, object]:
    if args.command == "load":
        return await cmd_load(args)
    if args.command == "index":
        return await cmd_index(args)
    if args.command == "bench":
        return await cmd_bench(args)
    if args.command == "drop":
        return await cmd_drop(args)
    raise ValueError(f"unknown command {args.command!r}")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    result = asyncio.run(_dispatch(args))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
