import { useCallback, useMemo, useState } from "react";
import {
  sectorCrowdingApi,
  type CrowdingAlertRow,
  type CrowdingAlerts,
} from "../sectorCrowdingApi";

/**
 * 高拥挤度告警面板（水位 ≥ 80%）。
 *
 * ## 为什么"水位"而不是"拥挤度绝对值"
 *
 * 拥挤度的绝对值没有跨板块可比性（大盘股板块天然成交额高）。水位是**相对该板块
 * 自己近 6 年最拥挤时刻**的比例，因此 87% 在任何一个板块上都读得通：
 * "它现在处于自己历史上第 87 分位的拥挤状态"。
 *
 * ## 颜色口径
 *
 * - ≥ 90%：红色（历史极值区，风险最高）
 * - 80%~90%：橙色（已触发告警）
 *
 * ## 排序与导出
 *
 * 支持按水位/名称排序；导出 CSV 用前端 Blob 直接下载（不必后端再开接口）——
 * 数据已经在手上（≤ 几十行），多一个接口只会多一处失败点。
 */

type SortKey = "water" | "name";

function pct(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${(value * 100).toFixed(1)}%`;
}

function yi(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${(value / 1e8).toFixed(1)}亿`;
}

/** 水位 → 高亮级别（红 ≥90% / 橙 80~90%）。 */
export function waterTone(water: number | null | undefined,
                          highThreshold: number): "high" | "warn" | "none" {
  if (water === null || water === undefined || !Number.isFinite(water)) return "none";
  return water >= highThreshold ? "high" : "warn";
}

function downloadCsv(rows: CrowdingAlertRow[], highThreshold: number): void {
  const header = ["板块代码", "板块名称", "拥挤度水位", "当前平滑拥挤度",
                  "近6年最高拥挤度", "最后更新日期"];
  const lines = [header.join(",")];
  for (const row of rows) {
    lines.push([
      row.sector_code,
      `"${(row.sector_name || "").replace(/"/g, '""')}"`,
      row.water_level === null ? "" : (row.water_level * 100).toFixed(2),
      row.ma5_crowding ?? "",
      row.max_ma5_crowding ?? "",
      row.trade_date ?? "",
    ].join(","));
  }
  const blob = new Blob([`\uFEFF${lines.join("\n")}`],
                       { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `crowding_alerts_${new Date().toISOString().slice(0, 10)}.csv`;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  URL.revokeObjectURL(url);
  void highThreshold;
}

export default function SectorCrowdingAlertPanel({
  data, onPick, onNotice, loading,
}: {
  data: CrowdingAlerts | null;
  /** 点"查看详情" → 通知父级在详情区打开该板块。 */
  onPick: (code: string, name: string) => void;
  onNotice?: (text: string) => void;
  loading?: boolean;
}) {
  const [sortKey, setSortKey] = useState<SortKey>("water");
  const highThreshold = data?.high_threshold ?? 0.9;

  const rows = useMemo(() => {
    const list = [...(data?.alerts ?? [])];
    if (sortKey === "name") {
      list.sort((left, right) => left.sector_name.localeCompare(
        right.sector_name, "zh-Hans-CN"));
    } else {
      list.sort((left, right) => (right.water_level ?? 0) - (left.water_level ?? 0));
    }
    return list;
  }, [data, sortKey]);

  const copyAll = useCallback(async () => {
    if (rows.length === 0) return;
    const text = rows
      .map((row) => `${row.sector_name}\t${pct(row.water_level)}`)
      .join("\n");
    try {
      await navigator.clipboard.writeText(text);
      onNotice?.(`已复制 ${rows.length} 个告警板块`);
    } catch {
      onNotice?.("复制失败：浏览器未授权剪贴板（可手动选中列表复制）");
    }
  }, [rows, onNotice]);

  const addAll = useCallback(async () => {
    if (rows.length === 0) return;
    let ok = 0;
    const failed: string[] = [];
    for (const row of rows) {
      try {
        await sectorCrowdingApi.addWatch(row.sector_code, row.sector_name);
        ok += 1;
      } catch {
        failed.push(row.sector_name);
      }
    }
    onNotice?.(failed.length === 0
      ? `已把 ${ok} 个告警板块加入自选池`
      : `加入自选池：成功 ${ok} 个，失败 ${failed.length} 个（${failed.slice(0, 3).join("、")}）`);
  }, [rows, onNotice]);

  return (
    <section className="crowding-alert-panel">
      <div className="crowding-alert-head">
        <h3>⚠️ 高拥挤度告警（水位 ≥ {(data?.threshold ?? 0.8) * 100}%）</h3>
        <span className="muted-text">
          最后更新：{data?.trade_date || "—"}
          {data?.updated_at ? ` ${data.updated_at.slice(11, 16)}` : ""}
          ，共 <b>{rows.length}</b> 个板块触发告警
        </span>
        <span style={{ flex: 1 }} />
        <label className="muted-text">
          排序
          <select className="mono" value={sortKey}
                  onChange={(event) => setSortKey(event.target.value as SortKey)}>
            <option value="water">按水位↓</option>
            <option value="name">按名称</option>
          </select>
        </label>
        <button className="btn-ghost" disabled={rows.length === 0}
                onClick={() => void copyAll()}
                title="把「板块名 + 水位」复制到剪贴板">复制列表</button>
        <button className="btn-ghost" disabled={rows.length === 0}
                onClick={() => void addAll()}
                title="把全部告警板块加入自选池（幂等）">全部加入自选池</button>
        <button className="btn-ghost" disabled={rows.length === 0}
                onClick={() => downloadCsv(rows, highThreshold)}
                title="导出为 CSV（Excel 可直接打开，带 BOM 不乱码）">导出 CSV</button>
      </div>

      {loading && <div className="info-box">正在读取告警…</div>}

      {!loading && rows.length === 0 && (
        <div className="crowding-alert-empty">
          当前无高拥挤度板块
          <span className="muted-text">
            （水位 = 该板块当前平滑拥挤度 / 近 6 年最高值；未被刷新的板块显示"数据不足"）
          </span>
        </div>
      )}

      {rows.length > 0 && (
        <ul className="crowding-alert-list">
          {rows.map((row) => {
            const tone = waterTone(row.water_level, highThreshold);
            return (
              <li key={row.sector_code} className={`crowding-alert-item tone-${tone}`}>
                <button className="crowding-alert-name"
                        onClick={() => onPick(row.sector_code, row.sector_name)}
                        title="点击查看该板块近 6 年拥挤度与水位曲线">
                  {row.sector_name || row.sector_code}
                </button>
                <span className={`crowding-water tone-${tone}`}>
                  {pct(row.water_level)}
                </span>
                <span className="muted-text mono">
                  平滑 {row.ma5_crowding === null
                    ? "—" : (row.ma5_crowding * 100).toFixed(3)}
                </span>
                <span className="muted-text mono">
                  近6年最高 {row.max_ma5_crowding === null
                    ? "—" : (row.max_ma5_crowding * 100).toFixed(3)}
                </span>
                <span className="muted-text mono">成交额 {yi(row.sector_amount)}</span>
                <span className="muted-text mono">更新 {row.trade_date}</span>
                <span style={{ flex: 1 }} />
                <button className="btn-ghost tiny"
                        onClick={() => onPick(row.sector_code, row.sector_name)}>
                  查看详情
                </button>
                <button className="btn-ghost tiny"
                        onClick={() => {
                          void sectorCrowdingApi
                            .addWatch(row.sector_code, row.sector_name)
                            .then(() => onNotice?.(
                              `已加入自选池：${row.sector_name || row.sector_code}`))
                            .catch((exc) => onNotice?.(
                              `加入自选池失败：${exc instanceof Error
                                ? exc.message : String(exc)}`));
                        }}>
                  加入自选池
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
