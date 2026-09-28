"""免费档限流熔断护栏的测试。

## 为什么这条护栏是本轮最关键的一项

`light` 层（占 **82%** 调用量）首位已切到免费档 `qwen-siliconflow-7b`，
而它**有限流机制**。没有这条护栏，限流是**不可见**的：
每次调用先白撞一次 429，再落到下一跳 —— 拖慢延迟、白耗下一跳配额。

## 本文件钉住六件事

1. 429 判据只认**机器可读标识**（不认文案）
2. **连续** N 次才锁；中途成功就清零
3. 锁定期内 `is_locked()` 为真（调用方据此**跳过该跳**）
4. **状态落盘** —— 换一个实例仍读得到（AGENTS.md：放内存重启即失效）
5. 锁定期满**自动解锁**（半开重试，不永久弃用）
6. `snapshot()` 区分「未锁定」与「剩余 0 秒」（没量到 ≠ 量到 0）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.infrastructure.llm.rate_limit_guard import (  # noqa: E402
    LOCK_MINUTES,
    LOCK_THRESHOLD,
    RateLimitGuard,
    looks_rate_limited,
)


@pytest.fixture
def guard(tmp_path):
    return RateLimitGuard(path=str(tmp_path / "g.json"))


# ============================================================
# ① 判据只认机器可读标识
# ============================================================


def test_detects_429_by_markers():
    for text in ("429 Too Many Requests", "HTTP 429", "rate limit exceeded",
                 "AllocationQuota.FreeTierOnly", "Throttling"):
        assert looks_rate_limited(RuntimeError(text)), text


def test_does_not_confuse_other_failures_with_rate_limit():
    """超时 / 网络 / 鉴权 **不是**限流 —— 混进来会让正常故障触发锁定。"""
    for text in ("Connection timeout", "401 Unauthorized",
                 "模型输出非合法JSON", "circuit_open"):
        assert not looks_rate_limited(RuntimeError(text)), text


# ============================================================
# ② 连续计数 → 锁定
# ============================================================


def test_locks_after_consecutive_rate_limits(guard):
    m = "qwen-siliconflow-7b"
    assert not guard.is_locked(m)
    for i in range(LOCK_THRESHOLD - 1):
        assert not guard.record_rate_limited(m, "429"), f"第 {i+1} 次不该锁"
    assert guard.record_rate_limited(m, "429"), "达到阈值应锁定"
    assert guard.is_locked(m)


def test_success_resets_the_consecutive_counter(guard):
    """★ "连续"的语义：中途成功一次就清零，否则偶发 429 会累积成误锁。"""
    m = "qwen-dashscope-flash"
    for _ in range(LOCK_THRESHOLD - 1):
        guard.record_rate_limited(m, "429")
    guard.record_success(m)                       # 成功一次
    assert not guard.record_rate_limited(m, "429"), (
        "成功之后计数应清零 —— 否则累计 3 次偶发 429 就会误锁")
    assert not guard.is_locked(m)


def test_total_counters_accumulate_for_observability(guard):
    """总次数只增不减（用于看"这个档历史上限流多不多"）。"""
    m = "m"
    for _ in range(LOCK_THRESHOLD + 2):
        guard.record_rate_limited(m, "429")
    snap = guard.snapshot()["models"][m]
    assert snap["total_429"] == LOCK_THRESHOLD + 2
    assert snap["last_reason"] == "429"


# ============================================================
# ③ 落盘（**AGENTS.md 硬约束**）
# ============================================================


def test_state_survives_a_new_instance(tmp_path):
    """★ 最核心的一条：**状态必须落盘**。

    放进程内存的冷却，重启即失效 —— 本项目实测踩过这个坑。
    """
    path = str(tmp_path / "persist.json")
    g1 = RateLimitGuard(path=path)
    m = "qwen-siliconflow-7b"
    for _ in range(LOCK_THRESHOLD):
        g1.record_rate_limited(m, "429")

    # 模拟进程重启：全新实例，不共享任何内存
    g2 = RateLimitGuard(path=path)
    assert g2.is_locked(m), "重启后锁定状态丢失 —— 落盘没生效"
    assert Path(path).exists()
    assert json.loads(Path(path).read_text(encoding="utf-8"))["models"][m]


def test_corrupt_state_file_does_not_crash(tmp_path):
    """坏文件不该让链路崩（探测失败 ≠ 判为限流）。"""
    p = tmp_path / "bad.json"
    p.write_text("{ this is not json", encoding="utf-8")
    g = RateLimitGuard(path=str(p))
    assert not g.is_locked("m")          # 保守：判不了就不锁
    g.record_rate_limited("m", "429")    # 也不该抛


# ============================================================
# ④ 自动解锁（半开重试）
# ============================================================


def test_lock_expires_automatically(guard):
    """★ 锁定期满自动解锁 —— 否则一个偶发限流窗口会**永久弃用**这个档。"""
    m = "m"
    now = 1_000_000.0
    for _ in range(LOCK_THRESHOLD):
        guard.record_rate_limited(m, "429", now=now)
    assert guard.is_locked(m, now=now + 1)
    assert not guard.is_locked(m, now=now + guard.lock_seconds + 1), (
        "锁定期满仍未解锁 —— 会永久弃用该模型")


def test_lock_duration_is_documented_constant(guard):
    assert guard.lock_seconds == LOCK_MINUTES * 60.0
    assert LOCK_THRESHOLD >= 2, "阈值 1 会让一次偶发 429 就锁定，太激进"


# ============================================================
# ⑤ 可观测：区分「未锁定」与「剩余 0 秒」
# ============================================================


def test_snapshot_distinguishes_not_locked_from_zero():
    """★ 没量到 ≠ 量到 0：未锁定时是 `None`，不是 `0`。"""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        g = RateLimitGuard(path=f"{d}/s.json")
        g.record_success("m")
        snap = g.snapshot()["models"]["m"]
        assert snap["locked"] is False
        assert snap["remaining_s"] is None, (
            "未锁定时 remaining_s 应为 None —— 用 0 糊过去会让人以为"
            "'刚好到期'，而实际是'根本没锁'")


def test_gateway_actually_consults_the_guard():
    """★ 接线判据：`gateway.py` 必须真的查它。

    为什么单测本模块证明不了：全绿，而 gateway 可能压根没调用 ——
    本项目实测过同款（"改了 gateway 但 planner 不传预算"）。
    """
    src = (ROOT / "src" / "infrastructure" / "llm" / "gateway.py"
           ).read_text(encoding="utf-8")
    assert "rate_limit_guard" in src, "gateway 没引用限流熔断模块"
    assert "is_locked(" in src, "gateway 没有跳过锁定模型的逻辑"
    assert "record_rate_limited(" in src, "gateway 没有记录 429"
    assert "record_success(" in src, "gateway 没有在成功时清零计数"
