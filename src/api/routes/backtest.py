"""策略回测API：对运行时后端（连接器）实时取数，跑纯本地规则回测。"""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from src.backtest.data import align_monthly
from src.backtest.engine import result_to_dict, run_backtest
from src.backtest.signals import TrendPEConfig
from src.core.schemas import DataPoint

router = APIRouter(prefix="/api/v1/backtest", tags=["backtest"])

_DISCLAIMER = (
    "⚠️ 历史回测不代表未来收益，结果仅供框架验证，不构成投资建议。"
    "投资有风险，入市需谨慎，盈亏自负。"
)

# 全历史行情拉取约10-30秒；月末月度数据短期不变，进程内TTL缓存避免演示重复等待。
_CACHE_TTL_SECONDS = 600.0
_fetch_cache: dict[str, tuple[float, list[DataPoint]]] = {}


def clear_fetch_cache() -> None:
    """测试用：清空回测取数缓存。"""
    _fetch_cache.clear()


async def _cached_fetch(backend, indicator: str) -> tuple[list[DataPoint], bool]:
    now = time.monotonic()
    hit = _fetch_cache.get(indicator)
    if hit is not None and now - hit[0] < _CACHE_TTL_SECONDS:
        return hit[1], True
    points = await backend.fetch(indicator)
    _fetch_cache[indicator] = (now, points)
    return points, False


class BacktestRequest(BaseModel):
    indicator: str = Field(default="PPI", description="月度宏观指标，如 CPI/PPI")
    code: str = Field(default="601088", description="A股代码")
    eps_pct: float = Field(default=1.0, ge=0.0, le=100.0)
    pe_watermark: float | None = Field(default=None)


@router.post("/run")
async def run(body: BacktestRequest, request: Request) -> dict:
    """实时取数回测（行情全历史拉取可能耗时数十秒）；无LLM、无未来函数。"""
    runtime = request.app.state.runtime
    indicator = body.indicator.strip()
    code = body.code.strip()
    if indicator not in ("CPI", "PPI"):
        raise HTTPException(status_code=400, detail="回测指标当前支持 CPI/PPI")
    if not code.isdigit() or len(code) != 6:
        raise HTTPException(status_code=400, detail="A股代码应为6位数字")

    try:
        indicator_points, ind_cached = await _cached_fetch(runtime.backend, indicator)
        price_points, price_cached = await _cached_fetch(
            runtime.backend, f"stock_close:{code}"
        )
    except Exception as exc:  # noqa: BLE001 数据源失败转4xx而非500
        raise HTTPException(
            status_code=502, detail=f"数据获取失败：{exc}"
        ) from exc

    bars = align_monthly(indicator_points, price_points, indicator)
    if len(bars) < 8:
        raise HTTPException(
            status_code=422,
            detail=f"指标与行情对齐月份不足（{len(bars)}个月，至少需要8个月）",
        )

    cfg = TrendPEConfig(
        indicator, eps_pct=body.eps_pct, pe_watermark=body.pe_watermark
    )
    result = run_backtest(bars, cfg)
    return {
        "asset": f"{code} A股",
        "indicator": indicator,
        "simulated": False,
        "range": {"start": bars[0].period, "end": bars[-1].period},
        "cache": {
            "indicator_hit": ind_cached,
            "price_hit": price_cached,
            "ttl_seconds": int(_CACHE_TTL_SECONDS),
        },
        **result_to_dict(result),
        "disclaimer": _DISCLAIMER,
    }
