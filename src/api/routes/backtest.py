"""策略回测API：对运行时后端（连接器）实时取数，跑纯本地规则回测。

异步任务模式：POST /run 立即返回 job_id（全历史行情拉取可达数十秒，
同步长请求会被浏览器/代理中断导致前端 "Failed to fetch"），
前端轮询 GET /jobs/{job_id} 展示「数据下载中」进度直至完成。
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from src.api.job_table import BACKTEST_RETENTION, purge_jobs
from src.backtest.data import align_monthly, month_key
from src.backtest.engine import CostConfig, result_to_dict, run_backtest
from src.backtest.signals import TrendPEConfig
from src.core.errors import brief
from src.core.schemas import DataPoint

router = APIRouter(prefix="/api/v1/backtest", tags=["backtest"])

_DISCLAIMER = (
    "⚠️ 历史回测不代表未来收益，结果仅供框架验证，不构成投资建议。"
    "投资有风险，入市需谨慎，盈亏自负。"
)

# 全历史行情拉取约10-30秒；月末月度数据短期不变，进程内TTL缓存避免演示重复等待。
_CACHE_TTL_SECONDS = 600.0
_fetch_cache: dict[str, tuple[float, list[DataPoint]]] = {}

# 回测支持的月度宏观指标（值单位：%，规则用相邻两月环比变化判定趋势）
_SUPPORTED_INDICATORS = ("CPI", "PPI", "M2", "社融")
_ASSET_QUOTE_PREFIX = {"stock": "stock_close", "index": "index_close", "etf": "etf_close"}
_ASSET_LABEL = {"stock": "A股", "index": "指数", "etf": "ETF"}
_DATE_RE = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")

# ========== 异步任务（避免长请求被浏览器/代理掐断） ==========

_STAGE_LABELS = {
    "queued": "排队中",
    "fetch_indicator": "下载宏观指标数据",
    "fetch_price": "下载全历史行情",
    "fetch_pe": "下载PE估值序列",
    "aligning": "对齐指标与行情",
    "running": "回测计算中",
    "done": "完成",
}
# 未命中缓存时各阶段预估耗时（秒），用于前端「预计还需X秒」提示
_STAGE_ESTIMATES = {
    "fetch_indicator": 8,
    "fetch_price": 20,
    "fetch_pe": 40,
}
# 前端轮询间隔（秒）：回测各阶段最短约 8 秒，1.5 秒轮询既不显卡顿也不打爆接口
_POLL_INTERVAL_SECONDS = 1.5
_jobs: dict[str, dict] = {}


def clear_fetch_cache() -> None:
    """测试用：清空回测取数缓存与任务表。"""
    _fetch_cache.clear()
    _jobs.clear()


def _purge_jobs() -> None:
    """淘汰过期与超额的已完成/失败任务（运行中任务保留）。"""
    purge_jobs(_jobs, BACKTEST_RETENTION)


async def _cached_fetch(backend, indicator: str) -> tuple[list[DataPoint], bool]:
    now = time.monotonic()
    hit = _fetch_cache.get(indicator)
    if hit is not None and now - hit[0] < _CACHE_TTL_SECONDS:
        return hit[1], True
    points = await backend.fetch(indicator)
    _fetch_cache[indicator] = (now, points)
    return points, False


class BacktestRequest(BaseModel):
    indicator: str = Field(default="PPI", description="月度宏观指标：CPI/PPI/M2/社融")
    code: str = Field(default="601088", description="6位证券代码")
    asset_type: str = Field(default="stock", description="标的类型：stock/index/etf")
    eps_pct: float = Field(default=1.0, ge=0.0, le=100.0)
    pe_watermark: float | None = Field(default=None, description="PE(TTM)水位线，仅个股")
    start_date: str | None = Field(default=None, description="起始月 YYYY-MM")
    end_date: str | None = Field(default=None, description="结束月 YYYY-MM")
    initial_capital: float = Field(default=1_000_000.0, gt=0.0)
    commission_rate: float = Field(default=0.00025, ge=0.0, description="单边佣金，默认万2.5")
    stamp_tax_rate: float | None = Field(default=None, description="卖出印花税；默认个股0.05%")
    slippage_rate: float = Field(default=0.0005, ge=0.0, description="单边滑点，默认0.05%")
    cash_annual_yield: float = Field(default=0.015, ge=0.0, description="空仓货基年化")


def _estimate_wait(pe_enabled: bool, cache_hint: dict[str, bool]) -> int:
    """按未命中缓存的取数阶段累加预估等待秒数。"""
    est = 0
    if not cache_hint.get("indicator"):
        est += _STAGE_ESTIMATES["fetch_indicator"]
    if not cache_hint.get("price"):
        est += _STAGE_ESTIMATES["fetch_price"]
    if pe_enabled and not cache_hint.get("pe"):
        est += _STAGE_ESTIMATES["fetch_pe"]
    return max(est, 10)


async def _run_job(job: dict, runtime, body: BacktestRequest) -> None:
    """后台执行取数+回测；结果/异常写入job，前端经GET轮询获取。"""
    try:
        indicator = body.indicator.strip()
        code = body.code.strip()
        asset_type = body.asset_type.strip().lower()
        quote_indicator = f"{_ASSET_QUOTE_PREFIX[asset_type]}:{code}"
        pe_enabled = body.pe_watermark is not None

        hint = {
            "indicator": _fetch_cache.get(indicator) is not None,
            "price": _fetch_cache.get(quote_indicator) is not None,
            "pe": pe_enabled and _fetch_cache.get(f"PE(TTM):{code}") is not None,
        }
        job["estimated_wait_seconds"] = _estimate_wait(pe_enabled, hint)

        job["stage"] = "fetch_indicator"
        try:
            indicator_points, ind_cached = await _cached_fetch(
                runtime.backend, indicator)
        except Exception as exc:  # noqa: BLE001 数据源失败转任务错误
            raise HTTPException(
                status_code=502, detail=f"数据获取失败：{brief(exc)}") from exc

        job["stage"] = "fetch_price"
        try:
            price_points, price_cached = await _cached_fetch(
                runtime.backend, quote_indicator)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=502, detail=f"数据获取失败：{brief(exc)}") from exc

        pe_points = None
        pe_cached = False
        if pe_enabled:
            job["stage"] = "fetch_pe"
            try:
                pe_points, pe_cached = await _cached_fetch(
                    runtime.backend, f"PE(TTM):{code}")
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=502, detail=f"数据获取失败：{brief(exc)}") from exc

        job["stage"] = "aligning"
        bars = align_monthly(indicator_points, price_points, indicator, pe_points)
        start = month_key(body.start_date) if body.start_date else None
        end = month_key(body.end_date) if body.end_date else None
        if start or end:
            bars = [
                b for b in bars
                if (not start or b.period >= start) and (not end or b.period <= end)
            ]
        if len(bars) < 8:
            raise HTTPException(
                status_code=422,
                detail=f"指标与行情对齐月份不足（{len(bars)}个月，至少需要8个月）",
            )

        job["stage"] = "running"
        cfg = TrendPEConfig(indicator, eps_pct=body.eps_pct,
                            pe_watermark=body.pe_watermark)
        stamp_tax = (
            body.stamp_tax_rate if body.stamp_tax_rate is not None
            else (0.0005 if asset_type == "stock" else 0.0)
        )
        cost = CostConfig(
            commission_rate=body.commission_rate,
            stamp_tax_rate=stamp_tax,
            slippage_rate=body.slippage_rate,
            cash_annual_yield=body.cash_annual_yield,
            initial_capital=body.initial_capital,
        )
        result = run_backtest(bars, cfg, cost=cost)
        pe_total = len(bars)
        pe_covered = sum(1 for b in bars if b.pe is not None)
        job["result"] = {
            "asset": f"{code} {_ASSET_LABEL[asset_type]}",
            "indicator": indicator,
            "simulated": False,
            "range": {"start": bars[0].period, "end": bars[-1].period},
            "pe_gate": {
                "enabled": pe_enabled,
                "watermark": body.pe_watermark,
                "coverage": round(pe_covered / pe_total, 4) if pe_total else 0.0,
                "months_covered": pe_covered,
            },
            "cache": {
                "indicator_hit": ind_cached,
                "price_hit": price_cached,
                "pe_hit": pe_cached,
                "ttl_seconds": int(_CACHE_TTL_SECONDS),
            },
            **result_to_dict(result),
            "disclaimer": _DISCLAIMER,
        }
        job["status"] = "done"
    except HTTPException as exc:
        job["status"] = "error"
        # detail 可能是结构化 dict（{"code","message"}），取其文案而非 str(dict)
        d = exc.detail
        job["error"] = (d.get("message", "") if isinstance(d, dict)
                        else str(d))
        job["error_code"] = exc.status_code
    except Exception as exc:  # noqa: BLE001 兜底：任务内异常不逃逸
        job["status"] = "error"
        job["error"] = f"回测执行异常：{brief(exc)}"
        job["error_code"] = 500
    finally:
        job["stage"] = "done" if job["status"] == "done" else job["stage"]
        job["finished_at"] = time.monotonic()


@router.post("/run")
async def run(body: BacktestRequest, request: Request) -> dict:
    """校验入参后立即返回job_id；取数+回测在后台任务执行，前端轮询进度。

    全历史行情拉取可达数十秒，同步长请求会被浏览器/代理中断
    （前端表现为 TypeError: Failed to fetch），故改为异步任务模式。
    """
    runtime = request.app.state.runtime
    indicator = body.indicator.strip()
    code = body.code.strip()
    asset_type = body.asset_type.strip().lower()
    if indicator not in _SUPPORTED_INDICATORS:
        raise HTTPException(
            status_code=400,
            detail=f"回测指标当前支持 {'/'.join(_SUPPORTED_INDICATORS)}",
        )
    if asset_type not in _ASSET_QUOTE_PREFIX:
        raise HTTPException(status_code=400, detail="标的类型应为 stock/index/etf")
    if not code.isdigit() or len(code) != 6:
        raise HTTPException(status_code=400, detail="证券代码应为6位数字")
    if body.pe_watermark is not None and asset_type != "stock":
        raise HTTPException(status_code=400, detail="PE闸门仅支持个股(stock)")
    if body.start_date and not _DATE_RE.match(body.start_date):
        raise HTTPException(status_code=400, detail="start_date格式应为 YYYY-MM")
    if body.end_date and not _DATE_RE.match(body.end_date):
        raise HTTPException(status_code=400, detail="end_date格式应为 YYYY-MM")
    start = month_key(body.start_date) if body.start_date else None
    end = month_key(body.end_date) if body.end_date else None
    if start and end and start > end:
        raise HTTPException(status_code=400, detail="start_date不能晚于end_date")

    _purge_jobs()
    job_id = uuid.uuid4().hex[:12]
    job: dict = {
        "job_id": job_id, "status": "running", "stage": "queued",
        "started_at": time.monotonic(), "finished_at": None,
        "estimated_wait_seconds": _estimate_wait(
            body.pe_watermark is not None,
            {"indicator": False, "price": False, "pe": False},
        ),
        "result": None, "error": None, "error_code": None,
    }
    _jobs[job_id] = job
    asyncio.create_task(_run_job(job, runtime, body))
    return {
        "job_id": job_id,
        "status": "running",
        "stage": job["stage"],
        "stage_label": _STAGE_LABELS[job["stage"]],
        "estimated_wait_seconds": job["estimated_wait_seconds"],
        "poll_interval_seconds": _POLL_INTERVAL_SECONDS,
        "message": "数据下载中：全历史行情拉取较慢，请等待轮询完成",
    }


@router.get("/jobs/{job_id}")
async def job_status(job_id: str) -> dict:
    """轮询回测任务：running返回进度，done返回完整回测结果，error返回原因。"""
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="回测任务不存在或已过期")
    elapsed = round(time.monotonic() - job["started_at"], 1)
    base = {
        "job_id": job_id,
        "status": job["status"],
        "stage": job["stage"],
        "stage_label": _STAGE_LABELS.get(job["stage"], job["stage"]),
        "elapsed_seconds": elapsed,
        "estimated_wait_seconds": job["estimated_wait_seconds"],
        "poll_interval_seconds": _POLL_INTERVAL_SECONDS,
    }
    if job["status"] == "running":
        return {**base, "message": (
            f"数据下载中：{_STAGE_LABELS.get(job['stage'], job['stage'])}…")}
    if job["status"] == "error":
        return {**base, "error": job["error"], "error_code": job["error_code"]}
    return {**base, "result": job["result"]}
