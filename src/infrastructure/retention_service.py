"""数据保留策略服务：按配置清理超期数据点、新闻缓存与增量流水表。

## 三档保留口径（均可经环境变量配置）

1. 数据点（`fact_data_points`）：最多保留最近 `data_retention_years` 年（默认 10）；
2. 新闻缓存（`news_cache`）：最多保留最近 `news_retention_days` 天（默认 30）；
3. **增量流水表**（认证/告警/通知，以及可选的历史行情台账）：
   见 `retention_passes.PASSES`。这是 2026-09-25 数据库审计后新增的一档 ——
   审计发现此前第 3 档**完全缺失**，约 40 张表只增不减
   （过期只 UPDATE 状态、查询时过滤，从不删行），主库因此涨到 6.32GB、
   其中 3.96GB 是"删了但没回收"的空闲页。

   为什么并进**本模块**而不是新开一个清理服务：保持"全项目只有一个清理
   入口、一个调度作业"这个已经成立的架构约束。调度侧仍然只有
   `data_retention_daily`（`scheduler/registry.py`）。

服务同时被「定时作业」与「应用启动钩子」调用：作业每日凌晨兜底，启动钩子保证
长期未跑定时任务的环境在重启时也能收敛。三档数据各自隔离失败：一档清理异常不
阻断其它档，整体不向调用方抛出（保留是增强能力，不能拖垮主链路）。
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

from src.core.config import Settings, get_settings
from src.infrastructure.repositories.repository_factory import (
    build_news_cache_repository,
    build_repository,
)
from src.infrastructure.retention_passes import (
    points_cutoff_for_years,
    run_passes,
)

logger = logging.getLogger(__name__)

#: 批量 VACUUM 的兜底超时（秒）。VACUUM 会重写整个库，6GB 级库需要分钟级。
_VACUUM_TIMEOUT_S = 1800.0


def _points_cutoff(years: int) -> str:
    """`fact_data_points` 保留 N 年的截止日。

    薄封装：算法收敛在 `retention_passes.points_cutoff_for_years`。
    必须只有一份实现 —— 数据源路由的**回填写入前过滤**用的是同一个口径，
    两处漂移就会退回"刚写进去就被下一次清理删掉"的自我抵消循环。
    """
    return points_cutoff_for_years(years)


def _points_cutoff(years: int) -> str:
    # 用 date 偏移近似 N 年（2月29日回退到28日），输出 YYYY-MM-DD。
    base = date.today()
    try:
        return base.replace(year=base.year - max(1, years)).isoformat()
    except ValueError:
        return base.replace(month=2, day=28, year=base.year - max(1, years)).isoformat()


async def run_retention(settings: Settings | None = None) -> dict[str, Any]:
    """执行一次保留清理，返回各档删除行数与说明（异常不外抛）。"""
    settings = settings or get_settings()
    result: dict[str, Any] = {
        "points_deleted": 0,
        "news_deleted": 0,
        "alerts_cutoff": "",
        "points_cutoff": "",
        "errors": [],
    }

    # —— 数据点：按保留年限 ——
    cutoff_points = _points_cutoff(int(settings.data_retention_years))
    result["points_cutoff"] = cutoff_points
    try:
        repo = build_repository(settings)
        try:
            await repo.ensure_schema()
            result["points_deleted"] = int(await repo.prune_before(cutoff_points))
        finally:
            await repo.close()
    except Exception as exc:  # noqa: BLE001 保留失败不阻断另一档/主链路
        logger.warning("数据点保留清理失败", exc_info=True)
        result["errors"].append(f"points: {type(exc).__name__}")

    # —— 新闻缓存：按保留天数 ——
    if settings.news_cache_enabled:
        cutoff_news = (
            datetime.now() - timedelta(days=int(settings.news_retention_days))
        ).isoformat(timespec="seconds")
        try:
            news_repo = build_news_cache_repository(settings)
            await news_repo.ensure_schema()
            result["news_deleted"] = int(await news_repo.prune(cutoff_news))
        except Exception as exc:  # noqa: BLE001
            logger.warning("新闻缓存保留清理失败", exc_info=True)
            result["errors"].append(f"news: {type(exc).__name__}")

    # —— 事件告警：**由 `retention_passes` 的 `fact_alerts` 档负责真正删除** ——
    #
    # 用户口径（2026-09-26）："事件告警的信息最多保留三天，超过3天的信息
    # 自动溢出删除。" 落地方式见下面的注释 —— 这里**刻意不再重复删一遍**。
    #
    # ## 为什么不在这个函数里直接调 `prune_alerts_before`
    #
    # 我最初在这里加了一段 `prune_alerts_before` 调用，但走查后发现
    # **已经有一条更完备的删除路径**：`retention_passes` 里的
    # `fact_alerts` 档（`time_column="expire_time"`, `setting=
    # "retention_alert_days"`），它带一个**级联**：
    #
    #     Cascade(table="user_alert_read", child_key="alert_id",
    #             parent_key="alert_id")
    #
    # 而 `prune_alerts_before` **没有**这条级联 —— 于是两套并存时，
    # 谁先跑到谁生效：新路径先跑会留下引用不存在告警的 `user_alert_read`
    # 孤儿行（"已读"记录指向一条被删掉的告警），表现是数据不自洽。
    #
    # 所以正确的落法是**把保留期改小**（`retention_alert_days` 365 → 3），
    # 而不是新开一条删除路径：
    #
    #   · `alert_expire_days = 3`     → `expire_time = trigger_time + 3天`
    #   · `retention_alert_days = 3`  → 按 `expire_time` 真正 DELETE + 级联
    #
    # 两个字段都在 `config.py`，注释里写明"改就一起改"。
    # `EventRepository.prune_alerts_before` 仍然保留为**显式 API**
    # （供运维/脚本按任意截止日清理，有完整测试 `test_alert_retention.py`），
    # 只是不参与这条每日作业 —— 避免同一件事有两条会互相打架的实现。
    alerts_cutoff = (
        datetime.now() - timedelta(days=int(settings.retention_alert_days))
    ).isoformat(timespec="seconds")
    result["alerts_cutoff"] = alerts_cutoff

    # —— 增量流水表：认证/告警/通知 + 可选的历史行情台账 ——
    # 逐表隔离失败已在 `run_passes` 内部处理；这里只汇总。
    try:
        passes = await run_passes(settings)
        result["passes"] = passes
        result["passes_deleted"] = sum(
            int(p.get("deleted") or 0) for p in passes)
        result["passes_cascaded"] = sum(
            int(p.get("cascaded") or 0) for p in passes)
        result["errors"].extend(
            f"{p['table']}: {p['error']}" for p in passes if p.get("error"))
    except Exception as exc:  # noqa: BLE001 这一档失败不影响上面两档
        logger.warning("增量保留清理失败", exc_info=True)
        result["errors"].append(f"passes: {type(exc).__name__}")

    # ⚠️ 级别取舍：只有**真删了行或有错误**才用 `warning`（本仓默认可见的级别）。
    #
    # 为什么不能一律 `info`：本仓 `main.py` 的模块 logger **没有配 handler**，
    # 其 `logger.info` 全部被丢弃（实测：`资金流快照预热完成` `竞价选股调度`
    # `事件告警启动补扫完成` 等启动钩子的 INFO **一条都不落盘**，而 uvicorn
    # 自己的输出有 201 条）。于是"清理到底跑没跑"在日志里完全看不出来 ——
    # 而保留是"静默失效"风险最高的一类功能（不清理不报错，只会某天发现库很大）。
    #
    # 为什么不一律 `warning`：绝大多数日子没有超期数据（实测主库/ pilot 都是
    # 0 行），每天一条"删除 0"的 WARNING 会变成噪声，久了就没人看了 ——
    # 那等于又把可见性弄丢了。所以：**有动作才响，没事就安静**。
    deleted_total = (int(result["points_deleted"]) + int(result["news_deleted"])
                     + int(result.get("passes_deleted", 0))
                     + int(result.get("passes_cascaded", 0)))
    message = (
        "数据保留完成：数据点删除 %s（截止 %s），新闻删除 %s，"
        "告警截止 %s（行数见流水档），"
        "流水档删除 %s（级联 %s）%s"
    )
    args = (
        result["points_deleted"], cutoff_points, result["news_deleted"],
        result["alerts_cutoff"],
        result.get("passes_deleted", 0), result.get("passes_cascaded", 0),
        ("，错误: " + ",".join(result["errors"])) if result["errors"] else "",
    )
    if deleted_total or result["errors"]:
        logger.warning(message, *args)
    else:
        logger.info(message, *args)
    return result


def vacuum_sync(settings: Settings | None = None) -> dict[str, Any]:
    """对主库执行一次 `VACUUM`，把空闲页真正还给文件系统。返回前后体积。

    ## 为什么需要它，以及为什么**不放进定时作业**

    审计实测主库 6.32GB 中 **3.96GB（63%）是 freelist 空闲页** ——
    清理删掉的行释放了页，但 `auto_vacuum=0` 意味着**文件永不缩小**。
    所以"清理了"并不等于"磁盘回来了"，必须显式 VACUUM。

    但它**不能**放进每日作业：

    1. VACUUM 会重写整个库，期间持有**排他锁**，所有读写全部阻塞
       （6GB 级库是分钟级 —— 实测这一档的超时给到 30 分钟）；
    2. 它需要**约等于库大小的临时空间**（与库同目录），空间不足会失败甚至损坏；
    3. 它是唯一会让"正在服务的实例"整段不可用的操作。

    因此它只作为**显式运维动作**暴露（`manage.py`/脚本），由使用者在停服窗口调用。
    本函数同步实现，调用方负责停服与选时机。

    ⚠️ 不要在服务进程正在写库时调用：见上面第 1 条。
    """
    settings = settings or get_settings()
    db_path = str(getattr(settings, "sqlite_path", "") or "")
    if not db_path:
        return {"ok": False, "error": "未配置 sqlite_path"}
    before = _file_size(db_path)
    try:
        conn = sqlite3.connect(db_path, timeout=_VACUUM_TIMEOUT_S)
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            # 先收缩 WAL，否则 vacuum 之后 .db-wal 可能仍占着空间
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 运维动作，失败要如实回报而不是抛
        logger.warning("VACUUM 失败：%s", exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "before": before, "after": _file_size(db_path)}
    after = _file_size(db_path)
    logger.info("VACUUM 完成：%.2f GB → %.2f GB", before / 1024 ** 3, after / 1024 ** 3)
    return {"ok": True, "before": before, "after": after,
            "freed": max(0, before - after)}


def _file_size(path: str) -> int:
    from pathlib import Path

    try:
        return Path(path).stat().st_size
    except OSError:
        return 0
