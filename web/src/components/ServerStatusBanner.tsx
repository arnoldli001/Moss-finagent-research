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
 * ## ★ 抖动容忍窗口（2026-09-30 用户口径，`CHG-0133` → 阈值 `CHG-0134`）
 *
 * 用户口径两句话，**阈值以第二句为准**：
 *   ①「如果只是几秒钟抖动，是否可以不显示这个提示？」
 *   ②「不要 5 秒就提示，要**重试周期的 2~3 倍**再提示一次」
 *
 * 原来只数**次数**（连续 2 次、失败后每 5 秒一探）⇒ **5~9 秒的瞬断就会弹红条**。
 * 而公网入口那条链有 5 跳（浏览器 → VPS nginx → frps → SSH 隧道 → frpc → 8110），
 * 实测这种几秒级抖动**会反复出现**（`data/run/tunnel-watchdog.log` 里
 * 09-29 22:11 / 22:41 / 09-30 09:37 都记过「主用不通但本机后端健康」）。
 * 一次十几秒内自愈的抖动，对用户没有任何可操作性 —— 弹了只会让他以为系统坏了。
 *
 * ## ★★ 阈值是**推导**出来的，不是第二个魔法数
 *
 * `GRACE_MS = RETRY_MS × GRACE_MULTIPLIER`（5 秒 × 3 = **15 秒**）。
 * 上一版把它写成独立的 `20000`（= 4 倍），与重试周期**没有关系** ——
 * 那样一来"2~3 倍"这条口径就只存在于注释里，改任何一个常数都会让它失真。
 * 现在只有一个旋钮：要"2 倍"就把 `GRACE_MULTIPLIER` 改成 2（= 10 秒）。
 *
 * 真实 48 秒中断（2026-09-30 10:50:05–10:50:53）**照样会显示** ——
 * 被过滤掉的只有"十几秒内自己好了"的那一类。
 *
 * ⚠️ 容忍期内**仍然按 `RETRY_MS` 探**：不能为了安静而把恢复检测也拖慢。
 * ⚠️ 浏览器 `offline` 事件**也走同一个窗口**（原来它立刻弹条）：
 * 网卡瞬断两三秒就闪一次"后端不可达"，正是这条口径要挡的东西。
 */
/** 失败后的重试间隔（毫秒）。 */
const RETRY_MS = 5000;
/** 抖动容忍窗口 = 重试周期的**倍数**（用户口径：2~3 倍）。 */
const GRACE_MULTIPLIER = 3;
/** 抖动容忍窗口：连续不可达不超过这个时长**不弹红条**。 */
const GRACE_MS = RETRY_MS * GRACE_MULTIPLIER;

export default function ServerStatusBanner() {
  const [down, setDown] = useState(false);
  const [checking, setChecking] = useState(false);
  //: 本轮"持续不可达"是从什么时候开始的（恢复即清空）。**唯一**的判据来源。
  const downSince = useRef<number | null>(null);
  const timer = useRef<number | undefined>(undefined);
  const alive = useRef(true);

  const tick = useCallback(async () => {
    window.clearTimeout(timer.current);
    const ok = await pingServer(4000);
    if (!alive.current) return;
    if (ok) {
      downSince.current = null;
      setDown(false);
    } else {
      if (downSince.current === null) downSince.current = Date.now();
      // 只有一个判据：**持续不可达够久**（阈值 = 重试周期 × 倍数）。
      // 不再单独数次数 —— 时长窗口本身就蕴含"失败过若干轮"，
      // 两个判据并存等于同一个决定有两个旋钮，迟早互相打架。
      setDown(Date.now() - downSince.current >= GRACE_MS);
    }
    // 在线时 15 秒探一次（几乎无成本）；怀疑宕机时按 RETRY_MS 探，尽快恢复显示
    timer.current = window.setTimeout(() => { void tick(); }, ok ? 15000 : RETRY_MS);
  }, []);

  useEffect(() => {
    alive.current = true;
    void tick();
    // 系统层面的断网/恢复事件比轮询更及时
    const onOnline = () => { downSince.current = null; void tick(); };
    // ⚠️ offline **不再立刻弹条**：网卡瞬断两三秒就闪"后端不可达"，
    //    正是抖动容忍窗口要挡的。这里只**开始计时**，是否显示仍由 tick 按
    //    同一个 GRACE_MS 判定（真断网超过窗口照样会显示）。
    const onOffline = () => {
      if (downSince.current === null) downSince.current = Date.now();
      void tick();
    };
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
    downSince.current = null;   // 手动重试 = 重新开始计时（用户主动问了，就该立刻给答案）
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
