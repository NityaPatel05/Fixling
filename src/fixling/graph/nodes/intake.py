"""``intake``: sanitize the untrusted issue text and extract an ``IssueSpec`` (spec E17)."""

from __future__ import annotations

import re
from typing import Any

from langchain_core.runnables import RunnableConfig

from fixling.errors import LLMFormatError, PromptTooLarge
from fixling.graph.deps import get_deps
from fixling.graph.nodes._common import node
from fixling.graph.state import RunState
from fixling.llm.render import fit, render, untrusted_issue
from fixling.models import IssueSpec
from fixling.repo.retrieval import extract_hints
from fixling.repo.sanitize import sanitize_report

_FENCE = re.compile(r"```[a-zA-Z]*\n(.*?)```", re.DOTALL)
BODY_LIMITS = (6000, 3000, 1500)


def fallback_spec(title: str, body: str) -> IssueSpec:
    """A deterministic spec for when the model cannot produce one: the run still proceeds, flagged as degraded."""
    snippets = [m.strip() for m in _FENCE.findall(body)][:3]
    symbols = extract_hints(f"{title}\n{body}", None).symbols[:8]
    return IssueSpec(
        summary=title.strip() or body.strip()[:200],
        kind="bug",
        expected_behavior="",
        actual_behavior=body.strip()[:300],
        repro_hints=snippets,
        mentioned_symbols=symbols,
        acceptance_criteria=[],
        ambiguity=5,
        size_estimate="small",
    )


@node("intake")
def intake(state: RunState, config: RunnableConfig) -> dict[str, Any]:
    deps = get_deps(config)
    run_id = state["run_id"]
    title_rep = sanitize_report(state.get("issue_title", ""))
    body_rep = sanitize_report(state.get("issue_body", ""))
    flags = list(state.get("flags", []))

    received = deps.events.append(
        run_id,
        "issue_received",
        "github" if state.get("issue_number") else "user",
        {
            "repo": state.get("repo"),
            "issue_number": state.get("issue_number"),
            "title": title_rep.text,
            "author": state.get("issue_author", "unknown"),
            "body_chars": len(body_rep.text),
        },
        node="intake",
    )
    removed: dict[str, int] = {}
    for rep in (title_rep, body_rep):
        for k, v in rep.removed.items():
            removed[k] = removed.get(k, 0) + v
    if removed:
        deps.events.append(
            run_id,
            "sanitize",
            "system",
            {
                "removed": removed,
                "hidden_text": [*title_rep.hidden_text, *body_rep.hidden_text],
                "suspicious": title_rep.suspicious or body_rep.suspicious,
            },
            node="intake",
            cause=received,
        )
    if title_rep.suspicious or body_rep.suspicious:
        flags.append("hidden_content")  # invisible content in an issue is a prompt-injection signal

    author = state.get("issue_author", "unknown")

    def build(level: int) -> list[dict[str, str]]:
        return render("intake", issue=untrusted_issue(title_rep.text, body_rep.text[: BODY_LIMITS[level]], author))

    degraded = False
    try:
        messages = fit(build, deps.llm.count_tokens, deps.cfg.models.max_prompt_tokens, levels=(0, 1, 2))
        spec = deps.llm.structured(run_id, "intake", IssueSpec, messages)
    except (LLMFormatError, PromptTooLarge) as exc:
        spec = fallback_spec(title_rep.text, body_rep.text)
        degraded = True
        flags.append("degraded_spec")
        deps.events.append(
            run_id, "error", "system", {"kind": "intake_fallback", "error": str(exc)}, node="intake", cause=received
        )
    deps.events.append(
        run_id, "spec", "llm", {**spec.model_dump(), "degraded": degraded}, node="intake", cause=received
    )
    return {
        "issue_title": title_rep.text,
        "issue_body_sanitized": body_rep.text,
        "spec": spec.model_dump(),
        "flags": flags,
    }
