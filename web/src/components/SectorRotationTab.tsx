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

  // ★ 视口交叉观察：sentinel 进入视口才创建 iframe（懒加载）。
  //
  // ⚠️ 这里用 **callback ref** 而不是 `useEffect` + `sentinelRef.current`：
  // `useEffect` 是在 commit phase 之后**异步**跑的，而 mount 时
  // `loading=true` ⇒ sentinel 那个 1px div 根本没渲染 ⇒
  // `sentinelRef.current` 是 null ⇒ effect 早早 return 了，根本没建
  // IntersectionObserver；等 `loadHistory` 完成、sentinel 入 DOM 时，
  // deps `[shouldLoad]` 也没变，effect **不会重跑**，
  // 结果 `shouldLoad` 永远 false、"滚动到此加载报告…" 永远挂着。
  // 改用 callback ref 是因为它在节点 mount 的同一渲染内就被 React 调用，
  // 一旦 sentinel 进 DOM 就立刻 observe()，跨过那个 useEffect 的竞态窗。
  const observerRef = useRef<IntersectionObserver | null>(null);
  const sentinelRefCb = useCallback((node: HTMLDivElement | null) => {
    // 1) 每次 callback ref 被调用（mount / unmount / 切换）都先清掉旧 observer，
    //    否则连续切到 rotation→非 rotation→rotation 时会留宿多个 observer。
    if (observerRef.current) {
      observerRef.current.disconnect();
      observerRef.current = null;
    }
    // 把节点存回 ref，供别处用（key 强制重挂 iframe 等不需要它了）。
    sentinelRef.current = node;
    if (!node || shouldLoad) return;
    // 2) 真正进入视口才创建 iframe——这是懒加载的全部目的。
    const obs = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setShouldLoad(true);
          obs.disconnect();
          observerRef.current = null;
        }
      },
      { rootMargin: "200px" },  // 预加载：距视口 200px 触发
    );
    obs.observe(node);
    observerRef.current = obs;
  }, [shouldLoad]);

  // iframe 加载超时兜底（实测隧道 ~51KB/s，800KB HTML 要 16s+）。
  //
  // ⚠️ deps 数组里**不**放 `iframeReady`：iframe.onLoad 触发的
  // `setIframeReady(true)` 已是 effect 内 reset 的"幂等终点"，再把它写回 deps
  // 会让 effect 在 onLoad 后**自己**被再调一次，紧接着
  // `setIframeReady(false)` 反手把它写回 —— `opacity` 永远停在 0，
  // 15s 后又被自己 closure 里的 `iframeReady=false` 改写成 iframeError，
  // 表现就是"点开行业轮动日报，灰底 +『加载超时』"，看起来像"不显示内容"。
  // 故只让 shouldLoad / frameKey / date（"iframe 真正要换内容"的那几次）
  // 控制 timer 行为：onLoad 与此同时直接 setIframeReady(true) 把 opacity
  // 顶到 1，不需要再走一遭 effect。
  useEffect(() => {
    if (!shouldLoad) return;
    setIframeReady(false);
    setIframeError(false);
    if (loadTimerRef.current !== null) {
      window.clearTimeout(loadTimerRef.current);
    }
    // ★ 这里读 `iframeReady` 是 closure 捕获：闭包里看到的**总是 effect
    // 入口瞬间的那个值**；timer 触发时的语义是"跑了 15s 还没收到 onLoad"，
    // 此时 iframeReady 仍是 false，与 onLoad→true 的语义互斥，无需
    // 把它放回 deps 来"重新对齐"。
    loadTimerRef.current = window.setTimeout(() => {
      setIframeError(true);   // 闭包真理：跑过一次就超时，setIframeReady 不可能在此刻是 true
    }, 15_000);
    return () => {
      if (loadTimerRef.current !== null) {
        window.clearTimeout(loadTimerRef.current);
      }
    };
  }, [shouldLoad, frameKey, date]);

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
          <div ref={sentinelRefCb} style={{ minHeight: 1 }} aria-hidden="true" />
          {!shouldLoad ? (
            // onClick 是兜底：万一 IntersectionObserver 在某些奇葩环境
            // （旧浏览器、offscreen worker、KeepAlive + display 切换的 race）
            // 没有触发，用户手动点一下就直接 setShouldLoad(true)，
            // 不至于让"灰底 + 加载超时"成为唯一的下场。
            <div className="muted-text" style={{ padding: 24, textAlign: "center", cursor: "pointer" }}
                 onClick={() => setShouldLoad(true)}
                 title="点击立即加载报告（正常情况下『滚动到此』会自动触发）">
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
              {/* ★ 显示条件修正：必须 `iframeReady=false` 才显示"加载超时"。
                  原版只看 `iframeError`，在以下场景会"内容出来了但还挂红 banner"：
                    · 首次加载 >15s（慢网络/echarts 下载），timer 在 onLoad 之前
                      已触发 setIframeError(true)，然后 onLoad 兜底再写
                      setIframeError(false)。React 18 的自动批处理里两条状态
                      写入都被认作更新，但只要第二次 setter 没把 banner 真隐藏
                      （偶发），错误框就留在已渲染的 iframe 上面；
                    · 子资源（echarts.min.js）失败触发 iframe.onError，
                      但 onLoad 之前/之后 iframe 主体已显示内容 —— 文案
                      "加载超时"误导但状态机写入一致。
                  一旦 iframeReady=true，说明 iframe 主体已渲染，"加载超时"
                  文案与现实不符，强制隐藏 banner。此时 iframeError state
                  仍可能为 true（避免 onLoad 里又重写），只是前端不再展示。
                  真正的错误态（iframeReady=false & iframeError=true）这条
                  分支完整保留 —— 加载真的卡死时 banner 仍然会出现。 */}
              {!iframeReady && iframeError && (
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
                  // ★ onLoad 时必须清掉 15s 超时兜底 timer：
                  // 这版 effect 的 deps 里没有 iframeReady（修过 race bug 后），
                  // 15s 计时器**不会**自动被 useEffect 取消，否则 iframe
                  // 加载完成后 15 秒照样触发 setIframeError(true)，
                  // 错误弹窗叠在已显示的内容上方 —— 看似"内容出来了，但
                  // 还有错误框"的二次故障。
                  if (loadTimerRef.current !== null) {
                    window.clearTimeout(loadTimerRef.current);
                    loadTimerRef.current = null;
                  }
                }}
                onError={() => {
                  setIframeError(true);
                  if (loadTimerRef.current !== null) {
                    window.clearTimeout(loadTimerRef.current);
                    loadTimerRef.current = null;
                  }
                }}
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
