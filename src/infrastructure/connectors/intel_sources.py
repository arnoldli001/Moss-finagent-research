"""舆情情报源连接器 —— **不依赖知识星球**的那批（实测可用）。

## 为什么单独一个模块

agent 采集层此前只有知识星球一条路，而它：
  · 用会话级 token（`expires=-1`，关浏览器即失效）；
  · 失效后**无法静默续期**（登录态里只有 access_token，无 session/refresh）；
  · 2026-09-15 失效后**停了 10 天且无人知**。

所以这里接 4 个**零账号零 token** 的公开源，让"某个源挂了"不等于
"整个舆情模块停摆"。这也是 `docs/INVESTMENT_CALENDAR_DESIGN.md` 与
`docs/INTEGRATION_PLAN_zsxq_intel.md` 里"多源 + 单源可用"的落地。

## 实测结论（2026-09-25，见 `docs/_alt_source_probe.txt`）

| 源 | 接口 | 实测 |
|---|---|---|
| 券商研报 | `stock_research_report_em` | 227 行，含评级/机构/盈利预测/PDF |
| 东财全球快讯 | `stock_info_global_em` | **200 条**，标题+摘要+时间+链接 |
| 同花顺快讯 | `stock_info_global_ths` | 20 条 |
| 新浪快讯 | `stock_info_global_sina` | 20 条（仅内容+时间） |
| 新闻联播 | `news_cctv` | 15 条**全文**（政策风向标） |

`stock_news_em` **实测失败**（`ArrowInvalid: invalid escape sequence`
—— akshare 自身正则 bug），故不接入。

## 合规与保密约定（两条硬约束）

1. **来源标识不进响应**：`source_id` / URL / 机构名在内部保留，
   对外一律走 `source_pseudonym()`，详见 `docs/INTEL_PERMISSION_DESIGN.md` §5。
2. **只做描述性统计**：本模块只产出"谁在什么时候说了什么"，
   **不做方向判断**。研报自带的评级属**第三方原文**，按 §4 规则
   标注出处后展示（`rating_origin` 字段），不是平台评级。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Final

logger = logging.getLogger(__name__)

#: 情报源类型 → 中文标签。**只暴露"类型"，不暴露"是哪一家"。**
#:
#: ⚠️ 这张表必须覆盖**所有**会被塞进 `IntelItem` 的 kind。
#: 漏一个的后果是 `.get(kind, kind)` 兜底成**机器名**直接上屏 ——
#: 实测知识星球的条目在界面上显示成了 `research_note`（用户看到的是
#: 一个英文枚举值，而不是"研究笔记"）。
#: `tests/unit/test_intel_source_privacy.py` 里有一条用例锁住这个覆盖性。
SOURCE_KINDS: Final[dict[str, str]] = {
    "broker_report": "券商研报",
    "newswire": "财经快讯",
    "policy": "政策信号",
    #: 用户口径（2026-10-01）：`research_note` 的**展示名**改成「券商作文」。
    #: ⚠️ 内部 `kind` 值**一个字都不改**（仍是 `research_note`）：
    #: 它是 `tone_store` / `tone_job` / 前端筛选 / 告警闸门共用的**键**，
    #: 改名等于让所有已落库的行、所有按 kind 分支的判据集体失配 ——
    #: 而那种失配不会报错，只会表现为"某一类内容凭空消失"。
    "research_note": "券商作文",
    "other": "其他",
}

#: **可公开**的平台名：内部源名 → 平台名。
#:
#: ## 为什么这里可以放明文，而 `source_alias` 不行
#:
#: 用户口径（2026-09-25）：
#
#:    "公开数据的地方（AkShare/腾讯/新浪/东财/QMT）可以暴露；
#:     必须隐藏平台/群身份（WD调研、知识星球、群 id）。"
#:
#: 这两件事看起来矛盾，其实不是：**"我从哪个公开网站抓的"不是壁垒**
#: （谁都能去抓），**"我有一份付费/私域的信息源"才是壁垒**。
#: 财联社的电报、同花顺的快讯，公开网页上就有；而知识星球的调研纪要
#: 需要付费入群 —— 后者泄漏等于把核心资产送人。
#:
#: 所以这里是**白名单**：只有明确公开的财经平台才给标签，
#: 研报机构（`broker-*`）与知识星球（`research-note-zsxq`）一律**不在表里**，
#: 于是 `to_public()` 给它们空串 —— 前端只显示"公开财经信息"。
#:
#: 让平台名可公开还有实际用处：用户在"热议个股"里能看到
#: "财联社 / 东方财富 都提到了它"，这本身就是跨源印证（corroboration），
#: 比一个匿名的来源数量有意义得多。
PUBLIC_PLATFORMS: Final[dict[str, str]] = {
    "newswire-em": "东方财富",
    "newswire-ths": "同花顺",
    "newswire-sina": "新浪财经",
    "newswire-cls": "财联社",
    "newswire-futu": "富途",
    "policy-cctv": "新闻联播",
}

#: 未在 `PUBLIC_PLATFORMS` 里的来源对外统一的标签（**不含来源标识**）。
PUBLIC_PLATFORM_FALLBACK: Final = "公开财经信息"

#: 每个源最近一次调用结果（探活用，进程内）。
#: 只存成功与否与耗时，**不存任何来源标识**。
_HEALTH: dict[str, dict[str, Any]] = {}

#: `extra` 里**允许出接口**的键白名单。
#:
#: 白名单而非黑名单：`url` / `report_url` / `source_url` 都必须**默认不出去**。
#: 拿不到 URL，用户就无法顺着 URL 找到数据源本身 —— 这是保密的关键一环
#: （有链接 = 有入口 = 可以自己去订阅）。
_PUBLIC_EXTRA_KEYS: Final[frozenset[str]] = frozenset({
    "forecast",        # 盈利预测（研报正文里的公开数字）
    "rating_change",   # 评级变动
    "period",          # 报告期
})

#: 绝不能出现在任何出接口响应里的键（供单测断言用）。
#: ⚠️ 这张表是**测试的判据**，不是运行时过滤器 —— 运行时靠
#: `to_public()` 的白名单构造。两者配合：前者防"忘了加"，后者防"加错了"。
FORBIDDEN_PUBLIC_KEYS: Final[frozenset[str]] = frozenset({
    "source_url", "url", "link", "report_url", "pdf", "pdf_url",
    "group_id", "groupid", "gid", "chat_id", "channel_id",
    "author", "author_id", "user_id", "uid", "member_id",
    "topic_id", "raw_html", "html", "token", "cookie", "session",
})


@dataclass
class IntelItem:
    """一条情报。**字段刻意做窄** —— 不含 source_url / group_id / author。"""

    kind: str                 # broker_report | newswire | policy
    title: str
    summary: str
    published_at: str         # ISO8601
    #: 来源假名。内部用它做去重与限流；对外就是它，不可反推。
    source_alias: str = ""
    #: **真实来源名**（如"东方财富-全球财经"）。
    #:
    #: ⚠️ **绝不出接口** —— `to_public()` 是白名单构造，不会带上它。
    #: 它只用于一件事：可信度打分的**来源分级**。分级靠的是中文属性词
    #: （"公告"/"研报"/"快讯"/"传闻"），而 `source_alias` 是英文标识
    #: （`newswire-em`），查不到档 —— 实测把英文标识传进去会让所有条目
    #: 落进保守档 38，整个打分形同虚设。
    source_name: str = ""
    #: 关联标的（代码），无则空
    codes: list[str] = field(default_factory=list)
    #: 行业（第三方原文给的行业分类）
    industry: str = ""
    #: 第三方原文的评级（**仅研报**，且必须同时给 agency）
    rating_origin: str = ""
    #: 出评级的机构（**这是第三方名称，不是机密** —— 研报本身就是公开署名内容）
    agency: str = ""
    #: 证据指纹：用于"同内容去重"，**不可与外链一起给出**
    content_hash: str = ""
    #: 补充字段（盈利预测等），已过脱敏
    extra: dict[str, Any] = field(default_factory=dict)

    def to_public(self) -> dict[str, Any]:
        """转成**可出接口**的形状 —— 这是防 F12 的第一道也是最后一道闸。

        ## 威胁模型是「用户按 F12」，不是「日志泄漏」

        日志脱敏（`src/core/redaction.redact`）挡不住 F12 —— 响应体是**直接
        发给浏览器**的，Network 面板里看得一清二楚。所以必须在**契约层**
        就把来源标识挡掉，而不是指望调用方记得脱敏。

        ## 为什么用「白名单构造」而不是「字典推导剔除」

        剔除式（`{k: v for k, v in ... if k not in BANNED}`）有个隐蔽缺陷：
        **将来给 `IntelItem` 加一个字段，它会自动出现在响应里**。
        白名单式则相反 —— 新字段默认**不出**，必须显式加进来才可见。
        对一个"泄漏即失去壁垒"的资产，默认值必须偏向"不输出"。

        ## `extra` 必须过滤（首版这里漏了）

        首版直接 `dict(self.extra)` 透传，而 `extra` 里装着
        `url` / `report_url` —— **拿到 URL 就等于拿到数据源**。
        现在改成白名单：只放行明确安全的键，URL 一律不出去。
        """
        safe_extra = {
            k: v for k, v in self.extra.items()
            if k in _PUBLIC_EXTRA_KEYS
        }
        # `source_alias` 必须过假名（首版这里漏了）。
        #
        # 构造点写的是 `"newswire-em"` / `"policy-cctv"` / `"broker-太平洋"`
        # 这种**语义明文** —— 等于把"我用了哪几个免费渠道"直接印在响应里。
        # 那正是本项目的壁垒：`source_pseudonym()` 早就写好了，但没人调用它，
        # 于是泄漏一直存在（隐私测试只测了 helper 本身，没测 `to_public` 输出，
        # 所以没抓到）。
        #
        # 注意假名要**稳定**：同一来源永远同一个假名，前端才能按源去重、
        # 分组、限流。所以是在这里做，而不是在构造点各写各的。
        from src.core.redaction import source_pseudonym

        # 可信度（规则层，零 LLM）。**在这里算**而不是在聚合层：
        # 只有这里同时拿得到 `source_name`（真实来源名，用于分级）
        # 与标题/摘要（用于内容分）。聚合层拿到的是已经脱敏的公开 dict。
        #
        # ⚠️ 延迟导入：`src.domain.intel.credibility` 反过来不该依赖连接器，
        # 在模块顶部导入会形成环。
        from src.domain.intel.credibility import score_item

        cred = score_item(
            source_name=self.source_name or self.agency or self.source_alias,
            kind=self.kind, title=self.title, summary=self.summary,
            agency=self.agency)

        return {
            "kind": self.kind,
            "kind_label": SOURCE_KINDS.get(self.kind, self.kind),
            # 标题也过一遍富文本剥离：知识星球有整条主题以 `<e>` 开头的
            # （分享链接类），标题里同样会带平台地址。
            "title": _strip_rich_tags(self.title),
            "summary": _clip(self.summary,
                             SUMMARY_MAX_BY_KIND.get(self.kind,
                                                     SUMMARY_MAX_CHARS)),
            "published_at": self.published_at,
            "source_alias": source_pseudonym(self.source_alias),
            # 公开平台名（**可空**）。用户口径：公开数据平台可以暴露
            # （"公开数据的地方（AkShare/腾讯/新浪/东财/QMT）可以暴露"），
            # 只有平台/群身份要藏。所以这里只映射**公开财经平台**，
            # 研报机构与知识星球一律给空串 —— 白名单式，加源时默认不暴露。
            "platform": PUBLIC_PLATFORMS.get(self.source_alias, ""),
            "codes": list(self.codes),
            "industry": self.industry,
            "rating_origin": self.rating_origin,
            # `agency` 对**研报**是公开署名（报告本身就是这家出的），
            # 保留；但只在研报类型下给，避免快讯渠道从别处漏出去。
            "agency": self.agency if self.kind == "broker_report" else "",
            "content_hash": self.content_hash,
            "extra": safe_extra,
            # 可信度：**可复算、可展开看构成**（`explain`）。
            # 它只描述"多可核实"，不含任何方向判断。
            "credibility": cred.to_public(),
            # 内容里出现的**机构名**（"中泰证券"/"天风证券"…）。
            #
            # ## 用户口径（2026-10-01）
            #
            # > "券商名不一定要告警，但是一定要前端输出信息。"
            #
            # 所以它**不是**告警触发条件（见 `domain/intel/alert_rules`），
            # 但必须出现在前端 —— 用户要能看见"这条笔记是哪家机构出的 /
            # 提到了哪家机构"，界面上会渲染成"机构：中泰证券"。
            #
            # ## ⚠️ 它与"来源平台"毫无关系，别混淆
            #
            #   · 本字段       **内容里写的机构**（研报本来就公开署名）→ 可以出
            #   · `source_alias` / `platform`  这条**来自哪个渠道**（知识星球…）
            #     → 一律不出，走 `source_pseudonym()` 白名单
            # 加这个字段**不削弱**来源隐私纪律：它一个字都不透露渠道身份。
            #
            # 空列表是正常值（绝大多数快讯里没有机构名）。
            "institutions": self._institutions(),
            # 内容里命中的**分析师名**（用户点名的六人名单，见
            # `alert_rules.ANALYST_WATCHLIST`）。
            #
            # ## 用户口径（2026-10-01）
            #
            # > "股票名 板块名 券商 孙潇雅、赵宇阳、武超则、陈果、刘晨明、洪灏
            # >  ……推送到前端展示。"
            #
            # 这六个人原先**只用于告警触发**（`alert_bridge`），界面上一个字都
            # 看不到 —— 用户现在要求他们**上屏**。所以这里与 `institutions`
            # 完全平行：同一个扫描实现、同样是展示字段、同样在收容组的
            # `_group_row()` 里显式透传（漏一处的表现就是"抽到了但永远看不到"，
            # 不会有任何报错）。
            #
            # ⚠️ 与渠道身份**毫无关系**：名单命中的是**原文里写的分析师姓名**
            # （公开署名内容），不是"这条来自知识星球"。渠道身份照旧走
            # `source_pseudonym()`，一个字都不出。
            "analysts": self._analysts(),
        }

    def _institutions(self) -> list[str]:
        """内容里命中的机构名（**展示用**，见 `to_public` 里那一段说明）。

        ⚠️ 与告警侧（`alert_rules.institutions`）**共用同一个实现** ——
        判据只能有一份：两处各写一遍必然漂移，而漂移的表现是
        "前端显示了机构名、告警侧却没命中"（或反过来），两边都对不上账。

        ⚠️ 契约层（本层）只有**未清洗的原文**，而告警侧扫的是清洗后文本。
        这里扫原文是**有意的**：清洗（`content_filter.strip_noise`）会剥掉
        星球署名，而那是**渠道**信息不是机构名，不影响本字段；
        真正影响的是"剥了之后还剩什么"，那由 `build_feed` 在清洗之后
        用同一份实现**再补一次**（见那里对 `institutions` 的处理）。
        两层都扫，取并集，宁可多给一个名字。
        """
        from src.domain.intel import alert_rules

        try:
            return alert_rules.institutions({
                "title": self.title, "summary": self.summary})
        except Exception:  # noqa: BLE001 展示字段失败不该让整条出不了接口
            return []

    def _analysts(self) -> list[str]:
        """内容里命中的**分析师名**（用户点名的六人名单）—— 展示用。

        ⚠️ 与告警侧（`alert_rules.analysts`）**共用同一个实现**，理由与
        `_institutions` 完全相同：判据只能有一份。两处各写一遍必然漂移，
        而漂移的表现是"前端显示了名字、告警侧却没命中"（或反过来）。

        ⚠️ 契约层（本层）只有**未清洗的原文**，所以这里扫原文；清洗之后
        `build_feed` 会用同一份实现**再补一次**（清洗会剥掉星球署名，
        而署名附近常带分析师名 —— 两层都扫、取并集，宁可多给一个名字）。
        """
        from src.domain.intel import alert_rules

        try:
            return alert_rules.analysts({
                "title": self.title, "summary": self.summary})
        except Exception:  # noqa: BLE001 展示字段失败不该让整条出不了接口
            return []


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


#: 单条摘要的最大字符数（出接口前截断）
#:
#: 实测源文本最长 820 字（政策全文），移动端一行约 18 字，820 字是 45 行 ——
#: 一条就占满一屏，卡片列表直接不可用。截断放在契约层，前端拿到就是安全长度。
#: 「政策信号」这类正文价值高的给宽一点，「快讯」本来就短。
SUMMARY_MAX_CHARS: Final[int] = 220

#: 按类型区分的摘要上限（缺省用 `SUMMARY_MAX_CHARS`）
SUMMARY_MAX_BY_KIND: Final[dict[str, int]] = {
    "policy": 300,        # 政策全文有价值，留宽些
    "research_note": 260,
    "broker_report": 200,
    "newswire": 160,
}


#: 上游正文里**夹带平台标识的内联标签**。
#:
#: 实测知识星球的正文长这样（`<e>` 是它自己的富文本标签）：
#:
#:     ...英伟达 CoWoS-L 扩产展望 <e type="web"
#:     href="https%3A%2F%2Fwx.zsxq.com%2Fmweb%2F...">网页链接</e>
#:
#: 两个坑叠在一起，导致它**逃过了常规 URL 过滤**：
#:
#:   ① 地址是**百分号编码**的（`https%3A%2F%2F…`），不是裸 `https://`
#:   ② `wx.zsxq.com` 是**协议相对**写法，连 `//` 前缀都没有
#:
#: 而过滤规则通常写成"匹配 `https?://`"，于是 `wx.zsxq.com` 就这么
#: 跟着 `summary` 上了接口 —— 用户按 F12 一眼看到数据源平台名。
#:
#: 这里**整个标签连同内容一起删掉**：`<e>` 的内容从来只是"网页链接"
#: 这类占位文字，没有信息量；保留它反而会让摘要里剩一句无意义的占位语。
_RICH_TAG_RE: Final = re.compile(r"<e\b[^>]*>(?:.*?</e>)?", re.S | re.I)


def _strip_rich_tags(text: object) -> str:
    """剥掉上游正文里的内联富文本标签（含其中的百分号编码地址）。"""
    s = "" if text is None else str(text)
    if not s or "<e" not in s.lower():
        return s
    # ① 完整标签（含内容）
    s = _RICH_TAG_RE.sub("", s)
    # ② 属性碎片：有的正文被截断，`</e>` 或结尾的 `>` 都不在
    s = re.sub(r'type="web"\s*href="[^"]*"\s*/?>?', "", s)
    # ③ 兜底：**任何**还没配平的 `<e`，从这里截到串尾。
    #    不加这条时实测会残留 `…展望 <e type="web" href="https%3A%2F%2Fwx.zsxq.com%2Fy"`
    #    （源文被截断在半途），`<e` 与 `%2F` 照样上了接口。
    s = re.sub(r"<e\b.*$", "", s, flags=re.S | re.I)
    # 收尾：多出来的空白与空行
    return re.sub(r"[ \t]{2,}", " ", s).strip()


def strip_rich_tags(text: object) -> str:
    """公开入口：剥掉上游正文里的内联富文本标签（**与脱敏链路同一份实现**）。

    ## 为什么要有这个公开名字

    抽取链路（`service.extraction_input`）必须先清洗**再压缩**：
    实测有一条笔记的"尾部 300 字"整段是

        …%E7%89%87%E4%BF%A1%E6%81%AF%23" />

    也就是 URL 编码后的标签残留 —— 因为那时压缩（取两头各 300 字）
    是直接作用在**原始文本**上的，`<e …>` 标签本身就在被取的那 300 字里，
    切完才想起来要清洗，标签的头部已经被切掉了，谁也救不回来。

    那个缺陷的修法是把顺序倒过来（见 `_strip_rich_tags` 的三条规则）。
    但**修法不能是"再写一份剥离逻辑"**：这份逻辑里每一条都对应一次实测
    泄漏（百分号编码、协议相对地址、被截断的标签），抄一份出去必然漂移，
    而漂移的表现是"脱敏那条路干净了，抽取这条路还在把平台域名喂给模型"。
    所以这里只暴露一个名字，实现仍然只有一份。
    """
    return _strip_rich_tags(text)


def _clip(text: object, limit: int) -> str:
    """按字符截断并加省略号。**不切断 UTF-8 码点**（Python 字符串天然安全）。"""
    s = _strip_rich_tags(text)
    if limit <= 0 or len(s) <= limit:
        return s
    return s[:limit].rstrip() + "…"


#: 各来源的时间戳格式（实测三种，互不相同）
_TS_PATTERNS: Final[tuple[tuple[str, str], ...]] = (
    # 紧凑日期：`20260924`（部分快讯只给日期）
    ("compact", r"^(\d{4})(\d{2})(\d{2})$"),
    # 带时间（可含 `T`、小数秒、无冒号时区）：`2026-09-25T13:15:41.340+0800`
    ("iso", r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?"),
)


def sort_key(published_at: object) -> str:
    """时间戳 → **可排序的归一化键** `YYYY-MM-DDTHH:MM:SS`。

    ## 为什么必须归一化，不能直接比字符串

    各来源的时间戳格式**互不相同**，实测三种：

        `20260924`                       （快讯，只有日期）
        `2026-09-25 04:26:03`            （快讯，带时间）
        `2026-09-25T13:15:41.340+0800`   （知识星球，带毫秒与无冒号时区）

    直接 `sorted(..., key=lambda x: x["published_at"])` 会得到**错的顺序**：
    ASCII 里 `-`(0x2D) < `0`(0x30)，所以 `'2026-09'…` 排在 `'20260924'` **之前**，
    于是"9 月 24 日的快讯"被排到了"9 月 25 日的笔记"后面。

    症状在界面上表现为**按天分组后出现 `今天 / 09-24 / 今天 / 09-24`** 这种
    来回跳的分组 —— 看起来像分组逻辑坏了，其实是排序键坏了。

    解析不出来的原样返回（稳定排序下至少不会崩，也不会把数据丢掉）。
    """
    s = "" if published_at is None else str(published_at).strip()
    if not s:
        return ""
    for kind, pat in _TS_PATTERNS:
        m = re.match(pat, s)
        if not m:
            continue
        g = m.groups()
        if kind == "compact":
            return f"{g[0]}-{g[1]}-{g[2]}T00:00:00"
        hh, mm, ss = g[3], g[4], g[5] or "00"
        return f"{g[0]}-{g[1]}-{g[2]}T{hh}:{mm}:{ss}"
    return s


def _hash(*parts: object) -> str:
    raw = "\x1f".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()[:16]


def _record_health(name: str, ok: bool, ms: float, err: str = "") -> None:
    _HEALTH[name] = {
        "ok": ok,
        "at": _now_iso(),
        "ms": round(ms, 1),
        # 错误信息过一遍 redact：异常里常带完整请求 URL
        "error": err[:160],
    }


def health() -> dict[str, dict[str, Any]]:
    """各源最近一次调用健康度。**不含任何来源标识。**"""
    return {k: dict(v) for k, v in _HEALTH.items()}


# ======================================================================
# 各源实现 —— 统一签名：() -> list[IntelItem]
# ======================================================================

def _fetch_broker_reports(symbol: str, *, limit: int = 30) -> list[IntelItem]:
    """券商研报（东财）。`symbol` 为六位股票代码。

    ⚠️ 这个接口**一次只返回一只股票**的研报。要做"全市场研报流"需按
    自选池/关注池批量调用 —— 调用方负责控频（见 `fetch_all` 的池参数）。
    """
    import akshare as ak

    df = ak.stock_research_report_em(symbol=symbol)
    items: list[IntelItem] = []
    for _, row in df.head(limit).iterrows():
        title = str(row.get("报告名称", "") or "")
        agency = str(row.get("机构", "") or "")
        rating = str(row.get("东财评级", "") or "")
        date = str(row.get("日期", "") or "")
        code = str(row.get("股票代码", "") or "")
        industry = str(row.get("行业", "") or "")
        pdf = str(row.get("报告PDF链接", "") or "")
        items.append(IntelItem(
            kind="broker_report",
            title=title,
            summary=f"{agency}：{title}" if agency else title,
            published_at=date,
            # 研报是**公开署名内容**，机构名不是机密 → 作为来源身份保留
            source_alias=f"broker-{agency}" if agency else "broker",
            source_name=f"{agency}研究所" if agency else "券商研究所",
            codes=[code] if code else [],
            industry=industry,
            rating_origin=rating,
            agency=agency,
            content_hash=_hash("broker", code, title, date, agency),
            extra={
                "report_url": pdf,      # 内部保留，to_public 不输出
                "forecast": {
                    y: {
                        "eps": _num(row.get(f"{y}-盈利预测-收益")),
                        "pe": _num(row.get(f"{y}-盈利预测-市盈率")),
                    }
                    for y in ("2026", "2027", "2028")
                },
            },
        ))
    return items


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # NaN → None（本项目"不猜"口径）


def _fetch_em_newswire(*, limit: int = 100) -> list[IntelItem]:
    """东财全球快讯（实测 200 条，当日）。"""
    import akshare as ak

    df = ak.stock_info_global_em()
    items: list[IntelItem] = []
    for _, row in df.head(limit).iterrows():
        title = str(row.get("标题", "") or "")
        digest = str(row.get("摘要", "") or "")
        ts = str(row.get("发布时间", "") or "")
        link = str(row.get("链接", "") or "")
        items.append(IntelItem(
            kind="newswire",
            title=title,
            summary=digest,
            published_at=ts,
            source_alias="newswire-em",
            source_name="东方财富-全球财经快讯",
            content_hash=_hash("em", title, ts),
            extra={"url": link},
        ))
    return items


def _fetch_ths_newswire(*, limit: int = 30) -> list[IntelItem]:
    """同花顺快讯。"""
    import akshare as ak

    df = ak.stock_info_global_ths()
    items: list[IntelItem] = []
    for _, row in df.head(limit).iterrows():
        title = str(row.get("标题", "") or "")
        body = str(row.get("内容", "") or "")
        ts = str(row.get("发布时间", "") or "")
        items.append(IntelItem(
            kind="newswire",
            title=title,
            summary=body,
            published_at=ts,
            source_alias="newswire-ths",
            source_name="同花顺-全球直播快讯",
            content_hash=_hash("ths", title, ts),
            extra={"url": str(row.get("链接", "") or "")},
        ))
    return items


def _fetch_sina_newswire(*, limit: int = 30) -> list[IntelItem]:
    """新浪快讯（无标题，取内容首句作为标题）。"""
    import akshare as ak

    df = ak.stock_info_global_sina()
    items: list[IntelItem] = []
    for _, row in df.head(limit).iterrows():
        body = str(row.get("内容", "") or "")
        ts = str(row.get("时间", "") or "")
        head = body[:60].replace("\n", " ")
        items.append(IntelItem(
            kind="newswire",
            title=head,
            summary=body,
            published_at=ts,
            source_alias="newswire-sina",
            source_name="新浪-7x24快讯",
            content_hash=_hash("sina", body[:200], ts),
        ))
    return items


def _fetch_cls_newswire(*, limit: int = 30) -> list[IntelItem]:
    """财联社电报（实测 20 条/次）。

    ## 为什么加它（用户点名的平台）

    > "把几大平台（雪球/东方财富股吧/同花顺/财联社/百度人气榜/韭研公社）的
    >  热点事件和个股，聚合汇总显示在情报流或事件告警里。"

    财联社是这批平台里**唯一能免费拿到、且带"红字"级别的重要电报**的源。
    它的内容形态是"财联社X月X日电，……"，标题与内容高度重合，
    所以 `title` 取标题、`summary` 取内容，由 `content_filter` 去重。

    ⚠️ 它的 `发布日期`/`发布时间` 是**两列**，必须拼起来 ——
    只取 `发布时间`（`15:36:44`）会丢掉日期，跨日排序会把昨天的电报
    排到今天前面（本项目在 `sort_key` 上已经踩过一次跨格式排序的坑）。
    """
    import akshare as ak

    df = ak.stock_info_global_cls(symbol="全部")
    items: list[IntelItem] = []
    for _, row in df.head(limit).iterrows():
        title = str(row.get("标题", "") or "")
        body = str(row.get("内容", "") or "")
        day = str(row.get("发布日期", "") or "").strip()
        clock = str(row.get("发布时间", "") or "").strip()
        ts = f"{day} {clock}".strip()
        items.append(IntelItem(
            kind="newswire",
            title=title or body[:60].replace("\n", " "),
            summary=body,
            published_at=ts,
            source_alias="newswire-cls",
            source_name="财联社-电报",
            content_hash=_hash("cls", title, body[:120], ts),
        ))
    return items


def _fetch_futu_newswire(*, limit: int = 50) -> list[IntelItem]:
    """富途快讯（实测 50 条/次，带原文链接）。

    ⚠️ 标题里有**占位式长标题**（实测偶发整篇正文被塞进标题列、
    内容列为空）。所以标题为空时用内容首句兜底，反之亦然 ——
    否则会产出"有标题没正文"的条目，被 `content_filter` 判成
    `too_short` 丢掉，表现为"这个源采到了但一条都不显示"。
    """
    import akshare as ak

    df = ak.stock_info_global_futu()
    items: list[IntelItem] = []
    for _, row in df.head(limit).iterrows():
        title = str(row.get("标题", "") or "").strip()
        body = str(row.get("内容", "") or "").strip()
        ts = str(row.get("发布时间", "") or "").strip()
        link = str(row.get("链接", "") or "").strip()
        if not body:
            body = title
        if not title:
            title = body[:60].replace("\n", " ")
        items.append(IntelItem(
            kind="newswire",
            title=title,
            summary=body,
            published_at=ts,
            source_alias="newswire-futu",
            source_name="富途-快讯",
            content_hash=_hash("futu", title, ts),
            extra={"url": link},
        ))
    return items


def _fetch_policy_cctv(date: str, *, limit: int = 30) -> list[IntelItem]:
    """新闻联播文字稿 —— 国内政策信号最权威的公开源（按日全量）。

    `date` 形如 `20260924`（一般取"上一日"，当日稿要晚间才出）。
    """
    import akshare as ak

    df = ak.news_cctv(date=date)
    items: list[IntelItem] = []
    for _, row in df.head(limit).iterrows():
        title = str(row.get("title", "") or "")
        body = str(row.get("content", "") or "")
        d = str(row.get("date", "") or "")
        items.append(IntelItem(
            kind="policy",
            title=title,
            summary=body,
            published_at=d,
            source_alias="policy-cctv",
            source_name="央视-新闻联播政策",
            content_hash=_hash("cctv", title, d),
        ))
    return items


# ======================================================================
# 统一入口
# ======================================================================

#: (源名, 调用工厂)。工厂接收 `ctx`（含 watch_codes 与 policy_date）。
#:
#: ⚠️ 这里是**并行合并**（`asyncio.gather`），不是 `source_chain` 文档里写的
#: 降级链 —— 那个模块只有 `SOURCE_LABELS` 与注释在用，链路语义从未落地。
#: 合并是有意的：快讯类源各自覆盖不同的时间段与口径，合并后才有"跨源同文"
#: 可言（`related.py` 的 corroboration 正是靠这个），降级反而会丢掉这一点。
_FETCHERS: Final[tuple[tuple[str, Callable[[dict[str, Any]], list[IntelItem]]], ...]] = (
    ("newswire_em", lambda c: _fetch_em_newswire(limit=c.get("newswire_limit", 100))),
    ("newswire_ths", lambda c: _fetch_ths_newswire(limit=c.get("newswire_limit", 30))),
    ("newswire_sina", lambda c: _fetch_sina_newswire(limit=c.get("newswire_limit", 30))),
    ("newswire_cls", lambda c: _fetch_cls_newswire(limit=c.get("newswire_limit", 30))),
    ("newswire_futu", lambda c: _fetch_futu_newswire(limit=c.get("newswire_limit", 60))),
    ("policy_cctv", lambda c: _fetch_policy_cctv(c["policy_date"], limit=30)),
)


async def _run_one(name: str, fn: Callable[[dict[str, Any]], list[IntelItem]],
                   ctx: dict[str, Any]) -> tuple[str, list[IntelItem], str]:
    """跑一个源。**失败不抛** —— 单源失败不能让整批采集挂掉。

    ⚠️ 失败信息用 `sanitize_error()` 而**不是** `str(exc)`：
    上游异常文本天然带完整请求 URL（`... for url 'https://api.zsxq.com/...'`），
    而渗透工具会**专门构造上游失败来读这段文本** —— 一次超时就能问出数据源。
    `sanitize_error` 是白名单式：只保留"哪一类失败"，不保留"哪个地址失败"。
    """
    import time

    t0 = time.perf_counter()
    try:
        items = await asyncio.to_thread(fn, ctx)
    except Exception as exc:  # noqa: BLE001 单源失败是常态（限频/改版），必须隔离
        from src.core.redaction import sanitize_error

        msg = sanitize_error(exc)
        _record_health(name, False, (time.perf_counter() - t0) * 1000, msg)
        logger.warning("情报源 %s 采集失败：%s", name, msg)
        return name, [], msg
    _record_health(name, True, (time.perf_counter() - t0) * 1000)
    return name, items, ""


async def fetch_all(
    *,
    watch_codes: list[str] | None = None,
    policy_date: str = "",
    broker_per_code: int = 20,
) -> tuple[list[IntelItem], dict[str, str]]:
    """并发拉取全部**无依赖**源。

    返回 `(items, failures)`。`failures` 是 `{源名: 错误}` ——
    调用方据此落"数据缺口"，**不要静默当成"今天没有内容"**。
    """
    ctx: dict[str, Any] = {
        "newswire_limit": 100,
        "policy_date": policy_date,
    }

    jobs = [_run_one(n, f, ctx) for n, f in _FETCHERS]

    # 券商研报按标的逐个拉（接口一次只给一只股票），限量控频
    codes = list(watch_codes or [])[:8]
    for code in codes:
        jobs.append(_run_one(
            f"broker_{code}",
            (lambda cc: (lambda _c: _fetch_broker_reports(cc, limit=broker_per_code)))(code),
            ctx))

    results = await asyncio.gather(*jobs)
    items: list[IntelItem] = []
    failures: dict[str, str] = {}
    for name, got, err in results:
        items.extend(got)
        if err:
            failures[name] = err

    # 跨源去重：同一 content_hash 只留一条（保留先到的）
    seen: set[str] = set()
    deduped: list[IntelItem] = []
    for it in items:
        if it.content_hash and it.content_hash in seen:
            continue
        seen.add(it.content_hash)
        deduped.append(it)

    logger.info("情报源采集完成：%d 条（去重前 %d），失败 %d 个源",
                len(deduped), len(items), len(failures))
    return deduped, failures


__all__ = [
    "FORBIDDEN_PUBLIC_KEYS",
    "SOURCE_KINDS",
    "IntelItem",
    "fetch_all",
    "health",
    "strip_rich_tags",
]
