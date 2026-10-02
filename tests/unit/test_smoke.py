import fixling
from fixling.config import Config, glob_match
from fixling.models import IssueSpec


def test_import() -> None:
    assert fixling is not None


def test_config_loads(cfg: Config) -> None:
    assert cfg.models.main_model == "fixling-main"
    assert cfg.models.node("repair").temps(4) == [0.2, 0.5, 0.7, 0.9]
    assert cfg.budgets.repair_candidates == 4
    assert "tests/**" in cfg.policy.protected_globs
    assert cfg.runs_dir.name == "runs"


def test_glob_match() -> None:
    assert glob_match("tests/a/b.py", "tests/**")
    assert glob_match("pkg/sub/conftest.py", "**/conftest.py")
    assert glob_match("conftest.py", "**/conftest.py")
    assert glob_match("Dockerfile.dev", "Dockerfile*")
    assert glob_match("a/b/c.pth", "**/*.pth")
    assert not glob_match("src/tests_util.py", "tests/**")
    assert not glob_match("src/pkg/mod.py", "tests/**")


def test_ambiguity_clamped() -> None:
    spec = IssueSpec(
        summary="s",
        kind="bug",
        expected_behavior="e",
        actual_behavior="a",
        repro_hints=[],
        mentioned_symbols=[],
        acceptance_criteria=[],
        ambiguity=99,
        size_estimate="small",
    )
    assert spec.ambiguity == 10


def test_lmstudio_base_url_can_be_overridden_from_the_environment(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from fixling.config import load_config

    monkeypatch.setenv("LMSTUDIO_BASE_URL", "http://192.168.1.50:1234/v1")
    assert load_config(env_file=str(tmp_path / "none.env")).models.lmstudio.base_url == "http://192.168.1.50:1234/v1"
    monkeypatch.delenv("LMSTUDIO_BASE_URL")
    assert load_config(env_file=str(tmp_path / "none.env")).models.lmstudio.base_url == "http://localhost:1234/v1"
