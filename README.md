# Fixling

**A 4B-parameter model on a consumer GPU that turns GitHub issues into verified, tamper-checked draft PRs, with a replayable evidence trail.**

Fixling watches your repositories for issues labelled `fixling`. For each one it runs a fixed [LangGraph](https://langchain-ai.github.io/langgraph/) pipeline in which a small local model (≤5B parameters, served by [LM Studio](https://lmstudio.ai)) makes narrow, schema-constrained decisions and deterministic code does everything else. It writes a failing test that reproduces the bug, samples several candidate fixes, tests each one in a locked-down Docker sandbox, re-verifies the winner in a clean container behind anti-test-tampering gates, and opens a **draft** PR whose description is built from an append-only event log: every claim cites the event it came from.

> **Why a pipeline and not a free-roaming agent?** Frontier-model research favours minimal bash loops. With ≤5B models that flips: small models loop and stall, so the harness has to carry the weight. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the evidence and the design.

## Status

Everything below was built and tested **without a language model** (scripted test doubles stand in for it) and, where noted, against real Docker. What needs a model, a GitHub token or an npm install is listed explicitly: nothing in this table is claimed beyond what ran.

| Area | State | Verified how |
|---|---|---|
| Pipeline (intake → triage → env → localize → reproduce → repair ×N → select → refine → clean verify → finalize) | built | scripted-LLM end-to-end tests on a seeded bug; also over **real Docker sandboxes** |
| Test-integrity gate (41 hack diffs blocked, 14 legit diffs pass) + clean-container verification | built | corpus tests, red-team tests, hacks planted in a real container |
| Hardened sandbox (non-root, no caps, no network, read-only rootfs, protected tests) | built | 27 real-Docker tests |
| Crash recovery (resume from checkpoint, no leaked containers) | built | killed mid-run, resumed; local and Docker |
| GitHub integration (poller, trusted push, draft PR, labels) | built | fake GitHub + real `git push` to a local bare repo; **not run against github.com** (needs a token) |
| API + live dashboard (SSE, replay, clarify/resume) | API built and tested; dashboard **source written, not yet built** | 18 API tests; `npm install` was not run |
| Evaluation harness (20 validated tasks, ablations, report, micro-benchmark) | built | tasks validated in the real grading path; **no results yet** (needs a model) |
| **Model bake-off, real fix rates, calibration numbers** | **not done** | needs LM Studio; see [docs/SETUP.md](docs/SETUP.md) |

## Quickstart

Requires Windows/Linux/macOS, Python 3.12 via [uv](https://docs.astral.sh/uv/), Docker, and LM Studio ≥ 0.4 with a ≤5B instruct model. Full steps: [docs/SETUP.md](docs/SETUP.md).

```bash
uv sync
cp .env.example .env                 # optional for local runs
uv run fixling doctor --deep         # LM Studio, Docker, base image, sandbox isolation

# fix a bug in a local repository from a markdown issue (first line = title)
uv run fixling run --repo path/to/repo --issue-file issue.md
#   -> runs/<id>/patch.diff and runs/<id>/report.md; replay with: uv run fixling replay <id>

# the full service: poller + worker + API + dashboard
uv run fixling serve                 # http://127.0.0.1:8000
cd web && npm install && npm run build   # one-off, builds the dashboard served by `serve`
```

## What you get on a run

| Outcome | Meaning | PR |
|---|---|---|
| `verified_fix` | the reproduction test fails before and passes after the fix in a clean container, no regressions, no integrity findings | draft PR, `Fixes #N`, label `fixling:verified` |
| `unverified_fix` | best candidate has regressions, flags, or no reproduction | draft PR, label `fixling:needs-review`, no `Fixes` |
| `repro_only` | a valid failing test, no passing fix | draft PR containing only the test |
| `needs_clarification` | issue too vague/large (or paused for you in `--interactive`) | issue comment with questions |
| `no_result` | environment broken, budget hit, model unavailable | issue comment with diagnostics |

Every PR carries a **confidence level (high / medium / low)** with reasons, derived only from recorded evidence (agreeing candidates, shuffled-order pass, independent tests, integrity flags).

## Evaluating it

```bash
uv run fixling bench-micro --label qwen3-4b-q8 --write     # Phase 0: pick a model (30 small tasks, real prompts)
uv run fixling eval --slice dev --k 1                       # smoke test on 10 dev tasks
uv run fixling eval --slice test --k 3                      # held-out baseline
uv run fixling eval --config evals/ablations.yaml#n8 --slice test --k 3
uv run fixling eval-report                                  # docs/EVAL_REPORT.md + charts
```

The report states resolve@1 with bootstrap intervals, pass@k, the **false-pass rate** (verified claims the hidden tests rejected), a **calibration table** ("when Fixling says HIGH, it is right X% of the time") and paired ablations with exact McNemar tests. It is generated, never hand-edited, and says so plainly while no results exist.

## Safety in one paragraph

The controller (host process) holds the only secrets and calls the model; sandboxes get none, run as uid 10001 with an empty capability set, **no network**, a read-only root and a RAM-backed workspace in which protected tests are root-owned. Issue text and repo content are untrusted: hidden Unicode/HTML-comment instructions are stripped and flagged, everything is wrapped as data. Candidate patches are checked by a fail-closed policy gate (protected paths, skip/xfail, `sys.exit`, constant asserts, test-environment sniffing, secrets, credential-file access, ...) and then re-applied to a **pristine** tree in a fresh container, so nothing done during candidate generation can influence the verdict. Only the controller pushes, only to `fixling/*` branches, only as drafts. See [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md), including what is *not* covered.

## Layout

```
src/fixling/      config, db, events/, llm/ (client + prompts), repo/ (index, retrieval, edits, workspace, env_setup),
                  sandbox/ (docker), verify/ (runner, policy, clean_verify, confidence), graph/ (state, nodes/, build),
                  gh/ (client, poller, push, publisher, pr_body), api/, worker.py, cli.py, evals/
web/              React dashboard (the only JavaScript in the project)
demo-repos/       minicart: the library the evaluation tasks are bugs in
evals/            tasks.jsonl, ablations.yaml, results/
tests/            unit/ integration/ security/ + fixtures (hack and legit diff corpora, tiny_repo)
docs/             ARCHITECTURE, THREAT_MODEL, SETUP, MODEL_BAKEOFF, EVAL_REPORT, plan and research
```

## Development

```bash
uv run pytest -q                       # unit + local-sandbox graph tests (no Docker, no model); add `-n 8` to parallelise
uv run pytest -m docker                # real Docker sandboxes (27 + 4 pipeline tests)
uv run ruff check . && uv run ruff format . && uv run mypy src
uv run python tests/fixtures/make_corpus.py   # regenerate the policy hack/legit corpora from real git diffs
```

## Honest limitations

- Targets **well-scoped, single-function bugs in small Python repositories**; it will not rewrite your architecture. When it cannot fix an issue it ships the failing test instead.
- A 4B model fails often. The harness exists to make failure cheap, visible and non-misleading, not to hide it.
- The evaluation set is small and mostly synthetic, one demo repository; no public-benchmark numbers yet.
- Docker containers share the host kernel; for hostile repositories use gVisor or a microVM (documented upgrade path, not built).
