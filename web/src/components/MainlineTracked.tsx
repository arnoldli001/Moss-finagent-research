import { useMemo, useState } from "react";
import {
  formatPct,
  formatScore,
  formatYi,
  toneOf,
  type MainlineTracked,
} from "../mainlineApi";

/**
 * 主线挖掘 · 重点板块跟踪（卡片列表）。
 *
 * ## 为什么是卡片而不是表格
 *
 * 跟踪池通常只有 5~15 个板块（精选名单的子集），每张卡要同时表达三类异质信息：
 * 评分（六维/建仓/门控）、**资金**（近 5/20 日累计净流入）、结构（龙头股/共振比例/
 * 衰减周期）。塞进表格就得 10+ 列、每列 4 个字符，反而看不清；卡片可以按
 * "结论（总分）→ 依据（分项）→ 资金与结构"三层排布，扫一眼就够。
 *
 * ## 两个刻意的取舍
 *
 * 1. **资金一律换算成亿元**（走 `formatYi`）：后端给的是元，`-2400000000` 这种数字
 *    在卡片里根本读不出来；换算口径只有 `formatYi` 一处，避免各面板显示不一致。
 * 2. **共振比例缺失时显示"—"而不是 0%**：`resonance_ratio=null` 表示样本不足，
 *    和"共振比例真的是 0"是两件事 —— 后者才意味着没有共振。
 *
 * 卡片整块可点 → 打开 `MainlineRadar` 下钻（与热力图共用同一个下钻回调）。
 */

type Props = {
  tracked: MainlineTracked[];
  onPick: (code: string, name: string) => void;
  loading?: boolean;
};

export default function MainlineTracked({ tracked, onPick, loading }: Props) {
  const [onlyResonance, setOnlyResonance] = useState(false);
  const [sortKey, setSortKey] = useState<"total" | "flow5" | "flow20">("total");

  const rows = useMemo(() => {
    const list = onlyResonance
      ? tracked.filter((row) => row.resonance)
      : [...tracked];
    return list.sort((left, right) => {
      const pick = (row: MainlineTracked): number | null => {
        switch (sortKey) {
          case "flow5": return row.flow_5d;
          case "flow20": return row.flow_20d;
          default: return row.total;
        }
      };
      const a = pick(left);
      const b = pick(right);
      // 缺值沉底：null 排最后，而不是当 0 参与比较
      const aMissing = a === null || a === undefined || !Number.isFinite(a);
      const bMissing = b === null || b === undefined || !Number.isFinite(b);
      if (aMissing && bMissing) return left.code.localeCompare(right.code);
      if (aMissing) return 1;
      if (bMissing) return -1;
      if (b !== a) return b - a;
      return left.code.localeCompare(right.code);
    });
  }, [tracked, onlyResonance, sortKey]);

  const resonanceCount = tracked.filter((row) => row.resonance).length;

  return (
    <section className="mainline-card">
      <div className="mainline-card-head">
        <h4>重点板块跟踪</h4>
        <span className="muted-text">
          共 {tracked.length} 个 · 共振 {resonanceCount} 个
          · 衰减周期 = 评分衰减到阈值所需交易日（越短越"快进快出"）
        </span>
      </div>

      <div className="mainline-alert-bar">
        <label className="chart-toggle">
          <input type="checkbox" checked={onlyResonance}
                 onChange={(event) => setOnlyResonance(event.target.checked)}
                 title="只看资金与动量同时达标的板块" />
          只看共振
        </label>
        <label className="muted-text">
          排序
          <select className="mono" value={sortKey}
                  onChange={(event) => setSortKey(
                    event.target.value as "total" | "flow5" | "flow20")}>
            <option value="total">按总分↓</option>
            <option value="flow5">按近 5 日净流入↓</option>
            <option value="flow20">按近 20 日净流入↓</option>
          </select>
        </label>
      </div>

      {loading && rows.length === 0 && <div className="info-box">正在读取跟踪数据…</div>}
      {!loading && rows.length === 0 && (
        <div className="mainline-empty">
          {tracked.length === 0
            ? "跟踪池为空。跟踪池来自精选名单（总分 ≥ 精选线且门控通过）—— 先点顶部「立即刷新」。"
            : "当前筛选下没有板块（取消「只看共振」试试）。"}
        </div>
      )}

      <div className="mainline-tracked-grid">
        {rows.map((row) => (
          <button key={row.code} className="mainline-tracked-card"
                  onClick={() => onPick(row.code, row.name)}
                  title={`${row.name || row.code}（${row.code}）`
                    + `\n档位 ${row.profile || "—"}`
                    + `\n点击查看三层明细与历史评分`}>
            <div className="mainline-tracked-top">
              <span className="mainline-tracked-name">{row.name || row.code}</span>
              <span className="mono muted-text">{row.code}</span>
              {row.resonance && <i className="mainline-res-tag">共振</i>}
              <span style={{ flex: 1 }} />
              <span className="mainline-tracked-total mono">
                {formatScore(row.total, 1)}
              </span>
            </div>

            <div className="mainline-tracked-metrics">
              <span>六维 <b className="mono">{formatScore(row.six_dim, 1)}</b></span>
              <span>建仓 <b className="mono">{formatScore(row.accumulation, 1)}</b></span>
              <span>门控 <b className="mono">{formatScore(row.gate_bonus, 1)}</b></span>
              {/* ETF 加分单独显示：它**不占权重**且在入选前就兑现，
                  与门控不是一回事，合并展示会让人以为都是"共振加成"。 */}
              {row.etf_bonus ? (
                <span title={row.etf_level === 2
                  ? "ETF 二级异动（历史级放量 + 份额净申购）"
                  : "ETF 一级异动（历史级放量）"}>
                  ETF <b className="mono">+{formatScore(row.etf_bonus, 1)}</b>
                </span>
              ) : null}
              <span>衰减
                <b className="mono">{row.decay_days === null
                  || row.decay_days === undefined ? "—" : `${row.decay_days}日`}</b>
              </span>
              <span>共振比例
                <b className="mono">{row.resonance_ratio === null
                  || row.resonance_ratio === undefined ? "—"
                  : `${(row.resonance_ratio * 100).toFixed(0)}%`}</b>
              </span>
            </div>

            {/* 主力资金：元 → 亿元，涨红跌绿 */}
            <div className="mainline-tracked-flow">
              <span>
                <i className="muted-text">主力 5 日</i>
                <b className={toneOf(row.flow_5d)}>{formatYi(row.flow_5d)}</b>
              </span>
              <span>
                <i className="muted-text">主力 20 日</i>
                <b className={toneOf(row.flow_20d)}>{formatYi(row.flow_20d)}</b>
              </span>
            </div>

            <div className="mainline-tracked-leaders">
              {row.leaders.length === 0 ? (
                <span className="muted-text">无龙头股数据</span>
              ) : (
                row.leaders.slice(0, 4).map((leader) => (
                  <span key={leader.code} className="mainline-leader-chip"
                        title={`${leader.name || leader.code}（${leader.code}）`
                          + `\n命中 ${leader.hits ?? "—"} 项`
                          + `\n近5日净流入 ${formatYi(leader.net_5d)}`
                          + `\n近10日涨幅 ${formatPct(leader.ret_10d)}`}>
                    {leader.name || leader.code}
                    <i className={toneOf(leader.ret_10d)}>
                      {formatPct(leader.ret_10d, 1)}
                    </i>
                  </span>
                ))
              )}
              {row.leaders.length > 4 && (
                <span className="muted-text">+{row.leaders.length - 4}</span>
              )}
            </div>

            {row.seat_note && (
              <div className="mainline-seat-note">席位：{row.seat_note}</div>
            )}
          </button>
        ))}
      </div>
    </section>
  );
}
