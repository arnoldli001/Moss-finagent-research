import { useEffect, useState } from "react";
import { api, DataHealth, DataSourceHealthRow } from "../api";

const ms = (value: number | null | undefined) =>
  value === null || value === undefined ? "—" : `${value.toFixed(0)} ms`;

const METHOD_LABEL: Record<string, string> = {
  quote: "实时快照",
  trend: "分时1m",
  bars: "分钟K线",
};

function LatencyRow({ row }: { row: DataSourceHealthRow }) {
  const tone =
    row.cooling_down
      ? "var(--high)"
      : row.realtime === false
        ? "var(--muted)"
        : (row.median_ms ?? 999) < 20
          ? "var(--low)"
          : (row.median_ms ?? 999) < 100
            ? "var(--medium)"
            : "var(--high)";
  return (
    <tr>
      <td>
        <b>{row.label}</b>
        <span className="muted-text"> · {row.kind}</span>
        {!row.realtime && <span className="badge badge-muted">仅EOD</span>}
        {row.cooling_down && (
          <span className="badge run-failed"
                title={row.last_error || "连续失败已熔断，冷却期内不再调用"}>
            冷却 {row.cooldown_seconds?.toFixed(0)}s
          </span>
        )}
      </td>
      <td className="muted-text">{METHOD_LABEL[row.method] ?? row.method}</td>
      <td className="mono num" style={{ color: tone }}>
        {row.median_ms === null ? "样本不足" : ms(row.median_ms)}
      </td>
      <td className="mono num muted-text">{ms(row.ewma_ms)}</td>
      <td className="mono num">
        {row.calls}
        {row.total_calls !== undefined && row.total_calls !== row.calls && (
          <span className="muted-text" title="本段 / 终身累计（熔断恢复后本段清零）">
            {" "}/ {row.total_calls}
          </span>
        )}
      </td>
      <td className="center">
        <span className={`badge ${row.cooling_down
          ? "run-failed"
          : (row.success_rate ?? 1) >= 0.99 ? "run-success" : "run-skipped"}`}
          title={[
            row.lifetime_success_rate === null
              || row.lifetime_success_rate === undefined
              ? null
              : `终身成功率 ${(row.lifetime_success_rate * 100).toFixed(0)}%`,
            row.cooling_down
              ? `冷却中（剩 ${row.cooldown_seconds?.toFixed(0)}s）`
              : row.last_error || null,
          ].filter(Boolean).join("｜") || undefined}>
          {row.success_rate === null
            ? "—"
            : `${(row.success_rate * 100).toFixed(0)}%`}
        </span>
      </td>
    </tr>
  );
}

export default function DataSourceHealth() {
  const [health, setHealth] = useState<DataHealth | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let disposed = false;
    const load = async () => {
      try {
        const payload = await api.dataHealth();
        if (!disposed) setHealth(payload);
      } catch (exc) {
        if (!disposed) setError(String(exc));
      }
    };
    void load();
    const timer = window.setInterval(() => void load(), 20000);
    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, []);

  if (error) return <p className="error-text">数据源健康度获取失败：{error}</p>;
  if (!health) return <p className="muted-text">加载数据源健康度…</p>;

  const intraday = health.intraday_sources;

  // 用户口径 2026-09-23：这个区域**只保留这张实测延迟表**，且**严格 6 列**
  // （数据源 / 用途 / 中位延迟 / EWMA / 调用 / 成功率）。
  // 原来的链路示意、"全部数据源能力矩阵"、"Tushare 数据健康度"、
  // "本地数据仓库"、底部 notes、以及第 7 列「备注」全部删除 —— 那些是排障信息，
  // 不是看盘要看的。备注与最近一次错误改为挂在成功率徽标的 `title` 上
  // （冷却状态改为数据源单元格上的一枚徽标），因此**列数不再多出一列**。
  // 情绪周期的决策结论也挪到了顶部「市场环境」行（见 `MarketContextStrip`）。
  return (
    <section className="panel data-health-panel">
      <div className="health-table-scroll">
        <table className="data-table dh-latency">
          <thead>
            <tr>
              <th>数据源</th><th>用途</th><th className="num">中位延迟</th>
              <th className="num">EWMA</th><th className="num">调用</th>
              <th className="center">成功率</th>
            </tr>
          </thead>
          <tbody>
            {(intraday.sources ?? []).map((row) => (
              <LatencyRow key={`${row.source}-${row.method}`} row={row} />
            ))}
          </tbody>
        </table>
      </div>
      {(intraday.sources ?? []).length === 0 && (
        <p className="muted-text">
          {intraday.available
            ? "尚无调用记录：打开一次「量化交易」面板后即开始统计真实延迟。"
            : intraday.note}
        </p>
      )}
    </section>
  );
}
