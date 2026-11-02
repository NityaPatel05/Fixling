"""Repair loop: ``repair_fanout`` -> N x ``repair_one`` -> ``select`` -> (``refine``) (spec E17).

Each candidate is generated from a fresh short prompt and tested in its OWN sandbox, so candidates can neither see
nor disturb each other. Whole-function search/replace edits are applied with the forgiving matcher in
``repo/edits.py``; the candidate's patch is whatever ``git diff`` says changed.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.types import Send

from fixling.errors import BudgetExceeded, LLMFormatError, PromptTooLarge
from fixling.graph.deps import Deps, get_deps
from fixling.graph.helpers import (
    failure_summary,
    read_run_file,
    safe_name,
    short,
    spec_text,
    target_views,
    write_run_file,
)
from fixling.graph.nodes._common import node
from fixling.graph.selection import (
    changed_files,
    dedupe,
    diff_line_count,
    is_clean,
    is_viable,
    patch_hash,
    rank,
    tied_for_best,
)
from fixling.graph.state import RunState
from fixling.llm.render import fit, render, repo_file
from fixling.models import Candidate, GateFinding, JudgeVerdict, RepairProposal
from fixling.repo.edits import apply_edits
from fixling.verify.policy import GateLike, NullGate, PolicyGate
from fixling.verify.runner import record_test_run, run_tests
from fixling.verify.secrets import scan_secrets

CAND_EXCLUDE = {"proposal", "patch", "repro_run", "impacted_run"}
DIFF_IN_PROMPT = 3500


def cand_state(c: Candidate) -> dict[str, Any]:
    return c.model_dump(mode="json", exclude=CAND_EXCLUDE)


def cand_from_state(d: dict[str, Any]) -> Candidate:
    return Candidate.model_validate(d)


def _seed(run_id: str, cand_id: str) -> int:
    return int(hashlib.sha256(f"{run_id}/{cand_id}".encode()).hexdigest()[:8], 16) % 2**31


# ---------------------------------------------------------------- fan-out
def fanout(state: RunState, config: RunnableConfig) -> list[Send] | str:
    """Conditional edge: one ``Send`` per candidate, or straight to ``finalize`` when the run is stopping."""
    if state.get("stop_reason") or not state.get("targets"):
        return "finalize"
    deps = get_deps(config)
    n = deps.cfg.budgets.repair_candidates
    rnd = state.get("round", 0)
    temps = deps.cfg.models.node("repair").temps(n)
    keys = (
        "run_id",
        "repo",
        "src_dir",
        "issue_number",
        "spec",
        "targets",
        "repro",
        "baseline",
        "retrieval",
        "refine_context",
    )
    base = {k: state.get(k) for k in keys}
    return [
        Send("repair_one", {**base, "round": rnd, "temperature": t, "cand_id": f"r{rnd}c{i}"})
        for i, t in enumerate(temps)
    ]


# ---------------------------------------------------------------- one candidate
@node("repair_one")
def repair_one(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
    deps = get_deps(config)
    run_id, cid = state["run_id"], state["cand_id"]
    cand = Candidate(id=cid, round=state["round"], temperature=state["temperature"])
    proposal, error = _propose(deps, state, cand)
    patch = ""
    if proposal is not None:
        patch = _apply_and_test(deps, state, cand, proposal)
    cand.patch_hash = patch_hash(patch) if patch.strip() else None
    cand.diff_lines = diff_line_count(patch)
    cand.files = changed_files(patch)
    if patch.strip():
        cand.patch_path = write_run_file(deps, run_id, f"candidates/{safe_name(cid)}.diff", patch)
    elif not (cand.apply_error or cand.llm_error):
        cand.apply_error = "the proposal changed nothing"
    artifact = (
        "### PROPOSAL\n" + (proposal.model_dump_json(indent=2) if proposal else str(error)) + "\n\n### PATCH\n" + patch
    )
    cand.event_seq = deps.events.append(
        run_id,
        "candidate",
        "llm",
        {
            "id": cid,
            "round": cand.round,
            "temperature": cand.temperature,
            "llm_error": cand.llm_error,
            "apply_error": cand.apply_error,
            "patch_hash": cand.patch_hash,
            "diff_lines": cand.diff_lines,
            "files": cand.files,
            "repro_passed": cand.repro_passed,
            "regressions": cand.regressions,
            "junit_ok": cand.junit_ok,
        },
        node="repair_one",
        artifact_text=artifact,
    )
    return {"candidates": [cand_state(cand)]}


def _propose(deps: Deps, state: dict[str, Any], cand: Candidate) -> tuple[RepairProposal | None, str | None]:
    targets, repro, ctx = state["targets"], state.get("repro"), state.get("refine_context")
    text = spec_text(state["spec"])

    def build(level: int) -> list[dict[str, str]]:
        shown = targets[: (len(targets), 2, 1)[level]]
        repro_code = repo_file(repro["path"], repro["code"], numbered=False) if repro and level < 2 else None
        if ctx:
            return render(
                "refine",
                spec=text,
                targets=target_views(shown),
                repro_test=repro_code,
                previous_diff=short(ctx["previous_diff"], DIFF_IN_PROMPT if level == 0 else 1200),
                previous_failure=short(ctx["previous_failure"], 1200 if level == 0 else 500),
            )
        return render(
            "repair",
            spec=text,
            targets=target_views(shown),
            repro_test=repro_code,
            failure=short(repro["failure_output"], 1200 if level == 0 else 500) if repro else "",
        )

    try:
        messages = fit(build, deps.llm.count_tokens, deps.cfg.models.max_prompt_tokens, range(3))
        proposal = deps.llm.structured(
            state["run_id"],
            "repair",
            RepairProposal,
            messages,
            temperature=cand.temperature,
            seed=_seed(state["run_id"], cand.id),
        )
        return proposal, None
    except BudgetExceeded:
        raise
    except (LLMFormatError, PromptTooLarge) as exc:
        cand.llm_error = str(exc)[:300]
        return None, cand.llm_error


def _apply_and_test(deps: Deps, state: dict[str, Any], cand: Candidate, proposal: RepairProposal) -> str:
    run_id, repro = state["run_id"], state.get("repro")
    extra = {repro["path"]: repro["code"]} if repro else None
    sandbox = deps.sandboxes.create(run_id, "cand", Path(state["src_dir"]), state["repo"], extra)
    try:
        allowed = {t["path"] for t in state["targets"]}
        applied = apply_edits(sandbox, proposal.edits[:4], allowed)
        if applied.errors:
            cand.apply_error = " | ".join(applied.errors)[:600]
        if applied.applied == 0:
            return ""
        patch = sandbox.diff()
        if not patch.strip():
            return ""
        if scan_secrets(patch, deps.cfg.secrets()):
            # Never let a credential reach disk or a PR: drop the patch itself, not just the finding.
            cand.apply_error = "the edit would add a credential to the code; never write secrets into source files"
            return ""
        _run_tests_for(deps, state, cand, sandbox)
        return patch
    finally:
        sandbox.close()


def _run_tests_for(deps: Deps, state: dict[str, Any], cand: Candidate, sandbox: Any) -> None:
    baseline = state.get("baseline") or {}
    passing = set(baseline.get("passed", []))
    repro = state.get("repro")
    repro_ids = list(repro["test_ids"]) if repro else []
    impacted = [t for t in (state.get("retrieval") or {}).get("related_tests", []) if t in passing]
    ids = list(dict.fromkeys([*repro_ids, *impacted]))
    run = run_tests(sandbox, ids or None, timeout=deps.cfg.budgets.test_timeout_s)
    record_test_run(deps.events, state["run_id"], "repair_one", run, f"candidate {cand.id}")
    cand.junit_ok = run.junit_ok
    if not run.junit_ok:
        cand.repro_passed = False if repro else None
        cand.failure_text = deps.events.redact(short(run.output, 1200))
        return
    passed = set(run.passed)
    watched = impacted if ids else sorted(passing)  # no targeted ids: the whole suite ran
    cand.regressions = [t for t in watched if t not in passed]
    if repro:
        cand.repro_passed = all(t in passed for t in repro_ids)
    failing = [t for t in [*repro_ids, *cand.regressions] if t not in passed]
    cand.failure_text = deps.events.redact(failure_summary(run.failure_details, failing, 1500))


# ---------------------------------------------------------------- select
def _judge_text(findings: list[GateFinding]) -> str:
    return "; ".join(f"{f.severity}:{f.rule}" for f in findings) or "none"


def _tests_text(c: Candidate, has_repro: bool) -> str:
    parts = []
    if has_repro:
        parts.append("reproduction test " + ("passes" if c.repro_passed else "FAILS"))
    parts.append("no regressions" if not c.regressions else f"{len(c.regressions)} regressions")
    return ", ".join(parts)


def _judge(deps: Deps, state: RunState, tied: list[Candidate], has_repro: bool) -> tuple[Candidate, dict[str, Any]]:
    """Tie-break only: the same small model is not independent evidence, so it never overrides test results."""
    verdicts: list[tuple[tuple[bool, bool, bool, int], Candidate, dict[str, Any]]] = []
    for c in tied[:2]:
        diff = short(read_run_file(deps, state["run_id"], c.patch_path or ""), DIFF_IN_PROMPT)
        messages = render(
            "judge",
            spec=spec_text(state["spec"]),
            diff=diff,
            findings=_judge_text(c.findings),
            tests=_tests_text(c, has_repro),
        )
        try:
            v = deps.llm.structured(state["run_id"], "judge", JudgeVerdict, messages)
        except (LLMFormatError, PromptTooLarge):
            continue
        key = (v.fix_is_general, not v.special_cases_inputs, not v.modifies_tests_to_pass, v.confidence)
        verdicts.append((key, c, v.model_dump()))
        deps.events.append(state["run_id"], "judge", "llm", {"candidate": c.id, **v.model_dump()}, node="select")
    if not verdicts:
        return tied[0], {}
    verdicts.sort(key=lambda t: t[0], reverse=True)
    return verdicts[0][1], {**verdicts[0][2], "candidate": verdicts[0][1].id}


@node("select")
def select(state: RunState, config: RunnableConfig) -> dict[str, Any]:
    deps = get_deps(config)
    run_id, rnd = state["run_id"], state.get("round", 0)
    repro = state.get("repro")
    has_repro = bool(repro)
    cands = [cand_from_state(c) for c in state.get("candidates", []) if c["round"] == rnd]
    gate: GateLike = (
        PolicyGate(
            deps.cfg.policy,
            base_dir=Path(state["src_dir"]),
            repro_source=repro["code"] if repro else None,
            secrets=deps.cfg.secrets(),
        )
        if deps.gates_enabled
        else NullGate()
    )

    for c in cands:
        if c.patch_path:
            c.findings = gate.check_diff(read_run_file(deps, run_id, c.patch_path))
            for f in c.findings:
                deps.events.append(
                    run_id,
                    "gate_finding",
                    "system",
                    {"candidate": c.id, **f.model_dump()},
                    node="select",
                    cause=c.event_seq,
                )
    ranked = rank(dedupe(cands), has_repro=has_repro)

    best: Candidate | None = ranked[0] if ranked else None
    judged: dict[str, Any] = {}
    tied = tied_for_best(ranked)
    if len(tied) >= 2:
        best, judged = _judge(deps, state, tied, has_repro)
    viable_best = best if best is not None and is_viable(best, has_repro=has_repro) else None

    budgets = deps.cfg.budgets
    if best and is_clean(best, has_repro=has_repro):
        route = "clean_verify"
    elif rnd < budgets.refine_rounds:
        route = "refine"
    elif viable_best:
        route = "clean_verify"
    else:
        route = "finalize"

    summary = [
        {
            "id": c.id,
            "score": c.score,
            "agreement": c.agreement,
            "repro_passed": c.repro_passed,
            "regressions": len(c.regressions),
            "findings": [f"{f.severity}:{f.rule}" for f in c.findings],
            "apply_error": bool(c.apply_error),
            "llm_error": bool(c.llm_error),
        }
        for c in ranked
    ]
    deps.events.append(
        run_id,
        "selection",
        "system",
        {
            "round": rnd,
            "ranked": summary,
            "best": best.id if best else None,
            "route": route,
            "judge_used": bool(judged),
        },
        node="select",
    )
    out: dict[str, Any] = {
        "best_candidate_id": best.id if best else None,
        "best_patch_path": best.patch_path if best else None,
        "selection": {"route": route, "round": rnd, "viable": viable_best is not None},
        "best": cand_state(best) if best else None,
    }
    if judged:
        out["judge"] = judged
    return out


# ---------------------------------------------------------------- refine
def failure_reason(c: Candidate) -> str:
    if c.llm_error:
        return "the model returned output that could not be parsed"
    if c.apply_error and not c.patch_path:
        return f"the edits could not be applied: {c.apply_error}"
    parts: list[str] = []
    if c.apply_error:
        parts.append(f"some edits could not be applied ({c.apply_error[:200]})")
    blocks = [f for f in c.findings if f.severity == "block"]
    if blocks:
        parts.append("rejected by integrity rules: " + "; ".join(f"{f.rule} ({f.detail})" for f in blocks[:3]))
    if c.repro_passed is False:
        parts.append("the failing test still fails")
    if c.regressions:
        parts.append("these previously passing tests now fail: " + ", ".join(c.regressions[:5]))
    if c.failure_text:
        parts.append(c.failure_text)
    return "\n".join(parts) or "the candidate did not pass verification"


@node("refine")
def refine(state: RunState, config: RunnableConfig) -> dict[str, Any]:
    deps = get_deps(config)
    best = state.get("best")
    c = cand_from_state(best) if best else None
    diff = "(no valid patch was produced)"
    if c and c.patch_path:
        diff = short(read_run_file(deps, state["run_id"], c.patch_path), DIFF_IN_PROMPT)
    ctx = {
        "candidate_id": c.id if c else None,
        "previous_diff": diff,
        "previous_failure": failure_reason(c) if c else "no candidate produced a usable edit",
    }
    deps.events.append(
        state["run_id"],
        "refine",
        "system",
        {
            "refine_round": state.get("round", 0) + 1,
            "from_candidate": ctx["candidate_id"],
            "reason": str(ctx["previous_failure"])[:300],
        },
        node="refine",
    )
    return {"round": state.get("round", 0) + 1, "refine_context": ctx}


def route_select(state: RunState) -> str:
    if state.get("stop_reason"):
        return "finalize"
    return str((state.get("selection") or {}).get("route", "finalize"))
