"""Evaluation report: metrics, comparisons and charts computed only from the recorded results (spec G5).

Nothing in the generated report is hand-written except the fixed limitations section; every number comes from the
``evals/results/*.jsonl`` files, so a reader can re-derive it.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fixling.evals.charts import Bar, bar_chart_svg
from fixling.evals.run_eval import RunRecord, read_records
from fixling.evals.stats import (
    MIN_CLUSTERS,
    CalibrationRow,
    Interval,
    bootstrap_ci,
    calibration,
    false_pass_rate,
    mean,
    paired_comparison,
    pass_at_k,
    per_task_rates,
    percentile,
    wilson,
)
from fixling.evals.tasks import Task

LIMITATIONS = """\
- **Small, synthetic-leaning task set.** The tasks are bugs in one small demo library (`minicart`): 10 hand-written
  and 10 synthetic AST mutations. Resolve rates here say how the system behaves on well-scoped single-function bugs;
  they do **not** predict performance on arbitrary real-world repositories.
- **No public-benchmark numbers yet.** SWE-bench Verified Mini and SWE-bench-Live slices are part of the plan but have
  not been run, so nothing here is comparable to published SWE-bench scores.
- **Statistical power is low.** With about 20 tasks the confidence intervals are wide; differences between
  ablations are only reported as significant when an exact McNemar test says so.
- **The grader is the same kind of artefact as the agent's tests.** Hidden tests were written by the author of the
  bugs. A fix that passes them is correct for the behaviours they check, not necessarily for every behaviour.
- **One machine, one model family.** Timings depend on the local GPU and on LM Studio's batching.\
"""


@dataclass
class ConfigMetrics:
    config: str
    runs: int
    tasks: int
    k: int
    resolve: Interval
    pass_k: float
    false_pass: Interval
    false_pass_claims: int
    repro_only_rate: float
    median_wall_s: float
    p90_wall_s: float
    tokens_per_resolved: float | None
    gpu_min_per_resolved: float | None
    calibration: list[CalibrationRow] = field(default_factory=list)
    task_success: dict[str, bool] = field(default_factory=dict)  # majority of k runs resolved
    clustered: bool = False  # the bootstrap resampled whole repositories (needs >= 5 of them)

    def summary(self) -> dict[str, Any]:
        """Flat JSON for the dashboard's Evals page."""
        return {
            "config": self.config,
            "n_tasks": self.tasks,
            "k": self.k,
            "resolve_at_1": round(self.resolve.estimate, 4),
            "ci_low": round(self.resolve.low, 4),
            "ci_high": round(self.resolve.high, 4),
            "pass_at_k": round(self.pass_k, 4),
            "false_pass_rate": round(self.false_pass.estimate, 4),
            "repro_only_rate": round(self.repro_only_rate, 4),
            "median_wall_s": round(self.median_wall_s, 1),
            "tokens_per_resolved": None if self.tokens_per_resolved is None else round(self.tokens_per_resolved),
        }


def compute_metrics(config: str, records: list[RunRecord], tasks: list[Task]) -> ConfigMetrics:
    repo_of = {t.task_id: t.repo_key for t in tasks}
    runs_by_task: dict[str, list[bool]] = defaultdict(list)
    for r in records:
        runs_by_task[r.task_id].append(r.resolved)
    rates = per_task_rates((r.task_id, r.resolved) for r in records)
    resolved_runs = [r for r in records if r.resolved]
    walls = [r.wall_ms / 1000 for r in records]
    tokens_total = sum(r.tokens_in + r.tokens_out for r in resolved_runs)
    return ConfigMetrics(
        config=config,
        runs=len(records),
        tasks=len(runs_by_task),
        k=max((len(v) for v in runs_by_task.values()), default=0),
        resolve=bootstrap_ci(rates, repo_of),
        pass_k=pass_at_k(dict(runs_by_task)),
        false_pass=false_pass_rate((r.verified_claim, r.resolved) for r in records),
        false_pass_claims=sum(1 for r in records if r.verified_claim),
        repro_only_rate=mean([1.0 if r.outcome == "repro_only" else 0.0 for r in records]),
        median_wall_s=percentile(walls, 0.5),
        p90_wall_s=percentile(walls, 0.9),
        tokens_per_resolved=tokens_total / len(resolved_runs) if resolved_runs else None,
        gpu_min_per_resolved=(sum(r.llm_ms for r in records) / 60000) / len(resolved_runs) if resolved_runs else None,
        calibration=calibration((r.confidence or "none", r.resolved) for r in records),
        task_success={t: rate >= 0.5 for t, rate in rates.items()},
        clustered=len({repo_of.get(t, t) for t in rates}) >= MIN_CLUSTERS,
    )


def _pct(iv: Interval) -> str:
    return str(iv)


def _num(v: float | None, fmt: str = "{:.0f}") -> str:
    return "n/a" if v is None else fmt.format(v)


def render_report(
    by_config: dict[str, list[RunRecord]],
    tasks: list[Task],
    *,
    baseline: str = "baseline",
    generated: str | None = None,
) -> tuple[str, dict[str, str]]:
    """Markdown report plus ``{filename: svg}`` charts."""
    stamp = generated or datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Fixling evaluation report",
        "",
        f"_Generated {stamp} from `evals/results/*.jsonl` by `fixling eval-report`. All numbers are computed, none are typed in._",
        "",
    ]
    if not any(by_config.values()):
        lines += [
            "**No evaluation has been run yet.** Load a model in LM Studio, then:",
            "",
            "```",
            "uv run fixling eval --slice dev --k 1        # smoke test on the 10 dev tasks",
            "uv run fixling eval --slice test --k 3       # held-out baseline",
            "uv run fixling eval-report                   # regenerate this file",
            "```",
            "",
            "## Limitations",
            "",
            LIMITATIONS,
            "",
        ]
        return "\n".join(lines), {}

    metrics = {name: compute_metrics(name, recs, tasks) for name, recs in by_config.items() if recs}
    charts: dict[str, str] = {}
    base = metrics.get(baseline) or next(iter(metrics.values()))
    models = sorted({r.model for recs in by_config.values() for r in recs if r.model})
    lines += [
        "## Setup",
        "",
        f"- Models: {', '.join(f'`{m}`' for m in models) or 'n/a'}",
        f"- Tasks scored: {base.tasks} (k = {base.k} runs each); sources: "
        + ", ".join(f"{s} ({n})" for s, n in sorted(_count(tasks, "source").items())),
        "- Resolved = every FAIL_TO_PASS test passes and no PASS_TO_PASS test fails, graded on the final patch in a clean container against hidden tests.",
        "",
        f"## Headline: `{base.config}`",
        "",
        "| metric | value |",
        "|---|---|",
        f"| resolve@1 ({'bootstrap clustered by repository' if base.clustered else 'task-level bootstrap; fewer than 5 repositories, so not clustered'}) | **{_pct(base.resolve)}** |",
        f"| pass@{base.k} | {base.pass_k:.1%} |",
        f"| false-pass rate (verified claims the grader rejected) | {_pct(base.false_pass)} over {base.false_pass_claims} claims |",
        f"| repro-only rate | {base.repro_only_rate:.1%} |",
        f"| median / p90 wall time per run | {base.median_wall_s:.0f}s / {base.p90_wall_s:.0f}s |",
        f"| tokens per resolved task | {_num(base.tokens_per_resolved)} |",
        f"| LLM-minutes per resolved task | {_num(base.gpu_min_per_resolved, '{:.1f}')} |",
        "",
        "### Is the confidence level honest?",
        "",
        "When Fixling says a level, how often is the fix actually correct?",
        "",
        "| stated confidence | runs | resolved | correct (95% Wilson) |",
        "|---|---|---|---|",
    ]
    for row in base.calibration:
        lines.append(f"| {row.level} | {row.runs} | {row.resolved} | {_pct(row.interval) if row.runs else 'n/a'} |")
    charts["calibration.svg"] = bar_chart_svg(
        "Correct rate by stated confidence",
        "How often a fix was actually correct, grouped by the confidence level Fixling reported.",
        [
            Bar(r.level, r.interval.estimate, r.interval.low, r.interval.high, f"n={r.runs}")
            for r in base.calibration
            if r.runs
        ],
    )
    lines += ["", "![Correct rate by stated confidence](calibration.svg)", ""]

    if len(metrics) > 1:
        lines += [
            "## Ablations",
            "",
            f"Each row changes one thing relative to `{base.config}`. Paired over tasks both ran; p is an exact McNemar test.",
            "",
        ]
        lines += [
            "| configuration | resolve@1 | delta vs baseline | wins / losses | p | false-pass | median wall |",
            "|---|---|---|---|---|---|---|",
        ]
        bars = []
        for name, m in metrics.items():
            if name == base.config:
                lines.append(
                    f"| **{name}** | {_pct(m.resolve)} | - | - | - | {m.false_pass.estimate:.1%} | {m.median_wall_s:.0f}s |"
                )
            else:
                wins, losses, p = paired_comparison(m.task_success, base.task_success)
                delta = m.resolve.estimate - base.resolve.estimate
                sig = " *" if p < 0.05 else ""
                lines.append(
                    f"| {name} | {_pct(m.resolve)} | {delta:+.1%} | {wins} / {losses} | {p:.3f}{sig} | {m.false_pass.estimate:.1%} | {m.median_wall_s:.0f}s |"
                )
            bars.append(Bar(name, m.resolve.estimate, m.resolve.low, m.resolve.high, f"n={m.tasks}"))
        lines += [
            "",
            "`*` = p < 0.05. With few tasks most differences will not reach it; that is a finding, not a failure.",
            "",
        ]
        charts["ablations.svg"] = bar_chart_svg(
            "resolve@1 by configuration", "Resolve rate with 95% bootstrap intervals for each configuration.", bars
        )
        lines += ["![resolve@1 by configuration](ablations.svg)", ""]

    lines += [
        "## Per-task results (baseline)",
        "",
        "| task | difficulty | split | resolved / runs | outcomes |",
        "|---|---|---|---|---|",
    ]
    by_task: dict[str, list[RunRecord]] = defaultdict(list)
    for r in by_config.get(base.config, []):
        by_task[r.task_id].append(r)
    for tid in sorted(by_task):
        rs = by_task[tid]
        outcomes = ", ".join(sorted({r.outcome or "error" for r in rs}))
        lines.append(
            f"| `{tid}` | {rs[0].difficulty} | {rs[0].split} | {sum(r.resolved for r in rs)} / {len(rs)} | {outcomes} |"
        )
    lines += ["", "## Limitations", "", LIMITATIONS, ""]
    return "\n".join(lines), charts


def _count(tasks: list[Task], attr: str) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for t in tasks:
        out[str(getattr(t, attr))] += 1
    return dict(out)


def write_outputs(
    results_dir: Path, tasks: list[Task], out_md: Path, *, baseline: str = "baseline"
) -> dict[str, ConfigMetrics]:
    """Read every results file, write the markdown report, its charts and one ``*.summary.json`` per configuration."""
    by_config: dict[str, list[RunRecord]] = defaultdict(list)
    for path in sorted(Path(results_dir).glob("*.jsonl")):
        for rec in read_records(path):
            by_config[rec.config].append(rec)
    markdown, charts = render_report(dict(by_config), tasks, baseline=baseline)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(markdown, encoding="utf-8", newline="")
    for name, svg in charts.items():
        (out_md.parent / name).write_text(svg, encoding="utf-8", newline="")
    metrics = {name: compute_metrics(name, recs, tasks) for name, recs in by_config.items() if recs}
    for name, m in metrics.items():
        (Path(results_dir) / f"{name}.summary.json").write_text(
            json.dumps(m.summary(), indent=2), encoding="utf-8", newline=""
        )
    return metrics


__all__ = ["ConfigMetrics", "compute_metrics", "render_report", "wilson", "write_outputs"]
