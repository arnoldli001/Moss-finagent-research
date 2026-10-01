"""★ 缺口路由：每类缺口的**唯一出口**（2026-09-29 实测缺陷的护栏）。

## 触发它的现场（不是设想，是量出来的）

`gap_queue` 的**唯一消费者**是 `gap_drain` 作业 → `DataGapResolverAgent`（A19，
**LLM 生成连接器 + 沙箱验证**）。而实测该队列的 35 条 pending **全是 A17 的散文**：

```
个股行情与财务明细未提供，故不给任何个股结论或推荐。
北向资金日度净买额（2024-08-19起交易所停止披露），外资实时流向不可得。
两融深市余额（仅有沪市13505.1亿，2026-09-24）。
美国联邦基金利率及目标区间（上游显示为?%）
```

A19 拿不到**可判定的指标名** ⇒ 必然失败，而每次都真的花一次 LLM 调用；
`attempts=0` 说明**一条都没被成功补过**（全是白烧）。同一队列里还混着维护审计
报的**指标缺口**（`stock_close:600036` 这类）：源与连接器都在，只是**批采作业**
没跑到 ⇒ 出口是 `catalog_*`，也不该让 LLM 去写连接器。

## 本文件守的四件事

1. **散文缺口不进 A19**（省 LLM 钱，且不再淹没真缺口）；
2. **判据是形态/登记表，不是标点或长度**这类会漂移的启发式
   （实测反例：`美国联邦基金利率及目标区间（上游显示为?%）` **没有标点**）；
3. **不传判据时行为与旧版逐条一致**（"新参数不传 ⇒ 行为不变"的纪律）；
4. **取数侧判据只有一份实现**（`ConnectorRouter.supports()` == `_matched()`）。
"""

from __future__ import annotations

import pytest

from src.domain.agents.decision.gap_queue import (
    ROUTE_CATALOG,
    ROUTE_PROSE,
    ROUTE_RESOLVER,
    GapQueue,
    drain_gaps,
    looks_like_indicator_id,
    reset_gap_queue_for_test,
    route_entries,
    route_gap,
)

#: A17 真实报上来的散文缺口（**逐字抄自 `data/gap_queue.jsonl`**，不是编的）。
#: 它们是"分类错就全错"的反面样本：文本里有业务词、有数字、有的没标点。
REAL_PROSE_GAPS = [
    "个股行情与财务明细未提供，故不给任何个股结论或推荐。",
    "北向资金日度净买额（2024-08-19起交易所停止披露），外资实时流向不可得。",
    "两融深市余额（仅有沪市13505.1亿，2026-09-24）。",
    "限售解禁规模与家数。",
    "美国联邦基金利率及目标区间（上游显示为?%）",
    "招商银行个股财务明细、股息率、净息差与资产质量数据",
    "量能MA5/MA10/MA50数值单位异常（万亿级偏差），环比缩量幅度不可用。",
]


@pytest.fixture
def queue(tmp_path):
    """隔离的缺口队列（写 tmp_path，不碰生产 data/）。"""
    reset_gap_queue_for_test()
    q = GapQueue(root=tmp_path)
    yield q
    reset_gap_queue_for_test()


# ============================================================
# ① 形态判据：什么算"可判定的指标 id"
# ============================================================


@pytest.mark.parametrize("text", [
    "净息差:600036", "stock_close:600036", "解禁计划:300068",
    "fred:UNRATE", "fred:DGS10", "idx_val:000300",
])
def test_code_and_series_shapes_are_indicator_ids(text):
    """`X:Y`（6 位代码 / 全大写序列号）是指标 id。"""
    assert looks_like_indicator_id(text) is True


def test_registered_bare_name_is_an_indicator_id():
    """登记表里查得到的裸名（`社融`/`CPI`）也是指标 id ——判据**现读**登记表。"""
    registered = {"社融", "CPI", "us_unemployment"}
    assert looks_like_indicator_id("社融", is_registered=registered.__contains__)
    assert not looks_like_indicator_id("美国失业率怎么看", is_registered=registered.__contains__)


@pytest.mark.parametrize("text", REAL_PROSE_GAPS)
def test_real_prose_gaps_are_not_indicator_ids(text):
    """★ 真实散文缺口一条都不许被判成指标 id（判据写宽了就是白烧 LLM）。"""
    assert looks_like_indicator_id(text) is False
    assert looks_like_indicator_id(text, is_registered=lambda _i: False) is False


def test_no_sentence_punctuation_heuristic():
    """★ 判据不许用"有没有标点" —— 实测有反例（没标点的散文）。"""
    no_punct = "美国联邦基金利率及目标区间（上游显示为?%）"
    assert "，" not in no_punct and "。" not in no_punct
    assert looks_like_indicator_id(no_punct) is False, \
        "没有标点不等于是指标 id（这就是不能用标点当判据的原因）"


# ============================================================
# ② 路由：唯一出口
# ============================================================


def test_connector_hit_routes_to_catalog():
    """有连接器认它 ⇒ 出口是批采作业，**不是** A19。"""
    v = route_gap("stock_close:600036", is_registered=lambda _i: True,
                  has_connector=lambda i: i.startswith("stock_close"))
    assert v.route == ROUTE_CATALOG
    assert v.indicator == "stock_close:600036"


def test_registered_without_connector_routes_to_resolver():
    """已登记但没有生产者 ⇒ A19 的本职（它是"缺生产者"，不是"缺调度"）。"""
    v = route_gap("生息资产平均余额:600036", is_registered=lambda _i: True,
                  has_connector=lambda _i: False)
    assert v.route == ROUTE_RESOLVER


def test_unregistered_id_shaped_gap_routes_to_resolver():
    """没登记但形态是指标 id ⇒ 也可能是"该新登记/新连接器"，交给 A19。"""
    v = route_gap("ind:brand_new:all", is_registered=lambda _i: False,
                  has_connector=lambda _i: False)
    assert v.route == ROUTE_RESOLVER


@pytest.mark.parametrize("text", REAL_PROSE_GAPS)
def test_prose_never_reaches_a19(text):
    """★ 核心判据：散文缺口的出口是 `prose`，**绝不进 A19**。"""
    v = route_gap(text, is_registered=lambda _i: False, has_connector=lambda _i: True)
    assert v.route == ROUTE_PROSE, f"{text!r} 不该被送进 A19"
    assert v.indicator == ""
    assert "A19" in v.reason


def test_route_entries_groups_by_exit():
    """分组函数：三条不同类的缺口各归各的出口。"""
    class _E:
        def __init__(self, ind):
            self.indicator = ind

    entries = [_E("stock_close:600036"), _E("生息资产:600036"),
               _E("限售解禁规模与家数。")]
    groups = route_entries(
        entries, is_registered=lambda i: i.startswith("生息资产"),
        has_connector=lambda i: i.startswith("stock_close"))
    assert [e.indicator for e in groups[ROUTE_CATALOG]] == ["stock_close:600036"]
    assert [e.indicator for e in groups[ROUTE_RESOLVER]] == ["生息资产:600036"]
    assert [e.indicator for e in groups[ROUTE_PROSE]] == ["限售解禁规模与家数。"]


# ============================================================
# ③ 队列接口：向后兼容（不传判据 = 旧行为）
# ============================================================


def test_pending_resolvable_without_predicates_equals_pending(queue):
    """★ 不传判据时必须与 `pending()` **逐条一致**（否则是静默行为变更）。"""
    for ind in ("stock_close:600036", "限售解禁规模与家数。"):
        queue.enqueue(ind, reason="r")
    assert [e.indicator for e in queue.pending_resolvable()] == \
        [e.indicator for e in queue.pending()]


def test_pending_resolvable_filters_prose_and_catalog(queue):
    """传了判据：只留 resolver 那一类，且 `routing()` 报得出分布。"""
    queue.enqueue("stock_close:600036", reason="批采的活")
    queue.enqueue("生息资产:600036", reason="缺生产者")
    queue.enqueue("限售解禁规模与家数。", reason="散文")
    keep = queue.pending_resolvable(
        is_registered=lambda i: i.startswith("生息资产"),
        has_connector=lambda i: i.startswith("stock_close"))
    assert [e.indicator for e in keep] == ["生息资产:600036"]
    assert queue.routing(is_registered=lambda i: i.startswith("生息资产"),
                         has_connector=lambda i: i.startswith("stock_close")) == {
        ROUTE_CATALOG: 1, ROUTE_RESOLVER: 1, ROUTE_PROSE: 1}


# ============================================================
# ④ 消费执行器：A19 到底被调了几次（行为判据，不是"看着像"）
# ============================================================


class _FakeResolver:
    def __init__(self, ok: bool = True):
        self.calls: list[str] = []
        self._ok = ok

    async def resolve(self, indicator: str, reason: str = ""):
        self.calls.append(indicator)

        class _R:
            success = self._ok
            error = "" if self._ok else "假的失败"

        return _R()


@pytest.mark.asyncio
async def test_drain_skips_prose_and_catalog_entries(queue, monkeypatch):
    """★ 端到端行为判据：散文与批采两类**一次 A19 都不许调**。"""
    monkeypatch.setattr("src.domain.agents.decision.gap_queue.get_gap_queue",
                        lambda *a, **k: queue)
    queue.enqueue("stock_close:600036", reason="批采")
    queue.enqueue("生息资产:600036", reason="缺生产者")
    queue.enqueue("限售解禁规模与家数。", reason="散文")
    queue.enqueue("美国联邦基金利率及目标区间（上游显示为?%）", reason="散文")

    fake = _FakeResolver()
    out = await drain_gaps(
        fake, max_items=10, is_registered=lambda i: i.startswith("生息资产"),
        has_connector=lambda i: i.startswith("stock_close"))
    assert fake.calls == ["生息资产:600036"], "只该为'缺生产者'那一条花 LLM"
    assert out["resolved"] == 1
    assert out["by_route"][ROUTE_PROSE] == 2


@pytest.mark.asyncio
async def test_drain_without_predicates_keeps_old_behaviour(queue, monkeypatch):
    """★ 反向判据：不传判据时**所有 pending 都照旧送 A19**（不许悄悄改语义）。"""
    monkeypatch.setattr("src.domain.agents.decision.gap_queue.get_gap_queue",
                        lambda *a, **k: queue)
    queue.enqueue("stock_close:600036", reason="a")
    queue.enqueue("限售解禁规模与家数。", reason="b")
    fake = _FakeResolver()
    out = await drain_gaps(fake, max_items=10)
    assert len(fake.calls) == 2
    assert out["queued"] == 2


@pytest.mark.asyncio
async def test_broken_predicate_degrades_to_old_behaviour(queue, monkeypatch):
    """判据自己抛异常时不许把队列打挂（退化成旧行为 = 宁可多花钱也别不补数）。"""
    monkeypatch.setattr("src.domain.agents.decision.gap_queue.get_gap_queue",
                        lambda *a, **k: queue)
    queue.enqueue("stock_close:600036", reason="a")

    def _boom(_i):
        raise RuntimeError("判据坏了")

    fake = _FakeResolver()
    out = await drain_gaps(fake, max_items=10, is_registered=_boom,
                           has_connector=_boom)
    assert fake.calls == ["stock_close:600036"]
    assert out["resolved"] == 1


# ============================================================
# ⑤ 取数侧判据只有一份实现
# ============================================================


# ============================================================
# ⑥ 月频重试：**不放弃**也不白试（用户 2026-09-30 口径）
# ============================================================


def test_retry_later_parks_the_entry_without_giving_up(queue):
    """★ 推到 30 天后：立刻不再出现在 pending，但**不消耗** attempts。

    为什么不能靠 `skipped`：源一旦恢复（如商务部镜像重新更新），
    被 skipped 的缺口**再没有任何路径回到 pending**（永久放弃）。
    """
    from src.domain.agents.decision.gap_queue import SOURCE_LAG_RETRY_S

    queue.enqueue("社融", reason="源停更")
    entry = queue.pending()[0]
    queue.mark(entry, "failed", result="取到但与库内同期")
    queue.retry_later(entry, SOURCE_LAG_RETRY_S, result="源停更：下月再试")

    assert queue.pending() == [], "月频重试期内不该再被取出"
    stored = next(iter(queue._entries.values()))
    assert stored.status == "pending", "必须仍是 pending（不是 skipped）"
    assert stored.attempts == 0, "月频重试**不消耗**尝试次数"
    assert stored.next_try_ms > 0
    assert "源停更" in stored.result

    # 把时刻拨回去 ⇒ 必须重新出现（这就是"源恢复后能自愈"的机器保证）
    stored.next_try_ms = 0
    queue._persist()
    assert [e.indicator for e in queue.pending()] == ["社融"]


@pytest.mark.asyncio
async def test_drain_catalog_gaps_three_states(queue, monkeypatch):
    """★ 取数侧补采的三态各自落对：补到新期 / **源停更** / 取不到。

    中间那一态是回答"为什么不补"的关键：**取到了、就是这一期** ⇒
    机器可读地写成"源停更"，而不是笼统记一句"失败了"。
    """
    from src.domain.agents.decision.gap_queue import (
        ROUTE_CATALOG,
        drain_catalog_gaps,
    )

    monkeypatch.setattr("src.domain.agents.decision.gap_queue.get_gap_queue",
                        lambda *a, **k: queue)
    for ind in ("stock_close:600036", "CPI", "PPI"):
        queue.enqueue(ind, reason="r")

    async def _fetch(ind: str) -> dict:
        if ind == "stock_close:600036":
            return {"ok": True, "before": "2026-09-14", "after": "2026-09-29"}
        if ind == "CPI":
            return {"ok": True, "before": "2026-09-01", "after": "2026-09-01"}
        return {"ok": False, "before": "", "after": "", "reason": "源不可达"}

    out = await drain_catalog_gaps(
        _fetch, max_items=10, is_registered=lambda _i: True,
        has_connector=lambda _i: True)
    assert out["resolved"] == 1, "补到新期的那条应 resolved"
    assert out["failed"] == 2

    by_ind = {e.indicator: e for e in queue._entries.values()}
    assert by_ind["stock_close:600036"].status == "resolved"
    assert "补到新期 2026-09-29" in by_ind["stock_close:600036"].result
    assert by_ind["CPI"].status == "pending", "源停更 ⇒ 月频重试（仍是 pending）"
    assert "源停更" in by_ind["CPI"].result
    assert by_ind["PPI"].status == "pending"
    assert "源不可达" in by_ind["PPI"].result


@pytest.mark.asyncio
async def test_drain_catalog_gaps_ignores_prose_and_bounds_backlog(queue, monkeypatch):
    """散文**一次都不许进取数侧**；且单轮有上限（700 条会把盘后作业跑成几小时）。"""
    from src.domain.agents.decision.gap_queue import (
        ROUTE_CATALOG,
        drain_catalog_gaps,
    )

    monkeypatch.setattr("src.domain.agents.decision.gap_queue.get_gap_queue",
                        lambda *a, **k: queue)
    queue.enqueue("限售解禁规模与家数。", reason="散文")
    for i in range(5):
        queue.enqueue(f"ind:x{i}", reason="id")

    calls: list[str] = []

    async def _fetch(ind: str) -> dict:
        calls.append(ind)
        return {"ok": True, "before": "", "after": ""}

    out = await drain_catalog_gaps(
        _fetch, max_items=2, is_registered=lambda _i: False,
        has_connector=lambda _i: True)
    assert calls == ["ind:x0", "ind:x1"], f"应只取前 2 条，实际 {calls}"
    assert out["backlog"] == 5, "队列剩余必须报出来（不许静默截断）"
    assert ROUTE_CATALOG not in calls and "限售解禁规模与家数。" not in calls


def test_router_supports_equals_matched():
    from src.infrastructure.connectors.router import ConnectorRouter

    class _C:
        source_name = "fake"

        def __init__(self, prefix):
            self._prefix = prefix

        async def fetch(self, indicator, start_date=None, end_date=None):
            return []

        def get_capabilities(self):
            return {"indicators": [f"{self._prefix}:{{code}}"]}

        def supports(self, indicator):
            return str(indicator).startswith(self._prefix)

    router = ConnectorRouter([(_C("stock_close"), _C("stock_close").supports),
                              (_C("fred"), _C("fred").supports)])
    for ind in ("stock_close:600036", "fred:UNRATE", "生息资产:600036", ""):
        assert router.supports(ind) == bool(router._matched(ind)), ind
    assert router.supports("stock_close:600036") is True
    assert router.supports("生息资产:600036") is False
