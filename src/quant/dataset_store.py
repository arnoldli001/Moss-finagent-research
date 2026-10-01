"""数据集分区存储：按「交易日 / 报告期 / 静态」把 Tushare 横截面落盘 + 增量同步。

设计要点（和 M1 的两个 store 保持同一套哲学）：

1. **一个分区一个文件**：`daily_basic/20260914.csv.gz` —— 一天一张全市场横截面。
   这样断点续传的粒度就是"一天"，重跑只补缺的那几天；
2. **命中判断看 manifest 的区间，不看"文件是否存在"**；
3. **股票池写进路径**（`a_share` / `all`），换池子不会误用旧缓存；
4. 存储格式自适应：装了 pyarrow 写 parquet，否则 CSV.gz；
5. 失败的交易日**记录下来**而不是静默跳过（覆盖率要可查）。
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from src.core.errors import (
    BRIEF_DEFAULT,
    BRIEF_TIGHT,
    brief,
)

logger = logging.getLogger(__name__)

from src.infrastructure.catalog.data_stores import store_rel as _store_rel  # noqa: E402

DEFAULT_ROOT = _store_rel("tushare_partitions")
_MANIFEST = "_manifest.json"


def _has_pyarrow() -> bool:
    try:
        import pyarrow  # type: ignore # noqa: F401
    except ImportError:
        return False
    return True


@dataclass
class SyncResult:
    """一次同步的结果（缺口不静默）。"""

    dataset: str
    requested: int = 0
    fetched: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    rows: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "requested": self.requested,
            "fetched": len(self.fetched),
            "skipped": len(self.skipped),
            "failed": len(self.failed),
            "rows": self.rows,
            "failed_keys": sorted(self.failed.keys())[:8],
        }


class DatasetStore:
    """某个数据集的分区缓存（分区键 = 交易日 / 报告期 / 'static'）。"""

    def __init__(self, dataset: str, *, root: str | Path = DEFAULT_ROOT,
                 universe: str = "a_share") -> None:
        if not dataset or "/" in dataset or "\\" in dataset:
            raise ValueError(f"非法数据集名 {dataset!r}")
        self.dataset = dataset
        self.universe = universe
        self.root = Path(root) / universe / dataset

    # ---------- 路径 ----------

    def _suffix(self) -> str:
        """**写入**时优先用的后缀（装了 pyarrow 就写 parquet，否则 CSV.gz）。"""
        return "parquet" if _has_pyarrow() else "csv.gz"

    def path(self, key: str) -> Path:
        return self.root / f"{key}.{self._suffix()}"

    def existing(self, key: str) -> Path | None:
        """**读取**时两种格式都认（返回实际存在的那个文件）。

        实测踩坑：写入格式取决于"当前解释器有没有 pyarrow"，而读取若只认同一种后缀，
        换个 Python 环境就看不见已有数据 —— 用 VeighNa Studio 的 python 跑服务时
        （它装了 pyarrow）前端显示"11 个数据集 / 0 万行"、因子筛选报
        "缓存里只有 0 个交易日"，而数据其实好好躺在 `.csv.gz` 里。
        所以读写必须解耦：**写按偏好，读按实际**。
        """
        for suffix in ("parquet", "csv.gz"):
            candidate = self.root / f"{key}.{suffix}"
            if candidate.exists():
                return candidate
        return None

    def manifest_path(self) -> Path:
        return self.root / _MANIFEST

    def manifest(self) -> dict[str, Any]:
        path = self.manifest_path()
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:  # noqa: BLE001
            logger.warning("%s manifest 损坏(%s)，按空处理并重建",
                           self.dataset, brief(exc, BRIEF_TIGHT))
            return {}

    def _write_manifest(self, manifest: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest_path().write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")

    def keys(self) -> list[str]:
        """所有分区键（**两种格式都扫**）。"""
        if not self.root.exists():
            return []
        keys: set[str] = set()
        for suffix in (".parquet", ".csv.gz"):
            for path in self.root.iterdir():
                if (path.is_file() and path.name.endswith(suffix)
                        and not path.name.startswith("_")):
                    keys.add(path.name[: -len(suffix)])
        return sorted(keys)

    def has(self, key: str) -> bool:
        return self.existing(key) is not None

    # ---------- 读写 ----------

    def write(self, key: str, frame: pd.DataFrame, *,
              meta: dict[str, Any] | None = None) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.path(key)
        if path.suffix == ".parquet":
            frame.to_parquet(path, index=False)
        else:
            frame.to_csv(path, index=False, compression="gzip")
        # 换格式重写时清掉另一种格式的旧文件，避免同一分区存两份
        for other in (".parquet", ".csv.gz"):
            stale = self.root / f"{key}{other}"
            if stale != path and stale.exists():
                stale.unlink()
        manifest = self.manifest()
        manifest[key] = {"rows": int(len(frame)), "columns": list(map(str, frame.columns)),
                         **(meta or {})}
        self._write_manifest(manifest)
        return path

    def read(self, key: str) -> pd.DataFrame:
        path = self.existing(key)
        if path is None:
            return pd.DataFrame()
        if path.suffix == ".parquet":
            return pd.read_parquet(path)
        dtypes = {column: str for column in
                  ("ts_code", "code", "trade_date", "ann_date", "end_date", "period")}
        return pd.read_csv(path, compression="gzip",
                           dtype={k: v for k, v in dtypes.items()})

    def load(self, keys: Sequence[str] | None = None,
             *, key_column: str | None = None) -> pd.DataFrame:
        """拼接多个分区为一个长表（自动补分区键列）。"""
        targets = list(keys) if keys is not None else self.keys()
        frames: list[pd.DataFrame] = []
        for key in targets:
            frame = self.read(key)
            if frame is None or len(frame) == 0:
                continue
            if key_column and key_column not in frame.columns:
                frame = frame.assign(**{key_column: key})
            frames.append(frame)
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)

    def coverage(self, keys: Sequence[str] | None = None) -> dict[str, Any]:
        """覆盖率摘要（分区数 / 行数 / 日期范围）。"""
        manifest = self.manifest()
        targets = list(keys) if keys is not None else self.keys()
        rows = sum(int(manifest.get(key, {}).get("rows", 0)) for key in targets)
        return {
            "dataset": self.dataset,
            "universe": self.universe,
            "partitions": len(targets),
            "rows": rows,
            "first": targets[0] if targets else "",
            "last": targets[-1] if targets else "",
        }

    # ---------- 同步 ----------

    async def sync(
        self, keys: Iterable[str], *,
        fetch: Callable[[str], Any],
        force: bool = False, concurrency: int = 3,
    ) -> SyncResult:
        """逐分区同步：已存在且非 force 的直接跳过（增量）。

        `fetch(key)` 返回 DataFrame 或 coroutine；空表记为失败（而不是写空文件），
        这样"接口没数据"和"确实没行情"能分辨。
        """
        import asyncio

        result = SyncResult(dataset=self.dataset)
        targets = [str(key) for key in keys]
        result.requested = len(targets)
        semaphore = asyncio.Semaphore(max(1, int(concurrency)))

        async def one(key: str) -> None:
            if not force and self.has(key):
                result.skipped.append(key)
                return
            async with semaphore:
                try:
                    frame = fetch(key)
                    if hasattr(frame, "__await__"):
                        frame = await frame
                except Exception as exc:  # noqa: BLE001 单分区失败不中断整批
                    result.failed[key] = f"{type(exc).__name__}: {brief(exc, BRIEF_DEFAULT)}"
                    return
            if frame is None or len(frame) == 0:
                result.failed[key] = "返回空表"
                return
            self.write(key, frame)
            result.fetched.append(key)
            result.rows += len(frame)

        for key in targets:
            await one(key)
        return result


# ==================================================================
# 交易日 / 报告期工具
# ==================================================================


def trading_days(store: DatasetStore | None = None, *,
                 start: str, end: str, calendar: Sequence[str] | None = None) -> list[str]:
    """交易日序列（YYYYMMDD）。

    优先用交易日历；没有日历时退化为"工作日"（会多请求几个节假日，
    但 Tushare 对节假日返回空表，会被记成 failed 而不是脏数据）。
    """
    if calendar:
        return [day for day in calendar if start <= day <= end]
    days = pd.bdate_range(pd.Timestamp(start), pd.Timestamp(end))
    return [stamp.strftime("%Y%m%d") for stamp in days]


def quarter_periods(start_year: int, end_year: int, *,
                    as_of: str | None = None, grace_days: int = 15) -> list[str]:
    """已过披露期的报告期（与 fundamental_source.report_periods 同口径）。"""
    from src.quant.fundamental_source import report_periods

    return report_periods(start_year, end_year, as_of=as_of, grace_days=grace_days)
