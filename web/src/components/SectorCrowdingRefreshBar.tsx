import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../errors";
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
 *
 * ## ★ 只读实例：**不给重试按钮**（2026-10-13 用户报障）
 *
 * 拥挤度参考库是共享的、写者只有一处声明（`configs/data_stores.yaml` 的
 * `writer`）。在非写者实例（如对外 pilot）上点刷新，后端现在回
 * **409 + `crowding_read_only` + 人话**（含"谁是写者、去哪刷"）。
 * 这一档**重试一万次也不会成功**（`errors.ts` 里 `item_not_retained` 那条
 * 用的是同一条纪律）—— 所以这里按**机器可读的码**判，不给"重试"，
 * 并把后端那句人话原样显示（写者名字在里面，前端不许自己编）。
 */

const POLL_MS = 1500;

/** 后端拒绝刷新的码（`src/api/routes/sector_crowding.py::CROWDING_READ_ONLY`）。 */
const READ_ONLY_CODE = "crowding_read_only";

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
  /** 终态失败（重试也没用）：只读实例、权限不足这一类。 */
  const [fatal, setFatal] = useState(false);
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

  const start = useCallback(async (poolOnly = true) => {
    setError("");
    setFatal(false);
    setBusy(true);
    try {
      // poolOnly=true（默认）：只刷**关注板块池**（清单里可见的板块，与主线挖掘同源）。
      // 2026-09-24 用户报障："一键刷新显示 1850 个板块，可我关注的池子只有几百个" ——
      // 全量扫（含行业/地区）既慢又与"关注"这个词不符，改成默认只刷池子，
      // 需要行业/地区口径时用旁边的「全量刷新」。
      const started = await sectorCrowdingApi.refreshAll(false, 0, poolOnly);
      taskRef.current = started.task_id;
      setStatus(started);
      stopPolling();
      timerRef.current = window.setInterval(
        () => void poll(taskRef.current), POLL_MS);
    } catch (exc) {
      setBusy(false);
      // ★ 只读实例是**终态**：后端给的是 `code=crowding_read_only` + 人话
      //   （写者是谁、去哪刷）。按**码**判，不按文案判 —— 文案会被"优化得更友好"，
      //   而每一次优化都会让某个隐藏分支静默失效（本项目已有先例）。
      const code = exc instanceof ApiError ? exc.code : "";
      setFatal(code === READ_ONLY_CODE);
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  }, [poll, stopPolling]);

  const running = busy || status?.status === "running";
  const percent = status ? Math.round((status.progress ?? 0) * 100) : 0;

  return (
    <div className="crowding-refreshbar">
      <button className="btn-ghost primary" disabled={running}
              onClick={() => void start(true)}
              title="只刷新**关注板块池**（清单里可见的板块，与主线挖掘同一份池子）；
                     从各板块上次更新日期的下一个交易日起增量拉取到最新交易日，
                     首次使用会自动回填近 6 年（约几分钟）">
        {running ? "刷新中…" : "一键刷新关注板块池"}
      </button>

      {!running && (
        <button className="btn-ghost" onClick={() => void start(false)}
                title="全量刷新（含行业/地区，约 1850 个板块）：数据先全量落库，
                       以后想看行业拥挤度不必重跑 6 年。日常增量用左边那个就够">
          全量刷新（含行业/地区）
        </button>
      )}

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
          {/* ⚠️ 用户口径（2026-09-27）：**前端不显示失败板块数**。
              失败对用户没有可操作性（看不到名单、也不能单独重刷），
              而且多为数据源限流/抖动、下一轮会自愈 —— 显示出来只是噪音。
              失败明细进 `logs/sector_crowding.log`，失败名单落库供
              "同一周内只补失败板块"用。 */}
        </span>
      )}

      {!running && !status && (
        <span className="muted-text">
          尚未刷新过。首次点击会全量回填近 6 年数据（约几分钟），之后都是增量。
        </span>
      )}

      {error && (
        <>
          <span className="error-text">
            {fatal ? error : `触发刷新失败：${error}`}
          </span>
          {/* 只读实例不给"重试"：那个按钮永远不会成功 —— 与 `errors.ts` 里
              `item_not_retained` 同一条纪律（终态错误不许装成可重试）。 */}
          {!fatal && (
            <button className="btn-ghost" onClick={() => void start()}>重试</button>
          )}
        </>
      )}
    </div>
  );
}
