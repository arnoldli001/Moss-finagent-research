import { useState } from "react";
import EtfFlowPanel from "./EtfFlowPanel";
import FundFlowBoard from "./FundFlowBoard";
import KeepAlive from "./KeepAlive";
import SectorCrowdingTab from "./SectorCrowdingTab";
import SectorRotationTab from "./SectorRotationTab";

/**
 * 资金流监控面板（页签容器）。
 *
 * 五个页签：板块资金流 / 个股资金流 / **ETF份额监控** / **行业轮动日报** / **板块拥挤度**。
 *
 * 「ETF份额监控」插在「个股资金流」与「板块拥挤度」之间（用户口径）：它读的是
 * 宽基 ETF 的份额申赎，与资金流是同一类"钱往哪走"的问题，放在两个资金流视图
 * 后面比挂在最后更顺；而「板块拥挤度」是独立模块（板块成交额占比），排最后。
 *
 * 「主线挖掘」曾经作为左栏嵌在这里，现已提为**与资金流监控同级的顶级页签**
 * （用户口径 2026-09-20，位置在「量化交易」右侧）。提级的原因：它自带五个
 * 子视图（评分热力 / 告警流水 / 重点跟踪 / 期货先行 / 回测报告），其中热力图
 * 与告警表格都需要横向空间 —— 压在 380px 侧栏里列数不够、雷达图要靠弹层，
 * 反而比"多切一次页签"更麻烦。
 *
 * ## 为什么把主体拆成 `FundFlowBoard` 而不是在原组件里加个 if
 *
 * 原来的 `FundFlowPanel` 自己管页签、自己抓资金流数据、并且**每 60 秒轮询一次**。
 * 如果在同一个组件里加第三个页签，切到"板块拥挤度"时那套轮询照样在跑
 * （白耗数据源与 CPU，而拥挤度页面根本不看资金流）。
 *
 * 拆开之后：页签状态留在容器，**只有当前页签的组件被挂载** ——
 * 资金流那套 `useEffect` + 定时器只在它的页签激活时存在；ETF 份额监控的
 * 快照轮询同理（它自己带 300 秒轮询，见 `EtfFlowPanel`）。
 * 板块资金流/个股资金流的内部逻辑一行未改（整段搬进 `FundFlowBoard`）。
 *
 * ★ 2026-09-27 例外：「板块拥挤度」「行业轮动日报」两个子页签改用
 * `<KeepAlive>` 保活（用户报障"打开加载很慢"，互切卸载重挂是根因之一，
 * 见渲染处的说明）；其余三个子页签维持"切走即卸载"的语义不变。
 */

type Tab = "sector" | "stock" | "etf" | "rotation" | "crowding";

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
          <button className={tab === "etf" ? "mode-btn active" : "mode-btn"}
                  onClick={() => setTab("etf")}
                  title="宽基 ETF 份额申赎监控：份额变化 + 指数分位 + 市场环境门控">
            ETF份额监控
          </button>
          <button className={tab === "rotation" ? "mode-btn active" : "mode-btn"}
                  onClick={() => setTab("rotation")}
                  title="行业轮动日报：行业热力图 + 主力流向（当日/近5日）+ 风格轮动 + 规则研判，每交易日收盘后自动生成">
            行业轮动日报
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
        {tab === "etf" && (
          <span className="muted-text">
            份额变化 = 真金白银的申购/赎回（与价格涨跌是两回事）；
            机会信号只在放行环境下才构成告警，被门控的会明确标注
          </span>
        )}
      </div>

      {/* ★ 2026-09-27：拥挤度/行业轮动两个子页签接 KeepAlive（用户报障
          "这两个界面打开加载很慢"）。原先五个子页签是三元切换 —— 互切即
          卸载重挂：拥挤度要重拉 554 板块清单 + 近6年最高，行业轮动的
          iframe（内嵌 1MB ECharts）整个销毁重建。保活后互切是 display
          切换，数据与滚动/筛选状态都保留。
          只保活这两个（报障点名、且最重）：板块/个股资金流与 ETF 面板
          维持原卸载语义，各自的后台轮询不会在隐藏时继续跑。
          ⚠️ KeepAlive 自身不能放进三元链（见 KeepAlive.tsx 的用法约束）。 */}
      {tab === "sector" || tab === "stock" ? (
        <FundFlowBoard tab={tab} />
      ) : null}
      {tab === "etf" ? (
        <EtfFlowPanel />
      ) : null}
      <KeepAlive active={tab === "rotation"}>
        <SectorRotationTab />
      </KeepAlive>
      <KeepAlive active={tab === "crowding"}>
        <SectorCrowdingTab />
      </KeepAlive>
    </div>
  );
}
