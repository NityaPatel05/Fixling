import { useMemo, useState } from "react";
import { api, type FixEvent } from "../api";
import { strs } from "../format";

/** Shown when a run is paused on a vague issue: edit the extracted spec, then resume, or cancel the run. */
export function ClarifyPanel({ runId, events, onDone }: { runId: string; events: FixEvent[]; onDone: () => void }) {
  const waiting = useMemo(() => [...events].reverse().find((e) => e.type === "interrupt"), [events]);
  const initial = useMemo(() => JSON.stringify(waiting?.payload.spec ?? {}, null, 2), [waiting]);
  const [draft, setDraft] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const text = draft ?? initial;

  const send = async (action: "approve" | "edit_spec" | "cancel"): Promise<void> => {
    setError(null);
    let spec: unknown;
    if (action === "edit_spec") {
      try {
        spec = JSON.parse(text);
      } catch {
        setError("The spec is not valid JSON.");
        return;
      }
    }
    setBusy(true);
    try {
      await api.resume(runId, spec === undefined ? { action } : { action, spec });
      onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="clarify" aria-label="needs your input">
      <h3>This run is waiting for you</h3>
      <p>The issue looks too vague or too large to fix automatically:</p>
      <ul>
        {strs(waiting?.payload.reasons).map((r) => (
          <li key={r}>{r}</li>
        ))}
      </ul>
      {strs(waiting?.payload.questions).length > 0 ? (
        <>
          <p>Questions Fixling would ask the author:</p>
          <ul>
            {strs(waiting?.payload.questions).map((q) => (
              <li key={q}>{q}</li>
            ))}
          </ul>
        </>
      ) : null}
      <label htmlFor="spec-json">Extracted spec (edit it to add what is missing)</label>
      <textarea id="spec-json" rows={14} value={text} spellCheck={false} onChange={(e) => setDraft(e.target.value)} />
      {error ? <p className="error">{error}</p> : null}
      <div className="actions">
        <button type="button" disabled={busy} onClick={() => void send(draft === null ? "approve" : "edit_spec")}>
          {draft === null ? "Proceed anyway" : "Resume with edited spec"}
        </button>
        <button type="button" className="danger" disabled={busy} onClick={() => void send("cancel")}>
          Cancel run
        </button>
      </div>
    </section>
  );
}
