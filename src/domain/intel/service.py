"""舆情情报聚合服务 —— 把 6 个源合成一份**可出接口**的情报流。

## 六源（全部实测可用，任一挂掉不影响其它）

| 源 | 依赖 | 实测 |
|---|---|---|
| 券商研报 | 无 | 227 行/只，含评级/机构/盈利预测 |
| 东财快讯 | 无 | 200 条，当日 |
| 同花顺快讯 | 无 | 20 条 |
| 新浪快讯 | 无 | 20 条 |
| 新闻联播 | 无 | 15 条全文（政策信号） |
| 知识星球 | token（7–14 天） | 增量水位线，330ms |

## 三条硬约束

1. **来源标识不出接口** —— 只出 `source_alias`（稳定假名），
   真实 group_id / URL / 机构内部标识全部留在内部。
   契约层由 `IntelItem.to_public()` 白名单构造保证。
2. **失败不静默** —— 单源失败进 `gaps`，接口如实返回"哪一类源不可用"，
   **不写成"今天没有内容"**。这是本项目"不猜"口径的延续。
3. **用户侧不显示故障** —— `gaps` 只给管理员语义（"某来源暂无更新"），
   前端不得把内部源名/错误原文展示给普通用户。

## 为什么不做 LLM 分析在这一层

本层只做"聚合 + 归一 + 去重"，**不调模型**。理由：
  · 采集要快（接口 2 秒内出结果），模型分析慢（本地 8B ~30s/批）；
  · 分析放调度任务里做，把结果存起来给接口读 —— 这样接口稳定、
    模型升级/重跑不影响在线延迟。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final

logger = logging.getLogger(__name__)

#: 单次聚合的显示上限（防一次性把上百条推给前端）
DEFAULT_LIMIT: Final = 60

#: 研报按关注标的逐个拉，限量控频
MAX_BROKER_CODES: Final = 8

#: 自选清单（研报按标的拉，不传标的的话**一条研报都取不到**）
_WATCHLIST_PATH: Final = Path("configs/intraday.yaml")


def watchlist_codes(limit: int = MAX_BROKER_CODES) -> list[str]:
    """从自选清单取关注标的，供研报采集用。

    ## 为什么必须有这个兜底（实测踩到的）

    研报是**按标的**拉的（`watch_codes`），不是"全市场最新研报"那种接口。
    而 `/intel/feed` 的 `codes` 参数**从来没人传** —— 于是默认情况下
    池子里一条研报都没有，连带两个后果：

      · 「仅研报」筛选永远空
      · 「高可信 ≥80」也是空的 —— 因为整个池子的来源档最高只到 74
        （权威媒体档），而研报档是 84

    用户会以为"没有高可信信息""没有研报"，而实际是**根本没去取**。

    自选清单本来就是这个用途（做T面板在用同一份），直接复用；
    将来前端加了"关注标的"输入框，用它覆盖这里即可。
    """
    try:
        import yaml

        raw = yaml.safe_load(_WATCHLIST_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError, ImportError):
        return []
    out: list[str] = []
    for row in (raw.get("watchlist") or []):
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or "").strip()
        if code and code not in out:
            out.append(code)
        if len(out) >= limit:
            break
    return out


#: 取样池大小。`limit` 是**返回条数**，这是"从多少条里挑"。
#:
#: ⚠️ 必须有这么一个大于 `limit` 的池子，否则筛选项形同虚设：
#: 实测 `limit=60` 时池内最高分只有 78，于是「高可信 ≥80」**永远空**，
#: 而全量里其实有 30 条 80+ 的 —— 用户会以为"没有高可信信息"。
#: 取值也为可分页留余量（将来加"下一页"时不必再改这里）。
#: 情报流的**时效窗口**（天）。
#:
#: 用户口径（2026-09-25 修订）："抓取的信息只保留3天，超过日期的直接溢出丢弃。"
#:
#: ⚠️ 这条口径**改过一次**：上一版是"最多保留最近 7 天的新闻信息"。
#: 现在是 **3 天** —— 再往前的内容对追热点没有价值（它的价值在复盘，
#: 那是另一个页面的事）。这里保留这条修订记录，是为了避免下次有人
#: 看到别处的旧说明（文档、注释、界面文案）与常量对不上时改错方向。
#:
#: 超过这个天数的条目在 `build_feed` 里被丢弃，并在该类**全部落在窗口外**时
#: 记一条数据缺口（"最近 N 天无更新"由同一个常量推导，不静默消失）。
#:
#: 实测背景：东财研报按个股返回**全部历史报告**，未加窗口时 76 条研报
#: 横跨 2017→2026，把当天内容挤出了 `limit`。
FEED_WINDOW_DAYS: Final = 3

#: 无明确倾向的条目**至少**多少条才收成一组。
#:
#: 太少了不成组：给一个"1 条"的分组比直接显示那一条更烦人，
#: 而且"点开查看"在只有一条时没有任何意义。
MIN_UNDETERMINED_GROUP: Final = 6

#: 筛选池大小（**与 `limit` 是两件事**：limit 是返回条数，池子是取样范围）。
FILTER_POOL: Final = 500


@dataclass
class IntelFeed:
    """一次聚合结果。**字段刻意做窄**，不含任何来源标识。"""

    items: list[dict[str, Any]] = field(default_factory=list)
    gaps: list[dict[str, str]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    fetched_at: str = ""
    degraded: bool = False
    #: 可信度分层计数（**筛选之前**、于池子上统计）。
    #:
    #: 单列出来的理由：筛选 tab 的角标必须与当前档位**无关** ——
    #: 拿筛过的 `items` 去数会让"切一次 tab 所有角标都变"，
    #: 用户会以为数据在动。
    credibility_dist: dict[str, int] = field(default_factory=dict)
    #: 相似新闻聚合统计（簇数 / 涉及条数 / 被收起的条数）。
    #:
    #: 单列出来是为了**可观测**：聚合是"悄悄减少条数"的操作，
    #: 没有这个统计就没人能发现阈值配错了（比如把不相关的事合在一起）。
    cluster_stats: dict[str, int] = field(default_factory=dict)
    #: 原文倾向分布 {偏多: n, 偏空: n, 中性: n}。
    #:
    #: ⚠️ **只统计 has_tone 的条目** —— 把未定算进多空比，
    #: 等于替用户做了一个我们并不确定的判断。
    tone_dist: dict[str, int] = field(default_factory=dict)
    #: 多/空筛选器的角标 `{"bull": n, "bear": n}`（用户口径 2026-09-26）。
    #:
    #: ## 为什么不能复用 `tone_dist`
    #:
    #: `tone_dist` 是**全量分布**（在可信度筛选之前统计，热度页要用），
    #: 而这个选择器是在**当前可信度档位之内**工作 —— 用户切到「高可信」时，
    #: 「偏多 N」那个数必须跟着变小，否则他点进去会发现"说好的 30 条只有 4 条"。
    #:
    #: ⚠️ 两者都**与多空筛选无关**（统计在筛选之前）：角标必须与当前选中项
    #: 无关，否则选中「偏多」后「偏空」的角标变成 0，用户就没法切回去了。
    direction_dist: dict[str, int] = field(default_factory=dict)
    #: 被多空筛选挡掉的条数（只在真的筛了时非零）。
    #:
    #: 用途与 `neutral_hidden` 同类：筛选是"悄悄减少条数"的操作，
    #: 没有这个数就没人能看出"规则把该留的筛掉了"。
    direction_hidden: int = 0
    #: 内容过滤统计 {丢弃原因: 条数}。
    #:
    #: 单列出来是为了**可观测**：过滤是悄悄减少条数的操作，
    #: 没有这个统计就没人能发现规则误杀（"今天为什么只有 3 条笔记"）。
    filter_stats: dict[str, int] = field(default_factory=dict)
    #: **收容之前的平铺池**（可信度筛选之后、收容组之前）。
    #:
    #: ## 为什么必须带出来
    #:
    #: `items` 是**展示口径**：`limit` 截断 + 收容组把"给不出方向"的几十条
    #: 并成一行 —— 实测默认视图下 `items` 只剩 **2 条**。而热议扫描
    #: （`intel._build_heat` → `hot_scan`）要的是"有哪些真实条目"，
    #: 于是它发现池子太小，就**又整跑了一遍六源聚合**（实测 +1.4 秒/请求）。
    #: 带上这份池子，那次重复聚合就不需要了。
    #:
    #: ⚠️ **不出接口**：`to_public()` 不包含它（这是最长的一份数据，
    #: 500 条带全文）。只有进程内的调用方（`_build_heat`）读它。
    scan_pool: list[dict[str, Any]] = field(default_factory=list)

    def to_public(self) -> dict[str, Any]:
        """出口白名单：**逐条剥掉仅供进程内使用的字段**。

        ⚠️ `items` 原来是**整份原样透传**的（`"items": self.items`），
        于是任何挂在 item 上的内部键都会跟着出去。现在有两个内部键：

            extract_text   抽取入参（**清洗后的全文**，专供 `tone_job` 分段抽取）
            market_terms   全文里命中的市场词（折叠判据用；**词未必出现在
                           展示摘要里**，发出去前端也标不出来）

        不剥掉 `extract_text`，浏览器就会收到全文，正好抵消 `to_public()` 对
        `summary` 的 260 字展示截断，把"移动端一条占满十屏"放回来。

        ⚠️ 与之相对，`summary_text`（单行 + ≤200 字的展示摘要）是**要出接口**的
        —— 它不在这份剔除名单里，这是刻意的：它是用户要看的那一句话
        （见 `_summary_of`）。"新增内部字段"与"新增展示字段"在这一处的区别
        就是有没有把键名加进元组，**加错方向不会有任何报错**。

        用**黑名单式剔除**而不是重建每个 item：item 的字段由各来源的
        `IntelItem.to_public()` 白名单生成，这里只需要摘掉"后挂上去的内部键"。

        ⚠️ **两层都要剥**。收容组（"未给出明确倾向"那种）的子条目挂在
        `group_items` 里，是 `_group_row()` 生成的**另一个 dict** ——
        只剥顶层会让组内条目的全文照样发出去（实测 21 条研究笔记全在组内，
        只剥顶层等于没剥）。
        """
        #: 只给进程内用的键。**加新内部键时改这一处**（两层的剔除共用它，
        #: 免得顶层剥了、组内忘了 —— 那个漏法不会有任何报错）
        internal = ("extract_text", "market_terms")

        def _strip(item: dict[str, Any]) -> dict[str, Any]:
            out = {k: v for k, v in item.items() if k not in internal}
            members = out.get("group_items")
            if isinstance(members, list):
                out["group_items"] = [
                    {k: v for k, v in m.items() if k not in internal}
                    for m in members
                ]
            return out

        return {
            "items": [_strip(it) for it in self.items],
            "gaps": self.gaps,
            "counts": self.counts,
            "fetched_at": self.fetched_at,
            # 前端据此显示"数据不完整"提示（**不显示具体源名**）
            "degraded": self.degraded,
            "credibility_dist": self.credibility_dist,
            "cluster_stats": self.cluster_stats,
            "tone_dist": self.tone_dist,
            "direction_dist": self.direction_dist,
            "direction_hidden": self.direction_hidden,
            "filter_stats": self.filter_stats,
        }


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ======================================================================
# 展示摘要：**单行** + 一个 200 字的上限（用户口径 2026-10-01）
# ======================================================================

#: 情报流里**一条摘要的显示上限**（字符）。
#:
#: 用户口径（2026-10-01）：
#:
#: > "本地模型提取的信息，要求输出文字不能超过200个"
#: > "精简200字以内，用户没时间看全文，要效率"
#:
#: ⚠️ 200 是**天花板，不是目标**。用户要的是"一眼扫完"，
#: 所以模型侧的提示词仍然只要 ≤90 字（`summarize.MAX_SUMMARY_CHARS`）——
#: 把提示词放宽到 200 会让摘要**变长**，与"要效率"正好相反。
#: 这个数字的职责只有一个：兜住"模型没听话/老数据是长文本摘要"的情况。
DISPLAY_SUMMARY_MAX_CHARS: Final = 200

#: 展示层折叠空白时**压成什么**。
_WS_COLLAPSE_TO: Final = " "

#: 折叠掉所有空白（换行/回车/全角空格/连续空格）的正则。
#:
#: 用户口径（2026-10-01）："前端信息不要换行。"
#:
#: ## 为什么必须由**后端**折叠，而不是交给 CSS
#:
#: 前端的 `white-space` 只影响**怎么渲染**，不影响字符串里有没有换行：
#: 一条 260 字的星球笔记里常有 5~10 个换行，`white-space: nowrap` 会把
#: 它们压成一行但**不产生空格**，于是"半导体。\n光伏"渲染成"半导体。光伏"…
#: 看起来没问题，直到出现英文/数字：`增长30%\n以上` → `增长30%以上`（对），
#: 而 `AI\n芯片` → `AI芯片`（也还行）。真正的坑是**段落被粘成一句**之后
#: 再叠加 200 字截断 —— 截断点会落在粘出来的"假句"中间。
#: 所以顺序是：**先折叠（换行变空格）→ 再按上限截断**，两件事都在这里做，
#: 前端只负责渲染（见 `IntelFeedTab` 的 `oneLine`，它做同样的折叠只为兜底）。
#:
#: ⚠️ 覆盖的空白比 `\s` 更宽：`\s` 在 Python 里**不含全角空格 U+3000**
#: （`\u3000` 不是 `str.isspace()` 之外的空格类，实测 `re.sub(r"\s+", " ", "Ａ\u3000Ｂ")`
#: 保留 U+3000）。中文内容里全角空格极常见（缩进、表格对齐），
#: 漏掉它就会出现"看起来有一格空白、但不是普通空格"的脏数据。
_WS_RUN_RE: Final = re.compile(r"[ \t\r\n\f\v\u00a0\u2000-\u200a\u3000]+")

#: 句子结束标点（截断时优先切在这些字符**之后**）。
#: 与 `tone.SENTENCE_ENDS` 同一套，但这里**不 import 它**：本模块是聚合/展示层，
#: 那个常量属于抽取层，复用它会让"改抽取的断句规则"顺手改掉展示截断
#: （两件事的判据不同：抽取要保句完整喂模型，展示只要读起来不像半句）。
_SUMMARY_STOPS: Final = "。！？!?；;…"


def one_line(text: str) -> str:
    """把文本折叠成**单行**（所有空白 → 一个半角空格），并去掉首尾空白。

    用户口径（2026-10-01）："前端信息不要换行。"

    实测的空白形态（都出现过）：半角空格、换行 `\\n`、回车 `\\r\\n`、
    制表符、全角空格 U+3000、不换行空格 U+00A0。
    只处理 `\\n` 是不够的 —— 星球的正文里两种都混着，
    而"漏一种"的表现是列表里某几条仍然换行，看起来像样式没生效。
    """
    return _WS_RUN_RE.sub(_WS_COLLAPSE_TO, text or "").strip()


def _excerpt(text: str, limit: int) -> str:
    """超长时在**句边界**截断，认不出句边界才按字数切。

    返回**不带**省略号，由调用方决定加不加（`had_tail` 的判据是"有没有丢内容"，
    把省略号混进返回值里会让那个判据变成"结尾是不是省略号"，很脆）。
    """
    if len(text) <= limit:
        return text
    window = text[:limit]
    # 只在**后半段**找句边界：太靠前（前 60%）就只剩半句话，
    # 那与硬截断没有区别，反而多丢内容。
    floor = int(limit * 0.6)
    best = max((window.rfind(ch) for ch in _SUMMARY_STOPS), default=-1)
    if best >= floor:
        return window[:best + 1].rstrip()
    return window.rstrip()


def _summary_of(item: dict[str, Any]) -> dict[str, Any]:
    """一条情报 → **展示用的一句话摘要**（含来源与"是不是摘录"）。

    ## 用户口径（2026-10-01）

    > "本地模型提取的信息，要求输出文字不能超过200个"
    > "精简200字以内，用户没时间看全文，要效率"

    ## ⚠️ 核心：**摘要**与**摘录**必须分得开

    用户抱怨的原话是"摘要还是被截断的"。把一条 3400 字的原文截到 200 字
    **仍然是一句被切断的话** —— 那是同一个缺陷换了个数字。所以优先级是：

        ① `summary_source == "model"`（抽取任务压过的一句话，天然短）
           —— 这是**摘要**，`kind="model"`
        ② 否则 → 原文在**句边界**上截断，并**标记为摘录**
           —— `kind="excerpt"`，`truncated=True` 时前端显示「摘录」徽标
        ③ 连正文都没有 → 空串（前端退回渲染原始 `summary`）

    ⚠️ **绝不能把摘录当成摘要显示**：用户会以为这句话就是全部内容，
    而它其实是"我们只挑了开头一段"。标记是这条纪律的唯一落点。

    ⚠️ 200 是**上限不是目标**：模型摘要落在 60 字就显示 60 字，**不补足**。
    补足（比如"凑满 200 字"）与"要效率"完全相反。
    """
    raw = str(item.get("summary") or "")
    # ⚠️ **模型摘要与原文截断共用 `item["summary"]` 这一个键** —— 判据只能是
    # `summary_source`（`build_feed` 在 join `tone_store` 时打的标记）。
    # 不要试图"从长度猜"：一句 40 字的原文截断与一句 40 字的模型摘要在
    # 字符串上完全一样，而猜错的代价正是这次要修的"把摘录当摘要显示"。
    from_model = str(item.get("summary_source") or "") == "model"
    text = _excerpt(one_line(raw), DISPLAY_SUMMARY_MAX_CHARS)
    if not text:
        return {"text": "", "kind": "", "truncated": False}
    kind = "model" if from_model else "excerpt"
    # 摘录**且**确实丢了内容才算截断（`kind="excerpt"` 但原文本来就 ≤200 字
    # 时不该标"摘录"—— 那会让用户以为我们藏了东西）。
    # ⚠️ 判据用**折叠后**的长度比：原文里的换行折叠后不占额外位置，
    # 拿折叠前的长度比会把"只是格式多"的条目误标成摘录。
    collapsed_len = len(one_line(raw))
    return {
        "text": text,
        "kind": kind,
        "truncated": bool(kind == "excerpt" and collapsed_len > len(text)),
    }


def extraction_input(text: str) -> str:
    """原文 → 抽取入参：**先清洗，再（由 `tone_job`）分段**（顺序不能反）。

    ## 实测的缺陷（这就是这个函数存在的唯一理由）

    原来是直接 `extraction_text(原文)`，于是压缩（当时是"超过 600 字取两头
    各 300 字"）作用在**带 `<e …>` 富文本标签的原始文本**上。
    一条真实笔记的"尾部 300 字"整段是：

        …%E7%89%87%E4%BF%A1%E6%81%AF%23" />

    —— URL 编码的标签残片。原因很直白：标签的头半截落在被切掉的那 300 字
    之外，切完再想清洗已经无从下手（`<e` 都不在串里了）。
    **先清洗就没有这个问题**：标签整条消失，留下的都是正文。

    ## ⚠️ 第四轮起这里**不再压缩**（压缩搬去了 `tone.segment_text`）

    取两头有一个实测的硬天花板：一条 3452 字的《碳化硅材料专题会议》取两头
    得到 591 字，而 `天岳先进`（688234）与 `第三代半导体` 就在被切掉的中间
    ~2500 字里 —— 模型给出的实体字段**全空**。它没失败，它没看见。
    现在这里返回**清洗后的全文**，由 `tone_job` 按句边界切成多段、
    每段一次调用。所以这个函数只保留一件事：**先清洗**。

    ## 为什么复用 `intel_sources` 里的那份实现

    它已经被脱敏链路用了很久，里面每一条规则都对应一次实测泄漏
    （百分号编码的地址、协议相对地址、被上游截断的标签）。这里再写一份
    必然漂移，而漂移的表现是"接口干净了、喂给模型的那份还带着平台域名"。
    所以只调公开入口 `strip_rich_tags`，不复制逻辑。

    ⚠️ 与 `content_filter.strip_noise` 的分工：那个剥的是**展示噪音**
    （表情标记、井号、星球署名，作用在 `title`/`summary` 上）；
    这里剥的是**富文本标签**，因为它的残片会挤占抽取的字数预算
    （窗口里 300 字有 200 字是 `%E7%89%87` 这种，正文等于没喂进去）。
    """
    from src.domain.intel.tone import extraction_text
    from src.infrastructure.connectors.intel_sources import strip_rich_tags

    return extraction_text(strip_rich_tags(text))


def market_terms(title: str, text: str) -> list[str]:
    """**全文中**命中的市场词（概念板块 / 个股 / 外部标的 / AI）—— 折叠判据用。

    ## 与展示用 `highlights` 的分工（两个不同的问法）

        highlights     "读者看得见的那段文字里，哪些词要标绿" —— 只看标题 +
                       展示摘要，而且词**必须逐字在那里面**（否则前端标不出来）
        market_terms   "这条**内容**涉不涉及个股/板块/外部标的/AI" —— 看**全文**

    实测例子（就在本地知识星球那批数据里）：一条 3452 字的
    《碳化硅材料专题会议》笔记，正文里有 `天岳先进`（688234）与
    `第三代半导体`，而模型摘要只写"碳化硅行业处成长期，中国厂商崛起…" ——
    摘要里**一个板块/个股名都没有**。只看摘要的话这条会被折叠，
    而它恰恰是用户最想看的那类内容（用户口径："涉及股票、概念板块、
    美股、AI 的…并且不折叠"）。

    ⚠️ 扫的是**清洗后**的全文（`strip_rich_tags`）：标签残片
    （`%E7%89%87…`）不该参与判断，也不该让"命中"看起来凭空出现。
    """
    from src.domain.intel import vocab
    from src.infrastructure.connectors.intel_sources import strip_rich_tags

    return vocab.highlights(title, strip_rich_tags(text))


def _rule_text_of(item: dict[str, Any]) -> str:
    """规则层判方向时**看哪段文本**：全文优先，退回标题 + 展示摘要。

    ## 为什么不是只看 `summary`（实测会漏掉用户报的那条）

    用户报障的那条笔记（"AI 超级计算机中的芯片数量增加一倍以上"）
    里，`增加一倍` 与 `增长` 都在**正文**里，而展示摘要在 260 字处截断 ——
    一条 3000 字的笔记，"签订订单"这类词经常落在被截掉的后半段
    （券商作文的结构就是"开头铺垫、结尾给标的与结论"）。

    所以有 `extract_text`（清洗后的全文，只有知识星球那条路有）就用它；
    其余来源上游只给到那么长，`summary` 就是全部正文。

    ## ⚠️ 与 `_has_direction` / 折叠判据共用同一个"全文优先"口径

    三处（规则层判方向、`_mentions_market` 判折叠、`body_store` 存全文）
    都遵循"有全文用全文"，各写各的判据必然漂移 ——
    漂移的表现是"这条被判偏多，但它的高亮词来自摘要、正文里找不到"。
    """
    full = str(item.get("extract_text") or "")
    if full:
        return full
    return f"{item.get('title') or ''} {item.get('summary') or ''}".strip()


def _mentions_market(item: dict[str, Any]) -> bool:
    """这条**内容**涉不涉及个股/板块/外部标的/AI（决定折不折叠）。

    优先用全文命中（`market_terms`，只有知识星球那条路算得出全文）；
    其它来源没有全文，退回展示文本上的命中。**注意 `[]` 也是有效答案**：
    全文扫过且没命中，就不该再拿摘要的命中来翻案（两者是同一套匹配规则，
    全文是超集）。
    """
    terms = item.get("market_terms")
    if isinstance(terms, list):
        return bool(terms)
    return bool(_highlights_of(item))


def _highlights_of(item: dict[str, Any]) -> list[str]:
    """条目 → **要标绿的那几个词**（概念板块 / 个股 / 美股 / AI 关键词）。

    已经算过就复用（挂在 item 上的 `highlights`）：一次请求里同一条会被
    "折叠判据"和"渲染"两处问到，算两次是白花的（叠加上词表匹配也不是免费的）。

    ⚠️ 出来的每个词都**逐字出现在标题或摘要里** —— 前端直接拿它在原文里
    替换，不做同义映射（核不到的词等于给用户看了一条假证据，
    与 `phrases` 同一条纪律）。
    """
    cached = item.get("highlights")
    if isinstance(cached, list):
        return cached
    from src.domain.intel import vocab

    return vocab.highlights(str(item.get("title") or ""),
                                 str(item.get("summary") or ""))


def _merge_names(current: Any, extra: list[str]) -> list[str]:
    """名字列表取并集（**保序、逐字去重、丢空串**）。

    机构名/分析师名都是"内容里出现的名字"，两处（契约层与清洗后全文）
    各扫一段，这里合并成一份。**只用逐字相等去重**，不做任何别名归并 ——
    "中泰证券"与"中泰"是两个写法，归并等于替用户改原文（与
    `alert_rules._dedup_names` 同一条纪律）。

    抽成一个函数是因为它现在被**两个字段**共用：各写一遍的话，将来
    给其中一个加限制（比如截断上限）必然只改一处，而表现是"机构名截断了、
    分析师名没有"，没人看得出为什么。
    """
    out: list[str] = []
    for name in list(current or []) + list(extra or []):
        s = str(name or "").strip()
        if s and s not in out:
            out.append(s)
    return out


async def build_feed(*, watch_codes: list[str] | None = None,
                     limit: int = DEFAULT_LIMIT,
                     policy_date: str = "",
                     sort: str = "credibility",
                     filter: str = "all",
                     direction: str = "all",
                     group_undetermined: bool = True) -> IntelFeed:
    """并发聚合六源。**单源失败隔离**，失败进 `gaps`。

    ## `limit` 是**返回条数**，不是取样池大小

    ⚠️ 这是个容易搞错的地方，实测踩过：函数内部会先取 `FILTER_POOL`
    条作为取样池，**筛完**再截到 `limit`。第一版拿 `limit` 当池子，
    于是 `limit=60` 时整个池子最多 60 条 —— 而实测那 60 条里
    分数最高的只有 78 分，于是「高可信 ≥80」**永远是空列表**，
    而全量里其实有 30 条 80+ 的。用户会以为"没有高可信信息"。

    `sort` / `filter` 见 `_balanced_take` 与 `matches_filter`。

    ## `direction` —— 原文倾向的多/空筛选（用户口径 2026-09-26）

    > "intel-controls 表头要加一个 多/空 的过滤选择器（多还是空 是模型
    >   分析出来的 每条信息第一个字【空】【多】）"

    取值 `all` / `bull` / `bear`，判据是**抽取任务落库的 `tone.tone`**
    （"偏多"/"偏空"），也就是列表标题前那个【多】/【空】标记的来源 ——
    所以"筛出来的"与"看到的标记"必然一致，不会出现"筛了偏多却有条没有标记"。

    ⚠️ **为什么是独立参数、不并进 `filter`**：多空与可信度是**两个正交的轴**。
    并进去的话，用户在「高可信」档下切到「偏多」就会**丢掉可信度档位**，
    想"高可信 + 偏多"就表达不出来。

    ⚠️ **不要**用 `credibility.MIN_CREDIBILITY_FOR_TONE` 之下那些条目来凑数：
    低可信条目的 tone 是 `source="skipped"`（按规则**不做**倾向分析），
    它们没有方向，筛出来只能是空。判据统一走 `_has_direction` + `tone.tone`。

    ## `group_undetermined` —— 谁能看到"收容组"

    `True`（默认，给 `/feed` 接口）：把"给不出方向"的条目收成一行。
    `False`：返回**未收容的平铺池**。

    ⚠️ **内部消费者必须传 `False`**，这不是可选项。踩过的坑：
    收容组上线后，`_intel_tone_extract` 与 `hot_scan` 都还在用默认值调
    `build_feed` —— 于是它们看到的"全部条目"变成了 **2 条**
    （1 条信号 + 1 个组），倾向抽取当轮只处理了 2 条就收工，
    而日志显示的是"抽取 0 条"，看起来像"没有可抽的"。
    收容是**展示层**的事，不该改变"有哪些内容"这个事实。
    """
    from src.infrastructure.connectors.intel_sources import fetch_all

    if not policy_date:
        policy_date = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")

    # ★ 研报按**标的**拉：不传标的就一条都没有，连带「仅研报」与
    #   「高可信 ≥80」两个筛选项**永远是空的**（实测：池内来源档最高
    #   只到 74 的权威媒体档，而研报档是 84）。用户会以为"没有研报/
    #   没有高可信信息"，实际是**根本没去取**。
    #   所以没有显式关注标的时，用自选清单兜底。
    if not watch_codes:
        watch_codes = watchlist_codes()

    items: list[dict[str, Any]] = []
    gaps: list[dict[str, str]] = []

    # ── 1) 五个无依赖源（并发，内部已隔离失败）──
    #
    # ★ 走**源级缓存**（2026-09-25 性能剖析后加）：把"取数"与"组装"拆开。
    #
    # 实测一次完整重建 2.6~3.3 秒，其中 zsxq 分页 ~1.2s + fetch_all ~0.5s
    # 全是网络，而 CPU（组装 356 条）只占 ~0.17s。原本 `build_feed` **每次
    # 调用都重新取数**，而接口按 `limit|codes|sort|filter` 分缓存键 ——
    # 于是"换一个筛选档"就要把完全相同的网络成本再付一遍。
    # 缓存后：取数一次、多档复用（TTL 见 `source_cache.DEFAULT_TTL`）。
    #
    # ⚠️ `fetch_all` 没有"只该跑一次"的副作用（留存/水位线都在 zsxq 那一路），
    # 所以这里不看 `fresh`。
    from src.domain.intel import source_cache

    async def _fetch_public() -> tuple[list[dict[str, Any]], dict[str, str]]:
        raw, fails = await fetch_all(
            watch_codes=(watch_codes or [])[:MAX_BROKER_CODES],
            policy_date=policy_date)
        # 在缓存**里**存 `to_public()` 的结果（纯 dict，JSON 可序列化形状）：
        # 它同时完成了脱敏与可信度计算，且两者都是确定性的，复用不会漂移。
        return [it.to_public() for it in raw], dict(fails)

    try:
        # ⚠️ 注意解包层级：`get_or_fetch` 返回 `(数据, fresh)`，而这里的数据
        # **本身**是 `(items, failures)` 二元组 —— 少写一层括号就会让
        # `fresh`(bool) 落到 `failures` 上，报 `'bool' object has no attribute
        # 'items'`（实测踩到）。`fetch_all` 没有"只该跑一次"的副作用，
        # 所以 `fresh` 用不上。
        (raw_public, failures), _fresh = await source_cache.get_or_fetch(
            f"fetch_all|{','.join((watch_codes or [])[:MAX_BROKER_CODES])}"
            f"|{policy_date}",
            _fetch_public,
        )
    except Exception as exc:  # noqa: BLE001 整批失败也要给出可读结果
        from src.core.redaction import sanitize_error

        logger.warning("情报源整批聚合失败：%s", sanitize_error(exc))
        raw_public, failures = [], {"all": sanitize_error(exc)}

    items.extend(raw_public)

    # 失败源 → 缺口条目。**只给"类型"，不给源名与错误原文**：
    # 前端展示的是"某类来源暂无更新"，而非内部故障细节。
    for name, _err in failures.items():
        kind = _kind_of(name)
        gaps.append({
            "kind": kind,
            "kind_label": KIND_LABELS.get(kind, kind),
            # message 面向**管理员**；普通用户界面只用 kind_label
            "message": f"{KIND_LABELS.get(kind, kind)}当前不可用",
        })

    # ── 2) 知识星球（增量，独立失败域）──
    #: 本轮**真正取到**的条目（留存用）。与 `items` 分开是因为下面还要把
    #: 留存行并进来 —— 并进来的那些不需要（也不该）再存一遍。
    fetched_pubs: list[dict[str, Any]] = []
    try:
        from src.domain.intel import item_store
        from src.infrastructure.connectors.intel_sources import IntelItem
        from src.infrastructure.connectors.zsxq_incremental import (
            fetch_incremental,
            save_watermark,
        )

        # ★ 走**源级缓存**，并把"增强后的最终结果"整个缓存下来。
        #
        # 为什么缓存最终结果而不是原始 topics：增强（`to_public` 脱敏+截断、
        # `extraction_input` 清洗全文、`market_terms` 全文扫词）是纯函数，
        # 缓存原始 topics 只会让每次命中都重算一遍。缓存确定的产物更省，
        # 也不会引入"两次增强结果不一致"。
        #
        # ⚠️ **副作用必须由 `fresh` 门控**（这里最容易写错）：
        # `item_store.persist`（留存落库）与 `save_watermark`（推进水位线）
        # 只能在**真取数**那一趟跑。命中缓存时也跑的话，水位线会被凭空推进，
        # 而它的语义是"这个区间已处理完" —— 推错了就是**内容永久丢失**
        # （`zsxq_incremental` 的注释专门记过这条）。
        async def _fetch_pubs() -> dict[str, Any]:
            inc = await asyncio.to_thread(fetch_incremental)
            pubs: list[dict[str, Any]] = []
            for t in inc.topics:
                # ⚠️ 必须走 `IntelItem.to_public()`，不能在这里手写 dict。
                #
                # 手写 dict 会**绕过脱敏与截断**（那两道闸都长在 `to_public` 上）：
                #   · `source_alias` 直接写明文 `"zsxq"` → 数据源泄漏
                #   · `summary` 不截断 → 实测最长 3400+ 字，移动端一条占满十屏
                # 首版就是这样：五个内置源的 `to_public` 修好了，这条旁路没有。
                # 统一从同一条出口走，将来加字段也只需要改一处。
                pub = IntelItem(
                    kind="research_note",
                    title=t.title,
                    summary=t.text,
                    published_at=t.created_at,
                    source_alias="research-note-zsxq",
                    # 真实来源名（**内部字段，不出接口**）：分级表靠中文属性词
                    # 识别档次，写英文标识（`zsxq`）会落进保守档 38。
                    source_name="知识星球-调研纪要",
                    content_hash=t.content_hash,
                ).to_public()
                # ── 抽取入参：**先清洗**，与展示用的 `summary` 分开 ──
                #
                # `to_public()` 把 `summary` 截到 260 字是**展示契约**（移动端
                # 一条 3400 字会占满十屏），但抽取需要看到**后半段** —— 研报的
                # 个股与盈利预测几乎都在结尾（"综上，推荐 XX，目标价…"）。
                # 共用同一个值时，抽取只能看到开头，"找利好/利空个股"就废了一半。
                #
                # ⚠️ 顺序是**清洗 → （`tone_job` 里）分段**，见 `extraction_input`：
                # 反过来的实测后果是"尾部 300 字全是 `%E7%89%87%E4%BF%A1%E6%81%AF`
                # 这种 URL 编码的标签残片"，正文一个字都没进去。
                #
                # ⚠️ 第四轮起这里是**清洗后的全文**（不再是"两头各 300 字"）：
                # 压缩改由 `tone.segment_text` 切段完成，因为取两头会把中间整段
                # 丢掉，而实体恰好常在那里（实测《碳化硅材料专题会议》：
                # `天岳先进` / `第三代半导体` 落在被切掉的中间 ~2500 字里，
                # 实体字段全空）。
                #
                # ⚠️ 这个键**只给进程内的 `tone_job` 用**（`jobs.py` 传的是
                # `feed.items`，不是 `to_public()` 的结果）。
                # `IntelFeed.to_public()` 会**剥掉**它 —— 否则全文会随接口
                # 发给浏览器，正是要避免的那件事。
                pub["extract_text"] = extraction_input(str(t.text or ""))
                # 全文里的市场词（折叠判据用）。同样只在进程内传：
                # 它是**全文**扫出来的，词未必出现在展示摘要里 ——
                # 直接发给前端会得到"标不出来的绿字"（前端找不到那个词）。
                pub["market_terms"] = market_terms(str(t.title or ""),
                                                   str(t.text or ""))
                pubs.append(pub)
            return {
                "pubs": pubs,
                # 推进水位线要用的几样一起缓存，保证"命中缓存"与"真取数"
                # 拿到的是同一批语义。
                "watermark": str(getattr(inc, "watermark", "") or ""),
                "newest": str(getattr(inc, "newest", "") or ""),
                "new_count": int(getattr(inc, "new_count", 0) or 0),
                "truncated": bool(getattr(inc, "truncated", False)),
                "upstream_empty": bool(getattr(inc, "upstream_empty", False)),
            }

        # ⚠️ 实测踩到：`fetch_incremental` 抛 `TokenExpired` 时，若不加这个
        # `try/finally`，赋值永远不执行，随后 `cache_fresh` 就是
        # `UnboundLocalError` —— 一个与本意无关的异常会接管整个失败路径，
        # 把"授权失效"伪装成崩溃。
        cache_fresh = False
        try:
            zsxq_payload, cache_fresh = await source_cache.get_or_fetch(
                "zsxq_incremental", _fetch_pubs,
                # 授权失效/空页这类"没拿到内容"的结果不进缓存：否则一次抖动
                # 会被缓存几十秒，用户看到的是"这个来源停了"。
                cacheable=lambda p: bool(p.get("pubs")))
        finally:
            pass

        fetched_pubs = list(zsxq_payload.get("pubs") or [])
        items.extend(fetched_pubs)
        # ── 留存（用户口径 2026-09-26："落库最多保留 3 天"）──
        #
        # 情报流是**每次请求现拼**的，而这条链路每轮只取"最新 N 条"。
        # 窗口一滑，早先取到的帖子就从页面上消失了（用户："原来的信息丢那里
        # 去了"）。把每轮取到的条目按 `content_hash` 存进 `item_store`
        # （3 天），下一轮在下面并回来 —— 窗口只决定"新取到什么"，
        # 留存决定"页面上能看到什么"。
        #
        # ⚠️ 同样只在**真取数**时落库：命中缓存时这批早就存过了，
        # 重复写库只是白付 I/O。
        #
        # ⚠️ 失败**不冒泡**（`item_store.persist` 内部已经吃掉异常）：
        # 留存是锦上添花，它挂了这一页照样要出得来。
        if cache_fresh and fetched_pubs:
            try:
                stats = await asyncio.to_thread(item_store.persist, fetched_pubs)
                if stats.get("written") or stats.get("pruned"):
                    logger.info("券商作文留存：新增 %s 条、清理 %s 条",
                                stats.get("written"), stats.get("pruned"))
            except Exception as exc:  # noqa: BLE001 兜底：绝不让留存影响出页
                logger.warning("券商作文留存异常（忽略）：%s", type(exc).__name__)
        # 只在成功时推进水位线：失败推进会导致**永久丢内容**
        # `newest` 必须一起写回：它是"已取到哪儿"的高水位线，不写回的话
        # 下一轮会把同一批最新内容再取一遍（本地小模型重复分析）。
        if (cache_fresh and zsxq_payload.get("watermark")
                and int(zsxq_payload.get("new_count") or 0)):
            await asyncio.to_thread(
                save_watermark, zsxq_payload["watermark"],
                note="intel_feed", newest=zsxq_payload.get("newest") or "")
        if zsxq_payload.get("truncated"):
            gaps.append({
                "kind": "research_note",
                "kind_label": KIND_LABELS["research_note"],
                # ⚠️ 这里的文案**就是前端展示的原文**（`IntelFeedTab` 的
                # `feed.gaps` 直接渲染 `message`），所以标签改名时必须
                # 连着它一起改 —— 只改 `KIND_LABELS` 会让缺口提示里
                # 留下一个界面上再也找不到的旧名字。
                #
                # ⚠️ **两句话必须分开**（2026-09-26）：`upstream_empty` 是
                # "这一趟没拿到最新页"（数据源偶发空页，约 1/6），而下面那句
                # "回填未取完"是"拿到了、只是没追到最老"。说反了会让用户以为
                # 这个来源停了 —— 而它下一趟就会自己好。
                "message": ("券商作文本次未取到（数据源返回空，已自动重试）"
                            "—— 内容没有被删，稍后会自动再试"
                            if zsxq_payload.get("upstream_empty") else
                            "券商作文回填未取完，下次运行将继续追平"),
            })
    except Exception as exc:  # noqa: BLE001 含 TokenExpired
        from src.core.redaction import sanitize_error
        from src.infrastructure.connectors.zsxq_source import TokenExpired

        if isinstance(exc, TokenExpired):
            # 授权过期：**用户侧只看到"暂无更新"**；管理员侧另有邮件提醒
            logger.warning("知识星球授权失效，本次跳过该源")
            gaps.append({
                "kind": "research_note",
                "kind_label": KIND_LABELS["research_note"],
                "message": "券商作文暂无更新",
                "admin_hint": "授权已失效，请运行 scripts/zsxq_authorize.py",
            })
        else:
            logger.warning("知识星球采集失败：%s", sanitize_error(exc))
            gaps.append({
                "kind": "research_note",
                "kind_label": KIND_LABELS["research_note"],
                "message": "券商作文暂无更新",
            })

    # ── 2.5) 把**留存**里的条目并回来（用户口径 2026-09-26）──
    #
    # ## 为什么必须有这一步
    #
    # 上面那条链路每轮只取"最新 N 条"。窗口一滑，早先取到的帖子就从页面上
    # 消失了 —— 不是被删了，是**没被取**（实测那个群 20 小时 88 条，
    # 而窗口只覆盖 7.4 小时，00:37~13:21 一条都取不到）。
    # 留存把它们按 `content_hash` 存 3 天，这里补回来。
    #
    # ⚠️ 顺序很要紧：**在时效窗口与去重之前**。并回来的条目要与本轮取到的
    # 走完全相同的下游（`counts`、3 天窗口、中性过滤、高亮、折叠判据）——
    # 跳过任何一道都会得到"留存条目在页面上比新条目多活几天"这种不一致。
    #
    # ⚠️ 只认**本轮没取到**的（`have`）：同一条两处都有时以本轮为准
    # （它的 `platform`/`codes` 等字段更全，而且这一轮确实拿到了它）。
    restored = 0
    try:
        from src.domain.intel import item_store

        kept = await asyncio.to_thread(item_store.load)
        have = {str(i.get("content_hash") or "") for i in items}
        for h, row in (kept or {}).items():
            if not h or h in have:
                continue
            it = _from_store(row)
            if it is None:
                continue
            items.append(it)
            have.add(h)
            restored += 1
        if restored:
            logger.info("券商作文留存并回 %d 条（本轮未取到的那些）", restored)
    except Exception as exc:  # noqa: BLE001 留存读不出来不该影响这一页
        logger.warning("券商作文留存读取异常（忽略）：%s", type(exc).__name__)

    # ── 2.5) 内容过滤：挡掉无内容与平台运营话术 ──
    #
    # 用户口径（2026-09-25）："知识星球爬取的数据，什么都没有也显示了，
    # 只有'#文字图片信息'，这种就直接过滤掉；内容中出现 WD调研、礼物
    # 等这些无关个股、行业、政策的信息，都要过滤掉。"
    #
    # ⚠️ 放在 **dedup 之前**：被丢掉的条目不该参与去重与聚类，
    # 否则垃圾会挤掉真实内容（`content_hash` 是内容指纹，过滤不改指纹，
    # 所以游标与跨次去重不受影响）。
    #
    # ⚠️ 用**规则**而不是本地模型：这类噪音是**格式性**的（空条目、
    # 表情标记、运营话术），正则能百分之百拦住；而模型会偶尔漏
    # （同一份数据两次结果不同）。而且它在流水线最前面、每条都要过 ——
    # 60 条 × 770ms = 46 秒，为一批注定要丢的垃圾付这个代价不值得。
    from src.domain.intel.content_filter import filter_items

    filter_log: list[dict[str, Any]] = []
    items, filter_stats = filter_items(items, log=filter_log)

    # ── 2.6) 机构名 + 分析师名：**清洗之后再补一次**（展示字段，见下）──
    #
    # 用户口径（2026-10-01）："券商名不一定要告警，但是一定要前端输出信息。"
    # 用户口径（2026-10-01 追加）："股票名 板块名 券商 孙潇雅、赵宇阳、武超则、
    # 陈果、刘晨明、洪灏 …… 推送到前端展示。"
    #
    # ## ★ 这四个类别是**结构化保证**，不是"提示词请求"
    #
    # 用户的原话是"必须输出"。**只把这句话写进提示词是不够的** ——
    # 模型漏一个，用户就漏一个，而这次需求的全部目的就是"别漏掉谁在唱多/唱空"。
    # 所以四类各有一条**与模型无关**的确定性来源，在这里（以及契约层）合并进 item：
    #
    #     股票名 + 板块名   `vocab.scan`（词表扫描，见下面 `highlights` 的处理）
    #     券商              `alert_rules.institutions`（`XX证券` 形态）
    #     分析师名          `alert_rules.analysts`（`ANALYST_WATCHLIST` 六人名单）
    #
    # 模型只负责**一句话事件摘要 + 方向 + 这些标的属于哪一侧**；上面四类
    # 一个字都不依赖它（`tone` 里同名的新字段只作审计，见 `tone.validate_people`）。
    # 所以"模型这次什么都没抽到"不会让任何一类从界面上消失。
    #
    # ## 为什么要**两层都算**（契约层 + 这里）
    #
    #   · 契约层只有**未清洗的原文**，那里扫一遍能保证"任何来源、任何调用路径
    #     的条目都带上这个字段"（`hot_scan` 等旁路也拿得到）；
    #   · 这一层拿得到**清洗后的全文**（`extract_text`，署名常在后段 ——
    #     实测 `国金证券` 在第 695 字），而提取侧 `to_public()` 时
    #     `extract_text` 还没挂上去。
    #
    # 取**并集**而不是替代：清洗会剥掉星球署名，被剥掉的那部分里若有机构名，
    # 契约层那一遍已经收过了 —— 两层各覆盖一段，合起来才不漏。
    #
    # ⚠️ 放在**过滤之后**：被丢掉的条目不该再做任何加工（它们的字段会被
    # 收容组/接口一起丢掉，算了也没人看）。
    from src.domain.intel import alert_rules

    for it in items:
        it["institutions"] = _merge_names(it.get("institutions"),
                                          alert_rules.institutions(it))
        it["analysts"] = _merge_names(it.get("analysts"),
                                      alert_rules.analysts(it))

    # ── 3) 归一：按时间倒序 + 去重 + 截断 ──    #
    # ⚠️ 排序键必须过 `sort_key()` 归一化，不能直接比 `published_at` 字符串。
    # 三种时间戳格式（`20260924` / `2026-09-25 04:26:03` /
    # `2026-09-25T13:15:41.340+0800`）直接按字符串排会出错：
    # `-`(0x2D) < `0`(0x30)，于是 `'2026-09-…'` 排在 `'20260924'` **之前**，
    # 界面按天分组后出现 `今天 / 09-24 / 今天 / 09-24` 来回跳。
    from src.infrastructure.connectors.intel_sources import sort_key

    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for it in sorted(items, key=lambda x: sort_key(x.get("published_at")),
                     reverse=True):
        h = str(it.get("content_hash") or "")
        if h and h in seen:
            continue
        if h:
            seen.add(h)
        deduped.append(it)

    counts: dict[str, int] = {}
    for it in deduped:
        k = str(it.get("kind") or "other")
        counts[k] = counts.get(k, 0) + 1

    # ── 3.5) 时效窗口：只留最近 `FEED_WINDOW_DAYS` 天 ──
    #
    # 用户口径（2026-09-25）：
    #
    #   "情报流 最多保留最近7天的新闻信息，超过7天的都过滤不要，
    #    来自财经日历刷屏的信息太多了。"
    #
    # ## 实测的"刷屏"元凶是**券商研报的历史归档**
    #
    # 上线前诊断（268 条返回）：
    #
    #     broker_report  76 条   2017-01-02 → 2026-09-23   ← 九年跨度
    #     research_note  25 条   2026-09-15
    #     newswire      152 条   当天
    #     policy         15 条   前一日
    #
    # 东财研报接口按**个股**返回，给的是这只票的**全部历史报告**，
    # 于是"今天的情报流"里混着 2017 年的一篇点评。它同时造成两个危害：
    # 把当天内容挤出 `limit`，以及让人误以为那是新消息
    # （标题形如「2022年年报点评：…」，很像日历条目 —— 用户说的"财经日历刷屏"
    #  多半指的就是这类带日期的归档）。
    #
    # ## 为什么是**硬窗口**而不是"降权"
    #
    # 降权解决不了挤占：`limit` 是有限的一页，一条 2017 年的研报哪怕排最后
    # 也占了一行。而且"七天前的信息"对追热点没有价值 —— 它的价值在
    # **复盘**，那是另一个页面的事（本项目有专门的回测/复盘模块）。
    #
    # ## 为什么放在 `counts` 之后
    #
    # `counts` 是筛选 tab 的角标（"这类一共有多少条"）。窗口是**采集侧**的
    # 时效纪律，不该让角标跟着变成"七天内的条数" —— 否则用户会以为
    # 某一类来源断了，而实际只是它七天没更新（那件事由 `window_dropped`
    # 缺口单独说明）。
    window_cut = (datetime.now(timezone.utc).astimezone()
                  - timedelta(days=FEED_WINDOW_DAYS)).strftime(
                      "%Y-%m-%dT%H:%M:%S")
    in_window: list[dict[str, Any]] = []
    dropped_by_kind: dict[str, int] = {}
    for it in deduped:
        if sort_key(it.get("published_at")) >= window_cut:
            in_window.append(it)
            continue
        k = str(it.get("kind") or "other")
        dropped_by_kind[k] = dropped_by_kind.get(k, 0) + 1
    deduped = in_window

    # 某一类"全部落在窗口外"要**如实报缺口**，不能让它静默消失 ——
    # 那会让用户以为这个功能没了（用户此前就问过"研究笔记怎么在前端看不到了？"）。
    for k, n in dropped_by_kind.items():
        if counts.get(k, 0) > 0 and all(
                str(it.get("kind") or "other") != k for it in deduped):
            gaps.append({
                "kind": k,
                "kind_label": KIND_LABELS.get(k, k),
                "message": f"{KIND_LABELS.get(k, k)}最近 "
                           f"{FEED_WINDOW_DAYS} 天无更新"
                           f"（已归档 {n} 条过期内容）",
            })

    from src.domain.intel.credibility import level_of

    taken = _balanced_take(deduped, FILTER_POOL, sort=sort)

    # ── 可信度分层计数：在**筛选之前**、于池子上统计 ──
    #
    # 顺序很重要：先统计再筛选，这样角标与当前档位无关。
    # 反过来（先筛后统计）会让"切一次 tab 所有角标都变"。
    dist: dict[str, int] = {}
    for it in taken:
        raw = it.get("credibility") or {}
        try:
            lv = level_of(int(raw.get("score")))[0]
        except (TypeError, ValueError):
            lv = "doubt"
        dist[lv] = dist.get(lv, 0) + 1

    # ── 相似新闻聚合（**在池子上做**，不是在 limit 之后）──
    #
    # ⚠️ 顺序很重要：聚类要在**截断之前**、于整个池子上做。
    # 第一版放在返回前、只对 `limit` 条做，结果 301 条里只合出
    # 11 簇 / 22 条 —— 因为同一件事的各个转载分散在池子的不同位置，
    # 只比最后 60 条自然匹配不上几对。
    #
    # 聚合之后**把非代表条收起**（用户口径："相似相同观点的可以聚合成
    # 一条的，把信息源在关联数字里"）—— 所以它同时是**去噪**：
    # 同一件事在情报流里只占一行，而不是刷五遍。
    from src.domain.intel.related import attach

    cluster_stats = attach(taken)

    # 用聚类算出的**独立佐证数**重算可信度。
    #
    # 这是设计稿公式里 `+ 独立佐证数 × 4` 那一项，也是"多来源印证"
    # 唯一诚实的算法：同一份信息被转发 3 次**不算** 3 个独立来源
    # （设计稿 §5.4 明确警告过"共振虚高"）。
    # ⚠️ 必须**封顶**：低权威来源不能靠"被转得多"刷分 ——
    # 来源分即上限这条性质不能被佐证项破坏。
    from src.domain.intel.credibility import apply_corroboration

    for it in taken:
        cred = it.get("credibility")
        if cred:
            it["credibility"] = apply_corroboration(
                cred, it.get("corroboration"))

    # 收起非代表条（同一簇只留时间最新的那条）
    taken = [it for it in taken if it.get("is_cluster_lead", True)]

    # ── 原文倾向：**只读存储**，接口里不调模型 ──
    #
    # 本地模型单条实测 ~770ms，一页 60 条现算就是 +46 秒 ——
    # 接口会从"秒回"退化成"超时"。所以抽取走定时任务
    # （`tone_job.run_once`，2 小时一次）落进 `tone_store`，
    # 这里只按 `content_hash` 查，O(1)。
    #
    # 查不到就**不给倾向字段**（而不是编一个）—— 前端会说"尚未抽取"。
    # 规则层能定的那些由 `tone.rule_tone` 在抽取时一并处理，
    # 所以"没抽过"与"抽过但未定"在存储里是两种状态。
    from src.domain.intel import tone, tone_store

    #: 请求路径上的计时（只在本函数内累加，供日志/排障）。
    #:
    #: ## 为什么要把这个数**量出来**（用户口径与硬约束都要求）
    #:
    #: 第六轮把"全文落库"搬进了 `build_feed`（每次请求都写一次文件）。
    #: 那条链路加在请求路径上**必须有个数**：它慢不慢不能靠读代码断言，
    #: 而它一旦变慢的表现是"情报流整体变卡"，与落库本身看不出关系。
    _t_store_ms = 0.0
    _t_rules_ms = 0.0
    _rule_filled = 0
    _rule_considered = 0

    for it in taken:
        hit = tone_store.get(str(it.get("content_hash") or ""))
        if hit:
            # ⚠️ 走 `view()` 而不是直接读键：存储里**同时存在两种格式的行**
            # （第二轮之前的行没有 events/bullish/bearish）。老行缺键是
            # 正常状态，直接下标会在"用户翻到一条几天前的老数据"时
            # KeyError，把整个情报流打挂。
            t = tone_store.view(hit)
            cleaned_bull, cleaned_bear = tone.drop_cross_side(
                t["bullish"], t["bearish"])
            it["tone"] = {
                "tone": t["tone"],
                "has_tone": t["has_tone"],
                # **已判定为中性** → 读取侧据此隐藏（见下方"中性条目不进
                # 情报流"）。必须由存储带过来：没这条记录就分不清
                # "中性"与"尚未抽取"，中性就会被当成未抽取照常显示。
                "neutral": t["neutral"],
                "phrases": t["phrases"],
                "codes": t["codes"],
                "confidence": t["confidence"],
                "source": t["source"],
                "explain": t["explain"],
                # ── 第二轮：原文里明写的利好/利空行业与个股 + 关键事件 ──
                #
                # 这三个字段是**抽取结果**，不是判断，所以照原样下发
                # （界面上要能拿它去原文核对，逐字校验已在抽取侧做完）。
                # 老行给空值，界面显示"这块没有"，**不编**。
                #
                # ⚠️ **读取侧再过一次"跨侧同名"清洗**（用户口径 2026-09-26）：
                # 「同一个板块不许同时挂在利好与利空两侧，同名两侧都不放」。
                # 抽取侧（`tone.extract_tone` / `merge_tone_results`）已经做了，
                # 这里再做一次是因为**存量行**是那次改动之前抽的 ——
                # 实测有行写着「利好：储能 / 利空：储能」，而那看起来像功能坏了。
                # 同一份实现、幂等，所以多过一道不会漂移。
                "events": t["events"],
                "bullish": cleaned_bull,
                "bearish": cleaned_bear,
            }
            # 模型生成的一句话摘要（比"原文截断"信息密度高得多）
            if t["summary"]:
                it["summary"] = t["summary"]
                it["summary_source"] = "model"
            continue

        # ── ★ 没有抽取结果 → **规则层兜底给方向**（2026-10-01 第六轮）──
        #
        # ## 用户报障
        #
        # > 一条明显偏多的笔记（"AI 超级计算机中的芯片数量增加一倍以上"）
        # > **没有【多】标记**。
        #
        # ## 实测的成因：抽取是**每 2 小时一班**，而条目是随时来的
        #
        # 试点实例的 `intel_tone_extract` 班次是 `20 */2 * * *`
        # （`registry.py`）。实测两次运行之间，`intel_zsxq_collect`
        # 又采了 30 条新笔记（游标 18:20 → 21:20）—— 那批条目在存储里
        # **一条判定都没有**，所以它们一律没有方向标记，直到下一个整点过 20 分。
        # 用户看到的"这条明明偏多却没有【多】"就是这一段空窗。
        #
        # ## 为什么这条路是允许的（硬约束没有被绕开）
        #
        # `tone.rule_tone` 是**纯词表 `in` 判断**：零模型、零网络、
        # 可复算、可解释（依据就是命中的那几个词，逐字来自原文）。
        # 请求路径被禁止的是**语义抽取**（本地 8B ~770ms/条 → 一页 46 秒），
        # 不是"算一个字符串包含关系"。
        #
        # ## ⚠️ `source="rules"` 必须原样带出去，不许伪装成模型判定
        #
        # `tone_job` 那边同样用 `rules` / `rules+llm` 区分"词表给的"与
        # "模型参与的"。把词表猜测标成 `rules+llm` 会让用户（与排障的人）
        # 以为模型看过这条 —— 那正是"把请求当保证"的同类错误。
        _t0 = time.monotonic()
        verdict = tone.rule_tone_verdict(_rule_text_of(it))
        _t_rules_ms += (time.monotonic() - _t0) * 1000.0
        _rule_considered += 1
        if verdict:
            it["tone"] = verdict
            _rule_filled += 1

    # ── 展示摘要：单行 + 200 字上限（用户口径 2026-10-01）──
    #
    # ⚠️ 必须在**倾向 join 之后**：模型摘要来自 `tone_store`
    # （上面那个循环把它写进了 `it["summary"]`），排在前面就只能拿到原文。
    #
    # ⚠️ 刻意**不改写** `it["summary"]`：那个键已经有读取方（前端兜底、
    # `_group_row`、`body_store.build_row` 的"摘要即正文"），就地截断会让
    # "全文"变成"又一份被截断的东西"。新字段只服务展示，见 `_summary_of`。
    for it in taken:
        it["summary_text"] = _summary_of(it)

    # 倾向分布（热度页的多空比用它）。**只统计 `has_tone` 的条目** ——
    # 把"未定"算进多空比等于替用户做了一个我们并不确定的判断。
    tone_dist: dict[str, int] = {}
    for it in taken:
        t = it.get("tone") or {}
        if t.get("has_tone"):
            k = str(t.get("tone"))
            tone_dist[k] = tone_dist.get(k, 0) + 1

    # ── 中性条目不进情报流（用户口径 2026-09-25）──
    #
    # > "倾向中性的信息就不要显示了，无法判断的要显示。"
    #
    # 理由成立：一条判定为"原文语气中性"的快讯（"某指数报跌0.15%"）
    # 不提供任何倾向信号，却占掉一行、把有信号的条目挤下去。
    #
    # ⚠️ **只丢"已判定为中性"的，不丢"未定"和"尚未抽取"的** ——
    # 用户明确说"无法判断的要显示"。三种状态不能混：
    #
    #     中性      已判定、确实没有倾向 → 不显示
    #     未定      两层判定冲突，**我们不知道** → 显示
    #     未抽取    定时任务还没跑到 → 显示（不该因为任务没跑就藏内容）
    #
    # ⚠️ 放在 `tone_dist` **之后**统计：热度页的多空比要反映全量分布，
    # 而不是"过滤后剩下的"。
    neutral_hidden = 0
    kept_signal: list[dict[str, Any]] = []
    for it in taken:
        t = it.get("tone") or {}
        # 两条判据都要：`neutral` 是抽取任务的显式标记（新数据），
        # `has_tone + 中性` 兼容早期落库的行（那时没写 `neutral` 字段）。
        if t.get("neutral") or (t.get("has_tone") and str(t.get("tone")) == "中性"):
            neutral_hidden += 1
            continue
        kept_signal.append(it)
    taken = kept_signal
    # ⚠️ **在可信度筛选之前**留一份平铺池：热议扫描要的是"有哪些真实条目"，
    # 与当前展示档位（`filter`）无关。放在 763 行之后会让"用户切到高可信档"
    # 顺带改变热议个股的扫描范围 —— 同一个页面里两个口径打架。
    scan_pool = kept_signal

    # ── 可信度筛选（在**池子**上做，不是在 limit 之后做）──
    taken = [it for it in taken if _passes_filter(it, filter)]

    # ── ★ 多/空筛选（用户口径 2026-09-26）──
    #
    # 判据是抽取任务落库的 `tone.tone`（"偏多"/"偏空"）—— 与列表标题前
    # 那个【多】/【空】标记**同一个来源**，所以"筛出来的"与"看到的标记"
    # 必然一致（不会出现"筛了偏多却有条没有标记"这种自相矛盾）。
    #
    # ⚠️ 位置在**收容组划分之前**：收容组装的是"给不出方向"的条目，
    # 用户既然要看"偏多"，那一组就整组不该出现（它一条都不匹配）。
    # 放在之后的话得额外把它踢掉，容易漏。
    #
    # ⚠️ `direction_dist` 统计放在**筛选之前**，理由与 `credibility_dist`
    # 完全一样：角标必须与当前档位无关，否则"切一次 tab 所有角标都变"。
    direction_dist = {"bull": 0, "bear": 0}
    for it in taken:
        d = direction_of(it)
        if d in direction_dist:
            direction_dist[d] += 1

    direction_hidden = 0
    if direction in ("bull", "bear"):
        kept_dir: list[dict[str, Any]] = []
        for it in taken:
            if direction_of(it) == direction:
                kept_dir.append(it)
            else:
                direction_hidden += 1
        taken = kept_dir

    # ── 无明确倾向的条目**聚合成一条**（用户口径 2026-09-25）──
    #
    # > "原文倾向未定的，可以聚合成一条，点开可查看。"
    #
    # ## 为什么这条很重要（不是排版偏好）
    #
    # 情报流里绝大多数是宏观数据/海外快讯（"某指数报跌0.15%"），
    # 它们**给不出方向**。让它们一条占一行，结果是：真正有倾向信号的
    # 那几条被稀释到看不见，而用户翻了半天全是"与我们无关"的东西。
    #
    # ## 三种"给不出方向"的状态必须一起收，理由是**用户看到的是同一个东西**
    #
    #     未定      两层判定冲突（模型与规则不一致）
    #     未抽取    定时任务还没跑到（本地模型 770ms/条，铺满要几小时）
    #     低可信    可信度 <50，按规则**不做**倾向分析
    #
    # 代码里它们状态不同，但界面上都显示"给不出方向"。只收其中一种，
    # 刷屏问题只解决三分之一 —— 所以一起收，并在摘要里**分别报数**
    # （"已判定未定 X / 尚未抽取 Y"），让这个合并是可解释的。
    #
    # ⚠️ **中性不在这里**：它已经被上面丢掉了（用户："倾向中性的信息
    # 就不要显示了"）。也**不能**把有方向的条目混进来 —— 这个组的前提
    # 就是"我们没说它偏多偏空"。
    signal: list[dict[str, Any]] = []
    undetermined: list[dict[str, Any]] = []
    for it in taken:
        (signal if _has_direction(it) else undetermined).append(it)

    # ── 高亮词 + 折叠判据（用户口径 2026-09-25）──
    #
    # > "知识星球内容如果不涉及任何美股、AI、概念板块、股票的可以折叠。
    # >   如果涉及股票、概念板块、美股、AI的…并且不折叠。"
    #
    # ## 折叠判据与高亮词**共用同一次匹配**
    #
    # 前端要标绿的那几个词，就是后端认定"这条涉及个股/板块/美股/AI"的那几个词。
    # 分两处实现必然漂移（绿字与"为什么这条没折叠"对不上），所以只算一次：
    # 命中为空 ⇒ 什么都不涉及 ⇒ 可以折叠。
    #
    # ## 为什么由后端算高亮（而不是让前端自己找）
    #
    # 一致性只是第一层理由；第二层是**词表在后端**：概念板块的 138 个名字读
    # `mainline_cache.db`、个股名读行情仓，前端拿不到也不该拿到这些名单。
    # `highlights` 出去的只有"哪几个词要标绿"，名单本身不出接口。
    #
    # ⚠️ 它是**纯词表匹配**（零模型、进程内词表一次载入后每条约 0.25ms，
    # 实测 60 条约 15ms）。接口路径不许调模型这条纪律没有被绕开。
    for it in undetermined:
        it["highlights"] = _highlights_of(it)

    # ── 收容组收哪些（用户口径 2026-09-25，两条口径叠加）──
    #
    # ① 财经快讯：**照旧折叠**（"未给出明确倾向的公开信息 不需要折叠了，
    #    你只需要把来自财经快讯的信息给折叠了，这里面绝大多数新闻都无异议。"）
    #    实测一天上百条快讯，"某指数报跌 0.15%"这种确实无异议也没信息量，
    #    真正刷屏的只有它们。
    # ② 其余类型（研究笔记 / 研报 / 政策…）：**看内容**。不涉及美股、AI、
    #    概念板块、个股的（例行的宏观数据、海外市场、公告罗列）可以折叠；
    #    一旦涉及其中任何一类，就必须留在普通条目里 —— 那是用户要看的信号，
    #    折进去等于"最有价值的那几条被最没价值的那一批藏起来"。
    #
    # ⚠️ 判据看的是**全文**（`_mentions_market` → `market_terms`），
    #    不是展示摘要：一条《碳化硅材料专题会议》的模型摘要里可能一个
    #    板块/个股名都没有（实测），而正文里有 `天岳先进` 与 `第三代半导体`。
    news_only = [it for it in undetermined
                 if str(it.get("kind") or "") == "newswire"]
    unfolded = [it for it in undetermined
                if str(it.get("kind") or "") != "newswire" and _mentions_market(it)]
    quiet = [it for it in undetermined
             if str(it.get("kind") or "") != "newswire"
             and not _mentions_market(it)]
    foldable = news_only + quiet

    by_time = lambda x: sort_key(x.get("published_at"))  # noqa: E731
    signal.sort(key=by_time, reverse=True)
    foldable.sort(key=by_time, reverse=True)
    unfolded.sort(key=by_time, reverse=True)

    # 数量太少就不成组：给一个"1 条"的分组比直接显示那一条更烦人。
    group = (_make_group(foldable)
             if (group_undetermined
                 and len(foldable) >= MIN_UNDETERMINED_GROUP) else None)

    # 未折叠的"无倾向"条目（研究笔记/研报/政策…）与信号条目一样是**普通条目**
    #
    # ⚠️ `foldable` 在**没有成组**时必须一起回到普通条目里。没成组有两种情况，
    #    两种都真的会发生：
    #      · `group_undetermined=False` —— 内部消费者（倾向抽取 / 热议 / 接口取数）
    #      · 可折叠条目不足 `MIN_UNDETERMINED_GROUP` 条，本来就不该成组
    #
    # 少写这一句的后果是**所有"无倾向"快讯凭空消失**：实测 `counts` 里
    # `newswire: 20` 而 `items` 是空列表 —— 而抽取任务看到的正是这个池子，
    # 于是日志显示"抽取 0 条"，看起来像"没有可抽的"。与
    # `zsxq_incremental.fresh_floor` 那次"内容永久不可见"同属一类事故。
    normal = sorted(signal + unfolded + (foldable if group is None else []),
                    key=by_time, reverse=True)

    if group is None:
        # 没有收容组 → 全部普通条目按时间排，取满额度
        out_items = normal[:max(1, limit)]
    else:
        # 分组占一行，普通条目用剩下的额度
        out_items = normal[:max(1, limit - 1)]
        out_items.append(group)

    # 高亮词：上面那轮只算过"无倾向"的条目（折叠判据要用），
    # 这里有方向的条目还没算过 —— 它们同样是页面上要标绿的。
    # 收容组那一行（`is_group`）跳过：它的标题/摘要都是服务端合成的，
    # 不是原文，在里面标绿只会误导。
    for it in out_items:
        if not it.get("is_group"):
            it["highlights"] = _highlights_of(it)

    # ★ 取样后**按时间倒序返回**。
    #
    # 取样用可信度决定"谁能进这一页"，但**展示顺序仍是时间** ——
    # 因为界面要回答的是"最近发生了什么，其中哪些更可核实"。
    # 完全按可信度排会让一条三天前的官方公告压在今天所有快讯之上，
    # 那是另一种误导（用户会以为它刚发生）。
    # 可信度的作用体现在**筛选 tab** 与每条旁边的分数环上。
    #
    # ⚠️ 分组行**不动位置**：它固定排在最后。它是"其余内容"的收容，
    # 插在中间会把时间线打断（用户读到一半突然看到"还有 120 条"）。
    head = out_items if group is None else out_items[:-1]
    head.sort(key=by_time, reverse=True)
    out_items = head if group is None else head + [group]

    # ── ★ 全文落库：**在这一层做**，不依赖定时任务（2026-10-01 第六轮）──
    #
    # ## 为什么搬到这里（用户报障：「点击看全文 → 全文读取失败」）
    #
    # 落库原来**只有** `_intel_tone_extract` 一处（`jobs.py`，每 2 小时一班）。
    # 那条链路的空窗很具体：条目是**随时**被采进来的，而抽取每 2 小时才跑 ——
    # 实测 18:20 那一班之后 `intel_zsxq_collect` 又采了 30 条新笔记
    # （游标 18:20 → 21:20），那批条目在两小时内**点开就是 404**，
    # 而 404 的文案是"未留存（或已超出 3 天留存窗口）"——
    # 用户看到的是"这个功能坏了"，与"刚采进来还没落库"完全不是一回事。
    #
    # ## 为什么这不违反"请求路径不调模型"（硬约束）
    #
    # 落库 = 一次 `content_hash` 判重 + 一次文件追加。判据是**内容指纹**，
    # 算它不花钱；存的是已经拿在手里的清洗后原文（`extract_text`）。
    # 被禁止的是"在请求里做语义抽取"（本地 8B 实测 ~770ms/条），
    # 那件事仍然**只**发生在 `tone_job` 里。倾向也一样：这里只走规则层
    # （纯词表 `in` 判断），不碰模型。
    #
    # ## 为什么两边都写（不是"搬过来、删掉任务那份"）
    #
    #   · 请求路径  兜住"用户正在看的那一批"（本文）
    #   · 定时任务  兜住"没人打开页面时也要有全文"
    # 两份用的是同一个 `save_many`（按 `content_hash` 短路 + 原子写），
    # 所以重复写不会让文件长大（实测见下方日志里的成本）。
    #
    # ## ⚠️ 落库对象是 `taken`（筛选**之前**的池子），不是 `out_items`
    #
    # `out_items` 只是**这一页**（默认 60 条）；用户点开的可能是收容组里
    # 展开的一条，也可能切到"高可信"档再看 —— 那时条目已经不是当前
    # `out_items` 的成员了。落库按**池子**做，与展示档位无关
    # （与 `scan_pool` 同一条理由：展示口径不该决定"我们手里有什么"）。
    #
    # ## 失败**绝不能**影响情报流
    #
    # 全文是"锦上添花"的数据（`body_store` 的 `load` 也是这个口径）。
    # 磁盘满、权限错、文件被别处锁住时，正确行为是"继续返回情报流"，
    # 而不是把 500 抛给用户 —— 所以整段包在 try 里，失败只记日志。
    from src.domain.intel import body_store

    _t0 = time.monotonic()
    try:
        _bodies = body_store.persist(taken, prune_now=False)
    except Exception as exc:  # noqa: BLE001 落库失败不该让情报流挂掉
        _bodies = {}
        logger.warning("全文落库失败（情报流照常返回）：%s", type(exc).__name__)
    _t_body_ms = (time.monotonic() - _t0) * 1000.0

    # 落库与规则层的成本**必须能看见**：它们是这条请求路径上新加的两件事，
    # 而"变慢了"在接口层面只表现为"情报流卡"，看不出是谁。DEBUG 级：
    # 生产默认不刷屏，排障时把它打开就能拿到数字（用户口径要求"报出成本"）。
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "build_feed 成本：全文落库 %.2fms（候选 %d 条，新增 %d 条）｜"
            "规则层倾向 %.2fms（%d 条无抽取结果，其中 %d 条给出方向）",
            _t_body_ms, len(taken), int(_bodies.get("written") or 0),
            _t_rules_ms, _rule_considered, _rule_filled,
        )

    return IntelFeed(
        items=out_items,
        gaps=gaps,
        counts=counts,
        fetched_at=_now_iso(),
        degraded=bool(gaps),
        credibility_dist=dist,
        cluster_stats=cluster_stats,
        tone_dist=tone_dist,
        direction_dist=direction_dist,
        direction_hidden=direction_hidden,
        filter_stats=filter_stats,
        # 收容之前的平铺池（进程内用，不出接口 —— 见字段文档）
        scan_pool=scan_pool,
    )


def _has_direction(item: dict[str, Any]) -> bool:
    """这条情报有没有**明确的原文倾向**（偏多/偏空/利好/利空）。

    ## 判据只有一份：委托给 `alert_bridge._has_direction`

    这里**不自己实现**。原来本函数写的是"只看 `tone.has_tone`"，
    而前端那个【多】/【空】标记、事件告警引擎、告警桥走的是
    `alert_bridge._has_direction` —— 后者多两条判据：

      · `neutral=True` 时**一律不算方向**（哪怕 `tone="偏多"`）；
      · 老存储行**没有 `has_tone` 键**时，按字面值 `偏多/偏空` 兜底
        （第二轮之前的行就是这样，它们不该被静默判成"没方向"）。

    两份实现并存时，差异不会报错，只会表现为"筛出来的与看到的标记不一致"
    —— 而那正是这次要消灭的东西。`alert_bridge` 的注释本身也写着
    "判据与 `service._has_direction` 完全一致"，所以**改这里等于改那份契约**：
    两边必须始终是同一次调用。
    """
    from src.domain.intel.alert_bridge import _has_direction as _canonical

    return _canonical(item.get("tone"))


def direction_of(item: dict[str, Any]) -> str:
    """这条情报的原文倾向 → `"bull"` / `"bear"` / `""`（给不出方向）。

    ## 判据**只有一份**，委托给 `alert_bridge.direction_of`

    "什么叫明确方向"在本仓已经有一个权威实现（`alert_bridge.direction_of`
    → `_has_direction`），事件告警引擎、告警桥、理由文案都走它。
    这里**不重复实现**，只把它的中文输出映射成筛选器用的短码：

        alert_bridge.direction_of  →  本函数  →  前端标记
        "偏多"                      →  "bull"  →  【多】
        "偏空"                      →  "bear"  →  【空】
        ""（未定/未抽取/中性）       →  ""      →  不显示

    自己再写一遍判断的代价是**双份口径必然分叉**，而分叉的表现极难查：
    "筛了【多】却漏掉几条明显带【多】的"。
    """
    from src.domain.intel.alert_bridge import direction_of as _canonical

    return {"偏多": "bull", "偏空": "bear"}.get(_canonical(item), "")


def _make_group(items: list[dict[str, Any]]) -> dict[str, Any]:
    """把一批"给不出方向"的条目收成**一行可展开**的条目。

    ## 为什么用合成条目而不是在前端折叠

    折叠逻辑放前端，就意味着"未定条目"仍然要**全部传过去** ——
    一页 500 条里可能 400 条是宏观快讯，payload 白涨十倍，
    而用户点开才看到（大概率还不点）。合成在服务端做，
    传输量与渲染量都按"一行"算。

    ## 子条目只给**必要字段**

    展开后要能看清"这条是什么、什么时候、哪个平台、几分可信"。
    `summary` 也要给 —— 用户点开就是为了读内容，只给标题等于让他再点一次。
    但**不给** `extra` / `related` / `tone.explain` 这些大字段：
    收容组里几十条，每条都带完整字段会让响应体重新膨胀回去。
    """
    n = len(items)
    latest = items[0].get("published_at") if items else ""

    return {
        "kind": "group",
        # 标题/摘要**不再提"倾向"**（用户口径 2026-09-25：词表映射多空没意义、
        # 判定都错了，界面上把原文倾向整个拿掉）。这一组现在的定位是纯粹的时间线
        # 收纳：把"照旧折叠的财经快讯"与"不涉及个股/板块/美股/AI 的其余内容"
        # 收成一行，免得它们把真正有信息的条目挤出视野。
        # 折叠判据本身没变（见上方 `foldable` 的注释），只是不再自称有方向判断。
        "kind_label": "其他公开信息",
        # ⚠️ 标题/摘要必须说清**这一组里到底有什么** —— 措辞跟着折叠口径改过两次：
        # 先是"所有无倾向条目"，再是"只收财经快讯"，现在是
        # "快讯 + 不涉及个股/板块/美股/AI 的其他内容"。
        # 每改一次口径都要改这里：标题写窄了，用户会以为组里只有快讯，
        # 于是不去展开、直接漏掉想看的内容（真实事故，见下）。
        "title": f"其他公开信息（{n} 条）",
        "summary": (
            f"这一组共 {n} 条。其中财经快讯照旧折叠（多为"
            "“某指数报跌 0.15%”这类例行播报）；其余类型里，"
            "不涉及个股、概念板块、美股或 AI 的也一并收在这里 —— "
            "多为宏观数据、海外市场与例行公告。"
            "涉及上述任何一类的条目都不会被折叠。点开可逐条查看。"
        ),
        "published_at": str(latest or ""),
        "source_alias": "",
        # ⚠️ 平台给**空串**而不是"公开财经信息"：这一组是跨来源的，
        # 标一个具体平台名会让人以为整组都来自那一处。
        "platform": "",
        "codes": [],
        "industry": "",
        "rating_origin": "",
        "agency": "",
        "content_hash": "",
        "extra": {},
        "credibility": None,
        "is_group": True,
        "group_count": n,
        "group_items": [_group_row(it) for it in items],
    }


def _group_row(item: dict[str, Any]) -> dict[str, Any]:
    """收容组里的**一行子条目**（只给展开时要显示的字段）。"""
    cred = item.get("credibility") or {}
    try:
        score = int(cred.get("score"))
    except (TypeError, ValueError):
        score = None
    return {
        "title": str(item.get("title") or ""),
        "summary": str(item.get("summary") or ""),
        "kind_label": str(item.get("kind_label") or ""),
        "published_at": str(item.get("published_at") or ""),
        "platform": str(item.get("platform") or ""),
        "credibility_score": score,
        "content_hash": str(item.get("content_hash") or ""),
        # ⚠️ 内容里的机构名要**透传**（与 `highlights` 同一个理由）：
        # 本函数是白名单投影，漏一个键的表现是"抽到了、接口永远看不到"，
        # 不会有任何报错。而用户口径是"**一定要**前端输出信息"
        # —— 组内条目展开后也得看得到"机构：中泰证券"。
        "institutions": list(item.get("institutions") or []),
        # ⚠️ 分析师名**同样必须透传**（用户口径：孙潇雅、赵宇阳、武超则、
        # 陈果、刘晨明、洪灏这六个名字要推到前端展示）。它与 `institutions`
        # 是同一类展示字段、同一个漏法、同一条纪律 —— 而这次的漏法更隐蔽：
        # 组内条目的渲染路径与顶层不同，漏了只会表现为"展开后看不到分析师"
        # （顶层还正常），看起来像渲染 bug 而不是投影缺键。
        "analysts": list(item.get("analysts") or []),
        # ⚠️ 方向标记（【多】/【空】）要的**最小投影**：前端只判
        # `has_tone` + `tone` 两个值。整份 `tone` 字典**不透传** ——
        # 它带着 `explain` / `bullish` / `bearish` 这些大字段，
        # 而收容组里几十条一起发出去会让响应体重新膨胀（见本函数 docstring）。
        #
        # 今天收容组里**不可能**出现有方向的条目（成组的前提就是"给不出方向"，
        # 见 `build_feed` 的 `signal` / `undetermined` 分流），所以这个键
        # 现在恒为"无方向"。**照样显式透传**：分流规则将来一变（比如允许
        # 未抽取的先入组），没有它就会静默少一个标记，而那种缺失没有任何报错。
        "tone": _tone_marker(item.get("tone")),
        # ⚠️ 展示摘要（单行 + 200 字上限，见 `_summary_of`）**必须透传**：
        # 本函数是白名单投影，漏一个键的表现是"顶层显示了、展开后没有"，
        # 不会有任何报错。用户口径 2026-10-01："精简200字以内，
        # 用户没时间看全文，要效率"——收容组展开后的那几十条同样要看摘要。
        # 没有它时前端退回 `summary`（260 字、可能带换行），
        # 于是"同一页里两种摘要长度"看起来像渲染不一致。
        "summary_text": _summary_of(item),
        # ⚠️ `extract_text` 必须**透传**：收容组里的条目是**另一个 dict**
        # （本函数就是白名单投影），不透传的话 `tone_job` 拿到的是组里的这份，
        # 抽取入参就退回 260 字展示摘要 —— 实测 21 条研究笔记**全部**落在收容组里，
        # 透传前"带 extract_text 的条数"是 **0**，等于整套两头取文根本没生效。
        # 它是内部字段，`IntelFeed.to_public()` 会在**顶层与组内**两处剥掉。
        "extract_text": str(item.get("extract_text") or ""),
        # ⚠️ 高亮词同样要透传（同一个理由）：组内条目展开后也要标绿，
        # 前端拿不到这个词表就只能不标。它是**要出接口**的字段
        # （与 `extract_text` 相反），`to_public()` 不会剥它。
        "highlights": list(item.get("highlights") or []),
    }


def _tone_marker(tone: Any) -> dict[str, Any]:
    """方向标记（【多】/【空】）要的**最小投影**：`{tone, has_tone, source}`。

    前端只靠前两个值决定"要不要在标题/摘要最前面加一个【多】/【空】"；
    `source` 是**审计**用的第三个值（用户口径 2026-10-01："本地模型提取的
    信息…"）：它区分"模型参与的判定"（`rules+llm`）与"纯词表给的"
    （`rules`）—— 界面上据此在 tooltip 里说明来源，而不是把词表猜测
    伪装成模型判定。

    ⚠️ 刻意**不**透传整份 `tone`：那是十几键的字典（含 `bullish`/`bearish`
    明细），而收容组里几十条一起发出去会让响应体膨胀 —— 与 `_group_row`
    的"只给展开时要显示的字段"同一条纪律。

    没有倾向字段（未抽取 / 老数据）时**返回空字典而不是缺键**：
    调用方（前端）不必写 `?.` 兜底，也不会把"没有方向"与"字段没透传"
    混成同一种情况。
    """
    t = tone if isinstance(tone, dict) else {}
    if not t:
        return {}
    return {"tone": str(t.get("tone") or ""),
            "has_tone": bool(t.get("has_tone")),
            "source": str(t.get("source") or "")}


def _from_store(row: dict[str, Any]) -> dict[str, Any] | None:
    """**留存行 → 情报流条目**（`item_store` 的读取侧重建）。

    ## 为什么要在读取侧重建，而不是把存下来的 dict 直接用

    两件事必须**按当前口径现算**，不能用存储时的值：

      · `kind_label` —— 它是展示名，"研究笔记 → 券商作文"改过一次。
        存下来的话改名改不动老数据（同 `body_store.view` 的理由）。
      · `extract_text` —— 抽取入参。留存的兄弟表 `body_store` 里还留着这条的
        全文（同一份 3 天留存），顺手补上；不补的话"留存回来的条目"喂给本地
        模型时只看得到 260 字的展示摘要，抽取质量静默下降。

    `credibility` 反而是**照抄存储值**：它是当时算出来的分数（来源轴 + 内容轴），
    重算会让"同一条老内容"的分数在来源分级表调整后于页面上跳动。
    """
    h = str(row.get("content_hash") or "")
    if not h:
        return None
    kind = str(row.get("kind") or "research_note")
    from src.domain.intel import body_store

    out: dict[str, Any] = {
        "content_hash": h,
        "title": str(row.get("title") or ""),
        "summary": str(row.get("summary") or ""),
        "published_at": str(row.get("published_at") or ""),
        "kind": kind,
        "kind_label": KIND_LABELS.get(kind, kind),
        "source_alias": str(row.get("source_alias") or ""),
        "platform": str(row.get("platform") or ""),
        "codes": list(row.get("codes") or []),
        "industry": str(row.get("industry") or ""),
        "agency": str(row.get("agency") or ""),
        "rating_origin": str(row.get("rating_origin") or ""),
        "credibility": dict(row.get("credibility") or {}),
        #: 标记它来自留存（本轮没取到）。页脚据此说明"其中有几条不是刚取的"，
        #: 排查时也靠它区分"这条怎么来的"。
        "retained": True,
    }
    text = ""
    try:
        text = body_store.view(body_store.get(h))["text"]
    except Exception as exc:  # noqa: BLE001 全文取不到就退回摘要，不抛
        logger.warning("留存条目取全文失败：%s", type(exc).__name__)
    if text:
        out["extract_text"] = extraction_input(text)
    return out


def _passes_filter(item: dict[str, Any], key: str) -> bool:
    """一条情报是否落在某个可信度筛选档里。

    判据复用 `credibility.matches_filter` —— **前后端两套口径漂移**
    是这类筛选最容易出的问题（界面说 80 分算高可信、服务端按 85 筛，
    用户看到"高可信"里有 82 分的、却没有 84 分的）。
    """
    if key in ("", "all"):
        return True
    from src.domain.intel.credibility import Credibility, matches_filter

    raw = item.get("credibility") or {}
    try:
        score = int(raw.get("score"))
    except (TypeError, ValueError):
        return False      # 缺分数的**不进任何档**（不假装它有分）
    cred = Credibility(
        score=score,
        source_base=int(raw.get("source_base") or 0),
        content_base=int(raw.get("content_base") or 0),
        source_reason=str(raw.get("source_reason") or ""),
        content_reason=str(raw.get("content_reason") or ""))
    return matches_filter(cred, str(item.get("kind") or ""), key)


def _balanced_take(items: list[dict[str, Any]], limit: int,
                   sort: str = "credibility") -> list[dict[str, Any]]:
    """取前 N 条：**类型之间轮流，类型内部按 `sort` 决定先后**。

    ## 为什么不能只按时间截断

    纯 `sorted(...)[:limit]` 在真实数据上会**整类消失**。实测：单次聚合
    156 条里 `newswire` 占 140 条，若按时间取前 60，结果几乎是清一色快讯
    —— `broker_report`（券商研报）与 `research_note`（研究笔记）**一条都
    出不来**。而这两类恰恰是用户最想看的，快讯反而是廉价那类。

    原因是数量级差异：日报类快讯一天上百条，研报一天几条。时间序截断
    等价于"按产量分配版面"，产量高的必然挤掉产量低的。

    ## 为什么轮流取样是**全局**的（不在天内做）

    试过"先按天分桶、天内再轮流"，结果**更糟**。原因是实测数据长这样：

        2026-09-25   newswire ×140
        2026-09-24   policy   ×15
        2026-09-22   research_note ×30

    **每一天只有一种类型**（快讯天天有、政策按日发、笔记断续来）。
    于是"按天取"必然退化回单类型 —— `limit=60`（默认值）时返回
    60 条清一色快讯，政策和笔记**一条都没有**，多样性完全失效。

    所以在**这份数据**上，"天分组 / 类型多样 / 严格时间序"三者
    不可能同时满足。取舍是：

      · 服务端保证 **类型多样 + 组内时间序**（这个函数）
      · 界面**不做天分组标题**，改为每行显示日期
        （见 `IntelFeedTab.tsx` 的说明）—— 时序信息由每行承载，
        不靠分组标题，于是也就不存在"同一天被切碎"的问题

    ## `sort` 只影响**谁能进这一页**，不影响展示顺序

    `credibility` 模式下桶内按可信度降序，于是稀缺的高可信条目
    （交易所公告、持牌研报）优先入选。但返回后 `build_feed` 仍按时间重排
    —— 展示顺序必须是时间，否则会误导（见 `build_feed` 的说明）。
    """
    if limit <= 0:
        return []
    from src.infrastructure.connectors.intel_sources import sort_key

    def _rank(it: dict[str, Any]) -> tuple:
        """桶内排序键。**时间永远做兜底**，保证同分时顺序稳定可预测。"""
        t = sort_key(it.get("published_at"))
        if sort == "time":
            return (t,)
        cred = it.get("credibility") or {}
        try:
            score = int(cred.get("score"))
        except (TypeError, ValueError):
            score = -1        # 缺分数的排最后（不假装它有分）
        return (score, t)

    buckets: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        buckets.setdefault(str(it.get("kind") or "other"), []).append(it)
    # 桶内重排：`items` 整体按时间排过，但切桶后必须再排一次 ——
    # 一是各来源时间戳格式不同、不重排会随到达顺序漂移；
    # 二是 `sort=credibility` 时这里才是"谁先入选"的真正决定处。
    for k in buckets:
        buckets[k].sort(key=_rank, reverse=True)
    # 类型顺序：谁有最新一条谁先露头（不写死字典序，否则某个类型永远第一）
    order = sorted(
        buckets,
        key=lambda k: sort_key(buckets[k][0].get("published_at")),
        reverse=True,
    )
    out: list[dict[str, Any]] = []
    idx = 0
    while len(out) < limit:
        progressed = False
        for k in order:
            bucket = buckets[k]
            if idx < len(bucket):
                out.append(bucket[idx])
                progressed = True
                if len(out) >= limit:
                    break
        if not progressed:      # 全部桶取空
            break
        idx += 1
    return out


#: 情报类型 → 中文标签（前端只认这个，不认内部源名）
#:
#: ⚠️ `research_note` 的展示名是**用户口径**（2026-10-01）：「券商作文」。
#: 内部 `kind` 值不变（它已被落库的行、告警闸门、前端筛选共用），
#: 变的只是给人看的那一面 —— 所以 `intel_sources.SOURCE_KINDS` 与
#: 前端的 `KIND_FALLBACK_LABELS` **三处必须同时改**
#: （`tests/unit/test_intel_label_rename.py` 把三处钉在一起）。
KIND_LABELS: Final[dict[str, str]] = {
    "broker_report": "券商研报",
    "newswire": "财经快讯",
    "policy": "政策信号",
    "research_note": "券商作文",
    "other": "其他",
}


def _kind_of(source_name: str) -> str:
    """内部源名 → 对外类型。**不把源名透出去。**"""
    if source_name.startswith("broker_"):
        return "broker_report"
    if source_name.startswith("newswire_"):
        return "newswire"
    if source_name.startswith("policy_"):
        return "policy"
    if source_name.startswith("zsxq"):
        return "research_note"
    return "other"


__all__ = [
    "DEFAULT_LIMIT",
    "KIND_LABELS",
    "IntelFeed",
    "build_feed",
]
