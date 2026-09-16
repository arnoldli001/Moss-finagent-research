import { IntradayNews, IntradaySentiment } from "../api";

/**
 * 消息面与市场情绪模块（底部）：
 *   - 板块内涨跌家数（上涨占比 + 横向对比条）
 *   - 个股相对板块强度（跑赢/跑输）
 *   - 大盘状态
 *   - NLP 新闻情绪（DeepSeek/本地模型，含偏多/偏空计数与逐条极性）
 */

const num = (value: number | null | undefined, digits = 2) =>
  value === null || value === undefined ? "—" : value.toFixed(digits);

const pctSigned = (value: number | null | undefined) =>
  value === null || value === undefined
    ? "—"
    : `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;

const POLARITY: Record<string, { label: string; cls: string }> = {
  positive: { label: "偏多", cls: "pol-pos" },
  negative: { label: "偏空", cls: "pol-neg" },
  neutral: { label: "中性", cls: "pol-neu" },
};

/** 涨跌家数对比条：绿(涨) / 灰(跌)，中轴50%。 */
function BreadthBar({ up, down }: { up: number | null; down: number | null }) {
  const total = (up ?? 0) + (down ?? 0);
  if (!total) return <span className="muted-text">涨跌家数缺口</span>;
  const upPct = ((up ?? 0) / total) * 100;
  return (
    <div className="breadth-bar" title={`上涨 ${up} / 下跌 ${down}（共 ${total} 家）`}>
      <div className="breadth-up" style={{ width: `${upPct}%` }} />
      <div className="breadth-down" style={{ width: `${100 - upPct}%` }} />
      <div className="breadth-mid" />
    </div>
  );
}

export default function IntradaySentimentPanel({
  sentiment, news,
}: {
  sentiment: IntradaySentiment | null;
  news: IntradayNews | null;
}) {
  if (!sentiment && !news) {
    return (
      <section className="panel intraday-panel">
        <h2>④ 消息面与市场情绪</h2>
        <div className="empty-tip muted-text">情绪与消息面数据未返回</div>
      </section>
    );
  }
  const items = news?.items ?? [];
  const newsTone = (news?.score ?? 0) > 0.15 ? "var(--high)"
    : (news?.score ?? 0) < -0.15 ? "var(--low)" : "var(--text)";

  return (
    <section className="panel intraday-panel">
      <div className="panel-head">
        <h2>④ 消息面与市场情绪</h2>
        {sentiment?.score !== null && sentiment?.score !== undefined && (
          <span className="muted-text">
            情绪得分 <b className="mono">{sentiment.score >= 0 ? "+" : ""}{sentiment.score.toFixed(2)}</b>
          </span>
        )}
      </div>

      <div className="sent-grid">
        {/* 板块内涨跌家数 */}
        <div className="sent-block">
          <h3>
            板块内涨跌家数
            {sentiment && sentiment.boards_bound === false && (
              <span className="muted-text"> · 参考板块（未绑定，未计入打分）</span>
            )}
          </h3>
          {sentiment?.boards?.length ? (
            <ul className="board-list">
              {sentiment.boards.map((board) => (
                <li key={board.name}>
                  <div className="board-head">
                    <span className="board-name">{board.name}</span>
                    {board.available ? (
                      <>
                        <span className="mono" style={{
                          color: (board.change_pct ?? 0) >= 0 ? "var(--low)" : "var(--high)",
                        }}>
                          {pctSigned(board.change_pct)}
                        </span>
                        <span className="muted-text mono">
                          {board.up_count ?? "—"}/{board.down_count ?? "—"}
                        </span>
                        {board.rank && (
                          <span className="muted-text">排名 {board.rank}</span>
                        )}
                      </>
                    ) : (
                      <span className="muted-text">不可用</span>
                    )}
                  </div>
                  {board.available && (
                    <>
                      <BreadthBar up={board.up_count} down={board.down_count} />
                      <div className="muted-text board-extra mono">
                        {board.breadth !== null && (
                          <span>上涨占比 {(board.breadth * 100).toFixed(0)}%</span>
                        )}
                        {board.amount !== null && <span>成交 {num(board.amount, 1)}亿</span>}
                        {board.net_inflow !== null && (
                          <span style={{
                            color: board.net_inflow >= 0 ? "var(--low)" : "var(--high)",
                          }}>
                            净流入 {num(board.net_inflow, 1)}亿
                          </span>
                        )}
                      </div>
                    </>
                  )}
                  {!board.available && board.gap && (
                    <div className="muted-text board-gap">{board.gap}</div>
                  )}
                </li>
              ))}
            </ul>
          ) : (
            <div className="muted-text">无关联板块数据</div>
          )}
        </div>

        {/* 相对强度 + 大盘 */}
        <div className="sent-block">
          <h3>个股相对板块强度</h3>
          <div className="rs-row">
            <div className="rs-cell">
              <span className="stat-label">个股涨跌</span>
              <b className="mono" style={{
                color: (sentiment?.stock_change_pct ?? 0) >= 0 ? "var(--low)" : "var(--high)",
              }}>
                {pctSigned(sentiment?.stock_change_pct)}
              </b>
            </div>
            <div className="rs-cell">
              <span className="stat-label">板块涨跌</span>
              <b className="mono" style={{
                color: (sentiment?.board_change_pct ?? 0) >= 0 ? "var(--low)" : "var(--high)",
              }}>
                {pctSigned(sentiment?.board_change_pct)}
              </b>
            </div>
            <div className="rs-cell">
              <span className="stat-label">
                相对强度
                {sentiment?.relative_strength_pct !== null
                  && sentiment?.relative_strength_pct !== undefined && (
                    <em className="muted-text">
                      {" "}
                      {sentiment.relative_strength_pct >= 0 ? "跑赢" : "跑输"}
                    </em>
                  )}
              </span>
              <b className="mono" style={{
                color: (sentiment?.relative_strength_pct ?? 0) >= 0
                  ? "var(--high)" : "var(--low)",
              }}>
                {pctSigned(sentiment?.relative_strength_pct)}
              </b>
            </div>
          </div>
          <div className="muted-text rs-scores mono">
            涨跌家数分 {num(sentiment?.breadth_score)} ／ 相对强度分 {num(sentiment?.rs_score)}
            <span className="muted-text"> （各占50%合成情绪因子得分）</span>
          </div>

          <h3>大盘状态</h3>
          <div className="index-row">
            <span>{sentiment?.index_name || "—"}</span>
            <b className="mono" style={{
              color: (sentiment?.index_change_pct ?? 0) >= 0 ? "var(--low)" : "var(--high)",
            }}>
              {num(sentiment?.index_price)} {pctSigned(sentiment?.index_change_pct)}
            </b>
            <span className="zone-badge zone-neutral">
              {sentiment?.index_state || "—"}
            </span>
          </div>
          <p className="muted-text sent-verdict">{sentiment?.verdict}</p>
        </div>

        {/* 新闻情绪 */}
        <div className="sent-block">
          <h3>
            消息面 NLP 情绪
            <span className="muted-text">
              {" "}
              {news?.llm_used
                ? `AI打分 ${num(news.llm_score)}（${news.model || "模型"}）`
                : "AI不可用，关键词计数口径"}
            </span>
          </h3>
          {news?.available ? (
            <>
              <div className="news-counts">
                <span className="stat-chip">
                  情绪分 <b className="mono" style={{ color: newsTone }}>
                    {news.score >= 0 ? "+" : ""}{num(news.score)}
                  </b>
                </span>
                <span className="stat-chip">
                  偏多 <b className="mono pol-pos">{news.positive_count}</b>
                </span>
                <span className="stat-chip">
                  偏空 <b className="mono pol-neg">{news.negative_count}</b>
                </span>
                <span className="stat-chip">
                  中性 <b className="mono">{news.neutral_count}</b>
                </span>
                <span className="stat-chip muted-text">
                  共 {news.news_count} 条
                </span>
              </div>
              {news.summary && <p className="news-summary">{news.summary}</p>}
              <ul className="news-list">
                {items.slice(0, 6).map((item, index) => {
                  const polarity = POLARITY[item.polarity] ?? POLARITY.neutral;
                  return (
                    <li key={index}>
                      <span className={`pol-tag ${polarity.cls}`}>{polarity.label}</span>
                      {item.source_url ? (
                        <a href={item.source_url} target="_blank" rel="noreferrer">
                          {item.title || "（无标题）"}
                        </a>
                      ) : (
                        <span>{item.title || "（无标题）"}</span>
                      )}
                      <span className="muted-text news-meta">
                        {" "}{item.source_name} {item.publish_time}
                      </span>
                    </li>
                  );
                })}
              </ul>
            </>
          ) : (
            <div className="muted-text">{news?.gap ?? "新闻数据不可用"}</div>
          )}
          {news?.gap && news.available && (
            <div className="warn-box gap-box">{news.gap}</div>
          )}
        </div>
      </div>

      {sentiment?.gaps && sentiment.gaps.length > 0 && (
        <div className="warn-box gap-box">
          情绪面缺口：{sentiment.gaps.join("；")}
        </div>
      )}
    </section>
  );
}
