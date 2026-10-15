from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from fixling.config import Config
from fixling.db import Database
from fixling.errors import GitHubError
from fixling.events.store import EventStore
from fixling.gh.publisher import GitHubPublisher
from fixling.runs import RunStore
from fixling.services import build_services
from fixling.worker import Worker

PATCH = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"


class FakeGH:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.fail_on: set[str] = set()

    def _log(self, name: str, *args: Any) -> None:
        self.calls.append((name, *args))
        if name in self.fail_on:
            raise GitHubError(f"{name} failed")

    def get_default_branch(self, repo: str) -> str:
        self._log("default_branch", repo)
        return "main"

    def create_pr(
        self, repo: str, head: str, base: str, title: str, body: str, *, draft: bool = True
    ) -> dict[str, Any]:
        self._log("create_pr", repo, head, base, title, body, draft)
        return {"number": 7, "html_url": f"https://github.com/{repo}/pull/7"}

    def add_labels(self, repo: str, number: int, labels: list[str]) -> None:
        self._log("add_labels", repo, number, tuple(labels))

    def remove_label(self, repo: str, number: int, label: str) -> None:
        self._log("remove_label", repo, number, label)

    def comment(self, repo: str, number: int, body: str) -> str:
        self._log("comment", repo, number, body)
        return f"https://github.com/{repo}/issues/{number}#c1"

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


def state(status: str, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "repo": "o/r",
        "issue_number": 3,
        "issue_title": "bulk_price is wrong @everyone",
        "outcome": {"status": status},
        "pr_body": "## Summary\n\nbody",
        "issue_comment": "Fixling finished.",
    }
    base.update(kw)
    return base


@pytest.fixture
def pub(cfg: Config, tmp_path: Path) -> tuple[GitHubPublisher, FakeGH, list[dict[str, Any]], RunStore, str]:
    db = Database(tmp_path / "t.sqlite")
    runs, events = RunStore(db), EventStore(db, tmp_path / "runs")
    gh = FakeGH()
    pushes: list[dict[str, Any]] = []

    def fake_push(**kw: Any) -> tuple[str, list[str]]:
        pushes.append(kw)
        return "fixling/issue-3-abcd1234", ["gitleaks was not run"]

    publisher = GitHubPublisher(cfg, gh, events, runs, push=fake_push)  # type: ignore[arg-type]
    run_id = runs.create_run(repo="o/r", issue_number=3) or ""
    return publisher, gh, pushes, runs, run_id


@pytest.mark.parametrize(
    ("status", "label", "prefix"),
    [
        ("verified_fix", "fixling:verified", "fixling: "),
        ("unverified_fix", "fixling:needs-review", "fixling: "),
        ("repro_only", "fixling:repro-only", "fixling (repro only): "),
    ],
)
def test_pr_outcomes_open_a_draft_pr_and_label_it(
    pub: tuple[GitHubPublisher, FakeGH, list[dict[str, Any]], RunStore, str], status: str, label: str, prefix: str
) -> None:
    publisher, gh, pushes, runs, run_id = pub
    url = publisher.publish(run_id, state(status), PATCH)
    assert url == "https://github.com/o/r/pull/7" and runs.get_run(run_id)["pr_url"] == url  # type: ignore[index]
    (push,) = pushes
    assert push["base_branch"] == "main" and push["patch"] == PATCH and push["issue_number"] == 3
    (create,) = [c for c in gh.calls if c[0] == "create_pr"]
    assert create[2:4] == ("fixling/issue-3-abcd1234", "main") and create[6] is True  # draft
    assert create[4].startswith(prefix) and "(#3)" in create[4] and create[5] == "## Summary\n\nbody"
    assert "@everyone" not in create[4] and "@​everyone" in create[4], (
        "model/issue-originated text is defanged in titles"
    )
    assert ("add_labels", "o/r", 7, (label,)) in gh.calls and ("add_labels", "o/r", 3, (label,)) in gh.calls
    assert ("remove_label", "o/r", 3, "fixling:running") in gh.calls
    comment = next(c for c in gh.calls if c[0] == "comment")
    assert "pull/7" in comment[3]


def test_events_record_the_push_pr_and_comment(
    pub: tuple[GitHubPublisher, FakeGH, list[dict[str, Any]], RunStore, str],
) -> None:
    publisher, _, _, _, run_id = pub
    publisher.publish(run_id, state("verified_fix"), PATCH)
    types = [e.type for e in publisher.events.list(run_id)]
    assert types == ["git", "pr_opened", "comment"]
    assert publisher.events.list(run_id)[0].payload["warnings"] == ["gitleaks was not run"]


@pytest.mark.parametrize(("status", "label"), [("needs_clarification", None), ("no_result", "fixling:failed")])
def test_non_pr_outcomes_only_comment(
    pub: tuple[GitHubPublisher, FakeGH, list[dict[str, Any]], RunStore, str], status: str, label: str | None
) -> None:
    publisher, gh, pushes, _, run_id = pub
    assert publisher.publish(run_id, state(status), "") is None
    assert pushes == [] and "create_pr" not in gh.names() and "comment" in gh.names()
    assert ("add_labels", "o/r", 3, (label,)) in gh.calls if label else "add_labels" not in gh.names()


def test_a_local_run_without_an_issue_number_publishes_a_pr_but_no_issue_chatter(
    pub: tuple[GitHubPublisher, FakeGH, list[dict[str, Any]], RunStore, str],
) -> None:
    publisher, gh, _, _, run_id = pub
    publisher.publish(run_id, state("verified_fix", issue_number=None), PATCH)
    assert "create_pr" in gh.names() and "comment" not in gh.names() and "remove_label" not in gh.names()


def test_push_failure_propagates_so_finalize_can_record_it(
    pub: tuple[GitHubPublisher, FakeGH, list[dict[str, Any]], RunStore, str],
) -> None:
    publisher, gh, _, runs, run_id = pub

    def boom(**kw: Any) -> tuple[str, list[str]]:
        raise GitHubError("the policy gate blocked the push")

    publisher._push = boom
    with pytest.raises(GitHubError, match="policy gate"):
        publisher.publish(run_id, state("verified_fix"), PATCH)
    assert "create_pr" not in gh.names() and runs.get_run(run_id)["pr_url"] is None  # type: ignore[index]


# ---------------------------------------------------------------- worker
class FakeSandboxes:
    def __init__(self) -> None:
        self.closed: list[str] = []

    def close_run(self, run_id: str) -> int:
        self.closed.append(run_id)
        return 0


class FakeDeps:
    def __init__(self) -> None:
        self.sandboxes = FakeSandboxes()


class FakePipeline:
    def __init__(self, services: Any) -> None:
        self.services = services
        self.deps = FakeDeps()
        self.started: list[dict[str, Any]] = []
        self.recovered: list[str] = []
        self.checkpoints: set[str] = set()
        self.start_error: Exception | None = None

    def start(self, init_state: dict[str, Any], *, interactive: bool = False) -> dict[str, Any]:
        if self.start_error:
            raise self.start_error
        self.started.append({**init_state, "interactive": interactive})
        self.services.runs.update_run(init_state["run_id"], status="done", outcome="verified_fix")
        return {}

    def state(self, run_id: str) -> dict[str, Any]:
        return {"x": 1} if run_id in self.checkpoints else {}

    def recover(self, run_id: str) -> dict[str, Any]:
        self.recovered.append(run_id)
        self.services.runs.update_run(run_id, status="done")
        return {}


@pytest.fixture
def wk(cfg: Config, tiny_repo: Path) -> tuple[Worker, FakePipeline, FakeGH, Any]:
    services = build_services(cfg)
    gh = FakeGH()
    pipeline = FakePipeline(services)
    worker = Worker(services, pipeline, gh=gh, poll_interval_s=0.05)  # type: ignore[arg-type]
    return worker, pipeline, gh, services


def queue_local(services: Any, cfg: Config, tiny_repo: Path, **extra: Any) -> str:
    run_id = services.runs.create_run(repo="tiny", issue_number=3, status="queued") or ""
    d = cfg.run_dir(run_id)
    d.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "repo": "tiny",
        "number": 3,
        "title": "T",
        "body": "B",
        "author": "alice",
        "local_path": str(tiny_repo),
        **extra,
    }
    (d / "issue.json").write_text(json.dumps(snapshot), encoding="utf-8")
    return run_id


def test_process_builds_the_initial_state_from_the_issue_snapshot(
    wk: tuple[Worker, FakePipeline, FakeGH, Any], cfg: Config, tiny_repo: Path
) -> None:
    worker, pipeline, _, services = wk
    run_id = queue_local(services, cfg, tiny_repo, interactive=True)
    worker.process(run_id)
    (call,) = pipeline.started
    assert (call["run_id"], call["repo"], call["issue_number"]) == (run_id, "tiny", 3)
    assert (call["issue_title"], call["issue_body"], call["issue_author"], call["interactive"]) == (
        "T",
        "B",
        "alice",
        True,
    )
    assert (Path(call["src_dir"]) / "src" / "tiny" / "pricing.py").exists() and not (
        Path(call["src_dir"]) / ".git"
    ).exists()
    assert services.runs.get_run(run_id)["status"] == "done" and worker.current is None  # type: ignore[index]


def test_a_crash_marks_the_run_failed_cleans_up_and_tells_the_issue(
    wk: tuple[Worker, FakePipeline, FakeGH, Any], cfg: Config, tiny_repo: Path
) -> None:
    worker, pipeline, gh, services = wk
    run_id = queue_local(services, cfg, tiny_repo)
    pipeline.start_error = RuntimeError("disk full ghp_" + "Z" * 36)
    worker.process(run_id)
    row = services.runs.get_run(run_id)
    assert (row["status"], row["outcome"], row["confidence"]) == ("failed", "no_result", "none")
    err = [e for e in services.events.list(run_id) if e.type == "error"]
    assert err and err[0].payload["kind"] == "crash" and "ghp_" + "Z" * 36 not in err[0].model_dump_json()  # redacted
    assert pipeline.deps.sandboxes.closed == [run_id]
    assert ("remove_label", "tiny", 3, "fixling:running") in gh.calls and (
        "add_labels",
        "tiny",
        3,
        ("fixling:failed",),
    ) in gh.calls
    assert "internal error" in next(c for c in gh.calls if c[0] == "comment")[3]


def test_crash_handling_survives_github_being_down(
    wk: tuple[Worker, FakePipeline, FakeGH, Any], cfg: Config, tiny_repo: Path
) -> None:
    worker, pipeline, gh, services = wk
    gh.fail_on = {"remove_label", "add_labels", "comment"}
    run_id = queue_local(services, cfg, tiny_repo)
    pipeline.start_error = RuntimeError("boom")
    worker.process(run_id)
    assert services.runs.get_run(run_id)["status"] == "failed"  # type: ignore[index]


def test_unfetchable_repo_is_a_failed_run_not_a_dead_worker(
    wk: tuple[Worker, FakePipeline, FakeGH, Any], cfg: Config, tmp_path: Path
) -> None:
    worker, _, _, services = wk
    run_id = services.runs.create_run(repo="o/r", issue_number=1, status="queued") or ""
    cfg.run_dir(run_id).mkdir(parents=True, exist_ok=True)
    (cfg.run_dir(run_id) / "issue.json").write_text(
        json.dumps({"repo": "o/r", "number": 1, "local_path": str(tmp_path / "gone")}), encoding="utf-8"
    )
    worker.process(run_id)
    assert services.runs.get_run(run_id)["status"] == "failed"  # type: ignore[index]


def test_recovery_resumes_runs_with_a_checkpoint_and_fails_the_rest(
    wk: tuple[Worker, FakePipeline, FakeGH, Any],
) -> None:
    worker, pipeline, _, services = wk
    with_cp = services.runs.create_run(repo="tiny", issue_number=1, status="running") or ""
    without = services.runs.create_run(repo="tiny", issue_number=2, status="running") or ""
    pipeline.checkpoints.add(with_cp)
    assert worker.recover_running() == [with_cp]
    assert pipeline.recovered == [with_cp]
    assert services.runs.get_run(without)["status"] == "failed"  # type: ignore[index]
    error = next(e for e in services.events.list(without) if e.type == "error")
    assert "no checkpoint" in error.payload["error"]


def test_the_loop_processes_queued_runs_in_fifo_order_one_at_a_time(
    wk: tuple[Worker, FakePipeline, FakeGH, Any], cfg: Config, tiny_repo: Path
) -> None:
    worker, pipeline, _, services = wk
    first = queue_local(services, cfg, tiny_repo)
    second = queue_local(services, cfg, tiny_repo)
    worker.start()
    deadline = time.time() + 30
    while len(pipeline.started) < 2 and time.time() < deadline:
        time.sleep(0.05)
    worker.stop()
    worker.join(timeout=5)
    assert [s["run_id"] for s in pipeline.started] == [first, second]
