"""Configuration: ``config/*.yaml`` plus environment (``.env``) settings."""

from __future__ import annotations

import fnmatch
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def project_root() -> Path:
    """Repo root: ``FIXLING_HOME`` if set, else the directory holding ``config/`` above this file or cwd."""
    env = os.environ.get("FIXLING_HOME")
    if env:
        return Path(env).resolve()
    here = Path(__file__).resolve()
    for candidate in (*here.parents, Path.cwd()):
        if (candidate / "config" / "models.yaml").exists():
            return candidate
    return Path.cwd()


class LMStudioCfg(BaseModel):
    base_url: str = "http://localhost:1234/v1"
    api_key: str = "lm-studio"
    timeout_s: float = 300
    max_parallel: int = 4


class NodeCfg(BaseModel):
    temperature: float | None = None
    temperatures: list[float] | None = None
    max_tokens: int = 1000
    thinking: bool = False

    def temp_default(self) -> float:
        if self.temperature is not None:
            return self.temperature
        return self.temperatures[0] if self.temperatures else 0.2

    def temps(self, n: int | None = None) -> list[float]:
        """Temperatures for fan-out nodes; falls back to the single ``temperature``."""
        base = self.temperatures or [self.temperature if self.temperature is not None else 0.2]
        if n is None:
            return list(base)
        return [base[i % len(base)] for i in range(n)]


class ExpertCfg(BaseModel):
    enabled: bool = False
    base_url: str | None = None
    model: str | None = None


class ModelsCfg(BaseModel):
    lmstudio: LMStudioCfg = Field(default_factory=LMStudioCfg)
    main_model: str = "fixling-main"
    embed_model: str = "fixling-embed"
    context_length: int = 32768
    max_prompt_tokens: int = 8000
    thinking_style: str = "none"  # "qwen3" appends /think or /no_think to the user turn; set after the bake-off
    nodes: dict[str, NodeCfg] = Field(default_factory=dict)
    expert: ExpertCfg = Field(default_factory=ExpertCfg)

    def node(self, name: str) -> NodeCfg:
        return self.nodes.get(name, NodeCfg())


class MutationCfg(BaseModel):
    enabled: bool = False
    timeout_s: int = 180


class BudgetsCfg(BaseModel):
    run_wall_clock_s: int = 1500
    run_max_llm_tokens: int = 250000
    reproduce_attempts: int = 3
    repair_candidates: int = 4
    refine_rounds: int = 2
    cmd_timeout_s: int = 120
    test_timeout_s: int = 600
    observation_chars: int = 4000
    baseline_runs: int = 3
    verify_shuffled: bool = True
    mutation: MutationCfg = Field(default_factory=MutationCfg)


class PolicyCfg(BaseModel):
    protected_globs: list[str] = Field(default_factory=list)
    pyproject_protected_sections: list[str] = Field(default_factory=list)
    generated_test_dir: str = "tests/fixling"
    max_diff_lines: int = 400
    max_files_changed: int = 6
    forbidden_cmd_patterns: list[str] = Field(default_factory=list)
    trigger_associations: list[str] = Field(default_factory=lambda: ["OWNER", "MEMBER", "COLLABORATOR"])


class Settings(BaseSettings):
    """Secrets and deployment knobs from the environment / ``.env`` (controller process only)."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    github_token: str = Field(default="", alias="GITHUB_TOKEN")
    fixling_repos: str = Field(default="", alias="FIXLING_REPOS")
    fixling_runs_dir: str = Field(default="runs", alias="FIXLING_RUNS_DIR")
    fixling_canary: str = Field(default="", alias="FIXLING_CANARY")
    fixling_dashboard_url: str = Field(default="http://localhost:8000", alias="FIXLING_DASHBOARD_URL")
    fixling_local_roots: str = Field(default="", alias="FIXLING_LOCAL_ROOTS")
    lmstudio_base_url: str = Field(default="", alias="LMSTUDIO_BASE_URL")

    @property
    def repos(self) -> list[str]:
        return [r.strip() for r in self.fixling_repos.split(",") if r.strip()]

    @property
    def local_roots(self) -> list[str]:
        """Directories the API may copy for local runs (empty = local runs via the API are disabled)."""
        return [r.strip() for r in self.fixling_local_roots.split(",") if r.strip()]


class Config(BaseModel):
    models: ModelsCfg
    budgets: BudgetsCfg
    policy: PolicyCfg
    settings: Settings
    root: Path

    @property
    def runs_dir(self) -> Path:
        p = Path(self.settings.fixling_runs_dir)
        return p if p.is_absolute() else self.root / p

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id

    @property
    def db_path(self) -> Path:
        return self.runs_dir / "fixling.sqlite"

    @property
    def checkpoint_path(self) -> Path:
        return self.runs_dir / "checkpoints.sqlite"

    def secrets(self) -> list[str]:
        """Values that must never be persisted (used by the event redactor)."""
        return [s for s in (self.settings.github_token, self.settings.fixling_canary) if s]


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping")
    return data


def load_config(root: Path | None = None, *, env_file: str | None = None) -> Config:
    root = (root or project_root()).resolve()
    cfg_dir = root / "config"
    dotenv = env_file or str(root / ".env")
    settings = Settings(_env_file=dotenv)  # type: ignore[call-arg]  # pydantic-settings runtime kwarg
    models = ModelsCfg.model_validate(_load_yaml(cfg_dir / "models.yaml"))
    if settings.lmstudio_base_url:  # e.g. LM Studio running on another machine in the LAN
        models.lmstudio.base_url = settings.lmstudio_base_url
    return Config(
        models=models,
        budgets=BudgetsCfg.model_validate(_load_yaml(cfg_dir / "budgets.yaml")),
        policy=PolicyCfg.model_validate(_load_yaml(cfg_dir / "policy.yaml")),
        settings=settings,
        root=root,
    )


@lru_cache(maxsize=1)
def get_config() -> Config:
    return load_config()


def glob_match(path: str, pattern: str) -> bool:
    """Gitignore-ish glob: ``**`` spans directories, a pattern without ``/`` matches any basename."""
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    if "/" not in pattern:
        return fnmatch.fnmatchcase(path.rsplit("/", 1)[-1], pattern)
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.fullmatch("".join(out), path) is not None
