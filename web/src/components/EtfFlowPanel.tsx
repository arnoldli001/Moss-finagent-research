import { useCallback, useEffect, useRef, useState } from "react";
import EtfFlowBacktest from "./EtfFlowBacktest";
import EtfFlowDetail from "./EtfFlowDetail";
import EtfFlowOverview from "./EtfFlowOverview";
import EtfFlowSignals from "./EtfFlowSignals";
import { formatDate } from "../mainlineApi";
import { etfFlowApi, type EtfFlowSnapshot } from "../etfFlowApi";

/**
 * ETF 份额监控（「资金流监控」的第二个子页签，夹在「个股资金流」与「板块拥挤度」之间）。
 *
 * ## 这个面板要回答的问题
 *
 * 宽基 ETF 的**份额变化**是"真金白银申购/赎回"留下的痕迹，与价格涨跌是两回事：
 * 份额在跌 + 价格在跌 = 赎回离场；份额在涨 + 价格在跌 = 有人在逆势申购
 * （宽基上通常是国家队/大型机构）。所以核心不是"这只 ETF 涨了多少"，而是
 * **份额的 1/5/10/20 日变化**，以及它发生在指数的什么位置（分位）。
 *
 * ## "机会信号不放行"只在一处说（用户口径：不要重复）
 *
 * 回测结论是**机会信号只在熊市有效**（牛市/震荡市方向相反），后端因此把非放行
 * 环境下的机会信号 `gated=true` 降级为观察项。用户如果只看信号列表里的
 * "🟢 机会信号"而不知道它已被门控，就会用熊市的口径在牛市里建仓 ——
 * 这是这个面板**最容易造成实际亏损的误解**。
 *
 * 所以这句话由**页签上方这条状态条**（`.etf-flow-gate-strip`）独家承担：
 * 它在**四个子视图里都看得见**，用户直接点「信号列表」也不会漏掉它。
 *
 * 「总览」横幅下方原来还有一块 `.etf-flow-regime-warn` 把同样的话又说了一遍
 * （外加"原因"、`source_notes`、`regime.gaps`）。用户口径（本轮）：
 * 「…… 提示的这些都不要显示，上方 etf-flow-gate-strip 也提示了，出现了重复」
 * → 那块已从 `EtfFlowOverview.tsx` 删除，**别再加回来**；
 * 被删掉的那三条信息在别处的落点见该文件头部注释（脚注「来源」/「环境判定」、
 * 面板的「数据缺口」条）。
 *
 * ## 数据获取：一个快照喂三个子视图
 *
 * `/etf-flow/snapshot` 一次返回 regime/indicators/positions/signals/resonance，
 * 所以总览、明细、信号三个子视图**切页签不重新请求**（与 MainlinePanel 同一套
 * 做法）；只有「回测结果」是独立接口，切过去才拉。低频轮询挂在容器上，
 * 子组件全部无副作用 —— 这个面板只在它的页签激活时挂载（见 FundFlowPanel）。
 */

/** 快照轮询周期：份额数据次日早间才更新，5 分钟一次足够，再密就是白打库。 */
const POLL_MS = 300000;

/** 子视图：总览 / 份额变化明细 / 信号列表 / 回测结果。 */
type Tab = "overview" | "detail" | "signals" | "backtest";

const TABS: { key: Tab; label: string; title: string }[] = [
  { key: "overview", label: "总览",
    title: "市场环境 + 核心宽基 ETF 份额指标 + 多产品共振" },
  { key: "detail", label: "份额变化明细",
    title: "每只 ETF 的份额与 1/5/10/20 日变化、对应指数分位" },
  { key: "signals", label: "信号列表",
    title: "机会/风险/行业反转信号，逐条列触发理由（已门控的单独标注）" },
  { key: "backtest", label: "回测结果",
    title: "按类型/环境/类型×环境的胜率与达标情况（门控规则的依据）" },
];

export default function EtfFlowPanel() {
  const [tab, setTab] = useState<Tab>("overview");
  const [snapshot, setSnapshot] = useState<EtfFlowSnapshot | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  /**
   * 用 ref 记录"是否已经有数据"，而不是把 snapshot 放进轮询 effect 的依赖：
   * 一旦进依赖，"取数成功 → state 变化 → effect 重跑 → 定时器重建"会互相触发，
   * 低频轮询就永远等不到下一次（与 MainlinePanel 同一个坑）。
   */
  const hasDataRef = useRef(false);

  const load = useCallback(async (force = false, silent = false) => {
    if (!silent) setLoading(true);
    try {
      setSnapshot(await etfFlowApi.snapshot({ refresh: force }));
      hasDataRef.current = true;
      setError("");
    } catch (exc) {
      // 静默轮询失败不覆盖上一次的成功结果：网络抖一下就把整屏清空是最糟的表现
      if (!silent) setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      if (!silent) setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!hasDataRef.current) void load();
    const timer = window.setInterval(() => void load(false, true), POLL_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  const gaps = snapshot?.gaps ?? [];
  const notes = snapshot?.source_notes ?? [];
  const regime = snapshot?.regime ?? null;
  /** 机会信号被门控：四个子视图都要能看见（见文件头的注释）。 */
  const gateClosed = regime !== null && regime.opportunity_allowed === false;

  return (
    <div className="etf-flow-root">
      {/* ① 顶部：交易日 + 时段 + 计数 + 操作 */}
      <div className="etf-flow-head">
        <div className="etf-flow-head-top">
          <b>ETF 份额监控</b>
          <span className="mono muted-text">{formatDate(snapshot?.trade_date)}</span>
          {snapshot?.session_label && (
            <span className="etf-flow-session">{snapshot.session_label}</span>
          )}
          {regime && (
            <span className={"etf-flow-regime-chip regime-" + (regime.key || "unknown")}>
              {regime.label || regime.key}
            </span>
          )}
          {snapshot?.cached && (
            <span className="muted-text"
                  title="命中服务端 TTL 缓存；点「立即刷新」可强制重算">
              缓存 {snapshot.cache_age ?? "—"}s
            </span>
          )}
          <span style={{ flex: 1 }} />
          <button className="btn-ghost tiny" disabled={loading}
                  title="跳过服务端缓存重算一次快照（份额数据次日早间更新，
                          盘后重算就能看到最新值）"
                  onClick={() => void load(true)}>
            {loading ? "刷新中…" : "↻ 立即刷新"}
          </button>
          <button className="btn-ghost tiny" disabled={loading}
                  title="只重新读取接口，不跳过缓存"
                  onClick={() => void load()}>
            {loading ? "…" : "重读"}
          </button>
        </div>

        {snapshot && (
          <div className="etf-flow-funnel">
            <span className="etf-flow-funnel-item">
              <i className="muted-text">信号</i><b>{snapshot.signal_count}</b>
            </span>
            <span className="etf-flow-funnel-item alert">
              <i className="muted-text">会告警</i><b>{snapshot.alert_count}</b>
            </span>
            <span className="etf-flow-funnel-item gated">
              <i className="muted-text">已门控</i><b>{snapshot.gated_count}</b>
            </span>
            <span className="muted-text">
              「会告警」= 强/中等级且未被环境门控；被门控的信号只进观察列表
            </span>
          </div>
        )}
      </div>

      {/* ② 门控状态条：跨子视图可见，避免用户只看信号列表而误读机会信号。
          ⚠️ 这是"机会信号不放行"的**唯一**展示位（用户口径：不要重复）——
             「总览」横幅下方那块复述同样内容 + 原因 + source_notes 的
             `.etf-flow-regime-warn` 已删除（见 EtfFlowOverview.tsx），
             所以这里也不能再指着它写"详见总览的环境横幅"。
             口径/依据仍可查：脚注的「来源：…」与「环境判定」两行。 */}
      {gateClosed && (
        <div className="etf-flow-gate-strip" role="status">
          ⚠️ 当前环境（{regime?.label || regime?.key}）
          <b>机会信号不放行，仅进观察列表</b>
          ：带「已门控」标记的信号不构成仓位建议
        </div>
      )}

      {/* ③ 数据缺口：空数据时必须显眼，否则用户只会看到一片空白 */}
      {gaps.length > 0 && (
        <div className="etf-flow-gaps">⚠️ 数据缺口：{gaps.join("；")}</div>
      )}

      {error && (
        <div className="error-box">
          读取 ETF 份额数据失败：{error}
          <div className="muted-text">
            （接口前缀 /api/v1/mainline/etf-flow；后端未挂载或份额数据未同步时
            会一直报错）
          </div>
        </div>
      )}

      {/* ④ 子视图切换 */}
      <div className="etf-flow-tabs">
        {TABS.map((item) => (
          <button key={item.key} title={item.title}
                  className={"etf-flow-tab" + (tab === item.key ? " active" : "")}
                  onClick={() => setTab(item.key)}>
            {item.label}
          </button>
        ))}
      </div>

      {/* ⑤ 子视图内容（前三个共用同一份快照） */}
      <div className="etf-flow-body">
        {tab === "overview" && (
          <EtfFlowOverview snapshot={snapshot} loading={loading} />
        )}
        {tab === "detail" && (
          <EtfFlowDetail snapshot={snapshot} loading={loading} />
        )}
        {tab === "signals" && (
          <EtfFlowSignals snapshot={snapshot} loading={loading} />
        )}
        {tab === "backtest" && (
          <EtfFlowBacktest tradeDate={snapshot?.trade_date} />
        )}
      </div>

      {/* ⑥ 脚注：来源 + 生成时间 + 免责声明 */}
      {snapshot && tab !== "backtest" && (
        <div className="etf-flow-foot muted-text">
          {notes.length > 0 ? `来源：${notes.join(" · ")}` : "来源：—"}
          {" "}· 生成于{" "}
          {(snapshot.generated_at || "").replace("T", " ").slice(0, 19) || "—"}
          {regime && (
            <div>
              环境判定：参考指数 {regime.index_name || "—"}
              （{regime.index_code || "—"}）近 {regime.window ?? "—"} 日收益；
              机会信号放行哪些环境由后端配置
              （configs/etf_flow.yaml 的 regime.opportunity_allowed）决定
              {(regime.gaps ?? []).length > 0
                && ` ⚠️ ${(regime.gaps ?? []).join("；")}`}
            </div>
          )}
          {snapshot.disclaimer && <div>{snapshot.disclaimer}</div>}
        </div>
      )}
    </div>
  );
}
