import { api, IntradaySnapshot } from "../api";
import { useEffect, useState } from "react";

/**
 * 市场环境条（新增三因子的原始数据展示）。
 *
 * ① 指数量能：个股所属指数（上证/创业板/科创50/深证成指）当日成交量的全天预测量
 *    相比昨日是增量还是缩量 —— 与指数涨跌配合决定方向分。
 * ② 板块涨幅排行：关联板块涨跌幅 + 在全A板块中的排名分位。
 * ③ 海外映射：美股隔夜涨跌（驱动跳空）+ 韩国SK海力士盘中同步
 *    （韩股 09:00–15:30 KST 与A股同时开市，是同步信号而非隔夜）。
 */

const pct = (v: number | null | undefined) =>
  v === null || v === undefined ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`;

const tone = (v: number | null | undefined) =>
  (v ?? 0) >= 0 ? "var(--low)" : "var(--high)";

function VolumeChip({ indexVolume }: { indexVolume: NonNullable<IntradaySnapshot["index_volume"]> }) {
  if (!indexVolume.available) {
    return (
      <span className="market-chip">
        指数量能 <em className="muted-text">{indexVolume.gap ?? "不可用"}</em>
      </span>
    );
  }
  const ratio = indexVolume.volume_ratio;
  const ratioText = ratio === null ? "量能比缺失"
    : ratio >= 1.1 ? `放量 ${ratio.toFixed(2)}×`
    : ratio <= 0.9 ? `缩量 ${ratio.toFixed(2)}×`
    : `平量 ${ratio.toFixed(2)}×`;
  // 用户口径 2026-09-23："缩量 放量 禁止追高 的提示块改成红色字体"。
  // 所以这里**放量与缩量都取红**（`--low`）—— 与上面 `CycleDecisionBadge` 里
  // 量能那一段同色。同一行里同一个词不能有两种颜色，否则看起来像两套结论。
  // 平量/缺失保持中性：它们不构成提示。
  const ratioTone = ratio === null ? "var(--muted)"
    : (ratio >= 1.1 || ratio <= 0.9) ? "var(--low)"
    : "var(--text)";
  return (
    <span className="market-chip" title={
      `${indexVolume.verdict || ratioText}；`
      + `今日累计 ${indexVolume.volume?.toFixed(0) ?? "—"}，`
      + `已交易时间占比 ${(indexVolume.elapsed_ratio * 100).toFixed(0)}%，`
      + `全天预测 ${indexVolume.projected_volume?.toFixed(0) ?? "—"}，`
      + `昨日 ${indexVolume.yesterday_volume?.toFixed(0) ?? "—"}`}>
      <b>{indexVolume.name || indexVolume.code}</b>
      <span className="mono" style={{ color: tone(indexVolume.change_pct) }}>
        {pct(indexVolume.change_pct)}
      </span>
      <em className="mono" style={{ color: ratioTone }}>{ratioText}</em>
    </span>
  );
}

function OverseasChip({ overseas }: { overseas: NonNullable<IntradaySnapshot["overseas"]> }) {
  if (!overseas.available) {
    return (
      <span className="market-chip">
        海外映射 <em className="muted-text">{overseas.verdict || "不可用"}</em>
      </span>
    );
  }
  const quotes = (overseas.quotes ?? []).filter((q) => q.available);
  return (
    <span className="market-chip overseas-chip"
          title={overseas.gaps.join("；") || "海外映射全部可用"}>
      <b>海外映射</b>
      <span className="mono" style={{ color: tone(overseas.score) }}>
        {overseas.score === null ? "—"
          : `${overseas.score >= 0 ? "+" : ""}${overseas.score.toFixed(2)}`}
      </span>
      {quotes.map((quote) => (
        <em key={quote.symbol} className="overseas-item"
            title={`${quote.symbol} ${quote.quote_time}`
              + (quote.market === "kr" ? "（韩股，盘中同步）" : "（美股，隔夜）")}>
          {quote.name}
          <span className="mono" style={{ color: tone(quote.change_pct) }}>
            {" "}{pct(quote.change_pct)}
          </span>
        </em>
      ))}
    </span>
  );
}

function DecisionBadge({
  decision,
}: {
  decision: NonNullable<IntradaySnapshot["cycle_decision"]>;
}) {
  if (!decision.available) return null;
  // 一票否决（大面/跌停超标）与"退潮期禁止回踩"是**今天的操作结论**，
  // 不是数据状态 —— 所以给最醒目的位置（市场环境行右侧）与最强对比色。
  const tone = !decision.t_allowed ? "veto" : decision.no_chase ? "warn" : "ok";
  const turnover = decision.turnover;
  // 量能那一段（用户口径：缩量XX亿不追高 / 放量XX亿可做T）。
  // 与情绪周期**合成同一枚**徽标：两者回答的是同一个问题（今天能不能做T/追高），
  // 并排两枚会让人以为是两套独立结论，而实际是**两个否决来源**（任一否决即整体否决）。
  const snapshotTurnover = decision.turnover_text || "";
  /* ★ 快照里没有量能时，**自己实时读**（用户口径 2026-09-24：
     "量能可以前端实时显示，不可能量能变化为空"）。
     原来量能只搭在个股快照里，而那条路为不让 46 只票整表重算变慢，
     最多等 2.5 秒、等不到就整段不显示 —— 冷启动/数据源熔断时用户看到的是
     "量能那段凭空消失"。现在改为：缺了就单独问一次
     `/intraday/market-turnover`（只读、不等待），并每 60 秒刷新一次。 */
  const [liveTurnover, setLiveTurnover] = useState<string>("");
  useEffect(() => {
    if (snapshotTurnover) return;          // 快照已有就不用多打一次
    let alive = true;
    const load = async () => {
      try {
        const r = await api.marketTurnover();
        if (alive) setLiveTurnover(r.available ? r.text : "");
      } catch { /* 量能读不到不影响其它内容 */ }
    };
    void load();
    const timer = window.setInterval(() => { void load(); }, 60000);
    return () => { alive = false; window.clearInterval(timer); };
  }, [snapshotTurnover]);
  const turnoverText = snapshotTurnover || liveTurnover;
  /* 结论句（后端拼好的 `summary`，形如「退潮期 · 缩量 3,706 亿 · 不做T降本 · 禁止追高」）
     里同样要去掉「（不）做T降本」—— 徽标上删了、hover 里又冒出来等于没删。
     只在前端做展示层裁剪，后端契约不动（`t_allowed` 仍在返回，其它调用方可能用）。 */
  const summaryText = String(decision.summary || "")
    .replace(/\s*·?\s*(不宜|适合)区间操作/g, "")
    .replace(/^\s*·\s*|\s*·\s*$/g, "")
    .trim();
  const title = [
    summaryText,
    decision.temperature !== null && decision.temperature !== undefined
      ? `做T环境温度 ${decision.temperature}/100` : "",
    turnoverText
      ? `全市场量能（${turnover?.moment ? `截至 ${turnover.moment.slice(0, 2)}:${turnover.moment.slice(2)}` : "盘中"}）：`
        + `${turnoverText}`
        + (turnover?.projected_amount != null
            ? `；预测全天 ${(turnover.projected_amount / 1e12).toFixed(2)} 万亿`
            : "")
        + (turnover?.prev_total_amount != null
            ? `，昨日 ${(turnover.prev_total_amount / 1e12).toFixed(2)} 万亿`
            : "")
        + (turnover?.ratio != null ? `（${turnover.ratio.toFixed(2)}×）` : "")
      : "",
    ...(turnover?.reasons ?? []),
    ...(decision.reasons ?? []),
    ...(turnover?.notes ?? []),
    decision.trade_date ? `数据日 ${decision.trade_date}` : "",
  ].filter(Boolean).join("\n");
  /* 配色（用户口径 2026-09-23）：**只用白色与蓝色**。
     原来阶段+flag 跟着徽标主色走（绿/橙/红），量能段又自成一套红绿，
     一行里四种颜色谁也压不住谁；现在：阶段 = 白（是什么环境）、
     量能 = 蓝（环境里的燃料）、flag = 蓝（所以怎么做）。
     徽标外框/底色仍保留 ok/warn/veto 三档，结论的严重程度不丢。
     ⚠️ 「不做T降本 / 可做T降本」这条**按用户要求删除**（2026-09-23：
     "不现实这种提示"）—— 后端 `cycle_decision` 仍在返回该字段，前端不再展示。 */
  return (
    <span className={`cycle-badge ${tone}`} title={title}>
      {decision.stage && <b className="cycle-stage">{decision.stage}</b>}
      {turnoverText ? (
        // 量能那一段：保留「缩量 1,180 亿 / 放量 320 亿」的数字，
        // 把「不追高 / 可做T」交给后面的 flags —— 否则会和「禁止追高」重复两遍。
        <span className="cycle-turnover">
          {turnoverText.replace(" 不追高", "").replace(" 可做T", "")}
        </span>
      ) : (
        // 取不到就**说出来**，不要静默少一段（用户会以为功能被删了）
        <span className="cycle-turnover muted-text" title="数据源可能正在熔断冷却，后台自动重试">
          量能取数中…
        </span>
      )}
      {/* ★ 中性的环境提示（用户口径 2026-09-24："按你说的建议来"）。
          原来是「可做T降本 / 不做T降本」—— 那是**操作建议**（合规自查里与
          "建议买价""强制卖出"同类），已被要求删除；现在改成只描述环境：
          「适合区间操作 / 不宜区间操作」，动作交给用户自己判断。 */}
      <span className="cycle-flag">
        {decision.t_allowed ? "适合区间操作" : "不宜区间操作"}
      </span>
      {decision.no_chase && <span className="cycle-flag">禁止追高</span>}
    </span>
  );
}

export default function MarketContextStrip({
  snapshot,
}: {
  snapshot: IntradaySnapshot | null;
}) {
  if (!snapshot) return null;
  const { index_volume: indexVolume, overseas, sentiment, cycle_decision: decision } =
    snapshot;
  const boards = sentiment?.boards ?? [];
  if (!indexVolume && !overseas && boards.length === 0 && !decision) return null;

  return (
    <div className="market-strip">
      <span className="muted-text">市场环境：</span>
      {indexVolume && <VolumeChip indexVolume={indexVolume} />}
      {boards.filter((b) => b.available).map((board) => {
        // rank 形如 "10/390" → 分位 = 1-(名次-1)/(总数-1)
        let rankText = "";
        let percentile: number | null = null;
        if (board.rank && board.rank.includes("/")) {
          const [position, total] = board.rank.split("/").map(Number);
          if (position >= 1 && total > 1) {
            percentile = 1 - (position - 1) / (total - 1);
            rankText = `排名 ${position}/${total}（前${Math.max(1, Math.round((1 - percentile) * 100))}%）`;
          }
        }
        return (
          <span key={board.name} className="market-chip"
                title={`${board.name} 上涨${board.up_count ?? "—"}家/下跌${board.down_count ?? "—"}家`}>
            <b>{board.name}</b>
            <span className="mono" style={{ color: tone(board.change_pct) }}>
              {pct(board.change_pct)}
            </span>
            {rankText && (
              <em className="mono" style={{
                color: percentile !== null && percentile >= 0.75 ? "var(--low)"
                  : percentile !== null && percentile <= 0.25 ? "var(--high)"
                  : "var(--muted)",
              }}>{rankText}</em>
            )}
          </span>
        );
      })}
      {overseas && <OverseasChip overseas={overseas} />}
      {/* 决策结论固定在**最右侧**：它是这一行里唯一"要我今天怎么做"的信息，
          而前面的指数/板块/海外都是"发生了什么"。 */}
      {decision && <span className="market-strip-spacer" />}
      {decision && <DecisionBadge decision={decision} />}
    </div>
  );
}
