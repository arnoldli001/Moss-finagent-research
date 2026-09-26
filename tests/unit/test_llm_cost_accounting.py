"""LLM 费用记账：归属（哪个功能/哪个租户）+ 计价 + 聚合。

对应实现：
  - `src/core/accounting.py`（记账上下文）
  - `src/api/accounting_middleware.py`（会话 → 租户/用户 + 请求路径）
  - `src/infrastructure/llm/audit.py`（随行落盘 `path` / `tenant_source`）
  - `src/domain/platform/llm_cost.py`（归属映射 + 聚合）

## 要钉死的五件事

1. **归属精确优先**：有 `path` 就按路径映射（事实）；没有才按 agent 名推断，
   并且**必须把"这是推断"标出来** —— 不然管理员会把估算当账单。
2. **每个前端功能单独成行**：这是"钱分别来自前端哪些功能"的落点。
3. **按租户**：会话身份优先于 tenancy Principal（后者生产上是
   `local/local-dev` 的 dev-bypass 假身份 —— 实测 50764 条访问审计全是它）。
4. **计价只有一份实现**：在线账本（`CostBudget`）与事后统计共用
   `call_cost_cny`，两处金额不会对不上。
5. **未登记价格的模型要能看见**：`deepseek-v4-flash`（旧名）实测 309 次，
   金额只能估算，面板必须显示这一点。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core import accounting
from src.core.accounting import AccountingContext
from src.core.budget import (
    CostBudget,
    call_cost_cny,
    model_is_priced,
    model_prices,
)
from src.domain.platform.llm_cost import (
    aggregate_llm_cost,
    cost_basis_notes,
    feature_of,
)


def _row(*, ts: str = "", path: str = "", agent_id: str = "A17_recommend",
         provider: str = "deepseek", model: str = "deepseek-flash",
         tokens_in: int = 0, tokens_out: int = 0, tenant: str = "",
         user: str = "", source: str = "session") -> dict:
    return {
        "ts": ts or datetime.now().isoformat(timespec="seconds"),
        "agent_id": agent_id, "provider": provider, "model": model,
        "tokens_in": tokens_in, "tokens_out": tokens_out,
        "tenant_id": tenant, "user_id": user, "tenant_source": source,
        "path": path,
    }


# ======================================================================
# 一、归属：精确（path）优先于推断（agent）
# ======================================================================

@pytest.mark.parametrize("path,expected", [
    ("/api/v1/research/analyze", "research"),
    ("/api/v1/mainline/scores", "mainline"),
    ("/api/v1/intel/feed", "intel.radar"),
    ("/api/v1/intel/alerts/scan", "intel.alerts"),
    ("/api/v1/intraday/snapshot", "quant.intraday"),
    ("/api/v1/backtest/run", "backtest"),
    ("/api/v1/fundflow/board", "fundflow"),
])
def test_path_maps_to_frontend_feature(path: str, expected: str) -> None:
    key, basis = feature_of(_row(path=path))
    assert (key, basis) == (expected, "page")


def test_path_wins_over_agent_name() -> None:
    """★ 同一个 Agent 既可能被页面触发、也可能被定时作业触发。

    有请求路径时按**路径**归属 —— 那是事实；按 agent 名归属只能算推断。
    """
    key, basis = feature_of(_row(path="/api/v1/research/analyze",
                                 agent_id="mainline_relevance"))
    assert (key, basis) == ("research", "page")


def test_agent_name_is_used_only_without_a_path() -> None:
    assert feature_of(_row(agent_id="mainline_relevance")) == \
        ("ops.script", "agent")
    assert feature_of(_row(agent_id="A09_meso")) == ("research", "agent")
    assert feature_of(_row(agent_id="intel_extract")) == ("intel.radar", "agent")
    assert feature_of(_row(agent_id="alert_analyzer")) == \
        ("intel.alerts", "agent")
    assert feature_of(_row(agent_id="完全没见过的名字")) == \
        ("sys.unknown", "unknown")


def test_non_page_buckets_are_separate_from_sellable_features() -> None:
    """脚本/作业各占一桶，**不能**并进任何一个前端功能。

    `mainline_relevance` 一个脚本就占全站费用的大头，并进"主线挖掘"
    会让那个功能的数字失去意义（管理员会以为客户把它烧光了）。
    """
    script, _ = feature_of(_row(agent_id="mainline_relevance"))
    job, _ = feature_of(_row(agent_id="A09_meso"))
    assert script == "ops.script"
    assert job == "research"


# ======================================================================
# 二、计价：与在线账本同一份实现
# ======================================================================

def test_call_cost_matches_the_budget_ledger() -> None:
    """★ 同一个计价函数：账本记的金额与监控页算的金额必须一致。"""
    prices = model_prices()
    assert "deepseek-flash" in prices, "测试依赖 models.yaml 里的真实价格"
    hit, miss, out = prices["deepseek-flash"]
    tokens_in, tokens_out = 1_000_000, 2_000_000
    expected = tokens_in * miss / 1e6 + tokens_out * out / 1e6

    direct = call_cost_cny(provider="deepseek", model="deepseek-flash",
                           tokens_in=tokens_in, tokens_out=tokens_out)
    assert direct == pytest.approx(expected)

    budget = CostBudget(daily_budget=1000.0)
    assert budget.record(provider="deepseek", model="deepseek-flash",
                         tokens_in=tokens_in, tokens_out=tokens_out) == \
        pytest.approx(expected)


def test_local_models_are_free_not_cheap() -> None:
    assert call_cost_cny(provider="ollama", model="qwen3:8b",
                         tokens_in=10_000, tokens_out=10_000) == 0.0


def test_unpriced_model_is_estimated_and_flagged() -> None:
    """旧模型名（`deepseek-v4-flash`）不在配置里 → 用兜底价**估算**并计数。"""
    assert model_is_priced("deepseek-v4-flash") is False
    assert call_cost_cny(provider="deepseek", model="deepseek-v4-flash",
                         tokens_in=1000, tokens_out=1000) > 0
    cost = aggregate_llm_cost([_row(model="deepseek-v4-flash",
                                    tokens_in=1000, tokens_out=1000)])
    assert cost["unpriced_calls"] == 1
    assert cost["unpriced_models"] == [("deepseek-v4-flash", 1)]
    notes = " ".join(cost_basis_notes(cost))
    assert "deepseek-v4-flash" in notes and "估算" in notes


# ======================================================================
# 三、聚合：汇总 / 按功能 / 按租户 / 按天
# ======================================================================

def test_aggregate_splits_by_feature_and_tenant() -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    rows = [
        # 页面触发（有 path）：精确归属到 research
        _row(path="/api/v1/research/analyze", tenant="vip", user="u1",
             tokens_in=1000, tokens_out=2000),
        _row(path="/api/v1/research/analyze", tenant="vip", user="u2",
             tokens_in=1000, tokens_out=2000),
        # 另一个租户的脚本（无 path）
        _row(agent_id="mainline_relevance", tenant="", tokens_in=100,
             tokens_out=100),
    ]
    cost = aggregate_llm_cost(rows)
    assert cost["calls"] == 3
    by_key = {f["key"]: f for f in cost["by_feature"]}
    assert by_key["research"]["calls"] == 2
    assert by_key["research"]["basis"] == "page"
    assert by_key["ops.script"]["calls"] == 1
    assert by_key["ops.script"]["basis"] == "agent"
    # 占比加起来约等于 100%
    assert sum(f["share_pct"] for f in cost["by_feature"]) == \
        pytest.approx(100.0, abs=0.2)
    # 按租户
    by_tenant = {t["tenant_id"]: t for t in cost["by_tenant"]}
    assert by_tenant["vip"]["calls"] == 2 and by_tenant["vip"]["users"] == 2
    assert by_tenant["(未归属)"]["calls"] == 1
    # 按天
    assert cost["by_day"][-1]["day"] == today
    assert cost["by_day"][-1]["calls"] == 3


def test_aggregate_separates_paid_free_and_unknown_provider() -> None:
    cost = aggregate_llm_cost([
        _row(tokens_in=100, tokens_out=100),
        _row(provider="ollama", model="qwen3:8b", tokens_in=100, tokens_out=100),
        _row(provider="", model="", tokens_in=100, tokens_out=100),
    ])
    assert cost["paid_calls"] == 1
    assert cost["free_calls"] == 1
    assert cost["unknown_provider_calls"] == 1
    # 免费与无 provider 的调用**不产生金额**
    assert cost["total_cny"] == pytest.approx(
        call_cost_cny(provider="deepseek", model="deepseek-flash",
                      tokens_in=100, tokens_out=100))
    assert any("provider" in n for n in cost_basis_notes(cost))


def test_empty_aggregate_is_all_zeros_not_an_error() -> None:
    cost = aggregate_llm_cost([])
    assert cost["total_cny"] == 0 and cost["by_feature"] == []
    assert cost["by_tenant"] == [] and cost["by_day"] == []


def test_tenant_source_is_surfaced() -> None:
    """老行没有 `tenant_source` 也要能读，且如实标出来。"""
    rows = [_row(tenant="vip", source="session"),
            {**_row(tenant="vip"), "tenant_source": None}]
    cost = aggregate_llm_cost(rows)
    vip = next(t for t in cost["by_tenant"] if t["tenant_id"] == "vip")
    assert set(vip["sources"]) == {"session", "unknown"}


# ======================================================================
# 四、审计落盘：会话身份 + 请求路径
# ======================================================================

def test_audit_row_records_path_and_session_identity(tmp_path) -> None:
    from src.infrastructure.llm.audit import LLMAuditLog
    from src.infrastructure.llm.models import LLMResponse

    resp = LLMResponse(content="ok", model_used="deepseek-flash",
                       provider="deepseek", tokens_in=10, tokens_out=20)
    log = LLMAuditLog(str(tmp_path))
    with accounting.use(AccountingContext(path="/api/v1/research/analyze",
                                          tenant_id="vip", user_id="u_1")):
        entry = log.record(trace_id="t", agent_id="A17_recommend",
                           task_tier="reasoning", response=resp, cached=False)
    assert entry["path"] == "/api/v1/research/analyze"
    assert entry["tenant_id"] == "vip" and entry["user_id"] == "u_1"
    assert entry["tenant_source"] == "session"
    # 真的落到文件里了（不是只在返回值里）
    on_disk = json.loads(log.path.read_text(encoding="utf-8").strip())
    assert on_disk["path"] == "/api/v1/research/analyze"


def test_audit_marks_calls_outside_any_request_as_unattributed(tmp_path) -> None:
    """后台作业/脚本没有身份也没有路径 —— 必须**如实**标成 none，而不是编一个。"""
    from src.infrastructure.llm.audit import LLMAuditLog
    from src.infrastructure.llm.models import LLMResponse

    log = LLMAuditLog(str(tmp_path))
    entry = log.record(
        trace_id="t", agent_id="mainline_relevance", task_tier="decision",
        response=LLMResponse(content="ok", model_used="deepseek-flash",
                             provider="deepseek"),
        cached=False)
    assert entry["path"] == "" and entry["tenant_source"] == "none"
    assert entry["tenant_id"] == ""


# ======================================================================
# 五、中间件：会话 Cookie → 租户/用户 + 路径
# ======================================================================

@pytest.fixture()
def env(tmp_path, monkeypatch):
    """隔离的认证库 + 一个只回显记账上下文的测试应用。"""
    monkeypatch.setenv("MOSS_ENV", "test")
    monkeypatch.setenv("MOSS_SQLITE_PATH", str(tmp_path / "acct.db"))
    from src.core.config import get_settings

    get_settings.cache_clear()
    from src.api.routes.auth import reset_auth_service

    reset_auth_service()
    yield
    reset_auth_service()
    get_settings.cache_clear()


def _app_with_middleware() -> FastAPI:
    from src.api.accounting_middleware import AccountingMiddleware
    from src.api.routes.auth import router as auth_router

    app = FastAPI()
    app.add_middleware(AccountingMiddleware)
    # 登录路由：测试要拿一张**真实**的会话 Cookie（而不是手工塞一个看起来像
    # 但校验不过的值），这样验的才是中间件的解析逻辑本身。
    app.include_router(auth_router)

    @app.get("/api/v1/research/analyze")
    async def probe() -> dict:
        ctx = accounting.current()
        return {"path": ctx.path, "tenant_id": ctx.tenant_id,
                "user_id": ctx.user_id}

    return app


def _login(client: TestClient, tier: str) -> None:
    import secrets

    from src.api.routes.auth import get_auth_service

    repo = get_auth_service()._repo
    suffix = secrets.token_hex(4)
    username = f"acct_{tier}_{suffix}"
    user_id = f"u_{suffix}"
    repo.create_user(user_id=user_id, username=username, display_name=username,
                     status="active", applied_tier=tier,
                     valid_until=(datetime.now().astimezone()
                                  + timedelta(days=30)).isoformat(
                                      timespec="seconds"))
    repo.set_password(user_id, "Acct#2026xyz")
    r = client.post("/api/v1/auth/login",
                    json={"account": username, "password": "Acct#2026xyz",
                          "remember_me": False})
    assert r.status_code == 200, r.text[:200]


def test_middleware_binds_session_and_path(env) -> None:
    """★ 没有它，"按租户监控"在生产上只有一个 `local` 假身份行。

    实测口径：改动前 `data/audit/access_audit.jsonl` 的 50764 行
    **全部**是 `tenant_id=local, user_id=local-dev, auth_source=dev-bypass`。
    """
    c = TestClient(_app_with_middleware())
    _login(c, "vip")
    body = c.get("/api/v1/research/analyze").json()
    assert body["tenant_id"] == "vip", "会话身份没进记账上下文"
    assert body["user_id"].startswith("u_")
    assert body["path"] == "/api/v1/research/analyze"


def test_middleware_without_a_session_is_empty_not_an_error(env) -> None:
    """匿名请求（登录页/探针）：上下文留空，**绝不**拦请求或 500。"""
    c = TestClient(_app_with_middleware())
    r = c.get("/api/v1/research/analyze")
    assert r.status_code == 200
    assert r.json()["tenant_id"] == "" and r.json()["user_id"] == ""
    assert r.json()["path"] == "/api/v1/research/analyze"


def test_middleware_ignores_a_bogus_session_cookie(env) -> None:
    """伪造/过期的 Cookie 只是"没有身份"，不是"请求失败"。"""
    c = TestClient(_app_with_middleware())
    c.cookies.set("moss_sid", "deadbeef" * 4)
    r = c.get("/api/v1/research/analyze")
    assert r.status_code == 200
    assert r.json()["tenant_id"] == ""
