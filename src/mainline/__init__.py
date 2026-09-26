"""主线挖掘模块（mainline mining）。

## 模块定位

在板块或题材出现大资金建仓行为时，于**启动前或启动初期**发出异常告警，
并自动记录告警上报时间与上报后未来几个月的最大涨幅，输出监控胜率报告。

## 漏斗式三层结构（V2.0 去重版）

    第一层 six_dim       开源六维基座（全市场初筛）── 前 20% ──▶ 候选池
    第二层 accumulation  三维建仓痕迹（仅候选池内）── 前 5~10 ──▶ 精选名单
    第三层 leader        龙头共振确认（**门控加分，不占权重**）

    最终分 = 第一层 × w + 第二层 × (100−w) + gate_bonus(0~20) + etf_bonus(0~20)

`w` = `synthesis.layer_weights.six_dim`。V2.0 是 50，**V2.3 起是 100**：
候选池内六维层 sd≈4.5、第二层 sd≈13.1，两项的典型幅度是 `w·sd`，
所以名义"50/50"实际约 **26/74**（按 `w·sd`；按方差占比约 11/89）——
排序由第二层主导。第二层仍然计算并展示，只是在合成分里不再占权重。
详见 `docs/MAINLINE_MINING.md` §16.34。

**V1.0 的问题与 V2.0 的修正**：V1.0 把六维、五维、龙头三层简单加权相加，
资金流 / 股东户数 / 成交量因子被重复加权 2-3 倍，回测胜率虚高、实盘失效。
V2.0 改成漏斗式串联 —— 每一层只处理上一层**未覆盖**的增量信息：

    - 第二层删除「主力资金强度」（第一层 moneyflow 已覆盖）、
      「筹码集中 / 股东户数」（第一层 chips 已覆盖）；
    - 第二层只保留「通用均线排列 / RSI」之外的**形态识别**；
    - 第三层不再计算原始资金流因子，只算派生指标（龙头资金集中度）
      与真正独立的新数据（龙虎榜席位）。

## 文件职责（严格单向依赖）

    config.py     configs/mainline.yaml → dataclass（mtime 热重载）
    models.py     领域模型（纯数据，无 IO、无算法）—— 模块的**契约**
    scoring.py    打分数学原语（纯函数；无数据 ≠ 0 分、样本不足返回 None）
    datastore.py  主线专属本地数据仓（回测与实盘**共用同一份输入**）
    sources.py    取数与规范化（Tushare / 本地行情仓 / 东财）
    six_dim.py    第一层：六维基座打分
    accumulation.py 第二层：三维建仓痕迹打分
    leader.py     第三层：龙头识别与共振门控
    service.py    漏斗编排 + 动态权重 + 告警规则 + 快照
    futures.py    期货先行信号（映射表 + 四类信号 + 告警）
    backtest.py   Purged Walk-Forward 回测 + 因子相关性验证 + 分场景验证
    report.py     监控胜率六章报告（Markdown）
    notify.py     飞书 / 钉钉推送
    storage.py    评分 / 告警 / 回测 / 期货校准的 SQLite 持久化

## 两条全局硬规则

1. **口径统一、不混用**：东财、同花顺、Tushare 的"主力资金"定义不同。
   本模块固定**一套**口径做纵向对比（板块资金流走本地行情仓的个股聚合），
   其余源只作展示与交叉校验，绝不混入同一条打分链路。
2. **无数据 ≠ 0 分**：取不到数的维度从权重里剔除并重新归一化，同时在
   `gaps` 里如实标注。"不知道"和"没有异常"在界面上长得一样，
   但结论完全不同 —— 用 0 分冒充会让数据源一抖动就全市场不告警。
"""

from __future__ import annotations

from src.mainline.config import MainlineConfig, load_config
from src.mainline.models import (
    AlertSignal,
    BoardInfo,
    BoardKind,
    BoardScore,
    MainlineSnapshot,
    SignalLevel,
)

__all__ = [
    "AlertSignal",
    "BoardInfo",
    "BoardKind",
    "BoardScore",
    "MainlineConfig",
    "MainlineSnapshot",
    "SignalLevel",
    "load_config",
]
