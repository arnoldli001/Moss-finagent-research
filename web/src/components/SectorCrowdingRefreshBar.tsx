import { useCallback, useEffect, useRef, useState } from "react";
import { sectorCrowdingApi, type CrowdingRefreshStatus } from "../sectorCrowdingApi";

/**
 * 一键刷新栏（板块拥挤度页签顶部）。
 *
 * ## 为什么要"点一下 → 拿 task_id → 轮询进度"
 *
 * 一轮全量回填要几分钟（2517 个板块 × 6 年），同步等待必然撞上反代/浏览器超时
 * （项目里已经踩过 300 秒超时的坑）。所以后端立即返回 `task_id`、后台线程跑，
 * 这个组件负责轮询 `refresh_status` 并把进度显示出来。
 *
 * ## 两个细节
 *
 * 1. **刷新中按钮置灰**：后端本身也有"已有任务在跑就复用"的保护，前端置灰只是
 *    让它更明显 —— 重复点击不会真的并发跑两轮。
 * 2. **完成后回调**：刷新结束必须让图表与告警面板重新取数，否则用户看到的是
 *    刷新前的旧数据，会以为"刷了没用"。
 */

const POLL_MS = 1500;

export default function SectorCrowdingRefreshBar({
  onFinished, initialStatus,
}: {
  /** 刷新完成（或失败）后触发，用于刷新图表与告警面板。 */
  onFinished?: (status: CrowdingRefreshStatus) => void;
  initialStatus?: CrowdingRefreshStatus | null;
}) {
  const [status, setStatus] = useState<CrowdingRefreshStatus | null>(
    initialStatus ?? null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const timerRef = useRef<number | null>(null);
  const taskRef = useRef("");

  const stopPolling = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearInterval(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const poll = useCallback(async (taskId: string) => {
    try {
      const next = await sectorCrowdingApi.refreshStatus(taskId);
      setStatus(next);
      if (next.status !== "running") {
        stopPolling();
        setBusy(false);
        if (next.status === "failed") setError(next.message || "刷新失败");
        onFinished?.(next);
      }
    } catch (exc) {
      stopPolling();
      setBusy(false);
      setError(`查询进度失败：${exc instanceof Error ? exc.message : String(exc)}`);
    }
  }, [stopPolling, onFinished]);

  useEffect(() => () => stopPolling(), [stopPolling]);

  const start = useCallback(async () => {
    setError("");
    setBusy(true);
    try {
      const started = await sectorCrowdingApi.refreshAll(false);
      taskRef.current = started.task_id;
      setStatus(started);
      stopPolling();
      timerRef.current = window.setInterval(
        () => void poll(taskRef.current), POLL_MS);
    } catch (exc) {
      setBusy(false);
      setError(`触发刷新失败：${exc instanceof Error ? exc.message : String(exc)}`);
    }
  }, [poll, stopPolling]);

  const running = busy || status?.status === "running";
  const percent = status ? Math.round((status.progress ?? 0) * 100) : 0;

  return (
    <div className="crowding-refreshbar">
      <button className="btn-ghost primary" disabled={running}
              onClick={() => void start()}
              title="从各板块上次更新日期的下一个交易日起增量拉取到最新交易日；
                     首次使用会自动全量回填近 6 年（约几分钟）">
        {running ? "刷新中…" : "一键刷新全部板块拥挤度"}
      </button>

      {running && (
        <div className="crowding-progress" role="status" aria-live="polite">
          <div className="crowding-progress-track">
            <div className="crowding-progress-fill" style={{ width: `${percent}%` }} />
          </div>
          <span className="mono muted-text">
            {status?.message || "刷新中…"}
            {status?.full_backfill ? "（首次全量回填近 6 年）" : ""}
          </span>
        </div>
      )}

      {!running && status && status.status === "done" && (
        <span className="crowding-done">
          ✅ {status.message}
          {status.seconds > 0 && (
            <span className="muted-text"> · 耗时 {status.seconds.toFixed(0)}s</span>
          )}
          {status.failed > 0 && (
            <span className="warn-text">
              {" "}· {status.failed} 个板块失败（详见 logs/sector_crowding.log）
            </span>
          )}
        </span>
      )}

      {!running && !status && (
        <span className="muted-text">
          尚未刷新过。首次点击会全量回填近 6 年数据（约几分钟），之后都是增量。
        </span>
      )}

      {error && (
        <>
          <span className="error-text">{error}</span>
          <button className="btn-ghost" onClick={() => void start()}>重试</button>
        </>
      )}
    </div>
  );
}
