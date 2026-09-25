/**
 * 投资日历：预约披露 / 限售解禁 / 宏观发布 / 交易日。
 *
 * ## 这个页签真正要解决的问题：**预期差**
 *
 * 用户口径（2026-09-25）："金融数据源讲究时效性，讲究预期差，
 * 未来的预期事件才更重要。"
 *
 * 所以宏观条目不是"列一个日期"，而是要回答三个问题：
 *   ① 哪天公布（这叫**时效**，由官方日程表给，不会错）
 *   ② 市场预期多少、上期多少（这叫**基准**）
 *   ③ 两者之差（这才是**预期差**）
 *
 * ## 四态必须都显示，而且不能说谎
 *
 * 实测数据现实：551 行里 **前值 439 条、预期只有 40 条**。
 * 也就是说"只有前值"才是常态，不是异常。所以：
 *
 * | 态 | 显示 | 为什么这么显示 |
 * |---|---|---|
 * | `pending` | 待公布 | 数据还没出 |
 * | `prior_only` | 仅前值 + "暂无一致预期值" | 常态。**绝不填 0 冒充预期** |
 * | `released` | 已公布 | 出了但没有预期值可比，**不计算预期差** |
 * | `surprise` | 公布 / 预期 / 差值 | 唯一能算预期差的情形 |
 *
 * ## `src_date`：不标就是误导
 *
 * 数据源的日期口径与官方日程**不一致**（实测源把 9 月 CPI 记在 10-14，
 * 官方日程是 10-09）。后端已经过滤掉差太远的，但差 ≤2 天时仍会采用，
 * 并用 `metrics.src_date` 标明"这个值其实是哪一期的"。
 * 界面必须把它显示出来 —— 否则用户看到"前值 49.8"会以为是本期的。
 */

import { useMemo, useState } from "react";
import {
  CALENDAR_KINDS, CalendarEvent, CalendarResult, EXPECTATION_STATES,
  UnlockStock, daysBetween, fmtMoney, fmtNum, fmtPct, fmtSigned, formatTime,
  todayKey,
} from "../../intelApi";

/** 日历里的分组顺序（按"用户关心程度"排，不是按字母）。 */
const KIND_ORDER = ["macro", "earnings", "unlock", "trade_day"];

/**
 * 会**成规模重复**的类型 —— 同一天同名事件要合并成一行。
 *
 * 实测（45 天窗口）：预约披露 **2320 条**（约 47 家/天）、解禁 26 条、
 * 宏观 7 条。一条一行的话第一屏会被 47 行"预约披露"占满，而真正需要
 * 提前知道的宏观日程要往下翻几屏 —— 那正好把"未来预期事件更重要"
 * 这个目的做反了。
 *
 * 宏观**不合并**：每条都有各自的预期/前值/预期差，合并就把信息抹掉了。
 */
const BULK_KINDS = new Set(["earnings", "unlock"]);

function kindInfo(kind: string) {
  return CALENDAR_KINDS[kind] ?? { label: kind || "其他", tone: "gray" };
}

/** 相对今天的天数 → 人话。`null`（解析失败）不猜。 */
function relLabel(date: string, today: string): string {
  const d = daysBetween(today, date);
  if (d === null) return "";
  if (d === 0) return "今天";
  if (d === 1) return "明天";
  if (d === 2) return "后天";
  if (d > 0) return `${d} 天后`;
  return `${-d} 天前`;
}

/**
 * 一条宏观事件的指标区。
 *
 * 返回 `null` 表示"这条没有可展示的数值" —— 调用方据此不渲染指标块，
 * 而不是渲染一排 `—`（那看着像"取数失败"，实际是"本来就没有"）。
 */
function MacroMetrics({ metrics }: { metrics: CalendarEvent["metrics"] }) {
  const state = String(metrics.state ?? "");
  const st = EXPECTATION_STATES[state];
  const exp = metrics.expected;
  const prev = metrics.previous;
  const pub = metrics.published;
  const sur = metrics.surprise;

  const hasAny = [exp, prev, pub, sur].some(
    (v) => v !== null && v !== undefined);
  if (!hasAny && !st) return null;

  return (
    <div className="cal-metrics">
      {st && (
        <span className={`cal-state ${st.tone}`} title={st.hint}>
          {st.label}
        </span>
      )}
      <dl className="cal-nums">
        {prev !== null && prev !== undefined && (
          <div className="cal-num">
            <dt>上期</dt>
            <dd className="code">{fmtNum(prev)}</dd>
          </div>
        )}
        {exp !== null && exp !== undefined && (
          <div className="cal-num">
            <dt>一致预期</dt>
            <dd className="code">{fmtNum(exp)}</dd>
          </div>
        )}
        {pub !== null && pub !== undefined && (
          <div className="cal-num">
            <dt>公布</dt>
            <dd className="code strong">{fmtNum(pub)}</dd>
          </div>
        )}
        {/* 预期差只在**同时有预期与公布**时才有意义（`surprise` 态） */}
        {sur !== null && sur !== undefined && (
          <div className="cal-num">
            <dt>预期差</dt>
            <dd
              className={`code strong ${
                Number(sur) > 0 ? "up" : Number(sur) < 0 ? "down" : ""
              }`}
            >
              {fmtSigned(sur)}
            </dd>
          </div>
        )}
      </dl>
      {/* 数据来源日期与官方日程不一致时必须标明 */}
      {metrics.src_date && (
        <div className="cal-srcdate" title="该项数值取自该日期的数据，与上面公布日程不是同一天">
          数值日期 {String(metrics.src_date).slice(0, 10)}
        </div>
      )}
      {!exp && !pub && state === "prior_only" && (
        <div className="cal-note">暂无一致预期值，只有上一期可比数据</div>
      )}
    </div>
  );
}

function EventCard({ ev, today }: { ev: CalendarEvent; today: string }) {
  const info = kindInfo(ev.kind);
  const rel = relLabel(ev.date, today);
  const scope = ev.scope ?? {};
  const count = Number(scope.company_count ?? 0);
  const isMacro = ev.kind === "macro" || ev.kind === "fed";

  return (
    <article className="cal-row">
      <div className="cal-date">
        <span className="cal-day code">{ev.date.slice(5)}</span>
        {/* 相对日只在 7 天内显示 —— 更远的"23 天后"没有决策价值，纯噪声 */}
        {rel && Math.abs(daysBetween(today, ev.date) ?? 99) <= 7 && (
          <span className="cal-rel">{rel}</span>
        )}
      </div>
      <div className="cal-body">
        <div className="cal-line">
          <span className={`cal-kind ${info.tone}`}>{info.label}</span>
          <span className="cal-title">{ev.title}</span>
          {/* `certainty`：交易日/官方日程是 rule（不会变），预约是可改期 */}
          {ev.certainty === "scheduled" && ev.kind !== "trade_day" && (
            <span className="cal-certain" title="预约日期，可能改期">可改期</span>
          )}
        </div>

        {isMacro ? (
          <MacroMetrics metrics={ev.metrics ?? {}} />
        ) : (
          <div className="cal-scope">
            {count > 0 && <span>{count} 家</span>}
            {(scope.industries?.length ?? 0) > 0 && (
              <span>{(scope.industries ?? []).slice(0, 4).join(" · ")}</span>
            )}
            {(scope.codes?.length ?? 0) > 0 && (
              <span className="code">
                {(scope.codes ?? []).slice(0, 5).join(" ")}
                {(scope.codes?.length ?? 0) > 5
                  ? ` 等${scope.codes?.length}只` : ""}
              </span>
            )}
            {count === 0
              && !(scope.industries?.length)
              && !(scope.codes?.length) && (
              <span className="muted-text">覆盖范围统计暂无</span>
            )}
          </div>
        )}

        {/* 改期记录：预约类才有，是"这个日期变过"的证据 */}
        {ev.changes?.length > 0 && (
          <div className="cal-changes">
            {ev.changes.slice(0, 3).map((c, i) => (
              <span className="cal-change" key={i}>
                {Object.entries(c).map(([k, v]) => `${k}: ${v}`).join(" · ")}
              </span>
            ))}
          </div>
        )}
      </div>
    </article>
  );
}

/**
 * 同一类型同一天的**批量条目** —— 合并成一行，可展开看明细。
 *
 * 为什么必须合并：预约披露 45 天 2320 条（≈47 家/天）。一条一行的话，
 * 日历会被同一句话刷屏，而用户真正需要提前知道的宏观日程被埋到下面。
 * 合并后一天一行，展开才列出具体标的。
 *
 * ⚠️ 这里只做**展示折叠**，不改任何数据 —— 展开后拿到的是完整列表。
 */
function BulkRow({ kind, events, today }: {
  kind: string;
  events: CalendarEvent[];
  today: string;
}) {
  const [open, setOpen] = useState(false);
  const info = kindInfo(kind);
  const first = events[0];
  const rel = relLabel(first.date, today);
  // 汇总覆盖范围：家数取各条之和（同一天内不会重复计同一家）
  const totalCount = events.reduce(
    (n, e) => n + Number(e.scope?.company_count ?? 0), 0);
  const codes = [...new Set(events.flatMap((e) => e.scope?.codes ?? []))];
  const industries = [...new Set(events.flatMap(
    (e) => e.scope?.industries ?? []))];
  const changed = events.filter((e) => (e.changes?.length ?? 0) > 0).length;

  // ── 解禁：把**市值合计**与**个股明细**都拿出来 ──
  //
  // 用户报障（2026-09-25）："限售股解禁 显示15家，点开却没有股票名称和
  // 解禁市值或占流通股百分比等信息"。根因不在前端 —— 后端当时用的
  // `stock_restricted_release_summary_em` 是**按日汇总**接口，
  // 个股名与占比在那个接口里根本不存在。后端改用个股明细接口后，
  // 这里才有东西可渲染。
  const unlock = useMemo(() => {
    const stocks: UnlockStock[] = [];
    let cap = 0;
    let hasCap = false;
    for (const e of events) {
      const c = Number(e.metrics?.unlock_market_cap);
      if (Number.isFinite(c)) { cap += c; hasCap = true; }
      for (const s of (e.scope?.stocks ?? []) as UnlockStock[]) {
        stocks.push(s);
      }
    }
    // 按市值倒序：解禁影响最大的是最大的那几只
    stocks.sort((a, b) => (b.market_cap ?? 0) - (a.market_cap ?? 0));
    return { stocks, cap: hasCap ? cap : null };
  }, [events]);

  const isUnlock = kind === "unlock";

  return (
    <article className={`cal-row bulk${open ? " open" : ""}`}>
      <div className="cal-date">
        <span className="cal-day code">{first.date.slice(5)}</span>
        {rel && Math.abs(daysBetween(today, first.date) ?? 99) <= 7 && (
          <span className="cal-rel">{rel}</span>
        )}
      </div>
      <div className="cal-body">
        <div className="cal-line">
          <span className={`cal-kind ${info.tone}`}>{info.label}</span>
          <span className="cal-title">
            {isUnlock && unlock.cap !== null
              ? `解禁市值 ${fmtMoney(unlock.cap)}`
              : events.length > 1
                ? `${events.length} 条`
                : (first.title || info.label)}
          </span>
          {totalCount > 0 && <span className="cal-bulk-n">{totalCount} 家</span>}
          {changed > 0 && (
            <span className="cal-certain" title="有改期记录">
              {changed} 条改期
            </span>
          )}
          <button
            className="cal-bulk-toggle"
            aria-expanded={open}
            onClick={() => setOpen((v: boolean) => !v)}
          >
            {open ? "收起" : "展开明细"}
          </button>
        </div>

        {industries.length > 0 && (
          <div className="cal-scope">
            <span>{industries.slice(0, 6).join(" · ")}</span>
          </div>
        )}

        {/* ── 解禁明细：股票名 / 解禁市值 / 占流通比 / 限售股类型 ── */}
        {open && isUnlock && unlock.stocks.length > 0 && (
          <ul className="cal-stocks">
            {unlock.stocks.map((s) => (
              <li key={`${s.code}-${s.share_type}`}>
                <span className="cal-stock-code code">{s.code}</span>
                <span className="cal-stock-name">{s.name || "—"}</span>
                <span className="cal-stock-cap code">{fmtMoney(s.market_cap)}</span>
                <span
                  className="cal-stock-pct code"
                  title="占解禁前流通市值比例（可大于 100%：解禁量可超过原流通盘）"
                >
                  {fmtPct(s.pct_of_float)}
                </span>
                <span className="cal-stock-type muted-text">
                  {s.share_type || ""}
                </span>
              </li>
            ))}
          </ul>
        )}

        {/* ── 其它批量类型（预约披露）沿用原来的清单 ── */}
        {open && !isUnlock && (
          <ul className="cal-bulk-list">
            {events.map((e) => (
              <li key={e.event_id}>
                <span className="cal-bulk-title">{e.title}</span>
                {(e.scope?.codes?.length ?? 0) > 0 && (
                  <span className="code">
                    {(e.scope?.codes ?? []).slice(0, 6).join(" ")}
                  </span>
                )}
                {(e.changes?.length ?? 0) > 0 && (
                  <span className="cal-certain">已改期</span>
                )}
              </li>
            ))}
          </ul>
        )}

        {/* 折叠时给一句"里面有多少标的"，免得用户以为没数据 */}
        {!open && codes.length > 0 && (
          <div className="cal-scope">
            <span className="code">
              {codes.slice(0, 8).join(" ")}
              {codes.length > 8 ? ` 等 ${codes.length} 只` : ""}
            </span>
          </div>
        )}
      </div>
    </article>
  );
}

export default function IntelCalendarTab({ cal }: { cal: CalendarResult }) {
  const today = todayKey();

  /**
   * 按日期分组，并按"类型"给分组内排序。
   *
   * 组内顺序固定为 宏观 → 披露 → 解禁 → 交易日：同一天里宏观数据
   * 影响面最大，先看它。这个顺序是**展示约定**，不改变任何数据。
   */
  const groups = useMemo(() => {
    const byDay = new Map<string, CalendarEvent[]>();
    for (const ev of cal.events ?? []) {
      const d = ev.date || "";
      if (!d) continue;
      const arr = byDay.get(d);
      if (arr) arr.push(ev);
      else byDay.set(d, [ev]);
    }
    return [...byDay.entries()]
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([day, evs]) => {
        // 先按类型归堆：大批量类型（披露/解禁）合并成一行，
        // 其余（宏观、交易日）逐条展示。
        const bulked = new Map<string, CalendarEvent[]>();
        const singles: CalendarEvent[] = [];
        for (const ev of evs) {
          if (!BULK_KINDS.has(ev.kind)) {
            singles.push(ev);
            continue;
          }
          const arr = bulked.get(ev.kind);
          if (arr) arr.push(ev);
          else bulked.set(ev.kind, [ev]);
        }
        type Row =
          | { t: "one"; ev: CalendarEvent; k: number }
          | { t: "bulk"; kind: string; evs: CalendarEvent[]; k: number };
        const rows: Row[] = [
          ...singles.map((ev) => ({
            t: "one" as const, ev, k: KIND_ORDER.indexOf(ev.kind),
          })),
          ...[...bulked.entries()].map(([kind, list]) => ({
            t: "bulk" as const, kind, evs: list, k: KIND_ORDER.indexOf(kind),
          })),
        ];
        // 展示约定：宏观最前，其次披露、解禁、交易日（未知类型排最后）
        rows.sort((a, b) => (a.k < 0 ? 99 : a.k) - (b.k < 0 ? 99 : b.k));
        return {
          day,
          label: relLabel(day, today) || day,
          rows,
          // 分组标题上的计数用**原始条数**（不是行数），否则"47 家披露"
          // 会显示成"1 项"，用户会以为数据丢了
          rawCount: evs.length,
        };
      });
  }, [cal.events, today]);

  // 未来 7 天的条数 —— 用户最关心的窗口，做成一排统计
  const soon = (cal.events ?? []).filter((ev) => {
    const d = daysBetween(today, ev.date);
    return d !== null && d >= 0 && d <= 7;
  }).length;

  return (
    <div className="intel-cal">
      <div className="cal-summary">
        <div className="cal-sum-item">
          <div className="cal-sum-lab">未来 7 日</div>
          <div className="cal-sum-val code">{soon}<small>项</small></div>
        </div>
        <div className="cal-sum-item">
          <div className="cal-sum-lab">本窗口共</div>
          <div className="cal-sum-val code">{(cal.events ?? []).length}<small>项</small></div>
          {/* 2356 项里 2320 是预约披露 —— 不说明的话这个数字很费解 */}
          <div className="cal-sum-fo">含预约披露等批量条目</div>
        </div>
        <div className="cal-sum-item grow">
          <div className="cal-sum-lab">抓取于</div>
          <div className="cal-sum-val small">
            {formatTime(cal.fetched_at, { withDate: true })}
          </div>
        </div>
      </div>

      {/* ⚠️ 交易日历**刻意无备源**：交易日是交易所规则，不存在"第二个可信
          来源"，用不可信的日历会让整个调度在错误的日子跑。这里如实说明，
          免得用户以为是漏接了备源。 */}
      <div className="cal-note-bar muted-text">
        宏观日程来自官方发布日程表（确定性高）；预约披露与解禁为预约类，
        日期可能变更，已标注改期记录。交易日为交易所规则，单一来源。
      </div>

      {groups.length === 0 && (
        <div className="empty-tip muted-text">该时间窗口内没有日程</div>
      )}

      {groups.map((g) => (
        <section className="cal-group" key={g.day}>
          <div className="cal-group-head">
            <span className="cal-group-day code">{g.day}</span>
            <span className="cal-group-rel">{g.label}</span>
            <span className="muted-text">{g.rawCount} 项</span>
          </div>
          {g.rows.map((r) => (
            r.t === "one"
              ? <EventCard ev={r.ev} today={today} key={r.ev.event_id} />
              : <BulkRow kind={r.kind} events={r.evs} today={today}
                  key={`${g.day}-${r.kind}`} />
          ))}
        </section>
      ))}

      {cal.degraded && (
        <div className="intel-gaps">
          {(cal.gaps ?? []).map((g, i) => (
            <span className="intel-gap" key={i}>
              {g.message || "部分日程来源当前不可用"}
            </span>
          ))}
        </div>
      )}

      {cal.disclaimer && (
        <p className="disclaimer">{cal.disclaimer}</p>
      )}
    </div>
  );
}
