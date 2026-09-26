"""开源策略案例库：从**公开可抓取**的源增量抓取策略案例，存库供前端展示。

## 一个必须先说清的定位问题

网上分享的"回测成功的策略"**不是已验证的结论**，而是线索。它们的共同问题：

- 绝大多数**没有说明样本区间、费用、滑点、复权口径**；
- 大量存在**未来函数**（用当日收盘价成交、用未公布财报、用全样本排序）；
- **选择性报告**严重（亏的策略不会发出来）。

所以本模块的产物每一条都带 `verified=0` 与来源链接，界面上必须原样展示
"**网络公开说法，未经本项目复现验证**"。把它们当成"策略库"直接照做，
比没有更危险 —— 这一点写在这里，也写在返回给前端的每个字段里。

## 只接**能真正抓到内容**的源（实测筛选）

2026-09-16 实测可达性：

| 源 | 结果 | 是否采用 |
|----|------|---------|
| GitHub 搜索 API | 200 / 161KB，结构化 JSON | ✅ 采用 |
| arXiv q-fin | 200 / 8KB，Atom XML | ✅ 采用 |
| 雪球搜索 | 200 / 110KB | ✅ 采用 |
| CSDN 搜索 | 200 / 174KB | ✅ 采用 |
| 聚宽社区 | 200 但仅 3KB（JS 壳，无正文） | ❌ 排除 |
| 新浪财经 RSS | 404 | ❌ 排除 |

**"HTTP 200 不等于能抓到东西"** —— 聚宽就是典型：状态码正常、字节数很小、
正文全靠前端渲染。所以适配器必须校验"真的解析出条目"，否则该源算失败。

## 增量与去重

案例 ID = 来源链接的 SHA-256 前 16 位。重复抓取只更新时间与摘要，
不产生重复行 —— 每周/每天定时抓也不会把表撑爆。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.core.errors import (
    BRIEF_TIGHT,
    brief,
)

logger = logging.getLogger(__name__)

CASE_TABLE = "quant_strategy_case"
#: 单次 HTTP 抓取超时（秒）。名字里带 `HTTP` 是刻意的：本模块原叫
#: `HTTP_TIMEOUT`，与 `quant/quant_select_worker.py` 的同名子进程超时
#: （1800 s）撞车，量纲与用途都不同。
HTTP_TIMEOUT = 25
USER_AGENT = "MossFinAgent-Research/0.1 (+local research; respects robots)"

DISCLAIMER = ("以下为**网络公开分享的策略案例**，仅作线索，未经本项目复现验证。"
              "公开策略普遍存在样本区间不明、费用/滑点未计、未来函数、"
              "以及只发布盈利案例的选择性报告问题 —— 请勿直接照做。")

# 主题分类：把抓到的案例归入可读的主题，便于表格按主题浏览
THEME_KEYWORDS: dict[str, tuple[str, ...]] = {
    "动量/趋势": ("momentum", "trend", "动量", "趋势", "均线", "突破", "breakout",
                  "moving average", "cross-sectional momentum", "follow",
                  "channel", "macd", "turtle", "海龟"),
    "价值/基本面": ("value", "估值", "基本面", "财务", "quality", "质量",
                    "factor investing", "roe", "dividend", "红利", "股息",
                    "fundamental", "低估值", "价值投资"),
    "波动/风险": ("volatility", "波动", "风险", "risk parity", "drawdown",
                  "止损", "回撤", "tail", "hedge", "对冲", "var ", "cvar",
                  "风险平价", "波动率"),
    "机器学习": ("machine learning", "deep learning", "reinforcement",
                 "神经网络", "机器学习", "深度学习", "lstm", "transformer",
                 "neural", "xgboost", "lightgbm", "ai ", "强化学习"),
    "统计套利": ("arbitrage", "套利", "pairs trading", "配对", "mean reversion",
                 "反转", "statistical", "reversal", "cointegration", "协整"),
    "组合优化": ("portfolio", "组合", "optimization", "优化", "asset allocation",
                 "配置", "weight", "efficient frontier", "risk budget",
                 "仓位", "择时"),
    "因子研究": ("factor", "因子", "ic ", "icir", "alpha", "multi-factor",
                 "多因子", "选股", "stock selection", "风格", "barra",
                 "量价", "alpha101", "worldquant"),
    "打板/情绪": ("打板", "涨停", "情绪", "龙头", "龙虎榜", "sentiment",
                  "limit up", "游资", "题材"),
    # 这一类对本项目**特别重要**：整个量化模块的设计重心就是防未来函数与过拟合。
    # 实测发现"其它"里绝大多数其实是这类内容（未来函数、回测避坑、过拟合、
    # 幸存者偏差），归到"其它"等于把最有价值的一批线索埋掉了。
    "回测方法论": ("未来函数", "look-ahead", "lookahead", "look ahead",
                   "回测避坑", "过拟合", "overfit", "数据泄露", "leakage",
                   "幸存者偏差", "survivorship", "前视偏差", "data snooping",
                   "样本外", "out-of-sample", "walk forward", "walk-forward",
                   "滑点", "手续费", "交易成本", "复权", "point-in-time",
                   "回测陷阱", "虚假收益", "回测误差"),
}

# 类型识别：把"框架/工具/教程"与"真正的策略案例"分开。
#
# 为什么必须分：GitHub 搜索"量化 回测"会返回一大堆回测**框架**
# （backtrader、vnpy 这类），它们是工具而不是策略。用户在"策略案例"表里
# 看到一排框架，会以为抓取坏了 —— 实测第一版就是这样：18 条 GitHub 结果里
# 真正讲策略的不到三分之一。
KIND_KEYWORDS: dict[str, tuple[str, ...]] = {
    "框架/工具": ("framework", "框架", "backtrader", "vnpy", "backtest engine",
                  "回测引擎", "toolkit", "工具", "平台", "platform", "sdk",
                  "panel", "工作台", "dashboard", "数据库", "接口", "api"),
    "教程/笔记": ("tutorial", "教程", "笔记", "入门", "learn", "入门到",
                  "读书", "课程", "guide", "how to", "从零"),
    "学术论文": ("arxiv", "paper", "we propose", "this paper", "we show",
                 "empirical evidence", "we document"),
}


def classify_kind(*texts: str) -> str:
    """判断这条是「策略案例」还是「框架/工具」「教程」「学术论文」。"""
    blob = " ".join(text.lower() for text in texts if text)
    for kind, keywords in KIND_KEYWORDS.items():
        if any(keyword in blob for keyword in keywords):
            return kind
    return "策略案例"


def classify_theme(*texts: str) -> str:
    """按关键词给案例归类（归不进任何一类就标"其它"）。"""
    blob = " ".join(text.lower() for text in texts if text)
    best, score = "其它", 0
    for theme, keywords in THEME_KEYWORDS.items():
        hits = sum(1 for keyword in keywords if keyword in blob)
        if hits > score:
            best, score = theme, hits
    return best


def case_id(url: str) -> str:
    return hashlib.sha256(str(url).strip().encode("utf-8")).hexdigest()[:16]


@dataclass
class StrategyCase:
    """一条公开策略案例。"""

    id: str
    theme: str
    title: str
    summary: str
    source_name: str
    source_url: str
    published_at: str = ""
    fetched_at: str = ""
    tags: list[str] = field(default_factory=list)
    kind: str = "策略案例"      # 策略案例 / 框架工具 / 教程笔记 / 学术论文
    # 永远是 0：本模块不做复现验证，也不假装验证过
    verified: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "theme": self.theme, "title": self.title,
            "summary": self.summary, "source_name": self.source_name,
            "source_url": self.source_url, "published_at": self.published_at,
            "fetched_at": self.fetched_at, "tags": self.tags,
            "kind": self.kind, "verified": self.verified,
            "disclaimer": DISCLAIMER,
        }


# ==================================================================
# HTTP
# ==================================================================


def _http_get(url: str, *, timeout: int = HTTP_TIMEOUT,
              accept: str = "") -> bytes:
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.read()


# ==================================================================
# 源适配器
# ==================================================================


@dataclass
class SourceSpec:
    """一个可抓取的源。"""

    name: str
    kind: str                 # github / arxiv / xueqiu / csdn / rss
    enabled: bool = True
    queries: tuple[str, ...] = ()
    endpoint: str = ""
    limit: int = 10
    tags: tuple[str, ...] = ()
    disabled_reason: str = ""


def _fetch_github(spec: SourceSpec) -> list[StrategyCase]:
    """GitHub 搜索（免鉴权，限 60 次/小时）。"""
    out: list[StrategyCase] = []
    for query in spec.queries:
        url = "https://api.github.com/search/repositories?" + urllib.parse.urlencode({
            "q": query, "sort": "updated", "order": "desc",
            "per_page": min(spec.limit, 30)})
        payload = json.loads(_http_get(url, accept="application/vnd.github+json"))
        for item in payload.get("items", [])[:spec.limit]:
            link = item.get("html_url", "")
            if not link:
                continue
            description = (item.get("description") or "").strip()
            topics = item.get("topics") or []
            out.append(StrategyCase(
                id=case_id(link),
                theme=classify_theme(item.get("name", ""), description,
                                     " ".join(topics)),
                kind=classify_kind(item.get("name", ""), description,
                                    " ".join(topics)),
                title=item.get("full_name", ""),
                summary=(f"{description[:200]}"
                         f"（⭐{item.get('stargazers_count', 0)}）"
                         if description else
                         f"⭐{item.get('stargazers_count', 0)}"),
                source_name=spec.name, source_url=link,
                published_at=(item.get("pushed_at") or
                              item.get("created_at") or "")[:10],
                tags=[*spec.tags, *topics[:4]],
                raw={"stars": item.get("stargazers_count"),
                     "language": item.get("language")}))
    return out


def _fetch_arxiv(spec: SourceSpec) -> list[StrategyCase]:
    """arXiv q-fin（Atom XML）。学术源的价值：方法可查证、写明样本区间。"""
    out: list[StrategyCase] = []
    for query in spec.queries:
        url = "http://export.arxiv.org/api/query?" + urllib.parse.urlencode({
            "search_query": query, "sortBy": "submittedDate",
            "sortOrder": "descending", "max_results": spec.limit})
        text = _http_get(url).decode("utf-8", "replace")
        entries = re.findall(r"<entry>(.*?)</entry>", text, re.S)
        for entry in entries:
            title = _first(entry, "title")
            summary = _first(entry, "summary")
            link = ""
            match = re.search(r'<link[^>]*href="([^"]+)"', entry)
            if match:
                link = match.group(1)
            published = _first(entry, "published")[:10]
            if not link or not title:
                continue
            out.append(StrategyCase(
                id=case_id(link),
                theme=classify_theme(title, summary),
                kind=classify_kind(title, summary),
                title=" ".join(title.split())[:180],
                summary=" ".join(summary.split())[:280],
                source_name=spec.name, source_url=link,
                published_at=published, tags=list(spec.tags),
                raw={"authors": re.findall(r"<name>(.*?)</name>", entry)[:5]}))
    return out


def _fetch_xueqiu(spec: SourceSpec) -> list[StrategyCase]:
    """雪球搜索接口（实测返回结构化 JSON）。"""
    out: list[StrategyCase] = []
    for query in spec.queries:
        url = ("https://xueqiu.com/query/v1/search/status.json?"
               + urllib.parse.urlencode({"q": query, "count": spec.limit,
                                         "page": 1}))
        payload = json.loads(_http_get(url))
        for item in (payload.get("list") or [])[:spec.limit]:
            status = item.get("status") or item
            text = (status.get("text") or status.get("description") or "")
            clean = re.sub(r"<[^>]+>", "", text).strip()
            identifier = status.get("id") or case_id(clean)
            link = f"https://xueqiu.com/{status.get('user_id', '')}/{identifier}"
            if not clean:
                continue
            out.append(StrategyCase(
                id=case_id(link if link else clean),
                theme=classify_theme(clean),
                kind=classify_kind(clean),
                title=" ".join(clean.split())[:120],
                summary=" ".join(clean.split())[:280],
                source_name=spec.name, source_url=link,
                published_at=_timestamp(status.get("created_at")),
                tags=list(spec.tags), raw={}))
    return out


def _fetch_csdn(spec: SourceSpec) -> list[StrategyCase]:
    """CSDN 搜索接口（实测返回结构化 JSON）。"""
    out: list[StrategyCase] = []
    for query in spec.queries:
        url = ("https://so.csdn.net/api/v3/search?"
               + urllib.parse.urlencode({"q": query, "t": "blog",
                                         "p": 1, "s": 0, "tm": 0,
                                         "size": spec.limit}))
        payload = json.loads(_http_get(url))
        for item in (payload.get("result_vos") or [])[:spec.limit]:
            link = item.get("url") or item.get("url_location") or ""
            title = re.sub(r"<[^>]+>", "", item.get("title") or "").strip()
            if not link or not title:
                continue
            out.append(StrategyCase(
                id=case_id(link), theme=classify_theme(title),
                kind=classify_kind(title, item.get("description") or ""),
                title=" ".join(title.split())[:150],
                summary=" ".join((item.get("description") or "").split())[:280],
                source_name=spec.name, source_url=link,
                published_at=_timestamp(item.get("create_time")),
                tags=list(spec.tags),
                raw={"author": item.get("author"),
                     "digg": item.get("digg_count")}))
    return out


def _fetch_rss(spec: SourceSpec) -> list[StrategyCase]:
    """通用 RSS/Atom 源（用户可在 configs/strategy_sources.yaml 里自行添加）。"""
    out: list[StrategyCase] = []
    for endpoint in (spec.endpoint, *spec.queries):
        if not endpoint:
            continue
        text = _http_get(endpoint).decode("utf-8", "replace")
        items = re.findall(r"<item>(.*?)</item>", text, re.S) or \
            re.findall(r"<entry>(.*?)</entry>", text, re.S)
        for item in items[:spec.limit]:
            title = _first(item, "title")
            link = _first(item, "link")
            if not link:
                match = re.search(r'<link[^>]*href="([^"]+)"', item)
                link = match.group(1) if match else ""
            summary = _first(item, "description") or _first(item, "summary")
            date = (_first(item, "pubDate") or _first(item, "published"))[:16]
            if not link or not title:
                continue
            clean_summary = re.sub(r"<[^>]+>", "", summary)
            out.append(StrategyCase(
                id=case_id(link), theme=classify_theme(title, clean_summary),
                kind=classify_kind(title, clean_summary),
                title=" ".join(title.split())[:180],
                summary=" ".join(clean_summary.split())[:280],
                source_name=spec.name, source_url=link, published_at=date,
                tags=list(spec.tags), raw={}))
    return out


_ADAPTERS = {"github": _fetch_github, "arxiv": _fetch_arxiv,
             "xueqiu": _fetch_xueqiu, "csdn": _fetch_csdn, "rss": _fetch_rss}


def _first(block: str, tag: str) -> str:
    match = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", block, re.S)
    return " ".join(match.group(1).split()) if match else ""


def _timestamp(value: Any) -> str:
    """毫秒/秒时间戳 → YYYY-MM-DD。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number > 1e11:      # 毫秒
        number /= 1000.0
    return time.strftime("%Y-%m-%d", time.localtime(number))


# ==================================================================
# 默认源清单
# ==================================================================


DEFAULT_SOURCES: tuple[SourceSpec, ...] = (
    SourceSpec(
        name="GitHub", kind="github", limit=8,
        queries=("quant backtest strategy stars:>100",
                 "量化 回测 策略 stars:>100",
                 "factor investing backtest stars:>50",
                 "A股 因子 选股 stars:>30"),
        tags=("开源实现",)),
    SourceSpec(
        name="arXiv q-fin", kind="arxiv", limit=8,
        queries=("cat:q-fin.PM AND abs:backtest",
                 "cat:q-fin.ST AND abs:factor",
                 "cat:q-fin.TR AND abs:momentum"),
        tags=("学术",)),
    SourceSpec(
        name="雪球", kind="xueqiu", enabled=False, limit=8,
        queries=("回测 策略", "因子 选股", "量化 实盘"),
        tags=("社区分享",),
        disabled_reason="搜索接口需 JS 通过风控（实测返回 WAF 挑战页 "
                        "_waf_bd8ce2ce37），要抓必须引入无头浏览器，"
                        "代价与收益不成比例"),
    SourceSpec(
        name="CSDN", kind="csdn", limit=8,
        queries=("量化回测策略", "多因子选股", "回测 未来函数"),
        tags=("技术博客",)),
)


def load_sources(path: str | Path = "configs/strategy_sources.yaml") -> list[SourceSpec]:
    """从 YAML 读源清单；文件不存在或解析失败时回退到内置清单。"""
    target = Path(path)
    if not target.exists():
        return list(DEFAULT_SOURCES)
    try:
        import yaml

        payload = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 配置坏了不该让抓取整体失效
        logger.warning("源清单解析失败(%s)，改用内置清单", brief(exc, BRIEF_TIGHT))
        return list(DEFAULT_SOURCES)
    specs: list[SourceSpec] = []
    for item in payload.get("sources", []):
        if not isinstance(item, dict) or not item.get("name"):
            continue
        specs.append(SourceSpec(
            name=str(item["name"]), kind=str(item.get("kind", "rss")),
            enabled=bool(item.get("enabled", True)),
            queries=tuple(item.get("queries") or ()),
            endpoint=str(item.get("endpoint", "")),
            limit=int(item.get("limit", 8)),
            tags=tuple(item.get("tags") or ())))
    return specs or list(DEFAULT_SOURCES)


def crawl(sources: list[SourceSpec] | None = None, *,
          progress: Any = None) -> tuple[list[StrategyCase], list[dict[str, Any]]]:
    """抓取全部启用的源，返回 (案例, 每个源的执行结果)。

    单个源失败**不影响**其它源：网络抖动、限流、页面改版都会发生，
    一轮里挂掉一个源就把整批丢掉是不合理的。
    """
    from src.quant.warehouse import QuantWarehouse, WarehouseConfig

    results: list[dict[str, Any]] = []
    cases: list[StrategyCase] = []
    for spec in (sources if sources is not None else load_sources()):
        if not spec.enabled:
            # **禁用也要出现在报告里**：源悄悄消失会让人以为"抓取覆盖了全网"，
            # 而实际上少了一半。诚实的做法是把"跳过了谁、为什么"一起返回。
            results.append({"source": spec.name, "ok": False, "skipped": True,
                            "count": 0,
                            "error": spec.disabled_reason or "已禁用"})
            continue
        adapter = _ADAPTERS.get(spec.kind)
        if adapter is None:
            results.append({"source": spec.name, "ok": False,
                            "error": f"未知源类型 {spec.kind}"})
            continue
        started = time.perf_counter()
        try:
            items = adapter(spec)
        except Exception as exc:  # noqa: BLE001 单源失败不影响其它源
            results.append({"source": spec.name, "ok": False,
                            "error": f"{type(exc).__name__}: {brief(exc, BRIEF_TIGHT)}"})
            if progress:
                progress(f"{spec.name} 失败：{brief(exc, BRIEF_TIGHT)}")
            continue
        elapsed = round(time.perf_counter() - started, 1)
        results.append({"source": spec.name, "ok": bool(items),
                        "count": len(items), "seconds": elapsed,
                        "error": "" if items else
                        "HTTP 成功但没有解析出条目（页面可能是 JS 渲染，"
                        "不能算抓到了内容）"})
        cases.extend(items)
        if progress:
            progress(f"{spec.name}: {len(items)} 条（{elapsed}s）")
    _ = QuantWarehouse(WarehouseConfig.from_env())      # 提前暴露配置问题
    return cases, results
