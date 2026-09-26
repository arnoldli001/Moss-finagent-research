"""全市场（沪深京三市）成交额预测量能：**同一时刻同比昨日**。

## 为什么不能用「累计 ÷ 已交易时间占比」

项目里 `IndexVolume`（指数量能）用的是时间线性外推：

    全天预测量 = 当前累计量 / 已交易时间占比

这在小样本（开盘半小时）会系统性高估 —— A股日内量能是 **U 型**：早盘一小时
常占全天 25~30%，而它只占交易时长的 12.5%。按时间外推，09:45 就能推出一个
比真实值高两三倍的"全天量"。

## 现在的口径：同一时刻同比（与开盘啦 `MarketCapacity` 同源）

    预测量能 = 昨日全天成交额 × (今日当前累计额 / 昨日**同一时刻**累计额)
    量能差   = 预测量能 - 昨日全天成交额

开盘啦 `MarketCapacity` 返回的 `yclnstr` 形态即此式产物（实测样例
`"23417亿(-3.05%,缩量736亿)"`）；它的 `last / s_zrcs`（今日累计 / 昨日同时刻）
与 `预测 / 昨日全天` 是同一个比例，证实了上式。该接口文档见
    https://raw.githubusercontent.com/xiyuxifeng/Trade/main/trade-strategy-ai/docs/bak/kaipan.md

**为什么这个口径稳**：分子分母取同一天里的同一时刻，U 型分布被完全约掉
（早盘占比在两天里几乎一样）。实测 2026-09-23（沪市，真实数据）：

| 时点 | 预测全天 | 相对昨日 |
|---|---|---|
| 10:30 | 8,060 亿 | 缩量 2,020 亿 |
| 11:30 | 8,137 亿 | 缩量 1,942 亿 |
| 14:00 | 8,159 亿 | 缩量 1,921 亿 |
| 14:50 | 8,274 亿 | 缩量 1,806 亿 |

10:30 到 14:50 预测只漂移约 2% —— 「开盘就能推」这个诉求成立。

## 数据源（腾讯一条独立通道，实测可用）

`https://web.ifzq.gtimg.cn/appstock/app/day/query?code=<symbol>`

一次请求返回**最近 5 个交易日**、每天 242 个点，每点形如：

    "0930 3951.37 3982082 6255472603.00"
     时刻    价     累计量     累计成交额(元)

今日曲线与昨日曲线在同一份响应里，不需要为"昨日同时刻"再打一次网络；两个口径
同源，不会出现"今日用快照、昨日用日线"那种错配。收盘后 1500 那个点就是全天额。

⚠️ 实测约束（都踩过）：

1. **东财 `push2*` 在本机被 TLS SNI 阻断且会漂移**，连项目自带的
   `MOSS_EM_DIRECT=1` 直连绕行也救不回来（2026-09-23 实测整跳不通）。因此东财
   只能当备用，不能当主源。
2. **京市（`bj899050`）的日内曲线不带成交额**：它的点只有 3 个字段
   （时刻/价/累计量）。所以京市走「用**自己的量比** × 昨日全天额」折算
   （见 `build_forecast` 的 `bse_prev_total` 分支）。京市成交额可从腾讯快照
   字段取（`qt[37]` 为万元；实测 1834639.29 万元 = 183.46 亿 = 北交所全市场）。
3. 京市占全市场约 1%（2026-09-23：183.46 亿 / 1.78 万亿），对 1000 亿的判定
   阈值影响可忽略。

## 与「2 万亿 / ±1000 亿」阈值的关系（用户口径 2026-09-23）

- 预测量能 < 2 万亿，**或** 相比昨日缩量 1000 亿以上 → 缩量，**不追高**；
- 否则（放量或基本持平）→ 放量，**可做T**。

`advice` 就是这两条的直译；`verdict_text` 是给前端直接显示的短句。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from src.core.errors import BRIEF_TIGHT, brief

logger = logging.getLogger(__name__)

#: 三市口径的指数代码（腾讯符号）。
#: 沪/深指数成交额 = 该市场**全部**成交额（实测：沪指 10,079.96 亿 vs 仓库逐股
#: 求和 10,078.48 亿，吻合到 0.015%）；北证50 同口径也等于整个北交所。
MARKET_SYMBOLS: tuple[tuple[str, str, str], ...] = (
    ("sh", "sh000001", "沪市"),
    ("sz", "sz399001", "深市"),
    ("bj", "bj899050", "京市"),
)

#: 用户阈值：全市场预测成交额低于此值即视为缩量（元）。
CONTRACTION_FLOOR = 2.0e12
#: 用户阈值：相比昨日缩量超过此值即视为缩量（元）。
CONTRACTION_DELTA = 1000e8

#: 腾讯接口地址。
DAY_QUERY_URL = "https://web.ifzq.gtimg.cn/appstock/app/day/query"
FQKLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"

_TENCENT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
    "Referer": "https://gu.qq.com/",
}

#: 缓存 TTL：盘中 30 秒（这一行跟着市场环境条一起刷新）；
#: 收盘后数字不再变，TTL 到期也只是重取同一份。
CACHE_TTL_LIVE = 30.0


def _parse_point(row: Any) -> tuple[str, float, float] | None:
    """腾讯分时点 → `(HHMM, 累计量, 累计成交额)`；缺成交额字段时成交额记 0。"""
    parts = str(row).split()
    if len(parts) < 3:
        return None
    try:
        volume = float(parts[2])
    except (TypeError, ValueError):
        return None
    amount = 0.0
    if len(parts) >= 4:
        try:
            amount = float(parts[3])
        except (TypeError, ValueError):
            amount = 0.0
    return parts[0], volume, amount


def parse_curve(rows: list[Any]) -> tuple[dict[str, float], dict[str, float]]:
    """分时行列表 → `({HHMM: 累计成交额}, {HHMM: 累计量})`。"""
    amounts: dict[str, float] = {}
    volumes: dict[str, float] = {}
    for row in rows:
        point = _parse_point(row)
        if point is None:
            continue
        stamp, volume, amount = point
        volumes[stamp] = volume
        if amount > 0:
            amounts[stamp] = amount
    return amounts, volumes


@dataclass
class MarketTurnoverForecast:
    """全市场成交额预测量能快照（全部字段可 JSON 序列化）。"""

    available: bool = False
    trade_date: str = ""
    prev_date: str = ""
    fetched_at: str = ""
    source: str = "tencent_day_query(沪深京指数)"

    # ---- 数字（单位：元）----
    #: 今日当前累计成交额（参与合计的市场之和）
    today_amount: float | None = None
    #: 昨日**同一时刻**累计成交额
    prev_same_time_amount: float | None = None
    #: 昨日全天成交额
    prev_total_amount: float | None = None
    #: 全天预测成交额
    projected_amount: float | None = None
    #: 预测量 - 昨日全天（正=放量，负=缩量）
    delta_amount: float | None = None
    #: 预测量 / 昨日全天
    ratio: float | None = None
    #: 对比时刻（HHMM）
    moment: str = ""

    # ---- 结论 ----
    #: "缩量" / "放量" / "平量"
    verdict: str = ""
    #: 前端直接可用的短句，如 "缩量 1,180 亿 不追高"
    verdict_text: str = ""
    #: 缩量时 False（不追高）
    chase_allowed: bool = True
    #: 缩量时 False（不做T）
    t_allowed: bool = True
    #: 判定依据（命中哪条阈值），进 tooltip
    reasons: list[str] = field(default_factory=list)
    #: 数据缺口/口径说明
    notes: list[str] = field(default_factory=list)
    gap: str | None = None

    #: 各市场明细：`{market: {name, today, prev_same, prev_total, projected}}`（元）
    markets: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: 参与合计的市场名（口径透明度：缺哪个市场一眼可见）
    markets_used: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "trade_date": self.trade_date,
            "prev_date": self.prev_date,
            "fetched_at": self.fetched_at,
            "source": self.source,
            "today_amount": self.today_amount,
            "prev_same_time_amount": self.prev_same_time_amount,
            "prev_total_amount": self.prev_total_amount,
            "projected_amount": self.projected_amount,
            "delta_amount": self.delta_amount,
            "ratio": self.ratio,
            "moment": self.moment,
            "verdict": self.verdict,
            "verdict_text": self.verdict_text,
            "chase_allowed": self.chase_allowed,
            "t_allowed": self.t_allowed,
            "reasons": list(self.reasons),
            "notes": list(self.notes),
            "gap": self.gap,
            "markets": self.markets,
            "markets_used": list(self.markets_used),
        }


def yi(value: float | None) -> str:
    """元 → 亿元文本（千分位，0 位小数）。"""
    return "—" if value is None else f"{value / 1e8:,.0f}"


def wan_yi(value: float | None) -> str:
    """元 → 万亿元文本（2 位小数）。"""
    return "—" if value is None else f"{value / 1e12:.2f}"


def classify_turnover(
    *, projected_amount: float | None, prev_total_amount: float | None,
) -> tuple[str, float | None, bool, list[str]]:
    """量能预测 → `(verdict, delta, 是否可做T/追高, reasons)`。

    阈值来自用户口径（2026-09-23）：
      - 预测 < 2 万亿 → 缩量；
      - 或相比昨日缩量 > 1000 亿 → 缩量；
      - 其余（放量或未达阈值的小幅缩量）→ 放量。
    """
    if projected_amount is None:
        return "", None, True, []
    if prev_total_amount is None:
        return "平量", None, True, ["缺昨日全天成交额，无法比较"]
    delta = projected_amount - prev_total_amount
    reasons: list[str] = []
    if projected_amount < CONTRACTION_FLOOR:
        reasons.append(
            f"预测 {wan_yi(projected_amount)} 万亿 < 2 万亿（低于量能地板）")
    if delta < -CONTRACTION_DELTA:
        reasons.append(f"预测比昨日缩量 {yi(-delta)} 亿 > 1000 亿")
    if reasons:
        return "缩量", delta, False, reasons
    if delta > 0:
        reasons.append(f"预测比昨日放量 {yi(delta)} 亿")
    else:
        reasons.append(
            f"预测与昨日基本持平（缩 {yi(-delta)} 亿，未达 1000 亿阈值）")
    return "放量", delta, True, reasons


def build_forecast(
    *,
    today_curves: dict[str, dict[str, float]],
    prev_curves: dict[str, dict[str, float]],
    names: dict[str, str] | None = None,
    today_volumes: dict[str, dict[str, float]] | None = None,
    prev_volumes: dict[str, dict[str, float]] | None = None,
    bse_prev_total: float | None = None,
    bse_today_amount: float | None = None,
    bse_prev_amount: float | None = None,
) -> MarketTurnoverForecast:
    """纯函数：两条曲线（今日/昨日，各市场）→ 预测量能快照。

    为什么抽成纯函数：这段是本模块唯一有"业务判断"的地方，必须能脱离网络单测
    （阈值边界、缺市场、时刻对齐、京市折算都在测试里覆盖）。

    Args:
        today_curves: `{market: {HHMM: 累计成交额}}`（今日）
        prev_curves:  `{market: {HHMM: 累计成交额}}`（昨日）
        today_volumes/prev_volumes: 同结构的**累计量**，用于京市（它的曲线没有
            成交额字段）：用京市自己的**同时刻量比**乘它的昨日全天成交额。
        bse_prev_total: 京市昨日全天成交额（元）。拿不到就如实少算一个市场。
        bse_today_amount / bse_prev_amount: 京市今日与昨日的**成交额**（元，
            来自快照）。两者都给到时优先用"成交额比"折算京市（与沪深同口径），
            否则退回用它自己的同时刻量比。
    """
    # `names` 缺省就用模块自己的三市中文名：调用方（取数层、测试、诊断）绝大多数
    # 只关心数字，不该被迫为"沪市/深市/京市"这三个字再传一遍。
    names = names or {key: name for key, _symbol, name in MARKET_SYMBOLS}
    today_volumes = today_volumes or {}
    prev_volumes = prev_volumes or {}
    result = MarketTurnoverForecast()

    # ---- 对比时刻：优先用今日各市场的最后一个点 ----
    #
    # ⚠️ 这里的判断要允许"今日只有京市（且只有量、没有额）"这一种输入 ——
    #    京市靠 `bse_prev_total` + 同时刻量比就能算出预测量，不该被这个前返回挡掉。
    #    所以只有**一条曲线都没有**时才提前返回。
    if not today_curves:
        result.gap = "今日分时曲线为空（未取到成交额）"
        return result
    moments = [max(curve) for curve in today_curves.values() if curve]
    if moments:
        moment = max(moments)
        reference = today_curves.get("sh") or next(iter(today_curves.values()))
        chosen = ""
        for stamp in sorted((s for s in reference if s <= moment), reverse=True):
            if all(stamp in prev_curves.get(market, {})
                   for market, curve in today_curves.items() if curve):
                chosen = stamp
                break
    else:
        # 今日只有京市（且它的曲线不带额）：时刻表取沪深与京市可用时刻的交集
        volume_moments = [max(curve) for curve in today_volumes.values() if curve]
        chosen = max(volume_moments) if volume_moments else ""
    if not chosen:
        result.gap = "昨日分时曲线缺少可同比的时刻，无法计算量能"
        return result
    result.moment = chosen

    # ---- 逐市场：今日累计 / 昨日同时刻 / 昨日全天 ----
    detail: dict[str, dict[str, Any]] = {}
    for market, name in names.items():
        today_curve = today_curves.get(market) or {}
        prev_curve = prev_curves.get(market) or {}
        item: dict[str, Any] = {
            "name": name, "today": None, "prev_same": None,
            "prev_total": None, "projected": None,
        }
        if today_curve:
            today_now = today_curve.get(chosen)
            prev_then = prev_curve.get(chosen)
            prev_total = prev_curve[max(prev_curve)] if prev_curve else None
            item.update(today=today_now, prev_same=prev_then, prev_total=prev_total)
            if (today_now is not None and prev_then not in (None, 0.0)
                    and prev_total is not None):
                item["projected"] = prev_total * today_now / prev_then
        detail[market] = item

    # ---- 京市：曲线无成交额 → 用**成交额比**或**量比**折算 ----
    #
    # 京市自己的日内曲线是完整的（有累计量），但第 4 个字段（成交额）缺失。
    # 它的成交额只能从**快照**拿（腾讯 `qt[37]`，万元），而快照只有"今天"。
    # 因此分两条路（取数层已经算好 bse_today_amount / bse_prev_total）：
    #
    #   ① 有昨日成交额 → 京市额比 = 今日额 / 昨日额（最准，与沪深同口径）；
    #   ② 只有昨日成交额 + 自己的量比 → 昨日额 × 量比（量的口径，够用）。
    #
    # 为什么不拿沪深的额比去折京市：那等于把沪深的价格效应算到京市头上
    # （实测会偏 3~8%）。京市占全市场约 1%，但它自己的量比是现成的，没理由不用。
    if bse_prev_total:
        bj = detail.setdefault("bj", {"name": names.get("bj", "京市")})
        bj_today_vol = (today_volumes.get("bj") or {}).get(chosen)
        bj_prev_vol = (prev_volumes.get("bj") or {}).get(chosen)
        if bse_today_amount and bse_prev_amount:
            ratio = bse_today_amount / bse_prev_amount
            bj.update(today=bse_today_amount, prev_same=bse_prev_amount,
                      prev_total=bse_prev_total,
                      projected=bse_prev_total * ratio,
                      folded_by="成交额比")
            result.notes.append(
                f"京市日内曲线不带成交额：按快照成交额比 {ratio:.3f}× 折算")
        elif bj_today_vol and bj_prev_vol:
            ratio = bj_today_vol / bj_prev_vol
            bj.update(prev_total=bse_prev_total,
                      projected=bse_prev_total * ratio,
                      folded_by="同时刻量比")
            result.notes.append(
                f"京市日内曲线不带成交额：按它自己的同时刻量比 "
                f"{ratio:.3f}× 折算（占全市场约 1%，对 1000 亿阈值影响可忽略）")
        else:
            result.notes.append("京市未计入：既无成交额比也无同时刻量比")
        detail["bj"] = bj

    # ---- 合计：能给出"预测量"的市场 ----
    used = [market for market, item in detail.items()
            if item.get("projected") is not None]
    missing = [names.get(market, market) for market in detail if market not in used]
    result.markets_used = [names.get(market, market) for market in used]
    result.markets = detail
    if not used:
        result.gap = "没有任何市场能算出全天预测量"
        return result

    result.projected_amount = sum(float(detail[m]["projected"]) for m in used)
    # ⚠️ 三个"合计"的**市场集合并不相同**：
    #   - `projected`：有昨日全天额 + 同时刻比例的市场（京市靠量比，也有）；
    #   - `today` / `prev_same`：必须有**成交额曲线**才有（京市没有）；
    #   - `prev_total`：只要知道昨日全天额就计入（京市有）。
    # 所以下面不能共用一个 `used`，否则要么漏掉京市的昨日全天（高估缩量），
    # 要么把京市那条 `None` 当成 0 混进今日累计额。
    today_parts = [float(detail[m]["today"]) for m in used
                   if detail[m].get("today") is not None]
    prev_same_parts = [float(detail[m]["prev_same"]) for m in used
                       if detail[m].get("prev_same") is not None]
    prev_total_parts = [float(detail[m]["prev_total"]) for m in used
                        if detail[m].get("prev_total") is not None]
    result.today_amount = sum(today_parts) if today_parts else None
    result.prev_same_time_amount = sum(prev_same_parts) if prev_same_parts else None
    result.prev_total_amount = sum(prev_total_parts) if prev_total_parts else None
    result.ratio = (result.projected_amount / result.prev_total_amount
                    if result.prev_total_amount else None)

    verdict, delta, allowed, reasons = classify_turnover(
        projected_amount=result.projected_amount,
        prev_total_amount=result.prev_total_amount)
    result.verdict = verdict
    result.delta_amount = delta
    result.chase_allowed = allowed
    result.t_allowed = allowed
    result.reasons = reasons
    if missing:
        # 京市已经在上面的 note 里说清原因了，这里不再重复一遍
        rest = [name for name in missing if name != names.get("bj")]
        if rest:
            result.notes.append("未计入 " + "、".join(rest) + "（该市场数据不可得）")
    if verdict == "缩量":
        result.verdict_text = (f"缩量 {yi(abs(delta))} 亿 不追高"
                               if delta is not None else "缩量 不追高")
    elif verdict == "放量":
        result.verdict_text = f"放量 {yi(delta)} 亿 可做T" if delta else "放量 可做T"
    else:
        result.verdict_text = "量能持平"
    result.available = result.projected_amount is not None
    return result


class MarketTurnoverProvider:
    """全市场成交额预测量能取数（腾讯 `day/query`，进程内 TTL 缓存）。"""

    def __init__(self, *, cache_ttl: float = CACHE_TTL_LIVE,
                 client: httpx.AsyncClient | None = None) -> None:
        self._cache: MarketTurnoverForecast | None = None
        self._cache_at = 0.0
        self._cache_ttl = cache_ttl
        self._client = client
        self._lock = asyncio.Lock()
        # 京市成交额：一天只变一次，缓存到进程重启即可。
        self._bse_amounts_cache: dict[str, float | None] | None = None

    def invalidate(self) -> None:
        self._cache = None
        self._cache_at = 0.0

    def snapshot_cached(self) -> MarketTurnoverForecast | None:
        """只读缓存，**绝不发网络请求**。

        为什么需要它：调用方（`IntradayService._market_turnover`）在逐票快照里
        最多只等 2.5 秒；等不到时要能"拿到已有的那份/什么都没有"，而不是再触发
        一次真实取数。返回 `None` 表示这一轮确实没有可用的量能数据。
        """
        return self._cache

    async def snapshot(self, *, force: bool = False) -> MarketTurnoverForecast:
        """取量能预测（带 TTL 缓存；并发调用共享同一次取数）。"""
        if (not force and self._cache is not None and self._cache_ttl > 0
                and time.monotonic() - self._cache_at < self._cache_ttl):
            return self._cache
        async with self._lock:
            if (not force and self._cache is not None and self._cache_ttl > 0
                    and time.monotonic() - self._cache_at < self._cache_ttl):
                return self._cache
            forecast = await self._fetch()
            self._cache = forecast
            self._cache_at = time.monotonic()
            return forecast

    async def _fetch(self) -> MarketTurnoverForecast:
        """拉三个市场的 day/query，拆出今日与昨日两条曲线。"""
        result = MarketTurnoverForecast()
        result.fetched_at = time.strftime("%Y-%m-%d %H:%M:%S")
        today_curves: dict[str, dict[str, float]] = {}
        prev_curves: dict[str, dict[str, float]] = {}
        today_volumes: dict[str, dict[str, float]] = {}
        prev_volumes: dict[str, dict[str, float]] = {}
        names: dict[str, str] = {}
        gaps: list[str] = []

        client = self._client
        owns_client = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=15.0, headers=_TENCENT_HEADERS)
        try:
            for market, symbol, name in MARKET_SYMBOLS:
                names[market] = name
                try:
                    resp = await client.get(DAY_QUERY_URL, params={"code": symbol})
                    resp.raise_for_status()
                    payload = resp.json()
                except Exception as exc:  # noqa: BLE001 网络/JSON 都算这一跳失败
                    gaps.append(f"{name}分时取数失败：{brief(exc, BRIEF_TIGHT)}")
                    continue
                node = (payload.get("data") or {}).get(symbol) or {}
                parsed: list[tuple[str, dict[str, float], dict[str, float]]] = []
                for day in node.get("data") or []:
                    amounts, volumes = parse_curve(day.get("data") or [])
                    if volumes:
                        parsed.append((str(day.get("date") or ""), amounts, volumes))
                if len(parsed) < 2:
                    gaps.append(f"{name}分时曲线不足两天")
                    continue
                parsed.sort(key=lambda item: item[0])
                prev_curves[market], prev_volumes[market] = parsed[-2][1], parsed[-2][2]
                today_curves[market], today_volumes[market] = parsed[-1][1], parsed[-1][2]
                if parsed[-1][0]:
                    result.trade_date = parsed[-1][0]
                if parsed[-2][0]:
                    result.prev_date = parsed[-2][0]

            if not today_curves:
                result.gap = "；".join(gaps) or "三市分时曲线全部为空"
                return result

            bse = await self._bse_amounts(client, result.trade_date, gaps)
            built = build_forecast(
                today_curves=today_curves, prev_curves=prev_curves, names=names,
                today_volumes=today_volumes, prev_volumes=prev_volumes,
                bse_prev_total=bse.get("prev_total"),
                bse_today_amount=bse.get("today_amount"),
                bse_prev_amount=bse.get("prev_amount"))
            built.fetched_at = result.fetched_at
            built.trade_date = built.trade_date or result.trade_date
            built.prev_date = built.prev_date or result.prev_date
            built.notes.extend(gaps)
            if not built.available:
                joined = "；".join(gaps)
                built.gap = f"{built.gap}；{joined}" if built.gap and joined else (
                    built.gap or joined or None)
            return built
        finally:
            if owns_client:
                await client.aclose()

    async def _bse_amounts(
        self, client: httpx.AsyncClient, trade_date: str, gaps: list[str],
    ) -> dict[str, float | None]:
        """京市的成交额：`{today_amount, prev_amount, prev_total}`（元）。

        口径：北证50 的成交额**就是整个北交所**（实测 2026-09-23：腾讯快照
        字段 `qt[37]` = 1834639.29 万元 = 183.46 亿，与新浪 `bj899050` 一致）。

        ## 为什么要绕这一下（实测踩过两次）

        1. `day/query`（沪深的成交额曲线来源）对 `bj899050` **只返回当日一天**，
           而且它的点**没有成交额字段**（只有量）—— 所以京市的"昨日成交额"和
           "昨日同时刻成交额"都不能从那里拿。
        2. `fqkline`（日K）对 `bj899050` **只有 1 行**且无成交额；沪指那 5 行也
           只有成交量。所以日K也推不出昨日。

        唯一稳定可得的是**快照的今日成交额**（`qt[37]`）。于是：
          - 若拿到 `day/query` 里京市自己的**两日成交量**，按量比反推昨日成交额；
          - 反推不到就退回只用"量比 × 今日额"给京市投影（见 `build_forecast`）。
        """
        if self._bse_amounts_cache is not None:
            return self._bse_amounts_cache
        symbol = "bj899050"
        out: dict[str, float | None] = {
            "today_amount": None, "prev_amount": None, "prev_total": None}
        try:
            resp = await client.get(
                FQKLINE_URL, params={"param": f"{symbol},day,,,5,qfq"})
            resp.raise_for_status()
            node = (resp.json().get("data") or {}).get(symbol) or {}
            qt = (node.get("qt") or {}).get(symbol) or []
            rows = node.get("day") or node.get("qfqday") or []
            if len(qt) > 37:
                try:
                    out["today_amount"] = float(qt[37]) * 1e4     # 万元 → 元
                except (TypeError, ValueError):
                    out["today_amount"] = None
            if out["today_amount"] is None:
                gaps.append("京市今日成交额不可得（快照无成交额字段），京市未计入")
                self._bse_amounts_cache = out
                return out
            # 京市要进「同时间同比」，需要**昨日同时刻成交额**。实测三条路都不通：
            #   - `day/query`：只返回当日一天，且它的点连成交额字段都没有；
            #   - `fqkline`：只有当日一行，无成交额；
            #   - 快照：只有今日成交额。
            # 所以这里**不猜**昨日额。京市占全市场约 1%（实测 183 亿 / 1.78 万亿），
            # 宁可如实少算一个市场，也不用"量比×价"那类近似去凑一个看起来完整的三市数。
            latest_date = str(rows[-1][0]).replace("-", "") if rows else ""
            if len(rows) >= 2 and latest_date == (trade_date or ""):
                today_volume = float(rows[-1][5] or 0.0)
                prev_volume = float(rows[-2][5] or 0.0)
                if today_volume and prev_volume:
                    out["prev_amount"] = out["today_amount"] * prev_volume / today_volume
                    out["prev_total"] = out["prev_amount"]
            if out["prev_amount"] is None:
                gaps.append(
                    "京市未计入同时间同比：腾讯只给当日成交额（day/query 仅当日且无"
                    "成交额字段、日K仅一行），拿不到昨日同时刻额；京市约占全市场 1%")
        except Exception as exc:  # noqa: BLE001
            gaps.append(f"京市成交额取数失败：{brief(exc, BRIEF_TIGHT)}")
        self._bse_amounts_cache = out
        return out


__all__ = [
    "CACHE_TTL_LIVE",
    "CONTRACTION_DELTA",
    "CONTRACTION_FLOOR",
    "DAY_QUERY_URL",
    "FQKLINE_URL",
    "MARKET_SYMBOLS",
    "MarketTurnoverForecast",
    "MarketTurnoverProvider",
    "build_forecast",
    "classify_turnover",
    "parse_curve",
    "wan_yi",
    "yi",
]
