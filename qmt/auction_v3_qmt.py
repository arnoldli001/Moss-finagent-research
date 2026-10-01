# -*- coding: utf-8 -*-
"""集合竞价选股 V3 · QMT(迅投 miniQMT / xtquant) 实盘策略

================================================================================
一、运行环境
================================================================================
- 64 位 Python 3.6 ~ 3.12（xtquant 自带多版本适配）
- 必须先启动并登录 **MiniQMT 客户端**（东莞证券 QMT 需开通「极简模式」）
- 依赖：xtquant（随 QMT 客户端提供）、numpy、pandas
- 部署：把本文件放到任意目录，用 QMT 自带 Python 或系统 Python 运行：
      python auction_v3_qmt.py --dry-run     # 只选股打印，不下单（首次务必先跑这个）
      python auction_v3_qmt.py               # 实盘

================================================================================
二、⚠️ 与回测的差异（上线前必读）
================================================================================
本脚本移植自回测 V3，但实盘无法完全复刻回测假设，差异如下：

1. **【数据缺口，影响最大】QMT 没有「涨停原因/题材」数据。**
   打分模型中「题材热度」(12 分) 与「题材龙头」(3 分) 在 QMT 内无法计算，
   「封流比」(6 分) 需要涨停封单量、日线也没有。合计约 **23.5/100 权重缺失**。
   本脚本按仓库既有口径「**缺席维度按剩余权重归一化**」处理，并额外提供
   `THEME_CSV` 外部注入接口（见 §参数区）。但请注意：
   **归一化后分数分布已改变，原 55 分门槛不再等价** —— 上线前请先用
   `--calibrate` 跑几天，用实际分布重新标定 SCORE_THRESHOLD。

2. **【成交价，影响次之】9:25 定格后无法再以该价格成交。**
   回测假设「9:25 集合竞价撮合价成交」。但 9:25-9:30 为静默期，此时报单
   进入 9:30 连续竞价，成交价是 9:30 的开盘价。本脚本用「9:25 价 ×(1+滑点)
   限价单」逼近，**实际会有滑点**。若要做到位，需在 9:24:5x 前挂单参与
   9:25 撮合，但那时竞价虚拟价还在变、且 9:20 后不可撤单 —— 风险更高。

3. **【卖出判定】回测用「收盘价判定 + 收盘价成交」。**
   实盘在 14:56:30 用当时价格近似收盘价做判定，委托参与 14:57-15:00 收盘
   集合竞价，成交价≈收盘价。**判定输入是 14:56 的价，不是收盘价**。

4. **【净值熔断】本脚本默认关闭**（DD_BREAKER_ROLL=0）。
   回测显示它能显著改善回撤，但该参数是在同一样本上选出的，**未经
   walk-forward 验证**。建议先 dry-run 观察，确认后再开。

================================================================================
三、⚠️ 风险声明
================================================================================
回测中「近 1 年年化 +280%」是**单一市场环境**下的结果（241 个交易日、219 笔
交易）；**四年全周期年化仅 +13.81%，最大回撤 −51.11%**。回测的 87% 卖出为
「收盘判定 + 收盘成交」这一乐观假设，且从未做过成交价压力测试。

**请先用小资金实盘验证 1-3 个月，不要直接上主仓位。**
本代码仅用于技术研究，不构成任何投资建议，使用风险自负。

================================================================================
四、代码结构
================================================================================
  §1 参数            §2 纯逻辑:打分模型      §3 纯逻辑:市场周期
  §4 纯逻辑:竞价特征  §5 QMT数据适配          §6 选股主流程
  §7 状态持久化      §8 交易执行             §9 卖出与风控
  §10 主循环时间状态机                       §11 入口
§2/§3/§4 是纯函数，不依赖 QMT，可单独单测。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

# ==============================================================================
# §1 参数区 —— 全部可调项集中在这里
# ==============================================================================

# ---- 1.1 策略参数（与回测 V3 严格对应）----
SCORE_THRESHOLD = 55.0        # 入选门槛。⚠️ 维度缺失后需重新标定，见文件头 §二.1
MAX_BUY_PER_DAY = 3           # 每日最多买入分数最高的 N 只
MAX_BUY_HIGH = 2              # 最高连板 ≥7 时降为 N 只
HIGH_STREAK = 7               # 「高位」的连板阈值
WEIGHT_BASE = 0.30            # 单只基准仓位（占权益）
WEIGHT_HIGH = 0.20            # 高位日单只仓位
MAX_WEIGHT_SINGLE = 0.35      # 单只个股市值 ≤ 权益 × 该值
ADD_RATIO = 0.30              # 已持有该股 → 本次买入额 = 已有市值 × 该值
RUSH_WEIGHT_MULT = 1.20       # 带「抢筹」标签 → 该股本次仓位 ×1.2

# ---- 1.2 前置筛选 ----
MAX_PREV_CLOSE = 37.0         # 昨日收盘价 ≥ 该值 → 剔除
CAP_MIN, CAP_MAX = 15e8, 110e8    # 流通市值区间（昨日收盘口径）
CAP_EXCLUDE_MIN, CAP_EXCLUDE_MAX = 15e8, 40e8   # V3 追加剔除段 → 实际可买 40~110 亿
MA_WINDOW = 20                # 昨收必须 > 20 日均价
EXCLUDE_PREFIX = ("300", "301", "688")          # 剔除创业板/科创板

# ---- 1.3 一票否决 ----
MIN_OPEN_GAP = 2.0            # 开盘涨幅 < 该值 → 否决（抢筹/大量抢筹豁免）
MAX_OPEN_GAP = 6.0            # 开盘涨幅 > 该值 → 否决（大量抢筹豁免）
MAX_AUCTION_VOL_RATIO = 15.0  # 竞价量比 > 该值 → 否决（抢筹/大量抢筹豁免）
MAX_BROKEN_RATE = 0.60        # 市场炸板率 > 该值 → 否决
HEAVY_RUSH_GAP = 6.0          # 主板 > 该涨幅 且带抢筹 且量比>7% → 标「大量抢筹」
HEAVY_RUSH_VOL_PCT = 7.0      # 上条的量比阈值（百分数）

# ---- 1.4 抢筹 / 抢跑 ----
JUMP_WINDOW_START = "09:24:40"
JUMP_WINDOW_END = "09:24:55"
RUSH_JUMP_UP = 1.01           # 跳空值 > 该值 → 抢筹
RUSH_JUMP_DOWN = 0.99         # 跳空值 < 该值 → 抢跑
RUSH_MIN_VS_YESTERDAY = 2.0   # 抢筹附加条件：今昨竞比 > 该值
RUSH_OUT_VOL_PCT = 10.0       # 抢跑附加条件：竞价量比 > 该值(%)
RUSH_HEAVY_VOL_PCT = 15.0     # 抢筹 且量比 > 该值 → 大量抢筹
RUSH_OUT_EXEMPT_STREAK = 1.0  # 抢跑豁免：昨日连板 > 该值
RUSH_OUT_EXEMPT_VS_YEST = 2.0 # 抢跑豁免：今昨竞比 < 该值
RUSH_OUT_EXEMPT_GAP = 1.0     # 抢跑豁免：9:25 涨幅 > 该值
TAKEOVER_START = "09:20:00"   # 承接强度取 9:20 后（不可撤单段）
TAKEOVER_MIN_POINTS = 8       # 少于此点数不做承接判定

# ---- 1.5 卖出规则（严格优先级）----
STOP_CLOSE_PCT = 0.05         # ② 收盘亏损 ≥5% → 清仓
DAY_DROP_PCT = -0.07          # ③ 当日收盘跌幅 ≥7% → 清仓
MA_EXIT_WINDOW = 5            # ④ 收盘跌破 5 日线 → 清仓
MAX_HOLD_DAYS = 10            # ⑤ 持有满 10 个交易日 → 清仓
TRIM_RET = 0.10               # ⑥ 收益 >10% 且当日跌 >2% → 卖 30%
TRIM_DAY = -0.02
TRIM_QTY = 0.30
T1_SELL_QTY = 0.50            # ⑦ 买入次日收盘未涨停 → 卖 50%

# ---- 1.6 组合层净值回撤熔断（默认关闭，见文件头 §二.4）----
DD_BREAKER_ROLL = 0           # 0 = 关闭；回测推荐 40
DD_BREAKER_DROP = 0.10

# ---- 1.7 交易与成本 ----
COMMISSION_RATE = 0.0001      # 佣金万分之一
COMMISSION_MIN = 0.0          # 免 5 元最低（如券商不免，改成 5.0）
STAMP_TAX_RATE = 0.001        # 卖出印花税千分之一
LOT = 100                     # 一手
INIT_CASH = 1_000_000.0       # 首次运行时的初始资金（之后以 state 为准）

# ---- 1.8 实盘执行 ----
BUY_SLIP = 0.01               # 买单限价 = 9:25价 ×(1+该值)，且不超过涨停价
SELL_SLIP = 0.01              # 卖单限价 = 现价 ×(1−该值)，且不低于跌停价
ORDER_WAIT_SEC = 20           # 下单后等待成交的秒数
POLL_INTERVAL = 3.0           # 竞价序列采集间隔（秒）
ADOPT_UNKNOWN_POSITIONS = False   # 是否接管非本策略建仓的持仓（默认否）
DRY_RUN = True                # 默认 dry-run，命令行 --live 才实盘

# ---- 1.9 路径 ----
BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "auction_v3_state.json"
TRADE_LOG = BASE_DIR / "auction_v3_trades.csv"
RUN_LOG = BASE_DIR / "auction_v3_run.log"
THEME_CSV = BASE_DIR / "theme_data.csv"   # 可选：外部题材数据（见 §1.10）

# ---- 1.10 可选外部题材数据 ----
# QMT 无题材数据。若你能在盘前生成 theme_data.csv，本脚本会自动读取并启用
# 「题材热度」「题材龙头」「封流比」三个维度。列名（首行表头，UTF-8）：
#   code,theme_name,theme_heat,is_leader,highest_ladder,limit_up_count,seal_to_float
#   sh600000,光伏,0.72,1,3,4,0.041
# code 支持 600000 / 600000.SH / sh600000 三种写法。
THEME_CSV_ENABLED = True

# ==============================================================================
# §2 纯逻辑：打分模型（阈值全部取自 src/auction_select/rulebook.py 的规则表）
# ==============================================================================
DIM_WEIGHTS: dict[str, float] = {
    "auction_strength": 19.0, "price_position": 14.0, "takeover": 12.0,
    "theme_heat": 12.0, "ladder": 10.0, "sentiment": 7.0,
    "seal_flow_ratio": 6.0, "auction_sentiment": 5.0, "previous_day": 5.0,
    "capital_fit": 4.0, "turnover": 3.0, "theme_leader": 3.0,
}
STAGE_ADJUST = {"主升期": 0.10, "发酵期": 0.05, "高位震荡期": 0.0,
                "试错期": -0.05, "退潮期": -0.25, "冰点": -0.35}


def clamp01(x: float | None) -> float | None:
    if x is None:
        return None
    return max(0.0, min(1.0, x))


def lerp(x: float, x0: float, y0: float, x1: float, y1: float) -> float:
    if x1 == x0:
        return y0
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def seg_at(x: float | None, segs: Sequence[tuple[float, float, float, float]],
           ) -> float | None:
    """分段线性映射。segs = ((lo, hi, score_lo, score_hi), ...) 升序不重叠。

    区间按 `lo < x <= hi` 取段（与规则表 `(a, b]` 的写法一致）。
    x 落在所有段之外时：取最近端的常数。
    """
    if x is None:
        return None
    for lo, hi, slo, shi in segs:
        if lo < x <= hi:
            return clamp01(lerp(x, lo, slo, hi, shi))
    first, last = segs[0], segs[-1]
    if x <= first[0]:
        return clamp01(first[2])
    if x > last[1]:
        return clamp01(last[3])
    # 落在两段之间的空隙（规则表里的不连续点）→ 取右侧段的起点
    for lo, hi, slo, shi in segs:
        if x > lo:
            continue
        return clamp01(slo)
    return None


_INF = float("inf")


def dim_auction_strength(v: float | None) -> float | None:
    """竞价量能。读「今昨竞比」。>10 往往见顶 → 压到 0.2（非单调）。"""
    if v is None:
        return None
    if v > 10.0:
        return 0.20
    return seg_at(v, ((0.0, 1.0, 0.0, 0.15), (1.0, 2.0, 0.15, 0.55),
                      (2.0, 3.0, 0.55, 0.75), (3.0, 5.0, 0.75, 1.0),
                      (5.0, 10.0, 1.0, 0.70)))


def dim_price_position(gap: float | None) -> float | None:
    """开盘位置。低开/平开剔除、>6% 剔除，最优点 2%~4%。"""
    if gap is None:
        return None
    if gap < 2.0:
        return 0.0
    if gap > 9.0:
        return 0.25
    return seg_at(gap, ((2.0, 4.0, 0.75, 1.0), (4.0, 6.0, 1.0, 0.85),
                        (6.0, 9.0, 0.85, 0.55)))


def dim_takeover(score: float | None) -> float | None:
    """承接强度 0~100 线性归一。"""
    return clamp01(score / 100.0) if score is not None else None


def dim_theme_heat(v: float | None) -> float | None:
    return clamp01(v)


def dim_ladder(streak: float | None) -> float | None:
    """连板梯队：3 板峰值，首板 0.55，高位递减。"""
    if streak is None:
        return None
    for upper, val in ((1.0, 0.55), (2.0, 0.85), (3.0, 1.0), (4.0, 0.82),
                       (5.0, 0.64)):
        if streak <= upper:
            return val
    return 0.30


def dim_sentiment(temperature: float | None, stage: str) -> float | None:
    """情绪周期：温度/100 + 阶段修正。"""
    if temperature is None:
        return None
    return clamp01(temperature / 100.0 + STAGE_ADJUST.get(stage, 0.0))


def dim_seal_flow(ratio: float | None) -> float | None:
    """封流比：封单/流通市值 ≥3% 满分。"""
    return seg_at(ratio, ((0.0, 0.03, 0.0, 1.0), (0.03, _INF, 1.0, 1.0)))


def dim_auction_sentiment(v: float | None) -> float | None:
    return clamp01(v)


def dim_previous_day(seal: float | None, amt_ratio: float | None) -> float | None:
    """昨日质量：封单/流通盘 与 竞价额/昨日成交额 两条曲线的均值。"""
    parts: list[float] = []
    a = seg_at(seal, ((0.0, 0.03, 0.0, 1.0),))
    if a is not None:
        parts.append(a)
    b = seg_at(amt_ratio, ((0.005, 0.06, 0.0, 1.0),))
    if b is not None:
        parts.append(b)
    return sum(parts) / len(parts) if parts else None


def dim_capital_fit(amt_over_cap: float | None) -> float | None:
    """盘口适配：竞价额/流通市值，过高疑对倒。"""
    return seg_at(amt_over_cap, ((0.001, 0.010, 0.0, 1.0),
                                 (0.010, 0.050, 1.0, 1.0),
                                 (0.050, _INF, 0.5, 0.5)))


def dim_turnover(v: float | None) -> float | None:
    """换手率：5~20% 最优，>30% 往往是出货。"""
    if v is None:
        return None
    return seg_at(v, ((0.0, 3.0, 0.0, 0.35), (3.0, 5.0, 0.35, 0.65),
                      (5.0, 20.0, 0.65, 1.0), (20.0, 30.0, 1.0, 0.65),
                      (30.0, _INF, 0.35, 0.35)))


def dim_theme_leader(is_leader: bool | None, highest: float | None,
                     streak: float | None) -> float | None:
    """题材龙头地位。无题材数据 → None（该维度缺席）。"""
    if is_leader is None and highest is None:
        return None
    if is_leader:
        return 1.0
    if highest is not None and streak is not None:
        return 0.8 if int(streak) >= int(highest) else 0.5
    if streak is not None and int(streak) >= 2:
        return 0.5
    return 0.25


def score_candidate(feat: dict[str, Any]) -> dict[str, Any]:
    """12 维加权总分。**缺席维度按剩余权重归一化**（与仓库 score_candidate 一致）。

    返回 {total_score, dims, available_weight, missing, notes}
    """
    raw: dict[str, float | None] = {
        "auction_strength": dim_auction_strength(feat.get("auction_volume_vs_yesterday")),
        "price_position": dim_price_position(feat.get("open_gap_pct")),
        "takeover": dim_takeover(feat.get("takeover_score")),
        "theme_heat": dim_theme_heat(feat.get("theme_heat")),
        "ladder": dim_ladder(feat.get("prev_limit_up_streak")),
        "sentiment": dim_sentiment(feat.get("market_temperature"),
                                   str(feat.get("market_stage") or "")),
        "seal_flow_ratio": dim_seal_flow(feat.get("prev_seal_to_float_ratio")),
        "auction_sentiment": dim_auction_sentiment(feat.get("auction_sentiment_score")),
        "previous_day": dim_previous_day(feat.get("prev_seal_to_float_ratio"),
                                         feat.get("auction_amount_ratio")),
        "capital_fit": dim_capital_fit(feat.get("auction_amount_over_cap")),
        "turnover": dim_turnover(feat.get("turnover_rate")),
        "theme_leader": dim_theme_leader(feat.get("theme_is_leader"),
                                         feat.get("theme_highest_ladder"),
                                         feat.get("prev_limit_up_streak")),
    }
    avail = {k: v for k, v in raw.items() if v is not None}
    missing = [k for k, v in raw.items() if v is None]
    wsum = sum(DIM_WEIGHTS[k] for k in avail)
    if wsum <= 0:
        return {"total_score": 0.0, "dims": raw, "available_weight": 0.0,
                "missing": missing, "notes": "所有维度均缺失"}
    base = sum(DIM_WEIGHTS[k] * v for k, v in avail.items()) / wsum * 100.0
    bonus = 0.0
    labels = feat.get("rush_labels") or []
    if "大量抢筹" in labels:
        bonus = 5.0
    elif "抢筹" in labels:
        bonus = 4.0
    return {"total_score": round(base + bonus, 2), "dims": raw,
            "available_weight": round(wsum, 1), "missing": missing, "bonus": bonus,
            "notes": f"有效权重 {wsum:.1f}/100，缺席 {len(missing)} 维"}


# ==============================================================================
# §3 纯逻辑：市场情绪周期（阈值取自 src/intraday/market_cycle.py）
# ==============================================================================
TH = {"limit_up_active": 30.0, "limit_up_hot": 60.0, "limit_up_dead": 15.0,
      "broken_rate_retreat": 0.60, "broken_rate_healthy": 0.30,
      "streak_main": 5, "streak_dead": 2, "streak_count_dead": 4,
      "big_loss_veto": 10, "limit_down_veto": 10}


def classify_stage(*, limit_up_count: int, broken_rate: float | None,
                   max_streak: int, streak2plus: int, big_loss_count: int,
                   limit_down_count: int) -> tuple[str, list[str]]:
    """计数 → 周期阶段。判定顺序刻意「风险优先」（退潮期尾巴常仍有 50+ 家涨停）。"""
    gates: list[str] = []
    if big_loss_count >= TH["big_loss_veto"]:
        gates.append(f"大面 {big_loss_count} 家 ≥ {TH['big_loss_veto']:.0f}（一票否决）")
    if limit_down_count > TH["limit_down_veto"]:
        gates.append(f"跌停 {limit_down_count} 家 > {TH['limit_down_veto']:.0f}（一票否决）")
    retreat = ((broken_rate is not None and broken_rate > TH["broken_rate_retreat"])
               or limit_up_count < TH["limit_up_dead"] or bool(gates))
    ice = (max_streak <= TH["streak_dead"] and streak2plus < TH["streak_count_dead"])
    if retreat and not ice:
        return "退潮期", gates
    if ice:
        return "冰点", gates
    if limit_up_count >= TH["limit_up_hot"] and max_streak >= TH["streak_main"]:
        if broken_rate is None or broken_rate < TH["broken_rate_healthy"]:
            return "主升期", gates
        return "高位震荡期", gates
    if max_streak >= TH["streak_main"]:
        return "高位震荡期", gates
    if limit_up_count >= TH["limit_up_active"]:
        return "发酵期", gates
    return "试错期", gates


def temperature_from(*, limit_up_count: int, broken_rate: float | None,
                     max_streak: int, limit_down_count: int) -> int:
    """做T环境温度 0~100。广度35 + 承接质量25 + 连板身位25 + 亏钱效应反向15。"""
    breadth = min(1.0, limit_up_count / TH["limit_up_hot"]) * 35.0
    quality = (12.5 if broken_rate is None else
               max(0.0, min(1.0, 1.0 - broken_rate / TH["broken_rate_retreat"])) * 25.0)
    height = min(1.0, max_streak / 7.0) * 25.0
    loss = max(0.0, min(1.0, 1.0 - limit_down_count / 15.0)) * 15.0
    return int(round(max(0.0, min(100.0, breadth + quality + height + loss))))


# ==============================================================================
# §4 纯逻辑：竞价特征（承接强度 / 跳空值 / 抢筹抢跑）
# ==============================================================================
@dataclass
class AuctionPoint:
    """一个竞价快照点。"""
    t: str            # HH:MM:SS
    price: float      # 虚拟/撮合价
    volume: float     # 累计竞价量（股）
    amount: float     # 累计竞价额（元）
    unmatched: float = 0.0    # 未匹配量（绝对值）
    direction: int = 0        # 未匹配方向：+1 买方剩余，-1 卖方剩余，0 平衡


def takeover_strength(series: list[AuctionPoint], *, pre_close: float | None = None,
                      min_points: int = TAKEOVER_MIN_POINTS) -> dict[str, Any]:
    """承接强度 0~100。

    取 9:20 后（不可撤单段）的价格斜率与未匹配量方向：
        score = clamp(50 + 斜率分×0.6 + 失衡分×0.4, 0, 100)
        斜率分 = clamp(slope%/3 × 50, -50, 50)
        失衡分 = (买剩余−卖剩余)/(买+卖) × 50
    """
    win = [p for p in series if TAKEOVER_START <= p.t <= "09:25:00" and p.price > 0]
    out: dict[str, Any] = {"score": None, "price_slope": None, "points": len(win),
                           "note": ""}
    if len(win) < max(1, min_points) or len(win) < 2:
        out["note"] = f"9:20 后仅 {len(win)} 个点（<{min_points}），不做承接判定"
        return out
    first, last = win[0].price, win[-1].price
    slope = (last / first - 1.0) * 100.0 if first > 0 else None
    out["price_slope"] = None if slope is None else round(slope, 4)
    slope_score = 0.0 if slope is None else max(-50.0, min(50.0, slope / 3.0 * 50.0))
    imbalance = 0.0
    p = win[-1]
    if p.direction > 0:
        buy, sell = p.unmatched, 0.0
    elif p.direction < 0:
        buy, sell = 0.0, p.unmatched
    else:
        buy = sell = 0.0
    if buy + sell > 0:
        imbalance = (buy - sell) / (buy + sell) * 50.0
    out["score"] = round(max(0.0, min(100.0, 50.0 + slope_score * 0.6
                                       + imbalance * 0.4)), 2)
    return out


def jump_gap(series: list[AuctionPoint], match_price: float | None
             ) -> tuple[float | None, dict[str, Any]]:
    """跳空值 = 09:25 正式撮合价 ÷ mean(09:24:40~09:24:55 撮合价)。

    ⚠️ 分子是 9:25 价、分母是窗口均价。写反会让「抢筹/抢跑」语义整体反转。
    """
    win = [p for p in series
           if JUMP_WINDOW_START <= p.t <= JUMP_WINDOW_END and p.price > 0]
    meta = {"points": len(win), "window_mean": None}
    if match_price is None or not win:
        return None, meta
    mean = sum(p.price for p in win) / len(win)
    meta["window_mean"] = round(mean, 4)
    if mean <= 0:
        return None, meta
    return round(match_price / mean, 4), meta


def rush_labels(*, jump: float | None, volume_vs_yesterday: float | None,
                volume_ratio_pct: float | None, open_gap_pct: float | None,
                prev_streak: float | None, code: str) -> tuple[list[str], list[str]]:
    """抢筹 / 大量抢筹 / 抢跑 标签。

    抢筹   : 跳空 > 1.01 且 今昨竞比 > 2
    大量抢筹: 抢筹 且 竞价量比 > 15%（或主板 >6% 且带抢筹 且量比 > 7%）
    抢跑   : 跳空 < 0.99 且 竞价量比 > 10%（三条豁免同时成立则不报）
    """
    labels: list[str] = []
    notes: list[str] = []
    is_main = code[:3] in ("600", "601", "603", "605", "000", "001", "002", "003")

    if jump is not None and jump > RUSH_JUMP_UP and (
            volume_vs_yesterday is not None
            and volume_vs_yesterday > RUSH_MIN_VS_YESTERDAY):
        labels.append("抢筹")
        notes.append(f"跳空 {jump:.4f}>{RUSH_JUMP_UP} 且今昨竞比 "
                     f"{volume_vs_yesterday:.2f}>{RUSH_MIN_VS_YESTERDAY} → 抢筹")
        heavy = (volume_ratio_pct is not None
                 and volume_ratio_pct > RUSH_HEAVY_VOL_PCT)
        if (is_main and open_gap_pct is not None and open_gap_pct > HEAVY_RUSH_GAP
                and volume_ratio_pct is not None
                and volume_ratio_pct > HEAVY_RUSH_VOL_PCT):
            heavy = True
        if heavy:
            labels.append("大量抢筹")
            notes.append(f"量比 {volume_ratio_pct}% 达标 → 大量抢筹")

    if jump is not None and jump < RUSH_JUMP_DOWN and (
            volume_ratio_pct is not None and volume_ratio_pct > RUSH_OUT_VOL_PCT):
        exempt = ((prev_streak is not None and prev_streak > RUSH_OUT_EXEMPT_STREAK)
                  and (volume_vs_yesterday is not None
                       and volume_vs_yesterday < RUSH_OUT_EXEMPT_VS_YEST)
                  and (open_gap_pct is not None
                       and open_gap_pct > RUSH_OUT_EXEMPT_GAP))
        if exempt:
            notes.append("命中抢跑三条件豁免，不报抢跑")
        else:
            labels.append("抢跑")
            notes.append(f"跳空 {jump:.4f}<{RUSH_JUMP_DOWN} 且量比 "
                         f"{volume_ratio_pct}%>{RUSH_OUT_VOL_PCT}% → 抢跑")
    return labels, notes


def compute_auction_sentiment(open_gaps: Iterable[float | None]) -> dict[str, Any]:
    """候选池 9:25 开盘涨幅 → 当日竞价情绪 0~1（池级因子）。"""
    vals = [float(g) for g in open_gaps if g is not None and g == g]
    if len(vals) < 3:
        return {"available": False, "score": None, "sample": len(vals)}
    avg = sum(vals) / len(vals)
    high_ratio = sum(1 for v in vals if v > 0) / len(vals)
    a = max(0.0, min(1.0, (avg - (-3.0)) / (5.0 - (-3.0))))
    b = max(0.0, min(1.0, (high_ratio - 0.25) / (0.75 - 0.25)))
    return {"available": True, "score": round(a * 0.7 + b * 0.3, 4),
            "avg_gap": round(avg, 3), "high_open_ratio": round(high_ratio, 3),
            "sample": len(vals)}


# ==============================================================================
# §5 QMT 数据适配层
# ==============================================================================
def log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        with RUN_LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def atomic_write(path: Path, payload: dict[str, Any] | str) -> None:
    """原子写：先写 .tmp 再改名。信号文件必须这么写 —— 否则对端可能读到半截 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = payload if isinstance(payload, str) else json.dumps(
        payload, ensure_ascii=False, indent=1)
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def to_qmt_code(code: str) -> str:
    """600000 / sh600000 / 600000.SH → 600000.SH"""
    c = code.strip().lower()
    if "." in c:
        num, _, mkt = c.partition(".")
        return f"{num}.{mkt.upper()}"
    if c[:2] in ("sh", "sz", "bj"):
        return f"{c[2:]}.{c[:2].upper()}"
    return f"{c}.SH" if c[:1] in ("6", "5", "9") else f"{c}.SZ"


def bare(code: str) -> str:
    return code.split(".")[0]


class QmtData:
    """行情适配：日线、合约信息、竞价快照。所有取数都走 get() 兜底，字段名容错。"""

    def __init__(self) -> None:
        from xtquant import xtdata          # 延迟导入，便于纯逻辑单测
        self.xt = xtdata

    # ---- 5.1 交易日 ----
    def trading_dates(self, start: str, end: str) -> list[str]:
        try:
            return list(self.xt.get_trading_dates("SH", start_time=start,
                                                  end_time=end))
        except Exception:                                        # noqa: BLE001
            return []

    # ---- 5.2 股票池 ----
    def main_board_codes(self) -> list[str]:
        """沪深主板 A 股（剔除创业板/科创板/北交所）。"""
        out: list[str] = []
        for sector in ("沪深A股", "上证A股", "深证A股"):
            try:
                out.extend(self.xt.get_stock_list_in_sector(sector))
            except Exception:                                    # noqa: BLE001
                continue
        seen, codes = set(), []
        for c in out:
            num = bare(c)
            if num in seen or len(num) != 6:
                continue
            if num[:3] in EXCLUDE_PREFIX or num[:1] in ("4", "8", "9"):
                continue
            if not (num.startswith(("600", "601", "603", "605"))
                    or num.startswith(("000", "001", "002", "003"))):
                continue
            seen.add(num)
            codes.append(to_qmt_code(num))
        return sorted(codes)

    # ---- 5.3 合约信息 ----
    def instrument(self, code: str) -> dict[str, Any]:
        try:
            return self.xt.get_instrument_detail(code) or {}
        except Exception:                                        # noqa: BLE001
            return {}

    # ---- 5.4 日线 ----
    def daily(self, codes: Sequence[str], end: str, count: int,
              fields: Sequence[str] = ("time", "open", "high", "low", "close",
                                       "volume", "amount", "preClose")) -> dict:
        """取截至 end 的最近 count 根日线。返回 {field: DataFrame(index=code)}。"""
        try:
            self.xt.download_history_data2(list(codes), "1d", "", end) \
                if hasattr(self.xt, "download_history_data2") else None
        except Exception:                                        # noqa: BLE001
            pass
        try:
            return self.xt.get_market_data_ex(
                list(fields), list(codes), period="1d", end_time=end,
                count=count, dividend_type="none", fill_data=False) or {}
        except Exception as e:                                   # noqa: BLE001
            log(f"[WARN] 取日线失败：{type(e).__name__} {e}")
            return {}

    # ---- 5.5 竞价快照 ----
    def full_tick(self, codes: Sequence[str]) -> dict[str, dict]:
        try:
            return self.xt.get_full_tick(list(codes)) or {}
        except Exception as e:                                   # noqa: BLE001
            log(f"[WARN] get_full_tick 失败：{type(e).__name__} {e}")
            return {}

    @staticmethod
    def tick_to_point(tick: dict[str, Any], t: str) -> AuctionPoint:
        """把 QMT tick 转成 AuctionPoint。字段名做了容错（不同版本可能不同）。"""
        def g(*names, default=0.0):
            for n in names:
                if n in tick and tick[n] is not None:
                    return tick[n]
            return default
        price = float(g("lastPrice", "open", default=0.0) or 0.0)
        volume = float(g("volume", "pvolume", default=0.0) or 0.0)
        amount = float(g("amount", default=0.0) or 0.0)
        # 未匹配量：level1 全推没有该字段；有 level2（l2quoteaux）时才有。
        unmatched = float(g("unmatchedVolume", "unmatched", default=0.0) or 0.0)
        direction = int(g("unmatchedDirection", "direction", default=0) or 0)
        return AuctionPoint(t=t, price=price, volume=volume, amount=amount,
                            unmatched=abs(unmatched), direction=direction)


def limit_up_price(pre_close: float, ratio: float = 0.10) -> float:
    """涨停价：前收 ×(1+幅度)，四舍五入到分（交易所口径，不用 Python 的银行家舍入）。"""
    return math.floor(pre_close * (1.0 + ratio) * 100.0 + 0.5) / 100.0


def limit_down_price(pre_close: float, ratio: float = 0.10) -> float:
    return math.ceil(pre_close * (1.0 - ratio) * 100.0 - 0.5) / 100.0


# ==============================================================================
# §6 选股主流程（前置筛选 → 一票否决 → 打分 → 排序）
# ==============================================================================
@dataclass
class Candidate:
    code: str
    name: str = ""
    prev_close: float = 0.0
    prev_streak: float = 0.0
    float_shares: float = 0.0
    prev_cap: float = 0.0
    prev_ma: float | None = None
    turnover_rate: float | None = None
    seal_to_float: float | None = None
    theme_name: str = ""
    theme_heat: float | None = None
    theme_is_leader: bool | None = None
    theme_highest_ladder: float | None = None
    # 9:25 定格后填充
    open: float = 0.0
    open_gap_pct: float | None = None
    auction_volume: float = 0.0
    auction_amount: float = 0.0
    auction_volume_ratio: float | None = None      # 今竞价量/昨全天量 ×100
    auction_volume_vs_yesterday: float | None = None  # 今竞价量/昨竞价量
    auction_amount_ratio: float | None = None      # 竞价额/昨日成交额
    auction_amount_over_cap: float | None = None   # 竞价额/流通市值
    takeover_score: float | None = None
    jump: float | None = None
    rush: list[str] = field(default_factory=list)
    feature: dict[str, Any] = field(default_factory=dict)
    scored: dict[str, Any] = field(default_factory=dict)
    veto: list[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        return float(self.scored.get("total_score") or 0.0)


def build_candidates(rows: list[dict[str, Any]]) -> tuple[list[Candidate], list[str]]:
    """前置筛选（9 条 + V3 追加 2 条）。rows 由 §10 的日线装配产出。"""
    notes: list[str] = []
    dropped: dict[str, int] = {}

    def drop(reason: str) -> None:
        dropped[reason] = dropped.get(reason, 0) + 1

    kept: list[Candidate] = []
    for r in rows:
        code = r["code"]
        num = bare(code)
        if num[:3] in EXCLUDE_PREFIX:
            drop("创业板/科创板")
            continue
        if not r.get("prev_limit_up"):
            drop("昨日未涨停")
            continue
        if r.get("is_st"):
            drop("ST")
            continue
        pc = r.get("prev_close") or 0.0
        if pc <= 0:
            drop("无昨收")
            continue
        if pc >= MAX_PREV_CLOSE:
            drop(f"昨收≥{MAX_PREV_CLOSE:g}元")
            continue
        ma = r.get("prev_ma")
        if ma is None:
            drop("无20日均价")
            continue
        if pc <= ma:
            drop("昨收未站上20日均价")
            continue
        cap = r.get("prev_cap") or 0.0
        if cap <= 0:
            drop("无流通市值")
            continue
        if not (CAP_MIN < cap < CAP_MAX):
            drop(f"流通市值∉({CAP_MIN/1e8:.0f},{CAP_MAX/1e8:.0f})亿")
            continue
        if CAP_EXCLUDE_MIN < cap < CAP_EXCLUDE_MAX:
            drop(f"V3剔除{CAP_EXCLUDE_MIN/1e8:.0f}~{CAP_EXCLUDE_MAX/1e8:.0f}亿")
            continue
        kept.append(Candidate(
            code=code, name=r.get("name") or "", prev_close=pc,
            prev_streak=float(r.get("prev_streak") or 0.0),
            float_shares=float(r.get("float_shares") or 0.0), prev_cap=cap,
            prev_ma=ma, turnover_rate=r.get("turnover_rate"),
            seal_to_float=r.get("seal_to_float"), theme_name=r.get("theme_name") or "",
            theme_heat=r.get("theme_heat"), theme_is_leader=r.get("theme_is_leader"),
            theme_highest_ladder=r.get("theme_highest_ladder")))
    for k, v in dropped.items():
        notes.append(f"{k} {v} 只")
    notes.insert(0, f"前置筛选：{len(rows)} → {len(kept)} 只")
    return kept, notes


def veto_candidate(c: Candidate, *, market_broken_rate: float | None,
                   market_allowed: bool) -> list[str]:
    """一票否决（10 条）。返回命中原因列表，空列表 = 通过。"""
    reasons: list[str] = []
    labels = c.rush
    exempt_rush = bool({"抢筹", "大量抢筹"} & set(labels))
    g = c.open_gap_pct
    if g is None:
        reasons.append("无开盘涨幅")
    else:
        if g < MIN_OPEN_GAP and not exempt_rush:
            reasons.append(f"开盘涨幅 {g:.2f}% 低于 {MIN_OPEN_GAP}%")
        if g > MAX_OPEN_GAP and "大量抢筹" not in labels:
            reasons.append(f"开盘涨幅 {g:.2f}% 高于 {MAX_OPEN_GAP}%")
    vr = c.auction_volume_ratio
    if vr is not None and vr > MAX_AUCTION_VOL_RATIO and not exempt_rush:
        reasons.append(f"竞价量比 {vr:.1f}% 异常放量（>{MAX_AUCTION_VOL_RATIO}%）")
    if "抢跑" in labels:
        reasons.append("竞价抢跑")
    if market_broken_rate is not None and market_broken_rate > MAX_BROKEN_RATE:
        reasons.append(f"市场炸板率 {market_broken_rate:.1%} 过高")
    if not market_allowed:
        reasons.append("情绪周期闸门关闭（退潮期/冰点不参与）")
    return reasons


def pick_top(cands: list[Candidate], *, top_n: int) -> list[Candidate]:
    ok = [c for c in cands if not c.veto and c.score >= SCORE_THRESHOLD]
    ok.sort(key=lambda c: -c.score)
    return ok[:top_n]


# ==============================================================================
# §7 状态持久化（持仓成本、买入日期、净值曲线 —— QMT 持仓不含买入日）
# ==============================================================================
class State:
    def __init__(self, path: Path = STATE_FILE) -> None:
        self.path = path
        self.data: dict[str, Any] = {"positions": {}, "equity": [], "last_day": "",
                                     "init_cash": INIT_CASH}
        if path.exists():
            try:
                self.data.update(json.loads(path.read_text(encoding="utf-8")))
            except Exception as e:                               # noqa: BLE001
                log(f"[WARN] 状态文件损坏，已忽略：{e}")

    @property
    def positions(self) -> dict[str, dict]:
        return self.data.setdefault("positions", {})

    def get(self, code: str) -> dict | None:
        return self.positions.get(bare(code))

    def put(self, code: str, **kw: Any) -> None:
        self.positions.setdefault(bare(code), {}).update(kw)

    def pop(self, code: str) -> None:
        self.positions.pop(bare(code), None)

    def record_equity(self, day: str, equity: float) -> None:
        eq = self.data.setdefault("equity", [])
        if eq and eq[-1]["day"] == day:
            eq[-1]["equity"] = equity
        else:
            eq.append({"day": day, "equity": equity})
        self.data["equity"] = eq[-400:]
        self.data["last_day"] = day

    def breaker_on(self, *, roll: int, drop: float, cur_equity: float) -> bool:
        """净值 < 过去 roll 日最高净值 ×(1−drop) → 熔断（只看历史，不含当日）。"""
        if roll <= 0:
            return False
        hist = [e["equity"] for e in self.data.get("equity", [])][-roll:]
        if len(hist) < roll:
            return False
        hi = max(hist)
        return hi > 0 and cur_equity < hi * (1.0 - drop)

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(self.path)


def log_trade(row: dict[str, Any]) -> None:
    header = ["time", "code", "name", "side", "price", "volume", "amount",
              "fee", "reason", "score"]
    new = not TRADE_LOG.exists()
    try:
        with TRADE_LOG.open("a", encoding="utf-8", newline="") as f:
            if new:
                f.write(",".join(header) + "\n")
            f.write(",".join(str(row.get(k, "")) for k in header) + "\n")
    except OSError:
        pass


# ==============================================================================
# §8 交易执行层（xttrader 封装）
# ==============================================================================
class Broker:
    """下单/查询封装。所有下单都先经过 _guard() 做数量与价格合法性检查。"""

    def __init__(self, trader: Any, account: Any, const: Any) -> None:
        self.t = trader
        self.acc = account
        self.c = const
        self.fills: dict[str, list[dict]] = {}

    # ---- 8.1 查询 ----
    def asset(self) -> Any:
        try:
            return self.t.query_stock_asset(self.acc)
        except Exception as e:                                   # noqa: BLE001
            log(f"[ERROR] 查资产失败：{e}")
            return None

    def positions(self) -> dict[str, Any]:
        try:
            return {bare(p.stock_code): p
                    for p in (self.t.query_stock_positions(self.acc) or [])
                    if getattr(p, "volume", 0) > 0}
        except Exception as e:                                   # noqa: BLE001
            log(f"[ERROR] 查持仓失败：{e}")
            return {}

    def equity(self) -> float:
        a = self.asset()
        if a is None:
            return 0.0
        return float(getattr(a, "total_asset", 0.0) or 0.0)

    def cash(self) -> float:
        a = self.asset()
        return float(getattr(a, "cash", 0.0) or 0.0) if a else 0.0

    # ---- 8.2 下单 ----
    def buy(self, code: str, shares: int, price: float, reason: str) -> int:
        shares = self._guard(code, shares, price)
        if shares <= 0:
            return -1
        return self._order(code, self.c.STOCK_BUY, shares, price, reason)

    def sell(self, code: str, shares: int, price: float, reason: str) -> int:
        shares = self._guard(code, shares, price)
        if shares <= 0:
            return -1
        return self._order(code, self.c.STOCK_SELL, shares, price, reason)

    def _guard(self, code: str, shares: int, price: float) -> int:
        """数量/价格合法性校验。**不在这里处理 DRY_RUN** —— 统一放到 _order，
        否则 dry-run 下会绕过成交回报、状态也不会更新，空跑就失去意义了。"""
        shares = int(shares // LOT * LOT)
        if shares <= 0 or price <= 0:
            return 0
        return shares

    def _order(self, code: str, side: int, shares: int, price: float,
               reason: str) -> int:
        if DRY_RUN:
            log(f"  [DRY-RUN] 模拟{'买入' if side == self.c.STOCK_BUY else '卖出'} "
                f"{code} {shares}股 限价{price:.3f} 原因={reason}")
            return 0
        try:
            oid = self.t.order_stock(self.acc, to_qmt_code(code), side, shares,
                                     self.c.FIX_PRICE, round(price, 2),
                                     "auction_v3", reason[:40])
            log(f"  下单 {'买入' if side == self.c.STOCK_BUY else '卖出'} "
                f"{code} {shares}股 限价{price:.3f} oid={oid} 原因={reason}")
            return int(oid) if oid is not None else -1
        except Exception as e:                                   # noqa: BLE001
            log(f"[ERROR] 下单异常 {code}：{type(e).__name__} {e}")
            return -1

    def cancel_all(self) -> None:
        if DRY_RUN:
            return
        try:
            for o in (self.t.query_stock_orders(self.acc) or []):
                if getattr(o, "order_status", 0) in (48, 49, 50, 55):  # 未成交态
                    self.t.cancel_order_stock(self.acc, o.order_id)
        except Exception as e:                                   # noqa: BLE001
            log(f"[WARN] 撤单失败：{e}")


# ==============================================================================
# §9 卖出与风控（对应回测 8 条规则，严格优先级）
# ==============================================================================
def day_change(code: str, day: str, close: float, data: QmtData) -> float | None:
    """当日涨跌幅：close/前收 − 1。前收取日线 preClose。"""
    try:
        d = data.daily([to_qmt_code(code)], day, 2)
        pc = (d.get("preClose") or {}).get(to_qmt_code(code))
        if pc is None or len(pc) == 0:
            return None
        pre = float(pc.iloc[-1])
        return (close / pre - 1.0) if pre > 0 else None
    except Exception:                                            # noqa: BLE001
        return None


def ma_of(code: str, day: str, window: int, data: QmtData,
          last_price: float | None = None) -> float | None:
    """N 日收盘均线。

    ⚠️ 规则④判定时点收盘价还没出来，所以：若日线里最后一根 bar 不是今天
    （盘中 QMT 通常不会写入未收盘的日线），就用 `last_price`（= 当前价，
    即收盘价的近似）补上今天这根。否则均线会整整体前移一天。
    """
    try:
        d = data.daily([to_qmt_code(code)], day, window + 6)
        df = (d.get("close") or {}).get(to_qmt_code(code))
        tm = (d.get("time") or {}).get(to_qmt_code(code))
        if df is None or len(df) == 0:
            return None
        vals = [float(x) for x in df.tolist()]
        if tm is not None and len(tm) > 0:
            try:
                last_ts = int(tm.tolist()[-1])
                last_day = datetime.fromtimestamp(last_ts / 1000.0).strftime("%Y%m%d")
                if last_day < day and last_price and last_price > 0:
                    vals.append(float(last_price))
            except (TypeError, ValueError, OSError):
                pass
        if len(vals) < window:
            return None
        return sum(vals[-window:]) / window
    except Exception:                                            # noqa: BLE001
        return None


@dataclass
class SellAction:
    code: str
    qty_ratio: float      # 1.0 = 全部，0.5 = 一半，0.3 = 三成
    reason: str
    priority: int


def plan_sells(*, code: str, name: str, held_days: int, cost: float,
               price: float, is_sealed: bool, ma_exit: float | None,
               d_chg: float | None, market_high: bool,
               rush: Sequence[str]) -> SellAction | None:
    """按回测 V3 的优先级判定卖出。held_days：买入当日=0（T+1 不可卖）。

    注意：这里传入的 `price` 是「收盘价近似值」（实盘为 14:56 价）。
    """
    if held_days <= 0 or cost <= 0 or price <= 0:
        return None
    ret = price / cost - 1.0
    # ① 最高连板 ≥7 且盈利 且竞价抢跑 → 全部清仓
    if market_high and ret > 0 and "抢跑" in rush:
        return SellAction(code, 1.0, "最高连板≥7且盈利且竞价抢跑 清仓", 1)
    # ② 收盘亏损 ≥5%
    if ret <= -STOP_CLOSE_PCT:
        return SellAction(code, 1.0, f"收盘亏损{STOP_CLOSE_PCT:.0%} 清仓", 2)
    # ③ 当日收盘跌幅 ≥7%
    if d_chg is not None and d_chg <= DAY_DROP_PCT:
        return SellAction(code, 1.0, f"当日收盘跌幅≥{abs(DAY_DROP_PCT):.0%} 清仓", 3)
    # ④ 收盘跌破 5 日线
    if ma_exit is not None and price < ma_exit:
        return SellAction(code, 1.0, f"收盘破{MA_EXIT_WINDOW}日线 清仓", 4)
    # ⑤ 持有满 10 日
    if held_days >= MAX_HOLD_DAYS:
        return SellAction(code, 1.0, f"持有满{MAX_HOLD_DAYS}日 清仓", 5)
    # ⑥ 收益 >10% 且当日跌 >2% → 卖 30%
    if ret > TRIM_RET and d_chg is not None and d_chg < TRIM_DAY:
        return SellAction(code, TRIM_QTY, "收益>10%且当日跌>2% 卖30%", 6)
    # ⑦ 买入次日收盘未涨停 → 卖 50%
    if held_days == 1 and not is_sealed:
        return SellAction(code, T1_SELL_QTY, "次日收盘未涨停 卖50%", 7)
    return None


# ==============================================================================
# §10 主策略：时间状态机
# ==============================================================================
class AuctionV3:
    def __init__(self, data: QmtData, broker: Broker, state: State) -> None:
        self.d = data
        self.b = broker
        self.st = state
        self.series: dict[str, list[AuctionPoint]] = {}
        self.codes: list[str] = []
        self.today = ""
        self.market: dict[str, Any] = {}
        self.themes: dict[str, dict] = {}
        self.picked: list[Candidate] = []
        self.watch: list[str] = []
        self.held_codes: list[str] = []

    # ---------------- 10.1 盘前：日线装配 + 市场情绪 ----------------
    def load_theme_csv(self) -> None:
        if not THEME_CSV_ENABLED or not THEME_CSV.exists():
            log("题材数据：未提供（题材热度/龙头/封流比三维度将缺席）")
            return
        try:
            import csv
            with THEME_CSV.open(encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    num = bare(str(row.get("code") or "").strip())
                    if not num:
                        continue
                    self.themes[num] = {
                        "theme_name": row.get("theme_name") or "",
                        "theme_heat": _f(row.get("theme_heat")),
                        "theme_is_leader": _b(row.get("is_leader")),
                        "theme_highest_ladder": _f(row.get("highest_ladder")),
                        "seal_to_float": _f(row.get("seal_to_float")),
                    }
            log(f"题材数据：已载入 {len(self.themes)} 只（{THEME_CSV.name}）")
        except Exception as e:                                   # noqa: BLE001
            log(f"[WARN] 题材数据读取失败：{type(e).__name__} {e}")

    def prepare(self) -> None:
        """盘前：全市场日线 → 昨日涨停/连板/MA20/流通市值 + 市场情绪。"""
        self.today = datetime.now().strftime("%Y%m%d")
        self.load_theme_csv()
        codes = self.d.main_board_codes()
        log(f"主板股票池：{len(codes)} 只")
        if not codes:
            log("[ERROR] 股票池为空 —— 请确认 MiniQMT 已登录且板块数据已下载")
            return
        # 市场情绪需要全市场涨停/跌停统计，这里直接用全池日线算
        need = MA_WINDOW + 12
        daily = self.d.daily(codes, self.today, need)
        rows = self._assemble(codes, daily)
        self.market = self._market_from(rows)
        self.st.data["market"] = {"day": self.today, **self.market}
        self.st.save()
        log(f"市场情绪：{self.market.get('stage')} 温度 "
            f"{self.market.get('temperature')} 涨停 {self.market.get('limit_up_count')} "
            f"炸板率 {self.market.get('broken_rate')}")
        cands, notes = build_candidates(rows)
        log("前置筛选：" + "；".join(notes))
        self._pre = cands
        # 观察列表 = 候选池 + **当前持仓**。持仓必须一起采集竞价序列，
        # 因为卖出规则①（最高连板≥7 且盈利 且竞价抢跑 → 清仓）需要当天
        # 9:25 的「抢跑」标签，而卖出判定发生在 14:56 —— 只能早上算好存起来。
        held = list(self.b.positions().keys())
        watch = sorted({c.code for c in cands} | {to_qmt_code(h) for h in held})
        self.codes = [c.code for c in cands]
        self.watch = watch
        self.held_codes = held
        log(f"候选池：{len(self.codes)} 只；观察列表（含持仓）：{len(watch)} 只")

    def _assemble(self, codes: Sequence[str], daily: dict) -> list[dict]:
        """把 get_market_data_ex 的 DataFrame 组装成逐只的行。"""
        rows: list[dict] = []
        close_map = daily.get("close") or {}
        vol_map = daily.get("volume") or {}
        pre_map = daily.get("preClose") or {}
        high_map = daily.get("high") or {}
        for code in codes:
            try:
                df = close_map.get(code)
                if df is None or len(df) < MA_WINDOW + 2:
                    continue
                closes = [float(x) for x in df.tolist()]
                prev_close = closes[-1]
                if prev_close <= 0:
                    continue
                detail = self.d.instrument(code)
                flt = float(detail.get("FloatVolume")
                            or detail.get("FloatVol") or 0.0)
                name = str(detail.get("InstrumentName") or "")
                if flt <= 0:
                    continue
                # 连板天数：从昨日往前数「收盘 ≥ 涨停价」
                streak = 0.0
                for i in range(len(closes) - 1, 0, -1):
                    p = closes[i - 1]
                    if p <= 0:
                        break
                    if closes[i] >= limit_up_price(p) - 0.001:
                        streak += 1
                    else:
                        break
                ma = sum(closes[-MA_WINDOW:]) / MA_WINDOW
                vol = vol_map.get(code)
                yday_vol = float(vol.tolist()[-1]) if vol is not None else 0.0
                turnover = (yday_vol / flt * 100.0) if flt > 0 else None
                hi = high_map.get(code)
                yday_high = float(hi.tolist()[-1]) if hi is not None else 0.0
                th = self.themes.get(bare(code), {})
                rows.append({
                    "code": code, "name": name, "prev_close": prev_close,
                    "prev_limit_up": streak >= 1.0, "prev_streak": streak,
                    "is_st": "ST" in name.upper(),
                    "float_shares": flt, "prev_cap": flt * prev_close,
                    "prev_ma": ma, "turnover_rate": turnover,
                    "yday_volume": yday_vol, "yday_high": yday_high,
                    "seal_to_float": th.get("seal_to_float"),
                    "theme_name": th.get("theme_name", ""),
                    "theme_heat": th.get("theme_heat"),
                    "theme_is_leader": th.get("theme_is_leader"),
                    "theme_highest_ladder": th.get("theme_highest_ladder"),
                })
            except Exception:                                    # noqa: BLE001
                continue
        return rows

    @staticmethod
    def _market_from(rows: list[dict]) -> dict[str, Any]:
        """全市场涨停/炸板/跌停统计 → 周期阶段与温度。

        口径：全部基于**昨日**已收盘的日线（前收 = 昨日收盘价）。
        涨停：昨日收盘 ≥ 涨停价；炸板：昨日最高 ≥ 涨停价 但收盘 < 涨停价；
        跌停：昨日收盘 ≤ 跌停价。
        """
        lu = [r for r in rows if r["prev_limit_up"]]
        broken = limit_down = 0
        for r in rows:
            pc, yh = r["prev_close"], r.get("yday_high") or 0.0
            if pc <= 0:
                continue
            if pc <= limit_down_price(pc) + 0.001:
                limit_down += 1
            if yh >= limit_up_price(pc) - 0.001 and not r["prev_limit_up"]:
                broken += 1
        denom = len(lu) + broken
        br = (broken / denom) if denom else None
        max_streak = max((r["prev_streak"] for r in lu), default=0)
        streak2 = sum(1 for r in lu if r["prev_streak"] >= 2)
        stage, gates = classify_stage(limit_up_count=len(lu), broken_rate=br,
                                      max_streak=int(max_streak), streak2plus=streak2,
                                      big_loss_count=0, limit_down_count=limit_down)
        temp = temperature_from(limit_up_count=len(lu), broken_rate=br,
                                max_streak=int(max_streak),
                                limit_down_count=limit_down)
        return {"stage": stage, "temperature": temp, "broken_rate": br,
                "limit_up_count": len(lu), "limit_down_count": limit_down,
                "broken_count": broken, "max_streak": int(max_streak),
                "streak2plus": streak2, "gates": gates,
                "allowed": stage not in ("退潮期", "冰点") and not gates,
                "universe": len(rows)}

    # ---------------- 10.2 竞价采集（9:15-9:25） ----------------
    def collect(self) -> None:
        """轮询 get_full_tick 构建 9:15-9:25 竞价序列（观察列表含持仓）。"""
        targets = getattr(self, "watch", None) or self.codes
        log(f"开始采集竞价序列（{len(targets)} 只，间隔 {POLL_INTERVAL:g}s）")
        end = datetime.now().replace(hour=9, minute=25, second=5, microsecond=0)
        while datetime.now() < end:
            tick = self.d.full_tick(targets)
            t = datetime.now().strftime("%H:%M:%S")
            for code, tk in tick.items():
                p = self.d.tick_to_point(tk, t)
                if p.price > 0:
                    self.series.setdefault(bare(code), []).append(p)
            time.sleep(POLL_INTERVAL)
        got = sum(len(v) for v in self.series.values())
        log(f"采集结束：{len(self.series)} 只有序列，共 {got} 个点")

    # ---------------- 10.3 9:25 定格 → 打分选股 ----------------
    def select(self) -> list[Candidate]:
        tick = self.d.full_tick(self.codes)
        for c in self._pre:
            tk = tick.get(to_qmt_code(c.code)) or {}
            open_px = float(tk.get("open") or tk.get("lastPrice") or 0.0)
            if open_px <= 0 and self.series.get(c.code):
                open_px = self.series[c.code][-1].price
            c.open = open_px
            if open_px > 0 and c.prev_close > 0:
                c.open_gap_pct = (open_px / c.prev_close - 1.0) * 100.0
            ser = self.series.get(c.code) or []
            if ser:
                c.auction_volume = ser[-1].volume
                c.auction_amount = ser[-1].amount
            if c.auction_volume > 0 and c.float_shares > 0:
                c.auction_amount_over_cap = (c.auction_amount / c.float_shares
                                             / c.prev_close) if c.prev_close else None
            tk_amt = float(tk.get("amount") or 0.0)
            if tk_amt > 0:
                c.auction_amount = tk_amt
                c.auction_amount_over_cap = (tk_amt / c.prev_cap
                                             if c.prev_cap > 0 else None)
            yday_vol = 0.0
            try:
                vol = self.d.daily([to_qmt_code(c.code)], self.today, 2).get("volume")
                df = (vol or {}).get(to_qmt_code(c.code))
                yday_vol = float(df.tolist()[-1]) if df is not None else 0.0
            except Exception:                                    # noqa: BLE001
                pass
            if c.auction_volume > 0 and yday_vol > 0:
                c.auction_volume_ratio = c.auction_volume / yday_vol * 100.0
            tk_y = self._yesterday_auction_volume(c.code)
            if c.auction_volume > 0 and tk_y > 0:
                c.auction_volume_vs_yesterday = c.auction_volume / tk_y
            if c.auction_amount > 0 and yday_vol > 0 and c.prev_close > 0:
                yday_amt = yday_vol * c.prev_close
                c.auction_amount_ratio = c.auction_amount / yday_amt
            c.takeover_score = takeover_strength(ser).get("score")
            c.jump, _ = jump_gap(ser, open_px or None)
            c.rush, rush_notes = rush_labels(
                jump=c.jump, volume_vs_yesterday=c.auction_volume_vs_yesterday,
                volume_ratio_pct=c.auction_volume_ratio,
                open_gap_pct=c.open_gap_pct, prev_streak=c.prev_streak,
                code=c.code)

        # 池级竞价情绪
        sent = compute_auction_sentiment([c.open_gap_pct for c in self._pre])
        log(f"竞价情绪：{sent}")
        # 持仓股的当日抢跑标签 → 存 state，供 14:56 的卖出规则①使用
        self._stash_held_rush()
        # 打分
        for c in self._pre:
            c.feature = {
                "auction_volume_vs_yesterday": c.auction_volume_vs_yesterday,
                "open_gap_pct": c.open_gap_pct, "takeover_score": c.takeover_score,
                "theme_heat": c.theme_heat, "prev_limit_up_streak": c.prev_streak,
                "market_temperature": self.market.get("temperature"),
                "market_stage": self.market.get("stage"),
                "prev_seal_to_float_ratio": c.seal_to_float,
                "auction_sentiment_score": sent.get("score"),
                "auction_amount_ratio": c.auction_amount_ratio,
                "auction_amount_over_cap": c.auction_amount_over_cap,
                "turnover_rate": c.turnover_rate,
                "theme_is_leader": c.theme_is_leader,
                "theme_highest_ladder": c.theme_highest_ladder,
                "rush_labels": c.rush,
            }
            c.scored = score_candidate(c.feature)
            c.veto = veto_candidate(c, market_broken_rate=self.market.get("broken_rate"),
                                    market_allowed=bool(self.market.get("allowed")))
        self._report()
        top_n = MAX_BUY_HIGH if self.market.get("max_streak", 0) >= HIGH_STREAK \
            else MAX_BUY_PER_DAY
        return pick_top(self._pre, top_n=top_n)

    def _yesterday_auction_volume(self, code: str) -> float:
        """昨日竞价量：用 1m 线 09:25 那根 bar 的成交量近似。"""
        try:
            d = self.d.xt.get_market_data_ex(
                ["volume", "time"], [to_qmt_code(code)], period="1m",
                end_time=self.today, count=600, dividend_type="none")
            df = (d.get("volume") or {}).get(to_qmt_code(code))
            tm = (d.get("time") or {}).get(to_qmt_code(code))
            if df is None or tm is None:
                return 0.0
            vs, ts = df.tolist(), tm.tolist()
            for i in range(len(ts) - 1, -1, -1):
                s = datetime.fromtimestamp(ts[i] / 1000.0)
                if s.strftime("%Y%m%d") < self.today and \
                        s.strftime("%H:%M") == "09:25":
                    return float(vs[i])
        except Exception:                                        # noqa: BLE001
            pass
        return 0.0

    def _stash_held_rush(self) -> None:
        """算持仓股今日 9:25 的抢筹/抢跑标签并存进 state。

        卖出规则①（最高连板≥7 且盈利 且竞价抢跑 → 清仓）的「抢跑」必须在
        9:25 定格时算 —— 14:56 已经拿不到竞价序列了。买点不一定在候选池里
        （可能是前几天买的），所以持仓单独算一遍。
        """
        if not self.held_codes:
            return
        tick = self.d.full_tick([to_qmt_code(c) for c in self.held_codes])
        out: dict[str, list[str]] = {}
        for code in self.held_codes:
            qc = to_qmt_code(code)
            tk = tick.get(qc) or {}
            ser = self.series.get(bare(code)) or []
            px = float(tk.get("open") or tk.get("lastPrice") or 0.0)
            if px <= 0 and ser:
                px = ser[-1].price
            st = self.st.get(code) or {}
            prev_close = float(st.get("prev_close") or tk.get("lastClose") or 0.0)
            gap = ((px / prev_close - 1.0) * 100.0) if (px > 0 and prev_close > 0) else None
            jump, _ = jump_gap(ser, px or None)
            vr = None
            av = ser[-1].volume if ser else 0.0
            try:
                vol = self.d.daily([qc], self.today, 2).get("volume")
                df = (vol or {}).get(qc)
                yv = float(df.tolist()[-1]) if df is not None else 0.0
                if av > 0 and yv > 0:
                    vr = av / yv * 100.0
            except Exception:                                    # noqa: BLE001
                pass
            labels, _ = rush_labels(jump=jump, volume_vs_yesterday=None,
                                    volume_ratio_pct=vr, open_gap_pct=gap,
                                    prev_streak=float(st.get("streak") or 0.0),
                                    code=bare(code))
            out[bare(code)] = labels
            self.st.put(code, prev_close=prev_close, today_jump=jump,
                        today_rush=labels)
        self.st.data["today_rush"] = {"day": self.today, "labels": out}
        self.st.save()
        if any(out.values()):
            log(f"持仓股当日标签：{ {k: v for k, v in out.items() if v} }")

    def _held_rush(self, code: str) -> list[str]:
        """取今天早上存下的持仓抢跑标签（卖出规则①用）。"""
        blk = self.st.data.get("today_rush") or {}
        if blk.get("day") != self.today:
            return []
        return list((blk.get("labels") or {}).get(bare(code)) or [])

    def _report(self) -> None:
        rows = sorted(self._pre, key=lambda c: -c.score)
        log("─" * 96)
        log(f"{'代码':<9}{'名称':<10}{'总分':>7}{'开盘%':>8}{'承接':>7}{'量比%':>8}"
            f"{'连板':>5}{'跳空':>8}  判定")
        for c in rows[:15]:
            tag = "入选" if (not c.veto and c.score >= SCORE_THRESHOLD) else (
                "否决" if c.veto else "低于门槛")
            extra = ("｜" + c.veto[0][:34]) if c.veto else ""
            log(f"{c.code:<9}{c.name[:8]:<10}{c.score:>7.2f}"
                f"{(c.open_gap_pct or 0):>8.2f}{(c.takeover_score or 0):>7.1f}"
                f"{(c.auction_volume_ratio or 0):>8.1f}{c.prev_streak:>5.0f}"
                f"{(c.jump or 0):>8.4f}  {tag}{extra}")
        log("─" * 96)

    # ---------------- 10.4 买入 ----------------
    def do_buy(self, picks: list[Candidate]) -> None:
        if not picks:
            log("今日无标的入选")
            return
        eq = self.b.equity() or self.st.data.get("init_cash", INIT_CASH)
        pos = self.b.positions()
        cash = self.b.cash()
        pct = WEIGHT_HIGH if self.market.get("max_streak", 0) >= HIGH_STREAK \
            else WEIGHT_BASE
        mv_total = sum(float(getattr(p, "market_value", 0) or 0)
                       for p in pos.values())
        for c in picks:
            mult = RUSH_WEIGHT_MULT if "抢筹" in c.rush else 1.0
            held = pos.get(c.code)
            if held is not None:
                cur_val = float(getattr(held, "market_value", 0) or 0)
                budget = cur_val * ADD_RATIO * mult
            else:
                budget = eq * pct * mult
            cap = eq * MAX_WEIGHT_SINGLE
            already = float(getattr(held, "market_value", 0) or 0) if held else 0.0
            budget = min(budget, max(0.0, cap - already), cash)
            shares = int(budget / c.open // LOT * LOT) if c.open > 0 else 0
            if shares <= 0:
                log(f"  {c.code} 预算不足，跳过")
                continue
            px = min(limit_up_price(c.prev_close), c.open * (1 + BUY_SLIP))
            oid = self.b.buy(c.code, shares, px, f"竞价入选 {c.score:.1f}分")
            if oid >= 0 or DRY_RUN:
                cash -= shares * px
                self.st.put(c.code, buy_day=self.today, cost=c.open,
                            shares=shares, name=c.name, score=c.score)
                log_trade({"time": datetime.now().strftime("%H:%M:%S"),
                           "code": c.code, "name": c.name, "side": "BUY",
                           "price": round(px, 3), "volume": shares,
                           "amount": round(shares * px, 2),
                           "fee": round(commission(shares * px), 2),
                           "reason": "竞价入选", "score": c.score})
        self.st.save()

    # ---------------- 10.5 收盘卖出（14:56:30 判定，参与收盘集合竞价） ----------------
    def do_sell(self) -> None:
        # 下午单独跑卖出时，沿用早盘存下的市场情绪快照（避免重下全市场日线）
        if not self.market:
            self.today = datetime.now().strftime("%Y%m%d")
            blk = self.st.data.get("market") or {}
            if blk.get("day") == self.today:
                self.market = blk
                log(f"沿用今日早盘市场情绪快照：{blk.get('stage')} "
                    f"温度 {blk.get('temperature')} 最高连板 {blk.get('max_streak')}")
            else:
                log("[WARN] 无今日市场情绪快照 → 规则①（最高连板≥7）本次不生效；"
                    "建议早上先跑一次 --buy-only 生成快照")
        pos = self.b.positions()
        if not pos:
            log("无持仓，跳过卖出判定")
            return
        held_codes = list(pos.keys())
        live = self.d.full_tick([to_qmt_code(c) for c in held_codes])
        actions: list[SellAction] = []
        for code, p in pos.items():
            st = self.st.get(code) or {}
            cost = float(st.get("cost") or getattr(p, "avg_price", 0)
                         or getattr(p, "open_price", 0) or 0)
            buy_day = str(st.get("buy_day") or "")
            if not buy_day:
                if not ADOPT_UNKNOWN_POSITIONS:
                    log(f"  {code} 非本策略持仓（无状态记录），跳过")
                    continue
                buy_day = self.today
            held = self._held_days(buy_day)
            tk = live.get(to_qmt_code(code)) or {}
            px = float(tk.get("lastPrice") or tk.get("open") or 0.0)
            if px <= 0:
                continue
            pc = float(tk.get("lastClose") or 0.0) or None
            d_chg = (px / pc - 1.0) if pc else None
            up = limit_up_price(pc) if pc else None
            sealed = bool(up and px >= up - 0.001)
            act = plan_sells(
                code=code, name=str(st.get("name") or code), held_days=held,
                cost=cost, price=px, is_sealed=sealed,
                ma_exit=ma_of(code, self.today, MA_EXIT_WINDOW, self.d,
                              last_price=px), d_chg=d_chg,
                market_high=self.market.get("max_streak", 0) >= HIGH_STREAK,
                rush=self._held_rush(code))
            if act:
                actions.append(act)
        actions.sort(key=lambda a: a.priority)
        for a in actions:
            p = pos[a.code]
            can_use = float(getattr(p, "can_use_volume", 0) or 0)
            qty = int(can_use * a.qty_ratio // LOT * LOT)
            if qty <= 0:
                log(f"  {a.code} 可卖 {can_use:.0f} 股，不足一手，跳过（{a.reason}）")
                continue
            tk = live.get(to_qmt_code(a.code)) or {}
            px = float(tk.get("lastPrice") or 0.0)
            low_limit = limit_down_price(float(tk.get("lastClose") or px))
            sell_px = max(low_limit, px * (1 - SELL_SLIP))
            oid = self.b.sell(a.code, qty, sell_px, a.reason)
            if oid >= 0 or DRY_RUN:
                if a.qty_ratio >= 1.0:
                    self.st.pop(a.code)
                log_trade({"time": datetime.now().strftime("%H:%M:%S"), "code": a.code,
                           "name": str(st.get("name") or ""), "side": "SELL",
                           "price": round(sell_px, 3),
                           "volume": qty, "amount": round(qty * sell_px, 2),
                           "fee": round(commission(qty * sell_px)
                                        + qty * sell_px * STAMP_TAX_RATE, 2),
                           "reason": a.reason, "score": ""})
        self.st.save()

    def _held_days(self, buy_day: str) -> int:
        """买入日至今的交易日数（买入当日 = 0）。"""
        try:
            dates = self.d.trading_dates(buy_day, self.today)
            days = [d for d in dates if str(d)[:8] <= self.today]
            if not days:
                return 0
            return max(0, len(days) - 1)
        except Exception:                                        # noqa: BLE001
            return 0

    # ---------------- 10.6 净值记录与熔断 ----------------
    def check_breaker(self) -> bool:
        if DD_BREAKER_ROLL <= 0:
            return False
        cur = self.b.equity()
        on = self.st.breaker_on(roll=DD_BREAKER_ROLL, drop=DD_BREAKER_DROP,
                                cur_equity=cur)
        if on:
            log(f"⚠️ 净值熔断触发：当前 {cur:,.0f} 低于 {DD_BREAKER_ROLL} 日高点 "
                f"{1 - DD_BREAKER_DROP:.0%} → 今日停止开仓")
        return on


def commission(amount: float) -> float:
    return max(amount * COMMISSION_RATE, COMMISSION_MIN)


def _f(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _b(v: Any) -> bool | None:
    if v is None or v == "":
        return None
    return str(v).strip().lower() in ("1", "true", "yes", "y", "是")


# ==============================================================================
# §11 入口
# ==============================================================================
class TraderCallback:
    """交易回调。动态继承 XtQuantTraderCallback，避免纯逻辑单测时导入 xtquant。"""

    def __new__(cls, *a, **kw):                                  # noqa: D102
        from xtquant.xttrader import XtQuantTraderCallback
        base = XtQuantTraderCallback

        class _CB(base):                                         # type: ignore
            def on_disconnected(self):
                log("[回调] 连接断开 —— 请检查 MiniQMT 是否在运行")

            def on_stock_order(self, order):
                log(f"[回调] 委托 {order.stock_code} 状态 {order.order_status}")

            def on_stock_trade(self, trade):
                log(f"[回调] 成交 {trade.stock_code} {trade.traded_volume}股 "
                    f"@{trade.traded_price}")

            def on_order_error(self, e):
                log(f"[回调] ❌ 委托失败 id={e.order_id} "
                    f"err={e.error_id} {e.error_msg}")

            def on_cancel_error(self, e):
                log(f"[回调] ❌ 撤单失败 id={e.order_id} {e.error_msg}")

        return _CB()


def connect(account_id: str, mini_path: str, session_id: int):
    from xtquant import xtconstant
    from xtquant.xttrader import XtQuantTrader
    from xtquant.xttype import StockAccount
    trader = XtQuantTrader(mini_path, session_id)
    trader.register_callback(TraderCallback())
    trader.start()
    r = trader.connect()
    if r != 0:
        raise RuntimeError(f"连接 MiniQMT 失败，返回码 {r}（请确认客户端已登录）")
    acc = StockAccount(account_id)
    s = trader.subscribe(acc)
    if s != 0:
        raise RuntimeError(f"订阅账号失败，返回码 {s}")
    log(f"已连接 MiniQMT，账号 {account_id}，会话 {session_id}")
    return trader, acc, xtconstant


def run_once(strategy: AuctionV3) -> None:
    """一整套盘前→竞价→选股→买入流程（卖出由 --sell-only / 常驻模式负责）。"""
    now = datetime.now()
    if now.strftime("%H:%M") > "09:15":
        log(f"[WARN] 当前 {now:%H:%M} 已过 9:15，竞价序列采集不完整，"
            f"承接强度/跳空值将缺失（对应维度会自动从权重中剔除）")
    strategy.prepare()
    if not strategy.codes:
        log("候选池为空，今日不交易")
        return
    strategy.collect()
    picks = strategy.select()
    if strategy.check_breaker():
        log("熔断生效 → 今日不开仓")
        picks = []
    strategy.do_buy(picks)


def resolve_phase(args) -> str:
    """决定本次进程该做哪一段。

    设计成**两次独立调用**而不是一个常驻进程 —— 常驻进程一旦崩溃/断连，
    下午的卖出席位就没人管了，风险不对称。建议用两个计划任务：
        09:05  → 选股并买入
        14:52  → 卖出判定（参与 14:57-15:00 收盘集合竞价）
    """
    if args.sell_only:
        return "sell"
    if args.buy_only:
        return "buy"
    if args.calibrate:
        return "calibrate"
    hm = datetime.now().strftime("%H:%M")
    if hm < "09:30":
        return "buy"
    if hm >= "14:50":
        return "sell"
    return "idle"


def main(argv: Sequence[str] | None = None) -> int:
    global DRY_RUN
    ap = argparse.ArgumentParser(description="集合竞价选股 V3 · QMT 实盘策略")
    ap.add_argument("--account", default=os.environ.get("QMT_ACCOUNT", ""),
                    help="资金账号")
    ap.add_argument("--mini-path", default=os.environ.get("QMT_MINI_PATH", ""),
                    help=r"MiniQMT 的 userdata_mini 路径，如 "
                         r"D:\东莞证券QMT\userdata_mini")
    ap.add_argument("--session-id", type=int, default=20260919)
    ap.add_argument("--live", action="store_true", help="实盘下单（默认 dry-run）")
    ap.add_argument("--sell-only", action="store_true",
                    help="只执行卖出判定（14:50 后跑）")
    ap.add_argument("--buy-only", action="store_true",
                    help="只执行选股买入（09:10 前跑）")
    ap.add_argument("--calibrate", action="store_true",
                    help="打印候选池打分分布，用于重新标定 SCORE_THRESHOLD")
    args = ap.parse_args(list(argv) if argv is not None else None)

    DRY_RUN = not args.live
    phase = resolve_phase(args)
    log("=" * 96)
    log(f"集合竞价选股 V3 · QMT —— {'【实盘】' if args.live else '【DRY-RUN 只打印不下单】'}"
        f"  阶段={phase}  当前 {datetime.now():%Y-%m-%d %H:%M:%S}")
    log(f"门槛 {SCORE_THRESHOLD:g}｜每日最多 {MAX_BUY_PER_DAY} 只｜仓位 {WEIGHT_BASE:.0%}"
        f"｜止损 {STOP_CLOSE_PCT:.0%}｜最长持有 {MAX_HOLD_DAYS} 日"
        f"｜熔断 {'关闭' if DD_BREAKER_ROLL <= 0 else f'{DD_BREAKER_ROLL}日/{DD_BREAKER_DROP:.0%}'}")

    if not args.account or not args.mini_path:
        log("[ERROR] 必须提供 --account 与 --mini-path"
            "（或设置环境变量 QMT_ACCOUNT / QMT_MINI_PATH）")
        return 2
    if phase == "idle":
        log("当前处于盘中非交易时段（09:30-14:50）。本策略只在两个时点动作："
            "09:25 买入、14:56 卖出判定。请用 --buy-only / --sell-only 显式指定。")
        return 0
    if DRY_RUN:
        log("提示：未加 --live，本次不会真实下单。")

    try:
        trader, acc, const = connect(args.account, args.mini_path, args.session_id)
    except Exception as e:                                       # noqa: BLE001
        log(f"[ERROR] 连接失败：{type(e).__name__} {e}")
        log(traceback.format_exc())
        return 3

    try:
        data = QmtData()
        st = State()
        broker = Broker(trader, acc, const)
        strategy = AuctionV3(data, broker, st)

        if phase == "sell":
            strategy.do_sell()
        elif phase == "calibrate":
            strategy.prepare()
            strategy.collect()
            strategy.select()
            log("── 打分分布（用于标定 SCORE_THRESHOLD）──")
            sc = sorted((c.score for c in strategy._pre), reverse=True)
            if sc:
                n = len(sc)
                for q in (0, 10, 25, 50, 75, 90):
                    log(f"  p{q:<3} = {sc[min(n - 1, int(n * q / 100))]:.2f}")
                log(f"  max = {sc[0]:.2f}  min = {sc[-1]:.2f}  样本 {n}")
                log(f"  当前门槛 {SCORE_THRESHOLD:g} → 通过 "
                    f"{sum(1 for s in sc if s >= SCORE_THRESHOLD)} 只")
            else:
                log("  候选池为空，无法标定")
        else:                                    # buy
            run_once(strategy)
    except Exception as e:                                       # noqa: BLE001
        log(f"[ERROR] 策略运行异常：{type(e).__name__} {e}")
        log(traceback.format_exc())
        return 4
    finally:
        try:
            trader.stop()
        except Exception:                                        # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
