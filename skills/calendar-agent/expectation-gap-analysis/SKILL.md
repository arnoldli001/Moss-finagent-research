---
name: expectation-gap-analysis
description: >
  当日历事件解析完成后使用。用于判断市场对事件的预期是否充分，
  识别预期差带来的交易机会。
tags: [calendar, expectation, alpha, gap]
---

# 事件预期差分析

## 触发条件
当日历事件解析完成后自动触发。

## 执行步骤

### Phase 1：获取市场一致预期
- 从研报数据库获取分析师一致预期
- 从期权市场获取隐含波动率
- 从历史数据获取季节性规律

### Phase 2：对比事件可能结果与一致预期
- 计算预期差方向和幅度
- 评估预期差的市场影响力

### Phase 3：输出潜在交易机会
- 预期差为正且幅度大 → 潜在做多机会
- 预期差为负且幅度大 → 潜在做空/回避机会

## 输出格式
```json
{
  "event": "宁德时代Q3财报",
  "consensus": "净利润120亿",
  "actual_estimate": "净利润135亿",
  "gap": "+12.5%",
  "direction": "positive_surprise",
  "trading_implication": "潜在做多机会",
  "confidence": 0.78
}
```
