from __future__ import annotations

import io
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from fixling.config import Config
from fixling.errors import WorkspaceError
from fixling.repo.workspace import auth_header_args, build_tree_tar, copy_local, fetch_repo
from fixling.verify.policy import PolicyGate


def entries(tar_bytes: bytes) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tf:
        return {m.name: m for m in tf.getmembers()}


def gate(cfg: Config) -> PolicyGate:
    return PolicyGate(cfg.policy)


def test_protected_files_and_their_directories_are_root_owned(tiny_repo: Path, cfg: Config) -> None:
    ents = entries(build_tree_tar(tiny_repo, gate(cfg)))
    for name in ("tests/test_pricing.py", "tests/test_textutil.py"):
        assert (ents[name].uid, ents[name].gid, ents[name].mode) == (0, 0, 0o444)
    # the directory holding protected files must not be writable, or the files could be deleted/replaced
    assert (ents["tests"].uid, ents["tests"].mode, ents["tests"].isdir()) == (0, 0o555, True)
    # ... except the generated-test directory, which stays agent-owned inside it
    assert (ents["tests/fixling"].uid, ents["tests/fixling"].mode) == (10001, 0o755)


def test_source_files_are_agent_owned_and_editable(tiny_repo: Path, cfg: Config) -> None:
    ents = entries(build_tree_tar(tiny_repo, gate(cfg)))
    assert (ents["src/tiny/pricing.py"].uid, ents["src/tiny/pricing.py"].mode) == (10001, 0o644)
    assert (ents["src/tiny"].uid, ents["src/tiny"].mode) == (10001, 0o755)
    assert ents["pyproject.toml"].uid == 10001  # not a protected glob (only some sections are)


def test_no_git_dir_or_noise_in_the_tar(tiny_repo: Path, cfg: Config) -> None:
    (tiny_repo / ".git").mkdir()
    (tiny_repo / ".git" / "config").write_text("[core]\n")
    (tiny_repo / "node_modules").mkdir()
    (tiny_repo / "node_modules" / "x.js").write_text("1")
    (tiny_repo / "src" / "tiny" / "__pycache__").mkdir()
    (tiny_repo / "src" / "tiny" / "__pycache__" / "a.pyc").write_bytes(b"\0")
    (tiny_repo / "big.bin").write_bytes(b"0" * 1_000_001)
    names = set(entries(build_tree_tar(tiny_repo, gate(cfg))))
    assert not any(n.startswith((".git", "node_modules")) or "__pycache__" in n or n == "big.bin" for n in names)


def test_nested_protected_file_locks_every_ancestor(tiny_repo: Path, cfg: Config) -> None:
    (tiny_repo / "src" / "tiny" / "conftest.py").write_text("x = 1\n")
    ents = entries(build_tree_tar(tiny_repo, gate(cfg)))
    assert ents["src/tiny/conftest.py"].uid == 0
    assert (ents["src/tiny"].uid, ents["src/tiny"].mode) == (0, 0o555)
    assert (ents["src"].uid, ents["src"].mode) == (0, 0o555)


def test_generated_dir_is_created_even_without_a_tests_directory(tmp_path: Path, cfg: Config) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "m.py").write_text("x = 1\n")
    ents = entries(build_tree_tar(tmp_path, gate(cfg)))
    assert ents["tests"].uid == 0 and ents["tests/fixling"].uid == 10001


def test_extra_files_must_live_in_the_generated_dir(tiny_repo: Path, cfg: Config) -> None:
    ents = entries(
        build_tree_tar(tiny_repo, gate(cfg), {"tests/fixling/test_issue_1.py": "def test_x():\n    assert 1\n"})
    )
    assert (ents["tests/fixling/test_issue_1.py"].uid, ents["tests/fixling/test_issue_1.py"].mode) == (10001, 0o644)
    with pytest.raises(WorkspaceError, match="outside"):
        build_tree_tar(tiny_repo, gate(cfg), {"src/tiny/evil.py": "x"})


def test_executable_bit_is_preserved(tiny_repo: Path, cfg: Config) -> None:
    script = tiny_repo / "run.sh"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    mode = entries(build_tree_tar(tiny_repo, gate(cfg)))["run.sh"].mode
    if os.name != "nt":  # Windows has no meaningful exec bit
        assert mode == 0o755


@pytest.mark.skipif(os.name == "nt", reason="creating symlinks needs privileges on Windows")
def test_symlink_escaping_the_tree_is_rejected(tiny_repo: Path, cfg: Config) -> None:
    (tiny_repo / "src" / "link").symlink_to("/etc/passwd")
    with pytest.raises(WorkspaceError, match="outside the repository"):
        build_tree_tar(tiny_repo, gate(cfg))


@pytest.mark.skipif(os.name == "nt", reason="creating symlinks needs privileges on Windows")
def test_symlink_inside_the_tree_is_kept(tiny_repo: Path, cfg: Config) -> None:
    (tiny_repo / "src" / "alias.py").symlink_to("tiny/pricing.py")
    ents = entries(build_tree_tar(tiny_repo, gate(cfg)))
    assert ents["src/alias.py"].issym() and ents["src/alias.py"].linkname == "tiny/pricing.py"


# ---------------------------------------------------------------- fetching
def init_repo(path: Path) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "a",
        "GIT_AUTHOR_EMAIL": "a@b",
        "GIT_COMMITTER_NAME": "a",
        "GIT_COMMITTER_EMAIL": "a@b",
    }
    path.mkdir(parents=True)
    (path / "a.py").write_text("x = 1\n")
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-qm", "one"]):
        subprocess.run(["git", *args], cwd=path, env=env, check=True, capture_output=True)
    (path / "a.py").write_text("x = 2\n")
    for args in (["add", "-A"], ["commit", "-qm", "two"]):
        subprocess.run(["git", *args], cwd=path, env=env, check=True, capture_output=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_copy_local_at_a_commit_and_working_tree(tmp_path: Path) -> None:
    src = tmp_path / "src"
    head = init_repo(src)
    first = subprocess.run(
        ["git", "rev-parse", "HEAD~1"], cwd=src, capture_output=True, text=True, check=True
    ).stdout.strip()
    sha = copy_local(src, tmp_path / "at_first", first)
    assert sha == first and (tmp_path / "at_first" / "a.py").read_text() == "x = 1\n"
    assert not (tmp_path / "at_first" / ".git").exists()
    sha2 = copy_local(src, tmp_path / "wt")
    assert (
        sha2 == head and (tmp_path / "wt" / "a.py").read_text() == "x = 2\n" and not (tmp_path / "wt" / ".git").exists()
    )


def test_fetch_repo_shallow_clone_strips_git(tmp_path: Path) -> None:
    src = tmp_path / "origin"
    head = init_repo(src)
    sha = fetch_repo("o/r", tmp_path / "clone", clone_url=src.as_uri())
    assert sha == head and (tmp_path / "clone" / "a.py").read_text() == "x = 2\n"
    assert not (tmp_path / "clone" / ".git").exists()


def test_fetch_failure_never_leaks_the_token(tmp_path: Path) -> None:
    token = "ghp_" + "S" * 36
    with pytest.raises(WorkspaceError) as ei:
        fetch_repo("o/r", tmp_path / "x", token, clone_url=(tmp_path / "does-not-exist").as_uri())
    assert token not in str(ei.value)


def test_auth_header_is_per_command_basic_auth() -> None:
    assert auth_header_args("") == []
    args = auth_header_args("tok")
    assert args[0] == "-c" and args[1].startswith("http.extraheader=AUTHORIZATION: basic ") and "tok" not in args[1]
