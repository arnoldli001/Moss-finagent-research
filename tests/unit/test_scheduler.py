"""定时调度器测试：注册表/运行记录/作业执行/Beat计划（全离线，无Redis）。"""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.scheduler.celery_app import _parse_cron, celery_app
from src.scheduler.jobs import execute_job
from src.scheduler.registry import JOB_REGISTRY, alert_scan_schedule
from src.scheduler.run_log import RunLog
from src.scheduler.service import cron_due


class _FakeGraph:
    def __init__(self, errors=None, points=720):
        self.calls = 0
        self._errors = errors or []
        self._points = points

    async def ainvoke(self, state):
        self.calls += 1
        return {
            **state,
            "raw_points": [0] * self._points,
            "storage_stats": {"inserted": 0, "skipped": self._points,
                              "total": self._points},
            "errors": list(self._errors),
        }


class _FakeRuntime:
    def __init__(self, graph):
        self.graph = graph


# ---------- 注册表 ----------

def test_registry_crons_are_valid_five_fields():
    for spec in JOB_REGISTRY.values():
        assert len(spec.cron.split()) == 5
        _parse_cron(spec.cron)  # 不抛即合法
    assert "snapshot_macro" in JOB_REGISTRY
    assert "run_log_cleanup" in JOB_REGISTRY


def test_beat_schedule_built_from_registry():
    for name in JOB_REGISTRY:
        entry = celery_app.conf.beat_schedule[name]
        assert entry["task"] == "moss_finagent.scheduler.dispatch"
        assert entry["args"] == (name,)


# ---------- 运行记录 ----------

def test_run_log_finish_and_read(tmp_dir):
    log = RunLog(f"{tmp_dir}/sched/runs.jsonl")
    rec = log.start("snapshot_macro")
    log.finish(rec, status="success", records_processed=720)
    rows = log.read_all()
    assert len(rows) == 1
    assert rows[0]["status"] == "success"
    assert rows[0]["duration_ms"] >= 0
    assert rows[0]["trigger"] == "schedule"


def test_pause_after_three_consecutive_failures(tmp_dir):
    log = RunLog(f"{tmp_dir}/sched/runs.jsonl")
    for _ in range(2):
        log.finish(log.start("j"), status="failed", error_message="boom")
    assert log.is_paused("j") is False
    log.finish(log.start("j"), status="failed", error_message="boom")
    assert log.is_paused("j") is True
    # 一次成功即解除
    log.finish(log.start("j"), status="success")
    assert log.is_paused("j") is False


def test_daily_summary(tmp_dir):
    log = RunLog(f"{tmp_dir}/sched/runs.jsonl")
    log.finish(log.start("a"), status="success", records_processed=10)
    log.finish(log.start("a"), status="success", records_processed=20)
    log.finish(log.start("a"), status="failed", error_message="采集超时")
    summary = log.daily_summary(datetime.now().strftime("%Y-%m-%d"))
    assert summary["total"] == 3 and summary["success"] == 2
    assert summary["failed"] == 1
    assert summary["success_rate"] == round(2 / 3, 4)
    assert summary["avg_duration_ms"] is not None
    assert "采集超时" in next(iter(summary["failure_reasons"]))


def test_prune_removes_records_beyond_ttl(tmp_dir):
    path = Path(f"{tmp_dir}/sched/runs.jsonl")
    log = RunLog(path)
    old_time = (datetime.now().astimezone() - timedelta(days=100)).isoformat()
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"job_name": "j", "status": "success",
                            "start_time": old_time, "end_time": old_time,
                            "duration_ms": 1}) + "\n")
    log.finish(log.start("j"), status="success")
    assert len(log.read_all()) == 2
    removed = log.prune(90)
    assert removed == 1
    assert len(log.read_all()) == 1


# ---------- 作业执行 ----------

async def test_execute_macro_job_success(tmp_dir):
    log = RunLog(f"{tmp_dir}/sched/runs.jsonl")
    graph = _FakeGraph()
    rec = await execute_job(_FakeRuntime(graph), "snapshot_macro", log)
    assert rec["status"] == "success"
    assert rec["records_processed"] == 720
    assert graph.calls == 1


async def test_industry_watchlist_runs_each_target(tmp_dir):
    log = RunLog(f"{tmp_dir}/sched/runs.jsonl")
    graph = _FakeGraph()
    rec = await execute_job(
        _FakeRuntime(graph), "snapshot_industry_watchlist", log, trigger="manual")
    assert rec["status"] == "success"
    assert graph.calls == 4  # 半导体/煤炭/创新药/白酒


async def test_failed_graph_marks_job_failed(tmp_dir):
    log = RunLog(f"{tmp_dir}/sched/runs.jsonl")
    graph = _FakeGraph(errors=["A08_macro: LLM超时"])
    rec = await execute_job(_FakeRuntime(graph), "snapshot_macro", log)
    assert rec["status"] == "failed"
    assert "A08_macro" in rec["error_message"]


async def test_paused_job_skipped_for_schedule_but_manual_runs(tmp_dir):
    log = RunLog(f"{tmp_dir}/sched/runs.jsonl")
    for _ in range(3):
        log.finish(log.start("snapshot_macro"), status="failed", error_message="x")
    graph = _FakeGraph()
    skipped = await execute_job(
        _FakeRuntime(graph), "snapshot_macro", log, trigger="schedule")
    assert skipped["status"] == "skipped"
    assert graph.calls == 0

    manual = await execute_job(
        _FakeRuntime(graph), "snapshot_macro", log, trigger="manual")
    assert manual["status"] == "success"
    assert graph.calls == 1


async def test_cleanup_job_prunes_old_records(tmp_dir):
    path = Path(f"{tmp_dir}/sched/runs.jsonl")
    old_time = (datetime.now().astimezone() - timedelta(days=200)).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"job_name": "j", "status": "success",
                            "start_time": old_time, "end_time": old_time,
                            "duration_ms": 1}) + "\n")
    log = RunLog(path)
    rec = await execute_job(None, "run_log_cleanup", log)
    assert rec["status"] == "success"
    assert rec["records_processed"] == 1
    remaining = log.read_all()
    assert len(remaining) == 1  # 仅清理作业自身刚写入的记录
    assert remaining[0]["job_name"] == "run_log_cleanup"


async def test_unknown_job_raises(tmp_dir):
    log = RunLog(f"{tmp_dir}/sched/runs.jsonl")
    with pytest.raises(KeyError):
        await execute_job(None, "not_registered", log)


# ---------- 事件告警扫描作业 ----------

class _FakeEventService:
    def __init__(self, result):
        self.result = result
        self.triggers: list[str] = []

    async def run(self, trigger):
        self.triggers.append(trigger)
        return self.result


class _EventRuntime:
    def __init__(self, service):
        self.event_service = service


def test_event_alert_jobs_cover_open_noon_and_close():
    """用户口径 2026-09-24：盘中也要出告警（开盘/午盘各一次），收盘后补全量。"""
    intraday = JOB_REGISTRY["event_alert_intraday"]
    noon = JOB_REGISTRY["event_alert_noon"]
    daily = JOB_REGISTRY["event_alert_daily"]
    for spec in (intraday, noon, daily):
        assert spec.kind == "event_alert_scan"
        assert spec.name in JOB_REGISTRY
    assert intraday.cron == "30 9,10,13,14 * * 1-5"   # 09:30/10:30/13:30/14:30
    assert noon.cron == "0 12 * * 1-5"                # 午休时段跑，13:00 前到位
    assert daily.cron == "30 17 * * 1-5"              # 收盘后全量（原口径保留）

    schedule = alert_scan_schedule()
    assert schedule["slots"] == [
        "09:30", "10:30", "12:00", "13:30", "14:30", "17:30"]
    assert schedule["weekday_only"] is True
    assert schedule["startup_scan"] is True
    assert {j["job"] for j in schedule["jobs"]} == {
        "event_alert_intraday", "event_alert_noon", "event_alert_daily"}


def test_alert_cron_due_only_fires_on_intraday_points():
    """盘中班次只在点位那一分钟到期，且周末不触发。"""
    thursday = datetime(2026, 9, 24)  # 周四
    assert cron_due("30 9,10,13,14 * * 1-5",
                    thursday.replace(hour=9, minute=30))
    assert cron_due("0 12 * * 1-5", thursday.replace(hour=12, minute=0))
    assert not cron_due("30 9,10,13,14 * * 1-5",
                        thursday.replace(hour=9, minute=29))
    assert not cron_due("30 9,10,13,14 * * 1-5",
                        thursday.replace(hour=11, minute=30))
    saturday = datetime(2026, 9, 26)
    assert not cron_due("30 9,10,13,14 * * 1-5",
                        saturday.replace(hour=9, minute=30))


async def test_startup_scan_triggers_event_alert_once():
    """服务启动补扫：调用 trigger('event_alert_intraday', source='startup')。

    `delay=0` 只跳过那 90 秒让路等待，路径本身与生产一致。
    """
    import src.api.main as main_mod

    calls: list[tuple[str, str]] = []

    class _Scheduler:
        async def trigger(self, name, *, source="startup"):
            calls.append((name, source))
            return {"status": "success", "records_processed": 2}

    class _Runtime:
        event_service = object()

    await main_mod._run_event_alert_on_startup(
        _Scheduler(), _Runtime(), delay=0)
    assert calls == [("event_alert_intraday", "startup")]


async def test_startup_scan_skips_without_event_stack():
    """子系统没装配（或没有调度器）时不去写一条注定 failed 的运行记录。"""
    import src.api.main as main_mod

    calls: list[str] = []

    class _Scheduler:
        async def trigger(self, name, *, source="startup"):
            calls.append(name)
            return {}

    class _Runtime:
        event_service = None

    await main_mod._run_event_alert_on_startup(
        _Scheduler(), _Runtime(), delay=0)
    await main_mod._run_event_alert_on_startup(None, _Runtime(), delay=0)
    assert calls == []


async def test_startup_scan_swallows_errors():
    """补扫失败只记日志：扫描炸了也不能让服务起不来。"""
    import src.api.main as main_mod

    class _Scheduler:
        async def trigger(self, name, *, source="startup"):
            raise RuntimeError("全部快讯源被阻断")

    class _Runtime:
        event_service = object()

    await main_mod._run_event_alert_on_startup(
        _Scheduler(), _Runtime(), delay=0)


async def test_execute_event_scan_success(tmp_dir):
    from src.domain.alerts.models import ScanResult

    log = RunLog(f"{tmp_dir}/sched/runs.jsonl")
    service = _FakeEventService(ScanResult(
        status="success", scanned=10, new_events=3, alerts_created=1,
        by_level={"high": 1}))
    rec = await execute_job(
        _EventRuntime(service), "event_alert_daily", log, trigger="schedule")
    assert rec["status"] == "success"
    assert rec["records_processed"] == 1
    assert service.triggers == ["schedule"]


async def test_execute_event_scan_partial_and_failed(tmp_dir):
    from src.domain.alerts.models import ScanResult

    log = RunLog(f"{tmp_dir}/sched/runs2.jsonl")
    partial = _FakeEventService(ScanResult(
        status="partial", scanned=5, new_events=2, alerts_created=1,
        errors=["备源cls超时"]))
    rec = await execute_job(
        _EventRuntime(partial), "event_alert_daily", log)
    assert rec["status"] == "partial" and "备源cls超时" in rec["error_message"]

    failed = _FakeEventService(ScanResult(
        status="failed", scanned=0, errors=["全部快讯源无数据或全部被阻断"]))
    rec2 = await execute_job(
        _EventRuntime(failed), "event_alert_daily", log, trigger="manual")
    assert rec2["status"] == "failed" and rec2["error_message"]


async def test_execute_event_scan_without_service_marks_failed(tmp_dir):
    log = RunLog(f"{tmp_dir}/sched/runs3.jsonl")

    class _NoService:
        event_service = None

    rec = await execute_job(_NoService(), "event_alert_daily", log)
    assert rec["status"] == "failed"
    assert "event_service未装配" in rec["error_message"]


async def test_execute_event_scan_in_progress_marks_skipped(tmp_dir):
    """D6：扫描锁冲突时调度作业记skipped而非failed。"""
    from src.domain.alerts.service import ScanInProgressError

    class _BusyService:
        async def run(self, trigger):
            raise ScanInProgressError("扫描已在执行中")

    log = RunLog(f"{tmp_dir}/sched/runs4.jsonl")
    rec = await execute_job(
        _EventRuntime(_BusyService()), "event_alert_daily", log)
    assert rec["status"] == "skipped"
    assert "跳过" in rec["error_message"]

def test_every_registry_kind_has_a_handler() -> None:
    """JOB_REGISTRY 里**每个 `kind`** 都必须在 jobs.py 里有分发分支。

    ## 这条测试来自一次实测事故

    JOB_REGISTRY 声明了 intel_zsxq_collect（2 小时增量采集）与
    intel_token_alert（授权到期邮件提醒），而 jobs.py 的分发链里
    **没有**对应的 if spec.kind == ... —— 于是它们每次触发都走到最底下
    那句 未知作业类型，记一条 failed 就结束。后果正命中用户点名的两条：

      · "建议 2 小时一次"的采集**从来没跑过**
      · token 7/14 天过期**没有任何提醒**，只在前端静默变成"暂无更新"

    而且**不会有任何明显症状**：任务在 registry 里、面板上看得见，
    只是每次都是 failed —— 除非有人专门去翻运行记录，否则发现不了。

    ## ⚠️ 必须按 `kind` 判，不能按 registry 的 **key**

    第一版写成 `for kind in JOB_REGISTRY`（那是 **key**），于是报了 19 个
    "缺失" —— 全是假阳性，因为 `JobSpec.kind` 与 key **可以不同**：

        key="snapshot_macro"       kind="graph_snapshot"        ← 已有分支
        key="strategy_cases_daily" kind="strategy_cases_fetch"  ← 已有分支
        key="intel_zsxq_collect"   kind="intel_zsxq_collect"    ← 确实没有

    运行时分发用的是 **`spec.kind`**，所以判据必须是 kind 的**去重集合**。
    """
    import inspect

    from src.scheduler import jobs as jobs_module

    src = inspect.getsource(jobs_module)
    kinds = {spec.kind for spec in JOB_REGISTRY.values()}
    missing = sorted(k for k in kinds if f'spec.kind == "{k}"' not in src)
    assert not missing, (
        "这些 kind 在 JOB_REGISTRY 里声明了，但 jobs.py 没有分发分支 —— "
        f"它们每次触发都只会计一条「未知作业类型」的 failed：{missing}")


def test_registry_kind_is_a_nonempty_string() -> None:
    """`kind` 必须非空 —— 空串会让分发永远落到"未知作业类型"。"""
    for key, spec in JOB_REGISTRY.items():
        assert str(spec.kind or "").strip(), f"{key} 的 kind 为空"


def test_job_registry_cron_not_overcrowded() -> None:
    """同一个 cron 上不该挤太多任务（会同一秒抢资源）。

    这条是**提示线**而不是死规则：超过阈值的时刻值得人看一眼是不是
    设计成并行的。
    """
    from collections import Counter

    counts = Counter(spec.cron for spec in JOB_REGISTRY.values())
    crowded = {cron: n for cron, n in counts.items() if n > 4}
    assert not crowded, (
        f"这些 cron 上挤了超过 4 个任务：{crowded} —— "
        "确认它们是设计成并行的，否则错开分钟")
