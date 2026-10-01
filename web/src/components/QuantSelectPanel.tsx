import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  type QuantBatchWatchResult,
  type QuantSector,
  type QuantSelectionItem,
  type QuantSelectionRun,
  type QuantSelectStatus,
  type QuantStockNews,
} from "../api";

/**
 * 量化选股（量化交易 tab 的子模块）。
 *
 * ## 这个模块做什么
 *
 * 每个 A 股开市日 **09:25–09:45** 与 **14:45**，服务端用那 3 个按流通市值分档
 * 训练好的模型（20–150 亿 / 150–500 亿 / 500 亿+）自动跑一轮选股；结果进这里，
 * **不自动写自选池** —— 用户点「＋加自选」才写 `configs/intraday.yaml`。
 *
 * ## 自定义板块（类同花顺/东财的炒股软件）
 *
 * 板块**既是选股范围也是归类标签**：
 *   - 当范围：勾选板块后点「选股」，后端把候选池换成这些板块的成分，
 *     阈值也在板块内部取（而不是"全市场选完再筛" —— 后者板块一变小就选不出票）；
 *   - 当标签：结果里每只票显示命中的板块，便于归类查看。
 *
 * `dynamic` 板块由服务端按规则求值（如"流通市值 20–150 亿 + 非 ST"），
 * `manual` 板块是手工维护的成分股。
 */

const STATUS_POLL_MS = 20000;

const WINDOW_LABEL: Record<string, string> = {
  open: "开盘窗口 9:25–9:45",
  close: "尾盘窗口 14:45",
  manual: "手动触发",
};

function fmtMoney(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const yi = value / 1e8;
  return `${yi >= 100 ? yi.toFixed(0) : yi.toFixed(1)}亿`;
}

/**
 * 当日涨跌幅：`+3.42%` / `-1.08%`；没有数据时 `—`。
 *
 * 用户口径 2026-09-18：选股结果表要能一眼看出"这票今天涨了还是跌了"
 * （判断现在追还是等回踩的第一步），所以补这一列、去掉「用到模型档」。
 */
function fmtPct(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
}

/** 涨跌幅的涨跌色（A股习惯：红涨绿跌，与项目其它模块一致）。 */
function pctTone(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "muted-text";
  }
  if (value > 0) return "impact-positive";
  if (value < 0) return "impact-negative";
  return "muted-text";
}

/**
 * 个股消息列的展示：`(标题, 时间)`。
 *
 * 只显示**最新一条**（表里放不下多条），完整标题进 title 属性；
 * 有链接时渲染成可点的 `<a>`（东财原文）。取不到时给"暂无"，
 * 并把服务端给的原因放进 title —— 让用户能区分"确实没消息"和"取数失败"。
 */
function NewsCell({ news }: { news: QuantStockNews | undefined }) {
  if (!news) {
    return <span className="muted-text">加载中…</span>;
  }
  const latest = news.items[0];
  if (!latest) {
    return (
      <span className="muted-text" title={news.reason || "近期没有该股相关消息"}>
        暂无
      </span>
    );
  }
  const when = latest.publish_time.length >= 16
    ? latest.publish_time.slice(5, 16) : latest.publish_time;
  return (
    <span className="qsel-news"
          title={`${latest.publish_time} ${latest.media}\n${latest.title}\n${latest.summary}`}>
      {latest.url
        ? <a href={latest.url} target="_blank" rel="noreferrer noopener">
            {latest.title}
          </a>
        : <span>{latest.title}</span>}
      <span className="muted-text mono qsel-news-time">{when}</span>
    </span>
  );
}

function fmtTime(value: string): string {
  if (!value) return "—";
  return value.length >= 16 ? value.slice(5, 16).replace("T", " ") : value;
}

/**
 * 把后端错误里的**绝对路径**洗掉（用户口径 2026-09-18：前端不要提示绝对路径）。
 *
 * 后端异常信息天然带 `D:\code\Moss-finagent-research\moss_selector\models` 这种
 * 本机路径：对用户既无意义（他不在那台机器的那个目录下操作），又暴露部署结构。
 * 但也不能把路径整段删掉 —— "哪个目录/哪个文件"是排障的关键线索，
 * 所以统一压成**相对项目的路径**（`moss_selector/models`），既看得懂又不泄露根路径。
 */
function tidyPathText(text: string): string {
  if (!text) return "";
  return text
    // Windows 绝对路径（可带盘符与反斜杠/正斜杠混合）
    .replace(/[A-Za-z]:[\\/][^\s，,；;）)"'「」]*/g, (match) => shortenPath(match))
    // 类 Unix 绝对路径（只处理明显的项目内目录，避免误伤 URL）
    .replace(/\/(?:home|Users|root|opt|srv)\/[^\s，,；;）)"'「」]*/g,
             (match) => shortenPath(match));
}

/** `/a/b/Moss-finagent-research/moss_selector/models` → `moss_selector/models` */
function shortenPath(raw: string): string {
  const normalized = raw.replace(/\\/g, "/").replace(/[)"'」]+$/, "");
  const parts = normalized.split("/").filter(Boolean);
  const anchor = parts.findIndex((part) =>
    /^(moss_selector|src|web|data|configs|docs|tests|scripts|logs)$/.test(part));
  if (anchor >= 0) return parts.slice(anchor).join("/");
  // 找不到项目内锚点时只保留最后一段，避免把整条本机路径摊在界面上
  return parts.length ? `…/${parts[parts.length - 1]}` : raw;
}

/** 服务端原因文案的统一出口：洗掉绝对路径后再展示。 */
function tidyReason(text: string): string {
  return tidyPathText(text);
}

/** 批量加入的**可核对回执**：分开说清加了几只、跳过几只、哪几只没成功。 */
function batchSummary(result: QuantBatchWatchResult): string {
  const parts: string[] = [];
  if (result.added.length > 0) parts.push(`新增 ${result.added.length} 只`);
  if (result.repaired.length > 0) parts.push(`补名 ${result.repaired.length} 只`);
  if (result.existing.length > 0) {
    parts.push(`已在池中 ${result.existing.length} 只（保持原样，未覆盖手配的板块/海外映射）`);
  }
  if (result.failed.length > 0) {
    parts.push(`失败 ${result.failed.length} 只（${result.failed.map((row) => row.code)
      .join("、")}：${result.failed[0].reason}）`);
  }
  if (result.missing_name.length > 0) {
    parts.push(`名称未解析 ${result.missing_name.length} 只（配置里名字留空，不拿代码冒充）`);
  }
  if (parts.length === 0) return "没有需要改动的票";
  return `自选池现有 ${result.total} 只：${parts.join("，")}`;
}

/**
 * 导出本轮选股的**股票名称**为 txt（用户口径：每只票用空格分开）。
 *
 * 名称缺失时回落到代码并在返回值里计数 —— 导出文件"少一只票"是发现不了的，
 * 用一个代码占位至少看得见。
 */
function exportNamesTxt(
  items: QuantSelectionItem[], filename: string,
): { count: number; fallback: number } {
  let fallback = 0;
  const names = items.map((item) => {
    const name = (item.name || "").trim();
    if (name) return name;
    fallback += 1;
    return item.code;
  });
  // 末尾补一个换行：文本编辑器与多数行情软件的"导入自选"都更省事
  const blob = new Blob([`${names.join(" ")}\n`],
                        { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  URL.revokeObjectURL(url);
  return { count: names.length, fallback };
}

export default function QuantSelectPanel({
  onAdded,
  onSectorsChanged,
}: {
  /**
   * 加入自选成功后通知父级（左侧自选侧栏）：把新加的代码**滚进视野并高亮**。
   *
   * 自选池是追加顺序，一键加 20 只之后它们在 40+ 只里的最后一段 ——
   * 侧栏默认按配置顺序显示，那一段正好在可视区之外，
   * 于是"加了自选但左侧列表里没有"（用户报障）。
   * 这里不改配置顺序（用户的排列有意义），只负责让新票露脸。
   */
  onAdded?: (codes: string[]) => void;
  /**
   * 板块成分变动后回报父级（左侧板块下拉），让它立刻重读列表。
   *
   * 板块的**新建/删除/加成分整体搬到了左侧抽屉**，本面板只在「选股结果」里
   * 往板块加票。不回报的话左侧还显示旧的数量与成分，用户以为没加进去。
   */
  onSectorsChanged?: () => void;
} = {}) {
  const [status, setStatus] = useState<QuantSelectStatus | null>(null);
  const [run, setRun] = useState<QuantSelectionRun | null>(null);
  const [history, setHistory] = useState<QuantSelectionRun[]>([]);
  const [sectors, setSectors] = useState<QuantSector[]>([]);
  const [tab, setTab] = useState<"result" | "history">("result");
  const [selectedSectors, setSelectedSectors] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [batchBusy, setBatchBusy] = useState(false);
  const [notice, setNotice] = useState<{ text: string; level: "info" | "warn" } | null>(
    null);
  const noticeTimer = useRef<number | null>(null);
  const [detailCode, setDetailCode] = useState("");
  /** 个股消息面：`{代码: 结果}`；按选股结果里的票懒加载（不阻塞结果表渲染）。 */
  const [news, setNews] = useState<Record<string, QuantStockNews>>({});

  /** 浮动提示：固定右下角、9 秒自消（不能把面板挤下去 —— 用户明确要求过）。 */
  const toast = useCallback((text: string, level: "info" | "warn" = "info") => {
    setNotice({ text, level });
    if (noticeTimer.current) window.clearTimeout(noticeTimer.current);
    noticeTimer.current = window.setTimeout(() => setNotice(null), 9000);
  }, []);

  useEffect(() => () => {
    if (noticeTimer.current) window.clearTimeout(noticeTimer.current);
  }, []);

  const loadStatus = useCallback(async () => {
    try {
      setStatus(await api.quantSelectStatus());
    } catch {
      setStatus(null);
    }
  }, []);

  const loadSectors = useCallback(async () => {
    try {
      const data = await api.quantSectors(true);
      setSectors(data.sectors);
    } catch {
      setSectors([]);
    }
  }, []);

  /** 「立即训练模型」：模型缺失时的重试入口（自动训练失败/等不及时用）。 */
  const [trainBusy, setTrainBusy] = useState(false);
  const trainNow = useCallback(async () => {
    setTrainBusy(true);
    try {
      const result = await api.quantSelectTrain();
      toast(result.message || "训练已开始");
      await loadStatus();
    } catch (error) {
      toast(`启动训练失败：${tidyReason(
        error instanceof Error ? error.message : String(error))}`, "warn");
    } finally {
      setTrainBusy(false);
    }
  }, [toast, loadStatus]);

  const loadLatest = useCallback(async () => {
    try {
      const data = await api.quantSelectLatest();
      setRun(data.available === false ? null : data);
    } catch {
      setRun(null);
    }
  }, []);

  const loadHistory = useCallback(async () => {
    try {
      const data = await api.quantSelectRuns(20);
      setHistory(data.runs);
    } catch {
      setHistory([]);
    }
  }, []);

  useEffect(() => {
    void loadStatus();
    void loadLatest();
    void loadSectors();
  }, [loadStatus, loadLatest, loadSectors]);

  // 状态轮询：定时选股在服务端跑，前端不需要触发，只要能看到"刚跑完"
  useEffect(() => {
    const timer = window.setInterval(() => { void loadStatus(); }, STATUS_POLL_MS);
    return () => window.clearInterval(timer);
  }, [loadStatus]);

  useEffect(() => {
    if (tab === "history") void loadHistory();
  }, [tab, loadHistory]);

  /**
   * 个股消息面：结果表出现后**按需加载**（懒加载，不拖慢选股结果本身的渲染）。
   *
   * - 只请求"结果里还没有消息"的代码，且服务端自身有 TTL 缓存，
   *   所以历史记录来回切换不会反复打对方接口；
   * - 整批失败只把这一批标记为"暂无"，页面其它部分照常 ——
   *   消息面是增强信息，坏了不该让选股结果不可用。
   */
  useEffect(() => {
    const codes = (run?.items ?? [])
      .map((item) => item.code)
      .filter((code) => /^\d{6}$/.test(code) && !(code in news));
    if (codes.length === 0) return;
    let cancelled = false;
    (async () => {
      try {
        const data = await api.quantSelectNews(codes, 3);
        if (!cancelled) {
          setNews((current) => ({ ...current, ...data.news }));
        }
      } catch {
        if (!cancelled) {
          setNews((current) => ({
            ...current,
            ...Object.fromEntries(codes.map((code) => [code, {
              code, items: [], source: "", reason: "消息面接口不可用",
            }])),
          }));
        }
      }
    })();
    return () => { cancelled = true; };
  }, [run, news]);


  const runSelection = useCallback(async () => {
    setBusy(true);
    toast("正在跑选股……全市场一轮要几十秒到几分钟，请勿关闭页面");
    try {
      const result = await api.quantSelectRun({
        sector_filter: selectedSectors,
        top_n: null,
      });
      setRun(result);
      setTab("result");
      if (result.error) {
        toast(`选股失败：${result.error}`, "warn");
      } else {
        toast(`选出 ${result.selected} 只（${result.trade_date}，`
          + `打分 ${result.scored} 只，阈值 ${result.threshold.toFixed(4)}）`);
      }
      await loadStatus();
    } catch (error) {
      toast(`选股失败：${error instanceof Error ? error.message : String(error)}`,
        "warn");
    } finally {
      setBusy(false);
    }
  }, [selectedSectors, toast, loadStatus]);

  const addToWatchlist = useCallback(async (item: QuantSelectionItem) => {
    try {
      await api.quantSelectAddToWatchlist(item.code, item.name);
      toast(`已加入自选：${item.code} ${item.name}`);
      setRun((current) => current === null ? current : {
        ...current,
        items: (current.items ?? []).map((row) =>
          row.code === item.code ? { ...row, added: true } : row),
      });
      onAdded?.([item.code]);
    } catch (error) {
      toast(`加入自选失败：${error instanceof Error ? error.message : String(error)}`,
        "warn");
    }
  }, [toast, onAdded]);

  const toggleSector = useCallback((name: string) => {
    setSelectedSectors((current) =>
      current.includes(name)
        ? current.filter((value) => value !== name)
        : [...current, name]);
  }, []);

  const items = useMemo(() => run?.items ?? [], [run]);
  /** 本轮里**还没进自选池**的票 —— 「全部加自选」只加这些。 */
  const pendingItems = useMemo(
    () => items.filter((item) => !item.added), [items]);

  const addAllToWatchlist = useCallback(async () => {
    if (pendingItems.length === 0) return;
    setBatchBusy(true);
    try {
      const result = await api.quantSelectAddManyToWatchlist(
        pendingItems.map((item) => ({ code: item.code, name: item.name })));
      // 只有**真的动过**的才回标成已加（existing 是没动过的，别谎报）
      const touched = new Set([...result.added, ...result.repaired]);
      if (touched.size > 0) {
        setRun((current) => current === null ? current : {
          ...current,
          items: (current.items ?? []).map((row) =>
            touched.has(row.code) ? { ...row, added: true } : row),
        });
        // 让左侧自选侧栏把这几只滚进视野（追加顺序 → 它们在最后一段）
        onAdded?.([...touched]);
      }
      toast(batchSummary(result), result.failed.length > 0 ? "warn" : "info");
    } catch (error) {
      toast(`批量加入自选失败：${error instanceof Error ? error.message : String(error)}`,
        "warn");
    } finally {
      setBatchBusy(false);
    }
  }, [pendingItems, toast, onAdded]);

  const exportNames = useCallback(() => {
    if (items.length === 0) return;
    const tag = `${run?.trade_date ?? ""}${run?.window ? `_${run.window}` : ""}`;
    const { count, fallback } = exportNamesTxt(
      items, `量化选股_${tag || "result"}.txt`);
    toast(fallback > 0
      ? `已导出 ${count} 只的名称（其中 ${fallback} 只没有名称，用代码占位）`
      : `已导出 ${count} 只的名称（空格分隔的 txt）`);
  }, [items, run, toast]);

  /**
   * 「全部加入板块」：把本轮**全部**选股结果写进一个自定义板块。
   *
   * ## 为什么是弹窗选目标，而不是固定加到某个板块
   *
   * 板块既是选股范围也是归类标签，用户口径 2026-09-21：结果要能"全部加进板块"，
   * 但目标由当次决定（今天加进「0918 开盘选股」，明天可能加进另一个），
   * 所以做成选择器：挑已有板块，或**就地新建**一个（新板块一律 `manual`，
   * 规则板块的成分由服务端求值，不该被结果覆盖）。
   *
   * 加完必须 `onSectorsChanged()` —— 板块管理已经搬到左侧抽屉，
   * 不回报的话左侧还显示旧的数量，用户会以为没加进去。
   */
  const [addAllOpen, setAddAllOpen] = useState(false);
  const [targetSectorId, setTargetSectorId] = useState<number | null>(null);
  const [newSectorName, setNewSectorName] = useState("");
  const [addAllBusy, setAddAllBusy] = useState(false);

  const openAddAll = useCallback(async () => {
    // 打开前重读：板块可能在左侧抽屉里刚建刚删，用旧列表会选到一个不存在的 id
    await loadSectors();
    setTargetSectorId(null);
    setNewSectorName("");
    setAddAllOpen(true);
  }, [loadSectors]);

  const submitAddAll = useCallback(async () => {
    if (items.length === 0) return;
    setAddAllBusy(true);
    try {
      let sectorId = targetSectorId;
      if (sectorId === null) {
        const name = newSectorName.trim();
        if (!name) {
          toast("请选择一个已有板块，或填一个新板块名", "warn");
          return;
        }
        const created = await api.quantSaveSector({ name, kind: "manual" });
        sectorId = created.id;
      }
      const result = await api.quantAddSectorMembers(
        sectorId, items.map((item) => ({ code: item.code, name: item.name })));
      toast(result.added > 0
        ? `已加入板块 ${result.added} 只（重复的自动跳过）`
        : "没有新增（这些票都已在板块里）");
      setAddAllOpen(false);
      await loadSectors();
      onSectorsChanged?.();
    } catch (error) {
      toast(`加入板块失败：${error instanceof Error ? error.message : String(error)}`,
        "warn");
    } finally {
      setAddAllBusy(false);
    }
  }, [items, targetSectorId, newSectorName, toast, loadSectors, onSectorsChanged]);

  const summary = useMemo(() => {
    if (!status) return null;
    return {
      model: status.model_version || "（未找到模型）",
      buckets: status.model_buckets,
      trainedAt: status.model_trained_at,
      lastRun: status.last_run_at,
      lastSelected: status.last_selected,
      running: status.running,
    };
  }, [status]);

  const detail = useMemo(() => {
    if (!run?.items) return null;
    return run.items.find((item) => item.code === detailCode) ?? null;
  }, [run, detailCode]);

  return (
    <div className="qsel">
      {/* ---------- 状态条 ---------- */}
      <div className="qsel-status">
        <div className="qsel-status-main">
          <b>量化选股</b>
          <span className="muted-text">
            开市日 9:25–9:45 与 14:45 自动跑；结果只进本模块，点「＋加自选」才写自选池
          </span>
        </div>
        {summary && (
          <div className="qsel-status-meta mono muted-text">
            {summary.buckets.length > 0 ? (
              <>模型档位 {summary.buckets.join(" / ")}</>
            ) : (
              <>模型：{summary.model}</>
            )}
            {summary.trainedAt && <> · 训练 {summary.trainedAt}</>}
            {summary.lastRun && (
              <> · 上一轮 {fmtTime(summary.lastRun)}（{summary.lastSelected} 只）</>
            )}
            {summary.running && <span className="qsel-live"> · 正在跑…</span>}
          </div>
        )}
        {status && !status.available && (
          <div className="warn-box">
            <b>量化选股暂不可用：</b>{tidyReason(status.reason) || "未找到已训练的模型"}
            {/* 模型缺失时后端会**自动开始训练**（用户口径 2026-09-18），
                因此这里不再让用户自己去敲命令；只有训练起不来才提示手动兜底。 */}
            {status.training ? (
              <span className="muted-text">
                —— 已自动开始训练模型（首次训练需要几分钟），完成后本页会自动恢复；
                期间可继续使用其它页签。
              </span>
            ) : (
              <span className="muted-text">
                —— 服务端会自动补训；也可以点下面按钮马上重试。
              </span>
            )}
            <button className="btn-ghost" disabled={trainBusy || status.training}
                    onClick={() => void trainNow()}
                    title="在本机后台重新训练三档模型（首次约需几分钟，不影响其它页签）">
              {status.training ? "训练中…" : trainBusy ? "启动中…" : "立即训练模型"}
            </button>
          </div>
        )}
        {status?.last_error && (
          <div className="warn-box">上一轮失败：{tidyReason(status.last_error)}</div>
        )}
      </div>

      {/* ---------- 子页签 ----------
          「自定义板块」页签已移除：板块的新建/删除/加成分整体搬到左侧抽屉
          （与「自选」同一个位置，见 IntradayTPanel 的抽屉与 useQuantSectors）。
          本页只保留与"跑一轮选股"直接相关的两件事：结果、历史。 */}
      <nav className="mode-switch qsel-tabs">
        <button className={tab === "result" ? "mode-btn active" : "mode-btn"}
                onClick={() => setTab("result")}>
          选股结果
        </button>
        <button className={tab === "history" ? "mode-btn active" : "mode-btn"}
                onClick={() => setTab("history")}>
          历史记录
        </button>
      </nav>

      {tab === "result" && (
        <div className="qsel-body">
          {/* 选股范围 + 手动跑 */}
          <div className="qsel-runbar">
            <span className="muted-text">选股范围</span>
            {sectors.length === 0 ? (
              <span className="muted-text">
                （还没有自定义板块 → 跑全市场；可在「自定义板块」里新建）
              </span>
            ) : (
              <div className="qsel-chips">
                <button className={selectedSectors.length === 0
                  ? "qsel-chip on" : "qsel-chip"}
                        onClick={() => setSelectedSectors([])}
                        title="不限定板块：全市场选股">
                  全市场
                </button>
                {sectors.map((sector) => (
                  <button key={sector.id}
                          className={selectedSectors.includes(sector.name)
                            ? "qsel-chip on" : "qsel-chip"}
                          onClick={() => toggleSector(sector.name)}
                          title={sector.kind === "dynamic"
                            ? `规则板块（服务端求值）：${JSON.stringify(sector.rule)}`
                            : `手工板块，${sector.member_count} 只成分`}>
                    {sector.name}
                    <span className="muted-text"> {sector.member_count}</span>
                  </button>
                ))}
              </div>
            )}
            <button className="btn-ghost" disabled={busy}
                    onClick={() => void runSelection()}
                    title="立刻按当前范围跑一轮（与定时任务同一套模型与口径）">
              {busy ? "选股中…" : "▶ 立即选股"}
            </button>
          </div>

          {run === null ? (
            <div className="info-box">
              还没有选股记录。开市日到点会自动跑；也可以点「▶ 立即选股」马上跑一轮。
            </div>
          ) : (
            <>
              <div className="qsel-runmeta muted-text mono">
                {run.trade_date} · {WINDOW_LABEL[run.window] ?? run.window} ·
                选出 {run.selected} 只（打分 {run.scored} 只，门槛{" "}
                {run.threshold > 0 ? run.threshold.toFixed(4) : "无（按分数取前 N）"}）
                {" "}· 耗时 {run.seconds.toFixed(0)}s
                {run.sector_filter.length > 0 && (
                  <> · 范围 {run.sector_filter.join("、")}</>
                )}
                {run.error && <span className="qsel-err"> · 失败：{run.error}</span>}
              </div>
              {run.model_detail && (
                <div className="muted-text mono qsel-model">{run.model_detail}</div>
              )}

              {/* 0 只但没有失败原因：这是最容易让人以为"点了没反应"的状态 ——
                  接口 200、耗时正常、error 为空，界面上只剩一张空表格。
                  高分池为 0 说明打分环节整体没产出（例如原生打分异常），
                  必须给一句明确的话，而不是留一张空表。 */}
              {!run.error && run.selected === 0 && run.scored === 0 && (
                <div className="warn-box">
                  <b>本轮没有选出任何标的</b>
                  <span>
                    候选池打分结果为 0 —— 通常是取数/因子环节没产出，或打分整体失败
                    （不是"今天没有符合条件的票"）。服务端会记录原因：
                    <span className="mono">data/run/quant_select_diag.log</span>
                  </span>
                </div>
              )}
              {!run.error && run.selected === 0 && run.scored > 0 && (
                <div className="info-box">
                  本轮按分数排序取前 {run.top_n || "N"} 只，但候选池里没有分数高于
                  门槛的票（当前门槛 {run.threshold.toFixed(4)}）。可在服务端
                  <span className="mono"> moss_selector/config.yaml </span>
                  调整 <span className="mono">selection</span> 口径。
                </div>
              )}

              {/* 数据滞后横幅：**必须有**。用的是旧行情时，分数/阈值/排名全都正常，
                  界面上没有任何别的迹象能让人发现这件事（2026-09-18 真实事故：
                  09:10 选的其实是 0915 的行情）。 */}
              {run.data_stale && (
                <div className="warn-box qsel-stale">
                  <b>⚠ 数据滞后</b>
                  <span>
                    {run.note
                      || `本轮用的是 ${run.trade_date} 的行情，`
                        + `最近一个已收盘交易日是 ${run.expected_trade_date}。`}
                  </span>
                  <span className="muted-text">
                    按「▶ 立即选股」重跑不会变新 —— 要先把行情仓库同步到最新
                    （服务端 <span className="mono">quant_data_sync</span> 作业会在
                    工作日 16:40 自动补，手动补跑：
                    <span className="mono"> python scripts/quant_sync.py download</span>
                    然后 <span className="mono">python scripts/quant_warehouse.py ingest</span>）。
                  </span>
                </div>
              )}

              {/* 批量动作：一键全部加自选 + 导出名称 txt（用户 2026-09-18 要求） */}
              <div className="qsel-actions">
                <button className="btn-ghost"
                        disabled={batchBusy || pendingItems.length === 0}
                        onClick={() => void addAllToWatchlist()}
                        title={pendingItems.length === 0
                          ? "这一轮的票都已在做T自选池里"
                          : "把本轮尚未加入的票一次性写进做T自选池"
                            + "（一次落盘、一次重建；已在池中的票保持原样不动）"}>
                  {batchBusy
                    ? "加入中…"
                    : `＋ 全部加自选（${pendingItems.length}）`}
                </button>
                <button className="btn-ghost"
                        disabled={items.length === 0}
                        onClick={exportNames}
                        title="下载 txt：每只票的名称用空格分开，可直接粘贴/导入行情软件">
                  ⤓ 导出名称 txt（{items.length}）
                </button>
                <button className="btn-ghost"
                        disabled={items.length === 0 || addAllBusy}
                        onClick={() => void openAddAll()}
                        title={"把本轮选出的全部票一次性写进某个自定义板块"
                          + "（可挑已有板块，也可就地新建；不写自选池）"}>
                  ＋ 加入板块（{items.length}）
                </button>
                <span className="muted-text">
                  已在自选池 {items.length - pendingItems.length} / {items.length} 只；
                  导出的是股票名称（空格分隔）
                </span>
              </div>

              {/* 结果表：**表头与数据一律居中、带完整表格线**（用户口径 2026-09-18：
                  原来表头左对齐、数据居中，看着对不齐很丑）。
                  「用到模型档」已从这里移除 —— 打分来源仍可在展开明细里看；
                  「归类板块」也移除，位置让给「个股消息」（对做决定更有用）。 */}
              <table className="qsel-table qsel-result-table">
                <thead>
                  <tr>
                    <th style={{ width: 46 }}>#</th>
                    <th style={{ width: 88 }}>代码</th>
                    <th style={{ width: 100 }}>名称</th>
                    <th style={{ width: 86 }}>分数</th>
                    <th style={{ width: 92 }}>当日涨跌</th>
                    <th style={{ width: 92 }}>流通市值</th>
                    <th>个股消息</th>
                    <th style={{ width: 100 }}>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {(run.items ?? []).map((item) => (
                    <tr key={item.code}
                        className={detailCode === item.code ? "row-active" : ""}
                        onClick={() => setDetailCode(
                          detailCode === item.code ? "" : item.code)}
                        style={{ cursor: "pointer" }}>
                      <td className="mono muted-text">{item.rank}</td>
                      <td className="mono">{item.code}</td>
                      <td>{item.name || "—"}</td>
                      <td className="mono">{item.score.toFixed(4)}</td>
                      <td className={`mono ${pctTone(item.pct_chg)}`}
                          title="当日已实现的收盘涨跌幅（不是预测值）">
                        {fmtPct(item.pct_chg)}
                      </td>
                      <td className="mono">{fmtMoney(item.circ_mv)}</td>
                      <td className="qsel-news-cell">
                        <NewsCell news={news[item.code]} />
                      </td>
                      <td>
                        <button className={item.added
                          ? "btn-ghost done" : "btn-ghost"}
                                disabled={item.added}
                                onClick={(event) => {
                                  event.stopPropagation();
                                  void addToWatchlist(item);
                                }}
                                title={item.added
                                  ? "已在做T自选池里"
                                  : "加入做T自选池（写 configs/intraday.yaml）"}>
                          {item.added ? "✓ 已加" : "＋加自选"}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>

              {detail && (
                <div className="qsel-detail">
                  <b>{detail.code} {detail.name}</b>
                  <span className="muted-text">
                    （点同一行收起）关键因子值：已做当日截面名次映射 [-1,1]，
                    越接近 1 表示该因子在当日全市场里越靠前
                  </span>
                  <table className="data-table">
                    <tbody>
                      {Object.entries(detail.factors).map(([key, value]) => (
                        <tr key={key}>
                          <td className="mono" style={{ width: 220 }}>{key}</td>
                          <td className="mono">
                            {value === null || value === undefined
                              ? "—（该因子当日缺失）"
                              : value.toFixed(4)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {detail.circ_mv !== null && (
                    <div className="muted-text mono">
                      流通市值 {fmtMoney(detail.circ_mv)} → 分档打分用「{detail.cap_bucket || "兜底"}」模型
                    </div>
                  )}
                  {/* 「归类板块」从结果表移除了（用户口径），但信息不能丢：
                      展开明细里仍然给出命中板块，便于核对"这票属于我哪个板块"。 */}
                  {detail.sectors.length > 0 && (
                    <div className="muted-text">
                      命中板块：
                      {detail.sectors.map((name) => (
                        <span key={name} className="flow-group">{name}</span>
                      ))}
                    </div>
                  )}
                  {/* 消息面：表里只放最新一条，这里给前几条 + 来源，便于快速判断题材 */}
                  {(news[detail.code]?.items ?? []).length > 0 && (
                    <div className="qsel-news-detail">
                      <span className="muted-text">
                        个股消息（{news[detail.code].source || "东方财富"}）：
                      </span>
                      {news[detail.code].items.map((entry) => (
                        <div key={`${entry.publish_time}-${entry.title}`}
                             className="qsel-news-detail-item">
                          <span className="mono muted-text">
                            {entry.publish_time.slice(5, 16)}
                          </span>
                          {entry.url
                            ? <a href={entry.url} target="_blank"
                                 rel="noreferrer noopener">{entry.title}</a>
                            : <span>{entry.title}</span>}
                          {entry.media && (
                            <span className="muted-text">（{entry.media}）</span>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {(run.gaps ?? []).length > 0 && (
                <div className="warn-box">
                  数据缺口（这些票的对应因子留空，未臆造）：
                  {run.gaps.slice(0, 6).join("；")}
                </div>
              )}
            </>
          )}
        </div>
      )}

      {tab === "history" && (
        <div className="qsel-body">
          {history.length === 0 ? (
            <div className="info-box">暂无历史记录。</div>
          ) : (
            <table className="data-table qsel-table">
              <thead>
                <tr>
                  <th>交易日</th>
                  <th style={{ width: 150 }}>窗口</th>
                  <th style={{ width: 70 }}>选出</th>
                  <th style={{ width: 90 }}>打分池</th>
                  <th style={{ width: 90 }}>阈值</th>
                  <th style={{ width: 90 }}>耗时</th>
                  <th>范围 / 失败原因</th>
                </tr>
              </thead>
              <tbody>
                {history.map((row) => (
                  <tr key={row.id}>
                    <td className="mono">{row.trade_date}</td>
                    <td className="muted-text">
                      {WINDOW_LABEL[row.window] ?? row.window}
                      <span className="mono"> · {fmtTime(row.ran_at)}</span>
                    </td>
                    <td className="mono">{row.selected}</td>
                    <td className="mono muted-text">{row.scored}</td>
                    <td className="mono muted-text">
                      {row.threshold > 0 ? row.threshold.toFixed(4) : "无门槛"}
                    </td>
                    <td className="mono muted-text">{row.seconds.toFixed(0)}s</td>
                    <td className="muted-text">
                      {row.error
                        ? <span className="qsel-err">{row.error}</span>
                        : (row.sector_filter.length > 0
                          ? row.sector_filter.join("、")
                          : "全市场")}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {/* 「全部加入板块」弹窗：挑已有板块，或就地新建一个 */}
      {addAllOpen && (
        <div className="qsel-modal-backdrop" onClick={() => setAddAllOpen(false)}>
          <div className="qsel-modal" onClick={(event) => event.stopPropagation()}>
            <div className="qsel-modal-head">
              <b>把本轮 {items.length} 只选股结果加入板块</b>
              <button className="notice-close" onClick={() => setAddAllOpen(false)}
                      aria-label="关闭">×</button>
            </div>
            {sectors.length === 0 ? (
              <div className="info-box">
                还没有自定义板块 —— 在下面填个名字，会新建一个手工板块再装进去。
              </div>
            ) : (
              <div className="qsel-chips">
                {sectors.map((sector) => (
                  <button key={sector.id}
                          className={targetSectorId === sector.id
                            ? "qsel-chip on" : "qsel-chip"}
                          onClick={() => {
                            setTargetSectorId(sector.id);
                            setNewSectorName("");
                          }}
                          title={sector.kind === "dynamic"
                            ? `规则板块（成分由服务端按规则求值）：${JSON.stringify(sector.rule)}`
                            : `手工板块，现有 ${sector.member_count} 只成分`}>
                    {sector.name}
                    <span className="muted-text"> {sector.member_count}</span>
                  </button>
                ))}
              </div>
            )}
            <div className="qsel-form-row">
              <input className="qsel-input"
                     placeholder="或新建板块：填名字（如 0918 开盘选股）"
                     value={newSectorName}
                     onChange={(event) => {
                       setNewSectorName(event.target.value);
                       // 填了新名字就让"已有板块"的选择失效，避免两个目标同时亮着
                       if (event.target.value !== "") setTargetSectorId(null);
                     }} />
            </div>
            <div className="qsel-form-row">
              <button className="btn-ghost primary" disabled={addAllBusy}
                      onClick={() => void submitAddAll()}>
                {addAllBusy ? "加入中…" : `＋ 加入板块（${items.length}）`}
              </button>
              <button className="btn-ghost" disabled={addAllBusy}
                      onClick={() => setAddAllOpen(false)}>取消</button>
              <span className="muted-text">
                重复的自动跳过；只写板块成分，不动自选池
              </span>
            </div>
          </div>
        </div>
      )}

      {notice && (
        <div className={`notice-toast ${notice.level}`}>
          <span className="notice-text">{notice.text}</span>
          <button className="notice-close" onClick={() => setNotice(null)}
                  aria-label="关闭提示">×</button>
        </div>
      )}
    </div>
  );
}
