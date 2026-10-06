# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the retrieval eval's metric math and baseline comparison (#32)."""

from __future__ import annotations

import pytest

from memory_manager.eval import EvalReport, compare, recall_at_k, reciprocal_rank


def _report(recall_at_k: float, mrr: float) -> EvalReport:
    return EvalReport(k=5, recall_at_k=recall_at_k, mrr=mrr, per_query=(), per_kind={})


class TestRecallAtK:
    def test_no_hit_is_zero(self) -> None:
        assert recall_at_k(["a", "b"], ["c"], k=2) == 0.0

    def test_full_hit_is_one(self) -> None:
        assert recall_at_k(["a", "b"], ["a", "b"], k=2) == 1.0

    def test_partial_hit_of_multiple_expected(self) -> None:
        assert recall_at_k(["a", "x", "y"], ["a", "b"], k=3) == pytest.approx(0.5)

    def test_only_counts_the_first_k_results(self) -> None:
        assert recall_at_k(["x", "y", "a"], ["a"], k=2) == 0.0

    def test_no_expected_ids_is_zero(self) -> None:
        assert recall_at_k(["a"], [], k=5) == 0.0


class TestReciprocalRank:
    def test_hit_at_rank_one(self) -> None:
        assert reciprocal_rank(["a", "b"], ["a"]) == pytest.approx(1.0)

    def test_hit_at_rank_three(self) -> None:
        assert reciprocal_rank(["x", "y", "a"], ["a"]) == pytest.approx(1 / 3)

    def test_no_hit_is_zero(self) -> None:
        assert reciprocal_rank(["x", "y"], ["a"]) == 0.0

    def test_first_of_several_expected_counts(self) -> None:
        assert reciprocal_rank(["x", "b", "a"], ["a", "b"]) == pytest.approx(1 / 2)


class TestCompare:
    def test_no_regression_when_equal_to_baseline(self) -> None:
        report = _report(0.9, 0.8)
        baseline = {"recall_at_k": 0.9, "mrr": 0.8}
        assert compare(report, baseline) == []

    def test_no_regression_when_improved(self) -> None:
        report = _report(0.95, 0.85)
        baseline = {"recall_at_k": 0.9, "mrr": 0.8}
        assert compare(report, baseline) == []

    def test_recall_drop_is_reported(self) -> None:
        report = _report(0.8, 0.8)
        baseline = {"recall_at_k": 0.9, "mrr": 0.8}
        regressions = compare(report, baseline)
        assert len(regressions) == 1
        assert "recall_at_k" in regressions[0]

    def test_mrr_drop_is_reported(self) -> None:
        report = _report(0.9, 0.7)
        baseline = {"recall_at_k": 0.9, "mrr": 0.8}
        regressions = compare(report, baseline)
        assert len(regressions) == 1
        assert "mrr" in regressions[0]

    def test_both_metrics_dropping_reports_both(self) -> None:
        report = _report(0.5, 0.5)
        baseline = {"recall_at_k": 0.9, "mrr": 0.8}
        assert len(compare(report, baseline)) == 2

    def test_tolerance_absorbs_a_small_drop(self) -> None:
        report = _report(0.89, 0.8)
        baseline = {"recall_at_k": 0.9, "mrr": 0.8}
        assert compare(report, baseline, tolerance=0.02) == []

    def test_drop_beyond_tolerance_is_still_reported(self) -> None:
        report = _report(0.85, 0.8)
        baseline = {"recall_at_k": 0.9, "mrr": 0.8}
        assert len(compare(report, baseline, tolerance=0.02)) == 1

    def test_metric_missing_from_baseline_is_ignored(self) -> None:
        report = _report(0.1, 0.1)
        assert compare(report, {}) == []
