"""``localize``: retrieval, then two small constrained LLM decisions (files, then functions) (spec E8/E17).

Both decisions are validated against the offered lists; anything else falls back to the deterministic ranking.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig

from fixling.errors import LLMFormatError, PromptTooLarge
from fixling.graph.deps import get_deps
from fixling.graph.helpers import qual_to_test_id, spec_text
from fixling.graph.nodes._common import node
from fixling.graph.state import RunState
from fixling.llm.render import first_line, fit, render
from fixling.models import Chunk, EditTarget, FileRanking, IssueSpec, TargetPick
from fixling.repo.index import build_index, repo_map
from fixling.repo.retrieval import retrieve

MAX_FILES = 3
MAX_TARGETS = 3
MAX_OFFERED_FUNCTIONS = 30
MAP_LEVELS = ((15, 2500), (10, 1500), (6, 800), (3, 400))


@node("localize")
def localize(state: RunState, config: RunnableConfig) -> dict[str, Any]:
    deps = get_deps(config)
    run_id = state["run_id"]
    spec = IssueSpec.model_validate(state["spec"])
    issue_text = f"{state.get('issue_title', '')}\n\n{state.get('issue_body_sanitized', '')}"
    chunks = build_index(Path(state["src_dir"]))
    res = retrieve(
        spec,
        issue_text,
        chunks,
        deps.llm,
        runs=deps.runs,
        repo_key=state["repo"],
        use_embeddings=deps.use_embeddings,
    )
    if not res.chunks:
        return {"stop_reason": "No Python source code was found in the repository to fix."}

    fallbacks: list[str] = []
    text = spec_text(state["spec"])
    max_prompt = deps.cfg.models.max_prompt_tokens

    # 1) which files?
    def build_rank(level: int) -> list[dict[str, str]]:
        n, budget = MAP_LEVELS[level]
        return render("rank_files", spec=text, repo_map=repo_map(chunks, res.files[:n], budget_tokens=budget))

    chosen: list[str] = []
    try:
        ranking = deps.llm.structured(
            run_id, "localize", FileRanking, fit(build_rank, deps.llm.count_tokens, max_prompt, range(len(MAP_LEVELS)))
        )
        chosen = [f for f in dict.fromkeys(ranking.files) if f in res.files][:MAX_FILES]
    except (LLMFormatError, PromptTooLarge):
        pass
    if not chosen:
        chosen = res.files[:2]
        fallbacks.append("files")

    # 2) which functions?
    offered = [c for c in res.chunks if c.path in chosen and c.kind in ("function", "method")]
    if len(offered) < MAX_TARGETS:  # include functions from the chosen files that did not make the global top 40
        have = {c.qualname for c in offered}
        offered += [
            c for c in chunks if c.path in chosen and c.kind in ("function", "method") and c.qualname not in have
        ]
    offered = offered[:MAX_OFFERED_FUNCTIONS]
    if not offered:
        return {"stop_reason": "The most likely files contain no functions that could be edited."}
    by_qual = {c.qualname: c for c in offered}

    def build_pick(level: int) -> list[dict[str, str]]:
        shown = offered[: (MAX_OFFERED_FUNCTIONS, 20, 12, 6)[level]]
        cands = [{"qualname": c.qualname, "signature": c.signature, "doc": first_line(c.docstring, 80)} for c in shown]
        return render("pick_targets", spec=text, candidates=cands)

    picked: list[Chunk] = []
    try:
        pick = deps.llm.structured(
            run_id, "localize", TargetPick, fit(build_pick, deps.llm.count_tokens, max_prompt, range(4))
        )
        picked = [by_qual[q] for q in dict.fromkeys(pick.targets) if q in by_qual][:MAX_TARGETS]
    except (LLMFormatError, PromptTooLarge):
        pass
    if not picked:
        picked = offered[:2]
        fallbacks.append("targets")

    targets = [EditTarget(path=c.path, qualname=c.qualname, start=c.start, end=c.end, source=c.source) for c in picked]
    related_ids = [qual_to_test_id(c.qualname) for c in res.related_tests]
    example = (
        res.related_tests[0]
        if res.related_tests
        else next(
            (
                c
                for c in chunks
                if c.kind == "function" and c.path.startswith("tests/") and c.signature.startswith("def test")
            ),
            None,
        )
    )
    deps.events.append(
        run_id,
        "retrieval",
        "system",
        {
            "candidate_files": res.files,
            "chosen_files": chosen,
            "targets": [t.qualname for t in targets],
            "hint_hits": res.hint_hits,
            "related_tests": related_ids,
            "used_embeddings": res.used_embeddings,
            "fallbacks": fallbacks,
        },
        node="localize",
    )
    return {
        "retrieval": {
            "files": chosen,
            "related_tests": related_ids,
            "hint_hits": res.hint_hits,
            "used_embeddings": res.used_embeddings,
            "fallbacks": fallbacks,
            "example_test": {"path": example.path, "source": example.source} if example else None,
        },
        "targets": [t.model_dump() for t in targets],
    }
