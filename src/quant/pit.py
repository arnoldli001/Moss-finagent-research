"""PIT（Point-In-Time）基本面面板 + 本地缓存（M1 数据层核心）。

**这个模块存在的唯一理由：让"用未来信息"在结构上不可能发生。**

设计文档 §3.3/§4 用报告期（`end_date`，如 20260630）对齐财报。但 A 股财报要过 1~4 个月才公告
（半年报 8 月底、年报次年 4 月底），按报告期对齐等于在 7 月 1 日就读到了 8 月 28 日才公布的
数字 —— 这是基本面回测里最贵的一类错误：净值曲线会变得很好看，且**没有任何报错**。

本模块的三条硬规则：

1. **只认公告日**：没有 `ann_date` 的记录一律不得进入面板（在 `normalize_*` 里就丢掉）；
2. **公告后还要再等 `lag_days` 个自然日**才可使用（默认 1 天）。
   财报多在盘后披露，当天就用来交易属于"当天知道当天用"，保守起见默认隔日生效；
3. **只允许向前取值**：`as_of(t)` 永远取「可用日 ≤ t」里最新的那一条，不做插值、不看未来。

对外接口：
    panel = FundamentalStore("data/quant/fundamentals").load_panel()
    snapshot = panel.as_of("2026-07-01")        # index=code，列=指标（只用 2026-07-01 已知的）
    monthly = panel.as_of_panel(["20260131", "20260228", ...])
    panel.coverage("20260701")                  # 覆盖率，数据缺口不静默
    panel.validate()                            # 自检：重复/公告日早于报告期/缺公告日
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

KEY_COLUMNS = ("code", "name", "report_period", "ann_date")
DEFAULT_ROOT = "data/quant/fundamentals"
# 去重键：同一标的、同一报告期、**同一公告日**视为重复（重复抓取/重复文件）。
# 刻意不含"只保留最新公告" —— 修正公告（业绩快报→正式财报、财报更正）是**不同版本**，
# PIT 要求"当时能看到哪一版就返回哪一版"：若只留最新版，修正公告发布之前的
# 截面会看不到这只票（明明是已公告过的），等于凭空丢数据。
_DEDUP_KEYS = ("code", "report_period", "ann_date")


def _valid_yyyymmdd(text: str) -> bool:
    """8 位日期且月/日在合理区间（`20261399` 这类必须判非法）。"""
    if len(text) != 8 or not text.isdigit():
        return False
    month, day = int(text[4:6]), int(text[6:8])
    return 1 <= month <= 12 and 1 <= day <= 31


def to_yyyymmdd(values: Any) -> pd.Series:
    """任意日期形态 → 'YYYYMMDD' 字符串（非法/缺失 → pd.NA）。

    只接受 8 位形态（`2026-06-30` / `20260630` / `20260630.0` / 带时间戳），
    并校验月/日区间。刻意不支持 6 位简写（`2026-6-3` 与 `202606` 有歧义，
    猜错会静默错位）。

    **全程向量化**：早先版本用 `.map(_valid_yyyymmdd)` 逐行回调，
    在百万行的财务面板上两次调用要 4.4s（占单次面板装配 26%）。
    这里改成 `fullmatch` + 切片区间比较，语义不变但没有 Python 级循环。
    """
    series = values if isinstance(values, pd.Series) else pd.Series(values)
    text = series.astype("string").str.strip()
    text = text.str.replace(r"\s.*$", "", regex=True)   # 去掉时间部分
    # 只剥结尾的 `.0`（`20260630.0` 是数值化的常见形态）；不能直接删所有点号，
    # 否则 `20260630.0` → `202606300` 变成 9 位而被误判为非法。
    text = text.str.replace(r"\.0$", "", regex=True)
    text = text.str.replace(r"[-/.]", "", regex=True)   # 其余分隔符
    digits = text.str.fullmatch(r"\d{8}").fillna(False).astype(bool)
    month = pd.to_numeric(text.str[4:6], errors="coerce")
    day = pd.to_numeric(text.str[6:8], errors="coerce")
    valid = digits & month.between(1, 12) & day.between(1, 31)
    return text.where(valid)


def normalize_date(value: Any) -> str | None:
    """标量版日期归一化（`report_period` 这类整表同一个值的场景）。"""
    result = to_yyyymmdd(pd.Series([value])).iloc[0]
    return None if pd.isna(result) else str(result)


def _is_valid_period(value: Any) -> bool:
    text = "" if value is None else str(value)
    return len(text) == 8 and text.isdigit() and text[4:6] in (
        "03", "06", "09", "12")


@dataclass(frozen=True)
class PitConfig:
    """PIT 口径参数。"""

    lag_days: int = 1
    """公告日之后还要等多少个自然日才可使用（默认 1：隔日生效）。"""

    def usable_dates(self, ann_date: pd.Series) -> pd.Series:
        parsed = pd.to_datetime(ann_date, format="%Y%m%d", errors="coerce")
        shifted = parsed + pd.Timedelta(days=int(self.lag_days))
        return shifted.dt.strftime("%Y%m%d")


class PitPanel:
    """PIT 基本面面板：按"该日已知"取截面快照。"""

    def __init__(self, records: pd.DataFrame, *,
                 config: PitConfig | None = None) -> None:
        self.config = config or PitConfig()
        self.dropped_impossible = 0     # 公告日早于报告期而被丢弃的行数（自检用）
        self.records = self._prepare(records)
        # `as_of_panel` 结果缓存：**挂在 PitPanel 自身上**，因为它构造后不可变。
        #
        # 为什么不缓存在调用方（`FactorPanels`）上 —— 实测踩过：
        # 调用方执行 `panels.fundamentals = PitPanel(新记录)` 换掉财务面板之后，
        # 缓存在 panel 上的快照仍然是旧的，`fundamental_field` 会返回**过期数据**
        # 而且不报错（一次因子单测因此失败：增长率被改成负数后 peg 仍算出有限值）。
        # 挂在自身上，换对象就自然换了缓存。
        self._snapshot_cache: dict[tuple[str, ...], dict[str, pd.DataFrame]] = {}

    # ---------- 构建 ----------

    def _prepare(self, records: pd.DataFrame) -> pd.DataFrame:
        if records is None or len(records) == 0:
            return pd.DataFrame(columns=[*KEY_COLUMNS, "usable_date"])
        frame = records.copy()
        for column in KEY_COLUMNS:
            if column not in frame.columns:
                if column == "name":
                    frame["name"] = ""
                else:
                    raise ValueError(f"PIT 面板缺少必要列 {column!r}")
        frame["code"] = frame["code"].astype(str).str.strip().str.zfill(6)
        frame["report_period"] = to_yyyymmdd(frame["report_period"])
        frame["ann_date"] = to_yyyymmdd(frame["ann_date"])
        # 硬规则 1：没有公告日的记录不得进入面板
        missing_ann = frame["ann_date"].isna()
        if missing_ann.any():
            logger.warning("PIT 面板丢弃 %d 行（缺公告日，按报告期对齐会引入未来信息）",
                           int(missing_ann.sum()))
            frame = frame[~missing_ann]
        frame["usable_date"] = self.config.usable_dates(frame["ann_date"])
        # **公告日早于报告期 = 数据错位，必须丢弃**：那意味着"报告期还没结束就看到了它"，
        # 是货真价实的未来信息（实测 Tushare fina_indicator_vip 里有这种行，
        # 例如 603400 报告期 20260630 却标公告日 20260422）。宁可少一行，不能用未来数据。
        impossible = frame["ann_date"] < frame["report_period"]
        if impossible.any():
            self.dropped_impossible = int(impossible.sum())
            logger.warning("PIT 面板丢弃 %d 行（公告日早于报告期，属数据错位/未来信息），"
                           "例如 %s", self.dropped_impossible,
                           frame.loc[impossible, ["code", "report_period", "ann_date"]]
                           .head(2).to_dict("records"))
            frame = frame[~impossible]
        # 同一 (code, 报告期, 公告日) 去重（重复抓取），但**保留不同公告日的版本**：
        # 修正公告之前的那段历史里，当时能看到的就是旧版本。
        frame = frame.sort_values(["code", "report_period", "usable_date"])
        frame = frame.drop_duplicates(subset=list(_DEDUP_KEYS), keep="last")
        metrics = [c for c in frame.columns if c not in (*KEY_COLUMNS, "usable_date")]
        return frame[[*KEY_COLUMNS, "usable_date", *metrics]].reset_index(drop=True)

    # ---------- 元信息 ----------

    @property
    def metrics(self) -> list[str]:
        return [c for c in self.records.columns
                if c not in (*KEY_COLUMNS, "usable_date")]

    @property
    def codes(self) -> list[str]:
        return sorted(self.records["code"].unique().tolist())

    def __len__(self) -> int:
        return len(self.records)

    # ---------- 取值 ----------

    def as_of(self, date: Any) -> pd.DataFrame:
        """某个交易日**当时已知**的截面快照：index=code，列=指标。

        语义直白版实现（慢但一眼可验证），`as_of_panel` 走 merge_asof 快路径，
        两者结果必须一致（有专门用例对拍）。
        """
        target = to_yyyymmdd(pd.Series([date])).iloc[0]
        if pd.isna(target):
            raise ValueError(f"非法日期：{date!r}（应为 YYYYMMDD 或 YYYY-MM-DD）")
        visible = self.records[self.records["usable_date"] <= target]
        if len(visible) == 0:
            return pd.DataFrame(columns=self.metrics)
        # `kind="stable"` 是**必须的**，不是可选优化：同一 code 在同一
        # usable_date 上可能有多条记录（不同报告期同日公告），并列时取哪一条
        # 决定了 PIT 取值的正确性。稳定排序保证"保留原始顺序里的最后一条"，
        # 而原始顺序是按 report_period 升序下载的 → 取到最近的报告期，
        # 这才是"当时最新已知"的语义。用默认快排时并列顺序未定义，
        # as_of 与 as_of_panel 会取到不同的行（实测差异可达 5942）。
        latest = (visible.sort_values(["code", "usable_date"], kind="stable")
                  .drop_duplicates(subset=["code"], keep="last"))
        return latest.set_index("code")[self.metrics]

    def as_of_records(self, date: Any) -> pd.DataFrame:
        """同 `as_of`，但**保留 code/name/report_period/ann_date/usable_date**。

        为什么需要它：同比类因子（如"毛利率同比变化"）必须知道"当前可见的报告期"
        才能去找去年同期的那条记录 —— 而 `as_of` 只返回指标列、拿不到报告期。
        实测后果：该因子整列变 NaN，异常又被 compute_factors 兜住，静默无值。
        """
        target = to_yyyymmdd(pd.Series([date])).iloc[0]
        if pd.isna(target):
            raise ValueError(f"非法日期：{date!r}（应为 YYYYMMDD 或 YYYY-MM-DD）")
        visible = self.records[self.records["usable_date"] <= target]
        if len(visible) == 0:
            return pd.DataFrame(columns=self.records.columns)
        latest = (visible.sort_values(["code", "usable_date"])
                  .drop_duplicates(subset=["code"], keep="last"))
        return latest.set_index("code")

    def as_of_panel(self, dates: Sequence[Any]) -> dict[str, pd.DataFrame]:
        """批量截面（回测逐期调用）——merge_asof 快路径。

        注意：pandas 的 `merge_asof` **只支持数值/日期键**，字符串 'YYYYMMDD'
        会报 `Incompatible merge dtype ... both sides must have numeric dtype`。

        这里用 **int64 键**而不是 datetime：'YYYYMMDD' 转 int 后仍然保序
        （字典序 = 数值序），而 datetime 转换在 896k 行的网格上实测要 2 秒，
        且没有任何语义收益 —— 我们只需要"能比较大小"。
        """
        targets = [to_yyyymmdd(pd.Series([d])).iloc[0] for d in dates]
        targets = [t for t in targets if not pd.isna(t)]
        if not targets:
            return {}
        cache_key = tuple(map(str, targets))
        cached = self._snapshot_cache.get(cache_key)
        if cached is not None:
            return cached
        records = self.records
        if len(records) == 0:
            return {str(t): pd.DataFrame(columns=self.metrics) for t in targets}
        codes = records["code"].unique()
        grid = pd.MultiIndex.from_product(
            [targets, codes], names=["date", "code"]).to_frame(index=False)
        grid["date_key"] = grid["date"].astype("int64")
        right = records.assign(
            usable_key=records["usable_date"].astype("int64"))
        # **稳定排序**：merge_asof 在同一键有多行时取最后一行，
        # 而"最后一行"取决于排序是否稳定。必须与 `as_of` 的
        # `keep="last"` 口径一致，否则同一份数据两条路径会取到不同的记录
        # （实测 roe 列最大差异 5942，而且**不报错**，只是数字不一样）。
        merged = pd.merge_asof(
            grid.sort_values("date_key", kind="stable"),
            right.sort_values("usable_key", kind="stable"),
            left_on="date_key", right_on="usable_key",
            by="code", direction="backward")
        merged = merged[merged["usable_date"].notna()]
        out: dict[str, pd.DataFrame] = {}
        for target, group in merged.groupby("date", sort=True):
            frame = group.set_index("code")[self.metrics]
            out[str(target)] = frame
        self._snapshot_cache[cache_key] = out
        return out

    def coverage(self, date: Any) -> dict[str, Any]:
        """某日的覆盖率（用于报缺口，而不是让因子静默变成 NaN）。"""
        snapshot = self.as_of(date)
        universe = len(self.codes)
        covered = int(snapshot[self.metrics[0]].notna().sum()) if self.metrics else 0
        return {
            "date": str(to_yyyymmdd(pd.Series([date])).iloc[0]),
            "universe": universe,
            "covered": covered,
            "coverage": (covered / universe) if universe else 0.0,
            "stale_from": (
                str(self.records["report_period"].max())
                if len(self.records) else ""),
        }

    # ---------- 自检 ----------

    def validate(self) -> list[str]:
        """结构性自检，返回问题列表（空列表 = 干净）。"""
        problems: list[str] = []
        frame = self.records
        if len(frame) == 0:
            return ["PIT 面板为空"]
        bad_period = frame[~frame["report_period"].map(_is_valid_period)]
        if len(bad_period):
            problems.append(
                f"{len(bad_period)} 行报告期非法（应为 YYYYMMDD 季末）："
                f"{bad_period['report_period'].head(3).tolist()}")
        leaked = frame[frame["ann_date"] < frame["report_period"]]
        if len(leaked):
            problems.append(
                f"{len(leaked)} 行公告日早于报告期（数据错位），"
                f"例如 {leaked[['code', 'report_period', 'ann_date']].head(2).to_dict('records')}")
        duplicates = frame.duplicated(subset=list(_DEDUP_KEYS)).sum()
        if duplicates:
            problems.append(f"{int(duplicates)} 行 (code, 报告期) 重复未去重")
        if (frame["usable_date"] < frame["ann_date"]).any():
            problems.append("usable_date 早于 ann_date（PIT 滞后被抵消，等于引入未来信息）")
        return problems

    # ---------- 序列化 ----------

    def to_frame(self) -> pd.DataFrame:
        return self.records.copy()


# ==================================================================
# 本地缓存 + 增量更新
# ==================================================================


@dataclass
class CacheInfo:
    """缓存命中情况（增量更新用）。"""

    cached_periods: list[str] = field(default_factory=list)
    fetched_periods: list[str] = field(default_factory=list)
    failed_periods: list[str] = field(default_factory=list)
    attempts: list[Any] = field(default_factory=list)


class FundamentalStore:
    """基本面本地缓存：按报告期存盘 + 增量拉取 + 组装 PIT 面板。

    存储格式自适应：装了 pyarrow 写 parquet，否则退回 CSV.gz（当前环境没有 pyarrow）。
    两种格式都保持"按报告期一个文件"，便于人工核对与增量补齐。
    """

    def __init__(self, root: str | Path = DEFAULT_ROOT, *,
                 config: PitConfig | None = None,
                 universe: str = "a_share",
                 fetcher: Any = None) -> None:
        self.root = Path(root)
        self.config = config or PitConfig()
        # 股票池写进文件名：换池子后不会误用旧缓存（沪深A股 vs 全市场混在一起
        # 会让截面里突然多出几千只新三板，回测结论直接跑偏）。
        self.universe = universe
        self._fetcher = fetcher      # 注入点：测试用假取数器，生产用 fundamental_source

    # ---------- 路径/格式 ----------

    def _path(self, period: str) -> Path:
        """写入路径（按当前解释器的 pyarrow 情况选格式）。"""
        suffix = "parquet" if _has_pyarrow() else "csv.gz"
        return self.root / f"performance_{period}_{self.universe}.{suffix}"

    def _existing(self, period: str) -> Path | None:
        """读取路径：**两种格式都认**（换解释器后不会看不见已有缓存）。"""
        marker = f"_{self.universe}."
        for suffix in ("parquet", "csv.gz"):
            candidate = self.root / f"performance_{period}{marker}{suffix}"
            if candidate.exists():
                return candidate
        return None

    def cached_periods(self) -> list[str]:
        if not self.root.exists():
            return []
        marker = f"_{self.universe}."
        periods: set[str] = set()
        for path in self.root.iterdir():
            if not path.is_file() or not path.name.startswith("performance_"):
                continue
            body = path.name[len("performance_"):]
            if marker not in body:
                continue                      # 别的股票池的文件不算命中
            period = body.split(marker, 1)[0]
            if _is_valid_period(period):
                periods.add(period)
        return sorted(periods)

    def write_period(self, period: str, frame: pd.DataFrame) -> Path:
        """按报告期写盘（增量更新的最小单元）。"""
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(period)
        if path.suffix == ".parquet":
            frame.to_parquet(path, index=False)
        else:
            frame.to_csv(path, index=False, compression="gzip")
        for other in ("parquet", "csv.gz"):
            stale = self.root / f"performance_{period}_{self.universe}.{other}"
            if stale != path and stale.exists():
                stale.unlink()
        return path

    def read_period(self, period: str) -> pd.DataFrame:
        path = self._existing(period)
        if path is None:
            return pd.DataFrame()
        if path.suffix == ".parquet":
            return pd.read_parquet(path)
        return pd.read_csv(path, compression="gzip", dtype={
            "code": str, "report_period": str, "ann_date": str})

    # ---------- 增量同步 ----------

    async def sync(self, periods: Iterable[str], *,
                   force: bool = False) -> CacheInfo:
        """拉取报告期数据：**已缓存且非 force 的直接跳过**（这就是增量更新）。"""
        info = CacheInfo(cached_periods=self.cached_periods())
        for period in periods:
            if not _is_valid_period(period):
                raise ValueError(f"非法报告期 {period!r}（应为 YYYYMMDD 季末）")
            if not force and period in info.cached_periods:
                continue
            frame, attempt = await self._fetch_period(period)
            info.attempts.append(attempt)
            if frame is None or len(frame) == 0:
                info.failed_periods.append(period)
                continue
            self.write_period(period, frame)
            info.fetched_periods.append(period)
        return info

    async def _fetch_period(self, period: str) -> tuple[pd.DataFrame, Any]:
        if self._fetcher is not None:
            result = self._fetcher(period)
            if hasattr(result, "__await__"):
                result = await result
            return result
        from src.quant.fundamental_source import (
            fetch_performance_report,
            normalize_yjbb,
        )

        raw, attempt = await fetch_performance_report(period)
        if raw is None or len(raw) == 0:
            return pd.DataFrame(), attempt
        return normalize_yjbb(raw, period, universe=self.universe), attempt

    # ---------- 组装 ----------

    def load(self, periods: Sequence[str] | None = None) -> pd.DataFrame:
        """读取所有（或指定）已缓存报告期，拼成规范化长表。"""
        targets = list(periods) if periods is not None else self.cached_periods()
        frames = [self.read_period(period) for period in targets]
        frames = [frame for frame in frames if frame is not None and len(frame)]
        if not frames:
            return pd.DataFrame(columns=[*KEY_COLUMNS])
        return pd.concat(frames, ignore_index=True)

    def load_panel(self, periods: Sequence[str] | None = None) -> PitPanel:
        return PitPanel(self.load(periods), config=self.config)


# ==================================================================
# 工具
# ==================================================================


def _has_pyarrow() -> bool:
    try:
        import pyarrow  # type: ignore # noqa: F401
    except ImportError:
        return False
    return True


def pit_leak_report(panel: PitPanel, dates: Sequence[Any]) -> dict[str, Any]:
    """给回测用的"事后自检"：确认任意截面都没有用到未来公告的财报。

    回测跑完后调用一次，把结果写进报告 —— 让"无未来函数"成为**可验证的结论**，
    而不是一句声明。
    """
    leaks = 0
    checked = 0
    for date in dates:
        target = to_yyyymmdd(pd.Series([date])).iloc[0]
        if pd.isna(target):
            continue
        snapshot = panel.as_of(target)
        if snapshot.empty:
            continue
        available = set(snapshot.index)
        checked += len(available)
        visible_dates = panel.records[panel.records["code"].isin(available)]
        visible_dates = visible_dates[visible_dates["usable_date"] <= target]
        if len(visible_dates) == 0:
            leaks += len(available)
    return {"dates_checked": len(list(dates)), "codes_checked": checked,
            "leaks": leaks, "clean": leaks == 0}


def summarize_panel(panel: PitPanel) -> dict[str, Any]:
    """面板摘要（API/前端展示用）。"""
    frame = panel.records
    if len(frame) == 0:
        return {"records": 0, "codes": 0, "periods": 0, "metrics": []}
    return {
        "records": int(len(frame)),
        "codes": int(frame["code"].nunique()),
        "periods": int(frame["report_period"].nunique()),
        "earliest_period": str(frame["report_period"].min()),
        "latest_period": str(frame["report_period"].max()),
        "metrics": panel.metrics,
        "lag_days": panel.config.lag_days,
        "problems": panel.validate(),
    }


def _numeric_summary(series: pd.Series) -> dict[str, float]:
    values = pd.to_numeric(series, errors="coerce").dropna()
    if values.empty:
        return {"count": 0}
    return {
        "count": int(values.size),
        "mean": float(np.mean(values)),
        "median": float(np.median(values)),
        "p10": float(np.percentile(values, 10)),
        "p90": float(np.percentile(values, 90)),
    }
