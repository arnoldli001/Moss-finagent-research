"""`/health` 暴露**外部搜索源**闸门状态的契约（`CHG-0122`）—— 离线、不联网。

## 这份测试守的是什么

搜索源的额度/凭据是"**要维护的东西**"，而运维看的是面板。把状态并进
`/health` ⇒ 复用既有的 20 秒轮询面，**不新增任何请求**（性能硬约束：禁止新增串行往返）。

代价是 `/health` 是**热路径**，所以三条判据都指向"别把它弄坏"：

| 判据 | 防的失效 |
|---|---|
| 字段在、形状对（`order` + 每家的 `provider/configured/exhausted`） | 字段名两边各改各的 ⇒ **不报错，只显示错**（与 `local_stores` 那次同一形状） |
| ★ **`/health` 绝不许发网络请求** | 本项目实测过"在请求路径上做真探测"：对 14GB 库 COUNT(*) ⇒ `/health` 卡到 **300 秒**超时，同屏面板一起卡死 |
| ★ 段内异常**降级不 500** | 搜索源是锦上添花的兜底，它坏了不该把整张健康检查打挂 |

★ 第二条用**会抛异常的哨兵**替换两家的 `web_search` —— 判据是"**没被调用**"，
不是"看着像没调用"（本项目已因"补丁打错层"真实花过一次搜索额度）。
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """与既有 `/health` 契约测试同一套隔离方式（`MOSS_ENV=test` + 临时库）。"""
    monkeypatch.setenv("MOSS_SQLITE_PATH", str(tmp_path / "health.db"))
    monkeypatch.setenv("LLM_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("MOSS_ENV", "test")
    from fastapi.testclient import TestClient

    from src.api.main import app

    with TestClient(app) as c:
        yield c


def _section(client) -> dict:
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200, resp.text
    return resp.json().get("data_sources", {}).get("search_sources") or {}


def test_health_exposes_search_sources_contract(client) -> None:
    """形状契约：前端/运维渲染要的字段与类型必须齐。"""
    sec = _section(client)
    assert sec, "`data_sources.search_sources` 缺失 —— 面板看不到搜索源额度"
    assert sec.get("available") is True, sec
    assert sec["order"] == ["bocha", "baidu"], "主/备顺序变了却没同步文档与判据"
    provs = sec["providers"]
    assert [p["provider"] for p in provs] == ["bocha", "baidu"]
    for p in provs:
        #: 这几个键是"能不能用/还剩多少"的最小可判集合
        assert "configured" in p and isinstance(p["configured"], bool)
        assert "exhausted" in p and isinstance(p["exhausted"], bool)
        assert "used_today" in p and "limit_today" in p


def test_health_never_calls_the_search_providers(client, monkeypatch) -> None:
    """★ **`/health` 绝不许发网络请求**（热路径纪律）。

    判据用**会抛异常的哨兵**：只要 `/health` 真的去搜了，这里就会炸。
    """
    from src.infrastructure.search import baidu, bocha

    def _forbidden(*_a, **_k):
        raise AssertionError("/health 调用了真实搜索 —— 会花钱且拖慢 20 秒轮询的面板")

    monkeypatch.setattr(bocha, "web_search", _forbidden)
    monkeypatch.setattr(baidu, "web_search", _forbidden)

    sec = _section(client)          # 不抛 ⇒ 确实没去搜
    assert sec.get("available") is True


def test_health_search_section_degrades_instead_of_500(client, monkeypatch) -> None:
    """段内异常 ⇒ 降级成 `available: False`，**不许** 500（同 `_data_health` 的纪律）。"""
    from src.infrastructure.search import router as r

    def boom() -> list:
        raise RuntimeError("搜索源状态炸了")

    monkeypatch.setattr(r, "providers_status", boom)
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200, "搜索源段坏了却把整张 /health 打挂"
    sec = resp.json()["data_sources"]["search_sources"]
    assert sec["available"] is False and "搜索源状态炸了" in sec["error"]
