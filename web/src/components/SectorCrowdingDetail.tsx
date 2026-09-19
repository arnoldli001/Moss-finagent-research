import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  sectorCrowdingApi,
  type CrowdingDetail as DetailPayload,
  type CrowdingRow,
} from "../sectorCrowdingApi";

/**
 * 单板块详情：近 6 年拥挤度曲线 + 水位曲线（双面板共享时间轴）。
 *
 * ## 为什么两条曲线要分开画
 *
 * 拥挤度是**绝对占比**（可能千分之几），水位是**相对自身历史的比例**（0~100%）。
 * 叠在一张图上必然一条被压成直线。拆成上下两个面板、共享 X 轴，
 * 才能看出"占比在涨、但还没到自己历史高位"这类关键区别。
 *
 * ## 为什么水位线要画 80%/90% 参考线
 *
 * 用户是拿它做决策的（"到没到该警惕的位置"）。没有参考线就得自己心算比例。
 */

const W = 960;
const PANEL_H = 150;
const GAP = 26;
const PAD = { top: 14, right: 70, bottom: 34, left: 64 };

const WATER_COLOR = "#e0b020";
const RAW_COLOR = "#4a9eff";
const MA_COLOR = "#2ea86e";
const HIGH_COLOR = "#d95c4a";

type Hover = { index: number } | null;

export default function SectorCrowdingDetail({
  sectorCode, sectorName, onNotice,
}: {
  sectorCode: string;
  sectorName: string;
  onNotice?: (text: string) => void;
}) {
  const [data, setData] = useState<DetailPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [hover, setHover] = useState<Hover>(null);
  const [showRaw, setShowRaw] = useState(true);
  const svgRef = useRef<SVGSVGElement | null>(null);

  useEffect(() => {
    if (!sectorCode) { setData(null); return; }
    let alive = true;
    setLoading(true);
    setError("");
    sectorCrowdingApi.detail(sectorCode)
      .then((payload) => { if (alive) setData(payload); })
      .catch((exc) => {
        if (alive) setError(exc instanceof Error ? exc.message : String(exc));
      })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [sectorCode]);

  const series = useMemo<CrowdingRow[]>(() => data?.series ?? [], [data]);

  const domains = useMemo(() => {
    if (series.length === 0) return null;
    const waters = series.map((row) => row.water_level)
      .filter((value): value is number => value !== null && Number.isFinite(value));
    const raws = series.map((row) => row.raw_crowding)
      .filter((value): value is number => value !== null && Number.isFinite(value));
    const mas = series.map((row) => row.ma5_crowding)
      .filter((value): value is number => value !== null && Number.isFinite(value));
    const waterTop = Math.max(1, Math.min(1.4, (Math.max(0, ...waters) || 0.5) * 1.1));
    const shareTop = Math.max(1e-6, Math.max(0, ...raws, ...mas) * 1.15);
    return { waterTop, shareTop };
  }, [series]);

  const plotW = W - PAD.left - PAD.right;
  const waterTopY = PAD.top;
  const waterBottomY = PAD.top + PANEL_H;
  const shareTopY = waterBottomY + GAP;
  const shareBottomY = shareTopY + PANEL_H;

  const x = useCallback((index: number) => PAD.left
    + (series.length <= 1 ? 0.5 : index / (series.length - 1)) * plotW,
    [series.length, plotW]);
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
    series.forEach((row, index) => {
      const value = pick(row);
      if (value === null || !Number.isFinite(value)) { open = false; return; }
      parts.push(`${open ? "L" : "M"}${x(index).toFixed(1)},`
        + `${yScale(value).toFixed(1)}`);
      open = true;
    });
    return parts.join(" ");
  }, [series, x, yWater, yShare]);

  const indexAt = (clientX: number): number | null => {
    const node = svgRef.current;
    if (!node || series.length === 0) return null;
    const rect = node.getBoundingClientRect();
    const ratio = ((clientX - rect.left) / rect.width * W - PAD.left) / plotW;
    const index = Math.round(Math.min(1, Math.max(0, ratio)) * (series.length - 1));
    return index >= 0 && index < series.length ? index : null;
  };

  if (!sectorCode) {
    return (
      <div className="info-box">
        在下方输入板块名称并选中，或点告警列表里的板块名，即可查看它的近 6 年
        拥挤度与水位曲线。
      </div>
    );
  }
  if (loading) return <div className="info-box">正在读取 {sectorName || sectorCode} …</div>;
  if (error) return <div className="error-box">读取失败：{error}</div>;
  if (!data || series.length === 0) {
    return (
      <div className="warn-box">
        {sectorName || sectorCode} 还没有拥挤度数据 ——
        点顶部「一键刷新」后它才会被回填（首次全量约几分钟）。
      </div>
    );
  }

  const hoverRow = hover ? series[hover.index] : null;
  const last = series[series.length - 1];
  const waterPct = (value: number | null | undefined) =>
    value === null || value === undefined ? "—" : `${(value * 100).toFixed(1)}%`;

  return (
    <div className="crowding-detail">
      <div className="chart-toolbar">
        <b>{data.sector_name || sectorCode}</b>
        <span className="mono muted-text">{sectorCode}</span>
        <span className="muted-text">
          {data.first_trade_date} ~ {data.last_trade_date}（{data.bars} 根）
        </span>
        <span className={data.water_level !== null
          && data.water_level >= data.threshold ? "crowding-water tone-warn"
          : "crowding-water"}>
          当前水位 {waterPct(data.water_level)}
        </span>
        <span className="muted-text">
          历史最高 {waterPct(data.max_water_level)}
        </span>
        <span style={{ flex: 1 }} />
        <label className="chart-toggle">
          <input type="checkbox" checked={showRaw}
                 onChange={(event) => setShowRaw(event.target.checked)} />
          原始占比
        </label>
        <button className="btn-ghost"
                onClick={() => {
                  void sectorCrowdingApi
                    .addWatch(sectorCode, data.sector_name)
                    .then(() => onNotice?.(
                      `已加入自选池：${data.sector_name || sectorCode}`))
                    .catch((exc) => onNotice?.(
                      `加入自选池失败：${exc instanceof Error
                        ? exc.message : String(exc)}`));
                }}>加入自选池</button>
      </div>

      <svg ref={svgRef} viewBox={`0 0 ${W} ${shareBottomY + PAD.bottom}`}
           className="intraday-svg" role="img"
           onMouseMove={(event) => {
             const index = indexAt(event.clientX);
             if (index !== null) setHover({ index });
           }}
           onMouseLeave={() => setHover(null)}>
        {/* ---------- 上：水位 ---------- */}
        <text x={PAD.left} y={waterTopY - 2} fontSize="10" fill="var(--muted)">
          拥挤度水位（当前平滑拥挤度 / 近 6 年最高值）
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
                {(value * 100).toFixed(0)}%
              </text>
            </g>
          );
        })}
        <line x1={PAD.left} x2={W - PAD.right} y1={yWater(data.threshold)}
              y2={yWater(data.threshold)} stroke={WATER_COLOR}
              strokeDasharray="6 3" strokeWidth="1.2" />
        <text x={W - PAD.right + 4} y={yWater(data.threshold) + 4}
              fontSize="10" fill={WATER_COLOR}>
          {(data.threshold * 100).toFixed(0)}%
        </text>
        <line x1={PAD.left} x2={W - PAD.right} y1={yWater(0.9)}
              y2={yWater(0.9)} stroke={HIGH_COLOR} strokeDasharray="6 3"
              strokeWidth="1.2" />
        <text x={W - PAD.right + 4} y={yWater(0.9) + 4} fontSize="10"
              fill={HIGH_COLOR}>90%</text>
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

        {/* X 轴日期（约 6 个刻度） */}
        {Array.from({ length: 6 }, (_, index) =>
          Math.round((series.length - 1) * index / 5)).map((position) => {
          const row = series[Math.min(position, series.length - 1)];
          if (!row) return null;
          return (
            <text key={`dx-${position}`} x={x(position)}
                  y={shareBottomY + 16} fontSize="10" fill="var(--muted)"
                  textAnchor="middle">{row.trade_date.slice(2, 6)}</text>
          );
        })}

        {hover && (
          <line x1={x(hover.index)} x2={x(hover.index)} y1={waterTopY}
                y2={shareBottomY} stroke="var(--muted)" strokeDasharray="3 3"
                opacity={0.6} />
        )}
      </svg>

      <div className="crowding-hover mono">
        <span className="muted-text">{hoverRow ? "光标" : "最新"}</span>
        <span>{(hoverRow ?? last).trade_date}</span>
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
