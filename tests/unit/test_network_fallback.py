"""联网兜底（`catalog/network_fallback.py`）三条护栏的测试。

## 为什么这个文件存在

用户的要求是「连接器找不到、指标登记里无 id、planner 目录里没有，
**都要自动去联网获取数据**，做最差的兜底，一定要找到数据」。
而"一定会联网"在没人看着的时候就是"一定会花钱"——
所以本条交付的**不是"能不能联网"，而是"什么条件下才允许联网"**。
下面的用例逐条钉住那三份护栏，每一条都说明"没有它会怎样"。

## 覆盖清单（缺一条就等于护栏缺一条）

1. **默认 fail-closed**（最重要）：不传 allowlist → 一律不允许兜底
2. 白名单：命中 → 允许；未命中 → 拒绝，且理由是人话 + 剩余额度
3. 预算：用尽 → 拒绝，**且不是抛异常**；`0 元` 读作"不许花"而不是"不限"
4. 频率：小时上限 → 第 N+1 次拒绝，理由含**剩余时间**
5. 冷却/熔断：失败 → 冷却（理由含剩余分钟）；限流 → 更长冷却；连续失败 → 熔断
6. **状态落盘**：新建实例（模拟重启）后计数仍在（用 tmp 路径，绝不写生产目录）
7. **「没量到」≠「量到 0」**：账本读不到 → `counters=None`；读到且为 0 才是 0
8. 并发：同一进程内并发计数不丢（有锁）
9. **不许真联网**：取数实现注入成假实现；只验护栏与编排
10. 触发判据：`DiagCode` 12 个码**逐个归类**（没有"没人管"的码）
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.infrastructure.catalog import network_fallback as nf  # noqa: E402
from src.infrastructure.catalog.local_data import DiagCode  # noqa: E402


def _settings():
    """生效的 Settings（`.env` 走这一路；白名单/防撞钟都从它回落）。"""
    from src.core.config import get_settings

    return get_settings()


from src.infrastructure.llm.circuit_breaker import (  # noqa: E402
    CircuitBreakerRegistry,
)

# ============================================================
# 测试替身（**全部进程内，不发任何网络请求**）
# ============================================================


class _Clock:
    """可注入时钟：测"等 10 分钟""跨 1 小时"不必真的等。"""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class _SourceError(RuntimeError):
    """带 `http_status` 的取数异常 —— 限流判据只认这个结构化字段。"""

    def __init__(self, message: str, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


class _Fetcher:
    """**假取数实现**：记录被调用的源，按脚本返回数据 / 返回空 / 抛错。"""

    def __init__(self, *, points=None, error=None, empty_sources=()) -> None:
        self.calls: list[tuple[str, str]] = []
        self._points = [{"period": "2026-09-01", "value": 1.0}] if points is None \
            else list(points)
        self._error = error
        self._empty = set(empty_sources)

    async def __call__(self, indicator: str, source: str) -> list:
        self.calls.append((indicator, source))
        if self._error is not None:
            raise self._error
        if source in self._empty:
            return []
        return list(self._points)


class _Result:
    """`local_data.MetricSeries` 的最小替身（`should_fallback` 只需要这两个字段）。"""

    def __init__(self, code: str = "", plan: dict | None = None) -> None:
        self.diag = type("_Diag", (), {"code": code})() if code else None
        self.plan = plan if plan is not None else {}


# ============================================================
# 隔离（autouse）：三种"会串用例"的东西一起处理
# ============================================================


@pytest.fixture(autouse=True)
def _isolate(tmp_dir, monkeypatch):
    """① 账本路径指到临时目录；② 熔断器每个用例一份新的；③ 清进程单例。

    ★ 为什么必须 autouse（照抄 tests/conftest.py 的纪律）：
    · **账本绝不能落到生产目录** —— `data/run/` 是 dev/pilot/生产**共用**的，
      测试里花掉的钱会真的吃掉生产的日预算额度（不报错，只表现为"今天怎么不兜底"）。
    · **熔断注册表是进程级单例**，一个用例把某个源打成 OPEN，下一个用例就会
      "莫名其妙被跳过"（且原因看起来完全合理）。靠"记得重置"防不住，所以 autouse。
      ⚠️ 必须换成**本用例专用的那一个**注册表对象（不能把类本身塞进去：
      那样每次 `get_or_create` 都会得到一只新注册表，计数器永远累积不起来 ——
      "熔断永不触发"会伪装成"测试通过"）。
    """
    monkeypatch.setenv("MOSS_NETWORK_FALLBACK_PATH",
                       str(Path(tmp_dir) / "nf_autouse.json"))
    registry = CircuitBreakerRegistry()
    monkeypatch.setattr(nf, "get_circuit_registry", lambda: registry)
    nf.reset_network_fallback()
    yield
    nf.reset_network_fallback()


def _fb(tmp_dir, clock=None, fetcher=None, allowlist=("A",), **kw) -> nf.NetworkFallback:
    """建一个兜底实例：显式 tmp 账本路径 + 可注入时钟（默认起点固定）。"""
    kw.setdefault("state_path", str(Path(tmp_dir) / "nf.json"))
    kw.setdefault("clock", clock or _Clock())
    return nf.NetworkFallback(allowlist=allowlist, fetcher=fetcher, **kw)


# ============================================================
# ① 默认 fail-closed（**最重要的一条**）
# ============================================================


def test_default_is_fail_closed_without_allowlist(tmp_dir):
    """★ 不传 allowlist → **不允许任何兜底**。

    为什么这条最重要：AGENTS.md《AI 首轮编码硬约束》——"**默认值即护栏**：
    安全的一侧做成默认；靠每个调用点记得传参数必然漏（本项目实测因此漏过
    一次付费兜底）"。用户要的是"一定要找到数据"，而默认必须是"不许去"，
    否则任何一次忘记传参都会变成静默联网。
    """
    fb = nf.NetworkFallback(state_path=str(Path(tmp_dir) / "d.json"))
    assert nf.DEFAULT_ALLOWLIST == (), "默认白名单必须为空（最严）"
    assert fb.allowlist == ()
    assert fb.enabled is False

    decision = fb.can_fallback("CPI")
    assert decision.allowed is False
    assert decision.code == nf.ReasonCode.ALLOWLIST_EMPTY
    # 理由必须是**人话 + 剩余额度**，不是枚举值裸输出
    assert "白名单为空" in decision.reason
    assert "2.00" in decision.reason, decision.reason


def test_default_fail_closed_also_blocks_fetch(tmp_dir):
    """默认态下 `fetch()` 连取数实现都不该碰（否则"问一句"就成了"去拿了"）。"""
    fetcher = _Fetcher()
    fb = _fb(tmp_dir, fetcher=fetcher, allowlist=None)   # None → 默认（空）
    assert asyncio.run(fb.fetch("CPI")) == []
    assert fetcher.calls == [], "白名单为空时居然调用了取数实现"
    snap = fb.snapshot()
    assert snap["enabled"] is False
    assert snap["allowlist"] == []


# ============================================================
# ② 白名单
# ============================================================


def test_allowlisted_source_is_allowed_and_reason_carries_remaining_budget(tmp_dir):
    """命中白名单 → 允许，且理由里带**剩余预算/剩余次数**（不是"OK"两个字）。"""
    fb = _fb(tmp_dir)
    decision = fb.can_fallback("CPI", source="A")
    assert decision.allowed is True
    assert decision.code == nf.ReasonCode.OK
    assert decision.matched_entry == "A"
    assert decision.remaining_cny == pytest.approx(2.0)
    assert decision.remaining_calls == 30
    assert "剩余" in decision.reason and "2.00" in decision.reason


def test_source_outside_allowlist_is_rejected_with_human_reason(tmp_dir):
    """白名单外的源 → 拒绝；理由必须说清"哪个源、白名单里有什么"。"""
    fb = _fb(tmp_dir)
    decision = fb.can_fallback("CPI", source="evil.example.com")
    assert decision.allowed is False
    assert decision.code == nf.ReasonCode.SOURCE_NOT_ALLOWED
    assert "evil.example.com" in decision.reason
    assert "A" in decision.reason, "理由里要列出白名单，否则人不知道该加什么"
    assert "剩余" in decision.reason


def test_unspecified_source_does_not_bypass_the_allowlist(tmp_dir):
    """`source=""`（未指定源）不会成为绕过白名单的口子。

    未指定源时只是"不做源级判定"（真正的源级判定在 `fetch()` 里逐个源做），
    所以这里允许、而 `fetch()` 仍然只能调白名单内的源 ——
    见 `test_fetch_never_touches_a_source_outside_the_allowlist`。
    """
    fb = _fb(tmp_dir)
    assert fb.can_fallback("CPI").allowed is True
    fetcher = _Fetcher()
    fb2 = _fb(tmp_dir, fetcher=fetcher, candidates=lambda ind: ["A", "Z"])
    asyncio.run(fb2.fetch("CPI"))
    assert [s for _i, s in fetcher.calls] == ["A"]


def test_domain_entry_matches_subdomain_but_not_lookalike():
    """域名条目按**点边界**后缀匹配：子域命中，`noteastmoney.com` 不命中。

    为什么不能用子串匹配：白名单是**花钱**的判据，宽一格就等于允许了一个
    无关域名（`noteastmoney.com` 这类看着像的域名正好是钓鱼的常见形态）。
    """
    allow = ("eastmoney.com", "AkshareConnector")
    assert nf.source_allowed("push2.eastmoney.com", allow) == "eastmoney.com"
    assert nf.source_allowed("EASTMONEY.COM", allow) == "eastmoney.com"
    assert nf.source_allowed("noteastmoney.com", allow) == ""
    assert nf.source_allowed("akshareconnector", allow) == "AkshareConnector"
    assert nf.source_allowed("", allow) == ""
    assert nf.source_allowed("anything", ()) == ""


# ============================================================
# ③ 预算
# ============================================================


def test_daily_budget_exhausted_rejects_and_never_raises(tmp_dir):
    """★ 预算用尽 → 拒绝；**拒绝不是抛异常**（兜底坏了不能拖垮主链路）。

    预算 0.04 元 / 单次 0.02 元 → 正好 2 次；第 3 次必须被挡住，
    而且**取数实现不该被再调一次**（"拒绝"要真的省下那次调用）。
    """
    fetcher = _Fetcher()
    fb = _fb(tmp_dir, fetcher=fetcher, daily_budget_cny=0.04)
    assert len(asyncio.run(fb.fetch("CPI"))) == 1
    assert len(asyncio.run(fb.fetch("CPIPP"))) == 1
    assert len(fetcher.calls) == 2

    third = asyncio.run(fb.fetch("CPI3"))          # 不抛异常，返回空
    assert third == []
    assert len(fetcher.calls) == 2, "预算已用尽却还是发了第 3 次调用"

    decision = fb.can_fallback("CPI4")
    assert decision.allowed is False
    assert decision.code == nf.ReasonCode.DAILY_BUDGET_EXHAUSTED
    assert "0.04/0.04 元" in decision.reason, decision.reason

    counters = fb.snapshot()["counters"]
    assert counters["spent_cny"] == pytest.approx(0.04)
    assert counters["calls_today"] == 2
    assert counters["rejected_by"][nf.ReasonCode.DAILY_BUDGET_EXHAUSTED] == 1


def test_zero_daily_budget_means_no_spend_not_unlimited(tmp_dir):
    """★ `daily_budget_cny=0` 必须读作"一分钱都不许花"。

    为什么专门测它：`core/budget.CostBudget` 里 `daily <= 0` 是"**不限**预算"，
    两个模块语义相反。若这里也照抄成"0 = 不限"，那"把预算配成 0 想关掉兜底"
    的人会得到**完全相反**的结果 —— 而且不报错。
    """
    fetcher = _Fetcher()
    fb = _fb(tmp_dir, fetcher=fetcher, daily_budget_cny=0)
    assert asyncio.run(fb.fetch("CPI")) == []
    assert fetcher.calls == []
    decision = fb.can_fallback("CPI")
    assert decision.code == nf.ReasonCode.DAILY_BUDGET_EXHAUSTED
    assert "0 元" in decision.reason and "一分钱都不许花" in decision.reason


def test_budget_survives_a_new_instance(tmp_dir):
    """★ **状态落盘**：新建实例（模拟进程重启）后账本仍在。

    AGENTS.md 硬约束 + 本项目实测踩过的坑：护栏状态放进程内存里，重启即失效
    （"跑一次测试/重启一次服务就把额度忘了"）。这里用 tmp 路径，绝不写生产目录。
    """
    path = str(Path(tmp_dir) / "persist.json")
    clock = _Clock()
    fetcher = _Fetcher()
    fb1 = nf.NetworkFallback(allowlist=("A",), state_path=path, clock=clock,
                             fetcher=fetcher)
    for ind in ("CPI", "PPI", "M2"):
        assert asyncio.run(fb1.fetch(ind))
    assert fb1.snapshot()["counters"]["calls_today"] == 3

    # 模拟重启：全新实例、全新锁与内存，只共享那个文件
    fb2 = nf.NetworkFallback(allowlist=("A",), state_path=path,
                             clock=_Clock(start=clock.now))
    counters = fb2.snapshot()["counters"]
    assert counters["calls_today"] == 3, "重启后调用次数丢了 —— 落盘没生效"
    assert counters["spent_cny"] == pytest.approx(0.06)
    assert counters["totals"]["successes"] == 3
    assert Path(path).exists()


def test_ledger_rolls_over_on_a_new_day(tmp_dir):
    """跨日自动重置**日**额度（累计计数保留）—— 否则第二天仍然"预算已用尽"。"""
    clock = _Clock()
    fetcher = _Fetcher()
    fb = _fb(tmp_dir, clock=clock, fetcher=fetcher, daily_budget_cny=0.04)
    assert asyncio.run(fb.fetch("CPI"))
    assert asyncio.run(fb.fetch("PPI"))
    assert fb.can_fallback("M2").allowed is False

    clock.advance(24 * 3600)                        # 第二天
    assert fb.can_fallback("M2").allowed is True
    counters = fb.snapshot()["counters"]
    assert counters["spent_cny"] == 0.0, "跨日没重置日花费"
    assert counters["calls_today"] == 0
    assert counters["totals"]["attempts"] == 2, "累计计数不该被跨日清掉"


# ============================================================
# ④ 频率（小时级）+ ⑤ 冷却 / 熔断
# ============================================================


def test_hourly_limit_rejects_with_remaining_time(tmp_dir):
    """★ 第 N+1 次拒绝，且理由里必须有**剩余时间**（"再过 N 分钟"）。

    为什么要有小时级上限（与日预算并存）：日预算管"总共花多少"，
    它管"多快问"——一个失控的重试循环（按秒重试）在日预算用完之前
    就已经把源打爆了。
    """
    clock = _Clock()
    fetcher = _Fetcher()
    fb = _fb(tmp_dir, clock=clock, fetcher=fetcher, max_calls_per_hour=2)
    assert asyncio.run(fb.fetch("CPI"))
    assert asyncio.run(fb.fetch("PPI"))
    clock.advance(600)                               # 让"剩余时间"是个人话数字
    assert asyncio.run(fb.fetch("M2")) == []
    assert len(fetcher.calls) == 2, "超过小时上限还是发了调用"

    decision = fb.can_fallback("M2")
    assert decision.allowed is False
    assert decision.code == nf.ReasonCode.HOURLY_LIMIT_REACHED
    assert decision.retry_after_s == pytest.approx(3000.0)
    assert "50 分钟" in decision.reason, decision.reason

    clock.advance(3000)                              # 窗口滑出去
    assert fb.can_fallback("M2").allowed is True


def test_source_failure_returns_empty_and_sets_cooldown(tmp_dir):
    """源失败 → 返回 `[]`（不抛）+ 记失败 + **该指标进冷却**（理由含剩余分钟）。

    没有冷却会怎样：源真的没有这个数据时，每一轮分析都去问一次同一个源 ——
    延迟白付、源被白打，而且**看起来一切正常**。
    """
    clock = _Clock()
    fetcher = _Fetcher(error=RuntimeError("源挂了"))
    fb = _fb(tmp_dir, clock=clock, fetcher=fetcher)
    assert asyncio.run(fb.fetch("CPI")) == []

    counters = fb.snapshot()["counters"]
    assert counters["totals"]["attempts"] == 1
    assert counters["totals"]["failures"] == 1, "一次尝试只该记一次失败"
    assert counters["totals"]["successes"] == 0

    clock.advance(60)
    decision = fb.can_fallback("CPI")
    assert decision.allowed is False
    assert decision.code == nf.ReasonCode.COOLDOWN
    assert "还剩 9 分钟" in decision.reason, decision.reason
    assert decision.retry_after_s == pytest.approx(540.0)
    # 别的指标不受影响（冷却是**按指标**的，不是一刀切关掉功能）
    assert fb.can_fallback("PPI").allowed is True

    clock.advance(541)
    assert fb.can_fallback("CPI").allowed is True


def test_rate_limited_source_gets_longer_cooldown_and_opens_circuit(tmp_dir):
    """限流（429）→ **更长**冷却（复用 `looks_rate_limited` 的机器可读判据）；
    连续失败 → 源级熔断，`can_fallback(source=...)` 给出熔断剩余时间。

    为什么限流要单独加长：源在限流，10 分钟后大概率仍在限流 ——
    短冷却只会继续白撞。计 30 分钟是「给它一个窗口」。
    """
    clock = _Clock()
    error = _SourceError("429 Too Many Requests", http_status=429)
    fetcher = _Fetcher(error=error)
    fb = _fb(tmp_dir, clock=clock, fetcher=fetcher)

    assert asyncio.run(fb.fetch("CPI")) == []
    decision = fb.can_fallback("CPI")
    assert decision.code == nf.ReasonCode.COOLDOWN
    assert decision.retry_after_s == pytest.approx(nf.RATE_LIMIT_COOLDOWN_SECONDS)
    assert decision.retry_after_s > nf.FAILURE_COOLDOWN_SECONDS, (
        "被限流的冷却不该与普通失败一样长")

    # 连续 3 次失败 → 熔断（熔断器用真实时间，三次调用间隔是瞬时）
    for ind in ("PPI", "M2", "社融"):
        clock.advance(nf.RATE_LIMIT_COOLDOWN_SECONDS + 1)    # 绕过指标冷却
        assert asyncio.run(fb.fetch(ind)) == []

    snap = fb.snapshot()
    assert snap["sources"]["A"]["state"] == "OPEN", snap["sources"]
    blocked = fb.can_fallback("CPI", source="A")
    assert blocked.allowed is False
    assert blocked.code == nf.ReasonCode.CIRCUIT_OPEN
    assert "熔断" in blocked.reason and "后自动半开重试" in blocked.reason
    # 熔断中的源不再被调用（省掉一次注定失败的等待）
    before = len(fetcher.calls)
    assert asyncio.run(fb.fetch("CPI")) == []
    assert len(fetcher.calls) == before, "熔断中的源还是被调用了"


# ============================================================
# ⑥ 可观测：「没量到」≠「量到 0」
# ============================================================


def test_missing_state_file_is_absent_not_measured_zero(tmp_dir):
    """账本**文件不存在**（冷启动）→ `available=False` + `counters=None`。

    为什么不给一组 0：AGENTS.md——"**「没量到」与「量到 0」必须分开显示**；
    读不到数据时显示未量到/无法统计，绝不用 0 糊过去（宁可不显示，也不显示假绿）"。
    详见 `test_unreadable_state_is_not_a_fake_zero` 的反面对照。
    """
    path = str(Path(tmp_dir) / "cold.json")
    fb = nf.NetworkFallback(allowlist=("A",), state_path=path)
    snap = fb.snapshot()
    assert snap["state_status"] == "absent"
    assert snap["available"] is False
    assert snap["counters"] is None, "冷启动不能假装'量到 0'"
    # 冷启动仍然允许兜底（否则功能永不生效），但状态说清是"账本为空"
    assert snap["admission"] == "open"
    assert fb.can_fallback("CPI").allowed is True


def test_unreadable_state_is_not_a_fake_zero(tmp_dir):
    """★ 账本**读不动** → `available=False` + `counters=None` + **拒绝兜底**。

    这是"没量到 ≠ 量到 0"最重要的一面：读不到账本时若按 0 处理，
    就是"无法记账地超支"。所以本模块在这里与 `rate_limit_guard` 的选择**相反**：
    那边"探测失败 ≠ 判为限流"（误锁可用模型是净损失），这边判的是花钱 ——
    读不到就 fail-closed，并且给出一句能照着做的修法。
    """
    path = Path(tmp_dir) / "broken.json"
    path.write_text("{ 这不是 JSON", encoding="utf-8")
    fetcher = _Fetcher()
    fb = nf.NetworkFallback(allowlist=("A",), state_path=str(path),
                            clock=_Clock(), fetcher=fetcher)

    snap = fb.snapshot()
    assert snap["available"] is False
    assert snap["state_status"] == "unreadable"
    assert snap["counters"] is None, "账本读不到却给了一组 0 —— 这就是假绿"
    assert snap["admission"] == "closed"
    assert "账本读不到" in snap["summary"]

    decision = fb.can_fallback("CPI")
    assert decision.allowed is False
    assert decision.code == nf.ReasonCode.STATE_UNAVAILABLE
    assert "fail-closed" in decision.reason and str(path) in decision.reason

    assert asyncio.run(fb.fetch("CPI")) == []
    assert fetcher.calls == [], "账本不可读却仍然去联网了"

    # 反面对照：账本**可读且真的是 0** 时，available=True 且 counters 给出 0
    good = nf.NetworkFallback(
        allowlist=("A",), state_path=str(Path(tmp_dir) / "good.json"),
        clock=_Clock())
    good._record_rejection("CPI", good.can_fallback("CPI", source="Z"))
    snap_good = good.snapshot()
    assert snap_good["available"] is True
    assert snap_good["state_status"] == "ok"
    assert snap_good["counters"]["spent_cny"] == 0.0, "量到的 0 就该显示 0"
    assert snap_good["counters"]["rejected_by"][nf.ReasonCode.SOURCE_NOT_ALLOWED] == 1


def test_can_fallback_is_a_pure_query(tmp_dir):
    """`can_fallback` **不许写账本** —— 否则"谁问得多，谁的拒绝计数就高"。

    它是给调用方当预检用的（可能被轮询）：观测数据不能被自己的查询污染。
    落账由 `fetch()` 负责（那里才是"决定不去了"真的发生的地方）。
    """
    path = Path(tmp_dir) / "pure.json"
    fb = nf.NetworkFallback(allowlist=("A",), state_path=str(path))
    for _ in range(5):
        fb.can_fallback("CPI")
        fb.can_fallback("CPI", source="Z")          # 会被拒绝
    assert not path.exists(), "纯查询写了账本"


def test_snapshot_is_health_ready(tmp_dir):
    """`snapshot()` 的形状（要接 `/health`）：可 JSON 序列化 + 关键字段齐 + 口径随行。

    "口径与局限随数据一起下发"（AGENTS.md）：成本是**估算**，所以
    `cost_basis` 必须跟快照一起给出去，否则读的人会把估算当账单。
    """
    fb = _fb(tmp_dir, fetcher=_Fetcher())
    asyncio.run(fb.fetch("CPI"))
    snap = fb.snapshot()
    json.dumps(snap, ensure_ascii=False)             # /health 要能序列化
    for key in ("available", "state_status", "state_error", "state_path",
                "admission", "enabled", "allowlist", "daily_budget_cny",
                "max_calls_per_hour", "call_cost_cny", "cost_basis",
                "failure_cooldown_s", "rate_limit_cooldown_s", "counters",
                "sources", "summary"):
        assert key in snap, f"快照缺字段：{key}（接 /health 时才发现就晚了）"
    counters = snap["counters"]
    for key in ("day", "spent_cny", "calls_today", "remaining_cny",
                "calls_last_hour", "remaining_calls_this_hour", "totals",
                "rejected_by", "cooldowns", "last_reason", "recent_events"):
        assert key in counters, f"counters 缺字段：{key}"
    assert counters["totals"]["attempts"] == 1
    assert counters["recent_events"][-1]["outcome"] == "success"
    assert "估算" in snap["cost_basis"]


def test_every_attempt_and_rejection_leaves_a_queryable_record(tmp_dir):
    """每次尝试/拒绝/成功都留下**可查询**的记录（计数 + 最近原因）。"""
    fetcher = _Fetcher(error=RuntimeError("boom"))
    fb = _fb(tmp_dir, fetcher=fetcher, allowlist=("A",))
    asyncio.run(fb.fetch("CPI"))                     # 失败一次
    fb2 = _fb(tmp_dir, fetcher=_Fetcher())
    asyncio.run(fb2.fetch("PPI"))                    # 成功一次
    asyncio.run(fb2.fetch("M2"))
    fb2._record_rejection("M2", fb2.can_fallback("M2", source="Z"))  # 拒绝一次

    counters = fb2.snapshot()["counters"]
    assert counters["totals"] == {"attempts": 3, "successes": 2,
                                  "failures": 1, "rejections": 1}
    events = counters["recent_events"]
    assert {e["outcome"] for e in events} == {"success", "failure", "rejected"}
    assert counters["last_reason"], "最近原因必须可读（人话）"
    assert len(events) <= nf.RECENT_EVENTS_MAX, "事件必须有界（/health 不能无限长）"


def test_human_seconds_speaks_human():
    """剩余时间必须是人话；**未量到**与"0 秒"分开。"""
    assert nf.human_seconds(540) == "9 分钟"
    assert nf.human_seconds(3600) == "1 小时"
    assert nf.human_seconds(3900) == "1 小时 5 分钟"
    assert nf.human_seconds(30) == "30 秒"
    assert nf.human_seconds(None) == "未量到"


# ============================================================
# ⑦ 并发
# ============================================================


def test_concurrent_records_do_not_lose_counts(tmp_dir):
    """★ 同一进程内并发计数不能丢（账本是"读-改-写"，必须串行）。

    为什么必须有这条：并发下丢失计数会让预算护栏**比配置更宽松** ——
    而这个方向的错误永远不会报错，只会在某天账单上出现。
    """
    clock = _Clock()
    fetcher = _Fetcher()
    fb = _fb(tmp_dir, clock=clock, fetcher=fetcher, max_calls_per_hour=100)
    threads_n, per_thread = 8, 5
    barrier = threading.Barrier(threads_n)

    def _worker(idx: int) -> None:
        barrier.wait()                               # 尽量同时开跑（放大竞争）
        for i in range(per_thread):
            asyncio.run(fb.fetch(f"IND{idx}-{i}"))

    workers = [threading.Thread(target=_worker, args=(n,)) for n in range(threads_n)]
    for t in workers:
        t.start()
    for t in workers:
        t.join()

    counters = fb.snapshot()["counters"]
    expected = threads_n * per_thread
    assert counters["calls_today"] == expected, "并发下丢了调用次数"
    assert counters["totals"]["attempts"] == expected
    assert counters["totals"]["successes"] == expected
    assert counters["spent_cny"] == pytest.approx(expected * nf.DEFAULT_CALL_COST_CNY)
    assert counters["rejected_by"] == {}, "预算/频率足够时不该出现拒绝"


# ============================================================
# ⑧ 编排（只验"找到源并调用"，不验网络）
# ============================================================


class _BaseConn:
    points: list = []
    inds: tuple = ()

    async def fetch(self, indicator, start_date=None, end_date=None):
        return list(self.points)

    def get_capabilities(self):
        return {"name": type(self).__name__, "indicators": list(self.inds)}

    @classmethod
    def supports(cls, indicator: str) -> bool:
        return indicator in cls.inds


class _ConnA(_BaseConn):
    points = [{"period": "2026-09-01", "value": "A"}]
    inds = ("CPI",)


class _ConnB(_BaseConn):
    points = [{"period": "2026-09-01", "value": "B"}]
    inds = ("CPI", "PPI")


def test_router_source_bridge_orchestrates_without_network(tmp_dir):
    """`RouterSourceBridge` 按 routes 顺序枚举源、按**源名**调用（无网络）。

    为什么不直接用 `source_name`（"AkShare"这类中文标签）当白名单键：
    文案会被本地化/优化，白名单跟着它漂移就变成"改了文案，兜底静默失效"。
    """
    routes = [(_ConnA(), _ConnA.supports), (_ConnB(), _ConnB.supports)]
    bridge = nf.RouterSourceBridge(routes)
    assert bridge.source_key(_ConnA()) == "_ConnA"
    assert bridge.candidates("CPI") == ["_ConnA", "_ConnB"]
    assert bridge.candidates("PPI") == ["_ConnB"]
    assert bridge.candidates("不存在") == []

    fb = nf.NetworkFallback.from_connector_routes(
        routes, allowlist=("_ConnB",), state_path=str(Path(tmp_dir) / "b.json"),
        clock=_Clock())
    got = asyncio.run(fb.fetch("CPI"))
    assert got == _ConnB.points, "白名单外/内的源没被按规则选中"

    # 从既有 router 上取 routes（只读；取不到返回 None，由接入方显式传）
    class _Router:
        _routes = routes

    assert nf.router_source_bridge(_Router()) is not None
    assert nf.router_source_bridge(object()) is None


def test_fetch_never_touches_a_source_outside_the_allowlist(tmp_dir):
    """★ 白名单是**调用前**的判据：白名单外的源一次都不能被调用。

    判据放在"调用前"而不是"调用后过滤"：兜底的代价在一次 HTTP 请求上就已经
    付出了（延迟+额度），事后过滤只是把结果丢掉。
    """
    fetcher = _Fetcher()
    fb = _fb(tmp_dir, fetcher=fetcher, allowlist=("B",),
             candidates=lambda ind: ["A", "B", "C"])
    got = asyncio.run(fb.fetch("CPI"))
    assert [s for _i, s in fetcher.calls] == ["B"]
    assert got


def test_fetch_without_wiring_is_observable(tmp_dir):
    """接线缺口（没注入取数实现）必须**可见**，不是静默返回空。

    "装了但从不生效"是本项目实测过的最贵的缺陷类型（判据在文件里、
    根本没人调它，且不报错）。所以这里记 `failures` + 事件里写清原因。
    """
    fb = nf.NetworkFallback(allowlist=("A",),
                            state_path=str(Path(tmp_dir) / "nowire.json"),
                            clock=_Clock())
    assert asyncio.run(fb.fetch("CPI")) == []
    counters = fb.snapshot()["counters"]
    assert counters["totals"]["failures"] == 1
    assert "未注入取数实现" in counters["last_reason"], counters["last_reason"]


def test_no_candidate_source_is_a_rejection_not_a_failure(tmp_dir):
    """白名单内没有支持该指标的源 → 记**拒绝**（`rejected_by` 分账），不是失败。

    分开的意义：失败=源坏了（要冷却、要看源健康），拒绝=没这个口子（要改配置）。
    混在一起会让"该加白名单"看起来像"源不稳定"。
    """
    fetcher = _Fetcher()
    fb = _fb(tmp_dir, fetcher=fetcher, allowlist=("A",),
             candidates=lambda ind: ["Z"])          # Z 不在白名单
    assert asyncio.run(fb.fetch("CPI")) == []
    assert fetcher.calls == []
    counters = fb.snapshot()["counters"]
    assert counters["totals"]["failures"] == 0
    assert counters["rejected_by"][nf.ReasonCode.NO_SOURCE_AVAILABLE] == 1


# ============================================================
# ⑨ 触发判据（哪类本地结果才允许联网）
# ============================================================


def test_trigger_codes_cover_every_diag_code_without_silent_gaps():
    """★ `DiagCode` 的 **12 个码必须逐个归类**（触发 / 不触发），不允许漏登记的。

    为什么这条是护栏而不是形式主义：将来新增一个诊断码时，如果没人管它，
    默认就是"不联网"（安全），但**没人知道这是有意的还是忘了** ——
    本项目的两次"静默少算"都是这么来的。断言写成
    "触发表 ∪ 不触发表 == DiagCode 全集"，新增码会立刻让这条测试变红。
    """
    real = {v for k, v in vars(DiagCode).items()
            if not k.startswith("_") and isinstance(v, str)}
    # ⚠️ 判据用**并集相等**，不写死码数。
    #    第一版这里有一行 `assert len(real) == 12`，与它自己的 docstring
    #    （"触发表 ∪ 不触发表 == DiagCode 全集"）**矛盾**：`local_data` 后来补了
    #    5 个**环境维度**码（`ENV_NOT_COVERED` / `PROD_ONLY` / `DEV_ONLY` /
    #    `DEV_SYNC_DELAY` / `PROD_PERMISSION_DENIED`）——并集判据立刻报出
    #    "有诊断码没被归类"（那是对的），而魔数那行只会说"不再是 12 个"，
    #    把"该归类"这条真信息盖掉了。
    #    **判据要表达意图，不要表达某个时刻的数量。**
    assert real, "DiagCode 一个码都没有 —— 枚举坏了"
    assert nf.FALLBACK_TRIGGER_CODES <= real, "触发表里有不存在的诊断码（漂移）"
    no_fallback = set(nf._NO_FALLBACK_REASONS)                     # noqa: SLF001
    assert nf.FALLBACK_TRIGGER_CODES | no_fallback == real, (
        "有诊断码没被归类："
        f"{sorted(real - nf.FALLBACK_TRIGGER_CODES - no_fallback)}")
    # 两张表不许重叠：重叠意味着同一个码既"触发"又"不触发"，
    # 行为取决于先查哪张表 —— 这是静默缺陷的温床
    overlap = nf.FALLBACK_TRIGGER_CODES & no_fallback
    assert not overlap, f"以下码同时出现在两张表里：{sorted(overlap)}"


def test_should_fallback_maps_local_results_to_a_decision():
    """触发判据是**机器可读**的（看诊断码，不看文案），且两边都给理由。"""
    for code in ("NO_DATA", "NO_COLUMN", "NO_TABLE",
                 "STALE_BEYOND_TOLERANCE", "CONN_FAIL"):
        ok, why = nf.should_fallback(_Result(code=code))
        assert ok is True, code
        assert code in why and len(why) > 10

    for code in ("NOT_APPLICABLE_FOR_ENTITY", "NO_PERMISSION", "ENTITY_UNMAPPED",
                 "TIME_OUT_OF_RANGE", "FREQ_MISMATCH", "UNIT_MISMATCH",
                 "DIM_MISMATCH"):
        ok, why = nf.should_fallback(_Result(code=code))
        assert ok is False, f"{code} 不该触发联网（联网也拿不到，白花预算）"
        assert code in why

    # 未知码 → 不联网（未知即不许花钱），但要说清"请登记"
    ok, why = nf.should_fallback(_Result(code="FUTURE_CODE"))
    assert ok is False and "登记" in why


def test_should_fallback_for_stale_data_uses_its_own_threshold():
    """本地有数据但**过旧** → 允许补采；阈值与 `local_data` 的展示容忍阈值分开。

    两个阈值混用会出现两种坏结果：用 400 天的展示阈值当触发 → 停更一年才补；
    用 30 天当展示阈值 → 正常季度数据被标成"过旧"。所以这里是**另一个常量**。
    """
    assert nf.STALE_TRIGGER_DAYS != 400
    ok, why = nf.should_fallback(_Result(plan={"stale_days": nf.STALE_TRIGGER_DAYS + 1}))
    assert ok is True and "超过触发阈值" in why
    ok, why = nf.should_fallback(_Result(plan={"stale_days": nf.STALE_TRIGGER_DAYS - 1}))
    assert ok is False and "不触发" in why
    # 新鲜度**未量到** → 保守判为"不联网"（缺量到就别花钱）
    ok, why = nf.should_fallback(_Result(plan={}))
    assert ok is False and "未量到" in why


# ============================================================
# ⑩ 路径隔离 + 建议白名单
# ============================================================


def test_state_path_follows_instance_isolation(monkeypatch):
    """★ 账本路径必须按实例隔离（否则 dev 的调试吃掉生产的额度）。

    学 `rate_limit_guard.resolve_state_path()`：显式变量 > `LLM_AUDIT_DIR` 同级 >
    默认路径。`data/run/` 是三个实例共用的目录，写死它等于跨实例共享预算。
    """
    monkeypatch.setenv("MOSS_NETWORK_FALLBACK_PATH", "tmp/explicit.json")
    assert nf.resolve_state_path() == "tmp/explicit.json"
    monkeypatch.delenv("MOSS_NETWORK_FALLBACK_PATH")
    monkeypatch.setenv("LLM_AUDIT_DIR", "data/dev/llm_audit")
    assert nf.resolve_state_path() == "data/dev/llm_audit/network_fallback.json"
    monkeypatch.delenv("LLM_AUDIT_DIR")
    assert nf.resolve_state_path() == nf.DEFAULT_STATE_PATH


def test_suggested_allowlist_is_machine_readable_and_every_entry_justified():
    """建议白名单：条目是机器可读标识、每条都有理由、与默认值不同。

    "有理由"是硬要求：白名单是花钱的判据，一个说不清理由的条目
    迟早会被当成"历史遗留"照抄下去。测试钉住它，新增条目必须同时写理由。
    """
    assert nf.SUGGESTED_ALLOWLIST != nf.DEFAULT_ALLOWLIST, (
        "建议值不能等于默认值 —— 默认必须是 fail-closed 的空白名单")
    assert len(set(nf.SUGGESTED_ALLOWLIST)) == len(nf.SUGGESTED_ALLOWLIST)
    for entry in nf.SUGGESTED_ALLOWLIST:
        assert entry.isascii(), f"白名单条目必须是机器可读标识：{entry!r}"
        assert entry in nf.SUGGESTED_ALLOWLIST_REASONS, f"{entry} 没有理由"
        assert len(nf.SUGGESTED_ALLOWLIST_REASONS[entry]) > 10
    for src, why in nf.NOT_SUGGESTED_SOURCES.items():
        assert src not in nf.SUGGESTED_ALLOWLIST, f"{src} 同时出现在两份名单里"
        assert len(why) > 10


def test_singleton_defaults_to_disabled_and_can_be_replaced(tmp_dir,
                                                           monkeypatch):
    """进程级单例：懒建 = 按配置（未配白名单时未启用）；接入方 `set_network_fallback` 换入生效实例。

    为什么要有它：`/health` 与链路必须读**同一个**实例，否则会出现
    "界面显示没兜底、实际在兜底"这种两边都不报错的共存。

    ⚠️ 2026-09-29：判据必须**自己控制输入**。`allowlist_from_env()` 现在会回落到
    Settings（`.env` 走这一路，见 `test_config_takes_effect.py` 的现场），
    而本工作区的 `.env` **已经真的配了白名单**（用户口径「找不到就去联网找」）
    ⇒ 再断言"懒建一定未启用"就是在测**开发者的 .env**，而不是测代码。
    所以这里显式清掉两个来源再判。
    """
    monkeypatch.delenv(nf.ALLOWLIST_ENV, raising=False)
    monkeypatch.setattr(_settings(), "network_fallback_allowlist", "",
                        raising=False)
    nf.reset_network_fallback()

    lazy = nf.get_network_fallback()
    assert lazy.enabled is False
    assert lazy.snapshot()["summary"].startswith("未启用")

    configured = nf.set_network_fallback(
        nf.NetworkFallback(allowlist=nf.SUGGESTED_ALLOWLIST,
                           state_path=str(Path(tmp_dir) / "set.json")))
    assert nf.get_network_fallback() is configured
    assert nf.get_network_fallback().enabled is True
    nf.reset_network_fallback()
    assert nf.get_network_fallback().enabled is False


def test_lazy_singleton_follows_the_configured_allowlist(monkeypatch):
    """★ 反向判据：**配了白名单就得启用**（否则"我配了但它没生效"又回来了）。

    这条是 2026-09-29 那个"配置≠生效"陷阱的机器复现：
    `.env` 里配了 6 个源，而单例仍然是空白名单 ⇒ 日志只有
    `[ALLOWLIST_EMPTY]`，看起来像护栏在工作。
    """
    monkeypatch.setattr(_settings(), "network_fallback_allowlist",
                        "AkshareConnector", raising=False)
    nf.reset_network_fallback()
    assert nf.get_network_fallback().enabled is True
    assert "AkshareConnector" in nf.get_network_fallback().allowlist
