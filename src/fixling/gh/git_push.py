"""Trusted, host-side branch push (spec E19, Part F).

This is the only place a token is ever used with git, and it never touches the sandbox. Before anything leaves the
machine the patch is scanned for secrets and re-checked by the policy gate against the real repository, and only
``fixling/*`` branches can be pushed.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path

from fixling.config import PolicyCfg
from fixling.errors import GitHubError, WorkspaceError
from fixling.repo.workspace import _git, _rmtree, auth_header_args
from fixling.verify.policy import PolicyGate, has_block
from fixling.verify.secrets import SECRET_PATTERNS, scan_secrets

BRANCH_PREFIX = "fixling/"
BOT_NAME = "Fixling Bot"
BOT_EMAIL = "fixling@users.noreply.github.com"
__all__ = ["SECRET_PATTERNS", "branch_name", "gitleaks_scan", "push_branch", "scan_secrets"]


def gitleaks_scan(patch: str, image: str = "zricethezav/gitleaks:latest") -> tuple[bool | None, str]:
    """Second opinion from gitleaks in a network-less container fed over stdin (no host mounts).

    Returns ``(clean, detail)``; ``clean`` is ``None`` when the tool could not run (Docker or image missing).
    """
    docker = shutil.which("docker")
    if not docker:
        return None, "docker CLI not found"
    try:
        proc = subprocess.run(
            [docker, "run", "--rm", "-i", "--network", "none", image, "detect", "--pipe", "--no-banner", "--redact"],
            input=patch.encode("utf-8"),
            capture_output=True,
            timeout=120,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return None, f"gitleaks did not run: {type(exc).__name__}"
    err = proc.stderr.decode("utf-8", errors="replace")
    if proc.returncode == 0:
        return True, "no leaks found"
    if proc.returncode == 1:
        return False, "gitleaks reported a leak"
    return None, f"gitleaks unavailable: {err.strip()[-160:]}"


def branch_name(run_id: str, issue_number: int | None) -> str:
    return f"{BRANCH_PREFIX}issue-{issue_number}-{run_id[:8]}" if issue_number else f"{BRANCH_PREFIX}run-{run_id[:8]}"


def push_branch(
    *,
    repo: str,
    token: str,
    base_branch: str,
    run_id: str,
    issue_number: int | None,
    issue_title: str,
    patch: str,
    policy: PolicyCfg,
    extra_secrets: Iterable[str] = (),
    clone_url: str | None = None,
    use_gitleaks: bool = True,
    work_root: Path | None = None,
) -> tuple[str, list[str]]:
    """Clone, re-check, commit and push ``patch`` as a new ``fixling/*`` branch. Returns ``(branch, warnings)``."""
    warnings: list[str] = []
    if not patch.strip():
        raise GitHubError("there is nothing to push: the patch is empty")
    leaks = scan_secrets(patch, [token, *extra_secrets])
    if leaks:
        raise GitHubError("secret scan blocked the push: " + ", ".join(leaks))
    if use_gitleaks:
        clean, detail = gitleaks_scan(patch)
        if clean is False:
            raise GitHubError(f"gitleaks blocked the push: {detail}")
        if clean is None:
            warnings.append(f"gitleaks was not run ({detail}); the built-in secret scan passed")

    branch = branch_name(run_id, issue_number)
    if not branch.startswith(BRANCH_PREFIX) or branch == base_branch:
        raise GitHubError(f"refusing to push to {branch!r}")
    url = clone_url or f"https://github.com/{repo}.git"
    tmp = Path(tempfile.mkdtemp(prefix="fixling-push-", dir=work_root))
    clone = tmp / "clone"
    try:
        try:
            _git(
                [
                    *auth_header_args(token),
                    "clone",
                    "--depth",
                    "1",
                    "--no-tags",
                    "--branch",
                    base_branch,
                    url,
                    str(clone),
                ],
                redact=token,
            )
            gate = PolicyGate(policy, base_dir=clone, secrets=[token, *extra_secrets])
            findings = gate.check_diff(patch)
            if has_block(findings):
                raise GitHubError(
                    "the policy gate blocked the push: "
                    + "; ".join(f"{f.rule} ({f.detail})" for f in findings if f.severity == "block")
                )
            patch_file = tmp / "change.diff"
            patch_file.write_text(patch if patch.endswith("\n") else patch + "\n", encoding="utf-8", newline="")
            _git(["checkout", "-q", "-b", branch], cwd=clone)
            _git(["apply", "--index", "--whitespace=nowarn", str(patch_file)], cwd=clone)
            message = f"fixling: {issue_title.strip()[:100]}" + (f" (#{issue_number})" if issue_number else "")
            _git(
                ["-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}", "commit", "-q", "-m", message],
                cwd=clone,
            )
            _git([*auth_header_args(token), "push", "origin", f"HEAD:refs/heads/{branch}"], cwd=clone, redact=token)
        except WorkspaceError as exc:  # git failures carry already-redacted text
            raise GitHubError(str(exc)) from exc
    finally:
        _rmtree(tmp)
    return branch, warnings
