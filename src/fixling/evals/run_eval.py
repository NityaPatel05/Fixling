"""Run the pipeline over evaluation tasks and grade the results (spec G4, G6).

Each (task, k) run: materialise the buggy base, run the real LangGraph pipeline with fixed budgets, then grade the
produced patch against the HIDDEN tests in a clean sandbox. One JSON line per run is appended to the results file,
so an interrupted evaluation resumes where it stopped.
"""

from __future__ import annotations

import contextlib
import json
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from fixling.config import Config
from fixling.evals.tasks import Task, grade, materialize, task_to_issue_text
from fixling.graph.deps import Deps, SandboxProvider
from fixling.graph.pipeline import Pipeline, make_checkpointer
from fixling.llm.client import LLMLike
from fixling.services import Services


# ---------------------------------------------------------------- ablation configurations
class Ablation(BaseModel):
    """One evaluation configuration. Every field left ``None`` keeps the default pipeline behaviour."""

    description: str = ""
    repair_candidates: int | None = None
    refine_rounds: int | None = None
    skip_repro: bool = False
    use_embeddings: bool | None = None
    gates_enabled: bool = True
    main_model: str | None = None
    unsupported: str | None = None  # configurations that exist in the plan but are not implemented yet


def load_ablations(path: Path) -> dict[str, Ablation]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return {name: Ablation.model_validate(body or {}) for name, body in raw.items()}


def split_config_ref(ref: str) -> tuple[Path | None, str]:
    """``evals/ablations.yaml#n8`` -> (path, "n8"); a bare name uses the default ablations file."""
    path, _, name = ref.partition("#")
    return (Path(path) if name else None), (name or path or "baseline")


# ---------------------------------------------------------------- records
class RunRecord(BaseModel):
    task_id: str
    k_index: int
    config: str
    source: str = ""
    difficulty: str = ""
    split: str = ""
    repo_key: str = ""
    run_id: str = ""
    resolved: bool = False
    grade_reason: str = ""
    outcome: str = ""
    confidence: str = ""
    verified_claim: bool = False  # the system said "verified_fix"
    false_pass: bool = False  # ... and the grader disagreed
    tokens_in: int = 0
    tokens_out: int = 0
    llm_ms: int = 0
    wall_ms: int = 0
    candidates: int = 0
    gate_blocks: int = 0
    stop_reason: str = ""
    model: str = ""
    error: str = ""
    finished_at: str = ""


def read_records(path: Path) -> list[RunRecord]:
    if not Path(path).exists():
        return []
    return [
        RunRecord.model_validate_json(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def append_record(path: Path, record: RunRecord) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("a", encoding="utf-8", newline="") as f:
        f.write(record.model_dump_json() + "\n")
        f.flush()


# ---------------------------------------------------------------- context
@dataclass
class EvalContext:
    """Everything one evaluation configuration needs. Built by ``build_context``; tests construct it directly."""

    cfg: Config
    services: Services
    provider: SandboxProvider
    llm: LLMLike
    project_root: Path
    config_name: str
    ablation: Ablation = field(default_factory=Ablation)
    pipeline_factory: Callable[[Deps], Pipeline] | None = None

    def deps(self) -> Deps:
        s = self.services
        deps = Deps(
            cfg=self.cfg,
            events=s.events,
            runs=s.runs,
            llm=self.llm,
            sandboxes=self.provider,
            budget=s.budget,
            use_embeddings=self.ablation.use_embeddings if self.ablation.use_embeddings is not None else True,
            skip_repro=self.ablation.skip_repro,
            gates_enabled=self.ablation.gates_enabled,
        )
        return deps

    def pipeline(self, deps: Deps) -> Pipeline:
        if self.pipeline_factory:
            return self.pipeline_factory(deps)
        return Pipeline(deps, make_checkpointer(self.cfg.checkpoint_path))


def apply_ablation(cfg: Config, ablation: Ablation) -> Config:
    """A deep copy of ``cfg`` with the ablation's budget and model overrides applied (the original is untouched)."""
    out = cfg.model_copy(deep=True)
    if ablation.repair_candidates is not None:
        out.budgets.repair_candidates = ablation.repair_candidates
    if ablation.refine_rounds is not None:
        out.budgets.refine_rounds = ablation.refine_rounds
    if ablation.main_model:
        out.models.main_model = ablation.main_model
    return out


# ---------------------------------------------------------------- one run
def run_task(task: Task, k_index: int, ctx: EvalContext) -> RunRecord:
    cfg, s = ctx.cfg, ctx.services
    record = RunRecord(
        task_id=task.task_id,
        k_index=k_index,
        config=ctx.config_name,
        source=task.source,
        difficulty=task.difficulty,
        split=task.split,
        repo_key=task.repo_key,
        model=cfg.models.main_model,
    )
    run_id = s.runs.create_run(repo=task.repo_key, model=cfg.models.main_model) or ""
    record.run_id = run_id
    started = time.monotonic()
    try:
        src = materialize(task, cfg.run_dir(run_id) / "src", ctx.project_root)
        title, body = task_to_issue_text(task)
        deps = ctx.deps()
        result = ctx.pipeline(deps).start(
            {
                "run_id": run_id,
                "repo": task.repo_key,
                "issue_number": None,
                "issue_title": title,
                "issue_body": body,
                "issue_author": "eval",
                "src_dir": str(src),
                "base_sha": "eval",
            }
        )
        outcome = result.get("outcome") or {}
        record.outcome = str(outcome.get("status", ""))
        record.confidence = str(outcome.get("confidence", ""))
        record.stop_reason = str(result.get("stop_reason") or "")
        patch_file = cfg.run_dir(run_id) / "patch.diff"
        patch = patch_file.read_text(encoding="utf-8") if patch_file.exists() else ""
        g = grade(task, src, patch, ctx.provider, run_id, cfg.budgets)
        record.resolved, record.grade_reason = g.resolved, g.reason
    except Exception as exc:  # a crashed run is a failed run, recorded, never a silent skip
        record.error = f"{type(exc).__name__}: {exc}"
        record.grade_reason = "the run crashed"
        s.events.append(
            run_id,
            "error",
            "system",
            {"kind": "eval", "error": record.error},
            node="eval",
            artifact_text=traceback.format_exc(),
        )
    finally:
        record.wall_ms = int((time.monotonic() - started) * 1000)
        with contextlib.suppress(Exception):  # cleanup must never mask the result
            ctx.provider.close_run(run_id)
    row = s.runs.get_run(run_id) or {}
    record.tokens_in, record.tokens_out, record.llm_ms = (
        int(row.get("tokens_in") or 0),
        int(row.get("tokens_out") or 0),
        int(row.get("llm_ms") or 0),
    )
    events = s.events.list(run_id)
    record.candidates = sum(1 for e in events if e.type == "candidate")
    record.gate_blocks = sum(1 for e in events if e.type == "gate_finding" and e.payload.get("severity") == "block")
    record.verified_claim = record.outcome == "verified_fix"
    record.false_pass = record.verified_claim and not record.resolved
    record.finished_at = datetime.now(UTC).isoformat()
    return record


def run_eval(
    tasks: list[Task],
    k: int,
    ctx: EvalContext,
    results_path: Path,
    *,
    on_record: Callable[[RunRecord], None] | None = None,
) -> list[RunRecord]:
    """Run every task ``k`` times (resuming from ``results_path``) and return all records for this configuration."""
    if ctx.ablation.unsupported:
        raise NotImplementedError(f"configuration {ctx.config_name!r} is not available yet: {ctx.ablation.unsupported}")
    done = {(r.task_id, r.k_index) for r in read_records(results_path) if r.config == ctx.config_name}
    for task in tasks:
        for i in range(k):
            if (task.task_id, i) in done:
                continue
            record = run_task(task, i, ctx)
            append_record(results_path, record)
            if on_record:
                on_record(record)
    return [r for r in read_records(results_path) if r.config == ctx.config_name]


def default_results_path(root: Path, config_name: str) -> Path:
    return root / "evals" / "results" / f"{config_name}-{datetime.now(UTC).strftime('%Y%m%d')}.jsonl"


def summary_row(rec: RunRecord) -> dict[str, Any]:
    row: dict[str, Any] = json.loads(rec.model_dump_json())
    return row
