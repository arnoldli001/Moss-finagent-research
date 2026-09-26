"""调度管理 = 管理员专属：把"看不见"和"调不动"两件事都钉死。

对应后端：
  - `src/api/routes/scheduler.py`（整条路由挂 `require_admin`）
  - `src/api/routes/my_features.py`（`ADMIN_ONLY_VIEWS`）

## 为什么值得单独一个文件（2026-09-26 实测缺口）

`/api/v1/scheduler/*` 原先**只有登录门槛**，没有任何授权判断。"调度管理只给
管理员"当时只体现在 `configs/platform_tiers.json` 的 `scheduler: false` ——
那是一个**可改的默认值**，不是规则：

  1. 管理员在「功能权限」页把 `scheduler` 勾给 VIP / 试用 → 对方立刻拿到页签；
  2. 更严重的是**页签从来不是边界**：带一个普通用户的会话 Cookie 直接
     `POST /api/v1/scheduler/jobs/snapshot_industry_watchlist/run` 就能触发
     **4 次完整研究图**（`src/scheduler/jobs.py::_graph_snapshot` 对
     `params.targets = [半导体, 煤炭, 创新药, 白酒]` 逐个 `ainvoke`）。

所以下面分别验证"看不见"（`visible_views`）与"调不动"（401/403），
其中"被授予了功能却仍然调不动"这一条是**核心**断言。

⚠️ 本文件在 `MOSS_ENV=test` 下运行：`LoginGateMiddleware` 在非公网环境
**完全放行**，所以这里拿到的 401 全部来自 `require_admin`，不是登录门槛。
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from src.api.routes.auth import get_auth_service, reset_auth_service
from src.domain.platform.config import reset_platform_config
from src.domain.quota.service import reset_quota_service

PASSWORD = "SchedAdm#2026x"

#: 调度管理的全部端点（读也要挡：作业清单/运行记录属**内部运维信息**，
#: `src/api/login_gate.py` 的文档里就把 `/api/v1/scheduler/jobs` 列在
#: "未登录也能拿到内部任务配置"那张表里）。
SCHEDULER_PATHS: tuple[str, ...] = (
    "/api/v1/scheduler/jobs",
    "/api/v1/scheduler/runs",
    "/api/v1/scheduler/runs/summary",
)
#: 手动触发（会跑真实作业）
TRIGGER_PATH = "/api/v1/scheduler/jobs/snapshot_macro/run"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """独立库 + 独立套餐配置的 TestClient（不跑 lifespan，因此不建真实图）。"""
    db_path = str(tmp_path / "sched.db")
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

    # `run_log` 平时由 lifespan 建（`src/api/main.py`）；这里不进 lifespan，
    # 手动放一个临时文件版的，这样"管理员能读到作业清单"这一步是真的走到了。
    from src.scheduler.run_log import RunLog

    monkeypatch.setattr(app.state, "run_log",
                        RunLog(str(tmp_path / "runs.jsonl")), raising=False)

    c = TestClient(app)
    assert str(getattr(get_auth_service()._repo, "_db_path", "")) == db_path
    get_auth_service()._repo.clear_all()
    yield c
    reset_auth_service()
    reset_quota_service()
    reset_platform_config()
    get_settings.cache_clear()


def make_and_login(client: TestClient, tier: str) -> None:
    """建一个该等级的用户并登录（照 `test_admin_platform_api` 的既有做法）。"""
    repo = get_auth_service()._repo
    suffix = secrets.token_hex(4)
    username = f"sched_{tier}_{suffix}"
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


# ======================================================================
# 一、未登录：401（"能看内部作业配置"这件事本身就不该对外）
# ======================================================================

def test_scheduler_requires_login(env) -> None:
    c = env
    for path in SCHEDULER_PATHS:
        assert c.get(path).status_code == 401, path
    assert c.post(TRIGGER_PATH).status_code == 401


# ======================================================================
# 二、登录了但不是管理员：403（**不是** 401，前端才不会跳登录页）
# ======================================================================

@pytest.mark.parametrize("tier", ["vip", "trial"])
def test_scheduler_rejects_non_admin(env, tier: str) -> None:
    c = env
    make_and_login(c, tier)
    for path in SCHEDULER_PATHS:
        r = c.get(path)
        assert r.status_code == 403, f"{tier} {path} → {r.status_code}"
    r = c.post(TRIGGER_PATH)
    assert r.status_code == 403, f"{tier} 手动触发 → {r.status_code}"


def test_admin_only_views_cannot_even_be_granted(env) -> None:
    """★ 核心断言：管理员专属页签**连"勾一下"这条路都没有了**。

    ## 这条测试的语义在 2026-09-25 变强了

    原版叫 `test_non_admin_can_never_see_the_tab_even_if_the_matrix_grants_it`，
    断言的是"矩阵里勾了也不下发"。用户口径：

        默认只有管理员有运行指标、调度管理的权限，不用加在功能权限设置的选项里
        写死默认管理员有这两个权限，其他用户都没这个权限且不可选择

    做法是把 `scheduler` / `metrics` 从 `FEATURES` 移出 —— 于是：
      · 权限矩阵**渲染不出这两行**（`feature_labels` 来自 `FEATURES`）；
      · 就算有人直接打接口想勾上，也会被**参数校验拒掉**（未知功能）。

    所以现在有两层，且第一层更硬：
      1. **配不出来**：`PUT .../tiers/trial` 带 `scheduler` → 400；
      2. **配了也不给**：`ADMIN_ONLY_VIEWS` 是规则，优先于套餐值；
      3. **给了也调不动**：路由挂 `require_admin`。
    """
    c = env
    make_and_login(c, "admin")

    # ① 配不出来：不再是"能勾但无效"，而是**直接拒绝**
    r = c.put("/api/v1/admin/platform/tiers/trial",
              json={"features": {"scheduler": True}})
    assert r.status_code == 400, (
        f"管理员专属功能本应拒绝配置，实际 {r.status_code}: {r.text[:200]}")
    assert "scheduler" in r.text, "错误信息里应指明是哪个 key 不认识"

    # 这两个 key 也不该出现在可配置列表里（前端据此渲染矩阵行）
    labels = c.get("/api/v1/admin/platform/tiers").json()
    plan_features = labels["tiers"][0].get("features", {})
    for gone in ("scheduler", "metrics"):
        assert gone not in plan_features, (
            f"{gone} 仍出现在权限矩阵里（应已从 FEATURES 移除）")
    c.post("/api/v1/auth/logout")

    # ② 普通用户拿不到页签，③ 接口也调不动
    make_and_login(c, "trial")
    body = c.get("/api/v1/me/features").json()
    assert body["is_admin"] is False
    for v in ("scheduler", "metrics"):
        assert v not in body["visible_views"], f"{v} 被下发给了试用用户"
    assert body["admin_only_views"] == ["metrics", "scheduler"]
    assert c.post(TRIGGER_PATH).status_code == 403
    assert c.get("/api/v1/scheduler/jobs").status_code == 403


def test_admin_still_sees_both_admin_only_tabs(env) -> None:
    """★ 反向断言：改成"写死管理员专属"之后，**管理员自己不能少了页签**。

    这是本次改动最容易踩的坑：`visible_views` 原来靠
    `enabled.get(feature)` 判断，而 `enabled` 是按 `FEATURES` 构造的 ——
    一旦把两项移出 `FEATURES`，那个判据恒为 False，**连管理员都看不到**
    「运行指标」和「调度管理」。所以必须单独锁一条管理员侧的用例。
    """
    c = env
    make_and_login(c, "admin")
    body = c.get("/api/v1/me/features").json()
    assert body["is_admin"] is True
    for v in ("scheduler", "metrics"):
        assert v in body["visible_views"], (
            f"管理员丢了 {v} 页签 —— 移出 FEATURES 时漏改了 visible 判据")


# ======================================================================
# 三、管理员：门槛要放行（否则"加门槛"会变成"谁都用不了"）
# ======================================================================

def test_admin_passes_the_gate(env) -> None:
    c = env
    make_and_login(c, "admin")
    body = c.get("/api/v1/scheduler/jobs").json()
    names = {j["name"] for j in body["jobs"]}
    assert {"snapshot_macro", "snapshot_industry_watchlist"} <= names
    assert c.get("/api/v1/scheduler/runs").status_code == 200
    assert c.get("/api/v1/scheduler/runs/summary").status_code == 200
    # 管理员 `visible_views` 里必须有它（否则是"管理员自己都进不去"）
    assert "scheduler" in c.get("/api/v1/me/features").json()["visible_views"]
    # 未注册作业名：过了门槛之后才 404（404 而非 401/403 证明门槛已放行）
    assert c.post("/api/v1/scheduler/jobs/__nope__/run").status_code == 404


def test_manual_run_weight_is_visible_to_the_frontend(env) -> None:
    """`snapshot_industry_watchlist` 的"一点 = 4 次完整研究图"必须**可被前端知道**。

    前端 `SchedulerPanel` 的确认弹窗读的就是 `kind` + `params.targets`
    （见 `runCostHint`）—— 这两个字段一旦不返回，确认框就会静默消失，
    "点一下跑 4 张图"又变成无提示的操作。
    """
    c = env
    make_and_login(c, "admin")
    jobs = {j["name"]: j for j in c.get("/api/v1/scheduler/jobs").json()["jobs"]}
    job = jobs["snapshot_industry_watchlist"]
    assert job["kind"] == "graph_snapshot"
    assert job["params"]["targets"] == ["半导体", "煤炭", "创新药", "白酒"]
