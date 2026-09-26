import { IntradayScoreCard, IntradaySignal } from "../api";

/**
 * 多指标合成打分模块（下半部分，核心引擎）。
 *
 * 总分 = Σ(维度得分 × 权重)；各项得分 ∈ [-1,1]，权重合计 100 → 总分 ∈ [-100,100]。
 * 表格每一行贡献分相加恰好等于总分（与后端 engine.compose_scorecard 同口径）。
 * 信号触发：|总分| ≥ 动手线 且价格触及关键档位 → 实心三角；提示线~动手线 → 空心三角。
 */

const ZONE_TEXT: Record<string, { label: string; cls: string }> = {
  strong_buy_zone: { label: "偏多回踩区", cls: "zone-strong-buy" },
  buy_zone: { label: "偏多试仓区", cls: "zone-buy" },
  neutral: { label: "震荡区间", cls: "zone-neutral" },
  sell_zone: { label: "偏空试减区", cls: "zone-sell" },
  strong_sell_zone: { label: "偏空冲高区", cls: "zone-strong-sell" },
};

const STRENGTH_TEXT: Record<string, string> = {
  solid: "实心三角 · 正式信号",
  hollow: "空心三角 · 软提示",
  forced_exit: "止损线触发（风控提示）",
  none: "无信号",
};

/** 总分刻度条：以0为中心，标注提示线/动手线，指针指示当前总分。 */
function ScoreGauge({ total, action, hint }: {
  total: number; action: number; hint: number;
}) {
  const clamp = (value: number) => Math.max(-100, Math.min(100, value));
  const pos = (value: number) => ((clamp(value) + 100) / 200) * 100;
  const marker = pos(total);
  const tone = total >= action ? "var(--high)"
    : total >= hint ? "var(--accent)"
    : total <= -action ? "var(--low)"
    : total <= -hint ? "var(--medium)"
    : "var(--muted)";
  return (
    <div className="score-gauge">
      <div className="gauge-track">
        <div className="gauge-zone gauge-sell" style={{
          left: `${pos(-100)}%`, width: `${pos(-action) - pos(-100)}%`,
        }} />
        <div className="gauge-zone gauge-watch-sell" style={{
          left: `${pos(-action)}%`, width: `${pos(-hint) - pos(-action)}%`,
        }} />
        <div className="gauge-zone gauge-neutral" style={{
          left: `${pos(-hint)}%`, width: `${pos(hint) - pos(-hint)}%`,
        }} />
        <div className="gauge-zone gauge-watch-buy" style={{
          left: `${pos(hint)}%`, width: `${pos(action) - pos(hint)}%`,
        }} />
        <div className="gauge-zone gauge-buy" style={{
          left: `${pos(action)}%`, width: `${pos(100) - pos(action)}%`,
        }} />
        <div className="gauge-tick" style={{ left: `${pos(-action)}%` }} title={`动手线 -${action}`} />
        <div className="gauge-tick" style={{ left: `${pos(-hint)}%` }} title={`提示线 -${hint}`} />
        <div className="gauge-tick" style={{ left: "50%" }} title="总分 0（中性）" />
        <div className="gauge-tick" style={{ left: `${pos(hint)}%` }} title={`提示线 +${hint}`} />
        <div className="gauge-tick" style={{ left: `${pos(action)}%` }} title={`动手线 +${action}`} />
        <div className="gauge-pointer" style={{ left: `${marker}%`, background: tone }} />
      </div>
      <div className="gauge-scale muted-text mono">
        <span>-100</span>
        <span>-{action} 动手线</span>
        <span>0</span>
        <span>+{action} 动手线</span>
        <span>+100</span>
      </div>
    </div>
  );
}

export default function IntradayScore({
  scorecard, signal, onEditWeights, overrideSource,
}: {
  scorecard: IntradayScoreCard | null;
  signal: IntradaySignal | null;
  /** 打开权重编辑弹窗（这是唯一能改「权重合计=100」口径的入口）。 */
  onEditWeights?: () => void;
  /** 非空表示这只票用的不是全局口径（档案 / YAML overrides）。 */
  overrideSource?: string;
}) {
  if (!scorecard) {
    return (
      <section className="panel intraday-panel">
        <div className="panel-head">
          <h2>③ 多指标合成打分</h2>
          {onEditWeights && (
            <button className="btn-ghost tiny" onClick={onEditWeights}
                    title="自定义这只票的因子权重（合计必须=100，保存后立即生效）">
              权重编辑
            </button>
          )}
        </div>
        <div className="empty-tip muted-text">
          打分未生成（分钟K线或行情缺口，见数据健康度）
        </div>
      </section>
    );
  }
  const zone = ZONE_TEXT[scorecard.zone] ?? ZONE_TEXT.neutral;
  const sum = scorecard.factors.reduce((acc, f) => acc + f.contribution, 0);
  const strength = signal?.strength ?? "none";
  const signalTone = signal?.kind === "low_buy" ? "high"
    : signal?.kind === "high_sell" ? "low"
    : signal?.kind === "stop_loss" ? "stop" : "none";

  return (
    <section className="panel intraday-panel">
      <div className="panel-head">
        <h2>③ 多指标合成打分 <span className="muted-text">（核心引擎）</span></h2>
        <span className={`zone-badge ${zone.cls}`}>{zone.label}</span>
        {onEditWeights && (
          <button className="btn-ghost tiny" onClick={onEditWeights}
                  title="自定义这只票的因子权重（合计必须=100，保存后立即生效）">
            权重编辑
          </button>
        )}
      </div>
      {overrideSource && (
        <div className="muted-text">
          本票口径：{overrideSource}
          —— 这张表用的权重不是全局默认值，逐项对照见「权重编辑」；
          其他标的仍走全局口径。
        </div>
      )}

      <div className="score-head">
        <div className="score-total">
          <span className="stat-label">总分</span>
          <span className="score-value mono"
                style={{
                  color: scorecard.total >= scorecard.threshold_hint ? "var(--high)"
                    : scorecard.total <= -scorecard.threshold_hint ? "var(--low)"
                    : "var(--text)",
                }}>
            {scorecard.total >= 0 ? "+" : ""}{scorecard.total.toFixed(1)}
          </span>
          <span className="stat-label">
            动手线 ±{scorecard.threshold_action}
            <br />提示线 ±{scorecard.threshold_hint}
          </span>
        </div>
        <div className="score-gauge-cell">
          <ScoreGauge total={scorecard.total}
                      action={scorecard.threshold_action}
                      hint={scorecard.threshold_hint} />
        </div>
        <div className={`score-signal signal-${signalTone}`}>
          <span className="stat-label">当前信号</span>
          <span className="signal-strength">
            {signal?.triggered ? STRENGTH_TEXT[strength] : "无信号"}
          </span>
          <span className="muted-text signal-reason">{signal?.reason ?? "—"}</span>
          {signal?.pushed && <span className="push-badge">已推送</span>}
          {signal?.blocked_by_stop_loss && (
            <span className="push-badge stop-badge">止损线约束生效</span>
          )}
        </div>
      </div>

      <p className="score-verdict">{scorecard.verdict}</p>

      <table className="audit-table score-table">
        <thead>
          <tr>
            <th>因子维度</th>
            <th className="num">权重</th>
            <th className="num">得分[-1,1]</th>
            <th className="num">贡献分</th>
            <th>计算依据</th>
          </tr>
        </thead>
        <tbody>
          {scorecard.factors.map((factor) => (
            <tr key={factor.key} className={factor.available ? "" : "factor-gap"}>
              <td className="factor-name">{factor.label}</td>
              <td className="num mono">{factor.weight}</td>
              <td className="num mono" style={{
                color: !factor.available ? "var(--muted)"
                  : factor.score > 0.15 ? "var(--high)"
                  : factor.score < -0.15 ? "var(--low)"
                  : "var(--text)",
              }}>
                {factor.available ? `${factor.score >= 0 ? "+" : ""}${factor.score.toFixed(2)}` : "—"}
              </td>
              <td className="num mono" style={{
                color: factor.contribution > 0 ? "var(--high)"
                  : factor.contribution < 0 ? "var(--low)" : "var(--muted)",
              }}>
                {factor.available
                  ? `${factor.contribution >= 0 ? "+" : ""}${factor.contribution.toFixed(1)}`
                  : "—"}
              </td>
              <td className="factor-detail muted-text">
                {factor.available ? factor.detail : `不计入（${factor.gap ?? "数据缺口"}）`}
              </td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr>
            <td>合计</td>
            <td className="num mono">
              {scorecard.available_weight}
              {scorecard.available_weight < scorecard.weights_sum && (
                <span className="muted-text" title="存在因子数据缺口">
                  /{scorecard.weights_sum}
                </span>
              )}
            </td>
            <td className="num muted-text">—</td>
            <td className="num mono"><b>{sum >= 0 ? "+" : ""}{sum.toFixed(1)}</b></td>
            <td className="muted-text">
              贡献分逐行相加 = 总分（口径一致）
              {scorecard.available_weight < 70 && (
                <b className="warn-inline">
                  ；有效权重 {scorecard.available_weight}/100 &lt; 70，
                  已禁止实心正式信号
                </b>
              )}
            </td>
          </tr>
        </tfoot>
      </table>

      <details className="score-detail">
        <summary>查看各因子中间量（可溯源）</summary>
        <div className="factor-inputs">
          {scorecard.factors.filter((f) => f.available).map((factor) => (
            <div key={factor.key} className="factor-input-row">
              <span className="factor-name">{factor.label}</span>
              <code>{JSON.stringify(factor.inputs)}</code>
            </div>
          ))}
        </div>
      </details>

      {scorecard.gaps.length > 0 && (
        <div className="warn-box gap-box">
          数据缺口：{scorecard.gaps.join("；")}
        </div>
      )}
    </section>
  );
}
