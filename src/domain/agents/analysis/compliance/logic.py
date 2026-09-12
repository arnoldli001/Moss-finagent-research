"""A12合规爆雷：本地合规风险规则计算（防幻觉：旗标与等级由规则计算，LLM只做定性综合）。

规则三类：
1. 比率旗标：关联交易/商誉/质押/担保四类高危比率（阈值按百分数口径，如50即50%）
2. 存贷双高：货币资金与有息负债同时畸高，账面资金真实性存疑
3. 信息层事件：A06提取的litigation负面事件，含"立案/调查/冻结/处罚"升级为严重

等级：存在任一严重旗标，或普通旗标≥3 → 高；存在任一普通旗标 → 中；否则无。
"""

from __future__ import annotations

from typing import Any

# (指标关键字, ((阈值%, 描述模板, 是否严重), ...))，高阈值在前取首个命中，避免重复旗标
_RATIO_RULES: tuple[tuple[str, tuple[tuple[float, str, bool], ...]], ...] = (
    ("关联交易", ((30.0, "关联交易占比{value:g}%超30%，存在利益输送嫌疑", False),)),
    ("商誉", ((30.0, "商誉/净资产{value:g}%超30%，存在商誉减值爆雷隐患", False),)),
    ("质押", (
        (80.0, "大股东股权质押比例{value:g}%超80%，平仓与控制权变更高危", True),
        (50.0, "大股东股权质押比例{value:g}%超50%，存在平仓与控制权风险", False),
    )),
    ("担保", (
        (100.0, "对外担保/净资产{value:g}%超100%，或有负债高危", True),
        (50.0, "对外担保/净资产{value:g}%超50%，或有负债风险", False),
    )),
)

# 存贷双高阈值（均为占总资产百分比）
_DOUBLE_HIGH_CASH = 40.0
_DOUBLE_HIGH_DEBT = 40.0

# 监管/诉讼严重关键词（命中即升级为严重旗标）
_SEVERE_EVENT_KEYWORDS = ("立案", "调查", "冻结", "处罚", "警示函", "退市", "违规")


def _find_value(data_points: list[dict[str, Any]], keyword: str) -> float | None:
    for p in data_points:
        if keyword in str(p.get("indicator", "")) and isinstance(p.get("value"), (int, float)):
            return float(p["value"])
    return None


def _ratio_flags(data_points: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """返回 (全部旗标, 严重旗标)。同一指标按最高命中阈值只产一条。"""
    flags: list[str] = []
    severe: list[str] = []
    for keyword, bands in _RATIO_RULES:
        value = _find_value(data_points, keyword)
        if value is None:
            continue
        for threshold, template, is_severe in bands:  # 高阈值在前
            if value > threshold:
                desc = template.format(value=value)
                flags.append(desc)
                if is_severe:
                    severe.append(desc)
                break
    return flags, severe


def _double_high_flag(data_points: list[dict[str, Any]]) -> str | None:
    cash = _find_value(data_points, "货币资金")
    debt = _find_value(data_points, "有息负债")
    if (cash is not None and debt is not None
            and cash > _DOUBLE_HIGH_CASH and debt > _DOUBLE_HIGH_DEBT):
        return (f"存贷双高：货币资金/总资产{cash:g}%且有息负债/总资产{debt:g}%"
                "同时畸高，账面资金真实性存疑")
    return None


def _event_flags(events: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """信息层A06事件 → 合规监管信号。"""
    flags: list[str] = []
    severe: list[str] = []
    for e in events:
        if e.get("event_type") != "litigation" or e.get("direction") != "negative":
            continue
        quote = str(e.get("evidence_quote") or e.get("subject") or "未具名事件")
        desc = f"涉诉/监管事件：{quote[:50]}"
        flags.append(desc)
        if any(k in quote for k in _SEVERE_EVENT_KEYWORDS):
            severe.append(desc)
    return flags, severe


def evaluate_compliance(
    data_points: list[dict[str, Any]], events: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """汇总全部合规信号并给出本地爆雷等级（权威值，LLM不得修改）。"""
    flags, severe = _ratio_flags(data_points)
    double_high = _double_high_flag(data_points)
    if double_high:
        flags.append(double_high)
        severe.append(double_high)
    event_flags, event_severe = _event_flags(events or [])
    flags += event_flags
    severe += event_severe

    severe_count = len(severe)
    if severe_count >= 1 or len(flags) >= 3:
        level = "高"
    elif flags:
        level = "中"
    else:
        level = "无"
    return {
        "compliance_flags": flags or ["未见明显合规风险信号"],
        "severe_flag_count": severe_count,
        "compliance_level_calc": level,
    }
