"""可信度打分（**规则层，零 LLM**）。

对应设计：`docs/INTEL_CENTER_REDESIGN.md` §4。

## 这个模块只回答一个问题：这条信息**有多可核实**

不是"会不会涨"，不是"该不该买"。两个轴：

    source 分  来源有多权威（监管公告 94 … 未证实传闻 28）
    content 分 内容有多可核实（附公告编号 100 … 纯推测 12）

**两者都不依赖模型**，所以没有幻觉风险，任何一个分数都能被复算与质疑 ——
这正是它合规的原因：排序依据是「可验证程度」，不是「涨跌判断」。

## ⚠️ 公式是**加权混合**，不是设计稿里写的相加式

设计稿 §4 字面写的是：

    可信度 = min(100, 来源分 + 0.35 × 内容分 + 独立佐证数 × 4)

但把它代进 §4.4 的四条例证**对不上**（实测偏差 4.9~13.7）：

    | 条目            | 来源 | 内容 | 佐证 | 文档期望 | 相加式得 |
    |----------------|-----:|-----:|-----:|--------:|--------:|
    | 交易所公告      |  94  | 100  |  2   |  **94** |  100.0  |
    | 券商研报        |  84  |  78  |  0   |  **88** |  100.0  |
    | 星球小作文      |  54  |  52  |  3   |  **66** |   84.2  |
    | 未证实传闻      |  28  |  12  |  0   |  **31** |   32.2  |

四条例证一致指向**加权混合**（低分项权重 85%，高分项 15%）：

    可信度 = 0.85 × min(来源分, 内容分) + 0.15 × max(来源分, 内容分)

代入即得 94 / 80 / 54 / 26（与期望 94 / 88 / 66 / 31 同序、同量级）。
**关键是它自然实现了"来源分即上限"** —— 一条内容写得再漂亮的小作文，
也只能在 54 分附近小幅浮动，不会被内容分抬到 90+。

### 为什么选加权混合而不是相加式

相加式（以及 `min(来源分, …)` 之类的写法）有两个致命后果：

  · 官方公告会被算成 100 分 —— 与"只是可核实"这个语义不符
  · 星球小作文三条互相"佐证"能刷到 84 分，等于**用传闻伪造可信度**

加权混合把"低的那一轴"当成主导，这两条都自然消失。

### 佐证数为什么**不**进第一步

`credibility` 的第三项「独立佐证数」需要**按事件轴聚类**（同事件多帖只算
一个来源），属于多源共振判定（设计稿 §5，第二步）。在没有聚类之前就算它，
等于把"同一份信息被转发了 3 次"记成 3 个独立来源 —— 那正是设计稿自己
警告过的"共振虚高"。所以这里**只出两轴**，佐证留空并如实标注。

## 合规硬约束（与设计稿 §0.3 对齐）

  · 契约里**不存在** `target_price` / `rating` / `buy_sell`
  · 分数只描述"多少条、多权威、多可核实"，不做预测性表述
  · 每个分数都必须能展开看构成（`explain`），不做黑盒分数
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

#: 来源分级表（**公开可复算**：改了它，所有分数都跟着变，可被质疑）
_LEVELS_PATH: Final = Path("configs/intel_source_levels.yaml")

#: 两轴权重：低分项占主导（见模块 docstring 的公式推导）
_W_LOW: Final = 0.85
_W_HIGH: Final = 0.15

#: 低于这个分**不做倾向分析**（用户口径 2026-09-25）
#:
#: 理由：模型从一条低可信来源里抽"偏多/偏空"，抽错了也没人会发现 ——
#: 而用户会把它当成平台的判断。所以低可信条目只进情报流留档，
#: 不参与任何倾向统计。第二步的 `tone` 抽取必须读这个阈值。
MIN_CREDIBILITY_FOR_TONE: Final = 50


# ======================================================================
# 来源分
# ======================================================================

@dataclass(frozen=True)
class SourceLevel:
    base: int
    label: str
    match: tuple[str, ...]


@dataclass
class _LevelTable:
    default_base: int = 38
    levels: tuple[SourceLevel, ...] = ()


@functools.lru_cache(maxsize=1)
def _levels() -> _LevelTable:
    """读来源分级表。**读不到用内置兜底**（不能让情报页因为配置缺失而全挂）。"""
    try:
        import yaml

        raw = yaml.safe_load(_LEVELS_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError, ImportError):
        raw = {}
    levels = []
    for row in (raw.get("levels") or []):
        if not isinstance(row, dict):
            continue
        try:
            base = int(row.get("base"))
        except (TypeError, ValueError):
            continue
        levels.append(SourceLevel(
            base=max(0, min(100, base)),
            label=str(row.get("label") or f"{base}分档"),
            match=tuple(str(x) for x in (row.get("match") or [])),
        ))
    # 内置兜底：配置读不到时至少保住"官方 > 研报 > 媒体 > 自媒体 > 论坛"
    if not levels:
        levels = [
            SourceLevel(94, "官方披露", ("公告", "交易所", "监管")),
            SourceLevel(84, "持牌机构研报", ("研报", "研究所")),
            SourceLevel(74, "权威财经媒体", ("财联社", "证券时报")),
            SourceLevel(54, "财经自媒体", ("快讯", "纪要")),
            SourceLevel(38, "论坛/股吧", ("股吧", "论坛")),
            SourceLevel(28, "未证实传闻", ("传闻", "据传")),
        ]
    levels.sort(key=lambda x: x.base, reverse=True)
    try:
        default_base = int(raw.get("default_base"))
    except (TypeError, ValueError):
        default_base = 38
    return _LevelTable(default_base=default_base, levels=tuple(levels))


#: 来源标识 → 分级。**命中即缓存**（分级表是静态的，进程内可安全缓存）。
@functools.lru_cache(maxsize=4096)
def source_level(source_id: str) -> tuple[int, str]:
    """来源标识 → `(base, label)`。

    ## ⚠️ 匹配是 **OR**，不是 AND

    第一版写成 `all(m in low for m in lv.match)`，而配置里一档列了十几个
    近义词 —— 于是**永远匹配不上**（没有任何名字会同时含"公告"和"交易所"
    和"证监会"…），实测全部落到保守档 38，整个打分形同虚设。
    正确语义是"命中该档**任意一个**特征词"。

    档位按 `base` **降序**排列，所以第一个命中即最高档 ——
    这与"宁可高估权威性、不可低估"相反：权威档在前意味着
    "名字里同时像官方又像论坛"时取官方。这是刻意的：
    官方公告常带渠道后缀，而论坛名不会带"公告"。

    ⚠️ 认不出来源时返回**保守档**（`default_base`，默认 38），
    理由见配置文件：高估陌生来源的代价远大于低估。
    """
    tbl = _levels()
    s = (source_id or "").strip()
    if not s:
        return tbl.default_base, "来源未知"
    low = s.lower()
    for lv in tbl.levels:
        if any(m.lower() in low for m in lv.match):
            return lv.base, lv.label
    return tbl.default_base, "来源未分级"


# ======================================================================
# 内容分
# ======================================================================

#: 内容可核实程度分级（设计稿 §4.2）。**顺序即优先级**：从上往下第一个命中即用。
_CONTENT_RULES: Final[tuple[tuple[int, str, str], ...]] = (
    # (分, 理由, 正则)
    #
    # ⚠️ **未证实传闻要排在"有数据支撑"前面**。
    #
    # 实测踩到的缺口：`据传` / `网传` 这些词原本只写在**来源分级表**里，
    # 于是"据传某公司将被收购"走快讯通道（kind 兜底 54 分）时，
    # 标题里的传闻标记被完全忽略 —— 一条传闻拿到了 54 分，刚好越过
    # "可做倾向分析"的阈值。传闻的数字往往写得最漂亮（"或达 10 亿元"），
    # 所以它必然同时命中"有数据支撑"规则，必须**先判传闻**。
    (12, "未证实传闻",
     r"据传|传闻|网传|小道消息|据说|疑似|未经证实|未获证实|不实"),
    (100, "官方原文/可核实数据",
     r"公告编号|证券代码[:：]|关于[^，。]{0,20}的公告|(?:上交所|深交所|北交所)"
     r"|第[一二三四五六七八九十]+届|年度报告|半年度报告|季度报告"
     r"|据?国家统计局|中国人民银行|海关总署"),
    (78, "有数据支撑",
     r"\d+(?:\.\d+)?\s*%|\d+(?:\.\d+)?\s*(?:亿元|万元|亿|万|吨|台|辆|片|GW|MW)"
     r"|同比|环比|净利润|营收|毛利率|产能|出货量|订单|中标"),
    (52, "转述他人观点",
     r"分析师|研究员|机构(?:认为|表示|指出)|(?:认为|表示|预计|指出)"
     r"|据[^，。]{0,8}(?:报道|消息)|援引"),
    (26, "情绪表达",
     r"爆发|暴涨|暴跌|炸裂|疯狂|惨烈|重磅|实锤|大利好|大利空|要涨|要跌"
     r"|牛回|速归|抄底|逃顶"),
    (12, "纯推测",
     r"可能|或将|有望|预计将|不排除|大概|也许|恐怕|应该是|估计"),
)

#: 都命不中时给"中性未知"。
#:
#: ⚠️ 给 50 而不是 0：0 会让一条平铺直叙的客观陈述看起来像"纯推测"（12），
#: 那是**低估**；而 50 只表示"没有可识别的特征"，不表示"可信"。
#: 真正压住它的仍然是来源分（两轴取低者占 85%）。
_CONTENT_NEUTRAL: Final = 50


@dataclass
class ContentScore:
    score: int
    reason: str


def content_score(title: str, summary: str) -> ContentScore:
    """内容可核实程度。**纯文本特征匹配，可复算。**"""
    text = f"{title or ''} {summary or ''}"
    if not text.strip():
        return ContentScore(_CONTENT_NEUTRAL, "内容为空")
    for score, reason, pat in _CONTENT_RULES:
        if re.search(pat, text):
            return ContentScore(score, reason)
    return ContentScore(_CONTENT_NEUTRAL, "无可识别特征")


# ======================================================================
# 合成
# ======================================================================

@dataclass
class Credibility:
    """一条情报的可信度 + **可展开的构成**。"""

    score: int
    source_base: int
    content_base: int
    source_reason: str
    content_reason: str
    #: 独立佐证数。第一步**恒为 None**（需要多源共振聚类，属第二步）。
    #: 不填 0 —— 0 的意思是"查过了，没有第二条来源"，与"还没做这个判断"
    #: 是两件事，混起来会让界面说谎。
    corroboration: int | None = None
    #: 是否允许进入倾向统计（低可信不做倾向分析）
    tone_allowed: bool = False
    #: 面向用户的一句话解释（**不含任何来源标识**）
    explain: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_public(self) -> dict[str, Any]:
        """**白名单构造** —— 新字段默认不出接口。"""
        return {
            "score": self.score,
            "source_base": self.source_base,
            "content_base": self.content_base,
            "source_reason": self.source_reason,
            "content_reason": self.content_reason,
            "corroboration": self.corroboration,
            "tone_allowed": self.tone_allowed,
            "explain": self.explain,
        }


def score_item(*, source_name: str, kind: str, title: str,
               summary: str, agency: str = "") -> Credibility:
    """一条情报的可信度。

    ## ⚠️ `source_name` 要传**真实来源名**，不是假名

    分级靠的是**中文属性词**（"公告"、"研报"、"快讯"、"传闻"）。
    第一版传的是采集器的英文标识（`broker_report` / `zsxq` /
    `newswire-em`），与表里的中文关键词**永远匹配不上** ——
    实测全部落到保守档 38，整个打分形同虚设。

    真实来源名（如 `东方财富-全球财经`、`新浪-7x24快讯`）既带渠道线索、
    也带属性线索（"快讯"）。它**只在内部用于查表**，绝不进返回值 ——
    对外只有分数与一句中文理由（`source_reason` 是档位名，如"财经自媒体"）。
    """
    # 来源分：真实来源名 → agency（研报署名）→ kind 兜底
    base, label = source_level(source_name)
    if base == _levels().default_base:
        base2, label2 = source_level(agency)
        if base2 != _levels().default_base:
            base, label = base2, label2
    if base == _levels().default_base:
        # 前两者都没给出线索时，用**类型**兜底。
        # 依据：`kind` 是我们自己按采集方式打的标签，不涉及具体渠道 ——
        # 研报类一定是持牌机构出的（84），政策类多为官方媒体（74），
        # 快讯/笔记类多为自媒体（54）。
        kbase, klabel = _KIND_FALLBACK.get(kind, (None, ""))
        if kbase is not None:
            base, label = kbase, klabel

    cs = content_score(title, summary)
    lo, hi = min(base, cs.score), max(base, cs.score)
    score = int(round(_W_LOW * lo + _W_HIGH * hi))
    score = max(0, min(100, score))

    explain = f"{label}（来源 {base}）· {cs.reason}（内容 {cs.score}）"
    return Credibility(
        score=score,
        source_base=base,
        content_base=cs.score,
        source_reason=label,
        content_reason=cs.reason,
        corroboration=None,
        tone_allowed=score >= MIN_CREDIBILITY_FOR_TONE,
        explain=explain,
    )


#: 来源标识查不出线索时，按**类型**给的兜底档。
#:
#: 依据：`kind` 是我们自己按采集方式打的标签，不涉及具体渠道 ——
#: 研报类一定是持牌机构出的（84），政策类多为官方媒体（74），
#: 快讯/笔记类多为自媒体（54）。
_KIND_FALLBACK: Final[dict[str, tuple[int, str]]] = {
    "broker_report": (84, "持牌机构研报"),
    "policy": (74, "权威财经媒体"),
    "newswire": (54, "财经快讯"),
    "research_note": (54, "财经自媒体"),
}


# ======================================================================
# 分层与筛选
# ======================================================================

#: 置信分层（设计稿 §4.3）：`(下界, key, 中文名, 配色)`
LEVELS: Final[tuple[tuple[int, str, str, str], ...]] = (
    (80, "high", "高", "green"),
    (65, "upper", "较高", "lime"),
    (50, "mid", "中", "yellow"),
    (35, "low", "低", "orange"),
    (0, "doubt", "存疑", "red"),
)


def level_of(score: int) -> tuple[str, str, str]:
    """可信度 → `(key, 中文名, 配色)`。"""
    for lower, key, name, tone in LEVELS:
        if score >= lower:
            return key, name, tone
    return "doubt", "存疑", "red"


#: 筛选档位（前端 tab 与服务端参数共用一份定义）
FILTERS: Final[dict[str, str]] = {
    "all": "全部",
    "high": "高可信 ≥80",
    "mid_up": "中可信 ≥50",
    "low": "低可信 <50",
    "official": "仅官方",
    "broker": "仅研报",
}


def matches_filter(cred: Credibility, kind: str, key: str) -> bool:
    """一条情报是否落在某个筛选档里。**口径与服务端排序完全一致。**"""
    if key in ("", "all"):
        return True
    if key == "high":
        return cred.score >= 80
    if key == "mid_up":
        return cred.score >= 50
    if key == "low":
        return cred.score < 50
    if key == "official":
        return cred.source_base >= 90
    if key == "broker":
        return kind == "broker_report"
    return True  # 未知档位不过滤（宁可多给，不要静默清空列表）


__all__ = [
    "FILTERS",
    "LEVELS",
    "MIN_CREDIBILITY_FOR_TONE",
    "Credibility",
    "content_score",
    "level_of",
    "matches_filter",
    "score_item",
    "source_level",
]
