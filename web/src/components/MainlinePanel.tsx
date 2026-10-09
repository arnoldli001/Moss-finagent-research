import { useCallback, useEffect, useRef, useState } from "react";
import MainlineAlertFlow from "./MainlineAlertFlow";
import MainlineBacktest from "./MainlineBacktest";
import MainlineFutures from "./MainlineFutures";
import MainlineHeatmap from "./MainlineHeatmap";
import MainlineRadar from "./MainlineRadar";
import MainlineTracked from "./MainlineTracked";
import {
  formatDate,
  mainlineApi,
  resolveTaskState,
  type MainlineFuturesSnapshot,
  type MainlineRefreshStatus,
  type MainlineSnapshot,
} from "../mainlineApi";
import {
  mainlineSnapshotParams,
  readMainlineSnapshot,
  writeMainlineSnapshot,
} from "../mainlineCache";

/**
 * 主线挖掘 · 主容器（**顶级页签**，位于「量化交易」右侧、「资金流监控」左侧）。
 *
 * ## 这个模块解决什么问题
 *
 * 每天从全市场板块里挑出"正在被资金建仓、且有主线扩散可能"的少数几个 —— 三层
 * 漏斗：六维基座筛出候选池 → 三维建仓痕迹选出精选 → 龙头共振门控加分。
 * 这个面板就是那套评分的**展示与复盘入口**：漏斗统计 → 评分热力图 → 告警流水
 * → 重点跟踪 → 期货先行 → 回测报告。
 *
 * ## 关键设计取舍
 *
 * 1. **一个快照接口喂全部子视图**。`/snapshot` 一次返回 scores/candidates/selected/
 *    alerts/tracked，所以切换子视图**不重新请求**（除了期货那一个独立接口）——
 *    来回切页签还各转一次圈是最劝退的体验。
 * 2. **轮询只有一处**，挂在容器上（300 秒），子组件全部无副作用；切到期货子视图时
 *    改拉 `/futures`。这是一份**独立页签**，不再有"父容器也轮一份"的问题。
 * 3. **手动刷新是"任务 + 轮询"而不是同步等待**。一轮评分要拉全市场日线，几十秒到
 *    几分钟（与板块拥挤度同一套做法），同步等必然被反代/浏览器掐断。
 * 4. **下钻用弹层**（`MainlineRadar`）而不是右侧分栏：弹层能同时服务
 *    "点热力图 / 点卡片 / 点告警"三个入口，只维护一份状态，也不会因为
 *    详情面板常驻而把主视图挤窄。
 * 5. **整页宽度排版**（不再是 380px 侧栏）。热力图列数、告警表列宽都按整页
 *    自适应 —— 这也是把它从「资金流监控」里提级出来的直接原因（见
 *    `FundFlowPanel.tsx` 的注释）。
 * 6. ★ **首帧先画本地缓存**（`mainlineCache.ts`，2026-10-08 加）。
 *    后端早就热了（服务端 p50 = 19 ms），但这条公网链路上**每次请求固定
 *    ~1.0~1.2 s**（64 字节的探针也一样），而这个面板是三元链渲染、
 *    切走即卸载 —— 没有缓存就等于"每切一次等一趟往返"。
 *    所以缓存命中时不再显示「正在读取评分…」，直接出内容，请求照发
 *    （stale-while-revalidate）；显示的是缓存那份的 `generated_at`，
 *    **不伪造新鲜度**。
 */

/** 快照轮询周期：评分一天只算一次，300 秒足够；再密就是白打库。 */
const POLL_MS = 300000;
/** 刷新任务轮询周期 */
const TASK_POLL_MS = 1500;
/**
 * 轮询容错次数：连续失败这么多次才认定"真的跟后端断了"。
 *
 * 为什么不能一失败就放弃（2026-09-23 用户报障）：刷新期间后端重启（改代码/换配置），
 * 有一次轮询就会撞上连接被拒 → 原来的实现立刻 `stopTaskPoll()` + 清空任务 +
 * 报「查询刷新进度失败：Failed to fetch」。用户看到的是"刷新报错"，而任务其实
 * 已经在后端跑完了 —— 一次网络抖动把整个交互判了死刑，还把原因说成模块没挂载。
 * 8 × 1.5s ≈ 12 秒的容忍窗口足够覆盖一次重启。
 */
const TASK_POLL_TOLERANCE = 8;

/** 子视图：评分热力图 / 告警流水 / 重点跟踪 / 期货先行 / 回测报告。 */
type Tab = "heat" | "alerts" | "tracked" | "futures" | "backtest";

const TABS: { key: Tab; label: string; title: string }[] = [
  { key: "heat", label: "评分热力", title: "全市场板块按总分排序的色块矩阵" },
  { key: "alerts", label: "告警流水", title: "历史告警及其后的5/10/20/60日表现" },
  { key: "tracked", label: "重点跟踪", title: "跟踪池的评分、资金与龙头股" },
  { key: "futures", label: "期货先行", title: "期货异动仪表盘 + 期股联动热力图" },
  { key: "backtest", label: "回测报告", title: "IC/胜率/分场景验证与报告正文" },
];

export default function MainlinePanel() {
  const [tab, setTab] = useState<Tab>("heat");
  /**
   * ★ 首帧用**本地缓存**初始化（0 往返就画出内容）。
   *
   * 用 `useState` 的惰性初值而不是 `useEffect` 里补一刀：这样**第一帧**
   * 就有数据，不会先闪一下「正在读取评分…」再被覆盖（那正是用户看到的
   * "每次都要等 2-3 秒"）。
   *
   * 过期 / 损坏 / 隐私模式一律返回 `null` → 退回原来的冷路径行为，
   * 不会更差（见 `mainlineCache.ts`）。
   */
  const [snapshot, setSnapshot] = useState<MainlineSnapshot | null>(
    () => readMainlineSnapshot()?.snapshot ?? null);
  const [futures, setFutures] = useState<MainlineFuturesSnapshot | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  /** 下钻目标（空 = 关闭） */
  const [picked, setPicked] = useState<{ code: string; name: string }>(
    { code: "", name: "" });
  /** 手动刷新任务：task_id + 运行态 + 进度文案 */
  const [task, setTask] = useState<{ id: string; state: string;
                                    message: string } | null>(null);
  const pollRef = useRef<number | null>(null);
  /** 连续轮询失败计数（成功一次即清零）：见 `TASK_POLL_TOLERANCE`。 */
  const pollFailRef = useRef(0);
  /**
   * 用 ref 记录"是否已经有数据"，而不是把 snapshot/futures 放进轮询 effect 的依赖：
   * 一旦进依赖，"取数成功 → state 变化 → effect 重跑 → 定时器重建"会形成循环
   * （每次轮询都重置计时器，低频轮询就永远等不到下一次触发）。
   */
  const hasSnapshotRef = useRef(false);
  const hasFuturesRef = useRef(false);
  /**
   * 首帧是否来自本地缓存 —— 决定挂载后第一次核对是**静默**还是**转圈**。
   *
   * 缓存命中时不能再用 `loadSnapshot()` 的非静默分支：那会把 `loading`
   * 置真，`MainlineHeatmap` 于是又显示「正在读取评分…」，
   * 刚画出来的内容被加载态盖掉 —— 优化等于没做。
   */
  const seededRef = useRef(snapshot !== null);

  const loadSnapshot = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      const data = await mainlineApi.snapshot(mainlineSnapshotParams());
      setSnapshot(data);
      // 每次取到新的都刷缓存：用户停留在页面上时 300 秒一次的静默轮询
      // 会让缓存一直新鲜，下次切回来仍是"0 往返"。
      writeMainlineSnapshot(data);
      hasSnapshotRef.current = true;
      setError("");
    } catch (exc) {
      // 静默轮询失败不覆盖上一次的成功结果：网络抖一下就把整屏清空是最糟的表现
      if (!silent) setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      if (!silent) setLoading(false);
    }
  }, []);

  const loadFutures = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      setFutures(await mainlineApi.futures({ top: 89 }));
      hasFuturesRef.current = true;
      setError("");
    } catch (exc) {
      if (!silent) setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      if (!silent) setLoading(false);
    }
  }, []);

  // 首屏 + 低频轮询：一次只挂一个定时器，按当前子视图决定拉哪个接口
  useEffect(() => {
    if (tab === "futures") {
      if (!hasFuturesRef.current) void loadFutures();
    } else if (!hasSnapshotRef.current) {
      // 缓存命中 → 静默核对（内容已经在屏幕上）；没命中 → 照旧显示加载态
      void loadSnapshot(seededRef.current);
      seededRef.current = false;
    }
    const timer = window.setInterval(() => {
      if (tab === "futures") void loadFutures(true);
      else void loadSnapshot(true);
    }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [tab, loadFutures, loadSnapshot]);

  const stopTaskPoll = useCallback(() => {
    if (pollRef.current !== null) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  useEffect(() => stopTaskPoll, [stopTaskPoll]);

  /** 轮询刷新任务；done 时把 result 直接写进快照（省一次请求）。 */
  const pollTask = useCallback(async (taskId: string) => {
    try {
      const status: MainlineRefreshStatus =
        await mainlineApi.refreshStatus(taskId);
      pollFailRef.current = 0;
      const state = resolveTaskState(status);
      setTask({ id: status.task_id || taskId, state,
                message: status.error || status.message });
      if (state !== "running" && state !== "queued") {
        stopTaskPoll();
        if (state === "done") {
          if (status.result) {
            setSnapshot(status.result);
            // 手动刷新的结果同样进缓存：下一次切回来仍是 0 往返
            writeMainlineSnapshot(status.result);
          } else await loadSnapshot(true);
          if (tab === "futures") await loadFutures(true);
        } else if (state === "idle" || state === "") {
          /* 后端"查不到这个任务"= 进程重启过（任务表在进程内存里，`state=idle`
             伴随 `message=还没有任务`）。任务其实多半已经跑完，所以直接重读快照，
             而不是把「还没有任务」原样摆在页面上让用户猜。 */
          setTask({ id: "", state: "done",
                    message: "刷新期间后端重启过，任务已结束，已重新读取快照" });
          await loadSnapshot(true);
          if (tab === "futures") await loadFutures(true);
        }
      }
    } catch (exc) {
      // 单次失败先重试：后端重启期间的连接被拒不该把正在跑的任务丢掉（见
      // `TASK_POLL_TOLERANCE` 的说明）。只有连续失败才认输并如实报原因。
      pollFailRef.current += 1;
      if (pollFailRef.current <= TASK_POLL_TOLERANCE) {
        setTask((prev) => (prev
          ? { ...prev, message: `连接中断，正在重试（${pollFailRef.current}/`
            + `${TASK_POLL_TOLERANCE}）…` }
          : prev));
        return;
      }
      stopTaskPoll();
      pollFailRef.current = 0;
      setTask(null);
      setError(`查询刷新进度失败：${exc instanceof Error
        ? exc.message : String(exc)}`);
    }
  }, [stopTaskPoll, loadSnapshot, loadFutures, tab]);

  const startRefresh = useCallback(async () => {
    setError("");
    setTask({ id: "", state: "running", message: "正在提交刷新任务…" });
    try {
      // sync_days=5：先补拉最近 5 个自然日再打分。
      // 为什么不传 0（纯读本地仓）：本地数据往往停在几天前，只重算会得到和上次
      // 一样的分数、看起来像"点了没反应"；5 天足够覆盖周末与节假日，代价只有几秒。
      const started = await mainlineApi.refresh("", 5);
      setTask({ id: started.task_id, state: "running",
                message: "任务已提交，正在同步数据并评分…" });
      stopTaskPoll();
      pollRef.current = window.setInterval(
        () => void pollTask(started.task_id), TASK_POLL_MS);
    } catch (exc) {
      setTask(null);
      setError(`触发刷新失败：${exc instanceof Error ? exc.message : String(exc)}`);
    }
  }, [pollTask, stopTaskPoll]);

  const openDrill = useCallback((code: string, name: string) => {
    setPicked({ code, name });
  }, []);
  const closeDrill = useCallback(() => setPicked({ code: "", name: "" }), []);

  const counts = snapshot
    ? {
      board: snapshot.board_count,
      scored: snapshot.scored_count,
      candidate: snapshot.candidate_count,
      selected: snapshot.selected_count,
      alert: snapshot.alerts?.length ?? snapshot.alert_count,
    }
    : null;

  const taskRunning = task !== null
    && (task.state === "running" || task.state === "queued");
  const gaps = snapshot?.gaps ?? [];
  const refreshHint = snapshot?.refresh_hint ?? "";

  /**
   * 本地仓状态摘要（后端在 snapshot 里顺手带回，不额外请求）。
   *
   * 只报"缓存文件 + 每张表行数 + 同步台账末端"，不铺全表：这是脚注，
   * 作用是让用户判断"没数据"到底是**没同步**还是**同步了但没算出来**。
   */
  const syncText = (() => {
    const status = snapshot?.data_status;
    if (!status) return "";
    const tables = Object.entries(status.tables ?? {});
    const sync = status.sync ?? [];
    const okCount = sync.filter((item) => item.status === "ok").length;
    const last = sync.filter((item) => item.span_end)
      .sort((left, right) => String(right.span_end)
        .localeCompare(String(left.span_end)))[0];
    return "本地仓 " + (status.cache_path || "—")
      + (tables.length > 0
        ? ` · ${tables.map(([name, rows]) => `${name} ${rows}`).join(" / ")}`
        : " · 无表")
      + ` · 数据集 ${okCount}/${sync.length} 正常`
      + (last ? ` · 最新数据到 ${formatDate(last.span_end)}（${last.dataset}）` : "")
      + (status.gaps.length > 0 ? ` · ⚠️ ${status.gaps.join("；")}` : "");
  })();

  return (
    <div className="mainline-root">
      {/* ① 顶部：交易日 + 时段 + 漏斗 */}
      <div className="mainline-head">
        <div className="mainline-head-top">
          <b>主线挖掘</b>
          <span className="mono muted-text">
            {formatDate(snapshot?.trade_date)}
          </span>
          {snapshot?.session_label && (
            <span className={`mainline-session state-${snapshot.session_state
              || "unknown"}`}>
              {snapshot.session_label}
            </span>
          )}
          {snapshot?.weight_mode && (
            <span className="muted-text" title="评分权重档位">
              {snapshot.weight_mode}
            </span>
          )}
          <span style={{ flex: 1 }} />
          <button className="btn-ghost tiny" disabled={taskRunning || loading}
                  title={"先补拉最近 5 个自然日的行情/资金流，再重算全部板块评分"
                    + "（后台任务，提交后可以关掉页面）"}
                  onClick={() => void startRefresh()}>
            {taskRunning ? "刷新中…" : "↻ 立即刷新"}
          </button>
          <button className="btn-ghost tiny" disabled={loading}
                  title="只重新读取接口，不触发重算"
                  onClick={() => void (tab === "futures"
                    ? loadFutures() : loadSnapshot())}>
            {loading ? "…" : "重读"}
          </button>
        </div>

        {/* 漏斗：全市场 → 候选 → 精选 → 告警 */}
        {counts && (
          <div className="mainline-funnel">
            <span className="mainline-funnel-item">
              <i className="muted-text">全市场</i><b>{counts.board}</b>
            </span>
            <span className="mainline-funnel-arrow">→</span>
            <span className="mainline-funnel-item">
              <i className="muted-text">已评分</i><b>{counts.scored}</b>
            </span>
            <span className="mainline-funnel-arrow">→</span>
            <span className="mainline-funnel-item candidate">
              <i className="muted-text">候选</i><b>{counts.candidate}</b>
            </span>
            <span className="mainline-funnel-arrow">→</span>
            <span className="mainline-funnel-item selected">
              <i className="muted-text">精选</i><b>{counts.selected}</b>
            </span>
            <span className="mainline-funnel-arrow">→</span>
            <span className="mainline-funnel-item alert">
              <i className="muted-text">告警</i><b>{counts.alert}</b>
            </span>
          </div>
        )}

        {taskRunning && (
          <div className="mainline-task" role="status" aria-live="polite">
            <span className="spinner" />
            <span className="muted-text">
              {task?.message || "刷新中…"}
              {task?.id ? ` · ${task.id}` : ""}
            </span>
          </div>
        )}
        {task && !taskRunning && task.state === "done" && (
          <div className="mainline-done">✅ {task.message || "刷新完成"}</div>
        )}
        {task && !taskRunning && task.state === "failed" && (
          <div className="error-box">刷新失败：{task.message || "未知原因"}</div>
        )}

        {refreshHint && (
          <div className="mainline-hint">💡 {refreshHint}</div>
        )}
      </div>

      {/* ② 子视图切换 */}
      <div className="mainline-tabs">
        {TABS.map((item) => (
          <button key={item.key} title={item.title}
                  className={"mainline-tab" + (tab === item.key ? " active" : "")}
                  onClick={() => setTab(item.key)}>
            {item.label}
          </button>
        ))}
      </div>

      {error && tab !== "futures" && (
        <div className="error-box">
          读取主线挖掘数据失败：{error}
          <div className="muted-text">
            {/* 提示必须分情况：把"连不上后端"说成"模块没挂载"会把用户引向错误的排查方向
                （2026-09-23 实测：后端正在重启，用户照着"模块没挂载"去查了半天）。 */}
            {error.includes("无法连接后端")
              ? "（连不上后端：多为服务正在重启或已停止；恢复后本面板会自动重新拉取）"
              : "（后端模块还没挂载时这里会一直报错；接口前缀 /api/v1/mainline）"}
          </div>
        </div>
      )}

      {/* ③ 空数据态：gaps 与 refresh_hint 必须显眼 —— 否则用户只会看到一片空白 */}
      {!loading && snapshot && snapshot.scores.length === 0 && tab !== "futures"
        && tab !== "backtest" && (
        <div className="mainline-empty mainline-empty-lg">
          <b>暂无评分数据</b>
          <div>
            交易日 {formatDate(snapshot.trade_date)} · 全市场
            {" "}{snapshot.board_count} 个板块，已评分 {snapshot.scored_count} 个。
            点上方「立即刷新」触发一次同步 + 评分。
          </div>
          {refreshHint && <div>💡 {refreshHint}</div>}
          {gaps.length > 0 && (
            <div>⚠️ {gaps.join("；")}</div>
          )}
        </div>
      )}

      {/* ④ 子视图内容（全部复用同一份 snapshot） */}
      <div className="mainline-body">
        {tab === "heat" && (
          <MainlineHeatmap scores={snapshot?.scores ?? []}
                           onPick={openDrill} loading={loading} />
        )}
        {tab === "alerts" && (
          <MainlineAlertFlow alerts={snapshot?.alerts ?? []}
                             onPick={openDrill} loading={loading} />
        )}
        {tab === "tracked" && (
          <MainlineTracked tracked={snapshot?.tracked ?? []}
                           onPick={openDrill} loading={loading} />
        )}
        {tab === "futures" && (
          <MainlineFutures data={futures} loading={loading} error={error} />
        )}
        {tab === "backtest" && <MainlineBacktest onPickBoard={openDrill} />}
      </div>

      {/* ⑤ 脚注：数据来源与缺口（只在 snapshot 视图显示） */}
      {snapshot && tab !== "backtest" && tab !== "futures" && (
        <div className="mainline-foot muted-text">
          {snapshot.source_notes.length > 0
            ? `来源：${snapshot.source_notes.join(" · ")}`
            : "来源：—"}
          {" "}· 生成于 {(snapshot.generated_at || "").replace("T", " ").slice(0, 19) || "—"}
          {/* 本地仓统计来自 snapshot 自带的 data_status（后端顺手回的），
              所以不用再单独打一次 /data/status */}
          {syncText && <div>{syncText}</div>}
          {gaps.length > 0 && (
            <div>⚠️ 缺数据：{gaps.join("；")}</div>
          )}
          {snapshot.disclaimer && <div>{snapshot.disclaimer}</div>}
        </div>
      )}

      {/* ⑥ 下钻弹层（热力图/卡片/告警共用） */}
      {picked.code && (
        <MainlineRadar boardCode={picked.code} boardName={picked.name}
                       tradeDate={snapshot?.trade_date} days={60}
                       onClose={closeDrill} />
      )}
    </div>
  );
}
