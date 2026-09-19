import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, FlowBoard, FlowEntity, FlowWatchItem } from "../api";
import FlowChart from "./FundFlowChart";

/**
 * 资金流监控面板主体（板块 / 个股大资金动向）。
 *
 * 页签按钮与容器在 FundFlowPanel.tsx；本组件只负责
 * **当前页签**的数据获取与渲染 —— 这样切到板块拥挤度时不会顺带跑这边
 * 的 60 秒轮询。
 *
 * 两个榜单 + 一张叠加走势图：
 *   1. **板块**：近 N 日净额均值排序（净流入前 20 / 净流出前 20），
 *      默认加入当日盘中净额绝对值最大的 20 个热门板块；
 *   2. **个股**：`净额均值 / 流通市值` 排序前 20 —— 用比值而不是绝对额，
 *      否则榜单永远是大市值股票的天下；
 *   3. 勾选任意板块/个股即叠加它们的近 N 日走势线（滚轮缩放、拖拽平移）。
 *
 * 刷新节奏：盘中每 60 秒取一次（服务端同节奏重算，命中缓存不重复取数），
 * 另有「立即刷新」强制穿透缓存；页面顶部标明数据时间与所处时段。
 */
const POLL_MS = 60000;

function yi(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const amount = value / 1e8;
  return `${amount >= 0 ? "+" : ""}${amount.toFixed(digits)}`;
}

function tone(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "var(--muted)";
  return value >= 0 ? "var(--low)" : "var(--high)";
}

type Tab = "sector" | "stock";

/** 入榜类别 → 徽标样式（让"为什么这只票在榜上"一眼可见） */
const GROUP_CLASS: Record<string, string> = {
  "昨日涨停": "group-limit",
  "净流入前10": "group-in",
  "净流出前10": "group-out",
  "自选": "group-watch",
  "其他": "group-other",
};

export default function FundFlowBoard({ tab }: { tab: "sector" | "stock" }) {
  const [board, setBoard] = useState<FlowBoard | null>(null);
  const [loading, setLoading] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [windowDays, setWindowDays] = useState(10);
  const [direction, setDirection] = useState<"in" | "out">("in");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [updatedAt, setUpdatedAt] = useState("");
  const [query, setQuery] = useState("");
  const [searchKind, setSearchKind] = useState<Tab>("sector");
  const [results, setResults] = useState<Record<string, unknown>[]>([]);
  const [searching, setSearching] = useState(false);
  const [watch, setWatch] = useState<Record<Tab, FlowWatchItem[]>>(
    { sector: [], stock: [] });
  // 已加入监控的 code 集合：用于把搜索结果的按钮变成"已加入"，避免用户重复点击
  const [watched, setWatched] = useState<Set<string>>(new Set());
  // 正在请求中的条目（`kind:code`），用于按钮 loading 态 —— 点了没反应最容易被当成坏了
  const [busy, setBusy] = useState("");
  const timerRef = useRef<number | null>(null);

  const flash = useCallback((text: string) => {
    setNotice(text);
    window.setTimeout(() => setNotice((current) => (current === text ? null : current)),
      8000);
  }, []);

  const load = useCallback(async (refresh = false) => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.fundflowSnapshot(refresh, windowDays, 20);
      setBoard(data);
      setUpdatedAt(new Date().toLocaleTimeString("zh-CN"));
    } catch (exc) {
      setError(`资金流取数失败：${exc instanceof Error ? exc.message : String(exc)}`);
    } finally {
      setLoading(false);
    }
  }, [windowDays]);

  const loadWatch = useCallback(async () => {
    try {
      const [sectors, stocks] = await Promise.all([
        api.fundflowWatch("sector"), api.fundflowWatch("stock")]);
      setWatch({ sector: sectors.items, stock: stocks.items });
      return { sectors: sectors.items, stocks: stocks.items };
    } catch {
      /* 选择列表失败不影响榜单 */
      return null;
    }
  }, []);

  // 首次进入 + 每 60 秒取一次（服务端同节奏，命中缓存不会重复取数）
  useEffect(() => {
    void load();
    void loadWatch().then((data) => {
      if (!data) return;
      setWatched(
        new Set([...data.sectors.map((item) => item.code),
                 ...data.stocks.map((item) => item.code)]));
    });
    timerRef.current = window.setInterval(() => void load(), POLL_MS);
    return () => {
      if (timerRef.current !== null) window.clearInterval(timerRef.current);
    };
  }, [load, loadWatch]);

  // 默认把已加入监控的实体勾上（用户上次的选择就是他要看的）
  useEffect(() => {
    setSelected((current) => {
      const next = new Set(current);
      board?.sectors.forEach((entity) => {
        if (entity.source === "manual") next.add(entity.code);
      });
      board?.stocks.forEach((entity) => {
        if (entity.source === "manual") next.add(entity.code);
      });
      return next;
    });
  }, [board]);

  const rank = useMemo(() => {
    const rows = (tab === "sector" ? board?.sector_rank : board?.stock_rank) ?? [];
    const positive = rows.filter((row) => (row.net_avg ?? row.today_net ?? 0) > 0);
    const negative = rows.filter((row) => (row.net_avg ?? row.today_net ?? 0) <= 0);
    const picked = direction === "in" ? positive : [...negative].reverse();
    return picked.slice(0, 20);
  }, [board, tab, direction]);

  const chartEntities = useMemo(() => {
    const pool = [...(board?.sectors ?? []), ...(board?.stocks ?? [])];
    const byCode = new Map(pool.map((entity) => [entity.code, entity]));
    // 加入监控但这次榜单没带的（例如刚加入还没轮到取历史）也要能画
    [...watch.sector, ...watch.stock].forEach((item) => {
      if (!byCode.has(item.code)) {
        const found = [...(board?.sector_rank ?? []), ...(board?.stock_rank ?? [])]
          .find((entity) => entity.code === item.code);
        if (found) byCode.set(item.code, found);
      }
    });
    return [...selected]
      .map((code) => byCode.get(code))
      .filter((entity): entity is FlowEntity => Boolean(entity && entity.series.length));
  }, [board, selected, watch]);

  const toggle = (code: string) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(code)) next.delete(code); else next.add(code);
      return next;
    });
  };

  const addWatch = async (kind: Tab, code: string, name: string) => {
    setBusy(`${kind}:${code}`);
    setError(null);
    try {
      const data = await api.fundflowAddWatch(kind, code, name);
      // 加入成功**不弹提示**（与做T面板统一口径：用户明确要求去掉这类提示语）。
      // 反馈走非打扰路径：搜索结果那一行的按钮会变成「✓ 已加入」并禁用，
      // 下方「监控中」也会立刻多出一个 chip —— 两处都是原地反馈，不挡内容。
      setWatch((current) => ({ ...current, [kind]: data.items }));
      setWatched((current) => new Set(current).add(code));
      setSelected((current) => new Set(current).add(code));
      // 回读一次确认落库（写接口返回的列表就是权威状态，这里再对齐一遍搜索按钮状态）
      void loadWatch().then((data) => {
        if (!data) return;
        setWatched(
          new Set([...data.sectors.map((item) => item.code),
                   ...data.stocks.map((item) => item.code)]));
      });
    } catch (exc) {
      setError(`加入监控失败：${exc instanceof Error ? exc.message : String(exc)}`);
    } finally {
      setBusy("");
    }
  };

  const removeWatch = async (kind: Tab, code: string) => {
    setBusy(`${kind}:${code}`);
    try {
      const data = await api.fundflowRemoveWatch(kind, code);
      flash(data.notice);
      setWatch((current) => ({ ...current, [kind]: data.items }));
      setWatched((current) => {
        const next = new Set(current);
        next.delete(code);
        return next;
      });
      setSelected((current) => {
        const next = new Set(current);
        next.delete(code);
        return next;
      });
    } catch (exc) {
      setError(`移除失败：${exc instanceof Error ? exc.message : String(exc)}`);
    } finally {
      setBusy("");
    }
  };

  const runSearch = async (kind: Tab, keyword: string) => {
    setSearching(true);
    setError(null);
    try {
      const data = await api.fundflowSearch(kind, keyword, 20);
      setResults(data.items);
      if (!data.items.length) flash("没有匹配的板块/个股");
    } catch (exc) {
      setError(`搜索失败：${exc instanceof Error ? exc.message : String(exc)}`);
    } finally {
      setSearching(false);
    }
  };

  return (
    <div className="fundflow-root">
      <div className="fundflow-head">
        <label className="muted-text">
          窗口
          <select value={windowDays} className="mono"
                  onChange={(event) => setWindowDays(Number(event.target.value))}>
            {[5, 10, 15, 20].map((days) => (
              <option key={days} value={days}>近{days}日</option>
            ))}
          </select>
        </label>
        <button className="btn-ghost" disabled={loading}
                onClick={() => void load(false)}
                title="取最新快照（服务端 60 秒缓存内直接返回，秒级；不会强制穿透所有数据层）">
          {loading ? "取数中…" : "立即刷新"}
        </button>
        {/* 强制穿透：会重算每一层（Tushare 板块截面一次就要几十秒），
            只在"怀疑缓存坏了"时用，所以与普通刷新分开、并给出耗时提示 */}
        <button className="btn-ghost" disabled={loading}
                onClick={() => void load(true)}
                title="强制穿透所有缓存重算（板块截面一次要几十秒，非必要别用）">
          强制重算
        </button>
        {board && (
          <span className="muted-text">
            {board.session_label} · 数据 {updatedAt} · 生成 {board.generated_at.slice(11, 19)}
            {board.trade_date && ` · 交易日 ${board.trade_date}`}
          </span>
        )}
        {board?.source_notes.map((note, index) => (
          <span key={index} className="flow-source muted-text">{note}</span>
        ))}
      </div>

      {error && <div className="error-box">{error}</div>}
      {/* 提示同样改成**悬浮 toast**：面板内插入会顶下去所有内容（与做T面板同一处理） */}
      {notice && (
        <div className="notice-toast info" role="status" aria-live="polite">
          <span className="notice-text">{notice}</span>
          <button className="notice-close" onClick={() => setNotice(null)}
                  title="关闭提示" aria-label="关闭提示">×</button>
        </div>
      )}
      {board?.gaps.map((gap, index) => (
        <div key={index} className="warn-box">{gap}</div>
      ))}

      <div className="fundflow-body">
        <aside className="fundflow-side">
          <div className="flow-block">
            <h3>
              {tab === "sector" ? "板块资金流排行" : "个股资金流排行"}
              <span className="muted-text">
                {tab === "sector" ? "（近窗口净额均值）" : "（净额均值 ÷ 流通市值）"}
              </span>
            </h3>
            <div className="mode-switch">
              <button className={direction === "in" ? "mode-btn active" : "mode-btn"}
                      onClick={() => setDirection("in")}>净流入前 20</button>
              <button className={direction === "out" ? "mode-btn active" : "mode-btn"}
                      onClick={() => setDirection("out")}>净流出前 20</button>
            </div>
            <table className="audit-table compact-table flow-table">
              <thead>
                <tr>
                  <th>名称</th>
                  {tab === "stock" ? (
                    <>
                      <th className="num"
                          title="流通市值（腾讯盘中快照优先，缺失回落到本地仓库日频值）">
                        流通市值
                      </th>
                      <th className="num"
                          title="今日涨幅（腾讯盘中实时；取不到留空，不用旧值冒充）">
                        今日涨幅
                      </th>
                      <th title="涨停原因（东财涨停池的行业/题材归类，含连板数）；非涨停股为空">
                        涨停原因
                      </th>
                    </>
                  ) : (
                    <>
                      <th className="num" title="近 N 日净额均值（元 → 亿元）">窗口均值</th>
                      <th className="num" title="当日盘中净额（同花顺即时口径）">当日</th>
                    </>
                  )}
                  <th />
                </tr>
              </thead>
              <tbody>
                {rank.length === 0 && (
                  <tr>
                    <td colSpan={tab === "stock" ? 5 : 4} className="muted-text">
                      暂无排行数据（见上方缺口说明）
                    </td>
                  </tr>
                )}
                {rank.map((row) => {
                  const watched = watch[row.kind ?? tab]
                    .some((item) => item.code === row.code);
                  return (
                    <tr key={row.code}
                        className={selected.has(row.code) ? "row-active" : ""}>
                      <td className="factor-name">
                        <button className="flow-pick"
                                onClick={() => toggle(row.code)}
                                title="勾选/取消：叠加到下方走势图">
                          {row.name || row.code}
                        </button>
                        {tab === "stock" && row.rank_group && (
                          <span className={`flow-group ${GROUP_CLASS[row.rank_group] ?? "group-other"}`}
                                title={`本票入榜口径：${row.rank_group}`}>
                            {row.rank_group}
                          </span>
                        )}
                        <span className="mono muted-text"> {row.code}</span>
                      </td>
                      {tab === "stock" ? (
                        <>
                          <td className="num mono">
                            {row.circ_mv ? `${(row.circ_mv / 1e8).toFixed(1)}亿` : "—"}
                          </td>
                          <td className="num mono" style={{ color: tone(row.change_pct) }}>
                            {row.change_pct === null || row.change_pct === undefined
                              ? "—"
                              : `${row.change_pct >= 0 ? "+" : ""}${row.change_pct.toFixed(2)}%`}
                          </td>
                          <td className="muted-text">{row.limitup_reason || "—"}</td>
                        </>
                      ) : (
                        <>
                          <td className="num mono" style={{ color: tone(row.net_avg) }}>
                            {yi(row.net_avg)}
                          </td>
                          <td className="num mono" style={{ color: tone(row.today_net) }}>
                            {yi(row.today_net)}
                          </td>
                        </>
                      )}
                      <td className="num">
                        <button className="btn-ghost tiny"
                                onClick={() => watched
                                  ? void removeWatch(row.kind ?? tab, row.code)
                                  : void addWatch(row.kind ?? tab, row.code, row.name)}
                                title={watched ? "从监控列表移除" : "加入监控列表（下次打开仍在）"}>
                          {watched ? "−" : "＋"}
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          <div className="flow-block">
            <h3>加入监控（可搜索）</h3>
            <div className="flow-search">
              <select value={searchKind} className="mono"
                      onChange={(event) => setSearchKind(event.target.value as Tab)}>
                <option value="sector">板块</option>
                <option value="stock">个股</option>
              </select>
              <input value={query} placeholder={
                  searchKind === "sector" ? "板块名关键字，如 半导体" : "代码/名称/拼音"}
                     onChange={(event) => setQuery(event.target.value)}
                     onKeyDown={(event) => {
                       if (event.key === "Enter") void runSearch(searchKind, query);
                     }} />
              <button className="btn-ghost" disabled={searching}
                      onClick={() => void runSearch(searchKind, query)}>
                {searching ? "…" : "搜索"}
              </button>
            </div>
            {results.length > 0 && (
              <ul className="flow-results">
                {results.map((item) => {
                  const code = String(item.code ?? "");
                  const name = String(item.name ?? code);
                  const key = `${searchKind}:${code}`;
                  const already = watched.has(code);
                  return (
                    <li key={code}>
                      <span>{name}</span>
                      <span className="mono muted-text"> {code}</span>
                      <button className={already ? "btn-ghost tiny done" : "btn-ghost tiny"}
                              disabled={busy === key || already}
                              onClick={() => void addWatch(searchKind, code, name)}
                              title={already ? "已在监控列表中" : "加入监控列表（下次打开仍在）"}>
                        {busy === key ? "加入中…" : already ? "✓ 已加入" : "＋ 加入"}
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
            <div className="flow-watch">
              <span className="muted-text">监控中：</span>
              {([...watch.sector, ...watch.stock]).length === 0 && (
                <span className="muted-text">暂无（首次进入会自动加入当日热门板块前 20）</span>
              )}
              {watch.sector.map((item) => (
                <span key={`s-${item.code}`} className="flow-chip">
                  {item.name || item.code}
                  <button className="chip-x"
                          onClick={() => void removeWatch("sector", item.code)}>×</button>
                </span>
              ))}
              {watch.stock.map((item) => (
                <span key={`k-${item.code}`} className="flow-chip stock">
                  {item.name || item.code}
                  <button className="chip-x"
                          onClick={() => void removeWatch("stock", item.code)}>×</button>
                </span>
              ))}
            </div>
          </div>
        </aside>

        <section className="flow-block flow-chart-block">
          <h3>
            资金流走势（{chartEntities.length} 条）
            <span className="muted-text">
              单位亿元 · 零轴以上为净流入 · 点下方图例可隐藏单条线
            </span>
          </h3>
          <div className="flow-toggles">
            {chartEntities.map((entity) => (
              <button key={entity.code}
                      className={`flow-toggle${hidden.has(entity.code) ? " off" : ""}`}
                      onClick={() => setHidden((current) => {
                        const next = new Set(current);
                        if (next.has(entity.code)) next.delete(entity.code);
                        else next.add(entity.code);
                        return next;
                      })}
                      title={entity.data_source || entity.name}>
                {hidden.has(entity.code) ? "○" : "●"} {entity.name}
              </button>
            ))}
            {chartEntities.length > 0 && (
              <>
                <button className="btn-ghost tiny" onClick={() => setHidden(new Set())}>
                  全显
                </button>
                <button className="btn-ghost tiny"
                        onClick={() => setSelected(new Set())}>清空选择</button>
              </>
            )}
          </div>
          <FlowChart entities={chartEntities} hidden={hidden} />
          <p className="muted-text">
            ⚠️ 口径：板块走势来自东财板块资金流历史（不可用时退回「同花顺板块成分 ×
            Tushare 个股资金流求和」的本地聚合口径，两者的数值不可逐一对应）；
            个股走势来自 Tushare moneyflow（日频，收盘后定稿，单位元）。
            两种口径都以各实体的「数据来源」标注，可逐条查看。
          </p>
        </section>
      </div>
    </div>
  );
}
