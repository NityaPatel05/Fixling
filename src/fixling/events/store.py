"""Append-only event store with redaction and artifact spill-over (spec E1)."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fixling.db import Database
from fixling.events.stream import EventBus
from fixling.models import Event

# GitHub token families (classic PAT, fine-grained PAT, OAuth/app/refresh tokens).
_TOKEN_PATTERNS = [
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
]
REDACTED = "[REDACTED]"


def truncate_middle(text: str, limit: int) -> str:
    """Keep the head and tail of ``text`` (``limit`` chars total) with a marker in the middle."""
    if len(text) <= limit:
        return text
    half = limit // 2
    omitted = len(text) - 2 * half
    return f"{text[:half]}\n... [{omitted} chars truncated] ...\n{text[-half:]}"


class EventStore:
    def __init__(
        self,
        db: Database,
        runs_dir: Path,
        *,
        observation_chars: int = 4000,
        secrets: Iterable[str] = (),
        bus: EventBus | None = None,
    ) -> None:
        self.db = db
        self.runs_dir = Path(runs_dir)
        self.observation_chars = observation_chars
        self.secrets = [s for s in secrets if s]
        self.bus = bus or EventBus()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ redaction
    def redact(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, REDACTED)
        for pat in _TOKEN_PATTERNS:
            text = pat.sub(REDACTED, text)
        return text

    def _redact_obj(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.redact(obj)
        if isinstance(obj, dict):
            return {self.redact(str(k)): self._redact_obj(v) for k, v in obj.items()}
        if isinstance(obj, list | tuple):
            return [self._redact_obj(v) for v in obj]
        return obj

    # ------------------------------------------------------------------ writes
    def append(
        self,
        run_id: str,
        type: str,
        source: str,
        payload: dict[str, Any] | None = None,
        node: str | None = None,
        cause: int | None = None,
        artifact_text: str | None = None,
    ) -> int:
        """Append one event and return its ``seq``. Never updates existing events."""
        body: dict[str, Any] = self._redact_obj(payload or {})
        artifact_rel: str | None = None
        ts = datetime.now(UTC).isoformat()
        with self._lock, self.db.transaction() as conn:
            row = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE run_id=?", (run_id,)).fetchone()
            seq = int(row[0])
            if artifact_text is not None:
                clean = self.redact(artifact_text)
                if len(clean) > self.observation_chars:
                    artifact_rel = f"artifacts/{seq}.txt"
                    path = self.runs_dir / run_id / artifact_rel
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(clean, encoding="utf-8")
                    body["text"] = truncate_middle(clean, self.observation_chars)
                    body["truncated"] = True
                else:
                    body["text"] = clean
                    body["truncated"] = False
            conn.execute(
                "INSERT INTO events(run_id, seq, ts, type, source, node, cause, payload, artifact)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (run_id, seq, ts, type, source, node, cause, json.dumps(body, default=str), artifact_rel),
            )
        self.bus.publish(self._to_event(run_id, seq, ts, type, source, node, cause, body, artifact_rel))
        return seq

    # ------------------------------------------------------------------ reads
    @staticmethod
    def _to_event(
        run_id: str,
        seq: int,
        ts: str,
        type: str,
        source: str,
        node: str | None,
        cause: int | None,
        payload: dict[str, Any],
        artifact: str | None,
    ) -> Event:
        return Event.model_validate(
            {
                "run_id": run_id,
                "seq": seq,
                "ts": ts,
                "type": type,
                "source": source,
                "node": node,
                "cause": cause,
                "payload": payload,
                "artifact": artifact,
            }
        )

    def _row_to_event(self, r: Any) -> Event:
        return self._to_event(
            r["run_id"],
            r["seq"],
            r["ts"],
            r["type"],
            r["source"],
            r["node"],
            r["cause"],
            json.loads(r["payload"] or "{}"),
            r["artifact"],
        )

    def list(self, run_id: str, after_seq: int = 0, limit: int | None = None) -> list[Event]:
        sql = "SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq"
        params: list[Any] = [run_id, after_seq]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [self._row_to_event(r) for r in self.db.query(sql, params)]

    def get(self, run_id: str, seq: int) -> Event | None:
        r = self.db.query_one("SELECT * FROM events WHERE run_id=? AND seq=?", (run_id, seq))
        return self._row_to_event(r) if r else None

    def last_seq(self, run_id: str) -> int:
        r = self.db.query_one("SELECT COALESCE(MAX(seq),0) FROM events WHERE run_id=?", (run_id,))
        return int(r[0]) if r else 0

    def read_artifact(self, run_id: str, seq: int) -> str | None:
        ev = self.get(run_id, seq)
        if ev is None:
            return None
        if ev.artifact:
            return (self.runs_dir / run_id / ev.artifact).read_text(encoding="utf-8")
        text = ev.payload.get("text")
        return text if isinstance(text, str) else None
