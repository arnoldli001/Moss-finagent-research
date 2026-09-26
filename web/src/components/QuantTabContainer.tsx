import { useState } from "react";
import IntradayTPanel from "./IntradayTPanel";
import PrivateFeatureNotice from "./PrivateFeatureNotice";
import { AuctionSelectPanel } from "../privatePanels";

/**
 * 量化交易容器（**顶部模块页签**宿主）。
 *
 * ## 三个同级模块并列在顶部
 *
 * | 模块 | 层级 | 内容 |
 * |---|---|---|
 * | 量化选股 | 组合级（不针对某一只票） | 定时跑批出池 → 结果手动加自选 |
 * | 竞价选股 | 组合级 | 开市日 9:25:00 出池，9:27 前完成 |
 * | 做T辅助 | **个股级**（围绕当前这一只票） | 内部再分「日内分时 / 日K」两种周期口径 |
 *
 * 2026-09-23 之前不是这么摆的：整页是做T辅助，而「量化选股」被塞进做T面板的
 * 「周期」条里当第三种"周期"、「竞价选股」靠在它右边再加一个按钮让父级换页。
 * 结果是"周期口径"和"独立模块"长得一模一样 —— 用户反馈"层级混乱"。
 * 现在页签**统一上移到本容器**：换页签 = 换模块；做T面板内部的「周期」条
 * 只剩「日内分时 / 日K」两项（它们确实只是做T的两种口径，见 IntradayTPanel）。
 *
 * ## 为什么页签由容器渲染，而不是塞进 IntradayTPanel
 *
 * 页签在三个模块下**都要可见**（尤其是切到竞价选股时不能消失），
 * 而 IntradayTPanel 在切到竞价选股时是**整块被卸载**的（理由见下），
 * 所以页签必须活在比它更高一层的容器里。
 *
 * ## 挂载策略（哪个分支挂载，决定了后台还在不在跑）
 *
 * - `t` / `select`：同一个 IntradayTPanel 实例，只切 `view` prop ——
 *   做T的标的、快照、轮询状态因此在两个页签之间**保留**（量化选股页签本来
 *   就共用左侧自选栏，切回去不该重新取数）；
 * - `auction`：渲染 AuctionSelectPanel、IntradayTPanel 整块卸载 ——
 *   它的自选轮询与分时请求都不会在后台继续跑（与资金流监控里
 *   `FundFlowBoard` 的拆分同一理由）。
 *
 * ## 竞价选股/量化选股为什么走 `../privatePanels` 而不是直接 import
 *
 * 这两个面板是私有商业版资产，不进公开仓库。用 `import.meta.glob` 解析后，
 * 本文件在**两种 checkout 下都能构建**：本地拿到真实面板，
 * 公开仓库拿到 `null` 而退化成占位提示。详见 `src/privatePanels.ts`。
 */

/** 顶部页签的键：两个组合级模块 + 做T辅助（个股级）。 */
type View = "select" | "auction" | "t";

/**
 * 页签顺序与文案（用户口径 2026-09-23）：量化选股 · 竞价选股 · 做T辅助。
 *
 * 集中成一张表，避免"文案/提示散在 JSX 里各写一遍"——
 * 以后要调顺序或加模块，只动这里。
 */
const MODULES: Array<{ key: View; label: string; title: string }> = [
  {
    key: "select",
    label: "量化选股",
    title: "量化选股：组合级模块，开市日 9:25–9:45 与 14:45 用 3 档模型自动跑批，结果需手动加自选",
  },
  {
    key: "auction",
    label: "竞价选股",
    title: "竞价选股：组合级模块，开市日 9:25:00 自动跑，只选昨日涨停且 30~110 亿的票，9:27 前出池",
  },
  {
    key: "t",
    label: "行情",
    title: "行情：个股级模块，围绕当前这一只票（内部再分 分时 / 日K 两种周期口径）",
  },
];

export default function QuantTabContainer({ onEditProfile }: {
  /** 打开「个股口径」抽屉（做T面板里的「⚙ 口径」按钮触发）。 */
  onEditProfile?: (code: string) => void;
} = {}) {
  // 默认落地 = 做T辅助：它原来就是本页的默认视图，也是盘中盯得最久的模块。
  // ⚠️ 别把它改成组合级模块：做T依赖"当前这只票"，落地即取数更符合使用顺序。
  const [view, setView] = useState<View>("t");

  return (
    <div className="quant-tab-root">
      {/* 顶部一级页签：三个同级模块并列（样式见 styles.css 的 .quant-modules）。
          渲染在容器里而不是各面板内部 —— 切到竞价选股时页签也要在。 */}
      <nav className="quant-modules" aria-label="量化交易模块">
        {MODULES.map((item) => (
          <button key={item.key}
                  className={`quant-module${view === item.key ? " active" : ""}`}
                  aria-current={view === item.key ? "page" : undefined}
                  title={item.title}
                  onClick={() => setView(item.key)}>
            {item.label}
          </button>
        ))}
        {/* 风险提示（用户口径 2026-09-23）：放在三个模块页签**右侧**，
            原文照抄不加改写 —— 这类免责声明被"润色"过就不再是用户要表达的意思。
            `margin-left:auto` 把它推到行尾；窄屏页签占满一行时它换到第二行右端
            （见 .quant-tabs-disclaimer）。
            ⚠️ 类名**不能**叫 `.quant-disclaimer`：那是多因子面板底部免责声明在用的类
            （`QuantFactorPanel.tsx`，`margin-top:14px`），复用会让这条被顶下去 14px、
            也会把 `margin-left:auto` 反向泄漏到那个段落上。 */}
        <span className="quant-tabs-disclaimer">
          本工具仅供参考学习研究，投资有风险，非指导建议，盈亏自负
        </span>
      </nav>

      {view === "auction" ? (
        AuctionSelectPanel !== null ? (
          <AuctionSelectPanel />
        ) : (
          <PrivateFeatureNotice feature="竞价选股" />
        )
      ) : (
        // 量化选股走的是**同一个做T面板**的 select 视图：它共用左侧自选栏
        // （选出来的票正是往自选池里加），拆成两个组件就得把抽屉也抽出来。
        <IntradayTPanel view={view === "select" ? "select" : "t"}
          onEditProfile={onEditProfile} />
      )}
    </div>
  );
}
