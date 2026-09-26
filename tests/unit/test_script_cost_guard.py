"""高额脚本成本护栏：入口拒绝 + 跑到一半中止。

对应实现：`src/core/budget.py::ScriptCostGuard`，接入点
`scripts/mainline_relevance.py`、`scripts/purify_members.py`
（全项目**唯一**两个大额支出：实测 478.38 元 / 117.26 元）。

## 要钉死的四件事

1. 入口：**预计花费 > 1 元 → 一次调用都不发**（用户口径："直接禁用"）；
2. 入口：今日预算剩余不够本次预计 → 同样拒绝（别跑到一半被别处拒绝）；
3. 跑到一半：实际花费越过上限 → 抛 `ScriptCostError` 中止，**已落库的保留**；
4. 计数基线是"当日累计增量"，跨进程/跨日都不会算错。

`--no-llm`（零成本路径）与"没有待判定对"必须**不被拦** —— 否则护栏会
把"只想重算一遍选择"的正常操作也一起禁掉。
"""

from __future__ import annotations

import asyncio

import pytest

from src.core import budget as B


@pytest.fixture(autouse=True)
def _isolated_budget(monkeypatch):
    """每个用例一个干净账本（默认日预算 20 元，与线上口径一致）。"""
    B.reset_budget_for_test(daily=20.0)
    monkeypatch.delenv("MOSS_SCRIPT_COST_CAP_CNY", raising=False)
    yield
    B.reset_budget_for_test(daily=20.0)


def _spend(amount: float, source: str = "test") -> None:
    """往账本里记一笔真实消耗（按 deepseek 计价折算 token）。"""
    # 用 deepseek-flash 的输出价（4.0 元/百万 token）换算成 token 数：
    # 直接调 `record` 而不是改内部字段，走的才是真实计价路径。
    tokens = int(round(amount / 4.0 * 1_000_000))
    B.get_budget().record(provider="deepseek", model="deepseek-flash",
                          tokens_in=0, tokens_out=tokens, source=source)


# ======================================================================
# 一、入口判：预计花费
# ======================================================================

def test_entry_blocks_when_estimate_exceeds_the_cap() -> None:
    guard = B.ScriptCostGuard("mainline_relevance")
    assert guard.cap == 1.0
    # 全量（4996 只）≈ 100 元 —— 正是要禁掉的那件事
    with pytest.raises(B.ScriptCostError) as exc:
        guard.check_entry(B.estimate_script_cost(4996))
    msg = str(exc.value)
    assert "超过单次上限" in msg
    assert "已拒绝启动" in msg
    # 指引必须给出"怎么小批量试跑"与"怎么显式抬闸"两条路
    assert "--limit" in msg and "MOSS_SCRIPT_COST_CAP_CNY" in msg


def test_entry_allows_a_small_trial_run() -> None:
    """`--limit 40`（≈0.8 元）必须放行 —— 否则护栏把试跑也一起禁了。"""
    guard = B.ScriptCostGuard("mainline_relevance")
    guard.check_entry(B.estimate_script_cost(40))   # 0.8 元，不抛
    assert B.estimate_script_cost(40) == pytest.approx(0.8)
    # 边界：正好等于上限放行，超过才拦
    guard.check_entry(1.0)
    with pytest.raises(B.ScriptCostError):
        guard.check_entry(1.01)


def test_entry_blocks_when_daily_budget_is_not_enough() -> None:
    """今日只剩 0.3 元、本次预计 0.8 元 → 拒绝启动（不要跑到一半才崩）。"""
    B.reset_budget_for_test(daily=0.30)
    guard = B.ScriptCostGuard("mainline_relevance")
    with pytest.raises(B.ScriptCostError, match="预算剩余"):
        guard.check_entry(0.8)


def test_cap_zero_disables_the_guard(monkeypatch) -> None:
    """`MOSS_SCRIPT_COST_CAP_CNY=0` = 关掉护栏（应急抬闸，不是默认）。"""
    monkeypatch.setenv("MOSS_SCRIPT_COST_CAP_CNY", "0")
    guard = B.ScriptCostGuard("mainline_relevance")
    assert guard.enabled is False
    guard.check_entry(9999.0)        # 不抛
    guard.check_running()            # 不抛
    assert "已关闭" in guard.describe()


def test_cap_can_be_raised_by_env(monkeypatch) -> None:
    """抬闸抬的是"单次上限"；**日预算仍是一道独立的门**（20 元/天）。

    这两条必须分开：把单次上限抬到 500 并不等于允许一次花掉 500 ——
    账本自己还会按日预算拒绝（见 `test_entry_blocks_when_daily_budget_is_not_enough`）。
    """
    monkeypatch.setenv("MOSS_SCRIPT_COST_CAP_CNY", "500")
    guard = B.ScriptCostGuard("mainline_relevance")
    assert guard.cap == 500.0
    guard.check_entry(15.0)          # 抬闸后放行（且不超过当日剩余 20 元）
    with pytest.raises(B.ScriptCostError, match="预算剩余"):
        guard.check_entry(100.0)     # 单次上限过了，日预算仍然拦住


# ======================================================================
# 二、跑到一半：实际花费
# ======================================================================

def test_running_guard_trips_on_actual_spend() -> None:
    guard = B.ScriptCostGuard("mainline_member_pure")
    assert guard.spent() == 0.0
    assert guard.over_cap() is False
    guard.check_running()            # 没花钱 → 不抛

    _spend(0.6)
    assert guard.spent() == pytest.approx(0.6, abs=1e-6)
    assert guard.over_cap() is False, "还没到 1 元不该中止"
    guard.check_running()

    _spend(0.5)                      # 累计 1.1 元 > 1 元
    assert guard.over_cap() is True
    with pytest.raises(B.ScriptCostError) as exc:
        guard.check_running()
    assert "已主动中止" in str(exc.value)
    assert "不重复计费" in str(exc.value)


def test_baseline_is_the_delta_not_the_day_total() -> None:
    """基线取"启动时的当日累计"：**之前**别的调用方花的钱不该算到本次头上。

    否则一个已经花掉 19 元的账本会让护栏"一启动就中止"，
    而此时本次运行其实一分钱都还没花。
    """
    _spend(3.0, source="research")
    guard = B.ScriptCostGuard("mainline_relevance")
    assert guard.spent() == 0.0
    assert guard.over_cap() is False
    _spend(1.2)
    assert guard.spent() == pytest.approx(1.2, abs=1e-6)


# ======================================================================
# 三、接入点：真的会被评分循环调用
# ======================================================================

def test_score_stocks_stops_when_the_cost_guard_trips(tmp_path) -> None:
    """★ `relevance.score_stocks` 必须**在取任务之前**问护栏。

    用一个"第二次就超限"的假护栏：只有前一只真正调用了 LLM，
    之后必须抛 `ScriptCostError` 且**已经落库的那一只保留**。
    """
    from src.mainline.relevance import RelevanceStore, ScoreTask, score_stocks

    class _Gateway:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, tier, system, prompt, **kwargs):
            self.calls += 1

            class _Resp:
                content = '{"scores": [{"name": "A", "score": 90, "reason": "r"}]}'
                model_used = "fake"

            return _Resp()

    class _TripsAfterFirst:
        """第一次放行，之后一律超限（模拟"跑到第 2 只时越过 1 元"）。"""

        def __init__(self) -> None:
            self.seen = 0

        def over_cap(self) -> bool:
            self.seen += 1
            return self.seen > 1

        def check_running(self) -> None:
            if self.over_cap():
                raise B.ScriptCostError("本次已花 1.20 元，达到单次上限 1.00 元")

    store = RelevanceStore(tmp_path / "cache.db")
    store.connect().close()
    gateway = _Gateway()
    tasks = [ScoreTask(code=f"60000{i}", name="X", business="b", industry="i",
                       themes=["A"], corr={"A": 0.5}) for i in range(4)]
    with pytest.raises(B.ScriptCostError, match="单次上限"):
        asyncio.run(score_stocks(
            store=store, gateway=gateway, tasks=tasks, tier="reasoning",
            concurrency=1, stall_seconds=60, retries=0,
            cost_guard=_TripsAfterFirst()))
    # 中止前那只**已经落库**（重跑命中缓存、不重复计费）
    assert gateway.calls == 1, "中止后不应再发调用"
    conn = store.connect()
    try:
        rows = conn.execute("SELECT COUNT(*) FROM ml_stock_theme").fetchone()[0]
    finally:
        conn.close()
    assert rows >= 1, "中止前已完成的那只必须已落库"


def test_score_stocks_without_a_guard_is_unchanged(tmp_path) -> None:
    """不传 `cost_guard` 时行为与以前完全一致（不得隐式加拦）。"""
    from src.mainline.relevance import RelevanceStore, ScoreTask, score_stocks

    class _Gateway:
        async def complete(self, tier, system, prompt, **kwargs):
            class _Resp:
                content = '{"scores": [{"name": "A", "score": 90, "reason": "r"}]}'
                model_used = "fake"

            return _Resp()

    store = RelevanceStore(tmp_path / "cache.db")
    store.connect().close()
    tasks = [ScoreTask(code="600001", name="X", business="b", industry="i",
                       themes=["A"], corr={"A": 0.5})]
    stats = asyncio.run(score_stocks(
        store=store, gateway=_Gateway(), tasks=tasks, tier="reasoning",
        concurrency=1, stall_seconds=60, retries=0))
    assert stats.stocks_scored == 1
