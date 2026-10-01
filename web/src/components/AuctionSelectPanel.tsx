import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  auctionApi,
  DIM_LABELS,
  DIM_MAX,
  type AuctionDetail,
  type AuctionFeature,
  type AuctionInfo,
  type AuctionPick,
  type AuctionRun,
  type AuctionSentimentCycle,
  type AuctionSeriesPoint,
  type AuctionState,
  type AuctionToday,
  type FermentingThemeCandidate,
  type ModeFit,
  type StageGuide,
  type WatchItem,
} from "../auctionApi";

/**
 * 竞价选股（量化交易 → 量化选股 右侧的页签）。
 *
 * ## 它和「量化选股」的分工
 *
 * - **量化选股**：日线级 3 档模型，选"未来 3 天可能涨 8%"的票（不限涨停）；
 * - **竞价选股**（本页签）：只看**昨日涨停**的票，读 9:15-9:25 的竞价微观结构，
 *   判断"今天开盘这一下资金还在不在"。开市日 9:25:00 自动跑，9:27 前出池。
 *
 * ## 布局（与需求一致）
 *
 * ① 运行状态条（调度是否在跑 / 今天跑了没有 / 是否按时）
 * ② 选股池（打分卡，点开看竞价曲线 + 特征项）
 * ③ 落选区（**只列已打分被否决**的票 + 否决原因，看得到"为什么没选它"）
 *    ⚠️ 前置筛选剔除的票**不在这里显示**（用户口径 2026-09-23）：它们没进打分链路，
 *    既无分数也无特征，混进来会被误读成"打过分但被否决了"。见下方 `vetoed` 的说明。
 * ④ 单只详情：竞价分时曲线 + K线 **下方**的综合打分（图一式）
 */

const POLL_MS = 30000;

function pct(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${value.toFixed(digits)}%`;
}

function num(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return value.toFixed(digits);
}

function yi(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${(value / 1e8).toFixed(digits)}亿`;
}

/** 分数配色：≥75 强、≥60 达标、其余弱。 */
function scoreTone(score: number): string {
  if (score >= 75) return "strong";
  if (score >= 60) return "ok";
  return "weak";
}

/**
 * 把"应该是数组"的字段安全地当数组用；不是数组就退化成空数组。
 *
 * ## 为什么需要它（2026-09-21 实测白屏）
 *
 * 后端有两张表喂同一个概念：`/today` 的 `features` 走 `_pick_to_api` 解开了
 * JSON 文本列，而 `/detail` 的 `feature` 曾是 `auction_feature` 的**原始行** ——
 * 于是 `rush_labels` 在 `/today` 是 `[]`、在 `/detail` 是字符串 `"[]"`。
 * `"[]" .join("、")` 抛 `TypeError: join is not a function`，
 * 而 React 没有错误边界 → **整棵树卸载 = 整页白屏**。
 *
 * 后端已统一解码（见 `_feature_to_api`），这里再兜一道：最差是这一格显示
 * "未打标"，不该让整个页面陪葬。新增此类字段时请一律走这个函数。
 */
function asArray<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

/**
 * 「发酵题材」标签副文案：最高板 / 家数 / 今日 9:25 家数 / 龙头。
 *
 * ⚠️ 为什么写成函数而不是直接塞进 JSX：`{c.highest:.0f}` 这种模板串写在 JSX
 * 文本里时，`:` 会被解析器当成标签语法报错（踩过）。搬出来之后 JSX 只剩简单表达式，
 * 也顺便让"数字怎么拼"这件事有一处可改。
 *
 * ⚠️ 数字必须带出来：样本只有几十只涨停股，只给题材名会被误读成
 * "全市场最热概念"（那不是这个判定的口径）。
 */
function fermentSummary(c: FermentingThemeCandidate): string {
  const parts: string[] = [];
  if (c.highest > 0) parts.push(`最高 ${Math.round(c.highest)} 板`);
  parts.push(`${c.count} 家`);
  if (c.today_count > 0) parts.push(`今9:25 ${c.today_count} 家`);
  if (c.leader_name) parts.push(`龙头 ${c.leader_name}`);
  return parts.join(" · ");
}

export default function AuctionSelectPanel() {
  const [info, setInfo] = useState<AuctionInfo | null>(null);
  const [state, setState] = useState<AuctionState | null>(null);
  const [today, setToday] = useState<AuctionToday | null>(null);
  const [loading, setLoading] = useState(false);
  /**
   * 「刷新」的**可见反馈**（用户口径 2026-09-22：点了看不出变化）。
   *
   * 为什么需要它：这个按钮原来只做三件事 —— 拉 `info`/`state`/`today`、
   * 替换状态、把 `loading` 转一下。而**数据没变时界面一模一样**：
   * 状态文字说的是调度器最近一次运行时间（`last_run_at`），刷新它当然不变；
   * 选股池在两次刷新之间也不会自己变。于是"点了没作用"是完全合理的感受。
   *
   * 加了时间戳之后，至少能回答"我刚才那次刷新到底成没成、拿回来的是几点的数据"。
   * 另外这也是个**诊断入口**：时间戳不往前走 = 请求根本没回来。
   */
  const [refreshedAt, setRefreshedAt] = useState<Date | null>(null);
  /**
   * 上次刷新拿到的交易日 + 池子只数：跨天/重跑后能一眼看出数据**变了**。
   *
   * ⚠️ 必须是 **ref**，不能是 state。`load` 是 `useCallback`，而挂载 effect 与
   * 轮询 effect 都以它为依赖 —— `snapshot` 一旦是 state 且进依赖数组，
   * 就会变成 load → setSnapshot → load 重建 → effect 重跑 → 再 load 的**自激循环**。
   * ref 不触发重建，读取到的还是最新值。
   */
  const snapshotRef = useRef<{ day: string; picked: number } | null>(null);
  /**
   * 情绪周期五线（大肉/大面/连板数/小周期/大周期，主板口径）。
   *
   * 与 `today` 分开拉：它是**日频**数据（本地行情仓收盘后落库），跟 9:25 那一轮
   * 选股结果无关；挂载时取一次即可，盘中没有变化的必要。
   */
  const [cycle, setCycle] = useState<AuctionSentimentCycle | null>(null);

  /** 拉情绪周期序列（图上的「↻ 刷新」也走它）。 */
  const loadCycle = useCallback(async () => {
    try {
      setCycle(await auctionApi.sentimentCycle(30));
    } catch {
      setCycle(null);      // 失败就不画图（图表本身会显示"暂不可用"）
    }
  }, []);

  /**
   * 拉关注列表。与 `load()` **分开**：它的数据来自 `/watch`（列表 + 最近一轮
   * 分数），而 `load()` 拉的是选股那一轮；把它并进 `load()` 会让每 30 秒的
   * 轮询多打一个接口，而关注列表只在用户加/删/重跑后才变。
   */
  const loadWatch = useCallback(async () => {
    try {
      const payload = await auctionApi.watch();
      setWatch(payload.items ?? []);
    } catch {
      // 关注列表取不到不该影响主面板（它是附加信息）
      setWatch([]);
    }
  }, []);
  /**
   * 最新一次拉到的 `run.finished_at`。
   *
   * ⚠️ 必须是 ref：`triggerRun` 在**轮询回调**里要比对"这一轮是不是新的"，
   * 而回调闭包捕获的是旧的 `run`。用 state 读不到最新值（闭包快照），
   * 且把 `run` 放进 `triggerRun` 依赖会让它每次都重建、把已排的定时器打乱。
   */
  const finishedAtRef = useRef<string>("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [detailCode, setDetailCode] = useState("");
  const [detail, setDetail] = useState<AuctionDetail | null>(null);
  const [showVetoed, setShowVetoed] = useState(false);
  const [running, setRunning] = useState(false);
  /** 当前阶段的 skill 操作指南（仓位/方向/风险）；取不到就是 null，不渲染这一块。 */
  const [guide, setGuide] = useState<StageGuide | null>(null);

  const toast = useCallback((text: string) => setNotice(text), []);

  /** 手动关注列表（用户口径 2026-09-22）：列表跨日保留，分数随每轮重跑更新。 */
  const [watch, setWatch] = useState<WatchItem[]>([]);
  const [watchCode, setWatchCode] = useState("");
  const [watchBusy, setWatchBusy] = useState(false);

  const addWatch = useCallback(async () => {
    const code = watchCode.trim();
    if (code.length !== 6) return;
    setWatchBusy(true);
    try {
      const out = await auctionApi.watchAdd(code);
      toast(out.message);
      setWatchCode("");
      await loadWatch();
    } catch (exc) {
      toast(`加入失败：${exc instanceof Error ? exc.message : exc}`);
    } finally {
      setWatchBusy(false);
    }
  }, [watchCode, toast, loadWatch]);

  const removeWatch = useCallback(async (code: string) => {
    setWatchBusy(true);
    try {
      const out = await auctionApi.watchRemove(code);
      toast(out.message);
      await loadWatch();
    } catch (exc) {
      toast(`移除失败：${exc instanceof Error ? exc.message : exc}`);
    } finally {
      setWatchBusy(false);
    }
  }, [toast, loadWatch]);

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(null), 9000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  const load = useCallback(async (options?: { manual?: boolean; silent?: boolean }) => {
    // `silent` = 轮询（每 30 秒）与手动刷新触发的补拉：**不**改 `loading`，
    // 否则按钮会每 30 秒就禁用一下、文案跳成"读取中…"，看着像坏了。
    if (!options?.silent) setLoading(true);
    setError("");
    try {
      const [infoPayload, statePayload, todayPayload] = await Promise.all([
        auctionApi.info(), auctionApi.state(), auctionApi.today(),
      ]);
      setInfo(infoPayload);
      setState(statePayload);
      setToday(todayPayload);
      setRefreshedAt(new Date());
      // 供 `triggerRun` 的轮询判断"这一轮是不是新的"（见 `finishedAtRef`）
      finishedAtRef.current = todayPayload.run?.finished_at ?? "";
      // 手动刷新时把"拿回来的是哪一天、几只"讲出来 —— 这是"有没有变化"的判据
      if (options?.manual) {
        const day = todayPayload.trade_date || "—";
        const n = (todayPayload.picked ?? []).length;
        const prev = snapshotRef.current;
        const changed = prev !== null && (prev.day !== day || prev.picked !== n);
        toast(changed
          ? `已刷新：${prev?.day ?? "—"} → ${day}，入池 ${prev?.picked ?? "—"} → ${n} 只`
          : `已刷新：${day} 入池 ${n} 只（与上次相同）`);
        snapshotRef.current = { day, picked: n };
      }
    } catch (exc) {
      setError(`读取竞价选股数据失败：${exc instanceof Error ? exc.message : exc}`);
    } finally {
      if (!options?.silent) setLoading(false);
    }
  }, [toast]);

  // 首次加载走 `loading`（按钮显示"读取中…"）；轮询走 `silent`
  useEffect(() => { void load(); }, [load]);

  // 关注列表面板：挂载时拉一次即可（加/删/重跑后由各自回调刷新）
  useEffect(() => { void loadWatch(); }, [loadWatch]);

  // 情绪周期五线：日频数据，挂载时取一次（图上有「↻ 刷新」可手动重取）
  useEffect(() => { void loadCycle(); }, [loadCycle]);

  // 低频兜底轮询：真正的运行发生在服务端 9:25，前端只负责把结果拉回来
  useEffect(() => {
    const timer = window.setInterval(() => void load({ silent: true }), POLL_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  // 打开详情时按需取该票的竞价序列与特征
  useEffect(() => {
    if (!detailCode) { setDetail(null); return; }
    let alive = true;
    auctionApi.detail(detailCode, today?.trade_date || "")
      .then((payload) => { if (alive) setDetail(payload); })
      .catch((exc) => {
        if (alive) setError(`读取 ${detailCode} 详情失败：${exc instanceof Error
          ? exc.message : exc}`);
      });
    return () => { alive = false; };
  }, [detailCode, today?.trade_date]);

  const run: AuctionRun | null = today?.run ?? null;
  /**
   * 当前周期在发酵的题材（用户口径 2026-09-22）。
   *
   * 取 `today.run` 而不是单独打接口：它是那轮运行的产物、与阶段同源，
   * 后端只在发酵/加速/分歧/主升阶段才给值。
   */
  const fermenting = run?.fermenting_theme;
  const picked = today?.picked ?? [];
  const overflow = today?.overflow ?? [];
  const nearMiss = today?.near_miss ?? [];
  const vetoed = today?.vetoed ?? [];
  /**
   * ⚠️ 「前置筛选剔除」**不再渲染**（用户口径 2026-09-23）。
   *
   * 那些票在**做任何判断之前**就被门槛拦掉了（不是连板/无涨跌幅限制/ST/上市不足…），
   * 既没有分数、也没有竞价特征。把它们整片摊在落选区，用户看到的是一大堆
   * 与"这次为什么没选它"无关的清单，还会被误读成"打过分但被否决了"。
   *
   * 后端仍然照常返回 `today.prefiltered`（回测与金标准计数要用，见
   * `scripts/backtest_auction.py`），**只是面板不显示** —— 所以这里连变量都不取，
   * 免得又有人顺手把它渲染回来。
   */
  /** 当天是否已经有**成功**记录（调度器 09:25 自动跑过就算）。 */
  const doneToday = run?.status === "done";

  /**
   * 该阶段的 skill 操作指南（`skills/jianmen-shortterm`）。
   *
   * 依赖是**阶段字符串**而不是 `run`：轮询每 30 秒回来一次，若依赖整个
   * `run` 对象就会每次都重打一次这个接口；阶段一天只变一次，没必要。
   *
   * 取不到时静默置 null —— 指南是**附加**信息（skill 是私有资产，
   * 某个 checkout 里可能没有），不该因为它让整个面板报错。
   */
  const marketStage = run?.market_stage ?? "";
  useEffect(() => {
    let alive = true;
    auctionApi.stageGuide(marketStage)
      .then((payload) => { if (alive) setGuide(payload.available ? payload : null); })
      .catch(() => { if (alive) setGuide(null); });
    return () => { alive = false; };
  }, [marketStage]);

  const rootRef = useRef<HTMLDivElement | null>(null);

  /**
   * 展开详情后把它滚进视野。
   *
   * 为什么需要：详情是**就地**插在被点的那张卡下面的（见 `inlineDetail`），
   * 点池子里靠下的票时，它整块长在屏幕之外 —— 用户看到的现象就是
   * "点了股票名/竞价详情，什么都没发生"（实测报障）。
   * 只在卡片顶部已经不可见、或贴在屏幕最底下时才滚，避免每次点击都跳一下。
   */
  useEffect(() => {
    if (!detailCode) return;
    const row = rootRef.current?.querySelector<HTMLElement>(".auction-pick.active");
    if (!row) return;
    const top = row.getBoundingClientRect().top;
    if (top < 0 || top > window.innerHeight - 140) {
      row.scrollIntoView({ block: "start", behavior: "smooth" });
    }
  }, [detailCode]);

  /**
   * 就地展开的详情：插在**被点的那张卡**下面，而不是单独挂一个区块。
   *
   * 原来的写法是把详情渲染成池子后面的独立 `<section>`（顺序是 池 → 够分未入池
   * → 差一点 → 详情）。本轮数据是 6 只入池 + 2 只差一点，详情区块落在点击位置
   * 下方约 1000px 处 —— 点第一张卡时它完全在屏幕外，看着就是"没反应"。
   * 就地展开后，点哪儿长哪儿，不存在这个问题。
   */
  const inlineDetail = (code: string): ReactNode => (
    detailCode === code ? (
      <div className="auction-pick-detail">
        <AuctionDetailView detail={detail} code={code} />
        {/* 卡片头部的按钮会变成「收起」，但详情在下面一屏，回来点它要再滚上去 */}
        <button className="btn-ghost tiny auction-pick-detail-close"
                onClick={() => setDetailCode("")}>收起详情</button>
      </div>
    ) : null
  );

  const triggerRun = useCallback(async () => {
    // 「立即跑一次」的重跑语义（实测踩到的坑）：
    // 调度器每个开市日 09:25 已经自动跑过一次，所以正常交易日点这个按钮时
    // "当天已完成"是**常态**而不是异常。如果还按 force=false 发出去，会被
    // scheduler 的判重挡回来 —— 前端只能显示
    // 「触发失败：RuntimeError: 20260918 已完成过选股（force=True 可强制重跑）」，
    // 按钮看上去永远是坏的。
    // 所以：当天已有成功记录 → 视为**显式重跑**（force=true，会覆盖当日结果），
    // 重跑前先让用户确认；当天还没跑 → 仍走普通调用，保留"非开市日不放行"的守卫。
    if (doneToday && !window.confirm(
      `今天（${today?.trade_date ?? ""}）已经跑过一次（入池 ${run?.picked ?? 0} 只）。\n\n` +
      "点「确定」用当前配置/参数**重跑**，并覆盖今天已落库的结果。")) {
      return;
    }
    setRunning(true);
    try {
      const outcome = await auctionApi.run("", doneToday);
      if (outcome.status === "failed") {
        toast(`触发失败：${outcome.error || "未知"}`);
        return;
      }
      // ⚠️ **不要**再写"约 6 秒"，也不要只补拉一次就完事（用户口径 2026-09-22）。
      //
      // 实测：`/run` 是**后台线程**跑的（避免顶到反代超时），耗时取决于预热：
      //   - 预热缓存命中 → 整轮 2~12 秒（实测 20260917~20260922 是 7.6/2.2/7.2/12.1 秒）；
      //   - 预热缓存**完全冷**（服务刚起、跨日第一次）→ 两三分钟，
      //     其中 `theme_strength_rank` 一项实测就 114 秒。
      // 原来固定 toast「约 6 秒」+ `setTimeout(load, 8000)`：冷预热时 8 秒时服务端
      // 还在跑，拉回来仍是旧结果 → 看着像"点了没反应"，要等下一轮 30 秒轮询。
      //
      // 现在改成**轮询到结果变化为止**（有新 `finished_at` 就停），并给出真实预期。
      toast(outcome.status === "done"
        ? "已完成（服务端同步返回了结果）"
        : "已触发，服务端正在后台跑；结果出来会自动刷新（冷预热可能需要两三分钟）");
      let tries = 0;
      const before = run?.finished_at ?? "";
      const timer = window.setInterval(() => {
        tries += 1;
        void load({ silent: true }).then(() => {
          // 用 `finished_at` 判断"这一轮是不是新的"，而不是"有没有数据"
          if (finishedAtRef.current && finishedAtRef.current !== before) {
            window.clearInterval(timer);
            toast("重跑完成，结果已更新");
          }
        });
        // 上限 40 次 × 15 秒 = 10 分钟，避免服务端卡住时无限轮询
        if (tries >= 40) window.clearInterval(timer);
      }, 15000);
    } catch (exc) {
      toast(`触发失败：${exc instanceof Error ? exc.message : exc}`);
    } finally {
      setRunning(false);
    }
  }, [toast, load, doneToday, run, today?.trade_date]);

  const statusText = useMemo(() => {
    if (!state) return "读取调度状态…";
    if (!state.started) return "调度未启动（服务未开启该模块，或 schedule.enabled=false）";
    if (state.running) return "正在运行…";
    if (!state.last_run_date) return "调度已就绪，等待今日 09:25 触发";
    const at = state.last_run_at ? state.last_run_at.slice(11, 19) : "";
    return `最近一次 ${state.last_run_date} ${at} · ${state.last_status || "—"}`;
  }, [state]);

  return (
    <div className="auction-root" ref={rootRef}>
      {/* ① 调度状态 + 「↻ 刷新」「▶ 立即跑一次」
          用户口径 2026-09-23：这一条**整行**挪到「竞价选股池（n）」标题右侧、同一水平高度
          （原来是页面顶部独立一行，标题左边隔了一大片空白）。
          历史口径：删掉「← 返回量化交易」按钮、「集合竞价选股」标题与那行调度说明
          （09:25 自动跑 / 09:27 出池 / 盘前 09:15 预热）—— 顶部页签已说明这是哪一页。 */}

      {error && <div className="error-box">{error}</div>}

      {state && !state.preheat_ready && (
        <div className="warn-box">
          预热数据尚未就绪（昨日涨停池 / 题材热度 / 连板梯队）。
          这些接口实测要 17~139 秒，所以放在 09:15 预热；点「立即跑一次」会顺带预热。
        </div>
      )}

      {/* 运行摘要 */}
      {run && (
        <div className="auction-meta muted-text">
          <span>交易日 <b>{run.trade_date}</b></span>
          <span>候选 <b>{run.candidates}</b> 只</span>
          <span>打分 <b>{run.scored}</b> 只</span>
          <span>入池 <b>{run.picked}</b> 只</span>
          <span>耗时 <b>{run.seconds?.toFixed?.(1) ?? run.seconds}s</b></span>
          <span className={run.on_time ? "ok-text" : "warn-text"}>
            {run.on_time ? "✓ 按时（≤09:27）" : "⚠ 超时"}
          </span>
          {run.market_stage && (
            <span>
              情绪周期「{run.market_stage}」温度 {run.market_temperature ?? "—"}/100
            </span>
          )}
        </div>
      )}
      {run?.gaps && run.gaps.length > 0 && (
        <details className="auction-gaps">
          <summary>{run.gaps.length} 条运行说明 / 数据缺口</summary>
          <ul>{run.gaps.map((gap, index) => <li key={index}>{gap}</li>)}</ul>
        </details>
      )}

      {/* ③ 情绪周期五线图（用户口径 2026-09-23：要在「情绪周期阶段指南」**上方** ——
          先看走势，再看这个阶段该怎么打；原来是放在指南下面的）。
          口径出自《情绪周期表的用法》：大肉/大面（赚钱与亏钱效应）、连板数、
          小周期（最高板）/大周期（连板压力高度）—— **只统计主板**（用户口径）。
          数据是本地行情仓的日频数据（收盘后落库），所以最后一个点通常是上一交易日。 */}
      <SentimentCycleChart data={cycle} onRefresh={() => void loadCycle()} />

      {/* ④ 情绪周期阶段指南（在五线图下面、选股池上方）。
          阶段判定本来就有，这里把 skill 里写给这个阶段的**仓位管理 / 择股方向 /
          风险**一并摊开（用户口径 2026-09-21）—— 原文出自
          `skills/jianmen-shortterm/references/`，后端原样透传，前端不改写。
          出处只写在这里的注释里：skill 是仓库内部目录，**不往界面上印**
          （用户口径 2026-09-21），所以面板头只有阶段名，没有路径标注。 */}
      {guide && (
        <section className="panel auction-guide">
          <div className="panel-head">
            <h3>
              情绪周期「{guide.stage}」阶段指南
              {guide.phase && guide.phase !== guide.stage
                ? <span className="muted-text">（择时口径「{guide.phase}」）</span>
                : null}
            </h3>
            {/* 右侧：这个周期在发酵的题材（用户口径 2026-09-22）。
                ⚠️ 后端只在发酵/加速/分歧/主升阶段给结论（退潮/冰点不给），
                   所以取不到就整块不渲染 —— 不留空位、也不假装"没有题材在发酵"。
                ⚠️ 必须带家数/最高板这些数字：样本只有几十只涨停股，
                   只给一个题材名会让人以为是"全市场最热概念"（不是）。 */}
            {fermenting?.available && fermenting.candidates.length > 0 && (
              <span className="auction-ferment"
                    title={fermenting.note || "按涨停原因给题材排序"}>
                <span className="muted-text">发酵题材</span>
                {fermenting.candidates.slice(0, 2).map((c, index) => (
                  <span key={c.theme}
                        className={index === 0
                          ? "auction-ferment-tag best" : "auction-ferment-tag"}>
                    {c.theme}
                    <small>{fermentSummary(c)}</small>
                  </span>
                ))}
              </span>
            )}
          </div>

          <div className="auction-guide-grid">
            <div className="auction-guide-card">
              <b>仓位管理</b>
              {guide.action && <p>{guide.action}</p>}
              {guide.position_hint && (
                <p className="muted-text">择时矩阵：{guide.position_hint}</p>
              )}
            </div>

            <div className="auction-guide-card">
              <b>择股方向技巧</b>
              <div className="auction-guide-tags">
                {guide.best_modes.map((mode, index) => (
                  <span key={`best-${index}`} className="auction-guide-tag best">
                    {mode}
                  </span>
                ))}
                {guide.secondary_modes.map((mode, index) => (
                  <span key={`second-${index}`} className="auction-guide-tag">
                    {mode}
                  </span>
                ))}
              </div>
              {(guide.best_modes.length > 0 || guide.secondary_modes.length > 0) && (
                <p className="muted-text">实心=首选 · 空心=次选</p>
              )}
            </div>

            <div className="auction-guide-card">
              <b className="warn-text">风险（该避开的）</b>
              <ul className="auction-guide-risk">
                {guide.avoid.map((item, index) => <li key={index}>{item}</li>)}
              </ul>
            </div>
          </div>

          {(guide.definition || guide.indicators.length > 0) && (
            <details className="auction-guide-more">
              <summary>
                阶段定义与 {guide.indicators.length} 条判据
                {guide.timing_stage ? `（择时矩阵行：${guide.timing_stage}）` : ""}
              </summary>
              {guide.definition && (
                <p className="muted-text auction-guide-def">{guide.definition}</p>
              )}
              <ul className="auction-guide-indicators">
                {guide.indicators.map((item, index) => (
                  <li key={index}>
                    <b>{item.name}</b>
                    <span>{item.rule}</span>
                    {item.source && (
                      <span className="muted-text mono">{item.source}</span>
                    )}
                  </li>
                ))}
              </ul>
            </details>
          )}

          {guide.gap && <div className="muted-text">（{guide.gap}）</div>}
        </section>
      )}

      {/* ② 选股池（标题这一行右侧放调度状态与两个动作按钮，用户口径 2026-09-23） */}
      <section className="panel intraday-panel">
        <div className="panel-head">
          <h3>竞价选股池（{picked.length}）</h3>
          <span className="muted-text">
            范围：昨日涨停 · 非 ST · 流通市值{" "}
            {(info?.universe.min_market_cap ?? 3e9) / 1e8}~
            {(info?.universe.max_market_cap ?? 11e9) / 1e8} 亿（昨收口径）·
            昨收 &lt; {info?.universe.max_prev_close_price ?? 45} 元 ·
            昨收 &gt; {info?.universe.ma_window ?? 20} 日均价；
            门槛 {today?.threshold ?? 51.9} 分，池上限 {info?.score.pool_size ?? 50} 只
          </span>
          <span className="auction-head-actions">
            <span className={state?.running ? "auction-status live" : "auction-status"}>
              {statusText}
            </span>
            {refreshedAt && (
              <span className="muted-text auction-refreshed"
                    title="上一次成功拉到数据的时间；点「刷新」会立刻更新">
                数据于 {refreshedAt.toLocaleTimeString("zh-CN", { hour12: false })} 拉取
              </span>
            )}
            <button className="btn-ghost" disabled={loading}
                    title="立刻重新拉取服务端最新数据（面板本身每 30 秒也会自动拉一次）"
                    onClick={() => void load({ manual: true })}>
              {loading ? "读取中…" : "↻ 刷新"}
            </button>
            <button className="btn-ghost primary" disabled={running}
                    title={doneToday
                      ? "今天已经跑过：用当前配置重跑一次并覆盖当日结果（点之前会先确认）"
                      : "立即跑一次（今天不是开市日会被拦下）"}
                    onClick={() => void triggerRun()}>
              {running ? "触发中…" : doneToday ? "▶ 重跑一次" : "▶ 立即跑一次"}
            </button>
          </span>
        </div>
        {!today || (today.message && picked.length === 0) ? (
          <div className="info-box">
            {today?.message || "还没有选股记录。开市日 09:25 会自动跑，也可点右上「立即跑一次」。"}
          </div>
        ) : picked.length === 0 ? (
          <div className="warn-box">
            今日无票达到 {today?.threshold ?? 50} 分门槛 ——
            出池为空是合法结果（语料：行情差时 2-3 只、极差时为空），不放水凑数。
            {nearMiss.length > 0 && " 下方「差一点」区列出了最接近的票。"}
          </div>
        ) : (
          <ul className="auction-pick-list">
            {picked.map((item) => (
              <AuctionPickCard key={item.code} item={item}
                               active={detailCode === item.code}
                               onOpen={() => setDetailCode(
                                 detailCode === item.code ? "" : item.code)}>
                {inlineDetail(item.code)}
              </AuctionPickCard>
            ))}
          </ul>
        )}
      </section>

      {/* ②b 手动关注列表（用户口径 2026-09-22）—— 紧贴选股池下方。
          用户需求："在竞价选股池下方，要支持用户手动添个股功能，在重跑一次
          触发时，同步给这些股打分和输出操作建议到前端（决策要不要卖，是冲高卖
          还是回踩冲高、还是加仓）。要求文字简洁控制字数在 50 字内）"。
          ⚠️ 建议文字由后端截到 50 字内（`service.advice_tip`），前端不再截
              —— 两处都截会让"到底按谁算"变成查不出来的问题。 */}
      <section className="panel intraday-panel auction-watch">
        <div className="panel-head">
          <h3>我的关注（{watch.length}）</h3>
        </div>
        <form className="auction-watch-add"
              onSubmit={(event) => { event.preventDefault(); void addWatch(); }}>
          <input className="auction-watch-input" value={watchCode}
                 placeholder="输 6 位代码，如 600325"
                 maxLength={6} inputMode="numeric"
                 onChange={(event) => setWatchCode(
                   event.target.value.replace(/\D/g, "").slice(0, 6))} />
          <button className="btn-ghost primary" type="submit"
                  disabled={watchBusy || watchCode.length !== 6}>
            {watchBusy ? "处理中…" : "＋ 加入关注"}
          </button>
          {watch.length > 0 && (
            <span className="muted-text">
              {/* ⚠️ 用字符串拼接而不是直接换行写：JSX 会把源码里的换行折叠成一个空格，
                  中文提示词中间就会多出一个空格（"打分， 手动加的票"）。 */}
              {"加完点上方「▶ 重跑一次」才会给这些票打分，手动加的票不进选股池，"
               + "只在「重跑一次」时一起打分并给操作建议"}
            </span>
          )}
        </form>
        {watch.length > 0 && (
          <ul className="auction-watch-list">
            {watch.map((item) => (
              <li key={item.code} className="auction-watch-item">
                <div className="auction-watch-head">
                  <b>{item.name || item.code}</b>
                  <span className="muted-text mono">{item.code}</span>
                  {item.score ? (
                    <>
                      <span className={`auction-score ${scoreTone(item.score.total_score)}`}>
                        {item.score.total_score.toFixed(1)}
                      </span>
                      <span className="muted-text">
                        {item.score.decision === "buy" ? "建议买"
                          : item.score.decision === "watch" ? "观察" : "不推荐"}
                      </span>
                    </>
                  ) : (
                    // ⚠️ 没分数时**不显示 0 分**：那会被读成"这只票很差"，
                    //    实际是"还没重跑、还没打分"
                    <span className="muted-text">待重跑一次后打分</span>
                  )}
                  <span style={{ flex: 1 }} />
                  <button className="btn-ghost tiny" type="button"
                          disabled={watchBusy}
                          onClick={() => void removeWatch(item.code)}>
                    移除
                  </button>
                </div>
                {item.score && (
                  <p className="auction-watch-advice"
                     title={(item.score.advice_basis || []).join(" · ")}>
                    {item.score.advice}
                  </p>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>

      {/* 差一点（未达门槛但接近） */}
      {/* ③ 达门槛但被池上限截掉 —— 与「差一点」互斥：那个区只收 < 门槛的。
          以前这些票够分、没被否决，却因为排在 pool_size 之外而**一行都不落库**，
          复盘时看不到任何分项明细（20260918 实测有 4 只）。现在按分数降序列出。 */}
      {overflow.length > 0 && (
        <section className="panel intraday-panel">
          <div className="panel-head">
            <h3>够分但未入池（{overflow.length}）</h3>
            <span className="muted-text">
              已过 {today?.threshold ?? 50} 分门槛、也没被否决，只是排在池上限
              （{info?.score.pool_size ?? 50} 只）之外；**不推荐、仅供复盘比对**
            </span>
          </div>
          <ul className="auction-pick-list">
            {overflow.map((item) => (
              <AuctionPickCard key={item.code} item={item} nearMiss
                               label="够分未入池"
                               active={detailCode === item.code}
                               onOpen={() => setDetailCode(
                                 detailCode === item.code ? "" : item.code)}>
                {inlineDetail(item.code)}
              </AuctionPickCard>
            ))}
          </ul>
        </section>
      )}

      {nearMiss.length > 0 && (
        <section className="panel intraday-panel">
          <div className="panel-head">
            <h3>差一点（{nearMiss.length}）</h3>
            <span className="muted-text">
              **未达门槛、不进推荐池**；列出是为了让你判断"是真的没机会"还是"程序没跑"
            </span>
          </div>
          <ul className="auction-pick-list">
            {nearMiss.map((item) => (
              <AuctionPickCard key={item.code} item={item} nearMiss
                               active={detailCode === item.code}
                               onOpen={() => setDetailCode(
                                 detailCode === item.code ? "" : item.code)}>
                {inlineDetail(item.code)}
              </AuctionPickCard>
            ))}
          </ul>
        </section>
      )}

      {/* ④ 单只详情：**已改为就地展开**（见 `inlineDetail`），不再单独挂区块 ——
          原来挂在这里时，详情落在被点卡片下方近 1000px 处，看着像"点了没反应"。 */}

      {/* ③ 落选区：只列**已打分被否决**的票。
          ⚠️ 「前置筛选剔除」不再出现在这里（用户口径 2026-09-23）：那批票在做任何
          判断之前就被拦掉了，没有分数也没有特征，摊在这里只会让人误以为
          "打过分然后被否决"。面板任何位置都不再列这批票（只数与明细都不显示）；
          要核对前置筛选的口径与计数，用回测脚本（`scripts/backtest_auction.py`
          带 `--check-golden`）或直接看接口返回的 `today.prefiltered`。 */}
      {vetoed.length > 0 && (
        <section className="panel intraday-panel">
          <div className="panel-head">
            <h3>落选区（{vetoed.length}）</h3>
            <span className="muted-text">
              已打分被否决 {vetoed.length} 只 —— 每条都标注依据，方便核对是不是"错杀"
            </span>
            <span style={{ flex: 1 }} />
            <button className="btn-ghost" onClick={() => setShowVetoed(!showVetoed)}>
              {showVetoed ? "收起" : "展开"}
            </button>
          </div>
          {showVetoed && (
            <ul className="auction-veto-list">
              {vetoed.map((item) => (
                <li key={item.code}>
                  <span className="mono">{item.code}</span>
                  <span>{item.name || "—"}</span>
                  <span className="muted-text">
                    开盘 {pct(item.features?.open_gap_pct)} ·
                    量比 {num(item.features?.auction_volume_ratio)}
                  </span>
                  <span className="error-text">
                    {item.veto_reasons.join("；")}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      {notice && (
        <div className="notice-toast info" role="status" aria-live="polite">
          <span className="notice-text">{notice}</span>
          <button className="notice-close" onClick={() => setNotice(null)}
                  aria-label="关闭提示">×</button>
        </div>
      )}
    </div>
  );
}

/** 打分卡：总分 + 分项条 + 买入理由（"符合 skill 的特征项"就在理由里）。 */
function AuctionPickCard({ item, active, nearMiss, label, onOpen, children }: {
  item: AuctionPick;
  active: boolean;
  nearMiss?: boolean;
  /** 覆盖默认徽标文案（如「够分未入池」）。 */
  label?: string;
  onOpen: () => void;
  /**
   * 展开时插在卡片**内部底部**的详情。
   *
   * 为什么由父级传进来、而不是卡片自己去取数：卡片只管展示一只票，
   * 数据加载（`auctionApi.detail`）与"当前展开哪一只"是父级的状态，
   * 卡片不该再持有一份。见父级的 `inlineDetail`。
   */
  children?: ReactNode;
}) {
  const feature = item.features || {};
  const tone = scoreTone(item.total_score);
  return (
    <li className={`auction-pick ${active ? "active" : ""}`}>
      <div className="auction-pick-head">
        <span className="auction-rank">#{item.rank || "—"}</span>
        <button className="crowding-alert-name" onClick={onOpen}>
          {item.name || item.code} <span className="mono muted-text">{item.code}</span>
        </button>
        <span className={`auction-score tone-${tone}`}>
          {item.total_score.toFixed(1)}
          <small>/100</small>
        </span>
        {nearMiss && (
          <span className="auction-tag warn">{label || "未达门槛"}</span>
        )}
        {feature.is_one_word_board && <span className="auction-tag">一字板</span>}
        {/* 模式徽标：**互斥分类**的结论（单一模式），不是加权标签。
            六式由择时（情绪周期阶段）裁定 —— 见 skills/jianmen-shortterm/SKILL.md */}
        {feature.mode_name && (
          <span className={feature.mode ? "auction-tag mode" : "auction-tag warn"}
                title={`${feature.mode_branch || ""}｜${feature.mode_reason || ""}`}>
            {feature.mode_name}
            {feature.mode ? ` ${(feature.mode_score ?? 0).toFixed(0)}分` : ""}
          </span>
        )}
        {/* 新龙头 / 有梯队：用户规则2 后半段的两个标签（市场级判定下发到龙头个股） */}
        {feature.is_new_leader && (
          <span className="auction-tag leader" title={feature.leader_detail || ""}>
            新龙头
          </span>
        )}
        {feature.has_tier && (
          <span className="auction-tag tier"
                title={`${feature.leader_detail || ""}\n同题材 ${feature.tier_count ?? "?"} 只`}>
            有梯队
          </span>
        )}
        {feature.is_market_top_board && (
          <span className="auction-tag plain"
                title={`当前市场最高 ${feature.market_max_streak ?? "—"} 板`}>
            最高板
          </span>
        )}
        {feature.is_weak_to_strong && <span className="auction-tag strong">弱转强</span>}
        {feature.pattern && <span className="auction-tag plain">{feature.pattern}</span>}
        {/* 抢筹族 / 抢跑（用户口径 2026-09-18）：抢筹族正向加分，
            抢跑与「急剧下坠型」同级；抢筹族还豁免量比/低开/主板高开三条否决 */}
        {asArray<string>(feature.rush_labels).map((label) => (
          <span key={label}
                className={label === "抢跑" ? "auction-tag weak" : "auction-tag strong"}
                title={`${label}：跳空值 ${feature.jump_gap ?? "—"}、` +
                       `今昨竞比 ${feature.auction_volume_vs_yesterday ?? "—"}、` +
                       `竞价量比 ${feature.auction_volume_ratio ?? "—"}%（今竞价量÷昨全天量）\n` +
                       `${feature.rush_note ?? ""}`}>
            {label === "抢跑" ? "⚠抢跑" : `★${label}`}
          </span>
        ))}
        <span style={{ flex: 1 }} />
        <button className="btn-ghost tiny" onClick={onOpen}>
          {active ? "收起" : "竞价详情"}
        </button>
      </div>

      <div className="auction-pick-facts mono">
        <span>开盘 <b>{pct(feature.open_gap_pct)}</b></span>
        <span>量比 <b>{num(feature.auction_volume_ratio)}</b></span>
        <span>竞价额 <b>{yi(feature.auction_amount)}</b></span>
        <span>承接 <b>{num(feature.takeover_score, 1)}</b></span>
        <span>昨日 <b>{feature.prev_board_text || "—"}</b></span>
        <span>题材 <b>{feature.theme_name || "—"}</b></span>
        <span>流通 <b>{yi(feature.circulating_market_value, 0)}</b></span>
        {feature.theme_reason && (
          <span className="muted-text">涨停原因：{feature.theme_reason}</span>
        )}
      </div>

      <DimBars dimScores={item.dim_scores} />

      <div className="auction-reason">{item.buy_reason}</div>

      {/* 展开的详情就地长在这张卡里面（父级传进来，见 `inlineDetail`） */}
      {active && children}
    </li>
  );
}

/** 分项条（相对该维度满分归一，横条长度即该维度得分率）。 */
function DimBars({ dimScores }: { dimScores: Record<string, number> }) {
  // 满分直接用 `DIM_MAX`（与后端 `auction_select/config.yaml` 的 score.weights
  // 逐项对齐）。**不要在这里另抄一份** —— 原来本地那张表停留在旧的 8 维度版本
  // （22/16/14/14/12/10/8/4），新增的封流比/换手率/题材龙头/竞价情绪全落进
  // `|| 20` 兜底，条长与实际权重不符，等于在说谎。
  const entries = Object.entries(dimScores || {});
  if (entries.length === 0) return null;
  return (
    <div className="auction-dims">
      {entries.map(([key, value]) => {
        const max = DIM_MAX[key] ?? 20;
        const ratio = Math.max(0, Math.min(1, value / max));
        return (
          <div className="auction-dim" key={key}
               title={`${DIM_LABELS[key] || key} ${value.toFixed(1)} / ${max}`}>
            <span className="auction-dim-name">{DIM_LABELS[key] || key}</span>
            <span className="auction-dim-track">
              <span className={`auction-dim-fill tone-${scoreTone(ratio * 100)}`}
                    style={{ width: `${(ratio * 100).toFixed(0)}%` }} />
            </span>
            <span className="auction-dim-value mono">{value.toFixed(1)}</span>
          </div>
        );
      })}
    </div>
  );
}

/** 模式判定面板：命中模式 + 六个模式的逐条判定明细（为什么命中/为什么否决）。 */
function ModePanel({ feature }: { feature: AuctionFeature }) {
  const fits = asArray<ModeFit>(feature.mode_fits);
  if (fits.length === 0) return null;
  return (
    <div className="auction-mode-panel">
      <div className="auction-mode-head">
        <b>模式判定</b>
        <span className="muted-text">
          模式由<b>择时（情绪周期阶段）</b>裁定 —— 六个模式互斥，不做加权求和
        </span>
      </div>
      <div className="auction-mode-verdict">
        {feature.mode ? (
          <>
            <span className="auction-tag mode">{feature.mode_name}</span>
            <span className="muted-text">{feature.mode_branch}</span>
            <span className="mono">机会分 {(feature.mode_score ?? 0).toFixed(0)}/100</span>
          </>
        ) : (
          <span className="warn-text">
            {feature.mode_name || "无匹配模式"} —— {feature.mode_reason}
          </span>
        )}
      </div>
      {feature.mode_context && (
        <div className="auction-mode-context muted-text mono">
          <span>周期 {String(feature.mode_context.raw_stage ?? "—")}</span>
          <span>温度 {String(feature.mode_context.temperature ?? "—")}</span>
          <span>最高板 {String(feature.mode_context.max_streak ?? "—")}</span>
          <span>涨停 {String(feature.mode_context.limit_up_count ?? "—")} 家</span>
        </div>
      )}
      <ul className="auction-mode-list">
        {fits.map((fit) => (
          <li key={fit.mode} className={fit.eligible ? "eligible" : "blocked"}>
            <div className="auction-mode-row">
              <span className={fit.eligible ? "auction-tag mode" : "auction-tag plain"}>
                {fit.eligible ? "✓" : "✗"} {fit.name}
              </span>
              <span className="muted-text">{fit.branch}</span>
              <span className="mono">{fit.score.toFixed(0)}</span>
            </div>
            {fit.blockers.length > 0 ? (
              <ul className="auction-mode-blockers">
                {fit.blockers.map((reason, index) => <li key={index}>{reason}</li>)}
              </ul>
            ) : (
              <div className="auction-mode-hint">
                <span>多方触发：{fit.entry_hint}</span>
                <span>空方触发：{fit.exit_hint}</span>
              </div>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** 单只详情：竞价曲线（上）+ 综合打分与特征项（下，图一式）。 */
function AuctionDetailView({ detail, code }: {
  detail: AuctionDetail | null;
  code: string;
}) {
  if (!detail) return <div className="info-box">正在读取 {code} 的竞价数据…</div>;
  const series = detail.snapshot?.series ?? [];
  const feature = detail.feature ?? detail.pick?.features ?? {};
  const pick = detail.pick;

  return (
    <div className="auction-detail">
      {series.length > 0 ? (
        <AuctionCurve series={series} preClose={detail.snapshot?.pre_close ?? null} />
      ) : (
        <div className="warn-box">
          没有竞价曲线数据（该票未被取到竞价序列，或数据源只有 9:25 快照）。
        </div>
      )}

      {/* K线下方这一块：综合打分 + 特征项（对应需求"图一式"） */}
      <div className="auction-detail-score">
        <div className="auction-detail-total">
          <span className={`auction-score big tone-${scoreTone(pick?.total_score ?? 0)}`}>
            {(pick?.total_score ?? 0).toFixed(1)}
            <small>/100</small>
          </span>
          <div className="auction-detail-total-meta">
            <b>{pick ? (pick.vetoed ? "已否决" : pick.decision) : "未打分"}</b>
            <span className="muted-text">
              总分 {pick?.total_score ?? "—"} · 门槛 {pick ? "见池" : "—"} ·
              {feature.market_stage ? ` 周期「${feature.market_stage}」` : ""}
              {feature.market_temperature != null
                ? ` 温度 ${feature.market_temperature}/100` : ""}
            </span>
          </div>
        </div>

        {pick && <DimBars dimScores={pick.dim_scores} />}

        {/* 模式判定：**独立于综合分** —— 综合分看"这只票质量如何"，
            模式看"此刻该用哪套打法"（由情绪周期择时裁定，六式互斥）。 */}
        <ModePanel feature={feature} />

        <table className="auction-feature-table">
          <tbody>
            <tr>
              <th>开盘涨幅</th><td>{pct(feature.open_gap_pct)}</td>
              <th>竞价量比</th><td>{num(feature.auction_volume_ratio)}</td>
            </tr>
            <tr>
              <th>竞价额</th><td>{yi(feature.auction_amount)}</td>
              <th>竞价额/昨成交</th><td>{pct((feature.auction_amount_ratio ?? 0) * 100)}</td>
            </tr>
            <tr>
              <th>9:20后斜率</th><td>{pct(feature.price_slope)}</td>
              <th>承接分</th><td>{num(feature.takeover_score, 1)}</td>
            </tr>
            <tr>
              <th>未匹配买/卖</th>
              <td>{num(feature.unmatched_buy_hand, 0)} / {num(feature.unmatched_sell_hand, 0)}</td>
              <th>竞价形态</th><td>{feature.pattern || "未识别"}</td>
            </tr>
            <tr>
              <th>昨日连板</th><td>{feature.prev_board_text || "—"}</td>
              <th>题材</th><td>{feature.theme_name || "—"}</td>
            </tr>
            {/* 跳空值 / 今昨竞比 / 抢筹抢跑（用户口径 2026-09-18） */}
            <tr>
              <th>跳空值</th>
              <td title="09:25 正式撮合价 ÷（09:24:40~09:24:55 撮合均价）">
                {num(feature.jump_gap, 4)}
                {feature.jump_gap_points != null && (
                  <span className="muted-text">（{feature.jump_gap_points} 点
                    {feature.jump_fallback ? "·回退" : ""}）</span>
                )}
              </td>
              <th>今昨竞比</th>
              <td title="今日竞价量 ÷ 昨日竞价量">{num(feature.auction_volume_vs_yesterday, 2)}</td>
            </tr>
            <tr>
              <th>抢筹/抢跑</th>
              <td>{asArray<string>(feature.rush_labels).join("、") || "未打标"}</td>
              <th>竞价量比</th>
              <td title="当日 9:25 竞价成交量 ÷ 上个交易日全天总成交量">
                {num(feature.auction_volume_ratio, 2)}%
              </td>
            </tr>
            <tr>
              <th>竞价额/昨成交额</th><td>{num(feature.auction_amount_ratio, 4)}</td>
              <th>今昨竞比</th>
              <td title="今日竞价量 ÷ 昨日竞价量（竞价量能维度读的是这个）">
                {num(feature.auction_volume_vs_yesterday, 2)}
              </td>
            </tr>
            {feature.rush_note && (
              <tr>
                <th>标签依据</th>
                <td colSpan={3}>{feature.rush_note}</td>
              </tr>
            )}
            <tr>
              <th>题材热度</th><td>{num(feature.theme_heat)}</td>
              <th>流通市值</th><td>{yi(feature.circulating_market_value, 0)}</td>
            </tr>
            <tr>
              <th>强弱</th>
              <td>{feature.is_weak_to_strong ? "弱转强" : feature.is_strong_to_weak
                ? "强转弱" : "—"}</td>
              <th>一字板</th><td>{feature.is_one_word_board ? "是" : "否"}</td>
            </tr>
          </tbody>
        </table>

        {/* 各维度"计算依据" —— 需求要的"符合集合竞价买入 skill 的特征项" */}
        {pick && Object.keys(pick.dim_notes || {}).length > 0 && (
          <ul className="auction-dim-notes">
            {Object.entries(pick.dim_notes).map(([key, note]) => (
              <li key={key}>
                <b>{DIM_LABELS[key] || key}</b>
                <span>{note}</span>
              </li>
            ))}
          </ul>
        )}

        {pick?.veto_reasons && pick.veto_reasons.length > 0 && (
          <div className="error-box">否决原因：{pick.veto_reasons.join("；")}</div>
        )}

        {feature.degraded && feature.degraded.length > 0 && (
          <details className="auction-gaps">
            <summary>{feature.degraded.length} 项数据降级说明（口径差异，不是错误）</summary>
            <ul>{feature.degraded.map((item, index) => <li key={index}>{item}</li>)}</ul>
          </details>
        )}

        {feature.theme_reason && (
          <div className="muted-text auction-theme-reason">
            涨停原因：{feature.theme_reason}
            {feature.theme_all && feature.theme_all.length > 0
              && `（所属题材：${feature.theme_all.slice(0, 8).join("、")}）`}
          </div>
        )}
      </div>
    </div>
  );
}

/**
 * 竞价曲线：价格折线 + 9:25 撮合点。
 *
 * 为什么还要画这条线：语料反复强调"9:20 后不可撤单段的**价格轨迹**"
 * 才是有效信息（9:15-9:20 可撤单、可以是假的）。图上把 9:20 画一条竖线分隔，
 * 一眼能看出"这段走势是不是可信的那一段"。
 */
function AuctionCurve({ series, preClose }: {
  series: AuctionSeriesPoint[];
  preClose: number | null;
}) {
  const W = 960;
  const H = 220;
  const PAD = { top: 16, right: 68, bottom: 30, left: 60 };

  const points = series.filter((p) => p.price != null && Number.isFinite(p.price));
  if (points.length < 2) {
    return <div className="info-box">竞价曲线点数不足（{points.length} 点）</div>;
  }
  const prices = points.map((p) => p.price as number);
  const low = Math.min(...prices);
  const high = Math.max(...prices);
  const span = Math.max(1e-6, high - low);
  const pad = span * 0.12;
  const yMin = low - pad;
  const yMax = high + pad;
  const t0 = points[0].time_seconds;
  const t1 = points[points.length - 1].time_seconds;
  const tSpan = Math.max(1, t1 - t0);

  const x = (t: number) => PAD.left + ((t - t0) / tSpan) * (W - PAD.left - PAD.right);
  const y = (price: number) =>
    PAD.top + (1 - (price - yMin) / Math.max(1e-9, yMax - yMin))
    * (H - PAD.top - PAD.bottom);

  const path = points
    .map((p, index) => `${index === 0 ? "M" : "L"}${x(p.time_seconds).toFixed(1)},`
      + `${y(p.price as number).toFixed(1)}`)
    .join(" ");

  // 9:20 分隔线（可靠段起点）
  const k920 = 9 * 3600 + 20 * 60;
  const has920 = t0 <= k920 && k920 <= t1;
  const openPoint = points[points.length - 1];

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="intraday-svg" role="img">
      <text x={PAD.left} y={12} fontSize="10" fill="var(--muted)">
        9:15-9:25 竞价价格轨迹（竖线右侧为 9:20 后不可撤单段，信息才可信）
      </text>
      {[0, 0.5, 1].map((ratio) => {
        const price = yMin + (yMax - yMin) * ratio;
        return (
          <g key={ratio}>
            <line x1={PAD.left} x2={W - PAD.right} y1={y(price)} y2={y(price)}
                  stroke="var(--border)" strokeDasharray="3 4" opacity={0.5} />
            <text x={PAD.left - 6} y={y(price) + 4} fontSize="10"
                  fill="var(--muted)" textAnchor="end">{price.toFixed(2)}</text>
          </g>
        );
      })}
      {preClose != null && preClose >= yMin && preClose <= yMax && (
        <>
          <line x1={PAD.left} x2={W - PAD.right} y1={y(preClose)} y2={y(preClose)}
                stroke="var(--muted)" strokeWidth="1" opacity={0.7} />
          <text x={W - PAD.right + 4} y={y(preClose) + 4} fontSize="10"
                fill="var(--muted)">昨收 {preClose.toFixed(2)}</text>
        </>
      )}
      {has920 && (
        <>
          <line x1={x(k920)} x2={x(k920)} y1={PAD.top} y2={H - PAD.bottom}
                stroke="var(--medium)" strokeDasharray="4 3" strokeWidth="1.2" />
          <text x={x(k920) + 3} y={PAD.top + 10} fontSize="10" fill="var(--medium)">
            9:20
          </text>
        </>
      )}
      <path d={path} fill="none" stroke="var(--accent)" strokeWidth="1.6" />
      <circle cx={x(openPoint.time_seconds)} cy={y(openPoint.price as number)} r="4"
              fill="var(--high)" />
      <text x={x(openPoint.time_seconds) - 6} y={y(openPoint.price as number) - 8}
            fontSize="10" fill="var(--high)" textAnchor="end">
        9:25 撮合 {(openPoint.price as number).toFixed(2)}
      </text>
      <text x={PAD.left} y={H - 8} fontSize="10" fill="var(--muted)">
        {points[0].time} → {openPoint.time} · {points.length} 点
      </text>
    </svg>
  );
}

// ======================================================================
// 情绪周期五线图（用户口径 2026-09-23）
// ======================================================================
//
// 口径出自《情绪周期表的用法》：五条线分别是
//   大肉（赚钱效应：涨停 或 大长腿）、大面（亏钱效应：跌停 或 冲高回落，且近 7 天涨停）、
//   连板数（≥2 板家数）、小周期（当日最高板）、大周期（连板压力高度）。
// 按用户口径**只统计主板**（北证/创业/科创不计入），数据由后端
// `src/auction_select/sentiment_cycle.py` 从本地行情仓日频算出。
//
// ## 两个刻意的画法取舍
//
// 1. **双 Y 轴**：家数类（大肉/大面/连板数，量级 0~100+）与板数类（小周期/大周期，0~10）
//    差一个数量级。文档那张图把它们画在同一根轴上，周期线只能贴着底部 ——
//    这里改成一个左轴（家数）、一个右轴（板数），五条线都看得清。
// 2. **不逐点标数字**（文档图是逐点标的）：30 个交易日 × 5 条线会糊成一片。
//    这里只标**每条线最后一个点**的当前值，明细走悬停 tooltip（含当日最高板龙头）。

const CYCLE_W = 1000;
const CYCLE_H = 230;
/** right 留 46：末尾「当前值」标签（如 61 / 23）要贴在线的右端，右轴刻度再往外让，
 *  两者不能挤在同一栏（2026-09-23 压字检查：标签 x≈958~978，刻度 x≈985~996）。 */
const CYCLE_PAD = { top: 14, right: 46, bottom: 24, left: 40 };

/** 线的样式（大肉=玫瑰红、大面=蓝、连板=绿、大周期=橙、小周期=紫）。
 *
 * `toggle` = 可点击图例隐藏/恢复（用户口径 2026-09-23：大肉/大面/连板数三条家数线要能点选隐藏，
 * 它们的量纲跟大/小周期差一个数量级，混在一张图里容易压住周期线）。
 * 大周期/小周期不带 toggle —— 它们是这张图的主线，且逐点标了数值。
 *
 * `annotate` = **逐点标数值**的纵向偏移（用户口径 2026-09-23：大周期与小周期要标数值，
 * 文档那张图正是逐点标的）。默认大周期标在点**上方**、小周期标在**下方**；
 * 当日小周期高于大周期（金叉态）时两者互换，否则两条标签会挤在中间互相压字。 */
const CYCLE_LINES: Array<{
  key: keyof AuctionSentimentCycle["series"];
  label: string; color: string; axis: "count" | "level";
  width: number; dash?: string; annotate?: boolean; toggle?: boolean;
}> = [
  { key: "win", label: "大肉", color: "#ff5f7e", axis: "count", width: 1, toggle: true },
  { key: "loss", label: "大面", color: "#4a9eff", axis: "count", width: 1.8, toggle: true },
  { key: "boards", label: "连板数", color: "#4fbf7f", axis: "count", width: 1.4, toggle: true },
  { key: "pressure", label: "大周期", color: "#e8a13c", axis: "level", width: 1.6,
    annotate: true },
  { key: "height", label: "小周期", color: "#a06ff0", axis: "level", width: 1.4,
    dash: "5 4", annotate: true },
];

/** 逐点数值标签相对数据点的纵向偏移（文本基线位置）。 */
const CYCLE_ANNOTATE_ABOVE = -9;
const CYCLE_ANNOTATE_BELOW = 14;

/** 左轴（家数）下限：固定画到 200（用户口径 2026-09-23），超过时按 50 的整数倍上扩。 */
const CYCLE_COUNT_FLOOR = 200;

function niceMax(value: number): number {
  if (!Number.isFinite(value) || value <= 0) return 10;
  const step = value > 80 ? 20 : value > 40 ? 10 : value > 12 ? 5 : 2;
  return Math.ceil(value / step) * step;
}

function SentimentCycleChart({ data, onRefresh }: {
  data: AuctionSentimentCycle | null;
  onRefresh: () => void;
}) {
  const [hover, setHover] = useState<{ index: number; x: number } | null>(null);
  /** 点图例隐藏的线（只有三条家数线可点，见 CYCLE_LINES.toggle）。 */
  const [hidden, setHidden] = useState<string[]>([]);
  const series = data?.series;
  const days = data?.days ?? [];
  const total = days.length;

  const visible = useMemo(
    () => CYCLE_LINES.filter((line) => !hidden.includes(line.key)),
    [hidden]);
  const toggleLine = useCallback((key: string) => {
    setHidden((prev) => (prev.includes(key)
      ? prev.filter((item) => item !== key)
      : [...prev, key]));
  }, []);

  const scales = useMemo(() => {
    if (!series || !total) return null;
    // 左轴（家数）：**下限固定 200**（用户口径 2026-09-23：原来随数据缩放到 0~120，
    // 同一个"大肉 50"在两张不同日期的图上高度不一样，看不出强弱变化）。
    // 只按**可见**的家数线取峰值 —— 数据真超过 200 时按 50 的整数倍上扩，
    // 否则大肉那种能到 487 的日子会被腰斩在框外（实测近 60 交易日峰值 487）。
    const counts = visible.filter((line) => line.axis === "count")
      .flatMap((line) => series[line.key]);
    const peak = counts.length ? Math.max(...counts) : 0;
    // 上扩按 200 的整数倍：5 条网格线的刻度才会都是 50 的整数倍
    // （200 → 0/50/100/150/200；400 → 0/100/200/300/400；600 → 0/150/300/450/600）
    const countMax = Math.max(CYCLE_COUNT_FLOOR, Math.ceil(peak / 200) * 200);
    const levelMax = niceMax(Math.max(...series.height, ...series.pressure, 2));
    return { countMax, levelMax };
  }, [series, total, visible]);
  /** 有序列又有标尺才画图（下面的三元里用这些常量，TS 才能收窄类型）。 */
  const chart = scales && series ? series : null;
  const countMax = scales?.countMax ?? 1;
  const levelMax = scales?.levelMax ?? 1;
  /** 三条家数线是否还有可见的（全隐藏 → 左轴刻度一起收起来）。 */
  const countVisible = visible.some((line) => line.axis === "count");

  const plotW = CYCLE_W - CYCLE_PAD.left - CYCLE_PAD.right;
  const plotH = CYCLE_H - CYCLE_PAD.top - CYCLE_PAD.bottom;
  const x = useCallback((index: number) =>
    CYCLE_PAD.left + (total <= 1 ? plotW / 2 : (index / (total - 1)) * plotW),
    [total, plotW]);
  const yCount = useCallback((value: number) =>
    CYCLE_PAD.top + plotH * (1 - (scales ? value / scales.countMax : 0)),
    [plotH, scales]);
  const yLevel = useCallback((value: number) =>
    CYCLE_PAD.top + plotH * (1 - (scales ? value / scales.levelMax : 0)),
    [plotH, scales]);

  const stage = data?.stage;

  return (
    <section className="panel intraday-panel auction-cycle">
      <div className="panel-head">
        <h3>情绪周期（主板）</h3>
        {stage?.label && (
          <span className={stage.breakout ? "ok-text" : "warn-text"}
                title={"小周期 = 当日最高板身位；大周期 = 连板压力高度。"
                       + "小周期突破大周期且赚钱效应同步上升 = 新周期启动（文档小结口径）"}>
            {stage.label}
          </span>
        )}
        <span className="muted-text">
          近 {total} 个交易日 · 口径：主板（10CM）· 数据截至{" "}
          {formatCycleDate(days[total - 1], true) ?? "—"}
        </span>
        <span style={{ flex: 1 }} />
        <button className="btn-ghost" onClick={onRefresh}
                title="从本地行情仓重取（日频、收盘后落库；盘中不会变）">
          ↻ 刷新
        </button>
      </div>

      {!data ? (
        <div className="muted-text">情绪周期数据读取中…</div>
      ) : !data.available || !chart ? (
        <div className="warn-box">
          情绪周期不可用：{(data.gaps || []).join("；") || "本地行情仓没有数据"}
        </div>
      ) : (
        <>
          <div className="auction-cycle-legend">
            {CYCLE_LINES.map((line) => {
              const off = hidden.includes(line.key);
              const body = (
                <>
                  <i style={off
                    ? { background: "transparent", boxShadow: `inset 0 0 0 1px ${line.color}` }
                    : { background: line.color }} />
                  <span className="auction-cycle-name">{line.label}</span>
                  <em className="muted-text">
                    {line.axis === "count" ? "左轴·家数" : "右轴·板数"}
                  </em>
                </>
              );
              return line.toggle ? (
                <button key={line.key} type="button"
                        className={`auction-cycle-key is-btn${off ? " is-off" : ""}`}
                        onClick={() => toggleLine(line.key)}
                        title={`点击${off ? "显示" : "隐藏"}「${line.label}」（只影响本图显示，不改口径）`}>
                  {body}
                </button>
              ) : (
                <span key={line.key} className="auction-cycle-key">{body}</span>
              );
            })}
            <span className="muted-text">
              大肉 = 涨停 或 大长腿≥10% · 大面 = 跌停 或 冲高回落≥10%（且近 7 天涨停）·
              连板数 = ≥2 板家数（剔新股/ST）{hidden.length > 0 && " · 已隐藏的线可再点图例恢复"}
            </span>
          </div>

          <svg viewBox={`0 0 ${CYCLE_W} ${CYCLE_H}`} className="intraday-svg"
               onMouseLeave={() => setHover(null)}
               onMouseMove={(event) => {
                 const rect = event.currentTarget.getBoundingClientRect();
                 const ratio = (event.clientX - rect.left) / rect.width * CYCLE_W;
                 const index = Math.max(0, Math.min(total - 1, Math.round(
                   ((ratio - CYCLE_PAD.left) / plotW) * (total - 1))));
                 setHover({ index, x: x(index) });
               }}>
            {/* 网格 + 左右轴刻度 */}
            {[0, 0.25, 0.5, 0.75, 1].map((tick) => {
              const y = CYCLE_PAD.top + plotH * (1 - tick);
              return (
                <g key={tick}>
                  <line x1={CYCLE_PAD.left} x2={CYCLE_W - CYCLE_PAD.right}
                        y1={y} y2={y} stroke="var(--border)" strokeDasharray="3 4"
                        opacity={0.6} />
                  {/* 左轴（家数）：三条家数线全藏了就一起收起来，免得留一根没意义的刻度 */}
                  {countVisible && (
                    <text x={CYCLE_PAD.left - 6} y={y + 3.5} fontSize={9}
                          fill="var(--muted)" textAnchor="end">
                      {Math.round(countMax * tick)}
                    </text>
                  )}
                  {/* 右轴（板数）刻度贴最右：左边那栏留给线的末尾当前值标签 */}
                  <text x={CYCLE_W - 4} y={y + 3.5} fontSize={9}
                        fill="var(--muted)" textAnchor="end">
                    {Math.round(levelMax * tick)}
                  </text>
                </g>
              );
            })}
            {/* X 轴日期：只标首、中、尾，避免糊成一片 */}
            {[0, Math.floor((total - 1) / 2), total - 1].map((index) => (
              <text key={index} x={x(index)} y={CYCLE_H - 8} fontSize={9}
                    fill="var(--muted)" textAnchor="middle">
                {formatCycleDate(days[index])}
              </text>
            ))}

            {/* 五条线（点图例隐藏的家数线不画） */}
            {visible.map((line) => {
              const values = (chart ?? series)![line.key];
              const y = line.axis === "count" ? yCount : yLevel;
              const path = values.map((value, index) =>
                `${index ? "L" : "M"}${x(index).toFixed(1)},${y(value).toFixed(1)}`)
                .join(" ");
              const last = values[values.length - 1];
              return (
                <g key={line.key}>
                  <path d={path} fill="none" stroke={line.color}
                        strokeWidth={line.width}
                        strokeDasharray={line.dash} />
                  {line.annotate === undefined ? (
                    /* 家数三条线：只标最后一个点的当前值（逐点标会糊） */
                    <text x={x(total - 1) + 4} y={y(last) + 3.5} fontSize={9}
                          fill={line.color}>
                      {last}
                    </text>
                  ) : (
                    /* 大周期/小周期：**逐点标数值**（用户口径 2026-09-23） */
                    values.map((value, index) => {
                      const small = series!.height[index];
                      const big = series!.pressure[index];
                      /* 小周期更高（金叉态）时大周期标签改标下方、小周期改标上方 */
                      const flip = small > big;
                      const above = line.key === "pressure" ? !flip : flip;
                      return (
                        <text key={index} x={x(index)}
                              y={y(value) + (above ? CYCLE_ANNOTATE_ABOVE : CYCLE_ANNOTATE_BELOW)}
                              fontSize={8.5} fill={line.color} textAnchor="middle">
                          {value}
                        </text>
                      );
                    })
                  )}
                </g>
              );
            })}

            {/* 悬停：竖直引导线 + 当日各值（数值在下面的浮层里给） */}
            {hover && (
              <line x1={hover.x} x2={hover.x} y1={CYCLE_PAD.top}
                    y2={CYCLE_PAD.top + plotH} stroke="var(--muted)"
                    strokeDasharray="3 3" opacity={0.7} />
            )}
          </svg>

          {hover && (
            <div className="auction-cycle-hover mono">
              <b>{formatCycleDate(days[hover.index])}</b>
              {visible.map((line) => (
                <span key={line.key}>
                  <i style={{ background: line.color }} />
                  {line.label} {(chart ?? series)![line.key][hover.index]}
                </span>
              ))}
              {hidden.length > 0 && (
                <span className="muted-text">
                  已隐藏：{CYCLE_LINES.filter((line) => hidden.includes(line.key))
                    .map((line) => line.label).join("/")}
                </span>
              )}
              <span className="muted-text">
                涨停 {chart.limit_up[hover.index]} · 跌停 {chart.limit_down[hover.index]}
              </span>
              {(data.leaders?.[days[hover.index]] || []).length > 0 && (
                <span className="muted-text">
                  最高板：{(data.leaders[days[hover.index]] || []).join(" / ")}
                </span>
              )}
            </div>
          )}

          {(data.gaps || []).length > 0 && (
            <div className="muted-text auction-cycle-gaps">
              ⚠ {data.gaps.join("；")}
            </div>
          )}
          {data.disclaimer && (
            <div className="muted-text auction-cycle-gaps">{data.disclaimer}</div>
          )}
        </>
      )}
    </section>
  );
}

/** `20260922` → `09-22`（图上位置紧，年份省掉）；`full=true` 时给 `2026-09-22`。 */
function formatCycleDate(raw: string | undefined, full = false): string {
  if (!raw || raw.length !== 8) return raw ?? "—";
  return full ? `${raw.slice(0, 4)}-${raw.slice(4, 6)}-${raw.slice(6, 8)}`
    : `${raw.slice(4, 6)}-${raw.slice(6, 8)}`;
}
