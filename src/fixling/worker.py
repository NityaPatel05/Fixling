"""Run queue consumer (spec E20): one run at a time (single GPU); parallelism lives inside a run."""

from __future__ import annotations

import contextlib
import json
import threading
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fixling.errors import FixlingError
from fixling.gh.client import GitHubClient
from fixling.gh.pr_body import clean_text
from fixling.gh.publisher import RESULT_LABEL
from fixling.graph.pipeline import Pipeline
from fixling.repo.workspace import copy_local, fetch_repo
from fixling.services import Services


class Worker(threading.Thread):
    def __init__(
        self,
        services: Services,
        pipeline: Pipeline,
        *,
        gh: GitHubClient | None = None,
        poll_interval_s: float = 2.0,
    ) -> None:
        super().__init__(name="fixling-worker", daemon=True)
        self.services, self.pipeline, self.gh = services, pipeline, gh
        self.poll_interval_s = poll_interval_s
        self._stop_event = threading.Event()
        self.current: str | None = None

    # ------------------------------------------------------------------ loop
    def run(self) -> None:
        self.recover_running()
        while not self._stop_event.is_set():
            row = self.services.runs.next_queued()
            if row is None:
                self._stop_event.wait(self.poll_interval_s)
                continue
            self.process(row["run_id"])

    def stop(self) -> None:
        self._stop_event.set()

    # ------------------------------------------------------------------ one run
    def process(self, run_id: str) -> None:
        self.current = run_id
        runs = self.services.runs
        runs.update_run(run_id, status="running")
        try:
            init_state, interactive = self._prepare(run_id)
            self.pipeline.start(init_state, interactive=interactive)
        except Exception as exc:
            self.finalize_on_crash(run_id, exc)
        finally:
            self.current = None
            self.services.cancel.clear(run_id)

    def _prepare(self, run_id: str) -> tuple[dict[str, Any], bool]:
        """Build the initial graph state from the issue snapshot and a pristine host copy of the repo."""
        cfg = self.services.cfg
        run_row = self.services.runs.get_run(run_id) or {}
        issue_path = cfg.run_dir(run_id) / "issue.json"
        issue: dict[str, Any] = json.loads(issue_path.read_text(encoding="utf-8")) if issue_path.exists() else {}
        repo = str(issue.get("repo") or run_row.get("repo") or "")
        src_dir = cfg.run_dir(run_id) / "src"
        if issue.get("local_path"):
            base_sha = copy_local(Path(issue["local_path"]), src_dir, issue.get("commit"))
        else:
            base_sha = fetch_repo(repo, src_dir, cfg.settings.github_token)
        self.services.runs.update_run(run_id, base_sha=base_sha)
        return (
            {
                "run_id": run_id,
                "repo": repo,
                "issue_number": issue.get("number") or run_row.get("issue_number"),
                "issue_title": issue.get("title", ""),
                "issue_body": issue.get("body", ""),
                "issue_author": issue.get("author", "unknown"),
                "src_dir": str(src_dir),
                "base_sha": base_sha,
            },
            bool(issue.get("interactive", False)),
        )

    # ------------------------------------------------------------------ recovery
    def recover_running(self) -> list[str]:
        """After a restart, continue runs that were mid-flight; mark the ones that cannot be continued."""
        recovered: list[str] = []
        for row in self.services.runs.list_runs(status="running"):
            run_id = row["run_id"]
            self.current = run_id
            try:
                if self.pipeline.state(run_id):
                    self.pipeline.recover(run_id)
                    recovered.append(run_id)
                else:
                    raise FixlingError("no checkpoint to resume from")
            except Exception as exc:
                self.finalize_on_crash(run_id, exc)
            finally:
                self.current = None
        return recovered

    # ------------------------------------------------------------------ crash path
    def finalize_on_crash(self, run_id: str, exc: BaseException) -> None:
        """Last resort when the graph itself failed: record it, tell the issue, never raise.

        Every step is best effort and independent, so one failing (say GitHub being down) cannot hide the others.
        """
        s = self.services
        message = f"{type(exc).__name__}: {exc}"
        trace = traceback.format_exc()
        with contextlib.suppress(Exception):
            s.events.append(
                run_id, "error", "system", {"kind": "crash", "error": message}, node="worker", artifact_text=trace
            )
        with contextlib.suppress(Exception):
            s.runs.update_run(
                run_id,
                status="failed",
                outcome="no_result",
                confidence="none",
                finished_at=datetime.now(UTC).isoformat(),
            )
        with contextlib.suppress(Exception):
            self.pipeline.deps.sandboxes.close_run(run_id)
        run = s.runs.get_run(run_id) or {}
        number, repo = run.get("issue_number"), run.get("repo")
        if self.gh and number and repo:
            gh = self.gh
            note = (
                f"Fixling hit an internal error and stopped: {clean_text(message, 200)}\n\n"
                f"Run `{run_id}` has the details."
            )
            with contextlib.suppress(Exception):
                gh.remove_label(repo, int(number), "fixling:running")
            with contextlib.suppress(Exception):
                gh.add_labels(repo, int(number), [RESULT_LABEL["no_result"] or "fixling:failed"])
            with contextlib.suppress(Exception):
                gh.comment(repo, int(number), note)
