"""``triage``: stop early when an issue is too vague or large to act on (spec E17).

In interactive mode the run pauses with ``interrupt()`` and a human can approve, edit the spec, or cancel. In
automatic mode the run ends with ``needs_clarification`` and the questions are posted to the issue.
"""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt
from pydantic import ValidationError

from fixling.graph.deps import Deps, get_deps, get_option
from fixling.graph.nodes._common import node
from fixling.graph.state import RunState
from fixling.models import IssueSpec

AMBIGUITY_LIMIT = 7


def triage_reasons(spec: IssueSpec) -> list[str]:
    reasons: list[str] = []
    if spec.ambiguity >= AMBIGUITY_LIMIT:
        reasons.append(f"the issue is too ambiguous to act on (ambiguity {spec.ambiguity}/10)")
    if spec.size_estimate == "large":
        reasons.append("the change looks too large for an automatic fix")
    if spec.kind == "other":
        reasons.append("the issue does not look like a bug report")
    return reasons


def clarifying_questions(spec: IssueSpec) -> list[str]:
    qs: list[str] = []
    if not spec.expected_behavior.strip():
        qs.append("What is the behaviour you expect?")
    if not spec.repro_hints:
        qs.append("Can you share a minimal example (code or steps) that reproduces the problem?")
    if not spec.mentioned_symbols:
        qs.append("Which function, class or file is affected?")
    if spec.size_estimate == "large":
        qs.append("Can the request be narrowed to a single function or behaviour?")
    if spec.kind == "other":
        qs.append("Is this a bug in existing behaviour? Fixling only handles bug reports.")
    return qs or ["Could you describe the expected and actual behaviour with a small example?"]


def _emit_once(deps: Deps, run_id: str, type_: str, key: str, payload: dict[str, Any]) -> None:
    """Nodes re-run from the top after an interrupt, so events emitted before ``interrupt()`` must be idempotent."""
    for ev in deps.events.list(run_id):
        if ev.type == type_ and ev.payload.get("key") == key:
            return
    deps.events.append(run_id, type_, "system", {**payload, "key": key}, node="triage")


@node("triage")
def triage(state: RunState, config: RunnableConfig) -> dict[str, Any]:
    deps = get_deps(config)
    run_id = state["run_id"]
    spec = IssueSpec.model_validate(state["spec"])
    flags = list(state.get("flags", []))
    reasons = triage_reasons(spec)
    questions = clarifying_questions(spec) if reasons else []
    decision = "clarify" if reasons else "proceed"

    if decision == "clarify" and get_option(config, "interactive", False):
        _emit_once(
            deps,
            run_id,
            "interrupt",
            "triage-clarify",
            {"reasons": reasons, "questions": questions, "spec": spec.model_dump()},
        )
        answer = interrupt(
            {"type": "clarify", "run_id": run_id, "reasons": reasons, "questions": questions, "spec": spec.model_dump()}
        )
        action = (answer or {}).get("action", "approve") if isinstance(answer, dict) else "approve"
        _emit_once(deps, run_id, "resume", "triage-resume", {"action": action})
        if action == "cancel":
            return {"stop_reason": "Cancelled by the reviewer while waiting for clarification."}
        if action == "edit_spec":
            try:
                spec = IssueSpec.model_validate((answer or {}).get("spec") or {})
            except ValidationError as exc:
                return {"stop_reason": f"The edited spec was invalid: {exc.errors()[0]['msg']}"}
        decision = "proceed"
        flags.append("human_approved")

    if "hidden_content" in flags:
        reasons = [*reasons, "hidden content was removed from the issue text"]
    deps.events.append(
        run_id,
        "triage",
        "system",
        {
            "decision": decision,
            "reasons": reasons,
            "questions": questions if decision == "clarify" else [],
            "ambiguity": spec.ambiguity,
            "kind": spec.kind,
            "size": spec.size_estimate,
            "flags": flags,
        },
        node="triage",
    )
    return {
        "spec": spec.model_dump(),
        "flags": flags,
        "triage": {"decision": decision, "reasons": reasons, "questions": questions if decision == "clarify" else []},
    }
