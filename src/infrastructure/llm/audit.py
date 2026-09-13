"""LLM调用哈希审计日志（JSONL追加写，不可变）。

每条记录：时间戳、任务层级、模型、prompt/response哈希、token数、
延迟、缓存命中、降级链——满足"所有LLM调用记录Trace"的架构红线。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

from src.infrastructure.llm.models import LLMResponse


class LLMAuditLog:
    """线程安全的JSONL审计追加器。"""

    def __init__(self, audit_dir: str = "data/audit") -> None:
        self._dir = Path(audit_dir)
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        self._dir.mkdir(parents=True, exist_ok=True)
        return self._dir / "llm_audit.jsonl"

    def record(
        self,
        *,
        trace_id: str,
        agent_id: str,
        task_tier: str,
        response: LLMResponse,
        cached: bool,
        error: str | None = None,
    ) -> dict:
        entry = {
            "ts": datetime.now().isoformat(),
            "trace_id": trace_id,
            "agent_id": agent_id,
            "task_tier": task_tier,
            "model": response.model_used,
            "provider": response.provider,
            "prompt_hash": response.prompt_hash,
            "response_hash": response.response_hash,
            "tokens_in": response.tokens_in,
            "tokens_out": response.tokens_out,
            "latency_ms": response.latency_ms,
            "cache_hit": cached,
            "cache_kind": response.cache_kind,
            "fallback_used": response.fallback_used,
            "provider_chain": response.provider_chain,
            "error": error,
        }
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return entry

    def read_all(self, limit: int | None = None) -> list[dict]:
        """按时间正序读取（limit取最近N条）。"""
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        if limit is not None:
            lines = lines[-limit:]
        return [json.loads(line) for line in lines if line.strip()]
