"""主线挖掘：本地数据仓（**回测与实盘共用同一份输入**）。

## 为什么必须有这一层

第一层六维 + 第二层三维 + 第三层龙头，一轮打分要读：板块指数（300 个板块 ×
400 日）、板块资金流、两融、北向持股、龙虎榜、宏观、财务。回测（2022-01 至今，
约 1150 个交易日）如果每次都现拉：

- **Tushare 会被打爆**：`margin_detail` 单日一次调用（6000 行上限），1150 天
  就是 1150 次；加上龙虎榜、板块指数分块，一轮回测上万次请求；
- **回测不可复现**：同一个日期区间两次跑出来不一样（数据源在更新），
  而"回测结果变了"必须能归因到模型改动，不是数据漂移；
- **实盘与回测口径会漂**：实盘读当天接口、回测读历史接口，两者字段口径
  不一致时，回测的高胜率在实盘上完全兑现不了。

因此本模块自建一个**独立的** SQLite 数据仓（默认 `data/mainline_cache.db`），
一次性把历史灌进去，之后打分与回测都只读它。

## 为什么独立于 `data/quant/warehouse.db`

那个 15 GiB 库是**只读行情仓**（2006 至今全市场日线/资金流/市值），备份与
清理策略和本模块完全不同。混在一起就会出现"清主线缓存顺手删掉 15 GiB 行情、
要重新下载一遍"。本模块只**只读**它，绝不写入。

## 两张库的分工

    warehouse.db（15 GiB，只读） 个股级日线 / 资金流 / 流通市值 / 财务指标
                                 → 板块级资金流由**成分股聚合**得到，
                                   覆盖 2010 至今，是回测唯一可行的口径
    mainline_cache.db（本层）    板块指数 / 板块资金流 / 两融 / 北向持股 /
                                 龙虎榜 / 宏观 / 期货 —— 即"Tushare 才有的东西"
                                 → 从此不再重复下载

## 三条硬规则

1. **口径统一**：板块资金流固定用「本地行情仓个股聚合（Tushare 个股口径）」，
   东财板块口径（`moneyflow_ind_dc`）只作展示与交叉校验 —— 需求 8.4 明确
   禁止混用。实测东财口径 `moneyflow_ind_dc` 只有 2024 年起的数据（概念板块
   更要到 2026 年），根本无法覆盖 2022 起的回测区间，这也是本层存在的直接原因。
2. **增量同步、幂等落库**：全部 `INSERT ... ON CONFLICT DO UPDATE`。
   一个交易日必然被重复同步（手动刷新、断点续跑、复盘重算），追加式写入
   会把这些日子变成 N 份重复行，而"今天主线是谁"的榜单一旦有重复板块就不可信。
3. **同步失败要留痕**：缺失的日期写进 `ml_sync` 台账并在返回值里报出来。
   "这批数据没同步上"与"那几天真的没数据"在打分侧长得一样，必须能区分。

## 交易日历

所有"缺哪些天"的判断都以**交易日历**为准（Tushare `trade_cal`，落库缓存），
不用自然日 —— 用自然日会把周末与节假日当成"缺失数据"，
然后对每个周末发一次注定拿不到数据的请求。
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from src.core.errors import BRIEF_DEFAULT, BRIEF_TIGHT, brief
from src.mainline.config import (
    MainlineConfig,
    load_config,
    load_sector_blacklist,
    load_theme_exclusions,
)
from src.mainline.models import (
    BoardBar,
    BoardFlow,
    BoardInfo,
    BoardKind,
    BoardSeries,
    FutureKind,
    HolderRow,
    SeatRow,
)

logger = logging.getLogger(__name__)

#: ⚠️ **本地行情仓的金额字段已经是「元」**（2026-09 逐票比对 Tushare 原始值确认）：
#: `quant_moneyflow.*_amount` = 原始万元 × 1e4、`quant_daily.amount` = 原始千元 × 1e3、
#: `quant_daily_basic.circ_mv/total_mv` = 原始万元 × 1e4。
#: **读这几个表时一个乘数都不能再加。** 早先按 Tushare 原始口径又乘了一次，
#: 板块资金流被放大了 1000~10000 倍 —— 而分数是横截面分位，整体放大后
#: 排序不变、界面上看不出任何异常，只有绝对金额（"净流入 24 万亿"）荒谬到扎眼。
_WAN = 1.0e4

#: 板块指数「当日数据」的**校验 + 补取**参数（见 `sync_board_bars` 的 2b 段）。
#:
#: 上游的申万/同花顺概念指数是**逐板块陆续发布**当天数据的：2026-09-23 实测
#: 19:07 那次同步只有 66/138 个板块带上当天，19:19 重跑就 138/138 了。
#: 而"返回非空 = 成功"的判断对这种缺口完全无感 —— 所以必须按当天日期校验并重试。
_BAR_DATE_RETRY_ROUNDS = 3
_BAR_DATE_RETRY_SLEEP = 20.0

#: 数据集名（`ml_sync` 台账的键，也是 `sync()` 的入参）
DATASETS = (
    "board",        # 板块目录
    "member",       # 成分股
    "board_bar",    # 板块指数日线
    "board_flow",   # 板块资金流（成分股聚合）
    "margin",       # 两融
    "northbound",   # 北向持股
    "holder",       # 股东户数
    "seat",         # 龙虎榜席位
    "macro",        # 宏观
    "future",       # 期货
    "future_meta",  # 期货品种名录
    "calendar",     # 交易日历
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ml_board (
    code TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT '', members INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT '', list_date TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS ml_member (
    board_code TEXT NOT NULL, code TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '', in_date TEXT NOT NULL DEFAULT '',
    out_date TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (board_code, code)
);
CREATE INDEX IF NOT EXISTS idx_ml_member_code ON ml_member(code);
-- 个股静态信息（目前只有**上市日期**，回测防前视偏差用）。
--
-- 为什么单独建表而不是给 ml_member 加一列：ml_member 每次同步会被
-- Tushare 的概念成分覆盖（`ON CONFLICT ... DO UPDATE`），静态信息混进去
-- 会在下一次同步时被清掉；而且上市日期是**个股属性**不是"板块-个股"关系，
-- 放在成员表里等于对同一只股票重复存几千遍。
CREATE TABLE IF NOT EXISTS ml_stock_meta (
    code TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
    list_date TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_ml_stock_meta_list ON ml_stock_meta(list_date);
CREATE TABLE IF NOT EXISTS ml_board_bar (
    board_code TEXT NOT NULL, trade_date TEXT NOT NULL,
    open REAL NOT NULL DEFAULT 0, high REAL NOT NULL DEFAULT 0,
    low REAL NOT NULL DEFAULT 0, close REAL NOT NULL DEFAULT 0,
    pre_close REAL NOT NULL DEFAULT 0, volume REAL NOT NULL DEFAULT 0,
    amount REAL NOT NULL DEFAULT 0, pct_change REAL NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (board_code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_ml_board_bar_date ON ml_board_bar(trade_date);
CREATE TABLE IF NOT EXISTS ml_board_flow (
    board_code TEXT NOT NULL, trade_date TEXT NOT NULL,
    net_amount REAL NOT NULL DEFAULT 0, net_rate REAL,
    elg_amount REAL, lg_amount REAL, sm_amount REAL,
    circ_mv REAL, amount REAL,
    source TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (board_code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_ml_board_flow_date ON ml_board_flow(trade_date);
CREATE TABLE IF NOT EXISTS ml_margin (
    code TEXT NOT NULL, trade_date TEXT NOT NULL,
    rzye REAL NOT NULL DEFAULT 0, rqye REAL NOT NULL DEFAULT 0,
    net_buy REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_ml_margin_date ON ml_margin(trade_date);
CREATE TABLE IF NOT EXISTS ml_northbound (
    code TEXT NOT NULL, trade_date TEXT NOT NULL,
    vol REAL NOT NULL DEFAULT 0, ratio REAL,
    source TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_ml_northbound_date ON ml_northbound(trade_date);
CREATE TABLE IF NOT EXISTS ml_holder (
    code TEXT NOT NULL, end_date TEXT NOT NULL,
    ann_date TEXT NOT NULL DEFAULT '', holder_num REAL,
    PRIMARY KEY (code, end_date)
);
CREATE TABLE IF NOT EXISTS ml_seat (
    trade_date TEXT NOT NULL, code TEXT NOT NULL,
    exalter TEXT NOT NULL DEFAULT '', side TEXT NOT NULL DEFAULT '',
    buy REAL NOT NULL DEFAULT 0, sell REAL NOT NULL DEFAULT 0,
    net_buy REAL NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (trade_date, code, exalter, side)
);
CREATE INDEX IF NOT EXISTS idx_ml_seat_date ON ml_seat(trade_date);
CREATE TABLE IF NOT EXISTS ml_macro (
    factor TEXT NOT NULL, period TEXT NOT NULL, value REAL,
    updated_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (factor, period)
);
CREATE TABLE IF NOT EXISTS ml_future (
    code TEXT NOT NULL, trade_date TEXT NOT NULL,
    open REAL NOT NULL DEFAULT 0, high REAL NOT NULL DEFAULT 0,
    low REAL NOT NULL DEFAULT 0, close REAL NOT NULL DEFAULT 0,
    settle REAL NOT NULL DEFAULT 0, pre_close REAL NOT NULL DEFAULT 0,
    volume REAL NOT NULL DEFAULT 0, amount REAL NOT NULL DEFAULT 0,
    oi REAL NOT NULL DEFAULT 0, oi_chg REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (code, trade_date)
);
CREATE TABLE IF NOT EXISTS ml_future_meta (
    code TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
    fut_code TEXT NOT NULL DEFAULT '', exchange TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'domestic', source TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
-- 全合约曲线（近月 / 远月）：期限结构信号唯一的数据来源。
-- 即使没有数据也要建表：不建的话每次取数都会抛 "no such table" 并写一条
-- 警告日志 —— 89 个品种轮一遍就是 89 条噪声，把真正的告警淹没。
CREATE TABLE IF NOT EXISTS ml_future_curve (
    code TEXT NOT NULL, trade_date TEXT NOT NULL,
    near REAL, far REAL, near_code TEXT NOT NULL DEFAULT '',
    far_code TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (code, trade_date)
);
CREATE TABLE IF NOT EXISTS ml_calendar (
    trade_date TEXT PRIMARY KEY, is_open INTEGER NOT NULL DEFAULT 1
);
-- ETF 日线 + 份额（第二层 etf 维度的数据源）
--
-- `shares` 来自 `fund_share.fd_share`（万份）。**`etf_share_size` 在 5000 积分档
-- 无权限**（实测），`fund_share` 是等效替代：都是"截至该日的基金份额"。
-- 份额是**存量**不是流量，因此"净申购"必须用当日与上一交易日之差，不能与
-- 同日其它字段混算。
CREATE TABLE IF NOT EXISTS ml_etf (
    code TEXT NOT NULL, trade_date TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    close REAL NOT NULL DEFAULT 0, pct_chg REAL NOT NULL DEFAULT 0,
    volume REAL NOT NULL DEFAULT 0, amount REAL NOT NULL DEFAULT 0,
    shares REAL,
    PRIMARY KEY (code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_ml_etf_date ON ml_etf(trade_date);
CREATE TABLE IF NOT EXISTS ml_etf_meta (
    code TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
    benchmark TEXT NOT NULL DEFAULT '', board_name TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
-- 宽基指数日线（ETF 份额监控算"指数位置分位"用）
CREATE TABLE IF NOT EXISTS ml_index (
    code TEXT NOT NULL, trade_date TEXT NOT NULL,
    close REAL NOT NULL DEFAULT 0, pct_chg REAL NOT NULL DEFAULT 0,
    amount REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_ml_index_date ON ml_index(trade_date);
CREATE TABLE IF NOT EXISTS ml_sync (
    dataset TEXT NOT NULL, span_start TEXT NOT NULL DEFAULT '',
    span_end TEXT NOT NULL DEFAULT '', rows INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT '', message TEXT NOT NULL DEFAULT '',
    seconds REAL NOT NULL DEFAULT 0, updated_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (dataset, span_start, span_end)
);
"""


def _same_theme(board_name: str, theme: str) -> bool:
    """板块名与 LLM 题材名是否指同一个题材（归一化后互相包含）。

    两边的写法本来就不统一：板块叫「氟化工**概念**」，`ml_stock_theme` 里
    同时存在「氟化工」与「氟化工概念」；还有「车联网(车路协同)」这类带括号的。
    所以去掉「概念/板块/指数/产业/行业」后缀、去掉括号、再判互相包含 ——
    **宁可宽松**：这里只影响一列展示信息，判宽了最多多显示一个业务分；
    判严了会把「昊华科技 氟化工 95 分」漏掉、显示成"无业务判定"，
    那正是这次要修的误导。
    """
    left = re.sub(r"[（(].*?[)）]", "", str(board_name or "")).strip()
    left = re.sub(r"(概念|板块|指数|产业|行业)$", "", left).strip()
    right = re.sub(r"[（(].*?[)）]", "", str(theme or "")).strip()
    right = re.sub(r"(概念|板块|指数|产业|行业)$", "", right).strip()
    if not left or not right:
        return False
    return left in right or right in left


def _year_spans(start: str, end: str, chunk_years: float) -> list[tuple[str, str]]:
    """把 `[start, end]` 按 `chunk_years` 年切片（用于绕过接口的行数上限）。

    `chunk_years <= 0` 时返回整段。切片的必要性见 `sync_etf` 的说明：
    `fund_share` 单次上限 2000 行，一次拉 8 年会把最早那段静默丢掉。
    """
    if chunk_years <= 0:
        return [(start, end)]
    try:
        begin = datetime.strptime(start, "%Y%m%d")
        finish = datetime.strptime(end or datetime.now().strftime("%Y%m%d"),
                                   "%Y%m%d")
    except (TypeError, ValueError):
        return [(start, end)]
    spans: list[tuple[str, str]] = []
    cursor = begin
    while cursor < finish:
        nxt = cursor + timedelta(days=int(max(chunk_years, 0.25) * 365.25))
        nxt = min(nxt, finish)
        spans.append((cursor.strftime("%Y%m%d"), nxt.strftime("%Y%m%d")))
        cursor = nxt + timedelta(days=1)
    return spans or [(start, end)]


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        result = float(value)
        return default if result != result else result
    except (TypeError, ValueError):
        return default


def _opt(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        result = float(value)
        return None if result != result else result
    except (TypeError, ValueError):
        return None


def _s(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _shift_days(stamp: str, days: int) -> str:
    """`YYYYMMDD` 平移若干天（解析失败返回空串，由调用方按"无公告日"处理）。

    用途：`quant_fina_indicator` 里 `ann_date` 偶尔缺失，此时用
    `end_date + 90 天` 作为**保守**的披露日估计 —— 宁可晚用（少赚一点信息），
    绝不能用早（那就是前视偏差，回测胜率会凭空虚高）。
    """
    text = _s(stamp)
    if len(text) != 8 or not text.isdigit():
        return ""
    try:
        from datetime import timedelta

        base = datetime.strptime(text, "%Y%m%d")
    except ValueError:
        return ""
    return (base + timedelta(days=int(days))).strftime("%Y%m%d")


# ==================================================================
# 同步台账
# ==================================================================


@dataclass
class SyncResult:
    """一次同步的结果（每个数据集一条）。"""

    dataset: str
    rows: int = 0
    seconds: float = 0.0
    status: str = "ok"          # ok / partial / skipped / failed
    missing: list[str] = field(default_factory=list)
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"dataset": self.dataset, "rows": self.rows,
                "seconds": round(self.seconds, 2), "status": self.status,
                "missing": self.missing[:30], "missing_count": len(self.missing),
                "message": self.message}

    @property
    def note(self) -> str:
        """一句话说明（供调度日志与接口展示）。

        ⚠️ `status == "ok"` 时**也必须带上 `message`**：像"清理了 N 个已不在
        名录里的板块"这类说明只在 message 里，早期写法在 ok 分支直接 return，
        把这类信息丢掉了 —— 结果是"同步成功、但目录悄悄变了"没人知道。
        """
        tail = f"（缺 {len(self.missing)} 项）" if self.missing else ""
        parts = [f"{self.dataset}：{self.rows} 行"]
        if self.status != "ok":
            parts.append(self.status)
        if tail:
            parts.append(tail)
        if self.message:
            parts.append(self.message)
        return "　".join(part for part in parts if part)


# ==================================================================
# 数据仓
# ==================================================================


@dataclass
class MainlineDataStore:
    """主线挖掘本地数据仓（同步 sqlite3；调用方用 `asyncio.to_thread` 包）。

    `remote` 是 `sources.DataSources`（Tushare / 本地行情仓 / 东财），
    注入式传入以便测试整体替换 —— 单测里绝不应该发真实网络请求。
    """

    config: MainlineConfig = field(default_factory=load_config)
    remote: Any = None
    _ready: bool = field(default=False, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _calendar: list[str] = field(default_factory=list, init=False)

    # ---------- 构造与建表 ----------

    def __post_init__(self) -> None:
        if self.remote is None:
            from src.mainline.sources import DataSources

            self.remote = DataSources(config=self.config)
        self.path = str(self.config.cache_file)

    @property
    def warehouse(self) -> Any:
        return getattr(self.remote, "warehouse", None)

    @property
    def tushare(self) -> Any:
        return getattr(self.remote, "tushare", None)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:  # 只读介质等场景：不加 PRAGMA 也能用
            pass
        return connection

    def ensure_schema(self) -> None:
        """建表（幂等；同步方法，可在任何线程调用）。"""
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            parent = os.path.dirname(os.path.abspath(self.path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            with self._connect() as connection:
                connection.executescript(_SCHEMA)
            self._ready = True

    def _write(self, sql: str, rows: Sequence[tuple]) -> int:
        if not rows:
            return 0
        self.ensure_schema()
        with self._lock, self._connect() as connection:
            connection.executemany(sql, rows)
        return len(rows)

    def _read(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        """只读查询；任何 SQLite 错误（含**建表本身失败**）都降级为空列表。

        ⚠️ `ensure_schema()` 必须在 `try` **里面**：库文件被换成非 SQLite
        内容（磁盘损坏、手工改错文件）时，抛错的是建表而不是查询 ——
        把它放在 try 外面，一次库损坏就会让整个面板 500，
        而本模块对读取的约定是"降级为空 + 记一条警告日志"。
        """
        try:
            self.ensure_schema()
            with self._connect() as connection:
                return connection.execute(sql, tuple(params)).fetchall()
        except sqlite3.Error as exc:
            logger.warning("主线数据仓读取失败：%s", brief(exc, BRIEF_TIGHT))
            return []

    def _log_sync(self, result: SyncResult, span: tuple[str, str]) -> None:
        try:
            self._write(
                "INSERT INTO ml_sync(dataset, span_start, span_end, rows, status,"
                " message, seconds, updated_at) VALUES(?,?,?,?,?,?,?,?)"
                " ON CONFLICT(dataset, span_start, span_end) DO UPDATE SET"
                " rows=excluded.rows, status=excluded.status,"
                " message=excluded.message, seconds=excluded.seconds,"
                " updated_at=excluded.updated_at",
                [(result.dataset, span[0], span[1], result.rows, result.status,
                  result.message[:400], result.seconds, _now())])
        except sqlite3.Error as exc:
            logger.warning("主线数据仓台账写入失败：%s", brief(exc, BRIEF_TIGHT))

    def sync_status(self) -> list[dict[str, Any]]:
        """读同步台账（每数据集最近一次），供接口展示数据新鲜度。"""
        rows = self._read(
            "SELECT dataset, span_start, span_end, rows, status, message,"
            " seconds, updated_at FROM ml_sync"
            " ORDER BY updated_at DESC")
        seen: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = str(row["dataset"])
            if key not in seen:
                seen[key] = {k: row[k] for k in row.keys()}
        return list(seen.values())

    def stats(self) -> dict[str, int]:
        """各表行数（面板上展示"本地仓里有多少数据"）。"""
        out: dict[str, int] = {}
        for table in ("ml_board", "ml_member", "ml_board_bar", "ml_board_flow",
                      "ml_margin", "ml_northbound", "ml_holder", "ml_seat",
                      "ml_macro", "ml_future", "ml_calendar"):
            rows = self._read(f"SELECT COUNT(*) AS n FROM {table}")
            out[table] = int(rows[0]["n"]) if rows else 0
        return out

    # ---------- 交易日历 ----------

    def calendar(self, start: str = "", end: str = "") -> list[str]:
        """交易日列表（升序）。本地为空时向 Tushare 取一次并落库。"""
        if not self._calendar:
            rows = self._read("SELECT trade_date FROM ml_calendar"
                              " WHERE is_open = 1 ORDER BY trade_date")
            self._calendar = [str(row["trade_date"]) for row in rows]
        if not self._calendar and self.tushare is not None:
            self.sync_calendar(start or "20150101",
                               end or datetime.today().strftime("%Y%m%d"))
        days = self._calendar
        if start:
            days = [day for day in days if day >= start]
        if end:
            days = [day for day in days if day <= end]
        return days

    def sync_calendar(self, start: str, end: str) -> SyncResult:
        """同步交易日历（一次拿全区间，落库缓存）。"""
        started = time.monotonic()
        result = SyncResult(dataset="calendar")
        days: list[str] = []
        try:
            days = self.tushare.trade_dates(start, end) if self.tushare else []
        except Exception as exc:  # noqa: BLE001
            result.status, result.message = "failed", brief(exc, BRIEF_TIGHT)
        if days:
            result.rows = self._write(
                "INSERT INTO ml_calendar(trade_date, is_open) VALUES(?, 1)"
                " ON CONFLICT(trade_date) DO UPDATE SET is_open=1",
                [(day,) for day in days])
            self._calendar = []
        elif result.status == "ok":
            result.status = "partial"
            result.message = "交易日历为空"
        result.seconds = time.monotonic() - started
        self._log_sync(result, (start, end))
        return result

    #: 修补日历时用来**交叉确认**的行情表。
    #:
    #: 为什么必须是"多表交叉"而不是单表：单张表在节假日也可能有零散行
    #: （实测 `ml_board_bar` 在 2026-05-01 仍有 32 个板块、2025-05-01 有 250 个），
    #: 拿单表补日历会把休市日写成交易日，于是那张表在这些日期上有数据、
    #: 其它板块没有 —— 宽表里出现一整行 NaN，`rolling(20)` 要求窗口内 20 个
    #: 有效值，一个 NaN 就让**之后 20 个交易日的波动率特征全变 NaN**。
    #: 这个坑本轮真踩到了（告警集 42% 的行 `vol20` 缺失）。
    CALENDAR_WITNESSES: tuple[str, ...] = ("ml_index", "ml_future", "ml_etf")

    def repair_calendar(self, *, start: str = "", end: str = "",
                        min_sources: int = 2,
                        apply: bool = True) -> SyncResult:
        """把「多张行情表都有数据、但交易日历里没有」的日期补回日历。

        ## 为什么必须补

        `sync_calendar` 完全依赖上游 `trade_cal`，上游给什么就是什么。
        实测 `ml_calendar` **整段缺 20251201~20251231（23 个交易日）**，
        而 `ml_index` / `ml_future` / `ml_etf` 这 23 天都有连续数据。
        日历一缺，后果是链式的：

        1. `rescore_mainline._trading_days` 只读日历 → **整个 12 月不被打分**，
           结果库里凭空少 23 天，最新回测窗口从 234 天缩到 211 天；
        2. 突破触发要取"过去 120 个交易日"的 `total` 序列，这个洞正好落在
           2026 年上半年的回看窗口里 → 上沿是用跨过空洞的数据算的；
        3. `score_total_history_sync` 的连续性闸门在 20260105 当天判为
           "表落后 38 天"而整条路径失效。

        判据用"多张独立行情表交叉确认"，`min_sources` 默认 2：
        单表有数据不算（可能是节假日脏数据），两表以上同日有数据才算真交易日。

        `start` / `end` 留空时，默认只在**日历自身已有的区间内**补洞 ——
        这是保守默认：交叉表能回溯到 2018 年，把 1200 多天历史全塞进日历
        会把日历的语义从"本轮研究区间"改成"全历史"，并且会让任何
        "按日历逐日重算"的脚本默认多跑几年。要补更早的区间就显式传参。
        """
        started = time.monotonic()
        result = SyncResult(dataset="calendar_repair")
        seen: dict[str, int] = {}
        with self._connect() as connection:
            bounds = connection.execute(
                "SELECT MIN(trade_date) AS a, MAX(trade_date) AS b"
                " FROM ml_calendar").fetchone()
            low = start or str(bounds["a"] or "")
            high = end or str(bounds["b"] or "")
            if not low or not high:
                result.status, result.message = "partial", "日历为空，无从判断区间"
            else:
                span = (low, high)
                existing = {str(row[0]) for row in connection.execute(
                    "SELECT trade_date FROM ml_calendar"
                    " WHERE trade_date BETWEEN ? AND ?", span)}
                for table in self.CALENDAR_WITNESSES:
                    for row in connection.execute(
                            f"SELECT DISTINCT trade_date FROM {table}"  # noqa: S608
                            " WHERE trade_date BETWEEN ? AND ?", span):
                        day = str(row[0])
                        if day and day not in existing:
                            seen[day] = seen.get(day, 0) + 1
        missing = sorted(day for day, count in seen.items() if count >= min_sources)
        weak = sorted(day for day, count in seen.items() if count < min_sources)
        result.missing = weak
        if missing and apply:
            result.rows = self._write(
                "INSERT INTO ml_calendar(trade_date, is_open) VALUES(?, 1)"
                " ON CONFLICT(trade_date) DO UPDATE SET is_open=1",
                [(day,) for day in missing])
            self._calendar = []
        if missing:
            head = "、".join(missing[:6])
            tail = "…" if len(missing) > 6 else ""
            result.message = (f"{'补入' if apply else '待补'} {len(missing)} 个交易日："
                              f"{head}{tail}")
        if weak:
            result.status = "partial"
        result.seconds = time.monotonic() - started
        self._log_sync(result, (start, end))
        return result

    # ---------- 板块目录与成分股 ----------

    def boards(self, *, refresh: bool = False,
               include_excluded: bool = False) -> list[BoardInfo]:
        """读本地板块目录（为空或 `refresh` 时向远端同步）。

        ## `include_excluded`：给"生成剔除清单"用的完整目录

        默认 `False` —— 两道剔除清单（`sector_blacklist.yaml` 与按 20 日胜率生成的
        `mainline_theme_exclusions.yaml`）里的板块都不返回，这就是打分池。

        `True` 时**跳过两道清单**，返回 `ml_board` 里的完整目录。
        ⚠️ 生成剔除清单的脚本**必须**用它：否则上次被剔掉的题材这次根本读不到，
        重新生成时它们会从清单里消失 —— 下一轮打分它们又回到池子里，来回振荡，
        而且每一次看起来都是"正常生成"。

        ## 黑名单在**读取时**排除（而不是靠删除）

        `configs/sector_blacklist.yaml` 里的板块（已淘汰的 `865xxx` 概念体系、
        宽基/风格指数）不进打分池 —— 它们与 `875xxx`/`885xxx` 存在同名不同码的
        换代关系，漏进来会让同名板块在池里出现两次。

        原来的实现是"导入池子时把这些行**从 `ml_board` 删掉**"，但那会连带删掉
        主线需要的其余 1800+ 个概念板块（详见 `_prune_boards` 的 docstring，
        这个 bug 实测踩了两次）。所以改成读时过滤：`ml_board` 保留完整目录
        （`relevance` 的题材→板块映射需要它），`boards()` 只返回可用池。
        """
        if refresh or not self._read("SELECT 1 FROM ml_board LIMIT 1"):
            self.sync_boards()
        rows = self._read("SELECT * FROM ml_board ORDER BY kind, code")
        if include_excluded:
            # 生成剔除清单的脚本走这条路：它必须看到**包括已被剔除的**题材
            return [BoardInfo(
                code=str(row["code"]), name=str(row["name"]),
                kind=BoardKind(str(row["kind"]))
                if str(row["kind"]) in BoardKind._value2member_map_
                else BoardKind.CONCEPT,
                members=int(row["members"] or 0),
                source=str(row["source"] or ""),
                list_date=str(row["list_date"] or "")) for row in rows]
        blocked = load_sector_blacklist(
            getattr(self.config.universe, "blacklist_file",
                    "sector_blacklist.yaml")) or frozenset()
        # 再叠加一层：按回测 20 日胜率生成的题材剔除清单（`theme_gate`）。
        # ⚠️ 两处**共用这一个出口**是刻意的 —— 打分（`service`）、告警、以及
        # `sync_board_bars` 全都经过 `boards()`，所以"剔除一个题材"只要在这里
        # 生效，就不可能出现"不打分但还在同步/还在告警"的半生效状态。
        blocked = blocked | load_theme_exclusions(
            getattr(self.config.universe, "theme_exclude_file",
                    "mainline_theme_exclusions.yaml"))
        out: list[BoardInfo] = []
        for row in rows:
            code = str(row["code"])
            if code in blocked:
                continue
            out.append(BoardInfo(
                code=code, name=str(row["name"]),
                kind=BoardKind(str(row["kind"]))
                if str(row["kind"]) in BoardKind._value2member_map_
                else BoardKind.CONCEPT,
                members=int(row["members"] or 0),
                source=str(row["source"] or ""),
                list_date=str(row["list_date"] or "")))
        return out

    def sync_boards(self) -> SyncResult:
        """同步板块目录，落 `ml_board`。

        `universe.source == "crowding"` 时**委托给 `import_crowding_pool`** ——
        否则日常同步会走"自建 ths_index 名录"这条路，把自己拉的两千多个板块
        写回 `ml_board`，把"只用拥挤度 445 个"的口径悄悄改掉
        （用户看到的「同花顺A50 / A50全收益」就是这么混进来的）。
        """
        if getattr(self.config.universe, "source", "crowding") == "crowding":
            return self.import_crowding_pool()
        started = time.monotonic()
        result = SyncResult(dataset="board")
        catalog = getattr(self.remote, "catalog", None)
        if catalog is None:
            result.status, result.message = "skipped", "无板块目录源"
            return result
        try:
            boards = catalog.all_boards(force=True)
        except Exception as exc:  # noqa: BLE001
            result.status, result.message = "failed", brief(exc, BRIEF_TIGHT)
            result.seconds = time.monotonic() - started
            self._log_sync(result, ("", ""))
            return result
        now = _now()
        result.rows = self._write(
            "INSERT INTO ml_board(code, name, kind, members, source, list_date,"
            " updated_at) VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(code) DO UPDATE SET name=excluded.name,"
            " kind=excluded.kind,"
            # members 只在远端给出正值时覆盖：名录源偶尔返回 0，
            # 用它覆盖会把已知的成分股数抹成 0，进而让板块被成员数过滤掉。
            " members=CASE WHEN excluded.members > 0 THEN excluded.members"
            "               ELSE ml_board.members END,"
            " source=excluded.source, list_date=excluded.list_date,"
            " updated_at=excluded.updated_at",
            [(item.code, item.name, item.kind.value, int(item.members or 0),
              item.source, item.list_date, now) for item in boards])
        removed = self._prune_boards({item.code for item in boards},
                                    source="tushare:ths_index")
        if not result.rows:
            result.status, result.message = "partial", "板块目录为空"
        elif removed:
            result.message = f"清理了 {removed} 个已不在名录里的板块"
        result.seconds = time.monotonic() - started
        self._log_sync(result, ("", ""))
        return result

    def _prune_boards(self, keep: set[str], *, source: str = "") -> int:
        """删掉**本名录源已移除**的板块行（返回删除数）。

        ## ⚠️ 为什么必须限定 `source`（实测踩过两次，损失很大）

        `config.universe.source == "crowding"` 时，`sync_boards` 委托给
        `import_crowding_pool()`，它只带来 335 个拥挤度板块；而不限定来源的
        清理会把 `ml_board` 里其余 **1800+ 个概念板块**当成"已不在名录里"删掉。

        后果极其隐蔽：`ml_board` 从 2232 掉到 335，而主线打分**只遍历
        `ml_board`** —— 1800+ 个板块静默退出打分。`ml_member` / `ml_board_bar`
        的数据都在（清理不碰它们），所以"数据看起来齐全"，只有排行榜悄悄变短。
        更麻烦的是这会让 `relevance` 的题材→板块映射塌掉（实测成分股归属从
        13.7 万掉到 5.3 万），而修复它要重跑一遍 LLM 打分。

        ## 两道安全阀

        1. `keep` 少于 10 个时不动手（正常的全市场目录远不止 10 个）；
        2. 只删 `ml_board` 本身。`ml_member` / `ml_board_bar` / `ml_board_flow`
           里的孤儿行留着 —— 删 17 万行成分股的代价远大于收益。
        """
        if len(keep) < 10:
            logger.warning("板块目录只拿到 %d 个，跳过清理（防止误删）", len(keep))
            return 0
        sql = "SELECT code, source FROM ml_board"
        params: tuple[Any, ...] = ()
        if source:
            sql += " WHERE source = ?"
            params = (source,)
        rows = self._read(sql, params)
        stale = [str(row["code"]) for row in rows
                 if str(row["code"]) not in keep]
        if not stale:
            return 0
        before = len(self._read("SELECT code FROM ml_board"))
        self.ensure_schema()
        with self._lock, self._connect() as connection:
            connection.executemany("DELETE FROM ml_board WHERE code = ?",
                                   [(code,) for code in stale])
        after = before - len(stale)
        logger.info("板块目录清理：删除 %d 个已不在名录里的板块（来源 %s），"
                    "目录 %d → %d", len(stale), source or "全部", before, after)
        # 目录萎缩告警：一次删除超过三成就很可能是"名录源变窄"而不是真下架，
        # 值得人工看一眼（这类故障以前是完全静默的）。
        if before >= 100 and after < before * 0.7:
            logger.warning(
                "⚠️ 板块目录大幅萎缩：%d → %d（删除 %d 个，来源 %s）。"
                "若这不是预期行为，请检查 sync_boards 的名录源是否变窄 —— "
                "主线打分只遍历 ml_board，板块会静默退出打分。",
                before, after, len(stale), source or "全部")
        return len(stale)

    def members(self, board_code: str) -> list[dict[str, Any]]:
        """读某板块成分股（含 in_date/out_date，申万口径可做 point-in-time）。"""
        rows = self._read(
            "SELECT code, name, in_date, out_date, source FROM ml_member"
            " WHERE board_code = ? ORDER BY code", (board_code,))
        return [{k: row[k] for k in row.keys()} for row in rows]

    def member_map(self) -> dict[str, list[str]]:
        """全部板块的成分股 `{board_code: [code]}`（一次读齐，避免 N 次查询）。"""
        rows = self._read("SELECT board_code, code FROM ml_member ORDER BY code")
        out: dict[str, list[str]] = {}
        for row in rows:
            out.setdefault(str(row["board_code"]), []).append(str(row["code"]))
        return out

    # ---------- 个股静态信息（上市日期） ----------

    def pure_member_relevance(self) -> dict[str, dict[str, int]]:
        """读提纯股池 `{板块: {股票: relevant}}`。

        `ml_member_pure` 是 `scripts/purify_members.py` 的产物：
        走势相关性（本地算，免费）+ 主营业务相关度（LLM 判定）。
        它是**用户配置的股池口径**（"每个概念板块用哪些股"）。

        ⚠️ 返回的是**全部**行（含 `relevant=0`），不是只返回相关的 ——
        调用方（`apply_pure_pool`）必须能区分「有行且为 0」与「没有行」，
        这两者的处置正好相反。只返回 1 会让这个区分丢失。
        """
        try:
            rows = self._read(
                "SELECT board_code, code, relevant FROM ml_member_pure")
        except Exception as exc:  # noqa: BLE001 表缺失不该让打分失败
            logger.info("主线挖掘数据仓：读 ml_member_pure 失败（回落 relevance）：%s",
                        brief(exc, BRIEF_TIGHT))
            return {}
        out: dict[str, dict[str, int]] = {}
        for row in rows:
            out.setdefault(str(row["board_code"]), {})[str(row["code"])] = \
                int(row["relevant"] or 0)
        return out

    def pure_member_business(self) -> dict[str, dict[str, dict[str, Any]]]:
        """读 `{板块: {股票: {score, source, reason}}}` —— **只用于展示**。

        ## 为什么需要它

        用户 2026-09-22 看到氟化工概念的龙头里有雅克科技（主营半导体材料），
        质疑"龙头股定义错了"。查下来：成员没错（同花顺的正式成员）、
        `identify_leaders()` 也没错（它只看资金/动量/量能三维，业务从不参与）——
        问题是**界面上看不出"这只票凭什么是这个概念的成员"**。

        所以把入池依据带上去，让"资金龙头"与"业务龙头"分得开。
        **不参与任何判定。**

        ## ⚠️ 为什么要回查 `ml_stock_theme`，不能只看 `ml_member_pure`

        `ml_member_pure.business_score` 对 `source='corr'` 的行**恒为 NULL**
        （相关性入池本来就没跑 LLM）。只看它会把**昊华科技**标成
        "仅股价相关" —— 而 LLM 明明给过它「氟化工 95 分，高端氟材料为核心主业」。

        所以对 `business_score` 为空的行，再按**题材名匹配**回查
        `ml_stock_theme`（LLM 的逐 (股票,题材) 判定）：
        两边名字做归一化（去掉「概念/板块/指数/产业/行业」后缀）后互相包含即算同一题材。
        这样标签才是可信的 —— 否则这一列会**用错误信息误导用户**，
        比不加这一列更糟。

        ## ⚠️ 只返回 `relevant=1`（真正入池）的成员

        这张表里既有"入池"也有"被剔除"的行，展示层只能讲入池的那部分：
        否则会把 `corr 排名在 70% 之外` 被剔掉的票也显示成板块成员，
        与股池口径不一致（实测 885652 会多出 3 只）。

        ⚠️ 读不到表时返回 `{}`（不影响打分，只是少一列展示信息）。
        """
        try:
            rows = self._read(
                "SELECT p.board_code, p.code, p.business_score, p.source,"
                " p.reason, b.name AS board_name"
                " FROM ml_member_pure p"
                " LEFT JOIN ml_board b ON b.code = p.board_code"
                # ⚠️ **只取真正入池的成员**（`relevant=1`）。展示层必须与股池口径
                # 一致：否则会把"没进池"的票也说成成员 —— 实测 885652 提纯池是
                # 10 只，不带这个条件时会多出 002838（`corr 排名在 70% 之外`
                # 被剔除、业务分 5）等 3 只，界面于是讲了一个和股池不同的故事。
                " WHERE p.relevant = 1")
        except Exception as exc:  # noqa: BLE001 表缺失不该让打分失败
            logger.info("主线挖掘数据仓：读 ml_member_pure 业务信息失败：%s",
                        brief(exc, BRIEF_TIGHT))
            return {}
        themes: dict[str, list[tuple[str, float]]] = {}
        try:
            for row in self._read(
                    "SELECT code, theme, business_score FROM ml_stock_theme"
                    " WHERE business_score IS NOT NULL"):
                themes.setdefault(str(row["code"]), []).append(
                    (str(row["theme"] or ""), float(row["business_score"] or 0.0)))
        except Exception as exc:  # noqa: BLE001 回查失败就退化成只读纯化表
            logger.info("主线挖掘数据仓：读 ml_stock_theme 失败（业务分回查跳过）：%s",
                        brief(exc, BRIEF_TIGHT))

        out: dict[str, dict[str, dict[str, Any]]] = {}
        for row in rows:
            code, board = str(row["code"]), str(row["board_code"])
            score = row["business_score"]
            source = str(row["source"] or "")
            reason = str(row["reason"] or "")
            # ⚠️ `ml_member_pure.business_score` 对 `source='corr'` **并非恒为 NULL**：
            # `purify_members.py` 会把 LLM 判过的分一起写进来，而选择阶段仍按
            # `corr >= 0.55` 把票收进来。实测（2026-09-23）池内 relevant=1 里
            # 这类有 **4216 条**，其中 2860 条是"LLM 判过且 <70"——即
            # **有明确反向证据仍靠相关性入池**。
            #
            # 这类票过去会被 `business_label` 说成"LLM 未判过该题材"，与数据相反
            # （用户报障：002140 东华科技 @ 885652 钛白粉概念）。所以下面把
            # **入池通道**（`admission`）与**业务分来源**（`source`）分开记录 ——
            # 两者是不同的问题，混在一个字段里必然说出错话。
            if score is None:
                found = [(t, s) for t, s in themes.get(code, [])
                         if _same_theme(str(row["board_name"] or ""), t)]
                if found:
                    best = max(found, key=lambda item: item[1])
                    score, source = best[1], "llm_theme"
                    reason = f"LLM 判定「{best[0]}」主营分 {best[1]:.0f}"
            out.setdefault(board, {})[code] = {
                "score": score, "source": source, "reason": reason,
                "admission": _admission(source, score)}
        return out


    def first_sync_epoch(self) -> float:
        """提纯表的刷新时刻（epoch 秒）；拿不到返回 0。"""
        return self._table_epoch("ml_member_pure", "refreshed_at")

    def member_epoch(self) -> float:
        """成分股表的刷新时刻（epoch 秒）—— 用于判断提纯结果是否**比名单旧**。"""
        return self._table_epoch("ml_member", "updated_at")

    def _table_epoch(self, table: str, column: str) -> float:
        try:
            rows = self._read(
                f"SELECT MAX({column}) v FROM {table}")
        except Exception:  # noqa: BLE001
            return 0.0
        value = str(rows[0]["v"] or "") if rows else ""
        if not value:
            return 0.0
        try:
            from datetime import datetime
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0

    def stock_list_dates(self) -> dict[str, str]:
        """`{股票代码: 上市日期 YYYYMMDD}`。

        ## 为什么必须要有它（回测的前视偏差）

        板块的成分股是**当前快照**（`ml_member` 全部行的 `out_date` 都是空，
        没有任何历史移出记录），而回测某一天时用的却是这份今天的名单。
        一只 2026 年才上市的票，会被算进 2023 年的板块资金流里 ——
        等于让模型看到未来才存在的股票。

        上市日期闸门只修住其中一个方向（"当时还没上市"），
        但那是**最严重**的方向：新股上市初期的成交/资金特征与老股完全不同，
        混进去会系统性地抬高板块分。

        ## 优先级

        1. `ml_stock_meta`（本地记录，回测可复现 —— 见 `sync_stock_meta`）
        2. 行情仓 `quant_stock_basic`（权威、全量；本地表为空时回落）

        两条路都拿不到代码时**不返回该代码**，由调用方决定策略。
        """
        out: dict[str, str] = {}
        try:
            rows = self._read(
                "SELECT code, list_date FROM ml_stock_meta")
            out = {str(row["code"]): str(row["list_date"] or "")
                   for row in rows}
        except Exception as exc:  # noqa: BLE001 本地表缺失不该让打分失败
            logger.info("主线挖掘数据仓：读 ml_stock_meta 失败（回落行情仓）：%s",
                        brief(exc, BRIEF_TIGHT))
        if out:
            return out
        return self._list_dates_from_warehouse()

    def _list_dates_from_warehouse(self) -> dict[str, str]:
        """从行情仓 `quant_stock_basic` 读上市日期（只读，5562 行，很快）。

        走 `self.warehouse.query`（与板块资金流、成分股截面同一入口），
        而不是自己开一个 sqlite 连接 —— 行情仓是 15 GiB 只读库，
        连接策略（`mode=ro` / timeout / row_factory）应当只有一处。
        """
        warehouse = self.warehouse
        if warehouse is None or not getattr(warehouse, "available",
                                            lambda: False)():
            logger.info("主线挖掘数据仓：行情仓不可用，上市日期为空")
            return {}
        try:
            rows = warehouse.query(
                "SELECT code, list_date FROM quant_stock_basic")
        except Exception as exc:  # noqa: BLE001 行情仓异常不该让打分失败
            logger.warning("主线挖掘数据仓：读 quant_stock_basic 失败：%s",
                           brief(exc, BRIEF_TIGHT))
            return {}
        return {str(row["code"]): str(row["list_date"] or "").strip()
                for row in rows}

    def sync_stock_meta(self) -> SyncResult:
        """把行情仓的上市日期**记录**到 `ml_stock_meta`（回测可复现）。

        为什么要落一份到本地：行情仓是按 `list_status='L'` 同步的**当前上市**
        名录，会随退市/新上市变化。回测要能复现"当时按哪份上市日期做的剔除"，
        就不能直接依赖一个会变的表。
        """
        started = time.monotonic()
        result = SyncResult(dataset="stock_meta")
        listing = self._list_dates_from_warehouse()
        if not listing:
            result.status, result.message = "failed", "行情仓没有 quant_stock_basic"
            result.seconds = time.monotonic() - started
            self._log_sync(result, ("", ""))
            return result
        now = _now()
        result.rows = self._write(
            "INSERT INTO ml_stock_meta(code, name, list_date, updated_at)"
            " VALUES(?,?,?,?) ON CONFLICT(code) DO UPDATE SET"
            " list_date=excluded.list_date, updated_at=excluded.updated_at",
            [(code, "", value, now) for code, value in sorted(listing.items())])
        result.status = "ok" if result.rows else "partial"
        result.seconds = time.monotonic() - started
        self._log_sync(result, ("", ""))
        return result

    def sync_members(self, *, limit: int = 0,
                     codes: Sequence[str] | None = None) -> SyncResult:
        """同步板块成分股（逐板块调用，成本高；`limit`/`codes` 可缩小范围）。

        `codes` 用于**定点排查**：只查某几个板块（如"CRO 概念 + 医药生物"），
        不必为了看两个板块就把 2200 个板块的成分股全拉一遍。
        """
        started = time.monotonic()
        result = SyncResult(dataset="member")
        boards = self.boards()
        if codes:
            wanted = set(codes)
            boards = [item for item in boards if item.code in wanted]
        if limit:
            boards = boards[:limit]
        catalog = getattr(self.remote, "catalog", None)
        if catalog is None or not boards:
            result.status, result.message = "skipped", "无目录或无板块"
            return result
        now = _now()
        total = 0
        failed: list[str] = []
        for board in boards:
            try:
                codes = catalog.members_of(board)
            except Exception as exc:  # noqa: BLE001
                failed.append(board.code)
                logger.info("成分股同步失败 %s：%s", board.code,
                            brief(exc, BRIEF_TIGHT))
                continue
            if not codes:
                failed.append(board.code)
                continue
            # 申万口径能拿到 in_date/out_date（point-in-time）；概念只有当前快照
            rows = [(board.code, code, "", "", "", board.source) for code in codes]
            if board.kind is BoardKind.SW_L1:
                rows = [(board.code, code, "", "", "", board.source)
                        for code in codes]
            total += self._write(
                "INSERT INTO ml_member(board_code, code, name, in_date,"
                " out_date, source, updated_at) VALUES(?,?,?,?,?,?,?)"
                " ON CONFLICT(board_code, code) DO UPDATE SET"
                " source=excluded.source, updated_at=excluded.updated_at",
                [(*row, now) for row in rows])
        result.rows = total
        result.missing = failed
        result.status = "ok" if not failed else "partial"
        if failed:
            result.message = f"{len(failed)} 个板块取不到成分股（已记录）"
        result.seconds = time.monotonic() - started
        self._log_sync(result, ("", ""))
        return result

    def sync_members_sw_pit(self) -> SyncResult:
        """用 `index_member_all` 落申万成分股的 **in_date/out_date**（point-in-time）。

        概念板块的成分股只有当前快照（`ths_member` 不带历史），回测里因此
        存在**幸存者偏差**；申万一级行业能拿到调入/调出日期，至少让行业口径
        的回测不受这个偏差影响。这是本模块对回测可信度最重要的一处补偿。
        """
        started = time.monotonic()
        result = SyncResult(dataset="member_pit")
        tushare = self.tushare
        if tushare is None:
            result.status, result.message = "skipped", "无 Tushare 源"
            return result
        boards = [item for item in self.boards()
                  if item.kind is BoardKind.SW_L1]
        now = _now()
        total = 0
        failed: list[str] = []
        for board in boards:
            try:
                frame = tushare.client.call("index_member_all",
                                            l1_code=board.code)
            except Exception as exc:  # noqa: BLE001
                failed.append(board.code)
                logger.info("index_member_all %s 失败：%s", board.code,
                            brief(exc, BRIEF_TIGHT))
                continue
            if frame is None or len(frame) == 0:
                failed.append(board.code)
                continue
            rows = []
            for row in frame.to_dict("records"):
                code = _s(row.get("ts_code")).split(".")[0].zfill(6)
                if not code or code == "000000":
                    continue
                rows.append((board.code, code, _s(row.get("name")),
                             _s(row.get("in_date")), _s(row.get("out_date")),
                             "tushare:index_member_all", now))
            total += self._write(
                "INSERT INTO ml_member(board_code, code, name, in_date,"
                " out_date, source, updated_at) VALUES(?,?,?,?,?,?,?)"
                " ON CONFLICT(board_code, code) DO UPDATE SET"
                " name=excluded.name, in_date=excluded.in_date,"
                " out_date=excluded.out_date, source=excluded.source,"
                " updated_at=excluded.updated_at", rows)
        result.rows = total
        result.missing = failed
        result.status = "ok" if not failed else "partial"
        result.seconds = time.monotonic() - started
        self._log_sync(result, ("", ""))
        return result

    # ---------- 板块指数日线 ----------

    def board_bars(self, codes: Sequence[str], *, start: str, end: str
                   ) -> dict[str, BoardSeries]:
        """读板块指数日线（只读本地，不触发同步）。"""
        if not codes:
            return {}
        marks = ",".join("?" for _ in codes)
        rows = self._read(
            f"SELECT * FROM ml_board_bar WHERE board_code IN ({marks})"
            " AND trade_date BETWEEN ? AND ? ORDER BY board_code, trade_date",
            (*codes, start, end))
        out: dict[str, BoardSeries] = {}
        for row in rows:
            code = str(row["board_code"])
            series = out.get(code)
            if series is None:
                series = BoardSeries(code=code, source=str(row["source"] or ""))
                out[code] = series
            series.bars.append(BoardBar(
                date=str(row["trade_date"]), open=_f(row["open"]),
                high=_f(row["high"]), low=_f(row["low"]),
                close=_f(row["close"]), volume=_f(row["volume"]),
                amount=_f(row["amount"]), pct_change=_f(row["pct_change"]),
                pre_close=_f(row["pre_close"])))
        return out

    def board_codes_with_bars(self, *, start: str, end: str,
                              fresh_days: int = 5) -> set[str]:
        """在 `[start, end]` 内**数据新鲜到 `end` 附近**的板块代码集合。

        用途：打分前先把"没数据 / 数据过期"的板块剔掉，**再**按 `max_boards` 截断。

        ⚠️ 两条都不能省，两条都踩过：

        1. **顺序不能反。** 早先是"先截断再过滤"：`max_boards=320` 会把 2197 个
           板块按 `(kind, code)` 排序的前 320 个留下 —— 而概念代码段 700xxx/86xxxx
           排在 88xxxx 之前，于是全部医药、半导体等概念在**打分之前就被丢掉**，
           最终可用板块数可以是 0，而界面上只显示"全市场 0 个板块"。
           `(kind, code)` 与"板块重不重要"毫无关系，用它截断等于随机丢弃。
        2. **必须查"新鲜度"，不能只看"窗口内有没有行"。** 只判断
           `trade_date BETWEEN start AND end` 会把**数据停在半年前**的板块也算进来：
           它的技术指标、量能、形态全是半年前的值，却和当日板块一起排序 ——
           分数照常产出、排序照常给出，没有任何报错。
           实测这个 bug 让同一天的标的池在 131 与 57 之间跳变（取决于 550 天回看
           窗口的左端是否跨过那批陈旧数据的末日），而面板上只看到"板块数变了"。

        新鲜度口径：取窗口内**第 `fresh_days + 1` 个不同的交易日**作为门槛，
        板块的最后一条数据不早于它就是新鲜的。用"第 N 个不同交易日"而不是
        `end - N 天`：后者在长假（春节）前后会把正常板块误判成过期。
        """
        rows = self._read(
            "SELECT board_code, MAX(trade_date) AS last FROM ml_board_bar"
            " WHERE trade_date BETWEEN ? AND ? GROUP BY board_code",
            (start, end))
        if not rows:
            return set()
        days = sorted({str(row["last"]) for row in rows}, reverse=True)
        cutoff = days[min(max(int(fresh_days), 0), len(days) - 1)]
        return {str(row["board_code"]) for row in rows
                if str(row["last"]) >= cutoff}

    def board_bar_dates(self, code: str) -> tuple[str, str]:
        rows = self._read(
            "SELECT MIN(trade_date) AS a, MAX(trade_date) AS b FROM ml_board_bar"
            " WHERE board_code = ?", (code,))
        if not rows or rows[0]["a"] is None:
            return "", ""
        return str(rows[0]["a"]), str(rows[0]["b"])

    def sync_board_bars(self, *, start: str, end: str,
                        codes: Sequence[str] | None = None,
                        progress: Callable[[str], None] | None = None
                        ) -> SyncResult:
        """同步板块指数日线（申万区间分块 + 概念逐板块区间）。

        实测口径（2026-09）：

        - `sw_daily(start_date, end_date)` 一次最多约 4000 行（单日 439 行），
          因此按 **8 个交易日**分块；
        - `ths_daily` 必须给 `ts_code`，历史按板块逐个取（一次拿全区间，
          实测单板块可回溯到 2020-07，覆盖 2022 起的回测区间）。

        ⚠️ 概念部分是**逐板块**调用，所以耗时与板块数成正比、**与日期区间无关**
        —— 补 10 天和补 3.7 年是同样的调用次数。380 个板块是一次长途跋涉，
        因此必须传 `progress` 才有可见性（否则只能盲等到结束）。
        """
        started = time.monotonic()
        result = SyncResult(dataset="board_bar")
        tushare = self.tushare
        if tushare is None:
            result.status, result.message = "skipped", "无 Tushare 源"
            return result
        wanted = set(codes) if codes else None
        boards = [item for item in self.boards()
                  if wanted is None or item.code in wanted]
        days = self.calendar(start, end)
        total = 0
        failed: list[str] = []

        # 1) 申万一级：区间分块批量取
        sw = [item for item in boards if item.kind is BoardKind.SW_L1]
        if sw and days:
            sw_codes = {item.code for item in sw}
            for offset in range(0, len(days), 8):
                window = days[offset:offset + 8]
                try:
                    frame = tushare.client.call(
                        "sw_daily", start_date=window[0], end_date=window[-1])
                except Exception as exc:  # noqa: BLE001
                    failed.extend(window)
                    logger.info("sw_daily %s~%s 失败：%s", window[0], window[-1],
                                brief(exc, BRIEF_TIGHT))
                    continue
                if frame is None or len(frame) == 0:
                    failed.extend(window)
                    continue
                rows = []
                for row in frame.to_dict("records"):
                    code = _s(row.get("ts_code"))
                    if code not in sw_codes:
                        continue
                    close = _f(row.get("close"))
                    change = _f(row.get("change"))
                    rows.append((code, _s(row.get("trade_date")),
                                 _f(row.get("open")), _f(row.get("high")),
                                 _f(row.get("low")), close,
                                 # `sw_daily` **不返回 pre_close**，只有 change
                                 # （实测 2026-09）；前收 = 收盘 - 涨跌额。
                                 # 用 0 当缺省会让"隔夜跳空"维度整列变成 inf。
                                 close - change if change else 0.0,
                                 _f(row.get("vol")),
                                 _f(row.get("amount")) * 1000.0,  # 千元 → 元
                                 _f(row.get("pct_change")),
                                 "tushare:sw_daily"))
                total += self._write(_BAR_UPSERT, rows)

        # 2) 概念：逐板块取全区间
        concept = [item for item in boards if item.kind is BoardKind.CONCEPT]
        for index, board in enumerate(concept, start=1):
            # 逐板块报进度：这是整轮里唯一的长循环，没有它就只能盲等。
            # 回调只传字符串（与 `sync_all` 同约定），打印/计数由调用方决定。
            if progress is not None:
                progress(f"ths_daily {index}/{len(concept)} {board.code} "
                         f"{board.name}")
            try:
                frame = tushare.client.call("ths_daily", ts_code=board.code,
                                            start_date=start, end_date=end)
            except Exception as exc:  # noqa: BLE001
                failed.append(board.code)
                logger.info("ths_daily %s 失败：%s", board.code,
                            brief(exc, BRIEF_TIGHT))
                continue
            if frame is None or len(frame) == 0:
                failed.append(board.code)
                continue
            rows = [(board.code, _s(row.get("trade_date")),
                     _f(row.get("open")), _f(row.get("high")),
                     _f(row.get("low")), _f(row.get("close")),
                     _f(row.get("pre_close")), _f(row.get("vol")),
                     0.0, _f(row.get("pct_change")), "tushare:ths_daily")
                    for row in frame.to_dict("records")]
            total += self._write(_BAR_UPSERT, rows)

        # 2b) **当日数据是逐板块陆续发布的**：必须校验 `end` 那天到没到，缺的再取一轮。
        #
        # 为什么不校验就是错的（2026-09-23 实测）：`ths_daily` 对每个板块都返回了
        # **非空**的历史区间，所以"返回非空 = 成功"这类判断全都会通过 ——
        # 于是 19:07 那次同步只有 **66/138** 个板块带上了 09-23，
        # 作业却报 `ok / missing=[]`；10 分钟后重跑，**138/138** 全都有了。
        # 后果不是"少几个板块"：评分按"窗口内有数据"筛板块（`board_codes_with_bars`
        # 只看区间），缺当天的那 72 个会**拿上一交易日的 K 线**参与当天打分 ——
        # 分数照出、排名照排、界面上看不出任何异常（本项目反复记录的那类静默）。
        pending = sorted(code for code in (item.code for item in boards)
                         if code not in self.board_codes_with_bar_on(end))
        rounds = 0
        while pending and rounds < _BAR_DATE_RETRY_ROUNDS:
            rounds += 1
            if progress is not None:
                progress(f"等当日指数发布：还差 {len(pending)} 个板块，第 {rounds} 轮")
            time.sleep(_BAR_DATE_RETRY_SLEEP)
            sw_pending = [item for item in boards
                          if item.code in pending and item.kind is BoardKind.SW_L1]
            if sw_pending:      # 申万是**批量**接口，缺了当天就整批重取一次
                try:
                    frame = tushare.client.call(
                        "sw_daily", start_date=end, end_date=end)
                    sw_codes = {item.code for item in sw_pending}
                    rows = [(str(row.get("ts_code")), _s(row.get("trade_date")),
                             _f(row.get("open")), _f(row.get("high")),
                             _f(row.get("low")), _f(row.get("close")),
                             _f(row.get("close")) - _f(row.get("change"))
                             if _f(row.get("change")) else 0.0,
                             _f(row.get("vol")), _f(row.get("amount")) * 1000.0,
                             _f(row.get("pct_change")), "tushare:sw_daily")
                            for row in (frame.to_dict("records")
                                        if frame is not None and len(frame) else [])
                            if str(row.get("ts_code")) in sw_codes]
                    total += self._write(_BAR_UPSERT, rows)
                except Exception as exc:  # noqa: BLE001 单轮失败留到下一轮
                    logger.info("sw_daily 当日补取失败：%s", brief(exc, BRIEF_TIGHT))
            for code in [item.code for item in boards
                         if item.code in pending
                         and item.kind is not BoardKind.SW_L1]:
                try:
                    frame = tushare.client.call("ths_daily", ts_code=code,
                                                start_date=end, end_date=end)
                except Exception as exc:  # noqa: BLE001 单板块失败留到下一轮
                    logger.info("ths_daily 当日补取 %s 失败：%s", code,
                                brief(exc, BRIEF_TIGHT))
                    continue
                if frame is None or len(frame) == 0:
                    continue
                rows = [(code, _s(row.get("trade_date")),
                         _f(row.get("open")), _f(row.get("high")),
                         _f(row.get("low")), _f(row.get("close")),
                         _f(row.get("pre_close")), _f(row.get("vol")),
                         0.0, _f(row.get("pct_change")), "tushare:ths_daily")
                        for row in frame.to_dict("records")]
                total += self._write(_BAR_UPSERT, rows)
            arrived = self.board_codes_with_bar_on(end)
            pending = [code for code in pending if code not in arrived]

        result.rows = total
        result.missing = sorted(set(failed))
        if pending:
            # 如实报出来：这些板块的当日指数上游还没发布，它们的分数会沿用前一交易日。
            # ⚠️ 只给"取到了历史、只是缺当天"的板块打 `日期:代码` 标记；整块空表的板块
            # 保留裸代码 —— 两者原因不同（一个是发布节奏，一个是这个板块根本取不到）。
            behind = sorted(set(pending) - set(failed))
            if behind:
                result.missing = sorted(set(result.missing)
                                        | {f"{end}:{code}" for code in behind})
            result.message = (f"{len(pending)} 个板块 {end} 当日指数上游未发布"
                              f"（已重试 {rounds} 轮）")
        result.status = "ok" if not result.missing else "partial"
        if result.missing and not result.message:
            result.message = f"{len(result.missing)} 项未取到"
        result.seconds = time.monotonic() - started
        self._log_sync(result, (start, end))
        return result

    def board_codes_with_bar_on(self, trade_date: str) -> set[str]:
        """**某一天**已经有板块指数数据的板块代码集合。

        与 `board_codes_with_bars` 的区别：后者判"窗口内新鲜"，本方法判"这一天有没有"。
        两者都要用 —— 前者决定谁参与打分，后者用来**如实报出"当天还没数据"的板块**
        （见 `sync_board_bars` 的 2b 段与 `service._compute` 的 gaps）。
        """
        rows = self._read(
            "SELECT DISTINCT board_code FROM ml_board_bar WHERE trade_date = ?",
            (str(trade_date),))
        return {str(row["board_code"]) for row in rows}

    # ---------- 板块资金流（成分股聚合） ----------

    def board_flows(self, codes: Sequence[str], *, start: str, end: str
                    ) -> dict[str, BoardFlow]:
        """读板块资金流（只读本地）。"""
        if not codes:
            return {}
        marks = ",".join("?" for _ in codes)
        rows = self._read(
            f"SELECT * FROM ml_board_flow WHERE board_code IN ({marks})"
            " AND trade_date BETWEEN ? AND ? ORDER BY board_code, trade_date",
            (*codes, start, end))
        out: dict[str, BoardFlow] = {}
        for row in rows:
            code = str(row["board_code"])
            flow = out.get(code)
            if flow is None:
                flow = BoardFlow(code=code, source=str(row["source"] or ""))
                out[code] = flow
            flow.points.append((str(row["trade_date"]),
                                _f(row["net_amount"])))
            flow.buy_elg = _f(row["elg_amount"])
            flow.buy_lg = _f(row["lg_amount"])
        return out

    def sync_board_flows(self, *, start: str, end: str,
                         codes: Sequence[str] | None = None,
                         chunk: int = 20) -> SyncResult:
        """用本地行情仓的**个股聚合**生成板块资金流。

        口径（需求 8.4 要求固定一套，这里选 Tushare 个股口径）：

            板块主力净流入 = Σ 成分股 net_mf_amount（万元 → 元）
            板块净占比     = 板块净流入 / 板块成交额
            板块流通市值   = Σ 成分股 circ_mv（万元 → 元）

        为什么不用东财板块口径（`moneyflow_ind_dc`）：实测该接口只有
        **2024 年起**的数据（概念板块更要到 2026 年），覆盖不了 2022 起的
        回测区间；而且它的"主力"定义与个股口径不同，混用就违反了"不混用"。

        成分股按 `ml_member` 取（申万带 in/out 日期，做 point-in-time 过滤）。
        """
        started = time.monotonic()
        result = SyncResult(dataset="board_flow")
        warehouse = self.warehouse
        if warehouse is None or not getattr(warehouse, "available", lambda: False)():
            result.status, result.message = "skipped", "本地行情仓不可用"
            return result

        # ⚠️ 先探一次：行情仓在**这个窗口**里到底有没有数据（2026-09-22 修）。
        #
        # 原来没有这一步。窗口内一行都没有时，`_aggregate` 对每个板块都返回 0 行，
        # 本方法于是记成 `status=ok, rows=0, message=''` —— 与「窗口本来就全是
        # 非交易日」**完全无法区分**。实测用户 19:48 那次刷新留下的记录正是
        # `board_flow | rows=0 | ok | (空) | 2.09 秒`，而真实原因是行情仓
        # `moneyflow` 停在 0915、窗口 0916~0922 一行都没有。
        #
        # 为什么必须报出来：资金流是打分的六维之一，缺了它分数照出、告警照发，
        # 界面上看不出任何异常 —— 只有「最新日期」不对（见 docs §16.73）。
        probe = warehouse.query(
            "SELECT COUNT(*) AS rows_in_window,"
            " (SELECT MAX(trade_date) FROM quant_moneyflow) AS latest"
            " FROM quant_moneyflow WHERE trade_date BETWEEN ? AND ?", (start, end))
        window_rows = int(probe[0]["rows_in_window"] or 0) if probe else 0
        latest = str(probe[0]["latest"] or "") if probe else ""
        # 只有在「行情仓确实落后于窗口起点」时才判失败：窗口本身就是非交易日时
        # 行数也会是 0，那时报失败就是误报。
        if window_rows == 0 and latest and latest < start:
            result.status = "failed"
            result.message = (
                f"行情仓 quant_moneyflow 最新只到 {latest}，{start}~{end} 一行都没有，"
                "板块资金流会整段缺失。先补行情仓（scripts/quant_sync.py download + "
                "scripts/quant_warehouse.py ingest），或直接跑 "
                "scripts/repair_mainline_data.py")
            result.seconds = time.monotonic() - started
            self._log_sync(result, (start, end))
            return result

        boards = [item for item in self.boards()
                  if codes is None or item.code in set(codes)]
        members = self.member_map()
        total = 0
        empty: list[str] = []
        for board in boards:
            pool = members.get(board.code) or []
            if not pool:
                empty.append(board.code)
                continue
            for offset in range(0, len(pool), chunk):
                group = pool[offset:offset + chunk]
                rows = self._aggregate(warehouse, board.code, group, start, end)
                total += self._write(_FLOW_UPSERT, rows)
        result.rows = total
        result.missing = empty
        result.status = "ok" if not empty else "partial"
        if empty:
            result.message = f"{len(empty)} 个板块没有成分股（已记录）"
        result.seconds = time.monotonic() - started
        self._log_sync(result, (start, end))
        return result

    def _aggregate(self, warehouse: Any, board_code: str, codes: Sequence[str],
                   start: str, end: str) -> list[tuple]:
        """单个板块一小组成分股的日度聚合（一条 SQL 出结果）。

        为什么分组（`chunk`）：SQLite 的 `IN (...)` 参数个数有上限
        （默认 999），而一个概念板块最多 400 只成分股 —— 一次全塞进去在
        大多数板块上没问题，但宽基概念会直接抛 "too many SQL variables"。
        """
        marks = ",".join("?" for _ in codes)
        sql = f"""
            SELECT m.trade_date AS trade_date,
                   SUM(m.net_mf_amount) AS net,
                   SUM(d.amount) AS amount,
                   SUM(b.circ_mv) AS circ
            FROM quant_moneyflow m
            JOIN quant_daily d
              ON d.code = m.code AND d.trade_date = m.trade_date
            LEFT JOIN quant_daily_basic b
              ON b.code = m.code AND b.trade_date = m.trade_date
            WHERE m.code IN ({marks}) AND m.trade_date BETWEEN ? AND ?
            GROUP BY m.trade_date
        """
        rows = warehouse.query(sql, (*codes, start, end))
        out: list[tuple] = []
        for row in rows:
            net = _opt(row["net"])
            if net is None:
                continue
            # ⚠️ 本地仓的字段**已经统一换算成「元」**（2026-09 实测比对 Tushare
            # 原始值确认）：`net_mf_amount` = 原始万元 × 1e4、
            # `quant_daily.amount` = 原始千元 × 1e3、
            # `quant_daily_basic.circ_mv` = 原始万元 × 1e4。
            # 所以这里**一个乘数都不能再加** —— 早先按 Tushare 原始口径乘了
            # 1e3/1e4，导致板块资金流被放大 1000~10000 倍，
            # 而分数是横截面分位，被整体放大后**看不出任何异常**。
            net_yuan = net
            amount = _f(row["amount"])
            circ = _f(row["circ"])
            out.append((board_code, str(row["trade_date"]), net_yuan,
                        (net_yuan / amount) if amount else None,
                        None, None, None, circ or None, amount or None,
                        "local:quant_warehouse 个股聚合"))
        return out

    # ---------- 两融 ----------

    def margin_series(self, codes: Sequence[str], *, start: str, end: str
                      ) -> dict[str, list[tuple[str, float]]]:
        """读多只个股的融资余额序列 `{code: [(date, rzye)]}`（只读本地）。"""
        if not codes:
            return {}
        marks = ",".join("?" for _ in codes)
        rows = self._read(
            f"SELECT code, trade_date, rzye FROM ml_margin WHERE code IN ({marks})"
            " AND trade_date BETWEEN ? AND ? ORDER BY code, trade_date",
            (*codes, start, end))
        out: dict[str, list[tuple[str, float]]] = {}
        for row in rows:
            out.setdefault(str(row["code"]), []).append(
                (str(row["trade_date"]), _f(row["rzye"])))
        return out

    def sync_margin(self, *, start: str, end: str) -> SyncResult:
        """同步两融明细（**逐交易日**，实测单日 2600~4500 行，无区间批量）。"""
        started = time.monotonic()
        result = SyncResult(dataset="margin")
        tushare = self.tushare
        if tushare is None:
            result.status, result.message = "skipped", "无 Tushare 源"
            return result
        missing = self._missing_dates("ml_margin", start, end)
        total = 0
        failed: list[str] = []
        for day in missing:
            try:
                rows = tushare.margin_daily(day)
            except Exception as exc:  # noqa: BLE001
                failed.append(day)
                logger.info("margin_detail %s 失败：%s", day, brief(exc, BRIEF_TIGHT))
                continue
            if not rows:
                failed.append(day)
                continue
            total += self._write(
                "INSERT INTO ml_margin(code, trade_date, rzye, rqye, net_buy)"
                " VALUES(?,?,?,?,?) ON CONFLICT(code, trade_date) DO UPDATE SET"
                " rzye=excluded.rzye, rqye=excluded.rqye,"
                " net_buy=excluded.net_buy",
                [(item.code, item.date, item.rzye, item.rqye, item.net_buy)
                 for item in rows])
        result.rows = total
        result.missing = sorted(set(failed))
        result.status = "ok" if not result.missing else (
            "partial" if total else "failed")
        result.message = f"{len(missing)} 个交易日待同步"
        result.seconds = time.monotonic() - started
        self._log_sync(result, (start, end))
        return result

    # ---------- 北向持股 ----------

    def northbound_series(self, codes: Sequence[str], *, start: str, end: str
                          ) -> dict[str, list[tuple[str, float]]]:
        """读北向持股数量序列 `{code: [(date, vol)]}`（只读本地）。"""
        if not codes:
            return {}
        marks = ",".join("?" for _ in codes)
        rows = self._read(
            f"SELECT code, trade_date, vol FROM ml_northbound"
            f" WHERE code IN ({marks}) AND trade_date BETWEEN ? AND ?"
            " ORDER BY code, trade_date", (*codes, start, end))
        out: dict[str, list[tuple[str, float]]] = {}
        for row in rows:
            out.setdefault(str(row["code"]), []).append(
                (str(row["trade_date"]), _f(row["vol"])))
        return out

    def northbound_all(self, *, start: str, end: str) -> list[dict[str, Any]]:
        """读区间内全部北向持股行（回测按日聚合成板块级用）。"""
        rows = self._read(
            "SELECT code, trade_date, vol FROM ml_northbound"
            " WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date, code",
            (start, end))
        return [{"code": str(row["code"]), "trade_date": str(row["trade_date"]),
                 "vol": _f(row["vol"])} for row in rows]

    def sync_northbound(self, *, start: str, end: str) -> SyncResult:
        """同步个股级北向持股（`hk_hold`，一次一天拿全市场）。

        ## 数据现实与降级语义（2026-09 实测，写在这里避免后人误判）

        - **2022-01 ~ 2024-08-16**：日频个股持股，单日 3000~4100 行 → 正常同步；
        - **2024-08-16 之后**：交易所只在**季末**披露快照，非季末日的
          `hk_hold(trade_date=...)` 会返回**港股**（不是空）。`TushareSource`
          已按 `.SH/.SZ/.BJ` 过滤，因此这里拿到空列表 = "这天没有个股级数据"；
        - 空列表**记成 gap 而不是 failed**：把"披露规则变了"当成"同步失败"，
          会让台账天天报红，真正的失败反而被淹没。

        第二层「北向资金」维度据此自动降级（季度快照 → 只能判季度变化），
        并在打分结果的 `gaps` 里如实标注。
        """
        started = time.monotonic()
        result = SyncResult(dataset="northbound")
        tushare = self.tushare
        if tushare is None:
            result.status, result.message = "skipped", "无 Tushare 源"
            return result
        missing = self._missing_dates("ml_northbound", start, end)
        total = 0
        empty: list[str] = []
        failed: list[str] = []
        for day in missing:
            try:
                rows = tushare.northbound_holdings(day)
            except Exception as exc:  # noqa: BLE001
                failed.append(day)
                logger.info("hk_hold %s 失败：%s", day, brief(exc, BRIEF_TIGHT))
                continue
            if not rows:
                empty.append(day)
                continue
            total += self._write(
                "INSERT INTO ml_northbound(code, trade_date, vol, ratio, source)"
                " VALUES(?,?,?,?,?) ON CONFLICT(code, trade_date) DO UPDATE SET"
                " vol=excluded.vol, ratio=excluded.ratio, source=excluded.source",
                [(code, day, volume, ratio, "tushare:hk_hold")
                 for code, volume, ratio in rows])
            # 当天有数据就写一行台账：`_missing_dates` 只看 ml_northbound，
            # 有行即视为已同步，因此无需为"已完成"再单独记一笔。
        result.rows = total
        result.missing = sorted(set(empty))
        if failed:
            result.status = "partial" if total else "failed"
            result.message = f"{len(failed)} 天调用失败"
        elif empty:
            # 有数据 + 有空档：空档属于披露规则变化，标 partial 但在 message 里说清
            result.status = "partial" if total else "ok"
            result.message = (f"{len(empty)} 天无个股级披露"
                              "（2024-08 起仅季末快照，属预期）")
        result.seconds = time.monotonic() - started
        self._log_sync(result, (start, end))
        return result

    # ---------- 股东户数 ----------

    def holder_rows(self, codes: Sequence[str] | None = None) -> list[HolderRow]:
        rows = self._read("SELECT * FROM ml_holder ORDER BY code, end_date")
        wanted = {str(item) for item in codes} if codes else None
        out: list[HolderRow] = []
        for row in rows:
            code = str(row["code"])
            if wanted is not None and code not in wanted:
                continue
            out.append(HolderRow(code=code, end_date=str(row["end_date"]),
                                 ann_date=str(row["ann_date"]),
                                 holder_num=_opt(row["holder_num"])))
        # 环比变化率在读取时现算（存的是原始户数，避免历史行被改写）
        by_code: dict[str, float | None] = {}
        for item in out:
            previous = by_code.get(item.code)
            if item.holder_num is not None and previous not in (None, 0):
                item.change_ratio = item.holder_num / previous - 1.0
            by_code[item.code] = item.holder_num
        return out

    def sync_holders(self) -> SyncResult:
        """同步股东户数。

        ⚠️ `stk_holdernumber` **不支持按报告期取全市场**（实测 `period` 与
        `start_date/end_date` 都被忽略，一次只返回约 5500 行"最新公告"）。
        因此这里是**滚动累积**：每次同步把当前快照并进本地表，
        跑得越久历史越完整。回测期内如果表是空的，第一层 chips 维度
        会如实记 gap，而不是拿 0 冒充。
        """
        started = time.monotonic()
        result = SyncResult(dataset="holder")
        tushare = self.tushare
        if tushare is None:
            result.status, result.message = "skipped", "无 Tushare 源"
            return result
        try:
            rows = tushare.holder_number([])
        except Exception as exc:  # noqa: BLE001
            result.status, result.message = "failed", brief(exc, BRIEF_TIGHT)
            result.seconds = time.monotonic() - started
            self._log_sync(result, ("", ""))
            return result
        result.rows = self._write(
            "INSERT INTO ml_holder(code, end_date, ann_date, holder_num)"
            " VALUES(?,?,?,?) ON CONFLICT(code, end_date) DO UPDATE SET"
            " ann_date=excluded.ann_date, holder_num=excluded.holder_num",
            [(item.code, item.end_date, item.ann_date, item.holder_num)
             for item in rows if item.end_date])
        if not result.rows:
            result.status, result.message = "partial", "上游未返回任何记录"
        result.seconds = time.monotonic() - started
        self._log_sync(result, ("", ""))
        return result

    # ---------- 龙虎榜席位 ----------

    def seats(self, *, start: str, end: str,
              codes: Sequence[str] | None = None) -> list[SeatRow]:
        rows = self._read(
            "SELECT * FROM ml_seat WHERE trade_date BETWEEN ? AND ?"
            " ORDER BY trade_date, code", (start, end))
        wanted = {str(item) for item in codes} if codes else None
        out: list[SeatRow] = []
        for row in rows:
            code = str(row["code"])
            if wanted is not None and code not in wanted:
                continue
            out.append(SeatRow(
                date=str(row["trade_date"]), code=code,
                exalter=str(row["exalter"]), buy=_f(row["buy"]),
                sell=_f(row["sell"]), net_buy=_f(row["net_buy"]),
                side=str(row["side"]), reason=str(row["reason"])))
        return out

    def sync_seats(self, *, start: str, end: str) -> SyncResult:
        """同步龙虎榜机构/营业部席位（逐交易日）。"""
        started = time.monotonic()
        result = SyncResult(dataset="seat")
        tushare = self.tushare
        if tushare is None:
            result.status, result.message = "skipped", "无 Tushare 源"
            return result
        missing = self._missing_dates("ml_seat", start, end)
        total = 0
        failed: list[str] = []
        for day in missing:
            try:
                rows = tushare.seat_rows(day)
            except Exception as exc:  # noqa: BLE001
                failed.append(day)
                logger.info("top_inst %s 失败：%s", day, brief(exc, BRIEF_TIGHT))
                continue
            if not rows:
                # 空是**正常**的（当天没有个股上榜），不该记成失败
                self._write("INSERT INTO ml_sync(dataset, span_start, span_end,"
                            " rows, status, message, seconds, updated_at)"
                            " VALUES('seat_day', ?, ?, 0, 'ok', '', 0, ?)"
                            " ON CONFLICT(dataset, span_start, span_end)"
                            " DO UPDATE SET updated_at=excluded.updated_at",
                            (day, day, _now()))
                continue
            total += self._write(
                "INSERT INTO ml_seat(trade_date, code, exalter, side, buy, sell,"
                " net_buy, reason) VALUES(?,?,?,?,?,?,?,?)"
                " ON CONFLICT(trade_date, code, exalter, side) DO UPDATE SET"
                " buy=excluded.buy, sell=excluded.sell,"
                " net_buy=excluded.net_buy, reason=excluded.reason",
                [(item.date, item.code, item.exalter, item.side, item.buy,
                  item.sell, item.net_buy, item.reason) for item in rows])
        result.rows = total
        result.missing = sorted(set(failed))
        result.status = "ok" if not result.missing else (
            "partial" if total else "failed")
        result.message = f"{len(missing)} 个交易日待同步"
        result.seconds = time.monotonic() - started
        self._log_sync(result, (start, end))
        return result

    # ---------- 复用板块拥挤度模块的板块池与成分股 ----------

    def import_crowding_pool(self, *, visible_only: bool = True,
                             source_db: str = "") -> SyncResult:
        """把**板块拥挤度模块**已经维护好的板块池导入 `ml_board`。

        ## 为什么要复用而不是自己再拉一遍

        拥挤度模块（`sector_crowding`）已经把同花顺 2517 个指数整理成
        `sector_crowding_list`：哪些可见、哪些隐藏、哪些人工置顶，
        并落了 `sector_meta`（板块类型/首末交易日/bar 数）与 `sector_member`
        （36.9 万条成分股映射）。使用者在那套界面上勾选/隐藏板块的**研究判断**
        就存在这张表里 —— 主线挖掘再自己拉一份 `ths_index` 只会得到两个
        慢慢漂移的"板块池"，而且使用者在拥挤度里做的增删在主线上不生效。

        实测（2026-09）：`visible=1` 的板块 **445 个**，正是需求说的"四百多个"。

        ## 选池规则：显式黑名单优先，`visible` 只是回退

        板块池 = `sector_crowding_list` 全部板块 **减去黑名单**（再减去名称规则
        排除项）。今天 933 - 488 = 445，与 `visible=1` 完全一致。

        为什么不用 `WHERE visible = 1`：那样将来新增的板块若默认 `visible=0`
        会被**静默漏掉且不报错**，而新概念恰恰是最该跟踪的对象。黑名单是
        **冻结**的显式清单（`configs/sector_blacklist.yaml`），新板块不在其中，
        因此自动入池。黑名单文件读不到时回退到 `visible = 1` 并在 message 里
        说明 —— 回退会让入池数量突变，必须让人看得见。

        `source_db` 为空时用 `settings.sqlite_path`（与拥挤度同库）。
        """
        started = time.monotonic()
        result = SyncResult(dataset="board_crowding")
        path = source_db or self._settings_sqlite_path()
        if not path or not Path(path).exists():
            result.status = "skipped"
            result.message = f"拥挤度库不存在：{path or '(未配置)'}"
            return result
        blacklist = load_sector_blacklist(
            getattr(self.config.universe, "blacklist_file",
                    "sector_blacklist.yaml"))
        try:
            with sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro",
                                 uri=True) as connection:
                connection.row_factory = sqlite3.Row
                if blacklist is None:
                    # 回退：黑名单不可用时保持旧行为，避免池子无声地变大
                    where = " WHERE visible = 1" if visible_only else ""
                else:
                    where = ""
                rows = connection.execute(
                    "SELECT sector_code, sector_name, visible"
                    " FROM sector_crowding_list" + where).fetchall()
        except sqlite3.Error as exc:
            result.status, result.message = "failed", brief(exc, BRIEF_TIGHT)
            result.seconds = time.monotonic() - started
            self._log_sync(result, ("", ""))
            return result
        now = _now()
        universe = self.config.universe
        # 排掉不含题材含义的策略 / 风格 / 行业 / 地区指数（规则与拥挤度一致）：
        # `visible=1` 里仍混着「同花顺金仓100」「同花顺漂移100」这类指数，
        # 它们不是概念，行情与宽基几乎重合，进榜单会长期占据头部。
        kept_rows: list[tuple] = []
        excluded: list[str] = []
        blocked: list[str] = []
        non_concept: list[str] = []
        for row in rows:
            code = str(row["sector_code"] or "")
            name = str(row["sector_name"] or "")
            if not code:
                continue
            if blacklist is not None and code in blacklist:
                # 被有意排除的板块：既不入池也不同步行情/资金流。
                # 单独计数而不是并进 excluded —— 两者的成因不同，
                # 「被黑名单挡掉多少」是这份清单是否生效的直接证据。
                blocked.append(code)
                continue
            if universe.is_excluded(name):
                excluded.append(name)
                continue
            if universe.is_excluded_code(code):
                # 非 A 股概念：GICS 式行业分类（871xxx）与地域/统计板块。
                # 单独一类而不是并进 excluded —— 成因不同，
                # 「按代码剔了多少」是这份清单是否生效的直接证据。
                # ⚠️ 只影响分析池，不停数据更新（地域板块在拥挤度界面仍可查看）。
                non_concept.append(code)
                continue
            kept_rows.append((code, name, BoardKind.CONCEPT.value, 0,
                              "sector_crowding:list", "", now))
        result.rows = self._write(
            "INSERT INTO ml_board(code, name, kind, members, source, list_date,"
            " updated_at) VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(code) DO UPDATE SET name=excluded.name,"
            " kind=excluded.kind,"
            # ⚠️ **source 只写不改**：一个板块可能同时来自"拥挤度池"与
            # "同花顺概念名录"。若这里无条件覆盖成 `sector_crowding:list`，
            # 之后再按来源清理时就分不清谁是谁了（实测踩过）。
            " source=CASE WHEN ml_board.source = '' THEN excluded.source"
            "               ELSE ml_board.source END,"
            " updated_at=excluded.updated_at", kept_rows)
        # ⚠️ 只清理**自己这个来源**的旧行（`source` 已改成"只写不改"，
        # 所以拥挤度池的成员会保留 `sector_crowding:list` 这个标记）。
        #
        # 这里**刻意不删除**其他来源的板块行（同花顺概念名录 1800+ 个）：
        # 那批板块有完整的 `ml_member` / `ml_board_bar` 数据，删掉是不可逆的，
        # 而且会让 `relevance` 的题材→板块映射塌掉（要重跑 LLM 打分才能恢复）。
        # 实测这个 bug 踩了两次，详见 `_prune_boards` 的 docstring。
        #
        # 原来删它们的目的是"防止自建 ths_index 名录的两千多个板块混进榜单
        # （同花顺A50 / A50全收益）"—— 这个问题现在由 `relevance` 的假板块规则
        # 解决（`同花顺*`、`样本股`、`等权` 等一律排除），不需要靠删数据。
        removed = self._prune_boards({row[0] for row in kept_rows},
                                     source="sector_crowding:list")
        mode = ("黑名单" if blacklist is not None
                else f"回退 visible_only={visible_only}")
        if not result.rows:
            result.status, result.message = "partial", "拥挤度板块池为空"
        else:
            result.message = (
                f"复用拥挤度板块池（{mode}）：{result.rows} 个入池"
                + (f"，黑名单挡掉 {len(blocked)} 个" if blocked else "")
                + (f"，排除 {len(excluded)} 个非概念指数" if excluded else "")
                + (f"，按代码剔除 {len(non_concept)} 个 GICS/地域/统计板块"
                   if non_concept else "")
                + (f"，清理 {removed} 个不在池内的旧板块" if removed else ""))
        result.seconds = time.monotonic() - started
        self._log_sync(result, ("", ""))
        if excluded:
            logger.info("主线标的池排除的非概念板块：%s", "、".join(excluded[:8]))
        if non_concept:
            logger.info("主线标的池按代码剔除的板块：%s",
                        "、".join(non_concept[:8]))
        return result

    def import_crowding_members(self, source_db: str = "") -> SyncResult:
        """把拥挤度的 `sector_member`（36.9 万条）导入 `ml_member`。

        比逐个板块调 `ths_member` 快几个数量级（零 API 调用），
        而且与拥挤度界面里看到的成分股**是同一份**。
        """
        started = time.monotonic()
        result = SyncResult(dataset="member_crowding")
        path = source_db or self._settings_sqlite_path()
        if not path or not Path(path).exists():
            result.status = "skipped"
            result.message = f"拥挤度库不存在：{path or '(未配置)'}"
            return result
        try:
            with sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro",
                                 uri=True) as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    "SELECT sector_code, stock_code, stock_name"
                    " FROM sector_member").fetchall()
        except sqlite3.Error as exc:
            result.status, result.message = "failed", brief(exc, BRIEF_TIGHT)
            result.seconds = time.monotonic() - started
            self._log_sync(result, ("", ""))
            return result
        now = _now()
        total = 0
        step = 20000          # 分批写：36 万行一次 executemany 会长时间持锁
        for offset in range(0, len(rows), step):
            chunk = rows[offset:offset + step]
            total += self._write(
                "INSERT INTO ml_member(board_code, code, name, in_date,"
                " out_date, source, updated_at) VALUES(?,?,?,?,?,?,?)"
                " ON CONFLICT(board_code, code) DO UPDATE SET"
                " name=excluded.name, source=excluded.source,"
                " updated_at=excluded.updated_at",
                [(str(row["sector_code"]), str(row["stock_code"]).zfill(6),
                  str(row["stock_name"] or ""), "", "",
                  "sector_crowding:member", now) for row in chunk
                 if str(row["sector_code"] or "") and str(row["stock_code"] or "")])
        result.rows = total
        if not total:
            result.status, result.message = "partial", "拥挤度成分股为空"
        result.seconds = time.monotonic() - started
        self._log_sync(result, ("", ""))
        return result

    @staticmethod
    def _settings_sqlite_path() -> str:
        """取 `settings.sqlite_path`（拥挤度与主线同库）。取不到返回空串。"""
        try:
            from src.core.config import get_settings

            return str(getattr(get_settings(), "sqlite_path", "") or "")
        except Exception as exc:  # noqa: BLE001 配置不可用时按"没有库"处理
            logger.info("读取 sqlite_path 失败：%s", brief(exc, BRIEF_TIGHT))
            return ""

    # ---------- ETF（第二层 etf 维度） ----------

    def etf_bars(self, codes: Sequence[str], *, start: str, end: str
                 ) -> dict[str, list[dict[str, float]]]:
        """读 ETF 日线 + 份额 `{code: [{trade_date, close, amount, shares, name}]}`。

        份额是**存量**：调用方算"净申购"时必须自己取相邻两日之差。
        """
        if not codes:
            return {}
        marks = ",".join("?" for _ in codes)
        rows = self._read(
            f"SELECT * FROM ml_etf WHERE code IN ({marks})"
            " AND trade_date BETWEEN ? AND ? ORDER BY code, trade_date",
            (*codes, start, end))
        out: dict[str, list[dict[str, float]]] = {}
        for row in rows:
            out.setdefault(str(row["code"]), []).append({
                "trade_date": str(row["trade_date"]),
                "close": _f(row["close"]), "amount": _f(row["amount"]),
                "volume": _f(row["volume"]), "shares": _opt(row["shares"]),
                "name": str(row["name"] or "")})
        return out

    def etf_codes(self) -> list[str]:
        """本地已有行情的 ETF 代码（升序）。"""
        rows = self._read("SELECT DISTINCT code FROM ml_etf ORDER BY code")
        return [str(row["code"]) for row in rows]

    def sync_etf_meta(self) -> SyncResult:
        """同步 ETF 名录（`fund_basic(market='E')`），落 `ml_etf_meta`。"""
        started = time.monotonic()
        result = SyncResult(dataset="etf_meta")
        tushare = self.tushare
        if tushare is None:
            result.status, result.message = "skipped", "无 Tushare 源"
            return result
        try:
            frame = tushare.client.call("fund_basic", market="E")
        except Exception as exc:  # noqa: BLE001
            result.status, result.message = "failed", brief(exc, BRIEF_TIGHT)
            result.seconds = time.monotonic() - started
            self._log_sync(result, ("", ""))
            return result
        now = _now()
        rows = [(_s(row.get("ts_code")), _s(row.get("name")),
                 _s(row.get("benchmark")), "", now)
                for row in (frame.to_dict("records")
                            if frame is not None else [])
                if _s(row.get("ts_code"))]
        result.rows = self._write(
            "INSERT INTO ml_etf_meta(code, name, benchmark, board_name,"
            " updated_at) VALUES(?,?,?,?,?)"
            " ON CONFLICT(code) DO UPDATE SET name=excluded.name,"
            " benchmark=excluded.benchmark, updated_at=excluded.updated_at", rows)
        if not result.rows:
            result.status, result.message = "partial", "名录为空"
        result.seconds = time.monotonic() - started
        self._log_sync(result, ("", ""))
        return result

    def etf_meta(self) -> dict[str, dict[str, str]]:
        rows = self._read("SELECT * FROM ml_etf_meta")
        return {str(row["code"]): {"name": str(row["name"] or ""),
                                   "benchmark": str(row["benchmark"] or ""),
                                   "board_name": str(row["board_name"] or "")}
                for row in rows}

    # ---------- 宽基指数（ETF 份额监控用） ----------

    def index_bars(self, codes: Sequence[str], *, start: str, end: str
                   ) -> dict[str, list[tuple[str, float]]]:
        """读指数日线 `{code: [(date, close)]}`（升序）。"""
        if not codes:
            return {}
        marks = ",".join("?" for _ in codes)
        rows = self._read(
            f"SELECT code, trade_date, close FROM ml_index"
            f" WHERE code IN ({marks}) AND trade_date BETWEEN ? AND ?"
            " ORDER BY code, trade_date", (*codes, start, end))
        out: dict[str, list[tuple[str, float]]] = {}
        for row in rows:
            out.setdefault(str(row["code"]), []).append(
                (str(row["trade_date"]), _f(row["close"])))
        return out

    def sync_index_bars(self, codes: Sequence[str], *, start: str, end: str,
                        chunk_years: float = 3.0) -> SyncResult:
        """同步宽基指数日线（`index_daily`）。

        **按年份切片**：单次返回有行数上限，一次拉 8 年会静默截断 ——
        而截断后回测区间"看起来就是那么长"，不会报错。
        """
        started = time.monotonic()
        result = SyncResult(dataset="index")
        tushare = self.tushare
        if tushare is None:
            result.status, result.message = "skipped", "无 Tushare 源"
            return result
        total = 0
        failed: list[str] = []
        for code in codes:
            for span_start, span_end in _year_spans(start, end, chunk_years):
                try:
                    frame = tushare.index_series(code, start=span_start,
                                                 end=span_end)
                except Exception as exc:  # noqa: BLE001
                    failed.append(f"{code}@{span_start}")
                    logger.info("index_daily %s 失败：%s", code,
                                brief(exc, BRIEF_TIGHT))
                    continue
                bars = getattr(frame, "bars", []) or []
                if not bars:
                    continue
                total += self._write(
                    "INSERT INTO ml_index(code, trade_date, close, pct_chg,"
                    " amount) VALUES(?,?,?,?,?)"
                    " ON CONFLICT(code, trade_date) DO UPDATE SET"
                    " close=excluded.close, pct_chg=excluded.pct_chg,"
                    " amount=excluded.amount",
                    [(code, bar.date, bar.close, bar.pct_change, bar.amount)
                     for bar in bars if bar.date])
        result.rows = total
        result.missing = failed
        result.status = "ok" if not failed else ("partial" if total else "failed")
        result.seconds = time.monotonic() - started
        self._log_sync(result, (start, end))
        return result

    def sync_etf(self, *, start: str, end: str,
                 codes: Sequence[str] | None = None,
                 chunk_years: float = 0.0) -> SyncResult:
        """同步 ETF 日线与份额。

        `codes` 为空时只同步**最近一个交易日**的全市场快照
        （`fund_daily(trade_date)` + `fund_share(trade_date)` 各一次调用）——
        这是每日盘后增量；给 `codes` 时按代码逐只补历史区间。

        ## 两个必须分区间拉取的理由

        ⚠️ **`fund_share` 单次最多返回 2000 行**（实测：`510300.SH` 从 2018-01-01
        请求只回到 2018-07-10）。要 2018 至今的完整历史必须按 `chunk_years`
        切片，否则最早的一段被**静默丢掉**，而回测区间看起来"就是那么长"。

        ⚠️ `fund_daily` 一次只返回**一个交易日**（与 `fut_daily` 不同），
        因此历史回填必须按代码逐只取区间，而不是按日期区间批量。
        """
        started = time.monotonic()
        result = SyncResult(dataset="etf")
        tushare = self.tushare
        if tushare is None:
            result.status, result.message = "skipped", "无 Tushare 源"
            return result
        total = 0
        failed: list[str] = []
        if not codes:
            day = self.calendar(start, end)[-1] if self.calendar(start, end) else end
            try:
                bars = tushare.client.call("fund_daily", trade_date=day)
                shares = tushare.client.call("fund_share", trade_date=day)
            except Exception as exc:  # noqa: BLE001
                result.status, result.message = "failed", brief(exc, BRIEF_TIGHT)
                result.seconds = time.monotonic() - started
                self._log_sync(result, (day, day))
                return result
            total += self._write_etf_rows(bars, shares, day)
        else:
            spans = (_year_spans(start, end, chunk_years) if chunk_years > 0
                     else [(start, end)])
            for code in codes:
                bars_frames: list[Any] = []
                share_map: dict[tuple[str, str], Any] = {}
                for span_start, span_end in spans:
                    try:
                        frame = tushare.client.call(
                            "fund_daily", ts_code=code,
                            start_date=span_start, end_date=span_end)
                        if frame is not None and len(frame):
                            bars_frames.append(frame)
                        share = tushare.client.call(
                            "fund_share", ts_code=code,
                            start_date=span_start, end_date=span_end)
                    except Exception as exc:  # noqa: BLE001
                        failed.append(f"{code}@{span_start}")
                        logger.info("ETF %s 同步失败：%s", code,
                                    brief(exc, BRIEF_TIGHT))
                        continue
                    for row in (share.to_dict("records")
                                if share is not None else []):
                        share_map[(code, str(row.get("trade_date")))] = \
                            row.get("fd_share")
                for frame in bars_frames:
                    total += self._write_etf_rows(frame, None, "",
                                                  share_map)
        result.rows = total
        result.missing = failed
        result.status = "ok" if not failed else ("partial" if total else "failed")
        result.seconds = time.monotonic() - started
        self._log_sync(result, (start, end))
        return result

    def _write_etf_rows(self, bars: Any, shares: Any, day: str,
                        share_map: dict[tuple[str, str], Any] | None = None
                        ) -> int:
        """把 `fund_daily`（+ `fund_share`）写成 `ml_etf` 行。

        ⚠️ 份额映射的键必须是 **`(ts_code, trade_date)` 二元组**。
        早先全市场快照按 `ts_code` 存、单只回填按 `trade_date` 存，而查表一律
        按 `ts_code` —— 于是**逐只回填的那条路径永远取不到份额**，
        `shares` 全为 NULL，"净申购"这个子因子整体失效。
        这种 bug 不会报错：份额列安静地是空的，而份额为空的维度会被
        `available=False` 剔除，最终表现只是"ETF 维度分数偏低"。
        """
        mapping = share_map
        if mapping is None:
            mapping = {}
            for row in (shares.to_dict("records") if shares is not None else []):
                code = _s(row.get("ts_code"))
                stamp = _s(row.get("trade_date")) or day
                mapping[(code, stamp)] = row.get("fd_share")
        if bars is None:
            return 0
        rows = []
        for row in bars.to_dict("records"):
            code = _s(row.get("ts_code"))
            if not code:
                continue
            stamp = _s(row.get("trade_date")) or day
            rows.append((code, stamp, self._etf_name(code),
                         _f(row.get("close")), _f(row.get("pct_chg")),
                         _f(row.get("vol")),
                         # ⚠️ `fund_daily.amount` 的口径是**千元**（与 `daily.amount`
                         # 同一约定），而本表统一存**元**。不换算的话下游按元设的
                         # 成交额门槛会高出 1000 倍 —— 实测这会把 37 只 ETF 里的
                         # 34 只全部过滤掉，而现象只是"没有 ETF 信号"。
                         _f(row.get("amount")) * 1000.0,
                         _opt(mapping.get((code, stamp)))))
        return self._write(
            "INSERT INTO ml_etf(code, trade_date, name, close, pct_chg, volume,"
            " amount, shares) VALUES(?,?,?,?,?,?,?,?)"
            " ON CONFLICT(code, trade_date) DO UPDATE SET name=excluded.name,"
            " close=excluded.close, pct_chg=excluded.pct_chg,"
            " volume=excluded.volume, amount=excluded.amount,"
            # shares 只在拿到值时覆盖：`fund_share` 偶发缺行时，
            # 用 NULL 覆盖会把已有的份额抹掉，进而让"净申购"永远算不出来。
            " shares=CASE WHEN excluded.shares IS NOT NULL"
            "             THEN excluded.shares ELSE ml_etf.shares END", rows)

    def _etf_name(self, code: str) -> str:
        rows = self._read("SELECT name FROM ml_etf_meta WHERE code = ?", (code,))
        return str(rows[0]["name"] or "") if rows else ""

    def sync_etf_shares(self, *, trade_date: str,
                        codes: Sequence[str] | None = None) -> SyncResult:
        """**只补份额**：给 `trade_date` 那些 `shares IS NULL` 的行回填 `fund_share`。

        ## 为什么必须单独有这么一条路径（2026-09-22 实测事故）

        份额是**次日 8:30 左右**才发布的（交易所口径），而日行情当晚就有。于是
        「当晚跑一次全市场快照同步」必然给最新交易日写入一批 `shares = NULL` 的行
        —— 2026-09-21 17:55 实测：`ml_etf` 当天落了 2138 行、只有 765 行有份额，
        9 只被监控的宽基里有 7 只中招。

        `_write_etf_rows` 里"NULL 不覆盖旧值"的保护（`CASE WHEN excluded.shares
        IS NOT NULL`）对此**完全无效**：那一行是当晚新插入的，根本没有旧值可保护。
        而 `etf_flow.build_snapshot` 以 `MAX(trade_date)` 为锚点、只读**最后一行**的
        份额，于是那 7 只的份额、1/5/10/20 日变化率、对应指数分位**一起变成空**，
        面板上表现为"核心宽基 ETF 份额没数据"。

        修法刻意选在数据层而不是展示层：
        * 只 `UPDATE ... WHERE shares IS NULL` —— 已经有值的份额一个都不动，
          所以这个方法是**幂等且不可破坏**的（重复调用只会把剩下的 NULL 补掉）；
        * **不插入任何新行** —— 没有 `fund_share` 记录的代码保持 NULL，
          而不是被写成一行"行情有、份额无"的新脏数据（这正是当初出事的机制）。

        ## 两条取数路径

        1. `fund_share(trade_date=day)` 一次拿全市场（实测 1762 行），一条 SQL 批量回填；
        2. 仍有 NULL 时按代码逐只 `fund_share(ts_code=..., start_date=end_date=day)`
           兜底 —— 两步的入参形状不同（这与 `sync_etf` 里那个"映射键必须是
           `(ts_code, trade_date)` 二元组"的坑是同一件事，见 `_write_etf_rows`）。

        `codes` 只影响**兜底路径**取哪些代码（例如只关心观测清单里的 9 只）；
        传空 = 全表扫。份额本来就还没发布的那些代码（`158008.OF` 这类场外基金）
        会一直留 NULL，这是**正确**的，不是缺口。
        """
        started = time.monotonic()
        result = SyncResult(dataset="etf_share")
        day = _s(trade_date)
        if not day:
            result.status, result.message = "skipped", "未给交易日"
            return result
        tushare = self.tushare
        if tushare is None:
            result.status, result.message = "skipped", "无 Tushare 源"
            return result
        self.ensure_schema()

        # ---- ① 全市场快照：一次调用 + 一条批量 UPDATE ----
        mapping: dict[str, Any] = {}
        types: dict[str, str] = {}
        try:
            frame = tushare.client.call("fund_share", trade_date=day)
        except Exception as exc:  # noqa: BLE001 接口不可用只降级，不拦启动
            result.status, result.message = "failed", brief(exc, BRIEF_TIGHT)
            result.seconds = time.monotonic() - started
            self._log_sync(result, (day, day))
            return result
        for row in (frame.to_dict("records") if frame is not None else []):
            code = _s(row.get("ts_code"))
            if not code:
                continue
            types[code] = _s(row.get("fund_type"), "ETF").upper()
            if row.get("fd_share") is not None:
                mapping[code] = row.get("fd_share")
        total = self._write(
            "UPDATE ml_etf SET shares=? WHERE code=? AND trade_date=?"
            " AND shares IS NULL",
            [(value, code, day) for code, value in mapping.items()])

        # ---- ② 兜底：仍为 NULL 的**场内 ETF** 逐只再问一次 ----
        #
        # ⚠️ 只重试接口**明确标为 ETF** 的代码，不重试 `158008.OF` 这类场外基金：
        # 接口这一轮没给它们份额，说明份额本来就还没发布，逐只再问一遍是**纯浪费**
        # —— 实测未发布那轮缺 1373 只，在 5000 积分档的频次限制下足以把整个启动
        # 自检拖垮，而结果一定是 0 行。
        #
        # ⚠️ 判据是 `== "ETF"`，**不能用 `types.get(code, "ETF")`**：那样"接口压根
        # 没提过这只代码"会被当成 ETF 去重试，恰好把要避开的那一批又捞回来。
        # 接口没回 `fund_type` 列（拿不到类型信息）时才退回"全都重试"——
        # 宁可多打几次，也不能漏补。
        missing = self.etf_missing_share_codes(day, codes=codes)
        retry = ([code for code in missing if types.get(code) == "ETF"]
                 if types else missing)
        failed: list[str] = []
        for code in retry:
            try:
                frame = tushare.client.call(
                    "fund_share", ts_code=code,
                    start_date=day, end_date=day)
            except Exception as exc:  # noqa: BLE001 单只失败不影响其余
                failed.append(code)
                logger.info("ETF %s %s 份额补拉失败：%s", code, day,
                            brief(exc, BRIEF_TIGHT))
                continue
            for row in (frame.to_dict("records") if frame is not None else []):
                value = row.get("fd_share")
                if value is None:
                    continue
                total += self._write(
                    "UPDATE ml_etf SET shares=? WHERE code=? AND trade_date=?"
                    " AND shares IS NULL", [(value, code, day)])

        result.rows = total
        result.missing = failed
        if failed:
            result.status = "partial"
        still = self.etf_missing_share_codes(day, codes=codes)
        result.message = (f"{day} 份额回填 {total} 行（接口返回 {len(mapping)} 只，"
                          f"重试 {len(retry)} 只，仍缺 {len(still)} 行）")
        result.seconds = time.monotonic() - started
        self._log_sync(result, (day, day))
        return result

    def etf_missing_share_codes(self, trade_date: str,
                                codes: Sequence[str] | None = None) -> list[str]:
        """`trade_date` 当天 `shares IS NULL` 的代码（升序；`codes` 非空则只看这些）。

        与 `sync_etf_shares` 配套：一个问"缺哪些"，一个补"缺的值"。
        分开是为了让调用方（启动自检 / 接口）能在**不联网**的前提下先判断
        "到底有没有缺口"，避免每次启动都白打一轮接口。
        """
        day = _s(trade_date)
        if not day:
            return []
        params: list[Any] = [day]
        clause = ""
        if codes:
            marks = ",".join("?" for _ in codes)
            clause = f" AND code IN ({marks})"
            params.extend(str(code) for code in codes)
        rows = self._read(
            "SELECT code FROM ml_etf WHERE trade_date = ? AND shares IS NULL"
            f"{clause} ORDER BY code", params)
        return [str(row["code"]) for row in rows]

    def etf_share_coverage(self, trade_date: str = "",
                           codes: Sequence[str] | None = None) -> dict[str, Any]:
        """`trade_date`（空 = `ml_etf` 最新交易日）的份额覆盖度。

        返回 `{trade_date, rows, with_share, missing, share_ratio, codes}`。
        `rows == 0` 表示本地根本没有该交易日的行情 —— 这是"需要整段同步"，
        与"有行情但份额没发布"是两件不同的事，调用方必须分开处理。

        ⚠️ 键名是 `share_ratio`（**已覆盖**的比例）而不是 `ratio`：早先叫
        `ratio` 时，调用方按直觉把它当成"缺口比例"用，于是分母正确、分子反了，
        判据恒为"没缺口"—— 一个键名歧义导致自检永远不补数，且不报任何错。

        `codes` 非空时**只统计这些代码**：`ml_etf` 里混着 `158008.OF` 这类
        份额长期不发布的场外基金，全表口径永远带着十几%的"缺口"，
        按它设阈值会天天误报。判据要贴着"谁会被展示"来定。
        """
        day = _s(trade_date)
        if not day:
            rows = self._read("SELECT MAX(trade_date) AS d FROM ml_etf")
            day = _s(rows[0]["d"]) if rows else ""
        out: dict[str, Any] = {"trade_date": day, "rows": 0, "with_share": 0,
                               "missing": 0, "share_ratio": 0.0, "codes": []}
        if not day:
            return out
        params: list[Any] = [day]
        clause = ""
        if codes:
            marks = ",".join("?" for _ in codes)
            clause = f" AND code IN ({marks})"
            params.extend(str(code) for code in codes)
        rows = self._read(
            "SELECT COUNT(*) AS n, SUM(shares IS NOT NULL) AS k"
            f" FROM ml_etf WHERE trade_date = ?{clause}", params)
        if rows:
            total = int(rows[0]["n"] or 0)
            with_share = int(rows[0]["k"] or 0)
            out["rows"], out["with_share"] = total, with_share
            out["missing"] = max(total - with_share, 0)
            out["share_ratio"] = round(with_share / total, 4) if total else 0.0
        if out["missing"]:
            out["codes"] = self.etf_missing_share_codes(day, codes=codes)
        return out

    def sync_catchup(self, *, days: int = 10) -> SyncResult:
        """把本地 ETF 数据补到**最新已收盘交易日**（行情 + 份额一起）。

        与 `sync_etf_shares` 的分工：那个只管"已有行缺份额"，这个管"根本还
        没有这一天的行"。两者的触发条件不同（见 `etf_share_guard`），
        合并成一个方法会让"只 UPDATE、不插行"的幂等保证失效 ——
        那条保证是这个模块不可破坏的前提。

        `days` 是回看天数：取 10 天而不是 1 天，因为 `build_indicators` 要算
        1/5/10 日变化率，只补最后一天会得到"有份额但变化率全是空"的面板。
        """
        started = time.monotonic()
        tushare = self.tushare
        if tushare is None:
            result = SyncResult(dataset="etf_catchup", status="skipped",
                                message="无 Tushare 源")
            return result
        today = datetime.now().strftime("%Y%m%d")
        try:
            recent = self.calendar(_shift_days(today, -int(days) - 5) or today,
                                   today)
        except Exception:  # noqa: BLE001 日历不可用就退回"今天为止"整段
            recent = []
        end = recent[-1] if recent else today
        start = _shift_days(end, -int(days)) or today
        result = self.sync_etf(start=start, end=end)
        result.dataset = "etf_catchup"
        result.seconds = time.monotonic() - started
        return result

    # ---------- 宏观 ----------

    def macro(self, factor: str, *, start: str = "", end: str = "",
              limit: int = 120) -> list[tuple[str, float]]:
        """读一个宏观因子的序列 `[(period, value)]`（升序，取尾部 limit 期）。"""
        rows = self._read(
            "SELECT period, value FROM ml_macro WHERE factor = ?"
            " AND period >= ? AND period <= ? ORDER BY period",
            (factor, start or "000000", end or "999999"))
        out = [(str(row["period"]), _f(row["value"])) for row in rows
               if row["value"] is not None]
        return out[-limit:] if limit else out

    def macro_latest(self) -> dict[str, tuple[str, float]]:
        """每个因子的最新一期 `{factor: (period, value)}`。"""
        rows = self._read(
            "SELECT factor, period, value FROM ml_macro m WHERE period ="
            " (SELECT MAX(period) FROM ml_macro WHERE factor = m.factor)")
        return {str(row["factor"]): (str(row["period"]), _f(row["value"]))
                for row in rows if row["value"] is not None}

    def sync_macro(self) -> SyncResult:
        """同步宏观因子（PMI / M1 / M2 / 社融 / Shibor / CPI / PPI）。

        每个因子一次调用拿全历史，落 `ml_macro(factor, period, value)`。
        取不到的因子（如 PPI 权限不足）**不写空值**，只在 message 里报出来 ——
        写了空值下游就没法区分"这个因子是 0"和"这个因子没数据"。
        """
        started = time.monotonic()
        result = SyncResult(dataset="macro")
        tushare = self.tushare
        if tushare is None:
            result.status, result.message = "skipped", "无 Tushare 源"
            return result
        total = 0
        failed: list[str] = []
        for factor, loader in _MACRO_LOADERS.items():
            try:
                series = loader(tushare)
            except Exception as exc:  # noqa: BLE001
                failed.append(factor)
                logger.info("宏观因子 %s 失败：%s", factor, brief(exc, BRIEF_TIGHT))
                continue
            if not series:
                failed.append(factor)
                continue
            total += self._write(
                "INSERT INTO ml_macro(factor, period, value, updated_at)"
                " VALUES(?,?,?,?) ON CONFLICT(factor, period) DO UPDATE SET"
                " value=excluded.value, updated_at=excluded.updated_at",
                [(factor, period, value, _now())
                 for period, value in series if value is not None])
        result.rows = total
        result.missing = failed
        result.status = "ok" if not failed else ("partial" if total else "failed")
        if failed:
            result.message = f"未取到的因子：{','.join(failed)}"
        result.seconds = time.monotonic() - started
        self._log_sync(result, ("", ""))
        return result

    # ---------- 期货 ----------

    def future_bars(self, codes: Sequence[str], *, start: str, end: str
                    ) -> dict[str, list[dict[str, float]]]:
        """读期货主力连续日线 `{code: [{trade_date, close, oi, ...}]}`。"""
        if not codes:
            return {}
        marks = ",".join("?" for _ in codes)
        rows = self._read(
            f"SELECT * FROM ml_future WHERE code IN ({marks})"
            " AND trade_date BETWEEN ? AND ? ORDER BY code, trade_date",
            (*codes, start, end))
        out: dict[str, list[dict[str, float]]] = {}
        for row in rows:
            out.setdefault(str(row["code"]), []).append({
                "trade_date": str(row["trade_date"]), "open": _f(row["open"]),
                "high": _f(row["high"]), "low": _f(row["low"]),
                "close": _f(row["close"]), "settle": _f(row["settle"]),
                "pre_close": _f(row["pre_close"]), "volume": _f(row["volume"]),
                "amount": _f(row["amount"]), "oi": _f(row["oi"]),
                "oi_chg": _f(row["oi_chg"])})
        return out

    def future_meta(self) -> dict[str, dict[str, str]]:
        rows = self._read("SELECT * FROM ml_future_meta")
        return {str(row["code"]): {k: str(row[k] or "") for k in row.keys()}
                for row in rows}

    def sync_futures(self, *, start: str, end: str,
                     codes: Sequence[str] | None = None) -> SyncResult:
        """同步内盘期货主力连续日线（逐品种一次调用，`fut_daily` 支持区间）。"""
        started = time.monotonic()
        result = SyncResult(dataset="future")
        tushare = self.tushare
        if tushare is None:
            result.status, result.message = "skipped", "无 Tushare 源"
            return result
        wanted = list(codes) if codes else self._future_codes()
        if not wanted:
            result.status, result.message = "skipped", "没有待同步的期货品种"
            return result
        total = 0
        failed: list[str] = []
        for code in wanted:
            try:
                frame = tushare.future_series([code], start=start, end=end)
            except Exception as exc:  # noqa: BLE001
                failed.append(code)
                logger.info("fut_daily %s 失败：%s", code, brief(exc, BRIEF_TIGHT))
                continue
            series = frame.get(code) if frame else None
            if series is None or len(series) == 0:
                failed.append(code)
                continue
            rows = [(code, _s(row.get("trade_date")),
                     _f(row.get("open")), _f(row.get("high")),
                     _f(row.get("low")), _f(row.get("close")),
                     _f(row.get("settle")), _f(row.get("pre_close")),
                     _f(row.get("vol")), _f(row.get("amount")),
                     _f(row.get("oi")), _f(row.get("oi_chg")))
                    for row in series.to_dict("records")]
            total += self._write(
                "INSERT INTO ml_future(code, trade_date, open, high, low, close,"
                " settle, pre_close, volume, amount, oi, oi_chg)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(code, trade_date) DO UPDATE SET"
                " open=excluded.open, high=excluded.high, low=excluded.low,"
                " close=excluded.close, settle=excluded.settle,"
                " pre_close=excluded.pre_close, volume=excluded.volume,"
                " amount=excluded.amount, oi=excluded.oi,"
                " oi_chg=excluded.oi_chg", rows)
        result.rows = total
        result.missing = failed
        result.status = "ok" if not failed else ("partial" if total else "failed")
        result.seconds = time.monotonic() - started
        self._log_sync(result, (start, end))
        return result

    def sync_future_meta(self, kind: FutureKind = FutureKind.DOMESTIC
                         ) -> SyncResult:
        """同步内盘期货品种名录（`fut_basic` 的主力合约，落 `ml_future_meta`）。"""
        started = time.monotonic()
        result = SyncResult(dataset="future_meta")
        tushare = self.tushare
        if tushare is None:
            result.status, result.message = "skipped", "无 Tushare 源"
            return result
        now = _now()
        rows: list[tuple] = []
        failed: list[str] = []
        for exchange in self.config.futures.exchanges:
            try:
                frame = tushare.client.call("fut_basic", exchange=exchange,
                                            fut_type="1")
            except Exception as exc:  # noqa: BLE001
                failed.append(exchange)
                logger.info("fut_basic %s 失败：%s", exchange, brief(exc, BRIEF_TIGHT))
                continue
            for row in (frame.to_dict("records") if frame is not None else []):
                code = _s(row.get("ts_code"))
                if not code:
                    continue
                rows.append((code, _s(row.get("name")), _s(row.get("fut_code")),
                             exchange, kind.value, "tushare:fut_basic", now))
        result.rows = self._write(
            "INSERT INTO ml_future_meta(code, name, fut_code, exchange, kind,"
            " source, updated_at) VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(code) DO UPDATE SET name=excluded.name,"
            " fut_code=excluded.fut_code, exchange=excluded.exchange,"
            " kind=excluded.kind, source=excluded.source,"
            " updated_at=excluded.updated_at", rows)
        result.missing = failed
        result.status = "ok" if not failed else ("partial" if result.rows else "failed")
        result.seconds = time.monotonic() - started
        self._log_sync(result, ("", ""))
        return result

    def sync_future_curve(self, trade_date: str) -> SyncResult:
        """同步某日**全部合约**行情并折出近月/远月价（期限结构信号的数据源）。

        `fut_daily(trade_date=...)` 一次返回当日所有月份合约（约 1000 行），
        按 `fut_code`（品种代码，如 `RB`）分组后取交割月最小/最大的两个合约。
        一次调用一天 —— 因此**回测区间默认不做**（1150 天就是 1150 次调用），
        只在实盘按需增量补齐；缺数据时 `term_structure` 记 gap 而不是记 0。
        """
        started = time.monotonic()
        result = SyncResult(dataset="future_curve")
        tushare = self.tushare
        if tushare is None:
            result.status, result.message = "skipped", "无 Tushare 源"
            return result
        try:
            frame = tushare.future_daily_all(trade_date)
        except Exception as exc:  # noqa: BLE001
            result.status, result.message = "failed", brief(exc, BRIEF_TIGHT)
            result.seconds = time.monotonic() - started
            self._log_sync(result, (trade_date, trade_date))
            return result
        if frame is None or len(frame) == 0:
            result.status, result.message = "partial", "该日无期货合约行情"
            result.seconds = time.monotonic() - started
            self._log_sync(result, (trade_date, trade_date))
            return result
        # 按品种分组：`RB2601.SHF` → 品种 `RB`，交割月 `2601`
        buckets: dict[str, list[tuple[str, float, str]]] = {}
        for row in frame.to_dict("records"):
            ts_code = _s(row.get("ts_code"))
            if "." not in ts_code:
                continue
            symbol, exchange = ts_code.split(".", 1)
            digits = "".join(ch for ch in symbol if ch.isdigit())
            variety = "".join(ch for ch in symbol if ch.isalpha())
            settle = _opt(row.get("settle")) or _opt(row.get("close"))
            if not variety or not digits or settle is None:
                continue
            code = f"{variety}.{exchange}"
            buckets.setdefault(code, []).append((digits, settle, ts_code))
        rows: list[tuple] = []
        for code, items in buckets.items():
            if len(items) < 2:
                continue
            items.sort(key=lambda item: item[0])
            near, far = items[0], items[-1]
            if near[0] == far[0]:
                continue
            rows.append((code, trade_date, near[1], far[1], near[2], far[2]))
        result.rows = self._write(
            "INSERT INTO ml_future_curve(code, trade_date, near, far, near_code,"
            " far_code) VALUES(?,?,?,?,?,?)"
            " ON CONFLICT(code, trade_date) DO UPDATE SET near=excluded.near,"
            " far=excluded.far, near_code=excluded.near_code,"
            " far_code=excluded.far_code", rows)
        if not result.rows:
            result.status, result.message = "partial", "没有可配对月份的品种"
        result.seconds = time.monotonic() - started
        self._log_sync(result, (trade_date, trade_date))
        return result

    def upsert_future_meta(self, rows: Sequence[dict[str, Any]]) -> int:
        """外部（映射表 / 外盘名录）写入期货品种元数据。"""
        now = _now()
        return self._write(
            "INSERT INTO ml_future_meta(code, name, fut_code, exchange, kind,"
            " source, updated_at) VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(code) DO UPDATE SET name=excluded.name,"
            " fut_code=excluded.fut_code, exchange=excluded.exchange,"
            " kind=excluded.kind, source=excluded.source,"
            " updated_at=excluded.updated_at",
            [(_s(item.get("code")), _s(item.get("name")),
              _s(item.get("fut_code")), _s(item.get("exchange")),
              _s(item.get("kind"), "domestic"), _s(item.get("source")), now)
             for item in rows if _s(item.get("code"))])

    def _future_codes(self) -> list[str]:
        """本地已有行情 + 元数据里的品种代码（去重升序）。"""
        rows = self._read(
            "SELECT code FROM ml_future_meta"
            " UNION SELECT code FROM ml_future ORDER BY code")
        return [str(row["code"]) for row in rows]

    # ---------- 板块级序列（第二层 leverage / northbound 用） ----------

    def board_margin_series(self, members: Sequence[str], *, start: str, end: str,
                            chunk: int = 400) -> list[tuple[str, float]]:
        """板块融资余额序列 `[(date, Σrzye)]`（成分股聚合）。

        分块是因为 SQLite 的 `IN (...)` 参数有上限（默认 999），而宽基概念
        板块可以有 400 只成分股 —— 一次全塞进去在多数板块上没事，
        在宽基上会直接抛 "too many SQL variables"。
        """
        return self._sum_series("ml_margin", "rzye", members,
                                start=start, end=end, chunk=chunk)

    def board_northbound_series(self, members: Sequence[str], *, start: str,
                                end: str,
                                chunk: int = 400) -> list[tuple[str, float]]:
        """板块北向持股数量序列 `[(date, Σvol)]`（成分股聚合）。"""
        return self._sum_series("ml_northbound", "vol", members,
                                start=start, end=end, chunk=chunk)

    def _sum_series(self, table: str, column: str, codes: Sequence[str], *,
                    start: str, end: str, chunk: int) -> list[tuple[str, float]]:
        """按日期对一批代码求和（分块查询后在内存里合并）。"""
        wanted = [str(code).zfill(6) for code in codes if code]
        if not wanted:
            return []
        buckets: dict[str, float] = {}
        for offset in range(0, len(wanted), max(1, chunk)):
            group = wanted[offset:offset + max(1, chunk)]
            marks = ",".join("?" for _ in group)
            rows = self._read(
                f"SELECT trade_date, SUM({column}) AS total FROM {table}"
                f" WHERE code IN ({marks}) AND trade_date BETWEEN ? AND ?"
                " GROUP BY trade_date", (*group, start, end))
            for row in rows:
                day = str(row["trade_date"])
                buckets[day] = buckets.get(day, 0.0) + _f(row["total"])
        return sorted(buckets.items())

    def bulk_member_market_stats(self, board_members: dict[str, list[str]], *,
                                 trade_date: str, lookback: int = 80
                                 ) -> dict[str, dict[str, dict[str, float]]]:
        """**一次查询**算出多个板块的成分股量价截面。

        返回 `{board_code: {code: {net_5d, ret_10d, amount_5d, ...}}}`。

        ## 为什么必须批量

        第三层的龙头识别原本对每个板块单独调一次 `member_market_stats`，
        实测 0.17 秒/板块。**只对精选 10 个板块跑**时是 1.7 秒，可以接受；
        但要把评估范围扩大到候选池（60 个）甚至全市场（300 个）时，
        就是 10~50 秒 —— 每轮打分都这样，面板直接不可用。

        这里改成"取全部成分股并集 → 一条 SQL → 在内存里按板块归组"：
        代码数量从 300 条查询降到 1 条，代价只是多占一点内存。
        反向索引（`code → [板块]`）是必需的：一只股票同时属于多个板块
        （医药生物与 CRO 概念共享几十只成分股），逐板块重复累加会算错。
        """
        wanted = sorted({str(code).zfill(6) for codes in board_members.values()
                         for code in codes if code})
        stats = self._member_market_stats_bulk(wanted, trade_date=trade_date,
                                               lookback=lookback)
        if not stats:
            return {code: {} for code in board_members}
        out: dict[str, dict[str, dict[str, float]]] = {}
        for board_code, codes in board_members.items():
            out[board_code] = {str(code).zfill(6): stats[str(code).zfill(6)]
                               for code in codes
                               if str(code).zfill(6) in stats}
        return out

    def _member_market_stats_bulk(self, codes: Sequence[str], *,
                                  trade_date: str, lookback: int
                                  ) -> dict[str, dict[str, float]]:
        """`member_market_stats` 的批量实现（一条 SQL 覆盖全部代码）。"""
        if not codes:
            return {}
        warehouse = self.warehouse
        if warehouse is None or not getattr(warehouse, "available", lambda: False)():
            return {}
        start = _shift_days(trade_date, -int(max(lookback, 20) * 2))
        out: dict[str, dict[str, float]] = {}
        step = 900          # 与 member_stats 同一上限理由（兼容老 SQLite 的 999）
        for offset in range(0, len(codes), step):
            group = list(codes[offset:offset + step])
            marks = ",".join("?" for _ in group)
            sql = f"""
                SELECT d.code AS code, d.trade_date AS trade_date,
                       d.close AS close, d.amount AS amount,
                       m.net_mf_amount AS net
                FROM quant_daily d
                LEFT JOIN quant_moneyflow m
                       ON m.code = d.code AND m.trade_date = d.trade_date
                WHERE d.code IN ({marks}) AND d.trade_date BETWEEN ? AND ?
                ORDER BY d.code, d.trade_date
            """
            try:
                rows = warehouse.query(sql, (*group, start, trade_date))
            except Exception as exc:  # noqa: BLE001
                logger.warning("成分股量价批量读取失败：%s", brief(exc, BRIEF_TIGHT))
                continue
            per_code: dict[str, list[tuple[float, float, float]]] = {}
            for row in rows:
                per_code.setdefault(str(row["code"]).zfill(6), []).append(
                    (_f(row["close"]), _f(row["amount"]),
                     _opt(row["net"]) or 0.0))
            for code, series in per_code.items():
                closes = [item[0] for item in series]
                amounts = [item[1] for item in series]
                nets = [item[2] for item in series]
                ret = None
                if len(closes) >= 11 and closes[-11]:
                    ret = closes[-1] / closes[-11] - 1.0
                out[code] = {
                    "close": closes[-1] if closes else 0.0,
                    "net_5d": float(sum(nets[-5:])),
                    "net_10d": float(sum(nets[-10:])),
                    "ret_10d": ret if ret is not None else 0.0,
                    "amount_5d": float(sum(amounts[-5:])),
                    "bars": float(len(series)),
                }
        return out

    def member_market_stats(self, members: Sequence[str], *, trade_date: str,
                            lookback: int = 80) -> dict[str, dict[str, float]]:
        """成分股的量价/资金截面 `{code: {net_5d, net_window, ret_window, amount}}`。

        第三层龙头股识别需要"近 5 日主力净流入 / 近 10 日涨幅 / 近 5 日成交额"
        三个指标。这里一次 SQL 把窗口内的日线 + 资金流都取回来，在内存里
        按窗口切片 —— 比三个指标各发一次查询快，也保证三者用的是**同一批
        交易日**（否则"近 5 日资金"和"近 5 日成交额"可能覆盖不同日期区间，
        这在停牌股上会真的发生）。
        """
        wanted = [str(code).zfill(6) for code in members if code]
        out: dict[str, dict[str, float]] = {}
        if not wanted:
            return out
        warehouse = self.warehouse
        if warehouse is None or not getattr(warehouse, "available", lambda: False)():
            return out
        start = _shift_days(trade_date, -int(max(lookback, 20) * 2))
        marks = ",".join("?" for _ in wanted)
        sql = f"""
            SELECT d.code AS code, d.trade_date AS trade_date, d.close AS close,
                   d.amount AS amount, m.net_mf_amount AS net
            FROM quant_daily d
            LEFT JOIN quant_moneyflow m
                   ON m.code = d.code AND m.trade_date = d.trade_date
            WHERE d.code IN ({marks}) AND d.trade_date BETWEEN ? AND ?
            ORDER BY d.code, d.trade_date
        """
        try:
            rows = warehouse.query(sql, (*wanted, start, trade_date))
        except Exception as exc:  # noqa: BLE001
            logger.warning("成分股量价读取失败：%s", brief(exc, BRIEF_TIGHT))
            return out
        per_code: dict[str, list[tuple[str, float, float, float]]] = {}
        for row in rows:
            # 本地仓已是「元」（见 `_aggregate` 的说明），不再乘 1e3 / 1e4
            per_code.setdefault(str(row["code"]).zfill(6), []).append(
                (str(row["trade_date"]), _f(row["close"]),
                 _f(row["amount"]), _opt(row["net"]) or 0.0))
        for code, series in per_code.items():
            if not series:
                continue
            closes = [item[1] for item in series]
            amounts = [item[2] for item in series]
            nets = [item[3] for item in series]
            ret = None
            if len(closes) >= 11 and closes[-11]:
                ret = closes[-1] / closes[-11] - 1.0
            out[code] = {
                "close": closes[-1] if closes else 0.0,
                "net_5d": float(sum(nets[-5:])),
                "net_10d": float(sum(nets[-10:])),
                "ret_10d": ret if ret is not None else 0.0,
                "amount_5d": float(sum(amounts[-5:])),
                "bars": float(len(series)),
            }
        return out

    # ---------- 成分股聚合指标（第一层 prosperity / chips 用） ----------

    def member_stats(self, codes: Sequence[str], *, trade_date: str,
                     chunk: int = 900) -> dict[str, dict[str, float | None]]:
        """成分股的个股级指标 `{code: {...}}`（第一层聚合的输入）。

        返回字段：

            roe_yoy / netprofit_yoy / or_yoy   最新已披露报告期的同比（%）
            circ_mv                            流通市值（元，最近一个交易日）
            holder_change                      股东户数环比变化（小数；负=集中）

        ## 分块大小为什么是 900

        打分时调用方会把**全市场成分股去重后的并集**（约 5000~6000 只）一次
        传进来 —— 比"300 个板块各查一次"少两个数量级的往返。SQLite 的
        `IN (...)` 参数上限在新版本是 32766、老版本是 999，取 **900** 是两者
        都安全的值（本机 3.53.1，但用户环境未必）。

        ## 为什么这里**不算**近 N 日均成交额

        曾经用 `ROW_NUMBER() OVER (PARTITION BY code ORDER BY trade_date DESC)`
        在这里算 `amount_5d`（近 30 日窗口）。实测那是**单个查询 67.8 秒**的
        元凶：窗口函数要在 1539 万行的 `quant_daily` 上按 code 排序 15 次
        （每次一个分块），占了整轮打分 70 秒里的 67 秒。而 `six_dim` 根本不读
        这个字段 —— 第一层六个维度一个都不用成交额。
        需要成交额的调用方（第三层龙头识别的成交维度）走
        `member_market_stats`，它用**日期区间 + JOIN** 的写法，同样的数据
        只要 0.09 秒。

        ## 为什么必须按 `ann_date <= trade_date` 取财务

        财务指标是**追溯披露**的：2024 年报的 `end_date` 是 20241231，但
        `ann_date` 可能到 20250328。回测到 2024-12-31 那天若只看 `end_date`
        就会用上三个月后才公布的 ROE —— 这是最典型的**前视偏差**，而且它会
        让回测胜率系统性偏高（"景气度"维度变得先知先觉）。
        这里用 `COALESCE(NULLIF(ann_date,''), end_date) <= trade_date` 截断，
        读到行之后再按 `ann_date（缺失时 end_date+90 天）<= trade_date` 二次过滤。

        ## 为什么市值取"最近一个交易日"而不是 trade_date

        回测某一天时，`quant_daily_basic` 在那一日可能没有行（停牌、未上市）。
        取 `<= trade_date` 的最近一行是安全的（仍是历史信息）；
        取 `>= trade_date` 就会用到未来数据。
        """
        wanted = [str(code).zfill(6) for code in codes if code]
        out: dict[str, dict[str, float | None]] = {}
        if not wanted:
            return out
        warehouse = self.warehouse
        if warehouse is None or not getattr(warehouse, "available", lambda: False)():
            return out
        step = max(1, int(chunk))
        for offset in range(0, len(wanted), step):
            group = wanted[offset:offset + step]
            out.update(self._member_stats_chunk(warehouse, group,
                                                trade_date=trade_date))
        # 股东户数只查一次（与分块无关，逐块查会把同一个空表读十几遍）
        for code, change in self._holder_change_map(wanted, trade_date).items():
            item = out.setdefault(code, {"roe_yoy": None, "netprofit_yoy": None,
                                         "or_yoy": None, "report_period": ""})
            item["holder_change"] = change
        return out

    def _member_stats_chunk(self, warehouse: Any, wanted: Sequence[str], *,
                            trade_date: str
                            ) -> dict[str, dict[str, float | None]]:
        """`member_stats` 的单块实现（一次处理 `chunk` 只代码）。"""
        out: dict[str, dict[str, float | None]] = {}
        marks = ",".join("?" for _ in wanted)

        # 1) 财务（严格按公告日截断，防前视）
        fina_sql = f"""
            SELECT f.code AS code, f.roe_yoy AS roe_yoy,
                   f.netprofit_yoy AS netprofit_yoy, f.or_yoy AS or_yoy,
                   f.end_date AS end_date, f.ann_date AS ann_date
            FROM quant_fina_indicator f
            JOIN (
                SELECT code, MAX(end_date) AS end_date
                FROM quant_fina_indicator
                WHERE code IN ({marks})
                  AND COALESCE(NULLIF(ann_date, ''), end_date) <= ?
                GROUP BY code
            ) pick ON pick.code = f.code AND pick.end_date = f.end_date
            WHERE f.code IN ({marks})
        """
        try:
            rows = warehouse.query(fina_sql, (*wanted, trade_date, *wanted))
        except Exception as exc:  # noqa: BLE001
            logger.warning("成分股财务读取失败：%s", brief(exc, BRIEF_TIGHT))
            rows = []
        for row in rows:
            code = str(row["code"]).zfill(6)
            end_date = _s(row["end_date"])
            ann_date = _s(row["ann_date"]) or _shift_days(end_date, 90)
            if ann_date and ann_date > trade_date:
                continue  # 双保险：子查询已过滤，这里再挡一次
            out[code] = {"roe_yoy": _opt(row["roe_yoy"]),
                         "netprofit_yoy": _opt(row["netprofit_yoy"]),
                         "or_yoy": _opt(row["or_yoy"]),
                         "report_period": end_date}

        # 2) 市值 —— **按整日截面读，不按代码列表逐块查**。
        #
        # 原来的写法是"每 900 只代码一组，各自 MAX(trade_date) GROUP BY 再 JOIN"，
        # 实测 5900 只票要 2.77 秒：`GROUP BY code` 在 1539 万行上每组都要找
        # 一次"该股最近一行"。改成**先定位 ≤ trade_date 的最近交易日，
        # 再一次性读那一天的整张截面**（`idx_quant_daily_basic_date` 直接命中），
        # 无 `IN`、无 `GROUP BY`，同一份数据 0.05 秒量级。
        #
        # 代价：当日停牌的股票拿不到市值（原写法会回退到它上一笔交易日）。
        # 这是**可接受且更正确**的：停牌股本来就不该参与当日的板块资金流聚合，
        # 用上一日市值只会让板块流通市值虚高。
        basic_sql = """
            SELECT code, circ_mv FROM quant_daily_basic
            WHERE trade_date = (SELECT MAX(trade_date) FROM quant_daily_basic
                                WHERE trade_date <= ?)
        """
        try:
            rows = warehouse.query(basic_sql, (trade_date,))
        except Exception as exc:  # noqa: BLE001
            logger.warning("成分股市值读取失败：%s", brief(exc, BRIEF_TIGHT))
            rows = []
        wanted_set = set(wanted)
        for row in rows:
            code = str(row["code"]).zfill(6)
            if code not in wanted_set:
                continue
            item = out.setdefault(code, {"roe_yoy": None, "netprofit_yoy": None,
                                         "or_yoy": None, "report_period": ""})
            # 本地仓已是「元」（见 `_aggregate` 的说明），不再乘 1e4
            item["circ_mv"] = _opt(row["circ_mv"]) or 0.0

        # 3) 近 N 日均成交额 —— **刻意不在这里算**。
        #
        # 曾用 `ROW_NUMBER() OVER (PARTITION BY code ORDER BY trade_date DESC)`
        # 在这里算 amount_5d，实测单个分块 0.2~4.5 秒、整轮累计 **67.8 秒**，
        # 是整次打分 70 秒里的绝对大头（窗口函数要在 1539 万行上按 code 排序
        # 15 次）。而 six_dim 六个维度一个都不读成交额 —— 纯粹的浪费。
        # 需要成交额的调用方走 `member_market_stats`（日期区间 + JOIN，
        # 同样数据 0.09 秒）。
        return out

    def _holder_change_map(self, codes: Sequence[str], trade_date: str
                           ) -> dict[str, float]:
        """股东户数环比变化 `{code: change}`（只取公告日 ≤ trade_date 的最近两期）。"""
        marks = ",".join("?" for _ in codes)
        if not marks:
            return {}
        rows = self._read(
            f"SELECT code, end_date, holder_num FROM ml_holder"
            f" WHERE code IN ({marks}) AND (ann_date = '' OR ann_date <= ?)"
            f" AND end_date <= ? ORDER BY code, end_date DESC",
            (*codes, trade_date, trade_date))
        seen: dict[str, list[float]] = {}
        for row in rows:
            value = _opt(row["holder_num"])
            if value is None:
                continue
            bucket = seen.setdefault(str(row["code"]).zfill(6), [])
            if len(bucket) < 2:
                bucket.append(value)
        out: dict[str, float] = {}
        for code, values in seen.items():
            if len(values) == 2 and values[1]:
                out[code] = values[0] / values[1] - 1.0
        return out

    # ---------- 缺口计算 ----------

    def _missing_dates(self, table: str, start: str, end: str,
                       *, column: str = "trade_date") -> list[str]:
        """本地表在 `[start, end]` 内**缺哪些交易日**。

        以交易日历为准而不是自然日：用自然日会把周末与节假日算成缺失，
        然后对每个周末发一次注定拿不到数据的请求。
        """
        days = self.calendar(start, end)
        if not days:
            return []
        rows = self._read(
            f"SELECT DISTINCT {column} AS d FROM {table}"
            f" WHERE {column} BETWEEN ? AND ?", (start, end))
        have = {str(row["d"]) for row in rows}
        return [day for day in days if day not in have]


# ==================================================================
# SQL 片段
# ==================================================================

_BAR_UPSERT = (
    "INSERT INTO ml_board_bar(board_code, trade_date, open, high, low, close,"
    " pre_close, volume, amount, pct_change, source)"
    " VALUES(?,?,?,?,?,?,?,?,?,?,?)"
    " ON CONFLICT(board_code, trade_date) DO UPDATE SET"
    " open=excluded.open, high=excluded.high, low=excluded.low,"
    " close=excluded.close, pre_close=excluded.pre_close,"
    " volume=excluded.volume, amount=excluded.amount,"
    " pct_change=excluded.pct_change, source=excluded.source")

_FLOW_UPSERT = (
    "INSERT INTO ml_board_flow(board_code, trade_date, net_amount, net_rate,"
    " elg_amount, lg_amount, sm_amount, circ_mv, amount, source)"
    " VALUES(?,?,?,?,?,?,?,?,?,?)"
    " ON CONFLICT(board_code, trade_date) DO UPDATE SET"
    " net_amount=excluded.net_amount, net_rate=excluded.net_rate,"
    " elg_amount=excluded.elg_amount, lg_amount=excluded.lg_amount,"
    " sm_amount=excluded.sm_amount, circ_mv=excluded.circ_mv,"
    " amount=excluded.amount, source=excluded.source")


# ==================================================================
# 宏观因子装载器
# ==================================================================


def _month_series(frame: Any, column: str, *, scale: float = 1.0
                  ) -> list[tuple[str, float]]:
    """从 Tushare 月度表抽 `(YYYYMM, value)`（按月份升序，跳过空值）。"""
    if frame is None or len(frame) == 0 or column not in getattr(frame, "columns", []):
        return []
    out: list[tuple[str, float]] = []
    for row in frame.to_dict("records"):
        period = _s(row.get("month") or row.get("MONTH"))
        value = _opt(row.get(column))
        if period and value is not None:
            out.append((period, value * scale))
    out.sort(key=lambda item: item[0])
    return out


def _pmi(tushare: Any) -> list[tuple[str, float]]:
    """制造业 PMI（`cn_pmi.PMI010000`）。"""
    return _month_series(tushare.client.call("cn_pmi"), "PMI010000")


def _m1(tushare: Any) -> list[tuple[str, float]]:
    """M1 同比（`cn_m.m1_yoy`）。"""
    return _month_series(tushare.client.call("cn_m"), "m1_yoy")


def _m2(tushare: Any) -> list[tuple[str, float]]:
    """M2 同比（`cn_m.m2_yoy`）。"""
    return _month_series(tushare.client.call("cn_m"), "m2_yoy")


def _sf(tushare: Any) -> list[tuple[str, float]]:
    """社融存量同比：由 `sf_month.stk_endval` 自算同比（接口不直接给同比）。"""
    raw = _month_series(tushare.client.call("sf_month"), "stk_endval")
    out: list[tuple[str, float]] = []
    for index, (period, value) in enumerate(raw):
        if index >= 12 and raw[index - 12][1]:
            base = raw[index - 12][1]
            if abs(base) > 1e-9:
                out.append((period, (value / base - 1.0) * 100.0))
    return out


def _shibor(tushare: Any) -> list[tuple[str, float]]:
    """3 个月 Shibor（月内均值，落到 YYYYMM）。"""
    frame = tushare.client.call("shibor", start_date="20150101")
    if frame is None or len(frame) == 0 or "3m" not in frame.columns:
        return []
    buckets: dict[str, list[float]] = {}
    for row in frame.to_dict("records"):
        day = _s(row.get("date"))
        value = _opt(row.get("3m"))
        if len(day) < 6 or value is None:
            continue
        buckets.setdefault(day[:6], []).append(value)
    return sorted((period, sum(items) / len(items))
                  for period, items in buckets.items() if items)


def _cpi(tushare: Any) -> list[tuple[str, float]]:
    """CPI 同比（`cn_cpi.nt_yoy`）。"""
    return _month_series(tushare.client.call("cn_cpi"), "nt_yoy")


def _ppi(tushare: Any) -> list[tuple[str, float]]:
    """PPI 同比（`cn_ppi.ppi_yoy`；接口不可用时返回空 → 上游记 gap）。"""
    return _month_series(tushare.client.call("cn_ppi"), "ppi_yoy")


#: 宏观因子 → 装载器（键名与 `configs/mainline_macro_sensitivity.yaml` 的 factors 对齐）
_MACRO_LOADERS: dict[str, Callable[[Any], list[tuple[str, float]]]] = {
    "pmi": _pmi,
    "m1_yoy": _m1,
    "m2_yoy": _m2,
    "sf_yoy": _sf,
    "shibor_3m": _shibor,
    "cpi_yoy": _cpi,
    "ppi_yoy": _ppi,
}

#: 允许通过 `sync()` 同步的数据集（顺序即同步顺序：先目录后行情）
#:
#: `member` 是"逐板块调 ths_member"（慢，2200 个板块约 4 分钟）；
#: `board_crowding` / `member_crowding` 是**直接复用拥挤度模块已经维护好的
#: 板块池与成分股**（零 API 调用，36.9 万条一次导入）。默认用后者。
def _sync_index(store: MainlineDataStore, *, start: str, end: str) -> SyncResult:
    """同步 ETF 份额监控用到的宽基指数日线。

    指数代码**从 `etf_flow.yaml` 的观察清单读**，不在 datastore 里硬编码：
    清单是可配置的（改观察池就该自动改同步范围），硬编码会让配置改了而
    数据不跟着变 —— 那种不一致只会在面板上表现为"某个指数没有分位"，
    排查成本很高。
    """
    from src.mainline.etf_flow import load_config as load_flow_config

    config = load_flow_config()
    codes = list(dict.fromkeys(group.index for group in config.groups
                               if group.index))
    if not codes:
        return SyncResult(dataset="index", status="skipped",
                          message="etf_flow.yaml 的观察清单里没有配置指数")
    return store.sync_index_bars(codes, start=start, end=end)


SYNC_ORDER: tuple[str, ...] = (
    "calendar", "board_crowding", "member_crowding", "member", "stock_meta",
    "board_bar", "board_flow",
    "margin", "northbound", "holder", "seat", "macro", "future_meta", "future",
    "etf_meta", "etf", "index")


def apply_pure_pool(member_map: dict[str, list[str]],
                    pure_map: dict[str, dict[str, int]]
                    ) -> tuple[dict[str, list[str]], int, int]:
    """用提纯结果（`ml_member_pure`）收窄成分股池。

    返回 `(收窄后的 member_map, 剔除对数, 未评估对数)`。

    ## 三种情况必须分开处理（这是本函数存在的全部理由）

        (板块,股票) 在 pure 里且 relevant=1   → **保留**
        (板块,股票) 在 pure 里且 relevant=0   → **剔除**（明确判定不相关）
        (板块,股票) **不在 pure 里**          → **保留**（未评估 ≠ 不相关）

    ⚠️ 第三条是本模块最贵的一个教训。`relevance.clean_member_map` 原先写的是
    `rel_ok = (board, code) in rel`（`rel` 只装 relevant=1 的行），
    于是**「从未被评估」与「判定为不相关」被当成同一件事**，
    实测导致 65.9%（264902/401932）的 (板块,股票) 对被静默剔除 ——
    而它们只是没进过 LLM 的候选队列。

    为什么"未评估"是**结构性**的而不是例外：`ml_stock_theme` 只对每只股票的
    「相关性前 `max_candidates` 个题材」打分，长尾题材天然没有判定；
    而且成分股名单每次同步都会新增成员，新成员必然没有判定。
    按"未评估即剔除"处理，等于让**数据覆盖度**冒充**相关性结论**。

    整板在 pure 里完全没有行时**原样保留**（数据缺口不该被静默清空）。
    """
    kept: dict[str, list[str]] = {}
    dropped = 0
    unrated = 0
    for board, codes in member_map.items():
        board_pure = pure_map.get(board)
        if not board_pure:
            kept[board] = [str(c) for c in codes if c]
            continue
        alive: list[str] = []
        for item in codes:
            code = str(item)
            if not code:
                continue
            flag = board_pure.get(code)
            if flag is None:
                unrated += 1
                alive.append(code)
            elif int(flag) == 1:
                alive.append(code)
            else:
                dropped += 1
        if alive:
            kept[board] = alive
    return kept, dropped, unrated


def filter_listed(member_map: dict[str, list[str]],
                  list_dates: dict[str, str],
                  target: str) -> tuple[dict[str, list[str]], int, int]:
    """按**上市日期**剔除 `target` 当日尚未上市（或日期未知）的成分股。

    返回 `(过滤后的 member_map, 剔除的未上市对数, 剔除的日期缺失对数)`。

    ## 为什么"日期未知"也剔除

    `quant_stock_basic` 是按 `list_status='L'` 同步的**当前上市**名录。
    一份**当前**成分股名单里出现"查不到上市日期"的代码，基本只有两种可能：
    已退市/暂停上市，或者代码本身是脏数据（实测 `ml_member` 里有
    `00000A`~`00000W` 这类非法代码，全来自 `tushare:ths_index` 的池外板块）。
    两种都不该参与打分 —— 宁可少算，也不要让状态未知的代码影响板块分。

    空 `list_dates`（行情仓不可用且本地表为空）时**原样返回**：
    这时候"全部剔除"会把所有板块打成 0 分，比带着前视偏差更糟 ——
    调用方应当据此记一条 gap（见 `service.py` 的闸门段）。

    抽成模块级纯函数是为了能直接单测：过滤规则一旦写错（比如把 `>` 写成 `>=`），
    现象只是"分数略变"，在面板上完全看不出来。
    """
    if not list_dates:
        return member_map, 0, 0
    kept: dict[str, list[str]] = {}
    dropped_unlisted = 0
    dropped_unknown = 0
    for board_code, codes in member_map.items():
        alive: list[str] = []
        for stock in codes:
            listed = list_dates.get(str(stock))
            if not listed:
                dropped_unknown += 1
                continue
            if listed > target:
                dropped_unlisted += 1
                continue
            alive.append(stock)
        if alive:
            kept[board_code] = alive
    return kept, dropped_unlisted, dropped_unknown


def sync_all(store: MainlineDataStore, *, start: str, end: str,
             datasets: Iterable[str] | None = None,
             progress: Callable[[str], None] | None = None) -> list[SyncResult]:
    """按依赖顺序同步多个数据集（`datasets=None` 时同步全部）。

    刻意做成**独立函数**而不是 store 的方法：`MainlineDataStore` 要保持
    "一个数据集一个方法"的可测性，而"按什么顺序同步"是**调度策略**，
    会随运行场景变（实盘只同步最近几天、回测一次性补全历史）。
    """
    wanted = list(datasets) if datasets else list(SYNC_ORDER)
    results: list[SyncResult] = []
    for name in wanted:
        if progress is not None:
            progress(name)
        try:
            if name == "calendar":
                results.append(store.sync_calendar(start, end))
            elif name == "board":
                results.append(store.sync_boards())
            elif name == "board_crowding":
                results.append(store.import_crowding_pool())
            elif name == "member_crowding":
                results.append(store.import_crowding_members())
            elif name == "member":
                results.append(store.sync_members())
            elif name == "member_pit":
                results.append(store.sync_members_sw_pit())
            elif name == "stock_meta":
                # 个股上市日期：回测的上市日期闸门依赖它（见 `stock_list_dates`）。
                # 放在 `member` 之后同步，保证"有了名单就有上市日期"。
                results.append(store.sync_stock_meta())
            elif name == "board_bar":
                results.append(store.sync_board_bars(start=start, end=end))
            elif name == "board_flow":
                results.append(store.sync_board_flows(start=start, end=end))
            elif name == "margin":
                results.append(store.sync_margin(start=start, end=end))
            elif name == "northbound":
                results.append(store.sync_northbound(start=start, end=end))
            elif name == "holder":
                results.append(store.sync_holders())
            elif name == "seat":
                results.append(store.sync_seats(start=start, end=end))
            elif name == "macro":
                results.append(store.sync_macro())
            elif name == "future_meta":
                results.append(store.sync_future_meta())
            elif name == "future":
                results.append(store.sync_futures(start=start, end=end))
            elif name == "etf_meta":
                results.append(store.sync_etf_meta())
            elif name == "etf":
                results.append(store.sync_etf(start=start, end=end))
            elif name == "index":
                # 宽基指数日线：ETF 份额监控的分位数与市场环境都靠它，
                # 不同步就会拿旧的指数判"当前处于什么环境" —— 而环境决定
                # 机会信号是否放行，过期数据会静默改变告警行为。
                # 指数代码来自 etf_flow.yaml 的观察清单，不在 datastore 里硬编码。
                results.append(_sync_index(store, start=start, end=end))
            else:
                results.append(SyncResult(dataset=name, status="skipped",
                                          message="未知数据集"))
        except Exception as exc:  # noqa: BLE001 单个数据集失败不该中断整批
            logger.warning("主线数据同步 %s 失败：%s", name,
                           brief(exc, BRIEF_DEFAULT))
            results.append(SyncResult(dataset=name, status="failed",
                                      message=brief(exc, BRIEF_TIGHT)))
    return results


__all__ = [
    "DATASETS",
    "SYNC_ORDER",
    "MainlineDataStore",
    "SyncResult",
    "sync_all",
]


#: 主营分达标门槛（与 `member_pure.BUSINESS_PASS` 同值）。
#: ⚠️ 复制常量是有意的：`mainline` 运行时**不导入** `member_pure`（它是离线脚本
#: 用的模块，导入会连带拉起 LLM 客户端依赖）。改门槛时**两处必须同时改**。
BUSINESS_PASS = 70.0


def _admission(source: str, score: float | None) -> str:
    """入池通道（**只用于展示**）：这只票凭什么进了提纯股池。

        `corr`                 相关性直通（LLM 未判过该题材 → 无证据，不罚）
        `business`             主营分达标（>= `BUSINESS_PASS`）
        `corr_business_failed` ⚠️ 靠相关性入池，但 LLM 判过且**未达**门槛
        `""`                   无任何业务信息（展示字段取不到时）
    """
    if source == "llm_theme":
        return "business" if (score or 0.0) >= BUSINESS_PASS else "corr"
    if source == "llm":
        return "business" if (score or 0.0) >= BUSINESS_PASS else "corr"
    if source == "corr":
        if score is None:
            return "corr"
        return "business" if score >= BUSINESS_PASS else "corr_business_failed"
    return ""
