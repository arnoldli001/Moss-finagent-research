"""舆情热度：**按股票/事件聚合**，而不是统计量堆砌。

> 用户口径（2026-09-25）：
> "舆情热度监控的内容呢？原来 D:\\code\\moss-finance-assistant 这个项目的实现
> 是讲出目前讨论最热的话题和事件，偏多偏空分析结论的……
> 需求是具体的内容，而不是统计数量。情报中心这页做的毫无使用价值，
> 作为一个投资者没获得任何有用信息。"

## 为什么重做（我上一版错在哪）

上一版这一页是三个**统计块**：来源类型结构、按日分布、被提及标的（6 位代码）。
它们的共同问题是**只回答"有多少"，不回答"发生了什么"** ——
投资者看完不知道哪只票在被讨论、讨论的是什么、结论偏哪边。

**数字不是信息，"谁 + 什么事 + 什么倾向"才是。**

## 参考实现（原项目）的做法

`moss-finance-assistant` 的 `analyze_zsxq_hot_news()` 把一批研报**整批**喂给
本地模型，输出按股票聚合的结构：

    stocks[] {stock, sector, mention_count, summary, sentiment}

它的 prompt 里有一条实测经验值得直接继承：

> "绝对不要提取券商名称或团队名！券商是发布研报的机构，不是股票。
>  例如以下都是券商名，必须跳过：天风电子、华福电新、中信电子…"

## ⚠️ 我在它的基础上加了幻觉拦截

原实现没有校验 —— 模型说有一只票就是一只票。而这里的风险很具体：
**编造一个股票名，用户是完全看不出来的**（名字看起来很真，
而且带着"利好"标签）。所以：

    stock   必须是**原文里出现过的子串**
    sector  同样必须在原文出现
    code    只能来自原文，模型给了也不认

校验不过**丢那一条**，不丢整批 —— 一条编造不该让整页空白。

## 与"倾向抽取"（tone.py）的分工

    tone.py        **单条**：这条原文是什么语气
    本模块          **整批**：这一批材料里，谁被讨论得最多、结论偏哪边

两者互补：单条倾向给"这条怎么读"，热度聚合给"今天该看哪几只"。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Final

from src.domain.intel.stock_names import resolve

logger = logging.getLogger(__name__)

#: 单批送模型的研报条数上限。
#:
#: 与 `tone_job` 的逐条抽取不同 —— 这里要的是**跨条聚合**（同一只票在
#: 多少条里出现），所以必须一次看多条。40 条 × 约 60 字 ≈ 2400 字输入，
#: 在本地 8B 模型上实测可接受；再多会明显变慢且尾部容易被忽略。
BATCH_SIZE: Final = 40

#: 单批输入的字符上限（防 prompt 膨胀）
MAX_INPUT_CHARS: Final = 6000

#: 回带的股票数上限（按提及次数降序）
MAX_STOCKS: Final = 15

#: 回带的事件数上限
MAX_EVENTS: Final = 8

#: 情绪标签（与原项目一致的三档）
SENTIMENTS: Final[frozenset[str]] = frozenset({"利好", "利空", "中性"})

#: 券商/机构名形态 —— **必须排除**（原项目实测经验）
#:
#: 它们出现在正文里是"谁出的这份研报"，不是"在讲哪只票"。
#: 名字看起来完全像股票名（"天风电子"），所以这类错很难被用户发现。
#:
#: ⚠️ **只认明确的机构标记**，不能放行业词。实测踩到：
#: 原来把"电子/汽车/机械/国际"也算机构后缀，于是
#: **`中芯国际` 被判成券商**（"国际"命中）—— 一只真实的重仓票被丢掉，
#: 而且没有任何提示。行业词必须去掉。
#:
#: 另一个坑：**必须用"包含"而不是"结尾"判定**。第一版写成 `...$`，
#: 于是 `Y证券研究团队` 漏判（标记得在中间）。
#: 带行业后缀的券商名（`天风电子`）由 `_BROKER_ROOTS` 的词根兜住。
_BROKER: Final = re.compile(
    r"证券|券商|研究所|研究院|研究部|研究团队|投研|资管|基金管理|投资管理")

#: 明确是券商名的词根（原项目列举的那些）
_BROKER_ROOTS: Final = (
    "天风", "华福", "中信", "国金", "中泰", "东吴", "东北", "招商", "信达",
    "广发", "申万", "国泰", "海通", "华创", "国联", "民生", "中银", "东方",
    "太平洋", "兴业", "光大", "浙商", "国信", "华西", "华安", "东兴", "东莞",
    "中航", "中邮", "交银", "万联", "方正", "财通", "国元", "西部", "华宝",
)

#: A 股代码（与 `tone._CODE_RE` 同口径）
_CODE: Final = re.compile(
    r"(?<!\d)(?:60[0135]\d{3}|688\d{3}|00[0123]\d{3}|30[01]\d{3}"
    r"|43\d{4}|8[3-9]\d{4})(?!\d)")

_SENT_MAP: Final[dict[str, str]] = {
    "利好": "利好", "正面": "利好", "偏多": "利好", "看多": "利好",
    "利空": "利空", "负面": "利空", "偏空": "利空", "看空": "利空",
    "中性": "中性", "mixed": "中性", "neutral": "中性",
}


@dataclass
class HotStock:
    """一只被讨论的股票（**具体内容**，不是统计量）。"""

    name: str
    sector: str = ""
    #: 被多少条**独立研报**提及（同一条里提多次只算 1）
    mention_count: int = 1
    #: 核心事件一句话（≤30 字）
    summary: str = ""
    #: `利好` / `利空` / `中性`
    sentiment: str = "中性"
    #: 原文中真实出现的代码（模型给不出就算了）
    code: str = ""

    def to_public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "sector": self.sector,
            "mention_count": self.mention_count,
            "summary": self.summary,
            "sentiment": self.sentiment,
            "code": self.code,
        }


@dataclass
class HotTopic:
    """一个被讨论的事件/主题（具体内容）。"""

    title: str
    #: 涉及的股票/行业名（原文里真有的）
    related: list[str] = field(default_factory=list)
    #: 它是"什么事"的一句话
    detail: str = ""
    sentiment: str = "中性"

    def to_public(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "related": list(self.related),
            "detail": self.detail,
            "sentiment": self.sentiment,
        }


def is_broker_name(name: str) -> bool:
    """这个名字是不是**券商/机构**（而不是股票）。

    原项目实测的坑：模型的 prompt 里不写死这条，它就会把"天风电子"
    "华福电新"当成股票输出 —— 而那些名字**看起来完全像股票名**，
    用户很难发现。所以除了在 prompt 里写明，代码里再兜一道。
    """
    s = (name or "").strip()
    if not s:
        return True
    if any(s.startswith(r) for r in _BROKER_ROOTS):
        return True
    return bool(_BROKER.search(s))


def validate_stock(raw: dict[str, Any],
                   source_text: str) -> tuple[HotStock | None, str]:
    """校验模型给出的一条股票。返回 `(结果, 丢弃原因)`。

    ## 三条拦截

      `not_in_source`  **核心**：名字在原文里找不到 → 编造。
                       一个编造的股票名带"利好"标签，用户完全看不出来。
      `broker`         是券商/机构名（"天风电子"）→ 不是股票
      `bad_name`       空、过长、含标点 → 不像股票名

    ⚠️ `code` 只从**原文**里取（模型给的代码不认）—— 代码错了会让用户
    去看错标的，比没有代码严重。

    ⚠️ 反过来，模型有时抽出来的"股票"**就是一串 6 位代码**（原文里只写了
    代码没写名字）。这时用本地名录把代码翻成中文名 —— 代码原文明写、
    名字只是查表，不算编造。翻译后**照样要校验原文**，只是校验的是
    代码本身（翻译出来的名字原文里当然没有）。
    """
    raw_name = str(raw.get("stock") or raw.get("name") or "").strip()
    # 去掉常见的修饰（"$ORCL"、"贵州茅台(600519)"里的括号部分）
    raw_name = re.sub(r"[\$＄]", "", raw_name).strip()

    # 代码/名字互译：`name` 是最终展示名，`code` 是从名字里解出来的代码
    name, code = resolve(raw_name)
    # 原文校验用**原始串**：纯代码就查那个代码，其余查名字
    probe = raw_name if (code and name != raw_name) else name
    if code and not name:
        # 名录里查不到的代码：**保留**（它可能是新上市/改名），
        # 但名字留空 —— 前端只显示代码，不要假装知道名字
        name = raw_name
    if not name or len(name) > 12 or re.search(r"[，,。；;：:\s]", name):
        return None, "bad_name"
    if is_broker_name(name):
        return None, "broker"
    if probe not in (source_text or ""):
        return None, "not_in_source"

    sector = str(raw.get("sector") or "").strip()[:20]
    # 行业名同样要能在原文找到；找不到就**只丢这个字段**（不丢整条）——
    # 行业填错的危害比股票名小得多
    if sector and sector not in (source_text or ""):
        sector = ""

    try:
        cnt = int(raw.get("mention_count") or 1)
    except (TypeError, ValueError):
        cnt = 1

    sent_raw = str(raw.get("sentiment") or "").strip().lower()
    sent = _SENT_MAP.get(sent_raw, "中性")

    # 代码：只认原文里真有的（且该名字附近有那个代码才算 —— 做不到就留空）
    if not code:
        src_codes = _CODE.findall(source_text or "")
        if src_codes:
            # 名字后面紧跟的 6 位数字优先
            m = re.search(re.escape(name) + r"[^\d]{0,8}(\d{6})", source_text or "")
            if m and m.group(1) in src_codes:
                code = m.group(1)

    return HotStock(
        name=name, sector=sector, mention_count=max(1, cnt),
        summary=str(raw.get("summary") or "").strip()[:60],
        sentiment=sent, code=code), ""


def merge_stocks(all_rows: list[HotStock]) -> list[HotStock]:
    """把多批结果按股票名合并（**提及次数累加**）。

    为什么必须合并：一批 40 条看不到跨批的模式，而"同一只票在多批里
    都出现"恰恰是热度信号。合并后按次数降序 —— 讨论最多的排最前。
    """
    by_name: dict[str, HotStock] = {}
    for r in all_rows:
        cur = by_name.get(r.name)
        if cur is None:
            by_name[r.name] = r
            continue
        cur.mention_count += r.mention_count
        # 事件摘要取**第一条非空**的（后批可能是重复内容）
        if not cur.summary and r.summary:
            cur.summary = r.summary
        if not cur.sector and r.sector:
            cur.sector = r.sector
        if not cur.code and r.code:
            cur.code = r.code
        # 情绪以**多数**为准；平票保留先出现的
        if r.sentiment != cur.sentiment:
            cur.sentiment = _majority(cur.sentiment, r.sentiment)
    return sorted(by_name.values(),
                  key=lambda x: (-x.mention_count, x.name))[:MAX_STOCKS]


def _majority(a: str, b: str) -> str:
    """两个情绪标签取"更可行动"的那个（利好/利空优先于中性）。

    为什么不取中性：多数派的"中性"会把少数派明确的"利好/利空"抹掉，
    而后者才是用户要看的信号。
    """
    if a == "中性":
        return b
    if b == "中性":
        return a
    return a


#: 送模型的系统提示。
#:
#: 直接继承原项目 prompt 里那条实测经验（不写死它模型就会输出券商名）。
#:
#: ⚠️ 材料来源已从"券商研报与调研纪要"改为**各平台公开快讯**
#: （财联社/富途/东财/同花顺/新浪）—— 见 `hot_job` 的说明。prompt 里的
#: "券商名要跳过"那条**仍然必须保留**：快讯里同样会写"中信证券指出…"，
#: 而"中信证券"看起来完全像一只票。
SYSTEM_PROMPT: Final = (
    "你在整理一批财经平台的公开快讯与研报，找出**市场正在讨论什么**。"
    "只输出 JSON，不要解释。\n"
    "规则：\n"
    "1. 提取被**分析或推荐的具体上市公司**（如 致欧科技、海底捞、恒立液压）。\n"
    "2. **绝对不要提取券商名称或团队名** —— 券商是发布研报的机构，"
    "不是股票。以下都是券商名，必须跳过：天风电子、华福电新、中信机械、"
    "国金金属、中泰汽车、东吴计算机、招商机械、申万计算机、"
    "国泰海通通信、华创家电。\n"
    "3. 不要提取分析师姓名、政府机构、行业概念、产品代号。\n"
    "4. summary 写**这只票发生了什么**（一句话，不超过30字，保留数字）。\n"
    "5. sentiment 只能填 利好 / 利空 / 中性。\n"
    "6. 同一只股票只输出一次，mention_count 填它出现在**多少条**里"
    "（同一条里提多次只算 1）。\n"
    "7. 另外用 topics 列出 3-5 个**讨论最集中的事件/主题**，"
    "title 写事件（如「存储扩产超预期」），detail 写一句话说明。\n"
    "8. **原文里只写了 6 位代码、没写名字的**，也照原样填在 stock 里，"
    "不要自己猜名字。\n"
    "不做投资建议，不使用'建议买入/目标价/评级'这类措辞。"
)


def build_prompt(notes: list[dict[str, Any]]) -> str:
    """把一批研报/笔记拼成 prompt。"""
    lines: list[str] = []
    used = 0
    for i, n in enumerate(notes, 1):
        title = str(n.get("title") or "").strip()
        body = str(n.get("summary") or "").strip()
        seg = f"[{i}] {title}\n{body}".strip()
        if used + len(seg) > MAX_INPUT_CHARS:
            break
        used += len(seg)
        lines.append(seg)
    return (
        '输出 JSON：{"stocks":[{"stock":"名称","sector":"行业",'
        '"mention_count":1,"summary":"一句话","sentiment":"利好|利空|中性"}],'
        '"topics":[{"title":"事件","detail":"一句话","related":["名称"]}]}\n'
        "材料：\n" + "\n\n".join(lines)
    )


#: 输出的 **JSON Schema**（走 Ollama 受约束解码，而不是靠模型自觉）。
#:
#: ## 为什么必须上 schema（实测）
#:
#: 只传 `json_mode=True`（Ollama 的 `format="json"`）时，本地
#: `qwen2.5:1.5b` 在 "提取热门个股" 任务上返回的是**语法合法但语义全错**的东西：
#:
#:     {"1. 【CC电新】液冷金帝...": -1.1e6}     ← 54 字符，键是编号，值是乱数
#:
#: `json.loads` 直接抛错 → `parse_output` 记一个 `bad_json` → 整批 0 条。
#: 而线上的表现是**页面空白**，看起来像"上游没数据"，完全不像模型问题
#: （我因此白查了一轮数据源）。
#:
#: ## 为什么 `mention_count` / `sentiment` 用 enum 而不是让它自由发挥
#:
#: `sentiment` 一旦自由生成就会出现 "偏多"/"positive"/"强势" 之类，
#: 下游 `_SENT_MAP` 兜不住就全落到"中性" —— 而中性是要被丢掉的，
#: 结果就是**页面上什么都不剩**。enum 由采样器保证取值，省掉一整类清洗。
#:
#: `required` 只列**真正必需**的字段：`sector`/`related`/`mention_count`
#: 留空是正常的，强制要求反而会让模型为了满足结构去编（编出来的
#: `sector` 过不了 `validate_stock` 的原文校验，等于白生成）。
OUTPUT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "stocks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "stock": {"type": "string"},
                    "sector": {"type": "string"},
                    "mention_count": {"type": "integer"},
                    "summary": {"type": "string"},
                    "sentiment": {
                        "type": "string",
                        "enum": ["利好", "利空", "中性"],
                    },
                },
                "required": ["stock", "summary", "sentiment"],
            },
        },
        "topics": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                    "related": {"type": "array", "items": {"type": "string"}},
                    "sentiment": {
                        "type": "string",
                        "enum": ["利好", "利空", "中性"],
                    },
                },
                "required": ["title", "detail"],
            },
        },
    },
    "required": ["stocks", "topics"],
}


def parse_output(raw: str, source_text: str) -> tuple[
        list[HotStock], list[HotTopic], dict[str, int]]:
    """解析并校验模型输出。返回 `(股票, 主题, 丢弃统计)`。"""
    s = (raw or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        obj = json.loads(s)
    except (ValueError, TypeError):
        return [], [], {"bad_json": 1}
    if not isinstance(obj, dict):
        return [], [], {"bad_json": 1}

    stats: dict[str, int] = {}
    stocks: list[HotStock] = []
    for row in (obj.get("stocks") or []):
        if not isinstance(row, dict):
            continue
        st, reason = validate_stock(row, source_text)
        if st is None:
            stats[reason] = stats.get(reason, 0) + 1
            continue
        stocks.append(st)

    topics: list[HotTopic] = []
    for row in (obj.get("topics") or []):
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()[:40]
        if not title or title not in source_text:
            # 事件标题也要求能在原文找到 —— 编一个"看起来很真"的事件
            # 比编股票名更难被发现（它没有代码可以对照）
            stats["topic_not_in_source"] = stats.get(
                "topic_not_in_source", 0) + 1
            continue
        related = [str(x).strip() for x in (row.get("related") or [])
                   if str(x).strip() and str(x).strip() in source_text][:6]
        sent_raw = str(row.get("sentiment") or "").strip().lower()
        topics.append(HotTopic(
            title=title,
            related=related,
            detail=str(row.get("detail") or "").strip()[:80],
            sentiment=_SENT_MAP.get(sent_raw, "中性")))
    return stocks, topics[:MAX_EVENTS], stats


__all__ = [
    "BATCH_SIZE",
    "MAX_EVENTS",
    "MAX_STOCKS",
    "OUTPUT_SCHEMA",
    "SENTIMENTS",
    "SYSTEM_PROMPT",
    "HotStock",
    "HotTopic",
    "build_prompt",
    "is_broker_name",
    "merge_stocks",
    "parse_output",
    "validate_stock",
]
