import Disclaimer from "./Disclaimer";
import { useCallback, useEffect, useState } from "react";
import SectorCrowdingAlertPanel from "./SectorCrowdingAlertPanel";
import SectorCrowdingDetailModal from "./SectorCrowdingDetailModal";
import SectorCrowdingOverview from "./SectorCrowdingOverview";
import SectorCrowdingRefreshBar from "./SectorCrowdingRefreshBar";
import { useCrowdingList } from "./useCrowdingList";
import { useCrowdingMetrics } from "./useCrowdingMetrics";
import { useMaxMa5 } from "./useMaxMa5";

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
 * 水位 NULL = 数据不足（未刷新或样本 < 750 根）→ 前端显示"数据不足"，
 * **不参与告警**（"不知道"和"不拥挤"是两件事）。
 *
 * ## 状态归属（这一版的重点）
 *
 * "该看哪些板块"只有**一份**清单（`useCrowdingList` → 服务端
 * `sector_crowding_list` 表）。散点总览与告警面板共用它：在哪边删除/新增/置顶，
 * 另一边立刻同步，关掉浏览器再回来还是上次的配置。
 *
 * 页面加载时**只读取已有数据、不自动触发刷新**：一轮全量回填要几分钟，
 * 打开页面就自动跑会让人以为页面卡住（需求也明确"仅前端一键手动触发"）。
 */

const POLL_MS = 60000;

export default function SectorCrowdingTab() {
  /** 只看概念板块（默认开：告警只对概念板块，与需求一致）。 */
  const [conceptsOnly, setConceptsOnly] = useState(true);
  /** 详情弹窗当前板块（空 = 关闭） */
  const [picked, setPicked] = useState<{ code: string; name: string }>(
    { code: "", name: "" });
  const [notice, setNotice] = useState<string | null>(null);
  const toast = useCallback((text: string) => setNotice(text), []);
  const list = useCrowdingList(conceptsOnly);
  const { maxMa5, refresh: refreshMax } = useMaxMa5();
  // 4 列周频异动指标：算完后要重读清单（那 4 列挂在清单行上）
  const metrics = useCrowdingMetrics(toast, () => { void list.reload(); });

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(null), 9000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  // 低频兜底轮询：服务端只有"一键刷新"会改数据，但刷新可能发生在另一个标签页；
  // 60 秒一次足够，且服务端是本地读库（毫秒级）。
  useEffect(() => {
    const timer = window.setInterval(() => { void list.reload(); }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [list.reload]);

  const openDetail = useCallback((code: string, name: string) => {
    setPicked({ code, name });
  }, []);
  const closeDetail = useCallback(() => setPicked({ code: "", name: "" }), []);

  const handleRefreshFinished = useCallback(() => {
    void list.reload();
    refreshMax();
  }, [list.reload, refreshMax]);

  const error = list.error;

  return (
    <div className="crowding-root">
      <Disclaimer compact />
      {error && <div className="error-box">读取拥挤度数据失败：{error}</div>}

      {/* ① 一键刷新栏 */}
      <SectorCrowdingRefreshBar onFinished={handleRefreshFinished} />

      <div className="crowding-meta muted-text">
        <span>最新交易日 <b>{list.tradeDate || "—"}</b></span>
        <span>水位分母：近 6 年平滑拥挤度最高值</span>
        <span>告警阈值：≥ {(list.threshold * 100).toFixed(0)}%
          （≥ {(list.highThreshold * 100).toFixed(0)}% 红色）</span>
        <label className="chart-toggle">
          <input type="checkbox" checked={conceptsOnly}
                 onChange={(event) => setConceptsOnly(event.target.checked)}
                 title="只统计概念板块（行业/地区/指数样本不计入）。数据本身是全量落库的，
                        取消勾选即可查看全部板块" />
          只看概念板块
        </label>
        <span style={{ flex: 1 }} />
        <button className="btn-ghost primary" disabled={metrics.running}
                title="立即重算 4 列周频异动指标（近5日/近1月/近2月拥挤度变化 + 近1月资金净流入占比）。
                       正常每周三 08:30 自动算一次，一轮约 3~5 分钟（要抓板块成分股）"
                onClick={() => void metrics.compute(true)}>
          {metrics.running
            ? `异动指标计算中… ${(metrics.status?.progress ?? 0) * 100 | 0}%`
            : "↻ 重算异动指标"}
        </button>
        <button className="btn-ghost" disabled={list.loading}
                onClick={() => { void list.reload();
                                 void metrics.reloadSummary(); }}>
          {list.loading ? "读取中…" : "↻ 重新读取"}
        </button>
      </div>

      {/* ② 全板块水位总览（清单内板块，可选中删除/新增）
          排在告警面板**上方**：先看全局"哪些板块又大又挤"，再往下看逐条明细 */}
      <section className="panel intraday-panel">
        <div className="panel-head">
          <h3>全板块水位总览</h3>
          <span className="muted-text">
            X 轴 = 板块成交额（亿元），Y 轴 = 拥挤度水位；右上角"又大又挤"最该警惕，
            左上角"小但极挤"可能是新热点。**成交额 &gt; 10 亿 或 拥挤度水位 &gt; 80%**
            的板块已在圆点右侧标出名字（高水位的优先占位）。
            只画**清单内**的板块 —— 太挤时选中删除即可，删的是看板显示，
            历史数据仍在库里
          </span>
        </div>
        {list.visible.length === 0 && !list.loading ? (
          <div className="info-box">
            清单里还没有板块。用下方搜索框添加，或在告警面板点「重置清单」回到默认全量。
          </div>
        ) : (
          <SectorCrowdingOverview
            items={list.visible}
            threshold={list.threshold} highThreshold={list.highThreshold}
            maxMa5={maxMa5}
            onPick={openDetail}
            onRemoveMany={list.removeMany}
            onAdd={list.add}
            onNotice={toast} />
        )}
      </section>

      {/* ③ 高拥挤度告警面板：清单全量 + ≥阈值标红 + 增删置顶 + 自定义告警 */}
      <SectorCrowdingAlertPanel
        items={list.visible}
        threshold={list.threshold} highThreshold={list.highThreshold}
        tradeDate={list.tradeDate} maxMa5={maxMa5}
        metrics={metrics.summary}
        monthDays={metrics.summary?.windows?.month_days ?? 20}
        minBarsForWaterLevel={list.minBarsForWaterLevel}
        loading={list.loading}
        onPick={openDetail}
        onAdd={list.add}
        onRemove={list.remove}
        onTogglePin={list.togglePin}
        onReset={list.reset}
        onAlertSet={list.setAlert}
        onAlertClear={list.clearAlert}
        onNotice={toast} />

      {/* ④ 单板块详情：居中弹窗（Esc / ✕ / 点遮罩关闭） */}
      <SectorCrowdingDetailModal
        sectorCode={picked.code} sectorName={picked.name}
        onClose={closeDetail} />

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
