"""定时调度管理：作业清单/手动触发/运行记录/日报。**管理员专属**。

## ⚠️ 为什么整条路由挂 `require_admin`（2026-09-26 加）

这一族接口原先**只有登录门槛**，没有任何授权判断。"调度管理"页签的可见性
来自 `GET /api/v1/me/features` 的 `visible_views`，而 `scheduler` 是**套餐里
可勾选的一项** —— 也就是说"只给管理员"当时只是 `configs/platform_tiers.json`
里的一个**默认值**，任何一个管理员在「功能权限」页把它勾给 VIP/试用，
对方立刻就能看到页签并点「手动运行」。

而"隐藏页签 ≠ 禁止访问"是本项目反复写明的既有结论（见
`src/api/routes/my_features.py` 的模块 docstring、`docs/INTEL_PERMISSION_DESIGN.md`）：
不带 Cookie 或带一个普通用户 Cookie 直接 `POST /api/v1/scheduler/jobs/{name}/run`
从来不需要经过前端。**能点的代价不是"看到一张表"**：

    snapshot_industry_watchlist  kind=graph_snapshot
    params.targets = [半导体, 煤炭, 创新药, 白酒]
      → src/scheduler/jobs.py::_graph_snapshot 逐个 ainvoke 完整研究图
      → 一次点击 = 4 次完整研究图（全 Agent 链 + 全部 LLM 调用，分钟级）

所以这里用**路由级依赖**（`dependencies=[Depends(require_admin)]`）而不是
在每个函数里各写一行：新加一个端点时**默认就是管理员专属**，
不会重演"新接口忘了加鉴权"这一类故障（与 `LoginGateMiddleware` 的
fail-closed 取向一致）。

与 `src/api/routes/my_features.py` 的 `ADMIN_ONLY_VIEWS` 构成两道门：
那里管"**看不看得见**"（非管理员不下发 `scheduler` 页签），
这里管"**调不调得动**"。只做前者等于没做 —— 见上面的 `curl` 一句。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from src.api.routes.admin import require_admin
from src.scheduler.jobs import execute_job
from src.scheduler.registry import JOB_REGISTRY

#: 整条路由的管理员门槛。返回的 `user_id` 不用（路由级依赖的返回值会被丢弃），
#: 这里要的只是"非管理员请求直接 401/403，进不到函数体"。
router = APIRouter(
    prefix="/api/v1/scheduler",
    tags=["scheduler"],
    dependencies=[Depends(require_admin)],
)


def _run_log(request: Request):
    return request.app.state.run_log


@router.get("/jobs")
async def list_jobs(request: Request) -> dict:
    log = _run_log(request)
    jobs = []
    for name, spec in JOB_REGISTRY.items():
        last = log.last_run(name)
        jobs.append({
            "name": name,
            "cron": spec.cron,
            "kind": spec.kind,
            "description": spec.description,
            "params": spec.params,
            "paused": log.is_paused(name),
            "last_run": (
                {"status": last["status"], "start_time": last["start_time"],
                 "duration_ms": last["duration_ms"],
                 "records_processed": last["records_processed"],
                 "error_message": last["error_message"]}
                if last else None
            ),
        })
    return {"jobs": jobs}


@router.post("/jobs/{name}/run", status_code=202)
async def trigger_job(name: str, request: Request) -> dict:
    """手动触发作业：API进程内直接执行（无需Redis/Worker），同样落运行记录。

    ⚠️ **同步执行**：`await execute_job(...)` 会一直等到作业跑完才返回，
    所以 `202` 在这里只表示"已受理并正在跑"，不代表"排队后立刻返回"。
    对 `graph_snapshot` 类作业（如 `snapshot_industry_watchlist` 的 4 个标的），
    这一等就是分钟级、且真实消耗 LLM 额度 —— 前端因此加了一次确认弹窗
    （见 `web/src/components/SchedulerPanel.tsx`）。
    """
    if name not in JOB_REGISTRY:
        raise HTTPException(status_code=404, detail=f"未注册作业: {name}")
    # ★ 手动触发是**第三条**触发路径（前两条：`_tick()` 定时、
    #   `main.py` 启动自检的 `scheduler.trigger`）。三条必须同判据，否则
    #   "按写权限裁掉更新作业"这件事会被这里一键绕过去（`CHG-0087`）。
    #
    # 为什么在**执行前**就拒，而不是让它跑到写闸门再失败：
    #   `quant_data_sync` 的第一段是**下载分区**，写库是最后一段 ——
    #   跑到最后才失败等于白下一遍数据（本项目为"下了但没入库"付过代价）。
    #   且失败会落一条 `failed`，看起来是故障，实际是"这台机器不负责写"。
    from src.scheduler.registry import job_deny_reason

    denied = job_deny_reason(name)
    if denied:
        raise HTTPException(
            status_code=409,
            detail=(f"作业 {name} 在本实例上不会执行：{denied}。"
                    "这是**写权限归属**决定的，不是故障（见 docs/PRD.md §18.6）；"
                    "要真正跑它，请在承担该存储更新责任的实例上触发。"))
    record = await execute_job(
        request.app.state.runtime, name, _run_log(request), trigger="manual"
    )
    return {"run": record}


@router.get("/runs")
async def list_runs(
    request: Request, job: str | None = None, limit: int = 100
) -> dict:
    limit = max(1, min(limit, 1000))
    return {"runs": _run_log(request).read_all(job, limit=limit)}


@router.get("/runs/summary")
async def runs_summary(request: Request, date: str | None = None) -> dict:
    return _run_log(request).daily_summary(date)
