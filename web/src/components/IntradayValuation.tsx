import type { IntradayValuation as IntradayValuationData } from "../api";

/**
 * 估值空间模块（左上）：同业 PE/PB 分位数对比。
 * 用于判断个股当前的绝对估值水平 —— 是「上涨空间」还是「估值透支」。
 *
 * 三个对标口径同时呈现：
 *   1. 个股自身近三年 PE/PB 分位（分位条 + 区间标注）
 *   2. 自选同业逐只 PE/PB 散点（相对中位数的溢价/折价）
 *   3. 巨潮行业中位数 PE（不依赖自选池的权威对标线）
 */

const HEADROOM_TEXT: Record<string, { label: string; cls: string }> = {
  ample: { label: "上涨空间充足", cls: "hr-ample" },
  moderate: { label: "估值中性偏多", cls: "hr-moderate" },
  stretched: { label: "估值合理偏贵", cls: "hr-stretched" },
  expensive: { label: "估值透支", cls: "hr-expensive" },
  unknown: { label: "数据不足", cls: "hr-unknown" },
};

const num = (value: number | null | undefined, digits = 2) =>
  value === null || value === undefined ? "—" : value.toFixed(digits);

/** 分位条：0%（最便宜）→ 100%（最贵），中位50%处设中性标记。 */
function PercentileBar({
  label, value, min, max, median, current,
}: {
  label: string;
  value: number | null;
  min: number | null;
  max: number | null;
  median: number | null;
  current: number | null;
}) {
  const pct = value === null ? null : Math.max(0, Math.min(100, value));
  const tone = pct === null ? "var(--muted)"
    : pct >= 80 ? "var(--low)"
    : pct >= 60 ? "var(--medium)"
    : pct <= 20 ? "var(--high)"
    : "var(--accent)";
  return (
    <div className="pct-row">
      <div className="pct-head">
        <span className="pct-label">{label}</span>
        <span className="pct-value mono" style={{ color: tone }}>
          {pct === null ? "—" : `${pct.toFixed(0)}%`}
        </span>
      </div>
      <div className="pct-track">
        <div className="pct-fill" style={{ width: `${pct ?? 0}%`, background: tone }} />
        <div className="pct-center" title="50% 分位（历史中枢）" />
      </div>
      <div className="pct-scale muted-text mono">
        <span>{num(min)}</span>
        <span>中位 {num(median)}</span>
        <span>{num(max)}</span>
      </div>
      <div className="pct-current muted-text">
        当前 <b className="mono">{num(current)}</b>
      </div>
    </div>
  );
}

export default function IntradayValuation({
  data, code,
}: {
  data: IntradayValuationData | null;
  code: string;
}) {
  if (!data) {
    return (
      <section className="panel intraday-panel">
        <h2>① 估值空间</h2>
        <div className="empty-tip muted-text">估值数据未返回（见数据健康度）</div>
      </section>
    );
  }
  const head = HEADROOM_TEXT[data.headroom] ?? HEADROOM_TEXT.unknown;
  const peers = data.peers ?? [];

  return (
    <section className="panel intraday-panel">
      <div className="panel-head">
        <h2>① 估值空间</h2>
        <span className={`headroom-badge ${head.cls}`}>{head.label}</span>
      </div>
      <p className="muted-text val-verdict">{data.verdict}</p>

      <div className="val-grid">
        <PercentileBar
          label="PE(TTM) 近三年分位"
          value={data.pe_percentile}
          min={data.pe_min} max={data.pe_max} median={data.pe_median}
          current={data.pe_ttm}
        />
        <PercentileBar
          label="PB 近三年分位"
          value={data.pb_percentile}
          min={data.pb_min} max={data.pb_max} median={data.pb_median}
          current={data.pb}
        />
      </div>

      <h3>同业对比
        <span className="muted-text">
          {" "}
          {data.peer_source === "configured"
            ? data.peer_label
            : data.peer_source === "cninfo_industry"
              ? "行业中位数"
              : "无同业数据"}
        </span>
      </h3>

      <table className="audit-table val-table">
        <thead>
          <tr>
            <th>标的</th><th>PE(TTM)</th><th>PB</th><th>涨跌幅</th><th>相对PE</th>
          </tr>
        </thead>
        <tbody>
          <tr className="val-subject">
            <td><b>{data.name || code}</b> <span className="mono">{code}</span></td>
            <td className="mono">{num(data.pe_ttm)}</td>
            <td className="mono">{num(data.pb)}</td>
            <td className="mono">—</td>
            <td className="muted-text">本股</td>
          </tr>
          {peers.map((peer) => {
            const premium = (
              data.pe_ttm && peer.pe_ttm
                ? ((peer.pe_ttm / data.pe_ttm) - 1) * 100
                : null
            );
            return (
              <tr key={peer.code}>
                <td>{peer.name || "—"} <span className="mono muted-text">{peer.code}</span></td>
                <td className="mono">{num(peer.pe_ttm)}</td>
                <td className="mono">{num(peer.pb)}</td>
                <td className="mono" style={{
                  color: (peer.change_pct ?? 0) >= 0 ? "var(--low)" : "var(--high)",
                }}>
                  {peer.change_pct === null ? "—"
                    : `${peer.change_pct >= 0 ? "+" : ""}${peer.change_pct.toFixed(2)}%`}
                </td>
                <td className="mono muted-text">
                  {premium === null ? "—"
                    : `${premium >= 0 ? "+" : ""}${premium.toFixed(0)}%`}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      <div className="val-footer">
        <span className="stat-chip">
          同业PE中位 <b className="mono">{num(data.peer_pe_median)}</b>
          {data.pe_vs_peer_pct !== null && (
            <em style={{ color: data.pe_vs_peer_pct <= 0 ? "var(--high)" : "var(--low)" }}>
              {data.pe_vs_peer_pct <= 0 ? "折价" : "溢价"}
              {Math.abs(data.pe_vs_peer_pct).toFixed(0)}%
            </em>
          )}
        </span>
        <span className="stat-chip">
          同业PB中位 <b className="mono">{num(data.peer_pb_median)}</b>
        </span>
        {data.industry_pe_median !== null && (
          <span className="stat-chip" title={data.industry_name}>
            行业中位PE <b className="mono">{num(data.industry_pe_median)}</b>
            <em className="muted-text">巨潮</em>
          </span>
        )}
        <span className="stat-chip">
          数据源 <em className="muted-text">{data.source_name || "—"}</em>
        </span>
      </div>
      {data.gap && <div className="warn-box gap-box">{data.gap}</div>}
      {data.pe_series_days < 250 && data.pe_series_days > 0 && (
        <div className="muted-text gap-box">
          分位样本 {data.pe_series_days} 个交易日（不足一年，分位代表性有限）
        </div>
      )}
    </section>
  );
}
