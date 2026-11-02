# Fixling architecture

How one issue becomes a verified draft PR, and why each piece exists. File references are to `src/fixling/`.

## 1. The shape of a run

```
GitHub ◄──► poller ─► queue ─► worker ─► LangGraph pipeline ◄──► LM Studio :1234  (≤5B model + embeddings)
(PAT)       (gh/)     (SQLite) (worker.py)  │ every step appends events ─► SQLite + runs/<id>/ artifacts
                                            │
 FastAPI (api/): REST + SSE ◄───────────────┘            React dashboard (web/): a pure view of the log
                                            │
                       docker exec (no secrets, no network)
             ┌──────────────────┴───────────────────────────┐
     work sandbox (baseline, repro)          per-candidate sandboxes ×N        clean verify sandbox
```

The controller is the only trusted process: it holds the GitHub token, talks to the model, owns the event log and is the only thing that ever pushes. Sandboxes execute untrusted code and hold nothing.

### The pipeline (`graph/build.py`)

```
START → intake → triage ─┬─ (vague/large) ─────────────────────────────────────────────────┐
                         └→ env → localize → reproduce → ┬→ repair_one ×N ─→ select ─┬→ clean_verify → (strength) ─┤
                                                          │        ▲                  │                             │
                                                          │        └── refine ◄───────┤ (no clean candidate,        │
                                                          └→ (no targets / stopped) ──┴→ finalize ◄───────────────┘  rounds left)
finalize → END      (every stop condition routes here; it always runs and never raises)
```

| Node | Does | Model call | Failure behaviour |
|---|---|---|---|
| `intake` (`graph/nodes/intake.py`) | sanitises the untrusted issue, extracts an `IssueSpec` | `intake` | falls back to a heuristic spec, flagged `degraded_spec` |
| `triage` | ambiguity ≥ 7, size = large or kind = other → ask for clarification; `interrupt()` in interactive mode | none | auto mode ends as `needs_clarification` |
| `env` | build/reuse the per-repo image, start the work sandbox, run the baseline 3× | none | broken environment → `no_result` with the log |
| `localize` | BM25 + embeddings + hard hints → model ranks files → model picks functions | `localize` ×2 | every answer validated against the offered list; deterministic fallback |
| `reproduce` | model writes a *new* failing test; accepted only if it fails with an assertion (not import/syntax/fixture), passes the integrity rules, and is deterministic | `reproduce` ≤3 | after 3 attempts continue without one (outcome capped at `unverified_fix`) |
| `repair_one` ×N | whole-function search/replace edits, each candidate in **its own sandbox**, run repro + impacted tests | `repair` | apply/format errors recorded per candidate |
| `select` | policy gate per patch, de-duplicate, score, judge only on exact ties | `judge` (tie-break only) | no viable candidate → `refine` or `finalize` |
| `refine` | fresh prompt built from the best failed attempt and *why* it failed | via `repair_one` | ≤2 rounds |
| `clean_verify` | apply the final patch (test + fix) to a **pristine** tree in a fresh container; full suite fixed + shuffled | none | patch that does not apply is a blocking finding |
| `strength` (optional) | independent tests written without seeing the patch | `extra_tests` | soft signal only |
| `finalize` | outcome, confidence, `patch.diff`, `report.md`, push + draft PR, close all sandboxes | none | each step isolated: one failure cannot lose the others |

Nodes never raise: the `@node` wrapper (`graph/nodes/_common.py`) turns budget/cancel, LLM outages and unexpected errors into a `stop_reason`, and every later node becomes a no-op so the run flows to `finalize`. (`finalize` is exempt from the budget guard, so a *stopped* run still records its outcome and frees its sandboxes; a test caught this.)

### Why this shape (research behind the decisions)

From the project's research notes (`docs/research/`, `docs/PLAN.md`):

- **Small models loop.** SWE-Protégé (arXiv 2602.22124) reports pervasive action looping in small-model agent trajectories (31% of trajectories) and that the prior 7B state of the art resolves about 17% of SWE-bench Verified. A fixed pipeline removes the loop.
- **Resample, don't patch spans.** A placebo-controlled repair study (arXiv 2609.00854) finds fresh resampling beats localized span repair, and that sub-4B span splices often fail to parse. Hence N fresh whole-function edits filtered by *executed* tests.
- **Agentless-style localize → repair → validate** is cheap and strong; the model makes narrow decisions with schema-constrained output (LM Studio `json_schema` response format), so malformed tool calls are rare.
- **Same-model judges are not independent evidence**, so the judge only breaks exact score ties and never overrides tests.
- **Cost is GPU time, not dollars:** budgets are wall clock, tokens and attempts (`config/budgets.yaml`).

These are findings the project adopted as design inputs; the measurements that would confirm them *for this system* (the ablations in `evals/ablations.yaml`) have not been run yet.

## 2. State, events and artifacts

- **LangGraph state stays small** (`graph/state.py`): ids, specs, summaries, paths. Candidates carry compact result fields; patch text and prompts live in `runs/<id>/` and in event artifacts. A finished run's state is asserted by a test to stay under 20 KB.
- **The event log is the source of truth.** `events` rows are append-only, enforced by SQLite triggers (`db.py`). Types: `run_started issue_received sanitize spec triage env retrieval test_run repro llm_call candidate gate_finding selection judge refine verify strength outcome git pr_opened comment interrupt resume budget error`. Large outputs spill to `runs/<id>/artifacts/<seq>.txt` with a head/tail copy inline; every string passes the secret redactor first.
- **The PR body and the dashboard are pure functions of the log** (`gh/pr_body.py`; `web/src/pipeline.ts`). Each evidence line cites `[event #N]`, and model-originated text is stripped of links, images, HTML and @mentions. `Fixes #N` appears only for `verified_fix`.
- **Replay** = re-render the log up to event N. **Resume** = LangGraph checkpoint (`runs/checkpoints.sqlite`); a crash mid-run continues from the last superstep, and nodes that create sandboxes are idempotent.

## 3. Test integrity: five layers

A coding agent's classic cheat is making the tests pass instead of making the code right. Fixling holds itself to the same standard it enforces:

| Layer | What | Where |
|---|---|---|
| L0 | the sandbox sees a fresh single-commit tree, no history, no `.git`; the base commit lives outside the workspace in a root-owned mount | `repo/workspace.py`, `sandbox/docker_sandbox.py` |
| L1 | protected files, and every directory containing one, are root-owned and read-only; `/workspace` itself is root-owned so the agent cannot move `tests/` away (a hole found while testing the first design) | `repo/workspace.py::build_tree_tar` |
| L2 | fail-closed policy gate on every diff: protected paths, size, binary/symlink, header consistency, skip/xfail, `sys.exit`, constant asserts, runner sniffing, always-true `__eq__`, secrets, credential files, ... | `verify/policy.py` (41 hack / 14 legit diffs in `tests/fixtures/`) |
| L3 | verification in a **fresh container** built from the trusted host copy, junit-based (exit code 0 alone never counts) | `verify/clean_verify.py`, `verify/runner.py` |
| L4 | FAIL_TO_PASS (the reproduction test) and PASS_TO_PASS (baseline-passing tests, flaky excluded), plus a shuffled-order run | `verify/clean_verify.py::evaluate` |

The gate runs in three places (before `select`, inside `clean_verify`, and again on a fresh clone before pushing) so a bypass in one is caught by another.

## 4. Confidence and outcomes (`verify/confidence.py`)

Pure functions of recorded facts. `high` needs: verified (F2P and P2P, no blocking finding), no flags, shuffled order passes, corroboration (≥2 independent candidates produced the same patch, or independently written tests pass), and a confident judge if one ran. Missing corroboration, any flag, an order-dependent suite, or judge concerns give `medium`; `unverified_fix` is `low`; `repro_only`/`no_result` have none. Every rule that fires adds a human-readable reason that ends up in the PR.

## 5. Concurrency

One run at a time (single GPU); parallelism is *inside* a run: N candidates sampled concurrently (LM Studio continuous batching, 4 slots by default), each in its own sandbox. The event store serialises appends with a lock and a transaction; the API streams via an in-process pub/sub that subscribes before replaying, so no event can fall into the gap.

## 6. Module map

| Module | Responsibility |
|---|---|
| `llm/client.py` | LM Studio client: constrained decoding, one repair retry, usage + `llm_call` events, embeddings |
| `llm/render.py`, `llm/prompts/*.md.j2` | static SYSTEM (prefix-cache friendly) + variable USER, one worked example each (validated against the schemas by a test) |
| `repo/index.py`, `repo/retrieval.py` | AST index, repo map, RRF fusion of BM25 / embeddings / hard hints |
| `repo/edits.py` | exact → whitespace-normalised → unique-fuzzy matching with recovery-oriented errors |
| `sandbox/` | hardened Docker sandbox, images (deps baked at build time, network only then), reaper |
| `graph/` | state, guards (budgets, cancel, stuck detector), selection/scoring, nodes, graph assembly, pipeline runner |
| `gh/` | REST client (rate-limit aware), trigger-gated poller, trusted push, publisher, PR body |
| `evals/` | task format, grader, runner, statistics, report, micro-benchmark |

## 7. Known deviations from the original plan

All are recorded with reasons in `PROGRESS.md` → Decisions. The ones that matter: sandbox files are loaded with a controller-only privileged `docker exec` because the daemon refuses `put_archive` on a read-only rootfs; per-candidate sandboxes are rebuilt from the trusted tree instead of `docker commit` (tmpfs is not captured); environment images install dependencies only and the repo is imported via `PYTHONPATH`.
