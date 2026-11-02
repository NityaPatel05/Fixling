"""Clean-container verification (spec E12, test-integrity layers L3 and L4).

The final patch is applied to a brand-new sandbox built from the TRUSTED host copy of the repository, so nothing
the agent did to tests, configs or the environment while generating candidates can influence the result.
"""

from __future__ import annotations

from fixling.config import BudgetsCfg
from fixling.events.store import EventStore
from fixling.models import GateFinding, TestRun, VerifyResult
from fixling.sandbox.base import Sandbox
from fixling.verify.policy import GateLike
from fixling.verify.runner import record_test_run, run_tests

PATCH_PATH = "/tmp/final.diff"
APPLY_CMD = f"git apply --check {PATCH_PATH} && git apply --whitespace=nowarn {PATCH_PATH}"


def empty_run(reason: str) -> TestRun:
    return TestRun(cmd="(not run)", exit_code=-1, duration_ms=0, output=reason)


def evaluate(
    patch: str,
    gate: GateLike,
    full: TestRun,
    shuffled: TestRun | None,
    baseline: dict[str, list[str]],
    repro_ids: list[str],
) -> VerifyResult:
    """Turn the two full-suite runs into the verdict (pure; unit-tested without any sandbox)."""
    base_pass = set(baseline.get("passed", []))
    base_skip = set(baseline.get("skipped", []))
    passed = set(full.passed)
    regressions = sorted(t for t in base_pass if t not in passed)
    new_skips = sorted(s for s in full.skipped if s not in base_skip and s not in repro_ids)
    f2p_ok = bool(repro_ids) and full.junit_ok and all(t in passed for t in repro_ids)
    shuffled_ok: bool | None = None
    if shuffled is not None:
        sp = set(shuffled.passed)
        shuffled_ok = (
            shuffled.junit_ok and all(t in sp for t in base_pass) and (not repro_ids or all(t in sp for t in repro_ids))
        )
        f2p_ok = f2p_ok and bool(repro_ids) and all(t in sp for t in repro_ids)
    return VerifyResult(
        f2p_ok=f2p_ok,
        p2p_ok=full.junit_ok and not regressions,
        regressions=regressions,
        new_skips=new_skips,
        shuffled_ok=shuffled_ok,
        full_run=full,
        findings=gate.check_diff(patch),
    )


def clean_verify(
    *,
    sandbox: Sandbox,
    patch: str,
    gate: GateLike,
    baseline: dict[str, list[str]],
    repro_ids: list[str],
    budgets: BudgetsCfg,
    events: EventStore,
    run_id: str,
) -> VerifyResult:
    """Apply ``patch`` in ``sandbox`` (a fresh container) and judge it. The caller closes the sandbox."""
    sandbox.write_file(PATCH_PATH, patch)
    applied = sandbox.exec(APPLY_CMD, 60)
    if applied.exit_code != 0:
        detail = (applied.stderr or applied.stdout).strip()[-400:]
        finding = GateFinding(
            rule="patch_apply_failed",
            severity="block",
            path="",
            detail=f"the final patch does not apply to the pristine repository: {detail}",
        )
        return VerifyResult(
            f2p_ok=False,
            p2p_ok=False,
            regressions=[],
            new_skips=[],
            shuffled_ok=None,
            full_run=empty_run(detail),
            findings=[finding, *gate.check_diff(patch)],
        )
    full = run_tests(sandbox, timeout=budgets.test_timeout_s)
    record_test_run(events, run_id, "clean_verify", full, "clean verify (fixed order)")
    shuffled: TestRun | None = None
    if budgets.verify_shuffled and full.junit_ok:
        shuffled = run_tests(sandbox, shuffled=True, timeout=budgets.test_timeout_s)
        record_test_run(events, run_id, "clean_verify", shuffled, "clean verify (shuffled order)")
    return evaluate(patch, gate, full, shuffled, baseline, repro_ids)
