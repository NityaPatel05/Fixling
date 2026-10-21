"""Evaluation tasks through the real grading path (local sandboxes, no Docker, no LM Studio)."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from fixling.config import Config
from fixling.evals.run_eval import EvalContext, read_records, run_eval
from fixling.evals.tasks import Task, check_task, grade, load_tasks, materialize, select
from fixling.models import Edit, FileRanking, IssueSpec, RepairProposal, ReproTest, TargetPick
from fixling.services import build_services
from tests.fakes import FakeLLM, LocalProvider

ROOT = Path(__file__).resolve().parents[2]
TASKS = load_tasks(ROOT / "evals" / "tasks" / "tasks.jsonl")
BY_ID = {t.task_id: t for t in TASKS}


def test_the_task_file_is_well_formed() -> None:
    assert len(TASKS) == 20 and len({t.task_id for t in TASKS}) == 20
    assert Counter((t.source, t.split) for t in TASKS) == {
        ("own", "dev"): 5,
        ("own", "test"): 5,
        ("synthetic", "dev"): 5,
        ("synthetic", "test"): 5,
    }
    assert len(select(TASKS, "dev")) == 10 and len(select(TASKS, "test")) == 10
    assert [t.task_id for t in select(TASKS, "all", ["minicart-update_qty_zero"])] == ["minicart-update_qty_zero"]
    for t in TASKS:
        assert t.FAIL_TO_PASS and t.PASS_TO_PASS and t.gold_patch.strip() and t.bug_patch.strip()
        assert "gold" not in t.problem_statement.lower() and "diff --git" not in t.problem_statement


def test_the_agent_never_sees_gold_or_hidden_material() -> None:
    from fixling.evals.tasks import task_to_issue_text

    for t in TASKS:
        title, body = task_to_issue_text(t)
        shown = title + body
        assert t.gold_patch not in shown
        assert not t.test_patch or t.test_patch not in shown  # synthetic tasks have no hidden test patch
        assert "tests/hidden" not in shown, "hidden test paths must not leak into the issue"


@pytest.mark.parametrize("task", TASKS, ids=lambda t: t.task_id)
def test_every_task_is_valid_in_the_grading_path(task: Task, cfg: Config, tmp_path: Path) -> None:
    """The bug is real (an empty patch does not resolve it) and the gold patch resolves it, in a clean sandbox."""
    services = build_services(cfg)
    provider = LocalProvider(tmp_path / "sandboxes", cfg, services.events, services.runs)
    check = check_task(task, ROOT, provider, cfg.budgets, tmp_path / "scratch")
    assert check.base_fails, f"{task.task_id}: the buggy base already resolves the task ({check.detail})"
    assert check.gold_resolves, f"{task.task_id}: the gold patch does not resolve it ({check.detail})"


def test_grading_rejects_a_patch_that_does_not_apply_and_one_that_breaks_tests(cfg: Config, tmp_path: Path) -> None:
    task = BY_ID["minicart-free_shipping_threshold"]
    services = build_services(cfg)
    provider = LocalProvider(tmp_path / "sandboxes", cfg, services.events, services.runs)
    tree = materialize(task, tmp_path / "base", ROOT)
    broken = grade(
        task,
        tree,
        "diff --git a/nope.py b/nope.py\n--- a/nope.py\n+++ b/nope.py\n@@ -1 +1 @@\n-x\n+y\n",
        provider,
        "g1",
        cfg.budgets,
    )
    assert not broken.resolved and "does not apply" in broken.reason
    ok = grade(task, tree, task.gold_patch, provider, "g2", cfg.budgets)
    assert ok.resolved and ok.reason.startswith("all target tests pass")


# ---------------------------------------------------------------- the eval loop with a scripted model
def script_for(repro_code: str, edit: Edit) -> dict[str, Any]:
    spec = IssueSpec(
        summary="shipping is wrong", kind="bug", expected_behavior="e", actual_behavior="a", repro_hints=[],
        mentioned_symbols=["shipping_cost"], acceptance_criteria=[], ambiguity=1, size_estimate="small",
    )  # fmt: skip
    return {
        "intake": spec,
        "localize": [
            FileRanking(files=["src/minicart/shipping.py"], reason="r"),
            TargetPick(targets=["src/minicart/shipping.py::shipping_cost"], reason="r"),
        ],
        "reproduce": ReproTest(test_code=repro_code, explanation="e"),
        "repair": RepairProposal(edits=[edit], rationale="r"),
    }


def make_ctx(cfg: Config, tmp_path: Path, llm: FakeLLM) -> EvalContext:
    services = build_services(cfg)
    provider = LocalProvider(tmp_path / "sandboxes", cfg, services.events, services.runs)
    return EvalContext(
        cfg=cfg, services=services, provider=provider, llm=llm, project_root=ROOT, config_name="baseline"
    )


def test_run_eval_resolves_a_task_with_a_correct_fix_and_resumes(cfg: Config, tmp_path: Path) -> None:
    task = BY_ID["minicart-free_shipping_threshold"]
    repro = (
        "from decimal import Decimal\n\nfrom minicart.shipping import shipping_cost\n\n\n"
        "def test_exactly_75_ships_free():\n    assert shipping_cost(Decimal('2'), Decimal('75.00')) == Decimal('0.00')\n"
    )
    fix = Edit(
        path="src/minicart/shipping.py",
        search="    if subtotal > FREE_SHIPPING_FROM:",
        replace="    if subtotal >= FREE_SHIPPING_FROM:",
    )
    llm = FakeLLM(script=script_for(repro, fix))
    ctx = make_ctx(cfg, tmp_path, llm)
    results = tmp_path / "results" / "baseline.jsonl"
    seen: list[Any] = []

    records = run_eval([task], 1, ctx, results, on_record=seen.append)
    (rec,) = records
    assert rec.resolved and rec.outcome == "verified_fix" and rec.verified_claim and not rec.false_pass, rec
    assert (
        rec.candidates == 4 and rec.task_id == task.task_id and rec.config == "baseline" and rec.repo_key == "minicart"
    )
    assert rec.wall_ms > 0 and rec.error == "" and len(seen) == 1

    calls_before = len(llm.calls)
    again = run_eval([task], 1, ctx, results)  # resumes: the (task, k) pair is already recorded
    assert len(llm.calls) == calls_before and len(again) == 1 and len(read_records(results)) == 1


def test_a_verified_claim_the_hidden_tests_reject_is_counted_as_a_false_pass(cfg: Config, tmp_path: Path) -> None:
    task = BY_ID["minicart-shipping_band_boundary"]  # hidden tests: 1 kg, 5 kg and 20 kg band edges
    weak_repro = (
        "from decimal import Decimal\n\nfrom minicart.shipping import shipping_cost\n\n\n"
        "def test_one_kilo_is_the_first_band():\n    assert shipping_cost(Decimal('1'), Decimal('10.00')) == Decimal('4.99')\n"
    )
    partial = Edit(
        path="src/minicart/shipping.py",
        search="        if weight_kg < heaviest:",
        replace="        if weight_kg < heaviest or weight_kg == Decimal('1'):",
    )  # fixes the one case the (weak) reproduction test checks, nothing else
    ctx = make_ctx(cfg, tmp_path, FakeLLM(script=script_for(weak_repro, partial)))
    (rec,) = run_eval([task], 1, ctx, tmp_path / "r.jsonl")
    assert rec.outcome == "verified_fix" and rec.verified_claim, "the system believed its own weak evidence"
    assert not rec.resolved and rec.false_pass, rec
    assert "target tests still fail" in rec.grade_reason


def test_a_crashed_run_is_recorded_as_a_failure_not_skipped(cfg: Config, tmp_path: Path) -> None:
    task = BY_ID["minicart-free_shipping_threshold"].model_copy(update={"bug_patch": "this is not a patch"})
    ctx = make_ctx(cfg, tmp_path, FakeLLM())
    (rec,) = run_eval([task], 1, ctx, tmp_path / "r.jsonl")
    assert not rec.resolved and "bug_patch does not apply" in rec.error and rec.grade_reason == "the run crashed"


def test_unsupported_configurations_refuse_to_run(cfg: Config, tmp_path: Path) -> None:
    from fixling.evals.run_eval import Ablation

    ctx = make_ctx(cfg, tmp_path, FakeLLM())
    ctx.ablation = Ablation(unsupported="not built yet")
    with pytest.raises(NotImplementedError, match="not built yet"):
        run_eval([TASKS[0]], 1, ctx, tmp_path / "r.jsonl")
