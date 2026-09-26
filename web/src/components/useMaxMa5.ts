import { useCallback, useEffect, useState } from "react";
import { sectorCrowdingApi } from "../sectorCrowdingApi";

/**
 * "各板块近 6 年最高平滑拥挤度"映射（告警面板与散点图共用）。
 *
 * 后端是**全历史聚合**（实测首次 1.7s，之后命中进程内缓存），所以只在页面打开
 * 与刷新完成后各取一次 —— 放进 60 秒轮询会让服务端反复算这个没人变的聚合值。
 */
export function useMaxMa5(): {
  maxMa5: Record<string, number>;
  error: string;
  refresh: () => void;
} {
  const [maxMa5, setMaxMa5] = useState<Record<string, number>>({});
  const [error, setError] = useState("");
  const [tick, setTick] = useState(0);

  const refresh = useCallback(() => setTick((value) => value + 1), []);

  useEffect(() => {
    let alive = true;
    sectorCrowdingApi.sectorsMaxMa5()
      .then((payload) => { if (alive) setMaxMa5(payload.max_ma5 ?? {}); })
      .catch((exc) => {
        if (alive) setError(exc instanceof Error ? exc.message : String(exc));
      });
    return () => { alive = false; };
  }, [tick]);

  return { maxMa5, error, refresh };
}
