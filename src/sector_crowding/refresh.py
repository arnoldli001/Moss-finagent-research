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
        # ⚠️ 用户口径（2026-09-27）：**前端不要显示失败信息**。
        # 失败板块对用户没有可操作性（他既看不到名单，也不能单独重刷），
        # 显示出来只是噪音 —— 而且失败大多是可自愈的（数据源限流/抖动），
        # 下一轮会自动补上。失败明细进日志，需要时查
        # `logs/sector_crowding.log`；失败名单另存库用于"同周只补失败板块"。
        return (f"刷新完成，本次新增 {self.inserted} 条记录，"
                f"最后更新日期 {self.last_update_date or '—'}")


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

def bias_factor(bars: int, curve: dict[str, float] | None) -> float:
    """按根数取分母放大系数 `r(n)`（线性插值，超出上界取 1.0）。

    `r(T) = median(M_full / M_T)`，在满窗板块上由
    `scripts/calibrate_shrink_k.py` 标定。语义：**只有这么长的历史时，
    样本最大值平均比真实 6 年极值小多少倍** —— 补上这个倍数，
    水位才与满窗板块可比。
    """
    if not curve:
        return 1.0
    points = sorted((int(k), float(v)) for k, v in curve.items())
    if not points:
        return 1.0
    if bars <= points[0][0]:
        return points[0][1]
    if bars >= points[-1][0]:
        return points[-1][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= bars <= x1:
            if x1 == x0:
                return y1
            ratio = (bars - x0) / (x1 - x0)
            return y0 + (y1 - y0) * ratio
    return 1.0


def compute_series(rows: list[dict[str, Any]], *, ma_window: int = 5,
                   lookback_years: int = 6, min_bars: int = 60,
                   bias_curve: dict[str, float] | None = None,
                   min_bars_publish: int = 60
                   ) -> list[dict[str, Any]]:
    """给"板块日成交额 + 全市场成交额"序列补上拥挤度 / MA / 水位（纯函数）。

    入参按 `trade_date` 升序，每项含 `trade_date / sector_amount / market_amount`。
    返回同一批行，追加 `raw_crowding / ma5_crowding / water_level`。

    水位分母 = 该板块**近 lookback_years 年**内 `ma5_crowding` 的最大值，
    且**逐日 expanding**（第 i 天的分母只看到第 i 天为止）—— 这是"回看时
    当时的水位是多少"的正确口径，也避免未来函数。

    ## 分母的样本量偏差校正（`bias_curve`，2026-09-27）

    分母是**样本最大值**，`E[M_n]` 随 n 单调上升 → 历史短的板块水位系统性虚高。
    原来用二值门槛（750 根）堵，但那只是把悬崖挪了个位置（750~1460 根有 1033 个
    板块，与 >=1460 根那组仍不等价）。

    现在改成**尺度校正** `denom = M_n · r(n)`，`r` 由截断回测标定：偏差随 n
    连续收敛，`n >= 750` 时 `r = 1`（实测结论），所以**满窗板块行为完全不变**。

    ⚠️ 曾先试"向同类中位数收缩"（James-Stein 式），**截断回测证伪**：
    越收越偏（k=0 偏差 +0.047 → k=250 时 +0.099）。原因见 config 注释。
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
    # 但水位会用发布下限另行把关，避免用 2 天数据算出的水位
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

    # 校正规程下发布下限放宽到 min_bars_publish；不给曲线时退回原二值门槛
    floor = int(min_bars_publish) if bias_curve else int(min_bars)

    water: list[float | None] = []
    valid_count = frame["ma5_crowding"].notna().cumsum().tolist()
    for index, maximum in enumerate(maxima):
        value = ma_values[index]
        count = int(valid_count[index])
        if value != value or maximum is None or maximum <= 0 or count < floor:
            water.append(None)
            continue
        denominator = float(maximum) * bias_factor(count, bias_curve)
        water.append(round(float(value / denominator), 6) if denominator > 0
                     else None)
    frame["water_level"] = water

    # ⚠️ pandas 会把 float 列里的 None **还原成 nan**（`None` 只在 object 列里成立）。
    # 必须在出口显式转回 None：JSON 里没有合法位置放 nan，接口返回 nan 会让前端
    # 的"数据不足"判断失效（实测踩到：water_level 返回 nan 而不是 null）。
    out = frame[[
        "trade_date", "sector_amount", "market_amount",
        "raw_crowding", "ma5_crowding", "water_level"]].to_dict("records")
    return [{key: (None if isinstance(value, float) and value != value else value)
             for key, value in row.items()} for row in out]


def peer_max_median(conn: Any, config: SectorCrowdingConfig) -> float | None:
    """**已废弃**：这是"向同类中位数收缩"用的先验，截断回测证伪后不再使用。

    保留函数是为了让任何旧调用点显式报错而不是静默取到 None
    （静默 None 会让水位悄悄退回旧口径，是最难发现的一类故障）。
    """
    raise RuntimeError(
        "peer_max_median 已废弃：向同类中位数收缩在截断回测里一致变差"
        "（k=0 偏差 +0.047 → k=250 时 +0.099）。现用尺度校正 "
        "`bias_factor(bars, config.window.bias_curve)`，见 docs/_water_bias_curve.txt")


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
        # 校正规程按根数查表，不需要横截面先验 —— 逐板块调用也没有 O(N²) 问题
        floor = (int(config.window.min_bars_publish)
                 if config.window.bias_correction_enabled
                 else int(config.window.min_bars_for_water_level))
        for code in codes:
            try:
                rows = db.query_sector_crowding(conn, code)
                if len(rows) < floor:
                    # 低于发布下限：水位必须清成 NULL（可能上一轮用更低的门槛写过值）
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
                    min_bars=config.window.min_bars_for_water_level,
                    bias_curve=(config.window.bias_curve
                                if config.window.bias_correction_enabled
                                else None),
                    min_bars_publish=config.window.min_bars_publish)
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
        # ★ 2026-09-27 第八轮：重算完成后**全量重建** max_ma5 物化表
        #   整批一次写，避免循环内每板块一次 UPSERT
        rebuilt = db.rebuild_max_ma5_table(conn, sector_codes=codes)
        logger.info("recompute: 重建 max_ma5 物化表 %d 行", rebuilt)
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
            min_bars=config.window.min_bars_for_water_level,
            bias_curve=(config.window.bias_curve
                        if config.window.bias_correction_enabled else None),
            min_bars_publish=config.window.min_bars_publish)
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
        # ★ 2026-09-27 第八轮：每板块 refresh 后**增量**更新 max_ma5 物化表
        #   全量回填（首次）→ 真正计算 max；增量刷新 → max(老值, 新批次 max)
        #   这是单板块的 UPSERT，避免扫全表
        try:
            db.rebuild_max_ma5_table(conn, sector_codes=[sector_code])
        except Exception as exc:  # noqa: BLE001 物化表刷新失败不影响主流程
            logger.warning("max_ma5 物化表增量更新失败 %s: %s",
                           sector_code, brief(exc, BRIEF_DEFAULT))
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

#: 全量刷新台账的键（存在 `METRIC_META_TABLE` 这个通用 KV 表里，不另建表）
_FULL_WEEK_KEY = "full_refresh_week"
_FULL_FAILED_KEY = "full_refresh_failed"


def _week_key() -> str:
    """ISO 周键，如 `2026-W39`（与 `metrics.week_key` 同口径）。"""
    iso = datetime.now().astimezone().date().isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _last_full_refresh(conn: Any) -> tuple[str, list[str]]:
    """上次**全量**刷新所在的周 + 那一次失败的板块代码。"""
    meta = db.get_metric_meta(conn)
    failed = str(meta.get(_FULL_FAILED_KEY) or "")
    return (str(meta.get(_FULL_WEEK_KEY) or ""),
            [code for code in failed.split(",") if code])


def _record_full_refresh(conn: Any, week: str, failed: list[str]) -> None:
    """记下这次全量刷新：周键 + 失败名单（供"同周只补失败板块"用）。"""
    db.set_metric_meta(conn, {
        _FULL_WEEK_KEY: week,
        _FULL_FAILED_KEY: ",".join(sorted(set(failed))),
    })


def _drop_blacklisted(boards: list[dict[str, Any]]
                      ) -> tuple[list[dict[str, Any]], int]:
    """剔除板块黑名单里的板块，返回 `(保留的板块, 剔除数量)`。

    ## 为什么拥挤度侧也要挡一次

    主线挖掘侧的黑名单只在 `import_crowding_pool` 生效（决定谁入 `ml_board`），
    但**拥挤度的日更并不经过那条路径** —— 不在这里再挡一次，
    那 488 个板块的日线仍会被持续拉取和写入，只是没有任何人读。
    用户口径是"黑名单板块**不再更新数据**"，所以必须挡在刷新入口。

    ## 两个刻意的边界

    1. **只挡更新，不删历史。** 已写入的 `sector_crowding_daily` 行保持原样：
       删历史是不可逆的，而"停止更新"是可逆的。代价是恢复跟踪某个板块时，
       它的日线会有一段缺口，需要单独回补（`refresh_all_incremental`
       按 `latest_trade_date` 增量，不会自动补这段）。
    2. **清单读不到时不挡**（`None` 而非空集合）。与主线侧同一口径：
       把"读不到清单"当成"不排除任何板块"会让刷新量无声翻倍，
       而把"读不到"当成"排除全部"更糟 —— 会一个板块都不刷。
       `load_sector_blacklist` 用 `None` 表达不可用，这里原样传递。
    """
    from src.mainline.config import load_sector_blacklist
    from src.sector_crowding.config import load_crowding_exclusions

    blacklist = load_sector_blacklist()
    if not blacklist:
        return boards, 0
    # ⚠️ 停日更用**整份**主黑名单 ∪ **拥挤度剔除清单**（2026-09-22 补）。
    #
    # 为什么这里可以用整份、而 `metrics` / `db` 不行：停日更**不删数据、
    # 不影响任何人看历史**，代价只是"以后不刷了"；而"算不算指标/显不显示"
    # 直接决定看板上有没有它。用户明确说的是「不再**下载数据**关注其拥挤度
    # 相关的信息」—— 下载这一侧他要求的就是"别刷了"，所以整份黑名单
    # （含历史遗留的 865xxx / GICS 行业）在这里一并停掉是符合口径的。
    #
    # 新增的 `crowding_exclusions.yaml` 是用户在**拥挤度功能**里点名剔除的
    # 那 82 个（含 GICS 式行业名与同花顺指数），它们不在主黑名单里，
    # 所以必须单独并进来，否则"不再下载"这一半要求会落空。
    extra = load_crowding_exclusions()
    if extra:
        blacklist = frozenset(blacklist) | frozenset(extra)
    kept = [item for item in boards
            if str(item.get("sector_code") or "") not in blacklist]
    return kept, len(boards) - len(kept)


def refresh_all_incremental(*, task_id: str = "", config: SectorCrowdingConfig | None = None,
                            concepts_only: bool = False,
                            max_sectors: int = 0,
                            pool_only: bool = True) -> RefreshTask:
    """后台执行：板块增量刷新。**同步函数**，由线程调用。

    `pool_only=True`（默认）只刷**关注板块池** —— 也就是
    `sector_crowding_list` 里**可见**的那些板块（用户在清单里增删/置顶的那份，
    主线挖掘的板块池正是从这里导入的，见 `mainline/datastore.py`）。
    2026-09-24 用户报障："一键刷新显示 1850 个板块，可我关注的池子只有几百个" ——
    原来按钮默认全量扫（2517 个板块减去黑名单 ≈ 1850），既慢又与"关注"这个词不符。

    `pool_only=False` 才是全量（含行业/地区）：数据先全量落库，告警再按概念过滤 ——
    想看行业拥挤度时用它，日常增量不必。

    `concepts_only=True` 是在此之上的**再**一层过滤（只保留概念板块），保留原语义。
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
        boards, blocked = _drop_blacklisted(boards)
        if blocked:
            logger.info("板块黑名单：跳过 %d 个板块的日更（清单 "
                        "configs/sector_blacklist.yaml）", blocked)
        if pool_only:
            # 关注板块池 = 清单里**可见**的板块（用户增删过的那份）。
            # 池子为空时**退回全量**而不是刷 0 个：按钮点了什么都不发生，
            # 用户只会以为"刷新坏了"。
            pool = set(db.list_visible_codes(conn, concepts_only=False))
            if pool:
                before = len(boards)
                boards = [item for item in boards
                          if str(item.get("sector_code") or "") in pool]
                logger.info("按关注板块池过滤：%d → %d 个板块（池内可见 %d 个）",
                            before, len(boards), len(pool))
            else:
                logger.warning("关注板块池为空（sector_crowding_list 无可见板块），"
                               "本次退回全量刷新")
        if max_sectors > 0:
            boards = boards[:max_sectors]

        # ── 同一周内不重复全量刷新（用户口径 2026-09-27）──────────────────
        #
        # 「如果上次全量刷新时间和这次手动触发是同一周，就不要全量刷新了，
        #   只刷新上次失败的概念板块即可。」
        #
        # 为什么合理：全量刷新实测 **1845 个板块 / 220 秒**，而这 1845 个里
        # 绝大多数上周已经刷过、数据没变；重复跑的唯一效果是**再撞一次数据源
        # 限流**（实测 8 个板块因 `ths_daily` 500次/分钟 超限而失败）。
        is_full = not pool_only
        if is_full:
            last_week, last_failed = _last_full_refresh(conn)
            this_week = _week_key()
            if last_week == this_week and last_failed:
                wanted = set(last_failed)
                before = len(boards)
                boards = [item for item in boards
                          if str(item.get("sector_code") or "") in wanted]
                logger.info("本周（%s）已全量刷过，改为只补上次失败的 %d 个板块"
                            "（原 %d 个）", this_week, len(boards), before)
                if not boards:
                    logger.info("上次失败名单已全部补齐，本次无板块需要刷新")
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

        # ⚠️ `workers` 必须在这里定义 —— 2026-09-27 我改这段时误删了它，
        # 结果是运行到 `ThreadPoolExecutor(max_workers=workers)` 直接
        # `NameError: name 'workers' is not defined`，**「一键刷新」整条路挂掉**。
        # 单测没兜住它，因为测试跑的是 `compute_series` / `recompute_*`，
        # 没有一条会走到这里 —— 见 tests 里新增的
        # `test_refresh_all_defines_workers`。
        workers = max(1, min(int(config.performance.worker_threads), 16))
        # 偏差校正是**按根数查表**，不需要横截面先验 —— 所以没有 O(N²) 问题，
        # worker 里直接查 config.window.bias_curve 即可（纯本地、无 IO）。
        floor = (int(config.window.min_bars_publish)
                 if config.window.bias_correction_enabled
                 else int(config.window.min_bars_for_water_level))
        logger.info("一键刷新：%d 线程；分母偏差校正%s（发布下限 %d 根）",
                    workers,
                    "开启" if config.window.bias_correction_enabled else "关闭",
                    floor)
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
        # 记全量刷新台账：周键 + 失败名单（供"同周只补失败板块"）。
        # 失败名单只取代码 —— `task.failed_sectors` 是 `code:原因` 形式。
        if is_full and conn is not None:
            try:
                codes = [str(item).split(":", 1)[0]
                         for item in task.failed_sectors]
                _record_full_refresh(conn, _week_key(), codes)
            except Exception:  # noqa: BLE001 台账写失败不该让刷新算失败
                logger.warning("全量刷新台账写入失败", exc_info=True)
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
                      max_sectors: int = 0,
                      pool_only: bool = True) -> dict[str, Any]:
    """一键刷新入口：注册任务 + 起后台线程，**立即返回** task_id。

    返回体与 `get_refresh_progress` 同构，前端一次就能拿到首帧进度。
    `pool_only=True` 只刷关注板块池（默认），`False` 才是全量 —— 见
    `refresh_all_incremental` 的说明。
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
                "max_sectors": max_sectors, "pool_only": pool_only},
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
