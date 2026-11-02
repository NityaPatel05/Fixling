import type { FixEvent } from "../api";
import { NODES, nodeStates, repairRounds, type NodeState } from "../pipeline";

const ICON: Record<NodeState, string> = { done: "✓", active: "●", pending: "○", warn: "!", failed: "✗" };

/** The fixed LangGraph pipeline; each node lights up according to the events seen up to `upTo`. */
export function PipelineGraph({ events, upTo, running }: { events: FixEvent[]; upTo: number; running: boolean }) {
  const states = nodeStates(events, upTo, running);
  const rounds = repairRounds(events, upTo);
  return (
    <ol className="pipeline" aria-label="pipeline progress">
      {NODES.map((n) => {
        const state = states[n.id] ?? "pending";
        return (
          <li key={n.id} className={`pnode pnode-${state}`}>
            <span className="picon" aria-hidden="true">
              {ICON[state]}
            </span>
            <span className="plabel">
              {n.label}
              {n.id === "repair" && rounds > 1 ? <em className="rounds"> x{rounds} rounds</em> : null}
            </span>
            <span className="sr-only">{state}</span>
          </li>
        );
      })}
    </ol>
  );
}
