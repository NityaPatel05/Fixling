import { api } from "../api";
import { usePoll } from "../hooks";
import { Badge } from "./Badge";
import { DiffView } from "./DiffView";

/** Every repair candidate side by side: temperature, test results, gate findings, score, and the diff. */
export function CandidatesTab({ runId }: { runId: string }) {
  const { data, error } = usePoll(() => api.candidates(runId), 3000, [runId]);
  if (error && !data) return <p className="error">{error}</p>;
  if (!data) return <p className="muted">Loading candidates...</p>;
  if (data.candidates.length === 0) return <p className="muted">No candidates yet.</p>;
  return (
    <div className="cards">
      {data.candidates.map((c) => {
        const blocks = c.findings.filter((f) => f.severity === "block");
        return (
          <article key={`${c.id}-${c.event}`} className={`card ${c.selected ? "card-selected" : ""}`}>
            <header>
              <strong>{c.id}</strong>
              <span className="muted">
                round {c.round} / t={c.temperature}
              </span>
              {c.selected ? <Badge tone="ok">selected</Badge> : null}
            </header>
            <dl className="facts">
              <dt>failing test</dt>
              <dd>
                {c.repro_passed === null ? (
                  <Badge tone="muted">n/a</Badge>
                ) : c.repro_passed ? (
                  <Badge tone="ok">passes</Badge>
                ) : (
                  <Badge tone="bad">still fails</Badge>
                )}
              </dd>
              <dt>regressions</dt>
              <dd>{c.regressions.length === 0 ? <Badge tone="ok">none</Badge> : <Badge tone="bad">{c.regressions.length}</Badge>}</dd>
              <dt>score</dt>
              <dd>{c.score === null ? "-" : c.score.toFixed(1)}</dd>
              <dt>identical patches</dt>
              <dd>{c.agreement === null ? "-" : c.agreement + 1}</dd>
              <dt>diff</dt>
              <dd>
                {c.diff_lines} lines, {c.files.length} file(s)
              </dd>
            </dl>
            {c.apply_error ? <p className="error">edit error: {c.apply_error}</p> : null}
            {c.llm_error ? <p className="error">model error: {c.llm_error}</p> : null}
            {c.findings.length > 0 ? (
              <ul className="findings">
                {c.findings.map((f, i) => (
                  <li key={i}>
                    <Badge tone={f.severity === "block" ? "bad" : "warn"}>{f.severity}</Badge> {f.rule}
                    {f.detail ? <span className="muted"> - {f.detail}</span> : null}
                  </li>
                ))}
              </ul>
            ) : null}
            {blocks.length > 0 ? <p className="muted">Blocking findings make a candidate ineligible.</p> : null}
            {c.diff ? <DiffView diff={c.diff} /> : <p className="muted">No patch was produced.</p>}
          </article>
        );
      })}
    </div>
  );
}
