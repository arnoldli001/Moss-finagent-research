/**
 * 板块概念拥挤度 API 封装。
 *
 * 路径说明：任务书写的 `/api/sector_crowding/...`，而项目路由统一挂在 `/api/v1`
 * 之下，因此实际前缀是 **`/api/v1/sector_crowding`**（后端
 * `src/api/routes/sector_crowding.py` 里 `router.prefix = "/sector_crowding"`，
 * 由 `api_router` 带上 `/api/v1`）。
 */

import { apiErrorFromResponse } from "./errors";
import { notifyUnauthorized } from "./unauthorized";

const PREFIX = "/api/v1/sector_crowding";

/** 单板块每日一行（拥挤度原始值 / MA5 / 水位）。 */
export type CrowdingRow = {
  trade_date: string;
  sector_code: string;
  sector_name: string;
  sector_amount: number | null;
  market_amount: number | null;
  raw_crowding: number | null;
  ma5_crowding: number | null;
  /** 平滑拥挤度 / 近6年平滑拥挤度最大值；null = 数据不足（不参与告警） */
  water_level: number | null;
};

/** 全板块最新水位总览的一行。 */
export type CrowdingOverviewRow = {
  sector_code: string;
  sector_name: string;
  trade_date: string;
  sector_amount: number | null;
  market_amount: number | null;
  raw_crowding: number | null;
  ma5_crowding: number | null;
  water_level: number | null;
  is_concept: number;
  bars: number | null;
};

/** 告警列表的一行（比总览多"近6年最高"）。 */
export type CrowdingAlertRow = CrowdingOverviewRow & {
  max_ma5_crowding: number | null;
  /** 服务端判定：水位 ≥ 阈值（`all=1` 时才可能为 false） */
  is_alert?: boolean;
};

/**
 * 看板清单的一行（`/config_list`）。
 *
 * 这是"该看哪些板块"的**唯一真相来源**：散点总览与告警面板都读它。
 * 与 `CrowdingOverviewRow` 的区别是多了配置位（visible/pinned/source）
 * 并把"没有当日行情"的板块也保留下来（此时各指标为 null）。
 */
export type CrowdingListItem = {
  sector_code: string;
  sector_name: string;
  is_concept: number;
  bars: number;
  /** false = 用户已从清单删除（软删，历史数据仍在库里） */
  visible: boolean;
  /** true = 置顶，两个面板都排最前 */
  pinned: boolean;
  /** "manual" = 用户前端新增；"default" = 首次种子化写入 */
  source: string;
  in_watchlist: boolean;
  // 注：自选池面板已移除（功能与看板清单重复），但后端仍带这个字段，
  // 前端不再使用（类型保留以免与接口返回不一致时误导）。
  trade_date: string;
  sector_amount: number | null;
  market_amount: number | null;
  raw_crowding: number | null;
  ma5_crowding: number | null;
  water_level: number | null;
  // ---- 自定义告警阈值（"告警"列）----
  /** "above" = 水位高于阈值告警；"below" = 低于阈值告警；"" = 未设置 */
  alert_mode: string;
  alert_threshold: number | null;
  /** 按当前水位判定是否已触发 */
  alert_on: boolean;
  // ---- 周频异动指标（每周自动算一次并持久化，见后端 metrics.py）----
  // null = 还没算过 / 历史不够长 / 成分股缺失 → 前端显示"—"（**不是 0**）
  /** 近5个交易日拥挤度水位变化（%） */
  chg_5d: number | null;
  /** 近1个月（20交易日）拥挤度水位变化（%） */
  chg_1m: number | null;
  /** 近2个月（40交易日）拥挤度水位变化（%） */
  chg_2m: number | null;
  /** 近1月资金净流入 / 板块1个月前流通市值（%） */
  flow_ratio: number | null;
  /** 近1月主力净流入合计（元） */
  net_inflow: number | null;
  /** 基准日板块流通市值合计（元） */
  circ_mv_base: number | null;
  metric_week: string;
  base_date_5d: string;
  base_date_1m: string;
  base_date_2m: string;
  flow_base_date: string;
  flow_last_date: string;
};

/** 周频异动指标的元信息（`/metrics/summary`）。 */
export type CrowdingMetricsSummary = {
  week: string;
  computed_at: string;
  count: number;
  /** 窗口口径（后端可配，表头提示要与之一致） */
  windows?: {
    short_days: number;
    month_days: number;
    long_days: number;
    /** 第 4 列的分母口径，当前固定"流通市值" */
    mv_basis: string;
  };
  /** 拥挤度数据截至日（前 3 列的末端） */
  as_of_date: string;
  /** 资金流数据截至日（第 4 列的末端，通常比上面晚几天） */
  flow_last_date: string;
  flow_base_date: string;
  coverage: { chg_5d: number; chg_1m: number; chg_2m: number; flow_ratio: number };
  progress: CrowdingMetricsStatus;
};

/** 周频指标计算任务进度。 */
export type CrowdingMetricsStatus = {
  task_id: string;
  status: string;
  total: number;
  processed: number;
  progress: number;
  rows_written: number;
  failed: number;
  current_sector: string;
  started_at: string;
  finished_at: string;
  seconds: number;
  error: string;
  week: string;
  as_of_date: string;
  flow_last_date: string;
  skipped: boolean;
  message: string;
};

export type CrowdingConfigList = {
  items: CrowdingListItem[];
  count: number;
  visible_count: number;
  pinned_count: number;
  seeded?: number;
  trade_date: string;
  threshold: number;
  high_threshold: number;
  /**
   * 水位需要的最少日线根数（后端 `min_bars_for_water_level`，当前 750 ≈ 3 年）。
   *
   * 板块日线不足这个数时 `water_level` **恒为 null** —— 因为
   * 水位 = 当前平滑拥挤度 / **近 6 年最高值**，对只有半年历史的新板块，
   * "自己的历史最高"就是最近这几天，水位会恒为 100%、一上线就误告警。
   * 所以那是**有意的样本量门槛，不是故障**；前端据此显示
   * 「数据不足（563/750）」而不是一个光秃秃的"—"。
   */
  min_bars_for_water_level?: number;
};

export type CrowdingLatest = {
  trade_date: string;
  count: number;
  threshold: number;
  high_threshold: number;
  sectors: CrowdingOverviewRow[];
};

export type CrowdingAlerts = {
  threshold: number;
  high_threshold: number;
  trade_date: string;
  updated_at: string;
  count: number;
  /** 其中触发告警（水位 ≥ 阈值）的数量 */
  alert_count?: number;
  /** 服务端是否以"清单全量"口径返回 */
  all?: boolean;
  alerts: CrowdingAlertRow[];
};

export type CrowdingDetail = {
  sector_code: string;
  sector_name: string;
  is_concept: boolean;
  bars: number;
  first_trade_date: string;
  last_trade_date: string;
  water_level: number | null;
  max_water_level: number | null;
  threshold: number;
  series: CrowdingRow[];
};

/** 一键刷新任务的进度快照。 */
export type CrowdingRefreshStatus = {
  task_id: string;
  /** running | done | failed | idle | unknown */
  status: string;
  total: number;
  processed: number;
  progress: number;
  inserted: number;
  failed: number;
  failed_sectors: string[];
  current_sector: string;
  started_at: string;
  finished_at: string;
  seconds: number;
  error: string;
  last_update_date: string;
  full_backfill: boolean;
  /** 后端直接给好的中文进度文案，前端不必自己拼 */
  message: string;
};

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!resp.ok) {
    // 401 → 广播"会话失效"，由 useAuth 退回登录页；
    // 否则这个页签只会弹一条"读取拥挤度数据失败"，把"登录过期"误报成"功能坏了"。
    if (resp.status === 401) notifyUnauthorized(url);
    throw apiErrorFromResponse(resp.status, await resp.text());
  }
  return resp.json() as Promise<T>;
}

export const sectorCrowdingApi = {
  /** 模块自检：表/行数/上次刷新状态（首屏判断"有没有数据"）。 */
  info: () => request<{
    module: string; db_path: string; latest_trade_date: string;
    /** 自选池面板已移除；`watchlist` 是后端自检仍会统计的行数 */
    tables: { daily: number; meta: number; watchlist: number };
    window: Record<string, number>;
    refresh: CrowdingRefreshStatus;
  }>(PREFIX),

  /**
   * 一键刷新拥挤度（立即返回 task_id，后台线程执行）。
   *
   * `poolOnly=true`（默认）只刷**关注板块池** —— `sector_crowding_list` 里可见的
   * 板块（主线挖掘复用的同一份池子，当前约 554 个）。
   * 2026-09-24 用户报障："一键刷新显示 1850 个板块，可我关注的池子只有几百个" ——
   * 原来这里传的是全量，慢且与"关注"不符。
   * `poolOnly=false` 才是全量（含行业/地区，约 1850 个），想看行业拥挤度时用。
   */
  refreshAll: (conceptsOnly = false, maxSectors = 0, poolOnly = true) =>
    request<{ ok: boolean } & CrowdingRefreshStatus>(`${PREFIX}/refresh_all`, {
      method: "POST",
      body: JSON.stringify({
        concepts_only: conceptsOnly, max_sectors: maxSectors,
        pool_only: poolOnly,
      }),
    }),

  /** 查询刷新进度（taskId 留空 → 最近一次任务）。 */
  refreshStatus: (taskId = "") =>
    request<CrowdingRefreshStatus>(
      `${PREFIX}/refresh_status${taskId ? `?task_id=${encodeURIComponent(taskId)}` : ""}`),

  /** 全板块最新水位（散点总览）。`useList` = 只返回清单里可见的板块。 */
  latest: (conceptsOnly = false, useList = false) =>
    request<CrowdingLatest>(
      `${PREFIX}/latest?concepts_only=${conceptsOnly ? "true" : "false"}`
      + `&use_list=${useList ? "true" : "false"}`),

  /**
   * 水位 ≥ 阈值的板块（threshold 留 0 → 用后端配置默认 0.8）。
   *
   * `all=true` 返回清单**全部板块**（`is_alert` 标记谁触发告警），
   * 供告警面板做"全量显示 + ≥80% 标红"。
   */
  alerts: (threshold = 0, conceptsOnly = true, useList = false, all = false) =>
    request<CrowdingAlerts>(
      `${PREFIX}/alerts?threshold=${threshold}`
      + `&concepts_only=${conceptsOnly ? "true" : "false"}`
      + `&use_list=${useList ? "true" : "false"}`
      + `&all=${all ? "true" : "false"}`),

  // ---------------- 看板清单（持久化配置） ----------------

  /** 读清单（首次调用后端会把默认可见板块种子化落库）。 */
  configList: (conceptsOnly = true) =>
    request<CrowdingConfigList>(
      `${PREFIX}/config_list?concepts_only=${conceptsOnly ? "true" : "false"}`),

  /** 新增/恢复一个板块到清单。 */
  listAdd: (sectorCode: string, sectorName = "", pinned?: boolean,
            conceptsOnly = true) =>
    request<{ ok: boolean } & CrowdingConfigList>(
      `${PREFIX}/config_list?concepts_only=${conceptsOnly ? "true" : "false"}`, {
        method: "POST",
        body: JSON.stringify({
          sector_code: sectorCode, sector_name: sectorName,
          pinned: pinned ?? null,
        }),
      }),

  /** 从清单删除（软删，不删历史数据）。 */
  listRemove: (sectorCode: string, conceptsOnly = true) =>
    request<{ ok: boolean } & CrowdingConfigList>(
      `${PREFIX}/config_list/${encodeURIComponent(sectorCode)}`
      + `?concepts_only=${conceptsOnly ? "true" : "false"}`, { method: "DELETE" }),

  /** 批量删除（散点图多选/框选后一次提交）。 */
  listBatchRemove: (sectorCodes: string[], conceptsOnly = true) =>
    request<{ ok: boolean; removed: number } & CrowdingConfigList>(
      `${PREFIX}/config_list/batch_delete`
      + `?concepts_only=${conceptsOnly ? "true" : "false"}`, {
        method: "POST",
        body: JSON.stringify({ sector_codes: sectorCodes }),
      }),

  /** 置顶/取消置顶。 */
  listPin: (sectorCode: string, pinned = true, conceptsOnly = true) =>
    request<{ ok: boolean } & CrowdingConfigList>(
      `${PREFIX}/config_list/${encodeURIComponent(sectorCode)}/pin`
      + `?pinned=${pinned ? "true" : "false"}`
      + `&concepts_only=${conceptsOnly ? "true" : "false"}`, { method: "POST" }),

  /** 清空清单配置 → 回到默认全量。 */
  listReset: (conceptsOnly = true) =>
    request<{ ok: boolean; cleared: number } & CrowdingConfigList>(
      `${PREFIX}/config_list/reset`
      + `?concepts_only=${conceptsOnly ? "true" : "false"}`, { method: "POST" }),

  // ---------------- 自定义告警阈值（"告警"列） ----------------

  /**
   * 设置某板块的告警阈值。
   *
   * `mode` 二选一：`above`（水位涨到阈值以上告警）/ `below`（跌破阈值告警）；
   * `threshold` 取值 [0, 1]，前端支持 3 位小数（水位本身就是 0~1 的比例量）。
   */
  alertSet: (sectorCode: string, mode: "above" | "below",
             threshold: number, conceptsOnly = true) =>
    request<{ ok: boolean } & CrowdingConfigList>(
      `${PREFIX}/alert/${encodeURIComponent(sectorCode)}`
      + `?concepts_only=${conceptsOnly ? "true" : "false"}`, {
        method: "POST",
        body: JSON.stringify({ sector_code: sectorCode, mode, threshold }),
      }),

  /** 清除某板块的告警阈值。 */
  alertClear: (sectorCode: string, conceptsOnly = true) =>
    request<{ ok: boolean; removed: boolean } & CrowdingConfigList>(
      `${PREFIX}/alert/${encodeURIComponent(sectorCode)}`
      + `?concepts_only=${conceptsOnly ? "true" : "false"}`, { method: "DELETE" }),

  /** 清除全部告警阈值。 */
  alertClearAll: (conceptsOnly = true) =>
    request<{ ok: boolean; cleared: number } & CrowdingConfigList>(
      `${PREFIX}/alert/clear_all`
      + `?concepts_only=${conceptsOnly ? "true" : "false"}`, { method: "POST" }),

  /**
   * 各板块历史最高平滑拥挤度（"近6年最高"列）。
   *
   * 单独接口 + 服务端进程内缓存（全历史聚合，实测首次 1.7s、之后命中缓存
   * 近乎瞬时），所以只在页面加载和刷新完成后取一次，不要放进轮询里。
   */
  sectorsMaxMa5: () =>
    request<{ count: number; cached_at: number;
              max_ma5: Record<string, number> }>(`${PREFIX}/sectors_max_ma5`),

  // ---------------- 周频异动指标（4 列） ----------------

  /** 周频指标元信息（最新周、各基准日、各列覆盖数）。 */
  metricsSummary: () =>
    request<CrowdingMetricsSummary>(`${PREFIX}/metrics/summary`),

  /**
   * 立即重算周频异动指标（后台任务，立即返回 task_id）。
   *
   * 正常由调度器每周三自动跑；`force=false` 时同一周已算过会直接跳过 ——
   * 所以重复点不会白跑几分钟。
   */
  metricsCompute: (force = false) =>
    request<{ ok: boolean } & CrowdingMetricsStatus>(
      `${PREFIX}/metrics/compute?force=${force ? "true" : "false"}`,
      { method: "POST" }),

  /** 周频指标计算进度。 */
  metricsStatus: (taskId = "") =>
    request<CrowdingMetricsStatus>(
      `${PREFIX}/metrics/status${taskId
        ? `?task_id=${encodeURIComponent(taskId)}` : ""}`),

  /** 板块搜索（输入名称查询）。 */
  search: (keyword: string, limit = 20) =>
    request<{ sectors: Record<string, unknown>[] }>(
      `${PREFIX}/sectors?keyword=${encodeURIComponent(keyword)}&limit=${limit}`),

  /** 单板块历史曲线（limit=0 → 全部）。 */
  detail: (sectorCode: string, limit = 0) =>
    request<CrowdingDetail>(
      `${PREFIX}/${encodeURIComponent(sectorCode)}?limit=${limit}`),

  /** 板块成分股（参考数据）。 */
  members: (sectorCode: string) =>
    request<{ sector_code: string;
              members: { stock_code: string; stock_name: string }[] }>(
      `${PREFIX}/members/${encodeURIComponent(sectorCode)}`),
};
