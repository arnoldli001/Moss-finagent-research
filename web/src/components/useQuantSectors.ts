import { useCallback, useEffect, useState } from "react";
import { api, type QuantSector } from "../api";

/**
 * 自定义板块的数据与增删（给左侧「自选」下拉用）。
 *
 * ## 为什么板块放在左侧而不是「量化选股」面板里
 *
 * 板块**既是选股范围也是归类标签**：选股结果要往里放，看盘时要按板块翻个股。
 * 这两件事都发生在左侧栏的操作路径上 —— 放在量化选股面板里，每看一次板块
 * 成分都得先切页签再切回来。用户口径 2026-09-21：板块要"和自选列一个位置"，
 * 所以这里把列表、新建、加成分、删除都收到左侧。
 *
 * ## 公开仓库下的降级
 *
 * `/api/v1/quant/sectors` 属**私有路由**（`src/api/routes/quant_select.py`
 * 不进公开仓库）。公开 checkout 里这条接口 404，此时 `available=false`，
 * 左侧下拉退化成只有「自选」—— 与裁剪前的开源版行为一致：不报错、不留空面板。
 *
 * 注意区分两种失败：
 *   - 404/405 = **这条路由不存在**（公开仓库）→ 判死 capabilities，隐藏板块 UI；
 *   - 其它错误（网络抖动等）→ 只记 error 提示，**不隐藏**板块 UI ——
 *     否则用户会以为自己在本地建的板块全丢了。
 */

/** 左侧列表当前展示的来源：自选池，或某个自定义板块。 */
export type DrawerSource = { kind: "watch" } | { kind: "sector"; id: number };

/** 新建/更新板块的请求体（与 `api.quantSaveSector` 对齐）。 */
export type SectorDraft = Parameters<typeof api.quantSaveSector>[0];

export function useQuantSectors() {
  const [sectors, setSectors] = useState<QuantSector[]>([]);
  /** 服务端是否具备板块能力（公开仓库 → false）。 */
  const [available, setAvailable] = useState(true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const reload = useCallback(async () => {
    try {
      const result = await api.quantSectors(true);
      setSectors(result.sectors);
      setAvailable(true);
      setError("");
    } catch (exc) {
      const message = exc instanceof Error ? exc.message : String(exc);
      if (message.includes("(404)") || message.includes("(405)")) {
        setAvailable(false);
        setSectors([]);
        setError("");
      } else {
        setError(message);
      }
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  /** 统一的"做一次写操作 + 成功后重读列表"包装（避免各处漏刷新）。 */
  const mutate = useCallback(async <T,>(action: () => Promise<T>): Promise<T> => {
    setBusy(true);
    try {
      const result = await action();
      await reload();
      return result;
    } finally {
      setBusy(false);
    }
  }, [reload]);

  const save = useCallback(
    (body: SectorDraft) => mutate(() => api.quantSaveSector(body)),
    [mutate],
  );

  const remove = useCallback(
    (id: number) => mutate(() => api.quantDeleteSector(id)),
    [mutate],
  );

  const addMembers = useCallback(
    (id: number, codes: { code: string; name?: string }[]) =>
      mutate(() => api.quantAddSectorMembers(id, codes)),
    [mutate],
  );

  const removeMember = useCallback(
    (id: number, code: string) => mutate(() => api.quantRemoveSectorMember(id, code)),
    [mutate],
  );

  return {
    sectors, available, error, busy,
    reload, save, remove, addMembers, removeMember,
  };
}
