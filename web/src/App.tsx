import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, api, TaskDetail, TraceDetail } from "./api";
import AccountMenu from "./components/AccountMenu";
import AdminMonitorPanel from "./components/AdminMonitorPanel";
import AdminPanel from "./components/AdminPanel";
import AdminPermissionPanel from "./components/AdminPermissionPanel";
import AdminTierPanel from "./components/AdminTierPanel";
import AgentChatView from "./components/AgentChatView";
import AgentTimeline from "./components/AgentTimeline";
import AlertBell from "./components/AlertBell";
import AlertsPanel from "./components/AlertsPanel";
import AlertToasts from "./components/AlertToasts";
import BacktestPanel from "./components/BacktestPanel";
import FundFlowPanel from "./components/FundFlowPanel";
import IntelPanel from "./components/intel/IntelPanel";
import LoginScreen from "./components/LoginScreen";
import MainlinePanel from "./components/MainlinePanel";
import QuantTabContainer from "./components/QuantTabContainer";
import ErrorBoundary from "./components/ErrorBoundary";
import KeepAlive from "./components/KeepAlive";
import MetricsPanel from "./components/MetricsPanel";
import ReportView from "./components/ReportView";
import SchedulerPanel from "./components/SchedulerPanel";
import ServerStatusBanner from "./components/ServerStatusBanner";
import StockProfilePanel from "./components/StockProfilePanel";
import TracePanel from "./components/TracePanel";
import { loadAgentMeta } from "./agentMeta";
import { useAlertsWs } from "./hooks/useAlertsWs";
import { readRoute, writeRoute } from "./route";
import { useAuth } from "./hooks/useAuth";
import { useFeatures } from "./hooks/useFeatures";

/**
 * 页签按钮（小工具组件）。
 *
 * `show=false` 时**不渲染** —— 页签清单由 `visible_views` 决定，
 * 所以"关掉功能"在界面上就是"页签消失"，而不是"点进去 403"。
 * 也顺手统一了 `white-space: nowrap`，避免中文标签折行。
 */
function TL({ label, active, onClick, show = true, tone = "" }: {
  label: string;
  active: boolean;
  onClick: () => void;
  show?: boolean;
  tone?: "admin" | "";
}) {
  if (!show) return null;
  const cls = ["tab", active ? "active" : "", tone === "admin" ? "tab-admin" : ""]
    .filter(Boolean).join(" ");
  return (
    <button className={cls} onClick={onClick} title={label}>{label}</button>
  );
}

const ANALYSIS_TYPES = [
  { value: "macro", label: "宏观" },
  { value: "industry", label: "行业" },
  { value: "stock", label: "个股" },
  { value: "full", label: "综合" },
];

/**
 * 可写进 URL 的页签白名单（与 `view` 的联合类型一一对应）。
 *
 * ## 为什么要有它（用户口径 2026-09-24）
 *
 * 用户："全都用 http://127.0.0.1:8100/，会导致刷新网页时总是跳到默认首页"。
 * 本应用**没有路由**，页签只活在 React state 里 —— 一刷新就回到默认页。
 * 正解不是"每个子界面开子域名"（那要重复部署、还要各自处理登录态与 CORS），
 * 而是**把当前页签写进 URL 的 hash**：刷新、收藏、发链接都能回到同一页，
 * 且**不需要后端配合**（hash 不会发给服务器，静态托管也不会 404）。
 *
 * 白名单的作用是**只认已知页签**：脏 hash（用户手改、旧链接）忽略掉，
 * 不能让它直接进 `setView`（那会把界面切到不存在的分支 → 白屏）。
 */
const HASH_VIEWS = [
  "research", "scheduler", "metrics", "backtest", "alerts", "intraday",
  "mainline", "fundflow", "mypools",
  // 用户口径（2026-09-25）：删除一级「情报中心」，把它的两个子页签
  // 提为一级。所以 hash 里出现的是这两项，而不再是 `intel`。
  "intel-hot", "intel-calendar",
  "admin", "admin-monitor", "admin-tiers", "admin-perms",
] as const;

export default function App() {
  const auth = useAuth();
  // 认证状态机：checking → 显示"正在检查登录状态"，避免闪一下登录页
  // （用户已登录时看到登录页一闪，会以为自己的登录丢了）。
  if (auth.phase === "checking") {
    return (
      <div className="auth-shell">
        <div className="auth-card auth-checking">
          <span className="spinner" /> 正在检查登录状态…
        </div>
      </div>
    );
  }
  if (auth.phase === "anonymous" || !auth.user) {
    // `notice` 是"被退回登录页"的原因（如会话过期），如实显示 —— 否则用户会以为
    // 自己点错了按钮，而实际是掉线（见 web/src/unauthorized.ts 的说明）。
    return <LoginScreen onLogin={auth.login} notice={auth.notice}
                        loginMode={auth.loginMode} />;
  }
  return <Workbench auth={auth} />;
}

type AuthApi = ReturnType<typeof useAuth>;

/**
 * 主工作台（已登录后渲染）。
 *
 * 从 `App` 里拆出来是**必须的**：`useAuth()` 与 `useAlertsWs()` 都是 Hook，
 * 若在同一个组件里先 `return <LoginScreen/>` 再调 `useAlertsWs()`，
 * 就等于"条件调用 Hook" —— React 会直接报 Hooks 顺序错误。
 * 拆成一个只在已登录时才渲染的组件，Hook 顺序天然稳定。
 */
function Workbench({ auth }: { auth: AuthApi }) {
  const [query, setQuery] = useState("当前宏观环境如何？对A股有什么含义？");
  const [analysisType, setAnalysisType] = useState("macro");
  const [target, setTarget] = useState("");
  const [task, setTask] = useState<TaskDetail | null>(null);
  const [trace, setTrace] = useState<TraceDetail | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 默认落地页 = 量化交易（用户口径 2026-09-18）。
  //
  // 为什么直接改默认值而不是做"记住上次"：本应用**没有**把 view 持久化到
  // localStorage/URL，所以每次打开都是全新挂载 —— 改 useState 初值就等于
  // "每次打开都默认显示量化交易"，语义完全吻合，不需要额外的持久化逻辑
  // （那种做法反而会让"想回到默认页"变得要清缓存）。
  //
  // ⚠️ 键名必须是 `"intraday"`：顶部导航的「量化交易」按钮绑的就是它
  // （`view === "intraday"` 判高亮）。写成 `"quant"` 之类会编译不过或落空。
  const [view, setView] =
    useState<"research" | "scheduler" | "metrics" | "backtest" | "alerts"
      | "intraday" | "mainline" | "fundflow"
      | "intel-hot" | "intel-calendar" | "admin"
      | "admin-monitor" | "admin-tiers" | "admin-perms">(
      "intraday");
  // 管理员也可以切到"业务视图"看租户侧界面（他是平台身份，但业务功能同样开放）。
  // 默认为 false：管理员登录后**先进系统管理**（那是他每天要做的事）。
  const [tenantView, setTenantView] = useState(false);


  const isAdmin = auth.user?.applied_tier === "admin";
  // 是否按管理员导航渲染。管理员点了「业务视图」后即为 false —— 此时他看到
  // 的是租户侧页签（且不受套餐限制，管理员本身含全部功能）。
  const adminNav = isAdmin && !tenantView;

  // ---- URL hash ⇄ 当前页签（刷新/收藏/分享都能回到同一页）----
  // 读：初始化 + 浏览器前进/后退 + 手改地址栏
  useEffect(() => {
    const apply = () => {
      const key = readRoute().view;
      if (!key || !(HASH_VIEWS as readonly string[]).includes(key)) return;
      const adminView = key.startsWith("admin");
      if (adminView && !isAdmin) return;      // 非管理员不认管理页签，留在原页
      if (isAdmin) setTenantView(!adminView); // 管理页签 ⇄ 业务视图的切换也跟着 URL
      setView(key as Parameters<typeof setView>[0]);
    };
    apply();
    window.addEventListener("hashchange", apply);
    return () => window.removeEventListener("hashchange", apply);
  }, [isAdmin]);

  // 写：页签变化回填地址栏。用 `replaceState` 而不是直接赋值 ——
  // 后者每个页签都塞一条历史，用户按"后退"要按十几次才能退出。
  useEffect(() => {
    // 保留子状态参数（code/mode/…）：切页签不该把它们清掉
    writeRoute(view);
  }, [view]);
  // 页签权限：由服务端下发。管理员走系统管理导航时不需要拉这个接口。
  const visibility = useFeatures(Boolean(auth.user) && !adminNav);
  const featuresFailed = visibility.failed;
  // 该用户能否看某个业务页签（管理员在业务视图下不受限）
  const showView = (v: string) =>
    adminNav ? false : (isAdmin ? true : visibility.isVisible(v));
  // 个股口径编辑的对象（空串 = 不显示面板）。
  // 做成"覆盖在任意页之上"而不是单独一个页签：口径是**从某只票出发**的动作
  // （做T面板里点「⚙ 口径」），做成页签会要求用户先切页再输代码。
  const [profileCode, setProfileCode] = useState("");
  const timer = useRef<number | null>(null);

  // 启动时拉取 agent 中文名/置信度中文映射（失败有本地兜底，不阻断渲染）
  useEffect(() => {
    void loadAgentMeta();
  }, []);

  // 事件告警：单一WS连接供铃铛未读数、Toast与告警面板共用
  // （同一条连接还带"情报流已就绪"的信令 `intelSeq` → 传给情报面板）
  const { connected, unread, incoming, intelSeq, refreshUnread } = useAlertsWs();
  const [toasts, setToasts] = useState<Alert[]>([]);
  const [incomingTick, setIncomingTick] = useState(0);
  const [openAlertId, setOpenAlertId] = useState<string | null>(null);

  useEffect(() => {
    if (!incoming) return;
    setIncomingTick((n) => n + 1);
    setToasts((prev) =>
      prev.some((t) => t.alert_id === incoming.alert_id)
        ? prev : [...prev, incoming].slice(-5));
    const id = incoming.alert_id;
    const timerId = window.setTimeout(
      () => setToasts((prev) => prev.filter((t) => t.alert_id !== id)),
      10000);
    return () => window.clearTimeout(timerId);
  }, [incoming]);

  const dismissToast = useCallback((alertId: string) => {
    setToasts((prev) => prev.filter((t) => t.alert_id !== alertId));
  }, []);

  /**
   * ★ 面板数据**保活预取**（用户报障 2026-09-26 第二次）。
   *
   * > "热点&研报小作文、事件告警 每次打开这个界面不能预加载到浏览器吗，
   * >   切界面还是会有 2 秒延迟…界面打开的时候应该立即能加载到。"
   *
   * 上一轮加的 `localStorage` 缓存本身是对的，但**缓存是冷的**：
   * 预加载原来只写在 `useAuth.login()` 里，而绝大多数访问是
   * **带 remember-me Cookie 刷新页面**（走 `probe()`，从不预取）。
   * 加上缓存有 TTL（告警 5 分钟 / 情报 10 分钟），放着就过期。
   *
   * 现在挂在**已登录**状态上：进页面就预取一次，之后每 4 分钟后台续期，
   * 让缓存在用户停留期间**永不过期** —— 于是点面板就是"挂载即有内容"。
   *
   * 放在 `AppInner`（已登录分支）而不是 `App`：那个组件在未登录时会
   * `return <LoginScreen/>`，此时预取毫无意义且会打 401。
   */
  useEffect(() => {
    let stop: (() => void) | null = null;
    let cancelled = false;
    void import("./panelPrefetch").then(({ startPanelKeepAlive }) => {
      if (cancelled) return;          // 组件已卸载：别留下没人清的定时器
      stop = startPanelKeepAlive();
    });
    return () => {
      cancelled = true;
      stop?.();
    };
  }, []);

  const openAlert = useCallback((alert: Alert) => {
    setOpenAlertId(alert.alert_id);
    setView("alerts");
    setToasts((prev) => prev.filter((t) => t.alert_id !== alert.alert_id));
  }, []);

  const stopPolling = () => {
    if (timer.current !== null) {
      window.clearInterval(timer.current);
      timer.current = null;
    }
  };

  const poll = useCallback((taskId: string) => {
    stopPolling();
    timer.current = window.setInterval(async () => {
      try {
        const detail = await api.task(taskId);
        setTask(detail);
        if (
          detail.status === "completed" ||
          detail.status === "failed" ||
          detail.status === "cancelled"
        ) {
          stopPolling();
          if (detail.status === "completed") {
            try {
              setTrace(await api.trace(taskId));
            } catch { /* trace可选 */ }
          }
        }
      } catch (e) {
        stopPolling();
        setError(String(e));
      }
    }, 2000);
  }, []);

  useEffect(() => stopPolling, []);

  const submit = async () => {
    setError(null);
    setSubmitting(true);
    setTask(null);
    setTrace(null);
    try {
      const resp = await api.submit({ query, analysis_type: analysisType, target });
      setTask({
        task_id: resp.task_id, trace_id: resp.task_id, status: "queued",
        conclusion: null, confidence: null, report: null, agent_messages: [],
        progress: "", errors: [], error: null, created_at: "",
      });
      poll(resp.task_id);
    } catch (e) {
      setError(`提交失败：${String(e)}。请确认后端已启动（uvicorn src.api.main:app --port 8100）`);
    } finally {
      setSubmitting(false);
    }
  };

  /**
   * 回首页（站名点击、登录后落地）。
   *
   * "首页"的定义与登录后的默认落地页一致：**量化交易**。
   * 管理员若正在系统管理里，先切回业务视图再落到首页 ——
   * 否则点了"回首页"却停在管理员控制台，与预期不符。
   */
  const goHome = () => {
    setProfileCode("");
    if (adminNav) setTenantView(true);
    setView("intraday");
  };

  const cancelTask = async () => {
    if (!task?.task_id) return;
    setCancelling(true);
    try {
      await api.cancelTask(task.task_id);
      stopPolling();
      setTask((prev) => prev ? { ...prev, status: "cancelled" } : prev);
    } catch (e) {
      setError(`停止失败：${String(e)}`);
    } finally {
      setCancelling(false);
    }
  };

  /**
 * 站标图形（内联 SVG，不依赖网络字体或图片）。
 *
 * 为什么用内联 SVG 而不是 emoji 或图片：emoji 在各平台渲染差异很大
 * （Windows 上是彩色字形、Linux 可能是黑白），而图片会多一次请求、
 * 且在深色/浅色主题下不好适配。SVG 用 `currentColor` 自动跟随主题色。
 */
function BrandMark() {
  return (
    <svg viewBox="0 0 24 24" width="22" height="22" fill="none"
      stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"
      strokeLinejoin="round">
      {/* 折线 = 行情走势；外框 = 工作台 */}
      <rect x="2.5" y="3.5" width="19" height="17" rx="2.5" />
      <path d="M5.5 15.5 9 11l3 2.5L18.5 7" />
      <circle cx="18.5" cy="7" r="1.3" fill="currentColor" stroke="none" />
    </svg>
  );
}

const running = task !== null && (task.status === "queued" || task.status === "running");

  return (
    <div className={view === "intraday" || view === "fundflow"
      || view === "mainline" || view === "intel-hot"
      || view === "intel-calendar" ? "app app-wide" : "app"}>
      <header className="header">
        {/* ★ 站名可点击回首页（用户口径 2026-09-23）。
            用 `<button>` 而不是 `<a href>`：本应用没有路由，用锚点会真的
            触发一次页面跳转（丢掉当前登录态与页内状态）。做成按钮 +
            回到"首页视图"，语义等价且不丢状态。
            `title`/`aria-label` 让它对键盘与读屏也可用。 */}
        <button className="brand" onClick={goHome}
          title="回到首页" aria-label="回到首页">
          <span className="brand-mark" aria-hidden="true">
            <BrandMark />
          </span>
          <span className="brand-text">
            <span className="brand-name">Moss-FinAgent-Research</span>
            <span className="brand-sub">多Agent投研工作台 · 全链路可溯源</span>
          </span>
        </button>
        {/* ★ 两套导航**互斥**（管理员看不到租户业务页签，反之亦然）。
            为什么要分开（用户口径 2026-09-23）：
              1. 权限模型不同 —— 管理员管的是"平台与人"，租户用的是"业务功能"，
                 混在一排会让人以为管理员也能用租户功能（那是另一套鉴权）；
              2. 页签太多会折行变丑，且管理员最常用的"待审批"被埋在最右。
            管理员通过账户菜单里的「切换到业务视图」仍可进入业务界面。 */}
        {adminNav ? (
          <nav className="tabs tabs-admin">
            {/* 这里原来有一个 `<span class="nav-group-label">系统管理</span>`
                作为分组标签。用户口径 2026-09-23：**删掉** ——
                它长得像页签却点不动（标签被做成了带边框的方框），
                既不提供信息也不提供动作，是纯噪声。 */}
            <TL label="用户管理" active={view === "admin"}
              tone="admin" onClick={() => setView("admin")} />
            <TL label="资源监控" active={view === "admin-monitor"}
              tone="admin" onClick={() => setView("admin-monitor")} />
            <TL label="资源管控" active={view === "admin-tiers"}
              tone="admin" onClick={() => setView("admin-tiers")} />
            <TL label="功能权限" active={view === "admin-perms"}
              tone="admin" onClick={() => setView("admin-perms")} />
            <TL label="业务视图" active={false}
              onClick={() => {
                setTenantView(true);
                // 业务视图的落地页取"他套餐里第一个可用页签"，
                // 而不是固定 intraday —— 万一管理员档被关了量化交易，
                // 切过去就是一片 403。
                setView(visibility.isVisible("intraday") || isAdmin
                  ? "intraday" : "research");
              }} />
          </nav>
        ) : (
          <nav className="tabs">
            {featuresFailed && (
              <span className="nav-warn"
                title="页签清单按套餐权限由服务端下发；读取失败时只显示最小集合">
                权限读取失败
              </span>
            )}
            {/* 管理员的"回系统管理"不放这里（用户口径 2026-09-23：
                业务视图里不要出现管理员相关入口）—— 走右上角头像菜单。 */}
            {/* ★ 页签清单来自服务端（`visible_views`），不在这里硬编码。
                管理员关掉某功能 → 该等级用户的这个页签**直接消失**。 */}
            <TL label="投研分析" active={view === "research"}
              show={showView("research")}
              onClick={() => setView("research")} />
            <TL label="调度管理" active={view === "scheduler"}
              show={showView("scheduler")}
              onClick={() => setView("scheduler")} />
            <TL label="运行指标" active={view === "metrics"}
              show={showView("metrics")}
              onClick={() => setView("metrics")} />
            <TL label="策略回测" active={view === "backtest"}
              show={showView("backtest")}
              onClick={() => setView("backtest")} />
            <TL label="量化交易" active={view === "intraday"}
              show={showView("intraday")}
              onClick={() => setView("intraday")} />
            {/* 主线挖掘：与「资金流监控」同级的顶级页签，位置固定在
                「量化交易」右侧、「资金流监控」左侧（用户口径 2026-09-20）。 */}
            <TL label="主线挖掘" active={view === "mainline"}
              show={showView("mainline")}
              onClick={() => setView("mainline")} />
            <TL label="资金流监控" active={view === "fundflow"}
              show={showView("fundflow")}
              onClick={() => setView("fundflow")} />
            {/* ── 舆情情报：**两个顶级页签** ──
                用户口径（2026-09-25）："删除当前的一级目录里的'情报中心'，
                并将二级目录页签'热点&研报小作文'、'投资日历'改为一级目录"。

                所以原来那个「情报中心」顶级页签没有了，它的两个子页签
                各自成为一级入口。两者**共用同一个功能 key**（`intel.hot`）——
                见 `my_features.py` 的 `VIEW_FEATURE`：一个售卖项可以对应
                多个页签，它们本来就是同一份公开信息的两种看法
                （"现在在说什么" vs "接下来会发生什么"）。 */}
            <TL label="热点&研报小作文" active={view === "intel-hot"}
              show={showView("intel-hot")}
              onClick={() => setView("intel-hot")} />
            <TL label="投资日历" active={view === "intel-calendar"}
              show={showView("intel-calendar")}
              onClick={() => setView("intel-calendar")} />
            {/* 事件告警不属于"售卖功能"：它是每个登录用户的基础能力 */}
            <TL label="事件告警" active={view === "alerts"}
              onClick={() => setView("alerts")} />
            {/* ★ 「我的自选池」顶层页签**已删除**（用户口径 2026-09-23）：
                加自选/删自选/每只票的阈值与权重因子都能在**做T辅助**面板里
                完成（那里有「我的池/共享」数据源开关 + 逐股权重档案编辑器），
                顶层再挂一个页签是重复入口。
                「我的自选池」**整个功能已删除**（用户口径 2026-09-23：
                "很鸡肋，不需要了"）—— 加自选/删自选一直是直接写
                `configs/intraday.yaml` 这份共享清单，从来不经过那张个人池表；
                逐股阈值/权重因子在做T面板的「⚙ 口径」里编辑（保留）。 */}
          </nav>
        )}
        <AlertBell unread={unread} connected={connected}
          onClick={() => setView("alerts")} />
        {/* 账户区放最后（顶栏最右）。
            ★ 用户口径 2026-09-23：**不要**再把"系统管理员"这类身份徽标挂在
            业务视图顶栏上 —— 顶栏只该是业务页签 + 一个很小的用户头像。
            身份、套餐、系统管理入口、设备、退出全部收进头像菜单里
            （`compact` 模式下只显示一个圆形头像）。 */}
        {auth.user && (
          <AccountMenu user={auth.user} compact
            isAdmin={!!isAdmin} inAdminView={adminNav}
            onEnterAdmin={() => { setTenantView(false); setView("admin"); }}
            onExitAdmin={() => setTenantView(true)}
            onLogout={auth.logout} onReload={auth.reload} />
        )}
      </header>

      {/* 后端可达性横幅（连不上时才出现）。
          放在 header 之后、面板之前：任何页签下的 "Failed to fetch"
          都能在这里找到一句人话解释，而不是只能靠反复点击试。 */}
      <ServerStatusBanner />

      {/* 错误边界：一个面板渲染崩了只该坏那一块，不该把整页卸载成白屏
          （2026-09-21 竞价选股点详情崩成白页的教训）。`resetKey={view}`
          让切页签自动恢复。 */}
      <ErrorBoundary resetKey={view} label={view}>
        {view === "admin" && auth.user ? (
          <AdminPanel selfId={auth.user.user_id} />
        ) : view === "admin-monitor" ? (
          <AdminMonitorPanel />
        ) : view === "admin-tiers" ? (
          <AdminTierPanel />
        ) : view === "admin-perms" ? (
          <AdminPermissionPanel />
        ) : view === "scheduler" ? (
          <SchedulerPanel />
        ) : view === "metrics" ? (
          <MetricsPanel />
        ) : view === "backtest" ? (
          <BacktestPanel />
        ) : view === "intraday" ? (
          <QuantTabContainer onEditProfile={setProfileCode} />
        ) : view === "mainline" ? (
          <MainlinePanel />
        ) : view === "fundflow" ? (
          <FundFlowPanel />
        ) : view === "intel-hot" || view === "intel-calendar"
            || view === "alerts" ? (
          // 这三个视图由下面**常驻的 `<KeepAlive>`** 承载（见那里的说明）。
          // 这里返回 null：三元链的语义是"只渲染命中的那一个"，切走即卸载，
          // 而这两个面板是用户反复来回切的，需要保活。
          null
        ) : (
          <div className="error-box">
            未知视图：{view}（请刷新页面；若持续出现请反馈）
          </div>
        )}
      </ErrorBoundary>

      {view === "research" && (
      <>
      <section className="submit-bar">
        <input
          className="query-input"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="输入投研问题…"
          disabled={running}
        />
        <select value={analysisType} onChange={(e) => setAnalysisType(e.target.value)} disabled={running}>
          {ANALYSIS_TYPES.map((t) => (
            <option key={t.value} value={t.value}>{t.label}</option>
          ))}
        </select>
        <input
          className="target-input"
          value={target}
          onChange={(e) => setTarget(e.target.value)}
          placeholder="标的（如 600519）"
          disabled={running}
        />
        <button onClick={submit} disabled={running || submitting || !query.trim()}>
          {running ? "分析中…" : submitting ? "提交中…" : "开始分析"}
        </button>
        {running && (
          <button
            className="stop-btn"
            onClick={cancelTask}
            disabled={cancelling}
          >
            {cancelling ? "停止中…" : "停止"}
          </button>
        )}
      </section>

      {error && <div className="error-box">{error}</div>}

      {running && (
        <div className="progress-box">
          <span className="spinner" /> Supervisor已调度，Agent协作执行中（任务 {task?.task_id}）
          {task?.progress && <span className="progress-text">{task.progress}</span>}
        </div>
      )}

      {task?.status === "cancelled" && (
        <div className="info-box">任务已停止：{task.error ?? "用户主动取消"}</div>
      )}

      {task?.status === "failed" && (
        <div className="error-box">
          任务失败：{task.error ?? "未知原因"}
          {task.errors.length > 0 && (
            <ul>{task.errors.map((e, i) => <li key={i}>{e}</li>)}</ul>
          )}
        </div>
      )}

      {/* running时实时展示Agent协作对话（轮询agent_messages） */}
      {running && task?.agent_messages && task.agent_messages.length > 0 && (
        <AgentChatView messages={task.agent_messages} />
      )}

      {trace && (
        <div className="grid">
          <AgentTimeline outputs={trace.agent_outputs} errors={trace.errors} />
          <TracePanel trace={trace} />
        </div>
      )}

      {/* completed时展示完整Agent协作对话 */}
      {!running && task?.agent_messages && task.agent_messages.length > 0 && (
        <AgentChatView messages={task.agent_messages} />
      )}

      {task?.report && <ReportView markdown={task.report} />}
      </>
      )}

      {/* ★ 保活面板（用户报障 2026-09-26 第二次：
          "热点&研报小作文、事件告警 每次打开这个界面不能预加载到浏览器吗，
           切界面还是会有 2 秒延迟…界面打开的时候应该立即能加载到"）。

          ## 三个必须遵守的摆放约束

          1. **不放三元链里** —— 放进去 `KeepAlive` 自己也会被卸载，
             它的"曾可见过"标记一起归零，保活完全失效。
          2. **不在上面那个 `ErrorBoundary` 里** —— 它带 `resetKey={view}`，
             切页签会重置；常驻面板放进去，任何一次渲染错误都会把它们清掉，
             那正好抵消了保活。
          3. **各自包一个独立 ErrorBoundary** —— 这两个面板现在**同时挂载**
             （新增的耦合面），一个崩了不该连累另一个。

          `active` 决定显隐；首次切过去才真正挂载（惰性，见 KeepAlive）。 */}
      <ErrorBoundary resetKey="intel" label="情报流">
        <KeepAlive active={view === "intel-hot" || view === "intel-calendar"}>
          <IntelPanel isAdmin={!!isAdmin} intelSeq={intelSeq}
            section={view === "intel-calendar" ? "calendar" : "hot"} />
        </KeepAlive>
      </ErrorBoundary>
      <ErrorBoundary resetKey="alerts" label="事件告警">
        <KeepAlive active={view === "alerts"}>
          <AlertsPanel
            incomingTick={incomingTick}
            openAlertId={openAlertId}
            onConsumeOpen={() => setOpenAlertId(null)}
            onReadChanged={refreshUnread}
          />
        </KeepAlive>
      </ErrorBoundary>

      <AlertToasts alerts={toasts} onDismiss={dismissToast} onOpen={openAlert} />

      {/* 个股口径抽屉：从「自选池管理」里点某只票的「口径」打开。
          覆盖在页面之上而不是切页签 —— 用户改完口径要能立刻回到原来的池。 */}
      {profileCode && (
        <div className="profile-drawer-backdrop"
          onClick={() => setProfileCode("")}>
          <div className="profile-drawer" onClick={(e) => e.stopPropagation()}>
            <StockProfilePanel code={profileCode} />
            <button className="account-mini profile-drawer-close"
              onClick={() => setProfileCode("")}>关闭</button>
          </div>
        </div>
      )}
    </div>
  );
}
