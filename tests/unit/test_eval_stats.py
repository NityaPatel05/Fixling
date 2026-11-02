from __future__ import annotations

import math

import pytest

from fixling.evals.stats import (
    bootstrap_ci,
    calibration,
    false_pass_rate,
    mcnemar_exact,
    mean,
    paired_comparison,
    pass_at_k,
    per_task_rates,
    percentile,
    wilson,
)


def test_wilson_matches_reference_values() -> None:
    iv = wilson(8, 10)
    assert (
        iv.estimate == 0.8 and iv.low == pytest.approx(0.4902, abs=1e-3) and iv.high == pytest.approx(0.9433, abs=1e-3)
    )
    zero, full = wilson(0, 20), wilson(20, 20)
    assert zero.low == 0.0 and 0.0 < zero.high < 0.17  # never a degenerate [0, 0] interval
    assert full.high == 1.0 and 0.83 < full.low < 1.0
    assert wilson(0, 0).estimate == 0.0
    assert wilson(5, 10).low < 0.5 < wilson(5, 10).high


def test_wilson_interval_narrows_with_more_data() -> None:
    small, large = wilson(5, 10), wilson(50, 100)
    assert (large.high - large.low) < (small.high - small.low)


def test_percentile_and_mean() -> None:
    assert percentile([1, 2, 3, 4, 5], 0.5) == 3 and percentile([1, 2, 3, 4], 0.5) == 2.5
    assert percentile([], 0.5) == 0.0 and percentile([7], 0.9) == 7
    assert mean([]) == 0.0 and mean([1, 2, 3]) == 2.0


def test_per_task_rates_average_over_k() -> None:
    rates = per_task_rates([("a", True), ("a", False), ("a", True), ("b", False)])
    assert rates == {"a": pytest.approx(2 / 3), "b": 0.0}


def test_bootstrap_is_deterministic_and_contains_the_point_estimate() -> None:
    rates = {f"t{i}": float(i % 2) for i in range(30)}
    a = bootstrap_ci(rates, resamples=2000)
    b = bootstrap_ci(rates, resamples=2000)
    assert a == b and a.estimate == 0.5 and a.low < 0.5 < a.high
    assert bootstrap_ci(rates, resamples=2000, seed=99) != a


def test_bootstrap_degenerate_cases() -> None:
    assert bootstrap_ci({}) == bootstrap_ci({}, resamples=10)
    all_solved = bootstrap_ci({"a": 1.0, "b": 1.0, "c": 1.0}, resamples=500)
    assert (all_solved.estimate, all_solved.low, all_solved.high) == (1.0, 1.0, 1.0)


def test_clustering_by_repo_widens_the_interval() -> None:
    # ten repos (two tasks each): five where everything is solved and five where nothing is. Resampling whole
    # repos is noisier than resampling tasks because tasks within a repo move together.
    rates = {f"easy{i}": 1.0 for i in range(10)} | {f"hard{i}": 0.0 for i in range(10)}
    clusters = {t: f"{t[:4]}-repo{int(t[len(t[:4]) if t.startswith('easy') else 4 :]) // 2}" for t in rates}
    assert len(set(clusters.values())) == 10
    plain = bootstrap_ci(rates, resamples=3000)
    clustered = bootstrap_ci(rates, clusters, resamples=3000)
    assert (clustered.high - clustered.low) > (plain.high - plain.low) * 1.2


def test_too_few_repositories_fall_back_to_task_level_resampling() -> None:
    # One repository must NOT yield a zero-width interval (false certainty): clustering needs >= 5 clusters.
    rates = {f"t{i}": float(i % 2) for i in range(20)}
    one_repo = {t: "only-repo" for t in rates}
    four_repos = {t: f"repo{i % 4}" for i, t in enumerate(rates)}
    plain = bootstrap_ci(rates, resamples=2000)
    assert bootstrap_ci(rates, one_repo, resamples=2000) == plain
    assert bootstrap_ci(rates, four_repos, resamples=2000) == plain
    assert plain.low < plain.estimate < plain.high


def test_pass_at_k() -> None:
    assert pass_at_k({"a": [False, True, False], "b": [False, False, False], "c": [True, True, True]}) == pytest.approx(
        2 / 3
    )
    assert pass_at_k({}) == 0.0


def test_mcnemar_exact() -> None:
    assert mcnemar_exact(0, 0) == 1.0
    assert mcnemar_exact(5, 5) == 1.0
    assert mcnemar_exact(10, 0) == pytest.approx(2 * (1 / 1024))
    assert mcnemar_exact(9, 1) == pytest.approx(2 * (1 + 10) / 1024)
    assert mcnemar_exact(3, 7) == mcnemar_exact(7, 3)
    assert 0 <= mcnemar_exact(40, 25) <= 1


def test_paired_comparison_only_counts_common_tasks() -> None:
    a = {"t1": True, "t2": True, "t3": False, "t4": True, "only_a": True}
    b = {"t1": False, "t2": True, "t3": True, "t4": False, "only_b": True}
    wins_a, wins_b, p = paired_comparison(a, b)
    assert (wins_a, wins_b) == (2, 1) and p == pytest.approx(mcnemar_exact(2, 1))


def test_calibration_table_orders_levels_and_handles_empty_levels() -> None:
    rows = [("high", True)] * 9 + [("high", False)] + [("medium", True), ("medium", False)] + [("low", False)]
    table = calibration(rows)
    assert [r.level for r in table] == ["high", "medium", "low", "none"]
    high = table[0]
    assert (high.runs, high.resolved) == (10, 9) and high.interval.estimate == 0.9
    assert table[3].runs == 0 and table[3].interval.estimate == 0.0
    assert table[0].interval.estimate > table[1].interval.estimate > table[2].interval.estimate  # a calibrated system


def test_false_pass_rate_counts_only_verified_claims() -> None:
    rows = [(True, True), (True, True), (True, False), (False, False), (False, True)]
    iv = false_pass_rate(rows)
    assert iv.estimate == pytest.approx(1 / 3)
    assert false_pass_rate([(False, True)]).estimate == 0.0


def test_interval_formatting() -> None:
    assert str(wilson(1, 2)).startswith("50.0% [")
    assert math.isclose(wilson(1, 2).estimate, 0.5)
