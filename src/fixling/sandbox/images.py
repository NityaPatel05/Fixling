"""Sandbox images: the shared base image and per-repo environment images (spec E5).

Network is used only here, at build time. Environment images bake in the repo's *dependencies* (not the repo
itself); at run time the code is imported through ``PYTHONPATH=/workspace:/workspace/src``.
"""

from __future__ import annotations

import hashlib
import io
import re
import tarfile
import tomllib
from pathlib import Path
from typing import Any

import docker
from docker.errors import BuildError, ImageNotFound

from fixling.errors import SandboxError

BASE_TAG = "fixling-base:latest"
DOCKER_DIR = Path(__file__).parent / "docker"
EXTRA_GROUPS = ("test", "tests", "testing", "dev", "develop")
# Provided by the base image; a repo pinning them could break the controller's test runner.
BASE_OWNED = {"pytest-randomly", "pytest-timeout", "junitparser", "mutmut", "coverage"}
_REQ_FILE = re.compile(r"(^|.*[-_])requirements([-_].*)?\.txt$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")


def get_client() -> docker.DockerClient:
    return docker.from_env()


def ensure_base(client: docker.DockerClient, *, force: bool = False) -> str:
    """Build ``fixling-base:latest`` if it is missing."""
    if not force:
        try:
            client.images.get(BASE_TAG)
            return BASE_TAG
        except ImageNotFound:
            pass
    try:
        client.images.build(path=str(DOCKER_DIR), dockerfile="Dockerfile.base", tag=BASE_TAG, rm=True)
    except BuildError as exc:
        raise SandboxError(f"building {BASE_TAG} failed: {_log_tail(exc)}") from exc
    return BASE_TAG


def _clean_requirement(line: str) -> str | None:
    line = line.split(" #", 1)[0].strip()
    if not line or line.startswith(("#", "-", ".", "/", "git+", "http", "file:")) or "@ file:" in line:
        return None  # includes, editable/local paths and VCS URLs are not installable in an offline-ish build
    m = _NAME.match(line)
    if m and m.group(0).lower().replace("_", "-") in BASE_OWNED:
        return None
    return line


def gather_requirements(tree_dir: Path) -> list[str]:
    """Installable dependency specs from pyproject.toml (project, extras, dependency groups) and requirements files."""
    found: list[str] = []
    pyproject = tree_dir / "pyproject.toml"
    if pyproject.exists():
        try:
            data: dict[str, Any] = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError):
            data = {}
        project = data.get("project", {})
        found.extend(project.get("dependencies", []) or [])
        extras = project.get("optional-dependencies", {}) or {}
        for group in EXTRA_GROUPS:
            found.extend(extras.get(group, []) or [])
        for group in EXTRA_GROUPS:
            for item in (data.get("dependency-groups", {}) or {}).get(group, []) or []:
                if isinstance(item, str):  # include-group tables are skipped
                    found.append(item)
    candidates = [p for p in sorted(tree_dir.glob("*.txt")) if _REQ_FILE.match(p.name)]
    req_dir = tree_dir / "requirements"
    if req_dir.is_dir():
        candidates += sorted(req_dir.glob("*.txt"))
    for path in candidates:
        try:
            found.extend(path.read_text(encoding="utf-8").splitlines())
        except (OSError, UnicodeDecodeError):
            continue
    cleaned: list[str] = []
    for raw in found:
        req = _clean_requirement(str(raw))
        if req and req not in cleaned:
            cleaned.append(req)
    return cleaned


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9_.-]+", "-", text.lower()).strip("-") or "repo"


def env_tag(repo_key: str, requirements: list[str], base_id: str) -> str:
    digest = hashlib.sha256(("\n".join(requirements) + "|" + base_id).encode("utf-8")).hexdigest()[:12]
    return f"fixling-env-{_slug(repo_key)}-{digest}"


ENV_DOCKERFILE = """FROM {base}
USER root
COPY reqs.txt /tmp/reqs.txt
RUN pip install --no-cache-dir -r /tmp/reqs.txt \\
 || (echo "bulk install failed; installing one by one" \\
     && while read -r r; do [ -n "$r" ] && (pip install --no-cache-dir "$r" || echo "SKIPPED: $r"); done \\
        < /tmp/reqs.txt)
USER 10001:10001
"""


def _build_context(requirements: list[str], base: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, text in (
            ("Dockerfile", ENV_DOCKERFILE.format(base=base)),
            ("reqs.txt", "\n".join(requirements) + "\n"),
        ):
            data = text.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def ensure_env_image(client: docker.DockerClient, repo_key: str, tree_dir: Path) -> tuple[str, list[str]]:
    """Return ``(image tag, requirements)``; builds (with network) only when the tag is missing."""
    base = ensure_base(client)
    base_id = client.images.get(base).id or base
    reqs = gather_requirements(tree_dir)
    tag = env_tag(repo_key, reqs, base_id)
    try:
        client.images.get(tag)
        return tag, reqs
    except ImageNotFound:
        pass
    try:
        client.images.build(
            fileobj=io.BytesIO(_build_context(reqs, base)), custom_context=True, tag=tag, rm=True, forcerm=True
        )
    except BuildError as exc:
        raise SandboxError(f"building environment image {tag} failed: {_log_tail(exc)}") from exc
    return tag, reqs


def _log_tail(exc: BuildError) -> str:
    lines = [str(chunk.get("stream", "")).strip() for chunk in (exc.build_log or []) if isinstance(chunk, dict)]
    return " | ".join(line for line in lines if line)[-800:] or str(exc)
