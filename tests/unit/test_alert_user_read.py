"""告警**已读状态按用户隔离**的回归测试（仓储层）。

## 这条测试来自一次实测发现的漏洞

用户问："事件告警里的信息目录中，哪些是已读信息，**每个用户状态不一样**，
这个做租户隔离了吗？"

实测答案是**没有，连名义上的隔离都没有**：

  · `DEFAULT_TENANT = "tenant_001"` 是**硬编码常量**
  · 所有路由的 `tenant_id` 参数默认就是它，而前端**从不传** → 永远是同一个值
  · 已读写在 `fact_alerts.status` 这个**行级单值**上

pilot 库实测：47 条告警**全部** `tenant_id='tenant_001'`，而系统里有
13 个 vip 用户。于是**任何一个人点"已读"，全体 13 个人的未读角标一起清零**。
"每个用户状态不一样"这件事在数据模型里根本不存在。

## 修复后的语义

  · `fact_alerts.status` 保留，但只表示**告警自身的生命周期**
    （`active` / `read` / `expired`）—— 其中历史遗留的 `read` 在新语义下
    对**每个用户**都算未读（那是旧行为留下的，不是"这个人读过"）
  · `user_alert_read(user_id, alert_id)` 才是"**这个用户**读过了"
  · 未读 = 该租户未过期的告警 − 该用户在读表里的记录

## 为什么在仓储层测而不是走 HTTP

按用户隔离是**仓储层**的属性。HTTP 那条路要先把认证栈塞进 fixture，
而认证与隔离是两件事 —— 混在一起测，将来认证方式一变这条测试就红，
但隔离其实没坏。所以这里直接给两个 `user_id`，断言"A 标记不影响 B"。
"""

from __future__ import annotations

import pytest

from src.domain.alerts.models import Alert
from src.infrastructure.repositories.event_sqlite_repo import (
    EventSqliteRepository,
)

TENANT = "tenant_001"


def _alert(i: int, *, status: str = "active") -> Alert:
    return Alert(
        alert_id=f"al_{i:04d}",
        alert_key=f"key_{i}",
        event_id=f"evt_{i}",
        alert_type="risk",
        alert_level="high",
        title=f"测试告警 {i}",
        description="正文",
        risk_score=70.0,
        opportunity_score=10.0,
        confidence=0.8,
        affected_stocks=[],
        affected_industries=[],
        impact_path="",
        source_name="测试快讯",
        source_url="",
        event_publish_time="2026-09-25 08:00:00",
        trigger_time="2026-09-25 09:00:00",
        # 空 expire_time = 不过期，避免懒过期把判定搅乱
        expire_time="",
        status=status,
        tenant_id=TENANT,
        disclaimer="不构成投资建议",
    )


@pytest.fixture()
def repo(tmp_path) -> EventSqliteRepository:
    r = EventSqliteRepository(str(tmp_path / "evt.db"))
    import asyncio

    asyncio.run(r.ensure_schema())
    return r


@pytest.mark.asyncio
async def test_read_state_is_per_user(repo: EventSqliteRepository) -> None:
    """★ 核心断言：**A 标记已读不影响 B**。

    这正是修复前不可能成立的 —— 那时"已读"是告警行上的单值。
    """
    await repo.upsert_alerts([_alert(i) for i in range(5)])

    # 两个用户起始都看到 5 条未读
    assert await repo.count_unread(TENANT, user_id="userA") == 5
    assert await repo.count_unread(TENANT, user_id="userB") == 5

    assert await repo.mark_read("al_0000", TENANT, user_id="userA") is True

    assert await repo.count_unread(TENANT, user_id="userA") == 4
    assert await repo.count_unread(TENANT, user_id="userB") == 5, (
        "A 标记已读把 B 的未读数也改了 —— 隔离失败")


@pytest.mark.asyncio
async def test_mark_all_read_only_affects_caller(
        repo: EventSqliteRepository) -> None:
    """★ "全部已读"只清**调用者自己**的角标。

    修复前这是一条 `UPDATE fact_alerts SET status='read' WHERE tenant_id=?`
    —— 一个人点一下，13 个人的红点一起消失。
    """
    await repo.upsert_alerts([_alert(i) for i in range(6)])

    n = await repo.mark_all_read(TENANT, user_id="userA")
    assert n == 6
    assert await repo.count_unread(TENANT, user_id="userA") == 0
    assert await repo.count_unread(TENANT, user_id="userB") == 6, (
        "A 的\"全部已读\"清掉了 B 的未读")


@pytest.mark.asyncio
async def test_same_alert_has_different_status_for_different_users(
        repo: EventSqliteRepository) -> None:
    """同一条告警，在 A 眼里是 `read`、在 B 眼里是 `active`。

    这是"每个用户状态不一样"的字面要求 —— 也是修复前做不到的。
    """
    await repo.upsert_alerts([_alert(0)])
    await repo.mark_read("al_0000", TENANT, user_id="userA")

    a_read = await repo.list_alerts(status="read", tenant_id=TENANT,
                                    user_id="userA")
    b_read = await repo.list_alerts(status="read", tenant_id=TENANT,
                                    user_id="userB")
    a_unread = await repo.list_alerts(status="active", tenant_id=TENANT,
                                      user_id="userA")
    b_unread = await repo.list_alerts(status="active", tenant_id=TENANT,
                                      user_id="userB")

    assert [x.alert_id for x in a_read] == ["al_0000"]
    assert [x.status for x in a_read] == ["read"]
    assert b_read == [], "B 没有已读，却查到了已读列表"
    assert a_unread == [], "A 已读，却还在未读列表里"
    assert [x.alert_id for x in b_unread] == ["al_0000"]
    assert [x.status for x in b_unread] == ["active"]


@pytest.mark.asyncio
async def test_repeated_mark_read_is_idempotent(
        repo: EventSqliteRepository) -> None:
    """重复标记是**幂等成功**，不是失败。

    修复前的接口会返回 404「告警不存在或已读」—— 而用户只是又点了一次。
    把"你已经读过"这个**正常状态**说成错误，会让人以为系统有问题。
    """
    await repo.upsert_alerts([_alert(0)])
    assert await repo.mark_read("al_0000", TENANT, user_id="userA") is True
    assert await repo.mark_read("al_0000", TENANT, user_id="userA") is True
    assert await repo.mark_all_read(TENANT, user_id="userA") == 0


@pytest.mark.asyncio
async def test_unknown_alert_id_is_rejected(
        repo: EventSqliteRepository) -> None:
    """不存在的 alert_id 不写脏读记录（返回 False）。"""
    await repo.upsert_alerts([_alert(0)])
    assert await repo.mark_read("al_nope", TENANT, user_id="userA") is False
    assert await repo.count_unread(TENANT, user_id="userA") == 1


@pytest.mark.asyncio
async def test_legacy_global_read_rows_count_as_unread_for_everyone(
        repo: EventSqliteRepository) -> None:
    """旧行为留下的全局 `read` 行，对新语义下**每个用户**都是未读。

    ## 为什么这条必须测（实测踩到）

    pilot 库里有 8 条行级 `status='read'`（旧代码里某个人点过已读留下的）。
    写入路径若只认 `status='active'`，那 8 条就**谁也标不动** ——
    接口报"告警不存在或已读"，而用户看不出为什么。

    所以写入条件必须是 `status IN ('active','read')`。
    """
    await repo.upsert_alerts([_alert(0, status="read"), _alert(1)])

    # 对 A 来说两条都未读（他从没标记过）
    assert await repo.count_unread(TENANT, user_id="userA") == 2
    # 那条遗留 `read` 也能被正常标记
    assert await repo.mark_read("al_0000", TENANT, user_id="userA") is True
    assert await repo.count_unread(TENANT, user_id="userA") == 1


@pytest.mark.asyncio
async def test_empty_user_id_keeps_legacy_global_behaviour(
        repo: EventSqliteRepository) -> None:
    """`user_id` 为空时**退回旧的全局行为**（不假装隔离成功）。

    内部调用方与既有接口（未登录的测试客户端）依赖这条路径。
    这里锁住它，免得将来有人"顺手"把空 user_id 也按用户处理 ——
    那会让未登录请求看到 0 条未读（`r.user_id = ''` 永远匹配不上），
    红点静默失效。
    """
    await repo.upsert_alerts([_alert(i) for i in range(3)])
    assert await repo.count_unread(TENANT) == 3
    assert await repo.mark_all_read(TENANT) == 3
    assert await repo.count_unread(TENANT) == 0
