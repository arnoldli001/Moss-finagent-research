---
name: portfolio-construction
description: >
  当所有分析结果就绪时使用。用于构建投资组合和分配仓位。
  触发短语：组合构建、仓位管理、投资组合。
tags: [portfolio, allocation, risk]
references:
  - references/portfolio-rules.md
---

# 组合构建与仓位分配

## 触发条件
当所有分析Agent完成后触发。

## 执行步骤

### Phase 1：三维度评估
- 确定性 × 弹性 × 时间窗口

### Phase 2：仓位分配
- 底仓50%：抗周期龙头
- 卫星仓35%：瓶颈环节弹性方
- 战术仓15%：预期差高赔率

### Phase 3：风控规则
- 单票仓位上限
- 行业集中度上限
- 止损止盈规则

## 输出格式
```json
{
  "portfolio": {
    "core": [
      {"code": "601138.SH", "name": "工业富联", "weight": "8%", "reason": "AI服务器代工龙头"}
    ],
    "satellite": [
      {"code": "300308.SZ", "name": "中际旭创", "weight": "5%", "reason": "光模块瓶颈环节"}
    ],
    "tactical": [
      {"code": "688256.SH", "name": "寒武纪", "weight": "2%", "reason": "国产AI芯片弹性标的"}
    ]
  },
  "risk_rules": {
    "max_single_position": "10%",
    "max_sector_concentration": "30%",
    "stop_loss": "-15%"
  }
}
```
