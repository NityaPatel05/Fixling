"""Search/replace edit application with exact -> whitespace-normalised -> fuzzy matching (spec E9).

Small models rarely reproduce code byte-for-byte, so matching is forgiving but never ambiguous: an edit
that could apply in more than one place is rejected with advice the next prompt can use to recover.
"""

from __future__ import annotations

import ast
import difflib
import textwrap
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Protocol

from fixling.models import Edit

FUZZY_MIN_RATIO = 0.90
FUZZY_MARGIN = 0.05
SNIPPET_MAX_LINES = 14


class FileAccess(Protocol):
    def read_file(self, path: str) -> str: ...

    def write_file(self, path: str, content: str) -> None: ...


class DirFiles:
    """``FileAccess`` over a host directory (unit tests, eval grading, trusted-side patching)."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _p(self, path: str) -> Path:
        full = (self.root / path).resolve()
        if not full.is_relative_to(self.root.resolve()):
            raise ValueError(f"path escapes workspace: {path}")
        return full

    def read_file(self, path: str) -> str:
        with self._p(path).open(encoding="utf-8", newline="") as f:  # newline="": keep CRLF visible
            return f.read()

    def write_file(self, path: str, content: str) -> None:
        p = self._p(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8", newline="")


@dataclass
class ApplyResult:
    patched_files: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    applied: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors and self.applied > 0


class _MatchError(Exception):
    pass


def normalize_path(path: str) -> str | None:
    """Relative POSIX path inside the workspace, or ``None`` if it is absolute / escapes."""
    p = path.strip().replace("\\", "/")
    for prefix in ("/workspace/", "./"):
        while p.startswith(prefix):
            p = p[len(prefix) :]
    pp = PurePosixPath(p)
    if not p or pp.is_absolute() or ".." in pp.parts or (len(p) > 1 and p[1] == ":"):
        return None
    return str(pp)


def _norm_lines(lines: Iterable[str]) -> str:
    return textwrap.dedent("\n".join(ln.rstrip() for ln in lines)).strip("\n")


def _common_indent(lines: Iterable[str]) -> str:
    indents = [ln[: len(ln) - len(ln.lstrip())] for ln in lines if ln.strip()]
    if not indents:
        return ""
    return min(indents, key=len)


def _reindent(replace: str, indent: str) -> str:
    body = textwrap.dedent(replace.strip("\n"))
    return "\n".join((indent + ln) if ln.strip() else ln for ln in body.split("\n"))


def _snippet(lines: list[str], start: int, end: int) -> str:
    end = min(end, start + SNIPPET_MAX_LINES)
    return "\n".join(f"{i + 1}: {lines[i]}" for i in range(start, end))


def _windows(n_content: int, n_search: int) -> Iterable[tuple[int, int]]:
    for size in sorted({max(1, n_search + d) for d in (0, -1, 1, -2, 2)}, key=lambda s: abs(s - n_search)):
        for start in range(0, n_content - size + 1):
            yield start, start + size


def _best_fuzzy(content_lines: list[str], search_lines: list[str]) -> tuple[float, tuple[int, int] | None, float]:
    """Return (best ratio, best window, best ratio among windows not overlapping the best)."""
    target = _norm_lines(search_lines)
    scored: list[tuple[float, int, int]] = []
    matcher = difflib.SequenceMatcher(autojunk=False)
    matcher.set_seq2(target)
    for start, end in _windows(len(content_lines), len(search_lines)):
        cand = _norm_lines(content_lines[start:end])
        matcher.set_seq1(cand)
        if matcher.real_quick_ratio() < 0.5 or matcher.quick_ratio() < 0.5:
            continue
        scored.append((matcher.ratio(), start, end))
    if not scored:
        return 0.0, None, 0.0
    scored.sort(reverse=True)
    best, bs, be = scored[0]
    second = max((r for r, s, e in scored[1:] if e <= bs or s >= be), default=0.0)
    return best, (bs, be), second


def _apply_one(content: str, edit: Edit, display_path: str) -> str:
    """Apply a single edit to ``content`` (LF-normalised). Raises ``_MatchError`` with recovery advice."""
    search, replace = edit.search, edit.replace
    if not search.strip():
        raise _MatchError(f"Empty SEARCH block for {display_path}. Copy the exact lines you want to change.")
    if search.strip() == replace.strip() and search == replace:
        raise _MatchError(f"SEARCH and REPLACE are identical for {display_path}; nothing would change.")

    # 1. exact. Leading/trailing newlines around a SEARCH block are formatting noise from the model, so the
    #    trimmed block is tried first (and REPLACE is trimmed on the same sides) to keep line structure intact.
    trimmed_replace = replace
    if search.startswith("\n"):
        trimmed_replace = trimmed_replace.lstrip("\n")
    if search.endswith("\n"):
        trimmed_replace = trimmed_replace.rstrip("\n")
    tried: list[tuple[str, str]] = []
    for needle, repl in ((search.strip("\n"), trimmed_replace), (search, replace)):
        if needle.strip() and (needle, repl) not in tried:
            tried.append((needle, repl))
    ambiguous = 0
    for needle, repl in tried:
        count = content.count(needle)
        if count == 1:
            return _replace_once(content, needle, repl)
        ambiguous = max(ambiguous, count)
    if ambiguous > 1:
        raise _MatchError(
            f"SEARCH block matches {ambiguous} places in {display_path}. Include more surrounding lines "
            "so it identifies exactly one location."
        )

    content_lines = content.split("\n")
    search_lines = search.strip("\n").split("\n")
    n = len(search_lines)

    # 2. whitespace-normalised (indentation / trailing spaces)
    want = _norm_lines(search_lines)
    hits = [i for i in range(len(content_lines) - n + 1) if _norm_lines(content_lines[i : i + n]) == want]
    if len(hits) > 1:
        raise _MatchError(
            f"SEARCH block matches {len(hits)} places in {display_path} (ignoring indentation). "
            "Include more surrounding lines."
        )
    if len(hits) == 1:
        return _splice(content_lines, hits[0], hits[0] + n, replace)

    # 3. fuzzy, only if unique
    best, window, second = _best_fuzzy(content_lines, search_lines)
    if window and best >= FUZZY_MIN_RATIO and second < best - FUZZY_MARGIN:
        return _splice(content_lines, window[0], window[1], replace)

    hint = ""
    if window and best >= 0.5:
        hint = (
            f" Closest match (ratio {best:.2f}) at lines {window[0] + 1}-{window[1]}:\n"
            f"{_snippet(content_lines, *window)}\n"
        )
        if best >= FUZZY_MIN_RATIO:
            hint += "It is ambiguous (another region is nearly as similar); include more distinguishing lines. "
    raise _MatchError(
        f"SEARCH block not found in {display_path}.{hint} Copy the search text exactly from the code shown."
    )


def _replace_once(content: str, needle: str, repl: str) -> str:
    idx = content.index(needle)
    before, after = content[:idx], content[idx + len(needle) :]
    if not repl.strip() and (before == "" or before.endswith("\n")) and after.startswith("\n"):
        after = after[1:]  # deleting whole lines: don't leave an empty line behind
        repl = ""
    return before + repl + after


def _splice(lines: list[str], start: int, end: int, replace: str) -> str:
    indent = _common_indent(lines[start:end])
    new_block = _reindent(replace, indent) if replace.strip() else ""
    out = lines[:start] + ([new_block] if replace.strip() else []) + lines[end:]
    return "\n".join(out)


def apply_edits(files: FileAccess, edits: list[Edit], allowed_paths: Collection[str]) -> ApplyResult:
    """Apply ``edits`` through ``files``; only paths in ``allowed_paths`` may be touched.

    Each patched ``.py`` file must still parse; a file that does not is left untouched and reported.
    """
    result = ApplyResult()
    allowed = {p for p in (normalize_path(a) for a in allowed_paths) if p}
    contents: dict[str, str] = {}
    crlf: dict[str, bool] = {}
    counts: dict[str, int] = {}

    for edit in edits:
        path = normalize_path(edit.path)
        if path is None:
            result.errors.append(f"Invalid path {edit.path!r}: use a relative path inside the repository.")
            continue
        if path not in allowed:
            result.errors.append(
                f"Edits to {path} are not allowed. Only edit these files: {', '.join(sorted(allowed)) or '(none)'}."
            )
            continue
        if path not in contents:
            try:
                raw = files.read_file(path)
            except (OSError, ValueError):
                result.errors.append(f"File {path} does not exist.")
                continue
            crlf[path] = "\r\n" in raw
            contents[path] = raw.replace("\r\n", "\n")
        try:
            contents[path] = _apply_one(contents[path], edit, path)
        except _MatchError as exc:
            result.errors.append(str(exc))
            continue
        counts[path] = counts.get(path, 0) + 1

    for path, text in contents.items():
        if path not in counts:
            continue
        if path.endswith(".py"):
            try:
                ast.parse(text)
            except SyntaxError as exc:
                result.errors.append(
                    f"The edited {path} has a syntax error at line {exc.lineno}: {exc.msg}. "
                    "The file was left unchanged; fix the indentation/brackets in your REPLACE block."
                )
                continue
        out = text.replace("\n", "\r\n") if crlf[path] else text
        files.write_file(path, out)
        result.patched_files.append(path)
        result.applied += counts[path]
    return result
