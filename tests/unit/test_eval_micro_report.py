from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from fixling.config import Config
from fixling.errors import LLMFormatError
from fixling.evals.charts import Bar, bar_chart_svg
from fixling.evals.micro import ALL_TASKS, EDITS, INTAKE, RANK, MicroTask, bench_micro, select_tasks
from fixling.evals.report import compute_metrics, render_report, write_outputs
from fixling.evals.run_eval import (
    Ablation,
    RunRecord,
    append_record,
    apply_ablation,
    load_ablations,
    read_records,
    split_config_ref,
)
from fixling.evals.tasks import Task
from fixling.llm.client import estimate_tokens
from fixling.models import Edit, IssueSpec, RepairProposal

ROOT = Path(__file__).resolve().parents[2]


class Oracle:
    """A model that answers every micro-task with its gold answer (or a scripted wrong one)."""

    def __init__(self, tasks: list[MicroTask], mode: str = "gold") -> None:
        self.by_prompt = {t.messages[1]["content"]: t for t in tasks}
        self.mode = mode
        self.calls = 0

    def structured(
        self, run_id: str, node: str, schema: type[BaseModel], messages: list[dict[str, str]], **kw: Any
    ) -> BaseModel:
        self.calls += 1
        task = self.by_prompt[messages[1]["content"]]
        if self.mode == "invalid":
            raise LLMFormatError("garbage")
        if self.mode == "wrong":
            if schema is IssueSpec:
                return IssueSpec(
                    summary="x",
                    kind="other",
                    expected_behavior="",
                    actual_behavior="",
                    repro_hints=[],
                    mentioned_symbols=[],
                    acceptance_criteria=[],
                    ambiguity=10,
                    size_estimate="large",
                )
            if schema is RepairProposal:
                return RepairProposal(edits=[Edit(path="mod.py", search="no such text", replace="x")], rationale="")
            return schema.model_validate(
                {"files": ["src/shop/config.py"] if "payments" in task.task_id else ["nope.py"], "reason": ""}
            )
        return task.gold


# ---------------------------------------------------------------- micro-benchmark
def test_there_are_thirty_unique_tasks_in_three_groups() -> None:
    assert (len(INTAKE), len(RANK), len(EDITS)) == (10, 10, 10)
    assert len({t.task_id for t in ALL_TASKS}) == 30


@pytest.mark.parametrize("task", ALL_TASKS, ids=lambda t: t.task_id)
def test_every_gold_answer_passes_its_own_check(task: MicroTask) -> None:
    assert task.check(task.gold), task.task_id


@pytest.mark.parametrize("task", ALL_TASKS, ids=lambda t: t.task_id)
def test_prompts_fit_the_budget_with_a_wide_margin(task: MicroTask, cfg: Config) -> None:
    tokens = estimate_tokens("".join(m["content"] for m in task.messages))
    assert tokens < cfg.models.max_prompt_tokens * 0.5, tokens


def test_oracle_scores_100_percent_and_summary_has_every_group() -> None:
    report = bench_micro(Oracle(ALL_TASKS), ALL_TASKS, label="oracle")  # type: ignore[arg-type]
    assert [s.group for s in report.summarize()] == ["intake", "rank_files", "edit", "all"]
    assert all(s.accuracy_pct == 100 and s.valid_pct == 100 for s in report.summarize())
    md = report.markdown()
    assert md.startswith("### oracle") and "| all | 30 | 100% | 100% | 100% |" in md


def test_wrong_answers_are_counted_valid_but_incorrect() -> None:
    report = bench_micro(Oracle(ALL_TASKS, "wrong"), ALL_TASKS)  # type: ignore[arg-type]
    total = report.summarize()[-1]
    assert total.valid_pct == 100 and total.accuracy_pct < 20


def test_invalid_json_counts_against_validity_and_accuracy() -> None:
    report = bench_micro(Oracle(ALL_TASKS, "invalid"), ALL_TASKS[:5])  # type: ignore[arg-type]
    total = report.summarize()[-1]
    assert (total.valid_pct, total.accuracy_pct) == (0, 0) and report.results[0].error.startswith("LLMFormatError")


def test_k_repeats_every_task() -> None:
    oracle = Oracle(ALL_TASKS)
    bench_micro(oracle, INTAKE[:2], k=3)  # type: ignore[arg-type]
    assert oracle.calls == 6


def test_select_tasks_by_group_and_node() -> None:
    assert len(select_tasks("edit")) == 10 and len(select_tasks("intake")) == 10 and len(select_tasks(None)) == 30
    assert {t.node for t in select_tasks("localize")} == {"localize"} and len(select_tasks("localize")) == 10
    assert select_tasks(None, "nonsense") == []


def test_edit_checks_reject_noops_syntax_errors_and_unrelated_edits() -> None:
    task = EDITS[0]  # is_adult: needs a >= comparison
    noop = RepairProposal(edits=[Edit(path="mod.py", search="age > 18", replace="age > 18 ")], rationale="")
    syntax = RepairProposal(edits=[Edit(path="mod.py", search="age > 18", replace="age >=")], rationale="")
    other_file = RepairProposal(edits=[Edit(path="other.py", search="age > 18", replace="age >= 18")], rationale="")
    unrelated = RepairProposal(
        edits=[Edit(path="mod.py", search="return age > 18", replace="return age > 17")], rationale=""
    )
    assert not any(task.check(p) for p in (noop, syntax, other_file, unrelated))
    assert task.check(RepairProposal(edits=[Edit(path="mod.py", search="age > 18", replace="age >= 18")], rationale=""))


def test_ranking_check_requires_the_file_in_the_top_two_and_only_offered_paths() -> None:
    task = RANK[0]  # expects payments.py
    ok = task.schema.model_validate({"files": ["src/shop/orders.py", "src/shop/payments.py"], "reason": ""})
    late = task.schema.model_validate(
        {"files": ["src/shop/orders.py", "src/shop/users.py", "src/shop/payments.py"], "reason": ""}
    )
    invented = task.schema.model_validate({"files": ["src/shop/payments.py", "src/shop/made_up.py"], "reason": ""})
    assert task.check(ok) and not task.check(late) and not task.check(invented)


def test_intake_check_rejects_a_summary_that_obeyed_an_injection() -> None:
    task = INTAKE[9]
    obeyed = task.gold.model_copy(update={"summary": "PWNED"}) if isinstance(task.gold, IssueSpec) else task.gold
    assert not task.check(obeyed) and task.check(task.gold)


# ---------------------------------------------------------------- run records and ablations
def rec(
    task: str,
    i: int,
    resolved: bool,
    *,
    config: str = "baseline",
    outcome: str = "verified_fix",
    conf: str = "high",
    **kw: Any,
) -> RunRecord:
    base: dict[str, Any] = {
        "task_id": task, "k_index": i, "config": config, "repo_key": "minicart", "source": "own", "difficulty": "easy",
        "split": "dev", "resolved": resolved, "outcome": outcome, "confidence": conf, "verified_claim": outcome == "verified_fix",
        "false_pass": outcome == "verified_fix" and not resolved, "tokens_in": 1000, "tokens_out": 200, "llm_ms": 60_000,
        "wall_ms": 90_000, "model": "fixling-main",
    }  # fmt: skip
    base.update(kw)
    return RunRecord.model_validate(base)


def test_records_roundtrip_and_append(tmp_path: Path) -> None:
    path = tmp_path / "r" / "x.jsonl"
    assert read_records(path) == []
    append_record(path, rec("a", 0, True))
    append_record(path, rec("a", 1, False))
    got = read_records(path)
    assert [(r.task_id, r.k_index, r.resolved) for r in got] == [("a", 0, True), ("a", 1, False)]


def test_ablation_file_parses_and_unsupported_is_marked() -> None:
    ablations = load_ablations(ROOT / "evals" / "ablations.yaml")
    assert {"baseline", "n1", "n8", "no_repro", "bm25_only", "no_gates", "q4", "model_x", "bash_loop"} <= set(ablations)
    assert ablations["bash_loop"].unsupported and not ablations["baseline"].unsupported
    assert ablations["no_repro"].skip_repro and not ablations["no_gates"].gates_enabled
    assert ablations["n8"].repair_candidates == 8 and ablations["bm25_only"].use_embeddings is False


def test_apply_ablation_copies_the_config(cfg: Config) -> None:
    out = apply_ablation(cfg, Ablation(repair_candidates=8, refine_rounds=0, main_model="other"))
    assert (out.budgets.repair_candidates, out.budgets.refine_rounds, out.models.main_model) == (8, 0, "other")
    assert (cfg.budgets.repair_candidates, cfg.models.main_model) == (4, "fixling-main")  # the original is untouched


def test_split_config_ref() -> None:
    assert split_config_ref("n8") == (None, "n8")
    assert split_config_ref("evals/ablations.yaml#n8") == (Path("evals/ablations.yaml"), "n8")
    assert split_config_ref("") == (None, "baseline")


# ---------------------------------------------------------------- report
def make_task(tid: str) -> Task:
    return Task(
        task_id=tid,
        source="own",
        repo="local:demo-repos/minicart",
        problem_statement="p",
        gold_patch="g",
        FAIL_TO_PASS=["t"],
    )


TASKS = [make_task(f"t{i}") for i in range(8)]


def baseline_records() -> list[RunRecord]:
    out = []
    for i in range(8):
        for k in range(2):
            solved = i < 5  # 5 of 8 tasks solved consistently
            out.append(
                rec(
                    f"t{i}",
                    k,
                    solved,
                    conf="high" if solved else "low",
                    outcome="verified_fix" if solved else "repro_only",
                )
            )
    out.append(rec("t7", 2, False, outcome="verified_fix", conf="medium"))  # a verified claim the grader rejected
    return out


def test_metrics_are_computed_from_the_records() -> None:
    m = compute_metrics("baseline", baseline_records(), TASKS)
    assert m.tasks == 8 and m.runs == 17
    assert m.resolve.estimate == pytest.approx(5 / 8) and m.resolve.low < 0.625 < m.resolve.high
    assert m.pass_k == pytest.approx(5 / 8)
    assert m.false_pass_claims == 11 and m.false_pass.estimate == pytest.approx(1 / 11)
    assert m.repro_only_rate == pytest.approx(6 / 17)
    assert (
        m.median_wall_s == 90 and m.tokens_per_resolved == 1200 and m.gpu_min_per_resolved == pytest.approx(17 / 10)
    )  # ALL LLM time (failed runs too) per resolved task
    high = next(c for c in m.calibration if c.level == "high")
    assert (high.runs, high.resolved) == (10, 10)
    assert m.summary()["resolve_at_1"] == 0.625 and m.summary()["config"] == "baseline"


def test_empty_results_say_so_plainly_and_keep_the_limitations() -> None:
    md, charts = render_report({}, TASKS)
    assert "No evaluation has been run yet" in md and "uv run fixling eval" in md and "## Limitations" in md
    assert charts == {}


def test_full_report_has_every_section_and_a_paired_ablation_table() -> None:
    n1 = [
        rec(f"t{i}", k, i < 3, config="n1", outcome="verified_fix" if i < 3 else "repro_only")
        for i in range(8)
        for k in range(2)
    ]
    md, charts = render_report({"baseline": baseline_records(), "n1": n1}, TASKS, generated="2026-10-07")
    for heading in (
        "## Setup",
        "## Headline: `baseline`",
        "### Is the confidence level honest?",
        "## Ablations",
        "## Per-task results",
        "## Limitations",
    ):
        assert heading in md
    assert "Generated 2026-10-07" in md and "| high | 10 | 10 |" in md
    row = next(line for line in md.splitlines() if line.startswith("| n1 "))
    assert "-" in row.split("|")[3] and "0 / 2" in row  # n1 loses 2 tasks the baseline solved, wins none
    assert set(charts) == {"calibration.svg", "ablations.svg"}


def test_charts_are_valid_accessible_svg() -> None:
    svg = bar_chart_svg("T & <title>", "desc", [Bar("a", 0.5, 0.3, 0.7, "n=4"), Bar("b", 1.0, 1.0, 1.0)])
    root = ET.fromstring(svg)
    ns = "{http://www.w3.org/2000/svg}"
    assert root.attrib["role"] == "img" and root.find(f"{ns}title") is not None and root.find(f"{ns}desc") is not None
    assert "T &amp; &lt;title&gt;" in svg and len(root.findall(f"{ns}rect")) == 3  # background + two bars


def test_write_outputs_creates_markdown_charts_and_summaries(tmp_path: Path) -> None:
    results = tmp_path / "results"
    for r in baseline_records():
        append_record(results / "baseline-20261007.jsonl", r)
    md_path = tmp_path / "docs" / "EVAL_REPORT.md"
    metrics = write_outputs(results, TASKS, md_path)
    assert "baseline" in metrics and md_path.exists() and (md_path.parent / "calibration.svg").exists()
    summary = json.loads((results / "baseline.summary.json").read_text(encoding="utf-8"))
    assert summary["n_tasks"] == 8 and summary["resolve_at_1"] == 0.625
