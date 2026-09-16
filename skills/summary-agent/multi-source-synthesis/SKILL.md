---
name: multi-source-synthesis
description: >
  当所有Agent分析完成后使用。应在风险评估和机会评估都完成后触发。
  用于生成最终预警对象。
  触发短语：汇总、综合判断、预警生成、决策。
tags: [synthesis, aggregation, decision, alert]
references:
  - references/synthesis-rules.md
---

# 多源分析结果聚合

## 触发条件
当政策分析、新闻分析、产业链分析、风险评估、机会评估全部完成后触发。

## 执行步骤

### Phase 1：结果收集
- 收集政策分析结果
- 收集新闻分析结果
- 收集产业链传导结果
- 收集风险评估结果
- 收集机会评估结果

### Phase 2：多源冲突解决
- 风险评估为高风险、机会评估为高机会时：
  - 根据置信度加权
  - 根据时间维度判断（短期风险 vs 中期机会）
  - 输出综合判断
- 多个Agent结论一致时，提升置信度

### Phase 3：最终预警生成
- 确定预警类型（风险/机会）
- 确定预警等级（高/中/低）
- 汇总受益/风险个股列表
- 标注发生时间
- 输出置信度和决策理由

## 输出格式
```json
{
  "alert_type": "opportunity",
  "alert_level": "high",
  "title": "固态电池政策利好",
  "description": "政策大力支持固态电池产业化",
  "risk_score": 25,
  "opportunity_score": 82,
  "confidence": 0.88,
  "affected_stocks": [
    {"code": "300750.SZ", "name": "宁德时代", "impact": "positive", "reason": "固态电池龙头"},
    {"code": "002460.SZ", "name": "赣锋锂业", "impact": "positive", "reason": "锂资源+固态电池"}
  ],
  "affected_industries": ["固态电池", "锂电材料"],
  "trigger_time": "2026-09-14T08:05:00+08:00"
}
```
