from __future__ import annotations

import asyncio
import sqlite3
import threading
from pathlib import Path

import numpy as np
import pytest

from fixling.db import Database
from fixling.events.store import EventStore, truncate_middle
from fixling.runs import RunStore

TOKEN = "ghp_" + "a" * 36
FINE = "github_pat_" + "B" * 40


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "t.sqlite")


@pytest.fixture
def store(db: Database, tmp_path: Path) -> EventStore:
    return EventStore(db, tmp_path / "runs", observation_chars=100, secrets=["CANARY_xyz"])


def test_append_assigns_increasing_seq_per_run(store: EventStore) -> None:
    assert store.append("r1", "run_started", "system") == 1
    assert store.append("r1", "spec", "llm", {"a": 1}) == 2
    assert store.append("r2", "run_started", "system") == 1
    assert [e.seq for e in store.list("r1")] == [1, 2]
    assert store.list("r1", after_seq=1)[0].type == "spec"
    assert store.get("r1", 2).payload == {"a": 1}  # type: ignore[union-attr]
    assert store.last_seq("r1") == 2


def test_events_are_append_only_in_the_database(store: EventStore, db: Database) -> None:
    store.append("r1", "run_started", "system")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("UPDATE events SET type='error' WHERE run_id='r1'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.execute("DELETE FROM events WHERE run_id='r1'")


def test_redaction_of_tokens_and_secrets_in_payload_and_artifact(store: EventStore) -> None:
    seq = store.append(
        "r1",
        "exec",
        "sandbox",
        {"cmd": f"echo {TOKEN}", "nested": {"k": [FINE, "CANARY_xyz"]}},
        artifact_text="CANARY_xyz",
    )
    ev = store.get("r1", seq)
    assert ev is not None
    blob = ev.model_dump_json()
    for secret in (TOKEN, FINE, "CANARY_xyz"):
        assert secret not in blob
    assert "[REDACTED]" in blob


def test_long_artifact_is_spilled_to_disk_and_truncated_inline(store: EventStore, tmp_path: Path) -> None:
    text = "A" * 500 + "MIDDLE" + "Z" * 500
    seq = store.append("r1", "exec", "sandbox", artifact_text=text)
    ev = store.get("r1", seq)
    assert ev is not None and ev.artifact == f"artifacts/{seq}.txt"
    assert ev.payload["truncated"] is True
    assert len(ev.payload["text"]) < len(text)
    assert store.read_artifact("r1", seq) == text
    assert (tmp_path / "runs" / "r1" / "artifacts" / f"{seq}.txt").exists()


def test_short_artifact_stays_inline(store: EventStore) -> None:
    seq = store.append("r1", "exec", "sandbox", artifact_text="hello")
    ev = store.get("r1", seq)
    assert ev is not None and ev.artifact is None and ev.payload["text"] == "hello"
    assert store.read_artifact("r1", seq) == "hello"
    assert store.read_artifact("r1", 99) is None


def test_truncate_middle_keeps_head_and_tail() -> None:
    out = truncate_middle("abcdefghij" * 20, 40)
    assert out.startswith("abcdefghij") and out.endswith("abcdefghij") and "truncated" in out
    assert truncate_middle("short", 40) == "short"


def test_concurrent_appends_have_unique_contiguous_seqs(store: EventStore) -> None:
    def work() -> None:
        for _ in range(25):
            store.append("r1", "llm_call", "llm")

    threads = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert [e.seq for e in store.list("r1")] == list(range(1, 201))


def test_publishes_to_subscribers(store: EventStore) -> None:
    async def scenario() -> list[int]:
        sub = store.bus.subscribe("r1")
        other = store.bus.subscribe("r2")
        await asyncio.to_thread(store.append, "r1", "run_started", "system")
        await asyncio.to_thread(store.append, "r1", "spec", "llm")
        got = [(await asyncio.wait_for(sub.queue.get(), 2)).seq for _ in range(2)]
        assert other.queue.empty()
        store.bus.unsubscribe(sub)
        store.bus.unsubscribe(other)
        return got

    assert asyncio.run(scenario()) == [1, 2]


def test_migration_is_idempotent(tmp_path: Path) -> None:
    p = tmp_path / "x.sqlite"
    Database(p).close()
    d = Database(p)
    assert d.query_one("PRAGMA user_version")[0] == 1  # type: ignore[index]


# ---------------------------------------------------------------- RunStore
def test_run_lifecycle_and_trigger_dedupe(db: Database) -> None:
    rs = RunStore(db)
    rid = rs.create_run(repo="o/r", issue_number=3, trigger_key="o/r#3@1")
    assert rid is not None
    assert rs.create_run(repo="o/r", issue_number=3, trigger_key="o/r#3@1") is None
    assert rs.next_queued()["run_id"] == rid  # type: ignore[index]
    rs.update_run(rid, status="running", base_sha="abc")
    assert rs.next_queued() is None
    rs.add_usage(rid, 10, 5, 100)
    rs.add_usage(rid, 1, 1, 1)
    assert rs.usage(rid) == (11, 6, 101)
    assert rs.get_run(rid)["status"] == "running"  # type: ignore[index]
    assert [r["run_id"] for r in rs.list_runs(status="running")] == [rid]
    with pytest.raises(ValueError):
        rs.update_run(rid, trigger_key="x")


def test_sandbox_registry_and_embed_cache(db: Database) -> None:
    rs = RunStore(db)
    rs.register_sandbox("c1", "r1", "work")
    rs.register_sandbox("c2", "r1", "verify")
    rs.mark_sandbox_removed("c1")
    assert [s["container_id"] for s in rs.live_sandboxes("r1")] == ["c2"]
    vecs = {"h1": np.array([1, 2, 3], dtype=np.float32), "h2": np.array([4, 5, 6], dtype=np.float32)}
    rs.put_embeddings("repo", vecs)
    got = rs.get_embeddings("repo", ["h1", "h2", "h3"])
    assert set(got) == {"h1", "h2"} and np.allclose(got["h2"], vecs["h2"])
