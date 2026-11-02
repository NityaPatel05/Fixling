"""Synthetic bugs by AST-guided mutation (SWE-smith style, spec G3).

Operators edit exact source spans (so patches stay one line), a mutation is kept only if the visible test suite
detects it, and the "issue" is a CI-style regression report derived from the failing assertions.
"""

from __future__ import annotations

import ast
import random
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from fixling.evals.gitdiff import IGNORE, diff_trees
from fixling.evals.suite import run_suite
from fixling.evals.tasks import Task

COMPARE_FLIP = {"<": "<=", "<=": "<", ">": ">=", ">=": ">", "==": "!=", "!=": "=="}
ARITH_SWAP = {"+": "-", "-": "+", "*": "/", "/": "*"}
_CMP = re.compile(r"<=|>=|==|!=|<|>")
_ARITH = re.compile(r"(?<![*/])([+\-*/])(?![*/=])")


@dataclass(frozen=True)
class Mutation:
    path: str  # relative to the repo
    function: str
    operator: str
    line: int
    original: str
    replacement: str
    new_source: str

    @property
    def key(self) -> str:
        return f"{self.path}:{self.line}:{self.operator}"


def _offsets(source: str) -> list[int]:
    out, pos = [], 0
    for line in source.splitlines(keepends=True):
        out.append(pos)
        pos += len(line)
    return out


def _span(starts: list[int], node: ast.expr | ast.stmt) -> tuple[int, int]:
    assert node.end_lineno is not None and node.end_col_offset is not None
    return starts[node.lineno - 1] + node.col_offset, starts[node.end_lineno - 1] + node.end_col_offset


class _Collector(ast.NodeVisitor):
    def __init__(self, path: str, source: str) -> None:
        self.path, self.source, self.starts = path, source, _offsets(source)
        self.stack: list[str] = []
        self.found: list[Mutation] = []

    def _add(
        self, operator: str, node: ast.AST, start: int, end: int, replacement: str, line: int | None = None
    ) -> None:
        new = self.source[:start] + replacement + self.source[end:]
        lineno: int = line if line is not None else getattr(node, "lineno", 0)
        try:
            ast.parse(new)
        except SyntaxError:
            return
        self.found.append(
            Mutation(
                self.path,
                ".".join(self.stack) or "<module>",
                operator,
                lineno,
                self.source[start:end],
                replacement,
                new,
            )
        )

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_Compare(self, node: ast.Compare) -> None:
        if len(node.ops) == 1 and self.stack:
            _, left_end = _span(self.starts, node.left)
            right_start, _ = _span(self.starts, node.comparators[0])
            gap = self.source[left_end:right_start]
            m = _CMP.search(gap)
            if m:
                self._add("compare_flip", node, left_end + m.start(), left_end + m.end(), COMPARE_FLIP[m.group()])
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if isinstance(node.op, ast.Add | ast.Sub | ast.Mult | ast.Div) and self.stack:
            _, left_end = _span(self.starts, node.left)
            right_start, _ = _span(self.starts, node.right)
            gap = self.source[left_end:right_start]
            m = _ARITH.search(gap)
            if m:
                self._add("arith_swap", node, left_end + m.start(), left_end + m.end(), ARITH_SWAP[m.group()])
        self.generic_visit(node)

    def visit_If(self, node: ast.If) -> None:
        if self.stack:
            start, end = _span(self.starts, node.test)
            self._add("negate_condition", node, start, end, f"not ({self.source[start:end]})", node.lineno)
            if (
                not node.orelse
                and len(node.body) == 1
                and isinstance(node.body[0], ast.Return | ast.Raise | ast.Continue)
            ):
                first, last = self.starts[node.lineno - 1], self.starts[(node.end_lineno or node.lineno)]  # whole lines
                self._add("drop_guard", node, first, last, "", node.lineno)
        self.generic_visit(node)


def find_mutations(source: str, path: str) -> list[Mutation]:
    if not source.isascii():
        return []  # column offsets are byte-based; the demo repositories are ASCII
    collector = _Collector(path, source)
    collector.visit(ast.parse(source))
    return collector.found


def _first_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip().removeprefix("failure:").removeprefix("error:").strip()
        if line and not line.startswith(("def ", "_____", "tests/", "src/", "cls", "self")):
            return line[:160]
    return text.strip()[:160]


def regression_report(m: Mutation, failing: list[str], details: dict[str, str]) -> str:
    lines = [f"A regression was introduced in `{m.function}` (`{m.path}`). These tests started failing:", ""]
    for test in failing[:5]:
        msg = _first_line(details.get(test, ""))
        lines.append(f"- `{test}`" + (f": {msg}" if msg else ""))
    lines += ["", "Please find the cause and fix it without changing the tests."]
    return "\n".join(lines)


def build_synthetic_tasks(
    clean: Path, scratch: Path, *, count: int, seed: int, repo: str, max_attempts: int = 120
) -> list[Task]:
    baseline = run_suite(clean)
    if baseline.failed or baseline.errors or not baseline.junit_ok:
        raise ValueError("the clean repository's own suite must pass before mutating it")
    base_pass = set(baseline.passed)

    pool: list[Mutation] = []
    for path in sorted((clean / "src").rglob("*.py")):
        if path.name == "__init__.py":
            continue
        rel = path.relative_to(clean).as_posix()
        pool += find_mutations(path.read_text(encoding="utf-8"), rel)
    random.Random(seed).shuffle(pool)

    tasks: list[Task] = []
    per_function: dict[str, int] = {}
    per_file: dict[str, int] = {}
    for attempt, m in enumerate(pool):
        if len(tasks) >= count or attempt >= max_attempts:
            break
        if per_function.get(m.function, 0) >= 1 or per_file.get(m.path, 0) >= 3:
            continue  # diversify: one bug per function, at most three per file
        buggy = scratch / f"synth-{len(tasks)}"
        shutil.copytree(clean, buggy, ignore=IGNORE, dirs_exist_ok=True)
        (buggy / m.path).write_text(m.new_source, encoding="utf-8", newline="")
        result = run_suite(buggy)
        if not result.junit_ok:
            shutil.rmtree(buggy, ignore_errors=True)
            continue
        failing = sorted(t for t in base_pass if t not in set(result.passed))
        if not failing:
            shutil.rmtree(buggy, ignore_errors=True)
            continue  # the suite does not notice: not a usable task
        details = {**result.failure_details}
        idx = len(tasks)
        short = re.sub(r"\W+", "_", m.function).strip("_").lower()
        tasks.append(
            Task(
                task_id=f"minicart-synth-{idx:02d}-{m.operator}-{short}",
                source="synthetic",
                repo=repo,
                bug_patch=diff_trees(clean, buggy),
                problem_title=f"Regression in {m.function}",
                problem_statement=regression_report(m, failing, details),
                gold_patch=diff_trees(buggy, clean),
                test_patch="",
                FAIL_TO_PASS=failing,
                PASS_TO_PASS=sorted(result.passed),
                difficulty="easy",
                split="dev" if idx < count // 2 else "test",
            )
        )
        per_function[m.function] = per_function.get(m.function, 0) + 1
        per_file[m.path] = per_file.get(m.path, 0) + 1
        shutil.rmtree(buggy, ignore_errors=True)
    return tasks
