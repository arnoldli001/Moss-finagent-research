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
SOURCE_KINDS: Final[dict[str, str]] = {
    "broker_report": "券商研报",
    "newswire": "财经快讯",
    "policy": "政策信号",
}

#: 每个源最近一次调用结果（探活用，进程内）。
#: 只存成功与否与耗时，**不存任何来源标识**。
_HEALTH: dict[str, dict[str, Any]] = {}


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
        """转成**可出接口**的形状。

        契约层就把敏感字段挡掉 —— 而不是指望调用方记得脱敏。
        这是 `INTEL_PERMISSION_DESIGN.md` §5.1「第 1 层」的落地：
        `source_url` / `group_id` / `author_id` **根本不存在于返回体**。
        """
        return {
            "kind": self.kind,
            "kind_label": SOURCE_KINDS.get(self.kind, self.kind),
            "title": self.title,
            "summary": self.summary,
            "published_at": self.published_at,
            "source_alias": self.source_alias,
            "codes": list(self.codes),
            "industry": self.industry,
            "rating_origin": self.rating_origin,
            "agency": self.agency,
            "content_hash": self.content_hash,
            "extra": dict(self.extra),
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


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
    """跑一个源。**失败不抛** —— 单源失败不能让整批采集挂掉。"""
    import time

    t0 = time.perf_counter()
    try:
        items = await asyncio.to_thread(fn, ctx)
    except Exception as exc:  # noqa: BLE001 单源失败是常态（限频/改版），必须隔离
        from src.core.redaction import redact

        msg = redact(f"{type(exc).__name__}: {exc}")
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
    "SOURCE_KINDS",
    "IntelItem",
    "fetch_all",
    "health",
]
