"""行情仓库的「新鲜度」判定：这次选股用的到底是哪一天的行情。

## 为什么需要它（真实事故）

2026-09-18 09:10 手动跑「量化选股」，选出来的日期是 **20260915**。
不是选股逻辑错 —— 本地 Tushare 仓库里最新就到 0915（0916/0917 没同步），
选股链路忠实地用了"它手上最新的一天"。

问题在于**这件事在界面上完全看不出来**：分数、阈值、排名、模型档位全都算得出来，
没有报错、没有缺口、`selected` 还是 20 只。用户看到的是一个正常的选股结果，
但它描述的是**两天前的市场**。这类"静默用旧数据"的错误在投研系统里最贵。

所以这里做两件事：

1. `latest_complete_trade_date()` —— 算出"**应该是**哪一天"（最近一个已收盘的交易日）；
2. `freshness()` —— 和数据实际用的那一天比，给出机器可判的 `data_stale` 与给人看的说明。

调用方负责把结论**显式报出去**（运行记录的 gap、接口字段、前端横幅），
而不是自己悄悄判断完就算了。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, time
from typing import Any

from src.core.errors import (
    BRIEF_TIGHT,
    brief,
)

logger = logging.getLogger(__name__)

#: Tushare 的 EOD 数据 15:00~16:00 才入库（见 `quant_sync.py` 的说明），
#: 所以"今天的数据已经发布"这条线画在 16:30，留足余量。
EOD_RELEASE = time(16, 30)


def calendar_days() -> list[str]:
    """本地缓存的交易日历（升序 YYYYMMDD）。取不到返回空列表。

    **只读本地缓存、绝不联网**：这个判断会被放在选股主链路里，
    为了"检查一下新不新鲜"去发一次网络请求，代价和风险都不合适。
    缓存不存在时返回空 → 调用方按"判不了就不判"处理（不猜）。
    """
    try:
        from src.quant.dataset_store import DatasetStore

        store = DatasetStore("trade_cal")
        if not store.has("static"):
            return []
        frame = store.read("static")
        if frame is None or not len(frame) or "trade_date" not in frame.columns:
            return []
        return sorted({str(day) for day in frame["trade_date"].tolist()})
    except Exception as exc:  # noqa: BLE001 日历不可用不该影响选股
        logger.info("交易日历缓存不可读：%s", brief(exc, BRIEF_TIGHT))
        return []


def latest_complete_trade_date(*, now: datetime | None = None,
                               days: Sequence[str] | None = None) -> str:
    """最近一个**行情已经发布**的交易日（YYYYMMDD）；判不了返回空串。

    - 今天本身是交易日、且当前时间已过 `EOD_RELEASE` → 今天；
    - 否则 → 今天之前最近的一个交易日。

    第二条正是盘中的口径：09:25 选股时"最新可用"就是昨天，
    今天的数据还没产生，不能把"今天"当成应该有的数据。
    """
    moment = now or datetime.now()
    today = moment.strftime("%Y%m%d")
    table = list(days) if days is not None else calendar_days()
    if not table:
        return ""
    if moment.time() >= EOD_RELEASE:
        usable = [day for day in table if day <= today]
    else:
        usable = [day for day in table if day < today]
    return usable[-1] if usable else ""


def freshness(data_date: str, *, now: datetime | None = None,
              days: Sequence[str] | None = None) -> dict[str, Any]:
    """对照"实际用的日期"与"应该用的日期"。

    返回 `{expected_trade_date, data_stale, note, checked}`：

    - `checked=False` 表示**没做判断**（交易日历不可用）——
      这时 `data_stale` 恒为 False，但调用方不该把它读成"确认是新鲜的"；
    - `data_stale=True` 时 `note` 是一句能直接展示给用户的话。
    """
    expected = latest_complete_trade_date(now=now, days=days)
    actual = str(data_date or "")
    if not expected or not actual:
        return {"expected_trade_date": expected, "data_stale": False,
                "note": "", "checked": bool(expected and actual)}
    if actual >= expected:
        return {"expected_trade_date": expected, "data_stale": False,
                "note": "", "checked": True}
    note = (f"数据滞后：本次用的是 {actual} 的行情，"
            f"最近一个已收盘交易日是 {expected}。"
            f"本地行情仓库没有同步到最新 —— 分数反映的是 {actual} 的市场，"
            f"不是 {expected} 的。")
    return {"expected_trade_date": expected, "data_stale": True,
            "note": note, "checked": True}


__all__ = [
    "EOD_RELEASE",
    "calendar_days",
    "freshness",
    "latest_complete_trade_date",
]
