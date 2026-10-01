"""Run summary rows, sandbox registry and embedding cache (the mutable tables next to the event log)."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import numpy as np
from ulid import ULID

from fixling.db import Database

RUN_FIELDS = {
    "repo",
    "issue_number",
    "issue_url",
    "status",
    "outcome",
    "confidence",
    "pr_url",
    "model",
    "started_at",
    "finished_at",
    "base_sha",
}
RUN_STATUSES = ("queued", "running", "interrupted", "done", "failed", "cancelled")


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class RunStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ runs
    def create_run(
        self,
        *,
        repo: str,
        issue_number: int | None = None,
        issue_url: str | None = None,
        trigger_key: str | None = None,
        model: str | None = None,
        status: str = "queued",
    ) -> str | None:
        """Insert a run. Returns the new ``run_id`` or ``None`` if ``trigger_key`` was already seen."""
        run_id = str(ULID()).lower()
        with self.db.transaction() as conn:
            if trigger_key is not None:
                dup = conn.execute("SELECT 1 FROM runs WHERE trigger_key=?", (trigger_key,)).fetchone()
                if dup:
                    return None
            conn.execute(
                "INSERT INTO runs(run_id, repo, issue_number, issue_url, trigger_key, status, model, started_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (run_id, repo, issue_number, issue_url, trigger_key, status, model, now_iso()),
            )
        return run_id

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        r = self.db.query_one("SELECT * FROM runs WHERE run_id=?", (run_id,))
        return dict(r) if r else None

    def list_runs(
        self, *, status: str | None = None, repo: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        where, params = [], []
        if status:
            where.append("status=?")
            params.append(status)
        if repo:
            where.append("repo=?")
            params.append(repo)
        sql = "SELECT * FROM runs" + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY run_id DESC LIMIT ?"
        return [dict(r) for r in self.db.query(sql, [*params, limit])]

    def next_queued(self) -> dict[str, Any] | None:
        r = self.db.query_one("SELECT * FROM runs WHERE status='queued' ORDER BY run_id ASC LIMIT 1")
        return dict(r) if r else None

    def update_run(self, run_id: str, **fields: Any) -> None:
        bad = set(fields) - RUN_FIELDS
        if bad:
            raise ValueError(f"unknown run fields: {sorted(bad)}")
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE runs SET {cols} WHERE run_id=?", [*fields.values(), run_id])

    def add_usage(self, run_id: str, tokens_in: int, tokens_out: int, llm_ms: int) -> None:
        self.db.execute(
            "UPDATE runs SET tokens_in=tokens_in+?, tokens_out=tokens_out+?, llm_ms=llm_ms+? WHERE run_id=?",
            (tokens_in, tokens_out, llm_ms, run_id),
        )

    def usage(self, run_id: str) -> tuple[int, int, int]:
        r = self.db.query_one("SELECT tokens_in, tokens_out, llm_ms FROM runs WHERE run_id=?", (run_id,))
        return (r["tokens_in"], r["tokens_out"], r["llm_ms"]) if r else (0, 0, 0)

    # ------------------------------------------------------------------ sandboxes
    def register_sandbox(self, container_id: str, run_id: str, role: str, ttl_s: int = 1800) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO sandboxes(container_id, run_id, role, created_at, expires_at, removed)"
            " VALUES(?,?,?,?,?,0)",
            (container_id, run_id, role, now_iso(), int(time.time()) + ttl_s),
        )

    def mark_sandbox_removed(self, container_id: str) -> None:
        self.db.execute("UPDATE sandboxes SET removed=1 WHERE container_id=?", (container_id,))

    def live_sandboxes(self, run_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM sandboxes WHERE removed=0"
        params: list[Any] = []
        if run_id:
            sql += " AND run_id=?"
            params.append(run_id)
        return [dict(r) for r in self.db.query(sql, params)]

    # ------------------------------------------------------------------ embedding cache
    def get_embeddings(self, repo_key: str, hashes: list[str]) -> dict[str, np.ndarray]:
        out: dict[str, np.ndarray] = {}
        for i in range(0, len(hashes), 500):
            part = hashes[i : i + 500]
            marks = ",".join("?" * len(part))
            rows = self.db.query(
                f"SELECT chunk_hash, vector FROM embed_cache WHERE repo_key=? AND chunk_hash IN ({marks})",
                [repo_key, *part],
            )
            for r in rows:
                out[r["chunk_hash"]] = np.frombuffer(r["vector"], dtype=np.float32)
        return out

    def put_embeddings(self, repo_key: str, items: dict[str, np.ndarray]) -> None:
        with self.db.transaction() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO embed_cache(repo_key, chunk_hash, vector) VALUES(?,?,?)",
                [(repo_key, h, np.asarray(v, dtype=np.float32).tobytes()) for h, v in items.items()],
            )
