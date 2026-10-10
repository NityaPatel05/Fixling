"""Small shared helpers for graph nodes."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any

from fixling.graph.deps import Deps
from fixling.llm.render import repo_file, untrusted_block

LAYOUT_ROOTS = ("src", "lib")


def spec_text(spec: dict[str, Any]) -> str:
    """The IssueSpec as short plain text. It came from untrusted issue text, so it stays inside the tag."""
    lines = [f"Summary: {spec.get('summary', '')}"]
    for key, label in (("expected_behavior", "Expected"), ("actual_behavior", "Actual")):
        if spec.get(key):
            lines.append(f"{label}: {spec[key]}")
    hints = [h for h in spec.get("repro_hints", []) if h]
    if hints:
        lines.append("Examples from the issue: " + " | ".join(h.replace("\n", " ") for h in hints[:3]))
    crit = [c for c in spec.get("acceptance_criteria", []) if c]
    if crit:
        lines.append("Acceptance: " + " | ".join(crit[:3]))
    return untrusted_block("\n".join(lines))


def module_name(path: str) -> str:
    """``src/tiny/pricing.py`` -> ``tiny.pricing`` (a ``src``/``lib`` layout root is dropped)."""
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if len(parts) > 1 and parts[0] in LAYOUT_ROOTS:
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def import_lines(targets: list[dict[str, Any]]) -> list[str]:
    """``from tiny.pricing import bulk_price`` for each target (methods import their class)."""
    by_module: dict[str, list[str]] = {}
    for t in targets:
        dotted = t["qualname"].split("::", 1)[1]
        top = dotted.split(".")[0]
        names = by_module.setdefault(module_name(t["path"]), [])
        if top not in names:
            names.append(top)
    return [f"from {mod} import {', '.join(names)}" for mod, names in by_module.items() if mod]


def target_views(targets: list[dict[str, Any]]) -> list[str]:
    return [repo_file(t["path"], t["source"], t["start"]) for t in targets]


def qual_to_test_id(qualname: str) -> str:
    """``tests/test_a.py::TestX.test_y`` -> ``tests/test_a.py::TestX::test_y`` (pytest node id)."""
    path, _, dotted = qualname.partition("::")
    return "::".join([path, *dotted.split(".")]) if dotted else path


def short(text: str, limit: int = 1500) -> str:
    return text if len(text) <= limit else text[: limit // 2] + "\n...\n" + text[-limit // 2 :]


def failure_summary(details: dict[str, str], ids: list[str], limit: int = 1500) -> str:
    chunks = [f"{i}:\n{details.get(i, '').strip()}" for i in ids if details.get(i)]
    return short("\n\n".join(chunks), limit)


def run_dir(deps: Deps, run_id: str) -> Path:
    d = deps.cfg.run_dir(run_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_run_file(deps: Deps, run_id: str, name: str, text: str) -> str:
    """Write ``runs/<id>/<name>`` (LF endings) and return the path relative to the run directory."""
    path = run_dir(deps, run_id) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")
    return name


def read_run_file(deps: Deps, run_id: str, name: str) -> str:
    return (run_dir(deps, run_id) / name).read_text(encoding="utf-8")


def repro_test_path(issue_number: int | None) -> str:
    return f"tests/fixling/test_issue_{issue_number if issue_number is not None else 0}.py"


_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_name(text: str) -> str:
    return _SAFE.sub("-", text).strip("-") or "x"


def combine_patches(*patches: str | None) -> str:
    """Concatenate git diffs that touch different files into one valid patch."""
    return "".join(p if p.endswith("\n") else p + "\n" for p in patches if p and p.strip())
