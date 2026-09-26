import { useEffect, useState } from "react";
import {
  sectorCrowdingApi,
  type CrowdingDetail as DetailPayload,
} from "../sectorCrowdingApi";
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
 * 内容取数是**按板块懒加载**的（近 6 年 1400+ 根日线 ≈ 400ms），所以打开时
 * 先给出 loading 态，而不是把整张空图先画出来。
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
    setLoading(true);
    setError("");
    setData(null);
    sectorCrowdingApi.detail(sectorCode)
      .then((payload) => { if (alive) setData(payload); })
      .catch((exc) => {
        if (alive) setError(exc instanceof Error ? exc.message : String(exc));
      })
      .finally(() => { if (alive) setLoading(false); });
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
          <button className="btn-ghost tiny crowding-modal-close"
                  onClick={onClose} aria-label="关闭">✕ 关闭</button>
        </div>

        <div className="crowding-modal-body">
          {loading && <div className="info-box">正在读取 {sectorName || sectorCode} …</div>}
          {!loading && error && <div className="error-box">读取失败：{error}</div>}
          {!loading && !error && data && (
            <SectorCrowdingDetailChart
              data={data} sectorCode={sectorCode} sectorName={sectorName}
              showRaw={showRaw} onToggleRaw={setShowRaw}
              expanded={expanded} onToggleExpand={() => setExpanded((v) => !v)} />
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
