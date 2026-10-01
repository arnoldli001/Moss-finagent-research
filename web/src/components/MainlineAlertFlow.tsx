import { useEffect, useMemo, useState, type CSSProperties } from "react";
import {
  formatDate,
  formatPct,
  formatScore,
  LEVEL_ORDER,
  mainlineApi,
  toneOf,
  type MainlineAlert,
} from "../mainlineApi";

/**
 * 主线挖掘 · 告警信号流水。
 *
 * ## 这张表要回答的问题
 *
 * 不是"今天触发了什么"，而是"**过去所有告警后来兑现了吗**"。所以每一行都带着
 * 告警后的 5/10/20/60 日涨幅与最大涨幅 —— 这是用户复盘模型可信度的唯一依据。
 * 因此：
 *
 * - 默认按告警日期**倒序**（最近的先看），同一天内强信号在前；
 * - 「未兑现」的行**整行标灰**：已经过了 20 个交易日、却连一点正收益都没有的告警。
 *   注意这里只降透明度、**不移除、不改变排序** —— 把失败样本藏起来正是最该避免的事；
 * - 等级筛选用按钮而不是下拉：三档一眼看得到各有几条，也少一次点击。
 *
 * ## 唯一会**隐藏**行的规则：20 日胜率未过门槛板块的强/中告警
 *
 * 用户的固定口径：*"不显示 20 日胜率低于 50% 的概念板块回测信息"*，
 * 随后收紧为 *"只显示 20 日胜率**大于** 50% 的概念板块"*，
 * 2026-09-25 又按"减少假阳性"放宽到 **40%**，
 * **2026-09-30 放宽到 39%**（让 CRO概念 0.3913 回到本页签）。所以**强信号与中信号**里，
 * 属于"20 日胜率未超过门槛"板块的那些行不显示；
 * **弱信号不受这条规则影响**（它问的不是弱信号）。
 *
 * ⚠️ 门槛**从接口读**（`min_win_rate`），不在前端写死：写死之后后端一改门槛，
 * 界面上的文案就会和实际筛选不一致，而这种不一致不会报错。
 *
 * 三个必须写明的边界：
 *
 * * **胜率只有一处口径**。这里不去自己数 `ret_20d`，而是问后端
 *   `/board-win-rates` —— 它和「回测收益展示」面板**共用同一份缓存结果**。
 *   自己数一遍的话，同一句"20 日胜率"会在两个页签里算出不同的数，
 *   而且界面照样出数、不会报错。
 * * **判不出来的不隐藏**。一条 20 日窗口都没走满的板块没有胜率，
 *   谈不上"未过门槛"（后端标 `gate = "pending"`）。
 * * **接口挂了就全显示**，并把这句写在界面上。宁可多显示，也不要在
 *   拿不到判定依据时静默藏掉用户的数据。
 *
 * 隐藏了多少条**必须写在表头上**：不写的话，"告警怎么变少了"会被读成"告警丢了"，
 * 而这是最容易被当成数据故障报上来的那种现象。
 *
 * ## 列宽为什么用 grid 而不是 table
 *
 * 与拥挤度告警面板同一套做法（见 `SectorCrowdingAlertPanel` 的注释）：表头与数据行
 * 共用 `--mainline-alert-grid` 一份列宽，所以能严格对齐；`table` 的自适应列宽在这里
 * 反而会出现"数字列忽宽忽窄、表头对不上"。窄屏整表横向滚动，不压扁数字。
 */

/** 触发维度 key → 中文短名（后端给的是 key，表格里要短标签）。 */
const DIM_TEXT: Record<string, string> = {
  trading: "交易",
  moneyflow: "资金",
  leverage: "杠杆",
  chips: "筹码",
  sentiment: "情绪",
  valuation: "估值",
  momentum: "动量",
  volume: "量能",
};

function dimText(key: string): string {
  return DIM_TEXT[key] ?? key;
}

/** 告警等级 → 行 class（强/中/弱三色 + none 兜底）。 */
function levelClass(level: string): string {
  const key = (level || "").toLowerCase();
  if (key === "strong" || key === "medium" || key === "weak") return key;
  return "none";
}

/**
 * 是否"未兑现"。
 *
 * 判定口径（**故意保守**）：有 20 日收益数据且 ≤ 0，说明告警后一个月都没赚钱；
 * 或者 20/60 日数据都还缺（还没走完），且未被确认 —— 这时也不好算"兑现"。
 * 只要任意一个窗口为正，就认为方向是对的，不算失败。
 */
export function isUnrealized(alert: MainlineAlert): boolean {
  if (alert.confirmed) return false;
  const windows = [alert.ret_5d, alert.ret_10d, alert.ret_20d, alert.ret_60d]
    .filter((value): value is number => value !== null && value !== undefined
      && Number.isFinite(value));
  if (windows.length === 0) return true;
  if (windows.some((value) => value > 0)) return false;
  return true;
}

/** 表头与数据行共用的列宽（改这里同时改两处；数组项数 = 列数，多一项就串位）。 */
const GRID = [
  "86px",    // 告警日期
  "38px",    // 等级圆点
  "minmax(96px, 1.2fr)",  // 板块
  "58px",    // 预警分
  "64px",    // 等级文字
  "minmax(88px, 1fr)",    // 触发维度
  "64px",    // 当日涨跌
  "60px",    // 5日
  "60px",    // 10日
  "60px",    // 20日
  "60px",    // 60日
  "72px",    // 最大涨幅
  "82px",    // 最大涨幅日期
  "72px",    // 是否确认
].join(" ");

type Props = {
  alerts: MainlineAlert[];
  onPick: (code: string, name: string) => void;
  loading?: boolean;
};

/** 会被"20 日胜率"规则管到的等级：**只有强信号与中信号**（弱信号不受影响）。 */
function isGatedLevel(level: string): boolean {
  const key = levelClass(level);
  return key === "strong" || key === "medium";
}

export default function MainlineAlertFlow({ alerts, onPick, loading }: Props) {
  const [level, setLevel] = useState<"all" | "strong" | "medium" | "weak">("all");
  const [onlyUnrealized, setOnlyUnrealized] = useState(false);
  /** 20 日胜率不达标、要隐藏的板块；`null` = 还没拿到 → 此时**不隐藏任何行**。 */
  const [hiddenBoards, setHiddenBoards] = useState<Set<string> | null>(null);
  /** 门槛本身也从接口读，界面文案不能写死一个数（见组件文档的 ⚠️）。 */
  const [minWinRate, setMinWinRate] = useState<number | null>(null);
  const [gateError, setGateError] = useState("");

  useEffect(() => {
    let alive = true;
    mainlineApi.boardWinRates()
      .then((payload) => {
        if (!alive) return;
        setHiddenBoards(new Set(payload.boards.filter((item) => item.hidden)
          .map((item) => item.board_code)));
        setMinWinRate(typeof payload.min_win_rate === "number"
          ? payload.min_win_rate : null);
        setGateError("");
      })
      .catch((exc: unknown) => {
        if (!alive) return;
        // 拿不到判定依据就**全显示**并明说：宁可多显示，
        // 也不要在没有依据时静默把用户的数据藏起来。
        setHiddenBoards(null);
        setGateError(exc instanceof Error ? exc.message : String(exc));
      });
    return () => { alive = false; };
  }, []);

  /** 被这条规则挡掉的告警（只数强/中信号）。 */
  const hidden = useMemo(() => {
    if (hiddenBoards === null) return [];
    return alerts.filter((row) => isGatedLevel(row.level)
      && hiddenBoards.has(row.board_code));
  }, [alerts, hiddenBoards]);

  /**
   * 过了胜率门槛的告警 —— 表上的一切（计数、兑现率、行）都以它为准。
   *
   * 计数与兑现率必须跟着一起算：只筛行不筛计数，就会出现"标着 312 条、
   * 实际列 200 行"的对不上账。
   */
  const gated = useMemo(() => {
    if (hiddenBoards === null) return alerts;
    return alerts.filter((row) => !(isGatedLevel(row.level)
      && hiddenBoards.has(row.board_code)));
  }, [alerts, hiddenBoards]);

  const counts = useMemo(() => ({
    all: gated.length,
    strong: gated.filter((row) => levelClass(row.level) === "strong").length,
    medium: gated.filter((row) => levelClass(row.level) === "medium").length,
    weak: gated.filter((row) => levelClass(row.level) === "weak").length,
    unrealized: gated.filter(isUnrealized).length,
  }), [gated]);

  const rows = useMemo(() => {
    let list = level === "all"
      ? [...gated]
      : gated.filter((row) => levelClass(row.level) === level);
    if (onlyUnrealized) list = list.filter(isUnrealized);
    return list.sort((left, right) => {
      // 日期倒序（字符串形式的紧凑日期可直接比较大小）
      const dateCompare = String(right.trade_date).localeCompare(
        String(left.trade_date));
      if (dateCompare !== 0) return dateCompare;
      const levelCompare = (LEVEL_ORDER[levelClass(right.level)] ?? 0)
        - (LEVEL_ORDER[levelClass(left.level)] ?? 0);
      if (levelCompare !== 0) return levelCompare;
      return left.board_code.localeCompare(right.board_code);
    });
  }, [gated, level, onlyUnrealized]);

  /** 兑现率 = 有 20 日数据的告警里，20 日收益为正的比例（复盘用的一眼结论）。 */
  const hitRate = useMemo(() => {
    const done = gated.filter((row) => row.ret_20d !== null
      && row.ret_20d !== undefined && Number.isFinite(row.ret_20d));
    if (done.length === 0) return null;
    const hit = done.filter((row) => (row.ret_20d ?? 0) > 0).length;
    return { hit, total: done.length, rate: hit / done.length };
  }, [gated]);

  /** 门槛文案：接口还没回来时先按 39% 显示（与后端 `DEFAULT_MIN_WIN_RATE` 一致）。 */
  const gatePct = `${((minWinRate ?? 0.39) * 100).toFixed(0)}%`;

  return (
    <section className="mainline-card mainline-alert-flow">
      <div className="mainline-card-head">
        <h4>告警信号流水</h4>
        <span className="muted-text">
          共 {counts.all} 条
          {hitRate
            ? ` · 20 日兑现 ${hitRate.hit}/${hitRate.total}
               （${(hitRate.rate * 100).toFixed(0)}%）`
            : " · 还没有走满 20 日的告警"}
          {counts.unrealized > 0 ? ` · 未兑现 ${counts.unrealized} 条` : ""}
          {hidden.length > 0
            ? ` · 已隐藏 ${hidden.length} 条（20 日胜率未超过 ${gatePct} 的板块，强/中信号）`
            : ""}
        </span>
      </div>

      {gateError && (
        <div className="error-box">
          读取板块 20 日胜率失败，本次<b>没有隐藏任何告警</b>（宁可多显示）：
          {gateError}
          <div className="muted-text">
            （接口 /api/v1/mainline/board-win-rates；恢复后本面板会自行生效）
          </div>
        </div>
      )}

      <div className="mainline-alert-bar">
        <div className="mode-switch">
          {([
            { key: "all", label: `全部（${counts.all}）` },
            { key: "strong", label: `🔴 强（${counts.strong}）` },
            { key: "medium", label: `🟡 中（${counts.medium}）` },
            { key: "weak", label: `🟢 弱（${counts.weak}）` },
          ] as const).map((item) => (
            <button key={item.key}
                    className={level === item.key ? "mode-btn active" : "mode-btn"}
                    onClick={() => setLevel(item.key)}>
              {item.label}
            </button>
          ))}
        </div>
        <span style={{ flex: 1 }} />
        <label className="chart-toggle">
          <input type="checkbox" checked={onlyUnrealized}
                 onChange={(event) => setOnlyUnrealized(event.target.checked)}
                 title="只看至今没有任何正收益、也没被确认的告警（复盘失败样本）" />
          只看未兑现
        </label>
      </div>

      {loading && rows.length === 0 && <div className="info-box">正在读取告警…</div>}
      {!loading && rows.length === 0 && (
        <div className="mainline-empty">
          {alerts.length === 0
            ? "暂无告警记录。告警由每日评分任务产生（总分 ≥ 阈值且触发维度达标）—— 先点顶部「立即刷新」跑当日评分。"
            : "当前筛选下没有告警（切回「全部」看看）。"}
        </div>
      )}

      {rows.length > 0 && (
        <div className="mainline-table-scroll">
          <div className="mainline-alert-scroll"
               style={{ "--mainline-alert-grid": GRID } as CSSProperties}>
            <div className="mainline-alert-header" role="row">
              <span className="mainline-hcell">告警日期</span>
              <span className="mainline-hcell" />
              <span className="mainline-hcell">板块</span>
              <span className="mainline-hcell num">预警分</span>
              <span className="mainline-hcell">等级</span>
              <span className="mainline-hcell">触发维度</span>
              <span className="mainline-hcell num">当日涨跌</span>
              <span className="mainline-hcell num" title="告警后 5 个交易日涨幅">5日</span>
              <span className="mainline-hcell num" title="告警后 10 个交易日涨幅">10日</span>
              <span className="mainline-hcell num" title="告警后 20 个交易日涨幅">20日</span>
              <span className="mainline-hcell num" title="告警后 60 个交易日涨幅">60日</span>
              <span className="mainline-hcell num" title="告警后区间最大涨幅">最大涨幅</span>
              <span className="mainline-hcell">最大涨幅日</span>
              <span className="mainline-hcell">是否确认</span>
            </div>

            <ul className="mainline-alert-list">
              {rows.map((row) => {
                const unrealized = isUnrealized(row);
                return (
                  <li key={row.alert_id || `${row.board_code}-${row.trade_date}`}
                      className={`mainline-alert-item level-${levelClass(row.level)}`
                        + (unrealized ? " unrealized" : "")}>
                    <span className="mono mainline-alert-date">
                      {formatDate(row.trade_date)}
                    </span>
                    <span className={`mainline-alert-dot level-${levelClass(row.level)}`}
                          title={row.level_label || row.level} />
                    <button className="mainline-link mainline-alert-name"
                            onClick={() => onPick(row.board_code, row.board_name)}
                            title="点击查看该板块三层明细">
                      {row.board_name || row.board_code}
                    </button>
                    <b className="mono num">{formatScore(row.score, 1)}</b>
                    <span className={`mainline-level level-${levelClass(row.level)}`}>
                      {row.level_label || row.level || "—"}
                    </span>
                    <span className="mainline-alert-dims"
                          title={row.triggered_dims.map(dimText).join("、")}>
                      {row.triggered_dims.length === 0
                        ? "—"
                        : row.triggered_dims.map(dimText).join("/")}
                      {row.resonance && <i className="mainline-res-tag">共振</i>}
                    </span>
                    <span className={"mono num " + toneOf(row.change_pct)}>
                      {formatPct(row.change_pct, 2)}
                    </span>
                    <span className={"mono num " + toneOf(row.ret_5d)}>
                      {formatPct(row.ret_5d, 2)}
                    </span>
                    <span className={"mono num " + toneOf(row.ret_10d)}>
                      {formatPct(row.ret_10d, 2)}
                    </span>
                    <span className={"mono num " + toneOf(row.ret_20d)}>
                      {formatPct(row.ret_20d, 2)}
                    </span>
                    <span className={"mono num " + toneOf(row.ret_60d)}>
                      {formatPct(row.ret_60d, 2)}
                    </span>
                    <b className={"mono num " + toneOf(row.max_gain_pct)}>
                      {formatPct(row.max_gain_pct, 1)}
                    </b>
                    <span className="muted-text mono">
                      {row.max_gain_date ? formatDate(row.max_gain_date) : "—"}
                    </span>
                    <span className={row.confirmed ? "mainline-confirmed"
                      : "muted-text"}
                          title={row.push_note
                            ? `推送备注：${row.push_note}`
                            : (row.pushed ? "已推送" : "未推送")}>
                      {row.confirmed
                        ? `✅ ${row.confirmed_date
                          ? formatDate(row.confirmed_date) : "已确认"}`
                        : unrealized ? "未兑现" : "待观察"}
                    </span>
                  </li>
                );
              })}
            </ul>
          </div>
        </div>
      )}

      {rows.length > 0 && (
        <div className="mainline-foot muted-text">
          「未兑现」= 未被确认，且 5/10/20/60 日涨幅<b>没有一个是正的</b>（整行标灰，
          但<b>不隐藏</b>：失败样本是复盘模型最该看的部分）；
          「预警分」是触发当日总分（含门控加分）；60 日列在告警不足 60 个交易日时为空。
          <div>
            唯一被隐藏的是<b>强/中信号里 20 日胜率未超过 {gatePct} 的板块</b>
            （胜率 = 该板块已走满的 20 日窗口里实际收益 &gt; 0 的比例，
            <b>严格大于 {gatePct}</b> 才留下 —— 恰好等于门槛的也算不达标，
            与「回测报告 → 回测收益展示」同一口径）；
            弱信号、以及一条 20 日窗口都没走满的板块（胜率判不出来）都不受这条规则影响。
          </div>
        </div>
      )}
    </section>
  );
}
