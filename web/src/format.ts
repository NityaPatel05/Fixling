import type { FixEvent, Payload } from "./api";

// Payloads come from the backend as JSON; these helpers narrow `unknown` without any casts to `any`.
export const str = (v: unknown, fallback = ""): string => (typeof v === "string" ? v : v == null ? fallback : String(v));
export const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
export const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);
export const strs = (v: unknown): string[] => arr(v).map((x) => str(x));
export const bool = (v: unknown): boolean | null => (typeof v === "boolean" ? v : null);

export function field(p: Payload, key: string): unknown {
  return p[key];
}

export function duration(seconds: number | null): string {
  if (seconds == null) return "-";
  if (seconds < 90) return `${Math.round(seconds)}s`;
  return `${(seconds / 60).toFixed(1)} min`;
}

export function clock(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleTimeString();
}

export function tokens(n: number): string {
  return n >= 10_000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

export const STATUS_LABEL: Record<string, string> = {
  verified_fix: "Verified fix",
  unverified_fix: "Unverified fix",
  repro_only: "Repro only",
  needs_clarification: "Needs clarification",
  no_result: "No result",
};

/** One-line description of an event, shown in the timeline. Mirrors `summarize_event` in the CLI. */
export function summarize(e: FixEvent): string {
  const p = e.payload;
  switch (e.type) {
    case "spec":
      return `${str(p.kind)}: ${str(p.summary).slice(0, 110)} (ambiguity ${str(p.ambiguity)}, ${str(p.size_estimate)})`;
    case "triage":
      return `${str(p.decision)}${strs(p.reasons).length ? " - " + strs(p.reasons).join("; ") : ""}`;
    case "env":
      return p.ok === false
        ? `failed: ${str(p.reason).slice(0, 140)}`
        : `baseline ${str(p.passed)} passed, ${strs(p.flaky).length} flaky, image ${str(p.image)}`;
    case "retrieval":
      return `files ${strs(p.chosen_files).join(", ")}; targets ${strs(p.targets).join(", ")}`;
    case "repro":
      return p.ok === true
        ? `valid reproduction ${strs(p.test_ids).join(", ")}`
        : `no valid reproduction after ${str(p.attempts)} attempts`;
    case "test_run":
      return `${str(p.label)}: ${str(p.passed)} passed, ${strs(p.failed).length} failed, ${strs(p.errors).length} errors`;
    case "llm_call":
      return `${str(p.node)} t=${str(p.temperature)} ${str(p.tokens_in)}+${str(p.tokens_out)} tok, ${str(p.latency_ms)}ms`;
    case "candidate":
      return `${str(p.id)} t=${str(p.temperature)} repro_passed=${str(p.repro_passed)} regressions=${strs(p.regressions).length}${
        p.apply_error || p.llm_error ? " (error)" : ""
      }`;
    case "selection":
      return `round ${str(p.round)}: best ${str(p.best)} -> ${str(p.route)}`;
    case "verify":
      return `f2p=${str(p.f2p_ok)} p2p=${str(p.p2p_ok)} shuffled=${str(p.shuffled_ok)} (${str(p.passed)} passed)`;
    case "outcome":
      return `${str(p.status)} / confidence ${str(p.confidence)}: ${str(p.reason).slice(0, 120)}`;
    case "gate_finding":
      return `${str(p.severity)} ${str(p.rule)}: ${str(p.detail).slice(0, 90)}`;
    case "error":
    case "budget":
      return str(p.error ?? p.stopped ?? p.warning).slice(0, 160);
    default:
      return Object.entries(p)
        .filter(([k]) => k !== "text")
        .slice(0, 3)
        .map(([k, v]) => `${k}=${typeof v === "object" ? "{...}" : str(v)}`)
        .join(", ")
        .slice(0, 160);
  }
}

export const EVENT_TYPES = [
  "run_started",
  "issue_received",
  "sanitize",
  "spec",
  "triage",
  "env",
  "retrieval",
  "test_run",
  "repro",
  "llm_call",
  "candidate",
  "gate_finding",
  "selection",
  "judge",
  "refine",
  "verify",
  "strength",
  "outcome",
  "git",
  "pr_opened",
  "comment",
  "interrupt",
  "resume",
  "budget",
  "error",
] as const;
