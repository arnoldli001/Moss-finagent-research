"""调度作业运行记录（JSONL，满足SCHEDULER_DESIGN.md四的监控要求）。

每条记录：run_id/job_name/trigger/start_time/end_time/duration_ms/status/
records_processed/error_message/detail/retries。
连续失败暂停为派生状态（最近N次全部failed），无需额外状态文件。

## `detail` 为什么必须存在（2026-10-07 实测）

作业函数返回 `(处理条数, 说明)`，但**成功时说明被丢掉了**：
分发链写的是 `error_message="" if status == "success" else detail[:500]`
（`jobs.py`），而 `service.py` 的完成日志又只打 `status` 与条数。
两层都丢 ⇒ 一条成功的作业在**任何地方都看不到它做了什么**。

代价是实测出来的：`intel_token_alert`（授权到期提醒）连续 12 天
`status=success、records_processed=0`，看不出"没提醒"还是"提醒了"，
更看不出原因是"收件人是占位地址"。用户的原话是"脚本没提示"。

`error_message` 的语义（= 失败原因）不动，另开 `detail` 字段承载
"这次干了什么/为什么没干" —— 成功与失败都要有。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from src.scheduler.registry import (
    PAUSE_AFTER_CONSECUTIVE_FAILURES, PAUSE_COOLDOWN_DAYS,
)


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
            "detail": None,
            "retries": retries,
        }

    def finish(
        self, record: dict[str, Any], *, status: str,
        records_processed: int = 0, error_message: str | None = None,
        detail: str | None = None,
    ) -> dict[str, Any]:
        record["end_time"] = datetime.now().astimezone().isoformat()
        start = datetime.fromisoformat(record["start_time"])
        record["duration_ms"] = int(
            (datetime.now().astimezone() - start).total_seconds() * 1000
        )
        record["status"] = status
        record["records_processed"] = records_processed
        record["error_message"] = error_message
        # `None` 与 `""` 要分开：`None` = 这条路径没提供说明，`""` = 提供了空说明。
        # 读侧（前端 `?? ""`）两者都当空处理，但写侧不能把"没提供"写成"空的"，
        # 否则以后想区分"没接 detail"与"detail 就是空"就没有依据了。
        record["detail"] = detail
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

    def paused_since(self, job_name: str) -> datetime | None:
        """如果当前是"暂停"状态，返回**最后一次失败**的时间；否则 `None`。

        与 `is_paused()` 的分工：`is_paused()` 回答"要不要跳过"，
        `paused_since()` 回答"这个暂停有多久了" —— 冷却期判定要用后者。
        """
        history = [r for r in self.read_all(job_name, limit=10000)
                   if r["status"] in ("success", "partial", "failed")]
        tail = history[-PAUSE_AFTER_CONSECUTIVE_FAILURES:]
        if (len(tail) != PAUSE_AFTER_CONSECUTIVE_FAILURES
                or not all(r["status"] == "failed" for r in tail)):
            return None
        raw = tail[-1].get("end_time") or tail[-1].get("start_time") or ""
        try:
            return datetime.fromisoformat(str(raw))
        except ValueError:
            return None

    def is_paused(self, job_name: str) -> bool:
        """最近 N 次执行全部失败 → 暂停（等人工介入）。**暂停会过期**。

        partial（部分指标成功）与 success 一样中断连续失败计数。

        ## ★ 为什么暂停必须"会过期"（2026-10-30 事故）

        原来的实现只看"最后 N 次是不是全失败"，**没有时间维度**。后果实测：
        `mainline_daily` 在 10-08/10-09/10-12 连续失败三次后，从 10-12 一直
        暂停到 10-30（**18 天**）—— 中间哪怕原因已经修好、哪怕服务器重启了
        十几次，它都**再没有被尝试过一次**。一个"防狂撞"的机制变成了
        "永久停机"，而且只有一行 INFO 日志。

        ⇒ 加**冷却期**：暂停超过 `PAUSE_COOLDOWN_DAYS` 天就自动放行一次。
        这样既保住原意（不每 30 分钟撞同一堵墙），又不会"永远不试"。
        仍然失败 ⇒ 继续暂停，且 `data_freshness_check` 每天会把
        "这个作业处于暂停"报出来（`PAUSED_job`）。
        """
        since = self.paused_since(job_name)
        if since is None:
            return False
        # 时区：`RunLog` 写的是 `astimezone()` 的本地时间，两边都带 tzinfo；
        # 若历史记录没有 tzinfo（早期版本），按本地时间补齐后再比。
        now = datetime.now().astimezone()
        if since.tzinfo is None:
            since = since.astimezone()
        return (now - since) < timedelta(days=PAUSE_COOLDOWN_DAYS)

    def last_run(self, job_name: str) -> dict[str, Any] | None:
        rows = self.read_all(job_name, limit=1)
        return rows[0] if rows else None

    def daily_summary(self, day: str | None = None) -> dict[str, Any]:
        """指定日（YYYY-MM-DD，默认今天）执行报表：成功率/平均耗时/失败原因分布。"""
        day = day or datetime.now().strftime("%Y-%m-%d")
        rows = [
            r for r in self.read_all(limit=100000)
            if str(r.get("start_time", "")).startswith(day)
            and r["status"] in ("success", "partial", "failed")
        ]
        total = len(rows)
        failed = [r for r in rows if r["status"] == "failed"]
        partial = [r for r in rows if r["status"] == "partial"]
        durations = [r["duration_ms"] for r in rows if r["duration_ms"] is not None]
        reason_counts: dict[str, int] = {}
        for r in failed + partial:
            reason = (r.get("error_message") or "未知错误")[:80]
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        return {
            "date": day,
            "total": total,
            "success": total - len(failed) - len(partial),
            "partial": len(partial),
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
