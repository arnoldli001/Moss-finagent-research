import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api, intradayWsUrl, IntradayBacktest, IntradaySnapshot, IntradayWatchItem,
  IntradayWatchRefresh, MarketCycle,
} from "../api";
import IntradayChart from "./IntradayChart";
import BoardPicker from "./BoardPicker";
import IntradayDailyPanel from "./IntradayDailyPanel";
import IntradayLevelFitPanel from "./IntradayLevelFit";
import DataSourceHealth from "./DataSourceHealth";
import IntradayScore from "./IntradayScore";
import IntradaySentimentPanel from "./IntradaySentiment";
import IntradayValuation from "./IntradayValuation";
import MarketContextStrip from "./MarketContextStrip";
import PrivateFeatureNotice from "./PrivateFeatureNotice";
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

/** 提示条自动消失时间（毫秒）：通知类信息不该一直占着屏幕。 */
const NOTICE_TTL_MS = 9000;

/**
 * 自选侧边栏的展开状态（localStorage 键）。
 *
 * 为什么记住：自选列表是"左侧滑动窗口"（参考同花顺持仓栏），用户收起它多半是为了
 * 给图让出宽度 —— 下次进来又被强行展开会很烦。窄屏默认收起。
 */
const DRAWER_KEY = "moss.intraday.watchDrawer";

function initialDrawerOpen(): boolean {
  try {
    const saved = window.localStorage.getItem(DRAWER_KEY);
    if (saved === "0") return false;
    if (saved === "1") return true;
  } catch {
    /* 隐私模式禁用 localStorage：按屏幕宽度决定 */
  }
  return window.innerWidth >= 1180;
}

/** 顶部提示条：带自增 id，用于「同一条消息再次出现也要重新计时」。 */
type Notice = { id: number; text: string; level: "info" | "warn" };

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

export default function IntradayTPanel({ onOpenAuction }: {
  /**
   * 「竞价选股」页签回调。
   *
   * 为什么用**回调**而不是在组件内部直接切页面：竞价选股是**组合级**子模块，
   * 它不需要也不可能针对当前这只票；而本组件内部已有 9 处
   * `mode === "intraday" | "daily" | "select"` 分支，再塞一个 mode 值要动
   * 全部 9 处、回归风险大。改成"父级换页"后，本组件**一行逻辑都不用改**，
   * 切走时整块做T面板被卸载（不会继续轮询）。
   */
  onOpenAuction?: () => void;
} = {}) {
  const [code, setCode] = useState(DEFAULT_CODE);
  const [inputCode, setInputCode] = useState(DEFAULT_CODE);
  const [snapshot, setSnapshot] = useState<IntradaySnapshot | null>(null);
  const [watch, setWatch] = useState<IntradayWatchItem[]>([]);
  const [watchRefresh, setWatchRefresh] = useState<IntradayWatchRefresh | null>(null);
  const [watchAt, setWatchAt] = useState<string>("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [wsState, setWsState] = useState<"connecting" | "live" | "fallback">("connecting");
  const [updatedAt, setUpdatedAt] = useState<string>("");
  const [showBacktest, setShowBacktest] = useState(false);
  const [backtest, setBacktest] = useState<IntradayBacktest | null>(null);
  const [backtestRunning, setBacktestRunning] = useState(false);
  const [horizon, setHorizon] = useState(6);
  const [watchBusy, setWatchBusy] = useState(false);
  // 刚加入自选（1.6 秒内把按钮文案变成"已加入"）—— 这是**替代提示气泡**的
  // 非打扰反馈：用户要求去掉"已加入自选……"的提示语，但成功后总得有个回执，
  // 否则就成了"点了不知道有没有生效"。
  const [justAdded, setJustAdded] = useState(false);
  // 海外映射输入（usNVDA,kr000660…）；留空则按板块默认映射回落
  const [overseasInput, setOverseasInput] = useState("");
  // 关联板块输入（PCB概念,PET铜箔…）；绑定后板块情绪/板块排行维度才会计入总分
  const [boardInput, setBoardInput] = useState("");
  /** 由当前个股自动推导出的"最相关概念"（**按相关性降序，默认关联前 3 个**） */
  const [autoBoard, setAutoBoard] = useState<string[]>([]);
  // 模式：日内分时做T / 日K（量价体系 + 擒牛线）
  const [mode, setMode] = useState<"intraday" | "daily" | "select">("intraday");
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
  /** 当前查看的标的（供 `loadWatch` 这种"空依赖"回调读取，避免闭包过期） */
  const codeRef = useRef("");

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

  // 选中/切换个股时，取这只票的"最相关概念"，供关联板块输入框自动填入。
  // 概念库来自 Tushare 同花顺指数名录（已剔除市场级/量化标签概念），
  // 相关性 = 窄度(55%) + 人均主力净额(28%) + 板块涨幅(17%)。
  // **默认关联前 3 个**（用户口径）：只填 1 个覆盖面太窄，
  // 而板块情绪/板块涨幅排行两个维度靠它绑定。
  useEffect(() => {
    let alive = true;
    if (!code) {
      setAutoBoard([]);
      return () => { alive = false; };
    }
    // 取 6 个候选、只用前 3 个：留点余量以便将来调数量，不必改接口
    api.stockBoards(code, 6)
      .then((data) => {
        if (!alive) return;
        setAutoBoard((data.boards ?? [])
          .map((board) => board.name)
          .filter(Boolean)
          .slice(0, 3));
      })
      .catch(() => { if (alive) setAutoBoard([]); });
    return () => { alive = false; };
  }, [code]);

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

  const loadWatch = useCallback(async (force = false, focus?: string) => {
    try {
      codeRef.current = focus ?? codeRef.current;
      // 把"当前正在看的标的"告诉后端：force 时它只重算这一只，其余走缓存 +
      // 后台补齐。自选 30+ 只时这是"强制刷新等 6~7 秒"的根治手段。
      // 用 ref 读当前代码而不是闭包变量：loadWatch 的依赖是 []（让轮询
      // 定时器不被重建），直接引用 `code` 会永远拿到首次渲染时的那个值。
      const data = await api.intradayWatchlist(force, 50, codeRef.current);
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

  /**
   * 自选侧边栏（左侧滑动窗口）。默认展开与否见 `initialDrawerOpen`；
   * 窄屏（<1180px）自动收起，避免刚进页面就把图挤成一条。
   */
  const [drawerOpen, setDrawerOpen] = useState<boolean>(initialDrawerOpen);

  const toggleDrawer = useCallback(() => {
    setDrawerOpen((current) => {
      const next = !current;
      try {
        window.localStorage.setItem(DRAWER_KEY, next ? "1" : "0");
      } catch {
        /* 记不住也不影响本次使用 */
      }
      return next;
    });
  }, []);

  // 窗口变窄时自动收起（用户手动收起的记忆保留，所以只在变窄这一侧强制）
  useEffect(() => {
    const onResize = () => {
      if (window.innerWidth < 1180) setDrawerOpen(false);
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

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
      await api.intradayAddWatch({
        code: target,
        name: current?.name ?? "",
        boards,
        overseas,
      });
      // 加入成功**不再弹提示**（用户要求去掉"已加入自选……"那类提示语）。
      // 反馈改走非打扰路径：自选列表本来就会立刻刷新（loadWatch），
      // 按钮也会短暂变成"已加入"，够用且不挡视线。
      setOverseasInput("");
      setBoardInput("");
      setJustAdded(true);
      // 侧栏是**追加**顺序，新票在末尾；不滚过去的话用户要自己找（报障过）
      revealAdded([target]);
      await loadWatch();
      window.setTimeout(() => setJustAdded(false), 1600);
    } catch (exc) {
      // 失败仍然必须报错：静默失败比弹提示更糟
      setError(`加自选失败：${String(exc)}`);
    } finally {
      setWatchBusy(false);
    }
  };

  /** 从自选移除。 */
  const removeFromWatch = async (target: string) => {
    setError(null);
    // ---- 乐观删除：先动界面，再等后端 ----
    // 用户点「移出自选」就该**立刻**看到它从列表消失。原实现是
    // "等接口 → 再回读整个列表"，而后端那次回读若撞上整表重算就会卡几秒
    // （实测 28 只票冷启动 ~114 秒），体验上就是"点删除没反应"。
    // 这里先本地移除并记住原位置，失败再**原样回滚**（不静默丢数据）。
    const previous = watch;
    setWatch((current) => current.filter((item) => item.code !== target));
    setWatchBusy(true);
    setNotice(null);
    try {
      const result = await api.intradayRemoveWatch(target);
      showNotice(`已移除自选：${target}（剩余 ${result.watchlist.length} 只）`);
      // 后端已经把数据准备好了，这里只是**对齐权威列表**（不再触发重算）
      void loadWatch();
    } catch (exc) {
      // 回滚到删除前的**完整列表**（顺序也还原），并把失败说清楚
      setWatch(previous);
      setError(`移除自选失败（已恢复原列表）：${String(exc)}`);
    } finally {
      setWatchBusy(false);
    }
  };

  const inWatchlist = watch.some((item) => item.code === inputCode);

  /** 自选列表的**展示排序**（只影响当前视图，不改配置里的顺序）。
   *
   * 为什么排序放在前端、且默认是"配置顺序"：盘中分数每分钟都变，若服务端按分数
   * 自动排，列表会自己跳动 —— 想点的那只票会在手指落下时换位置。所以默认不动，
   * 用户显式选了某个排序才按它显示；置顶项任何排序下都在最前。 */
  const [watchSort, setWatchSort] = useState<
    "config" | "chg_desc" | "chg_asc" | "score_desc" | "signal">("config");

  const SIGNAL_WEIGHT: Record<string, number> = {
    forced_exit: 4, solid: 3, hollow: 2, none: 1,
  };

  /** 刚加入自选、需要**在左侧列表里露脸**的代码（4 秒后清除高亮）。 */
  const [revealCodes, setRevealCodes] = useState<string[]>([]);
  const revealTimer = useRef<number | null>(null);
  const watchListRef = useRef<HTMLUListElement | null>(null);

  useEffect(() => () => {
    if (revealTimer.current) window.clearTimeout(revealTimer.current);
  }, []);

  /**
   * 「加自选」成功后的**定位反馈**：展开侧栏 → 刷新列表 → 滚到新票并高亮 4 秒。
   *
   * ## 为什么必须有这一步（用户报障）
   *
   * 自选池是**追加**语义（`upsert_watch` 把新票放到末尾）。用户从
   * 「量化选股」一键加了 20 只之后，它们在 43 只里的第 24~43 位 ——
   * 侧栏默认按配置顺序显示，那一段正好在可视区之外。
   * 结果就是"加了自选但列表里没有"，用户以为加错了地方（实测报障）。
   *
   * 这里**不去改配置顺序**（用户的排列是有意义的），而是把刚加的那几只
   * 滚进视野并短暂高亮 —— 加完就能看见它在哪。
   */
  const revealAdded = useCallback((codes: string[]) => {
    const cleaned = codes.filter(Boolean);
    if (cleaned.length === 0) return;
    // 收起状态下什么都不显示，等于没有反馈 —— 用户刚做完"加"这个动作，
    // 这正是需要把列表露出来的时刻。
    setDrawerOpen(true);
    setRevealCodes(cleaned);
    if (revealTimer.current) window.clearTimeout(revealTimer.current);
    revealTimer.current = window.setTimeout(() => setRevealCodes([]), 4000);
    void loadWatch();
  }, [loadWatch]);

  // 新票要等 `loadWatch()` 回来才在 DOM 里，所以滚动依赖 watch 一起触发
  useEffect(() => {
    if (revealCodes.length === 0) return;
    const list = watchListRef.current;
    if (list === null) return;
    for (const target of revealCodes) {
      const row = list.querySelector<HTMLElement>(`[data-code="${target}"]`);
      if (row !== null) {
        row.scrollIntoView({ block: "center", behavior: "smooth" });
        return;
      }
    }
  }, [revealCodes, watch]);

  /** 排序后的自选列表：置顶优先，其余按所选字段。 */
  const sortedWatch = useMemo(() => {    if (watchSort === "config") return watch;
    const value = (item: IntradayWatchItem): number => {
      if (watchSort === "signal") return SIGNAL_WEIGHT[item.signal_strength] ?? 0;
      if (watchSort === "score_desc") return item.total_score ?? -Infinity;
      return item.change_pct ?? -Infinity;
    };
    const sorted = [...watch].sort((left, right) => {
      const diff = value(right) - value(left);
      return watchSort === "chg_asc" ? -diff : diff;
    });
    // 置顶项永远在最前（排序只在组内生效）
    return [...sorted.filter((i) => i.pinned), ...sorted.filter((i) => !i.pinned)];
  }, [watch, watchSort]);

  /** 置顶/取消置顶（乐观更新：先动界面，失败回滚）。 */
  const togglePin = async (target: string, pinned: boolean) => {
    const previous = watch;
    // 乐观更新 + 本地重排，避免等接口回来列表才动
    setWatch((current) => {
      const next = current.map((item) =>
        item.code === target ? { ...item, pinned } : item);
      return [...next.filter((i) => i.pinned), ...next.filter((i) => !i.pinned)];
    });
    setWatchBusy(true);
    try {
      await api.intradayPinWatch(target, pinned);
      showNotice(pinned ? `已置顶：${target}` : `已取消置顶：${target}`);
    } catch (exc) {
      setWatch(previous);
      setError(`置顶失败（已恢复）：${String(exc)}`);
    } finally {
      setWatchBusy(false);
    }
  };

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

  /**
   * 提示条（notice）的唯一入口：自增 id + 自动消失计时。
   *
   * 为什么要 id：连续两次触发同一条消息时，纯文本 state 值相同，
   * React 不会重渲染、计时器也不会重置；带上自增 id 就每次都是"新消息"。
   */
  const noticeSeq = useRef(0);
  const showNotice = useCallback((text: string, level: "info" | "warn" = "info") => {
    noticeSeq.current += 1;
    setNotice({ id: noticeSeq.current, text, level });
  }, []);

  /**
   * 提示条自动消失。
   *
   * 旧实现把 notice 当永久状态：保存权重后那句「已保存权重档案并生效：…」
   * 会**一直挂在页面上**（用户实测报障）。通知类信息没有理由常驻 ——
   * 需要留住的信息（权重口径、拟合依据、数据健康度）都已经在各自面板里，
   * 顶部这条只负责"刚才那一下成功了"，看到就该走。
   */
  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(null), NOTICE_TTL_MS);
    return () => window.clearTimeout(timer);
  }, [notice]);

  // 切换标的时清掉上一条提示：它说的是上一只票的事，留着只会误导
  useEffect(() => { setNotice(null); }, [code]);

  /**
   * 重新拟合成功后的强刷。
   *
   * 必须用 `useCallback` 固定身份：这个回调会作为 prop 传进拟合面板，
   * 若每次渲染都新建，面板里依赖它的 `useCallback` 就会跟着变 ——
   * 一旦形成「快照更新 → 回调变 → 重新拟合 → 快照更新」就是死循环
   * （实测：页面每秒重渲染一次）。依赖里的 `load` 本身是稳定的 useCallback。
   */
  const handleFitted = useCallback(async () => {
    snapshotCache.current.delete(code);
    await load(code, true);
  }, [code, load]);

  return (
    <div className="intraday-root">
      <div className="intraday-toolbar">
        {/* 自选侧边栏开合：不必先进设置，一个图标按钮直接切 */}
        <button className={`btn-ghost drawer-toggle${drawerOpen ? " active" : ""}`}
                onClick={toggleDrawer}
                title={drawerOpen ? "收起自选列表（给图让出宽度）" : "展开自选列表（左侧滑动窗口）"}
                aria-expanded={drawerOpen}>
          <span className="drawer-icon" aria-hidden="true">☰</span>
          自选
          <span className="mono">{watch.length}</span>
        </button>
        <span className="intraday-title">股票做T辅助</span>
        {/* 「量化选股」是**组合级**子模块：它不针对某一只票，因此这里把整排
            个股级操作（选股/自选增删/板块/强制刷新/阈值回测/权重编辑/推送状态）
            全部收起来 —— 留着会让用户以为"选股结果会跟着这只票走"。
            自选侧边栏仍保留：既能看到自选池，量化选股的结果也是往它里面加。 */}
        {mode !== "select" && (
          <>
        <StockPicker
          value={inputCode}
          // ⚠️ 这里**不能**写成 `next.replace(/[^\d]/g, "")`：那个过滤器会把
          // 拼音与中文全删掉，于是"输拼音时输入框永远是空的"（实测用户报障：
          // 联想列表能出「301013 利和兴 LHX」，但输入框里看不到自己打的字）。
          // 现在只在"纯数字超长"时截到 6 位（防粘贴一串数字），其余原样保留，
          // 让拼音首字母 / 全拼 / 中文名都能正常显示与联想。
          onChange={(next) => setInputCode(
            /^\d+$/.test(next) ? next.slice(0, 6) : next)}
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
          <button className={`btn-ghost${justAdded ? " done" : ""}`}
                  disabled={watchBusy || justAdded}
                  onClick={() => void addToWatch(inputCode)}
                  title="加入自选池并写回 configs/intraday.yaml">
            {watchBusy ? "处理中…" : justAdded ? "✓ 已加入" : "＋ 加自选"}
          </button>
        )}
        <BoardPicker
          value={boardInput}
          onChange={setBoardInput}
          autoValue={autoBoard}
          disabled={watchBusy}
        />
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
          </>
        )}
      </div>

      {error && <div className="error-box">{error}</div>}
      {/* 提示：**悬浮 toast**（fixed 定位），不占文档流。
          原来它是插在面板里的一个 info-box，出现时会把下方所有面板整体下移 ——
          用户明确反馈"体验不好"。现在浮在右下角，最多覆盖一点空白，
          9 秒自动消失，也可手动关闭（用户口径：最多悬浮提示）。 */}
      {notice && (
        <div className={`notice-toast ${notice.level === "warn" ? "warn" : "info"}`}
             role="status" aria-live="polite">
          <span className="notice-text">{notice.text}</span>
          <button className="notice-close" onClick={() => setNotice(null)}
                  title="关闭提示（也会在 9 秒后自动消失）"
                  aria-label="关闭提示">×</button>
        </div>
      )}

      {/* 两栏：左=自选滑动窗口（可收起）／右=个股工作面。
          自选不再横向平铺 —— 十几只票会占掉 2~3 行，把图挤到下面去（用户报障）。 */}
      <div className={`intraday-body${drawerOpen ? " drawer-open" : ""}`}>
        <aside className="intraday-drawer" aria-hidden={!drawerOpen}>
          <div className="drawer-head">
            <b>自选</b>
            <span className="muted-text">（{watch.length}）</span>
            <select className="watch-sort mono" value={watchSort}
                    onChange={(event) => setWatchSort(event.target.value as
                      "config" | "chg_desc" | "chg_asc" | "score_desc" | "signal")}
                    title="列表排序（只改显示顺序，不改配置顺序；置顶项永远在最前）">
              <option value="config">配置顺序</option>
              <option value="chg_desc">涨幅↓</option>
              <option value="chg_asc">涨幅↑</option>
              <option value="score_desc">分数↓</option>
              <option value="signal">信号优先</option>
            </select>
            <button className="btn-ghost tiny" disabled={watchBusy}
                    onClick={() => void loadWatch(true, code)}
                    title="忽略缓存立即重算全部自选（盘中本来每分钟自动刷新，这里用于临时催一次）">
              ↻ 刷新
            </button>
            <button className="btn-ghost tiny drawer-hide" onClick={toggleDrawer}
                    title="收起自选列表">‹</button>
          </div>
          {/* 报价与打分是两条节奏，必须分开写：否则用户会把 60 秒前的信号当成此刻的信号 */}
          <div className={`watch-refresh drawer-status ${
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
              ? (watchRefresh.warming
                  // 服务端还没有任何缓存（首次部署 / 热缓存过期 / 自选池大改）：
                  // 这份列表只有代码·名称·板块，价格由报价快车道几秒内贴上，
                  // 分数与信号要等后台整表重算。不说清楚的话，用户会把 "—" 当成数据坏了。
                  ? "⟳ 首次重算中：先给自选清单与现价，分数稍后补上"
                  : watchRefresh.enabled && watchRefresh.window_open && watchRefresh.running
                  ? `⟳ 报价 ${watchRefresh.quote_interval_seconds}s`
                    + ` · 打分 ${watchRefresh.interval_seconds}s`
                    + `${watchRefresh.quote_last_run_at
                        ? ` · ${watchRefresh.quote_last_run_at}` : ""}`
                  : `⏸ ${watchRefresh.window_reason}${watchAt ? ` · 数据 ${watchAt}` : ""}`)
              : ""}
            {watchRefresh?.last_error || watchRefresh?.quote_last_error
              ? " ⚠刷新异常" : ""}
          </div>
          <ul className="watch-list" ref={watchListRef}>
            {watch.length === 0 && (
              <li className="muted-text watch-empty">
                暂无自选 —— 在上方输入 6 位代码后点「＋ 加自选」即可
              </li>
            )}
            {sortedWatch.map((item) => (
              <li key={item.code}
                  data-code={item.code}
                  className={`watch-row ${item.code === code ? "active" : ""}` +
                             (item.pinned ? " pinned" : "") +
                             (revealCodes.includes(item.code) ? " just-added" : "")}>
                <button className="watch-pick"
                        title={`切换到 ${item.name || item.code}`}
                        onClick={() => {
                          setInputCode(item.code);
                          setCode(item.code);
                          setShowBacktest(false);
                          setBacktest(null);
                          // 窄屏下选完就把列表收起来，避免它一直盖着图
                          if (window.innerWidth < 1180) setDrawerOpen(false);
                        }}>
                  <span className="watch-name">{item.name || item.code}</span>
                  <span className="watch-code mono muted-text">{item.code}</span>
                  <span className="watch-chg mono" style={{
                    color: (item.change_pct ?? 0) >= 0 ? "var(--low)" : "var(--high)",
                  }}>
                    {item.change_pct === null || item.change_pct === undefined
                      ? "—" : `${item.change_pct >= 0 ? "+" : ""}${item.change_pct.toFixed(2)}%`}
                  </span>
                  <span className="watch-score mono muted-text">
                    {item.total_score === null || item.total_score === undefined
                      ? "" : `分 ${item.total_score >= 0 ? "+" : ""}${item.total_score.toFixed(0)}`}
                  </span>
                  <SignalBadge item={item} />
                </button>
                <button className={`watch-pin ${item.pinned ? "on" : ""}`}
                        disabled={watchBusy}
                        title={item.pinned ? "取消置顶" : "置顶（永远排在最前）"}
                        onClick={(event) => {
                          event.stopPropagation();
                          void togglePin(item.code, !item.pinned);
                        }}>
                  {item.pinned ? "📌" : "📍"}
                </button>
                <button className="watch-remove" disabled={watchBusy}
                        title={`从自选移除 ${item.code}`}
                        onClick={() => void removeFromWatch(item.code)}>×</button>
              </li>
            ))}
          </ul>
        </aside>
        {/* 窄屏时的点击遮罩：点一下就把抽屉收起来 */}
        <button className="drawer-backdrop" aria-label="收起自选列表"
                onClick={() => setDrawerOpen(false)} />

        <div className="intraday-main">
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

          {/* 日内分时 / 日K / 竞价选股 / 量化选股 的口径切换：紧贴走势图上方（用户要求的位置），
              它决定下面整块图与打分是"分钟级"、"日线级"，还是"组合级选股"。
              标签用「周期」而不是「走势图口径」：这是用户的原话口径，
              而且它切换的其实是整个面板的计算周期（含档位与信号），不只是图。
              「量化选股」是**组合级**子模块（不针对当前那一只票），
              所以切过去时整块个股图与打分会被替换掉，见下方 mode === "select" 分支。 */}
          <div className="mode-bar">
            <span className="muted-text">周期</span>
            <div className="mode-switch">
              <button className={mode === "intraday" ? "mode-btn active" : "mode-btn"}
                      onClick={() => setMode("intraday")}
                      title="日内分时做T：分钟级多因子打分 + ±提示线/动手线 阈值信号">
                日内分时
              </button>
              <button className={mode === "daily" ? "mode-btn active" : "mode-btn"}
                      onClick={() => setMode("daily")}
                      title="日K：蜡烛柱状图 + 擒牛线（NML/QRL/CBX20/CBX60/SMX）+ 量柱体系 + 高量柱攻防 + B1-B15/S1-S6 战法；均线已去掉">
                日K
              </button>
              <button className={mode === "select" ? "mode-btn active" : "mode-btn"}
                      onClick={() => setMode("select")}
                      title="量化选股：开市日 9:25–9:45 与 14:45 用 3 档模型自动选股；支持自定义板块">
                量化选股
              </button>
              {/* 「竞价选股」紧挨在「量化选股」右侧；它是独立的组合级页面，
                  所以由父级换页而不是加进本组件的 mode（见组件签名处的说明）。 */}
              {onOpenAuction && (
                <button className="mode-btn"
                        onClick={onOpenAuction}
                        title="竞价选股：开市日 9:25:00 自动跑，只选昨日涨停且 30~110 亿的票，9:27 前出池">
                  竞价选股
                </button>
              )}
            </div>
            {mode === "daily" && (
              <span className="muted-text">
                （日K模式的档位由量价体系自算，止损/保护线不走分时档位）
              </span>
            )}
            {mode === "select" && (
              <span className="muted-text">
                （组合级选股：不针对当前这一只票，结果需手动点「＋加自选」）
              </span>
            )}
          </div>

          {mode === "select" ? (
        <PrivateFeatureNotice feature="量化选股（3档模型定时选股）" />
      ) : mode === "daily" ? (
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
              {/* 档位口径与拟合依据：用户必须知道现在用的是规则线还是拟合线 */}
              <IntradayLevelFitPanel
                code={code}
                fit={snapshot?.level_fit ?? null}
                levels={snapshot?.levels ?? null}
                onFitted={handleFitted}
              />
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
        </div>
      </div>

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
          // 权重档案只有"日内分时 / 日K"两套口径；`select` 是组合级模块，
          // 权重编辑按钮在它下面本来就被收起来了（见工具栏的 mode !== "select"）。
          mode={mode === "daily" ? "daily" : "intraday"}
          levels={snapshot?.levels ?? null}
          onClose={() => setWeightOpen(false)}
          onSaved={async ({ code: savedCode, describe }) => {
            // 权重档案保存后服务端立即换成新口径，但前端这份快照还是旧权重算出来的：
            // 必须清缓存 + 强刷，否则用户会看到「保存成功但分数没变」而反复保存。
            snapshotCache.current.delete(savedCode);
            showNotice(`已保存权重档案并生效：${describe}`);
            await load(savedCode, true);
            await loadOverrideSource(savedCode);
          }}
        />
      )}
    </div>
  );
}
