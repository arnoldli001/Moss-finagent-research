"""数据新鲜度评估模块——指数衰减置信度 + 五级分级 + 行业周期倍数。

参考设计文档：`docs/DATA_FRESHNESS_ARCHITECTURE.md`

核心公式：
  confidence = exp(-λ × days_since / (publish_cycle_days × industry_multiplier))

五级分级：
  0.9-1.0  fresh    ✅ 全权重参与
  0.7-0.9  normal   ✅ 全权重参与
  0.4-0.7  lagging  ⚠️ 权重×0.7，prompt 标注"数据可能滞后"
  0.1-0.4  stale    🔶 权重×0.3，prompt 标注"数据陈旧，仅供参考"
  0-0.1    expired  ⛔ 不展示（前端可通过 override 看）

关键约束（与文档对齐）：
- 长周期数据（船舶订单/核电装机）行业倍数 2.0-2.5，指数衰减更慢
- 日频 PE/PB/换手率等 7 天就过 0.7 阈值，30 天已 expired
- 月频 CPI/PPI 3 个月到 0.7，12 个月 expired
- 行业周期倍数**不直接改过滤阈值**，只改衰减速度
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

# ============ 指标前缀 → 发布频率映射（硬编码 MVP，未来可改 YAML） ============

# 每类：(publish_cycle_days, warning_days, filter_days)
_FREQ_BY_PREFIX: list[tuple[tuple[str, ...], int, int, int]] = [
    # === 日频（周期 1 天，7 天滞后，30 天过期）===
    (("PE(TTM):", "PB:", "PS:", "stock_close:", "index_close:", "etf_close:",
      "mkt:turnover", "mkt:margin_balance", "mkt:north_flow",
      "mkt:all_a_turnover", "mkt:chg_board", "mkt:kcb_board",
      "mkt:market_breadth", "mkt:turnover_rate:all_a", "mkt:turnover:hist",
      "mkt:turnover_rate:hist", "mkt:cybkcb:val", "mkt:cybkcb:turnover_hist",
      "ind:sw_first", "ind:sw_second",
      "ind:sw_third", "idx_val:", "sw_ind:",
      # 双创板块：成交额/涨跌家数截面是盘中实时（每 5 分钟作业采），
      # 估值/成交额日序列是日频。两类都按日频判，显式列出避免落到月频默认值。
      "mkt:cybkcb:",
      "fed:"), 1, 7, 30),

    # === 月频（周期 30 天，3 个月滞后，12 个月过期）===
    #
    # ⚠️ `ind:` 必须**排在日频规则之后**（它确实在后面，但原因值得写下来）：
    # 日频段里有 `ind:sw_first/sw_second/sw_third`，靠"更具体的 `ind:` 前缀"
    # 先命中；若把裸 `ind:` 提到日频段，`ind:penetration:`（年频）等也会被
    # 误判成日频。
    #
    # `mkt:cybkcb:` 必须**显式列出**（2026-09-26 修）：原来它不在任何规则里，
    # 于是落到 `_DEFAULT_FREQ`(30,90,365) 被当成**月频** —— 而 `mkt:cybkcb:turnover:all`
    # 是盘中每 5 分钟采的实时成交额！误判的直接后果是
    # `_is_db_fresh()` 认为"30 天前的都算新鲜"，同时 `_DB_SKIP_PREFIXES`
    # 又把它标成实时型，两边口径互相矛盾。
    (("CPI", "PPI", "M2", "社融", "PMI", "US_CPI", "US_CORE",
      "US_NONFARM", "US_unemp", "US_PCE", "us_fed", "ind:"),
     30, 90, 365),

    # === 季频（周期 90 天，6 个月滞后，18 个月过期）===
    #
    # ★ 2026-09-29：补入 `GDP`（`configs/indicators.yaml` 已按 **quarterly** 登记）。
    #
    # 为什么必须显式列（实测账，today = 2026-09-29，期别 = 2026-06-30）：
    #   不列 → 落到 `_DEFAULT_FREQ`(30,90,365) 被当成**月频**：
    #          conf = exp(-0.5×91/30) = 0.219 → `router._is_db_fresh`（≥0.4）判
    #          **不新鲜 → 每次查询都穿透去联网**，而库里的 H1 数据其实是当季最新的；
    #          面板上还会显示"陈旧，权重×0.3"。
    #   列进季频 → conf = exp(-0.5×91/90) = 0.513 → lagging，库够新即短路。
    # 与登记表 `frequency: quarterly` 口径一致（同一判断不许有两份实现）。
    # `GDP:同比` 靠前缀 `gdp` 一并覆盖（`_filter_days` 是 `startswith` + 大小写宽松）。
    (("GDP", "季度营收", "季度利润", "产能利用率", "quarterly", "q_"),
     90, 180, 540),

    # === 年频（周期 365 天，18 个月滞后，36 个月过期）===
    (("渗透率", "产能", "储量", "研发投入", "annual", "a_"),
     365, 540, 1095),

    # === 长周期行业数据（周期 90 天但过期阈值宽松）===
    (("船舶订单", "核电装机", "白酒库存", "铜产能", "coal_capacity"),
     90, 720, 1440),
]

# === 周频（周期 7 天，30 天滞后，90 天过期）===
_FREQ_BY_PREFIX.insert(1, (("weekly_inventory", "周度库存", "周度景气"), 7, 30, 90))

_DEFAULT_FREQ = (30, 90, 365)  # 不知道的默认月频


# ============ 行业周期倍数映射（MVP 硬编码）============

# 映射按中文行业名 → (英文key, multiplier)
_INDUSTRY_MULTIPLIERS: dict[str, float] = {
    # 短周期 1.0
    "消费电子": 1.0, "猪肉": 1.2, "化工大宗": 1.0, "食品饮料": 1.0,
    "纺织服装": 1.0, "家电": 1.0,
    # 中周期 1.5
    "半导体": 1.5, "芯片": 1.5, "光模块": 1.5, "AI算力": 1.5,
    "新能源车": 1.5, "光伏": 1.5, "储能": 1.5, "风电": 1.5,
    "机械设备": 1.5, "自动化设备": 1.5,
    # 长周期 2.0
    "船舶": 2.0, "核电": 2.0, "工程机械": 2.0, "白酒": 2.0,
    "医药": 2.0, "医疗器械": 2.0, "中药": 2.0,
    # 超长周期 2.5
    "铜": 2.5, "稀土": 2.5, "地产": 2.5, "航运": 2.5,
    "煤炭": 2.5, "石油": 2.5, "黄金": 2.5,
}


# ============ 核心类 ============

@dataclass
class FreshnessResult:
    """新鲜度评估结果。"""
    confidence: float          # 0-1 指数衰减置信度
    status: str                # fresh / normal / lagging / stale / expired
    status_icon: str           # ✅ / ⚠️ / 🔶 / ⛔
    weight_multiplier: float   # 参与分析的权重系数 (1.0 / 0.7 / 0.3 / 0.0)
    days_since: int            # 距今天数
    publish_cycle_days: int    # 该指标的发布周期
    multiplier: float          # 行业周期倍数
    should_display: bool       # 是否应该展示给 LLM（>0.1）
    note: str | None = None    # 状态标注文字

    def extra_dict(self) -> dict[str, Any]:
        """塞进 DataPoint.extra 的字典。"""
        return {
            "freshness": {
                "confidence": round(self.confidence, 3),
                "status": self.status,
                "icon": self.status_icon,
                "weight_multiplier": self.weight_multiplier,
                "days_since": self.days_since,
                "note": self.note,
            }
        }


# 衰减系数 λ
_LAMBDA = 0.5

_STATUS_THRESHOLDS = [
    (0.9, "fresh", "✅", 1.0, None),
    (0.7, "normal", "✅", 1.0, None),
    (0.4, "lagging", "⚠️", 0.7, "数据可能滞后，权重×0.7"),
    (0.1, "stale", "🔶", 0.3, "数据陈旧，仅供参考，权重×0.3"),
    (0.0, "expired", "⛔", 0.0, "数据过期，已从分析中过滤"),
]


class DataFreshnessEvaluator:
    """数据新鲜度评估器（单例 / 无状态，纯函数 + 配置查找）。"""

    @classmethod
    def evaluate(
        cls,
        indicator: str,
        period_date: Any,
        industry_hint: str | None = None,
        today: date | None = None,
    ) -> FreshnessResult:
        """评估一个指标的新鲜度。

        两层判定：
        1. **硬截止线 filter_days**（按指标发布频率查配置）——超过此天数 → 完全过期，
           should_display=False，不再参与分析（对应文档 7.1 的 filter_days）。
        2. **指数衰减 confidence**（exp(-λ × d/(周期×倍数))）——在有效期内按连续函数衰减，
           决定状态分级 fresh/normal/lagging/stale 和权重 multiplier。

        分离原因：filter_days=365 的月频 CPI，exp 衰减到 365 天时 conf≈0.002，
        不能用 conf<0.1 当作硬过滤——filter_days 才是设计上的硬截止。
        """
        today = today or date.today()
        pub_date = _parse_period_date(period_date)
        cycle = cls._publish_cycle(indicator)
        multiplier = cls._industry_multiplier(indicator, industry_hint)
        filter_d = cls._filter_days(indicator)
        days = (today - pub_date).days if pub_date else 99999

        # 1. 硬截止线：超过 filter_days → expired
        if days > filter_d:
            return FreshnessResult(
                confidence=0.0, status="expired", status_icon="⛔",
                weight_multiplier=0.0, days_since=days,
                publish_cycle_days=cycle, multiplier=multiplier,
                should_display=False,
                note=f"超过数据过期阈值 {filter_d} 天",
            )

        # 2. 指数衰减（硬截止线内连续评分）
        if days <= 0:
            confidence = 1.0
        else:
            confidence = math.exp(-_LAMBDA * days / (cycle * multiplier))
        # 硬截止内 confidence 下界 0.001（保持数值稳定性），上界 0.999
        confidence = max(0.001, min(0.999, confidence))

        # 硬截止内的状态分级：最高到 stale（conf<0.1 也当 stale 处理），
        # expired 状态只由 filter_days 硬截止决定（should_display=False）
        status, icon, weight, note = cls._classify_within_cutoff(confidence)
        return FreshnessResult(
            confidence=round(confidence, 4),
            status=status, status_icon=icon,
            weight_multiplier=weight,
            days_since=days,
            publish_cycle_days=cycle,
            multiplier=multiplier,
            should_display=True,  # 已通过硬截止线
            note=note,
        )

    @staticmethod
    def _classify_within_cutoff(confidence: float) -> tuple[str, str, float, str | None]:
        """硬截止内的分级：conf<0.1 也算 stale（不是 expired），expired 只由硬截止决定。"""
        # expired 永远由硬截止决定，这里只分 0.1+ 的区间
        if confidence >= 0.9:
            return "fresh", "✅", 1.0, None
        elif confidence >= 0.7:
            return "normal", "✅", 1.0, None
        elif confidence >= 0.4:
            return "lagging", "⚠️", 0.7, "数据可能滞后，权重×0.7"
        else:
            # 0.001 <= conf < 0.4 都算 stale（硬截止内能到的最低状态）
            return "stale", "🔶", 0.3, "数据陈旧，仅供参考，权重×0.3"

    @staticmethod
    def _filter_days(indicator: str) -> int:
        ind = indicator.lower()
        for prefixes, _c, _w, filter_d in _FREQ_BY_PREFIX:
            if any(ind.startswith(p.lower()) for p in prefixes):
                return filter_d
        return 365  # 默认 1 年

    @staticmethod
    def _publish_cycle(indicator: str) -> int:
        ind = indicator.lower()
        for prefixes, cycle, *_ in _FREQ_BY_PREFIX:
            if any(ind.startswith(p.lower()) for p in prefixes):
                return cycle
        return _DEFAULT_FREQ[0]

    @staticmethod
    def _industry_multiplier(indicator: str, hint: str | None) -> float:
        """行业周期倍数：先看 indicator 本身能否匹配，不行用 hint。"""
        candidates = [indicator]
        if hint:
            candidates.append(hint)
        # 先查行业名
        for cand in candidates:
            for key, mult in _INDUSTRY_MULTIPLIERS.items():
                if key in cand:
                    return mult
        return 1.0  # 默认短周期

    @staticmethod
    def _classify(confidence: float) -> tuple[str, str, float, str | None]:
        for thr, status, icon, weight, note in _STATUS_THRESHOLDS:
            if confidence >= thr:
                return status, icon, weight, note
        return _STATUS_THRESHOLDS[-1][1:]  # expired

    # ---------- 工具函数（供 router / connector 调用）----------

    @classmethod
    def db_expired_months(cls, indicator: str) -> int:
        """给 ConnectorRouter 用：这个指标的 DB 数据多少个月算过期。"""
        for prefixes, _c, _w, filter_d in _FREQ_BY_PREFIX:
            ind = indicator.lower()
            if any(ind.startswith(p.lower()) for p in prefixes):
                return max(1, filter_d // 30)
        return 12


# ============ 小工具 ============

def _parse_period_date(raw: Any) -> date:
    if not raw:
        return date(1970, 1, 1)
    try:
        s = str(raw)
        if len(s) == 7:  # YYYY-MM
            return datetime.strptime(s, "%Y-%m").date()
        if len(s) >= 10:  # YYYY-MM-DD
            return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        pass
    return date(1970, 1, 1)


# 全局快捷访问
freshness = DataFreshnessEvaluator()
