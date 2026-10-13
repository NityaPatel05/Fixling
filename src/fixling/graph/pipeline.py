"""Running, interrupting and resuming the graph for one run (spec E18/E20)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from fixling.graph.build import build_graph
from fixling.graph.deps import Deps
from fixling.runs import now_iso

RECURSION_LIMIT = 100


def make_checkpointer(path: Path) -> SqliteSaver:
    path.parent.mkdir(parents=True, exist_ok=True)
    return SqliteSaver(sqlite3.connect(str(path), check_same_thread=False))


def graph_config(deps: Deps, run_id: str, *, interactive: bool = False) -> RunnableConfig:
    return {
        "configurable": {"thread_id": run_id, "deps": deps, "interactive": interactive},
        "recursion_limit": RECURSION_LIMIT,
    }


class Pipeline:
    """One compiled graph plus the bookkeeping around invoke / interrupt / resume."""

    def __init__(self, deps: Deps, checkpointer: SqliteSaver | None = None) -> None:
        self.deps = deps
        self.checkpointer = checkpointer or make_checkpointer(deps.cfg.checkpoint_path)
        self.graph = build_graph(self.checkpointer)

    # ------------------------------------------------------------------ start
    def start(self, init_state: dict[str, Any], *, interactive: bool = False) -> dict[str, Any]:
        run_id = init_state["run_id"]
        # Redact before anything is checkpointed: the LangGraph checkpoint DB stores state verbatim.
        for key in ("issue_title", "issue_body"):
            if isinstance(init_state.get(key), str):
                init_state[key] = self.deps.events.redact(init_state[key])
        self.deps.runs.update_run(run_id, status="running", started_at=now_iso())
        self.deps.events.append(
            run_id,
            "run_started",
            "system",
            {
                "repo": init_state.get("repo"),
                "issue_number": init_state.get("issue_number"),
                "interactive": interactive,
            },
        )
        return self._invoke(init_state, run_id, interactive)

    # ------------------------------------------------------------------ resume
    def resume(self, run_id: str, value: Any, *, interactive: bool = True) -> dict[str, Any]:
        """Continue a run paused by ``interrupt()`` with the human's answer."""
        self.deps.runs.update_run(run_id, status="running")
        return self._invoke(Command(resume=value), run_id, interactive)

    def recover(self, run_id: str, *, interactive: bool = False) -> dict[str, Any]:
        """Continue from the last checkpoint after a crash (the graph re-runs the interrupted step)."""
        self.deps.runs.update_run(run_id, status="running")
        self.deps.events.append(run_id, "resume", "system", {"reason": "recovered after a restart"})
        return self._invoke(None, run_id, interactive)

    def _invoke(self, payload: Any, run_id: str, interactive: bool) -> dict[str, Any]:
        self.deps.budget.start(run_id)
        result: dict[str, Any] = self.graph.invoke(payload, graph_config(self.deps, run_id, interactive=interactive))
        if "__interrupt__" in result:
            self.deps.runs.update_run(run_id, status="interrupted")
        return result

    # ------------------------------------------------------------------ inspection
    def state(self, run_id: str) -> dict[str, Any]:
        snap = self.graph.get_state(graph_config(self.deps, run_id))
        return dict(snap.values)

    def is_finished(self, run_id: str) -> bool:
        snap = self.graph.get_state(graph_config(self.deps, run_id))
        return bool(snap.values) and not snap.next
