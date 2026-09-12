"""A07舆情分析：本地情绪指标计算（防幻觉：数值由代码计算，LLM只做解读与周期定位）。

情绪分模型：weighted_sentiment = Σ(sign × confidence) / Σ(confidence) ∈ [-1, 1]
- positive=+1, negative=-1, neutral=0
- 以事件置信度为权重，压低低可信事件对情绪的扰动
"""

from __future__ import annotations

from collections import Counter
from typing import Any

_SIGN = {"positive": 1.0, "negative": -1.0, "neutral": 0.0}


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
