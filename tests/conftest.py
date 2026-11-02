from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from fixling.config import Config, load_config
from tests.fakes import FakeLLM


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Config]:
    """Real config/*.yaml with runs/ redirected to a temp dir and no secrets from the environment."""
    for var in ("GITHUB_TOKEN", "FIXLING_REPOS", "FIXLING_CANARY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FIXLING_RUNS_DIR", str(tmp_path / "runs"))
    yield load_config(env_file=str(tmp_path / "nonexistent.env"))


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


FIXTURE_REPOS = Path(__file__).parent / "fixtures" / "repos"


@pytest.fixture
def tiny_repo(tmp_path: Path) -> Path:
    """A writable copy of tests/fixtures/repos/tiny_repo (seeded off-by-one bug in bulk_price)."""
    import shutil

    dest = tmp_path / "tiny_repo"
    shutil.copytree(FIXTURE_REPOS / "tiny_repo", dest, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    return dest


@pytest.fixture
def make_deps(cfg: Config, tmp_path: Path):  # type: ignore[no-untyped-def]
    """Build graph ``Deps`` over temp storage with a LocalProvider (no Docker, no LM Studio)."""
    from fixling.db import Database
    from fixling.events.store import EventStore
    from fixling.graph.deps import Deps
    from fixling.graph.guards import BudgetTracker
    from fixling.runs import RunStore
    from tests.fakes import LocalProvider

    def build(llm: object, **overrides: object) -> Deps:
        db = Database(tmp_path / "fixling.sqlite")
        events = EventStore(db, cfg.runs_dir, observation_chars=cfg.budgets.observation_chars, secrets=cfg.secrets())
        runs = RunStore(db)
        provider = LocalProvider(tmp_path / "sandboxes", cfg, events, runs)
        deps = Deps(
            cfg=cfg,
            events=events,
            runs=runs,
            llm=llm,  # type: ignore[arg-type]
            sandboxes=provider,
            budget=BudgetTracker(cfg.budgets, runs, events),
            use_embeddings=False,
        )
        for k, v in overrides.items():
            setattr(deps, k, v)
        return deps

    return build
