"""定时调度器测试：注册表/运行记录/作业执行/Beat计划（全离线，无Redis）。"""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.scheduler.celery_app import _parse_cron, celery_app
from src.scheduler.jobs import execute_job
from src.scheduler.registry import JOB_REGISTRY
from src.scheduler.run_log import RunLog


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
