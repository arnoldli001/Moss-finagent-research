"""情报流 → 事件告警**试验副本**（`scripts/intel_copy_to_alerts.py`）的行为。

用户口径（看「情报中心 → 热点&研报小作文」之后）：

> "其数据复制一份到事件告警，我先看下效果，不行就删除实现。"

这是**一次性试验**，所以这个文件盯的不是"功能对不对"，而是三件会伤到
真实数据的事：

1. `--purge` 只能删 `intelcopy:` 那一批 —— 删多一条就是把用户的真告警删了；
2. `--dry-run` 必须**一行都不写** —— 它是"我先看下效果"的那条路，
   如果它也写库，用户就没有"先看看"的选项了；
3. 强制评估**不许编造** 理由（`impact_path` 只能是输入里已有的字符串），
   因为用户会拿它去原文里核对。

⚠️ 用 `tmp_dir`（仓库 conftest 的快速临时目录）而不是 `tmp_path`：
后者的清理机制在本机单次耗时 30~60s（见 `tests/conftest.py` 的说明）。
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

from src.domain.alerts.models import (
    DEFAULT_TENANT,
    Alert,
    AlertLevel,
    AlertType,
    Event,
    EventType,
)
from src.domain.alerts.repository import EventRepository
from src.domain.alerts.thresholds import AlertEngine
from src.infrastructure.repositories.event_sqlite_repo import (
    EventSqliteRepository,
)


def _load_script_module():
    """按**文件路径**加载脚本（`scripts/` 不是包，没有 `__init__.py`）。

    ⚠️ 加载本身安全：脚本的 `src.*` 导入里只有纯领域层
    （`alert_bridge`），决定库路径的 `Settings()` 只在 `_main()` 里构造，
    而本文件所有用例都注入 `repo`/`feed_items`，**从不调 `_main()`**。
    """
    root = Path(__file__).resolve().parents[2]
    path = root / "scripts" / "intel_copy_to_alerts.py"
    spec = importlib.util.spec_from_file_location("intel_copy_to_alerts", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


trial = _load_script_module()


# ======================================================================
# 夹具：真 SQLite（临时库），**绝不连生产库**
# ======================================================================

@pytest.fixture
async def repo(tmp_dir):
    r = EventSqliteRepository(os.path.join(tmp_dir, "trial.db"))
    await r.ensure_schema()
    yield r
    await r.close()


def _alert(key: str, *, tenant: str = DEFAULT_TENANT) -> Alert:
    return Alert(
        alert_id=f"al_{key}", alert_key=key, content_key=f"ck_{key}",
        event_id=f"evt_{key}", alert_type=AlertType.RISK,
        alert_level=AlertLevel.HIGH, title=f"标题-{key}", description="描述",
        risk_score=90.0, opportunity_score=0.0, confidence=0.9,
        trigger_time="2026-10-02T10:00:00+08:00",
        expire_time="2099-01-01T00:00:00+08:00",
        tenant_id=tenant,
    )


def _event(key: str, *, tenant: str = DEFAULT_TENANT) -> Event:
    return Event(
        event_id=f"evt_{key}", event_key=key, event_type=EventType.STOCK,
        title=f"事件-{key}", source_name="测试源", fetch_time="2026-10-02T10:00:00+08:00",
        tenant_id=tenant,
    )


async def _counts(db_path: str) -> tuple[int, int]:
    """(告警数, 事件数) —— 直接开**只读**连接数，不给任何写机会。"""
    import sqlite3

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        alerts = conn.execute("SELECT COUNT(*) FROM fact_alerts").fetchone()[0]
        events = conn.execute("SELECT COUNT(*) FROM fact_events").fetchone()[0]
    finally:
        conn.close()
    return int(alerts), int(events)


def _null_out():
    """丢弃输出的 sink（`run` 的 `out` 只要求有 `write`）。"""

    class _Null:
        def write(self, text: str) -> int:
            return len(text)

    return _Null()


def _capture_out():
    """收集输出的 sink（返回 `(write目标, 行列表)`）。"""
    lines: list[str] = []

    class _Sink:
        def write(self, text: str) -> int:
            lines.append(text)
            return len(text)

    return _Sink(), lines


# ======================================================================
# 1) `delete_alerts`：前缀删除的边界（删多一条 = 删了用户的真数据）
# ======================================================================

@pytest.mark.asyncio
async def test_delete_alerts_only_removes_the_prefix(repo, tmp_dir):
    """只删 `intelcopy:`，**真实告警一条都不能少**。"""
    await repo.upsert_events([
        _event("intelcopy:a"), _event("intelcopy:b"), _event("real:1"),
    ])
    await repo.upsert_alerts([
        _alert("intelcopy:a:risk"), _alert("intelcopy:b:risk"),
        _alert("real:1:risk"), _alert("newswire:2:risk"),
    ])
    before = await repo.list_alerts(include_expired=True)
    assert len(before) == 4

    removed = await repo.delete_alerts(trial.PREFIX)

    assert removed == {"alerts": 2, "events": 2}
    left = {a.alert_key for a in await repo.list_alerts(include_expired=True)}
    assert left == {"real:1:risk", "newswire:2:risk"}
    # 事件侧同前缀一起删（否则会留下"未评估的孤儿事件"被下轮重新捞出来）
    assert {e.event_key for e in await repo.list_events()} == {"real:1"}
    # 复核数据库文件本身，确认不是 list_* 的过滤把它藏起来了
    assert await _counts(os.path.join(tmp_dir, "trial.db")) == (2, 1)


@pytest.mark.asyncio
async def test_delete_alerts_prefix_is_literal_not_a_like_pattern(repo):
    """前缀里的 `%` / `_` **不是通配符** —— 这正是用 `instr` 不用 `LIKE` 的原因。

    造两个"邻居"来压这条规则：

        alert_key = "intelcopyX…"    前缀差一个字符（`X` ≠ `:`）
        alert_key = "intel_copy…"    `_` 若被当成"任意单字符"就会被误删

    用 `LIKE 'intelcopy:%'` 时 `intel_copy:risk` **不会**命中（`_` 只匹配一个
    字符，而 `:` 得对上 `y`），但 `LIKE 'intel_copy:%'` 会命中
    `intelXcopy:risk` —— 也就是说错误取决于前缀，一旦前缀含这两个字符就崩。
    断言"两个字面邻居都不被删"能同时挡掉这两种写法。
    """
    neighbours = ["intelcopyX:risk", "intel_copy:risk", "intelcopy:0:risk"]
    await repo.upsert_alerts([_alert(k) for k in neighbours])
    await repo.upsert_events([_event("intelcopy:0"), _event("intel_copy")])

    removed = await repo.delete_alerts(trial.PREFIX)

    assert removed["alerts"] == 1
    left = {a.alert_key for a in await repo.list_alerts(include_expired=True)}
    assert left == {"intelcopyX:risk", "intel_copy:risk"}
    # 事件侧同样按字面判：`intel_copy` 不许被 `intelcopy:` 吃掉
    assert {e.event_key for e in await repo.list_events()} == {"intel_copy"}


@pytest.mark.asyncio
async def test_delete_alerts_works_for_a_non_bmp_prefix(repo):
    """非 BMP 字符（emoji）的前缀也要删得掉。

    这条专门压"不要用 `substr(alert_key, 1, len(prefix))`"：`len()` 数的是
    Python 码点，而 SQLite 的 `substr` 按**字符**数算，BMP 之外的字符
    两边对不上（前缀里有 emoji 时长度会算多），表现是 `substr(...) = ?`
    **静默一条都删不掉**、返回 `{"alerts": 0}` —— 而调用方会以为清理完成。
    本实现用 `instr(x, p) = 1`，不依赖任何长度计算。
    """
    prefix = "试验🚀:"
    await repo.upsert_alerts([
        _alert(f"{prefix}a:risk"), _alert(f"{prefix}b:risk"),
        _alert("试验:risk"),
    ])

    removed = await repo.delete_alerts(prefix)

    assert removed["alerts"] == 2
    assert [a.alert_key for a in await repo.list_alerts(include_expired=True)] \
        == ["试验:risk"]


@pytest.mark.asyncio
async def test_delete_alerts_supports_a_prefix_that_needs_like_escaping(repo):
    """反过来验一次：前缀**本身**含 `_` 时也要按字面工作。

    用 `LIKE` 的实现在这里必然多删（`_` 变成"任意单字符"），
    而 `instr(x, p) = 1` 的实现只认字面。
    """
    await repo.upsert_alerts([
        _alert("intel_copy:risk"), _alert("intelXcopy:risk"),
        _alert("real:risk"),
    ])

    removed = await repo.delete_alerts("intel_copy:")

    assert removed["alerts"] == 1
    left = {a.alert_key for a in await repo.list_alerts(include_expired=True)}
    assert left == {"intelXcopy:risk", "real:risk"}


@pytest.mark.asyncio
async def test_delete_alerts_is_tenant_scoped(repo):
    """按租户隔离：别的租户的同前缀数据不许被顺手删掉。"""
    await repo.upsert_alerts([
        _alert("intelcopy:a:risk"),
        _alert("intelcopy:b:risk", tenant="tenant_other"),
    ])
    removed = await repo.delete_alerts(trial.PREFIX)
    assert removed == {"alerts": 1, "events": 0}
    other = await repo.list_alerts(include_expired=True, tenant_id="tenant_other")
    assert [a.alert_key for a in other] == ["intelcopy:b:risk"]


@pytest.mark.asyncio
async def test_delete_alerts_rejects_empty_prefix(repo):
    """空前缀 = 删光该租户全部告警；宁可报错也不执行。"""
    await repo.upsert_alerts([_alert("real:risk")])
    with pytest.raises(ValueError):
        await repo.delete_alerts("")
    assert len(await repo.list_alerts(include_expired=True)) == 1


@pytest.mark.asyncio
async def test_port_default_delete_alerts_raises_not_silently_noops():
    """**假仓储不会静默无操作**。

    `EventRepository.delete_alerts` 默认实现抛 `NotImplementedError`
    （不是 `@abstractmethod`：那会把 `tests/unit/test_alert_service.py` 的
    `FakeRepo` 和未来的 postgres 实现一起打挂）。但"默认放行"也不行 ——
    一个返回 `{"alerts": 0}` 的默认实现会让回滚脚本报告"清理完成"，
    而数据一条没少。
    """

    class _MinimalRepo(EventRepository):
        async def ensure_schema(self) -> None: ...

        async def upsert_events(self, events): return {}

        async def list_events(self, event_type=None, limit=100,
                              tenant_id=DEFAULT_TENANT): return []

        async def existing_event_keys(self, event_keys,
                                      tenant_id=DEFAULT_TENANT): return set()

        async def list_unanalyzed_events(self, limit=100,
                                         tenant_id=DEFAULT_TENANT): return []

        async def mark_events_analyzed(self, event_ids,
                                       tenant_id=DEFAULT_TENANT): return 0

        async def upsert_alerts(self, alerts): return {}

        async def list_alerts(self, alert_type=None, alert_level=None,
                              status=None, limit=100, include_expired=False,
                              tenant_id=DEFAULT_TENANT): return []

        async def get_alert(self, alert_id, tenant_id=DEFAULT_TENANT): return None

        async def mark_read(self, alert_id, tenant_id=DEFAULT_TENANT): return False

        async def mark_all_read(self, tenant_id=DEFAULT_TENANT): return 0

        async def count_unread(self, tenant_id=DEFAULT_TENANT): return 0

        async def last_alert_time(self, alert_key,
                                  tenant_id=DEFAULT_TENANT): return None

        async def last_content_alert_time(self, content_key,
                                          tenant_id=DEFAULT_TENANT): return None

    # 能不实现就实例化 → 证明它**不是** abstractmethod
    fake = _MinimalRepo()
    with pytest.raises(NotImplementedError):
        await fake.delete_alerts(trial.PREFIX)


# ======================================================================
# 2) 强制评估：方向映射 + 理由**逐字来自输入**
# ======================================================================

def _item(tone: str, score: int, *, title: str = "某公司公告重大合同",
          published_at: str = "2026-10-02 10:00:00", chash: str = "h1",
          summary: str = "正文" * 40, explain: str = "词表计数（多2/空0）",
          phrases=("订单", "上调"), industries=("第三代半导体",),
          stocks=({"name": "天岳先进", "code": "688234", "count": 2},),
          codes=("688234",), people=("孙潇雅",), kind: str = "research_note",
          alias: str = "research-note-zsxq") -> dict:
    """真实形态的情报条目（`service.build_feed` 出来的那种形状）。

    ⚠️ 默认是**知识星球**（`kind=research_note`）—— 它在桥里**不过信度闸门**
    （`BYPASS_SOURCES`），所以"低信度不出告警"这类用例必须显式传
    `kind="newswire"`：知识星球 58 分会被桥强制抬到 80 照样出告警。
    """
    bucket: dict = {"industries": list(industries), "stocks": list(stocks),
                    "boards": []}
    return {
        "title": title, "summary": summary, "published_at": published_at,
        "source_alias": alias, "content_hash": chash,
        "kind": kind, "codes": list(codes), "industry": "",
        "credibility": {"score": score},
        "analysts": list(people), "institutions": ["中泰证券"],
        "tone": {
            "tone": tone, "has_tone": tone in ("偏多", "偏空"),
            "neutral": tone == "中性", "phrases": list(phrases),
            "explain": explain, "confidence": 0.8, "source": "rules",
            "events": [], "bullish": dict(bucket) if tone != "偏空" else {},
            "bearish": dict(bucket) if tone == "偏空" else {},
        },
    }


def test_forced_assessment_maps_bull_to_opportunity_and_bear_to_risk():
    """偏多 → opportunity，偏空 → risk（分数取可信度，不抬分）。"""
    bull = trial._force_assessment(_item("偏多", 95), include_undetermined=False)
    bear = trial._force_assessment(_item("偏空", 85), include_undetermined=False)

    assert bull is not None and bear is not None
    assert bull.sentiment == "positive"
    assert bull.risk_score == 0.0 and bull.opportunity_score == 95.0
    assert abs(bull.confidence - 0.95) < 1e-9
    assert bear.sentiment == "negative"
    assert bear.risk_score == 85.0 and bear.opportunity_score == 0.0
    # 不抬分：58 分就是 58 分、0.58 置信度 —— 引擎会如实把它挡在门槛外
    low = trial._force_assessment(_item("偏多", 58), include_undetermined=False)
    assert low is not None and low.opportunity_score == 58.0
    assert abs(low.confidence - 0.58) < 1e-9


def test_forced_assessment_direction_also_drives_industries_and_stocks():
    """行业取**方向桶**、个股复用桥的 `stock_entries`（个股优先）。"""
    bull = trial._force_assessment(_item("偏多", 95), include_undetermined=False)
    bear = trial._force_assessment(_item("偏空", 95), include_undetermined=False)
    assert bull is not None and bear is not None
    assert bull.affected_industries == ["第三代半导体"]
    assert bear.affected_industries == ["第三代半导体"]
    assert [s.name for s in bull.affected_stocks] == ["天岳先进"]
    assert bull.affected_stocks[0].code == "688234"


def test_forced_impact_path_is_only_concatenated_from_the_input():
    """`impact_path` **没有编造**：拆开必须逐字还原成输入里的字符串。

    这是本文件最重要的一条断言。`impact_path` 是用户唯一能回答
    "这条为什么进来"的字段，它一旦变成模型/脚本自己归纳的措辞，
    用户拿去原文就核对不到 —— 而那种错**不会报错**，只会悄悄误导。
    """
    explain = "词表计数（多2/空0）"
    phrases = ["订单", "上调"]
    item = _item("偏多", 95, explain=explain, phrases=tuple(phrases),
                 people=("孙潇雅",))
    assessment = trial._force_assessment(item, include_undetermined=False)
    assert assessment is not None

    path = assessment.impact_path
    assert explain in path
    for word in phrases:
        assert word in path
    assert "孙潇雅" in path and "中泰证券" in path
    # 逐字还原：整条理由 = 我们写的那几个固定前缀 + 输入里的字符串，
    # **没有任何第四种来源**（编造的因果链必然在这里露出来）。
    expected = (f"倾向判定：{explain}；"
                f"命中词：{'、'.join(phrases)}；"
                "命中的人/机构：孙潇雅、中泰证券")
    assert path == expected


def test_forced_assessment_marks_undetermined_in_the_text():
    """未定/中性：`--include-undetermined` 才收，且必须**写明**它是未定。"""
    item = _item("未定", 95, industries=(), stocks=(), codes=())
    assert trial._force_assessment(item, include_undetermined=False) is None

    kept = trial._force_assessment(item, include_undetermined=True)
    assert kept is not None
    assert kept.sentiment == "positive"       # 归机会侧（保守，报"利空"更伤）
    assert kept.opportunity_score == 95.0
    assert trial.UNDETERMINED_FLAG in kept.impact_path
    assert trial.UNDETERMINED_FLAG in kept.summary


def test_forced_summary_flags_a_missing_stock():
    """无标的必须明说（与桥同一条纪律：别让用户以为"系统找到了票"）。"""
    from src.domain.intel import alert_bridge

    item = _item("偏多", 95, stocks=(), codes=())
    assessment = trial._force_assessment(item, include_undetermined=False)
    assert assessment is not None
    assert assessment.affected_stocks == []
    assert alert_bridge.NO_STOCK_FLAG in assessment.summary


# ======================================================================
# 3) 前缀换名：试验数据不许污染真实告警的冷却表
# ======================================================================

def test_event_keys_are_namespaced_and_content_key_is_renamed():
    """`event_key` / `event_id` / `content_key` 全部带 `intelcopy:` 前缀。

    `content_key` 必须**独立换名**：真实告警的跨源同文冷却按它查
    （`repo.last_content_alert_time`）。沿用情报流原值，这批试验数据就会
    在 24h 内**抑制真实告警** —— 试验的副作用跑到生产上去了。
    """
    from src.domain.intel import alert_bridge

    item = _item("偏多", 95)
    pair = trial._force_pair(item, include_undetermined=False)
    assert pair is not None
    event, assessment = pair
    assert event.event_key == f"{trial.PREFIX}h1"
    assert event.event_id == "intelcopy_h1"
    assert event.content_key.startswith(trial.PREFIX)
    assert event.content_key != alert_bridge.build_event(item).content_key
    assert assessment.event_id == event.event_id
    # 脱敏**不在**脚本里做（桥也不做，出接口时才做），源名原样透传
    assert event.source_name == item["source_alias"]


def test_alert_key_derived_from_the_event_key_keeps_the_prefix():
    """`alert_key` = `event_key:type` —— 前缀能传导到告警表，`--purge` 才成立。"""
    engine = AlertEngine()
    item = _item("偏空", 95)
    pair = trial._force_pair(item, include_undetermined=False)
    assert pair is not None
    event, assessment = pair
    alert = engine.evaluate(event, assessment)
    assert alert is not None
    assert alert.alert_key.startswith(trial.PREFIX)
    assert alert.alert_type == AlertType.RISK
    assert alert.content_key.startswith(trial.PREFIX)


# ======================================================================
# 4) 端到端：写入 / dry-run / purge
# ======================================================================

@pytest.mark.asyncio
async def test_run_writes_alerts_and_purge_removes_them(repo, tmp_dir):
    """真写一遍 + `--purge` 删干净，且**不动**真实告警。"""
    await repo.upsert_alerts([_alert("real:1:risk")])
    sink, out = _capture_out()

    summary = await trial.run(
        target="dev", limit=10, feed_items=[
            _item("偏多", 95, chash="a"),
            _item("偏空", 85, chash="b"),
            _item("未定", 95, chash="c"),          # 未开开关 → 跳过
        ],
        repo=repo, engine=AlertEngine(), out=sink)

    assert summary["candidates"] == 2
    assert summary["events"] == {"inserted": 2, "skipped": 0}
    assert summary["alerts"] == {"inserted": 2, "skipped": 0}
    assert summary["skipped"] == {"未定/中性（未开 --include-undetermined）": 1}
    keys = {a.alert_key for a in await repo.list_alerts(include_expired=True)}
    assert keys == {"real:1:risk",
                    f"{trial.PREFIX}a:opportunity", f"{trial.PREFIX}b:risk"}
    # 幂等：同一批再跑一次不会重复写
    again = await trial.run(target="dev", limit=10, feed_items=[
        _item("偏多", 95, chash="a"), _item("偏空", 85, chash="b"),
    ], repo=repo, engine=AlertEngine(), out=_null_out())
    assert again["events"]["inserted"] == 0
    assert again["alerts"]["inserted"] == 0

    purged = await trial.run(target="dev", purge=True, repo=repo,
                             out=_null_out())
    assert purged["purge"] == {"alerts": 2, "events": 2}
    assert {a.alert_key for a in await repo.list_alerts(include_expired=True)} \
        == {"real:1:risk"}
    # 输出里必须有"怎么删"的提醒（用户只看到这一次输出）
    assert any("--purge" in text for text in out)


@pytest.mark.asyncio
async def test_dry_run_writes_nothing_at_all(repo, tmp_dir):
    """`--dry-run` **一行都不写**（靠只读连接数行数证明）。"""
    await repo.upsert_alerts([_alert("real:1:risk")])
    db = os.path.join(tmp_dir, "trial.db")
    before = await _counts(db)
    sink, out = _capture_out()

    summary = await trial.run(
        target="pilot", limit=10, dry_run=True,
        feed_items=[_item("偏多", 95, chash="a"), _item("偏空", 85, chash="b")],
        repo=repo, engine=AlertEngine(), out=sink)

    assert summary["dry_run"] is True and summary["candidates"] == 2
    assert await _counts(db) == before == (1, 0)
    assert any("没有写库" in text for text in out)
    assert any("--purge" in text for text in out)


@pytest.mark.asyncio
async def test_run_skips_undetermined_by_default_but_keeps_them_when_asked(repo):
    """开关语义：默认不收未定；打开后才收（并在理由里写明是未定）。"""
    items = [_item("未定", 95, chash="u", industries=(), stocks=(), codes=())]
    default = await trial.run(target="dev", feed_items=items, repo=repo,
                              engine=AlertEngine(), out=_null_out())
    assert default["candidates"] == 0

    kept = await trial.run(
        target="dev", feed_items=items, include_undetermined=True, repo=repo,
        engine=AlertEngine(), out=_null_out())
    assert kept["candidates"] == 1
    alerts = await repo.list_alerts(include_expired=True)
    assert len(alerts) == 1
    assert trial.UNDETERMINED_FLAG in alerts[0].impact_path
    assert alerts[0].opportunity_score == 95.0


# ======================================================================
# 5) 引擎门槛如实挡下低信度（不抬分的后果要看得见）
# ======================================================================

@pytest.mark.asyncio
async def test_low_credibility_entries_are_reported_as_engine_skipped(repo):
    """58 分的条目**能进候选**但引擎不出告警 —— 输出必须如实计数。

    桥对知识星球强制抬到 80；试验副本不抬分，所以 58 分会被
    `alert_confidence_min`（0.70）挡下。这不是 bug，是"不伪造信号强度"
    的代价，但它必须**出现在输出里**，否则用户看到的只是"告警比预期少"。

    ⚠️ 必须用**非旁路来源**（`kind="newswire"`）：知识星球的信度闸门是
    旁路的（`alert_bridge.BYPASS_SOURCES`），它 58 分也会被桥抬到 80
    照常出告警 —— 拿它测"低信度被挡"会测出完全相反的结论。
    """
    summary = await trial.run(
        target="dev",
        feed_items=[_item("偏多", 58, chash="low", kind="newswire",
                          alias="newswire-cls", people=())],
        repo=repo, engine=AlertEngine(), out=_null_out())
    assert summary["candidates"] == 1
    assert summary["alerts"]["inserted"] == 0
    assert summary["engine_skipped"] == 1
    assert await repo.list_alerts(include_expired=True) == []


@pytest.mark.asyncio
async def test_knowledge_planet_58_still_alerts_because_the_bridge_forces_the_score(repo):
    """对照组：**知识星球** 58 分照样出告警（桥把它抬到 80）。

    这条用例的作用是**把上一条的边界钉住**：如果哪次改动让"低信度一律不出
    告警"，这里会红 —— 而那个改动会直接打挂用户 2026-09-25 确认过的口径
    （"知识星球不看置信度"）。两条成对存在，才说明脚本没有偷偷改闸门。
    """
    summary = await trial.run(
        target="dev", feed_items=[_item("偏多", 58, chash="zsxq")], repo=repo,
        engine=AlertEngine(), out=_null_out())
    assert summary["candidates"] == 1
    assert summary["engine_skipped"] == 0
    alerts = await repo.list_alerts(include_expired=True)
    assert len(alerts) == 1
    assert alerts[0].opportunity_score == 80.0    # 桥的 FORCED_SCORE，不是 58
