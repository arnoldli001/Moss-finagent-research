/**
 * 页签清单与功能额度的**本地缓存**（按用户隔离）。
 *
 * ## 它解决什么（用户报障 2026-09-28）
 *
 * > "首次登录进去，一级目录只显示投资日历、事件告警，而投研分析、策略回测、
 * >   量化交易、主线挖掘、资金流监控、热点&研报小作文都要等 3-5 秒才出来"
 *
 * 根因是**页签清单只能等服务端**，而 `GET /me/features` 必须等认证结果就位
 * 才发得出去 —— 「认证往返 → 权限往返」两次串行。这条公网链路上每次冷请求
 * 实测约 0.9 秒，页签就只能一条一条往外冒。
 *
 * 现在两道保险：
 *
 * 1. **服务端把 `visible_views` 并进 `/auth/login` 与 `/auth/bootstrap`**
 *    （往返从 2 次降到 1 次，见 `src/api/routes/my_features.py` 的
 *    `visible_views_for_tier`）—— 这解决的是**第一次**登录（本机还没有缓存）；
 * 2. **本模块把上一次拿到的整份 `MyFeatures` 按用户存进 localStorage** ——
 *    之后每次刷新页面，首帧就有完整清单，页签立即长齐，随后后台再校验。
 *
 * 两道合起来：第一次登录 = 1 次往返（认证那趟）；之后 = **0 次**往返。
 *
 * ## 为什么连整份载荷一起存，而不是只存 `visible_views`
 *
 * `quant_views` 决定「量化交易」页内三个子页签（做T/量化选股/竞价选股）
 * 是否可用。只存可见页签的话，刷新后那一页会先按"三个子功能都没开"渲染一帧，
 * 用户看到子页签闪一下才出来。整份存下来就没有这一帧。
 *
 * ## 失效与安全（**别把这几条删掉**）
 *
 * - **按 `user_id` 分键**：换个人在同一台机器登录，读到 `user_id` 不匹配就当
 *   没有缓存 —— 不会让 B 看到 A 的页签；
 * - **登出即清**（`clearFeaturesCache`，由 `useAuth.logout` 调用）；
 * - 缓存只用于**首帧**，`useFeatures` 随后一定会重新校验并覆盖它 ——
 *   管理员刚关掉的功能会在这趟校验之后消失，而不是永远留着；
 * - **空清单按"没拿到"处理**（见 `seedFeaturesViews`）：服务端在身份异常时
 *   算出来就是空数组，那不等于"这个用户没有任何页签"；
 * - 真正拦住越权的是各业务端点的 `require_feature`，**不是这里** ——
 *   本模块与 `visible_views` 同属体验层，见 `my_features.py` 开头的说明。
 */

import type { MyFeatures } from "./api";

/** localStorage 键。带版本号：改结构时直接换键，不必写迁移代码。 */
const KEY = "moss.features.v1";

export type FeaturesCache = {
  /** 这份缓存属于谁 —— 读取时必须匹配，否则视为没有缓存。 */
  user_id: string;
  /** 上一次**成功**拿到的整份载荷（含 `quant_views` / `resources`）。 */
  data?: MyFeatures;
  /** 开机探测顺路带回来的页签清单（`/auth/login`、`/auth/bootstrap`）。 */
  seedViews?: string[];
  /** 写入时刻（毫秒）。 */
  at: number;
};

/** 读缓存。`userId` 为空或不匹配时一律返回 `null`（**保守方向**）。 */
export function readFeaturesCache(userId?: string): FeaturesCache | null {
  if (!userId) return null;
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as FeaturesCache;
    if (!parsed || parsed.user_id !== userId) return null;
    return parsed;
  } catch {
    // 隐私模式 / 配额满 / 内容被改坏：当作没有缓存，绝不让它影响渲染
    return null;
  }
}

function write(entry: FeaturesCache): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(entry));
  } catch {
    /* 同上：缓存写不进去不是错误，只是下次首帧没有它 */
  }
}

/** 成功拿到整份载荷后写入（`useFeatures` 在校验成功时调用）。 */
export function writeFeaturesCache(data: MyFeatures): void {
  if (!data || !data.user_id) return;
  write({
    user_id: data.user_id,
    data,
    seedViews: data.visible_views,
    at: Date.now(),
  });
}

/**
 * 把开机探测（`/auth/login`、`/auth/bootstrap`）**顺路带回**的清单写进缓存。
 *
 * 它比重校验更快也更权威：这是同一趟请求里由服务端算出来的答案，
 * 所以即使上一份缓存里还有某个已被撤销的页签，也会在这里被立刻覆盖掉。
 *
 * ⚠️ **空数组直接忽略**：服务端在"用户读不出来"这类异常下算出来就是空，
 * 那表示"没拿到"，不表示"一个页签都没有"。当成没拿到，让 `useFeatures`
 * 回退到最小集合，而不是把整个一级目录清空。
 */
export function seedFeaturesViews(
  userId: string, views: string[] | undefined, isAdmin?: boolean,
): void {
  if (!userId || !views || views.length === 0) return;
  const prev = readFeaturesCache(userId);
  // 已有完整载荷：只覆盖页签清单那两个字段，其余（quant_views 等）保留 ——
  // 否则会把上一趟拿到的子功能开关一起抹掉。
  const data = prev?.data
    ? { ...prev.data, visible_views: views, is_admin: isAdmin ?? prev.data.is_admin }
    : undefined;
  write({ user_id: userId, data, seedViews: views, at: Date.now() });
}

/** 登出时清掉（换个人登录不该看到上一个人的页签）。 */
export function clearFeaturesCache(): void {
  try {
    localStorage.removeItem(KEY);
  } catch {
    /* 清不掉也不该阻断登出 */
  }
}
