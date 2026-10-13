"""Outcome status and confidence, derived only from recorded evidence (spec A4, E14).

Both functions are pure: the same facts always give the same answer, and every rule that fires leaves a
human-readable reason that ends up in the PR body.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from fixling.models import ConfidenceLevel, GateFinding, Outcome, OutcomeStatus

ExtraTests = Literal["pass", "fail"] | None


@dataclass
class Facts:
    """Everything the final decision may depend on (assembled by ``finalize`` from state and events)."""

    clarify_reason: str | None = None  # set when the issue was too vague/large and no human resumed it
    stop_reason: str | None = None  # budget, cancel, error, env failure ...
    has_repro: bool = False  # a valid failing reproduction test exists
    has_candidate: bool = False  # a best candidate reached clean verification
    f2p_ok: bool = False
    p2p_ok: bool = False
    regressions: list[str] = field(default_factory=list)
    new_skips: list[str] = field(default_factory=list)
    shuffled_ok: bool | None = None
    findings: list[GateFinding] = field(default_factory=list)  # candidate + verify findings
    agreement: int = 1  # candidates that produced the same patch (including itself)
    extra_tests: ExtraTests = None
    judge_confidence: int | None = None
    judge_concerns: list[str] = field(default_factory=list)
    judge_flags: list[str] = field(default_factory=list)  # e.g. "special_cases_inputs"


def decide_outcome(f: Facts) -> Outcome:
    """Map facts to one of the five outcomes (spec A4) with a confidence level and reasons."""
    if f.clarify_reason:
        return Outcome(status="needs_clarification", reason=f.clarify_reason, confidence="none")

    blocks = [x for x in f.findings if x.severity == "block"]
    flags = [x for x in f.findings if x.severity == "flag"]

    if f.has_candidate:
        verified = f.has_repro and f.f2p_ok and f.p2p_ok and not blocks and not f.new_skips
        if verified:
            level, reasons = _confidence(f, flags)
            return Outcome(
                status="verified_fix",
                reason="The reproduction test fails before the fix and passes after it in a clean container, "
                "and no previously passing test regressed.",
                confidence=level,
                confidence_reasons=reasons,
            )
        why = _why_unverified(f, blocks)
        return Outcome(status="unverified_fix", reason=why, confidence="low", confidence_reasons=[why])

    if f.has_repro:
        return Outcome(
            status="repro_only",
            reason=f.stop_reason or "A valid failing test reproduces the issue, but no verified fix was found.",
            confidence="none",
        )
    return Outcome(
        status="no_result",
        reason=f.stop_reason or "The issue could not be reproduced and no fix was found.",
        confidence="none",
    )


def _why_unverified(f: Facts, blocks: list[GateFinding]) -> str:
    parts: list[str] = []
    if not f.has_repro:
        parts.append("no reproduction test could be written, so the fix is unproven")
    elif not f.f2p_ok:
        parts.append("the reproduction test does not pass with the fix in a clean container")
    if f.regressions:
        parts.append(f"{len(f.regressions)} previously passing test(s) regressed: {', '.join(f.regressions[:3])}")
    if not f.p2p_ok and not f.regressions:
        parts.append("the existing tests did not all pass")
    if f.new_skips:
        parts.append(f"the patch introduced {len(f.new_skips)} new skipped test(s)")
    if blocks:
        parts.append("integrity gate: " + "; ".join(f"{b.rule} ({b.detail})" for b in blocks[:3]))
    return "; ".join(parts).capitalize() + "." if parts else "The fix could not be fully verified."


def _confidence(f: Facts, flags: list[GateFinding]) -> tuple[ConfidenceLevel, list[str]]:
    """high / medium for a verified fix, with a reason for every rule that fired."""
    reasons: list[str] = [
        "Reproduction test failed before and passes after the fix; the full suite has no regressions."
    ]
    downgrade: list[str] = []
    if flags:
        downgrade.append("integrity flags: " + "; ".join(f"{x.rule} ({x.detail})" for x in flags[:3]))
    if f.shuffled_ok is False:
        downgrade.append("the suite is order-dependent: it fails when run in shuffled order")
    corroborated = f.agreement >= 2 or f.extra_tests == "pass"
    if f.agreement >= 2:
        reasons.append(f"{f.agreement} independent candidates produced the same patch.")
    if f.extra_tests == "pass":
        reasons.append("Independently written tests (never shown the patch) also pass.")
    if f.extra_tests == "fail":
        downgrade.append("independently written tests from the issue disagree with the fix")
    if not corroborated:
        downgrade.append("only one candidate produced this patch and nothing independent corroborates it")
    if f.judge_confidence is not None:
        if f.judge_confidence < 6:
            downgrade.append(f"the reviewer model is unsure (confidence {f.judge_confidence}/10)")
        if f.judge_flags:
            downgrade.append("reviewer concerns: " + ", ".join(f.judge_flags))
        if f.judge_concerns:
            downgrade.append("reviewer notes: " + "; ".join(f.judge_concerns[:2]))
    if f.shuffled_ok:
        reasons.append("The suite also passes in shuffled order.")
    if downgrade:
        return "medium", [*reasons, *("Downgraded: " + d for d in downgrade)]
    return "high", reasons


def status_label(status: OutcomeStatus) -> str:
    return {
        "verified_fix": "Verified fix",
        "unverified_fix": "Unverified fix (needs review)",
        "repro_only": "Reproduction only",
        "needs_clarification": "Needs clarification",
        "no_result": "No result",
    }[status]
