import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  IntradayBoardSeries, IntradayLevelPoint, IntradayLevels, IntradayMarker,
  IntradayQuote, IntradayTrendPoint,
} from "../api";

/**
 * 分时图与提示点（坐标轴策略参考同花顺 / 东方财富）。
 *
 * 坐标轴设计要点（这是本次重写的核心）：
 *  1. **Y轴只按分时数据定标**：以昨收为中心、按可见区间内 |最高-昨收| 与
 *     |最低-昨收| 的较大者对称取范围（×1.08留白）。
 *     绝不能把关键价位线（箱体上沿/下沿）纳入 min/max ——
 *     实测箱体 804~1009 而当日只在 858~884 波动时，Y轴被拉到205点宽，
 *     价格线被压成一条几乎水平的线，完全看不清。
 *  2. **视野外的价位线不入图**：档位在可见范围外的，改在右侧「档位」清单里
 *     标注（含距现价百分比），既不糊图也不丢信息。
 *  3. **双刻度**：左侧价格、右侧涨跌幅（同花顺/东财都是双轴，方便直接读强弱）。
 *  4. **动态刻度**：按 1/2/2.5/5×10^k 取"整齐"步长，避免出现 861.8342 这类标签。
 *  5. **交互式缩放/平移**：滚轮以光标为中心缩放时间轴、拖拽平移、双击复位；
 *     Y轴随可见时间窗**自动重算**（放大到某一段即自动放大该段的波动，
 *     与同花顺放大分时的行为一致）。
 */

const W = 1000;
const H = 380;
const PAD = { top: 18, right: 88, bottom: 46, left: 62 };
const MINUTES_TOTAL = 240; // 上午120 + 下午120
const MIN_SPAN_MINUTES = 10;
const HEIGHT = H - PAD.top - PAD.bottom;
// 分时波动的自然量程最多允许被"可执行档位"拉伸的倍数（防止箱体远档位压扁价格线）
const MAX_LEVEL_STRETCH = 1.6;

/** "YYYY-MM-DD HH:MM" → 当日交易分钟序号（0~240），午休折叠。 */
function sessionMinute(ts: string): number | null {
  const match = /(\d{2}):(\d{2})/.exec(ts.slice(10));
  if (!match) return null;
  const minutes = Number(match[1]) * 60 + Number(match[2]);
  const morningOpen = 9 * 60 + 30, morningClose = 11 * 60 + 30;
  const afternoonOpen = 13 * 60, afternoonClose = 15 * 60;
  if (minutes < morningOpen) return 0;
  if (minutes <= morningClose) return minutes - morningOpen;
  if (minutes < afternoonOpen) return MINUTES_TOTAL / 2;
  if (minutes <= afternoonClose) {
    return MINUTES_TOTAL / 2 + (minutes - afternoonOpen);
  }
  return MINUTES_TOTAL;
}

function timeLabel(minute: number): string {
  const morning = minute <= MINUTES_TOTAL / 2;
  const offset = morning ? minute : minute - MINUTES_TOTAL / 2;
  const base = morning ? 9 * 60 + 30 : 13 * 60;
  const total = base + offset;
  return `${String(Math.floor(total / 60)).padStart(2, "0")}:${String(Math.round(total % 60)).padStart(2, "0")}`;
}

/** 取"整齐"刻度步长：1/2/2.5/5 × 10^k（保证网格线落在人眼舒服的数值上）。 */
function niceStep(span: number, target = 5): number {
  if (!Number.isFinite(span) || span <= 0) return 1;
  const rough = span / target;
  const magnitude = Math.pow(10, Math.floor(Math.log10(rough)));
  const normalized = rough / magnitude;
  const step = normalized <= 1 ? 1
    : normalized <= 2 ? 2
    : normalized <= 2.5 ? 2.5
    : normalized <= 5 ? 5
    : 10;
  return step * magnitude;
}

function boardEquivPrice(
  series: IntradayBoardSeries, prevClose: number,
): { ts: string; price: number }[] {
  const points = series.points.filter((p) => p.price > 0);
  if (points.length < 2 || !prevClose) return [];
  const base = points[0].price;
  return points.map((p) => ({ ts: p.ts, price: prevClose * (p.price / base) }));
}

type Hover = { minute: number; price: number; ts: string; avg: number | null } | null;

function IntradayChart({
  quote, trend, levels, markers, boards, boardSeries, levelSeries,
}: {
  quote: IntradayQuote | null;
  trend: IntradayTrendPoint[];
  levels: IntradayLevels | null;
  markers: IntradayMarker[];
  boards: { name: string; change_pct: number | null }[];
  boardSeries: IntradayBoardSeries[];
  levelSeries?: IntradayLevelPoint[];
}) {
  const [hover, setHover] = useState<Hover>(null);
  // 时间轴可视窗口（当日交易分钟）
  const [view, setView] = useState<{ start: number; end: number }>(
    { start: 0, end: MINUTES_TOTAL });
  // 是否把箱体等远档位也纳入Y轴（默认否，避免压扁价格线）
  const [fitLevels, setFitLevels] = useState(false);
  const svgRef = useRef<SVGSVGElement | null>(null);
  const dragRef = useRef<{ minute: number; start: number; end: number } | null>(null);

  const prevClose = quote?.prev_close ?? null;
  const spanMinutes = view.end - view.start;

  const series = useMemo(() => {
    const points = trend
      .map((p) => ({ ...p, minute: sessionMinute(p.ts) }))
      .filter((p): p is IntradayTrendPoint & { minute: number } => p.minute !== null)
      .sort((a, b) => a.minute - b.minute);
    const boardLines = boardSeries
      .filter((s) => s.available && s.points.length > 1)
      .map((s) => ({
        name: s.name,
        source: s.source_name,
        points: boardEquivPrice(s, prevClose ?? 0)
          .map((p) => ({ ...p, minute: sessionMinute(p.ts) }))
          .filter((p): p is { ts: string; price: number; minute: number } =>
            p.minute !== null),
      }))
      .filter((s) => s.points.length > 1);
    return { points, boardLines };
  }, [trend, boardSeries, prevClose]);

  // 可见窗口内的数据点（Y轴与成交量都只按可见区间定标）
  const visible = useMemo(
    () => series.points.filter(
      (p) => p.minute >= view.start && p.minute <= view.end),
    [series.points, view]);

  const domain = useMemo(() => {
    if (!visible.length) return null;
    const prices: number[] = [];
    visible.forEach((p) => {
      prices.push(p.price);
      if (p.avg_price) prices.push(p.avg_price);
    });
    series.boardLines.forEach((board) => board.points.forEach((p) => {
      if (p.minute >= view.start && p.minute <= view.end) prices.push(p.price);
    }));
    let min = Math.min(...prices);
    let max = Math.max(...prices);
    if (fitLevels && levels) {
      // 可选：把档位线一并纳入（用户明确要看箱体全貌时才用）
      [levels.high_sell, levels.low_buy, levels.stop_loss,
       levels.box_high, levels.box_low, levels.boll_upper, levels.boll_lower]
        .forEach((value) => {
          if (typeof value === "number" && Number.isFinite(value)) {
            min = Math.min(min, value);
            max = Math.max(max, value);
          }
        });
    }
    const span = max - min;
    // 以昨收为中心对称取范围（同花顺/东财分时口径）：只算分时数据的偏离幅度
    if (prevClose && !fitLevels) {
      let reach = Math.max(
        Math.abs(prevClose - min), Math.abs(max - prevClose),
        prevClose * 0.002, // 极窄幅时给个下限，避免"一条直线"没有刻度
      );
      // 三个**可执行**档位（高抛/低吸/止损）若离得不远，就把它们纳进可见范围：
      // 止损线是最该看见的一条线，不能因为"按分时波动定标"而被裁掉。
      // 但只允许拉伸有限倍数（默认1.6×），否则箱体那种远档位又会把图压扁。
      if (levels) {
        [levels.high_sell, levels.low_buy, levels.stop_loss].forEach((value) => {
          if (typeof value !== "number" || !Number.isFinite(value)) return;
          const distance = Math.abs(value - prevClose);
          if (distance > reach && distance <= reach * MAX_LEVEL_STRETCH) {
            reach = distance;
          }
        });
      }
      reach *= 1.08;
      return { min: prevClose - reach, max: prevClose + reach,
               span: reach * 2, centered: true as const };
    }
    const pad = (span || (prevClose ?? max) * 0.01) * 0.12;
    return { min: min - pad, max: max + pad, span: (max - min) + pad * 2,
             centered: false as const };
  }, [visible, series.boardLines, view, prevClose, levels, fitLevels]);

  const x = useCallback((minute: number) =>
    PAD.left + ((minute - view.start) / spanMinutes) * (W - PAD.left - PAD.right),
    [view.start, spanMinutes]);
  const y = useCallback((price: number) =>
    domain
      ? H - PAD.bottom - ((price - domain.min) / (domain.span || 1)) * HEIGHT
      : H / 2,
    [domain]);

  const path = (points: { minute: number; price: number }[]) =>
    points
      .filter((p) => p.minute >= view.start - 1 && p.minute <= view.end + 1)
      .map((p, i) => `${i === 0 ? "M" : "L"}${x(p.minute).toFixed(1)},${y(p.price).toFixed(1)}`)
      .join(" ");

  const averagePath = (() => {
    const pts = visible.filter((p) => p.avg_price);
    return pts.length > 1
      ? path(pts.map((p) => ({ minute: p.minute, price: p.avg_price as number })))
      : "";
  })();

  const volumeMax = Math.max(1, ...visible.map((p) => p.volume || 0));

  // 档位线：只画落在可见Y范围内的；范围外的转到右侧清单提示
  // （API 里这些字段可空，这里统一收窄成 number，避免后续到处判空）
  type LevelLine = { key: string; price: number; label: string; cls: string };
  const allLevels: LevelLine[] = [];
  if (levels) {
    const push = (key: string, value: number | null | undefined,
                  label: string, cls: string) => {
      if (typeof value === "number" && Number.isFinite(value)) {
        allLevels.push({ key, price: value, label, cls });
      }
    };
    push("high_sell", levels.high_sell, "高抛", "lv-sell");
    push("low_buy", levels.low_buy, "低吸", "lv-buy");
    push("stop_loss", levels.stop_loss, "止损", "lv-stop");
    push("box_high", levels.box_high, "箱体上沿", "lv-box");
    push("box_low", levels.box_low, "箱体下沿", "lv-box");
    push("boll_upper", levels.boll_upper, "布林上轨", "lv-boll");
    push("boll_lower", levels.boll_lower, "布林下轨", "lv-boll");
  }
  const inViewLevels = domain
    ? allLevels.filter((l) => l.price >= domain.min && l.price <= domain.max)
    : [];
  const outViewLevels = domain
    ? allLevels.filter((l) => l.price < domain.min || l.price > domain.max)
    : [];
  const lastPrice = visible.length ? visible[visible.length - 1].price
    : (quote?.price ?? null);

  /**
   * 逐bar档位曲线：把 low_buy / high_sell / stop_loss 画成随时间变化的细线。
   * 数据是 5 分钟粒度、分时点是 1 分钟粒度，两边用同一个 `sessionMinute(ts)` 映射。
   */
  const levelDrift = (() => {
    if (!levelSeries || levelSeries.length < 2) return null;
    const build = (pick: (row: IntradayLevelPoint) => number) => {
      const pts = levelSeries
        .map((row) => ({ minute: sessionMinute(row.ts), price: pick(row) }))
        .filter((p): p is { minute: number; price: number } => p.minute !== null);
      return pts.length > 1 ? path(pts) : "";
    };
    const high = build((row) => row.high_sell);
    const low = build((row) => row.low_buy);
    const stop = build((row) => row.stop_loss);
    return high && low && stop ? { high, low, stop } : null;
  })();

  // 刻度：按整齐步长铺满可见范围
  const yTicks = useMemo(() => {
    if (!domain) return [];
    const step = niceStep(domain.span, 4);
    const ticks: number[] = [];
    const start = Math.ceil(domain.min / step) * step;
    for (let value = start; value <= domain.max + 1e-9; value += step) {
      ticks.push(Number(value.toFixed(6)));
      if (ticks.length > 12) break;
    }
    return ticks;
  }, [domain]);

  const xTicks = useMemo(() => {
    const step = spanMinutes <= 30 ? 5
      : spanMinutes <= 60 ? 10
      : spanMinutes <= 120 ? 20
      : 30;
    const ticks: number[] = [];
    for (let minute = Math.ceil(view.start / step) * step;
         minute <= view.end; minute += step) {
      ticks.push(minute);
    }
    return ticks;
  }, [view, spanMinutes]);

  // ---- 缩放/平移 ----
  const zoomAt = useCallback((factor: number, anchor: number) => {
    setView((current) => {
      const span = current.end - current.start;
      const next = Math.max(MIN_SPAN_MINUTES,
                            Math.min(MINUTES_TOTAL, span * factor));
      const ratio = span > 0 ? (anchor - current.start) / span : 0.5;
      let start = anchor - next * ratio;
      let end = start + next;
      if (start < 0) { start = 0; end = next; }
      if (end > MINUTES_TOTAL) { end = MINUTES_TOTAL; start = end - next; }
      return { start: Math.max(0, start), end: Math.min(MINUTES_TOTAL, end) };
    });
  }, []);

  const reset = useCallback(
    () => setView({ start: 0, end: MINUTES_TOTAL }), []);

  // 滚轮缩放：需要非 passive 监听才能 preventDefault（否则页面会跟着滚动）
  useEffect(() => {
    const node = svgRef.current;
    if (!node) return;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      const rect = node.getBoundingClientRect();
      const ratio = Math.min(
        1, Math.max(0, (event.clientX - rect.left) / rect.width));
      const plotRatio = Math.min(1, Math.max(0,
        (ratio * W - PAD.left) / (W - PAD.left - PAD.right)));
      const anchor = view.start + plotRatio * spanMinutes;
      zoomAt(event.deltaY > 0 ? 1.18 : 0.85, anchor);
    };
    node.addEventListener("wheel", onWheel, { passive: false });
    return () => node.removeEventListener("wheel", onWheel);
  }, [view.start, spanMinutes, zoomAt]);

  const minuteAt = (clientX: number) => {
    const node = svgRef.current;
    if (!node) return null;
    const rect = node.getBoundingClientRect();
    const plotRatio = ((clientX - rect.left) / rect.width * W - PAD.left)
      / (W - PAD.left - PAD.right);
    return view.start + Math.min(1, Math.max(0, plotRatio)) * spanMinutes;
  };

  const handleMove = (event: React.MouseEvent<SVGSVGElement>) => {
    const drag = dragRef.current;
    if (drag && domain) {
      const minute = minuteAt(event.clientX);
      if (minute !== null) {
        const delta = drag.minute - minute;
        const span = drag.end - drag.start;
        let start = drag.start + delta;
        let end = drag.end + delta;
        if (start < 0) { start = 0; end = span; }
        if (end > MINUTES_TOTAL) { end = MINUTES_TOTAL; start = end - span; }
        setView({ start, end });
      }
      return;
    }
    if (!domain || !visible.length) return;
    const minute = minuteAt(event.clientX);
    if (minute === null) return;
    let nearest = visible[0];
    let best = Infinity;
    for (const point of visible) {
      const distance = Math.abs(point.minute - minute);
      if (distance < best) { best = distance; nearest = point; }
    }
    setHover({
      minute: nearest.minute, price: nearest.price, ts: nearest.ts,
      avg: nearest.avg_price,
    });
  };

  const handleDown = (event: React.MouseEvent<SVGSVGElement>) => {
    const minute = minuteAt(event.clientX);
    if (minute === null) return;
    dragRef.current = { minute, start: view.start, end: view.end };
  };
  const endDrag = () => { dragRef.current = null; };

  if (!domain || !visible.length) {
    return (
      <div className="empty-tip muted-text">
        分时数据不可用（数据源缺口，见下方数据健康度）
      </div>
    );
  }

  const pctOf = (price: number) =>
    prevClose ? ((price / prevClose) - 1) * 100 : null;
  const zoomed = spanMinutes < MINUTES_TOTAL - 0.5;

  return (
    <div className="intraday-chart-wrap">
      <div className="chart-toolbar">
        <span className="muted-text">
          滚轮缩放 · 拖拽平移 · 双击复位
        </span>
        <button className="btn-ghost chart-btn" onClick={() => zoomAt(0.7, (view.start + view.end) / 2)}
                title="放大时间轴（Y轴自动跟随）">＋</button>
        <button className="btn-ghost chart-btn" onClick={() => zoomAt(1.4, (view.start + view.end) / 2)}
                title="缩小时间轴">－</button>
        <button className="btn-ghost chart-btn" onClick={reset}
                disabled={!zoomed}>复位</button>
        <label className="muted-text chart-toggle" title="勾选后把箱体等远档位也纳入Y轴（默认只按分时波动定标，避免压扁价格线）">
          <input type="checkbox" checked={fitLevels}
                 onChange={(e) => setFitLevels(e.target.checked)} />
          纳入远档位定标
        </label>
        <span className="muted-text">
          可视 {timeLabel(view.start)}–{timeLabel(view.end)}
        </span>
      </div>

      <svg
        ref={svgRef}
        viewBox={`0 0 ${W} ${H}`}
        className="intraday-svg"
        role="img"
        onMouseMove={handleMove}
        onMouseDown={handleDown}
        onMouseUp={endDrag}
        onMouseLeave={() => { endDrag(); setHover(null); }}
        onDoubleClick={reset}
      >
        {/* 横向网格 + 左价格刻度 + 右涨跌幅刻度（双轴，同花顺/东财口径） */}
        {yTicks.map((price) => {
          const pct = pctOf(price);
          const isPrev = prevClose !== null
            && Math.abs(price - prevClose) < domain.span * 0.004;
          return (
            <g key={price}>
              <line
                x1={PAD.left} x2={W - PAD.right} y1={y(price)} y2={y(price)}
                stroke={isPrev ? "var(--muted)" : "var(--border)"}
                strokeDasharray={isPrev ? "5 5" : "3 4"}
                opacity={isPrev ? 0.9 : 0.7} />
              <text x={PAD.left - 6} y={y(price) + 4} fontSize="10"
                    fill="var(--muted)" textAnchor="end">
                {price.toFixed(2)}
              </text>
              {pct !== null && (
                <text x={W - PAD.right + 6} y={y(price) + 4} fontSize="10"
                      textAnchor="start"
                      fill={pct > 0.001 ? "var(--low)"
                        : pct < -0.001 ? "var(--high)" : "var(--muted)"}>
                  {pct >= 0 ? "+" : ""}{pct.toFixed(2)}%
                </text>
              )}
            </g>
          );
        })}
        {prevClose !== null && (
          <text x={PAD.left - 6} y={y(prevClose) + 4} fontSize="9"
                fill="var(--muted)" textAnchor="end">昨收</text>
        )}

        {/* 纵向网格 + 时间刻度 */}
        {xTicks.map((minute) => (
          <g key={minute}>
            <line x1={x(minute)} x2={x(minute)} y1={PAD.top} y2={H - PAD.bottom}
                  stroke="var(--border)" opacity={0.3} />
            <text x={x(minute)} y={H - PAD.bottom + 14} fontSize="10"
                  fill="var(--muted)" textAnchor="middle">
              {timeLabel(minute)}
            </text>
          </g>
        ))}

        {/* 成交量（底部小柱，只按可见区间定标） */}
        {visible.map((point, i) => {
          const barHeight = ((point.volume || 0) / volumeMax) * 36;
          return (
            <rect key={i} x={x(point.minute) - 1.2} width={2.4}
                  y={H - PAD.bottom - barHeight} height={barHeight}
                  fill="var(--border)" opacity={0.5} />
          );
        })}

        {/* 关联板块（换算为等价价位，虚线） */}
        {series.boardLines.map((board, index) => (
          <path key={board.name} d={path(board.points)} fill="none"
                stroke={index === 0 ? "var(--medium)" : "var(--accent)"}
                strokeWidth="1.3" strokeDasharray="6 4" opacity={0.75} />
        ))}

        {/* 关键价位线（仅画可见范围内的）。
            有逐bar档位序列时画**随时间漂移的曲线**，而不是一条横贯全天的直线 ——
            档位（低吸/高抛/止损）是每分钟随 VWAP/布林重算的时刻量：
            实测 300308 当日低吸线从 862 抬到 898、603083 从 215.42 抬到 221.91。
            画成横线会让人以为"开盘就在低吸线以下"（那只是当前值的错觉），
            也会让人把后来的高位止损误读成早盘就该止损。 */}
        {levelDrift && (
          <>
            <path d={levelDrift.high} fill="none" className="lv-sell"
                  strokeWidth="1.3" strokeDasharray="7 3" opacity={0.9} />
            <path d={levelDrift.low} fill="none" className="lv-buy"
                  strokeWidth="1.3" strokeDasharray="7 3" opacity={0.9} />
            <path d={levelDrift.stop} fill="none" className="lv-stop"
                  strokeWidth="1.3" strokeDasharray="4 3" opacity={0.9} />
          </>
        )}
        {!levelDrift && inViewLevels.map((line) => (
          <g key={line.key} className={line.cls}>
            <line x1={PAD.left} x2={W - PAD.right} y1={y(line.price)}
                  y2={y(line.price)} strokeWidth="1.4" strokeDasharray="7 3" />
            <text x={PAD.left + 4} y={y(line.price) - 4} fontSize="10"
                  className="lv-text">
              {line.label} {line.price.toFixed(2)}
            </text>
          </g>
        ))}
        {/* 档位曲线模式下，只在右端标当前值（曲线自然收束到那里） */}
        {levelDrift && inViewLevels.map((line) => (
          <g key={`tag-${line.key}`} className={line.cls}>
            <text x={W - PAD.right + 4} y={y(line.price) + 4} fontSize="10"
                  className="lv-text">
              {line.label} {line.price.toFixed(2)}
            </text>
          </g>
        ))}

        {/* 分时线 + 均价线 + 现价点 */}
        <path d={averagePath} fill="none" stroke="var(--medium)" strokeWidth="1.6" />
        <path d={path(visible)} fill="none" stroke="var(--accent)" strokeWidth="2" />
        {lastPrice !== null && (
          <>
            <line x1={PAD.left} x2={W - PAD.right} y1={y(lastPrice)} y2={y(lastPrice)}
                  stroke="var(--accent)" strokeWidth="1" strokeDasharray="2 3"
                  opacity={0.7} />
            <circle cx={x(visible[visible.length - 1].minute)} cy={y(lastPrice)}
                    r="3.2" fill="var(--accent)" />
          </>
        )}

        {/* 做T三角标记 */}
        {markers.map((marker, index) => {
          const minute = sessionMinute(marker.ts);
          if (minute === null || minute < view.start || minute > view.end) return null;
          const up = marker.kind === "low_buy";
          const cx = x(minute);
          const cy = up ? y(marker.price) + 14 : y(marker.price) - 14;
          const color = marker.kind === "stop_loss"
            ? "var(--low)"
            : up ? "var(--high)" : "var(--low)";
          const solid = marker.strength !== "hollow";
          const size = solid ? 8 : 6;
          const points = up
            ? `${cx},${cy - size} ${cx - size},${cy + size} ${cx + size},${cy + size}`
            : `${cx},${cy + size} ${cx - size},${cy - size} ${cx + size},${cy - size}`;
          return (
            <g key={index}>
              <polygon points={points} fill={solid ? color : "none"}
                       stroke={color} strokeWidth={solid ? 1 : 1.8} />
              <title>{`${marker.ts.slice(11)} ${marker.label} @ ${marker.price}`}</title>
            </g>
          );
        })}

        {/* 悬浮十字光标 */}
        {hover && (
          <g>
            <line x1={x(hover.minute)} x2={x(hover.minute)} y1={PAD.top}
                  y2={H - PAD.bottom} stroke="var(--muted)" strokeDasharray="2 3" />
            <circle cx={x(hover.minute)} cy={y(hover.price)} r="3.5"
                    fill="var(--accent)" />
          </g>
        )}
      </svg>

      <div className="chart-readout">
        {hover ? (
          <>
            <span className="mono">{hover.ts.slice(5)}</span>
            <span>价 <b className="mono">{hover.price.toFixed(2)}</b></span>
            {prevClose && (
              <span>涨跌
                <b className="mono" style={{
                  color: hover.price >= prevClose ? "var(--low)" : "var(--high)",
                }}>
                  {" "}{(((hover.price / prevClose) - 1) * 100).toFixed(2)}%
                </b>
              </span>
            )}
            <span>均价 <b className="mono">{(hover.avg ?? 0).toFixed(2)}</b></span>
            {hover.avg && (
              <span className="muted-text">
                偏离 {(((hover.price / hover.avg) - 1) * 100).toFixed(2)}%
              </span>
            )}
          </>
        ) : (
          <>
            <span className="legend legend-price">— 分时</span>
            <span className="legend legend-avg">— 均价(VWAP)</span>
            {series.boardLines.map((board, index) => (
              <span key={board.name} className="legend"
                    style={{ color: index === 0 ? "var(--medium)" : "var(--accent)" }}>
                -- {board.name}
              </span>
            ))}
            <span className="legend legend-solid">▲ 实心=正式信号</span>
            <span className="legend legend-hollow">△ 空心=软提示</span>
          </>
        )}
      </div>

      {/* 档位清单：范围外的档位在这里显示，避免为看档位而把Y轴拉大 */}
      <div className="level-strip">
        {allLevels.map((line) => {
          const outside = line.price < domain.min || line.price > domain.max;
          const pct = lastPrice ? ((line.price / lastPrice) - 1) * 100 : null;
          return (
            <span key={line.key}
                  className={`level-chip ${line.cls}${outside ? " outside" : ""}`}
                  title={outside ? "该档位在当前Y轴范围外（避免压扁价格线，故不入图）"
                    : "已在图中标注"}>
              {line.label} <b className="mono">{line.price.toFixed(2)}</b>
              {pct !== null && (
                <em className="muted-text">
                  {pct >= 0 ? "+" : ""}{pct.toFixed(2)}%
                </em>
              )}
              {outside && <span className="muted-text">（图外）</span>}
            </span>
          );
        })}
        {outViewLevels.length > 0 && (
          <label className="muted-text chart-toggle">
            <input type="checkbox" checked={fitLevels}
                   onChange={(e) => setFitLevels(e.target.checked)} />
            把图外档位也纳入Y轴
          </label>
        )}
      </div>

      <div className="chart-foot muted-text">
        {boards.length > 0 && (
          <span>
            板块：
            {boards.map((b) => (
              <span key={b.name} style={{ marginRight: 10 }}>
                {b.name}
                <b className="mono" style={{
                  color: (b.change_pct ?? 0) >= 0 ? "var(--low)" : "var(--high)",
                }}>
                  {" "}{(b.change_pct ?? 0).toFixed(2)}%
                </b>
              </span>
            ))}
          </span>
        )}
        {series.boardLines.some((b) => b.source.includes("等权")) && (
          <span>｜板块线为「自选同业等权合成」口径（官方板块分时接口不可达）</span>
        )}
      </div>
    </div>
  );
}

// 自选列表每 5 秒更新一次（报价快车道），不该带着整张分时图一起重渲染 ——
// 必须 memo：图表有数百个 SVG 节点，5 秒一次会持续卡顿。
export default memo(IntradayChart);
