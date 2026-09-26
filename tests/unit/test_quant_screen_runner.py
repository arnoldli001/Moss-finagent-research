"""筛选子进程的**请求 → 配置映射**单测（不需要数据、不联网）。

存在的理由：这是一个真实事故的回归测试。
`ScreenConfig(min_icr=...)` 把 `min_icir` 拼错了一个字母，后果是
用户点「开始筛选」**每次必崩**：

    TypeError: ScreenConfig.__init__() got an unexpected keyword argument 'min_icr'
    Did you mean 'min_ic'?

而这类错误只要**真的跑一次**就会暴露，可它偏偏在装配面板之后才执行 ——
几分钟的等待换来一句 TypeError。把映射抽成独立函数后，
这里可以在毫秒级覆盖全部字段，包括"有没有漏掉某个参数"。
"""
from __future__ import annotations

import inspect
from typing import Any

import pytest

from src.quant.screen_runner import _screen_config_from_request
from src.quant.screening import ScreenConfig


class FakeRequest:
    """只用属性访问，避免依赖 pydantic（保持测试与 API 层解耦）。"""

    def __init__(self, **kwargs: Any) -> None:
        self.horizon = kwargs.get("horizon", 20)
        self.min_ic = kwargs.get("min_ic", 0.02)
        self.min_icir = kwargs.get("min_icir", 0.3)
        self.corr_threshold = kwargs.get("corr_threshold", 0.7)
        self.train_ratio = kwargs.get("train_ratio", 0.7)
        self.target_count = kwargs.get("target_count", 20)
        self.n_groups = kwargs.get("n_groups", 5)
        self.neutralize = kwargs.get("neutralize", True)
        self.exclude_st = kwargs.get("exclude_st", False)


def test_mapping_does_not_raise() -> None:
    """最常见的回归：参数名拼错会在构造时直接抛 TypeError。"""
    config = _screen_config_from_request(FakeRequest())
    assert isinstance(config, ScreenConfig)


def test_every_request_field_lands_on_the_right_config_field() -> None:
    """逐个字段验证映射，避免"改了一处、另一处忘改"。"""
    request = FakeRequest(horizon=15, min_ic=0.05, min_icir=0.8,
                          corr_threshold=0.6, train_ratio=0.6,
                          target_count=12, n_groups=7, neutralize=False,
                          exclude_st=True)
    config = _screen_config_from_request(request)
    assert config.ic_horizon == 15
    assert config.min_ic == pytest.approx(0.05)
    assert config.min_icir == pytest.approx(0.8), \
        "min_icir 必须映射到同名参数（历史 bug：写成了 min_icr）"
    assert config.corr_threshold == pytest.approx(0.6)
    assert config.train_ratio == pytest.approx(0.6)
    assert config.target_count == 12
    assert config.n_groups == 7
    assert config.neutralize_mv is False
    assert config.exclude_st is True


def test_no_unknown_keyword_is_passed() -> None:
    """反向检查：映射里用到的关键字必须都是 ScreenConfig 的真实字段。

    这条比逐字段断言更耐用 —— 以后往映射里加参数时，
    拼错名字会在这里被立刻抓到，而不是等用户点按钮。
    """
    source = inspect.getsource(_screen_config_from_request)
    body = source.split("ScreenConfig(", 1)[1]
    keywords = {
        line.strip().split("=", 1)[0]
        for line in body.splitlines()
        if "=" in line and not line.strip().startswith(("#", ")", '"'))
    }
    known = set(ScreenConfig.__dataclass_fields__)
    unknown = {name for name in keywords if name and name not in known}
    assert not unknown, f"映射里出现 ScreenConfig 不存在的字段：{unknown}"


def test_untouched_fields_keep_their_defaults() -> None:
    """接口没暴露的字段应保持默认值，而不是被意外覆盖成 None。"""
    config = _screen_config_from_request(FakeRequest())
    defaults = ScreenConfig()
    assert config.neutralize_industry == defaults.neutralize_industry
    assert config.winsorize == defaults.winsorize


# ============== 区间 / 内存护栏 ==============
# 存在的理由：筛选是子进程执行，OOM 只死子进程、服务没事 —— 但用户要等
# 几十分钟才发现任务没了。提前几秒拒绝，比"跑到一半被 OOM 杀掉"有用得多。
# 真实数字（2026-09-25 实测）：171 个交易日 ≈ 196 秒；660 个交易日 ≈ 10.3 分钟
# / 峰值 5088 MB。按行数外推到 2015 年起（约 2600 天）≈ 20 GB → 必须拒绝。


def test_estimate_is_calibrated_to_measured_runs() -> None:
    """模型对**分阶段实测**校准（663 天 × 5320 只，2026-09-25）。

    实测：17 字段整条链 5,406 MB；43 字段整条链 5,925 MB。
    两条性质必须同时成立：

    1. **估算 ≥ 实测**（护栏宁可严一点，低估会让子进程跑到一半被 OOM 杀掉）；
    2. 也不能夸张到把能跑的区间拒掉（≤ +40%）。
    """
    from src.quant.screen_runner import estimate_panel_mb

    days = [str(index) for index in range(663)]
    lazy = estimate_panel_mb(days, universe=5320, fields=17)
    eager = estimate_panel_mb(days, universe=5320, fields=43)
    assert 5406 <= lazy <= 5406 * 1.4, f"17 字段估算 {lazy:.0f} vs 实测 5406"
    assert 5925 <= eager <= 5925 * 1.4, f"43 字段估算 {eager:.0f} vs 实测 5925"
    # 字段越多越贵、区间越长越贵
    assert eager > lazy
    assert estimate_panel_mb(days * 2, universe=5320, fields=17) > lazy


def test_estimate_includes_the_screening_workflow_not_just_the_panel() -> None:
    """护栏算的是**整条链**，不只是面板。

    这条是上一版的教训：只算面板会得出"17 个字段只要 2.4 GB"，而实测整条链
    5.4 GB —— 少算的部分（相关性秩矩阵、中性化复制、IC/分组中间对象）
    恰恰是长区间的主要开销。用一个"字段数为 0"的极端值把流程项暴露出来。
    """
    from src.quant.screen_runner import estimate_panel_mb

    days = [str(index) for index in range(663)]
    no_fields = estimate_panel_mb(days, universe=5320, fields=0)
    assert no_fields > 3000, f"流程项没被计进去：{no_fields:.0f} MB"


def test_guard_rejects_oversized_window(monkeypatch) -> None:
    from src.quant import screen_runner

    monkeypatch.delenv("MOSS_SCREEN_MAX_PANEL_MB", raising=False)
    with pytest.raises(ValueError) as excinfo:
        screen_runner.guard_panel_budget([str(index) for index in range(2600)])
    message = str(excinfo.value)
    assert "预计占用" in message and "超过预算" in message
    assert "MOSS_SCREEN_MAX_PANEL_MB" in message      # 必须给出放宽的办法
    assert "GB" in message


def test_guard_passes_default_window_and_can_be_widened(monkeypatch) -> None:
    from src.quant import screen_runner

    monkeypatch.delenv("MOSS_SCREEN_MAX_PANEL_MB", raising=False)
    note = screen_runner.guard_panel_budget([str(index) for index in range(660)])
    assert "内存预算体检" in note

    # 显式放宽预算后，同样的区间就不再被拒（机器内存足够时的逃生门）
    monkeypatch.setenv("MOSS_SCREEN_MAX_PANEL_MB", "32768")
    assert screen_runner.guard_panel_budget(
        [str(index) for index in range(2600)])

    # 非法值不能把预算变成 0/负数（否则所有区间都会被杀）
    monkeypatch.setenv("MOSS_SCREEN_MAX_PANEL_MB", "not-a-number")
    expected = screen_runner.DEFAULT_PANEL_BUDGET_MB
    assert screen_runner.panel_budget_mb() == expected
