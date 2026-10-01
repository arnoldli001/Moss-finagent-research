"""白名单的**语义扩张**判据（`CHG-0136`）。

## 报障现场（用户 2026-09-30）

> 「为什么还是反馈股息率数据缺失？招商银行的股息率肯定在数据库里能找到的啊」

实测：`股息率TTM:600036` **采集成功**（60 点，最新 2026-09-29，本地 `quant_daily_basic`），
`PB:600036`、`PE(TTM):600036` 也都采到了。但：

| 指标 | 修前谁能看见 | 原因 |
|---|---|---|
| `PB:600036` | **没有 Agent** | A11 白名单写中文「市净率」，id 是英文 `PB` ⇒ 子串匹配不上 |
| `PE(TTM):600036` | 只有 A13_tech | 同上（「市盈率」⊄ `PE(TTM)`） |
| `ind:sw_first_dividend_yield:银行` | A09（A11 看不到） | A11 白名单没有英文 `dividend_yield` |

⇒ **采了白采**。根因不是缺同义词池（`catalog/synonym_dict.py` 早就有 154 条指标别名），
而是**匹配没用它**。

## 本文件守什么

① 中文概念词必须能命中英文 id（真报障那三条）；
② **反向**：不许把无关指标也吞进来（`'市盈率'` 会扩张出 `pe`，而
   `ind:penetration:…` 里含 `pe` —— 没有词边界守卫就会白花 token）；
③ 扩张确实**来自同义词池**（不是又抄了一份映射）；
④ 取数侧确实登记了"按行业点名"的形式（否则规划侧看不见）。
"""
from __future__ import annotations

import pytest


def _pts(*indicators: str) -> list[dict]:
    return [{"indicator": i, "value": 1.0, "period_date": "2026-09-30"} for i in indicators]


# ─────────────── ① 真报障：中文概念词必须命中英文 id ───────────────


@pytest.mark.parametrize("indicator", [
    "PB:600036",                        # 报障里最硬的一条：修前"无人可见"
    "PE(TTM):600036",
    "股息率TTM:600036",
    "ind:sw_first_dividend_yield:银行",  # 行业股息率（申万一级）
])
def test_a11_now_sees_these(indicator: str) -> None:
    """A11 金融风险必须看得见这些 —— 修前它们要么无人可见、要么被挡。"""
    from src.orchestration.supervisor import _filter_points_for_agent

    got = _filter_points_for_agent("A11_fin_risk", _pts(indicator))
    assert [p["indicator"] for p in got] == [indicator], (
        f"{indicator} 被白名单挡掉了 ⇒ 采了白采（这正是用户报障的形状）"
    )


def test_expansion_comes_from_the_shared_dictionary() -> None:
    """★ 扩张必须**来自** `synonym_dict.metric_aliases()`，不是又抄一份映射。

    判据：直接查池子，`市净率/市盈率/股息率` 的等价词里必须分别含 `pb/pe/`英文股息词。
    池子被清空或改名时这条会红 —— 那时该修的是接线，不是删判据。
    """
    from src.infrastructure.catalog.synonym_dict import metric_aliases

    ma = {str(k).lower(): {str(a).lower() for a in v} for k, v in metric_aliases().items()}
    assert "pb" in ma.get("市净率", set()), "同义词池里『市净率』没有英文等价词"
    assert "pe" in ma.get("市盈率", set()) or "pe_ttm" in ma.get("市盈率", set())
    assert {"dividend_yield", "dv_ratio", "dv_ttm"} & ma.get("股息率", set())


# ─────────────── ② 反向：不许扩张过宽 ───────────────


def test_expansion_does_not_swallow_irrelevant_indicators() -> None:
    """`'市盈率'` 扩张出 `pe`，而 `ind:penetration:…` 含 `pe` —— 必须挡住。

    这是**由这次改动新引入的风险**（改之前 A11 根本匹配不上英文，所以不存在
    过宽问题）。没有这条，"把白名单放松一点"会让每个 Agent 都吞进无关数据点，
    token 成本悄悄上去而没人发现。
    """
    from src.orchestration.supervisor import _filter_points_for_agent

    noise = ["ind:penetration:AI大模型应用", "ind:penetration:新能源汽车",
             "mkt:cybkcb:turnover:all", "ind:白酒批价(元/瓶)"]
    got = _filter_points_for_agent("A11_fin_risk", _pts(*noise))
    leaked = [p["indicator"] for p in got]
    # 允许"白名单剩 0 条时的兜底"（会整批返回），所以只在**部分命中**时判过宽
    assert leaked == [] or leaked == noise, (
        f"出现了部分命中 —— 说明扩张把无关指标也吞了：{leaked}"
    )


def test_ascii_alias_uses_word_boundary() -> None:
    """词边界守卫的直接判据（底层函数级，避免被"兜底返回"掩盖）。"""
    from src.orchestration.supervisor import _kw_hit

    assert _kw_hit("pb:600036", "pb") is True
    assert _kw_hit("pe(ttm):600036", "pe") is True
    assert _kw_hit("ind:penetration:ai大模型应用", "pe") is False, (
        "`pe` 命中了 `penetration` ⇒ 没有词边界守卫"
    )
    # 中文别名保持子串语义（指标名常带后缀）
    assert _kw_hit("股息率ttm:600036", "股息率") is True


def test_prefix_keywords_still_match() -> None:
    """★ **第一版边界规则打破过这里**（既有三条回归判据当场变红）。

    白名单里大量是**前缀式**关键词（`fed:` `fred:` `cal:` `ind:` `mkt:` `sw_`
    `idx_val:`），它们**以分隔符结尾**。若无条件要求"匹配位置后面不是字母数字"，
    `fed:` 在 `fed:policy_range` 里就会因为后面跟着 `p` 而**判不命中**
    ⇒ `fed:policy_range` 到不了 A08（用户报障过的那条）。

    所以边界**只在关键词该端本身是字母数字时**才检查 —— 这条判据把它钉住。
    """
    from src.orchestration.supervisor import _kw_hit

    for ind, kw in [
        ("fed:policy_range", "fed:"),
        ("fred:UNRATE", "fred:"),
        ("cal:解禁", "cal:"),
        ("ind:sw_third_pe_ttm:all", "ind:"),
        ("mkt:turnover:total", "mkt:"),
        ("ind:sw_first_dividend_yield:银行", "sw_"),
        ("idx_val:pe_ttm:沪深300", "idx_val:"),
    ]:
        assert _kw_hit(ind.lower(), kw) is True, (
            f"前缀关键词 {kw!r} 匹配不上 {ind!r} —— 边界规则又把前缀式关键词挡掉了"
        )


# ─────────────── ③ 取数侧登记了"按行业点名" ───────────────


def test_connector_advertises_per_industry_forms() -> None:
    """★ 「截面没行业标签」的修法：把一级/二级的 `{行业名}` 形式登记出来。

    实测依据：`ind:sw_first_dividend_yield:银行` **真取得到**（2026-09-30 银行
    股息率 **5.1%**、PE-TTM 7.34），但修前 `get_capabilities()` 只登记了**三级**的
    `{行业名}` 形式 ⇒ 规划侧看不见一级行业的按行业路径 ⇒ 只采 `:all` 无标签截面
    ⇒ Agent 无法把"银行"挑出来 ⇒ 报告写"股息率缺失"。
    """
    from src.infrastructure.connectors.sw_industry_valuation_connector import (
        SWIndustryValuationConnector as C,
    )

    caps = C().get_capabilities()
    inds = set(caps.get("indicators") or [])
    for must in ("ind:sw_first_dividend_yield:{行业名}",
                 "ind:sw_first_pe_ttm:{行业名}",
                 "ind:sw_second_dividend_yield:{行业名}",
                 "ind:sw_third_dividend_yield:{行业名}",
                 "ind:sw_first_dividend_yield:all"):
        assert must in inds, f"能力表缺 {must} ⇒ 规划侧看不见这条取数路径"
    assert "不带行业标签" in str(caps.get("notes")), (
        "notes 必须点明 `:all` 截面**不带行业标签** —— 否则下一个人还会踩"
    )
    # 能力表里登记的每一条，supports() 都必须认（否则登记是假的）
    for i in inds:
        assert C.supports(i.replace("{行业名}", "银行")), f"登记了却 supports()=False: {i}"
