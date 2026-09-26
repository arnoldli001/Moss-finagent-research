"""管理员平台接口测试：套餐配置 / 资源监控 / 功能权限 / 我的页签。

对应设计：§8.6.10、§9。

## 本文件要钉死的四件事

1. **权限门槛**：非管理员读写这些接口一律 403；
2. **资源监控的聚合口径**：异常只算 5xx（4xx 是客户端问题，不是异常），
   延迟要分位而不是只有均值；
3. **权限矩阵与套餐一致**：矩阵是评审视图，与 `/tiers` 不得漂移；
4. **`/me/features` 的 `visible_views`**：前端照着它渲染页签，
   所以"关了功能页签就消失"必须在服务端就成立。
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api.routes.auth import get_auth_service, reset_auth_service
from src.domain.platform.config import (
    FEATURES,
    RESOURCE_FIELDS,
    reset_platform_config,
)
from src.domain.quota.service import reset_quota_service

PASSWORD = "PlatformAdm#2026x"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """返回 `(client, 审计文件路径)`。

    审计目录也指到 tmp：资源监控是**读审计文件**做聚合的，
    不隔离的话测试会读到真实使用记录，断言语义就没了。
    """
    db_path = str(tmp_path / "plat.db")
    audit_dir = tmp_path / "audit"
    llm_audit_dir = tmp_path / "llm_audit"
    monkeypatch.setenv("MOSS_ENV", "test")
    monkeypatch.setenv("MOSS_SQLITE_PATH", db_path)
    monkeypatch.setenv("MOSS_NOTIFY_CHANNEL", "console")
    monkeypatch.setenv("MOSS_AUDIT_DIR", str(audit_dir))
    # ⚠️ LLM 审计目录**也必须隔离**：监控页的"本月 token"是从它累计的，
    #    不隔离就会读到**真实的** `data/audit/llm_audit.jsonl` ——
    #    断言会随本机历史数据漂移（今天全绿、明天变红），而且测试会去碰
    #    生产审计文件。这与夹具文档里"审计目录也指到 tmp"的理由是同一条，
    #    只是那份审计是后加的、当时漏了。
    monkeypatch.setenv("LLM_AUDIT_DIR", str(llm_audit_dir))
    monkeypatch.setenv("MOSS_TIER_CONFIG", str(tmp_path / "tiers.json"))
    from src.core.config import get_settings

    get_settings.cache_clear()
    reset_auth_service()
    reset_quota_service()
    reset_platform_config()

    from src.api.main import app

    c = TestClient(app)
    service = get_auth_service()
    assert str(getattr(service._repo, "_db_path", "")) == db_path
    service._repo.clear_all()
    yield c, audit_dir / "access_audit.jsonl"
    reset_auth_service()
    reset_quota_service()
    reset_platform_config()
    get_settings.cache_clear()


@pytest.fixture()
def llm_audit(tmp_path) -> Path:
    """LLM 审计路径（与 `env` 共用同一个 `tmp_path`）。

    为什么单独一个夹具而不是把 `env` 改成三元组：本文件有 20 多处
    `c, audit = env`，为了多返回一个路径去改遍全文件，只会让 diff 淹没
    真正的改动。两个夹具共用同一个 `tmp_path`，拿到的路径与 `env`
    里设置的 `LLM_AUDIT_DIR` 是同一个。
    """
    return tmp_path / "llm_audit" / "llm_audit.jsonl"


def write_llm_audit(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def llm_row(*, tenant: str, tokens_in: int = 0, tokens_out: int = 0,
            ts: str = "") -> dict:
    return {
        # 真实写入用的是**本地无时区**的 isoformat（`datetime.now()`），
        # 测试必须照抄这个口径，否则"只统计本月"这条断言会因为时区差
        # 而偶发失败（而这正是最容易写错的地方）。
        "ts": ts or datetime.now().isoformat(timespec="seconds"),
        "trace_id": "tr", "agent_id": "A08", "task_tier": "light",
        "tenant_id": tenant, "user_id": "u_x",
        "tokens_in": tokens_in, "tokens_out": tokens_out,
    }


def make_and_login(client: TestClient, tier: str) -> str:
    repo = get_auth_service()._repo
    suffix = secrets.token_hex(4)
    username = f"plat_{tier}_{suffix}"
    user_id = f"u_{suffix}"
    repo.create_user(user_id=user_id, username=username, display_name=username,
                     status="active", applied_tier=tier,
                     valid_until=(datetime.now().astimezone()
                                  + timedelta(days=30)).isoformat(
                                      timespec="seconds"))
    repo.set_password(user_id, PASSWORD)
    r = client.post("/api/v1/auth/login",
                    json={"account": username, "password": PASSWORD,
                          "remember_me": False})
    assert r.status_code == 200, r.text[:200]
    return user_id


def write_audit(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def audit_row(*, tenant: str, path: str, status: int = 200,
              latency: int = 100, user: str = "u_x") -> dict:
    return {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "method": "GET", "path": path, "status": status,
        "latency_ms": latency, "action": "",
        "user_id": user, "tenant_id": tenant, "auth_source": "session",
    }


# ======================================================================
# 一、权限门槛
# ======================================================================

def test_platform_endpoints_require_admin(env) -> None:
    c, _ = env
    for path in ("/api/v1/admin/platform/tiers",
                 "/api/v1/admin/platform/monitor",
                 "/api/v1/admin/platform/permissions-matrix"):
        assert c.get(path).status_code == 401, path

    make_and_login(c, "vip")
    for path in ("/api/v1/admin/platform/tiers",
                 "/api/v1/admin/platform/monitor",
                 "/api/v1/admin/platform/permissions-matrix"):
        r = c.get(path)
        assert r.status_code == 403, f"{path} → {r.status_code}"
    assert c.put("/api/v1/admin/platform/tiers/vip",
                 json={"resources": {"api_calls_per_minute": 1}}
                 ).status_code == 403


# ======================================================================
# 二、套餐读写
# ======================================================================

def test_admin_reads_tiers_with_metadata(env) -> None:
    c, _ = env
    make_and_login(c, "admin")
    r = c.get("/api/v1/admin/platform/tiers")
    assert r.status_code == 200, r.text[:200]
    body = r.json()
    assert {t["key"] for t in body["tiers"]} == {"admin", "vip", "trial"}
    # 字段清单必须一起下发：前端据此渲染表单，不自己硬编码
    assert set(body["features"]) == set(FEATURES)
    assert set(body["resources"]) == set(RESOURCE_FIELDS)
    for tier in body["tiers"]:
        assert set(tier["resources"]) == set(RESOURCE_FIELDS)
        assert set(tier["features"]) == set(FEATURES)


def test_admin_updates_tier_limits_and_pricing(env) -> None:
    c, _ = env
    make_and_login(c, "admin")
    r = c.put("/api/v1/admin/platform/tiers/vip", json={
        "resources": {"api_calls_per_minute": 777},
        "features": {"quant.auction": True},
        "pricing": {"quant.auction": 288.0},
    })
    assert r.status_code == 200, r.text[:250]
    tier = r.json()["tier"]
    assert tier["resources"]["api_calls_per_minute"] == 777
    assert tier["features"]["quant.auction"] is True
    assert tier["pricing"]["quant.auction"] == 288.0
    # 月费已下线：响应里不该再有这个键（前端类型也已同步移除）
    assert "monthly_price" not in tier

    # 再读一次确认持久化（不是只改了内存）
    again = c.get("/api/v1/admin/platform/tiers").json()
    vip = next(t for t in again["tiers"] if t["key"] == "vip")
    assert vip["pricing"]["quant.auction"] == 288.0
    assert "monthly_price" not in vip


def test_feature_only_save_does_not_touch_pricing(env) -> None:
    """★★ 只改开关时**绝不能**动定价。

    ## 这条来自"功能权限面板去掉定价"这个改动（用户口径 2026-09-23）

    界面不再有定价输入框，所以保存时**只发 `features`**。
    但如果后端把"没传的字段"当成"要清空"（例如
    `patch.get("pricing") or {}` 直接覆盖），管理员每改一次开关，
    该等级定价就会被静默抹成 0 —— 报价凭空消失，而界面上一切正常，
    要到客户对账时才发现。
    """
    c, _ = env
    make_and_login(c, "admin")
    r = c.put("/api/v1/admin/platform/tiers/vip",
              json={"pricing": {"quant.auction": 199.0}})
    assert r.status_code == 200, r.text[:200]
    before = next(t for t in
                  c.get("/api/v1/admin/platform/tiers").json()["tiers"]
                  if t["key"] == "vip")
    assert before["pricing"]["quant.auction"] == 199.0

    # 只改开关（模拟前端勾一下再保存）
    r = c.put("/api/v1/admin/platform/tiers/vip",
              json={"features": {"backtest": False}})
    assert r.status_code == 200, r.text[:200]

    after = next(t for t in
                 c.get("/api/v1/admin/platform/tiers").json()["tiers"]
                 if t["key"] == "vip")
    assert after["features"]["backtest"] is False, "开关没生效"
    assert after["pricing"]["quant.auction"] == 199.0, (
        "只改开关却把定价抹掉了 —— 报价会凭空消失，而界面看不出问题")
    assert after["features"]["research"] is True, "其它开关被这次提交影响了"


def test_legacy_bundle_sending_monthly_price_still_saves(env) -> None:
    """★ 旧版前端包仍会发 `monthly_price`：**保存必须成功**，不能 400。

    用户改完功能/资源面板时，浏览器里可能还缓存着旧包。若服务端对这个
    已下线的键报错，症状就是"资源管控页保存一律失败"——用户会以为整个
    面板坏了，而真正要做的只是硬刷新。所以接住它、忽略它、记一条日志。
    """
    c, _ = env
    make_and_login(c, "admin")
    r = c.put("/api/v1/admin/platform/tiers/vip",
              json={"monthly_price": 1299.0,
                    "resources": {"api_calls_per_minute": 456}})
    assert r.status_code == 200, r.text[:250]
    tier = r.json()["tier"]
    assert tier["resources"]["api_calls_per_minute"] == 456, (
        "带了一个已下线字段，同一次请求里的有效字段却被丢了")
    assert "monthly_price" not in tier


def test_tiers_payload_still_carries_pricing_field(env) -> None:
    """★ 每项功能的定价字段仍要下发（虽然界面不再编辑它）。

    去掉界面的输入框 **≠** 从接口删掉字段：加购价仍要能通过 API 读写
    （账单、将来的商务后台都可能依赖它）。删字段是**破坏性变更**，
    会让那些调用方静默拿到 undefined。
    权限矩阵按格子带 `price` 也依赖它（见 `test_...matrix...`）。
    """
    c, _ = env
    make_and_login(c, "admin")
    body = c.get("/api/v1/admin/platform/tiers").json()
    for tier in body["tiers"]:
        assert "pricing" in tier and isinstance(tier["pricing"], dict)
        assert set(tier["pricing"]) == set(FEATURES)
    assert set(body["features"]) == set(FEATURES)


def test_bad_patch_returns_400_with_reason(env) -> None:
    """非法输入 → 400 且**原因可读**（管理员要知道错在哪）。"""
    c, _ = env
    make_and_login(c, "admin")
    r = c.put("/api/v1/admin/platform/tiers/vip",
              json={"features": {"no.such": True}})
    assert r.status_code == 400
    assert "未知功能" in r.text

    r = c.put("/api/v1/admin/platform/tiers/gold", json={"label": "x"})
    assert r.status_code == 400
    assert "未知等级" in r.text


def test_tier_update_does_not_touch_other_tiers(env) -> None:
    c, _ = env
    make_and_login(c, "admin")
    before = {t["key"]: t["resources"] for t in
              c.get("/api/v1/admin/platform/tiers").json()["tiers"]}
    c.put("/api/v1/admin/platform/tiers/vip",
          json={"resources": {"watchlist_stocks": 42}})
    after = {t["key"]: t["resources"] for t in
             c.get("/api/v1/admin/platform/tiers").json()["tiers"]}
    assert after["trial"] == before["trial"]
    assert after["admin"] == before["admin"]
    assert after["vip"]["watchlist_stocks"] == 42


# ======================================================================
# 三、权限矩阵
# ======================================================================

def test_permissions_matrix_matches_tiers(env) -> None:
    """★ 矩阵与 `/tiers` 必须一致 —— 它是评审视图，不能漂移。"""
    c, _ = env
    make_and_login(c, "admin")
    matrix = c.get("/api/v1/admin/platform/permissions-matrix").json()
    tiers = {t["key"]: t for t in
             c.get("/api/v1/admin/platform/tiers").json()["tiers"]}

    assert {r["feature"] for r in matrix["rows"]} == set(FEATURES)
    for row in matrix["rows"]:
        for tier_key, cell in row["tiers"].items():
            expected = tiers[tier_key]["features"][row["feature"]]
            assert cell["enabled"] is expected, (
                f"{tier_key}.{row['feature']} 矩阵与套餐不一致")
            assert cell["price"] == tiers[tier_key]["pricing"][row["feature"]]


def test_matrix_reflects_a_change(env) -> None:
    c, _ = env
    make_and_login(c, "admin")
    c.put("/api/v1/admin/platform/tiers/trial",
          json={"features": {"backtest": True}})
    rows = c.get("/api/v1/admin/platform/permissions-matrix").json()["rows"]
    backtest = next(r for r in rows if r["feature"] == "backtest")
    assert backtest["tiers"]["trial"]["enabled"] is True


# ======================================================================
# 四、资源监控聚合
# ======================================================================

def test_monitor_requires_admin_and_handles_missing_audit(env) -> None:
    c, audit = env
    make_and_login(c, "admin")
    assert not audit.exists()
    r = c.get("/api/v1/admin/platform/monitor")
    assert r.status_code == 200, r.text[:200]
    body = r.json()
    assert body["by_tenant"] == [] and body["sampled_calls"] == 0
    assert body["overall"]["calls"] == 0


def test_monitor_aggregates_by_tenant(env) -> None:
    """★ 按租户聚合：调用次数、异常、延迟分位、活跃用户数。"""
    c, audit = env
    make_and_login(c, "admin")
    write_audit(audit, [
        audit_row(tenant="vip", path="/a", latency=100, user="u1"),
        audit_row(tenant="vip", path="/b", latency=200, user="u1"),
        audit_row(tenant="vip", path="/a", latency=300, user="u2"),
        audit_row(tenant="trial", path="/c", latency=50, user="u3"),
    ])
    body = c.get("/api/v1/admin/platform/monitor").json()
    assert body["overall"]["calls"] == 4
    by = {t["tenant_id"]: t for t in body["by_tenant"]}
    assert by["vip"]["calls"] == 3 and by["trial"]["calls"] == 1
    assert by["vip"]["users"] == 2
    assert by["vip"]["latency_ms"]["avg"] == 200.0
    assert by["vip"]["latency_ms"]["max"] == 300.0
    assert by["vip"]["latency_ms"]["p95"] >= 200.0
    assert by["vip"]["label"], "应带上等级中文名"


def test_monitor_counts_only_5xx_as_errors(env) -> None:
    """★ 异常只算 5xx。

    把 4xx 也算进去，一次扫描就能刷出几千个"异常"，
    指标立刻失去意义（而且会掩盖真正的服务端故障）。
    """
    c, audit = env
    make_and_login(c, "admin")
    write_audit(audit, [
        audit_row(tenant="vip", path="/a", status=500),
        audit_row(tenant="vip", path="/a", status=503),
        audit_row(tenant="vip", path="/a", status=404),
        audit_row(tenant="vip", path="/a", status=403),
        audit_row(tenant="vip", path="/a", status=200),
    ])
    body = c.get("/api/v1/admin/platform/monitor").json()
    vip = next(t for t in body["by_tenant"] if t["tenant_id"] == "vip")
    assert vip["calls"] == 5
    assert vip["errors"] == 2, f"实际 {vip['errors']}（4xx 不该算异常）"
    assert vip["error_rate"] == 0.4


def test_monitor_reports_top_paths_and_limits(env) -> None:
    c, audit = env
    make_and_login(c, "admin")
    write_audit(audit, [
        audit_row(tenant="vip", path="/hot") for _ in range(5)
    ] + [audit_row(tenant="vip", path="/cold")])
    body = c.get("/api/v1/admin/platform/monitor").json()
    vip = next(t for t in body["by_tenant"] if t["tenant_id"] == "vip")
    assert vip["top_paths"][0] == ["/hot", 5]
    # 与套餐上限并列显示 —— 监控的价值在于"用掉了几成"
    assert vip["limits"]["api_calls_per_minute"] > 0


def test_monitor_window_filters_old_rows(env) -> None:
    c, audit = env
    make_and_login(c, "admin")
    old = audit_row(tenant="vip", path="/old")
    old["at"] = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(
        timespec="seconds")
    write_audit(audit, [old, audit_row(tenant="vip", path="/new")])
    body = c.get("/api/v1/admin/platform/monitor?minutes=60").json()
    assert body["sampled_calls"] == 1, "时间窗口没生效"
    all_time = c.get("/api/v1/admin/platform/monitor?minutes=0").json()
    assert all_time["sampled_calls"] == 2


def test_monitor_tolerates_corrupt_lines(env) -> None:
    """★ 单行损坏不能让整页监控失败（日志是追加写的，断电会留半行）。"""
    c, audit = env
    make_and_login(c, "admin")
    audit.parent.mkdir(parents=True, exist_ok=True)
    with audit.open("a", encoding="utf-8") as fh:
        fh.write('{"at":"2026-01-01T00:00:00+00:00","path":"/half"\n')  # 坏行
    write_audit(audit, [audit_row(tenant="vip", path="/good")])
    r = c.get("/api/v1/admin/platform/monitor")
    assert r.status_code == 200
    assert r.json()["sampled_calls"] == 1


def test_monitor_marks_unauthenticated_traffic(env) -> None:
    """未登录流量归到 `(未登录)` 桶 —— 否则它凭空消失，排查时以为没请求。"""
    c, audit = env
    make_and_login(c, "admin")
    write_audit(audit, [audit_row(tenant="", path="/api/v1/auth/login",
                                  user="")])
    body = c.get("/api/v1/admin/platform/monitor").json()
    assert any(t["tenant_id"] == "(未登录)" for t in body["by_tenant"])


def test_monitor_does_not_fake_datasource_health(env) -> None:
    """★ 拿不到数据源健康时如实说明，**不显示假绿**。"""
    c, _ = env
    make_and_login(c, "admin")
    from src.api.main import app

    saved = getattr(app.state, "runtime", None)
    app.state.runtime = None
    try:
        body = c.get("/api/v1/admin/platform/monitor").json()
        assert body["data_sources"] == []
        assert body["data_sources_note"], "没有说明为什么是空的"
    finally:
        app.state.runtime = saved


# ======================================================================
# 四之二、剩余配额（目标口径：展示数据源健康**与剩余配额**）
# ======================================================================

def test_monitor_reports_remaining_quota(env) -> None:
    """★ 今日调用的"已用 / 上限 / 剩余"必须都能算出来。

    上限取该租户套餐里的 `api_calls_per_day`，已用取**今日**的访问审计行。
    """
    c, audit = env
    make_and_login(c, "admin")
    write_audit(audit, [audit_row(tenant="trial", path="/a") for _ in range(7)])
    body = c.get("/api/v1/admin/platform/monitor").json()
    trial = next(t for t in body["by_tenant"] if t["tenant_id"] == "trial")
    limits = c.get("/api/v1/admin/platform/tiers").json()["tiers"]
    day_limit = next(t for t in limits if t["key"] == "trial"
                     )["resources"]["api_calls_per_day"]

    u = trial["usage"]
    assert u["calls_today"] == 7
    assert u["calls_today_limit"] == day_limit
    assert u["calls_today_remaining"] == day_limit - 7
    assert u["calls_today_used_pct"] == round(7 / day_limit * 100, 1)


def test_monitor_quota_is_daily_not_window_scoped(env) -> None:
    """★★ 核心口径：日额度按**自然日**算，不受 `minutes` 窗口影响。

    这是一个真实的错误来源：用 60 分钟窗口的调用数去比"日上限"，
    会把"今天已经用掉 30%"显示成"用掉 0%"，于是管理员以为额度还很宽裕。
    所以窗口过滤只影响 `calls`/延迟等**窗口内**指标，
    `usage.calls_today` 必须是"今天零点以来"的全部调用。
    """
    c, audit = env
    make_and_login(c, "admin")
    in_window = audit_row(tenant="trial", path="/now")
    # 3 小时前：**不在** 60 分钟窗口内，但**仍在今天**（构造时避开跨零点：
    # 若当前时间距零点不足 3 小时，用 30 分钟前的行也满足"今天但不在窗口"）
    now = datetime.now(timezone.utc)
    day_start = datetime.now().astimezone().replace(
        hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    earlier = max(now - timedelta(hours=3), day_start)
    old = dict(audit_row(tenant="trial", path="/earlier"))
    old["at"] = earlier.isoformat(timespec="seconds")
    write_audit(audit, [old, in_window])

    body = c.get("/api/v1/admin/platform/monitor?minutes=60").json()
    trial = next(t for t in body["by_tenant"] if t["tenant_id"] == "trial")
    expected = 2 if earlier >= now - timedelta(minutes=60) else 1
    assert trial["calls"] == expected, "窗口内的调用数不对"
    assert trial["usage"]["calls_today"] == 2, (
        f"日额度必须覆盖今天全部调用（含窗口外的 {earlier.isoformat()}），"
        f"实际 {trial['usage']['calls_today']}")


def test_monitor_yesterday_rows_do_not_count_toward_today(env) -> None:
    """昨天的调用**不能**算进今日额度（否则每天开盘前额度就已经被占满）。"""
    c, audit = env
    make_and_login(c, "admin")
    yesterday = dict(audit_row(tenant="trial", path="/y"))
    yesterday["at"] = (datetime.now(timezone.utc)
                       - timedelta(days=1)).isoformat(timespec="seconds")
    write_audit(audit, [yesterday, audit_row(tenant="trial", path="/t")])
    body = c.get("/api/v1/admin/platform/monitor?minutes=0").json()
    trial = next(t for t in body["by_tenant"] if t["tenant_id"] == "trial")
    assert trial["calls"] == 2, "全时段视图应看到两条"
    assert trial["usage"]["calls_today"] == 1, "今日额度只该算今天那一条"


def test_monitor_remaining_never_negative(env) -> None:
    """超额调用时剩余夹在 0 —— 负数会显示成"-3200"，看起来像账算错了。"""
    c, audit = env
    make_and_login(c, "admin")
    c.put("/api/v1/admin/platform/tiers/trial",
          json={"resources": {"api_calls_per_day": 3}})
    write_audit(audit, [audit_row(tenant="trial", path="/a") for _ in range(5)])
    body = c.get("/api/v1/admin/platform/monitor").json()
    trial = next(t for t in body["by_tenant"] if t["tenant_id"] == "trial")
    u = trial["usage"]
    assert u["calls_today"] == 5 and u["calls_today_limit"] == 3
    assert u["calls_today_remaining"] == 0
    assert u["calls_today_used_pct"] == 100.0


def test_monitor_token_usage_comes_from_llm_audit(env, llm_audit) -> None:
    """★ 本月 token 用量来自 **LLM 审计**（逐次记录 tokens_in/out 的唯一地方）。

    按租户聚合、`in + out` 都算、且只统计本月。
    """
    c, audit = env
    make_and_login(c, "admin")
    # by_tenant 是按**访问审计**列租户的，所以先给 vip 造一条访问记录，
    # 它的 token 用量才会被展示（token 归属与访问归属来自两份不同的审计）
    write_audit(audit, [audit_row(tenant="vip", path="/a")])
    last_month = (datetime.now().replace(day=1, hour=0, minute=0, second=0,
                                        microsecond=0)
                  - timedelta(days=1)).isoformat(timespec="seconds")
    write_llm_audit(llm_audit, [
        llm_row(tenant="vip", tokens_in=100, tokens_out=50),
        llm_row(tenant="vip", tokens_in=10, tokens_out=5),
        llm_row(tenant="trial", tokens_in=7, tokens_out=3),
        llm_row(tenant="vip", tokens_in=999, tokens_out=1, ts=last_month),
        llm_row(tenant="", tokens_in=2, tokens_out=2),      # 平台自身用量
    ])
    body = c.get("/api/v1/admin/platform/monitor").json()
    # 只有访问审计里出现过的租户才有行；这里造一条 vip 的访问记录
    by = {t["tenant_id"]: t for t in body["by_tenant"]}
    assert "vip" in by, "没有访问记录的租户不显示（token 归属需要它）"

    limits = {t["key"]: t["resources"] for t in
              c.get("/api/v1/admin/platform/tiers").json()["tiers"]}
    u = by["vip"]["usage"]
    assert u["tokens_month"] == 165, f"上月记录或漏算：{u}"
    assert u["tokens_measured"] is True
    assert u["tokens_month_limit"] == limits["vip"]["llm_tokens_per_month"]
    assert u["tokens_month_remaining"] == u["tokens_month_limit"] - 165


def test_monitor_token_unmeasured_is_not_zero(env, llm_audit) -> None:
    """★★ 没有 LLM 审计时必须标"未量到"，**不能显示成"用掉 0 / 剩余 100%"**。

    "这个月一点没用"与"我们还没量到用量"对管理员的决策含义完全相反：
    前者可以放心，后者说明审计没接上、数字不可信。
    """
    c, audit = env
    make_and_login(c, "admin")
    write_audit(audit, [audit_row(tenant="vip", path="/a")])
    assert not llm_audit.exists()
    body = c.get("/api/v1/admin/platform/monitor").json()
    vip = next(t for t in body["by_tenant"] if t["tenant_id"] == "vip")
    assert vip["usage"]["tokens_month"] == 0
    assert vip["usage"]["tokens_measured"] is False
    assert vip["usage"]["tokens_month_used_pct"] is None, (
        "未量到时不该给出使用率 —— 0% 会被读成'没用过'")
    assert any("token" in n for n in body["quota_basis"]["notes"]), (
        "未量到必须在口径说明里讲清楚")


# ======================================================================
# 四之三、LLM 花费记账（总额 / 按前端功能 / 按租户 / 汇总）
# ======================================================================

def cost_llm_row(*, path: str = "", agent: str = "A17_recommend",
                 tenant: str = "", provider: str = "deepseek",
                 model: str = "deepseek-flash", tokens_in: int = 1000,
                 tokens_out: int = 2000, ts: str = "") -> dict:
    """一条**可用于计价**的 LLM 审计行（`llm_row` 没有 model/provider）。"""
    return {
        "ts": ts or datetime.now().isoformat(timespec="seconds"),
        "trace_id": "tr", "agent_id": agent, "task_tier": "reasoning",
        "tenant_id": tenant, "user_id": f"u_{tenant or 'none'}",
        "tenant_source": "session", "path": path,
        "provider": provider, "model": model,
        "tokens_in": tokens_in, "tokens_out": tokens_out, "cache_hit": False,
    }


def test_monitor_reports_llm_cost_summary(env, llm_audit) -> None:
    """★ 汇总：本月总花费 / 今日花费 / 计费与免费调用数。

    这是管理员"这个月烧了多少钱"的唯一入口。
    """
    c, _ = env
    make_and_login(c, "admin")
    write_llm_audit(llm_audit, [
        cost_llm_row(path="/api/v1/research/analyze", tenant="vip"),
        cost_llm_row(path="/api/v1/research/analyze", tenant="vip"),
        cost_llm_row(provider="ollama", model="qwen3:8b",
                     tokens_in=5000, tokens_out=5000),
    ])
    body = c.get("/api/v1/admin/platform/monitor").json()
    cost = body["llm_cost"]
    assert cost["readable"] is True
    assert cost["calls"] == 3
    assert cost["paid_calls"] == 2 and cost["free_calls"] == 1
    assert cost["total_cny"] > 0
    # 今日花费：这些行都是"现在"写的，必然落在今天
    assert cost["today_cny"] == pytest.approx(cost["total_cny"], abs=1e-6)
    assert cost["window_label"]
    assert cost["basis_notes"], "口径说明必须随数据下发"


def test_monitor_cost_splits_by_frontend_feature(env, llm_audit) -> None:
    """★★ 核心诉求：**钱分别来自前端哪些功能**，各自多少。

    有请求路径的行按路径精确归属；没有路径的（脚本/作业）单独成桶 ——
    否则 "mainline_relevance 一个脚本烧掉 487 元" 会被并进某个前端功能，
    让那个功能的数字失去意义。
    """
    c, _ = env
    make_and_login(c, "admin")
    write_llm_audit(llm_audit, [
        cost_llm_row(path="/api/v1/research/analyze", tenant="vip"),
        cost_llm_row(path="/api/v1/intel/feed", tenant="vip"),
        cost_llm_row(path="/api/v1/intraday/snapshot", tenant="trial"),
        cost_llm_row(agent="mainline_relevance", tenant=""),
    ])
    cost = c.get("/api/v1/admin/platform/monitor").json()["llm_cost"]
    by_key = {f["key"]: f for f in cost["by_feature"]}
    assert {"research", "intel.radar", "quant.intraday", "ops.script"} <= set(by_key)
    # 精确 vs 推断必须能分辨（界面照它显示"这个数字准不准"）
    assert by_key["research"]["basis"] == "page"
    assert by_key["ops.script"]["basis"] == "agent"
    # 前端功能标记：脚本桶不是可售卖功能
    assert by_key["research"]["page_feature"] is True
    assert by_key["ops.script"]["page_feature"] is False
    # 每行都有金额与占比
    assert all(f["cny"] > 0 for f in by_key.values())
    assert sum(f["share_pct"] for f in cost["by_feature"]) == \
        pytest.approx(100.0, abs=0.3)
    # 汇总里的"最大来源"
    assert cost["by_feature"][0]["cny"] >= cost["by_feature"][-1]["cny"]


def test_monitor_cost_per_tenant_and_tenant_row(env, llm_audit) -> None:
    """★ 按租户：`llm_cost.by_tenant` 与 `by_tenant[].llm_cost` 都要有。"""
    c, audit = env
    make_and_login(c, "admin")
    write_audit(audit, [audit_row(tenant="vip", path="/a")])
    write_llm_audit(llm_audit, [
        cost_llm_row(path="/api/v1/research/analyze", tenant="vip"),
        cost_llm_row(path="/api/v1/research/analyze", tenant="vip"),
        cost_llm_row(agent="mainline_relevance", tenant=""),
    ])
    body = c.get("/api/v1/admin/platform/monitor").json()
    by = {t["tenant_id"]: t for t in body["llm_cost"]["by_tenant"]}
    assert by["vip"]["calls"] == 2 and by["vip"]["cny"] > 0
    assert by["vip"]["sources"] == ["session"]
    assert by["(未归属)"]["calls"] == 1
    # 租户行里也要带上（管理员一眼看到"这个客户花了多少"）
    vip_row = next(t for t in body["by_tenant"] if t["tenant_id"] == "vip")
    assert vip_row["llm_cost"]["measured"] is True
    assert vip_row["llm_cost"]["cny"] == by["vip"]["cny"]
    # 只有费用、没有访问记录的租户**不能凭空消失**（钱会算不见）
    assert "(未归属)" in {t["tenant_id"] for t in body["by_tenant"]}


def test_monitor_flags_unpriced_model(env, llm_audit) -> None:
    """未登记价格的模型名：金额按兜底价估算，但**必须**被标出来。"""
    c, _ = env
    make_and_login(c, "admin")
    write_llm_audit(llm_audit, [
        cost_llm_row(path="/api/v1/research/analyze", tenant="vip",
                     model="deepseek-v4-flash"),
    ])
    cost = c.get("/api/v1/admin/platform/monitor").json()["llm_cost"]
    assert cost["unpriced_calls"] == 1
    assert cost["unpriced_models"] == [["deepseek-v4-flash", 1]]
    assert any("deepseek-v4-flash" in n for n in cost["basis_notes"])


def test_monitor_missing_llm_audit_is_not_zero_cost(env, llm_audit) -> None:
    """★★ 读不到审计时**不能**显示成"花了 0 元"。

    "这个月没花钱"与"监控没接上"对管理员的含义完全相反。
    """
    c, _ = env
    make_and_login(c, "admin")
    assert not llm_audit.exists()
    cost = c.get("/api/v1/admin/platform/monitor").json()["llm_cost"]
    assert cost["readable"] is False
    assert cost["total_cny"] == 0


def test_monitor_quota_basis_discloses_limits(env) -> None:
    """★ 口径与局限必须随数据一起下发（管理员据此决定要不要加额度）。"""
    c, audit = env
    make_and_login(c, "admin")
    write_audit(audit, [audit_row(tenant="vip", path="/a")])
    body = c.get("/api/v1/admin/platform/monitor").json()
    basis = body["quota_basis"]
    assert basis["day_start"] and basis["month_start"]
    assert basis["audit_lines"] >= 1
    joined = " ".join(basis["notes"])
    for word in ("访问审计", "LLM", "不是计费账单", "尾部"):
        assert word in joined, f"口径说明缺了「{word}」：{joined}"


def test_monitor_marks_tenants_without_plan(env) -> None:
    """没有套餐的租户（匿名/平台自身）要**说清是什么**，且不给额度。"""
    c, audit = env
    make_and_login(c, "admin")
    write_audit(audit, [audit_row(tenant="", path="/login", user="")])
    body = c.get("/api/v1/admin/platform/monitor").json()
    anon = next(t for t in body["by_tenant"] if t["tenant_id"] == "(未登录)")
    assert anon["known_tier"] is False
    assert "未登录" in anon["label"]
    assert anon["usage"]["calls_today_limit"] == 0
    assert anon["usage"]["calls_today_remaining"] == 0


# ======================================================================
# 五、`/me/features`：前端照着它渲染页签
# ======================================================================

def test_my_features_lists_visible_views(env) -> None:
    c, _ = env
    make_and_login(c, "vip")
    body = c.get("/api/v1/me/features").json()
    assert body["tier"] == "vip"
    assert set(body["features"]) == set(FEATURES)
    assert "research" in body["visible_views"]
    # VIP 默认竞价选股关闭 → quant 页仍可见（做T开着），但子项如实标注
    assert "intraday" in body["visible_views"]
    assert body["quant_views"]["quant.auction"] is False
    assert body["resources"]["api_calls_per_minute"] > 0
    assert "安全边界" in body["note"] or "不构成" in body["note"]


def test_my_features_hides_disabled_views(env) -> None:
    """★ 管理员关掉某个功能 → 该等级用户的 `visible_views` 里就没有它。

    这是"前端照着服务端渲染页签"的落点：前端不再自己判断。
    """
    c, _ = env
    make_and_login(c, "admin")
    c.put("/api/v1/admin/platform/tiers/trial",
          json={"features": {"backtest": True, "fundflow": True}})
    c.post("/api/v1/auth/logout")

    make_and_login(c, "trial")
    body = c.get("/api/v1/me/features").json()
    assert "backtest" in body["visible_views"]
    assert "fundflow" in body["visible_views"]
    assert "scheduler" not in body["visible_views"], (
        "试用档默认没开调度管理，却出现在可见页签里")


def test_my_features_quant_page_needs_only_one_subfeature(env) -> None:
    """★ 量化交易页：三个子功能**任意一个**开启即显示整页。

    否则"只买了做T"的用户连页面都进不去 —— 而他明明买了东西。
    """
    c, _ = env
    make_and_login(c, "admin")
    c.put("/api/v1/admin/platform/tiers/trial", json={
        "features": {"quant.intraday": False, "quant.select": False,
                     "quant.auction": True}})
    c.post("/api/v1/auth/logout")

    make_and_login(c, "trial")
    body = c.get("/api/v1/me/features").json()
    assert "intraday" in body["visible_views"]
    assert body["quant_views"]["quant.auction"] is True


def test_my_features_requires_login(env) -> None:
    c, _ = env
    assert c.get("/api/v1/me/features").status_code == 401
