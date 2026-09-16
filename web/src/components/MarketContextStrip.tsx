import { IntradaySnapshot } from "../api";

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
  const ratioTone = ratio === null ? "var(--muted)"
    : ratio >= 1.1 ? "var(--medium)" : ratio <= 0.9 ? "var(--muted)"
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

export default function MarketContextStrip({
  snapshot,
}: {
  snapshot: IntradaySnapshot | null;
}) {
  if (!snapshot) return null;
  const { index_volume: indexVolume, overseas, sentiment } = snapshot;
  const boards = sentiment?.boards ?? [];
  if (!indexVolume && !overseas && boards.length === 0) return null;

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
    </div>
  );
}
