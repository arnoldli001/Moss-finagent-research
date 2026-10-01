"""平台自有数据八族的 **prompt 教学**护栏（`docs/PRD.md` §19.17 的最后一层）。

## 这一层为什么必须单独守（AGENTS.md 原话）

> 「同一缺陷会在多层重复出现，改完一层必须问『还有哪一层会把它吃掉』……
>   实测同一个"数据到了但用不上"的缺陷在本项目里出现过**四个位置**：
>   白名单 · 连接器 · 登记表 · **prompt**（数据进得了上下文，
>   但 Agent 的 `system_prompt` 从没提过它 ⇒ 模型不知道那是信号）。」

2026-09-29 实测本仓真实状态：八族数据**已经进了计划、进了采集、过了白名单**，
而 A09/A10/A11/A13–A16/A20 的 prompt 里**一个字都没提**
（`估值水位` / `概念拥挤度` / `行业拥挤度` / `板块资金流` / `行业轮动` /
`主线报警` / `个股告警` / `解禁计划`）—— 模型看到
`- 行业拥挤度:银行 2026-09-24=0.257` 只会当成一行陌生文本。

## 本文件守的四件事（判据全部派生，零手写清单）

1. **族清单只有一份**：教学模块声明的族 == `PlatformDataConnector._FAMILY_CALIBERS` 的键
   （写两份必然漂移，而漂移的形式是"教了一个连接器没有的族"或反之）；
2. **谁看得见就得教谁**：白名单里出现平台族的 Agent（8 个）**必须**在 prompt 里
   带上教学块 —— 这条是**从白名单现读**的，新增第 9 个 Agent 时它会立刻红；
3. **教学块必须点名它看得见的每一个族**（不能只写一句"注意平台数据"）；
4. **"教了"必须能在真实对象上验到**（`system_prompt` 或 `_requirements()` 的**真实输出**里
   含教学块标记）—— 不是"我以为拼进去了"。
"""

from __future__ import annotations

import importlib

import pytest

from src.domain.agents.analysis.base import AnalysisPayload
from src.domain.agents.analysis.platform_data_teaching import (
    PLATFORM_FAMILIES,
    render_platform_data_teaching,
)
from src.infrastructure.connectors.platform_data_connector import (
    PlatformDataConnector,
)
from src.orchestration.supervisor import _AGENT_DATA_WHITELIST

#: 教学块的"指纹"（出现它 = 这一块拼进去了）
_MARKER = "平台自有数据使用规则"

#: agent_id → `模块:类`。**手写，但配了一条派生的完整性断言**
#: （`test_teaching_map_covers_every_agent_that_can_see_a_family`）：
#: 白名单里新出现一个能看见平台族的 Agent 而没更新本表 → 立刻红。
#: 这与 `test_contract_consistency._AGENT_CLASSES` 是同一套写法。
_AGENT_CLASSES: dict[str, str] = {
    "A09_meso": "src.domain.agents.analysis.meso.agent:MesoAnalysisAgent",
    "A10_micro": "src.domain.agents.analysis.micro.agent:MicroAnalysisAgent",
    "A11_fin_risk": "src.domain.agents.analysis.risk.agent:RiskAnalysisAgent",
    "A13_tech": "src.domain.agents.industry.tech.agent:TechIndustryAgent",
    "A14_consumer": "src.domain.agents.industry.consumer.agent:ConsumerIndustryAgent",
    "A15_cyclical": "src.domain.agents.industry.cyclical.agent:CyclicalIndustryAgent",
    "A16_pharma": "src.domain.agents.industry.pharma.agent:PharmaIndustryAgent",
    "A20_generic_industry": (
        "src.domain.agents.industry.generic.agent:GenericIndustryAgent"
    ),
}


def _families_seen_by(agent_id: str) -> list[str]:
    """该 Agent 的白名单里出现了哪些平台族（现读白名单）。"""
    keywords = _AGENT_DATA_WHITELIST.get(agent_id, ())
    return [f for f in PLATFORM_FAMILIES if any(k in f for k in keywords)]


def _agent_prompt_text(agent_id: str) -> str:
    """该 Agent **真实**会发给模型的文本（system_prompt ∪ `_requirements()`）。"""
    module_path, class_name = _AGENT_CLASSES[agent_id].split(":")
    cls = getattr(importlib.import_module(module_path), class_name)
    text = str(getattr(cls, "system_prompt", "") or "")
    # 行业 Agent 的教学块在 `_requirements()`（按请求的框架/行业动态渲染）
    req = getattr(cls, "_requirements", None)
    if req is not None:
        instance = cls.__new__(cls)
        instance.agent_id = agent_id          # 只读渲染需要它（不建 gateway）
        try:
            text += str(req(instance, AnalysisPayload(focus="600036", hint={})))
        except Exception as exc:  # noqa: BLE001 渲染失败要在断言里看得见，不吞
            text += f"<<_requirements 渲染失败: {type(exc).__name__}: {exc}>>"
    return text


# ============================================================
# ① 族清单只有一份
# ============================================================


def test_teaching_family_list_matches_the_connector() -> None:
    """★ 教学模块声明的族 == 连接器实际实现的族（**派生**，不手写两份）。"""
    implemented = tuple(PlatformDataConnector().get_capabilities()["indicators"])
    implemented_families = {str(i).split(":", 1)[0] for i in implemented}
    assert set(PLATFORM_FAMILIES) == implemented_families, (
        "教学模块与连接器的族清单不一致 —— 写两份必然漂移：\n"
        f"  只在教学里：{sorted(set(PLATFORM_FAMILIES) - implemented_families)}\n"
        f"  只在连接器：{sorted(implemented_families - set(PLATFORM_FAMILIES))}"
    )


def test_every_family_is_named_in_the_common_block() -> None:
    """公共块必须点名**每一个**族（只写一句"注意平台数据"等于没教）。"""
    block = render_platform_data_teaching("A10_micro")
    missing = [f for f in PLATFORM_FAMILIES if f not in block]
    assert not missing, f"公共教学块没点名这些族：{missing}"


# ============================================================
# ② 谁看得见就得教谁（从白名单现读）
# ============================================================


def test_teaching_map_covers_every_agent_that_can_see_a_family() -> None:
    """★ 派生完整性：白名单里能看见平台族的 Agent，必须都在教学映射表里。

    没有这条，新增一个 `A21_xxx`（白名单里写了 `行业拥挤度`）时**没人会想起**教它
    —— 数据进了上下文而模型不知道那是信号，且**不报错**。
    """
    visible = {
        aid for aid in _AGENT_DATA_WHITELIST if _families_seen_by(aid)
    }
    assert visible, "白名单里没有任何 Agent 认领平台族（判据坏了或白名单被清空）"
    missing = sorted(visible - set(_AGENT_CLASSES))
    extra = sorted(set(_AGENT_CLASSES) - visible)
    assert not missing, (
        f"这些 Agent 的白名单里有平台族，但教学映射表没覆盖：{missing}\n"
        "→ 补 `_AGENT_CLASSES` 一行，并确认它的 prompt 真的带了教学块。"
    )
    assert not extra, (
        f"教学映射表里有、白名单里却看不见平台族的 Agent：{extra}\n"
        "→ 删掉该行（否则它是在教一个拿不到的族）。"
    )


@pytest.mark.parametrize("agent_id", sorted(_AGENT_CLASSES))
def test_agent_prompt_carries_the_teaching_block(agent_id: str) -> None:
    """★★ 真实对象上验：教学块的**指纹**必须出现在它会发给模型的文本里。"""
    text = _agent_prompt_text(agent_id)
    assert _MARKER in text, (
        f"{agent_id} 的 prompt 里没有平台数据教学块（指纹 {_MARKER!r} 未命中）——\n"
        "数据进得了上下文，但模型不知道那是信号（AGENTS.md 的第四层）。"
    )
    # 还要点名它**看得见**的族（不能只有公共块、没有角色块）
    seen = _families_seen_by(agent_id)
    unseen = [f for f in seen if f not in text]
    assert not unseen, f"{agent_id} 的教学块没点名它看得见的族：{unseen}"


@pytest.mark.parametrize("agent_id", sorted(_AGENT_CLASSES))
def test_role_block_is_the_right_one(agent_id: str) -> None:
    """角色必须显式登记（否则静默落到 macro 兜底，教的是另一套口径）。"""
    block = render_platform_data_teaching(agent_id)
    if agent_id.startswith(("A13", "A14", "A15", "A16", "A20")):
        assert "行业拥挤度:{本行业}" in block, "行业 Agent 必须拿到行业角色的教学块"
    elif agent_id == "A10_micro":
        assert "估值水位" in block and "概念拥挤度" in block
        assert "行业拥挤度:{本行业}" not in block, "个股 Agent 不该拿到行业角色的口径"
    elif agent_id == "A09_meso":
        assert "这三个族就是你的主战场" in block or "三族就是你的主战场" in block
    elif agent_id == "A11_fin_risk":
        assert "减持压力" in block


def test_industry_teaching_forbids_treating_stale_as_today() -> None:
    """行业角色块必须写清"报告落伍要说落伍"（否则会把旧行情当今天）。"""
    block = render_platform_data_teaching("A14_consumer")
    assert "is_stale" in block and "落伍" in block


def test_macro_role_warns_about_conflicts() -> None:
    """宏观角色块要求"不一致时指出来，不要抹平"（防交叉验证被悄悄压平）。"""
    block = render_platform_data_teaching("A08_macro")
    assert "不一致" in block


# ============================================================
# ③ "prompt 声称了 → 连接器必须真的支持"（与 `test_contract_consistency`
#    的 `_PROMPT_DECLARED_FAMILIES` 同一套纪律，只是**平台族这一块**放在本文件，
#    因为那段教学文案是这里渲染的，出处与探测串必须成对维护）
# ============================================================

#: agent_id → `(prompt 出处原文, 该声称对应的指标探测串)`
#: ⚠️ 出处必须是**逐字**的 prompt 子串（下面 `test_prompt_platform_probes_are_real`
#: 会把它钉回原文；prompt 改了而这里没改 → 立刻红）。
_PROMPT_PLATFORM_PROBES: dict[str, tuple[str, tuple[str, ...]]] = {
    "A09_meso": (
        "这三族就是你的主战场",
        ("行业拥挤度:银行", "板块资金流:银行", "行业轮动:银行"),
    ),
    "A10_micro": (
        "你的估值结论必须与它一致",
        ("估值水位:600036", "概念拥挤度:600036"),
    ),
    "A11_fin_risk": (
        "潜在**减持压力**",
        ("解禁计划:600036", "个股告警:600036"),
    ),
    **{
        aid: (
            "行业拥挤度:{本行业}",
            ("行业拥挤度:银行", "板块资金流:银行", "行业轮动:银行"),
        )
        for aid in ("A13_tech", "A14_consumer", "A15_cyclical", "A16_pharma",
                    "A20_generic_industry")
    },
}


def test_prompt_platform_probes_are_real() -> None:
    """★ 自证：映射表里的"出处"必须真的是该 Agent prompt 的**原文子串**。

    没有这条，下面那条断言会守着一个**不存在的声称**给出虚假的安全感
    （AGENTS.md：「自己的检查脚本必须先自证」）。
    """
    problems: list[str] = []
    for agent_id, (quote, _probes) in _PROMPT_PLATFORM_PROBES.items():
        if quote not in _agent_prompt_text(agent_id):
            problems.append(f"{agent_id}: {quote!r}")
    assert not problems, (
        "以下「出处」在 prompt 里找不到（映射表已漂移，按当前 prompt 改写）：\n"
        + "\n".join(f"  · {p}" for p in problems)
    )


def test_prompt_platform_claims_have_a_connector() -> None:
    """★★ ③→①：prompt 声称会读的平台族，**必须**有连接器 `supports()`。

    这是"声称与实现必须有一边让步"的机器版本：prompt 写了却没人实现
    ⇒ 模型会去找一个永远不存在的数据，并把"没量到"写成"平台没有"。
    """
    problems: list[str] = []
    for agent_id, (quote, probes) in _PROMPT_PLATFORM_PROBES.items():
        for probe in probes:
            if not any(cls.supports(probe) for cls in _connector_classes()):
                problems.append(f"{agent_id}（出处 {quote!r}）→ {probe}")
    assert not problems, (
        "以下「prompt 声称的族」没有任何连接器支持：\n"
        + "\n".join(f"  · {p}" for p in problems)
        + "\n→ 三种合法归宿：① 补连接器实现；② 若生产者不在连接器里，登记说明；"
        "③ 从 prompt 删掉该声称。**不许两边都不动**。"
    )


def _connector_classes() -> tuple[type, ...]:
    """实现面：至少包含平台自有数据连接器（本文件只声称它实现的族）。"""
    return (PlatformDataConnector,)


def test_probe_table_covers_every_taught_agent() -> None:
    """派生完整性：被教的 8 个 Agent 都必须在探测表里有条目。"""
    assert set(_PROMPT_PLATFORM_PROBES) == set(_AGENT_CLASSES)
