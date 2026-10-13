from __future__ import annotations

from fixling.models import GateFinding
from fixling.verify.confidence import Facts, decide_outcome, status_label


def verified(**kw: object) -> Facts:
    base: dict[str, object] = {
        "has_repro": True,
        "has_candidate": True,
        "f2p_ok": True,
        "p2p_ok": True,
        "shuffled_ok": True,
        "agreement": 2,
    }
    base.update(kw)
    return Facts(**base)  # type: ignore[arg-type]


def finding(sev: str, rule: str = "literal_return") -> GateFinding:
    return GateFinding(rule=rule, severity=sev, path="a.py", detail="d")  # type: ignore[arg-type]


def test_needs_clarification_wins_over_everything() -> None:
    o = decide_outcome(Facts(clarify_reason="too vague", has_repro=True, has_candidate=True))
    assert (o.status, o.confidence, o.reason) == ("needs_clarification", "none", "too vague")


def test_high_confidence_requires_everything_to_line_up() -> None:
    o = decide_outcome(verified())
    assert o.status == "verified_fix" and o.confidence == "high"
    assert any("independent candidates" in r for r in o.confidence_reasons)
    assert any("shuffled" in r for r in o.confidence_reasons)


def test_extra_tests_corroborate_a_single_candidate() -> None:
    assert decide_outcome(verified(agreement=1, extra_tests="pass")).confidence == "high"


def test_single_uncorroborated_candidate_is_medium() -> None:
    o = decide_outcome(verified(agreement=1))
    assert o.confidence == "medium" and any("only one candidate" in r for r in o.confidence_reasons)


def test_each_downgrade_rule() -> None:
    cases = {
        "flags": verified(findings=[finding("flag")]),
        "shuffled": verified(shuffled_ok=False),
        "extra tests disagree": verified(extra_tests="fail"),
        "judge unsure": verified(judge_confidence=3),
        "judge flags": verified(judge_confidence=9, judge_flags=["special_cases_inputs"]),
        "judge notes": verified(judge_confidence=9, judge_concerns=["narrow"]),
    }
    for name, facts in cases.items():
        o = decide_outcome(facts)
        assert o.status == "verified_fix" and o.confidence == "medium", name
        assert any(r.startswith("Downgraded") for r in o.confidence_reasons), name


def test_confident_judge_does_not_downgrade() -> None:
    assert decide_outcome(verified(judge_confidence=8)).confidence == "high"


def test_regressions_make_it_unverified_with_a_reason() -> None:
    o = decide_outcome(verified(p2p_ok=False, regressions=["tests/t.py::test_a", "tests/t.py::test_b"]))
    assert o.status == "unverified_fix" and o.confidence == "low"
    assert "2 previously passing" in o.reason and "test_a" in o.reason


def test_block_findings_prevent_verified() -> None:
    o = decide_outcome(verified(findings=[finding("block", "protected_path")]))
    assert o.status == "unverified_fix" and "protected_path" in o.reason


def test_new_skips_prevent_verified() -> None:
    o = decide_outcome(verified(new_skips=["tests/t.py::test_x"]))
    assert o.status == "unverified_fix" and "skipped" in o.reason


def test_no_reproduction_caps_a_fix_at_unverified() -> None:
    o = decide_outcome(verified(has_repro=False, f2p_ok=False))
    assert o.status == "unverified_fix" and "no reproduction test" in o.reason.lower()


def test_failed_f2p_is_unverified() -> None:
    o = decide_outcome(verified(f2p_ok=False))
    assert o.status == "unverified_fix" and "does not pass with the fix" in o.reason


def test_repro_only_and_no_result() -> None:
    o = decide_outcome(Facts(has_repro=True, stop_reason="refine rounds exhausted"))
    assert (o.status, o.confidence, o.reason) == ("repro_only", "none", "refine rounds exhausted")
    o2 = decide_outcome(Facts(stop_reason="environment broken"))
    assert (o2.status, o2.confidence, o2.reason) == ("no_result", "none", "environment broken")
    assert decide_outcome(Facts()).status == "no_result"


def test_status_labels_cover_every_status() -> None:
    for s in ("verified_fix", "unverified_fix", "repro_only", "needs_clarification", "no_result"):
        assert status_label(s)  # type: ignore[arg-type]
