"""Statistics for the evaluation report (spec G5). Pure functions with no dependencies beyond the stdlib."""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

MIN_CLUSTERS = 5  # clustering by repository needs at least this many repositories to say anything


@dataclass(frozen=True)
class Interval:
    estimate: float
    low: float
    high: float

    def __str__(self) -> str:
        return f"{self.estimate:.1%} [{self.low:.1%}, {self.high:.1%}]"


def mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def percentile(xs: Sequence[float], q: float) -> float:
    """Linear-interpolation percentile, ``q`` in [0, 1]."""
    if not xs:
        return 0.0
    s = sorted(xs)
    pos = q * (len(s) - 1)
    lo, hi = math.floor(pos), math.ceil(pos)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def wilson(successes: int, n: int, z: float = 1.959964) -> Interval:
    """Wilson score interval: well behaved near 0 and 1 and for small ``n`` (unlike the normal approximation)."""
    if n == 0:
        return Interval(0.0, 0.0, 0.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return Interval(p, max(0.0, centre - half), min(1.0, centre + half))


def per_task_rates(outcomes: Iterable[tuple[str, bool]]) -> dict[str, float]:
    """``(task_id, resolved)`` for every run -> resolve rate per task (mean over its ``k`` runs)."""
    by_task: dict[str, list[bool]] = defaultdict(list)
    for task, ok in outcomes:
        by_task[task].append(ok)
    return {t: sum(v) / len(v) for t, v in by_task.items()}


def bootstrap_ci(
    rates: dict[str, float],
    cluster_of: dict[str, str] | None = None,
    *,
    resamples: int = 10_000,
    alpha: float = 0.05,
    seed: int = 1234,
    min_clusters: int = MIN_CLUSTERS,
) -> Interval:
    """Task-level bootstrap of the mean resolve rate, clustered by repo when ``cluster_of`` is given.

    Tasks from the same repository are not independent, so whole clusters are resampled (with replacement), and
    the tasks inside a drawn cluster come along. With fewer than ``min_clusters`` clusters that scheme is
    meaningless (a single cluster would give a zero-width interval, i.e. false certainty), so tasks are resampled
    individually instead. Deterministic for a fixed ``seed``.
    """
    if not rates:
        return Interval(0.0, 0.0, 0.0)
    point = mean(list(rates.values()))
    if cluster_of is not None and len({cluster_of.get(t, t) for t in rates}) < min_clusters:
        cluster_of = None
    clusters: dict[str, list[float]] = defaultdict(list)
    for task, rate in rates.items():
        clusters[(cluster_of or {}).get(task, task)].append(rate)
    groups = list(clusters.values())
    rng = random.Random(seed)
    stats: list[float] = []
    for _ in range(resamples):
        drawn = [groups[rng.randrange(len(groups))] for _ in groups]
        flat = [r for g in drawn for r in g]
        stats.append(mean(flat))
    return Interval(point, percentile(stats, alpha / 2), percentile(stats, 1 - alpha / 2))


def pass_at_k(per_task_runs: dict[str, list[bool]]) -> float:
    """Fraction of tasks solved in at least one of their runs."""
    if not per_task_runs:
        return 0.0
    return sum(1 for runs in per_task_runs.values() if any(runs)) / len(per_task_runs)


def mcnemar_exact(a_only: int, b_only: int) -> float:
    """Two-sided exact McNemar p-value from the discordant counts (A solved / B did not, and the reverse)."""
    n = a_only + b_only
    if n == 0:
        return 1.0
    k = min(a_only, b_only)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / (1 << n)  # exact integer arithmetic, no float overflow
    return min(1.0, 2 * tail)


def paired_comparison(a: dict[str, bool], b: dict[str, bool]) -> tuple[int, int, float]:
    """``(A-only wins, B-only wins, p-value)`` over the tasks both configurations attempted (task-level success)."""
    common = sorted(set(a) & set(b))
    a_only = sum(1 for t in common if a[t] and not b[t])
    b_only = sum(1 for t in common if b[t] and not a[t])
    return a_only, b_only, mcnemar_exact(a_only, b_only)


@dataclass(frozen=True)
class CalibrationRow:
    level: str
    runs: int
    resolved: int
    interval: Interval


def calibration(
    rows: Iterable[tuple[str, bool]], levels: Sequence[str] = ("high", "medium", "low", "none")
) -> list[CalibrationRow]:
    """``(confidence level, resolved)`` per run -> how often each stated confidence level was right.

    This is the headline credibility chart: "when Fixling says HIGH, it is right X% of the time".
    """
    counts: dict[str, list[bool]] = defaultdict(list)
    for level, ok in rows:
        counts[level].append(ok)
    out = []
    for level in levels:
        runs = counts.get(level, [])
        out.append(CalibrationRow(level, len(runs), sum(runs), wilson(sum(runs), len(runs))))
    return out


def false_pass_rate(rows: Iterable[tuple[bool, bool]]) -> Interval:
    """``(claimed verified, actually resolved)`` per run -> share of verified claims that the grader rejected."""
    claimed = [resolved for verified, resolved in rows if verified]
    wrong = sum(1 for ok in claimed if not ok)
    return wilson(wrong, len(claimed))
