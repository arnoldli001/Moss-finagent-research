"""个股口径 API 测试：每用户各自一份权重/阈值 + copy-on-write。

对应设计：§7.2.6③、§6.1。

## 本文件的核心命题

**"同一只票，两个用户可以有各自的权重，且互不影响。"**

旧表 `dim_intraday_profile` 的主键是 `code` **一列** ——
结构上就存不下两份，所以 A 调完 B 也被改。这不是"忘了加 where 条件"，
是**表结构层面的错误**，必须换表才能解决
（新表主键 `(tenant_id, user_id, code, mode)`）。

本文件用 HTTP 层测试把三件事钉死：
  1. 跨用户隔离（A 改权重，B 拿到的还是默认）；
  2. copy-on-write（**读不物化**，还原默认 = 删自己的行）；
  3. 无 user_id 泄漏通道（请求体里不接受 user_id）。
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from src.api.routes.auth import get_auth_service, reset_auth_service
from src.domain.auth.human_check import reset_challenge_service
from src.domain.auth.ratelimit import reset_ip_rate_limiter
from src.domain.quota.service import reset_quota_service

PASSWORD = "ProfileUser#2026x"


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("MOSS_ENV", "test")
    db_path = str(tmp_path / "profiles.db")
    monkeypatch.setenv("MOSS_SQLITE_PATH", db_path)
    monkeypatch.setenv("MOSS_NOTIFY_CHANNEL", "console")
    # 每个用例用独立库，逻辑名到真实用户名的映射也必须重置，
    # 否则第二个用例会拿着上一个用例的用户名去登录（库里已经没有那个人）
    _ACCOUNTS.clear()
    from src.core.config import get_settings

    get_settings.cache_clear()
    reset_auth_service()
    reset_quota_service()
    # 图形码与 IP 限流是**进程级单例**（窗口 300 秒 / 上限 60 次请求），
    # 不重置就跨用例累加：单跑本文件不超限，和别的登录密集文件一起跑
    # 就整片 429，而报错停在"登录"这一步，指不到限流器（实测 2026-09-23）。
    reset_challenge_service()
    reset_ip_rate_limiter()

    from src.api.main import app

    c = TestClient(app)
    service = get_auth_service()
    assert str(getattr(service._repo, "_db_path", "")) == db_path
    service._repo.clear_all()
    yield c
    reset_auth_service()
    reset_quota_service()
    reset_challenge_service()
    reset_ip_rate_limiter()
    get_settings.cache_clear()


#: 逻辑名 → 真实用户名。同一逻辑名（"alice"）**复用同一个账号**。
#:
#: ⚠️ 为什么需要这张表：测试的套路是"A 改完 → 登出 → B 改 → 登出 → 回来验 A"。
#: 如果每次 `make_and_login("alice")` 都新建一个账号，那"回来验 A"验的是
#: **第三个新用户**（自然是默认值），断言会以"隔离失效"的样子失败 ——
#: 而实际上隔离是好的。**辅助函数的语义必须与测试意图一致**：
#: 同一个名字 = 同一个人。
_ACCOUNTS: dict[str, str] = {}


def make_and_login(client: TestClient, name: str) -> str:
    """按逻辑名建号（首次）或登录（之后复用），返回 `user_id`。"""
    repo = get_auth_service()._repo
    username = _ACCOUNTS.get(name)
    user_id = ""
    if username is None:
        suffix = secrets.token_hex(4)
        username = f"{name}_{suffix}"
        user_id = f"u_{suffix}"
        repo.create_user(user_id=user_id, username=username,
                         display_name=name, status="active",
                         applied_tier="vip",
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


# ======================================================================
# 一、生效口径与 copy-on-write
# ======================================================================

def test_effective_profile_falls_back_to_default(client: TestClient) -> None:
    """★ 没调过 → 返回默认，且 **`from_user=False`**（前端据此显示"跟随系统默认"）。

    少了这个标志，界面上"我调过的"与"跟默认走的"长得一模一样，
    用户会以为自己调过、或者去"改"一份本来不该存在的档案。
    """
    make_and_login(client, "alice")
    r = client.get("/api/v1/me/profiles/600519")
    assert r.status_code == 200, r.text[:200]
    body = r.json()
    assert body["from_user"] is False
    assert body["source"] == "default"
    assert body["weights"] == {}
    assert body["explain"], "没有说明这条口径是从哪来的"


def test_save_then_read_returns_own_values(client: TestClient) -> None:
    make_and_login(client, "alice")
    r = client.put("/api/v1/me/profiles/600519", json={
        "code": "600519", "mode": "intraday",
        "weights": {"box": 17.0, "chan": 12.0},
        "thresholds": {"action": 20.0},
        "levels": {"atr_stop_mult": 0.6},
        "boards": ["白酒"],
    })
    assert r.status_code == 200, r.text[:250]
    assert r.json()["profile"]["from_user"] is True

    got = client.get("/api/v1/me/profiles/600519").json()
    assert got["from_user"] is True
    assert got["weights"] == {"box": 17.0, "chan": 12.0}
    assert got["boards"] == ["白酒"]
    assert "你自己" in got["explain"]


def test_reset_restores_inheritance(client: TestClient) -> None:
    """★ "还原系统默认" = **删掉我自己的行**，不是把默认值写进去。

    写进去之后，将来系统默认值升级就再也到不了这个用户
    （他被钉在"还原那一刻"的旧默认值上），而且库会膨胀出一堆假档案。
    """
    make_and_login(client, "alice")
    client.put("/api/v1/me/profiles/600519",
               json={"code": "600519", "weights": {"box": 99.0}})
    assert client.get("/api/v1/me/profiles/600519").json()["from_user"] is True

    r = client.delete("/api/v1/me/profiles/600519")
    assert r.status_code == 200
    assert r.json()["removed"] is True
    after = client.get("/api/v1/me/profiles/600519").json()
    assert after["from_user"] is False
    assert after["weights"] == {}, "还原后仍带着旧权重（没有真正回退）"


def test_reset_is_idempotent(client: TestClient) -> None:
    make_and_login(client, "alice")
    r = client.delete("/api/v1/me/profiles/600519")
    assert r.status_code == 200
    assert r.json()["removed"] is False
    assert "跟随系统默认" in r.json()["message"]


def test_list_only_returns_own_customizations(client: TestClient) -> None:
    make_and_login(client, "alice")
    client.put("/api/v1/me/profiles/600519", json={"code": "600519"})
    client.put("/api/v1/me/profiles/000001", json={"code": "000001"})
    client.get("/api/v1/me/profiles/300750")     # 只读不写

    rows = client.get("/api/v1/me/profiles").json()
    assert {p["code"] for p in rows["profiles"]} == {"600519", "000001"}
    assert rows["total"] == 2, "只读过的票不该出现在列表里（读不物化）"


# ======================================================================
# 二、跨用户隔离（本文件最重要的一组）
# ======================================================================

def test_same_code_two_users_are_independent(client: TestClient) -> None:
    """★★ 同一只票，A 调了权重，**B 必须看到默认值**。

    这是整个多用户改造里最核心的一条：旧表主键只有 `code`，
    物理上就存不下两份 —— 所以这条测试在旧结构下**不可能通过**。
    """
    make_and_login(client, "alice")
    client.put("/api/v1/me/profiles/600519", json={
        "code": "600519", "weights": {"box": 17.0}})
    client.post("/api/v1/auth/logout")

    make_and_login(client, "bob")
    got = client.get("/api/v1/me/profiles/600519").json()
    assert got["from_user"] is False, "B 看到了 A 的自定义口径"
    assert got["weights"] == {}, f"B 拿到了 A 的权重：{got['weights']}"
    assert client.get("/api/v1/me/profiles").json()["profiles"] == []

    # B 自己设一份，两份必须共存
    client.put("/api/v1/me/profiles/600519", json={
        "code": "600519", "weights": {"box": 3.0}})
    client.post("/api/v1/auth/logout")

    make_and_login(client, "alice")
    mine = client.get("/api/v1/me/profiles/600519").json()
    assert mine["weights"] == {"box": 17.0}, "A 的口径被 B 覆盖了"


def test_delete_only_affects_self(client: TestClient) -> None:
    """★ A "还原默认"不能把 B 的口径一起删掉。"""
    make_and_login(client, "alice")
    client.put("/api/v1/me/profiles/600519", json={"code": "600519",
                                                   "weights": {"box": 17.0}})
    client.post("/api/v1/auth/logout")

    make_and_login(client, "bob")
    client.put("/api/v1/me/profiles/600519", json={"code": "600519",
                                                   "weights": {"box": 3.0}})
    client.delete("/api/v1/me/profiles/600519")
    client.post("/api/v1/auth/logout")

    make_and_login(client, "alice")
    assert client.get("/api/v1/me/profiles/600519").json()["weights"] == {
        "box": 17.0}, "A 的口径被 B 的还原操作删掉了"


def test_mode_is_part_of_identity(client: TestClient) -> None:
    """做T（intraday）与日线（daily）是**两份**口径，互不污染。"""
    make_and_login(client, "alice")
    client.put("/api/v1/me/profiles/600519",
               json={"code": "600519", "mode": "intraday",
                     "weights": {"box": 17.0}})
    client.put("/api/v1/me/profiles/600519",
               json={"code": "600519", "mode": "daily",
                     "weights": {"trend": 7.0}})

    i = client.get("/api/v1/me/profiles/600519?mode=intraday").json()
    d = client.get("/api/v1/me/profiles/600519?mode=daily").json()
    assert i["weights"] == {"box": 17.0}
    assert d["weights"] == {"trend": 7.0}
    assert i["caliber_key"] != d["caliber_key"]


# ======================================================================
# 三、口径指纹（"计算可共享、参数不共享"的落点）
# ======================================================================

def test_caliber_key_same_for_same_params(client: TestClient) -> None:
    """★ 两个用户口径相同 → 指纹相同 → **可以共用一次计算**。

    这正是"计算可以共享"的机制：缓存键用指纹而不是 user_id，
    才不会把本该共享的重复计算掉。
    """
    make_and_login(client, "alice")
    client.put("/api/v1/me/profiles/600519", json={
        "code": "600519", "weights": {"box": 17.0, "chan": 12.0}})
    a = client.get("/api/v1/me/profiles/600519").json()["caliber_key"]
    client.post("/api/v1/auth/logout")

    make_and_login(client, "bob")
    client.put("/api/v1/me/profiles/600519", json={
        "code": "600519", "weights": {"chan": 12.0, "box": 17.0}})  # 顺序不同
    b = client.get("/api/v1/me/profiles/600519").json()["caliber_key"]

    assert a == b, "口径相同的两个用户指纹不同 —— 本该共享的计算被重复了"


def test_caliber_key_differs_for_different_params(client: TestClient) -> None:
    """★ 口径不同 → 指纹不同 → **绝不能共用缓存**（否则 B 看到 A 的指标）。"""
    make_and_login(client, "alice")
    client.put("/api/v1/me/profiles/600519", json={
        "code": "600519", "weights": {"box": 17.0}})
    a = client.get("/api/v1/me/profiles/600519").json()["caliber_key"]
    client.put("/api/v1/me/profiles/600519", json={
        "code": "600519", "weights": {"box": 18.0}})
    c = client.get("/api/v1/me/profiles/600519").json()["caliber_key"]
    assert a != c


def test_boards_do_not_affect_caliber_key(client: TestClient) -> None:
    """★ 板块归属是"这只票是什么"，权重是"怎么给它打分"。

    改板块名不该让口径指纹变化 —— 否则每次修订板块归属，
    该票的**全部缓存**失效，而且本该共享的计算不再共享。
    """
    make_and_login(client, "alice")
    client.put("/api/v1/me/profiles/600519", json={
        "code": "600519", "weights": {"box": 17.0}, "boards": ["白酒"]})
    k1 = client.get("/api/v1/me/profiles/600519").json()["caliber_key"]
    client.put("/api/v1/me/profiles/600519", json={
        "code": "600519", "weights": {"box": 17.0},
        "boards": ["白酒", "沪股通"], "overseas": ["usNVDA"]})
    k2 = client.get("/api/v1/me/profiles/600519").json()["caliber_key"]
    assert k1 == k2, "改板块归属导致口径指纹变化（缓存会被无谓地全清）"


# ======================================================================
# 四、输入校验与越权
# ======================================================================

def test_rejects_non_finite_numbers(client: TestClient) -> None:
    """★ NaN/Infinity 必须在入口挡住。

    它们会**静默污染后续所有计算**（总分变 NaN，界面显示"NaN 分"
    而没有任何报错），而且 `NaN` 不是合法 JSON、Python 却默认接受。
    """
    make_and_login(client, "alice")
    for bad in ('{"code":"600519","weights":{"box":NaN}}',
                '{"code":"600519","weights":{"box":Infinity}}'):
        r = client.put("/api/v1/me/profiles/600519",
                       content=bad.encode(),
                       headers={"Content-Type": "application/json"})
        assert r.status_code == 400, f"{bad} → {r.status_code} {r.text[:160]}"
        assert "有限数字" in r.text


def test_rejects_unknown_mode(client: TestClient) -> None:
    make_and_login(client, "alice")
    r = client.put("/api/v1/me/profiles/600519",
                   json={"code": "600519", "mode": "weekly"})
    assert r.status_code == 400
    assert "mode" in r.text


def test_rejects_invalid_code(client: TestClient) -> None:
    make_and_login(client, "alice")
    assert client.get("/api/v1/me/profiles/830799").status_code == 422
    assert client.put("/api/v1/me/profiles/sh600519",
                      json={"code": "sh600519"}).status_code == 422


def test_request_body_cannot_set_user_id(client: TestClient) -> None:
    """★ 请求体里塞 `user_id` **不能**改变写入归属（越权回归）。

    如果后端从 body 取 user_id，任何人都能给别人的票改权重 ——
    这是最典型的越权写法。归属只由会话决定。
    """
    alice_id = make_and_login(client, "alice")
    r = client.put("/api/v1/me/profiles/600519", json={
        "code": "600519", "weights": {"box": 17.0},
        "user_id": "u_victim", "user": "someone_else"})
    assert r.status_code == 200
    # 落库归属必须是 alice
    repo = get_auth_service()._repo
    stored = repo.get_user(alice_id)
    assert stored is not None
    from src.domain.quota.service import get_quota_service

    profile = get_quota_service()._repo.effective_profile(  # noqa: SLF001
        tenant_id="vip", user_id=alice_id, code="600519")
    assert profile.from_user is True and profile.weights == {"box": 17.0}
    # 且不存在一个叫 u_victim 的档案
    victim = get_quota_service()._repo.effective_profile(  # noqa: SLF001
        tenant_id="vip", user_id="u_victim", code="600519")
    assert victim.from_user is False


def test_profiles_require_login(client: TestClient) -> None:
    assert client.get("/api/v1/me/profiles").status_code == 401
    assert client.get("/api/v1/me/profiles/600519").status_code == 401
    assert client.put("/api/v1/me/profiles/600519",
                      json={"code": "600519"}).status_code == 401


def test_expired_user_cannot_save_profile(client: TestClient) -> None:
    """到期后能看不能写（与自选池同一口径）。"""
    uid = make_and_login(client, "expired")
    repo = get_auth_service()._repo
    past = (datetime.now().astimezone() - timedelta(days=1)).isoformat(
        timespec="seconds")
    repo.set_user_tier_and_validity(uid, valid_until=past)

    assert client.put("/api/v1/me/profiles/600519",
                      json={"code": "600519",
                            "weights": {"box": 1.0}}).status_code == 403
    # 读仍然允许：用户要能看到自己的东西
    assert client.get("/api/v1/me/profiles/600519").status_code == 200
