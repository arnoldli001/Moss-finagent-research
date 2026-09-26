import { useCallback, useEffect, useRef, useState } from "react";
import {
  sectorCrowdingApi,
  type CrowdingMetricsStatus,
  type CrowdingMetricsSummary,
} from "../sectorCrowdingApi";

/**
 * 周频异动指标的元信息 + 手动重算（前端 4 列的数据来源状态）。
 *
 * ## 为什么不放在 60 秒轮询里
 *
 * 这 4 列由后端**每周算一次并落库**，一天之内不会变；轮询它除了白打请求没有
 * 任何意义。所以只在页面打开时读一次 `summary`，以及"重算完成后"读一次。
 *
 * ## 重算是长任务
 *
 * 一轮要按板块抓成分股再回本地仓库聚合（900+ 板块约 3~5 分钟），所以走的是
 * "后台任务 + 轮询进度"（与「一键刷新」同一套），不是同步等待。轮询只在
 * 用户点了重算之后才启动，跑完自动停 —— 不会一直挂着打接口。
 */

const POLL_MS = 2000;

export type CrowdingMetricsController = {
  summary: CrowdingMetricsSummary | null;
  status: CrowdingMetricsStatus | null;
  /** 正在算（含后台任务在跑、界面在轮询） */
  running: boolean;
  /** 发起重算后到第一次拿到 running 之间的空档，用于禁用按钮 */
  starting: boolean;
  error: string;
  reloadSummary: () => Promise<void>;
  compute: (force?: boolean) => Promise<void>;
};

export function useCrowdingMetrics(
  onNotice?: (text: string) => void,
  onFinished?: () => void,
): CrowdingMetricsController {
  const [summary, setSummary] = useState<CrowdingMetricsSummary | null>(null);
  const [status, setStatus] = useState<CrowdingMetricsStatus | null>(null);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState("");
  const notify = useRef(onNotice);
  const finished = useRef(onFinished);
  notify.current = onNotice;
  finished.current = onFinished;

  const reloadSummary = useCallback(async () => {
    setError("");
    try {
      const next = await sectorCrowdingApi.metricsSummary();
      setSummary(next);
      setStatus(next.progress ?? null);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  }, []);

  useEffect(() => { void reloadSummary(); }, [reloadSummary]);

  const compute = useCallback(async (force = true) => {
    setStarting(true);
    setError("");
    try {
      const started = await sectorCrowdingApi.metricsCompute(force);
      setStatus(started);
      if (started.skipped) {
        notify.current?.(started.message || "本周已算过，跳过");
        await reloadSummary();
        finished.current?.();
      }
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
      notify.current?.(`重算失败：${exc instanceof Error ? exc.message : exc}`);
    } finally {
      setStarting(false);
    }
  }, [reloadSummary]);

  const running = status?.status === "running" || starting;

  // 只在自己发起过任务（status 有 task_id）且还在跑时轮询
  useEffect(() => {
    if (!running || !status?.task_id) return;
    let alive = true;
    const timer = window.setInterval(() => {
      sectorCrowdingApi.metricsStatus(status.task_id)
        .then((next) => {
          if (!alive) return;
          setStatus(next);
          if (next.status !== "running") {
            notify.current?.(next.message || "异动指标计算完成");
            void reloadSummary();
            finished.current?.();
          }
        })
        .catch(() => { /* 单次轮询失败不打断，下一轮再试 */ });
    }, POLL_MS);
    return () => { alive = false; window.clearInterval(timer); };
  }, [running, status?.task_id, reloadSummary]);

  return { summary, status, running, starting, error, reloadSummary, compute };
}
