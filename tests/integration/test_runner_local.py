"""Runner against real pytest subprocesses (slow: each case starts bash + python + pytest)."""

from __future__ import annotations

import signal
from pathlib import Path

import pytest

from fixling.verify.runner import classify_failure, is_green, run_tests
from tests.fakes import LocalSandbox


@pytest.fixture
def sb(tiny_repo: Path) -> LocalSandbox:
    return LocalSandbox(tiny_repo)


def test_full_suite_run(sb: LocalSandbox) -> None:
    run = run_tests(sb, timeout=120)
    assert run.junit_ok and is_green(run) and run.collected == 6 and len(run.passed) == 6
    assert "tests/test_pricing.py::test_bulk_price_small_order_has_no_discount" in run.passed


def test_selected_ids_and_failing_repro_is_classified(sb: LocalSandbox) -> None:
    sb.write_file(
        "tests/fixling/test_issue_1.py",
        "from tiny.pricing import bulk_price\n\ndef test_ten_units_get_discount():\n    assert bulk_price(10, 5.0) == 45.0\n",
    )
    tid = "tests/fixling/test_issue_1.py::test_ten_units_get_discount"
    run = run_tests(sb, [tid], timeout=120)
    assert run.failed == [tid] and run.collected == 1 and run.junit_ok and not is_green(run)
    assert classify_failure(tid, run) == "assertion"


@pytest.mark.parametrize(
    ("filename", "code", "expected"),
    [
        ("test_imp.py", "import not_a_real_module\n\ndef test_x():\n    assert True\n", {"import"}),
        ("test_syn.py", "def test_x(:\n    pass\n", {"syntax", "collection"}),
        (
            "test_exc.py",
            "import pytest\n\ndef test_x():\n    with pytest.raises(ValueError):\n        pass\n",
            {"expected_exception"},
        ),
        ("test_fix.py", "def test_x(missing_fixture):\n    assert True\n", {"fixture"}),
        ("test_zero.py", "def test_x():\n    1 / 0\n", {"other"}),
    ],
)
def test_broken_repro_files_are_classified(sb: LocalSandbox, filename: str, code: str, expected: set[str]) -> None:
    sb.write_file(f"tests/fixling/{filename}", code)
    run = run_tests(sb, [f"tests/fixling/{filename}"], timeout=120)
    (tid,) = [*run.failed, *run.errors]
    assert tid.startswith(f"tests/fixling/{filename}")  # collection errors are normalised to a file path too
    assert classify_failure(tid, run) in expected, run.failure_details[tid][:300]
    assert "" not in run.failure_details[tid] and "#x1B" not in run.failure_details[tid]


@pytest.mark.skipif(not hasattr(signal, "SIGALRM"), reason="pytest-timeout hard-exits without SIGALRM (Windows)")
def test_per_test_timeout_is_reported(sb: LocalSandbox) -> None:
    sb.write_file("tests/fixling/test_slow.py", "import time\n\ndef test_slow():\n    time.sleep(5)\n")
    run = run_tests(sb, ["tests/fixling/test_slow.py"], timeout=120, per_test_timeout=1)
    (tid,) = run.failed
    assert classify_failure(tid, run) == "timeout"


def test_sys_exit_zero_hack_is_not_green(sb: LocalSandbox) -> None:
    sb.write_file("tests/conftest.py", "import sys\nsys.exit(0)\n")
    run = run_tests(sb, timeout=120)
    assert not run.junit_ok and not is_green(run)  # exit code may be 0, but there is no valid junit


def test_repo_addopts_cannot_filter_tests(sb: LocalSandbox) -> None:
    sb.write_file("pytest.ini", "[pytest]\naddopts = -k nothing_matches_this\n")
    run = run_tests(sb, timeout=120)
    assert run.collected == 6  # -o addopts= neutralised the repo's filter


def test_shuffled_run(sb: LocalSandbox) -> None:
    run = run_tests(sb, shuffled=True, timeout=120)
    assert is_green(run) and run.collected == 6
    assert "-p randomly" in run.cmd


def test_wall_clock_timeout_marks_run_not_ok(sb: LocalSandbox) -> None:
    sb.write_file("tests/fixling/test_hang.py", "import time\n\ndef test_hang():\n    time.sleep(8)\n")
    run = run_tests(sb, ["tests/fixling/test_hang.py"], timeout=2, per_test_timeout=100)
    assert not run.junit_ok and not is_green(run) and run.output.startswith("[timed out]")
