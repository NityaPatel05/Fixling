"""Expired-container cleanup (spec E5): a safety net so a crash can never leak sandboxes."""

from __future__ import annotations

import contextlib
import threading
import time

import docker
from docker.errors import APIError, NotFound

from fixling.runs import RunStore

LABEL = "fixling.run"


def reap_once(client: docker.DockerClient, runs: RunStore | None = None, *, now: float | None = None) -> list[str]:
    """Force-remove sandbox containers whose ``fixling.expires`` label is in the past."""
    now = time.time() if now is None else now
    removed: list[str] = []
    for ct in client.containers.list(all=True, filters={"label": LABEL}):
        try:
            expires = int((ct.labels or {}).get("fixling.expires", "0"))
        except ValueError:
            expires = 0
        if expires < now:
            try:
                ct.remove(force=True)
            except NotFound:
                pass
            except APIError:
                continue
            removed.append(ct.id or "")
            if runs and ct.id:
                runs.mark_sandbox_removed(ct.id)
    return removed


def reconcile(
    client: docker.DockerClient, runs: RunStore, *, active_runs: set[str] | None = None
) -> dict[str, list[str]]:
    """Startup: align the DB with ``docker ps -a``.

    Rows whose container is gone are marked removed; labeled containers that belong to no active run are removed.
    """
    active = active_runs or set()
    live = {c.id: c for c in client.containers.list(all=True, filters={"label": LABEL})}
    gone: list[str] = []
    for row in runs.live_sandboxes():
        if row["container_id"] not in live:
            runs.mark_sandbox_removed(row["container_id"])
            gone.append(row["container_id"])
    orphans: list[str] = []
    for cid, ct in live.items():
        if (ct.labels or {}).get(LABEL) in active:
            continue
        try:
            ct.remove(force=True)
        except (NotFound, APIError):
            continue
        runs.mark_sandbox_removed(cid)
        orphans.append(cid)
    return {"marked_removed": gone, "orphans_removed": orphans}


class Reaper(threading.Thread):
    def __init__(self, client: docker.DockerClient, runs: RunStore, interval_s: float = 60.0) -> None:
        super().__init__(name="fixling-reaper", daemon=True)
        self.client, self.runs, self.interval_s = client, runs, interval_s
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.wait(self.interval_s):
            with contextlib.suppress(Exception):  # the reaper must never die
                reap_once(self.client, self.runs)

    def stop(self) -> None:
        self._stop_event.set()
