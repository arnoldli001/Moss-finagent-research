import { useMemo, useState } from "react";
import {
  formatPct,
  formatScore,
  toneOf,
  type MainlineScore,
} from "../mainlineApi";

/**
 * 主线挖掘 · 异动评分热力图。
 *
 * ## 为什么是"色块矩阵"而不是表格
 *
 * 全市场 31 个板块、每天一个分数，用表格看只能一行行读数字；色块矩阵把
 * "谁在变热"变成**一眼可扫的形状**：暖色集中在左上、冷色堆在右下，说明主线明确；
 * 颜色杂散则说明没有主线（这本身就是重要结论）。
 *
 * ## 分档规则（冷 → 热）
 *
 * 分数是 0~100 的绝对尺度，所以**不按当天最大值做相对归一** —— 相对归一会出现
 * "今天最高只有 40 分，40 分也被涂成最红"的假热度。这里按固定色带：
 *
 *     0~40 分  冷（蓝 → 青）     「没戏」
 *     40~55    温（绿）          「观察」
 *     55~70    热（黄 → 橙）     「候选线附近」（候选门槛 ≈ 55）
 *     70~100   烫（橙红 → 深红） 「精选线以上」（精选门槛 ≈ 70）
 *
 * ## 排序与缺失
 *
 * 卡片按 `total` 降序铺排（与后端 `scores` 顺序一致，不重排后端已给的顺序以外的东西）；
 * `total` 为 null 的板块（当天没算出来）单独排在最后并涂灰 —— **灰色不是"0 分"**，
 * 是"没数据"，用同一个色带会让人误以为它是最冷的板块。
 */

/** 色带控制点（分数 → RGB）。固定尺度，不随当天数据抖动。 */
const STOPS: { at: number; rgb: [number, number, number] }[] = [
  { at: 0, rgb: [34, 58, 96] },     // 冷蓝
  { at: 40, rgb: [46, 110, 128] },  // 青
  { at: 55, rgb: [58, 140, 92] },   // 绿
  { at: 70, rgb: [204, 150, 52] },  // 黄橙
  { at: 82, rgb: [214, 98, 58] },   // 橙红
  { at: 100, rgb: [168, 44, 44] },  // 深红
];

/** 分数 → 色带颜色（线性插值）。 */
export function heatColor(score: number | null | undefined): string {
  if (score === null || score === undefined || !Number.isFinite(score)) {
    return "rgba(139,152,165,0.16)";     // 灰 = 无数据，与"0 分"的冷蓝区分开
  }
  const value = Math.max(0, Math.min(100, score));
  for (let index = 0; index < STOPS.length - 1; index += 1) {
    const left = STOPS[index];
    const right = STOPS[index + 1];
    if (value <= right.at) {
      const ratio = (value - left.at) / (right.at - left.at);
      const rgb = left.rgb.map((channel, channelIndex) =>
        Math.round(channel + (right.rgb[channelIndex] - channel) * ratio));
      return `rgb(${rgb[0]},${rgb[1]},${rgb[2]})`;
    }
  }
  const last = STOPS[STOPS.length - 1].rgb;
  return `rgb(${last[0]},${last[1]},${last[2]})`;
}

/** 分数 → 档位中文名（图例 + 卡片 tooltip 用同一套说法）。 */
function tierText(score: number | null | undefined): string {
  if (score === null || score === undefined || !Number.isFinite(score)) {
    return "无评分数据";
  }
  if (score >= 70) return "烫（精选线以上）";
  if (score >= 55) return "热（候选线附近）";
  if (score >= 40) return "温（观察）";
  return "冷（没戏）";
}

type Props = {
  scores: MainlineScore[];
  /** 点击色块 → 打开下钻 */
  onPick: (code: string, name: string) => void;
  loading?: boolean;
};

export default function MainlineHeatmap({ scores, onPick, loading }: Props) {
  /** 高亮模式：默认全部；点图例切换只看候选/精选 */
  const [filter, setFilter] = useState<"all" | "candidate" | "selected">("all");

  const rows = useMemo(() => {
    const list = filter === "selected"
      ? scores.filter((row) => row.selected)
      : filter === "candidate"
        ? scores.filter((row) => row.candidate)
        : [...scores];
    // 有分数的按 total 降序在前，无分数的沉底（并列时按代码定序，结果稳定）
    return list.sort((left, right) => {
      const a = left.total;
      const b = right.total;
      const aMissing = a === null || a === undefined || !Number.isFinite(a);
      const bMissing = b === null || b === undefined || !Number.isFinite(b);
      if (aMissing && bMissing) return left.code.localeCompare(right.code);
      if (aMissing) return 1;
      if (bMissing) return -1;
      if (b !== a) return b - a;
      return left.code.localeCompare(right.code);
    });
  }, [scores, filter]);

  const candidateCount = scores.filter((row) => row.candidate).length;
  const selectedCount = scores.filter((row) => row.selected).length;

  return (
    <div className="mainline-heat">
      <div className="mainline-heat-bar">
        <div className="mode-switch">
          <button className={filter === "all" ? "mode-btn active" : "mode-btn"}
                  onClick={() => setFilter("all")}>
            全部（{scores.length}）
          </button>
          <button className={filter === "candidate" ? "mode-btn active" : "mode-btn"}
                  onClick={() => setFilter("candidate")}
                  title="进入候选池的板块">
            候选（{candidateCount}）
          </button>
          <button className={filter === "selected" ? "mode-btn active" : "mode-btn"}
                  onClick={() => setFilter("selected")}
                  title="进入精选名单的板块">
            精选（{selectedCount}）
          </button>
        </div>
        <span style={{ flex: 1 }} />
        <span className="muted-text">点击色块看三层明细</span>
      </div>

      {/* 图例：色带与上面 STOPS 一一对应，改色带必须同步改这里 */}
      <div className="mainline-heat-legend muted-text">
        <span>冷</span>
        {[15, 40, 55, 70, 82, 95].map((value) => (
          <i key={value} className="mainline-heat-swatch"
             style={{ background: heatColor(value) }} title={`${value} 分`} />
        ))}
        <span>热</span>
        <span className="mainline-heat-legend-note">
          &lt;40 冷 · 40~55 温 · 55~70 热（候选线）· ≥70 烫（精选线）
        </span>
        <i className="mainline-heat-swatch"
           style={{ background: heatColor(null) }} />
        <span>无数据</span>
      </div>

      {loading && rows.length === 0 && (
        <div className="info-box">正在读取评分…</div>
      )}
      {!loading && rows.length === 0 && (
        <div className="mainline-empty">
          {scores.length === 0
            ? "暂无评分数据。点顶部「立即刷新」拉取当日数据；若已刷新仍为空，看下方 gaps 说明。"
            : "当前筛选下没有板块（试试切回「全部」）。"}
        </div>
      )}

      <div className="mainline-heat-grid">
        {rows.map((row) => {
          const missing = row.total === null || row.total === undefined
            || !Number.isFinite(row.total);
          return (
            <button key={row.code}
                    className={"mainline-heat-cell"
                      + (row.selected ? " selected" : row.candidate ? " candidate" : "")
                      + (missing ? " missing" : "")}
                    style={missing ? undefined : { background: heatColor(row.total) }}
                    onClick={() => onPick(row.code, row.name)}
                    title={`${row.name || row.code}（${row.code}）`
                      + `\n总分 ${formatScore(row.total)} · ${tierText(row.total)}`
                      + `\n六维 ${formatScore(row.six_dim_score)}`
                      + ` · 建仓 ${formatScore(row.accumulation_score)}`
                      + ` · 门控 ${formatScore(row.gate_bonus)}`
                      // ETF 加分不占权重、且入选前就兑现，单独标注来源
                      + `${row.etf_bonus
                        ? ` · ETF +${formatScore(row.etf_bonus)}` : ""}`
                      // 覆盖率只做展示：第二层的分是"在可用维度上"归一化的，
                      // 覆盖率低的分与覆盖率高的分不可直接比较
                      + `${row.accumulation_coverage !== null
                        && row.accumulation_coverage !== undefined
                        && row.accumulation_coverage < 0.999
                        ? `\n建仓覆盖率 ${(row.accumulation_coverage * 100).toFixed(0)}%`
                        : ""}`
                      + `\n排名 ${row.rank ?? "—"}`
                      + `${row.level_label ? ` · ${row.level_label}` : ""}`
                      + `\n当日涨跌 ${formatPct(row.change_pct)}`
                      + `${row.gaps.length > 0
                        ? `\n缺数据：${row.gaps.join("；")}` : ""}`
                      + "\n点击查看三层明细"}>
              <span className="mainline-heat-name">{row.name || row.code}</span>
              <span className="mainline-heat-score mono">
                {missing ? "—" : formatScore(row.total, 1)}
              </span>
              <span className={"mainline-heat-delta " + toneOf(row.change_pct)}>
                {formatPct(row.change_pct, 1)}
              </span>
              {row.resonance && <span className="mainline-heat-res">共</span>}
            </button>
          );
        })}
      </div>
    </div>
  );
}
