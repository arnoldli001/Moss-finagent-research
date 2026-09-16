---
name: sentiment-heat-tracking
description: >
  当需要监控市场情绪时使用。应每日执行。
  追踪个股和板块在社交媒体上的讨论热度。
  触发短语：舆情监控、热度追踪、社交媒体、股吧。
tags: [sentiment, social-media, heat]
---

# 舆情热度追踪

## 触发条件
每2小时执行一次，监控社交媒体和股吧的舆情变化。

## 执行步骤

### Phase 1：数据采集
- 采集微博、雪球、股吧的讨论数据
- 提取个股和板块的讨论量

### Phase 2：热度计算
- 计算讨论量环比变化
- 识别异常升温的个股和板块

### Phase 3：输出热度排名和预警
```json
{
  "hot_topics": [
    {"sector": "固态电池", "discussion_count": 15200, "change": "+320%"},
    {"sector": "AI算力", "discussion_count": 28500, "change": "+85%"}
  ],
  "alerts": [
    {"type": "heat_surge", "sector": "固态电池", "level": "high"}
  ]
}
```
