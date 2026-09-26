"""ETF 回测统计口径（`src/mainline/etf_flow_stats.py`）单元测试。

只测纯函数：独立时段去重、Wilson 区间、目标线解析与达标判定。
不碰仓库与网络 —— 那三件事都是"给一个输入就该有一个确定答案"的算术，
而它们恰好是四张统计表里**唯一**没有任何自动化测试固化的部分
（`test_mainline_etf_flow.py` 明确不覆盖回测：它依赖本地全量数据）。

## 为什么每条边界都要测到

这几个函数的失效方式都是**静默**的：间隔判错只会让独立样本数偏大或偏小，
目标线取错只会让某一行的"达标"标记反过来 —— 两种都不会抛错，
在报表上看起来都是一组正常的数字。所以边界（恰好等于阈值、缺配、
非交易日、字典式配置缺某一期）必须逐条钉住。
"""

from __future__ import annotations

import pytest

from src.mainline.etf_flow_stats import (
    independent_episodes,
    resolve_targets,
    target_hit,
    wilson_interval,
)

#: 一小段连续交易日历（2023-01-03 起 20 个工作日，跳过周末）
CALENDAR = [
    "20230103", "20230104", "20230105", "20230106", "20230109",
    "20230110", "20230111", "20230112", "20230113", "20230116",
    "20230117", "20230118", "20230119", "20230120", "20230130",
    "20230131", "20230201", "20230202", "20230203", "20230206",
]


# ==================================================================
# 独立时段
# ==================================================================


def test_episodes_empty_input() -> None:
    assert independent_episodes([], calendar=CALENDAR, gap=5) == []


def test_episodes_single_date() -> None:
    assert independent_episodes(["20230105"], calendar=CALENDAR, gap=5) == [
        "20230105"]


def test_episodes_collapses_dense_run_to_first() -> None:
    """连续多日触发只算一个时段 —— 这正是"同一事件连报多日"的形状。"""
    dates = ["20230103", "20230104", "20230105", "20230106"]
    assert independent_episodes(dates, calendar=CALENDAR, gap=5) == ["20230103"]


def test_episodes_accepts_exactly_at_gap() -> None:
    """恰好相距 `gap` 个交易日要**接受**（判据是 `>=`，不是 `>`）。"""
    # 索引 0 与索引 5 相距 5
    assert independent_episodes(["20230103", "20230110"],
                               calendar=CALENDAR, gap=5) == [
        "20230103", "20230110"]


def test_episodes_rejects_one_short_of_gap() -> None:
    # 索引 0 与索引 4 相距 4
    assert independent_episodes(["20230103", "20230109"],
                               calendar=CALENDAR, gap=5) == ["20230103"]


def test_episodes_ignores_duplicates_and_order() -> None:
    dates = ["20230110", "20230103", "20230110", "20230104"]
    assert independent_episodes(dates, calendar=CALENDAR, gap=5) == [
        "20230103", "20230110"]


def test_episodes_counts_trading_days_not_natural_days() -> None:
    """跨周末时自然日与交易日会差开 —— 必须按交易日判重叠。

    `20230113`（周五）到 `20230117`（周二）相隔 4 个自然日，但中间夹了周末，
    交易日上相距 2 天。gap=3 时交易日口径判为**重叠**（2 < 3），
    若按自然日算（4 >= 3）就会错判成两个独立时段。
    """
    assert independent_episodes(["20230113", "20230117"],
                               calendar=CALENDAR, gap=3) == ["20230113"]


def test_episodes_greedy_is_anchored_on_last_accepted() -> None:
    """基准是"上一个被接受的日期"，不是"上一个原始日期"。

    这组信号落在索引 0 / 3 / 5 / 7 上，gap=4：

        按"上一个被接受"（本实现）：接受 0，跳过 3（距 0 只有 3），
            接受 5（距 0 有 5），跳过 7（距 5 只有 2）→ **2 个时段**
        按"上一个原始日期"（错误写法）：接受 0，3 距 0 不足 → 跳过，
            5 只和 **3** 比（差 2）→ 也跳过，7 只和 **5** 比（差 2）→ 跳过
            → **1 个时段**

    差别在于链式密集报的时候，错误写法会把后面那些**确实已经拉开距离**的
    信号一起吞掉 —— 独立样本数偏小，而报表上只是一个更小的数字。
    """
    dates = ["20230103", "20230106", "20230110", "20230113"]
    assert independent_episodes(dates, calendar=CALENDAR, gap=4) == [
        "20230103", "20230110"]


def test_episodes_date_outside_calendar_uses_insert_position() -> None:
    """不在日历上的日期（停牌/数据缺口）按"应当插入的位置"比较。

    `20230107` 是周六，日历上不存在，插入位置是 4（`20230106` 与 `20230109`
    之间）—— 也就是它离 `20230103` 有 4 个交易日，既不该被当成"紧挨着周一"
    而合并、也不该被当成同一天。gap=5 时判重叠、gap=4 时判独立。
    """
    assert independent_episodes(["20230103", "20230107"],
                               calendar=CALENDAR, gap=5) == ["20230103"]
    assert independent_episodes(["20230103", "20230107"],
                               calendar=CALENDAR, gap=4) == [
        "20230103", "20230107"]


def test_episodes_without_calendar_falls_back_to_natural_days() -> None:
    """日历不可用时退回自然日，方向是**保守**的（判得更严、独立样本更少）。"""
    # 自然日相隔 6 天：gap=6 接受，gap=7 拒绝
    assert independent_episodes(["20230103", "20230109"], calendar=[],
                               gap=6) == ["20230103", "20230109"]
    assert independent_episodes(["20230103", "20230109"], calendar=[],
                               gap=7) == ["20230103"]


def test_episodes_gap_below_one_still_dedupes_by_day() -> None:
    """gap=0/负数要按 1 处理：同一天触发的多只 ETF 只能算一个时段。"""
    assert independent_episodes(["20230103", "20230103"],
                               calendar=CALENDAR, gap=0) == ["20230103"]


# ==================================================================
# Wilson 区间
# ==================================================================


def test_wilson_no_samples() -> None:
    assert wilson_interval(0, 0) is None


def test_wilson_matches_hand_computed_value() -> None:
    """55/66 = 83.3% → [72.6%, 90.4%]（就是报表里 `opportunity_live` 那行）。"""
    low, high = wilson_interval(55, 66)  # type: ignore[misc]
    assert low == pytest.approx(0.7257, abs=1e-3)
    assert high == pytest.approx(0.9041, abs=1e-3)


def test_wilson_stays_inside_unit_interval() -> None:
    """极端比例下也不能跑出 [0,1] —— 这是选 Wilson 而不是正态近似的理由。"""
    for successes, total in ((5, 5), (0, 5), (1, 3), (0, 1)):
        low, high = wilson_interval(successes, total)  # type: ignore[misc]
        assert 0.0 <= low <= high <= 1.0


def test_wilson_widens_as_samples_shrink() -> None:
    """同样 83% 的胜率，样本越少区间越宽（3 个样本时几乎没有信息量）。"""
    wide = wilson_interval(5, 6)   # type: ignore[misc]
    narrow = wilson_interval(50, 60)  # type: ignore[misc]
    assert (wide[1] - wide[0]) > (narrow[1] - narrow[0])


def test_wilson_clamps_inconsistent_successes() -> None:
    """`successes > total` 是调用方的 bug，这里夹住而不是抛错。"""
    assert wilson_interval(10, 5) == wilson_interval(5, 5)


# ==================================================================
# 目标线解析
# ==================================================================


def test_resolve_dict_form_per_horizon() -> None:
    targets = {"median_return": {5: 0.01, 34: 0.045},
               "win_rate": {5: 0.58, 34: 0.65}}
    assert resolve_targets(targets, 5) == (0.01, 0.58)
    assert resolve_targets(targets, 34) == (0.045, 0.65)


def test_resolve_dict_form_missing_horizon_is_none() -> None:
    """字典式配置里缺某一期 = **不表态**，绝不回退去用别期的线顶上。

    这正是"T+5 永远未达标"那个坑的成因：旧配置只有一个
    `median_return_34`，却被所有观察期共用。
    """
    assert resolve_targets({"median_return": {34: 0.045}}, 5) == (None, None)


def test_resolve_legacy_suffix_form() -> None:
    """兼容旧配置：`median_return_34` 只对 T+34 生效，别的观察期不表态。"""
    targets = {"median_return_34": 0.045, "win_rate": 0.65}
    assert resolve_targets(targets, 34) == (0.045, 0.65)
    assert resolve_targets(targets, 5) == (None, 0.65)


def test_resolve_scalar_applies_to_every_horizon() -> None:
    assert resolve_targets({"median_return": 0.03}, 5) == (0.03, None)
    assert resolve_targets({"median_return": 0.03}, 34) == (0.03, None)


def test_resolve_string_keys_work_too() -> None:
    """YAML 里写成 `"5": ...` 或 `5: ...` 都应认得（前者是字符串键）。"""
    assert resolve_targets({"median_return": {"5": 0.02}}, 5) == (0.02, None)


def test_resolve_missing_and_invalid_values() -> None:
    assert resolve_targets({}, 5) == (None, None)
    # YAML 的 `true` 会被 pyyaml 解析成 bool —— 那不是数字，当缺配处理
    assert resolve_targets({"win_rate": True}, 5) == (None, None)
    assert resolve_targets({"win_rate": "abc"}, 5) == (None, None)


def test_resolve_short_prefers_its_own_key() -> None:
    """风险方向优先 `median_return_short`；缺配才回退到做多那条（对称门槛）。"""
    targets = {"median_return": {34: 0.045},
               "median_return_short": {34: 0.020}}
    assert resolve_targets(targets, 34, short=True) == (0.020, None)
    assert resolve_targets(targets, 34, short=False) == (0.045, None)
    # 没有 `median_return_short` 时用对称门槛，而不是"没有门槛"
    assert resolve_targets({"median_return": {34: 0.045}}, 34,
                           short=True) == (0.045, None)


# ==================================================================
# 达标判定（按信号方向）
# ==================================================================

#: 与 `configs/etf_flow.yaml` 当前取值一致，便于对照真实报表数字
TARGETS = {"median_return": {5: 0.010, 10: 0.015, 20: 0.025, 34: 0.045},
           "win_rate": {5: 0.58, 10: 0.60, 20: 0.62, 34: 0.65}}


def test_target_hit_long_direction() -> None:
    """机会信号放行桶：T+34 +9.18% / 胜率 83.33% → 达标。"""
    assert target_hit(kind="opportunity", median=0.0918, win_rate=0.8333,
                      target_median=0.045, target_win=0.65) is True


def test_target_hit_fails_when_win_rate_short() -> None:
    """中位数够但胜率不够 → 不达标（两个条件都要过）。"""
    assert target_hit(kind="opportunity", median=0.0918, win_rate=0.60,
                      target_median=0.045, target_win=0.65) is False


def test_target_hit_short_direction_is_reversed() -> None:
    """风险信号判的是"跌够没有"，两种口径下同一个数字结论相反。"""
    # 跌幅 5% 达到 4.5% 的门槛，且 70% 的时候在跌 → 判对
    assert target_hit(kind="risk", median=-0.0500, win_rate=0.30,
                      target_median=0.045, target_win=0.65) is True
    # 同样的数字按做多口径判 → False（这正是修正前的行为）
    assert target_hit(kind="opportunity", median=-0.0500, win_rate=0.30,
                      target_median=0.045, target_win=0.65) is False


def test_target_hit_short_direction_still_needs_magnitude() -> None:
    """⚠️ 方向修正**不等于**放宽门槛。

    实测 `risk_live` 的 T+34 中位数是 -2.00%，达不到 4.5% 的对称门槛，
    所以它**依然**显示未达标 —— 那是"方向对但幅度不够"的真实结论。
    把看空门槛悄悄调低会把"强度不足"粉饰成"达标"。
    """
    assert target_hit(kind="risk", median=-0.0200, win_rate=0.3036,
                      target_median=0.045, target_win=0.65) is False


def test_target_hit_short_win_rate_is_mirrored() -> None:
    """风险类的胜率门槛是镜像的：要求"大多数时候确实在跌"。

    跌幅够但胜率 80%（即大多数时候在涨）→ 跌得不干脆，不达标。
    """
    assert target_hit(kind="risk", median=-0.0600, win_rate=0.80,
                      target_median=0.045, target_win=0.65) is False
    assert target_hit(kind="risk", median=-0.0600, win_rate=0.30,
                      target_median=0.045, target_win=0.65) is True


def test_target_hit_industry_reversal_is_long_direction() -> None:
    """行业反转警示不是做空信号（模块只出警示不做动作），按做多口径判。"""
    assert target_hit(kind="industry_reversal", median=-0.0144, win_rate=0.3876,
                      target_median=0.045, target_win=0.65) is False


def test_target_hit_without_target_says_nothing() -> None:
    """没配目标或中位数为空 → None（**不表态**，与"未通过"是两件事）。"""
    assert target_hit(kind="opportunity", median=0.09, win_rate=0.8,
                      target_median=None, target_win=None) is None
    assert target_hit(kind="opportunity", median=None, win_rate=0.8,
                      target_median=0.01, target_win=None) is None


def test_target_hit_median_only_without_win_rate() -> None:
    """只配了收益线时，胜率不参与判定（而不是当成 0 直接判死）。"""
    assert target_hit(kind="opportunity", median=0.09, win_rate=0.10,
                      target_median=0.045, target_win=None) is True
