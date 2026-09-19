import { useState } from "react";
import QuantFactorPanel from "./QuantFactorPanel";
import { QuantSingleStockPanel } from "./QuantSingleStockPanel";

export default function BacktestPanel() {
  const [mode, setMode] = useState<"factor" | "single">("factor");

  return (
    <div>
      <nav className="mode-switch" style={{ marginBottom: 12 }}>
        <button className={mode === "factor" ? "mode-btn active" : "mode-btn"}
                onClick={() => setMode("factor")}
                title="多因子：35 个核心因子（价值/成长/质量/动量/波动/流动性/规模）">
          多因子（35 个因子）
        </button>
        <button className={mode === "single" ? "mode-btn active" : "mode-btn"}
                onClick={() => setMode("single")}
                title="单股票策略：指定一只票，用因子条件描述买卖点做时序回测，可一键保存策略">
          单股票策略回测
        </button>
      </nav>

      {mode === "single" ? (
        <QuantSingleStockPanel />
      ) : (
        <QuantFactorPanel />
      )}
    </div>
  );
}
