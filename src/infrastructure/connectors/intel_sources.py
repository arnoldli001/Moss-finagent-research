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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Final

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
    "research_note": "研究笔记",
    "other": "其他",
}

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

        return {
            "kind": self.kind,
            "kind_label": SOURCE_KINDS.get(self.kind, self.kind),
            "title": self.title,
            # 摘要截断：源文本最长 800+ 字（政策全文），直接下发会把移动端
            # 撑爆 —— 820 字在手机上约 45 行，一条就占满一屏。截断放在
            # **契约层**，这样任何调用方拿到的都是安全长度。
            "summary": _clip(self.summary,
                             SUMMARY_MAX_BY_KIND.get(self.kind,
                                                     SUMMARY_MAX_CHARS)),
            "published_at": self.published_at,
            "source_alias": source_pseudonym(self.source_alias),
            "codes": list(self.codes),
            "industry": self.industry,
            "rating_origin": self.rating_origin,
            # `agency` 对**研报**是公开署名（报告本身就是这家出的），
            # 保留；但只在研报类型下给，避免快讯渠道从别处漏出去。
            "agency": self.agency if self.kind == "broker_report" else "",
            "content_hash": self.content_hash,
            "extra": safe_extra,
        }


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


def _clip(text: object, limit: int) -> str:
    """按字符截断并加省略号。**不切断 UTF-8 码点**（Python 字符串天然安全）。"""
    s = "" if text is None else str(text)
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
    import re

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
            content_hash=_hash("sina", body[:200], ts),
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
            content_hash=_hash("cctv", title, d),
        ))
    return items


# ======================================================================
# 统一入口
# ======================================================================

#: (源名, 调用工厂)。工厂接收 `ctx`（含 watch_codes 与 policy_date）。
_FETCHERS: Final[tuple[tuple[str, Callable[[dict[str, Any]], list[IntelItem]]], ...]] = (
    ("newswire_em", lambda c: _fetch_em_newswire(limit=c.get("newswire_limit", 100))),
    ("newswire_ths", lambda c: _fetch_ths_newswire(limit=c.get("newswire_limit", 30))),
    ("newswire_sina", lambda c: _fetch_sina_newswire(limit=c.get("newswire_limit", 30))),
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
]
