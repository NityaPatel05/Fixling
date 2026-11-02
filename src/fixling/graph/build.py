"""Graph assembly (spec E18).

    START -> intake -> triage -> env -> localize -> reproduce -> [repair_one x N] -> select
          -> (clean_verify | refine -> [repair_one x N] -> select ...) -> (strength) -> finalize -> END

Every stop condition (budget, cancel, error, nothing to do) routes to ``finalize``, which always runs.
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from fixling.graph.nodes.env import env
from fixling.graph.nodes.finalize import finalize
from fixling.graph.nodes.intake import intake
from fixling.graph.nodes.localize import localize
from fixling.graph.nodes.repair import fanout, refine, repair_one, route_select, select
from fixling.graph.nodes.reproduce import reproduce
from fixling.graph.nodes.triage import triage
from fixling.graph.nodes.verify import clean_verify, route_verify, strength
from fixling.graph.state import RunState


def route_triage(state: RunState) -> str:
    if state.get("stop_reason") or (state.get("triage") or {}).get("decision") == "clarify":
        return "finalize"
    return "env"


def route_env(state: RunState) -> str:
    return "finalize" if state.get("stop_reason") else "localize"


def build_graph(checkpointer: BaseCheckpointSaver[Any] | None) -> CompiledStateGraph[RunState, Any, Any, Any]:
    g: StateGraph[RunState, Any, Any, Any] = StateGraph(RunState)
    g.add_node("intake", intake)
    g.add_node("triage", triage)
    g.add_node("env", env)
    g.add_node("localize", localize)
    g.add_node("reproduce", reproduce)
    g.add_node("repair_one", repair_one)
    g.add_node("select", select)
    g.add_node("refine", refine)
    g.add_node("clean_verify", clean_verify)
    g.add_node("strength", strength)
    g.add_node("finalize", finalize)

    g.add_edge(START, "intake")
    g.add_edge("intake", "triage")
    g.add_conditional_edges("triage", route_triage, ["env", "finalize"])
    g.add_conditional_edges("env", route_env, ["localize", "finalize"])
    g.add_edge("localize", "reproduce")
    g.add_conditional_edges("reproduce", fanout, ["repair_one", "finalize"])
    g.add_edge("repair_one", "select")  # waits for every Send branch of the round
    g.add_conditional_edges("select", route_select, ["clean_verify", "refine", "finalize"])
    g.add_conditional_edges("refine", fanout, ["repair_one", "finalize"])
    g.add_conditional_edges("clean_verify", route_verify, ["strength", "finalize"])
    g.add_edge("strength", "finalize")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer)
