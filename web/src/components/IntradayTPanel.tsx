import { getParam, setParam } from "../route";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api, intradayWsUrl, IntradayBacktest, IntradaySnapshot, IntradayWatchItem,
  IntradayWatchRefresh,
} from "../api";
import IntradayChart from "./IntradayChart";
import BoardPicker from "./BoardPicker";
import IntradayDailyPanel from "./IntradayDailyPanel";
import IntradayLevelFitPanel from "./IntradayLevelFit";
import DataSourceHealth from "./DataSourceHealth";
import IntradayScore from "./IntradayScore";
import IntradaySentimentPanel from "./IntradaySentiment";
import MarketContextStrip from "./MarketContextStrip";
import PrivateFeatureNotice from "./PrivateFeatureNotice";
import { QuantSelectPanel } from "../privatePanels";
import { StockPicker } from "./StockPicker";
import { useQuantSectors, type DrawerSource } from "./useQuantSectors";
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
  low_buy: "回踩区间提示",
  high_sell: "冲高区间提示",
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

/**
 * 估值结论标签（自选列表行内，紧跟「观望/回踩」信号右侧）。
 *
 * 用户口径 2026-09-23：原来主区域整块「① 估值空间」面板（PE/PB 分位条 +
 * 同业对比表）太占地方，结论只留这四档，收成列表里的一个标签：
 * 「估值透支 / 估值合理偏贵 / 估值合理 / 上涨空间充足」。
 *
 * 拿不到结论时**什么都不渲染**（不占位、不写"—"）：这一列是扫描用的，
 * 一片"—"会把「观望」那一列的对齐全部冲乱，也没有任何信息量。
 * 服务端冷启动第一轮可能还没有结论（估值走的是三年日频序列，见
 * `IntradayService._valuation_label` 的 TTL 缓存），下一轮自选刷新就会补上。
 */
function ValuationTag({ item }: { item: IntradayWatchItem }) {
  const label = item.valuation_label ?? "";
  if (!label) return null;
  const bucket = item.valuation_bucket || "unknown";
  return (
    <span className={`val-tag val-${bucket}`} title={`估值结论：${label}`}>
      {label}
    </span>
  );
}

/**
 * 把一段**用户输入**解析成股票列表 —— 顶部「加自选」与抽屉「加成分股」**共用**它。
 *
 * 用户口径 2026-09-23：
 *   - "支持一次输入多个股票加自选"；
 *   - "支持 6 位数字自动识别、股票名模糊匹配"；
 *   - "支持逗号 空格等分割符号来识别多个股的输入"（参考同花顺的批量加自选）。
 *
 * 规则（**顺序即优先级**）：
 *   1. 按 `,` `，` `、` `;` `；` 与空白切成 token；
 *   2. `^\d{6}$` → 直接当代码收下（不查库，最快）；
 *   3. 其余 → 走 `stockSearch` 按**股票名/拼音**模糊匹配：**完全同名优先**，
 *      否则**只在唯一命中时**自动采用 —— 多个候选不替用户猜（猜错会静默加错票）；
 *   4. 认不出的原样返回在 `unresolved` 里，由调用方明确报错。
 *
 * ⚠️ 第 4 条是**硬的**：绝不把一段文字当成股票代码存下去。实测踩过 ——
 *    旧的「加成分股」把 `00兆易创新` 原样存成 code，点它会 `switchTo("00兆易创新")`
 *    → `/intraday/snapshot` 返回 **400**「证券代码应为6位数字」。
 */
async function resolveStockTokens(text: string): Promise<{
  items: { code: string; name: string }[];
  unresolved: string[];
}> {
  const tokens = text.split(/[,，、;；\s]+/).map((t) => t.trim()).filter(Boolean);
  const items: { code: string; name: string }[] = [];
  const unresolved: string[] = [];
  const seen = new Set<string>();
  const push = (code: string, name: string) => {
    if (!seen.has(code)) { seen.add(code); items.push({ code, name }); }
  };
  for (const token of tokens) {
    if (/^\d{6}$/.test(token)) { push(token, ""); continue; }
    try {
      const found = (await api.stockSearch(token, 5)).stocks ?? [];
      const exact = found.find((entry) => entry.name === token);
      const picked = exact ?? (found.length === 1 ? found[0] : undefined);
      if (picked) push(picked.code, picked.name);
      else unresolved.push(token);
    } catch {
      unresolved.push(token);
    }
  }
  return { items, unresolved };
}

export default function IntradayTPanel({ view = "t", onEditProfile }: {
  /**
   * 顶部一级页签落在本面板上的**模块**：`t` = 做T辅助（本面板本体），
   * `select` = 量化选股（组合级，见 `QuantTabContainer`）。
   *
   * 为什么由父级传入、而不是本组件自己的 state：这两个模块在顶部是**并列页签**
   * （量化选股 · 竞价选股 · 做T辅助），页签状态必须活在容器里 ——
   * 否则切到竞价选股时本组件被卸载，页签会跟着一起消失。
   *
   * 为什么不是 9 处 `mode === …` 分支各加一个 `select` 判断：
   * 传进来的值只决定"这一页显示哪块内容"，做T自己的**周期口径**
   * （日内分时 / 日K）仍是本组件内部的 `tMode`，两者在下面合成同一个 `mode`，
   * 因此原有的分支一行都不用改。
   */
  view?: "t" | "select";
  /** 打开「个股口径」抽屉（阈值/权重/档位，只影响自己）。 */
  onEditProfile?: (code: string) => void;
} = {}) {
  /* ★ 当前标的从 URL 恢复（用户口径 2026-09-24："把它们也写进 URL"）。
   原来刷新会回到默认标的 —— 而"我现在看的是哪只票"正是最该被分享/复现的状态。 */
  const [code, setCode] = useState(() => {
    const fromUrl = getParam("code");
    return /^\d{6}$/.test(fromUrl) ? fromUrl : DEFAULT_CODE;
  });
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
  /**
   * 已保存的「个股绑定」全量（关联板块 / 海外映射）—— 只用于输入框的**联想下拉**。
   *
   * 用户口径 2026-09-23："这个配置加载最好是自动联想……避免加自选时无法迅速读取。"
   * 数据来自服务端**进程内热map**（启动时 `warm_bindings()` 灌好），所以这个列表
   * 在页面挂载时拉一次就够，不必跟着切股反复请求。
   */
  const [savedBindings, setSavedBindings] = useState<
    { code: string; name: string; boards: string[]; overseas: string[] }[]>([]);
  /** 由当前个股自动推导出的"最相关概念"（**按相关性降序，默认关联前 3 个**） */
  const [autoBoard, setAutoBoard] = useState<string[]>([]);
  // 做T辅助内部的**周期口径**：日内分时 / 日K（量价体系 + 擒牛线）。
  // 注意它只覆盖「做T辅助」这一个模块 —— 「量化选股」是顶部的另一个模块，
  // 由 `view` prop 决定，见下面的 mode 合成。
  const [tMode, setTMode] = useState<"intraday" | "daily">(
    () => (getParam("mode") === "daily" ? "daily" : "intraday"));
  // 合成后的显示口径 = 模块页签 + 周期口径。
  // 为什么合成而不是各判各的：本面板有 9 处 `mode === "intraday" | "daily"
  // | "select"` 分支（工具栏、周期条、主内容区都靠它分派），合成一个值
  // 等于"加模块页签"这件事对那 9 处**完全透明**。
  // 「量化选股」是模块级、优先级高于周期口径，所以 select 时忽略 tMode
  // （此时周期条整体隐藏，见主内容区）。
  const mode: "intraday" | "daily" | "select" =
    view === "select" ? "select" : tMode;
  // 权重编辑弹窗（做T权重自定义 + 写入个股权重档案）
  const [weightOpen, setWeightOpen] = useState(false);
  // 这只票当前生效的口径来源（"全局口径" / "权重档案（…）"）
  const [overrideSource, setOverrideSource] = useState("");
  const socketRef = useRef<WebSocket | null>(null);
  const pollRef = useRef<number | null>(null);
  // 请求竞态守卫：快速连点「切换标的」时，只接受最后一次请求的结果，
  // 避免慢的旧响应覆盖新标的的快照。
  const requestSeq = useRef(0);
  /** 当前查看的标的（供 `loadWatch` 这种"空依赖"回调读取，避免闭包过期） */
  const codeRef = useRef("");
  /**
   * 自选列表的数据源：**只有共享那一份**（`configs/intraday.yaml`）。
   *
   * 「我的池 / 共享」开关已随自选池功能删除（用户口径 2026-09-23）。
   * 「加自选 / 删自选」本来就写这份共享清单（见 `addToWatch`），
   * 所以删掉开关没有动到日常路径，只是少了那个会切到"用户自己的池"的分支。
   */

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

  /**
   * 联想下拉的数据源：**页面挂载时拉一次**全部已保存绑定（用户口径 2026-09-23）。
   *
   * 为什么不像切股那样跟着 `code` 拉：这份列表是"历史配置目录"，与当前看哪只票无关；
   * 跟着切股拉只会白打请求。它也用不到刷新 —— 服务端热map 只在自己保存时才变，
   * 而保存动作就发生在本页面（见 `addToWatch` 之后的重新拉取）。
   */
  const reloadSavedBindings = useCallback(() => {
    api.intradayStockBindings()
      .then((data) => setSavedBindings(data.items ?? []))
      .catch(() => setSavedBindings([]));
  }, []);

  useEffect(() => { reloadSavedBindings(); }, [reloadSavedBindings]);

  /** 历史配置联想的两个 datalist id（在 JSX 里挂在对应输入框上）。 */
  const BOARD_HISTORY_ID = "intraday-board-history";
  const OVERSEAS_HISTORY_ID = "intraday-overseas-history";
  /** 用户配过的板块名 / 海外代码，去重保序 —— 供输入框自动补全。 */
  const historyBoards = useMemo(() => {
    const out: string[] = [];
    for (const item of savedBindings) {
      for (const name of item.boards ?? []) {
        if (name && !out.includes(name)) out.push(name);
      }
    }
    return out;
  }, [savedBindings]);
  const historyOverseas = useMemo(() => {
    const out: string[] = [];
    for (const item of savedBindings) {
      for (const code of item.overseas ?? []) {
        if (code && !out.includes(code)) out.push(code);
      }
    }
    return out;
  }, [savedBindings]);

  /**
   * **切股时预填**这只票已保存的关联板块 / 海外映射（用户口径 2026-09-23）。
   *
   * 语义（与后端 `add_watch` 的回落规则一致）：
   *   - 先清空两个输入框（换票了，上一只的输入不该残留）；
   *   - 拉到已存配置就填上，用户可以接着改；改完点「加自选」即覆盖回库；
   *   - 没存过（`saved=false`）就保持空白，交给后端按板块默认映射回落。
   * ⚠️ 竞态：快速切股时只接受最后一次请求的结果，否则慢的旧响应会把新票的预填冲掉。
   */
  useEffect(() => {
    let alive = true;
    setBoardInput("");
    setOverseasInput("");
    api.intradayStockBinding(code)
      .then((data) => {
        if (!alive || !data.saved) return;
        setBoardInput((data.boards ?? []).join(","));
        setOverseasInput((data.overseas ?? []).join(","));
      })
      .catch(() => { /* 取不到就保持空白：后端保存时会自己回落，不影响加自选 */ });
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
      // ★ 两段式首屏：内存里没有这只票的快照时，**先要轻量版把图打出来**，
      //   再取完整版补消息面/估值/板块分时。
      //   冷标的一次完整快照实测 4.5~7 秒（板块分时单次可到 20 秒），
      //   而分时图/现价/关键价位/总分都在轻量版里 —— 用户点开一只票第一眼
      //   要看的就是这些，"六七秒才出图"就是这么来的。
      if (!cached) {
        const quick = await api.intradaySnapshot(target, false, true);
        if (seq !== requestSeq.current) return;
        setSnapshot(quick);
        setLoading(false);
      }
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
      //
      // ★ 数据源：**只有共享清单一份**（`configs/intraday.yaml`）。
      //   原先这里有个"我的池 / 共享"开关，按用户取 `intradayWatchlistMine`；
      //   自选池功能已删除（用户口径 2026-09-23："很鸡肋，不需要了"），
      //   所以那条分支和它的 localStorage 记忆一起拿掉了 —— 留着开关会让
      //   用户切到一个已经不存在的数据源。
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
/* 标的/周期变化 → 回填 URL（`setParam` 逐键写，不踩别人的键） */
  useEffect(() => { setParam("code", code); }, [code]);
  useEffect(() => { setParam("mode", tMode); }, [tMode]);

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
            // 轻量帧只渲染、**不进缓存**：它缺消息面/估值/板块分时，
            // 若被缓存下来，切走再切回就会拿一份"半成品"当完整快照用
            // （`load()` 命中缓存时不会再发完整请求）。完整帧紧随其后到达并覆盖。
            if (!payload.light) snapshotCache.current.set(code, data);
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

  // ⚠️ 「市场情绪周期（做T环境温度）」整条**2026-09-23 按用户要求从日内分时移除**
  //    （原来在这里有一个 `api.intradayMarketCycle()` 拉取 + 工具栏右侧的
  //    `.cycle-strip` 展示条）。连同它的 useState / useEffect 一起删掉 ——
  //    只删 JSX 会留下一个没人用的 state 和一个每切一次模式就白跑一次的请求。
  //    `MarketCycle` 类型与 `api.intradayMarketCycle` **都还在用**：权重编辑弹窗
  //    （`WeightProfileEditor`）仍然展示同一条情绪周期，那里保留。

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
    // **批量入口**（用户口径 2026-09-23）：输入框支持"一次输入多个股票"，
    // 逗号/空格/顿号/分号分隔；6 位数字直通，其余按股票名/拼音模糊匹配。
    // 解析与抽屉「加成分股」**共用 `resolveStockTokens`** —— 两处规则永远一致，
    // 也保证"认不出的输入"在两边都不会被当成代码存下去。
    const { items, unresolved } = await resolveStockTokens(target);
    if (unresolved.length > 0) {
      setError(`认不出这些输入，未加自选：${unresolved.join("、")}`
        + `（请填 6 位代码，或写完整股票名）`);
    }
    if (items.length === 0) return;
    if (items.length > 1) {
      // ⚠️ 多只**必须一次提交**：逐只循环会让"自选概览缓存"被反复作废，
      //    而随后那次整表重算是整个动作里最贵的一步（见 api.ts 里该接口的说明）。
      setWatchBusy(true);
      setError(null);
      try {
        await api.quantSelectAddManyToWatchlist(items);
        revealAdded(items.map((item) => item.code), items[0].code);
        showNotice(`已加入 ${items.length} 只自选`);
        void loadWatch(false, items[0].code);
      } catch (exc) {
        setError(`加自选失败：${String(exc)}`);
      } finally {
        setWatchBusy(false);
      }
      return;
    }
    target = items[0].code;      // 单只 → 走下面原有逻辑（保留乐观插入/高亮等行为）
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
      const added = await api.intradayAddWatch({
        code: target,
        name: current?.name ?? "",
        boards,
        overseas,
      });
      // ---- 乐观插入：新票**立刻**出现在左侧列表顶部 ----
      //
      // ⚠️ 为什么要乐观插入，而不是只靠随后的 `loadWatch()`（用户 2026-09-23 报障）：
      // 后端把新票放进列表要等**整表重算**（`_recompute_into_cache` 才写缓存，
      // 自选 40+ 只时冷算可达百秒），而 `loadWatch(force=false)` 走的是
      // "有旧缓存就直接给 + 后台补齐"——于是加完立刻取，拿到的仍是**没有它**的
      // 旧列表（或只有身份字段的占位表）。表现就是"加完自选，左侧看不到这只票"。
      //
      // ⚠️ 位置是**顶部**（用户 2026-09-23 要求：「新加自选股，要放在自选股池的顶部」）：
      // 后端 `upsert_watch(prepend=True)` 也把新票写进配置的置顶组之后，
      // 两边顺序一致 —— 否则刷新一次（`loadWatch` 覆盖本地状态）它就会跳回末尾。
      //
      // 与 `removeFromWatch` 的乐观删除对称：先动界面，等后端补齐。
      // 这里只补**身份字段**（代码/名称/板块），分数与信号留空由刷新填 ——
      // 编一个像样的分数比空着更糟（前端会把 null 显示成 "—"）。
      // 名称优先用接口回显（后端缺名时会查本地股票字典补齐），其次快照里的名字。
      const listed = added.watchlist?.find((item) => item.code === target);
      setWatch((currentWatch) => currentWatch.some((item) => item.code === target)
        ? currentWatch
        : [{
            code: target,
            name: listed?.name || current?.name || "",
            boards: listed?.boards ?? boards,
            total_score: null,
            signal_strength: "none" as const,
            signal_kind: "none" as const,
            price: current?.quote?.price ?? null,
            change_pct: current?.quote?.change_pct ?? null,
            // 新加的票不可能是置顶的（置顶要用户显式点），所以固定 false，
            // 不去读 config item 上并不存在的 `pinned` 字段
            pinned: false,
          }, ...currentWatch]);
      // 加入成功**不再弹提示**（用户要求去掉"已加入自选……"那类提示语）。
      // 反馈改走非打扰路径：自选列表本来就会立刻刷新（loadWatch），
      // 按钮也会短暂变成"已加入"，够用且不挡视线。
      setOverseasInput("");
      setBoardInput("");
      setJustAdded(true);
      // 刚保存的板块/映射要**立刻进联想下拉**（否则用户得刷新页面才看得到自己刚配的值）
      reloadSavedBindings();
      // 侧栏是**追加**顺序，新票在末尾；不滚过去的话用户要自己找（报障过）
      revealAdded([target], target);
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
   *
   * `focus` 跟上"当前正在看的标的"，让随后那次刷新走**单只快车道**：
   * `force=true` + `active=<新票>` 时后端只重算这一只（实测 0.23s），
   * 其余 45 只用缓存原值立即返回 —— 这就是"加一只票不再触发全表重算"的关键。
   * 不传 `focus` 的话会退化成整表重算（真实数据源约 200 秒）。
   */
  const revealAdded = useCallback((codes: string[], focus?: string) => {
    const cleaned = codes.filter(Boolean);
    if (cleaned.length === 0) return;
    // 收起状态下什么都不显示，等于没有反馈 —— 用户刚做完"加"这个动作，
    // 这正是需要把列表露出来的时刻。
    setDrawerOpen(true);
    setRevealCodes(cleaned);
    if (revealTimer.current) window.clearTimeout(revealTimer.current);
    revealTimer.current = window.setTimeout(() => setRevealCodes([]), 4000);
    // force=true 只在带了 focus 时才走单只快车道（后端契约：active 仅对 force 有意义）
    void loadWatch(focus !== undefined, focus);
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
      parts.push(`触及回踩线 ${touchLow.length} 次，触点处最高总分 `
        + `${bestLow !== null && bestLow >= 0 ? "+" : ""}${bestLow?.toFixed(1)}`);
    }
    if (touchHigh.length) {
      parts.push(`触及冲高线 ${touchHigh.length} 次，触点处最低总分 `
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

  /**
   * 自定义板块 —— 左侧下拉的第二个来源。
   *
   * 板块**既是选股范围也是归类标签**：选股结果要往里放，看盘时要按板块翻个股。
   * 所以列表、新建、加成分、删除全部收在这条抽屉里，不再放「量化选股」面板
   * （用户口径 2026-09-21：板块要"和自选列一个位置"，切页签才能看板块太绕）。
   *
   * 公开仓库没有 `/api/v1/quant/sectors`（私有路由），此时 `available=false`，
   * 下拉退化成只有「自选」—— 不报错、不留空面板，见 `useQuantSectors`。
   */
  const sectorsApi = useQuantSectors();
  const [source, setSource] = useState<DrawerSource>({ kind: "watch" });
  /** 新建板块表单是否就地展开（不跳页签）。 */
  const [creating, setCreating] = useState(false);
  const [newSector, setNewSector] = useState({
    name: "", kind: "manual" as "manual" | "dynamic",
    minMv: "20", maxMv: "150", industries: "",
  });
  /** 「加成分股」输入框（逗号/空格分隔，服务端幂等去重）。 */
  const [memberDraft, setMemberDraft] = useState("");
  /**
   * **按代码共享**的行情缓存：`code → {price, change_pct}`。
   *
   * 用户口径 2026-09-23：自定义板块的成分行也要像自选行那样显示涨跌幅。
   * 成分表本身不带行情，所以另取一批；而"**这些用户添加的股票，有可能在多个
   * 自定义板块里**"—— 所以缓存按 `code` 存（不是按板块），同一只票只抓一次，
   * 切板块时已缓存的直接命中。
   */
  const [memberQuotes, setMemberQuotes] = useState<
    Record<string, { price: number | null; change_pct: number | null }>>({});

  const activeSector = source.kind === "sector"
    ? sectorsApi.sectors.find((sector) => sector.id === source.id) ?? null
    : null;
  /** 下拉当前值。用 `activeSector` 而非 `source` 推导：板块被删时能自动回落。 */
  const sourceKey = activeSector !== null ? `sector:${activeSector.id}` : "watch";

  // 板块成分的涨跌幅：只对**尚未缓存**的代码发一次批量请求，结果并进共享缓存。
  useEffect(() => {
    if (activeSector === null) return;
    const missing = activeSector.members
      .map((member) => member.code)
      .filter((code) => /^\d{6}$/.test(code) && !(code in memberQuotes));
    if (missing.length === 0) return;
    let alive = true;
    api.intradayQuotes(missing)
      .then((data) => {
        if (!alive) return;
        setMemberQuotes((current) => {
          const next = { ...current };
          for (const item of data.items ?? []) {
            next[item.code] = { price: item.price, change_pct: item.change_pct };
          }
          // 取不到的也记成"查过了"（避免每次切板块都重打一遍这批代码）
          for (const code of missing) {
            if (!(code in next)) next[code] = { price: null, change_pct: null };
          }
          return next;
        });
      })
      .catch(() => { /* 行情是展示增强项：取不到就显示 "—"，不报错 */ });
    return () => { alive = false; };
  }, [activeSector, memberQuotes]);

  // 选中的板块被删掉 → 回落「自选」。不回落的话下拉的 value 指向一个不存在的
  // option，列表区会整块空白，看着像"板块连同里面的票一起没了"。
  useEffect(() => {
    if (source.kind === "sector"
        && !sectorsApi.sectors.some((sector) => sector.id === source.id)) {
      setSource({ kind: "watch" });
    }
  }, [source, sectorsApi.sectors]);

  /** 点左侧列表任一行 → 切到该票（自选行与板块成分行共用同一套动作）。 */
  const switchTo = useCallback((target: string) => {
    setInputCode(target);
    setCode(target);
    setShowBacktest(false);
    setBacktest(null);
    // 窄屏下选完就把列表收起来，避免它一直盖着图
    if (window.innerWidth < 1180) setDrawerOpen(false);
  }, []);

  /** 就地新建板块：手工成分股，或按流通市值/行业由服务端求值的规则板块。 */
  const createSector = useCallback(async () => {
    const name = newSector.name.trim();
    if (!name) {
      showNotice("板块名不能为空", "warn");
      return;
    }
    try {
      const rule = newSector.kind === "dynamic" ? {
        min_circ_mv: newSector.minMv ? Number(newSector.minMv) * 1e8 : null,
        max_circ_mv: newSector.maxMv ? Number(newSector.maxMv) * 1e8 : null,
        industries: newSector.industries.split(/[,，\s]+/).filter(Boolean),
        exclude_st: true,
      } : {};
      const saved = await sectorsApi.save({ name, kind: newSector.kind, rule });
      setNewSector({
        name: "", kind: "manual", minMv: "20", maxMv: "150", industries: "",
      });
      setCreating(false);
      setSource({ kind: "sector", id: saved.id });
      showNotice(`板块已保存：${name}`);
    } catch (exc) {
      showNotice(`保存板块失败：${String(exc)}`, "warn");
    }
  }, [newSector, sectorsApi, showNotice]);

  const addMembersToActive = useCallback(async () => {
    if (activeSector === null) return;
    // 解析**与顶部「加自选」共用同一个 `resolveStockTokens`**（用户口径 2026-09-23：
    // "加成分股复用顶部那个加自选功能，只是加进当前自定义板块"）。
    // 6 位数字直通、其余按名字模糊匹配、认不出的明确报错且**绝不入库**。
    const { items, unresolved } = await resolveStockTokens(memberDraft);
    if (unresolved.length > 0) {
      showNotice(`认不出这些输入，未加入：${unresolved.join("、")}`
        + `（请填 6 位代码，或写完整股票名）`, "warn");
    }
    if (items.length === 0) return;
    try {
      const result = await sectorsApi.addMembers(activeSector.id, items);
      setMemberDraft("");
      showNotice(result.added > 0
        ? `已加入 ${result.added} 只（重复的自动跳过）`
        : "没有新增（这些代码都已在板块里）");
    } catch (exc) {
      showNotice(`加入成分失败：${String(exc)}`, "warn");
    }
  }, [activeSector, memberDraft, sectorsApi, showNotice]);

  const deleteActiveSector = useCallback(async () => {
    if (activeSector === null) return;
    if (!window.confirm(`删除自定义板块「${activeSector.name}」？其成分股不会被删掉。`)) {
      return;
    }
    try {
      await sectorsApi.remove(activeSector.id);
      setSource({ kind: "watch" });
      showNotice(`已删除板块：${activeSector.name}`);
    } catch (exc) {
      showNotice(`删除板块失败：${String(exc)}`, "warn");
    }
  }, [activeSector, sectorsApi, showNotice]);

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
        <span className="intraday-title">个股行情</span>
        {/* 「量化选股」是**组合级模块**（顶部的同级页签，见 QuantTabContainer）：
            它不针对某一只票，因此这里把整排个股级操作
            （选股/自选增删/板块/强制刷新/阈值回测/权重编辑/推送状态）
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
          listId={BOARD_HISTORY_ID}
        />
        {/* 历史配置联想（用户口径 2026-09-23）：数据是**用户自己配过的值**，
            去重后按出现顺序排（不排序 —— 最近用的通常就是最可能再用的）。
            `关联板块` 那个 datalist 挂在 `BoardPicker` 内部的 input 上（原生补全）；
            海外映射本来是裸 input，这里直接加。 */}
        <datalist id={BOARD_HISTORY_ID}>
          {historyBoards.map((name) => <option key={name} value={name} />)}
        </datalist>
        <input className="overseas-input" value={overseasInput}
               list={OVERSEAS_HISTORY_ID}
               onChange={(e) => setOverseasInput(e.target.value)}
               placeholder="海外映射(可选) usNVDA,kr000660"
               title="加自选时一并写入海外映射；留空则回落到所属板块的默认映射" />
        <datalist id={OVERSEAS_HISTORY_ID}>
          {historyOverseas.map((code) => <option key={code} value={code} />)}
        </datalist>
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
            {sectorsApi.available ? (
              <select className="watch-source" value={sourceKey}
                      onChange={(event) => {
                        const value = event.target.value;
                        setMemberDraft("");
                        setCreating(false);
                        setSource(value === "watch"
                          ? { kind: "watch" }
                          : { kind: "sector", id: Number(value.slice(7)) });
                      }}
                      title="左侧列表显示哪个板块：自选池，或某个自定义板块">
                <option value="watch">自选（{watch.length}）</option>
                {sectorsApi.sectors.map((sector) => (
                  <option key={sector.id} value={`sector:${sector.id}`}>
                    {sector.name}（{sector.member_count}）
                  </option>
                ))}
              </select>
            ) : (
              <>
                <b>自选</b>
                <span className="muted-text">（{watch.length}）</span>
              </>
            )}
            {sectorsApi.available && (
              <button className="btn-ghost tiny"
                      onClick={() => setCreating((value) => !value)}
                      title="新建自定义板块（就地建，不用切到量化选股页签）">＋</button>
            )}
            {activeSector === null && (
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
            )}
            {/* 「口径」：打开**当前这只票**的阈值/权重/档位编辑。
                这个入口原来只挂在「我的自选池」面板里（点某只票的「口径」），
                而那个面板已删除（用户口径 2026-09-23："很鸡肋，不需要了"）。
                口径本身是要保留的能力，所以把它挪到这里 ——
                位置与"当前这只票"一致：你正在看哪只票，改的就是哪只票的。 */}
            {onEditProfile && code && (
              <button className="btn-ghost tiny" onClick={() => onEditProfile(code)}
                      title={`编辑 ${code} 的个股口径（阈值 / 权重 / 档位），只影响你自己`}>
                ⚙ 口径
              </button>
            )}
            <button className="btn-ghost tiny" disabled={watchBusy || sectorsApi.busy}
                    onClick={() => (activeSector === null
                      ? void loadWatch(true, code)
                      : void sectorsApi.reload())}
                    title={activeSector === null
                      ? "忽略缓存立即重算全部自选（盘中本来每分钟自动刷新，这里用于临时催一次）"
                      : "重新读取该板块的成分股"}>
              ↻ 刷新
            </button>
            <button className="btn-ghost tiny drawer-hide" onClick={toggleDrawer}
                    title="收起列表">‹</button>
          </div>
          {/* 板块视图的状态条：说明这个板块是手工还是规则、多少只成分 */}
          {activeSector !== null && (
            <div className="watch-refresh drawer-status"
                 title={activeSector.kind === "dynamic"
                   ? `规则板块（服务端实时求值）：${JSON.stringify(activeSector.rule)}`
                   : `手工板块：${activeSector.member_count} 只成分`}>
              {activeSector.kind === "dynamic" ? "⚙ 规则板块" : "✎ 手工板块"}
              {" · "}{activeSector.member_count} 只成分
              {activeSector.kind === "dynamic" ? "（服务端按规则求值）" : ""}
              {activeSector.note ? ` · ${activeSector.note}` : ""}
            </div>
          )}
          {/* 板块接口读不到时只提示，不隐藏板块 UI（可能只是网络抖动） */}
          {sectorsApi.error !== "" && (
            <div className="watch-refresh drawer-status" title={sectorsApi.error}>
              ⚠ 板块列表读取失败（自选不受影响）
            </div>
          )}
          {/* 报价与打分是两条节奏，必须分开写：否则用户会把 60 秒前的信号当成此刻的信号 */}
          {activeSector === null && (
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
          )}

          {/* 新建板块：就地展开，不跳页签 */}
          {creating && (
            <div className="drawer-form">
              <input className="drawer-input" placeholder="板块名（如：我的半导体）"
                     value={newSector.name}
                     onChange={(event) => setNewSector((current) => ({
                       ...current, name: event.target.value,
                     }))}
                     onKeyDown={(event) => {
                       if (event.key === "Enter") void createSector();
                     }} />
              <select className="drawer-input" value={newSector.kind}
                      onChange={(event) => setNewSector((current) => ({
                        ...current,
                        kind: event.target.value === "dynamic" ? "dynamic" : "manual",
                      }))}
                      title="手工=自己维护成分股；规则=服务端按市值/行业实时求值">
                <option value="manual">手工成分股</option>
                <option value="dynamic">规则（市值/行业）</option>
              </select>
              {newSector.kind === "dynamic" && (
                <>
                  <div className="drawer-form-row">
                    <span className="muted-text">流通市值</span>
                    <input className="drawer-input mono" value={newSector.minMv}
                           onChange={(event) => setNewSector((current) => ({
                             ...current, minMv: event.target.value,
                           }))} />
                    <span className="muted-text">~</span>
                    <input className="drawer-input mono" value={newSector.maxMv}
                           onChange={(event) => setNewSector((current) => ({
                             ...current, maxMv: event.target.value,
                           }))} />
                    <span className="muted-text">亿</span>
                  </div>
                  <input className="drawer-input" placeholder="行业含：半导体,医药"
                         value={newSector.industries}
                         onChange={(event) => setNewSector((current) => ({
                           ...current, industries: event.target.value,
                         }))} />
                </>
              )}
              <div className="drawer-form-row">
                <button className="btn-ghost tiny primary" disabled={sectorsApi.busy}
                        onClick={() => void createSector()}>
                  {sectorsApi.busy ? "保存中…" : "保存板块"}
                </button>
                <button className="btn-ghost tiny" onClick={() => setCreating(false)}>
                  取消
                </button>
              </div>
            </div>
          )}

          {/* 选中板块后的管理：加成分股 / 删除板块 */}
          {activeSector !== null && (
            <div className="drawer-form">
              {activeSector.kind === "manual" && (
                <input className="drawer-input"
                       placeholder="加成分股：6位代码或股票名（可逗号/空格分隔）"
                       title="支持一次加多只：用逗号、空格、顿号或分号分隔。6 位数字按代码识别；其余按股票名/拼音模糊匹配，认不出的不会加入（绝不会把文字当成代码存进板块）"
                       value={memberDraft}
                       onChange={(event) => setMemberDraft(event.target.value)}
                       onKeyDown={(event) => {
                         if (event.key === "Enter") void addMembersToActive();
                       }} />
              )}
              <div className="drawer-form-row">
                {activeSector.kind === "manual" && (
                  <button className="btn-ghost tiny" disabled={sectorsApi.busy}
                          onClick={() => void addMembersToActive()}>
                    ＋ 加入板块
                  </button>
                )}
                <button className="btn-ghost tiny danger" disabled={sectorsApi.busy}
                        onClick={() => void deleteActiveSector()}>
                  删除板块
                </button>
              </div>
            </div>
          )}

          <ul className="watch-list" ref={watchListRef}>
            {activeSector === null ? (
              <>
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
                        onClick={() => switchTo(item.code)}>
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
                  <ValuationTag item={item} />
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
              </>
            ) : (
              <>
                {activeSector.members.length === 0 && (
                  <li className="muted-text watch-empty">
                    {activeSector.kind === "dynamic"
                      ? "规则板块 —— 成分由服务端按市值/行业规则实时求值"
                      : "这个板块还没有成分股：用上面的输入框加，或到"
                        + "「量化选股 → 选股结果」点「＋ 加入板块」"}
                  </li>
                )}
                {/* 板块成分**复用自选行的设计**（用户口径 2026-09-23）：名称 + 代码 +
                    涨跌幅；若这只票**同时也在自选池里**，连「分 / 信号」一起显示 ——
                    那份数据是现成的，没必要再算一遍。
                    行情来自**按代码共享**的 `memberQuotes`（同一只票在多个板块里只抓一次）。 */}
                {activeSector.members.map((member) => {
                  const inWatch = watch.find((item) => item.code === member.code);
                  const pct = inWatch?.change_pct
                    ?? memberQuotes[member.code]?.change_pct ?? null;
                  return (
                    <li key={member.code}
                        data-code={member.code}
                        className={`watch-row${member.code === code ? " active" : ""}`}>
                      <button className="watch-pick"
                              title={`切换到 ${member.name || member.code}`}
                              onClick={() => switchTo(member.code)}>
                        <span className="watch-name">{member.name || member.code}</span>
                        <span className="watch-code mono muted-text">{member.code}</span>
                        <span className="watch-chg mono" style={{
                          color: (pct ?? 0) >= 0 ? "var(--low)" : "var(--high)",
                        }}>
                          {pct === null || pct === undefined
                            ? "—" : `${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%`}
                        </span>
                        {inWatch !== undefined && (
                          <span className="watch-score mono muted-text">
                            {inWatch.total_score === null
                              || inWatch.total_score === undefined ? ""
                              : `分 ${inWatch.total_score >= 0 ? "+" : ""}`
                                + `${inWatch.total_score.toFixed(0)}`}
                          </span>
                        )}
                        {inWatch !== undefined && <SignalBadge item={inWatch} />}
                        {inWatch !== undefined && <ValuationTag item={inWatch} />}
                      </button>
                      {activeSector.kind === "manual" && (
                        <button className="watch-remove" disabled={sectorsApi.busy}
                                title={`从板块移除 ${member.code}`}
                                onClick={() => void sectorsApi.removeMember(
                                  activeSector.id, member.code)}>×</button>
                      )}
                    </li>
                  );
                })}
              </>
            )}
          </ul>
        </aside>
        {/* 窄屏时的点击遮罩：点一下就把抽屉收起来 */}
        <button className="drawer-backdrop" aria-label="收起自选列表"
                onClick={() => setDrawerOpen(false)} />

        <div className="intraday-main">
          {/* 抬头与「周期」并排一行（用户口径 2026-09-23）：
              原来它们是上下两行，在 1080p 上白吃掉一整行高度、把下面的图往下推。
              现在抬头占满剩余宽度，「周期」条贴在它**右侧**；窄屏放不下时自动换行回落。 */}
          <div className="intraday-head-row">
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
                    回踩 {snapshot.levels.low_buy.toFixed(2)} ·
                    冲高 {snapshot.levels.high_sell.toFixed(2)} ·
                    止损 {snapshot.levels.stop_loss.toFixed(2)}
                  </span>
                )}
                {quote?.turnover_rate !== null && quote?.turnover_rate !== undefined && (
                  <span className="muted-text">换手 {quote.turnover_rate.toFixed(2)}%</span>
                )}
              </div>
            )}

          {/* 做T辅助（本模块）的**周期口径**切换。
              ⚠️ 位置：2026-09-23 起与个股抬头**并排一行、贴在它右侧**（用户口径），
              不再是"紧贴走势图上方"的独立一行 —— 那条独立行在 1080p 上白占高度。
              它决定下面整块图与打分是"分钟级"还是"日线级"。
              标签用「周期」而不是「走势图口径」：这是用户的原话口径，
              而且它切换的其实是整个面板的计算周期（含档位与信号），不只是图。
              ⚠️ 这里**只剩做T的两种口径**：「量化选股」「竞价选股」是与做T辅助
              同级的**模块**，已移到页面顶部并列（2026-09-23，见 QuantTabContainer）。
              所以本组件里 `mode === "select"` 现在只可能来自 `view` prop，
              进来时这条周期条整条隐藏，换成一句组合级说明。 */}
            <div className="mode-bar">
              {/* 这条「周期」条只属于**做T辅助**这一个模块：
                  「量化选股」「竞价选股」是和它**同级**的模块，2026-09-23 起
                  已经移到页面顶部并列（见 QuantTabContainer 的 .quant-modules）。
                  切到「量化选股」模块时整条隐藏 —— 日内分时/日K 不作用于组合级
                  选股，留着就是"点了没反应"的死按钮。 */}
              {mode === "select" ? (
                <span className="muted-text">
                  （组合级选股：不针对当前这一只票，结果需手动点「＋加自选」）
                </span>
              ) : (
                <>
                  <span className="muted-text">周期</span>
                  <div className="mode-switch">
                    <button className={mode === "intraday" ? "mode-btn active" : "mode-btn"}
                            onClick={() => setTMode("intraday")}
                            title="分时做T：分钟级多因子打分 + ±提示线/动手线 阈值信号">
                      分时
                    </button>
                    <button className={mode === "daily" ? "mode-btn active" : "mode-btn"}
                            onClick={() => setTMode("daily")}
                            title="日K：蜡烛柱状图 + 擒牛线（NML/QRL/CBX20/CBX60/SMX）+ 量柱体系 + 高量柱攻防 + B1-B15/S1-S6 战法；均线已去掉。注意：日K模式的档位由量价体系自算，止损/保护线不走分时档位">
                      日K
                    </button>
                  </div>
                  {/* ⚠️ 「市场情绪周期」展示条**2026-09-23 按用户要求删除**
                      （原位置就在「周期」两个按钮右侧，见 git 历史）。
                      注意**只删了日内分时这一处**：权重编辑弹窗
                      （`WeightProfileEditor`）里那条同源的情绪周期仍然保留。
                      「（日K模式的档位由量价体系自算…）」那句长提示同日**收进日K按钮的
                      tooltip**：它摆在行内会把这条并排的 row 挤到第二行（实测 506px），
                      而用户的口径是"抬头与周期必须在同一行"。 */}
                </>
              )}
            </div>
          </div>

          {mode === "select" ? (
        // 量化选股面板是私有商业版资产、不进公开仓库，所以经 privatePanels 解析：
        // 本地拿到真实面板，公开仓库拿到 null 退化成占位提示（详见 src/privatePanels.ts）。
        // `onAdded` 就是上面那个 revealAdded —— 一键加自选后把新票滚进视野并高亮。
        QuantSelectPanel !== null ? (
          <QuantSelectPanel onAdded={revealAdded}
                            onSectorsChanged={sectorsApi.reload} />
        ) : (
          <PrivateFeatureNotice feature="量化选股（3档模型定时选股）" />
        )
      ) : mode === "daily" ? (
        <IntradayDailyPanel code={code} />
      ) : (
        <>
          <MarketContextStrip snapshot={snapshot} />

          {/* 用户口径 2026-09-23：「① 估值空间」整块面板**从主区域移除**，
              结论收成左侧自选列表行里的一个标签（见上面的 `ValuationTag`）。
              因此这里不再有左列，「② 分时图与提示点」独占整行宽度
              —— 网格列定义见 styles.css 的 `.intraday-top`。 */}
          <div className="intraday-top">
            <section className="panel intraday-panel chart-panel">
              <div className="panel-head">
                <h2>② 分时图与提示点</h2>
                <span className="muted-text">
                  三角=做T信号（实心正式/空心软提示），虚线=冲高·回踩·止损·箱体
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
                              {stat.direction === "short" ? "冲高" : "回踩"}±{stat.threshold}
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
                              <td>{trade.exit_reason === "high_sell" ? "冲高"
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
        // 用户口径 2026-09-23：这个区域**只保留实测延迟表**（组件内部即那张表）。
        // 原来的「数据健康度（绝不使用模拟数据…）」标题、三源选中行
        // （分钟K线/日线/快照）、attempts 芯片行、health.gaps 缺口清单、
        // 以及 `snapshot.disclaimer` 全部删除。
        // ⚠️ 情绪周期的三条「一票否决」缺口**不是**删在这里 —— 它们是服务端
        //    `service.py` 拼进 `health.gaps` 的，已在那边移除；结论改由顶部
        //    「市场环境」行右侧的 `.cycle-badge` 呈现（见 `MarketContextStrip`）。
        // 免责声明改为只在「日K」口径底部展示一次（见 `IntradayDailyPanel`）。
        <DataSourceHealth />
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
