"""``reproduce``: have the model write a NEW failing test and accept it only if it fails for the right reason (E17).

A valid reproduction (1) passes the test-integrity rules, (2) fails on the unfixed code with an assertion or an
expected-exception failure and never with an import/syntax/collection/fixture error, and (3) gives the same result
twice. After the allowed attempts the run continues without one (flag ``no_repro``); the outcome is then capped at
``unverified_fix``.
"""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig

from fixling.errors import LLMFormatError, PromptTooLarge
from fixling.graph.deps import get_deps
from fixling.graph.helpers import (
    failure_summary,
    import_lines,
    repro_test_path,
    short,
    spec_text,
    target_views,
    write_run_file,
)
from fixling.graph.nodes._common import node
from fixling.graph.state import RunState
from fixling.llm.render import fit, render, repo_file
from fixling.models import ReproTest, TestRun
from fixling.verify.policy import check_test_source, has_block
from fixling.verify.runner import classify_failure, record_test_run, run_tests
from fixling.verify.secrets import scan_secrets

GOOD_KINDS = {"assertion", "expected_exception"}
BAD_KINDS = {"import", "syntax", "collection", "fixture", "timeout"}


def assess_repro(run: TestRun, path: str) -> tuple[list[str], str | None]:
    """Return ``(failing test ids that count as reproductions, rejection message or None)``."""
    if not run.junit_ok:
        tail = run.output[-400:].strip()
        return [], f"the test run produced no valid report (exit code {run.exit_code}): {tail}"
    if run.collected == 0:
        return [], "no test functions were collected; name them test_*"
    failing = [*run.failed, *run.errors]
    if not failing:
        return [], (
            "the test PASSED on the unfixed code, so it does not reproduce the bug. "
            "Assert the behaviour the issue says is correct, using the inputs from the issue."
        )
    good: list[str] = []
    for tid in failing:
        kind = classify_failure(tid, run)
        if kind in GOOD_KINDS:
            good.append(tid)
        elif kind in BAD_KINDS:
            return [], (
                f"the test failed with a {kind} problem, not a failing assertion: "
                f"{failure_summary(run.failure_details, [tid], 500)}"
            )
    if not good:
        return [], (
            "the test failed with an unexpected error instead of a failed assertion: "
            f"{failure_summary(run.failure_details, failing[:1], 500)}"
        )
    return good, None


@node("reproduce")
def reproduce(state: RunState, config: RunnableConfig) -> dict[str, Any]:
    deps = get_deps(config)
    run_id = state["run_id"]
    targets = state["targets"]
    if deps.skip_repro:  # ablation: repair without a reproduction test; the outcome is capped at unverified_fix
        deps.events.append(run_id, "repro", "system", {"ok": False, "attempts": 0, "skipped": True}, node="reproduce")
        return {"repro": None, "repro_attempts": 0, "flags": [*state.get("flags", []), "no_repro"]}
    sandbox = deps.sandboxes.get(run_id, state["work_sandbox_id"])
    if sandbox is None:
        return {"stop_reason": "The work sandbox is gone and could not be re-attached."}
    path = repro_test_path(state.get("issue_number"))
    text = spec_text(state["spec"])
    example = (state.get("retrieval") or {}).get("example_test")
    max_prompt = deps.cfg.models.max_prompt_tokens
    attempts = deps.cfg.budgets.reproduce_attempts
    previous_error: str | None = None
    history: list[str] = []

    for attempt in range(1, attempts + 1):

        def build(level: int, err: str | None = previous_error) -> list[dict[str, str]]:
            shown = targets[: (len(targets), 2, 1)[level]]
            ex = (
                repo_file(example["path"], short(example["source"], 1500), numbered=False)
                if example and level == 0
                else None
            )
            return render(
                "reproduce",
                spec=text,
                targets=target_views(shown),
                imports=import_lines(shown),
                example_test=ex,
                previous_error=err,
            )

        try:
            messages = fit(build, deps.llm.count_tokens, max_prompt, range(3))
            proposal = deps.llm.structured(run_id, "reproduce", ReproTest, messages)
        except (LLMFormatError, PromptTooLarge) as exc:
            previous_error = "your last answer was not valid JSON for the schema"
            history.append(f"attempt {attempt}: {exc}")
            continue

        code = proposal.test_code
        findings = check_test_source(path, code)
        leaked = scan_secrets(code, deps.cfg.secrets())
        if leaked:
            previous_error = (
                "the test contains a credential (" + ", ".join(leaked) + "); never write secrets into tests"
            )
            history.append(f"attempt {attempt}: {previous_error}")
            continue
        if has_block(findings):
            previous_error = "the test was rejected by the integrity rules: " + "; ".join(
                f"{f.rule} ({f.detail})" for f in findings if f.severity == "block"
            )
            history.append(f"attempt {attempt}: {previous_error}")
            continue

        sandbox.write_file(path, code)
        run = run_tests(sandbox, [path], timeout=deps.cfg.budgets.test_timeout_s, per_test_timeout=60)
        record_test_run(deps.events, run_id, "reproduce", run, f"repro attempt {attempt} (base)")
        failing, error = assess_repro(run, path)
        if error:
            previous_error = error
            history.append(f"attempt {attempt}: {error[:200]}")
            continue

        again = run_tests(sandbox, [path], timeout=deps.cfg.budgets.test_timeout_s, per_test_timeout=60)
        record_test_run(deps.events, run_id, "reproduce", again, f"repro attempt {attempt} (determinism check)")
        if set(again.failed) | set(again.errors) != set(failing):
            previous_error = (
                "the test gave different results on two runs; remove any randomness, time or ordering dependence"
            )
            history.append(f"attempt {attempt}: non-deterministic")
            continue

        patch = sandbox.diff()
        patch_path = write_run_file(deps, run_id, "repro.diff", patch)
        failure = deps.events.redact(failure_summary(run.failure_details, failing, 1500))
        deps.events.append(
            run_id,
            "repro",
            "llm",
            {
                "ok": True,
                "attempt": attempt,
                "path": path,
                "test_ids": failing,
                "explanation": proposal.explanation,
                "failure": failure,
            },
            node="reproduce",
            artifact_text=code,
        )
        return {
            "repro": {"path": path, "code": code, "test_ids": failing, "failure_output": failure},
            "repro_attempts": attempt,
            "repro_patch_path": patch_path,
        }

    deps.events.append(
        run_id,
        "repro",
        "system",
        {"ok": False, "attempts": attempts, "history": history, "last_error": previous_error},
        node="reproduce",
    )
    return {
        "repro": None,
        "repro_attempts": attempts,
        "repro_patch_path": None,
        "flags": [*state.get("flags", []), "no_repro"],
    }
