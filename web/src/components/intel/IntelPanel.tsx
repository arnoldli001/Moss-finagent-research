/**
 * 情报中心（舆情情报页签的主面板）。
 *
 * ## 三个子页签，为什么这样分
 *
 * | 子页签 | 回答的问题 | 数据来源 |
 * |---|---|---|
 * | 情报流 | "最近有什么公开信息" | `GET /intel/feed` |
 * | 舆情热度监控 | "这些信息在**结构上**长什么样" | 同上（换聚合维度） |
 * | 投资日历 | "**接下来**会发生什么" | `GET /intel/calendar` |
 *
 * 用户口径（2026-09-25）："金融数据源讲究时效性，讲究预期差，
 * 未来的预期事件才更重要。" —— 所以「投资日历」是与情报流**平级**的
 * 子页签，而不是塞在某个角落的小组件。它是这一页里唯一向前看的部分。
 *
 * ## 加载策略：三个接口各自独立、失败互不牵连
 *
 * `feed` / `calendar` / `health` 并发拉取，**任一失败只影响它自己那块**。
 * 不做"全部成功才渲染" —— 日历接口挂了不该让整个情报中心变白屏。
 * 每块自己显示错误与重试。
 *
 * ## 同步/异步的取舍
 *
 * `feed` 每次打开都会真去聚合六个源（实测 2~5s），所以：
 *   · 首次挂载拉一次；
 *   · **不轮询**（用户没要求实时，且每 2 小时才采集一次，轮询无意义）；
 *   · 提供手动「刷新」按钮。
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  CalendarResult, IntelFeed, SourceHealth,
  fetchIntelCalendar, fetchIntelFeed, fetchIntelHealth,
} from "../../intelApi";
import IntelCalendarTab from "./IntelCalendarTab";
import IntelFeedTab from "./IntelFeedTab";
import IntelHeatTab from "./IntelHeatTab";

type SubTab = "feed" | "heat" | "calendar";

const SUB_TABS: Array<{ key: SubTab; label: string; hint: string }> = [
  { key: "feed", label: "情报流", hint: "按时间倒序的公开信息聚合" },
  { key: "heat", label: "舆情热度监控", hint: "类型结构与按日分布" },
  { key: "calendar", label: "投资日历", hint: "未来日程与预期差" },
];

export default function IntelPanel({ isAdmin = false }: { isAdmin?: boolean }) {
  const [tab, setTab] = useState<SubTab>("feed");
  const [feed, setFeed] = useState<IntelFeed | null>(null);
  const [cal, setCal] = useState<CalendarResult | null>(null);
  const [health, setHealth] = useState<SourceHealth | null>(null);
  const [errFeed, setErrFeed] = useState("");
  const [errCal, setErrCal] = useState("");
  const [loading, setLoading] = useState(false);
  // 卸载后不再 setState（切页签时请求可能还在飞）
  const alive = useRef(true);

  const load = useCallback(async () => {
    setLoading(true);
    setErrFeed("");
    setErrCal("");
    // 并发、各自独立 settle —— 用 allSettled 而不是 all：
    // 一个接口挂掉不能连累另外两块。
    const [f, c, h] = await Promise.allSettled([
      fetchIntelFeed({ limit: 60 }),
      fetchIntelCalendar(45),
      fetchIntelHealth(),
    ]);
    if (!alive.current) return;
    if (f.status === "fulfilled") setFeed(f.value);
    else setErrFeed(errText(f.reason));
    if (c.status === "fulfilled") setCal(c.value);
    else setErrCal(errText(c.reason));
    if (h.status === "fulfilled") setHealth(h.value);
    setLoading(false);
  }, []);

  useEffect(() => {
    alive.current = true;
    void load();
    return () => { alive.current = false; };
  }, [load]);

  const degraded = (feed?.degraded ?? false) || (cal?.degraded ?? false);

  return (
    <div className="intel-root">
      {/* ── 页头 ── */}
      <div className="intel-head">
        <div className="intel-title">
          <h2>情报中心</h2>
          <span className="muted-text">
            公开信息聚合 · 统计量 · 公式参考
          </span>
        </div>
        <div className="intel-head-right">
          {health && (
            <span
              className={`intel-chip ${health.state === "healthy" ? "ok" : "warn"}`}
              title="采集健康度为聚合口径，不区分具体来源"
            >
              {health.state === "healthy" ? "采集正常" : "采集部分异常"}
            </span>
          )}
          {degraded && (
            <span className="intel-chip warn" title="部分来源当前暂无更新">
              数据不完整
            </span>
          )}
          <button className="intel-refresh" onClick={() => void load()}
            disabled={loading}>
            {loading ? "刷新中…" : "↻ 刷新"}
          </button>
        </div>
      </div>

      {/* ── 合规横幅：固定展示，不可关闭 ──
          为什么放最上面而不是页脚：这条决定了整页内容该怎么读 ——
          用户要先知道"这里没有买卖建议"，才不会把统计量当结论。 */}
      <div className="intel-compliance">
        <b>本平台只做三件事：聚合公开信息 · 输出统计量 · 公开计算公式。</b>
        不含证券投资咨询，不推荐具体证券，不给出买卖时机与目标价。
        页面中任何倾向性描述<b>仅为对第三方原文语气的归类统计</b>，
        不是平台判断。全部内容不构成投资建议。
      </div>

      {/* ── 子页签：手机上横向滑动 ── */}
      <div className="intel-subtabs" role="tablist" aria-label="情报中心子页签">
        {SUB_TABS.map((t) => (
          <button
            key={t.key}
            role="tab"
            aria-selected={tab === t.key}
            title={t.hint}
            className={`intel-subtab${tab === t.key ? " on" : ""}`}
            onClick={() => setTab(t.key)}
          >
            {t.label}
            {t.key === "feed" && feed && (
              <span className="intel-subtab-n">
                {Object.values(feed.counts ?? {}).reduce((a, b) => a + b, 0)}
              </span>
            )}
            {t.key === "calendar" && cal && (
              <span className="intel-subtab-n">{cal.events?.length ?? 0}</span>
            )}
          </button>
        ))}
      </div>

      {/* ── 内容 ── */}
      <div className="intel-body">
        {tab === "feed" && (
          errFeed ? (
            <ErrorBlock msg={errFeed} onRetry={() => void load()} />
          ) : feed ? (
            <IntelFeedTab feed={feed} />
          ) : (
            <LoadingBlock label="正在聚合公开信息…" />
          )
        )}

        {tab === "heat" && (
          errFeed ? (
            <ErrorBlock msg={errFeed} onRetry={() => void load()} />
          ) : feed ? (
            <IntelHeatTab feed={feed} />
          ) : (
            <LoadingBlock label="正在聚合公开信息…" />
          )
        )}

        {tab === "calendar" && (
          errCal ? (
            <ErrorBlock msg={errCal} onRetry={() => void load()} />
          ) : cal ? (
            <IntelCalendarTab cal={cal} />
          ) : (
            <LoadingBlock label="正在读取日程…" />
          )
        )}
      </div>

      {/* 管理员专属运维提示：来源授权过期之类**只在这里出现**，
          用户侧永远看不到（用户侧只显示"暂无更新"）。 */}
      {isAdmin && (feed?.admin_hints?.length ?? 0) > 0 && (
        <div className="intel-admin-hints">
          <b>管理员提示</b>
          <ul>
            {(feed?.admin_hints ?? []).map((h, i) => <li key={i}>{h}</li>)}
          </ul>
        </div>
      )}
    </div>
  );
}

function LoadingBlock({ label }: { label: string }) {
  return (
    <div className="intel-loading">
      <span className="spinner" /> {label}
    </div>
  );
}

function ErrorBlock({ msg, onRetry }: { msg: string; onRetry: () => void }) {
  return (
    <div className="info-box intel-error">
      <div>{msg}</div>
      <button className="intel-refresh" onClick={onRetry}>重试</button>
    </div>
  );
}

/** `unknown` 的 reason → 可读文本（不 JSON.stringify 未知对象）。 */
function errText(reason: unknown): string {
  if (reason instanceof Error) return reason.message;
  if (typeof reason === "string") return reason;
  return "加载失败，请稍后重试";
}
