import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { ArtifactViewer } from "../components/ArtifactViewer";
import { CandidatesTab } from "../components/CandidatesTab";
import { ClarifyPanel } from "../components/ClarifyPanel";
import { PipelineGraph } from "../components/PipelineGraph";
import { SummaryCard } from "../components/SummaryCard";
import { Timeline } from "../components/Timeline";
import { VerifyTab } from "../components/VerifyTab";
import { useRunEvents, usePoll } from "../hooks";
import { Link } from "../router";

type Tab = "timeline" | "candidates" | "verify" | "report";
const TABS: { id: Tab; label: string }[] = [
  { id: "timeline", label: "Timeline" },
  { id: "candidates", label: "Candidates" },
  { id: "verify", label: "Verify" },
  { id: "report", label: "Report" },
];

function ReportTab({ runId }: { runId: string }) {
  const { data, error } = usePoll(() => api.file(runId, "report.md"), 5000, [runId]);
  if (data) return <pre className="report">{data}</pre>;
  return <p className="muted">{error ? "The report is written when the run finishes." : "Loading..."}</p>;
}

export function RunPage({ runId }: { runId: string }) {
  const { events, live, error } = useRunEvents(runId);
  const summary = usePoll(() => api.summary(runId), 3000, [runId]);
  const [tab, setTab] = useState<Tab>("timeline");
  const [selected, setSelected] = useState<number | null>(null);
  const maxSeq = events.length > 0 ? (events[events.length - 1]?.seq ?? 0) : 0;
  const [replay, setReplay] = useState<number | null>(null); // null = follow the live end of the log
  const upTo = replay ?? maxSeq;

  useEffect(() => {
    setSelected(null);
    setReplay(null);
  }, [runId]);

  const visible = useMemo(() => events.filter((e) => e.seq <= upTo), [events, upTo]);
  const chosen = selected === null ? null : (events.find((e) => e.seq === selected) ?? null);
  const run = summary.data?.run;
  const latest = summary.data?.latest ?? {};
  const running = run ? run.status === "running" || run.status === "queued" : live;

  return (
    <div className="run-page">
      <p>
        <Link to="/">&larr; all runs</Link>
      </p>
      {error ? <p className="error">{error}</p> : null}
      {run?.status === "interrupted" ? <ClarifyPanel runId={runId} events={events} onDone={() => summary.reload()} /> : null}

      <div className="run-grid">
        <aside className="left">
          <h3>Pipeline</h3>
          <PipelineGraph events={events} upTo={upTo} running={running} />
        </aside>

        <main className="center">
          <div className="replay">
            <label htmlFor="replay">Replay</label>
            <input
              id="replay"
              type="range"
              min={0}
              max={maxSeq}
              value={upTo}
              onChange={(e) => {
                const n = Number(e.target.value);
                setReplay(n >= maxSeq ? null : n);
              }}
              disabled={maxSeq === 0}
            />
            <span className="muted">
              {replay === null ? (live ? "live" : "end") : `event #${replay} of ${maxSeq}`}
            </span>
            {replay !== null ? (
              <button type="button" className="link-button" onClick={() => setReplay(null)}>
                back to live
              </button>
            ) : null}
          </div>

          <div role="tablist" className="tabs">
            {TABS.map((t) => (
              <button
                key={t.id}
                role="tab"
                aria-selected={tab === t.id}
                type="button"
                className={tab === t.id ? "tab tab-on" : "tab"}
                onClick={() => setTab(t.id)}
              >
                {t.label}
              </button>
            ))}
          </div>

          {tab === "timeline" ? (
            <div className="split">
              <Timeline events={visible} selected={selected} onSelect={setSelected} />
              {chosen ? <ArtifactViewer runId={runId} event={chosen} onClose={() => setSelected(null)} /> : null}
            </div>
          ) : null}
          {tab === "candidates" ? <CandidatesTab runId={runId} /> : null}
          {tab === "verify" ? <VerifyTab latest={latest} /> : null}
          {tab === "report" ? <ReportTab runId={runId} /> : null}
        </main>

        <aside className="right">
          {run ? <SummaryCard run={run} outcome={latest.outcome} onChange={() => summary.reload()} /> : <p className="muted">Loading...</p>}
        </aside>
      </div>
    </div>
  );
}
