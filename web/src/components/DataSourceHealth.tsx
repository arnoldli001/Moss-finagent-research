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
      </td>
      <td className="muted-text">{METHOD_LABEL[row.method] ?? row.method}</td>
      <td className="mono" style={{ color: tone }}>
        {row.median_ms === null ? "样本不足" : ms(row.median_ms)}
      </td>
      <td className="mono muted-text">{ms(row.ewma_ms)}</td>
      <td className="mono">
        {row.calls}
        {row.total_calls !== undefined && row.total_calls !== row.calls && (
          <span className="muted-text" title="本段 / 终身累计（熔断恢复后本段清零）">
            {" "}/ {row.total_calls}
          </span>
        )}
      </td>
      <td>
        <span className={`badge ${row.cooling_down
          ? "run-failed"
          : (row.success_rate ?? 1) >= 0.99 ? "run-success" : "run-skipped"}`}
          title={row.lifetime_success_rate === null
            || row.lifetime_success_rate === undefined
            ? undefined
            : `终身成功率 ${(row.lifetime_success_rate * 100).toFixed(0)}%`}>
          {row.success_rate === null
            ? "—"
            : `${(row.success_rate * 100).toFixed(0)}%`}
        </span>
      </td>
      <td className="muted-text">
        {row.cooling_down
          ? `冷却中（剩 ${row.cooldown_seconds?.toFixed(0)}s）`
          : row.last_error || row.note}
      </td>
    </tr>
  );
}

export default function DataSourceHealth({ compact = false }: { compact?: boolean }) {
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
  const tushare = health.tushare;
  const warehouse = health.warehouse;
  const ranking = intraday.available ? intraday.ranking ?? {} : {};

  return (
    <section className="panel data-health-panel">
      <div className="panel-head">
        <h2>数据源健康度</h2>
        <span className="muted-text">
          实测延迟排序（中位数，样本不足 3 次不给结论）· {health.generated_at}
        </span>
      </div>

      {/* 做T 实时链路的实测速度与排序 */}
      <h3>做T 实时链路：按实测速度排序，互为备用</h3>
      {intraday.available ? (
        <>
          <div className="health-chain">
            {Object.entries(ranking).map(([method, order]) => (
              <div className="health-chain-row" key={method}>
                <span className="muted-text">{METHOD_LABEL[method] ?? method}</span>
                {(order as string[]).map((source, index) => (
                  <span key={source} className={`chain-node ${index === 0 ? "first" : ""}`}>
                    {index > 0 && <span className="chain-arrow">→</span>}
                    {intraday.capabilities?.[source]?.label ?? source}
                  </span>
                ))}
              </div>
            ))}
          </div>
          <table className="data-table">
            <thead>
              <tr>
                <th>数据源</th><th>用途</th><th>中位延迟</th><th>EWMA</th>
                <th>调用</th><th>成功率</th><th>备注</th>
              </tr>
            </thead>
            <tbody>
              {(intraday.sources ?? []).map((row) => (
                <LatencyRow key={`${row.source}-${row.method}`} row={row} />
              ))}
            </tbody>
          </table>
          {(intraday.sources ?? []).length === 0 && (
            <p className="muted-text">
              尚无调用记录：打开一次「量化交易」面板后即开始统计真实延迟。
            </p>
          )}
        </>
      ) : (
        <p className="muted-text">{intraday.note}</p>
      )}

      {!compact && (
        <>
          {/* 全部数据源能力矩阵 */}
          <h3>全部数据源能力矩阵</h3>
          <table className="data-table">
            <thead>
              <tr>
                <th>数据源</th><th>类型</th><th>实时</th>
                <th>提供的数据</th><th>说明</th>
              </tr>
            </thead>
            <tbody>
              {health.capability_matrix.map((row) => (
                <tr key={row.source}>
                  <td><b>{row.label}</b></td>
                  <td className="muted-text">{row.kind}</td>
                  <td>
                    <span className={`badge ${row.realtime ? "run-success" : "run-skipped"}`}>
                      {row.realtime ? "实时" : "仅EOD"}
                    </span>
                  </td>
                  <td className="muted-text">{row.fields}</td>
                  <td className="muted-text">{row.note}</td>
                </tr>
              ))}
            </tbody>
          </table>

          {/* Tushare 数据健康度 */}
          <h3>
            Tushare 数据健康度
            <span className="muted-text">
              {" "}· token {tushare.token.configured
                ? `已配置（${tushare.token.hint}）` : "未配置"}
              {" "}· {tushare.partitions} 个分区 / {(tushare.rows / 10000).toFixed(0)} 万行
            </span>
          </h3>
          <p className="muted-text">{tushare.note}</p>
          <table className="data-table">
            <thead>
              <tr>
                <th>数据集</th><th>分区</th><th>行数</th>
                <th>起始</th><th>最新</th><th>滞后</th><th>状态</th>
              </tr>
            </thead>
            <tbody>
              {tushare.datasets.map((item) => (
                <tr key={item.dataset}>
                  <td className="mono">{item.dataset}</td>
                  <td className="mono">{item.partitions}</td>
                  <td className="mono">{item.rows.toLocaleString()}</td>
                  <td className="mono muted-text">{item.first}</td>
                  <td className="mono">{item.last}</td>
                  <td className="mono">
                    {item.lag_days === null ? "—" : `${item.lag_days} 天`}
                  </td>
                  <td>
                    <span className={`badge ${item.stale ? "run-failed" : "run-success"}`}>
                      {item.stale ? "需更新" : "正常"}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {tushare.datasets.length === 0 && (
            <p className="muted-text">
              尚无本地缓存：执行 <code>python scripts/quant_sync.py download
              --start 2026-01-01 --end &lt;今天&gt;</code> 后可看到覆盖情况。
            </p>
          )}

          {/* 本地数据仓库（回测真正读的那一层） */}
          <h3>
            本地数据仓库（回测取数层）
            <span className="muted-text">
              {" "}· {warehouse && warehouse.available
                ? `${warehouse.dialect} · ${warehouse.dataset_count ?? 0} 张表 / ${((warehouse.total_rows ?? 0) / 10000).toFixed(0)} 万行`
                : "未启用"}
            </span>
          </h3>
          {warehouse && warehouse.available ? (
            <>
              <p className="muted-text">
                连接：{warehouse.description}
                {" "}· 时间跨度 {(warehouse.earliest_date || "—")} ~ {(warehouse.latest_date || "—")}
                {" "}· 主键即去重键，重复导入幂等；库不可用时自动回退 CSV 分区。
              </p>
              <table className="data-table">
                <thead>
                  <tr>
                    <th>数据集</th><th>表名</th><th>行数</th>
                    <th>起始</th><th>最新</th>
                  </tr>
                </thead>
                <tbody>
                  {(warehouse.tables ?? []).map((item) => (
                    <tr key={item.table}>
                      <td className="mono">{item.dataset}</td>
                      <td className="mono muted-text">{item.table}</td>
                      <td className="mono">{item.rows.toLocaleString()}</td>
                      <td className="mono muted-text">{item.first}</td>
                      <td className="mono">{item.last}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {(warehouse.tables ?? []).length === 0 && (
                <p className="muted-text">
                  仓库已连接但还没有数据：执行 <code>python
                  scripts/quant_warehouse.py ingest</code> 把 CSV 分区灌入。
                </p>
              )}
            </>
          ) : (
            <p className="muted-text">
              {warehouse?.hint ?? warehouse?.error ?? warehouse?.description
                ?? "仓库状态未知（健康度接口未返回该字段）"}
            </p>
          )}
        </>
      )}

      <ul className="health-notes">
        {health.notes.map((note) => <li key={note}>{note}</li>)}
      </ul>
    </section>
  );
}
