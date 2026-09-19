"""行情仓库新鲜度 + 同步作业的单元测试。

背景（真实事故）：2026-09-18 09:10 手动跑「量化选股」，选出来的日期是
**20260915** —— 本地 Tushare 仓库里最新就到 0915。选股逻辑没错，
它忠实地用了"手上最新的一天"，但界面上完全看不出用的是两天前的行情。

这里锁住三件事：
  1. 「最近一个已收盘交易日」的算法（**盘中不算今天**，收盘后算今天）；
  2. 滞后时要能给出机器可判的 `data_stale` 与人话说明；
  3. 同步作业在"没有缺口 / 日历不可用 / 仓库不可用"时**不猜、不误报成功**。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from src.quant.freshness import (
    EOD_RELEASE,
    freshness,
    latest_complete_trade_date,
)
from src.scheduler import jobs as jobs_mod

#: 2026-09-14(一) ~ 09-18(五) 是交易日；09-19/20 是周末
WEEK = ["20260914", "20260915", "20260916", "20260917", "20260918"]


# ======================================================================
# 最近一个「已收盘」交易日
# ======================================================================


def test_intraday_does_not_count_today() -> None:
    """盘中的"最新可用"是**昨天** —— 今天的数据还没产生，不能当成应有数据。"""
    at_0925 = datetime(2026, 9, 18, 9, 25)
    assert latest_complete_trade_date(now=at_0925, days=WEEK) == "20260917"
    at_1445 = datetime(2026, 9, 18, 14, 45)
    assert latest_complete_trade_date(now=at_1445, days=WEEK) == "20260917"


def test_after_eod_release_today_counts() -> None:
    """过了 EOD 发布线（16:30）之后，今天的行情就算"应该有"了。"""
    assert EOD_RELEASE.strftime("%H:%M") == "16:30"
    after = datetime(2026, 9, 18, 17, 0)
    assert latest_complete_trade_date(now=after, days=WEEK) == "20260918"
    # 刚过线 1 分钟也算（留的余量已在 EOD_RELEASE 里）
    just = datetime(2026, 9, 18, 16, 31)
    assert latest_complete_trade_date(now=just, days=WEEK) == "20260918"


def test_before_release_line_still_yesterday() -> None:
    edge = datetime(2026, 9, 18, 16, 29)
    assert latest_complete_trade_date(now=edge, days=WEEK) == "20260917"


def test_weekend_resolves_to_friday() -> None:
    """周末没有新数据，应该拿周五。"""
    saturday = datetime(2026, 9, 19, 10, 0)
    assert latest_complete_trade_date(now=saturday, days=WEEK) == "20260918"
    sunday = datetime(2026, 9, 20, 23, 0)
    assert latest_complete_trade_date(now=sunday, days=WEEK) == "20260918"


def test_empty_calendar_is_unknown_not_today() -> None:
    """日历不可用时返回空串（判不了），**绝不退化成"就当成今天"**。"""
    assert latest_complete_trade_date(now=datetime(2026, 9, 18, 10, 0),
                                      days=[]) == ""


# ======================================================================
# freshness：滞后判定
# ======================================================================


def test_stale_run_is_flagged_with_a_readable_note() -> None:
    info = freshness("20260915", now=datetime(2026, 9, 18, 9, 10), days=WEEK)
    assert info["data_stale"] is True
    assert info["expected_trade_date"] == "20260917"
    assert info["checked"] is True
    # 说明里必须同时出现"实际用的"和"应该用的"，否则用户没法判断差几天
    assert "20260915" in info["note"] and "20260917" in info["note"]


def test_fresh_run_is_not_flagged() -> None:
    info = freshness("20260917", now=datetime(2026, 9, 18, 9, 10), days=WEEK)
    assert info["data_stale"] is False
    assert info["note"] == ""


def test_newer_than_expected_is_not_stale() -> None:
    """数据比"应有日期"还新（比如收盘后同步了当天）→ 不算滞后。"""
    info = freshness("20260918", now=datetime(2026, 9, 18, 17, 0), days=WEEK)
    assert info["data_stale"] is False


def test_unknown_calendar_reports_checked_false() -> None:
    """`checked=False` 表示**没判**，调用方不能读成"确认新鲜"。"""
    info = freshness("20260915", now=datetime(2026, 9, 18, 9, 10), days=[])
    assert info["checked"] is False
    assert info["data_stale"] is False
    assert info["note"] == ""


def test_missing_data_date_is_not_stale() -> None:
    info = freshness("", now=datetime(2026, 9, 18, 9, 10), days=WEEK)
    assert info["data_stale"] is False
    assert info["checked"] is False


# ======================================================================
# 同步作业：接线 + 不猜
# ======================================================================


def test_registry_declares_the_sync_job() -> None:
    """作业必须在 registry 里（cron 只允许在这一处声明）。"""
    from src.scheduler.registry import JOB_REGISTRY

    spec = JOB_REGISTRY["quant_data_sync"]
    assert spec.kind == "quant_data_sync"
    # 16:40 工作日：EOD 数据 15:00~16:00 才入库，且要赶在次日 09:25 选股之前
    assert spec.cron == "40 16 * * 1-5"
    assert "daily" in spec.params["datasets"]


def test_sync_uses_a_dataset_that_actually_exists() -> None:
    """`datasets` 里写的名字必须在仓库注册表里 —— 写错只会在凌晨静默失败。"""
    from src.quant.warehouse import DATASET_TABLES
    from src.scheduler.registry import JOB_REGISTRY

    for name in JOB_REGISTRY["quant_data_sync"].params["datasets"]:
        assert name in DATASET_TABLES, f"{name} 不是仓库数据集"


class _Spec:
    def __init__(self, **params):
        self.params = params
        self.name = "quant_data_sync"


def test_sync_refuses_to_guess_without_calendar(monkeypatch) -> None:
    """日历不可用 → 明确失败，而不是"随便补到今天"。"""
    monkeypatch.setattr(jobs_mod, "_latest_complete_trade_date", lambda: "")
    processed, detail = _run(jobs_mod._quant_data_sync(_Spec()))
    assert processed == 0
    assert detail.startswith("失败")
    assert "交易日历" in detail


class _Warehouse:
    def __init__(self, *, available: bool = True, latest: str = "20260915"):
        self._available = available
        self._latest = latest
        self.ingested: list[tuple[str, list[str]]] = []

    def available(self) -> bool:
        return self._available

    def load(self, dataset, **kwargs):  # noqa: ANN001, ANN003
        import pandas as pd

        if not self._latest:
            return pd.DataFrame()
        return pd.DataFrame({"trade_date": [self._latest]})

    def ingest_dataset(self, dataset, *, keys=None):  # noqa: ANN001
        self.ingested.append((dataset, list(keys or [])))
        return len(keys or [])


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_sync_is_a_noop_when_already_current(monkeypatch) -> None:
    """仓库已经到目标日 → 直接返回，**不下载、不写库**（幂等，可反复跑）。"""
    warehouse = _Warehouse(latest="20260917")
    monkeypatch.setattr(jobs_mod, "_latest_complete_trade_date",
                        lambda: "20260917")
    _patch_warehouse(monkeypatch, warehouse)

    processed, detail = _run(jobs_mod._quant_data_sync(_Spec()))

    assert processed == 0
    assert "已是最新" in detail
    assert warehouse.ingested == []


def test_sync_reports_failure_when_warehouse_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(jobs_mod, "_latest_complete_trade_date",
                        lambda: "20260917")
    _patch_warehouse(monkeypatch, _Warehouse(available=False))

    processed, detail = _run(jobs_mod._quant_data_sync(_Spec()))

    assert processed == 0
    assert detail.startswith("失败")
    assert "仓库不可用" in detail


def _patch_warehouse(monkeypatch, warehouse) -> None:
    """把 `_quant_data_sync` 内部 import 的 QuantWarehouse 换成假的。

    函数体里是**延迟 import**（避免调度器启动就拖起 15GiB 的库连接），
    所以补丁要打在 `src.quant.warehouse` 模块属性上，而不是 jobs 模块上。
    """
    import src.quant.warehouse as warehouse_mod

    monkeypatch.setattr(warehouse_mod, "QuantWarehouse", lambda *a, **k: warehouse)


# ======================================================================
# 小工具
# ======================================================================


@pytest.mark.parametrize(("stamp", "expect"), [
    ("20260915", "20260916"),
    ("20260930", "20261001"),
    ("20261231", "20270101"),
    ("bad", "bad"),
])
def test_next_day(stamp, expect) -> None:
    assert jobs_mod._next_day(stamp) == expect


def test_warehouse_latest_reads_max_trade_date() -> None:
    assert jobs_mod._warehouse_latest(_Warehouse(latest="20260917"),
                                      "daily") == "20260917"
    assert jobs_mod._warehouse_latest(_Warehouse(latest=""), "daily") == ""
