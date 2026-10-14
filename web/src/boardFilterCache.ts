/**
 * 高拥挤度告警面板·**筛选记录**的本地缓存（2026-10-14 用户要求）。
 *
 * ## 用户原话
 *
 * > 「2、筛选记录可以保留在前端缓存里，或者持久化到本地数据库，不就有了联想了吗」
 *
 * ## 为什么是 localStorage 而不是库表
 *
 * 三个理由，按权重：
 *
 *  1. **它是"视图状态"，不是业务数据**。清单里"有哪 262 个板块"是**账号级配置**
 *     （已经按 `user_id` 落在库里，`CHG-0222`）；而"我现在只想看名字带『半导』的
 *     那几行"是**我自己此刻的视角**，换台机器就不成立，也不该被别的会话读到。
 *  2. **写频率**：筛选是**逐字符**变化的，落库 = 每敲一个字一条 `UPDATE`，
 *     在 SQLite 单写者上就是拿写锁做输入法的事（`CHG-0140` 那类"读路径藏写"的
 *     反面教材）。localStorage 是纯本地、同步、零成本。
 *  3. **刷新页面必须还在**：这正是要救的场景（打开面板 → 接着看昨天的板块），
 *     所以不能只放内存（`useState` 活不过 F5）。这一点与
 *     `crowdingDetailCache.ts` / `mainlineCache.ts` 同一套语义。
 *
 * 需要**跨设备**同步时再说（那时应当进"用户配置"接口，与清单同一张表）——
 * 现在是**明确不做**，不是忘了（见 `docs/PRD.md` §53.5）。
 *
 * ## 里面没有用户数据（所以键里不带 user_id）
 *
 * 存的是：一个筛选词 + 两个布尔开关。**没有**板块清单、持仓、告警阈值 ——
 * 那些都在库里按账号隔离。所以这里不按 `user_id` 分键（分键反而会让
 * "同一台机器换个账号登录"看到**空**筛选，而它本来就不含隐私）。
 * 登出时仍然清掉（`useAuth.logout`），保持"清了哪些"与其它缓存一致、可读。
 *
 * ## 绝不抛
 *
 * 隐私模式 / 配额满 / 内容损坏 / 结构对不上 —— 一律当作"没有记录"。
 * 读失败让面板回到默认视图，写失败只是下次不记得，都不该影响渲染。
 */

/** 缓存结构版本。改了字段含义就 +1，否则会读到旧结构的对象。 */
const VERSION = 1;

/** 键名：与 `moss.crowding.detail.*` / `moss.mainline.*` 同一命名空间。 */
const KEY = "moss.crowding.alertFilter.v1";

export type BoardFilterState = {
  /** 「板块」列筛选词（原样保存，不去空白 —— 用户敲的空格也是他的输入）。 */
  q: string;
  /** 「只看告警」（水位 ≥ 阈值） */
  onlyAlerts: boolean;
  /** 「只看已触发」（自定义告警已触发） */
  onlyTriggered: boolean;
};

/** 默认视图（没有记录 / 记录坏了时用它）。 */
export const DEFAULT_BOARD_FILTER: BoardFilterState = {
  q: "", onlyAlerts: false, onlyTriggered: false,
};

type Stored = BoardFilterState & { version?: number };

function sanitize(value: unknown): BoardFilterState | null {
  if (!value || typeof value !== "object") return null;
  const raw = value as Record<string, unknown>;
  const q = raw.q;
  // 结构校验：类型不对就当没有记录。**不做"尽量修补"** ——
  // 半截对象（比如 q 是数字）会让筛选框显示 `[object Object]` 之类的东西。
  if (typeof q !== "string") return null;
  return {
    q,
    onlyAlerts: raw.onlyAlerts === true,
    onlyTriggered: raw.onlyTriggered === true,
  };
}

/** 读筛选记录；没有 / 坏了 / 存不了 → `null`（调用方用默认值）。**绝不抛**。 */
export function readBoardFilter(): BoardFilterState | null {
  try {
    const raw = window.localStorage.getItem(KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Stored;
    if (parsed?.version !== VERSION) return null;   // 旧结构：当没有
    return sanitize(parsed);
  } catch {
    return null;
  }
}

/**
 * 写筛选记录。**绝不抛**。
 *
 * 全默认值时**删键**而不是写一条全默认的记录：否则 localStorage 里会长期留着
 * 一条"什么都没有"的条目，看键名的人会以为用户设过筛选（这也是
 * `clearBoardFilterCache` 存在的意义之外的一种"静默残留"）。
 */
export function writeBoardFilter(state: BoardFilterState): void {
  try {
    const clean = sanitize(state);
    if (!clean) return;
    if (clean.q === "" && !clean.onlyAlerts && !clean.onlyTriggered) {
      window.localStorage.removeItem(KEY);
      return;
    }
    const stored: Stored = { version: VERSION, ...clean };
    window.localStorage.setItem(KEY, JSON.stringify(stored));
  } catch {
    /* 隐私模式禁用 localStorage / 配额满：按"没记住"处理，不影响这次渲染 */
  }
}

/** 清空筛选记录（退出登录 / 切换账号时调；由 `useAuth.logout` 调用）。 */
export function clearBoardFilterCache(): void {
  try {
    window.localStorage.removeItem(KEY);
  } catch {
    /* 隐私模式：本来就没写进去 */
  }
}
