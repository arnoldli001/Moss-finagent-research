"""A05信息去伪：本地规则可信度评分（防幻觉：分数由规则计算，LLM只做定性复核）。

评分模型：rule_score = clamp(source_score × recency_factor + text_adjustment, 0, 1)
- source_score：来源白名单分级（官方监管1.0 → 社交自媒体0.35，未知0.5）
- recency_factor：时效衰减（7天内1.0 → 1年以上0.3，未知0.95轻微降权）
- text_adjustment：文本特征（夸张词扣分、含具体数据/规范引用加分）
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

# 来源分级白名单（匹配串小写包含即命中；顺序即优先级，先命中先得）
_SOURCE_TIERS: tuple[tuple[float, tuple[str, ...]], ...] = (
    (1.00, ("央行", "中国人民银行", "国家统计局", "证监会", "交易所", "财政部",
            "国务院", "发改委", "金融监管", "gov.cn", "统计局")),
    (0.85, ("新华社", "新华网", "央视", "人民日报", "证券时报", "中国证券报",
            "上海证券报", "证券日报", "21世纪经济报道", "财新", "路透", "彭博",
            "bloomberg", "reuters", "中证网")),
    (0.75, ("研报", "中金", "中信", "国泰君安", "高盛", "摩根", "评级",
            "证券公司", "基金公司", "wind", "同花顺ifind")),
    (0.65, ("新浪财经", "东方财富", "华尔街见闻", "第一财经", "财联社", "每经",
            "每日经济新闻", "界面新闻", "证券之星", "格隆汇", "智通财经")),
    (0.35, ("微博", "股吧", "雪球", "知乎", "微信公众号", "抖音", "小红书",
            "论坛", "贴吧", "自媒体", "微信群", "朋友圈")),
)
_DEFAULT_SOURCE_SCORE = 0.5

# 时效衰减阈值（天 → 系数），从近到远取首个命中区间
_RECENCY_BANDS: tuple[tuple[float, int], ...] = (
    (1.00, 7), (0.90, 30), (0.75, 90), (0.50, 365), (0.30, 3650),
)
_RECENCY_UNKNOWN = 0.95

# 标题党/情绪煽动词（每个-0.08，累计上限-0.40）
_SENSATIONAL_WORDS: tuple[str, ...] = (
    "震惊", "惊爆", "内幕", "必涨", "必跌", "稳赚", "包赚", "暴富",
    "一夜", "疯传", "速看", "删前快看", "惊天", "翻倍", "梭哈", "满仓干",
)
_SENSATIONAL_PENALTY = 0.08
_SENSATIONAL_CAP = 0.40

_HAS_NUMBERS = re.compile(r"[0-9０-９]+(?:\.[0-9]+)?%?")
_HAS_ATTRIBUTION = re.compile(r"据.{2,12}(报道|披露|公告|统计|消息)|来源[:：]")


def _source_score(source_name: str) -> float:
    lowered = (source_name or "").lower()
    for score, patterns in _SOURCE_TIERS:
        if any(p in lowered for p in patterns):
            return score
    return _DEFAULT_SOURCE_SCORE


def _recency_factor(publish_time: str, now: datetime | None = None) -> float:
    """ISO时间解析失败或为空 → 未知时效轻微降权。"""
    if not publish_time:
        return _RECENCY_UNKNOWN
    text = publish_time.strip().replace("Z", "+00:00").replace("/", "-")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return _RECENCY_UNKNOWN
    now = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    days = max(0.0, (now - dt).total_seconds() / 86400)
    for factor, upper in _RECENCY_BANDS:
        if days <= upper:
            return factor
    return 0.30


def _text_adjustment(text: str) -> float:
    adjustment = 0.0
    hits = sum(1 for w in _SENSATIONAL_WORDS if w in text)
    if hits:
        adjustment -= min(hits * _SENSATIONAL_PENALTY, _SENSATIONAL_CAP)
    if _HAS_NUMBERS.search(text):
        adjustment += 0.05
    if _HAS_ATTRIBUTION.search(text):
        adjustment += 0.05
    return adjustment


def score_item(item: dict, *, now: datetime | None = None) -> dict:
    """对单条信息做规则评分，返回分项与总分（供A05合并LLM复核）。"""
    text = f"{item.get('title', '')} {item.get('text', '')}"
    source = _source_score(item.get("source_name", ""))
    recency = _recency_factor(item.get("publish_time", ""), now)
    adjustment = _text_adjustment(text)
    rule_score = round(min(1.0, max(0.0, source * recency + adjustment)), 3)
    return {
        "source_score": source,
        "recency_factor": recency,
        "text_adjustment": round(adjustment, 3),
        "rule_score": rule_score,
    }
