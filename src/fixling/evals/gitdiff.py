"""Diff two directory trees with git (real ``git diff`` output, so patches apply with ``git apply``)."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

IGNORE = shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc", ".git", ".mypy_cache", ".ruff_cache")
GIT = [
    "git",
    "-c",
    "core.autocrlf=false",
    "-c",
    "core.safecrlf=false",
    "-c",
    "user.email=eval@fixling",
    "-c",
    "user.name=eval",
]


def _run(args: list[str], cwd: Path) -> str:
    # Decode bytes ourselves: text mode would translate CRLF in file content and corrupt the patch.
    return subprocess.run([*GIT, *args], cwd=cwd, capture_output=True, check=True).stdout.decode("utf-8")


def diff_trees(before: Path, after: Path) -> str:
    """Unified diff that turns ``before`` into ``after`` (new files, deletions and renames included)."""
    with tempfile.TemporaryDirectory(prefix="fixling-diff-") as tmp:
        work = Path(tmp) / "w"
        shutil.copytree(before, work, ignore=IGNORE)
        _run(["init", "-q"], work)
        _run(["add", "-A"], work)
        _run(["commit", "-qm", "before", "--allow-empty"], work)
        for child in work.iterdir():
            if child.name == ".git":
                continue
            shutil.rmtree(child) if child.is_dir() else child.unlink()
        shutil.copytree(after, work, dirs_exist_ok=True, ignore=IGNORE)
        _run(["add", "-A"], work)
        return _run(["diff", "--cached", "--no-color", "--binary", "-M"], work)
