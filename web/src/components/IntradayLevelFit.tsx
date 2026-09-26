import { useCallback, useEffect, useRef, useState } from "react";
import { api, IntradayLevelFit, IntradayLevels } from "../api";

/**
 * 档位神经网络拟合面板（做T辅助 ② 分时图下方）。
 *
 * 回答三件事，缺一件就会误导：
 *   1. **用哪几个维度拟合的**：筹码量能结构 / 箱体压力位 / 缠论结构 / VWAP偏离 /
 *      布林带 / MACD / KDJ·RSI（7 个客观维度）；
 *   2. **成功率是多少**：`过去 N 个交易日`（in-sample）与`留一日`（walk-forward）
 *      **两个都要显示** —— 只看前者一定会被过拟合骗到；
 *   3. **档位到底用哪套**：留一日达标才启用拟合线，否则明确写"回退规则口径"。
 *
 * 另外把「放宽搜索的上限」也摆出来：用户看到 39% 会问"是没搜到还是本来不行"，
 * 上限 71% 就直接回答了这个问题（行情结构决定的上限，不是调参能解决的）。
 */

const FEATURE_LABELS: Record<string, string> = {
  chip_volume_ratio: "筹码量能结构",
  box_position: "箱体/压力位",
  chan_position: "缠论结构",
  vwap_dev_atr: "VWAP偏离",
  boll_pct_b: "布林带",
  macd_atr: "MACD",
  kdj_rsi: "KDJ/RSI",
};

function rate(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

function pct(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined) return "—";
  return `${value >= 0 ? "+" : ""}${value.toFixed(digits)}%`;
}

/** 从 one-hot 混合系数里取被选中的锚点。 */
function picked(mix: number[], anchors: number[]): number | null {
  if (!mix?.length || !anchors?.length || mix.length !== anchors.length) return null;
  let index = 0;
  for (let i = 1; i < mix.length; i += 1) if (mix[i] > mix[index]) index = i;
  return anchors[index];
}

export default function IntradayLevelFitPanel({
  code, fit, levels, onFitted,
}: {
  code: string;
  /** 快照里的拟合摘要（服务端按交易日缓存，面板启动即有值）。 */
  fit: IntradayLevelFit | null;
  /** 当前档位（用于把"拟合线"换算成"实际价格线"显示）。 */
  levels: IntradayLevels | null;
  /** 手动重训成功后回传，父组件据此强刷快照（档位会跟着变）。 */
  onFitted: (fit: IntradayLevelFit) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  /**
   * 回调放进 ref。
   *
   * 为什么必须这样：父组件每次重渲染（分时快照每 15 秒一推、hover 也会触发）
   * 都会新建一个 `onFitted` 函数，如果它进 `useCallback` 的依赖，`run` 的身份
   * 就跟着变；而 `run` 一旦进了某个 effect 的依赖，就会形成
   * 「快照更新 → 回调变 → 重新拟合 → 快照更新」的死循环（实测：页面每秒重渲染）。
   * 用 ref 固定身份，回调只通过 ref 读最新值。
   */
  const onFittedRef = useRef(onFitted);
  useEffect(() => { onFittedRef.current = onFitted; }, [onFitted]);

  // 切换标的时收起详情，避免把上一只票的数字当成本票的
  useEffect(() => { setOpen(false); setError(null); }, [code]);

  const run = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const data = await api.intradayLevelFit(code, true);
      onFittedRef.current(data);
      setOpen(true);
    } catch (exc) {
      setError(`拟合失败：${exc instanceof Error ? exc.message : String(exc)}`);
    } finally {
      setBusy(false);
    }
  }, [code]);

  const metrics = fit?.metrics;
  const applied = Boolean(fit?.applied);
  const lowPct = fit ? picked(fit.low_mix, fit.low_anchors) : null;
  const highPct = fit ? picked(fit.high_mix, fit.high_anchors) : null;
  const stopPct = fit ? picked(fit.stop_mix, fit.stop_anchors) : null;
  const reference = levels?.vwap ?? levels?.price ?? null;
  const lineOf = (value: number | null, sign: number) =>
    value === null || reference === null ? null : reference * (1 + sign * value / 100);

  return (
    <div className="level-fit-strip">
      <span className="muted-text">档位口径：</span>
      {metrics?.available ? (
        applied ? (
          <b className="fit-on">神经网络拟合</b>
        ) : (
          <b className="fit-off">规则口径（拟合未过闸门）</b>
        )
      ) : (
        <span className="muted-text">{metrics?.reason || "拟合不可用"}</span>
      )}

      {metrics?.available && (
        <>
          <span className="mono" title="训练窗口内的成功率（用户口径：过去 N 个交易日）">
            样本内 <b>{rate(metrics.in_sample_rate)}</b>
            <span className="muted-text">（{metrics.touch_samples} 次触及）</span>
          </span>
          <span className="mono" title="留一日交叉验证：用其余交易日拟合、在留出日上验证 —— 决定能不能启用">
            留一日 <b className={metrics.gate_passed ? "fit-on" : "fit-off"}>
              {rate(metrics.walk_forward_rate)}
            </b>
          </span>
          <span className="mono muted-text" title="同一窗口内放宽搜索能达到的上限：用来区分'没搜到'与'到不了'">
            可达上限 {rate(metrics.best_achievable_rate)}
          </span>
          <span className="mono muted-text">
            {metrics.sessions} 日 / {metrics.bars} 根 · 前瞻 {metrics.horizon_bars} 根
          </span>
        </>
      )}

      <button className="btn-ghost tiny" disabled={busy} onClick={() => void run()}
              title="用这只票自己的历史重新拟合一次（约 1~5 秒），成功后档位立即按新线显示">
        {busy ? "拟合中…" : "重新拟合"}
      </button>
      <button className="btn-ghost tiny" onClick={() => setOpen((value) => !value)}>
        {open ? "收起依据" : "查看依据"}
      </button>

      {error && <span className="error-text"> {error}</span>}

      {open && (
        <div className="level-fit-detail">
          <table className="audit-table compact-table">
            <thead>
              <tr>
                <th>对比项</th><th className="num">样本内</th>
                <th className="num">留一日</th><th>说明</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>做T成功率</td>
                <td className="num mono">{rate(metrics?.in_sample_rate)}</td>
                <td className="num mono">{rate(metrics?.walk_forward_rate)}</td>
                <td className="muted-text">
                  触及回踩后、{metrics?.horizon_bars ?? 24} 根 bar 内先到冲高且不破止损
                  = 成功；只统计"触及过"的样本（不触及不计入分母）
                </td>
              </tr>
              <tr>
                <td>平均单轮收益</td>
                <td className="num mono" colSpan={2}>
                  {pct(metrics?.avg_round_trip_pct ?? null, 3)}
                </td>
                <td className="muted-text">已扣一轮双边摩擦成本（含佣金/印花税/滑点口径）</td>
              </tr>
              <tr>
                <td>目标 / 闸门</td>
                <td className="num mono" colSpan={2}>
                  {rate(metrics?.target_hit_rate)} {metrics?.gate_passed ? "✓ 已启用" : "✗ 未启用"}
                </td>
                <td className="muted-text">{metrics?.gate_reason}</td>
              </tr>
              {metrics?.best_achievable_rate !== null
                && metrics?.best_achievable_rate !== undefined && (
                <tr>
                  <td>放宽搜索上限</td>
                  <td className="num mono" colSpan={2}>{rate(metrics.best_achievable_rate)}</td>
                  <td className="muted-text">
                    {metrics.best_achievable_lines.length >= 4
                      ? `最优组合：回踩 −${metrics.best_achievable_lines[0].toFixed(2)}% / `
                        + `冲高 +${metrics.best_achievable_lines[1].toFixed(2)}% / `
                        + `止损 −${metrics.best_achievable_lines[2].toFixed(1)}%（`
                        + `${metrics.best_achievable_lines[3].toFixed(0)} 次触及）`
                      : "该窗口内能达到的最高成功率"}
                    {metrics.best_achievable_rate < (metrics.target_hit_rate ?? 0.8)
                      ? " —— 上限低于目标：这是行情结构决定的上限，调参解决不了"
                      : " —— 上限高于目标：机会存在，拟合还没泛化到它"}
                  </td>
                </tr>
              )}
            </tbody>
          </table>

          <div className="weight-editor-chips">
            <span className="stat-chip">
              <em>拟合回踩</em>
              {lowPct === null ? "—" : `−${lowPct.toFixed(2)}%`}
              {lineOf(lowPct, -1) !== null && (
                <span className="mono muted-text"> ≈ {lineOf(lowPct, -1)?.toFixed(2)}</span>
              )}
            </span>
            <span className="stat-chip">
              <em>拟合冲高</em>
              {highPct === null ? "—" : `+${highPct.toFixed(2)}%`}
              {lineOf(highPct, 1) !== null && (
                <span className="mono muted-text"> ≈ {lineOf(highPct, 1)?.toFixed(2)}</span>
              )}
            </span>
            <span className="stat-chip">
              <em>拟合止损</em>
              {stopPct === null ? "—" : `−${stopPct.toFixed(2)}%`}
              {lineOf(stopPct, -1) !== null && (
                <span className="mono muted-text"> ≈ {lineOf(stopPct, -1)?.toFixed(2)}</span>
              )}
            </span>
            {fit?.adjustment && (
              <span className="stat-chip">
                <em>调整项</em>
                结构 ×{fit.adjustment.structure?.toFixed(2)}
                {" / 环境 ×"}{fit.adjustment.environment?.toFixed(2)}
                {" / 微观 ×"}{fit.adjustment.micro?.toFixed(2)}
              </span>
            )}
          </div>

          <p className="muted-text">
            <b>参与拟合的 7 个客观维度：</b>
            {(fit?.features?.length
              ? fit.features.map((item) => FEATURE_LABELS[item.key] ?? item.label)
              : Object.values(FEATURE_LABELS)
            ).join(" · ")}
            ；其余 7 个维度（指数量能 / 消息面 / 市场情绪 / 情绪周期 / 海外映射 /
            板块排行 / 股性）只作为上面的**调整项乘数**做后置微调。
          </p>
          {fit?.notes?.length ? (
            <ul className="weight-editor-notes">
              {fit.notes.map((note, index) => <li key={index}>{note}</li>)}
            </ul>
          ) : null}
        </div>
      )}
    </div>
  );
}
