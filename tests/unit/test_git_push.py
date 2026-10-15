from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from fixling.config import Config
from fixling.errors import GitHubError
from fixling.gh import git_push
from fixling.gh.git_push import branch_name, gitleaks_scan, push_branch, scan_secrets
from tests.unit.test_policy import FIXTURES, TINY

ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "seed",
    "GIT_AUTHOR_EMAIL": "s@e",
    "GIT_COMMITTER_NAME": "seed",
    "GIT_COMMITTER_EMAIL": "s@e",
}


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, env=ENV, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """A bare 'GitHub' whose main branch holds the tiny_repo tree."""
    import shutil

    seed = tmp_path / "seed"
    shutil.copytree(TINY, seed, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    git(seed, "init", "-q", "-b", "main")
    git(seed, "add", "-A")
    git(seed, "commit", "-qm", "initial")
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(bare)], check=True, capture_output=True)
    return bare


def legit(name: str) -> str:
    return next((FIXTURES / "legit").glob(f"*{name}.diff")).read_text(encoding="utf-8")


def hack(name: str) -> str:
    return next((FIXTURES / "hacks").glob(f"*{name}.diff")).read_text(encoding="utf-8")


def push(cfg: Config, origin: Path, patch: str, tmp_path: Path, **kw: object) -> tuple[str, list[str]]:
    args: dict[str, object] = {
        "repo": "o/r",
        "token": "ghp_" + "P" * 36,
        "base_branch": "main",
        "run_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "issue_number": 3,
        "issue_title": "bulk_price is wrong",
        "patch": patch,
        "policy": cfg.policy,
        "clone_url": origin.as_uri(),
        "use_gitleaks": False,
        "work_root": tmp_path,
    }
    args.update(kw)
    return push_branch(**args)  # type: ignore[arg-type]


def test_pushes_a_fixling_branch_with_the_patch_as_one_bot_commit(cfg: Config, origin: Path, tmp_path: Path) -> None:
    before = git(origin, "rev-parse", "main")
    branch, warnings = push(cfg, origin, legit("fix_with_repro_test"), tmp_path)
    assert branch == "fixling/issue-3-01ARZ3ND" and warnings == []
    assert git(origin, "rev-parse", "main") == before, "the base branch must never move"
    assert (
        git(origin, "log", "-1", "--format=%an <%ae>|%s", branch)
        == "Fixling Bot <fixling@users.noreply.github.com>|fixling: bulk_price is wrong (#3)"
    )
    assert git(origin, "rev-list", "--count", f"main..{branch}") == "1"
    files = git(origin, "diff", "--name-only", f"main..{branch}").splitlines()
    assert sorted(files) == ["src/tiny/pricing.py", "tests/fixling/test_issue_1.py"]
    assert ">= BULK_THRESHOLD" in git(origin, "show", f"{branch}:src/tiny/pricing.py")


def test_working_clone_is_always_removed(cfg: Config, origin: Path, tmp_path: Path) -> None:
    push(cfg, origin, legit("fix_off_by_one"), tmp_path)
    with pytest.raises(GitHubError):
        push(cfg, origin, hack("edit_existing_test"), tmp_path)
    assert not list(tmp_path.glob("fixling-push-*"))


def test_policy_gate_blocks_before_anything_is_pushed(cfg: Config, origin: Path, tmp_path: Path) -> None:
    with pytest.raises(GitHubError, match=r"policy gate blocked.*protected_path"):
        push(cfg, origin, hack("edit_existing_test"), tmp_path)
    assert git(origin, "branch", "--list", "fixling/*") == ""


def test_secrets_in_the_patch_block_the_push_before_cloning(cfg: Config, origin: Path, tmp_path: Path) -> None:
    patch = legit("fix_off_by_one").replace("BULK_THRESHOLD:", 'BULK_THRESHOLD:  # key = "ghp_' + "Q" * 36 + '"')
    with pytest.raises(GitHubError, match=r"secret scan blocked.*github token") as ei:
        push(cfg, origin, patch, tmp_path)
    assert "Q" * 36 not in str(ei.value), "the error must name the kind of secret, never echo it"
    assert not list(tmp_path.glob("fixling-push-*")) and git(origin, "branch", "--list", "fixling/*") == ""


def test_the_canary_value_is_treated_as_a_secret(cfg: Config, origin: Path, tmp_path: Path) -> None:
    canary = "FIXLING_CANARY_7f3a91c2d4"
    patch = legit("fix_off_by_one").replace("BULK_THRESHOLD:", f"BULK_THRESHOLD:  # {canary}")
    with pytest.raises(GitHubError, match="configured secret"):
        push(cfg, origin, patch, tmp_path, extra_secrets=[canary])


def test_empty_patch_is_refused(cfg: Config, origin: Path, tmp_path: Path) -> None:
    with pytest.raises(GitHubError, match="nothing to push"):
        push(cfg, origin, "  \n", tmp_path)


def test_only_fixling_branches_are_ever_pushed() -> None:
    assert branch_name("01ARZ3NDEKTSV4RRFFQ69G5FAV", 3).startswith("fixling/")
    assert branch_name("01ARZ3NDEKTSV4RRFFQ69G5FAV", None) == "fixling/run-01ARZ3ND"


def test_failed_clone_never_leaks_the_token(cfg: Config, tmp_path: Path) -> None:
    token = "ghp_" + "L" * 36
    with pytest.raises(GitHubError) as ei:
        push(cfg, tmp_path / "missing.git", legit("fix_off_by_one"), tmp_path, token=token)
    assert token not in str(ei.value)


def test_a_patch_that_does_not_apply_fails_cleanly(cfg: Config, origin: Path, tmp_path: Path) -> None:
    stale = legit("fix_off_by_one").replace("-    if qty > BULK_THRESHOLD:", "-    if qty > SOMETHING_ELSE:")
    with pytest.raises(GitHubError):
        push(cfg, origin, stale, tmp_path)
    assert git(origin, "branch", "--list", "fixling/*") == ""


# ---------------------------------------------------------------- scanners
def test_builtin_secret_patterns() -> None:
    assert scan_secrets("ordinary diff text") == []
    assert scan_secrets("x = 'AKIAABCDEFGHIJKLMNOP'") == ["aws access key"]
    assert scan_secrets("-----BEGIN RSA PRIVATE KEY-----") == ["private key"]
    assert scan_secrets('password = "abcdefghijklmnopqrstuvwx"') == ["generic api key assignment"]
    assert scan_secrets("gho_" + "x" * 30) == ["github token"]
    assert scan_secrets("has-the-value", ["the-value", ""]) == ["configured secret #1"]


def test_gitleaks_scan_reports_clean_leak_and_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    class Proc:
        def __init__(self, code: int, err: bytes = b"") -> None:
            self.returncode, self.stderr = code, err

    monkeypatch.setattr(git_push.shutil, "which", lambda _: "docker")
    for code, expected in ((0, True), (1, False), (125, None)):
        monkeypatch.setattr(git_push.subprocess, "run", lambda *a, _c=code, **k: Proc(_c, b"pull access denied"))
        assert gitleaks_scan("diff")[0] is expected
    monkeypatch.setattr(git_push.shutil, "which", lambda _: None)
    assert gitleaks_scan("diff") == (None, "docker CLI not found")


def test_missing_gitleaks_is_a_warning_not_a_failure(
    cfg: Config, origin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(git_push, "gitleaks_scan", lambda patch: (None, "docker CLI not found"))
    _, warnings = push(cfg, origin, legit("fix_off_by_one"), tmp_path, use_gitleaks=True)
    assert warnings and "gitleaks was not run" in warnings[0]


def test_a_gitleaks_hit_blocks_the_push(
    cfg: Config, origin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(git_push, "gitleaks_scan", lambda patch: (False, "gitleaks reported a leak"))
    with pytest.raises(GitHubError, match="gitleaks blocked"):
        push(cfg, origin, legit("fix_off_by_one"), tmp_path, use_gitleaks=True)
