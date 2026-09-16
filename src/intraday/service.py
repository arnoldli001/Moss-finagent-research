"""做T辅助编排服务（前端四个面板的唯一数据入口）。

一次 snapshot() 的完整链路：
  行情（分钟bars/分时/快照，多源容灾）
    → 因果指标特征（VWAP按日重置；BOLL/MACD/KDJ/RSI连续序列）
    → 关键价位（箱体上下沿 + 布林融合 + 止损硬约束）
    → 七因子打分（权重合计100）→ 总分 → 阈值信号（±20/±30）
    → 分时图三角标记（逐bar重放，与实时打分同源）
  并行装配：日线上下文、关联板块快照与分时、大盘状态、估值空间、消息面NLP
    → 推送（仅正式信号/止损，多渠道 + 冷却）

工程约定：
- 所有子链路独立失败互不阻断，原因进入 health.gaps（绝不静默用模拟数据）；
- 各子 provider 自带 TTL 缓存，服务层只做并发编排；
- 结论一律附免责声明。
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from src.core.exceptions import DataFetchError
from src.intraday import indicators as ind
from src.intraday.backtest import DISCLAIMER as BACKTEST_DISCLAIMER
from src.intraday.backtest import run_threshold_backtest
from src.intraday.board import BoardContextProvider
from src.intraday.chan_facts import ChanFacts, extract_chan_facts
from src.intraday.character import CharacterProfile, analyze_character
from src.intraday.config import (
    IntradayConfig,
    WatchConfig,
    load_intraday_config,
)
from src.intraday.engine import compute_levels, replay_levels, run_engine
from src.intraday.factors import (
    FactorContext,
    cross_bonus_for,
    macd_score_from_gap,
    osc_score_from_jr,
    sentiment_score_from,
)
from src.intraday.features import (
    build_intraday_features,
    build_oscillator_features,
    day_level_constants,
    replay_totals,
    resample_bars,
)
from src.intraday.market_cycle import MarketCycle, MarketCycleProvider
from src.intraday.models import (
    BacktestResult,
    Bar,
    DataHealth,
    IntradaySnapshot,
    LevelPoint,
    NewsSentiment,
    Quote,
    ScorePoint,
    SentimentPanel,
    SignalMarker,
    SourceAttempt,
    TrendPoint,
    ValuationSpace,
    WatchItem,
)
from src.intraday.notifier import SignalNotifier
from src.intraday.sentiment import NewsSentimentAnalyzer
from src.intraday.sources import (
    SOURCE_LABELS,
    IntradayDataProvider,
    close_indicator,
    daily_bars_from_points,
)
from src.intraday.valuation import ValuationProvider

logger = logging.getLogger(__name__)

DEFAULT_INDEX = "000001"
_INTRADAY_DAYS = 6  # 5分钟上下文天数（腾讯单次接口上限约320根）
# 权重档案的进程内缓存秒数：档案是低频变更的用户数据，
# 但自选池每分钟会给每只票各跑一次快照，不缓存就是 N 次 SQLite 读。
_PROFILE_CACHE_TTL = 5.0

_SESSION_LABELS = {
    "pre_open": "盘前", "call_auction": "集合竞价", "trading": "交易中",
    "lunch_break": "午间休市", "closed": "已收盘",
}


def session_state(now: datetime | None = None) -> tuple[str, str]:
    """当前交易时段（北京时间口径）→ (状态码, 中文标签)。"""
    moment = now or datetime.now()
    if moment.weekday() >= 5:
        return "closed", "周末休市"
    minutes = moment.hour * 60 + moment.minute
    if minutes < 9 * 60 + 15:
        return "pre_open", _SESSION_LABELS["pre_open"]
    if minutes < 9 * 60 + 30:
        return "call_auction", _SESSION_LABELS["call_auction"]
    if minutes <= 11 * 60 + 30:
        return "trading", _SESSION_LABELS["trading"]
    if minutes < 13 * 60:
        return "lunch_break", _SESSION_LABELS["lunch_break"]
    if minutes <= 15 * 60:
        return "trading", _SESSION_LABELS["trading"]
    return "closed", _SESSION_LABELS["closed"]


# ==================== 自选池自动刷新窗口 ====================

# 用户口径：集合竞价 09:15 开始，到 11:30；下午 13:00 到 15:00。
# 非盘中（盘前/午休/收盘后/周末/节假日）完全不取数 —— 那些时段行情不会变，
# 每分钟重算 N 只标的只是白白消耗数据源配额与 CPU。
_WATCH_REFRESH_WINDOWS = ((9 * 60 + 15, 11 * 60 + 30), (13 * 60, 15 * 60))
# 集合竞价开始到开盘之间，市场时钟（当天第一笔成交前 tick 的 timetag）可能还停在
# 上一交易日，此时**不能**据此判定"今天休市"，否则 09:15~09:30 会被整段跳过。
_MORNING_OPEN_MINUTE = 9 * 60 + 30


def watchlist_refresh_window(now: datetime | None = None) -> tuple[bool, str]:
    """当前是否处于「自选池自动刷新」时段 → (是否刷新, 人话原因)。

    判定用**本机日历时间**（周末直接排除），再用**市场自己的时钟**排除节假日：
    春节/国庆等「工作日但休市」的日期里，行情不会变，刷新只是空转；
    而市场时钟取不到时（QMT 未启动）仍然照常刷新 —— 那种情况正是要靠
    腾讯/新浪兜底取数的时候，不能因为 QMT 掉了就把自动刷新一起停掉。

    原因文案会原样透给前端，用户能直接看到"为什么现在没在自动刷新"。
    """
    moment = now or datetime.now()
    if moment.weekday() >= 5:
        return False, "周末休市"
    minutes = moment.hour * 60 + moment.minute
    morning_start, morning_end = _WATCH_REFRESH_WINDOWS[0]
    afternoon_start, _afternoon_end = _WATCH_REFRESH_WINDOWS[1]
    if minutes < morning_start:
        return False, f"盘前（{morning_start // 60:02d}:{morning_start % 60:02d} 开始自动刷新）"
    if minutes > morning_end and minutes < afternoon_start:
        return False, "午间休市"
    if minutes > _WATCH_REFRESH_WINDOWS[1][1]:
        return False, "已收盘"
    if minutes >= _MORNING_OPEN_MINUTE:
        from src.intraday.sources import live_session_date

        session = live_session_date()
        if session and session != moment.strftime("%Y-%m-%d"):
            return False, f"非交易日（市场时钟停在 {session}）"
    return True, "盘中自动刷新中"


def _seconds_until_next_tick(interval: float,
                             now: datetime | None = None) -> float:
    """到下一个刷新时刻的秒数：对齐整分钟刻度 **+2 秒**。

    为什么要对齐刻度：分钟bar在整分钟之后才生成，若在 xx:59.8 取数，拿到的是
    上一根bar，等于整个刷新周期都在看落后一分钟的数据。多等 2 秒让新bar先落地。
    为什么要对齐而不是简单 sleep(interval)：漂移会累积（每次多花 1 秒取数，
    十分钟后就偏了一分钟），对齐刻度后每分钟都在同一个相位上刷新。
    """
    moment = now or datetime.now()
    step = max(1, int(interval // 60) or 1)
    base = moment.replace(second=0, microsecond=0)
    target = base + timedelta(minutes=step)
    return max(1.0, (target - moment).total_seconds() + 2.0)


class IntradayService:
    """做T辅助服务（进程内单例，挂在 Runtime 上）。"""

    def __init__(
        self,
        *,
        backend: Any = None,
        gateway: Any = None,
        news_fetcher: Any = None,
        config_path: str | None = None,
        profile_repo: Any = None,
        cycle_provider: MarketCycleProvider | None = None,
    ) -> None:
        self._backend = backend
        # 保留 LLM 网关与新闻源句柄：配置热重载时重建消息面分析器要沿用它们
        self._gateway = gateway
        self._news_fetcher = news_fetcher
        self._config_path = config_path
        self._config: IntradayConfig = load_intraday_config(config_path)
        self._data = IntradayDataProvider(self._config)
        self._board = BoardContextProvider(self._config, self._data)
        self._valuation = ValuationProvider(self._config, self._data, backend)
        self._sentiment = self._build_sentiment_analyzer(self._config)
        self._notifier = SignalNotifier(self._config)
        # 市场情绪周期（涨停池/炸板池/跌停池）：进程内单例 + TTL 缓存。
        # 自选池里 N 只票共用同一份情绪周期，不能让每只票各拉一次四池。
        self._cycle = cycle_provider or MarketCycleProvider(
            ttl_seconds=self._config.factors.cycle.ttl_seconds)
        # 权重档案仓储（数据库）。未装配时（如部分单测）所有档案接口返回空，
        # 而不是抛异常 —— 做T主链路不该因为"档案库没配"而整体不可用。
        self._profile_repo = profile_repo
        self._profile_cache: dict[str, Any] | None = None
        # 股性画像缓存：日线级特征，同一交易日内不必重算（键=代码，值=(时间戳, 画像)）
        self._character_cache: dict[str, tuple[float, CharacterProfile]] = {}
        # 自选池概览缓存 + 盘中自动刷新循环。
        # 缓存的意义：全表重算要为每只票各跑一次轻量快照（实测整表 ~1.1s 且每只票
        # 都要打一次数据源），前端每分钟取一次若都重算，就会和刷新循环重复取数。
        self._watch_cache: tuple[float, list[WatchItem]] | None = None
        self._watch_lock = asyncio.Lock()
        # 版本号：每写一次概览缓存 +1（增删自选也 +1）。WS 推送据此判断"有没有新版"，
        # 否则每 15 秒都会把同一份列表重复推给前端。
        self._watch_generation = 0
        self._refresh_task: asyncio.Task[None] | None = None
        # 后台重算（stale-while-revalidate）的进行中标记与任务强引用
        self._watch_refresh_inflight = False
        self._watch_refresh_tasks: set[Any] = set()
        self._refresh_state: dict[str, Any] = {
            "last_run_at": "", "last_seconds": 0.0,
            "last_count": 0, "last_error": "",
        }
        # 报价快车道：现价/涨跌幅每 N 秒走一次**批量**快照（QMT 9 只 0.54ms），
        # 而总分/信号仍按 watchlist_refresh_seconds 每分钟重算。两者解耦 ——
        # 用户要的"价格跟手"不该把整套打分也拉起来（那要 N 只票的 bars/trend/日线/板块）。
        self._quote_overlay: dict[str, Quote] = {}
        self._quote_generation = 0
        self._quote_task: asyncio.Task[None] | None = None
        self._quote_state: dict[str, Any] = {
            "quote_last_run_at": "", "quote_last_seconds": 0.0,
            "quote_last_count": 0, "quote_last_error": "",
        }
        # 日K快照的进程内缓存（键=标的）：前端按「数据源周期×3」轮询时避免重复计算
        self._daily_cache: dict[str, tuple[float, Any]] = {}

    @property
    def config(self) -> IntradayConfig:
        return self._config

    @property
    def notifier(self) -> SignalNotifier:
        return self._notifier

    @property
    def data_provider(self) -> IntradayDataProvider:
        """行情数据源（供路由层取证券简称等轻量用途）。"""
        return self._data

    @property
    def news_llm_stats(self) -> dict[str, Any]:
        """消息面 LLM 调用统计（做T链路唯一会调模型的地方）。

        暴露它是为了让"省了多少"可验证：`reused_calls` 是"新闻没变所以复用上次
        打分、跳过 LLM"的次数。实测依据见 `NewsSentimentAnalyzer.analyze`。
        """
        return {
            "llm_calls": int(getattr(self._sentiment, "llm_calls", 0)),
            "reused_calls": int(getattr(self._sentiment, "reused_calls", 0)),
            "cache_ttl_seconds": self._config.data.news_cache_ttl,
            "task_tier": self._config.factors.news.news_task_tier,
            "reuse_when_unchanged": self._config.factors.news.reuse_when_unchanged,
        }

    async def aclose(self) -> None:
        await self.stop_watchlist_refresh()
        await self.stop_quote_refresh()
        await self._data.aclose()
        await self._notifier.aclose()

    def _build_sentiment_analyzer(self, config: IntradayConfig,
                                  previous: Any = None) -> NewsSentimentAnalyzer:
        """按配置构造消息面分析器（热重载时沿用同一网关/新闻源句柄）。"""
        gateway = previous._gateway if previous is not None else self._gateway  # noqa: SLF001
        fetcher = (previous._news_fetcher  # noqa: SLF001
                   if previous is not None else self._news_fetcher)
        news = config.factors.news
        return NewsSentimentAnalyzer(
            gateway, fetcher, cache_ttl=config.data.news_cache_ttl,
            task_tier=news.news_task_tier, llm_retries=news.llm_retries,
            item_text_chars=news.item_text_chars,
            reuse_when_unchanged=news.reuse_when_unchanged)

    def _reload_config(self) -> IntradayConfig:
        """按 mtime 热重载配置；配置变更后重建依赖旧配置的子组件。"""
        fresh = load_intraday_config(self._config_path)
        if fresh is not self._config:
            self._config = fresh
            self._data = IntradayDataProvider(fresh)
            self._board = BoardContextProvider(fresh, self._data)
            self._valuation = ValuationProvider(fresh, self._data, self._backend)
            self._sentiment = self._build_sentiment_analyzer(
                fresh, previous=self._sentiment)
            self._notifier = SignalNotifier(fresh)
            # 自选池/权重可能被改过：缓存与已算好的分数必须作废，否则前端
            # 最长 60 秒还看到旧自选（"加了票没反应"）。
            self.invalidate_watchlist_cache()
            logger.info("做T配置已热重载，子组件已重建")
        return self._config

    # ==================== 自选池增删（前端可操作） ====================

    def _lookup_name(self, code: str) -> str:
        """从本地股票字典取中文名（取不到返回空串，**不退回代码**）。

        字典查不到时会按需从东财补录一次（ETF/指数不在 Tushare stock_basic 里），
        任何异常都不该阻断"加自选"这个动作本身。
        """
        try:
            from src.quant.stock_directory import stock_directory

            return stock_directory().name_of(code)
        except Exception as exc:  # noqa: BLE001 字典不可用不影响加自选
            logger.debug("解析 %s 的名称失败：%s", code, str(exc)[:80])
            return ""

    def add_watch(
        self, code: str, *, name: str = "", boards: list[str] | None = None,
        peers: list[str] | None = None, industry: str = "",
        overseas: list[str] | None = None,
    ) -> IntradayConfig:
        """新增/更新自选标的并写回 configs/intraday.yaml（注释保留）。

        **名称缺失时用本地股票字典补齐**（不再退回代码本身）。

        这里曾经有过一次真实事故：前端把"当前已加载快照里的名称"发过来，
        用户没先加载这只票时名称就是空，于是配置里被写成 `name: "603083"` ——
        界面上自选股显示一排代码，认不出是什么公司，而且**看起来像正常数据**，
        不会有人报错。名称是给人看的第一信息，不能让它静默退化成代码。
        """
        from src.intraday.config import WatchConfig, upsert_watch

        code = str(code).strip().split(".")[0].zfill(6)
        resolved = (name or "").strip()
        if not resolved or resolved == code:
            resolved = self._lookup_name(code)
        item = WatchConfig(
            code=code, name=resolved, boards=list(boards or []),
            peers=list(peers or []), industry=industry,
            overseas=list(overseas or []))
        saved = upsert_watch(item, self._config_path)
        self._config = saved
        self._reset_components(saved)
        return saved

    def remove_watch(self, code: str) -> IntradayConfig:
        """从自选池移除标的并写回 configs/intraday.yaml（幂等）。"""
        from src.intraday.config import remove_watch as remove_from_yaml

        saved = remove_from_yaml(code, self._config_path)
        self._config = saved
        self._reset_components(saved)
        return saved

    def _reset_components(self, config: IntradayConfig) -> None:
        """自选池变更后重建依赖配置的子组件（保住数据源连接池与缓存）。"""
        self._config = config
        self._data = IntradayDataProvider(config)
        self._board = BoardContextProvider(config, self._data)
        self._valuation = ValuationProvider(config, self._data, self._backend)
        self._notifier = SignalNotifier(config)
        # 自选池变了：概览缓存必须作废，否则刚加的票最长 60 秒不出现
        self.invalidate_watchlist_cache()

    # ==================== 主快照 ====================

    async def snapshot(self, code: str, *,
                       force_refresh: bool = False,
                       light: bool = False,
                       config_patch: IntradayConfig | None = None) -> IntradaySnapshot:
        """组装做T辅助完整快照（前端四面板唯一数据源）。

        light=True 为「轻量快照」：只算行情 + 关键价位 + 七因子总分 + 信号，
        跳过估值空间、消息面 LLM 打分、板块分时与大盘指数。用于自选列表页
        （那里只要「哪只票现在触发了信号」），避免为 N 只标的各跑一次 LLM 与
        同业批量快照——实测完整快照首个请求曾达 54s，会明显拖慢交互。

        config_patch：前端「权重预览」用的**临时口径**（不落库、不影响这只票
        已有的档案）。给了它就完全按它算，不再解析个股覆盖 ——
        否则预览结果会变成"档案 + 你的改动"的叠加，用户看不出改动本身的效果。
        """
        config = self._reload_config()
        # 个股口径优先级：**数据库权重档案 > YAML overrides > 全局配置**。
        # 两者同时存在时必须明说是哪一份在生效 —— 同一只票两套参数下结论不同，
        # 用户不知道看的是哪一套就会做出错误动作（这条规则在 config.py 里反复强调）。
        if config_patch is not None:
            config = config_patch
            override, override_source = None, "预览口径（未保存）"
        else:
            override, override_source = await self._resolve_override(code, config)
            if override is not None and not override.is_empty():
                config = config.with_override(code, override)
        if force_refresh:
            self._data.invalidate()
            self._sentiment.invalidate()
        watch = config.watch(code)
        attempts: list[SourceAttempt] = []
        gaps: list[str] = []
        if config_patch is not None:
            gaps.append("⚠️ 当前为**权重预览口径**（页面上的分数不是该票的生效口径），"
                        "点「保存」后才会写入数据库")
        elif override is not None and not override.is_empty():
            gaps.append(f"已应用{override_source}：{override.describe()} —— "
                        "该标的的打分/档位与全局口径不同，"
                        "面板上的总分与信号只对这只票有效")

        if config.load_error:
            # 配置写坏时面板会照常出数，必须显式告知「当前用的是默认参数」，
            # 否则用户改的权重/自选池被静默忽略而毫无察觉。
            gaps.append(
                f"⚠️ configs/intraday.yaml 解析失败，当前使用内置默认参数"
                f"（你配置的权重与自选池未生效）：{config.load_error}")
        # 打分用板块：只认自选池里显式声明的关联板块；未声明则板块类维度记为不可用。
        scored_board_names = config.board_names(code)
        boards_bound = bool(scored_board_names)
        # 展示用板块：未绑定时给全部配置板块做「参考板块」，只在图上/面板上展示，
        # 不参与任何打分（否则会出现「银行股按 PCB 板块排名打分」这种脏数据）。
        board_names = scored_board_names or config.reference_board_names()
        if not boards_bound:
            gaps.append(
                "该标的未绑定关联板块（当前展示的是配置里的参考板块）："
                "板块情绪/板块排行维度未计入总分 —— 在「自选」中用「加自选」"
                "填写关联板块即可绑定")
        peer_codes = [c for c in (watch.peers if watch else []) if c != code]

        tasks: list[Any] = [
            self._data.fetch_bars(code, days=_INTRADAY_DAYS),
            self._data.fetch_trend(code),
            self._data.fetch_quote(code),
            self._fetch_daily_bars(code),
            # 市场情绪周期：自选池里所有标的共用同一份（Provider 内部按 TTL 缓存），
            # 放在基础任务里 —— 轻量快照也要它，因为它是「今天能不能做T」的闸门。
            self._market_cycle(),
            *[self._board.fetch_snapshot(n, _board_kind(config, n))
              for n in board_names],
        ]
        if not light:
            # 板块分时、大盘、估值、消息面只服务「完整面板」；
            # 轻量模式（自选列表）不需要板块分时的整条链（含 ~13s 的东财尝试）。
            #
            # `wait=False`：板块分时是**全链路最慢的一环**（实测单次 22 秒 ——
            # akshare 每次都重拉全市场概念板块名单），而面板的价格/打分必须立刻出来。
            # 所以首次没有缓存时先返回"后台获取中"，几秒后的下一次刷新自动出现，
            # 而不是让用户盯着一个 20 秒的转圈。
            tasks.extend([
                *[self._board.fetch_series(n, _board_kind(config, n), peer_codes,
                                           wait=False)
                  for n in board_names],
                self._board.fetch_index(DEFAULT_INDEX),
                self._sentiment.analyze(
                    code=code, name=watch.name if watch else "",
                    limit=config.factors.news.max_items,
                    max_items=config.factors.news.max_items),
                self._valuation.fetch(
                    code, name=watch.name if watch else "", watch=watch),
                self._fetch_index_volume(code, config),
                self._fetch_overseas(watch, config, code),
            ])
        results = await asyncio.gather(*tasks, return_exceptions=True)
        cursor = 0

        def take(label: str) -> Any:
            nonlocal cursor
            value = results[cursor]
            cursor += 1
            if isinstance(value, BaseException):
                logger.warning("做T子链路失败 %s(%s): %s",
                               label, code, str(value)[:160])
                return None
            return value

        bars_result = take("分钟bars")
        trend_result = take("分时")
        quote_result = take("快照")
        daily_result = take("日线")
        cycle_result = take("市场情绪周期")
        board_snapshots = [take(f"板块快照[{n}]") for n in board_names]
        if light:
            # 轻量模式不取板块分时，直接给空列表（前端列表页不渲染分时图）
            board_series = [None] * len(board_names)
            index_info = None
            news = None
            valuation = None
            index_volume = None
            overseas = None
        else:
            board_series = [take(f"板块分时[{n}]") for n in board_names]
            index_info = take("大盘指数")
            news = take("消息面")
            valuation = take("估值空间")
            # 注意：这两个子链路返回 (对象, 尝试记录) 二元组，必须解包 ——
            # 直接把元组当对象用会让 hasattr(..., "to_dict") 恒为假，
            # 于是指数量能/海外映射静默变成 None（实测踩过）。
            index_volume, index_volume_attempts = _unpack(take("指数量能"))
            overseas, overseas_attempts = _unpack(take("海外映射"))
            attempts.extend(index_volume_attempts)
            attempts.extend(overseas_attempts)

        bars, intraday_source = _split_frame(bars_result, attempts, gaps, "分钟K线")
        trend, trend_source = _split_frame(trend_result, attempts, gaps, "分时")
        quote, quote_source = _split_quote(quote_result, attempts, gaps)
        daily_bars, daily_source = _split_daily(daily_result, attempts, gaps)

        name = watch.name if watch else ""
        if quote is None and bars is not None and len(bars):
            quote = _quote_from_bars(code, bars)
            if quote is not None:
                quote_source = f"{intraday_source}（由分钟线推算，非实时快照）"
                gaps.append("实时快照不可用，行情由分钟线末根bar推算")
        if quote is not None and not name:
            name = quote.name

        boards = [b for b in board_snapshots if b is not None]
        series = [s for s in board_series if s is not None]
        # 只有已绑定板块的快照才允许进入打分（名字对得上，避免参考板块污染分数）
        scored_boards = [b for b in boards if b.name in set(scored_board_names)]
        for board in boards:
            if not board.available and board.gap:
                gaps.append(f"板块[{board.name}]：{board.gap}")
        for item in series:
            if not item.available and item.gap:
                gaps.append(f"板块分时[{item.name}]：{item.gap}")
        news_obj = news if isinstance(news, NewsSentiment) else None
        if news_obj is not None and news_obj.gap:
            gaps.append(f"消息面：{news_obj.gap}")

        trade_date = ""
        features: pd.DataFrame | None = None
        today_features: pd.DataFrame | None = None
        if bars is not None and len(bars):
            features = build_intraday_features(bars, config)
            trade_date = str(features["day"].iloc[-1])
            today_features = features[
                features["day"] == trade_date].reset_index(drop=True)

        # ---- 技能库四因子：情绪周期 / 缠论结构 / 筹码量能 / 股性画像 ----
        market_cycle = cycle_result if isinstance(cycle_result, MarketCycle) else None
        if market_cycle is not None and not market_cycle.available:
            gaps.append(f"市场情绪周期：{market_cycle.gap or '不可用'}"
                        "（该维度不计入总分，有效权重相应减少）")
        elif market_cycle is not None:
            for gate in market_cycle.gates:
                gaps.append(f"⛔ 情绪周期一票否决：{gate}（退潮/冰点不做T降本）")
            if not market_cycle.t_allowed and config.factors.cycle.veto_signals:
                gaps.append(
                    f"⛔ 周期阶段「{market_cycle.stage}」：禁止低吸做T（做T大概率 T 反），"
                    "高抛减仓方向不禁止")
        character = self._character_for(
            code, name=name, daily_bars=daily_bars, config=config)
        if character is not None and not character.available and character.gap:
            gaps.append(f"股性画像：{character.gap}（股性因子不计入总分）")
        chan_snapshot = (
            extract_chan_facts(daily_bars, config)
            if config.weights.chan > 0
            else ChanFacts(available=False, gap="该票权重把缠论结构设为0"))
        chip_snapshot = (
            _extract_chip(today_features=today_features, daily_bars=daily_bars,
                          quote=quote, config=config)
            if config.weights.chip > 0
            else ChipSnapshot(available=False, gap="该票权重把筹码量能设为0"))

        state, state_label = session_state()
        stale = bool(trade_date) and trade_date != datetime.now().strftime("%Y-%m-%d")
        if stale:
            # 盘中还拿着上一交易日的行情，是**数据链**的问题而不是"今天没开市"：
            # 实测 QMT 本地分钟库不会自动写入当天bar，未触发当日增量补下载时
            # 只会返回上一交易日，分时图就会把昨天一整天原样画出来。
            # 节假日/盘前同样会 stale，那时不该甩锅给行情源，所以分开措辞。
            hint = ("；本地行情源未更新至今日（QMT 当日分钟数据需增量补下载，"
                    "腾讯源应自动兜底）"
                    if state in ("call_auction", "trading", "lunch_break") else "")
            gaps.append(
                f"当前非当日行情，展示最近交易日 {trade_date} 的完整数据"
                f"（{state_label}）{hint}")
        elif state in ("pre_open", "lunch_break", "closed"):
            gaps.append(f"{state_label}：分数基于最近一根已完成bar，档位仅供参考")

        levels = None
        scorecard = None
        signal = None
        markers: list[SignalMarker] = []
        score_series: list[ScorePoint] = []
        level_series: list[LevelPoint] = []
        if (today_features is not None and len(today_features)
                and quote is not None):
            levels = self._compute_levels(
                quote=quote, today_features=today_features,
                daily_bars=daily_bars, config=config)
            ctx = self._build_context(
                quote=quote, features=features, today_features=today_features,
                daily_bars=daily_bars, boards=scored_boards, news=news_obj,
                config=config, index_volume=index_volume, overseas=overseas,
                chan=chan_snapshot, chip=chip_snapshot, cycle=market_cycle,
                character=character)
            # 逐bar档位：回放与画图都要按"当时那一刻"的档位，而不是当前值
            # （实测 300308 低吸线当日从 862 抬到 898、603083 从 215 抬到 222；
            #  拿当前值横贯全天会把早盘正常回踩误判成破位/误读成"开盘在低吸线下"）
            levels_series = replay_levels(
                today_features,
                box_high=levels.box_high, box_low=levels.box_low,
                box_span_days=levels.box_span_days, atr=levels.atr,
                config=config)
            replay = replay_totals(
                intraday_features=today_features,
                oscillator_features=build_oscillator_features(
                    resample_bars(_bars_frame(features), 30), config),
                score_constants=day_level_constants(
                    box_position=ctx.box_position,
                    breadth=ctx.breadth,
                    relative_strength_pct=_relative_strength(quote, scored_boards),
                    news_llm_score=ctx.news_llm_score,
                    news_positive=ctx.news_positive,
                    news_negative=ctx.news_negative,
                    news_count=ctx.news_count,
                    config=config,
                    index_change_pct=ctx.index_change_pct,
                    index_volume_ratio=ctx.index_volume_ratio,
                    board_rank=ctx.board_rank,
                    board_change_pct=ctx.board_change_pct,
                    overseas_overnight_pct=ctx.overseas_overnight_pct,
                    overseas_intraday_pct=ctx.overseas_intraday_pct,
                    overseas_available=ctx.overseas_available,
                    chan_zd=ctx.chan_zd, chan_zg=ctx.chan_zg,
                    chan_divergence=ctx.chan_divergence,
                    chan_divergence_strength=ctx.chan_divergence_strength,
                    chan_available=ctx.chan_available,
                    chip_avg_volume=(
                        None if chip_snapshot is None else chip_snapshot.avg_volume),
                    chip_available=ctx.chip_available,
                    cycle_temperature=ctx.cycle_temperature,
                    cycle_available=ctx.cycle_available,
                    character_atr_pct=ctx.character_atr_pct,
                    character_trend_efficiency=ctx.character_trend_efficiency,
                    character_available=ctx.character_available),
                daily_macd_score=_daily_macd_score(daily_bars, config, quote.price),
                daily_osc_score=_daily_osc_score(daily_bars, config),
                weights=config.weights.as_dict(),
                config=config,
            )
            output = run_engine(
                ctx=ctx, levels=levels, config=config,
                ts=str(today_features["ts"].iloc[-1]), replay=replay,
                levels_series=levels_series)
            scorecard = output.scorecard
            signal = output.signal
            markers = output.markers
            # 逐bar总分序列：回答"摸到档位为什么没信号"（触发需价格与总分同时达标）
            if replay is not None and len(replay):
                score_series = [
                    ScorePoint(
                        ts=str(row.ts),
                        price=None if row.close is None else float(row.close),
                        total=float(row.total))
                    for row in replay.itertuples(index=False)
                    if getattr(row, "total", None) is not None
                    and math.isfinite(float(row.total))
                ]
            # 逐bar档位序列：供前端把档位画成随时间漂移的曲线（见 LevelPoint 说明）
            if levels_series:
                level_series = [
                    LevelPoint(ts=str(row.ts), low_buy=round(item.low_buy, 4),
                               high_sell=round(item.high_sell, 4),
                               stop_loss=round(item.stop_loss, 4))
                    for row, item in zip(today_features.itertuples(index=False),
                                         levels_series, strict=False)
                ]
            if markers:
                gaps.append(
                    "图上三角标记为「以当前日级因子（箱体/情绪/消息面）对当日逐bar的近似重放」；"
                    "分钟级因子随bar变化，日级因子按最新值恒定")

        snapshot = IntradaySnapshot(
            code=code, name=name, trade_date=trade_date,
            session_state=state, session_label=state_label,
            quote=quote,
            trend=_to_trend(trend),
            bars=_to_bars(today_features),
            levels=levels, scorecard=scorecard, signal=signal, markers=markers,
            score_series=score_series, level_series=level_series,
            valuation=valuation if isinstance(valuation, ValuationSpace) else None,
            sentiment=self._build_sentiment_panel(
                quote=quote, boards=boards, scored_boards=scored_boards,
                boards_bound=boards_bound, series=series,
                index_info=index_info, config=config),
            news=news_obj,
            index_volume=(
                index_volume.to_dict() if hasattr(index_volume, "to_dict") else None),
            overseas=(
                _overseas_to_dict(overseas) if overseas is not None else None),
            health=DataHealth(
                chosen_intraday_source=intraday_source,
                chosen_daily_source=daily_source,
                chosen_quote_source=quote_source,
                attempts=attempts, gaps=gaps, stale=stale, trade_date=trade_date),
            config_snapshot=config.snapshot(),
            notifier=self._notifier.channel_status(),
            disclaimer=config.disclaimer,
        )
        if trend_source and intraday_source and trend_source != intraday_source:
            snapshot.health.gaps.append(
                f"分时与分钟K线来自不同数据源（{trend_source} / {intraday_source}）")

        if signal is not None and signal.triggered and snapshot.scorecard is not None:
            await self._push(snapshot, signal)
        return snapshot

    async def _push(self, snapshot: IntradaySnapshot, signal: Any) -> None:
        """推送（失败仅记录，不影响面板返回）。"""
        try:
            results, pushed = await self._notifier.push(snapshot, signal)
            signal.pushed = pushed
            snapshot.notifier["last_push"] = [r.model_dump() for r in results]
        except Exception as exc:  # noqa: BLE001
            logger.warning("做T推送异常(%s): %s", snapshot.code, str(exc)[:150])
            snapshot.notifier["last_push_error"] = str(exc)[:200]

    # ==================== 数据装配 ====================

    async def _fetch_daily_bars(self, code: str) -> tuple[pd.DataFrame | None, str]:
        """日线上下文（复用项目采集链：QMT→本地CSV→AkShare）。"""
        if self._backend is None:
            raise DataFetchError("日线上下文不可用：未注入数据采集链")
        points = await self._backend.fetch(close_indicator(code))
        frame = daily_bars_from_points(points or [])
        if frame.empty:
            raise DataFetchError(f"日线数据为空({code})")
        return frame, SOURCE_LABELS["router"]

    # ==================== 权重档案（数据库，优先级高于 YAML overrides） ====================

    async def _profiles_cached(self, *, force: bool = False) -> dict[str, Any]:
        """全部权重档案（按代码索引），进程内短 TTL 缓存。

        为什么要缓存：自选池每分钟会给每只票各跑一次快照，若每次都查一次库，
        N 只票就是 N 次 SQLite 读。档案是低频变更的用户数据，5 秒 TTL 完全够，
        保存/删除时主动失效即可。
        """
        if self._profile_repo is None:
            return {}
        now = time.monotonic()
        if not force and self._profile_cache is not None:
            stamp, cached = self._profile_cache
            if now - stamp < _PROFILE_CACHE_TTL:
                return cached
        try:
            items = await self._profile_repo.list(limit=500)
        except Exception as exc:  # noqa: BLE001 档案库故障不该让做T面板整体不可用
            logger.warning("读取做T权重档案失败（按无档案继续）：%s", str(exc)[:160])
            self._profile_cache = (now, {})
            return {}
        mapping = {item.code: item for item in items}
        self._profile_cache = (now, mapping)
        return mapping

    def invalidate_profile_cache(self) -> None:
        self._profile_cache = None

    async def _profile_for(self, code: str) -> Any:
        return (await self._profiles_cached()).get(str(code).strip())

    async def _resolve_override(
        self, code: str, config: IntradayConfig,
    ) -> tuple[Any, str]:
        """解析该票实际生效的个股覆盖 → (CodeOverride | None, 来源人话说明)。"""
        profile = await self._profile_for(code)
        if profile is not None and not profile.is_empty():
            raw = profile.as_override()
            # 再过一遍配置层校验：字段名/取值合法性由 config 统一负责，
            # 库里历史遗留的未知字段必须在这里被拦住（而不是静默塞进模型）。
            validated = IntradayConfig.validate_override({
                "weights": dict(raw.weights),
                "daily_weights": dict(raw.daily_weights),
                "thresholds": dict(raw.thresholds),
                "levels": dict(raw.levels),
            })
            source = (f"权重档案（{profile.source}，更新于 {profile.updated_at or '未知时间'}）"
                      + (f"，备注：{profile.note}" if profile.note else ""))
            return validated, source
        override = config.override_for(code)
        if override is not None and not override.is_empty():
            return override, f"YAML 个股微调（configs/intraday.yaml 的 overrides.{code}）"
        return None, ""

    async def profiles(self, *, limit: int = 200) -> list[Any]:
        """列出全部权重档案（前端「档案库」列表）。"""
        if self._profile_repo is None:
            return []
        return await self._profile_repo.list(limit=limit)

    async def profile(self, code: str) -> Any:
        """取单只票的档案（不存在返回 None）。"""
        if self._profile_repo is None:
            return None
        return await self._profile_repo.get(str(code).strip())

    async def save_profile(self, profile: Any) -> Any:
        """保存/更新权重档案，并让所有相关缓存立即失效。

        必须失效的三处缓存，少一处就会"改了没生效"：
          1. `_profile_cache`（档案自身）；
          2. `_daily_cache`（日K快照，权重变了总分就变了）；
          3. `_watch_cache`（自选概览里的总分）。
        前端侧还有一份 snapshotCache，由「保存后强制刷新」负责清。
        """
        if self._profile_repo is None:
            raise RuntimeError("权重档案仓储未装配（intraday profile repository unavailable）")
        from src.intraday.config import IntradayConfig

        # 保存前把覆盖项过一次全局配置校验：字段名写错、阈值不单调等问题
        # 必须在**入库之前**被拒绝，否则坏数据进了库、之后每次快照都要报错。
        IntradayConfig.validate_override({
            "weights": dict(profile.weights),
            "daily_weights": dict(profile.daily_weights),
            "thresholds": dict(profile.thresholds),
            "levels": dict(profile.levels),
        })
        saved = await self._profile_repo.upsert(profile)
        self.invalidate_profile_cache()
        self._daily_cache.clear()
        self.invalidate_watchlist_cache()
        return saved

    async def delete_profile(self, code: str) -> bool:
        """删除权重档案（幂等）；删除后该票回落到 YAML overrides / 全局口径。"""
        if self._profile_repo is None:
            raise RuntimeError("权重档案仓储未装配（intraday profile repository unavailable）")
        removed = await self._profile_repo.delete(str(code).strip())
        self.invalidate_profile_cache()
        self._daily_cache.clear()
        self.invalidate_watchlist_cache()
        return removed

    # ==================== 技能库四因子的取数入口 ====================

    async def _market_cycle(self, *, force: bool = False) -> MarketCycle:
        """市场情绪周期快照（Provider 内部按 TTL 缓存，自选池共用一份）。"""
        if not self._config.factors.cycle.enabled:
            return MarketCycle(available=False, gap="factors.cycle.enabled=false")
        return await self._cycle.snapshot(force=force)

    def _character_for(self, code: str, *, name: str = "",
                       daily_bars: pd.DataFrame | None,
                       config: IntradayConfig) -> CharacterProfile | None:
        """股性画像（进程内按「日线最后一根bar的日期」缓存，当日不重算）。

        缓存键带上日期而不是纯时间戳：股性来自日线，一天之内不会变，
        用 5 分钟 TTL 只会让自选池每分钟把 N 只票的画像重算一遍。
        """
        if config.weights.character <= 0 and not config.factors.character.suggest_weights:
            return None
        stamp = ""
        if daily_bars is not None and len(daily_bars) and "ts" in daily_bars.columns:
            stamp = str(daily_bars["ts"].iloc[-1])
        key = f"{code}|{stamp}"
        hit = self._character_cache.get(key)
        if hit is not None:
            return hit[1]
        profile = analyze_character(
            daily_bars, code=code, name=name, mode="intraday")
        # 只保留最近若干条，避免长期运行时缓存无限增长（自选池规模 × 交易日数）
        if len(self._character_cache) > 200:
            self._character_cache.clear()
        self._character_cache[key] = (time.monotonic(), profile)
        return profile

    async def character(self, code: str, *, mode: str = "intraday",
                        refresh: bool = False) -> CharacterProfile:
        """对外接口：取该标的的股性画像（含推荐权重与档位）。"""
        config = self._reload_config()
        bars, _source = await self._fetch_daily_bars(code)
        name = self._lookup_name(code)
        if refresh:
            self._character_cache.clear()
        if mode == "daily":
            # 日线模式仍用同一份日线画像，只是推荐的是日线权重配方
            return analyze_character(bars, code=code, name=name, mode="daily")
        profile = self._character_for(
            code, name=name, daily_bars=bars, config=config)
        return profile if profile is not None else analyze_character(
            bars, code=code, name=name, mode="intraday")

    async def market_cycle(self, *, force: bool = False) -> MarketCycle:
        """对外接口：当前市场情绪周期快照。"""
        return await self._market_cycle(force=force)

    async def _fetch_index_volume(
        self, code: str, config: IntradayConfig,
    ) -> tuple[Any, list[SourceAttempt]]:
        """个股所属指数的量能（上证/创业板/科创50/深证成指）。"""
        from src.intraday.market import board_index_for

        index_code, index_name = board_index_for(code)
        snapshot, _, attempts = await self._data.fetch_index_volume(index_code)
        if not snapshot.name:
            snapshot.name = index_name
        return snapshot, attempts

    async def _fetch_overseas(
        self, watch: WatchConfig | None, config: IntradayConfig,
        code: str = "",
    ) -> tuple[Any, list[SourceAttempt]]:
        """海外映射行情（美股隔夜 + 韩股盘中同步）。

        映射来源：个股配置 → 所属板块默认映射（见 config.overseas_for）。
        """
        from src.intraday.market import OverseasQuote, score_overseas

        symbols = config.overseas_for(code) if code else (
            list(watch.overseas) if watch and watch.overseas else [])
        if not symbols:
            return score_overseas([], config.factors.overseas), []
        quotes, _, attempts = await self._data.fetch_overseas(symbols)
        # 未返回的标的补成不可用条目（如实标注缺口）
        got = {quote.symbol for quote in quotes}
        for symbol in symbols:
            if symbol not in got:
                quotes.append(OverseasQuote(
                    symbol=symbol, name=symbol,
                    market="kr" if symbol.startswith("kr") else "us",
                    available=False, gap="腾讯未返回该代码"))
        return score_overseas(quotes, config.factors.overseas), attempts

    def _compute_levels(self, *, quote: Quote, today_features: pd.DataFrame,
                        daily_bars: pd.DataFrame | None,
                        config: IntradayConfig) -> Any:
        """日线箱体 + 当日布林 + VWAP → 做T关键价位。"""
        box_high = box_low = None
        span_days = 0
        if daily_bars is not None and len(daily_bars) >= 5:
            box_high, box_low, span_days = ind.box_levels(
                daily_bars, lookback=config.factors.box.lookback_days)
        atr_value = None
        if daily_bars is not None and len(daily_bars) >= 15:
            atr_value = ind.last_value(ind.atr(daily_bars))
        boll = ind.bollinger(
            today_features, window=config.factors.boll.window,
            num_std=config.factors.boll.num_std)
        last = today_features.iloc[-1]
        # 当日已成交低点：止损位的硬上限依据（见 compute_levels 的三道护栏）
        day_low = None
        if "low" in today_features.columns and len(today_features):
            raw = today_features["low"].min()
            day_low = None if raw != raw else float(raw)
        return compute_levels(
            price=quote.price, box_high=box_high, box_low=box_low,
            box_span_days=span_days,
            boll_upper=ind.last_value(boll["upper"]),
            boll_mid=ind.last_value(boll["mid"]),
            boll_lower=ind.last_value(boll["lower"]),
            pct_b=_finite(last.get("pct_b")),
            bandwidth=_finite(last.get("bandwidth")),
            vwap=_finite(last.get("vwap")),
            atr=atr_value, day_low=day_low, config=config)

    def _build_context(
        self, *, quote: Quote, features: pd.DataFrame,
        today_features: pd.DataFrame, daily_bars: pd.DataFrame | None,
        boards: list[Any], news: NewsSentiment | None,
        config: IntradayConfig,
        index_volume: Any = None, overseas: Any = None,
        chan: ChanFacts | None = None, chip: ChipSnapshot | None = None,
        cycle: MarketCycle | None = None,
        character: CharacterProfile | None = None,
    ) -> FactorContext:
        """组装十四因子打分上下文（每个字段都来自真实数据或 None）。"""
        last = today_features.iloc[-1]
        price = quote.price
        box_high = box_low = None
        span_days = 0
        if daily_bars is not None and len(daily_bars) >= 5:
            box_high, box_low, span_days = ind.box_levels(
                daily_bars, lookback=config.factors.box.lookback_days)
        primary = _primary_board(boards)
        # 板块涨幅排行（同花顺快照自带「涨幅排名 10/390」）
        board_rank = primary.rank if primary else None
        board_rank_percentile = None
        if board_rank:
            from src.intraday.market import _parse_rank

            parsed = _parse_rank(board_rank)
            if parsed is not None:
                position, total = parsed
                board_rank_percentile = 1.0 - (position - 1) / max(1, total - 1)
        # 30分钟周期用连续序列（跨日累计），与回测/标记重放口径一致
        bars_30m = resample_bars(_bars_frame(features), 30)
        return FactorContext(
            price=price,
            vwap=_finite(last.get("vwap")),
            dev_z=_finite(last.get("dev_z")),
            dev_pct=_finite(last.get("dev_pct")),
            box_high=box_high, box_low=box_low,
            box_position=(
                None if None in (box_high, box_low)
                else ind.box_position(price, box_high, box_low)),
            box_span_days=span_days,
            pct_b=_finite(last.get("pct_b")),
            bandwidth=_finite(last.get("bandwidth")),
            bandwidth_pctl=_finite(last.get("bw_pctl")),
            macd_intraday=_macd_frame(today_features, config),
            macd_daily=_macd_frame(daily_bars, config),
            kdj_intraday=_kdj_frame(bars_30m, config),
            rsi_intraday=_rsi_series(bars_30m, config),
            kdj_daily=_kdj_frame(daily_bars, config),
            rsi_daily=_rsi_series(daily_bars, config),
            breadth=primary.breadth if primary else None,
            up_count=primary.up_count if primary else None,
            down_count=primary.down_count if primary else None,
            board_name=primary.name if primary else "",
            board_change_pct=primary.change_pct if primary else None,
            stock_change_pct=quote.change_pct,
            news_llm_score=news.llm_score if news else None,
            news_positive=news.positive_count if news else 0,
            news_negative=news.negative_count if news else 0,
            news_count=news.news_count if news else 0,
            # ---- 新增三因子 ----
            index_code=getattr(index_volume, "code", "") or "",
            index_name=getattr(index_volume, "name", "") or "",
            index_change_pct=getattr(index_volume, "change_pct", None),
            index_volume_ratio=getattr(index_volume, "volume_ratio", None),
            index_volume_yesterday=getattr(index_volume, "yesterday_volume", None),
            index_volume_projected=getattr(index_volume, "projected_volume", None),
            index_elapsed_ratio=getattr(index_volume, "elapsed_ratio", 0.0) or 0.0,
            index_gap=getattr(index_volume, "gap", None),
            board_rank=board_rank,
            board_rank_percentile=board_rank_percentile,
            board_rank_gap=None if board_rank else "该板块快照未提供涨幅排名",
            overseas_available=getattr(overseas, "available", None),
            overseas_overnight_pct=getattr(overseas, "overnight_avg_pct", None),
            overseas_intraday_pct=getattr(overseas, "intraday_avg_pct", None),
            overseas_symbols=[
                q.symbol for q in (getattr(overseas, "quotes", None) or [])
                if getattr(q, "available", False)],
            overseas_gap="；".join(getattr(overseas, "gaps", None) or []) or None,
            # ---- 技能库四因子 ----
            chan_available=chan.available if chan is not None else None,
            chan_zd=None if chan is None else chan.zd,
            chan_zg=None if chan is None else chan.zg,
            chan_divergence=None if chan is None else chan.divergence,
            chan_divergence_strength=(
                0.0 if chan is None else chan.divergence_strength),
            chan_bars_used=0 if chan is None else chan.bars_used,
            chan_gap=None if chan is None else chan.gap,
            chip_available=chip.available if chip is not None else None,
            chip_volume_ratio=None if chip is None else chip.volume_ratio,
            chip_position=None if chip is None else chip.position,
            chip_turnover_pct=quote.turnover_rate,
            chip_gap=None if chip is None else chip.gap,
            cycle_available=cycle.available if cycle is not None else None,
            cycle_stage="" if cycle is None else cycle.stage,
            cycle_temperature=None if cycle is None else float(cycle.temperature),
            cycle_t_allowed=None if cycle is None else cycle.t_allowed,
            cycle_gates=[] if cycle is None else list(cycle.gates),
            cycle_gap=None if cycle is None else cycle.gap,
            character_available=(
                character.available if character is not None else None),
            character_atr_pct=None if character is None else character.atr_pct,
            character_trend_efficiency=(
                None if character is None else character.trend_efficiency),
            character_t_friendly=None if character is None else character.t_friendly,
            character_grade="" if character is None else character.grade,
            character_regime="" if character is None else character.regime,
            character_gap=None if character is None else character.gap,
        )

    @staticmethod
    def _build_sentiment_panel(
        *, quote: Quote | None, boards: list[Any], series: list[Any],
        index_info: Any, config: IntradayConfig,
        scored_boards: list[Any] | None = None,
        boards_bound: bool = True,
    ) -> SentimentPanel:
        """消息面与市场情绪面板（板块涨跌家数 + 相对强度 + 大盘状态）。

        ``boards`` 是**展示**用的板块快照（未绑定时是参考板块）；
        ``scored_boards`` 才是**打分**用的已绑定板块，两者必须分开。
        """
        gaps: list[str] = []
        primary = _primary_board(
            boards if scored_boards is None else scored_boards)
        breadth = primary.breadth if primary else None
        board_change = primary.change_pct if primary else None
        stock_change = quote.change_pct if quote else None
        rs = (None if stock_change is None or board_change is None
              else stock_change - board_change)
        score = sentiment_score_from(breadth, rs, config.factors.sentiment)
        if not boards_bound:
            gaps.append("关联板块未绑定，板块情绪维度不可用（参考板块不参与打分）")
        elif primary is None or not primary.available:
            gaps.append("板块实时快照不可用（涨跌家数缺口）")
        if board_change is None:
            gaps.append("板块涨跌幅缺口，个股相对强度无法计算")
        info = index_info if isinstance(index_info, dict) else {}
        if info and not info.get("available"):
            gaps.append(f"大盘状态缺口：{info.get('gap', '未知')}")
        if score is None:
            verdict = "情绪面数据不足，本维度未计入总分"
        elif score > 0.15:
            verdict = "板块与个股情绪偏暖（利于低吸做T）"
        elif score < -0.15:
            verdict = "板块与个股情绪偏冷（利于高抛做T）"
        else:
            verdict = "板块情绪中性"
        return SentimentPanel(
            board_name=primary.name if primary else "",
            board_change_pct=board_change,
            up_count=primary.up_count if primary else None,
            down_count=primary.down_count if primary else None,
            breadth=breadth,
            breadth_score=(
                None if breadth is None else round((breadth - 0.5) * 2.0, 4)),
            boards=[b for b in boards if b is not None],
            boards_bound=boards_bound,
            board_series=[s for s in series if s is not None],
            stock_change_pct=stock_change,
            relative_strength_pct=None if rs is None else round(rs, 4),
            rs_score=(None if rs is None else round(
                ind.clip(rs / config.factors.sentiment.rs_scale_pct), 4)),
            index_name=str(info.get("index_name") or ""),
            index_code=str(info.get("index_code") or ""),
            index_price=info.get("index_price"),
            index_change_pct=info.get("index_change_pct"),
            index_state=str(info.get("index_state") or ""),
            score=score, verdict=verdict, gaps=gaps,
            source_name="同花顺板块 + 腾讯行情",
        )

    # ==================== 自选列表 ====================

    async def watchlist(self, *, limit: int = 20,
                        force: bool = False) -> list[WatchItem]:
        """自选标的概览（轻量快照并发，含当前总分与信号）。

        **默认走进程内缓存**：盘中由 `_watchlist_refresh_loop` 每分钟重算一次并写入，
        前端每分钟取一次直接命中缓存 —— 用户要的"自选股每分钟自动刷新"就是这条链路；
        `force=True`（前端「刷新」按钮）绕过缓存立即重算。

        用 light=True 重算：列表页只需「哪只票现在触发信号」，不必为每只标的各跑一次
        LLM 消息面打分与同业批量快照（否则 N 只票会互相抢数据源，整体变慢）。
        """
        ttl = self._watch_cache_ttl()
        cached = self._watch_cache
        if not force and cached is not None:
            # **有缓存就直接给，绝不让请求排队等重算**。
            # 过期时改为后台重算（stale-while-revalidate）：盘中刷新循环每 60 秒
            # 本来就会更新它，而 60~300 秒前的自选概览对用户仍然是有用的
            # （价格由报价快车道单独贴，见 `_apply_quote_overlay`）。
            # 实测踩过：缓存刚过期时请求会进 `_watch_lock` 等整表重算（~2.3 秒），
            # 多个标签页 + 刷新循环叠在一起就是"刷新时快时慢"。
            if ttl <= 0 or time.monotonic() - cached[0] >= ttl:
                self._schedule_watchlist_refresh()
            return self._apply_quote_overlay(cached[1][:limit])
        # 单飞：刷新循环与前端请求（多个标签页）可能同时进来，只真正算一次
        async with self._watch_lock:
            cached = self._watch_cache
            if not force and cached is not None and ttl > 0:
                return self._apply_quote_overlay(cached[1][:limit])
            items = await self._compute_watchlist(limit=self._watch_cache_size)
            self._watch_cache = (time.monotonic(), items)
            self._watch_generation += 1
            self._refresh_state["last_count"] = len(items)
            return self._apply_quote_overlay(items[:limit])

    def _schedule_watchlist_refresh(self) -> None:
        """后台重算自选概览（去重，不阻塞调用方）。

        与刷新循环共用同一条重算路径；即使循环被关掉（配置 0）或正在等下一分钟
        刻度，一次读请求也能顺手把数据刷新起来 —— 自愈而不是让用户等。
        """
        if self._watch_refresh_inflight:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:      # 无事件循环（同步调用）时不做后台刷新
            return
        self._watch_refresh_inflight = True

        async def _refresh() -> None:
            try:
                await self.watchlist(force=True)
            except Exception as exc:  # noqa: BLE001 后台失败只记日志
                logger.warning("后台重算自选概览失败：%s", str(exc)[:160])
            finally:
                self._watch_refresh_inflight = False

        # 任务强引用必须留下：asyncio 只持弱引用，被 GC 掉会在运行中静默取消
        task = loop.create_task(_refresh())
        self._watch_refresh_tasks.add(task)
        task.add_done_callback(self._watch_refresh_tasks.discard)

    def _apply_quote_overlay(self, items: list[WatchItem]) -> list[WatchItem]:
        """把报价快车道的实时价贴到概览上（分数/信号仍是最近一次重算的值）。

        这是「现价 5 秒 / 打分 60 秒」两个节奏的解耦点：只替换 `price` 与
        `change_pct`，其余字段原样保留。因此前端**必须**能看出两者新鲜度不同
        （状态栏写"报价 5s · 打分 60s"），否则用户会把 60 秒前的信号当成此刻的信号。

        快车道没覆盖到的标的保留重算时的价格，而不是清空 —— 清空会让列表闪成 "—"。
        """
        if not self._quote_overlay:
            return items
        merged: list[WatchItem] = []
        for item in items:
            quote = self._quote_overlay.get(item.code)
            if quote is None:
                merged.append(item)
                continue
            merged.append(item.model_copy(update={
                "price": quote.price, "change_pct": quote.change_pct,
                "quote_ts": quote.ts}))
        return merged

    def _watch_cache_ttl(self) -> float:
        """缓存有效期：盘中按配置（默认 60 秒）；非盘中行情不会变，放长到 5 分钟。

        否则收盘后前端每分钟取一次都会触发全表重算（N 只票 × 每分钟），
        白白消耗数据源与 CPU，还看不出任何差别。
        """
        allowed, _reason = watchlist_refresh_window()
        if allowed:
            return float(self._config.data.watchlist_cache_ttl)
        return 300.0

    @property
    def _watch_cache_size(self) -> int:
        """缓存总是按最大规模重算，避免 limit 不同就各存一份（不同 limit 只影响切片）。"""
        return 50

    def invalidate_watchlist_cache(self) -> None:
        """自选池增删后必须调用：否则刚加的票最长 60 秒不出现（"加了没反应"）。"""
        self._watch_cache = None
        self._watch_generation += 1

    async def _compute_watchlist(self, *, limit: int = 20) -> list[WatchItem]:
        config = self._reload_config()
        codes = [item.code for item in config.watchlist][:limit]
        if not codes:
            return []
        snapshots = await asyncio.gather(
            *(self.snapshot(code, light=True) for code in codes),
            return_exceptions=True)
        items: list[WatchItem] = []
        for code, result in zip(codes, snapshots, strict=True):
            watch = config.watch(code)
            boards = list(watch.boards) if watch else []
            if isinstance(result, BaseException) or result is None:
                # 快照失败时也不能把名称丢掉：配置里的名字 / 字典里的名字都行，
                # 实在没有就留空（**不要用代码冒充名称**）。
                items.append(WatchItem(
                    code=code,
                    name=(watch.name if watch and watch.name != code else "")
                         or self._lookup_name(code),
                    boards=boards))
                continue
            resolved = result.name or (
                watch.name if watch and watch.name != code else "")
            items.append(WatchItem(
                code=code, name=resolved or self._lookup_name(code),
                boards=boards,
                total_score=result.scorecard.total if result.scorecard else None,
                signal_strength=(
                    result.signal.strength if result.signal else "none"),
                signal_kind=result.signal.kind if result.signal else "none",
                price=result.quote.price if result.quote else None,
                change_pct=result.quote.change_pct if result.quote else None))
        return items

    # ==================== 自选池盘中自动刷新 ====================

    async def start_watchlist_refresh(self) -> None:
        """启动「自选池盘中每分钟自动刷新」循环（幂等，重复调用不会起两个）。"""
        interval = self._config.data.watchlist_refresh_seconds
        if interval <= 0:
            logger.info("自选池自动刷新未启用（watchlist_refresh_seconds=0）")
            return
        if self._refresh_task is not None and not self._refresh_task.done():
            return
        self._refresh_task = asyncio.create_task(
            self._watchlist_refresh_loop(), name="intraday-watchlist-refresh")
        logger.info("自选池自动刷新已启动：每 %d 秒一次，窗口 09:15~11:30 / 13:00~15:00",
                    interval)

    async def stop_watchlist_refresh(self) -> None:
        """停止刷新循环（关停时调用；可重复调用）。"""
        task, self._refresh_task = self._refresh_task, None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _watchlist_refresh_loop(self) -> None:
        """盘中每分钟重算一次自选池（把所有自选股的分钟数据取到最新）。

        循环本身**不会**因为单次失败而死：数据源抖动、某只票停牌、配置被写坏都
        只影响这一轮，下一分钟照常继续。非盘中只做窗口判断、不取数。
        """
        interval = max(15, self._config.data.watchlist_refresh_seconds)
        while True:
            try:
                allowed, reason = watchlist_refresh_window()
                if allowed:
                    started = time.perf_counter()
                    try:
                        items = await self.watchlist(force=True)
                        self._refresh_state.update(
                            last_run_at=datetime.now().strftime("%H:%M:%S"),
                            last_seconds=round(time.perf_counter() - started, 2),
                            last_count=len(items), last_error="")
                    except Exception as exc:  # noqa: BLE001 单轮失败不影响下一轮
                        self._refresh_state["last_error"] = str(exc)[:200]
                        logger.warning("自选池自动刷新失败：%s", str(exc)[:200])
                await asyncio.sleep(_seconds_until_next_tick(interval))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 循环必须活着
                logger.exception("自选池自动刷新循环异常，5 秒后继续")
                await asyncio.sleep(5)

    def watchlist_refresh_status(self) -> dict[str, Any]:
        """自动刷新的运行状态（前端展示"上次刷新/为什么没在刷"）。"""
        allowed, reason = watchlist_refresh_window()
        cached = self._watch_cache
        task = self._refresh_task
        quote_task = self._quote_task
        return {
            "enabled": self._config.data.watchlist_refresh_seconds > 0,
            "interval_seconds": self._config.data.watchlist_refresh_seconds,
            "window_open": allowed,
            "window_reason": reason,
            "running": task is not None and not task.done(),
            "generation": self._watch_generation,
            "cache_age_seconds": (
                None if cached is None else round(time.monotonic() - cached[0], 1)),
            "cached_count": 0 if cached is None else len(cached[1]),
            # 报价快车道（现价/涨跌幅）与打分重算是两条独立节奏，状态里分开报，
            # 前端才能显示"报价 5s · 打分 60s"。
            "quote_enabled": self._config.data.quote_refresh_seconds > 0,
            "quote_interval_seconds": self._config.data.quote_refresh_seconds,
            "quote_running": quote_task is not None and not quote_task.done(),
            "quote_generation": self._quote_generation,
            "quote_covered": len(self._quote_overlay),
            **self._refresh_state,
            **self._quote_state,
        }

    # ==================== 报价快车道（现价/涨跌幅） ====================

    async def start_quote_refresh(self) -> None:
        """启动「报价快车道」循环（幂等）。

        只更新现价与涨跌幅，用 `fetch_quotes()` 的**批量**快照（实测 9 只 0.54ms），
        不碰打分链路 —— 所以它可以比整表重算快一个数量级。
        """
        interval = self._config.data.quote_refresh_seconds
        if interval <= 0:
            logger.info("报价快车道未启用（quote_refresh_seconds=0）")
            return
        if self._quote_task is not None and not self._quote_task.done():
            return
        self._quote_task = asyncio.create_task(
            self._quote_refresh_loop(), name="intraday-quote-refresh")
        logger.info("报价快车道已启动：每 %d 秒更新现价/涨跌幅（打分仍每 %d 秒一次）",
                    interval, self._config.data.watchlist_refresh_seconds)

    async def stop_quote_refresh(self) -> None:
        task, self._quote_task = self._quote_task, None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _quote_refresh_loop(self) -> None:
        """盘中每 N 秒批量取一次自选标的的快照，写入报价覆盖层。

        **不对齐整分钟**（与打分循环相反）：价格是连续变化的，没有"分钟的边界"
        这回事；对齐只会平白增加延迟。非盘中同样不取数。
        """
        interval = max(1, self._config.data.quote_refresh_seconds)
        while True:
            try:
                allowed, _reason = watchlist_refresh_window()
                if allowed:
                    codes = [item.code for item in self._reload_config().watchlist]
                    if codes:
                        started = time.perf_counter()
                        try:
                            quotes = await self._data.fetch_quotes(codes)
                            self._quote_overlay.update(quotes)
                            if quotes:
                                self._quote_generation += 1
                            self._quote_state.update(
                                quote_last_run_at=datetime.now().strftime("%H:%M:%S"),
                                quote_last_seconds=round(
                                    time.perf_counter() - started, 3),
                                quote_last_count=len(quotes), quote_last_error="")
                        except Exception as exc:  # noqa: BLE001 单轮失败不影响下一轮
                            self._quote_state["quote_last_error"] = str(exc)[:200]
                            logger.warning("报价快车道刷新失败：%s", str(exc)[:200])
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 循环必须活着
                logger.exception("报价快车道循环异常，2 秒后继续")
                await asyncio.sleep(2)

    # ==================== 日K级别做T ====================

    async def daily(self, code: str, *, refresh: bool = False,
                    config_patch: IntradayConfig | None = None) -> Any:
        """日K级别做T（量价体系：量柱/高量柱攻防/16形态/B1-B15/S1-S6）。

        复用项目日线采集链；取数失败返回 available=False 的快照并在 gaps 说明，
        不抛错（前端按缺口展示，避免整页报错）。

        **进程内缓存** `daily_snapshot_ttl` 秒（默认 180）：日线链路每次要做一次
        网络取数 + 250 根bar的量价规则，前端按「数据源周期的 3 倍」轮询时，
        多标签页/重复请求不该各算一遍。`refresh=True`（面板首屏与手动刷新）绕过缓存。

        config_patch：前端「权重预览」的临时口径 —— 一旦给出就**既不读也不写**
        日K缓存（否则预览结果会被缓存成正式结果，下次打开看到的就是预览分数）。
        """
        from src.intraday.daily import fetch_daily_snapshot

        ttl = self._config.data.daily_snapshot_ttl
        now = time.monotonic()
        hit = self._daily_cache.get(code)
        if (config_patch is None and not refresh and hit is not None
                and ttl > 0 and now - hit[0] < ttl):
            return hit[1]
        config = config_patch if config_patch is not None else self._reload_config()
        watch = config.watch(code)
        # 情绪周期（自选池共用一份缓存）是日线加权总分的一个输入。
        # 股性画像**不在这里取** —— 它必须用与日K面板同一份日线，
        # 否则两条取数路径在数据源落后时会拿到不同的末日（见 daily._daily_character）。
        try:
            cycle = await self._market_cycle()
        except Exception as exc:  # noqa: BLE001
            logger.info("日K做T取情绪周期失败(%s): %s", code, str(exc)[:120])
            cycle = None
        snapshot = await fetch_daily_snapshot(
            code, watch.name if watch else "", config, self._backend,
            cycle=cycle, character=None)
        if not snapshot.name and snapshot.available:
            # 日线链路本身不带证券简称，用一次轻量快照补名（失败不影响分析）
            try:
                quote, _, _ = await self._data.fetch_quote(code)
                snapshot.name = quote.name or ""
            except Exception:  # noqa: BLE001 取名失败不阻断
                pass
        if snapshot.available and config_patch is None:
            self._daily_cache[code] = (time.monotonic(), snapshot)
        return snapshot

    # ==================== 阈值回测 ====================

    async def backtest(self, code: str, *, horizon: int = 6,
                       days: int = 20) -> BacktestResult:
        """阈值回测（先做回测，再谈实盘做T）。"""
        config = self._reload_config()
        watch = config.watch(code)
        name = watch.name if watch else ""
        try:
            bars, source, _ = await self._data.fetch_bars(code, days=days)
        except DataFetchError as exc:
            return BacktestResult(
                available=False, code=code, name=name,
                gaps=[str(exc)[:200]], disclaimer=BACKTEST_DISCLAIMER)
        daily_bars = None
        if self._backend is not None:
            try:
                points = await self._backend.fetch(close_indicator(code))
                daily_bars = daily_bars_from_points(points or [])
            except Exception as exc:  # noqa: BLE001 日线缺失时用分钟合成日线
                logger.warning("回测日线取数失败(%s): %s", code, str(exc)[:120])
        result = run_threshold_backtest(
            code=code, name=name, bars_5m=bars, bars_daily=daily_bars,
            config=config, horizon=horizon)
        result.gaps = [
            *(result.gaps or []),
            f"5分钟样本来源：{source}（{len(bars)}根，约{result.days}个交易日）",
        ]
        return result


# ==================== 模块级小工具 ====================

def _unpack(result: Any) -> tuple[Any, list[SourceAttempt]]:
    """(对象, 尝试记录) 二元组 或 None → (对象|None, 尝试记录列表)。

    子链路统一返回二元组；take() 在异常时给 None。此处统一解包，
    避免把元组当对象使用导致静默丢数据。
    """
    if isinstance(result, tuple) and len(result) == 2:
        value, attempts = result
        return value, list(attempts or [])
    return result, []


def _board_kind(config: IntradayConfig, name: str) -> str:
    board = config.board_config(name)
    return board.kind if board else "concept"


def _overseas_to_dict(snapshot: Any) -> dict[str, Any]:
    """OverseasSnapshot → 纯JSON（避免把 dataclass 直接塞进响应模型）。"""
    return {
        "available": getattr(snapshot, "available", False),
        "score": getattr(snapshot, "score", None),
        "overnight_score": getattr(snapshot, "overnight_score", None),
        "intraday_score": getattr(snapshot, "intraday_score", None),
        "overnight_avg_pct": getattr(snapshot, "overnight_avg_pct", None),
        "intraday_avg_pct": getattr(snapshot, "intraday_avg_pct", None),
        "verdict": getattr(snapshot, "verdict", ""),
        "gaps": list(getattr(snapshot, "gaps", None) or []),
        "quotes": [
            quote.to_dict() for quote in (getattr(snapshot, "quotes", None) or [])
        ],
    }


def _split_frame(result: Any, attempts: list[SourceAttempt], gaps: list[str],
                 label: str) -> tuple[pd.DataFrame | None, str]:
    """(frame, source, attempts) 或异常 → (frame|None, source)。"""
    if result is None or isinstance(result, BaseException):
        detail = "子链路返回空" if result is None else str(result)[:200]
        attempts.append(SourceAttempt(
            source=f"全部{label}数据源", ok=False, detail=detail))
        gaps.append(f"{label}缺口：{detail[:160]}")
        return None, ""
    frame, source, source_attempts = result
    attempts.extend(source_attempts)
    return frame, source


def _split_quote(result: Any, attempts: list[SourceAttempt],
                 gaps: list[str]) -> tuple[Quote | None, str]:
    return _split_frame(result, attempts, gaps, "快照")


def _split_daily(result: Any, attempts: list[SourceAttempt],
                 gaps: list[str]) -> tuple[pd.DataFrame | None, str]:
    if result is None or isinstance(result, BaseException):
        detail = "子链路返回空" if result is None else str(result)[:200]
        attempts.append(SourceAttempt(
            source=SOURCE_LABELS["router"], ok=False, detail=detail))
        gaps.append(f"日线缺口：{detail[:160]}")
        return None, ""
    frame, source = result
    attempts.append(SourceAttempt(
        source=source, ok=bool(frame is not None and len(frame)),
        rows=0 if frame is None else len(frame),
        detail="日线OHLCV（复用项目QMT→本地CSV→AkShare三级采集链）"))
    return frame, source


def _quote_from_bars(code: str, bars: pd.DataFrame) -> Quote | None:
    """无快照源时由分钟线推算基础行情（来源已在 gaps 中标注，不冒充实时快照）。"""
    if bars is None or not len(bars):
        return None
    last_day = str(bars["ts"].iloc[-1])[:10]
    today = bars[bars["ts"].astype(str).str.startswith(last_day)]
    if today.empty:
        return None
    price = float(today["close"].iloc[-1])
    day_open = float(today["open"].iloc[0])
    previous = bars[bars["ts"].astype(str).str.slice(0, 10) < last_day]
    prev_close = float(previous["close"].iloc[-1]) if len(previous) else day_open
    change = price - prev_close
    return Quote(
        code=code, price=price, prev_close=prev_close, open=day_open,
        high=float(today["high"].max()), low=float(today["low"].min()),
        change=change,
        change_pct=None if not prev_close else change / prev_close * 100.0,
        volume=float(today["volume"].sum()),
        amount=float(today["amount"].sum()) if "amount" in today.columns else None)


def _finite(value: Any) -> float | None:
    """任意取值 → 有限 float；NaN/None/非数值一律 None。"""
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if result != result or result in (float("inf"), float("-inf")) else result


def _bars_frame(features: pd.DataFrame | None) -> pd.DataFrame:
    """特征表 → OHLCV表（供30分钟重采样）。"""
    columns = ["ts", "open", "high", "low", "close", "volume", "amount"]
    if features is None or len(features) == 0:
        return pd.DataFrame(columns=columns)
    frame = features.copy()
    if "amount" not in frame.columns:
        frame["amount"] = 0.0
    return frame[[c for c in columns if c in frame.columns]]


def _macd_frame(source: pd.DataFrame | None,
                config: IntradayConfig) -> pd.DataFrame | None:
    if source is None or len(source) < 2:
        return None
    frame = source if "close" in source.columns else None
    if frame is None:
        return None
    return ind.macd(frame, fast=config.factors.macd.fast,
                    slow=config.factors.macd.slow,
                    signal=config.factors.macd.signal)


def _kdj_frame(source: pd.DataFrame | None,
               config: IntradayConfig) -> pd.DataFrame | None:
    if source is None or len(source) < 2:
        return None
    return ind.kdj(source, n=config.factors.kdj_rsi.kdj_n)


def _rsi_series(source: pd.DataFrame | None,
                config: IntradayConfig) -> pd.Series | None:
    if source is None or len(source) < 2:
        return None
    return ind.rsi(source, n=config.factors.kdj_rsi.rsi_n)


def _daily_macd_score(daily_bars: pd.DataFrame | None,
                      config: IntradayConfig, price: float) -> float | None:
    """日线 MACD 分量（当日恒定，重放与实时打分共用）。"""
    frame = _macd_frame(daily_bars, config)
    if frame is None or price <= 0:
        return None
    dif = ind.last_value(frame["dif"])
    dea = ind.last_value(frame["dea"])
    if dif is None or dea is None:
        return None
    state, _ = ind.macd_cross_state(frame)
    return macd_score_from_gap(
        (dif - dea) / price, config.factors.macd.gap_scale_pct,
        cross_bonus_for(state, config.factors.macd.cross_bonus))


def _daily_osc_score(daily_bars: pd.DataFrame | None,
                     config: IntradayConfig) -> float | None:
    """日线 KDJ/RSI 分量。"""
    frame = _kdj_frame(daily_bars, config)
    rsi_series = _rsi_series(daily_bars, config)
    if frame is None and rsi_series is None:
        return None
    return osc_score_from_jr(
        ind.last_value(frame["j"]) if frame is not None else None,
        ind.last_value(rsi_series) if rsi_series is not None else None,
        config.factors.kdj_rsi.rsi_scale)


def _primary_board(boards: list[Any]) -> Any:
    """主板块（第一个可用板块；全不可用则返回第一个，便于展示缺口）。"""
    for board in boards:
        if board is not None and board.available:
            return board
    return boards[0] if boards else None


def _relative_strength(quote: Quote | None, boards: list[Any]) -> float | None:
    board = _primary_board(boards)
    if quote is None or board is None:
        return None
    if quote.change_pct is None or board.change_pct is None:
        return None
    return quote.change_pct - board.change_pct


def _to_trend(frame: pd.DataFrame | None) -> list[TrendPoint]:
    if frame is None or not len(frame):
        return []
    points: list[TrendPoint] = []
    for row in frame.itertuples(index=False):
        price = _finite(getattr(row, "price", None))
        if price is None:
            continue
        points.append(TrendPoint(
            ts=str(getattr(row, "ts", "")), price=price,
            avg_price=_finite(getattr(row, "avg_price", None)),
            volume=_finite(getattr(row, "volume", None)) or 0.0))
    return points


def _to_bars(frame: pd.DataFrame | None) -> list[Bar]:
    if frame is None or not len(frame):
        return []
    bars: list[Bar] = []
    for row in frame.itertuples(index=False):
        close = _finite(getattr(row, "close", None))
        if close is None:
            continue
        bars.append(Bar(
            ts=str(getattr(row, "ts", "")),
            open=_finite(getattr(row, "open", None)) or close,
            high=_finite(getattr(row, "high", None)) or close,
            low=_finite(getattr(row, "low", None)) or close,
            close=close,
            volume=_finite(getattr(row, "volume", None)) or 0.0,
            amount=_finite(getattr(row, "amount", None)) or 0.0,
            vwap=_finite(getattr(row, "vwap", None))))
    return bars


# ==================== 技能库四因子的取数助手 ====================

@dataclass
class ChipSnapshot:
    """筹码/量能结构取数结果。"""

    available: bool = False
    gap: str | None = None
    volume_ratio: float | None = None
    position: float | None = None
    avg_volume: float | None = None

    """筹码/量能结构取数结果。"""

    available: bool = False
    gap: str | None = None
    volume_ratio: float | None = None
    position: float | None = None
    avg_volume: float | None = None


def _extract_chip(*, today_features: pd.DataFrame | None, daily_bars: pd.DataFrame | None,
                  quote: Quote | None, config: IntradayConfig,
                  now: datetime | None = None) -> ChipSnapshot:
    """今日量能进度 × 放量位置（+ 换手率打折）。

    量比口径：`今日预测量能 / 近5日日均量`。
    预测量 = 当前累计量 / 当日已交易时间占比 —— 与指数量能因子同一口径，
    这样"今天放量了吗"在盘中任意时刻都可比（直接拿累计量比全天均量，
    早盘永远显示缩量）。
    """
    from src.intraday.market import elapsed_session_ratio

    if today_features is None or not len(today_features):
        return ChipSnapshot(available=False, gap="当日分时样本为空")
    if daily_bars is None or len(daily_bars) < 5 or "volume" not in daily_bars.columns:
        return ChipSnapshot(available=False, gap="日线样本不足（至少5根）以计算近5日均量")
    avg_volume = _finite(daily_bars["volume"].tail(5).mean())
    if not avg_volume or avg_volume <= 0:
        return ChipSnapshot(available=False, gap="近5日均量为0或缺失")
    cumulative = _finite(today_features["volume"].sum()) if "volume" in today_features else None
    if cumulative is None or cumulative <= 0:
        return ChipSnapshot(available=False, gap="当日累计成交量为0")
    ratio_of_day = elapsed_session_ratio(now)
    if not ratio_of_day or ratio_of_day <= 0.01:
        return ChipSnapshot(available=False, gap="尚未开盘（当日交易时间占比≈0）")
    projected = cumulative / ratio_of_day
    volume_ratio = projected / avg_volume
    price = quote.price if quote is not None else _finite(today_features["close"].iloc[-1])
    day_high = _finite(today_features["high"].max()) if "high" in today_features else None
    day_low = _finite(today_features["low"].min()) if "low" in today_features else None
    position = None
    if price and day_high and day_low and day_high > day_low:
        position = max(0.0, min(1.0, (price - day_low) / (day_high - day_low)))
    return ChipSnapshot(available=True, volume_ratio=volume_ratio,
                        position=position, avg_volume=float(avg_volume))

