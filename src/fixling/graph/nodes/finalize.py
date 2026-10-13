"""``finalize``: always runs, never raises (spec E17).

Decides the outcome from recorded facts, writes ``patch.diff`` and ``report.md``, optionally publishes a draft PR
or issue comment, closes every sandbox of the run and records the result. Each step is isolated so that one
failure (say, GitHub being down) cannot lose the others.
"""

from __future__ import annotations

import traceback
from datetime import UTC, datetime
from typing import Any

from langchain_core.runnables import RunnableConfig

from fixling.gh.pr_body import RunInfo, build_issue_comment, build_pr_body
from fixling.graph.deps import Deps, get_deps
from fixling.graph.helpers import read_run_file, write_run_file
from fixling.graph.nodes._common import node
from fixling.graph.nodes.verify import final_patch
from fixling.graph.state import RunState
from fixling.models import GateFinding, Outcome
from fixling.verify.confidence import Facts, decide_outcome


def _findings(items: list[dict[str, Any]]) -> list[GateFinding]:
    return [GateFinding.model_validate(i) for i in items if isinstance(i, dict)]


def build_facts(state: RunState) -> Facts:
    """Everything ``decide_outcome`` may use, taken from state only (which is itself built from events)."""
    flags = list(state.get("flags", []))
    triage = state.get("triage") or {}
    verify = state.get("verify")
    best = state.get("best") or {}
    judge = state.get("judge") or {}
    strength = state.get("strength") or {}

    findings: list[GateFinding] = []
    if verify:  # only the verified candidate's findings matter once it reached clean verification
        findings += _findings(best.get("findings", []))
        findings += _findings(verify.get("findings", []))
    if "hidden_content" in flags:
        findings.append(
            GateFinding(
                rule="hidden_content", severity="flag", path="issue", detail="hidden text was removed from the issue"
            )
        )
    unique = {(f.rule, f.path, f.detail): f for f in findings}

    judge_flags: list[str] = []
    if judge:
        for key, label in (
            ("special_cases_inputs", "special-cases inputs"),
            ("modifies_tests_to_pass", "touches tests"),
        ):
            if judge.get(key):
                judge_flags.append(label)
        if judge.get("fix_is_general") is False:
            judge_flags.append("not general")
    clarify = None
    if triage.get("decision") == "clarify":
        clarify = "The issue needs more detail before it can be acted on: " + "; ".join(triage.get("reasons", []))
    status = strength.get("status")
    return Facts(
        clarify_reason=clarify,
        stop_reason=state.get("stop_reason"),
        has_repro=bool(state.get("repro")),
        has_candidate=verify is not None,
        f2p_ok=bool(verify and verify.get("f2p_ok")),
        p2p_ok=bool(verify and verify.get("p2p_ok")),
        regressions=list((verify or {}).get("regressions", [])),
        new_skips=list((verify or {}).get("new_skips", [])),
        shuffled_ok=(verify or {}).get("shuffled_ok"),
        findings=list(unique.values()),
        agreement=int(best.get("agreement", 0)) + 1,
        extra_tests=status if status in ("pass", "fail") else None,
        judge_confidence=judge.get("confidence") if judge else None,
        judge_concerns=list(judge.get("concerns", [])) if judge else [],
        judge_flags=judge_flags,
    )


def outcome_patch(deps: Deps, state: RunState, outcome: Outcome) -> str:
    """The patch that ships with this outcome: fix + test, the test alone, or nothing."""
    try:
        if outcome.status in ("verified_fix", "unverified_fix"):
            return final_patch(deps, state)
        if outcome.status == "repro_only" and state.get("repro_patch_path"):
            return read_run_file(deps, state["run_id"], state["repro_patch_path"] or "")
    except OSError:
        return ""
    return ""


def _run_info(deps: Deps, state: RunState) -> RunInfo:
    run = deps.runs.get_run(state["run_id"]) or {}
    started = run.get("started_at")
    wall_ms = 0
    if started:
        wall_ms = int((datetime.now(UTC) - datetime.fromisoformat(started)).total_seconds() * 1000)
    return RunInfo(
        repo=state.get("repo", ""),
        issue_number=state.get("issue_number"),
        run_id=state["run_id"],
        model=deps.cfg.models.main_model,
        tokens_in=int(run.get("tokens_in") or 0),
        tokens_out=int(run.get("tokens_out") or 0),
        llm_ms=int(run.get("llm_ms") or 0),
        wall_ms=wall_ms,
        dashboard_url=deps.cfg.settings.fixling_dashboard_url,
    )


def _error(deps: Deps, run_id: str, step: str, exc: Exception) -> None:
    deps.events.append(
        run_id,
        "error",
        "system",
        {"kind": f"finalize:{step}", "error": f"{type(exc).__name__}: {exc}"},
        node="finalize",
        artifact_text=traceback.format_exc(),
    )


@node("finalize")
def finalize(state: RunState, config: RunnableConfig) -> dict[str, Any]:
    deps = get_deps(config)
    run_id = state["run_id"]
    try:
        facts = build_facts(state)
        outcome = decide_outcome(facts)
    except Exception as exc:  # decision logic must never take the run down
        _error(deps, run_id, "decide", exc)
        outcome = Outcome(
            status="no_result", reason=f"Internal error while deciding the outcome: {exc}", confidence="none"
        )

    patch = outcome_patch(deps, state, outcome)
    if patch:
        write_run_file(deps, run_id, "patch.diff", patch)
    outcome_seq = deps.events.append(
        run_id,
        "outcome",
        "system",
        {**outcome.model_dump(), "flags": list(state.get("flags", [])), "patch_lines": patch.count("\n")},
        node="finalize",
    )

    body = ""
    try:
        info = _run_info(deps, state)
        body = build_pr_body(deps.events.list(run_id), outcome, info, patch, flags=list(state.get("flags", [])))
        write_run_file(deps, run_id, "report.md", body)
    except Exception as exc:
        _error(deps, run_id, "report", exc)

    pr_url = (deps.runs.get_run(run_id) or {}).get("pr_url") or state.get("pr_url")
    if deps.publisher is not None and not pr_url:
        try:
            comment = build_issue_comment(
                outcome, _run_info(deps, state), None, list((state.get("triage") or {}).get("questions", []))
            )
            pr_url = deps.publisher.publish(
                run_id, {**state, "outcome": outcome.model_dump(), "pr_body": body, "issue_comment": comment}, patch
            )
        except Exception as exc:
            _error(deps, run_id, "publish", exc)

    try:
        deps.sandboxes.close_run(run_id)
    except Exception as exc:
        _error(deps, run_id, "close_sandboxes", exc)

    try:
        deps.runs.update_run(
            run_id,
            status="done",
            outcome=outcome.status,
            confidence=outcome.confidence,
            pr_url=pr_url,
            finished_at=datetime.now(UTC).isoformat(),
        )
    except Exception as exc:
        _error(deps, run_id, "record", exc)
    return {"outcome": {**outcome.model_dump(), "event": outcome_seq}, "pr_url": pr_url}
