/**
 * 多因子库的**本地缓存**。
 *
 * ## 为什么需要它（2026-09-28 用户报障）
 *
 * > "多因子库（35 个）这个界面首次加载要等几秒才出现。"
 *
 * 与 alerts/intel 同源问题，但有两个独立原因：
 *
 *   1. **面板会被卸载重挂**。`App.tsx` 用三元链渲染「策略回测」视图，
 *      切走即卸载 `BacktestPanel` → `QuantFactorPanel`，组件内 `useState`
 *      全归零，回到时是全新挂载，必重发两次冷请求。
 *   2. **挂载后必发两次取数**：`/quant/factors`（毫秒级，但首次冷握手
 *      ≈0.9s）+ `/quant/data-status`（60s 后端缓存 + `asyncio.to_thread`，
 *      冷请求 0.x~1.xs）。两次在 ~51 KB/s 隧道上叠成"几秒"。
 *
 * 与 alerts/intel 一致的设计：
 *
 *   - **localStorage 而不是内存**：跨刷新存活是上次特意说一下的场景。
 *   - **先画缓存、再后台核对**（stale-while-revalidate）：挂载即有内容，
 *     同时发请求核对；拿到新的再覆盖。已画旧数据时不显示"加载中"。
 *   - **登出清空**：换人登录不该看到上一份因子库（其实因子库是静态的，
 *     但登出时清干净是惯例）。
 *
 * ## 与 alertsCache 的一处不同：TTL 拉到 24 小时
 *
 * 因子库是**静态产品口径**（35 个核心因子，价值/成长/质量/动量/波动/
 * 流动性/规模），不会因为时间流逝而需要重新拉取。但保活续期间隔
 * `KEEPALIVE_MS = 4 分钟`，TTL 必须 > 4 分钟（否则续期还没续上就过期），
 * 设 24 小时是给将来"加因子"留的缓冲 —— 真实场景下一次完整拉取仍走
 * 保活续期，**不会**真等 24 小时才更新。
 */
import { QuantFactorList } from "./api";

/** 缓存结构版本。改了字段含义就 +1，否则会读到旧结构的对象。 */
const VERSION = 1;

/** 缓存有效期。因子库是静态产品口径，24 小时足够；
 *  真实场景下一次完整拉取由保活续期驱动，TTL 到期才会回退到冷路径。 */
const MAX_AGE_MS = 24 * 60 * 60 * 1000;

const KEY = "moss.quant.factors.v1";

export type QuantFactorSnapshot = {
  library: QuantFactorList;
  at: number;
};

/** 读缓存。**绝不抛**：隐私模式 / 配额满 / 内容损坏都只是"没有缓存"。 */
export function readQuantFactors(): QuantFactorSnapshot | null {
  try {
    const raw = window.localStorage.getItem(KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as QuantFactorSnapshot & { version?: number };
    if (!parsed || !parsed.library || typeof parsed.library.count !== "number") return null;
    if (Date.now() - Number(parsed.at || 0) > MAX_AGE_MS) return null;
    return parsed;
  } catch {
    return null;
  }
}

/** 写缓存。**绝不抛** —— 存不下只是下次慢一点，不该影响这次渲染。 */
export function writeQuantFactors(library: QuantFactorList): void {
  try {
    const snap: QuantFactorSnapshot = { library, at: Date.now() };
    window.localStorage.setItem(KEY,
                                JSON.stringify({ version: VERSION, ...snap }));
  } catch {
    /* 隐私模式禁用 localStorage / 配额满：按"没有缓存"处理 */
  }
}

/** 清空（退出登录、切换账号时调）。 */
export function clearQuantCache(): void {
  try {
    window.localStorage.removeItem(KEY);
  } catch {
    /* 隐私模式：本来就没写进去 */
  }
}