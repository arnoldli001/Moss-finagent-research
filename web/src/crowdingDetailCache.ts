/**
 * 板块拥挤度·单板块历史曲线的**本地缓存**。
 *
 * ## 为什么需要它（2026-09-30 用户报障）
 *
 * > "板块拥挤度 里，打开单个概念的历史拥挤度数据的图，**十几秒才出数据**，
 * >  看下什么原因，加载这么慢"
 *
 * 根因是**响应体积**（不是数据库）：后端全链路只花 **13 ms**，而这条曲线
 * 近 6 年 1400+ 根日线，明文 424 KB、gzip 66 KB ⇒ 在 ~51 KB/s 的公网隧道上
 * 1.3~1.5 秒，在**已登记过的劣化档 ~4.6 KB/s** 上就是 **14~16 秒**。
 *
 * 服务端侧已按显示精度瘦身（gzip 砍半，见 `docs/PRD.md` §22 / `CHG-0137`）。
 * **但客户端这一层原本完全没有缓存** —— 关掉弹窗再打开同一个板块，
 * 那份 33 KB 会**原样重传一遍**。隧道带宽是这条链路上最稀缺的资源
 * （`docs/OPS_GUIDE.md`：另有一次劣化到 ~4.6 KB/s，同一请求要 20 秒），
 * 所以"重传一份手里已经有的曲线"是纯浪费。
 *
 * ## 与 alerts / intel / quant 同一套语义（不另造一套）
 *
 * `AGENTS.md` 明令"面板数据必须复用既有的 cache / prefetch / keepalive 机制，
 * **不得自造一套**"，所以这里照抄 `quantCache.ts` 的形状：
 *
 *   - **localStorage 而不是内存**：内存缓存活不过一次刷新（F5），
 *     而"刷新页面 → 再点开同一个板块"正是要救的那条路；
 *   - **先画缓存、再后台核对**（stale-while-revalidate）：打开即有曲线，
 *     同时发请求核对，拿到新的再覆盖；
 *   - **按 `sector_code` 分键**：混在一起会让"点开另一个板块"看到上一个的曲线
 *     —— 那比慢更糟（**看着有据的错误数据**）。这是本项目登记过的缺陷形状。
 *
 * ## ★ 刻意**不设 TTL**（与 quantCache 的唯一区别，理由如下）
 *
 * `quantCache` 有 24 小时 TTL，超期就返回 `null`。这里**不这么做**：
 *
 *   - 本功能要的就是"**打开即有图**"。设了 TTL，隔夜后再打开就退回冷路径，
 *     用户又要等一次几秒 —— 那正是本次报障要消灭的体验；
 *   - 而"旧数据"在这里**不会误导**：拥挤度是**日频**数据，曲线上的
 *     `last_trade_date` 就画在图上，用户看得见自己看的是哪一天。
 *
 * 所以**一律返回已缓存的数据**，新数据回来再覆盖。年龄由
 * `readCrowdingDetailAge()` 单独暴露，供调用方决定要不要显示"正在核对"。
 */
import type { CrowdingDetail } from "./sectorCrowdingApi";
import { roundPayload } from "./floatPrecision";

/** 缓存结构版本。改了字段含义就 +1，否则会读到旧结构的对象。 */
const VERSION = 1;

/** 键前缀。按 `sector_code` 追加，所以每个板块一份。 */
const KEY_PREFIX = "moss.crowding.detail.v1.";

/**
 * 单次最多保留几个板块的曲线。
 *
 * ## 为什么必须有上限（不是"防御性编程"）
 *
 * 一份曲线 gzip 后 ~33 KB，但 **localStorage 存的是明文 JS 字符串**
 * （浏览器不会帮你压缩），实测一份 ~258 KB。`localStorage` 配额通常只有
 * **5 MB/源**，所以**约 19 份就会撑爆** —— 而撑爆的后果不是"缓存失效"，
 * 是 `setItem` 抛 `QuotaExceededError`，**连带其它模块的缓存一起写不进去**
 * （告警、情报、因子库共用同一个 5 MB）。
 *
 * 所以这里主动 LRU 淘汰：只留最近打开的 **8** 个板块（~2 MB，留足余量）。
 * 用户是在板块之间来回对比，8 个覆盖了绝大多数来回切的动作。
 */
const MAX_ENTRIES = 8;

export type CrowdingDetailSnapshot = {
  detail: CrowdingDetail;
  at: number;
};

type StoredEntry = CrowdingDetailSnapshot & { version?: number };

function keyOf(sectorCode: string): string {
  return KEY_PREFIX + sectorCode;
}

function parseEntry(raw: string | null, sectorCode: string): StoredEntry | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as StoredEntry;
    // 结构校验：宁可当作没有缓存，也不要让半截对象把图表画崩
    if (!parsed || !Array.isArray(parsed.detail?.series)) return null;
    if (parsed.detail.sector_code !== sectorCode) return null;
    return parsed;
  } catch {
    return null;
  }
}

/**
 * 读缓存。**绝不抛**：隐私模式 / 配额满 / 内容损坏都只是"没有缓存"。
 *
 * 返回非 `null` 就可以**立刻画图**，不必等网络（SWR 的 stale 那一半）。
 * **不做过期判定** —— 理由见模块头"刻意不设 TTL"。
 */
export function readCrowdingDetail(sectorCode: string): CrowdingDetail | null {
  if (!sectorCode) return null;
  try {
    return parseEntry(window.localStorage.getItem(keyOf(sectorCode)),
                      sectorCode)?.detail ?? null;
  } catch {
    return null;
  }
}

/**
 * 缓存写入时刻（epoch ms）；没有缓存时返回 `null`。
 *
 * 与 `readCrowdingDetail` 分开，是因为"**能不能画**"与"**该不该提示正在核对**"
 * 是两个判断：前者只看有没有数据，后者才看新不新。
 */
export function readCrowdingDetailAge(sectorCode: string): number | null {
  if (!sectorCode) return null;
  try {
    const entry = parseEntry(window.localStorage.getItem(keyOf(sectorCode)),
                             sectorCode);
    if (!entry) return null;
    const at = Number(entry.at || 0);
    return at > 0 ? at : null;
  } catch {
    return null;
  }
}

/**
 * 写缓存。**绝不抛** —— 存不下只是下次慢一点，不该影响这次渲染。
 *
 * ## ★ 写入前按 3 位有效数字取整（用户 2026-09-30 口径）
 *
 * > 「一律 3 位有效数字，同时作用于服务端出口和**前端缓存写入**」
 *
 * 服务端出口已经在压了（同一个口径），这里再压一次是**幂等**的；
 * 它真正防的是**服务端还没重启/还没上线**时写进来的旧精度数据 ——
 * 那些 19 位小数会以 ~258 KB/份的速度吃掉 localStorage 配额。
 *
 * ⚠️ 缓存里存的必须是**与界面显示一致**的精度：否则会出现
 * "缓存命中时显示一种精度、联网核对后变成另一种"的抖动。
 */
export function writeCrowdingDetail(detail: CrowdingDetail): void {
  const code = detail?.sector_code;
  if (!code || !Array.isArray(detail.series)) return;
  try {
    evictIfNeeded(code);
    const entry: StoredEntry = {
      version: VERSION,
      detail: roundPayload(detail),
      at: Date.now(),
    };
    window.localStorage.setItem(keyOf(code), JSON.stringify(entry));
  } catch {
    /* 隐私模式禁用 localStorage / 配额满：按"没有缓存"处理。
       注意这里**不**再抛 —— 缓存写失败不该让用户看不到图。 */
  }
}

/**
 * 淘汰最旧的条目，给 `incoming` 腾位置。
 *
 * 用**我们自己写进去的 `at`** 排序，不用 `localStorage` 的插入序 ——
 * 插入序在"重写同一个键"时**不更新**（覆盖写不改变位置），
 * 于是刚看过的板块会被误当成最旧的淘汰掉。
 */
function evictIfNeeded(incoming: string): void {
  const mine: { key: string; at: number }[] = [];
  for (let i = 0; i < window.localStorage.length; i += 1) {
    const k = window.localStorage.key(i);
    if (!k || !k.startsWith(KEY_PREFIX)) continue;
    // 只动**本模块**的键：其余模块的缓存（告警/情报/因子库）不归这里管
    let at = 0;
    try {
      at = Number((JSON.parse(window.localStorage.getItem(k) || "{}") as
        StoredEntry).at || 0);
    } catch {
      at = 0;   // 损坏的条目当成最旧，优先淘汰
    }
    mine.push({ key: k, at });
  }
  // 已经在缓存里 → 覆盖写不会新增条目，不必淘汰
  if (mine.some((m) => m.key === keyOf(incoming))) return;
  if (mine.length < MAX_ENTRIES) return;
  mine.sort((a, b) => a.at - b.at);
  for (const doomed of mine.slice(0, mine.length - MAX_ENTRIES + 1)) {
    window.localStorage.removeItem(doomed.key);
  }
}

/** 清空本模块的全部缓存（退出登录 / 切换账号时调）。 */
export function clearCrowdingDetailCache(): void {
  try {
    const doomed: string[] = [];
    for (let i = 0; i < window.localStorage.length; i += 1) {
      const k = window.localStorage.key(i);
      if (k && k.startsWith(KEY_PREFIX)) doomed.push(k);
    }
    doomed.forEach((k) => window.localStorage.removeItem(k));
  } catch {
    /* 隐私模式：本来就没写进去 */
  }
}
