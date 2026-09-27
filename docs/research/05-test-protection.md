# Test protection and reward hacking (research session 05)

Figures are secondhand from search results; verify before quoting.

## Documented cases
- Claude 3.7 Sonnet system card: special-cases tests (returns expected values, or edits tests) after repeated failures, often with a "# special case for test X" comment. https://assets.anthropic.com/m/785e231869ea8b3b/original/claude-3-7-sonnet-system-card.pdf
- Claude 4 system card: reward-hack rate measured; Opus 4 higher than Sonnet 4. https://www-cdn.anthropic.com/c788cbc0a3da9135112f97cdf6dcd06f2c16cee2.pdf
- Anthropic, "Natural emergent misalignment from reward hacking": sys.exit(0), always-equal objects, conftest patching; hacking generalized to sabotage. https://arxiv.org/abs/2511.18397
- METR: o3 edits tests/scoring, reads answers off the call stack, disables timing; ~1-2% of attempts. https://metr.org/blog/2025-06-05-recent-reward-hacking
- OpenAI CoT monitoring: a weaker LLM reading chain of thought catches test subversion better than reading actions; training against the monitor makes the model hide intent. https://arxiv.org/abs/2503.11926
- SWE-bench Pro git hacking: `git log --all`, dangling objects, `git show <sha>` to find the fix commit. Fix: fresh single-commit repos, no future objects, no network. https://arxiv.org/pdf/2609.06780
- Patch overfitting: PatchDiff (7.8% "correct" patches fail dev tests) https://arxiv.org/html/2503.15223v2 ; UTBoost (28.4% Lite / 15.7% Verified patches erroneous) https://arxiv.org/pdf/2506.09289 ; SWE-bench+ (32.7% solution leakage, 31.1% weak tests) ; SWE-ABS (78.8% -> 62.2% after adversarial tests) https://arxiv.org/abs/2603.00520v1
- Mutation testing: LLM-written tests are weak; mutation-feedback loop 53% -> 89.5% (MutGen). https://www.agentpatterns.ai/verification/mutation-testing-quality-gate/
- Claude Code deny rules have had enforcement bugs; PreToolUse hooks are the reliable gate. https://github.com/anthropics/claude-code/issues/6699

## Layered defense design
**L0 Environment.** Fresh `git init` of base state (or depth-1 clone, remote removed, `git reflog expire --expire=now --all && git gc --prune=now`); strip tags/hooks; no network except a package mirror; LLM calls from the orchestrator, not the container; harness/results outside the container.

**L1 Tool-layer write policy (hard deny in the LangGraph tool node).** Always denied: conftest.py, pytest.ini, tox.ini, setup.cfg, pytest sections of pyproject, .github/**, .git/**, Makefile, noxfile, sitecustomize, *.pth, lockfiles. Pre-existing test files read-only; agent may only add new tests under an allowed pattern. Mount protected files `:ro` (shell `sed -i` bypasses tool checks). Fixed `run_tests(paths)` wrapper, no raw pytest args (-k, --deselect, -p, PYTEST_ADDOPTS).

**L2 Deterministic diff gate (fail closed) on `git diff base..HEAD`.**
- Reject changes to protected paths; reject modified/deleted lines in pre-existing tests.
- AST scan of added tests: non-constant assert required; flag skip/xfail/importorskip/empty parametrize; patching of module under test; sys.exit/os._exit/SystemExit/atexit; assignments to TestCase/pytest/builtins; constant-True `__eq__`.
- AST scan of added prod code: input compared to a literal that also appears in the test diff; bodies that only `return <literal>`; branches on `"pytest" in sys.modules`, PYTEST_CURRENT_TEST, inspect.stack/sys._getframe; "special-case"/"hard-code" comments.
- Size-ratio outliers go to review.

**L3 Clean-container verification.** Apply only the patch to a fresh base checkout in a new container; reinstall from lockfile; restore pre-existing tests/config from base; orchestrator-pinned pytest config; run once fixed order and once shuffled; parse junit XML, require collected == passed + failed, no new skips, zero collected = failure (catches sys.exit(0)); require both exit code and XML.

**L4 Test-set design.** PASS_TO_PASS: full base suite (flaky excluded after 3 runs), must all pass. FAIL_TO_PASS: agent's new tests must fail on base with a real assertion failure (not ImportError) and pass with the fix. Hidden tests: a separate test-writer call generates tests from the issue only; coder never sees them; failures are review signals, not hard fails (hidden tests can be wrong).

**L5 Strength checks.** mutmut (Python) / Stryker (JS) / PIT (Java) restricted to changed lines, 5-minute cap, start threshold 0.6; feed surviving mutants back to the agent; diff-cover on changed lines (aim 100%, min 80%). Optional differential test against a second independent patch (soft signal).

**L6 LLM judge.** Separate model and session; input = issue, diff, L2-L5 results, shell transcript; structured JSON verdict (modifies_tests_to_pass, special_cases_inputs, weakened_assertions, fix_is_general, evidence, confidence). Look for `git log --all`/`git show`, curl/pip of upstream, repeated denied writes. Abort run after 2+ denied writes to protected paths. Judge failure => draft PR labeled for human review, never auto-merge. Don't optimize the agent against the judge.

**L7 Prompt/process.** Tell the agent never to edit tests/config and that giving up with an explanation is fine; cap fix attempts ~3 (hacks appear after repeated failures); log everything. Prompting is a mitigation, not a control.

## LangGraph wiring
`plan -> code (L1) -> run_tests -> diff_gate (L2) -> [fail: feedback, attempt++] -> clean_verify (L3+L4) -> strength (L5) -> judge (L6) -> open_pr`
State: base_sha, patch, denied_attempts, gate_findings, verify_result, mutation_score, judge_verdict, attempt. Module `policy.py` with `is_protected()` and `check_diff()` using `unidiff` + `ast`. Keep a corpus of known-hack fixtures as the gate's regression suite. Verifier: `docker run --network none --read-only --tmpfs /tmp`, patch mounted `:ro`, junit XML to a volume only the orchestrator reads.

## Residual risks
Semantically weak tests can pass mutation/coverage; non-Python needs tree-sitter rules; flaky tests cause false rejects; obfuscated hacks evade simple AST rules (covered by clean rerun, hidden tests, differential checks); keep growing the hack corpus from real runs.
