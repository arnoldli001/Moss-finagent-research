import { memo, useCallback, useEffect, useMemo, useState } from "react";
import { api, IntradayDailySignal, IntradayDailySnapshot } from "../api";
import { NiuLineChart } from "../privatePanels";
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
 *       多方条件（逐条条件明细） → 卖出风控 → 保护线 → 高量纪律。
 *
 * 刷新：日线bar在盘中**由日线链的在线源提供**（当天那根是"形成中bar"，未收盘），
 * 因此按「数据源更新周期的 3 倍」= 3 分钟自动刷新；非盘中降到 10 分钟兜一次
 * （收盘后日线源补齐当日收盘bar时能自动反映）。
 *
 * ⚠️ 2026-09-23 修正：这里原先写"QMT 订阅后当天形成中bar 1 秒内到位"。
 * QMT 自 2026-09-22 起已默认关闭，当天这根 bar 的来源改成日线链里的
 * **腾讯财经日K**（`tencent_daily_connector`）—— 注意它**带日期区间时不给当天
 * 形成中bar**，链上已改为"要最新就发不带区间的请求"。面板上那句
 * "QMT 日线订阅未生效"的旧提示也一并改掉了（它会把用户引到一个已关闭的开关上）。
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

/** 今天（本地时区，`YYYY-MM-DD`）—— 与后端 `trade_date` 同口径。
 *  名字刻意不叫 `todayText`：组件里已有一个同名的局部字符串常量（用于渲染提示）。 */
function localToday(now: Date = new Date()): string {
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`
    + `-${String(now.getDate()).padStart(2, "0")}`;
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
              {signal.entry !== null && <span>多方触发 {signal.entry.toFixed(2)}</span>}
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
    // 有缓存先铺出来（不闪白、不转圈），随后静默刷新覆盖。
    // **切标的时也要铺**（键是 target，不是当前 code）：否则切一只没看过的票
    // 会先把上一次的图清掉、转 2~4 秒圈（用户报"日K刷出来要十秒"的观感来源之一）。
    if (cached) {
      setSnapshot(cached);
      setLoading(false);
    } else {
      setLoading(true);
    }
    setError(null);
    try {
      let data = await api.intradayDaily(target, refresh);
      /* 盘中却拿到"不含今日"的快照 → 补一次强刷。
         为什么会有这种快照：服务端日K快照缓存 180 秒，跨过开盘那一刻时里面
         可能还是盘前那一份（当时本来就没有当天 bar）。这种情况很罕见，
         补一次强刷即可；**不要**因此把每次切标的都改成强刷（见下面 effect 的说明）。 */
      if (!refresh && data.trade_date && data.trade_date !== localToday()
          && inDailyRefreshWindow()) {
        data = await api.intradayDaily(target, true);
      }
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

  // 切标的/首屏：**先吃服务端缓存**（`refresh=false`），按 3 分钟节奏轮询。
  //
  // 为什么不再"首屏强刷"（原 `load(code, true)`，2026-09-23 改）：
  //   强刷 = 服务端跳过 180 秒快照缓存 → 每次都重走一遍日线采集链（实测 0.6~3s 网络）
  //   + 重算 250 根bar的量价规则与 30 根bar信号回放（0.7~1.4s CPU）。
  //   用户切一只票就要等 2~4 秒，自选池刷新的日线上下文再一并发起时更慢，
  //   观感就是"日K刷出来要十秒"。
  //   **现在强刷已无必要**：数据层保证"盘中算出来的快照一定含当天形成中bar"
  //   （日线链带 `min_date` 下限，见 docs/INTRADAY_T_DESIGN.md §11.3c），
  //   而服务端快照缓存只有 180 秒 —— 正好等于本面板自己的刷新节奏，
  //   所以缓存里的那份最多比"重新算一份"旧 3 分钟，代价却只有 15~45ms（实测）。
  //   跨开盘那一刻缓存里可能还是盘前的旧快照，`load` 里已有"不含今日就补一次强刷"的兜底。
  //   手动「↻ 立即刷新」按钮仍是强刷，用户想立刻要最新数据时随时可用。
  useEffect(() => {
    void load(code);
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
  // 最后一根**就是今天这根还没收盘的 bar**：形成中的价量还会变（量柱/形态同理），
  // 不标出来用户会把"此刻的量"当成今天最终的量（2026-09-23 起日K真的有当天bar了）。
  const formingBar = live && snapshot.trade_date === todayText;
  // 标记表按**倒序**（最近的在前）：操作日志的读法，与图上从左到右的时间轴互补
  const marks = [...(snapshot.signal_history ?? [])].reverse();

  return (
    <div className="daily-root">
      <section className="panel intraday-panel">
        {/* 面板头 **2026-09-23 按用户口径清空**：原来那行
            「日K（量价体系 + 擒牛线）· 刷新节奏 · 2026-09-23 · 120根日K · 高量柱/…」
            与下面的判决句、图例、图本身重复，白占一整行。
            只留「当日形成中bar」这一个**必须诚实标注**的标记（盘中才出现）——
            形成中的价量还会变，不标出来用户会把此刻的量当成今天最终的量。 */}
        {formingBar && (
          <div className="panel-head">
            <span className="watch-refresh live"
                  title="最后一根是今天这根**还没收盘**的bar：收盘价、成交量、量柱形态都还会随盘中变化；收盘后自动变成当日收盘bar">
              当日形成中bar（未收盘）
            </span>
          </div>
        )}
        {notice && <div className="info-box">{notice}</div>}
        {barsStale && (
          <div className="warn-box">
            当前日线最后一根是 <b>{snapshot.trade_date}</b>，不含今日（{todayText}）
            —— 本页结论基于上一交易日。盘中（含午休）本应出现当天的形成中bar；
            若一直如此，说明在线日线源都还没给出当日bar（休市日属正常），
            可点下方「↻ 立即刷新」重试，并看「数据源健康」里日线链各源的返回。
          </div>
        )}
        {/* 判决句 + 两个操作按钮**同一行**（用户口径 2026-09-23）：
            判决占面板宽的 60%，按钮贴它右侧；窄屏放不下时自动换行。
            原来这两个按钮在面板头那一行 —— 头清掉后挪到这里，别再占一行。 */}
        <div className="daily-verdict-row">
          <p className="score-verdict">{snapshot.verdict}</p>
          <button className="btn-ghost" onClick={() => void load(code, true)}
                  title={"忽略缓存立即重取日线并重算（收盘后日线源补齐当日bar时用）"
                         + (updatedAt ? `；上次取数 ${updatedAt}` : "")}>
            ↻ 立即刷新
          </button>
          <button className="btn-ghost" onClick={() => setWeightOpen(true)}
                  title="自定义日线做T的7因子权重（合计必须=100，保存后写入权重档案并立即生效）">
            权重编辑
          </button>
        </div>
        {/* 擒牛线主图：两个条件都满足才画 ——
            ① 后端有没有给出 niuline 数据（运行时能力）。
               后端 `src/intraday/daily.py` 对 `niuline` 的导入是可选依赖
               （`except ImportError` → 写进 gaps），所以：
                 - 本地/完整版（含 src/intraday/niuline.py）→ 有数据
                 - 开源版（不发布该公式文件）→ 无数据 → 显示占位提示
               刻意不用构建标志位：那种开关会和后端实际能力脱节，
               表现为"图上说没有、后端其实算了"或反过来，且两边都不报错。
            ② `NiuLineChart !== null`（编译期能力）。
               图表组件本身也是私有资产、不进公开仓库，经 `../privatePanels`
               解析：本地有文件 → 组件；公开仓库无文件 → null。
               这一条不能省：若直接静态 import，公开仓库 `tsc` 会报 TS2307，
               整个前端构建失败（详见 src/privatePanels.ts）。 */}
        {snapshot.niuline && NiuLineChart !== null ? (
          <NiuLineChart snapshot={snapshot} />
        ) : (
          <PrivateFeatureNotice feature="擒牛线主图档位线" />
        )}
      </section>

      {/* 最近30个交易日的买卖标记（逐bar因果回放） */}
      <section className="panel intraday-panel">
        <div className="panel-head">
          <h3>最近 30 个交易日买卖标记</h3>
          <span className="muted-text">
            ▲ 多方触发 ｜ ▼ 空方触发 / 风控 —— 每根bar只用该日及之前的数据重算，无未来函数；
            同一信号连续触发只记首次。「离场参考」对**空方触发**是止损价、
            对**风控**是「跌破就走」的参考价（不是买入建议）。
            图上标记固定在**蜡烛带外侧**（多方触发在下方、空方触发/风控在上方），
            同一根bar同侧只画一个三角 —— 三角旁的数字是信号代码，
            逐个信号的价位见下方表格。
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
                <th className="num" title="多方触发=触发价；风控=参考价（非买入建议）">
                  参考价
                </th>
                <th className="num" title="空方触发=止损价；风控/多方触发若规则不产出该价则为 —">
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
            <h3>多方条件 B1–B15</h3>
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
            <h3>风险与离场条件 S1–S6</h3>
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
