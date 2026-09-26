"""管理员控制台 API 测试：审批 / 增删 / 套餐 / 有效期 / 权限门槛。

对应设计：`docs/PLATFORM_MULTI_TENANCY_DESIGN.md` §8.6.10。

## 为什么这一层测试必须有（不是"接口多了补个测试"）

本系统的注册是**审批制**：新用户落 `pending`、**不能登录**。
管理台是让 `pending` 变成 `active` 的**唯一入口** ——
它坏掉的表现是"用户注册完永远进不来"，而不是任何报错。

## 本文件重点钉死的四件事

1. **权限门槛**：非管理员一律 403（且是 403 不是 401，见路由里的说明）；
2. **审批真的能让人登录**：审批前 401、审批后 200 —— 端到端，不只看状态字段；
3. **管理员不能把自己锁在门外**：改自己的等级/状态必须被拒；
4. **软删释放邮箱**：删掉用户后同一邮箱能重新注册（否则用户换个邮箱才能回来）。
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from src.api.routes.auth import get_auth_service, reset_auth_service
from src.core.idempotency import reset_idempotency_store

ADMIN_PASSWORD = "AdminConsole#2026x"
USER_PASSWORD = "NormalUser#2026x"


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    """隔离库 + 内存通知通道的 TestClient（与 test_auth_routes 同口径）。"""
    monkeypatch.setenv("MOSS_ENV", "test")
    db_path = str(tmp_path / "admin.db")
    monkeypatch.setenv("MOSS_SQLITE_PATH", db_path)
    monkeypatch.setenv("MOSS_NOTIFY_CHANNEL", "console")
    from src.core.config import get_settings

    get_settings.cache_clear()
    reset_auth_service()

    from src.api.main import app

    test_client = TestClient(app)
    service = get_auth_service()
    # 隔离哨兵：装配出的必须是本用例的临时库（见 test_auth_routes 里的说明）
    assert str(getattr(service._repo, "_db_path", "")) == db_path, (
        "认证服务没有指向隔离库，会读写真实库")
    service._repo.clear_all()
    # 幂等键是**进程级单例**（`src/core/idempotency.py`），不在临时库里。
    # 不清就会跨用例残留：上一个用例的键命中后直接回放缓存响应，
    # 而那个响应属于**另一个测试的库**里已经不存在的账号。
    reset_idempotency_store()
    yield test_client
    reset_idempotency_store()
    reset_auth_service()
    get_settings.cache_clear()


def _make_user(admin_tier: str, *, username: str = "", status: str = "active",
               days: int = 30) -> tuple[str, str, str]:
    """直接建号（绕过注册审批），返回 (user_id, username, password)。"""
    repo = get_auth_service()._repo
    suffix = secrets.token_hex(4)
    username = username or f"u_{suffix}"
    user_id = f"u_{suffix}"
    repo.create_user(
        user_id=user_id, username=username, display_name=username,
        status=status, applied_tier=admin_tier,
        valid_until=(datetime.now().astimezone()
                     + timedelta(days=days)).isoformat(timespec="seconds"))
    repo.set_password(user_id, ADMIN_PASSWORD if admin_tier == "admin"
                      else USER_PASSWORD)
    return user_id, username, (ADMIN_PASSWORD if admin_tier == "admin"
                               else USER_PASSWORD)


def _login(client: TestClient, account: str, password: str) -> None:
    r = client.post("/api/v1/auth/login",
                    json={"account": account, "password": password,
                          "remember_me": False})
    assert r.status_code == 200, f"登录失败：{r.status_code} {r.text[:200]}"


# ======================================================================
# 一、权限门槛
# ======================================================================

def test_admin_endpoints_require_login(client: TestClient) -> None:
    """未登录 → 401（前端据此跳登录页）。"""
    for path in ("/api/v1/admin/overview", "/api/v1/admin/users"):
        assert client.get(path).status_code == 401, path


def test_non_admin_gets_403_not_401(client: TestClient) -> None:
    """★ 已登录但不是管理员 → **403 而不是 401**。

    401 会让前端去跳登录页 —— 而"登录一百次也不是管理员"，
    那个跳转是死循环。403 才能让前端做出正确反应：提示无权限、不跳转。
    """
    _, username, password = _make_user("vip")
    _login(client, username, password)
    r = client.get("/api/v1/admin/overview")
    assert r.status_code == 403, f"实际 {r.status_code} {r.text[:200]}"
    assert "管理员" in r.text


def test_admin_can_reach_overview(client: TestClient) -> None:
    _, username, password = _make_user("admin")
    _login(client, username, password)
    r = client.get("/api/v1/admin/overview")
    assert r.status_code == 200
    body = r.json()
    assert "counts" in body and "pending" in body
    assert body["tiers"] == ["admin", "vip", "trial"]


def test_overview_reports_which_instance_console_manages(
        client: TestClient) -> None:
    """★ 管理台必须自报"我在管哪个实例的账号库"。

    2026-09-23 实测踩到：客户在**试点实例**（8110 / data/pilot/）注册，
    界面提示"等待管理员审批"；而管理员打开的是**调试实例**（8100 /
    data/dev/）的用户管理 —— 两边账号库是分开的，于是"看不到申请记录"，
    看起来像注册根本没落库，排查方向被完全带偏。

    同机跑多个实例时，这行信息就是"我现在在管谁"的唯一凭据，
    所以它必须是**接口契约**的一部分，而不是靠人去猜端口号。
    """
    _, username, password = _make_user("admin")
    _login(client, username, password)
    inst = client.get("/api/v1/admin/overview").json()["instance"]
    assert inst["env"], "没返回环境名"
    assert inst["db"], "没返回账号库路径"
    # 必须是**实际生效**的那份路径，而不是默认值 —— 否则这行信息会骗人
    from src.core.config import get_settings

    assert inst["db"] == get_settings().sqlite_path


# ======================================================================
# 二、审批：从 pending 到能登录（端到端）
# ======================================================================

def test_pending_user_cannot_login_until_approved(client: TestClient) -> None:
    """★ 核心流程：**待审批不能登录 → 管理员审批 → 能登录**。

    只断言 `status` 字段变了是不够的 —— 真正要证明的是
    "审批之后这个人**真的能进来**"。所以这里走完整的登录 HTTP 调用。
    """
    admin_id, admin_name, admin_pwd = _make_user("admin")
    uid, uname, upwd = _make_user("trial", status="pending")

    # 审批前：登录必须被拒，且原因要说清楚（不是含糊的"失败"）。
    #
    # ⚠️ 状态码是 **403 而不是 401**，这是后端刻意的状态码约定
    # （见 `_fail()` 的映射表）：
    #   401 = 凭据/会话问题 → 前端跳登录页
    #   403 = **凭据是对的、账号状态不允许** → 前端跳提示页
    #         （"等待审批"/"已到期"/"已禁用"）
    # 用 401 会让前端一直让人重新输密码，而密码根本没错。
    r = client.post("/api/v1/auth/login",
                    json={"account": uname, "password": upwd,
                          "remember_me": False})
    assert r.status_code == 403, f"实际 {r.status_code} {r.text[:200]}"
    assert r.json()["detail"]["code"] == "status"
    assert "审批" in r.text, f"驳回原因没有提到审批：{r.text[:200]}"

    # 管理员审批
    _login(client, admin_name, admin_pwd)
    r = client.post(f"/api/v1/admin/users/{uid}/approve",
                    json={"tier": "vip", "days": 90, "note": "同事"})
    assert r.status_code == 200, f"{r.status_code} {r.text[:250]}"
    body = r.json()
    assert body["user"]["status"] == "active"
    assert body["user"]["tier"] == "vip"
    assert body["user"]["valid_until"][:10] == (
        datetime.now().astimezone() + timedelta(days=90)).date().isoformat()

    # 审批后：同一个账号真的能登录
    client.post("/api/v1/auth/logout")
    _login(client, uname, upwd)
    me = client.get("/api/v1/auth/me").json()
    assert me["user_id"] == uid and me["applied_tier"] == "vip"


def test_approve_then_reject_keeps_reason(client: TestClient) -> None:
    admin_name = _make_user("admin")[1]
    admin_pwd = ADMIN_PASSWORD
    uid, uname, upwd = _make_user("trial", status="pending")

    _login(client, admin_name, admin_pwd)
    r = client.post(f"/api/v1/admin/users/{uid}/reject?note=信息不完整")
    assert r.status_code == 200
    client.post("/api/v1/auth/logout")

    r = client.post("/api/v1/auth/login",
                    json={"account": uname, "password": upwd,
                          "remember_me": False})
    assert r.status_code == 403      # 状态问题（见上一条测试的状态码说明）
    assert "信息不完整" in r.text, f"驳回原因没传给用户：{r.text[:200]}"


def test_approve_rejects_bad_tier(client: TestClient) -> None:
    admin_name = _make_user("admin")[1]
    uid = _make_user("trial", status="pending")[0]
    _login(client, admin_name, ADMIN_PASSWORD)
    r = client.post(f"/api/v1/admin/users/{uid}/approve",
                    json={"tier": "superuser", "days": 30})
    assert r.status_code == 400
    assert "套餐等级" in r.text


# ======================================================================
# 三、增删与有效期
# ======================================================================

def test_admin_creates_user_who_can_login(client: TestClient) -> None:
    """管理员开号 → 该账号**能直接登录**（不需要走审批）。"""
    admin_name = _make_user("admin")[1]
    _login(client, admin_name, ADMIN_PASSWORD)

    r = client.post("/api/v1/admin/users", json={
        "username": "colleague", "email": "colleague@example.com",
        "password": USER_PASSWORD, "tier": "vip", "days": 30,
        "must_change_password": False})
    assert r.status_code == 200, f"{r.status_code} {r.text[:250]}"
    created = r.json()["user"]
    assert created["tier"] == "vip" and created["status"] == "active"

    client.post("/api/v1/auth/logout")
    _login(client, "colleague", USER_PASSWORD)


def test_create_user_rejects_weak_password(client: TestClient) -> None:
    admin_name = _make_user("admin")[1]
    _login(client, admin_name, ADMIN_PASSWORD)
    r = client.post("/api/v1/admin/users", json={
        "username": "weakling", "password": "123456", "tier": "trial"})
    assert r.status_code == 400
    assert "密码" in r.text


def test_create_user_rejects_duplicate_username(client: TestClient) -> None:
    admin_name = _make_user("admin")[1]
    _login(client, admin_name, ADMIN_PASSWORD)
    body = {"username": "dupe", "password": USER_PASSWORD, "tier": "trial"}
    assert client.post("/api/v1/admin/users", json=body).status_code == 200
    r = client.post("/api/v1/admin/users", json=body)
    assert r.status_code == 409


# ======================================================================
# 三之二、幂等键：让"自动重试"不会开出两个账号
# ======================================================================

def test_create_user_is_idempotent_with_same_key(client: TestClient) -> None:
    """★ 核心回归：同一个 `X-Idempotency-Key` 重发 → **只建一个**。

    这正是真实故障的形状：浏览器报 `Failed to fetch`，但服务端其实已经
    建好了（响应丢在回程）。客户端于是自动重试一次 —— 如果没有幂等键，
    就会开出两个同名账号（第二个撞唯一约束报 409，用户看到"失败"，
    但账号已经存在，比报错更难排查）。
    """
    admin_name = _make_user("admin")[1]
    _login(client, admin_name, ADMIN_PASSWORD)
    body = {"username": "retried", "password": USER_PASSWORD, "tier": "vip",
            "days": 30}
    headers = {"X-Idempotency-Key": "4f89-11d3-9a0c-0305e82c3301"}

    first = client.post("/api/v1/admin/users", json=body, headers=headers)
    second = client.post("/api/v1/admin/users", json=body, headers=headers)
    assert first.status_code == 200, first.text[:250]
    assert second.status_code == 200, second.text[:250]
    assert first.json()["user"]["user_id"] == second.json()["user"]["user_id"]
    # 回放必须**逐字**一致：响应里含初始密码，客户端靠它告知用户
    assert first.json()["initial_password"] == second.json()["initial_password"]

    users = client.get("/api/v1/admin/users").json()["users"]
    assert sum(u["username"] == "retried" for u in users) == 1, (
        "幂等键没有生效，重发建出了两个账号")


def test_create_user_with_new_key_creates_another_user(
        client: TestClient) -> None:
    """**不同**的键必须各自执行 —— 管理员确实想开两个号时不能被去重。"""
    admin_name = _make_user("admin")[1]
    _login(client, admin_name, ADMIN_PASSWORD)
    body = {"username": "twin", "password": USER_PASSWORD, "tier": "trial"}
    r1 = client.post("/api/v1/admin/users", json=body,
                     headers={"X-Idempotency-Key": "key-aaaa-0001"})
    # 同名第二个必然 409（唯一约束）；换名 + 换键应当成功
    body2 = {**body, "username": "twin2"}
    r2 = client.post("/api/v1/admin/users", json=body2,
                     headers={"X-Idempotency-Key": "key-bbbb-0002"})
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["user"]["user_id"] != r2.json()["user"]["user_id"]


def test_idempotency_key_is_scoped_per_admin(client: TestClient) -> None:
    """★ 安全性质：A 管理员的键**不能**让 B 命中 A 的响应。

    开号的响应体里有**初始密码**。键又是客户端随便给的字符串 ——
    如果不按主体再命名一次空间，B 只要猜到/枚举到 A 的键，
    就能读到 A 建号时设的明文密码。
    """
    a_name = _make_user("admin")[1]
    b_name = _make_user("admin")[1]
    shared_key = {"X-Idempotency-Key": "shared-client-key-1"}

    _login(client, a_name, ADMIN_PASSWORD)
    ra = client.post("/api/v1/admin/users", headers=shared_key, json={
        "username": "from_a", "password": USER_PASSWORD, "tier": "trial"})
    assert ra.status_code == 200
    client.post("/api/v1/auth/logout")

    _login(client, b_name, ADMIN_PASSWORD)
    rb = client.post("/api/v1/admin/users", headers=shared_key, json={
        "username": "from_b", "password": USER_PASSWORD, "tier": "trial"})
    assert rb.status_code == 200, rb.text[:250]
    # 必须是 B 自己建的那个，而不是回放 A 的结果
    assert rb.json()["user"]["username"] == "from_b"
    assert rb.json()["user"]["user_id"] != ra.json()["user"]["user_id"]


def test_invalid_idempotency_key_is_ignored_not_rejected(
        client: TestClient) -> None:
    """畸形键 → **忽略**（照常开号），不是 400。

    幂等键是可选优化、不是业务参数。中间代理或旧版前端多带一个畸形头，
    不该让一次本来能成功的开号失败 —— 那恰好就是要修的症状。
    """
    admin_name = _make_user("admin")[1]
    _login(client, admin_name, ADMIN_PASSWORD)
    body = {"username": "badkey", "password": USER_PASSWORD, "tier": "trial"}
    # 注意用**纯 ASCII** 的畸形键：HTTP 头本身不允许非 ASCII（httpx 会先报错），
    # 所以现实里能到达服务端的畸形键只能是这类（过短 / 含空格 / 含斜杠）。
    bad = {"X-Idempotency-Key": "too short / with slash"}
    r = client.post("/api/v1/admin/users", json=body, headers=bad)
    assert r.status_code == 200, r.text[:250]
    # 键非法 = 没有幂等保护 → 再发一次应当撞唯一约束（证明真的没有去重）
    again = client.post("/api/v1/admin/users", json=body, headers=bad)
    assert again.status_code == 409


def test_change_tier_and_validity(client: TestClient) -> None:
    """改套餐 + 改有效期（管理台最常用动作）。"""
    admin_name = _make_user("admin")[1]
    uid = _make_user("trial")[0]
    _login(client, admin_name, ADMIN_PASSWORD)

    r = client.patch(f"/api/v1/admin/users/{uid}",
                     json={"tier": "vip", "days": 365})
    assert r.status_code == 200, r.text[:250]
    u = r.json()["user"]
    assert u["tier"] == "vip"
    assert u["valid_until"][:10] == (
        datetime.now().astimezone() + timedelta(days=365)).date().isoformat()


def test_expire_user_blocks_login(client: TestClient) -> None:
    """把有效期设成过去 → 该用户登录被拒，且原因说明是到期。"""
    admin_name = _make_user("admin")[1]
    uid, uname, upwd = _make_user("vip", days=30)
    _login(client, admin_name, ADMIN_PASSWORD)

    past = (datetime.now().astimezone() - timedelta(days=1)).isoformat(
        timespec="seconds")
    r = client.patch(f"/api/v1/admin/users/{uid}",
                     json={"valid_until": past})
    assert r.status_code == 200, r.text[:250]

    client.post("/api/v1/auth/logout")
    r = client.post("/api/v1/auth/login",
                    json={"account": uname, "password": upwd,
                          "remember_me": False})
    assert r.status_code == 403      # 状态问题：凭据没错，是账号到期
    assert "到期" in r.text, f"到期原因没说清：{r.text[:200]}"


def test_disable_then_reenable(client: TestClient) -> None:
    admin_name = _make_user("admin")[1]
    uid, uname, upwd = _make_user("vip")
    _login(client, admin_name, ADMIN_PASSWORD)

    assert client.patch(f"/api/v1/admin/users/{uid}",
                        json={"status": "disabled"}).status_code == 200
    client.post("/api/v1/auth/logout")
    r = client.post("/api/v1/auth/login",
                    json={"account": uname, "password": upwd,
                          "remember_me": False})
    assert r.status_code == 403 and "禁用" in r.text

    _login(client, admin_name, ADMIN_PASSWORD)
    assert client.patch(f"/api/v1/admin/users/{uid}",
                        json={"status": "active"}).status_code == 200
    client.post("/api/v1/auth/logout")
    _login(client, uname, upwd)


def test_delete_user_revokes_sessions_and_frees_email(client: TestClient) -> None:
    """★ 删除用户：**踢掉在线会话** + **释放邮箱**（可重新注册）。

    只把 status 改成 deleted 是不够的：
      - 会话没吊销 → 已登录的浏览器**还能继续用**（前端只要不清 Cookie）；
      - 邮箱没释放 → 这个人再也无法用同一邮箱注册（换邮箱才能回来）。
    """
    admin_name = _make_user("admin")[1]
    repo = get_auth_service()._repo
    uid, uname, upwd = _make_user("vip")
    email = f"freed_{secrets.token_hex(3)}@example.com"
    repo.bind_contact(user_id=uid, kind="email", value=email, verified=True)

    _login(client, admin_name, ADMIN_PASSWORD)
    r = client.delete(f"/api/v1/admin/users/{uid}?note=离职")
    assert r.status_code == 200, r.text[:250]

    # 邮箱被释放
    assert repo.find_user_by_contact("email", email) is None, (
        "删除用户后邮箱仍被占用，该邮箱无法重新注册")

    # 该用户的会话全部吊销
    assert repo.count_active_sessions_by_user().get(uid, 0) == 0

    client.post("/api/v1/auth/logout")
    r = client.post("/api/v1/auth/login",
                    json={"account": uname, "password": upwd,
                          "remember_me": False})
    assert r.status_code == 401, (
        "已删除用户必须是 401（凭据问题）而不是 403 —— 软删后按用户名查不到人，"
        "对外**不能**说'这个账号被删了'，那本身就是枚举信道")
    assert "账号或密码错误" in r.text


# ======================================================================
# 四、防自锁（管理员把自己关在门外）
# ======================================================================

def test_admin_cannot_demote_self(client: TestClient) -> None:
    """★ 管理员不能改自己的套餐等级。

    系统只认 `applied_tier == 'admin'`。唯一的 admin 把自己改成 trial 之后，
    **没有任何界面**能把权限加回来（只能改库）—— 这是运维事故级的问题，
    必须在服务端堵住，而不是靠"提醒用户别这么干"。
    """
    admin_id, admin_name, admin_pwd = _make_user("admin")
    _login(client, admin_name, admin_pwd)
    r = client.patch(f"/api/v1/admin/users/{admin_id}", json={"tier": "trial"})
    assert r.status_code == 400
    assert "自己" in r.text

    # 权限仍在
    assert client.get("/api/v1/admin/overview").status_code == 200


def test_admin_cannot_disable_or_delete_self(client: TestClient) -> None:
    admin_id, admin_name, admin_pwd = _make_user("admin")
    _login(client, admin_name, admin_pwd)
    assert client.patch(f"/api/v1/admin/users/{admin_id}",
                        json={"status": "disabled"}).status_code == 400
    assert client.delete(f"/api/v1/admin/users/{admin_id}").status_code == 400


# ======================================================================
# 五、密码重置与强制下线
# ======================================================================

def test_admin_resets_password_and_kills_sessions(client: TestClient) -> None:
    """管理员重置密码 → 旧密码失效、新密码可登、旧会话全被踢。"""
    admin_name = _make_user("admin")[1]
    uid, uname, upwd = _make_user("vip")
    _login(client, admin_name, ADMIN_PASSWORD)

    new_pwd = "ResetByAdmin#2026z"
    r = client.post(f"/api/v1/admin/users/{uid}/reset-password",
                    json={"new_password": new_pwd,
                          "must_change_password": False})
    assert r.status_code == 200, r.text[:250]
    assert get_auth_service()._repo.count_active_sessions_by_user().get(uid, 0) == 0

    client.post("/api/v1/auth/logout")
    assert client.post("/api/v1/auth/login",
                       json={"account": uname, "password": upwd,
                             "remember_me": False}).status_code == 401
    _login(client, uname, new_pwd)


def test_admin_kick_all_sessions(client: TestClient) -> None:
    admin_name = _make_user("admin")[1]
    uid = _make_user("vip")[0]
    _login(client, admin_name, ADMIN_PASSWORD)
    r = client.delete(f"/api/v1/admin/users/{uid}/sessions")
    assert r.status_code == 200
    assert "revoked_sessions" in r.json()


# ======================================================================
# 六、留痕（四眼原则：操作必须能归因到人）
# ======================================================================

def test_admin_actions_are_attributed(client: TestClient) -> None:
    """★ 每次审批/改等级都要在流水里留下**操作人**。

    匿名管理员在合规上等于没有管理员：出了事无法回答"是谁放行的"。
    """
    admin_id, admin_name, admin_pwd = _make_user("admin")
    uid = _make_user("trial", status="pending")[0]
    _login(client, admin_name, admin_pwd)
    client.post(f"/api/v1/admin/users/{uid}/approve",
                json={"tier": "vip", "days": 30, "note": "审批备注ABC"})

    reviews = client.get(f"/api/v1/admin/users/{uid}").json()["reviews"]
    assert reviews, "审批没有留下任何流水"
    approve = next(r for r in reviews if r["action"] == "approve")
    assert approve["reviewer_id"] == admin_id, "流水里没有记录操作人"
    assert approve["to_status"] == "active"
    assert "审批备注ABC" in (approve.get("note") or "")


def test_review_history_visible_in_list(client: TestClient) -> None:
    admin_name = _make_user("admin")[1]
    uid = _make_user("trial")[0]
    _login(client, admin_name, ADMIN_PASSWORD)
    client.patch(f"/api/v1/admin/users/{uid}", json={"tier": "vip"})
    rows = client.get("/api/v1/admin/reviews").json()["reviews"]
    assert any(r["user_id"] == uid and r["action"] == "admin:tier"
               for r in rows)


def test_user_detail_masks_contacts(client: TestClient) -> None:
    """管理台也**不返回明文邮箱** —— 管理员不需要看全量，脱敏足够定位。"""
    admin_name = _make_user("admin")[1]
    uid = _make_user("vip")[0]
    email = f"maskme_{secrets.token_hex(3)}@example.com"
    get_auth_service()._repo.bind_contact(
        user_id=uid, kind="email", value=email, verified=True)
    _login(client, admin_name, ADMIN_PASSWORD)

    r = client.get(f"/api/v1/admin/users/{uid}")
    assert r.status_code == 200
    assert email not in r.text, "管理台返回了明文邮箱"
    contacts = r.json()["contacts"]
    assert any(c["masked"].endswith("@example.com") for c in contacts)


def test_unknown_user_returns_404(client: TestClient) -> None:
    _login(client, _make_user("admin")[1], ADMIN_PASSWORD)
    assert client.get("/api/v1/admin/users/u_nope").status_code == 404
    assert client.patch("/api/v1/admin/users/u_nope",
                        json={"tier": "vip"}).status_code == 404
