---
name: bottom-opportunity-identification
description: >
  当需要寻找底部布局机会时使用。应定期对全市场执行。
  筛选低拥挤度+低估值分位的板块。
  触发短语：底部布局、价值洼地、低估机会、逆向投资。
tags: [opportunity, bottom, value, contrarian]
---

# 底部布局机会识别

## 触发条件
每周对全市场行业和概念板块执行一次。

## 筛选条件
- 拥挤度 ≤ 0.2
- 估值分位 ≤ 20%
- 基本面改善信号（盈利增速触底回升、营收增速转正）
- 政策催化或资金信号

## 输出格式
```json
{
  "opportunities": [
    {
      "sector": "银行",
      "crowding_score": 0.12,
      "pe_percentile": 8,
      "dividend_yield": 5.8,
      "fundamental_signal": "不良率企稳，净息差降幅收窄",
      "recommendation": "底仓配置"
    }
  ]
}
```
