"""Docker-backed ``SandboxProvider``: owns every live sandbox of every run (spec E5/E17)."""

from __future__ import annotations

import threading
from pathlib import Path

import docker
from docker.errors import NotFound

from fixling.config import Config
from fixling.events.store import EventStore
from fixling.repo.env_setup import EnvResult, setup_env
from fixling.repo.workspace import build_tree_tar
from fixling.runs import RunStore
from fixling.sandbox.base import Sandbox
from fixling.sandbox.docker_sandbox import DockerSandbox, SandboxLimits
from fixling.sandbox.images import ensure_env_image
from fixling.verify.policy import PolicyGate


class DockerProvider:
    def __init__(
        self,
        client: docker.DockerClient,
        cfg: Config,
        events: EventStore,
        runs: RunStore,
        limits: SandboxLimits | None = None,
    ) -> None:
        self.client, self.cfg, self.events, self.runs, self.limits = client, cfg, events, runs, limits
        self._live: dict[str, DockerSandbox] = {}
        self._images: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()

    def _track(self, sb: DockerSandbox) -> DockerSandbox:
        with self._lock:
            self._live[sb.id] = sb
        return sb

    def _image(self, repo_key: str, src_dir: Path) -> str:
        key = (repo_key, str(src_dir))
        with self._lock:
            cached = self._images.get(key)
        if cached:
            return cached
        tag, _ = ensure_env_image(self.client, repo_key, src_dir)  # cached by tag, so cheap after the first build
        with self._lock:
            self._images[key] = tag
        return tag

    def prepare_env(self, run_id: str, repo_key: str, src_dir: Path) -> tuple[EnvResult, Sandbox | None]:
        result, sb = setup_env(
            client=self.client,
            budgets=self.cfg.budgets,
            events=self.events,
            runs=self.runs,
            run_id=run_id,
            repo_key=repo_key,
            src_dir=src_dir,
            gate=PolicyGate(self.cfg.policy),
        )
        if result.image:
            with self._lock:
                self._images[(repo_key, str(src_dir))] = result.image
        if sb is not None:
            self._track(sb)
        return result, sb

    def create(
        self, run_id: str, role: str, src_dir: Path, repo_key: str, extra_files: dict[str, str] | None = None
    ) -> Sandbox:
        sb = DockerSandbox.create(
            self.client, self._image(repo_key, src_dir), run_id, role, runs=self.runs, limits=self.limits
        )
        self._track(sb)
        try:
            sb.put_tree(build_tree_tar(src_dir, PolicyGate(self.cfg.policy), extra_files))
        except Exception:
            self._forget(sb)
            sb.close()
            raise
        return sb

    def get(self, run_id: str, sandbox_id: str) -> Sandbox | None:
        with self._lock:
            sb = self._live.get(sandbox_id)
        if sb is not None and not sb.closed:
            return sb
        try:
            container = self.client.containers.get(sandbox_id)
        except NotFound:
            return None
        if container.status != "running":
            return None
        return self._track(DockerSandbox(self.client, container, "work", run_id, self.runs))

    def _forget(self, sb: DockerSandbox) -> None:
        with self._lock:
            self._live.pop(sb.id, None)

    def close_run(self, run_id: str) -> int:
        with self._lock:
            mine = [sb for sb in self._live.values() if sb.run_id == run_id]
        closed = 0
        for sb in mine:
            sb.close()
            self._forget(sb)
            closed += 1
        # anything registered in the DB but not tracked here (e.g. created before a restart)
        for row in self.runs.live_sandboxes(run_id):
            try:
                self.client.containers.get(row["container_id"]).remove(force=True)
                closed += 1
            except NotFound:
                pass
            self.runs.mark_sandbox_removed(row["container_id"])
        return closed
