---
name: multi-dimension-risk-assessment
description: >
  当事件分析结果就绪时使用。应在产业链传导分析完成后触发。
  用于从多个维度评估事件的风险等级。
  触发短语：风险评估、风险等级、风险预警、下跌风险。
tags: [risk, assessment, multi-dimension, warning]
references:
  - references/risk-factors.md
  - references/risk-thresholds.md
---

# 多维度风险评估

## 触发条件
当产业链传导Agent完成分析后触发。

## 执行步骤

### Phase 1：基本面风险评估
- 盈利增速是否下滑
- 现金流是否恶化
- 负债率是否过高

### Phase 2：估值风险评估
- 当前估值分位（PE/PB历史分位）
- 是否接近历史见顶规律

### Phase 3：拥挤度风险评估
- 成交额占比是否接近20%预警线
- 换手率是否异常放大

### Phase 4：流动性风险评估
- 资金面是否收紧
- 成交量是否萎缩

### Phase 5：政策风险评估
- 监管方向是否不利
- 合规性是否存在问题

### Phase 6：综合评分
- 输出综合风险评分（0-100）
- 风险等级：高风险（≥75）/ 中风险（60-74）/ 低风险（<60）
- 置信度评估

## 输出格式
```json
{
  "risk_score": 78,
  "risk_level": "high",
  "dimensions": {
    "fundamental": {"score": 25, "level": "low"},
    "valuation": {"score": 85, "level": "high"},
    "crowding": {"score": 82, "level": "high"},
    "liquidity": {"score": 45, "level": "medium"},
    "policy": {"score": 20, "level": "low"}
  },
  "affected_stocks": [
    {"code": "300750.SZ", "name": "宁德时代", "risk": "high"}
  ],
  "confidence": 0.82
}
```
