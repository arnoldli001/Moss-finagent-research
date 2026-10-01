import { useEffect, useState } from "react";
import {
  sectorCrowdingApi,
  type CrowdingDetail as DetailPayload,
} from "../sectorCrowdingApi";
import {
  readCrowdingDetail,
  readCrowdingDetailAge,
  writeCrowdingDetail,
} from "../crowdingDetailCache";
import SectorCrowdingDetailChart from "./SectorCrowdingDetailChart";

/**
 * 单板块详情**居中弹窗**。
 *
 * ## 为什么从"下方面板"改成弹窗
 *
 * 原来的详情渲染在告警面板下方，列表有几十行时"点查看详情 → 视线要跳到很远
 * 的下面"，看完还要滚回来点下一个 —— 逐条对比板块时非常别扭。弹窗把注意力
 * 锁在当前板块上，关掉即回到原位置，列表上的选中项不丢。
 *
 * ## 关闭方式（三种都要有）
 *
 * - 右上角 ×
 * - 点遮罩空白处
 * - 按 Esc
 *
 * ## 取数：**先画缓存、再后台核对**（stale-while-revalidate，`CHG-0137`）
 *
 * 这条曲线近 6 年 1400+ 根日线，即服务端已瘦身（gzip 砍半），在 ~51 KB/s
 * 的公网隧道上仍是秒级；劣化档（~4.6 KB/s）下十几秒 —— 那正是用户报障
 * 「十几秒才出数据」的现场。
 *
 * 原写法每次打开都 `setData(null)` + `setLoading(true)`，**必发一次冷请求**，
 * 于是"关掉再看同一个板块"也要重付一遍。现在：
 *
 *   1. 命中缓存 → **立刻画出曲线**（`loading` 保持 false，不显示"正在读取"）；
 *   2. 同时照常发请求核对，拿到新的覆盖 + 写回缓存；
 *   3. 没命中缓存 → 才走原来的 loading 态。
 *
 * ⚠️ **旧数据期间不显示"正在读取"**，但要显示"正在核对" —— 否则用户会以为
 * 看到的就是最新的。拥挤度是日频数据，旧曲线的日期也画在图上。
 */
export default function SectorCrowdingDetailModal({
  sectorCode, sectorName, onClose,
}: {
  /** 空字符串 = 不显示（父级用它控制开关） */
  sectorCode: string;
  sectorName: string;
  onClose: () => void;
}) {
  const [data, setData] = useState<DetailPayload | null>(null);
  const [loading, setLoading] = useState(false);
  /** 手里已有（缓存的）曲线、正在后台核对 —— 与 `loading` 是两件事 */
  const [revalidating, setRevalidating] = useState(false);
  const [error, setError] = useState("");
  const [showRaw, setShowRaw] = useState(true);
  /** 放大视图：弹窗放开到几乎满屏，图表因此更宽、能看清更细的结构 */
  const [expanded, setExpanded] = useState(false);

  const open = Boolean(sectorCode);

  // Esc 关闭：绑在 window 上而不是弹窗节点上，避免焦点不在弹窗里时按 Esc 没反应
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  // 打开期间锁掉背后页面的滚动，关掉后恢复
  useEffect(() => {
    if (!open) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { document.body.style.overflow = previous; };
  }, [open]);

  useEffect(() => {
    if (!sectorCode) { setData(null); return; }
    let alive = true;
    setError("");

    // ① 缓存优先：有就直接画，不进入 loading（这是"打开即有图"的那一半）
    const cached = readCrowdingDetail(sectorCode);
    const cachedAge = readCrowdingDetailAge(sectorCode);
    setData(cached);
    setLoading(cached === null);
    // 缓存很新（< 30 秒）就不必再核对：刚看过一遍，重传纯属浪费隧道带宽。
    // 30 秒是"同一次比较动作内"的量级，不会漏掉盘中刷新。
    const fresh = cached !== null && cachedAge !== null
      && Date.now() - cachedAge < 30_000;
    setRevalidating(cached !== null && !fresh);

    if (fresh) return () => { alive = false; };

    // ② 后台核对（无论有没有缓存都发；有缓存时不阻塞渲染）
    sectorCrowdingApi.detail(sectorCode)
      .then((payload) => {
        if (!alive) return;
        setData(payload);
        writeCrowdingDetail(payload);
      })
      .catch((exc) => {
        if (!alive) return;
        // 有缓存时不要把画面换成错误框 —— 旧曲线仍然可读，
        // 失败只是"这次没核对上"。把提示留给没有数据可画的那种情况。
        setError(exc instanceof Error ? exc.message : String(exc));
      })
      .finally(() => {
        if (alive) { setLoading(false); setRevalidating(false); }
      });
    return () => { alive = false; };
  }, [sectorCode]);

  if (!open) return null;

  return (
    <div className="crowding-modal-backdrop"
         role="presentation"
         onClick={onClose}>
      <div className={"crowding-modal" + (expanded ? " expanded" : "")}
           role="dialog" aria-modal="true"
           aria-label={`${sectorName || sectorCode} 拥挤度详情`}
           onClick={(event) => event.stopPropagation()}>
        <div className="crowding-modal-head">
          <h3>{data?.sector_name || sectorName || sectorCode}</h3>
          <span className="mono muted-text">{sectorCode}</span>
          <span className="muted-text">
            水位 = 当前平滑拥挤度 / 近 6 年最高值
          </span>
          <span style={{ flex: 1 }} />
          {revalidating && <span className="muted-text">正在核对…</span>}
          <button className="btn-ghost tiny crowding-modal-close"
                  onClick={onClose} aria-label="关闭">✕ 关闭</button>
        </div>

        <div className="crowding-modal-body">
          {loading && <div className="info-box">正在读取 {sectorName || sectorCode} …</div>}
          {/* 只有"画不出图"时才用错误框顶掉内容；有旧曲线时错误降级为脚注 */}
          {!loading && error && !data && (
            <div className="error-box">读取失败：{error}</div>
          )}
          {!loading && !error && data && (
            <SectorCrowdingDetailChart
              data={data} sectorCode={sectorCode} sectorName={sectorName}
              showRaw={showRaw} onToggleRaw={setShowRaw}
              expanded={expanded} onToggleExpand={() => setExpanded((v) => !v)} />
          )}
          {!loading && error && data && (
            <div className="muted-text">
              本次核对失败（{error}）—— 上图是上一次读到的数据。
            </div>
          )}
        </div>

        <div className="crowding-modal-foot muted-text">
          鼠标移入图表显示<b>十字轴</b>（X=日期、Y=最近曲线的读数）·
          滚轮缩放时间轴 · 拖拽平移 · 双击或「复位」回到全区间 ·
          <b> ＋/－</b> 键缩放、<b>0</b> 键复位 ·
          按 Esc、点右上角 ✕ 或点弹窗外空白处关闭
        </div>
      </div>
    </div>
  );
}
