"""`sync_gap` 的判定 + 启动自检的行为。

回归的事故（2026-09-23）：用户报「量化选股用的是 20260917 的行情，最近一个
已收盘交易日是 20260922」。根因是 `stk_limit` **分区已到 0922、仓库停在 0917**
（下载了没灌库），而这个缺口在健康度页面上**看不见** —— 分区一列与仓库一列
各自都很正常。本模块把这个缺口变成显式判定，启动时自检并自动补。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from src.quant.sync_gap import (
    DAILY_DATASETS,
    build_sync_gap,
    partition_last_map,
    warehouse_last_map,
)


def _gap(partition: dict[str, str], warehouse: dict[str, str],
         expected: str = "20260922",
         datasets: tuple[str, ...] | list[str] = DAILY_DATASETS) -> dict[str, Any]:
    return build_sync_gap(expected=expected, partition_last=partition,
                          warehouse_last=warehouse, datasets=datasets)


# ======================================================================
# 一、三种状态必须分开：已同步 / 已下载未灌库 / 两者都落后
# ======================================================================


def test_synced_when_warehouse_reaches_expected() -> None:
    out = _gap({"daily": "20260922"}, {"daily": "20260922"}, datasets=["daily"])
    assert out["synced"] is True
    assert out["behind"] == []
    assert out["note"] == ""


def test_partition_ahead_is_need_ingest_not_need_download() -> None:
    """**这是本次事故的核心分支**：数据已经在本地，只差一步 ingest。

    把它和"需要联网下载"混为一谈，就会去重新下载一份早就有的数据，
    而真正的动作（灌库）反而没做 —— 2026-09-23 的 `stk_limit` 正是如此。
    """
    out = _gap({"stk_limit": "20260922"}, {"stk_limit": "20260917"},
               datasets=["stk_limit"])
    assert out["synced"] is False
    assert out["need_ingest"] == ["stk_limit"]
    assert out["need_download"] == []
    assert out["rows"][0]["state"] == "need_ingest"
    assert out["rows"][0]["label"] == "已下载未灌库"
    assert "已下载但未灌库" in out["note"]


def test_both_behind_is_need_download() -> None:
    """分区也不比仓库新 → 必须先联网下载（`moneyflow` 实测就是这类）。"""
    out = _gap({"moneyflow": "20260915"}, {"moneyflow": "20260915"},
               datasets=["moneyflow"])
    assert out["need_download"] == ["moneyflow"]
    assert out["need_ingest"] == []
    assert "分区与仓库都落后" in out["note"]


def test_mixed_gap_reports_both_kinds() -> None:
    """真实现场：一个已下载未灌库 + 一个两者都落后，必须同时说清。"""
    out = _gap(
        {"stk_limit": "20260922", "moneyflow": "20260915", "daily": "20260922"},
        {"stk_limit": "20260917", "moneyflow": "20260915", "daily": "20260922"},
        datasets=["daily", "stk_limit", "moneyflow"])
    assert out["need_ingest"] == ["stk_limit"]
    assert out["need_download"] == ["moneyflow"]
    assert "stk_limit 已下载但未灌库" in out["note"]
    assert "moneyflow 分区与仓库都落后" in out["note"]
    # daily 已经是最新的，不该出现在 behind 里
    assert "daily" not in out["behind"]


# ======================================================================
# 二、判不了就说不判定（缺数据不猜）
# ======================================================================


def test_no_calendar_means_unchecked_and_no_claim() -> None:
    """交易日历不可用 → `checked=False`，**不能**报告成"已同步"。"""
    out = _gap({"daily": "20260922"}, {"daily": "20260922"}, expected="",
               datasets=["daily"])
    assert out["checked"] is False
    assert out["synced"] is False
    assert out["rows"][0]["state"] == "unknown"
    assert "不猜" in out["note"]


def test_missing_warehouse_stat_is_unknown_not_ok() -> None:
    """仓库侧没有这个数据集的统计 → unknown。**不能**当成已同步。"""
    out = _gap({"daily": "20260922"}, {}, datasets=["daily"])
    assert out["rows"][0]["state"] == "unknown"
    assert out["unknown"] == ["daily"]
    assert out["synced"] is False


def test_missing_partition_stat_with_warehouse_behind_is_need_download() -> None:
    """分区统计缺失但仓库确实落后 → 保守判为"需要下载"（而不是 unknown）。

    理由：仓库落后是**仓库侧给出的确定事实**，这一条已经足够触发补数据；
    分区侧缺失只影响"能不能省掉下载"，不该让整条判定失效。
    """
    out = _gap({}, {"daily": "20260917"}, datasets=["daily"])
    assert out["rows"][0]["state"] == "need_download"


def test_unknown_datasets_block_the_synced_claim() -> None:
    """已同步但有个别数据集缺统计 → **不能**声称已同步。

    `synced` 的语义是「已确认全部同步」，不是「没发现落后」。两者在
    "全都判定不了"时会分叉 —— 这个 bug 正是写完测试才发现的：
    原来缺统计时报 `synced=True`，启动自检于是认为一切正常、连补偿都不做。
    """
    out = _gap({"daily": "20260922"}, {"daily": "20260922", "daily_basic": ""},
               datasets=["daily", "daily_basic"])
    assert out["unknown"] == ["daily_basic"]
    assert out["synced"] is False, "缺统计时必须报「未确认」，而不是「已同步」"
    assert "无法确认" in out["note"]
    assert "不当作已同步" in out["note"]


# ======================================================================
# 三、健康度两段 payload → 两张水位表
# ======================================================================


def test_partition_last_map_reads_tushare_payload() -> None:
    payload = {"datasets": [{"dataset": "daily", "last": "20260922"},
                            {"dataset": "moneyflow", "last": "20260915"},
                            {"last": "20260922"}]}   # 缺 dataset 名的条目要跳过
    assert partition_last_map(payload) == {"daily": "20260922",
                                          "moneyflow": "20260915"}


def test_warehouse_last_map_reads_warehouse_payload() -> None:
    payload = {"tables": [{"dataset": "daily", "last": "20260922"},
                          {"dataset": "stk_limit", "last": "20260917"}]}
    assert warehouse_last_map(payload) == {"daily": "20260922",
                                           "stk_limit": "20260917"}


@pytest.mark.parametrize("payload", [{}, {"datasets": None}, {"datasets": []}])
def test_maps_tolerate_empty_payloads(payload: dict) -> None:
    assert partition_last_map(payload) == {}


# ======================================================================
# 四、启动自检：缺了就补，不缺就不动，异常不外抛
# ======================================================================


class _Scheduler:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def trigger(self, name: str, *, source: str = "startup") -> dict:
        self.calls.append((name, source))
        return {"status": "success", "records_processed": 7}


def _run_startup_check(monkeypatch, gap: dict, *, delay: float = 0.0):
    """跑一次启动自检，返回 `(调度器, 异常)`。"""
    from src.api import data_health
    from src.api import main as main_mod

    monkeypatch.setattr(data_health, "build_data_health",
                        lambda runtime, force=False: {"sync_gap": gap})
    scheduler = _Scheduler()
    error: Exception | None = None
    try:
        asyncio.run(main_mod._check_quant_sync_at_startup(
            scheduler, object(), delay=delay))
    except Exception as exc:  # noqa: BLE001 测试要断言"不外抛"
        error = exc
    return scheduler, error


def test_startup_check_triggers_catchup_when_behind(monkeypatch) -> None:
    gap = _gap({"stk_limit": "20260922"}, {"stk_limit": "20260917"},
               datasets=["stk_limit"])
    scheduler, error = _run_startup_check(monkeypatch, gap)
    assert error is None
    assert scheduler.calls == [("quant_data_sync", "startup")], \
        "发现缺口时必须触发一次补偿同步"


def test_startup_check_does_nothing_when_synced(monkeypatch) -> None:
    gap = _gap({"daily": "20260922"}, {"daily": "20260922"}, datasets=["daily"])
    scheduler, error = _run_startup_check(monkeypatch, gap)
    assert error is None
    assert scheduler.calls == [], "已同步时不该触发任何作业"


def test_startup_check_does_not_guess_without_calendar(monkeypatch) -> None:
    """判不了就不补（不猜）——避免在没有日历的情况下误触发下载。"""
    gap = _gap({}, {}, expected="", datasets=["daily"])
    scheduler, error = _run_startup_check(monkeypatch, gap)
    assert error is None
    assert scheduler.calls == []


def test_startup_check_swallows_trigger_failure(monkeypatch) -> None:
    """调度器抛异常也不能影响服务启动 —— 自检不是前置条件。"""
    from src.api import data_health
    from src.api import main as main_mod

    gap = _gap({"stk_limit": "20260922"}, {"stk_limit": "20260917"},
               datasets=["stk_limit"])
    monkeypatch.setattr(data_health, "build_data_health",
                        lambda runtime, force=False: {"sync_gap": gap})

    class _Boom:
        async def trigger(self, *_a, **_k):
            raise RuntimeError("调度器炸了")

    error: Exception | None = None
    try:
        asyncio.run(main_mod._check_quant_sync_at_startup(_Boom(), object()))
    except Exception as exc:  # noqa: BLE001
        error = exc
    assert error is None


def test_startup_check_survives_health_build_failure(monkeypatch) -> None:
    """健康度组装失败（仓库/分区统计读不出来）同样不许外抛。"""
    from src.api import data_health
    from src.api import main as main_mod

    def _boom(runtime, force=False):
        raise RuntimeError("仓库统计炸了")

    monkeypatch.setattr(data_health, "build_data_health", _boom)
    error: Exception | None = None
    try:
        asyncio.run(main_mod._check_quant_sync_at_startup(_Scheduler(), object()))
    except Exception as exc:  # noqa: BLE001
        error = exc
    assert error is None


# ======================================================================
# 五、自检「做过没有」必须可观测
# ======================================================================


def test_startup_check_records_its_outcome(monkeypatch) -> None:
    """自检要把结论记下来 —— 否则"没消息"与"没运行"分不清。

    背景：自检成功只打 `logger.info`，而 uvicorn 默认把应用侧 logger 停在
    WARNING，于是"启动时查过、结论已同步"在日志里完全看不到。记进
    `last_check()` 并挂到健康度载荷上，页面才确认得了它跑过。
    """
    from src.quant import sync_gap as mod

    monkeypatch.setattr(mod, "_LAST_CHECK", {})
    gap = _gap({"daily": "20260922"}, {"daily": "20260922"}, datasets=["daily"])
    _run_startup_check(monkeypatch, gap)

    recorded = mod.last_check()
    assert recorded["expected"] == "20260922"
    assert recorded["synced"] is True
    assert recorded["action"] == "none"
    assert recorded["checked_at"], "要带时间戳，否则看不出是什么时候查的"


def test_startup_check_records_the_catchup_it_triggered(monkeypatch) -> None:
    from src.quant import sync_gap as mod

    monkeypatch.setattr(mod, "_LAST_CHECK", {})
    gap = _gap({"stk_limit": "20260922"}, {"stk_limit": "20260917"},
               datasets=["stk_limit"])
    scheduler, _error = _run_startup_check(monkeypatch, gap)

    recorded = mod.last_check()
    assert scheduler.calls == [("quant_data_sync", "startup")]
    assert recorded["synced"] is False
    assert recorded["behind"] == ["stk_limit"]
    assert recorded["action"] == "已触发补偿同步"
    assert recorded["job_status"] == "success"


def test_last_check_is_empty_before_any_run(monkeypatch, tmp_path) -> None:
    """没有记录时返回空 dict —— 不编造「已检查」。

    ⚠️ 必须同时把**落盘路径**指到临时目录：`last_check()` 在进程内为空时会
    回退读 `data/quant/sync_check.json`，不隔离的话这个测试会读到本机真实
    运行留下的记录，于是"没跑过"这条断言在开发机上永远失败、在 CI 上却通过。
    """
    from src.quant import sync_gap as mod

    monkeypatch.setattr(mod, "_LAST_CHECK", {})
    monkeypatch.setattr(mod, "_check_file",
                        lambda: tmp_path / "missing.json")
    assert mod.last_check() == {}


def test_record_check_persists_to_disk(monkeypatch, tmp_path) -> None:
    """记录要落盘：内存里的那份进程外看不到，页面/排查就无从确认自检跑过。"""
    import json

    from src.quant import sync_gap as mod

    monkeypatch.setattr(mod, "_LAST_CHECK", {})
    target = tmp_path / "sync_check.json"
    monkeypatch.setattr(mod, "_check_file", lambda: target)

    mod.record_check({"checked_at": "2026-09-23 08:55:00",
                      "expected": "20260922", "synced": True})

    assert json.loads(target.read_text(encoding="utf-8"))["synced"] is True
    # 清空内存后仍读得到 → 证明它真的落在盘上，而不只是留在进程里
    monkeypatch.setattr(mod, "_LAST_CHECK", {})
    assert mod.last_check()["expected"] == "20260922"


def test_record_check_never_raises_on_unwritable_path(monkeypatch) -> None:
    """写不进盘（只读介质等）不能影响启动 —— 自检不是前置条件。"""
    from src.quant import sync_gap as mod

    monkeypatch.setattr(mod, "_LAST_CHECK", {})
    monkeypatch.setattr(mod, "_check_file",
                        lambda: __import__("pathlib").Path("/\0bad/sync.json"))
    mod.record_check({"synced": True})            # 不该抛
    assert mod.last_check()["synced"] is True    # 内存那份仍然有效
