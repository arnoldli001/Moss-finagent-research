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


def test_mapping_does_not_raise() -> None:
    """最常见的回归：参数名拼错会在构造时直接抛 TypeError。"""
    config = _screen_config_from_request(FakeRequest())
    assert isinstance(config, ScreenConfig)


def test_every_request_field_lands_on_the_right_config_field() -> None:
    """逐个字段验证映射，避免"改了一处、另一处忘改"。"""
    request = FakeRequest(horizon=15, min_ic=0.05, min_icir=0.8,
                          corr_threshold=0.6, train_ratio=0.6,
                          target_count=12, n_groups=7, neutralize=False)
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
