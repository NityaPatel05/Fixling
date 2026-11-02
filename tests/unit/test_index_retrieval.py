from __future__ import annotations

from pathlib import Path

import numpy as np

from fixling.config import Config
from fixling.db import Database
from fixling.errors import LLMUnavailable
from fixling.models import IssueSpec
from fixling.repo.index import build_index, index_file, is_test_path, repo_map
from fixling.repo.retrieval import extract_hints, retrieve, tokenize
from fixling.runs import RunStore
from tests.fakes import FakeLLM


def spec(**kw: object) -> IssueSpec:
    base: dict[str, object] = {
        "summary": "bulk_price gives no discount for exactly 10 units",
        "kind": "bug",
        "expected_behavior": "10 units get 10 percent off",
        "actual_behavior": "10 units are charged full price",
        "repro_hints": [],
        "mentioned_symbols": [],
        "acceptance_criteria": [],
        "ambiguity": 1,
        "size_estimate": "small",
    }
    base.update(kw)
    return IssueSpec.model_validate(base)


# ---------------------------------------------------------------- index
def test_build_index_chunks_and_qualnames(tiny_repo: Path) -> None:
    chunks = build_index(tiny_repo)
    by_q = {c.qualname: c for c in chunks}
    assert "src/tiny/pricing.py::bulk_price" in by_q
    bp = by_q["src/tiny/pricing.py::bulk_price"]
    assert bp.kind == "function" and bp.signature == "def bulk_price(qty: int, unit_price: float) -> float"
    assert "if qty > BULK_THRESHOLD" in bp.source and bp.docstring and "BULK_THRESHOLD" in bp.docstring
    assert bp.source.splitlines()[0].startswith("def bulk_price") and bp.start < bp.end
    assert by_q["src/tiny/pricing.py::<module>"].kind == "module"
    assert "tests/test_pricing.py::test_coupon_is_clamped" in by_q


def test_index_classes_methods_decorators_and_async() -> None:
    src = (
        "class Cart(Base):\n"
        '    """A cart."""\n\n'
        "    @property\n"
        "    def total(self) -> float:\n"
        "        return 1.0\n\n"
        "    class Inner:\n"
        "        def deep(self): ...\n\n"
        "async def fetch(x): ...\n"
    )
    chunks = {c.qualname: c for c in index_file("m.py", src)}
    assert chunks["m.py::Cart"].kind == "class" and "total(self)" in chunks["m.py::Cart"].source
    assert chunks["m.py::Cart"].signature == "class Cart(Base)"
    total = chunks["m.py::Cart.total"]
    assert total.kind == "method" and total.start == 4  # decorator line included
    assert "m.py::Cart.Inner.deep" in chunks
    assert chunks["m.py::fetch"].signature == "async def fetch(x)"


def test_index_skips_noise_and_survives_syntax_errors(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "ok.py").write_text("def f(): ...\n")
    (tmp_path / "pkg" / "broken.py").write_text("def (:\n")
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "dep.py").write_text("def hidden(): ...\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.py").write_text("def nm(): ...\n")
    quals = {c.qualname for c in build_index(tmp_path)}
    assert "pkg/ok.py::f" in quals and "pkg/broken.py::<module>" in quals
    assert not any("hidden" in q or "nm" in q for q in quals)


def test_is_test_path() -> None:
    assert is_test_path("tests/test_a.py") and is_test_path("pkg/test_b.py") and is_test_path("pkg/conftest.py")
    assert is_test_path("a/b_test.py")
    assert not is_test_path("src/pkg/testing_utils.py")
    assert not is_test_path("src/tiny/pricing.py")


def test_repo_map_format_and_budget(tiny_repo: Path) -> None:
    chunks = build_index(tiny_repo)
    text = repo_map(chunks, ["src/tiny/pricing.py", "src/tiny/textutil.py"])
    assert text.splitlines()[0] == "src/tiny/pricing.py"
    assert "  def bulk_price(qty: int, unit_price: float) -> float" in text
    assert "src/tiny/textutil.py" in text
    tiny = repo_map(chunks, ["src/tiny/pricing.py", "src/tiny/textutil.py"], budget_tokens=20)
    assert "textutil" not in tiny and len(tiny) <= 20 * 3.5 + 1


def test_repo_map_class_line() -> None:
    chunks = index_file(
        "c.py", "class Cart:\n    def add(self, item, qty): ...\n    def total(self): ...\n\ndef apply(t): ...\n"
    )
    text = repo_map(chunks, ["c.py"])
    assert text == "c.py\n  class Cart:  add(self, item, qty)  total(self)\n  def apply(t)"


# ---------------------------------------------------------------- retrieval
def test_tokenizer_splits_identifiers_and_drops_keywords() -> None:
    assert tokenize("applyDiscount_toCart for HTTPServer") == ["apply", "discount", "to", "cart", "http", "server"]
    assert tokenize("if x is None: return a") == []


def test_extract_hints_paths_traces_symbols() -> None:
    text = (
        'Traceback:\n  File "src/tiny/pricing.py", line 11, in bulk_price\n'
        "Calling `bulk_price(10, 5.0)` and `Cart.total` fails; also see tests/test_pricing.py"
    )
    h = extract_hints(text, spec(mentioned_symbols=["apply_coupon"]))
    assert ("src/tiny/pricing.py", 11) in h.trace
    assert "src/tiny/pricing.py" in h.paths and "tests/test_pricing.py" in h.paths
    assert {"bulk_price", "Cart.total", "apply_coupon"} <= set(h.symbols)


def test_symbol_hint_ranks_target_first_and_excludes_tests(tiny_repo: Path) -> None:
    chunks = build_index(tiny_repo)
    res = retrieve(spec(), "Calling `bulk_price(10, 5.0)` returns 50.0, expected 45.0", chunks, None)
    assert res.files[0] == "src/tiny/pricing.py"
    assert res.chunks[0].qualname == "src/tiny/pricing.py::bulk_price"
    assert "src/tiny/pricing.py::bulk_price" in res.hint_hits
    assert all(not c.path.startswith("tests/") for c in res.chunks)
    assert not res.used_embeddings


def test_traceback_line_hint_selects_enclosing_function(tiny_repo: Path) -> None:
    chunks = build_index(tiny_repo)
    bp = next(c for c in chunks if c.qualname.endswith("::apply_coupon"))
    text = f'File "src/tiny/pricing.py", line {bp.start + 1}, in <module>\nValueError'
    res = retrieve(None, text, chunks, None)
    assert res.chunks[0].qualname == "src/tiny/pricing.py::apply_coupon"


def test_bm25_finds_by_vocabulary_without_hints(tiny_repo: Path) -> None:
    chunks = build_index(tiny_repo)
    res = retrieve(
        spec(summary="slug has wrong separators", actual_behavior="slugify output", expected_behavior="dashes"),
        "",
        chunks,
        None,
    )
    assert res.chunks[0].qualname == "src/tiny/textutil.py::slugify"


def test_related_tests_are_listed_not_targeted(tiny_repo: Path) -> None:
    chunks = build_index(tiny_repo)
    res = retrieve(spec(), "`bulk_price` is wrong for 10 units", chunks, None)
    names = {c.qualname for c in res.related_tests}
    assert "tests/test_pricing.py::test_bulk_price_small_order_has_no_discount" in names
    assert "tests/test_textutil.py::test_slugify_basic" not in names


def test_embeddings_are_cached_and_fused(tiny_repo: Path, cfg: Config) -> None:
    chunks = build_index(tiny_repo)
    runs = RunStore(Database(":memory:"))
    llm = FakeLLM()
    calls = {"n": 0}
    orig = llm.embed

    def counting(texts: list[str]) -> np.ndarray:
        calls["n"] += len(texts)
        return orig(texts)

    llm.embed = counting  # type: ignore[method-assign]
    r1 = retrieve(spec(), "`bulk_price` is wrong", chunks, llm, runs=runs, repo_key="tiny")
    first = calls["n"]
    r2 = retrieve(spec(), "`bulk_price` is wrong", chunks, llm, runs=runs, repo_key="tiny")
    assert r1.used_embeddings and r2.used_embeddings
    assert calls["n"] == first + 1  # second run only embeds the query: chunk vectors came from the cache
    assert r2.chunks[0].qualname == "src/tiny/pricing.py::bulk_price"  # hard hint still wins


def test_embedding_outage_falls_back_to_bm25(tiny_repo: Path) -> None:
    class Down(FakeLLM):
        def embed(self, texts: list[str]) -> np.ndarray:
            raise LLMUnavailable("down")

    res = retrieve(spec(), "`bulk_price` is wrong", build_index(tiny_repo), Down())
    assert not res.used_embeddings and res.chunks[0].qualname.endswith("::bulk_price")


def test_use_embeddings_false_never_calls_embed(tiny_repo: Path) -> None:
    class Boom(FakeLLM):
        def embed(self, texts: list[str]) -> np.ndarray:
            raise AssertionError("embed must not be called")

    res = retrieve(spec(), "`bulk_price`", build_index(tiny_repo), Boom(), use_embeddings=False)
    assert not res.used_embeddings


def test_empty_repo_returns_empty_result() -> None:
    res = retrieve(spec(), "anything", [], None)
    assert res.files == [] and res.chunks == []
