"""Build and validate ``evals/tasks/tasks.jsonl`` from the demo repositories (spec G2, G3).

    uv run python -m fixling.evals.build_tasks [--synthetic 10] [--seed 7]

Every task is validated mechanically before it is written: the hidden tests fail on the buggy base, the repository's
visible suite still passes there (the bug really is invisible to it), and the gold patch fixes everything.
"""

from __future__ import annotations

import argparse
import shutil
import tempfile
from pathlib import Path

from fixling.evals.gitdiff import IGNORE, diff_trees
from fixling.evals.own_bugs import OWN_BUGS, OwnBug
from fixling.evals.suite import run_suite
from fixling.evals.tasks import Task, save_tasks, validate_tasks

REPO = "local:demo-repos/minicart"


def project_root() -> Path:
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / "demo-repos").is_dir():
            return p
    return Path.cwd()


def inject(clean: Path, dest: Path, edits: list[tuple[str, str, str]]) -> None:
    shutil.copytree(clean, dest, ignore=IGNORE)
    for rel, old, new in edits:
        path = dest / rel
        text = path.read_text(encoding="utf-8")
        if text.count(old) != 1:
            raise ValueError(f"{rel}: expected exactly one occurrence of {old!r}, found {text.count(old)}")
        path.write_text(text.replace(old, new), encoding="utf-8", newline="")


def build_own_task(bug: OwnBug, clean: Path, scratch: Path) -> Task:
    buggy = scratch / f"{bug.slug}-buggy"
    inject(clean, buggy, bug.edits)
    hidden_rel = f"tests/hidden/test_{bug.slug}.py"
    with_hidden = scratch / f"{bug.slug}-hidden"
    shutil.copytree(buggy, with_hidden, ignore=IGNORE)
    (with_hidden / hidden_rel).parent.mkdir(parents=True, exist_ok=True)
    (with_hidden / hidden_rel).write_text(bug.hidden_test, encoding="utf-8", newline="")

    # 1. the visible suite passes on the buggy base: the bug is invisible to it (this is also the PASS_TO_PASS list)
    visible = run_suite(buggy)
    if visible.failed or visible.errors or not visible.junit_ok:
        raise ValueError(f"{bug.slug}: the visible suite already fails on the buggy base: {visible.failed[:3]}")
    # 2. the hidden tests fail on the buggy base
    hidden_on_bug = run_suite(with_hidden, [hidden_rel])
    f2p = sorted([*hidden_on_bug.failed, *hidden_on_bug.errors])
    if not f2p:
        raise ValueError(f"{bug.slug}: the hidden tests do not detect the bug")
    # 3. ... and all pass on the clean tree (the gold fix)
    clean_hidden = scratch / f"{bug.slug}-gold"
    shutil.copytree(clean, clean_hidden, ignore=IGNORE)
    (clean_hidden / hidden_rel).parent.mkdir(parents=True, exist_ok=True)
    (clean_hidden / hidden_rel).write_text(bug.hidden_test, encoding="utf-8", newline="")
    gold_run = run_suite(clean_hidden)
    if gold_run.failed or gold_run.errors or not gold_run.junit_ok:
        raise ValueError(f"{bug.slug}: the gold tree does not pass everything: {gold_run.failed[:3]}")
    all_hidden = sorted(t for t in gold_run.passed if t.startswith("tests/hidden/") and bug.slug in t)
    if not set(f2p) <= set(all_hidden):
        raise ValueError(
            f"{bug.slug}: tests fail on the buggy base but not on the gold tree: {set(f2p) - set(all_hidden)}"
        )
    # Hidden tests that already pass on the buggy base guard behaviour the bug does not touch: regression guards.
    hidden_guards = [t for t in all_hidden if t not in set(f2p)]

    return Task(
        task_id=f"minicart-{bug.slug}",
        source="own",
        repo=REPO,
        bug_patch=diff_trees(clean, buggy),
        problem_title=bug.title,
        problem_statement=bug.issue,
        gold_patch=diff_trees(buggy, clean),
        test_patch=diff_trees(buggy, with_hidden),
        FAIL_TO_PASS=f2p,
        PASS_TO_PASS=sorted([*visible.passed, *hidden_guards]),
        difficulty=bug.difficulty,
        split=bug.split,
    )


def build_all(root: Path, synthetic: int = 10, seed: int = 7) -> list[Task]:
    clean = root / "demo-repos" / "minicart"
    tasks: list[Task] = []
    with tempfile.TemporaryDirectory(prefix="fixling-tasks-") as tmp:
        scratch = Path(tmp)
        for bug in OWN_BUGS:
            tasks.append(build_own_task(bug, clean, scratch))
        if synthetic:
            from fixling.evals.synth_bugs import build_synthetic_tasks

            tasks += build_synthetic_tasks(clean, scratch, count=synthetic, seed=seed, repo=REPO)
    problems = validate_tasks(tasks)
    if problems:
        raise ValueError("; ".join(problems))
    return tasks


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--synthetic", type=int, default=10)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    root = project_root()
    tasks = build_all(root, args.synthetic, args.seed)
    out = args.out or root / "evals" / "tasks" / "tasks.jsonl"
    save_tasks(out, tasks)
    dev = sum(1 for t in tasks if t.split == "dev")
    print(f"wrote {len(tasks)} tasks ({dev} dev / {len(tasks) - dev} test) to {out}")


if __name__ == "__main__":
    main()
