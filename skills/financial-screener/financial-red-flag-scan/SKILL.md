---
name: financial-red-flag-scan
description: >
  当现金流异常检测完成后使用。用于检测大存大贷和商誉风险。
  触发短语：大存大贷、商誉、关联交易、毛利率异常。
tags: [financial, red-flag, goodwill]
---

# 大存大贷与商誉检测

## 触发条件
当现金流异常检测完成后触发。

## 执行步骤

### Phase 1：大存大贷检测
- 检测存款和贷款是否同时很高

### Phase 2：商誉检测
- 计算商誉/净资产比例（>35%预警）

### Phase 3：关联交易检测
- 检测关联交易占比

### Phase 4：毛利率异常检测
- 检测毛利率是否远高于同行

## 输出格式
```json
{
  "red_flags": [
    {"type": "goodwill_high", "severity": "medium", "value": "42%"}
  ],
  "overall_risk": "medium",
  "recommendation": "谨慎关注"
}
```
