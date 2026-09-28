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
            "agent_messages": [],
            "progress": ["FakeGraph 已完成"],
        }

    async def astream(self, state):
        """模拟LangGraph的astream：以单chunk返回最终state，便于API层实时合并。"""
        yield await self.ainvoke(state)


class FakeBackend:
    """回测API用：返回12个月确定性PPI发布点与月末收盘价。"""

    def __init__(self):
        self.calls: dict[str, int] = {}

    def get_capabilities(self):
        return {"routes": [
            {"name": "模拟源(测试替身)", "simulated": True,
             "indicators": ["ind:测试指标"]},
            {"name": "AkShare", "simulated": False, "indicators": ["CPI", "PPI"]},
        ]}

    async def fetch(self, indicator):
        from src.core.schemas import DataPoint

        self.calls[indicator] = self.calls.get(indicator, 0) + 1
        if indicator in ("PPI", "M2"):
            values = [100 + i * 2 for i in range(12)]  # 持续环比上行→信号1
            return [
                DataPoint(indicator=indicator, value=float(values[i]),
                          period_date=f"2024-{i + 1:02d}-09")
                for i in range(12)
            ]
        if indicator.startswith(("stock_close:", "index_close:", "etf_close:")):
            prices = [10 * 1.01 ** i for i in range(12)]
            return [
                DataPoint(indicator=indicator, value=round(prices[i], 3),
                          period_date=f"2024-{i + 1:02d}-28")
                for i in range(12)
            ]
        if indicator.startswith("PE(TTM):"):
            return [
                DataPoint(indicator=indicator, value=15.0,
                          period_date=f"2024-{i + 1:02d}-27")
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


async def test_stock_name_resolved_to_code(client, monkeypatch):
    """target填中文简称时后端解析为6位代码采集，原始名称保留用于展示。"""
    from src.api.routes import research

    async def fake_resolve(text):
        assert "中际旭创" in text
        return "300308", "中际旭创"

    monkeypatch.setattr(research, "resolve_stock", fake_resolve)

    captured: dict = {}

    class CapturingGraph(FakeGraph):
        async def ainvoke(self, state):
            captured["state"] = state
            return await super().ainvoke(state)

    state, http = client
    state.runtime.graph = CapturingGraph()
    async with http:
        resp = await http.post("/api/v1/research/analyze", json={
            "query": "中际旭创目前A股是否有价值洼地，值得持有3-6个月？",
            "analysis_type": "stock", "target": "中际旭创",
        })
        task_id = resp.json()["task_id"]
        await _wait_done(state.store, task_id)

    assert captured["state"]["target"] == "300308"
    assert captured["state"]["target_display"] == "中际旭创"
    # 任务记录展示中文名而非代码
    assert state.store.get(task_id).target == "中际旭创"


async def test_stock_resolve_failure_keeps_original(client, monkeypatch):
    """名称表不可用时保留原target，任务不失败（下游诚实输出数据不足）。"""
    from src.api.routes import research

    async def fake_resolve(text):
        return None

    monkeypatch.setattr(research, "resolve_stock", fake_resolve)
    state, http = client
    async with http:
        resp = await http.post("/api/v1/research/analyze", json={
            "query": "某不存在公司值得买吗",
            "analysis_type": "stock", "target": "某不存在公司",
        })
        assert resp.status_code == 202
        record = await _wait_done(state.store, resp.json()["task_id"])
        assert record.status == "completed"


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
        # 免费档限流熔断（light 层首位是免费档，被限流时链会自动前进）：
        # 这是它**唯一**的可见面 —— 没有它就只能从"延迟上升 + 下一跳配额
        # 被多吃"上后知后觉。判据区分「未量到」与「量到 0」。
        guard = body["model_gateway"]["rate_limit_guard"]
        assert guard["available"] is True
        assert guard["threshold"] >= 2
        assert guard["path"], "状态文件路径要可见（运维要能直接去看它）"
        for snap in guard["models"].values():
            assert snap["remaining_s"] is None or snap["remaining_s"] > 0, (
                "未锁定时 remaining_s 必须是 None，不能用 0 糊过去")
        sources = body["data_sources"]
        statuses = {c["name"]: c["status"] for c in sources["connectors"]}
        assert statuses["模拟源(测试替身)"] == "simulated"
        assert sources["storage"]["status"] == "ok"
        assert sources["redis_cache"] == "disabled"


async def test_agents_meta_endpoint(client):
    """前端展示元数据：id→中文名 + 置信度中文映射。"""
    _, http = client
    async with http:
        resp = await http.get("/api/v1/agents/meta")
        body = resp.json()
        assert resp.status_code == 200
        assert body["agents"]["A17_recommend"]["name"] == "投研建议Agent"
        assert body["agents"]["A09_meso"]["name"] == "中观分析Agent"
        assert body["confidence_zh"] == {"high": "高", "medium": "中", "low": "低"}
        # 全部运行时 Agent 均有中文名
        for agent_id in (
            "A05_verifier", "A06_extractor", "A07_sentiment",
            "A11_fin_risk", "A12_compliance",
            "A13_tech", "A14_consumer", "A15_cyclical", "A16_pharma",
            "A17_recommend", "A18_audit",
        ):
            assert agent_id in body["agents"]


async def test_scheduler_jobs_list_and_manual_trigger(client):
    """作业清单 / 手动触发 / 运行记录 / 日报的机制。

    ⚠️ 调度路由自 2026-09-26 起是**管理员专属**（`require_admin`，见
    `tests/unit/test_scheduler_admin_gate.py`）。这条测试考的是作业执行机制，
    不是鉴权，所以用 FastAPI 的依赖覆盖把门槛短路掉。

    为什么不是 `monkeypatch.setattr(scheduler, "require_admin", fake)`：
    路由级 `Depends(require_admin)` 在**构造路由时**就抓住了那个函数对象，
    之后再改模块属性不会影响已经建好的依赖 —— 那种写法会**静默无效**
    （测试照样 401 失败，但看不出是覆盖没生效）。
    """
    state, http = client
    from src.api.main import app
    from src.api.routes.admin import require_admin

    app.dependency_overrides[require_admin] = lambda: "test-admin"
    try:
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
            assert any(r["run_id"] == record["run_id"]
                       for r in runs.json()["runs"])

            summary = await http.get("/api/v1/scheduler/runs/summary")
            assert summary.json()["success"] >= 1

            assert (await http.post(
                "/api/v1/scheduler/jobs/nope/run")).status_code == 404
    finally:
        app.dependency_overrides.pop(require_admin, None)


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


async def _run_backtest(http: httpx.AsyncClient, payload: dict) -> httpx.Response:
    """回测异步任务辅助：提交任务后轮询至终态，返回终态响应。

    POST /run 立即返回job_id（长请求会被浏览器/代理掐断，故为异步），
    测试同事件循环轮询GET /jobs/{id}，FakeBackend瞬时完成。
    """
    started = await http.post("/api/v1/backtest/run", json=payload)
    if started.status_code != 200:
        return started
    job_id = started.json()["job_id"]
    for _ in range(200):
        status = await http.get(f"/api/v1/backtest/jobs/{job_id}")
        assert status.status_code == 200
        body = status.json()
        if body["status"] != "running":
            return status
        await asyncio.sleep(0)
    raise AssertionError("回测任务轮询超时")


def _backtest_result(job_response: httpx.Response) -> dict:
    body = job_response.json()
    if body["status"] == "error":
        raise AssertionError(f"回测任务失败: {body.get('error')}")
    return body["result"]


async def test_backtest_run_endpoint(client):
    from src.api.routes.backtest import clear_fetch_cache

    clear_fetch_cache()
    _, http = client
    async with http:
        resp = await _run_backtest(http, {"indicator": "PPI", "code": "601088"})
        assert resp.status_code == 200
        body = _backtest_result(resp)
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
        resp2 = await _run_backtest(http, {"indicator": "PPI", "code": "601088"})
        body2 = _backtest_result(resp2)
        assert body2["cache"]["price_hit"] is True
        assert backend.calls["stock_close:601088"] == 1
        assert body2["equity_curve"] == body["equity_curve"]

        # 提交响应必须是异步任务契约：立即返回job_id与预估等待
        clear_fetch_cache()
        started = await http.post("/api/v1/backtest/run",
                                  json={"indicator": "PPI", "code": "601088"})
        assert started.status_code == 200
        sj = started.json()
        assert sj["status"] == "running" and sj["job_id"]
        assert sj["estimated_wait_seconds"] >= 10
        assert "数据下载中" in sj["message"]
        running = await http.get(f"/api/v1/backtest/jobs/{sj['job_id']}")
        assert running.status_code == 200
        assert running.json()["stage_label"]

        unknown = await http.get("/api/v1/backtest/jobs/no_such_job")
        assert unknown.status_code == 404


async def test_backtest_validation(client):
    _, http = client
    async with http:
        bad_code = await http.post("/api/v1/backtest/run",
                                   json={"indicator": "PPI", "code": "ABC"})
        assert bad_code.status_code == 400
        bad_ind = await http.post("/api/v1/backtest/run",
                                  json={"indicator": "GDP", "code": "601088"})
        assert bad_ind.status_code == 400
        bad_type = await http.post("/api/v1/backtest/run",
                                   json={"indicator": "PPI", "code": "601088",
                                         "asset_type": "futures"})
        assert bad_type.status_code == 400
        pe_on_etf = await http.post("/api/v1/backtest/run",
                                    json={"indicator": "PPI", "code": "510300",
                                          "asset_type": "etf",
                                          "pe_watermark": 20})
        assert pe_on_etf.status_code == 400
        bad_date = await http.post("/api/v1/backtest/run",
                                   json={"indicator": "PPI", "code": "601088",
                                         "start_date": "2024/03"})
        assert bad_date.status_code == 400
        reversed_range = await http.post("/api/v1/backtest/run",
                                         json={"indicator": "PPI",
                                               "code": "601088",
                                               "start_date": "2024-10",
                                               "end_date": "2024-03"})
        assert reversed_range.status_code == 400


async def test_backtest_date_range_filter(client):
    from src.api.routes.backtest import clear_fetch_cache

    clear_fetch_cache()
    _, http = client
    async with http:
        resp = await _run_backtest(http, {
            "indicator": "PPI", "code": "601088",
            "start_date": "2024-03", "end_date": "2024-10",
            # 零成本便于断言
            "commission_rate": 0.0, "stamp_tax_rate": 0.0,
            "slippage_rate": 0.0, "cash_annual_yield": 0.0,
        })
        assert resp.status_code == 200
        body = _backtest_result(resp)
        assert body["range"] == {"start": "2024-03", "end": "2024-10"}
        assert body["periods"] == 8
        assert len(body["equity_curve"]) == 8


async def test_backtest_pe_gate_fetches_pe_and_index_routing(client):
    from src.api.routes.backtest import clear_fetch_cache

    clear_fetch_cache()
    state, http = client
    async with http:
        # PE闸门开启：应额外拉取PE(TTM)，覆盖率100%
        resp = await _run_backtest(http, {
            "indicator": "PPI", "code": "601088", "pe_watermark": 20,
            "commission_rate": 0.0, "stamp_tax_rate": 0.0,
            "slippage_rate": 0.0, "cash_annual_yield": 0.0,
        })
        assert resp.status_code == 200
        body = _backtest_result(resp)
        assert body["pe_gate"]["enabled"] is True
        assert body["pe_gate"]["watermark"] == 20
        assert body["pe_gate"]["coverage"] == 1.0
        assert body["cache"]["pe_hit"] is False

        # 指数标的走 index_close 前缀；M2 指标可用；ETF默认无印花税
        resp2 = await _run_backtest(http, {
            "indicator": "M2", "code": "000300", "asset_type": "index",
        })
        assert resp2.status_code == 200
        body2 = _backtest_result(resp2)
        assert "指数" in body2["asset"]
        assert body2["rule"]["cost"]["stamp_tax_rate"] == 0.0
        backend = state.runtime.backend
        assert backend.calls["PE(TTM):601088"] == 1
        assert backend.calls["index_close:000300"] == 1
        assert backend.calls["M2"] == 1


async def test_backtest_cost_and_capital_in_response(client):
    from src.api.routes.backtest import clear_fetch_cache

    clear_fetch_cache()
    _, http = client
    async with http:
        resp = await _run_backtest(http, {
            "indicator": "PPI", "code": "601088",
            "initial_capital": 500_000,
            "commission_rate": 0.0003, "stamp_tax_rate": 0.0005,
            "slippage_rate": 0.001, "cash_annual_yield": 0.02,
        })
        assert resp.status_code == 200
        s = _backtest_result(resp)["strategy"]
        assert s["initial_capital"] == 500_000
        assert s["final_equity"] > 0
        assert s["trades"] >= 1
        assert s["total_transaction_cost"] > 0
        rule = _backtest_result(resp)["rule"]
        assert rule["cost"]["commission_rate"] == 0.0003
        assert rule["cost"]["cash_annual_yield"] == 0.02
