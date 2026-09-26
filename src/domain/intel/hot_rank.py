"""热议榜：各金融平台的**人气/热搜**排名（免费源，多源主备）。

> 用户口径（2026-09-25）："舆情热度关注的是各大金融平台 贴吧里的热议事件、
> 新闻热搜事件，对应的行业、股票，其利好利多分析。"
> 以及："把几大平台（雪球/东方财富股吧/同花顺/财联社/百度人气榜/韭研公社）
> 的热点事件和个股，聚合汇总显示在情报流或事件告警里。"

所以这一页的输入不是"我抓到的研报"，而是**公开平台上正在被热议什么**。
本模块负责把那份排名取回来。

## 平台覆盖现状（2026-09-25 逐个实测，**不接必然失败的源**）

| 平台 | 接口 | 实测 |
|---|---|---|
| 东方财富股吧 | `stock_comment_em` 千股千评（关注指数） | ✅ 5199 只，含中文名 |
| 百度股市通 | `stock_hot_search_baidu` | ✅ 12 条 |
| 东方财富 | `stock_hot_up_em` 人气飙升榜 | ⚠️ 偶发 `RemoteDisconnected` |
| 东方财富 | `stock_hot_rank_em` 人气榜 | ⚠️ 偶发 `RemoteDisconnected` |
| 财联社 | `stock_info_global_cls` | ✅ 走 `intel_sources`（快讯，非榜） |
| 同花顺 | `stock_info_global_ths` | ✅ 走 `intel_sources`（快讯，非榜） |
| **雪球** | `stock_hot_tweet_xq` / `_follow_xq` / `_deal_xq` | ❌ 三个全挂（`KeyError`/非 JSON）|
| **韭研公社** | 无免费接口 | ❌ 不接 |

**雪球与韭研公社拿不到，如实登记**，不接一个永远失败的源 ——
本项目在 QMT 那条链上已经吃过"留一个必然失败的源"的教训。

## 为什么主源换成了千股千评（原来是东财人气榜）

原主源（人气飙升榜）的问题是**当下就取不到**：实测两个东财人气接口
会同时 `RemoteDisconnected`，重试 3 次仍失败。而千股千评：

  * 5199 只**全覆盖**，直接给中文名（不用再拿代码去查表）；
  * 关注指数 + 目前排名 + 综合得分 + 机构参与度，一次拿全；
  * 实测**未出现过连接失败**。

它唯一的代价是 T-1（`交易日` 是上一交易日），这对"当前在热议什么"
完全够用 —— 热度榜看的是趋势，不是秒级。

## 为什么"飙升榜"值得留在备源里

人气榜前几名长期被同一批票占据（大盘股、老热点），**变化**才含信息。
`排名较昨日变动` 直接给了这个变化量。所以只要它活着就用它。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

logger = logging.getLogger(__name__)

#: 回带条数上限（热榜是"看当下"，给太多等于没筛）
MAX_ROWS: Final = 20

#: 落盘位置。与 `hot_job` 同一目录、同一套约定：
#: **定时任务写、接口只读**（见下面 `load` 的说明）。
_STORE_DIR: Final = Path("data") / "intel"
_STORE_FILE: Final = "hot_rank.json"

#: 进程内缓存（键固定 `"latest"`）。热榜是**当前快照**，不做历史序列。
_CACHE: dict[str, Any] = {}


def store_path(root: Path | None = None) -> Path:
    return (root or Path.cwd()) / _STORE_DIR / _STORE_FILE


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def save(data: dict[str, Any], *, root: Path | None = None) -> None:
    """原子落盘（先写 `.tmp` 再 replace），并把进程内缓存一起刷掉。

    ⚠️ 写失败**不抛给调用方**：这份文件是**缓存**，不是业务数据。
    只读文件系统 / 磁盘满时应该退化成"这次现抓、下次再抓"，
    而不是让情报流整个 500。
    """
    try:
        p = store_path(root)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(p)
    except OSError as exc:
        logger.warning("热榜落盘失败（本次仍照常返回，下次再试）：%s",
                       type(exc).__name__)
    _CACHE["latest"] = data


def load(*, root: Path | None = None, force: bool = False) -> dict[str, Any]:
    """读最近一次落盘的热榜。读不到返回空 dict（页面照常渲染，只是没内容）。"""
    if _CACHE.get("latest") is not None and not force:
        return _CACHE["latest"]  # type: ignore[return-value]
    p = store_path(root)
    data: dict[str, Any] = {}
    if p.exists():
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(obj, dict):
                data = obj
        except (OSError, ValueError):
            logger.warning("热榜结果读取失败（按空处理）")
    _CACHE["latest"] = data
    return data


def age_seconds(data: dict[str, Any], *, key: str = "at") -> float | None:
    """某个时间戳距今多少秒。**解析不出来返回 `None`**（调用方按"当作过期"处理）。

    两个键各有用途，**不要混用**：

        at        最近一次**成功**抓到数据的时间（数据本身多旧）
        tried_at  最近一次**尝试**的时间（含失败）

    重试节奏看 `tried_at`、展示看 `at`。只看 `at` 的话，主源宕机期间
    每个请求都会重试一次（又变回 3 秒／次）；只看 `tried_at` 又会把
    "10 分钟前抓到、但上游已经 3 天没更新"的旧榜当成新的。
    """
    raw = str(data.get(key) or "")
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)
            ).total_seconds()


async def refresh(*, root: Path | None = None) -> dict[str, Any]:
    """抓一次热榜；**成功才覆盖数据，失败只记尝试**。

    返回当前生效的那份 dict（`{"at","tried_at","rows","sources","failures"}`）。

    ## 为什么失败时不能把空结果写进去

    主源（东财千股千评）是 T-1、备源偶发 `RemoteDisconnected` —— 抓失败是
    **常态**。如果失败也覆盖，落盘的 `rows` 会变成空，于是下一个读缓存的
    请求拿到空榜 → 页面显示"平台人气榜当前不可用"，而上一份**完全可用**的
    榜单被我们自己删掉了。所以：`at`/`rows`/`sources` 只在拿到行时更新，
    `tried_at`/`failures` 每次都更新。
    """
    res = await fetch_hot_rank()
    previous = load(root=root)
    data = dict(previous)
    data["tried_at"] = _now()
    data["failures"] = dict(res.failures)
    if res.rows:
        data["at"] = _now()
        data["rows"] = [row.to_public() for row in res.rows]
        # 源名与失败原因**只留在本地文件里**（管理员排障用），
        # 不进接口 —— 与 `hot_rank` 原有的"内部字段不出接口"一致。
        data["sources"] = list(res.sources)
    else:
        # 保留上一次的行与 at（数据确实还是那一份），只记下这次没抓到
        data.setdefault("rows", [])
        data.setdefault("sources", [])
        data.setdefault("at", "")
    save(data, root=root)
    return data


@dataclass
class HotRankRow:
    """热榜一行。**只有客观字段**，不含任何倾向判断。

    `platform` 是**可公开**的平台名（东财股吧/百度股市通）。
    用户明确允许暴露公开数据平台："公开数据的地方（AkShare/腾讯/新浪/东财/QMT）
    可以暴露"，只有平台/群身份（知识星球调研）要藏。
    """

    name: str
    code: str = ""
    rank: int = 0
    #: 排名较昨日变动（正数 = 排名上升 = 关注度升高）。缺失为 0
    rank_change: int = 0
    pct_change: float | None = None
    #: 综合热度（百度热搜才有）
    heat: float | None = None
    #: 关注指数（千股千评，0-100）
    focus: float | None = None
    #: 综合得分（千股千评）
    score: float | None = None
    #: 机构参与度（千股千评，0-1）
    inst: float | None = None
    #: 来源平台（**可公开**）
    platform: str = ""

    def to_public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "code": self.code,
            "rank": self.rank,
            "rank_change": self.rank_change,
            "pct_change": self.pct_change,
            "heat": self.heat,
            "focus": self.focus,
            "score": self.score,
            "inst": self.inst,
            "platform": self.platform,
        }


@dataclass
class HotRankResult:
    rows: list[HotRankRow] = field(default_factory=list)
    #: 实际用到的源名（**内部**，用于管理员排障；不出接口）
    sources: list[str] = field(default_factory=list)
    #: 失败原因（内部）
    failures: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.rows)


def _f(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # NaN → None


def _clean_code(raw: Any) -> str:
    """`SZ000592` / `SH601091` → `000592`（去掉市场前缀）。"""
    s = str(raw or "").strip().upper()
    for p in ("SH", "SZ", "BJ"):
        if s.startswith(p):
            return s[len(p):]
    return s


def _fetch_focus_em() -> list[HotRankRow]:
    """东方财富股吧 —— 千股千评的**关注指数**榜（主源）。

    为什么它是主源：5199 只全覆盖、**直接给中文名**（用户投诉过
    "被提及的标的都是 6 位编码，需要映射成中文股票名"）、字段最全、
    实测最稳。代价是 `交易日` 是 T-1。

    `关注指数` 有大量并列（实测前几名都是 95.6 / 95.2），所以排序
    必须带第二键 —— 否则每次调用顺序都可能不同，前端看起来像在抖。
    """
    import akshare as ak

    df = ak.stock_comment_em()
    out: list[HotRankRow] = []
    for _, r in df.iterrows():
        name = str(r.get("名称") or "").strip()
        if not name:
            continue
        code = _clean_code(r.get("代码"))
        # `万  科Ａ` 这类名字里有连续空格（东财原始数据如此），清掉
        name = re.sub(r"\s+", "", name)
        out.append(HotRankRow(
            name=name,
            code=code,
            rank=int(_f(r.get("目前排名")) or 0),
            pct_change=_f(r.get("涨跌幅")),
            focus=_f(r.get("关注指数")),
            score=_f(r.get("综合得分")),
            inst=_f(r.get("机构参与度")),
            platform="东方财富股吧",
        ))
    out.sort(key=lambda x: (-(x.focus or 0), -(x.score or 0), x.code))
    return out[:MAX_ROWS]


def _fetch_up_em() -> list[HotRankRow]:
    """东财**人气飙升榜**（含排名变动，信息量最大 —— 但当前不稳定）。"""
    import akshare as ak

    df = ak.stock_hot_up_em()
    out: list[HotRankRow] = []
    for _, r in df.iterrows():
        name = str(r.get("股票名称") or "").strip()
        if not name:
            continue
        out.append(HotRankRow(
            name=name,
            code=_clean_code(r.get("代码")),
            rank=int(_f(r.get("当前排名")) or 0),
            rank_change=int(_f(r.get("排名较昨日变动")) or 0),
            pct_change=_f(r.get("涨跌幅")),
            platform="东方财富",
        ))
    # 变动大的排前面（"飙升"才是信号）
    out.sort(key=lambda x: -x.rank_change)
    return out[:MAX_ROWS]


def _fetch_rank_em() -> list[HotRankRow]:
    """东财人气榜（只给当前排名）。"""
    import akshare as ak

    df = ak.stock_hot_rank_em()
    out: list[HotRankRow] = []
    for _, r in df.iterrows():
        name = str(r.get("股票名称") or "").strip()
        if not name:
            continue
        out.append(HotRankRow(
            name=name,
            code=_clean_code(r.get("代码")),
            rank=int(_f(r.get("当前排名")) or 0),
            pct_change=_f(r.get("涨跌幅")),
            platform="东方财富",
        ))
    out.sort(key=lambda x: x.rank)
    return out[:MAX_ROWS]


def _fetch_baidu() -> list[HotRankRow]:
    """百度 A 股热搜（第三源：口径不同，能给"综合热度"）。

    ⚠️ 需要传日期，而日期格式是 `YYYYMMDD`；传错会返回空表而不是报错，
    所以调用方**不能**把空表当成"今天没热搜"。

    ⚠️ 它的名称列是 `名称/代码` **合在一格**（实测形如
    `"贵州茅台/600519"`，也有只给代码的行）。必须切开 —— 直接透传
    会把"名字/代码"当股票名显示，用户看到的是一串编码，
    这正是被投诉的那个问题（"被提及的标的都是 6 位编码"）。
    """
    from datetime import datetime

    import akshare as ak

    from src.domain.intel.stock_names import name_of

    df = ak.stock_hot_search_baidu(symbol="A股",
                                   date=datetime.now().strftime("%Y%m%d"))
    out: list[HotRankRow] = []
    for _, r in df.iterrows():
        raw = str(r.get("名称/代码") or "").strip()
        if not raw:
            continue
        name, code = _split_name_code(raw)
        if not name:
            # 只有代码时用本地名录翻中文名；仍翻不出就只留代码
            code = code or raw
            name = name_of(code) or code
        pct = _f(str(r.get("涨跌幅") or "").replace("%", "").replace("+", ""))
        out.append(HotRankRow(
            name=name,
            code=code,
            heat=_f(r.get("综合热度")),
            pct_change=pct,
            platform="百度股市通",
        ))
    return out[:MAX_ROWS]


def _split_name_code(raw: str) -> tuple[str, str]:
    """`"贵州茅台/600519"` / `"600519"` / `"贵州茅台"` → `(名称, 代码)`。

    分隔符实测用 `/`，但上游改过口径，所以 `/`、`|`、空格、括号都认。
    """
    s = raw.strip()
    m = re.match(r"^(.*?)\s*[/|｜]\s*(\d{6})\s*$", s)
    if m and m.group(1).strip():
        return m.group(1).strip(), m.group(2)
    m = re.match(r"^(.*?)[（(]\s*(\d{6})\s*[)）]\s*$", s)
    if m and m.group(1).strip():
        return m.group(1).strip(), m.group(2)
    if re.fullmatch(r"\d{6}", s):
        return "", s
    return s, ""


#: 源链：**主备互用**，前一个成功即返回（与项目既有 source_chain 同思路）。
#:
#: ⚠️ 顺序是有意的：千股千评放第一是因为它**当下真的能取到**；
#: 东财人气两兄弟放后面是因为此刻两个接口会同时 `RemoteDisconnected`，
#: 但它们带"排名较昨日变动"这个别处没有的信号，一旦恢复就该被用到。
_SOURCES: Final[tuple[tuple[str, Any], ...]] = (
    ("hot_focus_em", _fetch_focus_em),
    ("hot_up_em", _fetch_up_em),
    ("hot_rank_em", _fetch_rank_em),
    ("hot_baidu", _fetch_baidu),
)


#: 单源重试次数。
#:
#: ⚠️ 实测这些免费接口**会偶发 `RemoteDisconnected`**（对端直接关连接）：
#: 同一个调用前一次成功、后一次失败。不重试就会无谓降级到备源，
#: 而备源的信息量更少（人气榜没有"排名变动"）。
_RETRY: Final = 2


async def _fetch_with_retry(fn: Any) -> list[HotRankRow]:
    """调一个源，失败重试 `_RETRY` 次。**空表也当失败**（见调用方说明）。"""
    last: Exception | None = None
    for attempt in range(_RETRY + 1):
        try:
            return await asyncio.to_thread(fn)
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < _RETRY:
                await asyncio.sleep(0.6 * (attempt + 1))
    if last is not None:
        raise last
    return []


async def fetch_hot_rank() -> HotRankResult:
    """取热议榜。**主源（含重试）成功即返回**；全失败返回空。

    调用方据此显示"热榜暂无数据"缺口，而不是假装今天没人讨论。
    """
    res = HotRankResult()
    for name, fn in _SOURCES:
        try:
            rows = await _fetch_with_retry(fn)
        except Exception as exc:  # noqa: BLE001 单源失败不影响其它源
            from src.core.redaction import sanitize_error

            res.failures[name] = sanitize_error(exc)
            logger.info("热榜源 %s 失败：%s", name, res.failures[name])
            continue
        if not rows:
            # ⚠️ "成功但为空"**不算成功**（本项目既有纪律：空表多半是
            # 参数/口径变了，而不是"今天真的没人讨论"）
            res.failures[name] = "返回空表"
            continue
        res.rows = rows
        res.sources = [name]
        return res
    return res


__all__ = ["MAX_ROWS", "HotRankResult", "HotRankRow", "age_seconds",
           "fetch_hot_rank", "load", "refresh", "save", "store_path"]
