"""情报内容过滤：挡掉**无关**与**无内容**的条目，并清理格式噪音。

> 用户口径（2026-09-25）："这知识星球爬取的数据，什么都没有也显示了，
> 只有'#文字图片信息'，这种就直接过滤掉不显示了，还有内容中出现
> WD调研、礼物 等这些无关个股、行业、政策的信息，都要过滤掉。
> 用简易字符过滤或本地小模型快速过滤精简都可以。"

## 为什么用**规则**而不是本地模型

用户给了两个选项（规则 / 小模型），选规则的三个理由：

1. **这类噪音是格式性的，不是语义性的** —— "#文字图片信息" 空条目、
   `[玫瑰]` `[红包]` 表情标记、`#标签` 井号，用正则就能百分之百拦住，
   而模型会**偶尔漏**（同一份数据跑两次结果不同）。
2. **它在流水线最前面**，每条都要过。规则是微秒级、零算力，
   模型是 770ms/条 —— 60 条就是 46 秒，而这些条目**大多数注定要被丢掉**。
   用模型做粗筛是把最贵的东西放在最前面。
3. **可解释、可复算**：剥掉了什么、为什么剥，都能逐条对上。
   模型只会给一个"无关"的判断，用户没法质疑它。

## 分工：这一步只做"格式噪音 + 明显无关"

**语义判断（这条讲的是不是市场相关的事）留给模型** ——
`garbage` / `emoji` / `too_short` / `no_signal` 四类都能由规则确定，
而"通篇是运营话术"那种要看语义，不在本模块的职责里。
这样分开之后，模型那一层只需要处理规则筛剩下的少量条目。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Final

#: 内容（标题+正文）短于这个字符数就认为"没有实质内容"。
#:
#: 实测知识星球有大量只有 `#文字图片信息` 或一张图配几个字的条目 ——
#: 它们在情报流里占一行，点开什么都没有。50 是保守值：
#: 一条真正有信息的快讯（"某公司中标12.5亿元订单，同比+30%"）约 25 字，
#: 但那种走的是 newswire 通道，研究笔记的正文通常远超 50 字。
MIN_CONTENT_CHARS: Final = 50

#: 标题里只要**全是**这些内容就当无标题（配合短正文判为无内容）
_TITLE_JUNK: Final = re.compile(
    r"^[\s#＃\-—_=*·.、,，。!！?？+~～/\\|()（）\[\]【】]*$")

#: 纯"标签/占位"标题（实测形态）。大小写不敏感，允许前后缀空白。
_PLACEHOLDER_TITLE: Final = re.compile(
    r"^(?:[#＃](?:文字图片信息|图片信息|文字信息|图片|视频|链接|分享)"
    r"|文字图片信息|图片信息|视频分享)$", re.I)

#: 格式噪音字符（**只清理，不作为丢弃依据**）
#:
#: 实测来源用表情做小标题的项目符号：
#:   `[玫瑰]1、国产化…` / `[红包]煤价秋季…` / `❗当前公司货币现金9.65亿元`
#: `[xx]` 形态在中文财经文本里几乎只会是表情标记（真括号引用会写成（xx）或【xx】），
#: 所以可以安全剥掉。井号是平台的标签语法，留一个新号更乱。
_EMOJI_TAG: Final = re.compile(r"\[[^\]\n]{1,6}\]")
_HASH_TAG: Final = re.compile(r"[#＃](?=\S)")
#: 平台自带的"展开全文"之类尾巴
_TAIL_NOISE: Final = re.compile(r"(?:\.{3}|…)?\s*(?:展开全文|全文|阅读全文)\s*$")

#: **明确与个股/行业/政策无关**的平台运营内容。
#:
#: 用户点名的："内容中出现 WD调研、礼物 等这些无关个股、行业、政策的信息"。
#:
#: ⚠️ 分两类，处理方式**不同**：
#:
#:   · **运营话术**（星球福利/加群/打赏/限时优惠）→ 整条丢掉。
#:     它不是信息，是广告。
#:   · **星球署名**（群名，如 `WD调研`）→ **只剥掉字样，不丢条目**。
#:     知识星球的帖子常以群名开头做署名，那一条本身可能是有效研报
#:     （实测保留的条目全是「【广发机械】…」「【申万计算机】…」这种署名形态）。
#:     为署名丢掉整条会把真内容一起杀了。
_OPERATIONAL: Final = re.compile(
    r"(?:星球|圈子)(?:福利|活动|优惠|续费|新人)|"
    r"(?:加入|进入|扫码)(?:星球|圈子|群)|"
    r"限时(?:优惠|折扣|活动)|"
    r"(?:领取|赠送|送)(?:福利|资料|礼包|课程)|"
    r"打赏|赞赏|红包封面|"
    r"(?:点击|戳)(?:链接|阅读原文)")

#: 星球署名/群名（**剥掉字样，保留条目**）。
#:
#: 群名是**可变配置**，所以这里不写死某一个名字 —— 写死等于换个星球就失效，
#: 而且把真实群名明文写进代码本身就不该做（那正是要保护的资产）。
#: 用"XX调研 / XX纪要 / XX投研"这种**形态**去认。
#:
#: ⚠️ 三个实测踩到的点：
#:
#:   1. 后界必须含**冒号** —— 真实形态是 `WD调研：半导体设备…`，
#:      只认空白/括号的话一个字都剥不掉（第一版就是这样）。
#:   2. 不能用通用的 `研究` —— `【某某证券研究所】` 是**真实署名**，
#:      剥掉它等于把"这是哪家研报"这个溯源信息删了。
#:      只用 `调研 / 纪要 / 投研` 这三个平台群名常用的词。
#:   3. 署名必须**含字母/数字**（`WD调研`）**或带平台前缀**
#:      （`群纪要` / `星球纪要` / `圈投研`）。纯中文且无前缀的
#:      `市场调研` / `行业纪要` 是**章节名**而不是署名，剥掉会让正文
#:      读不通（"：半导体设备再强调"开头很怪）。这两条判据自然分开两类，
#:      不需要枚举词表。
#:
#: 前缀 `群|星球|圈` 与名字部分**分开写**，不能合并进同一个 `{2,10}`
#: —— 合并后 `群纪要`（群 + 2 字）会因为名字部分只剩 0 字而匹配不上，
#: 而那正是要剥的形态之一。
_BYLINE: Final = re.compile(
    r"(?:^|[\s\n（(【\[])((?:"
    # 分支 A：带平台前缀 → 前缀后的名字可以全是中文
    r"(?:群|星球|圈)[\u4e00-\u9fffA-Za-z0-9]{0,10}?(?:调研|纪要|投研)"
    r"|"
    # 分支 B：无前缀 → 必须含字母/数字（区分"WD调研"与"市场调研"）
    r"(?=[\u4e00-\u9fffA-Za-z0-9]{1,12}?(?:调研|纪要|投研))"
    r"(?=[^：:\s]{0,12}[A-Za-z0-9])"
    r"[\u4e00-\u9fffA-Za-z0-9]{1,12}?(?:调研|纪要|投研)"
    r"))(?=[\s\n：:，,。.、）)】\]]|$)")

#: 市场信号词：个股 / 行业 / 政策 / 数据。
#:
#: 用途见 `looks_market_related()` —— **只作为"值得送模型"的旁证**，
#: 不作为丢弃依据（宁可多送几条给模型，也不要在这里误杀）。
_SIGNAL: Final = re.compile(
    r"(?:公告|业绩|营收|净利|毛利|订单|中标|产能|扩产|排产|价格|涨价|降价|"
    r"库存|供需|政策|补贴|规划|试点|审批|获批|监管|处罚|减值|减持|回购|"
    r"解禁|并购|重组|投建|投产|量产|渗透率|市占率|国产化|出口|进口|关税|"
    r"板块|行业|指数|龙头|份额|同比|环比|%|亿元|万吨)")


@dataclass
class CleanResult:
    """清洗结果。`keep=False` 的条目**不进入**后续流水线。"""

    keep: bool
    #: 清洗后的标题与正文（格式噪音已剥离）
    title: str = ""
    text: str = ""
    #: 丢弃原因（`too_short` / `placeholder` / `operational` / `empty`）
    reason: str = ""
    #: 命中的规则名（便于审计"为什么这条被丢了"）
    hits: list[str] = field(default_factory=list)


def strip_noise(text: str) -> str:
    """剥掉格式噪音（表情标记、井号、展开全文尾巴、星球署名、多余空行）。

    ⚠️ **只清理，不判断** —— 调用方据此决定要不要丢。
    """
    s = text or ""
    if not s:
        return ""
    s = _EMOJI_TAG.sub(" ", s)
    s = _HASH_TAG.sub("", s)
    s = _TAIL_NOISE.sub("", s)
    # 星球署名/群名：**只剥字样**（见 `_BYLINE` 的说明 ——
    # 为署名丢掉整条会把真内容一起杀了）
    s = _BYLINE.sub(lambda m: m.group(0).replace(m.group(1), " "), s)
    # 统一空白：知识星球的正文有大量 `\n \n\n` 与全角空格
    s = re.sub(r"[ \t\u3000]{2,}", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def looks_market_related(text: str) -> bool:
    """文本里有没有市场信号词。

    **不作为丢弃依据** —— 一条没有命中信号词的内容也可能是有效信息
    （例如"某公司实控人变更"）。它的用途是给调用方一个旁证：
    规则层认为"明显无关"时可以据此更放心地丢。
    """
    return bool(_SIGNAL.search(text or ""))


def clean(title: str, text: str, *, kind: str = "") -> CleanResult:
    """清洗一条情报，返回"要不要留 + 清洗后的文本"。

    ## 判定顺序（先便宜后贵，且先"确定无关"后"内容不足"）

      1. `placeholder`  标题是纯占位（`#文字图片信息`）→ 丢
      2. `operational`  正文是平台运营话术（星球福利/加群/打赏）→ 丢
      3. `empty`        清洗后连标题带正文都不足 `MIN_CONTENT_CHARS` → 丢

    三条都是**能明确判定**的，所以用规则；判不了的（语义是否相关）
    交给下游模型，不在这一步猜。
    """
    t_raw = (title or "").strip()
    s_raw = (text or "").strip()
    t = strip_noise(t_raw)
    s = strip_noise(s_raw)
    hits: list[str] = []

    # ① 纯占位标题
    if t_raw and _PLACEHOLDER_TITLE.match(t_raw):
        return CleanResult(False, t, s, "placeholder", ["placeholder_title"])
    if t_raw and _TITLE_JUNK.match(t_raw) and len(s) < MIN_CONTENT_CHARS:
        return CleanResult(False, t, s, "placeholder", ["junk_title"])

    # ② 平台运营话术（用户点名的"WD调研、礼物"这类）
    if _OPERATIONAL.search(f"{t} {s}"):
        return CleanResult(False, t, s, "operational", ["operational_terms"])

    # ③ 内容不足
    body = f"{t} {s}".strip()
    if not body:
        return CleanResult(False, t, s, "empty", ["no_text"])
    if len(body) < MIN_CONTENT_CHARS:
        return CleanResult(False, t, s, "too_short", ["short_content"])

    return CleanResult(True, t, s, "", hits)


def filter_items(items: list[dict[str, Any]], *,
                 log: list[dict[str, Any]] | None = None
                 ) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """对"已出接口形状"的情报条目做过滤（就地清洗标题/摘要）。

    ## 为什么在**契约之后**做

    输入是 `IntelItem.to_public()` 的输出。这样：
      · 清洗后的文本就是用户看到的文本，不会出现"库里是脏的、
        界面上是干净的"这种两处不一致
      · `content_hash` 已经算完了 —— 过滤**不改变**指纹，
        所以游标与去重不受影响（改指纹会让同一条内容反复入库）

    `log` 传入时会追加被丢弃条目的原因（供管理员排障：
    "今天为什么只有 3 条笔记"）。
    """
    out: list[dict[str, Any]] = []
    stats: dict[str, int] = {}
    for it in items:
        res = clean(str(it.get("title") or ""), str(it.get("summary") or ""),
                    kind=str(it.get("kind") or ""))
        if not res.keep:
            stats[res.reason] = stats.get(res.reason, 0) + 1
            if log is not None:
                log.append({
                    "kind": it.get("kind"),
                    "reason": res.reason,
                    "hits": res.hits,
                    # ⚠️ 只记**长度**与原因，不记原文 —— 原文可能夹带上游标识
                    "title_len": len(str(it.get("title") or "")),
                })
            continue
        it["title"] = res.title
        it["summary"] = res.text
        out.append(it)
    return out, stats


__all__ = [
    "MIN_CONTENT_CHARS",
    "CleanResult",
    "clean",
    "filter_items",
    "looks_market_related",
    "strip_noise",
]
