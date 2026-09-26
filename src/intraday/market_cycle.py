"""市场情绪周期：涨停家数 / 炸板率 / 最高连板 → 周期阶段与做T环境温度。

## 为什么做T需要一个「市场级」因子

做T是**顺周期**动作，同一套回踩/冲高规则在不同周期阶段的胜率差一个数量级：

- **主升期**：主线内「所有的止盈事后看都是卖飞」→ 做T等于自己把仓位做丢；
- **高位震荡期**：最适合冲高回踩（右侧没有左侧值博率高）；
- **退潮期**：补涨和龙头都大幅杀跌 → 做T大概率 T 反，回踩被埋、高吸更惨；
- **冰点**：连板高度掉到 2 板、连板家数不足 4 家 → 没有接力资金，做T的对手盘都没了。

所以它不是一个「偏多还是偏空」的方向票，而是**做T可行性的闸门 + 温度**。
落到打分上就是：温度决定它给多少分，退潮/冰点直接触发一票否决（`t_allowed=False`）。

## 数据来源

东方财富涨停池/炸板池/跌停池，经 akshare 取（实测 2026-09-16 单次 0.1~0.2 秒，
四个池合计 < 1 秒）。全部为**盘中实时**更新，因此按 `date` + TTL 缓存。

取不到时**如实记 gap 并置 `available=False`**，绝不用「上一次的数据」冒充今天 ——
周期阶段判断错一天，退潮期就会当成震荡期放开做T，这是会造成实际亏损的那类错误。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from src.core.errors import (
    BRIEF_DEFAULT,
    brief,
)

logger = logging.getLogger(__name__)

Stage = Literal["冰点", "试错期", "发酵期", "主升期", "高位震荡期", "退潮期"]

# 周期阶段阈值（与技能库《市场情绪周期》里的口径一致，改这里就等于改全局判定）
THRESHOLDS: dict[str, float] = {
    "limit_up_active": 30.0,      # 涨停家数 ≥30 → 市场活跃
    "limit_up_hot": 60.0,         # 涨停家数 ≥60 → 主升/高潮
    "limit_up_dead": 15.0,        # 涨停家数 <15 → 冰点特征
    "broken_rate_retreat": 0.60,  # 炸板率 >60% → 退潮特征
    "broken_rate_healthy": 0.30,  # 炸板率 <30% → 承接健康
    "streak_main": 5,             # 最高连板 ≥5 → 有身位
    "streak_dead": 2,             # 最高连板 ≤2 → 冰点
    "streak_count_dead": 4,       # 连板家数 <4 → 冰点
    "big_loss_veto": 10,          # 大面 ≥10 家 → 一票否决
    "limit_down_veto": 10,        # 跌停 >10 家 → 一票否决
}

DEFAULT_CACHE_TTL = 60.0


@dataclass
class MarketCycle:
    """市场情绪周期快照（全部字段可 JSON 序列化）。"""

    available: bool = False
    trade_date: str = ""
    fetched_at: str = ""
    source: str = "eastmoney_pool(akshare)"
    gap: str | None = None

    # ---- 原始计数 ----
    limit_up_count: int = 0
    limit_down_count: int = 0
    broken_count: int = 0
    strong_count: int = 0
    first_board: int = 0
    streak2plus: int = 0
    max_streak: int = 0
    big_loss_count: int = 0

    # ---- 派生 ----
    broken_rate: float | None = None
    promotion_rate: float | None = None   # 连板家数 / 涨停家数（晋级占比，粗口径）

    # ---- 结论 ----
    stage: Stage = "试错期"
    temperature: int = 50
    t_allowed: bool = True
    gates: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available, "trade_date": self.trade_date,
            "fetched_at": self.fetched_at, "source": self.source, "gap": self.gap,
            "limit_up_count": self.limit_up_count,
            "limit_down_count": self.limit_down_count,
            "broken_count": self.broken_count, "strong_count": self.strong_count,
            "first_board": self.first_board, "streak2plus": self.streak2plus,
            "max_streak": self.max_streak, "big_loss_count": self.big_loss_count,
            "broken_rate": self.broken_rate, "promotion_rate": self.promotion_rate,
            "stage": self.stage, "temperature": self.temperature,
            "t_allowed": self.t_allowed, "gates": list(self.gates),
            "notes": list(self.notes), "gaps": list(self.gaps),
            "thresholds": dict(THRESHOLDS),
        }


def _to_int(value: Any) -> int:
    try:
        if value is None:
            return 0
        number = float(value)
    except (TypeError, ValueError):
        return 0
    if number != number:  # NaN
        return 0
    return int(number)


def _count_rows(frame: Any) -> int:
    if frame is None:
        return 0
    try:
        return int(len(frame))
    except TypeError:
        return 0


def _max_streak(frame: Any) -> int:
    """涨停池里的最高连板数（池子自带「连板数」列）。"""
    if frame is None or "连板数" not in getattr(frame, "columns", []):
        return 0
    series = frame["连板数"]
    best = 0
    for value in series:
        best = max(best, _to_int(value))
    return best


def _count_streak_at_least(frame: Any, level: int) -> int:
    if frame is None or "连板数" not in getattr(frame, "columns", []):
        return 0
    return sum(1 for value in frame["连板数"] if _to_int(value) >= level)


def _big_loss_count(limit_down: Any, broken: Any) -> int:
    """大面家数：跌停 + 跌幅超过 9% 的炸板股（炸板后大幅回落＝当日接力资金被埋）。"""
    total = _count_rows(limit_down)
    if broken is None or "涨跌幅" not in getattr(broken, "columns", []):
        return total
    for value in broken["涨跌幅"]:
        try:
            if float(value) <= -9.0:
                total += 1
        except (TypeError, ValueError):
            continue
    return total


def classify_stage(*, limit_up_count: int, broken_rate: float | None, max_streak: int,
                   streak2plus: int, big_loss_count: int,
                   limit_down_count: int) -> tuple[Stage, list[str]]:
    """计数 → 周期阶段 + 一票否决项（纯函数，便于单测穷举）。

    判定顺序刻意是「风险优先」：先看一票否决与退潮，再看冰点，最后才认主升 ——
    退潮期的尾巴常常仍有 50+ 家涨停（补涨股在动），若按涨停家数先判成「发酵期」
    会把最危险的阶段认成最安全的阶段。
    """
    gates: list[str] = []
    if big_loss_count >= THRESHOLDS["big_loss_veto"]:
        gates.append(f"大面 {big_loss_count} 家 ≥ {THRESHOLDS['big_loss_veto']:.0f}（一票否决）")
    if limit_down_count > THRESHOLDS["limit_down_veto"]:
        gates.append(
            f"跌停 {limit_down_count} 家 > {THRESHOLDS['limit_down_veto']:.0f}（一票否决）")
    retreat = (
        (broken_rate is not None and broken_rate > THRESHOLDS["broken_rate_retreat"])
        or limit_up_count < THRESHOLDS["limit_up_dead"]
        or bool(gates)
    )
    ice = (
        max_streak <= THRESHOLDS["streak_dead"]
        and streak2plus < THRESHOLDS["streak_count_dead"]
    )
    if retreat and not ice:
        return "退潮期", gates
    if ice:
        return "冰点", gates
    if limit_up_count >= THRESHOLDS["limit_up_hot"] and max_streak >= THRESHOLDS["streak_main"]:
        if broken_rate is None or broken_rate < THRESHOLDS["broken_rate_healthy"]:
            return "主升期", gates
        return "高位震荡期", gates
    if max_streak >= THRESHOLDS["streak_main"]:
        return "高位震荡期", gates
    if limit_up_count >= THRESHOLDS["limit_up_active"]:
        return "发酵期", gates
    return "试错期", gates


def temperature_from(*, limit_up_count: int, broken_rate: float | None, max_streak: int,
                     limit_down_count: int) -> int:
    """做T环境温度 0~100（越高越适合动手）。

    四项加权：涨停广度35 + 承接质量25 + 连板身位25 + 亏钱效应反向15。
    刻意**不给方向**（不表示看多），只表示「今天的做T环境好不好」。
    """
    breadth = min(1.0, limit_up_count / THRESHOLDS["limit_up_hot"]) * 35.0
    if broken_rate is None:
        quality = 12.5  # 炸板率缺失给中位分，不假装很好也不假装很差
    else:
        quality = max(0.0, min(1.0, 1.0 - broken_rate / THRESHOLDS["broken_rate_retreat"])) * 25.0
    height = min(1.0, max_streak / 7.0) * 25.0
    loss = max(0.0, min(1.0, 1.0 - limit_down_count / 15.0)) * 15.0
    return int(round(max(0.0, min(100.0, breadth + quality + height + loss))))


def build_market_cycle(*, trade_date: str, limit_up: Any, broken: Any, limit_down: Any,
                       strong: Any = None, source: str = "eastmoney_pool(akshare)") -> MarketCycle:
    """四个池子 → 周期快照（纯函数，不做任何 IO，测试可直接喂 DataFrame）。"""
    now = datetime.now().isoformat(timespec="seconds")
    limit_up_count = _count_rows(limit_up)
    limit_down_count = _count_rows(limit_down)
    broken_count = _count_rows(broken)
    if limit_up_count == 0 and broken_count == 0:
        return MarketCycle(
            available=False, trade_date=trade_date, fetched_at=now, source=source,
            gap=f"{trade_date} 涨停池与炸板池均为空（非交易日或行情源未就绪）")

    max_streak = _max_streak(limit_up)
    streak2plus = _count_streak_at_least(limit_up, 2)
    first_board = max(0, limit_up_count - streak2plus)
    denominator = limit_up_count + broken_count
    broken_rate = round(broken_count / denominator, 4) if denominator > 0 else None
    big_loss = _big_loss_count(limit_down, broken)
    stage, gates = classify_stage(
        limit_up_count=limit_up_count, broken_rate=broken_rate, max_streak=max_streak,
        streak2plus=streak2plus, big_loss_count=big_loss,
        limit_down_count=limit_down_count)
    temperature = temperature_from(
        limit_up_count=limit_up_count, broken_rate=broken_rate,
        max_streak=max_streak, limit_down_count=limit_down_count)

    broken_text = (f"（炸板率 {broken_rate * 100:.0f}%）" if broken_rate is not None
                   else "（炸板率不可得）")
    notes = [
        f"涨停 {limit_up_count} 家（首板 {first_board} / 连板 {streak2plus}），"
        f"最高 {max_streak} 板，炸板 {broken_count} 家{broken_text}",
        f"跌停 {limit_down_count} 家，大面 {big_loss} 家 → 周期阶段「{stage}」，"
        f"做T环境温度 {temperature}/100",
    ]
    if stage == "主升期":
        notes.append("主升期：主线内做T极易卖飞（「主升段所有的止盈事后看都是卖飞」），"
                     "建议降低做T频率、以持股为主")
    elif stage == "高位震荡期":
        notes.append("高位震荡期：最适合冲高回踩的阶段（右侧没有左侧值博率高）")
    elif stage == "退潮期":
        notes.append("退潮期：补涨与龙头同时大幅杀跌，做T大概率 T 反 → 本模块禁止正式做T信号")
    elif stage == "冰点":
        notes.append("冰点：连板高度与连板家数双低，没有接力资金，做T缺少对手盘 → 禁止动手")
    elif stage == "发酵期":
        notes.append("发酵期：有板块效应但尚未主升，回踩胜率尚可，快进快出")
    else:
        notes.append("试错期：资金四处乱窜没有合力，只做小仓位试错，不要重仓做T")

    return MarketCycle(
        available=True, trade_date=trade_date, fetched_at=now, source=source,
        limit_up_count=limit_up_count, limit_down_count=limit_down_count,
        broken_count=broken_count, strong_count=_count_rows(strong),
        first_board=first_board, streak2plus=streak2plus, max_streak=max_streak,
        big_loss_count=big_loss, broken_rate=broken_rate,
        promotion_rate=(round(streak2plus / limit_up_count, 4) if limit_up_count else None),
        stage=stage, temperature=temperature,
        t_allowed=not gates and stage not in ("退潮期", "冰点"),
        gates=gates, notes=notes, gaps=[],
    )


def _default_fetcher(date: str) -> dict[str, Any]:
    """默认取数器：akshare 东财四池（同步、阻塞，调用方负责丢线程池）。"""
    import akshare as ak

    return {
        "limit_up": ak.stock_zt_pool_em(date=date),
        "broken": ak.stock_zt_pool_zbgc_em(date=date),
        "limit_down": ak.stock_zt_pool_dtgc_em(date=date),
        "strong": ak.stock_zt_pool_strong_em(date=date),
    }


class MarketCycleProvider:
    """情绪周期取数 + 缓存（进程内单例，注入 service）。

    - `fetcher` 可注入，测试不碰网络。
    - 缓存键是**解析后的交易日**：盘中同一交易日内 TTL 到期会重取（池子实时变化），
      跨日自动失效。
    - 取数失败时返回 `available=False` 的快照并记 gap；**不返回上一次的旧值**，
      因为周期阶段错一天会导致「退潮期被当成震荡期」这种会造成实际亏损的误判。
    """

    def __init__(self, *, ttl_seconds: float = DEFAULT_CACHE_TTL,
                 fetcher: Callable[[str], dict[str, Any]] | None = None,
                 lookback_days: int = 7) -> None:
        self._ttl = max(0.0, float(ttl_seconds))
        self._fetcher = fetcher or _default_fetcher
        self._lookback_days = max(1, int(lookback_days))
        self._cache: dict[str, tuple[float, MarketCycle]] = {}
        self._lock = asyncio.Lock()

    # ---- 查询 ----

    async def snapshot(self, *, force: bool = False, now: datetime | None = None) -> MarketCycle:
        moment = now or datetime.now()
        async with self._lock:
            if not force:
                cached = self._fresh_from_cache(moment)
                if cached is not None:
                    return cached
            cycle = await asyncio.to_thread(self._fetch_sync, moment)
            self._cache[cycle.trade_date or moment.strftime("%Y%m%d")] = (
                time.monotonic(), cycle)
            return cycle

    def _fresh_from_cache(self, moment: datetime) -> MarketCycle | None:
        stamp = self._cache.get(moment.strftime("%Y%m%d"))
        if stamp is None:
            return None
        saved_at, cycle = stamp
        if self._ttl > 0 and (time.monotonic() - saved_at) > self._ttl:
            return None
        return cycle

    def invalidate(self) -> None:
        self._cache.clear()

    # ---- 取数 ----

    def _fetch_sync(self, moment: datetime) -> MarketCycle:
        last_error: str | None = None
        for offset in range(self._lookback_days):
            date = (moment - timedelta(days=offset)).strftime("%Y%m%d")
            try:
                pools = self._fetcher(date)
            except Exception as exc:  # noqa: BLE001 网络/接口变动都不该打断做T主链路
                last_error = f"{type(exc).__name__}: {brief(exc, BRIEF_DEFAULT)}"
                logger.info("情绪周期取数失败(%s): %s", date, last_error)
                continue
            cycle = build_market_cycle(
                trade_date=date,
                limit_up=pools.get("limit_up"), broken=pools.get("broken"),
                limit_down=pools.get("limit_down"), strong=pools.get("strong"))
            if cycle.available:
                return cycle
            last_error = cycle.gap or "池子为空"
        return MarketCycle(
            available=False, trade_date=moment.strftime("%Y%m%d"),
            fetched_at=datetime.now().isoformat(timespec="seconds"),
            gap=f"近{self._lookback_days}天均未取到涨停池数据：{last_error or '未知原因'}",
            notes=["情绪周期不可用 → 该维度按「不可用」处理并从有效权重中扣除"],
        )
