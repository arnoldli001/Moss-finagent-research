"""换源线索 → A19 既有管道的接线（`CHG-0127`）—— 离线、不花钱、确定性。

## 这份测试守的是什么

用户口径要的是「源停更 → 换源 … **找到后就更新数据源地址**」。在此之前，
path C 搜到的候选网址**终点只是人**（审计 + 管理员面板），**没有喂进任何自动管道**。
本模块把它接进**既有**的 A19 缺口管道（`AGENTS.md`：只调用既有入口，不另造一套）。

| 判据 | 防的失效 |
|---|---|
| ★ **网址真的到达 A19** —— 用假 resolver 接住 `drain_gaps`，断言 `resolve()` 收到的 `reason` 里含那个 URL | "我写进入队了" ≠ "A19 收得到"：中间隔着 `enqueue` 的截断与 `drain_gaps` 的取值方式 |
| 入队带 `source="path_c_search"` | 事后无法区分"线索送来的"与"A17 报的" |
| 同一批网址**只入队一次**（去重） | 每晚重复送给 A19 ⇒ 重复烧钱 |
| 无网址 / 队列坏掉 ⇒ **不抛、返回 False** | 换源是补救路径，不许被打挂 |
| 只带前 3 个网址 | 无限长 reason ⇒ 被截断后反而丢了关键信息 |
| **路由判据**：没有连接器的指标必须路由到 `resolver`（A19 的活） | 路由到 `prose`/`catalog` ⇒ A19 根本收不到，白入队 |
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.domain.agents.decision import gap_queue as GQ
from src.infrastructure.catalog import source_reroute as SR


@pytest.fixture()
def queue_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """把缺口队列指到临时目录（**绝不碰真实 data/gap_queue.jsonl**）。"""
    monkeypatch.setattr(GQ, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(GQ, "_SINGLETON", None, raising=False)
    q = GQ.GapQueue(root=tmp_path)
    monkeypatch.setattr(GQ, "get_gap_queue", lambda root=None: q)
    return q


def _hits(*urls: str) -> list[dict[str, str]]:
    return [{"url": u, "title": f"源{i}", "snippet": "", "site": ""}
            for i, u in enumerate(urls)]


# ─────────────── 入队本身 ───────────────
def test_lead_is_enqueued_with_url_in_reason(queue_tmp):
    ok = SR._enqueue_lead_for_a19("水泥熟料产能利用率",
                                  _hits("https://a.example/1", "https://b.example/2"))
    assert ok is True
    pend = queue_tmp.pending()
    assert len(pend) == 1
    e = pend[0]
    assert e.indicator == "水泥熟料产能利用率"
    assert "https://a.example/1" in e.reason, "网址没写进 reason —— A19 就什么都收不到"
    assert e.source == "path_c_search", "来源没标 ⇒ 事后分不清是谁送来的"


def test_second_call_with_same_urls_is_deduped(queue_tmp):
    """★ 同一批网址只入队一次 —— 否则每晚重复送给 A19（重复烧钱）。"""
    first = SR._enqueue_lead_for_a19("X", _hits("https://a.example/1"))
    second = SR._enqueue_lead_for_a19("X", _hits("https://a.example/1"))
    assert first is True and second is False, "去重失效"
    assert len(queue_tmp.pending()) == 1


def test_only_first_three_urls_are_carried(queue_tmp):
    SR._enqueue_lead_for_a19("Y", _hits(*[f"https://a.example/{i}" for i in range(8)]))
    e = queue_tmp.pending()[0]
    assert "https://a.example/0" in e.reason
    assert "https://a.example/3" not in e.reason, "带了超过 3 个网址（reason 会被截断）"


def test_no_urls_means_no_enqueue(queue_tmp):
    assert SR._enqueue_lead_for_a19("Z", []) is False
    assert SR._enqueue_lead_for_a19("Z", [{"title": "没有 url"}]) is False
    assert queue_tmp.pending() == []


def test_broken_queue_never_raises(monkeypatch):
    """队列坏了 ⇒ 返回 False，**不许**把换源链打挂。"""
    def boom(*_a, **_k):
        raise RuntimeError("队列炸了")

    monkeypatch.setattr(GQ, "get_gap_queue", boom)
    assert SR._enqueue_lead_for_a19("Q", _hits("https://a.example/1")) is False


# ─────────────── ★ 路由判据：必须真的到得了 A19 ───────────────
def test_no_connector_indicator_routes_to_resolver():
    """没有连接器的指标必须路由到 `resolver` —— 那是 A19 唯一会处理的出口。

    路由到 `prose` / `catalog` ⇒ **A19 根本收不到**，这条线就白接了
    （`drain_gaps` 只把 resolver 那一类送进去）。
    """
    v = GQ.route_gap("水泥熟料产能利用率",
                     is_registered=lambda _i: True,      # 登记过（否则判成散文）
                     has_connector=lambda _i: False)     # 但没有连接器
    assert v.route == GQ.ROUTE_RESOLVER, (
        f"路由成了 {v.route!r} ⇒ A19 收不到这条线索（判断依据：{v.reason}）")


def test_unregistered_lead_routes_to_prose_and_never_reaches_a19():
    """★ **前置条件**（本轮实测踩到）：线索要能被 A19 接手，指标必须**已登记**。

    实测（真实路由判据）：

      · `水泥熟料产能利用率`（**未登记**的自由串）→ `prose`
        —— 理由写得很准：「非指标形态（散文缺口）：A19 需要**可判定的指标名**，
        送进去只会白花一次 LLM 调用」；
      · `产权比率` / `ROE` / `ROA`（**已登记但无连接器**）→ `resolver` ✅

    这条判据把那个边界**记成机器事实**，免得下一个人（或下一轮的我）
    拿一个自由串去验，然后得出"这条线是空的"这种**错结论** ——
    本轮我就这么错过一次。

    ⚠️ 生产上这个前置条件**自然满足**：path C 只由
    `_try_reroute_on_source_lag()` 调用，而它的 `indicator` 来自 catalog 缺口取数器
    ⇒ **一定是已登记的**。自由串只会出现在手写探针里。
    """
    v = GQ.route_gap("水泥熟料产能利用率",
                     is_registered=lambda _i: False, has_connector=lambda _i: False)
    assert v.route == GQ.ROUTE_PROSE
    assert v.route != GQ.ROUTE_RESOLVER, "未登记的自由串被送进了 A19（白花 LLM 调用）"


# ─────────────── ★★ 契约判据：网址真的送进 A19 ───────────────
@pytest.mark.asyncio
async def test_the_url_actually_reaches_the_resolver(queue_tmp):
    """★★ **端到端契约**：走 `drain_gaps` 真跑一遍，断言假 resolver 收到的
    `reason` 里**含那个网址**。

    为什么必须有这一条：本判据的失败模式是"我写了入队，但 A19 拿不到" ——
    中间隔着 `enqueue` 的 200 字截断与 `drain_gaps` 的 `resolve(indicator, reason)`
    取值方式。只断言"入队成功"是**证明不了**这一点的。
    """
    SR._enqueue_lead_for_a19("水泥熟料产能利用率", _hits("https://data.ccement.com/x"))

    seen: list[tuple[str, str]] = []

    class _FakeResolver:
        async def resolve(self, indicator: str, reason: str):
            seen.append((indicator, reason))

            class _R:
                success = True
                error = ""

            return _R()

    out = await GQ.drain_gaps(
        _FakeResolver(), max_items=5,
        is_registered=lambda _i: True, has_connector=lambda _i: False)
    assert out["resolved"] == 1, f"没被 A19 处理：{out}"
    assert seen, "resolver 一次都没被调用"
    ind, reason = seen[0]
    assert ind == "水泥熟料产能利用率"
    assert "https://data.ccement.com/x" in reason, (
        f"A19 收到的 reason 里没有那个网址 ⇒ 线索没真正到达 A19：{reason!r}")


@pytest.mark.asyncio
async def test_discover_enqueues_each_time_it_finds_leads(queue_tmp, tmp_path,
                                                         monkeypatch):
    """`discover_candidate_urls()` 找到线索时**真的会**入队（接线点存在）。"""
    monkeypatch.setattr(SR, "_log_path", lambda root=None: tmp_path / "audit.jsonl")

    class _Out:
        ok = True
        served_by = "bocha"
        spent = True
        blocked_by = ""
        from_cache = False
        hits = _hits("https://ok.example/d")

    def fake_search(*_a, **_k):
        return _Out()

    _hits_, _note = await SR.discover_candidate_urls("某指标", search=fake_search,
                                                     root=tmp_path)
    pend = queue_tmp.pending()
    assert len(pend) == 1 and "https://ok.example/d" in pend[0].reason


def test_audit_record_still_says_not_actionable(queue_tmp):
    """反向：入队**不改变**审计里那条"线索不是可用源"的定性。"""
    src = Path("src/infrastructure/catalog/source_reroute.py").read_text(encoding="utf-8")
    assert '"actionable": False' in src, (
        "线索记录缺少 actionable=False —— 会被下一个人当成已经能用的源")
    #: 入队只是"送去评估"，不是"已经建成"
    assert "待 A19 判定能否建成连接器" in src
