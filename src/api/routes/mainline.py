"""主线挖掘：REST 接口。

路由前缀 `/api/v1/mainline`。任务书写的是 `/api/mainline`，但本项目**所有**
路由都自带 `/api/v1` 前缀、`api_router` 本身不加前缀（见
`src/api/routes/sector_crowding.py` / `fundflow.py` 的同一写法），
所以这里也必须把 `/api/v1` 写进 prefix —— 否则真机路径会少这一层，前端全 404。

## 三类接口的同步策略

**读接口（snapshot / board / alerts / futures / backtest）—— 只读本地，快。**
它们绝不联网，因此可以在请求里同步返回。本地仓为空时返回**空结构 + `gaps`**，
而不是去联网拉一遍：一个页签的首屏不该被某个数据源超时绑住 30 秒。

**刷新接口（refresh / data/sync / backtest/run）—— 立刻返回 `task_id`。**
这三件事分别是"跑一轮打分"（几秒）、"同步几天数据"（几十秒到几分钟）、
"跑一次全区间回测"（几十分钟）。同步等待必然顶到浏览器/反代超时
（项目里已经踩过 300 秒超时的坑），因此一律后台线程 + 轮询。

**轮询接口（refresh_status）—— 单一入口。**
三类任务共用一个任务表，`task_id` 前缀区分类型。这样前端只需要一个轮询函数。

## 任务表为什么是进程内的 dict

与 `sector_crowding.refresh` 同口径：Demo 阶段单进程，任务状态放内存最简单，
重启后丢失可接受（任务本身就是"再点一次"的事）。
代价写清楚：**多进程部署时轮询会打到没有该任务的 worker 上**，
那时需要换成 Redis —— 这在 `docs/MAINLINE_MINING.md` 里写明了。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from src.core.config import get_settings
from src.core.errors import BRIEF_TIGHT, brief
from src.mainline import alert_returns, warm
from src.mainline.backtest import MainlineBacktester
from src.mainline.config import load_config, load_theme_exclusions
from src.mainline.datastore import MainlineDataStore, sync_all
from src.mainline.etf_flow import FlowSignal
from src.mainline.etf_flow import build_snapshot as build_flow_snapshot
from src.mainline.etf_flow import load_config as load_flow_config
from src.mainline.etf_flow_backtest import render as render_flow_report
from src.mainline.etf_flow_backtest import run as run_flow_backtest
from src.mainline.futures import FuturesService
from src.mainline.report import render as render_report
from src.mainline.service import MainlineService
from src.mainline.storage import build_mainline_repository, normalize_backtest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/mainline", tags=["mainline"])

#: 任务表（进程内；见模块文档的边界说明）
_TASKS: dict[str, dict[str, Any]] = {}
_TASK_LOCK = threading.Lock()
#: 任务上限：只保留最近 N 条，防止长时间运行把内存吃满
_TASK_KEEP = 40

#: 快照结果缓存 `{trade_date: (写入时刻, payload)}`。
#:
#: 为什么必须有：`/snapshot` 会**真算一遍当日评分**（全市场 1.2~15 秒，
#: 板块越多越慢），而面板左栏是常驻的、每 300 秒轮询一次 —— 没有缓存就是
#: 每 5 分钟白烧一次全市场计算。TTL 取 `data.snapshot_ttl_seconds`（默认 300），
#: 与前端轮询周期一致：既不重复算，也不会让面板停留在过期数据上。
_SNAPSHOT_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_SNAPSHOT_LOCK = threading.Lock()

#: 预热快照用的参数。**必须与前端实际请求逐项一致**，否则那份热快照永远命中不了
#: （键里带参数指纹）。前端 `MainlinePanel` 调的是
#: `mainlineApi.snapshot({top: 60, alertLimit: 100})`，其余走默认 ——
#: 所以这两个常量就是路由的默认值本身。
WARM_TOP = 60
WARM_ALERT_LIMIT = 100

#: `_data_status()` 的缓存：`{水位线: (写入时刻, payload)}`。
#:
#: 为什么必须缓存：它实测 **2.9 秒**（11 张表逐张 `COUNT`），而它一天只变一次。
#: 只热快照、不热它的话，重启后第一个请求照样要等将近 3 秒 —— 优化只做了一半。
_DATA_STATUS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_DATA_STATUS_LOCK = threading.Lock()


def _data_status_cached(watermark: str) -> dict[str, Any]:
    """按水位线缓存的 `data_status()`（见 `_DATA_STATUS_CACHE` 的说明）。"""
    if watermark:
        with _DATA_STATUS_LOCK:
            hit = _DATA_STATUS_CACHE.get(watermark)
        if hit is not None:
            return dict(hit[1])
    data = _data_status()
    if watermark:
        with _DATA_STATUS_LOCK:
            if len(_DATA_STATUS_CACHE) > 8:
                _DATA_STATUS_CACHE.clear()
            _DATA_STATUS_CACHE[watermark] = (time.monotonic(), dict(data))
    return data


# ==================================================================
# 组装（懒加载 + 单例；测试可整体替换）
# ==================================================================


class _Context:
    """服务组装缓存（配置热重载会换掉 config 对象，所以按 mtime 失效）。"""

    def __init__(self) -> None:
        self._key: tuple[Any, ...] = ()
        self.config = None
        self.flow_config: Any = None
        self.store: MainlineDataStore | None = None
        self.repo: Any = None
        self.service: MainlineService | None = None
        self.futures: FuturesService | None = None

    def get(self) -> _Context:
        config = load_config()
        try:
            mtime = config.source_path and __import__("pathlib").Path(
                config.source_path).stat().st_mtime
        except OSError:
            mtime = None
        key = (id(config), mtime)
        if key == self._key and self.service is not None:
            return self
        self.config = config
        # ETF 份额监控的配置与主线挖掘分文件（etf_flow.yaml），随 config 一起
        # 按 mtime 失效重建 —— 两者都在同一个 `_Context.get()` 里刷新。
        self.flow_config = load_flow_config()
        self.store = MainlineDataStore(config=config)
        try:
            self.repo = build_mainline_repository(get_settings())
        except Exception as exc:  # noqa: BLE001 没有仓储也能算分（只是不落盘）
            logger.warning("主线挖掘仓储不可用（降级为不落盘）：%s",
                           brief(exc, BRIEF_TIGHT))
            self.repo = None
        self.service = MainlineService(config=config, store=self.store,
                                       repo=self.repo)
        self.futures = FuturesService(config=config, store=self.store)
        self._key = key
        return self


_CONTEXT = _Context()


def _service() -> MainlineService:
    return _CONTEXT.get().service  # type: ignore[return-value]


def _futures() -> FuturesService:
    return _CONTEXT.get().futures  # type: ignore[return-value]


def _store() -> MainlineDataStore:
    return _CONTEXT.get().store  # type: ignore[return-value]


def _repo() -> Any:
    return _CONTEXT.get().repo


# ==================================================================
# 任务表
# ==================================================================


def _new_task(kind: str, message: str = "") -> str:
    task_id = f"{kind}-{uuid.uuid4().hex[:12]}"
    with _TASK_LOCK:
        if len(_TASKS) >= _TASK_KEEP:
            for key in sorted(_TASKS, key=lambda item: _TASKS[item]["at"])[
                    : max(1, _TASK_KEEP // 4)]:
                _TASKS.pop(key, None)
        _TASKS[task_id] = {"task_id": task_id, "kind": kind, "state": "running",
                           "message": message, "progress": "", "result": None,
                           "error": "", "at": time.time()}
    return task_id


def _update(task_id: str, **fields: Any) -> None:
    with _TASK_LOCK:
        slot = _TASKS.get(task_id)
        if slot is not None:
            slot.update(fields)


def _run_task(task_id: str, fn: Callable[[Callable[[str], None]], Any]
              ) -> None:
    """后台线程跑一个任务；异常一律转成 `state=failed` 而不是让线程静默死掉。"""
    def progress(text: str) -> None:
        _update(task_id, progress=text)

    try:
        result = fn(progress)
        _update(task_id, state="done", result=result, progress="完成")
    except Exception as exc:  # noqa: BLE001 后台任务的异常必须落到状态里
        logger.exception("主线挖掘后台任务失败：%s", task_id)
        _update(task_id, state="failed", error=brief(exc, BRIEF_TIGHT))


def _spawn(kind: str, fn: Callable[[Callable[[str], None]], Any],
           message: str = "") -> str:
    task_id = _new_task(kind, message)
    thread = threading.Thread(target=_run_task, args=(task_id, fn),
                              name=f"mainline-{kind}", daemon=True)
    thread.start()
    return task_id


# ==================================================================
# 读接口
# ==================================================================


@router.get("/snapshot")
async def snapshot(trade_date: str = Query(default=""),
                   top: int = Query(default=WARM_TOP, ge=1, le=500),
                   alert_limit: int = Query(default=WARM_ALERT_LIMIT,
                                            ge=1, le=1000),
                   limit_boards: int = Query(default=0, ge=0, le=2000),
                   refresh: bool = Query(default=False,
                                         description="跳过缓存强制重算")) -> dict:
    """读当前主线挖掘快照（**只读本地仓**；本地为空时返回空结构 + gaps）。

    ## 缓存键是「**数据水位线**」，不是时间 TTL

    这条原来用 `data.snapshot_ttl_seconds`（300 秒）：数据一天只变一次，
    却每 5 分钟让第一个访问者白烧一次全市场重算（实测冷算 **8.4~22.6 秒**，
    随 IO 竞争浮动），而且**每次重启缓存清零**、第一个打开面板的人替所有人挨那一下。

    现在键里带**底层板块日线的最新交易日**（`data_watermark()`，0 ms）：

        水位线没变 → 算多少遍结果都一样 → 命中，不管过了多久
        水位线变了 → 立刻失效，不需要等 TTL

    于是 `snapshot_ttl_seconds` 这个参数对 `/snapshot` **不再起作用** ——
    时间流逝本身不改变任何输入。要强制重算仍用 `refresh=true`。

    ## 落盘的那份（`src/mainline/warm.py`）

    算完会顺手把这份载荷写进 `data/mainline/warm_snapshot.json`，
    启动时（lifespan）直接装回进程内缓存。所以"进程重启"不再等于"冷启动"。
    """
    service = _service()
    # 0 ms 的 `MAX(trade_date)`；拿不到就退化成旧行为（不按水位线判有效性）
    try:
        watermark = service.data_watermark()
    except Exception as exc:  # noqa: BLE001 探针失败不该让面板打不开
        logger.warning("读数据水位线失败（本次不按水位线判缓存）：%s",
                       brief(exc, BRIEF_TIGHT))
        watermark = ""
    key = f"{trade_date}|{top}|{alert_limit}|{limit_boards}"
    cache_key = f"{watermark}|{key}"
    if not refresh:
        with _SNAPSHOT_LOCK:
            cached = _SNAPSHOT_CACHE.get(cache_key)
        if cached is not None:
            payload = dict(cached[1])
            payload["cached"] = True
            payload["cache_age"] = round(time.monotonic() - cached[0], 1)
            return payload
    payload = await asyncio.to_thread(
        _compute_snapshot_payload, trade_date, top, alert_limit, limit_boards,
        watermark)
    with _SNAPSHOT_LOCK:
        if len(_SNAPSHOT_CACHE) > 16:
            _SNAPSHOT_CACHE.clear()      # 键里带 top/limit，防参数枚举把内存撑满
        _SNAPSHOT_CACHE[cache_key] = (time.monotonic(), dict(payload))
    return payload


def _compute_snapshot_payload(trade_date: str, top: int, alert_limit: int,
                              limit_boards: int, watermark: str,
                              root: Path | None = None) -> dict[str, Any]:
    """算一份快照载荷（同步；调用方负责丢线程）。

    算完**顺手落盘**成热快照 —— 这样"任何一次现算"都同时把下一次重启的
    冷启动也消掉了（包括用户点「立即刷新」那次）。
    """
    service = _service()
    snap = service.score_date_sync(trade_date, limit_boards=limit_boards)
    payload = snap.to_dict(top=top, alert_limit=alert_limit)
    status = _data_status_cached(watermark or snap.trade_date)
    payload["data_status"] = status
    payload["cached"] = False
    _persist_warm(payload, watermark=watermark or snap.trade_date,
                  key=f"{trade_date}|{top}|{alert_limit}|{limit_boards}",
                  data_status=status, root=root)
    return payload


def _persist_warm(payload: dict[str, Any], *, watermark: str, key: str,
                  data_status: dict[str, Any] | None = None,
                  root: Path | None = None) -> None:
    """落一份热快照（best-effort，失败只记日志）。"""
    try:
        warm.save(warm.make({}, watermark=watermark, key=key,
                            payload=payload, data_status=data_status),
                  root=root)
    except Exception as exc:  # noqa: BLE001 缓存写不进去不影响本次结果
        logger.warning("主线热快照落盘失败：%s", brief(exc, BRIEF_TIGHT))


def warm_persist(snap: Any, *,
                 top: int = WARM_TOP,
                 alert_limit: int = WARM_ALERT_LIMIT,
                 limit_boards: int = 0,
                 root: Path | None = None) -> bool:
    """把**一份已经算好的**快照落成热快照（`mainline_daily` 用，不重复计算）。

    返回是否落盘成功。参数默认值与前端实际请求一致（见 `WARM_TOP` 的说明）。
    `root` 只给测试用（把落盘位置指到临时目录）。
    """
    try:
        watermark = str(getattr(snap, "trade_date", "") or "")
        payload = snap.to_dict(top=top, alert_limit=alert_limit)
        status = _data_status_cached(watermark)
        payload["data_status"] = status
        payload["cached"] = False
        warm.save(warm.make({}, watermark=watermark,
                            key=f"|{top}|{alert_limit}|{limit_boards}",
                            payload=payload, data_status=status), root=root)
        return True
    except Exception as exc:  # noqa: BLE001 预热失败不该让日更作业判 failed
        logger.warning("主线热快照预热失败：%s", brief(exc, BRIEF_TIGHT))
        return False


def warm_refresh(*, root: Path | None = None) -> dict[str, Any]:
    """**重算并落盘**一份热快照（定时预热作业用）。返回落盘信息。"""
    service = _service()
    watermark = service.data_watermark()
    payload = _compute_snapshot_payload("", WARM_TOP, WARM_ALERT_LIMIT, 0,
                                        watermark, root=root)
    return {"watermark": watermark, "boards": len(payload.get("scores") or []),
            "alerts": len(payload.get("alerts") or [])}


def warm_load(*, root: Path | None = None) -> dict[str, Any]:
    """启动时把落盘的热快照装进进程内缓存（lifespan 调，best-effort）。

    为什么必须做：进程内缓存每次重启清零，于是**重启后第一个打开面板的人**
    要等一次全市场重算（实测 8.4~22.6 秒）。2026-09-25 一天为了发布重启 5 次，
    就是 5 次。装上之后重启 = 第一个请求也是毫秒级。

    返回一行可打日志的摘要；**任何失败都不抛**（缓存坏了不该拦住启动）。
    """
    try:
        data = warm.load(root=root, force=True)
        if not data:
            return {"loaded": False, "reason": "还没有落盘的热快照"}
        service = _service()
        watermark = service.data_watermark()
        key = f"|{WARM_TOP}|{WARM_ALERT_LIMIT}|0"
        payload = warm.payload_for(data, watermark=watermark, key=key)
        if payload is None:
            # 水位线或参数对不上：留着文件（下次现算会覆盖），本次不装
            return {"loaded": False, "reason": "水位线不匹配（数据已更新）",
                    "file_watermark": str(data.get("watermark") or ""),
                    "now": watermark}
        status = data.get("data_status") or {}
        with _SNAPSHOT_LOCK:
            _SNAPSHOT_CACHE[f"{watermark}|{key}"] = (time.monotonic(),
                                                     dict(payload))
        if status:
            with _DATA_STATUS_LOCK:
                _DATA_STATUS_CACHE[watermark] = (time.monotonic(), dict(status))
        return {"loaded": True, "watermark": watermark,
                "boards": len(payload.get("scores") or []),
                "alerts": len(payload.get("alerts") or []),
                "age_seconds": warm.age_seconds(data)}
    except Exception as exc:  # noqa: BLE001 启动预热失败不该拦住服务启动
        logger.warning("主线热快照启动加载失败（不影响启动）：%s",
                       brief(exc, BRIEF_TIGHT))
        return {"loaded": False, "reason": type(exc).__name__}
    with _SNAPSHOT_LOCK:
        if len(_SNAPSHOT_CACHE) > 16:
            _SNAPSHOT_CACHE.clear()      # 键里带 top/limit，防参数枚举把内存撑满
        _SNAPSHOT_CACHE[key] = (time.monotonic(), payload)
    return payload


@router.get("/saved")
async def saved(trade_date: str = Query(default=""),
                limit: int = Query(default=500, ge=1, le=2000)) -> dict:
    """读**已落库**的某日评分榜（不重算）。

    与 `/snapshot` 的区别：这个接口毫秒级返回，用于面板首屏先出内容、
    再等 `/snapshot` 算完替换 —— 避免每次打开页签都等一轮打分。
    """
    repo = _repo()
    if repo is None:
        return {"trade_date": trade_date, "scores": [], "gaps": ["存储不可用"]}
    scores = await repo.load_scores(trade_date, limit=limit)
    return {"trade_date": trade_date, "scores": scores,
            "count": len(scores),
            "latest": await repo.latest_trade_date()}


@router.get("/board/{code}")
async def board_detail(code: str, trade_date: str = Query(default=""),
                       days: int = Query(default=60, ge=5, le=500)) -> dict:
    """单个板块下钻：三层明细（含每个维度的 raw）+ 近期评分序列。"""
    service = _service()
    repo = _repo()
    raw_scores = await repo.score_history(code, limit=days) if repo else []
    history: list[dict[str, Any]] = []
    for row in reversed(raw_scores or []):
        history.append({
            "trade_date": str(row.get("trade_date") or ""),
            "total": row.get("total"), "six_dim": row.get("six_dim"),
            "accumulation": row.get("accumulation"),
            "level": str(row.get("level") or "")})
    if not history:
        # 没有落库历史时，至少把当前这一天的分算出来返回，避免面板全空
        snap = await asyncio.to_thread(service.score_date_sync, trade_date)
        target = next((item for item in snap.scores if item.code == code), None)
        if target is None:
            raise HTTPException(404, f"板块 {code} 不在本地板块目录或当日无数据")
        return {"code": code, "name": target.name,
                "trade_date": snap.trade_date, "score": target.to_dict(),
                "layers": [layer.to_dict() for layer in target.layers],
                "history": [], "tracked": None,
                "note": "本地还没有该板块的评分历史（继续运行即可累积）"}
    latest = raw_scores[0]
    payload = latest.get("payload") or {}
    return {"code": code,
            "name": str(latest.get("board_name") or payload.get("name") or ""),
            "trade_date": str(latest.get("trade_date") or ""),
            "score": payload,
            "layers": payload.get("layers") or [],
            "history": history,
            "tracked": None}


@router.get("/alerts")
async def alerts(level: str = Query(default=""),
                 start: str = Query(default=""), end: str = Query(default=""),
                 limit: int = Query(default=200, ge=1, le=2000)) -> dict:
    """读告警流水（新的在前）。`level` 支持 ``"strong,medium"`` 逗号列表。"""
    repo = _repo()
    if repo is None:
        return {"alerts": [], "count": 0, "gaps": ["存储不可用"]}
    rows = await repo.load_alerts(level=level, start=start, end=end, limit=limit)
    return {"alerts": rows, "count": len(rows)}


@router.get("/futures")
async def futures_dashboard(trade_date: str = Query(default=""),
                            top: int = Query(default=89, ge=1, le=200)) -> dict:
    """期货先行信号仪表盘（仪表盘 / 期股联动热力图 / 映射 / 告警）。"""
    service = _futures()
    dashboard = await asyncio.to_thread(service.dashboard, trade_date, top=top)
    payload = dashboard.to_dict(top=top)
    payload["chain_titles"] = service.system_titles()
    return payload


@router.get("/futures/mappings")
async def futures_mappings() -> dict:
    """读期货品种↔板块映射表（面板的"映射详情"直接用）。"""
    service = _futures()
    return {"mappings": [item.to_dict() for item in service.mappings],
            "chains": service.system_titles(),
            "gap": service.mapping_gap}


@router.post("/futures/calibrate")
async def futures_calibrate(as_of: str = Query(default="")) -> dict:
    """用过去 250 个交易日滚动相关校准映射强度（需求 5.3）。"""
    service = _futures()
    return await asyncio.to_thread(service.calibrate, as_of=as_of)


@router.get("/backtest")
async def backtest(run_id: str = Query(default=""),
                   signal_limit: int = Query(default=300, ge=1, le=2000)
                   ) -> dict:
    """读一次回测结果（`run_id` 为空取最近一次），含 Markdown 报告正文。

    ⚠️ **两条返回路径都必须过 `normalize_backtest`**：空壳分支早先手写了
    `"correlation": {}`，而 `{}` 在前端是真值 —— `report.correlation ?? null`
    兜不住它，紧接着的 `correlation.factors.length` 抛
    `TypeError: Cannot read properties of undefined (reading 'length')`，
    整个「回测报告」页签白屏。契约的**形状**只能有一处定义。
    """
    repo = _repo()
    if repo is None:
        return normalize_backtest(
            {"error": "存储不可用", "gaps": ["主线挖掘仓储未初始化"]})
    data = await repo.load_backtest(run_id)
    if not data:
        return normalize_backtest({"gaps": ["还没有回测记录"]})
    return normalize_backtest(data)


@router.get("/backtest/list")
async def backtest_list(limit: int = Query(default=20, ge=1, le=100)) -> dict:
    """回测历史摘要列表（不含 Markdown 正文）。"""
    repo = _repo()
    if repo is None:
        return {"runs": [], "gaps": ["存储不可用"]}
    return {"runs": await repo.list_backtests(limit=limit)}


async def _alert_returns_payload(
        *, start: str, end: str, level_list: list[str], split: str,
        dedup_days: int, horizon_list: list[int], leaders: bool,
        stock_window: int, top_leaders: int, min_win_rate: float,
        history_only: bool, limit: int, refresh: bool) -> dict:
    """算（或取缓存）「回测收益展示」的载荷 —— `/alert-returns` 与
    `/board-win-rates` **共用这一个函数与同一把缓存键**。

    为什么必须共用：告警流水页签要的只是板块级"20 日胜率过没过门槛"这一件事。
    若它为这件事另算一遍（哪怕口径写对），一旦哪天两边有一处口径漂移，
    同一句"20 日胜率"就会在两个页签里显示不同的数 —— 这是最难被发现的一类故障。
    共用之后，第二个页签通常是**零成本**的：第一个页签已经把结果放进了缓存。

    ⚠️ 两个接口的**默认参数必须逐项一致**，否则缓存键对不上、每个页签各算一遍
    （不会报错，只是白花 0.5 秒 ×N）。`board_win_rates` 因此全部取
    `alert_returns.DEFAULT_*` 常量，而不是另写一组字面量。
    """
    repo = _repo()
    if repo is None:
        return {"rows": [], "boards": [], "stats": {},
                "gaps": ["存储不可用"]}

    key = "|".join(str(item) for item in (
        start, end, ",".join(level_list), split, dedup_days,
        ",".join(str(h) for h in horizon_list), leaders, stock_window,
        top_leaders, min_win_rate, history_only, limit))
    if not refresh:
        cached = alert_returns.cache_get(key)
        if cached is not None:
            return {**cached, "cached": True}

    # ⚠️ `limit` 给足：仓储的默认 200 会让面板只看到最近 200 条告警，
    # 而"历史数据（最早到 2026.8.25）"要求的是**全量**。
    alerts = await repo.load_alerts(level=",".join(level_list) if level_list else "",
                                    start=start, end=end, limit=200000)
    # 池级剔除清单（按 20 日胜率生成）从 config 读出来传进去：`alert_returns`
    # 本身不依赖 config，见它 `build()` 里 `excluded_boards` 的说明。
    excluded_boards = load_theme_exclusions(
        getattr(_CONTEXT.get().config.universe, "theme_exclude_file",
                "mainline_theme_exclusions.yaml"))
    payload = await asyncio.to_thread(
        alert_returns.build, alerts, _store(), start=start, end=end,
        levels=level_list or alert_returns.DEFAULT_LEVELS,
        horizons=horizon_list, dedup_days=dedup_days, split=split,
        leaders=leaders, stock_window=stock_window, top_leaders=top_leaders,
        min_win_rate=min_win_rate, history_only=history_only,
        excluded_boards=excluded_boards)
    payload["cached"] = False
    if limit > 0:
        payload["rows"] = payload["rows"][:limit]
    alert_returns.cache_put(key, payload)
    return payload


def _horizon_list(text: str) -> list[int]:
    """把 `"7,20,60"` 解析成正整数列表；全非法时回落到默认周期。"""
    out: list[int] = []
    for item in (text or "").split(","):
        try:
            value = int(item.strip())
        except (TypeError, ValueError):
            continue
        if value > 0:
            out.append(value)
    return out or list(alert_returns.DEFAULT_HORIZONS)


@router.get("/alert-returns")
async def alert_returns_panel(
        start: str = Query(default="", description="YYYYMMDD；留空 = 最早"),
        end: str = Query(default="", description="YYYYMMDD；留空 = 最新"),
        levels: str = Query(default="strong,medium",
                            description="逗号列表；默认只追踪中信号与强信号"),
        split: str = Query(default="20260825",
                           description="历史 / 近期 的分界日（含）"),
        dedup_days: int = Query(default=10, ge=0, le=60,
                                description="同一概念多少交易日内重复触发折叠成 X2/X3"),
        horizons: str = Query(default="7,20,60",
                              description="追踪周期（交易日），逗号列表"),
        leaders: bool = Query(default=False,
                              description="是否计算区间领涨成分股（+5~10 秒）"),
        stock_window: int = Query(default=20, ge=1, le=120),
        top_leaders: int = Query(default=3, ge=1, le=10),
        min_win_rate: float = Query(default=0.4, ge=0.0, le=1.0,
                                    description="板块筛选：20 日胜率门槛，**严格大于**它才显示"),
        history_only: bool = Query(default=False),
        limit: int = Query(default=0, ge=0, le=5000,
                           description="只返回前 N 行（0 = 全部）"),
        refresh: bool = Query(default=False, description="跳过缓存强制重算"),
) -> dict:
    """信号收益追踪面板：信号后 7/20/60 日最大涨幅 + 实际收益 + 区间领涨成分股。

    ## 为什么拆成一次请求里的两段（`leaders`）

    板块级只要 0.5 秒，领涨成分股要走 15 GiB 行情仓、约 6 秒。让首屏为一个
    "增强列"多等 6 秒不值得，所以前端**先不带 `leaders` 拉一次**把表格画出来，
    再带 `leaders=true` 补一次（结果进进程内缓存，之后都是毫秒级）。

    ## 口径

    * 「最大涨幅」= 信号日收盘 → 窗口内最高价的最大偏离（理论空间，未扣成本）
    * 「实际收益」= 窗口末收盘涨跌幅；「最多浮亏」= 窗口内最低价
    * 窗口**没走满不给最终值**（"暂时不填"），只在 `current_*` 里给至今进度
    * 同一概念 `dedup_days` 个交易日内重复触发折叠成 `alert_count`（X2/X3）
    * **板块筛选走 20 日胜率**：`boards[].win_rate_20d` **不大于** `min_win_rate` 的板块
      判为 `gate="hidden"`（前端不显示它的排行与逐条行）——**严格大于**，恰好等于门槛
      的也隐藏；一条 20 日窗口都没走满的板块是 `gate="pending"`，**不隐藏**（无从判定）
    """
    # ⚠️ 处理函数**不能**叫 `alert_returns`：那会把上面 import 的模块名遮住，
    # 于是 `alert_returns.cache_get` 变成"函数对象上没有这个属性"
    # （`AttributeError: 'function' object has no attribute 'cache_get'`）。
    level_list = [item.strip() for item in (levels or "").split(",") if item.strip()]
    return await _alert_returns_payload(
        start=start, end=end, level_list=level_list, split=split,
        dedup_days=dedup_days, horizon_list=_horizon_list(horizons),
        leaders=leaders, stock_window=stock_window, top_leaders=top_leaders,
        min_win_rate=min_win_rate, history_only=history_only, limit=limit,
        refresh=refresh)


@router.get("/board-win-rates")
async def board_win_rates(
        min_win_rate: float = Query(default=0.4, ge=0.0, le=1.0,
                                    description="20 日胜率门槛，**不大于**它的板块判为 hidden"),
        refresh: bool = Query(default=False, description="跳过缓存强制重算"),
) -> dict:
    """板块级「20 日胜率过没过门槛」—— 给告警流水页签隐藏不达标板块的强/中告警。

    载荷刻意做小：调用方只要一个 `board_code → 是否隐藏` 的映射，不需要行。
    参数与 `/alert-returns` 的默认值逐项一致，所以两个页签共用一份缓存结果。
    """
    payload = await _alert_returns_payload(
        start="", end="",
        level_list=list(alert_returns.DEFAULT_LEVELS),
        split=alert_returns.DEFAULT_SPLIT,
        dedup_days=alert_returns.DEFAULT_DEDUP_DAYS,
        horizon_list=list(alert_returns.DEFAULT_HORIZONS),
        leaders=False,
        stock_window=alert_returns.DEFAULT_STOCK_WINDOW,
        top_leaders=alert_returns.DEFAULT_LEADERS,
        min_win_rate=min_win_rate, history_only=False, limit=0, refresh=refresh)

    boards = [{
        "board_code": item.get("board_code", ""),
        "board_name": item.get("board_name", ""),
        "signals": item.get("signals", 0),
        "done_20d": item.get("done_20d", 0),
        "win_rate_20d": item.get("win_rate_20d"),
        "gate": item.get("gate", ""),
        "hidden": item.get("gate") == "hidden",
    } for item in (payload.get("boards") or [])]
    return {
        "as_of": payload.get("as_of", ""),
        "generated_at": payload.get("generated_at", ""),
        "min_win_rate": float(min_win_rate),
        "levels": list(alert_returns.DEFAULT_LEVELS),
        "boards": boards,
        "hidden": sum(1 for item in boards if item["hidden"]),
        "pending": sum(1 for item in boards if item["gate"] == "pending"),
        "cached": bool(payload.get("cached", False)),
        # "没有请求领涨股"这条缺口与本接口无关，滤掉免得读成故障
        "gaps": [gap for gap in (payload.get("gaps") or [])
                 if "leaders=true" not in gap],
    }


@router.get("/data/status")
async def data_status() -> dict:
    """本地数据仓状态：路径、各表行数、同步台账。"""
    return _data_status()


def _data_status() -> dict[str, Any]:
    store = _store()
    try:
        return {"cache_path": store.path, "tables": store.stats(),
                "sync": store.sync_status(), "gaps": []}
    except Exception as exc:  # noqa: BLE001 状态读不到不该让面板 500
        return {"cache_path": getattr(store, "path", ""), "tables": {},
                "sync": [], "gaps": [brief(exc, BRIEF_TIGHT)]}


# ==================================================================
# 任务接口
# ==================================================================


class RefreshRequest(BaseModel):
    trade_date: str = Field(default="", description="留空 = 本地最新交易日")
    sync_days: int = Field(
        default=0, ge=0, le=2000,
        description="先同步最近 N 个自然日的数据再打分（0 = 只读本地仓不联网）")
    datasets: list[str] = Field(
        default_factory=list,
        description="指定同步的数据集；留空 = 按依赖顺序同步全部")


@router.post("/refresh")
async def refresh(body: RefreshRequest | None = None) -> dict:
    """一键刷新主线挖掘（可选先同步数据，再跑一轮打分）。"""
    payload = body or RefreshRequest()
    store = _store()
    service = _service()

    def job(progress: Callable[[str], None]) -> dict[str, Any]:
        sync_notes: list[str] = []
        if payload.sync_days > 0:
            from datetime import datetime, timedelta

            end = payload.trade_date or datetime.now().strftime("%Y%m%d")
            start = (datetime.strptime(end, "%Y%m%d")
                     - timedelta(days=int(payload.sync_days))).strftime("%Y%m%d")
            progress(f"同步数据 {start}~{end}")
            results = sync_all(store, start=start, end=end,
                               datasets=payload.datasets or None,
                               progress=lambda name: progress(f"同步 {name}"))
            sync_notes = [item.note for item in results]
        progress("计算主线评分")
        snap = service._compute(  # noqa: SLF001 后台任务里直接调同步内核
            payload.trade_date, 0, [])
        result = snap.to_dict()
        result["sync_notes"] = sync_notes
        result["data_status"] = _data_status()
        return result

    task_id = _spawn("refresh", job, "刷新主线挖掘")
    return {"ok": True, "task_id": task_id}


class SyncRequest(BaseModel):
    start: str = Field(default="", description="YYYYMMDD；留空 = 最近 90 天")
    end: str = Field(default="", description="YYYYMMDD；留空 = 今天")
    datasets: list[str] = Field(default_factory=list,
                                description="留空 = 全部（按依赖顺序）")


@router.post("/data/sync")
async def data_sync(body: SyncRequest | None = None) -> dict:
    """同步本地数据仓（板块目录 / 指数 / 资金流 / 两融 / 北向 / 龙虎榜 / 宏观 / 期货）。"""
    payload = body or SyncRequest()
    store = _store()

    def job(progress: Callable[[str], None]) -> dict[str, Any]:
        from datetime import datetime, timedelta

        end = payload.end or datetime.now().strftime("%Y%m%d")
        start = payload.start or (
            datetime.strptime(end, "%Y%m%d") - timedelta(days=90)
        ).strftime("%Y%m%d")
        progress(f"同步 {start}~{end}")
        results = sync_all(store, start=start, end=end,
                           datasets=payload.datasets or None,
                           progress=lambda name: progress(f"同步 {name}"))
        return {"start": start, "end": end,
                "results": [item.to_dict() for item in results],
                "notes": [item.note for item in results],
                "tables": store.stats()}

    task_id = _spawn("sync", job, "同步主线数据")
    return {"ok": True, "task_id": task_id}


class BacktestRequest(BaseModel):
    start: str = Field(default="", description="YYYYMMDD；留空 = 数据起点")
    end: str = Field(default="", description="YYYYMMDD；留空 = 数据终点")
    step_days: int = Field(default=0, ge=0, le=60,
                           description="采样步长（交易日）；0 = 用配置里的值")
    save: bool = Field(default=True, description="是否落库（报告可在 /backtest 读回）")


class EtfFlowBacktestRequest(BaseModel):
    start: str = Field(default="", description="YYYYMMDD；留空 = 配置里的起点")
    end: str = Field(default="", description="YYYYMMDD；留空 = 本地数据终点")


@router.post("/backtest/run")
async def backtest_run(body: BacktestRequest | None = None) -> dict:
    """跑一次 Walk-Forward 回测（后台线程；用 `/refresh_status` 轮询）。"""
    payload = body or BacktestRequest()
    context = _CONTEXT.get()
    config = context.config

    def job(progress: Callable[[str], None]) -> dict[str, Any]:
        if payload.step_days:
            config.backtest.step_days = int(payload.step_days)
        runner = MainlineBacktester(config=config, store=context.store)
        report = runner.run(payload.start, payload.end, progress=progress)
        report.markdown = render_report(report, config)
        repo = context.repo
        if payload.save and repo is not None and not report.error:
            import asyncio as _asyncio

            try:
                _asyncio.run(repo.save_backtest(report))
            except Exception as exc:  # noqa: BLE001 落库失败不该丢掉本次结果
                logger.warning("回测落库失败：%s", brief(exc, BRIEF_TIGHT))
                report.gaps.append(f"回测结果未能落库：{brief(exc, BRIEF_TIGHT)}")
        return report.to_dict()

    task_id = _spawn("backtest", job, "Walk-Forward 回测")
    return {"ok": True, "task_id": task_id}


@router.get("/relevance")
async def relevance_stats() -> dict:
    """成分股提纯状态（只读本地，不联网）。

    回答三个问题：

    1. **提纯数据齐了吗** —— 打分覆盖股票数、走势相关性对数、题材/板块映射数；
    2. **提纯力度多大** —— 各板块成分股「前 → 后」以及剔除原因的分解；
    3. **口径是什么** —— 相关性前 N、总市值门槛、是否排除 ST（供前端如实展示）。

    数据由 `scripts/mainline_relevance.py` 离线产出；本接口**只读**，
    缺数据时返回 `ready=False` 与原因，而不是伪造 0。
    """
    context = _CONTEXT.get()
    config = context.config
    rel = config.relevance
    path = config.cache_file
    out: dict[str, Any] = {
        "enabled": bool(rel.enabled),
        "ready": False,
        "criterion": {
            "kinds": list(rel.kinds),
            "top_themes": rel.top_themes,
            "min_total_mv": rel.min_total_mv,
            "exclude_st": rel.exclude_st,
            "corr_weight": rel.corr_weight,
            "corr_window": rel.corr_window,
            "llm_tier": rel.llm_tier,
        },
        "gaps": [],
    }
    if not rel.enabled:
        out["gaps"].append("成分股提纯已关闭（config.relevance.enabled=false）")
        return out
    if not path.exists():
        out["gaps"].append(f"主线数据仓不存在：{path}")
        return out

    from src.mainline.relevance import RelevanceStore

    store = RelevanceStore(path)
    conn = store.connect()
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table'"
            " AND name='ml_member_clean'").fetchone()
        if exists is None:
            out["gaps"].append("提纯表不存在：请先执行 "
                               "python scripts/mainline_relevance.py --stage all")
            return out
        corr = store.corr_stats(conn)
        score = store.score_coverage(conn)
        before = int(conn.execute(
            "SELECT COUNT(*) FROM ml_member m JOIN ml_theme_board t"
            " ON t.board_code = m.board_code").fetchone()[0])
        after = int(conn.execute(
            "SELECT COUNT(*) FROM ml_member_clean WHERE relevant = 1"
        ).fetchone()[0])
        themes = int(conn.execute(
            "SELECT COUNT(DISTINCT theme) FROM ml_theme_board").fetchone()[0])
        out.update({
            "ready": bool(score["codes"] and after),
            "stocks_scored": score["codes"],
            "theme_records": score["pairs"],
            "corr_pairs": corr["pairs"],
            "corr_boards": corr["boards"],
            "corr_stocks": corr["codes"],
            "themes": themes,
            "pairs_before": before,
            "pairs_after": after,
            "keep_ratio": round(after / before, 4) if before else 0.0,
        })
        if not score["codes"]:
            out["gaps"].append(
                "还没有打分结果：请先执行 "
                "python scripts/mainline_relevance.py --stage score")
        elif after < before:
            out["gaps"].append(f"已剔除 {before - after} 个成分股归属"
                               f"（保留 {after / before:.0%}）")
        # 逐板块明细（供前端展示"哪些板块被剔得最狠"）
        rows = conn.execute(
            "SELECT m.board_code, b.name,"
            " COUNT(*) AS before,"
            " SUM(CASE WHEN c.relevant = 1 THEN 1 ELSE 0 END) AS after"
            " FROM ml_member m JOIN ml_theme_board t ON t.board_code = m.board_code"
            " JOIN ml_board b ON b.code = m.board_code"
            " LEFT JOIN ml_member_clean c ON c.board_code = m.board_code"
            " AND c.code = m.code"
            " GROUP BY m.board_code, b.name"
            " HAVING before > 0 ORDER BY before DESC LIMIT 200").fetchall()
        out["boards"] = [{
            "board_code": str(r["board_code"]),
            "name": str(r["name"] or ""),
            "before": int(r["before"] or 0),
            "after": int(r["after"] or 0),
        } for r in rows]
        return out
    finally:
        conn.close()


@router.get("/refresh_status")
async def refresh_status(task_id: str = Query(default="")) -> dict:
    """查询后台任务状态（三类任务共用；`task_id` 留空返回最近一次）。"""
    with _TASK_LOCK:
        if not task_id:
            if not _TASKS:
                return {"task_id": "", "state": "idle", "message": "还没有任务",
                        "progress": "", "result": None, "error": ""}
            task_id = max(_TASKS, key=lambda item: _TASKS[item]["at"])
        slot = _TASKS.get(task_id)
        if slot is None:
            return {"task_id": task_id, "state": "unknown",
                    "message": "任务不存在或已过期（服务可能重启过）",
                    "progress": "", "result": None, "error": ""}
        return dict(slot)


@router.get("/tasks")
async def tasks(limit: int = Query(default=20, ge=1, le=100)) -> dict:
    """最近的后台任务列表（面板上显示"上次同步/回测是什么时候"）。"""
    with _TASK_LOCK:
        items = sorted(_TASKS.values(), key=lambda item: -item["at"])[:limit]
        return {"tasks": [{"task_id": item["task_id"], "kind": item["kind"],
                           "state": item["state"], "message": item["message"],
                           "progress": item["progress"], "error": item["error"]}
                          for item in items]}


# ==================================================================
# ETF 份额监控
# ==================================================================
# 与主线挖掘共用这个 router / 这个库：它在需求里是「资金流监控」下的一个页签，
# 但数据与服务都归 mainline 模块维护，单独开一个 router 只会让两条路由
# 各自持有半个 _CONTEXT。


def _flow_config() -> Any:
    """ETF 份额监控配置（缓存于 `_CONTEXT`，避免每次请求重读 YAML）。"""
    return _CONTEXT.get().flow_config


def _flow_snapshot_sync(trade_date: str) -> dict:
    snap = build_flow_snapshot(_store(), config=_flow_config(),
                               trade_date=trade_date)
    return snap.to_dict()


@router.get("/etf-flow/snapshot")
async def etf_flow_snapshot(trade_date: str = Query(default=""),
                            refresh: bool = Query(
                                default=False, description="跳过缓存强制重算")
                            ) -> dict:
    """ETF 份额监控快照：环境判定 + 指标 + 分位 + 信号 + 共振。

    带 TTL 缓存（与 `/snapshot` 同一策略）：面板常驻轮询时，
    重算一次要读 9 只 ETF 约 270 行 + 4 条指数，虽不贵但没必要每轮都做。
    """
    config = _flow_config()
    ttl = float(getattr(config, "snapshot_ttl_seconds", 300.0) or 300.0)
    key = f"etf-flow|{trade_date}"
    now = time.monotonic()
    if not refresh:
        with _SNAPSHOT_LOCK:
            cached = _SNAPSHOT_CACHE.get(key)
        if cached is not None and now - cached[0] < ttl:
            payload = dict(cached[1])
            payload["cached"] = True
            payload["cache_age"] = round(now - cached[0], 1)
            return payload
    try:
        payload = await asyncio.to_thread(_flow_snapshot_sync, trade_date)
    except Exception as exc:  # noqa: BLE001 单页数据不该让整页 500
        logger.warning("ETF 份额快照失败：%s", brief(exc, BRIEF_TIGHT))
        return {"trade_date": trade_date, "indicators": [], "positions": [],
                "signals": [], "resonance": [], "signal_count": 0,
                "alert_count": 0, "gated_count": 0,
                "regime": {"key": "range", "label": "震荡市",
                           "opportunity_allowed": False, "risk_allowed": True},
                "gaps": [f"快照生成失败：{brief(exc, BRIEF_TIGHT)}"],
                "cached": False}
    payload["cached"] = False
    with _SNAPSHOT_LOCK:
        if len(_SNAPSHOT_CACHE) > 16:
            _SNAPSHOT_CACHE.clear()
        _SNAPSHOT_CACHE[key] = (time.monotonic(), payload)
    return payload


@router.get("/etf-flow/signals")
async def etf_flow_signals(kind: str = Query(default=""),
                           level: str = Query(default=""),
                           code: str = Query(default=""),
                           group: str = Query(default=""),
                           start: str = Query(default=""),
                           end: str = Query(default=""),
                           gated: bool | None = Query(default=None),
                           alerts_only: bool = Query(
                               default=True,
                               description="只看会真正告警的（未被门控 + 强/中等级）"),
                           limit: int = Query(default=200, ge=1, le=2000)) -> dict:
    """读**已落库**的 ETF 份额信号历史（新的在前）。

    默认 `alerts_only=true`：只返回会真正告警的信号。要看全部异动就传
    `alerts_only=false` —— 被环境门控降级的和被观察列表里的都会出现，
    但 `gated` 字段会如实标出哪些不构成仓位建议。
    """
    repo = _repo()
    if repo is None:
        return {"signals": [], "count": 0, "gaps": ["存储不可用"]}
    rows = await repo.load_etf_signals(
        kind=kind, level=level, code=code, group=group, start=start, end=end,
        gated=gated, alerts_only=alerts_only, limit=limit)
    return {"signals": rows, "count": len(rows),
            "latest_trade_date": await repo.latest_etf_trade_date(), "gaps": []}


@router.post("/etf-flow/save")
async def etf_flow_save(trade_date: str = Query(default="")) -> dict:
    """把当前快照的信号落库（幂等 upsert）。返回写入行数。"""
    repo = _repo()
    if repo is None:
        raise HTTPException(status_code=503, detail="存储不可用")
    payload = await asyncio.to_thread(_flow_snapshot_sync, trade_date)
    config = _flow_config()
    signals = _rebuild_flow_signals(payload, config)
    written = await repo.save_etf_signals(signals)
    return {"trade_date": payload.get("trade_date", ""), "written": written,
            "signal_count": len(signals)}


@router.get("/etf-flow/backtest")
async def etf_flow_backtest(run_id: str = Query(default=""),
                            with_markdown: bool = Query(default=False)) -> dict:
    """读一次 ETF 份额回测结果；`run_id` 留空取最近一次。"""
    repo = _repo()
    if repo is None:
        return {"run_id": "", "gaps": ["存储不可用"]}
    payload = await repo.load_etf_backtest(run_id)
    if not payload:
        return {"run_id": "", "gaps": ["还没有回测记录，请先 POST /etf-flow/backtest/run"]}
    if not with_markdown:
        payload.pop("markdown", None)
    return payload


@router.get("/etf-flow/backtest/list")
async def etf_flow_backtest_list(limit: int = Query(default=20, ge=1, le=100)
                                 ) -> dict:
    """ETF 份额回测历史摘要（新的在前）。"""
    repo = _repo()
    if repo is None:
        return {"runs": [], "gaps": ["存储不可用"]}
    return {"runs": await repo.list_etf_backtests(limit=limit)}


@router.post("/etf-flow/backtest/run")
async def etf_flow_backtest_run(body: EtfFlowBacktestRequest | None = None
                                ) -> dict:
    """后台跑一次 ETF 份额回测（分钟级，立即返回 task_id）。"""
    body = body or EtfFlowBacktestRequest()

    def job(progress: Callable[[str], None]) -> dict:
        report = run_flow_backtest(_store(), config=_flow_config(),
                                   start=body.start, end=body.end,
                                   progress=progress)
        if report.error:
            raise RuntimeError(report.error)
        repo = _repo()
        report.markdown = render_flow_report(report)
        if repo is not None:
            asyncio.run(repo.save_etf_backtest(report))
        return {"run_id": report.run_id, "signals": len(report.records),
                "range": [report.range_start, report.range_end],
                "seconds": report.seconds}

    task_id = _spawn("etf-flow-backtest", job, "ETF 份额回测已启动")
    return {"task_id": task_id, "state": "running"}


def _rebuild_flow_signals(payload: dict[str, Any], config: Any) -> list[Any]:
    """把快照 JSON 还原成 `FlowSignal` 对象（落库需要 `alert_id` 等属性）。

    刻意**不**把对象缓存在 `_CONTEXT` 里：快照有 TTL 缓存，缓存的
    `to_dict()` 与对象的生命周期一旦不一致，落库就会写进上一次的信号。
    重新构造虽然多一次解析，但语义明确且没有共享可变状态。

    ⚠️ 字段白名单必须覆盖 `FlowSignal.to_dict()` 的**全部**键。
    漏掉一个不会报错 —— 它会静默变成字段的默认值再被写回库里，
    在界面上表现为"这条信号没有理由"，而原因完全看不出来。
    这里曾漏掉 `reasons`，导致落库的理由全是空数组。
    """
    out: list[Any] = []
    for item in payload.get("signals", []) or []:
        if not isinstance(item, dict):
            continue
        out.append(FlowSignal(**{
            key: item.get(key) for key in (
                "date", "code", "name", "group", "group_label", "kind", "level",
                "index_code", "index_name", "index_percentile", "change_1d",
                "change_5d", "resonance_count", "resonance_total", "resonance",
                "regime", "gated", "reasons", "forward") if key in item}))
    return out


__all__ = ["router"]
