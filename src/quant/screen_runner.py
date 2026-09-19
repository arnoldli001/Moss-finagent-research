"""因子筛选的**子进程执行器**（供 API 与命令行调用，也用于内存体检）。

为什么必须独立进程（实测事故）：全市场 171 个交易日 × 5238 只 × 35 因子的中性化
与 IC 计算会占用大量内存，一旦在 web 服务进程里跑崩/耗尽内存，整个服务会**无 traceback
消失**（日志停在最后一行，Windows 事件日志里也没有 Application Error —— 是内存耗尽，
不是原生崩溃）。隔离到子进程后：算炸只死子进程，服务照常，前端能看到明确的失败原因。

用法：
    python -m src.quant.screen_runner --request req.json --response resp.json
    python -m src.quant.screen_runner --probe      # 只打印峰值内存，用于体检
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from src.core.errors import (
    BRIEF_DEFAULT,
    BRIEF_LOG,
    brief,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _read_json(path: str) -> str:
    """读 JSON 文本，容忍 BOM（PowerShell 的 `Out-File -Encoding utf8` 会带 BOM，
    直接 json.loads 会报 'Unexpected UTF-8 BOM'，实测踩过）。"""
    return Path(path).read_text(encoding="utf-8-sig")


def _peak_memory_mb() -> float:
    """当前进程峰值常驻内存（Windows 用 win32 API，其它平台退回 resource）。"""
    try:
        import ctypes
        from ctypes import wintypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        if ctypes.windll.psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), counters.cb):
            return counters.PeakWorkingSetSize / 1024 / 1024
    except Exception:  # noqa: BLE001 非 Windows 或 API 不可用时退回
        pass
    try:
        import resource

        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    except Exception:  # noqa: BLE001
        return 0.0


def _screen_config_from_request(body: Any) -> Any:
    """把 API 请求映射成 ScreenConfig。

    单独抽成函数是为了**能被单测直接调用**：参数名写错（如 `min_icir` 写成
    `min_icr`）会让「开始筛选」每次必崩，而只有真的跑一次才会暴露。
    有了这个函数，测试可以在不装配面板、不联网的情况下覆盖全部字段映射。
    """
    from src.quant.screening import ScreenConfig

    return ScreenConfig(
        ic_horizon=body.horizon,
        min_ic=body.min_ic,
        min_icir=body.min_icir,
        corr_threshold=body.corr_threshold,
        train_ratio=body.train_ratio,
        target_count=body.target_count,
        n_groups=body.n_groups,
        neutralize_mv=body.neutralize)


def run_request(payload: dict) -> dict:
    """执行一次筛选，返回可 JSON 序列化的结果。"""
    from src.api.routes.quant import ScreenRequest
    from src.quant.factor_library_v2 import compute_factors
    from src.quant.panels import build_panels
    from src.quant.screening import screen as run_screen

    body = ScreenRequest(**payload)
    started = time.perf_counter()

    from src.quant.dataset_store import DatasetStore

    start = body.start.replace("-", "")
    end = (body.end or time.strftime("%Y%m%d")).replace("-", "")
    days = [day for day in DatasetStore("daily_basic").keys()
            if start <= day <= end]
    if len(days) < 40:
        raise ValueError(f"{start}~{end} 缓存里只有 {len(days)} 个交易日，"
                         f"至少需要 40 天才能做 IC 检验")
    panels = build_panels(days)
    if not panels.codes:
        raise ValueError("面板为空：请确认已下载 daily / daily_basic 数据")
    factors = compute_factors(panels, keys=body.factors or None)
    # 参数名必须与 ScreenConfig 字段**逐字对应**。
    # 这里曾把 `min_icir` 写成 `min_icr`，导致整个「开始筛选」按钮一点就崩：
    # `TypeError: ScreenConfig.__init__() got an unexpected keyword argument 'min_icr'`。
    # 这类错误 Python 会立刻报出来，但只有**真的跑一次**才会暴露 ——
    # 所以下面 `_screen_config_from_request` 有专门的单测守着。
    config = _screen_config_from_request(body)
    result = run_screen(panels, factors, config=config)
    out = result.as_dict()
    out.update({
        "start": days[0], "end": days[-1], "trading_days": len(days),
        "universe_size": len(panels.codes), "panels_gaps": panels.gaps,
        "elapsed_seconds": round(time.perf_counter() - started, 1),
        "peak_memory_mb": round(_peak_memory_mb(), 0),
        "config": body.model_dump(),
    })
    return out


def run_single_request(payload: dict) -> dict:
    """执行一次**单股票**多因子条件回测（在同一子进程隔离里跑）。

    为什么单股票也要隔离：面板装配本身（170 天全市场 → 30 秒级、数百 MB）
    与筛选是同一条路径，服务进程里跑崩一样会无 traceback 消失。
    隔离之后"算炸只死子进程"这条保护对两个模式都成立。
    """
    from src.api.routes.quant import SingleBacktestRequest
    from src.quant.panels import build_panels
    from src.quant.single_backtest import (
        CostConfig,
        SingleBacktestConfig,
        run_single_backtest,
    )

    body = SingleBacktestRequest(**payload)
    started = time.perf_counter()
    days = _trading_days(body.start, body.end)
    if len(days) < 30:
        raise ValueError(f"{body.start}~{body.end} 缓存里只有 {len(days)} 个交易日，"
                         f"至少需要 30 天才能做单股票回测（还要留出训练/样本外）")

    code = str(body.code).zfill(6)
    # _price_panel 需要涨跌停价 → stk_limit；不需要全市场，故只装这一只票
    panels = build_panels(days, codes=[code])
    if code not in set(panels.codes) and not any(
            code in frame.columns for frame in panels.prices.values()):
        raise ValueError(
            f"{code} 不在本地数据里：请确认代码是否正确、是否已退市，"
            f"或用 scripts/quant_sync.py 补下载")

    config = SingleBacktestConfig(
        code=code, entry=body.entry, exit=body.exit,
        initial_cash=body.initial_cash, position_pct=body.position_pct,
        stop_loss_pct=body.stop_loss_pct,
        take_profit_pct=body.take_profit_pct,
        max_hold_days=body.max_hold_days, min_hold_days=body.min_hold_days,
        t_plus_1=body.t_plus_1,
        respect_price_limits=body.respect_price_limits,
        respect_suspension=body.respect_suspension,
        train_ratio=body.train_ratio,
        costs=CostConfig(
            commission_rate=body.commission_rate,
            min_commission=body.min_commission,
            stamp_tax_rate=body.stamp_tax_rate,
            transfer_fee_rate=body.transfer_fee_rate,
            slippage_bps=body.slippage_bps))
    result = run_single_backtest(panels, code, config=config, name=body.name)
    out = result.as_dict()
    out.update({
        "start": days[0], "end": days[-1], "trading_days": len(days),
        "panels_gaps": panels.gaps, "panel_origins": panels.origins[:12],
        "elapsed_seconds": round(time.perf_counter() - started, 1),
        "peak_memory_mb": round(_peak_memory_mb(), 0),
    })
    if body.auto_save:
        from src.quant.strategy_store import StrategyError, save_from_result

        try:
            record = save_from_result(out, name=body.name, source="auto",
                                      auto_saved=True)
            out["saved_strategy"] = {"id": record.id, "name": record.name,
                                     "auto_saved": True}
        except StrategyError as exc:
            # 未达门槛不算失败：回测结果照常返回，只是说明为什么没存
            out["saved_strategy"] = {"auto_saved": False, "reason": brief(exc, BRIEF_DEFAULT)}
    return out


def _trading_days(start: str, end: str) -> list[str]:
    from src.quant.dataset_store import DatasetStore

    start_key = (start or "").replace("-", "")
    end_key = (end or time.strftime("%Y%m%d")).replace("-", "")
    return [day for day in DatasetStore("daily").keys()
            if start_key <= day <= end_key]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="量化回测子进程执行器")
    parser.add_argument("--request", help="请求 JSON 文件路径")
    parser.add_argument("--response", help="结果 JSON 输出路径")
    parser.add_argument("--probe", action="store_true", help="内存体检模式")
    parser.add_argument("--mode", default="screen", choices=("screen", "single"),
                        help="screen=全市场因子筛选；single=单股票条件回测")
    args = parser.parse_args(argv)
    runner = run_single_request if args.mode == "single" else run_request

    if args.probe:
        payload = json.loads(_read_json(args.request)) if args.request else {}
        try:
            result = runner(payload)
            print(json.dumps({"ok": True,
                              "peak_memory_mb": result["peak_memory_mb"],
                              "elapsed_seconds": result["elapsed_seconds"],
                              "mode": args.mode},
                             ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                             ensure_ascii=False))
            return 1
        return 0

    if not args.request or not args.response:
        parser.error("需要 --request 与 --response")
    payload = json.loads(_read_json(args.request))
    try:
        result = runner(payload)
        Path(args.response).write_text(
            json.dumps({"ok": True, "result": result}, ensure_ascii=False),
            encoding="utf-8")
        return 0
    except Exception as exc:  # noqa: BLE001 失败原因写给父进程，不吞掉
        import traceback

        Path(args.response).write_text(json.dumps({
            "ok": False,
            "error": f"{type(exc).__name__}: {brief(exc, BRIEF_LOG)}",
            "traceback": traceback.format_exc()[-1200:],
        }, ensure_ascii=False), encoding="utf-8")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
