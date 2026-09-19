import { useCallback, useMemo, useRef, useState } from "react";
import type { CrowdingOverviewRow } from "../sectorCrowdingApi";

/**
 * 全板块水位总览（散点图）。
 *
 * ## 为什么是散点而不是柱状/排行
 *
 * 要回答的问题是"**哪些板块同时处于历史高位**"。2500 个板块排成柱状图没法看，
 * 而散点（X = 成交额占比、Y = 水位）能一眼分出四个象限：
 * 右上 = 又大又挤（最该警惕）、左上 = 小但极挤（可能是新热点）。
 *
 * ## 交互
 *
 * - 滚轮在 X 轴方向缩放（时间无关，这里缩放的是成交额占比轴）、拖拽平移；
 * - 悬停显示板块名/水位/成交额占比/更新日；
 * - 点圆点 → 打开详情；
 * - 80% / 90% 两条水平参考线（与告警面板同一口径）。
 */

const W = 960;
const H = 340;
const PAD = { top: 16, right: 74, bottom: 36, left: 62 };

const THRESHOLD_COLOR = "#e0b020";
const HIGH_COLOR = "#d95c4a";
const NORMAL_COLOR = "#4a9eff";

function toYi(value: number | null | undefined): number | null {
  if (value === null || value === undefined || !Number.isFinite(value)) return null;
  return value / 1e8;
}

export default function SectorCrowdingOverview({
  rows, threshold, highThreshold, onPick,
}: {
  rows: CrowdingOverviewRow[];
  threshold: number;
  highThreshold: number;
  onPick: (code: string, name: string) => void;
}) {
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState(0);
  const [hover, setHover] = useState<CrowdingOverviewRow | null>(null);
  const svgRef = useRef<SVGSVGElement | null>(null);
  const dragRef = useRef<{ x: number; pan: number } | null>(null);

  /** 只画有水位的点：NULL 的点画在 Y 轴上没有位置，"数据不足"另用文字说明。 */
  const points = useMemo(() => rows
    .filter((row) => row.water_level !== null
      && Number.isFinite(row.water_level as number)
      && row.raw_crowding !== null && Number.isFinite(row.raw_crowding as number))
    .map((row) => ({
      row,
      amount: toYi(row.sector_amount) ?? 0,
      water: (row.water_level as number) * 100,
    })), [rows]);

  const missing = rows.length - points.length;

  const xDomain = useMemo(() => {
    if (points.length === 0) return { min: 0, max: 1 };
    const values = points.map((item) => item.amount).sort((a, b) => a - b);
    const max = Math.max(1, values[values.length - 1]);
    const full = { min: 0, max };
    // 缩放：以 max 为基准收窄可见区间（X 轴从 0 起，便于比较"占全市场多少"）
    const visible = full.max / zoom;
    const start = Math.min(Math.max(0, pan * full.max), Math.max(0, full.max - visible));
    return { min: start, max: start + visible };
  }, [points, zoom, pan]);

  const yDomain = useMemo(() => {
    // Y 轴固定 0~100%+：水位是比例量，固定量纲才能跨刷新对比
    const maxWater = points.length
      ? Math.max(100, ...points.map((item) => item.water)) : 100;
    return { min: 0, max: Math.min(140, Math.ceil(maxWater / 10) * 10) };
  }, [points]);

  const x = useCallback((amount: number) =>
    PAD.left + ((amount - xDomain.min) / Math.max(1e-9, xDomain.max - xDomain.min))
    * (W - PAD.left - PAD.right), [xDomain]);
  const y = useCallback((water: number) =>
    PAD.top + (1 - (water - yDomain.min)
      / Math.max(1e-9, yDomain.max - yDomain.min))
    * (H - PAD.top - PAD.bottom), [yDomain]);

  const onWheel = useCallback((event: React.WheelEvent<SVGSVGElement>) => {
    event.preventDefault();
    setZoom((current) => {
      const next = event.deltaY > 0 ? current * 1.2 : current / 1.2;
      return Math.min(20, Math.max(1, next));
    });
  }, []);

  const yTicks = useMemo(() => Array.from({ length: 6 }, (_, index) =>
    yDomain.min + ((yDomain.max - yDomain.min) * index) / 5), [yDomain]);
  const xTicks = useMemo(() => Array.from({ length: 6 }, (_, index) =>
    xDomain.min + ((xDomain.max - xDomain.min) * index) / 5), [xDomain]);

  if (rows.length === 0) {
    return (
      <div className="info-box">
        还没有拥挤度数据。点上方「一键刷新全部板块拥挤度」开始回填
        （首次约几分钟，之后都是增量）。
      </div>
    );
  }

  return (
    <div className="crowding-overview">
      <div className="chart-toolbar">
        <span className="muted-text">
          滚轮缩放 X 轴 · 拖拽平移 · 点圆点看详情
        </span>
        <button className="btn-ghost chart-btn"
                onClick={() => setZoom((value) => Math.min(20, value * 1.3))}>＋</button>
        <button className="btn-ghost chart-btn"
                onClick={() => setZoom((value) => Math.max(1, value / 1.3))}>－</button>
        <button className="btn-ghost chart-btn"
                onClick={() => { setZoom(1); setPan(0); }}
                disabled={zoom === 1 && pan === 0}>复位</button>
        <span className="muted-text">
          共 {points.length} 个板块有水位
          {missing > 0 && ` · ${missing} 个数据不足（未刷新或样本 < 60 根）`}
        </span>
      </div>

      <svg ref={svgRef} viewBox={`0 0 ${W} ${H}`} className="intraday-svg"
           role="img"
           onWheel={onWheel}
           onMouseDown={(event) => {
             dragRef.current = { x: event.clientX, pan };
           }}
           onMouseMove={(event) => {
             const drag = dragRef.current;
             if (!drag) return;
             const rect = svgRef.current?.getBoundingClientRect();
             if (!rect) return;
             const delta = (drag.x - event.clientX) / rect.width;
             setPan(Math.max(0, drag.pan + delta * (xDomain.max - xDomain.min)
               / Math.max(1e-9, xDomain.max || 1)));
           }}
           onMouseUp={() => { dragRef.current = null; }}
           onMouseLeave={() => { dragRef.current = null; setHover(null); }}
           onDoubleClick={() => { setZoom(1); setPan(0); }}>
        {/* Y 网格 + 百分比刻度 */}
        {yTicks.map((value) => (
          <g key={`y-${value}`}>
            <line x1={PAD.left} x2={W - PAD.right} y1={y(value)} y2={y(value)}
                  stroke="var(--border)" strokeDasharray="3 4" opacity={0.6} />
            <text x={PAD.left - 6} y={y(value) + 4} fontSize="10"
                  fill="var(--muted)" textAnchor="end">{value.toFixed(0)}%</text>
          </g>
        ))}
        {/* X 刻度：成交额（亿元） */}
        {xTicks.map((value) => (
          <text key={`x-${value}`} x={x(value)} y={H - PAD.bottom + 16}
                fontSize="10" fill="var(--muted)" textAnchor="middle">
            {value.toFixed(0)}亿
          </text>
        ))}
        <text x={W - PAD.right + 6} y={PAD.top + 10} fontSize="10"
              fill="var(--muted)">水位</text>

        {/* 阈值参考线 */}
        <line x1={PAD.left} x2={W - PAD.right} y1={y(threshold * 100)}
              y2={y(threshold * 100)} stroke={THRESHOLD_COLOR}
              strokeDasharray="6 3" strokeWidth="1.2" />
        <text x={PAD.left + 4} y={y(threshold * 100) - 4} fontSize="10"
              fill={THRESHOLD_COLOR}>告警线 {(threshold * 100).toFixed(0)}%</text>
        <line x1={PAD.left} x2={W - PAD.right} y1={y(highThreshold * 100)}
              y2={y(highThreshold * 100)} stroke={HIGH_COLOR}
              strokeDasharray="6 3" strokeWidth="1.2" />
        <text x={PAD.left + 4} y={y(highThreshold * 100) - 4} fontSize="10"
              fill={HIGH_COLOR}>高危线 {(highThreshold * 100).toFixed(0)}%</text>

        {points.map((item) => {
          const water = item.water;
          const color = water >= highThreshold * 100 ? HIGH_COLOR
            : water >= threshold * 100 ? THRESHOLD_COLOR : NORMAL_COLOR;
          const active = hover?.sector_code === item.row.sector_code;
          return (
            <circle key={item.row.sector_code}
                    cx={x(item.amount)} cy={y(water)}
                    r={active ? 6 : 3.5} fill={color}
                    opacity={active ? 1 : 0.75}
                    style={{ cursor: "pointer" }}
                    onMouseEnter={() => setHover(item.row)}
                    onClick={() => onPick(item.row.sector_code, item.row.sector_name)}>
              <title>
                {`${item.row.sector_name}\n水位 ${water.toFixed(1)}%`
                  + `\n当前平滑拥挤度 ${item.row.ma5_crowding === null
                    ? "—" : (item.row.ma5_crowding * 100).toFixed(3)}`
                  + `\n成交额 ${item.amount.toFixed(0)}亿`
                  + `\n更新 ${item.row.trade_date}`}
              </title>
            </circle>
          );
        })}

        {hover && (
          <g>
            <circle cx={x(toYi(hover.sector_amount) ?? 0)}
                    cy={y((hover.water_level ?? 0) * 100)} r={8}
                    fill="none" stroke="var(--text)" strokeWidth="1" opacity={0.8} />
          </g>
        )}
      </svg>

      {hover && (
        <div className="crowding-hover mono">
          <b>{hover.sector_name}</b>
          <span>水位 {((hover.water_level ?? 0) * 100).toFixed(1)}%</span>
          <span>平滑 {hover.ma5_crowding === null
            ? "—" : (hover.ma5_crowding * 100).toFixed(3)}</span>
          <span>成交额 {(toYi(hover.sector_amount) ?? 0).toFixed(0)}亿</span>
          <span className="muted-text">更新 {hover.trade_date}</span>
        </div>
      )}
    </div>
  );
}
