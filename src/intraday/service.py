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
import hashlib
import json
import logging
import math
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from src.core.errors import (
    BRIEF_DEFAULT,
    BRIEF_TIGHT,
    brief,
)
from src.core.exceptions import ConfigError, DataFetchError
from src.core.trading_session import (
    MORNING_OPEN,
    SESSION_LABELS,
    WATCH_WINDOWS,
)
from src.core.trading_session import (
    session_state as _session_state,
)
from src.intraday import hot_cache
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
from src.intraday.engine import (
    apply_level_fit,
    compute_levels,
    replay_levels,
    run_engine,
)
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
from src.intraday.impact import annotate_level_basis
from src.intraday.level_fit import (
    LevelFitResult,
    adjustment_factors,
    build_dataset,
    fit_levels,
)
from src.intraday.market_amount import MarketTurnoverProvider
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
from src.intraday.valuation import HEADROOM_LABELS, ValuationProvider

logger = logging.getLogger(__name__)

#: 大盘基准指数代码（纯代码；带名称的兜底在 `intraday/market.py:DEFAULT_BOARD_INDEX`）。
#: 两者曾同名 `DEFAULT_INDEX` —— 一个 str 一个元组，故显式区分命名。
DEFAULT_INDEX_CODE = "000001"
# 5分钟上下文天数（腾讯单次接口上限约320根 ≈ 6.6 个交易日）。
# 档位拟合还想要更长窗口（用户口径是"过去 10 个交易日"），见 `_intraday_days`：
# 数据源能吃多少就取多少，拟合侧按**实际拿到的天数**如实标注。
_INTRADAY_DAYS = 6
# 权重档案的进程内缓存秒数：档案是低频变更的用户数据，
# 但自选池每分钟会给每只票各跑一次快照，不缓存就是 N 次 SQLite 读。
_PROFILE_CACHE_TTL = 5.0

#: 「最近点开过的日K标的」保留多少只（`CHG-0099`）。
#:
#: 取值 20 的依据：预热一轮的墙钟预算 90 s、并发 3 时实测约 1.06 s/只
#: ⇒ 自选池 50 只 + 20 只最近 ≈ 74 s，仍在预算内。取更大（如 50）会和
#: 自选池抢预算，反而让"最该热的"排在后面被截断。
RECENT_DAILY_LIMIT = 20

_SESSION_LABELS = SESSION_LABELS


def session_state(now: datetime | None = None) -> tuple[str, str]:
    """当前交易时段（北京时间口径）→ (状态码, 中文标签)。"""
    return _session_state(now)


# ==================== 自选池自动刷新窗口 ====================

# 用户口径：集合竞价 09:15 开始，到 11:30；下午 13:00 到 15:00。
# 非盘中（盘前/午休/收盘后/周末/节假日）完全不取数 —— 那些时段行情不会变，
# 每分钟重算 N 只标的只是白白消耗数据源配额与 CPU。
_WATCH_REFRESH_WINDOWS = WATCH_WINDOWS
# 集合竞价开始到开盘之间，市场时钟（当天第一笔成交前 tick 的 timetag）可能还停在
# 上一交易日，此时**不能**据此判定"今天休市"，否则 09:15~09:30 会被整段跳过。
_MORNING_OPEN_MINUTE = MORNING_OPEN


def sort_watch_items(items: list[Any]) -> list[Any]:
    """自选列表排序：**置顶优先**，其余保持配置顺序（用户要求的排序/置顶）。

    排序规则刻意做得保守：

      - 置顶项永远在最前（组内仍按配置顺序，符合"我把它们钉在这里"的直觉）；
      - 非置顶项**保持 `configs/intraday.yaml` 里的顺序** —— 不按涨跌幅/分数自动排。

    为什么不做"按分数自动排序"：盘中分数每分钟都在变，列表会**自己跳动**，
    用户想点的那只票会在手指落下的瞬间换位置（交易软件里公认的糟糕体验）。
    要给用户灵活排序，正确做法是让他显式置顶，而不是让系统自动重排
    （前端另有只影响当前视图的排序下拉，属于展示层，不改配置）。
    """
    if not items:
        return []
    pinned = [item for item in items if getattr(item, "pinned", False)]
    others = [item for item in items if not getattr(item, "pinned", False)]
    return pinned + others


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


def _push_allowed_now(now: datetime | None = None) -> tuple[bool, str]:
    """推送闸门之二：明确的"这个时点不该推送" → (是否允许, 人话原因)。

    ## 为什么不能直接用 `auto_select.is_trading_day()`（2026-09-25 实测）

    `is_trading_day()` 的兜底语义是"市场时钟取不到 / 时钟没推进 → 按交易日处理"，
    反过来说：**时钟停在上一交易日时它会判 False**。而"时钟停在上一交易日"有两种
    完全不同的原因，它区分不了（实测两个场景返回值相同）：

    | 场景 | 真交易日 09:20（竞价期） | 节假日 09:20 |
    |---|---|---|
    | `is_trading_day()` | **False** | **False** |

    竞价期当天第一笔成交还没产生，tick 的 timetag 不会前进（`watchlist_refresh_window`
    的 docstring 专门解释过这一点）。真把 `is_trading_day()` 当**推送**闸门，
    **每个正常交易日的 09:15~09:30 都会被误杀** —— 而那正是做T里"开盘定方向"
    最关键的时段。所以这里只用**不会误杀**的判据：

    1. 周末 —— `weekday()` 无歧义；
    2. 15:05 盘后 —— 当天行情已定稿，这时再冒出来的"信号"必然是重算在旧数据上
       重复触发的，推给用户只会让他对着收盘价做动作。

    ## 11:30~13:00 午休为什么**不**拦

    午休推的信号仍然是**当天**的，拦掉会损失"下午开盘前提醒"；而且午休期间
    市场时钟回落到上一交易日也是正常现象，拿来判"休市"同样会误杀。
    数据归属由闸门一（`SignalNotifier._stale_guard`）把关，这里不重复承担。

    ## 与闸门一的分工

    - **闸门一**（`should_push` 的 stale 检查）："这份数据属于哪一天" —— 主力防线。
      节假日拿上一交易日收盘数据算出的信号，**任何时刻**都会被它拦下；
    - **闸门二**（本函数）："这个时点还该不该推送" —— 兜底，覆盖 stale 判据失灵的
      情况（行情源给出假日期、快照来自旧版缓存等）。
    """
    moment = now or datetime.now()
    if moment.weekday() >= 5:
        return False, "周末休市"
    from src.core.trading_session import POST_CLOSE

    if moment.hour * 60 + moment.minute >= POST_CLOSE:
        return False, "已收盘后不再推送（当日行情已定稿）"
    return True, ""


def _is_trading_day_for_refresh() -> bool:
    """完整交易日判定（周末 + 市场时钟），供「整表重算」闸门使用。

    ## 为什么不直接用 `watchlist_refresh_window()` 的返回值

    那个函数的时钟判据在 **09:15~09:30 竞价时段刻意失效**（见它自己的 docstring：
    当天第一笔成交前 tick 的 timetag 不会前进）。这个取舍对"交易日的早盘要不要
    刷新"是对的，但**它不能回答"今天到底开不开市"** —— 2026-09-25 中秋节
    （周五休市）正是靠这个缺口，在早盘窗口里把上一交易日的收盘信号又推了一遍。

    ## 这里的 False 只用来"少做一轮重算"，不用来拦推送

    `is_trading_day()` 在真交易日的 09:15~09:30 也会返回 False（竞价期时钟没推进），
    所以它**不适合**当推送闸门（会误杀早盘信号，那一职责由 `_push_allowed_now` +
    `SignalNotifier._stale_guard` 承担）。但用作"这一轮整表重算值不值得跑"没问题：

      - 真交易日竞价期误跳过一轮：没有损失 —— 竞价还没结束，自选股也没定下来；
      - 节假日跳过：正是要的效果（48 只 × 3~5 秒的 CPU 与随之而来的推送都没了）；
      - 时钟完全取不到时会 fail-open（`is_trading_day` 自身语义），下一轮照常。

    失败一律按"是交易日"处理：判错方向的代价只是多跑一轮（有缓存、成本可控），
    而误判成休市会让正常交易日漏掉整表刷新。
    """
    try:
        from src.intraday.auto_select import is_trading_day

        return bool(is_trading_day())
    except Exception as exc:  # noqa: BLE001 判定不可用 → 放行（fail-open）
        logger.debug("交易日判定失败，按交易日处理：%s", brief(exc, BRIEF_TIGHT))
        return True


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


#: 启动后多久**才允许自动整表重算**（秒）。见 `IntradayService._startup_grace_left`。
#:
#: 2026-09-18 实测：冷启动整表重算是 50 只票 × 3~5 秒的 CPU 活儿。即使用户首屏
#: 已经有热缓存（`hot_cache`），首屏那次**完整快照**（默认标的，冷取实测 7.5s）
#: 仍要和它抢同一个 GIL —— 实测首屏要 220~231 秒。推迟十几秒对任何信号都没有
#: 影响（自选分数本来就是分钟级），但能把首屏从"几分钟"压回"一次快照的时间"。
_WATCHLIST_STARTUP_GRACE = 20.0

#: 整表重算的最短间隔（秒）：避免"刷新循环"与"读请求触发的后台重算"在同一轮里
#: 各跑一遍（实测冷启动时是双份工作，每份 50 只 × 3~5 秒）。
_FULL_RECOMPUTE_MIN_INTERVAL = 30.0

#: 完全没有缓存时，首屏那次 `/watchlist` **最多等多久**（秒）。
#: 等到了给真数据；等不到就先给占位列表，重算在后台继续 ——
#: 冷启动 50 只实测 199 秒，绝不能让它挂住首屏（用户目标：10 秒内可用）。
_COLD_WATCHLIST_WAIT = 3.0

#: 全市场量能预测在**逐票快照**里最多等多久（秒）。
#:
#: 见 `IntradayService._market_turnover`：它要打 3~4 个腾讯接口，而自选池 46 只
#: 每只票都会取一次快照。等不到就不显示这一段的结论（provider 的 30 秒缓存
#: 在后台继续填充），也不能让每只票都快照变慢。
_TURNOVER_WAIT = 2.5


class IntradayService:
    """做T辅助服务（进程内单例，挂在 Runtime 上）。"""

    #: 整表重算的并发上限（只数）。理由与实测见 `_gather_light_snapshots`。
    #: 取 1：实测事件循环最长只被占用 0.4s（50 只并发时是 36.5s），
    #: 而整表总耗时不变（那本来就是跑不掉的 CPU 时间）。
    _WATCH_COMPUTE_BATCH = 1

    #: 批间让路时长（秒）：把事件循环还给 HTTP 请求 / WebSocket 推送。
    _WATCH_COMPUTE_YIELD = 0.05

    #: 启动宽限期（秒）。实例属性而不是只读常量：测试要能置 0，
    #: 才能单独验证"盘中启动时循环第一轮就刷"这个窗口语义。
    _startup_grace_seconds = _WATCHLIST_STARTUP_GRACE

    #: 无缓存时首屏最多等整表重算多久（秒）。同样是实例属性，便于测试缩短。
    _cold_watchlist_wait = _COLD_WATCHLIST_WAIT

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
        # 个股绑定（关联板块/海外映射）**内存热map**：`None` = 还没热加载过。
        # 用户口径 2026-09-23：启动时 `warm_bindings()` 灌满，之后加自选是纯内存读。
        # 存 `None` 而不是 `{}` 是有意的 —— 那样 `binding_for` 能区分
        # "还没加载"与"加载了但一只都没配"，前者要触发一次懒加载。
        self._bindings: dict[str, tuple[list[str], list[str]]] | None = None
        # 股性画像缓存：日线级特征，同一交易日内不必重算（键=代码，值=(时间戳, 画像)）
        self._character_cache: dict[str, tuple[float, CharacterProfile]] = {}
        # 档位拟合缓存：键=代码，值=(交易日, 单调时钟, 拟合结果)。
        # 拟合是 CPU 密集的（网格搜索 + 小网络，实测 0.3~2 秒/票），
        # 因此**整天只算一次**：盘中每 15 秒推一次快照，不能每次都重训。
        self._fit_cache: dict[str, tuple[str, float, Any]] = {}
        # 筹码量能快照缓存（供拟合的 7 个客观维度取 volume_ratio）
        self._chip_cache: dict[str, Any] = {}
        # 自选池概览缓存 + 盘中自动刷新循环。
        # 缓存的意义：全表重算要为每只票各跑一次轻量快照（实测整表 ~1.1s 且每只票
        # 都要打一次数据源），前端每分钟取一次若都重算，就会和刷新循环重复取数。
        self._watch_cache: tuple[float, list[WatchItem]] | None = None
        self._watch_lock = asyncio.Lock()
        # 单只增删自选期间置 True：让 `invalidate_watchlist_cache()` 早退，
        # 改由 `_invalidate_watch_entry()` 按"一只"的粒度动缓存。
        # 否则"加一只票"会放大成"整张缓存作废 + 全表重算"（实测 12.4s 空跑 /
        # 真实数据源约 200s）—— 见 `_invalidate_watch_entry` 的说明。
        self._watch_cache_single_change = False
        # 创建时刻与"上次整表重算完成时刻"（都取单调钟）：
        # - `_boot_at` 用于启动宽限期（见 `_WATCHLIST_STARTUP_GRACE`）；
        # - `_full_recompute_at` 用于去重（见 `_FULL_RECOMPUTE_MIN_INTERVAL`）。
        self._boot_at = time.monotonic()
        self._full_recompute_at = 0.0
        # 冷启动（无缓存）时那次整表重算的任务句柄：多个请求共享同一次计算。
        self._cold_task: asyncio.Task[list[WatchItem]] | None = None
        # 版本号：每写一次概览缓存 +1（增删自选也 +1）。WS 推送据此判断"有没有新版"，
        # 否则每 15 秒都会把同一份列表重复推给前端。
        self._watch_generation = 0
        # ---- 多用户「按用户分片的自选概览缓存」**已随自选池功能一起删除** ----
        #
        # 用户口径 2026-09-23："我的自选池很鸡肋，不需要了"。那份按
        # `(tenant_id, user_id)` 分片的缓存只服务于"某用户自己的池"这条路径；
        # 「加自选/删自选」一直是直接写 `configs/intraday.yaml`（共享清单，
        # 见 `IntradayTPanel.addToWatch`），因此删掉它没有动到日常路径。
        # 现在只有下面这一份共享概览缓存。
        self._refresh_task: asyncio.Task[None] | None = None
        # 估值结论缓存：键=代码，值=(单调钟, label, bucket)。
        #
        # 用户口径 2026-09-23：估值结论从主区域整块面板收成自选列表里的一个标签，
        # 因此每轮自选重算都要拿到它。PE/PB 是**日频**序列，一天之内不会变，
        # 但它要经采集链取「三年日频序列」——实测冷取 1.3~1.9 秒/票，
        # 45 只就是一分多钟，绝不能每分钟重付一遍（整表重算本身已经约 200 秒）。
        # 用 `config.data.valuation_cache_ttl`（默认 3600 秒）兜住：
        # 同一小时内每只票只取一次，之后是纯内存命中。
        # warm 情况下 0.00~0.08 秒/票（采集链/本地库命中），整表只多付几秒。
        self._valuation_cache: dict[str, tuple[float, str, str]] = {}
        # 正在后台补算估值的代码（去重）与其任务强引用集合。
        self._valuation_refreshing: set[str] = set()
        self._valuation_tasks: set[asyncio.Task[Any]] = set()
        # 全市场成交额预测量能（沪深京三市，同时间同比昨日）。
        #
        # 用户口径 2026-09-23：在「市场环境」行右侧给出「缩量XX亿 不追高 /
        # 放量XX亿 可做T」。它与情绪周期**并列**进入同一枚徽标（见 `_cycle_decision`）。
        #
        # ⚠️ 它一次取数要打 3~4 个腾讯接口，而自选池会给**每只票**各取一次快照
        # （46 只）。所以这里做两层保护：
        #   ① provider 内部的 TTL 缓存（30 秒，全市场口径不需要更快）；
        #   ② `_turnover_task` 单飞 —— 并发调用共享同一次取数，且**最多等
        #      `_TURNOVER_WAIT` 秒**，等不到就不显示这一段（宁可少一段结论，
        #      也不能把每只票的快照都拖慢）。
        self._turnover = MarketTurnoverProvider()
        self._turnover_task: asyncio.Task[Any] | None = None
        self._turnover_tasks: set[asyncio.Task[Any]] = set()
        # 自动选股循环 + 最近一轮结果（窗口 9:25-9:40 / 14:45-15:00 + 盘后一次）
        self._auto_select_task: asyncio.Task[None] | None = None
        self._auto_select_state: dict[str, Any] = {}
        # 后台重算（stale-while-revalidate）的进行中标记与任务强引用
        self._watch_refresh_inflight = False
        # "整表重算正在进行中"：区别于上面的"已排了一个后台重算任务"。
        # 刷新循环直接调 `watchlist(force=True)`，不设 `_watch_refresh_inflight`，
        # 只有这个标记能让"读请求触发的后台重算"看出循环正在跑、别叠加。
        self._full_recompute_inflight = False
        self._watch_refresh_tasks: set[Any] = set()
        # ── 轻量快照缓存：让「点开一只票」的首帧变成一次内存读 ────────────────
        # 背景：自选整表刷新循环本来就会给**每只票**算一份轻量快照
        # （`_gather_light_snapshots`，46 只 × 3~5 秒 CPU，一轮约 200 秒，
        # 而循环间隔 60 秒 —— 等于**一直在算**）。原先算完只拿去拼自选列表就丢了，
        # 用户点开某只票时再从头算一遍，于是与循环抢同一份 CPU：
        # 实测（2026-09-24）稳态下「未开过的自选票」轻量快照要 5~17 秒，
        # 用户报障"点中控技术分时图六七秒才出来"。
        # 现在把循环算出来的那份按代码留一份，交互首帧直接命中（毫秒级），
        # 随后 WS 的完整帧照常覆盖 —— 图先出来，其余面板后补。
        self._light_cache: dict[str, tuple[float, Any]] = {}
        # TTL 取 10 分钟：循环一轮（46 只 × 3~5 秒 CPU）本身要 150~230 秒，
        # 而循环间隔只有 60 秒 —— 排在队尾的票两次刷新之间就可能隔 3 分钟以上，
        # TTL 太短等于"刚填上就过期"。首帧用一份几分钟前的轻量快照是可接受的：
        # 它只负责"图先出来"，紧接着的完整帧与 15 秒一次的 WS 推送会立刻纠正。
        self._LIGHT_CACHE_TTL = 600.0
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
        # 最近被**点开过**的日K标的（有界 LRU，最近在前）。
        # 为什么要记它（`CHG-0099`）：日K预热的覆盖原先只有自选池，而用户点开的票
        # 可能来自搜索 / 量化选股 / 板块入口 —— 那些票第一次仍然是冷加载（实测
        # 4.19 s），之后就**应该**被预热。记"点开"行为是最便宜的信号：
        # 零配置、不需要枚举全市场，也不猜"用户会看什么"。
        self._recent_daily: dict[str, float] = {}
        # 上次关停前的自选概览快照（热加载）：文件路径 + 已加载标记。
        # 冷启动首个 /watchlist 要 59.8 秒（26~39 只票全量取数），热加载让首屏
        # 0 秒出数据、新鲜度交给后台重算 —— 详见 src/intraday/hot_cache.py。
        self._snapshot_dir = str(
            getattr(getattr(self._config, "data", None), "hot_cache_dir", "")
            or hot_cache.DEFAULT_CACHE_DIR)
        self._snapshot_loaded = False
        self._snapshot_state: dict[str, Any] = {
            "loaded": False, "reason": "", "count": 0, "saved_at": 0.0,
            "age_seconds": 0.0,
        }
        # ── 单票**完整快照**的落盘热加载（重启后首屏毫秒级出图）─────────────
        # 上面那份热加载只管"自选概览"（几十条行摘要）；用户重启后真正盯的是
        # **当前这一只票**的四面板完整快照 —— 它要跑完整取数链，冷进程实测
        # QMT 在时 6.8 秒、QMT 关闭后 11.1 秒，首屏就被这一项卡住。
        # 现在每次算完按代码落盘（`hot_cache.save_snapshot_payload`），重启后
        # **第一次**请求先返回这份旧快照（面板上写明"重启前缓存，正在刷新"），
        # 后台真算一遍覆盖 —— 首屏从 11 秒降到毫秒级。
        #
        # `_snapshot_fresh_at` 是关键：它记"本进程已经给这只票算过新鲜快照"，
        # 一旦算过就**不再吃落盘缓存**（稳态下重复快照只要 ~3 秒，没必要
        # 拿旧数据糊弄已经热起来的进程）。
        self._snapshot_fresh_at: dict[str, float] = {}
        self._snapshot_refresh_inflight: set[str] = set()
        self._snapshot_refresh_tasks: set[asyncio.Task[Any]] = set()
        #: 单票快照热加载的最大可用时长（秒）。与自选概览同口径 24 小时：
        #: 更旧的一律不加载（宁可等一次真算，也不把昨天的分时当今天的画）。
        self._snapshot_payload_max_age = hot_cache.DEFAULT_MAX_AGE_SECONDS

    @property
    def config(self) -> IntradayConfig:
        return self._config

    # ----------------------------------------------------------------
    # 上次关停前缓存的热加载（重启后首屏立刻有数据）
    # ----------------------------------------------------------------

    def load_cached_watchlist(self, *, max_age_seconds: float | None = None) -> dict[str, Any]:
        """把上一轮落盘的自选概览塞进缓存（**标为过期**），返回加载状态。

        为什么标成过期：`watchlist()` 里已有这条分支 ——

            if not force and cached is not None:
                if ttl <= 0 or time.monotonic() - cached[0] >= ttl:
                    self._schedule_watchlist_refresh()      # 后台重算
                return self._apply_quote_overlay(cached[1][:limit])

        把时间戳设成"很久以前"就自动命中它：**首请求立刻返回旧列表，
        同时后台起一次真实重算**。不需要为热加载新增返回路径，
        也就不会和既有的 stale-while-revalidate 逻辑分叉。

        幂等：同一进程重复调用只在第一次真正读盘。
        """
        if self._snapshot_loaded:
            return dict(self._snapshot_state)
        self._snapshot_loaded = True
        try:
            config = self._reload_config()
            codes = [item.code for item in config.watchlist]
        except Exception as exc:  # noqa: BLE001 配置读不到就别加载
            self._snapshot_state = {
                "loaded": False, "reason": f"自选配置不可读：{brief(exc, BRIEF_TIGHT)}",
                "count": 0, "saved_at": 0.0, "age_seconds": 0.0,
            }
            return dict(self._snapshot_state)

        kwargs: dict[str, Any] = {"cache_dir": self._snapshot_dir}
        if max_age_seconds is not None:
            kwargs["max_age_seconds"] = max_age_seconds
        outcome = hot_cache.load_snapshot(known_codes=codes or None, **kwargs)
        if not outcome.loaded:
            self._snapshot_state = {
                "loaded": False, "reason": outcome.reason, "count": 0,
                "saved_at": outcome.saved_at, "age_seconds": outcome.age_seconds,
            }
            logger.info("自选概览热加载未生效：%s", outcome.reason)
            return dict(self._snapshot_state)

        items: list[WatchItem] = []
        for raw in outcome.items:
            try:
                item = WatchItem(**raw)
            except Exception:  # noqa: BLE001 单条坏了就丢这条，不影响其它
                continue
            # 估值结论**不跟着热缓存走**：它是"这一小时算出来的"，落盘的那条可能
            # 是十几小时前的（快照最长可用 24 小时）。热缓存的契约是"先给旧的价格
            # 与分数，后台补新的"，而价格/分数都带 `quote_ts` 让前端标明新鲜度；
            # 估值标签只有四个字、没有时间戳可挂 —— 显示隔夜的估值档次会被当成此刻的。
            # 因此这里清空，由随即触发的后台重算补上（估值有 1 小时 TTL 缓存，
            # 补这一轮最多几十秒）。
            item.valuation_label = ""
            item.valuation_bucket = ""
            items.append(item)
        if not items:
            self._snapshot_state = {
                "loaded": False, "reason": "快照条目无法反序列化", "count": 0,
                "saved_at": outcome.saved_at, "age_seconds": outcome.age_seconds,
            }
            return dict(self._snapshot_state)

        # 时间戳取"很久以前" → 下一次 `watchlist()` 立即返回它并触发后台重算
        self._watch_cache = (time.monotonic() - 1e6, sort_watch_items(items))
        self._watch_generation += 1
        self._snapshot_state = {
            "loaded": True, "reason": outcome.reason, "count": len(items),
            "saved_at": outcome.saved_at, "age_seconds": outcome.age_seconds,
            "dropped": list(outcome.dropped_codes),
        }
        logger.info(
            "自选概览热加载成功：%d 条（%.1f 分钟前的数据，已标记为过期 → "
            "首请求立即可用并自动触发后台重算）%s",
            len(items), outcome.age_seconds / 60,
            f"，丢弃已不在自选池的 {outcome.dropped_codes}" if outcome.dropped_codes else "")
        return dict(self._snapshot_state)

    def _persist_watch_snapshot(self, items: list[WatchItem]) -> None:
        """把当前概览落盘（供下次启动热加载）。失败只记日志。"""
        if not items:
            return
        hot_cache.save_snapshot(items, cache_dir=self._snapshot_dir)

    # ----------------------------------------------------------------
    # 单票完整快照的落盘热加载（重启后首屏毫秒级出图）
    # ----------------------------------------------------------------

    def _snapshot_fingerprint(self, code: str, config: IntradayConfig) -> str:
        """口径指纹：权重/阈值/档位/因子参数/自选绑定任一变化 → 旧快照作废。

        为什么必须有这一道：`configs/intraday.yaml` 是**按 mtime 热重载**的，
        用户改完权重不用重启。若还把重启前的快照端上去，面板上的分数是旧口径算的
        —— 正是"改了权重但分数没变"那种最容易被当成 bug 的现象。

        `config.snapshot()` 是确定性字典（无时间戳，实测），直接拿它做材料即可。
        自选绑定（关联板块/海外映射/同业）影响板块类与海外类维度，也要进指纹。
        """
        try:
            material = {
                "config": config.snapshot(),
                "boards": list(config.board_names(code)),
                "overseas": list(config.overseas_for(code)),
                "peers": list(getattr(config.watch(code), "peers", []) or []),
            }
            blob = json.dumps(material, ensure_ascii=False, sort_keys=True,
                              default=str)
        except Exception as exc:  # noqa: BLE001 指纹算不出来就当作"无法校验"
            logger.info("快照口径指纹计算失败(%s)：%s",
                        code, brief(exc, BRIEF_TIGHT))
            return ""
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]

    def _load_persisted_snapshot(self, code: str,
                                 config: IntradayConfig) -> IntradaySnapshot | None:
        """读这只票上次落盘的完整快照（标为"重启前缓存"，附刷新中说明）。

        不可用时返回 None，调用方按原有路径真算 —— 热加载永远是**优化**，
        不能成为新的失败点。
        """
        outcome = hot_cache.load_snapshot_payload(
            code, cache_dir=self._snapshot_dir,
            fingerprint=self._snapshot_fingerprint(code, config),
            max_age_seconds=self._snapshot_payload_max_age)
        if not outcome.loaded:
            logger.info("单票快照热加载未生效(%s)：%s", code, outcome.reason)
            return None
        try:
            snapshot = IntradaySnapshot.model_validate(outcome.payload)
        except Exception as exc:  # noqa: BLE001 结构对不上就当没有
            logger.info("单票快照反序列化失败(%s)：%s",
                        code, brief(exc, BRIEF_TIGHT))
            return None
        snapshot = snapshot.model_copy(deep=True)
        # **不伪造新鲜度**：原样保留 generated_at / trade_date / health.stale /
        # session_label（前端本来就按它们显示"更新 14:31:02（快照 14:30:58）"
        # 与"非当日"警告），并额外在 gaps 里把"这是重启前缓存"写明。
        age = outcome.age_seconds
        age_text = (f"{age / 60:.0f} 分钟前" if age < 3600
                    else f"{age / 3600:.1f} 小时前")
        snapshot.health.gaps.append(
            f"⚠️ 本面板是**上次运行缓存的快照**（{age_text}生成于 "
            f"{snapshot.generated_at}），正在后台刷新，完成后自动覆盖 —— "
            "分数/信号/信号三角请以刷新后的为准")
        logger.info("单票快照热加载成功(%s)：%s", code, outcome.reason)
        return snapshot

    def _schedule_snapshot_refresh(self, code: str) -> None:
        """后台把这只票的完整快照真算一遍（去重；不阻塞请求）。

        用 `force_refresh=True`：热加载上来的是旧数，必须**绕过数据源 TTL 缓存**
        重新取一遍，否则"刷新"只是把同一份旧数又算一遍。
        """
        if code in self._snapshot_refresh_inflight:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:      # 无事件循环（同步调用）时不做后台刷新
            return
        self._snapshot_refresh_inflight.add(code)

        async def _refresh() -> None:
            try:
                await self.snapshot(code, force_refresh=True)
            except Exception as exc:  # noqa: BLE001 后台失败只记日志
                logger.warning("后台刷新单票快照失败(%s)：%s",
                               code, brief(exc, BRIEF_DEFAULT))
            finally:
                self._snapshot_refresh_inflight.discard(code)

        # 任务强引用必须留下：asyncio 只持弱引用，被 GC 掉会在运行中静默取消
        task = loop.create_task(_refresh(),
                                name=f"intraday-snapshot-refresh-{code}")
        self._snapshot_refresh_tasks.add(task)
        task.add_done_callback(self._snapshot_refresh_tasks.discard)

    def _remember_snapshot(self, code: str, snapshot: IntradaySnapshot,
                           config: IntradayConfig) -> None:
        """记下"本进程已算过这只票的新鲜快照"，并落盘供下次重启热加载。"""
        self._snapshot_fresh_at[code] = time.monotonic()
        # 只留最近若干只：自选轮动久了不该让这个字典无限长大
        if len(self._snapshot_fresh_at) > 200:
            self._snapshot_fresh_at.clear()
            self._snapshot_fresh_at[code] = time.monotonic()
        if not self._snapshot_is_worth_caching(snapshot):
            return
        try:
            payload = snapshot.model_dump(mode="json")
        except Exception as exc:  # noqa: BLE001 序列化失败就不落盘
            logger.info("单票快照序列化失败（不落盘）：%s",
                        brief(exc, BRIEF_TIGHT))
            return
        hot_cache.save_snapshot_payload(
            code, payload, cache_dir=self._snapshot_dir,
            fingerprint=self._snapshot_fingerprint(code, config),
            trade_date=snapshot.trade_date)

    @staticmethod
    def _snapshot_is_worth_caching(snapshot: IntradaySnapshot) -> bool:
        """只把**算全了**的快照落盘（几分钟的图 vs 一次重启的首屏）。

        为什么要有这道闸（2026-09-24 实测踩到）：落盘的这份会在下次启动时被
        **当作首屏第一帧**端给用户。若某次快照是降级的（数据源全挂时快照照样
        生成，只是 trend/bars 为空、分数为 None），把它缓存下来就等于
        "重启后先给用户看一屏空白图"。宁可这一轮不缓存（下次启动多等一次真算），
        也不让空图占住首屏。

        判据取"有分时序列 + 有快照行情"：这两项在取数链正常时一定有
        （`trend` 是前端主图的数据源，`quote` 是所有面板的前提）。
        """
        return bool(snapshot.trend) and snapshot.quote is not None
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
            gateway_cache_ttl_hours=news.gateway_cache_ttl_hours,
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
            logger.debug("解析 %s 的名称失败：%s", code, brief(exc, BRIEF_TIGHT))
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

        # ---- 关联板块 / 海外映射：**已存的优先**（用户口径 2026-09-23）----
        #
        # 用户原话："后续不管删除自选还是新加自选，都可以自动加载之前的配置，
        # 当有手动输入关联板块或海外映射时，配置再跟随刷新。"
        #
        # 规则：**调用方（前端输入框）给了值就用给的**；没给（空）才回落到库里
        # 已存的绑定。于是：
        #   - 删了自选再加回来 → 输入框是空的 → 自动还原上次配的板块/映射；
        #   - 手动填了新的 → 用新的，并**回写**覆盖旧绑定（"配置跟随刷新"）。
        #
        # ⚠️ 这里读的是**内存热map**（见 `warm_bindings`），不查库 ——
        #    `add_watch` 是同步方法、且加一只票必须立刻返回，不能等 SQLite。
        saved_boards, saved_overseas = self.binding_for(code)
        final_boards = list(boards or []) or saved_boards
        final_overseas = list(overseas or []) or saved_overseas

        item = WatchConfig(
            code=code, name=resolved, boards=list(final_boards),
            peers=list(peers or []), industry=industry,
            overseas=list(final_overseas))
        # ⚠️ **单只粒度**：`_reset_components` 内部会调 `invalidate_watchlist_cache()`，
        # 那是"整张作废"的语义 —— 加一只票不该让另外 45 只的分数一起失效
        # （实测：整表重算 12.4s 空跑 / 真实数据源约 200s，而重算一只 0.08s）。
        # 这里用护栏让它早退，改由 `_invalidate_watch_entry()` 精确处理。
        #
        # 本方法是**同步**的（路由层直接调用），所以不能在这里等单只快照：
        # 只写入一行**身份字段**（与前端乐观插入同一套路），分数由前端随后那次
        # `force=true&active=<新票>` 触发 `_refresh_one_watch()` 补上。
        with self._watch_cache_single_change_guard():
            saved = upsert_watch(item, self._config_path, prepend=True)
            self._config = saved
            self._reset_components(saved)
        # 绑定落库 + 立刻更新内存热map（本次调用之后就该读得到新值）。
        self._remember_binding(code, final_boards, final_overseas)
        row = next((watch for watch in saved.watchlist
                    if watch.code == code), None)
        self._invalidate_watch_entry(code, fresh=WatchItem(
            code=code,
            name=(row.name if row and row.name != code else "") or self._lookup_name(code),
            boards=list(row.boards) if row else list(boards or []),
            pinned=bool(getattr(row, "pinned", False))))
        return saved

    @contextmanager
    def _watch_cache_single_change_guard(self):
        """在"单只增删自选"期间抑制"整张缓存作废"（见 `_invalidate_watch_entry`）。"""
        previous = getattr(self, "_watch_cache_single_change", False)
        self._watch_cache_single_change = True
        try:
            yield
        finally:
            self._watch_cache_single_change = previous

    # ==================== 个股绑定（关联板块 / 海外映射） ====================
    #
    # 用户口径 2026-09-23："加自选时把关联板块、海外映射保存到数据库里，
    # 后续无论删自选还是新加自选都自动加载之前的配置。这个配置加载最好自动联想，
    # **启动系统时就热加载**，避免加自选时无法迅速读取。"

    def warm_bindings(self, *, force: bool = False) -> int:
        """把"已配过板块/映射的票"一次性灌进内存（**启动时调用**）。

        为什么必须预热：`add_watch` 是**同步**方法（路由直接调用、加一只票要立刻
        返回），它没法 `await` 一次 SQLite 查询。把这批数据常驻内存后，
        加自选就是纯内存读 —— 这正是用户要的"迅速读取，别等库"。

        Returns:
            这次装进内存的条数；没注入档案仓储时返回 0（降级不报错）。
        """
        if self._bindings is not None and not force:
            return len(self._bindings)
        rows: dict[str, tuple[list[str], list[str]]] = {}
        repo = self._profile_repo
        if repo is not None and hasattr(repo, "all_bindings"):
            try:
                rows = dict(repo.all_bindings())
            except Exception as exc:  # noqa: BLE001 绑定是增强项，读不到就空跑
                logger.warning("个股绑定热加载失败（降级为空）：%s",
                               brief(exc, BRIEF_TIGHT))
                rows = {}
        self._bindings = rows
        if rows:
            logger.info("个股绑定已热加载 %d 只（关联板块/海外映射）", len(rows))
        return len(rows)

    def binding_for(self, code: str) -> tuple[list[str], list[str]]:
        """取某只票已存的 `(关联板块, 海外映射)`；没配过 → 两个空列表。

        首次调用会**顺带**热加载一次（懒预热），避免"忘了调 `warm_bindings`
        就整条功能静默失效"——启动路径与运行路径都能自愈。
        """
        if self._bindings is None:
            self.warm_bindings()
        rows = self._bindings or {}
        return rows.get(str(code).strip().zfill(6), ([], []))

    def all_bindings(self) -> dict[str, tuple[list[str], list[str]]]:
        """全部已配绑定（前端"联想下拉提示历史配置"用）。"""
        if self._bindings is None:
            self.warm_bindings()
        return dict(self._bindings or {})

    def _remember_binding(self, code: str, boards: list[str],
                          overseas: list[str]) -> None:
        """内存热map + 数据库**双写**（内存先行，保证本次调用之后立刻读得到）。

        ⚠️ 落库失败**只告警不抛**：绑定是增强项，而"加自选"是主功能 ——
        不能因为写绑定失败就让用户加不进自选（自选本身已写进 YAML）。
        """
        key = str(code).strip().zfill(6)
        if self._bindings is None:
            self.warm_bindings()
        if self._bindings is not None:
            self._bindings[key] = (list(boards), list(overseas))
        repo = self._profile_repo
        if repo is None or not hasattr(repo, "set_binding"):
            return
        try:
            repo.set_binding(key, boards, overseas)
        except Exception as exc:  # noqa: BLE001 见上：不阻断加自选
            logger.warning("个股绑定落库失败(%s)：%s", key, brief(exc, BRIEF_TIGHT))

    def add_watch_many(self, items: list[dict[str, str]]) -> dict[str, Any]:
        """批量加入自选池：**一次落盘、一次重建子组件**。

        ## 为什么不循环调 `add_watch`

        `add_watch` 每次都走 `upsert_watch` → `save_watchlist`（写文件 + 校验 +
        全量 reload）再 `_reset_components`（重建 5 个子组件 + 作废自选概览缓存）。
        「一键全部加自选」有 20 只票就是 20 次落盘、20 次重建 ——
        实测这一类重复重建最贵的部分是随后的自选概览**整表重算**（冷启动约 114 秒），
        前端表现就是点完按钮卡住一段时间。合并成一次后只有一次重建。

        ## 已经在自选池里的票**不动**

        `upsert_watch` 是**整条替换**（`WatchConfig` 含 `boards/peers/industry/overseas`），
        重复加入会把用户手工配过的板块/海外映射清空。所以这里：
        已有票**原样保留**；只有当它的名字是空的（历史事故留下的 `name: "603083"` 那类）
        才顺手把名字补上，其余字段仍然不动。

        返回按结果分类，**不谎报成功**：写不进去的（代码格式错）进 `failed`，
        名称解析不出来的进 `missing_name`。
        """
        from src.intraday.config import WatchConfig, load_intraday_config, save_watchlist

        config = load_intraday_config(self._config_path)
        merged = list(config.watchlist)
        position = {item.code: index for index, item in enumerate(merged)}
        added: list[str] = []
        repaired: list[str] = []
        existing: list[str] = []
        failed: list[dict[str, str]] = []
        missing_name: list[str] = []
        seen: set[str] = set()

        for raw in items or []:
            code = str((raw or {}).get("code") or "").strip().split(".")[0]
            if not code.isdigit() or len(code) != 6:
                failed.append({"code": str((raw or {}).get("code") or ""),
                               "reason": "代码格式不对（要 6 位数字）"})
                continue
            if code in seen:
                continue
            seen.add(code)
            name = str((raw or {}).get("name") or "").strip()
            if not name or name == code:
                name = self._lookup_name(code)
            if code in position:
                current = merged[position[code]]
                if not current.name and name:
                    merged[position[code]] = current.model_copy(update={"name": name})
                    repaired.append(code)
                else:
                    existing.append(code)
                continue
            if not name:
                # ⚠️ 名称解析不出来时**仍然加进去**（票是该在池子里的），
                # 但要把代码报给前端：`dump_watchlist_block` 写盘时用的是
                # `item.name or item.code`，所以这种条目在配置文件里会显示成代码
                # （历史事故里那种"界面上认不出是什么公司、还不报错"的状态）。
                # 用 `missing_name` 把它显式说出来，用户才有机会手工补名。
                missing_name.append(code)
            position[code] = len(merged)
            merged.append(WatchConfig(code=code, name=name))
            added.append(code)

        if not added and not repaired:
            return {"saved": False, "requested": len(items or []),
                    "added": [], "repaired": [], "existing": existing,
                    "failed": failed, "missing_name": missing_name,
                    "total": len(config.watchlist)}

        saved = save_watchlist(merged, self._config_path)
        self._reset_components(saved)
        logger.info("批量加自选：新增 %d、补名 %d、已在池中 %d、失败 %d",
                    len(added), len(repaired), len(existing), len(failed))
        return {"saved": True, "requested": len(items or []),
                "added": added, "repaired": repaired, "existing": existing,
                "failed": failed, "missing_name": missing_name,
                "total": len(saved.watchlist)}

    def remove_watch(self, code: str) -> IntradayConfig:
        """从自选池移除标的并写回 configs/intraday.yaml（幂等）。

        ## 为什么删除后要"精确更新缓存"而不是整个作废

        原实现走 `_reset_components()` → `invalidate_watchlist_cache()`，
        把概览缓存**置空**。置空的后果是下一次 `/watchlist` 请求要**整表重算**
        （实测 28 只票冷启动约 114 秒），而前端删完立刻回读列表 ——
        正好撞上这次重算，用户看到的就是"删一只票要等好几秒"。

        现在：配置与子组件照常重置，但概览缓存**按代码精确剔除** ——
        列表其余部分完全有效，没必要重算。

        ⚠️ 上面这段设计原先**没有生效**：`_reset_components()` 会先调
        `invalidate_watchlist_cache()` 把 `_watch_cache` 置空，于是按旧缓存做剔除的
        分支永远走不到，"精确剔除"是一段死代码。2026-09-23 用
        `_watch_cache_single_change_guard()` 抑制那次整张作废后才真正生效。
        """
        from src.intraday.config import remove_watch as remove_from_yaml

        # ⚠️ 2026-09-23 修：这段"精确剔除"**过去一直是死代码** ——
        # `_reset_components()` 先把 `_watch_cache` 置空，于是按 `cached` 做剔除
        # 的分支永远不成立，"删一只票"照样退化成"下次读触发整表重算"。
        # 现在用护栏抑制"整张作废"，剔除才真正生效。
        target = str(code).strip().split(".")[0].zfill(6)
        # 先记下"缓存里有没有它"：`remove_watch` 是**幂等**的，删一个不存在的
        # 代码必须**什么都不做** —— 既不动缓存对象、也不递增代次（否则前端会收到
        # 一次无意义的"列表变了"推送）。以**缓存**为准而不是 `_reload_config()`：
        # 缓存才是前端看到的列表，而配置可能已被外部改写。
        cached_codes = ({item.code for item in self._watch_cache[1]}
                        if self._watch_cache is not None else set())
        with self._watch_cache_single_change_guard():
            saved = remove_from_yaml(code, self._config_path)
            self._config = saved
            self._reset_components(saved)
        if target in cached_codes:
            self._invalidate_watch_entry(target)
            logger.info("自选概览缓存已精确剔除 %s（无需重算整表）", target)
        return saved

    def set_watch_pinned(self, code: str, pinned: bool) -> IntradayConfig:
        """置顶/取消置顶一只自选（写回配置文件；幂等）。

        置顶状态**存在配置里**而不是前端 localStorage：换浏览器、换机器应该一致，
        而且服务端的自动刷新循环也要按同样顺序产出列表（否则前端每次轮询回来
        顺序又变回去）。
        """
        from src.intraday.config import WatchConfig, upsert_watch

        target = str(code).strip().split(".")[0].zfill(6)
        config = self._reload_config()
        current = config.watch(target)
        if current is None:
            raise ConfigError(f"自选池里没有 {target}，无法置顶")
        if bool(current.pinned) == bool(pinned):
            return config                       # 幂等：状态没变就不写盘
        item = WatchConfig(
            code=current.code, name=current.name, boards=list(current.boards),
            industry=current.industry, peers=list(current.peers),
            overseas=list(current.overseas), pinned=bool(pinned))
        saved = upsert_watch(item, self._config_path)
        self._config = saved
        # ⚠️ **不调 `_reset_components()`**：置顶只改一个展示顺序字段，
        # 数据源/板块/估值 provider 完全不关心它；而 `_reset_components` 会
        # `invalidate_watchlist_cache()` 把概览缓存**置空** → 下次读取整表重算
        # （实测"取消置顶"因此要 13.31s）。只更新配置与缓存里的那一条即可。
        cached = self._watch_cache
        if cached is not None:
            for entry in cached[1]:
                if entry.code == target:
                    entry.pinned = bool(pinned)
            self._watch_cache = (time.monotonic(), sort_watch_items(list(cached[1])))
            self._watch_generation += 1
        logger.info("自选置顶%s：%s", "已设置" if pinned else "已取消", target)
        return saved

    async def warm_board_snapshots(self) -> dict[str, int]:
        """**一次子进程**把自选池涉及的所有板块快照预热进缓存（启动后调用）。

        为什么要预热：自选 26 只票涉及几十个板块，逐个取要几十次子进程
        （每次约 1s 固定成本）→ 整表重算 110 秒。启动后先用**一个**子进程
        把它们全取回来，之后每只票的快照都命中缓存，整表重算的板块部分
        从"几十次进程启动"降到"零"。

        在后台线程池里跑（`asyncio.to_thread` 不需要，子进程本身是异步等待），
        失败只记日志 —— 预热是**优化**，不该影响任何请求。
        """
        try:
            config = self._reload_config()
            names: list[str] = []
            for item in config.watchlist:
                for name in item.boards:
                    if name not in names:
                        names.append(name)
            if not names:
                logger.info("板块快照预热：自选池没有配置板块，跳过")
                return {}
            started = time.perf_counter()
            result = await self._board.warm_concept_snapshots(names)
            logger.info("板块快照预热完成：%d 个板块（%.1fs）",
                        len(result), time.perf_counter() - started)
            return result
        except Exception as exc:  # noqa: BLE001 预热失败不影响主链路
            logger.info("板块快照预热失败（不影响功能）：%s", brief(exc, BRIEF_DEFAULT))
            return {}

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
                       config_patch: IntradayConfig | None = None,
                       use_light_cache: bool = True) -> IntradaySnapshot:
        """组装做T辅助完整快照（前端四面板唯一数据源）。

        light=True 为「轻量快照」：只算行情 + 关键价位 + 七因子总分 + 信号，
        跳过估值空间、消息面 LLM 打分、板块分时与大盘指数。用于自选列表页
        （那里只要「哪只票现在触发了信号」），避免为 N 只标的各跑一次 LLM 与
        同业批量快照——实测完整快照首个请求曾达 54s，会明显拖慢交互。

        config_patch：前端「权重预览」用的**临时口径**（不落库、不影响这只票
        已有的档案）。给了它就完全按它算，不再解析个股覆盖 ——
        否则预览结果会变成"档案 + 你的改动"的叠加，用户看不出改动本身的效果。

        use_light_cache：`light=True` 时是否可以先吃「自选循环刚算好的那份」。
        交互路径（HTTP/WS 首帧）用默认 True：图要立刻出；生产路径
        （`_gather_light_snapshots` 本身）必须传 False，否则循环会一直读到自己的
        旧结果、自选列表再也不刷新。
        """
        if light and use_light_cache and not force_refresh and config_patch is None:
            cached = self._light_cache.get(code)
            if cached is not None:
                age = time.monotonic() - cached[0]
                if age <= self._LIGHT_CACHE_TTL:
                    logger.debug("轻量快照命中缓存 %s（%.1fs 前算的）", code, age)
                    # 浅拷贝后返回：调用方（含前端序列化）不该拿到循环里的同一份对象
                    return cached[1].model_copy()
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
            # 强制刷新是显式要求"现在这一刻"的数据：连"本进程已算过"的记录也清掉，
            # 免得后续非 force 请求又把刚被否定的那份当新鲜数据。
            self._snapshot_fresh_at.pop(code, None)
        # ---- 完整快照的落盘热加载（只服务"本进程还没算过这只票"的第一帧）----
        # 放在 config 解析之后：指纹要用**生效口径**（含个股权重档案）。
        # 交互路径（HTTP/WS 首帧）会命中它，冷启动首屏因此从 ~11 秒降到毫秒级；
        # 后台随后的真算会覆盖面板，并让后续请求走正常路径。
        if (not light and not force_refresh and config_patch is None
                and code not in self._snapshot_fresh_at):
            persisted = self._load_persisted_snapshot(code, config)
            if persisted is not None:
                self._schedule_snapshot_refresh(code)
                return persisted
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
            self._data.fetch_bars(code, days=self._intraday_days),
            self._data.fetch_trend(code),
            self._data.fetch_quote(code),
            self._fetch_daily_bars(code),
            # 市场情绪周期：自选池里所有标的共用同一份（Provider 内部按 TTL 缓存），
            # 放在基础任务里 —— 轻量快照也要它，因为它是「今天能不能做T」的闸门。
            self._market_cycle(),
            # 全市场成交额预测量能：与周期一样是"今天能不能做T"的闸门，
            # 轻量快照也要（自选列表的每一行都会带上它）。见 `_market_turnover`。
            self._market_turnover(),
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
                self._board.fetch_index(DEFAULT_INDEX_CODE),
                self._sentiment.analyze(
                    code=code, name=watch.name if watch else "",
                    limit=config.factors.news.max_items,
                    max_items=config.factors.news.max_items,
                    # 强制刷新必须一路穿到 LLM 网关（否则只会绕开进程内缓存，
                    # 磁盘缓存照样命中 → 前端看到"刷新了但数据没变"）
                    force=force_refresh),
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
        turnover_result = take("全市场量能预测")
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
        # 全市场量能预测（沪深京三市，同时间同比昨日）→ 并入顶部决策徽标。
        # ⚠️ 同样**不塞进 gaps**：它是操作结论（"缩量XX亿 不追高"），不是数据缺口。
        # 取不到时只是徽标里少一段，不在这里报"缺口"（否则每条自选都会多一行噪声）。
        turnover_forecast = (
            turnover_result if getattr(turnover_result, "available", False) else None)

        # 情绪周期的结论（一票否决 / 退潮期禁止回踩）**不再塞进 gaps**：
        # 它不是"数据缺口"，而是**决策依据** —— 用户口径 2026-09-23 要求把它
        # 显示到顶部「市场环境」行右侧（见 `cycle_decision`），
        # 而不是混在底部"数据健康度"的缺口列表里。
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
        # 供档位拟合取"筹码量能结构"这一维（拟合的 7 个客观维度之一）
        self._chip_cache[code] = chip_snapshot

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
        # 档位拟合结果与调整项乘数：即使本次没进打分分支（数据缺口）也要有定义，
        # 否则快照组装处会 UnboundLocalError（实测：无数据源时整条链路 500）。
        fit: LevelFitResult | None = None
        adjustment: dict[str, float] = {}
        markers: list[SignalMarker] = []
        score_series: list[ScorePoint] = []
        level_series: list[LevelPoint] = []
        if (today_features is not None and len(today_features)
                and quote is not None):
            raw_levels = self._compute_levels(
                quote=quote, today_features=today_features,
                daily_bars=daily_bars, config=config)
            # 只给**当前**这一份档位补来源标注（回踩线由箱体还是布林下轨决定、
            # 触发价在哪、档位差被护栏动过没有）：面板上那条横线要说得出出处，
            # 否则用户只能对着一个数字猜。逐bar的 levels_series 刻意不标 ——
            # 那是 240 个对象，标注字段会让 WS 每 15 秒推的载荷无谓变大。
            levels = annotate_level_basis(raw_levels, config)
            ctx = self._build_context(
                quote=quote, features=features, today_features=today_features,
                daily_bars=daily_bars, boards=scored_boards, news=news_obj,
                config=config, index_volume=index_volume, overseas=overseas,
                chan=chan_snapshot, chip=chip_snapshot, cycle=market_cycle,
                character=character)
            # ---- 神经网络拟合档位（7 个客观维度拟合 + 7 个调整项微调）----
            # 必须在算 levels_series 之前：图上那条漂移曲线与"此刻档位"必须同源，
            # 否则用户会看到曲线在 892、而右上角标签写 898（两个口径打架）。
            fit = await self._fit_levels_for(
                code=code, features=features, ctx=ctx,
                trade_date=trade_date, config=config, daily_bars=daily_bars)
            # 调整项乘数：其余 7 个维度按"作用面"归并成三个有界乘数（≤±50%）。
            # 打分核与打分卡**同一批函数**，因此这里不需要等打分卡算完；
            # 面板上改权重会经由 ctx 的因子参数一并生效（同源，不存在两套口径）。
            adjustment = (adjustment_factors(ctx, None, config.factors)
                          if fit is not None else {})
            levels = apply_level_fit(
                levels, fit, ctx, adjustment_scale=adjustment,
                blend=config.factors.level_fit.blend)
            # 逐bar档位：回放与画图都要按"当时那一刻"的档位，而不是当前值
            # （实测 300308 回踩线当日从 862 抬到 898、603083 从 215 抬到 222；
            #  拿当前值横贯全天会把早盘正常回踩误判成破位/误读成"开盘在回踩线下"）
            levels_series = replay_levels(
                today_features,
                box_high=levels.box_high, box_low=levels.box_low,
                box_span_days=levels.box_span_days, atr=levels.atr,
                config=config)
            if fit is not None and fit.metrics.available and adjustment:
                levels_series = [
                    apply_level_fit(item, fit, ctx, adjustment_scale=adjustment,
                                    blend=config.factors.level_fit.blend)
                    for item in levels_series]
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

        # ---- 分时卖点（量价关系）----
        #
        # 与日线 S 系列分工：S 系列判"这一段该不该持有"（日K），
        # 这里判"**此刻盘中该不该卖**"（分钟级 trend + VWAP）。
        #
        # 量能基数（"放天量"的分母）取近 N 日日均成交量；拿不到就不给
        # 放量判据（`sell_points` 会如实记 gap），不用当日量自我比较冒充。
        sell_verdict: dict[str, Any] | None = None
        try:
            from src.intraday import sell_points as _sell_points

            _avg_volume = await self._average_volume(code, trade_date, days=5)
            _verdict = _sell_points.detect_all(
                trend, prev_close=(quote.prev_close if quote else None),
                limit_up=(quote.limit_up if quote else None),
                avg_volume=_avg_volume,
                volume_ratio=None)
            sell_verdict = _verdict.to_dict()
            gaps.extend(_verdict.gaps)
        except ImportError:  # 公开版不含分时卖点模块，属预期
            logger.info("分时卖点模块未包含（商业版功能）")
        except Exception as exc:  # noqa: BLE001 卖点算不出来不该拖垮整个面板
            logger.warning("分时卖点计算失败（降级）：%s", exc)
            gaps.append(f"分时卖点不可用：{type(exc).__name__}: {exc}")

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
            cycle_decision=_cycle_decision(
                market_cycle, config, turnover=turnover_forecast),
            level_fit=(
                None if fit is None else {
                    **fit.to_dict(),
                    "adjustment": adjustment,
                    "applied": bool(
                        fit.metrics.available and fit.metrics.gate_passed),
                }),
            sell_points=sell_verdict,
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
            # ---- 推送闸门之二：这个时点不该推送 → 不发（2026-09-25 中秋事故）----
            # 闸门一（`SignalNotifier.should_push()` 的 stale 检查）才是主力防线：
            # 它判"这份数据属于哪一天"，节假日拿上一交易日收盘数据算出的信号
            # **任何时刻**都会被它拦下。这里再加一道**时点**兜底，覆盖 stale 判据
            # 失灵的情况（行情源给出假日期、快照来自旧版缓存等）。
            # ⚠️ 判据必须用 `_push_allowed_now()` 而不是 `is_trading_day()` ——
            # 后者在真交易日的 09:15~09:30 竞价期也会判 False（时钟还没推进），
            # 拿来当推送闸门会把每天最关键的早盘信号整段误杀（见该函数 docstring）。
            allowed, why = _push_allowed_now()
            if allowed:
                await self._push(snapshot, signal, trade_date=trade_date)
            else:
                logger.info(
                    "不推送（%s）：%s %s %s，数据日 %s，信号时间 %s，stale=%s",
                    why, snapshot.code, signal.kind, signal.strength,
                    trade_date or "?", signal.ts, stale)
        if light and use_light_cache:
            # 交互路径算出来的那份也留档：同一只票**第二次点开**就是毫秒级
            # （第一次仍要真算 —— 那份 CPU 躲不掉，但只需付一次）。
            self._light_cache[code] = (time.monotonic(), snapshot)
        if not light and config_patch is None:
            # 完整快照：记"本进程已算过这只票"并落盘，供下次重启热加载。
            # ⚠️ 权重预览（config_patch）**不能落盘**：那是临时口径，落盘会让
            # 重启后的首帧显示一份"不属于任何已保存口径"的分数。
            self._remember_snapshot(code, snapshot, config)
        return snapshot

    async def _push(self, snapshot: IntradaySnapshot, signal: Any, *,
                    trade_date: str = "") -> None:
        """推送（失败仅记录，不影响面板返回）。

        `trade_date` 透传给 `SignalNotifier.push()` —— 当日去重按**行情所属
        交易日**分桶，而不是本机日期（见 `notify_dedup` 模块）。
        """
        try:
            results, pushed = await self._notifier.push(
                snapshot, signal, trade_date=trade_date)
            signal.pushed = pushed
            snapshot.notifier["last_push"] = [r.model_dump() for r in results]
        except Exception as exc:  # noqa: BLE001
            logger.warning("做T推送异常(%s): %s", snapshot.code, brief(exc, BRIEF_DEFAULT))
            snapshot.notifier["last_push_error"] = brief(exc, BRIEF_DEFAULT)

    # ==================== 数据装配 ====================

    async def _fetch_daily_bars(self, code: str) -> tuple[pd.DataFrame | None, str]:
        """日线上下文（复用项目采集链：AkShare→腾讯→Tushare→baostock→本地CSV）。

        **带市场时钟做新鲜度下限**（`min_date`）：不带区间时路由会走"本地 DB 命中
        就直接返回"的短路，而盘中 DB 里最新就是**昨天** —— 于是分时的日线上下文
        （量价/均线/前期高低点）永远停在昨天，而日K面板（显式区间）却已经是当天的
        形成中bar，两条路的口径就分裂了（同一个 app 里两个"最新日线"）。
        下限取"行情源自己走到的交易日"：盘中=今天、盘前/节假日=上一交易日，
        取不到时是 None（fail-open，退回原行为）。
        """
        if self._backend is None:
            raise DataFetchError("日线上下文不可用：未注入数据采集链")
        # 延迟导入：与 `daily()` 里那条 `fetch_daily_snapshot` 的导入方式保持一致
        from src.intraday.daily import _session_min_date

        points = await self._backend.fetch(
            close_indicator(code), min_date=_session_min_date())
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
        items = None
        last_error: Exception | None = None
        for _attempt in (1, 2):
            try:
                items = await self._profile_repo.list(limit=500)
                break
            except Exception as exc:  # noqa: BLE001 档案库故障不该让做T面板整体不可用
                last_error = exc
        if items is None:
            logger.warning("读取做T权重档案失败（按上一次结果继续）：%s",
                           brief(last_error, BRIEF_DEFAULT))
            # ⚠️ **绝不把"读失败"缓存成"没有档案"**（2026-09-24 实测踩到）。
            # 原实现是 `self._profile_cache = (now, {})` —— 于是档案库抖一下，
            # 接下来 5 秒内所有快照都退回**全局口径**：有档案的票分数直接是错的，
            # 而且"单票快照落盘热加载"的口径指纹会因此与磁盘上那份对不上、
            # 整份缓存作废（表现为冷启动首屏偶尔从 1 秒退化成 6~11 秒）。
            # 失败只影响"这一次"：沿用上一次成功的结果（没有就空），不污染缓存，
            # 下一次调用立刻重试。所以上面还做了一次立即重试 —— 冷启动的第一次
            # 调用正是最要紧的那一次（它决定口径指纹）。
            if self._profile_cache is not None:
                return self._profile_cache[1]
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

    async def level_fit(
        self, code: str, *, refresh: bool = False,
        features: pd.DataFrame | None = None, daily_bars: pd.DataFrame | None = None,
        quote: Quote | None = None,
    ) -> LevelFitResult | None:
        """取这只票的档位拟合结果（按交易日缓存；`refresh=True` 强制重算）。

        为什么单独开一个入口而不是内联在快照里：拟合是 CPU 密集的（网格搜索 +
        小网络），必须能被"面板要求时才跑一次、之后整天复用"地调用。
        数据由调用方给（快照里本来就有），避免这里再取一次盘。
        """
        config = self._reload_config()
        moment = datetime.now()
        trade_date = moment.strftime("%Y-%m-%d")
        cached = self._fit_cache.get(code)
        if (not refresh and cached is not None
                and cached[0] == trade_date
                and (time.monotonic() - cached[1]) < config.factors.level_fit.cache_seconds):
            return cached[2]
        if features is None or daily_bars is None or quote is None:
            # 没有现成数据就不在这里重新取数（避免把慢取数引进工具链）
            return cached[2] if cached is not None else None
        try:
            fit = await asyncio.to_thread(
                self._fit_levels_sync, code=code, features=features,
                daily_bars=daily_bars, quote=quote, config=config,
                trade_date=trade_date)
        except Exception as exc:  # noqa: BLE001 拟合失败不该让整个接口 500
            logger.warning("档位拟合失败(%s): %s", code, brief(exc, BRIEF_DEFAULT))
            return None
        self._fit_cache[code] = (trade_date, time.monotonic(), fit)
        return fit

    def _fit_levels_sync(
        self, *, code: str, features: pd.DataFrame, daily_bars: pd.DataFrame,
        quote: Quote, config: IntradayConfig, trade_date: str,
    ) -> LevelFitResult:
        """同步拟合（在 `asyncio.to_thread` 里跑，不阻塞事件循环）。"""
        cfg = config.factors.level_fit
        atr_pct = None
        if daily_bars is not None and len(daily_bars) >= 15:
            atr_series = ind.atr(daily_bars)
            atr_value = ind.last_value(atr_series)
            if atr_value is not None and quote.price:
                atr_pct = float(atr_value) / float(quote.price) * 100.0
        bars = _bars_frame(features)
        frame = bars.copy()
        if "ts" not in frame.columns and len(features):
            frame["ts"] = features["ts"].astype(str).to_numpy()
        box_high = box_low = None
        if daily_bars is not None and len(daily_bars) >= 5:
            box_high, box_low, _span = ind.box_levels(
                daily_bars, lookback=config.factors.box.lookback_days)
        last = features.iloc[-1] if len(features) else None
        dataset = build_dataset(
            bars=frame,
            day_low=None if last is None else _finite(last.get("low")),
            day_high=None if last is None else _finite(last.get("high")),
            pct_b=None if last is None else _finite(last.get("pct_b")),
            bandwidth=None if last is None else _finite(last.get("bandwidth")),
            dev_z=None if last is None else _finite(last.get("dev_z")),
            chip_volume_ratio=self._last_chip_ratio(code),
            box_position=(None if None in (box_high, box_low) or quote.price is None
                          else ind.box_position(quote.price, box_high, box_low)),
            chan_position=None,
            macd_atr=self._macd_atr(last, quote.price, atr_pct),
            kdj_rsi=self._kdj_rsi(features, config),
            atr_pct=atr_pct, cfg=cfg)
        return fit_levels(code=code, dataset=dataset, cfg=cfg,
                          trade_date=trade_date)

    def _last_chip_ratio(self, code: str) -> float | None:
        snapshot = self._chip_cache.get(code)
        return None if snapshot is None else snapshot.volume_ratio

    @staticmethod
    def _macd_atr(last: Any, price: float | None, atr_pct: float | None) -> float:
        """MACD 的 DIF-DEA 用日ATR% 归一化（跨票可比）。"""
        if last is None or not price or not atr_pct:
            return 0.0
        dif = _finite(last.get("dif"))
        dea = _finite(last.get("dea"))
        if dif is None or dea is None:
            return 0.0
        return float((dif - dea) / max(1e-9, price * atr_pct / 100.0))

    @staticmethod
    def _kdj_rsi(features: pd.DataFrame, config: IntradayConfig) -> float:
        """KDJ/RSI 合成到 [-1,1]（与打分因子同口径：30 分钟 KDJ + RSI 各半）。"""
        parts: list[float] = []
        try:
            osc = build_oscillator_features(
                resample_bars(_bars_frame(features), 30), config)
        except Exception:  # noqa: BLE001 缺数据不该让拟合失败
            return 0.0
        if osc is not None and len(osc):
            last = osc.iloc[-1]
            value = _finite(last.get("kdj_j"))
            if value is not None:
                parts.append(max(-1.0, min(1.0, (50.0 - value) / 50.0)))
            rsi = _finite(last.get("rsi"))
            if rsi is not None:
                parts.append(max(-1.0, min(1.0, (50.0 - rsi) / 40.0)))
        return float(sum(parts) / len(parts)) if parts else 0.0

    @property
    def _intraday_days(self) -> int:
        """5 分钟 bars 的取数窗口（天）。

        为什么不是一个常数：档位拟合需要"过去 10 个交易日"，但**上下文**（分时图、
        打分的分钟特征）只需要最近几天。两者共用同一次取数，于是取
        `max(上下文天数, 拟合窗口)` —— 拿得到就多拟合几天，拿不到（数据源上限）
        拟合侧会按实际天数如实标注，绝不假装有 10 天。
        """
        fit_days = int(self._reload_config().factors.level_fit.sessions)
        return max(_INTRADAY_DAYS, min(12, fit_days))

    async def _fit_levels_for(
        self, *, code: str, features: pd.DataFrame | None,
        ctx: Any, trade_date: str, config: IntradayConfig,
        daily_bars: pd.DataFrame | None = None,
    ) -> LevelFitResult | None:
        """快照路径用的拟合入口：按交易日缓存，**整天只算一次**。

        为什么要缓存：拟合是 CPU 密集的（网格搜索 75 组合 × 留一日 10 折 +
        小网络训练，实测 0.3~2 秒/票），而盘中每 15 秒就要推一次快照。
        没有缓存的话每次推送都要重训，面板会直接卡住。

        `daily_bars` 由调用方传（快照里刚取过同一份）：不传时才自己去取，
        且**取不到就放弃拟合**而不是抛错 —— 拟合是增强项，绝不能把主链路带崩。
        """
        cfg = config.factors.level_fit
        if not cfg.enabled or not cfg.fit_on_snapshot:
            return None
        cached = self._fit_cache.get(code)
        if cached is not None and cached[0] == trade_date:
            age = time.monotonic() - cached[1]
            if cfg.cache_seconds <= 0 or age < cfg.cache_seconds:
                return cached[2]
        if features is None or not len(features):
            return None
        if daily_bars is None:
            try:
                daily = await self._fetch_daily_bars(code)
                daily_bars = daily[0] if isinstance(daily, tuple) else daily
            except Exception as exc:  # noqa: BLE001 拟合是增强项，取不到就算了
                logger.info("档位拟合跳过(%s)：日线不可用 %s", code, brief(exc, BRIEF_TIGHT))
                return None
        # 现价直接取上下文里的（快照路径本来就在用同一份），不再多打一次行情：
        # `fetch_quote` 在不同数据源上返回形态不一（Quote / tuple），
        # 在拟合路径上多做一次取数既慢又容易踩到"熔断冷却"。
        quote = ctx if hasattr(ctx, "price") else None
        if quote is None or not getattr(quote, "price", None):
            return None
        try:
            fit = await asyncio.to_thread(
                self._fit_levels_sync, code=code, features=features,
                daily_bars=daily_bars, quote=quote, config=config,
                trade_date=trade_date)
        except Exception as exc:  # noqa: BLE001 拟合失败不影响做T主链路
            logger.warning("档位拟合失败(%s): %s", code, brief(exc, BRIEF_DEFAULT))
            return None
        self._fit_cache[code] = (trade_date, time.monotonic(), fit)
        return fit

    async def effective_config(self, code: str) -> IntradayConfig:
        """该票**当前实际生效**的口径（权重档案 > YAML 覆盖 > 全局配置）。

        与 `snapshot` 内部解析覆盖的那段逻辑同源，抽出来是为了让「改动前」有
        一个可比较的基准：权重预览要显示"这条线改完会变成多少"，就必须先知道
        它现在是多少 —— 而"现在"用的是这只票自己的档案口径，不是全局默认值。
        """
        config = self._reload_config()
        override, _source = await self._resolve_override(code, config)
        if override is not None and not override.is_empty():
            return config.with_override(code, override)
        return config

    async def current_levels(
        self, code: str, *, force_refresh: bool = False,
    ) -> Any:
        """该票**当前口径**下的关键价位（回踩/冲高/止损），命中快照缓存时几乎零成本。

        用途：权重预览的「改动前」那一列。为什么不另算一遍档位：
        `compute_levels` 需要当日分钟特征 + 日线箱体 + 布林 + ATR，
        在预览路径上重取这些等于把 1~3 秒的预览再翻一倍；而档位**价格**
        只由行情与"箱体/布林/VWAP/ATR"这些非档位参数决定 ——
        档位参数（止损%、ATR倍数、兜底距离）只影响止损距离与护栏，
        拿当前这一份做基准已经足够回答"这条线原来在哪"。
        """
        snapshot = await self.snapshot(
            code, force_refresh=force_refresh, light=True)
        return snapshot.levels

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

    async def market_turnover_now(self, *, refresh: bool = False) -> dict:
        """大盘量能：**独立、只读、不等待**，供前端实时小接口使用。

        ## 为什么需要它（用户口径 2026-09-24）

        用户："量能可以前端实时显示，不可能量能变化为空，今天缩量 2117 亿。"
        原来量能只搭在**个股快照**里，而那条路为了让 46 只票的整表重算不变慢，
        最多只等 `_TURNOVER_WAIT`(2.5s)，等不到就**整段不显示**（见
        `_market_turnover` 的取舍说明）。冷启动或数据源熔断时，用户看到的就是
        "量能那段凭空消失" —— 而它其实正在后台取，几十秒后就有了。

        所以单独开一个"随时可问"的读法：
          · 有缓存 → 直接给（`available=True`）；
          · 没缓存 → **立刻返回 `available=False` 并起后台取数**，前端显示
            "量能取数中"，而不是让这一段静默消失。
        `refresh=True` 时强制重取（前端手动刷新用）。
        """
        forecast = None if refresh else self._turnover.snapshot_cached()
        if forecast is None or not getattr(forecast, "available", False):
            # 起后台任务但**不等**：单飞复用已有句柄，避免并发重复打腾讯接口
            try:
                import asyncio as _aio

                if self._turnover_task is None or self._turnover_task.done():
                    self._turnover_task = _aio.create_task(
                        self._turnover.snapshot(force=refresh))
                    self._turnover_tasks.add(self._turnover_task)
                    self._turnover_task.add_done_callback(
                        self._turnover_tasks.discard)
            except Exception:  # noqa: BLE001 起任务失败也不能让接口 500
                pass
            return {"available": False, "text": "", "turnover": {},
                    "reason": "量能取数中（数据源可能正在熔断冷却），后台自动重试"}
        block = _turnover_decision(forecast) or {}
        return {
            "available": True,
            "text": str(block.get("text") or ""),
            "t_allowed": bool(block.get("t_allowed", True)),
            "chase_allowed": bool(block.get("chase_allowed", True)),
            "reasons": list(block.get("reasons") or []),
            "turnover": forecast.to_dict(),
            "reason": "",
        }

    async def _market_turnover(self) -> Any:
        """全市场成交额预测量能（单飞 + 限时等待）。

        ## 为什么不能直接 `await provider.snapshot()`

        这个方法在**每只票的快照**里都会被调到（自选池 46 只 = 46 次），而它背后
        是 3~4 个腾讯接口。provider 的 TTL 只有 30 秒，冷启动那一轮必然穿透 ——
        若在这里同步等网络，每只票都要多等一次取数，自选整表重算（本来就约 200
        秒）会被显著拖长，报价快车道也跟着变慢。

        所以：

        1. **单飞**：并发调用共享同一个 `_turnover_task`（腾讯那几个接口是同一个
           全市场口径，重复取没有意义）；
        2. **最多等 `_TURNOVER_WAIT` 秒**：等不到就返回 `None`，这一轮不显示量能
           那一段（徽标少一段结论，但不影响其它任何东西）。下一次快照/刷新时
           provider 缓存已热，直接命中。

        代价是"服务重启后的第一份快照可能没有量能那一段"，这在诚实性上是可接受
        的（宁可不显示，也不要为了显示而拖慢整表）。
        """
        # 已经有缓存（含"上一轮后台任务刚写好"）就直接给，不再建任务
        cached = self._turnover.snapshot_cached()
        if cached is not None:
            return cached
        task = self._turnover_task
        if task is None or task.done():
            task = asyncio.get_running_loop().create_task(
                self._turnover.snapshot(), name="intraday-market-turnover")
            self._turnover_task = task
        # 强引用 + 异常兜底：超时返回后任务仍在后台跑，抛异常而没人取结果时
        # asyncio 会打 "never retrieved" 噪声。
        self._turnover_tasks.add(task)
        task.add_done_callback(self._turnover_tasks.discard)
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=_TURNOVER_WAIT)
        except asyncio.TimeoutError:
            # 首次取数要打 3~4 个腾讯接口，超过 _TURNOVER_WAIT 是正常的：
            # 这一轮先不显示量能那一段，任务继续在后台跑完并写缓存，
            # **下一次**快照/刷新就直接命中（`snapshot_cached` 在上面）。
            return None
        except Exception as exc:  # noqa: BLE001 取数失败同样只表现为"少一段结论"
            logger.info("全市场量能预测取数失败：%s", brief(exc, BRIEF_TIGHT))
            return self._turnover.snapshot_cached()

    async def market_turnover(self) -> Any:
        """对外接口：当前全市场量能预测（供接口/诊断直接取）。"""
        return await self._turnover.snapshot()

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
        """对外接口：取该标的的股性画像（含预填权重与档位）。"""
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

    async def _average_volume(self, code: str, trade_date: str,
                              *, days: int = 5) -> float | None:
        """近 N 日日均成交量（"放天量"判据的分母）。

        为什么不用当日自身的量做基数：那就变成"自己跟自己比"，
        永远得不出"天量"的结论。必须拿**历史同期**做基数才叫放量。

        取不到就返回 None —— `sell_points` 会据此**去掉**放量判据并记 gap，
        而不是塞一个编造的基数进去（那会让"天量"判据永远成立或永远不成立）。

        ⚠️ 复用既有的 `_fetch_daily_bars`（QMT→本地CSV→AkShare 采集链），
        **不要**去访问 `self._warehouse` —— 那个属性在原实现里根本不存在
        （只知道用 `self._warehouse.load(...)` 取数，类里从未赋值），
        调用时必然抛异常、被下面的 except 吞掉，表现为"放量判据永远缺席"。
        """
        try:
            frame, _source = await self._fetch_daily_bars(code)
            if frame is None or frame.empty:
                return None
            # 兼容不同的日期列名（`date` / `trade_date`）
            date_col = "date" if "date" in frame.columns else "trade_date"
            if date_col not in frame.columns:
                return None
            # 排除当日（当日量正是要比较的对象）
            history = frame[frame[date_col].astype(str) < str(trade_date)]
            tail = history.tail(int(days))
            if not len(tail):
                return None
            vol_col = "volume" if "volume" in tail.columns else "volume_lot"
            if vol_col not in tail.columns:
                return None
            values = [float(v) for v in tail[vol_col].tolist()
                      if v == v and float(v) > 0]
            if not values:
                return None
            return sum(values) / len(values)
        except Exception as exc:  # noqa: BLE001 拿不到就不判放量
            logger.debug("取日均成交量失败（不判放量）：%s", exc)
            return None

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
            verdict = "板块与个股情绪偏暖（利于回踩区间提示）"
        elif score < -0.15:
            verdict = "板块与个股情绪偏冷（利于冲高区间提示）"
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
                        force: bool = False,
                        active: str | None = None) -> list[WatchItem]:
        """自选标的概览（轻量快照并发，含当前总分与信号）。

        **默认走进程内缓存**：盘中由 `_watchlist_refresh_loop` 每分钟重算一次并写入，
        前端每分钟取一次直接命中缓存 —— 用户要的"自选股每分钟自动刷新"就是这条链路。

        ## 强制刷新为什么要"按优先级"（2026-09-17 用户报障）

        用户反馈：自选 30+ 只时点「强制刷新」要 6~7 秒。实测确认了两个成本来源：

        | 环节 | 单只耗时 | 说明 |
        |---|---|---|
        | `fetch_quote` 报价 | 2.1s（冷）/ ~0ms（热） | QMT/腾讯快照 |
        | 板块概念快照子进程 | 3.2~6.5s/板块 | 一只票 2~4 个板块，**且每只票都要重付** |

        39 只 × 每只 2~4 个板块 = 几十个子进程抢 CPU，于是"整表重算"变成几秒起步。
        但**用户真正在看的只有一只票** —— 没必要为了刷新 39 只列表而阻塞他看的那只。

        因此 `active`（前端当前查看的标的）给定时：

        - **只有 active 这一只重新取数**（它必须是新鲜的：用户正盯着它）；
        - 其余标的**直接用缓存值返回**（1 分钟前的分数/信号仍然有用，
          价格还有报价快车道单独贴），并把整表重算丢到后台去（去重）。

        这样强制刷新的响应时间从"随自选数量增长"变成"基本恒定（≈1 只票）"。
        不传 `active` 时保持旧语义（整表重算），供刷新循环/后台任务复用。
        """
        ttl = self._watch_cache_ttl()
        cached = self._watch_cache

        # ---- 优先路径：强制刷新 + 指定了当前标的 ----
        # 刻意不走 `_watch_lock`：后台整表重算可能正持有它（实测 28 只约 109 秒冷启动），
        # 用户点「强制刷新」若排在那后面，就会又变成"等好几秒"（实测 6.3s）。
        # 单只快照自己有 60 秒缓存与数据源层缓存，重复触发不会放大成本。
        if force and active:
            fresh = await self._refresh_one_watch(active)
            if fresh is not None:
                base_items = list(cached[1]) if cached is not None else []
                if not base_items:
                    # ⚠️ 缓存为空时**不能把 `[fresh]` 当整表** —— 那会让自选列表
                    # 从 46 只缩成 1 只（前端此后只能看到这只票）。
                    # 正确做法：铺出配置里的**完整清单**（身份字段 + 空分数），
                    # 把刚算好的那一只替换进去，再整表写回缓存。
                    # 分数留空由下一次整表重算补齐 —— 与 `_placeholder_watchlist`
                    # 同一口径：宁可先没有分数，也不能把列表变短。
                    full = self._placeholder_watchlist(self._watch_cache_size)
                    merged_full = [fresh if item.code == active else item
                                   for item in full]
                    if not any(item.code == active for item in merged_full):
                        merged_full.insert(0, fresh)
                    self._watch_cache = (time.monotonic(), merged_full)
                    self._watch_generation += 1
                    self._schedule_watchlist_refresh()
                    return self._apply_quote_overlay(merged_full[:limit])
                merged = [fresh if item.code == active else item
                          for item in base_items]
                if not any(item.code == active for item in merged):
                    merged.insert(0, fresh)
                self._watch_cache = (time.monotonic(), merged)
                self._watch_generation += 1
                # ⚠️ 这里**不**再调 `_schedule_watchlist_refresh()`：
                # 实测那样会和"当前这只票的同步重算"叠在一起变成**双份工作**
                # （第一只票时 5.2s → 冷缓存下 66.7s），而且盘中每分钟的刷新循环
                # 本来就会把整表补齐。宁可让其余标的短暂用旧值，也不要抢 CPU。
                return self._apply_quote_overlay(merged[:limit])

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
        if not force:
            # ---- 完全没有缓存，且不是用户显式要求刷新 ----
            #
            # 为什么不能像以前那样"就地同步算一遍"（2026-09-18 实测）：
            # 冷启动整表重算是 **50 只 × 3~5 秒 ≈ 200 秒**。以前这条分支会让
            # 首屏的 `GET /intraday/watchlist` 直接挂 200 秒 —— 而这正是用户
            # 报障"每次重启前端加载要一分钟"的主因之一（实测冷进程里
            # `/watchlist` 219.8s，而其它请求 1~2 秒）。
            #
            # 触发这条分支的现实场景并不罕见：
            #   - 首次部署（还没有快照文件）；
            #   - 快照超过 24 小时（周一早上开机，上一份是上周五收盘写的）；
            #   - 自选池改动较大（快照条目与当前池无交集，`hot_cache` 会整体丢弃）；
            #   - 快照格式版本升级。
            #
            # 口径与项目里其它"慢链路"一致 —— **先给能立刻给的东西，重算放后台**：
            # 配置里的代码/名称/板块立刻铺出来（价格由报价快车道 5 秒内贴上），
            # 分数与信号**留空**（前端显示 "—" 与"观望"，绝不伪造数字）。
            if self._startup_grace_left() > 0:
                # 启动宽限期内连后台重算都先不跑（把 CPU 让给首屏那次完整快照），
                # 但**要排上**：宽限一到自动补算，否则列表会永远停在占位值。
                self._schedule_watchlist_refresh()
                return self._apply_quote_overlay(self._placeholder_watchlist(limit))
            # 过了宽限期：起一次重算，最多等 `_COLD_WATCHLIST_WAIT` 秒 ——
            # 自选少 / 数据源热时这点时间足够拿到真数据；50 只冷取则等不到，
            # 那就先给占位值，让重算在后台继续（结果会写回缓存并由 WS 推送）。
            try:
                items = await asyncio.wait_for(
                    asyncio.shield(self._cold_recompute_task()),
                    timeout=self._cold_watchlist_wait)
            except asyncio.TimeoutError:
                return self._apply_quote_overlay(self._placeholder_watchlist(limit))
            except Exception as exc:  # noqa: BLE001 重算失败也不能让首屏 500
                # 没有缓存 + 重算失败（数据源全挂/配置写坏）时，给占位列表比给 500
                # 好得多：用户至少能看到自选清单，`last_error` 也会说明原因。
                logger.warning("冷启动整表重算失败，先返回占位列表：%s", brief(exc, BRIEF_DEFAULT))
                return self._apply_quote_overlay(self._placeholder_watchlist(limit))
            return self._apply_quote_overlay(items[:limit])

        # 显式 `force=True`（前端「立即刷新」/`/scan`）走到这里：**必须真的算**，
        # 调用方要的就是"现在这一刻"的结果。
        items = await self._recompute_into_cache()
        return self._apply_quote_overlay(items[:limit])

    def _cold_recompute_task(self) -> asyncio.Task[list[WatchItem]]:
        """冷启动（无缓存）时的那次整表重算 —— 进程内单飞。

        单飞的必要性：多个标签页 / WS 首帧 / 轮询会**同时**打进来，
        不共享同一个任务就会排在 `_watch_lock` 上各算一遍（实测 50 只 × 3 遍）。
        """
        task = self._cold_task
        if task is None or task.done():
            task = asyncio.get_running_loop().create_task(
                self._recompute_into_cache(), name="intraday-watchlist-cold")
            self._cold_task = task
            # 强引用 + 异常兜底：超时返回占位值后这个任务仍在后台跑，
            # 若它抛异常而没人取结果，asyncio 会在 GC 时打 "never retrieved" 噪声。
            self._watch_refresh_tasks.add(task)

            def _done(finished: asyncio.Task[list[WatchItem]]) -> None:
                self._watch_refresh_tasks.discard(finished)
                if finished.cancelled():
                    return
                error = finished.exception()
                if error is not None:
                    logger.warning("冷启动整表重算失败：%s", brief(error, BRIEF_DEFAULT))

            task.add_done_callback(_done)
        return task

    async def _recompute_into_cache(self) -> list[WatchItem]:
        """真正算一次整表并写回缓存（`_watch_lock` 单飞）。调用方决定"等不等"。"""
        async with self._watch_lock:
            self._full_recompute_inflight = True
            try:
                items = await self._compute_watchlist(limit=self._watch_cache_size)
            finally:
                self._full_recompute_inflight = False
            self._watch_cache = (time.monotonic(), items)
            self._full_recompute_at = time.monotonic()
            self._watch_generation += 1
            self._refresh_state["last_count"] = len(items)
            self._persist_watch_snapshot(items)          # 供下次启动热加载
            return items

    def _placeholder_watchlist(self, limit: int) -> list[WatchItem]:
        """没有任何缓存时的**占位自选列表**（只有身份字段，没有任何数字）。

        为什么只给身份字段：分数/信号/价格要真算才有，编一个"看起来像"的值
        比空着更糟（用户会拿它做交易决定）。空值在前端显示为 "—"，
        而 `warming=True` 会告诉用户"首次重算中"。

        为什么需要它：见 `watchlist()` 里"完全没有缓存"分支的说明 ——
        没有它，冷启动首屏要等 200 秒的整表重算。
        """
        config = self._reload_config()
        items = [
            WatchItem(
                code=item.code,
                name=item.name or self._lookup_name(item.code),
                boards=list(item.boards),
                pinned=bool(getattr(item, "pinned", False)),
            )
            for item in config.watchlist[:max(0, limit)]
        ]
        return sort_watch_items(items)

    def _startup_grace_left(self) -> float:
        """启动宽限期还剩几秒（0 = 已过）。

        见 `_WATCHLIST_STARTUP_GRACE`：冷启动的整表重算要与首屏那次完整快照抢
        CPU/GIL，把首屏从 8 秒拖到几分钟。宽限期内**自动**重算一律不启动
        （用户显式点「刷新」仍立即生效 —— 那是人在等结果）。

        `_startup_grace_seconds` 做成实例属性而不是只读常量：测试要把它置 0
        才能验证"循环第一轮就刷"这个窗口语义（见
        `tests/unit/test_intraday_watchlist_refresh.py`）。
        """
        return max(0.0, float(self._startup_grace_seconds)
                   - (time.monotonic() - self._boot_at))

    async def _refresh_one_watch(self, code: str) -> WatchItem | None:
        """只重算**一只**标的的概览行（强制刷新时给当前查看的那只走快车道）。

        复用 `_compute_watchlist` 的组装逻辑，但把范围缩到一只：
        这样耗时与自选数量无关（实测单只 light 快照 2~4s，而整表 39 只是 6~7s+，
        且不会再触发几十个板块子进程）。
        """
        try:
            items = await self._compute_watchlist(limit=0, only=[code])
        except Exception as exc:  # noqa: BLE001 单只失败不该让整次刷新报错
            logger.info("单只自选快照失败(%s)：%s", code, brief(exc, BRIEF_TIGHT))
            return None
        return items[0] if items else None

    def _schedule_watchlist_refresh(self) -> None:
        """后台重算自选概览（去重，不阻塞调用方）。

        与刷新循环共用同一条重算路径；即使循环被关掉（配置 0）或正在等下一分钟
        刻度，一次读请求也能顺手把数据刷新起来 —— 自愈而不是让用户等。

        **启动宽限期内延后、但一定会跑**：首屏（含当前标的的完整快照）优先占用
        CPU；宽限期一到就自动补算。注意不能直接"丢掉不排" —— 冷启动且没有热缓存
        时，占位列表要人把它算出来，丢排会让首屏永远停在 "—"。
        """
        if self._watch_refresh_inflight:
            return
        # 刷新循环正在跑整表重算（或刚跑完）时不再叠一个：两边都是 50 只 × 3~5 秒，
        # 叠起来就是白白多付两分钟 CPU，而且第二个还要先排队等锁。
        if self._recompute_already_running():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:      # 无事件循环（同步调用）时不做后台刷新
            return
        self._watch_refresh_inflight = True

        async def _refresh() -> None:
            try:
                grace = self._startup_grace_left()
                if grace > 0:
                    logger.info("后台整表重算延后 %.0f 秒启动（先让出首屏）", grace)
                    await asyncio.sleep(grace)
                await self.watchlist(force=True)
            except Exception as exc:  # noqa: BLE001 后台失败只记日志
                logger.warning("后台重算自选概览失败：%s", brief(exc, BRIEF_DEFAULT))
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
        # ⚠️ `getattr` 兜底而不是直接读属性：部分测试用 `IntradayService.__new__`
        # 之类的轻量构造（不跑 `__init__`），直接访问会 AttributeError。
        if getattr(self, "_watch_cache_single_change", False):
            # ⚠️ 单只增删正在接管作废逻辑（见 `_invalidate_watch_entry`）：
            # 这里**必须早退**。否则"加一只票"会退化成"作废整张缓存 → 下一次读
            # 触发 46 只全表重算（真实数据源约 200 秒）"，而重算一只只要几秒。
            return
        self._watch_cache = None
        self._watch_generation += 1

    def _invalidate_watch_entry(self, code: str, *, fresh: WatchItem | None = None
                                ) -> None:
        """**只**作废/替换缓存里的某一只（自选池增删一只时的正确粒度）。

        ## 为什么要专门做这个（2026-09-23 用户报障"加自选要十秒"）

        实测（45 只自选）：

            add_watch（写盘 + 重建子组件）      0.20s
            作废整张缓存 → 后台整表重算（46 只） 12.4s    ← 真实数据源约 200s（见
                                                          `_gather_light_snapshots`）
            只重算新加的那一只                  0.08s    ← 差两个数量级

        根因是一条放大链：`save_watchlist` → `reset_config_cache()` →
        每次 `_reload_config()` 都拿到**新对象** → `invalidate_watchlist_cache()`
        清空整张缓存 → 下一次读发现无缓存 → 触发全表重算。
        **改一只票却要重算全部**，这就是"加载十秒"的来源
        （`_recompute_into_cache` 期间进程在跑 46 只的 pandas 计算）。

        ## 语义

        - `fresh=None`（删除）：把它从缓存列表里**摘掉**；
        - `fresh=<行>`（新增）：插到**最前面**（与 `upsert_watch(prepend=True)`
          的配置顺序一致，否则下一次全量重算会让它跳回末尾），其余行原样保留。

        缓存里其余 45 行的分数**保持有效** —— 它们和这只新票毫无关系，
        没有任何理由跟着一起作废。
        """
        cached = self._watch_cache
        if cached is None:
            return                      # 本来就没缓存，无需处理
        stamp, items = cached
        kept = [item for item in items if item.code != code]
        if fresh is not None:
            # 插到**最后一个置顶项之后**，与 `upsert_watch(prepend=True)` 的
            # 配置顺序规则完全一致 —— 硬插到 index 0 会把用户显式钉住的票挤下去，
            # 而 `sort_watch_items()` 的契约是"置顶项永远在最前"。
            last_pinned = -1
            for index, item in enumerate(kept):
                if getattr(item, "pinned", False):
                    last_pinned = index
            kept.insert(last_pinned + 1, fresh)
        self._watch_cache = (stamp, kept)
        self._watch_generation += 1

    async def _valuation_label(self, code: str, ttl: float) -> tuple[str, str]:
        """单只票的估值结论 `(label, bucket)`。

        **过期时永不阻塞调用方**：缓存里有值就先给（哪怕已过 TTL），过期的那些
        改为 `_schedule_valuation_refresh` 在后台补 —— 与自选概览的
        stale-while-revalidate 同一套路子。

        为什么必须这样：实测这一批（45 只）**冷取 42.4 秒**、命中缓存 0.00 秒。
        若在 TTL 到期时同步重取，那 42 秒会直接叠加在"自选整表重算"（本身约
        200 秒）上，把报价刷新从 60 秒一次拖成 5 分钟一次 —— 而估值结论是
        **日频**量（PE/PB 三年序列），一个小时前的那四个字完全够用。
        """
        cached = self._valuation_cache.get(code)
        if cached is not None:
            if ttl <= 0 or (time.monotonic() - cached[0]) < ttl:
                return cached[1], cached[2]
            self._schedule_valuation_refresh([code])   # 过期：先给旧的，后台补新的
            return cached[1], cached[2]
        # 从没算过（冷启动 / 新加自选）：这一次必须等 —— 但只在**第一次**
        # 遇到这只票时等，之后都走上面的缓存分支。
        return await self._fetch_valuation_label(code)

    async def _fetch_valuation_label(self, code: str) -> tuple[str, str]:
        """真的去取一次并写缓存（只在冷启动/新加自选/后台补算时被调到）。"""
        try:
            space = await self._valuation.fetch(code)
        except Exception as exc:  # noqa: BLE001 估值缺失绝不能拖垮自选列表
            logger.info("自选估值结论取数失败(%s)：%s", code, brief(exc, BRIEF_TIGHT))
            return "", ""
        bucket = str(getattr(space, "headroom", "") or "")
        label = HEADROOM_LABELS.get(bucket, "")
        if not label:
            # 拿不到档位就整块不显示（宁可不显示，也不要给一个编出来的结论）
            return "", ""
        self._valuation_cache[code] = (time.monotonic(), label, bucket)
        return label, bucket

    def _schedule_valuation_refresh(self, codes: list[str]) -> None:
        """后台补算这些票的估值结论（去重；不阻塞调用方）。

        与 `_schedule_watchlist_refresh` 同样"排了就要跑"，但在补算中的代码不重复排
        —— 否则每个读请求都会再起一轮 42 秒的取数。
        """
        todo = [code for code in codes if code not in self._valuation_refreshing]
        if not todo:
            return
        self._valuation_refreshing.update(todo)

        async def _run() -> None:
            try:
                await self._valuation_labels(todo, ttl=0.0, force=True)
            except Exception as exc:  # noqa: BLE001 后台补算失败只记日志
                logger.info("估值结论后台补算失败：%s", brief(exc, BRIEF_TIGHT))
            finally:
                self._valuation_refreshing.difference_update(todo)

        task = asyncio.get_running_loop().create_task(
            _run(), name="intraday-valuation-refresh")
        # 强引用：任务只被弱引用时可能被 GC 掉（与自选刷新任务同样的处理）。
        self._valuation_tasks.add(task)
        task.add_done_callback(self._valuation_tasks.discard)

    async def _valuation_labels(self, codes: list[str], ttl: float,
                                *, force: bool = False) -> dict[str, tuple[str, str]]:
        """批量取估值结论：**限并发 + 批间让路**（与轻量快照同一套路子）。

        为什么不能简单地 for 循环串行：实测冷取（45 只）串行要一分多钟，
        全部压在同一轮自选重算里。分 4 只一批并发实测降到 **42.4 秒**，
        同时批间 `await asyncio.sleep(0)` 把事件循环让出去（否则 WS 推送会被饿死）。

        `force=True` 时绕过 TTL 直接重取（后台补算用；此时没有"先给旧值"的问题）。
        单只失败不影响其它票：`return_exceptions=True` 后失败的记成空结论。
        """
        out: dict[str, tuple[str, str]] = {}
        batch = 4
        for start in range(0, len(codes), batch):
            chunk = codes[start:start + batch]
            results = await asyncio.gather(
                *((self._fetch_valuation_label(code) if force
                   else self._valuation_label(code, ttl)) for code in chunk),
                return_exceptions=True)
            for code, result in zip(chunk, results, strict=True):
                out[code] = ("", "") if isinstance(result, BaseException) else result
            if start + batch < len(codes):
                await asyncio.sleep(0)
        return out

    async def _compute_watchlist(self, *, limit: int = 20,
                                 only: list[str] | None = None) -> list[WatchItem]:
        """共享自选列表（**配置来源**）—— 语义与原实现完全一致。

        多用户路径见 `_compute_watchlist_for_codes`：这里只是把"代码从配置里取"
        这一步做完，再交给同一个计算核，避免两份取数逻辑漂移。
        """
        config = self._reload_config()
        codes = [item.code for item in config.watchlist][:limit]
        if only:
            # 优先刷新：只算指定的这几只（顺序按配置里的顺序，保证列表稳定）
            wanted = {str(code) for code in only}
            codes = [item.code for item in config.watchlist
                     if str(item.code) in wanted]
        if not codes:
            return []
        return await self._compute_watchlist_for_codes(codes)

    async def _compute_watchlist_for_codes(
        self, codes: list[str], *,
        meta: dict[str, dict[str, Any]] | None = None,
    ) -> list[WatchItem]:
        """自选概览计算核：**代码由调用方给定**。

        ## 为什么把这一步单独抽出来（多用户改造的关键切口）

        原实现把"从 `configs/intraday.yaml` 取代码"与"计算概览"揉在一个函数里，
        于是**任何**想换数据来源的尝试都得复制整段计算 —— 而那段里有
        估值批量、限并发轻量快照、失败兜底、排序等一整套口径，
        复制一份必然漂移（改了 A 忘了 B）。

        拆开之后：
          - 共享路径（`_compute_watchlist`）传配置里的代码；
          - 多用户路径传**该用户自己池里的代码**（+ 他的板块/置顶元数据）。
        两条路走的是同一套计算，所以"取数口径"只有一处。

        `meta` 为 `{code: {"boards": [...], "name": "...", "pinned": bool}}`：
        DB 用户每只票的板块/置顶存在 `dim_user_watchlist_v2`，
        **不能**去 YAML 里查（那是别人的配置）。
        """
        if not codes:
            return []
        config = self._reload_config()
        meta = meta or {}
        # 估值结论与轻量快照**互不依赖**，但估值要打采集链，先把它拿到
        # （带 TTL 缓存，热的时候是纯内存命中，几乎不占时间）。
        valuations = await self._valuation_labels(
            codes, float(getattr(config.data, "valuation_cache_ttl", 3600) or 0))
        snapshots = await self._gather_light_snapshots(codes)
        items: list[WatchItem] = []
        for code, result in zip(codes, snapshots, strict=True):
            # 元数据优先用调用方给的（DB 用户的板块/置顶在库里），
            # 没给才回落到配置 —— 多用户路径**绝不能**去 YAML 查别人的配置。
            given = meta.get(code)
            watch = None if given is not None else config.watch(code)
            if given is not None:
                boards = [str(x) for x in (given.get("boards") or [])]
                given_name = str(given.get("name") or "")
                pinned = bool(given.get("pinned"))
            else:
                boards = list(watch.boards) if watch else []
                given_name = (watch.name if watch and watch.name != code else "")
                pinned = bool(getattr(watch, "pinned", False))
            valuation_label, valuation_bucket = valuations.get(code, ("", ""))
            if isinstance(result, BaseException) or result is None:
                # 快照失败时也不能把名称丢掉：配置/库里的名字、字典里的名字都行，
                # 实在没有就留空（**不要用代码冒充名称**）。
                items.append(WatchItem(
                    code=code,
                    name=given_name or self._lookup_name(code),
                    boards=boards,
                    valuation_label=valuation_label,
                    valuation_bucket=valuation_bucket,
                    pinned=pinned))
                continue
            resolved = result.name or given_name
            items.append(WatchItem(
                code=code, name=resolved or self._lookup_name(code),
                boards=boards,
                total_score=result.scorecard.total if result.scorecard else None,
                signal_strength=(
                    result.signal.strength if result.signal else "none"),
                signal_kind=result.signal.kind if result.signal else "none",
                price=result.quote.price if result.quote else None,
                change_pct=result.quote.change_pct if result.quote else None,
                valuation_label=valuation_label,
                valuation_bucket=valuation_bucket,
                pinned=pinned))
        return sort_watch_items(items)

    async def _gather_light_snapshots(self, codes: list[str]) -> list[Any]:
        """并发跑一批「轻量快照」，但**限制同时进行的只数**并把事件循环让出去。

        ## 为什么不能一把 `asyncio.gather(50 只)`（2026-09-18 实测，冷启动）

        用户报障："每次重启服务，前端加载数据都要一分钟。" 实测（冷进程 + 浏览器
        首屏的真实请求序列）：

        | 请求 | 冷启动 | 事件循环空闲后 |
        |---|---|---|
        | `agents/meta`（纯静态字典） | **25.5s** | 0.7s |
        | `intraday/watchlist`（**有热缓存**） | **22.0s** | 0.8s |
        | `health` / `intraday/weight-profiles` | **210s / 208s** | 1.0s |
        | `intraday/snapshot?code=300308` | **231s** | 1.1s |

        即：**任何**请求（哪怕只返回一个静态字典）都要等几分钟。原因是启动后
        `_watchlist_refresh_loop` 立刻做整表重算，把 50 只票的轻量快照
        `gather` 到一起 —— 每只票 3~5 秒的同步 pandas 计算（特征/指标/逐bar回放）
        全部压在事件循环上，同时还有几十个档位拟合线程在抢 GIL。
        事件循环被连续占用最长 **36.5s**（采样定位到 `features.py`/`indicators.py`
        的 DataFrame 计算），整表 198s 期间进程几乎不响应。

        ## 做法：限并发 + 批间让路

        每批只跑 `_WATCH_COMPUTE_BATCH` 只，批间 `await asyncio.sleep(YIELD)`。
        实测（50 只冷启动）：

        | 并发 | 整表耗时 | 事件循环最长被占用 |
        |---|---|---|
        | 50（原） | 198s | **36.5s** |
        | 2 | 193s | 4.2s |
        | **1（现在）** | 200s | **0.4s** |

        总耗时不变（那本来就是 CPU 时间，单核跑不掉），但事件循环再也不被饿死：
        首屏、`/health`、WebSocket 推送都能插空被服务。
        """
        batch = max(1, int(self._WATCH_COMPUTE_BATCH))
        results: list[Any] = []
        for start in range(0, len(codes), batch):
            chunk = codes[start:start + batch]
            # use_light_cache=False：这里是**生产者**，必须真算；
            # 算完把结果留一份给交互首帧（见 `snapshot` 的 use_light_cache 说明）。
            computed = await asyncio.gather(
                *(self.snapshot(code, light=True, use_light_cache=False)
                  for code in chunk),
                return_exceptions=True)
            now = time.monotonic()
            # `gather(..., return_exceptions=True)` 保序且保长，`strict=True` 把这个
            # 不变量写进代码：万一以后改成 `as_completed` 之类的乱序收集，
            # 这里会立刻报错，而不是把 A 的行情缓存到 B 的代码下。
            for code, item in zip(chunk, computed, strict=True):
                if not isinstance(item, BaseException):
                    self._light_cache[code] = (now, item)
            results.extend(computed)
            if start + batch < len(codes):
                await asyncio.sleep(self._WATCH_COMPUTE_YIELD)
        return results

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

        首轮延后 `_WATCHLIST_STARTUP_GRACE` 秒：见该常量的实测说明 —— 首屏要的是
        "热缓存 + 当前这一只的完整快照"，而整表重算是 50 只 × 3~5 秒的 CPU 活儿。
        """
        interval = max(15, self._config.data.watchlist_refresh_seconds)
        grace = self._startup_grace_left()
        if grace > 0:
            logger.info("自选池整表重算延后 %.0f 秒启动（先把 CPU 让给首屏）", grace)
            await asyncio.sleep(grace)
        while True:
            try:
                allowed, reason = watchlist_refresh_window()
                # 9:15-9:30 集合竞价期间**不查** `is_trading_day()`：
                # 行情时钟在 09:30 之前 tick 还没推进，`is_trading_day()` 会把真交易日
                # 误判成"非交易日"，但 09:25 集合竞价一撮合、watchlist 就该立刻看到
                # 开盘价 + 集合竞价涨幅 —— 否则 9:25-9:30 这 5 分钟面板上是 9/24 收盘价。
                # 节假日误判方向见下方 in_call_auction 注释：最坏多烧一轮 CPU（48 × 3-5s），
                # 比"集合竞价看不见开盘价"轻得多（用户口径 2026-09-28）。
                now_moment = datetime.now()
                in_call_auction = (
                    now_moment.hour * 60 + now_moment.minute) < MORNING_OPEN
                holiday_check = (
                    allowed and not in_call_auction
                    and not _is_trading_day_for_refresh())
                if holiday_check:
                    # ---- 整表重算闸门：非交易日不做这轮重算（2026-09-25 中秋事故）----
                    # `watchlist_refresh_window()` 的行情时钟判据在 **09:15~09:30**
                    # 刻意失效（竞价期 tick 还没推进，怕误杀正常交易日），于是节假日
                    # 的这个时段会放行。而一次整表重算是 48 只 × 3~5 秒的 CPU，
                    # 且每只票的 `snapshot()` 都可能触发推送 —— 2026-09-25 中秋节
                    # （周五休市）就是这样在早盘窗口里把 9/24 的收盘信号又推了一遍，
                    # 叠加飞书 webhook 失败回落，变成持续发邮件。
                    # 这里换成**完整交易日判定**（周末 + 市场时钟都查），非交易日直接
                    # 跳过整轮：既不发信，也不再空烧几十分钟 CPU。下一轮照常再判。
                    self._refresh_state.update(
                        last_run_at=now_moment.strftime("%H:%M:%S"),
                        last_error="")
                    logger.debug("非交易日跳过自选池整表重算（%s）", reason)
                    await asyncio.sleep(_seconds_until_next_tick(interval))
                    continue
                if allowed:
                    if self._recompute_already_running():
                        # 已有一次整表重算在进行 / 刚完成：这一轮**跳过**。
                        # 冷启动（无热缓存）时它有两条触发源会撞在一起 ——
                        # 读请求排下的后台重算 + 本循环的首轮，不跳过就是
                        # 一前一后各算 50 只（白白多付约 200 秒 CPU）。
                        await asyncio.sleep(_seconds_until_next_tick(interval))
                        continue
                    started = time.perf_counter()
                    try:
                        items = await self.watchlist(force=True)
                        self._refresh_state.update(
                            last_run_at=datetime.now().strftime("%H:%M:%S"),
                            last_seconds=round(time.perf_counter() - started, 2),
                            last_count=len(items), last_error="")
                    except Exception as exc:  # noqa: BLE001 单轮失败不影响下一轮
                        self._refresh_state["last_error"] = brief(exc, BRIEF_DEFAULT)
                        logger.warning("自选池自动刷新失败：%s", brief(exc, BRIEF_DEFAULT))
                await asyncio.sleep(_seconds_until_next_tick(interval))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 循环必须活着
                logger.exception("自选池自动刷新循环异常，5 秒后继续")
                await asyncio.sleep(5)

    def _recompute_already_running(self) -> bool:
        """是否已有整表重算在进行 / 排着队 / 刚刚完成。

        三个来源都要算上，否则冷启动时会撞出**双份整表重算**（每份 50 只 × 3~5 秒）：
        1. `_full_recompute_inflight`：正拿着 `_watch_lock` 算；
        2. `_watch_refresh_inflight`：读请求排下的那次后台重算（可能还在等启动宽限）；
        3. 最近 `_FULL_RECOMPUTE_MIN_INTERVAL` 内刚算完。
        """
        if self._full_recompute_inflight or self._watch_refresh_inflight:
            return True
        return (time.monotonic() - self._full_recompute_at
                < _FULL_RECOMPUTE_MIN_INTERVAL)

    async def start_auto_select(self) -> None:
        """启动「自动选股」循环（幂等；只在交易日的规定窗口内真正跑）。

        用户口径（2026-09-17）：
          - 触发时段：**9:25–9:40**（开盘定方向）与 **14:45–15:00**（尾盘定隔夜）；
          - 频率：**1 分钟一次**；
          - 盘后：日K触发多方条件的票也自动加自选（15:05 后一次）。

        为什么只在窗口内跑：一轮要精算几十只票（每只 2~4 秒），全天跑既没人看，
        又会持续抢数据源 —— 这两个窗口才是回踩决策真正发生的时候。
        """
        if not bool(getattr(self._config.data, "auto_select_enabled", True)):
            logger.info("自动选股未启用（auto_select_enabled=false）")
            return
        if self._auto_select_task is not None and not self._auto_select_task.done():
            return
        self._auto_select_task = asyncio.create_task(
            self._auto_select_loop(), name="intraday-auto-select")
        logger.info("自动选股已启动：窗口 09:25~09:40 / 14:45~15:00，每分钟一次；"
                    "盘后 15:05 再按日K信号补一次")

    async def stop_auto_select(self) -> None:
        """停止自动选股循环（关停时调用；可重复调用）。"""
        task, self._auto_select_task = self._auto_select_task, None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _auto_select_loop(self) -> None:
        """自动选股主循环：**每个窗口每轮只跑一次**（用窗口标签去重）。

        去重是必要的：窗口有 15 分钟、循环每分钟醒一次，不做去重就会在同一窗口里
        连跑 15 轮，每轮几十只票 —— 既浪费又会把自选写满。
        """
        from src.intraday.auto_select import in_trigger_window, is_post_close, is_trading_day

        interval = 60.0
        last_window = ""
        post_close_done_for = ""
        # 启动后先让出 90 秒再进第一轮：盘后启动时 `is_post_close(now)` 立刻为真，
        # 原先会在启动瞬间就开跑一轮几十只票的选股（每只 2~4 秒 + 可能触发 QMT
        # 历史补下载子进程），把磁盘/CPU 与首屏请求、`/health` 抢在一起 ——
        # 实测 `/health` 因此从 3.7 秒退化成 111.8~142.8 秒。
        # 延后不影响窗口语义：15:05 之后任意时刻跑一次即可；而盘中窗口
        # （9:25~9:40 / 14:45~15:00）本来也不在启动瞬间。
        await asyncio.sleep(90.0)
        while True:
            try:
                now = datetime.now()
                if is_trading_day(now):
                    window = in_trigger_window(now)
                    if window and window != last_window:
                        last_window = window
                        await self._run_auto_select(window, apply=True)
                    elif not window:
                        last_window = ""      # 离开窗口 → 下次进入同一窗口可再跑
                    # 盘后一次（每天只跑一次）
                    stamp = now.strftime("%Y%m%d")
                    if is_post_close(now) and post_close_done_for != stamp:
                        post_close_done_for = stamp
                        await self._run_auto_select("post_close", apply=True)
                await asyncio.sleep(_seconds_until_next_tick(interval))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 循环必须活着
                logger.exception("自动选股循环异常，5 秒后继续")
                await asyncio.sleep(5)

    async def _run_auto_select(self, window: str, *, apply: bool = True) -> Any:
        """跑一轮自动选股并（可选）写入自选；失败只记日志，不打断循环。"""
        from src.intraday.auto_select import (
            DEFAULT_MIN_INTRADAY_SCORE,
            DEFAULT_PRESCREEN_SIZE,
            DEFAULT_TOP_N,
            WatchlistAutoSelector,
        )

        data_cfg = self._config.data
        selector = WatchlistAutoSelector(
            self,
            top_n=int(getattr(data_cfg, "auto_select_top_n", DEFAULT_TOP_N)),
            min_intraday_score=float(getattr(
                data_cfg, "auto_select_min_intraday", DEFAULT_MIN_INTRADAY_SCORE)),
            prescreen_size=int(getattr(
                data_cfg, "auto_select_prescreen", DEFAULT_PRESCREEN_SIZE)))
        started = time.perf_counter()
        try:
            result = await selector.run(window=window)
            if apply and result.selected:
                existing = {item.code for item in await self.watchlist()}
                for item in result.selected:
                    if item.code in existing:
                        result.skipped.append(item.code)
                        continue
                    try:
                        await asyncio.to_thread(self.add_watch, item.code,
                                                name=item.name)
                        item.added = True
                        result.added.append(item.code)
                        logger.info("自动选股加入自选：%s %s（综合 %.1f，"
                                    "日K %.0f / 分时 %.0f，窗口 %s）",
                                    item.code, item.name, item.combined,
                                    item.daily_score or 0,
                                    item.intraday_score or 0, window)
                    except Exception as exc:  # noqa: BLE001
                        result.notes.append(
                            f"加入自选失败 {item.code}：{brief(exc, BRIEF_TIGHT)}")
            self._auto_select_state = {
                "last_run_at": datetime.now().strftime("%H:%M:%S"),
                "last_window": window,
                "last_seconds": round(time.perf_counter() - started, 2),
                "candidates": result.candidates, "scored": result.scored,
                "selected": [item.as_dict() for item in result.selected],
                "added": list(result.added),
            }
            return result
        except Exception as exc:  # noqa: BLE001 单轮失败不影响下一轮
            self._auto_select_state = {
                "last_run_at": datetime.now().strftime("%H:%M:%S"),
                "last_window": window, "last_error": brief(exc, BRIEF_DEFAULT)}
            logger.warning("自动选股失败(%s)：%s", window, brief(exc, BRIEF_DEFAULT))
            return None

    def auto_select_status(self) -> dict[str, Any]:
        """自动选股的运行状态（前端/排障用）。"""
        from src.intraday.auto_select import (
            DEFAULT_MIN_INTRADAY_SCORE,
            DEFAULT_TOP_N,
            in_trigger_window,
            is_trading_day,
        )

        data_cfg = self._config.data
        task = self._auto_select_task
        return {
            "enabled": bool(getattr(data_cfg, "auto_select_enabled", True)),
            "running": bool(task is not None and not task.done()),
            "trading_day": is_trading_day(),
            "current_window": in_trigger_window(),
            "windows": ["09:25-09:40", "14:45-15:00", "15:05 盘后一次"],
            "top_n": int(getattr(data_cfg, "auto_select_top_n", DEFAULT_TOP_N)),
            "min_intraday_score": float(getattr(
                data_cfg, "auto_select_min_intraday", DEFAULT_MIN_INTRADAY_SCORE)),
            **(self._auto_select_state or {}),
        }

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
            # 还没有任何缓存（首次部署 / 快照过期 / 自选池大改）：此刻返回的是
            # 只有代码/名称的**占位列表**，分数与价格要等后台重算。
            # 前端据此显示"首次重算中"，而不是把 "—" 当成数据坏了。
            "warming": cached is None,
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
                            self._quote_state["quote_last_error"] = brief(exc, BRIEF_DEFAULT)
                            logger.warning("报价快车道刷新失败：%s", brief(exc, BRIEF_DEFAULT))
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 循环必须活着
                logger.exception("报价快车道循环异常，2 秒后继续")
                await asyncio.sleep(2)

    # ==================== 日K级别做T ====================

    def _remember_daily_view(self, code: str) -> None:
        """记一次"用户点开了这只票的日K"（有界，最近在前）。

        ⚠️ 记在**缓存判断之前**：命中缓存也是一次"点开"，也该刷新它的新鲜度
        （否则常看的票会因为"总是命中缓存"而永远排不进最近列表 —— LRU 的语义）。
        """
        key = str(code).strip()
        if not key:
            return
        # 重新插入 = 移到"最近"（dict 保序，Python 3.7+）
        self._recent_daily.pop(key, None)
        self._recent_daily[key] = time.monotonic()
        while len(self._recent_daily) > RECENT_DAILY_LIMIT:
            oldest = next(iter(self._recent_daily))
            self._recent_daily.pop(oldest, None)

    def recent_daily_codes(self, limit: int | None = None) -> list[str]:
        """最近点开过的日K标的（**最近在前**）。供日K预热扩覆盖用。"""
        codes = list(reversed(self._recent_daily.keys()))
        if limit is None:
            return codes
        return codes[:max(0, int(limit))]

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

        # 点开即记（含命中缓存的那次）：日K预热的覆盖靠它扩到"非自选但看过"的票
        self._remember_daily_view(code)
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
            logger.info("日K做T取情绪周期失败(%s): %s", code, brief(exc, BRIEF_TIGHT))
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
                gaps=[brief(exc, BRIEF_DEFAULT)], disclaimer=BACKTEST_DISCLAIMER)
        daily_bars = None
        if self._backend is not None:
            try:
                points = await self._backend.fetch(close_indicator(code))
                daily_bars = daily_bars_from_points(points or [])
            except Exception as exc:  # noqa: BLE001 日线缺失时用分钟合成日线
                logger.warning("回测日线取数失败(%s): %s", code, brief(exc, BRIEF_TIGHT))
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


def _cycle_decision(cycle: Any, config: Any,
                    turnover: Any = None) -> dict[str, Any] | None:
    """情绪周期 + 全市场量能 → **今日做T决策**（纯 JSON），供「市场环境」行右侧。

    用户口径 2026-09-23：原来这三条以"数据缺口"的形式堆在底部数据健康度里 ——

        ⛔ 情绪周期一票否决：大面 13 家 ≥ 10（一票否决）（退潮/冰点不宜区间操作）
        ⛔ 情绪周期一票否决：跌停 13 家 > 10（一票否决）（退潮/冰点不宜区间操作）
        ⛔ 周期阶段「退潮期」：禁止回踩区间提示（做T大概率 T 反），冲高减仓方向不禁止

    它们本来就不是"缺数据"，而是**今天的操作结论**。现在压缩成一句
    `XX期 ·（不）做T降本 · 禁止追高`，逐条依据留在 tooltip 里可核对。

    2026-09-23 追加：**全市场量能预测**（用户口径："缩量XX亿不追高，放量XX亿可做T"）
    并入同一个徽标。为什么合成一枚而不是并排两枚：两者回答的是同一个问题
    （"今天能不能做T/追高"），并排会让人以为是两套独立结论，而实际上
    它们是**两个否决来源**——任一条否决就整体否决。

    Returns:
        `{available, stage, temperature, t_allowed, no_chase, summary, reasons,
          turnover, turnover_text, turnover_verdict}`；
        周期不可用时返回 `None`（不显示徽标，也不编造结论）。
    """
    turnover_block = _turnover_decision(turnover)
    if cycle is None:
        # 周期取不到但量能取到了：仍然给出量能那一段（它是独立结论，
        # 不该因为"情绪周期缺数"就一起消失）。
        if turnover_block is None:
            return None
        return {
            "available": True, "stage": "", "temperature": None,
            "t_allowed": bool(turnover_block["t_allowed"]),
            "no_chase": bool(not turnover_block["chase_allowed"]),
            "veto_signals_on": False,
            "summary": turnover_block["text"],
            "reasons": list(turnover_block["reasons"]),
            "trade_date": str(getattr(turnover_block["raw"], "trade_date", "") or ""),
            "turnover": turnover_block["raw"].to_dict(),
            "turnover_text": turnover_block["text"],
            "turnover_verdict": turnover_block["verdict"],
        }
    if not getattr(cycle, "available", False):
        if turnover_block is None:
            return None
        return {
            "available": True, "stage": "", "temperature": None,
            "t_allowed": bool(turnover_block["t_allowed"]),
            "no_chase": bool(not turnover_block["chase_allowed"]),
            "veto_signals_on": False,
            "summary": turnover_block["text"],
            "reasons": list(turnover_block["reasons"]),
            "trade_date": str(getattr(cycle, "trade_date", "") or ""),
            "turnover": turnover_block["raw"].to_dict(),
            "turnover_text": turnover_block["text"],
            "turnover_verdict": turnover_block["verdict"],
        }
    stage = str(getattr(cycle, "stage", "") or "")
    gates = [str(gate) for gate in (getattr(cycle, "gates", None) or [])]
    # 配置里关掉"周期否决正式信号"时，t_allowed 不再作为禁止依据（与打分链路口径一致）
    veto_on = bool(getattr(getattr(config, "factors", None), "cycle", None)
                   and getattr(config.factors.cycle, "veto_signals", True))
    t_allowed = bool(getattr(cycle, "t_allowed", True)) or not veto_on
    # 一票否决（大面/跌停家数超阈值）时明确禁止追高：
    # 这种环境里追高当天的回撤概率最高，且禁止做T降本时没有日内纠错手段
    no_chase = bool(gates) or not t_allowed
    reasons = list(gates)

    # ---- 并入量能结论（两个否决来源取"最严"）----
    turnover_text = ""
    if turnover_block is not None:
        turnover_text = turnover_block["text"]
        t_allowed = t_allowed and bool(turnover_block["t_allowed"])
        no_chase = no_chase or bool(not turnover_block["chase_allowed"])
        reasons.extend(turnover_block["reasons"])

    flags = [("适合区间操作" if t_allowed else "不宜区间操作")]
    if no_chase:
        flags.append("禁止追高")
    # 量能那一段插在周期与做T之间：读起来是「退潮期 · 缩量 1,180 亿 · 不宜区间操作 · 禁止追高」
    parts = [stage] if stage else []
    if turnover_text:
        parts.append(turnover_text.replace(" 不追高", "").replace(" 可做T", ""))
    parts.extend(flags)
    return {
        "available": True,
        "stage": stage,
        "temperature": getattr(cycle, "temperature", None),
        "t_allowed": t_allowed,
        "no_chase": no_chase,
        "veto_signals_on": veto_on,
        "summary": " · ".join(part for part in parts if part),
        "reasons": reasons,
        "trade_date": str(getattr(cycle, "trade_date", "") or ""),
        "turnover": (turnover_block["raw"].to_dict()
                     if turnover_block is not None else None),
        "turnover_text": turnover_text,
        "turnover_verdict": (turnover_block["verdict"]
                             if turnover_block is not None else ""),
    }


def _turnover_decision(turnover: Any) -> dict[str, Any] | None:
    """量能预测 → `{text, verdict, t_allowed, chase_allowed, reasons, raw}`。

    不可用时返回 `None`（不显示"量能"这一段，也不编造结论）—— 与情绪周期同一
    原则：宁可少一段，也不要给一个没有数据支撑的操作建议。
    """
    if turnover is None or not getattr(turnover, "available", False):
        return None
    return {
        "text": str(getattr(turnover, "verdict_text", "") or ""),
        "verdict": str(getattr(turnover, "verdict", "") or ""),
        "t_allowed": bool(getattr(turnover, "t_allowed", True)),
        "chase_allowed": bool(getattr(turnover, "chase_allowed", True)),
        "reasons": list(getattr(turnover, "reasons", None) or []),
        "raw": turnover,
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

