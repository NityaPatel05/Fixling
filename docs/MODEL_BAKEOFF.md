# Model bake-off (Phase 0)

**Status: not run yet.** This needs LM Studio and 2-3 downloaded models; see [SETUP.md](SETUP.md) §2-3. The tables below are filled in by `fixling bench-micro --write`, never by hand.

## Protocol

- 30 tasks that use the real prompts and schemas: 10 issue → spec, 10 file ranking, 10 search/replace edits (judged by AST predicates; model output is never executed).
- Same machine, same context length, same sampling settings from `config/models.yaml`; one model loaded at a time under the identifier `fixling-main`.
- Repeat each candidate at Q8 (or Q6) and, for the finalist, at Q4. Use `--k 3` for the finalists to see variance.
- Record: **JSON-valid %** (after the client's one repair retry), **valid first try %**, **accuracy %**, mean latency, tokens/s. Thinking on vs off for Qwen3-style models (`thinking_style` in `config/models.yaml`).

## Decision rule

Choose the model with the best **accuracy on the `edit` group** among those with ≥ 95% JSON-valid; break ties on tokens/s (a run issues dozens of calls, so latency compounds). Write the winner into `config/models.yaml` and record the choice, the loaded context length and the parallel-slot behaviour in `PROGRESS.md` → Decisions.

## Results

_(appended below by `fixling bench-micro --write`)_
