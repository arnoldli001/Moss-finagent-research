---
name: sentiment-consistency-analysis
description: >
  当舆情热度追踪完成后使用。用于判断市场情绪的一致性，
  识别情绪极端值。
  触发短语：情绪一致性、分歧、过度乐观、过度悲观。
tags: [sentiment, consistency, nlp]
---

# 舆情情感极性与一致性

## 触发条件
当舆情热度追踪完成后触发。

## 执行步骤

### Phase 1：情感分类
- 对讨论文本进行情感分类（正面/负面/中性）

### Phase 2：一致性计算
- 计算正面/负面/中性比例
- 判断情绪一致性（分歧/一致）
- 识别情绪极端值（过度乐观/过度悲观）

## 输出格式
```json
{
  "sentiment_distribution": {
    "positive": 0.72,
    "negative": 0.18,
    "neutral": 0.10
  },
  "consistency": "high",
  "extreme": "over_optimistic",
  "trading_implication": "警惕短期回调风险"
}
```
