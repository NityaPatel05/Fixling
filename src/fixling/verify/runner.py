"""Controller-built pytest runs and junit parsing (spec E10).

The command is assembled here and never by the model. The junit file, not the exit code, is the source of
truth: a ``sys.exit(0)`` hack yields exit code 0 with no valid junit and is therefore treated as a failure.
"""

from __future__ import annotations

import re
import shlex
from typing import Literal

from junitparser import Error, Failure, JUnitXml, Skipped, TestCase

from fixling.events.store import EventStore, truncate_middle
from fixling.models import TestRun
from fixling.sandbox.base import Sandbox

JUNIT_PATH = "/tmp/junit.xml"
OUTPUT_CHARS = 4000
DETAIL_CHARS = 1500

FailureClass = Literal[
    "assertion", "expected_exception", "import", "syntax", "collection", "fixture", "timeout", "other"
]


def build_pytest_cmd(test_ids: list[str] | None = None, *, shuffled: bool = False, per_test_timeout: int = 120) -> str:
    """The exact pytest invocation. ``-o addopts=`` and an empty ``PYTEST_ADDOPTS`` neutralise repo-level options."""
    parts = [
        "python -m pytest -q --color=no -p no:cacheprovider",
        "-p randomly" if shuffled else "-p no:randomly",
        f"--timeout={per_test_timeout}",
        f"--junitxml={JUNIT_PATH}",
        "-o addopts=",
    ]
    if test_ids:
        parts.extend(shlex.quote(t) for t in test_ids)
    return f"rm -f {JUNIT_PATH}; PYTEST_ADDOPTS= " + " ".join(parts)


def junit_to_nodeid(classname: str, name: str) -> str:
    """``tests.test_a.TestX`` + ``test_y`` -> ``tests/test_a.py::TestX::test_y`` (uppercase segment = class)."""
    if not classname:
        # Collection errors have an empty classname and a dotted module name: ``tests.fixling.test_x``.
        if "/" in name or "::" in name or "." not in name:
            return name
        return name.replace(".", "/") + ".py"
    segs = classname.split(".")
    split = next((i for i, s in enumerate(segs) if s[:1].isupper()), len(segs))
    module = "/".join(segs[:split]) + ".py"
    classes = segs[split:]
    return "::".join([module, *classes, name])


_ANSI = re.compile(r"(?:\x1b|#x1B)\[[0-9;]*[A-Za-z]")


def _clean(text: str | None) -> str:
    return _ANSI.sub("", text or "")


def _case_outcome(case: TestCase) -> tuple[str, str]:
    """(kind, detail) where kind is passed|failed|error|skipped."""
    for r in case.result:
        if isinstance(r, Failure):
            return "failed", f"failure: {_clean(r.message)}\n{_clean(r.text)}"
        if isinstance(r, Error):
            return "error", f"error: {_clean(r.message)}\n{_clean(r.text)}"
        if isinstance(r, Skipped):
            return "skipped", f"skipped: {r.message or ''}"
    return "passed", ""


def parse_junit(xml: str, cmd: str, exit_code: int, duration_ms: int, output: str = "") -> TestRun:
    run = TestRun(cmd=cmd, exit_code=exit_code, duration_ms=duration_ms, output=output)
    if not xml.strip():
        return run
    try:
        tree = JUnitXml.fromstring(xml)
    except Exception:  # malformed xml (junitparser/lxml raise assorted types) -> junit_ok stays False
        return run
    for suite in tree:
        for case in suite:
            node = junit_to_nodeid(case.classname or "", case.name or "")
            run.collected += 1
            kind, detail = _case_outcome(case)
            if kind == "passed":
                run.passed.append(node)
            elif kind == "skipped":
                run.skipped.append(node)
            else:
                (run.failed if kind == "failed" else run.errors).append(node)
                run.failure_details[node] = detail[:DETAIL_CHARS]
    total = len(run.passed) + len(run.failed) + len(run.errors) + len(run.skipped)
    run.junit_ok = run.collected > 0 and run.collected == total
    return run


def run_tests(
    sandbox: Sandbox,
    test_ids: list[str] | None = None,
    *,
    shuffled: bool = False,
    timeout: int = 600,
    per_test_timeout: int = 120,
) -> TestRun:
    cmd = build_pytest_cmd(test_ids, shuffled=shuffled, per_test_timeout=per_test_timeout)
    res = sandbox.exec(cmd, timeout)
    try:
        xml = sandbox.read_file(JUNIT_PATH)
    except Exception:
        xml = ""
    output = truncate_middle((res.stdout + ("\n" + res.stderr if res.stderr else "")).strip(), OUTPUT_CHARS)
    if res.timed_out:
        output = "[timed out]\n" + output
    run = parse_junit(xml, cmd, res.exit_code, res.duration_ms, output)
    if res.timed_out:
        run.junit_ok = False
    return run


def is_green(run: TestRun) -> bool:
    """All collected tests passed AND the junit file backs it up (exit code alone is not trusted)."""
    return run.exit_code == 0 and run.junit_ok and not run.failed and not run.errors


_RULES: list[tuple[FailureClass, re.Pattern[str]]] = [
    ("timeout", re.compile(r"Timeout\b|timed out", re.I)),
    ("fixture", re.compile(r"failed on (setup|teardown)|fixture '[^']+' not found", re.I)),
    ("expected_exception", re.compile(r"DID NOT RAISE")),
    ("syntax", re.compile(r"\b(SyntaxError|IndentationError|TabError)\b")),
    ("import", re.compile(r"\b(ImportError|ModuleNotFoundError)\b")),
    ("collection", re.compile(r"collection failure|errors? during collection", re.I)),
    ("assertion", re.compile(r"^(failure|error): (assert\b|AssertionError)", re.M)),
]


def classify_failure(test_id: str, run: TestRun) -> FailureClass:
    """Why did ``test_id`` fail? Used to accept only 'right reason' reproduction failures."""
    detail = run.failure_details.get(test_id, "")
    if not detail:
        return "other"
    head = detail[:600]
    for klass, pat in _RULES:
        if pat.search(head if klass == "assertion" else detail):
            return klass
    if re.search(r"^E\s+assert\b|^E\s+AssertionError", detail, re.M):
        return "assertion"
    return "other"


def record_test_run(
    events: EventStore, run_id: str, node: str, run: TestRun, label: str, *, cause: int | None = None
) -> int:
    """Append a ``test_run`` event (summary inline, full output as artifact) and return its seq."""
    return events.append(
        run_id,
        "test_run",
        "sandbox",
        {
            "label": label,
            "cmd": run.cmd,
            "exit_code": run.exit_code,
            "duration_ms": run.duration_ms,
            "collected": run.collected,
            "passed": len(run.passed),
            "failed": run.failed,
            "errors": run.errors,
            "skipped": len(run.skipped),
            "junit_ok": run.junit_ok,
        },
        node=node,
        cause=cause,
        artifact_text=run.output,
    )
