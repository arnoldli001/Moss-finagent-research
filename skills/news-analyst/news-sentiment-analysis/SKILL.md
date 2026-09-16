---
name: news-sentiment-analysis
description: >
  当新闻实体抽取完成后使用。用于判断新闻对相关个股的情感极性
  及其强度，识别预期差事件。
  触发短语：利好、利空、中性、超预期、不及预期。
tags: [news, sentiment, nlp, alpha]
references:
  - references/sentiment-criteria.md
  - references/expectation-gap-framework.md
---

# 新闻情感分析

## 触发条件
当新闻实体抽取完成后自动触发。使用Qwen3:8B模型进行推理。

## 执行步骤

### Phase 1：情感极性判断
- **正面**：业绩超预期、中标大单、政策利好、技术突破
- **负面**：业绩暴雷、监管处罚、减持、诉讼
- **中性**：例行公告、人事变动、常规经营

### Phase 2：情感强度评估（1-5分）
- 1分：微弱影响，市场基本忽略
- 2分：轻微影响，短期波动
- 3分：中等影响，值得关注
- 4分：重大影响，可能改变短期趋势
- 5分：极重大影响，可能改变中长期逻辑

### Phase 3：预期差识别
- 对比市场一致预期与事件实际影响
- 判断是否属于“预期差”事件
- 预期差越大，潜在交易机会越大

### Phase 4：输出
```json
{
  "sentiment": "positive",
  "intensity": 4,
  "expectation_gap": true,
  "gap_direction": "positive_surprise",
  "gap_magnitude": "large",
  "confidence": 0.85,
  "reasoning": "中标金额远超市场预期，且为公司首次进入该客户供应链"
}
```

## 约束规则
- 情感判断必须基于具体事实，不得主观臆断
- 预期差判断需引用市场一致预期数据
- 置信度低于0.7时标注“需人工复核”
