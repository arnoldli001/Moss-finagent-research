/**
 * 面板数据**预取**（登录后 / 刷新页面后 / 保活续期）。
 *
 * ## 为什么需要这个模块（2026-09-26 用户报障，第二次）
 *
 * > "热点&研报小作文、事件告警 每次打开这个界面不能预加载到浏览器吗，
 * >   切界面还是会有 2 秒延迟，数据可以在本地服务器后台处理，界面打开的
 * >   时候应该立即能加载到或者提前把数据放到前端服务器。"
 *
 * 上一轮我给两个面板都加了 `localStorage` 缓存 + stale-while-revalidate，
 * 但**仍然会慢** —— 因为缓存是**空的**。根因有两条：
 *
 * ### ① 预加载只在"用户手动登录"那一个动作里触发
 *
 * `preloadAlerts` / intel 预取原来都写在 `useAuth.login()` 里。而绝大多数
 * 访问根本不是"登录"，而是**带着 remember-me Cookie 刷新页面** ——
 * 那条路径走的是 `probe()` → `authApi.bootstrap()`，它**从来不预取**。
 * 于是每次刷新页面后，缓存都是冷的，第一次点面板必然等一个完整往返。
 *
 * ### ② 缓存有 TTL（告警 5 分钟 / 情报 10 分钟），放着就过期
 *
 * 即使在登录时预取到了，用户只要在别的页面待够 TTL，切回来时缓存已经作废。
 * 在 ≈51 KB/s 的隧道上，一次冷取就是 2 秒级。
 *
 * ## 做法：把"预取"变成**常驻的保活**
 *
 * - 只要处于**已登录**状态就预取一次（不管是登录进来的还是刷新进来的）；
 * - 之后按 `KEEPALIVE_MS` **后台续期**，让缓存在用户停留期间**永不过期**；
 * - 续期用 `force` 绕过 `PRELOAD_FRESH_MS` 去重，否则第二次起会被判定
 *   "还新鲜"而直接返回、缓存永远不更新；
 * - 续期**不并发**（`inFlight` 去重）、**页面隐藏时跳过**
 *   （用户不在看，白花隧道带宽）、失败静默（预取是增益，不是功能）。
 *
 * ## 与"面板自己取数"的分工
 *
 * 这里只管**把数据放进缓存**，不碰任何 React 状态。
 * 面板挂载时照旧 `readAlerts/readIntelFeed` 立刻画、再后台核对 ——
 * 于是"预取成功"= 挂载即有内容，"预取失败"= 退回原来的行为（不会更差）。
 */

import { Alert, AlertSettings, api } from "./api";
import { preloadAlerts } from "./alertsCache";
import { feedCacheKeyOf, readIntelFeed, writeIntelFeed } from "./intelCache";
import { fetchIntelFeed } from "./intelApi";

/** 保活续期间隔。要**小于**两个缓存的 TTL（告警 5 分钟 / 情报 10 分钟），
 *  否则续期之间会出现"缓存刚好过期"的窗口 —— 那就白做了。
 *  取 4 分钟：比最短的 5 分钟 TTL 早 1 分钟续上。 */
export const KEEPALIVE_MS = 4 * 60 * 1000;

/**
 * 同一批预取的并发去重（多个入口同时触发时只跑一次）。
 *
 * ⚠️ 这里用**静态导入**而不是 `await import(...)`：
 * `intelCache` / `alertsCache` 本来就被面板静态导入（它们在主 chunk 里），
 * 再动态导入一次不会真的分包，反而会让 Vite 报
 * "dynamically imported but also statically imported" 警告 ——
 * 那是"看起来做了代码分割、其实没有"的假象。
 * 真正需要懒加载的是**本模块自身**（由 `App.tsx` 动态导入），
 * 因为它只在组件挂载后才需要。
 */
let inFlight: Promise<void> | null = null;

/** 记录每个目标的最近一次成功时刻（供 `resetPrefetchState` 与续期记账）。 */
const lastDone: Record<string, number> = {};

/**
 * 预取事件告警列表 + 设置（走 `/alerts/bootstrap`，**一次往返**取齐两者）。
 *
 * `force=false` 时受 `PRELOAD_FRESH_MS`（60 秒）去重保护 ——
 * 用于"用户刚登录/刚刷新"这种一次性的入口；
 * `force=true` 用于**保活续期**，必须真的重新取，否则缓存永远不更新。
 */
async function prefetchAlerts(force: boolean): Promise<void> {
  await preloadAlerts(() => api.alertsBootstrap({ limit: 100 }), { force });
}

/** 预取情报流（`/intel/feed?limit=60`），按面板的**初始档位**存键。 */
async function prefetchIntel(force: boolean): Promise<void> {
  const key = feedCacheKeyOf("all", "credibility", "all");
  if (!force) {
    const existing = readIntelFeed(key);
    // 已经有新鲜的就不重复取：多标签页各自预取时，否则每标签一份 62 KB。
    if (existing && Date.now() - existing.at < KEEPALIVE_MS) return;
  }
  const feed = await fetchIntelFeed({ limit: 60 });
  writeIntelFeed(key, feed);
}

/**
 * 预取两个面板的数据。**绝不抛** —— 预取失败只等于"回到优化前的行为"。
 *
 * @param force 绕过新鲜度去重，用于保活续期
 */
export async function prefetchPanels(force = false): Promise<void> {
  if (inFlight) return inFlight;
  inFlight = (async () => {
    // 两个目标互相独立：一个失败不该拖累另一个（各自 catch）。
    await Promise.allSettled([
      prefetchAlerts(force).then(
        () => { lastDone.alerts = Date.now(); },
        () => { /* 预取失败静默：面板自己会取 */ }),
      prefetchIntel(force).then(
        () => { lastDone.intel = Date.now(); },
        () => { /* 同上 */ }),
    ]);
  })();
  try {
    await inFlight;
  } finally {
    inFlight = null;
  }
}

/**
 * 启动**保活**：已登录时立刻预取一次，此后按 {@link KEEPALIVE_MS} 后台续期。
 *
 * 返回清理函数（组件卸载 / 登出时调用）。
 *
 * ## 为什么页面隐藏时跳过续期
 *
 * `document.visibilityState === "hidden"` 表示用户没在看这个页面
 * （切到别的标签、最小化）。此时续期纯属白花隧道带宽 ——
 * 而带宽正是这两个面板慢的根因。回到前台时会**立刻补一次**，
 * 所以不会因为跳过而拿到过期缓存。
 */
export function startPanelKeepAlive(): () => void {
  let stopped = false;
  let timer: number | null = null;

  const tick = async () => {
    if (stopped) return;
    try {
      // 页面不可见：跳过这一轮（回到前台会立刻补）。
      if (typeof document !== "undefined"
          && document.visibilityState === "hidden") return;
      await prefetchPanels(true);
    } catch {
      /* prefetchPanels 内部已 catch；这里是最后一道 */
    }
  };

  // ① 立刻预取一次（force=true 绕过 60 秒去重）：
  //    刚登录/刚刷新时缓存是冷的，这一次决定了用户点面板时**有没有东西可画**。
  void tick();

  timer = window.setInterval(() => { void tick(); }, KEEPALIVE_MS);

  // ② 回到前台立刻补一次：隐藏期间跳过的续期在这里补上。
  const onVisible = () => {
    if (document.visibilityState === "visible") void tick();
  };
  document.addEventListener("visibilitychange", onVisible);

  return () => {
    stopped = true;
    if (timer !== null) window.clearInterval(timer);
    document.removeEventListener("visibilitychange", onVisible);
  };
}

/** 退出登录时调用：清掉预取的记账，避免下一个人复用上一轮的"刚取过"。 */
export function resetPrefetchState(): void {
  lastDone.alerts = 0;
  lastDone.intel = 0;
  inFlight = null;
}

export type { Alert, AlertSettings };
