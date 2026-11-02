"""Label poller (spec E19): turns ``fixling``-labelled issues into queued runs, but only for trusted triggerers."""

from __future__ import annotations

import contextlib
import json
import threading
from typing import Any

from fixling.config import Config
from fixling.errors import GitHubError
from fixling.gh.client import WRITE_ROLES, GitHubClient
from fixling.runs import RunStore

TRIGGER_LABEL = "fixling"
RUNNING_LABEL = "fixling:running"


class Poller:
    def __init__(self, cfg: Config, gh: GitHubClient, runs: RunStore) -> None:
        self.cfg, self.gh, self.runs = cfg, gh, runs
        self._seen: set[str] = set()  # trigger keys already evaluated (accepted or rejected)
        self.rejections: list[dict[str, Any]] = []  # surfaced by the API: who tried, and why they were ignored

    def poll_once(self) -> list[str]:
        """One pass over every configured repo; returns the ids of newly queued runs."""
        queued: list[str] = []
        for repo in self.cfg.settings.repos:
            try:
                issues = self.gh.list_labeled_issues(repo, TRIGGER_LABEL)
            except GitHubError:
                continue  # transient (rate limit, outage): try again next pass
            for issue in issues:
                run_id = self._handle(repo, issue)
                if run_id:
                    queued.append(run_id)
        return queued

    def _handle(self, repo: str, issue: dict[str, Any]) -> str | None:
        number = int(issue["number"])
        event = self.gh.latest_label_event(repo, number, TRIGGER_LABEL)
        if event is None:
            return None
        key = f"{repo}#{number}@{event['id']}"
        if key in self._seen:
            return None
        self._seen.add(key)
        actor = str((event.get("actor") or {}).get("login", ""))
        role = self.gh.get_permission(repo, actor) if actor else "none"
        if role not in WRITE_ROLES:
            self.rejections.append({"key": key, "actor": actor, "role": role, "repo": repo, "issue": number})
            return None  # a drive-by label from someone without write access never starts a run
        run_id = self.runs.create_run(
            repo=repo,
            issue_number=number,
            issue_url=str(issue.get("html_url", "")),
            trigger_key=key,
            model=self.cfg.models.main_model,
            status="queued",
        )
        if run_id is None:
            return None  # already queued by an earlier poll or a restart
        self._save_issue(run_id, repo, issue, actor, event)
        self._swap_labels(repo, number)
        return run_id

    def _save_issue(self, run_id: str, repo: str, issue: dict[str, Any], actor: str, event: dict[str, Any]) -> None:
        """Snapshot what the worker needs, so the run never depends on the issue staying unchanged."""
        d = self.cfg.run_dir(run_id)
        d.mkdir(parents=True, exist_ok=True)
        snapshot = {
            "repo": repo,
            "number": issue["number"],
            "title": issue.get("title", ""),
            "body": issue.get("body") or "",
            "author": (issue.get("user") or {}).get("login", "unknown"),
            "url": issue.get("html_url", ""),
            "label_actor": actor,
            "label_event_id": event.get("id"),
        }
        (d / "issue.json").write_text(json.dumps(snapshot, indent=2), encoding="utf-8")

    def _swap_labels(self, repo: str, number: int) -> None:
        with contextlib.suppress(GitHubError):  # the unique trigger key already prevents a duplicate run
            self.gh.add_labels(repo, number, [RUNNING_LABEL])
            self.gh.remove_label(repo, number, TRIGGER_LABEL)


class PollerThread(threading.Thread):
    def __init__(self, poller: Poller, interval_s: float = 60.0) -> None:
        super().__init__(name="fixling-poller", daemon=True)
        self.poller, self.interval_s = poller, interval_s
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.is_set():
            with contextlib.suppress(Exception):  # the poller must never die
                self.poller.poll_once()
            self._stop_event.wait(self.interval_s)

    def stop(self) -> None:
        self._stop_event.set()
