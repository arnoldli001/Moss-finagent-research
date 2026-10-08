"""A07舆情分析：本地情绪指标计算（防幻觉：数值由代码计算，LLM只做解读与周期定位）。

情绪分模型：weighted_sentiment = Σ(sign × confidence) / Σ(confidence) ∈ [-1, 1]
- positive=+1, negative=-1, neutral=0
- 以事件置信度为权重，压低低可信事件对情绪的扰动
"""

from __future__ import annotations

from collections import Counter
from typing import Any

_SIGN = {"positive": 1.0, "negative": -1.0, "neutral": 0.0}


def group_events(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """按**标的代码**把事件分组（没有代码的事件退回按 `subject` 分组）。

    ## 为什么需要（用户 2026-10-08 报障的"舆情版"）

    > 「…未来半年能否持有高股息的**宁波银行**和**中国神华**？标的 601088」

    采集层现在**逐只**取新闻（`supervisor._fetch_news_per_code`）、A06 把代码带进
    事件表；若这里仍把全部事件混算，两只票的利好/利空会**互相抵消**成一个分
    （用户读到的"情绪偏暖"不知道是针对哪只）。

    ⚠️ 分组键 = `stock_code`（采集层盖的章，可核对）**优先**，没有代码时用
    `subject`（宏观/行业事件）——两类事件**不混在一组**。
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for e in events:
        key = str(e.get("stock_code") or "").strip() or str(
            e.get("subject") or "").strip() or "(未具名)"
        groups.setdefault(key, []).append(e)
    return groups


def compute_sentiment_metrics_by_group(
    events: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """逐组情绪指标（**复用** `compute_sentiment_metrics`，不写第二套算法）。

    两组数各算各的、口径与总体分完全一致（同一函数），
    所以"逐只分的和"与"总体分"不一致时，差异只可能来自**分组**本身。
    """
    return {
        key: compute_sentiment_metrics(group)
        for key, group in group_events(events).items()
    }


def compute_sentiment_metrics(events: list[dict[str, Any]]) -> dict[str, Any]:
    """从事件表计算情绪指标（纯本地计算，供A07注入prompt由LLM解读）。"""
    if not events:
        return {"weighted_sentiment": 0.0, "event_count": 0,
                "distribution": {"positive": 0, "negative": 0, "neutral": 0},
                "top_subjects": []}

    weight_sum = 0.0
    score_sum = 0.0
    for e in events:
        weight = float(e.get("confidence", 0.5))
        weight_sum += weight
        score_sum += _SIGN.get(e.get("direction", "neutral"), 0.0) * weight
    weighted = round(score_sum / weight_sum, 3) if weight_sum > 0 else 0.0

    subject_scores: dict[str, float] = {}
    for e in events:
        subject = e.get("subject", "")
        if subject:
            subject_scores[subject] = subject_scores.get(subject, 0.0) + (
                _SIGN.get(e.get("direction", "neutral"), 0.0)
                * float(e.get("confidence", 0.5))
            )
    top_subjects = [
        {"subject": s, "net_score": round(v, 3)}
        for s, v in sorted(subject_scores.items(), key=lambda kv: -abs(kv[1]))[:5]
    ]

    return {
        "weighted_sentiment": weighted,
        "event_count": len(events),
        "distribution": dict(Counter(e.get("direction", "neutral") for e in events)),
        "top_subjects": top_subjects,
    }
