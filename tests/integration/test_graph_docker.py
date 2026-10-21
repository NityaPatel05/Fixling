"""The whole pipeline over REAL hardened Docker sandboxes (``pytest -m docker``), driven by a scripted LLM."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import docker
import pytest

from fixling.config import Config
from fixling.db import Database
from fixling.events.store import EventStore
from fixling.graph.deps import Deps
from fixling.graph.guards import BudgetTracker
from fixling.graph.pipeline import Pipeline, make_checkpointer
from fixling.runs import RunStore
from fixling.sandbox.images import ensure_base
from fixling.sandbox.provider import DockerProvider
from fixling.verify.clean_verify import clean_verify
from fixling.verify.policy import PolicyGate, has_block
from tests.fakes import FakeLLM
from tests.integration.test_graph_fake_llm import Crash, Harness, script
from tests.unit.test_policy import FIXTURES, TINY

pytestmark = pytest.mark.docker


@pytest.fixture(scope="session")
def client() -> docker.DockerClient:
    try:
        c = docker.from_env()
        c.ping()
    except Exception as exc:
        pytest.skip(f"Docker daemon not available: {exc}")
    ensure_base(c)
    return c


@pytest.fixture
def make_docker_deps(cfg: Config, tmp_path: Path, client: docker.DockerClient) -> Callable[..., Deps]:
    def build(llm: Any, **overrides: Any) -> Deps:
        db = Database(tmp_path / "fixling.sqlite")
        events = EventStore(db, cfg.runs_dir, observation_chars=cfg.budgets.observation_chars, secrets=cfg.secrets())
        runs = RunStore(db)
        deps = Deps(
            cfg=cfg,
            events=events,
            runs=runs,
            llm=llm,
            sandboxes=DockerProvider(client, cfg, events, runs),
            budget=BudgetTracker(cfg.budgets, runs, events),
            use_embeddings=False,
        )
        for k, v in overrides.items():
            setattr(deps, k, v)
        return deps

    return build


def containers_of(client: docker.DockerClient, run_id: str) -> list[str]:
    return [c.name for c in client.containers.list(all=True, filters={"label": f"fixling.run={run_id}"})]


def test_seeded_bug_is_fixed_and_verified_in_real_sandboxes(
    make_docker_deps: Callable[..., Deps], cfg: Config, tiny_repo: Path, client: docker.DockerClient
) -> None:
    h = Harness(make_docker_deps, cfg, tiny_repo, FakeLLM(script=script()))
    outcome = h.run()["outcome"]
    assert outcome["status"] == "verified_fix" and outcome["confidence"] == "high", outcome
    patch = (cfg.run_dir(h.run_id) / "patch.diff").read_text(encoding="utf-8")
    assert "+    if qty >= BULK_THRESHOLD:" in patch and "tests/fixling/test_issue_1.py" in patch
    assert containers_of(client, h.run_id) == [], "every sandbox must be gone after finalize"
    assert h.deps.runs.live_sandboxes(h.run_id) == []
    verify = next(e for e in h.deps.events.list(h.run_id) if e.type == "verify")
    assert verify.payload["f2p_ok"] and verify.payload["p2p_ok"] and verify.payload["shuffled_ok"] is True


def test_crash_and_recovery_over_real_docker_leaks_no_containers(
    make_docker_deps: Callable[..., Deps], cfg: Config, tiny_repo: Path, client: docker.DockerClient
) -> None:
    first = Harness(make_docker_deps, cfg, tiny_repo, FakeLLM(script=script()))
    real_create = first.deps.sandboxes.create
    seen = {"cand": 0}

    def dying(run_id: str, role: str, *a: Any, **kw: Any) -> Any:
        if role == "cand":
            seen["cand"] += 1
            if seen["cand"] == 2:
                raise Crash
        return real_create(run_id, role, *a, **kw)

    first.deps.sandboxes.create = dying  # type: ignore[method-assign]
    with pytest.raises(Crash):
        first.run()

    # a restart: new provider, new connections, same sqlite files and the same Docker daemon
    llm = FakeLLM(script=script())
    deps = make_docker_deps(llm)
    result = Pipeline(deps, make_checkpointer(cfg.checkpoint_path)).recover(first.run_id)
    assert result["outcome"]["status"] == "verified_fix"
    assert containers_of(client, first.run_id) == [] and deps.runs.live_sandboxes(first.run_id) == []


@pytest.mark.parametrize(
    ("hack", "expect_rule"),
    [("conftest_sys_exit", "patch_apply_failed"), ("conftest_in_generated_dir", "protected_path")],
)
def test_clean_verify_catches_planted_hacks_in_a_real_container(
    make_docker_deps: Callable[..., Deps], cfg: Config, tiny_repo: Path, hack: str, expect_rule: str
) -> None:
    """Hack 05 targets root-owned ``tests/`` (the sandbox itself refuses it); 07 slips into the writable generated
    directory, applies cleanly, and is stopped by the junit check plus the policy gate."""
    deps = make_docker_deps(FakeLLM())
    run_id = deps.runs.create_run(repo="local/tiny") or ""
    patch = next((FIXTURES / "hacks").glob(f"*_{hack}.diff")).read_text(encoding="utf-8")
    sandbox = deps.sandboxes.create(run_id, "verify", tiny_repo, "local/tiny")
    try:
        result = clean_verify(
            sandbox=sandbox,
            patch=patch,
            gate=PolicyGate(cfg.policy, base_dir=TINY),
            baseline={"passed": ["tests/test_pricing.py::test_bulk_price_small_order_has_no_discount"], "skipped": []},
            repro_ids=[],
            budgets=cfg.budgets,
            events=deps.events,
            run_id=run_id,
        )
    finally:
        deps.sandboxes.close_run(run_id)
    assert not result.f2p_ok  # a hack never counts as a verified fix
    assert expect_rule in {f.rule for f in result.findings} and has_block(result.findings)
    if expect_rule == "patch_apply_failed":
        assert not result.p2p_ok  # nothing ran: the sandbox refused the write to the root-owned tests/ directory
