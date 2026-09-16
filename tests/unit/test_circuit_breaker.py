"""熔断器与幻觉防护单测。"""

import time

from src.infrastructure.llm.circuit_breaker import (
    CircuitBreakerRegistry,
    TimeWindowCircuitBreaker,
)
from src.infrastructure.llm.hallucination_guard import HallucinationGuard


class TestCircuitBreaker:
    def test_closed_allows_request(self) -> None:
        cb = TimeWindowCircuitBreaker("test", failure_threshold=3)
        assert cb.allow_request() is True

    def test_transitions_to_open_after_threshold(self) -> None:
        cb = TimeWindowCircuitBreaker("test", failure_threshold=3, failure_window_sec=60)
        for _ in range(3):
            cb.record_failure()
        assert cb.snapshot()["state"] == "OPEN"
        assert cb.allow_request() is False  # 熔断中

    def test_below_threshold_stays_closed(self) -> None:
        cb = TimeWindowCircuitBreaker("test", failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        assert cb.snapshot()["state"] == "CLOSED"
        assert cb.allow_request() is True

    def test_open_to_half_open_after_cooldown(self) -> None:
        cb = TimeWindowCircuitBreaker(
            "test", failure_threshold=1, recovery_cooldown_sec=0.1,
        )
        cb.record_failure()
        assert cb.snapshot()["state"] == "OPEN"
        time.sleep(0.15)
        assert cb.allow_request() is True  # 进入HALF_OPEN
        assert cb.snapshot()["state"] == "HALF_OPEN"

    def test_half_open_success_recovers_to_closed(self) -> None:
        cb = TimeWindowCircuitBreaker(
            "test", failure_threshold=1, recovery_cooldown_sec=0.1,
            half_open_success_needed=2,
        )
        cb.record_failure()
        time.sleep(0.15)
        cb.allow_request()  # → HALF_OPEN
        cb.record_success()
        cb.record_success()
        assert cb.snapshot()["state"] == "CLOSED"

    def test_half_open_failure_back_to_open(self) -> None:
        cb = TimeWindowCircuitBreaker(
            "test", failure_threshold=1, recovery_cooldown_sec=0.1,
        )
        cb.record_failure()
        time.sleep(0.15)
        cb.allow_request()  # → HALF_OPEN
        cb.record_failure()  # 探测失败
        assert cb.snapshot()["state"] == "OPEN"

    def test_failure_window_gc(self) -> None:
        cb = TimeWindowCircuitBreaker(
            "test", failure_threshold=3, failure_window_sec=0.1,
        )
        cb.record_failure()
        time.sleep(0.15)
        cb.record_failure()
        assert cb.snapshot()["state"] == "CLOSED"  # 窗口外失败已GC

    def test_registry_get_or_create(self) -> None:
        reg = CircuitBreakerRegistry()
        cb1 = reg.get_or_create("deepseek")
        cb2 = reg.get_or_create("deepseek")
        assert cb1 is cb2
        assert reg.get_or_create("ollama") is not cb1
        assert "deepseek" in reg.snapshot_all()
        assert "ollama" in reg.snapshot_all()


class TestHallucinationGuard:
    def test_passes_when_numbers_in_input(self) -> None:
        output = "CPI 2.1%，符合预期"
        context = "## 输入数据\nCPI 2.1%\n"
        report = HallucinationGuard.verify(output, context)
        assert report.passed is True
        assert report.unverified_numbers == []

    def test_flags_number_not_in_input(self) -> None:
        output = "PPI 3.5%，超出预期"
        context = "## 输入数据\nCPI 2.1%\n"
        report = HallucinationGuard.verify(output, context)
        assert report.passed is False
        assert any("3.5" in n for n in report.unverified_numbers)

    def test_flags_stock_code_not_in_input(self) -> None:
        output = "建议关注600519"
        context = "## 输入数据\n股票000001\n"
        report = HallucinationGuard.verify(output, context)
        assert report.passed is False
        assert "600519" in report.unverified_stock_codes

    def test_passes_stock_code_in_input(self) -> None:
        output = "600519基本面良好"
        context = "## 输入数据\n600519 贵州茅台"
        report = HallucinationGuard.verify(output, context)
        assert report.passed is True

    def test_empty_output_passes(self) -> None:
        report = HallucinationGuard.verify("", "some context")
        assert report.passed is True

    def test_confidence_decreases_with_issues(self) -> None:
        output = "PE 50倍，PB 8倍，建议关注600999"
        context = "## 输入数据\n股票000001\n"
        report = HallucinationGuard.verify(output, context)
        assert report.confidence < 1.0
        assert report.passed is False

    def test_render_warning(self) -> None:
        report = HallucinationGuard.verify(
            "PE 50倍", "## 输入数据\n股票000001\n",
        )
        assert report.render_warning() != ""
        assert "幻觉防护" in report.render_warning()

    def test_render_warning_empty_when_passed(self) -> None:
        report = HallucinationGuard.verify("600519", "600519")
        assert report.render_warning() == ""
