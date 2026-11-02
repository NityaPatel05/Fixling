import { useEffect, useState } from "react";
import { api, type FixEvent } from "../api";

/** Payload JSON plus the full prompt / output / diff artifact of one event, shown as inert text. */
export function ArtifactViewer({ runId, event, onClose }: { runId: string; event: FixEvent; onClose: () => void }) {
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setText(null);
    setError(null);
    const inline = event.payload.text;
    if (!event.artifact && typeof inline === "string") {
      setText(inline);
      return;
    }
    if (!event.artifact) return;
    let cancelled = false;
    api
      .artifact(runId, event.seq)
      .then((t) => !cancelled && setText(t))
      .catch((e: unknown) => !cancelled && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      cancelled = true;
    };
  }, [runId, event]);

  const { text: _omit, ...rest } = event.payload;
  void _omit;
  return (
    <aside className="artifact" aria-label={`event ${event.seq}`}>
      <header>
        <strong>
          #{event.seq} {event.type}
        </strong>
        <span className="muted">
          {event.source}
          {event.node ? ` / ${event.node}` : ""}
          {event.cause ? ` (caused by #${event.cause})` : ""}
        </span>
        <button type="button" className="link-button" onClick={onClose}>
          close
        </button>
      </header>
      <pre className="json">{JSON.stringify(rest, null, 2)}</pre>
      {error ? <p className="error">{error}</p> : null}
      {text !== null ? (
        <>
          <h4>{event.artifact ? "Full artifact" : "Output"}</h4>
          <pre className="artifact-text">{text}</pre>
        </>
      ) : null}
    </aside>
  );
}
