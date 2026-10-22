# Fixling evaluation report

_Generated 2026-10-07 16:07 UTC from `evals/results/*.jsonl` by `fixling eval-report`. All numbers are computed, none are typed in._

**No evaluation has been run yet.** Load a model in LM Studio, then:

```
uv run fixling eval --slice dev --k 1        # smoke test on the 10 dev tasks
uv run fixling eval --slice test --k 3       # held-out baseline
uv run fixling eval-report                   # regenerate this file
```

## Limitations

- **Small, synthetic-leaning task set.** The tasks are bugs in one small demo library (`minicart`): 10 hand-written
  and 10 synthetic AST mutations. Resolve rates here say how the system behaves on well-scoped single-function bugs;
  they do **not** predict performance on arbitrary real-world repositories.
- **No public-benchmark numbers yet.** SWE-bench Verified Mini and SWE-bench-Live slices are part of the plan but have
  not been run, so nothing here is comparable to published SWE-bench scores.
- **Statistical power is low.** With about 20 tasks the confidence intervals are wide; differences between
  ablations are only reported as significant when an exact McNemar test says so.
- **The grader is the same kind of artefact as the agent's tests.** Hidden tests were written by the author of the
  bugs. A fix that passes them is correct for the behaviours they check, not necessarily for every behaviour.
- **One machine, one model family.** Timings depend on the local GPU and on LM Studio's batching.
