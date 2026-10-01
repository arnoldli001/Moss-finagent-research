"""A01 数据采集Agent：从数据源获取原始数据并携带溯源元数据。

无LLM（configs/agents.yaml model=none）；数据后端通过FetchBackend结构化
协议注入（infrastructure.connectors实现），domain不直接依赖infrastructure。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Protocol

from src.core.base_agent import BaseAgent
from src.core.exceptions import AgentExecutionError
from src.core.models import AgentInput, AgentOutput
from src.core.schemas import Confidence, DataPoint
from src.domain.agents.data.collector.logic import (
    build_collection_summary,
    build_reasoning_steps,
    format_gap_log,
    format_ok_log,
)
from src.domain.agents.data.collector.models import CollectorPayload

logger = logging.getLogger(__name__)


class FetchBackend(Protocol):
    """采集后端结构化协议（duck typing，避免domain→infrastructure反向依赖）。"""

    async def fetch(
        self,
        indicator: str,
        start_date: str | None = None,
        end_date: str | None = None,
        *,
        deadline_sec: float | None = None,
    ) -> list[DataPoint]: ...

    def get_capabilities(self) -> dict[str, Any]: ...


class DataCollectorAgent(BaseAgent):
    """A01：按指标拉取原始数据，输出DataPoint列表（含溯源元数据）。"""

    def __init__(self, backend: FetchBackend, agent_id: str = "A01_data_collector") -> None:
        super().__init__(agent_id)
        self._backend = backend
        #: 后端是否接受 `deadline_sec`（懒探测一次；见 `_accepts_deadline`）
        self._deadline_supported: bool | None = None

    def _accepts_deadline(self) -> bool:
        """后端认不认 `deadline_sec`（**按签名判**，不靠 try/except 探）。

        为什么不能"先带参调用、TypeError 再退回两参数"：那会**把后端内部的
        TypeError 也当成"它不认这个参数"**，于是同一个请求被重发一次 ——
        既可能重复副作用，又会把真正的 bug 掩盖成"参数不兼容"。
        """
        if self._deadline_supported is None:
            try:
                import inspect

                params = inspect.signature(self._backend.fetch).parameters
                self._deadline_supported = bool(
                    "deadline_sec" in params
                    or any(p.kind is inspect.Parameter.VAR_KEYWORD
                           for p in params.values()))
            except (TypeError, ValueError):   # 取不到签名（C 扩展/替身）→ 保守当不支持
                self._deadline_supported = False
        return bool(self._deadline_supported)

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "capabilities": ["fetch_api", "web_crawl", "file_download"],
            "backend": self._backend.get_capabilities(),
        }

    def health_check(self) -> bool:
        try:
            return bool(self._backend.get_capabilities())
        except Exception:
            return False

    async def execute(self, input: AgentInput) -> AgentOutput:
        payload = CollectorPayload.model_validate(input.payload)
        started = time.perf_counter()
        try:
            # ★ 防撞钟（`deadline_sec`）：**只有交互路径会传**（
            #   `supervisor._live_fetch_one` / A17 的 `query_data`）。
            #   定时作业/预热路径不传 ⇒ 后端行为与原来逐字一致 ——
            #   重活正是要在那里做（用户口径：「10 秒找不到就自动终止，
            #   防止撞钟过度等待」，取值依据见
            #   `src/core/intel_limits.py::QUERY_DEADLINE_SEC`）。
            if payload.deadline_sec is not None and self._accepts_deadline():
                points = await self._backend.fetch(
                    payload.indicator, payload.start_date, payload.end_date,
                    deadline_sec=payload.deadline_sec,
                )
            else:
                points = await self._backend.fetch(
                    payload.indicator, payload.start_date, payload.end_date
                )
        except Exception as exc:
            # ★ 2026-09-29 用户要求：「数据采集 agent 要记录**任何未能获取到的**
            #   信息日志，展示在后端日志里，方便查看采集效果」。
            #   这里记 **ERROR**（不是 info）：取数抛异常是真故障，
            #   而全仓库没有 root handler ⇒ INFO 会被静默丢弃（只兜 WARNING+）。
            logger.error("%s", format_gap_log(
                payload.indicator, f"{type(exc).__name__}: {exc}",
                kind="error",
                seconds=time.perf_counter() - started))
            raise AgentExecutionError(f"A01采集失败({payload.indicator}): {exc}") from exc

        duration_ms = int((time.perf_counter() - started) * 1000)
        elapsed_s = time.perf_counter() - started
        if points:
            logger.info("%s", format_ok_log(
                payload.indicator, points, seconds=elapsed_s))
        else:
            # 空结果 = "没量到"，**不是**"量到 0"（AGENTS.md 三种情形之一）。
            logger.warning("%s", format_gap_log(
                payload.indicator,
                "所有数据源都没有返回数据点（没量到 ≠ 量到 0）",
                kind="empty", seconds=elapsed_s))
        data_refs = [p.data_id for p in points]
        has_publish_time = all(p.publish_time for p in points)
        confidence = (
            Confidence.HIGH
            if points and has_publish_time
            else (Confidence.MEDIUM if points else Confidence.LOW)
        )

        return AgentOutput(
            task_id=input.task_id,
            agent_id=self.agent_id,
            conclusion=build_collection_summary(payload.indicator, points),
            confidence=confidence,
            data_refs=data_refs,
            reasoning_steps=build_reasoning_steps(
                payload.indicator, len(points), data_refs, duration_ms
            ),
            result={
                "indicator": payload.indicator,
                "data_points": [p.model_dump(mode="json") for p in points],
            },
        )
