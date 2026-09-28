"""验收：`/health` 的 `rate_limit_guard` 段**真实契约**（不是看代码，是打响应体）。

## 为什么需要它

前端的展示口径（`web/src/rateGuardView.ts`，由 `npm run check:rateguard` 自证）
吃的是后端 `snapshot()` 的字段名。两边**各改各的**时没有任何东西会报错：

    · 后端把 `remaining_s` 改名 → 前端 `v.remaining_s ?? 0` → 永远显示"0 秒"
    · 后端把 `available` 去了   → 前端当"未上报"，永远黄着
    · 后端把 `models` 拍平     → 前端 `Object.entries` 拿到字符串 → 计数全 NaN

三种都不会失败，只会**显示错**。所以这里打真实 `/health` 响应体，
用**契约断言**把字段名与类型钉住（判据只认字段与类型，不认文案 —— 文案会改）。

自己起一个 TestClient，不依赖是否有实例在跑；`MOSS_SQLITE_PATH` 指向临时库。

跑法：
    uv run python -m pytest tests/unit/test_health_rate_limit_guard_contract.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """隔离环境下的 TestClient（临时库，绝不碰生产库）。"""
    monkeypatch.setenv("MOSS_SQLITE_PATH", str(tmp_path / "contract.db"))
    monkeypatch.setenv("LLM_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("MOSS_ENV", "test")
    from fastapi.testclient import TestClient

    from src.api.main import app

    with TestClient(app) as c:
        yield c


def test_health_exposes_rate_limit_guard_contract(client):
    """`/health` 必须给出前端渲染所需的**全部**字段与类型。"""
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    gw = body.get("model_gateway")
    assert isinstance(gw, dict), "model_gateway 段缺失"
    guard = gw.get("rate_limit_guard")
    assert isinstance(guard, dict), (
        "model_gateway.rate_limit_guard 缺失 —— 免费档限流就又变成不可见了")

    # ① 顶层：可用性 + 阈值口径 + 状态文件路径（运维要能直接去看它）
    assert isinstance(guard["available"], bool)
    for key in ("threshold", "lock_minutes", "path"):
        assert key in guard, f"缺字段 {key}"
    assert isinstance(guard["threshold"], int) and guard["threshold"] >= 2
    assert isinstance(guard["lock_minutes"], (int, float))
    assert str(guard["path"]).endswith(".json")

    # ② models 必须是 {模型名: 计数对象}，**不是**被拍平的字符串
    assert isinstance(guard["models"], dict)
    for name, snap in guard["models"].items():
        assert isinstance(name, str) and name, "模型名不能为空"
        assert isinstance(snap, dict), f"{name} 的快照必须是对象"
        for key in ("locked", "remaining_s", "consecutive_429",
                    "total_429", "total_success", "last_reason"):
            assert key in snap, f"{name} 缺字段 {key}"
        assert isinstance(snap["locked"], bool)
        for key in ("consecutive_429", "total_429", "total_success"):
            assert isinstance(snap[key], int) and snap[key] >= 0
        # ③ ★ 未锁定时 `remaining_s` 必须是 None（不是 0）
        if snap["locked"]:
            assert isinstance(snap["remaining_s"], (int, float))
            assert snap["remaining_s"] > 0, "锁定中的剩余时间必须 > 0"
        else:
            assert snap["remaining_s"] is None, (
                "未锁定时 remaining_s 必须是 null —— 用 0 会读成"
                "「刚好到期」，而实际是「根本没锁」")


def test_health_rate_guard_reports_unavailable_instead_of_zero(client, monkeypatch):
    """★ 读不到状态文件时：`available=false`，**不许**给出"0 次限流"。

    「没量到」与「量到 0」必须分开（AGENTS.md 硬约束）——
    这是同一类缺陷里最容易复发的一种：用 0 糊过去，界面上是绿的。
    """
    from src.infrastructure.llm import rate_limit_guard as mod

    class _Boom(mod.RateLimitGuard):
        def snapshot(self, **kw):  # noqa: ANN003
            raise OSError("模拟状态文件读不到")

    monkeypatch.setattr(mod, "_GUARD", _Boom(path="ignored.json"))

    body = client.get("/api/v1/health").json()
    guard = body["model_gateway"]["rate_limit_guard"]
    assert guard["available"] is False, "读不到时必须如实报 available=false"
    assert "error" in guard and guard["error"], "要给出原因，不能只留一个 false"
    assert "models" not in guard, (
        "读不到时不许给 models —— 空 dict 会被前端读成「0 次限流」（假绿）")
