"""投资日历 → 数据点（让解禁/财报/宏观日程进入统一索引体系）。

## 为什么需要它（用户 2026-09-28 指出）

> 「加入到索引清单，避免下次取解禁数据取不到」

在此之前，解禁数据只能通过 `/api/v1/intel/calendar` **实时调接口**拿到
—— 它**不在 `fact_data_points` 里**，所以：

  · `indicator_catalog` 索引表里没有它 → SmartFetcher 判"未登记"
  · 每次投研分析要解禁数据都得**现调东财+巨潮**（网络往返）
  · 而且**A17 根本不知道有这个能力**（已由 `capabilities.py` 补上清单）

本模块把日历事件**转成标准 DataPoint 落库**，于是：
    日历 → DataPoint → fact_data_points → indicator_catalog 索引
    → SmartFetcher 可判新鲜度 → **下次优先本地取**

## 指标命名（新增，需与 `configs/indicators.yaml` 对齐）

| 指标 | 含义 | period_date |
|---|---|---|
| `cal:unlock:market_cap` | 当日合计解禁市值（元） | 解禁日 |
| `cal:unlock:company_count` | 当日解禁家数 | 解禁日 |
| `cal:unlock:top_stock_cap` | 当日最大单只解禁市值（元） | 解禁日 |
| `cal:earnings:company_count` | 当日预约披露家数 | 披露日 |

**为什么拆成多条指标而不是一条带 extra 的**：`SmartFetcher` 与
`_AGENT_DATA_WHITELIST` 都按 `indicator` 名匹配；拆开后 A11（财务风险）
可以直接订阅 `cal:unlock:*`，而 A08（宏观）不会误收。

## 确定性

`CalendarEvent.certainty`：
  · `rule`      —— 交易所规则确定（解禁日**不会改期**）
  · `scheduled` —— 预约（财报披露**可改期**）

落库时把 certainty 写进 `extra`，让下游知道"这个日期能不能信"。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from src.core.intel_limits import UNLOCK_DETAIL_TOP_N as _UNLOCK_JSON_TOP_N
from src.core.schemas import (
    DataPoint,
    DataSourceType,
    FetchMethod,
    hash_content,
)

logger = logging.getLogger(__name__)

#: 日历事件 kind → 要落库的指标后缀
_KIND_METRICS: dict[str, tuple[str, ...]] = {
    "unlock": ("market_cap", "company_count", "top_stock_cap"),
    "earnings": ("company_count",),
}

#: 指标前缀（与 YAML 对齐）
CAL_PREFIX = "cal:"

#: 日历指标的来源标识（写溯源用）
_SOURCE_NAME = "投资日历"
_SOURCE_URL = "https://data.eastmoney.com/dxf/q/"


def _top_stock_cap(event: Any) -> float:
    """当日最大单只解禁市值（元）。无个股明细时返回 0。"""
    stocks = (event.scope or {}).get("stocks") or []
    caps = []
    for s in stocks:
        try:
            caps.append(float(s.get("market_cap") or 0))
        except (TypeError, ValueError):
            continue
    return max(caps) if caps else 0.0


def calendar_events_to_points(
    events: list[Any], *, task_id: str = "calendar_sync",
) -> list[DataPoint]:
    """`CalendarEvent` 列表 → `DataPoint` 列表（纯函数，便于测试）。

    只为 `unlock` / `earnings` 两类生成点 —— `macro` / `trade_day`
    是**日程**不是**数值**，没有可比的 value，硬造一个数值反而是假数据。
    （AGENTS.md：宁可不显示，也不显示假的。）
    """
    now = datetime.now(timezone.utc)
    points: list[DataPoint] = []

    for ev in events:
        kind = str(getattr(ev, "kind", "") or "")
        metrics_wanted = _KIND_METRICS.get(kind)
        if not metrics_wanted:
            continue
        day = str(getattr(ev, "date", "") or "").strip()
        if not day:
            continue
        scope = getattr(ev, "scope", None) or {}
        metrics = getattr(ev, "metrics", None) or {}
        certainty = str(getattr(ev, "certainty", "") or "scheduled")

        # 逐指标取值
        values: dict[str, float] = {}
        for key in metrics_wanted:
            if key == "company_count":
                values[key] = float(scope.get("company_count") or 0)
            elif key == "top_stock_cap":
                values[key] = _top_stock_cap(ev)
            else:
                # ★ `metrics` 的键带 kind 前缀（`unlock_market_cap`），
                #   而指标后缀是 `market_cap` —— 直接 `metrics.get("market_cap")`
                #   会**静默取到 0**（实测踩过：29 条解禁市值全是 0，
                #   而 top_stock_cap 有值，一眼看出是取键错了）。
                #   两种形态都试：先带前缀，再裸键。
                raw_val = metrics.get(f"{kind}_{key}")
                if raw_val is None:
                    raw_val = metrics.get(key)
                try:
                    values[key] = float(raw_val or 0)
                except (TypeError, ValueError):
                    values[key] = 0.0

        for key, value in values.items():
            indicator = f"{CAL_PREFIX}{kind}:{key}"
            # ★ 解禁数据必须逐条带溯源（AGENTS.md：每个数据点四件套）
            raw = f"{indicator}|{day}|{value}|{kind}|{certainty}"
            content_hash = hash_content(raw)
            points.append(DataPoint(
                # ⚠️ `data_id` 是**主键** —— 用稳定 ID（如 `cal_unlock_x_2026-09-28`）
                #    会导致后续值修正**永远写不进去**（`INSERT OR IGNORE` 撞主键即忽略）。
                #    实测踩过：market_cap 取键错误写成 0，修好取键后重跑仍全是 0
                #    （inserted=0/skipped=2407），因为 data_id 没变。
                #    带上内容哈希前缀 → 值变了就是一个新版本行（符合本表的
                #    「不同哈希同期数据共存形成版本序列」设计）。
                data_id=f"cal_{kind}_{key}_{day}_{content_hash[:8]}",
                indicator=indicator,
                value=value,
                unit="元" if key.endswith("cap") else "家",
                period_date=day,
                extra={
                    "kind": kind,
                    "certainty": certainty,
                    # `rule` = 交易所规则确定（解禁日不会改期）；
                    # `scheduled` = 预约（财报披露可改期）
                    "certainty_zh": ("规则确定" if certainty == "rule"
                                     else "预约可改"),
                    "company_count": scope.get("company_count"),
                    "codes": (scope.get("codes") or [])[:50],
                    "names": (scope.get("names") or [])[:20],
                    # 保留下跌风险最相关的信息：最大的几只
                    #
                    # ⚠️ 这个 **10** 是"回退路径的结论强度上限"，**不是**随手取的数：
                    #   `PlatformDataConnector` 读不到 `unlock_plan` 表时会回退到这份
                    #   JSON 明细，而它被截断过 ⇒ 那条路的"没有该标的"只能表述为
                    #   「未见于明细」，不许说成"无解禁"（假阴性）。
                    #   两处必须同源，所以从连接器 import 常量（单一真值源）。
                    "top_stocks": sorted(
                        (scope.get("stocks") or []),
                        key=lambda s: float(s.get("market_cap") or 0),
                        reverse=True)[:_UNLOCK_JSON_TOP_N],
                    "from_calendar": True,
                },
                source_name=_SOURCE_NAME,
                source_url=_SOURCE_URL,
                source_type=DataSourceType.OFFICIAL,
                publish_time=now,
                fetch_time=now,
                fetch_method=FetchMethod.API_CALL,
                raw_content_hash=content_hash,
                processed_by="calendar_sync",
                process_time=now,
                # ⚠️ `DataPoint.confidence` 是 **float**（0~1），不是 `Confidence` 枚举
                #    （踩过：传枚举 → pydantic `unable to parse string as a number`）。
                #    解禁日由交易所规则确定（certainty=rule）→ 高置信；
                #    财报预约可改期（scheduled）→ 中置信。
                confidence=0.95 if certainty == "rule" else 0.8,
            ))
    return points


async def sync_calendar_to_store(
    repo: Any, *, task_id: str = "calendar_sync",
    horizon_days: int = 45,
) -> dict[str, int]:
    """拉投资日历（解禁 + 财报）→ 转数据点 → 入库。

    返回 `{"fetched": n, "inserted": m, "skipped": k, "errors": [...]}`。
    失败不抛（日历是**增值数据**，拿不到不该拖垮调用方）。
    """
    import asyncio

    out: dict[str, Any] = {"fetched": 0, "inserted": 0, "skipped": 0,
                           "errors": [], "sources": []}
    events: list[Any] = []
    try:
        from src.domain.intel.calendar import (
            fetch_earnings_schedule,
            fetch_unlock_schedule,
        )

        for name, fn in (("unlock", fetch_unlock_schedule),
                         ("earnings", fetch_earnings_schedule)):
            try:
                # ★ 注意：这些函数返回 **`(events, tried)` 元组**，不是纯列表。
                #   踩过的坑（2026-09-28）：当成列表用 → `calendar_events_to_points`
                #   收到的是 `[[CalendarEvent...], ['unlock_em']]` 这种嵌套结构，
                #   逐项 `getattr(ev, "kind")` 全部拿不到 → **静默产出 0 条**
                #   （不报错，只是"看起来没有日历数据"）。
                got = await asyncio.to_thread(fn, horizon_days=horizon_days)
                if isinstance(got, tuple):
                    evs, tried = (got + (None,))[:2] if len(got) < 2 else got[:2]
                    events.extend(evs or [])
                    if tried:
                        out["sources"].extend(list(tried))
                else:
                    # 兼容将来改成"只返回 events"的形态
                    events.extend(got or [])
            except Exception as exc:  # noqa: BLE001 单类失败不阻断另一类
                out["errors"].append(f"{name}: {type(exc).__name__}: {exc}")
                logger.warning("日历同步：%s 拉取失败 %s", name, exc)
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"import: {exc}")
        return out

    points = calendar_events_to_points(events, task_id=task_id)
    out["fetched"] = len(points)
    if not points:
        return out

    try:
        stats = await repo.save_points(points, task_id)
        out["inserted"] = int(stats.get("inserted", 0))
        out["skipped"] = int(stats.get("skipped", 0))
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"save: {type(exc).__name__}: {exc}")
        logger.warning("日历同步：入库失败 %s", exc)
    return out


__all__ = [
    "CAL_PREFIX",
    "calendar_events_to_points",
    "sync_calendar_to_store",
]
