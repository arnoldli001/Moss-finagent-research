"""日K的**补充日频字段**：换手率与资金净流入额（来自本地行情仓 `data/quant/warehouse.db`）。

## 为什么需要它

日K面板的日线来自采集链（盘中是腾讯日K）。腾讯日K**不返回换手率、也不返回成交额**
（成交额如实置 None → 落库成 0），AkShare/baostock 那两跳要么被阻断、要么更慢，
于是"换手率""资金净流入"在链上根本取不到 —— 实测 300308 的 120 根日K里
`turnover` 非空 **0/120**、`amount` 末值 **0.0**。

而本地 Tushare 行情仓里躺着两张**日频定稿**表（`data/quant/warehouse.db`，15 GiB）：

| 表 | 列 | 口径 |
|---|---|---|
| `quant_daily_basic` | `turnover_rate`、`float_share` 等 [注1] | 换手率 [注2] |
| `quant_moneyflow` | `net_mf_amount`（元，主力净额） | Tushare 原始口径 |

[注1] 完整列：`turnover_rate`(%)、`turnover_rate_f`、`float_share`、`circ_mv`。
[注2] 换手率 = 成交量 / 流通股本，与日线成交量自洽。实测 300308 20260922：
      `turnover_rate=2.1153%` ↔ 234785 手 / 1.11e9 股 ✓

两张表都有 `(code, trade_date)` 联合索引，实测单只票 4 个月区间查询 **12~31 ms** ——
比一次网络取数便宜两个数量级，而且**不需要编造任何数字**。

## 只补展示，不喂规则引擎（刻意）

这两个字段**只挂到 `DailySnapshot.bars` 上供前端读数/区间统计**，不参与
`analyse_daily` 的量价规则、也不进打分：`character.py` 与 `daily_signals.py`
里都有"日线没有换手率列就跳过该项"的分支，一旦把列喂进去，**同一次改动会
悄悄改掉股性分与买卖标记** —— 那是另一件事，该单独评估、单独回归。

## fail-open

读不到（仓库缺失 / 该标的没数据 / SQLite 报 `disk I/O error`）时返回空表，
由调用方如实记一条缺口，**不影响日K主链路**：这两个字段是锦上添花。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.core.errors import BRIEF_TIGHT, brief
from src.mainline.warehouse import DEFAULT_WAREHOUSE, open_warehouse

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DayExtra:
    """一根日K的补充字段（取不到就是 None，不填 0）。"""

    turnover: float | None = None      # 换手率（%）
    net_mf: float | None = None        # 主力资金净流入额（元；负=净流出）


def load_daily_extras(
    code: str, start_date: str, end_date: str,
    *, warehouse: str = DEFAULT_WAREHOUSE,
) -> dict[str, DayExtra]:
    """按区间取 `{YYYY-MM-DD: DayExtra}`；取不到就返回空 dict（不抛错给主链路）。

    `start_date` / `end_date` 收 `YYYY-MM-DD` 或 `YYYYMMDD` 两种写法
    （日线链路给的是前者，仓库里存的是后者）。
    """
    start = _compact(start_date)
    end = _compact(end_date)
    if not code or not start or not end:
        return {}
    result: dict[str, DayExtra] = {}
    try:
        conn = open_warehouse(warehouse)
    except Exception as exc:  # noqa: BLE001 仓库不可用只影响这两个字段
        logger.info("行情仓不可用，跳过换手率/资金净流入: %s", brief(exc, BRIEF_TIGHT))
        return {}
    try:
        for row in conn.execute(
            "SELECT trade_date, turnover_rate FROM quant_daily_basic"
            " WHERE code = ? AND trade_date >= ? AND trade_date <= ?",
            (code, start, end),
        ):
            day = _iso(row[0])
            if day is None or row[1] is None:
                continue
            result[day] = DayExtra(turnover=float(row[1]))
        for row in conn.execute(
            "SELECT trade_date, net_mf_amount FROM quant_moneyflow"
            " WHERE code = ? AND trade_date >= ? AND trade_date <= ?",
            (code, start, end),
        ):
            day = _iso(row[0])
            if day is None or row[1] is None:
                continue
            prev = result.get(day) or DayExtra()
            result[day] = DayExtra(turnover=prev.turnover, net_mf=float(row[1]))
    except Exception as exc:  # noqa: BLE001 表缺失/IO 错误都不该影响日K
        logger.info("行情仓读取换手率/资金净流入失败(%s): %s", code, brief(exc, BRIEF_TIGHT))
        return {}
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    return result


def _compact(text: str | None) -> str:
    """`2026-09-22` / `20260922` → `20260922`（仓库里的存储口径）。"""
    digits = "".join(ch for ch in str(text or "") if ch.isdigit())
    return digits if len(digits) == 8 else ""


def _iso(text: object) -> str | None:
    """`20260922` → `2026-09-22`（与 `DailyBar.date` 同口径）。"""
    digits = "".join(ch for ch in str(text or "") if ch.isdigit())
    if len(digits) != 8:
        return None
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
