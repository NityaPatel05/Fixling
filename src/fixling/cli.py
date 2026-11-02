"""Fixling command line (spec E22)."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from fixling.config import Config, get_config
from fixling.events.store import EventStore
from fixling.models import Event
from fixling.repo.workspace import copy_local, fetch_repo

app = typer.Typer(
    help="Fixling: local small-model agent that turns GitHub issues into tested draft PRs.", no_args_is_help=True
)
console = Console()


@app.callback()
def main() -> None:
    """Fixling command line."""


# ---------------------------------------------------------------- event rendering
def summarize_event(e: Event) -> str:
    p = e.payload
    t = e.type
    if t == "spec":
        return (
            f"{p.get('kind')}: {p.get('summary', '')[:90]}  (ambiguity {p.get('ambiguity')}, {p.get('size_estimate')})"
        )
    if t == "triage":
        return f"{p.get('decision')} {('- ' + '; '.join(p.get('reasons', []))) if p.get('reasons') else ''}".strip()
    if t == "env":
        if p.get("ok"):
            return f"baseline: {p.get('passed')} passed, {len(p.get('flaky', []))} flaky, image {p.get('image')}"
        return f"FAILED: {str(p.get('reason', ''))[:120]}"
    if t == "retrieval":
        return f"files {p.get('chosen_files')} targets {p.get('targets')}"
    if t == "repro":
        return (
            f"valid reproduction {p.get('test_ids')}"
            if p.get("ok")
            else f"no valid reproduction after {p.get('attempts')} attempts"
        )
    if t == "test_run":
        failed, errors = len(p.get("failed", [])), len(p.get("errors", []))
        return f"{p.get('label')}: {p.get('passed')} passed, {failed} failed, {errors} errors"
    if t == "llm_call":
        tokens = f"{p.get('tokens_in')}+{p.get('tokens_out')}"
        return f"{p.get('node')} t={p.get('temperature')} {tokens} tok {p.get('latency_ms')}ms"
    if t == "candidate":
        err = p.get("apply_error") or p.get("llm_error")
        return (
            f"{p.get('id')} t={p.get('temperature')} repro_passed={p.get('repro_passed')} "
            f"regressions={len(p.get('regressions', []))}" + (f" ERROR {str(err)[:60]}" if err else "")
        )
    if t == "selection":
        return f"round {p.get('round')}: best {p.get('best')} -> {p.get('route')}"
    if t == "verify":
        return f"f2p={p.get('f2p_ok')} p2p={p.get('p2p_ok')} shuffled={p.get('shuffled_ok')} ({p.get('passed')} passed)"
    if t == "outcome":
        return f"{p.get('status')} / confidence {p.get('confidence')}: {str(p.get('reason', ''))[:100]}"
    if t in ("error", "budget"):
        return str(p.get("error") or p.get("stopped") or p.get("warning") or p)[:140]
    if t == "gate_finding":
        return f"{p.get('severity')} {p.get('rule')}: {p.get('detail', '')[:80]}"
    return ", ".join(f"{k}={v}" for k, v in list(p.items())[:3] if k != "text")[:140]


class EventTail(threading.Thread):
    """Prints new events of a run while the pipeline works in the main thread."""

    def __init__(self, events: EventStore, run_id: str, verbose: bool = False) -> None:
        super().__init__(daemon=True)
        self.events, self.run_id, self.verbose = events, run_id, verbose
        self._stop_event = threading.Event()
        self.last = 0

    def drain(self) -> None:
        for e in self.events.list(self.run_id, self.last):
            self.last = e.seq
            if e.type == "llm_call" and not self.verbose:
                continue
            console.print(f"[dim]#{e.seq:<4}[/dim] [cyan]{e.type:<14}[/cyan] {summarize_event(e)}", highlight=False)

    def run(self) -> None:
        while not self._stop_event.wait(0.5):
            self.drain()

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=2)
        self.drain()


# ---------------------------------------------------------------- commands
@app.command()
def doctor(
    deep: bool = typer.Option(False, "--deep", help="Also start a sandbox and prove it has no network."),
) -> None:
    """Check LM Studio, Docker, the base image and (with --deep) sandbox isolation."""
    cfg = get_config()
    table = Table(show_header=True, header_style="bold")
    for col in ("check", "status", "detail"):
        table.add_column(col)
    failed: list[str] = []

    def row(name: str, ok: bool, detail: str) -> None:
        if not ok:
            failed.append(name)
        table.add_row(name, "[green]ok[/green]" if ok else "[red]FAIL[/red]", detail)

    from fixling.errors import LLMUnavailable
    from fixling.llm.client import LLM

    try:
        LLM(cfg.models).health()
        row(
            "LM Studio",
            True,
            f"{cfg.models.lmstudio.base_url}: '{cfg.models.main_model}' and '{cfg.models.embed_model}' loaded",
        )
    except LLMUnavailable as exc:
        row("LM Studio", False, str(exc))

    client = None
    try:
        from fixling.sandbox.images import BASE_TAG, get_client

        client = get_client()
        client.ping()
        row("Docker", True, f"daemon {client.version().get('Version')}")
        try:
            client.images.get(BASE_TAG)
            row("Base image", True, BASE_TAG)
        except Exception:
            row(
                "Base image",
                False,
                f"{BASE_TAG} is missing; it is built on first use "
                f"(or run: docker build -t {BASE_TAG} src/fixling/sandbox/docker)",
            )
    except Exception as exc:
        row("Docker", False, f"{type(exc).__name__}: start Docker Desktop ({str(exc)[:80]})")

    row(
        "GitHub token",
        bool(cfg.settings.github_token),
        "set" if cfg.settings.github_token else "GITHUB_TOKEN not set (only needed for GitHub mode)",
    )
    if deep and client is not None:
        row("Sandbox isolation", *_deep_check(cfg))
    console.print(table)
    # a missing GitHub token alone is fine for local runs
    raise typer.Exit(code=1 if [n for n in failed if n != "GitHub token"] else 0)


def _deep_check(cfg: Config) -> tuple[bool, str]:
    from fixling.sandbox.docker_sandbox import DockerSandbox
    from fixling.sandbox.images import BASE_TAG, ensure_base, get_client

    client = get_client()
    ensure_base(client)
    sb = DockerSandbox.create(client, BASE_TAG, "doctor", "doctor")
    try:
        rep = sb.hardening_report()
        net = sb.exec("python -c \"import socket; socket.create_connection(('1.1.1.1', 53), 2)\"", 20)
        ok = rep["cap_drop"] == ["ALL"] and rep["read_only"] and rep["network_mode"] == "none" and net.exit_code != 0
        return ok, "no network, no capabilities, read-only rootfs" if ok else f"unexpected sandbox configuration: {rep}"
    finally:
        sb.close()


def _read_issue_file(path: Path) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8")
    lines = text.strip().splitlines()
    title = lines[0].lstrip("# ").strip() if lines else path.stem
    return title, "\n".join(lines[1:]).strip()


@app.command()
def run(
    repo: str = typer.Option(..., "--repo", help="Local path, or owner/name on GitHub."),
    issue: int | None = typer.Option(None, "--issue", help="Issue number (GitHub mode)."),
    issue_file: Path | None = typer.Option(None, "--issue-file", help="Markdown file: first line is the title."),
    no_pr: bool = typer.Option(True, "--no-pr/--pr", help="Never publish; write patch.diff and report.md locally."),
    interactive: bool = typer.Option(False, "--interactive", help="Pause for a human on vague issues."),
    embeddings: bool = typer.Option(True, "--embeddings/--no-embeddings"),
    strength: bool = typer.Option(False, "--strength/--no-strength", help="Also run independent tests on the fix."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Also print every LLM call."),
) -> None:
    """Run the pipeline on one issue and write ``patch.diff`` and ``report.md`` under runs/<id>/."""
    from fixling.errors import FixlingError
    from fixling.services import build_services, docker_provider

    if (issue is None) == (issue_file is None):
        raise typer.BadParameter("give exactly one of --issue or --issue-file")
    services = build_services()
    cfg = services.cfg
    local = Path(repo).exists()
    repo_key = Path(repo).resolve().name if local else repo

    title, body, author = "", "", "local"
    if issue_file:
        title, body = _read_issue_file(issue_file)
    else:
        from fixling.gh.client import GitHubClient

        gh = GitHubClient(cfg.settings.github_token)
        data = gh.get_issue(repo, int(issue or 0))
        title, body, author = data["title"], data.get("body") or "", data.get("user", {}).get("login", "unknown")

    run_id = services.runs.create_run(repo=repo_key, issue_number=issue, model=cfg.models.main_model)
    assert run_id is not None
    src_dir = cfg.run_dir(run_id) / "src"
    try:
        base_sha = copy_local(Path(repo), src_dir) if local else fetch_repo(repo, src_dir, cfg.settings.github_token)
    except FixlingError as exc:
        console.print(f"[red]could not fetch the repository:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    pipeline = services.pipeline(docker_provider(services), use_embeddings=embeddings, strength=strength)
    console.print(f"[bold]run {run_id}[/bold]  {repo_key}  model={cfg.models.main_model}")
    tail = EventTail(services.events, run_id, verbose)
    tail.start()
    try:
        result = pipeline.start(
            {
                "run_id": run_id,
                "repo": repo_key,
                "issue_number": issue,
                "issue_title": title,
                "issue_body": body,
                "issue_author": author,
                "src_dir": str(src_dir),
                "base_sha": base_sha,
            },
            interactive=interactive,
        )
        while "__interrupt__" in result and interactive:
            tail.drain()
            info = result["__interrupt__"][0].value
            console.print("\n[yellow]The issue needs clarification:[/yellow] " + "; ".join(info.get("reasons", [])))
            for q in info.get("questions", []):
                console.print(f"  - {q}")
            answer = typer.prompt("Proceed anyway? (approve/cancel)", default="cancel")
            result = pipeline.resume(run_id, {"action": "approve" if answer.startswith("a") else "cancel"})
    finally:
        tail.stop()
    outcome = result.get("outcome") or {}
    status, confidence = outcome.get("status", "unknown"), outcome.get("confidence", "-")
    console.print(f"\n[bold]{status}[/bold]  confidence: {confidence}")
    console.print(outcome.get("reason", ""))
    out = cfg.run_dir(run_id)
    for name in ("patch.diff", "report.md"):
        if (out / name).exists():
            console.print(f"  {name}: {out / name}")
    raise typer.Exit(code=0 if outcome.get("status") in ("verified_fix", "unverified_fix", "repro_only") else 1)


@app.command()
def replay(run_id: str, after: int = typer.Option(0, "--after", help="Only events after this seq.")) -> None:
    """Print a run's timeline from the event log."""
    from fixling.services import build_services

    services = build_services()
    row = services.runs.get_run(run_id)
    if row is None:
        console.print(f"[red]unknown run {run_id}[/red]")
        raise typer.Exit(code=2)
    table = Table(
        show_header=True,
        header_style="bold",
        title=f"{row['repo']}#{row['issue_number']}  {row['status']} / {row['outcome']}",
    )
    for col in ("seq", "time", "type", "node", "summary"):
        table.add_column(col, overflow="fold")
    for e in services.events.list(run_id, after):
        table.add_row(str(e.seq), e.ts.strftime("%H:%M:%S"), e.type, e.node or "", summarize_event(e))
    console.print(table)


@app.command()
def serve(
    port: int = typer.Option(8000, "--port"),
    host: str = typer.Option("127.0.0.1", "--host", help="Keep the default: the API has no authentication."),
) -> None:
    """Start the API, dashboard, worker, poller and reaper."""
    from fixling.api.server import serve as run_server

    run_server(host=host, port=port)


@app.command("bench-micro")
def bench_micro_cmd(
    label: str = typer.Option("model", "--label", help="Name for the results table, e.g. qwen3-4b-q8."),
    k: int = typer.Option(1, "--k", help="Repetitions per task."),
    node: str | None = typer.Option(None, "--node", help="Only this node/group: intake, rank_files, edit."),
    write: bool = typer.Option(False, "--write", help="Append the table to docs/MODEL_BAKEOFF.md."),
) -> None:
    """Phase 0 micro-benchmark for choosing a model: 30 tasks with the real prompts (needs LM Studio)."""
    from fixling.errors import LLMUnavailable
    from fixling.evals.micro import bench_micro, make_bench_llm, select_tasks

    cfg = get_config()
    llm, events, _ = make_bench_llm(cfg)
    try:
        llm.health()
    except LLMUnavailable as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
    tasks = select_tasks(node)
    console.print(f"running {len(tasks)} micro-tasks x{k} against '{cfg.models.main_model}' ...")
    report = bench_micro(llm, tasks, label=label, k=k, events=events)
    console.print(report.markdown())
    if write:
        path = cfg.root / "docs" / "MODEL_BAKEOFF.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text(encoding="utf-8") if path.exists() else "# Model bake-off\n\n"
        path.write_text(existing.rstrip() + "\n\n" + report.markdown(), encoding="utf-8", newline="")
        console.print(f"appended to {path}")


@app.command("eval")
def eval_cmd(
    tasks_file: Path = typer.Option(Path("evals/tasks/tasks.jsonl"), "--tasks"),
    k: int = typer.Option(1, "--k", help="Runs per task."),
    slice_name: str = typer.Option("dev", "--slice", help="dev, test or all."),
    config: str = typer.Option("baseline", "--config", help="Name, or evals/ablations.yaml#name."),
    task: list[str] = typer.Option([], "--task", help="Only these task ids."),
    report: bool = typer.Option(True, "--report/--no-report", help="Regenerate docs/EVAL_REPORT.md afterwards."),
) -> None:
    """Run the evaluation tasks (needs LM Studio and Docker) and append results to evals/results/."""
    from fixling.evals.report import write_outputs
    from fixling.evals.run_eval import (
        EvalContext,
        apply_ablation,
        default_results_path,
        load_ablations,
        run_eval,
        split_config_ref,
    )
    from fixling.evals.tasks import load_tasks, select
    from fixling.services import build_services, docker_provider

    base_cfg = get_config()
    root = base_cfg.root
    ab_path, name = split_config_ref(config)
    ablations = load_ablations(ab_path or root / "evals" / "ablations.yaml")
    if name not in ablations:
        console.print(f"[red]unknown configuration {name!r}; available: {', '.join(ablations)}[/red]")
        raise typer.Exit(code=2)
    ablation = ablations[name]
    if ablation.unsupported:
        console.print(f"[yellow]{name} is not available yet: {ablation.unsupported}[/yellow]")
        raise typer.Exit(code=2)

    all_tasks = load_tasks(tasks_file if tasks_file.is_absolute() else root / tasks_file)
    tasks = select(all_tasks, slice_name, task or None)
    if not tasks:
        console.print("[red]no tasks selected[/red]")
        raise typer.Exit(code=2)
    cfg = apply_ablation(base_cfg, ablation)
    services = build_services(cfg)
    services.llm.health()
    ctx = EvalContext(
        cfg=cfg, services=services, provider=docker_provider(services), llm=services.llm,
        project_root=root, config_name=name, ablation=ablation,
    )  # fmt: skip
    out = default_results_path(root, name)
    console.print(f"[bold]{name}[/bold]: {len(tasks)} tasks x {k} -> {out}")

    def show(r: Any) -> None:
        mark = "[green]resolved[/green]" if r.resolved else "[red]not resolved[/red]"
        status = f"{r.outcome or 'error'} / {r.confidence or '-'}"
        console.print(f"  {r.task_id} #{r.k_index}: {status} -> {mark} ({r.wall_ms / 1000:.0f}s)")

    run_eval(tasks, k, ctx, out, on_record=show)
    if report:
        write_outputs(root / "evals" / "results", all_tasks, root / "docs" / "EVAL_REPORT.md")
        console.print("updated docs/EVAL_REPORT.md")


@app.command("eval-report")
def eval_report_cmd(tasks_file: Path = typer.Option(Path("evals/tasks/tasks.jsonl"), "--tasks")) -> None:
    """Regenerate docs/EVAL_REPORT.md (and its charts) from evals/results/*.jsonl."""
    from fixling.evals.report import write_outputs
    from fixling.evals.tasks import load_tasks

    root = get_config().root
    tasks = load_tasks(tasks_file if tasks_file.is_absolute() else root / tasks_file)
    metrics = write_outputs(root / "evals" / "results", tasks, root / "docs" / "EVAL_REPORT.md")
    console.print(f"wrote docs/EVAL_REPORT.md ({len(metrics)} configuration(s))")


if __name__ == "__main__":  # pragma: no cover
    app()
