"""数据源健康汇总：全部数据源的**能力 + 实测速度 + 新鲜度**（供前端健康度面板）。

设计原则（实测驱动，不靠声明）：

- **能力**：能不能提供实时行情，是"能不能做T"的分水岭；
- **速度**：EWMA 延迟，来自做T模块的源健康表（真实调用记录）；
- **新鲜度**：该源最新数据的时间戳 vs 当前时间 —— 这条最难造假，
  也是"Tushare 不能做T"的实证依据（盘中调用返回空表）。

对外只读，不触发任何网络请求（除 Tushare 是读本地缓存目录）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from src.core.errors import (
    BRIEF_DEFAULT,
    BRIEF_TIGHT,
    brief,
)
from src.intraday.source_health import SOURCE_CAPABILITIES

logger = logging.getLogger(__name__)

# 健康度缓存：这份内容是分钟级信息，而组装它要遍历 3.5 万个分区清单 + 查仓库九表。
# 缓存 5 分钟 → 连续打开页面/多标签页不再重复付这份成本（实测首次 4.5 秒、命中 0ms）。
_CACHE_TTL = 300.0
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
# 仓库统计的落盘缓存（读 14GB SQLite，实测 6~28 秒）：请求永远读缓存秒回
_WAREHOUSE_STATS_FILE = Path("data/quant/warehouse_stats.json")
_WAREHOUSE_TTL = 600.0
_WAREHOUSE_REFRESHING = False
# Tushare 分区覆盖的落盘缓存：`coverage()` 要遍历 3.5 万个分区目录做 stat，
# 实测平时 2.9 秒，但**启动后磁盘被 akshare 子进程占满时会膨胀到 140 秒以上**，
# 于是首个 /health 请求要等两三分钟（用户看到"运维页一直转圈"）。
# 分区覆盖是小时级信息，没有理由让请求路径去遍历目录 —— 同一套
# "读缓存秒回 + 后台刷新" 口径（与 _warehouse_health 一致）。
_TUSHARE_STATS_FILE = Path("data/quant/tushare_stats.json")
_TUSHARE_TTL = 1800.0
_TUSHARE_REFRESHING = False


def _tushare_health(root: str = "data/quant/tushare",
                    universe: str = "a_share",
                    *, force: bool = False) -> dict[str, Any]:
    """Tushare 健康度：token 可用性 + 各数据集覆盖 + 新鲜度（滞后交易日数）。

    ## 为什么要落盘缓存 + 后台刷新（2026-09-17 实测）

    `coverage()` 会遍历 **35,711 个分区目录**逐个 `stat`。平时 2.9 秒，
    但服务刚起来时磁盘正被 akshare 子进程（25 只票的分钟线/板块快照）占满，
    这一步实测膨胀到 **140 秒以上** —— 首个 `/api/v1/health` 要 142.8 秒才返回，
    而这只是"运维看一眼数据源状态"。

    分区覆盖是**小时级**信息，不该由请求路径去遍历目录。改成与
    `_warehouse_health` 同一套口径：请求读落盘缓存秒回，过期就顺手起一个
    后台线程重算（去重），下次请求自然拿到新值。
    """
    if force:
        # 显式重算（预热/降级路径）：算完写回缓存。
        value = _build_tushare_health(root, universe)
        _write_tushare_stats(value)
        return value

    cached = _read_tushare_stats()
    if cached is None:
        # 完全没有缓存（首次部署）：**不在请求路径上现算**。
        # 遍历 3.5 万个分区在磁盘繁忙时要几十秒到一两分钟（实测 2026-09-17：
        # 首个 /health 因此要 142.8 秒），而这份数据本来就是小时级信息。
        # 返回"待生成"结构 + 起后台线程算，前端立刻能渲染、下次请求就有值 ——
        # 与「诚实数据」一致：不给假数字，而是明确标注尚未生成。
        _refresh_tushare_stats_async(root, universe)
        return _tushare_pending()

    stamp, payload = cached
    if time.time() - stamp > _TUSHARE_TTL:
        _refresh_tushare_stats_async(root, universe)
    return payload


def _tushare_pending() -> dict[str, Any]:
    """覆盖统计尚未生成时的占位结构（前端据此显示"统计生成中"）。"""
    return {
        "source": "tushare",
        "label": SOURCE_CAPABILITIES["tushare"]["label"],
        "kind": SOURCE_CAPABILITIES["tushare"]["kind"],
        "realtime": False,
        "token": {"configured": False, "hint": ""},
        "datasets": [],
        "partitions": 0,
        "rows": 0,
        "latest_date": "",
        "note": SOURCE_CAPABILITIES["tushare"]["note"],
        "intraday_usable": False,
        "stats_pending": True,
        "stats_note": "分区覆盖统计正在后台生成（首次部署或缓存被清理），稍后刷新即可",
    }


def _build_tushare_health(root: str, universe: str) -> dict[str, Any]:
    """真正去遍历分区的那一步（**只在后台线程/预热里调用**）。"""
    from src.quant.tushare_source import resolve_token, token_hint

    try:
        token = resolve_token()
        token_state = {"configured": True, "hint": token_hint(token)}
    except Exception as exc:  # noqa: BLE001 没 token 也是合法状态
        token_state = {"configured": False, "error": brief(exc, BRIEF_DEFAULT)}

    dataset_root = Path(root) / universe
    datasets: list[dict[str, Any]] = []
    if dataset_root.exists():
        from src.quant.dataset_store import DatasetStore

        for child in sorted(dataset_root.iterdir()):
            if not child.is_dir():
                continue
            store = DatasetStore(child.name, root=root, universe=universe)
            coverage = store.coverage()
            lag_days = _lag_days(coverage.get("last", ""))
            datasets.append({
                "dataset": child.name,
                "partitions": coverage["partitions"],
                "rows": coverage["rows"],
                "first": coverage["first"],
                "last": coverage["last"],
                "lag_days": lag_days,
                # 日频数据集滞后 > 3 天视为"该更新了"（含周末）
                "stale": bool(lag_days is not None and lag_days > 3
                              and child.name.endswith(("daily", "factor", "flow",
                                                       "limit", "suspend"))),
            })

    return {
        "source": "tushare",
        "label": SOURCE_CAPABILITIES["tushare"]["label"],
        "kind": SOURCE_CAPABILITIES["tushare"]["kind"],
        "realtime": False,
        "token": token_state,
        "datasets": datasets,
        "partitions": sum(item["partitions"] for item in datasets),
        "rows": sum(item["rows"] for item in datasets),
        "latest_date": max((item["last"] for item in datasets), default=""),
        "note": SOURCE_CAPABILITIES["tushare"]["note"],
        "intraday_usable": False,
    }


def _lag_days(last: str) -> int | None:
    """分区最新日期距今天的自然日数（用于新鲜度告警）。"""
    if not last or len(last) != 8 or not last.isdigit():
        return None
    import pandas as pd

    try:
        stamp = pd.Timestamp(last)
    except (ValueError, TypeError):
        return None
    return int((pd.Timestamp.today().normalize() - stamp).days)


def _intraday_source_health(runtime: Any) -> dict[str, Any]:
    """做T模块的源健康表（真实调用记录：EWMA 延迟/成功率/冷却）。"""
    service = getattr(runtime, "intraday", None)
    provider = getattr(service, "data_provider", None) if service else None
    tracker = getattr(provider, "health", None) if provider else None
    if tracker is None:
        return {"available": False,
                "note": "做T模块未启用（/intraday/snapshot 首次调用后开始统计）"}
    snapshot = tracker.snapshot()
    snapshot["available"] = True
    snapshot["capabilities"] = SOURCE_CAPABILITIES
    return snapshot


def _warehouse_health(root: str = "data/quant/tushare",
                      *, force: bool = False) -> dict[str, Any]:
    """本地数据仓库健康度：方言、连接、各表行数/时间跨度。

    单独列出来是因为它和"数据源"不是一回事：Tushare 是**采集**入口，
    仓库是**存放与查询**出口。回测实际读的是后者，所以它的行数与跨度
    才是"回测能拿到多少数据"的直接证据。

    ## 为什么落盘缓存 + 后台刷新（2026-09-16 实测）

    仓库是 **14.28 GB** 的 SQLite（WAL），查一次九表的行数/时间跨度实测
    **6 秒（页缓存热）~ 28 秒（冷）** —— 这是纯 I/O，不是逻辑慢。而它以前是
    **同步跑在事件循环里**的：打开「运行指标」页会让整个服务卡住，
    实测 `/health` 直接 300 秒超时（做T面板同时卡死）。

    现在：结果落盘到 `data/quant/warehouse_stats.json`，请求**永远读缓存秒回**；
    超过 `_WAREHOUSE_TTL` 就在后台线程里重算。启动时也会预热一次，
    所以用户第一次打开页面就是热的。
    """
    cached = _read_warehouse_stats()
    if cached is not None and not force:
        age = time.time() - cached[0]
        if age > _WAREHOUSE_TTL and not _WAREHOUSE_REFRESHING:
            _refresh_warehouse_stats_async(root)
        payload = dict(cached[1])
        payload["cached_age_seconds"] = round(age, 1)
        return payload
    if force:
        # 预热线程走这条（启动时把统计算出来并落盘），冷启动后第一次请求就是热的。
        try:
            from src.quant.warehouse import warehouse_status

            status = warehouse_status(root=root)
        except Exception as exc:  # noqa: BLE001 健康度面板不允许因此 500
            return {"available": False, "error": f"{type(exc).__name__}: "
                                                 f"{brief(exc, BRIEF_DEFAULT)}"}
        _write_warehouse_stats(status)
        return _decorate_warehouse(status)
    # 没有缓存（首次部署 / 缓存被清理）且不是预热：**别让请求等 14GB 的库扫描**
    # （实测冷页缓存 28 秒，磁盘繁忙时更久）。给占位结构 + 后台算，下次请求就有值；
    # 与 Tushare 覆盖统计同一口径，也和「诚实数据」一致：标注"生成中"而不是给假数字。
    _refresh_warehouse_stats_async(root)
    return {
        "dialect": "unknown", "total_rows": 0, "tables": [],
        "count_mode": "unknown", "dataset_count": 0,
        "latest_date": "", "earliest_date": "",
        "stats_pending": True,
        "stats_note": "仓库统计正在后台生成（首次部署或缓存被清理），稍后刷新即可",
    }


def _read_tushare_stats() -> tuple[float, dict[str, Any]] | None:
    """读 Tushare 分区覆盖的落盘缓存；缺失/损坏返回 None。"""
    try:
        raw = _TUSHARE_STATS_FILE.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    stamp = float(payload.pop("_cached_at", 0) or 0)
    return (stamp, payload) if stamp else None


def _write_tushare_stats(status: dict[str, Any]) -> None:
    """原子写：预热线程与请求线程可能同时写，别让读方看到半个 JSON。"""
    tmp = _TUSHARE_STATS_FILE.with_suffix(
        _TUSHARE_STATS_FILE.suffix + f".tmp{os.getpid()}")
    try:
        _TUSHARE_STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(
            json.dumps({**status, "_cached_at": time.time()},
                       ensure_ascii=False, indent=1),
            encoding="utf-8")
        os.replace(tmp, _TUSHARE_STATS_FILE)
    except OSError as exc:  # noqa: BLE001 写不进缓存不影响返回
        logger.debug("Tushare 统计缓存写入失败：%s", brief(exc, BRIEF_TIGHT))
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _refresh_tushare_stats_async(root: str, universe: str) -> None:
    """后台线程重算 Tushare 覆盖（带去重标记，不阻塞任何请求）。"""
    global _TUSHARE_REFRESHING
    if _TUSHARE_REFRESHING:
        return
    _TUSHARE_REFRESHING = True

    def _job() -> None:
        global _TUSHARE_REFRESHING
        try:
            _write_tushare_stats(_build_tushare_health(root, universe))
        except Exception as exc:  # noqa: BLE001 后台失败只记日志
            logger.debug("Tushare 统计后台刷新失败：%s", brief(exc, BRIEF_TIGHT))
        finally:
            _TUSHARE_REFRESHING = False

    threading.Thread(target=_job, name="tushare-stats-refresh",
                     daemon=True).start()


def _read_warehouse_stats() -> tuple[float, dict[str, Any]] | None:
    """读落盘缓存 → (写入时间戳, 内容)；不存在/损坏返回 None。"""
    try:
        payload = json.loads(_WAREHOUSE_STATS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    stamp = float(payload.pop("_cached_at", 0) or 0)
    return (stamp, payload) if stamp else None


def _write_warehouse_stats(status: dict[str, Any]) -> None:
    try:
        _WAREHOUSE_STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _WAREHOUSE_STATS_FILE.write_text(
            json.dumps({**status, "_cached_at": time.time()},
                       ensure_ascii=False, indent=1),
            encoding="utf-8")
    except OSError as exc:  # noqa: BLE001 写不进缓存不影响返回
        logger.debug("仓库统计缓存写入失败：%s", brief(exc, BRIEF_TIGHT))


def _decorate_warehouse(status: dict[str, Any]) -> dict[str, Any]:
    """补上面板需要的派生字段（数据集数 / 最新与最早日期）。"""
    tables = status.get("tables", [])
    status["dataset_count"] = len(tables)
    status["latest_date"] = max((item.get("last", "") for item in tables),
                                default="")
    status["earliest_date"] = min((item.get("first", "") for item in tables
                                   if item.get("first")), default="")
    return status


def _refresh_warehouse_stats_async(root: str) -> None:
    """后台线程重算仓库统计（带全局去重标记，不阻塞任何请求）。"""
    global _WAREHOUSE_REFRESHING
    if _WAREHOUSE_REFRESHING:
        return
    _WAREHOUSE_REFRESHING = True

    def _job() -> None:
        global _WAREHOUSE_REFRESHING
        try:
            from src.quant.warehouse import warehouse_status

            _write_warehouse_stats(warehouse_status(root=root))
        except Exception as exc:  # noqa: BLE001 后台失败只记日志
            logger.debug("仓库统计后台刷新失败：%s", brief(exc, BRIEF_TIGHT))
        finally:
            _WAREHOUSE_REFRESHING = False

    threading.Thread(target=_job, name="warehouse-stats-refresh",
                     daemon=True).start()


def warm(runtime: Any) -> None:
    """预热：后台算一次数据健康度（含仓库统计），让首次请求秒开。

    由 lifespan 在启动后调用；任何异常都不该影响服务启动。
    """
    def _job() -> None:
        try:
            build_data_health(runtime, force=True)
        except Exception as exc:  # noqa: BLE001
            logger.debug("数据健康度预热失败：%s", brief(exc, BRIEF_TIGHT))

    threading.Thread(target=_job, name="data-health-warm", daemon=True).start()


def _static_capability_matrix() -> list[dict[str, Any]]:
    """全部数据源的能力矩阵（不依赖运行时状态，前端首次打开就能看到全貌）。"""
    rows: list[dict[str, Any]] = []
    for source, capability in SOURCE_CAPABILITIES.items():
        rows.append({"source": source, **capability})
    return rows


def build_data_health(runtime: Any, *, force: bool = False,
                      cache_ttl: float = _CACHE_TTL) -> dict[str, Any]:
    """组装前端「数据健康度」需要的全部内容（带短 TTL 缓存）。

    为什么缓存：这份内容要遍历 35,711 个分区清单（实测 3.5 秒）+ 查仓库九表，
    而面板每次打开都会取一次。数据健康度是**分钟级**信息，30 秒内复用完全够用。
    实测：首次 ~5 秒、命中 <1 毫秒。

    `force=True` 会**一路透传**到仓库统计与 Tushare 覆盖两处：这两处都有落盘缓存，
    不透传的话"强制重算"只会重算外壳、里面仍是旧缓存（测试实测过这个坑：
    改了 `MOSS_QUANT_SQLITE` 后 `force=True` 仍报上一轮的 mysql 方言）。
    启动预热走的正是 `force=True`，保证下次请求读到的是刚算出来的值。
    """
    cached = _CACHE.get("value")
    if (not force and cached is not None
            and time.monotonic() - cached[0] < cache_ttl):
        return cached[1]
    value = _build_data_health_uncached(runtime, force=force)
    _CACHE["value"] = (time.monotonic(), value)
    return value


def _build_data_health_uncached(runtime: Any, *, force: bool = False) -> dict[str, Any]:
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "capability_matrix": _static_capability_matrix(),
        "intraday_sources": _intraday_source_health(runtime),
        "tushare": _tushare_health(force=force),
        "warehouse": _warehouse_health(force=force),
        "notes": [
            "做T实时链路按实测速度排序：QMT（本机终端，中位 0ms）优先，"
            "腾讯（54ms）为第一备用，新浪逐笔兜底；东财在本机网络被阻断。",
            "Tushare 只有 EOD 数据（当日 15:00~16:00 后入库），"
            "盘中调用返回空表，因此不参与做T实时链路，仅作盘后校验与因子源。",
            "回测取数优先走本地仓库（索引命中 5~20ms/截面），"
            "库不可用时自动回退 CSV 分区缓存，来源会在结果里标注 db/csv。",
            "仓库行数口径见 warehouse.count_mode：sqlite 下用 MAX(rowid)"
            "（写入为原地 upsert 且从不删除，故等于 COUNT(*)），"
            "避免对 14GB 库做全表 COUNT(*) —— 那曾让 /health 卡到 300 秒超时。",
        ],
    }


def invalidate_cache(*, include_disk: bool = False) -> None:
    """清空健康度缓存（测试/运维用）。

    `include_disk=True` 连**落盘的仓库/Tushare 统计**一起删 —— 否则调用方
    （或测试）会继续读到那份旧结果，看起来像"改了配置没生效"。
    """
    _CACHE.clear()
    if include_disk:
        for path, label in ((_WAREHOUSE_STATS_FILE, "仓库统计"),
                            (_TUSHARE_STATS_FILE, "Tushare统计")):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:  # noqa: BLE001 删不掉不影响内存缓存已清空
                logger.debug("删除%s缓存失败：%s", label, brief(exc, BRIEF_TIGHT))
