"""Hardened Docker sandbox (spec E5).

Agent processes run as uid 10001 with an EMPTY capability set, no network, a read-only root filesystem and
tmpfs scratch space. The controller loads files and reads the base-commit diff through *privileged execs*, which
exist only for the controller's own commands; nothing the agent runs ever holds a capability.

Layout choices that close holes found while probing the spec's layout against a real daemon:

* ``put_archive`` is rejected on a read-only rootfs (even for tmpfs mounts), so trees are loaded with
  ``docker exec --privileged -u 0 tar -x`` (ownership preserved).
* ``/workspace`` itself is root-owned (0755, no sticky bit): otherwise the agent could ``mv tests tests2`` and
  re-create a ``tests/`` of its own, whatever the ownership of the entries inside.
* The base commit lives in ``/gitbase``, a root-owned tmpfs outside the workspace.
"""

from __future__ import annotations

import base64
import io
import shlex
import shutil
import subprocess
import tarfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Any

import docker
from docker.errors import APIError, NotFound
from docker.models.containers import Container

from fixling.errors import SandboxError
from fixling.models import ExecResult
from fixling.runs import RunStore
from fixling.sandbox.base import DIFF_CMD, GITDIR, INIT_BASE_CMD, WORKSPACE, Owner, resolve_path

AGENT_UID = 10001
MAX_CAPTURE = 2_000_000  # bytes kept per stream
MAX_READ = 5_000_000
WRITE_CHUNK = 60_000  # base64 chars per exec, safely below argv limits
_POOL = ThreadPoolExecutor(max_workers=16, thread_name_prefix="sandbox-exec")


@dataclass(frozen=True)
class SandboxLimits:
    memory: str = "4g"
    cpus: float = 2.0
    pids: int = 512
    ttl_s: int = 1800
    workspace_size: str = "2g"
    tmp_size: str = "512m"


def _decode(data: bytes | None) -> str:
    return (data or b"")[:MAX_CAPTURE].decode("utf-8", errors="replace")


class DockerSandbox:
    def __init__(
        self,
        client: docker.DockerClient,
        container: Container,
        role: str,
        run_id: str,
        runs: RunStore | None = None,
    ) -> None:
        self.client = client
        self.container = container
        self.id: str = container.id or ""
        self.role = role
        self.run_id = run_id
        self.runs = runs
        self.closed = False

    # ------------------------------------------------------------------ lifecycle
    @classmethod
    def create(
        cls,
        client: docker.DockerClient,
        image: str,
        run_id: str,
        role: str,
        *,
        runs: RunStore | None = None,
        limits: SandboxLimits | None = None,
    ) -> DockerSandbox:
        lim = limits or SandboxLimits()
        name = f"fixling-{role}-{run_id[:12]}-{uuid.uuid4().hex[:6]}"
        container = client.containers.run(
            image,
            ["sleep", "infinity"],
            detach=True,
            name=name,
            user=f"{AGENT_UID}:{AGENT_UID}",
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            read_only=True,
            network_mode="none",
            tmpfs={
                "/tmp": f"rw,nosuid,nodev,size={lim.tmp_size}",
                WORKSPACE: f"rw,nosuid,nodev,exec,size={lim.workspace_size},uid=0,gid=0,mode=0755",
                GITDIR: "rw,nosuid,nodev,size=256m,uid=0,gid=0,mode=0755",
            },
            mem_limit=lim.memory,
            memswap_limit=lim.memory,
            nano_cpus=int(lim.cpus * 1_000_000_000),
            pids_limit=lim.pids,
            environment={"PYTHONPATH": f"{WORKSPACE}:{WORKSPACE}/src", "HOME": "/tmp"},
            labels={
                "fixling.run": run_id,
                "fixling.role": role,
                "fixling.expires": str(int(time.time()) + lim.ttl_s),
            },
        )
        sb = cls(client, container, role, run_id, runs)
        if runs:
            runs.register_sandbox(sb.id, run_id, role, lim.ttl_s)
        return sb

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.container.remove(force=True)
        except NotFound:
            pass
        except APIError as exc:
            raise SandboxError(f"removing sandbox {self.id[:12]} failed: {exc}") from exc
        finally:
            if self.runs:
                self.runs.mark_sandbox_removed(self.id)

    def __enter__(self) -> DockerSandbox:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ exec
    def _exec(
        self, argv: list[str], *, workdir: str = WORKSPACE, user: str | None = None, privileged: bool = False
    ) -> tuple[int, bytes, bytes]:
        kwargs: dict[str, Any] = {"workdir": workdir, "demux": True, "privileged": privileged}
        if user is not None:
            kwargs["user"] = user
        res = self.container.exec_run(argv, **kwargs)
        out, err = res.output if isinstance(res.output, tuple) else (res.output, b"")
        return int(res.exit_code if res.exit_code is not None else -1), out or b"", err or b""

    def exec(
        self, cmd: str, timeout_s: int, workdir: str = WORKSPACE, *, user: str | None = None, privileged: bool = False
    ) -> ExecResult:
        """Run ``cmd`` under ``bash -lc`` with a hard kill timeout inside the container and a watchdog outside."""
        argv = ["timeout", "-s", "KILL", str(timeout_s), "bash", "-lc", cmd]
        started = time.monotonic()
        fut = _POOL.submit(self._exec, argv, workdir=workdir, user=user, privileged=privileged)
        try:
            code, out, err = fut.result(timeout=timeout_s + 10)
        except FutureTimeout:
            self.container.kill()  # last resort: the in-container timeout did not fire
            return ExecResult(
                exit_code=137,
                stdout="",
                stderr="watchdog: command exceeded its time limit and the sandbox was killed",
                duration_ms=int((time.monotonic() - started) * 1000),
                timed_out=True,
            )
        elapsed = time.monotonic() - started
        return ExecResult(
            exit_code=code,
            stdout=_decode(out),
            stderr=_decode(err),
            duration_ms=int(elapsed * 1000),
            timed_out=code == 137 and elapsed >= timeout_s - 0.5,
        )

    def exec_root(self, cmd: str, timeout_s: int = 120) -> ExecResult:
        """Controller-only: runs as uid 0 with capabilities (git on the base commit, file ownership)."""
        return self.exec(cmd, timeout_s, user="0", privileged=True)

    # ------------------------------------------------------------------ files
    def put_tree(self, tar_bytes: bytes) -> None:
        """Extract a tar (ownership and modes preserved) into /workspace, then record the base commit."""
        docker_bin = shutil.which("docker")
        if not docker_bin:
            raise SandboxError("docker CLI not found on PATH (needed to stream the workspace tar)")
        proc = subprocess.run(
            [
                docker_bin,
                "exec",
                "-i",
                "--privileged",
                "-u",
                "0",
                self.id,
                "tar",
                "-xp",
                "--same-owner",
                "-C",
                WORKSPACE,
            ],
            input=tar_bytes,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            raise SandboxError(f"loading the workspace failed: {proc.stderr.decode(errors='replace')[-400:]}")
        res = self.exec_root(INIT_BASE_CMD, 180)
        if res.exit_code != 0:
            raise SandboxError(f"recording the base commit failed: {res.stderr[-400:]}")

    def write_file(self, path: str, content: str, owner: Owner = "agent") -> None:
        if owner != "agent":
            raise SandboxError("root-owned files can only be created through put_tree")
        dest = shlex.quote(resolve_path(path))
        data = base64.b64encode(content.encode("utf-8")).decode("ascii")
        chunks = [data[i : i + WRITE_CHUNK] for i in range(0, len(data), WRITE_CHUNK)] or [""]
        for i, chunk in enumerate(chunks):
            redirect = ">" if i == 0 else ">>"
            prep = f'mkdir -p "$(dirname {dest})" && ' if i == 0 else ""
            code, _, err = self._exec(["bash", "-c", f"{prep}printf %s '{chunk}' | base64 -d {redirect} {dest}"])
            if code != 0:
                raise SandboxError(f"cannot write {path}: {_decode(err).strip() or 'permission denied'}")

    def read_file(self, path: str) -> str:
        code, out, err = self._exec(["cat", "--", resolve_path(path)])
        if code != 0:
            raise SandboxError(f"cannot read {path}: {_decode(err).strip() or 'not found'}")
        if len(out) > MAX_READ:
            raise SandboxError(f"{path} is larger than {MAX_READ} bytes")
        return out.decode("utf-8", errors="replace")

    def diff(self) -> str:
        """Unified diff of the workspace against the base commit; new files included, binary-safe."""
        res = self.exec_root(DIFF_CMD, 120)
        if res.exit_code != 0:
            raise SandboxError(f"computing the diff failed: {res.stderr[-400:]}")
        return res.stdout

    # ------------------------------------------------------------------ introspection
    def hardening_report(self) -> dict[str, Any]:
        self.container.reload()
        host = self.container.attrs["HostConfig"]
        cfg = self.container.attrs["Config"]
        return {
            "user": cfg.get("User"),
            "cap_drop": host.get("CapDrop"),
            "cap_add": host.get("CapAdd"),
            "security_opt": host.get("SecurityOpt"),
            "read_only": host.get("ReadonlyRootfs"),
            "network_mode": host.get("NetworkMode"),
            "privileged": host.get("Privileged"),
            "pids_limit": host.get("PidsLimit"),
            "memory": host.get("Memory"),
            "binds": host.get("Binds"),
            "env": cfg.get("Env") or [],
            "mounts": sorted(
                [m.get("Destination") for m in self.container.attrs.get("Mounts", [])] + list(host.get("Tmpfs") or {})
            ),
        }


def make_tar(entries: list[tuple[str, bytes | None, int, int, int]]) -> bytes:
    """Test/helper: build a tar from ``(path, data|None-for-dir, uid, gid, mode)`` entries."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for path, data, uid, gid, mode in entries:
            info = tarfile.TarInfo(path)
            info.uid, info.gid, info.mode = uid, gid, mode
            if data is None:
                info.type = tarfile.DIRTYPE
                tf.addfile(info)
            else:
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()
