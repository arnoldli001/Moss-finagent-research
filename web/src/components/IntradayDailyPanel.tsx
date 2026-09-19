import { memo, useCallback, useEffect, useMemo, useState } from "react";
import { api, IntradayDailySignal, IntradayDailySnapshot } from "../api";
import PrivateFeatureNotice from "./PrivateFeatureNotice";
import WeightProfileEditor from "./WeightProfileEditor";

/**
 * 日K级别做T面板（量价体系）。
 *
 * 与「日内分时」模式互补：
 *   分时 → 今天在哪一档动手（VWAP/布林/箱体的分钟级打分）
 *   日K  → 这只票处在主力四阶段的哪一步、该用哪套战法（量柱/高量柱/B1-B15/S1-S6）
 *
 * 布局：K线图（含量柱与高量柱高亮） → 位置与量价形态 → 高量柱攻防 →
 *       买入信号库（逐条条件明细） → 卖出风控 → 保护线 → 高量纪律。
 *
 * 刷新：日线bar在盘中由逐笔驱动、**按分钟更新**（QMT 订阅后当天形成中bar 1 秒内到位），
 * 因此按「数据源更新周期的 3 倍」= 3 分钟自动刷新；非盘中降到 10 分钟兜一次
 * （收盘后日线源补齐当日收盘bar时能自动反映）。
 */

// 服务端未给 refresh_seconds 时的回落值（3 分钟 = 分钟级数据源周期 × 3）
const DEFAULT_REFRESH_SECONDS = 180;
// 非盘中兜底轮询：日线不会变，只是等收盘后数据源补齐当日bar
const IDLE_REFRESH_MS = 10 * 60_000;

// 日K快照的跨挂载缓存：切到"日内分时"再切回来、或在自选之间来回切时，
// 先把上一份日K图直接铺出来（服务端日K快照有 180 秒缓存，前端这份只是省掉往返），
// 再按 3 分钟节奏刷新。用户报的"日K和分时级别的自选股切换都有 2-3 秒延迟"。
const dailyCache = new Map<string, IntradayDailySnapshot>();

/** 是否处于盘中刷新窗口（与后端 `watchlist_refresh_window` 同口径）。 */
function inDailyRefreshWindow(now: Date = new Date()): boolean {
  const day = now.getDay();
  if (day === 0 || day === 6) return false;
  const minutes = now.getHours() * 60 + now.getMinutes();
  return (minutes >= 9 * 60 + 15 && minutes <= 11 * 60 + 30)
    || (minutes >= 13 * 60 && minutes <= 15 * 60);
}

const DIRECTION_CLS: Record<string, string> = {
  bullish: "dir-bullish",
  bearish: "dir-bearish",
  reversal: "dir-reversal",
  neutral: "dir-neutral",
};

function ConditionList({ signal }: { signal: IntradayDailySignal }) {
  return (
    <ul className="cond-list">
      {signal.conditions.map((cond, index) => (
        <li key={index} className={cond.met ? "cond-met" : "cond-miss"}>
          <span className="cond-mark">{cond.met ? "✓" : "✗"}</span>
          <span className="cond-label">
            {cond.label}
            {!cond.required && <em className="cond-soft">（参考项）</em>}
          </span>
          <span className="cond-actual mono">{cond.actual}</span>
          {cond.expected && (
            <span className="muted-text cond-expected">判定：{cond.expected}</span>
          )}
        </li>
      ))}
    </ul>
  );
}

function SignalCard({ signal }: { signal: IntradayDailySignal }) {
  const [open, setOpen] = useState(signal.triggered);
  return (
    <div className={`signal-card ${signal.triggered ? "signal-on" : ""}`}>
      <div className="signal-head" onClick={() => setOpen((v) => !v)}>
        <span className={`sig-code ${signal.triggered ? "on" : ""}`}>
          {signal.code}
        </span>
        <b>{signal.name}</b>
        <span className="signal-score muted-text mono">
          {signal.triggered ? "★ 触发" : `${(signal.score * 100).toFixed(0)}%`}
        </span>
        <span className="muted-text signal-toggle">{open ? "收起" : "展开"}</span>
      </div>
      <p className="signal-reason muted-text">{signal.reason}</p>
      {open && (
        <>
          <ConditionList signal={signal} />
          {(signal.entry !== null || signal.stop_loss !== null) && (
            <div className="signal-levels mono">
              {signal.entry !== null && <span>买点 {signal.entry.toFixed(2)}</span>}
              {signal.stop_loss !== null && (
                <span>止损 {signal.stop_loss.toFixed(2)}</span>
              )}
            </div>
          )}
          {signal.gaps.length > 0 && (
            <div className="signal-gaps muted-text">
              {signal.gaps.map((gap, index) => <div key={index}>· {gap}</div>)}
            </div>
          )}
        </>
      )}
    </div>
  );
}

function IntradayDailyPanel({ code }: { code: string }) {
  const [snapshot, setSnapshot] = useState<IntradayDailySnapshot | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [updatedAt, setUpdatedAt] = useState<string>("");
  // 权重编辑弹窗（日线做T的7因子权重）与保存后的提示
  const [weightOpen, setWeightOpen] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  // 依赖必须是空数组：`load` 一旦依赖 snapshot，下面那个 effect（依赖 load）
  // 就会在每次 setSnapshot 后重跑 → 无限刷新。
  const load = useCallback(async (target: string, refresh = false) => {
    const cached = dailyCache.get(target);
    // 有缓存先铺出来（不闪白、不转圈），随后静默刷新覆盖
    if (cached && !refresh) {
      setSnapshot(cached);
      setLoading(false);
    } else {
      setLoading(true);
    }
    setError(null);
    try {
      const data = await api.intradayDaily(target, refresh);
      dailyCache.set(target, data);
      setSnapshot(data);
      setUpdatedAt(new Date().toLocaleTimeString("zh-CN"));
    } catch (exc) {
      // 失败时**不要**继续用旧 snapshot：那会让"图是旧的"看起来像"图没数据"。
      setSnapshot(null);
      setError(`日K分析失败：${String(exc)}`);
    } finally {
      setLoading(false);
    }
  }, []);

  /**
   * 权重档案保存后：清掉模块级 `dailyCache` 再强刷。
   * 不清缓存的话，切回本面板/切到别的票再切回来会直接命中旧权重的旧快照，
   * 用户会看到「保存成功但分数没变」。
   */
  const refreshAfterSave = useCallback(async (target: string) => {
    dailyCache.delete(target);
    await load(target, true);
  }, [load]);

  /** 数据源更新周期的 3 倍：日线bar盘中由逐笔驱动、按分钟更新 → 3 分钟。 */
  const refreshMs = useMemo(() => {
    const configured = Number(snapshot?.config_snapshot?.refresh_seconds ?? 0);
    return (configured > 0 ? configured : DEFAULT_REFRESH_SECONDS) * 1000;
  }, [snapshot]);

  // 首屏强刷（要拿到**当天形成中**的日线bar，不能吃缓存），之后按周期轮询。
  // 之前这里只有 `useEffect(..., [code])` —— 只在挂载/换标的时取一次，
  // 所以盘中日K页面一直停在那里，必须手动刷新才动。
  useEffect(() => {
    void load(code, true);
    let disposed = false;
    let timer: number | null = null;
    const schedule = () => {
      // 非盘中日线不会变：收盘后按慢节奏兜一次（等日线源补齐当日收盘bar）
      const delay = inDailyRefreshWindow() ? refreshMs : IDLE_REFRESH_MS;
      timer = window.setTimeout(async () => {
        if (disposed) return;
        await load(code);
        if (!disposed) schedule();
      }, delay);
    };
    schedule();
    return () => {
      disposed = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [code, load, refreshMs]);

  if (loading && !snapshot) {
    return (
      <section className="panel intraday-panel">
        <h2>日K（量价体系 + 擒牛线）</h2>
        <div className="empty-tip muted-text"><span className="spinner" /> 正在取日线并跑量价规则…</div>
      </section>
    );
  }
  if (error) return <div className="error-box">{error}</div>;
  if (!snapshot) return null;
  if (!snapshot.available) {
    return (
      <section className="panel intraday-panel">
        <h2>日K（量价体系 + 擒牛线）</h2>
        <div className="warn-box">
          日K分析不可用：{snapshot.health.gaps.join("；") || "日线数据缺失"}
        </div>
      </section>
    );
  }

  const buyTriggered = snapshot.buy_signals.filter((s) => s.triggered);
  const riskTriggered = snapshot.sell_signals.filter((s) => s.triggered);
  const pattern = snapshot.pattern;
  const position = snapshot.position;
  const now = new Date();
  const todayText = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`
    + `-${String(now.getDate()).padStart(2, "0")}`;
  // 日线bar不是当天 = 结论基于上一交易日：显式说出来，别让用户以为分析的是今天
  const barsStale = !!snapshot.trade_date && snapshot.trade_date !== todayText;
  const live = inDailyRefreshWindow();
  // 标记表按**倒序**（最近的在前）：操作日志的读法，与图上从左到右的时间轴互补
  const marks = [...(snapshot.signal_history ?? [])].reverse();

  return (
    <div className="daily-root">
      <section className="panel intraday-panel">
        <div className="panel-head">
          <h2>日K（量价体系 + 擒牛线）</h2>
          <span className={`watch-refresh ${live ? "live" : ""}`}
                title="日线bar盘中由逐笔驱动、按分钟更新；这里取「数据源更新周期的 3 倍」自动刷新">
            {live
              ? `⟳ 每 ${Math.round(refreshMs / 60000)} 分钟自动刷新`
                + `${updatedAt ? ` · ${updatedAt}` : ""}`
              : `⏸ 非交易时段${updatedAt ? ` · 数据 ${updatedAt}` : ""}`}
          </span>
          <span className="muted-text">
            {snapshot.trade_date} · {snapshot.bars.length}根日K ·
            高量柱/安全线·风险线/量价16形态/B1-B15/S1-S6
          </span>
          <button className="btn-ghost" onClick={() => void load(code, true)}
                  title="忽略缓存立即重取日线并重算（收盘后日线源补齐当日bar时用）">
            ↻ 立即刷新
          </button>
          <button className="btn-ghost" onClick={() => setWeightOpen(true)}
                  title="自定义日线做T的7因子权重（合计必须=100，保存后写入权重档案并立即生效）">
            权重编辑
          </button>
        </div>
        {notice && <div className="info-box">{notice}</div>}
        {barsStale && (
          <div className="warn-box">
            当前日线最后一根是 <b>{snapshot.trade_date}</b>，不含今日（{todayText}）
            —— 本页结论基于上一交易日。盘中应当出现当天的形成中bar；
            若持续如此，说明 QMT 日线订阅未生效或日线源尚未补齐。
          </div>
        )}
        <p className="score-verdict">{snapshot.verdict}</p>
        {/* 擒牛线（同花顺公式档位线体系）为商业版功能，开源版不含公式与图表 */}
        <PrivateFeatureNotice feature="擒牛线主图档位线" />
      </section>

      {/* 最近30个交易日的买卖标记（逐bar因果回放） */}
      <section className="panel intraday-panel">
        <div className="panel-head">
          <h3>最近 30 个交易日买卖标记</h3>
          <span className="muted-text">
            ▲ 买点 ｜ ▼ 卖点 / 风控 —— 每根bar只用该日及之前的数据重算，无未来函数；
            同一信号连续触发只记首次。「离场参考」对**卖点**是止损价、
            对**风控**是「跌破就走」的参考价（不是买入建议）。
            图上标记固定在**蜡烛带外侧**（买点在下方、卖点/风控在上方），
            同一根bar同侧只画一个三角 —— 三角旁的数字是信号代码，
            逐个信号的价位见下方表格。本图 Y 轴按**五条擒牛线**取值
            （NML/QRL 是突破线，下跌途中会明显高于股价，属正常），
            想放大线附近的细节可取消勾选某条线，轴会按剩余线重新定标。
          </span>
        </div>
        {marks.length === 0 ? (
          <div className="muted-text">最近 30 个交易日没有触发任何买卖信号</div>
        ) : (
          <table className="audit-table compact-table">
            <thead>
              <tr>
                <th>日期</th><th>方向</th><th>信号</th>
                <th>说明</th><th className="num">当日收盘</th>
                <th className="num" title="买点=建议买入价；风控=参考价（非买入建议）">
                  参考价
                </th>
                <th className="num" title="卖点=止损价；风控/买点若规则不产出该价则为 —">
                  离场参考
                </th>
              </tr>
            </thead>
            <tbody>
              {marks.map((mark) => (
                <tr key={`${mark.date}-${mark.side}-${mark.code}`}>
                  <td className="mono">{mark.date}</td>
                  <td className={
                    mark.side === "buy" ? "impact-positive"
                      : mark.side === "risk" ? "" : "impact-negative"}>
                    {mark.side === "buy" ? "买" : mark.side === "risk" ? "风控" : "卖"}
                  </td>
                  <td className="mono">{mark.code}</td>
                  <td>{mark.name}</td>
                  <td className="num mono">{mark.price?.toFixed(2) ?? "—"}</td>
                  <td className="num mono">{mark.entry?.toFixed(2) ?? "—"}</td>
                  <td className="num mono">{mark.stop_loss?.toFixed(2) ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <div className="daily-grid">
        {/* 位置 + 量价形态 */}
        <section className="panel intraday-panel">
          <h3>位置与量价形态</h3>
          {position && (
            <div className="pos-block">
              <span className={`pos-badge pos-${position.label}`}>
                {position.label}
              </span>
              <span className="muted-text">
                区间分位 {(position.percentile * 100).toFixed(0)}%
                （近{position.window}日 {position.low}~{position.high}）
              </span>
              <div className="pos-detail muted-text mono">
                距高点 {position.from_high_pct.toFixed(2)}% ｜
                距低点 +{position.from_low_pct.toFixed(2)}%
              </div>
              <div className="muted-text">{position.note}</div>
            </div>
          )}
          {pattern && (
            <div className="pattern-block">
              <div className="pattern-head">
                <span className={`dir-tag ${DIRECTION_CLS[pattern.direction]}`}>
                  {pattern.category}·{pattern.name}
                </span>
                <b>{pattern.signal}</b>
              </div>
              <div className="muted-text mono pattern-dims">
                价格 {pattern.price_dir}{pattern.price_speed} ｜
                量能 {pattern.volume_dir}
              </div>
              <p className="muted-text pattern-meaning">
                {pattern.meaning}（{pattern.detail}）
              </p>
            </div>
          )}
        </section>

        {/* 高量柱攻防 */}
        <section className="panel intraday-panel">
          <h3>高量柱攻防（安全线 / 风险线）</h3>
          {snapshot.anchors.length === 0 && (
            <div className="muted-text">近60日未识别到高量柱</div>
          )}
          {snapshot.anchors.length > 0 && (
            <table className="audit-table compact-table">
              <thead>
                <tr>
                  <th>日期</th><th>类型</th><th>位置</th>
                  <th className="num">安全线</th><th className="num">风险线</th>
                  <th>距今</th><th>状态</th>
                </tr>
              </thead>
              <tbody>
                {snapshot.anchors.map((anchor) => (
                  <tr key={anchor.date}>
                    <td className="mono">{anchor.date.slice(5)}</td>
                    <td>{anchor.kind}</td>
                    <td>{anchor.position}</td>
                    <td className="num mono">{anchor.body_top.toFixed(2)}</td>
                    <td className="num mono">{anchor.body_bottom.toFixed(2)}</td>
                    <td className="mono">{anchor.days_since}天</td>
                    <td className={
                      anchor.status === "已破位" ? "impact-negative"
                        : anchor.status === "有效支撑" ? "impact-positive"
                        : "muted-text"}>
                      {anchor.status}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <h3>主力成本线（基准线五形态）</h3>
          {snapshot.cost_lines.length === 0 && (
            <div className="muted-text">近期未识别到成本线形态</div>
          )}
          <ul className="cost-list">
            {snapshot.cost_lines.map((line, index) => (
              <li key={index}>
                <span className="cost-kind">{line.kind}</span>
                <b className="mono">{line.price.toFixed(2)}</b>
                <span className="muted-text mono">{line.date.slice(5)}</span>
                <span className="mono" style={{
                  color: line.distance_pct >= 0 ? "var(--low)" : "var(--high)",
                }}>
                  {line.distance_pct >= 0 ? "+" : ""}{line.distance_pct.toFixed(2)}%
                </span>
                <span className={line.broken ? "impact-negative" : "impact-positive"}>
                  {line.broken ? "已破" : "未破"}
                </span>
              </li>
            ))}
          </ul>

          {snapshot.protective && (
            <>
              <h3>保护线体系（S5）</h3>
              <div className="protect-block">
                <div className="mono">
                  <span className="stat-chip">
                    止损 <b>{snapshot.protective.stop_line ?? "—"}</b>
                    <em className="muted-text">{snapshot.protective.stop_basis}</em>
                  </span>
                  {snapshot.protective.broken_stop && (
                    <span className="stat-chip stop-chip">已跌破止损线</span>
                  )}
                </div>
                <div className="muted-text mono">
                  MA20 {snapshot.protective.ma20?.toFixed(2) ?? "—"} ｜
                  MA60 {snapshot.protective.ma60?.toFixed(2) ?? "—"}
                </div>
                <div className="muted-text">{snapshot.protective.note}</div>
              </div>
            </>
          )}
        </section>
      </div>

      {/* 信号库 */}
      <div className="daily-grid">
        <section className="panel intraday-panel">
          <div className="panel-head">
            <h3>买入信号库 B1–B15</h3>
            <span className="muted-text">
              触发 {buyTriggered.length}/{snapshot.buy_signals.length}
            </span>
          </div>
          {snapshot.buy_signals.map((signal) => (
            <SignalCard key={signal.code} signal={signal} />
          ))}
        </section>

        <section className="panel intraday-panel">
          <div className="panel-head">
            <h3>卖出 / 风控 S1–S6</h3>
            <span className="muted-text">
              触发 {riskTriggered.length}/{snapshot.sell_signals.length}
            </span>
          </div>
          {snapshot.sell_signals.map((signal) => (
            <SignalCard key={signal.code} signal={signal} />
          ))}
          {snapshot.discipline.length > 0 && (
            <>
              <h3>高量纪律 / 操盘口诀命中</h3>
              <ul className="discipline-list">
                {snapshot.discipline.map((item, index) => (
                  <li key={index}>{item}</li>
                ))}
              </ul>
            </>
          )}
        </section>
      </div>

      {snapshot.health.gaps.length > 0 && (
        <div className="warn-box gap-box">
          {snapshot.health.gaps.map((gap, index) => <div key={index}>· {gap}</div>)}
        </div>
      )}
      <div className="disclaimer">{snapshot.disclaimer}</div>

      {weightOpen && (
        <WeightProfileEditor
          code={code}
          name={snapshot.name}
          mode="daily"
          onClose={() => setWeightOpen(false)}
          onSaved={async ({ code: savedCode, describe }) => {
            setNotice(`已保存权重档案并生效：${describe}`);
            await refreshAfterSave(savedCode);
          }}
        />
      )}
    </div>
  );
}

// 报价快车道每 5 秒更新自选列表，不该带着整个日K面板（120 根蜡烛 + 信号库）
// 一起重渲染 —— 它自己按 3 分钟节奏刷新，只有 code 变化才需要重建。
export default memo(IntradayDailyPanel);
