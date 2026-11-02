"""Environment setup and baseline test runs (spec E7): the deterministic ``env`` node logic."""

from __future__ import annotations

import time
from pathlib import Path

import docker
from pydantic import BaseModel, Field

from fixling.config import BudgetsCfg
from fixling.errors import SandboxError
from fixling.events.store import EventStore
from fixling.models import TestRun
from fixling.repo.workspace import build_tree_tar
from fixling.runs import RunStore
from fixling.sandbox.base import Sandbox
from fixling.sandbox.docker_sandbox import DockerSandbox
from fixling.sandbox.images import ensure_env_image
from fixling.verify.policy import PolicyGate
from fixling.verify.runner import record_test_run, run_tests


class Baseline(BaseModel):
    """How the untouched repo behaves: the reference for 'did we break anything?'."""

    passed: list[str] = Field(default_factory=list)
    failed: list[str] = Field(default_factory=list)
    flaky: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    runs: int = 0
    collected: int = 0


class EnvResult(BaseModel):
    ok: bool
    reason: str = ""
    image: str = ""
    requirements: list[str] = Field(default_factory=list)
    baseline: Baseline | None = None
    sandbox_id: str | None = None


def environment_problem(runs: list[TestRun]) -> str | None:
    """Why the baseline cannot be trusted (broken install, collection errors, no tests), or ``None``."""
    for i, run in enumerate(runs, start=1):
        if not run.junit_ok:
            tail = run.output[-300:].strip().replace("\n", " | ")
            return f"baseline run {i} produced no valid junit report (exit code {run.exit_code}): {tail}"
        if run.collected == 0:
            return f"baseline run {i} collected no tests"
        collection = [t for t in run.errors if "::" not in t]
        if collection:
            detail = run.failure_details.get(collection[0], "")[:200].replace("\n", " ")
            return f"collection errors in baseline run {i}: {', '.join(collection[:3])}: {detail}"
    return None


def summarize_baseline(runs: list[TestRun]) -> Baseline:
    """passed in every run -> passed; failed/errored in every run -> failed; skipped always -> skipped; else flaky."""
    outcomes: dict[str, list[str]] = {}
    total = len(runs)
    for run in runs:
        for kind, ids in (("p", run.passed), ("f", [*run.failed, *run.errors]), ("s", run.skipped)):
            for t in ids:
                outcomes.setdefault(t, []).append(kind)
    base = Baseline(runs=total, collected=max((r.collected for r in runs), default=0))
    for test_id, seen in sorted(outcomes.items()):
        if len(seen) == total and len(set(seen)) == 1:
            {"p": base.passed, "f": base.failed, "s": base.skipped}[seen[0]].append(test_id)
        else:
            base.flaky.append(test_id)
    return base


def setup_env(
    *,
    client: docker.DockerClient,
    budgets: BudgetsCfg,
    events: EventStore,
    runs: RunStore,
    run_id: str,
    repo_key: str,
    src_dir: Path,
    gate: PolicyGate,
) -> tuple[EnvResult, DockerSandbox | None]:
    """Build the env image, start the work sandbox, load the tree and record the baseline.

    On success the returned sandbox stays open (the caller owns it); on failure it is closed.
    """
    started = time.monotonic()
    try:
        image, reqs = ensure_env_image(client, repo_key, src_dir)
    except SandboxError as exc:
        return _fail(events, run_id, f"environment image build failed: {exc}", started), None
    sandbox = DockerSandbox.create(client, image, run_id, "work", runs=runs)
    try:
        sandbox.put_tree(build_tree_tar(src_dir, gate))
        result = baseline_result(sandbox, budgets, events, run_id, image, reqs, started)
    except Exception:
        sandbox.close()
        raise
    if not result.ok:
        sandbox.close()
        return result, None
    return result, sandbox


def baseline_result(
    sandbox: Sandbox,
    budgets: BudgetsCfg,
    events: EventStore,
    run_id: str,
    image: str,
    reqs: list[str],
    started: float,
) -> EnvResult:
    """Run the untouched suite ``baseline_runs`` times and judge whether the environment is usable.

    Shared by every sandbox provider. Emits one ``test_run`` event per run and a final ``env`` event.
    """
    test_runs: list[TestRun] = []
    for i in range(budgets.baseline_runs):
        run = run_tests(sandbox, timeout=budgets.test_timeout_s)
        record_test_run(events, run_id, "env", run, f"baseline {i + 1}/{budgets.baseline_runs}")
        test_runs.append(run)
        if not run.junit_ok:
            break  # no point repeating a broken environment
    problem = environment_problem(test_runs)
    if problem:
        return _fail(events, run_id, problem, started, image=image, reqs=reqs)
    baseline = summarize_baseline(test_runs)
    events.append(
        run_id,
        "env",
        "system",
        {
            "ok": True,
            "image": image,
            "requirements": len(reqs),
            "passed": len(baseline.passed),
            "failed": len(baseline.failed),
            "flaky": baseline.flaky,
            "skipped": len(baseline.skipped),
            "runs": baseline.runs,
            "duration_ms": int((time.monotonic() - started) * 1000),
        },
        node="env",
    )
    return EnvResult(ok=True, image=image, requirements=reqs, baseline=baseline, sandbox_id=sandbox.id)


def _fail(
    events: EventStore, run_id: str, reason: str, started: float, *, image: str = "", reqs: list[str] | None = None
) -> EnvResult:
    events.append(
        run_id,
        "env",
        "system",
        {"ok": False, "reason": reason, "image": image, "duration_ms": int((time.monotonic() - started) * 1000)},
        node="env",
    )
    return EnvResult(ok=False, reason=reason, image=image, requirements=reqs or [])
