/**
 * 板块概念拥挤度 API 封装。
 *
 * 路径说明：任务书写的 `/api/sector_crowding/...`，而项目路由统一挂在 `/api/v1`
 * 之下，因此实际前缀是 **`/api/v1/sector_crowding`**（后端
 * `src/api/routes/sector_crowding.py` 里 `router.prefix = "/sector_crowding"`，
 * 由 `api_router` 带上 `/api/v1`）。
 */

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

export type CrowdingWatchItem = {
  sector_code: string;
  sector_name: string;
  note: string;
  added_at: string;
  water_level: number | null;
  ma5_crowding: number | null;
  raw_crowding: number | null;
  sector_amount: number | null;
  market_amount: number | null;
  trade_date: string | null;
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
    const detail = await resp.text();
    throw new Error(`请求失败(${resp.status}): ${detail.slice(0, 300)}`);
  }
  return resp.json() as Promise<T>;
}

export const sectorCrowdingApi = {
  /** 模块自检：表/行数/上次刷新状态（首屏判断"有没有数据"）。 */
  info: () => request<{
    module: string; db_path: string; latest_trade_date: string;
    tables: { daily: number; meta: number; watchlist: number };
    window: Record<string, number>;
    refresh: CrowdingRefreshStatus;
  }>(PREFIX),

  /**
   * 一键刷新全部板块拥挤度（立即返回 task_id，后台线程执行）。
   *
   * `conceptsOnly=false` 时刷**全部**板块（含行业/地区）：数据先全量落库，
   * 告警再按"仅概念"过滤 —— 以后想看行业拥挤度不必重跑 6 年。
   */
  refreshAll: (conceptsOnly = false, maxSectors = 0) =>
    request<{ ok: boolean } & CrowdingRefreshStatus>(`${PREFIX}/refresh_all`, {
      method: "POST",
      body: JSON.stringify({ concepts_only: conceptsOnly, max_sectors: maxSectors }),
    }),

  /** 查询刷新进度（taskId 留空 → 最近一次任务）。 */
  refreshStatus: (taskId = "") =>
    request<CrowdingRefreshStatus>(
      `${PREFIX}/refresh_status${taskId ? `?task_id=${encodeURIComponent(taskId)}` : ""}`),

  /** 全板块最新水位（散点总览）。 */
  latest: (conceptsOnly = false) =>
    request<CrowdingLatest>(
      `${PREFIX}/latest?concepts_only=${conceptsOnly ? "true" : "false"}`),

  /** 水位 ≥ 阈值的板块（threshold 留 0 → 用后端配置默认 0.8）。 */
  alerts: (threshold = 0, conceptsOnly = true) =>
    request<CrowdingAlerts>(
      `${PREFIX}/alerts?threshold=${threshold}`
      + `&concepts_only=${conceptsOnly ? "true" : "false"}`),

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

  watchlist: () =>
    request<{ items: CrowdingWatchItem[]; count: number; threshold: number }>(
      `${PREFIX}/watchlist`),

  addWatch: (sectorCode: string, sectorName = "", note = "") =>
    request<{ ok: boolean; created: boolean; items: CrowdingWatchItem[];
              count: number }>(`${PREFIX}/watchlist`, {
      method: "POST",
      body: JSON.stringify({
        sector_code: sectorCode, sector_name: sectorName, note,
      }),
    }),

  removeWatch: (sectorCode: string) =>
    request<{ ok: boolean; removed: boolean; items: CrowdingWatchItem[];
              count: number }>(
      `${PREFIX}/watchlist/${encodeURIComponent(sectorCode)}`, { method: "DELETE" }),
};
