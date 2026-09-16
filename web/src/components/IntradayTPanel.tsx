import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api, intradayWsUrl, IntradayBacktest, IntradaySnapshot, IntradayWatchItem,
  IntradayWatchRefresh, MarketCycle,
} from "../api";
import IntradayChart from "./IntradayChart";
import IntradayDailyPanel from "./IntradayDailyPanel";
import DataSourceHealth from "./DataSourceHealth";
import IntradayScore from "./IntradayScore";
import IntradaySentimentPanel from "./IntradaySentiment";
import IntradayValuation from "./IntradayValuation";
import MarketContextStrip from "./MarketContextStrip";
import { StockPicker } from "./StockPicker";
import WeightProfileEditor from "./WeightProfileEditor";

/**
 * 股票做T辅助（Intraday T-Assist）主面板。
 *
 * 四个核心模块的布局与需求一致：
 *   ① 估值空间（左上）—— 同业 PE/PB 分位数对比，判断「上涨空间」还是「估值透支」
 *   ② 分时图与提示点（中上）—— 个股与关联板块分时 + 做T三角标记 + 关键价位线
 *   ③ 多指标合成打分（下半部分，核心引擎）—— 七因子加权模型 + 阈值信号
 *   ④ 消息面与市场情绪（底部）—— 板块涨跌家数 + 相对强度 + 大盘 + NLP新闻情绪
 *
 * 数据刷新：当前标的默认通过 WebSocket 由服务端按15秒主动推送；连接不可用时自动
 * 退回轮询。**自选池**由服务端在盘中（09:15~11:30 / 13:00~15:00）每分钟重算一次，
 * 这里每分钟取一次缓存、并在 WS 推来新版时立即更新，无需手动刷新。
 */

const DEFAULT_CODE = "300308";
const POLL_FALLBACK_MS = 20000;
// 自选池兜底轮询：WS 正常时靠服务端推送（报价 5 秒一版），这里只在 WS 断开时兜住。
// 取的是服务端缓存（60 秒才重算一次打分），所以即使 15 秒轮询也不会放大取数成本。
const WATCH_POLL_MS = 15000;

const SIGNAL_TEXT: Record<string, string> = {
  low_buy: "低吸做T",
  high_sell: "高抛做T",
  stop_loss: "止损",
  none: "观望",
};

function SignalBadge({ item }: { item: IntradayWatchItem }) {
  const cls = item.signal_strength === "forced_exit" ? "sig-stop"
    : item.signal_kind === "low_buy" && item.signal_strength === "solid" ? "sig-buy-solid"
    : item.signal_kind === "high_sell" && item.signal_strength === "solid" ? "sig-sell-solid"
    : item.signal_strength === "hollow" ? "sig-hollow"
    : "sig-none";
  const text = item.signal_strength === "none"
    ? "观望"
    : `${SIGNAL_TEXT[item.signal_kind]}·${
        item.signal_strength === "solid" ? "实心"
        : item.signal_strength === "hollow" ? "空心" : "强制"}`;
  return <span className={`sig-badge ${cls}`}>{text}</span>;
}

export default function IntradayTPanel() {
  const [code, setCode] = useState(DEFAULT_CODE);
  const [inputCode, setInputCode] = useState(DEFAULT_CODE);
  const [snapshot, setSnapshot] = useState<IntradaySnapshot | null>(null);
  const [watch, setWatch] = useState<IntradayWatchItem[]>([]);
  const [watchRefresh, setWatchRefresh] = useState<IntradayWatchRefresh | null>(null);
  const [watchAt, setWatchAt] = useState<string>("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [wsState, setWsState] = useState<"connecting" | "live" | "fallback">("connecting");
  const [updatedAt, setUpdatedAt] = useState<string>("");
  const [showBacktest, setShowBacktest] = useState(false);
  const [backtest, setBacktest] = useState<IntradayBacktest | null>(null);
  const [backtestRunning, setBacktestRunning] = useState(false);
  const [horizon, setHorizon] = useState(6);
  const [watchBusy, setWatchBusy] = useState(false);
  // 海外映射输入（usNVDA,kr000660…）；留空则按板块默认映射回落
  const [overseasInput, setOverseasInput] = useState("");
  // 关联板块输入（PCB概念,PET铜箔…）；绑定后板块情绪/板块排行维度才会计入总分
  const [boardInput, setBoardInput] = useState("");
  // 模式：日内分时做T / 日K级别做T（量价体系）
  const [mode, setMode] = useState<"intraday" | "daily">("intraday");
  // 权重编辑弹窗（做T权重自定义 + 写入个股权重档案）
  const [weightOpen, setWeightOpen] = useState(false);
  // 这只票当前生效的口径来源（"全局口径" / "权重档案（…）"）
  const [overrideSource, setOverrideSource] = useState("");
  // 市场情绪周期（做T环境温度；加分项，失败不影响主面板）
  const [cycle, setCycle] = useState<MarketCycle | null>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const pollRef = useRef<number | null>(null);
  // 请求竞态守卫：快速连点「切换标的」时，只接受最后一次请求的结果，
  // 避免慢的旧响应覆盖新标的的快照。
  const requestSeq = useRef(0);

  /**
   * 取快照。**先渲染缓存、再后台刷新**（stale-while-revalidate）：
   *
   * 自选股切来切去时，之前的实现每次都 `setSnapshot(null)` + 等服务端整份快照，
   * 于是切换要卡 2~3 秒（页面先空白，再等 REST 与 WS 首帧）。现在每只票的快照
   * 都留在进程内缓存里：命中缓存的那一瞬间就把图铺出来，随后静默刷新覆盖 ——
   * 用户感知是"秒切"。
   *
   * 没有缓存的票（首次查看）才清空显示 loading，避免把上一只票的图当成本票的图。
   */
  const snapshotCache = useRef(new Map<string, IntradaySnapshot>());

  const load = useCallback(async (target: string, refresh = false) => {
    const seq = ++requestSeq.current;
    const cached = snapshotCache.current.get(target);
    if (cached && !refresh) {
      setSnapshot(cached);
      setLoading(false);
    } else if (!cached) {
      setSnapshot(null);
      setLoading(true);
    } else {
      setLoading(true);
    }
    setError(null);
    try {
      const data = await api.intradaySnapshot(target, refresh);
      snapshotCache.current.set(target, data);
      if (seq !== requestSeq.current) return; // 已被更新的请求取代
      setSnapshot(data);
      setUpdatedAt(new Date().toLocaleTimeString("zh-CN"));
    } catch (exc) {
      if (seq !== requestSeq.current) return;
      setError(`做T快照获取失败：${String(exc)}`);
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, []);

  const loadWatch = useCallback(async (force = false) => {
    try {
      const data = await api.intradayWatchlist(force);
      setWatch(data.items);
      setWatchRefresh(data.auto_refresh);
      setWatchAt(new Date().toLocaleTimeString("zh-CN"));
    } catch {
      /* 自选列表失败不影响主面板 */
    }
  }, []);

  // 自选池自动刷新：服务端**两条节奏** —— 现价/涨跌幅每 5 秒（报价快车道，
  // 批量 tick），总分/信号每 60 秒（整表重算）。这里兜底轮询取服务端缓存，
  // WS 正常时由服务端在新版生成时立即推送。以前只有挂载时取一次，所以自选股的
  // 价格/信号一直停在进页面那一刻 —— 必须手动刷新才动。
  useEffect(() => {
    void loadWatch();
    const timer = window.setInterval(() => void loadWatch(), WATCH_POLL_MS);
    return () => window.clearInterval(timer);
  }, [loadWatch]);

  // WebSocket 实时推送（断开自动重连；重连期间用轮询兜住，不掉数据）
  useEffect(() => {
    // 注意：这里**不再** setSnapshot(null)。切标的时保留上一份快照，由 load() 用
    // 缓存（或加载态）覆盖 —— 清空会让页面先白一下，正是"切换卡顿"的一半来源。
    void load(code);
    let disposed = false;
    let socket: WebSocket | null = null;
    let retryTimer: number | null = null;
    let attempts = 0;
    const stopPolling = () => {
      if (pollRef.current !== null) {
        window.clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
    const startPolling = () => {
      if (pollRef.current !== null) return;
      setWsState("fallback");
      pollRef.current = window.setInterval(() => void load(code), POLL_FALLBACK_MS);
    };
    // 重连退避 2s→4s→8s→16s→30s 封顶。没有重连时，服务重启/网络抖一下就会
    // 永久停在「轮询模式」（实测：服务重启后页面上只剩 GET 轮询，WS 再没回来）。
    const scheduleRetry = () => {
      if (disposed || retryTimer !== null) return;
      attempts += 1;
      const delay = Math.min(30000, 2000 * 2 ** Math.min(attempts - 1, 4));
      retryTimer = window.setTimeout(() => {
        retryTimer = null;
        connect();
      }, delay);
    };
    const connect = () => {
      if (disposed) return;
      try {
        socket = new WebSocket(intradayWsUrl(code));
      } catch {
        startPolling();
        scheduleRetry();
        return;
      }
      socketRef.current = socket;
      socket.onopen = () => {
        if (disposed) return;
        attempts = 0;
        setWsState("live");
        stopPolling(); // WS 通了就停掉兜底轮询，避免双份取数
      };
      socket.onmessage = (event) => {
        if (disposed) return;
        try {
          const payload = JSON.parse(event.data as string);
          if (payload.type === "snapshot" && payload.data) {
            const data = payload.data as IntradaySnapshot;
            snapshotCache.current.set(code, data);   // WS 推来的也进缓存，切回来即秒开
            setSnapshot(data);
            setUpdatedAt(new Date().toLocaleTimeString("zh-CN"));
            setWsState("live");
            stopPolling();
          } else if (payload.type === "watchlist" && payload.data) {
            // 服务端自动刷新循环写出新一版自选概览时推送（每分钟一次，版号变了才推）
            const data = payload.data as {
              items: IntradayWatchItem[]; auto_refresh: IntradayWatchRefresh };
            setWatch(data.items);
            if (data.auto_refresh) {
              setWatchRefresh(data.auto_refresh);
              setWatchAt(new Date().toLocaleTimeString("zh-CN"));
            }
          } else if (payload.type === "error") {
            setError(`实时推送异常：${payload.detail}`);
          }
        } catch {
          /* 忽略非法帧 */
        }
      };
      // onerror 后浏览器必然再触发 onclose，重连只在 onclose 里做，避免双份定时器
      socket.onclose = () => {
        if (disposed) return;
        startPolling();
        scheduleRetry();
      };
    };
    connect();
    return () => {
      disposed = true;
      if (retryTimer !== null) window.clearTimeout(retryTimer);
      stopPolling();
      socket?.close();
      socketRef.current = null;
    };
  }, [code, load]);

  /**
   * 这只票当前生效的口径来源（权重档案 / YAML overrides / 全局）。
   *
   * 为什么单独取一次而不是从快照里读：快照里没有这个字段，而分数表只看得到
   * 权重数字 —— 不写清「这张表不是全局口径」，用户会拿它去跟别的票对照，
   * 然后得出完全错误的结论。
   */
  const loadOverrideSource = useCallback(async (target: string) => {
    try {
      const data = await api.intradayWeightProfile(target);
      const source = data.effective.source ?? "";
      // 后端把「没有任何个股覆盖」的口径写成以「全局口径」开头的说明；
      // 那种情况不必在标题下多写一行，只有真的按档案跑时才提醒
      setOverrideSource(source.startsWith("全局口径") ? "" : source);
    } catch {
      setOverrideSource("");  // 取不到就当全局口径，不阻塞主面板
    }
  }, []);

  useEffect(() => { void loadOverrideSource(code); }, [code, loadOverrideSource]);

  /**
   * 市场情绪周期（做T环境温度）。与打分里的「市场情绪周期」因子同源，
   * 这里只是把它已经算出来的结论摆在工具栏上：退潮期/冰点期禁止低吸做T
   * 这件事，用户应当在看信号之前就知道。
   */
  useEffect(() => {
    if (mode !== "intraday") return;
    let alive = true;
    api.intradayMarketCycle()
      .then((data) => { if (alive) setCycle(data); })
      .catch(() => { if (alive) setCycle(null); });
    return () => { alive = false; };
  }, [mode]);

  const submit = () => {
    const value = inputCode.trim();
    if (!/^\d{6}$/.test(value)) {
      setError("请输入6位证券代码（如 300308）");
      return;
    }
    setError(null);
    // 注意：这里**不依赖 loading 状态禁用按钮**。旧实现里按钮在请求期间 disabled，
    // 而首个快照最慢曾达 54 秒 → 用户点不动、以为坏了。现在按钮始终可点，
    // 请求竞态由 requestSeq 守卫，加载状态只做提示。
    setCode(value);
    setShowBacktest(false);
    setBacktest(null);
  };

  /** 加入自选（写回 configs/intraday.yaml，保留文件注释）。 */
  const addToWatch = async (target: string) => {
    if (!/^\d{6}$/.test(target)) {
      setError("请先在上方输入6位证券代码，再点「加自选」");
      return;
    }
    setWatchBusy(true);
    setError(null);
    setNotice(null);
    try {
      const current = snapshot?.code === target ? snapshot : null;
      // 关联板块：优先用输入框；留空则回落到面板里已绑定的板块（参考板块不写入，
      // 否则会把「随便看看的板块」固化成这只票的关联板块）。
      const typedBoards = boardInput
        .split(/[,，、\s]+/)
        .map((s) => s.trim())
        .filter(Boolean);
      const boundBoards = current?.sentiment?.boards_bound
        ? (current?.sentiment?.boards ?? []).map((b) => b.name)
        : [];
      const boards = typedBoards.length ? typedBoards
        : current?.sentiment?.board_name && current?.sentiment?.boards_bound
          ? [current.sentiment.board_name] : boundBoards;
      // 海外映射：输入框留空则交给后端按板块默认映射回落
      const overseas = overseasInput
        .split(/[,，\s]+/)
        .map((s) => s.trim())
        .filter(Boolean);
      const result = await api.intradayAddWatch({
        code: target,
        name: current?.name ?? "",
        boards,
        overseas,
      });
      setNotice(
        `已加入自选：${target} ${result.name || ""}` +
        (result.boards?.length
          ? `（关联板块 ${result.boards.join("/")}）` : "") +
        (result.overseas?.length
          ? `（海外映射 ${result.overseas.join("/")}）` : "") +
        `，共 ${result.watchlist.length} 只，已写入 configs/intraday.yaml`);
      setOverseasInput("");
      setBoardInput("");
      await loadWatch();
    } catch (exc) {
      setError(`加自选失败：${String(exc)}`);
    } finally {
      setWatchBusy(false);
    }
  };

  /** 从自选移除。 */
  const removeFromWatch = async (target: string) => {
    setWatchBusy(true);
    setError(null);
    setNotice(null);
    try {
      const result = await api.intradayRemoveWatch(target);
      setNotice(`已移除自选：${target}（剩余 ${result.watchlist.length} 只）`);
      await loadWatch();
    } catch (exc) {
      setError(`移除自选失败：${String(exc)}`);
    } finally {
      setWatchBusy(false);
    }
  };

  const inWatchlist = watch.some((item) => item.code === inputCode);

  const runBacktest = async () => {
    setBacktestRunning(true);
    setBacktest(null);
    try {
      setBacktest(await api.intradayBacktest({ code, horizon, days: 20 }));
    } catch (exc) {
      setError(`阈值回测失败：${String(exc)}`);
    } finally {
      setBacktestRunning(false);
    }
  };

  const quote = snapshot?.quote ?? null;
  const changeTone = (quote?.change_pct ?? 0) >= 0 ? "var(--low)" : "var(--high)";
  const generated = useMemo(() => snapshot?.generated_at?.slice(11, 19) ?? "", [snapshot]);
  // 分时数据**自身**的交易日：后端 `health.stale` 是按分钟K线的日期算的，而分时
  // 可能来自另一个数据源。按分时自己的 ts 判断，才不会张冠李戴地说
  // 「你看到的完整分时是昨天的」。
  const trendDate = useMemo(() => {
    let latest = "";
    for (const point of snapshot?.trend ?? []) {
      const date = String(point.ts ?? "").slice(0, 10);
      if (date > latest) latest = date;
    }
    return latest;
  }, [snapshot]);
  const now = new Date();
  const todayText = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`
    + `-${String(now.getDate()).padStart(2, "0")}`;
  const trendDateOff = !!trendDate && trendDate !== todayText;
  // 盘中/竞价/午休却拿着非当日分时 = 数据链出问题了。
  // 2026-09-16 实测：QMT 本地库没有当日分钟bar，未触发增量补下载时只回上一交易日，
  // 界面就把昨天 09:30~15:00 一整天原样画成了「今日分时」（用户报告的现象）。
  const sessionLive = ["call_auction", "trading", "lunch_break"].includes(
    snapshot?.session_state ?? "");
  // 传给图表的数组必须**引用稳定**：`snapshot?.trend ?? []` 每次渲染都会造一个新数组，
  // 那样即使图表做了 React.memo 也会被判定为"props 变了"而重渲染。
  // 报价快车道每 5 秒更新一次自选列表，若带着整张分时图一起重渲染，
  // 页面就会一直卡顿（用户报的"自动刷新异常慢"）。
  const trendPoints = useMemo(() => snapshot?.trend ?? [], [snapshot]);
  const signalMarkers = useMemo(() => snapshot?.markers ?? [], [snapshot]);
  const boardList = useMemo(() => snapshot?.sentiment?.boards ?? [], [snapshot]);
  const boardSeriesList = useMemo(
    () => snapshot?.sentiment?.board_series ?? [], [snapshot]);
  const levelSeries = useMemo(() => snapshot?.level_series ?? [], [snapshot]);

  /**
   * 「为什么没信号」——用户最常问的一类问题：
   *   价格明明摸到（甚至跌破）那条线，图上却没有三角。
   * 触发需要**价格触及档位**与**总分达标**两条同时满足，只显示当前总分回答不了
   * "触点那一刻是多少分"。这里用逐bar总分序列把答案直接写出来。
   */
  const signalDiag = useMemo(() => {
    const points = snapshot?.score_series ?? [];
    const levels = snapshot?.levels;
    if (!points.length || !levels) return null;
    const band = 0.003;   // 与后端 touch_band_pct 同口径（贴线即算触及）
    const touchLow = points.filter(
      (p) => p.price !== null && p.price <= levels.low_buy * (1 + band));
    const touchHigh = points.filter(
      (p) => p.price !== null && p.price >= levels.high_sell * (1 - band));
    const hint = snapshot?.scorecard?.threshold_hint ?? 20;
    const action = snapshot?.scorecard?.threshold_action ?? 30;
    const fired = (snapshot?.markers ?? []).length;
    const bestLow = touchLow.length
      ? Math.max(...touchLow.map((p) => p.total)) : null;
    const bestHigh = touchHigh.length
      ? Math.min(...touchHigh.map((p) => p.total)) : null;
    const blocked = (touchLow.length && (bestLow ?? 0) < hint)
      || (touchHigh.length && (bestHigh ?? 0) > -hint);
    const parts: string[] = [];
    if (fired > 0) parts.push(`今日已触发 ${fired} 个信号（图中三角）`);
    if (touchLow.length) {
      parts.push(`触及低吸线 ${touchLow.length} 次，触点处最高总分 `
        + `${bestLow !== null && bestLow >= 0 ? "+" : ""}${bestLow?.toFixed(1)}`);
    }
    if (touchHigh.length) {
      parts.push(`触及高抛线 ${touchHigh.length} 次，触点处最低总分 `
        + `${bestHigh !== null && bestHigh >= 0 ? "+" : ""}${bestHigh?.toFixed(1)}`);
    }
    if (!parts.length) {
      parts.push("今日价格未触及任何档位");
    }
    const current = snapshot?.scorecard?.total;
    parts.push(`当前总分 ${current !== undefined && current !== null
      ? `${current >= 0 ? "+" : ""}${current.toFixed(1)}` : "—"}`);
    parts.push(`触发需「价格触及档位」且「总分达标」同时满足`
      + `（提示线 ±${hint} / 动手线 ±${action}）`);
    if (blocked && fired === 0) {
      parts.push("→ 今日只有价格条件成立、总分未达标，故不出信号");
    }
    // 说明粒度与口径，避免用户拿 1 分钟分时点数去对"触及次数"
    parts.push("（触及按 5 分钟bar的**盘中极值**判定，与三角标记同一口径）");
    parts.push("注：档位线在图上是一条**随行情漂移的曲线**（每分钟随VWAP/布林重算），"
      + "右端标签才是当前值 —— 别拿当前值去回看早盘");
    return { level: blocked && fired === 0 ? "warn" : "info", text: parts.join("；") };
  }, [snapshot]);

  return (
    <div className="intraday-root">
      <div className="intraday-toolbar">
        <span className="intraday-title">股票做T辅助</span>
        <div className="mode-switch">
          <button className={mode === "intraday" ? "mode-btn active" : "mode-btn"}
                  onClick={() => setMode("intraday")}
                  title="日内分时做T：分钟级多因子打分 + ±20/±30 阈值信号">
            日内分时
          </button>
          <button className={mode === "daily" ? "mode-btn active" : "mode-btn"}
                  onClick={() => setMode("daily")}
                  title="日K级别做T：量柱体系 + 高量柱攻防 + B1-B15/S1-S6 战法">
            日K做T
          </button>
        </div>
        <StockPicker
          value={inputCode}
          onChange={(next) => setInputCode(next.replace(/[^\d]/g, ""))}
          onPick={(entry) => {
            // 点候选即切换标的：这是最常用的动作，不该再要求点一次「切换标的」
            setInputCode(entry.code);
            setError(null);
            setCode(entry.code);
            setShowBacktest(false);
            setBacktest(null);
          }}
          placeholder="代码 / 拼音首字母 / 中文名"
          width={250}
        />
        <button onClick={submit} title="切换到该标的（也可直接回车）">
          切换标的
        </button>
        {inWatchlist ? (
          <button className="btn-ghost" disabled={watchBusy}
                  onClick={() => void removeFromWatch(inputCode)}
                  title="从自选池移除并写回配置文件">
            {watchBusy ? "处理中…" : "− 移出自选"}
          </button>
        ) : (
          <button className="btn-ghost" disabled={watchBusy}
                  onClick={() => void addToWatch(inputCode)}
                  title="加入自选池并写回 configs/intraday.yaml">
            {watchBusy ? "处理中…" : "＋ 加自选"}
          </button>
        )}
        <input className="board-input" value={boardInput}
               onChange={(e) => setBoardInput(e.target.value)}
               placeholder="关联板块(可选) PCB概念,PET铜箔"
               title="加自选时一并写入关联板块：板块情绪与板块涨幅排行维度要靠它绑定；不填则该两维度不计入总分" />
        <input className="overseas-input" value={overseasInput}
               onChange={(e) => setOverseasInput(e.target.value)}
               placeholder="海外映射(可选) usNVDA,kr000660"
               title="加自选时一并写入海外映射；留空则回落到所属板块的默认映射" />
        <button className="btn-ghost" onClick={() => void load(code, true)}
                title="忽略缓存重新取数">
          强制刷新
        </button>
        <button className="btn-ghost" onClick={() => setShowBacktest((v) => !v)}>
          {showBacktest ? "隐藏阈值回测" : "阈值回测（先验证再做T）"}
        </button>
        <button className="btn-ghost" onClick={() => setWeightOpen(true)}
                title="自定义这只票的做T权重/阈值/档位（合计必须=100，保存后写入权重档案并立即生效）">
          权重编辑
        </button>
        <span className={`ws-pill ws-${wsState}`}
              title={wsState === "live"
                ? "服务端每15秒主动推送一次快照（分时线为1分钟粒度）"
                : wsState === "fallback"
                  ? "实时推送断开：已切到20秒轮询兜底，并在后台自动重连"
                  : "正在建立实时推送连接…"}>
          {wsState === "live" ? "实时推送" : wsState === "fallback" ? "轮询模式" : "连接中…"}
        </span>
        {loading && <span className="loading-pill"><span className="spinner" />取数中…</span>}
        {updatedAt && <span className="muted-text">更新 {updatedAt}（快照 {generated}）</span>}
        {snapshot && (
          <span className="muted-text">
            {snapshot.trade_date} · {snapshot.session_label}
            {snapshot.health.stale && " · 非当日"}
          </span>
        )}
      </div>

      {error && <div className="error-box">{error}</div>}
      {notice && <div className="info-box">{notice}</div>}

      {snapshot && (
        <div className="intraday-headline">
          <span className="hl-name">{snapshot.name || snapshot.code}</span>
          <span className="mono">{snapshot.code}</span>
          <span className="hl-price mono" style={{ color: changeTone }}>
            {quote?.price?.toFixed(2) ?? "—"}
          </span>
          <span className="mono" style={{ color: changeTone }}>
            {quote?.change !== null && quote?.change !== undefined
              ? `${quote.change >= 0 ? "+" : ""}${quote.change.toFixed(2)}` : "—"}
            {" "}
            {quote?.change_pct !== null && quote?.change_pct !== undefined
              ? `(${quote.change_pct >= 0 ? "+" : ""}${quote.change_pct.toFixed(2)}%)` : ""}
          </span>
          {snapshot.levels && (
            <span className="hl-levels mono muted-text">
              低吸 {snapshot.levels.low_buy.toFixed(2)} ·
              高抛 {snapshot.levels.high_sell.toFixed(2)} ·
              止损 {snapshot.levels.stop_loss.toFixed(2)}
            </span>
          )}
          {quote?.turnover_rate !== null && quote?.turnover_rate !== undefined && (
            <span className="muted-text">换手 {quote.turnover_rate.toFixed(2)}%</span>
          )}
        </div>
      )}

      <div className="intraday-watch">
        <span className="muted-text">自选（{watch.length}）：</span>
        {/* 自选池刷新状态：盘中由**服务端**每分钟重算全部自选，前端每分钟取缓存 +
            WS 推送新版。以前只有进页面时取一次，所以价格/信号一直不动、必须手动刷新。 */}
        <span className={`watch-refresh ${
          watchRefresh?.window_open && watchRefresh.running ? "live" : ""}`}
              title={watchRefresh
                ? (watchRefresh.last_error || watchRefresh.quote_last_error
                    ? `上次刷新失败：${watchRefresh.last_error
                        || watchRefresh.quote_last_error}`
                    : `现价 ${watchRefresh.quote_last_run_at || "—"}`
                      + `（${watchRefresh.quote_last_seconds}s / `
                      + `${watchRefresh.quote_last_count}只，覆盖`
                      + `${watchRefresh.quote_covered}只）`
                      + `；打分重算 ${watchRefresh.last_run_at || "—"}`
                      + `（${watchRefresh.last_seconds}s / ${watchRefresh.last_count}只）`)
                : "自动刷新状态未知"}>
          {watchRefresh
            ? (watchRefresh.enabled && watchRefresh.window_open && watchRefresh.running
                // 报价与打分是两条节奏，必须分开写：否则用户会把 60 秒前的信号
                // 当成此刻的信号
                ? `⟳ 报价 ${watchRefresh.quote_interval_seconds}s`
                  + ` · 打分 ${watchRefresh.interval_seconds}s`
                  + `${watchRefresh.quote_last_run_at
                      ? ` · ${watchRefresh.quote_last_run_at}` : ""}`
                : `⏸ ${watchRefresh.window_reason}${watchAt ? ` · 数据 ${watchAt}` : ""}`)
            : ""}
          {watchRefresh?.last_error || watchRefresh?.quote_last_error
            ? " ⚠刷新异常" : ""}
        </span>
        <button className="btn-ghost" disabled={watchBusy}
                onClick={() => void loadWatch(true)}
                title="忽略缓存立即重算全部自选（盘中本来每分钟自动刷新，这里用于盘中临时催一次）">
          ↻ 立即刷新
        </button>
        {watch.length === 0 && (
          <span className="muted-text">
            暂无自选 —— 在上方输入6位代码后点「＋ 加自选」即可
          </span>
        )}
        {watch.map((item) => (
          <div key={item.code}
               className={`watch-chip ${item.code === code ? "active" : ""}`}>
            <button className="chip-main"
                    title={`切换到 ${item.name || item.code}`}
                    onClick={() => { setInputCode(item.code); setCode(item.code); }}>
              <b>{item.name || item.code}</b>
              <span className="mono" style={{
                color: (item.change_pct ?? 0) >= 0 ? "var(--low)" : "var(--high)",
              }}>
                {item.change_pct === null || item.change_pct === undefined
                  ? "—" : `${item.change_pct >= 0 ? "+" : ""}${item.change_pct.toFixed(2)}%`}
              </span>
              {item.total_score !== null && item.total_score !== undefined && (
                <span className="mono muted-text">
                  {item.total_score >= 0 ? "+" : ""}{item.total_score.toFixed(0)}
                </span>
              )}
              <SignalBadge item={item} />
            </button>
            <button className="chip-remove" disabled={watchBusy}
                    title={`从自选移除 ${item.code}`}
                    onClick={() => void removeFromWatch(item.code)}>×</button>
          </div>
        ))}
      </div>

      {mode === "daily" ? (
        <IntradayDailyPanel code={code} />
      ) : (
        <>
          {cycle && (
            <div className={cycle.available && !cycle.t_allowed
              ? "warn-box cycle-strip" : "info-box cycle-strip"}>
              {cycle.available ? (
                <>
                  <b>市场情绪周期：{cycle.stage}</b>
                  <span className="mono">
                    做T环境温度 {cycle.temperature}/100
                  </span>
                  <span>
                    {cycle.t_allowed
                      ? "允许低吸做T"
                      : "禁止低吸做T（退潮/冰点期：低吸胜率极低）"}
                  </span>
                  <span className="muted-text">
                    涨停 {cycle.limit_up_count} / 跌停 {cycle.limit_down_count}
                    {" / 炸板 "}{cycle.broken_count}
                    {cycle.broken_rate !== null
                      && `（炸板率 ${(cycle.broken_rate * 100).toFixed(0)}%）`}
                    {" / 最高连板 "}{cycle.max_streak} 板
                    {cycle.trade_date && ` · ${cycle.trade_date}`}
                  </span>
                </>
              ) : (
                <span>市场情绪周期不可用：{cycle.gap ?? "数据缺口"}</span>
              )}
              {cycle.gates.length > 0 && (
                <span className="gate-text">{cycle.gates.join("；")}</span>
              )}
            </div>
          )}

          <MarketContextStrip snapshot={snapshot} />

          <div className="intraday-top">
            <IntradayValuation data={snapshot?.valuation ?? null} code={code} />
            <section className="panel intraday-panel chart-panel">
              <div className="panel-head">
                <h2>② 分时图与提示点</h2>
                <span className="muted-text">
                  三角=做T信号（实心正式/空心软提示），虚线=高抛·低吸·止损·箱体
                </span>
              </div>
              {trendDateOff && sessionLive && (
                <div className="warn-box">
                  <b>盘中却拿到非当日分时：</b>图中画的是 <b>{trendDate}</b> 的完整曲线，
                  不是今天的走势。原因是本地行情源未更新到当日（QMT 本地分钟库需要
                  当日增量补下载，正常情况下会自动补；腾讯源应自动兜底）。
                  请点「强制刷新」重试；若仍是此状态，请看下方「数据源健康」面板。
                </div>
              )}
              {trendDateOff && !sessionLive && (
                <div className="info-box">
                  当前展示 {trendDate} 的完整分时（{snapshot?.session_label}，非当日
                  {snapshot?.trade_date === trendDate ? "" : `；分钟K线最新 ${snapshot?.trade_date}`}）。
                </div>
              )}
              <IntradayChart
                quote={quote}
                trend={trendPoints}
                levels={snapshot?.levels ?? null}
                markers={signalMarkers}
                boards={boardList}
                boardSeries={boardSeriesList}
                levelSeries={levelSeries}
              />
              {signalDiag && (
                <div className={signalDiag.level === "info" ? "info-box" : "warn-box"}>
                  {signalDiag.text}
                </div>
              )}
            </section>
          </div>

          <IntradayScore scorecard={snapshot?.scorecard ?? null}
                         signal={snapshot?.signal ?? null}
                         onEditWeights={() => setWeightOpen(true)}
                         overrideSource={overrideSource} />

          <IntradaySentimentPanel sentiment={snapshot?.sentiment ?? null}
                                  news={snapshot?.news ?? null} />
        </>
      )}

      {showBacktest && (
        <section className="panel intraday-panel">
          <div className="panel-head">
            <h2>阈值回测 · 验证「动手线±30 / 提示线±20」</h2>
            <label className="eps-label">
              前瞻bar数
              <input type="number" min={1} max={48} value={horizon}
                     onChange={(e) => setHorizon(Number(e.target.value))}
                     style={{ width: 70, marginLeft: 6 }} />
            </label>
            <button className="btn-ghost" onClick={runBacktest}
                    disabled={backtestRunning}>
              {backtestRunning ? "回测中…" : "运行回测"}
            </button>
          </div>
          {!backtest && !backtestRunning && (
            <div className="muted-text">
              在历史5分钟序列上复现同一套打分（全因果指标、无未来函数），
              统计阈值信号的命中率与平均前瞻收益，并与全样基准对比。
            </div>
          )}
          {backtest && (
            <>
              {!backtest.available ? (
                <div className="warn-box">
                  回测不可用：{backtest.gaps.join("；")}
                </div>
              ) : (
                <>
                  <div className="summary-row">
                    <div className="stat-card">
                      <span className="stat-label">
                        样本（{backtest.range_start} ~ {backtest.range_end}）
                      </span>
                      <span className="stat-value">
                        {backtest.days}日 / {backtest.bars}根
                      </span>
                    </div>
                    <div className="stat-card">
                      <span className="stat-label">动手线 ±{backtest.action_line?.threshold ?? "—"} 信号</span>
                      <span className="stat-value">
                        {backtest.action_line?.signals ?? 0}
                        <span className="cache-badge">
                          命中 {backtest.action_line?.hit_rate === null
                            || backtest.action_line?.hit_rate === undefined
                            ? "—" : `${(backtest.action_line.hit_rate * 100).toFixed(0)}%`}
                        </span>
                      </span>
                    </div>
                    <div className="stat-card">
                      <span className="stat-label">平均前瞻收益（{backtest.horizon_bars}根bar）</span>
                      <span className="stat-value" style={{
                        color: (backtest.action_line?.excess_vs_baseline_pct ?? 0) > 0
                          ? "var(--high)" : "var(--low)",
                      }}>
                        {backtest.action_line?.avg_forward_return_pct === null
                          || backtest.action_line?.avg_forward_return_pct === undefined
                          ? "—"
                          : `${backtest.action_line.avg_forward_return_pct.toFixed(2)}%`}
                      </span>
                    </div>
                    <div className="stat-card">
                      <span className="stat-label">全样基准（随机时点做T）</span>
                      <span className="stat-value">
                        {backtest.baseline?.avg_forward_return_pct === null
                          || backtest.baseline?.avg_forward_return_pct === undefined
                          ? "—"
                          : `${backtest.baseline.avg_forward_return_pct.toFixed(2)}%`}
                      </span>
                    </div>
                    <div className="stat-card">
                      <span className="stat-label">机械做T（含成本）</span>
                      <span className="stat-value" style={{
                        color: (backtest.total_return_pct ?? 0) >= 0
                          ? "var(--high)" : "var(--low)",
                      }}>
                        {backtest.total_return_pct === null
                          ? "—" : `${backtest.total_return_pct.toFixed(2)}%`}
                        {backtest.win_rate !== null && (
                          <span className="cache-badge">
                            胜率 {(backtest.win_rate * 100).toFixed(0)}%
                            {backtest.max_drawdown_pct !== null
                              && ` · 回撤 ${backtest.max_drawdown_pct.toFixed(2)}%`}
                          </span>
                        )}
                      </span>
                    </div>
                  </div>

                  <p className="score-verdict">{backtest.verdict}</p>

                  <table className="audit-table">
                    <thead>
                      <tr>
                        <th>阈值</th><th className="num">信号数</th>
                        <th className="num">命中率</th>
                        <th className="num">平均前瞻</th>
                        <th className="num">中位前瞻</th>
                        <th className="num">超额(vs基准)</th>
                      </tr>
                    </thead>
                    <tbody>
                      {backtest.by_threshold
                        .filter((stat) => stat.signals > 0)
                        .map((stat, index) => (
                          <tr key={`${stat.direction}-${stat.threshold}-${index}`}>
                            <td className="mono">
                              {stat.direction === "short" ? "高抛" : "低吸"}±{stat.threshold}
                            </td>
                            <td className="num mono">{stat.signals}</td>
                            <td className="num mono">
                              {stat.hit_rate === null ? "—"
                                : `${(stat.hit_rate * 100).toFixed(1)}%`}
                            </td>
                            <td className="num mono" style={{
                              color: (stat.avg_forward_return_pct ?? 0) >= 0
                                ? "var(--high)" : "var(--low)",
                            }}>
                              {stat.avg_forward_return_pct === null ? "—"
                                : `${stat.avg_forward_return_pct.toFixed(3)}%`}
                            </td>
                            <td className="num mono">
                              {stat.median_forward_return_pct === null ? "—"
                                : `${stat.median_forward_return_pct.toFixed(3)}%`}
                            </td>
                            <td className="num mono" style={{
                              color: (stat.excess_vs_baseline_pct ?? 0) > 0
                                ? "var(--high)" : "var(--muted)",
                            }}>
                              {stat.excess_vs_baseline_pct === null ? "—"
                                : `${stat.excess_vs_baseline_pct >= 0 ? "+" : ""}${stat.excess_vs_baseline_pct.toFixed(3)}%`}
                            </td>
                          </tr>
                        ))}
                    </tbody>
                  </table>

                  {backtest.trades.length > 0 && (
                    <details className="score-detail">
                      <summary>机械做T成交明细（{backtest.trades.length}笔）</summary>
                      <table className="audit-table">
                        <thead>
                          <tr>
                            <th>买入</th><th>卖出</th>
                            <th className="num">买价</th><th className="num">卖价</th>
                            <th className="num">收益</th><th>退出原因</th>
                            <th className="num">入场总分</th>
                          </tr>
                        </thead>
                        <tbody>
                          {backtest.trades.slice(0, 40).map((trade, index) => (
                            <tr key={index}>
                              <td className="mono">{trade.entry_ts.slice(5)}</td>
                              <td className="mono">{trade.exit_ts.slice(5)}</td>
                              <td className="num mono">{trade.entry_price}</td>
                              <td className="num mono">{trade.exit_price}</td>
                              <td className="num mono" style={{
                                color: trade.return_pct >= 0 ? "var(--high)" : "var(--low)",
                              }}>
                                {trade.return_pct >= 0 ? "+" : ""}{trade.return_pct.toFixed(2)}%
                              </td>
                              <td>{trade.exit_reason === "high_sell" ? "高抛"
                                : trade.exit_reason === "stop_loss" ? "止损"
                                : trade.exit_reason === "sample_end" ? "样本末平仓"
                                : "收盘平仓"}</td>
                              <td className="num mono">{trade.total_score_at_entry.toFixed(1)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </details>
                  )}
                </>
              )}
              <div className="warn-box" style={{ marginTop: 12 }}>
                {backtest.disclaimer}
              </div>
              {backtest.gaps.length > 0 && (
                <div className="muted-text gap-box">
                  {backtest.gaps.map((gap, index) => <div key={index}>· {gap}</div>)}
                </div>
              )}
            </>
          )}
        </section>
      )}

      {snapshot && (
        <section className="panel intraday-panel health-panel">
          <h3>数据健康度（绝不使用模拟数据，失败即列缺口）</h3>
          <div className="health-row muted-text mono">
            <span>分钟K线：{snapshot.health.chosen_intraday_source ?? "缺口"}</span>
            <span>日线：{snapshot.health.chosen_daily_source ?? "缺口"}</span>
            <span>快照：{snapshot.health.chosen_quote_source ?? "缺口"}</span>
          </div>
          <div className="health-attempts">
            {snapshot.health.attempts.map((attempt, index) => (
              <span key={index} className={`attempt ${attempt.ok ? "ok" : "fail"}`}
                    title={attempt.detail}>
                {attempt.source} {attempt.ok ? `✓${attempt.rows}` : "✗"}
                {attempt.latency_ms !== null && ` ${attempt.latency_ms}ms`}
              </span>
            ))}
          </div>
          {snapshot.health.gaps.length > 0 && (
            <ul className="gap-list muted-text">
              {snapshot.health.gaps.map((gap, index) => <li key={index}>{gap}</li>)}
            </ul>
          )}
          <DataSourceHealth compact />
          <div className="disclaimer">{snapshot.disclaimer}</div>
        </section>
      )}

      {weightOpen && (
        <WeightProfileEditor
          code={code}
          name={snapshot?.name}
          mode={mode}
          onClose={() => setWeightOpen(false)}
          onSaved={async ({ code: savedCode, describe }) => {
            // 权重档案保存后服务端立即换成新口径，但前端这份快照还是旧权重算出来的：
            // 必须清缓存 + 强刷，否则用户会看到「保存成功但分数没变」而反复保存。
            snapshotCache.current.delete(savedCode);
            setNotice(`已保存权重档案并生效：${describe}`);
            await load(savedCode, true);
            await loadOverrideSource(savedCode);
          }}
        />
      )}
    </div>
  );
}
