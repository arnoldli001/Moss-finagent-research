/**
 * 情报中心（舆情情报页签的主面板）。
 *
 * ## 两个子页签，为什么这样分
 *
 * | 子页签 | 回答的问题 | 数据来源 |
 * |---|---|---|
 * | 热点&研报小作文 | "最近有什么公开信息、**哪些票和事在被讨论**" | `GET /intel/feed` |
 * | 投资日历 | "**接下来**会发生什么" | `GET /intel/calendar` |
 *
 * 用户口径（2026-09-25）："金融数据源讲究时效性，讲究预期差，
 * 未来的预期事件才更重要。" —— 所以「投资日历」是与情报流**平级**的
 * 子页签，而不是塞在某个角落的小组件。它是这一页里唯一向前看的部分。
 *
 * ⚠️ 原来的第三个子页签「舆情热度监控」**已删除**。用户口径：
 *
 *   "舆情热度监控的内容呢？…第二张图 被提及的标的都是6位编码，需要映射成
 *    中文股票名。原文倾向分布也没具体事件，只有数字，这没有意义，删除吧。
 *    需求是具体的内容，而不是统计数量。情报中心这页做的毫无使用价值。"
 *   "当前舆情热度监控没做好，不如直接融入到情报流里。"
 *
 * 它原本是三个统计块（来源类型结构 / 按日分布 / 纯数字倾向分布），
 * 共同问题是**只回答"有多少"，不回答"发生了什么"**。现在换成
 * `IntelHeatBlock`（热议个股 + 热议事件 + 平台人气榜，都带**可核对的
 * 原文标题**），挂在情报流顶部 —— 一个入口，且内容是具体的。
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  CalendarResult, IntelFeed,
  fetchIntelBootstrap, fetchIntelFeed, fetchIntelFeedStatus, formatTime,
} from "../../intelApi";
import IntelCalendarTab from "./IntelCalendarTab";
import IntelFeedTab from "./IntelFeedTab";
import {
  feedCacheKeyOf, readIntelFeed, writeIntelFeed,
} from "../../intelCache";

/**
 * 首屏取数的**超时兜底**（毫秒）。
 *
 * ## 为什么必须有它（2026-09-26 实测）
 *
 * 隧道抖动时单次请求可能挂很久：同一时段实测有一次 **TLS 握手 180 秒超时**，
 * 另一个 834 KB 的静态包三次分别 1.71 s / **35.8 s** / 1.67 s。
 * 没有超时的话，用户就要对着转圈等这么久 —— 而这期间他**手里本来就有
 * 上一次的数据**（localStorage 缓存）。
 *
 * 所以：超时后立刻解除忙碌态，让他先看手里那份；请求被 abort，
 * 下次切页签/刷新重新取。**有缓存时不报错**（用户看不到失败，只是数据略旧），
 * 没缓存时才如实说"请求超时"。
 *
 * ## 为什么是 6 秒
 *
 * 正常情况（本机 30 ms、公网热 1~3 秒）远到不了它；而抖动时的"谷底"是
 * 几十秒 —— 6 秒足够区分"慢但会回来"与"卡住了"。取太小会把正常的
 * 慢响应误判成超时，取太大就失去了兜底的意义。
 */
const LOAD_TIMEOUT_MS = 6000;

/**
 * 等"后台采集完成"的轮询间隔（毫秒）。
 *
 * ## 为什么是 2000，以及为什么只在 `refreshing` 期间轮询
 *
 * 这条轮询是 WebSocket 通知的**兜底**（隧道抖动 / WS 断开 / 浏览器把页面
 * 挂起时通知收不到）。两条理由把间隔定在 2 秒：
 *
 *   · 服务端一次采集实测 1~3 秒，2 秒的采样间隔意味着用户最多多等 2 秒
 *     —— 而**不轮询**的代价是"永远停在旧数据上，要手动点刷新"；
 *   · 它只在 `refreshing === true` 期间存在（通常 1~2 个 tick），
 *     一旦拿到数据、或超过 `READY_POLL_MAX_TICKS` 就**彻底停掉** ——
 *     绝不能做成常驻快轮询（那会把一条零成本的接口变成持续负载，
 *     而"面板开着就一直在打接口"这件事在压测里也看不出来）。
 *
 * 间隔与轮询上限都是**刻意重复**的数字（服务端并不知道前端怎么等），
 * 但两边都不用"猜"：服务端给了 `building` / `seq`，前端只是采样它。
 */
const READY_POLL_MS = 2000;
/** 兜底轮询的 tick 上限（2s × 15 = 30 秒）。超了就如实停下并提示。 */
const READY_POLL_MAX_TICKS = 15;

export default function IntelPanel({ isAdmin = false, intelSeq = 0,
  section = "hot" }: {
  isAdmin?: boolean;
  /**
   * 告警 WebSocket 推来的情报流序号（见 `useAlertsWs`）。
   *
   * `0` = 还没有收到过任何通知。收到更大的值 ⇒ 服务端建好了一份新的，
   * 面板立刻自己去拉（**用户不必点刷新**）。
   */
  intelSeq?: number;
  /**
   * 渲染哪一段。**由顶级页签决定，面板内不再有子页签。**
   *
   * 用户口径（2026-09-25）："删除当前的一级目录里的'情报中心'，
   * 并将二级目录页签'热点&研报小作文'、'投资日历'改为一级目录"。
   *
   * 所以原来那排 `.intel-subtabs` 已删除：`hot` / `calendar` 各自是
   * 一级入口（`App.tsx` 的两个 `<TL>`），面板只负责渲染被选中的那一段。
   * 两者仍共用一个面板，因为**加载逻辑是共用的**（都在挂载时并发拉
   * feed + calendar + health，切页签时各自独立 settle）——
   * 拆成两个组件会把这段并发与容错逻辑复制两份。
   */
  section?: "hot" | "calendar";
}) {
  const [feed, setFeed] = useState<IntelFeed | null>(null);
  const [cal, setCal] = useState<CalendarResult | null>(null);
  const [errFeed, setErrFeed] = useState("");
  const [errCal, setErrCal] = useState("");
  const [loading, setLoading] = useState(false);
  /** 日历**单独**的忙碌态。为什么必须与 `loading` 分开见 `loadAll` 的注释：
   * 日历比情报流慢三个数量级，共用一个标志会让整页被它绑住。 */
  const [loadingCal, setLoadingCal] = useState(false);
  /**
   * 服务端**正在后台采集**（`feed.refreshing`）。
   *
   * 单独一个 state 而不是每次读 `feed.refreshing`：就绪监听要在**拉数之外**
   * 决定"要不要继续等"，而 `feed` 在自动补拉期间会被替换成新对象；
   * 把等待状态挂在被替换的对象上，很容易出现"组件重渲染后才开始等"的抖动。
   */
  const [refreshing, setRefreshing] = useState(false);
  /** 兜底轮询超过了上限（30 秒仍没等到）—— 界面要**如实**说，不许静默转圈。 */
  const [readyTimeout, setReadyTimeout] = useState(false);
  /**
   * 筛选档与取样顺序**由面板持有**，因为它们会触发**服务端重新取样**。
   *
   * 为什么不让子组件自己过滤：前端过滤只能过滤"已取回的这一页"
   * （默认 60 条）。于是"高可信 ≥80"可能只显示 6 条，而全量里其实有
   * 30 条 —— 用户看到的是**取样的结果**，不是**数据的真相**。
   * 所以切档位要重新请求。
   */
  const [filter, setFilter] = useState("all");
  const [sort, setSort] = useState<"credibility" | "time">("credibility");
  /**
   * 原文倾向（多/空）档位。用户口径（2026-09-26）：
   *
   * > "intel-controls 表头要加一个 多/空 的过滤选择器（多还是空 是模型
   * >   分析出来的 每条信息第一个字【空】【多】）"
   *
   * 与 `filter`（可信度）是**两个独立的轴**，所以是两个 state、两个参数：
   * 合并成一个的话，"高可信 + 偏多"就表达不出来了。
   */
  const [direction, setDirection] = useState("all");
  // 卸载后不再 setState（切页签时请求可能还在飞）
  const alive = useRef(true);
  /** 手里这一份的序号。**只有它比服务端的序号小**才需要再拉一次。 */
  const haveSeq = useRef(0);

  /**
   * 统一的取数出口。**所有**取数都走这里（不新增第二条数据通路）。
   *
   * `refresh` 只由"用户点了刷新"传 —— 自动补拉命中 60 秒缓存正是我们要的
   * （见 `fetchIntelFeed` 的说明：手动刷新不带 `refresh=true` 就是**假刷新**）。
   */
  const loadFeed = useCallback(async (
    f: string, s: string, d: string = "all", opts: { refresh?: boolean } = {},
  ) => {
    const ck = feedCacheKeyOf(f, s, d);
    // ★ **先画缓存、再后台核对**（stale-while-revalidate）。
    //
    // 用户报障（2026-09-26）："热点&研报小作文、事件告警，这两个页面来回切，
    // 都会出现刚加载的信息界面，还需要等 2-3 秒才展示数据。"
    //
    // 根因：`App.tsx` 用三元链渲染视图，切走时 `IntelPanel` **整个卸载**，
    // 切回来是全新挂载 —— 组件内 `feed` state 归零，于是必发一次
    // `/intel/feed?limit=60`（实测 raw 62.7 KB / gzip 17.0 KB；
    // 在 ≈51 KB/s 的公网隧道上明文要 **1.2 秒**）。
    //
    // 所以：缓存里那一份**立刻画出来**，同时发请求核对，拿到新的再覆盖。
    // 已经画了旧数据时**不显示"加载中"** —— 那会让"立刻可用"又变回
    // "看起来在等"，等于白做。与 `alertsCache` 是同一套语义。
    if (!opts.refresh) {
      const cached = readIntelFeed(ck);
      if (cached) {
        if (!alive.current) return;
        setFeed(cached.feed);
        setRefreshing(false);      // 缓存里那份不触发"等 WS 补拉"轮询
        setErrFeed("");
      }
    }
    try {
      const data = await fetchIntelFeed(
        { limit: 60, filter: f, sort: s, direction: d, refresh: opts.refresh });
      if (!alive.current) return;
      setFeed(data);
      setErrFeed("");
      setRefreshing(!!data.refreshing);
      setReadyTimeout(false);
      // 缓存这一档的结果（键含 filter/sort/direction，否则切档会串）
      writeIntelFeed(ck, data);
      // 序号只往大取：服务端缓存是**多个键**（filter/sort 各一份），
      // 切档位可能拿到一份 seq 较小的 —— 那不代表"服务端退回去了"。
      if (typeof data.seq === "number" && data.seq > haveSeq.current) {
        haveSeq.current = data.seq;
      }
    } catch (e) {
      if (alive.current) setErrFeed(errText(e));
    }
  }, []);

  /**
   * 全量重载（首次挂载 / 手动刷新）：情报流 + 日历**各走各的**。
   *
   * ## ★ 为什么两路必须解耦（2026-09-26 用户报障"打开要等 5~10 秒"）
   *
   * 实测（真实会话，本机 8110）：
   *
   *     /intel/feed?limit=60        36 ms   ← 内容其实早就到了
   *     /intel/calendar?horizon_days=45   **11,133 ms**
   *
   * 原写法是 `await Promise.allSettled([feed, calendar])` 之后才
   * `setLoading(false)` —— 于是**界面的忙碌态被最慢的那一路绑住**：
   * 内容 36 毫秒就到了，用户却要盯着"正在读取日程…"看十几秒。
   * 而且 `section` 变化会重挂本面板，切到「投资日历」就等于重付这 11 秒。
   *
   * 现在：
   *   · feed 一到就解除 loading（内容立刻可读、翻筛选档不再被卡）；
   *   · 日历在后台自己加载，只在**日历那一栏**显示"正在读取日程…"；
   *   · 服务端同时给日历/热度加了小时级缓存 + 启动预热（见
   *     `domain/intel/prewarm.py` 的"慢聚合落盘缓存"），所以这一路
   *     命中缓存时是**毫秒级**、零上游 —— 前端解耦是"就算现拉也不卡人"，
   *     服务端缓存是"根本不用现拉"，两层都要。
   */
  const loadAll = useCallback(async (opts: { refresh?: boolean } = {}) => {
    // ★ 有本地缓存时**不要**进忙碌态：那会让"切回来立刻有内容"又退回
    //   "看起来在等"。与 `loadFeed` 的 stale-while-revalidate 配套 ——
    //   缓存那份由下面立刻画出来，这里只负责别盖住它。
    const ck = feedCacheKeyOf(filter, sort, direction);
    const cached = opts?.refresh ? null : readIntelFeed(ck);
    if (cached && alive.current) {
      // 先把缓存那份画出来（切页签/二次访问就是"立刻有内容"）
      setFeed(cached.feed);
      setRefreshing(false);
      setErrFeed("");
    }
    setLoading(!cached);
    setLoadingCal(true);
    setErrFeed("");
    setErrCal("");

    // ★ **一次往返**取齐情报流 + 日历（`/intel/bootstrap`）。
    //
    // 原来是两条（`/feed` + `/calendar`）。公网实测：情报流 53.5 KB 要 4.93 s、
    // 热度 **1.4 KB 也要 2.52 s** —— 说明瓶颈是**往返本身**（隧道 RTT +
    // 队头等待），不是字节数。既然每次往返付一笔固定开销，2 次并 1 次就省一笔。
    //
    // ⚠️ 热度不在这里另外取：`feed.heat` 本来就带（服务端附上的）。
    //
    // ⚠️ **超时兜底**：链路抖动时单次请求可能挂几十秒（实测有一次 TLS 握手
    //    180 秒超时）。没有缓存的用户只能等；**有缓存的用户不该等** ——
    //    超时后立刻解除忙碌态，让他先看手里那份，后台请求继续跑。
    const ac = new AbortController();
    const timer = window.setTimeout(() => ac.abort(), LOAD_TIMEOUT_MS);
    try {
      const r = await fetchIntelBootstrap({
        limit: 60, filter, sort, direction, horizonDays: 45,
      }, ac.signal);
      if (!alive.current) return;
      setFeed(r.feed);
      setErrFeed("");
      setRefreshing(!!r.feed.refreshing);
      setReadyTimeout(false);
      writeIntelFeed(ck, r.feed);
      if (typeof r.feed.seq === "number" && r.feed.seq > haveSeq.current) {
        haveSeq.current = r.feed.seq;
      }
      if (r.calendar) setCal(r.calendar);
    } catch (e) {
      if (!alive.current) return;
      // `AbortError` = 我们自己的超时。有缓存时**不报错**（用户看不到失败，
      // 只是数据略旧），没缓存时才如实说。
      const aborted = e instanceof DOMException && e.name === "AbortError";
      if (!aborted) {
        setErrFeed(errText(e));
        setErrCal(errText(e));
      } else if (!cached) {
        setErrFeed("请求超时（链路较慢）。可稍后点刷新重试。");
      }
    } finally {
      window.clearTimeout(timer);
      if (alive.current) {
        setLoading(false);
        setLoadingCal(false);
      }
    }
  }, [filter, sort, direction]);

  useEffect(() => {
    alive.current = true;
    void loadAll();
    return () => { alive.current = false; };
  }, [loadAll]);

  /**
   * ★ **就绪监听**：服务端后台采集完成 → 自动补拉一次（用户不用点刷新）。
   *
   * 用户的诉求原文是"可以做监听回调，有信息了再触发自动刷新前端"。
   * 两条路按优先级：
   *
   *   ① 主路 —— 告警 WebSocket 的 `intel_feed` 通知（`intelSeq` 变大）。
   *      这是**既有**的常驻连接（`useAlertsWs`，挂在 App 层），复用它等于
   *      白拿重连、心跳与登录鉴权，不用维护第二套通道。
   *   ② 兜底 —— WS 不可用（隧道抖动、页面被挂起）时，短轮询极廉价的
   *      `/feed/status`（只读进程内计数器）：序号比手里的大就拉数据。
   *      拿不到、或超过 `READY_POLL_MAX_TICKS` 就**停**，并如实提示。
   *
   * ⚠️ 触发条件是 `refreshing === true`。补拉回来的那份 `refreshing=false`
   * （缓存已经在 60 秒内），于是本 effect 立刻收敛 —— 不会形成"拉一次、
   * 又发现要等、再拉一次"的紧循环。
   */
  useEffect(() => {
    if (!refreshing) return;
    let stopped = false;
    let ticks = 0;
    let busy = false;      // 上一 tick 的请求还没回来就别再发（防堆叠）

    const pull = async () => {
      // ① 通知已经到了（WS 推的序号更大）→ 直接拉，不必再问 status
      if (intelSeq > haveSeq.current) {
        haveSeq.current = intelSeq;
        await loadFeed(filter, sort, direction);
        return;
      }
      // ② 兜底：问一次极廉价的状态
      try {
        const st = await fetchIntelFeedStatus();
        if (stopped) return;
        if (st.seq > haveSeq.current) {
          haveSeq.current = st.seq;
          await loadFeed(filter, sort, direction);
          return;
        }
        if (!st.building) {
          // 后台没在跑、序号也没前进 ⇒ 这一次重建失败了（或没排上）。
          // 不能永远转圈：如实停下并让界面说明情况。
          setRefreshing(false);
          setReadyTimeout(true);
        }
      } catch { /* 状态接口失败：下一个 tick 再试，不打扰用户 */ }
    };

    const id = window.setInterval(() => {
      if (busy || ticks >= READY_POLL_MAX_TICKS) {
        if (ticks >= READY_POLL_MAX_TICKS) {
          window.clearInterval(id);
          setRefreshing(false);
          setReadyTimeout(true);
        }
        return;
      }
      ticks += 1;
      busy = true;
      void pull().finally(() => { busy = false; });
    }, READY_POLL_MS);
    // 立刻先试一次：通知可能在 effect 挂上之前就到了
    void pull();

    return () => {
      stopped = true;
      window.clearInterval(id);
    };
  }, [refreshing, intelSeq, filter, sort, direction, loadFeed]);

  /**
   * 切筛选/排序/方向：**只重拉情报流**，不动日历。
   *
   * 为什么不像其它页签那样整块刷新：日历跟筛选无关，重拉会让"点一下 tab
   * 整页闪一下"，而且日历那一路要读好几个源。
   *
   * ⚠️ 切档位**不传** `refresh=true`：服务端按 `filter|sort|direction`
   * 分别缓存，切回上一个档位直接命中，这是缓存最主要的价值。
   */
  const onFilter = useCallback((f: string) => {
    setFilter(f);
    void loadFeed(f, sort, direction);
  }, [sort, direction, loadFeed]);
  const onSort = useCallback((s: "credibility" | "time") => {
    setSort(s);
    void loadFeed(filter, s, direction);
  }, [filter, direction, loadFeed]);
  const onDirection = useCallback((d: string) => {
    setDirection(d);
    void loadFeed(filter, sort, d);
  }, [filter, sort, loadFeed]);

  // 切档位时的忙碌态：不复用 `loading`（那是整页首次加载）
  const feedBusy = loading;

  /**
   * 首屏骨架的判据：**还没有任何一份数据**，或**正在采集且手里一条都没有**。
   *
   * 为什么不是 `!feed`：服务端冷启动返回的是一个**形状正确的空壳**
   * （`items: []` + `refreshing: true`）。直接渲染 `IntelFeedTab` 会让用户
   * 先看到一屏"没有内容"，一两秒后才突然冒出来 —— 比空屏更像故障。
   */
  const showSkeleton = !feed
    || (!!feed.refreshing && (feed.items?.length ?? 0) === 0);

  return (
    <div className="intel-root">

      {/* ── 合规横幅：固定展示，不可关闭 ──
          为什么放最上面而不是页脚：这条决定了整页内容该怎么读 ——
          用户要先知道"这里没有买卖建议"，才不会把统计量当结论。 */}
      <div className="intel-compliance">
        <b>本平台只做三件事：聚合公开信息 · 输出统计量 · 公开计算公式。</b>
        不含证券投资咨询，不推荐具体证券，不给出买卖时机与目标价。
        {/* ⚠️ 这句原为"页面中任何倾向性描述仅为对第三方原文语气的归类统计，
            不是平台判断"。2026-09-25 界面上的原文倾向已全部移除
            （词表映射多空判定不可靠），整页**不再输出任何倾向性描述** ——
            再留着这句会让用户以为页面上还有倾向标签而去找它。 */}
        页面只呈现公开信息本身与可复算的统计量，全部内容不构成投资建议。
      </div>

      {/* ── 工具条：只有一个手动刷新 ──
          ⚠️ 原这里是**子页签 + 刷新**。子页签已按用户口径删除
          （2026-09-25："将二级目录页签'热点&研报小作文'、'投资日历'
          改为一级目录"）—— 它们现在是顶栏的一级入口，面板内再放一排
          同样的按钮就是**两处导航指同一件事**，而且会让人以为还有第三层。

          刷新按钮必须保留：这是全页**唯一**的手动刷新入口。
          放在这里而不是塞进滚动区，是为了手机上滚动内容时它始终可见。 */}
      <div className="intel-subtabs-row">
        {/* ★ 手动刷新**必须传 `refresh=true`**：不带它时服务端会命中 60 秒
            缓存、原样把同一份还回来 —— 按钮先显示一次"刷新中…"、然后什么
            都没变，用户会以为"新内容真的还没有"。那是"假刷新"。
            ⚠️ 传了它请求也**不会**阻塞：服务端立刻返回当前这份并起后台任务，
            建好后由 WS 通知这里自动补拉（见上面的就绪监听）。 */}
        <span className="muted-text intel-section-hint">
          {section === "calendar" ? "未来日程与预期差" : "平台热点 + 券商研报 + 研究笔记"}
        </span>
        <button className="intel-refresh"
          onClick={() => void loadAll({ refresh: true })}
          disabled={loading}>
          {loading ? "刷新中…" : "↻ 刷新"}
        </button>
      </div>

      {/* ── 后台采集进行中的提示条 ──
          它不是错误、也不是加载遮罩：界面**已经把手里那份画出来了**，
          这里只说明"更新还在路上"。所以放在内容之上、不挡住内容。

          ⚠️ 带上"数据截至 HH:MM"（服务端的 `built_at`）：只说"正在采集"
          会让用户以为屏幕上这份是刚拉的；而它可能是 60 秒内的缓存。
          把时间说出来，用户才能判断"这份够不够新"。 */}
      {section === "hot" && refreshing && !showSkeleton && (
        <div className="intel-collecting">
          <span className="spinner" /> 正在采集最新公开信息
          {feed?.built_at ? `（当前内容截至 ${formatTime(feed.built_at)}）` : ""}
          ，完成后会自动刷新…
        </div>
      )}

      {/* ── 内容 ── */}
      <div className="intel-body">
        {section === "hot" && (
          errFeed ? (
            <ErrorBlock msg={errFeed} onRetry={() => void loadAll()} />
          ) : showSkeleton ? (
            /* ★ 首屏**立刻**是骨架，不是空屏也不是"没有内容"：
               服务端现在毫秒级返回（冷启动给一个形状正确的空壳 +
               refreshing=true），真正的数据 1~3 秒后由通知带回。 */
            <LoadingBlock label="正在采集公开信息…（首屏内容稍后自动出现）" />
          ) : feed ? (
            <>
              {readyTimeout && (
                /* 兜底轮询 30 秒仍没等到 → **如实说**，并给一条手动出路。
                   静默转圈是这里最坏的表现：用户不知道是自己的网、
                   还是服务坏了。 */
                <div className="info-box intel-error">
                  <div>采集耗时较长，还没有新内容。可以稍等，或再点一次刷新。</div>
                  <button className="intel-refresh"
                    onClick={() => void loadAll({ refresh: true })}>重试</button>
                </div>
              )}
              <IntelFeedTab feed={feed} sort={sort} onSort={onSort}
                filter={filter} onFilter={onFilter} busy={feedBusy}
                direction={direction} onDirection={onDirection} />
            </>
          ) : (
            <LoadingBlock label="正在聚合公开信息…" />
          )
        )}

        {section === "calendar" && (
          errCal ? (
            <ErrorBlock msg={errCal} onRetry={() => void loadAll()} />
          ) : cal ? (
            <IntelCalendarTab cal={cal} />
          ) : loadingCal ? (
            <LoadingBlock label="正在读取日程…" />
          ) : (
            <LoadingBlock label="暂无日程数据" />
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
