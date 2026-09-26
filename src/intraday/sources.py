"""分时/分钟级行情多源容灾取数（做T模块专用数据层）。

为什么独立于 ConnectorRouter：
  1. 做T需要的是「OHLCV多字段的分钟bar序列」，而 UnifyingDataPoint 契约是
     「一个指标一个标量」，硬塞进 DataPoint.extra 会让路由层的 TTL/DB 回填语义失真；
  2. 分钟数据是快变量（每分钟都变），若走路由会被逐次写入 SQLite，
     单只票每次320根bar会造成无意义的库膨胀；
  3. 日线上下文仍然**复用**项目既有采集链（QMT→本地CSV→AkShare），
     由 daily_bars_from_points() 归一化其 DataPoint.extra 两种列名口径。

数据源优先级（逐个尝试，首个成功即返回；全部失败则上抛 DataFetchError，
由上层写入 health.gaps —— 绝不使用模拟数据）：

  分钟K线 bars:  tencent → sina(逐笔聚合) → eastmoney → qmt
  当日分时 trend: tencent → sina(逐笔) → eastmoney(trends2) → qmt
  实时快照 quote: tencent(qt) → qmt(get_full_tick，仅启用时)

> **2026-09 变更：QMT 退到链尾且默认不参与（`data.qmt_enabled=false`）**。
> 本机 QMT 终端已失去行情权限（`127.0.0.1:58610` 拒连）且**短期内无法恢复**，
> 而它原来在链首 —— 每次取数都要先撞一次失败的 xtquant 连接（实测白等 4~5s
> 超时）才轮得到腾讯。
> QmtMinuteSource 的代码**全部保留**、位置写在 `intraday_sources` **最后**，
> 将来权限恢复时改 `qmt_enabled` 一个布尔值即可在链尾兜底。
> 上表就是**当前实际生效**的顺序。

各源特性（2026-09 实测于本机）：
  - tencent(腾讯)：一次请求即回 320根分钟K线 / 全天分时 / 实时快照，稳定且免Key。
    实测快照 87ms、分钟K 86ms、分时 78ms —— 三种用途全部可用，且**与东财/新浪
    是不同机房**，本项目历史事故中"东财+新浪同时不可用"时它一直正常。
  - sina(新浪)：逐笔成交（全天约 2000~4500 笔），聚合为分钟bar，作为最深兜底。
  - eastmoney(东财)：**本机 push2/push2his 被按 TLS SNI 阻断**（TCP 443 可连、
    域名请求握手后立刻断；且阻断会漂移 —— 有时连 IP 直连也断）。
    见 `src/core/eastmoney_direct.py` 的实测证据表与规避开关 `MOSS_EM_DIRECT`。
  - qmt(迅投XtMiniQmt)：最权威、可回补长历史，但需终端运行登录**且有行情权限**；
    本机已失去权限且短期无法恢复，故排在链尾并默认关闭。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pandas as pd

from src.core import symbols
from src.core.errors import (
    BRIEF_DEFAULT,
    BRIEF_TIGHT,
    brief,
)
from src.core.exceptions import DataFetchError
from src.intraday.config import IntradayConfig
from src.intraday.features import safe_float
from src.intraday.indicators import aggregate_to_bars
from src.intraday.models import Quote, SourceAttempt
from src.intraday.source_health import SourceHealthTracker

logger = logging.getLogger(__name__)

_TENCENT_MIN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
    "Referer": "https://gu.qq.com/",
}

# JSON 解析失败的标志：说明拿回来的是 HTML（限流/封禁页或接口改版），
# 而不是"这个交易日没有数据"。两者排查方向完全不同，必须分开报。
_NON_JSON_MARKERS = (
    "Expecting value", "JSONDecodeError", "Expecting ',' delimiter",
    "Expecting property name", "拒绝访问", "456",
)

# 数据源显示名（前端 health 面板与日志统一口径）
SOURCE_LABELS = {
    "qmt": "迅投QMT",
    "tencent": "腾讯行情",
    "eastmoney": "东方财富",
    "sina": "新浪财经",
    "router": "项目采集链",
}

BARS_COLUMNS = ["ts", "open", "high", "low", "close", "volume", "amount"]


def exchange_symbol(code: str, *, style: str = "prefix") -> str:
    """6位证券代码 → 带市场标识符号（个股/ETF通用）。

    style="prefix" → sz300308（腾讯/新浪）；style="qmt" → 300308.SZ（迅投）。
    归属规则与项目既有连接器一致，并**补齐ETF代码段**：
      - 5/6/9 开头 → 沪市（51/58 ETF、60/68 个股、900 B股）
      - 其余（0/1/2/3）→ 深市（15/16 ETF、00/30 个股）
    实测踩过的坑：早期只判断 6/9 开头，导致 588170（科创50ETF）被判成深市，
    取数全空且看不出原因。
    """
    try:
        return symbols.exchange_symbol(code, style=style)
    except symbols.SymbolError as exc:
        raise DataFetchError(str(exc)) from exc


def is_etf_code(code: str) -> bool:
    """6位代码是否为场内ETF（沪 51/56/58，深 15/16）。"""
    return symbols.is_etf_code(code)


def close_indicator(code: str) -> str:
    """日线行情采集指标：ETF 走 etf_close，个股走 stock_close。"""
    return f"etf_close:{code}" if is_etf_code(code) else f"stock_close:{code}"


def index_symbol(code: str) -> str:
    """指数代码 → 带市场前缀符号（000xxx沪 / 399xxx深 / 880xxx沪）。

    与项目既有连接器 quote_qmt_code 的归属规则保持一致：
    指数必须显式区分，避免 000001 在「指数」与「平安银行」之间歧义。
    """
    code = str(code).strip()
    if not (code.isdigit() and len(code) == 6):
        raise DataFetchError(f"指数代码须为6位数字: {code!r}")
    if code.startswith(("000", "880")):
        return f"sh{code}"
    if code.startswith("399"):
        return f"sz{code}"
    raise DataFetchError(f"暂支持000xxx(沪)/399xxx(深)/880xxx指数: {code}")


def _parse_quote_line(line: str) -> Quote | None:
    """腾讯快照一行 → Quote（字段不足/价格无效返回 None）。"""
    if '"' not in line:
        return None
    parts = line.split('"')[1].split("~")
    if len(parts) < 48:
        return None
    price = _number(parts[3])
    if price <= 0:
        return None
    return Quote(
        code=parts[2], name=parts[1], price=price,
        prev_close=_number(parts[4]), open=_number(parts[5]),
        high=_number(parts[41]), low=_number(parts[42]),
        change=_number(parts[31]), change_pct=_number(parts[32]),
        volume=_number(parts[36]), amount=_number(parts[37]) * 1e4,
        turnover_rate=_number(parts[38]), pe_ttm=_number(parts[39]),
        pb=_number(parts[46]), limit_up=_number(parts[47]),
    )


def quote_from_qmt_tick(code: str, tick: Any) -> Quote | None:
    """QMT `get_full_tick` 的单只 tick → Quote（价格无效返回 None）。

    单只快照与批量快照**共用这一份解析**：两条路径若各写一套字段口径，
    "自选列表里的涨跌幅"和"面板里的涨跌幅"迟早会对不上（涨跌幅是用户第一眼
    就看的东西，不一致比没有还糟）。注意 tick 里没有 PE/PB，那两个字段由
    估值面板单独取，不在这条链上。
    """
    if not isinstance(tick, dict):
        return None
    price = safe_float(tick.get("lastPrice"))
    if price is None or price <= 0:
        return None
    prev_close = safe_float(tick.get("lastClose"))
    change_pct = None
    if prev_close:
        change_pct = (price - prev_close) / prev_close * 100.0
    return Quote(
        code=code, price=price, prev_close=prev_close,
        open=safe_float(tick.get("open")), high=safe_float(tick.get("high")),
        low=safe_float(tick.get("low")),
        change=None if prev_close is None else price - prev_close,
        change_pct=change_pct, volume=safe_float(tick.get("volume")),
        amount=safe_float(tick.get("amount")),
    )


def _empty_bars() -> pd.DataFrame:
    return pd.DataFrame(columns=BARS_COLUMNS)


def _normalize_bars(frame: pd.DataFrame) -> pd.DataFrame:
    """统一bar表结构并按时间升序、去重、去无效行。"""
    if frame is None or len(frame) == 0:
        return _empty_bars()
    out = frame.copy()
    for column in BARS_COLUMNS:
        if column not in out.columns:
            out[column] = 0.0 if column in ("volume", "amount") else None
    out = out[BARS_COLUMNS]
    out = out.dropna(subset=["close"])
    out["ts"] = out["ts"].astype(str)
    out = out.drop_duplicates(subset=["ts"], keep="last")
    return out.sort_values("ts").reset_index(drop=True)


def _number(value: Any) -> float:
    result = safe_float(value)
    return 0.0 if result is None else result


# A股行情时间恒为北京时间（1991 年后无夏令时），用固定 +8 而不是系统本地时区：
# 服务器时区若是 UTC，用 astimezone 会得到错的时间。
_BEIJING = timezone(timedelta(hours=8))


def _ms_to_beijing(ts_ms: int) -> str | None:
    """QMT `time` 列（UTC 基准的 epoch 毫秒）→ 北京时间字符串。"""
    try:
        stamp = pd.Timestamp(int(ts_ms), unit="ms", tz="UTC").tz_convert(_BEIJING)
    except (ValueError, OSError, OverflowError):
        return None
    return stamp.strftime("%Y-%m-%d %H:%M")


def _qmt_ts_text(value: Any) -> str | None:
    """QMT 时间标识 → `YYYY-MM-DD HH:MM`。

    支持 DataFrame 索引里的 `20260915150000`/`202609151500`/`20260915`、
    Timestamp/datetime，以及带分隔符的字符串；无法识别返回 None。
    """
    if value is None:
        return None
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.strftime("%Y-%m-%d %H:%M")
    text = str(value).strip()
    if not text or text.lower() in ("nat", "none"):
        return None
    if text.isdigit():
        if len(text) == 14:
            return f"{text[:4]}-{text[4:6]}-{text[6:8]} {text[8:10]}:{text[10:12]}"
        if len(text) == 12:
            return f"{text[:4]}-{text[4:6]}-{text[6:8]} {text[8:10]}:{text[10:12]}"
        if len(text) == 8:
            return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
        return None
    try:
        return pd.Timestamp(text).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return None


# ==================================================================
# 当日新鲜度：市场自身的时钟 + QMT 当日增量补下载
# ==================================================================

# 「当前活着的交易日」探针的缓存秒数（一次探测约 0.2ms，30 秒足够覆盖跨日切换）
_SESSION_CLOCK_TTL = 30.0
# QMT 当日数据订阅的去重间隔（秒）见 `src/core/qmt_guard.py::subscribe_once`
# —— 项目日线链与做T链共用同一份去重表，避免对同一个终端重复订阅。
# 「多年份历史」补下载（隔离子进程，实测 2.34s）的去重间隔（秒）。
# 必须去重：次新股/新债的历史本来就不够请求的天数，若每次都补，每个请求都要
# 背一次两秒多的开销。
_QMT_HISTORY_TTL = 600.0

_session_clock: dict[str, Any] = {"stamp": 0.0, "date": ""}
_qmt_history_asked: dict[str, float] = {}


def series_date(frame: pd.DataFrame | None) -> str:
    """bar/分时序列的**最新交易日**（`YYYY-MM-DD`）；空序列返回 ""。

    取 `ts` 的最大日期而不是最后一行的日期，因为两个口径都可能踩坑：
      - 三个源的尾部都可能带一根**正在形成**的bar（实测 09:58 时 60 分钟周期的
        尾部时间戳已经是 10:30、5 分钟周期已经是 10:00），所以"最新日期"才是
        市场实际推进到的那一天；
      - 行序不保证严格递增（聚合/合并后尤其如此），按最后一行判断会误判成陈旧。
    """
    if frame is None or len(frame) == 0 or "ts" not in frame.columns:
        return ""
    return str(frame["ts"].astype(str).str.slice(0, 10).max())


def _probe_qmt_session_date() -> str:
    """向上证指数问「市场现在走到哪个交易日」；取不到返回 ""。

    这是 QMT 专用的探针，**只在 QMT 已启用时**才会走到（见 `live_session_date`
    的多源编排）—— 保留原名与行为，避免破坏既有单测的 monkeypatch 目标。
    """
    from src.core.qmt_guard import qmt_lock

    try:
        from xtquant import xtdata  # type: ignore
    except ImportError:
        return ""
    try:
        with qmt_lock():
            ticks = xtdata.get_full_tick(["000001.SH"]) or {}
    except Exception as exc:  # noqa: BLE001 QMT终端未启动/未登录/未订阅
        logger.debug("交易日时钟探测失败(QMT快照): %s", brief(exc, BRIEF_TIGHT))
        return ""
    tick = ticks.get("000001.SH") or {}
    text = _qmt_ts_text(tick.get("timetag"))
    if text is None and tick.get("time") is not None:
        # 注意口径差异（同一个库里的两套时间最容易踩）：K线 `time` 列是 **UTC 基准**
        # 的 epoch 毫秒（见 `_frame_to_bars` 的事故记录），而 tick 的 `time` 是
        # **本地时间**的 epoch 毫秒 —— 实测 1789523763000 对应本机 09:56:03。
        # 这里若沿用 `_ms_to_beijing` 会多算 8 小时，把盘中判成收盘后。
        try:
            text = datetime.fromtimestamp(
                int(tick["time"]) / 1000.0).strftime("%Y-%m-%d %H:%M")
        except (ValueError, OSError, OverflowError, TypeError):
            text = None
    return "" if text is None else text[:10]


# 市场时钟探针使用的指数：上证综指（腾讯符号）与东财 secid
_SESSION_INDEX_TENCENT = "sh000001"
_SESSION_INDEX_EM = "1.000001"
_SESSION_PROBE_TIMEOUT = 6.0


def _probe_tencent_session_date() -> str:
    """腾讯快照问「市场现在走到哪个交易日」。

    腾讯 `qt.gtimg.cn` 的返回行里第 31 位是 `YYYYMMDDHHMMSS` 格式的行情时间戳
    （实测 `20260922150000`），取前 8 位即当日。这是 QMT 不可用时最可靠的市场
    时钟：它来自行情源自身而不是本机日历，所以节假日/盘前会天然回落到上一个
    交易日，正是 `live_session_date` 需要的语义。
    """
    try:
        with httpx.Client(timeout=_SESSION_PROBE_TIMEOUT,
                          headers=_TENCENT_MIN_HEADERS) as client:
            resp = client.get(f"https://qt.gtimg.cn/q={_SESSION_INDEX_TENCENT}")
            resp.raise_for_status()
            resp.encoding = "gbk"
            text = resp.text
    except Exception as exc:  # noqa: BLE001 网络/编码异常都只意味着"这一路探针不可用"
        logger.debug("交易日时钟探测失败(腾讯快照): %s", brief(exc, BRIEF_TIGHT))
        return ""
    if '"' not in text:
        return ""
    parts = text.split('"')[1].split("~")
    if len(parts) < 33:
        return ""
    stamp = str(parts[30]).strip()
    if len(stamp) >= 8 and stamp[:8].isdigit():
        return f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}"
    return ""


def _probe_eastmoney_session_date() -> str:
    """东财 trends2 问「市场现在走到哪个交易日」（本机常被封，保留作为第三路）。"""
    url = ("https://push2his.eastmoney.com/api/qt/stock/trends2/get?"
           f"secid={_SESSION_INDEX_EM}&fields1=f1,f2,f3&fields2=f51,f53"
           "&iscr=0&ndays=1")
    try:
        with httpx.Client(timeout=_SESSION_PROBE_TIMEOUT,
                          headers=_TENCENT_MIN_HEADERS) as client:
            resp = client.get(url)
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.debug("交易日时钟探测失败(东财trends2): %s", brief(exc, BRIEF_TIGHT))
        return ""
    rows = ((payload.get("data") or {}).get("trends")) or []
    if not rows:
        return ""
    stamp = str(rows[0]).split(" ")[0] if " " in str(rows[0]) else str(rows[0])[:10]
    return stamp[:10] if len(stamp) >= 10 else ""


def live_session_date(*, refresh: bool = False) -> str:
    """当前「活的」交易日（`YYYY-MM-DD`）；时钟不可用时返回 ""（调用方放行）。

    ## 为什么不用「今天是不是工作日」判断
    春节/国庆等**工作日但休市**的日期里，上一交易日的完整分时就是正确数据；
    按日历判断会把它判成"陈旧"，从而把唯一可用的数据也拒掉。
    所以改问行情源本身：上证指数的行情时间戳就是市场推进到的时点 ——
    盘中是今天，节假日/盘前是上一个交易日，天然正确。

    ## 多源编排（2026-09 从「QMT 单源」改为三路）
    原实现**只**探 QMT。QMT 一失去行情权限，这个时钟就恒返回 ""，
    于是 `_try_source` 的新鲜度闸门整段失效 —— 陈旧数据不再被判失败，
    「昨天的完整分时」会顶着今天的名头画出来，而容灾链也不会往下走。
    所以现在按「当前实际可用的源」依次探测：
        腾讯（实测 87ms，最可靠）→ 东财 trends2 → QMT（仅启用时）
    任一路成功即返回；全部失败仍返回 ""，语义与原来一致（放行，fail-safe）。

    ## 时钟取不到时为什么是**放行**而不是拦下
    闸门的目的是"有更新的数据时不要用旧的"，判断依据缺失时宁可不过滤：
    真正会陈旧的是本地行情库，而它不可用时读取本来就会失败，
    腾讯/新浪返回的又恒是当日/最近交易日数据，放行不会放进陈旧数据。
    另外，探测失败不会清掉上一次的可用日期（日期**偏旧**只会让闸门更宽松，
    不会误杀新数据），因此这个缓存在最坏情况下也是 fail-safe 的。
    """
    now = time.monotonic()
    if not refresh and now - float(_session_clock["stamp"]) < _SESSION_CLOCK_TTL:
        return str(_session_clock["date"])
    date = _probe_tencent_session_date()
    if not date:
        date = _probe_eastmoney_session_date()
    if not date and _qmt_source_enabled():
        date = _probe_qmt_session_date()
    if date:
        _session_clock["date"] = date
    _session_clock["stamp"] = now
    return str(_session_clock["date"])


def _qmt_source_enabled() -> bool:
    """QMT 是否启用（`configs/intraday.yaml` 的 `data.qmt_enabled`，默认关闭）。

    单独抽成一个函数：市场时钟探针在**模块级**被调用（`live_session_date` 是自由
    函数，没有 provider 实例），拿不到 provider 上按需覆盖的配置，只能读热重载的
    全局配置。配置读取本身有 mtime 缓存，不会成为热点。
    读取失败按**关闭**处理：本机 QMT 无行情权限时它只会贡献一次连接超时。
    """
    try:
        from src.intraday.config import load_intraday_config

        return bool(load_intraday_config().data.qmt_enabled)
    except Exception as exc:  # noqa: BLE001 配置损坏不该让时钟探针炸掉
        logger.debug("读取 qmt_enabled 失败，按关闭处理: %s", brief(exc, BRIEF_TIGHT))
        return False


# ==================================================================
# 迅投 QMT（主源）
# ==================================================================

class QmtMinuteSource:
    """xtquant 分钟K线/分时/快照（线程池阻塞调用，未安装或终端未启动→DataFetchError）。"""

    name = "qmt"

    def __init__(self) -> None:
        self._xtdata: Any = None

    def _client(self) -> Any:
        if self._xtdata is not None:
            return self._xtdata
        try:
            from xtquant import xtdata  # type: ignore
        except ImportError as exc:
            raise DataFetchError(
                "xtquant未安装，请执行: uv sync --extra data") from exc
        xtdata.enable_hello = False
        self._xtdata = xtdata
        return xtdata

    def warm_today(self, code: str, period: str) -> bool:
        """订阅该标的该周期，让 QMT 把**当日**bar 推进本地库（实测 1 秒内到位）。

        ## 为什么必须做（2026-09-16 09:55 实测）
        QMT **不会**自动把当天分钟bar写进本地库，未触发之前
        `get_market_data_ex` 只会返回**上一交易日**的数据：

            600036.SH 当天 1m = 26 行   ← 此前对它做过一次订阅/补下载
            601988.SH / 000001.SZ / 600519.SH = 0 行   ← 从未触发过

        而分时链路把「数据里最新的一天」当成当日渲染，于是 09:52 打开招商银行
        看到的是**昨天 09:30~15:00 一整条完整曲线**（用户报告的现象）。

        ## 为什么是订阅而不是 download_history_data
        做T链路有一条硬性约定（`tests/unit/test_qmt_guard.py` 直接断言）：
        **绝不允许进程内调用 `download_history_data`** —— 它曾在无锁并发下原生
        崩溃、连 traceback 都没有就带走整个服务进程（见 `qmt_guard` 顶部事故记录）。
        而 `subscribe_quote` 与同模块已在进程内使用的 `get_full_tick`/
        `get_market_data_ex` 属于同一类接口，实测它把当日数据**连历史一起**推了下来
        （全新标的、订阅前为 0 行）：

            601318.SH / 000002.SZ   订阅前            当日=0 行   覆盖=0 个交易日
            subscribe_quote('1m', count=-1) 1 秒后    当日=38 行  覆盖=133 个交易日
            subscribe_quote('5m', count=-1) 1 秒后    当日=8  行  覆盖=172 个交易日

        比 `download_history_data`（进程内 0~45ms 但只补当日，或隔离子进程 2.34s
        补历史）还多两件事：**历史一并到位**（于是下面「覆盖交易日不够就补历史」
        那条隔离下载在绝大多数情况下不再触发），且**订阅让本地库随行情持续更新**，
        不必反复预热。

        返回本次是否**新建**了订阅 —— 首建时数据要 1 秒左右才落本地，
        `_load` 据此短暂等待，避免第一次请求因为"数据在路上"就降级到别的源。
        """
        from src.core.qmt_guard import subscribe_once

        qmt_code = exchange_symbol(code, style="qmt")
        try:
            return subscribe_once(qmt_code, period, client=self._client())
        except Exception as exc:  # noqa: BLE001 订阅失败不影响读取（旧数据仍可能可用）
            logger.warning("QMT订阅失败(%s %s): %s",
                           qmt_code, period, brief(exc, BRIEF_DEFAULT))
            return False

    def _await_today(self, frame: Any, read: Any) -> Any:
        """订阅首建后短暂等待当日数据落到本地（实测 1 秒内到位，最长等 1.2 秒）。

        只在**刚新建订阅**那一轮等：首屏不该因为"数据在路上"就降级到别的源；
        而已经订阅过的标的要么已经有数据、要么终端真的有问题，再等只是白等。
        `_load` 在 `asyncio.to_thread` 的工作线程里跑，这里的 sleep 不卡事件循环。

        等的锚点是 `live_session_date()`（市场自己的交易日）：节假日/盘前它会
        回落到上一交易日，于是这里等的是"最近那个交易日的数据"，语义依然正确。
        """
        session = live_session_date()
        if not session:
            return frame
        stamp = session.replace("-", "")
        deadline = time.monotonic() + 1.2

        def _stamp_present(data: Any) -> bool:
            # ⚠️ 这个判断**每次 read 只算一次**。原实现把它写在循环条件里，
            # 每次重试都用 `{str(value)[:8] for value in data.index}` 重建集合 ——
            # QMT 分钟线索引有上千个 Timestamp，`str()` 每个都要走一次 datetime
            # 格式化，于是每 0.2 秒烧掉一次可观 CPU。
            # 实测后果（2026-09-17）：并发加载几十只票时把这台机器的 CPU 打满
            # （单个进程累计 1796s CPU、2.4GB 内存），整个服务失去响应 ——
            # 表现就是"重启后前端好久没数据"。索引在同一份 DataFrame 上不会变，
            # 没有任何理由重复构造。
            if data is None or len(data) == 0:
                return False
            return stamp in {str(value)[:8] for value in data.index}

        while True:
            if _stamp_present(frame):
                return frame
            if time.monotonic() >= deadline:
                return frame
            time.sleep(0.2)
            frame = read()

    def _load(self, code: str, period: str, days: int) -> pd.DataFrame:
        """读本地分钟线（**同步**）；本地为空时触发**子进程隔离**的补下载。

        所有 xtquant 调用都必须在 `qmt_lock()` 里：实测服务进程曾在并发访问
        QMT（快速切标的 + 补下载同时发生）时无 traceback 猝死，见
        `src/core/qmt_guard.py` 顶部的事故记录。

        ## 为什么 async 调用方要用 `_load_async`

        补下载是 `subprocess.run`，最长 `timeout`=45s。**在协程里直接调用会按住
        整个事件循环** —— 实测 2026-09-17：服务启动后盘后自动选股立即开跑，
        几十只票逐个补下载，期间 `/api/v1/health` 排队 **111.8 秒**才返回，
        而服务其实 3.9 秒就已启动完成。用户感受就是"重启后前端好久没数据"。
        异步路径 `await _load_async(...)`（丢线程池）不受影响；本同步版保留给
        已经在线程里跑的调用方（`fetch_bars`/`_trend_sync` 都经 `to_thread`）。
        """
        from src.core.qmt_guard import download_history_isolated, qmt_lock

        qmt_code = exchange_symbol(code, style="qmt")
        # 先订阅当日数据：不做这一步，QMT 只会回上一交易日的数据，
        # 分时就会被渲染成「昨天的一整天」（见 warm_today 的实测记录）。
        xtdata = self._client()
        # 多取几天缓冲，保证跨节假日仍有足够样本
        start = (pd.Timestamp.now() - pd.Timedelta(days=max(days, 1) * 3 + 10)
                 ).strftime("%Y%m%d")

        def _read() -> Any:
            with qmt_lock():
                data = xtdata.get_market_data_ex(
                    [], [qmt_code], period=period, start_time=start,
                    dividend_type="front", fill_data=False)
            return data.get(qmt_code) if isinstance(data, dict) else None

        try:
            subscribed = self.warm_today(code, period)
            frame = _read()
            if subscribed:
                frame = self._await_today(frame, _read)
            empty = frame is None or len(frame) == 0
            covered = 0 if empty else self._covered_trading_days(frame)
            # 判断标准是「**覆盖了几个交易日**」而不是「有没有数据」：
            #   - 只认"空"会漏掉一个实测踩到的坑 —— 当日数据刚补进本地库后，
            #     库就不再是空的了，于是历史永远补不上（实测 600030 的 5m 只回
            #     7 根，而它本可以有 800 多根；分钟指标与阈值回测都会因此失真）；
            #   - 用"根数"判断又会把次新股误判成永远不完整。
            # 订阅会连历史一起推下来，所以这条隔离下载通常不会触发。
            short = (not empty and days > covered
                     and self._history_due(qmt_code, period))
            if empty or short:
                # 本地不完整 → 隔离补下载（子进程若原生崩溃不会带走服务进程）
                ok, detail = download_history_isolated(
                    qmt_code, period, start_time=start)
                if not ok:
                    if empty:
                        logger.warning("QMT补下载未成功(%s %s): %s",
                                       qmt_code, period, detail)
                        raise DataFetchError(
                            f"QMT本地无分钟行情且补下载失败({qmt_code} {period})：{detail}")
                    # 已经有一部分数据：补下载失败就降级用它，别把整条链打断
                    logger.warning(
                        "QMT历史补下载未成功(%s %s)，先用本地已有的 %d 根（覆盖 %d 个交易日）: %s",
                        qmt_code, period, len(frame), covered, detail)
                else:
                    frame = _read()
        except DataFetchError:
            raise
        except Exception as exc:  # noqa: BLE001 QMT服务未启动/终端未登录
            raise DataFetchError(f"QMT分钟行情读取失败({qmt_code}): {exc}") from exc
        if frame is None or len(frame) == 0:
            raise DataFetchError(f"QMT无分钟行情({qmt_code} period={period})")
        return frame

    async def _load_async(self, code: str, period: str, days: int) -> pd.DataFrame:
        """`_load` 的不阻塞事件循环版本（协程里用它，见 `_load` 的说明）。"""
        return await asyncio.to_thread(self._load, code, period, days)

    @staticmethod
    def _covered_trading_days(frame: Any) -> int:
        """原始QMT帧覆盖了几个交易日（索引前 8 位是 `YYYYMMDD`）。"""
        try:
            return len({str(value)[:8] for value in frame.index})
        except Exception:  # noqa: BLE001 索引异常时不做数量判断，只当覆盖不足
            return 0

    def _history_due(self, qmt_code: str, period: str) -> bool:
        """是否允许再为这个(标的,周期)触发一次"多年份历史"补下载。"""
        key = f"{qmt_code}:{period}"
        now = time.monotonic()
        last = _qmt_history_asked.get(key)
        if last is not None and now - last < _QMT_HISTORY_TTL:
            return False
        _qmt_history_asked[key] = now
        return True

    @staticmethod
    def _frame_to_bars(frame: pd.DataFrame) -> pd.DataFrame:
        """QMT 秒/分钟K线 → 标准 bar 表。

        **时间口径（实测踩坑）**：QMT 的 `time` 列是 epoch 毫秒且以 **UTC** 为基准，
        而 DataFrame 的索引才是本地时间字符串（`20260915150000` → 15:00）。
        早期直接用 `pd.Timestamp(ms, unit="ms")` 得到 UTC 朴素时间，实测当日最后一根
        1分钟bar 变成 `07:00`（真实 15:00），于是分时图 X 轴整体**早 8 小时**
        （09:30 的开盘显示成 01:30），而日期部分不变所以 `trade_date` 看起来是对的、
        缺陷一直没暴露。现在优先用索引/`stime` 字符串，缺失时才把 `time` 当 UTC 毫秒
        换算到北京时间。
        """
        rows = []
        index_values = list(frame.index)
        for position, record in enumerate(frame.itertuples(index=False)):
            mapping = record._asdict() if hasattr(record, "_asdict") else None
            if mapping is None:
                mapping = dict(zip(frame.columns, record, strict=True))
            ts_text = _qmt_ts_text(
                index_values[position] if position < len(index_values) else None)
            if ts_text is None:
                ts_text = _qmt_ts_text(mapping.get("stime"))
            if ts_text is None:
                ts_ms = mapping.get("time")
                if ts_ms is None:
                    continue
                ts_text = _ms_to_beijing(int(ts_ms))
            if ts_text is None:
                continue
            rows.append({
                "ts": ts_text,
                "open": _number(mapping.get("open")),
                "high": _number(mapping.get("high")),
                "low": _number(mapping.get("low")),
                "close": _number(mapping.get("close")),
                "volume": _number(mapping.get("volume")),
                "amount": _number(mapping.get("amount")),
            })
        return _normalize_bars(pd.DataFrame(rows))

    async def fetch_bars(self, code: str, period: str, days: int) -> pd.DataFrame:
        frame = await self._load_async(code, period, days)
        bars = self._frame_to_bars(frame)
        if bars.empty:
            raise DataFetchError(f"QMT分钟线为空({code} {period})")
        return bars

    def _trend_sync(self, code: str) -> pd.DataFrame:
        frame = self._load(code, "1m", 1)
        bars = self._frame_to_bars(frame)
        if bars.empty:
            raise DataFetchError(f"QMT分时为空({code})")
        latest = bars["ts"].str[:10].iloc[-1]
        today = bars[bars["ts"].str.startswith(latest)].copy()
        today["price"] = today["close"]
        cum_amount = (today["close"] * today["volume"]).cumsum()
        cum_volume = today["volume"].cumsum().replace(0.0, pd.NA)
        today["avg_price"] = (cum_amount / cum_volume).astype("float64")
        return today[["ts", "price", "avg_price", "volume", "amount"]]

    async def fetch_trend(self, code: str) -> pd.DataFrame:
        return await asyncio.to_thread(self._trend_sync, code)


# ==================================================================
# 腾讯行情（无依赖主力源）
# ==================================================================

class TencentSource:
    """腾讯行情：分钟K线 / 全天分时 / 实时快照（一次请求即回，免Key稳定）。"""

    name = "tencent"
    base_kline = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
    base_minute = "https://web.ifzq.gtimg.cn/appstock/app/minute/query"
    base_quote = "https://qt.gtimg.cn/q="
    base_fqkline = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    # 腾讯周期写法：m1/m5/m15/m30/m60
    period_map = {"1m": "m1", "5m": "m5", "15m": "m15", "30m": "m30", "60m": "m60"}

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        # 批量快照的**共享缓存**：`{symbol: Quote}` + 取数时刻。
        #
        # 为什么必须有：`_raw_quotes` 原来**完全没有缓存**，而自选池刷新会对
        # 每只票各调一次 `fetch_quote` → N 只票就是 N 次真实 HTTP。
        # 实测单次 2.1 秒，28 只票就是几十秒量级的串行等待
        # （用户报"自选 30+ 只时强制刷新要 6~7 秒"，根因就在这里）。
        #
        # 腾讯 qt 接口本身**支持一次请求多代码**，所以这里把最近一次批量结果
        # 缓存下来：同一批（或其子集）的后续请求直接命中，不再发 HTTP。
        # 5 秒窗口足够覆盖"一轮自选刷新"的全部请求，又不会让价格变旧。
        self._quote_cache: dict[str, Quote] = {}
        self._quote_at: float = 0.0
        self._quote_ttl = 5.0

    async def fetch_bars(self, code: str, period: str, days: int) -> pd.DataFrame:
        symbol = exchange_symbol(code)
        tencent_period = self.period_map.get(period)
        if tencent_period is None:
            raise DataFetchError(f"腾讯源不支持周期 {period}")
        # 每交易日48根5分钟bar：按天数换算请求根数（上限320根，腾讯接口限制）
        count = min(320, max(48, days * 48))
        url = f"{self.base_kline}?param={symbol},{tencent_period},,{count}"
        try:
            resp = await self._client.get(url, headers=_TENCENT_MIN_HEADERS)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001 网络/JSON异常统一转数据源失败
            raise DataFetchError(f"腾讯分钟K线请求失败({symbol}): {exc}") from exc
        node = (payload.get("data") or {}).get(symbol) or {}
        rows = node.get(tencent_period) or []
        if not rows:
            raise DataFetchError(f"腾讯分钟K线为空({symbol} {period})")
        records = []
        for row in rows:
            if len(row) < 6:
                continue
            stamp = str(row[0])
            records.append({
                "ts": (f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]} "
                       f"{stamp[8:10]}:{stamp[10:12]}"),
                # 腾讯字段序：时间,开,收,高,低,成交量(手),{},成交额(亿元)
                "open": _number(row[1]), "close": _number(row[2]),
                "high": _number(row[3]), "low": _number(row[4]),
                "volume": _number(row[5]),
                # 第8位「成交额」实测不可靠：2026-09-15 中际旭创当日逐bar求和为161.78亿，
                # 而快照口径真实成交额为156.14亿（高估约3.6%），若用它算VWAP会把均价
                # 抬到901元（真实869.6元）。故置0，交由 vwap_series 回退到典型价
                # (high+low+close)/3 —— 实测该口径VWAP=869.31，与真实值仅差0.03%。
                "amount": 0.0,
            })
        bars = _normalize_bars(pd.DataFrame(records))
        if bars.empty:
            raise DataFetchError(f"腾讯分钟K线解析为空({symbol} {period})")
        return bars

    async def fetch_trend(self, code: str) -> pd.DataFrame:
        """当日分时（1分钟）。腾讯的成交量/额字段为**累计值**，此处还原为增量。"""
        symbol = exchange_symbol(code)
        url = f"{self.base_minute}?code={symbol}"
        try:
            resp = await self._client.get(url, headers=_TENCENT_MIN_HEADERS)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise DataFetchError(f"腾讯分时请求失败({symbol}): {exc}") from exc
        node = (payload.get("data") or {}).get(symbol) or {}
        inner = node.get("data") or {}
        rows = inner.get("data") or []
        date = str(inner.get("date") or "")
        if not rows or len(date) != 8:
            raise DataFetchError(f"腾讯分时为空({symbol})")
        day = f"{date[:4]}-{date[4:6]}-{date[6:8]}"
        records = []
        prev_cum_volume = 0.0
        prev_cum_amount = 0.0
        for row in rows:
            parts = str(row).split()
            if len(parts) < 3:
                continue
            hhmm = parts[0]
            cum_volume = _number(parts[2]) if len(parts) > 2 else 0.0
            cum_amount = _number(parts[3]) if len(parts) > 3 else 0.0
            records.append({
                "ts": f"{day} {hhmm[:2]}:{hhmm[2:4]}",
                "price": _number(parts[1]),
                "volume": max(0.0, cum_volume - prev_cum_volume),
                "amount": max(0.0, cum_amount - prev_cum_amount),
                "cum_volume": cum_volume,
                "cum_amount": cum_amount,
            })
            prev_cum_volume, prev_cum_amount = cum_volume, cum_amount
        frame = pd.DataFrame(records)
        if frame.empty:
            raise DataFetchError(f"腾讯分时解析为空({symbol})")
        volume = frame["volume"]
        frame["avg_price"] = (
            (frame["price"] * volume).cumsum() / volume.cumsum().replace(0.0, pd.NA)
        ).astype("float64")
        return frame[["ts", "price", "avg_price", "volume", "amount"]]

    async def fetch_quote(self, code: str) -> Quote:
        """个股实时快照（腾讯 qt 接口 88 字段，含 PE/PB）。"""
        result = await self._raw_quotes([exchange_symbol(code)])
        quote = next(iter(result.values()), None)
        if quote is None:
            raise DataFetchError(f"腾讯个股快照无有效数据({code})")
        return quote

    async def fetch_index_quote(self, code: str) -> Quote:
        """指数实时快照（000300/399006 等）。"""
        result = await self._raw_quotes([index_symbol(code)])
        quote = next(iter(result.values()), None)
        if quote is None:
            raise DataFetchError(f"腾讯指数快照无有效数据({code})")
        return quote

    async def fetch_index_daily_kline(self, code: str,
                                      days: int = 10) -> pd.DataFrame:
        """指数日线（含成交量），用于「当日预测量能 vs 昨日量能」比较。

        腾讯 fqkline 返回列序：[日期, 开, 收, 高, 低, 成交量(手)]，最后一行为当日。
        """
        symbol = index_symbol(code)
        url = (f"{self.base_fqkline}?param={symbol},day,,,{max(2, days)},qfq")
        try:
            resp = await self._client.get(url, headers=_TENCENT_MIN_HEADERS)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise DataFetchError(f"腾讯指数日线请求失败({symbol}): {exc}") from exc
        node = (payload.get("data") or {}).get(symbol) or {}
        rows = node.get("day") or node.get("qfqday") or []
        records = []
        for row in rows:
            if len(row) < 6:
                continue
            records.append({
                "date": str(row[0]), "open": _number(row[1]),
                "close": _number(row[2]), "high": _number(row[3]),
                "low": _number(row[4]), "volume": _number(row[5]),
            })
        frame = pd.DataFrame(records)
        if frame.empty:
            raise DataFetchError(f"腾讯指数日线为空({symbol})")
        return frame

    async def fetch_overseas_quotes(self, symbols: list[str]) -> list[Any]:
        """海外映射行情（美股 us* / 韩股 kr*），一次请求取回。

        字段布局与A股不同（时间在[30]、涨跌幅在[32]），故单独解析。
        """
        if not symbols:
            return []
        try:
            resp = await self._client.get(
                f"{self.base_quote}{','.join(symbols)}",
                headers=_TENCENT_MIN_HEADERS)
            resp.raise_for_status()
            resp.encoding = "gbk"
            text = resp.text
        except Exception as exc:  # noqa: BLE001
            raise DataFetchError(f"腾讯海外行情请求失败: {exc}") from exc
        from src.intraday.market import parse_tencent_overseas_line

        quotes = []
        for line in text.strip().split("\n"):
            if "pv_none_match" in line:
                continue
            market = "kr" if "kr" in line.split("=")[0] else "us"
            quote = parse_tencent_overseas_line(line, market)
            if quote is not None:
                quotes.append(quote)
        if not quotes:
            raise DataFetchError("腾讯海外行情无有效行")
        return quotes

    async def fetch_quotes_batch(self, codes: list[str]) -> dict[str, Quote]:
        """批量个股快照（同业PE/PB对比用，≤60只一次取回）。"""
        if not codes:
            return {}
        return await self._raw_quotes([exchange_symbol(c) for c in codes])

    async def _raw_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """腾讯 qt 批量接口原始调用（一次请求多代码）。

        带 **5 秒共享缓存**（见 `__init__` 的说明）：同一批（或子集）在窗口内
        直接复用上次结果。自选池刷新时 N 只票各调一次 `fetch_quote`，
        原来会打 N 次 HTTP（每次约 2.1 秒），现在只打 1 次。
        """
        if not symbols:
            return {}
        now = time.monotonic()
        if self._quote_cache and (now - self._quote_at) < self._quote_ttl:
            if all(symbol in self._quote_cache for symbol in symbols):
                return {symbol: self._quote_cache[symbol] for symbol in symbols}
        try:
            resp = await self._client.get(
                f"{self.base_quote}{','.join(symbols)}", headers=_TENCENT_MIN_HEADERS)
            resp.raise_for_status()
            resp.encoding = "gbk"
            text = resp.text
        except Exception as exc:  # noqa: BLE001
            raise DataFetchError(f"腾讯批量快照失败: {exc}") from exc
        result: dict[str, Quote] = {}
        parsed: dict[str, Quote] = {}
        for line in text.strip().split("\n"):
            quote = _parse_quote_line(line)
            if quote is not None:
                parsed[quote.code] = quote
                result[quote.code] = quote
        if not result:
            raise DataFetchError("腾讯批量快照无有效行")
        # 合并进共享缓存（保留窗口内的其它代码，便于不同子集各自命中）
        if (now - self._quote_at) >= self._quote_ttl:
            self._quote_cache = {}
        # ⚠️ 缓存键必须是**查询用的符号**（`sh600150`），不能只存 `quote.code`
        # （那是 `600150`）—— 否则上面 `symbol in self._quote_cache` 永远不成立，
        # 缓存形同虚设。实测被单测抓出：同一代码连打 3 次发了 3 次 HTTP。
        by_code = {code.zfill(6): quote for code, quote in parsed.items()}
        for symbol in symbols:
            quote = parsed.get(symbol) or by_code.get(symbol[-6:])
            if quote is not None:
                self._quote_cache[symbol] = quote
        self._quote_at = now
        return result


# ==================================================================
# 东方财富（网络可达时启用）
# ==================================================================

class EastmoneySource:
    """东财分钟K线（akshare stock_zh_a_hist_min_em）。本机网络下常被阻断，保留在链上。"""

    name = "eastmoney"

    async def fetch_bars(self, code: str, period: str, days: int) -> pd.DataFrame:
        minutes = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "60m": "60"}.get(period)
        if minutes is None:
            raise DataFetchError(f"东财源不支持周期 {period}")
        frame = await asyncio.to_thread(self._load, code, minutes)
        if frame is None or len(frame) == 0:
            raise DataFetchError(f"东财分钟K线为空({code} {period})")
        records = []
        for _, row in frame.iterrows():
            records.append({
                "ts": str(row.get("时间")),
                "open": _number(row.get("开盘")), "high": _number(row.get("最高")),
                "low": _number(row.get("最低")), "close": _number(row.get("收盘")),
                "volume": _number(row.get("成交量")), "amount": _number(row.get("成交额")),
            })
        bars = _normalize_bars(pd.DataFrame(records))
        if bars.empty:
            raise DataFetchError(f"东财分钟K线解析为空({code} {period})")
        return bars.tail(max(48, days * 48)).reset_index(drop=True)

    @staticmethod
    def _load(code: str, minutes: str) -> Any:
        try:
            import akshare as ak
        except ImportError as exc:
            raise DataFetchError("akshare未安装，请执行: uv sync --extra data") from exc
        return ak.stock_zh_a_hist_min_em(symbol=code, period=minutes, adjust="")


# ==================================================================
# 新浪逐笔（最深兜底）
# ==================================================================

class SinaSource:
    """新浪逐笔成交（stock_intraday_sina）→ 聚合为分钟bar。

    逐笔数据自带价格与成交量，成交额以 价格×成交量 估算（新浪不提供逐笔金额），
    因此该路径的VWAP口径为「逐笔价格加权」，精度略低于真实成交额加权，
    在 health.attempts 中明确标注。
    """

    name = "sina"

    async def fetch_bars(self, code: str, period: str, days: int) -> pd.DataFrame:
        frame = await asyncio.to_thread(self._load_ticks, code)
        if frame is None or len(frame) == 0:
            raise DataFetchError(f"新浪逐笔为空({code})")
        minutes = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60}.get(period)
        if minutes is None:
            raise DataFetchError(f"新浪源不支持周期 {period}")
        # 逐笔原始帧的列是 ticktime/price/volume（没有 ts），必须先归一化再聚合，
        # 否则 aggregate_to_bars 会因缺 ts 列直接 KeyError——而这条路径恰恰是
        # 「QMT未启动 + 腾讯网络抖动」时唯一能兜住的分时来源，必须可靠。
        points = self._ticks_to_points(frame)
        if points.empty:
            raise DataFetchError(f"新浪逐笔解析为空({code})")
        bars = _normalize_bars(aggregate_to_bars(points, minutes=minutes))
        if bars.empty:
            raise DataFetchError(f"新浪逐笔聚合后为空({code} {period})")
        return bars

    async def fetch_trend(self, code: str) -> pd.DataFrame:
        frame = await asyncio.to_thread(self._load_ticks, code)
        if frame is None or len(frame) == 0:
            raise DataFetchError(f"新浪逐笔为空({code})")
        points = self._ticks_to_points(frame)
        if points.empty:
            raise DataFetchError(f"新浪逐笔解析为空({code})")
        # 同一分钟多笔 → 合并（取最后价，量与额求和）
        points["minute"] = points["ts"].str.slice(0, 16)
        grouped = points.groupby("minute", sort=True).agg(
            price=("price", "last"), volume=("volume", "sum"),
            amount=("amount", "sum")).reset_index()
        grouped["avg_price"] = (
            grouped["amount"].cumsum()
            / grouped["volume"].cumsum().replace(0.0, pd.NA)
        ).astype("float64")
        grouped["ts"] = grouped["minute"]
        return grouped[["ts", "price", "avg_price", "volume", "amount"]]

    @staticmethod
    def _ticks_to_points(frame: Any) -> pd.DataFrame:
        """新浪逐笔原始帧（ticktime/price/volume）→ 统一 ts/price/volume/amount 点序列。

        成交额以 价格×成交量 估算（新浪不提供逐笔金额），故该路径的 VWAP 口径为
        「逐笔价格加权」，精度略低于真实成交额加权，已在 health.attempts 中标注。
        """
        day = str(frame.attrs.get("date") or "")
        records = []
        for _, row in frame.iterrows():
            price = _number(row.get("price"))
            volume = _number(row.get("volume"))
            if price <= 0:
                continue
            records.append({
                "ts": f"{day} {str(row.get('ticktime'))[:5]}",
                "price": price,
                "volume": volume,
                "amount": price * volume,
            })
        if not records:
            return pd.DataFrame(columns=["ts", "price", "volume", "amount"])
        return pd.DataFrame(records)

    @staticmethod
    def _load_ticks(code: str) -> Any:
        try:
            import akshare as ak
        except ImportError as exc:
            raise DataFetchError("akshare未安装，请执行: uv sync --extra data") from exc
        symbol = exchange_symbol(code)
        # 新浪逐笔仅保留最近数个交易日，取最近5个自然日逐个尝试
        today = pd.Timestamp.now().normalize()
        last_error: Exception | None = None
        for offset in range(0, 6):
            day = today - pd.Timedelta(days=offset)
            if day.weekday() >= 5:  # 周末无交易
                continue
            try:
                frame = ak.stock_intraday_sina(
                    symbol=symbol, date=day.strftime("%Y%m%d"))
            except Exception as exc:  # noqa: BLE001 非交易日返回空/结构异常
                last_error = exc
                continue
            if frame is not None and len(frame) and "ticktime" in frame.columns:
                frame.attrs["date"] = day.strftime("%Y-%m-%d")
                return frame
        # 把"被限流封禁"和"确实没有交易日"区分开。
        # 实测：新浪对本机 IP 返回 HTTP 456 + HTML「拒绝访问」页（5~60 分钟自动解封），
        # akshare 拿到 HTML 去解析 JSON 就抛 "Expecting value" ——
        # 原来的文案统一写成"无可取交易日"，会把排查方向直接带偏到交易日历上。
        text = str(last_error)
        if any(marker in text for marker in _NON_JSON_MARKERS):
            raise DataFetchError(
                f"新浪逐笔返回非 JSON({symbol})：通常是新浪对该 IP 限流/封禁后返回"
                f"「拒绝访问」HTML 页（实测 HTTP 456，5~60 分钟自动解封），"
                f"也可能是接口结构变更。原始错误：{text[:120]}")
        raise DataFetchError(
            f"新浪逐笔无可取交易日({symbol}): {text[:120]}")


# ==================================================================
# 多源编排
# ==================================================================

class IntradayDataProvider:
    """分钟数据的多源容灾取数（failover + 进程内TTL缓存 + 尝试日志）。"""

    def __init__(self, config: IntradayConfig,
                 client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._client = client
        self._owns_client = client is None
        self._qmt = QmtMinuteSource()
        self._tencent = TencentSource(client) if client is not None else None
        self._eastmoney = EastmoneySource()
        self._sina = SinaSource()
        self._cache: dict[str, tuple[float, Any]] = {}
        # 失败冷却：key = "{source}:{method}" → 冷却截止的 monotonic 时间
        self._cooldown: dict[str, float] = {}
        # per-key 单飞锁：同一标的同一用途的并发请求只真正取数一次，其余等结果。
        # 没有它时，「WS推送 + 页面首屏 + 自选列表」会同时对同一标的取数，
        # 在 QMT/腾讯都不可用、退化到新浪逐笔（39页分页下载≈20s）时会被放大 N 倍。
        self._locks: dict[str, asyncio.Lock] = {}
        # 源健康表：EWMA 延迟 + 成功率 + 失败冷却 → 决定回退链顺序（测量驱动，不写死）
        self.health = SourceHealthTracker(
            cooldown_seconds=config.data.source_cooldown_seconds)

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._config.data.request_timeout, follow_redirects=True)
            self._tencent = TencentSource(self._client)
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
            self._tencent = None

    def _cache_get(self, key: str, ttl: int) -> Any | None:
        hit = self._cache.get(key)
        if hit is None:
            return None
        stamp, value = hit
        if ttl > 0 and time.monotonic() - stamp < ttl:
            return value
        return None

    def _cache_put(self, key: str, value: Any) -> None:
        self._cache[key] = (time.monotonic(), value)

    def invalidate(self) -> None:
        """清空取数缓存与熔断状态（手动刷新按钮/测试用）。

        注意：`invalidate()` 现在也会**关闭熔断器**（清空源健康表里的冷却），
        这样用户点「强制刷新」时能立刻重试 QMT，而不是继续等冷却 —— 这正是
        "用户刚重开 QMT 终端，点一下刷新就该恢复"的操作预期。
        """
        self._cache.clear()
        self._cooldown.clear()
        self.health.reset_cooldowns()

    def _cooldown_key(self, source: str, method: str) -> str:
        return f"{source}:{method}"

    def _lock_for(self, key: str) -> asyncio.Lock:
        """取该缓存键的单飞锁（懒创建；协程内单线程访问，无需再套锁）。"""
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _in_cooldown(self, source: str, method: str) -> int:
        """[兼容保留] 旧的冷却查询；权威判定已迁移到 `self.health.should_attempt()`。

        两条冷却账本并存会造成口径不一致，因此 `_mark_failed` 只做记录，
        真正的"要不要试这个源"一律问 `health`（它带半开探测与统计）。
        """
        until = self._cooldown.get(self._cooldown_key(source, method))
        if until is None:
            return 0
        return max(0, int(until - time.monotonic()))

    def _mark_failed(self, source: str, method: str) -> None:
        window = self._config.data.source_cooldown_seconds
        if window > 0:
            self._cooldown[self._cooldown_key(source, method)] = (
                time.monotonic() + window)

    def _ordered_sources(self, method: str = "bars") -> list[str]:
        """按**实测响应速度**排序（配置顺序只作先验）。

        实测（2026-09，逐源直测）：
            腾讯    快照 87ms / 分钟K 86ms / 分时 78ms（公网，三种用途全通）
            新浪    逐笔聚合（最慢，最深兜底）
            东财    本机 push2/push2his 被 TLS SNI 阻断（且阻断会漂移）
            迅投QMT  本机终端内存读取（原为 0ms，但已失去行情权限、短期无法恢复）

        ## 关于"谁在链首"
        `health.rank` **只对已有 ≥3 次样本的源重排**（样本不足时照抄传入的
        `prior` 顺序），所以**配置顺序就是默认顺序**。要改顺序只能改配置
        （`data.intraday_sources`），代码里的 `pool` 只是配置缺项时的兜底。

        实测踩过的坑：把 qmt 写在配置最前、只靠 `qmt_enabled=false` 关掉，
        结果所有取值都正确、但"意图"被藏进了另一个开关 —— 谁只改
        `qmt_enabled=true` 就会让它顶到链首。现在**顺序与开关表达同一个意图**：
        QMT 在链尾，且默认不参与。
        """
        # 内置候选池的顺序与 `DataParams.intraday_sources` 的默认值保持一致：
        # **qmt 在最后**（终端短期内无法取得行情权限），下一行按开关决定摘掉/保留。
        pool = ["tencent", "sina", "eastmoney", "qmt"]
        if not self._qmt_enabled():
            pool.remove("qmt")
        candidates = [s for s in self._config.data.intraday_sources if s in pool]
        # 配置里没写的源不该被静默丢弃：配置缺项时用内置池兜底补齐
        for source in pool:
            if source not in candidates:
                candidates.append(source)
        prior = candidates or pool
        ranked = self.health.rank(prior, method, prior=prior)
        # 探索式刷新：链路上首选源一旦成功就返回，后面的源拿不到调用 →
        # 证据会一直停在很久以前，因一次异常被降级的源就再也回不来。
        # 这里允许"证据过期最久"的源插到最前刷新一次（默认 60 秒一次）。
        return self.health.explore_refresh(ranked, method)

    def _qmt_enabled(self) -> bool:
        """QMT 是否参与本轮取数（以 provider 自己的配置为准）。

        市场时钟探针走**模块级** `_qmt_source_enabled()`（`live_session_date` 是
        自由函数，拿不到 provider 实例）；两处读的是同一份 `data.qmt_enabled`。
        """
        return bool(self._config.data.qmt_enabled)

    def _session_date(self) -> str:
        """当前活着的交易日，供新鲜度闸门比对；时钟不可用返回 ""（不闸门）。

        独立成一个方法是为了可测：闸门的行为不该取决于跑测试的机器上有没有
        开着 QMT 终端。
        """
        return live_session_date()

    async def _try_source(
        self, source: str, method: str, code: str, period: str, days: int,
        min_date: str = "",
    ) -> tuple[pd.DataFrame | None, SourceAttempt]:
        started = time.perf_counter()
        label = SOURCE_LABELS.get(source, source)
        try:
            if source == "qmt":
                if method == "bars":
                    frame = await self._qmt.fetch_bars(code, period, days)
                else:
                    frame = await self._qmt.fetch_trend(code)
            elif source == "tencent":
                await self._http()
                if method == "bars":
                    frame = await self._tencent.fetch_bars(code, period, days)  # type: ignore[union-attr]
                else:
                    frame = await self._tencent.fetch_trend(code)  # type: ignore[union-attr]
            elif source == "eastmoney":
                if method != "bars":
                    raise DataFetchError("东财源不提供分时明细，跳过")
                frame = await self._eastmoney.fetch_bars(code, period, days)
            elif source == "sina":
                if method == "bars":
                    frame = await self._sina.fetch_bars(code, period, days)
                else:
                    frame = await self._sina.fetch_trend(code)
            else:
                raise DataFetchError(f"未注册的数据源: {source}")
        except Exception as exc:  # noqa: BLE001 数据源失败不影响后续回退
            latency = int((time.perf_counter() - started) * 1000)
            detail = str(exc) or type(exc).__name__
            logger.warning("做T数据源失败 %s(%s %s): %s",
                           label, code, method, detail[:160])
            self._mark_failed(source, method)
            self.health.record_failure(source, method, detail)
            return None, SourceAttempt(
                source=label, ok=False, detail=detail[:200], latency_ms=latency)
        latency = int((time.perf_counter() - started) * 1000)
        latest = series_date(frame)
        if min_date and latest and latest < min_date:
            # 传输成功但**数据是陈旧的**。这必须判失败而不是判成功：分时链路会把
            # 「数据里最新的一天」当当日渲染，于是"昨天的完整分时"会顶着今天的
            # 名头画出来（用户实际遇到的现象）。判失败后链上后面的源（腾讯/新浪
            # 恒有当日数据）会接手，陈旧的那份由调用方作为兜底候选保留。
            detail = (f"{'分时' if method == 'trend' else '分钟K线'}未更新至当前交易日"
                      f"（最新 {latest}，当前交易日 {min_date}）")
            logger.warning("做T数据源数据陈旧 %s(%s %s): %s",
                           label, code, method, detail)
            self._mark_failed(source, method)
            self.health.record_failure(source, method, detail)
            return frame, SourceAttempt(
                source=label, ok=False, rows=len(frame), detail=detail,
                latency_ms=latency)
        self._cooldown.pop(self._cooldown_key(source, method), None)
        self.health.record_success(source, method, latency / 1000.0)
        return frame, SourceAttempt(
            source=label, ok=True, rows=0 if frame is None else len(frame),
            detail=f"period={period}" if method == "bars" else "分时明细",
            latency_ms=latency)

    def _stale_fallback(
        self, stale: list[tuple[str, pd.DataFrame]], attempts: list[SourceAttempt],
        what: str, code: str,
    ) -> tuple[pd.DataFrame, str, list[SourceAttempt]]:
        """所有源都拿不到当日数据时，返回**最新**的那份并显式标注。

        触发场景是「当天本来就没有数据」而不是「取数失败」：停牌、全天一字板
        无成交、或全部源的当日数据整体滞后。这时上一个交易日的完整数据仍然是
        用户想看的（前端会带「非当日」标记），但绝不能静默当成当日。

        刻意**不写缓存**：下次请求会重新走一遍容灾链，QMT 一旦补上当日数据
        就能立刻切回来，而不会被一份陈旧结果堵住 `minute_cache_ttl` 那么久。
        """
        best_date, best_label, best_frame = "", "", None
        for label, frame in stale:
            date = series_date(frame)
            if best_frame is None or date > best_date:
                best_date, best_label, best_frame = date, label, frame
        if best_frame is None:  # 调用方已保证 stale 非空，纯防御
            raise DataFetchError(f"{what}无可用数据({code})")
        session = self._session_date()
        detail = (f"全部数据源均未更新至当前交易日 {session}，"
                  f"回退展示最近交易日 {best_date} 的{what}（非当日）")
        attempts.append(SourceAttempt(
            source=best_label, ok=False, rows=len(best_frame), detail=detail))
        logger.warning("做T%s全部数据源陈旧(%s)：最新仅到 %s（当前交易日 %s）",
                       what, code, best_date, session)
        return best_frame, best_label, attempts

    async def fetch_bars(
        self, code: str, *, period: str | None = None, days: int = 5,
    ) -> tuple[pd.DataFrame, str, list[SourceAttempt]]:
        """分钟K线多源取数：返回 (bars, 命中源名, 尝试日志)。

        全部源失败抛 DataFetchError（由上层记入 health.gaps）。
        """
        target_period = period or self._config.data.intraday_period
        key = f"bars:{code}:{target_period}:{days}"
        cached = self._cache_get(key, self._config.data.minute_cache_ttl)
        if cached is not None:
            return cached[0], cached[1], []
        async with self._lock_for(key):
            # 单飞：等锁期间同键的其他请求可能已把结果写进缓存，进来先复查
            cached = self._cache_get(key, self._config.data.minute_cache_ttl)
            if cached is not None:
                return cached[0], cached[1], []
            return await self._fetch_bars_uncached(code, target_period, days, key)

    async def _fetch_bars_uncached(
        self, code: str, target_period: str, days: int, key: str,
    ) -> tuple[pd.DataFrame, str, list[SourceAttempt]]:
        attempts: list[SourceAttempt] = []
        session = self._session_date()
        stale: list[tuple[str, pd.DataFrame]] = []
        for source in self._ordered_sources("bars"):
            allowed, reason = self.health.should_attempt(source, "bars")
            if not allowed:
                attempts.append(SourceAttempt(
                    source=SOURCE_LABELS.get(source, source), ok=False, rows=0,
                    detail=reason))
                self.health.record_skip(source, "bars", reason)
                continue
            frame, attempt = await self._try_source(
                source, "bars", code, target_period, days, min_date=session)
            attempts.append(attempt)
            if frame is not None and len(frame):
                if not attempt.ok:  # 陈旧：留作兜底候选，继续找有当日数据的源
                    stale.append((SOURCE_LABELS.get(source, source), frame))
                    continue
                chosen = SOURCE_LABELS.get(source, source)
                self._cache_put(key, (frame, chosen))
                return frame, chosen, attempts
        if stale:
            return self._stale_fallback(stale, attempts, "分钟K线", code)
        raise DataFetchError(
            f"分钟K线全部数据源失败({code} {target_period}): "
            + " | ".join(f"{a.source}:{a.detail}" for a in attempts))

    async def fetch_trend(
        self, code: str,
    ) -> tuple[pd.DataFrame, str, list[SourceAttempt]]:
        """当日分时多源取数（per-key 单飞，避免并发重复下载）。"""
        key = f"trend:{code}"
        cached = self._cache_get(key, self._config.data.minute_cache_ttl)
        if cached is not None:
            return cached[0], cached[1], []
        async with self._lock_for(key):
            cached = self._cache_get(key, self._config.data.minute_cache_ttl)
            if cached is not None:
                return cached[0], cached[1], []
            return await self._fetch_trend_uncached(code, key)

    async def _fetch_trend_uncached(
        self, code: str, key: str,
    ) -> tuple[pd.DataFrame, str, list[SourceAttempt]]:
        attempts: list[SourceAttempt] = []
        session = self._session_date()
        stale: list[tuple[str, pd.DataFrame]] = []
        for source in self._ordered_sources("trend"):
            if source == "eastmoney":
                continue  # 东财分钟接口不提供当日分时明细
            allowed, reason = self.health.should_attempt(source, "trend")
            if not allowed:
                attempts.append(SourceAttempt(
                    source=SOURCE_LABELS.get(source, source), ok=False, rows=0,
                    detail=reason))
                self.health.record_skip(source, "trend", reason)
                continue
            frame, attempt = await self._try_source(
                source, "trend", code, "1m", 1, min_date=session)
            attempts.append(attempt)
            if frame is not None and len(frame):
                if not attempt.ok:  # 陈旧：留作兜底候选，继续找有当日数据的源
                    stale.append((SOURCE_LABELS.get(source, source), frame))
                    continue
                chosen = SOURCE_LABELS.get(source, source)
                self._cache_put(key, (frame, chosen))
                return frame, chosen, attempts
        if stale:
            return self._stale_fallback(stale, attempts, "分时", code)
        raise DataFetchError(
            f"分时数据全部数据源失败({code}): "
            + " | ".join(f"{a.source}:{a.detail}" for a in attempts))

    async def fetch_quotes(self, codes: list[str]) -> dict[str, Quote]:
        """**批量**实时快照（自选池「报价快车道」专用）：一次调用取全部标的。

        与 `fetch_quote()`（单只、带容灾链与尝试日志）的区别是**调用次数**：
        报价快车道每几秒跑一轮，若按标的逐个调，9 只票就是 9 次锁 + 9 次公网 RTT；
        这里两级都是"一次调用取全部"：

            ① QMT `get_full_tick([9只])` 实测中位 **0.54ms**（本机终端内存读取）
               —— 仅在 `data.qmt_enabled=true` 时参与，见下方 `_qmt_enabled()` 分支
            ② 腾讯 qt 批量（一次请求 9 个代码）约 **54ms**（公网，实测 4 只 52ms）

        QMT 没给到的代码（未订阅/停牌/终端未登录）再走腾讯批量补缺，仍然缺的
        才逐只走完整容灾链。返回 `{6位代码: Quote}`；取不到的代码**不出现**在结果里
        （调用方保留上一次的值，而不是把价格清空 —— 清空会让列表闪成"—"）。
        """
        wanted = [str(code).strip() for code in codes if str(code).strip()]
        if not wanted:
            return {}
        result: dict[str, Quote] = {}

        allowed, reason = self.health.should_attempt("qmt", "quote")
        # `qmt_enabled=false` 时必须在这里就断掉：批量快照原来**硬编码**先打 QMT，
        # 不看配置也不看 `_ordered_sources` —— 于是关掉 QMT 后每轮报价快车道
        # 仍会先撞一次失败的 xtquant 连接（实测 4~5s），还在健康表里留一条红色记录。
        if not self._qmt_enabled():
            allowed, reason = False, "QMT 已禁用（data.qmt_enabled=false）"
        if allowed:
            started = time.perf_counter()
            try:
                quotes = await asyncio.to_thread(self._qmt_quotes_sync, wanted)
                self.health.record_success(
                    "qmt", "quote", time.perf_counter() - started)
                result.update(quotes)
            except Exception as exc:  # noqa: BLE001 批量失败不影响腾讯补缺
                detail = str(exc) or type(exc).__name__
                logger.warning("QMT批量快照失败：%s", detail[:160])
                self.health.record_failure("qmt", "quote", detail)
        else:
            logger.debug("QMT批量快照跳过：%s", reason)

        missing = [code for code in wanted if code not in result]
        if missing:
            started = time.perf_counter()
            try:
                await self._http()
                quotes = await self._tencent.fetch_quotes_batch(missing)  # type: ignore[union-attr]
                self.health.record_success(
                    "tencent", "quote", time.perf_counter() - started)
                result.update(quotes)
            except Exception as exc:  # noqa: BLE001
                detail = str(exc) or type(exc).__name__
                logger.warning("腾讯批量快照失败(%d只)：%s", len(missing), detail[:160])
                self.health.record_failure("tencent", "quote", detail)

        for code in [c for c in wanted if c not in result]:
            try:
                quote, _, _ = await self.fetch_quote(code)
                result[code] = quote
            except Exception as exc:  # noqa: BLE001 单只兜底失败就跳过
                logger.debug("单只快照兜底失败(%s)：%s", code, brief(exc, BRIEF_TIGHT))
        return result

    async def fetch_quote(self, code: str) -> tuple[Quote, str, list[SourceAttempt]]:
        """实时快照：**按实测速度排序**的多源容灾链（腾讯 / QMT 互为备用）。

        ## 2026-09 变更：候选池改成配置驱动，QMT 排在腾讯**之后**
        原实现写死 `["qmt", "tencent"]` 并让 QMT 排第一，理由是它读本机终端内存
        （实测中位 0ms）而腾讯要付公网 RTT（中位 54ms）。这个结论在 QMT
        **有行情权限**时成立；本机失去权限后，QMT 每一次都失败并白等 4~5s 超时，
        排序反而把最慢的放在最前。现在候选池为
        `["tencent"] + (["qmt"] 仅当 data.qmt_enabled)` —— 腾讯在前、QMT 在末，
        与 `_ordered_sources` 同一口径。将来权限恢复时打开开关即可多一层兜底。

        PE/PB 仍由估值面板（`ValuationProvider`，走百度/腾讯批量/巨潮）单独取，
        不依赖这条快照链。
        """
        key = f"quote:{code}"
        cached = self._cache_get(key, 15)
        if cached is not None:
            return cached[0], cached[1], []
        attempts: list[SourceAttempt] = []
        pool = ["tencent"] + (["qmt"] if self._qmt_enabled() else [])
        ranked = self.health.rank(pool, "quote", prior=pool)
        order = self.health.explore_refresh(ranked, "quote")
        for source in order:
            allowed, reason = self.health.should_attempt(source, "quote")
            if not allowed:
                attempts.append(SourceAttempt(
                    source=SOURCE_LABELS[source], ok=False, rows=0, detail=reason))
                self.health.record_skip(source, "quote", reason)
                continue
            started = time.perf_counter()
            try:
                if source == "qmt":
                    quote = await asyncio.to_thread(self._qmt_quote_sync, code)
                    detail = "QMT快照（五档盘口）"
                else:
                    await self._http()
                    quote = await self._tencent.fetch_quote(code)  # type: ignore[union-attr]
                    detail = "实时快照（含PE/PB）"
            except Exception as exc:  # noqa: BLE001 单源失败继续回退
                elapsed = time.perf_counter() - started
                message = str(exc) or type(exc).__name__
                self._mark_failed(source, "quote")
                self.health.record_failure(source, "quote", message)
                attempts.append(SourceAttempt(
                    source=SOURCE_LABELS[source], ok=False, detail=message[:200],
                    latency_ms=int(elapsed * 1000)))
                continue
            elapsed = time.perf_counter() - started
            self._cooldown.pop(self._cooldown_key(source, "quote"), None)
            self.health.record_success(source, "quote", elapsed)
            attempts.append(SourceAttempt(
                source=SOURCE_LABELS[source], ok=True, rows=1, detail=detail,
                latency_ms=int(elapsed * 1000)))
            self._cache_put(key, (quote, SOURCE_LABELS[source]))
            return quote, SOURCE_LABELS[source], attempts
        raise DataFetchError(
            "实时快照全部数据源失败: "
            + " | ".join(f"{a.source}:{a.detail}" for a in attempts))

    async def fetch_index_quote(
        self, index_code: str,
    ) -> tuple[Quote, str, list[SourceAttempt]]:
        """大盘指数快照（腾讯；失败返回缺口而不上抛，情绪面板可降级展示）。"""
        started = time.perf_counter()
        try:
            await self._http()
            quote = await self._tencent.fetch_index_quote(index_code)  # type: ignore[union-attr]
            return quote, SOURCE_LABELS["tencent"], [SourceAttempt(
                source=SOURCE_LABELS["tencent"], ok=True, rows=1, detail="指数快照",
                latency_ms=int((time.perf_counter() - started) * 1000))]
        except Exception as exc:  # noqa: BLE001
            raise DataFetchError(f"指数快照失败({index_code}): {exc}") from exc

    async def fetch_peer_quotes(
        self, codes: list[str],
    ) -> tuple[dict[str, Quote], str, list[SourceAttempt]]:
        """同业批量快照（腾讯一次请求取回，含PE/PB）。"""
        if not codes:
            return {}, "", []
        started = time.perf_counter()
        try:
            await self._http()
            quotes = await self._tencent.fetch_quotes_batch(codes)  # type: ignore[union-attr]
            return quotes, SOURCE_LABELS["tencent"], [SourceAttempt(
                source=SOURCE_LABELS["tencent"], ok=True, rows=len(quotes),
                detail=f"同业批量快照 {len(codes)} 只",
                latency_ms=int((time.perf_counter() - started) * 1000))]
        except Exception as exc:  # noqa: BLE001
            raise DataFetchError(f"同业批量快照失败: {exc}") from exc

    async def fetch_overseas(
        self, symbols: list[str],
    ) -> tuple[list[Any], str, list[SourceAttempt]]:
        """海外映射行情（美股/韩股，腾讯一次请求取回）。"""
        if not symbols:
            return [], "", []
        started = time.perf_counter()
        try:
            await self._http()
            quotes = await self._tencent.fetch_overseas_quotes(symbols)  # type: ignore[union-attr]
            return quotes, SOURCE_LABELS["tencent"], [SourceAttempt(
                source=SOURCE_LABELS["tencent"], ok=True, rows=len(quotes),
                detail=f"海外映射快照 {len(symbols)} 只",
                latency_ms=int((time.perf_counter() - started) * 1000))]
        except Exception as exc:  # noqa: BLE001
            raise DataFetchError(f"海外映射快照失败: {exc}") from exc

    async def fetch_index_volume(
        self, index_code: str,
    ) -> tuple[Any, str, list[SourceAttempt]]:
        """指数量能：今日量（快照）+ 昨日量（日线）→ 全天预测量与量能比。

        「预测量能」= 当前累计量 / 已交易时间占比；收盘后即为当日实际量。
        """
        from src.intraday.market import elapsed_session_ratio

        started = time.perf_counter()
        attempts: list[SourceAttempt] = []
        try:
            await self._http()
            quote = await self._tencent.fetch_index_quote(index_code)  # type: ignore[union-attr]
            attempts.append(SourceAttempt(
                source=SOURCE_LABELS["tencent"], ok=True, rows=1,
                detail="指数快照（含成交额）",
                latency_ms=int((time.perf_counter() - started) * 1000)))
        except Exception as exc:  # noqa: BLE001
            attempts.append(SourceAttempt(
                source=SOURCE_LABELS["tencent"], ok=False,
                detail=f"指数快照失败: {brief(exc, BRIEF_TIGHT)}"))
            raise DataFetchError(f"指数量能取数失败({index_code}): {exc}") from exc
        yesterday_volume = None
        try:
            kline = await self._tencent.fetch_index_daily_kline(index_code, 5)  # type: ignore[union-attr]
            # 最后一行为当日；倒数第二行为上一交易日
            if len(kline) >= 2:
                yesterday_volume = _number(kline["volume"].iloc[-2])
        except Exception as exc:  # noqa: BLE001 日线缺失只影响量能比，不影响方向分
            logger.warning("指数日线取数失败(%s): %s", index_code, brief(exc, BRIEF_TIGHT))
            attempts.append(SourceAttempt(
                source=SOURCE_LABELS["tencent"], ok=False,
                detail=f"指数日线失败（量能比缺失）: {brief(exc, BRIEF_TIGHT)}"))
        ratio = elapsed_session_ratio()
        current_volume = quote.volume
        projected = None
        if current_volume is not None and ratio > 0:
            projected = current_volume / ratio
        volume_ratio = None
        if projected is not None and yesterday_volume:
            volume_ratio = projected / yesterday_volume
        from src.intraday.market import IndexVolume, score_index_volume

        # 结论文案（放量上涨/缩量下跌…）在快照里也要带上：前端顶栏直接展示，
        # 不让用户只看到「0.96 倍」却不知道这算放量还是缩量。
        _, verdict = score_index_volume(
            change_pct=quote.change_pct, volume_ratio=volume_ratio)

        return IndexVolume(
            code=index_code, name=quote.name, available=True,
            change_pct=quote.change_pct, volume=current_volume,
            amount=quote.amount, yesterday_volume=yesterday_volume,
            projected_volume=projected, volume_ratio=volume_ratio,
            elapsed_ratio=ratio, verdict=verdict,
            source_name=SOURCE_LABELS["tencent"]), \
            SOURCE_LABELS["tencent"], attempts

    def _qmt_quote_sync(self, code: str) -> Quote:
        from src.core.qmt_guard import qmt_lock

        qmt_code = exchange_symbol(code, style="qmt")
        xtdata = self._qmt._client()  # noqa: SLF001 同模块内部复用懒加载客户端
        try:
            # 快照也在锁内：xtquant 的 tick 查询同样不是线程安全的原生接口
            with qmt_lock():
                ticks = xtdata.get_full_tick([qmt_code]) or {}
            tick = ticks.get(qmt_code) or {}
        except Exception as exc:  # noqa: BLE001
            raise DataFetchError(f"QMT快照失败({qmt_code}): {exc}") from exc
        quote = quote_from_qmt_tick(code, tick)
        if quote is None:
            raise DataFetchError(f"QMT快照价格无效({qmt_code})")
        return quote

    def _qmt_quotes_sync(self, codes: list[str]) -> dict[str, Quote]:
        """QMT **批量**快照：一次 `get_full_tick` 取全部标的。

        实测（2026-09-16 11:44，本机终端）：9 只一次调用中位 **0.54ms**、
        单只 0.15ms —— 批量与单只几乎等价，但批量只占一次 QMT 锁、只走一次
        原生调用，所以报价快车道必须用批量而不是循环调单只。
        """
        from src.core.qmt_guard import qmt_lock

        # 保持"请求顺序 → 返回 dict"的可追溯性：QMT 用带市场后缀的代码
        mapping = {exchange_symbol(c, style="qmt"): c for c in codes}
        xtdata = self._qmt._client()  # noqa: SLF001
        try:
            with qmt_lock():
                ticks = xtdata.get_full_tick(list(mapping)) or {}
        except Exception as exc:  # noqa: BLE001
            raise DataFetchError(f"QMT批量快照失败({len(codes)}只): {exc}") from exc
        result: dict[str, Quote] = {}
        for qmt_code, code in mapping.items():
            quote = quote_from_qmt_tick(code, ticks.get(qmt_code) or {})
            if quote is not None:
                result[code] = quote
        if not result:
            raise DataFetchError(f"QMT批量快照无有效数据({len(codes)}只)")
        return result


# ==================================================================
# 日线上下文（复用项目既有采集链）
# ==================================================================

_DAILY_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "open": ("open", "开盘", "开盘价"),
    "high": ("high", "最高", "最高价"),
    "low": ("low", "最低", "最低价"),
    "close": ("close", "收盘", "收盘价"),
    "volume": ("volume", "成交量", "vol"),
    "amount": ("amount", "成交额", "成交金额"),
}


def daily_bars_from_points(points: list[Any]) -> pd.DataFrame:
    """项目采集链的 DataPoint 列表 → 日线OHLCV表。

    兼容两种 extra 口径（同一份数据在不同连接器下字段名不同）：
      - QMT/本地CSV：{"open","high","low","close","volume","amount"}
      - AkShare：中文列名 {"开盘","最高","最低","收盘","成交量","成交额"}
    value 字段兜底作为收盘价。
    """
    if not points:
        return _empty_bars()
    rows = []
    for point in points:
        extra = getattr(point, "extra", None) or {}
        period = getattr(point, "period_date", None)
        if not period:
            continue
        record: dict[str, Any] = {"ts": str(period)}
        for name, aliases in _DAILY_COLUMN_ALIASES.items():
            value = None
            for alias in aliases:
                if alias in extra:
                    value = safe_float(extra.get(alias))
                    if value is not None:
                        break
            record[name] = value
        if record["close"] is None:
            record["close"] = safe_float(getattr(point, "value", None))
        if record["close"] is None:
            continue
        for name in ("open", "high", "low"):
            if record[name] is None:
                record[name] = record["close"]
        record["volume"] = record["volume"] or 0.0
        record["amount"] = record["amount"] or 0.0
        rows.append(record)
    return _normalize_bars(pd.DataFrame(rows))
