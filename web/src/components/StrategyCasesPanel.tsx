import { useCallback, useEffect, useState } from "react";
import { api, type QuantCaseList } from "../api";

/**
 * 开源策略案例表（两个页面共用）。
 *
 * ## 必须写在界面上的话
 *
 * 这些内容是**从网上自动抓来的说法**，不是我们回测过的结论。公开策略普遍存在：
 * 样本区间不明、费用/滑点未计、未来函数、以及"只发盈利案例"的选择性报告。
 * 所以表里每一条都带来源链接与发布时间供核实，并且顶部固定显示免责说明。
 *
 * 一张只列"主题/策略简述/来源/发布时间"、却不说明真伪的表，会让人默认它是可信的 ——
 * 那是这个功能最危险的用法。
 */
export function StrategyCasesPanel({
  title, note, defaultThemes, limit = 40,
}: {
  title: string;
  note: string;
  defaultThemes?: string[];
  limit?: number;
}) {
  const [data, setData] = useState<QuantCaseList | null>(null);
  const [theme, setTheme] = useState("");
  const [showTools, setShowTools] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async (themeFilter: string, tools: boolean) => {
    setLoading(true);
    try {
      const payload = await api.quantCases({
        theme: themeFilter || undefined,
        limit,
        excludeKinds: tools ? "" : "框架/工具",
      });
      setData(payload);
      setError(null);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setLoading(false);
    }
  }, [limit]);

  useEffect(() => { void load("", false); }, [load]);

  return (
    <section className="panel">
      <div className="panel-head">
        <h2>{title}（{data?.count ?? 0}）</h2>
        <span className="muted-text">{note}</span>
      </div>

      <div className="warn-box" style={{ fontSize: 12 }}>
        ⚠ <b>以下是网络公开分享的策略案例，仅作线索，未经本项目复现验证。</b>
        {" "}公开策略普遍存在样本区间不明、费用/滑点未计、未来函数、以及只发布
        盈利案例的选择性报告问题 —— <b>请勿直接照做</b>。
        每条都附来源链接与发布时间，请自行核实。
      </div>

      <div className="submit-bar" style={{ flexWrap: "wrap" }}>
        <button className={theme === "" ? "btn-ghost tiny active" : "btn-ghost tiny"}
                onClick={() => { setTheme(""); void load("", showTools); }}>
          全部主题
        </button>
        {(data?.themes ?? []).slice(0, 8).map((item) => (
          <button key={item.theme}
                  className={theme === item.theme
                    ? "btn-ghost tiny active" : "btn-ghost tiny"}
                  onClick={() => { setTheme(item.theme);
                                   void load(item.theme, showTools); }}>
            {item.theme} {item.count}
          </button>
        ))}
        <label className="muted-text" style={{ marginLeft: "auto" }}>
          <input type="checkbox" checked={showTools}
                 onChange={(e) => { setShowTools(e.target.checked);
                                    void load(theme, e.target.checked); }} />
          {" "}包含框架/工具（非策略）
        </label>
      </div>

      {defaultThemes?.length ? (
        <p className="muted-text" style={{ fontSize: 11 }}>
          与本项目 35 因子相关的主题：{defaultThemes.join("、")}
        </p>
      ) : null}

      {error && <div className="error-text">加载失败：{error}</div>}
      {loading && <p className="muted-text">加载中…</p>}
      {!loading && data && data.count === 0 && (
        <p className="muted-text">
          案例库为空。先抓取一次：<code>uv run python scripts/quant_cases.py fetch</code>
          {" "}（可挂到计划任务：每周一给多因子页、每天给单股票页各抓一次）
        </p>
      )}

      {data && data.count > 0 && (
        <>
          <table className="data-table">
            <thead>
              <tr>
                <th style={{ width: 110 }}>主题</th>
                <th>策略简述</th>
                <th style={{ width: 92 }}>策略来源</th>
                <th style={{ width: 100 }}>发布时间</th>
              </tr>
            </thead>
            <tbody>
              {data.cases.map((item) => (
                <tr key={item.id}>
                  <td>
                    <span className="badge badge-muted">{item.theme}</span>
                    {item.kind && item.kind !== "策略案例" && (
                      <div className="muted-text" style={{ fontSize: 10 }}>
                        {item.kind}
                      </div>
                    )}
                  </td>
                  <td>
                    <b>{item.title}</b>
                    {item.summary && (
                      <div className="muted-text" style={{ fontSize: 11 }}>
                        {item.summary.slice(0, 150)}
                      </div>
                    )}
                  </td>
                  <td className="mono" style={{ fontSize: 11 }}>
                    <a href={item.source_url} target="_blank"
                       rel="noreferrer noopener">
                      {item.source_name} ↗
                    </a>
                  </td>
                  <td className="mono muted-text" style={{ fontSize: 11 }}>
                    {item.published_at || "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted-text" style={{ fontSize: 11 }}>
            来源分布：
            {(data.kinds ?? []).map((item) =>
              `${item.kind} ${item.count}`).join(" · ")}
            {" "}· 抓取任务：<code>scripts/quant_cases.py</code>
            （支持 GitHub / arXiv / CSDN；雪球因需 JS 过风控已禁用并记录原因）
          </p>
          <p className="muted-text" style={{ fontSize: 11 }}>
            {data.disclaimer}
          </p>
        </>
      )}
    </section>
  );
}
