"""行业轮动与资金流向监控：REST 接口。

路由前缀 `/api/v1/sector_rotation`（项目所有路由自带 /api/v1，见
`sector_crowding.py` 头部注释的同款说明）。

报告"一个交易日一份"：收盘后由调度作业（`sector_rotation_report`，
工作日 15:40）自动生成落盘；接口侧的策略是**读盘优先** ——

  - 请求的报告已落盘 → 直接返回文件（毫秒级）；
  - 落伍（关机错过调度/服务刚重启）且**已有旧报告** → ★ 2026-09-27 起
    **不再在请求里同步等 20~40 秒**：立刻返回旧报告 + 起后台任务补生成，
    完成后由页面横幅里的轻量轮询自动刷新（与情报中心 `/intel/feed`
    "请求永不等待"同一契约）；
  - 一份报告都没有（首跑）→ 只能同步生成一次（此时没有旧的可回）；
  - 盘中重复请求 → 距上次生成不足 `REGEN_TTL_SECONDS`（15 分钟）时
    仍返回落盘份，不为每个刷新重付一轮网络取数（生成失败同样进入冷却，
    防止"每个请求都重试一次必然失败的取数"）。

另：`/echarts.min.js` 提供**本地 vendored** 的 ECharts（约 1MB）。
原模板从 jsdelivr CDN 拉取，在 ≈51KB/s 的对外隧道上仅此一项 ≈20 秒，
且大陆可用性不稳定 —— 已改为本地文件（缺失时模板回退 CDN 兜底）；
历史上已落盘的旧报告在**读取时**做字符串替换切到本地地址。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse

from src.core.errors import BRIEF_DEFAULT, brief
from src.sector_rotation import report as report_mod
from src.sector_rotation import service, store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/sector_rotation", tags=["sector-rotation"])

#: 进程内记录最后一次生成尝试时间（盘中 TTL / 失败冷却用；落盘跨进程共享）
_LAST_GEN_AT = 0.0

#: 后台生成状态（进程内；`/build_status` 只读这几个字段，零 I/O）
_BUILDING: dict[str, object] = {
    "running": False,      # 是否有后台生成在跑
    "seq": 0,              # 每次完成 +1（前端比较它判断"是否建完"）
    "finished_at": "",     # 最近一次完成时刻（ISO）
    "error": "",           # 最近一次失败摘要（空 = 成功/从未失败）
}
_BUILD_TASK: asyncio.Task | None = None  # 持引用防 GC

#: 旧报告里的 CDN 地址（读取时替换为本地同前缀相对路径）
_CDN_ECHARTS = "https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"


async def _build_in_background() -> None:
    """后台补生成一份最新报告（成败都进 15 分钟冷却，防失败重试风暴）。"""
    global _LAST_GEN_AT  # noqa: PLW0603
    try:
        await service.generate(force=False)
        _BUILDING["error"] = ""
    except Exception as exc:  # noqa: BLE001 后台任务失败只记状态，不抛
        _BUILDING["error"] = brief(exc, BRIEF_DEFAULT)
        logger.warning("行业轮动后台生成失败（15分钟内不再重试）: %s",
                       _BUILDING["error"])
    finally:
        _LAST_GEN_AT = time.monotonic()
        _BUILDING["running"] = False
        _BUILDING["seq"] = int(_BUILDING["seq"]) + 1  # type: ignore[arg-type]
        _BUILDING["finished_at"] = datetime.now().isoformat(timespec="seconds")


def _kick_background_build() -> bool:
    """发起后台生成（去重：已在跑 → False）。

    为什么不需要锁：单线程 asyncio 下"检查 running → 置位 → create_task"
    之间**没有 await 点**，本身就是原子的；加 `asyncio.Lock` 反而引入
    "模块级锁绑定到第一个事件循环"的问题（pytest 每个用例一个新 loop，
    跨 loop 使用会 RuntimeError）。
    """
    global _BUILD_TASK  # noqa: PLW0603
    if _BUILDING["running"]:
        return False
    _BUILDING["running"] = True
    _BUILD_TASK = asyncio.create_task(_build_in_background())
    return True


async def _latest_report(*, allow_generate: bool = True) -> dict:
    """最近一份报告：读盘优先；落伍时**后台补生成 + 立刻回旧报告**。

    只有"一份报告都没有"的首跑才同步生成（此时没有旧的可以回）。
    落伍且已标记 `meta.background_refreshing` 的返回值供 `/report.html`
    注入"正在后台更新"横幅（页面会轮询 `/build_status` 自动刷新）。
    """
    global _LAST_GEN_AT  # noqa: PLW0603
    cached_date = store.latest_date()
    if cached_date and not service.is_stale():
        cached = store.load(cached_date)
        if cached:
            return cached
    if not allow_generate:
        if cached_date:
            cached = store.load(cached_date)
            if cached:
                return cached
        raise HTTPException(status_code=404, detail="还没有任何已生成的报告")

    def _cached_or_none() -> dict | None:
        return store.load(cached_date) if cached_date else None

    now = time.monotonic()
    in_cooldown = bool(cached_date) and (now - _LAST_GEN_AT) < service.REGEN_TTL_SECONDS
    if in_cooldown or bool(_BUILDING["running"]):
        cached = _cached_or_none()
        if cached is not None:
            return cached
        if in_cooldown:
            raise HTTPException(
                status_code=503,
                detail="报告生成处于冷却期（上次尝试失败或刚完成），请稍后刷新")
        # running 但无落盘：首跑任务已在跑，等它不如同步再来一份？
        # —— 不：并发两轮取数只会互相拖慢，这里直接 503 让前端稍后重试。
        raise HTTPException(status_code=503,
                            detail="首次报告正在后台生成，请稍后刷新")

    if cached_date:
        # ★ 有旧报告：后台补生成，立刻回旧的（带标记，供 HTML 注入横幅）
        _kick_background_build()
        cached = _cached_or_none()
        if cached is not None:
            payload = dict(cached)
            meta = dict(payload.get("meta") or {})
            meta["background_refreshing"] = True
            payload["meta"] = meta
            return payload

    # 一份都没有 → 同步生成（首跑，无可回退）
    payload = await service.generate(force=False)
    _LAST_GEN_AT = time.monotonic()
    return payload


def _localize_echarts(html: str) -> str:
    """旧落盘报告的 CDN 地址 → 本地同前缀地址（本地文件存在才替换）。"""
    if not report_mod.echarts_src().startswith("http"):
        return html.replace(_CDN_ECHARTS, "echarts.min.js")
    return html


def _inject_refresh_banner(html: str, *, seq0: int) -> str:
    """落伍报告注入"后台更新中"横幅 + 完成后自动刷新的轻量轮询。"""
    banner = (
        '<div id="__stale_banner" style="position:sticky;top:0;z-index:99;'
        'background:#fdf3e7;color:#b45309;border-bottom:1px solid #f5ddba;'
        'padding:8px 16px;font-size:13px;text-align:center;">'
        "正在后台生成最新交易日报告（约 20~40 秒）——当前显示的是旧一份，"
        "生成完成后本页将自动刷新。</div>\n"
        "<script>\n"
        "(function poll(){\n"
        "  setTimeout(function(){\n"
        "    fetch('build_status', {cache:'no-store'})\n"
        "      .then(function(r){return r.json()})\n"
        f"      .then(function(s){{ if(!s.building && s.seq > {seq0}) "
        "{ location.reload(); } else { poll(); } }})\n"
        "      .catch(function(){ poll(); });\n"
        "  }, 5000);\n"
        "})();\n"
        "</script>"
    )
    return html.replace("<body>", "<body>\n" + banner, 1)


@router.get("/report")
async def report(
    date: str = Query(default="", description="指定交易日（YYYY-MM-DD 或 YYYYMMDD）；空=最新"),
) -> dict:
    """行业轮动报告 JSON（读盘优先；落伍时后台补生成并先回旧报告）。"""
    if date:
        payload = store.load(date)
        if payload is None:
            raise HTTPException(
                status_code=404,
                detail=f"{date} 没有落盘报告（已生成日期见 /history）")
        return payload
    return await _latest_report()


@router.get("/report.html", response_class=HTMLResponse)
async def report_html(
    date: str = Query(default="", description="指定交易日；空=最新"),
) -> HTMLResponse:
    """独立 HTML 报告页（前端可 iframe / 新窗口打开）。"""
    target = date
    stale_serving = False
    seq0 = 0
    if not target:
        # 走与 JSON 接口相同的"读盘优先 + 落伍后台补生成"路径：
        # 关机错过调度后第一次打开页面，看到的必须是补出来的新报告，
        # 而不是昨天的旧 HTML —— 区别是现在**先秒开旧的**，后台补完自动刷新。
        payload = await _latest_report()
        target = str((payload.get("meta") or {}).get("trade_date") or "")
        stale_serving = bool((payload.get("meta") or {}).get("background_refreshing"))
        seq0 = int(_BUILDING["seq"])  # type: ignore[arg-type]
    html = store.load_html(target)
    if html is None:
        # JSON 在但 HTML 缺（旧版本落盘）→ 现场渲染补一份
        payload = store.load(target)
        if payload is None:
            raise HTTPException(status_code=404, detail=f"{target or date} 没有报告")
        html = report_mod.render_html(payload)
    html = _localize_echarts(html)
    if stale_serving:
        html = _inject_refresh_banner(html, seq0=seq0)
    return HTMLResponse(content=html)


@router.get("/history")
async def history() -> dict:
    """已落盘报告的交易日列表（新的在前）。"""
    dates = store.history()
    return {"count": len(dates), "dates": dates,
            "latest": dates[0] if dates else None}


@router.get("/build_status")
async def build_status() -> dict:
    """后台生成状态（进程内计数器，零 I/O —— 供报告页横幅轮询）。"""
    return {
        "building": bool(_BUILDING["running"]),
        "seq": int(_BUILDING["seq"]),  # type: ignore[arg-type]
        "finished_at": str(_BUILDING["finished_at"]),
        "error": str(_BUILDING["error"]),
    }


@router.get("/echarts.min.js")
async def echarts_js():
    """本地 vendored ECharts（约 1MB；替代 jsdelivr CDN 的 20 秒隧道等待）。"""
    path = report_mod._ECHARTS_LOCAL  # noqa: SLF001 同模块族的常量
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail="本地 echarts.min.js 缺失（src/sector_rotation/static/），"
                   "报告页将回退 CDN")
    return FileResponse(
        path, media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=604800"})  # 7天：文件版本不变


@router.post("/refresh")
async def refresh() -> dict:
    """强制重新生成最近交易日报告（穿透落盘与 TTL）。

    同步执行（约 20~40 秒）：这是**用户显式点了"重新生成"按钮**的场景，
    按钮本身有"生成中…"的加载态，等待是预期内的；被动打开页面走的
    `/report.html` 才需要"秒开旧报告 + 后台补"的异步路径。
    """
    global _LAST_GEN_AT  # noqa: PLW0603
    started = datetime.now()
    payload = await service.generate(force=True)
    _LAST_GEN_AT = time.monotonic()
    meta = payload.get("meta") or {}
    return {
        "ok": True,
        "trade_date": meta.get("trade_date"),
        "generated_at": meta.get("generated_at"),
        "elapsed_seconds": round((datetime.now() - started).total_seconds(), 1),
        "industries": len(payload.get("industries") or []),
        "indices": len(payload.get("indices") or []),
    }


@router.get("")
async def root() -> dict:
    """模块自检：落盘目录、已有报告数、最近交易日、ECharts 本地化状态。"""
    dates = store.history()
    return {
        "module": "sector_rotation",
        "store_dir": str(store.store_dir()),
        "reports": len(dates),
        "latest": dates[0] if dates else None,
        "narrative_engine": "rule-based-v1",
        "schedule": "工作日 15:40 自动生成（作业 sector_rotation_report）",
        "echarts_local": not report_mod.echarts_src().startswith("http"),
    }
