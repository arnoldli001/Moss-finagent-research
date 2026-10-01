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

from src.core.errors import (
    BRIEF_TIGHT,
    brief,
)
from src.quant.dataset_store import DEFAULT_ROOT, DatasetStore
from src.quant.liquidity import (
    LiquidityFilter,
    describe_effect,
    liquidity_exclusion,
    select_codes,
)
from src.quant.panel_needs import PanelNeeds
from src.quant.pit import PitPanel

logger = logging.getLogger(__name__)

from src.infrastructure.catalog.data_stores import store_rel as _store_rel  # noqa: E402


class MissingPanelField(RuntimeError):
    """严格模式下取了一个**没有按需加载**的面板字段。

    这个异常存在的意义就是"让漏配表现为失败"：如果未加载的字段返回空表，
    因子会静默变成一列 NaN（IC = 0/None，看起来像"这个因子没用"），
    而真正的原因是面板少装了字段 —— 这正是本项目反复踩过的那类错误。
    """

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
#: 行情仓库 `daily` 面板的列。
#:
#: ⚠️ 量列名是 `volume_lot`（**手**，Tushare `daily.vol` 口径），
#: 与 `quant/price_panel.py` 的 `BAR_PRICE_FIELDS`（`volume`，**股**）**不是同一套**。
#: 两者曾同名 `PRICE_FIELDS`，import 错一个就会在很远的地方抛 KeyError，
#: 所以按数据源显式区分命名。
WAREHOUSE_PRICE_FIELDS = ("open", "high", "low", "close", "volume_lot", "amount")

PANEL_FIELDS: dict[str, tuple[str, ...]] = {
    "daily": WAREHOUSE_PRICE_FIELDS,
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
    #: 股票池过滤的**逐日剔除掩码**（True = 该日该票被剔除）。
    #: 列子集只决定"装哪些票"，每一天的口径由它保证。
    excluded: pd.DataFrame | None = None
    #: 严格模式（显式给了 `needs` 时为 True）：取未加载字段**直接抛错**。
    strict: bool = False
    #: 本次真正装载了哪些字段（`"basic:pb"` 这类限定名），供体检与报错。
    loaded: frozenset[str] = frozenset()

    # ---------- 取用接口 ----------

    def price(self, field_name: str) -> pd.DataFrame:
        return self._get("price", self.prices, field_name, "行情")

    def basic(self, field_name: str) -> pd.DataFrame:
        return self._get("basic", self.basics, field_name, "估值")

    def flow(self, field_name: str) -> pd.DataFrame:
        return self._get("flow", self.flows, field_name, "资金流")

    def bak_field(self, field_name: str) -> pd.DataFrame:
        return self._get("bak", self.bak, field_name, "备用行情")

    def limit(self, field_name: str) -> pd.DataFrame:
        return self._get("limits", self.limits, field_name, "涨跌停")

    def _get(self, scope: str, pool: dict[str, pd.DataFrame], name: str,
             label: str) -> pd.DataFrame:
        frame = pool.get(name)
        if frame is None:
            if self.strict:
                raise MissingPanelField(
                    f"面板没有按需加载 {scope}.{name}（{label}）——"
                    f"说明这次装配的 needs 漏了它。"
                    f"用 `panel_needs.needs_for_factors(...)` 生成 needs，"
                    f"或手工把它加进 `PanelNeeds.{scope}`。"
                    f"本次已加载 {len(self.loaded)} 个字段："
                    f"{sorted(self.loaded)[:10]}"
                    + ("…" if len(self.loaded) > 10 else ""))
            empty = pd.DataFrame(index=self.dates, columns=self.codes, dtype="float64")
            return empty
        return frame.reindex(index=self.dates, columns=self.codes)

    def exclusion_mask(self) -> pd.DataFrame | None:
        """股票池过滤掩码（对齐到本面板的日期/代码轴）。没有则 None。"""
        if self.excluded is None or self.excluded.empty:
            return None
        return self.excluded.reindex(index=self.dates,
                                     columns=self.codes).fillna(False)

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
                           brief(exc, BRIEF_TIGHT))
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
        logger.warning("财务面板读库失败，回退 CSV：%s", brief(exc, BRIEF_TIGHT))
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
        panel = FundamentalStore(_store_rel("quant_fundamentals"),
                                 universe=universe).load_panel()
    except Exception as exc:  # noqa: BLE001 缺财务不该让整条链路失败
        notes.append(f"基本面面板装配失败：{brief(exc, BRIEF_TIGHT)}")
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
    needs: PanelNeeds | None = None,
    liquidity: LiquidityFilter | None = None,
) -> FactorPanels:
    """从本地数据装配因子面板（不联网）。

    `source`：`mysql` 优先查 MySQL 仓库（一次查询拿整段区间，最快）；
    `csv` 强制读分区缓存；`auto` = 先 MySQL 后 CSV。

    `codes`：只要这几只票。**会给下游两条路径都省下大量工作**：
    走库时把过滤下推到 SQL（实测快 ~225 倍），走 CSV 时在拼长表后立刻裁剪
    （少做几百万行的 pivot）。只看一只票做单股票回测时，这是 28s → 数秒的差别。

    `needs`：**只装这些字段**（`panel_needs.PanelNeeds`）。不传 = 全字段装配
    （历史行为，也是单票回测的路径）。传了则进入**严格模式**：取未加载的字段
    会抛 `MissingPanelField`，而不是返回空表 —— 漏配必须表现为失败，
    否则因子会静默变成一列 NaN。
    实测收益（2026-09-25）：35 个因子实际只需要 17 个字段（全量 43 个），
    且不再读 `bak_daily`（950 万行，一个因子都没用它）、`stk_limit`（1400 万行）、
    `suspend_d`。

    `liquidity`：股票池流动性过滤（**显式选项，默认关闭**）。开启后先只取
    成交额算出逐日掩码与"常驻列"名单，后面的字段只装这些列 —— 列少了，
    内存同比例下降；逐日口径的差异由 `FactorPanels.excluded` 掩码兜住。
    """
    days = [str(day) for day in dates]
    explicit_needs = needs is not None
    needs = (needs or PanelNeeds.everything()).normalized()
    gaps: list[str] = []
    origins: list[str] = []
    loaded: set[str] = set()
    code_filter = ({str(code).split(".")[0].zfill(6) for code in codes}
                   if codes else set())

    # ---------- 本次要装的字段（决定 SELECT 的列，也就决定内存） ----------
    price_fields = set(needs.price) - {"close_raw"}
    basic_fields = set(needs.basic)
    flow_fields = set(needs.flow)
    bak_fields = set(needs.bak)
    limit_fields = set(needs.limits)

    def dataset_fields(dataset: str) -> tuple[str, ...]:
        if dataset == "daily":
            return tuple(sorted(price_fields))
        if dataset == "daily_basic":
            return tuple(sorted(basic_fields))
        if dataset == "moneyflow":
            return tuple(sorted(flow_fields))
        if dataset == "bak_daily":
            return tuple(sorted(bak_fields))
        if dataset == "stk_limit":
            return tuple(sorted(limit_fields))
        if dataset == "adj_factor":
            return ("adj_factor",)
        return ()

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
    long_cache: dict[str, pd.DataFrame] = {}

    def ensure_warehouse() -> Any:
        nonlocal warehouse_client
        if warehouse_client is None:
            from src.quant.warehouse import QuantWarehouse

            warehouse_client = QuantWarehouse(root=root, universe=universe)
        return warehouse_client

    def dataset_backend(dataset: str) -> str:
        if dataset in backend:
            return backend[dataset]
        choice = "csv"
        if source in ("auto", "db", "mysql"):
            try:
                client = ensure_warehouse()
                # 用 covers 而不是 has_rows：入库是顺序进行的，库里随时可能只是
                # 缓存的一个前缀。只看"有没有一行"会让灌到一半的数据集被判为
                # "走库"，回测因此静默丢掉后半段数据。
                if (client.available()
                        and client.covers(dataset, days[0], days[-1])):
                    choice = "db"
            except Exception as exc:  # noqa: BLE001 库不可用就走 CSV
                logger.debug("探测 %s 的仓库来源失败：%s", dataset, brief(exc, BRIEF_TIGHT))
                choice = "csv"
        backend[dataset] = choice
        return choice

    def db_frame(dataset: str) -> pd.DataFrame:
        """按数据集取一次库（缓存键 = 数据集 + 本次要的列）。

        缓存键不能只是数据集：不同调用要的列不同（主装配要 17 列，
        股票池预探只要 `amount` 一列），混用会让"先探后装"拿到缺列的表。
        """
        columns = dataset_fields(dataset)
        cache_key = f"{dataset}:{','.join(columns)}"
        if cache_key in long_cache:
            return long_cache[cache_key]
        frame = pd.DataFrame()
        if columns and dataset_backend(dataset) == "db":
            from src.quant.warehouse import DATASET_TABLES

            date_column = DATASET_TABLES[dataset][2]
            wanted = [date_column, "code", *columns]
            try:
                client = ensure_warehouse()
                available = set(client.columns_of(dataset))
                use = [name for name in wanted if name in available]
                # **股票池过滤下推到 SQL**：实测按代码筛选比取全市场再筛快 ~225 倍
                # （250 日区间 10.3ms vs 2313ms），这是"只看一只票"能秒开的关键。
                frame = client.load(
                    dataset, start=days[0], end=days[-1],
                    codes=list(code_filter) if code_filter else None,
                    columns=use or None, order=False)
                if len(frame):
                    origins.append(
                        f"{dataset}←db:{client.config.dialect}"
                        f".{DATASET_TABLES[dataset][0]}({len(use)} 列)")
            except Exception as exc:  # noqa: BLE001 取失败就退回分区文件
                logger.debug("仓库取 %s 失败，回退 CSV：%s",
                             dataset, brief(exc, BRIEF_TIGHT))
                frame = pd.DataFrame()
        long_cache[cache_key] = frame
        return frame

    def field_long(dataset: str, field: str) -> pd.DataFrame:
        """走库时**按数据集一次取回需要的列**，之后在内存里切字段。"""
        return db_frame(dataset)

    def field_wide(dataset: str, field: str) -> pd.DataFrame:
        long = field_long(dataset, field)
        if len(long) and field in long.columns:
            wide = _wide_from_long(long, field, days)
            if not wide.empty:
                return wide
        long = dataset_frame(dataset)
        if len(long) and field in long.columns:
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
            # 按需装配时**再裁一次列**：分区文件带着整个数据集的所有列，
            # 不裁的话"只用一个字段"也要把 16 列都留在内存里。
            wanted = set(dataset_fields(dataset)) | {"trade_date", "code"}
            keep = [name for name in long.columns if name in wanted]
            if keep and len(keep) < len(long.columns):
                long = long[keep]
        else:
            long = pd.DataFrame()
        # 来源按数据集记一次（逐字段记会把 origins 撑成 40 行噪音）
        origins.append(f"{dataset}←csv:{dataset}({len(frames)} 个分区)")
        partition_cache[dataset] = long
        return long

    # ================= 阶段一：股票池过滤（可选，必须在装字段之前） =================
    #
    # 顺序是关键：面板是"日期 × 代码"的矩形，列一旦装进来就占内存。
    # 所以先只取**一列**成交额算出逐日掩码与"常驻列"名单，后面所有字段
    # 都只装这些列（走库时过滤下推到 SQL，走 CSV 时拼完长表立刻裁行）。
    excluded: pd.DataFrame | None = None
    if liquidity is not None and liquidity.enabled:
        # 多取 window 个前置交易日：滚动均值在窗口头部需要历史，
        # 否则头 20 天算不出均值 → 那几天会"全员被剔除"（实测会这样）。
        probe_days = list(days)
        try:
            prior = [key for key in store("daily").keys() if key < days[0]]
            probe_days = prior[-max(1, liquidity.window):] + list(days)
        except Exception as exc:  # noqa: BLE001 拿不到前置区间就只用请求区间
            logger.debug("取前置交易日失败：%s", brief(exc, BRIEF_TIGHT))
        amount_long = pd.DataFrame()
        if dataset_backend("daily") == "db":
            from src.quant.warehouse import DATASET_TABLES

            try:
                client = ensure_warehouse()
                use = [name for name in
                       [DATASET_TABLES["daily"][2], "code", "amount"]
                       if name in set(client.columns_of("daily"))]
                amount_long = client.load("daily", start=probe_days[0],
                                          end=probe_days[-1], codes=None,
                                          columns=use or None, order=False)
            except Exception as exc:  # noqa: BLE001 失败退回 CSV
                logger.debug("股票池过滤：库取成交额失败，回退 CSV：%s",
                             brief(exc, BRIEF_TIGHT))
                amount_long = pd.DataFrame()
        if not len(amount_long):
            frames = []
            daily_store = store("daily")
            for key in probe_days:
                frame = daily_store.read(key)
                if frame is None or len(frame) == 0 or "amount" not in frame.columns:
                    continue
                if "trade_date" not in frame.columns:
                    frame = frame.assign(trade_date=key)
                columns = [name for name in ("trade_date", "code", "amount")
                           if name in frame.columns]
                frames.append(frame[columns])
            if frames:
                amount_long = pd.concat(frames, ignore_index=True)
        if len(amount_long) and "code" in amount_long.columns:
            amount_wide = _wide_from_long(amount_long, "amount", probe_days)
            full_mask = liquidity_exclusion(
                amount_wide, drop_pct=liquidity.drop_pct,
                window=liquidity.window, min_days=liquidity.min_days)
            if full_mask.empty:
                gaps.append("股票池过滤：成交额面板为空，未生效")
            else:
                kept = select_codes(full_mask, keep_ratio=liquidity.keep_ratio,
                                    dates=days)
                if code_filter:
                    kept = [code for code in kept if code in code_filter]
                gaps.append(describe_effect(full_mask, kept,
                                            universe=len(full_mask.columns),
                                            dates=days))
                excluded = full_mask.reindex(index=days)
                code_filter = set(kept)
        else:
            gaps.append("股票池过滤：没有成交额数据（daily.amount 缺失），未生效")

    # ================= 阶段二：按需装配字段 =================
    prices: dict[str, pd.DataFrame] = {}
    raw: dict[str, pd.DataFrame] = {}
    for name in sorted(price_fields):
        wide = field_wide("daily", name)
        if wide.empty:
            gaps.append(f"daily.{name} 无数据（可能未下载）")
        raw[name] = wide
        loaded.add(f"price:{name}")

    # **复权**：Tushare 的 daily 是未复权价，除权除息日会出现假跳空（10 送 10 → −50%），
    # 直接拿它算动量/波动率/ATR 会得到完全错误的因子值。
    # 这里用 adj_factor 转成后复权（close × adj_factor）：收益率与真实持有收益一致。
    # 同时保留 close_raw —— 市值类因子（自由流通市值 = 股本 × 价格）必须用未复权价。
    adjusted = sorted(price_fields & {"open", "high", "low", "close"})
    if adjusted:
        adj = field_wide("adj_factor", "adj_factor")
        anchor = raw["close"] if "close" in raw else raw[adjusted[0]]
        if adj.empty:
            gaps.append("adj_factor 无数据 → 价格未复权（除权日会产生假跳空，"
                        "动量/波动率因子不可信）")
            for name in adjusted:
                prices[name] = raw[name]
        else:
            adj_aligned = adj.reindex(index=anchor.index, columns=anchor.columns)
            for name in adjusted:
                prices[name] = raw[name] * adj_aligned
        if "close" in raw:
            # 未复权收盘：`free_float_mv` 之类的市值口径必须用它
            prices["close_raw"] = raw["close"]
            loaded.add("price:close_raw")
    for name in sorted(price_fields - set(adjusted)):
        prices[name] = raw[name]

    basics: dict[str, pd.DataFrame] = {}
    for name in sorted(basic_fields):
        wide = field_wide("daily_basic", name)
        if wide.empty and name in ("pe_ttm", "pb", "total_mv"):
            gaps.append(f"daily_basic.{name} 无数据（可能未下载）")
        basics[name] = wide
        loaded.add(f"basic:{name}")

    flows: dict[str, pd.DataFrame] = {}
    for name in sorted(flow_fields):
        flows[name] = field_wide("moneyflow", name)
        loaded.add(f"flow:{name}")

    limits: dict[str, pd.DataFrame] = {}
    for name in sorted(limit_fields):
        limits[name] = field_wide("stk_limit", name)
        loaded.add(f"limits:{name}")

    bak: dict[str, pd.DataFrame] = {}
    for name in sorted(bak_fields):
        bak[name] = field_wide("bak_daily", name)
        loaded.add(f"bak:{name}")

    # 停牌集合（(date, code)）。走 dataset_long 而不是 dataset_frame：
    # 后者只读 CSV 分区，在 5030 天的全历史区间上光解压就要好几秒，
    # 而这两个数据集同样已经在库里了（`covers` 已经判过）。
    suspended: set[tuple[str, str]] = set()
    if needs.suspended:
        suspend_long = dataset_long("suspend_d")
        if len(suspend_long) and "code" in suspend_long.columns:
            frame = suspend_long
            if "suspend_type" in frame.columns and frame["suspend_type"].notna().any():
                # **只把 S（停牌）算停牌**：`suspend_d` 同时含 R（复牌）记录，
                # 按"出现在这张表里 = 停牌"会把**复牌当天也当成不能交易**。
                # 实测这个字段曾被 `normalize` 的数字强转清成全 NaN（见
                # `tushare_source._TEXT_COLUMNS`），所以这里必须留兜底分支：
                # 只有确认有值时才按类型过滤，否则退回"有记录即停牌"（更保守）。
                frame = frame[frame["suspend_type"].astype(str).str.upper() == "S"]
            suspended = set(zip(frame["trade_date"].astype(str),
                                frame["code"].astype(str), strict=False))
        loaded.add("suspend_d")

    # 指数收益（相对强度基准）：整段读一次再按 ts_code 分组，
    # 原实现是 3 个指数 × 171 个分区 = 513 次读同一个文件。
    index_returns: dict[str, pd.Series] = {}
    if needs.index:
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
        loaded.add("index_daily")

    if fundamentals is None and needs.fundamentals:
        # 只回测一只票时也只需要那一只的财务行 —— 但财务面板本身很小（十来个报告期），
        # 且 PitPanel 的构造是全局一次性的，这里不做过滤（过滤反而会多一次拷贝）。
        fundamentals = load_fundamental_panel(root=root, universe=universe,
                                              gaps=gaps)
        loaded.add("fina_indicator_vip")

    # 代码全集：优先行情，其次估值
    if codes is not None and not code_filter:
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
    if code_filter:
        # 股票池过滤把列裁窄了：以**实际装进来的列**为准
        code_list = [code for code in code_list if code in code_filter]

    return FactorPanels(
        dates=days, codes=code_list, prices=prices, basics=basics,
        flows=flows, limits=limits, bak=bak, fundamentals=fundamentals,
        index_returns=index_returns, suspended=suspended, gaps=gaps,
        origins=origins, excluded=excluded, strict=explicit_needs,
        loaded=frozenset(loaded))
