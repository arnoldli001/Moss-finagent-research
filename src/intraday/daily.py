"""日K级别做T编排（取日线 → 量价规则 → 信号 → 快照模型）。

数据来源**复用项目既有日线采集链**（QMT → 本地CSV → AkShare，经 ConnectorRouter），
与做T模块的日线上下文同源，不额外造取数逻辑。

输出 `DailySnapshot` 供前端绘制：
  - 日K线（含量柱标记，前端画蜡烛图 + 量柱）
  - 高量柱锚点（安全线=实顶 / 风险线=实底）与当前状态
  - 最近一日量价形态（16形态矩阵）
  - 位置量化（低位/中位/高位）
  - 主力成本线五形态
  - B1–B15 买入信号 / S1–S6 卖出风控（逐条条件明细）
  - S5 保护线体系 + S6 高量纪律
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from src.core.errors import (
    BRIEF_DEFAULT,
    BRIEF_TIGHT,
    brief,
)
from src.core.exceptions import DataFetchError
from src.core.trading_session import POST_CLOSE, minutes_of
from src.intraday.config import IntradayConfig
from src.intraday.daily_signals import build_verdict, run_daily_signals
from src.intraday.models import (
    DailyBar,
    DailySignalMark,
    DailySnapshot,
    DataHealth,
    SourceAttempt,
)
from src.intraday.sources import SOURCE_LABELS, close_indicator, daily_bars_from_points
from src.intraday.volume import (
    DailyContext,
    enrich_daily_frame,
    find_cost_lines,
    find_volume_anchors,
    high_volume_discipline,
    protection_lines,
    quantify_position,
)

# 日线回溯的自然日数：500 个自然日 ≈ 340 根交易日，覆盖
# lookback_days(默认250) + 绘图120 + 缓冲60，保证显式区间不会把样本切少。
_DAILY_LOOKBACK_DAYS = 500
# 买卖标记回放的bar数（用户口径：最近 30 个交易日）。
# 为什么不跟绘图bar数（120）走：回放要对每根bar重跑 22 个信号函数 + 高量柱/成本线，
# 实测 30 根 ≈ 0.46s、120 根 ≈ 1.8s；30 根足够复盘一个月的操作点，
# 而且日K快照本身有 180 秒缓存，这点开销可以接受。
_SIGNAL_REPLAY_BARS = 30

logger = logging.getLogger(__name__)

DISCLAIMER = (
    "⚠️ 本页为量价规则引擎的机械计算结果，规则源自主力行为学经验归纳，"
    "参数需按标的回测标定后使用；依赖 Level-2/竞价数据的规则仅给出日线可复现部分。"
    "不构成投资建议，据此操作风险自负。"
)


def _bar_from_row(row: Any) -> DailyBar:
    def number(key: str) -> float:
        value = row.get(key)
        try:
            result = float(value)
        except (TypeError, ValueError):
            return 0.0
        return 0.0 if result != result else result

    def optional(key: str) -> float | None:
        value = row.get(key)
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        return None if result != result else result

    return DailyBar(
        date=str(row.get("date") or row.get("ts") or ""),
        open=number("open"), high=number("high"), low=number("low"),
        close=number("close"), volume=number("volume"), amount=number("amount"),
        pct_chg=optional("pct_chg"), amplitude=optional("amplitude"),
        turnover=optional("turnover"),
        is_high_volume=bool(row.get("is_high_volume", False)),
        is_double_volume=bool(row.get("is_double_volume", False)),
        is_shrink_volume=bool(row.get("is_shrink_volume", False)),
        is_shrink_half=bool(row.get("is_shrink_half", False)),
        is_ladder_down=bool(row.get("is_ladder_down", False)),
        is_flat_volume=bool(row.get("is_flat_volume", False)),
        is_ground_volume=bool(row.get("is_ground_volume", False)),
        is_explode_volume=bool(row.get("is_explode_volume", False)),
        is_long_lower_shadow=bool(row.get("is_long_lower_shadow", False)),
        is_big_yang=bool(row.get("is_big_yang", False)),
        is_big_yin=bool(row.get("is_big_yin", False)),
        body_top=number("body_top"), body_bottom=number("body_bottom"),
        ma5=optional("ma5"), ma10=optional("ma10"),
        ma20=optional("ma20"), ma60=optional("ma60"),
    )


def analyse_daily(
    *, code: str, name: str, points: list[Any], config: IntradayConfig,
    attempts: list[SourceAttempt] | None = None,
    gaps: list[str] | None = None,
    chart_bars: int = 120,
    cycle: Any = None,
    character: Any = None,
    instrument: str = "",
) -> DailySnapshot:
    """由日线 DataPoint 列表生成日K做T快照（纯函数，便于单测）。

    `cycle`（市场情绪周期）与 `character`（个股股性画像）是运行时取到的外部上下文，
    以可选参数注入：缺席时对应的加权因子记为「不可用 + 缺口」，
    不会阻断其余六个因子出分。

    `instrument`：标的类别（`stock` / `etf` / `index` / `sector`），
    决定擒牛线用哪套公式。留空时由 `niuline.select_variant` 按代码段与名称推断
    —— 调用方**明确知道**类别时请显式传（000001 这类歧义代码只能靠它兜住）。

    注意 `character=None` **不等于**"股性不可用"：本函数会用**自己这份日线**
    （`enriched`）现算一份（见 `_daily_character`）。
    只有显式传入一个 unavailable 的画像时才会记缺口。
    """
    gaps = list(gaps or [])
    attempts = list(attempts or [])
    frame = daily_bars_from_points(points)
    if frame.empty:
        return DailySnapshot(
            available=False, code=code, name=name, gaps=gaps,
            health=DataHealth(attempts=attempts, gaps=gaps + ["日线数据为空"]),
            disclaimer=DISCLAIMER)
    frame = frame.rename(columns={"ts": "date"})
    if len(frame) < 30:
        gaps.append(f"日线样本仅 {len(frame)} 根（<30），量价规则可靠性不足")

    params = config.daily
    # 性能：量柱标记是逐bar的Python循环，全历史（数千根）要跑数秒。
    # 所有规则最多只回看 lookback_days（默认250）根，绘图只要120根，
    # 因此先切尾部再标记 —— 实测把日K分析从 7~14s 降到 1s 以内。
    keep = max(params.lookback_days, chart_bars) + 60
    if len(frame) > keep:
        frame = frame.tail(keep).reset_index(drop=True)
    enriched = enrich_daily_frame(frame, params)
    # 日线新鲜度检查：收盘后数据源可能还没补上今日bar。
    # 不说清楚的话，用户会以为分析的是今天（实测踩过：收盘后仍在用昨日bar）。
    last_date = str(enriched["date"].iloc[-1])
    today = datetime.now().strftime("%Y-%m-%d")
    if last_date != today:
        now = datetime.now()
        if now.weekday() < 5 and minutes_of(now) >= POST_CLOSE:
            gaps.append(
                f"日线最新为 {last_date}，尚未包含今日（{today}）——"
                "日线源在收盘后需一段时间才更新；本页结论基于上一交易日，"
                "可稍后用「强制刷新」重试")
        else:
            gaps.append(f"日线最新为 {last_date}（非当日，按最近交易日分析）")
    ctx = DailyContext(
        frame=enriched, params=params, index=len(enriched) - 1,
        anchors=find_volume_anchors(enriched, params),
        pattern=None, position=quantify_position(enriched),
        cost_lines=find_cost_lines(enriched, params))
    from src.intraday.volume import classify_volume_price

    ctx.pattern = classify_volume_price(enriched, ctx.index, params)
    signals = run_daily_signals(ctx)
    discipline = high_volume_discipline(enriched, ctx.anchors, params)
    protective = protection_lines(enriched, params)
    # 股性画像必须用**本面板同一份日线**来算：
    # 服务层若另外取一份（走的是不带区间的 TTL/DB 短路，日期与这里可能不同），
    # 就会出现"图上是 8/31 的K线，右侧股性却按 9/16 的数据算"这种口径分裂。
    if character is None:
        character = _daily_character(enriched, code=code, name=name)
    ma_values = {}
    for period in (params.ma_short, params.ma_long,
                   params.ma_trend_short, params.ma_trend_long):
        series = enriched["close"].rolling(period).mean()
        value = series.iloc[-1]
        ma_values[f"MA{period}"] = (
            None if value != value else round(float(value), 3))

    tail = enriched.tail(chart_bars)
    bars = [_bar_from_row(row) for _, row in tail.iterrows()]
    trade_date = str(enriched["date"].iloc[-1])
    verdict = build_verdict(
        signals["buy"], signals["sell"], discipline, ctx.pattern, ctx.position)

    # ---- 擒牛线（日K做T主图的档位线体系，用户 2026-09-18 提供的同花顺公式）----
    # 两套公式按标的类别自动选：个股走 AMOUNT 口径、指数/ETF/板块走 C*V 口径
    # （唯一差别在 CBX；选错会让成本线系统性偏移，见 src/intraday/niuline.py）。
    # 必须用**与图同一批 bars**（tail）来算，否则线与蜡烛错位。
    niuline_set = None
    try:
        from src.intraday.models import NiuLinePoint, NiuLineSet
        from src.intraday.niuline import LINE_META, build_series

        series = build_series(code, bars, instrument=instrument, name=name)
        points = [
            NiuLinePoint(
                date=str(bar.date),
                **{key: series.series[key][index]
                   for key in series.series if index < len(series.series[key])})
            for index, bar in enumerate(bars)
        ]
        niuline_set = NiuLineSet(
            variant=series.variant, reason=series.reason,
            price_basis=series.price_basis, cbx_scale=series.cbx_scale,
            n=series.n, m=series.m,
            latest=dict(series.latest), notes=list(series.notes), points=points,
            lines=[{"key": key, "label": label, "note": note}
                   for key, label, note in LINE_META])
    except ImportError:  # 公开版不含擒牛线公式，属预期
        gaps.append("擒牛线（主图档位线）为商业版功能，开源版未包含")
    except Exception as exc:  # noqa: BLE001 主图线失败不能拖垮整个日K面板
        gaps.append("擒牛线计算失败（主图档位线不可用，其余面板不受影响）："
                    + brief(exc, BRIEF_DEFAULT))

    from src.intraday.models import ProtectiveLines

    # 最近 N 个交易日的买卖标记（逐bar因果回放，供K线图打点）
    history = replay_daily_signals(enriched, params, bars=_SIGNAL_REPLAY_BARS)

    # ---- 日线做T加权决策总分（七因子，与规则信号并列而非替代）----
    # 结构/权重/阈值全部可前端按个股覆盖（见 weight-profiles 接口），
    # 这里只负责把已算好的中间量喂进合成器，不重复计算任何指标。
    scorecard = None
    try:
        from src.intraday.daily_score import compose_daily_scorecard

        scorecard = compose_daily_scorecard(
            code=code, frame=enriched, ma=ma_values, position=ctx.position,
            anchors=ctx.anchors, pattern=ctx.pattern,
            buy_signals=signals["buy"], sell_signals=signals["sell"],
            protective=protective, cycle=cycle, character=character,
            chan_structure=_daily_chan(enriched, config),
            weights=config.daily_weights.as_dict(),
            action=config.daily_thresholds.action,
            hint=config.daily_thresholds.hint,
            limit_pct=_limit_up_pct(code))
    except Exception as exc:  # noqa: BLE001 加权总分失败不能拖垮整个日K面板
        gaps.append(f"日线做T加权总分计算失败（其余面板不受影响）：{brief(exc, BRIEF_DEFAULT)}")
    if scorecard is not None:
        gaps.extend(scorecard.gaps)

    return DailySnapshot(
        available=True, code=code, name=name, trade_date=trade_date,
        generated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        bars=bars,
        anchors=ctx.anchors[:6],
        active_anchor=ctx.anchors[0] if ctx.anchors else None,
        pattern=ctx.pattern, position=ctx.position,
        cost_lines=ctx.cost_lines,
        buy_signals=signals["buy"], sell_signals=signals["sell"],
        signal_history=history,
        protective=ProtectiveLines(
            stop_line=protective.get("stop_line"),
            stop_basis=protective.get("stop_basis", ""),
            trail_line=protective.get("trail_line"),
            trail_basis=protective.get("trail_basis", ""),
            ma20=protective.get("ma20"), ma60=protective.get("ma60"),
            broken_stop=bool(protective.get("broken_stop")),
            broken_trail=False,
            note=protective.get("note", "")),
        ma=ma_values, discipline=discipline,
        scorecard=scorecard,
        niuline=niuline_set,
        verdict=verdict,
        health=DataHealth(
            chosen_daily_source=SOURCE_LABELS["router"],
            attempts=attempts, gaps=gaps, trade_date=trade_date),
        config_snapshot={
            "daily_params": params.model_dump(),
            "daily_weights": config.daily_weights.as_dict(),
            "daily_thresholds": {
                "action": config.daily_thresholds.action,
                "hint": config.daily_thresholds.hint,
            },
            # 前端按这个周期轮询：日线bar在盘中由逐笔驱动、按分钟更新，
            # 取「数据源更新周期的 3 倍」= 3 分钟（服务端缓存同值，见 daily_snapshot_ttl）。
            "refresh_seconds": config.data.daily_snapshot_ttl,
        },
        disclaimer=DISCLAIMER)



def replay_daily_signals(
    enriched: pd.DataFrame, params: Any, *, bars: int = 30,
) -> list[DailySignalMark]:
    """对最近 `bars` 根bar**逐bar因果回放**日K买卖信号，返回打点用的标记列表。

    ## 为什么要回放
    `analyse_daily` 只对**最后一根**bar 跑一遍信号，所以K线图上只能标出"此刻"
    触发与否，看不到"最近一个月什么时候给过买卖点" —— 而复盘/验证战法恰恰要看后者。

    ## 因果性（无未来函数）怎么保证
    对第 i 根bar，只把 `frame.iloc[: i + 1]` 交给规则：高量柱锚点、位置分位、
    主力成本线**全部按该bar及之前的数据重算**。这样不会出现"用后面才出现的高量柱
    去解释前面的买点"这种自欺欺人的标记 —— 与项目对回测口径的要求一致。

    只保留**触发**的项（未触发的是"差在哪"，属于当前bar的明细展示，
    由 `buy_signals`/`sell_signals` 承担）。同一信号**连续触发只记首次**
    —— 实测某只票 S6「高量不破」在 30 根里触发 23 次，逐根打点会糊成一片，
    而"它第一次是什么时候出现的"才是有用的信息。
    """
    marks: list[DailySignalMark] = []
    if len(enriched) < 2:
        return marks
    from src.intraday.volume import classify_volume_price

    start = max(1, len(enriched) - max(1, bars))
    previous_keys: set[tuple[str, str]] = set()
    for index in range(start, len(enriched)):
        window = enriched.iloc[: index + 1]
        try:
            ctx = DailyContext(
                frame=window, params=params, index=len(window) - 1,
                anchors=find_volume_anchors(window, params),
                pattern=None, position=quantify_position(window),
                cost_lines=find_cost_lines(window, params))
            ctx.pattern = classify_volume_price(window, ctx.index, params)
            signals = run_daily_signals(ctx)
        except Exception as exc:  # noqa: BLE001 单根bar异常不该打断整张图
            logger.debug("信号回放失败(第%d根)：%s", index, brief(exc, BRIEF_TIGHT))
            continue
        date = str(window["date"].iloc[-1])
        price = _optional_float(window["close"].iloc[-1])
        fired: set[tuple[str, str]] = set()
        emitted: list[DailySignalMark] = []
        for item in signals["buy"]:
            if item.triggered and item.kind == "buy":
                fired.add(("buy", item.code))
                emitted.append(DailySignalMark(
                    date=date, side="buy", code=item.code, name=item.name,
                    price=price, entry=item.entry, stop_loss=item.stop_loss))
        for item in signals["sell"]:
            if not item.triggered or item.kind not in ("sell", "risk"):
                continue
            side = "sell" if item.kind == "sell" else "risk"
            fired.add((side, item.code))
            emitted.append(DailySignalMark(
                date=date, side=side, code=item.code, name=item.name,
                price=price, entry=item.entry, stop_loss=item.stop_loss))
        # 连续触发去重：与上一根bar相同的信号不重复打点
        for mark in emitted:
            if (mark.side, mark.code) not in previous_keys:
                marks.append(mark)
        previous_keys = fired
    return marks


def _optional_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if result != result else result       # NaN → None


async def fetch_daily_snapshot(
    code: str, name: str, config: IntradayConfig, backend: Any,
    *, lookback_days: int | None = None,
    cycle: Any = None, character: Any = None,
) -> DailySnapshot:
    """从项目采集链取日线并分析（失败即如实记缺口）。

    `cycle` 由服务层取好后注入（情绪周期是市场级数据，自选池共用一份）；
    `character` 一般传 None，由 `analyse_daily` 用**本函数取到的同一份日线**现算 ——
    服务层另外取一份会因为两条路径的缓存口径不同而拿到不同末日（见 `_daily_character`）。
    """
    attempts: list[SourceAttempt] = []
    gaps: list[str] = []
    if backend is None:
        return DailySnapshot(
            available=False, code=code, name=name,
            gaps=["未注入数据采集链"], health=DataHealth(gaps=["未注入数据采集链"]),
            disclaimer=DISCLAIMER)
    started = datetime.now()
    # **显式给日期区间**：ConnectorRouter 在指定 start/end 时会跳过进程内 TTL 与
    # 本地 DB 短路（见 `router._cached_fetch`），这正是日K做T需要的：
    #   - 日线指标默认 4 小时 TTL（`_TTL_BY_PREFIX`）；
    #   - DB 里的"昨天日线"在 `DataFreshnessEvaluator` 里只是 lagging（conf≥0.4），
    #     被判为**可用** → 直接返回，永不穿透到网络。
    # 实测后果：盘中日K面板一直显示上一交易日的日线（用户报"自选股也是昨天的数据"）。
    # 取 500 个自然日 ≈ 340 根交易日，覆盖 lookback_days(250) + 绘图(120) + 缓冲。
    end_date = started.strftime("%Y-%m-%d")
    start_date = (started - timedelta(days=_DAILY_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    try:
        points = await backend.fetch(
            close_indicator(code), start_date=start_date, end_date=end_date)
    except DataFetchError as exc:
        attempts.append(SourceAttempt(
            source=SOURCE_LABELS["router"], ok=False, detail=brief(exc, BRIEF_DEFAULT)))
        gaps.append(f"日线取数失败：{brief(exc, BRIEF_DEFAULT)}")
        return DailySnapshot(
            available=False, code=code, name=name, gaps=gaps,
            health=DataHealth(attempts=attempts, gaps=gaps),
            disclaimer=DISCLAIMER)
    except Exception as exc:  # noqa: BLE001
        attempts.append(SourceAttempt(
            source=SOURCE_LABELS["router"], ok=False, detail=brief(exc, BRIEF_DEFAULT)))
        gaps.append(f"日线取数异常：{brief(exc, BRIEF_DEFAULT)}")
        return DailySnapshot(
            available=False, code=code, name=name, gaps=gaps,
            health=DataHealth(attempts=attempts, gaps=gaps),
            disclaimer=DISCLAIMER)
    attempts.append(SourceAttempt(
        source=SOURCE_LABELS["router"], ok=bool(points), rows=len(points or []),
        detail="日线OHLCV（复用项目QMT→本地CSV→AkShare三级采集链）",
        latency_ms=int((datetime.now() - started).total_seconds() * 1000)))
    return analyse_daily(
        code=code, name=name, points=points or [], config=config,
        attempts=attempts, gaps=gaps, cycle=cycle, character=character)



def _daily_chan(frame: Any, config: IntradayConfig) -> Any:
    """日线表 → 缠论事实（列名对齐 chan.py 的 ts/open/high/low/close/volume）。

    日线表在 `analyse_daily` 里已把 `ts` 改名成 `date`，这里改回来；
    与分时链路共用 `chan_facts.extract_chan_facts`，保证两个面板的中枢/背驰同口径。
    """
    from src.intraday.chan_facts import extract_chan_facts

    if frame is None:
        return None
    bars = frame.rename(columns={"date": "ts"})
    return extract_chan_facts(bars, config)


def _daily_character(frame: Any, *, code: str, name: str) -> Any:
    """用**本面板的日线**算个股股性画像（mode="daily"）。

    为什么在这里算而不是让服务层另外取一份：
    日K面板的日线带显式日期区间（绕开 TTL/DB 短路，见 §11.3），
    而服务层的轻量取数走短路口径 —— 两条路在数据源落后时拿到的**末日不同**
    （实测 QMT 未启动时分别是 2026-08-31 与 2026-09-16）。
    若各算各的，就会出现"图上是 8/31 的K线、右侧股性却按 9/16 的数据算"的口径分裂。
    """
    from src.intraday.character import analyze_character

    if frame is None:
        return None
    bars = frame.rename(columns={"date": "ts"})
    return analyze_character(bars, code=code, name=name, mode="daily")


def _limit_up_pct(code: str) -> float:
    """该代码的涨停幅度（%）——与 `character.limit_up_pct_for` 同一口径。"""
    from src.intraday.character import limit_up_pct_for

    return limit_up_pct_for(code)

