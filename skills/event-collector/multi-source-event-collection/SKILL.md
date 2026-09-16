---
name: multi-source-event-collection
description: >
  当定时任务触发时使用。按配置频率从政策网站、新闻API、
  投资日历等来源采集事件。
  触发短语：事件采集、数据采集、定时任务。
tags: [collection, data-source, ingestion]
references:
  - references/source-config.md
---

# 多源事件采集

## 触发条件
按以下频率定时触发：
- 政策网站：每小时
- 新闻API：每5分钟
- 投资日历：每日08:00
- 公告：每10分钟

## 执行步骤

### Phase 1：按数据源采集
- 政策网站：国务院、发改委、工信部、地方政府官网
- 新闻API：东方财富、新浪财经、财联社
- 投资日历：东方财富投资日历、同花顺投资日历
- 公告：巨潮资讯网、交易所官网

### Phase 2：事件标准化
- 统一事件消息格式
- 提取标题、内容、来源、发布时间

### Phase 3：事件去重
- 基于内容哈希生成事件指纹
- 与已有事件指纹比对
- 标记重复事件（is_duplicate=true）

## 输出格式
```json
{
  "message_id": "uuid",
  "sender": "policy_collector",
  "receiver": "event_bus",
  "message_type": "event",
  "timestamp": 1726300800000,
  "payload": {
    "event_id": "evt_20260914_001",
    "event_type": "policy",
    "title": "关于加快固态电池产业发展的指导意见",
    "content": "政策原文...",
    "source_url": "https://www.gov.cn/...",
    "publish_time": "2026-09-14T08:00:00+08:00"
  }
}
```
