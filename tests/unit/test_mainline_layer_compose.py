"""层合成的"缺数据"处理：`service.py` 必须用 `weighted_score`，不能手写加权。

## 为什么值得单独钉住

`service.py` 原来是：

    base_total = (six_dim.score * six_w + accumulation.score * acc_w) / 100

**没有看层的 `available`**。而 `weighted_score` 的契约是"全部子维度都缺时
返回 `(0.0, 0.0)`" —— 也就是说"这一层没有数据"的表示就是 **`score = 0`**。
于是第二层没数据的板块 `base_total` 被**腰斩**：`"没测到"` 被算成
`"测得极差"`。这正是 `weighted_score` 内部刻意避免的错误，在上一层又犯了回来。

实测影响（`scripts/coverage_fill_experiment.py`，V2.2 备份）：

- 旧窗口（覆盖率均值 0.676，10 分位 **0**）超过一成候选板块第二层覆盖率为 0，
  而它们随后跑赢 → `accumulation_coverage` 的 H20/H60 IC 是 **−0.0455/−0.0805**
  （覆盖率本不该有预测能力）；
- 修好后旧窗口 50/50 的池内 IC 由 +0.0539/+0.0569 升到 **+0.0796/+0.1053**；
- 中/新窗口覆盖率 >0.93，**一格不变**。

所以这个不变式一旦被改回去，旧窗口会静默变差（分数被腰斩），
而分数本身看起来完全正常。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.mainline.scoring import weighted_score  # noqa: E402


def test_missing_layer_is_excluded_not_treated_as_zero() -> None:
    """第二层没数据时，`base_total` 应等于第一层分，**不是**它的一半。"""
    # 第二层不可用：score=0（"没数据"的表示），available=False
    score, coverage = weighted_score([
        (80.0, 50.0, True),        # 六维层有数据
        (0.0, 50.0, False),        # 第二层没数据
    ])
    assert score == pytest.approx(80.0), "不能被腰斩成 40"
    assert coverage == pytest.approx(0.5), "覆盖率要如实反映只有一半权重参与"


def test_both_layers_available_keeps_the_old_arithmetic() -> None:
    """两层都有数据时，行为必须与原来的 `(six·w + acc·w)/100` **完全一致**。"""
    score, coverage = weighted_score([
        (80.0, 50.0, True), (40.0, 50.0, True)])
    assert score == pytest.approx((80.0 * 50 + 40.0 * 50) / 100.0)
    assert coverage == pytest.approx(1.0)
    # 100/0 时第二层即使有数据也不影响结果
    score, _ = weighted_score([(80.0, 100.0, True), (40.0, 0.0, True)])
    assert score == pytest.approx(80.0)


def test_only_second_layer_available_uses_it() -> None:
    """第一层没数据、第二层有 —— 用有的那一层，而不是报 0。"""
    score, coverage = weighted_score([
        (0.0, 50.0, False), (60.0, 50.0, True)])
    assert score == pytest.approx(60.0)
    assert coverage == pytest.approx(0.5)


def test_both_layers_missing_returns_zero_pair() -> None:
    """两层都没数据 → `(0.0, 0.0)`（调用方据此判为无数据）。"""
    assert weighted_score([(0.0, 50.0, False), (0.0, 50.0, False)]) == (0.0, 0.0)


def test_service_composes_layers_through_weighted_score() -> None:
    """源码级断言：`service.py` 不得再手写层间加权。

    行为单测只能钉住 `weighted_score`；而这次出问题的地方是
    **`service.py` 没有用它**。所以必须直接检查源码。
    """
    source = (ROOT / "src" / "mainline" / "service.py").read_text(
        encoding="utf-8")
    assert "weighted_score(" in source, "层合成必须走 weighted_score"
    assert "accumulation.score * acc_w" not in source, (
        "手写层间加权会把『层没数据』的 0 分算成真实分数")
