import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { IntradayDailyBar, IntradayDailySnapshot } from "../api";

/**
 * 日K线图（蜡烛 + 成交量 + 均线 + 高量柱安全线/风险线 + 保护线）。
 *
 * 与分时图共用同一套坐标轴策略：
 *  - Y轴按**可见区间**的K线高低点定标（整齐刻度 1/2/2.5/5×10^k）；
 *  - 成交量独立刻度画在下方 1/4 区，不与价格争高度；
 *  - 滚轮缩放、拖拽平移、双击复位，Y轴随可见区间自动重算；
 *  - 高量柱用描边高亮（量柱分析是这套体系的核心，必须一眼看见）。
 * 颜色遵循A股习惯：红涨绿跌（--low 红 / --high 绿），与项目其它模块一致。
 */

const W = 1000;
const H = 420;
const PAD = { top: 16, right: 76, bottom: 40, left: 58 };
const VOLUME_RATIO = 0.26;   // 成交量区占绘图高度比例
const MIN_BARS = 15;

function niceStep(span: number, target = 4): number {
  if (!Number.isFinite(span) || span <= 0) return 1;
  const rough = span / target;
  const magnitude = Math.pow(10, Math.floor(Math.log10(rough)));
  const normalized = rough / magnitude;
  const step = normalized <= 1 ? 1
    : normalized <= 2 ? 2
    : normalized <= 2.5 ? 2.5
    : normalized <= 5 ? 5 : 10;
  return step * magnitude;
}

type Hover = { index: number; bar: IntradayDailyBar } | null;

function DailyCandleChart({
  snapshot, height = H, emptyReason = "", emptyHint = "",
}: {
  snapshot: IntradayDailySnapshot;
  height?: number;
  /** 空图时的具体原因（由面板给出：正在加载 / 请求失败 / 后端确实无数据） */
  emptyReason?: string;
  /** 补充线索（数据源、耗时等），展示在原因后面 */
  emptyHint?: string;
}) {
  const bars = snapshot.bars;
  const markers = useMemo(() => snapshot.signal_history ?? [], [snapshot]);
  // 日期 → 下标：标记是按日期给的（回放用的是同一批bar），查表比逐点 findIndex 稳
  const indexByDate = useMemo(
    () => new Map(bars.map((bar, index) => [bar.date, index])), [bars]);
  const [range, setRange] = useState<{ start: number; end: number } | null>(null);
  const [hover, setHover] = useState<Hover>(null);
  const [showMa, setShowMa] = useState(true);
  const svgRef = useRef<SVGSVGElement | null>(null);
  const dragRef = useRef<{ x: number; start: number; end: number } | null>(null);

  const total = bars.length;
  const view = useMemo(() => {
    if (total === 0) return { start: 0, end: 0 };
    if (!range) return { start: 0, end: total - 1 };
    return range;
  }, [range, total]);
  const span = Math.max(1, view.end - view.start);

  const plotW = W - PAD.left - PAD.right;
  const priceH = (height - PAD.top - PAD.bottom) * (1 - VOLUME_RATIO);
  const volumeTop = PAD.top + priceH + 8;
  const volumeH = (height - PAD.top - PAD.bottom) * VOLUME_RATIO - 8;

  const x = useCallback((index: number) =>
    PAD.left + ((index - view.start) / span) * plotW, [view.start, span, plotW]);

  const visible = useMemo(
    () => bars.slice(Math.max(0, view.start), Math.min(total, view.end + 1)),
    [bars, view, total]);

  const domain = useMemo(() => {
    if (!visible.length) return null;
    let low = Math.min(...visible.map((b) => b.low));
    let high = Math.max(...visible.map((b) => b.high));
    if (showMa) {
      (["ma5", "ma10", "ma20", "ma60"] as const).forEach((key) => {
        const levels = visible.flatMap((b) => {
          const value = b[key];
          return typeof value === "number" ? [value] : [];
        });
        if (levels.length) {
          low = Math.min(low, ...levels);
          high = Math.max(high, ...levels);
        }
      });
    }
    const pad = (high - low || high * 0.01) * 0.06;
    return { low: low - pad, high: high + pad, span: (high - low) + pad * 2 };
  }, [visible, showMa]);

  const yPrice = useCallback((price: number) => {
    if (!domain) return PAD.top;
    return PAD.top + (1 - (price - domain.low) / (domain.span || 1)) * priceH;
  }, [domain, priceH]);

  const volumeMax = Math.max(1, ...visible.map((b) => b.volume || 0));
  const yVolume = useCallback(
    (volume: number) => volumeTop + volumeH -
      ((volume || 0) / volumeMax) * volumeH,
    [volumeTop, volumeH, volumeMax]);

  const yTicks = useMemo(() => {
    if (!domain) return [];
    const step = niceStep(domain.span, 4);
    const ticks: number[] = [];
    for (let value = Math.ceil(domain.low / step) * step;
         value <= domain.high + 1e-9; value += step) {
      ticks.push(Number(value.toFixed(4)));
      if (ticks.length > 12) break;
    }
    return ticks;
  }, [domain]);

  const barWidth = Math.max(1.5, (plotW / (span + 1)) * 0.62);

  const zoomAt = useCallback((factor: number, anchorIndex: number) => {
    setRange((current) => {
      const base = current ?? { start: 0, end: total - 1 };
      const currentSpan = base.end - base.start;
      const next = Math.max(MIN_BARS,
                            Math.min(total - 1, Math.round(currentSpan * factor)));
      const ratio = currentSpan > 0
        ? (anchorIndex - base.start) / currentSpan : 0.5;
      let start = Math.round(anchorIndex - next * ratio);
      let end = start + next;
      if (start < 0) { start = 0; end = next; }
      if (end > total - 1) { end = total - 1; start = Math.max(0, end - next); }
      return { start, end };
    });
  }, [total]);

  const reset = useCallback(() => setRange(null), []);

  useEffect(() => {
    const node = svgRef.current;
    if (!node) return;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      const rect = node.getBoundingClientRect();
      const ratio = Math.min(1, Math.max(0,
        ((event.clientX - rect.left) / rect.width * W - PAD.left) / plotW));
      const anchor = Math.round(view.start + ratio * span);
      zoomAt(event.deltaY > 0 ? 1.2 : 0.83, anchor);
    };
    node.addEventListener("wheel", onWheel, { passive: false });
    return () => node.removeEventListener("wheel", onWheel);
  }, [view.start, span, zoomAt, plotW]);

  const indexAt = (clientX: number): number | null => {
    const node = svgRef.current;
    if (!node) return null;
    const rect = node.getBoundingClientRect();
    const ratio = ((clientX - rect.left) / rect.width * W - PAD.left) / plotW;
    return Math.round(view.start + Math.min(1, Math.max(0, ratio)) * span);
  };

  const handleMove = (event: React.MouseEvent<SVGSVGElement>) => {
    const drag = dragRef.current;
    const index = indexAt(event.clientX);
    if (index === null) return;
    if (drag) {
      const delta = drag.x - index;
      let start = drag.start + delta;
      let end = drag.end + delta;
      const width = drag.end - drag.start;
      if (start < 0) { start = 0; end = width; }
      if (end > total - 1) { end = total - 1; start = Math.max(0, end - width); }
      setRange({ start, end });
      return;
    }
    const bar = bars[index];
    if (bar) setHover({ index, bar });
  };

  if (!total || !domain) {
    // 空状态必须**说清原因**：这里原来只写死「日K数据不可用」，于是"请求失败"
    // 与"后端确实没有日线"看起来一模一样，用户无法判断是自己网络问题还是数据问题
    // （实测用户报"非开盘时间强制刷新刷不出日K"，就是被这句笼统提示挡住了排查）。
    const reason = emptyReason
      || (total === 0 ? "该标的没有可用日K数据" : "当前可视区间没有K线");
    return (
      <div className="empty-tip muted-text">
        日K数据不可用：{reason}
        {emptyHint ? <span className="muted-text">（{emptyHint}）</span> : null}
      </div>
    );
  }

  const maSeries: { key: "ma5" | "ma10" | "ma20" | "ma60"; label: string;
                    color: string }[] = [
    { key: "ma5", label: "MA5", color: "#e8c07d" },
    { key: "ma10", label: "MA10", color: "#7fd1e8" },
    { key: "ma20", label: "MA20", color: "#c58fe8" },
    { key: "ma60", label: "MA60", color: "#8b98a5" },
  ];

  const anchor = snapshot.active_anchor;
  const stop = snapshot.protective?.stop_line ?? null;
  const inView = (level: number | null) =>
    level !== null && domain && level >= domain.low && level <= domain.high;

  return (
    <div className="daily-chart-wrap">
      <div className="chart-toolbar">
        <span className="muted-text">滚轮缩放 · 拖拽平移 · 双击复位</span>
        <button className="btn-ghost chart-btn"
                onClick={() => zoomAt(0.7, Math.round((view.start + view.end) / 2))}>＋</button>
        <button className="btn-ghost chart-btn"
                onClick={() => zoomAt(1.4, Math.round((view.start + view.end) / 2))}>－</button>
        <button className="btn-ghost chart-btn" onClick={reset}
                disabled={!range}>复位</button>
        <label className="chart-toggle">
          <input type="checkbox" checked={showMa}
                 onChange={(e) => setShowMa(e.target.checked)} />均线
        </label>
        <span className="muted-text">
          {bars[view.start]?.date} ~ {bars[view.end]?.date}（{span + 1}根）
        </span>
      </div>

      <svg ref={svgRef} viewBox={`0 0 ${W} ${height}`} className="intraday-svg"
           role="img"
           onMouseMove={handleMove}
           onMouseDown={(e) => {
             const index = indexAt(e.clientX);
             if (index !== null) {
               dragRef.current = { x: index, start: view.start, end: view.end };
             }
           }}
           onMouseUp={() => { dragRef.current = null; }}
           onMouseLeave={() => { dragRef.current = null; setHover(null); }}
           onDoubleClick={reset}>
        {/* 价格网格与刻度 */}
        {yTicks.map((price) => (
          <g key={price}>
            <line x1={PAD.left} x2={W - PAD.right} y1={yPrice(price)} y2={yPrice(price)}
                  stroke="var(--border)" strokeDasharray="3 4" opacity={0.6} />
            <text x={PAD.left - 6} y={yPrice(price) + 4} fontSize="10"
                  fill="var(--muted)" textAnchor="end">{price.toFixed(2)}</text>
          </g>
        ))}
        {/* 日期刻度（每约1/6取一个） */}
        {Array.from({ length: 6 }, (_, i) =>
          Math.round(view.start + (span * i) / 5)).map((index) => {
          const bar = bars[Math.min(index, total - 1)];
          if (!bar) return null;
          return (
            <text key={index} x={x(index)} y={height - PAD.bottom + 14}
                  fontSize="10" fill="var(--muted)" textAnchor="middle">
              {bar.date.slice(5)}
            </text>
          );
        })}

        {/* 高量柱安全线 / 风险线（当前生效锚点） */}
        {anchor && inView(anchor.body_top) && (
          <g className="lv-sell">
            <line x1={PAD.left} x2={W - PAD.right} y1={yPrice(anchor.body_top)}
                  y2={yPrice(anchor.body_top)} strokeWidth="1.3"
                  strokeDasharray="7 3" />
            <text x={PAD.left + 4} y={yPrice(anchor.body_top) - 4} fontSize="10"
                  className="lv-text">
              高量安全线 {anchor.body_top.toFixed(2)}（{anchor.date.slice(5)}）
            </text>
          </g>
        )}
        {anchor && inView(anchor.body_bottom) && (
          <g className="lv-stop">
            <line x1={PAD.left} x2={W - PAD.right} y1={yPrice(anchor.body_bottom)}
                  y2={yPrice(anchor.body_bottom)} strokeWidth="1.3"
                  strokeDasharray="7 3" />
            <text x={PAD.left + 4} y={yPrice(anchor.body_bottom) - 4} fontSize="10"
                  className="lv-text">
              高量风险线 {anchor.body_bottom.toFixed(2)}
            </text>
          </g>
        )}
        {/* S5 止损保护线 */}
        {inView(stop) && stop !== null && (
          <g className="lv-buy">
            <line x1={PAD.left} x2={W - PAD.right} y1={yPrice(stop)}
                  y2={yPrice(stop)} strokeWidth="1.3" strokeDasharray="2 4" />
            <text x={PAD.left + 4} y={yPrice(stop) + 12} fontSize="10"
                  className="lv-text">止损保护 {stop.toFixed(2)}</text>
          </g>
        )}

        {/* 均线 */}
        {showMa && maSeries.map(({ key, color }) => {
          const points = bars.map((bar, index) => {
            const value = bar[key];
            return typeof value === "number" && index >= view.start && index <= view.end
              ? { index, value } : null;
          }).filter((p): p is { index: number; value: number } => p !== null);
          if (points.length < 2) return null;
          const path = points.map((p, i) =>
            `${i === 0 ? "M" : "L"}${x(p.index).toFixed(1)},${yPrice(p.value).toFixed(1)}`)
            .join(" ");
          return <path key={key} d={path} fill="none" stroke={color}
                       strokeWidth="1.2" opacity={0.85} />;
        })}

        {/* 蜡烛 */}
        {visible.map((bar, offset) => {
          const index = view.start + offset;
          const cx = x(index);
          const up = bar.close >= bar.open;
          const color = up ? "var(--low)" : "var(--high)";
          const top = yPrice(Math.max(bar.open, bar.close));
          const bottom = yPrice(Math.min(bar.open, bar.close));
          return (
            <g key={bar.date}>
              <line x1={cx} x2={cx} y1={yPrice(bar.high)} y2={yPrice(bar.low)}
                    stroke={color} strokeWidth="1" />
              <rect x={cx - barWidth / 2} width={barWidth}
                    y={top} height={Math.max(1, bottom - top)}
                    fill={up ? "none" : color} stroke={color}
                    strokeWidth="1" />
            </g>
          );
        })}

        {/* 成交量（高量柱描边高亮 + 着色） */}
        {visible.map((bar, offset) => {
          const index = view.start + offset;
          const cx = x(index);
          const top = yVolume(bar.volume);
          const heightPx = volumeTop + volumeH - top;
          const up = bar.close >= bar.open;
          const color = up ? "var(--low)" : "var(--high)";
          return (
            <rect key={`v-${bar.date}`} x={cx - barWidth / 2} width={barWidth}
                  y={top} height={Math.max(0.8, heightPx)}
                  fill={bar.is_high_volume ? "var(--medium)" : color}
                  opacity={bar.is_high_volume ? 0.95 : 0.5}
                  stroke={bar.is_high_volume ? "var(--medium)" : "none"}
                  strokeWidth={bar.is_high_volume ? 1 : 0}>
              <title>
                {`${bar.date} 量 ${bar.volume.toFixed(0)}`
                  + (bar.is_high_volume ? "（高量柱）" : "")
                  + (bar.is_double_volume ? " 倍量" : "")
                  + (bar.is_explode_volume ? " 爆量" : "")
                  + (bar.is_ground_volume ? " 地量" : "")}
              </title>
            </rect>
          );
        })}

        {/* 买卖信号标记（最近 30 个交易日因果回放）：
            买点画在K线下方（红▲），卖点/风控画在上方（绿▼/橙▼）。
            与分时图的三角标记同一套视觉语言，一眼能分辨方向。 */}
        {markers.map((mark) => {
          const index = indexByDate.get(mark.date);
          if (index === undefined || index < view.start || index > view.end) return null;
          const bar = bars[index];
          if (!bar) return null;
          const cx = x(index);
          const isBuy = mark.side === "buy";
          const color = isBuy ? "var(--low)"
            : mark.side === "risk" ? "var(--medium)" : "var(--high)";
          // 贴在该bar的高低点外侧；再夹到绘图区内，避免极端bar把箭头画到图外
          const base = isBuy
            ? Math.min(volumeTop - 12, yPrice(bar.low) + 12)
            : Math.max(PAD.top + 12, yPrice(bar.high) - 12);
          const path = isBuy
            ? `M${cx},${base - 9} L${cx - 5},${base + 1} L${cx + 5},${base + 1} Z`
            : `M${cx},${base + 9} L${cx - 5},${base - 1} L${cx + 5},${base - 1} Z`;
          const label = `${mark.date} ${isBuy ? "买点" : mark.side === "risk" ? "风控" : "卖点"} `
            + `${mark.code} ${mark.name}`.trim()
            + (mark.entry !== null ? `｜建议买点 ${mark.entry.toFixed(2)}` : "")
            + (mark.stop_loss !== null ? `｜止损 ${mark.stop_loss.toFixed(2)}` : "")
            + (mark.price !== null ? `｜当日收盘 ${mark.price.toFixed(2)}` : "");
          // 标记旁直接给**可操作价位**：买点给建议买价（没有则给当日收盘），
          // 卖点/风控给止损位。只画 B8 这种编号用户还得去下面表格里找价。
          const priceText = isBuy
            ? (mark.entry ?? mark.price)
            : (mark.stop_loss ?? mark.price);
          const caption = priceText === null
            ? mark.code : `${mark.code} ${priceText.toFixed(2)}`;
          return (
            <g key={`${mark.date}-${mark.side}-${mark.code}`}>
              <path d={path} fill={color} opacity={0.92}>
                <title>{label}</title>
              </path>
              <text x={cx} y={isBuy ? base + 11 : base - 4} textAnchor="middle"
                    fontSize="8.5" fill={color} stroke="var(--panel)"
                    strokeWidth="2.5" paintOrder="stroke"
                    style={{ pointerEvents: "none" }}>
                {caption}
                <title>{label}</title>
              </text>
            </g>
          );
        })}

        {/* 悬浮十字光标 */}
        {hover && (
          <g>
            <line x1={x(hover.index)} x2={x(hover.index)} y1={PAD.top}
                  y2={volumeTop + volumeH} stroke="var(--muted)" strokeDasharray="2 3" />
          </g>
        )}
      </svg>

      <div className="chart-readout">
        {hover ? (          <>
            <span className="mono">{hover.bar.date}</span>
            <span>开 <b className="mono">{hover.bar.open.toFixed(2)}</b></span>
            <span>高 <b className="mono">{hover.bar.high.toFixed(2)}</b></span>
            <span>低 <b className="mono">{hover.bar.low.toFixed(2)}</b></span>
            <span>收 <b className="mono" style={{
              color: (hover.bar.pct_chg ?? 0) >= 0 ? "var(--low)" : "var(--high)",
            }}>{hover.bar.close.toFixed(2)}</b></span>
            {hover.bar.pct_chg !== null && (
              <span className="mono" style={{
                color: hover.bar.pct_chg >= 0 ? "var(--low)" : "var(--high)",
              }}>
                {hover.bar.pct_chg >= 0 ? "+" : ""}{hover.bar.pct_chg.toFixed(2)}%
              </span>
            )}
            <span>振 <b className="mono">{(hover.bar.amplitude ?? 0).toFixed(2)}%</b></span>
            <span>量 <b className="mono">{hover.bar.volume.toFixed(0)}</b></span>
            {hover.bar.is_high_volume && <span className="badge-tag">高量柱</span>}
            {hover.bar.is_double_volume && <span className="badge-tag">倍量</span>}
            {hover.bar.is_shrink_half && <span className="badge-tag">缩倍量</span>}
            {hover.bar.is_ladder_down && <span className="badge-tag">梯量</span>}
            {hover.bar.is_flat_volume && <span className="badge-tag">平量</span>}
            {hover.bar.is_ground_volume && <span className="badge-tag">地量</span>}
            {hover.bar.is_explode_volume && <span className="badge-tag">爆量</span>}
            {hover.bar.is_long_lower_shadow && <span className="badge-tag">大长腿</span>}
            {hover.bar.is_big_yang && <span className="badge-tag">大阳</span>}
            {hover.bar.is_big_yin && <span className="badge-tag">大阴</span>}
          </>
        ) : (
          <>
            {maSeries.map(({ key, label, color }) => (
              <span key={key} className="legend" style={{ color }}>
                — {label} {snapshot.ma[label] ?? "—"}
              </span>
            ))}
            <span className="legend" style={{ color: "var(--medium)" }}>
              ▮ 橙色量柱=高量柱
            </span>
            <span className="legend" style={{ color: "var(--low)" }}>
              ┄ 红虚线=高量安全线
            </span>
            <span className="legend" style={{ color: "#e0b020" }}>
              ┄ 黄虚线=高量风险线
            </span>
            <span className="legend" style={{ color: "var(--high)" }}>
              ┄ 绿点线=止损保护
            </span>
            <span className="legend" style={{ color: "var(--low)" }}>▲ 买点</span>
            <span className="legend" style={{ color: "var(--high)" }}>▼ 卖点</span>
            <span className="legend" style={{ color: "var(--medium)" }}>▼ 风控</span>
            <span className="legend muted-text">标记旁数字=建议买价/止损位</span>
          </>
        )}
      </div>
    </div>
  );
}

// 同理：报价推送（5 秒）与自选刷新不该重渲染 120 根蜡烛 + 量柱。
export default memo(DailyCandleChart);
