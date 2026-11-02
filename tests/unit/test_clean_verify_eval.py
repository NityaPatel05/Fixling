from __future__ import annotations

import pytest

from fixling.config import Config
from fixling.models import TestRun
from fixling.verify.clean_verify import evaluate
from fixling.verify.policy import PolicyGate, has_block
from tests.unit.test_policy import FIXTURES, TINY

PATCH = (FIXTURES / "legit" / "02_fix_with_repro_test.diff").read_text(encoding="utf-8")
REPRO = ["tests/fixling/test_issue_1.py::test_ten_units_get_bulk_discount"]
BASE = {
    "passed": ["tests/test_a.py::test_1", "tests/test_a.py::test_2", "tests/test_b.py::test_3"],
    "skipped": ["tests/test_b.py::test_old_skip"],
}


def run(
    passed: list[str], *, skipped: list[str] | None = None, ok: bool = True, failed: list[str] | None = None
) -> TestRun:
    skipped, failed = skipped or [], failed or []
    return TestRun(
        cmd="pytest", exit_code=0 if not failed else 1, duration_ms=1, passed=passed, skipped=skipped, failed=failed,
        collected=len(passed) + len(skipped) + len(failed) if ok else 0, junit_ok=ok,
    )  # fmt: skip


@pytest.fixture
def gate(cfg: Config) -> PolicyGate:
    return PolicyGate(cfg.policy, base_dir=TINY)


ALL = [*BASE["passed"], *REPRO]


def test_a_real_fix_is_verified(gate: PolicyGate) -> None:
    v = evaluate(PATCH, gate, run(ALL, skipped=BASE["skipped"]), run(ALL, skipped=BASE["skipped"]), BASE, REPRO)
    assert v.f2p_ok and v.p2p_ok and v.shuffled_ok is True and v.regressions == [] and v.new_skips == []
    assert not has_block(v.findings)


def test_regression_in_the_full_suite_fails_p2p(gate: PolicyGate) -> None:
    broken = [t for t in ALL if t != "tests/test_a.py::test_2"]
    v = evaluate(
        PATCH,
        gate,
        run(broken, failed=["tests/test_a.py::test_2"]),
        run(broken, failed=["tests/test_a.py::test_2"]),
        BASE,
        REPRO,
    )
    assert v.f2p_ok and not v.p2p_ok and v.regressions == ["tests/test_a.py::test_2"] and v.shuffled_ok is False


def test_repro_still_failing_fails_f2p(gate: PolicyGate) -> None:
    v = evaluate(PATCH, gate, run(BASE["passed"], failed=REPRO), run(BASE["passed"], failed=REPRO), BASE, REPRO)
    assert not v.f2p_ok and v.p2p_ok


def test_a_repro_that_only_passes_in_one_order_is_not_f2p(gate: PolicyGate) -> None:
    v = evaluate(PATCH, gate, run(ALL), run(BASE["passed"], failed=REPRO), BASE, REPRO)
    assert not v.f2p_ok and v.shuffled_ok is False


def test_order_dependence_is_reported_without_blaming_the_fix(gate: PolicyGate) -> None:
    shuffled_bad = [t for t in ALL if t != "tests/test_b.py::test_3"]
    v = evaluate(PATCH, gate, run(ALL), run(shuffled_bad, failed=["tests/test_b.py::test_3"]), BASE, REPRO)
    assert v.p2p_ok and v.shuffled_ok is False


def test_new_skips_are_detected_but_preexisting_ones_are_not(gate: PolicyGate) -> None:
    v = evaluate(PATCH, gate, run(ALL, skipped=[*BASE["skipped"], "tests/test_a.py::test_hidden"]), None, BASE, REPRO)
    assert v.new_skips == ["tests/test_a.py::test_hidden"] and v.shuffled_ok is None
    ok = evaluate(PATCH, gate, run(ALL, skipped=BASE["skipped"]), None, BASE, REPRO)
    assert ok.new_skips == []


def test_a_run_without_valid_junit_is_never_a_pass(gate: PolicyGate) -> None:
    v = evaluate(PATCH, gate, run([], ok=False), None, BASE, REPRO)  # e.g. sys.exit(0) planted by the patch
    assert not v.f2p_ok and not v.p2p_ok and v.regressions == sorted(BASE["passed"])


def test_no_repro_means_f2p_cannot_be_claimed(gate: PolicyGate) -> None:
    v = evaluate(PATCH, gate, run(BASE["passed"]), run(BASE["passed"]), BASE, [])
    assert not v.f2p_ok and v.p2p_ok


def test_policy_is_rechecked_on_the_final_patch(gate: PolicyGate) -> None:
    hack = (FIXTURES / "hacks" / "01_edit_existing_test.diff").read_text(encoding="utf-8")
    v = evaluate(hack, gate, run(ALL), run(ALL), BASE, REPRO)
    assert has_block(v.findings) and v.findings[0].rule == "protected_path"
