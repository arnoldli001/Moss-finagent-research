"""主线挖掘：评分 / 告警 / 回测 / 期货校准的 SQLite 持久化。

## 落哪个库

写 `settings.sqlite_path`（默认 `data/moss_finagent.db`，几 MB 的结果库），
**绝不写 `data/quant/warehouse.db`**：那是 15 GiB 只读行情仓，两者备份与清理
策略完全不同，混在一起会出现"清缓存顺手删掉 15 GiB 数据、要重下载一遍"。
范式与 `dim_fund_flow_watch` 同：原生 sqlite3（不用 ORM —— 读写都是整行进出）、
`CREATE TABLE IF NOT EXISTS`、`_ready` 标志、表名一律 `mainline_` 前缀防撞。

## 为什么 WAL + busy_timeout + 幂等 upsert

同一个库在服务进程里还有别的写入者（采集、告警链），实测并发
`CREATE TABLE IF NOT EXISTS` 会撞 "database is locked"，因此：① 建表只在首次
用之前做一次（`_ready` 短路），不在每次查询里重复 DDL；② WAL 让读写不互斥；
③ `busy_timeout=15000` 让并发写排队而非立刻报错。
写入全部 `INSERT ... ON CONFLICT(主键) DO UPDATE`：一个交易日必然被重复跑
（手动刷新、断点续跑、复盘重算），追加式写入会把这些日子变成 N 份重复行，
而"今天主线是谁"的排序榜一旦有重复板块就彻底不可信。

## 为什么读降级成空而不是抛错

本模块挂在 UI 面板后面：库损坏、表被删，都不该让整页 500 —— 用户看到
"暂无数据 + 一行警告日志"就够了，所以所有 `load_*` 捕获 `sqlite3.Error` 后
用 `brief(exc, BRIEF_TIGHT)` 记警告并返回空列表/空字典。
**代价必须说清**：读不到数据与"当日真的没有信号"在界面上长得一模一样，
调用方要把"存储不可用"与"当日无信号"分开提示。

## 为什么回读是 dict 不是 dataclass

写入时同时存**全量 JSON 载荷**（`payload` = `to_dict()`）与几列用于排序/过滤的
**提取列**（日期、板块、等级、分数）；回读也按这个分法返回：`payload` 反序列化成
dict，外加提取列。刻意**不做 dataclass 反水化**：`BoardScore` / `AlertSignal`
每加一个字段，逐字段反水化代码都要跟着改，改漏一个就是静默丢数据，而面板要的
只是 `to_dict()` 的形状。代价：回读结果里 `level` 是字符串而非 `SignalLevel`，
调用方按字符串比较（`SignalLevel.STRONG.value`）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from src.core.errors import BRIEF_TIGHT, brief
from src.mainline.etf_flow import FlowSignal
from src.mainline.models import (
    AlertSignal,
    BacktestMetrics,
    BacktestReport,
    BoardScore,
    FutureSignal,
)

logger = logging.getLogger(__name__)

#: 表名（统一 `mainline_` 前缀；短别名只为让下面的 SQL 保持可读）
SCORE_TABLE, SCORE = "mainline_score", "mainline_score s"
ALERT_TABLE, ALERT = "mainline_alert", "mainline_alert a"
BACKTEST_TABLE, BACKTEST = "mainline_backtest", "mainline_backtest b"
FUTURE_TABLE, FUTURE = "mainline_future_signal", "mainline_future_signal f"
CALIBRATION_TABLE = "mainline_mapping_calibration"
#: ETF 份额监控的信号与回测（同属主线挖掘页签，共用这个库）
ETF_SIGNAL_TABLE, ETF_SIGNAL = "mainline_etf_flow_signal", "mainline_etf_flow_signal e"
ETF_BACKTEST_TABLE = "mainline_etf_flow_backtest"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {SCORE_TABLE} (
    trade_date TEXT NOT NULL, board_code TEXT NOT NULL,
    board_name TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT '',
    total REAL NOT NULL DEFAULT 0, base_total REAL NOT NULL DEFAULT 0,
    six_dim REAL NOT NULL DEFAULT 0, accumulation REAL NOT NULL DEFAULT 0,
    leader REAL NOT NULL DEFAULT 0, gate_bonus REAL NOT NULL DEFAULT 0,
    rank INTEGER NOT NULL DEFAULT 0, candidate INTEGER NOT NULL DEFAULT 0,
    selected INTEGER NOT NULL DEFAULT 0,
    level TEXT NOT NULL DEFAULT 'none', weight_mode TEXT NOT NULL DEFAULT 'static',
    payload TEXT NOT NULL DEFAULT '{{}}', updated_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (trade_date, board_code)
);
CREATE INDEX IF NOT EXISTS idx_mainline_score_rank ON {SCORE_TABLE}(trade_date, total DESC);
CREATE TABLE IF NOT EXISTS {ALERT_TABLE} (
    alert_id TEXT PRIMARY KEY, trade_date TEXT NOT NULL DEFAULT '',
    board_code TEXT NOT NULL DEFAULT '', board_name TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT '', level TEXT NOT NULL DEFAULT 'none',
    score REAL NOT NULL DEFAULT 0, gate_bonus REAL NOT NULL DEFAULT 0,
    resonance REAL NOT NULL DEFAULT 0,
    entry_close REAL, max_gain_pct REAL, max_gain_date TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT '{{}}', created_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_mainline_alert_date ON {ALERT_TABLE}(trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_mainline_alert_board ON {ALERT_TABLE}(board_code, trade_date);
CREATE TABLE IF NOT EXISTS {BACKTEST_TABLE} (
    run_id TEXT PRIMARY KEY, range_start TEXT NOT NULL DEFAULT '',
    range_end TEXT NOT NULL DEFAULT '', started_at TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT '', seconds REAL NOT NULL DEFAULT 0,
    metrics TEXT NOT NULL DEFAULT '{{}}', folds TEXT NOT NULL DEFAULT '[]',
    scenes TEXT NOT NULL DEFAULT '[]', correlation TEXT NOT NULL DEFAULT '{{}}',
    markdown TEXT NOT NULL DEFAULT '',
    gaps TEXT NOT NULL DEFAULT '[]', error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS {FUTURE_TABLE} (
    trade_date TEXT NOT NULL, code TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT '',
    intensity REAL NOT NULL DEFAULT 0, level TEXT NOT NULL DEFAULT 'none',
    payload TEXT NOT NULL DEFAULT '{{}}', updated_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (trade_date, code)
);
CREATE TABLE IF NOT EXISTS {CALIBRATION_TABLE} (
    future_code TEXT PRIMARY KEY, future_name TEXT NOT NULL DEFAULT '',
    board_name TEXT NOT NULL DEFAULT '', strength INTEGER NOT NULL DEFAULT 0,
    calibrated_strength REAL, correlation REAL,
    calibrated_at TEXT NOT NULL DEFAULT '', window_days INTEGER NOT NULL DEFAULT 0
);
-- ETF 份额监控：`alert_id` = 日期+ETF代码+类型，天然幂等（同一天同一只ETF
-- 只会有一条机会信号）。提取 gated / regime 两列是因为它们是**过滤条件**：
-- 面板要能只看"会真正告警的"（gated=0 且 level in (strong,medium)），
-- 把它塞进 payload 就得把全表读出来再在内存里筛。
CREATE TABLE IF NOT EXISTS {ETF_SIGNAL_TABLE} (
    alert_id TEXT PRIMARY KEY, trade_date TEXT NOT NULL DEFAULT '',
    code TEXT NOT NULL DEFAULT '', name TEXT NOT NULL DEFAULT '',
    grp TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT '',
    level TEXT NOT NULL DEFAULT 'none', regime TEXT NOT NULL DEFAULT 'range',
    gated INTEGER NOT NULL DEFAULT 0,
    index_percentile REAL, change_1d REAL, change_5d REAL,
    resonance INTEGER NOT NULL DEFAULT 0,
    payload TEXT NOT NULL DEFAULT '{{}}', updated_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_etf_flow_date ON {ETF_SIGNAL_TABLE}(trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_etf_flow_code ON {ETF_SIGNAL_TABLE}(code, trade_date);
-- 回测正文与结构化结果分列存放：列表接口不取 markdown（单页几十 KB），
-- 与 mainline_backtest 同一处理。
CREATE TABLE IF NOT EXISTS {ETF_BACKTEST_TABLE} (
    run_id TEXT PRIMARY KEY, range_start TEXT NOT NULL DEFAULT '',
    range_end TEXT NOT NULL DEFAULT '', started_at TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT '', seconds REAL NOT NULL DEFAULT 0,
    days INTEGER NOT NULL DEFAULT 0, signals INTEGER NOT NULL DEFAULT 0,
    result TEXT NOT NULL DEFAULT '{{}}', markdown TEXT NOT NULL DEFAULT '',
    gaps TEXT NOT NULL DEFAULT '[]', error TEXT NOT NULL DEFAULT ''
);
"""

#: V2.0 重命名/新增的列：`(表, 列, 类型)`。库是**派生缓存**（可从数据源重算），
#: 因此发现旧版结构时直接重建而不是写迁移脚本 —— 迁移脚本要维护两套口径，
#: 而这里的代价只是"重跑一次打分"。
_V2_COLUMNS: tuple[tuple[str, str, str], ...] = (
    (SCORE_TABLE, "accumulation", "REAL NOT NULL DEFAULT 0"),
    (SCORE_TABLE, "base_total", "REAL NOT NULL DEFAULT 0"),
    (SCORE_TABLE, "gate_bonus", "REAL NOT NULL DEFAULT 0"),
    (SCORE_TABLE, "candidate", "INTEGER NOT NULL DEFAULT 0"),
    (SCORE_TABLE, "selected", "INTEGER NOT NULL DEFAULT 0"),
    (SCORE_TABLE, "rank", "INTEGER NOT NULL DEFAULT 0"),
    (ALERT_TABLE, "gate_bonus", "REAL NOT NULL DEFAULT 0"),
    (BACKTEST_TABLE, "correlation", "TEXT NOT NULL DEFAULT '{}'"),
)

#: `list_backtests` 只取这些列（**不含 markdown**，见该方法说明）
_SUMMARY = ("run_id, range_start, range_end, started_at, finished_at, seconds, "
            "metrics, gaps, error")


# ==================================================================
# 小工具
# ==================================================================


def _now() -> str:
    """本地时区 ISO 时间戳（与其它仓储同格式，便于人工比对）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _dumps(value: Any) -> str:
    """序列化 JSON。`default=str` 是兜底不是设计：宁可把 datetime 存成字符串，
    也不要因为一个字段让整批写入失败。"""
    return json.dumps(value, ensure_ascii=False, default=str)


def _loads(raw: Any, default: Any = None) -> Any:
    """反序列化一列 JSON；空值/坏值返回 default（不抛错）。"""
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _payload(row: sqlite3.Row) -> dict[str, Any]:
    """取一行的 `payload` JSON **对象**；不是对象就抛 ValueError，由逐行循环
    捕获 → 跳过该行。这样坏行只牺牲一行，不会把整页读成空。"""
    value = _loads(row["payload"], None)
    if not isinstance(value, dict):
        raise ValueError("payload 缺失或不是 JSON 对象")
    return value


def _num(value: Any, default: float = 0.0) -> float:
    """转 float；None / 不可转的值回落默认（REAL 列不接 None）。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _val(value: Any, default: str = "") -> str:
    """枚举或字符串统一取 `.value`（写入时是枚举，回读时是字符串）。

    ⚠️ 必须显式判 Enum：`str(SignalLevel.STRONG)` 得到的是
    ``'SignalLevel.STRONG'``（`Enum.__str__` 返回限定名），
    直接用 `str()` 会把等级列写成一串查不回来的垃圾（枚举过滤失效就此而来）。
    """
    if isinstance(value, Enum):
        return str(value.value)
    if value is None:
        return default
    text = getattr(value, "value", None)
    return text if isinstance(text, str) and text else (str(value) or default)


def _short(exc: BaseException) -> str:
    """异常短描述（`brief` 统一脱敏 + 截断）。"""
    return brief(exc, BRIEF_TIGHT)


def _levels(level: Any) -> list[str]:
    """把 level 归一成等级值列表；空/无效输入返回空（= 不过滤）。

    接受 ``""``（全部）、`SignalLevel.STRONG` 这类枚举、``"strong,medium"``
    逗号列表、以及列表/元组（面板多选）。

    ⚠️ 枚举必须先于"空值判断"处理：`SignalLevel` 继承 `str` 且
    `SignalLevel.NONE.value == ""`，所以 `not SignalLevel.NONE` 为真 ——
    按普通真值判断会把一个合法等级当成"不过滤"，`level=NONE` 就静默失效。
    """
    if isinstance(level, Enum):
        return [str(level.value)] if _val(level) else []
    if not level:
        return []
    source = level if isinstance(level, (list, tuple, set)) else str(level).split(",")
    seen: list[str] = []
    for item in source:
        text = _val(item).strip()
        if text and text not in seen:
            seen.append(text)
    return seen


def _where(board_code: str = "", start: str = "", end: str = "",
           levels: list[str] | None = None) -> tuple[str, list[Any]]:
    """拼参数化 `WHERE` 片段：板块 / 日期区间（含 start、**不含** end）/ 等级。

    统一构造函数而不是各处字符串拼接：等级条件曾因为直接拼在空区间片段后面
    产生过裸 `AND` 的语法错误（验证脚本一跑就暴露了）。
    """
    parts: list[str] = []
    params: list[Any] = []
    if board_code:
        parts.append("board_code = ?")
        params.append(board_code)
    if start:
        parts.append("trade_date >= ?")
        params.append(str(start))
    if end:
        parts.append("trade_date < ?")
        params.append(str(end))
    if levels:
        parts.append(f"level IN ({','.join('?' * len(levels))})")
        params.extend(levels)
    return (" WHERE " + " AND ".join(parts) if parts else ""), params


def _metrics(raw: Any) -> dict[str, Any]:
    """回读指标：用 `BacktestMetrics` 补齐全部键，面板不必写"字段不存在"分支。"""
    value = _loads(raw, None) if isinstance(raw, str) else raw
    if isinstance(value, dict) and value:
        try:
            return BacktestMetrics(**value).to_dict()
        except TypeError:
            return value
    return BacktestMetrics().to_dict()


def _json_list(row: sqlite3.Row, column: str) -> list[Any]:
    """读一个 JSON 数组列；坏值返回空列表（调用方不必再判类型）。"""
    value = _loads(row[column], None)
    return value if isinstance(value, list) else []


def _correlation(raw: Any) -> dict[str, Any] | None:
    """回读相关性验证：**空内容一律归一成 `None`**，否则补齐成完整结构。

    为什么必须归一：这一列落库时写的是 `{}`（没有做相关性验证时
    `FactorCorrelation().to_dict()` 之前的分支就是空字典），而 `{}` 在
    JavaScript 里是**真值**。前端写的是

        const correlation = report.correlation ?? null   // {} 逃过 ?? 兜底
        correlation.factors.length                       // undefined.length 💥

    于是"这次回测没算相关性"会让整个「回测报告」页签白屏，报
    `TypeError: Cannot read properties of undefined (reading 'length')`
    —— 报错位置在 `.length`，根因却在"空对象被当成了有内容"。
    这是取巧的类型契约，不是一个字段的显示问题，所以在**边界**上修：
    有内容 → 补齐全部键；没内容 → `None`（前端已有 `null` 分支）。
    """
    value = _loads(raw, None) if isinstance(raw, str) else raw
    if not isinstance(value, dict):
        return None
    factors = value.get("factors")
    matrix = value.get("matrix")
    high_pairs = value.get("high_pairs")
    if not factors and not matrix and not high_pairs:
        return None
    return {
        "factors": factors if isinstance(factors, list) else [],
        "matrix": matrix if isinstance(matrix, dict) else {},
        "high_pairs": high_pairs if isinstance(high_pairs, list) else [],
        "cross_layer_mean_abs": value.get("cross_layer_mean_abs"),
        "cross_layer_max_abs": value.get("cross_layer_max_abs"),
        "limit": value.get("limit"),
        "samples": value.get("samples"),
        "passed": bool(value.get("passed")),
        "note": str(value.get("note") or ""),
    }


#: 回测载荷的全部键与"空值应该长什么样"。放在模块级是为了让**两条返回路径**
#: （落库回读 / "还没有回测记录"的空壳）共用同一份契约 —— 早先空壳分支手写了
#: `"correlation": {}`，和落库分支各自维护，于是同一个类型 bug 修一处漏一处。
_BACKTEST_DEFAULTS: dict[str, Any] = {
    "run_id": "", "started_at": "", "finished_at": "", "seconds": None,
    "range_start": "", "range_end": "", "config_note": "",
    "metrics": {}, "folds": [], "scenes": [], "signals": [],
    "false_positives": [], "correlation": None, "markdown": "",
    "gaps": [], "error": "", "disclaimer": "",
}


def normalize_backtest(data: Any) -> dict[str, Any]:
    """把一份回测载荷补成前端契约的形状（**所有接口返回都必须过这里**）。

    修的是"类型契约"而不是某个字段：数组键保证是数组（`.length`/`.map`
    不会炸）、`correlation` 保证是完整对象或 `None`（不会是 `{}`）、
    标量键保证存在。这样前端就不必为"后端这次少给了哪个键"写分支。
    """
    source = data if isinstance(data, dict) else {}
    out = dict(_BACKTEST_DEFAULTS)
    out.update({key: value for key, value in source.items()
                if key in _BACKTEST_DEFAULTS})
    for key in ("folds", "scenes", "signals", "false_positives", "gaps"):
        value = out.get(key)
        if isinstance(value, list):
            out[key] = value
        elif isinstance(value, str) and value.strip():
            # 单独一条字符串（如 `"gaps": "还没有回测记录"`）包成一项，
            # **不要**当成坏值丢掉：那会把唯一的解释性文案静默吞掉。
            out[key] = [value]
        else:
            out[key] = []
    metrics = out.get("metrics")
    out["metrics"] = metrics if isinstance(metrics, dict) else {}
    out["correlation"] = _correlation(out.get("correlation"))
    for key in ("run_id", "started_at", "finished_at", "range_start",
                "range_end", "config_note", "markdown", "error", "disclaimer"):
        value = out.get(key)
        out[key] = "" if value is None else str(value)
    seconds = out.get("seconds")
    out["seconds"] = seconds if isinstance(seconds, (int, float)) else None
    return out


# ==================================================================
# 仓储
# ==================================================================


@dataclass
class MainlineRepository:
    """主线挖掘结果的 SQLite 仓储（`path` 为偏好库文件路径）。

    公开方法一律 async；阻塞的 sqlite3 调用全部走 `asyncio.to_thread`（同步驱动
    在事件循环里跑会把整个服务的请求一起卡住，写大 JSON 的回测行尤其明显）。
    写操作拿 `_lock` 串行化，读不加锁（WAL 下读不阻塞写）。
    """

    path: str
    _ready: bool = field(default=False, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    # ---------- 连接与建表 ----------

    def score_total_history_sync(self, end: str, *,
                                 lookback_days: int) -> dict[str, list[float]]:
        """每个板块在 `end` **之前**的 `total` 历史：`{code: [total, ...]}`。

        ⚠️ 两个容易搞错的地方：

        1. **必须读结果库**（本仓储），不是 `MainlineDataStore`（那是
           `mainline_cache.db`，里面**没有** `mainline_score` 表）。
           第一版就是放错了对象，运行时报 "no such table: mainline_score"，
           而调用方把异常吞成"历史为空"，于是突破触发**静默失效**。
        2. **严格 PIT**：只取 `trade_date < end`。含当日就是未来信息 ——
           回测会虚高，实盘拿不到。

        用途是"越过自己的长期震荡上沿"这条触发路径
        （见 `AlertRuleConfig.breakout_enabled`）。
        """
        if not end or lookback_days <= 0:
            return {}
        with self._connect() as connection:
            days = [str(row[0]) for row in connection.execute(
                "SELECT DISTINCT trade_date FROM mainline_score"
                " WHERE trade_date < ? ORDER BY trade_date DESC LIMIT ?",
                (end, int(lookback_days)))]
            if len(days) < 5:
                return {}
            # ⚠️ **连续性闸门**：表里最近的一天必须紧挨着 `end`。
            #
            # 为什么需要：这个查询只取"存在的数据里最近的 N 天"。在**正常
            # 重打分**里表是按时序写入的，所以"最近 N 天"就是紧邻的前 N 天，
            # 正确。但表处于**部分写入**状态时（重打分进行中、或上一轮被中断），
            # `end` 之后的日期还没有数据，查询会悄悄回退到**更早的一段**，
            # 于是上沿是用错误时期算出来的 —— 而它**看起来完全正常**。
            # 实测：V2.5 重打分跑到 2024-06 时问 `20260701` 的突破，
            # 拿到的"前 120 天"其实是 2023-10~2024-06，上沿被冻在同一个值。
            #
            # 判据用**自然日**差：交易日历不在本仓储里，而自然日差已经足够
            # 区分"隔了个周末/长假"（≤15 天）与"表落后几个月"。
            # 宁可不判（这条路径失效），也不要拿错时期的上沿去报。
            latest = connection.execute(
                "SELECT MAX(trade_date) AS d FROM mainline_score"
                " WHERE trade_date < ?", (end,)).fetchone()["d"]
            if not latest:
                return {}
            try:
                gap_days = (datetime.strptime(end, "%Y%m%d")
                            - datetime.strptime(str(latest), "%Y%m%d")).days
            except ValueError:
                return {}
            if gap_days > 15:
                return {}
            start = min(days)
            rows = connection.execute(
                "SELECT board_code, total FROM mainline_score"
                " WHERE trade_date >= ? AND trade_date < ?"
                " ORDER BY board_code, trade_date", (start, end)).fetchall()
        out: dict[str, list[float]] = {}
        for row in rows:
            out.setdefault(str(row["board_code"]), []).append(
                float(row["total"] or 0.0))
        return out

    def prosperity_raw_history_sync(self, end: str, *, lookback_days: int
                                    ) -> dict[str, dict[str, float]]:
        """`{板块: {景气度原始值}}`，取 `lookback_days` 个交易日**之前**那一天。

        用途：景气度的「增速变化率」口径要算 `Δg = g(t) − g(t−W)`，而
        `BoardInput.member_stats` 只有**当日**的基本面，拿不到历史
        （见 `six_dim.score_six_dim` 的 `history_raws`）。

        ⚠️ 数据来源是**历史评分行的 payload** —— `raw.roe_yoy / profit_yoy /
        revenue_yoy` 每个交易日都写在里面，所以不必新建历史表。
        与 `score_total_history_sync` 同一套路，也就同样要守两条规矩：

        1. **必须读结果库**（本仓储），不是 `MainlineDataStore`；
        2. **严格 PIT**：只取 `trade_date < end`，区间内第 `W` 个交易日。

        取不到（历史不足、重打分刚开始）就返回空字典，调用方回退到水平值口径。
        """
        if not end or lookback_days <= 0:
            return {}
        with self._connect() as connection:
            # 取到 `t−1 … t−W` 共 W 天（`LIMIT W+1` 是为了能判断"够不够 W 天"）。
            days = [str(row[0]) for row in connection.execute(
                "SELECT DISTINCT trade_date FROM mainline_score"
                " WHERE trade_date < ? ORDER BY trade_date DESC LIMIT ?",
                (end, int(lookback_days) + 1))]
            if len(days) < lookback_days:
                return {}
            # ⚠️ 下标是 `W−1` 不是 `W`：`days[0]` 已经是 `t−1` 了。
            # 第一版写成 `days[W]`，实际取到的是 `t−W−1` —— 差一天，
            # 而"差一天"在 Δg 里几乎看不出来，靠单测的精确断言才抓到。
            target = days[lookback_days - 1]
            rows = connection.execute(
                "SELECT board_code, payload FROM mainline_score"
                " WHERE trade_date = ?", (target,)).fetchall()
        out: dict[str, dict[str, float]] = {}
        for row in rows:
            try:
                payload = json.loads(str(row["payload"] or "{}"))
            except ValueError:
                continue
            raws: dict[str, float] = {}
            for layer in (payload.get("layers") or []):
                if str(layer.get("key")) != "six_dim":
                    continue
                for dim in (layer.get("dimensions") or []):
                    if str(dim.get("key")) != "prosperity":
                        continue
                    detail = dim.get("raw") or {}
                    for sub in ("roe_yoy", "profit_yoy", "revenue_yoy"):
                        value = detail.get(sub)
                        if isinstance(value, (int, float)):
                            raws[sub] = float(value)
            if raws:
                out[str(row["board_code"])] = raws
        return out

    def _connect(self) -> sqlite3.Connection:
        # timeout + WAL + busy_timeout 三者一起决定"与服务进程里其它 SQLite
        # 写入者共存"的能力：实测并发建表会撞 "database is locked"。
        connection = sqlite3.connect(self.path, timeout=15.0)
        connection.row_factory = sqlite3.Row
        with contextlib.suppress(sqlite3.Error):
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=15000")
        return connection

    def _ensure_schema_sync(self) -> None:
        """建表（同步；只由 `_ensure_schema` 调用，且只跑一次）。"""
        if not self.path:
            raise ValueError("主线挖掘存储：路径为空，无法建表")
        # 首次运行时 data/ 可能不存在 —— sqlite3.connect 不会替我们建目录。
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with self._connect() as connection:
            self._upgrade(connection)
            connection.executescript(_SCHEMA)
        self._ready = True

    @staticmethod
    def _upgrade(connection: sqlite3.Connection) -> None:
        """把 V1.0 结构就地升级到 V2.0（加列；重命名的列直接重建表）。

        `CREATE TABLE IF NOT EXISTS` 对**已存在的旧表**什么都不做，于是
        V1.0 建过的库在 V2.0 代码下会一直撞 "no such column: accumulation"。
        这里显式处理：能 `ALTER TABLE ADD COLUMN` 的就加，
        `five_dim → accumulation` 这种重命名没法加，直接把该表删了重建 ——
        它是派生缓存，重跑一轮打分就能补回来。
        """
        try:
            existing = {
                str(row[0]) for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
            if SCORE_TABLE in existing:
                columns = {str(row[1]) for row in connection.execute(
                    f"PRAGMA table_info({SCORE_TABLE})")}
                if "accumulation" not in columns and "five_dim" in columns:
                    logger.warning("主线挖掘存储：检测到 V1.0 评分表结构，重建为 V2.0")
                    connection.execute(f"DROP TABLE {SCORE_TABLE}")
                    existing.discard(SCORE_TABLE)
            for table, column, ddl in _V2_COLUMNS:
                if table not in existing:
                    continue
                columns = {str(row[1]) for row in connection.execute(
                    f"PRAGMA table_info({table})")}
                if column not in columns:
                    connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        except sqlite3.Error as exc:  # 升级失败不阻断建表（executescript 会兜底）
            logger.warning("主线挖掘存储：结构升级失败（继续建表）：%s",
                           _short(exc))

    async def _ensure_schema(self) -> None:
        """`_ready` 双检：建表在首次用之前做一次，之后走内存标志短路。

        刻意**不**在每个 `_*_sync` 里再调一次：那会与 `asyncio.Lock` 叠加成
        重入死锁。统一由公开方法在拿锁之前调一次，语义相同且无重入问题。
        """
        if self._ready:
            return
        async with self._lock:
            if not self._ready:
                await asyncio.to_thread(self._ensure_schema_sync)

    async def ensure_schema(self) -> None:
        """确保全部表与索引存在（幂等，可在 API lifespan 里直接 await）。"""
        await self._ensure_schema()

    async def _write(self, fn: Any, *args: Any) -> Any:
        """所有写路径的唯一入口：建表 → 拿写锁 → 在线程里执行 `fn`。"""
        await self._ensure_schema()
        async with self._lock:
            return await asyncio.to_thread(fn, *args)

    def _read_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(sql, params).fetchone()

    def _read(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return connection.execute(sql, params).fetchall()

    # ---------- 评分 ----------

    def _save_scores_sync(self, trade_date: str, scores: list[BoardScore]) -> int:
        now = _now()
        rows = [(str(item.trade_date or trade_date), str(item.code),
                 str(item.name or ""), _val(item.kind), _num(item.total),
                 _num(item.base_total), _num(item.six_dim.score),
                 _num(item.accumulation.score), _num(item.leader.score),
                 _num(item.gate_bonus), int(item.rank or 0),
                 1 if item.candidate else 0, 1 if item.selected else 0,
                 _val(item.level, "none"),
                 str(item.weight_mode or "static"), _dumps(item.to_dict()), now)
                for item in scores if str(getattr(item, "code", ""))]
        if not rows:
            return 0
        with self._connect() as connection:
            connection.executemany(
                f"INSERT INTO {SCORE_TABLE}(trade_date, board_code, board_name, kind,"
                " total, base_total, six_dim, accumulation, leader, gate_bonus,"
                " rank, candidate, selected, level, weight_mode, payload,"
                " updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(trade_date, board_code) DO UPDATE SET"
                " board_name=excluded.board_name, kind=excluded.kind,"
                " total=excluded.total, base_total=excluded.base_total,"
                " six_dim=excluded.six_dim, accumulation=excluded.accumulation,"
                " leader=excluded.leader, gate_bonus=excluded.gate_bonus,"
                " rank=excluded.rank, candidate=excluded.candidate,"
                " selected=excluded.selected, level=excluded.level,"
                " weight_mode=excluded.weight_mode,"
                " payload=excluded.payload, updated_at=excluded.updated_at", rows)
        return len(rows)

    async def save_scores(self, trade_date: str, scores: list[BoardScore]) -> int:
        """整日评分快照 upsert（键 `(trade_date, board_code)`），返回写入行数。

        `trade_date` 参数是**兜底**：对象自带日期优先，缺了才用它 —— 打分层
        偶尔会构造出没填日期的对象，用调用方的日期比往库里写空串好。
        """
        return await self._write(self._save_scores_sync, trade_date, scores)

    def _scores_sync(self, trade_date: str, limit: int) -> list[sqlite3.Row]:
        return self._read(f"SELECT * FROM {SCORE} WHERE trade_date = ?"
                          " ORDER BY total DESC, board_code LIMIT ?",
                          (trade_date, max(int(limit), 1)))

    def _score_rows(self, rows: list[sqlite3.Row]) -> list[dict]:
        out: list[dict] = []
        for row in rows:
            try:
                out.append({
                    "trade_date": str(row["trade_date"] or ""),
                    "board_code": str(row["board_code"] or ""),
                    "board_name": str(row["board_name"] or ""),
                    "kind": str(row["kind"] or ""), "total": _num(row["total"]),
                    "base_total": _num(row["base_total"]),
                    "six_dim": _num(row["six_dim"]),
                    "accumulation": _num(row["accumulation"]),
                    "leader": _num(row["leader"]),
                    "gate_bonus": _num(row["gate_bonus"]),
                    "rank": int(_num(row["rank"])),
                    "candidate": bool(row["candidate"]),
                    "selected": bool(row["selected"]),
                    "level": str(row["level"] or ""),
                    "weight_mode": str(row["weight_mode"] or ""),
                    "updated_at": str(row["updated_at"] or ""),
                    "payload": _payload(row)})
            except (ValueError, TypeError) as exc:  # 坏行只牺牲自己
                logger.warning("主线挖掘存储：跳过一行损坏评分：%s", _short(exc))
        return out

    async def latest_trade_date(self) -> str:
        """最近一个有评分快照的交易日（**没有数据时返回空串**，不抛错）。"""
        await self._ensure_schema()
        try:
            row = await asyncio.to_thread(
                self._read_one, f"SELECT MAX(trade_date) AS v FROM {SCORE_TABLE}")
            return str(row["v"] or "") if row is not None else ""
        except sqlite3.Error as exc:
            logger.warning("主线挖掘存储：读最近交易日失败（降级为空）：%s", _short(exc))
            return ""

    async def load_scores(self, trade_date: str = "", *,
                          limit: int = 500) -> list[dict]:
        """读某日评分榜（`total` 降序）；`trade_date` 为空时取最近的交易日。

        500 条上限足够（沪深申万一级 + 同花顺概念合计约 300 个板块）。
        """
        await self._ensure_schema()
        try:
            target = trade_date
            if not target:
                row = await asyncio.to_thread(
                    self._read_one, f"SELECT MAX(trade_date) AS v FROM {SCORE_TABLE}")
                target = str(row["v"] or "") if row is not None else ""
            if not target:
                return []
            rows = await asyncio.to_thread(self._scores_sync, target, limit)
            return self._score_rows(rows)
        except (sqlite3.Error, ValueError, OSError) as exc:
            logger.warning("主线挖掘存储：读评分失败（降级为空）：%s", _short(exc))
            return []

    async def score_history(self, board_code: str = "", *, start: str = "",
                            end: str = "", limit: int = 5000) -> list[dict]:
        """读评分时间序列（可只给板块，或只给日期区间），新的在前。

        给回测/复盘用：一个板块的分数怎么走、哪天越过了阈值。区间口径是
        **含 start、不含 end**，与 `load_alerts` 一致。
        """
        await self._ensure_schema()
        try:
            where, params = _where(board_code, start, end)
            rows = await asyncio.to_thread(
                self._read, f"SELECT * FROM {SCORE}{where}"
                " ORDER BY trade_date DESC, board_code LIMIT ?",
                (*params, max(int(limit), 1)))
            return self._score_rows(rows)
        except (sqlite3.Error, ValueError, OSError) as exc:
            logger.warning("主线挖掘存储：读评分历史失败（降级为空）：%s", _short(exc))
            return []

    # ---------- 告警 ----------

    def _save_alerts_sync(self, alerts: list[AlertSignal]) -> int:
        now = _now()
        rows = [(str(item.alert_id), str(item.trade_date or ""),
                 str(item.board_code or ""), str(item.board_name or ""),
                 _val(item.kind), _val(item.level, "none"), _num(item.score),
                 _num(item.gate_bonus),
                 # resonance 在本表存数值（布尔也能放，列名沿用需求文档）
                 1.0 if item.resonance else 0.0, item.entry_close,
                 item.max_gain_pct, str(item.max_gain_date or ""),
                 _dumps(item.to_dict()), now) for item in alerts]
        if not rows:
            return 0
        with self._connect() as connection:
            connection.executemany(
                f"INSERT INTO {ALERT_TABLE}(alert_id, trade_date, board_code,"
                " board_name, kind, level, score, gate_bonus, resonance,"
                " entry_close, max_gain_pct, max_gain_date, payload, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(alert_id) DO UPDATE SET score=excluded.score,"
                " gate_bonus=excluded.gate_bonus,"
                " resonance=excluded.resonance, entry_close=excluded.entry_close,"
                " max_gain_pct=excluded.max_gain_pct,"
                " max_gain_date=excluded.max_gain_date,"
                " payload=excluded.payload, created_at=excluded.created_at", rows)
        return len(rows)

    async def save_alerts(self, alerts: list[AlertSignal]) -> int:
        """告警流水 upsert（`alert_id` = 日期+板块+等级，同键即同一件事）。

        只覆盖内容、不新增行：`evaluate` 回填的 `max_gain_pct` / `max_gain_date`
        允许被后续复盘刷新，但同一次告警永远不会变成两行。
        """
        return await self._write(self._save_alerts_sync, alerts)

    def _alerts_sync(self, level: Any, board_code: str, start: str, end: str,
                     limit: int) -> list[dict]:
        where, params = _where(board_code, start, end, _levels(level))
        rows = self._read(f"SELECT * FROM {ALERT}{where}"
                          " ORDER BY trade_date DESC, score DESC LIMIT ?",
                          (*params, max(int(limit), 1)))
        out: list[dict] = []
        for row in rows:
            try:
                out.append({
                    "alert_id": str(row["alert_id"] or ""), "level": str(row["level"] or ""),
                    "trade_date": str(row["trade_date"] or ""), "score": _num(row["score"]),
                    "board_code": str(row["board_code"] or ""), "kind": str(row["kind"] or ""),
                    "board_name": str(row["board_name"] or ""), "payload": _payload(row),
                    "resonance": bool(row["resonance"]), "entry_close": row["entry_close"],
                    "max_gain_pct": row["max_gain_pct"], "created_at": str(row["created_at"] or ""),
                    "max_gain_date": str(row["max_gain_date"] or "")})
            except (ValueError, TypeError) as exc:
                logger.warning("主线挖掘存储：跳过一行损坏告警：%s", _short(exc))
        return out

    async def load_alerts(self, *, level: str = "", start: str = "",
                          end: str = "", limit: int = 200) -> list[dict]:
        """读告警流水（新的在前）。

        `level` 三种写法都接受：``""``（全部）、`SignalLevel.STRONG` 这类枚举、
        ``"strong,medium"`` 逗号列表；`start` 含、`end` 不含。
        """
        await self._ensure_schema()
        try:
            return await asyncio.to_thread(self._alerts_sync, level, "", start,
                                           end, limit)
        except (sqlite3.Error, ValueError, OSError) as exc:
            logger.warning("主线挖掘存储：读告警失败（降级为空）：%s", _short(exc))
            return []

    async def pending_confirmation(self, board_code: str, *, before: str,
                                   periods: int = 2) -> list[dict]:
        """某板块在 `before` **之前**最近 `periods` 条告警（新的在前）。

        用于「触发确认」规则：`before` 传当日，取回的就是上一次触发记录 ——
        只有连续 N 个信号周期内再次触发才算真启动，孤零零一次只是试盘。
        用严格小于（`trade_date < before`），所以当天自己的告警不会被算进来。
        """
        await self._ensure_schema()
        try:
            return await asyncio.to_thread(self._alerts_sync, "", board_code, "",
                                           str(before or ""), periods)
        except (sqlite3.Error, ValueError, OSError) as exc:
            logger.warning("主线挖掘存储：读待确认告警失败（降级为空）：%s", _short(exc))
            return []

    # ---------- 回测 ----------

    def _save_backtest_sync(self, report: BacktestReport) -> str:
        run_id = str(report.run_id or "")
        if not run_id:
            # 手动触发可能没给 ID：用结束时间造一个，保证"最近一次"仍可查。
            run_id = f"bt-{str(report.finished_at or _now())[:19]}"
            report.run_id = run_id
        metrics = report.metrics.to_dict() if report.metrics else {}
        correlation = (report.correlation.to_dict() if report.correlation
                       else {})
        with self._connect() as connection:
            connection.execute(
                f"INSERT INTO {BACKTEST_TABLE}(run_id, range_start, range_end,"
                " started_at, finished_at, seconds, metrics, folds, scenes,"
                " correlation, markdown, gaps, error)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(run_id) DO UPDATE SET"
                " range_start=excluded.range_start, range_end=excluded.range_end,"
                " started_at=excluded.started_at, finished_at=excluded.finished_at,"
                " seconds=excluded.seconds, metrics=excluded.metrics,"
                " folds=excluded.folds, scenes=excluded.scenes,"
                " correlation=excluded.correlation,"
                " markdown=excluded.markdown, gaps=excluded.gaps,"
                " error=excluded.error",
                (run_id, str(report.range_start or ""), str(report.range_end or ""),
                 str(report.started_at or ""), str(report.finished_at or ""),
                 _num(report.seconds), _dumps(metrics),
                 _dumps([fold.to_dict() for fold in report.folds]),
                 _dumps([scene.to_dict() for scene in report.scenes]),
                 _dumps(correlation),
                 str(report.markdown or ""), _dumps(list(report.gaps)),
                 str(report.error or "")))
        return run_id

    async def save_backtest(self, report: BacktestReport) -> str:
        """保存一次回测（返回 run_id；报告没带 ID 时由本方法生成并回填）。

        `markdown` 单独一列而不是塞进 JSON：它是报告正文（单页几十 KB），
        列表接口用 `list_backtests` 跳过它，避免"列 20 次回测要拖 1 MB 正文"。
        """
        return await self._write(self._save_backtest_sync, report)

    def _backtest_dict(self, row: sqlite3.Row, *, summary: bool) -> dict[str, Any]:
        data: dict[str, Any] = {
            "run_id": str(row["run_id"] or ""), "seconds": _num(row["seconds"]),
            "range_start": str(row["range_start"] or ""),
            "range_end": str(row["range_end"] or ""),
            "started_at": str(row["started_at"] or ""),
            "finished_at": str(row["finished_at"] or ""),
            "metrics": _metrics(row["metrics"]), "error": str(row["error"] or ""),
            "gaps": _json_list(row, "gaps")}
        if not summary:
            data["folds"] = _json_list(row, "folds")
            data["scenes"] = _json_list(row, "scenes")
            data["correlation"] = _correlation(row["correlation"])
            data["markdown"] = str(row["markdown"] or "")
        return data

    async def load_backtest(self, run_id: str = "") -> dict:
        """读一次回测的完整结果；`run_id` 为空时取**最近一次**。

        没有记录时返回 `{}`（而不是抛 KeyError）：面板首次打开就是这种情况。
        """
        await self._ensure_schema()
        try:
            sql = (f"SELECT * FROM {BACKTEST_TABLE} WHERE run_id = ?" if run_id else
                   f"SELECT * FROM {BACKTEST_TABLE} ORDER BY finished_at DESC,"
                   " started_at DESC, run_id DESC LIMIT 1")
            row = await asyncio.to_thread(self._read_one, sql, (run_id,) if run_id else ())
            return self._backtest_dict(row, summary=False) if row is not None else {}
        except (sqlite3.Error, ValueError, OSError) as exc:
            logger.warning("主线挖掘存储：读回测失败（降级为空）：%s", _short(exc))
            return {}

    async def list_backtests(self, *, limit: int = 20) -> list[dict]:
        """回测历史**摘要**列表（新的在前，不含 `markdown` / `folds` / `scenes`）。

        刻意显式列名而不是 `SELECT *`：正文列几十 KB，列表页只要标题行，
        拖上它会让"打开回测历史"慢一个数量级。
        """
        await self._ensure_schema()
        try:
            rows = await asyncio.to_thread(
                self._read, f"SELECT {_SUMMARY} FROM {BACKTEST_TABLE}"
                " ORDER BY finished_at DESC, started_at DESC, run_id DESC LIMIT ?",
                (max(int(limit), 1),))
            return [self._backtest_dict(row, summary=True) for row in rows]
        except (sqlite3.Error, ValueError, OSError) as exc:
            logger.warning("主线挖掘存储：读回测列表失败（降级为空）：%s", _short(exc))
            return []

    # ---------- 期货信号与映射校准 ----------

    def _save_futures_sync(self, trade_date: str, signals: list[FutureSignal]) -> int:
        now = _now()
        rows = [(str(item.trade_date or trade_date), str(item.code),
                 str(item.name or ""), _val(item.kind), _num(item.intensity),
                 _val(item.level, "none"), _dumps(item.to_dict()), now)
                for item in signals if str(getattr(item, "code", ""))]
        if not rows:
            return 0
        with self._connect() as connection:
            connection.executemany(
                f"INSERT INTO {FUTURE_TABLE}(trade_date, code, name, kind, intensity,"
                " level, payload, updated_at) VALUES(?,?,?,?,?,?,?,?)"
                " ON CONFLICT(trade_date, code) DO UPDATE SET name=excluded.name,"
                " kind=excluded.kind, intensity=excluded.intensity,"
                " level=excluded.level, payload=excluded.payload,"
                " updated_at=excluded.updated_at", rows)
        return len(rows)

    async def save_future_signals(self, trade_date: str,
                                  signals: list[FutureSignal]) -> int:
        """整日期货信号 upsert（键 `(trade_date, code)`），返回写入行数。"""
        return await self._write(self._save_futures_sync, trade_date, signals)

    async def latest_future_trade_date(self) -> str:
        """最近一个有期货信号的交易日（没有数据时返回空串）。"""
        await self._ensure_schema()
        try:
            row = await asyncio.to_thread(
                self._read_one, f"SELECT MAX(trade_date) AS v FROM {FUTURE_TABLE}")
            return str(row["v"] or "") if row is not None else ""
        except sqlite3.Error as exc:
            logger.warning("主线挖掘存储：读期货最近交易日失败（降级为空）：%s",
                           _short(exc))
            return ""

    async def load_future_signals(self, trade_date: str = "",
                                  *, limit: int = 200) -> list[dict]:
        """读某日期货信号（`intensity` 降序）；日期为空时取最近的交易日。"""
        await self._ensure_schema()
        try:
            target = trade_date
            if not target:
                row = await asyncio.to_thread(
                    self._read_one, f"SELECT MAX(trade_date) AS v FROM {FUTURE_TABLE}")
                target = str(row["v"] or "") if row is not None else ""
            if not target:
                return []
            rows = await asyncio.to_thread(
                self._read, f"SELECT * FROM {FUTURE} WHERE trade_date = ?"
                " ORDER BY intensity DESC, code LIMIT ?", (target, max(int(limit), 1)))
            out: list[dict] = []
            for row in rows:
                try:
                    payload = _payload(row)
                    payload.setdefault("trade_date", str(row["trade_date"] or ""))
                    out.append({
                        "trade_date": str(row["trade_date"] or ""),
                        "code": str(row["code"] or ""), "name": str(row["name"] or ""),
                        "kind": str(row["kind"] or ""), "level": str(row["level"] or ""),
                        "intensity": _num(row["intensity"]),
                        "updated_at": str(row["updated_at"] or ""), "payload": payload})
                except (ValueError, TypeError) as exc:
                    logger.warning("主线挖掘存储：跳过一行损坏期货信号：%s", _short(exc))
            return out
        except (sqlite3.Error, ValueError, OSError) as exc:
            logger.warning("主线挖掘存储：读期货信号失败（降级为空）：%s", _short(exc))
            return []

    def _save_calib_sync(self, rows: list[dict[str, Any]]) -> int:
        now = _now()
        values = []
        for item in rows or []:
            if not isinstance(item, dict):
                continue
            code = str(item.get("future_code") or item.get("code") or "").strip()
            if not code:
                continue  # 没有品种代码的行无法定位，跳过而不是写空主键
            values.append((code, str(item.get("future_name") or item.get("name") or ""),
                           str(item.get("board_name") or ""),
                           int(_num(item.get("strength"))),
                           item.get("calibrated_strength"), item.get("correlation"),
                           str(item.get("calibrated_at") or now),
                           int(_num(item.get("window_days")))))
        if not values:
            return 0
        with self._connect() as connection:
            # 逐键 upsert 而不是"先清空再写入"：映射表可能扩大但极少缩小，
            # 误删会让下一轮季度校准之前的信号全部失去强度。
            connection.executemany(
                f"INSERT INTO {CALIBRATION_TABLE}(future_code, future_name, board_name,"
                " strength, calibrated_strength, correlation, calibrated_at, window_days)"
                " VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(future_code) DO UPDATE SET"
                " future_name=excluded.future_name, board_name=excluded.board_name,"
                " strength=excluded.strength,"
                " calibrated_strength=excluded.calibrated_strength,"
                " correlation=excluded.correlation,"
                " calibrated_at=excluded.calibrated_at,"
                " window_days=excluded.window_days", values)
        return len(values)

    async def save_calibrations(self, rows: list[dict[str, Any]]) -> int:
        """写入季度校准结果（按 `future_code` upsert），返回写入行数。

        入参是 dict 列表而不是 `FutureMapping`：校准只产出一小撮字段
        （强度 / 相关系数 / 窗口），要求调用方先拼一个完整 dataclass 是多余负担。
        """
        return await self._write(self._save_calib_sync, rows)

    async def load_calibrations(self) -> dict[str, float]:
        """读校准强度：`{future_code: calibrated_strength}`。

        只回这一个 dict 是因为调用方只做一件事：拿它覆盖映射表的静态星级；
        整行取出来没人看，还要多维护一个结构。
        """
        await self._ensure_schema()
        try:
            rows = await asyncio.to_thread(
                self._read, "SELECT future_code, calibrated_strength FROM"
                f" {CALIBRATION_TABLE} WHERE calibrated_strength IS NOT NULL")
            return {str(row["future_code"]): _num(row["calibrated_strength"])
                    for row in rows if str(row["future_code"] or "")}
        except (sqlite3.Error, ValueError, OSError) as exc:
            logger.warning("主线挖掘存储：读映射校准失败（降级为空）：%s", _short(exc))
            return {}

    # ---------- ETF 份额监控 ----------

    def _save_etf_signals_sync(self, signals: list[FlowSignal]) -> int:
        now = _now()
        rows = []
        for item in signals:
            # 校验 date/code 而不是 alert_id：后者是 **派生属性**
            # （`f"{date}-{code}-{kind}"`），日期与代码都为空时它会得到
            # `"--opportunity"` 这种真值字符串 —— 拿它当有效性判据等于没判，
            # 库里会攒下一批查不回来的废行。
            trade_date = str(getattr(item, "date", "") or "")
            code = str(getattr(item, "code", "") or "")
            if not trade_date or not code:
                continue
            rows.append((
                str(getattr(item, "alert_id", "") or ""), trade_date, code,
                str(getattr(item, "name", "") or ""),
                str(getattr(item, "group", "") or ""),
                _val(getattr(item, "kind", "")),
                _val(getattr(item, "level", ""), "none"),
                str(getattr(item, "regime", "") or "range"),
                1 if getattr(item, "gated", False) else 0,
                getattr(item, "index_percentile", None),
                getattr(item, "change_1d", None),
                getattr(item, "change_5d", None),
                1 if getattr(item, "resonance", False) else 0,
                _dumps(item.to_dict()), now))
        if not rows:
            return 0
        with self._connect() as connection:
            connection.executemany(
                f"INSERT INTO {ETF_SIGNAL_TABLE}(alert_id, trade_date, code, name,"
                " grp, kind, level, regime, gated, index_percentile, change_1d,"
                " change_5d, resonance, payload, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(alert_id) DO UPDATE SET name=excluded.name,"
                " level=excluded.level, regime=excluded.regime, gated=excluded.gated,"
                " index_percentile=excluded.index_percentile,"
                " change_1d=excluded.change_1d, change_5d=excluded.change_5d,"
                " resonance=excluded.resonance, payload=excluded.payload,"
                " updated_at=excluded.updated_at", rows)
        return len(rows)

    async def save_etf_signals(self, signals: list[FlowSignal]) -> int:
        """ETF 份额信号 upsert（`alert_id` = 日期+代码+类型），返回写入行数。

        幂等键选 `alert_id` 而不是 `(trade_date, code)`：同一只 ETF 在同一天
        可能同时产生机会与行业反转两种情况（宽基组不会，但清单改配置后可能），
        用日期+代码会把它们互相覆盖掉一条。
        """
        return await self._write(self._save_etf_signals_sync, signals)

    @staticmethod
    def _etf_where(*, kind: str = "", level: str = "", code: str = "",
                   grp: str = "", start: str = "", end: str = "",
                   gated: bool | None = None,
                   alerts_only: bool = False) -> tuple[str, list[Any]]:
        """拼 ETF 信号表的参数化 `WHERE`（区间口径同其余表：含 start、不含 end）。"""
        parts: list[str] = []
        params: list[Any] = []
        if kind:
            kinds = [item for item in str(kind).split(",") if item]
            parts.append(f"kind IN ({','.join('?' * len(kinds))})")
            params.extend(kinds)
        if level:
            parts.append("level = ?")
            params.append(str(level))
        if code:
            parts.append("code = ?")
            params.append(str(code))
        if grp:
            parts.append("grp = ?")
            params.append(str(grp))
        if start:
            parts.append("trade_date >= ?")
            params.append(str(start))
        if end:
            parts.append("trade_date < ?")
            params.append(str(end))
        if gated is not None:
            parts.append("gated = ?")
            params.append(1 if gated else 0)
        if alerts_only:
            # "会真正告警的" = 未被环境门控降级 + 等级为强/中。
            # 只写 gated = 0 是不够的：走"分位不在极端区"那条路径的弱信号
            # gated 也是 0，但它们只是观察项，从不告警。
            parts.append("gated = 0 AND level IN ('strong','medium')")
        return (" WHERE " + " AND ".join(parts) if parts else ""), params

    def _etf_signals_sync(self, limit: int, **filters: Any) -> list[dict]:
        where, params = self._etf_where(**filters)
        # 等级用 CASE 排严重度而不是按字符串排：字符串序是 medium < none <
        # strong < weak，会把"中信号"排在"强信号"前面，而面板要的是
        # 最该看的先出现。`level` 在本表存的是英文枚举值。
        rows = self._read(
            f"SELECT * FROM {ETF_SIGNAL}{where}"
            " ORDER BY trade_date DESC,"
            " CASE level WHEN 'strong' THEN 0 WHEN 'medium' THEN 1"
            " WHEN 'weak' THEN 2 ELSE 3 END, code LIMIT ?",
            (*params, max(int(limit), 1)))
        out: list[dict] = []
        for row in rows:
            try:
                out.append({
                    "alert_id": str(row["alert_id"] or ""),
                    "trade_date": str(row["trade_date"] or ""),
                    "code": str(row["code"] or ""), "name": str(row["name"] or ""),
                    "group": str(row["grp"] or ""), "kind": str(row["kind"] or ""),
                    "level": str(row["level"] or ""),
                    "regime": str(row["regime"] or ""),
                    "gated": bool(row["gated"]),
                    "resonance": bool(row["resonance"]),
                    "index_percentile": row["index_percentile"],
                    "change_1d": row["change_1d"], "change_5d": row["change_5d"],
                    "updated_at": str(row["updated_at"] or ""),
                    "payload": _payload(row)})
            except (ValueError, TypeError) as exc:
                logger.warning("主线挖掘存储：跳过一行损坏 ETF 信号：%s", _short(exc))
        return out

    async def load_etf_signals(self, *, kind: str = "", level: str = "",
                               code: str = "", group: str = "", start: str = "",
                               end: str = "", gated: bool | None = None,
                               alerts_only: bool = False,
                               limit: int = 200) -> list[dict]:
        """读 ETF 份额信号（新的在前）。

        `alerts_only=True` 只返回**会真正告警的**（未被环境门控 + 强/中等级），
        这是面板默认视图；要看全部异动就把 `gated` 留成 None 且不传它。
        """
        await self._ensure_schema()
        try:
            return await asyncio.to_thread(
                self._etf_signals_sync, limit, kind=kind, level=level,
                code=code, grp=group, start=start, end=end, gated=gated,
                alerts_only=alerts_only)
        except (sqlite3.Error, ValueError, OSError) as exc:
            logger.warning("主线挖掘存储：读 ETF 信号失败（降级为空）：%s", _short(exc))
            return []

    async def latest_etf_trade_date(self) -> str:
        """最近一条 ETF 份额信号的日期（没有数据时返回空串）。"""
        await self._ensure_schema()
        try:
            row = await asyncio.to_thread(
                self._read_one,
                f"SELECT MAX(trade_date) AS v FROM {ETF_SIGNAL_TABLE}")
            return str(row["v"] or "") if row is not None else ""
        except sqlite3.Error as exc:
            logger.warning("主线挖掘存储：读 ETF 信号最近日期失败：%s", _short(exc))
            return ""

    def _save_etf_backtest_sync(self, report: Any) -> str:
        run_id = str(getattr(report, "run_id", "") or "")
        if not run_id:
            run_id = f"etfbt-{str(getattr(report, 'finished_at', '') or _now())[:19]}"
            report.run_id = run_id
        with self._connect() as connection:
            connection.execute(
                f"INSERT INTO {ETF_BACKTEST_TABLE}(run_id, range_start, range_end,"
                " started_at, finished_at, seconds, days, signals, result, markdown,"
                " gaps, error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(run_id) DO UPDATE SET"
                " range_start=excluded.range_start, range_end=excluded.range_end,"
                " started_at=excluded.started_at, finished_at=excluded.finished_at,"
                " seconds=excluded.seconds, days=excluded.days,"
                " signals=excluded.signals, result=excluded.result,"
                " markdown=excluded.markdown, gaps=excluded.gaps,"
                " error=excluded.error",
                (run_id, str(getattr(report, "range_start", "") or ""),
                 str(getattr(report, "range_end", "") or ""),
                 str(getattr(report, "started_at", "") or ""),
                 str(getattr(report, "finished_at", "") or ""),
                 _num(getattr(report, "seconds", 0)),
                 int(_num(getattr(report, "days", 0))),
                 len(getattr(report, "records", []) or []),
                 _dumps(report.to_dict(collapse_runs=True)),
                 str(getattr(report, "markdown", "") or ""),
                 _dumps(list(getattr(report, "gaps", []) or [])),
                 str(getattr(report, "error", "") or "")))
        return run_id

    async def save_etf_backtest(self, report: Any) -> str:
        """保存一次 ETF 份额回测（返回 run_id）。

        入参用 `Any` 鸭子类型而不是 import `etf_flow_backtest.BacktestReport`：
        那个模块会连带拉起 datastore 之类的重依赖，而存储层只需要
        `to_dict()` 和几个属性。代价是类型提示弱，换来的是导入图干净。
        """
        return await self._write(self._save_etf_backtest_sync, report)

    def _etf_backtest_dict(self, row: sqlite3.Row, *, summary: bool) -> dict[str, Any]:
        data: dict[str, Any] = {
            "run_id": str(row["run_id"] or ""), "seconds": _num(row["seconds"]),
            "range_start": str(row["range_start"] or ""),
            "range_end": str(row["range_end"] or ""),
            "started_at": str(row["started_at"] or ""),
            "finished_at": str(row["finished_at"] or ""),
            "days": int(_num(row["days"])), "signals": int(_num(row["signals"])),
            "error": str(row["error"] or ""), "gaps": _json_list(row, "gaps")}
        if not summary:
            data["result"] = _loads(row["result"], {}) or {}
            data["markdown"] = str(row["markdown"] or "")
        return data

    async def load_etf_backtest(self, run_id: str = "") -> dict:
        """读一次 ETF 份额回测；`run_id` 为空时取最近一次（无记录返回 `{}`）。"""
        await self._ensure_schema()
        try:
            sql = (f"SELECT * FROM {ETF_BACKTEST_TABLE} WHERE run_id = ?" if run_id
                   else f"SELECT * FROM {ETF_BACKTEST_TABLE} ORDER BY finished_at"
                   " DESC, started_at DESC, run_id DESC LIMIT 1")
            row = await asyncio.to_thread(self._read_one, sql,
                                          (run_id,) if run_id else ())
            return self._etf_backtest_dict(row, summary=False) if row else {}
        except (sqlite3.Error, ValueError, OSError) as exc:
            logger.warning("主线挖掘存储：读 ETF 回测失败（降级为空）：%s", _short(exc))
            return {}

    async def list_etf_backtests(self, *, limit: int = 20) -> list[dict]:
        """ETF 份额回测历史摘要（新的在前，不含 `markdown` 正文）。"""
        await self._ensure_schema()
        try:
            rows = await asyncio.to_thread(
                self._read, "SELECT run_id, range_start, range_end, started_at,"
                " finished_at, seconds, days, signals, gaps, error FROM"
                f" {ETF_BACKTEST_TABLE} ORDER BY finished_at DESC,"
                " started_at DESC, run_id DESC LIMIT ?", (max(int(limit), 1),))
            return [self._etf_backtest_dict(row, summary=True) for row in rows]
        except (sqlite3.Error, ValueError, OSError) as exc:
            logger.warning("主线挖掘存储：读 ETF 回测列表失败（降级为空）：%s",
                           _short(exc))
            return []


# ==================================================================
# 组装
# ==================================================================

def build_mainline_repository(settings: Any) -> MainlineRepository | None:
    """按配置组装仓储并建表（与 `build_fund_flow_repository` 同口径）。

    `sqlite_path` 为空时返回 None（打警告）：模块仍可算分（内存态），**只是不落盘**，
    这比让整个页签装配失败要好。这里顺手建表，让"拿到仓储 = 表已就绪"，
    调用方不必记得先 await `ensure_schema()`。
    """
    path = getattr(settings, "sqlite_path", "") or ""
    if not path:
        logger.warning("未配置 sqlite_path，主线挖掘结果无法持久化（仅内存）")
        return None
    repo = MainlineRepository(path=str(path))
    try:
        repo._ensure_schema_sync()
    except (sqlite3.Error, OSError, ValueError) as exc:
        logger.warning("主线挖掘存储：建表失败（降级：主线结果不落盘）：%s", _short(exc))
    return repo


async def ensure_mainline_tables(path: str) -> None:
    """给 API lifespan 用的便捷函数：确保 `path` 上全部主线表存在。

    失败**只警告不抛**：表建不出来（磁盘满 / 库被独占）时服务应该照常起来，
    由各接口的"降级成空"承接，而不是拒绝启动。
    """
    if not path:
        logger.warning("主线挖掘存储：sqlite_path 为空，跳过建表")
        return
    try:
        await MainlineRepository(path=str(path)).ensure_schema()
    except (sqlite3.Error, OSError, ValueError) as exc:
        logger.warning("主线挖掘存储：建表失败（降级：主线结果不落盘）：%s", _short(exc))


__all__ = [
    "ALERT_TABLE", "BACKTEST_TABLE", "CALIBRATION_TABLE", "ETF_BACKTEST_TABLE",
    "ETF_SIGNAL_TABLE", "FUTURE_TABLE", "SCORE_TABLE", "MainlineRepository",
    "build_mainline_repository", "ensure_mainline_tables", "normalize_backtest",
]
