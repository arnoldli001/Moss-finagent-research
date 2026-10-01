"""护栏：A17 的**假缺口**必须被拦下，但"如实登记的不可得"必须放行。

## 实测现场（2026-09-28）

某轮 A17 的结论一边引用「目标区间 3.75%~4.00%、有效利率 3.88%」
（数据就在同一份 prompt 里），一边写：
    「CME FedWatch利率概率（当前环境无法访问CME/FRED）」

后果两条：
  ① 结论自相矛盾，用户读到"用着 FRED 的数据说 FRED 访问不了"；
  ② 这条假缺口**入了队**（纯字符串无 status → 保守入队）→
     A19 盘后为它做一次**注定失败**的补取。

## 本文件守什么

· 假缺口（可达源 + 可达性归因 + 未标 unavailable）→ **拦住不入队**
· 如实登记的不可得（`status="unavailable"`）→ **放行**
  （拦它就变成"为了让台账好看而掩盖真实缺口"，方向相反）
· 真缺口（没提任何已知可达源）→ **放行**
"""

from __future__ import annotations

from src.orchestration.supervisor import (  # noqa: E402
    _FALSE_GAP_SOURCE_NAMES,
    _is_false_gap,
)


# ============================================================
# ① 必须拦下的假缺口（报障原文逐字）
# ============================================================

def test_reported_false_gap_is_blocked():
    """★ 报障原文那一句必须被拦住。"""
    item = "CME FedWatch利率概率（当前环境无法访问CME/FRED，仅能方向性判断）"
    assert _is_false_gap(item, ""), (
        "这条假缺口没被拦住 → 结论自相矛盾 + A19 白跑一次补取"
    )


def test_other_wording_variants_are_blocked():
    """同一件事的不同说法都要拦（文案会变，判据不能只认一句话）。"""
    variants = [
        "美国联邦基金利率（FRED 不可达）",
        "目标区间数据无法获取（FRED 访问受限）",
        "有效利率 fed:effr 访问不了",
        "联邦基金利率不可访问",
    ]
    for v in variants:
        assert _is_false_gap(v, ""), f"漏拦：{v}"


# ============================================================
# ② 必须放行的（不许拦过头）
# ============================================================

def test_unavailable_status_always_passes():
    """★ 标了 `unavailable` 的**一律放行** —— 那是如实登记，不是假缺口。

    拦它的方向是错的：等于"为了让台账好看而掩盖真实缺口"。
    """
    item = "CME FedWatch利率概率（当前环境无法访问CME/FRED）"
    assert not _is_false_gap(item, "unavailable")
    assert not _is_false_gap(item, "source_terminated")


def test_real_gaps_pass_through():
    """真缺口必须放行（没有点名任何已知可达源）。"""
    real = [
        "招商银行个股财务明细、股息率、净息差与资产质量数据",
        "北向资金日度净买额（2024-08-19起交易所停止披露）",
        "银行及高股息行业分项估值分位",
        "三市分项成交额未提供",
    ]
    for r in real:
        assert not _is_false_gap(r, ""), f"误拦了真缺口：{r}"


def test_reachability_attribution_is_required():
    """★ 只点名源、**没有**可达性归因的，不算假缺口（避免误伤）。"""
    # 说"FRED 的数据没进本次分析"是**纳入问题**（available_not_included），
    # 不是"FRED 不可访问" —— 两者处置完全不同
    assert not _is_false_gap("FRED 的联邦基金利率历史序列未纳入本次分析", "")
    assert not _is_false_gap("fed:target_upper 未提供", "")


# ============================================================
# ③ 判据本身不许腐烂
# ============================================================

def test_every_listed_source_is_really_available():
    """★★ 拦人的前提：清单里的源**现在真的可达**。

    没有这条，`_FALSE_GAP_SOURCE_NAMES` 会变成一份"当初可达、后来挂了"
    的化石 —— 那时我们就在**阻止系统报告真实缺口**（比假缺口更糟）。
    """
    # FRED 四件套必须全在清单里（它们是 2026-09-28 实测可达的那一族）
    for must in ("fed:effr", "fed:target_upper", "fed:target_lower", "FRED"):
        assert must in _FALSE_GAP_SOURCE_NAMES, f"清单缺 {must}"
    # 而**环境性不可达**的源绝不能进清单（进了就会把真缺口拦掉）
    assert "CME" not in _FALSE_GAP_SOURCE_NAMES
    assert "cmegroup" not in " ".join(_FALSE_GAP_SOURCE_NAMES).lower()


def test_capability_block_forbids_the_false_gap_wording():
    """★ 提示词侧也要禁止这种表述（双层防线：提示词 + 入口拦截）。"""
    from src.domain.agents.decision.capabilities import render_capability_block

    block = render_capability_block(compact=False)
    assert "无法访问 CME/FRED" in block, (
        "capabilities 里必须**逐字点出**这个错误表述，否则模型会继续这么写"
    )
    assert "unavailable" in block, (
        "必须告诉 A17：报这类缺口要带 status=unavailable"
    )
