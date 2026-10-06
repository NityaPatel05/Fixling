from __future__ import annotations

import pytest

from fixling.verify.runner import (
    build_pytest_cmd,
    classify_failure,
    is_green,
    junit_to_nodeid,
    parse_junit,
)

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="1" failures="1" skipped="1" tests="5">
<testcase classname="tests.test_a" name="test_ok" time="0.01"/>
<testcase classname="tests.test_a.TestCls" name="test_nested[1-2]" time="0.01"/>
<testcase classname="tests.test_a" name="test_bad" time="0.01"><failure message="assert 50.0 == 45.0">def test_bad():
&gt;       assert f() == 45.0
E       assert 50.0 == 45.0</failure></testcase>
<testcase classname="tests.test_a" name="test_skip" time="0"><skipped type="pytest.skip" message="nope"/></testcase>
<testcase classname="" name="tests/test_broken.py" time="0"><error message="collection failure">ImportError while importing test module 'x'
E   ModuleNotFoundError: No module named 'nope'</error></testcase>
</testsuite></testsuites>"""


def test_build_cmd_is_controller_owned_and_hardened() -> None:
    cmd = build_pytest_cmd(["tests/test_a.py::test_x[1-2]", "tests/b.py"])
    assert cmd.startswith("rm -f /tmp/junit.xml; PYTEST_ADDOPTS= python -m pytest -q")
    assert "-p no:randomly" in cmd and "--junitxml=/tmp/junit.xml" in cmd and "-o addopts=" in cmd
    assert "'tests/test_a.py::test_x[1-2]'" in cmd  # quoted: ids can't inject shell
    assert "-p randomly" in build_pytest_cmd(shuffled=True) and "-p no:randomly" not in build_pytest_cmd(shuffled=True)
    evil = build_pytest_cmd(["x; rm -rf /"])
    assert "'x; rm -rf /'" in evil


def test_junit_to_nodeid() -> None:
    assert junit_to_nodeid("tests.test_a", "test_x") == "tests/test_a.py::test_x"
    assert junit_to_nodeid("tests.sub.test_a.TestCls", "test_x") == "tests/sub/test_a.py::TestCls::test_x"
    assert junit_to_nodeid("", "tests/test_broken.py") == "tests/test_broken.py"
    assert junit_to_nodeid("", "tests.fixling.test_imp") == "tests/fixling/test_imp.py"


def test_parse_junit_buckets_and_details() -> None:
    run = parse_junit(JUNIT, "cmd", 1, 5)
    assert run.collected == 5 and run.junit_ok
    assert run.passed == ["tests/test_a.py::test_ok", "tests/test_a.py::TestCls::test_nested[1-2]"]
    assert run.failed == ["tests/test_a.py::test_bad"]
    assert run.errors == ["tests/test_broken.py"] and run.skipped == ["tests/test_a.py::test_skip"]
    assert "50.0 == 45.0" in run.failure_details["tests/test_a.py::test_bad"]
    assert not is_green(run)


def test_parse_junit_missing_or_malformed_is_not_ok() -> None:
    assert not parse_junit("", "c", 0, 1).junit_ok
    assert not parse_junit("<not xml", "c", 0, 1).junit_ok
    empty = parse_junit('<testsuites><testsuite name="pytest" tests="0"/></testsuites>', "c", 0, 1)
    assert empty.collected == 0 and not empty.junit_ok and not is_green(empty)


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        ("failure: assert 1 == 2\nE assert 1 == 2", "assertion"),
        ("failure: AssertionError: boom\n", "assertion"),
        ("failure: Failed: DID NOT RAISE <class 'ValueError'>", "expected_exception"),
        ("error: collection failure\nImportError while importing test module", "import"),
        ("error: collection failure\nE   SyntaxError: invalid syntax", "syntax"),
        ("error: collection failure\nsomething else", "collection"),
        ("error: failed on setup with \"fixture 'tmpx' not found\"", "fixture"),
        ("failure: Failed: Timeout >1.0s", "timeout"),
        ("failure: ZeroDivisionError: division by zero\nE   ZeroDivisionError", "other"),
        ("failure: NameError: name 'x' is not defined", "other"),
    ],
)
def test_classify_failure(detail: str, expected: str) -> None:
    run = parse_junit(JUNIT, "c", 1, 1)
    run.failure_details["t"] = detail
    assert classify_failure("t", run) == expected
    assert classify_failure("unknown", run) == "other"
