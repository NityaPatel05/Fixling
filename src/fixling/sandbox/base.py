"""Sandbox protocol (spec E5). Implementations: ``DockerSandbox`` (production), test doubles in tests/."""

from __future__ import annotations

from typing import Literal, Protocol

from fixling.models import ExecResult

WORKSPACE = "/workspace"
Owner = Literal["agent", "root"]

# Shared by every Sandbox implementation. The base commit lives in GITDIR (a root-owned mount OUTSIDE the
# workspace) so the agent can neither move nor forge it. ``safe.directory=*`` because git runs as root over a tree
# owned by the agent uid; hooks are disabled so repo content can never run code through git.
GITDIR = "/gitbase"
GIT = (
    f"git --git-dir={GITDIR} --work-tree={WORKSPACE} -c 'safe.directory=*' -c core.hooksPath=/dev/null "
    "-c core.autocrlf=false -c user.email=fixling@localhost -c user.name=fixling"
)
INIT_BASE_CMD = f"git --git-dir={GITDIR} init -q && {GIT} add -A && {GIT} commit -qm base && {GIT} tag base"
DIFF_CMD = f"{GIT} add -A -N . && {GIT} diff --binary base"


class Sandbox(Protocol):
    """A locked-down place to run untrusted code. Relative paths resolve against ``/workspace``."""

    id: str
    role: str

    def exec(self, cmd: str, timeout_s: int, workdir: str = WORKSPACE) -> ExecResult: ...

    def write_file(self, path: str, content: str, owner: Owner = "agent") -> None: ...

    def read_file(self, path: str) -> str: ...

    def put_tree(self, tar_bytes: bytes) -> None: ...

    def diff(self) -> str:
        """Binary-safe unified diff of the workspace against the base commit (new files included)."""
        ...

    def close(self) -> None: ...


def resolve_path(path: str) -> str:
    """Absolute path inside the sandbox for a possibly workspace-relative ``path``."""
    return path if path.startswith("/") else f"{WORKSPACE}/{path}"
