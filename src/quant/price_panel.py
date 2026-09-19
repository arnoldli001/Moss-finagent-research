"""价格面板：QMT 前复权日线 → 宽表面板 + 本地缓存 + 增量更新（M1 数据层另一半）。

数据来源：项目既有的 `XtQuantConnector`（`stock_close:{code}`，`adjust="qfq"`），
它已经把两根钉子钉好了（本会话刚修过）：
  - 所有 xtquant 调用串行化在 `src/core/qmt_guard.py` 的进程级锁里；
  - 本地无数据时走**子进程隔离**的补下载，子进程原生崩溃不会带走服务进程。

缓存设计：**按标的存一个文件 + 一份 manifest 记录已覆盖的日期区间**。
增量更新的判断依据是 manifest 里的区间，而不是"文件存在与否"——
否则"上次只拉了 2025 年、这次要 2020 年起"会被误判成命中，静默少了 5 年数据。
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from src.core.errors import (
    BRIEF_TIGHT,
    brief,
)

logger = logging.getLogger(__name__)

PRICE_FIELDS = ("open", "high", "low", "close", "volume", "amount")
DEFAULT_ROOT = "data/quant/prices"
_MANIFEST = "_manifest.json"


@dataclass
class PriceSyncInfo:
    """同步结果（缺口不静默）。"""

    requested: int = 0
    fetched: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    rows: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "fetched": len(self.fetched),
            "skipped": len(self.skipped),
            "failed": len(self.failed),
            "rows": self.rows,
            "failed_codes": sorted(self.failed.keys())[:10],
        }


def _has_pyarrow() -> bool:
    """装了 pyarrow 就写 parquet，否则 CSV.gz（读取两种都认，见 PriceStore.existing）。"""
    try:
        import pyarrow  # type: ignore # noqa: F401
    except ImportError:
        return False
    return True


def _points_to_frame(points: Sequence[Any]) -> pd.DataFrame:
    """DataPoint 列表 → index=YYYYMMDD、columns=PRICE_FIELDS 的日线表。"""
    rows: list[dict[str, Any]] = []
    for point in points:
        extra = getattr(point, "extra", None) or {}
        rows.append({
            "date": str(getattr(point, "period_date", ""))[:10].replace("-", ""),
            "close": getattr(point, "value", None),
            "open": extra.get("open"),
            "high": extra.get("high"),
            "low": extra.get("low"),
            "volume": extra.get("volume"),
            "amount": extra.get("amount"),
        })
    if not rows:
        return pd.DataFrame(columns=["date", *PRICE_FIELDS])
    frame = pd.DataFrame(rows)
    frame = frame[frame["date"].str.len() == 8]
    for column in PRICE_FIELDS:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = (frame.drop_duplicates(subset=["date"], keep="last")
             .sort_values("date").reset_index(drop=True))
    return frame[["date", *PRICE_FIELDS]]      # 固定列序，避免下游按位置取列出错


async def fetch_daily_bars(code: str, start: str, end: str) -> pd.DataFrame:
    """默认取数器：QMT 前复权日线（经项目既有连接器）。"""
    from src.infrastructure.connectors.xtquant_connector import XtQuantConnector

    connector = XtQuantConnector()
    points = await connector.fetch(f"stock_close:{code}", start, end)
    return _points_to_frame(points)


class PriceStore:
    """按标的缓存的前复权日线库。"""

    def __init__(self, root: str | Path = DEFAULT_ROOT, *,
                 fetcher: Callable[[str, str, str], Any] | None = None) -> None:
        self.root = Path(root)
        self._fetcher = fetcher or fetch_daily_bars

    # ---------- 路径 / manifest ----------

    def path(self, code: str) -> Path:
        """写入路径（按当前解释器的 pyarrow 情况选格式）。"""
        suffix = "parquet" if _has_pyarrow() else "csv.gz"
        return self.root / f"{str(code).zfill(6)}.{suffix}"

    def existing(self, code: str) -> Path | None:
        """读取路径：**两种格式都认**（换解释器后不会看不见已有缓存）。"""
        stem = str(code).zfill(6)
        for suffix in ("parquet", "csv.gz"):
            candidate = self.root / f"{stem}.{suffix}"
            if candidate.exists():
                return candidate
        return None

    def _manifest_path(self) -> Path:
        return self.root / _MANIFEST

    def manifest(self) -> dict[str, dict[str, Any]]:
        path = self._manifest_path()
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:  # noqa: BLE001 坏 manifest 不该致命
            logger.warning("价格 manifest 损坏（%s），按空处理并重建", brief(exc, BRIEF_TIGHT))
            return {}

    def _write_manifest(self, manifest: dict[str, dict[str, Any]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._manifest_path().write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")

    def cached_codes(self) -> list[str]:
        if not self.root.exists():
            return []
        stems = set()
        for path in self.root.iterdir():
            if not path.is_file():
                continue
            stem = path.name.split(".")[0]
            if path.name.endswith((".csv.gz", ".parquet")) and stem.isdigit():
                stems.add(stem)
        return sorted(stems)

    # ---------- 读写 ----------

    def read(self, code: str) -> pd.DataFrame | None:
        path = self.existing(code)
        if path is None:
            return None
        if path.suffix == ".parquet":
            frame = pd.read_parquet(path)
            frame["date"] = frame["date"].astype(str)
        else:
            frame = pd.read_csv(path, compression="gzip", dtype={"date": str})
        return frame.sort_values("date").reset_index(drop=True)

    def write(self, code: str, frame: pd.DataFrame, *,
              start: str, end: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.path(code)
        if path.suffix == ".parquet":
            frame.to_parquet(path, index=False)
        else:
            frame.to_csv(path, index=False, compression="gzip")
        for other in ("parquet", "csv.gz"):
            stale = self.root / f"{str(code).zfill(6)}.{other}"
            if stale != path and stale.exists():
                stale.unlink()
        manifest = self.manifest()
        manifest[str(code).zfill(6)] = {
            "start": start, "end": end, "rows": int(len(frame)),
            "first_date": str(frame["date"].min()) if len(frame) else "",
            "last_date": str(frame["date"].max()) if len(frame) else "",
        }
        self._write_manifest(manifest)

    def covers(self, code: str, start: str, end: str) -> bool:
        """缓存区间是否已覆盖请求区间（按 manifest，而非"文件存在"）。"""
        entry = self.manifest().get(str(code).zfill(6))
        if not entry:
            return False
        return entry.get("start", "99999999") <= start and entry.get("end", "") >= end

    # ---------- 同步 ----------

    async def sync(self, codes: Iterable[str], start: str, end: str, *,
                   force: bool = False, concurrency: int = 4) -> PriceSyncInfo:
        """拉取并缓存日线；已覆盖区间直接跳过（增量），失败只记录不抛。"""
        info = PriceSyncInfo()
        targets = [str(code).zfill(6) for code in codes]
        info.requested = len(targets)
        semaphore = asyncio.Semaphore(max(1, int(concurrency)))

        async def one(code: str) -> None:
            if not force and self.covers(code, start, end):
                info.skipped.append(code)
                return
            async with semaphore:
                try:
                    result = self._fetcher(code, start, end)
                    if hasattr(result, "__await__"):
                        result = await result
                except Exception as exc:  # noqa: BLE001 单只失败不中断整批
                    info.failed[code] = f"{type(exc).__name__}: {brief(exc, BRIEF_TIGHT)}"
                    return
            frame = result if isinstance(result, pd.DataFrame) else pd.DataFrame()
            if frame is None or len(frame) == 0:
                info.failed[code] = "返回空数据"
                return
            existing = self.read(code)
            merged = frame if existing is None else (
                pd.concat([existing, frame], ignore_index=True)
                .drop_duplicates(subset=["date"], keep="last")
                .sort_values("date").reset_index(drop=True))
            merged = merged[merged["date"] >= start]
            self.write(code, merged, start=start, end=end)
            info.fetched.append(code)
            info.rows += len(merged)

        await asyncio.gather(*(one(code) for code in targets))
        return info

    # ---------- 组装面板 ----------

    def load_frames(self, codes: Iterable[str] | None = None,
                    start: str | None = None,
                    end: str | None = None) -> dict[str, pd.DataFrame]:
        targets = ([str(c).zfill(6) for c in codes] if codes is not None
                   else self.cached_codes())
        frames: dict[str, pd.DataFrame] = {}
        for code in targets:
            frame = self.read(code)
            if frame is None or len(frame) == 0:
                continue
            if start:
                frame = frame[frame["date"] >= start]
            if end:
                frame = frame[frame["date"] <= end]
            if len(frame):
                frames[code] = frame
        return frames

    def load_panel(self, codes: Iterable[str] | None = None, *,
                   field: str = "close", start: str | None = None,
                   end: str | None = None) -> pd.DataFrame:
        """宽表面板：index=YYYYMMDD，columns=code（缺失即 NaN，不填充）。"""
        if field not in PRICE_FIELDS:
            raise ValueError(f"未知字段 {field!r}（可选：{', '.join(PRICE_FIELDS)}）")
        frames = self.load_frames(codes, start, end)
        if not frames:
            return pd.DataFrame()
        panel = pd.DataFrame({
            code: frame.set_index("date")[field] for code, frame in frames.items()})
        return panel.sort_index()


def price_panel_summary(store: PriceStore,
                        panel: pd.DataFrame) -> dict[str, Any]:
    """面板摘要（覆盖率/缺失率，供报告与前端展示）。"""
    if panel is None or panel.empty:
        return {"codes": 0, "dates": 0, "missing_ratio": 1.0}
    total = panel.shape[0] * panel.shape[1]
    missing = int(panel.isna().to_numpy().sum())
    return {
        "codes": int(panel.shape[1]),
        "dates": int(panel.shape[0]),
        "first_date": str(panel.index.min()),
        "last_date": str(panel.index.max()),
        "missing": missing,
        "missing_ratio": (missing / total) if total else 0.0,
        "cached_codes": len(store.cached_codes()),
    }
