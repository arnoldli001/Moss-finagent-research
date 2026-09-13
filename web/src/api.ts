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
  response_hash: string;
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
  data_sources: {
    connectors: {
      name: string;
      status: string;
      simulated: boolean;
      indicators: string[];
    }[];
    storage: { backend: string; status: string; indicators?: number; points?: number };
    redis_cache: string;
  };
  model_gateway: Record<string, string>;
  audit_chain: { valid: boolean; records: number };
};

export type SchedulerJob = {
  name: string;
  cron: string;
  kind: string;
  description: string;
  params: Record<string, unknown>;
  paused: boolean;
  last_run: {
    status: string;
    start_time: string;
    duration_ms: number;
    records_processed: number;
    error_message: string | null;
  } | null;
};

export type RunRecord = {
  run_id: string;
  job_name: string;
  trigger: string;
  start_time: string;
  end_time: string | null;
  duration_ms: number | null;
  status: "running" | "success" | "failed" | "skipped";
  records_processed: number;
  error_message: string | null;
  retries: number;
};

export type DailySummary = {
  date: string;
  total: number;
  success: number;
  failed: number;
  success_rate: number | null;
  avg_duration_ms: number | null;
  failure_reasons: Record<string, number>;
};

export type LlmMetrics = {
  window_calls: number;
  errors: number;
  error_rate: number | null;
  cache_hit_rate: number | null;
  fallback_rate: number | null;
  latency_ms: { p50: number | null; p95: number | null; p99: number | null;
                 avg: number | null; max: number | null };
  slow_calls_over_3s: number;
  tokens: { in: number; out: number; total: number };
  by_provider: {
    provider: string; calls: number; error_rate: number | null;
    cache_hit_rate: number | null; p95_latency_ms: number | null;
  }[];
  by_agent: { agent_id: string; calls: number }[];
};

export type BacktestResponse = {
  asset: string;
  indicator: string;
  simulated: boolean;
  range: { start: string; end: string };
  cache: { indicator_hit: boolean; price_hit: boolean; ttl_seconds: number };
  periods: number;
  signals: { long: number; neutral: number; avoid: number };
  directional: Record<string, {
    long: { n: number; hit_rate: number | null; avg_forward_return: number | null };
    avoid: { n: number; hit_rate: number | null; avg_forward_return: number | null };
    always_long_baseline: number | null;
    baseline_n: number;
  }>;
  strategy: {
    cumulative_return: number;
    cagr: number | null;
    annualized_volatility: number;
    max_drawdown: number;
    sharpe_rf0: number | null;
    invested_months: number;
    total_months: number;
    buy_and_hold: {
      cumulative_return: number; cagr: number | null;
      annualized_volatility: number; max_drawdown: number;
      sharpe_rf0: number | null;
    };
    excess_cumulative_return: number;
  };
  equity_curve: { period: string; strategy: number;
                   buy_and_hold: number; signal: number }[];
  disclaimer: string;
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
  schedulerJobs: () =>
    request<{ jobs: SchedulerJob[] }>("/api/v1/scheduler/jobs"),
  schedulerTrigger: (name: string) =>
    request<{ run: RunRecord }>(
      `/api/v1/scheduler/jobs/${encodeURIComponent(name)}/run`,
      { method: "POST" }
    ),
  schedulerRuns: (limit = 50) =>
    request<{ runs: RunRecord[] }>(
      `/api/v1/scheduler/runs?limit=${limit}`
    ),
  schedulerSummary: () =>
    request<DailySummary>("/api/v1/scheduler/runs/summary"),
  llmMetrics: (limit = 1000) =>
    request<{ limit: number; slow_call_threshold_ms: number;
              metrics: LlmMetrics }>(`/api/v1/metrics?limit=${limit}`),
  backtestRun: (body: { indicator: string; code: string; eps_pct: number }) =>
    request<BacktestResponse>("/api/v1/backtest/run", {
      method: "POST", body: JSON.stringify(body),
    }),
};
