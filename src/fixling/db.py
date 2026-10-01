"""SQLite access (WAL) with a tiny forward-only migration list (spec E1)."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

MIGRATIONS: list[str] = [
    """
    CREATE TABLE runs(
      run_id TEXT PRIMARY KEY, repo TEXT, issue_number INT, issue_url TEXT,
      trigger_key TEXT UNIQUE, status TEXT, outcome TEXT, confidence TEXT, pr_url TEXT,
      model TEXT, tokens_in INT DEFAULT 0, tokens_out INT DEFAULT 0, llm_ms INT DEFAULT 0,
      started_at TEXT, finished_at TEXT, base_sha TEXT);
    CREATE TABLE events(
      run_id TEXT, seq INT, ts TEXT, type TEXT, source TEXT, node TEXT,
      cause INT, payload TEXT, artifact TEXT, PRIMARY KEY(run_id, seq));
    CREATE TABLE sandboxes(
      container_id TEXT PRIMARY KEY, run_id TEXT, role TEXT, created_at TEXT,
      expires_at INT, removed INT DEFAULT 0);
    CREATE TABLE embed_cache(
      repo_key TEXT, chunk_hash TEXT, vector BLOB, PRIMARY KEY(repo_key, chunk_hash));
    CREATE INDEX idx_runs_status ON runs(status);
    CREATE INDEX idx_sandboxes_run ON sandboxes(run_id);
    CREATE TRIGGER events_no_update BEFORE UPDATE ON events
      BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
    CREATE TRIGGER events_no_delete BEFORE DELETE ON events
      BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
    """,
]


class Database:
    """One shared connection guarded by a re-entrant lock (runs are single-writer, reads are cheap)."""

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._migrate()

    def _migrate(self) -> None:
        # The event log is append-only: the triggers above enforce it in the database, not just by convention.
        with self._lock:
            version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
            for i, script in enumerate(MIGRATIONS[version:], start=version + 1):
                self._conn.executescript(f"BEGIN;{script};PRAGMA user_version={i};COMMIT;")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """``BEGIN IMMEDIATE`` ... ``COMMIT`` under the lock; rolls back on error."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)

    def query(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            row: sqlite3.Row | None = self._conn.execute(sql, params).fetchone()
            return row

    def close(self) -> None:
        with self._lock:
            self._conn.close()
