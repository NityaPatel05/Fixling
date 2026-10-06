"""Hybrid retrieval: hard hints + BM25 + embeddings fused with RRF (spec E8)."""

from __future__ import annotations

import hashlib
import keyword
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np
from pydantic import BaseModel, Field
from rank_bm25 import BM25Okapi

from fixling.errors import LLMUnavailable
from fixling.models import Chunk, IssueSpec
from fixling.repo.index import is_test_path

if TYPE_CHECKING:
    from fixling.llm.client import LLMLike
    from fixling.runs import RunStore

RRF_K = 60
HINT_BOOST = 1.0  # RRF scores are < 0.04, so any hard hit outranks pure ranking signals
TOP_FILES = 15
TOP_CHUNKS = 40
SOURCE_PREFIX = 1500

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PATH = re.compile(r"[\w./\\-]+\.py\b")
_TRACE = re.compile(r'File "([^"]+)", line (\d+)')
_BACKTICK = re.compile(r"`([^`\n]+)`")
_CALL = re.compile(r"\b([A-Za-z_][A-Za-z0-9_.]*)\(")
_KEYWORDS = set(keyword.kwlist) | {"self", "cls", "none", "true", "false"}


def tokenize(text: str) -> list[str]:
    """Split snake_case / camelCase identifiers, lowercase, drop Python keywords and 1-char tokens."""
    out: list[str] = []
    for word in _WORD.findall(text):
        for part in word.split("_"):
            for piece in _CAMEL.split(part):
                tok = piece.lower()
                if len(tok) > 1 and tok not in _KEYWORDS:
                    out.append(tok)
    return out


def chunk_text(c: Chunk) -> str:
    return f"{c.qualname} {c.signature} {c.docstring or ''} {c.source[:SOURCE_PREFIX]}"


def text_hash(c: Chunk) -> str:
    return hashlib.sha1(c.source.encode("utf-8")).hexdigest()


@dataclass
class HardHints:
    paths: list[str] = field(default_factory=list)
    trace: list[tuple[str, int]] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)

    def any(self) -> bool:
        return bool(self.paths or self.trace or self.symbols)


class RetrievalResult(BaseModel):
    files: list[str] = Field(default_factory=list)  # top-15, best first
    file_scores: dict[str, float] = Field(default_factory=dict)
    chunks: list[Chunk] = Field(default_factory=list)  # top-40 non-test chunks, best first
    chunk_scores: dict[str, float] = Field(default_factory=dict)  # qualname -> fused score
    related_tests: list[Chunk] = Field(default_factory=list)
    hint_hits: list[str] = Field(default_factory=list)  # qualnames matched by hard hints
    used_embeddings: bool = False


def extract_hints(issue_text: str, spec: IssueSpec | None) -> HardHints:
    hints = HardHints()
    for p in _PATH.findall(issue_text):
        norm = p.replace("\\", "/").lstrip("./")
        if norm not in hints.paths:
            hints.paths.append(norm)
    for path, line in _TRACE.findall(issue_text):
        hints.trace.append((path.replace("\\", "/"), int(line)))
        if path.replace("\\", "/") not in hints.paths:
            hints.paths.append(path.replace("\\", "/"))
    syms: list[str] = []
    for raw in _BACKTICK.findall(issue_text):
        raw = raw.strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*(\(\))?", raw):
            syms.append(raw.removesuffix("()"))
        syms.extend(m.group(1) for m in _CALL.finditer(raw))
    syms.extend(m.group(1) for m in _CALL.finditer(issue_text) if len(m.group(1)) > 2)
    if spec:
        syms.extend(spec.mentioned_symbols)
    for s in syms:
        s = s.strip().strip("()")
        if s and s.lower() not in _KEYWORDS and s not in hints.symbols:
            hints.symbols.append(s)
    return hints


def _path_matches(chunk_path: str, hint: str) -> bool:
    return chunk_path == hint or chunk_path.endswith("/" + hint) or hint.endswith("/" + chunk_path)


def _symbol_matches(chunk: Chunk, sym: str) -> bool:
    dotted = chunk.qualname.split("::", 1)[1]
    last = dotted.rsplit(".", 1)[-1]
    if sym in (dotted, last) or sym.endswith("." + dotted) or sym == chunk.path or chunk.path.endswith("/" + sym):
        return True
    return sym.rsplit(".", 1)[-1] == last  # module.func / obj.method style references


def hard_hint_hits(chunks: list[Chunk], hints: HardHints) -> dict[str, float]:
    """qualname -> boost. Traceback lines and exact symbols weigh most, bare path matches least."""
    hits: dict[str, float] = {}
    for c in chunks:
        if c.kind == "module":
            continue
        score = 0.0
        for path, line in hints.trace:
            if _path_matches(c.path, path) and c.start <= line <= c.end:
                score += 3.0
        for sym in hints.symbols:
            if _symbol_matches(c, sym):
                score += 2.0
        if any(_path_matches(c.path, p) for p in hints.paths):
            score += 0.5
        if score:
            hits[c.qualname] = score
    return hits


def _rank_by(scores: dict[str, float]) -> dict[str, int]:
    ordered = sorted(scores, key=lambda k: (-scores[k], k))
    return {q: i + 1 for i, q in enumerate(ordered)}


def _bm25_scores(chunks: list[Chunk], query: str) -> dict[str, float]:
    docs = [tokenize(chunk_text(c)) or ["_"] for c in chunks]
    q = tokenize(query)
    if not q:
        return {}
    scores = BM25Okapi(docs).get_scores(q)
    return {c.qualname: float(s) for c, s in zip(chunks, scores, strict=True) if s > 0}


def _embedding_scores(
    chunks: list[Chunk], query: str, llm: LLMLike, runs: RunStore | None, repo_key: str
) -> dict[str, float] | None:
    try:
        hashes = [text_hash(c) for c in chunks]
        cached = runs.get_embeddings(repo_key, hashes) if runs else {}
        missing = [(h, c) for h, c in zip(hashes, chunks, strict=True) if h not in cached]
        unique: dict[str, Chunk] = {}
        for h, c in missing:
            unique.setdefault(h, c)
        if unique:
            vecs = llm.embed([chunk_text(c) for c in unique.values()])
            fresh = dict(zip(unique.keys(), vecs, strict=True))
            if runs:
                runs.put_embeddings(repo_key, fresh)
            cached.update(fresh)
        qv = llm.embed([query])[0]
    except LLMUnavailable:
        return None
    mat = np.stack([cached[h] for h in hashes])
    sims = mat @ qv
    return {c.qualname: float(s) for c, s in zip(chunks, sims, strict=True)}


def find_related_tests(chunks: list[Chunk], top: list[Chunk], scores: dict[str, float], limit: int = 10) -> list[Chunk]:
    """Existing tests that mention the strongest candidates (the best two, plus others scoring >= 30% of the best)."""
    best = scores.get(top[0].qualname, 0.0) if top else 0.0
    strong = [c for i, c in enumerate(top[:5]) if i < 2 or scores.get(c.qualname, 0.0) >= 0.3 * best]
    names = {c.qualname.split("::", 1)[1].rsplit(".", 1)[-1] for c in strong if c.kind in ("function", "method")}
    names |= {c.qualname.split("::", 1)[1].split(".")[0] for c in strong if c.kind == "class"}
    names = {n for n in names if len(n) > 2}
    if not names:
        return []
    found: list[tuple[int, Chunk]] = []
    for c in chunks:
        if c.kind not in ("function", "method") or not is_test_path(c.path):
            continue
        body = f"{c.qualname} {c.source}"
        hits = sum(1 for n in names if re.search(rf"\b{re.escape(n)}\b", body))
        if hits and c.signature.removeprefix("async ").startswith("def test"):
            found.append((hits, c))
    found.sort(key=lambda t: (-t[0], t[1].qualname))
    return [c for _, c in found[:limit]]


def retrieve(
    spec: IssueSpec | None,
    issue_text: str,
    chunks: list[Chunk],
    llm: LLMLike | None = None,
    *,
    runs: RunStore | None = None,
    repo_key: str = "repo",
    use_embeddings: bool = True,
) -> RetrievalResult:
    """Rank code chunks and files for an issue. Test files are never fix targets (they become related tests)."""
    code = [c for c in chunks if c.kind != "module" and not is_test_path(c.path)]
    if not code:
        return RetrievalResult(related_tests=[])
    query_parts = [issue_text]
    if spec:
        query_parts += [spec.summary, spec.actual_behavior, spec.expected_behavior, " ".join(spec.mentioned_symbols)]
    query = "\n".join(p for p in query_parts if p)
    sem_query = "\n".join(
        p for p in ([spec.summary, spec.actual_behavior, spec.expected_behavior] if spec else [issue_text]) if p
    )

    hints = extract_hints(issue_text, spec)
    hits = hard_hint_hits(code, hints)
    bm25 = _bm25_scores(code, query)
    emb = _embedding_scores(code, sem_query, llm, runs, repo_key) if (llm and use_embeddings) else None

    fused: dict[str, float] = {c.qualname: 0.0 for c in code}
    for scores in (bm25, emb):
        if not scores:
            continue
        for q, rank in _rank_by(scores).items():
            fused[q] += 1.0 / (RRF_K + rank)
    for q, boost in hits.items():
        fused[q] += HINT_BOOST * boost

    by_qual = {c.qualname: c for c in code}
    ranked = sorted(fused, key=lambda q: (-fused[q], q))
    top_chunks = [by_qual[q] for q in ranked[:TOP_CHUNKS]]

    per_file: dict[str, list[float]] = {}
    for q in ranked:
        per_file.setdefault(by_qual[q].path, []).append(fused[q])
    file_scores = {p: max(s) + 0.3 * float(np.mean(sorted(s, reverse=True)[:3])) for p, s in per_file.items()}
    files = sorted(file_scores, key=lambda p: (-file_scores[p], p))[:TOP_FILES]

    return RetrievalResult(
        files=files,
        file_scores={p: file_scores[p] for p in files},
        chunks=top_chunks,
        chunk_scores={q: fused[q] for q in ranked[:TOP_CHUNKS]},
        related_tests=find_related_tests(chunks, top_chunks, fused),
        hint_hits=[q for q in ranked if q in hits][:10],
        used_embeddings=emb is not None,
    )
