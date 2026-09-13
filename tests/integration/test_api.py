"""API集成测试（ASGI内存传输，注入FakeRuntime，不联网）。"""

import asyncio

import httpx
import pytest

from src.api.main import app
from src.api.tasks import TaskStore
from src.infrastructure.repositories.macro_repo import MacroRepository
from src.scheduler.run_log import RunLog


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


class FakeBackend:
    """回测API用：返回12个月确定性PPI发布点与月末收盘价。"""

    def __init__(self):
        self.calls: dict[str, int] = {}

    def get_capabilities(self):
        return {"routes": [
            {"name": "模拟产业数据(Demo)", "simulated": True,
             "indicators": ["ind:科技行业PE(TTM)"]},
            {"name": "AkShare", "simulated": False, "indicators": ["CPI", "PPI"]},
        ]}

    async def fetch(self, indicator):
        from src.core.schemas import DataPoint

        self.calls[indicator] = self.calls.get(indicator, 0) + 1
        if indicator == "PPI":
            values = [100 + i * 2 for i in range(12)]  # 持续环比上行→信号1
            return [
                DataPoint(indicator="PPI", value=float(values[i]),
                          period_date=f"2024-{i + 1:02d}-09")
                for i in range(12)
            ]
        if indicator.startswith("stock_close:"):
            prices = [10 * 1.01 ** i for i in range(12)]
            return [
                DataPoint(indicator=indicator, value=round(prices[i], 3),
                          period_date=f"2024-{i + 1:02d}-28")
                for i in range(12)
            ]
        raise ValueError(f"unsupported {indicator}")


def _install_app_state(tmp_dir):
    app.state.store = TaskStore()
    app.state.run_log = RunLog(f"{tmp_dir}/scheduler/runs.jsonl")
    app.state.runtime = type("R", (), {})()
    app.state.runtime.gateway = FakeGateway()
    app.state.runtime.repo = MacroRepository(db_path=f"{tmp_dir}/api.db")
    app.state.runtime.agents = {"A08_macro": HealthyAgent("A08_macro")}
    app.state.runtime.graph = FakeGraph()
    app.state.runtime.backend = FakeBackend()
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
        sources = body["data_sources"]
        statuses = {c["name"]: c["status"] for c in sources["connectors"]}
        assert statuses["模拟产业数据(Demo)"] == "simulated"
        assert sources["storage"]["status"] == "ok"
        assert sources["redis_cache"] == "disabled"


async def test_scheduler_jobs_list_and_manual_trigger(client):
    state, http = client
    async with http:
        resp = await http.get("/api/v1/scheduler/jobs")
        jobs = resp.json()["jobs"]
        names = {j["name"] for j in jobs}
        assert {"snapshot_macro", "snapshot_industry_watchlist",
                "run_log_cleanup"} <= names
        macro = next(j for j in jobs if j["name"] == "snapshot_macro")
        assert len(macro["cron"].split()) == 5 and macro["paused"] is False

        run = await http.post("/api/v1/scheduler/jobs/snapshot_macro/run")
        assert run.status_code == 202
        record = run.json()["run"]
        assert record["status"] == "success"
        assert record["trigger"] == "manual"

        runs = await http.get("/api/v1/scheduler/runs")
        assert any(r["run_id"] == record["run_id"] for r in runs.json()["runs"])

        summary = await http.get("/api/v1/scheduler/runs/summary")
        assert summary.json()["success"] >= 1

        assert (await http.post("/api/v1/scheduler/jobs/nope/run")).status_code == 404


async def test_metrics_endpoint_shape(client):
    _, http = client
    async with http:
        resp = await http.get("/api/v1/metrics?limit=100")
        assert resp.status_code == 200
        body = resp.json()
        assert body["limit"] == 100
        m = body["metrics"]
        assert {"window_calls", "cache_hit_rate", "fallback_rate",
                "latency_ms", "by_provider", "by_agent"} <= set(m)
        assert {"p50", "p95", "p99", "avg", "max"} == set(m["latency_ms"])


async def test_backtest_run_endpoint(client):
    from src.api.routes.backtest import clear_fetch_cache

    clear_fetch_cache()
    _, http = client
    async with http:
        resp = await http.post("/api/v1/backtest/run",
                               json={"indicator": "PPI", "code": "601088"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["periods"] == 12
        assert body["range"] == {"start": "2024-01", "end": "2024-12"}
        assert body["rule"]["kind"] == "trend+PE_gate_long_only"
        assert body["signals"]["long"] >= 1
        assert "cumulative_return" in body["strategy"]
        assert len(body["equity_curve"]) == 12
        assert "不构成投资建议" in body["disclaimer"]
        assert body["cache"]["price_hit"] is False
        assert body["cache"]["ttl_seconds"] == 600

        # 第二次请求命中TTL缓存：后端不再重复拉取行情
        backend = app.state.runtime.backend
        resp2 = await http.post("/api/v1/backtest/run",
                                json={"indicator": "PPI", "code": "601088"})
        body2 = resp2.json()
        assert body2["cache"]["price_hit"] is True
        assert backend.calls["stock_close:601088"] == 1
        assert body2["equity_curve"] == body["equity_curve"]


async def test_backtest_validation(client):
    _, http = client
    async with http:
        bad_code = await http.post("/api/v1/backtest/run",
                                   json={"indicator": "PPI", "code": "ABC"})
        assert bad_code.status_code == 400
        bad_ind = await http.post("/api/v1/backtest/run",
                                  json={"indicator": "GDP", "code": "601088"})
        assert bad_ind.status_code == 400
