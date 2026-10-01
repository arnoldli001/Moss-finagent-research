"""`/health` 的熔断器分段：逐桶状态必须**看得见**（2026-10-01，配合按租户分桶）。

## 为什么需要这条判据

熔断从"按 provider 一桶"改成"provider × 租户"之后，"一个用户打挂所有人"这个
老症状会**换个形态**回来：某个租户的桶 OPEN 了，而全局看起来一切正常。
所以运维可见性不是加分项，而是这次修复的前提条件 —— 没有它，
"按租户隔离生效了"与"某个租户被静默降级了"在界面上长得一样。

判据钉三件事：

1. 字段真的出现在 `/health` 响应体里（打一次请求，不只看代码）；
2. 形状契约：`available/counters/open/unmeasured` 齐、类型对；
3. **打满一个桶后 `open` 里必须出现它**（否则这是个恒空的假字段）；
4. 空态用 `unmeasured`（"还没量到" ≠ "量到 0"，AGENTS.md 硬约束）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """与既有 `/health` 契约测试同一套隔离方式（MOSS_ENV=test + 临时库）。"""
    monkeypatch.setenv("MOSS_SQLITE_PATH", str(tmp_path / "health_cb.db"))
    monkeypatch.setenv("LLM_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("MOSS_ENV", "test")
    from fastapi.testclient import TestClient

    from src.api.main import app

    with TestClient(app) as c:
        yield c


def _section(client) -> dict:
    # 路径与既有 `/health` 契约测试一致（`test_health_search_sources.py` 用同一档）
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200, resp.text
    return resp.json().get("llm_circuit_breakers") or {}


def test_health_exposes_circuit_breaker_buckets(client) -> None:
    seg = _section(client)
    assert seg, "熔断器分段没挂上 /health"
    assert seg["available"] is True, seg
    for key in ("counters", "open", "unmeasured"):
        assert key in seg, f"缺字段 {key}"
    assert isinstance(seg["counters"], dict) and isinstance(seg["open"], list)
    # 空态必须是"未量到"而不是 0（冷启动没有任何桶是**有意义的 0**）
    assert seg["unmeasured"] == "未量到"


def test_open_bucket_shows_up_in_health(client) -> None:
    """★ 判据不许恒空：打满一个桶之后，它必须出现在 `open` 里。"""
    from src.infrastructure.llm.circuit_breaker import get_circuit_registry

    cb = get_circuit_registry().get_or_create("deepseek:vip")
    for _ in range(200):
        cb.record_failure()
    assert cb.snapshot()["state"] == "OPEN", "哨兵没生效，这条判据什么也没证明"

    seg = _section(client)
    assert "deepseek:vip" in seg["counters"], "逐桶快照里看不到这个桶"
    assert "deepseek:vip" in seg["open"], "开着的桶没有出现在 open 列表里"
    # 逐租户隔离的另一半：另一个租户的桶不该被带开
    assert "deepseek:trial" not in seg["open"]
