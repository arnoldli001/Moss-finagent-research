"""自定义板块的**额度强制**（`CHG-0223`）。

## 为什么要有这个文件

额度早就定义好了，却**从没被调用过**：

- `src/domain/quota/service.py:41-48` 的 `TIER_QUOTAS` 写着
  `sector_limit=20 / sector_size_limit=200`（trial 为 5/50）；
- `check_new_pool(kind='sector')` / `check_add_stock(kind='sector')`
  也早就在 `user_pool_sqlite_repo.py` 里；
- 而板块的写路径（`src/api/routes/quant_select.py`）**一次都没调用它们** ——
  所以隔离之前是"无限额度"，隔离之后必须真接上。

## 本文件钉三件事

1. **边界**：第 N 个成功、第 N+1 个被拒（`pool_limit` / `pool_full`），
   且错误文案里**带上限数字**（"操作失败"这种文案没法核对）；
2. **上限只有一个来源**：额度随**套餐等级**变（trial 5 vs vip 6+ 能过），
   不是代码里另写的常数；
3. **被拒的那次不留半成品**：替换成分被拒时，原有成分必须**一只不少**
   （`_replace_members` 先校验、后删除）。
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
from src.infrastructure.repositories.user_pool_sqlite_repo import (
    PoolQuota,
    PoolValidationError,
)

#: 同 `test_quant_sector_ownership.py`：私有资产不在公开 checkout 里 ⇒ 显式 skip。
_PRIVATE_MODULE = (Path(__file__).resolve().parents[2]
                   / "src" / "quant" / "quant_select_repo.py")
if not _PRIVATE_MODULE.exists():  # pragma: no cover - 只在公开 checkout 走到
    pytest.skip("量化选股模块未随仓库发布（私有资产，见 .gitignore:74）",
                allow_module_level=True)

from src.quant.quant_select_repo import QuantSector, QuantSelectSqliteRepository  # noqa: E402

OWNER = "u_quota_owner"
PASSWORD = "SectorQuota#2026x"


# ======================================================================
# 一、仓储层：上限来自 PoolQuota（不在这里另写一份数字）
# ======================================================================

def _repo(tmp_path) -> QuantSelectSqliteRepository:
    return QuantSelectSqliteRepository(str(tmp_path / "quota.db"))


def test_sector_count_limit(tmp_path) -> None:
    repo = _repo(tmp_path)
    quota = PoolQuota(sector_limit=2, sector_size_limit=3)
    for name in ("一板", "二板"):
        asyncio.run(repo.upsert_sector(QuantSector(user_id=OWNER, name=name),
                                       quota=quota))
    with pytest.raises(PoolValidationError) as exc:
        asyncio.run(repo.upsert_sector(QuantSector(user_id=OWNER, name="三板"),
                                       quota=quota))
    assert exc.value.code == "pool_limit"
    assert "2" in exc.value.message, f"文案里没有上限数字：{exc.value.message}"
    assert "自定义板块" in exc.value.message


def test_updating_existing_sector_is_not_blocked_by_limit(tmp_path) -> None:
    """★ 上限只拦"新建"：已经到顶时**改自己已有的板块**必须照常成功。

    否则用户会遇到"板块满了连改个名字都不行" —— 那会让人以为功能坏了。
    """
    repo = _repo(tmp_path)
    quota = PoolQuota(sector_limit=1, sector_size_limit=10)
    first = asyncio.run(repo.upsert_sector(QuantSector(user_id=OWNER, name="唯一板"),
                                           quota=quota))
    again = asyncio.run(repo.upsert_sector(
        QuantSector(user_id=OWNER, name="唯一板", note="改名不改数量"), quota=quota))
    assert again.id == first.id
    assert asyncio.run(repo.list_sectors(user_id=OWNER))[0].note == "改名不改数量"


def test_member_size_limit(tmp_path) -> None:
    repo = _repo(tmp_path)
    quota = PoolQuota(sector_limit=5, sector_size_limit=2)
    sector = asyncio.run(repo.upsert_sector(
        QuantSector(user_id=OWNER, name="板",
                    members=[{"code": "600150"}, {"code": "300308"}]),
        quota=quota))
    assert sector.id > 0
    with pytest.raises(PoolValidationError) as exc:
        asyncio.run(repo.add_members(sector.id, OWNER, [{"code": "000001"}],
                                     quota=quota))
    assert exc.value.code == "pool_full"
    assert "2" in exc.value.message


def test_rejected_replace_keeps_existing_members(tmp_path) -> None:
    """★★ 被拒的替换**不能**把原有成分清空（先校验、后删除的顺序判据）。"""
    repo = _repo(tmp_path)
    quota = PoolQuota(sector_limit=5, sector_size_limit=2)
    sector = asyncio.run(repo.upsert_sector(
        QuantSector(user_id=OWNER, name="板",
                    members=[{"code": "600150"}, {"code": "300308"}]),
        quota=quota))
    with pytest.raises(PoolValidationError):
        asyncio.run(repo.set_members(sector.id, OWNER,
                                     [{"code": code} for code in
                                      ("600150", "300308", "000001")],
                                     quota=quota))
    members = asyncio.run(repo.list_sectors(user_id=OWNER))[0].members
    assert sorted(m["code"] for m in members) == ["300308", "600150"], \
        "被拒的那次把成分删掉了"


def test_no_quota_means_no_check_only_for_internal_callers(tmp_path) -> None:
    """`quota=None` = 不校验，**只给迁移/单测**用（路由一律传，见下节）。"""
    repo = _repo(tmp_path)
    for index in range(3):
        asyncio.run(repo.upsert_sector(
            QuantSector(user_id=OWNER, name=f"板{index}")))
    assert len(asyncio.run(repo.list_sectors(user_id=OWNER))) == 3


# ======================================================================
# 二、HTTP 层：额度由服务端按**套餐等级**强制
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

    app.state.runtime = SimpleNamespace(quant_select=QuantSelectService(
        repo=QuantSelectSqliteRepository(db_path),
        result_file=str(tmp_path / "latest.json")))
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


_ACCOUNTS: dict[str, str] = {}


def make_and_login(client: TestClient, name: str, *, tier: str) -> str:
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


def _create(client: TestClient, name: str, members: int = 0):
    return client.post("/api/v1/quant/sectors", json={
        "name": name,
        "members": [{"code": f"60{index:04d}"} for index in range(members)],
    })


def test_trial_sector_limit_is_enforced_over_http(client: TestClient) -> None:
    """试用档 `sector_limit=5`：第 6 个 → **422** 且文案含上限数字。"""
    make_and_login(client, "trial_user", tier="trial")
    for index in range(5):
        assert _create(client, f"板块{index}").status_code == 200
    rejected = _create(client, "第6个")
    assert rejected.status_code == 422, rejected.text[:200]
    detail = rejected.json()["detail"]
    assert detail["code"] == "pool_limit"
    assert "5" in detail["message"], detail
    assert len(client.get("/api/v1/quant/sectors").json()["sectors"]) == 5


def test_vip_gets_more_than_trial(client: TestClient) -> None:
    """★ 上限**随套餐变**（同一份代码、同一个端点）：vip 能建到 6 个以上。

    这条是"额度只有一处来源"的判据 —— 如果上限被写死在路由里，
    trial 与 vip 会得到同一个数字。
    """
    make_and_login(client, "vip_user", tier="vip")
    for index in range(6):
        assert _create(client, f"板块{index}").status_code == 200, index
    assert len(client.get("/api/v1/quant/sectors").json()["sectors"]) == 6
