"""Run a repository's pytest suite on the host (trusted fixtures only) to build and validate evaluation tasks.

Task construction runs OUR OWN demo repositories, so the host interpreter is fine here; nothing from an issue or a
model ever reaches this code path (agent runs always use the Docker sandbox).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from fixling.models import TestRun
from fixling.verify.runner import parse_junit


def run_suite(tree: Path, test_ids: list[str] | None = None, timeout: int = 300) -> TestRun:
    """Run pytest in ``tree`` with ``src/`` (or the tree itself) on the path and return the parsed junit result."""
    with tempfile.TemporaryDirectory(prefix="fixling-suite-") as tmp:
        junit = Path(tmp) / "junit.xml"
        paths = [str(tree / "src"), str(tree)]
        env = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join(paths),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_ADDOPTS": "",
        }
        cmd = [sys.executable, "-m", "pytest", "-q", "--color=no", "-p", "no:cacheprovider", "-p", "no:randomly"]
        cmd += ["-o", "addopts=", f"--junitxml={junit}", *(test_ids or [])]
        proc = subprocess.run(
            cmd,
            cwd=tree,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        xml = junit.read_text(encoding="utf-8") if junit.exists() else ""
        return parse_junit(xml, " ".join(cmd), proc.returncode, 0, proc.stdout[-2000:])
