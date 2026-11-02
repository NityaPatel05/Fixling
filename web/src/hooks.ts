import { useCallback, useEffect, useRef, useState } from "react";
import { api, type FixEvent } from "./api";

/** Re-run `load` every `ms` milliseconds (and once immediately); keeps the last good value on errors. */
export function usePoll<T>(load: () => Promise<T>, ms: number, deps: unknown[] = []): { data: T | null; error: string | null; reload: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const loadRef = useRef(load);
  loadRef.current = load;

  const run = useCallback(async () => {
    try {
      setData(await loadRef.current());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void run();
    const id = window.setInterval(() => void run(), ms);
    return () => window.clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ms, run, ...deps]);

  return { data, error, reload: () => void run() };
}

/**
 * The run's full event list: one fetch, then a live SSE stream that resumes from the last seen `seq` after any
 * disconnect. Events are keyed by `seq`, so replays and reconnects can never duplicate or reorder them.
 */
export function useRunEvents(runId: string): { events: FixEvent[]; live: boolean; error: string | null } {
  const [events, setEvents] = useState<FixEvent[]>([]);
  const [live, setLive] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let source: EventSource | null = null;
    let retry: number | undefined;
    let last = 0;
    const seen = new Map<number, FixEvent>();
    setEvents([]);

    const accept = (batch: FixEvent[]): void => {
      let changed = false;
      for (const e of batch) {
        if (!seen.has(e.seq)) {
          seen.set(e.seq, e);
          changed = true;
        }
        if (e.seq > last) last = e.seq;
      }
      if (changed) setEvents([...seen.values()].sort((a, b) => a.seq - b.seq));
    };

    const open = (): void => {
      if (cancelled) return;
      source = new EventSource(`/api/runs/${runId}/stream?after=${last}`);
      source.onopen = () => setLive(true);
      source.addEventListener("event", (m: MessageEvent) => {
        try {
          accept([JSON.parse(String(m.data)) as FixEvent]);
        } catch {
          /* a malformed frame is ignored; the next reconnect refetches from `last` */
        }
      });
      source.addEventListener("end", () => {
        source?.close();
        setLive(false);
      });
      source.onerror = () => {
        source?.close();
        setLive(false);
        if (!cancelled) retry = window.setTimeout(open, 2000);
      };
    };

    api
      .events(runId, 0)
      .then((first) => {
        if (cancelled) return;
        accept(first);
        open();
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));

    return () => {
      cancelled = true;
      source?.close();
      if (retry !== undefined) window.clearTimeout(retry);
    };
  }, [runId]);

  return { events, live, error };
}
