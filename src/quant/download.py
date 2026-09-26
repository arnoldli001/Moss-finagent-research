"""Tushare 数据下载层：接口 → 规范化 → 分区落盘（增量、可断点续传）。

覆盖 35 因子需要的全部数据集：

| 数据集 | Tushare 接口 | 分区键 | 用途 |
|--------|--------------|--------|------|
| `daily` | `daily` | 交易日 | 成交额（Amihud/流动性）、涨跌幅校验 |
| `daily_basic` | `daily_basic` | 交易日 | 估值/市值/股本/换手/量比/涨跌停状态 |
| `adj_factor` | `adj_factor` | 交易日 | 复权因子（与 QMT 前复权对拍） |
| `moneyflow` | `moneyflow` | 交易日 | 资金净流入率 |
| `stk_limit` | `stk_limit` | 交易日 | 涨跌停价（回测可行性） |
| `suspend_d` | `suspend_d` | 交易日 | 停牌（不可成交） |
| `bak_daily` | `bak_daily` | 交易日 | 内盘/外盘、振幅、活跃度等特色字段 |
| `fina_indicator_vip` | `fina_indicator_vip` | 报告期 | **全市场财务横截面（PIT 骨架）** |
| `index_daily` | `index_daily` | 交易日 | 相对强度 RS 的基准 |
| `stock_basic` | `stock_basic` | static | 名录/行业/上市日期 |
| `namechange` | `namechange` | static | **历史名称（ST/*ST 判定）** |
| `trade_cal` | `trade_cal` | static | 交易日历（决定要拉哪些分区） |
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from src.quant.dataset_store import DEFAULT_ROOT, DatasetStore, SyncResult
from src.quant.tushare_source import (
    DAILY_BASIC_MAP,
    DAILY_MAP,
    FINA_MAP,
    LIMIT_MAP,
    MONEYFLOW_MAP,
    NAME_CHANGE_MAP,
    TushareClient,
    normalize,
    to_code,
)

logger = logging.getLogger(__name__)

# 需要按交易日拉取的数据集 → (接口名, 字段映射, 备注)
DAILY_DATASETS: dict[str, tuple[str, dict[str, str]]] = {
    "daily": ("daily", DAILY_MAP),
    "daily_basic": ("daily_basic", DAILY_BASIC_MAP),
    "adj_factor": ("adj_factor", {"ts_code": "ts_code", "trade_date": "trade_date",
                                  "adj_factor": "adj_factor"}),
    "moneyflow": ("moneyflow", MONEYFLOW_MAP),
    "stk_limit": ("stk_limit", LIMIT_MAP),
    "suspend_d": ("suspend_d", {"ts_code": "ts_code", "trade_date": "trade_date",
                                "suspend_type": "suspend_type",
                                "suspend_timing": "suspend_timing"}),
    "bak_daily": ("bak_daily", {
        "ts_code": "ts_code", "trade_date": "trade_date", "close": "bak_close",
        "turn_over": "bak_turnover", "vol_ratio": "bak_vol_ratio",
        "swing": "swing", "selling": "selling", "buying": "buying",
        "strength": "strength", "activity": "activity",
        "avg_turnover": "avg_turnover", "attack": "attack",
        "interval_3": "interval_3", "interval_6": "interval_6",
        "float_mv": "bak_float_mv", "total_mv": "bak_total_mv",
        "industry": "industry", "area": "area",
    }),
}

INDEX_CODES = ("000001.SH", "000300.SH", "399001.SZ", "399006.SZ", "000905.SH")


def _year_windows(start: str, end: str) -> list[tuple[str, str]]:
    """把 `[start, end]` 切成按自然年的窗口（用于按公告日区间拉取的接口）。

    端点做了裁剪（首年从 `start` 起、末年到 `end` 止），所以不会多拉数据。
    """
    first, last = int(start[:4]), int(end[:4])
    if last < first:
        return [(start, end)]
    windows: list[tuple[str, str]] = []
    for year in range(first, last + 1):
        low = start if year == first else f"{year}0101"
        high = end if year == last else f"{year}1231"
        windows.append((low, high))
    return windows


@dataclass
class DownloadReport:
    """一次下载任务的汇总（按数据集分组）。"""

    results: dict[str, SyncResult]
    universe: str = "a_share"

    def as_dict(self) -> dict[str, Any]:
        return {"universe": self.universe,
                "datasets": {name: result.as_dict()
                             for name, result in self.results.items()},
                "total_rows": sum(result.rows for result in self.results.values()),
                "total_failed": sum(len(result.failed) for result in self.results.values())}

    def summary_lines(self) -> list[str]:
        lines = []
        for name, result in self.results.items():
            lines.append(
                f"  {name:20s} 新拉 {len(result.fetched):4d} 跳过 "
                f"{len(result.skipped):4d} 失败 {len(result.failed):3d} 行数 {result.rows}")
        return lines


class TushareDownloader:
    """把 Tushare 接口数据同步到本地分区缓存。"""

    def __init__(self, client: TushareClient | None = None, *,
                 root: str | Path = DEFAULT_ROOT,
                 universe: str = "a_share") -> None:
        self.client = client or TushareClient()
        self.root = root
        self.universe = universe
        self._stores: dict[str, DatasetStore] = {}

    # ---------- store 管理 ----------

    def store(self, dataset: str) -> DatasetStore:
        if dataset not in self._stores:
            self._stores[dataset] = DatasetStore(
                dataset, root=self.root, universe=self.universe)
        return self._stores[dataset]

    # ---------- 交易日历 ----------

    async def calendar(self, start: str, end: str) -> list[str]:
        """交易日历（缓存为 static 分区），**按升序返回**。

        两个必须注意的点（都踩过）：

        1. Tushare 的 `trade_cal` 默认倒序，不排一下 `days[-N:]`（取最近 N 天）会取错端；
        2. **缓存必须记住覆盖区间**：只存"上次请求的那段"，下次请求更宽区间时
           会被静默缩窄（实测：先冒烟测了 9/1~9/15，随后请求 2026 全年，
           结果只下了 11 天却报"成功"）。因此这里把 start/end 写进 manifest，
           区间不够就重新拉取并扩展。
        """
        store = self.store("trade_cal")
        entry = store.manifest().get("static", {})
        cached_start, cached_end = entry.get("start", ""), entry.get("end", "")
        need_fetch = (
            not store.has("static")
            or not cached_start or not cached_end
            or cached_start > start or cached_end < end
        )
        if need_fetch:
            low = min(start, cached_start) if cached_start else start
            high = max(end, cached_end) if cached_end else end
            frame = await self.client.acall(
                "trade_cal", exchange="SSE", start_date=low, end_date=high,
                is_open="1")
            if frame is None or len(frame) == 0:
                logger.warning("交易日历为空，退化为按工作日请求")
                return []
            frame = frame.rename(columns={"cal_date": "trade_date"})
            store.write("static", frame[["trade_date"]],
                        meta={"start": low, "end": high})
        cached = store.read("static")
        days = sorted({str(day) for day in cached["trade_date"].tolist()})
        return [day for day in days if start <= day <= end]

    async def stock_basic(self) -> pd.DataFrame:
        store = self.store("stock_basic")
        if not store.has("static"):
            frame = await self.client.acall(
                "stock_basic", exchange="", list_status="L",
                fields="ts_code,name,industry,area,market,list_date,exchange")
            if frame is None or len(frame) == 0:
                return pd.DataFrame()
            normalized = frame.assign(code=to_code(frame["ts_code"]))
            store.write("static", normalized)
        return store.read("static")

    # ---------- 历史名称（ST 判定） ----------

    async def namechange(self, start: str = "20060101",
                         end: str | None = None, *,
                         force: bool = False) -> SyncResult:
        """拉取全部历史名称变更 → `namechange/static` 分区。

        供 `st_status.StStatus` 判定"某只票在某一天是不是 ST"。

        ## 为什么按**公告日**分年拉

        `namechange` 的 `start_date/end_date` 参数过滤的是 **ann_date**
        （窗口内公告的变更，即使生效日落在窗口之外 —— 实测窗口
        `20140101~20140131` 返回了一条 `start_date=20140305` 的记录）。
        所以按公告日逐年拉取即可覆盖全量，分年只是为了让单次响应小、
        失败重试便宜。每只票至少有一行（初始名称），实测全市场约 1.5 万行。

        ## 为什么是 static 分区（而不是按交易日）

        名称是**每只票一条时间轴**，不是每日横截面。跨年窗口可能返回
        同一条记录（年份边界上的公告），所以写完前按
        `(ts_code, start_date, name)` 去重。
        """
        store = self.store("namechange")
        end = end or time.strftime("%Y%m%d")
        entry = store.manifest().get("static", {})
        if (store.has("static") and not force
                and entry.get("start", "") <= start
                and entry.get("end", "") >= end):
            return SyncResult(dataset="namechange", requested=0,
                              skipped=["static(已覆盖)"],
                              rows=int(entry.get("rows", 0)))

        frames: list[pd.DataFrame] = []
        windows = _year_windows(start, end)
        for low, high in windows:
            frame = await self.client.acall(
                "namechange", start_date=low, end_date=high,
                fields="ts_code,name,start_date,end_date,ann_date,change_reason")
            if frame is not None and len(frame):
                frames.append(frame)
        if not frames:
            logger.warning("namechange 在 %s~%s 返回空表", start, end)
            return SyncResult(dataset="namechange", requested=len(windows),
                              failed={"all": "返回空表"})

        combined = pd.concat(frames, ignore_index=True)
        normalized = normalize(combined, NAME_CHANGE_MAP)
        normalized = normalized.assign(code=to_code(normalized["ts_code"]))
        normalized = normalized.drop_duplicates(
            subset=["ts_code", "start_date", "name"]).reset_index(drop=True)
        store.write("static", normalized,
                    meta={"start": start, "end": end,
                          "rows": int(len(normalized))})
        return SyncResult(dataset="namechange", requested=len(windows),
                          fetched=[f"{low}~{high}" for low, high in windows],
                          rows=int(len(normalized)))

    # ---------- 按交易日的数据集 ----------

    async def sync_daily(self, dataset: str, days: list[str], *,
                         force: bool = False, concurrency: int = 3) -> SyncResult:
        api, mapping = DAILY_DATASETS[dataset]
        store = self.store(dataset)

        async def fetch(day: str) -> pd.DataFrame:
            frame = await self.client.acall(api, trade_date=day)
            normalized = normalize(frame, mapping)
            if len(normalized) and "ts_code" in normalized.columns:
                normalized = normalized.assign(code=to_code(normalized["ts_code"]))
                normalized = self._filter_universe(normalized)
            return normalized

        return await store.sync(days, fetch=fetch, force=force,
                                concurrency=concurrency)

    async def sync_index(self, days: list[str], *,
                         force: bool = False) -> SyncResult:
        """指数日线（相对强度 RS 的基准）。"""
        store = self.store("index_daily")

        async def fetch(day: str) -> pd.DataFrame:
            frames = []
            for ts_code in INDEX_CODES:
                frame = await self.client.acall(
                    "index_daily", ts_code=ts_code,
                    start_date=day, end_date=day)
                if frame is not None and len(frame):
                    frames.append(frame)
            if not frames:
                return pd.DataFrame()
            merged = pd.concat(frames, ignore_index=True)
            return normalize(merged, {
                "ts_code": "ts_code", "trade_date": "trade_date",
                "open": "open", "high": "high", "low": "low", "close": "close",
                "pct_chg": "pct_chg", "vol": "volume_lot", "amount": "amount"})

        return await store.sync(days, fetch=fetch, force=force, concurrency=1)

    # ---------- 按报告期的财务横截面 ----------

    async def sync_fina(self, periods: list[str], *,
                        force: bool = False, concurrency: int = 2) -> SyncResult:
        store = self.store("fina_indicator_vip")

        async def fetch(period: str) -> pd.DataFrame:
            # **必须显式传 fields**：Tushare 只返回文档里标"默认显示=Y"的字段，
            # 像 ocf_to_profit（经营现金流/营业利润）、inv_turn 这些标 N 的
            # 不指定就拿不到 —— 实测后果是"经营现金流/净利润"因子整列空值。
            requested = sorted(set(FINA_MAP) | {"ts_code", "ann_date", "end_date"})
            frame = await self.client.acall(
                "fina_indicator_vip", period=period,
                fields=",".join(requested))
            normalized = normalize(frame, FINA_MAP)
            if len(normalized) and "ts_code" in normalized.columns:
                normalized = normalized.assign(code=to_code(normalized["ts_code"]))
                normalized = self._filter_universe(normalized)
                normalized = normalized.assign(report_period=period)
            return normalized

        return await store.sync(periods, fetch=fetch, force=force,
                                concurrency=concurrency)

    # ---------- 股票池过滤 ----------

    def _filter_universe(self, frame: pd.DataFrame) -> pd.DataFrame:
        if self.universe == "all" or "code" not in frame.columns:
            return frame
        from src.quant.fundamental_source import filter_universe

        filtered, _ = filter_universe(frame, universe=self.universe)
        return filtered

    # ---------- 一键下载 ----------

    async def download(
        self, start: str, end: str, *,
        datasets: list[str] | None = None,
        periods: list[str] | None = None,
        include_index: bool = True,
        include_fina: bool = True,
        include_namechange: bool = True,
        force: bool = False,
        days: list[str] | None = None,
        progress: Any = None,
    ) -> DownloadReport:
        """按区间下载全部数据集（增量）。`progress` 可传一个 callable 打进度。

        `days` 显式传入时以它为准（CLI 的 --max-days 走这条路），
        否则按交易日历推导 —— 之前 CLI 里自己切了 days 却没传下来，
        结果 --max-days 静默失效（实测踩过）。
        """
        await self.stock_basic()
        trading_days = list(days) if days is not None else await self.calendar(start, end)
        if not trading_days:
            trading_days = [stamp.strftime("%Y%m%d")
                            for stamp in pd.bdate_range(pd.Timestamp(start),
                                                        pd.Timestamp(end))]
        targets = datasets or list(DAILY_DATASETS)
        report = DownloadReport(results={}, universe=self.universe)

        for dataset in targets:
            if dataset not in DAILY_DATASETS:
                raise ValueError(f"未知数据集 {dataset!r}（可选：{list(DAILY_DATASETS)}）")
            if progress:
                progress(f"下载 {dataset}（{len(trading_days)} 个交易日）")
            report.results[dataset] = await self.sync_daily(
                dataset, trading_days, force=force)

        if include_index:
            if progress:
                progress(f"下载 index_daily（{len(trading_days)} 个交易日）")
            report.results["index_daily"] = await self.sync_index(
                trading_days, force=force)

        if include_fina and periods:
            if progress:
                progress(f"下载 fina_indicator_vip（{len(periods)} 个报告期）")
            report.results["fina_indicator_vip"] = await self.sync_fina(
                periods, force=force)

        if include_namechange:
            # 历史名称：ST 剔除的数据源。**必须跟随区间下载**，
            # 否则"剔除 ST"这个开关会因为缺数据而静默降级为不剔除。
            if progress:
                progress("下载 namechange（历史名称，ST 判定用）")
            report.results["namechange"] = await self.namechange(
                start, end, force=force)

        return report
