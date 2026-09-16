---
name: sector-crowding-calculation
description: >
  当每日收盘后使用。计算全A行业和概念板块的拥挤度得分。
  触发短语：拥挤度、成交额占比、换手率、交易拥挤。
tags: [crowding, sector, quant]
references:
  - references/crowding-methodology.md
---

# 板块拥挤度计算

## 触发条件
每日收盘后17:00触发。

## 执行步骤

### Phase 1：数据获取
- 获取板块日成交额和换手率
- 获取全市场成交额

### Phase 2：计算
- 成交额占比 = 板块成交额 / 全市场成交额
- 252日移动平均平滑
- 转换为历史分位数（expanding window，至少126个交易日）
- 综合得分 = (成交额占比分位数 + 换手率分位数) / 2

### Phase 3：阈值判断
- 高度拥挤：≥ 0.8
- 低度拥挤：≤ 0.2
- 变化预警：逼近阈值的板块

## 输出格式
```json
{
  "sector": "半导体",
  "crowding_score": 0.92,
  "volume_share_pct": 0.95,
  "turnover_pct": 0.89,
  "status": "高度拥挤"
}
```
