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

type Hover = {
  minute: number; price: number; ts: string; avg: number | null;
  /** 十字光标横线对应的价格（鼠标 Y 位置）——与吸附到数据点的 price 不同。 */
  cursorPrice: number;
} | null;

/** 档位线的键（用于显示开关与图例）。 */
type LevelKey = "high_sell" | "low_buy" | "stop_loss" | "boll";

const LEVEL_META: Record<LevelKey, { label: string; color: string; cls: string }> = {
  high_sell: { label: "冲高", color: "#d95c4a", cls: "lv-sell" },
  low_buy: { label: "回踩", color: "#2ea86e", cls: "lv-buy" },
  stop_loss: { label: "止损", color: "#e0b020", cls: "lv-stop" },
  boll: { label: "布林", color: "#4a9eff", cls: "lv-boll" },
};

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
  /**
   * 档位线的显示开关：默认全开。
   *
   * 为什么给开关：Y轴是按"可执行档位"伸缩的（见 domain 里的自适应护栏），
   * 止损线有时离现价很远（高波动股 ATR 口径下 4~5%），纳入定标会把分时线压扁。
   * 给用户一个「只看回踩冲高 / 连止损一起看」的选择，比替他决定更诚实。
   */
  const [showLevels, setShowLevels] = useState<Record<LevelKey, boolean>>(
    { high_sell: true, low_buy: true, stop_loss: true, boll: true });
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

  /**
   * 关键价位的**当前值** + 每条线的显示开关。
   *
   * ⚠️ 这里刻意用 `levels`（快照当前值）而不是 `levelSeries` 的末值：
   * `levels` 与分时点来自同一次快照，两者**同一时刻**；而 `level_series` 是
   * 5 分钟粒度、在 `pre_open`/`lunch_break`/`closed` 时可能整段缺失或停在上一根 bar，
   * 用它的末值会在图上画出一条"与价格线不同时刻"的档位线（用户看到的就是错位）。
   */
  const allLevels: { key: LevelKey; price: number; label: string; cls: string;
                     color: string }[] = [];
  if (levels) {
    const push = (key: LevelKey, value: number | null | undefined) => {
      if (typeof value === "number" && Number.isFinite(value)) {
        const meta = LEVEL_META[key];
        allLevels.push({ key, price: value, label: meta.label,
                         cls: meta.cls, color: meta.color });
      }
    };
    push("high_sell", levels.high_sell);
    push("low_buy", levels.low_buy);
    push("stop_loss", levels.stop_loss);
    push("boll", levels.boll_lower ?? null);
    push("boll", levels.boll_upper ?? null);
  }
  const visibleLevelLines = allLevels.filter((line) => showLevels[line.key]);

  // 漂移曲线在可见窗口内的价格范围（用于把档位线纳入Y轴定标）
  const driftRange = useMemo(() => {
    if (!levelSeries || !levelSeries.length) return null;
    let min = Infinity;
    let max = -Infinity;
    levelSeries.forEach((row) => {
      const minute = sessionMinute(row.ts);
      if (minute === null || minute < view.start || minute > view.end) return;
      [row.low_buy, row.high_sell, row.stop_loss].forEach((value) => {
        if (!Number.isFinite(value)) return;
        min = Math.min(min, value);
        max = Math.max(max, value);
      });
    });
    return min <= max ? { min, max } : null;
  }, [levelSeries, view]);

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
    // ★ Y 轴范围：**动态取"当天所有相关数值"的极值**（用户口径 2026-09-24，
    //   原话："分时图的 Y 坐标范围动态设置为（当天盘中股价最高值和最低值、
    //   冲高、回踩、止损、布林线）这些数值的最大值*1.015 和最小值*0.985"）。
    //
    //   与旧实现的三点差别（都是有意的）：
    //     ① **不再以昨收为中心对称展开** —— 改成按真实极值取区间，
    //        于是"股价在昨收上方运行"时，下方不会白白留一半空间；
    //     ② **档位与布林线无条件纳入**（回踩/冲高/止损/布林上下轨），
    //        不再有 `1.6` / `NEAR_MISS` 那套"够得着才纳入"的折中 ——
    //        用户明确要求它们必须在轴内，代价是极端行情下分时线会被压扁一点；
    //     ③ 留白从 `×1.08` 对称缩放改成 **上 ×1.015 / 下 ×0.985**（按用户给的公式）。
    //
    //   连带效果：`outViewLevels`（贴边"↑在图上方/↓在图下方"指示）基本不会再触发 ——
    //   档位既然总在轴内，就没有"图外"可标。那段代码保留（防数据异常），不删。
    let dayMin = Math.min(...prices);
    let dayMax = Math.max(...prices);
    /* ⚠️ **箱体上下沿不纳入**（用户口径里的清单本来就没有它）：
       实测（2026-09-24 截图）主板票 昨收 86.36，箱体下沿落到 ~70 一带，
       轴被拉到 -19.68% —— 而主板单日跌幅上限就是 -10%，那段空间**永远到不了**，
       于是止损线到量能柱之间出现一大片空白。清单核准为：
       当天分时价/均价 + 冲高 + 回踩 + 止损 + 布林上下轨。 */
    [levels?.high_sell, levels?.low_buy, levels?.stop_loss,
     levels?.boll_upper, levels?.boll_lower]
      .forEach((value) => {
        if (typeof value === "number" && Number.isFinite(value)) {
          dayMin = Math.min(dayMin, value);
          dayMax = Math.max(dayMax, value);
        }
      });
    // 兜底：极窄幅或数据异常时给一个下限，避免"一条直线 + 除零"
    if (!Number.isFinite(dayMin) || !Number.isFinite(dayMax)
        || dayMax - dayMin < 1e-6) {
      const base = Number.isFinite(dayMax) ? dayMax : (prevClose ?? 1);
      dayMin = base * 0.985;
      dayMax = base * 1.015;
    }
    /* ★ 再夹一层**涨跌幅限制带**：主板 ±10%、创业板/科创 ±20%、北交所 ±30%。
       为什么必须夹：把所有档位无条件纳入之后，只要有一条档位落在很远处
       （箱体/布林在极端行情下会离谱），Y 轴就会出现"永远到不了"的区间 ——
       表现就是用户截图里的"止损线下面一大片空白 + -19.68%"。
       夹完之后，超出的档位会走**已有的"贴边指示"分支**（↑在图上方/↓在图下方），
       既不浪费图面，也不丢信息。 */
    const limitPct = (() => {
      const code = String((series as { code?: string }).code || "");
      if (/^(30|68)/.test(code)) return 0.20;          // 创业板 / 科创板
      if (/^(4|8|92)/.test(code)) return 0.30;         // 北交所
      // 拿不到代码时的兜底：**看当日实际振幅** —— 主板不可能超过 10%，
      // 一旦超过，说明这不是主板（或数据异常），放宽到 20% 更安全。
      const reach = prevClose
        ? Math.max(Math.abs(prevClose - dayMin), Math.abs(dayMax - prevClose))
          / prevClose : 0;
      return reach > 0.105 ? 0.20 : 0.10;
    })();
    if (prevClose) {
      const bandLo = prevClose * (1 - limitPct);
      const bandHi = prevClose * (1 + limitPct);
      dayMin = Math.max(dayMin, bandLo);
      dayMax = Math.min(dayMax, bandHi);
    }
    const lo = dayMin * 0.985;
    const hi = dayMax * 1.015;
    return { min: lo, max: hi, span: hi - lo, centered: false as const };
  }, [visible, series.boardLines, view, prevClose, levels, fitLevels,
      visibleLevelLines, driftRange, spanMinutes]);

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

  /**
   * 逐bar档位**漂移曲线**：把 low_buy / high_sell / stop_loss 画成随时间变化的线。
   *
   * 档位是**时刻量**（每分钟随 VWAP/布林重算）：实测 300308 当日回踩线从 862 抬到
   * 898、603083 从 215.42 抬到 221.91。画成横贯全天的直线会让人误以为"开盘就在
   * 回踩线以下"（那只是当前值的错觉），也会把后来的高位止损误读成早盘就该止损。
   *
   * `minute` 与 `price` 都必须过滤成有限数字：`x(null)` → NaN 会让整条 `d`
   * 变成 `MNaN,NaN`，浏览器会**静默丢弃**该 path —— 表现出来就是"档位线根本不画"。
   */
  const driftPaths = useMemo(() => {
    const result: Partial<Record<LevelKey, string>> = {};
    if (!levelSeries || levelSeries.length < 2) return result;
    const build = (pick: (row: IntradayLevelPoint) => number) => {
      const pts = levelSeries
        .map((row) => ({ minute: sessionMinute(row.ts), price: pick(row) }))
        .filter((p): p is { minute: number; price: number } =>
          p.minute !== null && Number.isFinite(p.price));
      return pts.length > 1 ? path(pts) : "";
    };
    const pickers: Record<string, (row: IntradayLevelPoint) => number> = {
      high_sell: (row) => row.high_sell,
      low_buy: (row) => row.low_buy,
      stop_loss: (row) => row.stop_loss,
    };
    Object.keys(pickers).forEach((key) => {
      const built = build(pickers[key]);
      if (built) result[key as LevelKey] = built;
    });
    return result;
    // path() 每次渲染都是新引用，但它的取值只由 view/domain 决定 ——
    // 依赖里显式写出 view/domain，语义等价且不会漏重算。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [levelSeries, view, domain]);

  /** 落在可见 Y 范围内 / 范围外的档位（范围外的转到下方清单并标注原因）。 */
  const inViewLevels = domain
    ? visibleLevelLines.filter((l) => l.price >= domain.min && l.price <= domain.max)
    : [];
  const outViewLevels = domain
    ? visibleLevelLines.filter((l) => l.price < domain.min || l.price > domain.max)
    : [];
  const lastPrice = visible.length ? visible[visible.length - 1].price
    : (quote?.price ?? null);


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

  /**
   * 鼠标 Y 位置 → 价格（供十字光标的**横线**用）。
   *
   * 为什么横线要跟鼠标而不是吸附到数据点：用户要的是"我指的这个价位是多少、
   * 它离回踩/冲高线差多少"，横线粘在 1 分钟数据点上就失去这个能力。
   * 纵线仍吸附到最近的分钟点（读时间与均价用），两者各司其职。
   */
  const priceAt = (clientY: number): number | null => {
    const node = svgRef.current;
    if (!node || !domain) return null;
    const rect = node.getBoundingClientRect();
    const viewY = (clientY - rect.top) / rect.height * H;
    const ratio = (H - PAD.bottom - viewY) / HEIGHT;
    return domain.min + Math.min(1, Math.max(0, ratio)) * domain.span;
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
    const cursorPrice = priceAt(event.clientY);
    setHover({
      minute: nearest.minute, price: nearest.price, ts: nearest.ts,
      avg: nearest.avg_price,
      cursorPrice: cursorPrice === null ? nearest.price : cursorPrice,
    });
  };

  const handleDown = (event: React.MouseEvent<SVGSVGElement>) => {
    const minute = minuteAt(event.clientX);
    if (minute === null) return;
    dragRef.current = { minute, start: view.start, end: view.end };
  };
  const endDrag = () => { dragRef.current = null; };

  if (!domain || !visible.length) {
    /* ⚠️ 这句原来写的是"分时数据不可用（数据源缺口）"——**把两种完全不同的情况
       混成一句**，实测误导：用户点两次「＋」放大后，窗口被缩到当天**还没走到的时段**
       （10:24 的数据，窗口却落在 61~179 分钟），`visible` 为空 → 走到这里 →
       界面说"数据源缺口"，于是去查数据健康度，而数据其实是好的。
       现在按「窗口内没有数据」优先说，并给出可操作的出口（双击复位）。 */
    return (
      <div className="empty-tip muted-text">
        当前时间窗内没有分时数据 —— 可能是放大/平移到了当天尚未走到的时段，
        双击图面可复位到全天；若全天也无数据才是数据源缺口（见下方数据健康度）
      </div>
    );
  }

  /* ★ 放大锚点必须取**数据所在的中间时刻**，不能取"窗口中点"。
     实测（用户报障 2026-09-24）：全天窗口中点 = 第 120 分钟（午盘），
     而当天 10:24 时数据只到第 54 分钟 —— 从 240 分钟连续放大两次（×0.7、×0.7）
     得到 [61, 179] 分钟，整段落在**还没发生的时段**里，`visible` 直接为空。
     锚在数据的中间时刻则永远"缩向已有数据"。 */
  const dataFocus = visible[Math.floor((visible.length - 1) / 2)].minute;

  const pctOf = (price: number) =>
    prevClose ? ((price / prevClose) - 1) * 100 : null;
  const zoomed = spanMinutes < MINUTES_TOTAL - 0.5;

  return (
    <div className="intraday-chart-wrap">
      <div className="chart-toolbar">
        <span className="muted-text">
          滚轮缩放 · 拖拽平移 · 双击复位
        </span>
        <button className="btn-ghost chart-btn" onClick={() => zoomAt(0.7, dataFocus)}
                title="放大时间轴（Y轴自动跟随）">＋</button>
        <button className="btn-ghost chart-btn" onClick={() => zoomAt(1.4, dataFocus)}
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

        {/* 成交量（底部小柱，只按可见区间定标）
            ★ 红绿着色（用户口径 2026-09-24："分时线内的量能柱要做成红绿色，
              和股票软件里的一样的颜色效果"）。
            股票软件的惯例是**按这一分钟相对上一分钟的价格方向**着色：
              · 价涨 → 红（#e06a5a，与 K 线/分时线的"红涨"同一支色）
              · 价跌 → 绿（#2ea86e）
              · 持平 / 首根 → 灰（不硬套颜色，避免"没动也显示红绿"）
            用 `p.price` 而不是收盘价序列：分时点本身就是"这一分钟的价"，
            相邻两点比较即得方向，与券商分时界面口径一致。 */}
        {visible.map((point, i) => {
          const barHeight = ((point.volume || 0) / volumeMax) * 36;
          const prev = i > 0 ? visible[i - 1] : null;
          const dir = prev === null || prev.price === point.price
            ? 0 : (point.price > prev.price ? 1 : -1);
          const color = dir > 0 ? "#e06a5a" : dir < 0 ? "#2ea86e" : "#8b98a5";
          return (
            <rect key={i} x={x(point.minute) - 1.2} width={2.4}
                  y={H - PAD.bottom - barHeight} height={barHeight}
                  fill={color} opacity={dir === 0 ? 0.45 : 0.85}>
              <title>{`${timeLabel(point.minute)} 量 ${Math.round(point.volume || 0)}`
                      + (dir === 0 ? "" : dir > 0 ? "（价涨）" : "（价跌）")}</title>
            </rect>
          );
        })}

        {/* 关联板块（换算为等价价位，虚线） */}
        {series.boardLines.map((board, index) => (
          <path key={board.name} d={path(board.points)} fill="none"
                stroke={index === 0 ? "var(--medium)" : "var(--accent)"}
                strokeWidth="1.3" strokeDasharray="6 4" opacity={0.75} />
        ))}

        {/* 关键价位线（**只画可见范围内的**）。
            有逐bar档位序列时画**随时间漂移的曲线**，而不是一条横贯全天的直线 ——
            档位（回踩/冲高/止损）是每分钟随 VWAP/布林重算的时刻量：
            实测 300308 当日回踩线从 862 抬到 898、603083 从 215.42 抬到 221.91。
            画成横线会让人以为"开盘就在回踩线以下"（那只是当前值的错觉），
            也会让人把后来的高位止损误读成早盘就该止损。

            ⚠️ stroke 必须**显式内联**：早期版本只给了 className，
            而 CSS 里只有 `.lv-sell line {}`（元素选择器）—— 对 `<path>` 无效，
            于是三条档位曲线全部按 SVG 默认的 `stroke:none` 渲染 = 图上看不见。
            这正是"回踩/冲高/止损虚线没画出来"的直接原因之一。 */}
        {(["high_sell", "low_buy", "stop_loss"] as LevelKey[]).map((key) => {
          const d = driftPaths[key];
          if (!d || !showLevels[key]) return null;
          const meta = LEVEL_META[key];
          return (
            <path key={`drift-${key}`} d={d} fill="none" stroke={meta.color}
                  strokeWidth="1.5" strokeDasharray="7 3" opacity={0.95}>
              <title>{`${meta.label}线（随时间漂移）`}</title>
            </path>
          );
        })}
        {/* 档位**当前值**参考线（回踩/冲高/止损）。
            与上面的漂移曲线并存：曲线回答"这条线今天怎么走的"，参考线回答
            "此刻它在哪" —— 交易软件（同花顺/东财）也是这两个一起给的。

            ⚠️ stroke 必须**显式给**：早期版本只写了 className，
            而 CSS 里只有 `.lv-sell line {}`（元素选择器）—— 对 `<path>` 无效，
            于是档位线全部按 SVG 默认 `stroke:none` 渲染，图上一个点都看不到。
            「回踩/冲高/止损虚线没画出来」就是这个原因。 */}
        {(() => {
          /* ★ 标签**纵向去重叠**（用户口径 2026-09-24："止损文字提示和回踩提示
             重叠了，需要左右错开"）。
             根因：所有档位标签原本都画在同一个 x（PAD.left + 4），y 只跟价位走；
             回踩/止损价位常常很接近，两条标签就直接叠在一起，谁也读不出。
             修法：按 y 从上到下排一遍，凡是与已放置标签距离 < 12px 的就往上顶 12px —— 
             这是图表标注的常规做法，比"固定左右各放一半"更稳（档位数量会变）。 */
          const rows = inViewLevels
            .map((line) => ({ line, ly: y(line.price) - 4 }))
            .sort((a, b) => a.ly - b.ly);
          const placed: number[] = [];
          for (const row of rows) {
            while (placed.some((py) => Math.abs(py - row.ly) < 12)) row.ly -= 12;
            placed.push(row.ly);
          }
          return rows.map(({ line, ly }) => (
            <g key={`flat-${line.key}-${line.price}`}>
              <line x1={PAD.left} x2={W - PAD.right} y1={y(line.price)}
                    y2={y(line.price)} stroke={line.color} strokeWidth="1.5"
                    strokeDasharray="7 3" opacity={0.95}>
                <title>{`${line.label}线 ${line.price.toFixed(2)}（当前值）`}</title>
              </line>
              <text x={PAD.left + 4} y={ly} fontSize="10" fill={line.color}>
                {line.label} {line.price.toFixed(2)}
              </text>
            </g>
          ));
        })()}
        {/* 档位线**图外**时的贴边指示：一条贴在上下边缘的箭头线 + 数值。
            这比"什么都不画"诚实得多 —— 用户至少知道止损位在哪一侧、离多远，
            点一下档位清单里的开关就能把它真正画进来。 */}
        {outViewLevels.map((line, li) => {
          const above = line.price > domain.max;
          const edgeY = above ? PAD.top + 3 : H - PAD.bottom - 3;
          const gapPct = lastPrice ? ((line.price / lastPrice) - 1) * 100 : null;
          /* ★ 贴边标签**左右分侧**：同一侧的档位（比如止损与回踩都在图下方）
             edgeY 完全相同，纯纵向错开会钻出绘图区，所以改成奇偶分到左右两侧 ——
             两条一定不重叠，三条时同侧的两条再各自让 12px。 */
          const leftSide = li % 2 === 0;
          const dy = Math.floor(li / 2) * 12;
          return (
            <g key={`edge-${line.key}-${line.price}`}>
              <line x1={PAD.left} x2={W - PAD.right} y1={edgeY} y2={edgeY}
                    stroke={line.color} strokeWidth="1.2"
                    strokeDasharray="2 4" opacity={0.6} />
              <text x={leftSide ? PAD.left + 4 : W - PAD.right - 4}
                    y={above ? edgeY + 11 + dy : edgeY - 4 - dy}
                    fontSize="10" textAnchor={leftSide ? "start" : "end"}
                    fill={line.color}>
                {line.label} {line.price.toFixed(2)}
                {gapPct === null ? "" : `（${gapPct >= 0 ? "+" : ""}${gapPct.toFixed(2)}%）`}
                {above ? " ↑在图上方" : " ↓在图下方"}
              </text>
            </g>
          );
        })}
        {/* 档位曲线模式下，右端标当前值（曲线自然收束到那里）；
            同一个 y 上有多条时依次错开 11px，避免标签叠在一起看不清 */}
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

        {/* 档位线右端标当前值 —— 放在**最后**绘制：它是压在右轴上的小标签，
            必须盖在所有曲线之上，否则会被分时线/十字光标划断。
            同一 y 附近有多条时依次错开 11px，避免标签叠在一起看不清。 */}
        {(() => {
          const tags = inViewLevels
            .map((line) => ({ line, y: y(line.price) }))
            .sort((a, b) => a.y - b.y);
          let lastY = -Infinity;
          return tags.map(({ line, y: rawY }) => {
            const textY = Math.max(rawY + 4, lastY + 11);
            lastY = textY;
            return (
              <g key={`tag-${line.key}-${line.price}`}>
                <circle cx={W - PAD.right + 2} cy={rawY} r="2.4" fill={line.color} />
                <text x={W - PAD.right + 10} y={textY} fontSize="10"
                      fill={line.color}>
                  {line.label} {line.price.toFixed(2)}
                </text>
              </g>
            );
          });
        })()}

        {/* 悬浮十字光标：**纵线吸附到分钟点 + 横线跟随鼠标**。
            两条线各带一个"轴标"（左边价格、下边时间），并在读数条给出"光标"价格，
            这样"指到哪读到哪"，不用去右轴上一格格对。 */}
        {hover && (() => {
          const hx = x(hover.minute);
          const hy = y(hover.cursorPrice);
          const cursorPct = pctOf(hover.cursorPrice);
          return (
            <g className="crosshair">
              <line x1={hx} x2={hx} y1={PAD.top} y2={H - PAD.bottom}
                    stroke="var(--muted)" strokeDasharray="3 3" opacity="0.85" />
              <line x1={PAD.left} x2={W - PAD.right} y1={hy} y2={hy}
                    stroke="var(--muted)" strokeDasharray="3 3" opacity="0.85" />
              {/* 左轴价格标 */}
              <rect x={PAD.left - 60} y={hy - 8} width={58} height={16} rx={3}
                    fill="var(--accent)" opacity="0.92" />
              <text x={PAD.left - 6} y={hy + 4} fontSize="10" textAnchor="end"
                    fill="#0f1419" className="crosshair-label">
                {hover.cursorPrice.toFixed(2)}
              </text>
              {/* 右轴涨跌幅标 */}
              {cursorPct !== null && (
                <>
                  <rect x={W - PAD.right + 2} y={hy - 8} width={52} height={16} rx={3}
                        fill={cursorPct >= 0 ? "var(--low)" : "var(--high)"}
                        opacity="0.92" />
                  <text x={W - PAD.right + 6} y={hy + 4} fontSize="10"
                        fill="#0f1419" className="crosshair-label">
                    {cursorPct >= 0 ? "+" : ""}{cursorPct.toFixed(2)}%
                  </text>
                </>
              )}
              {/* 下轴时间标 */}
              <rect x={hx - 22} y={H - PAD.bottom + 2} width={44} height={15} rx={3}
                    fill="var(--accent)" opacity="0.92" />
              <text x={hx} y={H - PAD.bottom + 13} fontSize="10"
                    textAnchor="middle" fill="#0f1419" className="crosshair-label">
                {timeLabel(hover.minute)}
              </text>
              {/* 数据点 + 交点数值 */}
              <circle cx={hx} cy={y(hover.price)} r="3.5" fill="var(--accent)" />
              <circle cx={hx} cy={hy} r="2.5" fill="none"
                      stroke="var(--muted)" strokeWidth="1.2" />
            </g>
          );
        })()}
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
            {/* 十字光标横线那一点：用户指到哪就报哪 */}
            <span className="crosshair-readout">
              光标 <b className="mono">{hover.cursorPrice.toFixed(2)}</b>
              {prevClose && (
                <b className="mono" style={{
                  color: hover.cursorPrice >= prevClose ? "var(--low)" : "var(--high)",
                }}>
                  {" "}{(((hover.cursorPrice / prevClose) - 1) * 100).toFixed(2)}%
                </b>
              )}
            </span>
            {/* 距三条档位线多远：做T时最常问的一句话 */}
            {inViewLevels.length > 0 && (
              <span className="muted-text crosshair-levels">
                {inViewLevels.slice(0, 4).map((line) => (
                  <span key={`hv-${line.key}-${line.price}`} style={{ color: line.color }}>
                    {" "}{line.label}
                    {" "}{(((line.price / hover.cursorPrice) - 1) * 100).toFixed(2)}%
                  </span>
                ))}
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

      {/* 档位清单：**所有**档位都列出来（含未显示的），并给出显示开关与"图外"提示。
          为什么把开关放这里而不是藏进设置：止损线有时离现价 4~5%（ATR 口径），
          纳入定标会把分时线压扁；给用户一个"只看回踩冲高 / 连止损一起看"的选择，
          比替他决定更诚实。 */}
      <div className="level-strip">
        {allLevels.map((line, index) => {
          const outside = line.price < domain.min || line.price > domain.max;
          const pct = lastPrice ? ((line.price / lastPrice) - 1) * 100 : null;
          const shown = showLevels[line.key];
          return (
            <label key={`${line.key}-${line.price}-${index}`}
                   className={`level-chip ${line.cls}${outside ? " outside" : ""}${
                     shown ? "" : " hidden-line"}`}
                   title={outside
                     ? "该档位在当前Y轴范围外（避免压扁分时线，故不动Y轴）—— "
                       + "勾选下方「图外档位也纳入Y轴」即可把它画进来"
                     : "已在图中标注；取消勾选可临时隐藏这条线"}>
              <input type="checkbox" checked={shown}
                     onChange={(e) => setShowLevels((prev) => ({
                       ...prev, [line.key]: e.target.checked }))} />
              {line.label} <b className="mono">{line.price.toFixed(2)}</b>
              {pct !== null && (
                <em className="muted-text">
                  {pct >= 0 ? "+" : ""}{pct.toFixed(2)}%
                </em>
              )}
              {outside && <span className="muted-text">（图外）</span>}
            </label>
          );
        })}
        {outViewLevels.length > 0 && (
          <label className="muted-text chart-toggle">
            <input type="checkbox" checked={fitLevels}
                   onChange={(e) => setFitLevels(e.target.checked)} />
            图外档位也纳入Y轴
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
