import { Fragment, useMemo, useState } from "react";
import {
  DIRECTION_TEXT,
  formatDate,
  formatRatioPct,
  formatScore,
  FUTURES_KIND_TEXT,
  LEVEL_ORDER,
  SCOPE_TEXT,
  type FuturesAlert,
  type FuturesMapping,
  type MainlineFuturesSnapshot,
} from "../mainlineApi";

/**
 * 主线挖掘 · 期货先行信号子视图。
 *
 * ## 为什么把四块内容放在一个子视图里
 *
 * 期货数据的价值在**传导链**上：一个品种异动（① 仪表盘）→ 它和哪些板块历史上
 * 同向（② 联动热力图）→ 背后的产业逻辑是什么（③ 映射详情）→ 今天有没有真的
 * 触发告警（④ 告警列表）。四块互相引用（点一个品种要同时高亮矩阵行、展开映射表），
 * 拆成四个页签会让人反复来回切、还记不住刚才点的是哪个品种。所以合成一屏，
 * 用**共享选中态**串起来。
 *
 * ## 手写图形的两处细节
 *
 * - **强度条**用 div 宽度百分比（不是 SVG）：89 个品种、纯一维长度编码，
 *   `width: x%` 最简单也最省事，还能直接吃 CSS 过渡。
 * - **联动热力图**用 CSS Grid 画：行 = 期货品种、列 = 板块，格子背景按相关系数插值。
 *   缺失的格子留底色 —— **不能把"没有相关系数"画成 0**（0 意味着"不相关"，
 *   是个有信息的结论；缺失是"没算"，不是一回事）。
 *
 * ## 期货的 ret_* 是小数
 *
 * `ret_5d: 0.021` 表示 +2.1%（与板块评分里的百分数口径**不同**），所以这里统一走
 * `formatRatioPct`。这个坑不写清楚，早晚会有人在某个面板里再乘错一次 100。
 */

/** 相关系数 → 颜色：+1 红（强同向）/ 0 近乎透明 / -1 绿（强反向）。 */
function corrColor(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "rgba(139,152,165,0.10)";
  }
  const clamped = Math.max(-1, Math.min(1, value));
  const abs = Math.abs(clamped);
  if (clamped >= 0) {
    return `rgba(217,92,74,${(0.10 + abs * 0.78).toFixed(3)})`;
  }
  return `rgba(46,168,110,${(0.10 + abs * 0.78).toFixed(3)})`;
}

/** 异动强度 → 条形颜色（冷 → 热 5 档，与热力图同一套语义但更省事）。 */
function intensityColor(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "rgba(139,152,165,0.35)";
  }
  if (value >= 80) return "#c8503c";
  if (value >= 65) return "#d98a3b";
  if (value >= 50) return "#b9a33f";
  if (value >= 35) return "#3f8f7a";
  return "#3a6ea8";
}

/** 等级 → class 后缀（只认三档，其余归 none）。 */
function levelClass(level: string): string {
  const key = (level || "").toLowerCase();
  return ["strong", "medium", "weak"].includes(key) ? key : "none";
}

type Props = {
  data: MainlineFuturesSnapshot | null;
  loading?: boolean;
  error?: string;
};

export default function MainlineFutures({ data, loading, error }: Props) {
  /** 当前选中的期货品种（点强度条 / 矩阵行 / 告警都会设它） */
  const [picked, setPicked] = useState("");
  const [scope, setScope] = useState<"all" | "domestic" | "foreign"
    | "non_futures">("all");
  const [alertLevel, setAlertLevel] = useState<"all" | "strong" | "medium"
    | "weak">("all");
  /** 联动矩阵只画 |相关系数| ≥ 该值的格子（0 = 全部画） */
  const [corrMin, setCorrMin] = useState(0.3);

  const signals = data?.signals ?? [];
  const alerts = data?.alerts ?? [];
  const mappings = data?.mappings ?? [];
  const correlation = data?.correlation ?? {};
  const counts = data?.counts ?? {};

  const rows = useMemo(() => {
    const list = scope === "all"
      ? [...signals]
      : signals.filter((row) => (row.kind || "domestic") === scope);
    // intensity 降序；缺强度沉底（当 0 排会把"没算"读成"最弱"）
    return list.sort((left, right) => {
      const a = left.intensity;
      const b = right.intensity;
      const aMissing = a === null || a === undefined || !Number.isFinite(a);
      const bMissing = b === null || b === undefined || !Number.isFinite(b);
      if (aMissing && bMissing) return left.code.localeCompare(right.code);
      if (aMissing) return 1;
      if (bMissing) return -1;
      if (b !== a) return b - a;
      return left.code.localeCompare(right.code);
    });
  }, [signals, scope]);

  /** 矩阵列（板块名）：从相关系数矩阵汇总，按"被关联次数"降序，最多 24 列。 */
  const boardColumns = useMemo(() => {
    const counter = new Map<string, number>();
    for (const row of Object.values(correlation)) {
      for (const [board, value] of Object.entries(row ?? {})) {
        const name = String(board || "").trim();
        if (!name) continue;
        if (value === null || value === undefined || !Number.isFinite(value)) {
          continue;                      // 只有真算出系数才算"被关联"
        }
        counter.set(name, (counter.get(name) ?? 0) + 1);
      }
    }
    return [...counter.entries()]
      .sort((left, right) => right[1] - left[1]
        || left[0].localeCompare(right[0], "zh-Hans-CN"))
      .slice(0, 24)
      .map(([name]) => name);
  }, [correlation]);

  /** 矩阵行：当前选中品种永远第一行，其余按最强 |系数| 降序，最多 40 行。 */
  const matrixRows = useMemo(() => {
    const nameOf = new Map(signals.map((row) => [row.code, row.name]));
    const codes = Object.keys(correlation).filter((code) => {
      const row = correlation[code] ?? {};
      return Object.values(row).some((value) => value !== null
        && value !== undefined && Number.isFinite(value));
    });
    const maxAbs = (code: string): number => {
      const values = Object.values(correlation[code] ?? {})
        .filter((value): value is number => Number.isFinite(value))
        .map((value) => Math.abs(value));
      return values.length > 0 ? Math.max(...values) : 0;
    };
    codes.sort((left, right) => {
      if (left === picked) return -1;
      if (right === picked) return 1;
      const diff = maxAbs(right) - maxAbs(left);
      if (diff !== 0) return diff;
      return left.localeCompare(right);
    });
    return codes.slice(0, 40).map((code) => ({
      code,
      name: nameOf.get(code) ?? code,
      row: correlation[code] ?? {},
    }));
  }, [correlation, signals, picked]);

  /** 当前选中品种的映射行（点品种才展开，避免一次铺几百行）。 */
  const pickedMappings = useMemo(() => {
    if (!picked) return [];
    return mappings.filter((row) => row.future_code === picked);
  }, [mappings, picked]);

  const pickedSignal = signals.find((row) => row.code === picked) ?? null;

  const alertCounts = useMemo(() => ({
    all: alerts.length,
    strong: alerts.filter((row) => levelClass(row.level) === "strong").length,
    medium: alerts.filter((row) => levelClass(row.level) === "medium").length,
    weak: alerts.filter((row) => levelClass(row.level) === "weak").length,
  }), [alerts]);

  const alertRows = useMemo(() => {
    const list = alertLevel === "all"
      ? [...alerts]
      : alerts.filter((row) => levelClass(row.level) === alertLevel);
    return list.sort((left, right) => {
      const dateCompare = String(right.date).localeCompare(String(left.date));
      if (dateCompare !== 0) return dateCompare;
      return (LEVEL_ORDER[levelClass(right.level)] ?? 0)
        - (LEVEL_ORDER[levelClass(left.level)] ?? 0);
    });
  }, [alerts, alertLevel]);

  if (error) {
    return (
      <section className="mainline-card">
        <div className="error-box">读取期货先行信号失败：{error}</div>
      </section>
    );
  }

  return (
    <div className="mainline-futures">
      {/* ① 异动仪表盘 */}
      <section className="mainline-card">
        <div className="mainline-card-head">
          <h4>异动仪表盘</h4>
          <span className="muted-text">
            内盘 {counts.domestic
              ?? signals.filter((row) => row.kind === "domestic").length}
            {" "}· 外盘 {counts.foreign
              ?? signals.filter((row) => row.kind === "foreign").length}
            {" "}· 非期货 {counts.non_futures
              ?? signals.filter((row) => row.kind === "non_futures").length}
            {" "}· 告警 {counts.alert ?? alerts.length}
            {" "}· 交易日 {formatDate(data?.trade_date)}
            · 条形长度 = 异动强度（0~100）
          </span>
        </div>

        <div className="mainline-alert-bar">
          <div className="mode-switch">
            {([
              { key: "all", label: `全部（${signals.length}）` },
              { key: "domestic", label: `内盘（${counts.domestic ?? "—"}）` },
              { key: "foreign", label: `外盘（${counts.foreign ?? "—"}）` },
              { key: "non_futures", label: `非期货（${counts.non_futures ?? "—"}）` },
            ] as const).map((item) => (
              <button key={item.key}
                      className={scope === item.key ? "mode-btn active" : "mode-btn"}
                      onClick={() => setScope(item.key)}>
                {item.label}
              </button>
            ))}
          </div>
          <span style={{ flex: 1 }} />
          {picked && (
            <button className="btn-ghost tiny" onClick={() => setPicked("")}>
              取消选中 {picked}
            </button>
          )}
          <span className="muted-text">点品种看期股映射</span>
        </div>

        {loading && rows.length === 0 && <div className="info-box">正在读取期货信号…</div>}
        {!loading && signals.length === 0 && (
          <div className="mainline-empty">
            暂无期货信号。该子视图需要期货日线 + 持仓量数据；若尚未同步，
            看下方 gaps 说明（或先执行数据同步任务）。
          </div>
        )}

        <div className="mainline-gauge-scroll">
          {rows.map((row) => {
            const intensity = row.intensity;
            const safe = intensity === null || intensity === undefined
              || !Number.isFinite(intensity)
              ? 0 : Math.max(0, Math.min(100, intensity));
            const level = levelClass(row.level);
            return (
              <button key={row.code}
                      className={"mainline-gauge-row"
                        + (picked === row.code ? " active" : "")
                        + (level !== "none" ? ` level-${level}` : "")}
                      onClick={() => setPicked(picked === row.code ? "" : row.code)}
                      title={`${row.name}（${row.code}）· ${SCOPE_TEXT[row.kind]
                        ?? row.kind}\n强度 ${formatScore(intensity, 1)}`
                        + ` · ${row.level_label || row.level || "—"}`
                        + `\n收盘 ${row.close ?? "—"}`
                        + ` · 涨跌 ${formatRatioPct(row.change_pct)}`
                        + `\n5 日 ${formatRatioPct(row.ret_5d)}`
                        + ` · 10 日 ${formatRatioPct(row.ret_10d)}`
                        + ` · 20 日 ${formatRatioPct(row.ret_20d)}`
                        + `\n动量 z=${formatScore(row.z_5d, 2)}`
                        + ` · 持仓 5 日 ${formatRatioPct(row.oi_change_5d)}`
                        + `\n期限结构 ${formatScore(row.term_structure, 4)}`
                        + `${row.reasons.length > 0
                          ? `\n${row.reasons.join("\n")}` : ""}`}>
                <span className="mainline-gauge-name">{row.name || row.code}</span>
                <span className="mainline-gauge-track">
                  <i className="mainline-gauge-fill"
                     style={{ width: `${safe}%`,
                              background: intensityColor(intensity) }} />
                </span>
                <span className="mono mainline-gauge-value">
                  {formatScore(intensity, 1)}
                </span>
                <span className="mainline-gauge-kinds">
                  {row.kinds.map((kind) => (
                    <i key={kind} className="mainline-kind-chip"
                       title={FUTURES_KIND_TEXT[kind] ?? kind}>
                      {(FUTURES_KIND_TEXT[kind] ?? kind).slice(0, 2)}
                    </i>
                  ))}
                </span>
                <span className="mainline-gauge-boards muted-text">
                  {row.boards.length > 0 ? row.boards.join("/") : "—"}
                </span>
              </button>
            );
          })}
        </div>
      </section>

      {/* ② 期股联动热力图 */}
      <section className="mainline-card">
        <div className="mainline-card-head">
          <h4>期股联动热力图</h4>
          <span className="muted-text">
            行 = 期货品种，列 = 关联板块，颜色 = 历史相关系数
            （<span className="mainline-corr-pos">红 = 同向</span>、
            <span className="mainline-corr-neg">绿 = 反向</span>，越深越强；
            空白格 = 未计算，**不是 0**）
          </span>
        </div>
        <div className="mainline-alert-bar">
          <label className="chart-toggle">
            <input type="checkbox" checked={corrMin > 0}
                   onChange={(event) => setCorrMin(event.target.checked ? 0.3 : 0)} />
            只显示 |相关系数| ≥ 0.3
          </label>
          <span className="muted-text">
            共 {Object.keys(correlation).length} 个品种有系数，展示
            {" "}{matrixRows.length} 行 × {boardColumns.length} 列
          </span>
        </div>
        {matrixRows.length === 0 || boardColumns.length === 0 ? (
          <div className="mainline-empty">
            没有可用的期股相关系数（后端 `correlation` 为空）。
          </div>
        ) : (
          <div className="mainline-corr-scroll">
            {/* Fragment 带 key 平铺：网格容器要求每个直接子元素就是"一个格子"，
                所以不能包一层行 div（那会变成一个格子占满整行）。 */}
            <div className="mainline-corr-grid"
                 style={{ gridTemplateColumns:
                   `132px repeat(${boardColumns.length}, 56px)` }}>
              <span className="mainline-corr-corner">品种 \ 板块</span>
              {boardColumns.map((board) => (
                <span key={`h-${board}`} className="mainline-corr-head"
                      title={board}>
                  {board.length > 4 ? `${board.slice(0, 4)}…` : board}
                </span>
              ))}
              {matrixRows.map((row) => (
                <Fragment key={row.code}>
                  <button className={"mainline-corr-rowhead"
                            + (picked === row.code ? " active" : "")}
                          onClick={() => setPicked(
                            picked === row.code ? "" : row.code)}
                          title={`${row.name}（${row.code}）`}>
                    {row.name.length > 6 ? `${row.name.slice(0, 6)}…` : row.name}
                  </button>
                  {boardColumns.map((board) => {
                    const value = row.row[board];
                    const usable = value !== null && value !== undefined
                      && Number.isFinite(value);
                    const dim = corrMin > 0 && usable && Math.abs(value) < corrMin;
                    return (
                      <span key={`c-${row.code}-${board}`}
                            className={"mainline-corr-cell" + (dim ? " dim" : "")}
                            style={{ background: dim ? undefined : corrColor(value) }}
                            title={`${row.name} × ${board}`
                              + `\n相关系数 ${usable ? value.toFixed(3) : "未计算"}`}>
                        {usable ? value.toFixed(2) : ""}
                      </span>
                    );
                  })}
                </Fragment>
              ))}
            </div>
          </div>
        )}
      </section>

      {/* ③ 映射详情（点品种才展开） */}
      <section className="mainline-card">
        <div className="mainline-card-head">
          <h4>期股映射详情</h4>
          <span className="muted-text">
            {picked
              ? `当前品种 ${pickedSignal?.name ?? picked}（${pickedMappings.length} 条映射）`
              : "在上方仪表盘或矩阵里点一个品种，这里展开它的传导逻辑"}
          </span>
        </div>
        {pickedSignal && (
          <div className="mainline-futures-picked">
            <b>{pickedSignal.name}</b>
            <span className="mono muted-text">{pickedSignal.code}</span>
            <span>强度 <b className="mono">
              {formatScore(pickedSignal.intensity, 1)}</b></span>
            <span>{pickedSignal.level_label || pickedSignal.level || "—"}</span>
            <span className="muted-text">产业链 {pickedSignal.chain || "—"}</span>
            {pickedSignal.reasons.map((reason, index) => (
              <span key={index} className="muted-text">· {reason}</span>
            ))}
            {pickedSignal.gaps.length > 0 && (
              <span className="mainline-gaps">
                缺数据：{pickedSignal.gaps.join("；")}
              </span>
            )}
          </div>
        )}
        {picked && pickedMappings.length === 0 ? (
          <div className="mainline-empty">
            该品种没有配置期股映射（`mappings` 里没有它的记录）。
          </div>
        ) : pickedMappings.length > 0 ? (
          <div className="mainline-table-scroll">
            <table className="mainline-table">
              <thead>
                <tr>
                  <th>板块</th><th>方向</th><th className="num">强度</th>
                  <th className="num">领先天数</th><th>产业链</th>
                  <th className="num">校准强度</th><th>传导逻辑</th>
                </tr>
              </thead>
              <tbody>
                {pickedMappings.map((row) => (
                  <MappingRow key={`${row.future_code}-${row.board_code}`} row={row} />
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>

      {/* ④ 期货告警列表 */}
      <section className="mainline-card">
        <div className="mainline-card-head">
          <h4>期货告警</h4>
          <span className="muted-text">
            共 {alertCounts.all} 条 · 期货异动是板块主线的**先行**提示，
            要结合映射强度与领先天数决定是否提前关注
          </span>
        </div>
        <div className="mainline-alert-bar">
          <div className="mode-switch">
            {([
              { key: "all", label: `全部（${alertCounts.all}）` },
              { key: "strong", label: `🔴 强（${alertCounts.strong}）` },
              { key: "medium", label: `🟡 中（${alertCounts.medium}）` },
              { key: "weak", label: `🟢 弱（${alertCounts.weak}）` },
            ] as const).map((item) => (
              <button key={item.key}
                      className={alertLevel === item.key ? "mode-btn active" : "mode-btn"}
                      onClick={() => setAlertLevel(item.key)}>
                {item.label}
              </button>
            ))}
          </div>
        </div>
        {alertRows.length === 0 ? (
          <div className="mainline-empty">
            {alerts.length === 0 ? "今日没有期货告警。" : "当前筛选下没有告警。"}
          </div>
        ) : (
          <ul className="mainline-futures-alerts">
            {alertRows.map((row) => (
              <FuturesAlertRow key={row.alert_id} row={row}
                               onPick={() => setPicked(row.code)} />
            ))}
          </ul>
        )}
      </section>

      {(data?.source_notes.length ?? 0) > 0 && (
        <div className="mainline-foot muted-text">
          数据来源：{data?.source_notes.join(" · ")}
        </div>
      )}
      {(data?.gaps.length ?? 0) > 0 && (
        <div className="mainline-gaps">⚠️ {data?.gaps.join("；")}</div>
      )}
      {data?.disclaimer && (
        <div className="mainline-foot muted-text">{data.disclaimer}</div>
      )}
    </div>
  );
}

/** 映射表的一行（强度用 ★，比数字更快读懂"几星"）。 */
function MappingRow({ row }: { row: FuturesMapping }) {
  const stars = row.strength === null || row.strength === undefined
    ? "—"
    : "★".repeat(Math.max(0, Math.min(5, Math.round(row.strength))));
  return (
    <tr>
      <td>{row.board_name || row.board_code}</td>
      <td className={`mainline-dir dir-${row.direction || "auxiliary"}`}>
        {DIRECTION_TEXT[row.direction] ?? row.direction ?? "—"}
      </td>
      <td className="num mainline-stars"
          title={row.strength === null ? "未配置强度" : `${row.strength} 星`}>
        {stars}
      </td>
      <td className="num">{row.lead_days ?? "—"}</td>
      <td className="muted-text">{row.chain || "—"}</td>
      <td className="num muted-text">{formatScore(row.calibrated_strength, 1)}</td>
      <td className="mainline-logic">{row.logic || "—"}</td>
    </tr>
  );
}

/** 期货告警一行。 */
function FuturesAlertRow({ row, onPick }: { row: FuturesAlert; onPick: () => void }) {
  const level = levelClass(row.level);
  return (
    <li className={`mainline-futures-alert level-${level}`}>
      <span className={`mainline-alert-dot level-${level}`} />
      <span className="mono muted-text">{formatDate(row.date)}</span>
      <button className="mainline-link" onClick={onPick}
              title="点击后在矩阵中高亮该品种">
        {row.name || row.code}
      </button>
      <span className={`mainline-level level-${level}`}>
        {row.level_label || row.level || "—"}
      </span>
      <span className="mainline-futures-title">{row.title}</span>
      <span className="mainline-gauge-kinds">
        {row.kinds.map((kind) => (
          <i key={kind} className="mainline-kind-chip">
            {(FUTURES_KIND_TEXT[kind] ?? kind).slice(0, 2)}
          </i>
        ))}
      </span>
      <span className="muted-text">
        关联板块：{row.boards.length > 0 ? row.boards.join("/") : "—"}
      </span>
      <span className="muted-text mainline-logic" title={row.detail}>
        {row.detail}
      </span>
    </li>
  );
}
