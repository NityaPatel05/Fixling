import { api } from "../api";
import { usePoll } from "../hooks";

const PREFERRED = [
  "config",
  "n_tasks",
  "k",
  "resolve_at_1",
  "ci_low",
  "ci_high",
  "pass_at_k",
  "false_pass_rate",
  "repro_only_rate",
  "median_wall_s",
  "tokens_per_resolved",
];

function cell(v: unknown): string {
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toFixed(3);
  if (v === null || v === undefined) return "-";
  return typeof v === "object" ? JSON.stringify(v) : String(v);
}

/** Evaluation results written by `fixling eval`: one row per configuration (baseline and ablations). */
export function EvalsPage() {
  const { data, error } = usePoll(() => api.evals(), 30_000);
  if (error && !data) return <p className="error">{error}</p>;
  if (!data) return <p className="muted">Loading...</p>;
  if (data.length === 0) {
    return (
      <p className="muted">
        No evaluation results yet. Run <code>fixling eval --slice dev</code> (needs LM Studio) and they will appear here.
      </p>
    );
  }
  const keys = [...PREFERRED.filter((k) => data.some((d) => k in d)), ...Array.from(new Set(data.flatMap((d) => Object.keys(d)))).filter((k) => !PREFERRED.includes(k))].slice(0, 12);
  return (
    <table className="grid" aria-label="evaluation results">
      <thead>
        <tr>
          {keys.map((k) => (
            <th key={k}>{k}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {data.map((d, i) => (
          <tr key={i}>
            {keys.map((k) => (
              <td key={k}>{cell(d[k])}</td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
