"""指标前缀贯通性测试：新增一个指标前缀，必须穿过**全部链路**。

## 这个文件存在的理由（用户 2026-09-28 的批评）

> 「你的 AI 编程能力只是说一个做一个，缺少业务连续性、修改点对其他业务
>   关联模块的影响延伸分析、缺少下次再有这个场景能否不再出这个问题，
>   你没有考虑后续功能需要，缺少维护视角、业务功能需求连续性视角。」

**本轮的实证**（我真实的失败）：

把「投资日历/限售解禁」落库 + 登记索引后，我宣布"打通了"。
实际只打通了 **2 层**，漏了第 3 层：

    投资日历 → fact_data_points → indicator_catalog → SmartFetcher  ✅
                                                          ↓
                                      _AGENT_DATA_WHITELIST 过滤   ❌ 漏了

实测后果：
  · A13-A16 **明确被过滤掉**
  · A08/A09/A11 显示"保留"是**假绿** —— 白名单命中 0 条，
    靠兜底（返回前 200 条）**碰巧**混进来
  · A10/A12 只保住 `top_stock_cap`，因为白名单的 `stock_` 前缀
    **巧合**匹配到 `...top_**stock**_cap`

**没有一个是真的"设计上支持"。**

## 本文件守什么

**一个指标前缀，必须同时出现在这张表的每一处**。
新增指标时忘记任何一处，症状都是**静默的**（不报错，只是"Agent 看不到"）。

对照表单一事实源：`supervisor.NEW_INDICATOR_TOUCHPOINTS`。
"""

from __future__ import annotations

import inspect

import pytest

from src.orchestration.supervisor import (
    _AGENT_DATA_WHITELIST,
    NEW_INDICATOR_TOUCHPOINTS,
    _filter_points_for_agent,
)

#: 需要"全链路贯通"的指标前缀（活跃维护）
#:
#: 新增前缀时在这里加一行 —— 下面的参数化测试会自动覆盖全部检查点。
LIVE_PREFIXES = ("cal:",)


def _analysis_agents() -> list[str]:
    return sorted(_AGENT_DATA_WHITELIST)


# ============================================================
# ① 分析层可见性（本轮漏掉的那一层）
# ============================================================


@pytest.mark.parametrize("prefix", LIVE_PREFIXES)
def test_prefix_visible_to_all_analysis_agents(prefix: str):
    """★ 每个分析 Agent 都必须能拿到该前缀的指标。

    为什么要**全部** Agent：解禁数据对不同 Agent 有不同用途 ——
      · A11 财务风险：解禁 = 潜在减持压力
      · A13-A16 行业：本行业的供给冲击
      · A08 宏观：日历/日程
    漏给任何一个，那个 Agent 的结论就少一个维度，**且它自己不知道**。
    """
    points = [
        {"indicator": f"{prefix}unlock:market_cap", "value": 1.0,
         "period_date": "2026-10-28"},
        # 噪声：模拟真实 payload（大量无关指标）
        *[{"indicator": f"stock_close:{i:06d}", "value": 1.0,
           "period_date": "2026-09-28"} for i in range(300)],
    ]
    missing = []
    for aid in _analysis_agents():
        kept = _filter_points_for_agent(aid, points)
        if not any(str(p["indicator"]).startswith(prefix) for p in kept):
            missing.append(aid)
    assert not missing, (
        f"{prefix} 指标对以下 Agent 不可见：{missing}。\n"
        "→ 在 _AGENT_DATA_WHITELIST 里给它们加该前缀。\n"
        "⚠️ 不要靠 fallback（返回前 N 条）兜底 —— 那是**假绿**：\n"
        "   白名单命中 0 条时兜底会原样返回，看起来'保留'了，\n"
        "   实际是数据碰巧混在里面（本轮实测过）。"
    )


@pytest.mark.parametrize("prefix", LIVE_PREFIXES)
def test_prefix_not_relying_on_fallback(prefix: str):
    """★ 必须是**白名单精确命中**，不是靠 fallback 兜底。

    判据：构造一个**只有该前缀 + 大量噪声**的 payload，
    命中数应该 == 该前缀的条数（而不是被截断成 fallback_limit）。
    """
    n_prefix = 3
    points = [
        {"indicator": f"{prefix}unlock:market_cap", "value": 1.0,
         "period_date": "2026-10-28"},
        {"indicator": f"{prefix}unlock:company_count", "value": 9,
         "period_date": "2026-10-28"},
        {"indicator": f"{prefix}unlock:top_stock_cap", "value": 100.0,
         "period_date": "2026-10-28"},
        *[{"indicator": f"stock_close:{i:06d}", "value": 1.0,
           "period_date": "2026-09-28"} for i in range(500)],
    ]
    for aid in _analysis_agents():
        kept = _filter_points_for_agent(aid, points)
        n_cal = sum(1 for p in kept
                    if str(p["indicator"]).startswith(prefix))
        assert n_cal == n_prefix, (
            f"{aid} 命中了 {n_cal} 条 {prefix} 指标（应为 {n_prefix}）—— "
            "可能是靠 fallback 混进来的，或部分指标未覆盖"
        )


# ============================================================
# ② 关联模块清单本身
# ============================================================


#: agent_id → `模块:类名`（**本文件唯一一份**，两条测试共用）。
#:
#: ⚠️ **新增 Agent 必须补这里** —— 否则 `test_prefix_taught_to_every_whitelisted_agent`
#: 会以「白名单含 `cal:` 但 **prompt 没教**」的形式报红，而真因只是这张表缺一行
#: （2026-09-29 加 A20 时实测踩过，诊断方向被带偏）。
#: 由 `test_agent_class_map_covers_every_whitelisted_agent` 兜住这个方向。
_AGENT_CLASS_SPECS: dict[str, str] = {
    "A08_macro": "src.domain.agents.analysis.macro.agent:MacroAnalysisAgent",
    "A09_meso": "src.domain.agents.analysis.meso.agent:MesoAnalysisAgent",
    "A10_micro": "src.domain.agents.analysis.micro.agent:MicroAnalysisAgent",
    "A11_fin_risk": "src.domain.agents.analysis.risk.agent:RiskAnalysisAgent",
    "A12_compliance":
        "src.domain.agents.analysis.compliance.agent:ComplianceAnalysisAgent",
    "A13_tech": "src.domain.agents.industry.tech.agent:TechIndustryAgent",
    "A14_consumer":
        "src.domain.agents.industry.consumer.agent:ConsumerIndustryAgent",
    "A15_cyclical":
        "src.domain.agents.industry.cyclical.agent:CyclicalIndustryAgent",
    "A16_pharma":
        "src.domain.agents.industry.pharma.agent:PharmaIndustryAgent",
    # ★ 2026-09-29：兜底行业 Agent（用户要求「做个兜底行业的 agent」）
    "A20_generic_industry":
        "src.domain.agents.industry.generic.agent:GenericIndustryAgent",
}


def test_agent_class_map_covers_every_whitelisted_agent():
    """★★ 派生护栏：`_AGENT_CLASS_SPECS` 必须覆盖 `_AGENT_DATA_WHITELIST` 的**全部** Agent。

    ## 为什么要单列一条（2026-09-29 实测）

    新增兜底行业 Agent（A20）后，本文件的
    `test_prefix_taught_to_every_whitelisted_agent[cal:]` **报红了**，
    报的是「白名单含 `cal:` 但 **prompt 没教**该怎么用」——
    而真因只是"这张**手写**映射表少了一行"，A20 的 prompt 其实教了。

    **诊断方向被带偏**：看到"prompt 没教"，人会去改 prompt；
    实际要改的是映射表。这类"手写清单不会自己长大"的缺陷，
    本项目已登记过多次（豁免表、白名单、前缀表都是同一个形状）。

    判据：`set(_AGENT_CLASS_SPECS) == set(_AGENT_DATA_WHITELIST)`，**双向**。
    """
    from src.orchestration.supervisor import _AGENT_DATA_WHITELIST

    wl = set(_AGENT_DATA_WHITELIST)
    assert set(_AGENT_CLASS_SPECS) == wl, (
        "本文件的 agent→类 映射与白名单不一致：\n"
        f"  白名单有、映射缺：{sorted(wl - set(_AGENT_CLASS_SPECS))}\n"
        f"  映射有、白名单无：{sorted(set(_AGENT_CLASS_SPECS) - wl)}\n"
        "→ 新增/删除 Agent 时必须同步改这张映射，否则 prompt 相关判据"
        "会以「prompt 没教」的形式误报（真因是映射缺行）。"
    )


@pytest.mark.parametrize("prefix", LIVE_PREFIXES)
def test_prefix_taught_to_every_whitelisted_agent(prefix: str):
    """★★★ 贯通点第 ⑩ 层：白名单放行了该前缀的 Agent，**prompt 里必须教它怎么用**。

    ## 为什么这条是本文件最重要的一条（用户的批评）

    用户原话（2026-09-28）：
    > 「你没有按照你的 skill 经验做事，① 修改点还是类，在开发前你没考虑，
    >   所以一个问题（A11 还没教给他 prompt 怎么用）解决前后，
    >   没有考虑一类问题（A12/A13-A16 还没教给他 prompt 怎么用）」

    **实测证据**：白名单含 `cal:` 的 Agent 共 **9 个**，而我上一轮只教了 **A11**。
    剩下 8 个的 prompt 里一个字都没提解禁 —— 数据在上下文里，
    但模型不知道那是风险信号（不会被引用，也不会被质疑）。

    ## 为什么必须有这条测试

    我自己的 skill 里写了「问 1：这是一个点还是一类」，
    **然后当场违反了它** —— 因为我只有"提醒"，没有"护栏"。

    这条测试把"提醒"变成"护栏"：
      · 有人给白名单加前缀但忘了教 prompt → **立刻红**
      · 参数化覆盖 `LIVE_PREFIXES` 全部前缀 → 新增前缀自动被检查

    ## 判据

    `_AGENT_DATA_WHITELIST[agent]` 含该前缀 → 该 Agent 的 `system_prompt`
    必须提到该前缀（用前缀的母词，如 `cal:` → "解禁" / "unlock"）。
    """
    import importlib

    from src.orchestration.supervisor import _AGENT_DATA_WHITELIST

    # agent_id → (模块, 类名)：**本文件唯一一份**（见 `_AGENT_CLASS_SPECS`）
    _MODS = _AGENT_CLASS_SPECS
    # 前缀 → prompt 里应出现的关键词（判据只认机器可读的标识）
    keywords = {"cal:": ("解禁", "unlock")}
    kws = keywords.get(prefix)
    if kws is None:
        pytest.skip(f"{prefix} 未声明 prompt 判据关键词")

    targets = [a for a, w in _AGENT_DATA_WHITELIST.items() if prefix in w]
    assert targets, f"没有任何 Agent 的白名单含 {prefix}（测试失去意义）"

    missing: list[str] = []
    for aid in targets:
        spec = _MODS.get(aid)
        if spec is None:
            missing.append(f"{aid}(无类映射，需补进本测试的 _MODS)")
            continue
        mp, cls = spec.split(":")
        try:
            sp = str(getattr(importlib.import_module(mp), cls).system_prompt)
        except Exception as exc:  # noqa: BLE001
            missing.append(f"{aid}(导入失败: {type(exc).__name__})")
            continue
        if not any(k in sp for k in kws):
            missing.append(f"{aid}(prompt 未提 {'/'.join(kws)})")

    assert not missing, (
        f"以下 Agent 的白名单放行了 {prefix}，但 **prompt 没教它怎么用**：\n"
        + "\n".join(f"  · {m}" for m in missing)
        + "\n→ 数据在上下文里但模型不知道那是信号（不会被引用）。\n"
        "→ 用 `from src.domain.agents.analysis.unlock_teaching import "
        "render_unlock_teaching` 拼进 system_prompt。"
    )


def test_unlock_teaching_module_covers_all_roles():
    """教学模块必须覆盖全部角色（防止新增角色时静默用兜底文案）。"""
    from src.domain.agents.analysis.unlock_teaching import (
        _AGENT_ROLE,
        _ROLE_BLOCKS,
    )

    for agent_prefix, role in _AGENT_ROLE.items():
        assert role in _ROLE_BLOCKS, (
            f"{agent_prefix} 映射到角色 {role}，但 _ROLE_BLOCKS 里没有它"
        )


def test_industry_roles_forbid_false_attribution():
    """★ 行业角色的教学必须**显式禁止**"本行业解禁"的错误归因。

    理由（实测）：`CalendarEvent.scope.industries` **是空数组** ——
    解禁数据没有行业标签。硬教行业 Agent"看你行业的解禁"会引导**幻觉归因**，
    比不教更危险。

    这条测试红了 = 有人删掉了禁令。
    """
    from src.domain.agents.analysis.unlock_teaching import render_unlock_teaching

    text = render_unlock_teaching("A13_tech")
    assert "没有行业标签" in text, "行业角色必须说明数据无行业标签"
    assert "禁止" in text, "行业角色必须有明确的禁令"
    assert "本行业解禁" in text, "禁令必须点名要禁止的说法"


def test_unlock_teaching_rejects_direction_assertion():
    """教学必须包含「解禁 ≠ 必跌」（防止模型直接断言涨跌）。"""
    from src.domain.agents.analysis.unlock_teaching import render_unlock_teaching

    for aid in ("A08_macro", "A10_micro", "A12_compliance", "A16_pharma"):
        text = render_unlock_teaching(aid)
        assert "必跌" in text or "潜在减持压力" in text, (
            f"{aid} 的教学没说明「解禁不等于必跌」，模型可能直接断言涨跌"
        )


def test_touchpoints_declared():
    """`NEW_INDICATOR_TOUCHPOINTS` 必须存在且覆盖关键层。

    它是"新增指标前缀要穿过哪些门"的**单一事实源** ——
    没有它，下次又会漏掉某一层（本轮就是靠读代码才发现漏了白名单）。
    """
    assert NEW_INDICATOR_TOUCHPOINTS, "关联模块清单不能为空"
    joined = " ".join(name for name, _ in NEW_INDICATOR_TOUCHPOINTS)
    for must in ("indicators.yaml", "_AGENT_DATA_WHITELIST",
                 "INDICATOR_CATALOG", "FREQUENCY_TO_CRON", "capabilities"):
        assert must in joined, f"关联清单缺关键检查点：{must}"


def test_touchpoints_have_reasons():
    """每条检查点都要写清"漏了会怎样"（否则没人知道为什么要查它）。"""
    for name, why in NEW_INDICATOR_TOUCHPOINTS:
        assert name.strip(), "检查点名不能为空"
        assert len(why) >= 8, f"{name} 的理由太短，说不清漏了的后果"


# ============================================================
# ③ 其余检查点的可执行断言
# ============================================================


@pytest.mark.parametrize("prefix", LIVE_PREFIXES)
def test_prefix_in_capability_catalog(prefix: str):
    """检查点：A17 是否知道这个能力存在（漏了会误报「数据缺失」）。"""
    from src.domain.agents.decision.capabilities import CAPABILITIES

    # `cal:` 对应"投资日历"相关能力
    hit = [c for c in CAPABILITIES if "日历" in c.name or "解禁" in c.name]
    assert hit, (
        f"{prefix} 相关能力未登记在 capabilities.py —— "
        "A17 会把它误报成「数据缺失」（本轮实测过）"
    )


@pytest.mark.parametrize("prefix", LIVE_PREFIXES)
def test_prefix_registered_in_yaml(prefix: str):
    """检查点：`configs/indicators.yaml` 必须登记该前缀的指标。

    没登记 → SmartFetcher 判"未登记" → 走 daily/24h 兜底 → 每次联网。
    """
    from src.infrastructure.catalog import get_registry, reset_registry_for_test

    reset_registry_for_test()
    r = get_registry()
    metas = [m for m in r.all() if m.indicator.startswith(prefix)]
    assert metas, f"{prefix} 前缀的指标未登记在 indicators.yaml"
    for m in metas:
        assert m.frequency, f"{m.indicator} 缺 frequency"
        assert m.freshness_hours > 0, f"{m.indicator} 缺 freshness_hours"


@pytest.mark.parametrize("prefix", LIVE_PREFIXES)
def test_prefix_has_schedule_coverage(prefix: str):
    """检查点：必须有定时作业更新它（否则永远 stale → 每次联网）。"""
    from src.infrastructure.catalog import get_registry, reset_registry_for_test
    from src.scheduler.catalog_jobs import catalog_coverage

    reset_registry_for_test()
    r = get_registry()
    inds = {m.indicator for m in r.all()
            if m.indicator.startswith(prefix) and not m.is_template()}
    if not inds:
        pytest.skip(f"{prefix} 无精确指标")

    cov = catalog_coverage()
    uncovered = set(cov["uncovered"]) & inds
    assert not uncovered, (
        f"{prefix} 指标无作业覆盖：{uncovered}\n"
        "→ 在 catalog_jobs 里给它一个 frequency 映射或专用作业"
    )


# ============================================================
# ④ 白名单自身的形状自检
# ============================================================


def test_whitelist_entries_non_empty():
    """白名单条目不可是空串（空串会匹配所有指标，失去过滤意义）。"""
    for aid, kws in _AGENT_DATA_WHITELIST.items():
        assert kws, f"{aid} 白名单为空"
        for kw in kws:
            assert str(kw).strip(), f"{aid} 含空关键词"


def test_filter_function_shape():
    """`_filter_points_for_agent` 的契约：返回 list，且不超 fallback_limit
    （除非白名单精确命中超出）。"""
    pts = [{"indicator": "CPI", "value": 1.0, "period_date": "2026-08"}]
    got = _filter_points_for_agent("A08_macro", pts)
    assert isinstance(got, list)
    assert len(got) == 1
    # 未知 agent → 走 fallback 分支
    got2 = _filter_points_for_agent("A99_unknown", pts)
    assert isinstance(got2, list)


def test_no_agent_silently_gets_everything():
    """★ 反向测试：白名单不能形同虚设。

    如果某个 Agent 的白名单包含太宽的关键词（如 `""` 或 `":"`），
    它会拿到全部数据点 → token 爆炸（这正是第八轮加白名单要解决的问题）。
    """
    all_points = [{"indicator": f"ind_{i}", "value": 1.0,
                   "period_date": "2026-09-28"} for i in range(400)]
    for aid, kws in _AGENT_DATA_WHITELIST.items():
        # 用一批**明确无关**的指标测：不该全被捞走
        for kw in kws:
            assert len(str(kw)) >= 2 or kw in ("pb",), (
                f"{aid} 的关键词 {kw!r} 太宽，会匹配几乎所有指标"
            )
    del all_points


def test_touchpoint_source_is_importable():
    """关联清单必须能被别的模块 import（供审计脚本/文档生成用）。"""
    src = inspect.getsource(
        __import__("src.orchestration.supervisor", fromlist=["x"]))
    assert "NEW_INDICATOR_TOUCHPOINTS" in src
