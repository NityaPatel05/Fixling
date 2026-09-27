# 01 - GitHub repo research for a sandboxed issue-to-PR coding agent

Date: 2026-10-07. Stack in mind: LangGraph + FastAPI + one Docker sandbox per task + event log.
Caveat: details come from READMEs, docs and a few raw source files fetched this session. Where I only have a README-level view I say so. Items marked (memory) were not re-verified.

## The big picture (read this first)

Three schools exist, and your design should pick deliberately:

1. **Minimal loop, bash only** (mini-swe-agent, Live-SWE-agent). A linear message list, one shell tool, strong models do the rest. Scores 74%+ on SWE-bench Verified with about 100 lines of agent code. The lesson: with 2026-era models, scaffold complexity buys little; sandbox, limits and verification matter more.
2. **Production SDK with event sourcing** (OpenHands SDK). Immutable event log, condenser, stuck detector, security analyzer, agent server over REST/WebSocket. This is closest to your "FastAPI + event log" target.
3. **Workflow/pipeline** (Agentless, Open SWE's manager/planner/programmer/reviewer graphs). Fixed stages with deterministic code around the LLM. Closest to your LangGraph target.

The pragmatic synthesis: a LangGraph graph with a few coarse nodes (triage, plan, implement loop, verify, open PR), where the implement node is a mini-swe-agent-style bash loop, all state written as OpenHands-style append-only events, and deterministic middleware as the safety net (always push a branch, always open a PR, always enforce limits).

---

## mini-swe-agent (SWE-agent team) - MIT
https://github.com/SWE-agent/mini-swe-agent , docs https://mini-swe-agent.com

- **What**: Successor to SWE-agent (which is now in maintenance mode). Claims >74% SWE-bench Verified; core agent class is ~100 lines.
- **Architecture**: Four swappable layers: Agent (workflow), Environment (action execution), Model (LLM calls), Run scripts. Files: `src/minisweagent/agents/default.py`, `environments/local.py` (plus docker, podman, singularity, bubblewrap, contree), `models/litellm_model.py`.
- **History**: Strictly linear message list. No history processors, no branching. Trajectory = the prompt, which makes debugging and fine-tuning trivial.
- **Tools**: None except bash. Every action is a fresh `subprocess.run` (no persistent shell; `cd` and env vars do not persist, the prompt tells the model so). This makes swapping sandboxes trivial and avoids session-hang bugs.
- **Limits** (worth copying exactly): `step_limit`, `cost_limit` (default 3.0 USD), `wall_time_limit_seconds`, all checked at the top of `query()` before each LLM call; raises `LimitsExceeded`. Format errors do not crash: they feed an error message back, and `max_consecutive_format_errors` ends the run.
- **Termination**: Model runs `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` as the first line of output; agent raises `Submitted`, final message has role `exit` with `exit_status` and `submission` in `extra`. Control flow by exceptions (`Submitted`, `LimitsExceeded`, `FormatError`, `InterruptAgentFlow`) is a clean pattern.
- **Prompt details** (`config/default.yaml`): exactly one bash block per turn with a THOUGHT section; six-step workflow (explore, reproduce, edit, verify, edge cases, submit); env sets `PAGER=cat`, `MANPAGER=cat`, `TQDM_DISABLE=1`, pip progress off to keep output non-interactive; long observations are shown as 5k-char head + 5k-char tail with an elision note and advice to use head/tail/sed.
- **Borrow**: the limits check, exception-based control flow, head/tail truncation template, non-interactive env vars, subprocess-per-action model, "reproduce first" workflow text.

## SWE-agent - MIT
https://github.com/SWE-agent/SWE-agent , https://swe-agent.com/latest/background/architecture/

- **What**: The original agent-computer-interface (ACI) paper implementation. Maintenance mode, still the best reference for hard-won edge-case handling.
- **Architecture**: `sweagent` CLI creates `SWEEnv`, a thin wrapper over SWE-ReX that runs a server inside the Docker container; `Agent.forward()` prompts, parses, executes. Everything is driven by one YAML config (tool bundles, templates, parsers).
- **History processors** (`sweagent/agent/history_processors.py`): `LastNObservations` (replaces old observations with "(n lines omitted)"; breaks prompt caching unless `polling` batches updates), `CacheControlHistoryProcessor` (Anthropic cache marks on last N user messages), `RemoveRegex`, `ClosedWindowHistoryProcessor` (drops stale file views), `TagToolCallObservations` (mark outputs to keep). Lesson: observation masking is cheap and effective, but must be reconciled with prompt caching.
- **Error handling** (`sweagent/agent/agents.py`): `forward_with_handling()` requeries up to `max_requeries` (3) on `FormatError`, `_BlockedActionError` (blocklist), `BashIncorrectSyntaxError` (shell syntax check before running), content-policy errors; each has its own error template. `attempt_autosubmission_after_error()` salvages the patch (`/root/model.patch`, falls back to diffs from earlier trajectory steps if the runtime died). Consecutive-timeout counter (`max_consecutive_execution_timeouts`) and `total_execution_timeout`. `CostLimitExceededError` triggers autosubmit; a separate total-cost error aborts immediately.
- **Retry loop**: `RetryAgent` runs several attempts and a reviewer (`ScoreRetryLoop` or `ChooserRetryLoop`) scores or picks the best patch. Trajectory saved as `.traj` JSON (steps, history, info, replay_config).
- **Borrow**: autosubmit-on-failure (always salvage a diff), pre-execution shell syntax check, blocklist for interactive commands (vim, etc.), per-error requery templates, replayable trajectory containing the config.

## SWE-ReX - MIT
https://github.com/SWE-agent/SWE-ReX

- Runtime abstraction: `src/swerex/{deployment,runtime,server.py}`. One API over local, Docker, Modal, Fargate, Daytona; multiple parallel named shell sessions per runtime; handles command-completion detection, exit codes, interactive tools (ipython, gdb).
- **Borrow**: its shape. A small in-container HTTP server plus a deployment class that starts/stops the container is exactly the "Docker sandbox per task" you want, driven from FastAPI. You could use it directly or copy the pattern (start container, wait healthy, session create/run/close, `close()` always in `finally`).

## OpenHands and the Software Agent SDK - MIT
https://github.com/OpenHands/software-agent-sdk , paper https://arxiv.org/abs/2511.03690 (MLSys 2026), docs https://docs.openhands.dev/sdk/arch/events

The most relevant repo to your architecture. Four packages: `openhands-sdk` (agent, conversation, events), `openhands-tools` (TerminalTool, FileEditorTool, TaskTrackerTool), `openhands-workspace` (local/Docker/K8s), `openhands-agent-server` (FastAPI + WebSocket).

- **Event sourcing**: all interaction is an immutable append-only log. `LLMConvertibleEvent` subclasses (MessageEvent, ActionEvent, ObservationEvent, SystemPromptEvent, AgentErrorEvent) become LLM messages; internal events (ConversationStateUpdateEvent, CondensationRequest, Condensation, PauseEvent) do not. `Event.source` (user/agent/environment) is deliberately separate from the LLM role. Parallel tool calls share an `llm_response_id` and merge into one assistant message. Two error types: `AgentErrorEvent` (LLM-visible, recoverable) vs `ConversationErrorEvent` (terminal).
- **Why it matters**: pause, resume, deterministic replay, crash recovery and condensation all fall out of the log. Stateless components with immutable config plus a log as the only state.
- **Condenser**: `LLMSummarizingCondenser` keeps first `keep_first` (4) events, summarizes the middle when `max_size` (120) events is exceeded, keeps the tail; emits a `Condensation` event listing forgotten event IDs plus the summary, so the full log stays intact and the "view" is recomputed by filtering. Also `NoOpCondenser`, `PipelineCondenser`. Can be triggered reactively by `CondensationRequest` on context-window errors. Paper claims up to 2x API cost reduction without quality loss.
- **Stuck detector** (`openhands-sdk/.../conversation/stuck_detector.py`), scans only the last ~20 events with configurable thresholds: (1) same action + same observation repeating, (2) same action + same error, (3) agent monologue (consecutive agent messages with no user input), (4) alternating A,B,A,B pattern, (5) context-window error loop (currently disabled). Best concise stuck spec I found.
- **Security**: `SecurityAnalyzer` rates each tool call low/medium/high/unknown (LLM variant adds a `security_risk` field to the tool schema); `ConfirmationPolicy` (e.g. ConfirmRisky, default threshold high) decides whether to pause in `WAITING_FOR_CONFIRMATION`.
- **Agent server**: client posts serialized agent config to `/conversations`; server rebuilds the agent, runs the loop, streams events over WebSocket. Same code runs locally or in a container, swapped by `DockerWorkspace`.
- **Borrow**: event taxonomy, condensation-as-event, stuck-detector pattern list, risk-rating-as-tool-argument, REST + WebSocket event stream shape.

## Open SWE (LangChain) - MIT
https://github.com/langchain-ai/open-swe ; v1 blog https://www.langchain.com/blog/introducing-open-swe-an-open-source-asynchronous-coding-agent ; 2026 framework blog https://www.langchain.com/blog/open-swe-an-open-source-framework-for-internal-coding-agents

Two generations, useful contrast:
- **v1 (2025)**: LangGraph graphs: Manager (routes, creates issue context), Planner (research + plan, human can edit), Programmer (with embedded Reviewer), runs in a Daytona sandbox; "double texting" to inject instructions mid-run; GitHub issue label triggers it. Takeaway: plan approval and a reviewer inside the programmer loop were the key controls.
- **2026 rewrite (March 2026)**: built on Deep Agents + LangGraph. Pluggable sandbox providers (Modal, Daytona, Runloop, LangSmith); about 15 curated tools (files, shell, git/PR, Linear, Slack); `AGENTS.md` repo conventions plus issue/thread history assembled up front to reduce tool calls; **deterministic middleware hooks** (one injects follow-up messages mid-run, another auto-commits and opens a PR if the agent did not); subagent spawning; read-only reviewer graph. It states it formalizes patterns from Stripe (Minions), Ramp (Inspect), Coinbase (Cloudbot).
- Layout: `langgraph.json` lists graphs (`agent/server.py` main agent, `agent/reviewer.py`, `agent/analyzer.py`, ...), `agent/middleware/`, `agent/tools/`, `agent/sandboxes/`, prompts as Markdown/Jinja in `agent/resources/prompts/`; repo rules in its `AGENTS.md`: exceptions not silent error returns, prompts never hardcoded.
- **Borrow**: the middleware safety net idea (the PR gets opened even if the model forgets), per-thread persistent sandbox, sandbox provider interface, `commit_and_open_pr` as a single tool wrapping git + GitHub, prompt files separate from code.

## Aider - Apache-2.0
https://github.com/Aider-AI/aider ; repo map https://aider.chat/docs/repomap.html

Not an autonomous issue solver, but the best context and edit machinery.
- **Repo map**: tree-sitter extracts definitions and references, builds a file graph, personalized PageRank ranks by relevance to chat files, binary search over ranked entries fits a token budget (default roughly max(1024, min(max_input/8, 4096))), rendered with elided scope context. File: `aider/repomap.py`.
- **Edit formats** (whole, diff search/replace, udiff, editor-diff): tuned per model; search/replace blocks are the durable default. Fuzzy-match fallback on failed SEARCH blocks is a key robustness trick (memory).
- **Lint + test loop**: after each edit, run lint/test commands and feed failures back automatically; every change is a git commit so undo is trivial.
- **Borrow**: repo map as optional "orientation" context at task start; search/replace edit tool with fuzzy failure messages; auto-lint after edit.

## Agentless - MIT
https://github.com/OpenAutoCoder/Agentless , paper https://arxiv.org/abs/2407.01489

- No agent loop. Phases: hierarchical localization (file, then class/function, then edit lines), repair (sample many candidate diffs), patch validation (generate reproduction tests, select and run regression tests, rerank/majority vote). 32% SWE-bench Lite at $0.70 in 2024, which started the "simple beats complex" debate.
- **Borrow**: the validation half: write a reproduction test first, run existing regression tests selectively, accept a patch only if repro fails before / passes after and regressions are unchanged. N-candidate sampling plus a selector is an optional later upgrade. A fixed pipeline also maps naturally onto LangGraph nodes.

## AutoCodeRover - SONAR Source-Available License (not OSI open source)
https://github.com/AutoCodeRoverSG/auto-code-rover

- Two stages: context retrieval with AST-aware search APIs, then patch generation; optional spectrum-based fault localization when tests exist. About 46% Verified at under $0.70 per task.
- **Borrow ideas only, not code**: license restricts competitive use. Structure-aware search tools (find definition, find callers) beat grep for large repos.

## Moatless Tools - MIT
https://github.com/aorwall/moatless-tools

- Philosophy: "build good tools to insert the right context into the prompt." Action-based agents, `FileContext` tracking which code spans the model has seen, search tools, a tree-search variant, flows for function calling or ReAct. Reported 70.8% Verified with Claude 3.5 Sonnet.
- **Borrow**: FileContext idea (track and re-render the working set instead of replaying full file dumps). Lower priority.

## Live-SWE-agent - OpenAutoCoder
https://github.com/OpenAutoCoder/live-swe-agent , https://arxiv.org/abs/2511.13646

- mini-swe-agent plus a reflection prompt after each step ("should I create or revise a tool?"); agent writes Python scripts as tools at runtime. 77.4% Verified, 45.8% SWE-bench Pro, ~$0.50-0.70 per issue. Hurts weak models.
- **Borrow**: optional later feature; just a prompt change on a bash-only loop.

## SWE-smith - MIT
https://github.com/SWE-bench/SWE-smith

- Turns repos into Docker "SWE-gyms" and synthesizes tasks (LM rewrites, procedural mutations, PR mirroring, combining); 52k instances, 26k trajectories, 250+ environments. Linux only.
- **Borrow**: for evaluating YOUR agent on your own repos: mutate-and-break tasks give an automatic test oracle. Its per-repo environment-building scripts show how to auto-install and validate a repo's test suite.

## SWE-bench harness - MIT
https://github.com/SWE-bench/SWE-bench

- Three-layer Docker images (base, environment, instance) so env builds are cached; evaluation applies the model patch plus the test patch, runs specified tests, compares `FAIL_TO_PASS` vs `PASS_TO_PASS`. Local, Modal, or `sb-cli` cloud runs; results cached by run_id/instance_id; parallel workers.
- **Borrow**: image layering (base once, per-repo env image cached by dependency hash, tiny per-task layer); F2P/P2P as your definition of "tests meaningful and nothing regressed". Note: in 2026 OpenAI stopped reporting SWE-bench Verified citing contamination and flawed tests (https://openai.com/index/why-we-no-longer-evaluate-swe-bench-verified/); SWE-bench Pro is the replacement but also has audit complaints. Build your own small eval set.

## Open coding agents, 2025-2026 (reference, not deep-dived)
- **Codex CLI** (Apache-2.0, Rust, https://github.com/openai/codex): OS-level sandbox (Seatbelt on macOS, Landlock/seccomp on Linux), approval policies, `AGENTS.md`, non-interactive `exec` mode. Borrow: approval-policy vocabulary and the AGENTS.md convention.
- **OpenCode** (most starred open agent), **Cline** (Apache-2.0), **Goose** (Apache-2.0, Rust, moved to Linux Foundation's Agentic AI Foundation April 2026), **Pi**. Lists: https://github.com/bradAGI/awesome-cli-coding-agents , https://www.openhands.dev/blog/open-source-ai-coding-agents
- **Devika/OpenDevin forks**: OpenDevin became OpenHands; Devika is largely stale (memory). Skip.

## Sandbox projects

| Project | Mechanism | License | Notes |
|---|---|---|---|
| E2B (https://github.com/e2b-dev/E2B) | Firecracker microVMs, pause/snapshot, self-host via Terraform | Apache-2.0 (memory) | Needs KVM Linux to self-host |
| Daytona (https://github.com/daytonaio/daytona) | OCI sandboxes, sub-90ms start, snapshots | AGPL | **Repo archived; development went private June 2026.** Do not depend on it. Open SWE v1 used it. |
| microsandbox (https://github.com/zerocore-ai/microsandbox) | libkrun microVMs, fork/snapshot, OCI images, <100ms | Apache-2.0 | Beta; Apple Silicon / KVM / Windows WHP |
| Anthropic sandbox-runtime (https://github.com/anthropic-experimental/sandbox-runtime) | bubblewrap + seccomp (Linux), Seatbelt (macOS), proxy domain allowlist, deny-read/allow-write FS rules | Apache-2.0 | No container needed; Windows alpha |
| gVisor / Kata / Firecracker | User-space kernel vs microVM per workload | Apache-2.0 | Survey: https://dev.to/aiagentengineering/how-to-sandbox-ai-agents-in-2026-firecracker-gvisor-runtimes-isolation-strategies-14pk ; list https://github.com/restyler/awesome-sandbox |

Practical guidance: start with hardened Docker (non-root, no privileged, drop caps, read-only root plus writable workdir, CPU/mem/pids limits, per-task network) and add `--runtime=runsc` (gVisor) as a one-flag upgrade; keep a `Sandbox` interface so Firecracker/E2B can slot in. The sandbox-runtime config shape (allowedDomains, denyRead, allowWrite, denyWrite including `.git/hooks/`) is a good model for your egress allowlist and protected paths. Keep the GitHub token outside the sandbox: the sandbox produces a branch/patch, a trusted host-side service pushes and opens the PR.

---

## Specific things worth borrowing (ranked)

1. **Append-only typed event log with source vs role separation** (OpenHands `sdk/arch/events`); LLM history is a pure function of the log; condensation is just another event.
2. **Limits at the top of every LLM call** (mini-swe-agent `default.py`): steps, cost, wall time; typed exceptions; consecutive format-error counter.
3. **Autosubmit/salvage on any failure** (SWE-agent `attempt_autosubmission_after_error`) plus Open SWE's middleware that always commits and opens the PR. Every task ends with a branch and a clear status.
4. **Stuck detector** with the five OpenHands patterns over a sliding 20-event window; on trigger nudge once, then terminate.
5. **Observation hygiene**: head/tail truncation, non-interactive env vars, blocklist for interactive commands, shell syntax pre-check, masking of old observations compatible with prompt caching (batched masking).
6. **Test handling**: reproduction test first, F2P/P2P framing (SWE-bench), selective regression run (Agentless), auto-lint loop (Aider). For "write tests" tasks, require new tests to fail on the base commit and pass on the branch, and run them a few times for flake detection (my suggestion, not from a repo).
7. **Runtime abstraction** (SWE-ReX deployment/runtime split) behind a `Sandbox` protocol.
8. **Image layering and caching** (SWE-bench harness).
9. **Risk-rated tool calls plus confirmation policy** (OpenHands) for destructive actions.
10. **Eval on your own tasks** via SWE-smith-style synthetic breakage, since public benchmarks are contaminated.

## What I would not copy
- Multi-agent role proliferation (retry-reviewers, v1 Open SWE's four agents add cost; mini-swe-agent matches them with one loop).
- Agentless-style N-sample pipelines at first: expensive; one agent loop with verification fits your goal better.
- Daytona dependency (archived), AutoCodeRover code (license).

## Sources
- https://github.com/SWE-agent/mini-swe-agent (raw `agents/default.py`, `config/default.yaml`)
- https://github.com/SWE-agent/SWE-agent (raw `history_processors.py`, `agents.py`), https://swe-agent.com/latest/background/architecture/
- https://github.com/SWE-agent/SWE-ReX
- https://github.com/OpenHands/software-agent-sdk , https://arxiv.org/html/2511.03690v1 , https://docs.openhands.dev/sdk/arch/events , https://docs.openhands.dev/sdk/arch/condenser
- https://github.com/langchain-ai/open-swe (AGENTS.md) and the two LangChain blog posts above
- https://github.com/Aider-AI/aider , https://github.com/OpenAutoCoder/Agentless , https://github.com/AutoCodeRoverSG/auto-code-rover , https://github.com/aorwall/moatless-tools , https://github.com/SWE-bench/SWE-smith , https://github.com/SWE-bench/SWE-bench
- https://arxiv.org/abs/2511.13646 , https://github.com/e2b-dev/E2B , https://github.com/daytonaio/daytona , https://github.com/zerocore-ai/microsandbox , https://github.com/anthropic-experimental/sandbox-runtime , https://github.com/openai/codex
- https://openai.com/index/why-we-no-longer-evaluate-swe-bench-verified/
