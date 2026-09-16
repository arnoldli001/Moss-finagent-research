---
name: supply-chain-valuation-elasticity
description: >
  当产业链瓶颈识别完成后使用。用于评估各环节的估值弹性，
  判断哪个环节在产业周期中具有最大的市值弹性。
tags: [supply-chain, valuation, elasticity, lifecycle]
references:
  - references/lifecycle-elasticity-matrix.md
---

# 产业链估值弹性判断

## 触发条件
当产业链瓶颈识别完成后触发。

## 执行步骤

### Phase 1：产业生命周期定位
- 渗透率<1% → 预研期 → 材料弹性最大
- 渗透率1%-5% → 导入期 → 设备弹性最大
- 渗透率10%-30% → 成长期 → 设备弹性最大
- 渗透率>65% → 成熟期 → 整合厂下游弹性最大
- 技术革新期 → 材料弹性最大

### Phase 2：供需格局与议价能力评估
- 供给约束强+需求爆发 → 弹性极大
- 毛利率在产业链中的位置（对上游压价能力、对下游提价能力）

### Phase 3：价值占比与变化趋势
- 微笑曲线两端（研发、品牌）附加值高
- 技术迭代方向：价值占比提升的环节弹性更大

### Phase 4：输出估值弹性评分（1-5分）
```json
{
  "layer": "固态电解质",
  "lifecycle_stage": "导入期",
  "penetration_rate": "3%",
  "max_elasticity_layer": "设备",
  "elasticity_score": 5,
  "reasoning": "渗透率处于导入期，设备环节受益于产能建设需求，弹性最大"
}
```
