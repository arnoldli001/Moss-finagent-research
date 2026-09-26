"""景气度**低分压制**（`PROSPERITY_FLOOR`）的单元测试。

## 为什么单独钉这一组

这个改动会**改变每一个低景气板块的六维分**，进而改变候选池成员 ——
影响面比"改一个阈值"大得多。而且它的性质很容易被记错：

1. 它**不是等比例打折**：50 分以上完全不动，越接近 0 压缩越多；
2. 它**不是"忽视景气度"**：那需要把维度整个剔除、其余维度重新归一
   （离线测过，见 `docs/MAINLINE_PROSPERITY_MASK_OPTIONS.md`，
   留出窗口净亏，所以没做）；
3. 它的**端点必须精确**：`p=0 → floor`、`p=50 → 50`、`p>50 → p`。
   端点错了会让"最低分"变成别的数，而中间值看起来一切正常。

实测锚点（2026-09-22 离线）：煤炭 885914 的景气度均值 17.0 → 43.4，
候选天数 19 → 38（2.6% → 5.3%）。
"""

from __future__ import annotations

import pytest

from src.mainline.six_dim import PROSPERITY_FLOOR, compress_low


def test_zero_maps_to_floor() -> None:
    """0 分抬到 floor —— 这是这个改动唯一的"定义性"锚点。"""
    assert compress_low(0.0, 40.0) == pytest.approx(40.0)


def test_above_fifty_is_untouched() -> None:
    """50 分及以上**完全不动**：不能把强势板块也一起削。"""
    for value in (50.0, 60.0, 85.5, 100.0):
        assert compress_low(value, 40.0) == pytest.approx(value)


def test_fifty_is_the_fixed_point() -> None:
    assert compress_low(50.0, 40.0) == pytest.approx(50.0)


def test_midpoint_compresses_partially() -> None:
    """25 分 → 45：压缩到一半（λ=0.2 时，距离 50 的差距保留 20%）。"""
    assert compress_low(25.0, 40.0) == pytest.approx(45.0)


def test_monotone_and_within_bounds() -> None:
    """单调不减、且始终落在 [floor, 50] 内 —— 否则排序会被压坏。"""
    values = [float(v) for v in range(0, 51)]
    out = [compress_low(v, 40.0) for v in values]
    assert out == sorted(out)
    assert min(out) >= 40.0 - 1e-9
    assert max(out) <= 50.0 + 1e-9


def test_floor_at_or_above_fifty_is_a_noop() -> None:
    """`floor >= 50` 等于不压缩（λ=0），用于对照实验。"""
    for floor in (50.0, 60.0):
        for value in (0.0, 20.0, 49.9):
            assert compress_low(value, floor) == pytest.approx(value)


def test_default_floor_is_forty() -> None:
    assert PROSPERITY_FLOOR == 40.0
    assert compress_low(0.0) == pytest.approx(40.0)


def test_compression_preserves_order_between_boards() -> None:
    """两个板块的相对顺序不能被压反 —— 压制的目的是"少扣分"，
    不是"把差的说成好的"。"""
    pairs = [(10.0, 20.0), (0.0, 49.0), (33.0, 34.0)]
    for low, high in pairs:
        assert compress_low(low) < compress_low(high)


def test_score_six_dim_applies_floor_to_prosperity() -> None:
    """端到端：`score_six_dim` 产出的景气度维必须已经是压制后的值。

    构造 40 个板块：一半成分股是负增长（景气度会被压到横截面后段），
    一半是高增长。断言所有 **低于 50 分** 的景气度都不低于 floor ——
    这正是"压制"的可观测后果（未压制时低分板块会拿到接近 0 的分）。
    """
    from src.mainline.config import load_config
    from src.mainline.models import BoardInfo, BoardKind
    from src.mainline.six_dim import BoardInput, score_six_dim

    config = load_config(force=True)
    boards = []
    for index in range(40):
        bad = index < 20
        stats = {
            f"6000{index:02d}": {
                "roe_yoy": -30.0 if bad else 25.0,
                "netprofit_yoy": -40.0 if bad else 30.0,
                "or_yoy": -25.0 if bad else 20.0,
                "circ_mv": 5.0e9,
            }
        }
        boards.append(BoardInput(
            info=BoardInfo(code=f"8859{index:02d}.TI", name=f"板块{index}",
                           kind=BoardKind.CONCEPT),
            members=list(stats),
            member_stats=stats,
            circ_mv=5.0e9))
    scored = score_six_dim(boards, config=config)
    assert len(scored) == 40
    low_scores: list[float] = []
    for _code, (layer, _raw) in scored.items():
        prosperity = next(d for d in layer.dimensions if d.key == "prosperity")
        if prosperity.score < 50.0:
            low_scores.append(prosperity.score)
            assert prosperity.score >= PROSPERITY_FLOOR - 1e-6
    # 必须真的出现了"低分区"，否则这条断言是空转的
    assert low_scores, "构造的数据没有产生任何低于 50 的景气度，测试无意义"
