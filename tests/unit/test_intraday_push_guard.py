"""做T推送闸门：**节假日/上一交易日的数据不得推送**。

## 为什么单独钉一个文件（2026-09-25 中秋事故）

2026-09-25 是周五但**休市**（中秋，9/25–9/27 休市）。用户当天持续收到做T提醒邮件，
现象容易被误判成"发送滞后"，实际根因是**假期没有当日分时**：

1. 数据链回落到上一交易日（9/24）的**完整**分时（最后一根 bar 是 15:00）；
2. 快照照常打分，引擎是确定性的 —— 同一份输入必然重算出同一批正式信号
   （实测 9/24 收盘那 4 只：301136 / 300300 / 688668 / 301689 全是 solid）；
3. `service.snapshot()` **已经正确识别**了这件事（`stale = trade_date != 今天`
   在节假日必然为真），但当时它只把提示写进 `health.gaps` 当 UI 文案，
   **没有拦推送** —— 信号时间戳甚至明写着 `2026-09-24 15:00`；
4. 冷却窗口是"限流"不是"闸门"：30 分钟一到就能再发，进程重启还会清空冷却；
5. 叠加飞书 webhook 失败（`19022 Ip Not Allowed`）→ 每个信号回落成邮件。

于是本文件钉住两件事，防止这个洞再被打开：

- **闸门一**：`SignalNotifier.should_push()` 在数据非当日时必须拒绝推送（主力防线）；
- **闸门二**：非交易日不该做自选池整表重算（那是一条不停产生信号的源头），
  以及推送点上"时点不该推"的兜底判据。

## ⚠️ 闸门二里刻意的取舍：**不能用 `is_trading_day()` 当推送闸门**

修复过程中实测踩到的坑：`auto_select.is_trading_day()` 的语义是"市场时钟取不到 /
没推进 → 按交易日处理"，反过来说**时钟停在上一交易日它就判 False**。
而"时钟停在上一交易日"在**真交易日的 09:15~09:30 集合竞价期**同样成立
（当天第一笔成交前 tick 的 timetag 不会前进）—— 于是：

| 场景 | 真交易日 09:20 | 节假日 09:20 |
|---|---|---|
| `is_trading_day()` | False | False |

两者返回值完全相同，**区分不了**。所以推送闸门走 `_push_allowed_now()`
（周末 + 15:05 盘后这两个无歧义判据），数据归属交给闸门一。
本文件对"竞价期必须放行"有专门的正向用例。

## 时间怎么控制

`notifier.py` / `service.py` 都写的 `from datetime import datetime`，
所以直接 monkeypatch 模块上的 `datetime` 名字（冻成一个子类），
而不是去改系统时间 —— 与 `test_mainline_etf_share_guard.py` 同一手法。
信号/数据里的日期**一律由"今天"推导**（如"昨天"），
这样用例不依赖运行当天的真实日期，日历推进也不会突然变红。
"""

from __future__ import annotations

import datetime as _dt
import inspect

import pytest

from src.intraday import service as service_module
from src.intraday.config import IntradayConfig
from src.intraday.models import (
    DataHealth,
    IntradaySnapshot,
    ScoreCard,
    TradeSignal,
)
from src.intraday.notifier import SignalNotifier

#: 冻结时刻：2026-09-24（周四）10:00 —— "今天"是交易日、且盘中
NOW = (2026, 9, 24, 10, 0)
#: "今天"与"昨天"的字符串形式，供构造数据用
TODAY = "2026-09-24"
YESTERDAY = "2026-09-23"


def _frozen_datetime_class(moment: tuple[int, ...] = NOW):
    """一个 `datetime.now()` 恒返回 `moment` 的子类。"""

    class _Frozen(_dt.datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003 与 datetime.now 签名保持一致
            return cls(*moment)

    return _Frozen


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 notifier 与 service 里的 `datetime` 一起冻住（两个模块各自 import 了名字）。"""
    frozen = _frozen_datetime_class()
    monkeypatch.setattr("src.intraday.notifier.datetime", frozen)
    monkeypatch.setattr(service_module, "datetime", frozen)


@pytest.fixture
def freeze_at(monkeypatch: pytest.MonkeyPatch):
    """按需冻结到任意时刻：`freeze_at(2026, 9, 26, 10, 0)`。"""

    def _freeze(*moment: int) -> None:
        frozen = _frozen_datetime_class(tuple(moment))
        monkeypatch.setattr("src.intraday.notifier.datetime", frozen)
        monkeypatch.setattr(service_module, "datetime", frozen)

    return _freeze


def _signal(*, ts: str = f"{TODAY} 10:00", kind: str = "low_buy",
            strength: str = "solid", triggered: bool = True,
            total_score: float = 36.72) -> TradeSignal:
    return TradeSignal(kind=kind, strength=strength, triggered=triggered,
                       price=10.0, ts=ts, total_score=total_score, reason="测试信号")


def _snapshot(*, stale: bool, trade_date: str = TODAY,
              signal: TradeSignal | None = None) -> IntradaySnapshot:
    """带 `health` 的快照（`stale` 是闸门一的判据来源）。"""
    return IntradaySnapshot(
        code="688668", name="鼎通科技", trade_date=trade_date, signal=signal,
        scorecard=ScoreCard(total=36.72, threshold_action=30.0,
                            threshold_hint=20.0, zone="buy_zone",
                            verdict="", factors=[], weights_sum=100.0),
        health=DataHealth(stale=stale, trade_date=trade_date),
    )


def _notifier(*, cooldown_minutes: int = 30) -> SignalNotifier:
    config = IntradayConfig()
    # `load_intraday_config` 返回的是进程内同一个对象，直接改会泄漏给别的用例
    config = config.model_copy(deep=True)
    config.notify.cooldown_minutes = cooldown_minutes
    notifier = SignalNotifier(config)
    notifier._pending_code = "688668"  # noqa: SLF001 冷却键需要标的代码前缀
    return notifier


# ==================== 闸门一：数据非当日 → 不推送 ====================


def test_stale_data_is_never_pushed(frozen_clock: None) -> None:
    """★ 核心回归：节假日拿上一交易日数据算出的正式信号，必须被拒绝。

    这正是 2026-09-25 事故的直接原因 —— 修复前这里是 `True`。
    """
    signal = _signal(ts=f"{YESTERDAY} 15:00")
    snapshot = _snapshot(stale=True, trade_date=YESTERDAY, signal=signal)
    ok, reason = _notifier().should_push(signal, snapshot=snapshot)
    assert ok is False, "上一交易日的信号不得推送"
    assert "非当日" in reason
    # 原因里要带上信号所属日期，用户一眼能看出"这封邮件讲的是昨天"
    assert YESTERDAY in reason


def test_fresh_data_is_still_pushed(frozen_clock: None) -> None:
    """反向用例：当日新鲜数据必须照常推送（别把闸门做成"一律不推"）。"""
    signal = _signal(ts=f"{TODAY} 10:00")
    snapshot = _snapshot(stale=False, trade_date=TODAY, signal=signal)
    ok, reason = _notifier().should_push(signal, snapshot=snapshot)
    assert ok is True, reason


def test_signal_day_mismatch_blocks_even_without_stale_flag(
        frozen_clock: None) -> None:
    """兜底判据：`health.stale` 万一为假（旧版缓存/字段缺失），信号时间戳仍能拦住。"""
    signal = _signal(ts=f"{YESTERDAY} 15:00")
    snapshot = _snapshot(stale=False, trade_date=YESTERDAY, signal=signal)
    ok, reason = _notifier().should_push(signal, snapshot=snapshot)
    assert ok is False
    assert "非当日" in reason


def test_should_push_without_snapshot_keeps_old_semantics(frozen_clock: None) -> None:
    """不传 snapshot 时保持原语义（既有调用方与测试不受影响）。"""
    ok, _ = _notifier().should_push(_signal(ts=f"{YESTERDAY} 15:00"))
    assert ok is True


def test_stale_check_runs_before_cooldown(frozen_clock: None) -> None:
    """闸门必须先于冷却判定：冷却只负责"少发几次"，不能把非当日数据放行。"""
    signal = _signal(ts=f"{YESTERDAY} 15:00")
    snapshot = _snapshot(stale=True, trade_date=YESTERDAY, signal=signal)
    notifier = _notifier()
    ok, reason = notifier.should_push(signal, snapshot=snapshot)
    assert ok is False
    assert "冷却" not in reason          # 拒绝理由是数据归属，不是限流
    # 且**不能**因为被拒就把冷却键写脏（否则修复当天还会压掉正常信号）
    assert not notifier._last_push       # noqa: SLF001


def test_push_marks_nothing_when_stale(frozen_clock: None) -> None:
    """`push()` 走完整路径时同样不推、不发、不写冷却。"""
    import asyncio

    signal = _signal(ts=f"{YESTERDAY} 15:00")
    snapshot = _snapshot(stale=True, trade_date=YESTERDAY, signal=signal)
    notifier = _notifier()

    sent: list[str] = []

    async def _fake_dispatch(title: str, text: str):
        sent.append(title)
        return []

    notifier._dispatch = _fake_dispatch      # type: ignore[method-assign]  # noqa: SLF001
    results, pushed = asyncio.run(notifier.push(snapshot, signal))
    assert pushed is False
    assert sent == [], "被闸门拦下时不得触达任何发送通道（含邮件回落）"
    assert results[0].status == "suppressed"
    assert not notifier._last_push            # noqa: SLF001


# ==================== 闸门二：非交易日不做整表重算 ====================


def test_refresh_helper_passes_through_trading_day(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.intraday.auto_select.is_trading_day",
                        lambda moment=None: True)
    assert service_module._is_trading_day_for_refresh() is True  # noqa: SLF001


def test_refresh_helper_passes_through_holiday(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.intraday.auto_select.is_trading_day",
                        lambda moment=None: False)
    assert service_module._is_trading_day_for_refresh() is False  # noqa: SLF001


def test_refresh_helper_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """判定本身炸了要放行：误判成休市会让正常交易日**漏掉整表刷新**，
    代价远大于多跑一轮（有缓存、成本可控）。"""
    def _boom(moment=None):
        raise RuntimeError("clock down")

    monkeypatch.setattr("src.intraday.auto_select.is_trading_day", _boom)
    assert service_module._is_trading_day_for_refresh() is True  # noqa: SLF001


def test_refresh_loop_skips_recompute_on_holiday(
        monkeypatch: pytest.MonkeyPatch,
        freeze_at) -> None:
    """★ 核心回归：非交易日不得触发整表重算（那是信号的源头）。

    跑法：让循环的"睡到下一个整分"在第 2 次调用时抛异常终止循环，
    这样正好观察完第 1 轮 —— 它必须既不重算，也不碰数据源。

    时间锚定 10:00：避开 09:15-09:30 的 in_call_auction 窗口
    （2026-09-28 用户口径：集合竞价撮合后要立刻刷新；该窗口由放行路径处理，
    本测试只盯节假日闸门）。
    """
    import asyncio

    freeze_at(2026, 9, 28, 10, 0)          # 周一 10:00 — 已开盘+非竞价期

    computed: list[int] = []
    slept = 0

    async def _fake_sleep(seconds, *args, **kwargs):
        nonlocal slept
        slept += 1
        if slept >= 2:
            raise KeyboardInterrupt      # 结束这个无限循环（非 CancelledError）
        return None

    class _Loop(service_module.IntradayService):
        def __init__(self) -> None:
            super().__init__()
            self._startup_grace_seconds = 0.0

        async def watchlist(self, **kwargs):     # type: ignore[override]
            computed.append(1)
            return []

    monkeypatch.setattr(service_module, "watchlist_refresh_window",
                        lambda now=None: (True, "盘中自动刷新中"))
    monkeypatch.setattr("src.intraday.auto_select.is_trading_day",
                        lambda moment=None: False)
    monkeypatch.setattr(service_module.asyncio, "sleep", _fake_sleep)

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(_Loop()._watchlist_refresh_loop())  # noqa: SLF001

    assert computed == [], "非交易日不该做整表重算"


def test_refresh_loop_runs_during_call_auction_on_real_trading_day(
        monkeypatch: pytest.MonkeyPatch,
        freeze_at) -> None:
    """★ 2026-09-28 用户口径：真交易日 09:25 集合竞价撮合后必须立刻刷新。

    之前 `_is_trading_day_for_refresh()` 在 09:15-09:30 因市场时钟 tick 没推进
    会判 False，导致整表重算跳过 —— watchlist 在 9:25-9:30 这 5 分钟面板上
    仍显示 9/24 收盘价。新逻辑在集合竞价窗口里**不查**该闸门，让 watchlist
    在 9:25 撮合后立刻看到开盘价 + 集合竞价涨幅。
    """
    import asyncio

    freeze_at(2026, 9, 24, 9, 25)          # 周四 09:25 — 集合竞价撮合那一刻

    computed: list[int] = []
    slept = 0

    async def _fake_sleep(seconds, *args, **kwargs):
        nonlocal slept
        slept += 1
        if slept >= 2:
            raise KeyboardInterrupt
        return None

    class _Loop(service_module.IntradayService):
        def __init__(self) -> None:
            super().__init__()
            self._startup_grace_seconds = 0.0

        async def watchlist(self, **kwargs):     # type: ignore[override]
            computed.append(1)
            return []

    monkeypatch.setattr(service_module, "watchlist_refresh_window",
                        lambda now=None: (True, "盘中自动刷新中"))
    # 即便 `is_trading_day()` 仍判 False（市场时钟没推进），
    # 集合竞价窗口里整表重算也必须**真跑** —— 这是这次修复的目的。
    monkeypatch.setattr("src.intraday.auto_select.is_trading_day",
                        lambda moment=None: False)
    monkeypatch.setattr(service_module.asyncio, "sleep", _fake_sleep)

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(_Loop()._watchlist_refresh_loop())  # noqa: SLF001

    assert computed, "集合竞价窗口里必须重算（9:25 撮合价要立刻可见）"


def test_refresh_loop_recomputes_on_trading_day(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """反向用例：交易日必须照常重算（别把闸门做成一停到底）。"""
    import asyncio

    computed: list[int] = []
    slept = 0

    # 交易日路径每轮**必定**先重算再 sleep（`_recompute_already_running()` 的三个判据
    # 由真实重算设置，替身 `watchlist` 不碰它们，所以每轮都会真算），
    # 因此在第 2 次 sleep 抛异常 —— 观察完两轮且不会无限跑。
    async def _fake_sleep(seconds, *args, **kwargs):
        nonlocal slept
        slept += 1
        if slept >= 2:
            raise KeyboardInterrupt
        return None

    class _Loop(service_module.IntradayService):
        def __init__(self) -> None:
            super().__init__()
            self._startup_grace_seconds = 0.0

        async def watchlist(self, **kwargs):     # type: ignore[override]
            computed.append(1)
            return []

    monkeypatch.setattr(service_module, "watchlist_refresh_window",
                        lambda now=None: (True, "盘中自动刷新中"))
    monkeypatch.setattr("src.intraday.auto_select.is_trading_day",
                        lambda moment=None: True)
    monkeypatch.setattr(service_module.asyncio, "sleep", _fake_sleep)

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(_Loop()._watchlist_refresh_loop())  # noqa: SLF001

    # 关键不变量：交易日**确实在重算**（对比上面节假日用例的 `== []`）
    assert computed, "交易日必须照常重算"
    assert len(computed) == 2, f"两轮循环应各重算一次，实际 {len(computed)} 次"


def test_push_gate_allows_call_auction_on_real_trading_day(
        freeze_at) -> None:
    """★ 关键：**真交易日 09:15~09:30 集合竞价期必须放行**。

    这是修复过程中差点踩进去的坑：竞价期市场时钟还停在上一交易日
    （当天第一笔成交前 tick 的 timetag 不前进），而 `auto_select.is_trading_day()`
    恰恰"时钟停在上一交易日 → 判 False"。拿它当推送闸门，会把**每个正常交易日**
    早盘"开盘定方向"时段的信号整段误杀 —— 而那正是做T最关键的窗口。
    """
    freeze_at(2026, 9, 24, 9, 20)          # 周四 09:20，真交易日的竞价期
    allowed, why = service_module._push_allowed_now()   # noqa: SLF001
    assert allowed is True, f"竞价期不该被拦：{why}"


def test_push_gate_allows_lunch_break(freeze_at) -> None:
    """午休推的是**当天**信号，拦掉会损失"下午开盘前提醒"。"""
    freeze_at(2026, 9, 24, 12, 0)
    allowed, why = service_module._push_allowed_now()   # noqa: SLF001
    assert allowed is True, why


def test_push_gate_blocks_weekend(freeze_at) -> None:
    """周末休市无歧义，必须拦。"""
    freeze_at(2026, 9, 26, 10, 0)          # 周六
    allowed, why = service_module._push_allowed_now()   # noqa: SLF001
    assert allowed is False
    assert "周末" in why


def test_push_gate_blocks_after_close(freeze_at) -> None:
    """15:05 之后行情已定稿，此时的"信号"必然是重算在旧数据上重复触发的。"""
    freeze_at(2026, 9, 24, 15, 30)
    allowed, why = service_module._push_allowed_now()   # noqa: SLF001
    assert allowed is False
    assert "已收盘" in why


def test_push_gate_allows_during_trading(freeze_at) -> None:
    """盘中正常放行（别把闸门做成一律不推）。"""
    freeze_at(2026, 9, 24, 10, 30)
    allowed, why = service_module._push_allowed_now()   # noqa: SLF001
    assert allowed is True, why


def test_snapshot_push_site_uses_safe_gate() -> None:
    """`snapshot()` 的推送点必须走 `_push_allowed_now()`，**不得**走 `is_trading_day()`。

    不构造完整快照（那要拉满数据链），改为直接读源码钉住这个不变量：
    推送到 `_push()` 之前必须有一次 `_push_allowed_now()` 判断。真跑一遍代价
    过大且易碎，而"这道守卫被换回会误杀的 `is_trading_day`"正是要防的回归。
    """
    import ast
    import textwrap

    source = inspect.getsource(service_module.IntradayService.snapshot)
    tree = ast.parse(textwrap.dedent(source))

    pushes = [node for node in ast.walk(tree)
              if isinstance(node, ast.Attribute) and node.attr == "_push"]
    assert pushes, "snapshot() 里应当有推送调用点"
    push_line = min(node.lineno for node in pushes)

    gates = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
             and node.func.id == "_push_allowed_now"]
    assert gates, "snapshot() 里的推送必须经过 _push_allowed_now() 闸门"
    assert min(node.lineno for node in gates) < push_line, \
        "闸门必须位于推送调用之前"

    # 同时禁止误杀版判据出现在 snapshot() 里当推送守卫
    unsafe = [node for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
              and node.func.id == "is_trading_day"]
    assert not unsafe, (
        "snapshot() 不得用 is_trading_day() 拦推送 —— 它在真交易日 09:15~09:30 "
        "竞价期也会判 False，会误杀早盘信号")
