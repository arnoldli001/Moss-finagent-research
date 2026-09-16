"""个股微调：按代码覆盖权重/阈值/档位，且字段名写错必须报错。

## 为什么需要（用户诉求 2026-09-16）

> 几乎每个股都有红色实心倒三角出现止损信号，而实际都是很好的低吸位置点，
> 如何调节做T权重，或针对个股做微调？

根因是"一套全局参数覆盖所有票"：300308 的日线 ATR 占价 5.8%，默认 1% 的止损
距离只有 9 元，任何噪声都能打穿；而 600036 的 ATR 只占 1.7%，1% 反而是合适的。
所以要能给单只票微调，并且**改了什么必须在面板上看得见**（两套口径混着看很危险）。
"""

from __future__ import annotations

import pytest

from src.core.exceptions import ConfigError
from src.intraday.config import CodeOverride, IntradayConfig, load_intraday_config


def test_no_override_returns_same_config() -> None:
    """没配微调的标的原样返回（不能白白深拷贝一份，也别改变调用方拿到的对象）。"""
    config = IntradayConfig()
    assert config.for_code("600036") is config
    assert config.override_for("600036") is None


def test_override_applies_only_listed_fields() -> None:
    """只覆盖写了的项，其余沿用全局。"""
    config = IntradayConfig()
    config.overrides["300308"] = CodeOverride(
        weights={"boll": 4.0}, thresholds={"action": 35.0},
        levels={"stop_loss_pct": 2.0, "atr_stop_mult": 0.8})
    scoped = config.for_code("300308")

    assert scoped.weights.boll == pytest.approx(4.0)
    assert scoped.weights.box == config.weights.box          # 未列出的保持全局
    assert scoped.thresholds.action == pytest.approx(35.0)
    assert scoped.thresholds.hint == config.thresholds.hint
    assert scoped.levels.stop_loss_pct == pytest.approx(2.0)
    assert scoped.levels.atr_stop_mult == pytest.approx(0.8)
    assert scoped.levels.take_profit_buffer_pct == config.levels.take_profit_buffer_pct
    # 全局配置**不能被改到**（深拷贝）
    assert config.weights.boll != pytest.approx(4.0)
    assert config.levels.stop_loss_pct == pytest.approx(1.0)


def test_override_does_not_leak_to_other_codes() -> None:
    config = IntradayConfig()
    config.overrides["300308"] = CodeOverride(thresholds={"action": 40.0})
    assert config.for_code("600036") is config
    assert config.for_code("600036").thresholds.action == config.thresholds.action


def test_unknown_field_is_a_config_error() -> None:
    """字段名写错要**直接报错**：静默忽略会变成"改了却没生效"这种最难查的问题。"""
    with pytest.raises(ConfigError) as excinfo:
        IntradayConfig(overrides={"300308": {"weights": {"boll_band": 8.0}}})
    assert "boll_band" in str(excinfo.value)

    with pytest.raises(ConfigError):
        IntradayConfig(overrides={"300308": {"thresholds": {"actions": 30}}})
    with pytest.raises(ConfigError):
        IntradayConfig(overrides={"300308": {"levels": {"stop_loss": 2.0}}})


def test_override_key_must_be_six_digit_code() -> None:
    with pytest.raises(ConfigError) as excinfo:
        IntradayConfig(overrides={"30030": {"thresholds": {"action": 30}}})
    assert "6位" in str(excinfo.value)


def test_empty_override_behaves_like_none() -> None:
    config = IntradayConfig()
    config.overrides["300308"] = CodeOverride()
    assert config.for_code("300308") is config
    assert CodeOverride().is_empty() is True


def test_describe_is_human_readable() -> None:
    override = CodeOverride(
        weights={"boll": 8.0}, thresholds={"action": 35.0},
        levels={"atr_stop_mult": 0.8})
    text = override.describe()
    assert "权重" in text and "boll=8" in text
    assert "阈值" in text and "action=35" in text
    assert "档位" in text and "atr_stop_mult=0.8" in text


def test_config_file_documents_overrides() -> None:
    """部署配置里要有可照抄的示例（用户不需要翻代码猜字段名）。"""
    from pathlib import Path

    text = Path("configs/intraday.yaml").read_text(encoding="utf-8")
    assert "overrides" in text
    assert "atr_stop_mult" in text


def test_snapshot_reports_active_override() -> None:
    """生效时必须在 gaps 里说明 —— 同一只票两套参数下结论不同，用户得知道看的是哪套。"""
    import asyncio

    from src.intraday.service import IntradayService

    service = IntradayService(backend=None)
    service._config = service._config.model_copy(deep=True)  # noqa: SLF001
    # `_reload_config()` 按对象身份判断是否热重载，而上面做了深拷贝 → 它会用文件里的
    # 配置覆盖掉我们注入的这份（测试里必须把它钉住）
    service._reload_config = lambda: service._config  # type: ignore[method-assign]  # noqa: SLF001
    service._config.overrides["300308"] = CodeOverride(  # noqa: SLF001
        thresholds={"action": 35.0})
    # 数据源全部不可用也能拿到快照（缺口如实记录），重点看那条微调说明
    snapshot = asyncio.run(service.snapshot("300308", light=True))
    assert any("个股微调" in gap and "300308" in gap for gap in snapshot.health.gaps)
    assert snapshot.scorecard is not None
    # 阈值确实换成了微调后的值
    assert snapshot.scorecard.threshold_action == pytest.approx(35.0)


def test_load_config_without_overrides_still_works() -> None:
    """部署配置没写 overrides 时照常加载（向后兼容）。"""
    config = load_intraday_config(force=True)
    assert isinstance(config.overrides, dict)
