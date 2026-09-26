import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  sectorCrowdingApi,
  type CrowdingListItem,
} from "../sectorCrowdingApi";

/**
 * 全板块水位总览（散点图）+ **选中删除 / 新增板块**。
 *
 * ## 为什么是散点而不是柱状/排行
 *
 * 要回答的问题是"**哪些板块同时处于历史高位**"。上千个板块排成柱状图没法看，
 * 而散点（X = 成交额、Y = 水位）能一眼分出四个象限：
 * 右上 = 又大又挤（最该警惕）、左上 = 小但极挤（可能是新热点）。
 *
 * ## 只画清单里的板块
 *
 * 数据源是 `sector_crowding_list`（与告警面板**同一份**清单），所以在这里删掉
 * 的点不会出现在告警面板，反之亦然。板块多到看不清时，用选中删除把不关心的
 * 剔出去即可 —— 删的是**看板显示**，历史拥挤度数据仍在库里。
 *
 * ## 交互
 *
 * - **单击圆点 = 选中/取消**（不直接打开详情，避免想多选时误跳弹窗）
 * - Ctrl / ⌘ + 单击 = 逐个加选；Shift + 单击 = 从上次选中的点起整段加选
 * - 空白处拖拽 = **框选**（框内点全部加选）
 * - 双击圆点（或列表里的"查看详情"）= 打开详情弹窗
 * - 滚轮缩放 X 轴、拖拽平移
 */

const W = 960;
const H = 340;
/**
 * 右边距留得比一般图表大：X > 10 亿的圆点要在**右侧**标出板块名。
 * 原来的 74px 只够放"水位"两个字和一个刻度标签，标签会全被裁掉。
 */
const PAD = { top: 16, right: 186, bottom: 36, left: 62 };

const THRESHOLD_COLOR = "#e0b020";
const HIGH_COLOR = "#d95c4a";
const NORMAL_COLOR = "#4a9eff";
const SELECT_COLOR = "#9b7bff";

/**
 * 成交额超过这个数（**亿元**）就在圆点右侧标出板块名。
 * 目的是让"又大又挤"那批板块不用悬停就能认出来 —— 右上角是最该警惕的区域。
 */
const LABEL_MIN_YI = 10;
/**
 * 拥挤度水位超过这个**百分比**也标出板块名（不论成交额大小）。
 *
 * 80% 是面板的告警阈值，所以这条等价于"**进入告警区的板块全部标名**"。
 * 为什么不能只看成交额：实测水位 > 80% 的有 18 个，其中 6 个成交额不足 10 亿
 * （超导概念 96.8%/3.7亿、PET铜箔 94.8%/5.8亿、培育钻石 90.1%/1.5亿…）——
 * 这些"小但极挤"的点在散点图左上角，恰恰是最该被认出来的，只按成交额筛会全漏。
 */
const LABEL_MIN_WATER_PCT = 80;
/** 标签字号（需求明确要求"字体不要太大"） */
const LABEL_FONT = 10;
/** 单个汉字约占字号宽度的比例（用于估算标签宽度，中文近似 1:1） */
const LABEL_CHAR_W = LABEL_FONT;
/** 标签与圆点的水平间距 */
const LABEL_GAP = 6;
/** 标签行高（碰撞避让的最小间距） */
const LABEL_LINE_H = LABEL_FONT + 2;
/**
 * 每个标签最多尝试几档错位（0, ±1, ±2, ±3 行）。
 *
 * 为什么要有上限：成交额过线的板块数远超纵向可容纳的行数（实测 59 个候选只有
 * 23 行空间），无限尝试只会让后面的标签越飘越远、最后和它的圆点完全脱节。
 * 试满还放不下就放弃标注 —— 见 `labels` 的说明。
 *
 * 取 7 是实测的平衡点（`LABEL_LINE_H=12`）：
 *
 * | 档位 | 标注数 | 最大漂移 |
 * |---|---|---|
 * | 1  | 25 | 0px |
 * | 7  | 40 | 36px |
 * | 13 | 44 | 72px |
 * | 21 | 47 | 120px |
 *
 * 从 7 加到 13 只多标 4 个，最大漂移却翻倍 —— 标签离圆点超过 3 行就认不出
 * 对应哪个点了，反而更难看懂。宁可少标几个。
 */
const LABEL_TRIES = 7;

function toYi(value: number | null | undefined): number | null {
  if (value === null || value === undefined || !Number.isFinite(value)) return null;
  return value / 1e8;
}

export default function SectorCrowdingOverview({
  items, threshold, highThreshold, onPick, onRemoveMany, onAdd, onNotice,
  maxMa5,
}: {
  items: CrowdingListItem[];
  threshold: number;
  highThreshold: number;
  onPick: (code: string, name: string) => void;
  onRemoveMany: (codes: string[]) => Promise<number>;
  onAdd: (code: string, name: string, pinned?: boolean) => Promise<unknown>;
  onNotice?: (text: string) => void;
  maxMa5: Record<string, number>;
}) {
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState(0);
  const [hover, setHover] = useState<CrowdingListItem | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [keyword, setKeyword] = useState("");
  const [options, setOptions] = useState<Record<string, unknown>[]>([]);
  const [searching, setSearching] = useState(false);
  const [marquee, setMarquee] = useState<
    { x0: number; y0: number; x1: number; y1: number } | null>(null);
  const svgRef = useRef<SVGSVGElement | null>(null);
  const dragRef = useRef<{ mode: "marquee" | "pan"; x: number; y: number;
                            pan: number } | null>(null);
  const lastPickedRef = useRef<string | null>(null);

  /** 只画有水位的点：NULL 的点在 Y 轴上没有位置，"数据不足"另用文字说明。 */
  const points = useMemo(() => items
    .filter((row) => row.water_level !== null
      && Number.isFinite(row.water_level as number)
      && row.sector_amount !== null && Number.isFinite(row.sector_amount as number))
    .map((row) => ({
      row,
      amount: toYi(row.sector_amount) ?? 0,
      water: (row.water_level as number) * 100,
    })), [items]);

  const missing = items.length - points.length;

  // 清单变了（删除/新增）就把已经不存在的选中项清掉，避免"删完了计数还在"
  useEffect(() => {
    setSelected((current) => {
      if (current.size === 0) return current;
      const alive = new Set(items.map((item) => item.sector_code));
      const next = new Set([...current].filter((code) => alive.has(code)));
      return next.size === current.size ? current : next;
    });
  }, [items]);

  const xDomain = useMemo(() => {
    if (points.length === 0) return { min: 0, max: 1 };
    const values = points.map((item) => item.amount).sort((a, b) => a - b);
    const max = Math.max(1, values[values.length - 1]);
    const full = { min: 0, max };
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

  /** 屏幕坐标 → viewBox 坐标（框选要用）。 */
  const toLocal = useCallback((clientX: number, clientY: number) => {
    const rect = svgRef.current?.getBoundingClientRect();
    if (!rect) return null;
    return {
      x: (clientX - rect.left) / rect.width * W,
      y: (clientY - rect.top) / rect.height * H,
    };
  }, []);

  const pickPoint = useCallback((
    event: React.MouseEvent, code: string) => {
    // 双击的第 1 次 click 也会走到这里：先选中再打开弹窗、关掉后发现自己被选中，
    // 很莫名其妙。所以第二次 click 直接不处理（选中状态保持第一次的结果）。
    if (event.detail >= 2) return;
    setSelected((current) => {
      const next = new Set(current);
      if (event.shiftKey && lastPickedRef.current) {
        // 段选：按当前绘制顺序取两个点之间的所有点
        const codes = points.map((item) => item.row.sector_code);
        const from = codes.indexOf(lastPickedRef.current);
        const to = codes.indexOf(code);
        if (from >= 0 && to >= 0) {
          const [lo, hi] = from <= to ? [from, to] : [to, from];
          for (let index = lo; index <= hi; index += 1) next.add(codes[index]);
          return next;
        }
      }
      if (event.ctrlKey || event.metaKey) {
        if (next.has(code)) next.delete(code); else next.add(code);
      } else {
        // 普通单击 = 单选切换（再点一次取消）
        if (next.size === 1 && next.has(code)) next.clear();
        else { next.clear(); next.add(code); }
      }
      return next;
    });
    lastPickedRef.current = code;
  }, [points]);

  const yTicks = useMemo(() => Array.from({ length: 6 }, (_, index) =>
    yDomain.min + ((yDomain.max - yDomain.min) * index) / 5), [yDomain]);
  const xTicks = useMemo(() => Array.from({ length: 6 }, (_, index) =>
    xDomain.min + ((xDomain.max - xDomain.min) * index) / 5), [xDomain]);

  /**
   * 圆点右侧的板块名标签。
   *
   * ## 标注条件：成交额大 **或** 水位高
   *
   * - `amount > LABEL_MIN_YI`（10 亿）：成交额大的板块，即"大票"；
   * - `water > LABEL_MIN_WATER_PCT`（80%）：**拥挤度水位进入告警区**的板块，
   *   哪怕它成交额很小 —— 实测这类有 18 个，其中 6 个成交额不足 10 亿
   *   （超导概念 96.8%、PET铜箔 94.8%、培育钻石 90.1%…）。它们是"小但极挤"
   *   那一类，正是散点图左上角最该被看见的点，光靠成交额条件会全部漏掉。
   *
   * ## 为什么是"贪心放置、放不下就跳过"，而不是硬挤
   *
   * 实测过：候选（两个条件的并集）有 65 个，标签行高 12px，需要
   * `65 × 12 = 780px` 的纵向空间；而绘图区只有 278px —— 只能容纳 23 行。
   * 所以**物理上就不可能全标**，任何"把重叠的往下推"的算法最后都会在下边界
   * 堆成一坨（第一版实测 59 个标签里 390 对重叠）。
   *
   * 换成贪心放置：
   *
   * 1. 按**优先级**处理：先水位 > 80% 的（那是告警条件，最该被看见），
   *    再成交额大的；同一档内按数值降序；
   * 2. 每个标签先试原位，不行就上下交替错开（最多 `LABEL_TRIES` 档）；
   * 3. 所有位置都冲突就**放弃这个标签**，而不是叠上去。
   *
   * 结果宁可少标几个，也不出现读不出来的重叠。标题行会写出
   * "已标注 N 个（M 个因空间不足未标）"，不会让人误以为漏了。
   *
   * 标签只按 X/Y 定位，不影响散点本身；数量级是几十，O(n²) 判重叠完全够用。
   */
  const labels = useMemo(() => {
    const wanted = points
      .filter((item) => item.amount > LABEL_MIN_YI
        || item.water > LABEL_MIN_WATER_PCT)
      .sort((left, right) => {
        // 高水位（告警区）优先占位；同为高水位则水位高的先，否则成交额大的先
        const leftHot = left.water > LABEL_MIN_WATER_PCT ? 1 : 0;
        const rightHot = right.water > LABEL_MIN_WATER_PCT ? 1 : 0;
        if (leftHot !== rightHot) return rightHot - leftHot;
        return leftHot ? right.water - left.water : right.amount - left.amount;
      });

    const estimateWidth = (text: string): number => {
      let width = 0;
      for (const char of text) {
        width += /[\u4e00-\u9fa5\uff00-\uffef]/.test(char)
          ? LABEL_CHAR_W : LABEL_CHAR_W * 0.55;
      }
      return width;
    };

    const bottomLimit = H - PAD.bottom;
    const placed: {
      key: string; text: string; width: number; toRight: boolean;
      x: number; y: number; anchorY: number;
    }[] = [];

    for (const item of wanted) {
      const cx = x(item.amount);
      const cy = y(item.water);
      const text = item.row.sector_name || item.row.sector_code;
      const width = estimateWidth(text);
      // 右侧放得下就放右边，否则翻到左边。
      // 右侧留 8px 余量：宽度是**估算**的（中英文字宽不同），不留余量时个别
      // 长名字（如"数据中心(AIDC)"）会顶出画布被裁掉。
      const toRight = cx + LABEL_GAP + width <= W - 8;
      const anchorX = toRight ? cx + LABEL_GAP : cx - LABEL_GAP;
      const anchorY = cy + LABEL_FONT * 0.36;
      const left = toRight ? anchorX : anchorX - width;
      const right = toRight ? anchorX + width : anchorX;

      let landing: number | null = null;
      for (let step = 0; step < LABEL_TRIES && landing === null; step += 1) {
        // 上下交替错开：0, -1, +1, -2, +2 …
        const offset = step === 0
          ? 0 : (step % 2 === 1 ? -1 : 1) * Math.ceil(step / 2) * LABEL_LINE_H;
        const y = anchorY + offset;
        if (y - LABEL_FONT * 0.36 < PAD.top || y > bottomLimit) continue;
        const clash = placed.some((other) => {
          const oLeft = other.toRight ? other.x : other.x - other.width;
          const oRight = other.toRight ? other.x + other.width : other.x;
          if (!(left < oRight && oLeft < right)) return false;   // 水平不相交
          return Math.abs(other.y - y) < LABEL_LINE_H - 0.5;     // 垂直太近
        });
        if (!clash) landing = y;
      }
      if (landing === null) continue;   // 放不下就跳过，不叠上去
      placed.push({ key: item.row.sector_code, text, width, toRight,
                    x: anchorX, y: landing, anchorY });
    }
    return {
      items: placed,
      /** 符合标注条件的总数（成交额大 或 水位高） */
      candidates: wanted.length,
      /** 因空间不足没标出来的数量（标题里要如实说明，否则像漏了） */
      skipped: wanted.length - placed.length,
    };
  }, [points, x, y]);

  // 搜索新增（与告警面板同款联想）
  useEffect(() => {
    const text = keyword.trim();
    if (!text) { setOptions([]); return; }
    let alive = true;
    const timer = window.setTimeout(() => {
      setSearching(true);
      sectorCrowdingApi.search(text, 20)
        .then((payload) => { if (alive) setOptions(payload.sectors); })
        .catch(() => { if (alive) setOptions([]); })
        .finally(() => { if (alive) setSearching(false); });
    }, 300);
    return () => { alive = false; window.clearTimeout(timer); };
  }, [keyword]);

  const existing = useMemo(
    () => new Set(items.map((item) => item.sector_code)), [items]);

  const deleteSelected = useCallback(async () => {
    const codes = [...selected];
    if (codes.length === 0) return;
    setBusy(true);
    try {
      const removed = await onRemoveMany(codes);
      setSelected(new Set());
      onNotice?.(`已从清单删除 ${removed} 个板块（历史拥挤度数据仍保留）`);
    } catch (exc) {
      onNotice?.(`删除失败：${exc instanceof Error ? exc.message : String(exc)}`);
    } finally {
      setBusy(false);
    }
  }, [selected, onRemoveMany, onNotice]);

  const addSector = useCallback(async (code: string, name: string) => {
    setBusy(true);
    try {
      await onAdd(code, name);
      onNotice?.(`已加入清单：${name || code}`);
      setKeyword("");
      setOptions([]);
    } catch (exc) {
      onNotice?.(`加入失败：${exc instanceof Error ? exc.message : String(exc)}`);
    } finally {
      setBusy(false);
    }
  }, [onAdd, onNotice]);

  const resetView = useCallback(() => { setZoom(1); setPan(0); }, []);

  if (items.length === 0) {
    return (
      <div className="info-box">
        清单是空的。用下方搜索框添加板块，或在告警面板点「重置清单」回到默认全量。
      </div>
    );
  }

  return (
    <div className="crowding-overview">
      <div className="chart-toolbar">
        <span className="muted-text">
          单击圆点选中 · Ctrl/⌘ 加选 · Shift 段选 · 空白处拖拽框选 ·
          双击圆点看详情
        </span>
        <button className="btn-ghost chart-btn"
                onClick={() => setZoom((value) => Math.min(20, value * 1.3))}>＋</button>
        <button className="btn-ghost chart-btn"
                onClick={() => setZoom((value) => Math.max(1, value / 1.3))}>－</button>
        <button className="btn-ghost chart-btn" onClick={resetView}
                disabled={zoom === 1 && pan === 0}>复位</button>
        <span style={{ flex: 1 }} />
        <span className="muted-text">
          共 {items.length} 个板块在清单 · {points.length} 个有水位
          {missing > 0 && ` · ${missing} 个数据不足`}
          {labels.items.length > 0 && (
            <>
              {" · 已标注 "}
              <b>{labels.items.length}</b>
              {" 个板块名（成交额 > "}{LABEL_MIN_YI}{" 亿 或 水位 > "}
              {LABEL_MIN_WATER_PCT}{"%）"}
              {labels.skipped > 0 && (
                <span title={"绘图区纵向只有约 23 行标签位，而符合条件的板块有 "
                  + labels.candidates + " 个；放不下的会被跳过而不是叠在一起，"
                  + "其中高水位（告警区）的板块优先占位。"}>
                  （{labels.skipped} 个空间不足未标）
                </span>
              )}
            </>
          )}
        </span>
      </div>

      <div className="chart-toolbar">
        <div className="crowding-search">
          <input className="qsel-input" value={keyword}
                 placeholder="搜索板块名称/代码并加入清单"
                 onChange={(event) => setKeyword(event.target.value)} />
          {searching && <span className="muted-text">查询中…</span>}
          {options.length > 0 && (
            <ul className="crowding-suggest">
              {options.map((option) => {
                const code = String(option.sector_code ?? "");
                const name = String(option.sector_name ?? "");
                const bars = Number(option.bars ?? 0);
                const already = existing.has(code);
                return (
                  <li key={code}>
                    <button disabled={busy} onClick={() => void addSector(code, name)}>
                      <span>{name || code}</span>
                      <span className="mono muted-text"> {code}</span>
                      <span className="muted-text">
                        {" "}{bars > 0 ? `${bars} 根` : "未刷新"}
                        {option.is_concept ? "" : " · 非概念"}
                      </span>
                      <span className={already ? "muted-text" : "crowding-done"}>
                        {" "}{already ? "（已在清单）" : "＋ 加入"}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
        <span className="muted-text">
          {selected.size > 0 ? `已选中 ${selected.size} 个` : "未选中板块"}
        </span>
        <button className="btn-ghost" disabled={selected.size === 0 || busy}
                title="把选中的板块从看板清单删除（历史数据保留）"
                onClick={() => void deleteSelected()}>
          删除选中{selected.size > 0 ? `（${selected.size}）` : ""}
        </button>
        <button className="btn-ghost" disabled={selected.size === 0}
                onClick={() => setSelected(new Set())}>取消选择</button>
        <button className="btn-ghost"
                disabled={selected.size === 0}
                title="把选中的板块置顶（两个面板都排最前）"
                onClick={() => {
                  void (async () => {
                    setBusy(true);
                    try {
                      for (const code of selected) {
                        await sectorCrowdingApi.listPin(code, true);
                      }
                      onNotice?.(`已置顶 ${selected.size} 个板块`);
                    } catch (exc) {
                      onNotice?.(`置顶失败：${exc instanceof Error
                        ? exc.message : String(exc)}`);
                    } finally { setBusy(false); }
                  })();
                }}>置顶选中</button>
      </div>

      <svg ref={svgRef} viewBox={`0 0 ${W} ${H}`} className="intraday-svg"
           role="img"
           onWheel={onWheel}
           onMouseDown={(event) => {
             const local = toLocal(event.clientX, event.clientY);
             if (!local) return;
             // 空白处按下 = 框选；按住 Alt 拖 = 平移（框选和平移都要用左键拖，
             // 只能靠修饰键区分，默认给更常用的框选）
             const mode = event.altKey ? "pan" : "marquee";
             dragRef.current = { mode, x: event.clientX, y: event.clientY, pan };
             if (mode === "marquee") {
               setMarquee({ x0: local.x, y0: local.y, x1: local.x, y1: local.y });
             }
           }}
           onMouseMove={(event) => {
             const drag = dragRef.current;
             if (!drag) return;
             if (drag.mode === "marquee") {
               const local = toLocal(event.clientX, event.clientY);
               if (local) {
                 setMarquee((current) => current
                   ? { ...current, x1: local.x, y1: local.y } : current);
               }
               return;
             }
             const rect = svgRef.current?.getBoundingClientRect();
             if (!rect) return;
             const delta = (drag.x - event.clientX) / rect.width;
             setPan(Math.max(0, drag.pan + delta * (xDomain.max - xDomain.min)
               / Math.max(1e-9, xDomain.max || 1)));
           }}
           onMouseUp={() => {
             const drag = dragRef.current;
             dragRef.current = null;
             if (drag?.mode === "marquee" && marquee) {
               const x0 = Math.min(marquee.x0, marquee.x1);
               const x1 = Math.max(marquee.x0, marquee.x1);
               const y0 = Math.min(marquee.y0, marquee.y1);
               const y1 = Math.max(marquee.y0, marquee.y1);
               const hit = points.filter((item) => {
                 const px = x(item.amount); const py = y(item.water);
                 return px >= x0 && px <= x1 && py >= y0 && py <= y1;
               }).map((item) => item.row.sector_code);
               if (hit.length > 0) {
                 setSelected((current) => new Set([...current, ...hit]));
               }
             }
             setMarquee(null);
           }}
           onMouseLeave={() => {
             dragRef.current = null; setMarquee(null); setHover(null);
           }}
           onDoubleClick={resetView}>
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

        {/* 成交额 > 10 亿的板块名（小字，跟随圆点） */}
        {labels.items.map((item) => (
          <text key={`lb-${item.key}`}
                x={item.x} y={item.y}
                fontSize={LABEL_FONT}
                fill="var(--muted)"
                textAnchor={item.toRight ? "start" : "end"}
                className="crowding-point-label"
                pointerEvents="none">
            {item.text}
          </text>
        ))}

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
          const isSelected = selected.has(item.row.sector_code);
          const color = isSelected ? SELECT_COLOR
            : water >= highThreshold * 100 ? HIGH_COLOR
              : water >= threshold * 100 ? THRESHOLD_COLOR : NORMAL_COLOR;
          const active = hover?.sector_code === item.row.sector_code;
          const pinned = item.row.pinned;
          return (
            <circle key={item.row.sector_code}
                    cx={x(item.amount)} cy={y(water)}
                    r={isSelected ? 7 : active ? 6 : 3.5}
                    fill={color}
                    stroke={isSelected ? "var(--text)" : "none"}
                    strokeWidth={isSelected ? 1.2 : 0}
                    opacity={isSelected || active ? 1 : 0.78}
                    style={{ cursor: "pointer" }}
                    onMouseEnter={() => setHover(item.row)}
                    onClick={(event) => {
                      event.stopPropagation();
                      pickPoint(event, item.row.sector_code);
                    }}
                    onDoubleClick={() => onPick(
                      item.row.sector_code, item.row.sector_name)}>
              <title>
                {`${item.row.sector_name}${pinned ? " 📌" : ""}`
                  + `\n水位 ${water.toFixed(1)}%`
                  + `\n当前平滑拥挤度 ${item.row.ma5_crowding === null
                    ? "—" : (item.row.ma5_crowding * 100).toFixed(3)}`
                  + `\n近6年最高 ${maxMa5[item.row.sector_code] === undefined
                    ? "—" : (maxMa5[item.row.sector_code] * 100).toFixed(3)}`
                  + `\n成交额 ${item.amount.toFixed(0)}亿`
                  + `\n更新 ${item.row.trade_date}`
                  + "\n（单击选中 / 双击看详情）"}
              </title>
            </circle>
          );
        })}

        {marquee && (
          <rect x={Math.min(marquee.x0, marquee.x1)}
                y={Math.min(marquee.y0, marquee.y1)}
                width={Math.abs(marquee.x1 - marquee.x0)}
                height={Math.abs(marquee.y1 - marquee.y0)}
                fill="rgba(155,123,255,0.15)" stroke={SELECT_COLOR}
                strokeDasharray="4 3" pointerEvents="none" />
        )}

        {hover && (
          <g>
            <circle cx={x(toYi(hover.sector_amount) ?? 0)}
                    cy={y((hover.water_level ?? 0) * 100)} r={9}
                    fill="none" stroke="var(--text)" strokeWidth="1" opacity={0.8} />
          </g>
        )}
      </svg>

      {hover && (
        <div className="crowding-hover mono">
          <b>{hover.sector_name}{hover.pinned ? " 📌" : ""}</b>
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
