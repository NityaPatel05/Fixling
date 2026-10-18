"""FastAPI service: REST + SSE over the event log (spec E21).

The dashboard is a pure view of the event log; everything it shows is derived from ``/events`` and ``/stream``.
The service is meant for localhost: it rejects foreign ``Host`` headers (DNS rebinding) and sends no CORS headers.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from fixling.gh.client import GitHubClient
from fixling.graph.pipeline import Pipeline
from fixling.models import Event
from fixling.runs import RunStore
from fixling.services import Services

TERMINAL = {"done", "failed", "cancelled"}
EVENT_PAGE = 2000
HealthFn = Callable[[], dict[str, Any]]


class CreateRun(BaseModel):
    repo: str | None = None
    issue_number: int | None = Field(default=None, ge=1)
    local_path: str | None = None
    issue_title: str | None = Field(default=None, max_length=300)
    issue_body: str | None = Field(default=None, max_length=60_000)
    interactive: bool = False


class ResumeBody(BaseModel):
    action: str = Field(pattern="^(approve|edit_spec|cancel)$")
    spec: dict[str, Any] | None = None


def event_json(e: Event) -> dict[str, Any]:
    return {
        "run_id": e.run_id,
        "seq": e.seq,
        "ts": e.ts.isoformat(),
        "type": e.type,
        "source": e.source,
        "node": e.node,
        "cause": e.cause,
        "payload": e.payload,
        "artifact": e.artifact,
    }


def _seconds(row: dict[str, Any]) -> float | None:
    if not (row.get("started_at") and row.get("finished_at")):
        return None
    return (datetime.fromisoformat(row["finished_at"]) - datetime.fromisoformat(row["started_at"])).total_seconds()


def run_view(services: Services, row: dict[str, Any]) -> dict[str, Any]:
    d = services.cfg.run_dir(row["run_id"])
    return {
        **{k: v for k, v in row.items() if k != "trigger_key"},
        "duration_s": _seconds(row),
        "has_patch": (d / "patch.diff").exists(),
        "has_report": (d / "report.md").exists(),
    }


def default_health(services: Services) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        services.llm.health()
        out["lmstudio"] = {"ok": True, "detail": f"'{services.cfg.models.main_model}' and embeddings loaded"}
    except Exception as exc:
        out["lmstudio"] = {"ok": False, "detail": str(exc)}
    try:
        import docker

        docker.from_env().ping()
        out["docker"] = {"ok": True, "detail": "daemon reachable"}
    except Exception as exc:
        out["docker"] = {"ok": False, "detail": f"{type(exc).__name__}: {str(exc)[:120]}"}
    try:
        services.db.query_one("select 1")
        out["database"] = {"ok": True, "detail": str(services.cfg.db_path)}
    except Exception as exc:
        out["database"] = {"ok": False, "detail": str(exc)}
    out["github"] = {
        "ok": bool(services.cfg.settings.github_token),
        "detail": "token set" if services.cfg.settings.github_token else "no GITHUB_TOKEN (GitHub mode off)",
    }
    return out


def create_app(
    services: Services,
    pipeline: Pipeline | None = None,
    *,
    github: GitHubClient | None = None,
    health: HealthFn | None = None,
    web_dist: Path | None = None,
    on_error: Callable[[str, BaseException], None] | None = None,
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "[::1]", "testserver"),
    local_roots: list[Path] | None = None,
) -> FastAPI:
    app = FastAPI(title="Fixling", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))
    runs: RunStore = services.runs
    cfg = services.cfg
    roots = [r.resolve() for r in (local_roots or [Path(p) for p in cfg.settings.local_roots])]

    def need_run(run_id: str) -> dict[str, Any]:
        row = runs.get_run(run_id)
        if row is None:
            raise HTTPException(404, f"unknown run {run_id}")
        return row

    # ------------------------------------------------------------------ health / stats
    @app.get("/api/health")
    def get_health() -> dict[str, Any]:
        return (health or (lambda: default_health(services)))()

    @app.get("/api/stats")
    def stats() -> dict[str, Any]:
        rows = runs.list_runs(limit=5000)
        by_status: dict[str, int] = {}
        by_outcome: dict[str, int] = {}
        by_conf: dict[str, int] = {}
        durations: list[float] = []
        for r in rows:
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1
            if r.get("outcome"):
                by_outcome[r["outcome"]] = by_outcome.get(r["outcome"], 0) + 1
            if r.get("confidence"):
                by_conf[r["confidence"]] = by_conf.get(r["confidence"], 0) + 1
            if (s := _seconds(r)) is not None:
                durations.append(s)
        return {
            "total": len(rows),
            "by_status": by_status,
            "by_outcome": by_outcome,
            "by_confidence": by_conf,
            "tokens_in": sum(int(r.get("tokens_in") or 0) for r in rows),
            "tokens_out": sum(int(r.get("tokens_out") or 0) for r in rows),
            "median_duration_s": sorted(durations)[len(durations) // 2] if durations else None,
        }

    # ------------------------------------------------------------------ runs
    @app.get("/api/runs")
    def list_runs(repo: str | None = None, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        return [run_view(services, r) for r in runs.list_runs(repo=repo, status=status, limit=min(limit, 1000))]

    @app.post("/api/runs", status_code=201)
    def create_run(body: CreateRun) -> dict[str, Any]:
        issue: dict[str, Any] = {"interactive": body.interactive}
        if body.local_path:
            path = Path(body.local_path).resolve()
            if not any(path == r or path.is_relative_to(r) for r in roots):
                raise HTTPException(
                    403, "local_path is outside FIXLING_LOCAL_ROOTS (local runs via the API are disabled by default)"
                )
            if not path.is_dir():
                raise HTTPException(400, "local_path is not a directory")
            if not (body.issue_title or body.issue_body):
                raise HTTPException(400, "give issue_title/issue_body for a local run")
            issue |= {
                "repo": path.name,
                "number": None,
                "title": body.issue_title or "",
                "body": body.issue_body or "",
                "author": "local",
                "local_path": str(path),
            }
            repo, number = path.name, None
        elif body.repo and body.issue_number:
            if github is None:
                raise HTTPException(400, "GitHub mode needs GITHUB_TOKEN")
            data = github.get_issue(body.repo, body.issue_number)
            issue |= {
                "repo": body.repo,
                "number": body.issue_number,
                "title": data.get("title", ""),
                "body": data.get("body") or "",
                "author": (data.get("user") or {}).get("login", "unknown"),
                "url": data.get("html_url", ""),
            }
            repo, number = body.repo, body.issue_number
        else:
            raise HTTPException(422, "give repo + issue_number, or local_path + issue text")
        run_id = runs.create_run(
            repo=repo, issue_number=number, issue_url=issue.get("url"), model=cfg.models.main_model
        )
        assert run_id is not None
        d = cfg.run_dir(run_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "issue.json").write_text(json.dumps(issue, indent=2), encoding="utf-8")
        return run_view(services, runs.get_run(run_id) or {})

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        return run_view(services, need_run(run_id))

    @app.get("/api/runs/{run_id}/summary")
    def run_summary(run_id: str) -> dict[str, Any]:
        """The latest event of each kind that the run page's cards need, so the UI never rescans the log."""
        need_run(run_id)
        latest: dict[str, dict[str, Any]] = {}
        for e in services.events.list(run_id):
            if e.type in (
                "spec",
                "triage",
                "env",
                "retrieval",
                "repro",
                "selection",
                "verify",
                "outcome",
                "strength",
                "judge",
            ):
                latest[e.type] = event_json(e)
        return {"run": run_view(services, need_run(run_id)), "latest": latest}

    @app.get("/api/runs/{run_id}/events")
    def get_events(run_id: str, after: int = 0, limit: int = EVENT_PAGE) -> list[dict[str, Any]]:
        need_run(run_id)
        return [event_json(e) for e in services.events.list(run_id, after, min(limit, EVENT_PAGE))]

    @app.get("/api/runs/{run_id}/artifacts/{seq}", response_class=PlainTextResponse)
    def get_artifact(run_id: str, seq: int) -> str:
        need_run(run_id)
        text = services.events.read_artifact(run_id, seq)
        if text is None:
            raise HTTPException(404, "no artifact for that event")
        return text

    @app.get("/api/runs/{run_id}/files/{name}", response_class=PlainTextResponse)
    def get_file(run_id: str, name: str) -> str:
        """Only the two outputs a reviewer needs; the name is checked against an allow-list, never joined blindly."""
        need_run(run_id)
        if name not in ("patch.diff", "report.md"):
            raise HTTPException(404, "unknown file")
        path = cfg.run_dir(run_id) / name
        if not path.exists():
            raise HTTPException(404, f"{name} does not exist yet")
        return path.read_text(encoding="utf-8")

    @app.get("/api/runs/{run_id}/candidates")
    def get_candidates(run_id: str) -> dict[str, Any]:
        need_run(run_id)
        events = services.events.list(run_id)
        findings: dict[str, list[dict[str, Any]]] = {}
        for e in events:
            if e.type == "gate_finding":
                findings.setdefault(str(e.payload.get("candidate")), []).append(
                    {k: v for k, v in e.payload.items() if k != "candidate"}
                )
        scores: dict[str, dict[str, Any]] = {}
        best: str | None = None
        for e in events:
            if e.type == "selection":
                for r in e.payload.get("ranked", []):
                    scores[str(r["id"])] = r
                best = e.payload.get("best") or best
        out = []
        for e in events:
            if e.type != "candidate":
                continue
            cid = str(e.payload.get("id"))
            patch_file = cfg.run_dir(run_id) / "candidates" / f"{cid}.diff"
            out.append(
                {
                    **e.payload,
                    "event": e.seq,
                    "diff": patch_file.read_text(encoding="utf-8") if patch_file.exists() else None,
                    "findings": findings.get(cid, []),
                    "score": (scores.get(cid) or {}).get("score"),
                    "agreement": (scores.get(cid) or {}).get("agreement"),
                    "selected": cid == best,
                }
            )
        return {"best": best, "candidates": out}

    # ------------------------------------------------------------------ live stream
    @app.get("/api/runs/{run_id}/stream")
    async def stream(run_id: str, request: Request, after: int = 0) -> EventSourceResponse:
        need_run(run_id)
        last_id = request.headers.get("last-event-id")
        if last_id and last_id.isdigit():
            after = max(after, int(last_id))

        async def gen() -> AsyncIterator[dict[str, Any]]:
            sub = services.events.bus.subscribe(run_id)  # subscribe BEFORE replaying: nothing can slip between
            try:
                last = after
                for e in services.events.list(run_id, after):
                    last = e.seq
                    yield {"id": str(e.seq), "event": "event", "data": json.dumps(event_json(e))}
                while True:
                    row = runs.get_run(run_id) or {}
                    if row.get("status") in TERMINAL and sub.queue.empty():
                        yield {
                            "event": "end",
                            "data": json.dumps({"status": row.get("status"), "outcome": row.get("outcome")}),
                        }
                        return
                    try:
                        e = await asyncio.wait_for(sub.queue.get(), timeout=2.0)
                    except TimeoutError:
                        if await request.is_disconnected():
                            return
                        continue
                    if e.seq <= last:
                        continue  # already replayed
                    last = e.seq
                    yield {"id": str(e.seq), "event": "event", "data": json.dumps(event_json(e))}
            finally:
                services.events.bus.unsubscribe(sub)

        return EventSourceResponse(gen(), ping=15)

    # ------------------------------------------------------------------ control
    def _background(run_id: str, fn: Callable[[], Any]) -> None:
        def work() -> None:
            try:
                fn()
            except Exception as exc:
                if on_error:
                    on_error(run_id, exc)

        threading.Thread(target=work, name=f"resume-{run_id[:8]}", daemon=True).start()

    @app.post("/api/runs/{run_id}/resume", status_code=202)
    def resume_run(run_id: str, body: ResumeBody) -> dict[str, Any]:
        row = need_run(run_id)
        if row["status"] != "interrupted":
            raise HTTPException(409, f"run is {row['status']}, not waiting for input")
        if pipeline is None:
            raise HTTPException(503, "no pipeline is attached to this server")
        value = {"action": body.action, **({"spec": body.spec} if body.spec else {})}
        _background(run_id, lambda: pipeline.resume(run_id, value))
        return {"status": "resuming", "action": body.action}

    @app.post("/api/runs/{run_id}/cancel", status_code=202)
    def cancel_run(run_id: str) -> dict[str, Any]:
        row = need_run(run_id)
        if row["status"] in TERMINAL:
            raise HTTPException(409, f"run already {row['status']}")
        services.cancel.request(run_id)  # every guard stops the run at its next step, then finalize runs
        if row["status"] == "interrupted" and pipeline is not None:
            _background(run_id, lambda: pipeline.resume(run_id, {"action": "cancel"}))
        elif row["status"] == "queued":
            runs.update_run(run_id, status="cancelled", outcome="no_result", confidence="none")
        return {"status": "cancelling"}

    # ------------------------------------------------------------------ evals
    @app.get("/api/evals")
    def list_evals() -> list[dict[str, Any]]:
        results = cfg.root / "evals" / "results"
        out: list[dict[str, Any]] = []
        for p in sorted(results.glob("*.summary.json")) if results.exists() else []:
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return out

    # ------------------------------------------------------------------ dashboard
    dist = web_dist if web_dist is not None else cfg.root / "web" / "dist"
    if dist.exists() and (dist / "index.html").exists():
        if (dist / "assets").exists():
            app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False, response_model=None)
        def spa(path: str) -> FileResponse | JSONResponse:
            if path.startswith("api/"):
                return JSONResponse({"detail": "not found"}, status_code=404)
            candidate = (dist / path).resolve()
            if path and candidate.is_file() and candidate.is_relative_to(dist.resolve()):
                return FileResponse(candidate)
            return FileResponse(dist / "index.html")

    return app
