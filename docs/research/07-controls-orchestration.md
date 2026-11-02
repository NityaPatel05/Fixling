# 07 - Control loop and orchestration

Scope: stuck detection, limits, cost cutoffs, context condensation, event log, LangGraph structure, tool surface, GitHub integration, PR description from trace.
Convention: "SOURCED" = taken from a fetched page. "DESIGN" = my recommendation/pseudo-code, not from a source.

## 1. Stuck / loop detection

SOURCED (OpenHands SDK stuck detector, https://docs.openhands.dev/sdk/guides/agent-stuck-detector):
- Five patterns, checked on history after the last user message:
  1. same action -> same observation, 4+ times
  2. same action -> error, 3+ times
  3. monologue: 3+ agent messages with no tool call / user input
  4. alternating (A,B,A,B...) action-observation pairs, 6+ cycles
  5. repeated context-window errors
- Compares semantic content (tool name, action args, observation text, error text), NOT raw events, so timestamps/IDs don't hide a loop.
- On by default; `stuck_detection=True` on Conversation; `conversation.stuck_detector.is_stuck()`. Older implementation: https://docs.all-hands.dev/modules/python/openhands/controller/stuck
- mini-SWE-agent has no stuck detector; it relies on step_limit/cost_limit plus format-error counter (https://mini-swe-agent.com/latest/).

DESIGN: add two heuristics OpenHands lacks, both useful for a test-driven loop:
- No-progress on tests: track `failing_test_set_hash` after each test run; if unchanged across N=3 test runs, stuck.
- Diff-churn: same file edited and reverted (diff hash revisits an earlier value).

```python
def normalize(ev):            # strip noise before hashing
    s = re.sub(r"0x[0-9a-f]+|\d{4}-\d\d-\d\d[T ][\d:.]+|/tmp/\w+|\b\d+(\.\d+)?s\b", "", ev.text)
    return sha1(s[:2000].encode()).hexdigest()

def is_stuck(events) -> Optional[str]:
    pairs = [(sig(a), normalize(o)) for a, o in action_obs_pairs(events_since_last_user_msg)]
    if len(pairs) >= 4 and len(set(pairs[-4:])) == 1:                 return "repeat_action_obs"
    errs = [p for p, o in zip(pairs, obs) if o.is_error]
    if len(errs) >= 3 and len(set(errs[-3:])) == 1:                  return "repeat_action_error"
    if len(pairs) >= 6 and len(set(pairs[-6::2])) == 1 and len(set(pairs[-5::2])) == 1 and pairs[-6] != pairs[-5]:
                                                                      return "alternating"
    if trailing_messages_without_tools(events) >= 3:                  return "monologue"
    if len(test_failure_hashes) >= 3 and len(set(test_failure_hashes[-3:])) == 1: return "no_test_progress"
    return None
```
Response ladder (DESIGN): 1st detection -> inject corrective user message ("You repeated X; try a different approach, re-read the error") and clear the repeat window; 2nd -> force condensation + restate the task; 3rd -> terminate with status `stuck`, still open a draft PR / comment with partial work and trace. Never loop silently.

## 2. Max iterations, retries, limits

SOURCED (mini-SWE-agent, https://mini-swe-agent.com/latest/ and its fetched `agents/default.py` loop):
- `query()` checks before every model call: `0 < step_limit <= n_calls`, `0 < cost_limit <= cost`, wall-clock limit -> raises LimitsExceeded / TimeExceeded. Defaults reported: step_limit 100, cost_limit $1.00 (secondary: https://www.mintlify.com/cooperbench/CooperBench/cli/agent-config).
- Format errors: counter `n_consecutive_format_errors`, reset on any clean step, exit only past `max_consecutive_format_errors`.
- Each action is `subprocess.run` (stateless), history strictly linear, `save()` in `finally` so a trajectory exists even on crash.

SOURCED (LangGraph): default `recursion_limit` is 25 super-steps; raises `GraphRecursionError`; set as top-level config key, not under `configurable`; a model->tools round trip uses 2 steps (https://fast.io/resources/langgraph-recursion-limit/, https://www.mintlify.com/langchain-ai/langgraph/api/errors; secondary sources, verify on your pinned version).

DESIGN - layered limits, all enforced by YOUR code, not only by the framework:
| Limit | Suggested | On breach |
|---|---|---|
| model calls / task | 100-150 | graceful finish |
| recursion_limit | 2*max_calls + 20 | catch GraphRecursionError -> finish |
| wall-clock / task | 30-45 min | graceful finish |
| single command timeout | 120-600 s, kill process group | return timeout observation |
| observation size | truncate to ~10k chars head+tail | add "[truncated N chars]" |
| consecutive tool/format errors | 5 | stuck |
| cost / task | $ cap (see s3) | graceful finish |
| LLM API 429/5xx retries | exponential backoff + jitter, 5 tries; not counted against step budget | fail task |

Graceful finish = a terminal node (not an exception that skips cleanup): commit WIP to branch, push, open draft PR labeled `agent-incomplete`, comment reason. Idempotency: branch name `agent/issue-<n>-<runid>` so whole-task retries don't collide.

## 3. Hard cost cutoffs

SOURCED (LiteLLM, https://docs.litellm.ai/docs/proxy/users):
- `POST /key/generate {"max_budget": 10, "budget_duration": "30d"}`; spend tracked per key; once crossed, requests fail with `ExceededTokenBudget` auth error (newer: `BudgetExceededError`); `GET /key/info?key=...` returns `spend` and `max_budget`. Per-model-per-key budgets via `model_max_budget` (https://docs.litellm.ai/docs/proxy/budget_fallbacks).
- Mastra ships a CostGuardProcessor that checks cost before each LLM call (https://mastra.ai/reference/processors/cost-guard-processor.md) - same pattern as mini-SWE-agent.

DESIGN - two independent fences:
1. In-process (precise, per task), wrapping every model call:
```python
class Budget:
    def charge(s, resp):
        s.usd += litellm.completion_cost(completion_response=resp)
        s.tin += resp.usage.prompt_tokens; s.tout += resp.usage.completion_tokens
    def check(s):   # BEFORE the call, with worst-case estimate of next call
        if s.usd + est_next_call() > s.max_usd: raise BudgetExceeded
```
   Price cache read/write tokens separately or spend will be misreported.
2. Gateway (hard; survives agent bugs or prompt injection): mint a LiteLLM virtual key PER TASK with `max_budget = task_cap * 1.2`, short duration, `models` allowlist; give only that key to the agent container; delete it at task end. Add a team/global daily budget as ceiling.
Persist `cost_usd`, tokens, calls in the task row and the PR description.

## 4. History condensation / context management

SOURCED (OpenHands, https://docs.openhands.dev/sdk/arch/condenser):
- Condensers: NoOp, LLMSummarizing, Pipeline (chain).
- LLMSummarizing defaults: `max_size=120` events triggers; `keep_first=4` verbatim (system prompt + task); target after = max_size//2 = 60; keeps first N + last M, summarizes the middle into one summary event; later summaries fold earlier ones in. Also a manual trigger: `CondensationRequest` when the LLM returns a context-window error. Reported up to ~2x API cost reduction with no loss in performance. The full log keeps full fidelity; condensation only changes the LLM view.
- SWE-agent has pluggable history processors (https://swe-agent.com/latest/config/config/ confirms config-driven processors exist; specifics not verified).
- mini-SWE-agent: no condensation, linear history.

DESIGN cheap-first ladder:
1. Truncate each observation at write time (head+tail).
2. Mask old observations: after ~10 turns replace tool-output bodies with `[output elided, 2.1k chars, see event #123]`, keep the action (no LLM call).
3. LLM-summarize the middle only when tokens > ~60% of window; summary must preserve task, files touched, commands that worked/failed, test status, open hypotheses, next step.
4. Pinned scratchpad (plan / todo / findings) the agent rewrites; always in context.
5. Prompt caching: keep the prefix stable; condensation invalidates cache, so condense in big infrequent steps.

## 5. Event-sourced log

SOURCED (OpenHands events, https://docs.openhands.dev/sdk/arch/events): typed immutable Pydantic events in an append-only log. LLM-visible: MessageEvent, ActionEvent (tool call + reasoning + security risk), ObservationEvent, SystemPromptEvent, AgentErrorEvent (tool failure the model sees; run continues). Internal: ConversationStateUpdateEvent, Condensation/CondensationRequest, PauseEvent. ConversationErrorEvent terminates the run. `source` (user/agent/environment) is distinct from LLM `role`; do not derive one from the other.

DESIGN schema (JSONL + DB table, one row per event, seq is the key):
```json
{"run_id":"r_01H..","seq":42,"ts":"2026-10-07T12:00:00Z",
 "type":"action|observation|message|error|condensation|state|limit|stuck",
 "source":"agent|environment|user|system",
 "cause":41,
 "tool":"bash","args":{"cmd":"pytest -x tests/test_a.py","timeout":300},
 "result":{"exit_code":1,"stdout_trunc":"...","stderr_trunc":"...","truncated":true,
           "stdout_sha":"..","artifact_uri":"s3://.../42.out","duration_ms":8123},
 "llm":{"model":"...","in":12000,"out":800,"cache_read":9000,"cost_usd":0.043,"request_id":".."},
 "tags":{"kind":"test_run|edit|read|git|install",
         "tests":{"passed":10,"failed":2,"failed_ids":["tests/test_a.py::t1"]}},
 "git":{"head":"abc123","diff_sha":"def456"}}
```
`cause` = seq of the action an observation answers. Rules: append-only; full outputs stored as artifacts with a truncated copy inline; classify `tags.kind` at write time so PR generation and stuck detection don't re-parse; redact secrets before persisting. Replay: LLM view = `condenser(events)`, a pure function of the log, so crash recovery = reload events and rebuild the view. Replay does not re-execute side effects; observations are recorded, not recomputed.

## 6. LangGraph patterns

SOURCED:
- Checkpointer saves a StateSnapshot each super-step; `thread_id` is the cursor; use a durable checkpointer in prod. `interrupt()` pauses and persists indefinitely; resume with `Command(resume=...)`, which becomes the return value of `interrupt()`. (https://docs.langchain.com/oss/python/langgraph/interrupts, https://docs.langchain.com/oss/python/langgraph/human-in-the-loop)
- Open SWE v1 (2025): Manager -> Planner -> Programmer -> Reviewer, separate state per agent, plan approval, mid-run user feedback, GitHub label `open-swe-auto` triggers, tracking issue, PR linked to the issue, Daytona sandbox (https://www.langchain.com/blog/introducing-open-swe-an-open-source-asynchronous-coding-agent).
- Open SWE 2026 (Deep Agents based, MIT): ~15 curated tools, AGENTS.md + issue/thread history as context, sandboxes (Modal/Daytona/Runloop/LangSmith), subagents with isolated context, deterministic middleware `check_message_queue_before_model` (inject follow-ups) and `open_pr_if_needed` (safety net so a PR always gets made) (https://www.langchain.com/blog/open-swe-an-open-source-framework-for-internal-coding-agents, https://devops.com/open-swe-captures-the-architecture-that-stripe-coinbase-and-ramp-built-independently-for-internal-coding-agents/).

DESIGN graph (inner loop is a plain ReAct loop; policy lives in deterministic nodes):
```
START -> parse_issue -> setup_sandbox(clone, branch) -> [plan (optional interrupt)]
      -> agent_loop{ guard -> call_model -> exec_tools -> post_step } -> verify -> finalize -> END
guard:     checks budget, step/time, is_stuck(); routes to call_model | recover | finalize(status)
post_step: append events, condense if needed, update test-progress hash
verify:    orchestrator runs full tests + lint itself; on fail with budget left -> back to agent_loop (max 2 rounds)
finalize:  commit, push, open PR (draft if failing or limit hit), write body from trace. Always runs.
```
```python
class S(TypedDict):
    task: Task; events_cursor: int; budget: BudgetState; status: str
    stuck_strikes: int; verify_rounds: int      # small state; events live in DB
app = g.compile(checkpointer=PostgresSaver(...))
cfg = {"configurable": {"thread_id": run_id}, "recursion_limit": 300}
```
Notes: keep big data (events, diffs) out of graph state or checkpoints bloat; tool side effects must be idempotent since a node can re-run after resume; subagents ("explore repo", "reproduce bug") as subgraphs with their own message list that return a summary; FastAPI is a thin enqueue/status layer, graph runs in a worker, stream events over SSE from the event table. Human `interrupt` only at plan approval and risky commands; optional for an autonomous MVP.

## 7. Bash-only vs structured tools

SOURCED: mini-SWE-agent bash-only scaffold scores >74% on SWE-bench Verified (https://mini-swe-agent.com/latest/; https://www.swebench.com/bash-only.html returned 404 on fetch, claim from search snippet). SWE-agent originally argued a purpose-built ACI gave +10.7 pts over raw shell at publication time; it is now "superseded by mini-swe-agent" (https://swe-agent.com/latest/config/config/). A 2026 paper studies when restricting to execute_code helps (https://huggingface.co/papers/2607.10569.md, only seen in snippet). Open SWE ships ~15 curated tools.

DESIGN recommendation: hybrid, small surface.
- `bash` (stateless per call, timeout, fixed cwd) as workhorse; modern models are strong at it.
- A few structured tools where bash is error-prone or you need telemetry: `str_replace_edit` (exact, unique match, returns snippet), `view_file(range)`, `run_tests` (parses results into pass/fail IDs, feeding the no-progress detector and PR body), `finish(summary)`.
- GitHub operations (PR, comments) are NOT sandbox tools; the orchestrator does them with credentials the sandbox never sees.
- Command policy in the exec layer: block `git push`, `curl | sh`, writes outside workspace; network allowlist for package registries.

## 8. GitHub integration

SOURCED: installation token via `POST /app/installations/{id}/access_tokens` with an app JWT, valid 1 hour, scoped to repos/permissions; Octokit auto-refreshes (https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app). Create branch: `POST /repos/{owner}/{repo}/git/refs {"ref":"refs/heads/x","sha":"<base sha>"}`; fails on empty repos (https://docs.github.com/en/rest/git/refs#create-a-reference).

DESIGN:
- GitHub App, not PAT. Permissions: Contents RW, Pull requests RW, Issues RW, Metadata R (Checks R if reading CI). Webhooks: `issues` (labeled `agent-fix`), `issue_comment` (`/agent retry`). Verify `X-Hub-Signature-256`; dedupe by `X-GitHub-Delivery`; return 202 and enqueue.
- Per-task installation token restricted to the single repo; orchestrator uses it for clone/push; agent container has no token and cannot push.
- Flow: fetch issue (title, body, comments, labels) -> wrap as UNTRUSTED data (prompt injection) -> base SHA -> create branch `agent/issue-N-<run>` -> agent works -> orchestrator runs `git diff`, secret scan, size cap -> push -> `POST /repos/{o}/{r}/pulls {title, head, base, body, draft}`; include `Closes #N` only if verify passed -> comment on issue with PR link and cost -> label `agent-generated`. Never push to the default branch; keep branch protection and required review.
- Issue parsing: extract `{summary, repro_steps, expected, actual, mentioned_files, acceptance_criteria}`; if too ambiguous, comment a clarifying question rather than burn budget.
- Handle 403/429 secondary rate limits using Retry-After.

## 9. PR description from the trace

DESIGN: build deterministically from events; LLM only polishes prose, never invents facts.
```python
facts = {
 "issue": task.issue_url,
 "files_changed": git_diff_numstat(),
 "commands": [e for e in events if e.type=="action" and e.tags.kind in {"test_run","install","git"}],
 "tests_before": first_test_run_failures,
 "tests_after":  final_verify_result,        # orchestrator's own run, not the agent's claim
 "new_tests": added_test_files(diff),
 "stats": {"calls": n, "cost_usd": c, "duration": d, "condensations": k},
 "caveats": [],
}
caveats += ["Tests still failing: ..."]           if verify_failed
caveats += ["Stopped by limit: budget|steps|stuck"] if status != "done"
caveats += ["Modified dependency/lock files"]     if touches(lockfiles)
caveats += ["Skipped tests: ..."]                 if any_skipped
caveats += ["Command failed then worked around: ..."]  # nonzero exit followed by a different approach
```
Template:
```
## Summary        (LLM, from issue + diff, 2-4 lines)   Fixes #N
## Changes        (files + one line each, from diff)
## Verification
- Reproduced before fix: `pytest tests/test_a.py::t1` -> FAIL (event #31)
- After fix: full suite 214 passed, 0 failed, 3 skipped (orchestrator run, #188)
- Commands run: <details> deduped, with exit codes </details>
## Caveats / needs human attention   (from `caveats`; "None observed" only if truly empty)
## Run info       cost $0.84, 47 calls, 11m, run id, trace link
```
Rules: every claim cites an event seq; a Verification line is emitted only if backed by an event with matching exit code; if the agent's final message disagrees with orchestrator verification, the orchestrator wins and flags it; PR is draft when verification failed or status != done.

## 10. Source index
- OpenHands stuck detector: https://docs.openhands.dev/sdk/guides/agent-stuck-detector ; https://docs.all-hands.dev/modules/python/openhands/controller/stuck
- OpenHands events/condenser: https://docs.openhands.dev/sdk/arch/events ; https://docs.openhands.dev/sdk/arch/condenser
- mini-SWE-agent: https://mini-swe-agent.com/latest/ ; https://www.swebench.com/bash-only.html
- SWE-agent config: https://swe-agent.com/latest/config/config/
- LiteLLM budgets: https://docs.litellm.ai/docs/proxy/users ; https://docs.litellm.ai/docs/proxy/budget_fallbacks
- LangGraph: https://docs.langchain.com/oss/python/langgraph/interrupts ; https://docs.langchain.com/oss/python/langgraph/human-in-the-loop ; https://fast.io/resources/langgraph-recursion-limit/
- Open SWE: https://www.langchain.com/blog/introducing-open-swe-an-open-source-asynchronous-coding-agent ; https://www.langchain.com/blog/open-swe-an-open-source-framework-for-internal-coding-agents ; https://devops.com/open-swe-captures-the-architecture-that-stripe-coinbase-and-ramp-built-independently-for-internal-coding-agents/
- GitHub: https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app ; https://docs.github.com/en/rest/git/refs#create-a-reference

Gaps: did not read OpenHands stuck.py source (404), SWE-agent history-processor docs, or the execute_code ablation paper; LangGraph recursion facts are from secondary sources.
