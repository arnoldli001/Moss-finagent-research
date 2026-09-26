import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  formatDate,
  formatPct,
  formatScore,
  mainlineApi,
  toneOf,
  type AlertReturnBoard,
  type AlertReturnPayload,
  type AlertReturnRow,
  type AlertReturnWindow,
} from "../mainlineApi";

/**
 * 主线挖掘 · 回测收益展示面板（挂在「回测报告」页签里）。
 *
 * ## 这个面板要回答的三个问题
 *
 * ① 信号发出后**7 / 20 / 60 个交易日**最大涨幅是多少（理论可捕获空间）
 * ② 同一概念反复触发算几次 —— `dedup_days` 个交易日内折叠成 `X2/X3`
 * ③ 具体是**哪几只成分股**领涨（股名 + 区间涨幅）
 *
 * ## 三个不能含糊的显示规则
 *
 * **① 窗口没走满 → 不填最终值，不是填 0。** 数据到 2026-09-23 为止，
 * 2026-08-25 之后的信号连 60 个交易日都没走完。这时后端给
 * `max_gain_pct = null`，前端必须显示"——"并**同时**给出"至今 +x%（剩 N 日）"。
 * 把 null 渲染成 "0.00%" 会让"还没走完"读成"一分没涨"，是最容易被误信的错。
 *
 * **② 「最大涨幅」与「实际收益」分开两列，不能只留一个。** 窗口内最高价高于
 * 信号日收盘几乎是必然事件 —— 实测按"最大涨幅 > 0"算胜率是 **97%**，
 * 那个数没有区分度。判断一个板块"亏不亏钱"必须看**实际收益**（窗口末收盘）
 * 与**最多浮亏**（窗口内最低价）。所以两组数都给。
 *
 * **③ 领涨股分两次请求。** 板块级 0.5 秒、领涨股要 6~10 秒（走 15 GiB 行情仓），
 * 所以先把表格画出来，再补一次带 `leaders` 的请求（结果后端缓存 10 分钟）。
 * 这一列在补齐前显示"计算中…"，不是"没有数据"。
 *
 * **④ 板块筛选口径是「20 日胜率 > 40%」，不是「平均实际收益 ≥ 0」。**
 * 胜率 = 该板块**已走满**的 20 日窗口里「窗口末收盘 > 信号日收盘」的比例。
 * 两者不是同一件事：样本 `+10/+1/+1/-6/-7` 胜率 60%、均值为负；`+30/-1/-2`
 * 均值为正、胜率只有 33% —— 前者显示、后者隐藏。
 * **比较是严格大于**：恰好等于门槛的板块不显示。
 * 门槛由后端给（`stats.min_win_rate`，2026-09-25 由 50% 放宽到 40%），
 * 前端只渲染，不自己定义。
 * 一条 20 日窗口都没走满的板块（`gate === "pending"`）**不隐藏**：
 * 判不出胜率不等于胜率不达标，把它当不通过会让"最近一个月"整片消失。
 */

/** 渲染上限：1305 行全画出来会让浏览器卡住，先画最近的、可手动加载更多。 */
const PAGE = 300;

/**
 * 筛选门槛的**兜底值**。载荷里一定有 `stats.min_win_rate`（后端给），
 * 这里只是"万一字段缺失"时的防手滑值 —— 改门槛要改后端 `DEFAULT_MIN_WIN_RATE`，
 * 不要把这里当成真正的配置点。
 */
const FALLBACK_MIN_WIN_RATE = 0.4;

/** 稳定取一个数值型统计（`stats` 的值可能是 string/null，不能直接当 number）。 */
function statNum(payload: AlertReturnPayload | null, key: string): number | null {
  const value = payload?.stats?.[key];
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "" && Number.isFinite(Number(value))) {
    return Number(value);
  }
  return null;
}

/** 百分比统计：后端给的是**百分数**（2.91 → "+2.91%"）。 */
function statPct(payload: AlertReturnPayload | null, key: string,
                 digits = 2): string {
  return formatPct(statNum(payload, key), digits);
}

/** 比例统计：后端给的是**小数**（0.5868 → "58.7%"）。 */
function statRatio(payload: AlertReturnPayload | null, key: string,
                   digits = 1): string {
  const value = statNum(payload, key);
  if (value === null) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

type Props = {
  /** 点板块名 → 打开下钻（由 MainlinePanel 提供） */
  onPickBoard?: (code: string, name: string) => void;
};

/**
 * 概念板块收益排行的排序状态。
 *
 * `default` = **不动载荷原顺序**（后端按 20 日实际收益降序排的）。
 * 刻意不把"按实际收益排序"实现成前端的一次排序：那样排序口径就有两份，
 * 后端哪天改了排序规则，前端会继续按旧规则排，而界面上看不出来。
 */
type BoardSort = "default" | "win_desc" | "win_asc";

/** 当前排序的中文说明（卡片头上要写清楚"这张表现在按什么排"）。 */
const SORT_LABEL: Record<BoardSort, string> = {
  default: "按 20 日实际收益降序（后端默认）",
  win_desc: "按 20 日胜率降序",
  win_asc: "按 20 日胜率升序",
};

/** 列头上的排序箭头：未排序时给一个"可点"的提示符。 */
const SORT_CARET: Record<BoardSort, string> = {
  default: "⇅", win_desc: "▼", win_asc: "▲",
};

/**
 * 「告警日期超过 1 个月」的分界日（紧凑 `YYYYMMDD`）。
 *
 * 用**数据末端 `as_of`** 反推，而不是写死一个日期：写死的话，数据往前推进之后
 * "1 个月"会悄悄变成"一个半月"，而界面上完全看不出来。
 * （今天它与后端 `split` 那个历史/近期分界是同一天，但 `split` 是后端常量、
 * 不跟着数据走，所以这里必须自己算。）
 *
 * 拿不到 `as_of` 时返回空串 —— 调用方据此**不折叠**（宁可多显示明细）。
 */
function monthsBefore(compact: string, months: number): string {
  if (!/^\d{8}$/.test(compact)) return "";
  const date = new Date(Date.UTC(Number(compact.slice(0, 4)),
                                 Number(compact.slice(4, 6)) - 1 - months,
                                 Number(compact.slice(6, 8))));
  if (Number.isNaN(date.getTime())) return "";
  return `${date.getUTCFullYear()}`
    + `${String(date.getUTCMonth() + 1).padStart(2, "0")}`
    + `${String(date.getUTCDate()).padStart(2, "0")}`;
}

/**
 * 逐条追踪表的一行：**要么是一个概念（折叠），要么是一个信号周期**。
 *
 * 表头是同一套列，两种行各自往列里填数：折叠行填的是该概念的**合计**
 * （各项各周期收益的平均值 + 总胜率），明细行填的是那一个周期的值。
 */
type TrackEntry =
  | { kind: "group"; boardCode: string; board: AlertReturnBoard | undefined;
      rows: AlertReturnRow[]; date: string }
  | { kind: "row"; row: AlertReturnRow; date: string; nested?: boolean };

/** 折叠行在各周期列上取后端的哪个均值字段（口径由 `summarize_boards` 定）。 */
const GAIN_FIELD: Record<number, "avg_gain_7d" | "avg_gain_20d" | "avg_gain_60d"> = {
  7: "avg_gain_7d", 20: "avg_gain_20d", 60: "avg_gain_60d",
};

/** 该概念在该周期的**平均最大涨幅**；周期不是 7/20/60 时给 `null`（不编数）。 */
function gainAverage(board: AlertReturnBoard | undefined,
                     days: number): number | null {
  if (!board) return null;
  const field = GAIN_FIELD[days];
  return field ? board[field] ?? null : null;
}

/**
 * 把「告警日期超过 `cutoff`」的行按概念折叠，近一个月的行保持逐条。
 *
 * 排序与原来的逐条表一致：**整体按日期倒序**（折叠组用组内**最新**的信号日
 * 参与排序），所以"最近又起了一波"的概念永远在最上面；组内明细同样是倒序，
 * 展开后第一行就是该概念最近一次触发。
 *
 * `cutoff` 为空（拿不到 `as_of`）时**不折叠**：折叠是显示层的便利，
 * 拿不到判定依据时不该把明细藏起来。
 */
function buildEntries(rows: AlertReturnRow[], cutoff: string,
                      gateOf: Map<string, AlertReturnBoard>): TrackEntry[] {
  const recent: TrackEntry[] = [];
  const buckets = new Map<string, AlertReturnRow[]>();
  for (const row of rows) {
    if (!cutoff || row.signal_date >= cutoff) {
      recent.push({ kind: "row", row, date: row.signal_date });
      continue;
    }
    const bucket = buckets.get(row.board_code);
    if (bucket) bucket.push(row);
    else buckets.set(row.board_code, [row]);
  }
  const groups: TrackEntry[] = [];
  for (const [boardCode, bucket] of buckets) {
    const sorted = [...bucket].sort(
      (left, right) => right.signal_date.localeCompare(left.signal_date));
    groups.push({ kind: "group", boardCode, board: gateOf.get(boardCode),
                  rows: sorted, date: sorted[0].signal_date });
  }
  // 日期相同的组之间要有个稳定的次序，否则每次渲染行序会跳
  const codeOf = (item: TrackEntry) =>
    item.kind === "group" ? item.boardCode : item.row.board_code;
  return [...groups, ...recent].sort(
    (left, right) => right.date.localeCompare(left.date)
      || codeOf(left).localeCompare(codeOf(right)));
}

export default function MainlineAlertReturns({ onPickBoard }: Props) {
  /** 第一段（板块级，快） */
  const [panel, setPanel] = useState<AlertReturnPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  /** 第二段（带领涨股，慢）。与 `panel` 分开存：补齐失败不该把表格弄没了。 */
  const [withLeaders, setWithLeaders] = useState<AlertReturnPayload | null>(null);
  const [leadersLoading, setLeadersLoading] = useState(false);
  const [leadersError, setLeadersError] = useState("");
  const [view, setView] = useState<"recent" | "history" | "all" | "boards">("all");
  const [shown, setShown] = useState(PAGE);
  /** 概念板块收益排行的排序状态。默认沿用后端口径：按 20 日实际收益降序。 */
  const [sort, setSort] = useState<BoardSort>("default");
  /** 已展开的概念板块（按 `board_code`）。**默认全折叠** —— 这是用户口径。 */
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const leadersAsked = useRef(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const payload = await mainlineApi.alertReturns({ leaders: false });
      setPanel(payload);
      setShown(PAGE);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
      setPanel(null);
    } finally {
      setLoading(false);
    }
  }, []);

  const loadLeaders = useCallback(async () => {
    setLeadersLoading(true);
    setLeadersError("");
    try {
      setWithLeaders(await mainlineApi.alertReturns({ leaders: true }));
    } catch (exc) {
      // 领涨股失败只影响那一列：表格本身照常可用
      setLeadersError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setLeadersLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);
  // 第一段拿到结果后再补第二段：先让用户看到表格，再等增强列。
  useEffect(() => {
    if (panel && !leadersAsked.current) {
      leadersAsked.current = true;
      void loadLeaders();
    }
  }, [panel, loadLeaders]);

  const data = withLeaders ?? panel;
  const leadersReady = withLeaders !== null;

  /**
   * 板块与行筛选全在**本地**做，且筛选**固定开启**（前端没有开关）。
   *
   * 后端只返回一份 `rows`（`history` / `recent` 的重复副本已经去掉），
   * 所以这个筛选不该再触发一次请求 —— 它有全部 `boards[].passed`
   * （口径：20 日胜率 > 门槛，`pending` 也算通过），本地建个集合就够，
   * 切换是即时的。
   */
  const passedBoards = useMemo(() => new Set(
    (data?.boards ?? []).filter((item) => item.passed)
      .map((item) => item.board_code)), [data]);

  const scoped = useMemo(() => (data?.rows ?? []).filter(
    (row) => passedBoards.has(row.board_code)), [data, passedBoards]);

  const rows = useMemo(() => view === "recent"
    ? scoped.filter((row) => row.segment === "recent")
    : view === "history" ? scoped.filter((row) => row.segment === "history")
      : scoped, [scoped, view]);

  const counts = useMemo(() => ({
    all: scoped.length,
    recent: scoped.filter((row) => row.segment === "recent").length,
    history: scoped.filter((row) => row.segment === "history").length,
  }), [scoped]);

  /**
   * 板块排行也固定只列通过的（口径同 `passedBoards`），再按用户点的列排序。
   *
   * 排序在**本地**做：这 79 个板块已经在载荷里，点一下列头不该再请求一次。
   *
   * `default` 直接用**载荷原顺序**（后端按 20 日实际收益降序排的）——
   * 前端自己再排一遍等于把排序口径复制成两份，两边迟早不一致。
   */
  const boards = useMemo(() => {
    const list = (data?.boards ?? []).filter((item) => item.passed);
    if (sort === "default") return list;
    // 胜率相同时沿用默认次序，保证排序结果稳定、且"同一档里实际收益高的仍在前"
    const rank = new Map(list.map((item, index) => [item.board_code, index]));
    const sign = sort === "win_desc" ? -1 : 1;
    return [...list].sort((left, right) => {
      const a = left.win_rate_20d;
      const b = right.win_rate_20d;
      // 胜率判不出来的（一条 20 日窗口都没走满）**两个方向都排最后**：
      // 它是"未知"，不是"最低"
      if (a === null && b === null) return 0;
      if (a === null) return 1;
      if (b === null) return -1;
      if (a !== b) return (a - b) * sign;
      return (rank.get(left.board_code) ?? 0) - (rank.get(right.board_code) ?? 0);
    });
  }, [data, sort]);

  /** 点列头三态循环：胜率降序 → 胜率升序 → 回到默认（按 20 日实际收益）。 */
  const cycleSort = useCallback(() => {
    setSort((current) => current === "default" ? "win_desc"
      : current === "win_desc" ? "win_asc" : "default");
  }, []);

  /**
   * 板块 → 判定明细。逐条追踪表要用它显示**这一行为什么在这张表里** ——
   * 只看得到"通过筛选的行"、看不到胜率本身的话，用户没法核对筛选对不对，
   * 也没法判断某个板块是不是被误伤。
   */
  const gateOf = useMemo(() => {
    const out = new Map<string, AlertReturnBoard>();
    for (const item of data?.boards ?? []) out.set(item.board_code, item);
    return out;
  }, [data]);

  /** 「超过 1 个月」的分界：由数据末端反推（见 `monthsBefore`）。 */
  const cutoff = useMemo(() => monthsBefore(data?.as_of ?? "", 1), [data]);

  const entries = useMemo(
    () => buildEntries(rows, cutoff, gateOf), [rows, cutoff, gateOf]);

  /** 展开后拍平：折叠行后面紧跟它自己的明细（组内已倒序）。 */
  const flat = useMemo(() => {
    const out: TrackEntry[] = [];
    for (const item of entries) {
      out.push(item);
      if (item.kind === "group" && expanded.has(item.boardCode)) {
        for (const row of item.rows) {
          out.push({ kind: "row", row, date: row.signal_date, nested: true });
        }
      }
    }
    return out;
  }, [entries, expanded]);

  const groups = entries.filter((item) => item.kind === "group").length;
  const kept = entries.length - groups;            // 仍逐条显示的行数
  const visible = flat.slice(0, shown);

  /** 展开 / 收起一个概念。 */
  const toggle = useCallback((boardCode: string) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(boardCode)) next.delete(boardCode);
      else next.add(boardCode);
      return next;
    });
  }, []);
  if (error) {
    return (
      <section className="mainline-card">
        <div className="error-box">
          读取信号收益面板失败：{error}
          <div className="muted-text">
            （这个面板从本地告警表 + 板块指数日线现算；告警表为空时先跑一次当日评分）
          </div>
          <button className="btn-ghost tiny" onClick={() => void load()}>↻ 重试</button>
        </div>
      </section>
    );
  }

  const horizons = data?.horizons ?? [7, 20, 60];

  return (
    <div className="mainline-returns">
      {/* ============ ① 口径与进度 ============ */}
      <section className="mainline-card">
        <div className="mainline-card-head">
          <h4>回测收益展示（信号后各周期最大涨幅追踪）</h4>
          <span className="muted-text">
            {data
              ? `数据截至 ${formatDate(data.as_of)} · 历史/近期分界 ${formatDate(data.split)}`
                + ` · 同概念 ${data.dedup_days} 个交易日内重复触发折叠为 X2/X3`
                + ` · 追踪 ${horizons.join("/")} 个交易日`
                + (data.cached ? " · 缓存结果" : "")
              : "正在读取…"}
          </span>
        </div>
        <div className="mainline-alert-bar">
          <button className="btn-ghost tiny" disabled={loading}
                  onClick={() => void load()}>
            {loading ? "读取中…" : "↻ 重新读取"}
          </button>
          <button className="btn-ghost tiny"
                  disabled={leadersLoading}
                  title="区间领涨成分股要走 15 GiB 行情仓，约 6~10 秒；结果后端缓存 10 分钟"
                  onClick={() => void loadLeaders()}>
            {leadersLoading ? "算领涨股中…"
              : leadersReady ? "↻ 重算领涨股" : "⟳ 算区间领涨股"}
          </button>
          <span style={{ flex: 1 }} />
          <span className="muted-text">
            信号周期 {statNum(data, "signals") ?? "—"} 个
            （原始告警 {statNum(data, "alert_total") ?? "—"} 条，
            折叠 {statNum(data, "folded") ?? 0} 条）
          </span>
        </div>

        {!data && loading && (
          <div className="mainline-empty">正在计算各周期最大涨幅…（板块级约 0.5 秒）</div>
        )}
        {data && (
          <div className="mainline-metric-grid">
            <MetricCard label="信号周期" value={String(statNum(data, "signals") ?? "—")}
                        hint="同一概念 10 个交易日内的重复触发已折叠为 1 个周期（X2/X3 记在次数列）" />
            <MetricCard label="强 / 中信号"
                        value={`${statNum(data, "strong") ?? 0} / ${statNum(data, "medium") ?? 0}`}
                        hint="只追踪强信号与中信号；弱信号不进入本面板" />
            <MetricCard label="历史 / 近期"
                        value={`${statNum(data, "history") ?? 0} / ${statNum(data, "recent") ?? 0}`}
                        hint={`分界日 ${formatDate(data.split)}：之前算历史，之后算近期与未来`} />
            <MetricCard label="20日平均最大涨幅" value={statPct(data, "avg_gain_20d")}
                        hint="窗口走满的样本；信号日收盘 → 窗口内最高价，理论可捕获空间" />
            <MetricCard label="20日平均实际收益" value={statPct(data, "avg_ret_20d")}
                        tone={toneOf(statNum(data, "avg_ret_20d"))}
                        hint="窗口末收盘 vs 信号日收盘 —— 判断赚不赚钱看这个，不是最大涨幅" />
            <MetricCard label="20日胜率" value={statRatio(data, "win_rate_20d")}
                        hint="20 日实际收益 > 0 的比例（按最大涨幅算会虚高到 97%，没有区分度）· 覆盖全部信号，含被筛掉的板块" />
            {/* 「20日最多浮亏」汇总卡已按用户口径移除（2026-09-25）。
                ⚠️ 底层数据 `worst_20d` **仍然计算并保留** —— 下面两张明细表
                里的「最多浮亏」列还在用它（逐条行 line ~687、板块折叠行 ~941）。
                移除的只是这张"全样本最差一次"的卡片：它把不同板块、不同时间的
                最差值揉成一个数，与"这套信号整体怎么样"没有稳定关系，
                却容易被读成"我大概会亏这么多"。
                要恢复：在「20日胜率」与「胜率达标板块」之间加回一张
                `MetricCard label="20日最多浮亏" value={statPct(data, "worst_20d")}`
                即可（`statPct` / `toneOf` 都还在用，未删）。 */}
            <MetricCard label="胜率达标板块"
                        value={`${statNum(data, "boards_passed") ?? 0} / ${statNum(data, "boards") ?? 0}`}
                        hint='口径：20 日胜率**严格大于**门槛（已走满的 20 日窗口里实际收益 > 0 的比例）——
                              不达标的板块，它的排行与逐条行都不显示；恰好等于门槛的也算不达标；
                              一条 20 日窗口都没走满的板块判不出来，因此不隐藏' />
          </div>
        )}
        {/* ⚠️ 卡片上的均值/胜率覆盖**全部**信号（含被筛掉的板块）：那是"这套信号
            整体怎么样"，不是"下面这张表怎么样"。不说清这一句，用户会拿卡片上的
            胜率和表里每一行对不上账。 */}
        {data && (
          <div className="muted-text">
            上方卡片是<b>全样本</b>口径（含被筛掉的板块）；下方两张表只显示 20 日胜率{" "}
            &gt; {((statNum(data, "min_win_rate") ?? FALLBACK_MIN_WIN_RATE) * 100).toFixed(0)}% 的板块。
          </div>
        )}
        {data && statNum(data, "partial_60d") !== null
          && (statNum(data, "partial_60d") ?? 0) > 0 && (
          <div className="muted-text">
            ℹ️ {statNum(data, "partial_60d")} 个周期的 60 日窗口还没走满 ——
            这些行的「60日最大」按口径<b>暂时不填</b>（显示「——」），
            只有「至今」列给出截至最新数据的进度，避免拿没走完的数和历史行比。
          </div>
        )}
        {data && (statNum(data, "win_rate_hidden") ?? 0) > 0 && (
          <div className="muted-text">
            （已按「20 日胜率 &gt; {((statNum(data, "min_win_rate") ?? FALLBACK_MIN_WIN_RATE) * 100).toFixed(0)}%」
            隐藏 {statNum(data, "win_rate_hidden")} 条信号周期
            （{statNum(data, "boards_hidden")} 个板块，其中历史 {statNum(data, "history_hidden")} 条）
            —— 判定口径是已走满的 20 日窗口里实际收益 &gt; 0 的比例，不是最大涨幅；
            恰好等于 {((statNum(data, "min_win_rate") ?? FALLBACK_MIN_WIN_RATE) * 100).toFixed(0)}% 的也算不达标）
          </div>
        )}
        {data && (statNum(data, "pool_excluded_boards") ?? 0) > 0 && (
          <div className="muted-text">
            🚫 {statNum(data, "pool_excluded_boards")} 个题材已在<b>池级剔除清单</b>里
            （按历史 20 日胜率生成：胜率 ≤ 40% 且已走满 ≥ 3 个窗口），
            它们的 {statNum(data, "pool_excluded_rows")} 条历史信号在本面板也不显示
            —— 后续主线挖掘与告警都不会再包含这些题材。
            「判不准」的（没有历史窗口、或窗口太少的）**不在**这份清单里，仍在池内观察。
          </div>
        )}
        {data && (statNum(data, "boards_pending") ?? 0) > 0 && (
          <div className="muted-text">
            ℹ️ {statNum(data, "boards_pending")} 个板块一条 20 日窗口都还没走满，
            <b>胜率判不出来，因此不隐藏</b>（把它们当"不达标"会让最近一个月的板块整片消失）。
          </div>
        )}
        {leadersError && (
          <div className="error-box">
            区间领涨成分股计算失败：{leadersError}
            <div className="muted-text">（其余列不受影响，可点「⟳ 算区间领涨股」重试）</div>
          </div>
        )}
      </section>

      {/* ============ ② 板块排行 ============ */}
      <section className="mainline-card">
        <div className="mainline-card-head">
          <h4>概念板块收益排行</h4>
          <span className="muted-text">
            只列 20 日胜率 &gt; {((statNum(data, "min_win_rate") ?? FALLBACK_MIN_WIN_RATE) * 100).toFixed(0)}% 的板块
            · 当前 {SORT_LABEL[sort]}
            · 点「20日胜率」列头切换升 / 降序；「领涨股」取该板块最近 3 个不同成分股
          </span>
        </div>
        {boards.length === 0 ? (
          <div className="mainline-empty">
            没有 20 日胜率 &gt; {((statNum(data, "min_win_rate") ?? FALLBACK_MIN_WIN_RATE) * 100).toFixed(0)}% 的板块
            （口径：已走满的 20 日窗口里，窗口末收盘 &gt; 信号日收盘 的比例）。
          </div>
        ) : (
          <div className="mainline-table-scroll">
            <table className="mainline-table">
              <thead>
                <tr>
                  <th>概念板块</th>
                  <th className="num">周期/次数</th>
                  <th>时间范围</th>
                  <th className="num" title="窗口走满样本的平均最大涨幅">最大涨幅 7/20/60日</th>
                  <th className="num" title="窗口末收盘 vs 信号日收盘">实际收益 7/20/60日</th>
                  <th className="num sortable"
                      aria-sort={sort === "win_desc" ? "descending"
                        : sort === "win_asc" ? "ascending" : "none"}
                      title="20 日实际收益 > 0 的比例（只算已走满的 20 日窗口）——板块筛选就按它。点一下可按这列排序">
                    <button className="mainline-sort" onClick={cycleSort}>
                      20日胜率
                      <span className={"mainline-sort-caret"
                        + (sort === "default" ? " idle" : "")}>
                        {SORT_CARET[sort]}
                      </span>
                    </button>
                  </th>
                  <th className="num" title="窗口内最低价相对信号日收盘（最差一次）">最多浮亏</th>
                  <th>领涨成分股</th>
                </tr>
              </thead>
              <tbody>
                {boards.map((item) => <BoardRow key={item.board_code} row={item}
                                               onPickBoard={onPickBoard} />)}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* ============ ③ 逐条追踪 ============ */}
      <section className="mainline-card">
        <div className="mainline-card-head">
          <h4>信号周期逐条追踪</h4>
          <span className="muted-text">
            全量历史，最新的在最上面；共 {rows.length} 条
            {groups > 0
              ? `（告警日期超过 1 个月的已按概念折叠为 ${groups} 组，`
                + `最近一个月 ${kept} 条仍逐条列出）`
              : ""}
            {flat.length > visible.length ? ` · 已显示前 ${visible.length} 条` : ""}
            {view === "recent" ? " · 近一个月与未来（窗口未走满，按「至今」看进度）" : ""}
          </span>
        </div>
        <div className="mainline-alert-bar">
          <div className="mode-switch">
            {([["all", `全部（${counts.all}）`],
               ["recent", `近期与未来（${counts.recent}）`],
               ["history", `历史（${counts.history}）`],
               ["boards", "只看板块排行"]] as const).map(([key, label]) => (
              <button key={key} className={view === key ? "mode-btn active" : "mode-btn"}
                      onClick={() => { setView(key); setShown(PAGE); }}>
                {label}
              </button>
            ))}
          </div>
        </div>
        {view === "boards" ? (
          <div className="mainline-empty">已切到「只看板块排行」——上方表格即是。</div>
        ) : visible.length === 0 ? (
          <div className="mainline-empty">
            没有符合条件的信号周期（已固定按「20 日胜率 &gt;{" "}
            {((statNum(data, "min_win_rate") ?? FALLBACK_MIN_WIN_RATE) * 100).toFixed(0)}%」筛掉不达标的板块）。
          </div>
        ) : (
          <>
            <div className="mainline-table-scroll">
              <table className="mainline-table mainline-returns-table">
                <thead>
                  <tr>
                    <th>告警日期</th>
                    <th>概念题材</th>
                    <th className="num"
                        title="该板块已走满的 20 日窗口里，窗口末收盘 > 信号日收盘 的比例 —— 整张表就按它筛（严格大于门槛才留下）">
                      板块20日胜率
                    </th>
                    <th>等级</th>
                    <th className="num" title="同一概念 10 个交易日内的重复触发次数">次数</th>
                    <th className="num">预警分</th>
                    {horizons.map((days) => (
                      <th key={days} className="num"
                          title={`信号日收盘 → 之后 ${days} 个交易日内最高价的最大涨幅；窗口没走满不填`}>
                        {days}日最大
                      </th>
                    ))}
                    <th className="num" title="最新收盘相对信号日收盘；折叠行是该概念的平均值">至今</th>
                    <th className="num"
                        title="走满的 20 日窗口末收盘收益；折叠行给的是该概念 7 / 20 / 60 日**实际收益的平均值**（20 日加粗）">
                      20日实际
                    </th>
                    <th className="num" title="窗口内最低价相对信号日收盘；折叠行是该概念最差的一次">最多浮亏</th>
                    <th>区间领涨成分股（股名 + 涨幅）</th>
                  </tr>
                </thead>
                <tbody>
                  {visible.map((item) => (item.kind === "group" ? (
                    <GroupRow key={`g-${item.boardCode}`} entry={item}
                              horizons={horizons} leadersReady={leadersReady}
                              expanded={expanded.has(item.boardCode)}
                              onToggle={() => toggle(item.boardCode)}
                              onPickBoard={onPickBoard} />
                  ) : (
                    <Row key={`r-${item.row.board_code}-${item.row.signal_date}`}
                         row={item.row} horizons={horizons}
                         leadersReady={leadersReady} nested={item.nested}
                         rate={gateOf.get(item.row.board_code)}
                         onPickBoard={onPickBoard} />
                  )))}
                </tbody>
              </table>
            </div>
            {flat.length > visible.length && (
              <button className="btn-ghost tiny"
                      onClick={() => setShown((current) => current + PAGE)}>
                加载更多（还有 {flat.length - visible.length} 条）
              </button>
            )}
          </>
        )}
      </section>

      {(data?.gaps?.length ?? 0) > 0 && (
        <div className="mainline-gaps">⚠️ 说明：{data?.gaps?.join("；")}</div>
      )}
      {data?.note && <div className="mainline-foot muted-text">{data.note}</div>}
    </div>
  );
}

/** 指标卡（与回测页签的 `mainline-metric-card` 同款）。 */
function MetricCard({ label, value, hint, tone }: {
  label: string; value: string; hint: string; tone?: "up" | "down" | "flat";
}) {
  return (
    <div className="mainline-metric-card" title={hint}>
      <span className="muted-text">{label}</span>
      <b className={"mainline-metric-value" + (tone ? ` ${tone}` : "")}>{value}</b>
    </div>
  );
}

/** 板块排行行。 */
function BoardRow({ row, onPickBoard }: {
  row: AlertReturnBoard; onPickBoard?: (code: string, name: string) => void;
}) {
  return (
    <tr>
      <td>
        <button className="mainline-link"
                onClick={() => onPickBoard?.(row.board_code, row.board_name)}>
          {row.board_name || row.board_code}
        </button>
      </td>
      <td className="num">
        {row.signals} / {row.alert_total}
      </td>
      <td className="mono muted-text">
        {formatDate(row.first_date)}~{formatDate(row.last_date)}
      </td>
      <td className="num muted-text">
        {formatPct(row.avg_gain_7d, 1)} / {formatPct(row.avg_gain_20d, 1)} /{" "}
        {formatPct(row.avg_gain_60d, 1)}
      </td>
      <td className="num">
        <span className={toneOf(row.avg_ret_7d)}>{formatPct(row.avg_ret_7d, 1)}</span>
        {" / "}
        <b className={toneOf(row.avg_ret_20d ?? row.avg_ret_20d_now)}>
          {formatPct(row.avg_ret_20d ?? row.avg_ret_20d_now, 1)}
        </b>
        {" / "}
        <span className={toneOf(row.avg_ret_60d)}>{formatPct(row.avg_ret_60d, 1)}</span>
      </td>
      <td className="num"
          title={row.gate === "pending"
            ? "该板块一条 20 日窗口都还没走满 —— 胜率判不出来，所以不隐藏"
            : `已走满 ${row.done_20d} 个 20 日窗口，其中 ${row.win_rate_20d === null
              ? "—" : (row.win_rate_20d * 100).toFixed(0)}% 的窗口末收盘为正`}>
        {row.win_rate_20d === null
          ? <span className="muted-text">—（未走满）</span>
          : `${(row.win_rate_20d * 100).toFixed(0)}%`}
        {row.win_rate_20d !== null && (
          <span className="muted-text"> ({row.done_20d}满)</span>
        )}
      </td>
      <td className={"num " + toneOf(row.worst_20d)}>
        {formatPct(row.worst_20d, 1)}
      </td>
      <td>
        <Leaders leaders={row.leaders} source="" ready={row.leaders.length > 0} />
      </td>
    </tr>
  );
}

/** 领涨成分股单元格：`名称 +x.x%` 枚举，最多 3 只。 */
function Leaders({ leaders, source, ready }: {
  /** 逐条表给 `AlertReturnLeader`、板块排行给 `AlertReturnBoardLeader` —— 这里只要三者 */
  leaders: { code: string; name: string; gain_pct: number | null;
             entry_close?: number | null; high?: number | null }[];
  source: string;
  ready: boolean;
}) {
  if (leaders.length === 0) {
    return (
      <span className="muted-text">
        {ready ? "—（成分股名单缺失或信号后还没有个股K线）" : "计算中…"}
      </span>
    );
  }
  return (
    <span className="mainline-leaders">
      {leaders.map((item) => (
        <span key={item.code} className="mainline-leader"
              title={`${item.name || item.code}（${item.code}）`
                + ` 区间最大涨幅 ${item.gain_pct ?? "—"}%`
                + (item.high && item.entry_close
                  ? `　${item.entry_close.toFixed(2)} → ${item.high.toFixed(2)}` : "")}>
          <b>{item.name || item.code}</b>
          <i className={toneOf(item.gain_pct)}>{formatPct(item.gain_pct, 1)}</i>
        </span>
      ))}
      {source === "member" && (
        <span className="muted-text"
              title="该板块没有提纯股池结果，回落到原始成员表（可能含只沾边的公司）">
          ⚠️原始成员
        </span>
      )}
    </span>
  );
}

/**
 * 一个周期的窗口单元格。
 *
 * 三条显示规则（见组件顶部注释）：
 * * `done` → 直接给最终值
 * * `partial` → **最终值留空**，给出"至今 x%（剩 N 日）"
 * * `pending` → 信号日之后还没有K线，整列"——"
 */
function WindowCell({ stat }: { stat: AlertReturnWindow | undefined }) {
  if (!stat) return <td className="num muted-text">—</td>;
  if (stat.status === "pending") {
    return (
      <td className="num muted-text" title="信号日之后还没有K线">
        —
      </td>
    );
  }
  if (stat.status === "done") {
    return (
      <td className={"num " + toneOf(stat.max_gain_pct)}
          title={`最大涨幅出现在 ${formatDate(stat.max_gain_date)}`
            + `（${stat.bars_used} 个交易日走满）`}>
        {formatPct(stat.max_gain_pct, 1)}
      </td>
    );
  }
  return (
    <td className="num" title={`窗口还没走满（已 ${stat.bars_used} 个交易日，`
      + `还差 ${stat.remaining_days} 日）—— 按口径暂时不填最终值`}>
      <span className="muted-text">——</span>
      <div className="mainline-window-partial">
        至今 <span className={toneOf(stat.current_gain_pct)}>
          {formatPct(stat.current_gain_pct, 1)}
        </span>
        <span className="muted-text">/剩{stat.remaining_days}日</span>
      </div>
    </td>
  );
}

/** 「板块20日胜率」单元格：整张表就是按它筛的，所以它必须能被核对。 */
function RateCell({ rate }: { rate?: AlertReturnBoard }) {
  const value = rate?.win_rate_20d ?? null;
  if (value === null) {
    return (
      <td className="num"
          title={rate?.gate === "pending"
            ? "该板块一条 20 日窗口都还没走满 —— 胜率判不出来，因此不隐藏"
            : "这次载荷里没有该板块的判定数据"}>
        <span className="muted-text">—（未走满）</span>
      </td>
    );
  }
  return (
    <td className="num"
        title={`该板块已走满 ${rate?.done_20d ?? 0} 个 20 日窗口，`
          + `其中 ${(value * 100).toFixed(1)}% 的窗口末收盘为正`
          + " —— 严格大于门槛才留在本表"}>
      {`${(value * 100).toFixed(0)}%`}
      <span className="muted-text"> ({rate?.done_20d ?? 0}满)</span>
    </td>
  );
}

/** 逐条追踪行 = 一个信号周期。 */
function Row({ row, horizons, leadersReady, rate, nested, onPickBoard }: {
  row: AlertReturnRow; horizons: number[]; leadersReady: boolean;
  /** 该板块的 20 日胜率判定 —— 让"这一行为什么在表里"可以被核对 */
  rate?: AlertReturnBoard;
  /** 从折叠组里展开出来的明细行（左侧加一道标记） */
  nested?: boolean;
  onPickBoard?: (code: string, name: string) => void;
}) {
  const ret20 = row.windows["20"];
  const classes = [row.segment === "recent" ? "mainline-returns-recent" : "",
                   nested ? "mainline-returns-nested" : ""].filter(Boolean).join(" ");
  return (
    <tr className={classes}>
      <td className="mono">{formatDate(row.signal_date)}</td>
      <td>
        <button className="mainline-link"
                onClick={() => onPickBoard?.(row.board_code, row.board_name)}>
          {row.board_name || row.board_code}
        </button>
      </td>
      <RateCell rate={rate} />
      <td>{row.level_label || row.level}</td>
      <td className="num" title={row.alert_count > 1
        ? `触发日：${row.alert_dates.join("、")}` : ""}>
        {row.alert_count > 1
          ? <b className="mainline-count-x">X{row.alert_count}</b>
          : <span className="muted-text">1</span>}
      </td>
      <td className="num mono">{formatScore(row.score, 1)}</td>
      {horizons.map((days) => (
        <WindowCell key={days} stat={row.windows[String(days)]} />
      ))}
      <td className={"num " + toneOf(row.current_pct)}
          title={`信号日收盘 ${row.entry_close ?? "—"} → 最新 ${formatDate(row.latest_date)}`
            + ` 收盘 ${row.latest_close ?? "—"}`}>
        {formatPct(row.current_pct, 1)}
      </td>
      <td className={"num " + toneOf(ret20?.ret_pct)}>
        {ret20 ? formatPct(ret20.ret_pct, 1) : "—"}
      </td>
      <td className={"num " + toneOf(ret20?.worst_pct)}>
        {ret20 ? formatPct(ret20.worst_pct, 1) : "—"}
      </td>
      <td><Leaders leaders={row.leaders} source={row.leaders_source}
                   ready={leadersReady} /></td>
    </tr>
  );
}

/**
 * 折叠行 = 一个概念（告警日期超过 1 个月的那些信号周期合并成一行）。
 *
 * ## 这一行上的数是什么
 *
 * 全部取自后端的板块汇总（`boards[]`，口径见 `summarize_boards`）：
 *
 * * 各周期列 → 该概念**窗口走满样本的平均最大涨幅**（7 / 20 / 60 日）
 * * 「20日实际」→ 平均**实际收益**，7 / 20 / 60 日都给（20 日加粗）——
 *   这是"各项各周期收益"里唯一能看出"拿住到底赚不赚钱"的一组
 * * 「板块20日胜率」→ **总胜率**，也是整张表筛选用的那个数
 * * 「至今」「最多浮亏」→ 平均值 / 最差的一次
 *
 * 统计口径覆盖该概念**全部**信号周期（含最近一个月那几条 —— 它们仍单独列在
 * 下方，不在这组里），所以展开后自己手算平均值会和这里对不上，
 * 差别就是那几条。悬停提示里写明了这一点。
 *
 * 排版上刻意做得比明细行"重"：字重、底色和一道分隔线是唯一能让人一眼看出
 * "这行是汇总、不是某一天"的线索。
 */
function GroupRow({ entry, horizons, leadersReady, expanded, onToggle, onPickBoard }: {
  entry: Extract<TrackEntry, { kind: "group" }>;
  horizons: number[]; leadersReady: boolean; expanded: boolean;
  onToggle: () => void;
  onPickBoard?: (code: string, name: string) => void;
}) {
  const board = entry.board;
  const head = entry.rows[0];
  const name = board?.board_name || head.board_name || entry.boardCode;
  const scores = entry.rows.map((row) => row.score)
    .filter((value): value is number => typeof value === "number");
  const meanScore = scores.length
    ? scores.reduce((sum, value) => sum + value, 0) / scores.length : null;
  const alerts = board?.alert_total
    ?? entry.rows.reduce((sum, row) => sum + row.alert_count, 0);
  const span = `${formatDate(board?.first_date ?? entry.rows[entry.rows.length - 1].signal_date)}`
    + `~${formatDate(board?.last_date ?? head.signal_date)}`;
  return (
    <tr className={"mainline-returns-group" + (expanded ? " open" : "")}>
      <td className="mono muted-text" title="这一组覆盖的信号区间">{span}</td>
      <td>
        <button className="mainline-group-toggle" onClick={onToggle}
                aria-expanded={expanded}
                title={expanded
                  ? "收起这个概念的历史明细"
                  : "展开这个概念的历史明细（按告警日期倒序）"}>
          <span className="mainline-caret">{expanded ? "▾" : "▸"}</span>
          {name}
        </button>
        <span className="muted-text">
          {" "}{entry.rows.length} 个周期 / {alerts} 次告警
        </span>
        {onPickBoard && (
          <button className="mainline-link mainline-group-drill"
                  title="查看该板块三层明细"
                  onClick={() => onPickBoard(entry.boardCode, name)}>↗</button>
        )}
      </td>
      <RateCell rate={board} />
      <td className="muted-text">
        {board
          ? <><span className="mainline-level level-strong">🔴{board.strong}</span>
            {" "}
            <span className="mainline-level level-medium">🟡{board.medium}</span></>
          : "—"}
      </td>
      <td className="num muted-text">{alerts}</td>
      <td className="num mono muted-text">{formatScore(meanScore, 1)}</td>
      {horizons.map((days) => (
        <td key={days} className="num muted-text"
            title={`该概念窗口走满样本在 ${days} 日内的平均最大涨幅`}>
          {formatPct(gainAverage(board, days), 1)}
        </td>
      ))}
      <td className={"num " + toneOf(board?.avg_current ?? null)}
          title="该概念所有信号周期「至今」收益的平均值">
        {formatPct(board?.avg_current ?? null, 1)}
      </td>
      <td className="num"
          title={"该概念全部信号周期（含最近一个月那几条，它们单列在下方）"
            + "的实际收益平均值：7 / 20 / 60 日，只算窗口走满的样本"}>
        <span className={toneOf(board?.avg_ret_7d ?? null)}>
          {formatPct(board?.avg_ret_7d ?? null, 1)}
        </span>
        {" / "}
        <b className={toneOf(board?.avg_ret_20d ?? null)}>
          {formatPct(board?.avg_ret_20d ?? null, 1)}
        </b>
        {" / "}
        <span className={toneOf(board?.avg_ret_60d ?? null)}>
          {formatPct(board?.avg_ret_60d ?? null, 1)}
        </span>
      </td>
      <td className={"num " + toneOf(board?.worst_20d ?? null)}
          title="该概念所有样本里最差的一次：20 日窗口内最低价相对信号日收盘">
        {formatPct(board?.worst_20d ?? null, 1)}
      </td>
      <td><Leaders leaders={board?.leaders ?? []} source=""
                   ready={leadersReady} /></td>
    </tr>
  );
}
