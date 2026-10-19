import { useState } from "react";
import { api, type FixEvent, type RunRow } from "../api";
import { STATUS_LABEL, duration, strs, tokens } from "../format";
import { Badge, confidenceTone, outcomeTone, safeHref, statusTone } from "./Badge";

export function SummaryCard({ run, outcome, onChange }: { run: RunRow; outcome: FixEvent | undefined; onChange: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const status = run.outcome ?? "";
  const reasons = strs(outcome?.payload.confidence_reasons);
  const pr = safeHref(run.pr_url);
  const cancellable = run.status === "running" || run.status === "queued" || run.status === "interrupted";

  const cancel = async (): Promise<void> => {
    setBusy(true);
    setError(null);
    try {
      await api.cancel(run.run_id);
      onChange();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="summary" aria-label="run summary">
      <h3>
        {run.repo}
        {run.issue_number ? ` #${run.issue_number}` : ""}
      </h3>
      <p>
        <Badge tone={statusTone(run.status)}>{run.status}</Badge>{" "}
        {status ? <Badge tone={outcomeTone(run.outcome)}>{STATUS_LABEL[status] ?? status}</Badge> : null}{" "}
        {run.confidence && run.confidence !== "none" ? (
          <Badge tone={confidenceTone(run.confidence)}>confidence: {run.confidence}</Badge>
        ) : null}
      </p>
      {outcome ? <p>{String(outcome.payload.reason ?? "")}</p> : null}
      {reasons.length > 0 ? (
        <ul className="reasons">
          {reasons.map((r) => (
            <li key={r}>{r}</li>
          ))}
        </ul>
      ) : null}
      <dl className="facts">
        <dt>model</dt>
        <dd>{run.model ?? "-"}</dd>
        <dt>tokens in / out</dt>
        <dd>
          {tokens(run.tokens_in)} / {tokens(run.tokens_out)}
        </dd>
        <dt>LLM time</dt>
        <dd>{duration(run.llm_ms / 1000)}</dd>
        <dt>wall time</dt>
        <dd>{duration(run.duration_s)}</dd>
      </dl>
      <p className="links">
        {pr ? (
          <a href={pr} target="_blank" rel="noreferrer">
            Open the draft PR
          </a>
        ) : null}
        {run.has_patch ? (
          <a href={`/api/runs/${run.run_id}/files/patch.diff`} target="_blank" rel="noreferrer">
            patch.diff
          </a>
        ) : null}
        {run.has_report ? (
          <a href={`/api/runs/${run.run_id}/files/report.md`} target="_blank" rel="noreferrer">
            report.md
          </a>
        ) : null}
      </p>
      {error ? <p className="error">{error}</p> : null}
      {cancellable ? (
        <button type="button" className="danger" disabled={busy} onClick={() => void cancel()}>
          Cancel run
        </button>
      ) : null}
    </section>
  );
}
