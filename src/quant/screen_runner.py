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
import os
import sys
import time
from pathlib import Path
from typing import Any, Sequence

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
    """当前进程峰值常驻内存（Windows 用 win32 API，其它平台退回 resource）。

    2026-09-25 修：原实现在 Windows 上**恒返回 0.0**（内存体检等于瞎的 ——
    `--probe` 报 `peak_memory_mb: 0.0`，而外部采样同一进程得到 5.1 GB）。
    两个原因：

    1. `GetCurrentProcess()` 返回的是**伪句柄** `-1`。不声明
       `restype = wintypes.HANDLE` 时 ctypes 按 `c_int` 取，符号扩展之后
       是个无效句柄，调用直接失败；
    2. `psapi.GetProcessMemoryInfo` 依赖 `psapi.dll` 已被加载 ——
       现代 Windows 应优先用 kernel32 的 `K32GetProcessMemoryInfo`。
    """
    if sys.platform == "win32":
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

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            get_info = getattr(kernel32, "K32GetProcessMemoryInfo", None)
            if get_info is None:                      # 很老的 Windows 才走这里
                get_info = ctypes.WinDLL("psapi", use_last_error=True).\
                    GetProcessMemoryInfo
            get_info.argtypes = [wintypes.HANDLE,
                                 ctypes.POINTER(ProcessMemoryCounters),
                                 wintypes.DWORD]
            get_info.restype = wintypes.BOOL
            counters = ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            if get_info(kernel32.GetCurrentProcess(), ctypes.byref(counters),
                        counters.cb):
                return counters.PeakWorkingSetSize / 1024 / 1024
        except Exception:  # noqa: BLE001 非 Windows 或 API 不可用时退回
            pass
    try:
        import resource

        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    except Exception:  # noqa: BLE001
        return 0.0


# ==================================================================
# 内存预算：**整条筛选链**（区间 × 股票 × 字段 的硬约束）
# ==================================================================
#
# 2026-09-25 做了分阶段归因实测（663 个交易日 × 5320 只 × 35 因子，
# 每个阶段各跑一个独立进程，读进程自身的 PeakWorkingSetSize）：
#
# | 阶段 | 全量装配（43 字段） | 按需装配（17 字段） |
# |---|---:|---:|
# | 只 build_panels | 5,719 MB / 150s | **3,466 MB / 42s** |
# | + compute_factors | 5,731 MB / +101s | 3,557 MB / +102s |
# | + screen（整条链） | **5,925 MB** / +273s | **5,343 MB** / +229s |
#
# 两个结论都很重要：
#   1. 按需装配把**装配**这一步砍掉 39% 内存、快 3.9 倍；
#   2. 但**整条链的峰值由筛选流程本身主导**（中性化要再复制一份面板、
#      相关性聚类要给每个因子做全区间秩矩阵 ≈ 35 × 8 字节/格 ≈ 1 GB），
#      所以端到端只降了 ~10% 内存、~18% 耗时。
# 护栏估算的是"整条链"，因此必须把流程那部分算进去，否则会低估。

#: 固定开销（MB）：解释器 + pandas/numpy + 与规模无关的结构。
_SCREEN_BASE_MB = 1500.0
#: 面板部分：每个「交易日 × 股票 × 字段」格（实测约 5.9 字节，
#: 面板 + 中性化复制；取 8 字节偏保守）。
_SCREEN_MB_PER_CELL_PER_FIELD = 8.0 / 1024.0 / 1024.0
#: 流程部分：每个「交易日 × 股票」格（实测约 1050 字节 —— 相关性秩矩阵、
#: IC 序列、分组回测的中间对象；取 1200 偏保守）。
_SCREEN_MB_PER_CELL = 1200.0 / 1024.0 / 1024.0
#: 估算用的股票数：取 A 股当前全市场只数。全历史早期只数更少，故为**高估**，
#: 方向上安全（宁可提前拒绝，也不要跑到一半被 OOM 杀掉）。
ASSUMED_UNIVERSE = 5500
#: 全量装配的字段数（`PanelNeeds.everything()` ≈ 43 个字段）。
#: 只用于"没给字段数"时的默认估算（= 历史口径）。
DEFAULT_FIELD_COUNT = 51
#: 默认预算 8 GB。实测口径：663 个交易日（2024 起 / 17 字段 / 35 因子）
#: ≈ 7.6 分钟 / **5.4 GB**（通过）；约 4 年（≈1000 个交易日）时估算触到 8 GB；
#: 2590 个交易日（2015 起）≈ 19 GB（拒绝，需要显式放宽预算并腾出内存）；
#: 5037 个交易日（2006 起）≈ 35 GB（单机不可行，需要分批或抽样装配）。
DEFAULT_PANEL_BUDGET_MB = 8192.0


def panel_budget_mb() -> float:
    """当前预算（可用 `MOSS_SCREEN_MAX_PANEL_MB` 显式放宽）。"""
    try:
        value = float(os.environ.get("MOSS_SCREEN_MAX_PANEL_MB", ""))
    except (TypeError, ValueError):
        return DEFAULT_PANEL_BUDGET_MB
    return value if value > 0 else DEFAULT_PANEL_BUDGET_MB


def estimate_panel_mb(days: Sequence[str],
                      universe: int = ASSUMED_UNIVERSE,
                      fields: int = DEFAULT_FIELD_COUNT) -> float:
    """估算**整条筛选链**的峰值内存（MB）。

    式子 = 固定开销 + 面板项（格 × 字段 × 8 B）+ 流程项（格 × 1200 B）。
    系数对**分阶段实测**校准，并且刻意留了余量（估算 ≥ 实测，实测见文件顶部表格）：

    | 场景 | 交易日 | 字段 | 实测峰值 | 本式估算 |
    |---|---:|---:|---:|---:|
    | 2024 起 / 按需 | 663 | 17 | 5,406 MB | 5,998 MB |
    | 2024 起 / 全量 | 663 | 43 | 5,925 MB | 6,698 MB |

    ⚠️ 与上一版的关键差别：**流程项必须计进去**。只算面板会得出"17 个字段
    只要 2.4 GB"这种结论，而实测整条链是 5.4 GB —— 低估正是护栏里危险的方向。
    """
    cells = len(days) * max(1, int(universe))
    panel = cells * max(1, int(fields)) * _SCREEN_MB_PER_CELL_PER_FIELD
    workflow = cells * _SCREEN_MB_PER_CELL
    return _SCREEN_BASE_MB + panel + workflow


def guard_panel_budget(days: Sequence[str], *,
                       universe: int = ASSUMED_UNIVERSE,
                       fields: int = DEFAULT_FIELD_COUNT) -> str:
    """区间太长/字段太多就**提前拒绝**，返回一句体检说明。

    为什么需要这道闸：筛选跑在子进程里，OOM 只死子进程、服务没事 ——
    但用户要等几十分钟才发现任务没了。提前几秒拒绝比"跑到一半被杀"有用得多
    （这是 screen_runner 隔离设计的延伸，不是替代）。
    """
    estimate = estimate_panel_mb(days, universe, fields)
    budget = panel_budget_mb()
    if estimate > budget:
        raise ValueError(
            f"规模太大：整条筛选链预计占用约 {estimate / 1024:.1f} GB，"
            f"超过预算 {budget / 1024:.1f} GB"
            f"（{len(days)} 个交易日 × 约 {universe} 只 × {fields} 个字段）。"
            f"实测参考：663 个交易日（2024 年起）≈ 7.6 分钟 / 5.4 GB；"
            f"约 4 年是 8 GB 预算的上限；2590 个交易日（2015 年起）≈ 19 GB。"
            f"可选：缩短区间、少选几个因子、打开「股票池过滤」（按 20 日均成交额"
            f"剔除最差的 30%，列同比例变少），或用环境变量 "
            f"MOSS_SCREEN_MAX_PANEL_MB 显式放宽预算"
            f"（例如 2015 年起需要 20480，且机器要有 20 GB 以上空闲内存）。")
    return (f"内存预算体检：{len(days)} 个交易日 × {fields} 个字段 ≈ 预计峰值 "
            f"{estimate:.0f} MB（预算 {budget:.0f} MB；含面板与筛选流程两部分）")


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
        exclude_st=body.exclude_st,
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
    # **按需装配**：只装这次真正用到的字段（35 个因子实际只需要 17 个，
    # 而不是 43 个；`bak_daily` / `stk_limit` / `suspend_d` 整个数据集都不再读）。
    # 实测收益：装配 15.3s → 5.4s，峰值内存同比例下降，且结果**逐位一致**
    # （见 docs/QUANT_M2_FACTORS.md §9）。
    from src.quant.liquidity import LiquidityFilter
    from src.quant.panel_needs import needs_for_screening

    liquidity = LiquidityFilter(enabled=bool(body.liquidity_filter),
                                drop_pct=float(body.liquidity_drop_pct))
    needs = needs_for_screening(body.factors or None,
                                neutralize_mv=body.neutralize,
                                liquidity=liquidity.enabled)
    # 区间 × 字段数 一起进护栏：字段少了，能安全跑的区间就长了
    head = guard_panel_budget(days, fields=needs.field_count)
    panels = build_panels(days, needs=needs, liquidity=liquidity)
    if not panels.codes:
        raise ValueError("面板为空：请确认已下载 daily / daily_basic 数据")
    factors = compute_factors(panels, keys=body.factors or None)
    # 参数名必须与 ScreenConfig 字段**逐字对应**。
    # 这里曾把 `min_icir` 写成 `min_icr`，导致整个「开始筛选」按钮一点就崩：
    # `TypeError: ScreenConfig.__init__() got an unexpected keyword argument 'min_icr'`。
    # 这类错误 Python 会立刻报出来，但只有**真的跑一次**才会暴露 ——
    # 所以下面 `_screen_config_from_request` 有专门的单测守着。
    config = _screen_config_from_request(body)

    # ST 剔除（历史名称口径）：只有"开关打开 **且** 数据在"才生成掩码。
    # 缺数据时 screen() 会在 notes 里如实写"本次未剔除"，而不是假装剔了。
    st_mask = None
    st_available = False
    if config.exclude_st:
        from src.quant.st_status import StStatus

        status = StStatus.load()
        st_available = status.available
        if st_available:
            st_mask = status.mask(panels.dates, panels.codes)

    # 股票池过滤的逐日掩码（True = 当日剔除）。**与 ST 掩码分开传**：
    # 两个来源要各自出现在结果说明里 —— 合并之后说明会指鹿为马
    # （实测：把并集说成"已剔除 ST：13.46%"，而其中绝大部分是流动性过滤）。
    pool_mask = panels.exclusion_mask()

    result = run_screen(panels, factors, config=config, st_mask=st_mask,
                        pool_mask=pool_mask)
    out = result.as_dict()
    out["notes"] = [
        head,
        f"面板按需装配：{needs.field_count} 个字段 / "
        f"{len(needs.datasets)} 个数据集（全量为 "
        f"{DEFAULT_FIELD_COUNT} 个字段 / 10 个数据集）",
        *out.get("notes", []),
    ]
    out.update({
        "start": days[0], "end": days[-1], "trading_days": len(days),
        "universe_size": len(panels.codes), "panels_gaps": panels.gaps,
        "elapsed_seconds": round(time.perf_counter() - started, 1),
        "peak_memory_mb": round(_peak_memory_mb(), 0),
        "exclude_st": config.exclude_st, "st_available": st_available,
        "panel_fields": sorted(panels.loaded),
        "liquidity_filter": liquidity.enabled,
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
        respect_st=body.respect_st,
        train_ratio=body.train_ratio,
        costs=CostConfig(
            commission_rate=body.commission_rate,
            min_commission=body.min_commission,
            stamp_tax_rate=body.stamp_tax_rate,
            transfer_fee_rate=body.transfer_fee_rate,
            slippage_bps=body.slippage_bps))
    # ST 状态只在真的要用时读（本地小文件，但没必要给每次回测都加一步 I/O）
    st_status = None
    if body.respect_st:
        from src.quant.st_status import StStatus

        st_status = StStatus.load()
    result = run_single_backtest(panels, code, config=config, name=body.name,
                                 st_status=st_status)
    out = result.as_dict()
    out.update({
        "start": days[0], "end": days[-1], "trading_days": len(days),
        "panels_gaps": panels.gaps, "panel_origins": panels.origins[:12],
        "elapsed_seconds": round(time.perf_counter() - started, 1),
        "peak_memory_mb": round(_peak_memory_mb(), 0),
        "respect_st": bool(body.respect_st),
        "st_available": bool(st_status is not None and st_status.available),
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
