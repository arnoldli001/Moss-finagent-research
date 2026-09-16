"""因子面板装配：把分区缓存拼成「日期 × 代码」宽表，供 35 因子计算使用。

**这是全流程最容易出错的一层**，三个硬约束：

1. **PIT 只在财务上**：财务数据走 `PitPanel.as_of(date)`（按公告日 + 滞后）；
   行情/估值是当日公开数据，直接用当日截面；
2. **不做任何前向填充到未来**：某个交易日缺的股票就是 NaN，绝不 ffill 到未来
   （ffill 会把"昨天有、今天停牌"的数据带到停牌日，回测里表现为"停牌还能成交"）；
3. **单位统一**：金额元、股本股、比率为百分数（由 tushare_source 规范化保证）。

装配结果 `FactorPanels` 暴露：
    price_panel(field)      → DataFrame(index=date, columns=code)
    basic(field)            → 同上（PE/PB/市值/换手/量比…）
    fundamental(date)       → DataFrame(index=code)，按 PIT 取
    fundamental_field(field, dates) → DataFrame(index=date, columns=code)
    index_return(dates)     → Series（基准指数区间收益，用于相对强度）
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from src.quant.dataset_store import DEFAULT_ROOT, DatasetStore
from src.quant.pit import PitPanel

logger = logging.getLogger(__name__)

PRICE_SOURCES: dict[str, tuple[str, str]] = {
    # field → (数据集, 列名)
    "open": ("daily", "open"),
    "high": ("daily", "high"),
    "low": ("daily", "low"),
    "close": ("daily", "close"),
    "volume_lot": ("daily", "volume_lot"),
    "amount": ("daily", "amount"),
}

BASIC_FIELDS = (
    "pe", "pe_ttm", "pb", "ps", "ps_ttm", "dv_ratio", "dv_ttm",
    "turnover_rate", "turnover_rate_f", "volume_ratio",
    "total_share", "float_share", "free_share", "total_mv", "circ_mv",
)
# 注：曾列过 `limit_status`，但 Tushare 的 `daily_basic` 根本不返回它
# （下载下来的分区里就没有这一列），也没有任何因子消费它 —— 涨跌停的真实
# 来源是 `stk_limit` 的 up_limit/down_limit（见下面的 limits）。留着它的
# 唯一效果是让每个字段都去 CSV 回退读一遍 171 个分区去找一个不存在的列。

# 每个数据集在面板里**需要的全部字段**。
#
# 为什么要显式列出来：走库时若按字段逐个 SELECT，`daily_basic` 的 16 个字段
# 就要查 16 次（每次搬 88 万行 × 3 列），实测比"整表读一次再切片"更慢
# （171 日面板 42.5s vs 31.1s）。有了这张表就能一次把需要的列都取回来，
# 之后在内存里切分 —— 与 CSV 路径的做法一致。
BAK_FIELDS = ("swing", "selling", "buying", "strength", "activity",
              "avg_turnover", "attack", "interval_3", "interval_6",
              "bak_vol_ratio", "bak_turnover")
FLOW_FIELDS = ("net_mf_amount", "buy_lg_amount", "sell_lg_amount",
               "buy_elg_amount", "sell_elg_amount")
PRICE_FIELDS = ("open", "high", "low", "close", "volume_lot", "amount")

PANEL_FIELDS: dict[str, tuple[str, ...]] = {
    "daily": PRICE_FIELDS,
    "adj_factor": ("adj_factor",),
    "daily_basic": BASIC_FIELDS,
    "moneyflow": FLOW_FIELDS,
    "stk_limit": ("up_limit", "down_limit"),
    "bak_daily": BAK_FIELDS,
    "index_daily": ("close",),
    "suspend_d": ("suspend_type",),
}


@dataclass
class FactorPanels:
    """因子计算所需的全部面板。"""

    dates: list[str]
    codes: list[str]
    prices: dict[str, pd.DataFrame] = field(default_factory=dict)
    basics: dict[str, pd.DataFrame] = field(default_factory=dict)
    flows: dict[str, pd.DataFrame] = field(default_factory=dict)
    limits: dict[str, pd.DataFrame] = field(default_factory=dict)
    bak: dict[str, pd.DataFrame] = field(default_factory=dict)
    fundamentals: PitPanel | None = None
    index_returns: dict[str, pd.Series] = field(default_factory=dict)
    suspended: set[tuple[str, str]] = field(default_factory=set)
    gaps: list[str] = field(default_factory=list)
    # 每个字段的取数来源（db:sqlite.quant_daily / csv:daily(171 个分区)）。
    # 回测复现性依赖它：不说清"读的是库还是文件"，环境一变结果就无法解释。
    origins: list[str] = field(default_factory=list)

    # ---------- 取用接口 ----------

    def price(self, field_name: str) -> pd.DataFrame:
        return self._get(self.prices, field_name, "行情")

    def basic(self, field_name: str) -> pd.DataFrame:
        return self._get(self.basics, field_name, "估值")

    def flow(self, field_name: str) -> pd.DataFrame:
        return self._get(self.flows, field_name, "资金流")

    def bak_field(self, field_name: str) -> pd.DataFrame:
        return self._get(self.bak, field_name, "备用行情")

    def _get(self, pool: dict[str, pd.DataFrame], name: str,
             label: str) -> pd.DataFrame:
        frame = pool.get(name)
        if frame is None:
            empty = pd.DataFrame(index=self.dates, columns=self.codes, dtype="float64")
            return empty
        return frame.reindex(index=self.dates, columns=self.codes)

    def fundamental_frame(self, date: str) -> pd.DataFrame:
        """某个交易日的 PIT 财务截面（index=code）。缺数据返回空表。"""
        if self.fundamentals is None:
            return pd.DataFrame(index=self.codes)
        snapshot = self.fundamentals.as_of(date)
        return snapshot.reindex(self.codes) if len(snapshot) else snapshot

    def fundamental_field(self, name: str) -> pd.DataFrame:
        """把 PIT 财务字段展开成面板（不做前向填充到未来）。

        **走 `as_of_panel` 批量快路径，不逐日调用 `as_of`。**
        实测（171 交易日 × 4260 只 × 10 因子的一次筛选）：逐日 `as_of` 要
        把 33 万行财务记录"过滤 + 排序 + 按 code 去重"做 171 遍，
        单这一步就占了整轮 127 秒里的 **59.9 秒（47%）** —— 是最大的热点，
        而且完全没必要：`as_of_panel` 用一次 `merge_asof` 就能算出全部截面
        （它的文档里写明与 `as_of` 有专门的对拍用例，两者结果必须一致）。
        """
        if self.fundamentals is None or name not in self.fundamentals.metrics:
            return pd.DataFrame(index=self.dates, columns=self.codes, dtype="float64")
        snapshots = self._fundamental_snapshots()
        rows: dict[str, pd.Series] = {}
        for date in self.dates:
            snapshot = snapshots.get(date)
            if snapshot is not None and len(snapshot) and name in snapshot.columns:
                rows[date] = snapshot[name].reindex(self.codes)
            else:
                rows[date] = pd.Series(index=self.codes, dtype="float64")
        return pd.DataFrame(rows).T.reindex(index=self.dates, columns=self.codes)

    def _fundamental_snapshots(self) -> dict[str, pd.DataFrame]:
        """本面板全部交易日的 PIT 财务截面。

        缓存在 **`PitPanel` 自己身上**（见 `PitPanel._snapshot_cache`），
        不缓存在这里 —— 调用方可能整体替换 `self.fundamentals`，
        缓存在这一层就会返回**过期数据**且不报错（实测踩过：
        财务字段被改后 `peg` 仍按旧值计算，因子单测因此失败）。
        """
        panel = self.fundamentals
        if panel is None or len(panel) == 0:
            return {}
        try:
            return panel.as_of_panel(self.dates)
        except Exception as exc:  # noqa: BLE001 快路径失败退回逐日，宁可慢也不能错
            logger.warning("as_of_panel 快路径失败(%s)，退回逐日 as_of",
                           str(exc)[:100])
            return {date: panel.as_of(date) for date in self.dates}

    def is_suspended(self, date: str, code: str) -> bool:
        return (date, code) in self.suspended

    def summary(self) -> dict[str, Any]:
        return {
            "dates": len(self.dates),
            "codes": len(self.codes),
            "date_range": [self.dates[0], self.dates[-1]] if self.dates else [],
            "price_fields": sorted(self.prices),
            "basic_fields": sorted(self.basics),
            "flow_fields": sorted(self.flows),
            "fundamental_metrics": list(self.fundamentals.metrics)
            if self.fundamentals else [],
            "suspended_cells": len(self.suspended),
            "gaps": list(self.gaps)[:10],
        }


def _wide(store: DatasetStore, value_column: str,
          dates: Sequence[str]) -> pd.DataFrame:
    """把分区缓存（长表）拼成宽表：index=date, columns=code。"""
    frames = []
    for key in dates:
        frame = store.read(key)
        if frame is None or len(frame) == 0 or value_column not in frame.columns:
            continue
        if "code" not in frame.columns:
            continue
        series = frame.set_index("code")[value_column]
        series.name = key
        frames.append(series)
    if not frames:
        return pd.DataFrame()
    wide = pd.concat(frames, axis=1).T
    wide.index.name = "date"
    return wide.sort_index()


_FUNDAMENTAL_CACHE: dict[tuple[str, str, int, str], Any] = {}


def _load_fina_frame(store: DatasetStore, keys: Sequence[str], *,
                     root: str | Path = DEFAULT_ROOT) -> pd.DataFrame:
    """读财务分区：**库优先，CSV 兜底**。

    为什么值得改：财务面板是 106 个季度分区，每次筛选都要读一遍；
    走 CSV 要解压 106 个 gzip 文件（实测约 5 秒），而库里
    `quant_fina_indicator` 一条范围查询就够了。

    这里不按"日期区间"过滤 —— PIT 面板的构造需要**全部历史报告期**，
    因为 `as_of` 要按公告日回看。所以走的是整表读，正好适合库。

    `root` 必须由调用方显式传入，**不要从 `store.root` 反推**：
    `DatasetStore.root` 是 `<缓存根>/<universe>/<数据集>`，反推很容易少一层，
    而 SQLite 会在错误的路径上**凭空建一个空库**并"成功连接"，
    然后 `covers()` 查不到表 → 静默回退 CSV。实测就是这样：
    库读没生效、白付 5 秒，却一句日志都没有。
    """
    from src.quant.warehouse import DATASET_TABLES, QuantWarehouse

    warehouse = QuantWarehouse(root=root, universe=store.universe)
    try:
        if warehouse.available() and warehouse.covers("fina_indicator_vip"):
            frame = warehouse.load("fina_indicator_vip")
            if len(frame):
                logger.debug("财务面板走库：%d 行（表 %s）", len(frame),
                             DATASET_TABLES["fina_indicator_vip"][0])
                return frame
        # 用 warning 而不是 debug：库明明可用却没走上，属于值得看一眼的情况
        logger.warning("财务面板未能走库（root=%s，库可用=%s），回退 CSV 分区",
                       root, warehouse.available())
    except Exception as exc:  # noqa: BLE001 库不可用就退回 CSV，不影响正确性
        logger.warning("财务面板读库失败，回退 CSV：%s", str(exc)[:120])
    finally:
        warehouse.close()
    return store.load(list(keys))


def load_fundamental_panel(*, root: str | Path = DEFAULT_ROOT,
                           universe: str = "a_share",
                           gaps: list[str] | None = None) -> PitPanel | None:
    """装配 PIT 财务面板：**优先 Tushare `fina_indicator_vip`**（字段最全，
    含 fcff/毛利率/增速/现金流质量），缺失时回退 AkShare 业绩报表。

    两者都带 `ann_date`，因此都能满足"按公告日对齐"的硬要求；
    优先 Tushare 是因为 35 因子里有 11 个依赖它独有的字段。

    **结果按数据身份缓存**：装配一次要读 107 个分区 + 7.9s 的日期归一化，
    而回测/选股是反复装配面板的循环（换个调仓参数就重建一次），
    数据没变就不该重算。缓存键含分区数与最新分区键，所以补数后自动失效。
    返回的 `PitPanel` 是**共享只读对象**：请勿修改它（`as_of` 返回的是切片）。
    """
    from src.quant.pit import FundamentalStore, PitPanel

    notes = gaps if gaps is not None else []
    tushare_store = DatasetStore("fina_indicator_vip", root=root, universe=universe)
    keys = tushare_store.keys()
    if keys:
        cache_key = (str(root), universe, len(keys), keys[-1])
        cached = _FUNDAMENTAL_CACHE.get(cache_key)
        if cached is not None:
            logger.debug("PIT 财务面板命中缓存（%d 期）", len(keys))
            return cached
        frame = _load_fina_frame(tushare_store, keys, root=root)
        if len(frame):
            panel = PitPanel(frame)
            problems = panel.validate()
            if problems:
                notes.append(f"Tushare 财务面板自检有问题：{problems[:2]}")
            logger.info("PIT 财务面板来源：Tushare fina_indicator_vip（%d 期 / %d 行）",
                        len(keys), len(frame))
            _FUNDAMENTAL_CACHE[cache_key] = panel
            return panel
        notes.append("Tushare 财务分区存在但内容为空")

    try:
        panel = FundamentalStore("data/quant/fundamentals",
                                 universe=universe).load_panel()
    except Exception as exc:  # noqa: BLE001 缺财务不该让整条链路失败
        notes.append(f"基本面面板装配失败：{str(exc)[:120]}")
        return None
    if len(panel) == 0:
        notes.append("基本面 PIT 面板为空（既没有 Tushare 财务横截面，"
                     "也没有 AkShare 业绩报表缓存）")
        return None
    notes.append("PIT 财务面板来源：AkShare 业绩报表（字段较少，"
                 "成长/质量类因子覆盖会偏低）")
    return panel


def _wide_from_long(frame: pd.DataFrame, value_column: str,
                    dates: Sequence[str]) -> pd.DataFrame:
    """长表 → 宽表（MySQL 一次性查回来的结果用这个 reshape）。"""
    if frame is None or len(frame) == 0 or value_column not in frame.columns:
        return pd.DataFrame()
    if "code" not in frame.columns or "trade_date" not in frame.columns:
        return pd.DataFrame()
    subset = frame[["trade_date", "code", value_column]]
    wide = subset.pivot_table(index="trade_date", columns="code",
                              values=value_column, aggfunc="last")
    wide.index.name = "date"
    return wide.reindex(index=[str(day) for day in dates]).sort_index()


def build_panels(
    dates: Sequence[str], *,
    root: str | Path = DEFAULT_ROOT,
    universe: str = "a_share",
    codes: Sequence[str] | None = None,
    fundamentals: PitPanel | None = None,
    source: str = "auto",
) -> FactorPanels:
    """从本地数据装配因子面板（不联网）。

    `source`：`mysql` 优先查 MySQL 仓库（一次查询拿整段区间，最快）；
    `csv` 强制读分区缓存；`auto` = 先 MySQL 后 CSV。

    `codes`：只要这几只票。**会给下游两条路径都省下大量工作**：
    走库时把过滤下推到 SQL（实测快 ~225 倍），走 CSV 时在拼长表后立刻裁剪
    （少做几百万行的 pivot）。只看一只票做单股票回测时，这是 28s → 数秒的差别。
    """
    days = [str(day) for day in dates]
    gaps: list[str] = []
    origins: list[str] = []
    code_filter = ({str(code).split(".")[0].zfill(6) for code in codes}
                   if codes else set())

    def store(dataset: str) -> DatasetStore:
        return DatasetStore(dataset, root=root, universe=universe)

    # 每个数据集**只决定一次**数据来源（库 or 分区文件），之后所有字段共用。
    #
    # 原实现是"每个字段各试一次库、失败后再扫一遍 CSV 分区"：40 天 × 41 个字段
    # 的量级下，光重复解压同一个 CSV.gz 就占了 65% 的时间（实测 26.5s/41.3s），
    # 另外 `keys()` 每次都要对 ~4300 个分区做文件存在性探测（15.7 万次 stat）。
    # 先定位来源、再按字段取，是"库优先"真正省钱的前提。
    backend: dict[str, str] = {}
    warehouse_client: Any = None
    long_cache: dict[tuple[str, str], pd.DataFrame] = {}

    def dataset_backend(dataset: str) -> str:
        if dataset in backend:
            return backend[dataset]
        choice = "csv"
        if source in ("auto", "db", "mysql"):
            nonlocal warehouse_client
            try:
                if warehouse_client is None:
                    from src.quant.warehouse import QuantWarehouse

                    warehouse_client = QuantWarehouse(root=root, universe=universe)
                # 用 covers 而不是 has_rows：入库是顺序进行的，库里随时可能只是
                # 缓存的一个前缀。只看"有没有一行"会让灌到一半的数据集被判为
                # "走库"，回测因此静默丢掉后半段数据。
                if (warehouse_client.available()
                        and warehouse_client.covers(dataset, days[0], days[-1])):
                    choice = "db"
            except Exception as exc:  # noqa: BLE001 库不可用就走 CSV
                logger.debug("探测 %s 的仓库来源失败：%s", dataset, str(exc)[:100])
                choice = "csv"
        backend[dataset] = choice
        return choice

    db_cache: dict[str, pd.DataFrame] = {}

    def db_frame(dataset: str) -> pd.DataFrame:
        """按数据集取一次库（缓存键是**数据集**，不是字段）。

        缓存键必须是数据集：按字段缓存会让 `daily_basic` 的 16 个字段
        各发起一次 88 万行的查询，比整表读一次慢得多。
        """
        if dataset in db_cache:
            return db_cache[dataset]
        frame = pd.DataFrame()
        if dataset_backend(dataset) == "db":
            from src.quant.warehouse import DATASET_TABLES

            date_column = DATASET_TABLES[dataset][2]
            wanted = [date_column, "code", *PANEL_FIELDS.get(dataset, ())]
            try:
                available = set(warehouse_client.columns_of(dataset))
                columns = [name for name in wanted if name in available]
                # **股票池过滤下推到 SQL**：实测按代码筛选比取全市场再筛快 ~225 倍
                # （250 日区间 10.3ms vs 2313ms），这是"只看一只票"能秒开的关键。
                frame = warehouse_client.load(
                    dataset, start=days[0], end=days[-1],
                    codes=list(code_filter) if code_filter else None,
                    columns=columns or None, order=False)
                if len(frame):
                    origins.append(
                        f"{dataset}←db:{warehouse_client.config.dialect}"
                        f".{DATASET_TABLES[dataset][0]}({len(columns)} 列)")
            except Exception as exc:  # noqa: BLE001 取失败就退回分区文件
                logger.debug("仓库取 %s 失败，回退 CSV：%s",
                             dataset, str(exc)[:100])
                frame = pd.DataFrame()
        db_cache[dataset] = frame
        return frame

    def field_long(dataset: str, field: str) -> pd.DataFrame:
        """走库时**按数据集一次取回需要的列**，之后在内存里切字段。"""
        cache_key = (dataset, field)
        if cache_key in long_cache:
            return long_cache[cache_key]
        frame = db_frame(dataset)
        long_cache[cache_key] = frame
        return frame

    def field_wide(dataset: str, field: str) -> pd.DataFrame:
        long = field_long(dataset, field)
        if len(long):
            wide = _wide_from_long(long, field, days)
            if not wide.empty:
                return wide
        long = dataset_frame(dataset)
        if len(long):
            wide = _wide_from_long(long, field, days)
            if not wide.empty:
                return wide
        return pd.DataFrame(index=[str(day) for day in days], dtype="float64")

    # **每个数据集的分区只读一次**（关键：原实现每个字段各读一遍，
    # 48 个字段 × 171 个分区 ≈ 8200 次 gzip 解压，实测 ~92s，
    # 其中绝大部分是把同一个文件反复解开；改成一次读入后切片，
    # 分区读取次数降到 8 个数据集 × 171 ≈ 1370 次）。
    partition_cache: dict[str, pd.DataFrame] = {}

    def dataset_long(dataset: str) -> pd.DataFrame:
        """整段长表：库里有就查库，否则读 CSV 分区。

        给"需要整张表而不是单个字段"的消费方用（停牌集合、指数收益）。
        这类用法过去直接调 `dataset_frame`，等于**永远读 CSV** ——
        即使库完备也一样，在 5030 天的全历史区间上是纯浪费。
        """
        if dataset_backend(dataset) == "db":
            frame = db_frame(dataset)
            if len(frame):
                return frame
        return dataset_frame(dataset)

    def dataset_frame(dataset: str) -> pd.DataFrame:
        dataset_store = store(dataset)
        frames = []
        for key in days:
            frame = dataset_store.read(key)
            if frame is None or len(frame) == 0:
                continue
            # 分区文件不一定带日期列（分区键就是日期）；补进去，
            # 这样宽表路径统一用 `_wide_from_long`，不必两套逻辑。
            if "trade_date" not in frame.columns:
                frame = frame.assign(trade_date=key)
            frames.append(frame)
        if frames:
            long = pd.concat(frames, ignore_index=True)
            if "code" not in long.columns and "ts_code" in long.columns:
                long["code"] = (long["ts_code"].astype(str)
                                .str.split(".").str[0].str.zfill(6))
            # CSV 路径做不到把过滤下推到读取，但可以在这里立刻裁掉 ——
            # 后面每个字段的 pivot 都会因此少处理几百万行。
            if code_filter and "code" in long.columns:
                long = long[long["code"].astype(str).str.zfill(6)
                            .isin(code_filter)]
        else:
            long = pd.DataFrame()
        # 来源按数据集记一次（逐字段记会把 origins 撑成 40 行噪音）
        origins.append(f"{dataset}←csv:{dataset}({len(frames)} 个分区)")
        partition_cache[dataset] = long
        return long

    prices: dict[str, pd.DataFrame] = {}
    raw: dict[str, pd.DataFrame] = {}
    for name in ("open", "high", "low", "close", "volume_lot", "amount"):
        wide = field_wide("daily", name)
        if wide.empty:
            gaps.append(f"daily.{name} 无数据（可能未下载）")
        raw[name] = wide

    # **复权**：Tushare 的 daily 是未复权价，除权除息日会出现假跳空（10 送 10 → −50%），
    # 直接拿它算动量/波动率/ATR 会得到完全错误的因子值。
    # 这里用 adj_factor 转成后复权（close × adj_factor）：收益率与真实持有收益一致。
    # 同时保留 close_raw —— 市值类因子（自由流通市值 = 股本 × 价格）必须用未复权价。
    adj = field_wide("adj_factor", "adj_factor")
    if adj.empty:
        gaps.append("adj_factor 无数据 → 价格未复权（除权日会产生假跳空，"
                    "动量/波动率因子不可信）")
        prices.update(raw)
    else:
        adj_aligned = adj.reindex(index=raw["close"].index, columns=raw["close"].columns)
        for name in ("open", "high", "low", "close"):
            prices[name] = raw[name] * adj_aligned
        prices["volume_lot"] = raw["volume_lot"]
        prices["amount"] = raw["amount"]
    prices["close_raw"] = raw["close"]

    basics: dict[str, pd.DataFrame] = {}
    for name in BASIC_FIELDS:
        wide = field_wide("daily_basic", name)
        if wide.empty and name in ("pe_ttm", "pb", "total_mv"):
            gaps.append(f"daily_basic.{name} 无数据（可能未下载）")
        basics[name] = wide

    flows: dict[str, pd.DataFrame] = {}
    for name in ("net_mf_amount", "buy_lg_amount", "sell_lg_amount",
                 "buy_elg_amount", "sell_elg_amount"):
        flows[name] = field_wide("moneyflow", name)

    limits: dict[str, pd.DataFrame] = {}
    for name in ("up_limit", "down_limit"):
        limits[name] = field_wide("stk_limit", name)

    bak: dict[str, pd.DataFrame] = {}
    for name in ("swing", "selling", "buying", "strength", "activity",
                 "avg_turnover", "attack", "interval_3", "interval_6",
                 "bak_vol_ratio", "bak_turnover"):
        bak[name] = field_wide("bak_daily", name)

    # 停牌集合（(date, code)）。走 dataset_long 而不是 dataset_frame：
    # 后者只读 CSV 分区，在 5030 天的全历史区间上光解压就要好几秒，
    # 而这两个数据集同样已经在库里了（`covers` 已经判过）。
    suspended: set[tuple[str, str]] = set()
    suspend_long = dataset_long("suspend_d")
    if len(suspend_long) and "code" in suspend_long.columns:
        suspended = set(zip(suspend_long["trade_date"].astype(str),
                            suspend_long["code"].astype(str), strict=False))

    # 指数收益（相对强度基准）：整段读一次再按 ts_code 分组，
    # 原实现是 3 个指数 × 171 个分区 = 513 次读同一个文件。
    index_returns: dict[str, pd.Series] = {}
    index_long = dataset_long("index_daily")
    if len(index_long) and "ts_code" in index_long.columns:
        index_long = index_long.copy()
        index_long["date"] = index_long["trade_date"].astype(str)
        for ts_code in ("000300.SH", "000001.SH", "399006.SZ"):
            hit = index_long[index_long["ts_code"] == ts_code]
            if len(hit):
                index_returns[ts_code] = pd.Series(
                    hit["close"].astype(float).to_numpy(),
                    index=hit["date"].astype(str)).sort_index()

    if fundamentals is None:
        # 只回测一只票时也只需要那一只的财务行 —— 但财务面板本身很小（十来个报告期），
        # 且 PitPanel 的构造是全局一次性的，这里不做过滤（过滤反而会多一次拷贝）。
        fundamentals = load_fundamental_panel(root=root, universe=universe,
                                              gaps=gaps)

    # 代码全集：优先行情，其次估值
    if codes is not None:
        code_list = [str(code).zfill(6) for code in codes]
    else:
        code_list = []
        for pool in (basics, prices):
            for frame in pool.values():
                if frame is not None and not frame.empty:
                    code_list = [str(code) for code in frame.columns]
                    break
            if code_list:
                break

    return FactorPanels(
        dates=days, codes=code_list, prices=prices, basics=basics,
        flows=flows, limits=limits, bak=bak, fundamentals=fundamentals,
        index_returns=index_returns, suspended=suspended, gaps=gaps,
        origins=origins)
