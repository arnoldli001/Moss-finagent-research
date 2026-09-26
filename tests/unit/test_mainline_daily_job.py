"""`mainline_daily` 作业的失败判定：**可选数据集失败不该判整轮失败**。

## 为什么单独立一条测试（2026-09-23 实测）

`sync_all` 的 `board_crowding` / `member_crowding` 要读应用库里的
`sector_crowding_list` / `sector_member`，而这两张表只在生产库建 ——
`--env dev` 的隔离库里没有，于是 dev 下**天天 failed**。

`_mainline_daily` 原来是"任一数据集 failed 就 `detail["failed"]=True`"，
而 `execute_job` 会据此把作业记成 failed。后果不是"多几条红字"：

    RunLog.is_paused() = 最近 N 次全部 failed → 作业被**自动暂停**
    （PAUSE_AFTER_CONSECUTIVE_FAILURES = 3）

被暂停的是**整个每日同步**（`sync_all(datasets=None)`）—— 连带
`board_bar` / `board_flow` 也一起停了，界面表现是"主线面板的日期再也不动"，
而台账里只写着 crowding 两档失败，看不出真正被停的是别的东西。

所以：可选档失败 → 记名（`message` 里写明）但**不判失败**。
"""

from __future__ import annotations

import asyncio

import pytest

from src.scheduler import jobs as jobs_mod


class _Spec:
    name = "mainline_daily"
    params = {"sync_days": 5, "notify": False}


class _Snapshot:
    trade_date = "20260923"
    board_count_total = 138
    candidate_count = 28
    selected_count = 10
    alerts: list = []


def _patch_mainline(monkeypatch, results):
    """把 `_mainline_daily` 里延迟 import 的四个依赖换成假的。"""
    import src.mainline.config as config_mod
    import src.mainline.datastore as datastore_mod
    import src.mainline.notify as notify_mod
    import src.mainline.service as service_mod
    import src.mainline.storage as storage_mod

    class _Store:
        def __init__(self, *_a, **_k):
            pass

    class _Service:
        def __init__(self, *_a, **_k):
            pass

        async def score_date(self, _date, save=False):  # noqa: ANN001, FBT002
            return _Snapshot()

    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: object())
    monkeypatch.setattr(datastore_mod, "MainlineDataStore", _Store)
    monkeypatch.setattr(datastore_mod, "sync_all",
                        lambda *a, **k: list(results))
    monkeypatch.setattr(service_mod, "MainlineService", _Service)
    monkeypatch.setattr(storage_mod, "build_mainline_repository",
                        lambda *a, **k: None)
    monkeypatch.setattr(notify_mod, "push_alerts", lambda *a, **k: [])


def _result(dataset: str, status: str = "ok"):
    from src.mainline.datastore import SyncResult

    return SyncResult(dataset=dataset, rows=1, status=status,
                      message="no such table: x" if status == "failed" else "")


def test_optional_dataset_failure_does_not_fail_the_job(monkeypatch) -> None:
    """crowding 两档 failed、其余正常 → 作业**不算失败**，但说明里要看得见。"""
    _patch_mainline(monkeypatch, [
        _result("board_crowding", "failed"),
        _result("member_crowding", "failed"),
        _result("board_bar"),
        _result("board_flow"),
    ])

    _processed, detail = asyncio.run(jobs_mod._mainline_daily(_Spec()))

    assert detail["failed"] is False, "可选数据集失败不该判作业失败"
    assert "可选数据集未同步" in detail["message"]
    assert "board_crowding" in detail["message"]
    assert "20260923" in detail["message"]


def test_real_dataset_failure_still_fails_the_job(monkeypatch) -> None:
    """非可选档失败仍然要判 failed —— 别把这条一起放宽了。"""
    _patch_mainline(monkeypatch, [
        _result("board_crowding", "failed"),
        _result("board_bar", "failed"),
    ])

    _processed, detail = asyncio.run(jobs_mod._mainline_daily(_Spec()))

    assert detail["failed"] is True
    assert "同步失败 1 项" in detail["message"], "只有 board_bar 该计入失败数"


@pytest.mark.parametrize("name", ["board_bar", "board_flow", "index", "etf", "seat"])
def test_scoring_inputs_are_not_optional(name: str) -> None:
    """决定打分口径的数据集**不许**进可选名单（写错等于把真故障静音）。"""
    assert name not in jobs_mod._MAINLINE_OPTIONAL_DATASETS
