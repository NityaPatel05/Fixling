"""LangGraph state (spec E15). It stays small: ids, specs and summaries. Prompts, outputs and patches live in the
event store and under ``runs/<id>/`` (referenced by path)."""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


def keep_first(old: str | None, new: str | None) -> str | None:
    """Reducer for ``stop_reason``: the first reason set wins, and parallel branches may all set it."""
    return old or new


class RunState(TypedDict, total=False):
    # input (set by the caller before the first node)
    run_id: str
    repo: str
    issue_number: int
    issue_title: str
    issue_body: str  # raw, untrusted
    issue_author: str
    base_sha: str
    src_dir: str  # trusted host copy of the repo at base_sha

    # derived
    issue_body_sanitized: str
    flags: list[str]  # integrity/safety flags (e.g. "hidden_content", "no_repro")
    spec: dict[str, Any]  # IssueSpec
    triage: dict[str, Any]
    env_image: str
    work_sandbox_id: str
    baseline: dict[str, Any]  # Baseline
    retrieval: dict[str, Any]
    targets: list[dict[str, Any]]  # EditTarget
    repro: dict[str, Any] | None  # {path, code, test_ids, failure_output}
    repro_attempts: int
    repro_patch_path: str | None
    candidates: Annotated[list[dict[str, Any]], operator.add]
    round: int
    refine_context: dict[str, Any] | None
    best: dict[str, Any] | None  # summary of the round's best Candidate
    selection: dict[str, Any] | None  # {route, round, viable} decided by ``select``
    best_candidate_id: str | None
    best_patch_path: str | None
    verify: dict[str, Any] | None
    strength: dict[str, Any] | None
    judge: dict[str, Any] | None
    outcome: dict[str, Any] | None
    pr_url: str | None
    stop_reason: Annotated[str | None, keep_first]
