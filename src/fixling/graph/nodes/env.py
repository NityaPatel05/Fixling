"""``env``: build the environment, start the work sandbox and record the baseline (spec E7/E17)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig

from fixling.graph.deps import get_deps
from fixling.graph.nodes._common import node
from fixling.graph.state import RunState


@node("env")
def env(state: RunState, config: RunnableConfig) -> dict[str, Any]:
    deps = get_deps(config)
    run_id = state["run_id"]
    # Idempotent on resume: if the sandbox from a previous attempt is still alive, keep it and its baseline.
    existing = state.get("work_sandbox_id")
    if existing and state.get("baseline") and deps.sandboxes.get(run_id, existing) is not None:
        return {}
    result, sandbox = deps.sandboxes.prepare_env(run_id, state["repo"], Path(state["src_dir"]))
    if not result.ok or sandbox is None or result.baseline is None:
        return {"stop_reason": f"The environment could not be set up: {result.reason}"}
    return {
        "env_image": result.image,
        "work_sandbox_id": sandbox.id,
        "baseline": result.baseline.model_dump(),
    }
