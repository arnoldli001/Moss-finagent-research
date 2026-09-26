"""认证 HTTP 端点的端到端测试（含 Cookie 行为与状态码映射）。

对应设计：`docs/PLATFORM_MULTI_TENANCY_DESIGN.md` §8.6.6（API 清单）、
§8.6.11（两层 Cookie）。

## 为什么必须有这一层测试（服务层测试不够）

服务层测不出"Cookie 放错了哪个令牌"这类问题 —— 而本轮**实测就踩到了**：
`moss_rt` 里误放 `refresh_token`（而非 `remember_token`），
表现为 `/auth/refresh` 一律 401、**"关掉浏览器再打开免密"直接失效**。
那种 bug 在服务层测试里完全看不出来，只有走 HTTP + Cookie 才暴露。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api.routes.auth import (
    COOKIE_CSRF,
    COOKIE_REFRESH,
    COOKIE_SESSION,
    REFRESH_COOKIE_PATH,
    get_auth_service,
    reset_auth_service,
)
from src.domain.auth.human_check import reset_challenge_service
from src.domain.auth.ratelimit import reset_ip_rate_limiter
from src.infrastructure.notify import MemoryNotifier

EMAIL = "route@example.com"
PASSWORD = "CorrectHorse#2026"
#: 明确命名"错的密码"，避免再出现"想测失败却传了正确密码"的歧义
WRONG_PASSWORD = "DefinitelyNotThePassword#9999"
USERNAME = "routeuser"


# ======================================================================
# 夹具：每个用例独立的库 + 内存通知通道
# ======================================================================

@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    """带隔离 SQLite 与内存通知通道的 TestClient。

    用 `monkeypatch.setenv` 而不是直接改 os.environ：pytest 会在用例结束
    自动还原，避免污染后续用例（本项目有其他测试也读 Settings）。

    ⚠️ **必须显式清库**：认证服务是进程内单例（`get_auth_service()`），
    不清的话上一个用例的用户/会话/验证码会留到下一个用例 ——
    实测表现为"第二个用例登录后看到 12 个会话""重发限流莫名不生效"，
    这类失败很难归因到"夹具没重置"。
    """
    monkeypatch.setenv("MOSS_ENV", "test")
    db_path = str(tmp_path / "route.db")
    monkeypatch.setenv("MOSS_SQLITE_PATH", db_path)
    monkeypatch.setenv("MOSS_NOTIFY_CHANNEL", "console")
    from src.core.config import get_settings

    get_settings.cache_clear()
    reset_auth_service()
    # ★ 图形码与 IP 限流都是**进程内单例**，必须逐用例重置。
    #   不重置的话：上一个用例的"失败计数"会留到下一个用例，
    #   于是某个用例莫名开始要求图形码 —— 表现为一片 400，极难归因。
    reset_challenge_service()
    reset_ip_rate_limiter()

    from src.api.main import app

    test_client = TestClient(app)
    service = get_auth_service()

    # ★ 隔离哨兵：装配出来的库**必须**是本用例的临时库。
    #
    # 为什么值得专门断言：单例一旦指向**真实库**（`data/moss_finagent.db`），
    # 症状完全不像"隔离失效" —— 会变成"登录 401""用户名已存在""接口莫名 429"，
    # 让人去怀疑 Cookie、限流、并发，而这些方向全错。
    # 全量跑时实测偶发过这一类失败（子集跑从不复现），根因极难定位；
    # 有了这条断言，同样的故障会直接指向真正的原因。
    actual = str(getattr(service._repo, "_db_path", ""))
    assert actual == db_path, (
        f"认证服务没有指向隔离库：期望 {db_path}，实际 {actual}。"
        f"这会读写真实库并污染其它用例。")

    service._repo.clear_all()                      # 显式重置，不依赖新建文件
    memory = MemoryNotifier()
    service._notifier = memory                     # 内存通道便于断言验证码
    yield test_client
    reset_auth_service()
    reset_challenge_service()
    reset_ip_rate_limiter()
    get_settings.cache_clear()


def _captcha(client: TestClient) -> dict:
    """领一个图形码，返回请求里要带的两个字段（令牌 + 答案）。

    ★ 图形码现在是**真校验**：只带令牌不带答案会被判 `captcha_failed`
    （以前"令牌非空即通过"，等于没有防护）。测试若沿用旧写法，
    会以一片 400 的形式失败 —— 那正是这次安全修复该有的表现。

    `debug_answer` 只在 dev/test 返回，本文件的环境是 `MOSS_ENV=test`，
    所以测试不必真的去"看图"。
    """
    body = client.get("/api/v1/auth/captcha").json()
    return {"captcha_token": str(body["captcha_token"]),
            "captcha_answer": str(body.get("debug_answer") or "")}


def _send_code(client: TestClient, email: str, scene: str = "register") -> str:
    response = client.post("/api/v1/auth/verify-code", json={
        "scene": scene, "email": email, **_captcha(client)})
    assert response.status_code == 200, response.text
    return get_auth_service()._notifier.last_code()  # type: ignore[attr-defined]


def _register(client: TestClient, email: str = EMAIL,
              username: str = USERNAME) -> str:
    code = _send_code(client, email)
    response = client.post("/api/v1/auth/register", json={
        "email": email, "username": username, "password": PASSWORD, "code": code})
    assert response.status_code == 200, response.text
    users = get_auth_service()._repo.list_users()
    return str(users[0].user_id)


def _activate(user_id: str) -> None:
    get_auth_service()._repo.update_user_status(user_id, "active",
                                                reviewed_by="admin1")


def _login(client: TestClient, *, password: str = PASSWORD,
           remember: bool = False, email: str = EMAIL):
    """登录辅助。

    ⚠️ **`password` 必须显式表达意图**：本文件曾出现"想测失败却传了正确密码"
    的 bug —— 辅助函数默认用正确密码，而失败用例直接调它就变成了"验证登录成功"，
    断言自然失败。所以成功用例传 `PASSWORD`、失败用例传 `WRONG_PASSWORD`，
    不再依赖默认值传达意图。
    """
    return client.post("/api/v1/auth/login", json={
        "account": email, "password": password, "remember_me": remember})


# ======================================================================
# 注册链路
# ======================================================================

def test_captcha_issues_token(client: TestClient) -> None:
    response = client.get("/api/v1/auth/captcha")
    assert response.status_code == 200
    assert response.json()["captcha_token"]


def test_verify_code_requires_captcha(client: TestClient) -> None:
    """图形码是**真校验**：不带答案直接发码必须被拒。

    ★ 这条守的是一个安全属性：以前"令牌非空即通过"，脚本带个假串
    就能把邮件额度打爆。现在服务端按**答案哈希**校验，
    不带答案一律 `captcha_failed`。
    """
    response = client.post("/api/v1/auth/verify-code",
                           json={"scene": "register", "email": EMAIL})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "captcha_failed"

    # 带令牌但**答案错**，同样必须被拒（这才是真校验与"只查非空"的区别）
    wrong = client.post("/api/v1/auth/verify-code", json={
        "scene": "register", "email": EMAIL,
        "captcha_token": client.get("/api/v1/auth/captcha").json()[
            "captcha_token"],
        "captcha_answer": "ZZZZ"})
    assert wrong.status_code == 400
    assert wrong.json()["detail"]["code"] == "captcha_failed"

    # 正确令牌 + 正确答案 → 放行
    ok = client.post("/api/v1/auth/verify-code", json={
        "scene": "register", "email": EMAIL, **_captcha(client)})
    assert ok.status_code == 200, ok.text


def test_captcha_token_is_single_use(client: TestClient) -> None:
    """★ 令牌一次性：用过之后同样的令牌不能再发码。

    否则抓到一个令牌就能无限发码（刷爆邮件额度、骚扰目标邮箱）。
    """
    one = _captcha(client)
    first = client.post("/api/v1/auth/verify-code", json={
        "scene": "register", "email": EMAIL, **one})
    assert first.status_code == 200, first.text
    replay = client.post("/api/v1/auth/verify-code", json={
        "scene": "register", "email": "another@example.com", **one})
    assert replay.status_code == 400
    assert replay.json()["detail"]["code"] == "captcha_failed"


def test_verify_code_then_register_pending(client: TestClient) -> None:
    code = _send_code(client, EMAIL)
    assert len(code) == 6 and code.isdigit()
    response = client.post("/api/v1/auth/register", json={
        "email": EMAIL, "username": USERNAME, "password": PASSWORD, "code": code})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] and body["status"] == "pending"


def test_pending_user_login_returns_403_with_status_code(
    client: TestClient,
) -> None:
    """待审批 → 403 + `status`：前端据此跳"等待审批"页而不是"密码错"。"""
    _register(client)
    response = _login(client)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "status"


# ======================================================================
# 登录 / Cookie / 三层令牌
# ======================================================================

def test_login_sets_three_cookies_and_no_token_in_body(
    client: TestClient,
) -> None:
    user_id = _register(client)
    _activate(user_id)
    response = _login(client, remember=True)
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] and body["user"]["username"] == USERNAME
    # ★ 令牌只走 HttpOnly Cookie，**响应体绝不回显**（否则会漏进前端日志/localStorage）
    assert "access_token" not in str(body) and "refresh_token" not in str(body)
    for name in (COOKIE_SESSION, COOKIE_REFRESH, COOKIE_CSRF):
        assert name in response.cookies, f"缺少 Cookie: {name}"


def test_login_without_remember_does_not_set_remember_cookie(
    client: TestClient,
) -> None:
    """没勾"记住我"就不该下发长窗口 Cookie。"""
    user_id = _register(client)
    _activate(user_id)
    response = _login(client, remember=False)
    assert response.status_code == 200
    assert COOKIE_REFRESH not in response.cookies


def test_session_cookie_is_single_entry_not_jwt(client: TestClient) -> None:
    """会话锚点必须是**不透明 ID**（不含 `.` 的两段 JWT 结构），
    否则无法即时吊销单个会话。"""
    user_id = _register(client)
    _activate(user_id)
    response = _login(client)
    session_cookie = response.cookies[COOKIE_SESSION]
    assert session_cookie and "." not in session_cookie


def test_remember_cookie_is_path_scoped(client: TestClient) -> None:
    """收窄 Path：日常请求不带长窗口令牌，暴露面小一个数量级。"""
    user_id = _register(client)
    _activate(user_id)
    response = _login(client, remember=True)
    header = " ".join(response.headers.get_list("set-cookie"))
    assert f"Path={REFRESH_COOKIE_PATH}" in header


def test_me_returns_masked_contacts_only(client: TestClient) -> None:
    user_id = _register(client)
    _activate(user_id)
    _login(client)
    body = client.get("/api/v1/auth/me").json()
    assert body["username"] == USERNAME
    assert EMAIL not in str(body), "接口不得返回明文邮箱"
    assert body["contacts"]["email"][0]["masked"].endswith("@example.com")


# ======================================================================
# 免密恢复（需求核心场景）+ 轮换与重放
# ======================================================================

def test_reopen_browser_without_password(client: TestClient) -> None:
    """★ 需求核心：**关掉浏览器再打开，不用再输密码**。

    模拟方式：清掉会话 Cookie（关浏览器的效果），只保留记住我 Cookie。
    """
    user_id = _register(client)
    _activate(user_id)
    assert _login(client, remember=True).status_code == 200
    saved = client.cookies.get(COOKIE_REFRESH)
    assert saved

    client.cookies.clear()                                   # ← "关掉浏览器"
    client.cookies.set(COOKIE_REFRESH, saved, path=REFRESH_COOKIE_PATH)
    response = client.post("/api/v1/auth/refresh")

    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert COOKIE_SESSION in response.cookies, "恢复后必须下发新的会话 Cookie"


def test_refresh_rotates_and_reads_from_remember_token(
    client: TestClient,
) -> None:
    """★ 回归：`moss_rt` 必须是 **remember token**。

    本轮实测踩过：误把 `refresh_token` 写进该 Cookie →
    `/auth/refresh` 一律 401 → 免密恢复彻底失效。
    """
    user_id = _register(client)
    _activate(user_id)
    _login(client, remember=True)
    assert client.post("/api/v1/auth/refresh").status_code == 200, (
        "refresh 失败通常意味着 moss_rt 里放的不是 remember token")


def test_replayed_remember_token_is_rejected_and_family_revoked(
    client: TestClient,
) -> None:
    """重放 = 令牌被复制走了 → 撤销整条链并强制重新登录。"""
    user_id = _register(client)
    _activate(user_id)
    _login(client, remember=True)
    old = client.cookies.get(COOKIE_REFRESH)
    # 第一次用（正常轮换）
    assert client.post("/api/v1/auth/refresh").status_code == 200
    # 再用同一个旧令牌 → 重放
    client.cookies.clear()
    client.cookies.set(COOKIE_REFRESH, old, path=REFRESH_COOKIE_PATH)
    replay = client.post("/api/v1/auth/refresh")
    assert replay.status_code == 401
    assert replay.json()["detail"]["code"] == "replay_detected"


def test_refresh_without_cookie_is_401(client: TestClient) -> None:
    assert client.post("/api/v1/auth/refresh").status_code == 401


# ======================================================================
# 开机探测 `/auth/bootstrap`：一次往返定登录态
#
# 为什么单独立一节：这是**公网首屏**唯一的认证请求。早先它是三次串行
# 往返（`/me` 401 → `/refresh` → `/me`），公网单次 0.4~2 秒，于是
# 用户看到"正在检查登录状态"停十秒（2026-09-26 报障）。
# ======================================================================

def test_bootstrap_reports_authenticated_with_live_session(
    client: TestClient,
) -> None:
    """有会话时：一次请求直接给出身份 + 图形码判定，且**不轮换令牌**。"""
    user_id = _register(client)
    _activate(user_id)
    _login(client, remember=True)
    rt_before = client.cookies.get(COOKIE_REFRESH)

    body = client.get("/api/v1/auth/bootstrap").json()

    assert body["authenticated"] is True
    assert body["renewed"] is False, "有有效会话时不该走续期"
    assert body["username"] == USERNAME
    assert body["session_id"], "必须带上当前会话 id"
    assert EMAIL not in str(body), "接口不得返回明文邮箱"
    assert body["login_mode"]["require_captcha"] is False
    assert client.cookies.get(COOKIE_REFRESH) == rt_before, (
        "有会话就轮换 remember token 会平白制造'重放'风险"
        "（响应一丢，整条令牌家族被撤销）")


def test_bootstrap_silently_resumes_without_session(client: TestClient) -> None:
    """★ 需求核心：只剩"记住我"Cookie 时，**一次** bootstrap 就免密进入。

    这一步等价于原来的 `/me`(401) → `/refresh` → `/me`(200) 三次往返。

    ⚠️ 为什么用 `TestClient(client.app)` 新建一个客户端来模拟"关掉浏览器"：
    `client.cookies.set()` 的默认 path 是 `/`，而 `moss_rt` 真实 path 是
    `/api/v1/auth` —— 于是测试里会**同时存在两张同名 cookie**，
    后续 `cookies.get("moss_rt")` 直接抛 `CookieConflict`。
    那会看起来像"实现没换 cookie"，实际只是测试自己把 jar 搞脏了。
    """
    user_id = _register(client)
    _activate(user_id)
    _login(client, remember=True)
    saved = client.cookies.get(COOKIE_REFRESH)
    assert saved

    fresh = TestClient(client.app)              # ← "关掉浏览器再打开"
    fresh.cookies.clear()
    fresh.cookies.set(COOKIE_REFRESH, saved, path=REFRESH_COOKIE_PATH)
    response = fresh.get("/api/v1/auth/bootstrap")
    body = response.json()

    assert body["authenticated"] is True
    assert body["renewed"] is True, "应报明本次是续期换来的会话"
    assert body["username"] == USERNAME
    header = " ".join(response.headers.get_list("set-cookie"))
    assert COOKIE_SESSION in header, "续期后必须下发新的会话 Cookie"
    # 续期轮换了令牌 → 下发的必须是**新的**那一张
    assert saved not in header


def test_bootstrap_without_any_cookie_is_not_authenticated(
    client: TestClient,
) -> None:
    """什么都没带 → 明确回 `authenticated:false`，而不是 401。

    这是前端敢用"不抛异常"写法的前提：未登录是**正常结论**。
    """
    response = client.get("/api/v1/auth/bootstrap")
    assert response.status_code == 200, "未登录不该是错误码"
    body = response.json()
    assert body["authenticated"] is False
    assert body["renewed"] is False
    assert body["login_mode"]["require_captcha"] is False


def test_bootstrap_replayed_token_is_not_authenticated_and_clears_cookies(
    client: TestClient,
) -> None:
    """重放令牌 → 不进入应用，并**清掉 Cookie**，避免前端拿着废令牌反复试。"""
    user_id = _register(client)
    _activate(user_id)
    _login(client, remember=True)
    old = client.cookies.get(COOKIE_REFRESH)
    assert old
    # 正常轮换一次，让 old 变成"用过的旧令牌"
    assert client.post("/api/v1/auth/refresh").status_code == 200

    replay = TestClient(client.app)
    replay.cookies.clear()
    replay.cookies.set(COOKIE_REFRESH, old, path=REFRESH_COOKIE_PATH)
    response = replay.get("/api/v1/auth/bootstrap")

    assert response.json()["authenticated"] is False
    cleared = " ".join(response.headers.get_list("set-cookie"))
    assert f"{COOKIE_REFRESH}=" in cleared, "废令牌必须被清掉（下发删除指令）"
    assert 'Max-Age=0' in cleared or 'max-age=0' in cleared


def test_bootstrap_refresh_false_never_touches_remember_token(
    client: TestClient,
) -> None:
    """`refresh=false` 时**只读**：即便有可续期的令牌也不换会话。

    这条守的是"幂等只读"语义 —— 客户端如果想在不产生写操作的前提下
    探一下登录态，必须真的没有写操作。
    """
    user_id = _register(client)
    _activate(user_id)
    _login(client, remember=True)
    saved = client.cookies.get(COOKIE_REFRESH)

    ro = TestClient(client.app)
    ro.cookies.clear()
    ro.cookies.set(COOKIE_REFRESH, saved, path=REFRESH_COOKIE_PATH)
    response = ro.get("/api/v1/auth/bootstrap?refresh=false")

    assert response.json()["authenticated"] is False
    header = " ".join(response.headers.get_list("set-cookie"))
    assert COOKIE_SESSION not in header, "只读模式不该下发任何新会话"
    assert COOKIE_REFRESH not in header, "只读模式不该动 remember token"


def test_bootstrap_disables_caching(client: TestClient) -> None:
    """登录态**绝不能被缓存**（中间层/浏览器缓存住会串号）。"""
    response = client.get("/api/v1/auth/bootstrap")
    assert "no-store" in response.headers.get("cache-control", "")


# ======================================================================
# 会话自助：看设备 / 踢设备 / 登出
# ======================================================================

def test_sessions_lists_current_device(client: TestClient) -> None:
    user_id = _register(client)
    _activate(user_id)
    _login(client)
    body = client.get("/api/v1/auth/sessions").json()
    assert len(body["sessions"]) == 1
    assert body["sessions"][0]["current"] is True


def test_logout_invalidates_session(client: TestClient) -> None:
    user_id = _register(client)
    _activate(user_id)
    _login(client)
    assert client.post("/api/v1/auth/logout").status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 401


def test_me_requires_login(client: TestClient) -> None:
    assert client.get("/api/v1/auth/me").status_code == 401


# ======================================================================
# 改密 / 找回
# ======================================================================

def test_change_password_and_other_session_revoked(
    client: TestClient,
) -> None:
    user_id = _register(client)
    _activate(user_id)
    _login(client)
    new_password = "BrandNewPass#2027"
    response = client.post("/api/v1/auth/password/change", json={
        "old_password": PASSWORD, "new_password": new_password})
    assert response.status_code == 200, response.text
    # 当前会话仍可用
    assert client.get("/api/v1/auth/me").status_code == 200
    # 旧密码失效、新密码可用
    client.cookies.clear()
    assert _login(client).status_code == 401
    ok = client.post("/api/v1/auth/login", json={
        "account": EMAIL, "password": new_password})
    assert ok.status_code == 200


def test_change_password_requires_login(client: TestClient) -> None:
    response = client.post("/api/v1/auth/password/change", json={
        "old_password": PASSWORD, "new_password": "BrandNewPass#2027"})
    assert response.status_code == 401


def test_forgot_does_not_leak_existence(client: TestClient) -> None:
    """★ 不存在的邮箱也必须"看起来成功"，否则找回接口成了枚举工具。"""
    user_id = _register(client)
    _activate(user_id)
    known = client.post("/api/v1/auth/password/forgot", json={
        "email": EMAIL, **_captcha(client)})
    unknown = client.post("/api/v1/auth/password/forgot", json={
        "email": "ghost@example.com", **_captcha(client)})
    assert known.status_code == unknown.status_code == 200
    assert known.json()["message"] == unknown.json()["message"]


def test_reset_password_end_to_end(client: TestClient) -> None:
    user_id = _register(client)
    _activate(user_id)
    _login(client)
    code = _send_code(client, EMAIL, scene="reset_password")
    token = get_auth_service()._repo.issue_reset(user_id=user_id, channel="email")
    response = client.post("/api/v1/auth/password/reset", json={
        "email": EMAIL, "code": code, "token": token,
        "new_password": "ResetPass#2029"})
    assert response.status_code == 200, response.text
    # 全部会话失效
    assert client.get("/api/v1/auth/me").status_code == 401
    fresh = client.post("/api/v1/auth/login", json={
        "account": EMAIL, "password": "ResetPass#2029"})
    assert fresh.status_code == 200


# ======================================================================
# 防枚举与状态码映射
# ======================================================================

def test_login_failure_same_status_and_code_for_unknown_account(
    client: TestClient,
) -> None:
    user_id = _register(client)
    _activate(user_id)
    wrong = _login(client, password=WRONG_PASSWORD)
    unknown = client.post("/api/v1/auth/login", json={
        "account": "ghost@example.com", "password": WRONG_PASSWORD})
    assert wrong.status_code == unknown.status_code == 401
    assert (wrong.json()["detail"]["code"]
            == unknown.json()["detail"]["code"] == "credentials")
    assert wrong.json()["detail"]["message"] == unknown.json()["detail"]["message"]


def test_rate_limit_maps_to_429(client: TestClient) -> None:
    """限流必须是 429（前端据此显示"稍后再试"），不是 400/500。"""
    _send_code(client, EMAIL)
    again = client.post("/api/v1/auth/verify-code", json={
        "scene": "register", "email": EMAIL, **_captcha(client)})
    assert again.status_code == 429
    assert again.json()["detail"]["code"] == "rate_limited"


def test_locked_maps_to_401(client: TestClient) -> None:
    """账号锁定要映射成 401（"凭据/会话问题"），而不是 500 或 400。

    ⚠️ **IP 限流会先于账号锁定生效**（实测踩到：连错几次后拿到的是
    400 `captcha_required`，而不是 `locked`）。这是**设计如此**且顺序合理：
    先拦住"来源异常"，再谈账号状态。

    所以本用例把 IP 失败阈值临时抬高，让它**专测账号维度**的锁定；
    "来源维度"的拦截由 `test_login_requires_captcha_after_repeated_failures`
    覆盖 —— 两个维度分开测，才说得清是哪一层在起作用。
    """
    from src.domain.auth.ratelimit import get_ip_rate_limiter

    limiter = get_ip_rate_limiter()
    limiter._max_failures = 10_000     # noqa: SLF001 关掉 IP 维度，专测账号维度
    try:
        user_id = _register(client)
        _activate(user_id)
        for _ in range(5):
            _login(client, password=WRONG_PASSWORD)
        locked = _login(client)
        assert locked.status_code in (401, 403), locked.text
        assert locked.json()["detail"]["code"] == "locked"
    finally:
        limiter._max_failures = 8      # noqa: SLF001 还原默认


def test_login_requires_captcha_after_repeated_failures(
        client: TestClient) -> None:
    """★★ 连续失败若干次后，登录必须**先过图形码**。

    这是防**密码喷洒**的关键：账号维度永远不触发锁定
    （每个账号只试 1 次），所以必须在**来源维度**上加一道门槛。
    """
    from src.domain.auth.ratelimit import get_ip_rate_limiter

    limiter = get_ip_rate_limiter()
    # 从干净状态开始：`_register` 内部可能已经有过失败登录，
    # 不清零的话"还差几次触发"就不可预测，测试会变得脆弱。
    limiter.reset()
    user_id = _register(client)
    _activate(user_id)
    limiter.reset()                    # 注册流程不计入本用例

    # 连错到超过阈值（默认 8）
    for _ in range(9):
        _login(client, password=WRONG_PASSWORD)

    # 此时不带图形码 → 被要求先过图形码（而不是继续验密）
    blocked = _login(client)
    assert blocked.status_code == 400, blocked.text
    assert blocked.json()["detail"]["code"] == "captcha_required"
    assert blocked.json()["detail"]["require_captcha"] is True

    # 带上正确图形码 → 回到正常认证流程。
    # 只断言"不再是 captcha_required"，**不**断言具体是 `locked` 还是
    # `credentials` —— 那取决于失败次数是否恰好越过锁定阈值，
    # 而本用例的命题是"图形码先于认证"，与账号锁定无关
    # （账号锁定由 `test_locked_maps_to_401` 专门覆盖）。
    with_captcha = client.post("/api/v1/auth/login", json={
        "account": EMAIL, "password": WRONG_PASSWORD, "remember_me": False,
        **_captcha(client)})
    code = with_captcha.json()["detail"]["code"]
    assert code != "captcha_required", with_captcha.text
    assert code in {"locked", "credentials"}, with_captcha.text

    stats = get_ip_rate_limiter().stats()
    assert stats["tracked_ips"] >= 1


def test_login_success_clears_failure_counter(
        client: TestClient) -> None:
    """★ 成功登录后**不再要求图形码**（否则本人被自己的手滑反复惩罚）。

    只清失败计数、保留请求计数：成功说明大概率是本人，但请求总量仍要计着，
    否则"拿一个能登的账号狂刷接口"就没人管了。
    """
    from src.domain.auth.ratelimit import get_ip_rate_limiter

    limiter = get_ip_rate_limiter()
    limiter.reset()
    user_id = _register(client)
    _activate(user_id)
    limiter.reset()

    # 失败若干次但**未达阈值** → 不要求图形码
    for _ in range(3):
        _login(client, password=WRONG_PASSWORD)
    assert client.get("/api/v1/auth/login-mode").json()[
        "require_captcha"] is False

    # 成功一次 → 失败计数清零
    assert _login(client, password=PASSWORD).status_code == 200
    for _ in range(3):
        _login(client, password=WRONG_PASSWORD)
    assert client.get("/api/v1/auth/login-mode").json()[
        "require_captcha"] is False, "成功登录没有清掉失败计数"


def test_login_mode_endpoint_reports_requirement(
        client: TestClient) -> None:
    """`/login-mode` 让前端**提前**知道要不要渲染图形码。

    没有它的话，前端只能"先试一次登录看 400"—— 那会让正常用户白填一次表单。
    """
    first = client.get("/api/v1/auth/login-mode").json()
    assert first["require_captcha"] is False, "第一次登录不该要求图形码"

    user_id = _register(client)
    _activate(user_id)
    for _ in range(9):
        _login(client, password=WRONG_PASSWORD)
    after = client.get("/api/v1/auth/login-mode").json()
    assert after["require_captcha"] is True


def test_ip_rate_limit_blocks_flood(client: TestClient) -> None:
    """★ 单 IP 请求总量超限 → 429 + `Retry-After`。

    这一层拦的是"高频"本身，不依赖密码对错 —— 脚本连发 100 次
    会被直接挡住，而不是白跑 100 次昂贵的密码哈希。
    """
    from src.domain.auth.ratelimit import get_ip_rate_limiter

    limiter = get_ip_rate_limiter()
    limiter._max_requests = 5        # noqa: SLF001 收紧阈值便于测试
    try:
        last = None
        for _ in range(8):
            last = _login(client, password=WRONG_PASSWORD)
        assert last is not None and last.status_code == 429, last.text
        assert last.json()["detail"]["code"] == "rate_limited"
        assert last.headers.get("Retry-After")
    finally:
        limiter._max_requests = 60   # noqa: SLF001 还原
