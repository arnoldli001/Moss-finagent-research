"""板块拥挤度·周频异动指标（前端 4 列，用于辅助判断大资金建仓异动）。

## 4 列的口径

| 列 | 计算式 |
|---|---|
| 近5日拥挤度变化 | `水位(最新日) / 水位(5个交易日前) - 1` |
| 近1月拥挤度变化 | `水位(最新日) / 水位(20个交易日前) - 1` |
| 近2月拥挤度变化 | `水位(最新日) / 水位(40个交易日前) - 1` |
| 近1月资金净流入占比 | `Σ 成分股主力净流入(近20交易日) / 板块流通市值(20个交易日前)` |

**成分股名单优先用主线挖掘的提纯结果**（`ml_member_pure`，`relevant=1`），
提纯不可用（未评估 / 提纯退化）时回退 `sector_member` 原始名单；这一行的
来源会写进 `member_source`，见 `members.py` 的说明。

**"变化百分比"是水位（water_level）的变化，不是拥挤度绝对值的变化。** 水位本来就是
"相对自己近 6 年最高"的比例量，取它的变化率才有跨板块可比性 —— 0.5% 的水位涨到
1.5% 是"翻三倍"，但 1.5% 和 0.5% 绝对比出来的 1% 在板块之间毫无意义。

## 为什么这 4 列要周频持久化

其中第 4 列要按板块抓 `ths_member` 成分股再回本地仓库聚合（近 20 日逐日净流入 ×
上千只成分股），900+ 个板块跑一遍是分钟级；而**它依赖的资金流数据本身就是 T+2 左右
才落仓**，每天重算既慢又几乎是同一个数。所以按周算一次、落
`sector_crowding_metric` 表，前端直接读；需要立刻更新时用接口手动触发。

## 数据滞后与"没算过"的区分

- 拥挤度（前 3 列）来自本地拥挤度库，最新交易日通常比行情仓库晚 1~2 天；
- 资金流（第 4 列）来自行情仓库 `quant_moneyflow`，实测比 `quant_daily` 再晚 2 天。
  所以每一列都**自带基准日/末端日**（`base_date_*` / `flow_last_date`），
  前端 hover 能看到"这个数是用哪两天的数据算的"，不会把滞后的数当成当天的。
- 算不出来（板块太新、缺少基准日水位、成分股缺失）一律写 NULL → 前端显示"—"。
  **不能写 0**："没算出来/数据不足"和"没有变化"是两件事（与水位 NULL 同一口径）。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from src.sector_crowding import db, sources
from src.sector_crowding.config import (
    SectorCrowdingConfig,
    load_config,
    load_crowding_exclusions,
)
from src.sector_crowding.members import open_pure_db, pure_member_codes


def _sector_blacklist() -> frozenset[str]:
    """不算拥挤度指标的板块 = **用户点名剔除的主线批次** ∪ **拥挤度剔除清单**。

    ## 两条来源为什么都要

    * `load_removed_concepts()`（184 个）：用户在主线程点名池级剔除的板块，
      他明确说过「不计入板块拥挤度检测范围」；
    * `load_crowding_exclusions()`（82 个）：用户在**拥挤度功能**里单独点名
      剔除的板块（含 GICS 式行业名与同花顺指数）。

    ⚠️ **刻意不用整份 `sector_blacklist.yaml`（604 条）**：那里面大头是历史
    遗留（淘汰的 `865xxx` 概念体系、GICS 行业、地域板块），而拥挤度模块
    **刻意保留**它们（"以后想看行业拥挤度不用重跑 6 年"）。按整份过滤会把
    最新一周 1878 个板块砍到 1541 —— 多砍的绝大多数是行业指数，是**过度过滤**。

    两份清单任一不可用 → 只并入可用的那份；两份都不可用 → 空集 = 不过滤。
    """
    out: set[str] = set()
    try:
        from src.mainline.config import load_removed_concepts

        loaded = load_removed_concepts()
        if loaded:
            out |= set(loaded)
        else:
            logger.warning("主线批次剔除清单不可用，本轮不含这部分")
    except Exception as exc:  # noqa: BLE001 清单坏了不该让指标任务崩掉
        logger.warning("主线批次剔除清单读取失败，本轮不含这部分：%s",
                       type(exc).__name__)
    try:
        extra = load_crowding_exclusions()
        if extra:
            out |= set(extra)
        else:
            logger.warning("拥挤度剔除清单不可用，本轮不含这部分")
    except Exception as exc:  # noqa: BLE001
        logger.warning("拥挤度剔除清单读取失败，本轮不含这部分：%s",
                       type(exc).__name__)
    return frozenset(out)


def _scope_to_pool(conn: sqlite3.Connection, codes: list[str], *,
                   pool_only: bool = True,
                   concepts_only: bool = True) -> list[str]:
    """把待算板块**收口到看板默认视图真正渲染的那些**。

    ## 为什么收（2026-09-27）

    改之前 `compute_all_metrics()` 对 `compute_water_changes()` 返回的**全部**
    板块算 —— 实测一周 **1479 个**，而看板默认视图只渲染 **262 个**。

    ## ⚠️ `concepts_only` 必须跟着看板的默认勾选状态

    看板的「只看概念板块」勾选框**默认勾上**（`SectorCrowdingTab.tsx`
    `useState(true)` → `config_list?concepts_only=true`）。
    第一版收口漏了这点、用了 `concepts_only=False`，于是取到 **554**，
    多算了 **292 个非概念板块**（行业指数 / 地区 / 指数样本 / 同花顺自建组合）。

    判据必须是**默认渲染集合**，不是"可能被渲染的集合" —— 勾选框是逃生口
    （tooltip 自己就写"数据本身是全量落库的，取消勾选即可查看全部"）。
    取消勾选时那 292 行的 4 列会显示「—」，这是有意接受的代价。

    ⚠️ 这**不是**主线那套「20 日胜率」门槛：拥挤度刻意**不**吃
    `sector_blacklist.yaml`（604 条），因为它刻意保留行业指数
    （"以后想看行业拥挤度不用重跑 6 年"，见 `_sector_blacklist`）。
    这里收的是**看板可见性**，不是题材优劣。

    ## 清单为空时退回全量

    首装还没种子化、或用户把清单清空时，交集是空集。这时**不能**算成
    "本周 0 个板块、0 行" —— 那会像一个成功的空周，把库里的历史周次
    序列弄断。所以退回全量并告警：宁可多算，不可静默算空。
    """
    if not pool_only:
        return codes
    pool = set(db.list_visible_codes(conn, concepts_only=concepts_only))
    kept = [code for code in codes if code in pool]
    if not kept:
        logger.warning("看板清单为空，本轮退回全量计算（%d 个板块）", len(codes))
        return codes
    logger.info("周频指标按看板清单收口：%d → %d 个板块（少算 %d，概念 only=%s）",
                len(codes), len(kept), len(codes) - len(kept), concepts_only)
    return kept


logger = logging.getLogger(__name__)

#: A 股交易日历近似：用 `quant_daily` 里真实出现过的日期，不要用自然日推算
_WAREHOUSE_DATASET_FOR_CALENDAR = "quant_daily"


# ======================================================================
# 周键与交易日历
# ======================================================================

def week_key(day: str | date | None = None) -> str:
    """ISO 周键，如 `2026-W38`（用于"本周算过没有"与幂等 UPSERT）。"""
    if day is None:
        target = date.today()
    elif isinstance(day, date):
        target = day
    else:
        text = str(day).strip()
        target = (datetime.strptime(text, "%Y%m%d").date() if len(text) == 8
                  else date.fromisoformat(text))
    iso = target.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _open_warehouse(config: SectorCrowdingConfig) -> sqlite3.Connection:
    path = Path(config.warehouse_path)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def trading_days(wh: sqlite3.Connection, *, end: str = "",
                 limit: int = 0) -> list[str]:
    """倒序交易日（最近的在前）。`end` 留空 = 仓库里最新的一天。"""
    sql = f"SELECT DISTINCT trade_date FROM {_WAREHOUSE_DATASET_FOR_CALENDAR}"
    params: list[Any] = []
    if end:
        sql += " WHERE trade_date <= ?"
        params.append(str(end))
    sql += " ORDER BY trade_date DESC"
    if limit > 0:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [str(row[0]) for row in wh.execute(sql, params)]


def _latest_dataset_date(wh: sqlite3.Connection, dataset: str) -> str:
    row = wh.execute(f"SELECT MAX(trade_date) FROM {dataset}").fetchone()
    return str(row[0] or "") if row else ""


# ======================================================================
# 前 3 列：水位变化
# ======================================================================

def water_level_change(base: float | None, now: float | None, *,
                       min_base: float = 0.0) -> float | None:
    """`(now / base - 1) * 100`。

    基准水位为 0、缺失、或低于 `min_base` → None。后一种情况见
    `MetricConfig.min_base_water` 的说明：从 0.04% 涨到 0.02 元算出来的
    "+5000%" 只是"从几乎没成交变成有一点成交"，不是资金异动，却会在按列
    降序时把真正的异动板块全挤下去。
    """
    if base is None or now is None:
        return None
    if not (base > 0) or base < float(min_base):
        return None
    return round((float(now) / float(base) - 1.0) * 100.0, 4)


def compute_water_changes(conn: sqlite3.Connection, *,
                          short_days: int = 5,
                          month_days: int = 20,
                          long_days: int = 40,
                          min_base_water: float = 0.0,
                          codes: set[str] | None = None
                          ) -> tuple[dict[str, dict[str, Any]], str]:
    """批量算所有板块的水位变化（一次全表扫描 + 内存分板块，别按板块查库）。

    返回 `({sector_code: {...}}, 最新交易日)`。

    ## ⚠️ 这里**也要**过滤黑名单（2026-09-22 补）

    `refresh._drop_blacklisted()` 只挡**日更**（不再往 `sector_crowding_daily`
    写新行），而本函数是**全表扫描** `sector_crowding_daily` ——
    所以黑名单里的板块会带着**停更前的老数据**继续算指标，
    而且因为 `series[-1][0] != latest` 大部分会被丢掉，偶尔却可能
    在"全库最新日恰好等于它最后一天"时混进来。

    用户的口径是「**不计入板块拥挤度检测范围**」，所以这里一并挡掉：
    黑名单是这三件事（不入主线池 / 停止日更 / 不算拥挤度指标）的
    **唯一事实来源**。历史行保留不动（`db.DAILY_TABLE` 不删）。
    """
    blacklist = _sector_blacklist()
    sql = (f"SELECT sector_code, trade_date, water_level FROM {db.DAILY_TABLE} ")
    params: list[Any] = []
    if codes:
        # 只算指定板块（用户新增板块时的"临时算"走这条路）：
        # 全表 217 万行扫一遍要 2~3 秒，**单板块只查它自己的行**是毫秒级。
        marks = ",".join("?" * len(codes))
        sql += f"WHERE sector_code IN ({marks}) "
        params.extend(sorted(codes))
    sql += "ORDER BY sector_code, trade_date"
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return {}, ""

    per_sector: dict[str, list[tuple[str, float | None]]] = {}
    latest = ""
    for row in rows:
        code = str(row["sector_code"])
        if code in blacklist:
            continue
        day = str(row["trade_date"])
        if day > latest:
            latest = day
        per_sector.setdefault(code, []).append((day, row["water_level"]))

    # 全库最新交易日：各板块自己的最后一天可能更早（停牌/退市），
    # 那类板块整体不参与本周指标，否则会拿"两个月前的数"冒充"近5日变化"
    want = (("5d", short_days), ("1m", month_days), ("2m", long_days))

    out: dict[str, dict[str, Any]] = {}
    for code, series in per_sector.items():
        if series[-1][0] != latest:
            continue
        out[code] = {"trade_date": latest, "chg_5d": None, "chg_1m": None,
                     "chg_2m": None, "base_date_5d": "", "base_date_1m": "",
                     "base_date_2m": ""}
        now_water = series[-1][1]
        for tag, span in want:
            if len(series) <= span:
                continue          # 历史不够长 → 该列为 NULL（前端"—"）
            base_date, base_water = series[-1 - span]
            out[code][f"base_date_{tag}"] = base_date
            out[code][f"chg_{tag}"] = water_level_change(
                base_water, now_water, min_base=min_base_water)
    return out, latest


# ======================================================================
# 第 4 列：资金净流入 / 流通市值
# ======================================================================

def _sector_members_cached(conn: sqlite3.Connection, sector_code: str, *,
                           config: SectorCrowdingConfig,
                           pure_conn: sqlite3.Connection | None = None
                           ) -> tuple[list[str], str]:
    """板块成分股 + **来源标记**（`purified` / `raw`）。

    ## 为什么要优先用提纯名单

    拥挤度的第 4 列是「窗口内主力净流入 ÷ 基准日流通市值」，分母分子都靠
    成分股。原始 `ths_member` 名单里有大量沾边个股，会把真实的主力集中度
    **稀释**掉；而主线挖掘早就在用 `ml_member_pure`（双门限提纯）。
    两套子系统各用一份名单，等于同一时刻两边在讲不同的故事。实测提纯
    压缩比中位 0.51（培育钻石 885937：20 → 11 只）。

    ## 提纯不可用时必须回退，而且回退要能被审计

    提纯表只覆盖 324 个板块，拥挤度跟踪 1878 个 —— 剩下的只能用原始名单。
    所以调用方既要知道名单，也要知道**这份名单是哪来的**：来源会落进
    `sector_crowding_metric.member_source`，重算之后可以直接查
    "多少板块真的用上了提纯"。
    """
    if config.metrics.use_purified_members and pure_conn is not None:
        codes = pure_member_codes(pure_conn, sector_code,
                                  min_members=config.metrics.min_purified_members)
        if codes:
            return codes, "purified"

    ttl = max(1, int(config.metrics.member_cache_days))
    row = conn.execute(
        f"SELECT MAX(updated_at) AS t, COUNT(*) AS n FROM {db.MEMBER_TABLE} "
        f"WHERE sector_code = ?", (sector_code,)).fetchone()
    cached_at = str(row["t"] or "") if row else ""
    cached_n = int(row["n"] or 0) if row else 0
    if cached_n > 0 and cached_at:
        try:
            age = datetime.now().astimezone() - datetime.fromisoformat(cached_at)
            if age < timedelta(days=ttl):
                return ([str(item["stock_code"]) for item in
                         conn.execute(
                             f"SELECT stock_code FROM {db.MEMBER_TABLE} "
                             f"WHERE sector_code = ?", (sector_code,))],
                        "raw")
        except ValueError:
            pass  # 时间戳格式异常就当过期，重抓一次即可

    last_error: Exception | None = None
    for attempt in range(max(1, int(config.metrics.member_retry_times) + 1)):
        try:
            fetched = sources.fetch_board_members(sector_code)
            if not fetched:
                return [], "raw"
            db.upsert_members(conn, sector_code, fetched)
            return ([str(item["code"]) for item in fetched if item.get("code")],
                    "raw")
        except Exception as exc:  # noqa: BLE001 单板块失败不该中断整轮
            last_error = exc
            if attempt + 1 < max(1, int(config.metrics.member_retry_times) + 1):
                time.sleep(0.8 * (attempt + 1))
    logger.warning("成分股抓取失败 %s：%s", sector_code, last_error)
    return [], "raw"


def _flow_for_codes(wh: sqlite3.Connection, codes: list[str], *,
                    start: str, end: str, base_date: str
                    ) -> tuple[float | None, float | None]:
    """一批成分股的「窗口内净流入合计」与「基准日流通市值合计」。"""
    if not codes:
        return None, None
    marks = ",".join("?" * len(codes))
    net = wh.execute(
        f"SELECT SUM(net_mf_amount) FROM quant_moneyflow "
        f"WHERE trade_date > ? AND trade_date <= ? AND code IN ({marks})",
        [start, end, *codes]).fetchone()[0]
    mv = wh.execute(
        f"SELECT SUM(circ_mv) FROM quant_daily_basic "
        f"WHERE trade_date = ? AND code IN ({marks})",
        [base_date, *codes]).fetchone()[0]
    return (float(net) if net is not None else None,
            float(mv) if mv is not None else None)


@dataclass
class MetricTask:
    """一轮周频指标的进度（前端轮询用，结构对齐 RefreshTask）。"""

    task_id: str
    status: str = "running"        # running | done | failed
    total: int = 0
    processed: int = 0
    rows_written: int = 0
    failed_sectors: list[str] = field(default_factory=list)
    current_sector: str = ""
    started_at: str = ""
    finished_at: str = ""
    seconds: float = 0.0
    error: str = ""
    week: str = ""
    as_of_date: str = ""
    flow_last_date: str = ""
    skipped: bool = False

    @property
    def progress(self) -> float:
        if self.total <= 0:
            return 0.0
        return round(min(1.0, self.processed / self.total), 4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id, "status": self.status,
            "total": self.total, "processed": self.processed,
            "progress": self.progress, "rows_written": self.rows_written,
            "failed": len(self.failed_sectors),
            "failed_sectors": self.failed_sectors[:20],
            "current_sector": self.current_sector,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "seconds": round(self.seconds, 1), "error": self.error,
            "week": self.week, "as_of_date": self.as_of_date,
            "flow_last_date": self.flow_last_date, "skipped": self.skipped,
            "message": self._message(),
        }

    def _message(self) -> str:
        if self.status == "running":
            return (f"计算中… 已处理 {self.processed}/{self.total} 个板块"
                    + (f"（当前：{self.current_sector}）"
                       if self.current_sector else ""))
        if self.status == "failed":
            return f"计算失败：{self.error}"
        if self.skipped:
            return f"本周（{self.week}）已算过，跳过重复计算"
        return (f"计算完成（{self.week}）：{self.rows_written} 个板块，"
                f"拥挤度截至 {self.as_of_date or '—'}，"
                f"资金流截至 {self.flow_last_date or '—'}"
                + (f"；{len(self.failed_sectors)} 个板块失败"
                   if self.failed_sectors else ""))


_TASKS: dict[str, MetricTask] = {}
_TASKS_LOCK = threading.Lock()
_TASKS_MAX = 10
_RUN_LOCK = threading.Lock()


def get_metric_progress(task_id: str = "") -> dict[str, Any]:
    with _TASKS_LOCK:
        if task_id:
            task = _TASKS.get(task_id)
            return task.to_dict() if task else {
                "task_id": task_id, "status": "unknown",
                "message": "任务不存在（可能服务已重启）"}
        if not _TASKS:
            return {"task_id": "", "status": "idle", "total": 0, "processed": 0,
                    "progress": 0.0, "rows_written": 0, "failed": 0,
                    "failed_sectors": [], "current_sector": "", "started_at": "",
                    "finished_at": "", "seconds": 0.0, "error": "", "week": "",
                    "as_of_date": "", "flow_last_date": "", "skipped": False,
                    "message": "尚未计算过周频指标"}
        latest = max(_TASKS.values(), key=lambda item: item.started_at or "")
        return latest.to_dict()


def _register(task: MetricTask) -> None:
    with _TASKS_LOCK:
        _TASKS[task.task_id] = task
        if len(_TASKS) > _TASKS_MAX:
            for stale in sorted(_TASKS.values(),
                                key=lambda item: item.started_at or "")[:-_TASKS_MAX]:
                _TASKS.pop(stale.task_id, None)


def compute_metrics_for_codes(codes: list[str] | set[str],
                              *, config: SectorCrowdingConfig | None = None
                              ) -> dict[str, Any]:
    """**临时算**指定板块的周频 4 列（用户新增板块到前端时调用）。

    ## 为什么需要它（用户 2026-09-27 定的产品规则）

    > 前端已配置数据要提前算，用户打开前端直接就加载算好的数据，而不是等结果。
    > 对于非前端配置数据，在用户**新增**概念板块到前端时，**再临时算**。

    所以范围是这样切的：

    * **预计算** = 清单里**已配置且可见**的板块（`visible = 1`，当前 554 个）
      —— 打开页面就有数，不用等
    * **按需计算** = 用户**新增**的那个板块 —— 就是本函数

    ⚠️ 与 `compute_all_metrics()` 的区别只在**范围**：
    本函数走 `codes=` 过滤，**不扫全表**（全表 217 万行要 2~3 秒，
    单板块是毫秒级），所以可以在请求里同步等它跑完。

    ⚠️ **不写 `metric_meta`**：那是"整周算过了吗"的台账，
    局部补算不能把整周标记成已完成，否则下一轮周任务会误判跳过。
    """
    out: dict[str, Any] = {"requested": len(codes), "computed": 0,
                           "failed": [], "week": ""}
    wanted = {str(code) for code in codes if str(code or "").strip()}
    if not wanted:
        return out
    config = config or load_config()
    conn = None
    conn_wh = None
    try:
        conn = db.get_db_connection(config)
        db.init_tables(conn)
        conn_wh = _open_warehouse(config)
        changes, as_of = compute_water_changes(
            conn, short_days=config.metrics.short_days,
            month_days=config.metrics.month_trading_days,
            long_days=(config.metrics.month_trading_days
                       * config.metrics.long_month_multiplier),
            min_base_water=config.metrics.min_base_water,
            codes=wanted)
        if not changes:
            return out

        flow_last = _latest_dataset_date(conn_wh, "quant_moneyflow")
        span = max(1, int(config.metrics.month_trading_days))
        calendar: list[str] = []
        flow_base = ""
        if flow_last:
            calendar = trading_days(conn_wh, end=flow_last, limit=span + 1)
            if len(calendar) >= span + 1:
                flow_base = calendar[-1]

        names = {str(row["sector_code"]): str(row["sector_name"] or "")
                 for row in conn.execute(
                     f"SELECT sector_code, sector_name FROM {db.META_TABLE}")}
        week = week_key()
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        payload: list[dict[str, Any]] = []
        for code in sorted(changes):
            item = changes[code]
            try:
                net, mv, _members, member_source = _one_sector_flow(
                    code, config, flow_base, flow_last, span)
            except Exception as exc:  # noqa: BLE001 单板块失败不拖垮请求
                out["failed"].append(code)
                logger.warning("临时算·%s 失败：%s", code, exc)
                continue
            ratio = (round(net / mv * 100.0, 4)
                     if net is not None and mv not in (None, 0) and mv > 0
                     else None)
            payload.append({
                "sector_code": code, "sector_name": names.get(code, ""),
                "compute_week": week, "computed_at": stamp,
                "base_date_5d": item.get("base_date_5d", ""),
                "chg_5d": item.get("chg_5d"),
                "base_date_1m": item.get("base_date_1m", ""),
                "chg_1m": item.get("chg_1m"),
                "base_date_2m": item.get("base_date_2m", ""),
                "chg_2m": item.get("chg_2m"),
                "flow_base_date": flow_base, "flow_last_date": flow_last,
                "net_inflow": net, "circ_mv_base": mv, "flow_ratio": ratio,
                "member_source": member_source,
            })
        if payload:
            out["computed"] = db.upsert_metrics(conn, payload)
        out["week"] = week
        out["as_of"] = as_of
        logger.info("临时算完成：请求 %d，写入 %d，失败 %d",
                    out["requested"], out["computed"], len(out["failed"]))
        return out
    finally:
        if conn is not None:
            conn.close()
        if conn_wh is not None:
            conn_wh.close()


def compute_all_metrics(*, config: SectorCrowdingConfig | None = None,
                        force: bool = False, task_id: str = ""
                        ) -> MetricTask:
    """算一轮全板块周频指标并落库（幂等：同一周已有结果且 `force=False` 就跳过）。

    `force=True`（前端手动按钮 / 接口显式要求）才重算同一周。
    """
    config = config or load_config()
    task = MetricTask(task_id=task_id or f"metric-{int(time.time())}",
                      started_at=datetime.now().astimezone().isoformat(
                          timespec="seconds"),
                      week=week_key())
    _register(task)
    started = time.perf_counter()
    conn = None
    conn_wh = None
    try:
        conn = db.get_db_connection(config)
        db.init_tables(conn)
        existing = db.latest_metric_week(conn)
        if not force and existing == task.week:
            task.status, task.skipped = "done", True
            task.rows_written = len(db.query_metrics(conn, week=task.week))
            return task

        conn_wh = _open_warehouse(config)
        changes, as_of = compute_water_changes(
            conn, short_days=config.metrics.short_days,
            month_days=config.metrics.month_trading_days,
            long_days=(config.metrics.month_trading_days
                       * config.metrics.long_month_multiplier),
            min_base_water=config.metrics.min_base_water)
        task.as_of_date = as_of
        if not changes:
            raise RuntimeError("拥挤度库还没有数据，先做一次「一键刷新」")

        # 资金流末端日：moneyflow 比 daily 落仓更晚，用它自己的最大日期，
        # 并把窗口基准日也定在它的交易日序列上（口径自洽）
        flow_last = _latest_dataset_date(conn_wh, "quant_moneyflow")
        task.flow_last_date = flow_last
        span = max(1, int(config.metrics.month_trading_days))
        calendar: list[str] = []
        flow_base = ""
        if flow_last:
            calendar = trading_days(conn_wh, end=flow_last, limit=span + 1)
            if len(calendar) >= span + 1:
                flow_base = calendar[-1]

        names = {str(row["sector_code"]): str(row["sector_name"] or "")
                 for row in conn.execute(
                     f"SELECT sector_code, sector_name FROM {db.META_TABLE}")}

        codes = _scope_to_pool(conn, sorted(changes),
                               pool_only=config.metrics.pool_only,
                               concepts_only=config.metrics.pool_concepts_only)
        task.total = len(codes)
        logger.info("周频指标开算：%d 个板块，拥挤度截至 %s，资金流窗口 %s~%s",
                    len(codes), as_of, flow_base or "—", flow_last or "—")

        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        workers = max(1, int(config.metrics.member_workers))
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="crowding-metric") as pool:
            futures = {pool.submit(_one_sector_flow, code, config, flow_base,
                                   flow_last, span): code for code in codes}
            pending: list[dict[str, Any]] = []
            # `rows_written` 要累计"落库过的行数"，不能用 len(pending)——
            # pending 每 200 条清空一次，那样最后只会剩下不足 200 的尾巴
            written = 0
            source_tally: dict[str, int] = {}
            for done, future in enumerate(as_completed(futures), start=1):
                code = futures[future]
                payload = changes[code]
                try:
                    net, mv, members, member_source = future.result()
                except Exception as exc:  # noqa: BLE001 单板块失败不影响整轮
                    task.failed_sectors.append(code)
                    logger.warning("周频指标·%s 失败：%s", code, exc)
                    net = mv = None
                    member_source = ""
                if member_source:
                    source_tally[member_source] = source_tally.get(member_source, 0) + 1
                ratio = None
                if net is not None and mv is not None and mv > 0:
                    ratio = round(net / mv * 100.0, 4)
                pending.append({
                    "sector_code": code, "sector_name": names.get(code, ""),
                    "compute_week": task.week, "computed_at": stamp,
                    "base_date_5d": payload.get("base_date_5d", ""),
                    "chg_5d": payload.get("chg_5d"),
                    "base_date_1m": payload.get("base_date_1m", ""),
                    "chg_1m": payload.get("chg_1m"),
                    "base_date_2m": payload.get("base_date_2m", ""),
                    "chg_2m": payload.get("chg_2m"),
                    "flow_base_date": flow_base, "flow_last_date": flow_last,
                    "net_inflow": net, "circ_mv_base": mv, "flow_ratio": ratio,
                    "member_source": member_source,
                })
                task.processed = done
                task.current_sector = names.get(code, code)
                if len(pending) >= 200:
                    written += db.upsert_metrics(conn, pending, commit=True)
                    pending = []
                task.rows_written = written + len(pending)
        if pending:
            written += db.upsert_metrics(conn, pending, commit=True)
        task.rows_written = written

        db.set_metric_meta(conn, {
            "last_week": task.week, "last_computed_at": stamp,
            "as_of_date": as_of, "flow_last_date": flow_last,
            "flow_base_date": flow_base,
            "sectors": str(task.rows_written),
            # 名单来源要留痕：否则无法回答"提纯名单到底有没有生效"
            "purified_sectors": str(source_tally.get("purified", 0)),
            "raw_sectors": str(source_tally.get("raw", 0)),
            "use_purified_members": str(bool(config.metrics.use_purified_members)),
            # 收口开关也要留痕：否则无法回答"这一周到底按哪个范围算的"
            "pool_only": str(bool(config.metrics.pool_only)),
            "pool_concepts_only": str(bool(config.metrics.pool_concepts_only)),
        })
        task.status = "done"
        return task
    except Exception as exc:  # noqa: BLE001 任务级失败要留痕
        task.status = "failed"
        task.error = f"{type(exc).__name__}: {exc}"
        logger.exception("周频指标计算失败")
        return task
    finally:
        if conn is not None:
            conn.close()
        if conn_wh is not None:
            conn_wh.close()
        task.seconds = time.perf_counter() - started
        task.finished_at = datetime.now().astimezone().isoformat(timespec="seconds")


def _one_sector_flow(code: str, config: SectorCrowdingConfig, flow_base: str,
                     flow_last: str, span: int
                     ) -> tuple[float | None, float | None, int, str]:
    """worker：一个板块的资金流（自带连接 —— SQLite 连接不能跨线程共享）。

    返回 `(净流入, 基准日流通市值, 成分股数, 名单来源)`。第四个值是
    `purified` / `raw`，会写进 `sector_crowding_metric.member_source`：
    没有它就无法回答"这套提纯名单到底有没有真的生效"。
    """
    if not flow_base or not flow_last:
        return None, None, 0, ""
    conn = db.get_db_connection(config)
    pure_conn = None
    wh = None
    try:
        pure_conn = open_pure_db(config.mainline_cache_path)
        members, source = _sector_members_cached(conn, code, config=config,
                                                 pure_conn=pure_conn)
        if not members:
            return None, None, 0, source
        wh = _open_warehouse(config)
        # 窗口左端取基准日**前一天**：窗口 = (base, last] 共 span 个交易日，
        # 与"基准日水位 → 最新水位"的跨度严格一致
        prev_days = trading_days(wh, end=flow_base, limit=2)
        window_start = prev_days[-1] if len(prev_days) >= 2 else ""
        if not window_start:
            return None, None, 0, source
        net, mv = _flow_for_codes(wh, members, start=window_start,
                                  end=flow_last, base_date=flow_base)
        return net, mv, len(members), source
    finally:
        if pure_conn is not None:
            pure_conn.close()
        if wh is not None:
            wh.close()
        conn.close()


def start_metric_compute(*, force: bool = False,
                         config: SectorCrowdingConfig | None = None) -> dict[str, Any]:
    """起后台线程算指标，立即返回进度快照（前端轮询口径同「一键刷新」）。

    `force=False` 且同一周已算过时，**在起线程之前就返回 skipped** ——
    否则接口先回一句"计算中… 0/N"，前端白等一轮轮询才发现什么都没做，
    用户会以为按钮坏了。
    """
    config = config or load_config()
    with _TASKS_LOCK:
        running = [item for item in _TASKS.values() if item.status == "running"]
    if running:
        logger.info("已有周频指标任务在执行，复用 %s", running[0].task_id)
        return running[0].to_dict()

    week = week_key()
    if not force:
        try:
            probe = db.get_db_connection(config)
            try:
                existing = db.latest_metric_week(probe)
                count = len(db.query_metrics(probe, week=week)) if existing else 0
            finally:
                probe.close()
            if existing == week:
                return MetricTask(
                    task_id="", status="done", skipped=True, week=week,
                    rows_written=count,
                    started_at=datetime.now().astimezone().isoformat(
                        timespec="seconds"),
                ).to_dict()
        except Exception as exc:  # noqa: BLE001 探测失败就照常算，别把功能挡住
            logger.warning("周频指标跳过判定失败（改为照常计算）：%s", exc)

    if not _RUN_LOCK.acquire(blocking=False):
        return {"task_id": "", "status": "running", "skipped": False,
                "message": "已有计算任务在执行"}
    task = MetricTask(task_id=f"metric-{int(time.time() * 1000) % 10**10}",
                      started_at=datetime.now().astimezone().isoformat(
                          timespec="seconds"),
                      week=week)
    _register(task)

    def _runner() -> None:
        try:
            compute_all_metrics(config=config, force=force,
                                task_id=task.task_id)
        finally:
            _RUN_LOCK.release()

    threading.Thread(target=_runner, name="crowding-metric-refresh",
                     daemon=True).start()
    return task.to_dict()


def metrics_summary(*, config: SectorCrowdingConfig | None = None) -> dict[str, Any]:
    """给接口用的指标元信息（最新周、各基准日、覆盖数量）。"""
    config = config or load_config()
    conn = db.get_db_connection(config)
    try:
        db.init_tables(conn)
        week = db.latest_metric_week(conn)
        meta = db.get_metric_meta(conn)
        rows = list(db.query_metrics(conn, week=week).values()) if week else []
        def _dated(field: str) -> list[str]:
            return [str(row[field]) for row in rows if row.get(field)]
        return {
            "week": week,
            "computed_at": meta.get("last_computed_at", ""),
            "count": len(rows),
            # 窗口口径回传前端：表头提示里的"20 个交易日"必须与实际算法一致，
            # 否则改了 config 之后界面还在说旧口径
            "windows": {
                "short_days": int(config.metrics.short_days),
                "month_days": int(config.metrics.month_trading_days),
                "long_days": int(config.metrics.month_trading_days
                                 * config.metrics.long_month_multiplier),
                "mv_basis": "circ_mv",
            },
            "as_of_date": meta.get("as_of_date", "")
            or (max(_dated("base_date_5d"), default="")),
            "flow_last_date": meta.get("flow_last_date", "")
            or (max(_dated("flow_last_date"), default="")),
            "flow_base_date": meta.get("flow_base_date", ""),
            "coverage": {
                "chg_5d": sum(1 for row in rows if row.get("chg_5d") is not None),
                "chg_1m": sum(1 for row in rows if row.get("chg_1m") is not None),
                "chg_2m": sum(1 for row in rows if row.get("chg_2m") is not None),
                "flow_ratio": sum(1 for row in rows
                                  if row.get("flow_ratio") is not None),
            },
            "progress": get_metric_progress(),
        }
    finally:
        conn.close()


__all__ = [
    "MetricTask",
    "compute_all_metrics",
    "compute_water_changes",
    "get_metric_progress",
    "metrics_summary",
    "start_metric_compute",
    "trading_days",
    "water_level_change",
    "week_key",
]
