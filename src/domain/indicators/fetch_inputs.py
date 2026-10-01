"""派生指标的**输入取数**：先把候选名全部取完，再交给同步的 `derive()`。

## 为什么要单独一个模块

`derive.py` 是**纯计算**（AST 白名单求值，可离线单测，不碰库也不碰网 —— 这是它
自己的纪律）。而"把公式里的数据名变成真实数值"要碰 A01/连接器，属于取数侧。
本模块就是那个**取数器的唯一实现**，被两处共用：

  * `scripts/derived_metrics.py`（人工按需跑，交互路径 ⇒ 带 10s 防撞钟预算）；
  * `scheduler` 的 `derived_metrics_monthly` 作业（定时路径 ⇒ **不传预算**，
    重活正是要在那里做；掐掉它"按期累积趋势"就永远攒不起来）。

## ⚠️ 必须先 await 完再交给 `derive()`（本项目实测踩过）

第一版在同步 `fetch` 里用 `run_until_complete` —— 那是在**已经运行的事件循环**
里再跑一个循环，实测每条候选都是 `RuntimeError: This event loop is already running`，
于是**所有候选名都"取不到"**：看起来像"数据缺失"，实际是**取数器自己错**
（`AGENTS.md` 记过三次的同一个坑）。所以本模块的形态就是"预取成 dict + 同步查表"。
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from src.domain.indicators.derive import DerivedSpec, Fetcher

logger = logging.getLogger(__name__)

#: 取一条指标：`(indicator) -> [DataPoint]`。是否带防撞钟预算由**调用方**决定。
CollectFn = Callable[[str], Awaitable[list[Any]]]

#: 「这个数据名在本地哪个列里」的旁路查询（只用于 trace，可缺省）。
ColumnLookup = Callable[[str], Iterable[str]]


def candidate_names(specs: Iterable[DerivedSpec]) -> list[str]:
    """全部候选数据名（去重保序）——决定要预取哪些。"""
    names: list[str] = []
    for spec in specs:
        for symbol in spec.symbols:
            for cand in spec.inputs.get(symbol, ()):
                if cand not in names:
                    names.append(cand)
    return names


async def build_fetcher(
    collect: CollectFn, specs: Iterable[DerivedSpec], code: str, *,
    local_columns: ColumnLookup | None = None,
) -> tuple[Fetcher, list[str]]:
    """造一个 `fetch(name, code)` 并返回 `(fetch, trace)`。

    对每个候选名按 **`名字:代码` → `名字`** 的顺序试（具体口径优先，
    基名兜底），取**最新一期**。取不到就**不放进 cache** —— `derive()` 会把它
    当缺口如实报出来（绝不用 0 兜）。
    """
    trace: list[str] = []
    cache: dict[str, tuple[float, str, str]] = {}

    for name in candidate_names(specs):
        if local_columns is not None:
            try:
                cols = list(local_columns(name))
            except Exception as exc:  # noqa: BLE001 旁路查询失败不影响取数
                cols = []
                trace.append(f"{name}: 本地列查询失败 {type(exc).__name__}")
            else:
                trace.append(f"{name}: 本地同名列 {cols or '无'}")
        for indicator in (f"{name}:{code}", name) if code else (name,):
            try:
                points = await collect(indicator)
            except Exception as exc:  # noqa: BLE001 取不到就是取不到
                trace.append(f"{name}: {indicator} → "
                             f"{type(exc).__name__}: {str(exc)[:90]}")
                continue
            values = [p for p in points if getattr(p, "value", None) is not None]
            if values:
                latest = max(values, key=lambda p: getattr(p, "period_date", "") or "")
                trace.append(f"{name}: {indicator} → "
                             f"{latest.value} @ {latest.period_date}")
                cache[name] = (float(latest.value),
                               str(getattr(latest, "source_name", "") or ""),
                               str(getattr(latest, "period_date", "") or ""))
                break
            trace.append(f"{name}: {indicator} → 空结果")
        if name not in cache:
            trace.append(f"{name}: **未获取到**（本地/连接器都没有）")

    def fetch(name: str, code: str) -> tuple[float, str, str] | None:  # noqa: ARG001
        return cache.get(name)

    return fetch, trace


__all__ = ["CollectFn", "ColumnLookup", "build_fetcher", "candidate_names"]
