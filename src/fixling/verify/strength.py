"""Optional strength checks (spec E13): independent tests that never saw the patch.

Results are soft signals for the confidence level, never a reason to reject a fix on their own. Mutation testing
is intentionally not implemented in v1 (``budgets.mutation.enabled`` stays false); the hook is here so the
PR body and confidence logic already know how to consume a mutation score.
"""

from __future__ import annotations

from typing import Any

from fixling.errors import LLMFormatError, PromptTooLarge
from fixling.events.store import EventStore
from fixling.graph.helpers import import_lines, spec_text
from fixling.llm.client import LLMLike
from fixling.llm.render import fit, render
from fixling.models import ReproTest
from fixling.sandbox.base import Sandbox
from fixling.verify.clean_verify import APPLY_CMD, PATCH_PATH
from fixling.verify.policy import check_test_source, has_block
from fixling.verify.runner import is_green, record_test_run, run_tests

EXTRA_PATH = "tests/fixling/test_independent.py"


def generate_extra_tests(
    llm: LLMLike, run_id: str, spec: dict[str, Any], targets: list[dict[str, Any]], max_prompt: int
) -> str | None:
    """Ask for independent tests from the issue text alone; ``None`` if the model cannot produce acceptable ones."""
    text = spec_text(spec)

    def build(level: int) -> list[dict[str, str]]:
        return render("extra_tests", spec=text, imports=import_lines(targets[: (len(targets), 1)[min(level, 1)]]))

    try:
        messages = fit(build, llm.count_tokens, max_prompt, range(2))
        code = llm.structured(run_id, "extra_tests", ReproTest, messages).test_code
    except (LLMFormatError, PromptTooLarge):
        return None
    return None if has_block(check_test_source(EXTRA_PATH, code)) else code


def run_extra_tests(
    *,
    base_sandbox: Sandbox,
    patched_sandbox: Sandbox,
    code: str,
    patch: str,
    events: EventStore,
    run_id: str,
    timeout: int,
) -> dict[str, Any]:
    """Run the independent tests on the unpatched tree (expect failure) and the patched tree (expect pass)."""
    base_sandbox.write_file(EXTRA_PATH, code)
    base = run_tests(base_sandbox, [EXTRA_PATH], timeout=timeout, per_test_timeout=60)
    record_test_run(events, run_id, "strength", base, "independent tests (base)")

    patched_sandbox.write_file(PATCH_PATH, patch)
    applied = patched_sandbox.exec(APPLY_CMD, 60)
    if applied.exit_code != 0:
        return {"status": None, "reason": "the patch did not apply in the strength sandbox"}
    patched_sandbox.write_file(EXTRA_PATH, code)
    after = run_tests(patched_sandbox, [EXTRA_PATH], timeout=timeout, per_test_timeout=60)
    record_test_run(events, run_id, "strength", after, "independent tests (patched)")

    if not after.junit_ok or not base.junit_ok:
        return {"status": None, "reason": "the independent tests could not be run"}
    if is_green(after):
        # Passing on the base tree too means they do not exercise the bug: informative, but not corroboration.
        corroborates = not is_green(base)
        return {
            "status": "pass" if corroborates else None,
            "reason": "pass on patched, fail on base" if corroborates else "tests also pass on the unfixed code",
        }
    return {"status": "fail", "reason": "independent tests fail on the patched tree", "failed": after.failed[:5]}
