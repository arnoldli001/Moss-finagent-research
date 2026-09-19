"""多因子 API：因子库、数据状态、IC 筛选、分层回测（供「策略回测」页的多因子模式使用）。

长任务走**异步 job**（与 `routes/backtest.py` 同一套约定）：
全市场 171 个交易日 × 35 因子的 IC + 分层回测要几十秒，同步请求会被浏览器/代理掐断。
"""
from __future__ import annotations

import asyncio
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.api.job_table import QUANT_RETENTION, purge_jobs
from src.core.errors import (
    BRIEF_DEFAULT,
    BRIEF_LOG,
    BRIEF_TIGHT,
    brief,
)
from src.quant.dataset_store import DEFAULT_ROOT, DatasetStore

router = APIRouter(prefix="/api/v1/quant", tags=["quant"])

# 仓库根目录（子进程执行器的 cwd，保证 `-m src.quant.screen_runner` 可导入）
_REPO_ROOT = Path(__file__).resolve().parents[3]

_DISCLAIMER = ("⚠️ 因子统计与回测均为历史数据统计，不代表未来收益，"
               "不构成投资建议。因子有效性会衰减，请定期复核。")

_jobs: dict[str, dict] = {}


def clear_jobs() -> None:
    """测试用：清空任务表。"""
    _jobs.clear()


def _purge_jobs() -> None:
    purge_jobs(_jobs, QUANT_RETENTION)


# ==================================================================
# 因子库
# ==================================================================


@router.get("/factors")
async def list_factors() -> dict:
    """35 个因子的元信息（前端因子库面板直接用）。"""
    from src.quant.factor_library_v2 import (
        CATEGORY_LABELS,
        FACTORS,
        list_factor_specs,
    )

    specs = list_factor_specs()
    grouped: dict[str, list[dict]] = {name: [] for name in CATEGORY_LABELS}
    for spec in specs:
        grouped.setdefault(spec.category, []).append({
            "key": spec.key, "label": spec.label, "direction": spec.direction,
            "formula": spec.formula})
    return {
        "count": len(specs),
        "categories": [{"key": key, "label": label,
                        "count": len(grouped.get(key, [])),
                        "factors": grouped.get(key, [])}
                       for key, label in CATEGORY_LABELS.items()],
        "factors": [spec.as_dict() for spec in specs],
        "registry_size": len(FACTORS),
        "disclaimer": _DISCLAIMER,
    }


@router.get("/data-status")
async def data_status(universe: str = "a_share", root: str = DEFAULT_ROOT) -> dict:
    """本地因子数据缓存体检（前端顶部数据条用）。"""
    datasets: list[dict] = []
    import os
    from pathlib import Path

    dataset_root = Path(root) / universe
    if dataset_root.exists():
        for child in sorted(dataset_root.iterdir()):
            if not child.is_dir():
                continue
            store = DatasetStore(child.name, root=root, universe=universe)
            coverage = store.coverage()
            datasets.append({
                "dataset": child.name,
                "partitions": coverage["partitions"],
                "rows": coverage["rows"],
                "first": coverage["first"],
                "last": coverage["last"],
            })
    fundamentals = Path("data/quant/fundamentals")
    ak_share_periods = (len(list(fundamentals.glob("performance_*.csv.gz")))
                        if fundamentals.exists() else 0)
    prices = Path("data/quant/prices")
    qmt_codes = len(list(prices.glob("*.csv.gz"))) if prices.exists() else 0
    # 仓库层（回测实际读的那一层）：与 CSV 缓存分开报，否则"数据有多少"
    # 会混成两个数说不清 —— CSV 分区是采集落地，库是查询层。
    try:
        from src.quant.warehouse import warehouse_status

        warehouse = warehouse_status(root=root)
    except Exception as exc:  # noqa: BLE001 数据条不允许因此 500
        warehouse = {"available": False,
                     "error": f"{type(exc).__name__}: {brief(exc, BRIEF_TIGHT)}"}
    return {
        "universe": universe,
        "datasets": datasets,
        "akshare_periods": ak_share_periods,
        "qmt_price_codes": qmt_codes,
        "total_rows": sum(item["rows"] for item in datasets),
        "warehouse": {
            "available": warehouse.get("available", False),
            "dialect": warehouse.get("dialect", ""),
            "description": warehouse.get("description", ""),
            "tables": warehouse.get("tables", []),
            "total_rows": warehouse.get("total_rows", 0),
            "error": warehouse.get("error", ""),
        },
        "ready": any(item["dataset"] == "daily_basic" and item["partitions"] > 0
                     for item in datasets),
        "hint": ("数据未就绪：先在命令行执行 "
                 "`python scripts/quant_sync.py download --start 2026-01-01 "
                 "--end <今天>` 下载因子数据")
        if not datasets else "",
        "pid": os.getpid(),
    }


# ==================================================================
# 筛选 / 回测（异步任务）
# ==================================================================


class ScreenRequest(BaseModel):
    start: str = Field(default="2026-01-01", description="开始日期 YYYY-MM-DD")
    end: str = Field(default="", description="结束日期 YYYY-MM-DD，空=到今天")
    factors: list[str] = Field(default_factory=list,
                               description="参与筛选的因子键；空=全部 35 个")
    horizon: int = Field(default=20, ge=1, le=120, description="IC 前瞻交易日数")
    min_ic: float = Field(default=0.02, ge=0.0, le=1.0)
    min_icir: float = Field(default=0.3, ge=0.0, le=10.0)
    corr_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    train_ratio: float = Field(default=0.7, gt=0.0, lt=1.0)
    target_count: int = Field(default=20, ge=1, le=35)
    neutralize: bool = Field(default=True, description="是否做市值中性化")
    n_groups: int = Field(default=5, ge=2, le=10)


class SingleBacktestRequest(BaseModel):
    """单股票多因子条件回测请求。"""

    code: str = Field(description="股票代码（6 位，如 600519）")
    entry: str = Field(description="入场条件（DSL 时序模式），如 "
                                   "close > MA(close,20) AND momentum_20 > 0")
    exit: str = Field(default="", description="出场条件；空=只靠止损/止盈/到期")
    start: str = Field(default="", description="开始日期，空=用全部缓存")
    end: str = Field(default="", description="结束日期，空=到今天")
    name: str = Field(default="", description="策略名（保存时用）")
    initial_cash: float = Field(default=100_000.0, gt=0)
    position_pct: float = Field(default=1.0, gt=0.0, le=1.0)
    stop_loss_pct: float = Field(default=0.0, ge=0.0, lt=1.0)
    take_profit_pct: float = Field(default=0.0, ge=0.0, le=10.0)
    max_hold_days: int = Field(default=20, ge=0, le=500)
    min_hold_days: int = Field(default=1, ge=0, le=250)
    train_ratio: float = Field(default=0.7, gt=0.0, lt=1.0)
    t_plus_1: bool = Field(default=True, description="是否遵守 T+1")
    respect_price_limits: bool = Field(default=True, description="涨停不买/跌停不卖")
    respect_suspension: bool = Field(default=True, description="停牌不交易")
    commission_rate: float = Field(default=0.0003, ge=0.0, le=0.01)
    min_commission: float = Field(default=5.0, ge=0.0)
    stamp_tax_rate: float = Field(default=0.0005, ge=0.0, le=0.01)
    transfer_fee_rate: float = Field(default=0.00001, ge=0.0, le=0.01)
    slippage_bps: float = Field(default=5.0, ge=0.0, le=200.0)
    auto_save: bool = Field(default=False,
                            description="达标就自动存入策略库（一键保存）")


class StrategySaveRequest(BaseModel):
    """手动保存策略：可以直接给一次回测结果，也可以只给参数。"""

    code: str = Field(description="股票代码")
    entry: str = Field(description="入场条件")
    exit: str = Field(default="", description="出场条件")
    name: str = Field(default="", description="策略名")
    config: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = Field(
        default=None, description="回测结果快照（有则一并存下指标与自检提示）")
    thresholds: dict[str, float] | None = Field(
        default=None, description="覆盖自动保存门槛（仅 auto_save 时生效）")


def _run_screen_sync(body: ScreenRequest) -> dict:
    """**在子进程里**执行完整筛选流水线。

    为什么隔离（实测事故）：全市场 171 交易日 × 5238 只 × 35 因子的中性化/IC/回测
    会占大量内存，直接在 web 服务进程里跑，一旦崩了或耗尽内存，服务会**无 traceback
    消失**（日志停在最后一行，Windows 事件日志里也没有 Application Error）。
    隔离后：子进程崩只影响这一次任务，前端能拿到明确的失败原因，服务照常。
    也顺带解决"算完不释放内存"的问题 —— 子进程退出即全部回收。
    """
    return _run_isolated(body.model_dump(), mode="screen")


def _run_single_sync(body: SingleBacktestRequest) -> dict:
    """单股票回测也走同一套子进程隔离（面板装配是共同的重活）。"""
    return _run_isolated(body.model_dump(), mode="single")


def _run_isolated(payload: dict, *, mode: str) -> dict:
    import json
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory(prefix=f"quant_{mode}_") as tmp:
        request_path = f"{tmp}/request.json"
        response_path = f"{tmp}/response.json"
        with open(request_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        completed = subprocess.run(  # noqa: S603 固定解释器与固定参数，无 shell
            [sys.executable, "-m", "src.quant.screen_runner",
             "--request", request_path, "--response", response_path,
             "--mode", mode],
            cwd=_REPO_ROOT, capture_output=True, timeout=1800, check=False)
        try:
            with open(response_path, encoding="utf-8") as handle:
                result = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            tail = (completed.stderr or b"").decode("utf-8", "replace")[-400:]
            raise RuntimeError(
                f"回测子进程未产出结果（exit={completed.returncode}）："
                f"{tail or brief(exc, BRIEF_DEFAULT)}") from exc
    if not result.get("ok"):
        raise RuntimeError(result.get("error", "回测失败"))
    payload_out = result["result"]
    payload_out["disclaimer"] = _DISCLAIMER
    return payload_out


async def _run_job(job_id: str, body: ScreenRequest) -> None:
    job = _jobs[job_id]
    try:
        job["stage"] = "计算因子面板"
        payload = await asyncio.to_thread(_run_screen_sync, body)
        job.update(status="done", result=payload, finished_at=time.monotonic(),
                   stage="完成")
    except Exception as exc:  # noqa: BLE001 失败也写回结果，前端能看到原因
        job.update(status="failed", error=f"{type(exc).__name__}: {brief(exc, BRIEF_LOG)}",
                   finished_at=time.monotonic(), stage="失败")


async def _run_single_job(job_id: str, body: SingleBacktestRequest) -> None:
    job = _jobs[job_id]
    try:
        job["stage"] = f"装配 {body.code} 的因子面板"
        payload = await asyncio.to_thread(_run_single_sync, body)
        job.update(status="done", result=payload, finished_at=time.monotonic(),
                   stage="完成")
    except Exception as exc:  # noqa: BLE001
        job.update(status="failed", error=f"{type(exc).__name__}: {brief(exc, BRIEF_LOG)}",
                   finished_at=time.monotonic(), stage="失败")


@router.post("/backtest/single")
async def start_single_backtest(body: SingleBacktestRequest) -> dict:
    """启动单股票多因子条件回测（异步），返回 job_id。

    条件里引用的因子会在子进程里**按需计算**（只算用到的那几个），
    且面板按 `codes=[code]` 装配 —— 实测比全市场路径快得多。
    """
    _purge_jobs()
    job_id = uuid.uuid4().hex[:12]
    _jobs[job_id] = {"status": "running", "stage": "排队中",
                     "started_at": time.monotonic(),
                     "request": body.model_dump()}
    asyncio.create_task(_run_single_job(job_id, body))
    return {"job_id": job_id, "status": "running"}


@router.get("/backtest/single/{job_id}")
async def single_backtest_status(job_id: str) -> dict:
    """轮询单股票回测任务。"""
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"任务不存在或已过期：{job_id}")
    payload = {
        "job_id": job_id, "status": job["status"], "stage": job.get("stage", ""),
        "elapsed": round(time.monotonic() - job["started_at"], 1),
    }
    if job["status"] == "done":
        payload["result"] = job["result"]
    elif job["status"] == "failed":
        payload["error"] = job["error"]
    return payload


# ==================================================================
# 策略库（一键保存 / 列出 / 读取 / 删除）
# ==================================================================


@router.get("/strategies")
async def list_strategies(min_excess: float = 0.0, only_winners: bool = False,
                          code: str = "", limit: int = 200) -> dict:
    """策略档案（默认按**近一年超额**降序）。

    数据来源优先数据库（`quant_strategy` 表，可跨策略查询），
    数据库不可用时回退到 JSON 文件档案 —— 并在 `source` 里如实说明用的是哪个，
    否则"我的策略到底存哪了"会变成一个查不出来的问题。
    """
    from src.quant.warehouse import QuantWarehouse, StrategyArchive, WarehouseConfig

    config = WarehouseConfig.from_env()
    warehouse = QuantWarehouse(config)
    source = "file"
    items: list[dict] = []
    stats: dict = {}
    if warehouse.available():
        archive = StrategyArchive(warehouse, disclaimer=_DISCLAIMER)
        try:
            items = archive.list(limit=limit, code=code,
                                 only_winners=only_winners or min_excess > 0,
                                 min_excess=min_excess if min_excess else None)
            stats = archive.stats()
            source = f"db:{config.dialect}.{stats.get('table', 'quant_strategy')}"
        except Exception as exc:  # noqa: BLE001 库查询失败回退文件档案
            stats = {"error": f"{type(exc).__name__}: {brief(exc, BRIEF_DEFAULT)}"}

    if source == "file":
        from src.quant.strategy_store import strategy_store

        store = strategy_store()
        records = store.list()
        items = [_strategy_summary(record) for record in records]
        if min_excess:
            items = [item for item in items
                     if (item.get("oos_return_pct") or 0) >= min_excess]
        stats = {"available": False, "total": len(records),
                 "directory": str(store.root),
                 "hint": "数据库不可用，当前展示的是文件档案"}
    return {
        "count": len(items),
        "source": source,
        "stats": stats,
        "database": {"dialect": config.dialect,
                     "description": config.description,
                     "available": warehouse.available()},
        "filters": {"min_excess": min_excess, "only_winners": only_winners,
                    "code": code},
        "strategies": items,
        "disclaimer": _DISCLAIMER,
    }


@router.get("/help")
async def quant_help(topic: str = "factors") -> dict:
    """操作说明书（前端"?"按钮的内容，从后端返回以便与代码同步）。

    `topic`：`factors` = 多因子筛选页使用说明；`single` = 单股票策略回测说明
    （含全部 DSL 函数、面板字段、35 因子清单、全部参数与默认值）。
    """
    from src.quant.help_content import build_help

    try:
        payload = build_help(topic)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {**payload, "disclaimer": _DISCLAIMER}


@router.get("/cases")
async def list_strategy_cases(theme: str = "", source: str = "",
                              kind: str = "", limit: int = 60,
                              exclude_kinds: str = "框架/工具") -> dict:
    """开源策略案例（网络公开分享的策略线索）。

    **这些不是已验证的结论**：每条都带 `verified=0` 与来源链接，
    并有 `disclaimer` 说明它们普遍存在样本区间不明、费用未计、未来函数、
    以及只发布盈利案例的选择性报告问题。前端必须原样展示这段说明。

    `exclude_kinds` 默认排除"框架/工具"：GitHub 搜索"量化 回测"会返回大量
    回测框架（backtrader 这类），它们不是策略案例，混在表里会让人以为抓取坏了。
    """
    from src.quant.strategy_cases import DISCLAIMER
    from src.quant.warehouse import strategy_case_store

    store = strategy_case_store(disclaimer=DISCLAIMER)
    excluded = tuple(item.strip() for item in exclude_kinds.split(",")
                     if item.strip())
    items = store.list(theme=theme, source=source, kind=kind, limit=limit,
                       exclude_kinds=excluded)
    return {
        "count": len(items),
        "themes": store.themes(),
        "kinds": store.kind_stats(),
        "filters": {"theme": theme, "source": source, "kind": kind,
                    "exclude_kinds": list(excluded)},
        "cases": items,
        "verified": False,
        "note": "本节内容是自动抓取的网络公开分享，未经本项目复现验证；"
                "抓取任务见 scripts/quant_cases.py（可挂周/日定时）",
        "disclaimer": DISCLAIMER,
    }


@router.get("/stocks/search")
async def search_stocks(q: str = "", limit: int = 20,
                        types: str = "") -> dict:
    """股票联动联想：支持 **代码 / 中文名 / 拼音首字母 / 全拼**。

    例：`603083`、`jqkj`、`PAYH`、`平安`、`gzmt` 都能命中。
    数据来自本地字典（Tushare stock_basic + pypinyin 离线生成），
    字典里没有的代码（ETF/指数）会按需从东财补录一次并落库。
    """
    from src.quant.stock_directory import stock_directory

    directory = stock_directory()
    kind_filter = tuple(item.strip() for item in types.split(",") if item.strip())
    hits = directory.search(q, limit=limit, types=kind_filter)
    return {
        "query": q, "count": len(hits),
        "total_in_directory": directory.count(),
        "stocks": [entry.as_dict() for entry in hits],
        "hint": "支持代码 / 中文名 / 拼音首字母 / 全拼",
    }


@router.get("/stocks/{code}")
async def get_stock(code: str, auto_enrich: bool = True) -> dict:
    """按代码取名称与拼音（前端展示与名称补全用）。"""
    from src.quant.stock_directory import stock_directory

    directory = stock_directory()
    entry = directory.get(code, auto_enrich=auto_enrich)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"字典里没有 {code}")
    return {"stock": entry.as_dict()}


@router.get("/stocks/{code}/boards")
async def get_stock_boards(code: str, limit: int = 12) -> dict:
    """某只票的**关联概念板块**（按相关性降序）——「关联板块」输入框的默认值与联想来源。

    数据源：Tushare `ths_member(con_code=…)` 反查 + `ths_index` 名词典；
    相关性 = 0.55×窄度(1/成员数^0.35) + 0.28×人均主力净额 + 0.17×板块涨幅。
    已剔除市场级/量化标签类概念（"同花顺全A""百元股""上市首五日"等）。

    `auto_default` 是给前端**自动关联**用的：取相关性最高的那个概念名。
    """
    from src.quant.concept_repo import concept_repository
    from src.quant.stock_directory import stock_directory

    repo = concept_repository()
    try:
        name = stock_directory().name_of(code, auto_enrich=False) or ""
    except Exception:  # noqa: BLE001 名录不可用不影响概念查询
        name = ""
    boards, stale = repo.boards_of(code, name=name, limit=max(1, min(limit, 40)))
    return {
        "code": code,
        "name": name,
        "count": len(boards),
        "boards": [board.as_dict() for board in boards],
        "auto_default": boards[0].name if boards else "",
        "stale": stale,
        "note": ("概念归属来自 Tushare 同花顺指数口径；相关性=窄度(55%)+人均主力净额(28%)"
                 "+板块涨幅(17%)，已剔除市场级与量化标签概念"),
    }


@router.get("/concepts/suggest")
async def suggest_concepts(q: str = "", limit: int = 12) -> dict:
    """概念板块名联想（「关联板块」输入框打字提示用）。

    按"越窄越靠前"排序：打「芯片」时 `芯片概念` 应排在泛泛的大类前；
    同名概念会去重（实测名录里"激光雷达"有两个 ts_code）。
    """
    from src.quant.concept_repo import concept_repository

    items = concept_repository().suggest(q, limit=max(1, min(limit, 30)))
    return {"query": q, "count": len(items), "items": items,
            "hint": "概念名来自 Tushare 同花顺指数名录（已剔除市场级概念）"}


@router.post("/stocks/directory/rebuild")
async def rebuild_stock_directory() -> dict:
    """重建股票字典（从 Tushare stock_basic 批量生成代码/名称/拼音）。"""
    from src.quant.stock_directory import DirectoryError, stock_directory

    directory = stock_directory()
    try:
        report = directory.build_from_stock_basic()
    except DirectoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {**report, "dataset": "stock_basic",
            "note": "ETF/指数不在 stock_basic 里，会在首次查询时按需补录"}


@router.get("/strategies/presets")
async def list_strategy_presets() -> dict:
    """可回测的公开范式清单（含每个范式的依据与机制说明）。"""
    from src.quant.strategy_presets import list_presets

    presets = list_presets()
    return {"count": len(presets), "presets": presets,
            "note": "收录的是有公开研究支持的范式，不代表推荐使用；"
                    "有效性必须由样本外 + 跨标的篮子实测决定",
            "disclaimer": _DISCLAIMER}


@router.post("/strategies")
async def save_strategy(body: StrategySaveRequest) -> dict:
    """保存策略（一键保存入口）。

    同一套参数重复保存**不会产生新文件**，而是更新已有记录 ——
    否则试参数时会攒出几十个只差一个数字的文件。
    """
    from src.quant.strategy_store import StrategyError, strategy_store

    try:
        record = strategy_store().save(
            code=body.code, entry=body.entry, exit_condition=body.exit,
            name=body.name, config=body.config, result=body.result,
            source="api", thresholds=body.thresholds)
    except StrategyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"saved": True, "strategy": record.as_dict(),
            "directory": str(strategy_store().root),
            "disclaimer": _DISCLAIMER}


@router.post("/strategies/verdict")
async def strategy_verdict(result: dict[str, Any]) -> dict:
    """判断一条回测结果是否达标（在"保存"前先让用户看到门槛与差距）。

    用 POST 而不是 GET：入参是一整份回测结果（含逐笔成交），
    塞进 URL 查询串既超长又需要二次编码，表达上也不对（这不是"取资源"）。
    """
    from src.quant.strategy_store import DEFAULT_THRESHOLDS, verdict

    judgement = verdict(result)
    return {"verdict": judgement, "defaults": DEFAULT_THRESHOLDS,
            "disclaimer": _DISCLAIMER}


@router.get("/strategies/{strategy_id}")
async def get_strategy(strategy_id: str) -> dict:
    from src.quant.strategy_store import StrategyError, strategy_store

    try:
        record = strategy_store().load(strategy_id)
    except StrategyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"strategy": record.as_dict(), "disclaimer": _DISCLAIMER}


@router.delete("/strategies/{strategy_id}")
async def delete_strategy(strategy_id: str) -> dict:
    from src.quant.strategy_store import StrategyError, strategy_store

    try:
        removed = strategy_store().delete(strategy_id)
    except StrategyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not removed:
        raise HTTPException(status_code=404, detail=f"策略不存在：{strategy_id}")
    return {"deleted": True, "id": strategy_id}


def _strategy_summary(record: Any) -> dict:
    metrics = record.metrics or {}
    oos = (record.segments or {}).get("oos", {}) or {}
    return {
        "id": record.id, "name": record.name, "code": record.code,
        "entry": record.entry, "exit": record.exit,
        "created_at": record.created_at, "updated_at": record.updated_at,
        "auto_saved": record.auto_saved, "saved_count": record.saved_count,
        "total_return_pct": metrics.get("total_return_pct"),
        "max_drawdown_pct": metrics.get("max_drawdown_pct"),
        "sharpe": metrics.get("sharpe"),
        "trade_count": metrics.get("trade_count"),
        "win_rate_pct": metrics.get("win_rate_pct"),
        "oos_return_pct": oos.get("return_pct"),
        "oos_trades": oos.get("trades"),
        "data_range": record.data_range,
        "warning_count": len(record.warnings or []),
    }


@router.post("/screen")
async def start_screen(body: ScreenRequest) -> dict:
    """启动因子筛选任务（异步），返回 job_id。"""
    _purge_jobs()
    job_id = uuid.uuid4().hex[:12]
    _jobs[job_id] = {"status": "running", "stage": "排队中",
                     "started_at": time.monotonic(), "request": body.model_dump()}
    asyncio.create_task(_run_job(job_id, body))
    return {"job_id": job_id, "status": "running"}


@router.get("/screen/{job_id}")
async def screen_status(job_id: str) -> dict:
    """轮询筛选任务。"""
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"任务不存在或已过期：{job_id}")
    payload = {
        "job_id": job_id, "status": job["status"], "stage": job.get("stage", ""),
        "elapsed": round(time.monotonic() - job["started_at"], 1),
    }
    if job["status"] == "done":
        payload["result"] = job["result"]
    elif job["status"] == "failed":
        payload["error"] = job["error"]
    return payload


@router.get("/ic")
async def quick_ic(start: str = "2026-01-01", end: str = "",
                   horizon: int = 20) -> dict:
    """快速 IC 表（不中性化、不去重，几秒内返回，用于快速看一眼）。"""
    from src.quant.factor_library_v2 import compute_factors
    from src.quant.panels import build_panels
    from src.quant.screening import forward_returns, ic_table

    start_key = start.replace("-", "")
    end_key = (end or time.strftime("%Y%m%d")).replace("-", "")
    store = DatasetStore("daily_basic")
    days = [day for day in store.keys() if start_key <= day <= end_key][-120:]
    if len(days) < 40:
        raise HTTPException(status_code=400,
                            detail=f"缓存里只有 {len(days)} 个交易日，至少需要 40 天")

    def work() -> dict:
        panels = build_panels(days)
        factors = compute_factors(panels)
        table = ic_table(factors, forward_returns(panels.price("close"), horizon))
        return {"rows": table.replace({float("nan"): None}).to_dict("records"),
                "trading_days": len(days), "universe": len(panels.codes),
                "gaps": panels.gaps, "disclaimer": _DISCLAIMER}

    return await asyncio.to_thread(work)
