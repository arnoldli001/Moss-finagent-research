import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CrowdingDetail as DetailPayload, CrowdingRow } from "../sectorCrowdingApi";

/**
 * 单板块详情的**纯展示**部分（双面板曲线），数据由外层喂进来。
 *
 * 拆出来的原因：详情现在既能出现在居中弹窗里（主入口），也要能单独渲染，
 * 取数逻辑（`useEffect` + 请求）只应该有一份 —— 放在弹窗壳里。
 * 这里只管画图，不碰网络。
 *
 * ## 缩放口径：只缩 X 轴（时间），Y 轴自适应
 *
 * 详情是**时间序列**（近 6 年 1400+ 根日线），所以缩放只作用在时间轴上：
 *
 * - 滚轮 / ＋－ 按钮 / `+` `-` 键：以**光标位置为锚点**缩放（放大时光标下的那天
 *   不动，符合直觉）；
 * - 按住拖拽：平移时间窗；
 * - 双击 / 「复位」：回到全区间。
 *
 * **Y 轴按可见区间重算**，而不是固定全历史量程。固定量程时放大只会把曲线拉宽、
 * 看不出细节（水位在 0~100% 里挤成一条线）；自适应后放大哪段就看哪段的起伏 ——
 * 这正是"放大来细看"想要的。代价是纵轴刻度会随缩放变化，所以刻度标签一直显示，
 * 当前窗口的日期范围也在工具条里写出来。
 */

const W = 960;
const PANEL_H = 150;
const GAP = 26;
const PAD = { top: 14, right: 70, bottom: 34, left: 64 };

const WATER_COLOR = "#e0b020";
const RAW_COLOR = "#4a9eff";
const MA_COLOR = "#2ea86e";
const HIGH_COLOR = "#d95c4a";

const MIN_SPAN = 5;          // 最少显示 5 个点（再少就没有"曲线"了）
const MAX_ZOOM = 60;         // 最多放大 60 倍
const DRAG_THRESHOLD = 4;    // 超过这个像素才算拖拽，避免点一下被当成平移

type Hover = { index: number } | null;

/**
 * 十字轴状态：鼠标在绘图区里的位置。
 *
 * `x`/`y` 是**画布坐标**（viewBox 单位），用来画跟随鼠标的横竖线；
 * 读数取的是**离鼠标最近的那条曲线在该日期的真实值**（吸附），
 * 而不是鼠标位置的插值 —— 十字轴的价值就在于告诉你"这条线上那天的值是多少"。
 */
type Cursor = {
  x: number;
  y: number;
  index: number;
  /** 鼠标落在哪个面板：上=水位，下=成交额占比 */
  phase: "water" | "share";
  /** 吸附到的那条线的读数 */
  snapped: number | null;
  snappedLabel: string;
};

/** 可见区间（原序列的索引，`end` 含） */
type View = { start: number; end: number };

export default function SectorCrowdingDetailChart({
  data, sectorCode, sectorName, showRaw, onToggleRaw, expanded, onToggleExpand,
}: {
  data: DetailPayload;
  sectorCode: string;
  sectorName: string;
  showRaw: boolean;
  onToggleRaw: (next: boolean) => void;
  /** 放大视图（弹窗占更大面积）；由弹窗壳控制 */
  expanded?: boolean;
  onToggleExpand?: () => void;
}) {
  const [hover, setHover] = useState<Hover>(null);
  const [cursor, setCursor] = useState<Cursor | null>(null);
  const [view, setView] = useState<View | null>(null);
  const svgRef = useRef<SVGSVGElement | null>(null);
  const dragRef = useRef<{ x: number; moved: boolean; view: View } | null>(null);

  const series = useMemo<CrowdingRow[]>(() => data.series ?? [], [data]);
  const n = series.length;

  // 换了板块就把缩放复位（同一个组件实例会被复用）
  useEffect(() => { setView(null); setHover(null); setCursor(null); }, [sectorCode]);

  /** 当前可见窗口；未缩放过时就是全区间。 */
  const window_ = useMemo<View>(() => {
    if (n === 0) return { start: 0, end: 0 };
    const fallback = { start: 0, end: n - 1 };
    if (!view) return fallback;
    const start = Math.max(0, Math.min(view.start, n - 1));
    const end = Math.max(start, Math.min(view.end, n - 1));
    return { start, end };
  }, [view, n]);

  const visible = useMemo(
    () => series.slice(window_.start, window_.end + 1),
    [series, window_]);

  const zoomRatio = n > 1 ? (n - 1) / Math.max(1, window_.end - window_.start) : 1;
  const zoomed = zoomRatio > 1.001;

  /**
   * Y 轴量程**只按可见区间**算 —— 见模块头的说明。
   * 下限留一点余量，否则贴 0 的曲线会被画在下边框上。
   */
  const domains = useMemo(() => {
    if (visible.length === 0) return null;
    const waters = visible.map((row) => row.water_level)
      .filter((value): value is number => value !== null && Number.isFinite(value));
    const raws = visible.map((row) => row.raw_crowding)
      .filter((value): value is number => value !== null && Number.isFinite(value));
    const mas = visible.map((row) => row.ma5_crowding)
      .filter((value): value is number => value !== null && Number.isFinite(value));
    // 水位：全区间时保持 0~100% 的固定量纲（便于跨板块对比），放大后才收窄
    const waterTop = zoomed
      ? Math.max(0.01, Math.min(1.4, Math.max(0, ...waters) * 1.15))
      : Math.max(1, Math.min(1.4, (Math.max(0, ...waters) || 0.5) * 1.1));
    const shareTop = Math.max(1e-6, Math.max(0, ...raws, ...mas) * 1.15);
    return { waterTop, shareTop };
  }, [visible, zoomed]);

  const plotW = W - PAD.left - PAD.right;
  const waterTopY = PAD.top;
  const waterBottomY = PAD.top + PANEL_H;
  const shareTopY = waterBottomY + GAP;
  const shareBottomY = shareTopY + PANEL_H;

  /** 相对下标 → 画布 X（0 = 窗口最左，len-1 = 窗口最右）。 */
  const x = useCallback((offset: number) => PAD.left
    + (visible.length <= 1 ? 0.5 : offset / (visible.length - 1)) * plotW,
    [visible.length, plotW]);
  const yWater = useCallback((value: number) => waterBottomY
    - (value / (domains?.waterTop ?? 1)) * (waterBottomY - waterTopY),
    [domains, waterBottomY, waterTopY]);
  const yShare = useCallback((value: number) => shareBottomY
    - (value / (domains?.shareTop ?? 1)) * (shareBottomY - shareTopY),
    [domains, shareBottomY, shareTopY]);

  /**
   * 画折线。`which` 决定用哪个 Y 轴 —— **不要**用"函数相等"来推断用哪条轴：
   * 传进来的箭头函数每次渲染都是新引用，比较结果不稳定（第一版就是这么写的）。
   */
  const path = useCallback((which: "water" | "share",
                            pick: (row: CrowdingRow) => number | null) => {
    const yScale = which === "water" ? yWater : yShare;
    const parts: string[] = [];
    let open = false;
    visible.forEach((row, offset) => {
      const value = pick(row);
      if (value === null || !Number.isFinite(value)) { open = false; return; }
      parts.push(`${open ? "L" : "M"}${x(offset).toFixed(1)},`
        + `${yScale(value).toFixed(1)}`);
      open = true;
    });
    return parts.join(" ");
  }, [visible, x, yWater, yShare]);

  /**
   * 缩放：`factor > 1` 缩小（看更宽），`< 1` 放大。
   * `anchorRatio`（0~1）是要保持不动的点在窗口里的相对位置 —— 滚轮用它实现
   * "光标下那天不动"，按钮用 0.5 从中间缩放。
   *
   * 最大倍率在这里**真正夹住**：不能只靠按钮的 disabled —— 滚轮和键盘不走那个
   * 分支，不加限制能一路缩到 368×（只剩 5 个点），曲线变成折线段反而没用了。
   */
  const zoomBy = useCallback((factor: number, anchorRatio = 0.5) => {
    if (n <= 1) return;
    const minSpan = Math.max(MIN_SPAN - 1, Math.ceil((n - 1) / MAX_ZOOM));
    setView((current) => {
      const base = current ?? { start: 0, end: n - 1 };
      const span = base.end - base.start;
      if (span <= 0) return base;
      let nextSpan = Math.round(span * factor);
      nextSpan = Math.max(minSpan, Math.min(n - 1, nextSpan));
      if (nextSpan === span) return base;
      const anchor = base.start + span * Math.min(1, Math.max(0, anchorRatio));
      let start = Math.round(anchor - (anchor - base.start) * (nextSpan / span));
      start = Math.max(0, Math.min(start, n - 1 - nextSpan));
      return { start, end: start + nextSpan };
    });
  }, [n]);

  const resetView = useCallback(() => setView(null), []);

  /** 平移（按像素换算成数据点数）。 */
  const panBy = useCallback((deltaPx: number) => {
    if (n <= 1) return;
    setView((current) => {
      const base = current ?? { start: 0, end: n - 1 };
      const span = base.end - base.start;
      if (span <= 0 || span >= n - 1) return base;
      const shift = Math.round(-deltaPx / plotW * span);
      if (shift === 0) return base;
      const start = Math.max(0, Math.min(base.start + shift, n - 1 - span));
      return { start, end: start + span };
    });
  }, [n, plotW]);

  /** 客户端 X → 窗口内的相对比例（0~1，可越界，调用方自行 clamp）。 */
  const ratioAt = useCallback((clientX: number): number => {
    const node = svgRef.current;
    if (!node) return 0.5;
    const rect = node.getBoundingClientRect();
    const local = (clientX - rect.left) / rect.width * W;
    return (local - PAD.left) / plotW;
  }, [plotW]);

  /** 客户端 X → 原序列索引（考虑当前窗口）。 */
  const indexAt = useCallback((clientX: number): number | null => {
    if (visible.length === 0) return null;
    const ratio = Math.min(1, Math.max(0, ratioAt(clientX)));
    const offset = Math.round(ratio * (visible.length - 1));
    const index = window_.start + offset;
    return index >= 0 && index < n ? index : null;
  }, [ratioAt, visible.length, window_.start, n]);

  /**
   * 客户端坐标 → 画布坐标（viewBox 单位）。十字轴要用它来放横线、定读数。
   * 注意要按 SVG 实际的渲染宽高比换算，不能假设 viewBox 的宽高比等于元素尺寸。
   */
  const localPoint = useCallback((clientX: number, clientY: number,
                                  vbHeight: number) => {
    const node = svgRef.current;
    if (!node) return null;
    const rect = node.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return null;
    return {
      x: (clientX - rect.left) / rect.width * W,
      y: (clientY - rect.top) / rect.height * vbHeight,
    };
  }, []);

  /**
   * 更新十字轴：竖线吸附到最近交易日，横线吸附到离鼠标最近的那条**曲线**，
   * 并把该线在该日期的真实值作为读数。
   *
   * 为什么要"吸附曲线"而不是直接用鼠标的 Y：水位、MA5、原始占比 三条线可能
   * 差得很远，只用鼠标 Y 换算出来的数不对应任何一条线，读出来没有意义。
   */
  const updateCursor = useCallback((clientX: number, clientY: number,
                                    vbHeight: number) => {
    if (visible.length === 0 || !domains) { setCursor(null); return; }
    const local = localPoint(clientX, clientY, vbHeight);
    const index = indexAt(clientX);
    if (!local || index === null) { setCursor(null); return; }
    // 只在绘图区内显示十字轴，落在左右留白/边框上就不显示
    if (local.x < PAD.left || local.x > W - PAD.right) { setCursor(null); return; }

    const row = series[index];
    const inWater = local.y >= waterTopY && local.y <= waterBottomY;
    const inShare = local.y >= shareTopY && local.y <= shareBottomY;
    if (!inWater && !inShare) { setCursor(null); return; }
    const phase: "water" | "share" = inWater ? "water" : "share";

    // 候选线：yScale 把值换算成画布 Y，未知的线（数据不足）跳过
    const candidates: { value: number; y: number; label: string }[] = [];
    if (phase === "water") {
      if (row.water_level !== null && Number.isFinite(row.water_level)) {
        candidates.push({ value: row.water_level, y: yWater(row.water_level),
                          label: "水位" });
      }
    } else {
      if (row.ma5_crowding !== null && Number.isFinite(row.ma5_crowding)) {
        candidates.push({ value: row.ma5_crowding, y: yShare(row.ma5_crowding),
                          label: "MA5" });
      }
      if (showRaw && row.raw_crowding !== null
          && Number.isFinite(row.raw_crowding)) {
        candidates.push({ value: row.raw_crowding, y: yShare(row.raw_crowding),
                          label: "原始" });
      }
    }
    if (candidates.length === 0) {
      setCursor({ x: x(index - window_.start), y: local.y, index, phase,
                  snapped: null, snappedLabel: "" });
      setHover({ index });
      return;
    }
    let best = candidates[0];
    for (const item of candidates) {
      if (Math.abs(item.y - local.y) < Math.abs(best.y - local.y)) best = item;
    }
    setCursor({ x: x(index - window_.start), y: best.y, index, phase,
                snapped: best.value, snappedLabel: best.label });
    setHover({ index });
  }, [visible.length, domains, localPoint, indexAt, series, showRaw,
      x, window_.start, yWater, yShare, waterTopY, waterBottomY,
      shareTopY, shareBottomY]);

  const onWheel = useCallback((event: React.WheelEvent<SVGSVGElement>) => {
    // 只缩 X：纵向滚动页面的行为交给外层，横向/滚轮缩放才拦
    event.preventDefault();
    zoomBy(event.deltaY > 0 ? 1.25 : 1 / 1.25, ratioAt(event.clientX));
  }, [zoomBy, ratioAt]);

  // 键盘缩放（弹窗里按 +/- ；不给 input 抢走）
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) return;
      if (event.key === "+" || event.key === "=") { zoomBy(1 / 1.4, 0.5); }
      else if (event.key === "-" || event.key === "_") { zoomBy(1.4, 0.5); }
      else if (event.key === "0") { resetView(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [zoomBy, resetView]);

  if (n === 0) {
    return (
      <div className="warn-box">
        {sectorName || sectorCode} 还没有拥挤度数据 ——
        点顶部「一键刷新」后它才会被回填（首次全量约几分钟）。
      </div>
    );
  }

  const hoverRow = hover ? series[hover.index] : null;
  const last = visible[visible.length - 1];
  const waterPct = (value: number | null | undefined) =>
    value === null || value === undefined ? "—" : `${(value * 100).toFixed(1)}%`;
  const firstDate = visible[0]?.trade_date ?? "";
  const lastDate = last?.trade_date ?? "";
  /** SVG 的 viewBox 高度：十字轴换算鼠标坐标要用 */
  const vbHeight = shareBottomY + PAD.bottom;
  /** 读数格式：水位按百分比 1 位，占比按百分比 3 位（与纵轴刻度一致） */
  const readout = cursor?.snapped === null || cursor?.snapped === undefined
    ? "—"
    : cursor.phase === "water"
      ? `${(cursor.snapped * 100).toFixed(1)}%`
      : `${(cursor.snapped * 100).toFixed(3)}%`;

  return (
    <div className="crowding-detail">
      <div className="chart-toolbar">
        <b>{data.sector_name || sectorName || sectorCode}</b>
        <span className="mono muted-text">{sectorCode}</span>
        <span className="muted-text">
          {zoomed
            ? <>窗口 <b>{firstDate}</b> ~ <b>{lastDate}</b>（{visible.length} 根 / 共 {n} 根）</>
            : <>{data.first_trade_date} ~ {data.last_trade_date}（{data.bars} 根）</>}
        </span>
        <span className={data.water_level !== null
          && data.water_level >= data.threshold ? "crowding-water tone-warn"
          : "crowding-water"}>
          当前水位 {waterPct(data.water_level)}
        </span>
        <span style={{ flex: 1 }} />
        <span className="muted-text">
          滚轮缩放 · 拖拽平移 · 双击复位
        </span>
        <button className="btn-ghost chart-btn" title="放大（+）"
                disabled={zoomRatio >= MAX_ZOOM}
                onClick={() => zoomBy(1 / 1.4, 0.5)}>＋</button>
        <button className="btn-ghost chart-btn" title="缩小（-）"
                disabled={!zoomed}
                onClick={() => zoomBy(1.4, 0.5)}>－</button>
        <button className="btn-ghost chart-btn" title="复位时间轴（0）"
                disabled={!zoomed} onClick={resetView}>复位</button>
        <span className="muted-text mono">{zoomRatio.toFixed(1)}×</span>
        {onToggleExpand && (
          <button className="btn-ghost chart-btn"
                  title={expanded ? "退出放大视图" : "放大视图（弹窗占更大面积）"}
                  onClick={onToggleExpand}>
            {expanded ? "⤡ 收起" : "⤢ 放大视图"}
          </button>
        )}
        <label className="chart-toggle">
          <input type="checkbox" checked={showRaw}
                 onChange={(event) => onToggleRaw(event.target.checked)} />
          原始占比
        </label>
      </div>

      <svg ref={svgRef} viewBox={`0 0 ${W} ${vbHeight}`}
           className="intraday-svg" role="img"
           style={{ cursor: dragRef.current?.moved ? "grabbing" : "crosshair" }}
           onWheel={onWheel}
           onMouseDown={(event) => {
             dragRef.current = { x: event.clientX, moved: false, view: window_ };
           }}
           onMouseMove={(event) => {
             const drag = dragRef.current;
             if (drag) {
               const dx = event.clientX - drag.x;
               if (!drag.moved && Math.abs(dx) < DRAG_THRESHOLD) {
                 // 还没到拖拽阈值：先只更新十字光标，避免手一抖就把图移走
                 updateCursor(event.clientX, event.clientY, vbHeight);
                 return;
               }
               drag.moved = true;
               panBy(event.clientX - drag.x);
               drag.x = event.clientX;
               return;
             }
             updateCursor(event.clientX, event.clientY, vbHeight);
           }}
           onMouseUp={() => { dragRef.current = null; }}
           onMouseLeave={() => {
             dragRef.current = null; setHover(null); setCursor(null);
           }}
           onDoubleClick={resetView}>
        {/* ---------- 上：水位 ---------- */}
        <text x={PAD.left} y={waterTopY - 2} fontSize="10" fill="var(--muted)">
          拥挤度水位（当前平滑拥挤度 / 近 6 年最高值）
          {zoomed ? "　· 纵轴按当前窗口自适应" : ""}
        </text>
        {[0, 0.25, 0.5, 0.75, 1].map((ratio) => {
          const value = ratio * (domains?.waterTop ?? 1);
          return (
            <g key={`wy-${ratio}`}>
              <line x1={PAD.left} x2={W - PAD.right} y1={yWater(value)}
                    y2={yWater(value)} stroke="var(--border)"
                    strokeDasharray="3 4" opacity={0.5} />
              <text x={PAD.left - 6} y={yWater(value) + 4} fontSize="10"
                    fill="var(--muted)" textAnchor="end">
                {(value * 100).toFixed(zoomed ? 1 : 0)}%
              </text>
            </g>
          );
        })}
        {domains && data.threshold <= domains.waterTop && (
          <>
            <line x1={PAD.left} x2={W - PAD.right} y1={yWater(data.threshold)}
                  y2={yWater(data.threshold)} stroke={WATER_COLOR}
                  strokeDasharray="6 3" strokeWidth="1.2" />
            <text x={W - PAD.right + 4} y={yWater(data.threshold) + 4}
                  fontSize="10" fill={WATER_COLOR}>
              {(data.threshold * 100).toFixed(0)}%
            </text>
          </>
        )}
        {domains && 0.9 <= domains.waterTop && (
          <>
            <line x1={PAD.left} x2={W - PAD.right} y1={yWater(0.9)}
                  y2={yWater(0.9)} stroke={HIGH_COLOR} strokeDasharray="6 3"
                  strokeWidth="1.2" />
            <text x={W - PAD.right + 4} y={yWater(0.9) + 4} fontSize="10"
                  fill={HIGH_COLOR}>90%</text>
          </>
        )}
        <path d={path("water", (row) => row.water_level)} fill="none"
              stroke={WATER_COLOR} strokeWidth="1.6" />

        {/* ---------- 下：原始占比 / MA5 ---------- */}
        <text x={PAD.left} y={shareTopY - 2} fontSize="10" fill="var(--muted)">
          成交额占全市场比例
        </text>
        {[0, 0.5, 1].map((ratio) => {
          const value = ratio * (domains?.shareTop ?? 1);
          return (
            <g key={`sy-${ratio}`}>
              <line x1={PAD.left} x2={W - PAD.right} y1={yShare(value)}
                    y2={yShare(value)} stroke="var(--border)"
                    strokeDasharray="3 4" opacity={0.5} />
              <text x={PAD.left - 6} y={yShare(value) + 4} fontSize="10"
                    fill="var(--muted)" textAnchor="end">
                {(value * 100).toFixed(2)}%
              </text>
            </g>
          );
        })}
        {showRaw && (
          <path d={path("share", (row) => row.raw_crowding)} fill="none"
                stroke={RAW_COLOR} strokeWidth="1" opacity={0.55} />
        )}
        <path d={path("share", (row) => row.ma5_crowding)} fill="none"
              stroke={MA_COLOR} strokeWidth="1.6" />

        {/* X 轴日期（约 6 个刻度，按当前窗口取） */}
        {Array.from({ length: 6 }, (_, index) =>
          Math.round((visible.length - 1) * index / 5)).map((offset) => {
          const row = visible[Math.min(offset, visible.length - 1)];
          if (!row) return null;
          return (
            <text key={`dx-${offset}`} x={x(offset)}
                  y={shareBottomY + 16} fontSize="10" fill="var(--muted)"
                  textAnchor="middle">{row.trade_date.slice(2, 6)}</text>
          );
        })}

        {/* ---------- 十字轴：竖线吸附交易日、横线吸附最近曲线 ---------- */}
        {cursor && (
          <g className="crowding-crosshair" pointerEvents="none">
            {/* 竖线：贯穿两个面板，顶/底部各放一个日期标签 */}
            <line x1={cursor.x} x2={cursor.x} y1={waterTopY}
                  y2={shareBottomY} stroke="var(--muted)"
                  strokeDasharray="3 3" opacity={0.65} />
            <text x={cursor.x} y={waterTopY - 4} fontSize="10"
                  fill="var(--text)" textAnchor="middle"
                  className="crowding-crosshair-date">
              {series[cursor.index]?.trade_date ?? ""}
            </text>
            {/* 横线：只在鼠标所在面板内画 + 右侧显示该线读数 */}
            <line x1={PAD.left} x2={W - PAD.right} y1={cursor.y} y2={cursor.y}
                  stroke="var(--muted)" strokeDasharray="3 3" opacity={0.65} />
            <rect x={W - PAD.right + 2} y={cursor.y - 8} width={PAD.right - 4}
                  height={16} rx={3} className="crowding-crosshair-tag" />
            <text x={W - PAD.right + 6} y={cursor.y + 4} fontSize="10"
                  fill="var(--text)" className="mono">
              {readout}
            </text>
            {/* 吸附点：明确"这个读数是哪条线上哪一天的值" */}
            {cursor.snapped !== null && (
              <circle cx={cursor.x} cy={cursor.y} r={3.5}
                      fill="none" stroke="var(--text)" strokeWidth="1.3" />
            )}
          </g>
        )}
      </svg>

      <div className="crowding-hover mono">
        <span className="muted-text">{hoverRow ? "十字轴" : "窗口末"}</span>
        <span>{(hoverRow ?? last).trade_date}</span>
        {cursor && (
          <span className="crowding-crosshair-readout">
            {cursor.snappedLabel
              ? `${cursor.snappedLabel} ${readout}`
              : "该线数据不足"}
          </span>
        )}
        <span style={{ color: WATER_COLOR }}>
          水位 {waterPct((hoverRow ?? last).water_level)}
        </span>
        <span style={{ color: MA_COLOR }}>
          MA5 {(hoverRow ?? last).ma5_crowding === null
            ? "—" : `${((hoverRow ?? last).ma5_crowding! * 100).toFixed(3)}%`}
        </span>
        <span style={{ color: RAW_COLOR }}>
          原始 {(hoverRow ?? last).raw_crowding === null
            ? "—" : `${((hoverRow ?? last).raw_crowding! * 100).toFixed(3)}%`}
        </span>
        <span className="muted-text">
          板块成交额 {(((hoverRow ?? last).sector_amount ?? 0) / 1e8).toFixed(0)}亿
        </span>
      </div>
    </div>
  );
}
