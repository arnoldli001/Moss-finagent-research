"""登录门槛中间件的测试（`src/api/login_gate.py`）。

## 为什么这一层必须有测试，而且要端到端

2026-09-23 在 dev 实例上实测：**不带任何 Cookie** 直接打接口，

    /api/v1/intraday/watchlist          → 200（整份自选清单）
    /api/v1/intraday/snapshot?code=...  → 200（整套做T取数入口）
    /api/v1/scheduler/jobs              → 200
    /api/v1/openapi.json                → 200（完整接口面）
    /api/v1/auth/_debug/whoami          → 200（**库路径 + 用户名列表**）

也就是说"每个路由自己查会话"这条防线只做了一半。把实例开到公网上之前，
这一层必须由测试**逐条钉死**：哪些路径免登录、哪些必须 401、
登录之后是否放行、以及 dev 环境**不能**被它拦住（否则本地开发全废）。

只测 `is_public_path()` 这种纯函数是不够的 —— 它证明不了"中间件真的装上了、
且顺序正确"。真正会出问题的是接线，不是判断。
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from src.api.login_gate import DOC_PATHS, PUBLIC_EXACT, is_public_path
from src.api.routes.auth import get_auth_service, reset_auth_service
from src.domain.auth.human_check import reset_challenge_service
from src.domain.auth.ratelimit import reset_ip_rate_limiter

PASSWORD = "GatePassword#2026"


# ======================================================================
# 一、路径判断（纯函数）
# ======================================================================

@pytest.mark.parametrize("path", [
    "/",                     # ★ 单页应用的入口 HTML：拦掉它 = 要登录才能打开登录页
    "/index.html",
    "/favicon.ico",
    "/api/v1/health/live",   # 0 I/O 存活探针：登录页的连接状态条要用
    "/api/v1/auth/login",
    "/api/v1/auth/captcha",
    "/api/v1/auth/refresh",  # ★ 只带"记住我"Cookie，最容易漏掉
    "/api/v1/auth/login-mode",
    "/assets/index-abc.js",
])
def test_public_paths_do_not_need_login(path: str) -> None:
    assert is_public_path(path), f"{path} 应当免登录"


@pytest.mark.parametrize("path", [
    "/api/v1/intraday/watchlist",
    "/api/v1/intraday/snapshot",
    "/api/v1/scheduler/jobs",
    "/api/v1/metrics",
    "/api/v1/admin/users",
    "/api/v1/me/features",
    "/api/v1/health",        # 深度健康检查含内部拓扑 → 需登录
    "/api/v1/my/pools",
])
def test_protected_paths_need_login(path: str) -> None:
    assert not is_public_path(path), f"{path} 不该免登录"


def test_docs_paths_are_not_public() -> None:
    """文档路径既不在白名单里，也被单独列进 `DOC_PATHS` 做 404。"""
    for path in DOC_PATHS:
        assert not is_public_path(path), path


def test_public_list_is_explicit_not_wildcard() -> None:
    """白名单是**穷举**的：没有 `"/"` 这种前缀式放行会顺手放掉一切。"""
    assert "/" in PUBLIC_EXACT
    # 前缀白名单只该有这三个：认证族、前端静态资源
    from src.api.login_gate import PUBLIC_PREFIXES

    assert set(PUBLIC_PREFIXES) == {"/api/v1/auth/", "/assets/", "/static/"}


# ======================================================================
# 二、夹具：dev 实例 与 pilot（公网）实例
# ======================================================================

def _build_client(tmp_path, monkeypatch, *, env: str,
                  db_name: str = "gate.db") -> TestClient:
    monkeypatch.setenv("MOSS_ENV", env)
    db_path = str(tmp_path / db_name)
    monkeypatch.setenv("MOSS_SQLITE_PATH", db_path)
    if env == "pilot":
        # 公网环境的一组"自洽"配置：真邮件通道 + 承认单实例
        monkeypatch.setenv("ALERT_SMTP_USER", "gate@example.com")
        monkeypatch.setenv("ALERT_SMTP_AUTH_CODE", "unit-test-code")
        monkeypatch.setenv("MOSS_PILOT_SINGLE_INSTANCE_ACK", "1")
        monkeypatch.delenv("MOSS_TENANCY_ENFORCE", raising=False)
        monkeypatch.delenv("MOSS_NOTIFY_CHANNEL", raising=False)
    else:
        monkeypatch.setenv("MOSS_NOTIFY_CHANNEL", "console")

    from src.core.config import get_settings

    get_settings.cache_clear()
    reset_auth_service()
    reset_challenge_service()
    reset_ip_rate_limiter()

    from src.api.main import app

    # ★ `base_url` 必须是 **https**：公网环境下 `_cookie_kwargs` 会给 Cookie
    #   打 `Secure`，而 httpx 在 `http://` 下**不会存也不会发** Secure Cookie
    #   —— 于是登录明明成功、下一个请求却是 401，排查时极易误判成门槛写错。
    #   这和真实部署一致：pilot 一定跑在 Cloudflare Tunnel 的 HTTPS 之后。
    client = TestClient(app, base_url="https://testserver")
    service = get_auth_service()
    # 隔离哨兵（与 test_auth_routes 同口径）：单例指错库会让"登录 401"
    # 这类症状指向完全错误的方向。
    actual = str(getattr(service._repo, "_db_path", ""))
    assert actual == db_path, f"认证服务没指向隔离库：{actual} != {db_path}"
    service._repo.clear_all()
    return client


@pytest.fixture()
def dev_client(tmp_path, monkeypatch) -> TestClient:
    client = _build_client(tmp_path, monkeypatch, env="dev")
    yield client
    _teardown()


@pytest.fixture()
def pilot_client(tmp_path, monkeypatch) -> TestClient:
    """公网试点实例：`is_public` 为真 → 登录门槛生效。"""
    pilot_dir = tmp_path / "pilot"
    pilot_dir.mkdir(parents=True, exist_ok=True)
    client = _build_client(tmp_path, monkeypatch, env="pilot",
                           db_name="pilot/moss_pilot.db")
    yield client
    _teardown()


def _teardown() -> None:
    from src.core.config import get_settings

    reset_auth_service()
    reset_challenge_service()
    reset_ip_rate_limiter()
    get_settings.cache_clear()


def _make_user(*, status: str = "active", tier: str = "trial") -> tuple[str, str]:
    """直接建号（绕过注册审批），返回 (user_id, username)。"""
    repo = get_auth_service()._repo
    suffix = secrets.token_hex(4)
    username = f"gate_{suffix}"
    user_id = f"u_{suffix}"
    repo.create_user(user_id=user_id, username=username, display_name=username,
                     status=status, applied_tier=tier,
                     valid_until=(datetime.now().astimezone()
                                  + timedelta(days=30)).isoformat(
                                      timespec="seconds"))
    repo.set_password(user_id, PASSWORD)
    return user_id, username


def _login(client: TestClient, username: str) -> None:
    r = client.post("/api/v1/auth/login", json={
        "account": username, "password": PASSWORD, "remember_me": False})
    assert r.status_code == 200, f"登录失败：{r.status_code} {r.text[:200]}"


# ======================================================================
# 三、公网实例：未登录必须被挡
# ======================================================================

@pytest.mark.parametrize("path", [
    "/api/v1/intraday/watchlist?limit=5",
    "/api/v1/scheduler/jobs",
    "/api/v1/metrics?limit=1",
    "/api/v1/health",
])
def test_pilot_blocks_anonymous_api_access(
        pilot_client: TestClient, path: str) -> None:
    """★ 核心回归：这些路径在 dev 上**曾经全是 200**（实测）。

    它们没有自己的会话校验，全靠这一层兜住。任何一条回到 200，
    都意味着公网实例上有一块数据裸奔。
    """
    r = pilot_client.get(path)
    assert r.status_code == 401, f"{path} 未登录竟然 {r.status_code}"
    assert "登录" in r.text


def test_pilot_blocks_anonymous_pool_write(pilot_client: TestClient) -> None:
    """写操作同样在门槛内（别只测 GET）。

    ⚠️ 路径要挑一个**确实存在**的写接口：`/api/v1/me/pools` 已随自选池功能
    删除，用它测会变成"路由不存在"，虽然登录门槛仍在最前面返回 401，
    但那样这条用例就同时在验证两件不相干的事（门槛 + 路由是否存在），
    将来路由再变一次，失败信息会指向错误的方向。
    """
    r = pilot_client.put("/api/v1/me/profiles/600036",
                         json={"code": "600036", "weights": {}})
    assert r.status_code == 401, r.text[:200]


def test_pilot_keeps_login_and_probe_paths_open(
        pilot_client: TestClient) -> None:
    """登录族与存活探针必须仍然可用 —— 否则没人能登录进去。"""
    assert pilot_client.get("/api/v1/health/live").status_code == 200
    # 登录页开机就要问"要不要图形码"
    assert pilot_client.get("/api/v1/auth/login-mode").status_code == 200
    # 领图形码（没登录也得能给，否则登录页画不出来）
    assert pilot_client.get("/api/v1/auth/captcha").status_code == 200


def test_pilot_hides_api_docs(pilot_client: TestClient) -> None:
    """接口面不对公网暴露：文档与 schema 一律 404（不是 401）。"""
    for path in ("/openapi.json", "/docs", "/redoc"):
        r = pilot_client.get(path)
        assert r.status_code == 404, f"{path} 返回 {r.status_code}，应当 404"


def test_pilot_allows_authenticated_access(pilot_client: TestClient) -> None:
    """登录之后必须放行（否则门槛就成了"谁都进不来"的死锁）。

    ⚠️ 用 `/api/v1/me/features` 而不是 `/api/v1/intraday/watchlist`：
    后者要读 `app.state.runtime`，而它只在 lifespan 里装配 ——
    `TestClient` 不做 `with` 就不跑 lifespan，于是会拿到 500
    （`AttributeError: 'State' object has no attribute 'runtime'`），
    与"门槛有没有放行"完全无关，却会把断言搞成一片红。
    """
    _make_user()
    _, username = _make_user()
    _login(pilot_client, username)
    r = pilot_client.get("/api/v1/me/features")
    assert r.status_code not in (401, 403), f"登录后仍被拦：{r.status_code}"


def test_pilot_rejects_session_of_disabled_user(
        pilot_client: TestClient) -> None:
    """★ 管理员一停用，手上那张**还没到期**的票必须立刻失效。

    不能等 30 分钟滑动窗口 —— "停用"是应急处置（比如怀疑账号被盗），
    它生效的延迟必须是零。
    """
    user_id, username = _make_user()
    _login(pilot_client, username)
    assert pilot_client.get("/api/v1/me/features").status_code != 401

    repo = get_auth_service()._repo
    repo.update_user_status(user_id, "disabled", operator="admin",
                           note="测试停用", ip="127.0.0.1")

    r = pilot_client.get("/api/v1/me/features")
    assert r.status_code == 401, "停用后的会话还能用，门槛形同虚设"


def test_pilot_rejects_revoked_session(pilot_client: TestClient) -> None:
    """登出（撤销会话）之后同一张 Cookie 不能再进。"""
    _, username = _make_user()
    _login(pilot_client, username)
    assert pilot_client.get("/api/v1/me/features").status_code != 401
    pilot_client.post("/api/v1/auth/logout?all_devices=false")
    assert pilot_client.get("/api/v1/me/features").status_code == 401


def test_pilot_health_live_has_no_session_dependency(
        pilot_client: TestClient) -> None:
    """存活探针在**完全没有会话**时也要 200 —— 它是前端判断"后端在不在"的唯一依据。"""
    pilot_client.cookies.clear()
    r = pilot_client.get("/api/v1/health/live")
    assert r.status_code == 200
    assert r.json()["ok"] is True


# ======================================================================
# 四、dev 实例：门槛必须**不生效**（否则本地开发全废）
# ======================================================================

def test_dev_is_not_gated(dev_client: TestClient) -> None:
    """本地调试不该被登录拦住 —— dev 的安全性靠"只绑 127.0.0.1 + 独立库"。

    这一条同时也是"加门槛时别把本地开发搞死"的回归测试：
    门槛的判据是 `is_public`，dev/test 都不在内。

    ⚠️ 故意用 `/api/v1/me/features` 而不是 `/api/v1/scheduler/jobs`：
    后者要读 `app.state.run_log`，而它只在 lifespan 里装配 ——
    `TestClient` 不做 `with` 就不跑 lifespan。用它会得到一个与门槛无关的
    `AttributeError`，把这条测试的意图（"dev 不该被拦"）淹掉。
    """
    r = dev_client.get("/api/v1/me/features")
    # dev 下没有会话时，这个端点会自己返回 401（它自己查会话）——
    # 关键是**不能是登录门槛返回的那一句**。门槛的文案是"需要登录：请先登录…"。
    assert r.status_code in (200, 401), r.text[:200]
    assert "需要登录：请先登录" not in r.text, (
        "dev 环境不该被登录门槛拦截")


def test_docs_decision_follows_the_environment() -> None:
    """文档参数必须按环境走：公网不注册，dev/test 保留。

    为什么单测这个函数而不是打接口：`FastAPI(...)` 的文档路由是在**模块
    import 那一刻**按当时的环境注册的，之后再改环境也不会变 ——
    用"打接口"来测会变成"哪个环境先 import 了 main 就决定结果"的顺序耦合
    （实测：先用 pilot 建过一次 app，后面 dev 的用例就再也拿不到 200 了）。
    """
    from src.api.login_gate import docs_kwargs
    from src.core.config import Settings

    public = docs_kwargs(Settings(_env_file=None, env="pilot"))
    assert public == {"docs_url": None, "redoc_url": None, "openapi_url": None}

    local = docs_kwargs(Settings(_env_file=None, env="dev"))
    assert local["openapi_url"] == "/openapi.json"
    assert local["docs_url"] == "/docs"


# ======================================================================
# 五、配置层：公网环境的 Secure Cookie
# ======================================================================

def test_public_env_sets_secure_cookie_flag() -> None:
    """★ 公网实例的 Cookie 必须带 `Secure`。

    漏掉它 = 凭据在 HTTPS 隧道上仍会**明文回发** —— 隧道保护的就是这一段，
    Cookie 不带 Secure 等于把隧道的意义白做。而判据必须是 `is_public`
    （prod 或 pilot），不是 `is_prod`：试点实例同样在公网。
    """
    from src.core.config import Settings

    assert Settings(_env_file=None, env="pilot").is_public is True
    assert Settings(_env_file=None, env="prod").is_public is True
    assert Settings(_env_file=None, env="dev").is_public is False


# ======================================================================
# 六、WebSocket 也必须过门槛（实测：HTTP 门槛管不到 WS）
# ======================================================================

def test_pilot_blocks_anonymous_websocket(pilot_client: TestClient) -> None:
    """★★ WebSocket 不能被登录门槛漏掉。

    `LoginGateMiddleware` 是 `BaseHTTPMiddleware`，只包装 `http` scope ——
    **WS 连接根本不经过它**，而 `/ws/intraday`、`/ws/alerts` 自身也没有
    会话校验。公网实测（2026-09-23）：匿名连 `wss://<临时域名>/api/v1/ws/intraday`
    握手成功并收到首帧快照（中际旭创 922.50）—— 等于把量化信号流敞开。

    这条用例钉住"匿名 WS 必须被拒绝"，防止那次修复被后来的重构悄悄去掉。
    """
    import pytest
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as exc:
        with pilot_client.websocket_connect("/api/v1/ws/intraday?code=300308"):
            pass
    assert exc.value.code == 4401, f"关闭码应为 4401（需要登录），实际 {exc.value.code}"


def test_dev_websocket_not_gated(dev_client: TestClient) -> None:
    """dev 不拦（与 HTTP 门槛同一判据：`is_public`）—— 本地调试不该先登录。

    dev 上 runtime 可能没装配（TestClient 不跑 lifespan），所以这里只断言
    "不是被门槛以 4401 关掉"，而不是断言能收到数据。
    """
    from starlette.websockets import WebSocketDisconnect

    try:
        with dev_client.websocket_connect("/api/v1/ws/intraday?code=300308"):
            pass
    except WebSocketDisconnect as exc:
        assert exc.code != 4401, "dev 环境不该被登录门槛拦下"
    except AttributeError:
        # dev 的 TestClient 不跑 lifespan → `app.state.runtime` 不存在，
        # 端点会在装配检查处抛 AttributeError。这与"门槛"无关，放行即可；
        # 要断言的是**不是**被 4401（需要登录）关掉的。
        pass
