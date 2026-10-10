"""Run budgets, cancellation and loop detection (spec E16)."""

from __future__ import annotations

import hashlib
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from fixling.config import BudgetsCfg
from fixling.errors import BudgetExceeded
from fixling.events.store import EventStore
from fixling.runs import RunStore

SOFT_WARNING = 0.75


class CancelRegistry:
    """Thread-safe set of run ids the user asked to stop (checked by every guard)."""

    def __init__(self) -> None:
        self._ids: set[str] = set()
        self._lock = threading.Lock()

    def request(self, run_id: str) -> None:
        with self._lock:
            self._ids.add(run_id)

    def is_set(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._ids

    def clear(self, run_id: str) -> None:
        with self._lock:
            self._ids.discard(run_id)


class BudgetTracker:
    """Wall clock + token budgets. The wall clock counts from the last ``start`` (i.e. per process segment)."""

    def __init__(
        self,
        budgets: BudgetsCfg,
        runs: RunStore,
        events: EventStore,
        cancel: CancelRegistry | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.budgets, self.runs, self.events = budgets, runs, events
        self.cancel = cancel or CancelRegistry()
        self._clock = clock
        self._start: dict[str, float] = {}
        self._warned: set[str] = set()
        self._lock = threading.Lock()

    def start(self, run_id: str) -> None:
        with self._lock:
            self._start[run_id] = self._clock()
            self._warned.discard(run_id)

    def elapsed(self, run_id: str) -> float:
        with self._lock:
            return self._clock() - self._start.setdefault(run_id, self._clock())

    def tokens(self, run_id: str) -> int:
        t_in, t_out, _ = self.runs.usage(run_id)
        return t_in + t_out

    def check(self, run_id: str) -> None:
        """Raise ``BudgetExceeded`` if the run was cancelled or has used up a budget; warn once at 75%."""
        if self.cancel.is_set(run_id):
            raise BudgetExceeded("cancelled by user")
        elapsed, tokens = self.elapsed(run_id), self.tokens(run_id)
        if elapsed > self.budgets.run_wall_clock_s:
            raise BudgetExceeded(f"wall-clock budget exceeded ({int(elapsed)}s > {self.budgets.run_wall_clock_s}s)")
        if tokens > self.budgets.run_max_llm_tokens:
            raise BudgetExceeded(f"token budget exceeded ({tokens} > {self.budgets.run_max_llm_tokens})")
        near = (
            elapsed > SOFT_WARNING * self.budgets.run_wall_clock_s
            or tokens > SOFT_WARNING * self.budgets.run_max_llm_tokens
        )
        with self._lock:
            first = near and run_id not in self._warned
            if first:
                self._warned.add(run_id)
        if first:
            self.events.append(
                run_id,
                "budget",
                "system",
                {"warning": "75% of a run budget used", "elapsed_s": int(elapsed), "tokens": tokens},
            )


# ---------------------------------------------------------------- stuck detection (fallback loop only)
_NUMBERS = re.compile(r"0x[0-9a-fA-F]+|\d+")


def _norm_hash(text: str) -> str:
    return hashlib.sha1(_NUMBERS.sub("N", text).strip().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class StuckSignal:
    rule: str
    action: str  # nudge -> restate -> stop


class StuckDetector:
    """Detects an agent looping over a window of recent steps (used by the optional bash-loop fallback).

    Rules: the same action and observation x4; the same action ending in an error x3; two actions alternating for
    6 cycles; 3 consecutive messages without a tool call; an unchanged failing-test set across 3 test runs.
    Responses escalate: nudge, restate the task, stop.
    """

    LADDER = ("nudge", "restate", "stop")

    def __init__(self, window: int = 20) -> None:
        self._steps: deque[tuple[str, str, bool]] = deque(maxlen=window)
        self._no_tool = 0
        self._test_hashes: deque[str] = deque(maxlen=3)
        self._escalations = 0

    def _signal(self, rule: str) -> StuckSignal:
        action = self.LADDER[min(self._escalations, len(self.LADDER) - 1)]
        self._escalations += 1
        self._steps.clear()
        self._no_tool = 0
        self._test_hashes.clear()
        return StuckSignal(rule, action)

    def record(self, action: str, observation: str, *, is_error: bool = False) -> StuckSignal | None:
        self._no_tool = 0
        a, o = _norm_hash(action), _norm_hash(observation)
        self._steps.append((a, o, is_error))
        steps = list(self._steps)
        if len(steps) >= 4 and len({(s[0], s[1]) for s in steps[-4:]}) == 1:
            return self._signal("same action and observation x4")
        if len(steps) >= 3 and all(s[2] and s[0] == steps[-1][0] for s in steps[-3:]):
            return self._signal("same action failing x3")
        if len(steps) >= 12:
            acts = [s[0] for s in steps[-12:]]
            if len(set(acts)) == 2 and all(acts[i] != acts[i + 1] for i in range(11)):
                return self._signal("alternating between two actions")
        return None

    def record_no_tool(self) -> StuckSignal | None:
        self._no_tool += 1
        if self._no_tool >= 3:
            return self._signal("3 consecutive messages without a tool call")
        return None

    def record_test_run(self, failing_tests: list[str]) -> StuckSignal | None:
        self._test_hashes.append(_norm_hash("\n".join(sorted(failing_tests))))
        if len(self._test_hashes) == 3 and len(set(self._test_hashes)) == 1 and failing_tests:
            return self._signal("failing test set unchanged across 3 runs")
        return None
