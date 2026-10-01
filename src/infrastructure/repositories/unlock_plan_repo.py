"""限售解禁计划的**专用表**（`app_db::unlock_plan`）：逐股一行，按月落库一次。

## 为什么要有这张表（用户 2026-09-29 原话）

> 「可以搞个脚本，按投研分析要查的股票、解禁日期、**解禁数量**落入到数据库
>   **单独的表中**，每月自动调度一次，把投资日历里的解禁数据落入到数据库。
>   然后 agent 数据连接对接这个单独的表，查询**未来一个月**这个股的解禁数据。
>   **直接精准哈希就找到了**，可以本次直接先落入数据库一次。」

换句话说：把"解禁"从**聚合指标 + JSON 列里翻明细**，改成**一张逐股的表 + 索引精确查**。

## 旧路径的三个缺点（实测，不是推测）

1. **明细被截断**：`calendar_store` 把每日明细压进 `extra_json.top_stocks`，
   而且**只留市值最大的 10 只**（`[:10]`）。于是"某只票在某天有没有解禁"这件事
   在旧路径上**答不准** —— 排在第 11 位之后的票会被答成「无解禁计划」，
   而那是**假阴性**（真值：有解禁，只是没进 top10）。
2. **查一次要全表扫**：`cal:unlock:*` 是"按日聚合"的指标，逐股查询只能把
   三段 JSON 全读出来在内存里翻；前缀范围扫描也只是避免退化成 `LIKE` 全扫。
3. **字段不够**：旧明细只有 `code/name/market_cap/pct_of_float/share_type`，
   而源里还有 **`解禁数量`（股）**、`实际解禁数量`、`解禁前一交易日收盘价`、
   前/后 20 日涨跌幅 —— 用户明确要"解禁数量"。

## 本表的判据（每条都对应上面一条缺点）

- **逐股一行**：主键 `(code, unlock_date, share_type)` ⇒ 幂等 upsert，
  同一天同一只票的不同限售类型各占一行；
- **索引精确查**：`idx_unlock_plan_code_date(code, unlock_date)` ——
  "未来一个月这只票的解禁"是一条索引点查（`SEARCH ... USING INDEX`），不是扫 JSON；
- **口径随行下发**：`shares`（解禁数量/股）、`market_cap`（实际解禁市值/元）、
  `pct_of_float`（占解禁前流通市值比例/%）、`share_type`（限售股类型）、
  `certainty`（`rule`=交易所规则确定 / `scheduled`=预约可改）、
  `source` + `fetched_at`（这份数据是什么时候从哪个源落的）；
- **覆盖窗口显式**：`window()` 给出 `min/max(unlock_date)` 与 `fetched_at` ——
  **查询窗口超出覆盖范围时必须说"超出已落库窗口"，不许答"无解禁"**
  （"没量到"与"量到 0"分开，这是本项目反复付代价的那条纪律）。

## 写权限（`CHG-0087` 的口径）

写之前先问 `data_stores.writable_here("app_db")`：`app_db` 是 `per_env + writer: own`
（每个实例各写各的库）⇒ `decided=True, allowed=True`。不可写时**fail-closed 抛错**，
并且错误消息里带上"照抄就能用"的命令（拒绝要给出路）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from src.infrastructure.catalog import data_stores

logger = logging.getLogger(__name__)

#: 存储名（**绝不写路径字面量** —— registry 是唯一事实源）
_STORE = "app_db"
TABLE = "unlock_plan"

#: 缺省落库视野：**未来 400 天**（≈13 个月）。
#: 为什么不是 30 天：用户要查"未来一个月"，而月度作业若只落 30 天，
#: 月初那几天一过就出现"窗口有交集但没数据"的假阴性；一年多留一档更稳。
DEFAULT_HORIZON_DAYS = 400

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    code            TEXT NOT NULL,
    name            TEXT NOT NULL DEFAULT '',
    unlock_date     TEXT NOT NULL,
    shares          REAL,
    actual_shares   REAL,
    market_cap      REAL,
    pct_of_float    REAL,
    close_before    REAL,
    chg_before_20d  REAL,
    chg_after_20d   REAL,
    share_type      TEXT NOT NULL DEFAULT '',
    certainty       TEXT NOT NULL DEFAULT 'rule',
    source          TEXT NOT NULL DEFAULT '',
    fetched_at      TEXT NOT NULL,
    raw_hash        TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (code, unlock_date, share_type)
);
CREATE INDEX IF NOT EXISTS idx_unlock_plan_code_date
    ON {TABLE}(code, unlock_date);
CREATE INDEX IF NOT EXISTS idx_unlock_plan_date
    ON {TABLE}(unlock_date);
"""


def calendar_ready() -> bool:
    """表存在且**有行**（供探针/诊断用；不抛）。"""
    try:
        with connect() as conn:
            return bool(conn.execute(
                f"SELECT 1 FROM {TABLE} LIMIT 1").fetchone())
    except sqlite3.Error:
        return False


def _path() -> str:
    return str(data_stores.resolve_store(_STORE))


def connect() -> sqlite3.Connection:
    """打开 app_db（读写）。**调用方负责**先过写闸门（见 `assert_writable`）。"""
    conn = sqlite3.connect(_path(), timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)      # 幂等建表 + 建索引
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def assert_writable() -> str:
    """写闸门（fail-closed）：不可写就抛**带出路**的错误。"""
    decision = data_stores.writable_here(_STORE)
    if not decision.allowed:
        raise PermissionError(
            f"{_STORE} 在本实例不可写：{decision.reason}。"
            "解禁计划表由**本环境自己的 app_db** 承载（per_env + writer: own），"
            "要在别的环境落库请用该环境的身份跑："
            "`MOSS_ENV=pilot uv run python scripts/unlock_plan.py --ingest`")
    return decision.reason


def _hash_row(row: dict[str, Any]) -> str:
    payload = json.dumps({k: row.get(k) for k in (
        "code", "name", "unlock_date", "shares", "actual_shares", "market_cap",
        "pct_of_float", "close_before", "chg_before_20d", "chg_after_20d",
        "share_type", "certainty", "source")},
        ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _num(raw: Any) -> float | None:
    """任意值 → float；`None`/空/非数字 → None（**不是 0**）。"""
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value:          # NaN
        return None
    return value


def _shares(raw: Any) -> float | None:
    """股数：源是 `np.float64`（实测 `2836860447.0000005` 这种尾差）→ **一律取整**。

    股是**离散量**，任何小数位都是浮点噪声（`akshare` 的整数列走 float64 出来），
    带小数尾巴会让"解禁数量"看起来像估算值。所以不做"小数位很小才取整"这种
    半吊子判定 —— 那会把 `12345678.4` 这种噪声原样落库。
    """
    value = _num(raw)
    return None if value is None else float(round(value))


# ======================================================================
# 写入
# ======================================================================


@dataclass
class IngestResult:
    """一次落库的结果（**每个数字都可复核**）。"""

    rows_in: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    window_start: str = ""
    window_end: str = ""
    source: str = ""
    store: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows_in": self.rows_in, "inserted": self.inserted,
            "updated": self.updated, "unchanged": self.unchanged,
            "window": [self.window_start, self.window_end],
            "source": self.source, "store": self.store,
            "error": self.error or None,
        }

    def render(self) -> str:
        if self.error:
            return f"解禁计划落库失败：{self.error}"
        return (f"解禁计划落库完成：源 {self.rows_in} 行 → 新增 {self.inserted} / "
                f"更新 {self.updated} / 未变 {self.unchanged}；"
                f"覆盖窗口 {self.window_start}~{self.window_end}（{self.store}）")


def upsert(rows: list[dict[str, Any]], *, source: str = "") -> IngestResult:
    """幂等 upsert（同内容重复落库不产生新行、也不刷新 `fetched_at`）。"""
    result = IngestResult(rows_in=len(rows), source=source, store=_STORE)
    if not rows:
        return result
    assert_writable()
    now = datetime.now().isoformat(timespec="seconds")
    dates = sorted({str(r.get("unlock_date") or "") for r in rows if r.get("unlock_date")})
    if dates:
        result.window_start, result.window_end = dates[0], dates[-1]

    with connect() as conn:
        for row in rows:
            code = str(row.get("code") or "").strip()
            day = str(row.get("unlock_date") or "").strip()
            if not code or not day:
                continue                    # 缺主键的行直接跳过（不猜）
            share_type = str(row.get("share_type") or "").strip()
            payload = {
                "code": code, "name": str(row.get("name") or ""),
                "unlock_date": day, "shares": _shares(row.get("shares")),
                "actual_shares": _shares(row.get("actual_shares")),
                "market_cap": _num(row.get("market_cap")),
                "pct_of_float": _num(row.get("pct_of_float")),
                "close_before": _num(row.get("close_before")),
                "chg_before_20d": _num(row.get("chg_before_20d")),
                "chg_after_20d": _num(row.get("chg_after_20d")),
                "share_type": share_type,
                "certainty": str(row.get("certainty") or "rule"),
                "source": source or str(row.get("source") or ""),
            }
            raw_hash = _hash_row(payload)
            existing = conn.execute(
                f"SELECT raw_hash FROM {TABLE} "
                "WHERE code=? AND unlock_date=? AND share_type=?",
                (code, day, share_type)).fetchone()
            if existing is not None and existing["raw_hash"] == raw_hash:
                result.unchanged += 1
                continue                    # 内容没变 → 连 fetched_at 都不动
            conn.execute(
                f"INSERT INTO {TABLE} (code, name, unlock_date, shares,"
                " actual_shares, market_cap, pct_of_float, close_before,"
                " chg_before_20d, chg_after_20d, share_type, certainty,"
                " source, fetched_at, raw_hash) "
                "VALUES (:code, :name, :unlock_date, :shares, :actual_shares,"
                " :market_cap, :pct_of_float, :close_before, :chg_before_20d,"
                " :chg_after_20d, :share_type, :certainty, :source, :fetched_at,"
                " :raw_hash) "
                "ON CONFLICT(code, unlock_date, share_type) DO UPDATE SET "
                " name=excluded.name, shares=excluded.shares,"
                " actual_shares=excluded.actual_shares,"
                " market_cap=excluded.market_cap,"
                " pct_of_float=excluded.pct_of_float,"
                " close_before=excluded.close_before,"
                " chg_before_20d=excluded.chg_before_20d,"
                " chg_after_20d=excluded.chg_after_20d,"
                " certainty=excluded.certainty, source=excluded.source,"
                " fetched_at=excluded.fetched_at, raw_hash=excluded.raw_hash",
                {**payload, "fetched_at": now, "raw_hash": raw_hash})
            if existing is None:
                result.inserted += 1
            else:
                result.updated += 1
        conn.commit()
    logger.info(result.render())
    return result


def ingest(*, horizon_days: int = DEFAULT_HORIZON_DAYS,
           today: date | None = None) -> IngestResult:
    """把"投资日历"的解禁明细落到本表（月度作业与手工脚本**共用这一份实现**）。

    数据源与投资日历**完全同源**（`domain/intel/calendar.py::fetch_unlock_schedule`，
    主源东财 `stock_restricted_release_detail_em`，按个股给明细），
    所以表里的数与日历里看到的是同一个口径。
    """
    from src.domain.intel.calendar import fetch_unlock_schedule

    result = IngestResult(source="unlock_em", store=_STORE)
    try:
        events, tried = fetch_unlock_schedule(horizon_days=horizon_days)
    except Exception as exc:  # noqa: BLE001 取数失败要如实返回，不抛给调度器
        result.error = f"取数失败（源尝试过：{tried if 'tried' in dir() else '—'}）：{exc}"
        logger.warning("解禁计划落库：%s", result.error)
        return result

    rows: list[dict[str, Any]] = []
    for event in events:
        scope = getattr(event, "scope", None) or {}
        day = str(getattr(event, "date", "") or "")
        for stock in (scope.get("stocks") or []):
            if not stock.get("code"):
                continue
            ratio = _num(stock.get("pct_of_float"))
            rows.append({
                "code": str(stock.get("code")).strip(),
                "name": str(stock.get("name") or ""),
                "unlock_date": day,
                "shares": _shares(stock.get("shares")),
                "actual_shares": _shares(stock.get("actual_shares")),
                "market_cap": _num(stock.get("market_cap")),
                "pct_of_float": ratio,
                "close_before": _num(stock.get("close_before")),
                "chg_before_20d": _num(stock.get("chg_before_20d")),
                "chg_after_20d": _num(stock.get("chg_after_20d")),
                "share_type": str(stock.get("share_type") or ""),
                "certainty": str(getattr(event, "certainty", "") or "rule"),
            })
    if not rows:
        result.error = "源返回 0 条明细（网络/接口问题，或视野内确实没有解禁）"
        result.rows_in = 0
        return result
    saved = upsert(rows, source="unlock_em")
    saved.error = result.error
    return saved


# ======================================================================
# 读取（连接器的唯一入口）
# ======================================================================


def query(code: str, start: str, end: str) -> list[dict[str, Any]]:
    """窗口内该股的解禁计划（走 `idx_unlock_plan_code_date`，**索引点查**）。"""
    code = str(code or "").strip()
    if not code:
        return []
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM {TABLE} WHERE code=? AND unlock_date>=? "
            "AND unlock_date<=? ORDER BY unlock_date ASC",
            (code, start, end)).fetchall()
    return [dict(r) for r in rows]


def query_plan(sql: str, params: tuple = ()) -> list[str]:
    """`EXPLAIN QUERY PLAN` 的 detail 列表（护栏用它证明"是 SEARCH 不是 SCAN"）。

    ⚠️ 传**真实查询语句**即可，`EXPLAIN QUERY PLAN` 由本函数加 —— 第一版把前缀
    留给调用方，结果探针传了裸 SELECT，`r["detail"]` 取不到列、又被"0 行"掩盖，
    看起来像"计划是空的"（自证型假绿：判据在**没有真的执行 EXPLAIN** 时也返回空表）。
    """
    with connect() as conn:
        rows = conn.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
    return [str(r["detail"]) for r in rows]


def window() -> dict[str, Any]:
    """本表**已落库的覆盖窗口**与新鲜度（"超出窗口"要靠它说清）。"""
    with connect() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS n, MIN(unlock_date) AS lo, MAX(unlock_date) AS hi,"
            f" MAX(fetched_at) AS fetched_at FROM {TABLE}").fetchone()
    return {"rows": int(row["n"] or 0), "start": row["lo"] or "",
            "end": row["hi"] or "", "fetched_at": row["fetched_at"] or "",
            "store": _STORE}


def expected_horizon_end(*, today: date | None = None,
                         horizon_days: int = DEFAULT_HORIZON_DAYS) -> str:
    """作业声明的视野右端（判断"这次查询有没有超出已落库窗口"用）。"""
    base = today or date.today()
    return (base + timedelta(days=horizon_days)).isoformat()
