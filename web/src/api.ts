export type AgentOutputSummary = {
  agent_id: string;
  conclusion: string;
  confidence: string;
  data_refs: string[];
  result: Record<string, unknown>;
};

export type LlmAuditEntry = {
  ts: string;
  model: string;
  provider: string;
  prompt_hash: string;
  tokens_in: number;
  tokens_out: number;
  latency_ms: number;
  cache_hit: boolean;
  cache_kind: string;
  fallback_used: boolean;
  provider_chain: string[];
  error: string | null;
};

export type TaskDetail = {
  task_id: string;
  trace_id: string;
  status: "queued" | "running" | "completed" | "failed";
  conclusion: string | null;
  confidence: string | null;
  report: string | null;
  errors: string[];
  error: string | null;
  created_at: string;
};

export type TraceDetail = {
  trace_id: string;
  status: string;
  query: string;
  agent_outputs: AgentOutputSummary[];
  errors: string[];
  llm_calls: LlmAuditEntry[];
  audit_chain: { valid: boolean; head: string; records: number };
};

export type Health = {
  status: string;
  agents: Record<string, string>;
  data_sources: Record<string, string>;
  model_gateway: Record<string, string>;
  audit_chain: { valid: boolean; records: number };
};

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!resp.ok) {
    const detail = await resp.text();
    throw new Error(`请求失败(${resp.status}): ${detail.slice(0, 200)}`);
  }
  return resp.json() as Promise<T>;
}

export const api = {
  submit: (body: {
    query: string;
    analysis_type: string;
    target: string;
  }) =>
    request<{ task_id: string; status: string; plan: string[] }>(
      "/api/v1/research/analyze",
      { method: "POST", body: JSON.stringify(body) }
    ),
  task: (taskId: string) =>
    request<TaskDetail>(`/api/v1/research/${taskId}`),
  trace: (taskId: string) => request<TraceDetail>(`/api/v1/trace/${taskId}`),
  report: (taskId: string) =>
    request<{ report: string; disclaimer: string }>(
      "/api/v1/report/generate",
      { method: "POST", body: JSON.stringify({ task_id: taskId }) }
    ),
  health: () => request<Health>("/api/v1/health"),
};
