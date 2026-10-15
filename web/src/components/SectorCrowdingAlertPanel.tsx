import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { filterBoards, pinyinHint, suggestBoards } from "../boardSearch";
import { readBoardFilter, writeBoardFilter } from "../boardFilterCache";
import { createPortal } from "react-dom";
import {
  sectorCrowdingApi,
  type CrowdingAlertRow,
  type CrowdingListItem,
  type CrowdingMetricsSummary,
} from "../sectorCrowdingApi";
import SectorCrowdingAlertEditor from "./SectorCrowdingAlertEditor";

/**
 * 高拥挤度告警面板 = **清单全量 + 告警标红**。
 *
 * ## 为什么不是"只显示 ≥80% 的那几行"
 *
 * 原来的实现只渲染触发告警的板块（当天 28 个），于是这个面板既看不到"快到线"
 * 的板块，也没法在此增删 —— 用户要管的是"**我关注的一批板块现在挤不挤**"，
 * 而不是"全市场今天谁触发阈值"。改成清单全量后：
 *
 * - 水位 ≥ 80%：整行红色（`tone-high` / `tone-warn`，与散点图同一口径）
 * - 未到线：正常色，但仍在列表里，能一眼看出"离告警还有多远"
 * - 置顶（📌）的板块永远排最前，与水位无关
 *
 * ## 增删置顶都落库
 *
 * 三个操作都写 `sector_crowding_list`（服务端持久化），返回体里带整份新清单；
 * 因此**下次打开页面读到的就是上次的配置**，不依赖 localStorage。
 *
 * ## 4 列周频异动指标（查看详情左侧）
 *
 * 近5日/近1月/近2月拥挤度水位变化 + 近1月资金净流入占流通市值比，用来辅助
 * 判断大资金建仓异动。这 4 列由后端**每周算一次并落库**（`metrics.py`），
 * 前端只读不算。
 *
 * ## 为什么是"表头 + 纯数字单元格"而不是每个格子写列名
 *
 * 列名放在**顶部表头行**（`.crowding-alert-header`），格子内只留数字：
 * 一行 12 列、每格再重复一遍"近5日"，横向空间全被标签吃掉、数字反而看不清。
 * 表头与数据行共用同一份列宽（CSS 变量 `--crowding-grid`），所以能严格对齐 ——
 * 这也意味着**行容器必须是 grid 而不是 flex**：flex 每行独立算宽度，表头对不上。
 * 每列表头都可点击排序（同列再点切升降序）。
 *
 * 口径细节：变化的基准是**各窗口起点那天的水位**，不是"20 天前的今天"——
 * 窗口按交易日数，且资金流末端日常比拥挤度晚 2 天，所以每列表头 hover 会给出
 * 实际用的两个日期，避免把滞后的数当成当天的。
 */

type SortKey = "rank" | "water" | "alert" | "name" | "amount"
  | "chg_5d" | "chg_1m" | "chg_2m" | "flow_ratio";

type SortDir = "desc" | "asc";

function pct(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${(value * 100).toFixed(1)}%`;
}

/**
 * 水位单元格的文字。
 *
 * ⚠️ 「水位为空」有两种完全不同的成因，不能都显示成一个"—"：
 *
 *   ① 板块**日线不足** `minBarsForWaterLevel`（后端 750 ≈ 3 年）
 *      → 显示「数据不足 563/750」。这是**有意的样本量门槛，不是故障**：
 *        水位 = 当前拥挤度 / **近 6 年最高值**，历史太短时"自己的历史最高"
 *        就是最近这几天，水位恒为 100%、一上线就误告警。
 *        用户 2026-09-22 正是被这个空白误导，问「商业航天、人形机器人的
 *        水位为什么是空的」。
 *   ② 其它情况（该板块当天数据缺失）→ 仍然显示"—"。
 */
function waterText(value: number | null | undefined,
                   bars: number | null | undefined,
                   minBars: number | undefined): string {
  if (value !== null && value !== undefined && Number.isFinite(value)) {
    return `${(value * 100).toFixed(1)}%`;
  }
  const count = bars ?? 0;
  if (minBars && minBars > 0 && count > 0 && count < minBars) {
    return `数据不足 ${count}/${minBars}`;
  }
  return "—";
}

/** 变化百分比 / 占比列：带正负号，1 位小数。 */
function signed(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const text = value.toFixed(1);
  return value > 0 ? `+${text}` : text;
}

/** 变化方向 → CSS class（涨绿跌红，与 A 股看板习惯一致）。 */
function deltaTone(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "flat";
  if (value > 0) return "up";
  if (value < 0) return "down";
  return "flat";
}

/**
 * 拥挤度排名：按水位（= 拥挤度水平）降序，第 1 名最拥挤。
 *
 * **在"只看告警"过滤之前的整份清单里排名**，这样勾选过滤时名次不会跳变 ——
 * 名次是板块在清单里的绝对位置，不该随视图筛选而改。
 * 水位为 NULL（数据不足）的排在最后，且并列时按板块代码定序（结果稳定，
 * 不会因排序抖动而每次刷新换一个名次）。
 */
function buildRanks(rows: { sector_code: string; water_level: number | null }[]
): Map<string, number> {
  const ordered = [...rows].sort((left, right) => {
    const a = left.water_level;
    const b = right.water_level;
    if (a === null && b === null) return left.sector_code.localeCompare(right.sector_code);
    if (a === null) return 1;
    if (b === null) return -1;
    if (b !== a) return b - a;
    return left.sector_code.localeCompare(right.sector_code);
  });
  const ranks = new Map<string, number>();
  ordered.forEach((row, index) => ranks.set(row.sector_code, index + 1));
  return ranks;
}

/** 表头与数据行**共用**的列宽（改这里就同时改了两处，不会错位）。
 *
 * 两条约束：
 * 1. 数组项数 = 实际列数，顺序也必须一致（少一项整行就串位）；
 * 2. 数值列的宽度要够放"右对齐的数字"，窄了数字会被挤到与表头错开。
 */
const GRID = [
  "46px",    // 排名
  "56px",    // 置顶（📌 / 📌✓，省掉"已置顶"文字）
  "52px",    // 操作(删除)
  "96px",    // 代码（同花顺板块代码，如 885959.TI）
  "minmax(200px, 1fr)",  // 板块（居中显示，见 .crowding-alert-name）
  "92px",    // 告警（点格子设置阈值）
  "62px",    // 水位
  "74px",    // 平滑
  "82px",    // 近6年最高
  "78px",    // 成交额
  "80px",    // 近5日
  "80px",    // 近1月
  "80px",    // 近2月
  "84px",    // 资金占比
  "78px",    // 操作(查看详情)
].join(" ");

function yi(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${(value / 1e8).toFixed(1)}亿`;
}

function raw3(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return (value * 100).toFixed(3);
}

/** 水位 → 高亮级别（红 ≥90% / 橙 80~90% / 未达线）。 */
export function waterTone(water: number | null | undefined,
                          threshold: number,
                          highThreshold: number): "high" | "warn" | "none" {
  if (water === null || water === undefined || !Number.isFinite(water)) return "none";
  if (water >= highThreshold) return "high";
  return water >= threshold ? "warn" : "none";
}

function downloadCsv(rows: CrowdingAlertRow[], nameOf: (row: CrowdingAlertRow) => string): void {
  const header = ["板块代码", "板块名称", "拥挤度水位", "当前平滑拥挤度",
                  "近6年最高拥挤度", "板块成交额", "最后更新日期"];
  const lines = [header.join(",")];
  for (const row of rows) {
    lines.push([
      row.sector_code,
      `"${nameOf(row).replace(/"/g, '""')}"`,
      row.water_level === null ? "" : (row.water_level * 100).toFixed(2),
      row.ma5_crowding ?? "",
      row.max_ma5_crowding ?? "",
      row.sector_amount ?? "",
      row.trade_date ?? "",
    ].join(","));
  }
  const blob = new Blob([`\uFEFF${lines.join("\n")}`],
                       { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `crowding_alerts_${new Date().toISOString().slice(0, 10)}.csv`;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  URL.revokeObjectURL(url);
}

export default function SectorCrowdingAlertPanel({
  items, threshold, highThreshold, tradeDate, updatedAt, maxMa5, metrics,
  monthDays = 20, loading, minBarsForWaterLevel,
  onPick, onAdd, onRemove, onTogglePin, onReset, onAlertSet, onAlertClear,
  onNotice,
}: {
  items: CrowdingListItem[];
  threshold: number;
  highThreshold: number;
  /**
   * 水位需要的最少日线根数（后端 `min_bars_for_water_level`）。
   * 板块不足这个数时水位恒为空 —— 传进来是为了把那个空格显示成
   * 「数据不足 563/750」而不是看起来像故障的"—"。
   */
  minBarsForWaterLevel?: number;
  tradeDate: string;
  updatedAt?: string;
  /** 各板块历史最高平滑拥挤度（"/sectors_max_ma5"，单独接口 + 缓存） */
  maxMa5: Record<string, number>;
  /** 周频异动指标元信息（基准日/覆盖数），用于表头 hover 与脚注 */
  metrics?: CrowdingMetricsSummary | null;
  /** "近1月"折算多少个交易日（后端可配，表头说明要与后端一致） */
  monthDays?: number;
  loading?: boolean;
  onPick: (code: string, name: string) => void;
  onAdd: (code: string, name: string, pinned?: boolean) => Promise<unknown>;
  onRemove: (code: string) => Promise<void>;
  onTogglePin: (code: string, pinned: boolean) => Promise<void>;
  onReset: () => Promise<number>;
  /** 设置告警阈值（方向 + 数值） */
  onAlertSet: (code: string, mode: "above" | "below",
               threshold: number) => Promise<void>;
  /** 清除告警阈值 */
  onAlertClear: (code: string) => Promise<void>;
  onNotice?: (text: string) => void;
}) {
  const [sortKey, setSortKey] = useState<SortKey>("water");
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  /**
   * 置顶是否"锁死位置"。
   *
   * **默认 false**：点任意列排序时，置顶板块**不插队**，全表严格按该列数值排。
   * 这是用户的明确要求 —— 原来默认 true，一排序就看到 11 个置顶板块杵在顶部、
   * 挡住真实的名次顺序，看起来就是"排序没生效"。
   *
   * 置顶配置本身始终保留在库里（`pinned` 标记、每行的 📌 样式、蓝色左边框都在），
   * 只是不参与排序；点「置顶恢复」才把它们重新固定到最前。
   *
   * 排序动作会把它重置为 false：既然用户点了某一列，他要的一定是那一列的顺序。
   */
  const [pinsActive, setPinsActive] = useState(false);
  const [keyword, setKeyword] = useState("");
  const [options, setOptions] = useState<Record<string, unknown>[]>([]);
  const [searching, setSearching] = useState(false);
  const [busy, setBusy] = useState("");
  /**
   * 筛选记录（**本地缓存**，2026-10-14 用户要求）。
   *
   * > 「筛选记录可以保留在前端缓存里，或者持久化到本地数据库，不就有了联想了吗」
   *
   * 走 localStorage（`boardFilterCache.ts`，内含"为什么不是库表"的三条理由）。
   * ⚠️ **只读一次**：`useState(() => ...)` 的惰性初值，否则每帧都去 parse 一遍
   * JSON；读失败/没记录一律回默认（读函数绝不抛）。
   *
   * ⚠️⚠️ **只恢复筛选词，不恢复「只看告警 / 只看已触发」**（2026-10-15 事故）：
   * 用户报障「高拥挤度告警 这个板块的数据怎么不展示了」—— 查实当天 `alert_on`
   * 对每一行都是 `False`（"已触发"= **0** 行），而 v1 把那两个开关也记进了缓存 ⇒
   * 前一天点过「只看已触发」，第二天打开就是**一张空表**，看着完全像"数据没了"。
   * 那两个开关是**会话内的临时视图开关**（随时可点、且能把数据藏光），不该跨会话
   * 复活；缓存结构版本 +1 顺带把 v1 的旧记录一次性丢弃（刷新即恢复）。
   */
  const [cachedFilter] = useState(() => readBoardFilter());
  const [onlyAlerts, setOnlyAlerts] = useState(false);
  /** 只看自定义告警已触发的板块 */
  const [onlyTriggered, setOnlyTriggered] = useState(false);
  /**
   * 「板块」列的**筛选词**（2026-10-14 用户要求，同一天追加全拼与"记住筛选"）。
   *
   * > 「给高度拥挤告警的数据面板中，"板块"这列加一个检索过滤功能，当前板块太多了，
   * >   想找其中一个板块要翻好久，支持联想或模糊搜索」
   * > 「1.没有全拼，你就把当前拥挤度数据清单里，所有板块字段的板块名称，都映射出
   * >   中文拼音全称字段，然后不就有了搜索联想拼音池了吗」
   *
   * 清单 262 个板块，靠滚动找要翻很久。匹配判据在 `boardSearch.ts`
   * （名称 / 代码 / 拼音首字母 / 拼音全拼四路，多词 AND，纯函数 + 单独自证）。
   */
  const [boardFilter, setBoardFilter] = useState(cachedFilter?.q ?? "");
  /** 输入框是否聚焦（决定联想下拉开不开） */
  const [filterFocused, setFilterFocused] = useState(false);
  /** 联想下拉里键盘选中的第几项（-1 = 没选） */
  const [activeSuggest, setActiveSuggest] = useState(-1);
  /** 联想下拉的固定定位（portal 挂到 body，所以要自己算坐标） */
  const [popRect, setPopRect] = useState<
    { left: number; top: number; width: number } | null>(null);
  const filterInputRef = useRef<HTMLInputElement | null>(null);
  /** 正在编辑告警的板块（null = 未打开编辑面板） */
  const [alertEdit, setAlertEdit] = useState<string | null>(null);

  // 联想：输入停顿 300ms 再查（避免每敲一个字打一次库）
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

  /** 行内显示用的合并视图（清单 + 近6年最高 + 告警判定）。 */
  const view = useMemo(() => items.map((item) => {
    const water = item.water_level;
    return {
      ...item,
      max_ma5_crowding: maxMa5[item.sector_code] ?? null,
      is_alert: water !== null && Number.isFinite(water) && water >= threshold,
      tone: waterTone(water, threshold, highThreshold),
    };
  }), [items, maxMa5, threshold, highThreshold]);

  /** 拥挤度排名（整份清单口径，不受"只看告警"影响）。 */
  const ranks = useMemo(() => buildRanks(view), [view]);

  const rows = useMemo(() => {
    let list = onlyAlerts ? view.filter((row) => row.is_alert) : [...view];
    if (onlyTriggered) list = list.filter((row) => row.alert_on);
    // ★ 「板块」列筛选（2026-10-14）：**本地过滤** —— 清单已经在内存里，
    //   打字即时生效、不发请求（那个"输入板块名称查询"的框是**加入清单**用的，
    //   两件事；判据见 `boardSearch.ts`）。
    list = filterBoards(list, boardFilter);
    /** 数值型排序键的取值器（NULL 统一沉底，不参与大小比较）。 */
    const metricOf = (row: (typeof list)[number]): number | null => {
      switch (sortKey) {
        case "rank": return ranks.get(row.sector_code) ?? null;
        case "water": return row.water_level;
        case "amount": return row.sector_amount;
        case "chg_5d": return row.chg_5d;
        case "chg_1m": return row.chg_1m;
        case "chg_2m": return row.chg_2m;
        case "flow_ratio": return row.flow_ratio;
        // 按告警阈值排；没设过的排最后（NULL 沉底规则已覆盖）
        case "alert": return row.alert_threshold;
        case "name": return null;      // 名称走下面的分支，不进这里
        default: return null;
      }
    };
    list.sort((left, right) => {
      // 置顶优先 —— 但"临时取消置顶"时这一条整条跳过，让置顶板块平等参与排序。
      // 注意不是"置顶的排在后面"，而是**完全不插队**，否则按列排序仍会在
      // 置顶/非置顶分成两段，看不出全表的真实顺序。
      if (pinsActive && left.pinned !== right.pinned) {
        return left.pinned ? -1 : 1;
      }
      if (sortKey === "name") {
        const cmp = (left.sector_name || left.sector_code).localeCompare(
          right.sector_name || right.sector_code, "zh-Hans-CN");
        return sortDir === "desc" ? -cmp : cmp;
      }
      const a = metricOf(left);
      const b = metricOf(right);
      if (a === null && b === null) return 0;
      if (a === null) return 1;          // NULL 永远排在最后
      if (b === null) return -1;
      const diff = a - b;
      return sortDir === "desc" ? -diff : diff;
    });
    return list;
  }, [view, ranks, sortKey, sortDir, onlyAlerts, onlyTriggered, pinsActive,
      boardFilter]);

  /**
   * 点表头/列头排序。
   *
   * 同列再点切升降序，换列时默认降序（先看最大的）。**任何一次排序都解除置顶的
   * 位置锁定** —— 用户点了某一列，要的就是那一列的完整顺序；置顶板块继续插队
   * 会让他看到的"第 1 名"不是该列的最大值。置顶配置不受影响，随时可恢复。
   */
  const toggleSort = useCallback((next: SortKey) => {
    setPinsActive(false);
    setSortKey((current) => {
      if (current === next) {
        setSortDir((dir) => (dir === "desc" ? "asc" : "desc"));
        return current;
      }
      setSortDir("desc");
      return next;
    });
  }, []);

  /** 当前排序列的箭头（未排序的列不显示箭头，避免每列都挂个符号）。 */
  const sortArrow = useCallback((key: SortKey) => {
    if (sortKey !== key) return null;
    return <span className="crowding-sort-arrow">
      {sortDir === "desc" ? "▼" : "▲"}
    </span>;
  }, [sortKey, sortDir]);

  /**
   * 联想候选（**本地、零请求**）：`suggestBoards` 是纯函数，见 `boardSearch.ts`。
   *
   * ★ 不变式：候选集**必须**是筛选结果的子集（先 `matchBoard` 再排序），
   * 否则会出现"下拉里点了它、列表里却没有它"。那条不变式在
   * `npm run check:boardsearch` 里有专门的断言（含多词场景）。
   *
   * 上限 8 条：下拉是"帮我少打几个字"，不是"再给我一张表"。
   */
  const suggestions = useMemo(
    () => suggestBoards(view, boardFilter, 8), [view, boardFilter]);

  /** 下拉当前该不该显示（有词、聚焦、有候选 —— 三个条件缺一不可）。 */
  const suggestOpen = filterFocused && boardFilter.trim() !== ""
    && suggestions.length > 0;

  /** 点/回车选中一个候选 → 用**它的板块名**填满筛选框（列表随即只剩它）。 */
  const applySuggestion = useCallback((row: CrowdingListItem) => {
    setBoardFilter(row.sector_name || row.sector_code);
    setActiveSuggest(-1);
    // 选中即收下拉：不然它盖在刚筛出来的那一行上
    setFilterFocused(false);
  }, []);

  /**
   * 把筛选记录写进本地缓存（用户要求"筛选记录保留在前端缓存"）。
   *
   * ⚠️ **只写筛选词**：两个"只看…"开关刻意不写（见 state 那里的 2026-10-15 事故）。
   * 这条在 `test_board_filter_structure.py` 里是**回归守卫**：谁把开关加回来就变红。
   */
  useEffect(() => {
    writeBoardFilter({ q: boardFilter });
  }, [boardFilter]);

  /**
   * 联想下拉的坐标（`position: fixed` + portal 到 body）。
   *
   * ## 为什么必须 portal（不是审美问题）
   *
   * 表头在 `.crowding-alert-scroll`（`overflow: auto`）里 —— 就地渲染的下拉
   * 会被**裁掉**，看起来就是"联想弹不出来"。同一个文件里告警阈值编辑面板
   * 也踩过这一脚（见下面 `alertEdit` 的注释），解法一样：挂到 body。
   * 代价是要自己算坐标，所以：
   *   - 打开时算一次（`useLayoutEffect`，画之前就位，不会闪一下）；
   *   - 滚动/改窗口大小时重算，且**用捕获阶段**监听 —— 横向滚动发生在
   *     内层滚动容器上，不冒泡到 window，普通监听收不到。
   */
  useLayoutEffect(() => {
    if (!suggestOpen) { setPopRect(null); return; }
    const place = () => {
      const box = filterInputRef.current?.getBoundingClientRect();
      if (!box) return;
      setPopRect({ left: box.left, top: box.bottom + 4,
                   width: Math.max(box.width, 260) });
    };
    place();
    window.addEventListener("scroll", place, true);
    window.addEventListener("resize", place);
    return () => {
      window.removeEventListener("scroll", place, true);
      window.removeEventListener("resize", place);
    };
  }, [suggestOpen, boardFilter, suggestions.length]);

  const alertCount = view.filter((row) => row.is_alert).length;
  const pinnedCount = view.filter((row) => row.pinned).length;
  /** 设了自定义告警的板块数 / 其中已触发的 */
  const alertSetCount = view.filter((row) => row.alert_threshold !== null).length;
  const triggeredCount = view.filter((row) => row.alert_on).length;

  const guard = useCallback(async (tag: string, action: () => Promise<void>,
                                okText?: string) => {
    setBusy(tag);
    try {
      await action();
      if (okText) onNotice?.(okText);
    } catch (exc) {
      onNotice?.(`操作失败：${exc instanceof Error ? exc.message : String(exc)}`);
    } finally {
      setBusy("");
    }
  }, [onNotice]);
  const addSector = useCallback(async (code: string, name: string) => {
    await guard(`add:${code}`, async () => {
      await onAdd(code, name);
    }, `已加入清单：${name || code}`);
    setKeyword("");
    setOptions([]);
  }, [guard, onAdd]);

  const copyAll = useCallback(async () => {
    if (rows.length === 0) return;
    const text = rows
      .map((row) => `${row.sector_name || row.sector_code}\t${pct(row.water_level)}`)
      .join("\n");
    // 筛选生效时**如实说明只复制了筛出来的那部分** —— 否则用户拿到的列表
    // 少了几百行却看不出来（"复制列表"看起来像"复制全部"）。
    const scope = boardFilter.trim()
      ? `（当前筛选「${boardFilter.trim()}」命中 ${rows.length}/${view.length}）` : "";
    try {
      await navigator.clipboard.writeText(text);
      onNotice?.(`已复制 ${rows.length} 个板块${scope}`);
    } catch {
      onNotice?.("复制失败：浏览器未授权剪贴板（可手动选中列表复制）");
    }
  }, [rows, view.length, boardFilter, onNotice]);

  const addAllAlerts = useCallback(async () => {
    const missing = view.filter((row) => row.is_alert && !row.pinned);
    if (missing.length === 0) { onNotice?.("没有需要置顶的告警板块"); return; }
    let ok = 0;
    const failed: string[] = [];
    for (const row of missing) {
      try {
        await onTogglePin(row.sector_code, true);
        ok += 1;
      } catch { failed.push(row.sector_name || row.sector_code); }
    }
    onNotice?.(failed.length === 0
      ? `已置顶 ${ok} 个告警板块`
      : `置顶成功 ${ok} 个，失败 ${failed.length} 个（${failed.slice(0, 3).join("、")}）`);
  }, [view, onTogglePin, onNotice]);

  const csvRows = rows as unknown as CrowdingAlertRow[];

  return (
    <section className="crowding-alert-panel">
      <div className="crowding-alert-head">
        <h3>⚠️ 高拥挤度告警（水位 ≥ {(threshold * 100).toFixed(0)}% 标红）</h3>
        <span className="muted-text">
          更新 {tradeDate || "—"}
          {updatedAt ? ` ${updatedAt.slice(11, 16)}` : ""}
          ，清单 <b>{view.length}</b> 个板块、其中 <b className="error-text">
            {alertCount}</b> 个触发告警
          {pinnedCount > 0 && (pinsActive
            ? <> · 置顶 <b>{pinnedCount}</b>
                <span className="warn-text">（已锁定在最前，按列排序会被打断）</span>
              </>
            : <> · 置顶 <b>{pinnedCount}</b>
                <span className="muted-text">（按数值参与排序中）</span>
              </>)}
          {alertSetCount > 0 && (
            <> · 自定义告警 <b>{alertSetCount}</b> 条
              {triggeredCount > 0
                ? <span className="error-text">（已触发 {triggeredCount}）</span>
                : <span className="muted-text">（无触发）</span>}
            </>
          )}
        </span>
        <span style={{ flex: 1 }} />
        <label className="chart-toggle">
          <input type="checkbox" checked={onlyAlerts}
                 onChange={(event) => setOnlyAlerts(event.target.checked)}
                 title="勾选后只显示水位 ≥ 阈值的板块" />
          只看告警
        </label>
        <label className="chart-toggle">
          <input type="checkbox" checked={onlyTriggered}
                 onChange={(event) => setOnlyTriggered(event.target.checked)}
                 title="只显示你设的自定义告警已触发的板块" />
          只看已触发（{triggeredCount}）
        </label>
        {/* ★ 「板块」列筛选（2026-10-14 用户要求）：262 个板块靠翻页找太慢。
            输入框**就在「板块」列表头里**（见下面表头那一格）—— 放工具栏里
            用户找不到（用户原话：「板块那一列 我看还没支持搜索过滤」）。
            这里只留"当前筛了什么、筛出几条"的状态条，避免筛选生效了却看不出来。 */}
        {boardFilter.trim() && (
          <>
            <span className="muted-text">
              筛出 <b>{rows.length}</b> / {view.length}
            </span>
            <button className="btn-ghost tiny" onClick={() => setBoardFilter("")}
                    title="清空筛选（Esc 也可以）">清空筛选</button>
          </>
        )}
        <label className="muted-text">
          排序
          <select className="mono" value={sortKey}
                  onChange={(event) => {
                    const next = event.target.value as SortKey;
                    // 同样解除置顶锁：换排序键 = 想看新的那一列的顺序
                    setPinsActive(false);
                    setSortKey(next);
                    setSortDir("desc");
                  }}>
            <option value="water">按水位↓（即排名↑）</option>
            <option value="rank">按拥挤度排名</option>
            <option value="chg_5d">按近5日变化↓</option>
            <option value="chg_1m">按近1月变化↓</option>
            <option value="chg_2m">按近2月变化↓</option>
            <option value="flow_ratio">按近1月资金占比↓</option>
            <option value="amount">按成交额↓</option>
            <option value="name">按名称</option>
          </select>
        </label>
        <button className="btn-ghost tiny"
                title="升序/降序切换（表头点击同样可切）"
                onClick={() => {
                  setPinsActive(false);
                  setSortDir((dir) => (dir === "desc" ? "asc" : "desc"));
                }}>
          {sortDir === "desc" ? "降序 ↓" : "升序 ↑"}
        </button>
        {/* 置顶开关：默认是"不锁位置"（排序永远按数值），点它才把置顶钉回最前。
            只改显示顺序，不动库里的 pinned（见 pinsActive 的说明）。 */}
        {pinnedCount === 0 ? null : pinsActive ? (
          <button className="btn-ghost tiny primary"
                  title={`当前置顶行锁在最前（${pinnedCount} 个），会打断按列排序的顺序。`
                    + "\n点此解除锁定，让它们按数值一起排序（配置不变）。"}
                  onClick={() => {
                    setPinsActive(false);
                    onNotice?.(`已解除置顶锁定：${pinnedCount} 个置顶板块`
                      + "现在按所选列一起排序（置顶配置未改动，可随时恢复）");
                  }}>
            📌 解除置顶锁定（{pinnedCount}）
          </button>
        ) : (
          <button className="btn-ghost tiny"
                  title={`置顶板块现在**按数值参与排序**，不锁位置。`
                    + `\n点此恢复：让 ${pinnedCount} 个置顶板块重新固定排在最前。`}
                  onClick={() => {
                    setPinsActive(true);
                    onNotice?.(`已恢复置顶，${pinnedCount} 个置顶板块重新排在最前`);
                  }}>
            📌 置顶恢复（{pinnedCount}）
          </button>
        )}
        {/* 联想数据源已改成**我们自己的下拉**（`filterPop`），不再用原生
            `<datalist>` —— 原生 datalist 只按 `<option value>`（板块名）匹配，
            敲拼音 `huagong` 时一个候选都不弹，而"拼音池"正是用户要的
            （用户原话：「都映射出中文拼音全称字段，然后不就有了搜索联想拼音池了吗」）。 */}
      </div>

      {/* 搜索新增 */}
      <div className="crowding-alert-head">
        <div className="crowding-search">
          <input className="qsel-input" value={keyword}
                 placeholder="输入板块名称/代码搜索并加入清单"
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
                    <button disabled={busy === `add:${code}`}
                            onClick={() => void addSector(code, name)}>
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
        <span className="muted-text">共 {view.length} 个板块在清单</span>
        <span style={{ flex: 1 }} />
        <button className="btn-ghost" disabled={rows.length === 0}
                onClick={() => void copyAll()}>复制列表</button>
        <button className="btn-ghost" disabled={view.length === 0}
                onClick={() => void addAllAlerts()}>置顶全部告警</button>
        <button className="btn-ghost" disabled={view.length === 0}
                onClick={() => downloadCsv(csvRows, (row) =>
                  row.sector_name || row.sector_code)}
                title={"导出为 CSV（Excel 可直接打开，带 BOM 不乱码）"
                  + (boardFilter.trim()
                    ? `\n⚠️ 当前筛选「${boardFilter.trim()}」生效中，只导出命中的 `
                      + `${rows.length} 行（不是全部 ${view.length} 行）` : "")}>导出 CSV</button>
        <button className="btn-ghost" disabled={view.length === 0}
                title="清空清单配置，回到默认显示全部概念板块"
                onClick={() => {
                  if (!window.confirm("清空清单配置？删除与置顶都会一起清掉。")) return;
                  void guard("reset", async () => {
                    const cleared = await onReset();
                    onNotice?.(`已清空 ${cleared} 条配置，回到默认全量`);
                  });
                }}>重置清单</button>
      </div>

      {loading && <div className="info-box">正在读取清单…</div>}

      {!loading && rows.length === 0 && (
        <div className="crowding-alert-empty">
          {/* ⚠️ 三种"空"必须分开说：清单本来就空 / 筛没了 / 真没告警。
              合成一句「当前没有触发告警的板块」时，用户把筛选词打错会以为
              是"今天没告警"—— 静默的假信号比空列表更糟。 */}
          {view.length === 0
            ? "清单是空的。用上方搜索框添加板块，或点「重置清单」回到默认全量。"
            : boardFilter.trim()
              ? <>筛选词「{boardFilter.trim()}」在清单 {view.length} 个板块里没有命中。</>
              : "当前没有触发告警的板块"}
          {boardFilter.trim() && view.length > 0
            ? <span className="muted-text">
                （只筛「板块」列的名称/代码/拼音首字母，不影响数据本身；Esc 或「清空筛选」取消）
              </span>
            : <span className="muted-text">
                （水位 = 该板块当前平滑拥挤度 / 近 6 年最高值；未刷新的板块"数据不足"）
              </span>}
        </div>
      )}

      {/* 表头 + 数据行共用一个滚动容器：列宽由 --crowding-grid 统一下发，
          所以表头与每一行严格对齐；窄屏时整张表横向滚动而不是压扁。 */}
      <div className="crowding-alert-scroll"
           style={{ "--crowding-grid": GRID } as React.CSSProperties}>
      {/* ⚠️ 条件用 `view.length > 0`（清单非空）**而不是** `rows.length > 0`（筛完非空）：
          用后者时，只要筛选把行筛光（或"只看…"开关把行藏光），**表头连同筛选框
          一起消失** —— 用户面前只剩一句"没有命中"，却找不到改筛选词的地方。
          这正是 2026-10-15 那次"数据不展示了"的一部分。表头是列标题 + 筛选入口，
          清单里有东西就该在。 */}
      {view.length > 0 && (
        <div className="crowding-alert-header" role="row">
          <button className="crowding-hsort num" title="拥挤度排名（水位降序，第 1 名最拥挤）"
                  onClick={() => toggleSort("rank")}>
            排名{sortArrow("rank")}
          </button>
          <span className="crowding-hcell num">置顶</span>
          <span className="crowding-hcell num">操作</span>
          <span className="crowding-hcell center">代码</span>
          {/*
            ★ 「板块」列的表头 = 排序按钮 + **筛选输入框**（2026-10-14 用户要求）。
            为什么不放在工具栏里：用户第一眼就是往这一列找（原话：「板块那一列
            我看还没支持搜索过滤」）。放列头上还有两个好处：
              · 表头是 `position: sticky` 的，**纵向滚动时筛选框一直在**，不用滚回顶部；
              · 输入框与它筛的那一列在同一个视觉位置，不存在"这个框筛的是哪一列"的歧义。
            ⚠️ 不能把 input 塞进原来那个 `<button>` 里（按钮套输入框是非法 HTML，
            点击与键盘行为都会打架），所以表头这一格改成 `<span>` + 里面两个控件。
          */}
          <span className="crowding-hcell center crowding-hfilter">
            <button className="crowding-hsort" title="按板块名称排序"
                    onClick={() => toggleSort("name")}>
              板块{sortArrow("name")}
            </button>
            <input ref={filterInputRef} className="qsel-input crowding-col-filter"
                   value={boardFilter} spellCheck={false}
                   placeholder="筛这列…"
                   title={"按板块筛选本列（**本地即时**，不发请求、不改数据）：\n"
                     + "· 名称：化工 / 体材料（中间片段也行）\n"
                     + "· 代码：885959 或 885959.ti\n"
                     + "· 拼音首字母：hg → 化工\n"
                     + "· 拼音全拼：huagong → 化工原料（后端算好下发）\n"
                     + "· 空格分隔多个词 = 全部命中（如「半导 材料」「bandao chanpin」）\n"
                     + "· ↓↑ 选候选 / Enter 选中 / Esc 或「清空筛选」取消\n"
                     + "筛选记录会记住（下次打开面板还在）"}
                   onChange={(event) => {
                     setBoardFilter(event.target.value);
                     setActiveSuggest(-1);
                   }}
                   onFocus={() => setFilterFocused(true)}
                   onBlur={() => setFilterFocused(false)}
                   onKeyDown={(event) => {
                     if (event.key === "Escape") {
                       // Esc：先收候选、再清筛选（分两步，避免"只想关下拉却把词清了"）
                       if (suggestOpen) { setFilterFocused(false); return; }
                       setBoardFilter("");
                       return;
                     }
                     if (!suggestOpen) return;
                     if (event.key === "ArrowDown") {
                       event.preventDefault();
                       setActiveSuggest((i) => (i + 1) % suggestions.length);
                     } else if (event.key === "ArrowUp") {
                       event.preventDefault();
                       setActiveSuggest((i) =>
                         (i - 1 + suggestions.length) % suggestions.length);
                     } else if (event.key === "Enter") {
                       event.preventDefault();
                       const picked = suggestions[activeSuggest >= 0 ? activeSuggest : 0];
                       if (picked) applySuggestion(picked);
                     }
                   }} />
          </span>
          <button className="crowding-hsort num"
                  title={"自定义告警阈值：点每行的格子设置水位线\n"
                    + "「高于设定值告警」看拥挤风险；「低于设定值告警」看退潮机会\n"
                    + "取值 [0, 1]，支持 3 位小数"}
                  onClick={() => toggleSort("alert")}>
            告警{sortArrow("alert")}
          </button>
          <button className="crowding-hsort num" title="拥挤度水位 = 平滑拥挤度 / 近 6 年最高值"
                  onClick={() => toggleSort("water")}>
            水位{sortArrow("water")}
          </button>
          <span className="crowding-hcell num">平滑</span>
          <span className="crowding-hcell num">近6年最高</span>
          <button className="crowding-hsort num" title="板块当日成交额"
                  onClick={() => toggleSort("amount")}>
            成交额{sortArrow("amount")}
          </button>
          <button className="crowding-hsort num"
                  title={`近5个交易日拥挤度水位变化（%）`
                    + (metrics?.as_of_date
                      ? `\n截至 ${metrics.as_of_date}` : "") + `\n点击排序`}
                  onClick={() => toggleSort("chg_5d")}>
            近5日{sortArrow("chg_5d")}
          </button>
          <button className="crowding-hsort num"
                  title={`近1个月（${monthDays}个交易日）拥挤度水位变化（%）`
                    + (metrics?.as_of_date
                      ? `\n截至 ${metrics.as_of_date}` : "") + `\n点击排序`}
                  onClick={() => toggleSort("chg_1m")}>
            近1月{sortArrow("chg_1m")}
          </button>
          <button className="crowding-hsort num"
                  title={`近2个月（${monthDays * 2}个交易日）拥挤度水位变化（%）`
                    + (metrics?.as_of_date
                      ? `\n截至 ${metrics.as_of_date}` : "") + `\n点击排序`}
                  onClick={() => toggleSort("chg_2m")}>
            近2月{sortArrow("chg_2m")}
          </button>
          <button className="crowding-hsort num"
                  title={`近1月成分股主力净流入 / 板块基准日流通市值（%）`
                    + (metrics?.flow_last_date
                      ? `\n资金流截至 ${metrics.flow_last_date}`
                        + (metrics.flow_base_date
                          ? `（基准 ${metrics.flow_base_date}）` : "")
                      : "") + `\n点击排序`}
                  onClick={() => toggleSort("flow_ratio")}>
            资金占比{sortArrow("flow_ratio")}
          </button>
          <span className="crowding-hcell num">操作</span>
        </div>
      )}

      {/* 告警阈值编辑面板：portal 挂到 body，脱离 `.crowding-alert-scroll`
          （overflow:auto + max-height:620px）的截断。原方案是塞进滚动容器里
          `position:relative`，点下面的行时弹窗会被截在视口外，感官上"弹不出来"。
          挂到 body + 编辑面板改 `position:fixed` 后是真弹层，遮罩和外点关闭都按预期生效。 */
      }
      {alertEdit !== null && (() => {
        const row = view.find((item) => item.sector_code === alertEdit);
        if (!row) return null;
        return createPortal(
          <>
            <div className="crowding-alert-editor-mask" />
            <SectorCrowdingAlertEditor
              sectorCode={row.sector_code}
              sectorName={row.sector_name || row.sector_code}
              currentWater={row.water_level}
              mode={row.alert_mode === "below" ? "below" : "above"}
              threshold={row.alert_threshold}
              busy={busy === `alert:${row.sector_code}`}
              onClose={() => setAlertEdit(null)}
              onSave={(mode, threshold) => {
                void (async () => {
                  await guard(`alert:${row.sector_code}`, async () => {
                    await onAlertSet(row.sector_code, mode, threshold);
                  }, `已设置告警：${row.sector_name || row.sector_code} `
                    + `${mode === "above" ? "高于" : "低于"} `
                    + `${threshold.toFixed(3)} 时提醒`);
                  setAlertEdit(null);
                })();
              }}
              onClear={() => {
                void (async () => {
                  await guard(`alert:${row.sector_code}`, async () => {
                    await onAlertClear(row.sector_code);
                  }, `已清除告警：${row.sector_name || row.sector_code}`);
                  setAlertEdit(null);
                })();
              }} />
          </>,
          document.body);
      })()}

      {rows.length > 0 && (
        <ul className="crowding-alert-list">{rows.map((row) => {
            const tone = row.tone;
            const name = row.sector_name || row.sector_code;
            const rank = ranks.get(row.sector_code) ?? null;
            return (
              <li key={row.sector_code}
                  className={`crowding-alert-item tone-${tone}`
                    + (row.pinned ? " pinned" : "")}>
                <span className="mono muted-text crowding-rank">
                  {rank === null ? "—" : rank}
                </span>
                <button className={`btn-ghost tiny crowding-pin`
                          + (row.pinned ? " active" : "")}
                        disabled={busy === `pin:${row.sector_code}`}
                        title={row.pinned ? "取消置顶" : "置顶（永远排最前）"}
                        onClick={() => void guard(`pin:${row.sector_code}`,
                          () => onTogglePin(row.sector_code, !row.pinned))}>
                  {row.pinned ? "📌" : "☆"}
                </button>
                <button className="btn-ghost tiny crowding-del"
                        disabled={busy === `del:${row.sector_code}`}
                        title="从清单删除（历史拥挤度数据仍保留在库里）"
                        onClick={() => void guard(`del:${row.sector_code}`,
                          () => onRemove(row.sector_code),
                          `已从清单删除：${name}`)}>
                  删除
                </button>
                <span className="crowding-code mono" title={row.sector_code}>
                  {row.sector_code}
                </span>
                <button className="crowding-alert-name"
                        onClick={() => onPick(row.sector_code, name)}
                        title="点击查看该板块近 6 年拥挤度与水位曲线">
                  {name}
                </button>
                {/* 数值列一律 .num（右对齐 + 等宽数字）：格子里有的元素默认左对齐、
                    有的被 .crowding-water 设成右对齐，混着来就会和表头对不上 */}
                {/* 告警列：点格子设置阈值（方向 + 数值在弹出面板里选） */}
                <button
                  className={"crowding-alert-cell tiny"
                    + (row.alert_threshold === null ? " unset" : "")
                    + (row.alert_on ? " fired" : "")}
                  aria-expanded={alertEdit === row.sector_code}
                  title={row.alert_threshold === null
                    ? "点此设置告警水位（高于/低于设定值就告警）"
                    : `${row.alert_mode === "below" ? "低于" : "高于"} `
                      + `${row.alert_threshold.toFixed(3)} 就告警`
                      + (row.alert_on ? "\n当前已触发" : "\n当前未触发")
                      + "\n点击修改"}
                  onClick={() => setAlertEdit(
                    alertEdit === row.sector_code ? null : row.sector_code)}>
                  {row.alert_threshold === null ? "＋设置" : (
                    <>
                      <span className="crowding-alert-dir">
                        {row.alert_mode === "below" ? "↓" : "↑"}
                      </span>
                      <span className="mono">
                        {row.alert_threshold.toFixed(3)}
                      </span>
                      {row.alert_on && <span className="crowding-alert-dot">●</span>}
                    </>
                  )}
                </button>
                <span
                  className={`crowding-water num tone-${tone}`}
                  title={
                    row.water_level === null || row.water_level === undefined
                      ? `板块日线 ${row.bars ?? 0} 根，不足 `
                        + `${minBarsForWaterLevel ?? "?"} 根（≈3 年）`
                        + " → 水位不分发（分母是近 6 年最高值，历史太短会恒为 100%）"
                      : ""
                  }
                >
                  {waterText(row.water_level, row.bars, minBarsForWaterLevel)}
                </span>
                <span className="crowding-num muted-text mono">
                  {raw3(row.ma5_crowding)}
                </span>
                <span className="crowding-num muted-text mono">
                  {raw3(row.max_ma5_crowding)}
                </span>
                <span className="crowding-num muted-text mono">
                  {yi(row.sector_amount)}
                </span>
                {/* 4 个指标列：列名在表头，格子里只放数字 */}
                <b className={`crowding-metric-value num ${deltaTone(row.chg_5d)}`}
                   title={row.base_date_5d
                     ? `近5日：基准 ${row.base_date_5d} → ${row.trade_date}` : undefined}>
                  {signed(row.chg_5d)}
                </b>
                <b className={`crowding-metric-value num ${deltaTone(row.chg_1m)}`}
                   title={row.base_date_1m
                     ? `近1月：基准 ${row.base_date_1m} → ${row.trade_date}` : undefined}>
                  {signed(row.chg_1m)}
                </b>
                <b className={`crowding-metric-value num ${deltaTone(row.chg_2m)}`}
                   title={row.base_date_2m
                     ? `近2月：基准 ${row.base_date_2m} → ${row.trade_date}` : undefined}>
                  {signed(row.chg_2m)}
                </b>
                <b className={`crowding-metric-value num ${deltaTone(row.flow_ratio)}`}
                   title={row.flow_base_date
                     ? `资金占比：净流入 ${row.net_inflow === null ? "—"
                         : `${(row.net_inflow / 1e8).toFixed(1)}亿`}`
                       + ` / 基准流通市值 ${row.circ_mv_base === null ? "—"
                         : `${(row.circ_mv_base / 1e8).toFixed(0)}亿`}`
                       + `（${row.flow_base_date} → ${row.flow_last_date}）`
                     : "本周还没算过，点顶部「重算异动指标」"}>
                  {signed(row.flow_ratio)}
                </b>
                <button className="btn-ghost tiny"
                        onClick={() => onPick(row.sector_code, name)}>
                  查看详情
                </button>
              </li>
            );
          })}
        </ul>
      )}
      </div>

      {rows.length > 0 && (
        <div className="crowding-metric-foot muted-text">
          <b>周频异动指标</b>（辅助判断大资金建仓异动）：
          「近5日/近1月/近2月」= 各窗口起点水位 → 最新水位的**变化百分比**（点表头可排序）；
          「资金占比」= 近 {monthDays} 个交易日成分股主力净流入 ÷ 基准日板块流通市值。
          {metrics?.week ? (
            <>
              {" "}本周 <b>{metrics.week}</b>
              {metrics.computed_at
                ? `（${metrics.computed_at.slice(0, 16).replace("T", " ")} 算）` : ""}；
              拥挤度截至 <b>{metrics.as_of_date || rows[0]?.trade_date || "—"}</b>，
              资金流截至 <b>{metrics.flow_last_date || "—"}</b>
              {metrics.flow_base_date ? `（基准 ${metrics.flow_base_date}）` : ""}
              {" "}· 每周三 08:30 自动重算一次，也可以点上方「重算异动指标」立即更新。
            </>
          ) : (
            <> 本周还没算过 —— 点上方「重算异动指标」开始计算（只算当前视图里的
              概念板块约 262 个，热缓存几秒；冷缓存要抓成分股，首次可能 3~5 分钟）。</>
          )}
        </div>
      )}
      {/*
        联想下拉：portal 到 body + `position: fixed`（坐标由上面那个
        `useLayoutEffect` 算）。**必须**这样挂 —— 就地渲染会被
        `.crowding-alert-scroll` 的 `overflow: auto` 裁掉。
        `onMouseDown` 里 `preventDefault()`：不这么做，点候选会先让输入框失焦
        （`onBlur` 关掉下拉），click 还没派发下拉就没了 —— 点了没反应。
      */}
      {suggestOpen && popRect && createPortal(
        <ul className="crowding-suggest crowding-filter-pop"
            style={{ left: popRect.left, top: popRect.top, width: popRect.width }}
            onMouseDown={(event) => event.preventDefault()}>
          {suggestions.map((row, index) => {
            const hint = pinyinHint(row);
            return (
              <li key={row.sector_code}>
                <button className={index === activeSuggest ? "active" : ""}
                        onClick={() => applySuggestion(row)}>
                  <span>{row.sector_name || row.sector_code}</span>
                  <span className="mono muted-text"> {row.sector_code}</span>
                  {hint && <span className="muted-text crowding-py">{hint}</span>}
                </button>
              </li>
            );
          })}
        </ul>,
        document.body)}
    </section>
  );
}
