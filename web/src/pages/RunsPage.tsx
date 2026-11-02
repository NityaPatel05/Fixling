import { useState, type FormEvent } from "react";
import { api } from "../api";
import { Badge, confidenceTone, outcomeTone, statusTone } from "../components/Badge";
import { HealthBar } from "../components/HealthBar";
import { STATUS_LABEL, duration, tokens } from "../format";
import { usePoll } from "../hooks";
import { Link, navigate } from "../router";

function StartForm() {
  const [repo, setRepo] = useState("");
  const [issue, setIssue] = useState("");
  const [interactive, setInteractive] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent): Promise<void> => {
    e.preventDefault();
    setError(null);
    const n = Number.parseInt(issue, 10);
    if (!repo.includes("/") || !Number.isInteger(n) || n < 1) {
      setError("Enter a repository as owner/name and an issue number.");
      return;
    }
    setBusy(true);
    try {
      const run = await api.createRun({ repo, issue_number: n, interactive });
      navigate(`/runs/${run.run_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="start" onSubmit={(e) => void submit(e)} aria-label="start a run">
      <input aria-label="repository" placeholder="owner/name" value={repo} onChange={(e) => setRepo(e.target.value)} />
      <input aria-label="issue number" placeholder="issue #" inputMode="numeric" value={issue} onChange={(e) => setIssue(e.target.value)} />
      <label className="inline">
        <input type="checkbox" checked={interactive} onChange={(e) => setInteractive(e.target.checked)} /> pause on vague issues
      </label>
      <button type="submit" disabled={busy}>
        Start run
      </button>
      {error ? <span className="error">{error}</span> : null}
    </form>
  );
}

export function RunsPage() {
  const runs = usePoll(() => api.runs(), 3000);
  const stats = usePoll(() => api.stats(), 10_000);
  const s = stats.data;
  return (
    <div>
      <HealthBar />
      {s ? (
        <section className="stats" aria-label="totals">
          <div>
            <strong>{s.total}</strong>
            <span>runs</span>
          </div>
          <div>
            <strong>{s.by_outcome.verified_fix ?? 0}</strong>
            <span>verified fixes</span>
          </div>
          <div>
            <strong>{s.by_outcome.repro_only ?? 0}</strong>
            <span>repro only</span>
          </div>
          <div>
            <strong>{tokens(s.tokens_in + s.tokens_out)}</strong>
            <span>tokens used</span>
          </div>
          <div>
            <strong>{duration(s.median_duration_s)}</strong>
            <span>median run</span>
          </div>
        </section>
      ) : null}
      <StartForm />
      {runs.error && !runs.data ? <p className="error">{runs.error}</p> : null}
      <table className="grid runs" aria-label="runs">
        <thead>
          <tr>
            <th>Issue</th>
            <th>Status</th>
            <th>Outcome</th>
            <th>Confidence</th>
            <th>Duration</th>
            <th>Tokens</th>
          </tr>
        </thead>
        <tbody>
          {(runs.data ?? []).map((r) => (
            <tr key={r.run_id}>
              <td>
                <Link to={`/runs/${r.run_id}`}>
                  {r.repo}
                  {r.issue_number ? ` #${r.issue_number}` : ""}
                </Link>
              </td>
              <td>
                <Badge tone={statusTone(r.status)}>{r.status}</Badge>
              </td>
              <td>{r.outcome ? <Badge tone={outcomeTone(r.outcome)}>{STATUS_LABEL[r.outcome] ?? r.outcome}</Badge> : "-"}</td>
              <td>{r.confidence && r.confidence !== "none" ? <Badge tone={confidenceTone(r.confidence)}>{r.confidence}</Badge> : "-"}</td>
              <td>{duration(r.duration_s)}</td>
              <td>{tokens(r.tokens_in + r.tokens_out)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {runs.data && runs.data.length === 0 ? (
        <p className="muted">
          No runs yet. Label an issue <code>fixling</code> on a watched repository, or start one above.
        </p>
      ) : null}
    </div>
  );
}
