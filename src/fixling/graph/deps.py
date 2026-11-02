"""Dependency container handed to every node through ``config["configurable"]["deps"]`` (no module singletons)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from langchain_core.runnables import RunnableConfig

from fixling.config import Config
from fixling.events.store import EventStore
from fixling.graph.guards import BudgetTracker
from fixling.llm.client import LLMLike
from fixling.runs import RunStore
from fixling.sandbox.base import Sandbox

if TYPE_CHECKING:
    from fixling.repo.env_setup import EnvResult


class SandboxProvider(Protocol):
    """Creates and tracks sandboxes. ``DockerProvider`` in production, a local double in tests."""

    def prepare_env(self, run_id: str, repo_key: str, src_dir: Path) -> tuple[EnvResult, Sandbox | None]:
        """Build/reuse the environment image, start the work sandbox, record the baseline (spec E7)."""
        ...

    def create(
        self, run_id: str, role: str, src_dir: Path, repo_key: str, extra_files: dict[str, str] | None = None
    ) -> Sandbox:
        """A fresh sandbox holding the trusted tree (plus ``extra_files``); its base commit is recorded."""
        ...

    def get(self, run_id: str, sandbox_id: str) -> Sandbox | None:
        """Re-attach to a live sandbox (after a restart), or ``None`` if it is gone."""
        ...

    def close_run(self, run_id: str) -> int:
        """Remove every sandbox belonging to ``run_id``; returns how many were closed."""
        ...


class Publisher(Protocol):
    """Pushes a branch and opens a draft PR (spec E19). Absent in local ``--no-pr`` runs."""

    def publish(self, run_id: str, state: dict[str, Any], patch: str) -> str | None:
        """Return the PR URL, or ``None`` when nothing was published."""
        ...


@dataclass
class Deps:
    cfg: Config
    events: EventStore
    runs: RunStore
    llm: LLMLike
    sandboxes: SandboxProvider
    budget: BudgetTracker
    publisher: Publisher | None = None
    use_embeddings: bool = True
    strength: bool = False  # run the optional strength checks (independent tests) before finalize
    skip_repro: bool = False  # ablation: do not write a reproduction test
    gates_enabled: bool = True  # ablation: False disables the L2 policy gate (sandbox ownership and junit checks stay)


def get_deps(config: RunnableConfig) -> Deps:
    deps = (config.get("configurable") or {}).get("deps")
    if not isinstance(deps, Deps):
        raise RuntimeError("graph invoked without config['configurable']['deps']")
    return deps


def get_option(config: RunnableConfig, key: str, default: Any = None) -> Any:
    return (config.get("configurable") or {}).get(key, default)
