"""PR body / local report as a pure function of the event log and the outcome (spec E19).

No LLM is involved and nothing is invented: every evidence line cites the event it came from, ``Fixes #N`` appears
only for a verified fix, and any model-originated text is stripped of links, images, HTML and @mentions first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from fixling.models import Event, Outcome
from fixling.verify.confidence import status_label

_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_HTML = re.compile(r"<[^>\n]{1,200}>")
_URL = re.compile(r"https?://\S+")
_MENTION = re.compile(r"@(?=[A-Za-z0-9_-])")
ZWSP = chr(0x200B)


def clean_text(text: str, limit: int = 300) -> str:
    """Neutralise model-originated text before it is posted to GitHub."""
    text = _IMAGE.sub("", text)
    text = _LINK.sub(r"\1", text)
    text = _HTML.sub("", text)
    text = _URL.sub("[link removed]", text)
    text = _MENTION.sub("@" + ZWSP, text)
    text = " ".join(text.replace("`", "'").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass
class RunInfo:
    repo: str
    issue_number: int | None
    run_id: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    llm_ms: int = 0
    wall_ms: int = 0
    dashboard_url: str = "http://localhost:8000"


def _cite(*seqs: int | None) -> str:
    nums = sorted({s for s in seqs if s})
    if not nums:
        return ""
    return f" [event #{nums[0]}]" if len(nums) == 1 else f" [events #{nums[0]} to #{nums[-1]}]"


def numstat(patch: str) -> list[tuple[str, int, int]]:
    """``(path, added, removed)`` per file from a unified diff."""
    rows: list[tuple[str, int, int]] = []
    path, add, rem = None, 0, 0
    for line in patch.split("\n"):
        if line.startswith("diff --git "):
            if path is not None:
                rows.append((path, add, rem))
            m = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            path, add, rem = (m.group(2) if m else line[11:]), 0, 0
        elif line.startswith("+") and not line.startswith("+++"):
            add += 1
        elif line.startswith("-") and not line.startswith("---"):
            rem += 1
    if path is not None:
        rows.append((path, add, rem))
    return rows


def _last(events: list[Event], type_: str) -> Event | None:
    return next((e for e in reversed(events) if e.type == type_), None)


def _by_type(events: list[Event], type_: str) -> list[Event]:
    return [e for e in events if e.type == type_]


def _first_line(text: str) -> str:
    for ln in text.splitlines():
        if ln.strip():
            return ln.strip()
    return ""


def _fmt_ms(ms: int) -> str:
    s = ms / 1000
    return f"{s:.0f}s" if s < 120 else f"{s / 60:.1f} min"


def evidence_lines(events: list[Event], outcome: Outcome) -> list[str]:
    lines: list[str] = []
    repro = next((e for e in events if e.type == "repro" and e.payload.get("ok")), None)
    if repro:
        ids = ", ".join(f"`{t}`" for t in repro.payload.get("test_ids", [])[:3])
        failure = clean_text(_first_line(str(repro.payload.get("failure", ""))), 160)
        lines.append(f"Reproduced before the fix: {ids} → FAIL ({failure}){_cite(repro.seq)}")
    elif any(e.type == "repro" for e in events):
        e = next(e for e in events if e.type == "repro")
        lines.append(
            f"No valid reproduction test could be written ({e.payload.get('attempts', '?')} attempts){_cite(e.seq)}"
        )

    verify = _last(events, "verify")
    if verify:
        p = verify.payload
        lines.append(
            f"After the fix (clean container): full suite {p.get('passed', 0)} passed, "
            f"{p.get('failed', 0)} failed, {p.get('skipped', 0)} skipped{_cite(verify.seq)}"
        )
        if p.get("shuffled_ok") is not None:
            lines.append("Shuffled test order: " + ("passed" if p["shuffled_ok"] else "FAILED") + _cite(verify.seq))
        if p.get("regressions"):
            lines.append(f"Regressions: {', '.join('`' + t + '`' for t in p['regressions'][:5])}{_cite(verify.seq)}")

    cands = _by_type(events, "candidate")
    if cands:
        passing = sum(1 for e in cands if e.payload.get("repro_passed"))
        sel = _last(events, "selection")
        best_agreement = 0
        if sel:
            ranked = sel.payload.get("ranked", [])
            best_agreement = max((r.get("agreement", 0) for r in ranked[:1]), default=0)
        extra = f", {best_agreement + 1} identical" if best_agreement else ""
        lines.append(
            f"Candidates tried: {len(cands)} ({passing} passed the reproduction test{extra})"
            f"{_cite(cands[0].seq, cands[-1].seq)}"
        )
    strength = _last(events, "strength")
    if strength and strength.payload.get("status"):
        lines.append(
            f"Independent tests (written without seeing the patch): {strength.payload['status']}{_cite(strength.seq)}"
        )
    return lines


def integrity_lines(events: list[Event], outcome: Outcome) -> list[str]:
    """One line summarising the test-integrity gate, from ``gate_finding`` and ``verify`` events."""
    found: list[dict[str, Any]] = [e.payload for e in _by_type(events, "gate_finding")]
    verify = _last(events, "verify")
    if verify:
        found += [f for f in verify.payload.get("findings", []) if isinstance(f, dict)]
    blocks = [f for f in found if f.get("severity") == "block"]
    flags = sorted({str(f.get("rule", "")) for f in found if f.get("severity") == "flag"})
    protected_ok = not any(f.get("rule") == "protected_path" for f in blocks)
    return [
        " · ".join(
            [
                "Protected files untouched " + ("✓" if protected_ok else "✗"),
                "Test gate: " + ("no blocking findings ✓" if not blocks else f"{len(blocks)} blocking finding(s) ✗"),
                "Flags: " + (", ".join(flags) if flags else "none"),
            ]
        )
    ]


def caveats(events: list[Event], outcome: Outcome, flags: list[str]) -> list[str]:
    out: list[str] = []
    if outcome.status == "repro_only":
        out.append("No fix was found. This draft only adds a failing test that reproduces the issue.")
    if outcome.status == "unverified_fix":
        out.append(
            "This fix is not fully verified. Review it carefully before merging: " + clean_text(outcome.reason, 400)
        )
    if "no_repro" in flags:
        out.append("A failing reproduction test could not be written, so this change is unproven.")
    if "degraded_spec" in flags:
        out.append("The issue could not be summarised by the model; a simpler heuristic summary was used.")
    if "hidden_content" in flags:
        out.append("Invisible or hidden content was found in the issue text and removed before processing.")
    out.append("Written by a small local model; always review the diff.")
    return out


def build_pr_body(
    events: list[Event], outcome: Outcome, info: RunInfo, patch: str, *, flags: list[str] | None = None
) -> str:
    flags = flags or []
    spec = next((e for e in events if e.type == "spec"), None)
    summary = clean_text(str(spec.payload.get("summary", "")), 400) if spec else ""
    n = info.issue_number
    link = ""
    if n:
        link = f"Fixes #{n}" if outcome.status == "verified_fix" else f"Related to #{n}"

    out: list[str] = [f"## Summary\n\n{summary or '(no summary)'}" + (f"\n\n{link}" if link else "")]
    rows = numstat(patch)
    if rows:
        table = "\n".join(f"| `{p}` | +{a} | -{r} |" for p, a, r in rows)
        out.append("## Changes\n\n| File | Added | Removed |\n|---|---|---|\n" + table)

    ev = evidence_lines(events, outcome)
    out.append("## Evidence\n\n" + ("\n".join(f"- {line}" for line in ev) if ev else "- (none recorded)"))
    out.append("## Integrity checks\n\n" + "\n".join(f"- {line}" for line in integrity_lines(events, outcome)))

    level = outcome.confidence.upper() if outcome.confidence != "none" else "NONE"
    reasons = outcome.confidence_reasons or [clean_text(outcome.reason, 400)]
    out.append(f"## Confidence: {level}\n\n" + "\n".join(f"- {clean_text(r, 400)}" for r in reasons))
    out.append("## Caveats\n\n" + "\n".join(f"- {c}" for c in caveats(events, outcome, flags)))
    out.append(
        "## Run info\n\n"
        f"- Outcome: {status_label(outcome.status)}\n"
        f"- Model: `{info.model}`; tokens in/out: {info.tokens_in}/{info.tokens_out}; "
        f"LLM time {_fmt_ms(info.llm_ms)}; wall time {_fmt_ms(info.wall_ms)}\n"
        f"- Run `{info.run_id}` · dashboard: {info.dashboard_url}/runs/{info.run_id}"
    )
    return "\n\n".join(out) + "\n"


def build_issue_comment(outcome: Outcome, info: RunInfo, pr_url: str | None, questions: list[str] | None = None) -> str:
    """Comment for outcomes that do not produce a PR (and a pointer for those that do)."""
    if outcome.status == "needs_clarification":
        qs = "\n".join(f"- {clean_text(q, 200)}" for q in (questions or []))
        return (
            "Fixling could not act on this issue yet: " + clean_text(outcome.reason, 400) + "\n\n"
            "Could you help with the following?\n\n" + qs + "\n\nLabel the issue again once it is updated.\n"
        )
    if outcome.status == "no_result":
        return (
            f"Fixling could not produce a result: {clean_text(outcome.reason, 400)}\n\n"
            f"Run `{info.run_id}` has the details in the dashboard.\n"
        )
    where = f"Draft PR: {pr_url}" if pr_url else "No PR was created."
    return f"Fixling finished with **{status_label(outcome.status)}**. {where}\n"
