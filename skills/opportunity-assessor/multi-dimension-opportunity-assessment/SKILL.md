---
name: multi-dimension-opportunity-assessment
description: >
  当事件分析结果就绪时使用。应在产业链传导分析完成后触发。
  用于从多个维度评估事件的机会等级。
  触发短语：机会评估、买入机会、弹性空间、上涨空间。
tags: [opportunity, assessment, alpha, upside]
references:
  - references/opportunity-criteria.md
---

# 多维度机会评估

## 触发条件
当产业链传导Agent完成分析后触发。

## 执行步骤

### Phase 1：业绩确定性评估
- 订单是否充足
- 产能是否释放
- 增速是否可持续

### Phase 2：估值安全边际评估
- 当前估值分位
- 与可比公司估值对比
- 当前市值是否低于中性估值40%以上

### Phase 3：催化剂强度评估
- 近期事件催化
- 政策催化
- 订单/产能/技术催化

### Phase 4：预期差评估
- 市场认知是否充分
- 机构持仓是否集中
- 催化剂是否明确

### Phase 5：综合评分
- 输出综合机会评分（0-100）
- 机会等级：高机会（≥80）/ 中机会（65-79）/ 低机会（<65）

## 输出格式
```json
{
  "opportunity_score": 85,
  "opportunity_level": "high",
  "dimensions": {
    "earnings_certainty": {"score": 88, "level": "high"},
    "valuation_safety": {"score": 82, "level": "high"},
    "catalyst_strength": {"score": 85, "level": "high"},
    "expectation_gap": {"score": 80, "level": "high"}
  },
  "affected_stocks": [
    {"code": "002460.SZ", "name": "赣锋锂业", "opportunity": "high"}
  ],
  "confidence": 0.86
}
```
