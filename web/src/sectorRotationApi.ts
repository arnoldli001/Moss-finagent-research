/**
 * 行业轮动日报 API 封装（后端 `src/api/routes/sector_rotation.py`）。
 *
 * 前缀 `/api/v1/sector_rotation`。报告本体（图表/热力图）由后端
 * `report.html` 接口以独立 HTML 返回，前端 iframe 嵌入 ——
 * 不在 React 侧重复实现一遍 ECharts 渲染（两套渲染必然慢慢漂开）。
 */

import { apiErrorFromResponse } from "./errors";
import { notifyUnauthorized } from "./unauthorized";

const PREFIX = "/api/v1/sector_rotation";

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!resp.ok) {
    if (resp.status === 401) notifyUnauthorized(url);
    throw apiErrorFromResponse(resp.status, await resp.text());
  }
  return resp.json() as Promise<T>;
}

export const sectorRotationApi = {
  /** 已落盘报告的交易日列表（新的在前）。 */
  history: () =>
    request<{ count: number; dates: string[]; latest: string | null }>(
      `${PREFIX}/history`),

  /** 强制重新生成最近交易日报告（同步，约 20~40 秒）。 */
  refresh: () =>
    request<{
      ok: boolean; trade_date: string; generated_at: string;
      elapsed_seconds: number; industries: number; indices: number;
    }>(`${PREFIX}/refresh`, { method: "POST" }),

  /** 报告 HTML 页的地址（iframe src / 新窗口打开用；date 为空=最新）。 */
  htmlUrl: (date?: string) =>
    `${PREFIX}/report.html${date ? `?date=${encodeURIComponent(date)}` : ""}`,
};
