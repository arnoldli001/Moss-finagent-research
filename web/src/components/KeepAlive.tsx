/**
 * 轻量 **keep-alive**：把面板一直挂载、用 CSS 控制显隐。
 *
 * ## 为什么需要它（2026-09-26 用户报障，第二次）
 *
 * > "热点&研报小作文、事件告警 每次打开这个界面不能预加载到浏览器吗，
 * >   切界面还是会有 2 秒延迟…界面打开的时候应该立即能加载到。"
 *
 * `App.tsx` 原来用一条**三元链**渲染视图：
 *
 * ```tsx
 * ) : view === "intel-hot" ? <IntelPanel/> : view === "alerts" ? <AlertsPanel/> : ...
 * ```
 *
 * 三元链的语义是"**只渲染命中的那一个**"—— 切走时组件被**卸载**，
 * 切回来是**重新挂载**：组件内所有 `useState` 归零、DOM 重新构建。
 * 对这两个面板的后果：
 *
 * - **数据**要重取（`panelPrefetch` 的缓存解决了这一半）；
 * - **交互状态**丢失 —— 情报流展开的卡片、滚动位置、筛选档位都会重置。
 *
 * 所以缓存之外还需要这一层：把面板**保持挂载**，切回来是"显示"而不是"重建"。
 *
 * ## 用法上的硬要求：本组件自己必须**始终挂载**
 *
 * ```tsx
 * {/* ✅ 对：KeepAlive 一直在树上，只切换 active *\/}
 * <KeepAlive active={view === "alerts"}><AlertsPanel/></KeepAlive>
 *
 * {/* ❌ 错：放在三元链里 —— KeepAlive 自己也被卸载，状态照样丢 *\/}
 * {view === "alerts" ? <KeepAlive active><AlertsPanel/></KeepAlive> : ...}
 * ```
 *
 * 第二行的写法会让 `KeepAlive` 的 `useState` 一起归零，"保活"完全失效。
 *
 * ## 惰性挂载
 *
 * `everActive` 一旦为真就永久为真 —— 于是"从没打开过的页面"不付任何
 * 构建/取数成本，只有在用户**第一次切过去之后**才常驻。
 * 用 `useRef` 而不是 `useState`：它的变化发生在**渲染期**，
 * 而同一次渲染就能决定 `children` 挂不挂载 —— 用 state + effect 会多出
 * 一帧"active 为真但 children 还没挂载"的空档，用户切过去会看到闪白。
 *
 * ## 为什么用 `display: none` 而不是 `visibility` / 移出屏幕
 *
 * `display: none` 让子树**不参与布局**。`visibility: hidden` 仍然占位
 * （会把页面撑出空白），`position: absolute; left: -9999px` 仍然参与合成。
 *
 * ## 为什么不做成通用 keep-alive（只给这两个面板用）
 *
 * 其它面板（量化、竞价、主线…）各自的取数与副作用成本高得多，
 * 全部常驻会显著增加内存与后台请求。这两个面板**恰好**是用户反复来回切的，
 * 收益最大、风险最小 —— 所以只给它们用。
 */

import { ReactNode, useRef } from "react";

export default function KeepAlive({
  active, children, className,
}: {
  /** 当前是否可见。一旦为真过，`children` 就永久保持挂载。 */
  active: boolean;
  children: ReactNode;
  /** 可选的外层类名（比如宽屏布局需要的 `app-wide`）。 */
  className?: string;
}) {
  const everActive = useRef(active);
  if (active && !everActive.current) {
    // 渲染期更新 ref：本次渲染就能挂载 children，不产生闪白的那一帧。
    everActive.current = true;
  }

  if (!everActive.current) return null;   // 从没打开过：不挂载、不取数
  return (
    <div
      className={className}
      // `display: none` 而非条件渲染 —— 条件渲染会卸载，等于没做 keep-alive。
      style={active ? undefined : { display: "none" }}
      // 隐藏时对读屏器也隐藏，否则辅助技术仍会读到不可见的内容
      aria-hidden={active ? undefined : true}
    >
      {children}
    </div>
  );
}
