from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fixling.config import Config
from fixling.db import Database
from fixling.errors import GitHubError
from fixling.gh.poller import Poller
from fixling.runs import RunStore


class FakeGitHub:
    """Just the poller-facing surface of ``GitHubClient``, with scripted data and a call log."""

    def __init__(self) -> None:
        self.issues: dict[str, list[dict[str, Any]]] = {}
        self.events: dict[tuple[str, int], dict[str, Any] | None] = {}
        self.roles: dict[str, str] = {}
        self.calls: list[tuple[str, ...]] = []
        self.list_error: GitHubError | None = None
        self.label_error: GitHubError | None = None

    def list_labeled_issues(self, repo: str, label: str) -> list[dict[str, Any]]:
        if self.list_error:
            raise self.list_error
        return self.issues.get(repo, [])

    def latest_label_event(self, repo: str, number: int, label: str) -> dict[str, Any] | None:
        return self.events.get((repo, number))

    def get_permission(self, repo: str, user: str) -> str:
        self.calls.append(("permission", repo, user))
        return self.roles.get(user, "none")

    def add_labels(self, repo: str, number: int, labels: list[str]) -> None:
        if self.label_error:
            raise self.label_error
        self.calls.append(("add", repo, str(number), *labels))

    def remove_label(self, repo: str, number: int, label: str) -> None:
        self.calls.append(("remove", repo, str(number), label))


def issue(n: int = 3, **kw: Any) -> dict[str, Any]:
    base = {
        "number": n,
        "title": f"issue {n}",
        "body": "it is broken",
        "user": {"login": "reporter"},
        "html_url": f"https://github.com/o/r/issues/{n}",
    }
    base.update(kw)
    return base


def label_event(eid: int, actor: str = "maintainer") -> dict[str, Any]:
    return {"id": eid, "event": "labeled", "label": {"name": "fixling"}, "actor": {"login": actor}}


@pytest.fixture
def setup(cfg: Config, tmp_path: Path) -> tuple[Poller, FakeGitHub, RunStore]:
    cfg.settings.fixling_repos = "o/r"
    gh = FakeGitHub()
    gh.roles = {"maintainer": "maintain", "writer": "write", "reader": "read", "owner": "admin"}
    runs = RunStore(Database(tmp_path / "t.sqlite"))
    return Poller(cfg, gh, runs), gh, runs  # type: ignore[arg-type]


def test_a_write_access_trigger_queues_a_run_and_swaps_labels(
    setup: tuple[Poller, FakeGitHub, RunStore], cfg: Config
) -> None:
    poller, gh, runs = setup
    gh.issues["o/r"] = [issue(3)]
    gh.events[("o/r", 3)] = label_event(100)
    (run_id,) = poller.poll_once()
    row = runs.get_run(run_id)
    assert (
        row is not None and row["status"] == "queued" and row["trigger_key"] == "o/r#3@100" and row["issue_number"] == 3
    )
    assert ("add", "o/r", "3", "fixling:running") in gh.calls and ("remove", "o/r", "3", "fixling") in gh.calls
    snap = json.loads((cfg.run_dir(run_id) / "issue.json").read_text(encoding="utf-8"))
    assert snap["title"] == "issue 3" and snap["author"] == "reporter" and snap["label_actor"] == "maintainer"


@pytest.mark.parametrize("role", ["maintain", "write", "admin"])
def test_every_write_role_is_trusted(setup: tuple[Poller, FakeGitHub, RunStore], role: str) -> None:
    poller, gh, _ = setup
    gh.roles["someone"] = role
    gh.issues["o/r"] = [issue(5)]
    gh.events[("o/r", 5)] = label_event(7, "someone")
    assert len(poller.poll_once()) == 1


@pytest.mark.parametrize("actor", ["reader", "stranger"])
def test_a_label_from_someone_without_write_access_is_ignored(
    setup: tuple[Poller, FakeGitHub, RunStore], actor: str
) -> None:
    poller, gh, runs = setup
    gh.issues["o/r"] = [issue(3)]
    gh.events[("o/r", 3)] = label_event(100, actor)
    assert poller.poll_once() == [] and runs.list_runs() == []
    assert not [c for c in gh.calls if c[0] in ("add", "remove")]  # no labels touched either
    assert poller.rejections[0]["actor"] == actor and poller.rejections[0]["issue"] == 3


def test_the_same_label_event_never_starts_two_runs(setup: tuple[Poller, FakeGitHub, RunStore]) -> None:
    poller, gh, runs = setup
    gh.issues["o/r"] = [issue(3)]
    gh.events[("o/r", 3)] = label_event(100)
    assert len(poller.poll_once()) == 1 and poller.poll_once() == []
    fresh = Poller(poller.cfg, gh, runs)  # type: ignore[arg-type]  # a restarted process has no in-memory memory
    assert fresh.poll_once() == [] and len(runs.list_runs()) == 1  # the UNIQUE trigger_key still protects


def test_relabelling_creates_a_new_trigger_and_a_new_run(setup: tuple[Poller, FakeGitHub, RunStore]) -> None:
    poller, gh, runs = setup
    gh.issues["o/r"] = [issue(3)]
    gh.events[("o/r", 3)] = label_event(100)
    poller.poll_once()
    gh.events[("o/r", 3)] = label_event(200)
    assert len(poller.poll_once()) == 1 and len(runs.list_runs()) == 2


def test_issues_without_a_label_event_are_skipped(setup: tuple[Poller, FakeGitHub, RunStore]) -> None:
    poller, gh, _ = setup
    gh.issues["o/r"] = [issue(3)]
    assert poller.poll_once() == []


def test_a_github_outage_skips_the_pass_quietly(setup: tuple[Poller, FakeGitHub, RunStore]) -> None:
    poller, gh, _ = setup
    gh.list_error = GitHubError("rate limited")
    assert poller.poll_once() == []


def test_label_swap_failure_does_not_lose_the_run(setup: tuple[Poller, FakeGitHub, RunStore]) -> None:
    poller, gh, runs = setup
    gh.issues["o/r"] = [issue(3)]
    gh.events[("o/r", 3)] = label_event(100)
    gh.label_error = GitHubError("forbidden", status=403)
    assert len(poller.poll_once()) == 1 and len(runs.list_runs()) == 1


def test_no_repos_means_nothing_to_poll(setup: tuple[Poller, FakeGitHub, RunStore], cfg: Config) -> None:
    poller, gh, _ = setup
    cfg.settings.fixling_repos = ""
    gh.issues["o/r"] = [issue(3)]
    assert poller.poll_once() == []
