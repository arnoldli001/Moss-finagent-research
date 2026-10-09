"""做T自选**按账号归属**的存储底座（`src/intraday/user_watch.py`，`CHG-0227`）。

## 这一层为什么单独测

它是"自选从一份共享 YAML 改成每人一份"的**底座**：按 `user_id` 定位的仓储子 API
＋ owner 作用域 ＋ 额度校验 ＋ 存量迁移。**接线（REST/WS/作业）还没做** ——
所以这一层必须自己有判据，否则"底座写好了"只是一句话。

## 钉住的四件事

1. **归属**：两个账号各自一份，互不可见、互不可改；
2. **稳定性**：`tenant_id` 装的是套餐等级 ⇒ 读/写**都不带它**做过滤
   （升级/降级不该让自选"消失"，也不该分叉出第二行）；
3. **空身份**：写路径**抛错**，绝不落到公共清单（这是本轮要消灭的形态）；
4. **迁移**：已有按账号自选就不迁移；没有管理员就一行都不迁（与板块同一政策）。
"""

from __future__ import annotations

import secrets
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.api.routes.auth import get_auth_service, reset_auth_service  # noqa: E402
from src.domain.auth.human_check import reset_challenge_service  # noqa: E402
from src.domain.auth.ratelimit import reset_ip_rate_limiter  # noqa: E402
from src.domain.quota.service import reset_quota_service  # noqa: E402
from src.infrastructure.repositories.user_pool_sqlite_repo import (  # noqa: E402
    PoolQuota,
    PoolValidationError,
    reset_profile_repo,
)

ALICE = "u_alice"
BOB = "u_bob"


@pytest.fixture()
def watch_env(tmp_path, monkeypatch):
    """每个用例一个独立的应用库（`get_profile_repo()` 按 `settings.sqlite_path` 取库）。"""
    monkeypatch.setenv("MOSS_ENV", "test")
    db_path = str(tmp_path / "watch.db")
    monkeypatch.setenv("MOSS_SQLITE_PATH", db_path)
    monkeypatch.delenv("MOSS_LEGACY_WATCH_OWNER", raising=False)
    from src.core.config import get_settings

    get_settings.cache_clear()
    reset_profile_repo()
    reset_quota_service()
    yield db_path
    reset_profile_repo()
    reset_quota_service()
    get_settings.cache_clear()


def _watch():
    from src.intraday import user_watch

    return user_watch


def _seed_user(db_path: str, user_id: str, tier: str = "admin",
               username: str = "", created: str = "2026-09-23T15:44:16+00:00") -> None:
    """手搓一张 `dim_user`（迁移的归属候选要用它）。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS dim_user ("
            " user_id TEXT PRIMARY KEY, username TEXT NOT NULL,"
            " status TEXT NOT NULL DEFAULT 'active',"
            " applied_tier TEXT NOT NULL DEFAULT 'trial',"
            " created_at TEXT NOT NULL)")
        conn.execute(
            "INSERT OR REPLACE INTO dim_user(user_id, username, status,"
            " applied_tier, created_at) VALUES (?,?,'active',?,?)",
            (user_id, username or user_id, tier, created))
        conn.commit()
    finally:
        conn.close()


# ======================================================================
# 一、归属与稳定性
# ======================================================================

def test_two_accounts_are_isolated(watch_env) -> None:
    uw = _watch()
    uw.add_item(ALICE, "vip", code="300308", name="中际旭创")
    uw.add_item(BOB, "vip", code="600150", name="中国船舶")

    assert [r.code for r in uw.list_items(ALICE)] == ["300308"]
    assert [r.code for r in uw.list_items(BOB)] == ["600150"]


def test_new_item_goes_to_the_top(watch_env) -> None:
    """用户口径 2026-09-23：新加的自选放**顶部**（YAML 时代的 `prepend=True`）。"""
    uw = _watch()
    uw.add_item(ALICE, "vip", code="600150")
    uw.add_item(ALICE, "vip", code="300308")
    assert [r.code for r in uw.list_items(ALICE)] == ["300308", "600150"]


def test_pinned_always_first(watch_env) -> None:
    uw = _watch()
    uw.add_item(ALICE, "vip", code="600150")          # 后加的（本来在顶）
    uw.add_item(ALICE, "vip", code="300308")
    assert uw.pin_item(ALICE, "600150", True) is True
    assert [r.code for r in uw.list_items(ALICE)] == ["600150", "300308"]


def test_tier_change_does_not_hide_or_duplicate(watch_env) -> None:
    """★ `tenant_id` 只记来源：用户升/降套餐后，自选**仍在**、且**只有一行**。

    这一条正是既有 `(tenant_id, user_id)` 双条件写法会踩的坑
    （`dim_user_pool` 实测 `tenant='trial'` ⇒ 升级后查不到自己的池）。
    """
    uw = _watch()
    uw.add_item(ALICE, "trial", code="300308", name="中际旭创")
    # 套餐从 trial 升到 vip 后再加同一只（模拟"升级后重新点一次加自选"）
    uw.add_item(ALICE, "vip", code="300308", name="中际旭创")
    rows = uw.list_items(ALICE)
    assert [r.code for r in rows] == ["300308"], f"按新档位分叉出了第二行：{rows}"
    assert rows[0].tenant_id == "trial", "更新应保留最早那行的来源登记"
    # 升级后仍然读得到（不带 tenant 过滤）
    assert len(uw.list_items(ALICE)) == 1


def test_cannot_touch_others_items(watch_env) -> None:
    uw = _watch()
    uw.add_item(ALICE, "vip", code="300308")
    assert uw.remove_item(BOB, "300308") is False
    assert uw.pin_item(BOB, "300308", True) is False
    assert [r.code for r in uw.list_items(ALICE)] == ["300308"]


def test_remove_is_idempotent(watch_env) -> None:
    uw = _watch()
    uw.add_item(ALICE, "vip", code="300308")
    assert uw.remove_item(ALICE, "300308") is True
    assert uw.remove_item(ALICE, "300308") is False     # 重复删不抛
    assert uw.list_items(ALICE) == []


def test_default_pool_id_carries_user_id(watch_env) -> None:
    """`dim_user_pool` 主键是 `pool_id` 一列（全局唯一）⇒ 池 id 必须带账号。"""
    uw = _watch()
    uw.add_item(ALICE, "vip", code="300308")
    uw.add_item(BOB, "vip", code="600150")           # 第二个账号建池不该撞主键
    assert uw.pool_id_for(ALICE) != uw.pool_id_for(BOB)
    assert uw.list_items(ALICE) and uw.list_items(BOB)


# ======================================================================
# 二、空身份：写路径必须报错
# ======================================================================

def test_write_without_owner_raises(watch_env) -> None:
    """★ 绝不"拿不到身份就写公共清单"（那正是本轮要消灭的形态）。"""
    uw = _watch()
    for call in (
        lambda: uw.add_item("", "vip", code="300308"),
        lambda: uw.remove_item("", "300308"),
        lambda: uw.pin_item("", "300308", True),
        lambda: uw.list_items(""),
        lambda: uw.ensure_default_pool(""),
    ):
        with pytest.raises(uw.WatchOwnerRequired):
            call()


def test_owner_scope_roundtrip() -> None:
    uw = _watch()
    assert uw.current_owner() == uw.SYSTEM and uw.has_owner() is False
    with uw.owner_scope(ALICE, "vip"):
        assert uw.current_owner() == (ALICE, "vip") and uw.has_owner() is True
        assert uw.owner_key() == ALICE
    assert uw.current_owner() == uw.SYSTEM, "owner 作用域没有复原"


# ======================================================================
# 三、额度
# ======================================================================

def test_watchlist_total_limit(watch_env) -> None:
    uw = _watch()
    quota = PoolQuota(pool_limit=5, pool_size_limit=2, watchlist_limit=2)
    uw.add_item(ALICE, "trial", code="600150", quota=quota)
    uw.add_item(ALICE, "trial", code="300308", quota=quota)
    with pytest.raises(PoolValidationError) as exc:
        uw.add_item(ALICE, "trial", code="000001", quota=quota)
    assert exc.value.code == "watchlist_full"
    assert "2" in exc.value.message, exc.value.message


def test_adding_existing_code_is_not_blocked_by_limit(watch_env) -> None:
    """★ 到顶后**更新已有那只**（比如补板块）必须照常成功 —— 否则用户以为坏了。"""
    uw = _watch()
    quota = PoolQuota(watchlist_limit=1, pool_size_limit=1)
    uw.add_item(ALICE, "trial", code="600150", quota=quota)
    again = uw.add_item(ALICE, "trial", code="600150", name="中国船舶",
                        boards=["船舶"], quota=quota)
    assert again.boards == ("船舶",)
    assert [r.code for r in uw.list_items(ALICE)] == ["600150"]


def test_illegal_code_rejected_even_without_quota(watch_env) -> None:
    """代码合法性校验**不随 quota 传不传而变**（北交所/全角一律拒）。"""
    uw = _watch()
    with pytest.raises(PoolValidationError):
        uw.add_item(ALICE, "vip", code="830799")       # 北交所：取数层不覆盖


# ======================================================================
# 四、SYSTEM（作业）视角与迁移
# ======================================================================

def test_system_codes_is_the_union(watch_env) -> None:
    uw = _watch()
    uw.add_item(ALICE, "vip", code="300308")
    uw.add_item(BOB, "vip", code="600150")
    uw.add_item(BOB, "vip", code="300308")            # 与 A 重合
    assert uw.system_codes() == ["300308", "600150"]


def test_system_falls_back_to_legacy_yaml(watch_env) -> None:
    """并集为空 ⇒ 回落 YAML（老机器兼容路径），且**只有 SYSTEM 有这条回落**。"""
    from src.intraday.config import WatchConfig

    uw = _watch()

    class _Cfg:
        watchlist = [WatchConfig(code="600036", name="招商银行")]

    codes, source = uw.all_codes_for_system(_Cfg())
    assert (codes, source) == (["600036"], "legacy-yaml")
    assert uw.list_items(ALICE) == [], "登录用户不该继承共享清单"


def test_migration_assigns_to_earliest_admin(watch_env) -> None:
    from src.intraday.config import WatchConfig

    _seed_user(watch_env, "u_admin_old", tier="admin")
    _seed_user(watch_env, "u_vip", tier="vip", created="2026-09-24T04:09:46+00:00")
    uw = _watch()

    class _Cfg:
        watchlist = [WatchConfig(code="600036", name="招商银行",
                                 boards=["银行"], pinned=True)]

    report = uw.migrate_legacy_watchlist(_Cfg())
    assert report["migrated"] is True and report["rows"] == 1
    assert "u_admin_old" in report["owner"]
    rows = uw.list_items("u_admin_old")
    assert [r.code for r in rows] == ["600036"] and rows[0].pinned is True
    assert uw.list_items("u_vip") == []


def test_migration_skips_when_owner_data_exists(watch_env) -> None:
    from src.intraday.config import WatchConfig

    _seed_user(watch_env, "u_admin_old", tier="admin")
    uw = _watch()
    uw.add_item(BOB, "vip", code="600150")            # 已经有人在用按账号自选

    class _Cfg:
        watchlist = [WatchConfig(code="600036", name="招商银行")]

    report = uw.migrate_legacy_watchlist(_Cfg())
    assert report["migrated"] is False and "跳过" in report["reason"]
    assert uw.list_items("u_admin_old") == []


def test_migration_without_admin_migrates_nothing(watch_env) -> None:
    """★ 没有管理员 ⇒ 一行都不迁（与板块同一政策，`needs_owner=True`）。"""
    from src.intraday.config import WatchConfig

    uw = _watch()

    class _Cfg:
        watchlist = [WatchConfig(code="600036", name="招商银行")]

    report = uw.migrate_legacy_watchlist(_Cfg())
    assert report["migrated"] is False and report["needs_owner"] is True
    assert report["rows"] == 0 and uw.system_codes() == []


def test_migration_owner_env_override(watch_env, monkeypatch) -> None:
    from src.intraday.config import WatchConfig

    _seed_user(watch_env, "u_admin_old", tier="admin")
    _seed_user(watch_env, "u_real", tier="vip", username="vip_legacy_account")
    monkeypatch.setenv("MOSS_LEGACY_WATCH_OWNER", "vip_legacy_account")
    uw = _watch()

    class _Cfg:
        watchlist = [WatchConfig(code="600036", name="招商银行")]

    report = uw.migrate_legacy_watchlist(_Cfg())
    assert "u_real" in report["owner"], report
    assert [r.code for r in uw.list_items("u_real")] == ["600036"]


def test_migration_dry_run_changes_nothing(watch_env) -> None:
    from src.intraday.config import WatchConfig

    _seed_user(watch_env, "u_admin_old", tier="admin")
    uw = _watch()

    class _Cfg:
        watchlist = [WatchConfig(code="600036", name="招商银行")]

    report = uw.migrate_legacy_watchlist(_Cfg(), dry_run=True)
    assert report["migrated"] is False and "[dry-run]" in report["reason"]
    assert uw.system_codes() == []


def test_module_is_wired_into_the_service() -> None:
    """★ 接线判据（`CHG-0224`）：底座**已被服务层真的用起来**。

    上一轮这条是反向断言（`callers == []`，逼接线的人来改）。本轮接完线，
    它就翻成正向：读路径（`current_owner`）与写路径（`owner_scope`）都必须出现，
    而且**作业那一侧**（SYSTEM）必须仍然可用 —— 见下面几条服务层用例。
    """
    root = Path(__file__).resolve().parents[2]
    service_text = (root / "src" / "intraday" / "service.py").read_text(
        encoding="utf-8")
    routes_text = (root / "src" / "api" / "routes" / "intraday.py").read_text(
        encoding="utf-8")
    assert "user_watch.current_owner()" in service_text, "读路径没接 owner"
    # 写路径是"有 owner 就写那个账号、没有才走老 YAML 路径"的**分流**，
    # 而"设作用域"是路由 / WS 的职责（只有它们知道会话是谁）。
    assert "if owner_id:" in service_text, "写路径没有按 owner 分流"
    assert "_add_watch_for_owner" in service_text, "按账号的写路径不存在"
    assert "user_watch.owner_scope(" in routes_text, "路由层没设 owner 作用域"
    assert "ws_identity(websocket)" in routes_text, "WS 没按连接解析身份"


# ======================================================================
# 五、服务层：按账号隔离真的生效（`CHG-0224` 接线）
# ======================================================================

_ROOT = Path(__file__).resolve().parents[2]


def _service(tmp_path):
    """最小可用的做T服务：**真实的** `configs/intraday.yaml` 拷贝到临时目录。

    为什么用真配置：那份 YAML 里躺着 58 只共享自选 —— 正好当"反向判据"：
    没有 DB 行的账号**一只都不该继承**（隔离的核心命题）。
    """
    from src.intraday.service import IntradayService

    path = tmp_path / "intraday.yaml"
    path.write_text((_ROOT / "configs" / "intraday.yaml").read_text(
        encoding="utf-8"), encoding="utf-8")
    return IntradayService(backend=None, config_path=str(path))


def test_owner_does_not_inherit_the_shared_yaml(watch_env, tmp_path) -> None:
    """★★ 隔离的核心：YAML 里那 58 只**不会**出现在某个账号的清单里。"""
    import asyncio

    uw = _watch()
    service = _service(tmp_path)
    assert len(uw.legacy_yaml_codes(service.config)) > 10, "夹具前提不成立"
    with uw.owner_scope(ALICE, "vip"):
        items = asyncio.run(service.watchlist())
    assert items == [], f"账号继承了共享清单：{[i.code for i in items][:5]}"


def test_owner_sees_only_own_rows_with_names_and_boards(watch_env, tmp_path) -> None:
    import asyncio

    uw = _watch()
    uw.add_item(ALICE, "vip", code="300308", name="中际旭创", boards=["通信设备"])
    uw.add_item(BOB, "vip", code="600150", name="中国船舶")
    service = _service(tmp_path)

    with uw.owner_scope(ALICE, "vip"):
        mine = asyncio.run(service.watchlist())
    assert [item.code for item in mine] == ["300308"]
    assert mine[0].name == "中际旭创"
    assert mine[0].boards == ["通信设备"]

    with uw.owner_scope(BOB, "vip"):
        theirs = asyncio.run(service.watchlist())
    assert [item.code for item in theirs] == ["600150"]
    assert theirs[0].boards == []


def test_watch_config_is_per_owner(watch_env, tmp_path) -> None:
    """同一只票，两个账号的关联板块可以不同（隔离落到最细的一处）。"""
    uw = _watch()
    uw.add_item(ALICE, "vip", code="300308", name="中际旭创", boards=["通信设备"])
    service = _service(tmp_path)
    with uw.owner_scope(ALICE, "vip"):
        assert list(service.watch_config("300308").boards) == ["通信设备"]
    with uw.owner_scope(BOB, "vip"):
        assert list(service.watch_config("300308").boards) != ["通信设备"]


def test_system_view_is_the_union(watch_env, tmp_path) -> None:
    """作业侧（无身份）看到的是**并集**，且不是某个人的清单。"""
    uw = _watch()
    uw.add_item(ALICE, "vip", code="300308")
    uw.add_item(BOB, "vip", code="600150")
    service = _service(tmp_path)
    assert service.all_watch_codes() == ["300308", "600150"]
    assert uw.current_owner() == uw.SYSTEM


# ======================================================================
# 六、接口层：两个真实账号（真登录、真路由、真库）
# ======================================================================

PASSWORD = "WatchUser#2026x"


@pytest.fixture()
def api_client(watch_env, tmp_path, monkeypatch):
    """真路由 + 真会话 Cookie + 真库的最小运行时（只装配做T服务）。"""
    from fastapi.testclient import TestClient

    reset_auth_service()
    reset_challenge_service()
    reset_ip_rate_limiter()
    from src.api.main import app

    app.state.runtime = SimpleNamespace(intraday=_service(tmp_path),
                                        intraday_profile_repo=None)
    client = TestClient(app)
    get_auth_service()._repo.clear_all()
    _ACCOUNTS.clear()
    yield client
    reset_auth_service()
    reset_challenge_service()
    reset_ip_rate_limiter()


_ACCOUNTS: dict[str, str] = {}


def _make_and_login(client, name: str) -> str:
    from datetime import datetime, timedelta

    repo = get_auth_service()._repo
    username = _ACCOUNTS.get(name)
    user_id = ""
    if username is None:
        suffix = secrets.token_hex(4)
        username = f"{name}_{suffix}"
        user_id = f"u_{suffix}"
        repo.create_user(user_id=user_id, username=username, display_name=name,
                         status="active", applied_tier="vip",
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


def test_http_watchlist_requires_login(api_client) -> None:
    assert api_client.get("/api/v1/intraday/watchlist").status_code == 401
    assert api_client.post("/api/v1/intraday/watchlist",
                           json={"code": "300308"}).status_code == 401


def test_http_watchlist_is_per_account(api_client) -> None:
    """★★ A 加的自选，B **看不到**；B 删同一只票也删不掉 A 的。"""
    _make_and_login(api_client, "alice")
    created = api_client.post("/api/v1/intraday/watchlist", json={
        "code": "300308", "name": "中际旭创", "fetch_name": False})
    assert created.status_code == 200, created.text[:300]
    assert [row["code"] for row in created.json()["watchlist"]] == ["300308"]

    mine = api_client.get("/api/v1/intraday/watchlist").json()
    assert [item["code"] for item in mine["items"]] == ["300308"]

    api_client.post("/api/v1/auth/logout")
    _make_and_login(api_client, "bob")
    theirs = api_client.get("/api/v1/intraday/watchlist").json()
    assert theirs["count"] == 0, f"B 看到了 A 的自选：{theirs}"
    # B 删同一只票：自选按 (user, code) 定位 ⇒ 删的是**他自己**的（幂等 no-op）
    assert api_client.delete(
        "/api/v1/intraday/watchlist/300308").status_code == 200

    api_client.post("/api/v1/auth/logout")
    _make_and_login(api_client, "alice")
    assert [item["code"] for item in
            api_client.get("/api/v1/intraday/watchlist").json()["items"]] \
        == ["300308"], "B 的越权删除动了 A 的自选"


def test_http_pin_is_per_account(api_client) -> None:
    _make_and_login(api_client, "alice")
    api_client.post("/api/v1/intraday/watchlist", json={
        "code": "300308", "name": "中际旭创", "fetch_name": False})
    pinned = api_client.post(
        "/api/v1/intraday/watchlist/pin?code=300308&pinned=true")
    assert pinned.status_code == 200, pinned.text[:200]
    assert pinned.json()["watchlist"][0]["pinned"] is True

    api_client.post("/api/v1/auth/logout")
    _make_and_login(api_client, "bob")
    # B 没有这只票 ⇒ 置顶应 404（"我这儿没有它"），而不是悄悄改 A 的
    assert api_client.post(
        "/api/v1/intraday/watchlist/pin?code=300308&pinned=true"
    ).status_code == 404


def test_http_config_watchlist_is_own_only(api_client) -> None:
    """`GET /intraday/config` 回显的 watchlist 也必须是**自己那份**。

    旧实现回显 `config.watchlist`（共享 YAML 的 58 只）—— 那正是用户
    "看到所有用户的自选股"的入口之一。
    """
    _make_and_login(api_client, "alice")
    api_client.post("/api/v1/intraday/watchlist", json={
        "code": "600150", "name": "中国船舶", "fetch_name": False})
    payload = api_client.get("/api/v1/intraday/config").json()
    assert [row["code"] for row in payload["watchlist"]] == ["600150"]


