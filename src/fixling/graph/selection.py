"""Candidate normalisation, de-duplication and scoring (spec E16/E17 ``select``). Pure functions."""

from __future__ import annotations

import hashlib
import re

from fixling.models import Candidate, GateFinding

# Score weights (spec E17): reproduce passes +100, no impacted regressions +50, block -1000, flag -10,
# each duplicate (agreement) +5, diff size -0.1/line.
W_REPRO = 100.0
W_NO_REGRESSION = 50.0
W_BLOCK = -1000.0
W_FLAG = -10.0
W_AGREE = 5.0
W_DIFF_LINE = -0.1
W_UNUSABLE = -5000.0


_HEADER = re.compile(r"^diff --git ", re.M)


def normalize_patch(patch: str) -> str:
    """Canonical form for equality: per-file sections sorted, trailing whitespace stripped, index lines dropped."""
    sections = [s for s in _HEADER.split(patch.replace("\r\n", "\n")) if s.strip()]
    normed: list[str] = []
    for sec in sections:
        lines = [ln.rstrip() for ln in sec.split("\n") if not ln.startswith("index ")]
        normed.append("\n".join(lines).strip())
    return "\n".join(sorted(normed))


def patch_hash(patch: str) -> str:
    return hashlib.sha1(normalize_patch(patch).encode("utf-8")).hexdigest()


def diff_line_count(patch: str) -> int:
    return sum(1 for ln in patch.split("\n") if (ln.startswith(("+", "-")) and not ln.startswith(("+++", "---"))))


def changed_files(patch: str) -> list[str]:
    return sorted(set(re.findall(r"^diff --git a/(\S+) b/", patch, re.M)))


def dedupe(cands: list[Candidate]) -> list[Candidate]:
    """Fold identical patches into the first copy; its ``agreement`` counts how many others matched.

    Candidates without a patch are never merged (their failures are individual).
    """
    out: list[Candidate] = []
    seen: dict[str, Candidate] = {}
    for c in cands:
        key = c.patch_hash
        if not key:
            out.append(c)
            continue
        first = seen.get(key)
        if first is None:
            c = c.model_copy(update={"agreement": c.agreement})
            seen[key] = c
            out.append(c)
        else:
            first.agreement += 1
    return out


def blocks(findings: list[GateFinding]) -> list[GateFinding]:
    return [f for f in findings if f.severity == "block"]


def flags(findings: list[GateFinding]) -> list[GateFinding]:
    return [f for f in findings if f.severity == "flag"]


def usable(c: Candidate) -> bool:
    """A candidate that produced an applied, non-empty patch whose tests could be evaluated."""
    return bool(c.patch_path and c.patch_hash and c.junit_ok)


def score_candidate(c: Candidate, *, has_repro: bool) -> float:
    if not usable(c):
        return W_UNUSABLE
    score = W_DIFF_LINE * c.diff_lines + W_AGREE * c.agreement
    if has_repro and c.repro_passed:
        score += W_REPRO
    if not c.regressions:
        score += W_NO_REGRESSION
    score += W_BLOCK * len(blocks(c.findings)) + W_FLAG * len(flags(c.findings))
    return round(score, 4)


def rank(cands: list[Candidate], *, has_repro: bool) -> list[Candidate]:
    """Score every candidate and return them best first (stable on ties: earlier candidate wins)."""
    scored = [c.model_copy(update={"score": score_candidate(c, has_repro=has_repro)}) for c in cands]
    return sorted(scored, key=lambda c: -c.score)


def tied_for_best(ranked: list[Candidate]) -> list[Candidate]:
    if not ranked or not usable(ranked[0]):
        return []
    return [c for c in ranked if usable(c) and abs(c.score - ranked[0].score) < 1e-9]


def is_viable(c: Candidate, *, has_repro: bool) -> bool:
    """Passes the reproduction (when one exists), no block findings, and the patch applied."""
    if not usable(c) or blocks(c.findings):
        return False
    return bool(c.repro_passed) if has_repro else True


def is_clean(c: Candidate, *, has_repro: bool) -> bool:
    return is_viable(c, has_repro=has_repro) and not c.regressions
