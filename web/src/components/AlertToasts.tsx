import { useEffect, useRef } from "react";
import { Alert } from "../api";

type Props = {
  alerts: Alert[];
  onDismiss: (alertId: string) => void;
  onOpen: (alert: Alert) => void;
};

/** 高告警蜂鸣（WebAudio合成，不依赖音频文件）。 */
function beep() {
  try {
    const Ctx = window.AudioContext
      ?? (window as unknown as { webkitAudioContext: typeof AudioContext })
        .webkitAudioContext;
    const ctx = new Ctx();
    [0, 0.25].forEach((delay) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = "sine";
      osc.frequency.value = 880;
      gain.gain.value = 0.08;
      osc.connect(gain).connect(ctx.destination);
      osc.start(ctx.currentTime + delay);
      osc.stop(ctx.currentTime + delay + 0.18);
    });
    window.setTimeout(() => void ctx.close(), 1200);
  } catch { /* 浏览器禁止自动播放时静默 */ }
}

/** 告警Toast栈：风险红/机会金，10秒后由父组件移除，点击进详情。 */
export default function AlertToasts({ alerts, onDismiss, onOpen }: Props) {
  const beeped = useRef<Set<string>>(new Set());

  useEffect(() => {
    const high = alerts.filter((a) => a.alert_level === "high");
    const fresh = high.find((a) => !beeped.current.has(a.alert_id));
    if (fresh) {
      beeped.current.add(fresh.alert_id);
      beep();
    }
  }, [alerts]);

  return (
    <div className="toast-stack">
      {alerts.map((a) => (
        <div
          key={a.alert_id}
          className={`toast toast-${a.alert_type}`}
          onClick={() => onOpen(a)}
          role="alert"
        >
          <div className="toast-title">
            <span className={`badge alert-tag-${a.alert_type}`}>
              {a.alert_type === "risk" ? "风险" : "机会"}·{a.alert_level}
            </span>
            <span className="toast-text">{a.title}</span>
          </div>
          <div className="toast-meta">
            风险 {Math.round(a.risk_score)} / 机会 {Math.round(a.opportunity_score)}
            {" · "}置信 {(a.confidence * 100).toFixed(0)}%
          </div>
          <button
            className="toast-close"
            onClick={(e) => { e.stopPropagation(); onDismiss(a.alert_id); }}
            aria-label="关闭"
          >
            ×
          </button>
        </div>
      ))}
    </div>
  );
}
