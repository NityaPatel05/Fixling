# 06 - Evals for the sandboxed coding agent (LangGraph + FastAPI + Docker)

Goal: 30-50 SWE-bench-style tasks (own repos + public subset). Metrics: resolve rate, cost/task, time/task.
Sourcing note: facts below were pulled from web search/fetch on 2026-10-07; items marked (unverified) come from my own knowledge or a single secondary source.

## 1. Benchmark landscape (what each is for)

| Benchmark | Size / shape | Use for us | Source |
|---|---|---|---|
| SWE-bench Lite | 300 tasks, self-contained, fewer files edited | Cheap smoke test, but old and likely contaminated | https://www.swebench.com/SWE-bench/guides/evaluation/ |
| SWE-bench Verified | 500 human-vetted tasks (Python, 12 repos) | Public baseline for comparison to published numbers; saturated/contaminated | https://github.com/SWE-bench/SWE-bench |
| SWE-bench Verified Mini | 50 tasks (HF `MariusHobbhahn/swe-bench-verified-mini`), preserves difficulty distribution, ~5GB of images vs ~130GB | Best "cheap public subset" for the public half | https://hal.cs.princeton.edu/swebench_verified_mini |
| SWE-bench Multilingual | 300 tasks, 42 repos, 9 languages (C, C++, Go, Java, JS/TS, PHP, Ruby, Rust) | Only if our agent must handle non-Python | https://swebench.com/multilingual |
| SWE-bench Live | 1,890 tasks (latest count), 223 repos, issues since 2024, monthly updates, automated curation | Fresh, contamination-resistant public tasks; take the newest month | https://arxiv.org/html/2505.23419v2 |
| SWE-bench Pro (Scale) | 1,865 tasks: 731 public (11 GPL repos), 858 held-out, 276 commercial; Python/JS/TS/Go; min 10 changed lines, avg 107 lines / 4.1 files; human-augmented problem statements + requirements + interface specs; 50-turn cap with SWE-Agent; best public ~43.6% (Sonnet 4.5, paper time) | Harder, less contaminated; sample ~5-10 public tasks as a stretch set | https://arxiv.org/html/2509.16941v2 |
| SWE-rebench | 21k+ auto-mined Python tasks; fresh monthly slice; 5 runs per model with SEM and pass@5 | Decontaminated public tasks + methodology template | https://arxiv.org/html/2505.20411v2 |
| SWE-smith | 52k synthetic instances, 250+ repo envs; synthesize bug, keep those that break >=1 test, LLM writes issue | Synthetic bugs on OUR repos when real PRs are scarce | https://github.com/SWE-bench/SWE-smith |

Status of Verified: OpenAI announced it stopped reporting SWE-bench Verified (flawed/over-narrow tests, evidence of memorized repo details/task IDs) and moved to SWE-bench Pro. https://decrypt.co/359012/openai-benchmark-measure-ai-coding-supremacy-contaminated . Treat Verified numbers as an upward-biased sanity check, not a headline. SWE-rebench found DeepSeek-V3-0324 at 39.7% on Verified vs 21.3% on fresh tasks.

## 2. SWE-bench harness mechanics

- Instance fields (standard SWE-bench format; (unverified) from my knowledge, the fetched guide only documents predictions): `instance_id` (`owner__repo-PRnumber`), `repo`, `base_commit`, `problem_statement`, `patch` (gold), `test_patch`, `FAIL_TO_PASS`, `PASS_TO_PASS`, `environment_setup_commit`, `version`.
- Prediction file (JSON/JSONL): `{"instance_id", "model_name_or_path", "model_patch"}`; model_patch is a unified diff string. https://www.swebench.com/SWE-bench/guides/evaluation/
- Run: `python -m swebench.harness.run_evaluation --dataset_name princeton-nlp/SWE-bench_Lite --predictions_path preds.jsonl --max_workers 8 --run_id X`; newer CLI: `swebench eval verified -p preds -j N --run_id X`. Results in `logs/evaluation/<run_id>/results.json`; cached by run_id+instance_id, so change run_id when re-evaluating. https://github.com/SWE-bench/SWE-bench
- Docker images: three layers (base -> env -> instance). Default pulls prebuilt images from DockerHub; `--namespace ''` forces local build (needed on ARM). https://swebench.com/SWE-bench/reference/harness
- Resources: x86_64, >=120GB free disk, 16GB RAM, 8 cores recommended; workers < min(0.75*cores, 24). A 50-task subset only needs its own images (~5GB for Verified Mini).
- Resolved = all FAIL_TO_PASS now pass AND all PASS_TO_PASS still pass after applying `test_patch`. The agent must never see test_patch or gold patch.
- Cloud eval: `sb-cli` (hosted eval on AWS, needs API key) or Modal. https://github.com/SWE-bench/SWE-bench . Local eval of ~40 tasks is cheap, so run locally.

## 3. Cheap representative public subset

- Use `MariusHobbhahn/swe-bench-verified-mini` (50) as the anchor. HAL leaderboard shows 50-task run costs from ~$5 (Gemini 2.0 Flash) to ~$1,600 (Opus 4.1 high with heavy scaffold): cost is dominated by model and scaffold, so measure your own. https://hal.cs.princeton.edu/swebench_verified_mini
- mini-SWE-agent supports `--subset lite|verified|full|multimodal|multilingual|smith|rebench`, `--slice 0:25`, `--shuffle`, `--workers`, and caps (`MSWEA_GLOBAL_COST_LIMIT`, default per-instance `cost_limit: 3`). https://mini-swe-agent.com/latest/usage/swebench/
- mini-SWE-agent roulette post: ~$0.30/instance for GPT-5/Sonnet-4 class at max performance; gains flatten past ~50 steps; its random 50-instance draw was by chance easier than the full 500 (scores biased high), so subset luck is real. https://www.swebench.com/post-250820-mini-roulette.html
- Run mini-SWE-agent with the same model on the same public tasks as an external anchor: a gap vs your agent tells you about your scaffold, not the model.
- Public half (about 20 tasks): 10 from Verified Mini (comparable to published numbers) + 10 from newest SWE-bench Live or SWE-rebench slice (post model cutoff). Optional 5 SWE-bench Pro public.

## 4. Mining tasks from your own repos

Pipeline (SWE-bench / SWE-rebench style, https://arxiv.org/html/2505.20411v2):
1. Collect merged PRs linked to issues (GitHub GraphQL `closingIssuesReferences`, or "fixes #N" in body). Require PR touches both source and test files.
2. Split diff by path: `test_patch` = tests dirs / `*_test.*`; `patch` = rest. Problem statement = issue title+body (or PR description if no issue, lightly rewritten).
3. Environment: Dockerfile per repo at `base_commit` (SWE-rebench has an LLM draft install recipes with retries; for a few repos do it by hand).
4. Validate by execution: at base_commit with test_patch applied (record failures), then with gold patch (must pass). FAIL_TO_PASS = fail-then-pass; PASS_TO_PASS = pass-both. Keep only tasks with >=1 F2P; run 3x to drop flaky tests.
5. Manual quality filter (~5 min/task): issue unambiguous, tests do not pin unspecified names/signatures (the main Verified flaw per OpenAI), no solution leaked in issue text. SWE-bench Pro handles this by adding requirements + interface specs; do likewise when tests pin names.
6. Difficulty spread: bucket by gold-patch size (<10, 10-50, 50+ lines), aim ~30/50/20. Drop trivial one-liners (Pro excludes <10 lines).
7. Fallback if few PRs: SWE-smith-style synthetic bugs (mutate functions or revert past PRs; keep those breaking >=1 test; LLM writes the issue). Less realistic: report as separate stratum. https://github.com/SWE-bench/SWE-smith
- Related: SWE-Factory https://huggingface.co/papers/2506.10954 ; SWE-rebench harness fork https://github.com/SWE-rebench/SWE-bench-fork ; RepoLaunch (auto env setup) https://arxiv.org/pdf/2603.05026 . I did not research or verify a public "nemotron-style" mining pipeline; skip unless needed.
- Contamination for own repos: private repos are safe; public ones may be in training data, so prefer PRs merged after the model's cutoff.
- Own-repo tasks should run in the same Docker image the agent sandbox uses, with network off at grading.

## 5. Harness design for OUR agent

- Eval driver (a script, not the FastAPI app): per task, create sandbox from instance image at base_commit, call agent API with problem_statement, extract `git diff` as model_patch, write preds.jsonl, grade with swebench harness (public) or own `grade.py` (own tasks, same F2P/P2P semantics and JSON schema).
- Log per task: resolved, patch applied?, wall-clock, tokens (in / cached / out), $ cost, step count, tool errors, sandbox timeouts, termination reason (submitted / step cap / cost cap / crash). In LangGraph use a usage callback or LangSmith/Langfuse traces.
- Caps: cost $3/task (mini-SWE-agent default), 50 steps (Pro uses 50 turns; mini sees gains flatten), 30 min wall-clock.
- Frameworks: inspect_ai (UK AISI) has docker sandboxes, epochs, log viewer and an inspect_evals SWE-bench task; Task = dataset + solver + scorer, `sandbox="docker"`. Good if you expect to grow past ~100 tasks; wrapping a LangGraph agent as a solver is possible. https://inspect.aisi.org.uk/sandboxing.html . Harbor hosts SWE-bench datasets too (`harbor run -d swe-bench/swe-bench-verified`). https://hub.harborframework.com/datasets/swe-bench/swe-bench-verified . For 40 tasks a plain script + swebench harness suffices.

## 6. Variance, pass@k, statistics at small N

- Agents are stochastic. SWE-rebench runs each model 5x and reports mean resolve rate, SEM and pass@5 (e.g. LLaMa-4-Maverick mean 12.2% vs pass@5 27.6%, i.e. unreliable). Report pass@1 (mean over runs) and pass@k separately. https://arxiv.org/html/2505.20411v2
- Error bars: Anthropic's paper recommends reporting SEM/95% CI (1.96*SEM), cluster-robust SEs when tasks share a source (clustered SEs can be >3x naive; cluster by repo), and paired comparisons between configs to cut variance. https://www.anthropic.com/research/statistical-approach-to-model-evals , https://arxiv.org/pdf/2411.00640
- My calc (binomial normal approx): N=40, p=0.4 gives SE about 7.7pp, 95% CI about +/-15pp; N=50, p=0.5 about +/-14pp. You cannot distinguish 40% from 50%. Use Wilson intervals near 0/1.
- Comparing two agent versions on identical tasks: paired analysis (exact McNemar on discordant tasks, or paired bootstrap over tasks). Expect only ~10-15 discordant tasks; a 10pp gain at N=40 needs about 4 net flips beyond chance, and significance (p<0.05) needs roughly 8+ wins vs 0-1 losses. Treat small N as a regression detector for >=15-20pp changes, not for fine ranking.
- Unit: per-task resolved fraction over k=3 runs (0, 1/3, 2/3, 1); report mean, task-level bootstrap CI, pass@3. Repeats cut run noise but not task-sampling noise.
- Hold model, temperature, caps, prompts fixed; pin dataset commit and harness version.

## 7. Reporting cost and time

- Per task: tokens (in / cached-in / out), $, agent wall-clock and grader time separately, steps. Report mean, median, p90 (failed runs burn the cap, heavy tail).
- Headline: cost per RESOLVED task = total spend / #resolved. Also show resolve-rate vs cost scatter across configs (Pareto view, as HAL does).
- Include cap-terminated failures in cost. Exclude infra failures (docker pull errors) from the resolve denominator but report their count.
- Record exact model ID, price table date, scaffold commit, dataset revision.

## 8. Concrete recipe

1. Install Docker Desktop (WSL2), `pip install swebench`; sanity check that gold patches of 3 Verified Mini tasks resolve (`--predictions_path gold`).
2. Build `evals/tasks.jsonl` with 40 tasks: 20 own (stratified by repo and patch size) + 10 Verified Mini + 10 recent SWE-bench Live/SWE-rebench. Freeze commit hashes and image digests.
3. Write `run_eval.py` (sandbox up, agent call, diff extraction, grade, log) with `--k`, `--workers 4`, caps.
4. Baselines: mini-SWE-agent on the 20 public tasks (same model); your agent k=3 on all 40.
5. Report: resolve@1 mean with 95% task-bootstrap CI (cluster by repo), pass@3, $/task (mean, median), $/resolved, minutes/task (median, p90), by stratum.
6. Iterate on a 10-task dev slice; keep the other 30 as held-out test to avoid overfitting prompts (mirrors Pro's held-out set).
7. Re-mine 5-10 fresh own-repo tasks monthly to detect overfitting.

## 9. Budget estimate (assumptions stated)

Assume ~$0.30/task for a frontier model under mini-SWE-agent (roulette post) and $0.50-$1.50/task for a heavier LangGraph agent (my assumption).
- One 40-task pass: ~$12 (mini-like) to ~$60 (heavy).
- k=3 per config: ~$36-$180.
- mini-SWE-agent anchor, 20 tasks x3: ~$18.
- Worst case with every run at the $3 cap: 40 x $3 x 3 = $360 per config.
- Plan $200-400 for the first campaign (baseline + ~3 variants at k=3), less with a cheaper model.
- Compute: local Docker (8 cores/16GB) at 4 workers; ~30-50GB disk; grading 1-3 min/task, about 20-40 min per pass; agent time 5-15 min/task, 4 parallel, about 1-2.5 h per pass.
- Authoring labor is the real cost: 30-60 min per own-repo task (env + vetting), so 20 tasks is about 10-20 hours. SWE-smith-style synthesis lowers that at a realism cost.

## 10. Pitfalls
- Leakage: gold patch, test_patch or future git history visible in the sandbox (strip future commits, remove remotes, no network).
- Tests that over-constrain naming cause false negatives; add interface specs.
- Flaky tests: validate 3x.
- Image/arch mismatch on Windows/ARM: use x86_64 WSL2, or `--namespace ''`.
- Subset luck: 50-instance draws can be easier/harder than the full set; anchor with mini-SWE-agent.
- Do not compare to leaderboard numbers made with different scaffolds/caps, or on Verified (contaminated).

## Sources
- https://github.com/SWE-bench/SWE-bench
- https://www.swebench.com/SWE-bench/guides/evaluation/
- https://swebench.com/SWE-bench/reference/harness
- https://mini-swe-agent.com/latest/usage/swebench/
- https://www.swebench.com/post-250820-mini-roulette.html
- https://hal.cs.princeton.edu/swebench_verified_mini
- https://swebench.com/multilingual
- https://arxiv.org/html/2505.23419v2 (SWE-bench Live)
- https://arxiv.org/html/2509.16941v2 (SWE-bench Pro) ; https://scale.com/leaderboard/swe_bench_pro_public
- https://arxiv.org/html/2505.20411v2 (SWE-rebench)
- https://github.com/SWE-bench/SWE-smith
- https://huggingface.co/papers/2506.10954 (SWE-Factory)
- https://arxiv.org/pdf/2603.05026 (RepoLaunch)
- https://decrypt.co/359012/openai-benchmark-measure-ai-coding-supremacy-contaminated
- https://www.anthropic.com/research/statistical-approach-to-model-evals ; https://arxiv.org/pdf/2411.00640
- https://inspect.aisi.org.uk/sandboxing.html ; https://hub.harborframework.com/datasets/swe-bench/swe-bench-verified
