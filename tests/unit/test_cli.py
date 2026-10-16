from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from fixling.cli import app, summarize_event
from fixling.config import Config, get_config
from fixling.models import Event
from fixling.services import build_services

runner = CliRunner()


@pytest.fixture
def cli_cfg(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> Config:
    """Point the CLI's cached config at the temp-dir config from the ``cfg`` fixture."""
    monkeypatch.setattr("fixling.cli.get_config", lambda: cfg)
    monkeypatch.setattr("fixling.services.get_config", lambda: cfg)
    get_config.cache_clear()
    return cfg


def ev(type_: str, **payload: Any) -> Event:
    return Event(run_id="r", seq=1, ts=datetime.now(UTC), type=type_, source="system", payload=payload)  # type: ignore[arg-type]


def test_help_lists_every_command() -> None:
    out = runner.invoke(app, ["--help"]).output
    for command in ("doctor", "run", "replay", "serve", "bench-micro", "eval", "eval-report"):
        assert command in out


def test_run_needs_exactly_one_issue_source() -> None:
    both_missing = runner.invoke(app, ["run", "--repo", "."])
    # Typer renders usage errors inside a coloured box: strip ANSI codes and box drawing before matching.
    plain = re.sub(r"\s+", " ", re.sub(r"\x1b\[[0-9;]*m|[─-╿]", " ", both_missing.output))
    # (Rich also splits "--issue" into separate styled spans, so match the stable words, not the dashes.)
    assert both_missing.exit_code == 2 and "give exactly one of" in plain and "issue" in plain and "file" in plain
    both_given = runner.invoke(app, ["run", "--repo", ".", "--issue", "3", "--issue-file", "x.md"])
    assert both_given.exit_code != 0


def test_replay_prints_the_timeline(cli_cfg: Config) -> None:
    services = build_services(cli_cfg)
    rid = services.runs.create_run(repo="o/r", issue_number=7) or ""
    services.events.append(
        rid, "spec", "llm", {"kind": "bug", "summary": "bulk_price is wrong", "ambiguity": 1, "size_estimate": "small"}
    )
    services.events.append(rid, "outcome", "system", {"status": "verified_fix", "confidence": "high", "reason": "ok"})
    result = runner.invoke(app, ["replay", rid], terminal_width=200)
    assert result.exit_code == 0
    assert "o/r#7" in result.output and "bulk_price is wrong" in result.output and "verified_fix" in result.output


def test_replay_unknown_run(cli_cfg: Config) -> None:
    result = runner.invoke(app, ["replay", "nope"])
    assert result.exit_code == 2 and "unknown run" in result.output


def test_bench_micro_reports_an_unreachable_lm_studio_clearly(cli_cfg: Config) -> None:
    cli_cfg.models.lmstudio.base_url = "http://127.0.0.1:9/v1"  # nothing listens there
    result = runner.invoke(app, ["bench-micro"])
    assert result.exit_code == 2 and "lms server start" in result.output


def test_eval_rejects_unknown_and_unsupported_configurations(cli_cfg: Config) -> None:
    unknown = runner.invoke(app, ["eval", "--config", "does_not_exist"])
    assert unknown.exit_code == 2 and "unknown configuration" in unknown.output
    unsupported = runner.invoke(app, ["eval", "--config", "bash_loop"])
    assert unsupported.exit_code == 2 and "not available yet" in unsupported.output


@pytest.mark.parametrize(
    ("event", "needle"),
    [
        (ev("spec", kind="bug", summary="s", ambiguity=2, size_estimate="small"), "ambiguity 2"),
        (ev("triage", decision="clarify", reasons=["too vague"]), "clarify - too vague"),
        (ev("env", ok=False, reason="collection errors"), "FAILED: collection errors"),
        (ev("env", ok=True, passed=81, flaky=["t"], image="img"), "baseline: 81 passed, 1 flaky"),
        (ev("retrieval", chosen_files=["a.py"], targets=["a.py::f"]), "a.py::f"),
        (ev("repro", ok=True, test_ids=["t::x"]), "valid reproduction"),
        (ev("repro", ok=False, attempts=3), "no valid reproduction after 3"),
        (ev("test_run", label="baseline 1/3", passed=81, failed=[], errors=[]), "81 passed, 0 failed"),
        (ev("llm_call", node="repair", temperature=0.2, tokens_in=900, tokens_out=120, latency_ms=4000), "900+120 tok"),
        (ev("candidate", id="r0c1", temperature=0.5, repro_passed=True, regressions=[], apply_error="x"), "ERROR x"),
        (ev("selection", round=0, best="r0c0", route="clean_verify"), "best r0c0 -> clean_verify"),
        (ev("verify", f2p_ok=True, p2p_ok=True, shuffled_ok=True, passed=82), "f2p=True p2p=True"),
        (ev("outcome", status="repro_only", confidence="none", reason="no fix"), "repro_only / confidence none"),
        (ev("error", error="boom"), "boom"),
        (ev("gate_finding", severity="block", rule="protected_path", detail="tests/x.py"), "block protected_path"),
        (ev("git", branch="fixling/issue-3-abc"), "branch=fixling/issue-3-abc"),
    ],
)
def test_summarize_event(event: Event, needle: str) -> None:
    assert needle in summarize_event(event)


def test_cli_module_has_no_import_side_effects(tmp_path: Path) -> None:
    import importlib

    import fixling.cli

    importlib.reload(fixling.cli)  # importing must not touch Docker, LM Studio or the filesystem
