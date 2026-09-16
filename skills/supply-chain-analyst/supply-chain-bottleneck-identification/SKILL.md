---
name: supply-chain-bottleneck-identification
description: >
  当需要分析产业链变化时使用。应在政策分析或新闻分析完成后触发。
  用于识别当前产业链最紧张的环节，发现被市场忽视的瓶颈。
  触发短语：产业链、供应链、瓶颈、卡点、稀缺环节。
tags: [supply-chain, bottleneck, constraint, alpha]
references:
  - references/supply-chain-map.md
  - references/bottleneck-criteria.md
  - references/serenity-framework.md
---

# 产业链瓶颈识别

## 触发条件
当政策分析Agent或新闻分析Agent输出影响行业列表后触发。

## 核心方法论
借鉴Serenity Skill的供应链瓶颈猎手框架：

不问“AI推荐什么股票”，问“如果这个趋势继续扩张，哪一环会先不够用？”

超额收益来源：第一层瓶颈（GPU、HBM、电力）已被充分定价。
真正的alpha在第二层、第三层——光模块、激光器、InP衬底、SOI晶圆、
外延设备、晶圆级测试、IC载板、特殊玻纤等。

## 执行步骤

### Phase 1：产业链图谱加载
- 加载目标行业的产业链图谱（上中下游环节）
- 识别关键环节和主要公司映射

### Phase 2：瓶颈评估
对每个环节评估：
- **供给约束强度**：技术壁垒、产能建设周期、政策限制
- **需求爆发力度**：下游需求增速、渗透率提升速度
- **扩产周期**：从投资到量产的时间
- **瓶颈迁移路径**：当前瓶颈→下一阶段瓶颈

### Phase 3：瓶颈排序
- 按“紧张度×弹性”排序
- 输出当前瓶颈环节和下一阶段瓶颈环节
- 识别被市场忽视的隐性瓶颈

### Phase 4：输出
```json
{
  "current_bottleneck": ["HBM", "CoWoS先进封装"],
  "next_bottleneck": ["电力接入", "中压变压器"],
  "hidden_bottlenecks": ["IC载板", "特殊玻纤"],
  "bottleneck_ranking": [
    {"layer": "HBM", "tightness": 5, "elasticity": 4},
    {"layer": "CoWoS", "tightness": 5, "elasticity": 3},
    {"layer": "电力接入", "tightness": 5, "elasticity": 5}
  ]
}
```

## 参考框架
- Serenity Skill供应链瓶颈猎手框架：从物理供应链的咽喉位置出发，
  找那些没人注意但一旦断货整个行业都得停下来等的公司。
- 分层拆解：趋势确认 → 供应链物理拆解 → 稀缺层识别 → 证据链验证 → 排序。
