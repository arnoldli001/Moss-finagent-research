"""投研路由并发治理测试：准入闸门、同问合流、结果缓存、TaskStore 回收。

覆盖 2026-09-25 并发改造的四个行为，全部用**假 graph**（不跑真实 Agent/LLM）：

1. **准入控制**：在飞任务数达到 `MOSS_RESEARCH_MAX_INFLIGHT` 时，
   新请求排队；等不到名额返回 503，而不是无限挂起或无限并发。
2. **同问合流**：相同 query+type+target 在首条计算期间到达 →
   返回**同一个 task_id**，且**不新起第二条管线**（graph 只被调用一次）。
3. **结果缓存命中**：任务完成后相同查询直接返回缓存，不重跑管线。
4. **TaskStore 回收**：大量任务跑完后，任务/令牌/句柄数不超过上限（不泄漏）。

为什么这些测试值得存在：改造前这四处**都没有任何保护**，
N 个用户 = N 条完整管线（7~15 次 LLM 调用 + 20 万 token 预算），
且任务表只增不减。回归会直接表现为 LLM 配额被打爆或内存单调增长。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import research as R
from src.api.tasks import TaskStore

# ---------------------------------------------------------------- 假 graph

class FakeGraph:
    """假 LangGraph：记录被调用次数，按需产出报告或一直挂着。"""

    def __init__(self, *, report: str | None = "# 报告\n内容",
                 delay: float = 0.0, hang: bool = False) -> None:
        self.calls = 0
        self.report = report
        self.delay = delay
        self.hang = hang

    async def astream(self, state: dict):
        self.calls += 1
        if self.hang:
            await asyncio.sleep(3600)
        if self.delay:
            await asyncio.sleep(self.delay)
        yield {"audit": {
            "agent_outputs": [{"agent_id": "A17_recommend",
                               "conclusion": "综合结论", "confidence": "high",
                               "data_refs": ["x"], "result": {}}],
            "final_report": self.report,
            "progress": ["完成"],
        }}


def _client(graph: FakeGraph) -> tuple[TestClient, TaskStore]:
    store = TaskStore()
    runtime = SimpleNamespace(graph=graph, gateway=None, data_health=None)
    app = FastAPI()
    app.include_router(R.router)
    app.state.runtime = runtime
    app.state.store = store
    c = TestClient(app)
    c.__enter__()
    return c, store


@pytest.fixture(autouse=True)
def _reset_module_state():
    """每个用例前后清空模块级治理状态（它们是进程级单例）。"""
    R._RESULT_CACHE.clear()
    R._inflight_by_hash.clear()
    R._admission = None
    R._inflight = 0
    yield
    R._RESULT_CACHE.clear()
    R._inflight_by_hash.clear()
    R._admission = None
    R._inflight = 0


def _body(query: str = "AI算力产业链景气度", **over) -> dict:
    b = {"query": query, "analysis_type": "macro", "target": "半导体"}
    b.update(over)
    return b


def _wait_done(c: TestClient, task_id: str, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = c.get(f"/api/v1/research/{task_id}")
        if r.status_code == 200 and r.json()["status"] in (
                "completed", "failed", "cancelled"):
            return r.json()
        time.sleep(0.05)
    raise AssertionError(f"任务 {task_id} 超时未结束")


# ---------------------------------------------------------------- 1. 结果缓存

def test_second_identical_request_hits_result_cache_not_pipeline():
    """相同查询第二次提交：走结果缓存，管线只被调用一次。"""
    g = FakeGraph()
    c, _ = _client(g)
    first = c.post("/api/v1/research/analyze", json=_body()).json()
    _wait_done(c, first["task_id"])
    assert g.calls == 1

    second = c.post("/api/v1/research/analyze", json=_body()).json()
    # 缓存命中直接返回完整结果，且不新起管线
    assert second.get("cache_hit") is True
    assert second["final_report"]
    assert g.calls == 1, "缓存命中不应再跑一次管线"
    c.__exit__(None, None, None)


def test_force_refresh_bypasses_result_cache():
    """force_refresh=true 绕过结果缓存（但仍受准入控制约束）。"""
    g = FakeGraph()
    c, _ = _client(g)
    t1 = c.post("/api/v1/research/analyze", json=_body()).json()
    _wait_done(c, t1["task_id"])
    t2 = c.post("/api/v1/research/analyze",
                json=_body(options={"force_refresh": True})).json()
    assert t2["task_id"] != t1["task_id"]
    _wait_done(c, t2["task_id"])
    assert g.calls == 2, "强制刷新应真的重跑一次"
    c.__exit__(None, None, None)


# ---------------------------------------------------------------- 2. 同问合流

def test_identical_concurrent_request_is_merged_not_duplicated():
    """首条还在算时，相同查询到达 → 复用同一 task_id，不新起管线。

    这正是改造前会退化成"N 条完整管线"的场景（首页热门问题被多人同时点）。
    """
    g = FakeGraph(delay=0.4)
    c, _ = _client(g)

    first = c.post("/api/v1/research/analyze", json=_body()).json()
    assert first.get("deduplicated") is not True

    # 等首条真正进入运行（占用 qhash 占位）
    deadline = time.time() + 5
    while time.time() < deadline and not R._inflight_by_hash:
        time.sleep(0.02)
    assert R._inflight_by_hash, "应已登记同问占位"

    second = c.post("/api/v1/research/analyze", json=_body()).json()
    assert second["task_id"] == first["task_id"], "应复用同一个 task_id"
    assert second.get("deduplicated") is True

    _wait_done(c, first["task_id"])
    assert g.calls == 1, f"同问合流后管线只应跑一次，实际 {g.calls} 次"
    c.__exit__(None, None, None)


def test_different_queries_are_not_merged():
    """不同查询必须各跑各的，不能被误合流。"""
    g = FakeGraph(delay=0.3)
    c, _ = _client(g)
    a = c.post("/api/v1/research/analyze", json=_body("问题甲")).json()
    b = c.post("/api/v1/research/analyze", json=_body("问题乙")).json()
    assert a["task_id"] != b["task_id"]
    _wait_done(c, a["task_id"])
    _wait_done(c, b["task_id"])
    assert g.calls == 2
    c.__exit__(None, None, None)


# ---------------------------------------------------------------- 3. 准入控制

def test_admission_rejects_when_queue_timeout_exceeded(monkeypatch):
    """在飞任务占满闸门时，新请求等不到名额应返回 503（而非挂起或并发跑）。"""
    monkeypatch.setattr(R, "_MAX_INFLIGHT", 1)
    monkeypatch.setattr(R, "_QUEUE_TIMEOUT_S", 0.2)
    R._admission = None

    g = FakeGraph(hang=True)          # 首条永远不结束 → 闸门被独占
    c, _ = _client(g)

    first = c.post("/api/v1/research/analyze", json=_body("长任务")).json()
    # 等首条占住名额
    deadline = time.time() + 5
    while time.time() < deadline and R._inflight < 1:
        time.sleep(0.02)
    assert R._inflight == 1, "首条应占用名额"

    r = c.post("/api/v1/research/analyze", json=_body("被拒任务"))
    assert r.status_code == 503
    assert "队列已满" in r.json()["detail"]
    assert R._inflight == 1, "被拒请求不应占用名额"
    assert g.calls == 1, "被拒请求不应起管线"

    # 收尾：取消长任务，让闸门释放
    c.post(f"/api/v1/research/{first['task_id']}/cancel")
    c.__exit__(None, None, None)


def test_admission_slot_released_after_completion():
    """任务结束后名额必须归还，否则闸门会被永久占掉（渐进式死锁）。"""
    monkeypatch_max = 2
    R._MAX_INFLIGHT = monkeypatch_max
    g = FakeGraph()
    c, _ = _client(g)
    for i in range(6):
        body = _body(f"第{i}个问题")
        r = c.post("/api/v1/research/analyze", json=body)
        assert r.status_code == 202, f"第{i}次提交被拒：{r.json()}"
        _wait_done(c, r.json()["task_id"])
    assert R._inflight == 0, f"全部完成后在飞数应为 0，实际 {R._inflight}"
    c.__exit__(None, None, None)


# ---------------------------------------------------------------- 4. TaskStore 回收

def test_task_store_does_not_grow_unbounded(monkeypatch):
    """超过容量上限后，已完成任务应被淘汰（改造前只增不减）。"""
    import src.api.tasks as T

    monkeypatch.setattr(T, "_MAX_TASKS", 20)
    store = TaskStore()
    from src.core.cancel import CancellationToken

    for i in range(200):
        tid = f"task_{i}"
        store.create(tid, trace_id=tid, tenant_id="t", query="q",
                     analysis_type="full", target="x")
        store.register_token(tid, CancellationToken(tid))
        store.register_handle(tid, None)  # type: ignore[arg-type]
        store.set_live_state(tid, {"progress": []})
        store.update(tid, status="running")
        store.update(tid, status="completed", final_report="报告")
        store.clear_live_state(tid)
        store.evict(force=True)

    st = store.stats()
    assert st["tasks"] <= 20, f"任务数应被容量钳住，实际 {st['tasks']}"
    # 关键：三表必须同步回收 —— 只清 _tasks 而留 _tokens 就是原来的泄漏
    assert st["tokens"] <= 20, f"令牌未同步回收：{st['tokens']}"
    assert st["live_state"] == 0


def test_task_store_keeps_inflight_tasks_despite_capacity(monkeypatch):
    """容量淘汰**绝不能**删掉在飞任务（否则轮询会 404、取消会失效）。"""
    import src.api.tasks as T

    monkeypatch.setattr(T, "_MAX_TASKS", 1)
    store = TaskStore()
    for i in range(5):
        tid = f"run_{i}"
        store.create(tid, trace_id=tid, tenant_id="t", query="q",
                     analysis_type="full", target="x")
        store.update(tid, status="running")
    store.evict(force=True)
    st = store.stats()
    assert st["tasks"] == 5, "在飞任务不应被容量淘汰"
    for i in range(5):
        assert store.get(f"run_{i}") is not None


# ---------------------------------------------------------------- 5. 容量观测接口

def test_capacity_endpoint_is_reachable_and_not_shadowed():
    """/research/capacity 不能被 /research/{task_id} 抢走（路由顺序回归）。"""
    g = FakeGraph()
    c, store = _client(g)
    r = c.get("/api/v1/research/capacity")
    assert r.status_code == 200, f"被 {r.status_code} 抢走：{r.json()}"
    data = r.json()
    assert "max_inflight" in data and "inflight" in data
    assert data["task_store"]["tasks"] == 0
    assert "budget" in data and "spent_cny" in data["budget"]
    c.__exit__(None, None, None)


# ---------------------------------------------------------------- 6. 日成本预算

def test_budget_exhausted_rejects_without_taking_admission_slot():
    """预算不足时提交应 503，且**不占用并发名额**（否则白占名额让人排队）。"""
    import src.core.budget as B

    B.reset_budget_for_test(daily=0.10)      # 单笔预扣 0.25，0.10 必然不够
    try:
        g = FakeGraph()
        c, _ = _client(g)
        r = c.post("/api/v1/research/analyze", json=_body("预算测试"))
        assert r.status_code == 503
        assert "预算" in r.json()["detail"]
        assert R._inflight == 0, "预算拒绝不应占用并发名额"
        assert g.calls == 0, "预算拒绝不应起管线"
        c.__exit__(None, None, None)
    finally:
        B.reset_budget_for_test(daily=20.0)


def test_budget_reservation_released_after_task():
    """任务结束后预扣必须退还，否则 reserved 单调累积最终全站拒绝。"""
    import src.core.budget as B

    B.reset_budget_for_test(daily=20.0)
    try:
        g = FakeGraph()
        c, _ = _client(g)
        r = c.post("/api/v1/research/analyze", json=_body("预扣测试"))
        assert r.status_code == 202
        _wait_done(c, r.json()["task_id"])
        snap = B.get_budget().snapshot()
        assert snap["reserved_cny"] == 0.0, (
            f"任务结束后预扣应归零，实际 {snap['reserved_cny']}")
        c.__exit__(None, None, None)
    finally:
        B.reset_budget_for_test(daily=20.0)


def test_budget_costs_match_config_prices():
    """成本算术必须与 models.yaml 的价格一致（防改价后口径漂移）。"""
    import src.core.budget as B

    b = B.CostBudget(daily_budget=20.0, model_config_path="configs/models.yaml")
    # deepseek-flash: 输入未命中 1.0、输出 4.0（元/百万token）
    # 30000 输入 + 4000 输出 = 0.03 + 0.016 = 0.046 元
    used = b.record(provider="deepseek", model="deepseek-flash",
                    tokens_in=30000, tokens_out=4000, source="test")
    assert abs(used - 0.046) < 1e-6, f"成本算术不符：{used}"
    # 本地模型不计费
    assert b.record(provider="ollama", model="qwen2.5:1.5b",
                    tokens_in=99999, tokens_out=99999) == used


def test_budget_zero_means_unlimited():
    """预算设 0 = 不限（保留"关掉护栏"的能力，便于开发期）。"""
    import src.core.budget as B

    B.reset_budget_for_test(daily=0.0)
    try:
        b = B.get_budget()
        assert b.remaining == float("inf")
        assert b.can_afford(1e9) is True
        for _ in range(50):
            assert b.reserve_task() is True
    finally:
        B.reset_budget_for_test(daily=20.0)
