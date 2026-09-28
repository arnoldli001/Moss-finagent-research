"""采集深度权威表 —— 每个指标一次取多少条。

## 为什么需要它（2026-09-28 用户裁定）

用户原话：「采集路径的条数上限默认设置为60，具体的细分如下」+ 14 条细分类别。

背景：`akshare_connector._nbs_price_yoy_points` 遍历**固定的 2016–2026 nodes**，
把每个 `YYYY年M月` 列全收进来 —— **`start_date`/`end_date` 在签名里但从不使用**。
所以 CPI/PPI 每次采集都是 **≈120 条（11 年月度全量）**，与"需要多少"无关。
120 不是魔数，是"拉全量历史"的副产品。

## 判据来源与优先级（**有序表，先匹配者胜**）

用户给的 14 类里有重叠（宏观/另类 10 · 宏观月度 12 · 宏观季度 8），
所以**规则必须有序**，具体优先于一般：

    宏观季度(8) → 宏观月度(12) → 宏观/另类(10) → … → 默认(60)

⚠️ 若写成无序 dict，Python 的插入序会**静默决定**结果 —— 那是不可 review 的。
本模块用 tuple-of-rules，顺序即语义，并有测试钉住"具体优先"。
"""
from __future__ import annotations

import re

#: 默认深度（用户裁定：默认 60）
DEFAULT_DEPTH = 60

#: 有序规则表：`(类别名, 匹配模式, 深度)` —— **先匹配者胜**。
#: 匹配规则：`*` 后缀 = 前缀匹配；否则用 `re.search`（支持 `^xx:` 这类锚定）。
#: 深度语义：**保留最新 N 条**（趋势分析需要最近的数据，不是最早的数据）。
_CATEGORY_DEPTH: tuple[tuple[str, str, int], ...] = (
    # ---- 宏观（具体 → 一般，顺序即优先级）----
    ("宏观季度数据",     r"^(GDP|gdp)",                       8),   # 两年
    ("宏观月度数据",     r"^(CPI|PPI|M2|社融|PMI)",          12),   # 一年
    ("宏观/另类数据",    r"^(us_|fed:|macro_)",               10),
    ("宏观/另类数据",    r"^ind_",                            10),   # 行业另类
    ("指数快照类",       r"^idx_val:",                        20),
    # ---- 行情/财务 ----
    ("日线K线",          r"^(stock_close|stock_open|stock_high|stock_low):", 15),
    ("分钟级行情",       r"^(min_|intraday_)",                 60),
    ("结构化行情/财务",  r"^(PE|PB|PS|PCF|ROE|ROA)",          100),
    ("结构化行情/财务",  r"^stock_",                          100),
    ("结构化行情/财务",  r"^(mkt:|val:|bk_|sw_)",             100),
    # ---- 文本类 ----
    ("研报/公告文本",    r"^(report|announcement|公告)",       30),
    ("研报/公告检索",    r"^(news_|intel_)",                   15),
)

#: 类别 → 深度（供文档/审计展示；**不含优先级语义**，仅供查表）
CATEGORY_TABLE: dict[str, int] = {}
for _name, _pat, _depth in _CATEGORY_DEPTH:
    CATEGORY_TABLE.setdefault(_name, _depth)

#: 「全量拉取 + 去重」类：不截断（增量轮询自己去重）
FULL_FETCH_PATTERNS: tuple[str, ...] = (r"^(news|intel)_(poll|incremental)",)

#: 「按只/按维度」计数类 —— 语义不是"条数"，本模块不截断，仅登记以免被误当条数
COUNT_BY_ENTITY: dict[str, int] = {
    "股票标的批量分析": 7,
    "深度多维分析": 22,
    "多标的批量": 100,
}


def _matches(indicator: str, pattern: str) -> bool:
    if pattern.endswith("*"):
        return indicator.startswith(pattern[:-1])
    try:
        return re.search(pattern, indicator) is not None
    except re.error:  # 防御：坏模式不该让整条链路炸
        return False


def depth_for(indicator: str) -> int:
    """该指标一次取多少条（**保留最新 N 条**）。"""
    for _name, pattern, depth in _CATEGORY_DEPTH:
        if _matches(indicator, pattern):
            return depth
    return DEFAULT_DEPTH


def category_of(indicator: str) -> str:
    """命中的类别名（用于日志/审计，让"为什么是 12 条"可解释）。"""
    for name, pattern, _depth in _CATEGORY_DEPTH:
        if _matches(indicator, pattern):
            return name
    for pattern in FULL_FETCH_PATTERNS:
        if _matches(indicator, pattern):
            return "全量拉取+去重"
    return "默认"


def is_full_fetch(indicator: str) -> bool:
    """是否属于「全量拉取 + 去重」（不截断）。"""
    return any(_matches(indicator, p) for p in FULL_FETCH_PATTERNS)


def trim(points: list, indicator: str) -> list:
    """按该指标的深度**截断为最新 N 条**。

    ⚠️ 保留的是**最新** N 条，不是最早 N 条 —— 写反了会静默给出十年前的数据。

    ⚠️ **不依赖调用方的顺序**：本函数自己按 `period_date` 升序排后再取尾部。
    为什么（实测风险）：`smart_fetch.py` 原实现是 `points[:limit]`（前 N 条），
    而连接器有的按期升序、有的降序返回 —— 同一个 `[:N]` 在不同连接器上
    含义相反。**把方向交给调用方 = 迟早有一处反了**，所以在唯一的截断点定死。

    元素可以是 `DataPoint`（有 `.period_date`）或 dict（有 `"period_date"`）。
    缺 `period_date` 时退化为**保持原顺序**取尾部（不猜，但也不静默改变语义）。
    """
    if is_full_fetch(indicator):
        return points
    n = depth_for(indicator)
    if len(points) <= n:
        return points

    def _key(p: object) -> str:
        if isinstance(p, dict):
            return str(p.get("period_date") or "")
        return str(getattr(p, "period_date", "") or "")

    has_dates = all(_key(p) for p in points)
    ordered = sorted(points, key=_key) if has_dates else points
    return ordered[-n:]
