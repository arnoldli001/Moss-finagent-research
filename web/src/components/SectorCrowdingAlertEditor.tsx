import { useEffect, useRef, useState } from "react";

/**
 * 自定义告警阈值编辑面板（点「告警」列的格子弹出）。
 *
 * ## 两个方向，不是一个
 *
 * 同一个阈值配上方向才有意义，所以面板做成 **方向 + 数值** 一步设置：
 *
 * - `above`（高于就告警）：水位涨到阈值以上提醒 —— 看**拥挤风险**
 *   （"这只票/板块已经挤到历史高位了"）；
 * - `below`（低于就告警）：跌破阈值提醒 —— 看**冷清下来的机会**
 *   （"热度退到低位了，可以留意"）。
 *
 * 两个方向共用一个数值输入，而不是各存一个阈值：用户的心智是"我给这个板块
 * 设一条线"，不是"维护两条线"。
 *
 * ## 取值范围与精度
 *
 * 水位本身就是 `[0, 1]` 的比例量（1.000 = 100% = 该板块近 6 年最拥挤时刻），
 * 所以数值限定在这个区间、保留 3 位小数（0.001 一档 = 0.1%，与水位本身的
 * 精度同量级）。输入框与滑杆双向绑定：滑杆快速定个大概，输入框敲精确值。
 *
 * ## 为什么不照抄同花顺
 *
 * 同花顺的提醒设置是"价格上限/下限 + 涨跌幅"那套，锚点是**价格**；这里锚点是
 * **水位**（相对自身近 6 年最高值的比例），所以面板里直接把当前水位和阈值并排
 * 显示、并实时给出"当前 0.62 → 阈值 0.80，还差 0.18"的判定，让人知道这条线
 * 是松还是紧。
 */
export default function SectorCrowdingAlertEditor({
  sectorCode, sectorName, currentWater, mode, threshold, busy, onSave, onClear,
  onClose,
}: {
  sectorCode: string;
  sectorName: string;
  /** 当前水位，用于实时显示"离阈值还有多远" */
  currentWater: number | null;
  mode: "above" | "below";
  threshold: number | null;
  busy?: boolean;
  onSave: (mode: "above" | "below", threshold: number) => void;
  onClear: () => void;
  onClose: () => void;
}) {
  const [direction, setDirection] = useState<"above" | "below">(mode);
  const [text, setText] = useState(
    threshold === null ? "0.800" : threshold.toFixed(3));
  const boxRef = useRef<HTMLDivElement | null>(null);

  // 点面板外面关掉（编辑中的数值不落库，直接丢弃才符合"弹窗式设置"的预期）
  useEffect(() => {
    const onDown = (event: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(event.target as Node)) {
        onClose();
      }
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    // 下一帧再绑，免得打开面板的那次点击立刻把它关掉
    const timer = window.setTimeout(() => {
      document.addEventListener("mousedown", onDown);
      window.addEventListener("keydown", onKey);
    }, 0);
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onKey);
    };
  }, [onClose]);

  const parsed = Number(text);
  const valid = text.trim() !== "" && Number.isFinite(parsed)
    && parsed >= 0 && parsed <= 1;
  const value = valid ? parsed : null;

  const pct = (v: number | null | undefined) =>
    v === null || v === undefined ? "—" : `${(v * 100).toFixed(1)}%`;

  /** 实时判定：这条线现在是"已触发"还是"还差多少"。 */
  const verdict = (() => {
    if (value === null) return { text: "数值无效", tone: "flat" as const };
    if (currentWater === null) {
      return { text: "该板块暂无水位（数据不足）", tone: "flat" as const };
    }
    const gap = currentWater - value;
    const fired = direction === "above" ? gap > 0 : gap < 0;
    if (fired) return { text: "按当前水位：已触发", tone: "up" as const };
    return {
      text: `按当前水位：未触发，还差 ${(Math.abs(gap) * 100).toFixed(1)} 个点`,
      tone: "flat" as const,
    };
  })();

  return (
    <div className="crowding-alert-editor" ref={boxRef} role="dialog"
         aria-label={`${sectorName || sectorCode} 告警设置`}>
      <div className="crowding-alert-editor-head">
        <b>{sectorName || sectorCode}</b>
        <span className="mono muted-text">{sectorCode}</span>
        <span style={{ flex: 1 }} />
        <button className="btn-ghost tiny" onClick={onClose}
                aria-label="关闭">✕</button>
      </div>

      <div className="crowding-alert-editor-row">
        <span className="muted-text">当前水位</span>
        <b className="mono">{pct(currentWater)}</b>
      </div>

      <div className="crowding-alert-editor-row">
        <span className="muted-text">触发方向</span>
        <div className="crowding-alert-modes">
          <button className={"btn-ghost tiny" + (direction === "above" ? " active" : "")}
                  onClick={() => setDirection("above")}
                  title="水位升到阈值以上时告警（看拥挤风险）">
            ↑ 高于设定值就告警
          </button>
          <button className={"btn-ghost tiny" + (direction === "below" ? " active" : "")}
                  onClick={() => setDirection("below")}
                  title="水位跌破阈值时告警（看热度退潮的机会）">
            ↓ 低于设定值就告警
          </button>
        </div>
      </div>

      <div className="crowding-alert-editor-row">
        <span className="muted-text">告警水位</span>
        <input className="qsel-input mono crowding-alert-value"
               type="number" min={0} max={1} step={0.001}
               value={text}
               onChange={(event) => setText(event.target.value)}
               onKeyDown={(event) => {
                 if (event.key === "Enter" && valid) onSave(direction, parsed);
               }} />
        <span className="muted-text">（0 ~ 1，3 位小数）</span>
      </div>

      <input className="crowding-alert-slider" type="range"
             min={0} max={1} step={0.001}
             value={value ?? 0.5}
             onChange={(event) => setText(Number(event.target.value).toFixed(3))} />

      <div className="crowding-alert-editor-row">
        <span className="muted-text">快捷</span>
        {[0.2, 0.5, 0.8, 0.9].map((preset) => (
          <button key={preset} className="btn-ghost tiny"
                  onClick={() => setText(preset.toFixed(3))}>
            {(preset * 100).toFixed(0)}%
          </button>
        ))}
      </div>

      <div className={`crowding-alert-verdict ${verdict.tone}`}>
        {direction === "above"
          ? `水位 > ${value === null ? "—" : value.toFixed(3)} 时告警`
          : `水位 < ${value === null ? "—" : value.toFixed(3)} 时告警`}
        {" · "}{verdict.text}
      </div>

      <div className="crowding-alert-editor-foot">
        <button className="btn-ghost tiny"
                disabled={busy || threshold === null}
                title="清除这个板块的告警设置"
                onClick={onClear}>清除告警</button>
        <span style={{ flex: 1 }} />
        <button className="btn-ghost tiny" onClick={onClose}>取消</button>
        <button className="btn-ghost tiny primary"
                disabled={busy || !valid}
                onClick={() => { if (valid) onSave(direction, parsed); }}>
          {busy ? "保存中…" : "保存"}
        </button>
      </div>
    </div>
  );
}
