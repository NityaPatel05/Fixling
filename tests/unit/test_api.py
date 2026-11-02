from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from fixling.api.app import create_app
from fixling.config import Config
from fixling.services import Services, build_services

REPO_SECRET_LOOKALIKE = "ghp_" + "A" * 36


class FakePipeline:
    def __init__(self, services: Services) -> None:
        self.services = services
        self.resumed: list[tuple[str, dict[str, Any]]] = []
        self.fail = False

    def resume(self, run_id: str, value: dict[str, Any]) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("resume exploded")
        self.resumed.append((run_id, value))
        self.services.runs.update_run(run_id, status="done", outcome="no_result")
        return {}


@pytest.fixture
def api(cfg: Config, tmp_path: Path) -> tuple[TestClient, Services, FakePipeline, list[tuple[str, BaseException]]]:
    services = build_services(cfg)
    pipeline = FakePipeline(services)
    errors: list[tuple[str, BaseException]] = []
    app = create_app(
        services,
        pipeline,  # type: ignore[arg-type]
        health=lambda: {"lmstudio": {"ok": True, "detail": "x"}},
        web_dist=tmp_path / "no-dist",
        on_error=lambda rid, exc: errors.append((rid, exc)),
        local_roots=[tmp_path / "allowed"],
    )
    return TestClient(app), services, pipeline, errors


def seed_run(services: Services, **kw: Any) -> str:
    rid = services.runs.create_run(repo=kw.pop("repo", "o/r"), issue_number=kw.pop("issue_number", 3)) or ""
    if kw:
        services.runs.update_run(rid, **kw)
    return rid


def test_health_and_unknown_host_rejection(api: Any) -> None:
    client, *_ = api
    assert client.get("/api/health").json()["lmstudio"]["ok"] is True
    # DNS-rebinding protection: a page on evil.example pointing at 127.0.0.1 is refused
    assert client.get("/api/health", headers={"host": "evil.example"}).status_code == 400
    assert not any(h.lower().startswith("access-control") for h in client.get("/api/health").headers)


def test_run_listing_filters_and_detail(api: Any) -> None:
    client, services, *_ = api
    a = seed_run(services, status="done", outcome="verified_fix", confidence="high")
    b = seed_run(services, repo="o/other", issue_number=9, status="queued")
    listed = client.get("/api/runs").json()
    assert [r["run_id"] for r in listed] == [b, a] and "trigger_key" not in listed[0]
    assert [r["run_id"] for r in client.get("/api/runs", params={"status": "done"}).json()] == [a]
    assert [r["run_id"] for r in client.get("/api/runs", params={"repo": "o/other"}).json()] == [b]
    detail = client.get(f"/api/runs/{a}").json()
    assert detail["outcome"] == "verified_fix" and detail["has_patch"] is False
    assert client.get("/api/runs/nope").status_code == 404


def test_events_pages_and_artifacts(api: Any) -> None:
    client, services, *_ = api
    rid = seed_run(services)
    for i in range(5):
        services.events.append(rid, "exec", "sandbox", {"i": i})
    seq = services.events.append(rid, "exec", "sandbox", {"i": 99}, artifact_text="x" * 9000)
    assert [
        e["payload"]["i"] for e in client.get(f"/api/runs/{rid}/events", params={"after": 3, "limit": 2}).json()
    ] == [3, 4]  # events 4 and 5 (seq > 3), payload i is seq - 1
    assert client.get(f"/api/runs/{rid}/artifacts/{seq}").text == "x" * 9000
    assert client.get(f"/api/runs/{rid}/artifacts/1").status_code == 404  # no artifact on that event


def test_files_endpoint_is_an_allow_list(api: Any, cfg: Config) -> None:
    client, services, *_ = api
    rid = seed_run(services)
    d = cfg.run_dir(rid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "patch.diff").write_text("diff --git a/x b/x\n", encoding="utf-8")
    (d / "secret.txt").write_text("nope", encoding="utf-8")
    assert client.get(f"/api/runs/{rid}/files/patch.diff").text.startswith("diff --git")
    assert client.get(f"/api/runs/{rid}/files/report.md").status_code == 404
    for evil in ("secret.txt", "..%2F..%2Ffixling.sqlite", "issue.json"):
        assert client.get(f"/api/runs/{rid}/files/{evil}").status_code == 404


def test_summary_returns_the_latest_event_of_each_kind(api: Any) -> None:
    client, services, *_ = api
    rid = seed_run(services)
    services.events.append(rid, "spec", "llm", {"summary": "first"})
    services.events.append(rid, "spec", "llm", {"summary": "second"})
    services.events.append(rid, "outcome", "system", {"status": "verified_fix"})
    s = client.get(f"/api/runs/{rid}/summary").json()
    assert (
        s["latest"]["spec"]["payload"]["summary"] == "second"
        and s["latest"]["outcome"]["payload"]["status"] == "verified_fix"
    )
    assert "verify" not in s["latest"]


def test_candidates_view_joins_patches_findings_and_selection(api: Any, cfg: Config) -> None:
    client, services, *_ = api
    rid = seed_run(services)
    d = cfg.run_dir(rid) / "candidates"
    d.mkdir(parents=True)
    (d / "r0c0.diff").write_text("diff --git a/a b/a\n", encoding="utf-8")
    services.events.append(rid, "candidate", "llm", {"id": "r0c0", "temperature": 0.2, "repro_passed": True})
    services.events.append(rid, "candidate", "llm", {"id": "r0c1", "temperature": 0.5, "repro_passed": False})
    services.events.append(
        rid, "gate_finding", "system", {"candidate": "r0c1", "rule": "literal_return", "severity": "flag"}
    )
    services.events.append(
        rid, "selection", "system", {"ranked": [{"id": "r0c0", "score": 149.8, "agreement": 1}], "best": "r0c0"}
    )
    data = client.get(f"/api/runs/{rid}/candidates").json()
    assert data["best"] == "r0c0"
    c0, c1 = data["candidates"]
    assert c0["selected"] and c0["diff"].startswith("diff --git") and c0["score"] == 149.8 and c0["agreement"] == 1
    assert not c1["selected"] and c1["diff"] is None and c1["findings"][0]["rule"] == "literal_return"


# ---------------------------------------------------------------- creating runs
def test_create_run_for_a_github_issue(api: Any, cfg: Config) -> None:
    client, services, *_ = api

    class FakeGH:
        def get_issue(self, repo: str, number: int) -> dict[str, Any]:
            return {
                "title": "T",
                "body": "B",
                "user": {"login": "alice"},
                "html_url": "https://github.com/o/r/issues/3",
            }

    app = create_app(services, None, github=FakeGH(), web_dist=Path("nope"))  # type: ignore[arg-type]
    c = TestClient(app)
    created = c.post("/api/runs", json={"repo": "o/r", "issue_number": 3, "interactive": True})
    assert created.status_code == 201 and created.json()["status"] == "queued"
    snap = json.loads((cfg.run_dir(created.json()["run_id"]) / "issue.json").read_text(encoding="utf-8"))
    assert (snap["title"], snap["author"], snap["interactive"], snap["number"]) == ("T", "alice", True, 3)
    assert (
        client.post("/api/runs", json={"repo": "o/r", "issue_number": 3}).status_code == 400
    )  # no GitHub client attached


def test_create_local_run_only_under_allowed_roots(api: Any, tmp_path: Path, cfg: Config) -> None:
    client, *_ = api
    allowed = tmp_path / "allowed" / "proj"
    allowed.mkdir(parents=True)
    ok = client.post("/api/runs", json={"local_path": str(allowed), "issue_title": "t", "issue_body": "b"})
    assert ok.status_code == 201
    snap = json.loads((cfg.run_dir(ok.json()["run_id"]) / "issue.json").read_text(encoding="utf-8"))
    assert snap["local_path"] == str(allowed.resolve()) and snap["repo"] == "proj"
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert client.post("/api/runs", json={"local_path": str(outside), "issue_title": "t"}).status_code == 403
    traversal = f"{allowed}/../../elsewhere"
    assert client.post("/api/runs", json={"local_path": traversal, "issue_title": "t"}).status_code == 403
    assert client.post("/api/runs", json={"local_path": str(allowed)}).status_code == 400  # no issue text
    assert client.post("/api/runs", json={}).status_code == 422


def test_create_run_validates_input(api: Any) -> None:
    client, *_ = api
    assert client.post("/api/runs", json={"repo": "o/r", "issue_number": 0}).status_code == 422
    assert client.post("/api/runs", json={"local_path": "x", "issue_title": "t" * 301}).status_code == 422


# ---------------------------------------------------------------- control
def test_resume_only_when_interrupted_and_runs_in_the_background(api: Any) -> None:
    client, services, pipeline, _ = api
    waiting = seed_run(services, status="interrupted")
    done = seed_run(services, status="done")
    assert client.post(f"/api/runs/{done}/resume", json={"action": "approve"}).status_code == 409
    assert client.post(f"/api/runs/{waiting}/resume", json={"action": "bogus"}).status_code == 422
    r = client.post(f"/api/runs/{waiting}/resume", json={"action": "edit_spec", "spec": {"summary": "s"}})
    assert r.status_code == 202
    deadline = time.time() + 5
    while not pipeline.resumed and time.time() < deadline:
        time.sleep(0.02)
    assert pipeline.resumed == [(waiting, {"action": "edit_spec", "spec": {"summary": "s"}})]


def test_resume_failure_is_reported_to_the_error_handler(api: Any) -> None:
    client, services, pipeline, errors = api
    pipeline.fail = True
    rid = seed_run(services, status="interrupted")
    client.post(f"/api/runs/{rid}/resume", json={"action": "approve"})
    deadline = time.time() + 5
    while not errors and time.time() < deadline:
        time.sleep(0.02)
    assert errors and errors[0][0] == rid and "exploded" in str(errors[0][1])


def test_cancel_variants(api: Any) -> None:
    client, services, pipeline, _ = api
    running = seed_run(services, status="running")
    queued = seed_run(services, status="queued")
    waiting = seed_run(services, status="interrupted")
    finished = seed_run(services, status="done")
    assert client.post(f"/api/runs/{running}/cancel").status_code == 202 and services.cancel.is_set(running)
    assert client.post(f"/api/runs/{queued}/cancel").status_code == 202
    assert services.runs.get_run(queued)["status"] == "cancelled"  # type: ignore[index]  # never started, so just dropped
    assert client.post(f"/api/runs/{waiting}/cancel").status_code == 202
    deadline = time.time() + 5
    while not pipeline.resumed and time.time() < deadline:
        time.sleep(0.02)
    assert pipeline.resumed == [(waiting, {"action": "cancel"})]
    assert client.post(f"/api/runs/{finished}/cancel").status_code == 409


def test_stats(api: Any) -> None:
    client, services, *_ = api
    seed_run(services, status="done", outcome="verified_fix", confidence="high")
    seed_run(services, status="done", outcome="repro_only", confidence="none")
    seed_run(services, status="queued")
    s = client.get("/api/stats").json()
    assert s["total"] == 3 and s["by_status"] == {"done": 2, "queued": 1}
    assert s["by_outcome"] == {"verified_fix": 1, "repro_only": 1} and s["by_confidence"]["high"] == 1


def test_evals_endpoint_reads_summaries(api: Any, cfg: Config) -> None:
    client, *_ = api
    assert client.get("/api/evals").json() == []
    results = cfg.root / "evals" / "results"
    results.mkdir(parents=True, exist_ok=True)
    (results / "zz-test.summary.json").write_text(
        json.dumps({"config": "baseline", "resolve_at_1": 0.5}), encoding="utf-8"
    )
    try:
        assert {"config": "baseline", "resolve_at_1": 0.5} in client.get("/api/evals").json()
    finally:
        (results / "zz-test.summary.json").unlink()


# ---------------------------------------------------------------- SSE
def read_sse(client: TestClient, url: str, headers: dict[str, str] | None = None) -> list[tuple[str, str, str]]:
    """(id, event, data) triples from a finite SSE response."""
    out: list[tuple[str, str, str]] = []
    cur: dict[str, str] = {}
    with client.stream("GET", url, headers=headers or {}) as resp:
        for line in resp.iter_lines():
            if line.startswith(":"):
                continue
            if not line:
                if cur:
                    out.append((cur.get("id", ""), cur.get("event", ""), cur.get("data", "")))
                cur = {}
                continue
            key, _, value = line.partition(":")
            cur[key] = value.strip()
    return out


def test_stream_replays_a_finished_run_then_ends(api: Any) -> None:
    client, services, *_ = api
    rid = seed_run(services, status="done", outcome="verified_fix")
    for i in range(3):
        services.events.append(rid, "exec", "sandbox", {"i": i})
    frames = read_sse(client, f"/api/runs/{rid}/stream")
    events = [f for f in frames if f[1] == "event"]
    assert [json.loads(f[2])["payload"]["i"] for f in events] == [0, 1, 2] and [f[0] for f in events] == ["1", "2", "3"]
    assert frames[-1][1] == "end" and json.loads(frames[-1][2])["outcome"] == "verified_fix"


def test_stream_resumes_after_last_event_id_or_query(api: Any) -> None:
    client, services, *_ = api
    rid = seed_run(services, status="done")
    for i in range(4):
        services.events.append(rid, "exec", "sandbox", {"i": i})
    by_header = [
        json.loads(f[2])["seq"]
        for f in read_sse(client, f"/api/runs/{rid}/stream", {"last-event-id": "2"})
        if f[1] == "event"
    ]
    by_query = [json.loads(f[2])["seq"] for f in read_sse(client, f"/api/runs/{rid}/stream?after=3") if f[1] == "event"]
    assert by_header == [3, 4] and by_query == [4]


def test_stream_delivers_live_events_without_gaps_or_duplicates(api: Any) -> None:
    client, services, *_ = api
    rid = seed_run(services, status="running")
    services.events.append(rid, "run_started", "system", {})

    def producer() -> None:
        time.sleep(0.6)
        for i in range(5):
            services.events.append(rid, "exec", "sandbox", {"i": i})
            time.sleep(0.05)
        services.runs.update_run(rid, status="done", outcome="no_result")

    t = threading.Thread(target=producer)
    t.start()
    frames = read_sse(client, f"/api/runs/{rid}/stream")
    t.join()
    seqs = [json.loads(f[2])["seq"] for f in frames if f[1] == "event"]
    assert seqs == [1, 2, 3, 4, 5, 6], seqs  # replayed #1, then 5 live events, each exactly once and in order
    assert frames[-1][1] == "end"


def test_dashboard_is_served_when_built(cfg: Config, tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>dash</html>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")
    c = TestClient(create_app(build_services(cfg), None, web_dist=dist))
    assert c.get("/").text == "<html>dash</html>" and c.get("/runs/abc").text == "<html>dash</html>"  # SPA fallback
    assert c.get("/assets/app.js").text == "console.log(1)"
    assert c.get("/api/nope").status_code == 404 and c.get("/api/nope").json() == {"detail": "not found"}
    assert "secret" not in c.get("/..%2Foutside.txt").text and "secret" not in c.get("/%2e%2e/outside.txt").text
