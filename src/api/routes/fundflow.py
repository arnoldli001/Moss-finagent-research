"""资金流监控 API。

接口一览（前缀 `/api/v1`）：
  GET    /fundflow/snapshot?refresh=&window=&top=   榜单 + 走势（盘中 60 秒缓存）
  GET    /fundflow/watch?kind=sector|stock         已选中的板块/个股
  POST   /fundflow/watch                           加入监控（幂等）
  DELETE /fundflow/watch/{kind}/{code}             移除监控
  GET    /fundflow/search?kind=&q=                 搜索板块（同花顺即时名）或个股（仓库名录）
  GET    /fundflow/pick?kind=&top=&window=         选股对接：给出「先板块后个股」的候选

设计取舍：
- **榜单与走势同一个响应**：前端要在一个页面里同时画榜与线，拆两个接口会多一次往返，
  而它们必然来自同一份取数（同一时刻的资金流）。
- **`refresh=true` 才会忽略 60 秒缓存**：盘中自动刷新走缓存（服务端自己也在重算），
  手动「刷新」按钮才强制穿透 —— 与做T面板的 `force` 语义保持一致。
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request

from src.core.errors import (
    BRIEF_DEFAULT,
    BRIEF_TIGHT,
    brief,
)
from src.fundflow import sector_filter
from src.fundflow.service import FundFlowService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["fundflow"])

Kind = Literal["sector", "stock"]


def _service(request: Request) -> FundFlowService:
    service = getattr(request.app.state.runtime, "fundflow", None)
    if service is None:
        raise HTTPException(
            status_code=503, detail="资金流监控模块未装配（fundflow service unavailable）")
    return service


def _validate_kind(kind: str) -> Kind:
    if kind not in ("sector", "stock"):
        raise HTTPException(status_code=400, detail="kind 只能是 sector 或 stock")
    return kind  # type: ignore[return-value]


def _validate_code(kind: str, code: str) -> str:
    value = (code or "").strip()
    if not value:
        raise HTTPException(status_code=400, detail="缺少 code/板块名")
    if kind == "stock" and not (value.isdigit() and len(value) == 6):
        raise HTTPException(status_code=400, detail="个股代码应为 6 位数字")
    return value


@router.get("/fundflow/snapshot")
async def snapshot(
    request: Request,
    refresh: Annotated[bool, Query(description="true=忽略 60 秒缓存强制重算")] = False,
    window: Annotated[int, Query(ge=5, le=30, description="窗口交易日数")] = 10,
    top: Annotated[int, Query(ge=5, le=50, description="榜单条数")] = 20,
) -> dict:
    """资金流监控完整快照：板块榜 + 个股榜 + 已选实体的近 N 日走势。"""
    service = _service(request)
    try:
        board = await service.snapshot(force=refresh, window_days=window, top=top)
    except Exception as exc:  # noqa: BLE001
        logger.warning("资金流快照失败: %s", brief(exc, BRIEF_DEFAULT))
        raise HTTPException(
            status_code=502, detail=f"资金流取数失败：{brief(exc, BRIEF_DEFAULT)}") from exc
    payload = board.to_dict()
    payload["notice"] = board.refresh_hint
    return payload


@router.get("/fundflow/watch")
async def watch(
    request: Request,
    kind: Annotated[Kind, Query(description="sector=板块 / stock=个股")] = "sector",
) -> dict:
    """当前监控列表（首次使用时 sector 会被播种默认热门前 20）。"""
    service = _service(request)
    if not service.repo:
        raise HTTPException(status_code=503, detail="资金流监控仓储未装配（选择无法持久化）")
    try:
        await service.ensure_defaults()
        items = await service.watchlist(kind)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=500, detail=f"读取监控列表失败：{brief(exc, BRIEF_DEFAULT)}") from exc
    return {"kind": kind, "count": len(items),
            "items": [item.to_dict() for item in items],
            "source": "db:sqlite.dim_fund_flow_watch"}


@router.post("/fundflow/watch")
async def add_watch(request: Request, body: dict[str, Any]) -> dict:
    """加入监控（幂等；板块用官方名，个股用 6 位代码）。"""
    service = _service(request)
    kind = _validate_kind(str(body.get("kind") or "sector"))
    code = _validate_code(kind, str(body.get("code") or ""))
    name = str(body.get("name") or "").strip()
    try:
        items = await service.add(kind, code, name)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=400, detail=f"加入监控失败：{brief(exc, BRIEF_DEFAULT)}") from exc
    return {"ok": True, "kind": kind, "code": code,
            "count": len(items), "items": [item.to_dict() for item in items],
            "notice": f"已加入资金流监控：{name or code}"}


@router.delete("/fundflow/watch/{kind}/{code}")
async def remove_watch(kind: str, code: str, request: Request) -> dict:
    """移除监控（幂等）。"""
    service = _service(request)
    target_kind = _validate_kind(kind)
    target = _validate_code(target_kind, code)
    try:
        removed, items = await service.remove(target_kind, target)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=400, detail=f"移除监控失败：{brief(exc, BRIEF_DEFAULT)}") from exc
    return {"ok": True, "kind": target_kind, "code": target, "removed": removed,
            "count": len(items), "items": [item.to_dict() for item in items],
            "notice": (f"已移除 {target}" if removed else f"{target} 本来就不在监控列表里")}


@router.get("/fundflow/search")
async def search(
    request: Request,
    kind: Annotated[Kind, Query()] = "sector",
    q: Annotated[str, Query(description="板块名关键字；个股可留空返回榜单前若干")] = "",
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> dict:
    """搜索可加入监控的对象。

    - `kind=sector`：在**同花顺即时资金流的板块名单**里做包含匹配
      （名单就是数据源的官方名，加进去必然能取到数）；
    - `kind=stock`：走仓库股票名录（代码/名称/拼音），与做T自选同一个目录。
    """
    service = _service(request)
    target_kind = _validate_kind(kind)
    keyword = (q or "").strip()
    if target_kind == "sector":
        # 名单取**取数口径本身**（Tushare moneyflow_ind_dc 的板块名）：
        # 用户加入的名字必须能被取到数，拿别的名单（同花顺概念名）会出现
        # "加进去了但走势永远空白"。
        snapshot = await service.provider.sector_snapshot()
        # 剔除清单里的板块**不提供搜索**。否则用户可以把它手工加回来，
        # 而加回来之后榜单/走势依然不显示它（`_build` 会再滤一次），
        # 表现为"加入了却什么都没有" —— 比直接搜不到更让人困惑。
        names, excluded = sector_filter.filter_names(list(snapshot.keys()))
        if keyword:
            hits = [name for name in names if keyword in name]
        else:
            hits = sorted(names, key=lambda name: -abs(
                float((snapshot.get(name) or {}).get("net") or 0.0)))
        return {"kind": "sector", "query": keyword, "total": len(names),
                "excluded": excluded,
                "items": [{"code": name, "name": name,
                           "net_yi": ((snapshot.get(name) or {}).get("net") or 0) / 1e8,
                           "change_pct": (snapshot.get(name) or {}).get("pct_change"),
                           "companies": (snapshot.get(name) or {}).get("company_num")}
                          for name in hits[:limit]]}
    directory = service._directory()  # noqa: SLF001 名录是可选增强，取不到就走榜单
    items: list[dict[str, Any]] = []
    if directory is not None and keyword:
        try:
            found = directory.search(keyword, limit=limit,
                                     types=("股票", "指数", "ETF"))
            items = [{"code": entry.code, "name": entry.name,
                      "type": getattr(entry, "instrument_type", "")}
                     for entry in found]
        except Exception as exc:  # noqa: BLE001
            logger.info("个股搜索失败：%s", brief(exc, BRIEF_TIGHT))
    if not items:
        board = await service.snapshot()
        items = [{"code": item.code, "name": item.name,
                  "net_avg": item.net_avg, "net_to_mv": item.net_to_mv}
                 for item in board.stock_rank[:limit]]
    return {"kind": "stock", "query": keyword, "items": items}


@router.get("/fundflow/pick")
async def pick(
    request: Request,
    window: Annotated[int, Query(ge=5, le=30)] = 10,
    top: Annotated[int, Query(ge=5, le=50)] = 20,
) -> dict:
    """选股对接：先板块后个股。

    输出两份机器可读的候选清单（供「选股功能模块」直接消费）：
      - `sectors`：净额均值居前/居后的板块（含当日盘中净额与涨跌幅）；
      - `stocks`：净额均值/流通市值 居前的个股（含所属板块信息位，取不到就留空）。
    刻意**不做二次打分**：这里只给"钱在往哪去"的原始排序，
    选股策略怎么用（阈值、权重）属于选股模块的口径，不在这里替它决定。
    """
    service = _service(request)
    board = await service.snapshot(window_days=window, top=top)
    return {
        "generated_at": board.generated_at,
        "trade_date": board.trade_date,
        "window_days": board.window_days,
        "sectors": [{"code": item.code, "name": item.name,
                     "net_avg": item.net_avg, "today_net": item.today_net,
                     "change_pct": item.change_pct} for item in board.sector_rank],
        "stocks": [{"code": item.code, "name": item.name,
                    "net_avg": item.net_avg, "net_to_mv": item.net_to_mv,
                    "circ_mv": item.circ_mv} for item in board.stock_rank],
        "gaps": list(board.gaps),
        "notice": "该清单只给资金流原始排序；阈值/权重由选股模块决定",
    }
