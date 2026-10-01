"""搜索源**备用通路演练**（离线、确定性、不花钱）—— `CHG-0117`。

## 这份测试就是 R1 的机器化

`.trae/skills/backup-path-availability/SKILL.md` R1：每条备用必须有
**"断开主用"的演练**。本文件做的正是那件事：把主源打成各种坏法，
断言**备用真的顶上来**；再把两家都打断，断言**如实报错而不是静默空**。

判据全部是"**取到没取到**"，不是"看着像不像"。

## 为什么全部离线

搜索**每次调用都花钱**（博查一次性总量 1000 / 百度每天 100）。
本项目已因"单测打到真实网络"真实花过一次钱（见 PRD §19.33.7 自伤②），
所以这里一律用**注入的假 transport / 假 provider**，
并有一条**结构判据**确保跑完额度账本不变。
"""
from __future__ import annotations

from typing import Any

import pytest

from src.infrastructure.search import router
from src.infrastructure.search.bocha import SearchHit, SearchOutcome


def _ok(n: int = 1) -> SearchOutcome:
    return SearchOutcome(True, [SearchHit(title=f"源{i}", url=f"https://e{i}.example")
                                for i in range(n)], "假结果", spent=True)


def _fail(blocked_by: str) -> SearchOutcome:
    return SearchOutcome(False, reason=f"假的失败({blocked_by})", blocked_by=blocked_by)


class _FakeProvider:
    """可编排的假 provider：按顺序吐出预置结果，并记录被调用次数。"""

    def __init__(self, outcomes: list[SearchOutcome]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def web_search(self, query: str, *, count: int = 5, transport: Any = None):
        self.calls += 1
        if self.outcomes:
            return self.outcomes.pop(0)
        return _fail("budget")

    def budget_state(self) -> dict[str, Any]:
        return {"used_today": self.calls, "limit_today": 60, "exhausted": False}

    def api_key(self) -> str:
        return "fake"


@pytest.fixture()
def fakes(monkeypatch: pytest.MonkeyPatch):
    """把两家 provider 换成假货（**不联网、不花钱**）。"""
    made: dict[str, _FakeProvider] = {}

    def install(primary: list[SearchOutcome], backup: list[SearchOutcome]):
        made["bocha"] = _FakeProvider(primary)
        made["baidu"] = _FakeProvider(backup)
        monkeypatch.setattr(router, "_provider",
                            lambda name: made[name])
        return made

    return install


# ─────────────── 主源好的时候：备用**不该被调用**（省钱） ───────────────
def test_primary_ok_means_backup_is_not_touched(fakes):
    made = fakes([_ok(2)], [_ok(9)])
    out = router.web_search("q")
    assert out.ok and out.served_by == "bocha"
    assert len(out.hits) == 2
    assert made["baidu"].calls == 0, "主源可用却调了备用 —— 白花钱"


# ─────────────── ★ R1：主源各种坏法，备用必须顶上 ───────────────
@pytest.mark.parametrize("blocked", [
    "budget",          # 额度用尽（最可能真实发生的一种）
    "no_key",          # 凭据没配
    "config_error",    # 配置读取失败
    "http",            # 被服务端拒（如 403）
    "transport",       # 网络异常
    "parse",           # 返回体坏了
])
def test_backup_serves_when_primary_is_blocked(fakes, blocked):
    made = fakes([_fail(blocked)], [_ok(1)])
    out = router.web_search("q")
    assert out.ok is True, f"主源 {blocked} 时备用没顶上 —— 这条备用是装饰品"
    assert out.served_by == "baidu"
    assert made["baidu"].calls == 1


def test_two_providers_both_dead_is_reported_not_silent(fakes):
    """两家都挂 ⇒ **必须如实报**，不许静默返回空（那会像"网上没有"）。"""
    fakes([_fail("budget")], [_fail("transport")])
    out = router.web_search("q")
    assert out.ok is False
    assert out.blocked_by == "all_failed"
    #: 逐家明细必须都在 —— 否则"两家都没额度"与"两家都连不上"长得一样
    assert "bocha:budget" in out.reason and "baidu:transport" in out.reason


def test_provider_order_is_primary_then_backup(fakes):
    made = fakes([_fail("http")], [_ok(1)])
    router.web_search("q")
    assert made["bocha"].calls == 1 and made["baidu"].calls == 1
    assert router.PROVIDER_ORDER[0] == "bocha", "主源顺序变了却没改文档/测试"


def test_unknown_provider_name_does_not_crash_the_chain(fakes):
    """写错源名 ⇒ 记一条失败继续走，**不要**把整条链打挂。"""
    fakes([_ok(1)], [_ok(1)])
    out = router.web_search("q", providers=("nosuch", "bocha"))
    assert out.ok and out.served_by == "bocha"
    assert out.ok  # 前一家写错不该影响后一家


def test_status_reports_both_providers(fakes):
    fakes([], [])
    st = router.providers_status()
    assert [s["provider"] for s in st] == list(router.PROVIDER_ORDER)
    assert all("exhausted" in s for s in st)


# ─────────────── ★ 结构判据：跑完不许动真实额度账本 ───────────────
def test_router_never_touches_real_providers_when_faked(fakes, monkeypatch):
    """注入假 provider 后，**真实 provider 一次都不许被调用**。

    这是为"单测误花真钱"那次事故立的（PRD §19.33.7 自伤②）：
    判据要证明"测试够不到网络"，而不是"我记得打对了补丁"。
    """
    from src.infrastructure.search import baidu, bocha

    def _forbidden(*_a, **_k):
        raise AssertionError("单测触到了真实搜索 provider —— 会花真钱")

    monkeypatch.setattr(bocha, "web_search", _forbidden)
    monkeypatch.setattr(baidu, "web_search", _forbidden)

    fakes([_fail("budget")], [_ok(2)])
    out = router.web_search("q")
    assert out.ok and out.served_by == "baidu"
