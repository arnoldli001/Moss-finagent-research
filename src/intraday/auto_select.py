"""自动选股：从候选池里挑出"日K买入信号 + 分时打分高"的票，按综合分排序取前 N。

## 用户口径（2026-09-17）

> 遍历热门股前 50 和昨日涨停股、昨日成交额前 200 的股。当日 K 出现买入信号、
> 且日内分时多指标合成打分大于 40 分 → 提示低吸，自动加入自选，**最多 6 个**，
> 选分时和日K打分都高的股，排序给出前 6 个。

## 为什么不能"把 250 只票全跑一遍"

单只票的完整评分要 2~4 秒（含行情与板块），250 只就是 **8~16 分钟**，
而要求是"1 分钟刷新一次"。所以本模块**分两段**：

1. **候选池**（几乎零成本，本地仓库 + 一次批量快照）：
   三组并集去重 —— 热门股前 50（按近 N 日成交额）、昨日涨停股、昨日成交额前 200；
2. **两段打分**：
   - **粗筛**（便宜）：只算"日K是否触发买入信号"与"分时快速打分"，
     先按分时 + 日K 的可得信号排序，截到 `prescreen_size`（默认 30）；
   - **精算**（贵）：只对粗筛留下的票跑完整日K + 分时打分，产出最终排序。

这样每轮成本从 8~16 分钟压到 `prescreen_size × 2~4s`，配合 1 分钟节奏尚可，
且**得分越高的票越会被精算**（不会漏掉真正的强票）。

## 时间窗与频率（不要每时每刻都算）

只在**交易日的 9:25–9:40 与 14:45–15:00** 触发，每分钟一次 —— 这两个窗口是
低吸决策最关键的时段（开盘定方向、尾盘定隔夜）。其余时间算出来的结果既没人看，
又会持续占用数据源。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from datetime import time as dt_time
from typing import Any

from src.core.errors import (
    BRIEF_TIGHT,
    brief,
)

logger = logging.getLogger(__name__)

#: 用户要求：最多自动加入 6 只
DEFAULT_TOP_N = 6
#: 分时打分门槛（用户口径：> 40 分才提示低吸）
DEFAULT_MIN_INTRADAY_SCORE = 40.0
#: 粗筛后进入精算的票数上限（控制单轮耗时）
DEFAULT_PRESCREEN_SIZE = 30
#: 候选池规模：热门前 50 + 昨日成交额前 200（昨日涨停则并入）
DEFAULT_HOT_SIZE = 50
DEFAULT_AMOUNT_SIZE = 200
#: 触发窗口（本地时间）：开盘后定方向、尾盘定隔夜
TRIGGER_WINDOWS: tuple[tuple[dt_time, dt_time], ...] = (
    (dt_time(9, 25), dt_time(9, 40)),
    (dt_time(14, 45), dt_time(15, 0)),
)
#: 盘后（日K信号）触发时刻：收盘后 5 分钟（等日线定稿）
POST_CLOSE_TIME = dt_time(15, 5)


@dataclass
class AutoSelectItem:
    """一只入选（或候选）标的的打分明细。"""

    code: str
    name: str = ""
    daily_score: float | None = None
    intraday_score: float | None = None
    combined: float = 0.0
    buy_signals: list[str] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)
    reason: str = ""
    added: bool = False

    def as_dict(self) -> dict[str, Any]:
        """接口/日志用的普通字典。"""
        return {
            "code": self.code, "name": self.name,
            "daily_score": self.daily_score,
            "intraday_score": self.intraday_score,
            "combined": round(self.combined, 2),
            "buy_signals": list(self.buy_signals),
            "groups": list(self.groups),
            "reason": self.reason,
            "added": self.added,
        }


@dataclass
class AutoSelectResult:
    """一轮自动选股的结果（含未入选的候选，便于排查"为什么没选它"）。"""

    ran_at: str = ""
    window: str = ""            # open / close / post_close / manual
    candidates: int = 0         # 候选池去重后的数量
    scored: int = 0             # 真正跑了精算的数量
    selected: list[AutoSelectItem] = field(default_factory=list)
    rejected: list[AutoSelectItem] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        """接口返回。"""
        return {
            "ran_at": self.ran_at, "window": self.window,
            "candidates": self.candidates, "scored": self.scored,
            "selected": [item.as_dict() for item in self.selected],
            "rejected": [item.as_dict() for item in self.rejected[:20]],
            "added": list(self.added), "skipped": list(self.skipped),
            "notes": list(self.notes), "seconds": round(self.seconds, 2),
        }


def in_trigger_window(moment: datetime | None = None) -> str:
    """当前是否落在触发窗口里 → 返回窗口名（空串=不在窗口内）。

    Returns:
        `"open"`（9:25–9:40）/ `"close"`（14:45–15:00）/ `""`。
    """
    now = (moment or datetime.now()).time()
    if TRIGGER_WINDOWS[0][0] <= now <= TRIGGER_WINDOWS[0][1]:
        return "open"
    if TRIGGER_WINDOWS[1][0] <= now <= TRIGGER_WINDOWS[1][1]:
        return "close"
    return ""


def is_post_close(moment: datetime | None = None) -> bool:
    """是否已过"盘后"触发点（15:05 之后）。"""
    return (moment or datetime.now()).time() >= POST_CLOSE_TIME


def is_trading_day(moment: datetime | None = None) -> bool:
    """交易日判定：先看周末，再用市场时钟（`live_session_date`）排除节假日。

    市场时钟取不到时**按交易日处理** —— 宁可多跑一轮（有缓存、成本低），
    也不要因为 QMT 没启动就整段跳过。
    """
    now = moment or datetime.now()
    if now.weekday() >= 5:
        return False
    try:
        from src.intraday.sources import live_session_date

        session = live_session_date()
        if session and session != now.strftime("%Y-%m-%d"):
            return False
    except Exception:  # noqa: BLE001 时钟不可用 → 按交易日处理
        pass
    return True


class AutoSelector:
    """自动选股器（只依赖注入进来的取数/评分函数，便于单测与解耦）。

    Args:
        pool_provider: 提供候选池（`async (hot, amount) -> dict[code, list[str]]`）
        daily_scorer: 日K打分（`async (code) -> (score, [买入信号]) | None`）
        intraday_scorer: 分时打分（`async (code) -> score | None`）
        top_n / min_intraday_score / prescreen_size: 见模块常量说明
    """

    def __init__(self, *, pool_provider: Any, daily_scorer: Any,
                 intraday_scorer: Any, top_n: int = DEFAULT_TOP_N,
                 min_intraday_score: float = DEFAULT_MIN_INTRADAY_SCORE,
                 prescreen_size: int = DEFAULT_PRESCREEN_SIZE) -> None:
        self._pool = pool_provider
        self._daily = daily_scorer
        self._intraday = intraday_scorer
        self.top_n = int(top_n)
        self.min_intraday = float(min_intraday_score)
        self.prescreen = int(prescreen_size)

    async def run(self, *, window: str = "manual",
                  dry_run: bool = False) -> AutoSelectResult:
        """跑一轮自动选股（**不做写入**，是否加自选由调用方决定）。"""
        started = time.perf_counter()
        result = AutoSelectResult(
            ran_at=datetime.now().isoformat(timespec="seconds"), window=window)
        pool = await self._pool(DEFAULT_HOT_SIZE, DEFAULT_AMOUNT_SIZE)
        result.candidates = len(pool)
        if not pool:
            result.notes.append("候选池为空：热门股/昨日涨停/成交额榜都取不到数据")
            result.seconds = time.perf_counter() - started
            return result

        # ---- 粗筛：先算"分时快速分"，按它排序，只把靠前的送去精算 ----
        prescored: list[tuple[float, str]] = []
        for code in pool:
            try:
                score = await self._intraday(code)
            except Exception as exc:  # noqa: BLE001 单只失败不影响整轮
                logger.debug("分时粗筛失败 %s: %s", code, brief(exc, BRIEF_TIGHT))
                continue
            if score is None:
                continue
            prescored.append((float(score), code))
        prescored.sort(reverse=True)
        shortlist = [code for _score, code in prescored[:self.prescreen]]
        result.notes.append(
            f"候选 {len(pool)} 只 → 分时可得 {len(prescored)} 只 → "
            f"取前 {len(shortlist)} 只进入精算（单轮耗时受此控制）")

        # ---- 精算：日K + 分时都算一遍，双条件筛选 ----
        for code in shortlist:
            try:
                daily = await self._daily(code)
                intraday_score = await self._intraday(code)
            except Exception as exc:  # noqa: BLE001
                logger.debug("精算失败 %s: %s", code, brief(exc, BRIEF_TIGHT))
                continue
            if daily is None or intraday_score is None:
                continue
            daily_score, signals = daily
            result.scored += 1
            item = AutoSelectItem(
                code=code, name=pool[code][0] if pool[code] else "",
                daily_score=daily_score, intraday_score=float(intraday_score),
                buy_signals=list(signals), groups=list(pool[code][1:]))
            # 双条件：日K有买入信号 AND 分时 > 阈值
            has_buy = bool(signals)
            meets_intraday = item.intraday_score >= self.min_intraday
            if has_buy and meets_intraday:
                item.combined = self.combine(item.daily_score, item.intraday_score)
                item.reason = (f"日K买入信号 {'/'.join(signals)}；"
                               f"分时 {item.intraday_score:.0f} 分")
                result.selected.append(item)
            else:
                missing = []
                if not has_buy:
                    missing.append("无日K买入信号")
                if not meets_intraday:
                    missing.append(f"分时 {item.intraday_score:.0f} < {self.min_intraday:.0f}")
                item.reason = "、".join(missing)
                result.rejected.append(item)

        # ---- 排序取前 N：分时与日K都高的优先 ----
        result.selected.sort(key=lambda entry: -entry.combined)
        result.selected = result.selected[:self.top_n]
        result.rejected.sort(key=lambda entry: -(entry.intraday_score or 0))
        result.seconds = time.perf_counter() - started
        logger.info("自动选股(%s)：候选 %d → 精算 %d → 入选 %d（%.1fs）",
                    window, result.candidates, result.scored,
                    len(result.selected), result.seconds)
        return result

    @staticmethod
    def combine(daily_score: float | None, intraday_score: float | None) -> float:
        """综合分：两个口径**都高**才算高。

        用几何平均而不是算术平均：算术平均会让"日K 90 分 + 分时 0 分"拿到 45 分，
        排在"两者都 45 分"前面 —— 但那明显不是用户要的"两个都高"。
        几何平均对"有一个很低"惩罚极重（90×0 → 0），与用户口径一致。
        """
        left = max(0.0, float(daily_score or 0.0))
        right = max(0.0, float(intraday_score or 0.0))
        return (left * right) ** 0.5


# ============================================================================
# 生产装配：真实候选池 + 真实打分器（把 service / 仓库接进来）
# ============================================================================


class WatchlistAutoSelector(AutoSelector):
    """接入真实数据源的自动选股器。

    三组候选：
      1. **热门股前 50**：近 N 日成交额最高的票（"钱最多的地方"）；
      2. **昨日涨停**：仓库自算（`daily.high >= stk_limit.up_limit`）；
      3. **昨日成交额前 200**：与 1 本质同类但阈值更高，实际是并集。

    打分器直接复用 `IntradayService` 的既有能力（日K快照 + 分时快照），
    不另起一套口径 —— 否则"自动选出来的票"和"手动点开看到的分数"会不一致。
    """

    def __init__(self, service: Any, *, warehouse: Any = None, **kwargs: Any) -> None:
        self._service = service
        self._warehouse = warehouse
        # 名录缓存：(交易日, {code: name})。全市场 5000+ 行，交易日里不会变，
        # 没必要每轮选股重查一遍（详见 `_name_map` 的说明）。
        self._name_cache: tuple[str, dict[str, str]] | None = None
        super().__init__(pool_provider=self._build_pool,
                         daily_scorer=self._score_daily,
                         intraday_scorer=self._score_intraday, **kwargs)

    # ---------------- 候选池 ----------------

    async def _build_pool(self, hot: int, amount: int) -> dict[str, list[str]]:
        """三组并集 → `{code: [name, 组1, 组2...]}`。"""
        import asyncio

        pool: dict[str, list[str]] = {}

        def add(code: str, name: str, group: str) -> None:
            key = str(code).zfill(6)
            if key not in pool:
                pool[key] = [name, group]
            elif group not in pool[key]:
                pool[key].append(group)

        # ① 成交额榜（一次读仓库，同时满足"热门前 50"与"成交额前 200"）
        rows = await asyncio.to_thread(self._top_amount, max(hot, amount))
        for index, (code, name, _amount) in enumerate(rows):
            if index < hot:
                add(code, name, f"热门前{hot}")
            add(code, name, f"成交额前{amount}")

        # ② 昨日涨停（仓库自算，确定性口径）
        try:

            provider = self._warehouse_provider()
            limit_up = await asyncio.to_thread(provider.limit_up_codes_from_warehouse)
        except Exception as exc:  # noqa: BLE001 涨停池失败不阻断另外两组
            logger.info("自动选股：昨日涨停取数失败（%s）", brief(exc, BRIEF_TIGHT))
            limit_up = {}
        if limit_up:
            names = await self._names(list(limit_up))
            for code in limit_up:
                add(code, names.get(str(code), ""), "昨日涨停")

        logger.info("自动选股候选池：%d 只（成交额 %d / 涨停 %d）",
                    len(pool), len(rows), len(limit_up))
        return pool

    def _warehouse_provider(self) -> Any:
        """复用资金流 provider 的仓库读取能力（不重复造轮子）。"""
        if self._warehouse is None:
            from src.fundflow.provider import FundFlowProvider

            self._warehouse = FundFlowProvider()
        return self._warehouse

    def _top_amount(self, size: int) -> list[tuple[str, str, float]]:
        """近 N 日成交额合计最高的票（读本地仓库，零网络）。"""

        provider = self._warehouse_provider()
        client = provider._warehouse_client()  # noqa: SLF001 同项目内复用
        if client is None:
            return []
        try:
            frame = client.load("daily", start=_recent_start(), end=_today())
        except Exception as exc:  # noqa: BLE001
            logger.info("成交额榜读取失败：%s", brief(exc, BRIEF_TIGHT))
            return []
        if frame is None or len(frame) == 0 or "amount" not in frame.columns:
            return []
        frame = frame.copy()
        frame["code"] = frame["code"].astype(str).str.zfill(6)
        totals = (frame.groupby("code", as_index=False)["amount"].sum()
                  .sort_values("amount", ascending=False).head(size))
        names = self._name_map()
        # 纯列取数，不逐行构造 Series（`iterrows` 在 5000+ 行时会把事件循环按住，
        # 见 `_name_map` 的实测记录）。
        return [
            (str(code), names.get(str(code), ""), float(amount or 0.0))
            for code, amount in zip(totals["code"].tolist(),
                                    totals["amount"].tolist(), strict=False)
        ]

    def _name_map(self) -> dict[str, str]:
        """代码→名称（仓库名录；不可用返回空 dict，不猜）。

        ## 为什么不用 `iterrows()`

        原来是 `{row["code"]: row["name"] for _, row in frame.iterrows()}`。
        名录是**全市场 5000+ 行**，`iterrows()` 每行都会重新构造一个 Series
        （dtype 归一到 object）—— 实测它在事件循环里被观察到是启动后
        **111.8 秒**卡顿的现场之一（线程转储：`frame.py iterrows →
        string_arrow._from_sequence`）。

        两步修：
          1. 纯列取数（`frame["code"].tolist()`），不做逐行 Series；
          2. 名录按**当天**缓存：交易日里它不会变，没必要每轮选股重查一遍。
        """
        today = datetime.now().strftime("%Y%m%d")
        cached = self._name_cache
        if cached is not None and cached[0] == today:
            return cached[1]
        mapping = self._load_name_map()
        self._name_cache = (today, mapping)
        return mapping

    def _load_name_map(self) -> dict[str, str]:
        """真正读库的那一步（抽出便于单测注入，不碰网络/子进程）。"""
        provider = self._warehouse_provider()
        client = provider._warehouse_client()  # noqa: SLF001
        if client is None:
            return {}
        try:
            frame = client._read_sql(  # noqa: SLF001
                "SELECT code, name FROM quant_stock_directory", [])
        except Exception:  # noqa: BLE001
            return {}
        if frame is None or len(frame) == 0 or "code" not in frame.columns:
            return {}
        codes = frame["code"].astype(str).str.zfill(6).tolist()
        names = (frame["name"].astype(str).tolist()
                 if "name" in frame.columns else [""] * len(codes))
        return {code: (name if name != "nan" else "")
                for code, name in zip(codes, names, strict=False)}

    async def _names(self, codes: list[str]) -> dict[str, str]:
        # `_name_map()` 是**同步**读库（且首次要全表 5000+ 行）。这里必须丢到
        # 线程里，否则它会按住事件循环 —— 实测盘后自动选股在启动后立刻跑，
        # 期间 `/health` 排队 111.8 秒。
        mapping = await asyncio.to_thread(self._name_map)
        return {code: mapping.get(str(code).zfill(6), "") for code in codes}

    # ---------------- 打分器 ----------------

    async def _score_daily(self, code: str) -> tuple[float, list[str]] | None:
        """日K打分 + 触发的**买入**信号代码列表（复用服务层既有口径）。

        用 `service.daily(code)` —— 与前端「日K做T」页签**同一个方法**，
        因此"自动选出来的票"与"手动点开看到的分数"必然一致（不会有两套口径）。
        """
        snapshot = await self._service.daily(code)
        if snapshot is None or not getattr(snapshot, "available", False):
            return None
        score = 0.0
        scorecard = getattr(snapshot, "scorecard", None)
        if scorecard is not None:
            score = float(getattr(scorecard, "total", 0.0) or 0.0)
        signals: list[str] = []
        for item in (getattr(snapshot, "buy_signals", None) or []):
            if getattr(item, "triggered", False):
                signals.append(str(getattr(item, "code", "")))
        return score, signals

    async def _score_intraday(self, code: str) -> float | None:
        """分时多指标合成打分（0~100，与面板上「低吸」同一口径）。"""
        snapshot = await self._service.snapshot(code, light=True)
        if snapshot is None:
            return None
        scorecard = getattr(snapshot, "scorecard", None)
        if scorecard is None:
            return None
        return float(getattr(scorecard, "total", 0.0) or 0.0)


def _today() -> str:
    """今天（紧凑格式），供仓库区间查询。"""
    return datetime.now().strftime("%Y%m%d")


def _recent_start(days: int = 20) -> str:
    """近 N 个自然日（够覆盖"近 10 个交易日"的成交额口径）。"""
    from datetime import timedelta

    return (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
