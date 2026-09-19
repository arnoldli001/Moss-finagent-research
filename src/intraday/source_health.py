"""数据源健康与延迟追踪：按**实测响应速度**给数据源排序 + 自动容灾。

## 为什么需要它（实测数据，2026-09-15，32 次直连快照）

| 源 | 调用方式 | 中位延迟 | p90 | 数据新鲜度 |
|----|----------|---------:|----:|------------|
| **QMT（迅投）** | 本机终端 `get_full_tick` | **0 ms** | 0 ms | 实时（含盘口五档） |
| 腾讯 | 公网 HTTP `qt.gtimg.cn` | 54 ms | 62 ms | 实时 |
| Tushare | 公网 API | 66~248 ms | — | **仅 EOD**：当日盘中/晚间调用返回空表 |

分时/分钟K线：QMT 1m 中位 **17ms**、5m 中位 **8ms**（同样是本机内存读取）。
Tushare 没有实时行情（其"实时分钟/实时日线"属**单独收费权限**，未购买），
所以做T的实时链路只能由 QMT / 腾讯 / 新浪承担，Tushare 只能作为**盘后校验与因子源**。

结论落到代码上：**不能把顺序写死在配置里**。QMT 快是因为它是本机终端，但一旦终端
未启动/掉线就必须立刻让位给腾讯；腾讯偶发抖动时也应该被降权。这里用
EWMA 延迟 + 失败冷却做「测量驱动」的排序，配置里的顺序只作为**初始先验**。
"""
from __future__ import annotations

import math
import statistics
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from src.core.errors import (
    BRIEF_DEFAULT,
    brief,
)

# 各源能力（用于排序先验与前端展示的口径说明）
SOURCE_CAPABILITIES: dict[str, dict[str, Any]] = {
    "qmt": {
        "label": "迅投QMT",
        "kind": "本机终端",
        "realtime": True,
        "fields": "五档盘口/逐笔/1m-60m/前复权日线",
        "note": "需 XtMiniQmt 运行登录；L2 需券商单独开通",
    },
    "tencent": {
        "label": "腾讯行情",
        "kind": "公网HTTP",
        "realtime": True,
        "fields": "快照(含PE/PB)/分时/分钟K",
        "note": "免 token，作为 QMT 的第一备用",
    },
    "sina": {
        "label": "新浪财经",
        "kind": "公网HTTP",
        "realtime": True,
        "fields": "逐笔成交/概念板块快照",
        "note": "免 token，逐笔兜底",
    },
    "eastmoney": {
        "label": "东方财富",
        "kind": "公网HTTP",
        "realtime": True,
        "fields": "分钟K/板块分钟",
        "note": "本机网络被阻断（实测 RemoteDisconnected），仅在冷却后偶尔重试",
    },
    "tushare": {
        "label": "Tushare Pro",
        "kind": "公网API",
        "realtime": False,
        "fields": "EOD 日线/估值/财务/资金流/涨跌停/停复牌",
        "note": "5000 积分档；当日数据 15:00~16:00 后才入库，不能用于盘中",
    },
}


@dataclass
class SourceStat:
    """单个 (数据源, 用途) 的运行时统计。"""

    source: str
    method: str
    calls: int = 0
    failures: int = 0
    # 终身累计（不随恢复清零）——故障历史必须留痕，否则"这个源历史上靠不靠得住"
    # 就查不出来了；而 calls/failures 是**当前这一段**的口径，用来算成功率。
    total_calls: int = 0
    total_failures: int = 0
    ewma_ms: float | None = None
    last_ms: float | None = None
    samples: list[float] = field(default_factory=list)   # 最近若干次延迟（毫秒）
    last_error: str = ""
    last_success_at: float = 0.0
    last_attempt_at: float = 0.0    # 最近一次真正发起调用的时间（含失败）
    cooldown_until: float = 0.0
    next_probe_at: float = 0.0      # 半开探测的下次放行时间

    # 排序用的延迟：**取滑动窗口的中位数**，不用 EWMA。
    # 原因（实测）：QMT 首次调用要懒加载 xtquant 并连接终端（~1s 级），
    # 用 alpha=0.3 的 EWMA 会把这个冷启动值带着走很久 —— 面板上显示
    # "QMT 110ms"，而它稳定后其实是 0ms，排序会因此把快源排到慢源后面。
    # 中位数对单个异常值不敏感，且样本少于 3 次时干脆不给结论（交给先验顺序）。
    WINDOW = 20

    @property
    def median_ms(self) -> float | None:
        if len(self.samples) < 3:
            return None
        return statistics.median(self.samples)

    def as_dict(self) -> dict[str, Any]:
        capability = SOURCE_CAPABILITIES.get(self.source, {})
        return {
            "source": self.source,
            "label": capability.get("label", self.source),
            "kind": capability.get("kind", ""),
            "realtime": capability.get("realtime", False),
            "method": self.method,
            "calls": self.calls,
            "failures": self.failures,
            "total_calls": self.total_calls,
            "total_failures": self.total_failures,
            "success_rate": round(1 - self.failures / self.calls, 3) if self.calls else None,
            "lifetime_success_rate": (round(1 - self.total_failures / self.total_calls, 3)
                                      if self.total_calls else None),
            "ewma_ms": round(self.ewma_ms, 1) if self.ewma_ms is not None else None,
            "median_ms": (round(self.median_ms, 1)
                          if self.median_ms is not None else None),
            "samples": len(self.samples),
            "last_ms": round(self.last_ms, 1) if self.last_ms is not None else None,
            "last_error": self.last_error[:120],
            "cooling_down": self.cooldown_remaining() > 0,
            "cooldown_seconds": round(self.cooldown_remaining(), 1),
            "half_open_in": (round(max(0.0, self.next_probe_at - time.monotonic()), 1)
                             if self.cooldown_remaining() > 0 else 0.0),
            "note": capability.get("note", ""),
        }

    def cooldown_remaining(self) -> float:
        return max(0.0, self.cooldown_until - time.monotonic())


class SourceHealthTracker:
    """线程安全的源健康表（EWMA 延迟 + 成功率 + **熔断/半开**重试）。

    熔断器三态（实测需求：用户会随手关掉/重开 QMT 终端）：

        closed    正常调用
        open      失败后进入冷却，冷却期内不再调用（避免每次都白等超时）
        half_open 冷却期间每隔 probe_interval 秒**放行一次探测**，
                  成功即立刻恢复（否则用户重开 QMT 后最长要等满 300 秒才被重新使用）

    没有半开机制时，用户关掉 QMT 再打开，面板会显示"QMT 冷却中（剩 280s）"，
    明明终端已经回来了却还在用慢源 —— 这正是实测中暴露的体验问题。
    """

    def __init__(self, *, alpha: float = 0.3, cooldown_seconds: int = 300,
                 probe_interval_seconds: float = 30.0,
                 evidence_ttl_seconds: float = 60.0) -> None:
        self._alpha = alpha
        self._cooldown_seconds = cooldown_seconds
        self._probe_interval = max(1.0, probe_interval_seconds)
        self._evidence_ttl = max(1.0, evidence_ttl_seconds)
        self._stats: dict[tuple[str, str], SourceStat] = {}
        self._lock = threading.Lock()

    # ---------- 熔断状态 ----------

    def should_attempt(self, source: str, method: str) -> tuple[bool, str]:
        """本次是否放行 + 原因（熔断冷却期内的周期性探测走这里）。"""
        with self._lock:
            stat = self._stats.get((source, method))
            if stat is None:
                return True, ""
            remaining = stat.cooldown_remaining()
            if remaining <= 0:
                return True, ""
            if time.monotonic() >= stat.next_probe_at:
                stat.next_probe_at = time.monotonic() + self._probe_interval
                return True, f"半开探测（冷却剩 {remaining:.0f}s）"
            return False, f"熔断冷却中（剩 {remaining:.0f}s）"

    def evidence_age(self, source: str, method: str) -> float:
        """距离上次真正调用该源过了多少秒（用于"探索式刷新证据"）。

        为什么需要它：链路里只要首选源成功就返回，**后面的源永远拿不到调用**，
        于是它的延迟证据会一直停在很久以前 —— 一个因为冷启动变慢而被降级的源
        （如 QMT 首次懒加载 1.2s）就再也回不到第一。所以排序之后还要允许
        "证据过期"的源插到最前刷新一次（默认每 60 秒一个源）。
        """
        with self._lock:
            stat = self._stats.get((source, method))
            if stat is None or stat.last_attempt_at <= 0:
                return float("inf")     # 从未测过
            return time.monotonic() - stat.last_attempt_at

    def explore_refresh(self, order: list[str], method: str) -> list[str]:
        """把"证据过期最久"的非首选源提到最前，让它刷新一次证据。

        每次请求最多插一个，代价可控（默认 60 秒一次额外调用），
        换来的是**任何源都不会因为一次异常而永久失去竞争资格**。
        """
        if len(order) <= 1:
            return order
        candidates = order[1:]
        ages = {name: self.evidence_age(name, method) for name in candidates}
        stale = [name for name in candidates
                 if ages[name] > self._evidence_ttl]
        if not stale:
            return order
        target = max(stale, key=lambda name: ages[name])
        if ages[target] == float("inf"):
            # 从未调用过的源：只有在首选源已经有证据时才插队，
            # 否则启动后的第一个请求会被一个新源抢走（先验顺序本就更可信）
            if all(ages[name] == float("inf") or ages[name] > self._evidence_ttl
                   for name in candidates):
                return order
        return [target] + [name for name in order if name != target]

    # ---------- 记录 ----------

    def record_success(self, source: str, method: str, seconds: float) -> None:
        milliseconds = seconds * 1000.0
        with self._lock:
            stat = self._stats.setdefault((source, method), SourceStat(source, method))
            # 熔断恢复（半开探测成功）：**清空延迟窗口**再记这次。
            # 不清的话，故障期那些慢样本会把 EWMA 拖住（实测：QMT 恢复后 EWMA 仍是
            # 91ms，被腾讯 76ms 压着排第二；而它排在第二就永远轮不到调用 →
            # 排名再也回不来）。清窗后它立刻用新证据重新竞争。
            #
            # **失败计数同样要清**：成功率若是终身累计，一个源故障 3 次后恢复，
            # 面板会永远显示"成功率 25%"，与"它现在是好的"直接矛盾 ——
            # 这张表的用途恰恰是回答"现在能不能信它"。所以 calls/failures 按
            # "当前这一段"计，终身量另存 total_*，历史不丢。
            recovered = stat.cooldown_until > 0
            if recovered:
                stat.samples.clear()
                stat.ewma_ms = None
                stat.calls = 0
                stat.failures = 0
            stat.calls += 1
            stat.total_calls += 1
            stat.last_ms = milliseconds
            stat.last_attempt_at = time.monotonic()
            stat.ewma_ms = (milliseconds if stat.ewma_ms is None
                            else (1 - self._alpha) * stat.ewma_ms
                            + self._alpha * milliseconds)
            stat.samples.append(milliseconds)
            if len(stat.samples) > SourceStat.WINDOW:
                del stat.samples[:-SourceStat.WINDOW]
            stat.last_success_at = time.time()
            stat.last_error = ""
            stat.cooldown_until = 0.0
            stat.next_probe_at = 0.0        # 熔断器闭合（立即恢复为可用）

    def record_failure(self, source: str, method: str, error: str) -> None:
        with self._lock:
            stat = self._stats.setdefault((source, method), SourceStat(source, method))
            stat.calls += 1
            stat.failures += 1
            stat.total_calls += 1
            stat.total_failures += 1
            stat.last_error = brief(error, BRIEF_DEFAULT)
            stat.last_attempt_at = time.monotonic()
            if self._cooldown_seconds > 0:
                stat.cooldown_until = time.monotonic() + self._cooldown_seconds
                # 探测失败 → 重新排下一次 половину探测（不让探测变成高频重试）
                stat.next_probe_at = time.monotonic() + self._probe_interval

    def record_skip(self, source: str, method: str, reason: str) -> None:
        """冷却跳过：不计成功也不计失败，只留原因。"""
        with self._lock:
            stat = self._stats.setdefault((source, method), SourceStat(source, method))
            stat.last_error = f"跳过：{reason}"[:200]

    # ---------- 排序 ----------

    def rank(self, sources: list[str], method: str,
             *, prior: list[str] | None = None) -> list[str]:
        """按「探测优先 → 实测延迟 → 先验」排序。

        分组规则（实测教训：这里最初写成"没实测的排最后"，结果造成**死锁**）：

            0  半开探测到期 → **排最前**，必须让它真的被调用一次
            1  有实测延迟（样本≥3 用中位数，1~2 个样本用 EWMA）→ 按延迟升序
            2  从未调用过 → 按先验顺序（先验只是猜测，但总得试一次才有证据）
            3  冷却中且探测未到期 → 最后

        **为什么"没实测的排最后"是错的**：QMT 失败后进入冷却 → 排到腾讯后面 →
        腾讯每次都能成功返回 → QMT 那一次探测永远轮不到 → 用户重开 QMT 终端后
        排名也永远回不来。所以这里的"探测到期"必须置顶，让恢复探测一定能执行。
        """
        prior_order = prior or sources
        prior_index = {name: index for index, name in enumerate(prior_order)}
        now = time.monotonic()

        def sort_key(name: str) -> tuple:
            with self._lock:
                stat = self._stats.get((name, method))
                if stat is None:
                    return (2, math.inf, prior_index.get(name, 999))
                cooling = stat.cooldown_remaining() > 0
                probe_due = cooling and now >= stat.next_probe_at
                if probe_due:
                    return (0, math.inf, prior_index.get(name, 999))
                if cooling:
                    return (3, math.inf, prior_index.get(name, 999))
                median = stat.median_ms
                if median is not None:
                    return (1, median, prior_index.get(name, 999))
                if stat.ewma_ms is not None:
                    # 1~2 个样本：用 EWMA 作为初值（否则刚恢复的源会因为
                    # "样本不足"被排到慢源后面，等于永远无法重新证明自己）
                    return (1, stat.ewma_ms, prior_index.get(name, 999))
                return (2, math.inf, prior_index.get(name, 999))

        return sorted(sources, key=sort_key)

    # ---------- 快照 ----------

    def snapshot(self, candidates: list[str] | None = None) -> dict[str, Any]:
        """健康表快照 + 每个用途的完整排序（含尚未调用过的候选源）。"""
        with self._lock:
            rows = [stat.as_dict() for stat in self._stats.values()]
        rows.sort(key=lambda row: (row["method"], row["median_ms"] is None,
                                   row["median_ms"] or 0.0))
        pool = candidates or ["qmt", "tencent", "sina", "eastmoney"]
        ranking: dict[str, list[str]] = {}
        methods = {row["method"] for row in rows} | {"quote", "trend", "bars"}
        for method in sorted(methods):
            ranking[method] = self.rank(pool, method, prior=pool)
        return {"sources": rows, "ranking": ranking,
                "measured_at": time.strftime("%Y-%m-%d %H:%M:%S")}

    def reset(self) -> None:
        """测试用：清空统计。"""
        with self._lock:
            self._stats.clear()

    def reset_cooldowns(self) -> None:
        """关闭所有熔断器（不清延迟统计）——「强制刷新」按钮用。

        用户手动刷新时的心智是"我现在就想让它重新试一遍"，
        所以冷却必须立刻清零，而不是继续等 300 秒。
        """
        with self._lock:
            for stat in self._stats.values():
                stat.cooldown_until = 0.0
                stat.next_probe_at = 0.0


def summarize_latency(values: list[float]) -> dict[str, float]:
    """延迟摘要（毫秒）——给基准脚本与文档复用。

    p90 用标准的 nearest-rank 定义：第 ceil(0.9n) 个最小值为 p90
    （n=5 时取第 5 个；而不是 int(0.9n)-1 那个会取到第 4 个的写法）。
    """
    if not values:
        return {}
    ordered = sorted(values)
    rank = max(1, math.ceil(len(ordered) * 0.9))
    return {
        "n": len(values),
        "median_ms": round(statistics.median(ordered) * 1000, 1),
        "p90_ms": round(ordered[rank - 1] * 1000, 1),
        "max_ms": round(ordered[-1] * 1000, 1),
    }
