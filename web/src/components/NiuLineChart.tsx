import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { IntradayDailyBar, IntradayDailySnapshot } from "../api";

/**
 * 擒牛线主图（日K）。
 *
 * ## 与旧的 DailyCandleChart 的关系（口径已更正）
 *
 * 用户 2026-09-18 更正口径：当初说的"**把原来的 K 线去掉**"，
 * 指的是**去掉 K 线上的均线（MA5/10/20/60）**，而**不是**去掉蜡烛柱状图本身。
 * 上一版按字面理解把蜡烛整段删了，导致日K界面只剩五条擒牛线、看不到开高低收
 * —— 已按更正后的口径把蜡烛加回来，均线**不画**（`IntradayDailyBar` 里
 * `ma5/ma10/ma20/ma60` 字段仍在，只是不渲染）。
 *
 * 图层次序（从下到上）：网格 → **蜡烛** → 高量柱安全线/风险线 → 五条擒牛线
 * → 信号标记 → 十字光标。蜡烛用 span 内固定的 `barWidth`，缩放到很密时
 * 退化成 1px 线（仍能看出涨跌颜色）。
 *
 * ## 五条线（后端按标的类别选两套公式，见 src/intraday/niuline.py）
 *
 * | 线 | 含义 |
 * |---|---|
 * | NML | 前20日最高价 + ATRV/2（上沿半档） |
 * | QRL | 前20日最高价 + ATRV（突破线） |
 * | CBX20 | 20日量价均价（短期成本线） |
 * | CBX60 | 60日量价均价（中期成本线） |
 * | SMX | 10日收盘均线 |
 *
 * **用法（用户原话）**：股价站稳 SMX 或 CBX20 之上才算企稳；多头趋势中
 * 回踩 SMX 或 CBX20 一般是回踩点。因此右上角状态条直接给出"现价相对各线的
 * 位置"（站上/跌破），不用用户自己在图上比。
 *
 * 交互沿用旧图：滚轮缩放、拖拽平移、双击复位、十字光标读数。
 */

const W = 1000;
const H = 420;
const PAD = { top: 16, right: 92, bottom: 40, left: 58 };
const VOLUME_RATIO = 0.22;
const MIN_BARS = 15;

/** 线色：成本线用暖/绿系、突破线用红、趋势线用青，彼此可辨且和量柱不撞色。 */
const LINE_STYLE: Record<string, { color: string; width: number; dash?: string }> = {
  cbx60: { color: "#e8c07d", width: 1.4 },
  cbx20: { color: "#7fd1e8", width: 1.6 },
  smx: { color: "#6fd08c", width: 1.6 },
  nml: { color: "#c58fe8", width: 1.2, dash: "6 3" },
  qrl: { color: "#ff8a80", width: 1.6, dash: "6 3" },
};

/** 状态条与图例的显示顺序（成本线在前 —— 用法是围绕它们的）。 */
const LINE_ORDER = ["smx", "cbx20", "cbx60", "nml", "qrl"] as const;

/** 默认显示的线：五条全开（用户要看的是"站稳哪条"，不该先手动打开）。 */
const DEFAULT_VISIBLE: Record<string, boolean> = {
  smx: true, cbx20: true, cbx60: true, nml: true, qrl: true,
};

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

/**
 * 十字光标状态：光标所在的那根bar + **光标自身的位置**。
 *
 * 为什么把光标位置也记进来（2026-09-23 用户口径）：实时读数要给出
 * 「X 轴的日期」（= 光标所在bar）+「Y 轴的价格」（= 光标当前高度对应的价）
 * +「该日K 的涨跌幅/最高/最低」。横线原来的做法是钉在**收盘价**上，
 * 那样读数只能给收盘价，鼠标上下移动没有任何反馈 —— 现在横线与右侧价格标签
 * 都跟着鼠标走。
 */
type Hover = {
  /** 光标所在bar的下标（竖线与「该日K」读数都用它） */
  index: number;
  /** 光标高度对应的价格（已按价格区裁剪，落到量柱区也不会读出离谱的价） */
  price: number;
  /** 光标在 SVG viewBox 里的 y（画横线与右侧价格标签用） */
  vy: number;
  /** 浮层读数框相对图表容器的位置（已做过边界翻转，不会跑出图外） */
  left: number;
  top: number;
} | null;

/** 浮层读数框的估宽/估高：只用于"贴边时翻到另一侧"，不参与布局计算。 */
const HOVER_W = 196;
const HOVER_H = 168;

/** 区间测量：拖拽选中的一段bar（含首尾），以及它算出来的统计量。 */
type Measure = {
  start: number;
  end: number;
  /** 拖拽中（此时浮层读数框先让位，避免两个框打架） */
  dragging: boolean;
} | null;

/** 成交量：单位**手**，量大时换算成万手/亿手（图上量柱也是手）。 */
function formatVolume(volume: number | null | undefined): string {
  if (volume === null || volume === undefined || !Number.isFinite(volume)) return "—";
  if (Math.abs(volume) >= 1e8) return `${(volume / 1e8).toFixed(2)}亿手`;
  if (Math.abs(volume) >= 1e4) return `${(volume / 1e4).toFixed(2)}万手`;
  return `${volume.toFixed(0)}手`;
}

/** 资金：元 → 亿元（保留两位；负号保留，用户口径就是"净流入/净流出"）。 */
function formatMoney(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const yi = value / 1e8;
  return `${yi >= 0 ? "+" : ""}${yi.toFixed(2)}亿`;
}

/** 一组bar的统计量（区间测量用）。涨跌幅按**首收 → 末收**算，与行情软件口径一致。 */
function summarize(bars: IntradayDailyBar[], start: number, end: number) {
  const slice = bars.slice(start, end + 1);
  if (!slice.length) return null;
  const first = slice[0];
  const last = slice[slice.length - 1];
  const base = first.close;
  const pct = base ? (last.close - base) / base * 100 : null;
  let high = -Infinity;
  let low = Infinity;
  let turnoverSum = 0;
  let turnoverCount = 0;
  let flowSum = 0;
  let flowCount = 0;
  slice.forEach((bar) => {
    high = Math.max(high, bar.high);
    low = Math.min(low, bar.low);
    if (typeof bar.turnover === "number" && Number.isFinite(bar.turnover)) {
      turnoverSum += bar.turnover;
      turnoverCount += 1;
    }
    if (typeof bar.net_mf === "number" && Number.isFinite(bar.net_mf)) {
      flowSum += bar.net_mf;
      flowCount += 1;
    }
  });
  return {
    days: slice.length, start: first.date, end: last.date,
    firstClose: base, lastClose: last.close, pct,
    high, low,
    turnoverSum, turnoverCount,
    flowSum, flowCount,
  };
}

function NiuLineChart({
  snapshot, height = H, emptyReason = "", emptyHint = "",
}: {
  snapshot: IntradayDailySnapshot;
  height?: number;
  emptyReason?: string;
  emptyHint?: string;
}) {
  const bars = snapshot.bars;
  const lines = snapshot.niuline;
  const points = useMemo(() => lines?.points ?? [], [lines]);
  const markers = useMemo(() => snapshot.signal_history ?? [], [snapshot]);
  const indexByDate = useMemo(
    () => new Map(bars.map((bar, index) => [bar.date, index])), [bars]);

  const [range, setRange] = useState<{ start: number; end: number } | null>(null);
  const [hover, setHover] = useState<Hover>(null);
  const [visibleLines, setVisibleLines] = useState<Record<string, boolean>>(
    DEFAULT_VISIBLE);
  const svgRef = useRef<SVGSVGElement | null>(null);
  /** 浮层读数框的定位基准（`position: relative` 的那个容器）。 */
  const wrapRef = useRef<HTMLDivElement | null>(null);
  /** 鼠标左键拖动：默认**平移**；开了区间测量（或按住 Shift）时变成**框选**。 */
  const dragRef = useRef<{ mode: "pan" | "measure"; x: number;
                            start: number; end: number } | null>(null);
  /** 区间测量：拖出来的那一段（`null` = 没在测）。 */
  const [measure, setMeasure] = useState<Measure>(null);
  /** 区间测量模式开关（工具栏按钮；Shift+拖拽可临时用，不必先开）。 */
  const [measureMode, setMeasureMode] = useState(false);
  const [showVolume, setShowVolume] = useState(true);
  /** 蜡烛柱状图开关。默认为开 —— 它是日K图的主体，关掉只是"想单独看擒牛线"。 */
  const [showCandle, setShowCandle] = useState(true);

  const total = bars.length;
  const view = useMemo(() => {
    if (total === 0) return { start: 0, end: 0 };
    if (!range) return { start: 0, end: total - 1 };
    return range;
  }, [range, total]);
  const span = Math.max(1, view.end - view.start);

  const plotW = W - PAD.left - PAD.right;
  const priceH = (height - PAD.top - PAD.bottom)
    * (showVolume ? 1 - VOLUME_RATIO : 1);
  const volumeTop = PAD.top + priceH + 8;
  const volumeH = (height - PAD.top - PAD.bottom) * VOLUME_RATIO - 8;

  const x = useCallback((index: number) =>
    PAD.left + ((index - view.start) / span) * plotW, [view.start, span, plotW]);

  const visible = useMemo(
    () => bars.slice(Math.max(0, view.start), Math.min(total, view.end + 1)),
    [bars, view, total]);

  /**
   * Y 轴定义域：**只看擒牛线的值**，不看 K 线高低（2026-09-18 回退）。
   *
   * ## 为什么不用"线 + K 线极值并集"（试过，回退了）
   *
   * 并集的动机是"画布别浪费、蜡烛别看不清"。实测上线后**蜡烛反而整段看不见了**，
   * 用户直接报障。原因是本图的蜡烛是**只描边不填充**画的（阳线 `fill="none"`），
   * 一旦定义域被 K 线极值（含上下影线）撑大，蜡烛实体就被压得很扁 ——
   * 只剩 1px 上下的空心矩形，在深色背景上基本等于"消失"。
   * 而原来"只看线"的定义域更窄，蜡烛反而更立体。
   *
   * 所以这里回到**只看线**：擒牛线是这张图的"档位"主体，
   * CBX60 这类中期成本线常明显低于近期高点，把 K 线极值并进来会把五条线压成
   * 底部一团，而"回踩 CBX20 是不是回踩点"恰恰取决于线附近的细节。
   * 线的值算不出来的暖机段（None）自动跳过；全部线都还没暖机时退回 K 线极值，
   * 至少不空图。
   */
  const domain = useMemo(() => {
    if (!visible.length) return null;
    const start = Math.max(0, view.start);
    const end = Math.min(total, view.end + 1);
    const levels: number[] = [];
    for (let index = start; index < end; index += 1) {
      const point = points[index];
      if (!point) continue;
      LINE_ORDER.forEach((key) => {
        if (!visibleLines[key]) return;
        const value = point[key];
        if (typeof value === "number" && Number.isFinite(value)) levels.push(value);
      });
    }
    if (!levels.length) {
      // 全部线都还没暖机（样本太短的次新股）：退回 K 线极值，至少不空图
      levels.push(...visible.map((bar) => bar.high),
                  ...visible.map((bar) => bar.low));
    }
    let low = Math.min(...levels);
    let high = Math.max(...levels);
    if (!Number.isFinite(low) || !Number.isFinite(high)) return null;
    if (high === low) { low -= 0.5; high += 0.5; }
    const pad = (high - low) * 0.08;
    return { low: low - pad, high: high + pad, span: (high - low) + pad * 2 };
  }, [visible, points, view.start, view.end, total, visibleLines]);

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
  /**
   * 蜡烛实体宽度：比 `barWidth` 略窄一点，缩放很密时退化成 1px 竖线
   * （蜡烛密集时实体互相粘连会比留缝更难看，且 1px 仍能靠颜色分辨涨跌）。
   */
  const candleWidth = Math.max(1, (plotW / (span + 1)) * 0.7);

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

  /** Esc 清除区间测量（拖歪了想重来时不必去点按钮）。 */
  useEffect(() => {
    if (!measure) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMeasure(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [measure]);

  const indexAt = (clientX: number): number | null => {
    const node = svgRef.current;
    if (!node) return null;
    const rect = node.getBoundingClientRect();
    const ratio = ((clientX - rect.left) / rect.width * W - PAD.left) / plotW;
    return Math.round(view.start + Math.min(1, Math.max(0, ratio)) * span);
  };

  /** 价格轴的反函数：viewBox 的 y → 价格（与 `yPrice` 互逆；越界一律裁到价格区）。 */
  const priceAtY = useCallback((vy: number) => {
    if (!domain) return 0;
    const clamped = Math.min(PAD.top + priceH, Math.max(PAD.top, vy));
    return domain.low + (1 - (clamped - PAD.top) / priceH) * domain.span;
  }, [domain, priceH]);

  const handleMove = (event: React.MouseEvent<SVGSVGElement>) => {
    const drag = dragRef.current;
    const index = indexAt(event.clientX);
    if (index === null) return;
    if (drag && drag.mode === "measure") {
      // 框选：anchor 与当前下标排序后作为区间（拖回起点也不会出现 start > end）
      setMeasure({
        start: Math.min(drag.x, index),
        end: Math.max(drag.x, index),
        dragging: true,
      });
      return;
    }
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
    if (!bars[index]) return;
    const node = svgRef.current;
    const wrap = wrapRef.current;
    if (!node || !wrap) return;
    const rect = node.getBoundingClientRect();
    const wrapRect = wrap.getBoundingClientRect();
    // viewBox 与 CSS 尺寸不成 1:1（viewBox 1000×height 被拉伸到容器宽），必须按比例换
    const vy = (event.clientY - rect.top) / rect.height * height;
    // 浮层默认落在光标右下方；贴到右/下边缘时翻到另一侧，绝不跑出图外
    const localX = event.clientX - wrapRect.left;
    const localY = event.clientY - wrapRect.top;
    const left = localX + 14 + HOVER_W > wrapRect.width
      ? Math.max(4, localX - HOVER_W - 14) : localX + 14;
    const top = localY + 14 + HOVER_H > wrapRect.height
      ? Math.max(4, localY - HOVER_H - 14) : localY + 14;
    setHover({
      index,
      price: priceAtY(vy),
      vy: Math.min(PAD.top + priceH, Math.max(PAD.top, vy)),
      left, top,
    });
  };

  if (!total || !domain || !lines) {
    const reason = emptyReason
      || (!lines ? "擒牛线数据缺失" : total === 0
        ? "该标的没有可用日K数据" : "当前可视区间没有数据");
    return (
      <div className="empty-tip muted-text">
        擒牛线主图不可用：{reason}
        {emptyHint ? <span className="muted-text">（{emptyHint}）</span> : null}
      </div>
    );
  }

  const lastClose = bars[total - 1]?.close ?? null;
  const hoverBar = hover ? bars[hover.index] : null;
  const hoverPoint = hover ? points[hover.index] : null;
  const shown = hoverPoint ?? points[total - 1] ?? null;
  const shownBar = hoverBar ?? bars[total - 1] ?? null;

  /**
   * 标记中心与K线之间保留的**像素**间隙（与价格定义域无关，因此先算出像素位置、
   * 不依赖"把间距换算成价格"这种容易弄反的做法）。
   */
  const MARKER_CLEAR = 4;

  /**
   * 标记分组：同一根bar同一侧只画一个三角，其余同侧信号只进 tooltip。
   *
   * 为什么必须合并：日线信号回放里同一根bar经常同时触发 2~3 条（实测 003040 的
   * 2026-08-10 是 B12+S3+S6、08-31 是 B8+B15+S3），逐个画就是三个三角叠在同一个
   * x 上 —— 视觉上正是"多余、凌乱"，数字标签还会互相压字。
   *
   * 顺带做**有上限的**防重叠推挤：相邻同侧标记太近时最多推开 `MAX_PUSH`，
   * 保证任何一个三角离自己的K线都不超过约 20px。
   *
   * ⚠️ 踩过的坑：一开始做成"必须满足最小间距、不够就一直推"，结果密集段越推越远，
   * 实测把标记推到了离K线 190px 的地方 —— 比原来的问题更糟。
   * **不能为了排版把它们赶离K线**：挨着K线看不清可以忍，飘走就是错的。
   */
  type MarkerGroup = {
    date: string; index: number; isBuy: boolean;
    marks: typeof markers; center?: number; showText?: boolean;
  };

  const markerGroups = useMemo<MarkerGroup[]>(() => {
    const byKey = new Map<string, MarkerGroup>();
    markers.forEach((mark) => {
      const index = indexByDate.get(mark.date);
      if (index === undefined || index < view.start || index > view.end) return;
      const isBuy = mark.side === "buy";
      const key = `${mark.date}-${isBuy ? "buy" : "sell"}`;
      const found = byKey.get(key);
      if (found) found.marks.push(mark);
      else byKey.set(key, { date: mark.date, index, isBuy, marks: [mark] });
    });
    const groups = [...byKey.values()].sort((a, b) => a.index - b.index);
    if (!domain) return groups;

    const MIN_GAP = 11;                       // 三角+数字大致需要的垂直空间
    const MAX_PUSH = 16;                      // 允许被推开的像素上限
    /** 文字最小间距：比三角形更宽，否则密集段数字会连成一片 */
    const TEXT_GAP = 22;
    const lastY = { buy: Number.NEGATIVE_INFINITY,
                    sell: Number.POSITIVE_INFINITY };
    const lastTextY = { buy: Number.NEGATIVE_INFINITY,
                        sell: Number.POSITIVE_INFINITY };
    return groups.map((group) => {
      const bar = bars[group.index];
      // ⚠️ 两侧都用同一个 yPrice(·) + MARKER_CLEAR：早先写的是
      // `yPrice(low - gap) + 4` / `yPrice(high + gap) - 4`，看着对称其实不是 ——
      // y 越小价格越高，所以"加 4"把多方触发推**近**K线、"减 4"把空方触发推**远**，
      // 实测两侧间距差一倍（5px vs 12px）。现在统一在像素域留间隙。
      const natural = group.isBuy
        ? yPrice(bar.low) + MARKER_CLEAR + 5     // 多方触发：K线下方
        : yPrice(bar.high) - MARKER_CLEAR - 5;   // 空方触发/风控：K线上方
      let center = natural;
      if (group.isBuy) {
        if (natural - lastY.buy < MIN_GAP) {
          center = Math.min(lastY.buy + MIN_GAP, natural + MAX_PUSH);
        }
        lastY.buy = center;
      } else {
        if (lastY.sell - natural < MIN_GAP) {
          center = Math.max(lastY.sell - MIN_GAP, natural - MAX_PUSH);
        }
        lastY.sell = center;
      }
      /**
       * 文字只在**排得开**的时候画。
       *
       * 密集段（本标的 30 根里有 21 个信号）数字标签必然互相压字 ——
       * 那正是用户说的"多余凌乱"。三角形必须都在（那是位置信息），
       * 但文字是重复信息（下方表格、悬停 tooltip 都有），宁可省掉也不要糊成一片。
       * 节流用独立的游标，避免"文字被省略后挤在一起"。
       */
      const textKey = group.isBuy ? "buy" : "sell";
      const textY = center + (group.isBuy ? 16 : -9);
      const spaced = Math.abs(textY - lastTextY[textKey]) >= TEXT_GAP;
      if (spaced) lastTextY[textKey] = textY;
      return { ...group, center, showText: spaced };
    });
  }, [markers, indexByDate, view.start, view.end, bars, domain, yPrice]);

  /** 现价相对某条线的位置（"站稳"判据：收盘价 ≥ 线值）。 */
  const stance = (level: number | null | undefined): {
    text: string; tone: "above" | "below" | "none";
  } => {
    if (typeof level !== "number" || !Number.isFinite(level) || lastClose === null) {
      return { text: "—", tone: "none" };
    }
    return lastClose >= level
      ? { text: "站上", tone: "above" }
      : { text: "跌破", tone: "below" };
  };

  const labelFor = (key: string): string =>
    lines.lines?.find((line) => line.key === key)?.label ?? key.toUpperCase();
  // 注：后端每条线还带一个 `note`（口径/公式说明），**刻意不渲染** ——
  // 用户口径 2026-09-23：公式细节属于核心机密，不显示给前端。

  /** 区间测量的统计量（选区为空/越界时为 null）。 */
  const measureResult = useMemo(() => {
    if (!measure || !total) return null;
    const start = Math.max(0, Math.min(measure.start, measure.end));
    const end = Math.min(total - 1, Math.max(measure.start, measure.end));
    return summarize(bars, start, end);
  }, [measure, bars, total]);

  /**
   * 测量框位置：贴在选区上方居中，越界就夹回容器内。
   * 需要把 viewBox 的 x 换算成容器像素 —— SVG 被拉伸到容器宽，所以要按比例缩。
   * `top` 用 SVG 自己的偏移量：容器里在它上面还有工具栏与口径条，
   * 写死 6px 会把口径条盖住（实测踩过）。
   */
  const measureBox = (() => {
    // SVG 在容器内的纵向偏移：`SVGElement` 没有 `offsetTop`（那是 HTMLElement 的），
    // 所以用两者 rect 的差换算。
    const svgRect = svgRef.current?.getBoundingClientRect();
    const wrapRect = wrapRef.current?.getBoundingClientRect();
    const svgTop = svgRect && wrapRect ? svgRect.top - wrapRect.top + 6 : 6;
    if (!measure) return { left: 8, top: svgTop };
    const svgWidth = wrapRef.current?.clientWidth ?? W;
    const center = (x(measure.start) + x(measure.end)) / 2 / W * svgWidth;
    const width = 236;
    const left = Math.max(4, Math.min(svgWidth - width - 4, center - width / 2));
    return { left, top: svgTop };
  })();
  const measureLeft = measureBox.left;
  const measureTop = measureBox.top;

  /** 折线路径：None 处断开（暖机段不画线，也不连到起点）。 */
  const pathFor = (key: string): string => {
    const parts: string[] = [];
    let open = false;
    for (let index = view.start; index <= view.end; index += 1) {
      const point = points[index];
      const value = point ? point[key as keyof typeof point] : null;
      if (typeof value !== "number" || !Number.isFinite(value)) {
        open = false;
        continue;
      }
      parts.push(`${open ? "L" : "M"}${x(index).toFixed(1)},`
        + `${yPrice(value).toFixed(1)}`);
      open = true;
    }
    return parts.join(" ");
  };

  return (
    <div className="daily-chart-wrap niuline-wrap" ref={wrapRef}>
      <div className="chart-toolbar">
        <span className="muted-text">滚轮缩放 · 拖拽平移 · Shift+拖拽测量区间 · 双击复位</span>
        <button className="btn-ghost chart-btn"
                onClick={() => zoomAt(0.7, Math.round((view.start + view.end) / 2))}>＋</button>
        <button className="btn-ghost chart-btn"
                onClick={() => zoomAt(1.4, Math.round((view.start + view.end) / 2))}>－</button>
        <button className="btn-ghost chart-btn" onClick={reset}
                disabled={!range}>复位</button>
        <button className={`btn-ghost chart-btn${measureMode ? " active" : ""}`}
                onClick={() => {
                  // 关掉测量模式时顺手清掉已画的区间（留着会让人以为还在测）
                  setMeasureMode((on) => {
                    if (on) setMeasure(null);
                    return !on;
                  });
                }}
                title={"区间测量：打开后拖拽即可框选一段日K（也可以随时按住 Shift 拖拽），"
                       + "显示区间涨跌幅、区间交易日数、换手合计与资金净流入/流出"}>
          {measureMode ? "✓ 区间测量" : "区间测量"}
        </button>
        <label className="chart-toggle">
          <input type="checkbox" checked={showCandle}
                 onChange={(e) => setShowCandle(e.target.checked)} />K线
        </label>
        <label className="chart-toggle">
          <input type="checkbox" checked={showVolume}
                 onChange={(e) => setShowVolume(e.target.checked)} />成交量
        </label>
        <span className="muted-text">
          {bars[view.start]?.date} ~ {bars[view.end]?.date}（{span + 1}根）
        </span>
      </div>

      {/* ⚠️ 这里原来有一条"口径条"，会显示：变体（个股口径 / 指数/ETF/板块口径）、
          CBX 的均价公式（SUM(AMOUNT,N)/SUM(V,N) 或 SUM(C*V,N)/SUM(V,N)）、
          参数 N/M、口径判定的理由、以及"成交额不可用 → 退回指数口径"的说明。

          用户口径 2026-09-23：**这些是核心机密，不显示给前端**。
          因此整块删掉 —— 不是改成 tooltip、也不是改文案，而是不再下发到界面。
          下面只保留使用者真正需要的：每条线的**当期数值**与"站上/跌破"判断。 */}

      {/* 用法状态条：站稳/跌破一眼可见（用户口径：站稳 SMX 或 CBX20 才叫企稳） */}
      <div className="niuline-stance">
        {LINE_ORDER.map((key) => {
          const level = shown ? shown[key as keyof typeof shown] : null;
          const state = stance(typeof level === "number" ? level : null);
          return (
            <label key={key}
                   className={`niuline-stance-item${visibleLines[key] ? "" : " off"}`}
                   title={visibleLines[key] ? "点勾选框隐藏这条线" : "点勾选框显示这条线"}>
              <input type="checkbox" checked={visibleLines[key]}
                     onChange={() => setVisibleLines((current) => ({
                       ...current, [key]: !current[key],
                     }))} />
              <span className="niuline-dot"
                    style={{ background: LINE_STYLE[key]?.color ?? "#888" }} />
              <span className="niuline-key">{labelFor(key)}</span>
              <span className="mono">
                {typeof level === "number" ? level.toFixed(2) : "—"}
              </span>
              <span className={`niuline-state ${state.tone}`}>{state.text}</span>
            </label>
          );
        })}
        <span className="muted-text">
          用法：站稳 SMX / CBX20 之上才算企稳；多头趋势中回踩它们是回踩参考
        </span>
      </div>

      <svg ref={svgRef} viewBox={`0 0 ${W} ${height}`} className="intraday-svg"
           role="img"
           onMouseMove={handleMove}
           onMouseDown={(e) => {
             const index = indexAt(e.clientX);
             if (index === null) return;
             // Shift+拖拽随时可框选；开了「区间测量」则拖拽默认就是框选（不再平移）
             if (measureMode || e.shiftKey) {
               dragRef.current = {
                 mode: "measure", x: index, start: view.start, end: view.end };
               setMeasure({ start: index, end: index, dragging: true });
               return;
             }
             dragRef.current = {
               mode: "pan", x: index, start: view.start, end: view.end };
           }}
           onMouseUp={() => {
             dragRef.current = null;
             setMeasure((current) => (current ? { ...current, dragging: false } : current));
           }}
           onMouseLeave={() => {
             dragRef.current = null;
             setHover(null);
             setMeasure((current) => (current ? { ...current, dragging: false } : current));
           }}
           onDoubleClick={() => { reset(); setMeasure(null); }}>
        {yTicks.map((price) => (
          <g key={price}>
            <line x1={PAD.left} x2={W - PAD.right} y1={yPrice(price)} y2={yPrice(price)}
                  stroke="var(--border)" strokeDasharray="3 4" opacity={0.6} />
            <text x={PAD.left - 6} y={yPrice(price) + 4} fontSize="10"
                  fill="var(--muted)" textAnchor="end">{price.toFixed(2)}</text>
          </g>
        ))}
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

        {/* 蜡烛柱状图（2026-09-18 更正口径后加回）。
            必须是**网格之后、擒牛线之前**的第一层：五条线要压在蜡烛上方，
            被蜡烛盖住就没法看"站上/跌破哪条线"了。 */}
        {showCandle && visible.map((bar) => {
          const barIndex = indexByDate.get(bar.date);
          if (barIndex === undefined) return null;
          const cx = x(barIndex);
          const up = bar.close >= bar.open;
          const color = up ? "var(--low)" : "var(--high)";
          const top = yPrice(Math.max(bar.open, bar.close));
          const bottom = yPrice(Math.min(bar.open, bar.close));
          return (
            <g key={bar.date}>
              <line x1={cx} x2={cx} y1={yPrice(bar.high)} y2={yPrice(bar.low)}
                    stroke={color} strokeWidth="1" />
              <rect x={cx - candleWidth / 2} width={candleWidth}
                    y={top} height={Math.max(1, bottom - top)}
                    fill={up ? "none" : color} stroke={color}
                    strokeWidth="1" />
            </g>
          );
        })}

        {/* 高量柱安全线 / 风险线：与量柱体系配合看 */}
        {snapshot.active_anchor && (
          <>
            <line x1={PAD.left} x2={W - PAD.right}
                  y1={yPrice(snapshot.active_anchor.body_top)}
                  y2={yPrice(snapshot.active_anchor.body_top)}
                  className="lv-sell" strokeWidth="1.2" strokeDasharray="7 3" />
            <line x1={PAD.left} x2={W - PAD.right}
                  y1={yPrice(snapshot.active_anchor.body_bottom)}
                  y2={yPrice(snapshot.active_anchor.body_bottom)}
                  className="lv-stop" strokeWidth="1.2" strokeDasharray="7 3" />
          </>
        )}

        {/* 五条擒牛线 */}
        {LINE_ORDER.map((key) => {
          if (!visibleLines[key]) return null;
          const path = pathFor(key);
          if (!path) return null;
          const style = LINE_STYLE[key];
          const value = shown ? shown[key as keyof typeof shown] : null;
          return (
            <g key={key}>
              <path d={path} fill="none" stroke={style.color}
                    strokeWidth={style.width}
                    strokeDasharray={style.dash} opacity={0.95} />
              {/* 右轴贴现值：省得用户在图上找刻度 */}
              {typeof value === "number" && (
                <text x={W - PAD.right + 5}
                      y={(hover ? yPrice(value) : yPrice(value)) + 4}
                      fontSize="10" fill={style.color} className="mono">
                  {value.toFixed(2)}
                </text>
              )}
            </g>
          );
        })}

        {/* 成交量（保留：量柱体系是这套做T的另一半） */}
        {showVolume && visible.map((bar, offset) => {
          const index = view.start + offset;
          const cx = x(index);
          const top = yVolume(bar.volume);
          const up = bar.close >= bar.open;
          return (
            <rect key={`v-${bar.date}`} x={cx - barWidth / 2} width={barWidth}
                  y={top} height={Math.max(0.8, volumeTop + volumeH - top)}
                  fill={bar.is_high_volume ? "var(--medium)"
                    : up ? "var(--low)" : "var(--high)"}
                  opacity={bar.is_high_volume ? 0.95 : 0.5}>
              <title>
                {`${bar.date} 收 ${bar.close.toFixed(2)} 量 ${bar.volume.toFixed(0)}`
                  + (bar.is_high_volume ? "（高量柱）" : "")}
              </title>
            </rect>
          );
        })}

        {/* 买卖信号标记：**紧贴各自K线**（多方触发红▲在下方、空方触发绿▼/风控橙▼在上方），
            间距按价格比例取，因此不同价位的票贴得一样近。
            同一根bar同一侧只画一个三角（其余信号进 tooltip），相邻标记做碰撞推挤。
            数据来源：日线信号回放最近 30 根（`signal_history`），只保留首次触发。 */}
        {markerGroups.map((group) => {
          const { date, isBuy, marks: groupMarks } = group;
          const center = typeof group.center === "number"
            ? group.center
            : (isBuy ? volumeTop - 12 : PAD.top + 12);
          const cx = x(group.index);
          // 优先级：真空方触发(sell) > 风控(risk) —— 颜色跟着最重要的那个走
          const lead = groupMarks.find((mark) => mark.side === "sell")
            ?? groupMarks[0];
          const color = isBuy ? "var(--low)"
            : lead.side === "sell" ? "var(--high)" : "var(--medium)";
          // 多方触发：尖端朝上（▲）贴在K线下方；空方触发/风控：尖端朝下（▼）贴在K线上方
          const path = isBuy
            ? `M${cx},${center - 5} L${cx - 5},${center + 5} L${cx + 5},${center + 5} Z`
            : `M${cx},${center + 5} L${cx - 5},${center - 5} L${cx + 5},${center - 5} Z`;
          const caption = groupMarks.map((mark) => mark.code).join("+");
          const title = groupMarks.map((mark) => {
            const kind = mark.side === "buy" ? "多方触发"
              : mark.side === "risk" ? "风控" : "空方触发";
            const price = mark.side === "buy"
              ? (mark.entry ?? mark.price) : (mark.stop_loss ?? mark.price);
            return `${mark.date} ${kind} ${mark.code} ${mark.name}`.trim()
              + (price !== null && price !== undefined
                ? `（${mark.side === "buy" ? "触发价" : "止损/参考"} `
                  + `${price.toFixed(2)}）` : "");
          }).join("\n");
          return (
            <g key={`${date}-${isBuy ? "buy" : "sell"}`}>
              <path d={path} fill={color} opacity={0.92} data-signals={caption}>
                <title>{title}</title>
              </path>
              {/* 文字一律放在三角**远离K线**的一侧，免得压住蜡烛；
                  排不开时不画（见 markerGroups 里的说明） */}
              {group.showText !== false && (
                <text x={cx} y={center + (isBuy ? 16 : -9)} textAnchor="middle"
                      fontSize="8.5" fill={color} stroke="var(--panel)"
                      strokeWidth="2.5" paintOrder="stroke"
                      style={{ pointerEvents: "none" }}>
                  {caption}
                  <title>{title}</title>
                </text>
              )}
            </g>
          );
        })}

        {/* 区间测量的选区：半透明底 + 两条边界线（画在最上层，压住蜡烛与标记） */}
        {measure && (
          <g style={{ pointerEvents: "none" }}>
            <rect x={x(measure.start)} y={PAD.top}
                  width={Math.max(1, x(measure.end) - x(measure.start))}
                  height={(showVolume ? volumeTop + volumeH : PAD.top + priceH) - PAD.top}
                  fill="var(--accent)" opacity={0.14} />
            {[measure.start, measure.end].map((index, i) => (
              <line key={i} x1={x(index)} x2={x(index)} y1={PAD.top}
                    y2={showVolume ? volumeTop + volumeH : PAD.top + priceH}
                    stroke="var(--accent)" strokeWidth="1" opacity={0.85} />
            ))}
          </g>
        )}

        {/* 十字光标（用户口径 2026-09-23）：
            竖线钉在**光标所在的那根bar**（对应 X 轴日期），横线跟**鼠标**走
            （对应 Y 轴价格，右侧轴上给标签）；两条线的读数在下面的浮层里。 */}
        {hover && (
          <g style={{ pointerEvents: "none" }}>
            <line x1={x(hover.index)} x2={x(hover.index)}
                  y1={PAD.top} y2={showVolume ? volumeTop + volumeH : PAD.top + priceH}
                  stroke="var(--muted)" strokeDasharray="3 3" opacity={0.75} />
            <line x1={PAD.left} x2={W - PAD.right} y1={hover.vy} y2={hover.vy}
                  stroke="var(--accent)" strokeDasharray="3 3" opacity={0.9} />
            {/* Y 轴价格标签：贴在右侧价格轴上，跟着横线上下走 */}
            <rect x={W - PAD.right + 2} y={hover.vy - 8}
                  width={PAD.right - 8} height={16} rx={3}
                  fill="var(--accent)" opacity={0.92} />
            <text x={W - PAD.right + 6} y={hover.vy + 3.5} fontSize={10}
                  fill="#fff">
              {hover.price.toFixed(2)}
            </text>
            {/* X 轴日期标签：盖住该位置的刻度，明确"当前是哪一天" */}
            <rect x={x(hover.index) - 27} y={height - PAD.bottom + 3}
                  width={54} height={15} rx={3}
                  fill="var(--accent)" opacity={0.92} />
            <text x={x(hover.index)} y={height - PAD.bottom + 14} fontSize={10}
                  fill="#fff" textAnchor="middle">
              {bars[hover.index].date.slice(5)}
            </text>
          </g>
        )}
      </svg>

      {/* 光标浮层读数：跟着鼠标实时更新（日期 / Y 轴价格 / 当日K线的高低价、量、换手、资金）。
          `pointer-events: none` 是关键 —— 否则它自己会吃掉 mousemove，
          光标移到框上就"跳"成停在框边缘。框选过程中先让位给区间测量框。 */}
      {hover && hoverBar && !measure?.dragging && (
        <div className="niuline-hover mono"
             style={{ left: hover.left, top: hover.top }}>
          <div className="niuline-hover-date">{hoverBar.date}</div>
          <div className="niuline-hover-row">
            <span className="muted-text">光标价</span>
            <b>{hover.price.toFixed(2)}</b>
          </div>
          <div className="niuline-hover-row">
            <span className="muted-text">开/收</span>
            <span>{hoverBar.open.toFixed(2)} / {hoverBar.close.toFixed(2)}</span>
          </div>
          <div className="niuline-hover-row">
            <span className="muted-text">最高</span>
            <span>{hoverBar.high.toFixed(2)}</span>
          </div>
          <div className="niuline-hover-row">
            <span className="muted-text">最低</span>
            <span>{hoverBar.low.toFixed(2)}</span>
          </div>
          <div className="niuline-hover-row">
            <span className="muted-text">涨跌幅</span>
            <span className={(hoverBar.pct_chg ?? 0) >= 0 ? "up" : "down"}>
              {hoverBar.pct_chg === null || hoverBar.pct_chg === undefined
                ? "—"
                : `${hoverBar.pct_chg >= 0 ? "+" : ""}${hoverBar.pct_chg.toFixed(2)}%`}
            </span>
          </div>
          <div className="niuline-hover-row">
            <span className="muted-text">量</span>
            <span>{formatVolume(hoverBar.volume)}</span>
          </div>
          <div className="niuline-hover-row">
            <span className="muted-text">换手</span>
            <span className={hoverBar.turnover === null ? "muted-text" : ""}>
              {hoverBar.turnover === null || hoverBar.turnover === undefined
                ? "—" : `${hoverBar.turnover.toFixed(2)}%`}
            </span>
          </div>
          <div className="niuline-hover-row"
               title="主力资金净流入额（本地行情仓日频定稿，T-1；当日那根形成中bar没有该值）">
            <span className="muted-text">资金</span>
            <span className={(hoverBar.net_mf ?? 0) >= 0 ? "up" : "down"}>
              {hoverBar.net_mf === null || hoverBar.net_mf === undefined
                ? "—" : formatMoney(hoverBar.net_mf)}
            </span>
          </div>
        </div>
      )}

      {/* 区间测量结果（用户口径 2026-09-23）：拖出来的那一段的统计量。
          位置：默认贴在选区上方居中，越界就夹回容器内。 */}
      {measureResult && (
        <div className="niuline-measure mono"
             style={{ left: measureLeft, top: measureTop }}>
          <div className="niuline-measure-head">
            <span>{measureResult.start} ~ {measureResult.end}</span>
            <button className="niuline-measure-close" title="清除区间（Esc）"
                    onClick={() => setMeasure(null)}>×</button>
          </div>
          <div className="niuline-hover-row">
            <span className="muted-text">区间交易日</span>
            <b>{measureResult.days} 天</b>
          </div>
          <div className="niuline-hover-row">
            <span className="muted-text">区间涨跌</span>
            <b className={(measureResult.pct ?? 0) >= 0 ? "up" : "down"}>
              {measureResult.pct === null ? "—"
                : `${measureResult.pct >= 0 ? "+" : ""}${measureResult.pct.toFixed(2)}%`}
            </b>
          </div>
          <div className="niuline-hover-row">
            <span className="muted-text">区间最高/最低</span>
            <span>{measureResult.high.toFixed(2)} / {measureResult.low.toFixed(2)}</span>
          </div>
          <div className="niuline-hover-row"
               title={measureResult.turnoverCount === measureResult.days
                 ? "区间内每日换手率之和（本地行情仓日频定稿）"
                 : `区间内 ${measureResult.turnoverCount}/${measureResult.days} 根有换手率`
                   + "（当日那根形成中bar与停牌日没有）"}>
            <span className="muted-text">换手合计</span>
            <span className={measureResult.turnoverCount ? "" : "muted-text"}>
              {measureResult.turnoverCount
                ? `${measureResult.turnoverSum.toFixed(2)}%`
                  + (measureResult.turnoverCount < measureResult.days
                    ? `（${measureResult.turnoverCount}/${measureResult.days}根）` : "")
                : "—"}
            </span>
          </div>
          <div className="niuline-hover-row"
               title={"主力资金净流入额合计（本地行情仓 moneyflow 日频定稿）；"
                      + "正值=净流入，负值=净流出"}>
            <span className="muted-text">
              {measureResult.flowCount === 0 ? "资金净流入"
                : measureResult.flowSum >= 0 ? "区间净流入" : "区间净流出"}
            </span>
            <span className={measureResult.flowCount === 0 ? "muted-text"
              : measureResult.flowSum >= 0 ? "up" : "down"}>
              {measureResult.flowCount === 0 ? "—"
                : formatMoney(Math.abs(measureResult.flowSum))}
            </span>
          </div>
        </div>
      )}

      {/* 读数条：光标/最新一根的精确数值。蜡烛已经画回来了，这里仍然保留 ——
          图上读不出小数点后两位，且缩放到很密时蜡烛只有 1px。 */}
      {shownBar && (
        <div className="niuline-readout mono">
          <span className="muted-text">{hover ? "光标" : "最新"}</span>
          <span>{shownBar.date}</span>
          <span>开 {shownBar.open.toFixed(2)}</span>
          <span>高 {shownBar.high.toFixed(2)}</span>
          <span>低 {shownBar.low.toFixed(2)}</span>
          <span>收 {shownBar.close.toFixed(2)}</span>
          <span className={
            (shownBar.pct_chg ?? 0) >= 0 ? "up" : "down"}>
            {shownBar.pct_chg === null || shownBar.pct_chg === undefined
              ? "—" : `${shownBar.pct_chg >= 0 ? "+" : ""}${shownBar.pct_chg.toFixed(2)}%`}
          </span>
          {LINE_ORDER.map((key) => {
            const value = shown ? shown[key as keyof typeof shown] : null;
            return (
              <span key={key} style={{ color: LINE_STYLE[key]?.color }}>
                {labelFor(key)} {typeof value === "number" ? value.toFixed(2) : "—"}
              </span>
            );
          })}
        </div>
      )}
    </div>
  );
}

export default memo(NiuLineChart);
