# Articles, blogs, engineering posts (research session 03)

[F] = read in full; [S] = search snippet only, verify before quoting.

## Top lessons
1. Keep the model loop minimal (bash + a strict edit tool); put determinism around it. Stripe "blueprints" mix code nodes (lint, push) with agent nodes, which maps onto a LangGraph graph. [F]
2. Cap the retry budget in code. Stripe allows max 2 CI rounds, then hands off to a human. Out-of-budget runs open a draft PR labelled "needs human". [F]
3. Sandbox needs filesystem and network isolation. Real credentials stay outside, behind a git proxy restricting pushes. Anthropic reports 84% fewer permission prompts: autonomy comes from the boundary. [F]
4. Long-running state lives outside the context: git commits, a progress file, a JSON feature list with immutable acceptance criteria (agents only flip the pass flag; JSON is overwritten less than Markdown). Each session starts with a smoke test. [F]
5. Main failure modes: declaring victory early, and overwriting a correct patch (60-69% of failures occur after the right function was already edited [S]). Mitigate with test-first work, hashed acceptance tests, git checkpoints with rollback, and a reviewer that checks tests weren't weakened.
6. Single writer plus fresh-context reviewer. Cognition moved from "don't build multi-agents" (2025) to "single-threaded writes, review loops and escalation are fine" (Apr 2026). Open SWE uses Planner/Programmer/Reviewer. [S/F]
7. Event sourcing: the agent emits schema-validated intentions, a deterministic harness persists them to an append-only log; resume is replay (OpenHands SDK [S]; ESAA [F]). Derive UI, resume state, and PR body from the log.
8. Copilot coding agent rules [S]: push only to an agent-prefixed branch, draft PR, never self-merge, only trusted users trigger runs. Treat issue text as untrusted (prompt injection).
9. Codex loop [S]: history is resent each turn, so keep the prompt append-only for cache hits; don't reorder tools or edit earlier messages. Tool design: strict args, absolute paths, error messages that explain recovery (Anthropic SWE-bench post [F]).
10. Ramp Inspect [S]: prebuilt per-repo images refreshed ~30 min plus snapshots for fast start; agent verifies its own work end to end; build an eval set of issues early. mini-swe-agent (~100 lines, 65-74% Verified [S]) shows scaffolding can stay small.

## What to implement
1. Graph as blueprint: fetch_issue -> plan -> (optional LangGraph interrupt approval) -> write_failing_tests -> implement loop -> deterministic lint/test -> fresh-context review -> push -> draft PR.
2. Bounded budgets (steps, tokens, wall time, CI rounds = 2) enforced in graph state.
3. Docker sandbox, no host mounts, no real credentials; egress allowlist proxy; git/GitHub proxy outside the sandbox holds the token and restricts pushes to the agent branch.
4. State = git + append-only event log. Event types: issue_received, plan, tool_call, observation, test_run, commit, pr_opened, error. Log drives UI, resume, PR body.
5. Test-first: acceptance tests derived from the issue are hashed and immutable; reviewer flags deleted/weakened tests.
6. Single writer + clean-context reviewer; no parallel writers in v1.
7. Append-only prompt for cache hits; few tools, strict schemas, absolute paths.
8. PR description from the event log: issue link, plan, tests added, commands and results, budget used, known gaps. Always draft.
9. Issue text is untrusted; only write-access users trigger runs; branch prefix restriction.
10. Eval harness on a fixed issue set from day one; prebuilt per-repo images.
11. "Ask for clarification" exit path for ambiguous issues; checkpoint after each green state, roll back on regression.

## Sources
- Anthropic harnesses: https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents [F]
- Anthropic SWE-bench Sonnet: https://www.anthropic.com/news/swe-bench-sonnet [F]
- Claude Code sandboxing: https://www.anthropic.com/engineering/claude-code-sandboxing [F]
- Anthropic tools/context (not fetched): https://www.anthropic.com/engineering/writing-tools-for-agents ; https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- Stripe Minions Part 2: https://stripe.dev/blog/minions-stripes-one-shot-end-to-end-coding-agents-part-2 [F]; Part 1: https://stripe.dev/blog/minions-stripes-one-shot-end-to-end-coding-agents
- Open SWE: https://www.langchain.com/blog/introducing-open-swe-an-open-source-asynchronous-coding-agent [F]; Medium: https://medium.com/@gsaidheeraj/langchain-open-swe-in-depth-guide-to-the-open-source-asynchronous-coding-agent-3957c49153e9
- Ramp Inspect: https://modal.com/blog/how-ramp-built-a-full-context-background-coding-agent-on-modal [S]
- Cognition multi-agent: https://www.zenml.io/llmops-database/multi-agent-systems-in-production-code-generation-and-review-at-scale ; https://patmcguinness.substack.com/p/the-ai-agent-architecture-debate [S]
- Codex loop: https://openai.com/index/unrolling-the-codex-agent-loop/ [S] ; https://codex.danielvaughan.com/2026/04/21/codex-cli-prompt-caching-maximise-cache-hits-cost-reduction/
- Copilot coding agent: https://docs.github.com/en/copilot/how-tos/agents/copilot-coding-agent [S]
- mini-swe-agent: https://github.com/SWE-agent/mini-swe-agent [S]
- OpenHands SDK: https://arxiv.org/pdf/2511.03690 [S]; ESAA: https://arxiv.org/html/2602.23193v1 [F]
- Failure analyses: https://arxiv.org/abs/2607.09510 ; https://arxiv.org/pdf/2605.29442 [F, abstract-level]
