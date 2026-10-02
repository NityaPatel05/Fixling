"""Test doubles. ``FakeLLM`` mirrors ``fixling.llm.client.LLM`` for deterministic graph tests."""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from pydantic import BaseModel

from fixling.errors import LLMFormatError

Scripted = BaseModel | dict[str, Any] | Exception | Callable[..., Any]


@dataclass
class Call:
    node: str
    schema: type[BaseModel]
    messages: list[dict[str, str]]
    temperature: float | None


@dataclass
class FakeLLM:
    """Scripted responses keyed by node name.

    ``script[node]`` is either a single response (reused for every call) or a list consumed in order
    (the last element repeats). A response is a pydantic instance, a dict (validated against the
    requested schema), an Exception (raised), or a callable ``(call) -> response``.
    """

    script: dict[str, Scripted | list[Scripted]] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)
    embed_dim: int = 16
    _idx: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _next(self, node: str) -> Scripted:
        with self._lock:
            return self._next_locked(node)

    def _next_locked(self, node: str) -> Scripted:
        if node not in self.script:
            raise LLMFormatError(f"FakeLLM has no scripted response for node {node!r}")
        entry = self.script[node]
        if isinstance(entry, list):
            i = min(self._idx[node], len(entry) - 1)
            self._idx[node] += 1
            return entry[i]
        return entry

    def structured(
        self,
        run_id: str,
        node: str,
        schema: type[BaseModel],
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        seed: int | None = None,
    ) -> BaseModel:
        call = Call(node, schema, messages, temperature)
        with self._lock:
            self.calls.append(call)
        item = self._next(node)
        if callable(item) and not isinstance(item, BaseModel | Exception):
            item = item(call)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, dict):
            return schema.model_validate(item)
        assert isinstance(item, BaseModel)
        return item

    def sample_n(
        self,
        run_id: str,
        node: str,
        schema: type[BaseModel],
        messages: list[dict[str, str]],
        temperatures: list[float],
    ) -> list[BaseModel | Exception]:
        out: list[BaseModel | Exception] = []
        for t in temperatures:
            try:
                out.append(self.structured(run_id, node, schema, messages, temperature=t))
            except Exception as exc:
                out.append(exc)
        return out

    def embed(self, texts: list[str]) -> np.ndarray:
        rng_vectors = []
        for t in texts:
            seed = abs(hash(t)) % (2**32)
            v = np.random.default_rng(seed).standard_normal(self.embed_dim)
            rng_vectors.append(v / np.linalg.norm(v))
        return np.array(rng_vectors, dtype=np.float32).reshape(len(texts), self.embed_dim)

    def count_tokens(self, text: str) -> int:
        return int(len(text) / 3.5)

    def health(self) -> None:
        return None


# ---------------------------------------------------------------- fake LM Studio over HTTP
# openai>=3 talks through ``httpx2``, so respx cannot intercept it; inject a MockTransport instead.
import json as _json  # noqa: E402
from typing import Any as _Any  # noqa: E402

import httpx2  # noqa: E402
import openai  # noqa: E402


def chat_body(content: str, *, finish: str = "stop", reasoning: str | None = None) -> dict[str, _Any]:
    message: dict[str, _Any] = {"role": "assistant", "content": content}
    if reasoning:
        message["reasoning"] = reasoning
    return {
        "id": "c1",
        "object": "chat.completion",
        "created": 0,
        "model": "fixling-main",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
    }


class Seq:
    """Handler yielding scripted items in order (the last repeats). Items: dict body, Response, Exception, callable."""

    def __init__(self, *items: _Any) -> None:
        self.items = list(items)
        self.requests: list[httpx2.Request] = []

    @property
    def call_count(self) -> int:
        return len(self.requests)

    def bodies(self) -> list[dict[str, _Any]]:
        return [_json.loads(r.content) for r in self.requests]

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        item = self.items[min(len(self.requests) - 1, len(self.items) - 1)]
        if callable(item):
            item = item(request)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, httpx2.Response):
            return item
        return httpx2.Response(200, json=item)


class Router:
    """Dispatch by URL path suffix, e.g. ``Router({"/chat/completions": Seq(...)})``."""

    def __init__(self, routes: dict[str, _Any]) -> None:
        self.routes = routes

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        for suffix, handler in self.routes.items():
            if request.url.path.endswith(suffix):
                return handler(request)  # type: ignore[no-any-return]
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    def client(self, base_url: str = "http://localhost:1234/v1") -> openai.OpenAI:
        return openai.OpenAI(
            base_url=base_url,
            api_key="x",
            max_retries=0,
            http_client=httpx2.Client(transport=httpx2.MockTransport(self)),
        )


def connect_error(request: httpx2.Request) -> httpx2.ConnectError:
    return httpx2.ConnectError("refused", request=request)


# ---------------------------------------------------------------- local (NON-isolated) sandbox for tests
import io  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402
import tarfile  # noqa: E402
import time  # noqa: E402
import uuid  # noqa: E402
from pathlib import Path  # noqa: E402

from fixling.models import ExecResult  # noqa: E402
from fixling.sandbox.base import DIFF_CMD, INIT_BASE_CMD  # noqa: E402


def _find_bash() -> str:
    """Git Bash on Windows (``bash`` on PATH may be WSL's, which cannot see the host Python)."""
    import shutil

    for cand in ("C:/Program Files/Git/bin/bash.exe", "C:/Program Files (x86)/Git/bin/bash.exe"):
        if Path(cand).exists():
            return cand
    return shutil.which("bash") or "bash"


class LocalSandbox:
    """Runs commands on the host inside ``root``. TEST DOUBLE ONLY: it provides no isolation whatsoever.

    Maps the sandbox paths ``/workspace`` and ``/tmp`` onto host directories so controller code written for the
    real sandbox (junit at ``/tmp/junit.xml``) runs unchanged.
    """

    def __init__(self, root: Path, role: str = "work") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.tmp = self.root.parent / f"{self.root.name}_tmp"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.gitdir = self.root.parent / f"{self.root.name}_git"
        self.id = f"local-{uuid.uuid4().hex[:8]}"
        self.role = role
        self.closed = False
        self.commands: list[str] = []

    def _map(self, s: str) -> str:
        return (
            s.replace("/gitbase", self.gitdir.as_posix())
            .replace("/tmp/", f"{self.tmp.as_posix()}/")
            .replace("/workspace", self.root.as_posix())
        )

    def _host(self, path: str) -> Path:
        mapped = self._map(path if path.startswith("/") else f"/workspace/{path}")
        return Path(mapped)

    def exec(self, cmd: str, timeout_s: int, workdir: str = "/workspace") -> ExecResult:
        self.commands.append(cmd)
        env = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join([self.root.as_posix(), (self.root / "src").as_posix()]),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_ADDOPTS": "",
            "GIT_CEILING_DIRECTORIES": self.root.parent.as_posix(),
        }
        started = time.monotonic()
        try:
            p = subprocess.run(
                [_find_bash(), "-c", self._map(cmd)],
                cwd=self._map(workdir),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
            )
            code, out, err, timed_out = p.returncode, p.stdout, p.stderr, False
        except subprocess.TimeoutExpired as exc:
            code, out, err, timed_out = (
                137,
                (exc.stdout or b"").decode() if isinstance(exc.stdout, bytes) else "",
                "",
                True,
            )
        return ExecResult(
            exit_code=code,
            stdout=out,
            stderr=err,
            duration_ms=int((time.monotonic() - started) * 1000),
            timed_out=timed_out,
        )

    def write_file(self, path: str, content: str, owner: str = "agent") -> None:
        p = self._host(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8", newline="")

    def read_file(self, path: str) -> str:
        with self._host(path).open(encoding="utf-8", newline="") as f:
            return f.read()

    def put_tree(self, tar_bytes: bytes) -> None:
        with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tf:
            tf.extractall(self.root, filter="data")

    def init_base(self) -> None:
        res = self.exec(INIT_BASE_CMD, 60)
        assert res.exit_code == 0, res.stderr

    def diff(self) -> str:
        res = self.exec(DIFF_CMD, 60)
        assert res.exit_code == 0, res.stderr
        return res.stdout

    def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------- local sandbox provider for graph tests
from fixling.config import Config  # noqa: E402
from fixling.events.store import EventStore  # noqa: E402
from fixling.repo.env_setup import EnvResult, baseline_result  # noqa: E402
from fixling.repo.workspace import build_tree_tar  # noqa: E402
from fixling.runs import RunStore  # noqa: E402
from fixling.verify.policy import PolicyGate  # noqa: E402


class LocalProvider:
    """``SandboxProvider`` over ``LocalSandbox``: same tar, ownership and baseline code as production, no Docker."""

    def __init__(self, workdir: Path, cfg: Config, events: EventStore, runs: RunStore) -> None:
        self.workdir, self.cfg, self.events, self.runs = Path(workdir), cfg, events, runs
        self.live: dict[str, LocalSandbox] = {}
        self.created: list[tuple[str, str]] = []  # (run_id, role) for every sandbox ever created
        self._n = 0
        self._lock = threading.Lock()

    def _new(self, run_id: str, role: str, src_dir: Path, extra: dict[str, str] | None) -> LocalSandbox:
        with self._lock:
            self._n += 1
            n = self._n
        sb = LocalSandbox(self.workdir / f"{run_id}-{role}-{n}" / "w", role)
        sb.put_tree(build_tree_tar(src_dir, PolicyGate(self.cfg.policy), extra))
        sb.init_base()
        with self._lock:
            self.live[sb.id] = sb
            self.created.append((run_id, role))
        self.runs.register_sandbox(sb.id, run_id, role)
        return sb

    def prepare_env(self, run_id: str, repo_key: str, src_dir: Path) -> tuple[EnvResult, LocalSandbox | None]:
        started = time.monotonic()
        sb = self._new(run_id, "work", src_dir, None)
        result = baseline_result(sb, self.cfg.budgets, self.events, run_id, "local", [], started)
        if not result.ok:
            sb.close()
            return result, None
        return result, sb

    def create(
        self, run_id: str, role: str, src_dir: Path, repo_key: str, extra_files: dict[str, str] | None = None
    ) -> LocalSandbox:
        return self._new(run_id, role, src_dir, extra_files)

    def get(self, run_id: str, sandbox_id: str) -> LocalSandbox | None:
        sb = self.live.get(sandbox_id)
        return sb if sb is not None and not sb.closed else None

    def close_run(self, run_id: str) -> int:
        mine = [sb for sb in self.live.values() if not sb.closed and self.runs_row(sb.id) == run_id]
        for sb in mine:
            sb.close()
            self.runs.mark_sandbox_removed(sb.id)
        for row in self.runs.live_sandboxes(run_id):  # registered by an earlier process (crash recovery)
            self.runs.mark_sandbox_removed(row["container_id"])
        return len(mine)

    def runs_row(self, sandbox_id: str) -> str | None:
        rows = [r for r in self.runs.live_sandboxes() if r["container_id"] == sandbox_id]
        return rows[0]["run_id"] if rows else None

    def open_count(self) -> int:
        return sum(1 for sb in self.live.values() if not sb.closed)
