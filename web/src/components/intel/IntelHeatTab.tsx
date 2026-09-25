/**
 * 舆情热度监控。
 *
 * ## 这个页签刻意**只放能算出来的东西**
 *
 * 设计稿（`docs/INTEL_CENTER_REDESIGN.md` §7 页 3）原来列了 6 张 KPI：
 * 全市场热度 / 多空比 / 情绪分歧度 / 高可信占比 / 来源集中度 / 异常放大。
 * 其中**多空比、情绪分歧度、异常放大、可信度**都依赖 P1 的分层打分与
 * "原文倾向"抽取（`credibility` / `tone` 字段），当前接口**不产出**。
 *
 * 所以这里不去"估一个数"充数 —— 那正是本项目明令禁止的做法
 * （"数据缺失时置 None 并记 degraded，绝不用 0 或中性值冒充"）。
 * 只展示**既有数据能直接支撑**的统计量，并把缺的那些**如实说明**，
 * 等 P1 落地后再补。宁可页面薄一点，也不要一排看着专业、实则编造的指标。
 *
 * ## 为什么不做"来源集中度"
 *
 * `source_alias` 是稳定假名，**能**用来算集中度。但把它显示出来
 * （哪怕是"共 5 个来源，最大占比 62%"）就是在交代我们聚合了几个渠道。
 * 这个信息对用户没有决策价值，对我们却是壁垒，所以不做。
 */

import { useMemo } from "react";
import { IntelFeed, dayKey, formatTime, todayKey } from "../../intelApi";

/** 类型 → 中文名（后端已给 `kind_label`，这里只用于固定顺序）。 */
const KIND_ORDER = ["newswire", "broker_report", "research_note", "policy"];

/**
 * 类型 → 中文名的**完整兜底表**。
 *
 * ⚠️ 为什么不能只从 `feed.items` 里现场建映射：`items` 是**限量后**的一页
 * （默认 60 条），而 `counts` 是去重后的**全量**计数。某个类型在这一页里
 * 一条都没有时，从 `items` 里就找不到它的 label，于是界面上出现了
 * `research_...`（原始机器名被 `?? key` 兜底透出来了）。
 *
 * 这张表与后端 `intel_sources.SOURCE_KINDS` 保持一致；
 * `feed.items` 里带回来的 `kind_label` 优先（后端改了这边自动跟随）。
 */
const KIND_LABELS: Record<string, string> = {
  newswire: "财经快讯",
  broker_report: "券商研报",
  policy: "政策信号",
  research_note: "研究笔记",
  other: "其他",
};

type Bucket = { key: string; label: string; n: number };

export default function IntelHeatTab({ feed }: { feed: IntelFeed }) {
  const items = feed.items ?? [];
  const today = todayKey();

  /** 类型分布（用后端**全量计数**，不是当前页条数）。 */
  const byKind: Bucket[] = useMemo(() => {
    // label 优先用 `items` 里后端给的 `kind_label`；这一页没有该类型时
    // 退回本地兜底表（否则会显示成 `research_note` 这种机器名）。
    const fromItems: Record<string, string> = {};
    for (const it of items) fromItems[it.kind] = it.kind_label;
    const counts = feed.counts ?? {};
    return Object.entries(counts)
      .map(([key, n]) => ({
        key,
        n,
        label: fromItems[key] ?? KIND_LABELS[key] ?? key,
      }))
      .sort((a, b) => {
        const ia = KIND_ORDER.indexOf(a.key);
        const ib = KIND_ORDER.indexOf(b.key);
        if (ia !== ib) return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
        return b.n - a.n;
      });
  }, [feed.counts, items]);

  const total = byKind.reduce((a, b) => a + b.n, 0);

  /**
   * 按天分布 —— 这回答"采集节奏是否正常"。
   *
   * 用 `published_at` 的**日期部分**分桶，而不是小时：源的时间戳精度不一
   * （快讯常常只有日期没有时间），按小时分桶会得到一根虚假的尖峰
   * （所有"只有日期"的条目都掉进 00:00）。
   */
  const byDay = useMemo(() => {
    const map = new Map<string, number>();
    for (const it of items) {
      const d = dayKey(it.published_at);
      if (!d) continue;
      map.set(d, (map.get(d) ?? 0) + 1);
    }
    return [...map.entries()].sort(([a], [b]) => b.localeCompare(a));
  }, [items]);

  /** 被提及的标的/行业（谁被提得多）—— 这是纯计数，可复核。 */
  const mentions = useMemo(() => {
    const codeN = new Map<string, number>();
    const indN = new Map<string, number>();
    for (const it of items) {
      for (const c of it.codes ?? []) {
        if (c) codeN.set(c, (codeN.get(c) ?? 0) + 1);
      }
      if (it.industry) indN.set(it.industry, (indN.get(it.industry) ?? 0) + 1);
    }
    const top = (m: Map<string, number>, k: number) =>
      [...m.entries()].sort((a, b) => b[1] - a[1]).slice(0, k);
    return { codes: top(codeN, 12), industries: top(indN, 12) };
  }, [items]);

  const todayN = byDay.find(([d]) => d === today)?.[1] ?? 0;
  const maxDay = Math.max(1, ...byDay.map(([, n]) => n));
  const maxKind = Math.max(1, ...byKind.map((b) => b.n));

  /**
   * 原文倾向分布（第三步的产物）。
   *
   * ⚠️ **分母只算 `has_tone` 的条目** —— 把"未定"算进多空比，
   * 等于替用户做了一个我们并不确定的判断。所以这里的三个数加起来
   * 会**小于**总条数，那是正常的，界面上要说明。
   */
  const toneDist = feed.tone_dist ?? {};
  const bull = toneDist["偏多"] ?? 0;
  const bear = toneDist["偏空"] ?? 0;
  const neutral = toneDist["中性"] ?? 0;
  const toned = bull + bear + neutral;
  const pct = (n: number) => (toned > 0 ? Math.round((n / toned) * 100) : null);

  return (
    <div className="intel-heat">
      <div className="heat-kpis">
        <div className="heat-kpi">
          <div className="heat-lab">本窗口条目</div>
          <div className="heat-val code">{total}<small>条</small></div>
          <div className="heat-fo">去重后全量计数</div>
        </div>
        <div className="heat-kpi">
          <div className="heat-lab">今日新增</div>
          <div className="heat-val code">{todayN}<small>条</small></div>
          <div className="heat-fo">{today}</div>
        </div>
        <div className="heat-kpi">
          <div className="heat-lab">覆盖天数</div>
          <div className="heat-val code">{byDay.length}<small>天</small></div>
          <div className="heat-fo">
            {byDay.length > 0 ? `${byDay[byDay.length - 1][0]} 起` : "—"}
          </div>
        </div>
        <div className="heat-kpi">
          <div className="heat-lab">研报占比</div>
          <div className="heat-val code">
            {total > 0
              ? `${Math.round(((byKind.find((b) => b.key === "broker_report")?.n ?? 0) / total) * 100)}`
              : "—"}
            <small>%</small>
          </div>
          <div className="heat-fo">券商研报 / 全部条目</div>
        </div>
      </div>

      <div className="heat-cols">
        <section className="heat-card">
          <h3>来源类型结构</h3>
          <p className="heat-hint muted-text">
            按<b>类型</b>统计。具体渠道不展示 —— 那不是使用者需要的信息。
          </p>
          <ul className="heat-bars">
            {byKind.map((b) => (
              <li key={b.key}>
                <span className="heat-bar-lab">{b.label}</span>
                <span className="heat-bar-track">
                  <span
                    className="heat-bar-fill"
                    style={{ width: `${Math.round((b.n / maxKind) * 100)}%` }}
                  />
                </span>
                <span className="heat-bar-n code">{b.n}</span>
              </li>
            ))}
            {byKind.length === 0 && (
              <li className="muted-text">暂无数据</li>
            )}
          </ul>
        </section>

        <section className="heat-card">
          <h3>按日分布</h3>
          <p className="heat-hint muted-text">
            用来判断<b>采集节奏</b>是否连续。只按日期分桶 —— 部分来源的时间戳
            只到日精度，按小时分桶会造出一根假尖峰。
          </p>
          <ul className="heat-bars">
            {byDay.slice(0, 14).map(([d, n]) => (
              <li key={d}>
                <span className="heat-bar-lab code">{d.slice(5)}</span>
                <span className="heat-bar-track">
                  <span
                    className="heat-bar-fill alt"
                    style={{ width: `${Math.round((n / maxDay) * 100)}%` }}
                  />
                </span>
                <span className="heat-bar-n code">{n}</span>
              </li>
            ))}
            {byDay.length === 0 && <li className="muted-text">暂无数据</li>}
          </ul>
        </section>
      </div>

      <section className="heat-card">
        <h3>被提及的标的与行业</h3>
        <p className="heat-hint muted-text">
          纯计数：这些标的/行业在当前窗口的条目里<b>被提到过几次</b>。
          提及次数不代表任何倾向 —— 倾向属于原文，不属于本平台。
        </p>
        {mentions.industries.length > 0 && (
          <div className="heat-tags">
            <span className="heat-tags-lab">行业</span>
            {mentions.industries.map(([name, n]) => (
              <span className="heat-tag" key={name}>
                {name}<b>{n}</b>
              </span>
            ))}
          </div>
        )}
        {mentions.codes.length > 0 ? (
          <div className="heat-tags">
            <span className="heat-tags-lab">标的</span>
            {mentions.codes.map(([code, n]) => (
              <span className="heat-tag code" key={code}>
                {code}<b>{n}</b>
              </span>
            ))}
          </div>
        ) : (
          <div className="muted-text" style={{ fontSize: 12 }}>
            当前窗口的条目里没有可提取的证券代码（快讯类通常没有）。
          </div>
        )}
      </section>

      <section className="heat-card">
        <h3>原文倾向分布</h3>
        <p className="heat-hint muted-text">
          这是<b>第三方原文自己的语气</b>归类，不是平台判断、不构成投资建议。
          分母是<b>已判定倾向的 {toned} 条</b>（未定的不计入 ——
          把"未定"算进多空比等于替用户做了一个我们并不确定的判断）。
        </p>
        {toned === 0 ? (
          <div className="muted-text" style={{ fontSize: 12 }}>
            当前窗口还没有已抽取倾向的条目。抽取是 2 小时一次的定时任务，
            新条目在下一批抽到之前没有倾向。
          </div>
        ) : (
          <>
            <ul className="heat-bars">
              {[["偏多", bull], ["中性", neutral], ["偏空", bear]].map(
                ([label, n]) => (
                  <li key={String(label)}>
                    <span className="heat-bar-lab">{label}</span>
                    <span className="heat-bar-track">
                      <span
                        className={`heat-bar-fill ${
                          label === "偏多" ? "tone-bull"
                            : label === "偏空" ? "tone-bear" : ""}`}
                        style={{
                          width: `${Math.round((Number(n) / Math.max(1, toned)) * 100)}%`,
                        }}
                      />
                    </span>
                    <span className="heat-bar-n code">{String(n)}</span>
                  </li>
                ))}
            </ul>
            <div className="heat-tone-sum">
              多空比 <b className="code">{bull} : {bear}</b>
              {pct(bull) !== null && (
                <span className="muted-text">
                  （偏多 {pct(bull)}% / 中性 {pct(neutral)}% / 偏空 {pct(bear)}%）
                </span>
              )}
            </div>
          </>
        )}
      </section>

      {/* 缺什么就说什么，不用占位数字糊过去 */}
      <section className="heat-card heat-todo">
        <h3>尚未提供的统计量</h3>
        <p className="muted-text">
          以下几项依赖尚未接入的能力，当前接口不产出这些字段，
          因此**不给估值**：
        </p>
        <ul className="heat-todo-list">
          <li><b>情绪分歧度 / 异常放大</b> —— 需要倾向的时序基线（先积累若干天）</li>
          <li><b>与平台信号交叉验证</b> —— 需要板块拥挤度/ETF 份额对齐</li>
        </ul>
        <p className="muted-text" style={{ marginTop: 6 }}>
          宁可这一页薄一点，也不用估算值充数。
        </p>
      </section>

      <div className="intel-foot muted-text">
        抓取于 {formatTime(feed.fetched_at, { withDate: true })}
        {feed.degraded && " · 数据不完整（部分来源暂无更新）"}
      </div>
    </div>
  );
}
