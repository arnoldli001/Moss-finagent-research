/**
 * 事件告警的**本地缓存**与登录后**预加载**。
 *
 * ## 为什么需要它（2026-09-26 用户报障）
 *
 * > "事件告警 首次打开也要加载 2-3 秒才出数据，切网站内界面再切回也要
 * >   2 秒显示……数据入库，首次取库，减少冷启动，用户登录成功就预加载进来。"
 *
 * 量出来的账（本机 8110 对公网隧道，真实会话）：
 *
 *     后端本机 `/alerts?limit=100`      **28~74 ms**   112,520 B
 *     同一条走公网域名                  **5,705 ms**（第二次直接读超时）
 *     隧道带宽（834 KB 静态包 16.3 s）  **≈ 51 KB/s**
 *     本机同一静态包                    10 ms（80 MB/s）
 *
 * 所以瓶颈**既不是数据库也不是后端**（`fact_alerts` 只有 95 行、索引齐全），
 * 而是**把 112 KB 挪过隧道**。这决定了优化的方向：
 *
 *   · 少传 —— 服务端 `alert_to_public(for_list=True)` 已砍掉每条重复的
 *     disclaimer 与内部字段（省 ~15%）；
 *   · **少发请求** —— 也就是本模块：把已经取到的列表**留在浏览器里**，
 *     切回来直接画，不要为了"看起来是新的"再走一次 2 秒的隧道。
 *
 * ## 为什么切回来不等后台刷新就先画
 *
 * 用户对"切回来"的期待是**立刻**。先画缓存、再后台核对（stale-while-
 * revalidate）在这个带宽下是唯一能兑现"立刻"的办法。代价是极端情况下
 * 会短暂显示略旧的一份，所以 `MAX_AGE_MS` 收紧到 5 分钟 —— 过期就
 * 老实走网络，不再拿旧数据糊弄。
 *
 * ## 为什么放 localStorage 而不是内存
 *
 * 内存缓存活不过一次刷新（F5），而"打开网站 → 点事件告警"正是要救的那条路。
 * localStorage 能跨刷新；数据是**告警标题与事件描述**，不含令牌、不含原文链接
 * （服务端对普通用户已去掉 `source_url`），且**退出登录时清空**（见 clear）。
 */

import { Alert, AlertSettings } from "./api";

/** 缓存结构版本。改了字段含义就 +1，否则会读到旧结构的对象。 */
const VERSION = 1;

/** 缓存有效期。超过就当没有 —— 宁可变慢，也不能拿五分钟前的未读状态骗人。 */
const MAX_AGE_MS = 5 * 60 * 1000;

const KEY_PREFIX = "moss.alerts.v1:";

export type AlertsSnapshot = {
  alerts: Alert[];
  total: number;
  unread: number;
  /** 这是哪一档筛选的结果。缓存**按筛选档分开**存 —— 混在一起会让
   *  "仅未读"那一档拿到"全部"的列表，看起来像筛选没生效。 */
  filterKey: string;
  at: number;
  /** `/alerts/bootstrap` 顺带取回的告警设置。
   *
   * 为什么把它一起存下来（2026-09-26）：面板打开时会**另外**发一条
   * `/alerts/settings`（`AlertsPanel.tsx` 的 `useEffect`）。在隧道
   * ~51 KB/s、单次往返 0.4~2 秒的现实下，这是一次**完全可以省掉**的往返 ——
   * 预加载时 `bootstrap` 已经把 settings 一起取回来了，扔掉再取一遍没有理由。
   * 与列表同寿命（`MAX_AGE_MS`），过期就老实重新拉。 */
  settings?: AlertSettings;
};

/** 筛选档 → 缓存键。`/alerts/bootstrap` 预加载时用全空档（面板的初始状态）。 */
export function filterKeyOf(type: string, level: string,
                            status: string): string {
  return `${type || "-"}|${level || "-"}|${status || "-"}`;
}

/** 读缓存。**绝不抛**：隐私模式 / 配额满 / 内容损坏都只是"没有缓存"。 */
export function readAlerts(filterKey: string): AlertsSnapshot | null {
  try {
    const raw = window.localStorage.getItem(KEY_PREFIX + filterKey);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as AlertsSnapshot & { version?: number };
    if (!parsed || !Array.isArray(parsed.alerts)) return null;
    if (Date.now() - Number(parsed.at || 0) > MAX_AGE_MS) return null;
    return parsed;
  } catch {
    return null;
  }
}

/** 写缓存。**绝不抛** —— 存不下只是下次慢一点，不该影响这次渲染。 */
export function writeAlerts(filterKey: string, data: {
  alerts: Alert[]; total: number; unread: number;
  settings?: AlertSettings;
}): void {
  try {
    const snap: AlertsSnapshot = {
      alerts: data.alerts, total: data.total, unread: data.unread,
      filterKey, at: Date.now(),
      ...(data.settings ? { settings: data.settings } : {}),
    };
    window.localStorage.setItem(KEY_PREFIX + filterKey,
                                JSON.stringify({ version: VERSION, ...snap }));
  } catch {
    /* 隐私模式禁用 localStorage / 配额满：按"没有缓存"处理 */
  }
}

/** 读缓存里的告警设置（没有/过期返回 null）。
 *
 * 供面板在打开时**免除**一次 `/alerts/settings` 往返 —— 那份设置在
 * 预加载时已经跟着 bootstrap 回来了，见 `AlertsSnapshot.settings` 的说明。
 * 读失败一律当"没有"，让调用方回退到正常请求。 */
export function readAlertSettings(filterKey: string): AlertSettings | null {
  return readAlerts(filterKey)?.settings ?? null;
}

/** 清空本模块写的全部键（退出登录、切换账号时调）。
 *
 * ⚠️ 必须在登出时调：下一个人在同一台机器上登录，不该看到上一个人的告警。
 * 只删自己前缀的键，不动别人的。
 */
export function clearAlertsCache(): void {
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

/** 登录成功后**预加载**（用户口径："用户登录成功就预加载进来"）。
 *
 * 为什么是"登录后主动拉一次"而不是让服务端推：
 * 登录那一刻 WebSocket 往往还没建好，推了没人接；而前端主动拉是确定性的，
 * 且天然带 Cookie 与鉴权。
 *
 * 调用方**不必 await**（`login()` 不该被它拖慢）：它是纯后台增益，
 * 失败也只是"第一次打开告警页时慢一次"，正是没优化前的行为。
 *
 * ## 两道去重（这个带宽下，重复拉一次就是几秒钟）
 *
 * ① **已经有新鲜的缓存就不拉**：多标签页各自登录/刷新时，否则每个标签
 *    都会拉一份 112 KB —— 在 ~51 KB/s 上那是自己打自己。
 * ② **进程内单飞**：同一页面里并发的两次预加载合成一次。
 */
const PRELOAD_FRESH_MS = 60 * 1000;
let inflight: Promise<boolean> | null = null;

export async function preloadAlerts(
  fetchBootstrap: () => Promise<{
    alerts: Alert[]; total: number; unread: number;
    settings: AlertSettings;
  }>,
  opts: { force?: boolean } = {},
): Promise<boolean> {
  const key = filterKeyOf("", "", "");
  // `force=true` 用于**保活续期**（见 `panelPrefetch.ts`）：
  // 必须真的重新取，否则会被下面的 60 秒新鲜度判定挡住、
  // 缓存永远停在第一次那份，TTL 一到又变冷。
  if (!opts.force) {
    const existing = readAlerts(key);
    if (existing && Date.now() - existing.at < PRELOAD_FRESH_MS) return true;
  }
  if (inflight) return inflight;

  inflight = (async () => {
    try {
      const data = await fetchBootstrap();
      // settings 一起存下来：面板打开时就不必再发一条 `/alerts/settings`
      writeAlerts(key, data);
      return true;
    } catch {
      return false;
    } finally {
      inflight = null;
    }
  })();
  return inflight;
}
