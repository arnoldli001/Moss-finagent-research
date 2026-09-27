import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  sectorCrowdingApi,
  type CrowdingConfigList,
  type CrowdingListItem,
} from "../sectorCrowdingApi";

/**
 * 看板清单的唯一状态所有者。
 *
 * ## 为什么抽成 hook
 *
 * "该看哪些板块"是**一张清单**，散点总览与告警面板都要读它、都要改它
 * （总览里删点、告警面板里增删置顶）。如果各自 `useState` 一份，删完这边那边
 * 还在 —— 所以只允许有一个所有者，两个面板通过同一个 hook 实例读写。
 * 服务端是唯一持久化真相（`sector_crowding_list` 表），每次写操作都用
 * **响应体里回传的整份清单**覆盖本地状态，不再多发一次 GET（少一次往返，
 * 也避免"写完立刻读"撞上 WAL 可见性时序）。
 *
 * ## 失败处理
 *
 * 写操作失败时不改本地状态、把异常抛给调用方去提示 —— 界面上看到的永远是
 * 服务端真实落库的内容，不会出现"点了删除、刷新后又回来"的错觉。
 */
export type CrowdingListController = {
  items: CrowdingListItem[];
  /** 只含 visible=true，置顶在前 */
  visible: CrowdingListItem[];
  /** **首次**加载（手里还没有任何数据）。已有数据时为 false —— 见 `refreshing` */
  loading: boolean;
  /**
   * 后台静默重取中（手里**有**数据、正在核对）。
   *
   * 与 `loading` 分开是为了让"切回来闪一下『读取中…』"消失：
   * `reload()` 不再清空 `payload`，旧数据一直可读，只在按钮上转个字。
   */
  refreshing: boolean;
  error: string;
  tradeDate: string;
  threshold: number;
  highThreshold: number;
  /** 水位需要的最少日线根数（后端配；不足则水位恒空 —— 见 `waterText`） */
  minBarsForWaterLevel: number;
  visibleCount: number;
  pinnedCount: number;
  reload: () => Promise<void>;
  add: (code: string, name: string, pinned?: boolean) => Promise<CrowdingListItem | null>;
  remove: (code: string) => Promise<void>;
  removeMany: (codes: string[]) => Promise<number>;
  togglePin: (code: string, pinned: boolean) => Promise<void>;
  reset: () => Promise<number>;
  /** 设置告警阈值（above = 高于告警 / below = 低于告警） */
  setAlert: (code: string, mode: "above" | "below",
             threshold: number) => Promise<void>;
  /** 清除某个板块的告警阈值 */
  clearAlert: (code: string) => Promise<void>;
};

export function useCrowdingList(conceptsOnly: boolean): CrowdingListController {
  const [payload, setPayload] = useState<CrowdingConfigList | null>(null);
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  /** 在飞请求序号：只有最后一次发出的请求才允许改状态。 */
  const reqRef = useRef(0);

  /**
   * 读清单（**stale-while-revalidate**）。
   *
   * ## 为什么不清空 payload（2026-09-27 用户报障"每次加载内容就延迟 1-2 秒"）
   *
   * 原写法每次 `setLoading(true)` 且**不保留**旧数据，于是：
   *   · 60 秒轮询每触发一次，表格就闪一下"读取中…"；
   *   · 这个页签进了 KeepAlive 之后，互切本来已经 0.03 秒，却因为
   *     轮询刚把 payload 清掉而在切回来时看到空表 —— 用户感知仍是 1~2 秒。
   *
   * 这条链路上一次冷请求的固定开销 ≈ 0.85~0.9 秒（见 e2e-latency-budget
   * §〇），所以"有没有旧数据可画"比"新数据早到晚到"重要得多：
   * 旧数据先画着，新数据回来再覆盖。
   *
   * ## `reqRef` 是必须的，不是防御性编程
   *
   * 用户快速连点「只看概念板块」会并发两个请求，**后发的可能先回**。
   * 没有序号守卫时，先发那个的 `finally` 会把 `loading/refreshing` 清掉，
   * 而界面还在等真正要显示的那一份 —— 表现为按钮提前恢复、数据却是旧的。
   */
  const reload = useCallback(async () => {
    const seq = ++reqRef.current;
    // 手里没数据才算"首次加载"；有数据就只是后台核对（不清屏、不闪）
    const hasData = payload !== null;
    if (hasData) setRefreshing(true);
    else setLoading(true);
    setError("");
    try {
      const next = await sectorCrowdingApi.configList(conceptsOnly);
      if (seq !== reqRef.current) return;    // 已被更新的请求取代，丢弃
      setPayload(next);
    } catch (exc) {
      if (seq !== reqRef.current) return;
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      if (seq === reqRef.current) {
        setLoading(false);
        setRefreshing(false);
      }
    }
    // ⚠️ `payload` 故意不进 deps（本仓库没有 eslint，这只是给下一个人看的）：
    //    它只用来判断"首次 or 复核"，进了会让每次数据更新都重建回调
    //    → 轮询 effect 反复重挂。
  }, [conceptsOnly]);

  useEffect(() => { void reload(); }, [reload]);

  const add = useCallback(async (code: string, name: string, pinned?: boolean) => {
    const next = await sectorCrowdingApi.listAdd(code, name, pinned, conceptsOnly);
    setPayload(next);
    return next.items.find((item) => item.sector_code === code) ?? null;
  }, [conceptsOnly]);

  const remove = useCallback(async (code: string) => {
    setPayload(await sectorCrowdingApi.listRemove(code, conceptsOnly));
  }, [conceptsOnly]);

  const removeMany = useCallback(async (codes: string[]) => {
    if (codes.length === 0) return 0;
    const next = await sectorCrowdingApi.listBatchRemove(codes, conceptsOnly);
    setPayload(next);
    return next.removed ?? codes.length;
  }, [conceptsOnly]);

  const togglePin = useCallback(async (code: string, pinned: boolean) => {
    setPayload(await sectorCrowdingApi.listPin(code, pinned, conceptsOnly));
  }, [conceptsOnly]);

  const reset = useCallback(async () => {
    const next = await sectorCrowdingApi.listReset(conceptsOnly);
    setPayload(next);
    return next.cleared ?? 0;
  }, [conceptsOnly]);

  const setAlert = useCallback(async (code: string, mode: "above" | "below",
                                      threshold: number) => {
    setPayload(await sectorCrowdingApi.alertSet(code, mode, threshold,
                                                conceptsOnly));
  }, [conceptsOnly]);

  const clearAlert = useCallback(async (code: string) => {
    setPayload(await sectorCrowdingApi.alertClear(code, conceptsOnly));
  }, [conceptsOnly]);

  const items = payload?.items ?? [];
  // 后端已按 置顶 → 水位 排好序；这里只做置顶稳定性兜底（复制后再排，
  // 不能就地 sort —— 那会改到 `items` 本身的顺序）
  const visible = useMemo(() => [...items]
    .filter((item) => item.visible)
    .sort((left, right) => (left.pinned === right.pinned
      ? 0 : (left.pinned ? -1 : 1))), [items]);

  return {
    items,
    visible,
    loading,
    refreshing,
    error,
    tradeDate: payload?.trade_date ?? "",
    threshold: payload?.threshold ?? 0.8,
    highThreshold: payload?.high_threshold ?? 0.9,
    // 后端没给就退化成 0（=不做"数据不足"提示），而不是猜一个 750：
    // 猜错会把"有水位"的板块也标成数据不足。
    minBarsForWaterLevel: payload?.min_bars_for_water_level ?? 0,
    visibleCount: payload?.visible_count ?? visible.length,
    pinnedCount: payload?.pinned_count
      ?? items.filter((item) => item.pinned).length,
    reload,
    add,
    remove,
    removeMany,
    togglePin,
    reset,
    setAlert,
    clearAlert,
  };
}
