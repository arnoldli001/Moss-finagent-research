import { useCallback, useEffect, useRef, useState } from "react";

import { pingServer } from "../api";
import { isLocalDevHost } from "../errors";

/** 后端可达性横幅：**只在真的连不上时出现**。
 *
 * ## 为什么值得单独做一个组件
 *
 * 2026-09-23 的实测故障：管理员点「直接开号 → 创建」，浏览器只报一句
 * `Failed to fetch`。这句话不区分"后端没启动""端口不对""连接被防火墙拦"
 * "后端刚重启、浏览器复用了旧连接"——四种情况的处置完全不同，
 * 而页面上没有任何地方能看出当前是哪一种。于是只能靠反复点击试。
 *
 * 根因（uvicorn 默认 5 秒 keep-alive 与浏览器连接复用竞态）已经在
 * `manage.py` 里修掉了。但**可见性**这一半必须在界面上：
 * 下次真出问题时，用户应该能一眼看到"服务此刻不可达"，而不是从
 * 某个按钮的报错里去猜。
 *
 * ## 用哪个探针（别改错）
 *
 * 用 `/api/v1/health/live`（0 I/O），**不是** `/api/v1/health`
 * ——后者要连 Ollama、校验审计链，正常也要 0.3~2.5 秒，
 * 拿它当探针会在服务只是"忙"的时候误报宕机。
 *
 * ## 为什么连续失败两次才报警
 *
 * 单次探测失败可能只是一次瞬时抖动（比如后端正好在做热重载）。
 * 立刻弹红条会造成"明明能用却报警"的噪音，比不报还伤信任。
 * 两次失败之间隔 5 秒，真正宕机时 5 秒内就会亮，代价可接受。
 */
export default function ServerStatusBanner() {
  const [down, setDown] = useState(false);
  const [checking, setChecking] = useState(false);
  const streak = useRef(0);
  const timer = useRef<number | undefined>(undefined);
  const alive = useRef(true);

  const tick = useCallback(async () => {
    window.clearTimeout(timer.current);
    const ok = await pingServer(4000);
    if (!alive.current) return;
    streak.current = ok ? 0 : streak.current + 1;
    setDown(!ok && streak.current >= 2);
    // 在线时 15 秒探一次（几乎无成本）；怀疑宕机时 5 秒一次，尽快恢复显示
    timer.current = window.setTimeout(() => { void tick(); }, ok ? 15000 : 5000);
  }, []);

  useEffect(() => {
    alive.current = true;
    void tick();
    // 系统层面的断网/恢复事件比轮询更及时
    const onOnline = () => { streak.current = 0; void tick(); };
    const onOffline = () => { streak.current = 99; setDown(true); };
    window.addEventListener("online", onOnline);
    window.addEventListener("offline", onOffline);
    return () => {
      alive.current = false;
      window.clearTimeout(timer.current);
      window.removeEventListener("online", onOnline);
      window.removeEventListener("offline", onOffline);
    };
  }, [tick]);

  const onRetry = async () => {
    setChecking(true);
    streak.current = 0;
    await tick();
    setChecking(false);
  };

  if (!down) return null;

  return (
    <div className="conn-banner" role="alert">
      <span className="conn-dot" aria-hidden="true" />
      <span className="conn-text">
        <b>后端服务当前不可达。</b>
        刚才那些「无法连接」的提示就是这么来的 —— 不是你的操作有问题。
        {isLocalDevHost() ? (
          <>
            请在项目目录执行&nbsp;
            <code>python manage.py start --daemon --replace</code>
            &nbsp;，确认 <code>python manage.py status</code> 显示「后端 API
            本项目运行中」后点右侧重试。
          </>
        ) : (
          "服务可能正在维护重启，请稍后点右侧重试；若持续不可达请联系管理员。"
        )}
      </span>
      <button className="auth-inline-btn" onClick={onRetry} disabled={checking}>
        {checking ? "检测中…" : "重试连接"}
      </button>
    </div>
  );
}
