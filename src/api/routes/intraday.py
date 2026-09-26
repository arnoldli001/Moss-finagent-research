"""做T辅助API：快照 / 自选列表 / 配置口径 / 阈值回测 / 信号扫描 / 实时WebSocket。

接口一览：
  GET  /api/v1/intraday/snapshot?code=300308       单个标的全量快照（四面板数据）
  GET  /api/v1/intraday/watchlist                  自选标的概览
  GET  /api/v1/intraday/config                     当前打分口径（权重/阈值/板块/推送通道）
  POST /api/v1/intraday/backtest                   ±20/±30阈值回测
  POST /api/v1/intraday/scan                       自选扫描（返回触发信号的标的并推送）
  WS   /api/v1/ws/intraday?code=300308             按周期推送最新快照（默认15秒）

设计取舍：快照由服务层单次组装，REST 与 WS 共用同一份 payload，
避免前端为四个面板分别发请求导致档位/分数不同步。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from src.core.errors import (
    BRIEF_DEFAULT,
    BRIEF_TIGHT,
    brief,
)
from src.intraday.config import FACTOR_LABELS
from src.intraday.service import IntradayService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["intraday"])

# WebSocket 推送节奏：完整快照（分时图/打分/板块，单次 ~0.4s）按 15 秒推；
# 循环本身每秒醒一次，这样"报价快车道"的新价（5 秒一轮）能及时推出去 ——
# 若把循环整体设成 15 秒，快车道做出 5 秒的数据也会被卡在 15 秒才到屏幕上。
_WS_INTERVAL_SECONDS = 15.0
_WS_TICK_SECONDS = 1.0


def _service(request: Request) -> IntradayService:
    service = getattr(request.app.state.runtime, "intraday", None)
    if service is None:
        raise HTTPException(
            status_code=503, detail="做T辅助模块未装配（intraday service unavailable）")
    return service


def _watchlist_payload(items: list[Any], status: dict[str, Any]) -> dict:
    """自选概览的统一 payload（REST 与 WS 共用一份口径，避免两边字段漂移）。

    `auto_refresh` 是「为什么现在在/不在自动刷新」的完整状态：前端直接显示
    `⟳ 每分钟自动刷新 · 10:31:02` 或 `⏸ 午间休市`，不需要自己再算一遍交易时段。
    """
    return {
        "items": [item.model_dump() for item in items],
        "count": len(items),
        "auto_refresh": status,
    }


def _validate_code(code: str) -> str:
    """校验6位沪深代码（与 WatchConfig / exchange_symbol 口径一致）。"""
    value = (code or "").strip()
    if not (value.isdigit() and len(value) == 6):
        raise HTTPException(status_code=400, detail="证券代码应为6位数字")
    if value.startswith(("8", "4", "920")):
        raise HTTPException(
            status_code=400,
            detail=f"北交所标的暂不支持（数据源未覆盖）: {value}")
    return value


@router.get("/intraday/snapshot")
async def snapshot(
    request: Request,
    code: str = Query(default="300308", description="6位证券代码"),
    refresh: bool = Query(default=False, description="强制刷新（忽略缓存TTL）"),
    light: bool = Query(
        default=False,
        description=("轻量快照：只要行情/分时/关键价位/总分/信号，"
                     "跳过消息面LLM、估值、板块分时、大盘、海外映射。"
                     "用于「先出图，再补全」的两段式首屏")),
) -> dict:
    """做T辅助完整快照：估值空间/分时与提示点/多因子打分/消息面情绪。

    `light=true` 时返回的子集仍是一个完整 `IntradaySnapshot`（被跳过的字段为
    null/空数组），前端可以直接渲染分时图与分数，等完整版到达再补齐其余面板。
    实测冷标的一次完整快照 4.5~7 秒，而用户点开一只票**第一眼要看的就是分时图**。
    """
    service = _service(request)
    target = _validate_code(code)
    try:
        result = await service.snapshot(target, force_refresh=refresh, light=light)
    except Exception as exc:  # noqa: BLE001 取数失败转502而非500
        logger.warning("做T快照失败(%s): %s", target, brief(exc, BRIEF_DEFAULT))
        raise HTTPException(
            status_code=502, detail=f"做T快照组装失败：{brief(exc, BRIEF_DEFAULT)}") from exc
    return result.model_dump()


@router.get("/intraday/quotes")
async def quotes(
    request: Request,
    codes: str = Query(default="", description="逗号分隔的 6 位代码，一次取一批"),
) -> dict:
    """**批量**实时快照（只读；给自定义板块的成分行显示涨跌幅用）。

    为什么单开一个：自选池的行情走 `/intraday/watchlist`，那个**只覆盖自选**；
    而自定义板块的成分里没进自选的票，前端手上一点价格都没有（成分表本身不带行情）。

    口径与代价：走 `IntradayDataProvider.fetch_quotes()` —— **一次请求取一批**
    （实测 9 只约 54ms），不是逐只循环。前端再按 `code` 缓存，于是"同一只票
    出现在多个自定义板块里"只抓一次（用户口径 2026-09-23 明确点到这一点）。

    ⚠️ 坏代码**逐条跳过**而不是整批 400：一行脏数据不该让整列变成"—"
    （实测踩过：板块里存了 `00兆易创新` 这种把文字当代码的行，会让快照接口 400）。
    取不到的票**不出现**在结果里（前端保留上次的值）。
    """
    service = _service(request)
    wanted: list[str] = []
    for raw in (codes or "").split(","):
        text = raw.strip()
        if not text:
            continue
        try:
            wanted.append(_validate_code(text))
        except HTTPException:
            logger.info("批量快照跳过非法代码：%s", text)
    if not wanted:
        return {"items": [], "count": 0}
    try:
        found = await service.data_provider.fetch_quotes(wanted)
    except Exception as exc:  # noqa: BLE001 行情是展示增强项，失败不该让整页报错
        logger.warning("批量快照失败：%s", brief(exc, BRIEF_TIGHT))
        return {"items": [], "count": 0}
    items = []
    for code in wanted:
        quote = found.get(code)
        if quote is None:
            continue
        items.append({
            "code": code,
            "name": getattr(quote, "name", "") or "",
            "price": getattr(quote, "price", None),
            "change_pct": getattr(quote, "change_pct", None),
        })
    return {"items": items, "count": len(items)}


@router.post("/intraday/auto-select")
async def auto_select(
    request: Request,
    apply: bool = Query(default=False,
                        description="true=把选出的票真正加入自选（会写 configs/intraday.yaml）"),
    top_n: int = Query(default=6, ge=1, le=20,
                       description="最多选几只（用户口径：6）"),
    min_intraday: float = Query(default=40.0, ge=0, le=100,
                                description="分时多指标合成打分门槛（用户口径：>40）"),
    prescreen: int = Query(default=30, ge=5, le=120,
                           description="粗筛后进入精算的票数上限（控制单轮耗时）"),
) -> dict:
    """**自动选股**：日K多方条件 + 分时打分 > 阈值 → 按综合分排序取前 N。

    候选池 = 热门股前 50 ∪ 昨日涨停 ∪ 昨日成交额前 200（并集去重）。
    只在交易日的 9:25–9:40 与 14:45–15:00 由后台定时触发（每分钟一次）；
    本接口用 `apply=true` 可手动触发并直接写入自选。

    为什么分两段打分：250 只票各跑一次完整评分要 8~16 分钟，
    而要求是 1 分钟一轮 —— 所以先按分时粗筛取前 `prescreen` 只，再精算。
    """
    from src.intraday.auto_select import WatchlistAutoSelector

    service = _service(request)
    selector = WatchlistAutoSelector(service, top_n=top_n,
                                     min_intraday_score=min_intraday,
                                     prescreen_size=prescreen)
    result = await selector.run(window="manual")
    added: list[str] = []
    if apply and result.selected:
        added = await _add_selected_to_watchlist(service, result)
    return {"ok": True, "applied": bool(apply), "added": added,
            **result.as_dict()}


async def _add_selected_to_watchlist(service: Any, result: Any) -> list[str]:
    """把入选标的写入自选池（幂等：已在自选里的跳过）。

    复用 `service.add_watch`（同步方法，内部走 Moss 既有写盘逻辑：
    原子替换 + 校验 + 保留注释），**不自己拼 YAML**。
    """
    import asyncio

    added: list[str] = []
    existing = {item.code for item in await service.watchlist()}
    for item in result.selected:
        if item.code in existing:
            result.skipped.append(item.code)
            continue
        try:
            await asyncio.to_thread(service.add_watch, item.code, name=item.name)
            item.added = True
            added.append(item.code)
        except Exception as exc:  # noqa: BLE001 单只失败不影响其余
            result.notes.append(f"加入自选失败 {item.code}：{brief(exc, BRIEF_TIGHT)}")
    return added


@router.get("/intraday/watchlist")
async def watchlist(
    request: Request, limit: int = Query(default=20, ge=1, le=200),
    force: bool = Query(
        default=False,
        description="true=绕过缓存立即重算（前端「刷新」按钮）；默认取自动刷新循环写入的缓存"),
    active: str = Query(
        default="",
        description="前端当前正在查看的标的代码：force=true 时**只重算这一只**，"
                    "其余走缓存并在后台补齐（自选 30+ 只时避免整表重算的几秒等待）"),
) -> dict:
    """自选标的概览（轻量快照并发，含当前总分与信号）。

    盘中由 `IntradayService` 的自动刷新循环每分钟重算一次，这里默认直接返回缓存，
    前端每分钟取一次即"自选股自动刷新"，不需要用户手动点。

    `limit` 上限从 50 提到 200：自选池很容易超过 50 只（实测写入后已达 39 只，
    多选几次就破 50），而前端漏传 `limit` 时只会拿到默认 20 只 ——
    表现就是"加了自选但列表里没有"（实测踩过）。上限放宽后前端可以一次取全。

    `active` 是「强制刷新不卡」的关键：见 `IntradayService.watchlist` 的说明。
    """
    service = _service(request)
    items = await service.watchlist(limit=limit, force=force,
                                    active=active or None)
    return _watchlist_payload(items, service.watchlist_refresh_status())


class WatchRequest(BaseModel):
    """新增/更新自选标的入参。"""

    code: str = Field(description="6位证券代码，如 300308")
    name: str = Field(default="", description="证券简称（留空则自动向行情源取一次）")
    boards: list[str] = Field(
        default_factory=list,
        description="关联板块名（须与 configs/intraday.yaml 的 boards 一致；留空用默认板块）")
    peers: list[str] = Field(
        default_factory=list, description="同业股票池（PE/PB对比 + 板块分时合成用）")
    industry: str = Field(default="", description="巨潮行业分类名（行业中位数对标用）")
    overseas: list[str] = Field(
        default_factory=list,
        description="海外映射代码（us*/kr*，如 usNVDA、kr000660）；"
                    "留空则回落到所属板块的默认映射")
    fetch_name: bool = Field(
        default=True, description="name留空时是否自动向行情源取证券简称")


@router.get("/intraday/stock-bindings")
async def stock_bindings(request: Request) -> dict:
    """**全部**已保存的「关联板块 / 海外映射」绑定（用户口径 2026-09-23）。

    用途：前端在关联板块/海外映射输入框上做**联想下拉**，提示"你以前给哪些票
    配过什么"。数据来自服务进程内的热map（启动时 `warm_bindings()` 灌好），
    所以这个接口**不打数据库**。

    返回 `{"items": [{code, name, boards, overseas}], "count": n}`；
    `name` 取自当前自选池（配过的票可能已不在自选里，那时 name 为空串）。
    """
    service = _service(request)
    try:
        rows = service.all_bindings()
    except Exception as exc:  # noqa: BLE001 绑定是增强项，读不到就给空的
        logger.info("读取个股绑定失败：%s", brief(exc, BRIEF_TIGHT))
        rows = {}
    names = {}
    try:
        names = {item.code: item.name for item in service.config.watchlist}
    except Exception:  # noqa: BLE001 名字只是展示，取不到不影响主信息
        names = {}
    items = [
        {"code": code, "name": names.get(code, ""),
         "boards": boards, "overseas": overseas}
        for code, (boards, overseas) in sorted(rows.items())
    ]
    return {"items": items, "count": len(items)}


@router.get("/intraday/stock-bindings/{code}")
async def stock_binding(code: str, request: Request) -> dict:
    """某只票已保存的绑定 —— 前端**切换/加入自选时预填输入框**用。

    ⚠️ 与 `POST /intraday/watchlist` 的分工：那个接口的 `boards`/`overseas`
    为空时后端也会自动回落到这里存的值（所以**不预填也不会丢配置**）；
    这个接口只是让用户**看得见**已存了什么、还能直接改。
    """
    service = _service(request)
    target = _validate_code(code)
    boards, overseas = service.binding_for(target)
    return {
        "ok": True, "code": target,
        "boards": boards, "overseas": overseas,
        # 空 = 这只票还没配过（前端据此决定要不要覆盖输入框）
        "saved": bool(boards or overseas),
    }


@router.post("/intraday/watchlist")
async def add_watch(body: WatchRequest, request: Request) -> dict:
    """新增自选标的（写回 configs/intraday.yaml，保留文件注释，进程内即时生效）。"""
    service = _service(request)
    code = _validate_code(body.code)
    name = body.name.strip()
    if not name and body.fetch_name:
        try:
            quote, _, _ = await service.data_provider.fetch_quote(code)
            name = quote.name or ""
        except Exception as exc:  # noqa: BLE001 取名失败不阻断加自选
            logger.info("加自选时取证券简称失败(%s): %s", code, brief(exc, BRIEF_TIGHT))
    peers = [_validate_code(peer) for peer in body.peers]
    try:
        config = service.add_watch(
            code, name=name, boards=body.boards, peers=peers,
            industry=body.industry.strip(), overseas=body.overseas)
    except Exception as exc:  # noqa: BLE001 磁盘写入/配置校验失败转400
        raise HTTPException(
            status_code=400, detail=f"写入自选失败：{brief(exc, BRIEF_DEFAULT)}") from exc
    return {
        "ok": True, "code": code, "name": name,
        # 回显绑定结果：板块绑定决定「板块情绪/板块排行」两个维度能否计入总分
        "boards": config.board_names(code),
        "boards_bound": config.boards_bound(code),
        "overseas": config.overseas_for(code),
        "watchlist": [item.model_dump() for item in config.watchlist],
    }


@router.delete("/intraday/watchlist/{code}")
async def remove_watch(code: str, request: Request) -> dict:
    """移除自选标的（写回配置文件；不存在时幂等成功）。"""
    service = _service(request)
    target = _validate_code(code)
    try:
        config = service.remove_watch(target)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=400, detail=f"移除自选失败：{brief(exc, BRIEF_DEFAULT)}") from exc
    return {
        "ok": True, "code": target,
        "watchlist": [item.model_dump() for item in config.watchlist],
    }


@router.post("/intraday/watchlist/pin")
async def pin_watch(
    request: Request,
    code: str = Query(description="要置顶/取消置顶的 6 位代码"),
    pinned: bool = Query(default=True, description="true=置顶，false=取消置顶"),
) -> dict:
    """置顶 / 取消置顶一只自选（幂等）。

    置顶状态写在 `configs/intraday.yaml` 的 `pinned: true` 上（不是前端 localStorage），
    因此换浏览器/换机器一致，服务端刷新循环产出的顺序也一致。
    置顶项在列表里**永远排最前**，其余保持配置顺序（不按分数自动排 —— 那会让列表
    每分钟自己跳动，想点的票在手指落下时换位置）。
    """
    from src.core.exceptions import ConfigError

    service = _service(request)
    target = _validate_code(code)
    try:
        config = service.set_watch_pinned(target, pinned)
    except ConfigError as exc:
        raise HTTPException(status_code=404, detail=brief(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=400, detail=f"置顶失败：{brief(exc, BRIEF_DEFAULT)}") from exc
    return {
        "ok": True, "code": target, "pinned": bool(pinned),
        "watchlist": [item.model_dump() for item in config.watchlist],
    }


@router.get("/intraday/daily")
async def daily(
    request: Request,
    code: str = Query(default="300308", description="6位证券代码"),
    refresh: bool = Query(
        default=False,
        description="true=绕过进程内缓存立即重算（面板首屏/手动刷新用）"),
) -> dict:
    """日K级别做T：量柱/高量柱攻防/量价16形态/B1-B15买入/S1-S6卖出风控。

    与日内分时模式互补：分时解决「今天在哪一档动手」，日K解决
    「这只票现在处于主力四阶段的哪一步、该用哪套战法」。
    """
    service = _service(request)
    target = _validate_code(code)
    snapshot = await service.daily(target, refresh=refresh)
    return snapshot.model_dump()


@router.get("/intraday/config")
async def module_config(request: Request) -> dict:
    """当前打分口径（前端展示权重表与阈值，保证与后端计算完全一致）。"""
    service = _service(request)
    config = service.config
    weights = config.weights.as_dict()
    return {
        "weights": [
            {"key": key, "label": FACTOR_LABELS.get(key, key), "weight": weights[key]}
            for key in FACTOR_LABELS
        ],
        "weights_sum": sum(weights.values()),
        "load_error": config.load_error,
        "thresholds": {
            "action": config.thresholds.action,
            "hint": config.thresholds.hint,
        },
        "levels": config.levels.model_dump(),
        "data": config.data.model_dump(),
        "session": config.session.model_dump(),
        "boards": [board.model_dump() for board in config.boards],
        # 做T链路唯一会调模型的地方：消息面情绪（默认走本地 Ollama，零 API token）。
        # 暴露调用统计，才能验证"新闻没变就不重跑"到底省了多少。
        "news_llm": service.news_llm_stats,
        # 个股微调：前端可展示"这只票改了哪些参数"，避免两套口径混着看
        "overrides": {
            code: {"weights": item.weights, "thresholds": item.thresholds,
                   "levels": item.levels, "describe": item.describe()}
            for code, item in config.overrides.items()
        },
        "watchlist": [item.model_dump() for item in config.watchlist],
        "notifier": service.notifier.channel_status(),
        "snapshot": config.snapshot(),
        "disclaimer": config.disclaimer,
    }


class BacktestRequest(BaseModel):
    """阈值回测入参。"""

    code: str = Field(default="300308", description="6位证券代码")
    horizon: int = Field(
        default=6, ge=1, le=48,
        description="前瞻bar数（6根5分钟≈30分钟，做T的典型持有期）")
    days: int = Field(default=20, ge=1, le=120, description="请求的历史交易日数")


@router.post("/intraday/backtest")
async def backtest(body: BacktestRequest, request: Request) -> dict:
    """±20/±30 阈值回测：命中率/平均前瞻收益/机械做T收益（无未来函数）。"""
    service = _service(request)
    target = _validate_code(body.code)
    result = await service.backtest(
        target, horizon=body.horizon, days=body.days)
    return result.model_dump()


@router.post("/intraday/scan")
async def scan(request: Request) -> dict:
    """扫描自选标的，返回触发信号的标的（正式信号会自动推送）。

    `force=True`：这是**手动触发**的扫描，调用方要的就是"现在这一刻"的结果，
    返回自动刷新循环写入的缓存（最长 60 秒前）会与动作语义不符。
    """
    service = _service(request)
    items = await service.watchlist(force=True)
    triggered = [
        item.model_dump() for item in items
        if item.signal_strength in ("solid", "forced_exit")
    ]
    return {
        "scanned": len(items),
        "triggered": len(triggered),
        "items": triggered,
        "all": [item.model_dump() for item in items],
    }


@router.get("/intraday/market-turnover")
async def market_turnover(request: Request, refresh: bool = False) -> dict:
    """大盘量能（**独立小接口**，前端可单独轮询，不依赖个股快照）。

    详见 `IntradayService.market_turnover_now` 的说明：原来量能只搭在个股快照里，
    冷启动/数据源熔断时会整段消失；这个端点让前端能把它**单独实时读**出来，
    取不到时如实返回 `available=False` + 原因，而不是静默空白。
    """
    service = _service(request)
    return await service.market_turnover_now(refresh=refresh)


@router.websocket("/ws/intraday")
async def intraday_ws(websocket: WebSocket,
                      code: str = "300308") -> None:
    """按周期推送最新快照（服务端主动推，前端无需轮询）。"""
    # ★ 登录门槛：WebSocket **不经过** `LoginGateMiddleware`
    #   （那是 BaseHTTPMiddleware，只管 http scope）。实测公网匿名连上
    #   `/ws/intraday` 就能收到整套快照，所以这里单独校验会话。
    from src.api.session_ctx import ws_allow

    if not await ws_allow(websocket):
        return
    service = getattr(websocket.app.state.runtime, "intraday", None)
    if service is None:
        await websocket.close(code=1013)  # 模块不可用
        return
    target = (code or "").strip()
    if not (target.isdigit() and len(target) == 6):
        await websocket.close(code=1008)  # 参数非法
        return
    await websocket.accept()
    # 指纹 = (打分版号, 报价版号)：任一变化就推一版自选概览。
    # 用版号而不是"每轮都推"，避免同一份数据被每 15 秒重发一遍。
    pushed: tuple[Any, Any] = (-1, -1)
    last_snapshot = 0.0
    # ── 两段式首帧：先轻量出图，再完整补面板 ──────────────────────────────
    # 用户实测「点开一只票的分时图要六七秒」。原因不在图，而在**完整快照**里
    # 那几段与图无关的重活：消息面 LLM 打分、估值空间、板块分时、大盘指数、
    # 海外映射（实测冷标的完整快照 4.5~7 秒，其中板块分时单次可到 20 秒）。
    # 轻量快照只算行情 + 分时 + 关键价位 + 总分 + 信号，这些正是首屏要看的，
    # 连上后**第一帧就发轻量**，完整版在下一拍（1 秒后）补上 —— 图先出来，
    # 消息面/估值面板随后自己填满，而不是让用户盯着整个面板转圈。
    light_pending = True
    full_pending = True
    try:
        while True:
            now = time.monotonic()
            due = now - last_snapshot >= _WS_INTERVAL_SECONDS
            if light_pending or full_pending or due:
                use_light = light_pending
                light_pending = False
                if not use_light:
                    full_pending = False
                    last_snapshot = now
                try:
                    snapshot = await service.snapshot(target, light=use_light)
                    await websocket.send_json({
                        "type": "snapshot", "light": use_light,
                        "data": snapshot.model_dump()})
                except Exception as exc:  # noqa: BLE001 单次失败不断开连接
                    await websocket.send_json({
                        "type": "error", "detail": brief(exc, BRIEF_DEFAULT)})
            try:
                status = service.watchlist_refresh_status()
                fingerprint = (status.get("generation"),
                               status.get("quote_generation"))
                if fingerprint != pushed:
                    items = await service.watchlist()
                    await websocket.send_json({
                        "type": "watchlist",
                        "data": _watchlist_payload(items, status)})
                    pushed = fingerprint
            except Exception:  # noqa: BLE001 自选列表失败不影响主快照推送
                pass
            await asyncio.sleep(_WS_TICK_SECONDS)
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 任何WS异常只做清理
        pass
