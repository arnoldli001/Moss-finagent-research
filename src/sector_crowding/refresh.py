"""板块概念拥挤度：计算与刷新（一键刷新的后端实现）。

## 计算口径

    原始拥挤度 = 板块成交额 / 全市场成交额
    平滑拥挤度 = 原始拥挤度的 MA5（窗口可配置）
    拥挤度水位 = 平滑拥挤度 / 近6年平滑拥挤度最大值 × 100%

分母是**该板块自身**近 6 年的平滑拥挤度最大值（不是全市场、不是跨板块）——
"这个板块自己历史上最拥挤的时候"才是水位 100% 的定义。

## 三个必须守住的正确性要点

1. **水位必须用完整历史算，不能用本次增量窗口算**
   增量刷新只拉"上次之后"的几天。若只用这几天算 MA5 与 6 年最大值，
   MA5 会因缺前 4 天而失真、6 年最大值会退化成"这几天里的最大" →
   水位虚高、天天告警。所以每次刷新都从库里**重读该板块完整历史**再算。
2. **滚动窗口不能有未来函数**
   第 i 天的 MA5 只用 i-4..i；第 i 天的"6 年最大值"只用到 i 为止 ——
   `expanding.max()` 而不是 `max()`。否则历史水位会被后来的高点压低，
   回看曲线时"当时到底警没警"就失真了。
3. **分母为 0 或样本不足 → water_level = NULL**
   前端显示"数据不足"。把 NULL 当 0 会漏告警，当 100 会误告警。

## 一键刷新的并发模型

`POST /refresh_all` 立即返回 `task_id`，后台**线程**跑；前端轮询
`GET /refresh_status`。为什么不用 asyncio 任务：抓取是同步的 Tushare HTTP，
放进事件循环会卡住整个服务（项目里已有过这类事故记录）；用线程池 + 短连接
是最贴合现有代码形态的做法。

进度写在进程内字典（`_TASKS`）里：一键刷新是**单实例内的临时任务**，
不需要跨进程/重启可见 —— 服务重启后任务本就中断了，保留"幽灵进度"反而误导。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.core.errors import (
    BRIEF_DEFAULT,
    BRIEF_LOG,
    BRIEF_TIGHT,
    brief,
)
from src.sector_crowding import db, sources
from src.sector_crowding.config import SectorCrowdingConfig, load_config

logger = logging.getLogger(__name__)

#: 任务表：task_id → RefreshTask（进程内；见模块 docstring 的说明）
_TASKS: dict[str, RefreshTask] = {}
_TASKS_LOCK = threading.Lock()
#: 任务表上限（防止长期运行累积内存；只保留最近 N 个）
_TASKS_MAX = 20


# ======================================================================
# 任务与进度
# ======================================================================

@dataclass
class RefreshTask:
    """一次"一键刷新全部板块"的进度与结果。"""

    task_id: str
    status: str = "running"          # running | done | failed
    total: int = 0
    processed: int = 0
    inserted: int = 0
    failed_sectors: list[str] = field(default_factory=list)
    current_sector: str = ""
    started_at: str = ""
    finished_at: str = ""
    seconds: float = 0.0
    error: str = ""
    last_update_date: str = ""
    full_backfill: bool = False

    @property
    def progress(self) -> float:
        if self.total <= 0:
            return 0.0
        return round(min(1.0, self.processed / self.total), 4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id, "status": self.status,
            "total": self.total, "processed": self.processed,
            "progress": self.progress, "inserted": self.inserted,
            "failed": len(self.failed_sectors),
            "failed_sectors": self.failed_sectors[:20],
            "current_sector": self.current_sector,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "seconds": round(self.seconds, 1), "error": self.error,
            "last_update_date": self.last_update_date,
            "full_backfill": self.full_backfill,
            "message": self._message(),
        }

    def _message(self) -> str:
        if self.status == "running":
            return (f"刷新中… 已处理 {self.processed}/{self.total} 个板块"
                    + (f"（当前：{self.current_sector}）" if self.current_sector else ""))
        if self.status == "failed":
            return f"刷新失败：{self.error}"
        return (f"刷新完成，本次新增 {self.inserted} 条记录，"
                f"最后更新日期 {self.last_update_date or '—'}"
                + (f"；{len(self.failed_sectors)} 个板块失败（详见日志）"
                   if self.failed_sectors else ""))


def get_refresh_progress(task_id: str = "") -> dict[str, Any]:
    """查任务进度。`task_id` 留空返回最近一次任务（前端首屏用）。"""
    with _TASKS_LOCK:
        if task_id:
            task = _TASKS.get(task_id)
            return task.to_dict() if task else {
                "task_id": task_id, "status": "unknown",
                "message": "任务不存在（可能服务已重启）"}
        if not _TASKS:
            return {"task_id": "", "status": "idle", "total": 0, "processed": 0,
                    "progress": 0.0, "inserted": 0, "failed": 0,
                    "failed_sectors": [], "current_sector": "",
                    "started_at": "", "finished_at": "", "seconds": 0.0,
                    "error": "", "last_update_date": "", "full_backfill": False,
                    "message": "尚未执行过刷新"}
        latest = max(_TASKS.values(), key=lambda item: item.started_at or "")
        return latest.to_dict()


def _register(task: RefreshTask) -> None:
    with _TASKS_LOCK:
        _TASKS[task.task_id] = task
        if len(_TASKS) > _TASKS_MAX:
            for stale in sorted(_TASKS.values(),
                                key=lambda item: item.started_at or "")[:-_TASKS_MAX]:
                _TASKS.pop(stale.task_id, None)


# ======================================================================
# 计算
# ======================================================================

def compute_series(rows: list[dict[str, Any]], *, ma_window: int = 5,
                   lookback_years: int = 6, min_bars: int = 60
                   ) -> list[dict[str, Any]]:
    """给"板块日成交额 + 全市场成交额"序列补上拥挤度 / MA / 水位（纯函数）。

    入参按 `trade_date` 升序，每项含 `trade_date / sector_amount / market_amount`。
    返回同一批行，追加 `raw_crowding / ma5_crowding / water_level`。

    水位分母 = 该板块**近 lookback_years 年**内 `ma5_crowding` 的最大值，
    且**逐日 expanding**（第 i 天的分母只看到第 i 天为止）—— 这是"回看时
    当时的水位是多少"的正确口径，也避免未来函数。
    """
    if not rows:
        return []
    import pandas as pd

    frame = pd.DataFrame(rows)
    frame["trade_date"] = frame["trade_date"].astype(str)
    frame = frame.sort_values("trade_date").reset_index(drop=True)
    for column in ("sector_amount", "market_amount"):
        if column not in frame.columns:
            frame[column] = None
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    # 原始拥挤度：分母 <=0 或缺失 → NaN（不参与后续，最终 water_level=NULL）
    frame["raw_crowding"] = frame["sector_amount"] / frame["market_amount"].where(
        frame["market_amount"] > 0)
    # MA5：min_periods=1 让开头几天也有值（不足窗口时是"已有天数的均值"），
    # 但水位会用 min_bars 另行把关，避免用 2 天数据算出的水位
    frame["ma5_crowding"] = frame["raw_crowding"].rolling(
        max(1, int(ma_window)), min_periods=1).mean()

    # 近 N 年窗口：按日期的日历窗口做 expanding max
    window_start = frame["trade_date"].map(
        lambda day: sources.lookback_start(day, lookback_years))
    frame["_window_start"] = window_start
    maxima: list[float | None] = []
    current_start = ""
    running_max: float | None = None
    ma_values = frame["ma5_crowding"].tolist()
    for index, start in enumerate(frame["_window_start"].tolist()):
        value = ma_values[index]
        if start != current_start:
            # 窗口左端前移：重算该窗口内的最大值（窗口内最多 ~1460 天，代价可接受）
            current_start = start
            candidates = [item for item in ma_values[: index + 1]
                          if item == item]
            running_max = max(candidates) if candidates else None
        if value == value:
            running_max = value if running_max is None else max(running_max, value)
        maxima.append(running_max)
    frame["_max_ma5"] = maxima

    water: list[float | None] = []
    valid_count = frame["ma5_crowding"].notna().cumsum().tolist()
    for index, maximum in enumerate(maxima):
        value = ma_values[index]
        if (value != value or maximum is None or maximum <= 0
                or valid_count[index] < int(min_bars)):
            water.append(None)
        else:
            water.append(round(float(value / maximum), 6))
    frame["water_level"] = water

    # ⚠️ pandas 会把 float 列里的 None **还原成 nan**（`None` 只在 object 列里成立）。
    # 必须在出口显式转回 None：JSON 里没有合法位置放 nan，接口返回 nan 会让前端
    # 的"数据不足"判断失效（实测踩到：water_level 返回 nan 而不是 null）。
    out = frame[[
        "trade_date", "sector_amount", "market_amount",
        "raw_crowding", "ma5_crowding", "water_level"]].to_dict("records")
    return [{key: (None if isinstance(value, float) and value != value else value)
             for key, value in row.items()} for row in out]


def recompute_stored_water_levels(*, config: SectorCrowdingConfig | None = None,
                                  conn: Any = None,
                                  progress: Any = None) -> dict[str, Any]:
    """**不联网**重算已入库数据的原始拥挤度 / MA5 / 水位。

    为什么需要它：水位依赖三个可调参数（`ma_window` / `max_lookback_years` /
    `min_bars_for_water_level`）。改参数后如果只能靠"重新抓 2517 个板块"来生效，
    调一次参数就要十分钟且白耗接口配额 —— 而板块成交额与全市场成交额都已在库里，
    完全可以在本地重算。

    实测踩到的场景：首次全量回填后把 `min_bars_for_water_level` 从 60 提到 750
    （见 config.yaml 的说明），若不重算，库里仍留着按 60 根算出的水位 ——
    界面上会继续显示那些"历史太短所以恒为 100%"的误告警。

    返回 `{sectors, rows, skipped, seconds}`。单板块失败跳过，不影响其它板块。
    """
    config = config or load_config()
    own = conn is None
    conn = conn or db.get_db_connection(config)
    started = time.perf_counter()
    stats = {"sectors": 0, "rows": 0, "skipped": 0, "seconds": 0.0}
    try:
        codes = [str(row["sector_code"]) for row in conn.execute(
            f"SELECT DISTINCT sector_code FROM {db.DAILY_TABLE} "
            f"ORDER BY sector_code")]
        for code in codes:
            try:
                rows = db.query_sector_crowding(conn, code)
                if len(rows) < int(config.window.min_bars_for_water_level):
                    # 样本不足：水位必须清成 NULL（可能上一轮用更低的 min_bars 写过值）
                    db.upsert_sector_crowding(conn, [
                        {"trade_date": item["trade_date"], "sector_code": code,
                         "sector_name": item.get("sector_name", ""),
                         "sector_amount": item.get("sector_amount"),
                         "market_amount": item.get("market_amount"),
                         "raw_crowding": item.get("raw_crowding"),
                         "ma5_crowding": item.get("ma5_crowding"),
                         "water_level": None} for item in rows], commit=False)
                    stats["skipped"] += 1
                    continue
                computed = compute_series(
                    rows, ma_window=config.ma_window,
                    lookback_years=config.lookback_years,
                    min_bars=config.window.min_bars_for_water_level)
                # commit=False：整批重算只在最后提交一次（逐板块提交要 656 秒）
                db.upsert_sector_crowding(conn, [
                    {**item, "sector_code": code,
                     "sector_name": (rows[0].get("sector_name") or "")}
                    for item in computed], commit=False)
                stats["sectors"] += 1
                stats["rows"] += len(computed)
            except Exception as exc:  # noqa: BLE001 单板块失败不影响其它
                logger.warning("重算水位失败 %s：%s", code, brief(exc, BRIEF_DEFAULT))
                stats["skipped"] += 1
            if progress is not None:
                progress(code)
        conn.commit()          # 整批一次提交
        stats["seconds"] = round(time.perf_counter() - started, 1)
        logger.info("本地重算完成：%d 个板块 / %d 行，跳过 %d，耗时 %.1fs",
                    stats["sectors"], stats["rows"], stats["skipped"],
                    stats["seconds"])
        return stats
    finally:
        if own:
            conn.close()


def reclassify_boards(*, config: SectorCrowdingConfig | None = None,
                      conn: Any = None) -> dict[str, Any]:
    """按当前规则**重新判定**所有板块的 `is_concept`（不联网、不重算水位）。

    为什么需要：`is_concept` 只在刷新板块时写入。改了概念判定规则
    （`non_concept_board_types` / 名称正则 / 代码前缀）之后，已入库的板块仍带着
    旧判定 —— 告警列表会继续把行业指数当概念。这里只读板块列表 + 回写标志。
    """
    config = config or load_config()
    own = conn is None
    conn = conn or db.get_db_connection(config)
    try:
        boards = {item["sector_code"]: item for item in sources.list_boards(config)}
        changed = 0
        for row in db.query_sector_meta(conn):
            code = str(row["sector_code"])
            board = boards.get(code)
            if board is None:
                continue
            fresh = bool(board["is_concept"])
            if bool(row["is_concept"]) == fresh:
                continue
            db.update_sector_meta(
                conn, sector_code=code, sector_name=str(row["sector_name"] or ""),
                is_concept=fresh, board_type=str(row["board_type"] or ""),
                advance_watermark=False)
            changed += 1
        concepts = sum(1 for item in boards.values() if item["is_concept"])
        logger.info("板块重新分类：%d 个板块，概念 %d 个，变更 %d 个",
                    len(boards), concepts, changed)
        return {"sectors": len(boards), "concepts": concepts, "changed": changed}
    finally:
        if own:
            conn.close()


# ======================================================================
# 单板块刷新
# ======================================================================

def calculate_and_store(sector_code: str, start_date: str, end_date: str,
                        conn: Any, *, sector_name: str = "",
                        config: SectorCrowdingConfig | None = None,
                        warehouse: Any = None,
                        market_map: dict[str, float] | None = None
                        ) -> dict[str, Any]:
    """拉一个板块的日线 → 与全市场对齐 → 用**完整历史**算水位 → UPSERT。

    返回 `{inserted, start, end, bars, first, last}`；失败抛异常给调用方。

    ⚠️ 关键点（见模块 docstring 第 1 条）：`start_date` 是**本次要抓的区间**，
    但水位必须用**库里已有的完整历史 + 本次新数据**一起算。所以这里先把
    `[start_date, end_date]` 的新行 UPSERT 进去，再整段读回来重算并回写。
    """
    config = config or load_config()
    own_warehouse = warehouse is None
    warehouse = warehouse or sources.open_warehouse(config)
    if warehouse is None:
        raise RuntimeError("行情仓库不可用（无法取全市场成交额）")

    try:
        if market_map is None:
            market_map = sources.market_amount_by_day(
                warehouse, start=start_date, end=end_date)
        fetched = sources.fetch_board_daily(
            config, sector_code, start=start_date, end=end_date)
        if not fetched:
            return {"inserted": 0, "start": start_date, "end": end_date,
                    "bars": 0, "first": "", "last": "",
                    "note": "接口无数据（板块可能已停更/代码不在同花顺板块内）"}

        # ① 先写入本次抓到的原始行（水位稍后统一重算）
        rows = []
        for item in fetched:
            day = str(item["trade_date"])
            rows.append({
                "trade_date": day, "sector_code": sector_code,
                "sector_name": sector_name,
                "sector_amount": item["sector_amount"],
                "market_amount": market_map.get(day),
                "raw_crowding": None, "ma5_crowding": None, "water_level": None})
        db.upsert_sector_crowding(conn, rows)

        # ② 读回该板块**完整历史**，补全 market_amount 缺失的日子，再整体重算
        history = db.query_sector_crowding(conn, sector_code)
        missing_days = [item["trade_date"] for item in history
                        if item.get("market_amount") in (None, 0)]
        if missing_days:
            extra = sources.market_amount_by_day(
                warehouse, start=min(missing_days), end=max(missing_days))
            market_map = {**market_map, **extra}
        for item in history:
            if item.get("market_amount") in (None, 0):
                item["market_amount"] = market_map.get(item["trade_date"])
                item["sector_amount"] = item.get("sector_amount")

        computed = compute_series(
            history, ma_window=config.ma_window,
            lookback_years=config.lookback_years,
            min_bars=config.window.min_bars_for_water_level)
        db.upsert_sector_crowding(conn, [
            {**item, "sector_code": sector_code, "sector_name": sector_name}
            for item in computed])

        days = [item["trade_date"] for item in computed]
        return {"inserted": len(rows), "start": start_date, "end": end_date,
                "bars": len(computed), "first": days[0] if days else "",
                "last": days[-1] if days else "", "note": ""}
    finally:
        if own_warehouse:
            warehouse.close()


def refresh_single_sector(sector_code: str, *, conn: Any = None,
                          sector_name: str = "", is_concept: bool = True,
                          board_type: str = "",
                          config: SectorCrowdingConfig | None = None,
                          warehouse: Any = None,
                          market_map: dict[str, float] | None = None
                          ) -> dict[str, Any]:
    """增量刷新单个板块（水位线 = `sector_meta.last_update_date`）。

    - 从未入库 → 从"近 6 年 + MA 暖机余量"起全量回填；
    - 已有 → 从 `last_update_date` 的**下一个交易日**拉到最新交易日；
    - 已是最新 → 直接返回 `skipped`（幂等，不报错、不重复写）。

    **成功才推进水位线**：写入或计算出错时不推进，否则那段日期成为永久空洞。
    """
    config = config or load_config()
    own = conn is None
    conn = conn or db.get_db_connection(config)
    own_warehouse = warehouse is None
    warehouse = warehouse or sources.open_warehouse(config)
    if warehouse is None:
        raise RuntimeError("行情仓库不可用（无法取全市场成交额）")

    started = time.perf_counter()
    try:
        latest = sources.latest_trade_date(warehouse)
        if not latest:
            raise RuntimeError("行情仓库里没有交易日（quant_daily 为空）")

        last_update = db.get_last_update_date(conn, sector_code)
        if last_update:
            start = sources.next_trading_day(warehouse, last_update)
            full_backfill = False
            if not start:
                return {"sector_code": sector_code, "status": "skipped",
                        "reason": f"已是最新（{last_update}）", "inserted": 0,
                        "seconds": round(time.perf_counter() - started, 2)}
            if start > latest:
                return {"sector_code": sector_code, "status": "skipped",
                        "reason": f"已是最新（{last_update}）", "inserted": 0,
                        "seconds": round(time.perf_counter() - started, 2)}
        else:
            # 全量回填：多取 ma_window 个自然日的余量，保证首日 MA 有完整窗口
            start = sources.lookback_start(latest, config.lookback_years)
            full_backfill = True

        if market_map is None:
            market_map = sources.market_amount_by_day(
                warehouse, start=start, end=latest)

        outcome = calculate_and_store(
            sector_code, start, latest, conn, sector_name=sector_name,
            config=config, warehouse=warehouse, market_map=market_map)

        # 成功 → 推进水位线
        db.update_sector_meta(
            conn, sector_code=sector_code, sector_name=sector_name,
            is_concept=is_concept, board_type=board_type,
            first_trade_date=outcome.get("first", ""),
            last_update_date=latest, bars=outcome.get("bars", 0),
            advance_watermark=bool(outcome.get("bars")))
        elapsed = round(time.perf_counter() - started, 2)
        logger.info(
            "拥挤度刷新 %s(%s)：%s → %s，写入 %d 条，累计 %d 根，耗时 %.2fs%s",
            sector_code, sector_name or "-",
            outcome.get("start"), outcome.get("end"), outcome.get("inserted", 0),
            outcome.get("bars", 0), elapsed,
            "（全量回填）" if full_backfill else "")
        return {"sector_code": sector_code, "status": "ok",
                "start": outcome.get("start", ""), "end": outcome.get("end", ""),
                "inserted": outcome.get("inserted", 0),
                "bars": outcome.get("bars", 0), "first": outcome.get("first", ""),
                "last": outcome.get("last", ""), "seconds": elapsed,
                "full_backfill": full_backfill,
                "note": outcome.get("note", "")}
    finally:
        if own and conn is not None:
            conn.close()
        if own_warehouse and warehouse is not None:
            warehouse.close()


# ======================================================================
# 全量/增量刷新（一键刷新入口）
# ======================================================================

def refresh_all_incremental(*, task_id: str = "", config: SectorCrowdingConfig | None = None,
                            concepts_only: bool = False,
                            max_sectors: int = 0) -> RefreshTask:
    """后台执行：全部板块增量刷新。**同步函数**，由线程调用。

    `concepts_only=False` 默认刷**全部**板块（含行业/地区）：数据先全量落库，
    告警再按"仅概念"过滤 —— 这样以后想看行业拥挤度不用重跑 6 年。
    """
    config = config or load_config()
    task_id = task_id or uuid.uuid4().hex[:12]
    with _TASKS_LOCK:
        task = _TASKS.get(task_id)
    if task is None:
        task = RefreshTask(task_id=task_id)
        _register(task)
    task.status = "running"
    task.started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    started = time.perf_counter()

    conn = None
    warehouse = None
    try:
        db.init_tables(config=config)
        conn = db.get_db_connection(config)
        warehouse = sources.open_warehouse(config)
        if warehouse is None:
            raise RuntimeError("行情仓库不可用")

        boards = sources.list_boards(config)
        if concepts_only:
            boards = [item for item in boards if item["is_concept"]]
        if max_sectors > 0:
            boards = boards[:max_sectors]
        task.total = len(boards)
        task.full_backfill = not db.latest_trade_date(conn)
        logger.info("一键刷新开始：%d 个板块（%s）", task.total,
                    "首次全量回填" if task.full_backfill else "增量")

        latest = sources.latest_trade_date(warehouse)
        # 全市场成交额一次算满 6 年窗口（增量刷新时各板块起点不同，统一取最宽）
        market_start = sources.lookback_start(latest, config.lookback_years)
        market_map = sources.market_amount_by_day(
            warehouse, start=market_start, end=latest)
        logger.info("全市场成交额：%d 个交易日（%s ~ %s）",
                    len(market_map), market_start, latest)

        workers = max(1, min(int(config.performance.worker_threads), 16))
        inserted_total = 0
        done = 0
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="crowding") as pool:
            futures = {}
            for board in boards:
                futures[pool.submit(
                    _refresh_one_isolated, board, config, market_map
                )] = board
            for future in as_completed(futures):
                board = futures[future]
                done += 1
                try:
                    outcome = future.result()
                except Exception as exc:  # noqa: BLE001 单板块失败不影响其它
                    task.failed_sectors.append(
                        f"{board['sector_code']}:{brief(exc, BRIEF_TIGHT)}")
                    logger.warning("板块刷新失败 %s(%s)：%s",
                                   board["sector_code"], board.get("sector_name"),
                                   brief(exc, BRIEF_DEFAULT))
                    outcome = None
                if outcome and outcome.get("status") == "ok":
                    inserted_total += int(outcome.get("inserted") or 0)
                task.processed = done
                task.inserted = inserted_total
                task.current_sector = str(board.get("sector_name")
                                          or board.get("sector_code") or "")

        task.processed = task.total
        task.status = "done"
        task.last_update_date = latest
        logger.info("一键刷新完成：处理 %d，写入 %d，失败 %d，耗时 %.1fs",
                    task.total, inserted_total, len(task.failed_sectors),
                    time.perf_counter() - started)
    except Exception as exc:  # noqa: BLE001 任务级失败要留痕并让前端看到
        task.status = "failed"
        task.error = f"{type(exc).__name__}: {brief(exc, BRIEF_LOG)}"
        logger.exception("一键刷新失败")
    finally:
        if conn is not None:
            conn.close()
        if warehouse is not None:
            warehouse.close()
        task.seconds = time.perf_counter() - started
        task.finished_at = datetime.now().astimezone().isoformat(timespec="seconds")
    return task


def _refresh_one_isolated(board: dict[str, Any], config: SectorCrowdingConfig,
                          market_map: dict[str, float]) -> dict[str, Any]:
    """线程池 worker：每个板块自带连接（SQLite 连接不能跨线程共享）。

    按 `retry_times` 重试：Tushare 偶发 RemoteDisconnected / 频次抖动，
    单次失败就让整个板块留到下轮，代价是"某个板块总也补不齐"。
    """
    attempts = max(1, int(config.performance.retry_times) + 1)
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return refresh_single_sector(
                board["sector_code"], sector_name=str(board.get("sector_name") or ""),
                is_concept=bool(board.get("is_concept", True)),
                board_type=str(board.get("board_type") or ""),
                config=config, market_map=market_map)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(1.0 * (attempt + 1))
    raise last_error if last_error else RuntimeError("未知失败")


def start_refresh_all(*, concepts_only: bool = False,
                      max_sectors: int = 0) -> dict[str, Any]:
    """一键刷新入口：注册任务 + 起后台线程，**立即返回** task_id。

    返回体与 `get_refresh_progress` 同构，前端一次就能拿到首帧进度。
    """
    with _TASKS_LOCK:
        running = [item for item in _TASKS.values() if item.status == "running"]
    if running:
        # 已有任务在跑：直接返回那个任务，避免并发写同一批板块（会互相抢锁）
        logger.info("已有刷新任务在执行，复用 %s", running[0].task_id)
        return running[0].to_dict()

    task = RefreshTask(task_id=uuid.uuid4().hex[:12], status="running",
                       started_at=datetime.now().astimezone().isoformat(
                           timespec="seconds"))
    _register(task)
    thread = threading.Thread(
        target=refresh_all_incremental, name="crowding-refresh",
        kwargs={"task_id": task.task_id, "concepts_only": concepts_only,
                "max_sectors": max_sectors},
        daemon=True)
    thread.start()
    return task.to_dict()


__all__ = [
    "RefreshTask",
    "calculate_and_store",
    "compute_series",
    "get_refresh_progress",
    "recompute_stored_water_levels",
    "reclassify_boards",
    "refresh_all_incremental",
    "refresh_single_sector",
    "start_refresh_all",
]
