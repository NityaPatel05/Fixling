"""Wiring: build the object graph (DB, events, LLM, sandboxes, budgets) once, for the CLI, worker and API alike."""

from __future__ import annotations

from dataclasses import dataclass

from fixling.config import Config, get_config
from fixling.db import Database
from fixling.events.store import EventStore
from fixling.graph.deps import Deps, SandboxProvider
from fixling.graph.guards import BudgetTracker, CancelRegistry
from fixling.graph.pipeline import Pipeline
from fixling.llm.client import LLM
from fixling.runs import RunStore


@dataclass
class Services:
    cfg: Config
    db: Database
    events: EventStore
    runs: RunStore
    cancel: CancelRegistry
    budget: BudgetTracker
    llm: LLM

    def deps(self, sandboxes: SandboxProvider, **overrides: object) -> Deps:
        deps = Deps(
            cfg=self.cfg,
            events=self.events,
            runs=self.runs,
            llm=self.llm,
            sandboxes=sandboxes,
            budget=self.budget,
        )
        for key, value in overrides.items():
            setattr(deps, key, value)
        return deps

    def pipeline(self, sandboxes: SandboxProvider, **overrides: object) -> Pipeline:
        return Pipeline(self.deps(sandboxes, **overrides))


def build_services(cfg: Config | None = None) -> Services:
    """Create storage, the event store (with secret redaction), the budget tracker and the LM Studio client."""
    cfg = cfg or get_config()
    db = Database(cfg.db_path)
    events = EventStore(db, cfg.runs_dir, observation_chars=cfg.budgets.observation_chars, secrets=cfg.secrets())
    runs = RunStore(db)
    cancel = CancelRegistry()
    budget = BudgetTracker(cfg.budgets, runs, events, cancel)
    llm = LLM(cfg.models, events, runs, budget_check=budget.check)
    return Services(cfg=cfg, db=db, events=events, runs=runs, cancel=cancel, budget=budget, llm=llm)


def docker_provider(services: Services) -> SandboxProvider:
    """The production sandbox provider. Imported lazily so unit tests never need the Docker SDK to connect."""
    from fixling.sandbox.images import get_client
    from fixling.sandbox.provider import DockerProvider

    return DockerProvider(get_client(), services.cfg, services.events, services.runs)
