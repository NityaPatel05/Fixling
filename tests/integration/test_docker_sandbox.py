"""Sandbox hardening and env setup against a real Docker daemon (``pytest -m docker``)."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import docker
import pytest

from fixling.config import Config
from fixling.db import Database
from fixling.errors import SandboxError
from fixling.events.store import EventStore
from fixling.repo.env_setup import setup_env
from fixling.repo.workspace import build_tree_tar
from fixling.runs import RunStore
from fixling.sandbox.docker_sandbox import DockerSandbox, SandboxLimits
from fixling.sandbox.images import ensure_base, ensure_env_image, gather_requirements
from fixling.sandbox.reaper import reap_once, reconcile
from fixling.verify.policy import PolicyGate
from fixling.verify.runner import classify_failure, is_green, run_tests

pytestmark = pytest.mark.docker


@pytest.fixture(scope="session")
def client() -> docker.DockerClient:
    try:
        c = docker.from_env()
        c.ping()
    except Exception as exc:
        pytest.skip(f"Docker daemon not available: {exc}")
    ensure_base(c)
    return c


@pytest.fixture(scope="session")
def tiny_image(client: docker.DockerClient) -> str:
    from tests.conftest import FIXTURE_REPOS

    tag, _ = ensure_env_image(client, "tiny", FIXTURE_REPOS / "tiny_repo")
    return tag


@pytest.fixture
def store(tmp_path: Path) -> RunStore:
    return RunStore(Database(tmp_path / "t.sqlite"))


@pytest.fixture
def sb(
    client: docker.DockerClient, tiny_image: str, tiny_repo: Path, cfg: Config, store: RunStore
) -> Iterator[DockerSandbox]:
    box = DockerSandbox.create(client, tiny_image, "testrun0001", "work", runs=store)
    box.put_tree(build_tree_tar(tiny_repo, PolicyGate(cfg.policy)))
    try:
        yield box
    finally:
        box.close()


def agent(sb: DockerSandbox, cmd: str, timeout: int = 20) -> tuple[int, str]:
    r = sb.exec(cmd, timeout)
    return r.exit_code, (r.stdout + r.stderr).strip()


# ---------------------------------------------------------------- hardening (spec Part F items 1-3)
def test_container_is_hardened_as_specified(sb: DockerSandbox, monkeypatch: pytest.MonkeyPatch) -> None:
    rep = sb.hardening_report()
    assert rep["user"] == "10001:10001"
    assert rep["cap_drop"] == ["ALL"] and not rep["cap_add"]
    assert "no-new-privileges:true" in rep["security_opt"]
    assert rep["read_only"] is True and rep["network_mode"] == "none" and rep["privileged"] is False
    assert rep["pids_limit"] == 512 and rep["memory"] == 4 * 1024**3
    assert not rep["binds"], "no host directories may be mounted"
    assert sorted(rep["mounts"]) == ["/gitbase", "/tmp", "/workspace"]


def test_agent_has_no_capabilities_and_no_way_to_get_root(sb: DockerSandbox) -> None:
    _, out = agent(sb, "id -u; grep -E 'CapEff|CapBnd' /proc/self/status")
    assert out.splitlines()[0] == "10001"
    assert all(line.split()[-1] == "0000000000000000" for line in out.splitlines()[1:])
    assert agent(sb, "su -c id 2>&1 </dev/null | head -1")[1].lower().find("uid=0") == -1


def test_no_secrets_in_the_container_environment(
    client: docker.DockerClient, tiny_image: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "Z" * 36)
    box = DockerSandbox.create(client, tiny_image, "testrun0002", "work")
    try:
        env = " ".join(box.hardening_report()["env"])
        assert "GITHUB_TOKEN" not in env and "ghp_" not in env
        assert "ghp_" not in agent(box, "env")[1]
    finally:
        box.close()


def test_network_is_unreachable(sb: DockerSandbox) -> None:
    code, out = agent(sb, "python -c \"import socket; socket.create_connection(('1.1.1.1', 53), 2)\"")
    assert code != 0 and "Errno" in out  # e.g. [Errno 101] Network is unreachable
    assert agent(sb, "python -c \"import socket; socket.create_connection(('host.docker.internal', 1234), 2)\"")[0] != 0


def test_rootfs_is_read_only_but_tmp_is_writable(sb: DockerSandbox) -> None:
    assert agent(sb, "touch /usr/x")[0] != 0
    assert agent(sb, "touch /tmp/x && echo ok")[1] == "ok"


# ---------------------------------------------------------------- test protection (layer L1)
def test_protected_tests_cannot_be_modified_deleted_replaced_or_moved(sb: DockerSandbox) -> None:
    for cmd in (
        "echo 'assert True' >> tests/test_pricing.py",
        "rm tests/test_pricing.py",
        "touch tests/test_new.py",
        "mv tests tests_old",
        "mv tests/test_pricing.py tests/fixling/stolen.py",
        "rm -rf tests",
        "cp /dev/null tests/test_pricing.py",
    ):
        assert agent(sb, cmd)[0] != 0, f"should have been denied: {cmd}"
    assert "assert bulk_price(5, 5.0) == 25.0" in sb.read_file("tests/test_pricing.py")


def test_no_new_top_level_entries_and_no_dotgit(sb: DockerSandbox) -> None:
    assert agent(sb, "touch pytest.ini")[0] != 0 and agent(sb, "mkdir evil")[0] != 0
    assert agent(sb, "mv src src2")[0] != 0
    assert ".git" not in agent(sb, "ls -a /workspace")[1].split()
    assert agent(sb, "touch /gitbase/x")[0] != 0 and agent(sb, "rm -rf /gitbase/refs")[0] != 0


def test_generated_tests_and_source_edits_are_allowed(sb: DockerSandbox) -> None:
    assert agent(sb, "echo 'def test_x():\n    assert 1' > tests/fixling/test_x.py && echo ok")[1] == "ok"
    assert agent(sb, "sed -i 's/>= 10/> 10/' src/tiny/pricing.py && echo ok")[1] == "ok"


# ---------------------------------------------------------------- exec / files / diff
def test_exec_basics_and_workdir(sb: DockerSandbox) -> None:
    r = sb.exec("pwd; echo out; echo err >&2; exit 3", 20)
    assert (r.exit_code, r.stdout.split()[:2], r.stderr.strip(), r.timed_out) == (
        3,
        ["/workspace", "out"],
        "err",
        False,
    )
    assert sb.exec("pwd", 20, workdir="/tmp").stdout.strip() == "/tmp"


def test_exec_timeout_kills_the_command(sb: DockerSandbox) -> None:
    started = time.monotonic()
    r = sb.exec("sleep 60", 2)
    assert r.timed_out and r.exit_code == 137 and time.monotonic() - started < 15
    assert sb.exec("echo still alive", 10).stdout.strip() == "still alive"  # the sandbox itself survives


def test_write_and_read_roundtrip_including_large_and_unicode(sb: DockerSandbox) -> None:
    text = "héllo ✓\n" + ("x = 'a' * 100\n" * 20_000)  # ~300 KB: forces several base64 chunks
    sb.write_file("tests/fixling/test_big.py", text)
    assert sb.read_file("tests/fixling/test_big.py") == text
    assert sb.read_file("/workspace/src/tiny/__init__.py") == ""


def test_writing_a_protected_path_raises(sb: DockerSandbox) -> None:
    with pytest.raises(SandboxError, match="cannot write"):
        sb.write_file("tests/test_pricing.py", "assert True\n")
    with pytest.raises(SandboxError):
        sb.write_file("pytest.ini", "[pytest]\n")
    with pytest.raises(SandboxError, match="root-owned"):
        sb.write_file("x.py", "x", owner="root")


def test_read_missing_file_raises(sb: DockerSandbox) -> None:
    with pytest.raises(SandboxError, match="cannot read"):
        sb.read_file("nope.py")


def test_diff_covers_edits_new_files_and_ignores_pristine_state(sb: DockerSandbox) -> None:
    assert sb.diff() == ""
    sb.write_file("src/tiny/pricing.py", sb.read_file("src/tiny/pricing.py").replace("qty > BULK", "qty >= BULK"))
    sb.write_file("tests/fixling/test_issue_1.py", "def test_x():\n    assert 1 == 1\n")
    diff = sb.diff()
    assert "+    if qty >= BULK_THRESHOLD:" in diff and "-    if qty > BULK_THRESHOLD:" in diff
    assert "new file mode" in diff and "tests/fixling/test_issue_1.py" in diff


def test_diff_survives_the_agent_trying_to_hide_changes(sb: DockerSandbox) -> None:
    # the agent cannot reach /gitbase, so it cannot rewrite or replace the base commit
    agent(sb, "cd /workspace && git init -q 2>&1; git commit --allow-empty -qm hijack 2>&1; true")
    sb.write_file("src/tiny/textutil.py", "def slugify(t):\n    return t\n")
    assert "textutil.py" in sb.diff()


# ---------------------------------------------------------------- the runner inside the real sandbox
def test_baseline_suite_is_green_in_the_sandbox(sb: DockerSandbox) -> None:
    run = run_tests(sb, timeout=120)
    assert is_green(run) and run.collected == 6


def test_shuffled_and_timeout_classification_work_on_linux(sb: DockerSandbox) -> None:
    assert is_green(run_tests(sb, shuffled=True, timeout=120))
    sb.write_file("tests/fixling/test_slow.py", "import time\n\ndef test_slow():\n    time.sleep(5)\n")
    run = run_tests(sb, ["tests/fixling/test_slow.py"], timeout=60, per_test_timeout=1)
    (tid,) = run.failed
    assert classify_failure(tid, run) == "timeout"


def test_sys_exit_conftest_inside_the_sandbox_is_not_green(sb: DockerSandbox) -> None:
    # conftest in the generated dir IS writable by the agent; the runner must still refuse to call it a pass
    sb.write_file("tests/fixling/conftest.py", "import sys\nsys.exit(0)\n")
    assert not is_green(run_tests(sb, timeout=120))


def test_close_removes_container_and_registry_row(
    client: docker.DockerClient, tiny_image: str, store: RunStore
) -> None:
    box = DockerSandbox.create(client, tiny_image, "testrun0003", "verify", runs=store)
    cid = box.id
    assert [s["container_id"] for s in store.live_sandboxes("testrun0003")] == [cid]
    box.close()
    box.close()  # idempotent
    assert store.live_sandboxes("testrun0003") == []
    with pytest.raises(docker.errors.NotFound):
        client.containers.get(cid)


# ---------------------------------------------------------------- reaper
def test_reaper_removes_only_expired_containers(client: docker.DockerClient, tiny_image: str, store: RunStore) -> None:
    keep = DockerSandbox.create(client, tiny_image, "testrun0004", "work", runs=store)
    old = DockerSandbox.create(client, tiny_image, "testrun0005", "work", runs=store, limits=SandboxLimits(ttl_s=-5))
    try:
        removed = reap_once(client, store)
        assert old.id in removed and keep.id not in removed
        assert client.containers.get(keep.id).status in ("running", "created")
        assert old.id not in [s["container_id"] for s in store.live_sandboxes()]
    finally:
        keep.close()
        old.close()


def test_reconcile_removes_orphans_and_marks_missing_rows(
    client: docker.DockerClient, tiny_image: str, store: RunStore
) -> None:
    orphan = DockerSandbox.create(client, tiny_image, "testrun0006", "work")  # not in the DB: a crash leftover
    store.register_sandbox("deadbeef" * 8, "testrun0007", "work")  # DB row without a container
    try:
        res = reconcile(client, store)
        assert orphan.id in res["orphans_removed"] and ("deadbeef" * 8) in res["marked_removed"]
        with pytest.raises(docker.errors.NotFound):
            client.containers.get(orphan.id)
    finally:
        orphan.close()


# ---------------------------------------------------------------- images and env setup
def test_requirements_are_gathered_from_pyproject_and_requirements_files(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = ["requests>=2", "pytest-randomly"]\n'
        '[project.optional-dependencies]\ntest = ["hypothesis"]\ndocs = ["sphinx"]\n'
        '[dependency-groups]\ndev = ["ruff"]\n',
        encoding="utf-8",
    )
    (tmp_path / "requirements-dev.txt").write_text(
        "-r base.txt\nattrs  # comment\n./local\ngit+https://x/y.git\n", encoding="utf-8"
    )
    assert gather_requirements(tmp_path) == ["requests>=2", "hypothesis", "ruff", "attrs"]


def test_env_image_is_cached_by_requirements(client: docker.DockerClient, tiny_image: str, tiny_repo: Path) -> None:
    again, reqs = ensure_env_image(client, "tiny", tiny_repo)
    assert again == tiny_image and reqs == []


def test_env_image_installs_dependencies_at_build_time(client: docker.DockerClient, tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "d"\nversion = "0"\ndependencies = ["six"]\n', encoding="utf-8"
    )
    tag, reqs = ensure_env_image(client, "withdeps", tmp_path)
    assert reqs == ["six"]
    out = client.containers.run(
        tag, ["python", "-c", "import six; print(six.__version__)"], remove=True, network_mode="none"
    )
    assert out.decode().strip()


def test_setup_env_records_a_stable_baseline(
    client: docker.DockerClient, tiny_repo: Path, cfg: Config, tmp_path: Path, store: RunStore
) -> None:
    events = EventStore(store.db, tmp_path / "runs")
    result, box = setup_env(
        client=client, budgets=cfg.budgets, events=events, runs=store, run_id="testrun0008",
        repo_key="tiny", src_dir=tiny_repo, gate=PolicyGate(cfg.policy),
    )  # fmt: skip
    try:
        assert result.ok and box is not None and result.baseline is not None
        assert len(result.baseline.passed) == 6 and not result.baseline.flaky and not result.baseline.failed
        assert result.baseline.runs == cfg.budgets.baseline_runs == 3
        types = [e.type for e in events.list("testrun0008")]
        assert types.count("test_run") == 3 and types[-1] == "env"
        assert events.list("testrun0008")[-1].payload["ok"] is True
    finally:
        if box:
            box.close()


def test_setup_env_detects_flaky_tests(
    client: docker.DockerClient, tiny_repo: Path, cfg: Config, tmp_path: Path, store: RunStore
) -> None:
    # passes on odd runs, fails on even ones: the counter file persists in /tmp across runs in one container
    (tiny_repo / "tests" / "test_flaky.py").write_text(
        "import pathlib\n\n\ndef test_flaky():\n    p = pathlib.Path('/tmp/flaky_count')\n"
        "    n = int(p.read_text()) + 1 if p.exists() else 1\n    p.write_text(str(n))\n    assert n % 2 == 1\n",
        encoding="utf-8",
    )
    events = EventStore(store.db, tmp_path / "runs")
    result, box = setup_env(
        client=client, budgets=cfg.budgets, events=events, runs=store, run_id="testrun0009",
        repo_key="tiny", src_dir=tiny_repo, gate=PolicyGate(cfg.policy),
    )  # fmt: skip
    try:
        assert result.ok and result.baseline is not None
        assert result.baseline.flaky == ["tests/test_flaky.py::test_flaky"]
        assert len(result.baseline.passed) == 6
    finally:
        if box:
            box.close()


def test_setup_env_reports_a_broken_environment(
    client: docker.DockerClient, tiny_repo: Path, cfg: Config, tmp_path: Path, store: RunStore
) -> None:
    (tiny_repo / "src" / "tiny" / "pricing.py").write_text("def broken(:\n", encoding="utf-8")
    events = EventStore(store.db, tmp_path / "runs")
    result, box = setup_env(
        client=client, budgets=cfg.budgets, events=events, runs=store, run_id="testrun0010",
        repo_key="tiny", src_dir=tiny_repo, gate=PolicyGate(cfg.policy),
    )  # fmt: skip
    assert not result.ok and box is None and "collection" in result.reason.lower()
    assert store.live_sandboxes("testrun0010") == []  # nothing leaked
    assert events.list("testrun0010")[-1].payload["ok"] is False
