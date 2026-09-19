import { useCallback, useEffect, useMemo, useState } from "react";
import {
  sectorCrowdingApi,
  type CrowdingAlerts,
  type CrowdingLatest,
  type CrowdingWatchItem,
} from "../sectorCrowdingApi";
import SectorCrowdingAlertPanel from "./SectorCrowdingAlertPanel";
import SectorCrowdingDetail from "./SectorCrowdingDetail";
import SectorCrowdingOverview from "./SectorCrowdingOverview";
import SectorCrowdingRefreshBar from "./SectorCrowdingRefreshBar";
import SectorCrowdingWatchlist from "./SectorCrowdingWatchlist";

/**
 * 板块拥挤度页签（资金流监控面板的第三个页签，排在"个股资金流"之后）。
 *
 * ## 指标口径（详见 docs/SECTOR_CROWDING.md）
 *
 *     板块每日成交额   = 同花顺板块指数日线成交额（vol × avg_price）
 *     全市场每日成交额 = 全部A股当日成交额之和
 *     原始拥挤度       = 板块 / 全市场
 *     平滑拥挤度       = 原始拥挤度的 MA5（可配置）
 *     拥挤度水位       = 平滑拥挤度 / **近6年**平滑拥挤度最大值 × 100%
 *
 * 水位 NULL = 数据不足（未刷新或样本 < 60 根）→ 前端显示"数据不足"，
 * **不参与告警**（"不知道"和"不拥挤"是两件事）。
 *
 * ## 结构（自上而下，与需求一致）
 *
 * 1. 一键刷新栏（刷新按钮 + 进度 + 最后更新）
 * 2. 高拥挤度告警面板（水位 ≥ 80%）
 * 3. 全板块水位总览散点
 * 4. 单板块详情（搜索 + 曲线）
 * 5. 自选池管理
 *
 * 页面加载时**只读取已有数据、不自动触发刷新**：一轮全量回填要几分钟，
 * 打开页面就自动跑会让人以为页面卡住（需求也明确"仅前端一键手动触发"）。
 */

const POLL_MS = 60000;

export default function SectorCrowdingTab() {
  const [overview, setOverview] = useState<CrowdingLatest | null>(null);
  const [alerts, setAlerts] = useState<CrowdingAlerts | null>(null);
  const [watchlist, setWatchlist] = useState<CrowdingWatchItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [picked, setPicked] = useState<{ code: string; name: string }>(
    { code: "", name: "" });
  /** 只看概念板块（默认开：告警只对概念板块，与需求一致）。 */
  const [conceptsOnly, setConceptsOnly] = useState(true);

  const toast = useCallback((text: string) => setNotice(text), []);

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(null), 9000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  const loadAll = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [latest, alertPayload, watch] = await Promise.all([
        sectorCrowdingApi.latest(conceptsOnly),
        sectorCrowdingApi.alerts(0, conceptsOnly),
        sectorCrowdingApi.watchlist(),
      ]);
      setOverview(latest);
      setAlerts(alertPayload);
      setWatchlist(watch.items);
    } catch (exc) {
      setError(`读取拥挤度数据失败：${exc instanceof Error
        ? exc.message : String(exc)}`);
    } finally {
      setLoading(false);
    }
  }, [conceptsOnly]);

  useEffect(() => { void loadAll(); }, [loadAll]);

  // 低频兜底轮询：服务端只有"一键刷新"会改数据，但刷新可能发生在另一个标签页；
  // 60 秒一次足够，且服务端是本地读库（毫秒级）。
  useEffect(() => {
    const timer = window.setInterval(() => void loadAll(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [loadAll]);

  const threshold = alerts?.threshold ?? 0.8;
  const highThreshold = alerts?.high_threshold ?? 0.9;
  const hasData = (overview?.count ?? 0) > 0;

  const newestUpdate = useMemo(() => {
    const days = (overview?.sectors ?? [])
      .map((row) => row.trade_date).filter(Boolean).sort();
    return days.length > 0 ? days[days.length - 1] : "";
  }, [overview]);

  return (
    <div className="crowding-root">
      {error && <div className="error-box">{error}</div>}

      {/* ① 一键刷新栏 */}
      <SectorCrowdingRefreshBar onFinished={() => void loadAll()} />

      <div className="crowding-meta muted-text">
        <span>最新交易日 <b>{newestUpdate || overview?.trade_date || "—"}</b></span>
        <span>水位分母：近 6 年平滑拥挤度最高值</span>
        <span>告警阈值：≥ {(threshold * 100).toFixed(0)}%
          （≥ {(highThreshold * 100).toFixed(0)}% 红色）</span>
        <label className="chart-toggle">
          <input type="checkbox" checked={conceptsOnly}
                 onChange={(event) => setConceptsOnly(event.target.checked)}
                 title="只统计概念板块（行业/地区/指数样本不计入）。数据本身是全量落库的，
                        取消勾选即可查看全部板块" />
          只看概念板块
        </label>
        <span style={{ flex: 1 }} />
        <button className="btn-ghost" disabled={loading} onClick={() => void loadAll()}>
          {loading ? "读取中…" : "↻ 重新读取"}
        </button>
      </div>

      {/* ② 高拥挤度告警面板 */}
      <SectorCrowdingAlertPanel
        data={alerts} loading={loading}
        onPick={(code, name) => setPicked({ code, name })}
        onNotice={toast} />

      {/* ③ 全板块水位总览 */}
      <section className="panel intraday-panel">
        <div className="panel-head">
          <h3>全板块水位总览</h3>
          <span className="muted-text">
            X 轴 = 板块成交额（亿元），Y 轴 = 拥挤度水位；右上角"又大又挤"最该警惕，
            左上角"小但极挤"可能是新热点
          </span>
        </div>
        {!hasData && !loading ? (
          <div className="info-box">
            还没有数据。点上方「一键刷新全部板块拥挤度」开始回填近 6 年
            （首次约几分钟，之后都是增量刷新）。
          </div>
        ) : (
          <SectorCrowdingOverview
            rows={overview?.sectors ?? []}
            threshold={threshold} highThreshold={highThreshold}
            onPick={(code, name) => setPicked({ code, name })} />
        )}
      </section>

      {/* ④ 单板块详情 */}
      <section className="panel intraday-panel">
        <div className="panel-head">
          <h3>单板块详情</h3>
          <span className="muted-text">
            上：拥挤度水位（含 {(threshold * 100).toFixed(0)}% 告警线与 90% 高危线）；
            下：成交额占全市场比例（细线=原始，粗线=MA5）
          </span>
        </div>
        <SectorCrowdingDetail
          sectorCode={picked.code} sectorName={picked.name} onNotice={toast} />
      </section>

      {/* ⑤ 自选池 */}
      <SectorCrowdingWatchlist
        items={watchlist} threshold={threshold} highThreshold={highThreshold}
        loading={loading} onChanged={() => void loadAll()}
        onPick={(code, name) => setPicked({ code, name })} onNotice={toast} />

      {notice && (
        <div className="notice-toast info" role="status" aria-live="polite">
          <span className="notice-text">{notice}</span>
          <button className="notice-close" onClick={() => setNotice(null)}
                  aria-label="关闭提示">×</button>
        </div>
      )}
    </div>
  );
}
