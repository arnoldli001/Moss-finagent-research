"""自定义板块的**归属隔离**（2026-10-08，`CHG-0222`/`CHG-0223`）。

## 本文件的核心命题

**"板块是每个账号自己的一份"** —— 隔离之前，`dim_quant_sector` 是一张
`name TEXT NOT NULL UNIQUE` 的全局表，任何账号看到的是同一份、而且改得动
（pilot 实测：1 个管理员 + 32 个 VIP 客户共用同一批 7 个板块）。

## 两层判据（仓库层 + HTTP 层）

- **仓库层**：归属并进 SQL 谓词（`WHERE id=? AND user_id=?`），别人的 id
  表现为"不存在"，且**不留任何副作用**（不能删掉别人的成分）；
- **HTTP 层**：未登录 401；别人的板块 404（**与不存在同一个状态码**，
  否则就是一条可枚举别人 id 的通道）；同名板块在**不同账号下可以并存**。

README.md 里那句"知道别人的 `pool_id` 也读不到（404）"讲的是已删除的个人池
路由；本文件把同一条纪律钉在**现役**的自定义板块上。
"""

from __future__ import annotations

import asyncio
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.api.routes.auth import get_auth_service, reset_auth_service
from src.domain.auth.human_check import reset_challenge_service
from src.domain.auth.ratelimit import reset_ip_rate_limiter
from src.domain.quota.service import reset_quota_service

#: 量化选股（含自定义板块）是**私有资产**：`src/quant/quant_select_*.py` 在
#: `.gitignore` 里（`.gitignore:74`）。公开 checkout 里没有这个模块 ⇒
#: 本文件按**知名降级**显式 skip，既不制造红灯、也不静默绿灯
#: （同一模式见 `tests/unit/test_prd_sync_check.py:44-61`，
#: 由 `tests/unit/test_shipped_deps.py` 的守卫统一要求）。
_PRIVATE_MODULE = (Path(__file__).resolve().parents[2]
                   / "src" / "quant" / "quant_select_repo.py")
if not _PRIVATE_MODULE.exists():  # pragma: no cover - 只在公开 checkout 走到
    pytest.skip("量化选股模块未随仓库发布（私有资产，见 .gitignore:74）",
                allow_module_level=True)

from src.quant.quant_select_repo import QuantSector, QuantSelectSqliteRepository  # noqa: E402

OWNER_A = "u_owner_a"
OWNER_B = "u_owner_b"
PASSWORD = "SectorUser#2026x"


# ======================================================================
# 一、仓库层：归属并进查询谓词
# ======================================================================

def _repo(tmp_path) -> QuantSelectSqliteRepository:
    return QuantSelectSqliteRepository(str(tmp_path / "sector.db"))


def _seed(repo: QuantSelectSqliteRepository, owner: str, name: str,
          codes: list[str]) -> QuantSector:
    return asyncio.run(repo.upsert_sector(QuantSector(
        user_id=owner, name=name,
        members=[{"code": code} for code in codes])))


def test_owner_is_required(tmp_path) -> None:
    """★ 空 `user_id` 必须**报错**，不能退化成"公共板块"。

    这是本文件最重要的一条"防静默"判据：`alerts._viewer_user_id` 那种
    "拿不到身份 → 退回全局行为"的降级在**读**路径上可以接受，
    在**写**路径上等于隔离失效（那个文件自己也写明了这一点）。
    """
    repo = _repo(tmp_path)
    with pytest.raises(ValueError):
        asyncio.run(repo.upsert_sector(QuantSector(name="没有归属")))
    with pytest.raises(ValueError):
        asyncio.run(repo.list_sectors(user_id=""))


def test_two_owners_are_isolated_in_repo(tmp_path) -> None:
    repo = _repo(tmp_path)
    _seed(repo, OWNER_A, "机器人概念", ["300124"])
    _seed(repo, OWNER_B, "福建板块", ["600033", "000993"])

    a_names = [s.name for s in asyncio.run(repo.list_sectors(user_id=OWNER_A))]
    b_names = [s.name for s in asyncio.run(repo.list_sectors(user_id=OWNER_B))]
    assert a_names == ["机器人概念"], "A 看到了别人的板块"
    assert b_names == ["福建板块"], "B 看到了别人的板块"


def test_members_of_others_are_not_read(tmp_path) -> None:
    """成分只查**属于本次这几个板块**的行（不是"查全表再过滤"）。"""
    repo = _repo(tmp_path)
    _seed(repo, OWNER_A, "A板", ["600150"])
    _seed(repo, OWNER_B, "B板", ["300308"])
    a = asyncio.run(repo.list_sectors(user_id=OWNER_A, with_members=True))
    assert [m["code"] for m in a[0].members] == ["600150"]


def test_same_name_can_coexist_across_owners(tmp_path) -> None:
    """★★ 两个账号各自有一个「机器人概念」—— 旧表的全局 UNIQUE 下这条**不可能通过**。

    旧行为更糟：后者建同名板块会命中前者那一行，`UPDATE` 它、再 `_replace_members`
    抹掉它的成分（前端完全看不出来撞了）。
    """
    repo = _repo(tmp_path)
    first = _seed(repo, OWNER_A, "机器人概念", ["300124"])
    second = _seed(repo, OWNER_B, "机器人概念", ["002472"])

    assert first.id != second.id
    a = asyncio.run(repo.list_sectors(user_id=OWNER_A))[0]
    b = asyncio.run(repo.list_sectors(user_id=OWNER_B))[0]
    assert [m["code"] for m in a.members] == ["300124"], "同名建板改掉了别人的成分"
    assert [m["code"] for m in b.members] == ["002472"]


def test_same_name_within_one_owner_still_updates(tmp_path) -> None:
    """同一账号内同名 = 更新（保持原有的幂等语义，前端"保存"不必先查 id）。"""
    repo = _repo(tmp_path)
    first = _seed(repo, OWNER_A, "我的池", ["600150"])
    second = asyncio.run(repo.upsert_sector(QuantSector(
        user_id=OWNER_A, name="我的池", note="v2")))
    assert second.id == first.id
    rows = asyncio.run(repo.list_sectors(user_id=OWNER_A))
    assert len(rows) == 1 and rows[0].note == "v2"


def test_cannot_touch_others_sector(tmp_path) -> None:
    """★ 越权必须**零副作用**：删不掉、改不掉，而且不能连带删掉成分。"""
    repo = _repo(tmp_path)
    a = _seed(repo, OWNER_A, "A板", ["600150", "300308"])

    assert asyncio.run(repo.delete_sector(a.id, user_id=OWNER_B)) is False
    assert asyncio.run(repo.remove_member(a.id, OWNER_B, "600150")) is False
    with pytest.raises(KeyError):
        asyncio.run(repo.set_members(a.id, OWNER_B, [{"code": "000001"}]))
    with pytest.raises(KeyError):
        asyncio.run(repo.add_members(a.id, OWNER_B, [{"code": "000001"}]))

    still = asyncio.run(repo.list_sectors(user_id=OWNER_A))[0]
    assert sorted(m["code"] for m in still.members) == ["300308", "600150"], \
        "越权请求改了别人板块的成分"


def test_owner_is_not_exposed_to_clients(tmp_path) -> None:
    """`to_dict()` 里没有归属字段：归属只由服务端会话决定（与 `/me/profiles` 同纪律）。"""
    repo = _repo(tmp_path)
    payload = _seed(repo, OWNER_A, "A板", []).to_dict()
    assert "user_id" not in payload and "tenant_id" not in payload


# ======================================================================
# 二、HTTP 层：401 / 404 / 同名并存 / 请求体不能指定归属
# ======================================================================

@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("MOSS_ENV", "test")
    db_path = str(tmp_path / "api.db")
    monkeypatch.setenv("MOSS_SQLITE_PATH", db_path)
    monkeypatch.setenv("MOSS_NOTIFY_CHANNEL", "console")
    _ACCOUNTS.clear()
    from src.core.config import get_settings

    get_settings.cache_clear()
    reset_auth_service()
    reset_quota_service()
    reset_challenge_service()
    reset_ip_rate_limiter()

    from src.api.main import app
    from src.quant.quant_select_service import QuantSelectService

    service = QuantSelectService(
        repo=QuantSelectSqliteRepository(db_path),
        result_file=str(tmp_path / "latest.json"))
    # 注入最小 runtime：板块端点只需要 `quant_select`（与
    # `test_intraday_weight_profiles_api.py` 同一套替身写法）。
    # **不用 `with TestClient(...)`**：那会跑整段 lifespan（调度器、预热），
    # 与本文件要验的东西无关，还会让用例变慢、变脆。
    app.state.runtime = SimpleNamespace(quant_select=service)
    c = TestClient(app)
    service_auth = get_auth_service()
    assert str(getattr(service_auth._repo, "_db_path", "")) == db_path
    service_auth._repo.clear_all()
    yield c
    reset_auth_service()
    reset_quota_service()
    reset_challenge_service()
    reset_ip_rate_limiter()
    get_settings.cache_clear()


#: 逻辑名 → 真实用户名（同一逻辑名 = 同一个人；理由见 `test_my_profiles_api.py`）。
_ACCOUNTS: dict[str, str] = {}


def make_and_login(client: TestClient, name: str, *,
                   tier: str = "vip") -> str:
    repo = get_auth_service()._repo
    username = _ACCOUNTS.get(name)
    user_id = ""
    if username is None:
        suffix = secrets.token_hex(4)
        username = f"{name}_{suffix}"
        user_id = f"u_{suffix}"
        repo.create_user(user_id=user_id, username=username, display_name=name,
                         status="active", applied_tier=tier,
                         valid_until=(datetime.now().astimezone()
                                      + timedelta(days=30)).isoformat(
                                          timespec="seconds"))
        repo.set_password(user_id, PASSWORD)
        _ACCOUNTS[name] = username
    r = client.post("/api/v1/auth/login",
                    json={"account": username, "password": PASSWORD,
                          "remember_me": False})
    assert r.status_code == 200, f"登录 {username} 失败：{r.text[:200]}"
    if not user_id:
        found = repo.get_user_by_username(username)
        assert found is not None
        user_id = found.user_id
    return user_id


def _create(client: TestClient, name: str, codes: list[str] | None = None,
            **extra) -> dict:
    r = client.post("/api/v1/quant/sectors",
                    json={"name": name,
                          "members": [{"code": c} for c in (codes or [])],
                          **extra})
    assert r.status_code == 200, r.text[:300]
    return r.json()


def test_sector_endpoints_require_login(client: TestClient) -> None:
    """★ 未登录 401（隔离前这组端点**没有任何身份依赖**，谁都能读写）。"""
    assert client.get("/api/v1/quant/sectors").status_code == 401
    assert client.post("/api/v1/quant/sectors",
                       json={"name": "x"}).status_code == 401
    assert client.delete("/api/v1/quant/sectors/1").status_code == 401


def test_two_accounts_are_isolated_over_http(client: TestClient) -> None:
    """★★ A 建的板块：B 的列表里没有、按 id 直取/删除 **404**，且 A 那边仍在。"""
    make_and_login(client, "alice")
    created = _create(client, "端侧算力", ["300124"])
    sector_id = created["id"]
    assert created["member_count"] == 1

    client.post("/api/v1/auth/logout")
    make_and_login(client, "bob")

    listed = client.get("/api/v1/quant/sectors").json()
    assert listed["count"] == 0, f"B 看到了 A 的板块：{listed}"
    rejected = client.delete(f"/api/v1/quant/sectors/{sector_id}")
    assert rejected.status_code == 404
    # 结构化里带机器可读 code（前端文案层认它），且**不承认**"这个 id 存在"
    assert rejected.json()["detail"]["code"] == "sector_not_found"
    r = client.put(f"/api/v1/quant/sectors/{sector_id}/members",
                   json={"members": [{"code": "000001"}]})
    assert r.status_code == 404
    assert client.delete(
        f"/api/v1/quant/sectors/{sector_id}/members/300124").status_code == 404

    client.post("/api/v1/auth/logout")
    make_and_login(client, "alice")
    mine = client.get("/api/v1/quant/sectors").json()
    assert [s["name"] for s in mine["sectors"]] == ["端侧算力"]
    assert [m["code"] for m in mine["sectors"][0]["members"]] == ["300124"], \
        "别人的越权请求改了 A 的板块"


def test_same_name_across_accounts_over_http(client: TestClient) -> None:
    make_and_login(client, "alice")
    _create(client, "机器人概念", ["300124"])
    client.post("/api/v1/auth/logout")
    make_and_login(client, "bob")
    _create(client, "机器人概念", ["002472"])       # 旧全局唯一键下这里会 500/覆盖
    assert [s["name"] for s in client.get("/api/v1/quant/sectors").json()["sectors"]] \
        == ["机器人概念"]
    assert client.get("/api/v1/quant/sectors").json()["sectors"][0]["member_count"] == 1


def test_request_body_cannot_set_owner(client: TestClient) -> None:
    """★ 请求体里塞 `user_id`/`tenant_id` **不改变归属**（越权的第一步就是让前端传它）。"""
    mine = make_and_login(client, "alice")
    victim = "u_victim_should_not_own_this"
    _create(client, "想嫁祸的板块", ["600150"],
            user_id=victim, tenant_id="admin")
    listed = client.get("/api/v1/quant/sectors").json()
    assert [s["name"] for s in listed["sectors"]] == ["想嫁祸的板块"]

    from src.core.config import get_settings
    from src.quant.quant_select_repo import QuantSelectSqliteRepository

    repo = QuantSelectSqliteRepository(get_settings().sqlite_path)
    with repo._connect() as conn:                    # noqa: SLF001
        owners = [row["user_id"] for row in conn.execute(
            "SELECT user_id FROM dim_quant_sector").fetchall()]
    assert owners == [mine], f"归属被请求体改掉了：{owners}"
    assert victim not in owners
