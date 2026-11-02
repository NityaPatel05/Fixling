"""Publishes a finished run to GitHub: a draft PR for fixes and repros, an issue comment otherwise (spec E19)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fixling.config import Config
from fixling.events.store import EventStore
from fixling.gh.client import GitHubClient
from fixling.gh.git_push import push_branch
from fixling.gh.pr_body import clean_text
from fixling.runs import RunStore

LABELS = {
    "fixling": "0e8a16",
    "fixling:running": "fbca04",
    "fixling:verified": "1d76db",
    "fixling:needs-review": "d93f0b",
    "fixling:repro-only": "5319e7",
    "fixling:failed": "b60205",
}
RESULT_LABEL = {
    "verified_fix": "fixling:verified",
    "unverified_fix": "fixling:needs-review",
    "repro_only": "fixling:repro-only",
    "needs_clarification": None,
    "no_result": "fixling:failed",
}
PR_STATUSES = ("verified_fix", "unverified_fix", "repro_only")


class GitHubPublisher:
    def __init__(
        self,
        cfg: Config,
        gh: GitHubClient,
        events: EventStore,
        runs: RunStore,
        *,
        push: Callable[..., tuple[str, list[str]]] = push_branch,
        use_gitleaks: bool = True,
    ) -> None:
        self.cfg, self.gh, self.events, self.runs = cfg, gh, events, runs
        self._push = push
        self.use_gitleaks = use_gitleaks

    def publish(self, run_id: str, state: dict[str, Any], patch: str) -> str | None:
        repo, number = state["repo"], state.get("issue_number")
        outcome = state["outcome"]
        status = outcome["status"]
        pr_url: str | None = None

        if status in PR_STATUSES and patch.strip():
            pr_url = self._open_pr(run_id, state, patch, status)
        self._finish_issue(run_id, repo, number, state, status, pr_url)
        return pr_url

    # ------------------------------------------------------------------ PR
    def _open_pr(self, run_id: str, state: dict[str, Any], patch: str, status: str) -> str:
        repo, number = state["repo"], state.get("issue_number")
        base = self.gh.get_default_branch(repo)
        branch, warnings = self._push(
            repo=repo,
            token=self.cfg.settings.github_token,
            base_branch=base,
            run_id=run_id,
            issue_number=number,
            issue_title=clean_text(state.get("issue_title", ""), 100),
            patch=patch,
            policy=self.cfg.policy,
            extra_secrets=self.cfg.secrets(),
            use_gitleaks=self.use_gitleaks,
        )
        self.events.append(
            run_id, "git", "github", {"branch": branch, "base": base, "warnings": warnings}, node="finalize"
        )
        prefix = "fixling (repro only): " if status == "repro_only" else "fixling: "
        title = f"{prefix}{clean_text(state.get('issue_title', ''), 80)}" + (f" (#{number})" if number else "")
        pr = self.gh.create_pr(repo, branch, base, title, state["pr_body"], draft=True)
        url = str(pr.get("html_url", ""))
        self.events.append(
            run_id, "pr_opened", "github", {"url": url, "number": pr.get("number"), "branch": branch}, node="finalize"
        )
        self.runs.update_run(run_id, pr_url=url)
        label = RESULT_LABEL[status]
        if label and pr.get("number"):
            self.gh.add_labels(repo, int(pr["number"]), [label])
        return url

    # ------------------------------------------------------------------ issue
    def _finish_issue(
        self, run_id: str, repo: str, number: int | None, state: dict[str, Any], status: str, pr_url: str | None
    ) -> None:
        if not number:
            return
        self.gh.remove_label(repo, number, "fixling:running")
        label = RESULT_LABEL.get(status)
        if label:
            self.gh.add_labels(repo, number, [label])
        body = state.get("issue_comment") or ""
        if pr_url:
            body = f"Fixling opened a draft PR for this issue: {pr_url}\n\n" + body
        if body.strip():
            url = self.gh.comment(repo, number, body)
            self.events.append(run_id, "comment", "github", {"url": url, "issue": number}, node="finalize")
