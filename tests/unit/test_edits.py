from __future__ import annotations

from pathlib import Path

import pytest

from fixling.models import Edit
from fixling.repo.edits import DirFiles, apply_edits, normalize_path

SRC = """\
def total(items):
    result = 0
    for item in items:
        result += item.price * item.qty
    return result


def discount(total, pct):
    if pct > 100:
        pct = 100
    return total - total * pct / 100
"""


@pytest.fixture
def ws(tmp_path: Path) -> DirFiles:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "cart.py").write_text(SRC, encoding="utf-8", newline="")
    return DirFiles(tmp_path)


def run(ws: DirFiles, *edits: Edit, allowed: tuple[str, ...] = ("src/cart.py",)):
    return apply_edits(ws, list(edits), allowed)


def test_exact_match(ws: DirFiles) -> None:
    r = run(
        ws,
        Edit(path="src/cart.py", search="    if pct > 100:\n        pct = 100\n", replace="    pct = min(pct, 100)\n"),
    )
    assert r.ok and r.patched_files == ["src/cart.py"]
    assert "pct = min(pct, 100)" in ws.read_file("src/cart.py")
    assert "if pct > 100" not in ws.read_file("src/cart.py")


def test_exact_match_with_surrounding_blank_lines(ws: DirFiles) -> None:
    r = run(ws, Edit(path="src/cart.py", search="\n    return result\n\n", replace="    return round(result, 2)"))
    assert r.ok
    assert "return round(result, 2)" in ws.read_file("src/cart.py")


def test_indentation_shifted_search_is_reindented(ws: DirFiles) -> None:
    # model dedented the block: matches ignoring indentation, replace is re-indented to the file's level
    r = run(
        ws,
        Edit(
            path="src/cart.py",
            search="for item in items:\n    result += item.price * item.qty",
            replace="for item in items:\n    result += item.price * item.qty * 2",
        ),
    )
    assert r.ok
    text = ws.read_file("src/cart.py")
    assert "    for item in items:\n        result += item.price * item.qty * 2\n" in text


def test_trailing_whitespace_differences_are_ignored(ws: DirFiles) -> None:
    r = run(
        ws,
        Edit(
            path="src/cart.py",
            search="    result = 0   \n    for item in items:  ",
            replace="    result = 1\n    for item in items:",
        ),
    )
    assert r.ok
    assert "result = 1" in ws.read_file("src/cart.py")


def test_fuzzy_unique_match(ws: DirFiles) -> None:
    # one token differs (qty -> quantity): not exact, not whitespace-equal, but uniquely similar
    r = run(
        ws,
        Edit(
            path="src/cart.py",
            search="    for item in items:\n        result += item.price * item.quantity",
            replace="    for item in items:\n        result += item.price * item.qty * 1",
        ),
    )
    assert r.ok, r.errors
    assert "item.qty * 1" in ws.read_file("src/cart.py")


def test_fuzzy_ambiguous_is_rejected(tmp_path: Path) -> None:
    body = "def a():\n    x = compute(1)\n    return x\n\n\ndef b():\n    x = compute(2)\n    return x\n"
    (tmp_path / "m.py").write_text(body, newline="")
    fs = DirFiles(tmp_path)
    r = apply_edits(
        fs, [Edit(path="m.py", search="    x = compute(3)\n    return x", replace="    return 0")], ["m.py"]
    )
    assert not r.ok and r.patched_files == []
    assert "not found" in r.errors[0]
    assert fs.read_file("m.py") == body


def test_exact_multiple_matches_rejected(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text("x = 1\ny = 2\nx = 1\n", newline="")
    r = apply_edits(DirFiles(tmp_path), [Edit(path="m.py", search="x = 1", replace="x = 3")], ["m.py"])
    assert not r.ok and "matches 2 places" in r.errors[0]


def test_not_found_gives_closest_match_advice(ws: DirFiles) -> None:
    r = run(
        ws,
        Edit(path="src/cart.py", search="def discount(amount, rate):\n    return amount * rate / 100", replace="pass"),
    )
    assert not r.ok and r.patched_files == []
    msg = r.errors[0]
    assert (
        "SEARCH block not found in src/cart.py" in msg and "Closest match" in msg and "def discount(total, pct)" in msg
    )


def test_syntax_error_reverts_the_file(ws: DirFiles) -> None:
    before = ws.read_file("src/cart.py")
    r = run(ws, Edit(path="src/cart.py", search="    return result\n", replace="    return (result\n"))
    assert not r.ok and r.patched_files == []
    assert "syntax error" in r.errors[0] and "line" in r.errors[0]
    assert ws.read_file("src/cart.py") == before


def test_path_outside_allowed_is_rejected(ws: DirFiles, tmp_path: Path) -> None:
    (tmp_path / "other.py").write_text("a = 1\n", newline="")
    r = run(ws, Edit(path="other.py", search="a = 1", replace="a = 2"))
    assert not r.ok and "not allowed" in r.errors[0]
    assert (tmp_path / "other.py").read_text() == "a = 1\n"


@pytest.mark.parametrize("bad", ["../x.py", "/etc/passwd", "a/../../b.py", "C:/x.py", ""])
def test_unsafe_paths_rejected(ws: DirFiles, bad: str) -> None:
    r = run(ws, Edit(path=bad, search="a", replace="b"), allowed=(bad, "src/cart.py"))
    assert not r.ok and r.patched_files == []


def test_normalize_path_variants() -> None:
    assert normalize_path("./src/a.py") == "src/a.py"
    assert normalize_path("/workspace/src/a.py") == "src/a.py"
    assert normalize_path("src\\a.py") == "src/a.py"
    assert normalize_path("../a.py") is None


def test_empty_search_rejected(ws: DirFiles) -> None:
    r = run(ws, Edit(path="src/cart.py", search="  \n", replace="import os\n"))
    assert not r.ok and "Empty SEARCH" in r.errors[0]


def test_identical_edit_rejected(ws: DirFiles) -> None:
    r = run(ws, Edit(path="src/cart.py", search="    result = 0", replace="    result = 0"))
    assert not r.ok and "identical" in r.errors[0]


def test_missing_file(ws: DirFiles) -> None:
    r = run(ws, Edit(path="src/nope.py", search="a", replace="b"), allowed=("src/nope.py",))
    assert not r.ok and "does not exist" in r.errors[0]


def test_crlf_file_roundtrip(tmp_path: Path) -> None:
    (tmp_path / "w.py").write_bytes(b"def f():\r\n    return 1\r\n")
    fs = DirFiles(tmp_path)
    r = apply_edits(fs, [Edit(path="w.py", search="    return 1", replace="    return 2")], ["w.py"])
    assert r.ok
    assert (tmp_path / "w.py").read_bytes() == b"def f():\r\n    return 2\r\n"


def test_multiple_edits_same_file_are_sequential(ws: DirFiles) -> None:
    r = run(
        ws,
        Edit(path="src/cart.py", search="    result = 0\n", replace="    result = 0.0\n"),
        Edit(path="src/cart.py", search="    return result\n", replace="    return float(result)\n"),
    )
    assert r.ok and r.applied == 2
    text = ws.read_file("src/cart.py")
    assert "result = 0.0" in text and "float(result)" in text


def test_partial_success_reports_errors_but_applies_good_edits(ws: DirFiles) -> None:
    r = run(
        ws,
        Edit(path="src/cart.py", search="    result = 0\n", replace="    result = 0.0\n"),
        Edit(path="src/cart.py", search="no such line anywhere at all", replace="x"),
    )
    assert r.applied == 1 and len(r.errors) == 1 and not r.ok
    assert "result = 0.0" in ws.read_file("src/cart.py")


def test_deletion_with_empty_replace(ws: DirFiles) -> None:
    r = run(ws, Edit(path="src/cart.py", search="    if pct > 100:\n        pct = 100\n", replace=""))
    assert r.ok
    assert "pct = 100" not in ws.read_file("src/cart.py")


def test_unicode_content(tmp_path: Path) -> None:
    (tmp_path / "u.py").write_text('MSG = "héllo wörld ✓"\n', encoding="utf-8", newline="")
    fs = DirFiles(tmp_path)
    r = apply_edits(fs, [Edit(path="u.py", search='MSG = "héllo wörld ✓"', replace='MSG = "héllo ✓"')], ["u.py"])
    assert r.ok and fs.read_file("u.py") == 'MSG = "héllo ✓"\n'
