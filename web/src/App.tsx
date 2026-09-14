import { useCallback, useEffect, useRef, useState } from "react";
import { api, TaskDetail, TraceDetail } from "./api";
import AgentTimeline from "./components/AgentTimeline";
import BacktestPanel from "./components/BacktestPanel";
import MetricsPanel from "./components/MetricsPanel";
import ReportView from "./components/ReportView";
import SchedulerPanel from "./components/SchedulerPanel";
import TracePanel from "./components/TracePanel";

const ANALYSIS_TYPES = [
  { value: "macro", label: "宏观" },
  { value: "industry", label: "行业" },
  { value: "stock", label: "个股" },
  { value: "full", label: "综合" },
];

export default function App() {
  const [query, setQuery] = useState("当前宏观环境如何？对A股有什么含义？");
  const [analysisType, setAnalysisType] = useState("macro");
  const [target, setTarget] = useState("");
  const [task, setTask] = useState<TaskDetail | null>(null);
  const [trace, setTrace] = useState<TraceDetail | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] =
    useState<"research" | "scheduler" | "metrics" | "backtest">("research");
  const timer = useRef<number | null>(null);

  const stopPolling = () => {
    if (timer.current !== null) {
      window.clearInterval(timer.current);
      timer.current = null;
    }
  };

  const poll = useCallback((taskId: string) => {
    stopPolling();
    timer.current = window.setInterval(async () => {
      try {
        const detail = await api.task(taskId);
        setTask(detail);
        if (detail.status === "completed" || detail.status === "failed") {
          stopPolling();
          setTrace(await api.trace(taskId));
        }
      } catch (e) {
        stopPolling();
        setError(String(e));
      }
    }, 2000);
  }, []);

  useEffect(() => stopPolling, []);

  const submit = async () => {
    setError(null);
    setSubmitting(true);
    setTask(null);
    setTrace(null);
    try {
      const resp = await api.submit({ query, analysis_type: analysisType, target });
      setTask({
        task_id: resp.task_id, trace_id: resp.task_id, status: "queued",
        conclusion: null, confidence: null, report: null, errors: [],
        error: null, created_at: "",
      });
      poll(resp.task_id);
    } catch (e) {
      setError(`提交失败：${String(e)}。请确认后端已启动（uvicorn src.api.main:app --port 8100）`);
    } finally {
      setSubmitting(false);
    }
  };

  const running = task !== null && (task.status === "queued" || task.status === "running");

  return (
    <div className="app">
      <header className="header">
        <h1>Moss-FinAgent-Research</h1>
        <span className="subtitle">多Agent投研工作台 · 全链路可溯源</span>
        <nav className="tabs">
          <button
            className={view === "research" ? "tab active" : "tab"}
            onClick={() => setView("research")}
          >
            投研分析
          </button>
          <button
            className={view === "scheduler" ? "tab active" : "tab"}
            onClick={() => setView("scheduler")}
          >
            调度管理
          </button>
          <button
            className={view === "metrics" ? "tab active" : "tab"}
            onClick={() => setView("metrics")}
          >
            运行指标
          </button>
          <button
            className={view === "backtest" ? "tab active" : "tab"}
            onClick={() => setView("backtest")}
          >
            策略回测
          </button>
        </nav>
      </header>

      {view === "scheduler" ? (
        <SchedulerPanel />
      ) : view === "metrics" ? (
        <MetricsPanel />
      ) : view === "backtest" ? (
        <BacktestPanel />
      ) : (
      <>
      <section className="submit-bar">
        <input
          className="query-input"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="输入投研问题…"
          disabled={running}
        />
        <select value={analysisType} onChange={(e) => setAnalysisType(e.target.value)} disabled={running}>
          {ANALYSIS_TYPES.map((t) => (
            <option key={t.value} value={t.value}>{t.label}</option>
          ))}
        </select>
        <input
          className="target-input"
          value={target}
          onChange={(e) => setTarget(e.target.value)}
          placeholder="标的（如 600519）"
          disabled={running}
        />
        <button onClick={submit} disabled={running || submitting || !query.trim()}>
          {running ? "分析中…" : submitting ? "提交中…" : "开始分析"}
        </button>
      </section>

      {error && <div className="error-box">{error}</div>}

      {running && (
        <div className="progress-box">
          <span className="spinner" /> Supervisor已调度，Agent协作执行中（任务 {task?.task_id}）
        </div>
      )}

      {task?.status === "failed" && (
        <div className="error-box">
          任务失败：{task.error ?? "未知原因"}
          {task.errors.length > 0 && (
            <ul>{task.errors.map((e, i) => <li key={i}>{e}</li>)}</ul>
          )}
        </div>
      )}

      {trace && (
        <div className="grid">
          <AgentTimeline outputs={trace.agent_outputs} errors={trace.errors} />
          <TracePanel trace={trace} />
        </div>
      )}

      {task?.report && <ReportView markdown={task.report} />}
      </>
      )}
    </div>
  );
}
