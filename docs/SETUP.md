# Setup: from a fresh checkout to your first verified fix

Everything that does **not** need a language model is already tested (see the README status table). These steps cover the parts that do. Commands marked *(verify)* depend on your LM Studio version: confirm with `--help` before relying on them.

## 1. Tools

| Tool | Check |
|---|---|
| Python 3.12 via uv | `uv --version` (uv installs 3.12 itself on first `uv sync`) |
| Docker Desktop (WSL2 backend on Windows) | `docker run --rm hello-world`; give WSL ≥ 8 GB RAM and ≥ 4 CPUs |
| Git | `git config --global core.autocrlf false` (prevents CRLF noise in patches) |
| Node 20+ | only to build the dashboard: `node -v` |
| LM Studio ≥ 0.4 | https://lmstudio.ai, then enable the CLI: `lms --help` *(verify install steps for your version)* |

```bash
uv sync
uv run fixling doctor           # LM Studio will fail until step 2; Docker and the base image should pass
```

The sandbox base image (`fixling-base`) is built automatically on first use (a few minutes: apt + pip).

## 2. Load models in LM Studio

Small models degrade a lot at Q4, so prefer **Q8_0** (or Q6_K if VRAM is tight). Candidates from the plan: Qwen3-4B-Instruct-2507, Phi-4-mini-instruct (3.8B), Gemma 4 E4B (if available), plus an embedding model (`nomic-embed-text-v1.5` or `Qwen3-Embedding-0.6B`).

```bash
lms server start --port 1234
lms load <main-model-key>  --identifier fixling-main  --context-length 32768 --gpu max   # (verify flags)
lms load <embed-model-key> --identifier fixling-embed                                    # (verify flags)
curl -s http://localhost:1234/v1/models        # both identifiers must be listed
uv run fixling doctor --deep                   # all green, including sandbox isolation
```

Two things to check once, then record the answers in `PROGRESS.md` → Decisions:

1. **Parallel slots.** LM Studio serves 4 concurrent requests by default (Fixling samples 4 candidates at once). If the loaded context is *split across slots*, each request gets `context / 4`; Fixling's prompts are capped at 8000 tokens (`max_prompt_tokens`), so raise the context until each slot has ≥ 8k.
2. **Thinking style.** For Qwen3-style models set `thinking_style: qwen3` in `config/models.yaml` so Fixling appends `/no_think` to nodes configured with `thinking: false` (the default). Other families: leave `none`.

## 3. Pick a model (Phase 0 bake-off)

```bash
uv run fixling bench-micro --label qwen3-4b-q8 --write      # appends a table to docs/MODEL_BAKEOFF.md
# unload, load the next candidate under the same identifier, repeat with another --label
```

30 small tasks using the real prompts: issue → spec, file ranking, and search/replace edits. Compare JSON-valid %, first-try validity, accuracy and tokens/s (see `docs/MODEL_BAKEOFF.md`), choose, and keep that model loaded as `fixling-main`.

## 4. First run on a local repository

```bash
uv run fixling run --repo demo-repos/minicart --issue-file issue.md
```

`issue.md` is a bug report whose first line is the title, e.g. `Exactly 75.00 still pays shipping` followed by a few sentences. Watch the timeline in the terminal; afterwards:

```bash
uv run fixling replay <run_id>          # the full event timeline
cat runs/<run_id>/report.md             # what a PR body would say, with event citations
```

## 5. The dashboard

```bash
cd web && npm install && npm run build     # ONE-OFF. This is the only step that installs JavaScript dependencies
cd .. && uv run fixling serve              # http://127.0.0.1:8000  (API at /api/docs)
```

`npm run build` runs `tsc --noEmit` first. The dashboard source was written without a TypeScript compiler available, so **expect to fix a few type errors on the first build** and please say so if you hit any. For development: `npm run dev` (proxies `/api` to `fixling serve`).

## 6. GitHub mode

1. Create 1-2 test repositories you own and push your code (e.g. `demo-repos/minicart` as its own repo).
2. Create a **fine-grained PAT** limited to those repositories: Contents RW, Pull requests RW, Issues RW, Metadata R. Put it in `.env` as `GITHUB_TOKEN`, and set `FIXLING_REPOS=you/minicart`.
3. Turn on branch protection for `main` (require a PR review): Fixling can never merge, but this keeps it that way even if something else goes wrong.
4. `uv run fixling serve`. It creates the labels (`fixling`, `fixling:running`, `fixling:verified`, `fixling:needs-review`, `fixling:repro-only`, `fixling:failed`) and polls every 60 s.
5. Open an issue, add the `fixling` label. Within one run you should get a draft PR on a `fixling/issue-N-…` branch and a comment on the issue. Labels from users without write access are ignored (the poller keeps them in memory as `Poller.rejections`; they are not shown in the dashboard yet).

## 7. Evaluate

```bash
uv run fixling eval --slice dev  --k 1     # smoke test
uv run fixling eval --slice test --k 3     # held-out baseline (hours: ~3-8 min per run on a 4B model)
uv run fixling eval --config evals/ablations.yaml#n1 --slice test --k 1
uv run fixling eval-report                 # docs/EVAL_REPORT.md
```

Results append to `evals/results/<config>-<date>.jsonl` and an interrupted evaluation resumes where it stopped. `bash_loop` (pipeline vs free-form loop) is not implemented and says so.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `LM Studio is not reachable` | `lms server start --port 1234`; or set `LMSTUDIO_BASE_URL` |
| `Models not loaded in LM Studio: fixling-embed` | load the embedding model with that identifier (or retrieval silently uses BM25 only) |
| first run is slow | environment image is being built (network, once per dependency set) |
| `needs_clarification` on a reasonable issue | the model rated it ambiguous; rerun with `--interactive` or add detail |
| every candidate "edit error" | search text not copied exactly; inspect the `candidate` artifact; try a larger model or lower temperature in `config/models.yaml` |
