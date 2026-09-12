"""调度作业运行记录（JSONL，满足SCHEDULER_DESIGN.md四的监控要求）。

每条记录：run_id/job_name/trigger/start_time/end_time/duration_ms/status/
records_processed/error_message/retries。
连续失败暂停为派生状态（最近N次全部failed），无需额外状态文件。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from src.scheduler.registry import PAUSE_AFTER_CONSECUTIVE_FAILURES


class RunLog:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def start(
        self, job_name: str, trigger: str = "schedule", retries: int = 0
    ) -> dict[str, Any]:
        return {
            "run_id": uuid.uuid4().hex[:16],
            "job_name": job_name,
            "trigger": trigger,
            "start_time": datetime.now().astimezone().isoformat(),
            "end_time": None,
            "duration_ms": None,
            "status": "running",
            "records_processed": 0,
            "error_message": None,
            "retries": retries,
        }

    def finish(
        self, record: dict[str, Any], *, status: str,
        records_processed: int = 0, error_message: str | None = None,
    ) -> dict[str, Any]:
        record["end_time"] = datetime.now().astimezone().isoformat()
        start = datetime.fromisoformat(record["start_time"])
        record["duration_ms"] = int(
            (datetime.now().astimezone() - start).total_seconds() * 1000
        )
        record["status"] = status
        record["records_processed"] = records_processed
        record["error_message"] = error_message
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def read_all(self, job_name: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        records = []
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if job_name is None or rec["job_name"] == job_name:
                    records.append(rec)
        return records[-limit:]

    def is_paused(self, job_name: str) -> bool:
        """最近N次执行全部失败 → 暂停（等人工介入）。"""
        history = [r for r in self.read_all(job_name, limit=10000)
                   if r["status"] in ("success", "failed")]
        tail = history[-PAUSE_AFTER_CONSECUTIVE_FAILURES:]
        return (
            len(tail) == PAUSE_AFTER_CONSECUTIVE_FAILURES
            and all(r["status"] == "failed" for r in tail)
        )

    def last_run(self, job_name: str) -> dict[str, Any] | None:
        rows = self.read_all(job_name, limit=1)
        return rows[0] if rows else None

    def daily_summary(self, day: str | None = None) -> dict[str, Any]:
        """指定日（YYYY-MM-DD，默认今天）执行报表：成功率/平均耗时/失败原因分布。"""
        day = day or datetime.now().strftime("%Y-%m-%d")
        rows = [
            r for r in self.read_all(limit=100000)
            if str(r.get("start_time", "")).startswith(day)
            and r["status"] in ("success", "failed")
        ]
        total = len(rows)
        failed = [r for r in rows if r["status"] == "failed"]
        durations = [r["duration_ms"] for r in rows if r["duration_ms"] is not None]
        reason_counts: dict[str, int] = {}
        for r in failed:
            reason = (r.get("error_message") or "未知错误")[:80]
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        return {
            "date": day,
            "total": total,
            "success": total - len(failed),
            "failed": len(failed),
            "success_rate": round((total - len(failed)) / total, 4) if total else None,
            "avg_duration_ms": int(sum(durations) / len(durations)) if durations else None,
            "failure_reasons": reason_counts,
        }

    def prune(self, ttl_days: int) -> int:
        """删除end_time早于ttl cutoff的记录，返回删除条数（全量重写）。"""
        if not self._path.exists():
            return 0
        cutoff = datetime.now().astimezone() - timedelta(days=ttl_days)
        kept, removed = [], 0
        for rec in self.read_all(limit=10_000_000):
            end = rec.get("end_time")
            if end:
                try:
                    if datetime.fromisoformat(end) < cutoff:
                        removed += 1
                        continue
                except ValueError:
                    pass
            kept.append(rec)
        with self._path.open("w", encoding="utf-8") as f:
            for rec in kept:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return removed
