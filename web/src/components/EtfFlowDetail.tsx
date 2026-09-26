import { useMemo } from "react";
import { formatScore, formatRatioPct, formatYi, toneOf } from "../mainlineApi";
import {
  ETF_GROUP_LEVEL_TEXT,
  formatPercentile,
  formatSharesWan,
  indexByGroup,
  positionByIndex,
  type EtfFlowSnapshot,
} from "../etfFlowApi";

/**
 * ETF 份额监控 · 份额变化明细（每只 ETF 一行）。
 *
 * ## 这张表的读法
 *
 * 核心不是"ETF 涨跌"，而是**份额变化**：份额降 + 价格跌 = 赎回离场；
 * 份额涨 + 价格跌 = 有人逆势申购（宽基上通常是国家队/大型机构）。
 * 所以列的顺序是"份额 → 各窗口变化 → 对应指数分位"，分位决定了这次申购
 * 是发生在低位（有意义）还是高位（可能是反向指标）。
 *
 * ## 两个显示硬规矩
 *
 * 1. **`null` 一律显示"—"**：`change_5d = null` 意味着 5 个交易日前没有基准
 *    （停牌/未同步/新上市），把它画成 `0.00%` 会让用户以为"份额没变"。
 * 2. **指数分位不带正负号**：它是"位置"（0.088 = 近 34 日的 8.8% 分位），
 *    不是涨跌幅，所以走 `formatPercentile()` 而不是 `formatPct()`。
 */

type Props = {
  snapshot: EtfFlowSnapshot | null;
  loading: boolean;
};

export default function EtfFlowDetail({ snapshot, loading }: Props) {
  const indicators = snapshot?.indicators ?? [];
  const positions = snapshot?.positions ?? [];
  const resonance = snapshot?.resonance ?? [];

  const positionOf = useMemo(() => positionByIndex(positions), [positions]);
  const indexOfGroup = useMemo(() => indexByGroup(resonance), [resonance]);

  /** 分位窗口：行情缺失时 positions 为空，表头就不能写死 34 日。 */
  const window = positions[0]?.window ?? null;

  if (!snapshot) {
    return (
      <div className="etf-flow-empty">
        {loading
          ? "正在读取份额明细…"
          : "还没有数据。点右上「立即刷新」按最新份额重算一次快照。"}
      </div>
    );
  }

  return (
    <section className="etf-flow-card">
      <div className="etf-flow-card-head">
        <h4>份额变化明细</h4>
        <span className="muted-text">
          共 {indicators.length} 只 · 按观测清单顺序（configs/etf_flow.yaml）
          · 份额单位万份
        </span>
      </div>

      {indicators.length === 0 ? (
        <div className="etf-flow-empty">
          暂无 ETF 指标数据。可能是份额数据还没同步到最新交易日 ——
          看顶部「数据缺口」提示，或点「立即刷新」重算。
        </div>
      ) : (
        <div className="etf-flow-table-scroll">
          <table className="etf-flow-table">
            <thead>
              <tr>
                <th>名称</th>
                <th>代码</th>
                <th>等级</th>
                <th className="num">份额(万份)</th>
                <th className="num" title="相对前一交易日">1日</th>
                <th className="num" title="相对 5 个交易日前">5日</th>
                <th className="num" title="相对 10 个交易日前">10日</th>
                <th className="num" title="相对 20 个交易日前">20日</th>
                <th className="num"
                    title={`指数收盘价在近 ${window ?? "—"} 个交易日中的分位`
                      + "（越大越接近区间高位）"}>
                  指数分位
                </th>
                <th className="num"
                    title="当日成交额 / 近 60 日成交额中位数（放量倍数）">
                  放量
                </th>
                <th>数据缺口</th>
              </tr>
            </thead>
            <tbody>
              {indicators.map((item) => {
                const index = indexOfGroup[item.group] ?? "";
                const position = index ? positionOf[index] ?? null : null;
                return (
                  <tr key={item.code}>
                    <td>{item.name || item.code}</td>
                    <td className="mono muted-text">{item.code}</td>
                    <td className="muted-text">
                      {ETF_GROUP_LEVEL_TEXT[item.level] || item.level || "—"}
                    </td>
                    <td className="num mono">{formatSharesWan(item.shares)}</td>
                    <td className={"num mono " + toneOf(item.change_1d)}>
                      {formatRatioPct(item.change_1d)}
                    </td>
                    <td className={"num mono " + toneOf(item.change_5d)}>
                      {formatRatioPct(item.change_5d)}
                    </td>
                    <td className={"num mono " + toneOf(item.change_10d)}>
                      {formatRatioPct(item.change_10d)}
                    </td>
                    <td className={"num mono " + toneOf(item.change_20d)}>
                      {formatRatioPct(item.change_20d)}
                    </td>
                    <td className="num mono"
                        title={position
                          ? `${position.name || position.code} 收盘`
                            + ` ${formatScore(position.close, 2)}`
                            + `（近 ${position.window} 日 ${position.mode} 分位；`
                            + `区间 ${formatScore(position.low, 2)}`
                            + ` ~ ${formatScore(position.high, 2)}）`
                          : "该系列没有对应宽基指数（行业主题组），不做分位判断"}>
                      {formatPercentile(position?.percentile ?? null)}
                    </td>
                    <td className="num mono muted-text"
                        title={item.amount === null || item.amount === undefined
                          ? "当日成交额缺失"
                          : `当日成交额 ${formatYi(item.amount)}`
                            + "（放量倍数 = 当日成交额 / 近 60 日成交额中位数）"}>
                      {item.amount_ratio === null
                        || item.amount_ratio === undefined
                        ? "—"
                        : `${item.amount_ratio.toFixed(2)}×`}
                    </td>
                    <td className="etf-flow-gap-cell">
                      {item.gaps.length === 0
                        ? <span className="muted-text">—</span>
                        : <span className="etf-flow-warn-tag"
                                title={item.gaps.join("；")}>
                            ⚠️ {item.gaps.length} 项
                          </span>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <div className="etf-flow-note muted-text">
        「—」= 该窗口<b>没有基准数据</b>（历史不足/停牌/未同步），与"变化为 0"是两件事；
        份额变化率是相对 N 个交易日前的变化。指数分位只对配有宽基指数的系列有效，
        行业主题组显示"—"。
        {positions.length > 0 && (
          <> 当日分位窗口：{positions.map((item) => (
            <span key={item.code} className="etf-flow-inline-chip">
              {item.name || item.code} {formatPercentile(item.percentile)}
            </span>
          ))}</>
        )}
      </div>
    </section>
  );
}
