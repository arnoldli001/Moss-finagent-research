"""`auto_recommend` 的层尺度算式单测 —— V2.3 改动的核心。

## 为什么值得单独测

V2.2 的 `layer_weights` 写着 `50/50`，但候选池内两层分数的**尺度差约 3 倍**
（`six_dim` sd≈4.5，`accumulation` sd≈13.1）。两层原始分直接相加时，
每一项的典型幅度是 `w_i·sd_i`，于是六维层实际只推动约 **1/4** 的分数、
合成方差只占约 **1/10** —— "50/50"实际是"约 26/74"（按 `w·sd`）
或"约 11/89"（按方差）。

这个错误**不会报任何错**：配置里写着 50/50、日志里写着 50/50。
唯一能发现它的方式就是把两层的尺度代进这个算式。所以算式本身必须钉住 ——
算错了，`auto_recommend` 的 R6 规则会静默不触发，等于没有。

> ⚠️ 这里同时钉住一个**我自己先写错的**东西：最初我把"方差占比"当成
> "有效权重"，于是认为"尺度相同时名义 = 实际"。单测立刻打脸 ——
> 尺度相同、`w=0.3` 时方差占比只有 **0.155**，看起来像"权重没生效"，
> 其实生效了。所以有效权重用 `w·sd` 归一（尺度相同时精确等于名义权重），
> 方差占比只用来描述问题有多严重。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from scripts.auto_recommend import effective_share, variance_share  # noqa: E402

#: V2.2 旧窗口候选池实测（`scripts/layer_weight_sim.py`）
SD_SIX, SD_ACC = 4.52, 13.07


def test_effective_share_equals_nominal_when_scales_match() -> None:
    """尺度相同时，`w·sd` 归一的份额**精确等于**名义权重（任意 w）。"""
    for weight in (0.0, 0.3, 0.5, 0.7, 1.0):
        assert effective_share(weight, 7.0, 7.0) == pytest.approx(weight)


def test_variance_share_does_not_equal_nominal_off_the_midpoint() -> None:
    """⚠️ 方差占比**不是**有效权重：均衡尺度下 `w=0.3` 只有 0.155。

    这条测试的作用是防止以后有人把 `variance_share` 当成有效权重来用。
    """
    assert variance_share(0.3, 4.0, 4.0) == pytest.approx(0.1552, abs=1e-3)
    # 只有中点才碰巧相等
    assert variance_share(0.5, 4.0, 4.0) == pytest.approx(0.5)


def test_unequal_scales_reveal_the_nominal_50_50_gap() -> None:
    """V2.2 实测尺度：名义 50/50，实际约 26/74（`w·sd`）/ 11/89（方差）。"""
    share = effective_share(0.5, SD_SIX, SD_ACC)
    assert share is not None
    assert share == pytest.approx(0.257, abs=0.005)
    var = variance_share(0.5, SD_SIX ** 2, SD_ACC ** 2)
    assert var is not None
    assert var == pytest.approx(0.107, abs=0.005)
    # 两者都远离名义 0.5，超过 R6 的 0.15 阈值 —— 规则当时会触发
    assert abs(share - 0.5) > 0.15
    assert abs(var - 0.5) > 0.15


def test_v23_config_has_no_nominal_vs_actual_gap() -> None:
    """V2.3 的 100/0 不存在这个问题：实际份额必然等于名义的 100%。

    这也是 `auto_recommend` 在 V2.3 配置下**不再**报 R6 的原因 ——
    不是规则坏了，而是它要修的东西已经修掉了。
    """
    for sd_six, sd_acc in ((SD_SIX, SD_ACC), (1.0, 1.0), (10.0, 0.1)):
        assert effective_share(1.0, sd_six, sd_acc) == pytest.approx(1.0)
    assert effective_share(0.0, SD_SIX, SD_ACC) == pytest.approx(0.0)


def test_both_measures_are_none_without_variance() -> None:
    """两层尺度都是 0（例如只剩一个板块）→ 返回 None 而不是除零。"""
    assert effective_share(0.5, 0.0, 0.0) is None
    assert variance_share(0.5, 0.0, 0.0) is None
