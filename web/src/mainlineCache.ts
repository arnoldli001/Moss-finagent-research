/**
 * 主线挖掘快照的**本地缓存**（首帧先画，再后台核对）。
 *
 * ## 为什么需要它（2026-10-08 用户报障）
 *
 * > "为什么每次打开 主线挖掘，热加载 切界面也要等 2-3 秒？"
 *
 * 量出来的账（本机 8110 + 公网链路，真实会话与 `access_audit.jsonl`）：
 *
 *     /api/v1/mainline/snapshot  服务端 p50 **19 ms**（今天 27 次）
 *     同一条走公网入口           每次请求**固定 ~1.0~1.2 s**
 *                                （64 字节的 `/health/live` 实测 1.03~1.74 s）
 *     打开面板时服务端偶发排队    1.4~2.5 s（09:08:24 = 2,504 ms）
 *
 * 也就是说：**后端早就热了**（水位线缓存 + 落盘热快照，见
 * `src/api/routes/mainline.py` 与 `src/mainline/warm.py`），
 * 慢的是"每次打开都要重新走一趟公网"，而 `MainlinePanel` 又**没有**客户端缓存 ——
 * 于是每次挂载都只能显示「正在读取评分…」。
 *
 * ## 与 alerts / intel / quant 同源，做法也照抄（不自造一套）
 *
 * `AGENTS.md`《性能硬约束》要求"面板数据必须复用既有的 cache / prefetch /
 * keepalive 机制"。所以这里与 `alertsCache.ts` / `intelCache.ts` /
 * `quantCache.ts` 逐条对齐：
 *
 *   - **localStorage 而不是内存**：内存缓存活不过一次刷新（F5），
 *     而"打开网站 → 点主线挖掘"正是要救的那条路；
 *   - **绝不抛**：隐私模式 / 配额满 / 内容损坏一律当"没有缓存"；
 *   - **退出登录时清空**（见 `clearMainlineCache`）；
 *   - **先画缓存、再后台核对**（stale-while-revalidate）—— 面板挂载时
 *     照旧发请求，拿到新的再覆盖。所以这里**不伪造新鲜度**：
 *     面板顶部/脚注显示的是 `trade_date` / `generated_at`，来自被画出来的
 *     那一份，用户看到的就是"这份数据是什么时候生成的"。
 *
 * ## 参数只有一处来源（★ 别再写第二遍）
 *
 * 后端的热快照缓存键里带 `top|alert_limit` 指纹（`mainline.py::WARM_TOP`），
 * 前端一旦在别处写死另一组数字，**后端那份热快照就永远命中不了**，
 * 而这个失效**不报错**（同一形状的事故见 `tests/unit/test_api_no_loop_blocking.py`
 * 的 `test_warm_uses_the_same_cache_key_as_the_route`）。
 * 所以请求参数与缓存键都从 `mainlineSnapshotParams()` 现算。
 *
 * ## TTL 取 10 分钟的理由
 *
 * 评分一天只算一次，10 分钟内画出来的那份**不可能**是错的口径；
 * 而 `panelPrefetch.ts` 的保活续期是 **4 分钟**（`KEEPALIVE_MS`），
 * 必须**小于** TTL，否则续期之间会出现"缓存已过期、面板又是冷的"窗口
 * ——判据见 `tests/unit/test_frontend_prefetch_structure.py`。
 */

import type { MainlineSnapshot } from "./mainlineApi";

/** 缓存结构版本。改了字段含义就 +1，否则会读到旧结构的对象。 */
const VERSION = 1;

/** 缓存有效期（见文件头"TTL 取 10 分钟的理由"）。 */
export const MAINLINE_MAX_AGE_MS = 10 * 60 * 1000;

/** 缓存键前缀。只删自己前缀的键，不动别人的。 */
const KEY_PREFIX = "moss.mainline.snapshot.v1:";

/**
 * 面板实际请求的参数 —— **唯一来源**。
 *
 * ⚠️ 必须与后端 `src/api/routes/mainline.py` 的 `WARM_TOP` / `WARM_ALERT_LIMIT`
 * 逐项一致：后端启动时按这两个值把落盘热快照装进进程内缓存，
 * 参数对不上就命中不了（不报错，只是每次都要现算 8~23 秒）。
 */
export const MAINLINE_TOP = 60;
export const MAINLINE_ALERT_LIMIT = 100;

/** 面板请求参数（面板与预取共用，避免两处各写一份）。 */
export function mainlineSnapshotParams(): { top: number; alertLimit: number } {
  return { top: MAINLINE_TOP, alertLimit: MAINLINE_ALERT_LIMIT };
}

/** 参数 → 缓存键。参数变了键就变，不会拿"另一个档位"的列表糊弄。 */
export function mainlineCacheKey(
  params: { top?: number; alertLimit?: number } = {},
): string {
  const { top, alertLimit } = { ...mainlineSnapshotParams(), ...params };
  return `${KEY_PREFIX}${top}|${alertLimit}`;
}

/** 缓存里存的东西：整份快照 + 写入时刻。 */
export type MainlineSnapshotSnapshot = {
  snapshot: MainlineSnapshot;
  at: number;
};

/** 读缓存。**绝不抛**：隐私模式 / 配额满 / 内容损坏都只是"没有缓存"。 */
export function readMainlineSnapshot(
  key: string = mainlineCacheKey(),
): MainlineSnapshotSnapshot | null {
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as MainlineSnapshotSnapshot & {
      version?: number;
    };
    // 形状校验只认"面板必须有的那一个字段"：缺 scores 的载荷画出来是空白，
    // 那比"没有缓存"更糟（用户会以为今天没有主线）。
    if (!parsed || !parsed.snapshot || !Array.isArray(parsed.snapshot.scores)) {
      return null;
    }
    if (Date.now() - Number(parsed.at || 0) > MAINLINE_MAX_AGE_MS) return null;
    return parsed;
  } catch {
    return null;
  }
}

/**
 * 写缓存。**绝不抛** —— 存不下只是下次慢一点，不该影响这次渲染。
 *
 * 失败是**静默**的，但代价是可见的（下次打开回到冷路径），
 * 且不会更差 —— 所以这里不额外做降级提示。
 */
export function writeMainlineSnapshot(
  snapshot: MainlineSnapshot,
  key: string = mainlineCacheKey(),
): void {
  try {
    const entry: MainlineSnapshotSnapshot = { snapshot, at: Date.now() };
    window.localStorage.setItem(key,
                                JSON.stringify({ version: VERSION, ...entry }));
  } catch {
    /* 隐私模式禁用 localStorage / 配额满：按"没有缓存"处理 */
  }
}

/** 清空本模块写的全部键（退出登录、切换账号时调）。
 *
 * ⚠️ 必须在登出时调：下一个人在同一台机器上登录，不该看到上一个人的评分与告警。
 */
export function clearMainlineCache(): void {
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
