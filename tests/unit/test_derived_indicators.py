"""★ 派生指标流水线的护栏（用户 2026-09-29：「先联网搜公式 → 找数据 → 计算」）。

判据分四组，对应流水线四步：

| 组 | 判据 |
|---|---|
| ① 公式 | 注册表**必须有出处**（无 `sources` ⇒ 加载即报错）；公式符号与 `inputs` 必须对齐 |
| ② 数据名 | 候选名**按序**尝试，命中的那个要连来源/期间一起记下 |
| ③ 取数 | 取不到 = **缺口**（缺哪个、试过哪些候选名），**绝不用 0 兜** |
| ④ 计算 | `safe_eval` 只认数字与白名单算子；未知符号/非法算子/除零**必须报错**，不许静默 |
"""

from __future__ import annotations

import pytest

from src.domain.indicators.derive import (
    DerivedSpec,
    FormulaError,
    derive,
    find_spec,
    formula_symbols,
    load_specs,
    safe_eval,
)

# ---------------------------------------------------------------- ④ 计算


def test_safe_eval_computes_the_nim_formula() -> None:
    """净息差 = 利息净收入 ÷ 生息资产平均余额 × 100。"""
    value = safe_eval("利息净收入 / 生息资产平均余额 * 100",
                      {"利息净收入": 1000.0, "生息资产平均余额": 50000.0})
    assert value == pytest.approx(2.0)


@pytest.mark.parametrize(
    "formula",
    [
        "__import__('os').system('echo hi')",   # 代码执行
        "open('/etc/passwd').read()",            # 文件访问
        "(1).__class__.__mro__",                 # 属性穿透
        "[x for x in (1, 2)]",                   # 推导式
        "利息净收入 if 1 else 0",                 # 条件表达式
        "lambda: 1",                             # lambda
    ],
)
def test_safe_eval_rejects_anything_but_arithmetic(formula: str) -> None:
    """★ 白名单求值：**任何**非算术语法都必须报错（不是"返回个近似值"）。

    这是本模块存在的主要理由之一：公式将来会由（联网 + 编码 agent）产出，
    用 `eval` 等于把任意代码执行权交给模型输出。
    """
    with pytest.raises(FormulaError):
        safe_eval(formula, {"利息净收入": 1.0})


def test_safe_eval_refuses_missing_input_and_zero_denominator() -> None:
    """缺口不许当 0 用；除零必须报错（不许返回 inf/nan）。"""
    with pytest.raises(FormulaError, match="未提供的输入"):
        safe_eval("利息净收入 / 生息资产平均余额", {"利息净收入": 1.0})
    with pytest.raises(FormulaError, match="缺口不能当 0 用"):
        safe_eval("a / b", {"a": 1.0, "b": None})
    with pytest.raises(FormulaError):
        safe_eval("a / b", {"a": 1.0, "b": 0.0})


def test_formula_symbols_are_extracted_in_order() -> None:
    """步②「从公式里提取数据名称」的实现。"""
    assert formula_symbols("利息净收入 / 生息资产平均余额 * 100") == [
        "利息净收入", "生息资产平均余额"]


# ---------------------------------------------------------------- ① 公式与注册表


def test_shipped_specs_load_and_are_self_consistent() -> None:
    """★ 随仓库发布的注册表必须能加载：每个符号都有候选名、都有出处 URL。"""
    specs = load_specs()
    assert specs, "注册表为空（那就没有派生能力）"
    for spec in specs:
        assert spec.sources, f"{spec.id} 缺出处"
        assert all(str(s).startswith("http") for s in spec.sources), (
            f"{spec.id} 的出处必须是 URL（人工可核）")
        for symbol in spec.symbols:
            assert spec.inputs.get(symbol), f"{spec.id} 的 {symbol} 没有候选数据名"


def test_spec_without_sources_is_rejected(tmp_path) -> None:
    """★ 判据 ①：**没有出处的公式不许加载**。

    用户要求"联网搜索概念的意义及公式"——落到代码上就是这一条：
    出处是硬要求，因为公式写错会产出"看着有据的错误数字"。
    """
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "derived:\n"
        "  - id: '拍脑袋:{code}'\n"
        "    formula: 'a / b'\n"
        "    inputs: {a: [A], b: [B]}\n",
        encoding="utf-8")
    with pytest.raises(FormulaError, match="sources"):
        load_specs(bad)


def test_spec_whose_inputs_miss_a_symbol_is_rejected(tmp_path) -> None:
    """公式引用了输入，但候选名没给 ⇒ 加载即报错（否则运行时才炸）。"""
    bad = tmp_path / "bad2.yaml"
    bad.write_text(
        "derived:\n"
        "  - id: 'X:{code}'\n"
        "    formula: 'a / b'\n"
        "    sources: ['https://example.com']\n"
        "    inputs: {a: [A]}\n",
        encoding="utf-8")
    with pytest.raises(FormulaError, match="inputs"):
        load_specs(bad)


def test_find_spec_matches_prefix_and_aliases() -> None:
    """`净息差:600036` / `银行息差` 都要能找到同一条（用户说的就是"银行息差"）。"""
    assert find_spec("净息差:600036") is not None
    assert find_spec("银行息差").prefix == "净息差"


# ---------------------------------------------------------------- ②③ 取数与缺口

_SPEC = DerivedSpec(
    id="净息差:{code}", name="净息差",
    formula="利息净收入 / 生息资产平均余额 * 100",
    inputs={"利息净收入": ("利息净收入", "利息收入减利息支出"),
            "生息资产平均余额": ("生息资产平均余额", "生息资产", "总资产")},
    unit="%", sources=("https://example.com",),
)


def test_candidate_names_are_tried_in_order_and_recorded() -> None:
    """★ 判据 ②：候选名**按序**尝试；退化到近似名要如实记下用了哪个。"""
    seen: list[str] = []

    def fetch(name: str, code: str):
        seen.append(name)
        if name == "利息净收入":
            return 1000.0, "年报", "2026-06-30"
        if name == "生息资产":            # 精确名没有，退到中间档
            return 50000.0, "年报", "2026-06-30"
        return None

    result = derive("净息差:600036", "600036", fetch, specs=[_SPEC])
    assert result.ok and result.value == pytest.approx(2.0)
    assert seen[:3] == ["利息净收入", "生息资产平均余额", "生息资产"], (
        "必须按候选名顺序试，且命中即停")
    assert result.used_inputs["生息资产平均余额"].used_name == "生息资产", (
        "退化的那个名字要记进 used_inputs（口径随数据下发）")
    extra = result.extra()
    assert extra["used_inputs"]["生息资产平均余额"]["name"] == "生息资产"
    assert extra["formula"] and extra["sources"]


def test_missing_input_reports_a_gap_not_a_zero() -> None:
    """★ 判据 ③：取不到 = **缺口**（缺哪个 + 试过哪些候选名），不是 0。

    AGENTS.md：「没量到」≠「量到 0」——用 0 兜会产出"看着有据"的错误净息差。
    """
    result = derive("净息差:600036", "600036", lambda name, code: None, specs=[_SPEC])
    assert not result.ok and result.value is None
    assert set(result.missing) == {"利息净收入", "生息资产平均余额"}
    assert result.missing["生息资产平均余额"] == ["生息资产平均余额", "生息资产", "总资产"]
    extra = result.extra()
    assert extra["missing_inputs"] and "取不到" in extra["missing_reason"]


def test_partial_inputs_still_report_the_gap() -> None:
    """只差一个输入也是缺口（**不许**用已经有的一半硬算）。"""
    def fetch(name: str, code: str):
        return (1000.0, "年报", "2026-06-30") if name == "利息净收入" else None

    result = derive("净息差:600036", "600036", fetch, specs=[_SPEC])
    assert not result.ok
    assert list(result.missing) == ["生息资产平均余额"]
    assert result.used_inputs  # 拿到的那一半仍如实带出（便于排查）


def test_unknown_indicator_is_a_named_gap() -> None:
    """没登记的派生指标：给可读原因，不是异常、也不是空值。"""
    result = derive("不存在的派生指标:600036", "600036",
                    lambda name, code: 1.0, specs=[_SPEC])
    assert not result.ok and "没有登记的派生公式" in result.reason
