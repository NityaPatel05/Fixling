# Fixling — Project Plan (overview)
### A local, small-model (≤5B, LM Studio) agent that turns GitHub issues into tested draft PRs, orchestrated with LangGraph

> Full build spec (modules, interfaces, configs, tests, phases): see **IMPLEMENTATION_PLAN.md**.
> Research this is based on: `research/01..07`.

## 1. Context
You researched how to build a sandboxed agent that turns an issue into a branch with tests and a PR. Your constraints:
- LLM runs **locally in LM Studio on your GPU, model ≤ ~5B parameters**
- **LangGraph** orchestrates everything
- **Python** target repos (v1)
- **GitHub polling + local web dashboard**
- Goal: **portfolio / job showcase**: real-world, unique, measurable

The research assumes frontier models paid per dollar. With a ≤5B model the central conclusion flips, so the architecture changes.

## 2. The key design decision
| Research says (frontier models) | Reality for ≤5B local models | Decision |
|---|---|---|
| Minimal bash-only loop wins (mini-swe-agent 74%) | Small models loop and stall (SWE-Protégé: 31% of trajectories loop; prior 7B SOTA about 17% on SWE-bench Verified) | **Fixed LangGraph pipeline.** The model makes narrow decisions; deterministic code does the rest |
| Patch the localized span | Fresh resampling beats localized infill; sub-4B span splices fail to parse 65% of the time | **N fresh whole-function edits, filtered by executed tests** |
| Cost = dollars | Cost = GPU time + tokens, $0 | Budgets in tokens, wall-clock and attempts |
| Long contexts + condensation | 4B models: 16-32k context, quality drops with long prompts | **Fresh, short (< 8k) prompt per node** |
| Free-form tool calls | Small models break JSON | **LM Studio JSON-schema constrained output** for every LLM decision |

**Unique pitch:** *"Harness over model size: a 4B model on a consumer GPU that turns issues into verified, tamper-checked draft PRs, with a replayable evidence trail and published ablations."*

**Honest scope:** targets well-scoped bugs (single function, clear repro, small repos). When it can't fix an issue it still ships a **"repro-only" draft PR** with a validated failing test.

## 3. Assessment of your research lessons
**Keep:** trust zones (controller holds secrets; sandbox has none, `--network none`) · controller pushes + opens PR · history-stripped single-commit workspace · test-protection layers L0-L4 · F2P/P2P · reproduction test first · submit best, not last · append-only event log · own eval set with k=3 and bootstrap CIs.

**Adapt:** bash loop → optional fallback and ablation · stuck detector → fallback loop + duplicate-candidate detection · LLM judge → tie-breaker only (same small model isn't independent) · hidden tests → second test-gen call, soft flag · mutation testing → optional, time-capped · GitHub App → fine-grained PAT for v1 · webhooks → polling · keep stable prompt prefix (llama.cpp prefix-cache reuse).

**Drop/defer:** gVisor (not native on Docker Desktop for Windows; documented upgrade path) · LiteLLM budget keys · LLM condensation · multi-agent roles · SWE-bench Pro.

## 4. Model setup (LM Studio)
- LM Studio ≥ 0.4: OpenAI-compatible API, `response_format: json_schema`, **4 parallel requests** (continuous batching), embeddings, `lms` CLI.
- Bake off (week 1): Qwen3-4B-Instruct (newest 4B), Phi-4-mini (3.8B), Gemma 4 E4B, at **Q8/Q6** (small models degrade at Q4).
- Small embedding model (nomic-embed-text-v1.5 or Qwen3-Embedding-0.6B) for retrieval.
- Thinking on/off configurable per node; optional "expert escalation" hook (off by default) as a stretch ablation.

## 5. Architecture
```
            ┌──────────── Controller (Windows host, trusted) ────────────┐
GitHub ◄──► │ poller ─► queue ─► worker ─► LangGraph pipeline            │ ◄──► LM Studio :1234
 (PAT)      │                  │            │ events ─► SQLite + artifacts│      (4B model + embeddings)
            │ FastAPI (REST + SSE) ◄─────────┘                             │
            │ React dashboard (timeline, diff, replay, approve/clarify)    │
            └───────────────┬──────────────────────────────────────────────┘
                            │ docker exec (no secrets, --network none)
              ┌─────────────▼─────────────┐   ┌─────────────────────────────┐
              │ Work sandbox (per run)    │   │ Clean verify sandbox (fresh)│
              │ repo @ base, deps baked   │   │ base + patch only, ro tests │
              └───────────────────────────┘   └─────────────────────────────┘
```

### LangGraph pipeline
```
START → intake (issue → IssueSpec JSON, sanitized untrusted text)
      → triage (too vague/large → interrupt for clarification)
      → env (per-repo image, baseline test runs ×3, flaky detection)
      → localize (stacktrace/symbol hints + BM25 + embeddings → LLM ranks files → picks functions)
      → reproduce (LLM writes a NEW failing test; must fail with AssertionError, ≤3 tries)
      → repair fan-out (Send API, N=4 parallel, varied temperature; whole-function search/replace)
      → apply + test each candidate in its own sandbox
      → select (dedupe, policy gate, score; judge only on ties)
      → refine (≤2 rounds, fresh prompt) if nothing passes
      → clean_verify (fresh container, protected tests restored, full suite fixed + shuffled)
      → strength (optional: mutation + independent tests)
      → finalize (ALWAYS: outcome + confidence, push branch, draft PR with evidence, issue comment)
END
```
Outcomes: `verified_fix` · `unverified_fix` · `repro_only` · `needs_clarification` · `no_result`.

### Test-integrity gates
L0 fresh single-commit workspace · L1 protected files root-owned/read-only in the sandbox · L2 diff gate (protected paths, AST hack rules: skips, `sys.exit`, constant asserts, `PYTEST_CURRENT_TEST` sniffing, literal special-cases) · L3 clean-container verify · L4 F2P/P2P · hack-diff corpus as regression tests.

### Security
Hardened container (non-root, cap-drop ALL, no-new-privileges, read-only rootfs, tmpfs, limits, `--network none`) · network only during dependency install · trigger gate (collaborators only) · sanitizer for hidden unicode/HTML comments · gitleaks pre-push · PR body built from facts · draft only, `fixling/` branch prefix · reaper · canary + prompt-injection red-team tests · THREAT_MODEL.md.

### Event log and PR
Append-only SQLite events (spec, retrieval, llm_call, exec, test_run, candidate, gate_finding, verify, outcome, pr_opened…). The dashboard, replay and PR body are pure functions of the log; every PR claim cites an event number. The PR shows Evidence, Integrity checks, and **Confidence: High/Medium/Low** with reasons.

## 6. Tech stack
Python 3.12 + uv · LangGraph + langgraph-checkpoint-sqlite · openai SDK → LM Studio · pydantic v2 · docker SDK · FastAPI + SSE · httpx (GitHub REST) · rank-bm25 + numpy · stdlib ast · unidiff · junitparser · pytest (+pytest-randomly, pytest-timeout in sandbox) · React + Vite + TS + Tailwind.

## 7. Evals (portfolio centerpiece)
40 tasks: 20 own (demo repos: 10 hand-written + 10 synthetic AST-mutation bugs) + 10 SWE-bench Verified Mini (smallest, labeled "easy subset") + 10 recent SWE-bench-Live/SWE-rebench. 10 dev / 30 held-out. k=3.
Metrics: resolve@1 ± bootstrap CI, pass@3, **confidence calibration**, false-pass rate, repro-only rate, time and tokens per resolved task.
Ablations: N=1/4/8 · with/without repro · BM25 vs +embeddings · gates on/off · Q4 vs Q8 · **pipeline vs bash loop at 4B** · alternative 4B model.

## 8. Demo repos
`minicart` (pricing/discounts/rounding), `textkit` (text utilities), optional `dateslice`: 800-2000 LOC each, 40-120 tests, 10 seeded realistic issues each (including vague ones and prompt-injection attempts).

## 9. Timeline (about 10 weeks)
| Phase | Weeks | Deliverable |
|---|---|---|
| 0 Model bake-off | days 1-4 | micro-benchmark table, model chosen |
| 1 Foundations | 1-2 | LLM client, event store, hardened sandbox, env setup, retrieval, edits, minicart repo |
| 2 LangGraph MVP | 3-4 | full pipeline via CLI, local patch + report, resume after crash |
| 3 Verification | 5 | policy gates + hack corpus, clean verify, confidence, repro-only |
| 4 GitHub | 6 | poller, push, draft PR with evidence, security tests |
| 5 Dashboard | 7 | FastAPI + SSE, React run timeline, candidates, clarify/resume |
| 6 Evals | 8-9 | EVAL_REPORT.md with CIs, calibration, ablations |
| 7 Polish | 10 | README + demo GIF, ARCHITECTURE.md, THREAT_MODEL.md, 1-2 stretch items |

## 10. Verification of the build
Unit tests (edits, policy corpora, runner, retrieval, guards, PR body) · integration with FakeLLM + Docker · security tests (canary, injection, network, token absence) · live smoke on minicart · eval smoke `fixling eval --slice dev --k 1` · full suite before each phase is called done.

## Sources (new this session; the rest are in research/)
- LM Studio API: https://lmstudio.ai/docs/developer/api-changelog , https://lmstudio.ai/docs/developer/openai-compat/structured-output
- SWE-Protégé: https://arxiv.org/html/2602.22124
- Fresh resampling vs localized repair: https://arxiv.org/html/2609.00854v1
- Local model VRAM tiers: https://insiderllm.com/guides/best-local-coding-models-2026/
- gVisor on Docker Desktop: https://dev.to/rimelek/using-gvisors-container-runtime-in-docker-desktop-374m
