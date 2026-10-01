/**
 * 管理员 · 资源监控：按租户的调用频次 / 量级 / 延迟 / 异常次数
 * + **LLM 花费记账**（总额 / 按前端功能 / 按租户 / 按天）+ 数据源健康。
 *
 * 对应后端：`GET /api/v1/admin/platform/monitor`。
 *
 * ## 数据来源是**已有的两份审计**，不是新埋点
 *
 * - `data/audit/access_audit.jsonl`（`TenantAuditLog`）：每次请求的
 *   `tenant_id / path / status / latency_ms` → 调用、异常、延迟、配额；
 * - `data/audit/llm_audit.jsonl`（`LLMAuditLog`）：每次 LLM 调用的
 *   `tokens_in/out / model / provider / agent_id / tenant_id / path`
 *   → **费用**（`tokens × 单价`，单价取自 `configs/models.yaml`）。
 *
 * 后端只做聚合 —— 建第二套埋点会出现"审计一套、监控另一套"，
 * 两者不一致时没人知道该信哪个。
 *
 * ## 界面上刻意保留的四个口径
 *
 * 1. **异常只算 5xx**：4xx 是客户端问题。混进去的话一次扫描就能刷出几千个
 *    "异常"，指标立刻失效（还会掩盖真正的服务端故障）。
 * 2. **延迟给分位不只给均值**：均值会被少量慢请求拉平，p95 才是"用户感受到的慢"。
 * 3. **上限与用量并排**：监控的价值是"用掉了几成"，只给绝对值没法判断健康度。
 * 4. **费用要标"依据是什么"**：按前端功能分组时，`basis=page` 是**精确**的
 *    （调用发生时记录了请求路径），`basis=agent` 是**推断**的（历史行只能
 *    按 Agent 名归类）。两者显示成一样的数字，会让人把估算当账单。
 */

import { useCallback, useEffect, useState } from "react";
import {
  MonitorCost,
  MonitorCostFeature,
  MonitorPayload,
  MonitorTenant,
  platformApi,
} from "../api";
import { explain } from "./LoginScreen";

const WINDOWS = [
  { value: 15, label: "最近 15 分钟" },
  { value: 60, label: "最近 1 小时" },
  { value: 360, label: "最近 6 小时" },
  { value: 1440, label: "最近 24 小时" },
  { value: 0, label: "全部" },
];

/** 归属依据的中文说明（直接显示，不让管理员猜这个数字准不准）。 */
const BASIS_LABEL: Record<MonitorCostFeature["basis"], string> = {
  page: "精确（按请求路径）",
  agent: "推断（按 Agent 名）",
  mixed: "混合（部分精确）",
  unknown: "无法判定",
};

export default function AdminMonitorPanel() {
  const [minutes, setMinutes] = useState(60);
  const [data, setData] = useState<MonitorPayload | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [auto, setAuto] = useState(true);

  const load = useCallback(async (m: number) => {
    setBusy(true); setError("");
    try {
      setData(await platformApi.monitor(m));
    } catch (e) {
      setError(explain(e));
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => { void load(minutes); }, [load, minutes]);

  // 自动刷新：监控页放着不动要能自己更新（10 秒；比接口本身轻得多）
  useEffect(() => {
    if (!auto) return;
    const timer = window.setInterval(() => void load(minutes), 10000);
    return () => window.clearInterval(timer);
  }, [auto, load, minutes]);

  const o = data?.overall;

  return (
    <div className="admin">
      <div className="admin-head">
        <h2>资源监控</h2>
        <select className="auth-input" value={minutes}
          onChange={(e) => setMinutes(Number(e.target.value))}>
          {WINDOWS.map((w) => (
            <option key={w.value} value={w.value}>{w.label}</option>
          ))}
        </select>
        <label className="auth-check" style={{ margin: 0 }}>
          <input type="checkbox" checked={auto}
            onChange={(e) => setAuto(e.target.checked)} />
          自动刷新（10 秒）
        </label>
        <button className="account-mini" disabled={busy}
          onClick={() => void load(minutes)}>
          {busy ? "读取中…" : "立即刷新"}
        </button>
      </div>

      {error && <div className="error-box">{error}</div>}

      {o && (
        <div className="monitor-cards">
          <Metric label="总调用" value={o.calls} />
          <Metric label="服务端异常（5xx）" value={o.errors}
            tone={o.errors > 0 ? "warn" : "ok"} />
          <Metric label="平均延迟" value={`${o.avg_ms} ms`} />
          <Metric label="P95 延迟" value={`${o.p95_ms} ms`}
            tone={o.p95_ms > 3000 ? "warn" : "ok"} />
          <Metric label="租户数" value={data?.by_tenant.length ?? 0} />
        </div>
      )}

      <p className="auth-hint">
        数据源：访问审计 <code>{data?.audit_file ?? "—"}</code>
        （采样最近 {data?.sampled_calls ?? 0} 条）。
        <b>异常只统计 5xx</b> —— 4xx 是客户端问题，混进来会掩盖真正的服务端故障。
        延迟以 <b>P95</b> 为准：均值会被少量慢请求拉平。
      </p>

      {/* 配额口径的如实说明（后端 `quota_basis.notes` 原样渲染）。
          为什么把"统计局限"显示在页面上而不是只写在代码注释里：
          管理员要拿这个数字判断"该不该给这个客户加额度/续期"，
          如果累计偏低而他不知道，就会做出错误的商务决定。 */}
      {(data?.quota_basis?.notes?.length ?? 0) > 0 && (
        <details className="quota-basis">
          <summary>剩余配额的口径与局限（点开）</summary>
          <NoteList items={data!.quota_basis.notes} />
        </details>
      )}

      <h3 className="admin-section">LLM 花费（{data?.llm_cost?.window_label ?? "本月"}）</h3>
      <CostSection cost={data?.llm_cost} />

      <h3 className="admin-section">按租户</h3>
      <table className="admin-table">
        <thead>
          <tr>
            <th>租户</th><th>调用</th><th>异常</th><th>异常率</th>
            <th>活跃用户</th><th>平均 / P95 / 最大</th>
            <th>用量 vs 上限（每分钟）</th>
            <th>剩余额度（今日 / 本月）</th>
            <th>本月 LLM 花费</th><th>最频繁接口</th>
          </tr>
        </thead>
        <tbody>
          {(data?.by_tenant ?? []).map((t) => (
            <TenantRow key={t.tenant_id} t={t} minutes={minutes} />
          ))}
          {(data?.by_tenant ?? []).length === 0 && (
            <tr><td colSpan={10} className="account-dim">
              这段时间没有调用记录。
            </td></tr>
          )}
        </tbody>
      </table>

      <h3 className="admin-section">最慢 / 最频繁的接口</h3>
      <table className="admin-table">
        <thead>
          <tr><th>接口</th><th>调用</th><th>异常</th>
            <th>平均</th><th>P95</th></tr>
        </thead>
        <tbody>
          {(data?.by_path ?? []).map((p) => (
            <tr key={p.path}>
              <td className="mono">{p.path}</td>
              <td>{p.calls}</td>
              <td className={p.errors ? "account-warn" : ""}>{p.errors}</td>
              <td>{p.avg_ms} ms</td>
              <td className={p.p95_ms > 3000 ? "account-warn" : ""}>
                {p.p95_ms} ms
              </td>
            </tr>
          ))}
          {(data?.by_path ?? []).length === 0 && (
            <tr><td colSpan={5} className="account-dim">无</td></tr>
          )}
        </tbody>
      </table>

      <h3 className="admin-section">数据源 / 数据库可用性</h3>
      {data?.data_sources_note && (
        <div className="info-box">{data.data_sources_note}</div>
      )}
      <table className="admin-table">
        <thead>
          <tr><th>名称</th><th>类型</th><th>状态</th><th>说明</th></tr>
        </thead>
        <tbody>
          {(data?.data_sources ?? []).map((s) => (
            <tr key={`${s.kind}:${s.name}`}>
              <td>{s.name}</td>
              <td>{s.kind === "database" ? "数据库" : "数据源"}</td>
              <td>
                <span className={s.available
                  ? "admin-badge s-active" : "admin-badge s-disabled"}>
                  {s.available ? "可用" : "不可用"}
                </span>
              </td>
              <td className="account-dim">{s.detail || "—"}</td>
            </tr>
          ))}
          {(data?.data_sources ?? []).length === 0 && (
            <tr><td colSpan={4} className="account-dim">
              无数据（未装配时如实留空，不显示假绿）。
            </td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

function Metric({ label, value, tone = "" }: {
  label: string; value: string | number; tone?: string;
}) {
  return (
    <div className={`monitor-card ${tone}`}>
      <div className="monitor-card-value">{value}</div>
      <div className="monitor-card-label">{label}</div>
    </div>
  );
}

/** 金额显示：小额保留 2 位（0.18 元），大额加千分位。**不显示"¥0.00"来糊弄**。 */
function fmtMoney(cny: number): string {
  if (!Number.isFinite(cny)) return "—";
  if (cny > 0 && cny < 0.005) return "<0.01 元";
  return `${cny.toLocaleString("zh-CN", {
    minimumFractionDigits: 2, maximumFractionDigits: 2 })} 元`;
}

/**
 * 口径说明里的 `**强调**` 渲染成 `<b>`。
 *
 * 后端的说明文案是纯文本（它同时要进日志与接口），一直带着 Markdown 的
 * `**`；直接丢进 JSX 会**原样显示星号**。既然这些说明是"要不要信这个数字"
 * 的关键，就别让它看起来像排版事故。
 */
function renderNote(text: string) {
  return text.split("**").map((part, i) =>
    i % 2 === 1 ? <b key={i}>{part}</b> : <span key={i}>{part}</span>);
}

/** 口径说明列表（访问配额 / LLM 花费共用同一套渲染）。 */
function NoteList({ items }: { items: string[] }) {
  return (
    <ul>
      {items.map((n, i) => <li key={i}>{renderNote(n)}</li>)}
    </ul>
  );
}

/**
 * LLM 花费区：汇总卡 + 按前端功能 + 按天趋势 + 口径说明。
 *
 * 四个必须分开显示的量，混在一起就没法处理：
 *   · `paid_calls` / `free_calls` —— 本地模型是**免费**的，不是"便宜"；
 *   · `cached_calls` —— 本地缓存命中，**没有请求提供商**，金额计 0，
 *     省下的钱用 `avoided_cny`（反事实）单列 —— 混进"花费"就把好事记成坏事；
 *   · `unpriced_calls` —— 用了未登记价格的模型名，金额是**估算**；
 *   · `readable=false` —— 读不到审计，**不能**显示成"花了 0 元"。
 */
function CostSection({ cost }: { cost?: MonitorCost }) {
  if (!cost) {
    return <div className="account-dim">加载中…</div>;
  }
  if (cost.readable === false) {
    return (
      <div className="info-box">
        读不到 LLM 调用审计，本月花费<b>无法统计</b>（这不等于"没花钱"）。
        审计写入由 `LLMAuditLog` 负责，路径见后端配置 `llm_audit_dir`。
      </div>
    );
  }
  const top = cost.by_feature[0];
  const cached = cost.cached_calls ?? 0;
  const saved = cost.avoided_cny ?? 0;
  const trendMax = Math.max(0.0001, ...cost.by_day.map((d) => d.cny));
  return (
    <>
      <div className="monitor-cards">
        <Metric label={`总花费（${cost.window_label}）`}
          value={fmtMoney(cost.total_cny)} />
        <Metric label="今日花费" value={fmtMoney(cost.today_cny)} />
        <Metric label="计费调用 / 免费调用"
          value={`${cost.paid_calls} / ${cost.free_calls}`}
          tone={cost.free_calls > 0 ? "ok" : ""} />
        <Metric label="最大来源"
          value={top ? `${top.label}（${top.share_pct}%）` : "—"} />
        <Metric label="未定价模型的调用"
          value={cost.unpriced_calls}
          tone={cost.unpriced_calls > 0 ? "warn" : "ok"} />
        <Metric label="缓存命中（未产生费用）"
          value={cached > 0 ? `${cached} 次 / 省 ${fmtMoney(saved)}` : "0"}
          tone={cached > 0 ? "ok" : ""} />
      </div>
      {cached > 0 && (
        <div className="account-dim" style={{ marginBottom: 8 }}>
          其中 <b>{cached}</b> 次命中本地响应缓存（exact / semantic）——
          这些调用<b>没有请求提供商</b>，金额按 0 计；若未命中约需
          <b> {fmtMoney(saved)}</b>（反事实估算，<b>不是</b>账单）。
        </div>
      )}

      <table className="admin-table">
        <thead>
          <tr>
            <th>来源功能</th><th>花费</th><th>占比</th>
            <th>调用</th><th>token</th><th>归属依据</th>
          </tr>
        </thead>
        <tbody>
          {cost.by_feature.map((f) => (
            <tr key={f.key}>
              <td>
                <div>{f.label}</div>
                <div className="account-dim mono">{f.key}</div>
              </td>
              <td><b>{fmtMoney(f.cny)}</b></td>
              <td>
                <div className="meter" title={`${f.share_pct}%`}>
                  <div className="meter-fill" style={{ width: `${f.share_pct}%` }} />
                  <span className="meter-text">{f.share_pct}%</span>
                </div>
              </td>
              <td>{f.calls}</td>
              <td className="account-dim">{fmtNum(f.tokens)}</td>
              <td className="account-dim"
                title={f.basis === "agent"
                  ? "这一行是按 Agent 名推断的（调用发生时还没有请求路径字段）"
                  : BASIS_LABEL[f.basis]}>
                {BASIS_LABEL[f.basis]}
              </td>
            </tr>
          ))}
          {cost.by_feature.length === 0 && (
            <tr><td colSpan={6} className="account-dim">
              本月还没有 LLM 调用记录。
            </td></tr>
          )}
        </tbody>
      </table>

      {cost.unpriced_calls > 0 && (
        <div className="warn-box">
          有 <b>{cost.unpriced_calls}</b> 次调用使用了<b>未登记价格</b>的模型名
          （{cost.unpriced_models.map(([m, n]) => `${m}×${n}`).join("、")}），
          金额按兜底价（输入 {cost.fallback_price.input_cache_miss} /
          输出 {cost.fallback_price.output} 元每百万 token）<b>估算</b>。
          把模型名加进 <code>configs/models.yaml</code> 或改掉调用方即可消除。
        </div>
      )}

      <h4 className="admin-section">每日花费（近 {cost.by_day.length} 天）</h4>
      <table className="admin-table">
        <thead>
          <tr><th>日期</th><th>花费</th><th>分布</th><th>调用</th></tr>
        </thead>
        <tbody>
          {[...cost.by_day].reverse().map((d) => (
            <tr key={d.day}>
              <td className="mono">{d.day}</td>
              <td>{fmtMoney(d.cny)}</td>
              <td>
                <div className="meter" title={`${d.cny} 元`}>
                  <div className="meter-fill"
                    style={{ width: `${Math.round(d.cny / trendMax * 100)}%` }} />
                </div>
              </td>
              <td className="account-dim">{d.calls}</td>
            </tr>
          ))}
          {cost.by_day.length === 0 && (
            <tr><td colSpan={4} className="account-dim">无</td></tr>
          )}
        </tbody>
      </table>

      {(cost.basis_notes?.length ?? 0) > 0 && (
        <details className="quota-basis">
          <summary>花费的口径与局限（点开）</summary>
          <NoteList items={cost.basis_notes} />
        </details>
      )}
    </>
  );
}

function TenantRow({ t, minutes }: { t: MonitorTenant; minutes: number }) {
  const perMin = t.limits.api_calls_per_minute || 0;
  // 窗口内的"每分钟调用"：minutes=0（全部）时无法换算，如实显示 —
  const rate = minutes > 0 ? t.calls / minutes : null;
  const pct = rate !== null && perMin > 0
    ? Math.min(100, (rate / perMin) * 100) : null;
  return (
    <tr>
      <td>
        <div>{t.label}</div>
        <div className="account-dim mono">{t.tenant_id}</div>
      </td>
      <td>{t.calls}</td>
      <td className={t.errors ? "account-warn" : ""}>{t.errors}</td>
      <td className={t.error_rate > 0.05 ? "account-warn" : ""}>
        {(t.error_rate * 100).toFixed(1)}%
      </td>
      <td>{t.users}</td>
      <td>
        {t.latency_ms.avg} / <b>{t.latency_ms.p95}</b> / {t.latency_ms.max} ms
      </td>
      <td>
        {pct === null ? <span className="account-dim">—</span> : (
          <div className="meter" title={`${rate?.toFixed(2)} / ${perMin} 次每分钟`}>
            <div className={pct > 80 ? "meter-fill warn" : "meter-fill"}
              style={{ width: `${pct}%` }} />
            <span className="meter-text">
              {rate?.toFixed(1)}/{perMin}
            </span>
          </div>
        )}
      </td>
      <td><QuotaCell t={t} /></td>
      <td>
        {t.llm_cost.measured ? (
          <span title={`本月 ${t.llm_cost.calls} 次 LLM 调用、`
            + `${fmtNum(t.llm_cost.tokens)} token，占总花费 `
            + `${t.llm_cost.share_pct}%`}>
            <b>{fmtMoney(t.llm_cost.cny)}</b>
            <span className="account-dim">　{t.llm_cost.share_pct}%</span>
          </span>
        ) : (
          <span className="account-dim"
            title="LLM 审计里还没有本月记录 —— 这不等于「花了 0 元」">
            未量到
          </span>
        )}
      </td>
      <td className="account-dim mono">
        {t.top_paths.slice(0, 2).map(([p, n]) => `${p}×${n}`).join("  ")}
      </td>
    </tr>
  );
}

/** 大数字紧凑显示（token 上限动辄千万，原样打印会把这一列撑爆）。 */
function fmtNum(n: number): string {
  if (!Number.isFinite(n)) return "—";
  if (Math.abs(n) < 10000) return n.toLocaleString("zh-CN");
  const w = n / 10000;
  return `${Number.isInteger(w) ? w : w.toFixed(1)}万`;
}

/** 一条"已用 / 上限"+ 剩余 的进度条。 */
function QuotaMeter({ label, used, limit, pct, remaining }: {
  label: string; used: number; limit: number;
  pct: number | null; remaining: number;
}) {
  const p = Math.max(0, Math.min(100, pct ?? 0));
  return (
    <div className="quota-row"
      title={`${label}：已用 ${used} / 上限 ${limit}，剩余 ${remaining}`}>
      <span className="quota-label">{label}</span>
      <div className="meter">
        <div className={p > 80 ? "meter-fill warn" : "meter-fill"}
          style={{ width: `${p}%` }} />
        <span className="meter-text">{fmtNum(used)}/{fmtNum(limit)}</span>
      </div>
      <span className={p > 80 ? "quota-remain warn" : "quota-remain"}>
        剩 {fmtNum(remaining)}
      </span>
    </div>
  );
}

/** 剩余额度单元格：今日调用 + 本月 token。
 *
 * ⚠️ 三种状态必须**分清**，这是这一格存在的全部意义：
 *   ① 有上限 + 有数据 → 画进度条；
 *   ② 有上限 + **还没量到** → 显示"未量到"，**不能画成 0%** ——
 *      "这个月一点没用"和"我们还没量到用量"对管理员的决策含义完全相反
 *      （前者可以放心，后者可能是审计没接上）；
 *   ③ 没有上限（匿名/平台自身流量，`known_tier=false`）→ 显示 "—"。
 */
function QuotaCell({ t }: { t: MonitorTenant }) {
  const u = t.usage;
  const hasDay = u.calls_today_limit > 0;
  const hasTokens = u.tokens_month_limit > 0;
  if (!hasDay && !hasTokens) {
    return (
      <span className="account-dim"
        title="该租户没有套餐额度（匿名或平台自身流量），不计入配额">
        —
      </span>
    );
  }
  return (
    <div className="quota-cell">
      {hasDay && (
        <QuotaMeter label="今日调用" used={u.calls_today}
          limit={u.calls_today_limit} pct={u.calls_today_used_pct}
          remaining={u.calls_today_remaining} />
      )}
      {hasTokens && (u.tokens_measured ? (
        <QuotaMeter label="本月 token" used={u.tokens_month}
          limit={u.tokens_month_limit} pct={u.tokens_month_used_pct}
          remaining={u.tokens_month_remaining} />
      ) : (
        <div className="quota-row">
          <span className="quota-label">本月 token</span>
          <span className="account-dim"
            title="LLM 审计里还没有本月记录 —— 这不等于「用掉 0」">
            未量到（上限 {fmtNum(u.tokens_month_limit)}）
          </span>
        </div>
      ))}
    </div>
  );
}
