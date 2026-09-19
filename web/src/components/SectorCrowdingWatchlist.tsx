import { useCallback, useEffect, useState } from "react";
import {
  sectorCrowdingApi,
  type CrowdingWatchItem,
} from "../sectorCrowdingApi";

/**
 * 自选池管理（板块拥挤度页签内）。
 *
 * 与"资金流监控"的自选池是**两个独立的池子**：那边关注资金净流入，
 * 这边关注拥挤度水位。合成一个池会带来"加了却不显示某个指标"的困惑。
 *
 * 每行显示该板块**最新水位**（后端 JOIN 好，前端不必再逐板块请求）。
 */

function pct(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${(value * 100).toFixed(1)}%`;
}

export default function SectorCrowdingWatchlist({
  items, threshold, highThreshold, loading, onChanged, onPick, onNotice,
}: {
  items: CrowdingWatchItem[];
  threshold: number;
  highThreshold: number;
  loading?: boolean;
  onChanged: () => void;
  onPick: (code: string, name: string) => void;
  onNotice?: (text: string) => void;
}) {
  const [keyword, setKeyword] = useState("");
  const [options, setOptions] = useState<Record<string, unknown>[]>([]);
  const [searching, setSearching] = useState(false);

  // 联想：输入停顿 300ms 再查（避免每敲一个字打一次库）
  useEffect(() => {
    const text = keyword.trim();
    if (!text) { setOptions([]); return; }
    let alive = true;
    const timer = window.setTimeout(() => {
      setSearching(true);
      sectorCrowdingApi.search(text, 20)
        .then((payload) => { if (alive) setOptions(payload.sectors); })
        .catch(() => { if (alive) setOptions([]); })
        .finally(() => { if (alive) setSearching(false); });
    }, 300);
    return () => { alive = false; window.clearTimeout(timer); };
  }, [keyword]);

  const add = useCallback(async (code: string, name: string) => {
    try {
      const result = await sectorCrowdingApi.addWatch(code, name);
      onNotice?.(result.created
        ? `已加入自选池：${name || code}`
        : `${name || code} 已在自选池里`);
      setKeyword("");
      setOptions([]);
      onChanged();
    } catch (exc) {
      onNotice?.(`加入失败：${exc instanceof Error ? exc.message : String(exc)}`);
    }
  }, [onNotice, onChanged]);

  const remove = useCallback(async (code: string, name: string) => {
    try {
      await sectorCrowdingApi.removeWatch(code);
      onNotice?.(`已移出自选池：${name || code}`);
      onChanged();
    } catch (exc) {
      onNotice?.(`移除失败：${exc instanceof Error ? exc.message : String(exc)}`);
    }
  }, [onNotice, onChanged]);

  return (
    <section className="crowding-watchlist">
      <div className="crowding-alert-head">
        <h3>自选池（{items.length}）</h3>
        <div className="crowding-search">
          <input className="qsel-input" value={keyword}
                 placeholder="输入板块名称/代码搜索并加入自选池"
                 onChange={(event) => setKeyword(event.target.value)} />
          {searching && <span className="muted-text">查询中…</span>}
          {options.length > 0 && (
            <ul className="crowding-suggest">
              {options.map((option) => {
                const code = String(option.sector_code ?? "");
                const name = String(option.sector_name ?? "");
                const bars = Number(option.bars ?? 0);
                return (
                  <li key={code}>
                    <button onClick={() => void add(code, name)}>
                      <span>{name || code}</span>
                      <span className="mono muted-text"> {code}</span>
                      <span className="muted-text">
                        {" "}{bars > 0 ? `${bars} 根` : "未刷新"}
                        {option.is_concept ? "" : " · 非概念"}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
        <span style={{ flex: 1 }} />
        <button className="btn-ghost" disabled={loading}
                onClick={onChanged}>↻ 刷新</button>
      </div>

      {items.length === 0 ? (
        <div className="info-box">
          自选池为空。可在上方搜索板块加入，或点告警列表里的「加入自选池」。
        </div>
      ) : (
        <ul className="crowding-watch-list">
          {items.map((item) => {
            const water = item.water_level;
            const tone = water === null || water === undefined ? "none"
              : water >= highThreshold ? "high"
                : water >= threshold ? "warn" : "none";
            return (
              <li key={item.sector_code} className={`crowding-watch-item tone-${tone}`}>
                <button className="crowding-alert-name"
                        onClick={() => onPick(item.sector_code, item.sector_name)}>
                  {item.sector_name || item.sector_code}
                </button>
                <span className={`crowding-water tone-${tone}`}>{pct(water)}</span>
                <span className="muted-text mono">
                  平滑 {item.ma5_crowding === null
                    ? "—" : (item.ma5_crowding * 100).toFixed(3)}
                </span>
                <span className="muted-text mono">
                  成交额 {item.sector_amount === null
                    ? "—" : `${(item.sector_amount / 1e8).toFixed(0)}亿`}
                </span>
                <span className="muted-text mono">更新 {item.trade_date || "—"}</span>
                <span style={{ flex: 1 }} />
                <button className="btn-ghost tiny"
                        onClick={() => void remove(item.sector_code, item.sector_name)}>
                  移出
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
