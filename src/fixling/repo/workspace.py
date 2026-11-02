"""Trusted workspace handling: fetch a repo, build the sandbox tar with protective ownership (spec E6).

The host copy under ``runs/<id>/src`` is pristine and trusted (used for clean verification and policy base
comparison); the sandbox only ever sees a tar built from it, with no ``.git`` history.
"""

from __future__ import annotations

import base64
import io
import os
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path, PurePosixPath

from fixling.errors import WorkspaceError
from fixling.repo.index import SKIP_DIRS
from fixling.verify.policy import PolicyGate

AGENT_UID = 10001
ROOT_UID = 0
MAX_FILE_BYTES = 1_000_000
PRUNE = SKIP_DIRS | {".git"}


# ---------------------------------------------------------------- fetching
def _git(args: list[str], cwd: Path | None = None, timeout: int = 300, redact: str = "") -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"}
    try:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceError(f"git {args[0]} timed out") from exc
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout).strip()
        raise WorkspaceError(f"git {args[0]} failed: {msg.replace(redact, '[REDACTED]') if redact else msg}")
    return proc.stdout.strip()


def auth_header_args(token: str) -> list[str]:
    """``-c http.extraheader=...`` for one command, so the token never lands in ``.git/config``."""
    if not token:
        return []
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
    return ["-c", f"http.extraheader=AUTHORIZATION: basic {basic}"]


def _rmtree(path: Path) -> None:
    def onerror(func: object, p: str, _exc: object) -> None:
        os.chmod(p, stat.S_IWRITE)
        func(p)  # type: ignore[operator]

    shutil.rmtree(path, onerror=onerror)


def fetch_repo(
    repo: str, dest: Path, token: str = "", *, branch: str | None = None, clone_url: str | None = None
) -> str:
    """Shallow-clone ``repo`` into ``dest`` and return the base commit sha. ``.git`` is removed afterwards."""
    url = clone_url or f"https://github.com/{repo}.git"
    dest = Path(dest)
    if dest.exists():
        _rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = [*auth_header_args(token), "clone", "--depth", "1", "--no-tags", "--single-branch"]
    if branch:
        args += ["--branch", branch]
    _git([*args, url, str(dest)], redact=token)
    sha = _git(["rev-parse", "HEAD"], cwd=dest)
    _rmtree(dest / ".git")
    return sha


def copy_local(src: Path, dest: Path, commit: str | None = None) -> str:
    """Copy a local repo (at ``commit`` via ``git archive`` if given, else the working tree) and return its sha."""
    src, dest = Path(src), Path(dest)
    if dest.exists():
        _rmtree(dest)
    dest.mkdir(parents=True)
    if commit:
        data = subprocess.run(
            ["git", "-C", str(src), "archive", "--format=tar", commit], capture_output=True, check=False
        )
        if data.returncode != 0:
            raise WorkspaceError(f"git archive {commit} failed: {data.stderr.decode(errors='replace')[-300:]}")
        with tarfile.open(fileobj=io.BytesIO(data.stdout)) as tf:
            tf.extractall(dest, filter="data")
        return _git(["rev-parse", commit], cwd=src)
    shutil.copytree(src, dest, dirs_exist_ok=True, ignore=shutil.ignore_patterns(*PRUNE, "*.pyc"))
    try:
        return _git(["rev-parse", "HEAD"], cwd=src)
    except WorkspaceError:
        return "local"


# ---------------------------------------------------------------- tar building
def _tarinfo(
    name: str, uid: int, mode: int, *, directory: bool = False, size: int = 0, mtime: float = 0
) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.uid = info.gid = uid
    info.uname = info.gname = ""
    info.mode = mode
    info.mtime = int(mtime)
    if directory:
        info.type = tarfile.DIRTYPE
    else:
        info.size = size
    return info


def _walk(src: Path) -> tuple[list[str], list[str], dict[str, str]]:
    """(relative dirs, relative files, symlinks rel->target) with noise pruned and unsafe links rejected."""
    root = src.resolve()
    dirs: list[str] = []
    files: list[str] = []
    links: dict[str, str] = {}
    for cur, dnames, fnames in os.walk(src, followlinks=False):
        dnames[:] = sorted(d for d in dnames if d not in PRUNE and not d.endswith(".egg-info"))
        rel_dir = Path(cur).relative_to(src).as_posix()
        if rel_dir != ".":
            dirs.append(rel_dir)
        for d in list(dnames):
            full = Path(cur) / d
            if full.is_symlink():
                dnames.remove(d)
                _check_link(full, root)
                links[(Path(rel_dir) / d).as_posix() if rel_dir != "." else d] = os.readlink(full)
        for f in sorted(fnames):
            full = Path(cur) / f
            rel = (Path(rel_dir) / f).as_posix() if rel_dir != "." else f
            if full.is_symlink():
                _check_link(full, root)
                links[rel] = os.readlink(full)
            elif full.stat().st_size <= MAX_FILE_BYTES:
                files.append(rel)
    return dirs, files, links


def _check_link(link: Path, root: Path) -> None:
    target = Path(os.path.realpath(link))
    if not target.is_relative_to(root):
        raise WorkspaceError(f"symlink {link.name} points outside the repository ({os.readlink(link)})")


def _ancestors(path: str) -> list[str]:
    parts = PurePosixPath(path).parts[:-1]
    return ["/".join(parts[: i + 1]) for i in range(len(parts))]


def build_tree_tar(src_dir: Path, gate: PolicyGate, extra_files: dict[str, str] | None = None) -> bytes:
    """Tar of ``src_dir`` (no ``.git``) with ownership that makes protected tests hard to tamper with.

    * protected files: root, 0444 (exec bit kept); every ancestor directory of a protected file: root, 0555
      (a writable parent would let the agent delete or replace a read-only file)
    * the generated-test directory: created agent-owned inside its (root-owned) parent
    * everything else: agent (uid 10001); edits to existing files work, new top-level entries cannot be added
    * ``extra_files`` (path -> text) are written agent-owned and must live under the generated-test directory
    """
    src_dir = Path(src_dir)
    gen = gate.cfg.generated_test_dir.strip("/")
    extra = extra_files or {}
    for p in extra:
        if not (p == gen or p.startswith(gen + "/")):
            raise WorkspaceError(f"extra file {p} is outside {gen}/")
    dirs, files, links = _walk(src_dir)
    protected = {f for f in files if gate.is_protected(f)} | {k for k in links if gate.is_protected(k)}
    root_dirs: set[str] = set()
    for f in protected:
        root_dirs.update(_ancestors(f))
    root_dirs.update(_ancestors(gen + "/x"))  # parents of the generated dir, e.g. tests/
    root_dirs.discard(gen)
    all_dirs = sorted(set(dirs) | root_dirs | {gen} | {a for p in extra for a in _ancestors(p)})

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for d in all_dirs:
            if d in root_dirs:
                tf.addfile(_tarinfo(d, ROOT_UID, 0o555, directory=True))
            else:
                tf.addfile(_tarinfo(d, AGENT_UID, 0o755, directory=True))
        for f in files:
            full = src_dir / f
            st = full.stat()
            executable = bool(st.st_mode & stat.S_IXUSR)
            is_protected = f in protected
            uid = ROOT_UID if is_protected else AGENT_UID
            mode = (0o555 if executable else 0o444) if is_protected else (0o755 if executable else 0o644)
            with full.open("rb") as fh:
                tf.addfile(_tarinfo(f, uid, mode, size=st.st_size, mtime=st.st_mtime), fh)
        for link, target in links.items():
            info = _tarinfo(link, ROOT_UID if link in protected else AGENT_UID, 0o777)
            info.type, info.linkname, info.size = tarfile.SYMTYPE, target, 0
            tf.addfile(info)
        for path, text in extra.items():
            data = text.encode("utf-8")
            tf.addfile(_tarinfo(path, AGENT_UID, 0o644, size=len(data)), io.BytesIO(data))
    return buf.getvalue()
