"""Shared node wrapper: every node either returns a partial state update or routes the run to ``finalize``."""

from __future__ import annotations

import functools
import traceback
from collections.abc import Callable
from typing import Any, Protocol

from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphBubbleUp

from fixling.errors import BudgetExceeded, LLMUnavailable
from fixling.graph.deps import get_deps


class NodeFn(Protocol):
    """A graph node: LangGraph needs the parameter to be NAMED ``config`` to inject the run config."""

    def __call__(self, state: Any, config: RunnableConfig) -> dict[str, Any]: ...


def node(name: str) -> Callable[[NodeFn], NodeFn]:
    """Wrap a node body.

    * Once ``stop_reason`` is set, every later node is a no-op, so the run flows straight to ``finalize``.
    * Budget / cancel checks run before the body; ``BudgetExceeded`` becomes a ``stop_reason``.
    * LLM outages and unexpected errors are recorded as events and also become a ``stop_reason``: a node never
      crashes the graph, because ``finalize`` must always run.
    * LangGraph control-flow exceptions (``interrupt``) pass through untouched.
    """

    def decorate(fn: NodeFn) -> NodeFn:
        @functools.wraps(fn)
        def wrapper(state: Any, config: RunnableConfig) -> dict[str, Any]:
            deps = get_deps(config)
            run_id = state["run_id"]
            if state.get("stop_reason") and name != "finalize":
                return {}
            try:
                if name != "finalize":  # a stopped run MUST still finalize: record the outcome, free sandboxes
                    deps.budget.check(run_id)
                return fn(state, config) or {}
            except GraphBubbleUp:
                raise
            except BudgetExceeded as exc:
                deps.events.append(run_id, "budget", "system", {"stopped": exc.reason}, node=name)
                return {"stop_reason": exc.reason}
            except LLMUnavailable as exc:
                deps.events.append(run_id, "error", "system", {"kind": "llm_unavailable", "error": str(exc)}, node=name)
                return {"stop_reason": f"The language model is unavailable: {exc}"}
            except Exception as exc:
                deps.events.append(
                    run_id,
                    "error",
                    "system",
                    {"kind": type(exc).__name__, "error": str(exc)},
                    node=name,
                    artifact_text=traceback.format_exc(),
                )
                return {"stop_reason": f"Internal error in {name}: {type(exc).__name__}: {exc}"}

        return wrapper

    return decorate
