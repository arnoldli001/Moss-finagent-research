---
name: multi-method-valuation
description: >
  当需要对公司进行估值时使用。应在财务数据获取后触发。
  采用DCF、PE、PB、PS、PEG等多种方法。
  触发短语：估值、DCF、PE、PB、目标价。
tags: [valuation, dcf, relative, target-price]
references:
  - references/valuation-methods.md
---

# 多方法估值计算

## 触发条件
当财务数据获取完成后触发。

## 执行步骤

### Phase 1：DCF模型
- 预测未来3-5年自由现金流
- 确定WACC和终值
- 折现计算内在价值

### Phase 2：相对估值
- PE/PB/PS/PEG
- 基于可比公司和历史分位

### Phase 3：情景分析
- 保守/基准/乐观三种情景
- 输出合理估值区间和目标价

## 输出格式
```json
{
  "dcf_value": 85.5,
  "pe_valuation": {"low": 72, "mid": 88, "high": 105},
  "target_price": 88,
  "current_price": 75,
  "upside": "17.3%",
  "scenarios": {
    "conservative": 72,
    "base": 88,
    "optimistic": 105
  }
}
```
