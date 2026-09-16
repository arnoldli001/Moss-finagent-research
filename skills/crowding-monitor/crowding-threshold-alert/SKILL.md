---
name: crowding-threshold-alert
description: >
  当拥挤度计算完成后使用。输出高度拥挤和低度拥挤板块。
  触发短语：拥挤预警、拥挤度阈值。
tags: [crowding, alert, threshold]
---

# 拥挤度阈值预警

## 触发条件
当拥挤度计算完成后触发。

## 执行步骤

### Phase 1：高度拥挤预警
- 综合得分 ≥ 0.8 → 警惕风险

### Phase 2：低度拥挤预警
- 综合得分 ≤ 0.2 → 可能存在机会

### Phase 3：变化预警
- 逼近阈值的板块

## 输出格式
```json
{
  "high_crowding": [
    {"sector": "半导体", "score": 0.92, "trend": "up"}
  ],
  "low_crowding": [
    {"sector": "银行", "score": 0.12, "trend": "down"}
  ],
  "threshold_approaching": [
    {"sector": "新能源", "score": 0.78, "direction": "up"}
  ]
}
```
