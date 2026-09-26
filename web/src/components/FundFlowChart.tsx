import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { FlowEntity } from "../api";

/**
 * 资金流走势图（多实体叠加 + 可放大查看）。
 *
 * 设计要点：
 *  1. **一张图叠多条线**（用户要求"金额数据可以同时显示多个板块或个股的走势线"），
 *     每条线一个开关；线名与颜色在下方图例里，点一下即隐藏/显示。
 *  2. **放大查看**：滚轮以光标为中心缩放时间轴、拖拽平移、双击复位，
 *     另有 5/10/20 日快捷窗口。Y 轴随可见窗口**自动重算**（放大后能看到小波段，
 *     否则几条线挤在一起看不出差别）。
 *  3. **正负分色**：单位统一为"亿元"，零轴画实线；净流入为正、净流出为负，
 *     悬停时逐条给出当日数值（做资金流监控，正负比大小更重要）。
 *  4. **数据不可得的实体不进图**：只在图例里标注缺口，绝不画一条 0 值的假线
 *     （那会让人以为"这个板块资金流是 0"）。
 */

const W = 960;
const H = 320;
const PAD = { top: 16, right: 84, bottom: 34, left: 66 };

/** 线与图例配色（深色主题下区分度经过挑选）。 */
const COLORS = [
  "#4a9eff", "#2ea86e", "#e0b020", "#d95c4a", "#9b6dff",
  "#20b8c4", "#f07ab0", "#8bc34a", "#ff8c42", "#6c8cff",
  "#c9a227", "#5ac8fa",
];

type Hover = { index: number } | null;

function niceStep(span: number, target = 4): number {
  if (!Number.isFinite(span) || span <= 0) return 1;
  const rough = span / target;
  const magnitude = Math.pow(10, Math.floor(Math.log10(rough)));
  const normalized = rough / magnitude;
  const step = normalized <= 1 ? 1 : normalized <= 2 ? 2
    : normalized <= 2.5 ? 2.5 : normalized <= 5 ? 5 : 10;
  return step * magnitude;
}

/** 元 → 亿元（图表统一用亿元，太长的数字读不出来）。 */
function toYi(value: number | null | undefined): number | null {
  if (value === null || value === undefined || !Number.isFinite(value)) return null;
  return value / 1e8;
}

function signedYi(value: number): string {
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}亿`;
}

export default function FlowChart({
  entities, hidden, height,
}: {
  entities: FlowEntity[];
  /** 被图例关掉的 code 集合。 */
  hidden: Set<string>;
  height?: number;
}) {
  const boxHeight = height ?? H;
  const [range, setRange] = useState<{ start: number; end: number } | null>(null);
  const [hover, setHover] = useState<Hover>(null);
  const svgRef = useRef<SVGSVGElement | null>(null);
  const dragRef = useRef<{ x: number; start: number; end: number } | null>(null);

  // 所有日期（并集，升序）：不同数据源的交易日可能不完全一致
  const dates = useMemo(() => {
    const all = new Set<string>();
    entities.forEach((entity) => entity.series.forEach((point) => all.add(point.date)));
    return [...all].sort();
  }, [entities]);

  const visible = useMemo(() => {
    const start = range?.start ?? 0;
    const end = range?.end ?? Math.max(0, dates.length - 1);
    return { start: Math.max(0, start), end: Math.min(dates.length - 1, end) };
  }, [range, dates.length]);

  // 切换实体集合时复位视窗（否则旧的索引会指到新数据的别处）
  useEffect(() => { setRange(null); setHover(null); }, [entities.length]);

  const lines = useMemo(() => entities
    .filter((entity) => !hidden.has(entity.code))
    .map((entity, index) => {
      const color = COLORS[entities.findIndex((item) => item.code === entity.code)
        % COLORS.length];
      const points = entity.series
        .map((point) => ({ date: point.date, value: toYi(point.net) }))
        .filter((point): point is { date: string; value: number } =>
          point.value !== null);
      return { entity, color, points, index };
    }), [entities, hidden]);

  const windowDates = dates.slice(visible.start, visible.end + 1);

  /**
   * K 线数据：**只有个股**有（板块没有 OHLC）。
   *
   * 图表布局（用户要求"上下都有，时间轴对齐，纵轴各自拆开"）：
   *   ┌─ 资金流（亿元，零轴分正负）      ← 主图
   *   ├─ K 线（价格，独立纵轴）          ← 只在勾选的实体里有 OHLC 时出现
   *   └─ 成交量（股/手，独立纵轴）
   * 三块共用同一个 x 轴（同一批 `dates` 索引），因此放大/平移完全同步，
   * 纵向各自定标 —— 资金流(亿元) 与 价格(元)、量(股) 量纲差几个数量级，
   * 塞进同一根纵轴只会互相压扁（这也是用户说的"归一化或上下两幅图"里的后者）。
   */
  /** 价格折线的配色：按实体顺序取用。**不复用涨跌色**（红涨绿跌）——
 *  折线表达的是"这条线的身份"，不是"今天涨还是跌"；用涨跌色会让
 *  同一条线在不同日子换颜色，反而看不出是哪只标的。 */
const PRICE_COLORS = ["#4a9eff", "#d9a13b", "#2ea86e", "#d95c4a",
                      "#a06cd5", "#22b8cf"];

const priceLines = useMemo(() => entities
    .filter((entity) => !hidden.has(entity.code))
    .filter((entity) => entity.series.some((point) => point.close != null))
    .map((entity) => {
      const color = COLORS[entities.findIndex((item) => item.code === entity.code)
        % COLORS.length];
      const points = entity.series
        .filter((point) => point.close != null)
        .map((point) => ({
          date: point.date,
          open: point.open ?? point.close ?? 0,
          high: point.high ?? point.close ?? 0,
          low: point.low ?? point.close ?? 0,
          close: point.close as number,
          volume: point.volume ?? null,
        }));
      return { entity, color, points };
    }), [entities, hidden]);

  const hasKline = priceLines.length > 0 && priceLines.some((l) => l.points.length > 0);
  const chartHeight = hasKline ? Math.round(boxHeight * 0.5) : boxHeight;
  const klineHeight = hasKline ? Math.round(boxHeight * 0.3) : 0;
  const volumeHeight = hasKline ? Math.round(boxHeight * 0.2) : 0;

  const domain = useMemo(() => {
    const values: number[] = [0];
    lines.forEach((line) => line.points.forEach((point) => {
      const index = dates.indexOf(point.date);
      if (index >= visible.start && index <= visible.end) values.push(point.value);
    }));
    if (!values.length) return null;
    const min = Math.min(...values);
    const max = Math.max(...values);
    const span = max - min || Math.max(1, Math.abs(max));
    const pad = span * 0.12;
    return { min: min - pad, max: max + pad, span: (max - min) + pad * 2 };
  }, [lines, dates, visible]);

  /** K 线纵轴（可见区间内的最低价/最高价，独立于资金流）。 */
  const klineDomain = useMemo(() => {
    const lows: number[] = [];
    const highs: number[] = [];
    priceLines.forEach((line) => line.points.forEach((point) => {
      const index = dates.indexOf(point.date);
      if (index < visible.start || index > visible.end) return;
      lows.push(point.low);
      highs.push(point.high);
    }));
    if (!lows.length) return null;
    const min = Math.min(...lows);
    const max = Math.max(...highs);
    const span = max - min || Math.max(0.01, Math.abs(max) * 0.01);
    const pad = span * 0.08;
    return { min: min - pad, max: max + pad, span: (max - min) + pad * 2 };
  }, [priceLines, dates, visible]);

  /** 成交量纵轴（线性，0 起）。 */
  const volumeDomain = useMemo(() => {
    let peak = 0;
    priceLines.forEach((line) => line.points.forEach((point) => {
      const index = dates.indexOf(point.date);
      if (index < visible.start || index > visible.end) return;
      if (point.volume && point.volume > peak) peak = point.volume;
    }));
    return peak > 0 ? { max: peak * 1.1 } : null;
  }, [priceLines, dates, visible]);

  const x = useCallback((index: number) =>
    PAD.left + ((index - visible.start) / Math.max(1, visible.end - visible.start))
    * (W - PAD.left - PAD.right), [visible]);
  const y = useCallback((value: number) =>
    domain
      ? PAD.top + (1 - (value - domain.min) / (domain.span || 1))
        * (chartHeight - PAD.top - PAD.bottom)
      : chartHeight / 2, [domain, chartHeight]);

  // K 线区与量区的纵向映射（各自独立定标；x 轴与主图共用）
  const klineTop = chartHeight + 6;
  const volumeTop = klineTop + klineHeight + 6;
  const yPrice = useCallback((value: number) =>
    klineDomain
      ? klineTop + (1 - (value - klineDomain.min) / (klineDomain.span || 1))
        * Math.max(1, klineHeight - 18)
      : klineTop, [klineDomain, klineTop, klineHeight]);
  const yVolume = useCallback((value: number) =>
    volumeDomain
      ? volumeTop + (1 - value / (volumeDomain.max || 1)) * Math.max(1, volumeHeight - 14)
      : volumeTop, [volumeDomain, volumeTop, volumeHeight]);

  /** 成交量单位：股 → 万手（1 手 = 100 股），表格太小读不出原值。 */
  const toWanShou = (volume: number) => volume / 100 / 10000;

  const label = (date: string) => (date.length === 8
    ? `${date.slice(4, 6)}-${date.slice(6, 8)}`
    : date.slice(5));

  const zoomAt = useCallback((factor: number, anchor: number) => {
    setRange((current) => {
      const total = dates.length;
      const now = current ?? { start: 0, end: total - 1 };
      const span = now.end - now.start + 1;
      const next = Math.max(3, Math.min(total, span * factor));
      const ratio = span > 0 ? (anchor - now.start) / span : 0.5;
      let start = Math.round(anchor - next * ratio);
      let end = start + next - 1;
      if (start < 0) { start = 0; end = next - 1; }
      if (end > total - 1) { end = total - 1; start = end - next + 1; }
      return { start: Math.max(0, start), end: Math.min(total - 1, end) };
    });
  }, [dates.length]);

  useEffect(() => {
    const node = svgRef.current;
    if (!node || dates.length < 3) return;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      const rect = node.getBoundingClientRect();
      const ratio = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
      const plotRatio = Math.min(1, Math.max(0,
        (ratio * W - PAD.left) / (W - PAD.left - PAD.right)));
      const span = visible.end - visible.start;
      zoomAt(event.deltaY > 0 ? 1.18 : 0.85, visible.start + plotRatio * span);
    };
    node.addEventListener("wheel", onWheel, { passive: false });
    return () => node.removeEventListener("wheel", onWheel);
  }, [dates.length, visible, zoomAt]);

  const indexAt = (clientX: number): number | null => {
    const node = svgRef.current;
    if (!node) return null;
    const rect = node.getBoundingClientRect();
    const plotRatio = ((clientX - rect.left) / rect.width * W - PAD.left)
      / (W - PAD.left - PAD.right);
    const span = visible.end - visible.start;
    return Math.round(visible.start + Math.min(1, Math.max(0, plotRatio)) * span);
  };

  const handleDown = (event: React.MouseEvent<SVGSVGElement>) => {
    const index = indexAt(event.clientX);
    if (index === null) return;
    dragRef.current = { x: event.clientX, start: visible.start, end: visible.end };
  };
  const handleMove = (event: React.MouseEvent<SVGSVGElement>) => {
    const drag = dragRef.current;
    if (drag) {
      const node = svgRef.current;
      if (!node) return;
      const rect = node.getBoundingClientRect();
      const perPixel = (visible.end - visible.start)
        / Math.max(1, rect.width * (W - PAD.left - PAD.right) / W);
      const delta = Math.round((drag.x - event.clientX) * perPixel);
      const span = drag.end - drag.start;
      let start = drag.start + delta;
      let end = drag.end + delta;
      if (start < 0) { start = 0; end = span; }
      if (end > dates.length - 1) { end = dates.length - 1; start = end - span; }
      setRange({ start, end });
      return;
    }
    const index = indexAt(event.clientX);
    if (index !== null) setHover({ index });
  };
  const endDrag = () => { dragRef.current = null; };

  const ready = lines.length > 0 && domain !== null && dates.length > 0;

  /**
   * 图例里是否**所有线都被关掉**了（与"还没勾选任何实体"区分开）。
   *
   * 两种情况下 `lines` 都是空的、`ready` 都是 false，但用户该做的事完全相反：
   * 一个要"去左边勾选"，另一个要"点「全显」恢复"。共用一句
   * "左侧勾选板块/个股后…"会把已经勾好、只是把线全隐藏的人
   * 指向一个他早就做完的动作上。
   */
  const allHidden = useMemo(
    () => entities.length > 0
      && entities.every((entity) => hidden.has(entity.code)),
    [entities, hidden]);
  const hoverDate = hover ? dates[hover.index] : null;

  return (
    <div className="flow-chart-wrap">
      <div className="chart-toolbar">
        <span className="muted-text">滚轮缩放 · 拖拽平移 · 双击复位</span>
        <button className="btn-ghost chart-btn" title="放大"
                onClick={() => zoomAt(0.7, (visible.start + visible.end) / 2)}>＋</button>
        <button className="btn-ghost chart-btn" title="缩小"
                onClick={() => zoomAt(1.4, (visible.start + visible.end) / 2)}>－</button>
        <button className="btn-ghost chart-btn" disabled={!range}
                onClick={() => setRange(null)}>复位</button>
        {[5, 10, 20].map((days) => (
          <button key={days} className="btn-ghost chart-btn"
                  disabled={dates.length <= days}
                  onClick={() => setRange({
                    start: Math.max(0, dates.length - days), end: dates.length - 1 })}>
            近{days}日
          </button>
        ))}
        <span className="muted-text">
          可视 {windowDates.length ? label(windowDates[0]) : "—"} ~
          {windowDates.length ? ` ${label(windowDates[windowDates.length - 1])}` : ""}
          （共 {dates.length} 个交易日，单位：亿元）
        </span>
      </div>

      {!ready ? (
        <div className="empty-tip muted-text">
          {allHidden
            ? `全部 ${entities.length} 条走势线已被隐藏 —— 点上方的图例或`
              + "「全显」即可恢复（图例按钮现在是「全显」）。"
            : "暂无可画的数据：左侧勾选板块/个股后，这里会叠加它们的资金流走势线。"}
        </div>
      ) : (
        <svg ref={svgRef}
             viewBox={`0 0 ${W} ${hasKline ? boxHeight + 70 : boxHeight}`}
             className="flow-svg"
             role="img"
             onMouseDown={handleDown} onMouseMove={handleMove}
             onMouseUp={endDrag} onMouseLeave={() => { endDrag(); setHover(null); }}
             onDoubleClick={() => setRange(null)}>
          {/* 横向网格 + 左轴（亿元） */}
          {(() => {
            const step = niceStep(domain.span, 4);
            const ticks: number[] = [];
            for (let value = Math.ceil(domain.min / step) * step;
                 value <= domain.max; value += step) {
              ticks.push(Number(value.toFixed(6)));
              if (ticks.length > 12) break;
            }
            return ticks.map((value) => (
              <g key={value}>
                <line x1={PAD.left} x2={W - PAD.right} y1={y(value)} y2={y(value)}
                      stroke={Math.abs(value) < 1e-9 ? "var(--muted)" : "var(--border)"}
                      strokeDasharray={Math.abs(value) < 1e-9 ? "4 4" : "3 4"}
                      opacity={Math.abs(value) < 1e-9 ? 0.9 : 0.7} />
                <text x={PAD.left - 6} y={y(value) + 4} fontSize="10"
                      fill="var(--muted)" textAnchor="end">
                  {value.toFixed(1)}
                </text>
              </g>
            ));
          })()}

          {/* 时间轴：数据点多时按间隔抽稀 */}
          {windowDates.map((date, offset) => {
            const step = windowDates.length <= 8 ? 1
              : Math.ceil(windowDates.length / 8);
            if (offset % step !== 0) return null;
            const index = visible.start + offset;
            return (
              <g key={date}>
                <line x1={x(index)} x2={x(index)} y1={PAD.top}
                      y2={boxHeight - PAD.bottom} stroke="var(--border)" opacity={0.25} />
                <text x={x(index)} y={boxHeight - PAD.bottom + 14} fontSize="10"
                      fill="var(--muted)" textAnchor="middle">{label(date)}</text>
              </g>
            );
          })}

          {/* 零轴：资金流监控里这一条比什么都重要（上=净流入 / 下=净流出） */}
          <line x1={PAD.left} x2={W - PAD.right} y1={y(0)} y2={y(0)}
                stroke="var(--muted)" strokeWidth="1.4" opacity="0.9" />
          <text x={W - PAD.right + 6} y={y(0) + 4} fontSize="10" fill="var(--muted)">
            0
          </text>

          {/* 各实体的折线 */}
          {lines.map((line) => {
            // 逐点算坐标：只有 1 个点也要能看见（新加入的板块可能只有 1 天数据）
            const coords = line.points
              .map((point) => {
                const index = dates.indexOf(point.date);
                if (index < visible.start - 1 || index > visible.end + 1) return null;
                return { px: x(index), py: y(point.value) };
              })
              .filter((item): item is { px: number; py: number } => item !== null);
            if (coords.length === 1) {
              return (
                <circle key={line.entity.code} r="3" fill={line.color}
                        cx={coords[0].px} cy={coords[0].py}>
                  <title>{`${line.entity.name}（${line.entity.data_source}）`}</title>
                </circle>
              );
            }
            if (coords.length < 2) return null;
            const d = `M${coords.map((item) =>
              `${item.px.toFixed(1)},${item.py.toFixed(1)}`).join(" L")}`;
            return (
              <path key={line.entity.code} d={d} fill="none"
                    stroke={line.color} strokeWidth="1.8" opacity="0.95">
                <title>{`${line.entity.name}（${line.entity.data_source}）`}</title>
              </path>
            );
          })}

          {/* 悬停：竖线 + 每条线的当日值 */}
          {hoverDate && hover && (
            <g>
              <line x1={x(hover.index)} x2={x(hover.index)} y1={PAD.top}
                    y2={boxHeight - PAD.bottom} stroke="var(--muted)"
                    strokeDasharray="3 3" />
              {lines.map((line) => {
                const point = line.points.find((item) => item.date === hoverDate);
                if (!point) return null;
                return (
                  <circle key={`h-${line.entity.code}`} r="3"
                          fill={line.color} cx={x(hover.index)}
                          cy={y(point.value)} />
                );
              })}
            </g>
          )}

          {/* ================= K 线与成交量（与资金流共用时间轴，纵轴各自独立） =================
              用户要求"叠加对应个股或板块的K线和成交量柱状图，方便对比查看"。
              量纲差几个数量级（资金流亿元 / 价格元 / 量股），因此**不塞进同一根纵轴**
              （那会互相压扁到看不出形态），而是上下三块、x 轴严格对齐、缩放平移同步。 */}
          {hasKline && klineDomain && (
            <g>
              {/* 分隔线与区块标题 */}
              <line x1={PAD.left - 6} x2={W - PAD.right + 6} y1={klineTop - 3}
                    y2={klineTop - 3} stroke="var(--border)" strokeWidth="1" />
              <text x={PAD.left - 6} y={klineTop + 9} fontSize="10"
                    textAnchor="end" fill="var(--muted)">价格</text>
              {(() => {
                const step = niceStep(klineDomain.span, 3);
                const ticks: number[] = [];
                for (let value = Math.ceil(klineDomain.min / step) * step;
                     value <= klineDomain.max; value += step) ticks.push(value);
                return ticks.map((value) => (
                  <g key={`k-${value}`}>
                    <line x1={PAD.left} x2={W - PAD.right} y1={yPrice(value)}
                          y2={yPrice(value)} stroke="var(--border)"
                          strokeDasharray="2 4" opacity="0.45" />
                    <text x={PAD.left - 6} y={yPrice(value) + 4} fontSize="9"
                          textAnchor="end" fill="var(--muted)">
                      {value.toFixed(2)}
                    </text>
                  </g>
                ));
              })()}
              {/* ★ 价格：**连接成折线**（用户口径 2026-09-23：
                  "板块资金流—资金流走势 下方坐标轴 关于价格的数据 要连接成折线"）。
                  原来这里画蜡烛（high-low 细线 + 实心/空心矩形实体）。
                  对**板块/指数**这类标的，开盘≈收盘，实体只有 1px 高，
                  整排看起来就是"一条条小横杠"——既读不出趋势，也看不出拐点。
                  改成收盘价折线后走势一眼可读；单日 OHLC 仍保留在点上的
                  tooltip 里，信息一条没丢。 */}
              {priceLines.map((line, li) => {
                const color = PRICE_COLORS[li % PRICE_COLORS.length];
                const pts = line.points
                  .map((point) => ({ point, index: dates.indexOf(point.date) }))
                  .filter(({ index }) => index >= visible.start
                    && index <= visible.end);
                if (pts.length === 0) return null;
                return (
                  <g key={`p-${line.entity.code}`}>
                    <polyline fill="none" stroke={color} strokeWidth="1.6"
                      strokeLinejoin="round" strokeLinecap="round"
                      points={pts.map(({ point, index }) =>
                        `${x(index)},${yPrice(point.close)}`).join(" ")} />
                    {pts.map(({ point, index }) => (
                      <circle key={`pd-${point.date}`} cx={x(index)}
                        cy={yPrice(point.close)} r="2" fill={color}>
                        <title>{`${line.entity.name} ${point.date} 开${point.open} 高${point.high} 低${point.low} 收${point.close}`}</title>
                      </circle>
                    ))}
                  </g>
                );
              })}
            </g>
          )}

          {hasKline && volumeDomain && (
            <g>
              <line x1={PAD.left - 6} x2={W - PAD.right + 6} y1={volumeTop - 3}
                    y2={volumeTop - 3} stroke="var(--border)" strokeWidth="1" />
              <text x={PAD.left - 6} y={volumeTop + 9} fontSize="10"
                    textAnchor="end" fill="var(--muted)">量(万手)</text>
              {priceLines.map((line) => line.points.map((point) => {
                if (!point.volume) return null;
                const index = dates.indexOf(point.date);
                if (index < visible.start || index > visible.end) return null;
                const slot = (W - PAD.left - PAD.right)
                  / Math.max(1, visible.end - visible.start + 1);
                const width = Math.max(1.5, Math.min(7, slot * 0.6));
                const rising = point.close >= point.open;
                return (
                  <rect key={`v-${line.entity.code}-${point.date}`}
                        x={x(index) - width / 2} y={yVolume(point.volume)}
                        width={width}
                        height={Math.max(1, volumeTop + volumeHeight - 14 - yVolume(point.volume))}
                        fill={rising ? "#e06a5a" : "#2ea86e"} opacity="0.75">
                    <title>{`${line.entity.name} ${point.date} 量 ${toWanShou(point.volume).toFixed(1)} 万手`}</title>
                  </rect>
                );
              }))}
              <text x={PAD.left - 6} y={yVolume(volumeDomain.max) + 10} fontSize="9"
                    textAnchor="end" fill="var(--muted)">
                {toWanShou(volumeDomain.max).toFixed(0)}
              </text>
            </g>
          )}
        </svg>
      )}

      {/* 图例 + 当日读数（点图例即隐藏/显示，与用户"多线对比"的用法对齐） */}
      <div className="flow-legend">
        {lines.map((line) => {
          const point = hoverDate
            ? line.points.find((item) => item.date === hoverDate) : null;
          return (
            <span key={line.entity.code} className="flow-legend-item">
              <i style={{ background: line.color }} />
              {line.entity.name}
              {point && (
                <b className="mono" style={{
                  color: point.value >= 0 ? "var(--low)" : "var(--high)",
                }}> {signedYi(point.value)}</b>
              )}
            </span>
          );
        })}
      </div>
    </div>
  );
}
