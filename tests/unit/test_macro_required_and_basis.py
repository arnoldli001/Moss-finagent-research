"""★ 宏观「必查项 + 口径」的护栏（2026-09-30）。

## 触发它的用户口径

> 「`fred:*` 接进 A08 的**取证清单与口径示例**，**优先从数据处理侧解决**
>  （比如把"美债收益率/失业率"作为宏观结论的**必查项**写进**数据侧的组装逻辑**），
>  而不是写进 prompt。」

## 守的四件事

1. **必查项确定性补齐**：宏观口径的问句一定带中国四件套 **+ 美国/利率侧 9 条**，
   不依赖 LLM 记不记得、也不依赖关键词碰巧命中；
2. **不许把"已停产"的 id 排进计划** —— `us_*` 五条的源已停更并 `enabled: false`，
   排进去只会产出"无连接器/无数据"的缺口（**自己制造缺口**）；
3. **必查项必须真的可达**：每条都得在登记表里且 `enabled`（判据**现读 registry**，
   所以摘掉一条、或改回停产，测试立刻红）；
4. **口径随数据走**：`macro_basis_for()` 只对**实际拿到的**指标给口径，
   未知指标如实不返回（不编）。
"""

from __future__ import annotations

import pytest

from src.orchestration.supervisor import (
    _MACRO_BASIS,
    _MACRO_INDICATORS,
    _US_RATES_INDICATORS,
    ensure_macro_indicators,
    macro_basis_for,
)

#: 五个**已停产**的旧登记（源：AkShare 东财 `macro_usa_*`，实测最新行 2025-09 且值为 nan）。
RETIRED_US_IDS = ("us_unemployment", "us_nonfarm", "us_pce",
                  "us_core_cpi", "us_fed_rate")


def test_macro_query_gets_both_groups_deterministically():
    """★ 宏观问句 ⇒ 中国四件套 **+** 美国/利率侧必查项，一次到位。"""
    out = ensure_macro_indicators("macro", "美联储降息节奏怎么看", [])
    for ind in _MACRO_INDICATORS:
        assert ind in out, f"中国宏观 {ind} 没被补齐"
    for ind in _US_RATES_INDICATORS:
        assert ind in out, f"美国/利率必查项 {ind} 没被补齐"


def test_keyword_hit_also_triggers_even_if_llm_says_full():
    """LLM 把宏观问判成 `full` 时也要补（它只看"问了几件事"）。"""
    out = ensure_macro_indicators("full", "美债收益率与失业率怎么看", [])
    assert "fred:DGS10" in out and "fred:UNRATE" in out


def test_non_macro_query_is_untouched():
    """★ 反向判据：非宏观问句**一条都不许补**（否则每个问句都背着 13 条取数）。"""
    resolved = ["PE(TTM):600036", "股息率TTM:600036"]
    assert ensure_macro_indicators("stock", "招商银行怎么样", resolved) == resolved


def test_retired_us_ids_never_enter_the_plan():
    """★★ 已停产的 `us_*` 不许再排进计划（它们的源停了，排了就是自己制造缺口）。"""
    for text in ("美联储降息", "美国失业率与CPI", "宏观环境如何"):
        out = ensure_macro_indicators("macro", text, [])
        bad = [i for i in out if i in RETIRED_US_IDS or i.startswith("us_")]
        assert not bad, f"「{text}」的计划里混进了已停产的 {bad}"


def test_required_items_are_registered_and_enabled():
    """★★ 判据**现读登记表**：每条必查项都必须登记且 `enabled`。

    这条红了意味着"必查项里有一条根本取不到" —— 要么补登记，要么把它
    从必查项里拿掉（**不许留着一条永远取不到的必查项**：那会让结论每次都
    少一维，而用户看不出是"没量到"还是"没有"）。
    """
    from src.infrastructure.catalog.registry import get_registry

    reg = get_registry()
    missing: list[str] = []
    for ind in list(_MACRO_INDICATORS) + list(_US_RATES_INDICATORS):
        meta = reg.get(ind)
        if meta is None:
            missing.append(f"{ind}（未登记）")
        elif not meta.enabled:
            missing.append(f"{ind}（已停产）")
    assert not missing, "必查项里有取不到的：\n  " + "\n  ".join(missing)


def test_retired_ids_are_registered_as_disabled():
    """★ 反向：五条旧登记必须**还在登记表里、且 enabled=false**。

    不删条目是刻意的（"改口径要留废止痕迹"：下一个人看到 `us_unemployment`
    得能查出"它为什么死了、替代是哪个"）。所以判据是"在册但停产"，
    不是"消失"。
    """
    from src.infrastructure.catalog.registry import get_registry

    reg = get_registry()
    for ind in RETIRED_US_IDS:
        meta = reg.get(ind)
        assert meta is not None, f"{ind} 不该被删（要留废止痕迹）"
        assert meta.enabled is False, f"{ind} 必须是停产状态"


def test_basis_only_for_what_we_actually_have():
    """口径跟着**实际拿到的**数据走：未知指标不编口径。"""
    got = macro_basis_for(["fred:DGS10", "fred:UNRATE", "某个没登记的指标"])
    assert "fred:DGS10" in got and "10 年期" in got["fred:DGS10"]
    assert "fred:UNRATE" in got
    assert "某个没登记的指标" not in got, "未知指标不许编口径"


def test_basis_covers_every_required_item():
    """★ 必查项必须**都有口径**（否则"这个数是什么"没人知道，模型只能猜）。"""
    no_note = [i for i in list(_MACRO_INDICATORS) + list(_US_RATES_INDICATORS)
               if i not in _MACRO_BASIS]
    assert not no_note, f"这些必查项没有口径说明：{no_note}"


def test_basis_notes_state_the_unit_not_just_a_name():
    """口径要说清**单位/量纲/是否需派生** —— 光写名字等于没写。

    实测踩过：`fred:PAYEMS` 是**水平值（千人）**而模型会当"新增非农"读；
    `fred:CPILFESL` 是**指数**而会被当"同比 %"读。
    """
    assert "千人" in _MACRO_BASIS["fred:PAYEMS"]
    assert "不是「新增」" in _MACRO_BASIS["fred:PAYEMS"]
    assert "指数" in _MACRO_BASIS["fred:CPILFESL"]
    assert "指数" in _MACRO_BASIS["fred:PCEPILFE"]
    assert "%" in _MACRO_BASIS["fred:DGS10"]
