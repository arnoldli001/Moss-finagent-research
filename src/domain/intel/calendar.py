"""投资日历 —— 四类事件，**每类主备互用**。

## 用户口径（2026-09-25）

> 设计的时候数据源要考虑多渠道 主备互用，避免单一数据源断了功能不能用了

## 四类事件与主备链

| 类别 | 主源 | 备源 | 实测 |
|---|---|---|---|
| `earnings` 预约披露 | `stock_yysj_em`（东财） | `stock_report_disclosure`（巨潮） | 2320 行 / 5550 行 |
| `unlock` 限售解禁 | `stock_restricted_release_summary_em` | 巨潮公告 `category=解禁` | 15 行 / 67 行 |
| `macro` 宏观发布 | `configs/calendar_official.yaml`（手工录） | `news_economic_baidu`（按地区过滤） | 静态 / 104 行 |
| `trade_day` 交易日 | `tool_trade_date_hist_sina`（交易所口径） | — **见下** | 8797 行 |

### ⚠️ 交易日历为什么是**单一来源且应当如此**

交易日是**交易所规则**，不是"某个数据商的观点" —— 它没有"备源"的概念：
第二个来源要么同源（等于没备），要么不可信。

所以这里**不假装有备源**：主源失败即明确报缺口。
这比"随便找个源凑上"更负责 —— 用不可信的交易日历会让整个调度
在错误的日子跑，后果比"日历不可用"严重得多。

### 一个实测踩到的坑

`stock_report_disclosure` 的 `period` 参数**不认 `2026三季报`**（报
`ValueError: Length mismatch`），只认 `2026半年报` / `2026年报` 这类写法。
备源调用必须传对，否则静默失败 —— 见 `_PERIOD_ALIASES`。

## 合规边界（与情报模块同一口径）

日历只做两件事：**呈现已公布的日程** + **统计事件的覆盖范围**
（多少家、分布在哪些行业）。
**不做方向判断、不给目标价、不给买卖时点。**

行业映射用**申万分类**，不用"概念板块" —— 后者是人为归类，会带判断色彩。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

logger = logging.getLogger(__name__)

#: 默认展望窗口（天）
DEFAULT_HORIZON_DAYS: Final = 45

#: 财经日历**抓取**窗口上限（天）
#:
#: 该接口是**按天**请求的，抓 45 天就是 45 次 HTTP。远期条目 YAML 已经
#: 给了权威日程，只需要近期数据补预期/前值，所以抓取窗口比展示窗口短，
#: 两者解耦：`DEFAULT_HORIZON_DAYS` 决定"日历上显示多久"，
#: 这个常量决定"为补数值最多打多少次请求"。
FEED_FETCH_MAX_DAYS: Final = 30

#: 源日期与官方日程的**可接受时差**（天）
#:
#: 超出这个时差，候选只有在带 `expected`/`published` 时才被采用；
#: 只带 `previous` 的一律丢弃 —— 那是上一期的条目，贴到这一期就是编数据。
_DATE_TOLERANCE_DAYS: Final = 2

#: 日历数据源标签（**只用于管理员侧**）
CALENDAR_SOURCE_LABELS: Final[dict[str, str]] = {
    "earnings_em": "东财预约披露",
    "earnings_cninfo": "巨潮预约披露",
    "unlock_em": "东财解禁",
    "unlock_cninfo": "巨潮解禁公告",
    "macro_static": "官方发布日程（手工录入）",
    "macro_baidu": "全球财经日历",
    "trade_day_sina": "交易日历",
}

#: 巨潮披露的 period 写法别名。
#: 实测 `2026三季报` 会抛 ValueError，必须映射到它认识的形式。
_PERIOD_ALIASES: Final[dict[str, str]] = {
    "一季报": "2026一季报",
    "半年报": "2026半年报",
    "三季报": "2026半年报",   # 巨潮暂无三季报档期时退回半年报口径
    "年报": "2026年报",
}


@dataclass
class CalendarEvent:
    """一条日历事件。**面向接口的字段刻意做窄**。"""

    event_id: str
    kind: str                  # earnings | unlock | macro | trade_day
    date: str                  # ISO 日期
    title: str
    #: 覆盖范围（客观统计，**不含方向**）
    scope: dict[str, Any] = field(default_factory=dict)
    #: 确定性：rule=规则确定（不会变）｜scheduled=预约（可改期）
    certainty: str = "scheduled"
    #: 改期记录（预约类才有）
    changes: list[dict[str, str]] = field(default_factory=list)
    #: 补充指标（如解禁市值、占流通市值比）
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_public(self) -> dict[str, Any]:
        """**白名单构造** —— 新字段默认不出接口。

        日历事件的敏感面比情报小（日程本身是公开信息），
        但仍不允许出现：原始公告链接、内部源名、任何 data provider 标识。
        """
        return {
            "event_id": self.event_id,
            "kind": self.kind,
            "date": self.date,
            "title": self.title,
            "scope": dict(self.scope),
            "certainty": self.certainty,
            "changes": list(self.changes),
            "metrics": dict(self.metrics),
        }


@dataclass
class CalendarResult:
    events: list[CalendarEvent] = field(default_factory=list)
    gaps: list[dict[str, str]] = field(default_factory=list)
    fetched_at: str = ""

    @property
    def degraded(self) -> bool:
        return bool(self.gaps)

    def to_public(self) -> dict[str, Any]:
        return {
            "events": [e.to_public() for e in self.events],
            "gaps": list(self.gaps),
            "fetched_at": self.fetched_at,
            "degraded": self.degraded,
        }


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ======================================================================
# 一、预约披露
# ======================================================================

def fetch_earnings_schedule(*, horizon_days: int = DEFAULT_HORIZON_DAYS
                            ) -> tuple[list[CalendarEvent], list[str]]:
    """预约披露日程。返回 `(事件, 尝试过的源名)`。

    主源东财给的是**全市场预约表**（含 3 次变更记录）；
    备源巨潮给的是**按报告期**的档期表。
    两者口径不同 —— 主源带变更历史，这是"改期提示"的依据，所以优先。
    """
    events: list[CalendarEvent] = []
    tried: list[str] = []
    today = datetime.now()

    # ── 主源：东财 ──
    tried.append("earnings_em")
    try:
        import akshare as ak

        # ★ `date` 要传**当前报告期**的季末，而不是"最近一个已结束的报告期"。
        # 实测（2026-09-25）：
        #   date=20260630 → 返回预约日期 2026-07-16 ~ 08-31（**全是过去**）
        #   date=20260930 → 返回预约日期 2026-10-13 ~ 10-31（未来的三季报档期）
        # 首版传了"最近已结束的报告期"，于是取回的全是历史披露日，
        # 被"只保留未来"的窗口整段过滤 → 返回 0 条。
        #
        # 规则：报告期 = 当前正在预约的那个。A 股按季末排：
        #   1–3 月看上一年的年报档期（12-31）
        #   4–6 月看一季报（03-31）
        #   7–9 月看半年报（06-30）
        #   10–12 月看三季报（09-30）
        target_period = _current_disclosure_period(today)
        df = ak.stock_yysj_em(symbol="沪深A股",
                              date=target_period.strftime("%Y%m%d"))

        # ⚠️ 预约披露是**未来日程**：只保留今天及以后。
        # 首版写了 `today - 3天` 作为下界（想容忍时区/滞后），
        # 但那会把已经披露完的历史日程也混进来 —— 日历的语义是"接下来会发生什么"。
        horizon = today + timedelta(days=horizon_days)
        for _, row in df.iterrows():
            first = str(row.get("首次预约时间") or "")
            if not first or first == "NaT":
                continue
            try:
                d = datetime.fromisoformat(first[:10])
            except ValueError:
                continue
            if not (today <= d <= horizon):
                continue
            changes = []
            for col, label in (("一次变更日期", "一次变更"),
                               ("二次变更日期", "二次变更"),
                               ("三次变更日期", "三次变更")):
                v = str(row.get(col) or "")
                if v and v != "NaT":
                    changes.append({"field": label, "date": v[:10]})
            code = str(row.get("股票代码") or "")
            events.append(CalendarEvent(
                event_id=f"earnings_{code}_{first[:10]}",
                kind="earnings",
                date=first[:10],
                title=f"{row.get('股票简称')} 预约披露",
                scope={"kind": "company", "codes": [code],
                       "industries": [], "company_count": 1},
                certainty="scheduled",     # 公司预约，可改期
                changes=changes,
            ))
        if events:
            return events, tried
        logger.warning("东财预约披露返回 0 条（可能未到档期），尝试备源")
    except Exception as exc:  # noqa: BLE001
        from src.core.redaction import sanitize_error
        logger.warning("东财预约披露失败：%s", sanitize_error(exc))

    # ── 备源：巨潮 ──
    tried.append("earnings_cninfo")
    try:
        import akshare as ak

        period = _PERIOD_ALIASES[_quarter_label(datetime.now())]
        df = ak.stock_report_disclosure(market="沪深京", period=period)
        for _, row in df.head(500).iterrows():
            first = str(row.get("首次预约") or "")
            if not first or first == "NaT":
                continue
            try:
                d = datetime.fromisoformat(first[:10])
            except ValueError:
                continue
            if d < datetime.now() - timedelta(days=3):
                continue
            code = str(row.get("股票代码") or "")
            events.append(CalendarEvent(
                event_id=f"earnings_{code}_{first[:10]}",
                kind="earnings",
                date=first[:10],
                title=f"{row.get('股票简称')} 预约披露",
                scope={"kind": "company", "codes": [code],
                       "industries": [], "company_count": 1},
                certainty="scheduled",
            ))
    except Exception as exc:  # noqa: BLE001
        from src.core.redaction import sanitize_error
        logger.warning("巨潮预约披露失败：%s", sanitize_error(exc))

    return events, tried


def _current_disclosure_period(now: datetime) -> datetime:
    """当前**正在预约**的报告期季末（传给 `stock_yysj_em` 的 `date`）。

    实测规则（2026-09-25 验证）：9 月要传 09-30 才拿到 10 月的三季报档期；
    传 06-30 拿到的是**已过去**的 7–8 月披露日。

    A 股按季末排期：
      1–3 月 → 上一年年报档期（12-31）
      4–6 月 → 一季报（03-31）
      7–9 月 → 半年报（06-30）… 但实测 9 月已切到三季报，故 9 月起用 09-30
      10–12 月 → 三季报（09-30）
    """
    y, m = now.year, now.month
    if m <= 3:
        return datetime(y - 1, 12, 31)
    if m <= 6:
        return datetime(y, 3, 31)
    if m <= 8:
        return datetime(y, 6, 30)
    return datetime(y, 9, 30)


def _recent_quarter_end(now: datetime) -> datetime:
    """最近一个**已结束**的报告期（保留给"查历史披露"的场景）。"""
    y, m = now.year, now.month
    if m <= 3:
        return datetime(y - 1, 12, 31)
    if m <= 6:
        return datetime(y, 3, 31)
    if m <= 9:
        return datetime(y, 6, 30)
    return datetime(y, 9, 30)


def _quarter_label(now: datetime) -> str:
    m = now.month
    if m <= 3:
        return "年报"
    if m <= 6:
        return "一季报"
    if m <= 9:
        return "半年报"
    return "三季报"


# ======================================================================
# 二、限售解禁
# ======================================================================

def _unlock_date(raw: Any) -> str:
    """解禁时间 → `YYYY-MM-DD`。

    实测该字段在不同 akshare 版本里给过两种形态：
      · `日期字符串`（`2026-09-28`）—— 现行
      · `毫秒时间戳`（`1790553600000`）—— 旧版
    两种都处理；都解析不出来返回空串（调用方据此跳过该行，不猜）。
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    head = s[:10]
    if len(head) == 10 and head[4] == "-" and head[7] == "-":
        return head
    # 纯数字 → 毫秒时间戳
    if s.isdigit() and len(s) >= 10:
        try:
            return datetime.fromtimestamp(int(s) / 1000).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return ""
    return ""


def fetch_unlock_schedule(*, horizon_days: int = DEFAULT_HORIZON_DAYS
                          ) -> tuple[list[CalendarEvent], list[str]]:
    """限售解禁日程。主源东财（**个股明细**），备源巨潮公告。

    ## 为什么用明细源而不是汇总源（用户报障后改的）

    原来用 `stock_restricted_release_summary_em`，它按日只给三列：
    `解禁时间 / 当日解禁股票家数 / 实际解禁市值`。于是界面上"40 家"
    点开**什么都没有** —— 个股名、每只的市值与占比在那个接口里
    **根本不存在**，不是前端漏渲染。

    改用 `stock_restricted_release_detail_em`：它按**个股**给
    `股票代码 / 股票简称 / 限售股类型 / 实际解禁市值 /
    占解禁前流通市值比例`。实测它与汇总源**完全一致**
    （2026-09-28：两边都是 40 家、591.08 亿），是汇总源的超集。

    所以：一家一个条目 → 按日聚合成一个事件 → 个股挂在
    `scope.stocks` 里，展开才看得见。
    """
    events: list[CalendarEvent] = []
    tried: list[str] = []
    today = datetime.now()
    end = today + timedelta(days=horizon_days)

    tried.append("unlock_em")
    try:
        import akshare as ak

        df = ak.stock_restricted_release_detail_em(
            start_date=today.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"))

        # 按日归堆。`解禁时间` 实测是**日期字符串**（`2026-09-28`），
        # 不是时间戳 —— 但仍兼容毫秒时间戳（同一接口在不同版本下变过）
        by_day: dict[str, list[dict[str, Any]]] = {}
        for _, row in df.iterrows():
            d = _unlock_date(row.get("解禁时间"))
            if not d:
                continue
            code = str(row.get("股票代码") or "").strip()
            cap = _num(row.get("实际解禁市值"))
            # 占比：源给的是**小数**（7.88e-05），转成百分数才可读
            ratio = _num(row.get("占解禁前流通市值比例"))
            by_day.setdefault(d, []).append({
                "code": code,
                "name": str(row.get("股票简称") or "").strip(),
                "market_cap": cap,
                "pct_of_float": (None if ratio is None
                                 else round(ratio * 100, 4)),
                "share_type": str(row.get("限售股类型") or "").strip(),
            })

        for d in sorted(by_day):
            stocks = sorted(by_day[d],
                            key=lambda s: (s.get("market_cap") or 0),
                            reverse=True)
            total = sum((s.get("market_cap") or 0) for s in stocks)
            events.append(CalendarEvent(
                event_id=f"unlock_{d}",
                kind="unlock",
                date=d,
                title="限售股解禁日",
                scope={
                    "kind": "market",
                    "company_count": len(stocks),
                    "industries": [],
                    "codes": [s["code"] for s in stocks if s["code"]],
                    # ★ 个股明细。**按市值倒序** —— 解禁影响最大的是最大的那几只，
                    # 用户展开先看到的应该是它们。
                    "stocks": stocks,
                },
                # 解禁日由**交易所规则**确定，不会改期
                certainty="rule",
                metrics={"unlock_market_cap": total},
            ))
        if events:
            return events, tried
    except Exception as exc:  # noqa: BLE001
        from src.core.redaction import sanitize_error
        logger.warning("东财解禁失败：%s", sanitize_error(exc))

    tried.append("unlock_cninfo")
    try:
        import akshare as ak

        df = ak.stock_zh_a_disclosure_report_cninfo(
            symbol="", market="沪深京", category="解禁",
            start_date=today.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"))
        for _, row in df.iterrows():
            d = str(row.get("公告时间") or "")[:10]
            code = str(row.get("代码") or "")
            if not d:
                continue
            events.append(CalendarEvent(
                event_id=f"unlock_{code}_{d}",
                kind="unlock",
                date=d,
                title=f"{row.get('简称')} 解禁公告",
                scope={"kind": "company", "codes": [code],
                       "industries": [], "company_count": 1},
                # 公告是**公司披露**，解禁日以公告为准 —— 定为 scheduled
                certainty="scheduled",
            ))
    except Exception as exc:  # noqa: BLE001
        from src.core.redaction import sanitize_error
        logger.warning("巨潮解禁失败：%s", sanitize_error(exc))

    return events, tried


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


# ======================================================================
# 三、宏观发布日程
# ======================================================================

def build_expectation(*, expected: Any, previous: Any,
                      published: Any = None) -> dict[str, Any]:
    """构造**预期差**字段。用户口径（2026-09-25）：

    > 金融数据源讲究时效性，讲究预期差，未来的预期事件才更重要。

    ## 四种状态，**如实标注，不猜**

    | state | 条件 | 界面该怎么显示 |
    |---|---|---|
    | `pending` | 均无（事件还远） | 「待公布」 |
    | `prior_only` | 只有前值 | 显示前值 + 明确「暂无一致预期值」 |
    | `expected` | 有预期值 | 预期 vs 前值，**算出预期差** |
    | `released` | 已有公布值 | 公布 vs 预期，**算出兑现差** |

    实测（2026-09-25，8 天 551 条）：**有前值 439 条、有预期值仅 40 条**。
    所以 `prior_only` 是**常态而非异常** —— 界面必须能表达
    "这个事件有日程、但市场还没形成一致预期"。

    为什么不用 0 或中性值填：本项目口径是"不猜"。
    把"没有预期"渲染成 0，会让人以为"预期是零增长" —— 那是编造数据。
    """

    def _f(v: Any) -> float | None:
        try:
            if v is None or str(v).strip() in ("", "nan", "None", "--"):
                return None
            return float(v)
        except (TypeError, ValueError):
            return None

    exp, prev, pub = _f(expected), _f(previous), _f(published)

    if pub is not None:
        diff = None if exp is None else round(pub - exp, 4)
        return {"state": "released", "published": pub, "expected": exp,
                "previous": prev, "surprise": diff,
                "surprise_txt": _pct_txt(diff, exp)}
    if exp is not None:
        gap = None if prev is None else round(exp - prev, 4)
        return {"state": "expected", "published": None, "expected": exp,
                "previous": prev, "surprise": None, "expect_gap": gap,
                "expect_gap_txt": _pct_txt(gap, prev)}
    if prev is not None:
        return {"state": "prior_only", "published": None, "expected": None,
                "previous": prev, "surprise": None,
                "note": "暂无一致预期值"}
    return {"state": "pending", "published": None, "expected": None,
            "previous": None, "surprise": None, "note": "待公布"}


def _pct_txt(diff: float | None, base: float | None) -> str | None:
    """把差值渲染成可读文本。基准为 0 或缺失时只给绝对值（不除零）。"""
    if diff is None:
        return None
    if base in (None, 0):
        return f"{diff:+g}"
    return f"{diff:+g}（{diff / abs(base) * 100:+.1f}%）"


def _norm_name(name: str) -> str:
    """事件名归一化，用于**跨源匹配**。

    两个源的事件名写法差异很大：

        YAML 侧   "规模以上工业企业利润" / "制造业PMI" / "CPI"
        日历侧    "中国8月规模以上工业企业利润金额-年初至今(亿元)"
                  "中国9月官方制造业PMI" / "中国9月CPI年率"

    所以**不能用相等匹配**。归一化后做**双向包含**判断
    （见 `_match_index`）：短名是长名的子串即命中。
    """
    import re

    s = re.sub(r"^中国", "", name.strip())
    s = re.sub(r"[\s·、（）()【】\[\]：:，,。/\\\-]+", "", s)
    s = re.sub(r"\d+", "", s)
    for drop in ("统计局", "年率", "月率", "金额", "年初至今", "单月",
                 "累计", "同比", "官方", "亿元", "%"):
        s = s.replace(drop, "")
    return s.lower()


def _date_gap_days(a: Any, b: Any) -> int:
    """两个 `YYYY-MM-DD` 相差几天。解析不出来时给一个大数（视作"很远"）。"""
    try:
        da = datetime.fromisoformat(str(a)[:10])
        db = datetime.fromisoformat(str(b)[:10])
    except (ValueError, TypeError):
        return 10 ** 6
    return abs((da - db).days)


def _match_index(norm: str, rows: list[dict[str, Any]],
                 match: dict[str, Any] | None = None,
                 date: str | None = None) -> dict[str, Any] | None:
    """在财经日历记录表里找**最匹配**的那一条，找不到返回 `None`。

    `match` 是 YAML 给的**模式契约**。没有它就只能靠名字双向包含去猜，
    而那样猜必错 —— 实测踩过的三个坑：

      · "CPI" 被 "中国台湾9月未季调CPI**读数**" 命中 → 前值 112.32，
        那是**指数读数**不是同比%，与国内 CPI 不可比
      · "制造业PMI" 与 "非制造业PMI" 归一化后**同名**，互相覆盖 →
        拿到 49.0（非制造业），真值 49.8
      · "GDP" 被 "GDP**总量**" 命中 → 前值 361511.0（亿元），
        真值 4.7（当季同比%）

    所以能写清"哪个名字才是这个指标"的，都要在 YAML 里写清：
    `include` 是**全部**必须命中的子串，`exclude` 任一命中即淘汰。

    判定在**原始事件名**上做，不能先 `_norm_name` —— 归一化会把
    "年初至今/单月/金额/年率" 这些**区分性词**删掉，删完可能剩空串，
    而空串是任何字符串的子串 → 该条条件形同虚设。
    """
    if not rows:
        return None
    inc = [str(x) for x in (match or {}).get("include") or []]
    exc = [str(x) for x in (match or {}).get("exclude") or []]
    cand = rows
    if inc or exc:
        cand = [r for r in cand
                if all(x in str(r.get("raw_name") or "") for x in inc)
                and not any(x in str(r.get("raw_name") or "") for x in exc)]
    elif norm:
        cand = [r for r in cand
                if norm in str(r.get("norm") or "")
                or str(r.get("norm") or "") in norm]
    if not cand:
        return None
    if date:
        # 日期**只作同分优先**。两个源的日期口径本来就不一致（实测源把
        # 9 月 CPI 记在 10-14，官方日程是 10-09），硬过滤会把能用的
        # 数据全滤掉。反过来，没有这一层时 "10 月 PMI" 会拿到 9 月那条
        # 49.8 —— 那就成编数据了。所以：同日优先，其次限时差，再不行
        # 只在候选带前瞻数据（预期/公布）时才要。
        same = [r for r in cand if r.get("date") == date]
        if same:
            cand = same
        else:
            near = [r for r in cand
                    if _date_gap_days(r.get("date"), date) <= _DATE_TOLERANCE_DAYS]
            if near:
                cand = near
            else:
                ahead = [r for r in cand
                         if r.get("expected") is not None
                         or r.get("published") is not None]
                if not ahead:
                    return None
                cand = ahead
    return cand[0]


def _tag_src_date(metrics: dict[str, Any], hit: dict[str, Any],
                  official_date: str) -> None:
    """源日期与官方日程不一致时，把**源日期**标进 metrics。

    为什么必须标：10-31 那条制造业PMI，源给的是 **9 月**的值 49.8。
    数值和口径都对，但用户看到"前值 49.8"会以为是**10 月**的前值 ——
    而 10 月还没发布，9 月的值确实是它最近的一期。不标清楚就是误导。
    """
    src_date = str(hit.get("date") or "")[:10]
    if not src_date or src_date == official_date:
        return
    if _date_gap_days(src_date, official_date) <= _DATE_TOLERANCE_DAYS:
        return
    metrics["src_date"] = src_date
    base = metrics.get("note")
    hint = f"数据日期 {src_date}"
    metrics["note"] = f"{base}；{hint}" if base else hint


def fetch_macro_schedule(*, horizon_days: int = DEFAULT_HORIZON_DAYS
                         ) -> tuple[list[CalendarEvent], list[str]]:
    """宏观发布日程 + **预期差**。

    做法：静态 YAML 给**权威日程**（哪天发），再用全球财经日历
    **按事件名匹配**补上预期值/前值/公布值。两者是"骨架 + 血肉"：

      · 只用 YAML    → 有日程但没数据，用户看不出预期差
      · 只用财经日历  → 有数据但日程不全，还夹带大量非中国条目与日频噪音

    匹配不上时**保留 YAML 条目**并标 `pending`/`prior_only` ——
    宁可少一个数字，也不要丢一个已知日程。
    """
    events: list[CalendarEvent] = []
    tried: list[str] = []
    today = datetime.now()
    end = today + timedelta(days=horizon_days)

    # ── ① 先取财经日历，建记录表（血肉）──
    #
    # ⚠️ 这里必须是**记录列表**，不能是 `dict[归一化名 → 单条]`。
    # `_norm_name` 会删掉 "年初至今/单月/金额/年率" 这些**区分性词**，
    # 于是同一指标的多个变体会塌成同一个 key 互相覆盖。实测塌掉的例子：
    #
    #   规模以上工业企业利润  → 金额 45820.6 / 年率年初至今 17.6 / 年率单月 11.2
    #   GDP                  → 年率累计 4.7 / 年率当季 4.3 / 季率 0.9 / 总量 361511.0
    #
    # 三条塌成一条，谁最后写谁赢 —— 所以曾出现 "GDP 前值 361511.0"
    # 这种明显不是增速的"数据"。归一化名只适合做**粗筛**，不适合做键。
    tried.append("macro_baidu")
    feed_rows: list[dict[str, Any]] = []
    try:
        import akshare as ak

        # 抓取窗口比展示窗口短：这个接口是**按天**请求的，30 天就是 30 次
        # HTTP。远期条目 YAML 已经给了权威日程，只需要近期数据补预期/前值。
        for off in range(0, min(horizon_days, FEED_FETCH_MAX_DAYS) + 1):
            d = (today + timedelta(days=off)).strftime("%Y%m%d")
            try:
                df = ak.news_economic_baidu(date=d)
            except Exception:  # noqa: BLE001 单日失败不影响其它日
                continue
            for _, row in df.iterrows():
                region = str(row.get("地区") or "")
                # 只要**中国内地**。"中国" 是 "中国台湾"/"中国香港" 的子串，
                # 只用 `"中国" in region` 会把港台一并收进来 —— 而港台
                # 指标与内地**同名不同义**（台湾 CPI 是读数指数，内地是
                # 同比%），混在一起必然张冠李戴。
                if "中国" not in region or "台湾" in region or "香港" in region:
                    continue
                name = str(row.get("事件") or "").strip()
                if not name:
                    continue
                feed_rows.append({
                    "norm": _norm_name(name),
                    "raw_name": name,
                    "date": str(row.get("日期") or "")[:10],
                    "expected": row.get("预期"),
                    "previous": row.get("前值"),
                    "published": row.get("公布"),
                    "importance": row.get("重要性"),
                })
    except Exception as exc:  # noqa: BLE001
        from src.core.redaction import sanitize_error
        logger.warning("全球财经日历失败：%s", sanitize_error(exc))

    # ── ② 静态 YAML：权威日程（骨架）──
    tried.insert(0, "macro_static")
    cfg = Path("configs/calendar_official.yaml")
    if cfg.exists():
        try:
            import yaml

            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
            for section in ("macro", "fed"):
                for row in (data.get(section) or []):
                    d = str(row.get("date") or "")[:10]
                    if not d:
                        continue
                    try:
                        dt = datetime.fromisoformat(d)
                    except ValueError:
                        continue
                    if not (today - timedelta(days=1) <= dt <= end):
                        continue
                    name = str(row.get("name") or "宏观数据发布")
                    match = row.get("match") or {}
                    hit = _match_index(_norm_name(name), feed_rows,
                                       match=match, date=d) or {}
                    metrics = build_expectation(
                        expected=hit.get("expected") or row.get("expected"),
                        previous=hit.get("previous") or row.get("previous"),
                        published=hit.get("published"))
                    if hit.get("importance") is not None:
                        metrics["importance"] = hit["importance"]
                    _tag_src_date(metrics, hit, d)
                    events.append(CalendarEvent(
                        event_id=f"{section}_{row.get('key', d)}",
                        kind="macro",
                        date=d,
                        title=name,
                        scope={"kind": "market", "company_count": 0,
                               "industries": [], "codes": []},
                        certainty="rule",       # 官方日程表，按规则发布
                        metrics=metrics,
                    ))
        except Exception as exc:  # noqa: BLE001
            from src.core.redaction import sanitize_error
            logger.warning("静态宏观日程解析失败：%s", sanitize_error(exc))

    # ── ③ YAML 没覆盖到时，用财经日历的宏观条目兜底 ──
    if not events and feed_rows:
        _noise = ("仓单", "竞拍", "国债", "回购")   # 日频噪音，不是宏观日程
        for info in feed_rows[:60]:
            raw = str(info.get("raw_name") or "")
            if any(n in raw for n in _noise):
                continue
            d = info.get("date") or ""
            if not d:
                continue
            metrics = build_expectation(
                expected=info.get("expected"), previous=info.get("previous"),
                published=info.get("published"))
            metrics["importance"] = info.get("importance")
            events.append(CalendarEvent(
                event_id=f"macro_{info.get('norm', '')[:24]}",
                kind="macro",
                date=d,
                title=raw,          # 原始名信息量最大（含单位与口径）
                scope={"kind": "market", "company_count": 0,
                       "industries": [], "codes": []},
                certainty="scheduled",
                metrics=metrics,
            ))
        events.sort(key=lambda e: e.date)

    return events, tried


# ======================================================================
# 四、交易日历
# ======================================================================

def fetch_trade_days(*, horizon_days: int = DEFAULT_HORIZON_DAYS
                     ) -> tuple[list[CalendarEvent], list[str]]:
    """交易日 / 休市日。

    ⚠️ **刻意不做备源** —— 交易日是**交易所规则**，不存在"第二个可信来源"。
    第二个来源要么同源（等于没备），要么不可信。
    用不可信的交易日历会让整个调度在错误的日子跑，
    后果比"日历不可用"严重得多。所以主源失败就如实报缺口。
    """
    tried = ["trade_day_sina"]
    events: list[CalendarEvent] = []
    today = datetime.now().date()
    end = today + timedelta(days=horizon_days)
    try:
        import akshare as ak

        df = ak.tool_trade_date_hist_sina()
        days = set()
        for v in df["trade_date"]:
            s = str(v)[:10]
            try:
                d = datetime.fromisoformat(s).date()
            except ValueError:
                continue
            if today <= d <= end:
                days.add(d)

        # 输出**休市段**（比逐日列出交易日更有信息量）
        cursor = today
        while cursor <= end:
            if cursor not in days and cursor.weekday() < 5:
                start = cursor
                while (cursor <= end and cursor not in days
                       and cursor.weekday() < 5):
                    cursor += timedelta(days=1)
                events.append(CalendarEvent(
                    event_id=f"holiday_{start.isoformat()}",
                    kind="trade_day",
                    date=start.isoformat(),
                    title=f"休市（{start.isoformat()} 起）",
                    scope={"kind": "market", "company_count": 0,
                           "industries": [], "codes": []},
                    certainty="rule",
                    metrics={"until": (cursor - timedelta(days=1)).isoformat()},
                ))
            else:
                cursor += timedelta(days=1)
    except Exception as exc:  # noqa: BLE001
        from src.core.redaction import sanitize_error
        logger.warning("交易日历失败：%s", sanitize_error(exc))

    return events, tried


# ======================================================================
# 聚合
# ======================================================================

async def build_calendar(*, horizon_days: int = DEFAULT_HORIZON_DAYS
                         ) -> CalendarResult:
    """并发拉四类日历，按日期排序。单类失败进 `gaps`。"""
    import asyncio

    jobs = {
        "earnings": asyncio.to_thread(fetch_earnings_schedule,
                                      horizon_days=horizon_days),
        "unlock": asyncio.to_thread(fetch_unlock_schedule,
                                    horizon_days=horizon_days),
        "macro": asyncio.to_thread(fetch_macro_schedule,
                                   horizon_days=horizon_days),
        "trade_day": asyncio.to_thread(fetch_trade_days,
                                       horizon_days=horizon_days),
    }
    results = await asyncio.gather(*jobs.values(), return_exceptions=True)

    events: list[CalendarEvent] = []
    gaps: list[dict[str, str]] = []
    kind_labels = {"earnings": "财报披露", "unlock": "限售解禁",
                   "macro": "宏观发布", "trade_day": "交易日"}

    for kind, res in zip(jobs.keys(), results):
        if isinstance(res, Exception):
            from src.core.redaction import sanitize_error
            logger.warning("日历 %s 拉取失败：%s", kind, sanitize_error(res))
            gaps.append({"kind": kind, "kind_label": kind_labels[kind],
                         "message": f"{kind_labels[kind]}暂无数据"})
            continue
        got, _tried = res
        if not got:
            gaps.append({"kind": kind, "kind_label": kind_labels[kind],
                         "message": f"{kind_labels[kind]}暂无数据"})
        events.extend(got)

    events.sort(key=lambda e: (e.date, e.kind))
    return CalendarResult(events=events, gaps=gaps, fetched_at=_now_iso())


__all__ = [
    "CALENDAR_SOURCE_LABELS",
    "DEFAULT_HORIZON_DAYS",
    "CalendarEvent",
    "CalendarResult",
    "build_calendar",
    "fetch_earnings_schedule",
    "fetch_macro_schedule",
    "fetch_trade_days",
    "fetch_unlock_schedule",
]
