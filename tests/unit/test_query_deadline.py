"""★ 查询**防撞钟**（用户 2026-09-29 口径：10 秒找不到就自动终止）。

## 现场（实测数字，不是设计偏好）

一次真实端到端里**墙钟 275s / 采集 254.5s**，其中 **251.4s 是单一指标**
（`大股东质押比例` 的"质押股东明细整表聚合"：126,826 行 / 254 个分页请求）。
用户看到的是"分析跑了四分半"，而它只在等一条数据。
取值 10s 的实测依据见 `src/core/intel_limits.py::QUERY_DEADLINE_SEC` 的表：
能用的族最慢 **6.7s**（板块资金流冷启动），10s 只切掉"不可达"与"病态慢"两类。

## 本文件守的四件事

1. **带上预算就真的会终止**（慢连接器 30s → 0.2s 预算下必须抛错，且错误里带防撞钟话术）；
2. **不传预算时行为不变**（定时作业/预热路径要能跑完重活，否则"预热养缓存"永远养不起来）；
3. **超时≠源坏了**：不许记失败冷却（否则一个只是慢的源会被永久踢出链）；
4. **A01 采集会把预算传下去**（契约：`CollectorPayload.deadline_sec`）。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from src.core.exceptions import DataFetchError
from src.core.intel_limits import QUERY_DEADLINE_SEC, deadline_reason
from src.core.schemas import DataPoint
from src.infrastructure.connectors.base import BaseConnector
from src.infrastructure.connectors.router import ConnectorRouter


class _SlowConnector(BaseConnector):
    """慢连接器替身：`sleep_sec` 后返回一个点。"""

    source_name = "慢源替身"
    source_url = "test://slow"

    def __init__(self, sleep_sec: float) -> None:
        self.sleep_sec = sleep_sec
        self.calls = 0

    @staticmethod
    def supports(indicator: str) -> bool:
        return indicator.startswith("slow:")

    async def fetch(self, indicator: str, start_date: str | None = None,
                    end_date: str | None = None) -> list[DataPoint]:
        self.calls += 1
        await asyncio.sleep(self.sleep_sec)
        return [DataPoint(indicator=indicator, value=1.0,
                          period_date="2026-09-29", source_name=self.source_name)]

    def get_capabilities(self) -> dict[str, Any]:
        return {"name": self.source_name, "indicators": ["slow:{code}"]}


def _router(connector: BaseConnector) -> ConnectorRouter:
    return ConnectorRouter([(connector, connector.supports)],
                           disable_cache=True, disable_db=True)


def test_default_deadline_is_ten_seconds() -> None:
    """默认值就是用户口径的 10 秒（改它要同步改实测表）。"""
    assert QUERY_DEADLINE_SEC == 10.0
    assert "防撞钟" in deadline_reason("x", QUERY_DEADLINE_SEC)


@pytest.mark.asyncio
async def test_deadline_terminates_a_slow_lookup() -> None:
    """★ 带 0.2s 预算 + 30s 的慢源 → 必须终止并抛出**带防撞钟话术**的错误。"""
    connector = _SlowConnector(sleep_sec=30.0)
    router = _router(connector)
    with pytest.raises(DataFetchError) as excinfo:
        await asyncio.wait_for(
            router.fetch("slow:600036", deadline_sec=0.2), timeout=5.0)
    message = str(excinfo.value)
    assert "防撞钟" in message, message
    assert "slow:600036" in message
    assert connector.calls == 1


@pytest.mark.asyncio
async def test_timeout_does_not_record_failure_cooldown() -> None:
    """★ 超时是"这次太慢"，不是"源坏了" —— **不许**记失败冷却。

    记冷却的后果：一个只是慢的源会被永久踢出链，而它下次可能 1 秒就返回
    （本项目把这类"修一个坏一个"登记过多次）。
    """
    connector = _SlowConnector(sleep_sec=30.0)
    router = _router(connector)
    with pytest.raises(DataFetchError):
        await asyncio.wait_for(
            router.fetch("slow:600036", deadline_sec=0.2), timeout=5.0)
    assert router._failure_cache == {}, "超时不许进失败冷却"  # noqa: SLF001


@pytest.mark.asyncio
async def test_without_deadline_slow_source_still_completes() -> None:
    """★ 不传预算 = 不限时：预热/定时作业要能把重活跑完（否则缓存永远养不起来）。"""
    connector = _SlowConnector(sleep_sec=0.3)
    router = _router(connector)
    points = await router.fetch("slow:600036")
    assert len(points) == 1 and points[0].value == 1.0


@pytest.mark.asyncio
async def test_deadline_within_budget_is_not_triggered() -> None:
    """预算够用时**不许**误杀（否则 10s 会变成"所有网络指标都拿不到"）。"""
    connector = _SlowConnector(sleep_sec=0.05)
    router = _router(connector)
    points = await router.fetch("slow:600036", deadline_sec=2.0)
    assert len(points) == 1


@pytest.mark.asyncio
async def test_collector_passes_deadline_to_backend() -> None:
    """契约：`CollectorPayload.deadline_sec` 必须被传到后端（否则设了也没用）。"""
    from src.core.models import AgentInput
    from src.domain.agents.data.collector.agent import DataCollectorAgent

    seen: dict[str, Any] = {}

    class _Backend:
        async def fetch(self, indicator: str, start_date: str | None = None,
                        end_date: str | None = None, *,
                        deadline_sec: float | None = None) -> list[DataPoint]:
            seen["deadline"] = deadline_sec
            return []

        def get_capabilities(self) -> dict[str, Any]:
            return {"name": "stub"}

    agent = DataCollectorAgent(_Backend())  # type: ignore[arg-type]
    await agent.execute(AgentInput(task_id="t", tenant_id="tenant_001",
                                   payload={"indicator": "CPI",
                                            "deadline_sec": 3.5}))
    assert seen["deadline"] == 3.5

    # 不传时保持 None（定时作业路径行为不变）
    await agent.execute(AgentInput(task_id="t", tenant_id="tenant_001",
                                   payload={"indicator": "CPI"}))
    assert seen["deadline"] is None


@pytest.mark.asyncio
async def test_collector_tolerates_backend_without_deadline_kwarg() -> None:
    """后端不认 `deadline_sec`（老实现/替身）→ 退回两参数调用，不许因此失败。"""
    from src.core.models import AgentInput
    from src.domain.agents.data.collector.agent import DataCollectorAgent

    class _LegacyBackend:
        async def fetch(self, indicator: str, start_date: str | None = None,
                        end_date: str | None = None) -> list[DataPoint]:
            return [DataPoint(indicator=indicator, value=2.0,
                              period_date="2026-09-29")]

        def get_capabilities(self) -> dict[str, Any]:
            return {"name": "legacy"}

    agent = DataCollectorAgent(_LegacyBackend())  # type: ignore[arg-type]
    out = await agent.execute(AgentInput(
        task_id="t", tenant_id="tenant_001",
        payload={"indicator": "CPI", "deadline_sec": 3.5}))
    assert out.result["data_points"], "退回两参数调用后仍应拿到数据"
