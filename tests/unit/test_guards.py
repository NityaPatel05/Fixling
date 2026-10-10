from __future__ import annotations

from pathlib import Path

import pytest

from fixling.config import BudgetsCfg
from fixling.db import Database
from fixling.errors import BudgetExceeded
from fixling.events.store import EventStore
from fixling.graph.guards import BudgetTracker, CancelRegistry, StuckDetector
from fixling.runs import RunStore


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def env(tmp_path: Path) -> tuple[BudgetTracker, RunStore, EventStore, Clock, str]:
    db = Database(tmp_path / "t.sqlite")
    runs, events, clock = RunStore(db), EventStore(db, tmp_path / "runs"), Clock()
    rid = runs.create_run(repo="o/r") or ""
    tracker = BudgetTracker(BudgetsCfg(run_wall_clock_s=100, run_max_llm_tokens=1000), runs, events, clock=clock)
    tracker.start(rid)
    return tracker, runs, events, clock, rid


def test_within_budget_passes(env: tuple[BudgetTracker, RunStore, EventStore, Clock, str]) -> None:
    tracker, _, events, _, rid = env
    tracker.check(rid)
    assert events.list(rid) == []


def test_wall_clock_exceeded(env: tuple[BudgetTracker, RunStore, EventStore, Clock, str]) -> None:
    tracker, _, _, clock, rid = env
    clock.t += 101
    with pytest.raises(BudgetExceeded, match="wall-clock"):
        tracker.check(rid)


def test_token_budget_exceeded(env: tuple[BudgetTracker, RunStore, EventStore, Clock, str]) -> None:
    tracker, runs, _, _, rid = env
    runs.add_usage(rid, 600, 401, 10)
    with pytest.raises(BudgetExceeded, match="token"):
        tracker.check(rid)


def test_soft_warning_is_emitted_once_at_75_percent(
    env: tuple[BudgetTracker, RunStore, EventStore, Clock, str],
) -> None:
    tracker, _, events, clock, rid = env
    clock.t += 80
    tracker.check(rid)
    tracker.check(rid)
    warnings = [e for e in events.list(rid) if e.type == "budget"]
    assert len(warnings) == 1 and "75%" in warnings[0].payload["warning"]


def test_cancel_stops_the_run(env: tuple[BudgetTracker, RunStore, EventStore, Clock, str]) -> None:
    tracker, _, _, _, rid = env
    tracker.cancel.request(rid)
    with pytest.raises(BudgetExceeded, match="cancelled"):
        tracker.check(rid)
    tracker.cancel.clear(rid)
    tracker.check(rid)


def test_restart_resets_the_wall_clock(env: tuple[BudgetTracker, RunStore, EventStore, Clock, str]) -> None:
    tracker, _, _, clock, rid = env
    clock.t += 101
    tracker.start(rid)
    tracker.check(rid)


def test_cancel_registry_is_per_run() -> None:
    c = CancelRegistry()
    c.request("a")
    assert c.is_set("a") and not c.is_set("b")


# ---------------------------------------------------------------- stuck detection
def test_same_action_and_observation_four_times() -> None:
    d = StuckDetector()
    sigs = [d.record("ls", "a.py") for _ in range(4)]
    assert sigs[:3] == [None] * 3 and sigs[3] is not None and "x4" in sigs[3].rule and sigs[3].action == "nudge"


def test_numbers_and_addresses_are_normalised() -> None:
    d = StuckDetector()
    sigs = [d.record("pytest -q", f"3 failed in {i}.01s at 0x7f{i}a") for i in range(4)]
    assert sigs[3] is not None


def test_same_failing_action_three_times() -> None:
    d = StuckDetector()
    sigs = [d.record("python x.py", f"Traceback {i}", is_error=True) for i in range(3)]
    assert sigs[2] is not None and "failing x3" in sigs[2].rule


def test_alternating_actions_for_six_cycles() -> None:
    d = StuckDetector()
    sig = None
    for i in range(12):
        sig = d.record("edit" if i % 2 == 0 else "test", f"result variant {'abcdefghijkl'[i]}") or sig
    assert sig is not None and "alternating" in sig.rule


def test_three_messages_without_a_tool_call() -> None:
    d = StuckDetector()
    assert d.record_no_tool() is None and d.record_no_tool() is None
    sig = d.record_no_tool()
    assert sig is not None and "without a tool call" in sig.rule


def test_progress_resets_the_no_tool_counter() -> None:
    d = StuckDetector()
    d.record_no_tool()
    d.record_no_tool()
    d.record("ls", "ok")
    assert d.record_no_tool() is None


def test_unchanged_failing_tests_across_three_runs() -> None:
    d = StuckDetector()
    assert d.record_test_run(["t1", "t2"]) is None and d.record_test_run(["t2", "t1"]) is None
    sig = d.record_test_run(["t1", "t2"])
    assert sig is not None and "unchanged" in sig.rule
    d2 = StuckDetector()
    assert [d2.record_test_run(t) for t in (["a"], ["b"], ["c"])] == [None] * 3
    assert StuckDetector().record_test_run([]) is None


def test_escalation_ladder_nudge_restate_stop() -> None:
    d = StuckDetector()
    actions = []
    for _ in range(3):
        sigs = [d.record("ls", "same") for _ in range(4)]
        actions.append(sigs[-1].action if sigs[-1] else None)
    assert actions == ["nudge", "restate", "stop"]
