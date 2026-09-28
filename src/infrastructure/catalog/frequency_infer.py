"""从 data_point 序列**推断**更新频率（第十三轮）。

## 为什么需要它（用户实测打脸）

用户原话（2026-09-28）：
> 「在 moss_finagent.db 里 fact_data_points、fact_events 等表有很多数据，
>   可以建立索引，比如中国8月规模以上工业增加值月率-单月、中国8月城镇调查失业率」
> 「本地数据库对于板块估值、个股估值信息等股市数据都是有的，
>   也不用都从网上取，**优先本地取**」

### 问题：自动登记的频率是错的

事实表里有 **752 个指标**，而 `configs/indicators.yaml` 只声明了 43 个。
剩下的靠 SmartFetcher 自动登记，用的是兜底值 **`frequency=daily, freshness_hours=24`**。

后果（实测）：

| 指标 | 数据实际间隔 | 登记的 freshness | 结果 |
|---|---|---|---|
| `资产负债率:300308` | **季度**（90 天） | 24h | **永远 stale → 每次都联网** |
| `流动比率:300308` | 季度 | 24h | 同上 |
| `PE(TTM):510300` | 月（30 天） | 24h | 同上 |
| `PB:510300` | 月 | 24h | 同上 |

**这与"优先本地取"完全相反**：季度更新的财务数据被当成日频，
每天去网上拉 90 次，而库里明明有。

### 修法：从数据自身推断

`period_date` 序列的**相邻间隔中位数**就是最可靠的频率证据
—— 它来自真实数据，不需要人工声明，也不会漂移。
"""
from __future__ import annotations

import statistics
from datetime import datetime

#: 推断结果 → (中位间隔上限[天], frequency, 建议 freshness_hours)
#:
#: freshness 取"间隔 × 1.5"量级：留出发布延迟（如月频数据次月中旬才出）
#:
#: ⚠️ **边界必须 >1 天下限给 daily**（2026-09-28 实测踩坑）：
#: 最初写 `(1.5, "realtime", 0.5)`，于是**日频数据**（间隔 = 1 天）
#: 被判成 `realtime`、freshness = 0.5h → **永久 stale** →
#: 审计里 fresh 占比从 752/769 崩到 41/769。
#:
#: 正确语义：`realtime` 只适用于**同一天内多条**的记录
#: （由 `gaps == 0` 分支单独处理）；只要有跨天间隔，最多算 `daily`。
FREQUENCY_BANDS: tuple[tuple[float, str, float], ...] = (
    # (中位间隔上限[天], frequency, freshness_hours)
    (2.0, "daily", 26.0),        # 逐日 / 隔日
    (10.0, "weekly", 168.0),     # 周频
    (45.0, "monthly", 720.0),    # 月频
    (120.0, "quarterly", 2160.0),  # 季频
    (400.0, "yearly", 8760.0),   # 年频
)

#: 同一天内多条（gaps 全 0）→ 日内实时快照
INTRADAY_FREQUENCY = ("realtime", 0.5)

#: 解析不出来时的兜底
DEFAULT_FREQUENCY = "unknown"
DEFAULT_FRESHNESS_HOURS = 24.0

_DATE_FORMATS = (
    "%Y-%m-%d", "%Y-%m", "%Y%m%d", "%Y/%m/%d", "%Y.%m", "%Y%m",
)


def parse_period_date(raw: str | None) -> datetime | None:
    """尽量解析 period_date（ISO / 月 / 紧凑 / 点分 都支持）。

    解析不出来返回 None —— **不猜**。调用方按"证据不足"处理。
    """
    s = str(raw or "").strip()
    if not s:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def infer_frequency_from_periods(
    periods: list[str | None],
) -> tuple[str, float, int]:
    """从 period_date 序列推断 (frequency, freshness_hours, 样本数)。

    判据 = 相邻期间的**中位间隔**（天）：
      · 用中位而不是均值 —— 单次数据回填（如一次性补齐 5 年历史）
        会产生一个巨大的间隔，均值会被它带偏。
      · 样本 < 2 期 → 返回 unknown（**证据不足就不猜**，
        与 AGENTS.md「没量到与量到 0 必须分开」同源）。

    返回第三个值是参与推断的**有效期数**，供调用方判断可信度。
    """
    parsed = sorted(
        d for d in (parse_period_date(p) for p in periods) if d is not None
    )
    if len(parsed) < 2:
        return (DEFAULT_FREQUENCY, DEFAULT_FRESHNESS_HOURS, len(parsed))

    gaps = [(parsed[i + 1] - parsed[i]).days
            for i in range(len(parsed) - 1)]
    gaps = [g for g in gaps if g > 0]
    if not gaps:
        # 全部同期（同一天多条）→ 日内高频快照
        # ⚠️ 只有这种情况才算 realtime：**有跨天间隔就不能算**
        #    （否则日频数据会被判成 0.5h 新鲜度而永久 stale，实测踩过）
        return (*INTRADAY_FREQUENCY, len(parsed))

    median_gap = statistics.median(gaps)
    for limit, freq, fresh in FREQUENCY_BANDS:
        if median_gap <= limit:
            return (freq, fresh, len(parsed))
    return ("yearly", 8760.0, len(parsed))


def infer_from_rows(rows: list[dict]) -> tuple[str, float, int]:
    """便捷入口：从 `[{period_date: ...}, ...]` 推断。"""
    return infer_frequency_from_periods(
        [r.get("period_date") for r in rows])


__all__ = [
    "DEFAULT_FREQUENCY",
    "DEFAULT_FRESHNESS_HOURS",
    "FREQUENCY_BANDS",
    "infer_frequency_from_periods",
    "infer_from_rows",
    "parse_period_date",
]
