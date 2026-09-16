"""数据源健康与延迟排序单测（离线）。

实测背景（2026-09-16 复测，QMT mini 重开之后，每项 20 次采样）：
    用途    源        中位      p90
    quote   QMT       **0.2 ms**   0.3 ms   （终端内存读取）
    quote   腾讯      53.6 ms      61.7 ms
    bars    QMT       **8.0 ms**   11.9 ms
    bars    腾讯      97.5 ms      104.9 ms
    trend   QMT       **15.5 ms**  18.0 ms
    trend   腾讯      75.8 ms      126.8 ms
    新浪逐笔   全部失败（本机 IP 被新浪封禁，HTTP 456「拒绝访问」）
    东财       全部失败（本机网络被阻断，RemoteDisconnected）

因此排序必须是"测量驱动 + 可自动降级 + 能自动恢复"，而不是写死配置顺序。
"""
from __future__ import annotations

import time

import pytest

from src.intraday.source_health import (
    SOURCE_CAPABILITIES,
    SourceHealthTracker,
    summarize_latency,
)

POOL = ["qmt", "tencent", "sina", "eastmoney"]


def test_capability_matrix_marks_realtime_sources() -> None:
    """实时能力是做T的分水岭：Tushare 必须被标成非实时。"""
    assert SOURCE_CAPABILITIES["qmt"]["realtime"] is True
    assert SOURCE_CAPABILITIES["tencent"]["realtime"] is True
    assert SOURCE_CAPABILITIES["tushare"]["realtime"] is False
    assert "EOD" in SOURCE_CAPABILITIES["tushare"]["fields"]


def test_rank_without_measurement_uses_prior() -> None:
    tracker = SourceHealthTracker()
    assert tracker.rank(POOL, "quote", prior=POOL) == POOL


def test_few_samples_compete_via_ewma(monkeypatch) -> None:
    """样本 1~2 个也要参与竞争（用 EWMA 作初值）。

    这条规则和"样本不足不重排"是权衡后的选择：早期版本要求"≥3 样本才参与排序"，
    结果**熔断恢复的源因为样本被清空而排到最后，永远拿不到调用 → 排名死锁**。
    现在的兜底是：1~2 样本用 EWMA 先参与，同时用探索式刷新（见下一条）保证
    被降级的源总能拿到新证据。
    """
    tracker = SourceHealthTracker()
    tracker.record_success("tencent", "quote", 0.001)   # 只有 2 个样本
    tracker.record_success("tencent", "quote", 0.001)
    assert tracker.rank(POOL, "quote", prior=POOL)[0] == "tencent"


def test_explore_refresh_gives_stale_source_a_chance(monkeypatch) -> None:
    """任何源都不该因为一次异常永久失去竞争资格。

    链路里首选源成功就返回，后面的源拿不到调用 → 证据永远停在过去。
    探索式刷新把"证据过期最久"的源插到最前，每 60 秒一次。
    """
    tracker = SourceHealthTracker(evidence_ttl_seconds=60)
    base = time.monotonic()
    # QMT 先被调用过（证据是"很久以前"），随后被腾讯超过
    monkeypatch.setattr(time, "monotonic", lambda: base)
    tracker.record_success("qmt", "quote", 0.002)
    tracker.record_failure("qmt", "quote", "短暂抖动")
    tracker.record_success("qmt", "quote", 0.002)       # 熔断恢复
    tracker.record_success("tencent", "quote", 0.05)
    tracker.record_success("tencent", "quote", 0.05)
    tracker.record_success("tencent", "quote", 0.05)

    # 90 秒后：QMT 的证据已过期 → 应该被插到最前刷新一次
    monkeypatch.setattr(time, "monotonic", lambda: base + 90)
    ranked = tracker.rank(POOL, "quote", prior=POOL)
    refreshed = tracker.explore_refresh(ranked, "quote")
    assert refreshed[0] in {"qmt", "sina", "eastmoney"}, refreshed
    assert tracker.evidence_age("qmt", "quote") > 60

    # 刚调用过的源不会被反复插队
    tracker.record_success("qmt", "quote", 0.002)
    ranked_after = tracker.rank(POOL, "quote", prior=POOL)
    assert tracker.explore_refresh(ranked_after, "quote")[0] == ranked_after[0] \
        or tracker.evidence_age(ranked_after[0], "quote") <= 60


def test_rank_prefers_measured_faster_source() -> None:
    tracker = SourceHealthTracker()
    for value in (0.0005, 0.0004, 0.0006):        # QMT ≈0.5ms
        tracker.record_success("qmt", "quote", value)
    for value in (0.054, 0.056, 0.052):           # 腾讯 ≈54ms
        tracker.record_success("tencent", "quote", value)
    order = tracker.rank(POOL, "quote", prior=POOL)
    assert order[0] == "qmt" and order[1] == "tencent"


def test_cold_start_outlier_does_not_poison_ranking() -> None:
    """QMT 首次调用要懒加载 xtquant/连终端（~1s），中位数必须把它吸收掉。

    这正是从 EWMA 改成"滑动窗口中位数"的原因：EWMA 会把冷启动值带很久，
    面板上显示 QMT 110ms、排序把快源排到慢源后面。
    """
    tracker = SourceHealthTracker()
    tracker.record_success("qmt", "quote", 1.2)    # 冷启动
    for value in (0.001, 0.001, 0.001, 0.001):
        tracker.record_success("qmt", "quote", value)
    for value in (0.054, 0.055, 0.053):
        tracker.record_success("tencent", "quote", value)
    snapshot = tracker.snapshot(POOL)
    qmt_row = next(row for row in snapshot["sources"]
                   if row["source"] == "qmt" and row["method"] == "quote")
    assert qmt_row["median_ms"] is not None and qmt_row["median_ms"] < 5
    assert qmt_row["ewma_ms"] > 100, "EWMA 会被冷启动带偏（所以排序不用它）"
    assert tracker.rank(POOL, "quote", prior=POOL)[0] == "qmt"


def test_failure_demotes_source_to_the_end() -> None:
    """QMT 掉线（终端未启动）→ 自动让位腾讯；恢复后回到第一。"""
    tracker = SourceHealthTracker(cooldown_seconds=300)
    for value in (0.001, 0.001, 0.001):
        tracker.record_success("qmt", "quote", value)
    for value in (0.054, 0.054, 0.054):
        tracker.record_success("tencent", "quote", value)
    assert tracker.rank(POOL, "quote", prior=POOL)[0] == "qmt"

    tracker.record_failure("qmt", "quote", "XtMiniQmt 未启动")
    order = tracker.rank(POOL, "quote", prior=POOL)
    assert order[0] == "tencent", "失败的源必须让位"
    assert order[-1] == "qmt", "冷却中的源排最后但保留（不能从候选里消失）"

    tracker.record_success("qmt", "quote", 0.001)    # 恢复
    assert tracker.rank(POOL, "quote", prior=POOL)[0] == "qmt"


def test_success_rate_and_snapshot_fields() -> None:
    tracker = SourceHealthTracker(cooldown_seconds=60)
    tracker.record_success("qmt", "bars", 0.008)
    tracker.record_success("qmt", "bars", 0.009)
    tracker.record_failure("qmt", "bars", "超时")
    snapshot = tracker.snapshot(POOL)
    row = next(item for item in snapshot["sources"]
               if item["source"] == "qmt" and item["method"] == "bars")
    assert row["calls"] == 3 and row["failures"] == 1
    assert row["success_rate"] == round(2 / 3, 3)
    assert row["cooling_down"] is True
    assert row["label"] == "迅投QMT"


def test_snapshot_ranks_every_method_over_full_pool() -> None:
    """排序要覆盖全部候选源（含尚未调用过的），前端才能画出完整回退链。"""
    tracker = SourceHealthTracker()
    tracker.record_success("qmt", "quote", 0.001)
    snapshot = tracker.snapshot(POOL)
    assert set(snapshot["ranking"]) >= {"quote", "trend", "bars"}
    for method, order in snapshot["ranking"].items():
        assert set(order) == set(POOL), method


def test_record_skip_does_not_count_as_call() -> None:
    """冷却跳过不是失败：不能污染成功率，但要留下原因。"""
    tracker = SourceHealthTracker()
    tracker.record_skip("tencent", "quote", "失败冷却中")
    snapshot = tracker.snapshot(POOL)
    row = next(item for item in snapshot["sources"]
               if item["source"] == "tencent" and item["method"] == "quote")
    assert row["calls"] == 0 and row["failures"] == 0
    assert "跳过" in row["last_error"]


def test_reset_clears_stats() -> None:
    tracker = SourceHealthTracker()
    tracker.record_success("qmt", "quote", 0.001)
    tracker.reset()
    assert tracker.snapshot(POOL)["sources"] == []


def test_summarize_latency() -> None:
    summary = summarize_latency([0.001, 0.002, 0.054, 0.056, 0.062])
    assert summary["n"] == 5
    assert summary["median_ms"] == 54.0
    assert summary["p90_ms"] == 62.0
    assert summarize_latency([]) == {}


def test_cooldown_expires(monkeypatch) -> None:
    tracker = SourceHealthTracker(cooldown_seconds=1)
    tracker.record_failure("qmt", "quote", "挂了")
    assert tracker.rank(POOL, "quote", prior=POOL)[-1] == "qmt"
    # 让冷却到期（把 monotonic 往后推 10 秒）
    base = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: base + 10)
    assert tracker.rank(POOL, "quote", prior=POOL)[0] == "qmt"


# ==================== 熔断器的半开重试（实测需求） ====================


def test_half_open_probe_allows_recovery_within_cooldown(monkeypatch) -> None:
    """用户随手关掉再打开 QMT 终端后，不该再干等满 300 秒冷却。

    实测场景：QMT 失败 → 进入 300s 冷却 → 用户 10 秒后重开终端 →
    没有半开机制时面板会一直显示"QMT 冷却中（剩 290s）"，明明终端已回来。
    """
    tracker = SourceHealthTracker(cooldown_seconds=300, probe_interval_seconds=30)
    tracker.record_failure("qmt", "quote", "XtMiniQmt 未启动")

    # 刚失败：冷却期内不放行
    allowed, reason = tracker.should_attempt("qmt", "quote")
    assert allowed is False and "熔断冷却中" in reason

    # 30 秒后：放行一次半开探测
    base = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: base + 31)
    allowed, reason = tracker.should_attempt("qmt", "quote")
    assert allowed is True and "半开探测" in reason

    # 紧接着的第二次请求：探测已经排过，不再放行（避免变成高频重试）
    allowed, _ = tracker.should_attempt("qmt", "quote")
    assert allowed is False

    # 探测成功 → 熔断器立刻闭合，恢复正常可用
    tracker.record_success("qmt", "quote", 0.001)
    assert tracker.should_attempt("qmt", "quote")[0] is True
    assert tracker.rank(POOL, "quote", prior=POOL)[0] == "qmt"


def test_half_open_probe_failure_reschedules(monkeypatch) -> None:
    """探测失败 → 延长冷却并按探测间隔重排，不会退化成每次请求都重试。"""
    tracker = SourceHealthTracker(cooldown_seconds=300, probe_interval_seconds=30)
    tracker.record_failure("qmt", "quote", "第一次失败")
    base = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: base + 31)
    assert tracker.should_attempt("qmt", "quote")[0] is True

    monkeypatch.setattr(time, "monotonic", lambda: base + 32)
    tracker.record_failure("qmt", "quote", "探测仍失败")
    monkeypatch.setattr(time, "monotonic", lambda: base + 40)
    assert tracker.should_attempt("qmt", "quote")[0] is False
    monkeypatch.setattr(time, "monotonic", lambda: base + 65)
    assert tracker.should_attempt("qmt", "quote")[0] is True


def test_half_open_state_is_visible_in_snapshot() -> None:
    """面板要能看出"还有多久会再试一次"。"""
    tracker = SourceHealthTracker(cooldown_seconds=300, probe_interval_seconds=30)
    tracker.record_failure("qmt", "trend", "终端掉线")
    row = next(item for item in tracker.snapshot(POOL)["sources"]
               if item["source"] == "qmt" and item["method"] == "trend")
    assert row["cooling_down"] is True
    assert 0 < row["half_open_in"] <= 30


# ==================== 排序死锁（实测踩坑，两个都不能少） ====================


def test_probe_due_ranks_first_not_last(monkeypatch) -> None:
    """死锁修复 ①：探测到期的源必须**排最前**，否则永远轮不到它被调用。

    实测场景：QMT 失败进冷却 → 排到腾讯后面 → 腾讯每次都成功返回 →
    QMT 的探测永远执行不到 → 用户重开 QMT 终端后排名也回不来。
    """
    tracker = SourceHealthTracker(cooldown_seconds=300, probe_interval_seconds=30)
    for value in (0.05, 0.05, 0.05):
        tracker.record_success("tencent", "quote", value)
    tracker.record_failure("qmt", "quote", "XtMiniQmt 未启动")

    # 冷却期内且探测未到期 → 排最后
    assert tracker.rank(POOL, "quote", prior=POOL)[-1] == "qmt"
    # 探测到期 → 排最前（必须让它真的被调用一次）
    base = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: base + 31)
    assert tracker.rank(POOL, "quote", prior=POOL)[0] == "qmt"


def test_recovery_resets_latency_window(monkeypatch) -> None:
    """死锁修复 ②：熔断恢复时清空延迟窗口，否则故障期的慢样本会把源永久压在后面。

    实测：QMT 恢复后 EWMA 仍是 91ms（被故障期 131ms 的样本拖着），
    被腾讯 76ms 压着排第二；而它排第二就永远拿不到调用 → 排名死锁。
    """
    tracker = SourceHealthTracker(cooldown_seconds=300, probe_interval_seconds=30)
    for value in (0.13, 0.14, 0.12):          # 故障前偏慢
        tracker.record_success("qmt", "quote", value)
    for value in (0.05, 0.05, 0.05):
        tracker.record_success("tencent", "quote", value)
    assert tracker.rank(POOL, "quote", prior=POOL)[0] == "tencent"

    tracker.record_failure("qmt", "quote", "终端掉线")
    base = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: base + 31)
    assert tracker.rank(POOL, "quote", prior=POOL)[0] == "qmt"   # 探测到期

    tracker.record_success("qmt", "quote", 0.001)                # 探测成功（已恢复）
    row = next(item for item in tracker.snapshot(POOL)["sources"]
               if item["source"] == "qmt" and item["method"] == "quote")
    assert row["samples"] == 1, "恢复后应只保留新样本（清空故障期窗口）"
    assert row["ewma_ms"] == pytest.approx(1.0), "EWMA 也不该被故障期样本拖着"
    assert tracker.rank(POOL, "quote", prior=POOL)[0] == "qmt", \
        "用新证据（1ms）应立刻回到第一"


def test_few_samples_still_compete_with_measured_slow_source() -> None:
    """样本 1~2 个也要参与竞争（用 EWMA 作初值）：刚恢复的源不能因"样本不足"被冷藏。"""
    tracker = SourceHealthTracker()
    for value in (0.08, 0.08, 0.08):
        tracker.record_success("tencent", "quote", value)
    tracker.record_success("qmt", "quote", 0.001)     # 只有 1 个样本
    assert tracker.rank(POOL, "quote", prior=POOL)[0] == "qmt"


# ==================================================================
# 成功率口径：本段 vs 终身
# ==================================================================


def _row(tracker: SourceHealthTracker, source: str, method: str = "bars") -> dict:
    return next(item for item in tracker.snapshot(POOL)["sources"]
                if item["source"] == source and item["method"] == method)


def test_recovery_resets_era_counters_but_keeps_lifetime() -> None:
    """恢复后成功率必须回到 100%，但故障历史不能被抹掉。

    这是端到端容灾验证里发现的问题：`record_success` 恢复时清了延迟窗口，
    却没清失败计数，于是"故障 3 次后恢复"的源在面板上**永远**显示成功率 25%
    —— 与"它现在是好的"直接矛盾。这张表存在的意义恰恰是回答"现在能不能信它"。
    """
    tracker = SourceHealthTracker(cooldown_seconds=300, probe_interval_seconds=30)
    for _ in range(3):
        tracker.record_success("qmt", "bars", 0.008)
    for _ in range(3):
        tracker.record_failure("qmt", "bars", "XtMiniQmt 未运行或未登录")

    broken = _row(tracker, "qmt")
    assert broken["success_rate"] == pytest.approx(0.5)      # 3 成功 / 6 次
    assert broken["cooling_down"] is True

    tracker.record_success("qmt", "bars", 0.008)             # 半开探测成功 = 恢复
    fixed = _row(tracker, "qmt")
    assert fixed["cooling_down"] is False
    assert fixed["success_rate"] == pytest.approx(1.0), \
        "恢复后本段成功率必须回到 100%（否则面板永远显示故障期的低值）"
    assert fixed["calls"] == 1, "本段调用计数应从恢复那一刻重新开始"
    assert fixed["failures"] == 0
    assert fixed["total_failures"] == 3, "终身失败次数必须保留（故障历史不能丢）"
    assert fixed["total_calls"] == 7, "终身调用次数 = 3 成功 + 3 失败 + 1 恢复"
    assert fixed["lifetime_success_rate"] < 1.0, "终身成功率应如实反映历史故障"


def test_failures_keep_counting_without_recovery() -> None:
    """没有恢复就不该清零：连续失败时本段与终身计数同步增长。"""
    tracker = SourceHealthTracker(cooldown_seconds=300)
    for _ in range(5):
        tracker.record_failure("sina", "bars", "返回非 JSON")
    row = _row(tracker, "sina")
    assert row["calls"] == row["total_calls"] == 5
    assert row["failures"] == row["total_failures"] == 5
    assert row["success_rate"] == pytest.approx(0.0)


def test_skip_does_not_count_as_call_or_failure() -> None:
    """熔断跳过既不算成功也不算失败 —— 否则"被跳过的源"成功率会被越算越低。"""
    tracker = SourceHealthTracker(cooldown_seconds=300)
    tracker.record_success("qmt", "bars", 0.008)
    before = _row(tracker, "qmt")
    tracker.record_skip("qmt", "bars", "熔断冷却中（剩 300s）")
    after = _row(tracker, "qmt")
    assert after["calls"] == before["calls"]
    assert after["failures"] == before["failures"]
    assert after["total_calls"] == before["total_calls"]


def test_probe_slot_is_reserved_once_per_interval(monkeypatch) -> None:
    """探测名额每次放行后要推后一个周期：否则并发请求会一起涌向正在恢复的源。

    实测教训：端到端验证时用 `should_attempt` 轮询"探测到期没有"，
    结果轮询本身把名额吃掉了，导致恢复探测根本没发生。
    """
    tracker = SourceHealthTracker(cooldown_seconds=300, probe_interval_seconds=30)
    tracker.record_failure("qmt", "bars", "掉线")
    base = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: base + 31)
    first, reason = tracker.should_attempt("qmt", "bars")
    second, _ = tracker.should_attempt("qmt", "bars")
    assert first is True and "半开探测" in reason
    assert second is False, "同一探测周期内只放行一次"
    monkeypatch.setattr(time, "monotonic", lambda: base + 62)
    third, _ = tracker.should_attempt("qmt", "bars")
    assert third is True, "下一个周期应再次放行"
