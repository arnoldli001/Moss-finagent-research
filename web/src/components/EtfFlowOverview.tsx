import { useMemo } from "react";
import { formatRatioPct, formatScore, toneOf } from "../mainlineApi";
import {
  directionTone,
  ETF_GROUP_LEVEL_TEXT,
  formatPercentile,
  formatSharesWan,
  indexByGroup,
  positionByIndex,
  type EtfFlowIndicator,
  type EtfFlowSnapshot,
} from "../etfFlowApi";

/**
 * ETF 份额监控 · 总览（环境横幅 → 核心宽基指标卡 → 多产品共振指示灯）。
 *
 * ## 环境横幅现在只留"事实"，门控提示不再说第二遍（用户口径）
 *
 * 回测结论是**机会信号只在熊市有效**（牛市/震荡市方向相反），所以后端在非放行
 * 环境会把机会信号 `gated=true` 降级为观察项。这件事原来在**两个地方**各说一遍：
 *
 *   ① 页签上方那条 `.etf-flow-gate-strip`（跨四个子视图可见，见 EtfFlowPanel）；
 *   ② 本文件环境横幅下的 `.etf-flow-regime-warn` 说明块
 *      （"当前环境机会信号不放行，仅进观察列表" + 原因 + `source_notes` + gaps）。
 *
 * 用户口径（本轮）：「etf-flow-regime-warn 这个内容 …… 提示的这些都不要显示，
 * 上方 etf-flow-gate-strip 也提示了，出现了重复」→ **② 已删除**，
 * 门控提示由 ① 独家承担；横幅只留"参考指数 / 收益 / 收盘 / 两个放行标签"这些事实。
 *
 * ⚠️ 删掉它**没有丢信息**，三条都在别处还看得见（逐条核对过，别凭印象加回来）：
 *   · `regime.gaps` —— 后端 `build_snapshot()` 里已经
 *     `out.gaps.extend(out.regime.gaps)`（src/mainline/etf_flow.py），
 *     由面板的「⚠️ 数据缺口」条显示；脚注「环境判定」那一行也再列一次；
 *   · `source_notes` —— 由面板脚注的「来源：…」原文列出，后端那句
 *     "机会信号仅在熊市有效，详见 docs/ETF_FLOW_BACKTEST.md" 就在那里；
 *   · "原因"那段道理 —— 结论已由门控提示条一句话说完，展开的推导在
 *     docs/ETF_FLOW_BACKTEST.md（脚注「来源」里也有指向）。
 *
 * ⚠️ 熊市里那条 `.etf-flow-regime-warn`（**风险**信号不放行）**保留**：
 *    它讲的是另一条规则（风险信号默认排除熊市），而门控提示条只覆盖机会信号，
 *    两者不重复 —— 别顺手一起删了。若将来要让风险门控也跨子视图可见，
 *    正确做法是让状态条把两种门控都讲清楚，而不是把这块说明加回来。
 *
 * ## "对应指数分位"是怎么对上的
 *
 * ETF 指标本身不带指数，只有 `resonance[].index` 里有（观测清单里配的）。
 * 所以这里用 `indexByGroup()` 建一次映射，再按指数代码取分位 —— 行业组没有
 * 对应宽基指数（后端 index 给空串），显示"—"是**正确**的，不是缺数据。
 */

/** 份额变化列（键直接取自 EtfFlowIndicator 的字段名，避免手抄字符串写错）。 */
const CHANGE_COLUMNS: {
  key: "change_1d" | "change_5d" | "change_10d" | "change_20d";
  label: string;
}[] = [
  { key: "change_1d", label: "1日" },
  { key: "change_5d", label: "5日" },
  { key: "change_10d", label: "10日" },
  { key: "change_20d", label: "20日" },
];

type Props = {
  snapshot: EtfFlowSnapshot | null;
  loading: boolean;
};

export default function EtfFlowOverview({ snapshot, loading }: Props) {
  const positions = snapshot?.positions ?? [];
  const resonance = snapshot?.resonance ?? [];
  const indicators = snapshot?.indicators ?? [];

  const positionOf = useMemo(() => positionByIndex(positions), [positions]);
  const indexOfGroup = useMemo(() => indexByGroup(resonance), [resonance]);

  if (!snapshot) {
    return (
      <div className="etf-flow-empty">
        {loading
          ? "正在读取 ETF 份额快照…"
          : "还没有数据。点右上「立即刷新」按最新份额重算一次快照。"}
      </div>
    );
  }

  const regime = snapshot.regime;
  /** 分位随系列走：核心锚点组（沪深300）才有，辅助组按自己对应的指数。 */
  const positionFor = (item: EtfFlowIndicator) => {
    const index = indexOfGroup[item.group] ?? "";
    return index ? positionOf[index] ?? null : null;
  };

  return (
    <div className="etf-flow-overview">
      {/* ① 市场环境横幅 */}
      {regime && (
        <section className={`etf-flow-regime regime-${regime.key || "unknown"}`}>
          <div className="etf-flow-regime-top">
            <b className="etf-flow-regime-label">{regime.label || regime.key}</b>
            <span className="muted-text">
              参考指数 {regime.index_name || "—"}
              {regime.index_code ? `（${regime.index_code}）` : ""}
            </span>
            <span>
              近 {regime.window ?? "—"} 日收益
              <b className={"mono " + toneOf(regime.change)}>
                {formatRatioPct(regime.change)}
              </b>
            </span>
            <span className="muted-text">
              收盘 <b className="mono">{formatScore(regime.close, 2)}</b>
            </span>
            <span style={{ flex: 1 }} />
            <span className={"etf-flow-gate-tag "
              + (regime.opportunity_allowed ? "pass" : "block")}>
              机会信号{regime.opportunity_allowed ? "✅ 放行" : "⛔ 不放行"}
            </span>
            <span className={"etf-flow-gate-tag "
              + (regime.risk_allowed ? "pass" : "block")}>
              风险信号{regime.risk_allowed ? "✅ 放行" : "⛔ 不放行"}
            </span>
          </div>

          {/* ⚠️ 这里原来有一块"机会信号不放行"的说明（`.etf-flow-regime-warn`：
              结论 + 原因 + source_notes + gaps）。用户口径（本轮）：它与页签上方的
              `.etf-flow-gate-strip` **重复**，"提示的这些都不要显示" → 已删除，
              门控提示只由那条状态条讲（信息去哪了见文件头注释）。 */}

          {/* 风险信号的门控是另一条规则（默认排除熊市），门控提示条只覆盖机会信号
              → 这条**不重复**，保留 */}
          {!regime.risk_allowed && (
            <div className="etf-flow-regime-warn" role="status">
              <b>当前环境（{regime.label || regime.key}）风险信号同样不放行</b>
              ：这一格回测方向相反（熊市里"高位 + 赎回"之后往往反弹），
              被门控的风险信号也只进观察列表。
            </div>
          )}
        </section>
      )}

      {/* ② 核心宽基 ETF 指标卡 */}
      <section className="etf-flow-card">
        <div className="etf-flow-card-head">
          <h4>核心宽基 ETF 份额</h4>
          <span className="muted-text">
            共 {indicators.length} 只 · 份额单位万份，变化率是相对 N 个交易日前的变化
            （口径见 configs/etf_flow.yaml）
          </span>
        </div>

        {indicators.length === 0 ? (
          <div className="etf-flow-empty">
            暂无 ETF 指标数据（观测清单为空，或份额/行情还没同步到最新交易日）。
            下方「数据缺口」会说明缺的是哪一段 —— 先点「立即刷新」重算。
          </div>
        ) : (
          <div className="etf-flow-grid">
            {indicators.map((item) => {
              const position = positionFor(item);
              /**
               * 份额是否来自更早的交易日（当天份额次日 8:30 才发布）。
               *
               * ⚠️ 这一格必须显式标出来：`shares_date` 与 `trade_date` 不同时，
               * 卡片上的数字是**昨天**的口径，但"收盘"是今天的 —— 不标清楚，
               * 用户会拿昨天的份额配今天的价格做判断。
               */
              const staleShares = Boolean(item.shares_date
                && item.shares_date !== item.trade_date);
              return (
                <div key={item.code} className="etf-flow-metric">
                  <div className="etf-flow-metric-top">
                    <b>{item.name || item.code}</b>
                    <span className="mono muted-text">{item.code}</span>
                    {item.gaps.length > 0 && (
                      <i className="etf-flow-warn-tag"
                         title={item.gaps.join("；")}>数据缺口</i>
                    )}
                  </div>
                  <div className="etf-flow-metric-sub muted-text">
                    <span>{ETF_GROUP_LEVEL_TEXT[item.level] || item.level || "—"}</span>
                    <span>·</span>
                    <span>{item.trade_date ? item.trade_date : "—"}</span>
                  </div>

                  <div className="etf-flow-metric-shares">
                    <span className="muted-text">份额</span>
                    <b className="mono">{formatSharesWan(item.shares)}</b>
                    <span className="muted-text">万份</span>
                    {staleShares && (
                      <i className="muted-text mono"
                         title={`当日（${item.trade_date}）份额次日 8:30 才发布，`
                           + `尚未入库；这里显示的是 ${item.shares_date} 的口径，`
                           + "变化率也以该日为基准"}>
                        （{item.shares_date} 口径）
                      </i>
                    )}
                  </div>

                  <div className="etf-flow-metric-changes">
                    {CHANGE_COLUMNS.map((column) => (
                      <span key={column.key}>
                        <i className="muted-text">{column.label}</i>
                        <b className={"mono " + toneOf(item[column.key])}>
                          {formatRatioPct(item[column.key])}
                        </b>
                      </span>
                    ))}
                  </div>

                  <div className="etf-flow-metric-foot muted-text">
                    <span title={position
                      ? `${position.name || position.code} 近 ${position.window} 日`
                        + `（${position.mode}）区间 ${formatScore(position.low, 2)}`
                        + ` ~ ${formatScore(position.high, 2)}`
                      : "该系列没有对应宽基指数（行业主题组）"}>
                      指数分位
                      <b className="mono">{formatPercentile(position?.percentile ?? null)}</b>
                    </span>
                    <span>C {formatScore(item.close, 3)}</span>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </section>

      {/* ③ 多产品共振指示灯 */}
      <section className="etf-flow-card">
        <div className="etf-flow-card-head">
          <h4>多产品共振</h4>
          <span className="muted-text">
            同系列<b>多只 ETF 同向</b>才算共振（阈值 = 配置里的
            resonance.min_etfs，当前 3 只）；只有 1 只的辅助组天然无法共振，
            显示「未共振」不代表看空
          </span>
        </div>

        {resonance.length === 0 ? (
          <div className="etf-flow-empty">
            暂无共振数据（观测清单为空，或当日各系列份额都没取到）。
          </div>
        ) : (
          <ul className="etf-flow-reso-list">
            {resonance.map((group) => (
              <li key={group.group}
                  className={"etf-flow-reso" + (group.resonance ? " on" : "")}>
                <span className={"etf-flow-light " + directionTone(group.direction)}
                      title={group.direction === "in" ? "当日净申购方向"
                        : group.direction === "out" ? "当日净赎回方向" : "方向不明"} />
                <span className="etf-flow-reso-name">{group.label || group.group}</span>
                <span className="muted-text mono">
                  {group.index_name || group.index || "—"}
                </span>
                <span className="etf-flow-reso-count mono">
                  <b className="up">{group.rising}</b> 增
                  {" / "}
                  <b className="down">{group.falling}</b> 减
                  {" / "}
                  {group.total}
                </span>
                {group.resonance
                  ? <i className="etf-flow-reso-tag">共振</i>
                  : <span className="muted-text">未共振</span>}
                <span className="etf-flow-reso-etfs">
                  {group.etfs.map((etf) => (
                    <span key={etf.code} className="etf-flow-soft-chip"
                          title={`${etf.name || etf.code}（${etf.code}）`
                            + `\n1日 ${formatRatioPct(etf.change_1d)}`
                            + `\n5日 ${formatRatioPct(etf.change_5d)}`}>
                      {etf.name || etf.code}
                      <i className={toneOf(etf.change_1d)}>
                        {formatRatioPct(etf.change_1d)}
                      </i>
                    </span>
                  ))}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
