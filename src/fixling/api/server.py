"""``fixling serve``: API + dashboard + worker + poller + reaper in one process (spec E21)."""

from __future__ import annotations

import uvicorn

from fixling.api.app import create_app
from fixling.gh.client import GitHubClient
from fixling.gh.poller import Poller, PollerThread
from fixling.gh.publisher import LABELS, GitHubPublisher
from fixling.graph.pipeline import Pipeline
from fixling.sandbox.reaper import Reaper, reconcile
from fixling.services import Services, build_services, docker_provider
from fixling.worker import Worker


def serve(host: str = "127.0.0.1", port: int = 8000, *, services: Services | None = None) -> None:
    services = services or build_services()
    cfg = services.cfg
    provider = docker_provider(services)

    gh: GitHubClient | None = None
    publisher: GitHubPublisher | None = None
    if cfg.settings.github_token:
        gh = GitHubClient(cfg.settings.github_token)
        publisher = GitHubPublisher(cfg, gh, services.events, services.runs)
        for repo in cfg.settings.repos:
            try:
                gh.ensure_labels(repo, LABELS)
            except Exception as exc:
                print(f"could not create labels on {repo}: {exc}")

    pipeline = Pipeline(services.deps(provider, publisher=publisher))
    worker = Worker(services, pipeline, gh=gh)

    try:  # startup reconciliation: align the DB with Docker and drop orphaned sandboxes from a previous crash
        from fixling.sandbox.images import get_client

        client = get_client()
        reconcile(client, services.runs, active_runs={r["run_id"] for r in services.runs.list_runs(status="running")})
        Reaper(client, services.runs).start()
    except Exception as exc:
        print(f"Docker is not available ({type(exc).__name__}); runs will fail until it is started")

    worker.start()
    if gh is not None and cfg.settings.repos:
        PollerThread(Poller(cfg, gh, services.runs)).start()
        print(f"polling {', '.join(cfg.settings.repos)} for the 'fixling' label")

    app = create_app(services, pipeline, github=gh, on_error=worker.finalize_on_crash)
    print(f"Fixling dashboard and API: http://{host}:{port}")
    try:
        uvicorn.run(app, host=host, port=port, log_level="info")
    finally:
        worker.stop()
