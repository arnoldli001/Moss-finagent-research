"""跨通道一致性：本地陈旧要**升级**、两条不一致**不许静默改数**（`CHG-0157`）。

## 口径（PRD §三十三）

四跳链的顺序**不动**（本地优先：实测本地一次 ~85ms，联网是秒级）。缺的是
"本地命中 ≠ 可以用它"这条判断：

* **R2 陈旧才升级**，判据是**该指标声明的新鲜度**（`IndicatorMeta.freshness_hours`，
  与 `SmartFetcher` 判 stale 同一个契约）；未登记 ⇒ **不升级**（不猜）但如实标注；
* **R3 两条都拿到时不静默改数**：在线期次更新 ⇒ 用在线并标注；
  同期次值不同 ⇒ **保留本地**（顺序优先）+ 留痕；在线不可得 ⇒ 保留本地 + 标注；
* **R4 必须留痕**：记一条采集异常 `kind=cross_channel` ——
  `hop_stats` 只回答"哪一跳答出来的"，**"命中的是旧数据"在命中率里看不见**。

## 自证

`test_selfproof_without_tolerance_there_is_no_upgrade` 把"容忍度"关掉
（模拟"查不到契约就不升级"），断言在线通道**一次都不被调用** ——
证明前面那些"会升级"的断言不是恒绿。
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from src.core import collection_anomalies as ca
from src.core import hop_stats
from src.infrastructure.catalog import local_data as ld
from src.infrastructure.catalog.local_data import MetricSeries
from src.orchestration import supervisor

KEY = "stock_close:600036"


class _Point:
    def __init__(self, value: Any, period_date: str = "2026-09-30",
                 source_name: str = "AkShare") -> None:
        self.value = value
        self.period_date = period_date
        self.source_name = source_name


class _Backend:
    def __init__(self, points: list[Any] | None = None) -> None:
        self.calls: list[str] = []
        self._points = list(points or [])

    async def fetch(self, indicator: str, *a, **kw) -> list[Any]:
        self.calls.append(indicator)
        return list(self._points)


class _Collector:
    def __init__(self, backend: _Backend) -> None:
        self._backend = backend


def _agents(backend: _Backend) -> dict[str, Any]:
    return {"A01_data_collector": _Collector(backend)}


def _install_local(monkeypatch, *, stale_days: float, period: str = "2026-09-01",
                   value: float = 1.11) -> None:
    class _Stub:
        def metric_series(self, metric: str, *, entity: str = "", limit: int = 10):
            return MetricSeries(
                metric=metric, entity=entity,
                points=[{"period": period, "value": value}],
                source="local", dataset_id="ds",
                plan={"stale_days": stale_days}, diag=None)

    monkeypatch.setattr(ld, "LocalDataExecutor", _Stub)


def _capture_anomalies(monkeypatch) -> list[tuple[str, str, str]]:
    seen: list[tuple[str, str, str]] = []

    def _record(kind, indicator, reason, **kw):
        seen.append((kind, indicator, reason))
        return None

    monkeypatch.setattr(ca, "record", _record)
    return seen


@pytest.fixture(autouse=True)
def _clean_stats():
    hop_stats.reset_for_test()
    yield
    hop_stats.reset_for_test()


def _run(monkeypatch, *, tolerance: float | None, stale_days: float,
         backend: _Backend, period: str = "2026-09-01",
         value: float = 1.11) -> tuple[str, list[tuple[str, str, str]]]:
    monkeypatch.setattr(supervisor, "_declared_tolerance_days",
                        lambda _ind: tolerance)
    _install_local(monkeypatch, stale_days=stale_days, period=period, value=value)
    seen = _capture_anomalies(monkeypatch)
    text = asyncio.run(supervisor.query_data_for_agent(
        KEY, limit=10, state={"validated_points": [], "focus_stock_code": "600036"},
        agents=_agents(backend)))
    return text, seen


# ======================================================================
# 一、本地新鲜：**不该**打扰在线通道（省钱、省时）
# ======================================================================

def test_fresh_local_does_not_call_the_online_channel(monkeypatch) -> None:
    backend = _Backend([_Point(2.22, "2026-09-30")])
    text, seen = _run(monkeypatch, tolerance=30.0, stale_days=0.5, backend=backend)

    assert backend.calls == [], (
        "本地还新鲜却去联网了 —— 顺序（便宜的先试）被破坏，成本翻倍")
    assert "本地库" in text and "陈旧" not in text
    assert not seen, "新鲜情形不该产生跨通道异常（否则异常区变噪音）"
    snap = hop_stats.snapshot()
    assert snap["counters"][hop_stats.HOP2_LOCAL] == 1
    assert snap["total"] == 1, "一次查询只能记一次命中（分母口径）"


# ======================================================================
# 二、本地陈旧 + 在线更新 ⇒ 用在线（正当事由）
# ======================================================================

def test_stale_local_upgrades_to_the_online_value(monkeypatch) -> None:
    backend = _Backend([_Point(2.22, "2026-09-30")])
    text, seen = _run(monkeypatch, tolerance=1.0, stale_days=30.0, backend=backend)

    assert backend.calls == [KEY], "陈旧到违反声明新鲜度却没去在线取"
    assert "跨通道刷新" in text and "已用在线刷新" in text
    assert "2.22" in text, "返回的应当是在线值"
    snap = hop_stats.snapshot()
    assert snap["counters"][hop_stats.HOP3_CONNECTOR] == 1
    assert snap["counters"][hop_stats.HOP2_LOCAL] == 0, (
        "最终答出来的是第三跳 —— 第二跳不该也记一次命中")
    assert snap["total"] == 1
    assert seen and seen[0][0] == "cross_channel"
    assert "used_online" in seen[0][2]


# ======================================================================
# 三、本地陈旧但在线不可得 ⇒ 用本地 + **如实标注**
# ======================================================================

def test_stale_local_kept_when_online_unavailable(monkeypatch) -> None:
    backend = _Backend([])
    text, seen = _run(monkeypatch, tolerance=1.0, stale_days=30.0, backend=backend)

    assert backend.calls, "陈旧时应当**尝试**在线"
    assert "本地库" in text and "在线通道本次未取到" in text
    assert hop_stats.snapshot()["counters"][hop_stats.HOP2_LOCAL] == 1
    assert seen and "kept_local_stale" in seen[0][2]


# ======================================================================
# 四、同期次值不同 ⇒ **保留本地** + 留痕（不静默改数）
# ======================================================================

def test_same_period_different_value_is_flagged_not_overwritten(monkeypatch) -> None:
    backend = _Backend([_Point(9.99, "2026-09-01")])       # 与本地同期次、值不同
    text, seen = _run(monkeypatch, tolerance=1.0, stale_days=30.0,
                      backend=backend, period="2026-09-01", value=1.11)

    assert "数值不一致" in text, "同期次数值不一致必须被标注出来"
    assert "1.11" in text and "9.99" in text, "两边都要能被看见（否则无法复核）"
    # 正文的**数据行**仍必须是本地值（在线值只能出现在差异说明里）
    data_lines = [ln for ln in text.splitlines() if ln.startswith("- ")]
    assert data_lines, "没有数据行 —— 返回形状变了"
    assert all("9.99" not in ln for ln in data_lines), (
        f"数据行里出现了在线值 ⇒ 已经静默改数了：{data_lines}")
    assert any("1.11" in ln for ln in data_lines)
    assert seen and "kept_local_online_differs" in seen[0][2]
    assert "local_value=1.11" in seen[0][2] and "online_value=9.99" in seen[0][2]


# ======================================================================
# 五、未登记容忍度 ⇒ 不猜、不升级，但标注
# ======================================================================

def test_unregistered_tolerance_is_not_guessed(monkeypatch) -> None:
    backend = _Backend([_Point(2.22, "2026-09-30")])
    text, seen = _run(monkeypatch, tolerance=None, stale_days=9999.0, backend=backend)

    assert backend.calls == [], "没有声明的新鲜度就不该猜着去联网"
    assert "未登记新鲜度容忍" in text
    assert not seen, "未登记是「我们没有契约」，不是缺陷 ⇒ 不记异常"


# ======================================================================
# 六、契约接线是真的（不是"没人走的路"）
# ======================================================================

def test_tolerance_comes_from_the_real_registry() -> None:
    """★ 判据来源必须是**真登记表**：`stock_close:600036` 声明了 26h。

    这条防的是"机制写好了但查不到契约 ⇒ 永远不升级"——
    本项目实测过这种形状（`describe()` 零个生产调用方，而且不报错）。
    """
    days = supervisor._declared_tolerance_days("stock_close:600036")
    assert days is not None and days > 0, (
        "登记表查不到 stock_close:600036 的新鲜度 ⇒ 升级路径永远不会生效")
    assert supervisor._declared_tolerance_days("完全没这个指标") is None


# ======================================================================
# 七、自证：关掉容忍度 ⇒ 升级路径**必须**消失
# ======================================================================

def test_selfproof_without_tolerance_there_is_no_upgrade(monkeypatch) -> None:
    """★ 自证：把容忍度关掉（`None`）后，在线通道一次都不该被调用。

    先断言"开着容忍度时确实会调用"，再断言"关掉后不会" —— 两半都有才叫自证。
    """
    on = _Backend([_Point(2.22, "2026-09-30")])
    _run(monkeypatch, tolerance=1.0, stale_days=30.0, backend=on)
    assert on.calls, "前置条件不成立：开着容忍度时居然没去在线取"

    off = _Backend([_Point(2.22, "2026-09-30")])
    _run(monkeypatch, tolerance=None, stale_days=30.0, backend=off)
    assert off.calls == [], (
        "容忍度关掉后仍然联网 ⇒ 上面那些「陈旧才升级」的断言测的不是这个判据")
