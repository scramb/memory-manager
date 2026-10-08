# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the ADR-0016 spike's synthetic-vector generator (#125).

Only the pure, DB-independent parts (`build_namespaces`, `iter_rows`,
`format_vector`) are covered here - `cmd_load`/`cmd_index`/`cmd_bench` talk
to a real Postgres and are exercised manually against the spike's own
scratch container, not in CI (no new CI dependency for a one-off research
tool).
"""

from __future__ import annotations

import math

from loadtest.vector_index import build_namespaces, format_vector, iter_rows, namespace_weights

_DIM = 16


def test_build_namespaces_has_one_of_each_kind_plus_the_requested_counts() -> None:
    namespaces = build_namespaces(users=5, groups=2, projects=1)

    kinds = [ns.kind for ns in namespaces]
    assert kinds.count("personal") == 5
    assert kinds.count("group") == 2
    assert kinds.count("project") == 1
    assert kinds.count("org") == 1
    assert len(namespaces) == 9


def test_namespace_weights_are_zipf_by_rank() -> None:
    weights = namespace_weights(4)
    assert weights == [1.0, 0.5, 1 / 3, 0.25]


def test_iter_rows_is_deterministic_for_the_same_seed() -> None:
    namespaces = build_namespaces(users=10, groups=3, projects=1)

    first = list(iter_rows(50, dim=_DIM, namespaces=namespaces, seed=7))
    second = list(iter_rows(50, dim=_DIM, namespaces=namespaces, seed=7))

    assert first == second


def test_iter_rows_differs_for_a_different_seed() -> None:
    namespaces = build_namespaces(users=10, groups=3, projects=1)

    first = list(iter_rows(50, dim=_DIM, namespaces=namespaces, seed=7))
    second = list(iter_rows(50, dim=_DIM, namespaces=namespaces, seed=8))

    assert first != second


def test_iter_rows_embeddings_are_unit_vectors_with_the_requested_dimension() -> None:
    namespaces = build_namespaces(users=10, groups=3, projects=1)

    for _, _, _, vector in iter_rows(20, dim=_DIM, namespaces=namespaces, seed=1):
        assert len(vector) == _DIM
        norm = math.sqrt(sum(v * v for v in vector))
        assert math.isclose(norm, 1.0, rel_tol=1e-6)


def test_iter_rows_every_row_has_a_valid_namespace_and_kind() -> None:
    namespaces = build_namespaces(users=10, groups=3, projects=1)
    aliases = {ns.alias: ns.kind for ns in namespaces}

    for _, namespace, kind, _ in iter_rows(100, dim=_DIM, namespaces=namespaces, seed=1):
        assert namespace in aliases
        assert aliases[namespace] == kind


def test_format_vector_round_trips_through_pgvectors_bracket_syntax() -> None:
    literal = format_vector([1.0, -2.5, 0.0], precision=2)
    assert literal == "[1.00,-2.50,0.00]"
