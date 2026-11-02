"""Evaluation tasks: format, materialisation and grading (spec G1, G4).

A task is a bug in a small repository plus everything needed to judge a fix, none of which the agent ever sees:
the gold patch, the hidden tests (``test_patch``) and the FAIL_TO_PASS / PASS_TO_PASS lists. For the repositories
in ``demo-repos/`` the buggy base is the clean repo plus ``bug_patch``, so a task file stays a few KB.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from fixling.config import BudgetsCfg
from fixling.graph.deps import SandboxProvider
from fixling.repo.workspace import copy_local
from fixling.verify.runner import run_tests

Source = Literal["own", "synthetic", "swe-verified-mini", "swe-live"]


class Task(BaseModel):
    task_id: str
    source: Source
    repo: str  # "local:demo-repos/minicart" (path relative to the project root) or "owner/name"
    base_commit: str | None = None
    bug_patch: str = ""  # turns the clean local repo into the buggy base
    problem_title: str = ""
    problem_statement: str
    gold_patch: str  # never shown to the agent
    test_patch: str = ""  # hidden tests, added at grading time only
    FAIL_TO_PASS: list[str] = Field(min_length=1)
    PASS_TO_PASS: list[str] = Field(default_factory=list)
    difficulty: Literal["easy", "medium", "hard"] = "medium"
    split: Literal["dev", "test"] = "dev"

    @model_validator(mode="after")
    def _local_tasks_need_a_path(self) -> Task:
        if self.repo.startswith("local:") and not self.repo.removeprefix("local:").strip():
            raise ValueError("local repo needs a path")
        return self

    @property
    def repo_key(self) -> str:
        """Cluster key for the bootstrap: tasks from one repository are not independent."""
        return self.repo.removeprefix("local:").rstrip("/").rsplit("/", 1)[-1]


def load_tasks(path: Path) -> list[Task]:
    tasks = [
        Task.model_validate_json(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    problems = validate_tasks(tasks)
    if problems:
        raise ValueError("invalid task file: " + "; ".join(problems))
    return tasks


def save_tasks(path: Path, tasks: list[Task]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(t.model_dump_json() for t in tasks) + "\n", encoding="utf-8", newline="")


def validate_tasks(tasks: list[Task]) -> list[str]:
    """Structural problems with a task set (empty list = fine)."""
    problems: list[str] = []
    for tid, n in Counter(t.task_id for t in tasks).items():
        if n > 1:
            problems.append(f"duplicate task_id {tid}")
    for t in tasks:
        if not t.gold_patch.strip():
            problems.append(f"{t.task_id}: empty gold_patch")
        if t.test_patch.strip() and "test" not in t.test_patch:
            problems.append(f"{t.task_id}: test_patch does not look like it touches tests")
    return problems


def select(tasks: list[Task], slice_name: str = "all", ids: list[str] | None = None) -> list[Task]:
    chosen = [t for t in tasks if slice_name == "all" or t.split == slice_name]
    return [t for t in chosen if not ids or t.task_id in ids]


# ---------------------------------------------------------------- patches and trees
def _git_env(tree: Path) -> dict[str, str]:
    # Never let git discover an unrelated enclosing repository (e.g. a home-directory repo) from a temp tree.
    return {**os.environ, "GIT_CEILING_DIRECTORIES": str(tree.resolve().parent)}


def apply_patch(tree: Path, patch: str) -> tuple[bool, str]:
    """``git apply`` ``patch`` onto the plain directory ``tree``. Returns ``(ok, error text)``."""
    if not patch.strip():
        return True, ""
    # Bytes, never text mode: on Windows text mode rewrites "\n" as "\r\n" on stdin and corrupts every patch.
    data = (patch if patch.endswith("\n") else patch + "\n").encode("utf-8")
    proc = subprocess.run(
        ["git", "apply", "--whitespace=nowarn", "-"],
        cwd=tree,
        input=data,
        capture_output=True,
        env=_git_env(tree),
        check=False,
    )
    out = (proc.stderr or proc.stdout).decode("utf-8", errors="replace")
    return proc.returncode == 0, out.strip()[-400:]


def materialize(task: Task, dest: Path, project_root: Path) -> Path:
    """Create the buggy base of ``task`` in ``dest`` (a fresh directory) and return it."""
    if not task.repo.startswith("local:"):
        raise NotImplementedError(f"{task.task_id}: only local demo repositories are materialised offline")
    source = (project_root / task.repo.removeprefix("local:")).resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"{task.task_id}: {source} does not exist")
    copy_local(source, dest)
    ok, err = apply_patch(dest, task.bug_patch)
    if not ok:
        raise ValueError(f"{task.task_id}: bug_patch does not apply: {err}")
    return dest


@dataclass
class Grade:
    resolved: bool
    reason: str = ""
    f2p_failed: list[str] = field(default_factory=list)
    p2p_failed: list[str] = field(default_factory=list)


def grade(
    task: Task,
    buggy_tree: Path,
    agent_patch: str,
    provider: SandboxProvider,
    run_id: str,
    budgets: BudgetsCfg,
) -> Grade:
    """Apply ``agent_patch`` and the hidden tests to a pristine copy and run FAIL_TO_PASS + PASS_TO_PASS cleanly.

    ``resolved`` means every FAIL_TO_PASS test passes and no PASS_TO_PASS test fails, with a valid junit report.
    """
    work = Path(tempfile.mkdtemp(prefix="fixling-grade-"))
    tree = work / "tree"
    try:
        shutil.copytree(buggy_tree, tree, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".git"))
        ok, err = apply_patch(tree, agent_patch)
        if not ok:
            return Grade(False, f"the agent patch does not apply: {err}")
        ok, err = apply_patch(tree, task.test_patch)
        if not ok:
            return Grade(False, f"the hidden tests do not apply on top of the agent patch: {err}")
        sandbox = provider.create(run_id, "grade", tree, task.repo_key)
        try:
            ids = [*task.FAIL_TO_PASS, *task.PASS_TO_PASS]
            run = run_tests(sandbox, ids, timeout=budgets.test_timeout_s)
        finally:
            sandbox.close()
        if not run.junit_ok:
            return Grade(False, "the grading run produced no valid report")
        passed = set(run.passed)
        f2p_failed = [t for t in task.FAIL_TO_PASS if t not in passed]
        p2p_failed = [t for t in task.PASS_TO_PASS if t not in passed and t not in run.skipped]
        if f2p_failed or p2p_failed:
            return Grade(
                False,
                "target tests still fail" if f2p_failed else "previously passing tests regressed",
                f2p_failed,
                p2p_failed,
            )
        return Grade(True, "all target tests pass and nothing regressed")
    finally:
        shutil.rmtree(work, ignore_errors=True)


@dataclass
class TaskCheck:
    task_id: str
    base_fails: bool  # the buggy base does NOT resolve the task
    gold_resolves: bool  # the gold patch does
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.base_fails and self.gold_resolves


def check_task(
    task: Task, project_root: Path, provider: SandboxProvider, budgets: BudgetsCfg, scratch: Path
) -> TaskCheck:
    """A task is only usable if the bug is real (empty patch fails) and the gold patch really fixes it."""
    tree = materialize(task, scratch / task.task_id, project_root)
    empty = grade(task, tree, "", provider, f"check-{task.task_id}", budgets)
    gold = grade(task, tree, task.gold_patch, provider, f"check-{task.task_id}", budgets)
    detail = (
        ""
        if (not empty.resolved and gold.resolved)
        else f"empty: {empty.reason}; gold: {gold.reason} {gold.f2p_failed[:2]}"
    )
    return TaskCheck(task.task_id, not empty.resolved, gold.resolved, detail)


def task_to_issue_text(task: Task) -> tuple[str, str]:
    """``(title, body)`` the agent sees: the problem statement only."""
    title = task.problem_title or task.problem_statement.strip().splitlines()[0][:100]
    return title, task.problem_statement


def dumps(tasks: list[Task]) -> str:
    return "\n".join(json.dumps(t.model_dump(), sort_keys=True) for t in tasks)
