# SPDX-License-Identifier: AGPL-3.0-only
"""Retrieval quality evaluation against the golden query set (#32).

`load_golden` parses `eval/golden.yaml`'s format into `GoldenQuery` entries.
`run_eval` scores every entry against an already-indexed Postgres pool with
`search.hybrid_search`, computing `recall_at_k` and `reciprocal_rank` per
query and aggregating both overall and per `kind` (see `golden.yaml`'s
header comment for what `kind` means). `compare` checks an `EvalReport`
against a committed baseline and reports any metric that regressed.

Building the temporary database the eval runs against, and reading/writing
`eval/baseline.json`, are CLI concerns (`cli.py`'s `eval` subcommand) - this
module only knows how to score queries that have already been run.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import asyncpg
import yaml

from memory_manager.index.embeddings import EmbeddingProvider
from memory_manager.search import hybrid_search

__all__ = [
    "EvalReport",
    "GoldenQuery",
    "KindMetrics",
    "QueryResult",
    "compare",
    "load_golden",
    "recall_at_k",
    "reciprocal_rank",
    "run_eval",
]


@dataclass(frozen=True)
class GoldenQuery:
    """One golden-set entry: `query` is expected to retrieve every id in `expected`."""

    id: str
    query: str
    expected: tuple[str, ...]
    lang: str
    kind: str


@dataclass(frozen=True)
class QueryResult:
    """One golden query's outcome against the index: its hits and resulting metrics."""

    golden: GoldenQuery
    hits: tuple[str, ...]
    recall: float
    reciprocal_rank: float


@dataclass(frozen=True)
class KindMetrics:
    """Recall@k and MRR aggregated over every query of one golden-set `kind`."""

    count: int
    recall_at_k: float
    mrr: float


@dataclass(frozen=True)
class EvalReport:
    """The result of one `run_eval` call: overall and per-`kind` metrics."""

    k: int
    recall_at_k: float
    mrr: float
    per_query: tuple[QueryResult, ...]
    per_kind: dict[str, KindMetrics]


def load_golden(path: Path) -> list[GoldenQuery]:
    """Parse the golden set at `path` (`eval/golden.yaml`'s format)."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [
        GoldenQuery(
            id=entry["id"],
            query=entry["query"],
            expected=tuple(entry["expected"]),
            lang=entry["lang"],
            kind=entry["kind"],
        )
        for entry in raw
    ]


def recall_at_k(results: Sequence[str], expected: Sequence[str], k: int) -> float:
    """The fraction of `expected` found in the first `k` of `results`.

    `0.0` if `expected` is empty - every golden entry has at least one
    expected id, but an empty list is handled rather than raising.
    """
    if not expected:
        return 0.0
    top_k = set(results[:k])
    hit = sum(1 for note_id in expected if note_id in top_k)
    return hit / len(expected)


def reciprocal_rank(results: Sequence[str], expected: Sequence[str]) -> float:
    """`1 / rank` of the first `results` entry that is in `expected`, else `0.0`."""
    expected_set = set(expected)
    for rank, note_id in enumerate(results, start=1):
        if note_id in expected_set:
            return 1.0 / rank
    return 0.0


async def run_eval(
    pool: asyncpg.Pool,
    golden: Sequence[GoldenQuery],
    *,
    provider: EmbeddingProvider | None = None,
    k: int = 5,
) -> EvalReport:
    """Run every `golden` query against the index in `pool` and score it.

    Each query runs through `hybrid_search(..., limit=k)`; its hits are the
    returned notes' ids, best first. Metrics are aggregated overall and per
    `GoldenQuery.kind`.
    """
    results: list[QueryResult] = []
    for entry in golden:
        notes = await hybrid_search(pool, entry.query, provider=provider, limit=k)
        hits = tuple(note.note_id for note in notes)
        results.append(
            QueryResult(
                golden=entry,
                hits=hits,
                recall=recall_at_k(hits, entry.expected, k),
                reciprocal_rank=reciprocal_rank(hits, entry.expected),
            )
        )

    return EvalReport(
        k=k,
        recall_at_k=_mean(result.recall for result in results),
        mrr=_mean(result.reciprocal_rank for result in results),
        per_query=tuple(results),
        per_kind=_aggregate_by_kind(results),
    )


def compare(
    report: EvalReport, baseline: Mapping[str, object], *, tolerance: float = 0.0
) -> list[str]:
    """Metrics of `report` that regressed against `baseline`.

    A metric regresses when it drops by more than `tolerance` below the
    matching `baseline` value; an equal or improved metric, or a metric
    missing from `baseline`, is not reported. Returns one message per
    regressed metric, empty if there is none.
    """
    regressions: list[str] = []
    for name, current in (("recall_at_k", report.recall_at_k), ("mrr", report.mrr)):
        baseline_value = baseline.get(name)
        if not isinstance(baseline_value, int | float):
            continue
        if current < baseline_value - tolerance:
            regressions.append(
                f"{name} regressed: {current:.4f} < baseline {baseline_value:.4f} "
                f"(tolerance {tolerance:.4f})"
            )
    return regressions


def _aggregate_by_kind(results: Sequence[QueryResult]) -> dict[str, KindMetrics]:
    by_kind: dict[str, list[QueryResult]] = {}
    for result in results:
        by_kind.setdefault(result.golden.kind, []).append(result)

    return {
        kind: KindMetrics(
            count=len(items),
            recall_at_k=_mean(item.recall for item in items),
            mrr=_mean(item.reciprocal_rank for item in items),
        )
        for kind, items in sorted(by_kind.items())
    }


def _mean(values: Iterable[float]) -> float:
    collected = list(values)
    return sum(collected) / len(collected) if collected else 0.0
