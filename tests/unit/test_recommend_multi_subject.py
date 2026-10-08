"""A17_recommend 的**多标的逐只表态**（`CHG-0230`）。

## 修的是什么（§41.36 审计的 A17 项）

用户问「…未来半年能否持有高股息的**宁波银行**和**中国神华**？」时：

    `RecommendationPayload.focus: str`            ← **单值**（= 中国神华）
    输出规格只有**一套** stance / position_advice / expected_return_3_6m
    ⇒ **两只票共用一个立场**，用户无法知道"中性偏多"是针对哪只；
      若模型只挑了最像的那只写，**另一只在最终交付里根本不存在**。

★ 这是"**结论出不来**"的最末端一环：数据层已经为两只票各采了一份
（`CHG-0216`/`CHG-0219`），但**最终交付只有一个立场**。

## 本文件守的四件事

1. ★ **单标的输出与修复前逐字一致** —— `subjects` 为空时，
   `output_spec` 与 `focus` 段落**一个字都不多**（结构性保证：只在多标的时追加）；
2. ★★ **多标的时明令逐只、且数量相等、不许合并**；
3. ★ **缓存 anchor 必须带上标的清单** —— 否则"宁波银行+中国神华"与"宁波银行"
   两次问句会共用同一个 anchor（focus 都是中国神华），
   语义层复用会把**只覆盖一只**的旧结论发给要两只的那次；
4. **三处注入点共用一份实现**（`_a17_subjects`），少于 2 只返回 `[]`。

跑法：
    uv run python -m pytest tests/unit/test_recommend_multi_subject.py -q
"""
from __future__ import annotations

from src.domain.agents.decision.recommend.agent import (
    RecommendationAgent,
    RecommendationPayload,
)
from src.orchestration.supervisor import _a17_subjects

BANK = "002142"
COAL = "601088"
_ANALYSES = [{"agent_id": "A10_micro", "conclusion": "估值合理", "confidence": "medium",
              "result": {}}]


def _agent() -> RecommendationAgent:
    # 既有测试（`test_liquidity_integration.py:211`）就是这么构造的：
    # `_render_prompt` 不需要真网关。
    return RecommendationAgent(gateway=None)  # type: ignore[arg-type]


def _payload(subjects: list[dict] | None = None, *, focus: str = "中国神华"
             ) -> RecommendationPayload:
    return RecommendationPayload(
        analyses=_ANALYSES, focus=focus, user_query="能否持有？",
        subjects=subjects or [],
    )


def _multi() -> list[dict]:
    return [{"code": BANK, "name": "宁波银行"}, {"code": COAL, "name": "中国神华"}]


# ===========================================================================
# ① 解析层：门槛是 2
# ===========================================================================


def test_multi_subjects_requires_at_least_two() -> None:
    """★ 门槛是 **2** —— 单标的时"一套立场"本来就正确，必须回空。"""
    assert RecommendationAgent._multi_subjects(_payload([])) == []          # noqa: SLF001
    assert RecommendationAgent._multi_subjects(                                # noqa: SLF001
        _payload([{"code": BANK, "name": "宁波银行"}])) == []


def test_multi_subjects_dedupes_and_keeps_order() -> None:
    got = RecommendationAgent._multi_subjects(_payload([                      # noqa: SLF001
        {"code": BANK, "name": "宁波银行"},
        {"code": COAL, "name": "中国神华"},
        {"code": BANK, "name": "宁波银行"},          # 重复不重复计
        {"code": "", "name": "空代码"},               # 无代码丢掉
    ]))
    assert got == [{"code": BANK, "name": "宁波银行"},
                   {"code": COAL, "name": "中国神华"}], got


def test_non_dict_subject_is_rejected_by_the_contract() -> None:
    """★ 非字典元素**在类型层就被挡住**（pydantic），到不了 `_multi_subjects`。

    ★ 这条是本轮**先写错**才发现的：我原本把 `"不是字典"` 塞进 payload 里
    想测"被忽略"，结果报 `ValidationError: subjects.4 Input should be a valid
    dictionary` —— 契约比我的实现更早挡住它。
    ⇒ `_multi_subjects` 里那句 `isinstance(s, dict)` 是**绕过校验的调用方**
      的纵深防御，不是主判据。把它钉下来，免得下次有人以为主判据在那里。
    """
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _payload([{"code": BANK}, "不是字典"])


def test_a17_subjects_from_state_requires_two_codes() -> None:
    """★ 编排层：`focus_stock_codes` 少于 2 ⇒ `[]`（A17 退回单标的口径）。"""
    assert _a17_subjects({"focus_stock_codes": ()}) == []
    assert _a17_subjects({"focus_stock_codes": (BANK,)}) == []
    # 缺键也不许炸（既有 state 装配点可能没有这个键）
    assert _a17_subjects({}) == []


def test_a17_subjects_carries_the_primary_name() -> None:
    """主焦点的中文名带上；**非主焦点的名字拿不到**（已知边界，如实返回空串）。"""
    got = _a17_subjects({
        "focus_stock_codes": (COAL, BANK),
        "focus_stock_code": COAL, "focus_stock_name": "中国神华",
    })
    assert got == [{"code": COAL, "name": "中国神华"},
                   {"code": BANK, "name": ""}], got


# ===========================================================================
# ② 输出规格：单标的**逐字不变**，多标的明令逐只
# ===========================================================================


def test_single_subject_output_spec_has_no_per_subject() -> None:
    """★ **回归护栏**：单标的时 prompt 里**不许出现** `per_subject`。

    这是"逐字不变"的可见证据 —— 只在多标的时追加，单标的连一个词都不多。
    """
    prompt, _anchor, _scope = _agent()._render_prompt(_payload([]))            # noqa: SLF001
    assert "per_subject" not in prompt
    assert "多标的" not in prompt
    assert "## 投研标的/主题\n中国神华\n" in prompt, prompt[:200]


def test_multi_subject_output_spec_demands_every_subject() -> None:
    """★★ **核心判据**：必须逐只、数量相等、不许合并。"""
    prompt, _anchor, _scope = _agent()._render_prompt(_payload(_multi()))      # noqa: SLF001
    assert "per_subject" in prompt
    assert "多标的" in prompt
    assert BANK in prompt and COAL in prompt
    assert "2 只各一条" in prompt, prompt[-400:]
    assert "不许合并" in prompt and "不许只写一只" in prompt


def test_multi_subject_focus_section_lists_both() -> None:
    prompt, _anchor, _scope = _agent()._render_prompt(_payload(_multi()))      # noqa: SLF001
    assert "宁波银行（002142）" in prompt and "中国神华（601088）" in prompt


def test_react_mode_puts_per_subject_inside_final_answer() -> None:
    """★ ReAct 分支：`per_subject` 必须写进 `final_answer` 里（那才是它的输出位）。"""
    prompt = _agent()._render_prompt(_payload(_multi()), react_mode=True)[0]   # noqa: SLF001
    assert "per_subject" in prompt
    assert "final_answer 内" in prompt, prompt[-500:]


# ===========================================================================
# ③ 缓存 anchor：不同标的清单**不许共用**一个 anchor
# ===========================================================================


def test_anchor_distinguishes_subject_sets() -> None:
    """★★ 否则语义层会把"只覆盖一只"的旧结论复用到"要两只"的那次。

    `focus` 在两种情况下**完全相同**（都是"中国神华"）——
    只有把标的清单并进 anchor 才能区分。
    """
    one = _agent()._render_prompt(_payload([]))[1]                             # noqa: SLF001
    two = _agent()._render_prompt(_payload(_multi()))[1]                       # noqa: SLF001
    assert one != two, "不同标的清单共用了同一个 anchor"


def test_anchor_includes_every_code() -> None:
    anchor = _agent()._render_prompt(_payload(_multi()))[1]                    # noqa: SLF001
    assert BANK in anchor and COAL in anchor, anchor
