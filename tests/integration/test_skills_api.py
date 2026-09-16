"""技能目录REST路由集成测试（不依赖runtime/lifespan）。"""

import httpx
import pytest

from src.api.main import app


@pytest.fixture()
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_skill_quality_gate_clean(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/skills/quality")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True, body["problems"]
    assert body["problems"] == []


@pytest.mark.asyncio
async def test_all_skill_indexes_include_a17(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/skills")
    assert resp.status_code == 200
    body = resp.json()
    assert "A17_recommend" in body
    names = {s["name"] for s in body["A17_recommend"]}
    assert "multi-source-synthesis" in names


@pytest.mark.asyncio
async def test_agent_skill_index_l0(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/skills/A10_micro")
    assert resp.status_code == 200
    body = resp.json()
    assert body["agent_id"] == "A10_micro"
    # L0只含frontmatter摘要，不泄露正文
    for skill in body["skills"]:
        assert set(skill) == {"name", "description", "tags"}
        assert "执行步骤" not in str(skill)


@pytest.mark.asyncio
async def test_agent_skill_detail_l1(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/skills/A10_micro/multi-method-valuation")
    assert resp.status_code == 200
    content = resp.json()["content"]
    assert "执行步骤" in content  # 正文可见


@pytest.mark.asyncio
async def test_unknown_agent_404(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/skills/A99_none")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_out_of_boundary_skill_404(client: httpx.AsyncClient) -> None:
    # A10请求风险技能（不在其边界内）→ 404，不泄露他agent技能
    resp = await client.get("/api/v1/skills/A10_micro/risk-scan")
    assert resp.status_code in (403, 404)
