---
name: cash-flow-anomaly-detection
description: >
  当需要分析公司财务健康度时使用。应在个股研究时触发。
  检测现金流异常信号。
  触发短语：财务排雷、现金流、财务健康。
tags: [financial, anomaly, cash-flow]
references:
  - references/red-flags.md
---

# 现金流异常检测

## 触发条件
当个股研究Agent需要评估财务健康度时触发。

## 执行步骤

### Phase 1：经营性现金流检测
- 检查经营性现金流是否持续为负
- 检查现金流净额是否远低于净利润
- 检查应付账款/应付票据是否异常增加

### Phase 2：投资现金流检测
- 检查购买固定资产支出是否持续高于经营现金流
- 检查是否大量出售资产

### Phase 3：筹资现金流检测
- 检查归还借款是否远大于借款
- 检查是否支付过高利息

## 输出格式
```json
{
  "anomalies": [
    {"type": "cash_flow_negative", "severity": "high", "description": "经营性现金流连续3年为负"}
  ],
  "risk_level": "high",
  "recommendation": "建议回避"
}
```
