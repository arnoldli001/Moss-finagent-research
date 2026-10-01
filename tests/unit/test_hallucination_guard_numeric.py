"""幻觉防护的**数值等价**判据（`docs/PRD.md` §19.16.5 第 2 条）。

## 现场（用户在前端截图报障）

真实端到端跑出来的结论里，这两句都被附加了「[幻觉防护提示]未在输入数据中
找到的数字」：

| 结论里写的 | 输入上下文里是 | 关系 |
|---|---|---|
| `1.70万亿` | `17028亿` | 同一个量级，单位不同（万亿 vs 亿） |
| `90.18%` | `90.183` | 同一个读数，精度不同（保留两位） |

两者都**同一个读数**，判成幻觉的直接后果是**这条护栏自己失去可信度**
（狼来了）—— 真出幻觉时没人再看它。

## 判据（不是"差不多就算过"）

① **末位精度容差**：输出的最后一位小数决定容差（`90.18` → ±0.005），
   即"输入里存在一个四舍五入到该精度就等于输出值"的读数；
② **金额单位换算**：`万亿/亿/千万/百万/万` 换算到同一单位再比。

## 反向判据（同样重要，防止修成假绿）

- 编造的数（`3.5%` 而输入只有 `2.1%`）**必须仍然报警**；
- **整数百分比/倍数不放宽**：输入里的日期（`2026-09-18`）自带被边界分隔的
  整数，放宽会让"编一个 18%"碰巧命中日期 ⇒ 必须仍然报警；
- 金额换算只认**金额候选**（输入里的日期整数不在金额候选池里）。
"""

from __future__ import annotations

import pytest

from src.infrastructure.llm.hallucination_guard import (
    _AMOUNT_RE,
    HallucinationGuard,
)


def _verify(output: str, context: str):
    return HallucinationGuard.verify(output, context)


# ============================================================
# 1. 金额单位：万亿 ↔ 亿
# ============================================================


class TestAmountUnitEquivalence:
    def test_wanyi_matches_yi_with_same_magnitude(self) -> None:
        """§19.16.5 现场 1：结论写 1.70万亿，输入是 17028亿。"""
        report = _verify(
            "招商银行总资产约1.70万亿，规模居股份行前列",
            "## 输入数据\n- 总资产 2026-06-30=17028亿 c0.95 新浪财务\n",
        )
        assert report.unverified_numbers == [], report.unverified_numbers
        assert report.passed is True

    def test_yi_matches_wanyi_with_same_magnitude(self) -> None:
        """反向也成立（输入用万亿、结论用亿）—— 换算不能是单向的。"""
        report = _verify(
            "总资产17028亿",
            "## 输入数据\n- 总资产 2026-06-30=1.7028万亿 c0.95\n",
        )
        assert report.unverified_numbers == []

    def test_longest_unit_wins_in_amount_regex(self) -> None:
        """`1.70万亿` 必须抠成 `万亿`，不能抠成 `万`（差 1 亿倍）。

        这是本条缺陷的**根因**：正则分支先匹配先赢，`亿` 写在 `万亿` 前面时
        `1.70万亿` → `1.70万`，于是拿去和输入做字符串比对必然不中。
        """
        tokens = [m.group(0) for m in _AMOUNT_RE.finditer("总资产1.70万亿")]
        assert tokens == ["1.70万亿"]

    def test_wrong_magnitude_still_flagged(self) -> None:
        """数量级真的错了必须报警 —— 否则就是把护栏修成了摆设。"""
        report = _verify(
            "总资产约1.70万亿",
            "## 输入数据\n- 总资产 2026-06-30=9000亿 c0.95\n",
        )
        assert report.passed is False
        assert any("1.70" in n for n in report.unverified_numbers)

    def test_integer_amount_converts(self) -> None:
        report = _verify(
            "解禁市值2万亿",
            "## 输入数据\n- 解禁市值 2026-10-08=20000亿 c1.00\n",
        )
        assert report.unverified_numbers == []

    def test_amount_candidates_exclude_dates(self) -> None:
        """日期里的整数**不能**当金额候选（否则 2万亿 会命中 2026-10-08）。"""
        report = _verify(
            "解禁市值2万亿",
            "## 输入数据\n- 数据日期 2026-10-08 c1.00（无金额）\n",
        )
        assert report.passed is False

    def test_yuan_input_rewritten_as_yiyuan(self) -> None:
        """源给**元**、结论写**亿元** —— 同一个数（真实端到端实测的现场）。

        `板块资金流:银行` 的 `value=-896960544.0`（元），而 A11/A20 的结论
        很自然地写成「主力净流出 **8.97亿元**」——旧判据读不懂这个改写，
        于是给一段**正确**的结论挂上「未在输入数据中找到的数字：8.97亿」。
        """
        report = _verify(
            "银行板块主力净流出8.97亿元",
            "## 输入数据\n- 板块资金流:银行 2026-09-28=-896960544.0 元 c0.80\n",
        )
        assert report.unverified_numbers == [], report.unverified_numbers

    def test_yuan_rewrite_does_not_manufacture_green(self) -> None:
        """反向：数值真的不对时照样报（不许因为豁免了单位就把错的也放过）。"""
        report = _verify(
            "银行板块主力净流入1.20亿元",
            "## 输入数据\n- 板块资金流:银行 2026-09-28=-896960544.0 元 c0.80\n",
        )
        assert report.passed is False
        assert any("1.20" in n for n in report.unverified_numbers)


# ============================================================
# 2. 末位精度：90.18% ↔ 90.183
# ============================================================


class TestRoundingPrecision:
    def test_two_decimal_rounding_matches_three_decimal_input(self) -> None:
        """§19.16.5 现场 2：结论写 90.18%，输入是 90.183。"""
        report = _verify(
            "资产负债率90.18%，处于行业高位",
            "## 输入数据\n- 资产负债率 2026-06-30=90.183 c0.95\n",
        )
        assert report.unverified_numbers == []
        assert report.passed is True

    def test_ratio_suffix_also_uses_tolerance(self) -> None:
        report = _verify(
            "PE 6.76倍",
            "## 输入数据\n- PE(TTM) 2026-09-28=6.7597 c0.95\n",
        )
        assert report.unverified_numbers == []

    def test_out_of_tolerance_still_flagged(self) -> None:
        """90.18 的容差是 ±0.005 —— 91.7 差得远，必须报。"""
        report = _verify(
            "资产负债率90.18%",
            "## 输入数据\n- 资产负债率 2026-06-30=91.7 c0.95\n",
        )
        assert report.passed is False

    def test_tolerance_is_not_fuzzy(self) -> None:
        """边界：差的最后一位就是超差（90.185 vs 90.18 → 报）。"""
        report = _verify("读数90.18%", "## 输入数据\n- 读数=90.186 c0.95\n")
        assert report.passed is False

    def test_precision_upgrade_also_matches(self) -> None:
        """结论比输入**更精确**（90.1830 vs 90.183）同样算同一个数。"""
        report = _verify("读数90.1830%", "## 输入数据\n- 读数=90.183 c0.95\n")
        assert report.unverified_numbers == []


# ============================================================
# 3. 反向判据：整数不放宽（防"日期恰巧命中"）
# ============================================================


class TestIntegerNotRelaxed:
    def test_integer_percent_does_not_match_date_digits(self) -> None:
        """编一个 `18%`，输入里只有日期 `2026-09-18` → **必须报警**。"""
        report = _verify(
            "净利率18%，盈利能力强",
            "## 输入数据\n- 公告日期 2026-09-18 c1.00\n",
        )
        assert report.passed is False
        assert any("18" in n for n in report.unverified_numbers)

    def test_integer_percent_literal_still_passes(self) -> None:
        report = _verify("净利率18%", "## 输入数据\n- 净利率=18% c0.95\n")
        assert report.passed is True

    def test_integer_ratio_does_not_match_bare_number(self) -> None:
        report = _verify("PE 50倍", "## 输入数据\n- 换手率=50 c0.95\n")
        assert report.passed is False


# ============================================================
# 4. 原判据不许退化（既有行为回归）
# ============================================================


class TestExistingBehaviourKept:
    def test_fabricated_percent_still_flagged(self) -> None:
        report = _verify("PPI 3.5%，超出预期", "## 输入数据\nCPI 2.1%\n")
        assert report.passed is False
        assert any("3.5" in n for n in report.unverified_numbers)

    def test_literal_hit_passes(self) -> None:
        report = _verify("CPI 2.1%，符合预期", "## 输入数据\nCPI 2.1%\n")
        assert report.passed is True

    def test_empty_output_passes(self) -> None:
        assert _verify("", "CPI 2.1%").passed is True

    def test_empty_context_skips_number_check(self) -> None:
        """无输入上下文时不做数字溯源（原行为：没有可比对的基线）。"""
        assert _verify("CPI 2.1%", "").passed is True

    def test_confidence_still_drops_with_issues(self) -> None:
        report = _verify(
            "PE 50倍，PB 8倍，建议关注600999", "## 输入数据\n股票000001\n",
        )
        assert report.confidence < 1.0
        assert report.passed is False

    def test_null_output_still_passes(self) -> None:
        assert _verify("", "").passed is True


@pytest.mark.parametrize(
    ("output", "context", "grounded"),
    [
        ("总资产1.70万亿", "- 总资产=17028亿", True),
        ("总资产1.70万亿", "- 总资产=17.0亿", False),
        ("占比90.18%", "- 占比=90.183", True),
        ("占比90.18%", "- 占比=90.19", False),
        ("市值5000万", "- 市值=0.5亿", True),
        ("市值5000万", "- 市值=5亿", False),
    ],
)
def test_numeric_equivalence_table(output: str, context: str, grounded: bool) -> None:
    """参数化对照表：每条都写清"该过"还是"该报"。"""
    report = _verify(output, context)
    assert (report.unverified_numbers == []) is grounded, report.unverified_numbers
