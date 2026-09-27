import { useCallback, useEffect, useRef, useState } from "react";
import { sectorRotationApi } from "../sectorRotationApi";

/**
 * 行业轮动日报（资金流监控 · 二级页签）。
 *
 * 报告本体是后端生成的独立 HTML（/api/v1/sector_rotation/report.html），
 * 这里只提供工具条：交易日切换、强制重新生成、新窗口打开。
 *
 * ## 为什么用 iframe 而不是在 React 里重画一遍
 *
 * 报告页有 4 张 ECharts + 热力图 + 全表，后端 HTML 已是完整产物
 * （每日调度落盘、可独立分享）。React 侧再实现一遍渲染，两套图表
 * 代码会随字段调整慢慢漂开 —— iframe 保证"面板里看到的"与
 * "调度落盘/独立链接打开的"是同一份。
 *
 * ## ★★★ 2026-09-27 第八轮：IntersectionObserver 懒加载 + 骨架屏
 *
 * 审计实证：iframe 加载首屏 800KB HTML（含 4 张 ECharts），隧道带宽
 * 仅 ~51 KB/s → 实测首屏 16.3s。改前用户看到的是"空白 iframe + 转圈"。
 *
 * 改造：
 *   1. **IntersectionObserver**：iframe 在"进入视口前"不创建。
 *      用户停在「板块拥挤度」面板（前面那页）时，行业轮动面板已经挂载
 *      但 iframe 仍是占位 div → 等用户切过来/滚动到位才创建。
 *   2. **骨架屏**：iframe 在创建→加载完成的窗口期里显示一个骨架，
 *      用户感知到"系统在加载"而不是"页面坏了"。
 *   3. **超时兜底**：iframe 加载超过 15s 仍未触发 onLoad → 显示重试。
 */

export default function SectorRotationTab() {
  const [dates, setDates] = useState<string[]>([]);
  const [date, setDate] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  // iframe key：刷新后强制重载（URL 不变时 iframe 不会自己更新）
  const [frameKey, setFrameKey] = useState(0);
  // ★ 2026-09-27：IntersectionObserver 懒加载
  const sentinelRef = useRef<HTMLDivElement | null>(null);
  const [shouldLoad, setShouldLoad] = useState(false);
  const [iframeReady, setIframeReady] = useState(false);
  const [iframeError, setIframeError] = useState(false);
  // 加载超时（15s）：超时就给重试按钮，避免"无限空白"
  const loadTimerRef = useRef<number | null>(null);

  const loadHistory = useCallback(async () => {
    try {
      const result = await sectorRotationApi.history();
      setDates(result.dates);
      setDate((current) => current || result.latest || "");
      setError("");
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void loadHistory(); }, [loadHistory]);

  // ★ 视口交叉观察：sentinel 进入视口才创建 iframe（懒加载）
  useEffect(() => {
    const node = sentinelRef.current;
    if (!node || shouldLoad) return;
    const obs = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setShouldLoad(true);
          obs.disconnect();
        }
      },
      { rootMargin: "200px" },  // 预加载：距视口 200px 触发
    );
    obs.observe(node);
    return () => obs.disconnect();
  }, [shouldLoad]);

  // iframe 加载超时兜底（实测隧道 ~51KB/s，800KB HTML 要 16s+）
  useEffect(() => {
    if (!shouldLoad) return;
    setIframeReady(false);
    setIframeError(false);
    if (loadTimerRef.current !== null) {
      window.clearTimeout(loadTimerRef.current);
    }
    loadTimerRef.current = window.setTimeout(() => {
      if (!iframeReady) setIframeError(true);
    }, 15_000);
    return () => {
      if (loadTimerRef.current !== null) {
        window.clearTimeout(loadTimerRef.current);
      }
    };
  }, [shouldLoad, frameKey, date, iframeReady]);

  const onRefresh = async () => {
    setRefreshing(true);
    setError("");
    try {
      const outcome = await sectorRotationApi.refresh();
      await loadHistory();
      setDate(outcome.trade_date || "");
      setFrameKey((key) => key + 1);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setRefreshing(false);
    }
  };

  const retryIframe = () => {
    setIframeError(false);
    setFrameKey((k) => k + 1);
  };

  return (
    <div>
      <div className="fundflow-head">
        <div className="mode-switch">
          <select
            className="mode-btn"
            value={date}
            onChange={(event) => setDate(event.target.value)}
            title="选择交易日（每天一份，收盘后自动生成）"
          >
            {dates.length === 0 && <option value="">暂无已生成报告</option>}
            {dates.map((stamp) => (
              <option key={stamp} value={stamp}>
                {`${stamp.slice(0, 4)}-${stamp.slice(4, 6)}-${stamp.slice(6, 8)}`}
              </option>
            ))}
          </select>
          <button className="mode-btn" onClick={onRefresh} disabled={refreshing}
                  title="穿透缓存重新取数生成（约 20~40 秒）">
            {refreshing ? "生成中…" : "重新生成"}
          </button>
          <a className="mode-btn" href={sectorRotationApi.htmlUrl(date || undefined)}
             target="_blank" rel="noreferrer" title="新窗口打开独立报告页">
            新窗口打开 ↗
          </a>
        </div>
        <span className="muted-text">
          每个交易日收盘后自动生成（指数/行业热力图/主力流向/风格轮动/规则研判）
        </span>
      </div>
      {error && <div className="muted-text" style={{ color: "#d93026" }}>{error}</div>}
      {loading ? (
        <div className="muted-text" style={{ padding: 24 }}>读取报告列表…</div>
      ) : dates.length === 0 ? (
        <div className="muted-text" style={{ padding: 24 }}>
          还没有已生成的报告 —— 点「重新生成」立即产出最近交易日的一份。
        </div>
      ) : (
        <>
          {/* 哨兵：进入视口 → 触发 iframe 创建（懒加载） */}
          <div ref={sentinelRef} style={{ minHeight: 1 }} aria-hidden="true" />
          {!shouldLoad ? (
            <div className="muted-text" style={{ padding: 24, textAlign: "center" }}>
              滚动到此加载报告…
            </div>
          ) : (
            <div style={{ position: "relative" }}>
              {/* 骨架屏：iframe 还没 ready 之前显示 */}
              {!iframeReady && !iframeError && (
                <div className="skeleton-iframe" aria-hidden="true">
                  <div className="skeleton-block" style={{ height: 60, marginBottom: 12 }} />
                  <div className="skeleton-block" style={{ height: 200, marginBottom: 12 }} />
                  <div className="skeleton-block" style={{ height: 200, marginBottom: 12 }} />
                  <span className="muted-text">报告加载中…（4 张 ECharts + 热力图 + 全表）</span>
                </div>
              )}
              {iframeError && (
                <div className="error-box">
                  报告加载超时（&gt;15s）。可点「
                  <button className="account-mini" onClick={retryIframe}>重试</button>」
                  或"新窗口打开"在新页查看。
                </div>
              )}
              <iframe
                key={`${date}-${frameKey}`}
                src={sectorRotationApi.htmlUrl(date || undefined)}
                title="行业轮动与资金流向监控日报"
                loading="lazy"
                onLoad={() => {
                  setIframeReady(true);
                  setIframeError(false);
                }}
                onError={() => setIframeError(true)}
                style={{
                  width: "100%", height: "78vh", border: "none", borderRadius: 8,
                  background: "#f4f6f9",
                  // ★ iframe 加载完成前**不透明度 0**，骨架屏盖在上面；
                  // 加载完成后切到 1，避免"骨架闪烁 → iframe 闪现"
                  opacity: iframeReady ? 1 : 0,
                  transition: "opacity 200ms ease",
                }}
              />
            </div>
          )}
        </>
      )}
    </div>
  );
}
