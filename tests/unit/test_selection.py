from __future__ import annotations

from fixling.graph.selection import (
    changed_files,
    dedupe,
    diff_line_count,
    is_clean,
    is_viable,
    normalize_patch,
    patch_hash,
    rank,
    score_candidate,
    tied_for_best,
)
from fixling.models import Candidate, GateFinding

PATCH_A = (
    "diff --git a/src/a.py b/src/a.py\nindex 111..222 100644\n--- a/src/a.py\n+++ b/src/a.py\n"
    "@@ -1,2 +1,2 @@\n x = 1\n-y = 2\n+y = 3\n"
)
PATCH_B = (
    "diff --git a/src/b.py b/src/b.py\nindex 333..444 100644\n--- a/src/b.py\n+++ b/src/b.py\n@@ -1 +1 @@\n-p\n+q\n"
)


def cand(cid: str = "c", **kw: object) -> Candidate:
    base: dict[str, object] = {
        "id": cid,
        "round": 0,
        "temperature": 0.2,
        "patch_path": f"runs/x/{cid}.diff",
        "patch_hash": patch_hash(PATCH_A),
        "diff_lines": 2,
        "repro_passed": True,
    }
    base.update(kw)
    return Candidate.model_validate(base)


def finding(sev: str, rule: str = "r") -> GateFinding:
    return GateFinding(rule=rule, severity=sev, path="x.py", detail="d")  # type: ignore[arg-type]


def test_patch_normalisation_ignores_index_lines_whitespace_and_file_order() -> None:
    reordered = PATCH_B + PATCH_A.replace("index 111..222", "index aaa..bbb") + ""
    assert patch_hash(PATCH_A + PATCH_B) == patch_hash(reordered)
    assert patch_hash(PATCH_A) == patch_hash(PATCH_A.replace("y = 3", "y = 3   "))
    assert patch_hash(PATCH_A) != patch_hash(PATCH_A.replace("y = 3", "y = 4"))
    assert normalize_patch("") == ""


def test_diff_stats() -> None:
    assert diff_line_count(PATCH_A) == 2  # one added, one removed; headers excluded
    assert changed_files(PATCH_A + PATCH_B) == ["src/a.py", "src/b.py"]


def test_dedupe_folds_identical_patches_and_counts_agreement() -> None:
    a1, a2, a3 = cand("c1"), cand("c2"), cand("c3")
    other = cand("c4", patch_hash=patch_hash(PATCH_B))
    failed = cand("c5", patch_path=None, patch_hash=None)
    failed2 = cand("c6", patch_path=None, patch_hash=None)
    out = dedupe([a1, a2, other, a3, failed, failed2])
    assert [c.id for c in out] == ["c1", "c4", "c5", "c6"]  # failed candidates are never merged
    assert out[0].agreement == 2 and out[1].agreement == 0


def test_score_follows_the_spec_weights() -> None:
    base = score_candidate(cand(), has_repro=True)
    assert base == 100 + 50 - 0.2  # repro passes, no regressions, 2 diff lines
    assert score_candidate(cand(repro_passed=False), has_repro=True) == 50 - 0.2
    assert score_candidate(cand(regressions=["t"]), has_repro=True) == 100 - 0.2
    assert score_candidate(cand(findings=[finding("block")]), has_repro=True) == base - 1000
    assert score_candidate(cand(findings=[finding("flag"), finding("flag")]), has_repro=True) == base - 20
    assert score_candidate(cand(agreement=2), has_repro=True) == base + 10
    assert score_candidate(cand(), has_repro=False) == 50 - 0.2  # no reproduction: no repro bonus
    assert score_candidate(cand(patch_path=None), has_repro=True) < -1000  # unusable
    assert score_candidate(cand(junit_ok=False), has_repro=True) < -1000


def test_rank_orders_best_first_and_keeps_earlier_candidate_on_ties() -> None:
    ranked = rank([cand("a", repro_passed=False), cand("b"), cand("c")], has_repro=True)
    assert [c.id for c in ranked] == ["b", "c", "a"]
    assert [c.id for c in tied_for_best(ranked)] == ["b", "c"]
    assert tied_for_best(rank([cand("x", patch_path=None, patch_hash=None)], has_repro=True)) == []
    assert tied_for_best([]) == []


def test_viability_and_cleanliness() -> None:
    assert is_viable(cand(), has_repro=True) and is_clean(cand(), has_repro=True)
    assert not is_viable(cand(repro_passed=False), has_repro=True)
    assert is_viable(cand(repro_passed=None), has_repro=False)  # without a repro nothing can fail it
    assert not is_viable(cand(findings=[finding("block")]), has_repro=True)
    assert is_viable(cand(findings=[finding("flag")]), has_repro=True)
    assert is_viable(cand(regressions=["t"]), has_repro=True) and not is_clean(cand(regressions=["t"]), has_repro=True)
    assert not is_viable(cand(patch_path=None), has_repro=True)
