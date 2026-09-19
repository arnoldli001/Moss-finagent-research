"""板块概念拥挤度：数据源适配（板块列表 / 板块日线 / 全市场成交额 / 交易日历）。

## 为什么走 Tushare `ths_daily` 而不是"成分股成交额求和"

任务书写的是"板块成交额 = 成分股当日成交额之和"。实测两条路都通了之后选后者：

- **成分股求和**（`ths_member` + 仓库 `quant_daily`）：可用，但成分股接口
  只给**当前快照**，用它回算 6 年历史有前视偏差（今天的成分不等于当年的成分）；
- **板块指数日线**（`ths_daily` 的 `vol × avg_price`）：0.2s/板块/全历史，
  成交额与"成分股求和"同量级，且完全不受成分变更影响。

`ths_daily` 没有 amount 列，但 `vol × avg_price` 就是成交额（avg_price 是当日均价）。
成分股仍会落 `sector_member` 表作为**参考数据**（前端展示"这个板块有哪些票"），
但不参与拥挤度计算。

## 全市场成交额

取自本地仓库 `quant_daily.amount`（全 A 求和，实测 1538 万行有 amount 列）。
走本地仓库的理由：零网络、毫秒级、6 年全历史一次算完可缓存。
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from src.core.errors import (
    BRIEF_DEFAULT,
    brief,
)
from src.sector_crowding.config import (
    SectorCrowdingConfig,
    is_concept_board,
    load_config,
)

logger = logging.getLogger(__name__)

#: 全市场成交额的进程内缓存：{结束日: {交易日: 成交额}}
_MARKET_AMOUNT_CACHE: dict[str, dict[str, float]] = {}


# ======================================================================
# 交易日历
# ======================================================================

def trading_days(conn: sqlite3.Connection, *, start: str = "",
                 end: str = "") -> list[str]:
    """从本地仓库取交易日列表（`quant_daily` 里出现过的日期就是交易日）。"""
    sql = "SELECT DISTINCT trade_date FROM quant_daily"
    params: list[Any] = []
    clauses = []
    if start:
        clauses.append("trade_date >= ?")
        params.append(str(start))
    if end:
        clauses.append("trade_date <= ?")
        params.append(str(end))
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY trade_date"
    return [str(row[0]) for row in conn.execute(sql, params)]


def next_trading_day(warehouse: sqlite3.Connection, after: str) -> str:
    """`after` 之后的第一个交易日（没有则返回 ''）。"""
    row = warehouse.execute(
        "SELECT MIN(trade_date) FROM quant_daily WHERE trade_date > ?",
        (str(after),)).fetchone()
    return str(row[0] or "") if row else ""


def lookback_start(trade_date: str, years: int) -> str:
    """`trade_date` 往前 `years` 年的日历日（YYYYMMDD）。

    用**日历日**而不是交易日推算：任务是"近 6 年最高值"，分母应覆盖整 6 个自然年；
    交易日数会随节假日浮动，按 250×6 算会少几周的窗口。
    """
    try:
        parsed = datetime.strptime(str(trade_date), "%Y%m%d")
    except ValueError:
        parsed = datetime.now()
    return (parsed - timedelta(days=365 * int(years))).strftime("%Y%m%d")


def latest_trade_date(warehouse: sqlite3.Connection) -> str:
    row = warehouse.execute("SELECT MAX(trade_date) FROM quant_daily").fetchone()
    return str(row[0] or "") if row else ""


# ======================================================================
# 全市场成交额
# ======================================================================

def market_amount_by_day(warehouse: sqlite3.Connection, *, start: str,
                         end: str, use_cache: bool = True) -> dict[str, float]:
    """全市场（全部 A 股）每日成交额合计。

    只统计 `amount > 0` 的行：仓库里存在 amount 为空/0 的记录（实测 1538 万行中
    有少量），把它们当 0 会**低估**全市场成交额、从而**高估**拥挤度 —— 于是
    正常板块也容易被推到 80% 水位告警。宁可分母偏小一点点（缺的就是那几行），
    也不能让分子分母口径不一致。
    """
    key = f"{start}~{end}"
    if use_cache and key in _MARKET_AMOUNT_CACHE:
        return _MARKET_AMOUNT_CACHE[key]
    rows = warehouse.execute(
        "SELECT trade_date, SUM(amount) FROM quant_daily "
        "WHERE trade_date >= ? AND trade_date <= ? AND amount > 0 "
        "GROUP BY trade_date ORDER BY trade_date", (str(start), str(end))).fetchall()
    result = {str(row[0]): float(row[1] or 0.0) for row in rows}
    if use_cache:
        _MARKET_AMOUNT_CACHE[key] = result
    return result


def clear_market_cache() -> None:
    _MARKET_AMOUNT_CACHE.clear()


# ======================================================================
# 板块列表
# ======================================================================

def list_boards(config: SectorCrowdingConfig | None = None) -> list[dict[str, Any]]:
    """板块列表（代码/名称/类型/是否概念）。

    来源 = Tushare `ths_index`（有权威 type 分类）+ 本地 `dim_concept`
    （补 ths_index 没返回的 851 条新概念）。两边并集，按 ts_code 去重。

    实测：`ths_index` 全量 1666 条（type I/N/TH/S/BB/R/ST），本地 `dim_concept`
    2517 条且**完全包含**那 1666 条 —— 所以并集就是 2517 条，
    取 type 时以 ths_index 为准、本地独有的标 type=''（未知，不因此判成非概念）。
    """
    config = config or load_config()
    boards: dict[str, dict[str, Any]] = {}

    # ① Tushare ths_index（8 个 type 逐个拉，失败不影响其它 type）
    try:
        from src.quant.tushare_source import TushareClient, resolve_token

        client = TushareClient(token=resolve_token())
        for kind in ("N", "I", "R", "S", "BB", "TH", "ST"):
            try:
                frame = client.call("ths_index", exchange="A", type=kind)
            except Exception as exc:  # noqa: BLE001 单 type 失败跳过
                logger.warning("ths_index(type=%s) 失败：%s", kind, brief(exc, BRIEF_DEFAULT))
                continue
            if frame is None or len(frame) == 0:
                continue
            for _, row in frame.iterrows():
                code = str(row.get("ts_code") or "").strip()
                if not code:
                    continue
                boards[code] = {
                    "sector_code": code,
                    "sector_name": str(row.get("name") or "").strip(),
                    "board_type": kind,
                    "members": _safe_int(row.get("count")),
                    "list_date": str(row.get("list_date") or "").strip(),
                }
    except Exception as exc:  # noqa: BLE001 无 token/无网络时退到本地名录
        logger.warning("ths_index 不可用（退到本地 dim_concept）：%s", brief(exc, BRIEF_DEFAULT))

    # ② 本地 dim_concept 补充
    try:
        from src.sector_crowding.db import get_db_connection

        with get_db_connection(config) as conn:
            for row in conn.execute(
                    "SELECT ts_code, name, members FROM dim_concept"):
                code = str(row["ts_code"] or "").strip()
                if not code:
                    continue
                if code in boards:
                    if not boards[code]["sector_name"]:
                        boards[code]["sector_name"] = str(row["name"] or "")
                    if not boards[code]["members"]:
                        boards[code]["members"] = _safe_int(row["members"])
                    continue
                boards[code] = {
                    "sector_code": code,
                    "sector_name": str(row["name"] or "").strip(),
                    "board_type": "",
                    "members": _safe_int(row["members"]),
                    "list_date": "",
                }
    except Exception as exc:  # noqa: BLE001
        logger.warning("本地 dim_concept 读取失败：%s", brief(exc, BRIEF_DEFAULT))

    out = []
    for item in boards.values():
        # 判定时把 `board_type` 一起传进去：同花顺的 type 是权威分类
        # （`type` 为空才回落到代码前缀/名称规则，见 is_concept_board）
        item["is_concept"] = is_concept_board(
            item["sector_code"], item["sector_name"], config,
            board_type=str(item.get("board_type") or ""))
        out.append(item)
    out.sort(key=lambda item: item["sector_code"])
    logger.info("板块列表：%d 个（其中概念 %d 个）",
                len(out), sum(1 for item in out if item["is_concept"]))
    return out


# ======================================================================
# 板块日线（成交额）
# ======================================================================

def fetch_board_daily(config: SectorCrowdingConfig,
                      sector_code: str, *, start: str, end: str
                      ) -> list[dict[str, Any]]:
    """抓单个板块的日线成交额（`ths_daily` 的 vol × avg_price）。

    返回按交易日升序的 `[{"trade_date","sector_amount"}]`；失败抛异常由调用方处理。
    按 `fetch_chunk_years` 切片请求：单次返回有上限（实测无日期时约 1950 行），
    6 年 ≈ 1460 个交易日虽在限内，但切片更稳且便于失败重试只重跑一段。
    """
    from src.quant.tushare_source import TushareClient, resolve_token

    client = TushareClient(token=resolve_token())
    chunk_years = max(1, int(config.data.fetch_chunk_years))
    rows: dict[str, float] = {}

    cursor_start = str(start)
    while cursor_start <= str(end):
        cursor_end = _add_years(cursor_start, chunk_years)
        if cursor_end > str(end):
            cursor_end = str(end)
        frame = client.call("ths_daily", ts_code=sector_code,
                            start_date=cursor_start, end_date=cursor_end)
        if frame is not None and len(frame):
            for _, row in frame.iterrows():
                day = str(row.get("trade_date") or "").strip()
                amount = _amount_from_row(row)
                if day and amount is not None and amount > 0:
                    rows[day] = amount
        cursor_start = _next_day(cursor_end)

    return [{"trade_date": day, "sector_amount": rows[day]}
            for day in sorted(rows)]


def _amount_from_row(row: Any) -> float | None:
    """`ths_daily` 一行 → 成交额。

    优先用 amount 列（若该接口/账号有），否则 `vol × avg_price`。
    `avg_price` 缺失时退到收盘价（口径略差，记 debug 不静默）。
    """
    amount = _safe_float(row.get("amount"))
    if amount is not None and amount > 0:
        return amount
    volume = _safe_float(row.get("vol"))
    if volume is None or volume <= 0:
        return None
    price = _safe_float(row.get("avg_price"))
    if price is None or price <= 0:
        price = _safe_float(row.get("close"))
        if price is None or price <= 0:
            return None
        logger.debug("avg_price 缺失，用 close 估算成交额（%s）",
                     row.get("trade_date"))
    return volume * price


def fetch_board_members(sector_code: str) -> list[dict[str, Any]]:
    """板块成分股（参考数据；`ths_member` 只给**当前**快照）。"""
    from src.quant.tushare_source import TushareClient, resolve_token

    client = TushareClient(token=resolve_token())
    frame = client.call("ths_member", ts_code=sector_code)
    if frame is None or len(frame) == 0:
        return []
    out = []
    for _, row in frame.iterrows():
        code = str(row.get("con_code") or "").strip()
        if not code:
            continue
        out.append({"code": code.split(".")[0], "full_code": code,
                    "name": str(row.get("con_name") or "").strip()})
    return out


# ======================================================================
# 工具
# ======================================================================

def stock_names(config: SectorCrowdingConfig | None = None) -> dict[str, str]:
    """代码 → 名称（本地仓库名录，零网络）。"""
    config = config or load_config()
    path = Path(config.warehouse_path)
    if not path.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10.0)
        try:
            return {str(row[0]).zfill(6): str(row[1] or "")
                    for row in conn.execute(
                        "SELECT code, name FROM quant_stock_directory")}
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.warning("个股名录读取失败：%s", brief(exc, BRIEF_DEFAULT))
        return {}


def open_warehouse(config: SectorCrowdingConfig | None = None
                   ) -> sqlite3.Connection | None:
    """只读打开行情仓库（不存在返回 None）。"""
    config = config or load_config()
    path = Path(config.warehouse_path)
    if not path.exists():
        logger.warning("行情仓库不存在：%s", path)
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def _add_years(day: str, years: int) -> str:
    try:
        parsed = datetime.strptime(str(day), "%Y%m%d")
    except ValueError:
        return str(day)
    try:
        return parsed.replace(year=parsed.year + int(years)).strftime("%Y%m%d")
    except ValueError:      # 2/29 落到了非闰年
        return (parsed + timedelta(days=365 * int(years))).strftime("%Y%m%d")


def _next_day(day: str) -> str:
    try:
        parsed = datetime.strptime(str(day), "%Y%m%d")
    except ValueError:
        return str(day)
    return (parsed + timedelta(days=1)).strftime("%Y%m%d")


def _safe_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def timed(label: str):
    """上下文管理器：记录耗时（日志用）。"""
    class _Timer:
        def __enter__(self):
            self.started = time.perf_counter()
            return self

        def __exit__(self, *_exc):
            self.seconds = time.perf_counter() - self.started
            logger.debug("%s 耗时 %.2fs", label, self.seconds)
            return False

    return _Timer()


__all__ = [
    "clear_market_cache",
    "fetch_board_daily",
    "fetch_board_members",
    "latest_trade_date",
    "list_boards",
    "lookback_start",
    "market_amount_by_day",
    "next_trading_day",
    "open_warehouse",
    "stock_names",
    "trading_days",
]
