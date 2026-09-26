import { useCallback, useEffect, useMemo, useState } from "react";
import {
  formatDate,
  formatPct,
  formatScore,
  formatYi,
  mainlineApi,
  toneOf,
  type MainlineBoardDetail,
  type MainlineDimension,
  type MainlineHistoryPoint,
  type MainlineLayer,
} from "../mainlineApi";

/**
 * 主线挖掘 · 下钻面板（雷达图 + 三层明细 + 龙头股 + 评分走势）。
 *
 * ## 为什么雷达图/走势图是手写 SVG
 *
 * 项目没有图表库（`package.json` 里只有 react/react-dom/marked），而这个模块
 * 需要的两张图都极其简单：一张六轴雷达、一条折线。引一个 200KB 的图表库只为画
 * 这两张图，等于给"资金流监控"这一屏加一个长期依赖；手写 SVG 反而更可控 ——
 * 配色直接吃 CSS 变量（`var(--accent)` 等），主题切换不用二次适配。
 *
 * ## 雷达图的轴不是硬编码的
 *
 * 六个轴来自 `layers[0].dimensions`（开源六维基座的 6 个维度），**顺序和数量都
 * 跟后端走**：后端加/减一个维度，这里自动跟着变。轴数 < 3 时退化成"无法画雷达"，
 * 直接不给图 —— 画个三角形会让人以为模型只有三个维度。层里没给 dimensions 时，
 * 退回用"六维分 / 建仓分 / 总分"三根轴兜底。
 *
 * ## 分数缺失（null）怎么画
 *
 * `null` ≠ 0。某个维度今天没算出来时，该轴的点取 **0**（图形缺口会明显凹进去，
 * 这正是我们想让人看到的"这块没数据"），同时在图例和明细表里写清楚"缺数据"。
 * 反过来，如果整层 `available=false`，那一层在明细表里整块标灰并显示 `notes`。
 *
 * ## 弹层行为
 *
 * 固定定位遮罩 + Esc / 点遮罩 / ✕ 关闭，与 `SectorCrowdingDetailModal` 同一套交互，
 * 不引入 portal（项目里其它弹窗也都是直接渲染 DOM）。
 */

type Props = {
  boardCode: string;
  boardName?: string;
  /** 指定交易日；留空 = 后端最新 */
  tradeDate?: string;
  /** 历史窗口（交易日），对应 `/board/{code}?days=` */
  days?: number;
  onClose: () => void;
};

/** 雷达图几何：正方 region，半径按 score 100 满格。 */
const RADAR = { size: 320, cx: 160, cy: 168, radius: 108 };
const TREND = { w: 660, h: 190, left: 44, right: 16, top: 16, bottom: 34 };

/** 极坐标 → 直角坐标（-90° 起画，第一根轴朝正上方）。 */
function polar(cx: number, cy: number, radius: number, index: number,
               count: number): { x: number; y: number } {
  const angle = (Math.PI * 2 * index) / count - Math.PI / 2;
  return { x: cx + radius * Math.cos(angle), y: cy + radius * Math.sin(angle) };
}

/** 把一个可能为 null 的分数压回 [0,100]（用于图形几何，展示仍走 formatScore）。 */
function clamp100(value: number | null | undefined): number {
  if (value === null || value === undefined || !Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(100, value));
}

/** 轴标签压缩：中文标签在雷达图外圈只能放 4 个字，超出用省略号。 */
function shortLabel(label: string, max = 5): string {
  const text = label.trim();
  return text.length <= max ? text : `${text.slice(0, max - 1)}…`;
}

/** raw 因子值 → 可读文本（后端 raw 是自由结构，只做有限几种渲染）。 */
function rawText(raw: MainlineDimension["raw"]): string {
  const parts: string[] = [];
  for (const [key, value] of Object.entries(raw ?? {})) {
    if (value === null || value === undefined || value === "") continue;
    const text = typeof value === "number"
      ? (Number.isFinite(value) ? String(Number(value.toFixed(4))) : "—")
      : String(value);
    parts.push(`${key}=${text}`);
  }
  return parts.join("  ");
}

/** 层可用性文案（coverage < 1 说明有维度缺数据，要能一眼看出）。 */
function coverageText(layer: MainlineLayer): string {
  if (layer.coverage === null || layer.coverage === undefined
      || !Number.isFinite(layer.coverage)) {
    return layer.available ? "可用" : "不可用";
  }
  const percent = Math.round(layer.coverage * 100);
  return `覆盖 ${percent}%`;
}

export default function MainlineRadar({
  boardCode, boardName, tradeDate, days = 60, onClose,
}: Props) {
  const [detail, setDetail] = useState<MainlineBoardDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    if (!boardCode) return;
    setLoading(true);
    setError("");
    try {
      setDetail(await mainlineApi.board(boardCode,
        { tradeDate, days }));
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
      setDetail(null);
    } finally {
      setLoading(false);
    }
  }, [boardCode, tradeDate, days]);

  useEffect(() => { void load(); }, [load]);

  // Esc 关闭：与项目里其它弹窗一致（用户按下 Esc 时期待的是"关掉最上层"）
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const score = detail?.score ?? null;
  const name = detail?.name || boardName || boardCode;

  /** 雷达轴：优先取第一层（开源六维基座）的维度；没有则退回三层分数兜底。 */
  const axes = useMemo(() => {
    const first = detail?.layers?.find(
      (layer) => Array.isArray(layer.dimensions) && layer.dimensions.length >= 3);
    if (first) {
      return first.dimensions.map((dim) => ({
        label: dim.label || dim.key,
        value: clamp100(dim.score),
        missing: dim.available === false
          || dim.score === null || dim.score === undefined,
        hint: dim.note || rawText(dim.raw),
      }));
    }
    if (!score) return [];
    return [
      { label: "六维基座", value: clamp100(score.six_dim_score),
        missing: score.six_dim_score === null, hint: "" },
      { label: "建仓资金", value: clamp100(score.accumulation_score),
        missing: score.accumulation_score === null, hint: "" },
      { label: "龙头集中", value: clamp100(score.leader_score),
        missing: score.leader_score === null, hint: "" },
      { label: "总分", value: clamp100(score.total), missing: false, hint: "" },
    ];
  }, [detail, score]);

  const layers = detail?.layers ?? [];
  const history = detail?.history ?? [];
  const tracked = detail?.tracked ?? null;
  const leaders = score?.leaders ?? [];

  return (
    <div className="mainline-modal-mask" onClick={onClose} role="presentation">
      <div className="mainline-modal" role="dialog" aria-modal="true"
           aria-label={`${name} 主线挖掘下钻`}
           onClick={(event) => event.stopPropagation()}>
        <div className="mainline-modal-head">
          <h3>
            {name}
            <span className="mono muted-text"> {boardCode}</span>
            {score?.kind ? <span className="muted-text"> · {score.kind}</span> : null}
          </h3>
          <span className="muted-text">
            交易日 {formatDate(detail?.trade_date || tradeDate || score?.trade_date)}
            {score?.rank !== null && score?.rank !== undefined
              ? <> · 排名 #{score.rank}</> : null}
            {score?.selected ? <b className="mainline-tag-selected"> 精选</b> : null}
            {score?.candidate && !score?.selected
              ? <span className="mainline-tag-candidate"> 候选</span> : null}
          </span>
          <span style={{ flex: 1 }} />
          <button className="btn-ghost tiny" onClick={() => void load()}
                  disabled={loading}>
            {loading ? "读取中…" : "↻ 重新读取"}
          </button>
          <button className="btn-ghost tiny" onClick={onClose}
                  aria-label="关闭">✕</button>
        </div>

        <div className="mainline-modal-body">
          {error && <div className="error-box">读取下钻数据失败：{error}</div>}
          {loading && !detail && <div className="info-box">正在读取板块明细…</div>}

          {/* ① 总分横幅：先给结论，再给明细 */}
          {score && (
            <div className="mainline-score-banner">
              <div className="mainline-score-main">
                <span className="mainline-score-value">
                  {formatScore(score.total)}
                </span>
                <span className="muted-text">总分（含门控）</span>
              </div>
              <div className="mainline-score-cells">
                <div><span className="muted-text">六维基座</span>
                  <b>{formatScore(score.six_dim_score)}</b></div>
                <div><span className="muted-text">建仓资金</span>
                  <b>{formatScore(score.accumulation_score)}</b></div>
                <div><span className="muted-text">龙头集中</span>
                  <b>{formatScore(score.leader_score)}</b></div>
                <div><span className="muted-text">门控加分</span>
                  <b>{formatScore(score.gate_bonus)}</b></div>
                <div><span className="muted-text">基础分</span>
                  <b>{formatScore(score.base_total)}</b></div>
                <div><span className="muted-text">当日涨跌</span>
                  <b className={toneOf(score.change_pct)}>
                    {formatPct(score.change_pct)}
                  </b></div>
                <div><span className="muted-text">当日主力净流入</span>
                  <b className={toneOf(score.net_today)}>
                    {formatYi(score.net_today)}
                  </b></div>
                <div><span className="muted-text">衰减周期</span>
                  <b>{score.decay_days === null || score.decay_days === undefined
                    ? "—" : `${score.decay_days} 日`}</b></div>
              </div>
              <div className="mainline-score-meta muted-text">
                权重模式 {score.weight_mode || "—"}
                {score.weights
                  ? ` · ${Object.entries(score.weights)
                      .map(([key, value]) => `${key} ${formatScore(value, 1)}`)
                      .join(" / ")}`
                  : ""}
                {score.level_label ? ` · 等级 ${score.level_label}` : ""}
                {score.resonance
                  ? ` · 共振${score.resonance_ratio === null
                    || score.resonance_ratio === undefined
                    ? "" : `（比例 ${(score.resonance_ratio * 100).toFixed(0)}%）`}`
                  : ""}
              </div>
              {score.reasons.length > 0 && (
                <ul className="mainline-reasons">
                  {score.reasons.map((text, index) => (
                    <li key={index}>{text}</li>
                  ))}
                </ul>
              )}
              {score.seat_note && (
                <div className="mainline-seat-note">席位备注：{score.seat_note}</div>
              )}
              {score.gaps.length > 0 && (
                <div className="mainline-gaps">
                  ⚠️ 缺失数据：{score.gaps.join("；")}
                </div>
              )}
            </div>
          )}

          <div className="mainline-drill-grid">
            {/* ② 六维雷达 */}
            <section className="mainline-card">
              <div className="mainline-card-head">
                <h4>六维雷达</h4>
                <span className="muted-text">
                  {axes.length >= 3
                    ? `${axes.length} 轴，满分 100`
                    : "维度不足，无法绘制"}
                </span>
              </div>
              {axes.length >= 3 ? (
                <svg className="mainline-radar-svg"
                     viewBox={`0 0 ${RADAR.size} ${RADAR.size}`}
                     role="img" aria-label={`${name} 六维雷达图`}>
                  {/* 网格环 20/40/60/80/100：5 圈足够读数，再多会糊成一团 */}
                  {[20, 40, 60, 80, 100].map((ring) => (
                    <polygon key={`ring-${ring}`}
                             points={axes.map((_, index) => {
                               const point = polar(RADAR.cx, RADAR.cy,
                                 (RADAR.radius * ring) / 100, index, axes.length);
                               return `${point.x},${point.y}`;
                             }).join(" ")}
                             fill="none" stroke="var(--border)"
                             strokeWidth={ring === 100 ? 1.2 : 0.6}
                             opacity={ring === 100 ? 0.9 : 0.5} />
                  ))}
                  {/* 轴线 + 轴标签 */}
                  {axes.map((axis, index) => {
                    const outer = polar(RADAR.cx, RADAR.cy, RADAR.radius,
                      index, axes.length);
                    const label = polar(RADAR.cx, RADAR.cy, RADAR.radius + 22,
                      index, axes.length);
                    // 标签按所在象限选锚点，否则右侧的字会盖住图形
                    const anchor = Math.abs(label.x - RADAR.cx) < 6 ? "middle"
                      : label.x > RADAR.cx ? "start" : "end";
                    return (
                      <g key={`axis-${axis.label}-${index}`}>
                        <line x1={RADAR.cx} y1={RADAR.cy}
                              x2={outer.x} y2={outer.y}
                              stroke="var(--border)" opacity={0.55} />
                        <text x={label.x} y={label.y + 3} fontSize="10"
                              fill={axis.missing ? "var(--medium)" : "var(--muted)"}
                              textAnchor={anchor}>
                          {shortLabel(axis.label)}{axis.missing ? "*" : ""}
                        </text>
                      </g>
                    );
                  })}
                  {/* 数据多边形：缺失维度取 0，图形上会明显凹进去 */}
                  <polygon points={axes.map((axis, index) => {
                              const point = polar(RADAR.cx, RADAR.cy,
                                (RADAR.radius * axis.value) / 100, index, axes.length);
                              return `${point.x},${point.y}`;
                            }).join(" ")}
                           fill="rgba(74,158,255,0.22)"
                           stroke="var(--accent)" strokeWidth="1.6" />
                  {axes.map((axis, index) => {
                    const point = polar(RADAR.cx, RADAR.cy,
                      (RADAR.radius * axis.value) / 100, index, axes.length);
                    return (
                      <circle key={`dot-${axis.label}-${index}`}
                              cx={point.x} cy={point.y} r={2.6}
                              fill={axis.missing ? "var(--medium)" : "var(--accent)"}>
                        <title>{`${axis.label} ${formatScore(
                          axis.value, 1)}${axis.missing ? "（缺数据，按 0 画）" : ""}`
                          + (axis.hint ? `\n${axis.hint}` : "")}</title>
                      </circle>
                    );
                  })}
                  <text x={RADAR.cx} y={RADAR.size - 4} fontSize="10"
                        fill="var(--muted)" textAnchor="middle">
                    带 * 的轴表示该维度当日缺数据（按 0 计入图形）
                  </text>
                </svg>
              ) : (
                <div className="mainline-empty">该板块没有可用的维度明细</div>
              )}
            </section>

            {/* ③ 近 N 日评分走势 */}
            <section className="mainline-card">
              <div className="mainline-card-head">
                <h4>近 {days} 日评分走势</h4>
                <span className="muted-text">
                  {history.length > 0
                    ? `${history.length} 个交易日（${formatDate(history[0]?.trade_date)}
                       → ${formatDate(history[history.length - 1]?.trade_date)}）`
                    : "无历史评分"}
                </span>
              </div>
              {history.length >= 2 ? (
                <MainlineTrendChart points={history} />
              ) : (
                <div className="mainline-empty">
                  历史评分不足 2 天，画不出走势（后端按交易日逐日落库，
                  新板块要等几天才有曲线）。
                </div>
              )}
            </section>
          </div>

          {/* ④ 三层明细表 */}
          <section className="mainline-card">
            <div className="mainline-card-head">
              <h4>三层评分明细</h4>
              <span className="muted-text">
                六维基座 + 建仓资金按权重合成基础分，门控项单独加分
              </span>
            </div>
            {layers.length === 0 ? (
              <div className="mainline-empty">后端未返回分层明细</div>
            ) : (
              layers.map((layer) => (
                <div key={layer.key}
                     className={"mainline-layer"
                       + (layer.available ? "" : " off")}>
                  <div className="mainline-layer-head">
                    <b>{layer.label || layer.key}</b>
                    <span className="muted-text">权重 {formatScore(layer.weight, 1)}</span>
                    <span className="muted-text">{coverageText(layer)}</span>
                    <span style={{ flex: 1 }} />
                    <b className="mono">{formatScore(layer.score)}</b>
                  </div>
                  {layer.notes.length > 0 && (
                    <div className="muted-text mainline-layer-note">
                      {layer.notes.join("；")}
                    </div>
                  )}
                  {layer.dimensions.length > 0 && (
                    <ul className="mainline-dim-list">
                      {layer.dimensions.map((dim) => (
                        <li key={`${layer.key}-${dim.key}`}
                            className={dim.available ? "" : "off"}>
                          <span className="mainline-dim-name">
                            {dim.label || dim.key}
                          </span>
                          <span className="mono mainline-dim-score">
                            {formatScore(dim.score)}
                          </span>
                          <span className="muted-text mono">
                            权重 {formatScore(dim.weight, 1)}
                          </span>
                          <span className="muted-text mono">
                            贡献 {formatScore(dim.contribution)}
                          </span>
                          <span className="muted-text mainline-dim-raw"
                                title={rawText(dim.raw)}>
                            {rawText(dim.raw) || "—"}
                          </span>
                          <span className="muted-text mainline-dim-note">
                            {dim.available
                              ? (dim.note || "")
                              : (dim.note || "该维度当日缺数据")}
                          </span>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              ))
            )}
          </section>

          {/* ⑤ 龙头股 */}
          <section className="mainline-card">
            <div className="mainline-card-head">
              <h4>龙头股（龙头集中度维度所用）</h4>
              <span className="muted-text">
                {score?.resonance
                  ? `共振成立，比例 ${score.resonance_ratio === null
                    || score.resonance_ratio === undefined ? "—"
                    : `${(score.resonance_ratio * 100).toFixed(0)}%`}`
                  : "未触发共振"}
              </span>
            </div>
            {leaders.length === 0 ? (
              <div className="mainline-empty">无龙头股数据（成分股资金流缺失时为空）</div>
            ) : (
              <div className="mainline-table-scroll">
                <table className="mainline-table">
                  <thead>
                    <tr>
                      <th>代码</th><th>名称</th><th className="num">命中数</th>
                      <th className="num">资金排名</th><th className="num">动量排名</th>
                      <th className="num">量能排名</th><th className="num">占板块额比</th>
                      <th className="num">近5日净流入</th><th className="num">近10日涨幅</th>
                      {/* ⚠️ 「龙头」是**资金/动量口径**：identify_leaders() 只看
                          资金/动量/量能三维，业务相关性从不参与。加这一列是为了让
                          「资金龙头」与「业务龙头」分得开 —— 2026-09-22 用户看到
                          氟化工概念的龙头里有雅克科技（主营半导体材料）提出质疑。 */}
                      <th>业务相关性</th>
                      <th>来源</th>
                    </tr>
                  </thead>
                  <tbody>
                    {leaders.map((leader) => (
                      <tr key={leader.code}>
                        <td className="mono">{leader.code}</td>
                        <td>{leader.name || "—"}</td>
                        <td className="num">{leader.hits ?? "—"}</td>
                        <td className="num">{leader.capital_rank ?? "—"}</td>
                        <td className="num">{leader.momentum_rank ?? "—"}</td>
                        <td className="num">{leader.volume_rank ?? "—"}</td>
                        <td className="num">
                          {leader.amount_ratio === null
                            || leader.amount_ratio === undefined
                            ? "—" : `${(leader.amount_ratio * 100).toFixed(2)}%`}
                        </td>
                        <td className={"num " + toneOf(leader.net_5d)}>
                          {formatYi(leader.net_5d)}
                        </td>
                        <td className={"num " + toneOf(leader.ret_10d)}>
                          {formatPct(leader.ret_10d)}
                        </td>
                        <td
                          className={
                            "muted-text " +
                            // ⚠️ 警示的判据是「业务口径是否被绕过」，不是 `source`：
                            // `source` 回答"业务分从哪来"，而 002140 东华科技这类
                            // 是 `source='corr'` **且带一个 65 分的业务分** ——
                            // LLM 明明判过、给了不及格的分数，票却按股价相关性
                            // 进了池（2026-09-23 用户报障）。只用 `source` 判断
                            // 会把它当成"未判过"，与数据相反。
                            (leader.business_warn
                              || leader.business_source === "corr"
                              ? "mainline-leader-weak-biz"
                              : "")
                          }
                          title={leader.business_reason || ""}
                        >
                          {leader.business_label || "—"}
                        </td>
                        <td className="muted-text">{leader.source || "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          {/* ⑥ 跟踪信息（资金累计 + 共振） */}
          {tracked && (
            <section className="mainline-card">
              <div className="mainline-card-head">
                <h4>重点跟踪数据</h4>
                <span className="muted-text">
                  档位 {tracked.profile || "—"} · 衰减周期
                  {" "}{tracked.decay_days === null
                    || tracked.decay_days === undefined
                    ? "—" : `${tracked.decay_days} 日`}
                </span>
              </div>
              <div className="mainline-tracked-metrics">
                <span>总分 <b>{formatScore(tracked.total)}</b></span>
                <span>六维 <b>{formatScore(tracked.six_dim)}</b></span>
                <span>建仓 <b>{formatScore(tracked.accumulation)}</b></span>
                <span>门控 <b>{formatScore(tracked.gate_bonus)}</b></span>
                <span>近5日净流入
                  <b className={toneOf(tracked.flow_5d)}>
                    {formatYi(tracked.flow_5d)}</b></span>
                <span>近20日净流入
                  <b className={toneOf(tracked.flow_20d)}>
                    {formatYi(tracked.flow_20d)}</b></span>
                <span>共振比例
                  <b>{tracked.resonance_ratio === null
                    || tracked.resonance_ratio === undefined ? "—"
                    : `${(tracked.resonance_ratio * 100).toFixed(0)}%`}</b></span>
              </div>
              {tracked.seat_note && (
                <div className="mainline-seat-note">席位备注：{tracked.seat_note}</div>
              )}
            </section>
          )}
        </div>
      </div>
    </div>
  );
}

/**
 * 评分走势折线（手写 SVG）。
 *
 * Y 轴**不固定 0~100**：一个常年 40~60 分的板块，固定 0~100 会把所有波动压成一条
 * 直线，看不出任何变化。所以取"实际区间上下各留 8% 余量"并设 10 分最小跨度，
 * 再在工具提示里给出准确数值。刻度线按数据区间自动分 4 档。
 */
function MainlineTrendChart({ points }: { points: MainlineHistoryPoint[] }) {
  const plotW = TREND.w - TREND.left - TREND.right;
  const plotH = TREND.h - TREND.top - TREND.bottom;

  const values = points
    .map((point) => point.total)
    .filter((value): value is number => value !== null && Number.isFinite(value));
  const rawMin = values.length > 0 ? Math.min(...values) : 0;
  const rawMax = values.length > 0 ? Math.max(...values) : 100;
  const span = Math.max(10, rawMax - rawMin);
  const min = Math.max(0, rawMin - span * 0.08);
  const max = Math.min(100, rawMax + span * 0.08);
  const range = max - min < 1e-6 ? 1 : max - min;

  const x = (index: number) =>
    TREND.left + (plotW * index) / Math.max(1, points.length - 1);
  const y = (value: number) =>
    TREND.top + plotH * (1 - (value - min) / range);

  // 缺失点直接断开（连线会把"没有数据"画成"平稳过渡"，是误导）
  const segments: string[] = [];
  let current: string[] = [];
  points.forEach((point, index) => {
    if (point.total === null || !Number.isFinite(point.total)) {
      if (current.length > 1) segments.push(current.join(" "));
      current = [];
      return;
    }
    current.push(`${x(index)},${y(point.total)}`);
  });
  if (current.length > 1) segments.push(current.join(" "));

  const ticks = [0, 1, 2, 3, 4].map((step) => min + (range * step) / 4);
  // X 轴只标首/中/末三个日期：60 个刻度会糊成一团
  const labelIndexes = [0, Math.floor((points.length - 1) / 2), points.length - 1];

  return (
    <div className="mainline-trend-wrap">
      <svg className="mainline-trend-svg"
           viewBox={`0 0 ${TREND.w} ${TREND.h}`}
           role="img" aria-label="评分走势">
        {ticks.map((value) => (
          <g key={`ty-${value.toFixed(2)}`}>
            <line x1={TREND.left} x2={TREND.w - TREND.right}
                  y1={y(value)} y2={y(value)}
                  stroke="var(--border)" strokeDasharray="3 4" opacity={0.55} />
            <text x={TREND.left - 6} y={y(value) + 3} fontSize="10"
                  fill="var(--muted)" textAnchor="end">{value.toFixed(0)}</text>
          </g>
        ))}
        {segments.map((segment, index) => (
          <polyline key={`seg-${index}`} points={segment} fill="none"
                    stroke="var(--accent)" strokeWidth="1.6"
                    strokeLinejoin="round" />
        ))}
        {/* 六维 & 建仓分做参考虚线：总分涨是六维还是资金推动的，一眼能分清 */}
        {([
          { key: "six_dim", color: "var(--medium)" },
          { key: "accumulation", color: "var(--high)" },
        ] as const).map((item) => {
          let path = "";
          points.forEach((point, index) => {
            const value = point[item.key];
            if (value === null || !Number.isFinite(value)) return;
            path += `${path ? "L" : "M"}${x(index)},${y(value)}`;
          });
          return path ? (
            <path key={item.key} d={path} fill="none" stroke={item.color}
                  strokeWidth="1" strokeDasharray="4 3" opacity={0.75} />
          ) : null;
        })}
        {points.map((point, index) => {
          if (point.total === null || !Number.isFinite(point.total)) return null;
          return (
            <circle key={`tp-${point.trade_date}-${index}`}
                    cx={x(index)} cy={y(point.total)} r={2}
                    fill="var(--accent)">
              <title>{`${formatDate(point.trade_date)}`
                + `\n总分 ${formatScore(point.total)}`
                + `\n六维 ${formatScore(point.six_dim)}`
                + `\n建仓 ${formatScore(point.accumulation)}`
                + `${point.level && point.level !== "none"
                  ? `\n等级 ${point.level}` : ""}`}</title>
            </circle>
          );
        })}
        {labelIndexes.map((index) => (
          <text key={`tx-${index}`} x={x(index)} y={TREND.h - 12}
                fontSize="10" fill="var(--muted)"
                textAnchor={index === 0 ? "start"
                  : index === points.length - 1 ? "end" : "middle"}>
            {formatDate(points[index]?.trade_date).slice(5)}
          </text>
        ))}
      </svg>
      <div className="mainline-legend muted-text">
        <span><i className="mainline-legend-line accent" />总分</span>
        <span><i className="mainline-legend-line medium" />六维基座</span>
        <span><i className="mainline-legend-line high" />建仓资金</span>
        <span>Y 轴按实际区间自适应（不是固定 0~100），鼠标悬停圆点看当日明细</span>
      </div>
    </div>
  );
}
