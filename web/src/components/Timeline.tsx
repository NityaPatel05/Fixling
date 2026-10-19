import { useMemo, useState } from "react";
import type { FixEvent } from "../api";
import { EVENT_TYPES, clock, summarize } from "../format";

const QUIET = new Set(["llm_call", "test_run"]); // numerous and rarely the interesting part: hidden until asked for
const PAGE = 400;

export function Timeline({
  events,
  selected,
  onSelect,
}: {
  events: FixEvent[];
  selected: number | null;
  onSelect: (seq: number) => void;
}) {
  const [hidden, setHidden] = useState<Set<string>>(new Set(QUIET));
  const [limit, setLimit] = useState(PAGE);

  const present = useMemo(() => {
    const seen = new Set(events.map((e) => e.type));
    return EVENT_TYPES.filter((t) => seen.has(t));
  }, [events]);
  const visible = useMemo(() => events.filter((e) => !hidden.has(e.type)), [events, hidden]);
  const shown = visible.slice(-limit);

  const toggle = (type: string): void => {
    setHidden((prev) => {
      const next = new Set(prev);
      if (next.has(type)) next.delete(type);
      else next.add(type);
      return next;
    });
  };

  return (
    <section aria-label="timeline">
      <div className="chips" role="group" aria-label="filter events by type">
        {present.map((t) => (
          <button key={t} type="button" className={`chip ${hidden.has(t) ? "chip-off" : "chip-on"}`} onClick={() => toggle(t)}>
            {t}
          </button>
        ))}
      </div>
      {visible.length > shown.length ? (
        <button type="button" className="link-button" onClick={() => setLimit((l) => l + PAGE)}>
          show {Math.min(PAGE, visible.length - shown.length)} earlier events
        </button>
      ) : null}
      <ul className="timeline">
        {shown.map((e) => (
          <li key={e.seq}>
            <button
              type="button"
              className={`trow ${selected === e.seq ? "trow-selected" : ""} trow-${e.type}`}
              onClick={() => onSelect(e.seq)}
            >
              <span className="tseq">#{e.seq}</span>
              <span className="ttime">{clock(e.ts)}</span>
              <span className="ttype">{e.type}</span>
              <span className="tsummary">{summarize(e)}</span>
            </button>
          </li>
        ))}
      </ul>
      {shown.length === 0 ? <p className="muted">No events yet.</p> : null}
    </section>
  );
}
