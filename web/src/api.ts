// Typed client for the Fixling API. The dashboard holds no business logic: it renders what these endpoints return.

export type RunRow = {
  run_id: string;
  repo: string;
  issue_number: number | null;
  issue_url: string | null;
  status: string;
  outcome: string | null;
  confidence: string | null;
  pr_url: string | null;
  model: string | null;
  tokens_in: number;
  tokens_out: number;
  llm_ms: number;
  started_at: string | null;
  finished_at: string | null;
  base_sha: string | null;
  duration_s: number | null;
  has_patch: boolean;
  has_report: boolean;
};

export type Payload = Record<string, unknown>;

export type FixEvent = {
  run_id: string;
  seq: number;
  ts: string;
  type: string;
  source: string;
  node: string | null;
  cause: number | null;
  payload: Payload;
  artifact: string | null;
};

export type HealthItem = { ok: boolean; detail: string };
export type Health = Record<string, HealthItem>;

export type Stats = {
  total: number;
  by_status: Record<string, number>;
  by_outcome: Record<string, number>;
  by_confidence: Record<string, number>;
  tokens_in: number;
  tokens_out: number;
  median_duration_s: number | null;
};

export type Finding = { rule: string; severity: string; path?: string; detail?: string };

export type CandidateView = {
  id: string;
  round: number;
  temperature: number;
  event: number;
  diff: string | null;
  findings: Finding[];
  score: number | null;
  agreement: number | null;
  selected: boolean;
  repro_passed: boolean | null;
  regressions: string[];
  apply_error: string | null;
  llm_error: string | null;
  diff_lines: number;
  files: string[];
};

export type CandidatesResponse = { best: string | null; candidates: CandidateView[] };
export type Summary = { run: RunRow; latest: Record<string, FixEvent> };
export type EvalSummary = Record<string, unknown>;

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText}: ${(await res.text()).slice(0, 300)}`);
  }
  return (await res.json()) as T;
}

async function text(url: string): Promise<string> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.text();
}

function post<T>(url: string, body?: unknown): Promise<T> {
  return request<T>(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

export const api = {
  health: () => request<Health>("/api/health"),
  stats: () => request<Stats>("/api/stats"),
  runs: () => request<RunRow[]>("/api/runs"),
  run: (id: string) => request<RunRow>(`/api/runs/${id}`),
  summary: (id: string) => request<Summary>(`/api/runs/${id}/summary`),
  events: (id: string, after = 0) => request<FixEvent[]>(`/api/runs/${id}/events?after=${after}`),
  candidates: (id: string) => request<CandidatesResponse>(`/api/runs/${id}/candidates`),
  artifact: (id: string, seq: number) => text(`/api/runs/${id}/artifacts/${seq}`),
  file: (id: string, name: "patch.diff" | "report.md") => text(`/api/runs/${id}/files/${name}`),
  evals: () => request<EvalSummary[]>("/api/evals"),
  createRun: (body: { repo: string; issue_number: number; interactive: boolean }) => post<RunRow>("/api/runs", body),
  resume: (id: string, body: { action: "approve" | "edit_spec" | "cancel"; spec?: unknown }) =>
    post<{ status: string }>(`/api/runs/${id}/resume`, body),
  cancel: (id: string) => post<{ status: string }>(`/api/runs/${id}/cancel`),
};
