"""价格面板：行情源前复权日线 → 宽表面板 + 本地缓存 + 增量更新（M1 数据层另一半）。

## 数据来源（2026-09 改为走项目采集链，不再直连 QMT）

原实现**直接 new `XtQuantConnector`**，理由写在 docstring 里：它已经把两根钉子
钉好了（进程级锁串行化、子进程隔离补下载）。但 2026-09 本机 QMT 终端失去行情
权限后，这个直连意味着**整个量化价格面板一律取不到数** —— 它绕过了项目里
已经配好的多源容灾链（AkShare → 腾讯 → Tushare → baostock），也绕过了
`ConnectorRouter` 的失败冷却与新鲜度下限。

现在改走同一个 `build_daily_connector_chain()`：与 `/intraday/daily`、
缠论、做T日线上下文**完全同一条链、同一套口径**。
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

#: 单只标的日线表的列。
#:
#: ⚠️ 量列名是 `volume`（**股**，来自 `DataPoint.extra`），与
#: `quant/panels.py` 的 `WAREHOUSE_PRICE_FIELDS`（`volume_lot`，**手**，Tushare 口径）
#: **不是同一套** —— 两者曾同名 `BAR_PRICE_FIELDS`，import 错一个就会在很远的地方
#: 抛 KeyError，故按数据源显式区分命名。
BAR_PRICE_FIELDS = ("open", "high", "low", "close", "volume", "amount")
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
    """DataPoint 列表 → index=YYYYMMDD、columns=BAR_PRICE_FIELDS 的日线表。"""
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
        return pd.DataFrame(columns=["date", *BAR_PRICE_FIELDS])
    frame = pd.DataFrame(rows)
    frame = frame[frame["date"].str.len() == 8]
    for column in BAR_PRICE_FIELDS:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = (frame.drop_duplicates(subset=["date"], keep="last")
             .sort_values("date").reset_index(drop=True))
    return frame[["date", *BAR_PRICE_FIELDS]]      # 固定列序，避免下游按位置取列出错


async def fetch_daily_bars(code: str, start: str, end: str,
                           indicator: str | None = None) -> pd.DataFrame:
    """默认取数器：项目多源采集链的前复权日线（**不直连 QMT**）。

    走 `build_daily_connector_chain()`（AkShare → 腾讯 → Tushare → baostock，
    QMT 与本地旧 CSV 按配置排在最后/关闭），失败自动故障转移。

    `indicator` 不传时按代码自动判定 `stock_close:` / `etf_close:`
    （ETF 段 51/56/58/15/16）—— 传错前缀会让 ETF 走个股接口，取回来的是
    "查无此码"的空结果，而在上层看起来与"这只票停牌"无法区分。
    指数**没有可靠的前缀推断**（`000001` 既可能是上证综指也可能是平安银行），
    所以必须由调用方显式传 `index_close:`。
    """
    from src.api.runtime import build_daily_connector_chain
    from src.core import symbols

    target = indicator or (
        f"etf_close:{code}" if symbols.is_etf_code(str(code).zfill(6))
        else f"stock_close:{code}")
    router = build_daily_connector_chain()
    points = await router.fetch(target, start, end)
    return _points_to_frame(points)


class PriceStore:
    """按标的缓存的前复权日线库。"""

    def __init__(self, root: str | Path = DEFAULT_ROOT, *,
                 fetcher: Callable[..., Any] | None = None) -> None:
        self.root = Path(root)
        self._fetcher = fetcher or fetch_daily_bars

    def _call_fetcher(self, code: str, start: str, end: str,
                      indicator: str) -> Any:
        """按取数器**实际支持的形参**调用（兼容只有 (code, start, end) 的自定义取数器）。

        用签名探测而不是统一改签名：`PriceStore` 的 `fetcher=` 是公开注入点，
        项目里已有若干只接受三个位置参数的取数器（例如测试与行业脚本），
        强行要求它们都加 `indicator` 会造成一片无谓的破坏。
        """
        accepts = getattr(self, "_fetcher_accepts_indicator", None)
        if accepts is None:
            import inspect

            try:
                params = inspect.signature(self._fetcher).parameters
                accepts = "indicator" in params or any(
                    p.kind is inspect.Parameter.VAR_KEYWORD
                    for p in params.values())
            except (TypeError, ValueError):  # 内建/不可introspect 的 callable
                accepts = False
            self._fetcher_accepts_indicator = accepts  # noqa: SLF001
        if accepts:
            return self._fetcher(code, start, end, indicator=indicator)
        return self._fetcher(code, start, end)

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
                   force: bool = False, concurrency: int = 4,
                   indicator_of: Callable[[str], str] | None = None) -> PriceSyncInfo:
        """拉取并缓存日线；已覆盖区间直接跳过（增量），失败只记录不抛。

        `indicator_of`：代码 → 采集指标（默认 `stock_close:{code}`）。
        指数与 ETF 必须传它（`index_close:` / `etf_close:`）—— 前缀决定了走哪个
        连接器分支，传错会得到空结果且**看起来像"该标的存在但无数据"**，
        排查方向会被带偏。
        注入的 `fetcher` 若声明了 `indicator` 形参则会收到它（便于测试与自定义取数器）。
        """
        info = PriceSyncInfo()
        targets = [str(code).zfill(6) for code in codes]
        info.requested = len(targets)
        semaphore = asyncio.Semaphore(max(1, int(concurrency)))
        to_indicator = indicator_of or (lambda code: f"stock_close:{code}")

        async def one(code: str) -> None:
            if not force and self.covers(code, start, end):
                info.skipped.append(code)
                return
            async with semaphore:
                try:
                    result = self._call_fetcher(code, start, end, to_indicator(code))
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
        if field not in BAR_PRICE_FIELDS:
            raise ValueError(f"未知字段 {field!r}（可选：{', '.join(BAR_PRICE_FIELDS)}）")
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
