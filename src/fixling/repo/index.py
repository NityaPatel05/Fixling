"""AST symbol index and repo map (spec E8)."""

from __future__ import annotations

import ast
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from fixling.models import Chunk

SKIP_DIRS = {
    ".git",
    ".hg",
    ".venv",
    "venv",
    "env",
    ".tox",
    ".nox",
    "build",
    "dist",
    "node_modules",
    "__pycache__",
    "site-packages",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}
MAX_FILE_BYTES = 1_000_000
MODULE_CHUNK_LINES = 40


def is_test_path(path: str) -> bool:
    p = PurePosixPath(path)
    name = p.name
    return (
        any(part in ("tests", "test") for part in p.parts[:-1])
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name == "conftest.py"
    )


def iter_python_files(tree_dir: Path) -> Iterable[Path]:
    for p in sorted(tree_dir.rglob("*.py")):
        rel = p.relative_to(tree_dir)
        if any(part in SKIP_DIRS or part.endswith(".egg-info") for part in rel.parts[:-1]):
            continue
        try:
            if p.is_symlink() or p.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        yield p


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    return f"{prefix} {node.name}({ast.unparse(node.args)}){ret}"


def _start_line(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> int:
    return min([node.lineno, *(d.lineno for d in node.decorator_list)])


def _chunk_source(lines: list[str], start: int, end: int) -> str:
    return "\n".join(lines[start - 1 : end])


def _walk_body(body: list[ast.stmt], rel: str, lines: list[str], prefix: str, in_class: bool, out: list[Chunk]) -> None:
    for node in body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            start, end = _start_line(node), node.end_lineno or node.lineno
            out.append(
                Chunk(
                    path=rel,
                    qualname=f"{rel}::{prefix}{node.name}",
                    kind="method" if in_class else "function",
                    start=start,
                    end=end,
                    signature=_signature(node),
                    docstring=ast.get_docstring(node),
                    source=_chunk_source(lines, start, end),
                )
            )
        elif isinstance(node, ast.ClassDef):
            start, end = _start_line(node), node.end_lineno or node.lineno
            bases = ", ".join(ast.unparse(b) for b in node.bases)
            sig = f"class {node.name}({bases})" if bases else f"class {node.name}"
            methods = [
                f"{n.name}({ast.unparse(n.args)})"
                for n in node.body
                if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
            ]
            doc = ast.get_docstring(node)
            summary = (
                sig
                + (f'\n    """{doc}"""' if doc else "")
                + ("\n    methods: " + "; ".join(methods) if methods else "")
            )
            out.append(
                Chunk(
                    path=rel,
                    qualname=f"{rel}::{prefix}{node.name}",
                    kind="class",
                    start=start,
                    end=end,
                    signature=sig,
                    docstring=doc,
                    source=summary,
                )
            )
            _walk_body(node.body, rel, lines, f"{prefix}{node.name}.", True, out)


def index_file(rel: str, text: str) -> list[Chunk]:
    lines = text.splitlines()
    chunks = [
        Chunk(
            path=rel,
            qualname=f"{rel}::<module>",
            kind="module",
            start=1,
            end=min(len(lines), MODULE_CHUNK_LINES),
            signature=rel,
            docstring=None,
            source="\n".join(lines[:MODULE_CHUNK_LINES]),
        )
    ]
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return chunks
    chunks[0].docstring = ast.get_docstring(tree)
    _walk_body(tree.body, rel, lines, "", False, chunks)
    return chunks


def build_index(tree_dir: Path) -> list[Chunk]:
    """Chunks for every Python file under ``tree_dir``: one module chunk, class summaries, functions/methods."""
    tree_dir = Path(tree_dir)
    chunks: list[Chunk] = []
    for p in iter_python_files(tree_dir):
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        chunks.extend(index_file(p.relative_to(tree_dir).as_posix(), text))
    return chunks


def _display(chunk: Chunk) -> str:
    """``def name(args)`` for functions, ``name(args)`` for methods (shown under their class)."""
    sig = chunk.signature
    return sig.removeprefix("async ").removeprefix("def ") if chunk.kind == "method" else sig


def repo_map(chunks: list[Chunk], files: list[str], budget_tokens: int = 2500) -> str:
    """Compact signature listing per file, trimmed to ``budget_tokens`` (~3.5 chars per token).

    ::

        src/pkg/cart.py
          class Cart:  add(self, item, qty)  total(self)
          def apply_discount(total, code)
    """
    by_file: dict[str, list[Chunk]] = defaultdict(list)
    for c in chunks:
        by_file[c.path].append(c)
    budget_chars = int(budget_tokens * 3.5)
    out: list[str] = []
    used = 0
    for path in files:
        entries = by_file.get(path, [])
        block = [path]
        classes = [c for c in entries if c.kind == "class"]
        for cls in classes:
            methods = [c for c in entries if c.kind == "method" and c.qualname.startswith(cls.qualname + ".")]
            line = f"  {cls.signature}:" + "".join(f"  {_display(m)}" for m in methods)
            block.append(line)
        for fn in (c for c in entries if c.kind == "function"):
            block.append(f"  {fn.signature}")
        text = "\n".join(block)
        if used + len(text) > budget_chars:
            if not out:  # always give at least a truncated first file
                out.append(text[:budget_chars])
            break
        out.append(text)
        used += len(text) + 1
    return "\n".join(out)
