import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, api, TaskDetail, TraceDetail } from "./api";
import AgentChatView from "./components/AgentChatView";
import AgentTimeline from "./components/AgentTimeline";
import AlertBell from "./components/AlertBell";
import AlertsPanel from "./components/AlertsPanel";
import AlertToasts from "./components/AlertToasts";
import BacktestPanel from "./components/BacktestPanel";
import IntradayTPanel from "./components/IntradayTPanel";
import MetricsPanel from "./components/MetricsPanel";
import ReportView from "./components/ReportView";
import SchedulerPanel from "./components/SchedulerPanel";
import TracePanel from "./components/TracePanel";
import { loadAgentMeta } from "./agentMeta";
import { useAlertsWs } from "./hooks/useAlertsWs";

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
  const [cancelling, setCancelling] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] =
    useState<"research" | "scheduler" | "metrics" | "backtest" | "alerts"
      | "intraday">(
      "research");
  const timer = useRef<number | null>(null);

  // 启动时拉取 agent 中文名/置信度中文映射（失败有本地兜底，不阻断渲染）
  useEffect(() => {
    void loadAgentMeta();
  }, []);

  // 事件告警：单一WS连接供铃铛未读数、Toast与告警面板共用
  const { connected, unread, incoming, refreshUnread } = useAlertsWs();
  const [toasts, setToasts] = useState<Alert[]>([]);
  const [incomingTick, setIncomingTick] = useState(0);
  const [openAlertId, setOpenAlertId] = useState<string | null>(null);

  useEffect(() => {
    if (!incoming) return;
    setIncomingTick((n) => n + 1);
    setToasts((prev) =>
      prev.some((t) => t.alert_id === incoming.alert_id)
        ? prev : [...prev, incoming].slice(-5));
    const id = incoming.alert_id;
    const timerId = window.setTimeout(
      () => setToasts((prev) => prev.filter((t) => t.alert_id !== id)),
      10000);
    return () => window.clearTimeout(timerId);
  }, [incoming]);

  const dismissToast = useCallback((alertId: string) => {
    setToasts((prev) => prev.filter((t) => t.alert_id !== alertId));
  }, []);

  const openAlert = useCallback((alert: Alert) => {
    setOpenAlertId(alert.alert_id);
    setView("alerts");
    setToasts((prev) => prev.filter((t) => t.alert_id !== alert.alert_id));
  }, []);

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
        if (
          detail.status === "completed" ||
          detail.status === "failed" ||
          detail.status === "cancelled"
        ) {
          stopPolling();
          if (detail.status === "completed") {
            try {
              setTrace(await api.trace(taskId));
            } catch { /* trace可选 */ }
          }
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
        conclusion: null, confidence: null, report: null, agent_messages: [],
        progress: "", errors: [], error: null, created_at: "",
      });
      poll(resp.task_id);
    } catch (e) {
      setError(`提交失败：${String(e)}。请确认后端已启动（uvicorn src.api.main:app --port 8100）`);
    } finally {
      setSubmitting(false);
    }
  };

  const cancelTask = async () => {
    if (!task?.task_id) return;
    setCancelling(true);
    try {
      await api.cancelTask(task.task_id);
      stopPolling();
      setTask((prev) => prev ? { ...prev, status: "cancelled" } : prev);
    } catch (e) {
      setError(`停止失败：${String(e)}`);
    } finally {
      setCancelling(false);
    }
  };

  const running = task !== null && (task.status === "queued" || task.status === "running");

  return (
    <div className={view === "intraday" ? "app app-wide" : "app"}>
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
          <button
            className={view === "intraday" ? "tab active" : "tab"}
            onClick={() => setView("intraday")}
          >
            做T辅助
          </button>
          <button
            className={view === "alerts" ? "tab active" : "tab"}
            onClick={() => setView("alerts")}
          >
            事件告警
          </button>
        </nav>
        <AlertBell unread={unread} connected={connected}
          onClick={() => setView("alerts")} />
      </header>

      {view === "scheduler" ? (
        <SchedulerPanel />
      ) : view === "metrics" ? (
        <MetricsPanel />
      ) : view === "backtest" ? (
        <BacktestPanel />
      ) : view === "intraday" ? (
        <IntradayTPanel />
      ) : view === "alerts" ? (
        <AlertsPanel
          incomingTick={incomingTick}
          openAlertId={openAlertId}
          onConsumeOpen={() => setOpenAlertId(null)}
          onReadChanged={refreshUnread}
        />
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
        {running && (
          <button
            className="stop-btn"
            onClick={cancelTask}
            disabled={cancelling}
          >
            {cancelling ? "停止中…" : "停止"}
          </button>
        )}
      </section>

      {error && <div className="error-box">{error}</div>}

      {running && (
        <div className="progress-box">
          <span className="spinner" /> Supervisor已调度，Agent协作执行中（任务 {task?.task_id}）
          {task?.progress && <span className="progress-text">{task.progress}</span>}
        </div>
      )}

      {task?.status === "cancelled" && (
        <div className="info-box">任务已停止：{task.error ?? "用户主动取消"}</div>
      )}

      {task?.status === "failed" && (
        <div className="error-box">
          任务失败：{task.error ?? "未知原因"}
          {task.errors.length > 0 && (
            <ul>{task.errors.map((e, i) => <li key={i}>{e}</li>)}</ul>
          )}
        </div>
      )}

      {/* running时实时展示Agent协作对话（轮询agent_messages） */}
      {running && task?.agent_messages && task.agent_messages.length > 0 && (
        <AgentChatView messages={task.agent_messages} />
      )}

      {trace && (
        <div className="grid">
          <AgentTimeline outputs={trace.agent_outputs} errors={trace.errors} />
          <TracePanel trace={trace} />
        </div>
      )}

      {/* completed时展示完整Agent协作对话 */}
      {!running && task?.agent_messages && task.agent_messages.length > 0 && (
        <AgentChatView messages={task.agent_messages} />
      )}

      {task?.report && <ReportView markdown={task.report} />}
      </>
      )}

      <AlertToasts alerts={toasts} onDismiss={dismissToast} onOpen={openAlert} />
    </div>
  );
}
