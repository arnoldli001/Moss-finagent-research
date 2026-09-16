---
name: event-deduplication
description: >
  当采集到事件后立即使用。用于避免同一事件被重复处理。
tags: [collection, deduplication, idempotent]
---

# 事件去重与幂等处理

## 触发条件
采集到事件后立即执行，写入events表之前必须完成去重。

## 执行步骤

### Phase 1：生成事件指纹
- 规范化标题：去除空格与标点符号，统一全角/半角字符
- 指纹 = sha1(规范化标题 + 发布日期 + 来源)，发布日期取publish_time的日期部分（YYYY-MM-DD）

### Phase 2：指纹比对
- 与已有事件指纹比对（数据库唯一键或内存set）
- 命中已有指纹则判定为重复事件

### Phase 3：标记重复事件
- 重复事件标记 is_duplicate=true
- 丢弃或归档，不进入分析流水线

### Phase 4：新事件放行
- 仅将新事件推入分析流水线
- 同一事件重放采集时结果一致，保证幂等可重放

## 输出格式
```json
{
  "total": 120,
  "duplicates": 15,
  "new_events": 105,
  "sample_fingerprints": [
    {"event_id": "evt_20260914_001", "fingerprint": "sha1:a3f1c9...", "is_duplicate": false},
    {"event_id": "evt_20260914_002", "fingerprint": "sha1:9b2c47...", "is_duplicate": true}
  ]
}
```

## 约束规则
- 跨源转载同一事件按"规范化标题+发布日期"跨源去重，不同来源的转载不产生重复记录
- 事件原始溯源必须保留首次来源（source_url、source_name），归档重复项时须记录其后续来源
- 去重逻辑必须是纯函数，可单元测试，不依赖外部可变状态
- 不得因去重丢失事件的首见时间（first_seen_time），重复命中时只更新重复标记不覆盖首见信息
