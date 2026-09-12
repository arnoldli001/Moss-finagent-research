"""API集成测试（ASGI内存传输，注入FakeRuntime，不联网）。"""

import asyncio

import httpx
import pytest

from src.api.main import app
from src.api.tasks import TaskStore
from src.infrastructure.repositories.macro_repo import MacroRepository


class FakeAuditLog:
    def read_all(self, limit=None):
        return [{
            "trace_id": "task_x", "model": "fake", "provider": "fake",
            "prompt_hash": "ph", "response_hash": "rh", "tokens_in": 10,
            "tokens_out": 5, "latency_ms": 12, "cache_hit": False,
            "cache_kind": "none", "fallback_used": False,
            "provider_chain": ["local_light"], "error": None,
        }]


class FakeGateway:
    audit_log = FakeAuditLog()


class HealthyAgent:
    def __init__(self, agent_id):
        self.agent_id = agent_id

    def health_check(self):
        return True


class FakeGraph:
    async def ainvoke(self, state):
        return {
            **state,
            "agent_outputs": [
                {"agent_id": "A08_macro", "conclusion": "复苏", "confidence": "medium",
                 "data_refs": ["d_CPI"], "result": {}},
                {"agent_id": "A17_recommend", "conclusion": "综合看多",
                 "confidence": "high", "data_refs": ["A08_macro"], "result": {"stance": "看多"}},
            ],
            "errors": [],
            "final_report": "# 报告\n⚠️ 不构成投资建议",
        }


def _install_app_state(tmp_dir):
    app.state.store = TaskStore()
    app.state.runtime = type("R", (), {})()
    app.state.runtime.gateway = FakeGateway()
    app.state.runtime.repo = MacroRepository(db_path=f"{tmp_dir}/api.db")
    app.state.runtime.agents = {"A08_macro": HealthyAgent("A08_macro")}
    app.state.runtime.graph = FakeGraph()
    return app.state


async def _wait_done(store, task_id, timeout=2.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        record = store.get(task_id)
        if record and record.status in ("completed", "failed"):
            return record
        await asyncio.sleep(0.01)
    raise AssertionError("task not finished in time")


@pytest.fixture
def client(tmp_dir):
    state = _install_app_state(tmp_dir)
    return state, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def test_analyze_flow(client):
    state, http = client
    async with http:
        resp = await http.post("/api/v1/research/analyze", json={
            "query": "分析茅台", "analysis_type": "full", "target": "600519",
        })
        assert resp.status_code == 202
        task_id = resp.json()["task_id"]
        assert resp.json()["status"] == "queued"

        record = await _wait_done(state.store, task_id)
        assert record.status == "completed"

        detail = await http.get(f"/api/v1/research/{task_id}")
        body = detail.json()
        assert body["status"] == "completed"
        assert body["conclusion"] == "综合看多" and body["confidence"] == "high"
        assert "不构成投资建议" in body["report"]


async def test_analyze_unknown_task_404(client):
    _, http = client
    async with http:
        assert (await http.get("/api/v1/research/nope")).status_code == 404
        assert (await http.get("/api/v1/trace/nope")).status_code == 404


async def test_data_macro_query(client, tmp_dir):
    state, http = client
    from src.core.schemas import DataPoint

    await state.runtime.repo.save_points([
        DataPoint(indicator="CPI", value=2.1, period_date="2026-08-01",
                  source_name="AkShare", source_url="https://x"),
    ], "t1")
    async with http:
        resp = await http.get("/api/v1/data/macro", params={"indicator": "CPI"})
        body = resp.json()
        assert resp.status_code == 200
        assert body["count"] == 1
        assert body["data_points"][0]["source_name"] == "AkShare"


async def test_trace_endpoint(client):
    state, http = client
    async with http:
        resp = await http.post("/api/v1/research/analyze", json={
            "query": "qq", "analysis_type": "macro",
        })
        task_id = resp.json()["task_id"]
        await _wait_done(state.store, task_id)

        # trace_id使用任务内真实ID，测试里FakeAuditLog只含task_x → 注入对齐
        state.runtime.gateway.audit_log = FakeAuditLog()
        body = (await http.get(f"/api/v1/trace/{task_id}")).json()
        assert body["status"] == "completed"
        assert body["audit_chain"]["valid"] is True
        assert isinstance(body["llm_calls"], list)


async def test_report_generate(client):
    state, http = client
    async with http:
        resp = await http.post("/api/v1/research/analyze", json={"query": "qq"})
        task_id = resp.json()["task_id"]
        await _wait_done(state.store, task_id)

        report = await http.post("/api/v1/report/generate", json={"task_id": task_id})
        assert report.status_code == 200
        assert report.json()["report"].startswith("# 报告")

        missing = await http.post("/api/v1/report/generate", json={"task_id": "nope"})
        assert missing.status_code == 404


async def test_health_aggregation(client):
    _, http = client
    async with http:
        resp = await http.get("/api/v1/health")
        body = resp.json()
        assert resp.status_code == 200
        assert body["agents"]["A08_macro"] == "healthy"
        assert "model_gateway" in body and "audit_chain" in body
