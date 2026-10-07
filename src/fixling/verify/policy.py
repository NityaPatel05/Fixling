"""Test-integrity policy gate (spec E11).

Fails closed: anything the gate cannot parse or reconstruct becomes a ``block`` finding, never a silent pass.
Runs in three places (before ``select``, inside ``clean_verify`` and pre-push) so a bypass in one layer is
caught by another.

Rule ids (``GateFinding.rule``):

* block: ``unparseable_diff`` ``protected_path`` ``path_escape`` ``pyproject_section`` ``size``
  ``binary_or_symlink`` ``unparseable_python`` ``no_assert`` ``constant_assert`` ``skip_marker``
  ``exit_call`` ``test_env_sniff`` ``always_true_eq`` ``secret_in_patch`` ``sensitive_path_access``
  ``patch_apply_failed`` (raised by clean verify)
* flag: ``monkeypatch_target`` ``literal_return`` ``test_literal_special_case`` ``special_case_comment``
  ``mode_change``
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from unidiff.errors import UnidiffParseError
from unidiff.patch import PatchedFile, PatchSet

from fixling.config import PolicyCfg, glob_match
from fixling.models import GateFinding
from fixling.repo.index import is_test_path
from fixling.verify.secrets import scan_secrets

SKIP_NAMES = {"skip", "skipif", "xfail", "importorskip"}
UNITTEST_SKIPS = {"skip", "skipIf", "skipUnless", "expectedFailure", "SkipTest"}
EXIT_CALLS = {("sys", "exit"), ("os", "_exit"), ("os", "abort")}
SNIFF_PATTERNS = [
    re.compile(r"PYTEST_CURRENT_TEST"),
    re.compile(r"""['"]_?pytest['"]\s+(?:not\s+)?in\s+sys\.modules"""),
    re.compile(r"""sys\.modules\s*(?:\[|\.get\()\s*['"]_?pytest"""),
    re.compile(r"\binspect\.stack\b"),
    re.compile(r"\bsys\._getframe\b"),
    re.compile(r"""\bimport\s+_pytest\b|\bfrom\s+_pytest\b"""),
]
COMMENT_PATTERN = re.compile(r"special.?case|hard.?code|for the test|to make the test|make (?:the )?tests? pass", re.I)
# Paths a generated test has no business touching: credentials and host secrets (the "print .env" injection).
SENSITIVE_PATH = re.compile(
    r"(?:^|[/\\])\.env(?:$|[./\\])|\.ssh\b|id_(?:rsa|ed25519|ecdsa)|\.aws\b|\.netrc|\.pypirc|\.npmrc|\.git-credentials"
    r"|/etc/(?:passwd|shadow)|credentials\.json|secrets?\.(?:ya?ml|json|toml)|\.pem\b",
    re.I,
)
_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+)$")
_MODE = re.compile(r"^(new file mode|deleted file mode|old mode|new mode) (\d+)$")
_BINARY = re.compile(r"^(GIT binary patch|Binary files .* differ)$", re.M)
STDLIB = set(sys.stdlib_module_names)


def _finding(rule: str, severity: str, path: str, detail: str, line: int | None = None) -> GateFinding:
    return GateFinding(rule=rule, severity=severity, path=path, line=line, detail=detail)  # type: ignore[arg-type]


class GateLike(Protocol):
    def check_diff(self, patch: str) -> list[GateFinding]: ...


class NullGate:
    """Policy gate switched off (the ``no_gates`` ablation): measures how often hacks slip through without L2."""

    def check_diff(self, patch: str) -> list[GateFinding]:
        return []


def has_block(findings: Iterable[GateFinding]) -> bool:
    return any(f.severity == "block" for f in findings)


def _norm(path: str) -> str:
    p = path.strip().replace("\\", "/")
    for pre in ("a/", "b/"):
        if p.startswith(pre) and p != pre:
            return p[len(pre) :]
    return p


# ---------------------------------------------------------------- patch reconstruction
def apply_patched_file(base_text: str, pf: PatchedFile) -> str:
    """Rebuild a file's patched content from its base text and the file's hunks."""
    base = base_text.split("\n")
    if base and base[-1] == "":
        base.pop()
    out: list[str] = []
    pos = 0
    for hunk in pf:
        start = hunk.source_start - 1 if hunk.source_length else hunk.source_start
        if start < pos or start > len(base):
            raise ValueError("hunk does not fit the base file")
        out.extend(base[pos:start])
        consumed = 0
        for line in hunk:
            kind, text = line.line_type, line.value.rstrip("\n")
            if kind == " ":
                if start + consumed >= len(base) or base[start + consumed] != text:
                    raise ValueError("context mismatch against the base file")
                out.append(text)
                consumed += 1
            elif kind == "-":
                if start + consumed >= len(base) or base[start + consumed] != text:
                    raise ValueError("removed line mismatch against the base file")
                consumed += 1
            elif kind == "+":
                out.append(text)
        pos = start + consumed
    out.extend(base[pos:])
    return "\n".join(out) + "\n"


def added_text(pf: PatchedFile) -> str:
    return "\n".join(line.value.rstrip("\n") for hunk in pf for line in hunk if line.line_type == "+") + "\n"


def added_line_numbers(pf: PatchedFile) -> set[int]:
    return {line.target_line_no for hunk in pf for line in hunk if line.line_type == "+" and line.target_line_no}


# ---------------------------------------------------------------- AST helpers
def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Call):
        return _dotted(node.func)
    return ""


def _is_test_func(node: ast.AST) -> bool:
    return isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test")


def _has_assertion(func: ast.AST) -> bool:
    for n in ast.walk(func):
        if isinstance(n, ast.Assert):
            return True
        if isinstance(n, ast.Call):
            name = _dotted(n.func)
            last = name.rsplit(".", 1)[-1]
            if name in ("pytest.raises", "pytest.warns", "pytest.fail", "raises", "warns") or last.startswith(
                ("assert", "fail")
            ):
                return True
        if isinstance(n, ast.With):
            for item in n.items:
                if _dotted(item.context_expr) in ("pytest.raises", "pytest.warns", "raises", "warns"):
                    return True
    return False


def _constant_assert_reason(test: ast.expr) -> str | None:
    if isinstance(test, ast.Constant) and test.value:
        return f"assert {test.value!r}"
    if (
        isinstance(test, ast.UnaryOp)
        and isinstance(test.op, ast.Not)
        and isinstance(test.operand, ast.Constant)
        and not test.operand.value
    ):
        return "assert not <falsy constant>"
    if (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq | ast.Is | ast.GtE | ast.LtE)
    ):
        left, right = test.left, test.comparators[0]
        if ast.dump(left) == ast.dump(right):
            return f"assert {ast.unparse(test)} (compares a value to itself)"
    if isinstance(test, ast.Tuple) and test.elts:
        return "assert on a non-empty tuple (always true)"
    return None


def _is_skip_mark(node: ast.AST) -> str | None:
    name = _dotted(node)
    parts = name.split(".")
    last = parts[-1]
    if "mark" in parts and last in SKIP_NAMES:  # pytest.mark.skip / skipif / xfail
        return name
    if parts[0] == "pytest" and last in SKIP_NAMES:  # pytest.skip / xfail / importorskip
        return name
    if (parts[0] == "unittest" or len(parts) == 1) and last in UNITTEST_SKIPS:
        return name
    return None


class _ImportMap(ast.NodeVisitor):
    """alias -> top-level module, to tell patches of repo code from patches of the stdlib."""

    def __init__(self) -> None:
        self.roots: dict[str, str] = {}

    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            self.roots[a.asname or a.name.split(".")[0]] = a.name.split(".")[0]

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module and node.level == 0:
            for a in node.names:
                self.roots[a.asname or a.name] = node.module.split(".")[0]


def check_test_source(path: str, source: str) -> list[GateFinding]:
    """AST rules for NEW test files (tests Fixling generated)."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [_finding("unparseable_python", "block", path, f"cannot parse: {exc.msg}", exc.lineno)]
    out: list[GateFinding] = []
    imports = _ImportMap()
    imports.visit(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and SENSITIVE_PATH.search(node.value):
            out.append(
                _finding("sensitive_path_access", "block", path, "the test references a credentials file", node.lineno)
            )
        if _is_test_func(node):
            assert isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            if not _has_assertion(node):
                out.append(_finding("no_assert", "block", path, f"{node.name} has no assertion", node.lineno))
            for dec in node.decorator_list:
                mark = _is_skip_mark(dec.func if isinstance(dec, ast.Call) else dec)
                if mark:
                    out.append(
                        _finding("skip_marker", "block", path, f"{node.name} is decorated with {mark}", dec.lineno)
                    )
        if isinstance(node, ast.ClassDef):
            for dec in node.decorator_list:
                mark = _is_skip_mark(dec.func if isinstance(dec, ast.Call) else dec)
                if mark:
                    out.append(
                        _finding(
                            "skip_marker", "block", path, f"class {node.name} is decorated with {mark}", dec.lineno
                        )
                    )
        if isinstance(node, ast.Assert):
            reason = _constant_assert_reason(node.test)
            if reason:
                out.append(_finding("constant_assert", "block", path, reason, node.lineno))
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets):
            values = node.value.elts if isinstance(node.value, ast.List | ast.Tuple) else [node.value]
            for v in values:
                mark = _is_skip_mark(v.func if isinstance(v, ast.Call) else v)
                if mark:
                    out.append(
                        _finding("skip_marker", "block", path, f"module-level pytestmark uses {mark}", node.lineno)
                    )
        if isinstance(node, ast.Call):
            name = _dotted(node.func)
            mark = _is_skip_mark(node.func)
            if mark and not isinstance(node.func, ast.Name):
                out.append(_finding("skip_marker", "block", path, f"call to {mark}", node.lineno))
            elif name in ("skipTest", "self.skipTest"):
                out.append(_finding("skip_marker", "block", path, "call to skipTest", node.lineno))
            parts = tuple(name.split("."))
            if (
                parts in EXIT_CALLS
                or name in ("exit", "quit", "atexit.register")
                or parts[-2:] == ("atexit", "register")
            ):
                out.append(_finding("exit_call", "block", path, f"call to {name}", node.lineno))
            out.extend(_monkeypatch_findings(path, node, name, imports.roots))
        if (
            isinstance(node, ast.Raise)
            and node.exc is not None
            and _dotted(node.exc) in ("SystemExit", "KeyboardInterrupt")
        ):
            out.append(_finding("exit_call", "block", path, f"raise {_dotted(node.exc)}", node.lineno))
    return out


def _monkeypatch_findings(path: str, node: ast.Call, name: str, roots: dict[str, str]) -> list[GateFinding]:
    last2 = name.split(".")[-2:]
    patchy = (
        name.endswith("monkeypatch.setattr")
        or name in ("patch", "mock.patch", "unittest.mock.patch", "patch.object", "mock.patch.object")
        or last2 == ["patch", "object"]
        or last2 == ["mock", "patch"]
    )
    if not patchy or not node.args:
        return []
    target = node.args[0]
    root = ""
    if isinstance(target, ast.Constant) and isinstance(target.value, str):
        root = target.value.split(".")[0]
    elif isinstance(target, ast.Name):
        root = roots.get(target.id, target.id)
    elif isinstance(target, ast.Attribute):
        root = roots.get(_dotted(target).split(".")[0], _dotted(target).split(".")[0])
    if root in STDLIB or root in ("builtins", "pytest"):
        return []
    shown = target.value if isinstance(target, ast.Constant) else ast.unparse(target)
    return [
        _finding("monkeypatch_target", "flag", path, f"patches {shown!r}, which may be code under test", node.lineno)
    ]


# ---------------------------------------------------------------- production-code rules
def _lines_of(node: ast.AST) -> range:
    start = int(getattr(node, "lineno", 0))
    end = int(getattr(node, "end_lineno", None) or start)
    return range(start, end + 1)


def _only_constant_return(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    body = list(func.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return len(body) == 1 and isinstance(body[0], ast.Return) and isinstance(body[0].value, ast.Constant)


def _always_true(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    body = [b for b in func.body if not (isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant))]
    return (
        len(body) == 1
        and isinstance(body[0], ast.Return)
        and isinstance(body[0].value, ast.Constant)
        and body[0].value.value is True
    )


def check_prod_source(
    path: str,
    patched: str,
    touched: set[int],
    base: str | None,
    repro_source: str | None,
) -> list[GateFinding]:
    """AST rules for production files, restricted to code on lines the patch added."""
    try:
        tree = ast.parse(patched)
    except SyntaxError as exc:
        return [_finding("unparseable_python", "block", path, f"patched file does not parse: {exc.msg}", exc.lineno)]
    base_funcs: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    if base is not None:
        try:
            base_tree = ast.parse(base)
            base_funcs = {
                n.name: n for n in ast.walk(base_tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
            }
        except SyntaxError:
            base_funcs = {}
    out: list[GateFinding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not touched.intersection(_lines_of(node)):
            continue
        before = base_funcs.get(node.name)
        if node.name == "__eq__" and _always_true(node):
            out.append(_finding("always_true_eq", "block", path, "__eq__ always returns True", node.lineno))
        if _only_constant_return(node) and not (before and _only_constant_return(before)):
            out.append(
                _finding("literal_return", "flag", path, f"{node.name} now only returns a constant", node.lineno)
            )
        if repro_source:
            params = {a.arg for a in [*node.args.args, *node.args.kwonlyargs]}
            for sub in ast.walk(node):
                if not isinstance(sub, ast.If) or sub.lineno not in touched:
                    continue
                if _literal_in_source(sub.test, params, repro_source):
                    out.append(
                        _finding(
                            "test_literal_special_case",
                            "flag",
                            path,
                            f"{node.name} special-cases a literal that appears in the reproduction test",
                            sub.lineno,
                        )
                    )
    return out


def _literal_in_source(test: ast.expr, params: set[str], source: str) -> bool:
    if not (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and isinstance(test.left, ast.Name)
        and test.left.id in params
        and isinstance(test.comparators[0], ast.Constant)
    ):
        return False
    value = test.comparators[0].value
    if value in (None, True, False, 0, 1, "", -1):
        return False
    needles = {repr(value), str(value), f'"{value}"' if isinstance(value, str) else str(value)}
    return any(n and n in source for n in needles)


# ---------------------------------------------------------------- the gate
class PolicyGate:
    def __init__(
        self,
        cfg: PolicyCfg,
        base_dir: Path | None = None,
        repro_source: str | None = None,
        secrets: Iterable[str] = (),
    ) -> None:
        self.cfg = cfg
        self.secrets = [s for s in secrets if s]
        self.base_dir = Path(base_dir) if base_dir else None
        self.repro_source = repro_source

    # -- paths
    def in_generated_dir(self, path: str) -> bool:
        gen = self.cfg.generated_test_dir.strip("/")
        return path == gen or path.startswith(gen + "/")

    def is_protected(self, path: str, *, is_new: bool = False) -> bool:
        path = _norm(path)
        if is_new and self.in_generated_dir(path) and not self._matches_non_tests_glob(path):
            return False
        return self._matches(path, self.cfg.protected_globs)

    def _matches_non_tests_glob(self, path: str) -> bool:
        """Generated tests are exempt from the broad ``tests/**`` glob only; conftest, ini files etc. stay blocked."""
        return self._matches(path, [g for g in self.cfg.protected_globs if g not in ("tests/**", "test/**")])

    @staticmethod
    def _matches(path: str, globs: list[str]) -> bool:
        """Case-insensitive: on case-insensitive filesystems ``Tests/x.py`` IS ``tests/x.py``."""
        low = path.lower()
        return any(glob_match(low, g.lower()) for g in globs)

    # -- diff
    def check_diff(self, patch: str) -> list[GateFinding]:
        if not patch.strip():
            return []
        try:
            ps = PatchSet(patch)
        except (UnidiffParseError, ValueError, IndexError) as exc:
            return [_finding("unparseable_diff", "block", "", f"cannot parse diff: {exc}")]
        findings: list[GateFinding] = []
        modes = self._modes(patch)
        if _BINARY.search(patch):
            findings.append(_finding("binary_or_symlink", "block", "", "diff contains binary content"))

        total = 0
        files = list(ps)
        for pf in files:
            total += pf.added + pf.removed
        if len(files) > self.cfg.max_files_changed:
            findings.append(
                _finding("size", "block", "", f"{len(files)} files changed (max {self.cfg.max_files_changed})")
            )
        if total > self.cfg.max_diff_lines:
            findings.append(_finding("size", "block", "", f"{total} changed lines (max {self.cfg.max_diff_lines})"))

        for pf in files:
            findings.extend(self._check_file(pf, modes))
        findings.extend(self._check_headers(patch, files, modes))
        findings.extend(self._check_secrets(files))
        seen: set[tuple[str, str, str, int | None]] = set()
        unique: list[GateFinding] = []
        for f in findings:
            key = (f.rule, f.path, f.detail, f.line)
            if key not in seen:
                seen.add(key)
                unique.append(f)
        return unique

    def _check_secrets(self, files: list[PatchedFile]) -> list[GateFinding]:
        """Added lines must not carry credentials, the run's own token or the red-team canary."""
        added = "\n".join(line.value for pf in files for hunk in pf for line in hunk if line.line_type == "+")
        return [
            _finding("secret_in_patch", "block", "", f"added lines contain a {label}")
            for label in scan_secrets(added, self.secrets)
        ]

    def _check_headers(self, patch: str, files: list[PatchedFile], modes: dict[str, list[str]]) -> list[GateFinding]:
        """``diff --git`` headers must agree with the ``---``/``+++`` lines the rest of the gate inspects.

        ``git apply`` can act on a header path that carries no hunks (mode-only changes) or that differs from the
        file headers; anything the per-file checks did not see is checked here, and fails closed.
        """
        known = {_norm(pf.path) for pf in files} | {_norm(pf.source_file) for pf in files}
        out: list[GateFinding] = []
        for line in patch.splitlines():
            m = _HEADER.match(line)
            if not m:
                continue
            for p in {_norm(m.group(1)), _norm(m.group(2))}:
                if p in known:
                    continue
                if self.is_protected(p):
                    out.append(
                        _finding("protected_path", "block", p, f"diff header touches protected file {p} without hunks")
                    )
                else:
                    out.append(
                        _finding("unparseable_diff", "block", p, f"diff header names {p} but no matching file section")
                    )
            if m.group(2) in known:
                continue  # per-file checks already reported modes for this section
            for mode in modes.get(m.group(2), []):
                if mode.endswith(("120000", "160000")):
                    out.append(_finding("binary_or_symlink", "block", m.group(2), f"symlink or submodule ({mode})"))
                elif mode.startswith(("old mode", "new mode")):
                    out.append(_finding("mode_change", "flag", m.group(2), f"file mode change ({mode})"))
        return out

    @staticmethod
    def _modes(patch: str) -> dict[str, list[str]]:
        """path -> mode lines ('new file mode 120000', ...) parsed from ``diff --git`` sections."""
        modes: dict[str, list[str]] = {}
        current: str | None = None
        for line in patch.splitlines():
            m = _HEADER.match(line)
            if m:
                current = m.group(2)
                modes.setdefault(current, [])
                continue
            mm = _MODE.match(line)
            if mm and current:
                modes[current].append(f"{mm.group(1)} {mm.group(2)}")
        return modes

    def _check_file(self, pf: PatchedFile, modes: dict[str, list[str]]) -> list[GateFinding]:
        out: list[GateFinding] = []
        path = _norm(pf.path)
        source = _norm(pf.source_file) if not pf.is_added_file and pf.source_file != "/dev/null" else path
        is_new = pf.is_added_file
        for p in {path, source}:
            if PurePosixPath(p).is_absolute() or ".." in PurePosixPath(p).parts or (len(p) > 1 and p[1] == ":"):
                out.append(_finding("path_escape", "block", p, "path leaves the repository"))
        # protected paths: both ends of a rename, deletions, additions and edits
        ends = [(path, is_new)] if source == path else [(path, is_new), (source, False)]
        for p, new in ends:
            if self.is_protected(p, is_new=new):
                verb = "adds" if new else ("deletes" if pf.is_removed_file else "modifies")
                out.append(_finding("protected_path", "block", p, f"patch {verb} protected file {p}"))
        for mode in modes.get(path, []):
            if mode.endswith("120000"):
                out.append(_finding("binary_or_symlink", "block", path, f"symlink ({mode})"))
            elif mode.endswith("160000"):
                out.append(_finding("binary_or_symlink", "block", path, f"submodule ({mode})"))
            elif mode.startswith(("old mode", "new mode")):
                out.append(_finding("mode_change", "flag", path, f"file mode change ({mode})"))
        if getattr(pf, "is_binary_file", False):
            out.append(_finding("binary_or_symlink", "block", path, "binary file in diff"))

        if pf.is_removed_file:
            return out
        if path == "pyproject.toml" or path.endswith("/pyproject.toml"):
            out.extend(self._check_pyproject(pf, path))
        if path.endswith(".py"):
            out.extend(self._check_python(pf, path, is_new))
        return out

    # -- pyproject
    def _check_pyproject(self, pf: PatchedFile, path: str) -> list[GateFinding]:
        base_text = self._read_base(path) if not pf.is_added_file else ""
        if base_text is None:
            return [_finding("pyproject_section", "block", path, "cannot compare pyproject.toml without the base tree")]
        try:
            new_text = apply_patched_file(base_text, pf) if not pf.is_added_file else added_text(pf)
            old = tomllib.loads(base_text) if base_text else {}
            new = tomllib.loads(new_text)
        except (ValueError, tomllib.TOMLDecodeError) as exc:
            return [_finding("pyproject_section", "block", path, f"cannot parse pyproject.toml change: {exc}")]
        out: list[GateFinding] = []
        for section in self.cfg.pyproject_protected_sections:
            if _get_path(old, section) != _get_path(new, section):
                out.append(_finding("pyproject_section", "block", path, f"changes protected section [{section}]"))
        return out

    # -- python files
    def _check_python(self, pf: PatchedFile, path: str, is_new: bool) -> list[GateFinding]:
        out: list[GateFinding] = []
        test_file = is_test_path(path)
        if is_new:
            patched = added_text(pf)
            base_text: str | None = None
        else:
            base_text = self._read_base(path)
            if base_text is None:
                return [_finding("unparseable_python", "block", path, "base file unavailable for analysis")]
            try:
                patched = apply_patched_file(base_text, pf)
            except ValueError as exc:
                return [_finding("unparseable_python", "block", path, f"cannot reconstruct patched file: {exc}")]

        if test_file:
            out.extend(check_test_source(path, patched))
            return out

        added = [line.value.rstrip("\n") for hunk in pf for line in hunk if line.line_type == "+"]
        for text in added:
            if any(pat.search(text) for pat in SNIFF_PATTERNS):
                detail = f"production code inspects the test runner: {text.strip()[:80]}"
                out.append(_finding("test_env_sniff", "block", path, detail))
            if "#" in text and COMMENT_PATTERN.search(text.split("#", 1)[1]):
                out.append(_finding("special_case_comment", "flag", path, f"suspicious comment: {text.strip()[:80]}"))
        out.extend(check_prod_source(path, patched, added_line_numbers(pf), base_text, self.repro_source))
        return out

    def _read_base(self, path: str) -> str | None:
        if not self.base_dir:
            return None
        p = (self.base_dir / path).resolve()
        if not p.is_relative_to(self.base_dir.resolve()):
            return None
        try:
            return p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    # -- commands (fallback loop only)
    def check_cmd(self, cmd: str) -> list[GateFinding]:
        return [
            _finding("forbidden_cmd", "block", "", f"command contains forbidden pattern {pat!r}")
            for pat in self.cfg.forbidden_cmd_patterns
            if pat in cmd
        ]


def _get_path(data: dict[str, Any], dotted: str) -> Any:
    cur: Any = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur
