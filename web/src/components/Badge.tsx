import type { ReactNode } from "react";

export type Tone = "ok" | "warn" | "bad" | "info" | "muted";

export function Badge({ tone, children }: { tone: Tone; children: ReactNode }) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

export function statusTone(status: string): Tone {
  switch (status) {
    case "done":
      return "ok";
    case "running":
      return "info";
    case "interrupted":
      return "warn";
    case "failed":
      return "bad";
    default:
      return "muted";
  }
}

export function outcomeTone(outcome: string | null): Tone {
  switch (outcome) {
    case "verified_fix":
      return "ok";
    case "unverified_fix":
    case "repro_only":
    case "needs_clarification":
      return "warn";
    case "no_result":
      return "bad";
    default:
      return "muted";
  }
}

export function confidenceTone(confidence: string | null): Tone {
  switch (confidence) {
    case "high":
      return "ok";
    case "medium":
      return "warn";
    case "low":
      return "bad";
    default:
      return "muted";
  }
}

/** Only http(s) links are ever rendered as hrefs; a `javascript:` URL from a model or issue must stay inert text. */
export function safeHref(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    const u = new URL(url);
    return u.protocol === "https:" || u.protocol === "http:" ? u.toString() : null;
  } catch {
    return null;
  }
}
