"""热议个股扫描：用**本地股票名录**在平台快讯里做确定性匹配（不靠模型猜）。

> 用户口径（2026-09-25）：
> "把几大平台（雪球/东方财富股吧/同花顺/财联社/百度人气榜/韭研公社）的
>  热点事件和个股，聚合汇总显示在情报流或事件告警里。"

## 为什么不用模型抽股票（上一版就是这么做的，效果很差）

上一版把一批快讯整个喂给本地 `qwen2.5:1.5b`，让它输出 `stocks[]`。
实测（2026-09-25，160 条真实快讯）：

    {"notes": 80, "batches": 2, "stocks": 1, "topics": 1,
     "rejected": {"bad_json": 1}}

**两批里有一批连 JSON 都没输出，另一批只抽出 1 只票，还是港股**
（瑞声科技 02018.HK —— 而这一页要的是 A 股）。原因不神秘：快讯里绝大多数
是宏观/海外内容（IMF、诺基亚、巴基斯坦股市），A 股名字**很少**，
1.5B 模型在这种稀疏输入上基本是抛硬币。

而"哪些票被提到了"这件事**根本不需要模型判断** —— 本地名录里有
5567 只 A 股的准确名字。用字符串匹配：

  · 精确（名字对得上就是对得上，不会编）；
  · 免费（不开模型，秒级）；
  · 可解释（能给出"是在哪条里提到的"）。

模型只留给它真正擅长、且无法确定性求解的部分 —— **一句话摘要**
与 **利好/利空归类**（走 `tone.py`，且只对信度达标的条目做）。

## 匹配规则（**宁可漏，不可错**）

| 形态 | 规则 | 理由 |
|---|---|---|
| 名称 ≥4 字 | 直接命中 | 4734/5567 只都是 4 字，几乎不会与日常词撞车 |
| 名称 3 字 | 需**同条内还有该股 6 位代码**，或名字**出现 ≥2 次** | 3 字名里有"农产品""金融街""张家界"这类日常词，直接匹配必然误报 |
| 6 位代码 | 直接命中（按 A 股前缀校验）| 最高置信度 |

为什么误报的代价必须这么高：**编一只票用户看不出来**。他会以为
"今天在热议 XX"，而原文根本没提 —— 这正是这一页上一版被投诉的原因
（"作为一个投资者没获得任何有用信息"）。

## 与 `hot_topics` / `hot_job` 的关系

    hot_scan（本模块）  **确定性**：谁被提到了、在哪几条、哪些平台
    hot_job             **模型侧**：只负责事件聚类与倾向，不负责认名字

两者输出的股票列表会合并去重（`hot_scan` 的结果优先，因为它可核对）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Final

logger = logging.getLogger(__name__)

#: 回带的个股数上限
MAX_STOCKS: Final = 20

#: 直接匹配的名称长度门槛（见模块 docstring 的规则表）
STRICT_NAME_LEN: Final = 4

#: 3 字名的**例外白名单**：不需要代码佐证也认。
#:
#: ## 为什么 3 字名默认不认（这条规则是被一次真实误报逼出来的）
#:
#: 之前 3 字名的规则是"同条有代码 **或** 出现 ≥2 次"。实测立刻抓到一个：
#:
#:     标题：梅卡曼德机器人亏损收窄 / 宇树科技王兴兴：机器人核心瓶颈…
#:     →  命中「机器人」(300024)
#:
#: `机器人` 确实是一只 A 股（新松机器人曾用名），但这两条说的是
#: "机器人"这个**行业词**。这类误报极难发现 —— 名字是真的、代码也是真的，
#: 只有语境是错的。同类日常词还有：`太阳能`、`大西洋`、`太平洋`、
#: `老百姓`、`指南针`、`向日葵`、`黑芝麻`、`农产品`、`金融街`…
#: 719 个 3 字名里有几十个是常用词，**逐个维护黑名单不可靠**。
#:
#: 所以默认收紧成"必须有代码"，只对下面这些**公认不会与日常词撞车**的
#: 高关注度个股开口子。方向是"宁可漏，不可错"：漏掉一只 3 字票，
#: 用户只是少看一条；认错一只，用户会以为原文在说它。
_PROMINENT_3: Final[frozenset[str]] = frozenset({
    # 白酒/食品
    "五粮液", "金龙鱼", "水井坊", "口子窖", "今世缘", "金徽酒",
    # 医药/医疗
    "片仔癀", "同仁堂", "白云山", "健康元", "天士力", "康恩贝", "马应龙",
    "九州通", "大参林", "康希诺", "美迪西", "艾力斯",
    # 新能源/汽车
    "比亚迪", "赛力斯", "亿华通", "阿特斯", "固德威", "福莱特", "福斯特",
    "贝特瑞", "欣旺达", "新宙邦", "特锐德",
    # 半导体/电子
    "寒武纪", "华润微", "立昂微", "芯源微", "纳芯微", "思瑞浦", "格科微",
    "景嘉微", "卓胜微", "新易盛", "欧菲光", "凌云光", "铂力特", "奥特维",
    "利元亨", "海目星",
    # 软件/互联网/消费
    "三六零", "深信服", "奇安信", "优刻得", "科沃斯", "珀莱雅", "爱美客",
    "老凤祥", "王府井", "美凯龙", "苏泊尔", "索菲亚", "吉比特", "拉卡拉",
    "埃夫特", "倍轻松",
})

#: 3 字名需要"额外证据"时的最小同条出现次数（仅对非白名单名称不再使用，
#: 保留常量是为了文档化曾经的规则与测试引用）
AMBIGUOUS_MIN_HITS: Final = 2

#: 每只票最多回带几条原文标题（给用户核对用）
MAX_EVIDENCE: Final = 3

#: A 股代码（与 `tone._CODE_RE` 同口径）
_CODE: Final = re.compile(
    r"(?<!\d)(?:60[0135]\d{3}|688\d{3}|00[0123]\d{3}|30[01]\d{3}"
    r"|43\d{4}|8[3-9]\d{4})(?!\d)")

#: **可公开**的平台标签（内部源名 → 平台名）。
#:
#: 用户口径（2026-09-25）："公开数据的地方（AkShare/腾讯/新浪/东财/QMT）
#: 可以暴露"，只有平台/群身份（知识星球调研）要藏。所以这里**只**映射
#: 公开财经平台；`research-note-zsxq` 与 `broker-*` 一律**不映射**，
#: 落到 `_FALLBACK_PLATFORM`，对外统一显示为"公开财经信息"。
_PLATFORM_LABELS: Final[dict[str, str]] = {
    "newswire-em": "东方财富",
    "newswire-ths": "同花顺",
    "newswire-sina": "新浪财经",
    "newswire-cls": "财联社",
    "newswire-futu": "富途",
    "policy-cctv": "新闻联播",
}

#: 未映射来源的对外标签（**不含来源标识**）
_FALLBACK_PLATFORM: Final = "公开财经信息"

#: 名称 → 代码 索引（进程内缓存）
_NAME_INDEX: dict[str, str] = {}
#: 代码 → 名称 索引（同一个名录的反向，用于"只写了代码"的条目）
_CODE_INDEX: dict[str, str] = {}
#: 一次性编译的匹配模式（名字按长度降序，保证最长优先）
_PATTERN: re.Pattern[str] | None = None
_INDEX_SOURCE: int = -1


@dataclass
class StockHit:
    """一只被平台快讯提到的 A 股（**全部字段可核对**）。"""

    name: str
    code: str
    #: 被多少条**不同快讯**提到（同一条里提多次只算 1）
    mention_count: int = 0
    #: 提到它的平台（**可公开**平台名，按出现次数降序）
    platforms: list[str] = field(default_factory=list)
    #: 最早/最新一次出现时间（原样透传，不解析）
    first_seen: str = ""
    last_seen: str = ""
    #: 原文标题（≤3 条，给用户核对"到底说的是什么"）
    evidence: list[str] = field(default_factory=list)
    #: 主要来源类型（`newswire` / `policy` / ...）
    kinds: list[str] = field(default_factory=list)

    def to_public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "code": self.code,
            "mention_count": self.mention_count,
            "platforms": list(self.platforms),
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "evidence": list(self.evidence),
            "kinds": list(self.kinds),
        }


def platform_of(source_alias: str) -> str:
    """内部源名 → **可公开**平台名。未映射的一律给中性标签。

    ⚠️ 情报条目过接口时 `source_alias` 已被 `source_pseudonym()` 换成
    `src-xxxxxxxx`，所以这里**查不到**。调用方应优先用条目自带的
    `platform` 字段（契约层按 `PUBLIC_PLATFORMS` 白名单给的）。
    这个函数只作为"直接扫内部条目"（不经接口）时的兜底。
    """
    return _PLATFORM_LABELS.get(str(source_alias or "").strip(),
                                _FALLBACK_PLATFORM)


def _build_index() -> dict[str, str]:
    """从本地名录建 `名称 → 代码` 索引（进程内缓存一次）。"""
    global _NAME_INDEX, _CODE_INDEX, _PATTERN, _INDEX_SOURCE

    from src.domain.intel.stock_names import _load

    table = _load()
    if len(table) == _INDEX_SOURCE and _PATTERN is not None:
        return _NAME_INDEX

    index: dict[str, str] = {}
    by_code: dict[str, str] = {}
    for code, name in table.items():
        n = str(name or "").strip()
        # 名录里唯一一个 2 字名（柳工）也收 —— 它不会与日常词撞车
        if len(n) < 3:
            if len(n) == 2:
                index[n] = code
                by_code.setdefault(code, n)
            continue
        index[n] = code
        by_code.setdefault(code, n)
    _NAME_INDEX = index
    _CODE_INDEX = by_code
    if index:
        # 长度降序 → Python 的 `|` 是有序选择，长名优先，
        # 避免 "中国石油" 被 "中国石化" 之类的前缀抢先匹配
        ordered = sorted(index, key=lambda s: (-len(s), s))
        _PATTERN = re.compile("|".join(re.escape(s) for s in ordered))
    else:
        _PATTERN = None
    _INDEX_SOURCE = len(table)
    logger.debug("热议扫描索引 %d 个名称", len(index))
    return _NAME_INDEX


def find_names(text: str) -> list[str]:
    """在一段文本里找出所有**名录中的 A 股名**（含 3 字名的额外证据规则）。

    返回去重后的名称列表（保持出现顺序）。
    """
    index = _build_index()
    if not text or _PATTERN is None:
        return []

    codes_in_text = set(_CODE.findall(text))
    out: list[str] = []
    seen: set[str] = set()
    for m in _PATTERN.finditer(text):
        n = m.group(0)
        if n in seen:
            continue
        code = index.get(n, "")
        if len(n) < STRICT_NAME_LEN:
            # 3 字（及个别 2 字）名：必须有代码佐证，白名单除外。
            # 见 `_PROMINENT_3` 的说明 —— 3 字名里混着大量日常词。
            if code not in codes_in_text and n not in _PROMINENT_3:
                continue
        if _is_broker_like(n, code, codes_in_text):
            # ⚠️ 实测抓到的误报：**券商名本身就是上市公司**。
            # `中信证券` 在名录里（600030），而快讯里"中信证券指出……"
            # 是在说**谁发的观点**，不是在讨论这只票。这类误报特别难发现：
            # 名字是真的、代码也是真的，只有"语境"是错的。
            # 规则：券商类名称必须**同条内出现它的代码**才算（那种情况
            # 是"中信证券(600030)发布业绩"这类真正的个股新闻）。
            continue
        seen.add(n)
        out.append(n)

    # 代码直接命中：补上"只写了代码没写名字"的那些
    for code in codes_in_text:
        n = _CODE_INDEX.get(code, "")
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _is_broker_like(name: str, code: str, codes_in_text: set[str]) -> bool:
    """这个名称是不是"以机构身份出现"（而不是被讨论的标的）。

    只有**券商类**名称需要这条规则 —— 它们既是机构名也是股票名，
    是名录匹配里唯一一类"名字对、语境错"的命中。
    """
    if not code:
        return False
    from src.domain.intel.hot_topics import is_broker_name

    if not is_broker_name(name):
        return False
    return code not in codes_in_text


def scan_items(items: list[dict[str, Any]], *,
               max_stocks: int = MAX_STOCKS) -> list[StockHit]:
    """扫一批情报条目，聚出"哪些 A 股正在被多个平台提到"。

    `items` 元素需含 `title` / `summary` / `published_at` / `source_alias`
    / `kind`（即 `build_feed` 的条目形态）。
    """
    index = _build_index()
    if not index:
        return []

    agg: dict[str, StockHit] = {}
    plat_count: dict[str, dict[str, int]] = {}

    # 按时间正序扫，这样 `last_seen` 天然是最后写入的那个
    for it in sorted(items, key=lambda x: str(x.get("published_at") or "")):
        title = str(it.get("title") or "")
        body = str(it.get("summary") or "")
        text = f"{title}\n{body}"
        if not text.strip():
            continue
        names = find_names(text)
        if not names:
            continue
        ts = str(it.get("published_at") or "")
        # 优先用契约层给的 `platform`（白名单过），拿不到再退回按源名映射。
        # 走接口的条目 `source_alias` 已被假名化，只能靠前者。
        plat = str(it.get("platform") or "").strip() or platform_of(
            str(it.get("source_alias") or ""))
        kind = str(it.get("kind") or "")

        for name in names:
            code = index.get(name, "")
            if not code:
                continue
            hit = agg.get(code)
            if hit is None:
                hit = StockHit(name=name, code=code, first_seen=ts)
                agg[code] = hit
                plat_count[code] = {}
            hit.mention_count += 1
            hit.last_seen = ts or hit.last_seen
            if not hit.first_seen:
                hit.first_seen = ts
            if plat != _FALLBACK_PLATFORM:
                pc = plat_count[code]
                pc[plat] = pc.get(plat, 0) + 1
            if kind and kind not in hit.kinds:
                hit.kinds.append(kind)
            head = title.strip()[:80]
            if head and head not in hit.evidence and len(hit.evidence) < MAX_EVIDENCE:
                hit.evidence.append(head)

    for code, hit in agg.items():
        pc = plat_count.get(code) or {}
        # 平台按"提到次数降序、名称升序"排（稳定，前端不会抖）
        hit.platforms = [p for p, _ in sorted(pc.items(),
                                             key=lambda kv: (-kv[1], kv[0]))]

    rows = sorted(agg.values(),
                  key=lambda x: (-x.mention_count, x.code))
    return rows[:max_stocks]


__all__ = [
    "AMBIGUOUS_MIN_HITS",
    "MAX_EVIDENCE",
    "MAX_STOCKS",
    "STRICT_NAME_LEN",
    "StockHit",
    "find_names",
    "platform_of",
    "scan_items",
]
