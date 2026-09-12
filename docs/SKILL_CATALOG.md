# Skill目录

## 一、分析引擎Skill清单

| Skill编号 | 名称 | 核心功能 | 关键公式/模型 |
|-----------|------|----------|---------------|
| Skill 1 | 政策解读 | 政策分类、传导路径、强度评分 | 政策力度×传导路径 |
| Skill 2 | 产业链分析 | 图谱构建、瓶颈识别、产能周期 | 生命周期×供需格局 |
| Skill 3 | 个股深度研究 | 股性分析、估值测算、催化剂 | DCF/PE/PEG |
| Skill 4 | 估值计算 | DCF、相对估值、情景分析 | 合理PE=(1-g/ROIC)/(r-g) |
| Skill 5 | 财务排雷 | 现金流、商誉、关联交易检测 | Altman Z-Score、Beneish M-Score |
| Skill 6 | 周期定位 | 宏观/市场/情绪周期判断 | 美林时钟、库存周期 |
| Skill 7 | 催化剂跟踪 | 事件驱动、弹性测算 | 事件影响×时间窗口 |
| Skill 8 | 组合管理 | 仓位分配、风控规则 | 风险平价、Black-Litterman |
| Skill 9 | 天气与自然周期 | 厄尔尼诺/拉尼娜影响 | NOAA指数×品种映射 |
| Skill 10 | 库存周期定位 | 四阶段分类、强势行业 | 收入×库存二维矩阵 |
| Skill 11 | 多周期共振 | 康波/朱格拉/库存嵌套 | 多周期相位对齐 |
| Skill 12 | CPI/PPI剪刀差 | 四象限分类、利润分配 | PPI-CPI剪刀差×方向 |
| Skill 13 | PPI上行轮动 | 历史复盘、阶段配置 | 四阶段模型 |
| Skill 14 | 通胀拐点预警 | 五大信号体系 | PPI环比见顶、剪刀差高点 |
| Skill 15 | 基金对比分析 | 多基金对比、组合优化 | 相关性矩阵、业绩归因 |
| Skill 16 | 产业拐点判断 | PPI+库存+产能三维 | 领先滞后模型、IC/IR |
| Skill 17 | 个股价值评估 | 多方法交叉验证 | DCF+相对估值+PEG |
| Skill 18 | 财务风险预警 | Z-Score+M-Score | Altman+Beneish |
| Skill 19 | 逻辑审计 | 推理路径记录、回测 | IC/IR检验、分层回测 |
| Skill 20 | 投研建议生成 | 信号→验证→建议 | 映射规则表、胜率验证 |

## 二、Skill目录结构

```
skills/
├── macro-analysis/
│   ├── SKILL.md
│   ├── scripts/
│   │   ├── calc_spread.py
│   │   └── detect_turning.py
│   ├── references/
│   │   └── cycle-framework.md
│   └── assets/
│       └── report-template.md
├── industry-chain/
├── stock-valuation/
├── financial-risk/
└── fund-comparison/
```

## 三、Skill开发规范

- 每个Skill必须包含SKILL.md（≤500行）
- scripts/存放确定性计算逻辑，不依赖LLM
- references/存放详细参考文档，按需加载
- assets/存放模板文件
