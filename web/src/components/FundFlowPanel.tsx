import { useState } from "react";
import FundFlowBoard from "./FundFlowBoard";
import SectorCrowdingTab from "./SectorCrowdingTab";

/**
 * 资金流监控面板（页签容器）。
 *
 * 三个页签：板块资金流 / 个股资金流 / **板块拥挤度**。
 *
 * ## 为什么把主体拆成 `FundFlowBoard` 而不是在原组件里加个 if
 *
 * 原来的 `FundFlowPanel` 自己管页签、自己抓资金流数据、并且**每 60 秒轮询一次**。
 * 如果在同一个组件里加第三个页签，切到"板块拥挤度"时那套轮询照样在跑
 * （白耗数据源与 CPU，而拥挤度页面根本不看资金流）。
 *
 * 拆开之后：页签状态留在容器，**只有当前页签的组件被挂载** ——
 * 资金流那套 `useEffect` + 定时器只在它的页签激活时存在。
 * 板块资金流/个股资金流的内部逻辑一行未改（整段搬进 `FundFlowBoard`）。
 */

type Tab = "sector" | "stock" | "crowding";

export default function FundFlowPanel() {
  const [tab, setTab] = useState<Tab>("sector");

  return (
    <div className="fundflow-root">
      <div className="fundflow-head">
        <div className="mode-switch">
          <button className={tab === "sector" ? "mode-btn active" : "mode-btn"}
                  onClick={() => setTab("sector")}>
            板块资金流
          </button>
          <button className={tab === "stock" ? "mode-btn active" : "mode-btn"}
                  onClick={() => setTab("stock")}>
            个股资金流
          </button>
          <button className={tab === "crowding" ? "mode-btn active" : "mode-btn"}
                  onClick={() => setTab("crowding")}
                  title="板块概念拥挤度：板块成交额占全市场比例，相对自身近 6 年最高值的水位">
            板块拥挤度
          </button>
        </div>
        {tab === "crowding" && (
          <span className="muted-text">
            拥挤度水位 = 当前平滑拥挤度 / 近 6 年平滑拥挤度最高值；
            ≥ 80% 触发告警，≥ 90% 红色高亮
          </span>
        )}
      </div>

      {tab === "crowding" ? (
        <SectorCrowdingTab />
      ) : (
        <FundFlowBoard tab={tab} />
      )}
    </div>
  );
}
