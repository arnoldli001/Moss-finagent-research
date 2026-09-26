"""事件告警保留期清理（"超过三天自动溢出删除"）回归测试。

## 需求原话（用户口径 2026-09-26）

> "事件告警的信息最多保留三天，超过3天的信息自动溢出删除。"

## 这条需求为什么需要两个机制（测试要同时守住两者）

| 机制 | 动作 | 触发 | 目的 |
|---|---|---|---|
| `expire_time` + `_expire_due` | `status='expired'` | **读路径**懒迁移 | 到期**不再显示** |
| `prune_alerts_before` | **`DELETE`** | 保留作业 | 数据**真正释放** |

改造前**只有前者**：`alert_expire_days=7` 把告警标成 `expired`，
但**行永远留在库里** —— `fact_alerts` 会无限增长。
表小的时候看不出来，攒够了就是查询变慢 + 库文件膨胀，
而且**不会有任何报错**（这正是"保留策略静默失效"的典型形态）。

所以这些用例守的是：
1. 超过保留期的告警**真的被删掉**（不是只改状态）；
2. 保留期内的**一条都不能少**；
3. 孤儿事件跟着删（否则 `list_unanalyzed_events` 会把它们当"未评估积压"
   反复捞出来）；
4. 空截止日必须**报错而不是删光全表**（最危险的失败方式）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.domain.alerts.models import Alert, Event
from src.infrastructure.repositories.event_sqlite_repo import EventSqliteRepository

TENANT = "tenant_001"


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _event(i: int, created: datetime) -> Event:
    return Event(
        event_id=f"ev{i}", event_key=f"k{i}", content_key=f"k{i}",
        event_type="policy", title=f"事件{i}", content="正文",
        source_name="测试源", source_url="https://example.invalid/1",
        publish_time=_iso(created), fetch_time=_iso(created),
        tenant_id=TENANT,
    )


def _alert(i: int, trigger: datetime, expire: datetime) -> Alert:
    return Alert(
        alert_id=f"al{i}", alert_key=f"k{i}:risk", content_key=f"k{i}:risk",
        event_id=f"ev{i}", alert_type="risk", alert_level="high",
        title=f"告警{i}", description="说明",
        risk_score=80.0, opportunity_score=10.0, confidence=0.9,
        source_name="测试源", event_publish_time=_iso(trigger),
        trigger_time=_iso(trigger), expire_time=_iso(expire),
        status="active", tenant_id=TENANT,
    )


@pytest.fixture
def repo(tmp_path):
    r = EventSqliteRepository(str(tmp_path / "alerts.db"))
    return r


async def _seed(repo, days_ago_list: tuple[int, ...]) -> None:
    """按"N 天前触发"造事件+告警各一条。"""
    now = datetime.now()
    events, alerts = [], []
    for i, days in enumerate(days_ago_list):
        created = now - timedelta(days=days)
        events.append(_event(i, created))
        alerts.append(_alert(i, created, created + timedelta(days=3)))
    await repo.ensure_schema()
    await repo.upsert_events(events)
    await repo.upsert_alerts(alerts)


async def _counts(repo) -> tuple[int, int]:
    events = await repo.list_events(limit=500, tenant_id=TENANT)
    alerts = await repo.list_alerts(limit=500, include_expired=True,
                                    tenant_id=TENANT)
    return len(alerts), len(events)


# ---------------------------------------------------------------- 核心行为

async def test_prunes_alerts_older_than_cutoff(repo):
    """超过保留期的告警必须被**真正删除**（不是只置 expired）。"""
    await _seed(repo, (5, 4, 3, 2, 1))
    before_alerts, _ = await _counts(repo)
    assert before_alerts == 5

    cutoff = _iso(datetime.now() - timedelta(days=3))
    result = await repo.prune_alerts_before(cutoff)

    assert result["alerts"] == 2, f"应删掉 5 天前/4 天前两条，实际 {result}"
    after_alerts, _ = await _counts(repo)
    assert after_alerts == 3, f"保留期内应剩 3 条，实际 {after_alerts}"


async def test_keeps_alerts_within_retention(repo):
    """保留期内的一条都不能少（这是"最多保留三天"的边界）。"""
    await _seed(repo, (2, 1))
    cutoff = _iso(datetime.now() - timedelta(days=3))
    result = await repo.prune_alerts_before(cutoff)
    assert result["alerts"] == 0, "3 天内的告警不该被删"
    alerts, _ = await _counts(repo)
    assert alerts == 2


async def test_orphan_events_are_removed_with_their_alerts(repo):
    """**超期**的孤儿事件必须跟着删 —— 否则会被当成"未评估积压"反复捞出来。

    这条守的是 `list_unanalyzed_events` 那条路径：它按 `analyzed=0` 取候选，
    孤儿事件永远满足条件，于是每轮扫描都在处理已经没有任何告警的死数据。
    """
    await _seed(repo, (5, 4))
    cutoff = _iso(datetime.now() - timedelta(days=3))
    result = await repo.prune_alerts_before(cutoff)
    assert result["alerts"] == 2, f"两条告警都超期，应删掉，实际 {result}"
    assert result["events"] == 2, f"超期孤儿事件应一起删，实际 {result}"


async def test_recent_orphan_event_is_kept(repo):
    """**未超期**的孤儿事件必须保留 —— 它可能还没轮到被分析。

    ⚠️ 这与上一条不矛盾，是本策略的刻意边界：
    采集与"生成告警"不是同一时刻（`list_unanalyzed_events` 是一轮轮跑的）。
    把 1 天前、还没产出告警的事件删掉，等于**丢掉一条还没被评估的线索**。
    所以事件侧同样按保留期（3 天）判，而不是"没告警就删"。
    """
    now = datetime.now()
    old = now - timedelta(days=5)          # 超期事件（有告警，会被一起删）
    recent = now - timedelta(days=1)       # 未超期事件（无告警，应保留）
    await repo.ensure_schema()
    await repo.upsert_events([_event(1, old), _event(2, recent)])
    await repo.upsert_alerts([_alert(1, old, old + timedelta(days=3))])

    cutoff = _iso(now - timedelta(days=3))
    result = await repo.prune_alerts_before(cutoff)

    assert result["alerts"] == 1
    assert result["events"] == 1, (
        f"只该删那条超期孤儿；未超期的 ev2 要留（它还没被分析），实际 {result}")
    _a, events = await _counts(repo)
    assert events == 1


async def test_shared_event_is_not_deleted(repo):
    """仍被其它告警引用的事件**不能**删（否则会删掉还在显示的告警的依据）。"""
    now = datetime.now()
    old = now - timedelta(days=5)
    await repo.ensure_schema()
    # 同一个 event 被两条告警引用：一条老、一条新
    await repo.upsert_events([_event(9, old)])
    await repo.upsert_alerts([
        _alert(9, old, old + timedelta(days=3)),
        Alert(
            alert_id="al9b", alert_key="k9:opportunity", content_key="k9:opportunity",
            event_id="ev9", alert_type="opportunity", alert_level="high",
            title="新告警", description="说明",
            risk_score=10.0, opportunity_score=85.0, confidence=0.9,
            trigger_time=_iso(now), expire_time=_iso(now + timedelta(days=3)),
            status="active", tenant_id=TENANT),
    ])
    cutoff = _iso(now - timedelta(days=3))
    result = await repo.prune_alerts_before(cutoff)
    assert result["alerts"] == 1, "只该删掉那条老的"
    assert result["events"] == 0, "事件仍被新告警引用，不该删"
    _a, events = await _counts(repo)
    assert events == 1


async def test_empty_cutoff_raises_instead_of_wiping_table(repo):
    """空截止日必须报错 —— 空 = 删光全表，这是最危险的失败方式。

    静默删光的话，用户看到的是"告警都没了"，而任何日志都不会说为什么。
    """
    await _seed(repo, (1,))
    with pytest.raises(ValueError):
        await repo.prune_alerts_before("")
    alerts, _ = await _counts(repo)
    assert alerts == 1, "报错后数据必须完好"


async def test_prune_is_idempotent(repo):
    """重复跑保留清理不应报错、也不该删掉保留期内的数据。"""
    await _seed(repo, (5, 1))
    cutoff = _iso(datetime.now() - timedelta(days=3))
    first = await repo.prune_alerts_before(cutoff)
    second = await repo.prune_alerts_before(cutoff)
    assert first["alerts"] == 1
    assert second["alerts"] == 0, "第二次不该再删到东西"
    alerts, _ = await _counts(repo)
    assert alerts == 1


# ---------------------------------------------------------------- 配置口径

def test_default_retention_is_three_days():
    """默认保留期必须是 **3 天**（用户口径："最多保留三天"）。

    守**两个**字段，因为它们是同一件事的两半，必须一起改：
      · `alert_expire_days`   → `expire_time`（读路径据此置 expired）
      · `retention_alert_days` → 真正 DELETE 的保留期（+ 级联 user_alert_read）
    只改一个的后果：改小前者 → 告警提前隐藏但数据还占着库；
    改小后者 → **删掉还没过期的告警**（用户会看到列表莫名少东西）。
    """
    from src.core.config import Settings

    expire = Settings.model_fields["alert_expire_days"].default
    retain = Settings.model_fields["retention_alert_days"].default
    assert expire == 3, f"alert_expire_days 默认应为 3，实际 {expire}"
    assert retain == 3, (
        f"retention_alert_days 默认应为 3（与 alert_expire_days 对齐），"
        f"实际 {retain}")
    assert retain >= expire, (
        "删除保留期不得短于过期期 —— 否则会删掉还没到期的告警")


async def test_retention_service_reports_alert_cutoff(repo, monkeypatch, tmp_path):
    """保留服务必须报出**告警截止日** —— 否则"清理跑没跑"在日志里看不见。

    保留是"静默失效"风险最高的一类功能：不清理不报错，
    只会某天发现库很大。所以截止日必须进结果字典。

    告警的**行数**由 `retention_passes` 的 `fact_alerts` 档统计
    （它带 `user_alert_read` 级联，见 `retention_service` 里的说明）；
    这里只守"截止日按 `retention_alert_days` 算出来了"。
    """
    import src.infrastructure.retention_service as R
    from src.core.config import Settings

    db = str(tmp_path / "alerts.db")
    r = EventSqliteRepository(db)
    await _seed(r, (5, 1))

    settings = Settings()
    monkeypatch.setattr(settings, "sqlite_path", db, raising=False)
    result = await R.run_retention(settings)

    assert "alerts_cutoff" in result
    assert result["alerts_cutoff"], "告警截止日不能为空"
    # 截止日应为「今天 - retention_alert_days」，且是合法 ISO 日期前缀
    from datetime import date

    expected = (date.today()
                - timedelta(days=int(settings.retention_alert_days))).isoformat()
    assert result["alerts_cutoff"].startswith(expected), (
        f"截止日应为 {expected} 起，实际 {result['alerts_cutoff']}")


async def test_retention_days_align_with_expire_days():
    """`retention_alert_days` 与 `alert_expire_days` 语义必须一致。

    两者取值不同时的**危险组合**是"删除期 < 过期期"：那会让一条**还没到期、
    列表上还该显示**的告警被物理删掉 —— 用户看到列表莫名少东西，
    而日志只会说"删除了 N 行"，完全联想不到配置。
    """
    from src.core.config import Settings

    s = Settings()
    assert s.retention_alert_days >= s.alert_expire_days, (
        f"删除保留期({s.retention_alert_days}) 不得短于 "
        f"过期期({s.alert_expire_days})")
