"""规划结果的**三方一致**判据（`CHG-0192`）。

## 这条判据防的是什么

`plan()` 的返回值被 `supervisor` 按**键名**逐处消费。在此之前它有**两份各自手写的口径**：

1. `_PLAN_JSON_SCHEMA`（给受约束解码用的 JSON Schema，5 个 property）；
2. `plan()` 末尾手写的 `return {...}` 字面量（5 个键）。

两者**没有任何机器判据保证一致**：往一处加字段、另一处忘了改，不会有任何报错，
只在下游表现为"某个字段是 None"。实测：它们当前**恰好一致** —— 但那是**巧合**。

现在多了第三份：`PlanResult`（类型契约）。判据钉住**三方相等**。

## 判据强度

- `test_schema_properties_match_the_model_fields` —— 形状判据（静态，无需起运行时）。
- `test_plan_returns_the_contract_keys` —— ★ **行为判据**：真的跑一次 `plan()`
  （喂 FakeGateway），断言返回的键 == schema properties，且 `analysis_type`
  落在枚举内。往 `PlanResult` 加字段而忘了同步 schema ⇒ 立刻红。
"""

from __future__ import annotations

from typing import Any

from src.orchestration.planner import (
    _PLAN_ANALYSIS_TYPES,
    _PLAN_JSON_SCHEMA,
    LLMSupervisorPlanner,
    PlanResult,
)


def test_schema_enum_is_derived_from_the_single_source():
    """★ schema 的 enum 必须**派生自** `_PLAN_ANALYSIS_TYPES`，不是另抄一份。

    把 enum 改回手写字面量（哪怕值一样）⇒ 本判据红 —— 因为那样它又成了
    第二份"合法取值"清单，改了常量不会同步。
    """
    enum = _PLAN_JSON_SCHEMA["properties"]["analysis_type"]["enum"]
    assert tuple(enum) == _PLAN_ANALYSIS_TYPES, (
        f"schema enum {enum} 与唯一来源 {_PLAN_ANALYSIS_TYPES} 不一致"
    )


def test_model_literal_matches_the_single_source():
    """`PlanResult.analysis_type` 的字面量取值必须 == 唯一来源。"""
    import typing

    hints = typing.get_type_hints(PlanResult)
    literal_args = typing.get_args(hints["analysis_type"])
    assert set(literal_args) == set(_PLAN_ANALYSIS_TYPES), (
        f"模型字面量 {sorted(literal_args)} != 唯一来源 {sorted(_PLAN_ANALYSIS_TYPES)}"
    )


def test_schema_properties_match_the_model_fields():
    """形状判据：schema 的 property 集合 == 模型的字段集合。"""
    props = set(_PLAN_JSON_SCHEMA.get("properties", {}))
    fields = set(PlanResult.model_fields)
    assert props == fields, (
        f"schema 与模型字段不一致：schema 多 {sorted(props - fields)}，"
        f"模型多 {sorted(fields - props)}"
    )


def test_required_fields_are_the_non_defaulted_ones():
    """`required` 必须是**没有默认值**的那几个（否则模型能构造出 schema 不允许的）。

    `analysis_type` 无默认（必填）；`agents` 有默认 `[]` —— 但 schema 把它列为
    required，因为**受约束解码**要求模型必须显式给出这两个键。这里钉住
    "required ⊆ 模型字段"，并显式记录 `agents` 的差异是刻意的。
    """
    required = set(_PLAN_JSON_SCHEMA.get("required", []))
    fields = set(PlanResult.model_fields)
    assert required <= fields, f"required 里有模型没有的字段：{sorted(required - fields)}"
    assert "analysis_type" in required, "analysis_type 必须 required"
    assert "agents" in required, (
        "agents 在 schema 里是 required（约束解码要求显式给出），"
        "即便模型给了默认值 —— 这个差异是刻意的，别顺手删"
    )


# ======================================================================
# ★ 行为判据：真的跑一次 plan()
# ======================================================================

class _FakeGateway:
    """最小 LLM 网关替身：返回一份合法的规划 JSON。"""

    def __init__(self, payload: str) -> None:
        self._payload = payload
        self.calls = 0

    async def complete(self, task_tier: str, system: str, prompt: str,
                       **kwargs: Any) -> Any:
        self.calls += 1

        class _Resp:
            content = self._payload  # type: ignore[misc]

        return _Resp()


def _run_plan(payload: str) -> dict[str, Any]:
    import asyncio

    planner = LLMSupervisorPlanner(_FakeGateway(payload))  # type: ignore[arg-type]
    out = asyncio.run(planner.plan(
        "分析一下银行板块",
        "银行",
        {"A08_macro": {"name": "宏观"}, "A17_recommend": {"name": "建议"}},
    ))
    assert out is not None, "plan() 返回 None（解析失败）—— 替身的 payload 不合法？"
    return out


def test_plan_returns_the_contract_keys():
    """★ 行为判据：`plan()` 实际返回的键必须 == schema properties。

    往 `PlanResult` 加一个字段（或往 `return` 里多塞一个键）而没同步 schema
    ⇒ 立刻红 —— 那正是"两份手写口径"会漂移的地方。
    """
    out = _run_plan(
        '{"analysis_type": "industry", "target": "银行", '
        '"agents": ["A08_macro"], "indicators": ["PE(TTM)"], "reasoning": "因为"}'
    )
    assert set(out) == set(_PLAN_JSON_SCHEMA["properties"]), (
        f"plan() 返回的键 {sorted(out)} != schema properties "
        f"{sorted(_PLAN_JSON_SCHEMA['properties'])}"
    )
    assert out["analysis_type"] in _PLAN_ANALYSIS_TYPES
    assert out["target"] == "银行"


def test_unknown_analysis_type_falls_back_with_a_warning():
    """未知 `analysis_type` 必须**回落**到 full 并留痕（不许原样透传）。

    原样透传的后果：supervisor 里那一串 `if analysis_type == ...` 全不命中，
    静默退化成一个谁都没设计的形态。受约束解码本该拦住它，这是兜底。
    """
    out = _run_plan('{"analysis_type": "quantum", "agents": ["A08_macro"]}')
    assert out["analysis_type"] == "full", (
        f"未知取值被原样透传：{out['analysis_type']!r}"
    )


def test_plan_result_rejects_a_type_outside_the_enum():
    """反证：模型层真的会拒绝枚举外的取值（否则上面那条回落判据是装饰）。"""
    import pydantic
    import pytest

    with pytest.raises(pydantic.ValidationError):
        PlanResult(analysis_type="quantum")      # type: ignore[arg-type]
