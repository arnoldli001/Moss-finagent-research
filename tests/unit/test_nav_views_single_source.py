"""一级目录页签清单**只有一个来源**：把三处下发口钉成逐字一致。

## 为什么值得单独一个文件（2026-09-28 用户报障）

> "首次登录进去，一级目录只显示投资日历、事件告警，而投研分析、策略回测、
>   量化交易、主线挖掘、资金流监控、热点&研报小作文都要等 3-5 秒才出来"

根因：页签清单原来**只有 `GET /me/features` 一个来源**，而那个请求必须等认证
结果就位之后才发得出去 —— 也就是「认证往返 → 权限往返」**两次串行**。
在这条公网链路上每次冷请求实测约 0.9 秒，页签就只能一条一条往外冒。

修法是把同一份清单并进 `/auth/login` 与 `/auth/bootstrap`
（见 `auth.py::_features_payload`），往返从 2 次降到 1 次；
前端再按用户缓存上次结果（`web/src/featuresCache.ts`），刷新时 0 次往返。

## ⚠️ 这个修法唯一的风险，就是本文件要挡的东西

一旦 `/me/features` 与 auth 响应**各算各的**，症状是"刚登录时少一个页签、
刷新一下又有了" —— 只在时序里出现，日志里什么都看不到，是最难查的一类 bug。

所以下面：

1. 纯函数层：管理员专属页签（`scheduler` / `metrics`）的边界、顺序、fail-closed；
2. HTTP 层：**三处响应里的 `visible_views` 必须完全相同**（逐字比对）。
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from src.api.routes.auth import get_auth_service, reset_auth_service
from src.api.routes.my_features import (
    ADMIN_ONLY_VIEWS,
    VIEW_FEATURE,
    build_visible_views,
    is_admin_tier,
    visible_views_for_tier,
)
from src.domain.platform.config import reset_platform_config
from src.domain.quota.service import reset_quota_service

PASSWORD = "NavViews#2026x"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """独立库 + 独立套餐配置的 TestClient（不跑 lifespan，因此不建真实图）。"""
    db_path = str(tmp_path / "nav.db")
    monkeypatch.setenv("MOSS_ENV", "test")
    monkeypatch.setenv("MOSS_SQLITE_PATH", db_path)
    monkeypatch.setenv("MOSS_NOTIFY_CHANNEL", "console")
    monkeypatch.setenv("MOSS_TIER_CONFIG", str(tmp_path / "tiers.json"))

    from src.core.config import get_settings

    get_settings.cache_clear()
    reset_auth_service()
    reset_quota_service()
    reset_platform_config()

    from src.api.main import app

    c = TestClient(app)
    assert str(getattr(get_auth_service()._repo, "_db_path", "")) == db_path
    get_auth_service()._repo.clear_all()
    yield c
    reset_auth_service()
    reset_quota_service()
    reset_platform_config()
    get_settings.cache_clear()


def login_as(client: TestClient, tier: str) -> tuple[str, dict]:
    """建一个该等级的用户并登录，返回 `(用户名, 登录响应体)`。"""
    repo = get_auth_service()._repo
    suffix = secrets.token_hex(4)
    username = f"nav_{tier}_{suffix}"
    repo.create_user(user_id=f"u_{suffix}", username=username,
                     display_name=username, status="active",
                     applied_tier=tier,
                     valid_until=(datetime.now().astimezone()
                                  + timedelta(days=30)).isoformat(
                                      timespec="seconds"))
    repo.set_password(f"u_{suffix}", PASSWORD)
    r = client.post("/api/v1/auth/login",
                    json={"account": username, "password": PASSWORD,
                          "remember_me": False})
    assert r.status_code == 200, r.text[:200]
    return username, r.json()


# ======================================================================
# 一、纯函数层
# ======================================================================

def test_admin_tier_is_case_and_space_insensitive() -> None:
    """管理员判据必须容错：库里的值可能带空格、大小写不统一。"""
    assert is_admin_tier("admin") is True
    assert is_admin_tier("  ADMIN  ") is True
    assert is_admin_tier("vip") is False
    assert is_admin_tier("") is False


@pytest.mark.parametrize("tier", ["vip", "trial", "", "no-such-tier"])
def test_admin_only_views_never_leak_to_non_admin(tier: str) -> None:
    """管理员专属页签（调度管理/运行指标）**永远**不下发给其他等级。"""
    views, admin = visible_views_for_tier(tier)
    assert admin is False
    for view in ADMIN_ONLY_VIEWS:
        assert view not in views, f"{tier} 拿到了管理员专属页签 {view}"


def test_admin_gets_admin_only_views_even_though_not_sellable() -> None:
    """`scheduler` / `metrics` **不在** `FEATURES`（可售卖项）里，但仍须给管理员。

    这是把它们移出售卖项时最容易漏改的一处：若走 `enabled.get(feature)` 判据，
    一查就是 False，表现是"管理员自己少了两个页签"。
    """
    views, admin = visible_views_for_tier("admin")
    assert admin is True
    for view in ADMIN_ONLY_VIEWS:
        assert view in views


def test_unknown_tier_falls_back_to_default_plan_but_stays_non_admin() -> None:
    """未知等级走套餐配置的**默认档**（不是空清单），但绝不因此变成管理员。

    ⚠️ 这条断言是**实测纠正**过来的（2026-09-28）：原先我写的是
    `views == []`，跑出来却是 5 个页签 —— 因为
    `get_platform_config().plan("no-such-tier")` **不抛异常**，
    而是回落到默认档。"未知等级 = 什么都没有"是个错误假设。

    真正 fail-closed 的是"**配置读不到**"那条分支，见下一个用例。
    """
    views, admin = visible_views_for_tier("no-such-tier")
    assert admin is False
    for view in ADMIN_ONLY_VIEWS:
        assert view not in views, f"未知等级拿到了管理员专属页签 {view}"


def test_config_read_failure_is_fail_closed(monkeypatch) -> None:
    """配置读不到时必须按"什么都没开"处理：既不抛异常，也**不能全开**。

    方向错了（全开）等于把"权限系统故障"变成"权限全开"，
    那比少显示几个页签严重得多。
    """
    import src.api.routes.my_features as mf

    def boom(*_args, **_kwargs):
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(mf, "get_platform_config", boom)
    views, admin = visible_views_for_tier("vip")
    assert views == [], f"配置读不到时不该下发任何页签，实际：{views}"
    assert admin is False


def test_visible_views_keep_view_feature_declaration_order() -> None:
    """顺序必须是 `VIEW_FEATURE` 的声明顺序 —— 前端照着它从左到右排页签。"""
    enabled = {k: True for k in VIEW_FEATURE.values()}
    views = build_visible_views(enabled, is_admin=True)
    declared = list(VIEW_FEATURE.keys())
    positions = [declared.index(v) for v in views]
    assert positions == sorted(positions), f"顺序被打乱：{views}"


# ======================================================================
# 二、HTTP 层：三处下发口必须逐字一致
# ======================================================================

@pytest.mark.parametrize("tier", ["admin", "vip", "trial"])
def test_login_bootstrap_and_features_agree(env, tier: str) -> None:
    """`/auth/login`、`/auth/bootstrap`、`/me/features` 的清单必须**完全相同**。

    前端把它们当成同一份东西用：登录/开机探测负责"首帧就有页签"，
    `/me/features` 负责后台校验。两者一旦分叉，用户会看到页签"闪一下变了"。
    """
    c = env
    _, login_body = login_as(c, tier)

    features_resp = c.get("/api/v1/me/features")
    assert features_resp.status_code == 200, features_resp.text[:200]
    features_body = features_resp.json()

    boot_resp = c.get("/api/v1/auth/bootstrap")
    assert boot_resp.status_code == 200, boot_resp.text[:200]
    boot_body = boot_resp.json()

    official = features_body["visible_views"]

    assert "visible_views" in login_body, (
        "登录响应必须带 `visible_views`：前端靠它首帧渲染一级目录，"
        "否则又要多等一趟 `/me/features` 往返（用户报障 2026-09-28）")
    assert "visible_views" in boot_body, (
        "开机探测必须带 `visible_views`：刷新页面这条路径同理")

    assert login_body["visible_views"] == official, (
        f"登录响应与 /me/features 分叉：{login_body['visible_views']} != {official}")
    assert boot_body["visible_views"] == official, (
        f"bootstrap 与 /me/features 分叉：{boot_body['visible_views']} != {official}")

    assert login_body["is_admin"] == features_body["is_admin"]
    assert boot_body["is_admin"] == features_body["is_admin"]


def test_bootstrap_anonymous_does_not_claim_views(env) -> None:
    """未登录时**不下发**页签清单。

    前端把"空清单"当"没拿到"处理（回退最小集合），这里挡住的是另一种写法：
    未登录也回一个 `visible_views: []`，那会让前端以为"这个用户一个页签都没有"。
    """
    body = env.get("/api/v1/auth/bootstrap").json()
    assert body["authenticated"] is False
    assert not body.get("visible_views"), f"未登录不该下发页签：{body.get('visible_views')}"
