from __future__ import annotations

import re
from pathlib import Path

import pytest

from fixling.db import Database
from fixling.events.store import EventStore
from fixling.gh.pr_body import RunInfo, build_issue_comment, build_pr_body, clean_text, numstat
from fixling.models import Outcome
from tests.unit.test_policy import FIXTURES

FIX_PATCH = (FIXTURES / "legit" / "02_fix_with_repro_test.diff").read_text(encoding="utf-8")
INFO = RunInfo(
    repo="o/r",
    issue_number=7,
    run_id="01ABC",
    model="fixling-main",
    tokens_in=1200,
    tokens_out=300,
    llm_ms=45_000,
    wall_ms=200_000,
)


@pytest.fixture
def events(tmp_path: Path) -> EventStore:
    return EventStore(Database(tmp_path / "t.sqlite"), tmp_path / "runs")


def story(
    ev: EventStore, *, verified: bool = True, summary: str = "bulk_price skips the 10-unit discount"
) -> dict[str, int]:
    """Record a plausible run and return the seq of each key event."""
    seq: dict[str, int] = {}
    seq["spec"] = ev.append("r", "spec", "llm", {"summary": summary, "kind": "bug"})
    seq["repro"] = ev.append(
        "r", "repro", "llm",
        {"ok": True, "test_ids": ["tests/fixling/test_issue_7.py::test_ten"], "failure": "assert 50.0 == 45.0\nE  where 50.0 = bulk_price(10, 5.0)"},
    )  # fmt: skip
    seq["c1"] = ev.append("r", "candidate", "llm", {"id": "r0c0", "repro_passed": True, "regressions": []})
    seq["c2"] = ev.append("r", "candidate", "llm", {"id": "r0c1", "repro_passed": True, "regressions": []})
    seq["c3"] = ev.append("r", "candidate", "llm", {"id": "r0c2", "repro_passed": False, "regressions": ["t"]})
    seq["sel"] = ev.append("r", "selection", "system", {"ranked": [{"id": "r0c0", "agreement": 1}], "best": "r0c0"})
    seq["verify"] = ev.append(
        "r", "verify", "sandbox",
        {"passed": 215, "failed": 0, "skipped": 3, "shuffled_ok": True, "regressions": [] if verified else ["tests/t.py::test_a"], "findings": []},
    )  # fmt: skip
    return seq


def body(
    ev: EventStore, outcome: Outcome, patch: str = FIX_PATCH, info: RunInfo = INFO, flags: list[str] | None = None
) -> str:
    return build_pr_body(ev.list("r"), outcome, info, patch, flags=flags)


VERIFIED = Outcome(
    status="verified_fix",
    reason="ok",
    confidence="high",
    confidence_reasons=["Reproduction test failed before and passes after the fix."],
)


def test_verified_body_has_every_section_and_closes_the_issue(events: EventStore) -> None:
    seqs = story(events)
    text = body(events, VERIFIED)
    for heading in (
        "## Summary",
        "## Changes",
        "## Evidence",
        "## Integrity checks",
        "## Confidence: HIGH",
        "## Caveats",
        "## Run info",
    ):
        assert heading in text
    assert "Fixes #7" in text and "Related to" not in text
    assert "| `src/tiny/pricing.py` | +1 | -1 |" in text and "tests/fixling/test_issue_1.py" in text
    assert (
        f"Reproduced before the fix: `tests/fixling/test_issue_7.py::test_ten` → FAIL (assert 50.0 == 45.0) [event #{seqs['repro']}]"
        in text
    )
    assert f"full suite 215 passed, 0 failed, 3 skipped [event #{seqs['verify']}]" in text
    assert f"Shuffled test order: passed [event #{seqs['verify']}]" in text
    assert (
        f"Candidates tried: 3 (2 passed the reproduction test, 2 identical) [events #{seqs['c1']} to #{seqs['c3']}]"
        in text
    )
    assert "Protected files untouched ✓" in text and "no blocking findings ✓" in text and "Flags: none" in text
    assert "`fixling-main`" in text and "tokens in/out: 1200/300" in text and "45s" in text and "3.3 min" in text
    assert "http://localhost:8000/runs/01ABC" in text


def test_every_evidence_line_cites_an_existing_event(events: EventStore) -> None:
    story(events)
    text = body(events, VERIFIED)
    evidence = text.split("## Evidence")[1].split("## Integrity")[0]
    lines = [ln for ln in evidence.splitlines() if ln.startswith("- ")]
    assert lines and all(re.search(r"\[events? #\d+", ln) for ln in lines)
    cited = {int(n) for n in re.findall(r"#(\d+)", evidence)}
    assert cited <= {e.seq for e in events.list("r")}, "every cited event number must exist in the log"


def test_unverified_fix_never_says_fixes_and_explains_why(events: EventStore) -> None:
    story(events, verified=False)
    out = Outcome(
        status="unverified_fix",
        reason="1 previously passing test(s) regressed: tests/t.py::test_a.",
        confidence="low",
        confidence_reasons=["1 previously passing test(s) regressed"],
    )
    text = body(events, out)
    assert "Related to #7" in text and "Fixes #7" not in text and "## Confidence: LOW" in text
    assert "not fully verified" in text and "Regressions: `tests/t.py::test_a`" in text


def test_repro_only_body(events: EventStore) -> None:
    events.append("r", "spec", "llm", {"summary": "s"})
    events.append("r", "repro", "llm", {"ok": True, "test_ids": ["t::x"], "failure": "assert 1 == 2"})
    out = Outcome(status="repro_only", reason="No fix", confidence="none")
    text = body(
        events,
        out,
        patch="diff --git a/tests/fixling/t.py b/tests/fixling/t.py\n+++ b/tests/fixling/t.py\n@@ -0,0 +1 @@\n+x\n",
    )
    assert "## Confidence: NONE" in text and "only adds a failing test" in text and "Fixes" not in text
    assert "After the fix" not in text


def test_missing_repro_and_flags_become_caveats(events: EventStore) -> None:
    events.append("r", "spec", "llm", {"summary": "s"})
    events.append("r", "repro", "system", {"ok": False, "attempts": 3})
    out = Outcome(status="unverified_fix", reason="No reproduction test could be written.", confidence="low")
    text = body(events, out, flags=["no_repro", "hidden_content", "degraded_spec"])
    assert "No valid reproduction test could be written (3 attempts)" in text
    assert "could not be written, so this change is unproven" in text
    assert "Invisible or hidden content" in text and "heuristic summary" in text


def test_gate_flags_and_blocks_are_reported(events: EventStore) -> None:
    story(events)
    events.append(
        "r",
        "gate_finding",
        "system",
        {"candidate": "r0c0", "rule": "literal_return", "severity": "flag", "detail": "x"},
    )
    events.append(
        "r",
        "gate_finding",
        "system",
        {"candidate": "r0c9", "rule": "protected_path", "severity": "block", "detail": "tests/x.py"},
    )
    line = body(events, VERIFIED).split("## Integrity checks")[1].split("##")[0]
    assert (
        "Protected files untouched ✗" in line and "1 blocking finding(s) ✗" in line and "Flags: literal_return" in line
    )


def test_model_originated_text_cannot_inject_markup_or_pings(events: EventStore) -> None:
    nasty = "Fixed ![x](http://evil.test/p.png) see [docs](http://evil.test) cc @octocat <script>alert(1)</script> https://evil.test/x"
    story(events, summary=nasty)
    text = body(events, VERIFIED)
    summary = text.split("## Summary")[1].split("##")[0]
    for bad in ("![", "](http", "<script>", "evil.test", "@octocat"):
        assert bad not in summary, bad
    assert "@​octocat" in summary and "[link removed]" in summary
    assert "Fixes #7" in summary


def test_no_issue_number_means_no_issue_link(events: EventStore) -> None:
    story(events)
    text = body(events, VERIFIED, info=RunInfo(repo="o/r", issue_number=None, run_id="x", model="m"))
    assert "Fixes" not in text and "Related to" not in text


def test_clean_text() -> None:
    assert clean_text("a `code` and **bold**") == "a 'code' and **bold**"
    assert clean_text("line1\n\n   line2") == "line1 line2"
    assert len(clean_text("x" * 1000)) == 300 and clean_text("x" * 1000).endswith("…")
    assert clean_text("@user and email a@b.co") == "@​user and email a@​b.co"


def test_numstat() -> None:
    assert numstat(FIX_PATCH) == [("src/tiny/pricing.py", 1, 1), ("tests/fixling/test_issue_1.py", 6, 0)]
    assert numstat("") == []


def test_issue_comments() -> None:
    info = INFO
    ask = build_issue_comment(
        Outcome(status="needs_clarification", reason="too vague", confidence="none"),
        info,
        None,
        ["What did you expect?", "Which function? @bob"],
    )
    assert "- What did you expect?" in ask and "@​bob" in ask and "Label the issue again" in ask
    assert "could not produce a result" in build_issue_comment(
        Outcome(status="no_result", reason="env broken", confidence="none"), info, None
    )
    done = build_issue_comment(VERIFIED, info, "https://github.com/o/r/pull/9")
    assert "Verified fix" in done and "pull/9" in done
