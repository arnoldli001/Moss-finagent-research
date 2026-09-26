/**
 * 情报流（热点&研报小作文）的**本地缓存**。
 *
 * ## 为什么需要它（2026-09-26 用户报障）
 *
 * > "热点&研报小作文、事件告警，这两个页面来回切，都会出现刚加载的信息界面，
 * >   还需要等 2-3 秒的延迟才展示数据，热加载这么慢吗？"
 *
 * 量出来的账：
 *
 *   1. **面板会被卸载重挂**。`App.tsx` 是 `view === "intel-hot" ? <IntelPanel/>`
 *      的三元链 —— 切到"事件告警"时 `IntelPanel` **整个卸载**，切回来是**全新挂载**，
 *      组件内所有 `useState`（包括 `feed`）全部归零。
 *   2. **挂载后必发一次 `/intel/feed?limit=60`**。实测该响应
 *      **raw 62.7 KB / gzip 17.0 KB**（60 条，`summary` 占 53%）。
 *      在公网隧道 ≈51 KB/s 上：**明文 1,224 ms**（gzip 后 332 ms）。
 *   3. 加上服务端 `refreshing` 时的"等 WS 通知再补拉"，用户看到的就是
 *      "刚加载的界面 + 再等 2~3 秒"。
 *
 * 对比：事件告警有 `alertsCache.ts`（stale-while-revalidate + 登录预加载），
 * 所以它切回来能**立刻画**；情报流**没有对应的东西** —— 这就是两者
 * 都慢但性质不同的原因。本模块补上这一层。
 *
 * ## 与 `alertsCache` 的一致设计
 *
 * - **localStorage 而不是内存**：内存缓存活不过一次刷新（F5），
 *   而"打开网站 → 点热点"正是要救的那条路。
 * - **按筛选档分开存**：`filter|sort|direction` 三者都进键 ——
 *   混在一起会让"切到高可信档"拿到"全部"的列表，看起来像筛选失效。
 * - **先画缓存、再后台核对**（stale-while-revalidate）：切回来立刻有内容，
 *   同时发请求核对；拿到新的再覆盖。**已画旧数据时不显示"加载中"** ——
 *   那会让"立刻可用"又退回"看起来在等"。
 * - **登出清空**：换个人在同一台机器登录，不该看到上一个人的情报。
 *
 * ## 与 `alertsCache` 的一处不同：`refreshing` 不入缓存
 *
 * 告警是"读一次就完"，而情报流有一个"服务端正在后台重建"的状态。
 * 把 `refreshing: true` 一起缓存下来的话，切回来会**立刻**又进入
 * "等待补拉"的轮询 —— 而那份数据其实已经够看了。
 * 所以缓存时**强制把 `refreshing` 置 false**，让切回来是"静默核对"语义。
 */

import { IntelFeed } from "./intelApi";

/** 缓存结构版本。改了字段含义就 +1，否则会读到旧结构的对象。 */
const VERSION = 1;

/** 缓存有效期。情报流是**分钟级**内容，10 分钟足够覆盖"来回切页签"的场景；
 *  再久就该老实走网络了 —— 情报的时效性比告警更强。 */
const MAX_AGE_MS = 10 * 60 * 1000;

const KEY_PREFIX = "moss.intel.feed.v1:";

export type IntelFeedSnapshot = {
  feed: IntelFeed;
  /** 这是哪一档筛选/排序/方向的结果 */
  cacheKey: string;
  at: number;
};

/** 筛选档 → 缓存键。三个参数都进键（漏一个就会串档）。 */
export function feedCacheKeyOf(filter: string, sort: string,
                              direction: string): string {
  return `${filter || "all"}|${sort || "credibility"}|${direction || "all"}`;
}

/** 读缓存。**绝不抛**：隐私模式 / 配额满 / 内容损坏都只是"没有缓存"。 */
export function readIntelFeed(cacheKey: string): IntelFeedSnapshot | null {
  try {
    const raw = window.localStorage.getItem(KEY_PREFIX + cacheKey);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as IntelFeedSnapshot & { version?: number };
    if (!parsed || !parsed.feed || !Array.isArray(parsed.feed.items)) return null;
    if (Date.now() - Number(parsed.at || 0) > MAX_AGE_MS) return null;
    return parsed;
  } catch {
    return null;
  }
}

/** 写缓存。**绝不抛** —— 存不下只是下次慢一点，不该影响这次渲染。 */
export function writeIntelFeed(cacheKey: string, feed: IntelFeed): void {
  try {
    // `refreshing` 强制置 false：见模块注释 —— 让切回来是"静默核对"，
    // 而不是立刻又进入"等 WS 补拉"的轮询。
    const snap: IntelFeedSnapshot = {
      feed: { ...feed, refreshing: false },
      cacheKey,
      at: Date.now(),
    };
    window.localStorage.setItem(KEY_PREFIX + cacheKey,
                                JSON.stringify({ version: VERSION, ...snap }));
  } catch {
    /* 隐私模式禁用 localStorage / 配额满：按"没有缓存"处理 */
  }
}

/** 清空本模块写的全部键（退出登录、切换账号时调）。
 *
 * ⚠️ 必须在登出时调：下一个人在同一台机器上登录，不该看到上一个人的情报。
 * 只删自己前缀的键，不动别人的。
 */
export function clearIntelCache(): void {
  try {
    const doomed: string[] = [];
    for (let i = 0; i < window.localStorage.length; i += 1) {
      const k = window.localStorage.key(i);
      if (k && k.startsWith(KEY_PREFIX)) doomed.push(k);
    }
    doomed.forEach((k) => window.localStorage.removeItem(k));
  } catch {
    /* 隐私模式：本来就没写进去，没什么可清 */
  }
}
