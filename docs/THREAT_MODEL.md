# Fixling threat model

Fixling feeds **untrusted text** (issues, repository contents) to a **weak model** whose output becomes **executable code**, then optionally publishes the result under your GitHub identity. This document says what is protected, from whom, by which control (with the test that checks it), and what is *not* protected.

## Assets

| Asset | Why it matters |
|---|---|
| GitHub token (fine-grained PAT) | push access to your demo repos |
| Host machine and files | the sandbox runs code the model wrote |
| Integrity of the repository's tests and CI | a "passing" PR must mean the code is right |
| The reviewer's trust in a PR | a misleading PR is worse than no PR |
| LM Studio endpoint | local, unauthenticated |

## Trust zones

```
 TRUSTED                                              UNTRUSTED
 controller process (token, LM Studio, event log)     issue text, repo contents, model output,
 host copy of the repo at base_sha (runs/<id>/src)    everything that runs inside a sandbox
 pip index / Docker daemon (build-time only)
```

The controller never runs repository code. Sandboxes never see a secret, a network or the Docker socket.

## Attackers

1. **Malicious issue author**: controls the issue title/body; may hide instructions in Unicode tag characters, zero-width text or HTML comments, or ask for secrets ("print `.env` in the test").
2. **Malicious or careless repository content**: tests, conftest, setup files, `.pth`, symlinks that try to break out or to make a patch look verified.
3. **A misbehaving model**: the realistic threat. Small models "obey" injected text and also cheat on their own: skip tests, special-case the test input, detect the test runner.
4. *Out of scope*: a compromised host, a malicious Docker daemon, someone with write access to your repositories (they can already merge code).

## Controls and how each is checked

| Threat | Control | Checked by |
|---|---|---|
| Hidden instructions in the issue | `repo/sanitize.py` strips tag characters, zero-width/bidi characters, HTML comments, images; hidden content is recorded in a `sanitize` event, flags the run and caps confidence at *medium*; all issue/spec text is wrapped in `<untrusted_issue>` with a "data, not instructions" line | `tests/unit/test_sanitize.py`; `tests/security/test_red_team.py::test_prompt_injection_cannot_reach_protected_files_or_the_model` |
| "Print `.env`" style secret exfiltration | generated tests that reference credential files are rejected (`sensitive_path_access`); a patch containing the token or the canary never reaches disk and is blocked (`secret_in_patch`); pre-push secret scan + optional gitleaks | `test_print_dotenv_injection_is_rejected_and_the_canary_never_leaks`, `test_canary_pasted_into_a_candidate_is_dropped_before_it_touches_disk` |
| Token leakage | token only in the controller's environment; per-command `http.extraheader` (never in `.git/config`); redaction of every event, artifact, failure text and the initial issue text before checkpointing; the sandbox environment is asserted token-free | `test_token_pasted_into_an_issue_is_redacted_everywhere`, `tests/unit/test_events_store.py`, `test_no_secrets_in_the_container_environment` |
| Sandbox escape / abuse | non-root uid 10001, `cap_drop=ALL`, `no-new-privileges`, read-only rootfs, `network_mode=none`, memory/CPU/pids limits, tmpfs workspace, no host mounts, no Docker socket; agent processes have an **empty capability set** (verified from `/proc/self/status`) | `tests/integration/test_docker_sandbox.py` (27 tests on a real daemon) |
| Network exfiltration | `network_mode=none` for every sandbox; dependencies are installed only while *building* the environment image | `test_network_is_unreachable` |
| Tampering with tests | L0-L4 (see ARCHITECTURE.md §3): root-owned protected files and directories, root-owned `/workspace`, fail-closed policy gate (41 hack diffs, 14 legit diffs), fresh-container verification, junit-based verdict | `tests/unit/test_policy.py`, `test_protected_tests_cannot_be_modified_deleted_replaced_or_moved`, `test_clean_verify_catches_planted_hacks_in_a_real_container` |
| Forged diff / base commit | the base commit lives in a root-owned tmpfs outside the workspace; the *verdict* never trusts the sandbox's own diff: the patch is re-applied to a pristine tree built from the host copy | `test_diff_survives_the_agent_trying_to_hide_changes` |
| Pushing something nasty | only the controller pushes, only `fixling/*` branches, **draft** PRs only, policy gate + secret scan re-run against a fresh clone first; branch protection on `main` is your last line | `tests/unit/test_git_push.py` |
| Drive-by triggering | the poller acts only if the user who applied the label has write/maintain/admin permission; one run per label event | `tests/unit/test_poller.py` |
| Misleading PR text | PR body is a pure function of the event log with a citation per claim; model text is stripped of links, images, HTML and @mentions; `Fixes #N` only for verified fixes | `tests/unit/test_pr_body.py` |
| Runaway runs | wall-clock/token budgets, per-command timeouts, user cancel, reaper for expired containers, crash recovery that leaks nothing | `tests/unit/test_guards.py`, graph tests (cancel, budget, crash recovery) |
| Local API abuse | binds to 127.0.0.1, rejects foreign `Host` headers (DNS rebinding), no CORS, allow-listed file endpoint, local-path runs disabled unless `FIXLING_LOCAL_ROOTS` is set | `tests/unit/test_api.py` |

## Residual risks (read these)

- **Shared kernel.** Containers are not a hard isolation boundary. A kernel or runc exploit by hostile code would escape. For repositories you do not trust, run Fixling in a VM or use gVisor / a microVM (documented upgrade path, not built; gVisor is not native on Docker Desktop for Windows).
- **A privileged exec exists by design.** The controller loads the workspace and reads the base-commit diff through `docker exec --privileged -u 0` (the daemon refuses `put_archive` on read-only roots). Those commands are fixed controller code, never model output, and agent processes never receive capabilities, but it is a place where the controller's host-side correctness matters.
- **The model can still be wrong in a way the tests do not catch.** A verified fix is "correct for the checked behaviours". Confidence levels say how much corroboration exists; the evaluation's *false-pass rate* measures how often a verified claim is rejected by hidden tests. That number is unknown until the evaluation runs.
- **Same-model judge.** It only breaks ties.
- **`.env`-style files are readable inside the sandbox** (they are part of the repository). They are protected from *leaving* by the controls above, not hidden from the code that runs there.
- **PAT scope.** A fine-grained token limited to the demo repositories is assumed. A token with broader scope increases blast radius.
- **LM Studio is unauthenticated.** Anyone who can reach port 1234 can use your model; keep it on localhost.
- **No authentication on the dashboard/API.** It is localhost-only by design; do not expose it.

## Reporting

This is a personal project; if you find a bypass of the test-integrity gate, add the diff to `tests/fixtures/make_corpus.py` (`/add-hack-fixture` in the Claude Code setup) so it becomes a permanent regression test.
