---
name: calendar-event-parsing
description: >
  当获取到投资日历数据时使用。应在每日开盘前执行。
  用于解析当日和未来一周的关键事件，关联受影响个股和行业。
  触发短语：投资日历、财报发布、限售解禁、股东大会、经济数据。
tags: [calendar, event, schedule, planning]
references:
  - references/event-importance-ranking.md
---

# 日历事件解析

## 触发条件
每日08:00由定时任务触发，解析当日和未来一周的投资日历。

## 执行步骤

### Phase 1：事件类型分类
- **财报类**：年报/季报/业绩预告发布
- **解禁类**：限售股解禁、大股东减持窗口
- **会议类**：股东大会、投资者交流会
- **宏观类**：GDP/CPI/PMI/利率决议发布
- **行业类**：行业展会、政策发布窗口

### Phase 2：受影响标的关联
- 解析事件直接关联的个股和行业
- 评估事件影响范围（个股/行业/全市场）
- 标注事件优先级（高/中/低）

### Phase 3：预期影响判断
- 判断事件可能的影响方向（利好/利空/中性）
- 评估市场对事件的预期是否充分
- 标注需要重点关注的标的

## 输出格式
```json
{
  "date": "2026-09-14",
  "events": [
    {
      "event_type": "财报发布",
      "company": "宁德时代",
      "code": "300750.SZ",
      "expected_impact": "positive",
      "importance": "high",
      "notes": "市场预期Q3净利润同比增长35%"
    }
  ]
}
```
