"""做T推送的**当日节流**：冷却窗口 + 每票每日上限，防循环刷屏。

## 用户口径（2026-09-25，两次收敛）

> "昨天盘中的邮件提醒也是做T辅助重复刷屏，要修改成个股股价触及当日冲高线
>   或回踩线才通知，不是一直循环刷信息。"
>
> "可以设置成发出一次通知就静默半小时，半小时后再检测，
>   一天最多单个票发4次。"

## 被钉住的 bug

引擎的"触及"是**状态**不是**事件**（`engine.decide_signal`）：

    价格 ≤ 回踩线×(1+触及带宽)  →  triggered=True
    价格 ≥ 冲高线×(1-触及带宽)  →  triggered=True

只要价格**停在带内**，自选池每分钟重算就重新判一次 `triggered=True`。
原来唯一的闸门 `cooldown_minutes` 只活在进程内存里（`_last_push`），
重启即清空 —— 于是同一只票在线上趴一上午能刷出十几封一样的邮件。

## 本文件钉住的不变量

1. 冷却窗口内**不再通知**（哪怕价格又触及了一次）；
2. 冷却过后**可以**再通知（半小时后再检测 —— 不是"一天只发一次"）；
3. **每只票每日上限 4 次**，且按票计（回踩 + 冲高共享配额，不是各 4 次）；
4. 节流**跨进程重启**有效（落盘）；
5. 发送失败**不计数**（否则用户没收到，当天却已静默）。
"""

from __future__ import annotations

import asyncio
import datetime as _dt

import pytest

from src.intraday import notify_dedup
from src.intraday.config import IntradayConfig
from src.intraday.models import (
    DataHealth,
    IntradaySnapshot,
    NotifyResult,
    ScoreCard,
    TradeSignal,
)
from src.intraday.notifier import SignalNotifier

TODAY = "2026-09-24"
CODE = "688668"


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """把节流记录指到临时目录 + 把时钟钉到测试日。

    为什么必须隔离：节流是**按交易日落盘**的，若用真实路径
    （`data/cache/intraday/notified_signals.json`），跑一次测试就会往仓库里
    写入"2026-09-24 已通知 4 次"—— 而生产实例读的是同一个文件，
    等于测试会**污染真实推送状态**（用户当天可能因此收不到通知）。
    """
    path = tmp_path / "notified_signals.json"
    monkeypatch.setattr(notify_dedup, "DEFAULT_PATH", path)

    class _Frozen(_dt.datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003 与 datetime.now 签名一致
            return cls(2026, 9, 24, 10, 0)

    # `notifier._stale_guard` 与 `notify_dedup._cooldown_left` 都调 datetime.now
    monkeypatch.setattr("src.intraday.notifier.datetime", _Frozen)
    monkeypatch.setattr("src.intraday.notify_dedup.datetime", _Frozen)
    notify_dedup.reset()
    yield path
    notify_dedup.reset()


def _signal(*, kind: str = "low_buy", price: float = 9.50,
            ts: str = f"{TODAY} 10:00", strength: str = "solid",
            triggered: bool = True) -> TradeSignal:
    return TradeSignal(kind=kind, strength=strength, triggered=triggered,
                       price=price, ts=ts, total_score=36.0, reason="测试信号")


def _snapshot(signal: TradeSignal) -> IntradaySnapshot:
    return IntradaySnapshot(
        code=CODE, name="鼎通科技", trade_date=TODAY, signal=signal,
        scorecard=ScoreCard(total=36.0, threshold_action=30.0,
                            threshold_hint=20.0, zone="buy_zone",
                            verdict="", factors=[], weights_sum=100.0),
        health=DataHealth(stale=False, trade_date=TODAY),
    )


def _notifier(*, cooldown: int = 30, daily_max: int = 4) -> SignalNotifier:
    config = IntradayConfig().model_copy(deep=True)
    config.notify.cooldown_minutes = cooldown
    config.notify.daily_max_per_code = daily_max
    notifier = SignalNotifier(config)
    notifier._pending_code = CODE          # noqa: SLF001 节流键需要标的代码
    return notifier


def _notify(notifier: SignalNotifier, signal: TradeSignal) -> tuple[bool, str]:
    """走 should_push 并（在允许时）记账，模拟一次完整的"触及→通知"。"""
    ok, reason = notifier.should_push(signal, trade_date=TODAY)
    if ok:
        notify_dedup.mark_notified(trade_date=TODAY, code=CODE,
                                   kind=signal.kind, price=signal.price)
        notify_dedup.reset()        # 强制重读磁盘，模拟下一轮重算/重启
    return ok, reason


# ==================== 冷却窗口 ====================


def test_second_touch_within_cooldown_is_silenced() -> None:
    """★ 核心回归：冷却窗口内重复触及不得再发（原来这里会一直发）。"""
    signal = _signal()
    notifier = _notifier()
    ok, reason = _notify(notifier, signal)
    assert ok is True, reason

    # 冷却期内（未推进时钟）价格又触及了一次
    ok, reason = notifier.should_push(signal, trade_date=TODAY)
    assert ok is False, "冷却窗口内不得重复通知"
    assert "冷却中" in reason


def test_can_notify_again_after_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 冷却过后**可以**再通知 —— 用户要的是"半小时后再检测"，不是"一天一次"。

    做法：直接把"上次通知时间"写成 31 分钟前（而不是 sleep 半小时）。
    """
    signal = _signal()
    notifier = _notifier()
    assert _notify(notifier, signal)[0] is True

    # 把落盘记录里的时间戳回拨 31 分钟，模拟冷却已过
    thirty_one_ago = (_dt.datetime(2026, 9, 24, 10, 0)
                      - _dt.timedelta(minutes=31)).isoformat(timespec="seconds")
    monkeypatch.setattr(notify_dedup, "_now", lambda: thirty_one_ago)
    notify_dedup.mark_notified(trade_date=TODAY, code=CODE,
                               kind=signal.kind, price=signal.price)
    notify_dedup.reset()

    ok, reason = notifier.should_push(signal, trade_date=TODAY)
    assert ok is True, f"冷却已过应当能再通知：{reason}"


# ==================== 每票每日上限 ====================


def test_daily_cap_blocks_the_fifth_notification() -> None:
    """★ 一天最多 4 次：第 5 次要被上限拦下（而不是被冷却拦下）。"""
    for i in range(4):
        decision = notify_dedup.check(
            trade_date=TODAY, code=CODE, kind="low_buy", price=9.50,
            cooldown_minutes=0, daily_max=4)     # 冷却设 0，单测上限本身
        assert decision.allowed is True, f"第 {i + 1} 次应当允许：{decision.reason}"
        notify_dedup.mark_notified(trade_date=TODAY, code=CODE,
                                   kind="low_buy", price=9.50)
        notify_dedup.reset()

    decision = notify_dedup.check(
        trade_date=TODAY, code=CODE, kind="low_buy", price=9.50,
        cooldown_minutes=0, daily_max=4)
    assert decision.allowed is False, "第 5 次必须被每日上限拦下"
    assert "上限" in decision.reason


def test_daily_cap_is_per_code_not_per_direction() -> None:
    """★ 上限按**票**计：回踩与冲高共享 4 次配额（否则一只票最多 8 封）。

    用户说的是"一天最多单个票发4次" —— 若按方向分桶，一只票
    "回踩 4 次 + 冲高 4 次"就是 8 封，与口径不符。
    """
    for _ in range(4):
        notify_dedup.mark_notified(trade_date=TODAY, code=CODE,
                                   kind="low_buy", price=9.50)
        notify_dedup.reset()
    # 换成冲高方向，配额已用完 → 仍应被拦
    decision = notify_dedup.check(
        trade_date=TODAY, code=CODE, kind="high_sell", price=11.20,
        cooldown_minutes=0, daily_max=4)
    assert decision.allowed is False, "上限必须按票计，不能按方向各给 4 次"
    assert "上限" in decision.reason


def test_cap_is_reset_next_trading_day() -> None:
    """换一天配额重置（不是"永久 4 次"）。"""
    for _ in range(4):
        notify_dedup.mark_notified(trade_date=TODAY, code=CODE,
                                   kind="low_buy", price=9.50)
        notify_dedup.reset()
    tomorrow = "2026-09-25"
    decision = notify_dedup.check(
        trade_date=tomorrow, code=CODE, kind="low_buy", price=9.50,
        cooldown_minutes=30, daily_max=4)
    assert decision.allowed is True, decision.reason


def test_cap_disabled_by_kill_switch() -> None:
    """`daily_cap_enabled=false` 时两道闸门都不生效（仅排查用）。"""
    config = IntradayConfig().model_copy(deep=True)
    config.notify.daily_cap_enabled = False
    notifier = SignalNotifier(config)
    notifier._pending_code = CODE          # noqa: SLF001
    for _ in range(9):
        notify_dedup.mark_notified(trade_date=TODAY, code=CODE,
                                   kind="low_buy", price=9.50)
        notify_dedup.reset()
    ok, reason = notifier.should_push(_signal(), trade_date=TODAY)
    assert ok is True, reason


# ==================== 跨重启 ====================


def test_throttle_survives_process_restart() -> None:
    """★ 节流必须**跨进程重启**成立 —— 内存冷却字典重启即清空，靠它挡不住。"""
    signal = _signal()
    assert _notify(_notifier(), signal)[0] is True

    reborn = _notifier()          # 全新实例，内存冷却为空
    ok, reason = reborn.should_push(signal, trade_date=TODAY)
    assert ok is False, "重启后不得把仍在冷却期内的信号再发一遍"
    assert "冷却中" in reason


def test_different_code_has_its_own_quota() -> None:
    """上限是"每只票"，不是全局限额 —— 别的票不该被牵连。"""
    for _ in range(4):
        notify_dedup.mark_notified(trade_date=TODAY, code=CODE,
                                   kind="low_buy", price=9.50)
        notify_dedup.reset()
    decision = notify_dedup.check(
        trade_date=TODAY, code="600036", kind="low_buy", price=38.0,
        cooldown_minutes=30, daily_max=4)
    assert decision.allowed is True, decision.reason


# ==================== 重新武装（可选，默认关） ====================


def test_rearm_is_off_by_default() -> None:
    """默认不重新武装：行为严格等于"冷却 + 每日上限"（与用户口径逐字一致）。"""
    assert IntradayConfig().levels.notify_rearm_pct == 0.0, \
        "默认必须是 0（不重新武装）"
    decision = notify_dedup.check(
        trade_date=TODAY, code=CODE, kind="low_buy", price=9.99,
        cooldown_minutes=0, daily_max=4, rearm_pct=0.0)
    assert decision.allowed is True   # rearm=0 → 不因"价格没走远"而拦


def test_rearm_blocks_when_price_has_not_moved() -> None:
    """显式开启重新武装后：价格没走远 → 拦（防止趴在线上一整天机械发信）。"""
    notify_dedup.mark_notified(trade_date=TODAY, code=CODE,
                               kind="low_buy", price=9.50)
    notify_dedup.reset()
    decision = notify_dedup.check(
        trade_date=TODAY, code=CODE, kind="low_buy", price=9.505,
        cooldown_minutes=0, daily_max=4, rearm_pct=0.5)
    assert decision.allowed is False
    assert "同一档位区间" in decision.reason


def test_rearm_allows_after_price_moves_far() -> None:
    """显式开启后：价格走了足够远再触及 → 允许（这是新的一次机会）。"""
    notify_dedup.mark_notified(trade_date=TODAY, code=CODE,
                               kind="low_buy", price=9.50)
    notify_dedup.reset()
    decision = notify_dedup.check(
        trade_date=TODAY, code=CODE, kind="low_buy", price=9.60,   # +1.05%
        cooldown_minutes=0, daily_max=4, rearm_pct=0.5)
    assert decision.allowed is True, decision.reason


# ==================== 与发送路径的配合 ====================


def test_failed_send_does_not_count() -> None:
    """★ 全渠道失败**不计次** —— 否则用户一封信没收到，配额却被消耗掉。"""
    signal = _signal()
    notifier = _notifier()

    async def _dispatch(title: str, text: str):
        return []

    notifier._dispatch = _dispatch      # type: ignore[method-assign]  # noqa: SLF001
    _, pushed = asyncio.run(notifier.push(_snapshot(signal), signal,
                                          trade_date=TODAY))
    assert pushed is False
    assert notify_dedup.record_of(trade_date=TODAY, code=CODE)["count"] == 0, \
        "失败不能计数，否则当天会因配额被静默"


def test_successful_send_counts_and_throttles() -> None:
    """发送成功 → 计数 +1 → 冷却期内再触及被拦（端到端）。"""
    signal = _signal()
    notifier = _notifier()
    calls: list[str] = []

    async def _dispatch(title: str, text: str):
        calls.append(title)
        return [NotifyResult(channel="email", status="sent", detail="已发送")]

    notifier._dispatch = _dispatch      # type: ignore[method-assign]  # noqa: SLF001
    _, pushed = asyncio.run(notifier.push(_snapshot(signal), signal,
                                          trade_date=TODAY))
    assert pushed is True
    assert notify_dedup.record_of(trade_date=TODAY, code=CODE)["count"] == 1

    notify_dedup.reset()
    reborn = _notifier()
    ok, reason = reborn.should_push(signal, trade_date=TODAY)
    assert ok is False, reason
    assert len(calls) == 1, "不应有第二次发送"


# ==================== 存储本身 ====================


def test_store_is_atomic_and_prunes_old_days(_isolated_store) -> None:
    """落盘是原子替换（无残留临时文件），且只保留最近几天。"""
    path = _isolated_store
    for i in range(10):
        notify_dedup.mark_notified(trade_date=f"2026-09-{i + 1:02d}",
                                   code=CODE, kind="low_buy", price=9.5)
        notify_dedup.reset()
    assert list(path.parent.glob("*.tmp*")) == [], "不应留下临时文件"

    import json

    days = json.loads(path.read_text(encoding="utf-8"))["days"]
    assert len(days) <= notify_dedup.DEFAULT_KEEP_DAYS, "过期的日子应被裁掉"
    assert "2026-09-10" in days, "最新的日子必须保留"


def test_corrupt_store_fails_open(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """记录文件损坏 → 按"没有记录"处理（宁可多收一封，不能永久收不到）。"""
    path = tmp_path / "broken.json"
    path.write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setattr(notify_dedup, "DEFAULT_PATH", path)
    notify_dedup.reset()
    assert notify_dedup.check(
        trade_date=TODAY, code=CODE, kind="low_buy", price=9.5,
        cooldown_minutes=30, daily_max=4).allowed is True
    # 而且还能正常写入（自愈）
    notify_dedup.mark_notified(trade_date=TODAY, code=CODE,
                               kind="low_buy", price=9.5)
    notify_dedup.reset()
    assert notify_dedup.check(
        trade_date=TODAY, code=CODE, kind="low_buy", price=9.5,
        cooldown_minutes=30, daily_max=4).allowed is False


def test_missing_trade_date_does_not_block() -> None:
    """交易日判据缺失时不拦（fail-open）—— 拦错了等于功能静默失效。"""
    notifier = _notifier()
    ok, reason = notifier.should_push(_signal(ts=""), trade_date="")
    assert ok is True, reason
