import type { FixEvent } from "./api";

export type NodeState = "done" | "active" | "pending" | "warn" | "failed";

export type PipelineNode = { id: string; label: string; types: string[] };

// Order matters: the first node without a marker event is the one currently working.
export const NODES: PipelineNode[] = [
  { id: "intake", label: "Understand issue", types: ["spec"] },
  { id: "triage", label: "Triage", types: ["triage"] },
  { id: "env", label: "Environment + baseline", types: ["env"] },
  { id: "localize", label: "Localize", types: ["retrieval"] },
  { id: "reproduce", label: "Reproduce", types: ["repro"] },
  { id: "repair", label: "Repair candidates", types: ["candidate"] },
  { id: "select", label: "Select", types: ["selection"] },
  { id: "verify", label: "Clean verify", types: ["verify"] },
  { id: "finalize", label: "Finalize", types: ["outcome"] },
];

/**
 * Pipeline state as of event `upTo`: a pure function of the event log, so replay is just re-rendering with a
 * smaller `upTo`. `running` says whether the run is still in progress at the end of the log.
 */
export function nodeStates(events: FixEvent[], upTo: number, running: boolean): Record<string, NodeState> {
  const seen = events.filter((e) => e.seq <= upTo);
  const out: Record<string, NodeState> = {};
  let lastDone = -1;
  NODES.forEach((n, i) => {
    const hits = seen.filter((e) => n.types.includes(e.type));
    if (hits.length === 0) {
      out[n.id] = "pending";
      return;
    }
    lastDone = i;
    out[n.id] = "done";
    const last = hits[hits.length - 1];
    if (last && ((n.id === "env" && last.payload.ok === false) || (n.id === "reproduce" && last.payload.ok === false))) {
      out[n.id] = "warn";
    }
  });
  const finished = seen.some((e) => e.type === "outcome");
  if (running && !finished) {
    const next = NODES[lastDone + 1];
    if (next) out[next.id] = "active";
  }
  const outcome = seen.filter((e) => e.type === "outcome").pop();
  if (outcome && outcome.payload.status === "no_result") out.finalize = "failed";
  return out;
}

/** Rounds of the repair loop seen so far (the graph shows "x2" when a refine round happened). */
export function repairRounds(events: FixEvent[], upTo: number): number {
  const rounds = new Set<number>();
  for (const e of events) {
    if (e.seq <= upTo && e.type === "candidate" && typeof e.payload.round === "number") rounds.add(e.payload.round);
  }
  return rounds.size;
}
