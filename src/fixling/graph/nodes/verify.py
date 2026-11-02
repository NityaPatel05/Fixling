"""``clean_verify`` and ``strength`` nodes (spec E12/E13/E17)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig

from fixling.graph.deps import get_deps
from fixling.graph.helpers import combine_patches, read_run_file, write_run_file
from fixling.graph.nodes._common import node
from fixling.graph.state import RunState
from fixling.verify.clean_verify import clean_verify as run_clean_verify
from fixling.verify.policy import GateLike, NullGate, PolicyGate
from fixling.verify.strength import generate_extra_tests, run_extra_tests


def final_patch(deps: Any, state: RunState) -> str:
    """Reproduction test (a new file in the generated-test directory) plus the winning fix, as one valid patch."""
    run_id = state["run_id"]
    repro_path, fix_path = state.get("repro_patch_path"), state.get("best_patch_path")
    repro = read_run_file(deps, run_id, repro_path) if repro_path else ""
    fix = read_run_file(deps, run_id, fix_path) if fix_path else ""
    return combine_patches(repro, fix)


@node("clean_verify")
def clean_verify(state: RunState, config: RunnableConfig) -> dict[str, Any]:
    deps = get_deps(config)
    run_id = state["run_id"]
    repro = state.get("repro")
    patch = final_patch(deps, state)
    write_run_file(deps, run_id, "final.diff", patch)
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
    sandbox = deps.sandboxes.create(run_id, "verify", Path(state["src_dir"]), state["repo"])
    try:
        result = run_clean_verify(
            sandbox=sandbox,
            patch=patch,
            gate=gate,
            baseline=state.get("baseline") or {},
            repro_ids=list(repro["test_ids"]) if repro else [],
            budgets=deps.cfg.budgets,
            events=deps.events,
            run_id=run_id,
        )
    finally:
        sandbox.close()
    full = result.full_run
    summary = {
        "f2p_ok": result.f2p_ok,
        "p2p_ok": result.p2p_ok,
        "regressions": result.regressions,
        "new_skips": result.new_skips,
        "shuffled_ok": result.shuffled_ok,
        "findings": [f.model_dump() for f in result.findings],
        "collected": full.collected,
        "passed": len(full.passed),
        "failed": len(full.failed) + len(full.errors),
        "skipped": len(full.skipped),
        "junit_ok": full.junit_ok,
    }
    deps.events.append(run_id, "verify", "sandbox", summary, node="clean_verify")
    return {"verify": summary}


def route_verify(state: RunState, config: RunnableConfig) -> str:
    if state.get("stop_reason"):
        return "finalize"
    return "strength" if get_deps(config).strength and state.get("verify") else "finalize"


@node("strength")
def strength(state: RunState, config: RunnableConfig) -> dict[str, Any]:
    deps = get_deps(config)
    run_id = state["run_id"]
    verify = state.get("verify") or {}
    if not (verify.get("f2p_ok") and verify.get("p2p_ok")):
        return {"strength": {"status": None, "reason": "skipped: the fix was not verified"}}
    code = generate_extra_tests(deps.llm, run_id, state["spec"], state["targets"], deps.cfg.models.max_prompt_tokens)
    if code is None:
        result: dict[str, Any] = {"status": None, "reason": "no acceptable independent tests could be generated"}
    else:
        src = Path(state["src_dir"])
        base_sb = deps.sandboxes.create(run_id, "strength-base", src, state["repo"])
        patched_sb = deps.sandboxes.create(run_id, "strength-patched", src, state["repo"])
        try:
            result = run_extra_tests(
                base_sandbox=base_sb,
                patched_sandbox=patched_sb,
                code=code,
                patch=final_patch(deps, state),
                events=deps.events,
                run_id=run_id,
                timeout=deps.cfg.budgets.test_timeout_s,
            )
        finally:
            base_sb.close()
            patched_sb.close()
    deps.events.append(run_id, "strength", "sandbox", result, node="strength")
    return {"strength": result}
