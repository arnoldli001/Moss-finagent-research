import { useCallback, useEffect, useState } from "react";
import {
  api,
  DailySummary,
  RunRecord,
  SchedulerJob,
} from "../api";

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
      setError(`加载调度信息失败：${String(e)}`);
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = window.setInterval(refresh, 15000);
    return () => window.clearInterval(t);
  }, [refresh]);

  const trigger = async (name: string) => {
    setRunningJob(name);
    setError(null);
    try {
      await api.schedulerTrigger(name);
      await refresh();
    } catch (e) {
      setError(`手动触发失败：${String(e)}`);
    } finally {
      setRunningJob(null);
    }
  };

  return (
    <div>
      <div className="sched-head">
        <div className="warn-box" style={{ margin: 0 }}>
          定时作业由独立 Celery Worker + Beat 进程执行；无 Worker 时可在本页手动触发（API进程内执行）。
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
              <th>作业</th><th>Cron</th><th>说明</th><th>状态</th>
              <th>最近执行</th><th>行数</th><th></th>
            </tr>
          </thead>
          <tbody>
            {jobs.map((j) => (
              <tr key={j.name}>
                <td className="agent-id">{j.name}</td>
                <td><code>{j.cron}</code></td>
                <td>{j.description}</td>
                <td>
                  {j.paused
                    ? <span className="badge run-failed">已熔断暂停</span>
                    : <span className="badge run-success">正常</span>}
                </td>
                <td>
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
                    onClick={() => trigger(j.name)}
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
