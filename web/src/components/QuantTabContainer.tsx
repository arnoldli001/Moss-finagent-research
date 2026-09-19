import { useState } from "react";
import IntradayTPanel from "./IntradayTPanel";
import PrivateFeatureNotice from "./PrivateFeatureNotice";

/**
 * 量化交易容器（页签宿主）。
 *
 * ## 为什么需要它
 *
 * 「竞价选股」按需求要挂在「量化选股」右侧。但 `IntradayTPanel` 内部已经有
 * 9 处 `mode === "intraday" | "daily" | "select"` 分支，再加一个 mode 值要动
 * 全部 9 处，回归风险大、也没必要 —— 竞价选股是**完全独立**的一个组合级页面，
 * 它不读当前个股、也不与做T/选股共享任何状态。
 *
 * 所以改成"父级换页"：本容器只持有一个 `view`，
 * - `view === "t"`：渲染原封不动的 `IntradayTPanel`；
 * - `view === "auction"`：渲染 `AuctionSelectPanel`。
 *
 * 两个分支**只挂载一个**，所以切到竞价选股时做T面板整块被卸载 ——
 * 它的自选轮询、分时请求都不会在后台继续跑（与资金流监控里
 * `FundFlowBoard` 的拆分同一理由）。
 */

type View = "t" | "auction";

export default function QuantTabContainer() {
  const [view, setView] = useState<View>("t");

  return (
    <div className="quant-tab-root">
      {view === "t" ? (
        <IntradayTPanel onOpenAuction={() => setView("auction")} />
      ) : (
        <PrivateFeatureNotice feature="竞价选股" />
      )}
    </div>
  );
}
