import { Component, type ErrorInfo, type ReactNode } from "react";

/**
 * 视图级错误边界：**一个面板崩了，不该把整页拖成白屏**。
 *
 * ## 为什么必须有（2026-09-21 实测）
 *
 * 竞价选股点「详情」时，后端把 `rush_labels` 返回成了字符串 `"[]"`（而不是
 * 数组），前端 `"[]".join("、")` 抛 `TypeError`。React 18 在**没有错误边界**时
 * 的行为是：**卸载整棵树** —— 用户看到的是"整个页面白掉"，
 * 既没有报错信息、也看不出是哪个面板、连切页签都做不到（页签也随之没了）。
 *
 * 有了这个边界：崩的只是那一块，顶部页签与导航还在，用户能切走，
 * 并且能看到"是哪个视图崩的 + 具体错误"，而不是一片空白。
 *
 * ## 为什么用 class 组件
 *
 * `componentDidCatch` / `getDerivedStateFromError` 目前**只有 class 组件**能实现
 * （没有对应的 Hook）。这是 React 官方推荐的唯一写法。
 *
 * ## 恢复方式
 *
 * - 切页签会自动重置（`resetKey` 变了就清错误）；
 * - 也给了「重试」按钮（用户改完配置/后端修好后不必刷新整页）。
 */
export default class ErrorBoundary extends Component<
  { children: ReactNode; resetKey?: string; label?: string },
  { error: Error | null }
> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // 控制台留全量堆栈（组件栈只有这里能拿到），方便定位到具体组件
    console.error("[ErrorBoundary] 视图渲染失败:", error, info.componentStack);
  }

  componentDidUpdate(prev: { resetKey?: string }) {
    // 切到别的页签时自动清掉上一次的错误，否则会一直卡在错误态
    if (prev.resetKey !== this.props.resetKey && this.state.error !== null) {
      this.setState({ error: null });
    }
  }

  render() {
    const { error } = this.state;
    if (error === null) return this.props.children;
    return (
      <section className="panel error-boundary">
        <h2>「{this.props.label ?? "当前页面"}」渲染出错了</h2>
        <div className="error-box">
          {error.name}: {error.message}
        </div>
        <p className="muted-text">
          其他页签不受影响，可以切换过去继续用。下面是排查提示：
        </p>
        <ul className="muted-text">
          <li>
            若是某个字段显示异常，多半是后端返回的**类型**与前端预期不符
            （例如该是数组却给了字符串）—— 打开浏览器控制台看完整堆栈。
          </li>
          <li>修好后点下面「重试」，不必刷新整个页面。</li>
        </ul>
        <button className="btn-ghost" onClick={() => this.setState({ error: null })}>
          重试
        </button>
      </section>
    );
  }
}
