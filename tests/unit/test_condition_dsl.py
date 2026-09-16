"""多因子条件 DSL 单测（离线，确定性合成数据）。

重点锁两类问题：
1. **优先级/括号**：设计文档 §5.2 的正则实现在 AND/OR 混合与括号上是错的，
   连文档自己的示例 4 都会被算成「四个条件全 AND」。这里的用例就是那份示例。
2. **三值逻辑**：A 股数据常缺，`PE != 30` 对 NaN 在 pandas 里返回 True，
   会把"未知"当成"满足条件"混进股票池。缺失必须记「未知」且不入选。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.quant.condition_dsl import (
    Condition,
    ConditionError,
    parse_condition,
)


def _frame() -> pd.DataFrame:
    """6 只股票的小截面，含 NaN 与并列值。"""
    return pd.DataFrame(
        {
            "PE": [10.0, 20.0, 30.0, 40.0, np.nan, 25.0],
            "ROE": [20.0, 8.0, 18.0, 5.0, 15.0, 12.0],
            "Momentum20": [8.0, -3.0, 2.0, 12.0, 1.0, -1.0],
            "Momentum60": [4.0, 6.0, 15.0, 3.0, 20.0, 2.0],
            "Turnover": [2.0, 0.5, 3.0, 1.2, np.nan, 0.9],
            "Industry": ["银行", "银行", "电子", "电子", "医药", "医药"],
        },
        index=["A", "B", "C", "D", "E", "F"],
    )


# ==================== 优先级 / 括号 / NOT ====================


def test_and_binds_tighter_than_or() -> None:
    """A OR B AND C 必须解析为 A OR (B AND C)。"""
    frame = _frame()
    # PE<12: 只有 A；ROE>15 且 PE>15: C（ROE18/PE30）；A 不满足 PE>15
    mask = parse_condition("PE < 12 OR ROE > 15 AND PE > 15").evaluate(frame)
    assert mask.tolist() == [True, False, True, False, False, False]


def test_parentheses_override_precedence() -> None:
    frame = _frame()
    mask = parse_condition("(PE < 12 OR ROE > 15) AND PE > 15").evaluate(frame)
    assert mask.tolist() == [False, False, True, False, False, False]


def test_doc_example_4_or_inside_parentheses_actually_works() -> None:
    """设计文档示例 4：正则实现会退化成全 AND，这里必须按 OR 语义选出更多股票。

    PE<25 AND ROE>12 AND (Mom20>5 OR Mom60>10) AND Turnover>1
      A: PE10 ROE20 Mom20=8 -> OK
      C: PE30 不满足 PE<25 -> 出局
      D: ROE5 出局
    """
    frame = _frame()
    condition = parse_condition(
        "PE < 25 AND ROE > 12 AND (Momentum20 > 5 OR Momentum60 > 10) "
        "AND Turnover > 1")
    mask = condition.evaluate(frame)
    assert mask.tolist() == [True, False, False, False, False, False]

    # 若 OR 被错误地当成 AND（文档实现的行为），A 会因为 Momentum60=4 被漏选
    naive = parse_condition(
        "PE < 25 AND ROE > 12 AND Momentum20 > 5 AND Momentum60 > 10 "
        "AND Turnover > 1").evaluate(frame)
    assert naive.sum() == 0, "全 AND 口径下应为 0 只 —— 这正是文档实现的后果"
    assert mask.sum() == 1


def test_not_negates_group() -> None:
    frame = _frame()
    mask = parse_condition("NOT (PE < 25)").evaluate(frame)
    # PE=30/40/25 满足；PE=NaN 是「未知」，NOT 未知 = 未知 → 不入选
    assert mask.tolist() == [False, False, True, True, False, True]


def test_double_not_returns_original() -> None:
    frame = _frame()
    base = parse_condition("PE < 25").evaluate(frame)
    double = parse_condition("NOT NOT (PE < 25)").evaluate(frame)
    assert base.tolist() == double.tolist()


# ==================== 三值逻辑 / 缺失值 ====================


def test_missing_value_is_unknown_not_true() -> None:
    """`PE != 30` 不能把 PE=NaN 的行选进来（pandas 默认会返回 True）。"""
    frame = _frame()
    mask = parse_condition("PE != 30").evaluate(frame)
    assert mask.tolist() == [True, True, False, True, False, True]

    naive = frame["PE"] != 30
    assert bool(naive.tolist()[4]) is True, "pandas 原生对比确实会把 NaN 判为满足"
    assert mask.tolist()[4] is False, "本 DSL 必须把它判为未知 → 不入选"


def test_unknown_propagates_through_and_or() -> None:
    """Kleene 三值逻辑：未知 AND 假 = 假；未知 OR 真 = 真；未知 OR 假 = 未知 → 不入选。"""
    frame = _frame()   # E 的 PE 缺失、ROE=15
    assert parse_condition("PE > 15 AND ROE > 100").evaluate(frame).tolist() == \
        [False, False, False, False, False, False]
    # E：未知 OR 真 → 真
    assert parse_condition("PE > 15 OR ROE > 10").evaluate(frame).tolist()[4] is True
    # E：未知 OR 假 → 未知 → 不入选
    assert parse_condition("PE > 15 OR ROE > 100").evaluate(frame).tolist()[4] is False


def test_explain_reports_unknown_count() -> None:
    frame = _frame()
    report = parse_condition("PE < 25").explain(frame)
    assert report["total"] == 6
    assert report["selected"] == 2          # A(10) 与 B(20)
    assert report["unknown"] == 1           # E 的 PE 缺失
    assert report["rejected"] == 3
    assert report["selected_ratio"] == pytest.approx(2 / 6)


# ==================== 算术 / BETWEEN / IN ====================


def test_arithmetic_on_both_sides() -> None:
    frame = _frame()
    mask = parse_condition("Momentum20 - Momentum60 > 2").evaluate(frame)
    # A: 8-4=4 True; B: -3-6=-9; C: 2-15=-13; D: 12-3=9 True; E: 1-20; F: -3
    assert mask.tolist() == [True, False, False, True, False, False]


def test_ratio_and_scalar_function() -> None:
    frame = _frame()
    mask = parse_condition("ABS(Momentum20 - Momentum60) > 10").evaluate(frame)
    assert mask.tolist() == [False, False, True, False, True, False]


def test_division_by_zero_is_missing_not_inf() -> None:
    frame = _frame().copy()
    frame["Zero"] = 0.0
    mask = parse_condition("PE / Zero > 1").evaluate(frame)
    assert mask.sum() == 0


def test_between() -> None:
    frame = _frame()
    mask = parse_condition("PE BETWEEN 15 AND 30").evaluate(frame)
    assert mask.tolist() == [False, True, True, False, False, True]


def test_in_list_of_strings() -> None:
    frame = _frame()
    mask = parse_condition('Industry IN ("银行", "医药")').evaluate(frame)
    assert mask.tolist() == [True, True, False, False, True, True]


def test_not_in_list() -> None:
    frame = _frame()
    mask = parse_condition('Industry NOT IN ("银行")').evaluate(frame)
    assert mask.tolist() == [False, False, True, True, True, True]


# ==================== 截面函数 ====================


def test_rank_is_percentile_0_to_100() -> None:
    frame = _frame()
    # Momentum20 = [8,-3,2,12,1,-1] → 最大(12)的百分位排名是 100
    report = parse_condition("RANK(Momentum20) >= 80").explain(frame)
    assert report["selected"] >= 1
    assert report["unknown"] == 0


def test_bare_numeric_expression_is_rejected() -> None:
    """直接写 RANK(PE)/PE 这种数值表达式必须报错，不能猜「非0即真」。"""
    frame = _frame()
    with pytest.raises(ConditionError, match="必须返回布尔结果"):
        parse_condition("RANK(PE)").evaluate(frame)
    with pytest.raises(ConditionError, match="必须返回布尔结果"):
        parse_condition("PE").evaluate(frame)


def test_pctl_uses_cross_sectional_quantile() -> None:
    frame = _frame()
    # PE 的 50% 分位 ≈ 25 → 选出 PE <= 25 的（NaN 未知，不入选）
    mask = parse_condition("PE <= PCTL(PE, 0.5)").evaluate(frame)
    assert mask.tolist() == [True, True, False, False, False, True]


def test_cross_sectional_aggregates() -> None:
    frame = _frame()
    mask = parse_condition("PE < AVG(PE)").evaluate(frame)
    mean = frame["PE"].mean()
    assert mask.tolist() == [
        bool(value < mean) if pd.notna(value) else False for value in frame["PE"]
    ]


def test_pctl_rejects_series_argument() -> None:
    with pytest.raises(ConditionError, match="分位参数必须是常数"):
        parse_condition("PE < PCTL(PE, PE)").evaluate(_frame())


# ==================== 错误处理 ====================


def test_unknown_factor_lists_available_ones() -> None:
    frame = _frame()
    with pytest.raises(ConditionError, match="未知因子"):
        parse_condition("PE_TTM < 30").evaluate(frame)


def test_unknown_factor_fails_early_with_whitelist() -> None:
    with pytest.raises(ConditionError, match="未知因子 'PE_TTM'"):
        parse_condition("PE_TTM < 30", known_factors=["PE", "ROE"])


def test_whitelist_accepts_extra_columns() -> None:
    condition = parse_condition('PE < 30 AND Industry == "银行"',
                                known_factors=["PE"],
                                known_columns=["Industry"])
    assert condition.factors == ("Industry", "PE")


@pytest.mark.parametrize("bad", [
    "PE <",
    "PE < 30 AND",
    "(PE < 30",
    "PE < 30)",
    "PE BETWEEN 10",
    "PE IN ()",
    "PE < 30 AND AND ROE > 1",
])
def test_syntax_errors_are_reported_with_position(bad: str) -> None:
    with pytest.raises(ConditionError):
        parse_condition(bad)


def test_syntax_error_message_has_pointer() -> None:
    with pytest.raises(ConditionError) as excinfo:
        parse_condition("PE < 30 AND (ROE > 15")
    message = str(excinfo.value)
    assert "^" in message
    assert "ROE" in message


def test_empty_condition_rejected() -> None:
    with pytest.raises(ConditionError, match="为空"):
        parse_condition("   ")


def test_no_code_execution_surface() -> None:
    """纯 AST 求值：恶意字符串只会得到「未知因子」，不会被执行。"""
    with pytest.raises(ConditionError):
        parse_condition('__import__("os").system("echo pwned") < 1').evaluate(
            _frame())


def test_percent_sign_is_documented_warning_not_silent_scaling() -> None:
    """`ROE > 15%` 不能被偷偷当成 0.15（那会选出几乎所有股票）。"""
    condition = parse_condition("ROE > 15%")
    assert condition.warnings and "百分数" in condition.warnings[0]
    mask = condition.evaluate(_frame())
    assert mask.tolist() == [True, False, True, False, False, False]


# ==================== 序列化 / 复用 ====================


def test_to_dict_serializes_ast_and_factors() -> None:
    condition = parse_condition("PE < 30 AND RANK(Momentum20) > 70")
    payload = condition.to_dict()
    assert payload["factors"] == ["Momentum20", "PE"]
    assert payload["ast"]["type"] == "logical"
    assert payload["ast"]["op"] == "AND"
    assert payload["ast"]["right"]["type"] == "compare"
    assert payload["ast"]["right"]["left"]["type"] == "call"


def test_condition_is_reusable_across_sections() -> None:
    condition = parse_condition("PE < 25")
    first = condition.select(_frame())
    second = condition.select(_frame())
    assert first.index.tolist() == second.index.tolist() == ["A", "B"]
    assert isinstance(condition, Condition)
