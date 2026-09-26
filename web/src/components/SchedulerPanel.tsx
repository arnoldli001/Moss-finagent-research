import { useCallback, useEffect, useState } from "react";
import {
  api,
  DailySummary,
  RunRecord,
  SchedulerJob,
} from "../api";
import { ApiError } from "../errors";

const STATUS_LABEL: Record<string, string> = {
  success: "成功",
  failed: "失败",
  running: "运行中",
  skipped: "已暂停跳过",
};

function fmtTime(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString("zh-CN", { hour12: false });
}

/**
 * 手动运行的**代价提示**：返回需要确认的文案，`null` = 不确认、直接执行。
 *
 * 为什么 `graph_snapshot` 必须先确认：它不是"刷新一下数据"，而是
 * **逐个标的跑完整研究图**（`src/scheduler/jobs.py::_graph_snapshot` 对
 * `params.targets` 循环 `runtime.graph.ainvoke`）。`snapshot_industry_watchlist`
 * 的 targets 是 [半导体, 煤炭, 创新药, 白酒] —— 点一下就是 **4 次完整研究图**
 * （全 Agent 链 + 全部 LLM 调用，分钟级），而 `POST /jobs/{name}/run` 是
 * **同步执行**的（跑完才返回），所以浏览器会一直转圈等它。
 *
 * 原先前端只放一个按钮、零提示：一次误点就是四次完整分析的真实消耗。
 * 这一层与后端的 `require_admin` 是两件事 —— 后者管"谁能点"，
 * 这里管"点下去之前知不知道自己在点什么"。
 */
function runCostHint(job: SchedulerJob): string | null {
  if (job.kind !== "graph_snapshot") return null;
  const raw = job.params?.targets;
  const targets = Array.isArray(raw) ? raw.map(String) : [];
  const count = targets.length || 1;
  const who = targets.length ? `：${targets.join("、")}` : "";
  return `「${job.name}」会依次跑 ${count} 个标的的完整研究图${who}。\n`
    + `这是 ${count} 次完整分析（全 Agent 链 + LLM 调用），跑完才返回，`
    + `期间本页会一直等待。\n\n确认现在手动运行？`;
}

function StatusBadge({ status }: { status: string }) {
  const cls =
    status === "success" ? "run-success"
    : status === "failed" ? "run-failed"
    : status === "skipped" ? "run-skipped"
    : "run-running";
  return <span className={`badge ${cls}`}>{STATUS_LABEL[status] ?? status}</span>;
}

export default function SchedulerPanel() {
  const [jobs, setJobs] = useState<SchedulerJob[]>([]);
  const [runs, setRuns] = useState<RunRecord[]>([]);
  const [summary, setSummary] = useState<DailySummary | null>(null);
  const [runningJob, setRunningJob] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [j, r, s] = await Promise.all([
        api.schedulerJobs(),
        api.schedulerRuns(30),
        api.schedulerSummary(),
      ]);
      setJobs(j.jobs);
      setRuns([...r.runs].reverse());
      setSummary(s);
      setError(null);
    } catch (e) {
      // 403 是**终态**，重试一万次也不会好 —— 必须与"网络抖动"分开说，
      // 否则用户会对着一个永远不会成功的页面反复刷新。
      setError(e instanceof ApiError && (e.status === 403 || e.status === 401)
        ? "调度管理仅对管理员开放：当前账号没有权限（这不是网络问题，刷新无效）。"
        : `加载调度信息失败：${String(e)}`);
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = window.setInterval(refresh, 15000);
    return () => window.clearInterval(t);
  }, [refresh]);

  const trigger = async (job: SchedulerJob) => {
    const hint = runCostHint(job);
    if (hint && !window.confirm(hint)) return;
    setRunningJob(job.name);
    setError(null);
    try {
      await api.schedulerTrigger(job.name);
      await refresh();
    } catch (e) {
      setError(
        e instanceof ApiError && (e.status === 403 || e.status === 401)
          ? "手动触发被拒绝：调度管理仅对管理员开放。"
          : `手动触发失败：${String(e)}`);
    } finally {
      setRunningJob(null);
    }
  };

  return (
    <div>
      <div className="sched-head">
        <div className="warn-box" style={{ margin: 0 }}>
          定时作业由独立 Celery Worker + Beat 进程执行；无 Worker 时可在本页手动触发（API进程内执行）。
          <b>本页仅管理员可用</b>，服务端已按管理员校验；
          「手动运行」是<b>同步</b>执行 —— 像 <code>snapshot_industry_watchlist</code> 这类
          多标的作业会跑多次完整研究图，点击后需确认，跑完前本页一直等待。
        </div>
        <button className="btn-ghost" onClick={refresh}>刷新</button>
      </div>

      {error && <div className="error-box">{error}</div>}

      {summary && (
        <div className="summary-row">
          <div className="stat-card">
            <span className="stat-label">今日执行</span>
            <span className="stat-value">{summary.total}</span>
          </div>
          <div className="stat-card">
            <span className="stat-label">成功率</span>
            <span className="stat-value">
              {summary.success_rate === null ? "—" : `${(summary.success_rate * 100).toFixed(0)}%`}
            </span>
          </div>
          <div className="stat-card">
            <span className="stat-label">成功/失败</span>
            <span className="stat-value">{summary.success} / {summary.failed}</span>
          </div>
          <div className="stat-card">
            <span className="stat-label">平均耗时</span>
            <span className="stat-value">
              {summary.avg_duration_ms === null ? "—" : `${(summary.avg_duration_ms / 1000).toFixed(1)}s`}
            </span>
          </div>
        </div>
      )}

      <section className="panel">
        <h2>作业注册表</h2>
        <table className="audit-table sched-table">
          <thead>
            <tr>
              <th>作业</th><th>说明</th><th>状态</th>
              <th>最近执行</th><th>行数</th><th></th>
            </tr>
          </thead>
          <tbody>
            {jobs.map((j) => (
              <tr key={j.name}>
                <td className="agent-id">{j.name}</td>
                <td>{j.description}</td>
                <td className="sched-status">
                  {j.paused
                    ? <span className="badge run-failed">已熔断暂停</span>
                    : <span className="badge run-success">正常</span>}
                </td>
                <td className="sched-last">
                  {j.last_run ? (
                    <span title={j.last_run.error_message ?? ""}>
                      <StatusBadge status={j.last_run.status} />
                      {" "}{fmtTime(j.last_run.start_time)}
                    </span>
                  ) : "—"}
                </td>
                <td>{j.last_run?.records_processed ?? "—"}</td>
                <td>
                  <button
                    className="btn-ghost"
                    disabled={runningJob !== null}
                    title={runCostHint(j) ? "该作业会跑多次完整研究图，点击后会先确认"
                                          : `手动运行 ${j.name}`}
                    onClick={() => void trigger(j)}
                  >
                    {runningJob === j.name ? "执行中…" : "手动运行"}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="panel">
        <h2>运行记录（最近30条）</h2>
        <table className="audit-table sched-table">
          <thead>
            <tr><th>开始时间</th><th>作业</th><th>触发</th><th>状态</th><th>耗时</th><th>行数</th><th>错误</th></tr>
          </thead>
          <tbody>
            {runs.map((r) => (
              <tr key={r.run_id}>
                <td>{fmtTime(r.start_time)}</td>
                <td className="agent-id">{r.job_name}</td>
                <td>{r.trigger === "manual" ? "手动" : "定时"}</td>
                <td><StatusBadge status={r.status} /></td>
                <td>{r.duration_ms === null ? "—" : `${(r.duration_ms / 1000).toFixed(2)}s`}</td>
                <td>{r.records_processed}</td>
                <td className="run-error" title={r.error_message ?? ""}>
                  {r.error_message ? r.error_message.slice(0, 60) : ""}
                </td>
              </tr>
            ))}
            {runs.length === 0 && (
              <tr><td colSpan={7} style={{ color: "var(--muted)" }}>暂无运行记录</td></tr>
            )}
          </tbody>
        </table>
      </section>
    </div>
  );
}
