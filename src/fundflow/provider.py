"""资金流取数：板块（同花顺实时 + 东财历史）与个股（Tushare 仓库 + 东财实时）。

## 为什么三类取数走三条不同的路

1. **板块实时（盘中每一分钟都在变）**：同花顺 `stock_fund_flow_industry/concept`。
   实测这两个接口**不在子进程隔离名单里也能用**，但它们与
   `stock_board_concept_info_ths` 同属同花顺系（py_mini_racer 生成 hexin-v），
   而项目已实测该库在 Windows + Python 3.12 上会让**整个进程原生崩溃**。
   因此这里所有 akshare/同花顺调用一律走 `run_json_subprocess` 隔离 ——
   崩溃只损失一次刷新，不会带走整个投研服务。
2. **板块历史（10 日净额走势）**：东财 `stock_sector_fund_flow_hist` /
   `stock_concept_fund_flow_hist`。单板块一次请求，慢（实测 5~19s 且偶发
   RemoteDisconnected），所以：**逐板块独立缓存 + 并发抓取 + 失败只影响那一条**。
3. **个股（权威且可回溯）**：Tushare `moneyflow` 已落在本地仓库（`data/quant`），
   直接 SQL 读，**零网络、毫秒级**；流通市值取 `daily_basic.circ_mv`。
   盘中要"实时净额"时再补一次东财 `stock_individual_fund_flow`（失败不影响日频序列）。

## 单位纪律

Tushare 的 `moneyflow` 在下载层已统一成**元**（见 `tushare_source._UNIT_SCALE`），
同花顺"流入/流出/净额"是**亿元**、东财 `stock_individual_fund_flow` 是**元**。
本模块**对外统一用元**，每个实体带 `unit` 字段，前端只需按"亿/万"格式化。
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from src.core.errors import (
    BRIEF_DEFAULT,
    BRIEF_TIGHT,
    brief,
)
from src.core.trading_session import session_state
from src.fundflow.models import FlowEntity, FlowPoint
from src.intraday.subproc import run_json_subprocess

logger = logging.getLogger(__name__)

# 同花顺即时资金流的单位是亿元
YI = 100_000_000.0
# 板块历史缓存：日频数据，收盘后才变，缓存久一点（盘中重取无意义）
SECTOR_HISTORY_TTL = 1800.0
# 实时快照缓存：盘中 60 秒（与做T自选池同一节奏）
REALTIME_TTL = 60.0
# 个股日频（本地仓库）：几乎零成本，缓存 300 秒足够
STOCK_TTL = 300.0
# 板块成分股：一天之内不变，缓存久一点（取一次要一个子进程）
MEMBERS_TTL = 6 * 3600.0
# 涨停池（含"涨停原因"）：盘中会变，60 秒缓存
LIMITUP_TTL = 60.0
# 昨日涨停（仓库自算）的缓存：同一交易日结果不变，给长一点避免反复扫仓库
LIMIT_UP_CACHE_TTL = 600.0

#: 腾讯快照 `~` 分隔字段的下标（实测 2026-09-17，共 88 段）。
#: 资金流监控的「流通市值 / 今日涨幅」两列靠它拿**盘中实时值** —— 本地仓库只有日频。
_TX_FIELD_NAME = 1
_TX_FIELD_PRICE = 3
_TX_FIELD_PRE_CLOSE = 4
_TX_FIELD_CHANGE_PCT = 32
_TX_FIELD_FLOAT_MV = 44
_TX_FIELD_TOTAL_MV = 45


def _norm_name(name: str) -> str:
    """板块名归一化（去空白/标点/大小写），用于跨数据源比对。"""
    return "".join(char for char in str(name).lower()
                   if char.isalnum() or "\u4e00" <= char <= "\u9fff")


def _session_state() -> str:
    """当前时段（与做T模块同口径；只用于决定要不要打盘中实时接口）。"""
    return session_state()[0]


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or not math.isfinite(number):
        return None
    return number


def _recent_days(days: int, *, end: datetime | None = None) -> list[str]:
    """最近 N 个**自然日**的 YYYYMMDD（含周末；查询端按交易日过滤即可）。

    刻意不查交易日历：本模块只是给一个足够宽的区间去 SQL 里筛，
    交易日历本身还要额外一次取数，而"多几天自然日"对结果无影响。
    """
    moment = end or datetime.now()
    return [(moment - timedelta(days=offset)).strftime("%Y%m%d")
            for offset in range(days * 2 + 6, -1, -1)]


class FundFlowProvider:
    """资金流取数（进程内单例，自带 TTL 缓存）。"""

    def __init__(self) -> None:
        self._sector_hist: dict[str, tuple[float, FlowEntity]] = {}
        self._realtime: tuple[float, dict[str, dict[str, Any]]] | None = None
        self._stock_cache: tuple[float, Any] = (0.0, None)
        # 昨日涨停（仓库自算）的进程内缓存：同一交易日结果不变，缓存 10 分钟。
        # 不缓存的话，资金流监控每次快照都会重付那 3.46 秒的仓库扫描。
        self._limit_up_cache: tuple[float, dict[str, str]] | None = None
        self._members: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._warehouse: Any = None
        self._warehouse_failed = False
        # Tushare 板块截面/历史（首选口径）
        self._tushare_client: Any = None
        self._tushare_failed = False
        self._sector_snapshot: tuple[float, dict[str, dict[str, Any]]] | None = None
        self._sector_snapshot_dirty = True

    # ==================== 个股：Tushare 本地仓库 ====================

    def _warehouse_client(self) -> Any:
        if self._warehouse is not None or self._warehouse_failed:
            return self._warehouse
        try:
            from src.quant.warehouse import QuantWarehouse

            client = QuantWarehouse()
            if not client.available():
                raise RuntimeError("数据仓库不可用")
            self._warehouse = client
        except Exception as exc:  # noqa: BLE001 仓库不可用是正常状态（未配置）
            logger.info("资金流监控：Tushare 仓库不可用（%s），个股日频将走东财", brief(exc, BRIEF_TIGHT))
            self._warehouse_failed = True
        return self._warehouse

    def _load_stock_panels(self, *,
                           start: str, end: str) -> tuple[pd.DataFrame, pd.DataFrame]:
        """按区间读 moneyflow + daily_basic（保留给需要自定义区间的调用方）。"""
        client = self._warehouse_client()
        if client is None:
            return pd.DataFrame(), pd.DataFrame()
        return (client.load("moneyflow", start=start, end=end),
                client.load("daily_basic", start=start, end=end))

    async def stock_frame(
        self, *, window_days: int = 10,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """近 N 日**全市场**资金流与流通市值（宽表，读本地仓库，零网络）。

        返回 `(flows, caps)`：
          - `flows`：列 `code/date/net/buy_lg/sell_lg/buy_elg/sell_elg`，只保留每只票
            最近 `window_days` 个交易日；
          - `caps`：列 `code/circ_mv`，每只票取区间内最新一天的流通市值。

        为什么整表读：榜单要**全市场排序**，必须有多少票看多少票。
        仓库 `load` 走索引区间扫描（10 个交易日 moneyflow 约 5 万行），实测毫秒级；
        用 DataFrame 而不是逐票 FlowEntity，是因为后者要为 5000 只票各建一个对象 ——
        排序只用得上 4 个标量列。
        """
        cached_at, cached = self._stock_cache
        if cached is not None and (time.monotonic() - cached_at) < STOCK_TTL:
            return cached
        empty = (pd.DataFrame(), pd.DataFrame())
        client = self._warehouse_client()
        if client is None:
            return empty
        days = _recent_days(window_days)
        # ---- 本地磁盘缓存 + 增量补充（用户口径 2026-09-17）----
        # 全市场 10 日 moneyflow + daily_basic + daily 三张表一次要 1~3 秒，
        # 而**跨交易日只有最后一天是新的**。所以：落盘一份 Parquet，
        # 下次进程启动直接读盘，只把"盘上最大日期之后"的新增日从仓库补上来。
        disk = self._load_frame_cache(window_days)
        if disk is not None:
            frame, caps, cached_max = disk
            fresh = [day for day in days if day > cached_max]
            if not fresh:
                self._stock_cache = (time.monotonic(), (frame, caps))
                logger.info("资金流面板命中本地缓存（%d 行，最新 %s，无需补数）",
                            len(frame), cached_max)
                return frame, caps
            appended = await asyncio.to_thread(
                self._load_incremental, fresh, window_days)
            if appended is not None:
                frame = pd.concat([frame, appended], ignore_index=True)
                # 只保留窗口内的日期，避免缓存无限增长
                frame = frame[frame["date"].isin(days)]
                caps = self._caps_from(frame)
                self._save_frame_cache(frame, caps, window_days)
                self._stock_cache = (time.monotonic(), (frame, caps))
                logger.info("资金流面板：本地缓存 + 增量 %d 天 → %d 行",
                            len(fresh), len(frame))
                return frame, caps
        # 冷路径：全量读仓库
        flows = await asyncio.to_thread(
            client.load, "moneyflow", start=days[0], end=days[-1])
        basics = await asyncio.to_thread(
            client.load, "daily_basic", start=days[0], end=days[-1],
        )
        # 日线（OHLCV）：走势图要叠 K 线与成交量柱，资金流表本身没有价格。
        # 与 moneyflow 同区间取，merge 后逐点即"资金流 + 当日 K 线"。
        bars = await asyncio.to_thread(
            client.load, "daily", start=days[0], end=days[-1])
        if flows is None or len(flows) == 0:
            return empty
        frame, caps = self._normalize_frames(flows, bars, basics=basics)
        if frame is None or len(frame) == 0:
            return empty
        # 每只票只留最近 window_days 个交易日（按日期截断，与"最近 N 日"口径一致）
        cutoff = sorted(frame["date"].unique())[-window_days:]
        frame = frame[frame["date"].isin(cutoff)]
        self._stock_cache = (time.monotonic(), (frame, caps))
        self._save_frame_cache(frame, caps, window_days)
        return frame, caps

    def _normalize_frames(self, flows: Any, bars: Any, *,
                          basics: Any = None) -> tuple[Any, Any]:
        """把仓库原始三表规范化成面板 `(frame, caps)`（**冷路径与增量共用同一份**）。

        抽成同一个函数是必须的：增量补数与全量读取若各写一套列名/单位变换，
        会出现"缓存里的旧数据与新增数据口径不一致"这种极难察觉的错位。
        """
        frame = flows.copy()
        frame["code"] = frame["code"].astype(str).str.zfill(6)
        frame["date"] = frame["trade_date"].astype(str)
        frame = frame.rename(columns={"net_mf_amount": "net",
                                      "buy_lg_amount": "buy_lg",
                                      "sell_lg_amount": "sell_lg",
                                      "buy_elg_amount": "buy_elg",
                                      "sell_elg_amount": "sell_elg"})
        if bars is not None and len(bars):
            price = bars.copy()
            price["code"] = price["code"].astype(str).str.zfill(6)
            price["date"] = price["trade_date"].astype(str)
            price = price.rename(columns={"volume_lot": "volume", "vol": "volume"})
            price_columns = [column for column in
                             ("date", "code", "open", "high", "low", "close",
                              "volume", "amount", "pct_chg")
                             if column in price.columns]
            price = price[price_columns].drop_duplicates(
                subset=["date", "code"], keep="last")
            frame = frame.merge(price, on=["date", "code"], how="left")
        keep = ["code", "date", "net", "buy_lg", "sell_lg", "buy_elg", "sell_elg",
                "open", "high", "low", "close", "volume", "amount", "pct_chg"]
        frame = frame[[column for column in keep if column in frame.columns]]
        caps = pd.DataFrame(columns=["code", "circ_mv"])
        if basics is not None and len(basics):
            basic = basics.copy()
            basic["code"] = basic["code"].astype(str).str.zfill(6)
            basic["date"] = basic["trade_date"].astype(str)
            basic = basic[basic["code"].isin(set(frame["code"]))]
            basic = basic.sort_values("date")
            caps = (basic.groupby("code", as_index=False)
                    .last()[["code", "circ_mv"]])
        return frame, caps

    # ---------------- 本地缓存（跨进程复用 + 增量补充） ----------------

    @staticmethod
    def _cache_dir() -> Path:
        """本地缓存目录（Parquet）。项目根的 `data/cache/fundflow`。"""
        root = Path(__file__).resolve().parents[2]
        target = root / "data" / "cache" / "fundflow"
        target.mkdir(parents=True, exist_ok=True)
        return target

    def _cache_files(self, window_days: int) -> tuple[Path, Path]:
        """缓存文件路径（按窗口天数分文件，避免不同窗口互相覆盖）。"""
        base = self._cache_dir() / f"stock_frame_{int(window_days)}d"
        return base.with_suffix(".parquet"), base.with_suffix(".caps.parquet")

    def _load_frame_cache(self, window_days: int) -> tuple[Any, Any, str] | None:
        """读本地缓存 → `(frame, caps, 最大日期)`；不可用返回 None（不抛错）。"""
        frame_path, caps_path = self._cache_files(window_days)
        if not frame_path.exists() or not caps_path.exists():
            return None
        try:
            frame = pd.read_parquet(frame_path)
            caps = pd.read_parquet(caps_path)
        except Exception as exc:  # noqa: BLE001 缓存损坏就当没有
            logger.info("资金流面板缓存读取失败（将全量重取）：%s", brief(exc, BRIEF_TIGHT))
            return None
        if frame is None or len(frame) == 0 or "date" not in frame.columns:
            return None
        # 缓存太旧（超过 30 个自然日）就没有增量价值，直接全量重取
        cached_max = str(frame["date"].astype(str).max())
        today = datetime.now().strftime("%Y%m%d")
        if cached_max < (datetime.now() - timedelta(days=30)).strftime("%Y%m%d"):
            logger.info("资金流面板缓存过期（%s），改为全量重取", cached_max)
            return None
        del today
        return frame, caps, cached_max

    def _load_incremental(self, fresh_days: list[str], window_days: int) -> Any:
        """只读 `fresh_days` 这几天的三张表（**不扫历史**）。

        这是"滑窗增量"的核心：新交易日只补最新一天，而不是把 10 天全市场重读。
        """
        client = self._warehouse_client()
        if client is None or not fresh_days:
            return None
        start, end = min(fresh_days), max(fresh_days)
        try:
            flows = client.load("moneyflow", start=start, end=end)
            bars = client.load("daily", start=start, end=end)
        except Exception as exc:  # noqa: BLE001
            logger.info("增量补数失败（回落全量）：%s", brief(exc, BRIEF_TIGHT))
            return None
        if flows is None or len(flows) == 0:
            return None
        # 增量日没有 daily_basic（市值变化慢，沿用缓存里那份即可）
        return self._normalize_frames(flows, bars, basics=None)[0]

    @staticmethod
    def _caps_from(frame: Any) -> Any:
        """从面板里提取 `code/circ_mv`（缓存里 daily_basic 的市值列随行保存）。"""
        if frame is None or len(frame) == 0 or "circ_mv" not in frame.columns:
            return pd.DataFrame(columns=["code", "circ_mv"])
        latest = frame.sort_values("date").groupby("code", as_index=False).last()
        return latest[["code", "circ_mv"]]

    def _save_frame_cache(self, frame: Any, caps: Any, window_days: int) -> None:
        """把面板与市值落盘（失败只记日志，绝不影响取数）。"""
        if frame is None or len(frame) == 0:
            return
        frame_path, caps_path = self._cache_files(window_days)
        try:
            frame.to_parquet(frame_path, index=False)
            caps.to_parquet(caps_path, index=False)
        except Exception as exc:  # noqa: BLE001
            logger.info("资金流面板缓存写入失败：%s", brief(exc, BRIEF_TIGHT))

    async def stock_series(
        self, codes: list[str], *, window_days: int = 10,
    ) -> dict[str, dict[str, Any]]:
        """按代码取近 N 日资金流 + 流通市值（基于 `stock_frame`，无网络）。"""
        frame, caps = await self.stock_frame(window_days=window_days)
        if frame is None or len(frame) == 0:
            return {}
        wanted = {str(code).split(".")[0].zfill(6) for code in codes} if codes else None
        if wanted is not None:
            frame = frame[frame["code"].isin(wanted)]
        cap_map: dict[str, float | None] = {}
        if caps is not None and len(caps):
            cap_map = {str(row["code"]): _finite(row["circ_mv"])
                       for _, row in caps.iterrows()}
        result: dict[str, dict[str, Any]] = {}
        for code, group in frame.groupby("code"):
            points = [
                FlowPoint(
                    date=str(row["date"]),
                    net=_finite(row.get("net")),
                    buy_lg=_finite(row.get("buy_lg")),
                    sell_lg=_finite(row.get("sell_lg")),
                    buy_elg=_finite(row.get("buy_elg")),
                    sell_elg=_finite(row.get("sell_elg")),
                    # 日线 OHLCV：走势图要叠 K 线与成交量柱（同一时间轴上对比）
                    open=_finite(row.get("open")),
                    high=_finite(row.get("high")),
                    low=_finite(row.get("low")),
                    close=_finite(row.get("close")),
                    volume=_finite(row.get("volume")),
                    amount=_finite(row.get("amount")),
                    pct_chg=_finite(row.get("pct_chg")),
                ).to_dict()
                for _, row in group.sort_values("date").iterrows()
            ]
            result[str(code)] = {"points": points,
                                 "circ_mv": cap_map.get(str(code))}
        return result

    # ==================== 板块：同花顺实时（子进程隔离） ====================

    async def _ths_intraday(self) -> dict[str, dict[str, Any]]:
        """同花顺即时板块资金流（盘中实时层；失败返回空，不影响主口径）。"""
        cached = self._realtime
        if cached is not None and (time.monotonic() - cached[0]) < REALTIME_TTL:
            return cached[1]
        payload = await run_json_subprocess(
            """
import akshare as ak
frames = {}
for label, fn in (("industry", ak.stock_fund_flow_industry),
                  ("concept", ak.stock_fund_flow_concept)):
    try:
        df = fn(symbol="即时")
    except Exception as exc:
        frames[label] = {"error": f"{type(exc).__name__}: {exc}"}
        continue
    rows = []
    for _, row in df.iterrows():
        rows.append({str(k): (None if v is None else v) for k, v in row.items()})
    frames[label] = {"rows": rows}
__emit(frames)
""",
            timeout=90.0, label="资金流-板块即时(同花顺)")
        merged: dict[str, dict[str, Any]] = {}
        if payload:
            for label in ("industry", "concept"):
                block = payload.get(label) or {}
                for row in block.get("rows") or []:
                    name = str(row.get("行业") or "").strip()
                    if not name:
                        continue
                    merged[name] = {
                        "net_yi": _finite(row.get("净额")),
                        "change_pct": _finite(row.get("行业-涨跌幅")),
                        "companies": _finite(row.get("公司家数")),
                        "leader": str(row.get("领涨股") or "").strip(),
                    }
        self._realtime = (time.monotonic(), merged)
        return merged

    def limit_up_codes_from_warehouse(self, *, lookback_days: int = 12,
                                      force: bool = False) -> dict[str, str]:
        """仓库自算**上一个交易日**的涨停股：`{code: 交易日}`。

        为什么要自算：东财 `push2ex` 只给"今天"（`date` 参数无效），
        而"昨日涨停"需要**历史**判别。本地仓库有 `daily`（最高价）与
        `stk_limit`（当日涨停价），两者一比就是确定性的涨停判定，
        不依赖任何在线接口，也不需要盘中数据。

        ## 两个性能修正（2026-09-17 实测）

        1. **只查近 `lookback_days` 个自然日**：原来查"20260101 至今"，
           在 5500 只票上要 **3.46 秒**；而我们只需要"上一交易日"这一天的数据，
           窗口缩到 12 个自然日就够（覆盖假期），实测降到 ~0.2 秒。
        2. **进程内缓存**：同一交易日的结果不变，缓存 10 分钟 ——
           资金流监控每次快照都会调它，不缓存就会反复付这几秒。

        Returns:
            `{6位代码: 涨停日}`；仓库不可用或数据缺失时返回空 dict（调用方记缺口）。
        """
        if not force and self._limit_up_cache is not None:
            saved_at, payload = self._limit_up_cache
            if (time.monotonic() - saved_at) < LIMIT_UP_CACHE_TTL:
                return payload
        client = self._warehouse_client()
        if client is None:
            return {}
        from datetime import timedelta

        end = datetime.now()
        start = (end - timedelta(days=max(5, int(lookback_days)))).strftime("%Y%m%d")
        today = end.strftime("%Y%m%d")
        try:
            prices = client.load("daily", start=start, end=today)
            limits = client.load("stk_limit", start=start, end=today)
        except Exception as exc:  # noqa: BLE001 仓库读取失败按"没有数据"处理
            logger.info("仓库读取 daily/stk_limit 失败：%s", brief(exc, BRIEF_TIGHT))
            return {}
        if prices is None or limits is None or len(prices) == 0 or len(limits) == 0:
            logger.info("仓库缺 daily/stk_limit → 无法自算昨日涨停")
            return {}
        # 仓库两表都用 `trade_date`；统一成 `date` 再合并（实测直接按 date
        # 合并会 KeyError: "['date'] not in index"）。
        for table in (prices, limits):
            if "trade_date" in table.columns and "date" not in table.columns:
                table.rename(columns={"trade_date": "date"}, inplace=True)
        merged = prices.merge(limits[["date", "code", "up_limit"]],
                              on=["date", "code"], how="inner")
        if merged.empty:
            return {}
        hit = merged[pd.to_numeric(merged["high"], errors="coerce")
                     >= pd.to_numeric(merged["up_limit"], errors="coerce") - 1e-6]
        if hit.empty:
            return {}
        dates = sorted(hit["date"].astype(str).unique())
        target = dates[-2] if len(dates) >= 2 else dates[-1]
        codes = hit[hit["date"].astype(str) == target]["code"].astype(str).unique()
        logger.info("仓库自算昨日涨停：%s 共 %d 只", target, len(codes))
        payload = {str(code): target for code in codes}
        self._limit_up_cache = (time.monotonic(), payload)
        return payload

    async def sector_realtime(self, *, force: bool = False) -> dict[str, dict[str, Any]]:
        """同花顺即时板块资金流（对外入口，保持与旧调用方兼容）。"""
        if force:
            self._realtime = None
        return await self._ths_intraday()

    # ==================== 板块：Tushare 东财口径（首选，本地/在线均可靠） ====================

    def _tushare(self) -> Any:
        """Tushare 客户端（一次装配、失败后不再重试）。"""
        if self._tushare_client is not None or self._tushare_failed:
            return self._tushare_client
        try:
            from src.quant.tushare_source import TushareClient

            self._tushare_client = TushareClient()
        except Exception as exc:  # noqa: BLE001 未配 token 是正常状态
            logger.info("资金流监控：Tushare 客户端不可用（%s）", brief(exc, BRIEF_TIGHT))
            self._tushare_failed = True
        return self._tushare_client

    def _sector_frame_sync(self) -> pd.DataFrame:
        """东财板块资金流（`moneyflow_ind_dc`，不带日期 = 最新交易日全量）。

        实测（2026-09-17）：不传 `trade_date` 返回**最新交易日**的 1031 个板块，
        列含 `net_amount`（净额，元）、`net_amount_rate`（净占比%）、
        `buy_elg_amount/buy_lg_amount`（超大单/大单，元）、`rank`（当日排名）。
        这比"同花顺即时 + 东财历史"两条链都稳：一次请求拿到全市场板块截面，
        且**当日就有数据**（不用等收盘）。
        """
        client = self._tushare()
        if client is None:
            return pd.DataFrame()
        try:
            # 注意：**不要**写 `to_thread(client.call, api, {})` ——
            # `call(self, api, **params)` 只接受关键字参数，把 `{}` 当第二个位置参数
            # 传进去永远是 `TypeError: takes 2 positional arguments but 3 were given`，
            # 而它会被这里捕获成"数据源不可用"，整条板块链静默变空（实测踩过）。
            frame = client.call("moneyflow_ind_dc")
        except Exception as exc:  # noqa: BLE001 权限不足/网络问题都按"不可用"处理
            logger.info("Tushare moneyflow_ind_dc 不可用：%s", brief(exc, BRIEF_DEFAULT))
            return pd.DataFrame()
        if frame is None or len(frame) == 0:
            return pd.DataFrame()
        frame = frame.copy()
        frame["trade_date"] = frame["trade_date"].astype(str)
        # ⚠️ 不传 trade_date 时接口会返回**多个交易日**的拼接（实测 5000 行里含
        # 20260914/15/16 三天），并不是"只要最新一天"。每个板块只保留最新一行，
        # 否则"当日净额"会随机取到几天前的值（实测踩过：截面日期显示 20260910）。
        frame = (frame.sort_values("trade_date")
                 .drop_duplicates(subset=["ts_code"], keep="last"))
        return frame

    @staticmethod
    def payload_from_frame(frame: pd.DataFrame) -> dict[str, dict[str, Any]]:
        """板块截面 DataFrame → `{板块名: {...}}`（**纯函数**，供同步调用方复用）。

        抽成纯函数的原因：`sector_snapshot()` 是 async 的（要合并盘中实时层），
        而"日K多因子选股"那条线是同步脚本、没有事件循环 —— 让它 `asyncio.run()`
        会与既有事件循环冲突。把解析逻辑独立出来后，两边共用同一份口径，
        不会出现"面板看到的板块净额"与"选股用的板块净额"不一致。
        """
        payload: dict[str, dict[str, Any]] = {}
        if frame is None or len(frame) == 0:
            return payload
        for _, row in frame.iterrows():
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            payload[name] = {
                "code": str(row.get("ts_code") or ""),
                "trade_date": str(row.get("trade_date") or ""),
                "net": _finite(row.get("net_amount")),
                "net_rate": _finite(row.get("net_amount_rate")),
                "buy_elg": _finite(row.get("buy_elg_amount")),
                "buy_lg": _finite(row.get("buy_lg_amount")),
                "pct_change": _finite(row.get("pct_change")),
                "rank": _finite(row.get("rank")),
                "content_type": str(row.get("content_type") or ""),
                "net_realtime": False,
            }
        return payload

    def sector_snapshot_sync(self) -> dict[str, dict[str, Any]]:
        """**同步**板块截面（不含盘中实时覆盖，走 Tushare 东财口径）。

        给同步脚本/流水线用（日K选股、定时任务）；网页面板仍走 async 版本
        （那边会再叠一层同花顺实时净额）。
        """
        if not self._sector_snapshot_dirty and self._sector_snapshot is not None:
            saved_at, payload = self._sector_snapshot
            if (time.monotonic() - saved_at) < REALTIME_TTL:
                return payload
        # **磁盘优先**：在线拉一次 1030 个板块实测 50 秒量级，而这是一份
        # "最新交易日截面"（一天只有一份）。进程重启后第一屏请求不该同步等它 ——
        # 先用落盘的那份（最多旧一天），同时后台刷新，下一轮自然就是新的。
        # 实测：磁盘优先把重启后的首屏从 52.94s 降到秒级。
        disk = self._load_sector_cache()
        if disk:
            self._sector_snapshot = (time.monotonic(), disk)
            threading.Thread(target=self._refresh_sector_async,
                             name="fundflow-sector-refresh", daemon=True).start()
            return disk
        payload = self.payload_from_frame(self._sector_frame_sync())
        if not payload:
            # 在线不可用 → 用落盘的上一份（标注 days_stale 让前端知道新旧）
            payload = self._load_sector_cache()
            if payload:
                logger.info("板块截面：在线不可用，改用本地缓存 %d 个板块", len(payload))
        if payload:
            self._sector_snapshot = (time.monotonic(), payload)
            self._sector_snapshot_dirty = False
            self._save_sector_cache(payload)
        return payload

    def _refresh_sector_async(self) -> None:
        """后台刷新板块截面（供"磁盘优先"路径使用；失败只记日志）。"""
        try:
            payload = self.payload_from_frame(self._sector_frame_sync())
        except Exception as exc:  # noqa: BLE001 后台失败不该影响任何请求
            logger.info("板块截面后台刷新失败：%s", brief(exc, BRIEF_TIGHT))
            return
        if payload:
            self._sector_snapshot = (time.monotonic(), payload)
            self._sector_snapshot_dirty = False
            self._save_sector_cache(payload)
            logger.info("板块截面后台刷新完成：%d 个板块", len(payload))

    def _sector_cache_path(self) -> Path:
        """板块截面的本地缓存文件。"""
        return self._cache_dir() / "sector_snapshot.parquet"

    def _save_sector_cache(self, payload: dict[str, dict[str, Any]]) -> None:
        """把板块截面落盘（失败只记日志）。

        为什么必须落盘：`moneyflow_ind_dc` 一次要拉 1030 个板块、单次实测 **50 秒量级**，
        而它是"最新交易日截面"（一天只有一份）。进程重启后第一屏请求若同步等它，
        用户看到的就是"一直显示取数中"（实测冷启动 52.94s）。
        """
        try:
            frame = pd.DataFrame([
                {"name": name, **{k: v for k, v in info.items()}}
                for name, info in payload.items()])
            frame.to_parquet(self._sector_cache_path(), index=False)
        except Exception as exc:  # noqa: BLE001
            logger.info("板块截面缓存写入失败：%s", brief(exc, BRIEF_TIGHT))

    def _load_sector_cache(self) -> dict[str, dict[str, Any]]:
        """读板块截面缓存（不存在/损坏返回空 dict；超过 5 个自然日视为过期）。"""
        path = self._sector_cache_path()
        if not path.exists():
            return {}
        try:
            if time.time() - path.stat().st_mtime > 5 * 86400:
                return {}
            frame = pd.read_parquet(path)
        except Exception as exc:  # noqa: BLE001
            logger.info("板块截面缓存读取失败：%s", brief(exc, BRIEF_TIGHT))
            return {}
        if frame is None or len(frame) == 0 or "name" not in frame.columns:
            return {}
        payload: dict[str, dict[str, Any]] = {}
        for _, row in frame.iterrows():
            name = str(row.get("name") or "").strip()
            if name:
                payload[name] = {key: (None if pd.isna(value) else value)
                                 for key, value in row.items() if key != "name"}
        return payload

    async def sector_snapshot(self) -> dict[str, dict[str, Any]]:
        """板块**截面**（最新交易日全量）：`{板块名: {code, net, net_rate, ...}}`。

        口径分层（用户要"尽可能盘中实时"）：
          - **主**：Tushare `moneyflow_ind_dc` —— 权威、带大单/超大单，但**日频**
            （实测最新到最近一个已结算交易日）；
          - **辅**：盘中若同花顺即时接口可用，则把它的**当日净额**覆盖到同名的板块上
            （`net_realtime=True` 标记），这样点击刷新能看到今天的钱在往哪走；
            同花顺口径是**亿元**，与 Tushare 的元不同，转换在这里做掉。
        """
        if not self._sector_snapshot_dirty and self._sector_snapshot is not None:
            saved_at, payload = self._sector_snapshot
            if (time.monotonic() - saved_at) < REALTIME_TTL:
                return payload
        frame = await asyncio.to_thread(self._sector_frame_sync)
        payload = self.payload_from_frame(frame)
        # 盘中实时覆盖（同花顺即时；失败只是没有这一层，不影响主口径）
        state = _session_state()
        if state in ("trading", "lunch_break", "call_auction"):
            ths = await self._ths_intraday()
            if ths:
                by_norm = {_norm_name(name): name for name in payload}
                for ths_name, info in ths.items():
                    target = payload.get(ths_name) or payload.get(
                        by_norm.get(_norm_name(ths_name), ""))
                    if target is None or info.get("net_yi") is None:
                        continue
                    target["net"] = float(info["net_yi"]) * YI
                    target["net_realtime"] = True
                    if info.get("change_pct") is not None:
                        target["pct_change"] = info["change_pct"]
                self._realtime_note = (
                    f"盘中净额已用同花顺即时口径覆盖 {sum(1 for v in payload.values() if v.get('net_realtime'))} 个同名板块")
        self._sector_snapshot = (time.monotonic(), payload)
        self._sector_snapshot_dirty = False
        return payload

    async def sector_history_tushare(
        self, name: str, code: str = "", *, window_days: int = 10, force: bool = False,
    ) -> FlowEntity | None:
        """单个板块近 N 日净额序列（Tushare `moneyflow_ind_dc` 按 `ts_code` 查）。

        实测（2026-09-17）：`{"ts_code": "BK1036.DC"}` 一次返回 **730 行**日频历史，
        单次调用即拿到 10 日窗口；比东财逐板块接口快且稳。
        """
        key = f"ts:{code or name}"
        cached = self._sector_hist.get(key)
        if not force and cached is not None:
            saved_at, entity = cached
            if (time.monotonic() - saved_at) < SECTOR_HISTORY_TTL:
                return entity
        if not code:
            return None
        client = self._tushare()
        if client is None:
            return None
        try:
            frame = await asyncio.to_thread(
                lambda: client.call("moneyflow_ind_dc", ts_code=code))
        except Exception as exc:  # noqa: BLE001
            logger.info("Tushare 板块历史不可用(%s)：%s", name, brief(exc, BRIEF_TIGHT))
            return None
        if frame is None or len(frame) == 0:
            return None
        frame = frame.copy()
        frame["trade_date"] = frame["trade_date"].astype(str)
        frame = frame.sort_values("trade_date").tail(window_days)
        entity = FlowEntity(kind="sector", code=name, name=name, available=True)
        entity.series = [
            FlowPoint(date=str(row["trade_date"]),
                      net=_finite(row.get("net_amount")),
                      buy_lg=_finite(row.get("buy_lg_amount")),
                      buy_elg=_finite(row.get("buy_elg_amount")),
                      close=_finite(row.get("close")))
            for _, row in frame.iterrows()
        ]
        entity.series = [point for point in entity.series if point.net is not None]
        entity.available = bool(entity.series)
        entity.data_source = "Tushare moneyflow_ind_dc（东财板块口径，日频，单位元）"
        if not entity.available:
            entity.gap = "该板块近日无资金流数据"
        self._sector_hist[key] = (time.monotonic(), entity)
        return entity

    # ==================== 板块：东财 akshare 口径（备用） ====================

    async def sector_members(self, name: str) -> list[dict[str, Any]]:
        """板块成分股（概念 → 行业各试一次）。

        数据源：东财 `stock_board_concept_cons_em` / `stock_board_industry_cons_em`。
        **同花顺这一版 akshare 没有成分股函数**（实测 1.18.94：`dir(ak)` 里只有
        `stock_board_concept_index_ths` / `_info_ths` / `_name_ths` / `_summary_ths`，
        没有 `cons_ths`），所以成分股走东财，而**即时资金流仍走同花顺** ——
        两者各自独立失败，谁挂了都不影响另一条。

        返回按流通市值降序（调用方据此截断），失败返回空列表。
        """
        cached = self._members.get(name)
        if cached is not None and (time.monotonic() - cached[0]) < MEMBERS_TTL:
            return cached[1]
        payload = await run_json_subprocess(
            """
import akshare as ak
name = __NAME__
out = {"attempts": []}
for label, fn in (("concept", ak.stock_board_concept_cons_em),
                  ("industry", ak.stock_board_industry_cons_em)):
    try:
        df = fn(symbol=name)
    except Exception as exc:
        out["attempts"].append(f"{label}: {type(exc).__name__}: {exc}")
        continue
    rows = []
    for _, row in df.iterrows():
        rows.append({str(k): (None if v is None else v) for k, v in row.items()})
    out["rows"] = rows
    out["label"] = label
    break
__emit(out)
""".replace("__NAME__", repr(name)),
            timeout=90.0, label=f"资金流-板块成分({name})")
        rows: list[dict[str, Any]] = []
        for row in (payload or {}).get("rows") or []:
            code = str(row.get("代码") or row.get("股票代码") or "").strip()
            if code.isdigit() and len(code) == 6:
                rows.append({"code": code,
                             "name": str(row.get("名称") or "").strip(),
                             "circ_mv": _finite(row.get("流通市值"))})
        if rows:
            rows.sort(key=lambda item: -(item.get("circ_mv") or 0.0))
        else:
            logger.info("板块成分股不可用(%s)：%s", name,
                        str((payload or {}).get("attempts"))[:160])
        self._members[name] = (time.monotonic(), rows)
        return rows

    async def sector_history_local(
        self, name: str, *, window_days: int = 10, max_members: int = 60,
    ) -> FlowEntity | None:
        """**本地聚合**口径的板块历史：成分股（同花顺）× 个股资金流（Tushare）求和。

        为什么需要这条兜底：东财的板块资金流历史接口实测经常整段不可用
        （RemoteDisconnected / 超时），而"板块近 10 日净流入走势"是本功能的核心需求之一。
        用「该板块成分股的个股资金流之和」来定义板块资金流，是**可解释、可复现**的
        真实口径（与东财的定义不同，因此必须标注 `data_source` 说明差异），
        而且完全走本地仓库 —— 零网络、毫秒级。

        刻意只取流通市值前 `max_members` 只：板块动辄几百只票，
        资金流本来就被头部主导；同时也能把一次查询的规模压住。
        """
        members = await self.sector_members(name)
        if not members:
            return None
        codes = [item["code"] for item in members[:max_members]]
        series = await self.stock_series(codes, window_days=window_days)
        if not series:
            return None
        totals: dict[str, float] = {}
        for payload in series.values():
            for point in payload.get("points") or []:
                net = point.get("net")
                if net is None:
                    continue
                totals[str(point["date"])] = totals.get(str(point["date"]), 0.0) + float(net)
        if not totals:
            return None
        points = [FlowPoint(date=date, net=value)
                  for date, value in sorted(totals.items())][-window_days:]
        entity = FlowEntity(kind="sector", code=name, name=name, available=True)
        entity.series = points
        entity.data_source = (
            f"本地聚合：同花顺板块成分（流通市值前 {min(len(members), max_members)} 只）"
            "× Tushare 个股资金流之和")
        entity.notes.append(
            f"该板块共 {len(members)} 只成分股，走势按流通市值前 "
            f"{min(len(members), max_members)} 只的净额求和（与东财板块口径不同，"
            "数值不可与东财直方图逐一对应）")
        return entity

    async def sector_history(self, name: str, *,
                             window_days: int = 10,
                             force: bool = False) -> FlowEntity:
        """单个板块近 N 日净额序列：东财优先，失败或为空时用本地聚合伙底。

        逐板块独立缓存：一个板块取不到不该让整张榜空白。
        """
        cached = self._sector_hist.get(name)
        if not force and cached is not None:
            saved_at, entity = cached
            if (time.monotonic() - saved_at) < SECTOR_HISTORY_TTL:
                return entity
        entity = await self._sector_history_eastmoney(name, window_days=window_days)
        if not entity.available:
            local = await self.sector_history_local(name, window_days=window_days)
            if local is not None and local.available:
                # 保留东财失败原因，便于排查"为什么这条线是本地口径"
                local.notes.append(f"东财口径不可用：{(entity.gap or '未知原因')[:120]}")
                entity = local
        self._sector_hist[name] = (time.monotonic(), entity)
        return entity

    async def _sector_history_eastmoney(
        self, name: str, *, window_days: int = 10,
    ) -> FlowEntity:
        """东财板块资金流历史（概念 → 行业各试一次）。"""
        payload = await run_json_subprocess(
            """
import akshare as ak
name = __NAME__
out = {}
for label, fn in (("concept", ak.stock_concept_fund_flow_hist),
                  ("industry", ak.stock_sector_fund_flow_hist)):
    try:
        df = fn(symbol=name)
    except Exception as exc:
        out[label] = {"error": f"{type(exc).__name__}: {exc}"}
        continue
    rows = []
    for _, row in df.tail(30).iterrows():
        rows.append({str(k): (None if v is None else (v if isinstance(v, str) else float(v)))
                     for k, v in row.items()})
    out[label] = {"rows": rows}
    break
__emit(out)
""".replace("__NAME__", repr(name)),
            timeout=60.0, label=f"资金流-板块历史({name})")
        entity = FlowEntity(kind="sector", code=name, name=name)
        if not payload:
            entity.gap = "板块资金流历史取数失败（子进程超时/崩溃或东财接口不可用）"
            return entity
        rows: list[dict[str, Any]] = []
        source = ""
        for label in ("concept", "industry"):
            block = payload.get(label) or {}
            if block.get("rows"):
                rows = block["rows"]
                source = ("东财概念资金流历史" if label == "concept"
                          else "东财行业资金流历史")
                break
            if block.get("error"):
                entity.gap = f"东财接口报错：{str(block['error'])[:120]}"
        if not rows:
            entity.gap = entity.gap or "东财未返回该板块的历史资金流（名称口径可能不同）"
            return entity
        points: list[FlowPoint] = []
        for row in rows:
            date = str(row.get("日期") or row.get("date") or "").strip()[:10]
            if not date:
                continue
            net = _finite(row.get("主力净流入-净额"))
            if net is None:
                net = _finite(row.get("净额"))
                if net is not None:
                    net *= YI          # 同花顺口径是亿元
            points.append(FlowPoint(
                date=date, net=net,
                buy_lg=_finite(row.get("大单净流入-净额")),
                buy_elg=_finite(row.get("超大单净流入-净额")),
                close=_finite(row.get("收盘价")),
            ))
        points = [p for p in points if p.net is not None][-window_days:]
        entity.available = bool(points)
        entity.data_source = source
        entity.series = points
        entity.unit = "元"
        if not entity.available:
            entity.gap = "该板块历史资金流为空（可能不是东财口径的板块名）"
        return entity

    async def sector_history_many(
        self, names: list[str], *, window_days: int = 10,
    ) -> dict[str, FlowEntity]:
        """并发取多个板块的历史（每个独立失败，互不影响）。"""
        if not names:
            return {}
        tasks = [self.sector_history(name, window_days=window_days)
                 for name in names]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        output: dict[str, FlowEntity] = {}
        for name, result in zip(names, results, strict=True):
            if isinstance(result, BaseException):
                entity = FlowEntity(kind="sector", code=name, name=name,
                                    gap=f"取数异常：{type(result).__name__}")
            else:
                entity = result
            output[name] = entity
        return output

    # ==================== 个股：盘中实时净额（东财，可选增强） ====================

    async def stock_realtime(self, codes: list[str]) -> dict[str, dict[str, Any]]:
        """个股当日实时资金流净额（东财，按票逐个取；失败返回空）。"""
        if not codes:
            return {}
        payload = await run_json_subprocess(
            """
import akshare as ak
codes = __CODES__
out = {}
for code in codes:
    market = "sh" if code[0] in ("6", "9") else "sz"
    try:
        df = ak.stock_individual_fund_flow(stock=code, market=market)
    except Exception as exc:
        out[code] = {"error": f"{type(exc).__name__}: {exc}"}
        continue
    if df is None or len(df) == 0:
        out[code] = {"error": "empty"}
        continue
    rows = []
    for _, row in df.tail(15).iterrows():
        rows.append({str(k): (None if v is None else (v if isinstance(v, str) else float(v)))
                     for k, v in row.items()})
    out[code] = {"rows": rows}
__emit(out)
""".replace("__CODES__", repr([str(code) for code in codes])),
            timeout=120.0, label="资金流-个股实时(东财)")
        result: dict[str, dict[str, Any]] = {}
        for code, block in (payload or {}).items():
            if not isinstance(block, dict) or not block.get("rows"):
                continue
            points = []
            for row in block["rows"]:
                date = str(row.get("日期") or "").strip()[:10]
                if not date:
                    continue
                points.append(FlowPoint(
                    date=date,
                    net=_finite(row.get("主力净流入-净额")),
                    buy_lg=_finite(row.get("大单净流入-净额")),
                    sell_lg=None,
                    buy_elg=_finite(row.get("超大单净流入-净额")),
                    close=_finite(row.get("收盘价")),
                ).to_dict())
            if points:
                result[code] = {"points": points}
        return result


def merge_stock_points(
    daily: list[dict[str, Any]], realtime: list[dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], str]:
    """把「东财实时序列」叠加到「Tushare 日频序列」上。

    规则：**同一天以实时为准**（盘中那天的东财值就是"到今天此刻"的进行中口径），
    历史日期仍用 Tushare（权威、单位已归一）。返回 (序列, 说明)。
    """
    if not realtime:
        return daily, ""
    by_date = {str(point.get("date")): dict(point) for point in daily}
    replaced = 0
    for point in realtime:
        date = str(point.get("date"))
        if not date:
            continue
        if date in by_date:
            by_date[date].update({key: value for key, value in point.items()
                                  if value is not None and key != "date"})
            replaced += 1
        else:
            by_date[date] = dict(point)
    merged = sorted(by_date.values(), key=lambda item: str(item.get("date")))
    return merged, (f"当日({replaced}处)以盘中实时口径覆盖" if replaced else "")


# ============================================================================
# 盘口补充：腾讯实时快照 + 东财涨停池（"流通市值 / 今日涨幅 / 涨停原因"三列）
# ============================================================================


async def fetch_tencent_snapshot(codes: list[str]) -> dict[str, dict[str, Any]]:
    """腾讯批量快照：`{code: {name, price, change_pct, circ_mv, total_mv}}`（**盘中实时**）。

    为什么需要它：本地仓库 `daily_basic.circ_mv` 与 `daily.pct_chg` 都是**日频**，
    收盘后才更新。资金流监控的"流通市值 / 今日涨幅"两列要盘中就能刷新，
    因此用腾讯 `qt.gtimg.cn` 一次请求批量取（实测一次可带上百只票，0.1~0.3 秒）。

    取不到就返回空 dict —— 调用方回落到仓库的日频值，**不编造**。
    """
    if not codes:
        return {}
    symbols = []
    for code in codes:
        digits = str(code).strip()[:6]
        if len(digits) != 6 or not digits.isdigit():
            continue
        prefix = "sh" if digits.startswith(("6", "9")) else "sz"
        symbols.append(f"{prefix}{digits}")
    if not symbols:
        return {}
    import httpx

    result: dict[str, dict[str, Any]] = {}
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
        "Referer": "https://gu.qq.com/",
    }
    try:
        async with httpx.AsyncClient(timeout=20.0, headers=headers) as client:
            response = await client.get(
                "https://qt.gtimg.cn/q=" + ",".join(symbols))
        response.encoding = "gbk"
        text = response.text
    except Exception as exc:  # noqa: BLE001 备用源失败不该打断主链路
        logger.info("腾讯快照不可用：%s", brief(exc, BRIEF_TIGHT))
        return {}
    for line in text.split("\n"):
        if "=" not in line or '"' not in line:
            continue
        try:
            payload = line.split('="', 1)[1].rstrip('";')
        except IndexError:
            continue
        fields = payload.split("~")
        if len(fields) < 46:
            continue
        code = fields[2].strip() if len(fields) > 2 else ""
        if len(code) != 6:
            continue
        result[code] = {
            "name": fields[_TX_FIELD_NAME].strip(),
            "price": _finite(fields[_TX_FIELD_PRICE]),
            "pre_close": _finite(fields[_TX_FIELD_PRE_CLOSE]),
            "change_pct": _finite(fields[_TX_FIELD_CHANGE_PCT]),
            # 腾讯给的是**亿元**，统一换算成元（与项目其它字段一致）
            "circ_mv": ((_finite(fields[_TX_FIELD_FLOAT_MV]) or 0.0) * YI
                        if _finite(fields[_TX_FIELD_FLOAT_MV]) is not None else None),
            "total_mv": ((_finite(fields[_TX_FIELD_TOTAL_MV]) or 0.0) * YI
                         if _finite(fields[_TX_FIELD_TOTAL_MV]) is not None else None),
            "source": "腾讯行情(qt.gtimg.cn)",
        }
    logger.info("腾讯快照：%d/%d 只（盘中实时涨幅与流通市值）", len(result), len(symbols))
    return result


async def fetch_limit_up_pool(trade_date: str = "") -> dict[str, dict[str, Any]]:
    """涨停池（含**涨停原因/所属行业**）：`{code: {theme, streak, float_mv, ...}}`。

    ## 为什么用 akshare 而不是东财 push2ex

    实测对比（2026-09-17）：

    | 数据源 | 支持指定历史日期 | 有"原因/行业" | 稳定性 |
    |---|---|---|---|
    | `ak.stock_zt_pool_em(date=...)` | ✅ 实测 20260914→55 只、20260915→32 只 | ✅ `所属行业` | 走子进程，稳 |
    | 东财 `push2ex` | ❌ `date` 参数无效，永远只给当天 | ✅ `hybk` | 当天曾返回 0 条 |

    「昨日涨停」需要**历史**那一份，push2ex 给不了（它只会回当天，
    用它会把今天的涨停当成昨天的）；akshare 能按日期取，因此作为主口径。

    走 `run_json_subprocess` 子进程隔离（akshare/东财在本项目里一律隔离，
    避免原生库崩溃带倒主进程）。
    """
    payload = await run_json_subprocess(
        """
import akshare as ak
date = __DATE__
out = {"rows": [], "date": date}
try:
    df = ak.stock_zt_pool_em(date=date)
except Exception as exc:
    out["error"] = f"{type(exc).__name__}: {exc}"
else:
    for _, row in df.iterrows():
        out["rows"].append({str(k): (None if v is None else v)
                            for k, v in row.items()})
__emit(out)
""".replace("__DATE__", repr(trade_date or "")),
        timeout=90.0, label=f"资金流-涨停池({trade_date or '今日'})")
    rows = (payload or {}).get("rows") or []
    if not rows:
        logger.info("涨停池为空或不可用：%s",
                    str((payload or {}).get("error") or "无数据")[:120])
        return {}
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        code = str(row.get("代码") or "").strip().zfill(6)
        if len(code) != 6 or not code.isdigit():
            continue
        streak = _finite(row.get("连板数"))
        result[code] = {
            "theme": str(row.get("所属行业") or "").strip(),
            "streak": int(streak) if streak is not None else None,
            "price": _finite(row.get("最新价")),
            "change_pct": _finite(row.get("涨跌幅")),
            "float_mv": _finite(row.get("流通市值")),
            "turnover_rate": _finite(row.get("换手率")),
            "sealed_fund": _finite(row.get("封板资金")),
            "broken_times": row.get("炸板次数"),
            "pool_date": str(trade_date or ""),
            "source": "东财涨停池(akshare)",
        }
    logger.info("涨停池：%s 共 %d 只（含涨停原因/所属行业）", trade_date or "今日", len(result))
    return result
