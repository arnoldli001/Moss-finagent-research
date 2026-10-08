"""「问句点名多只个股」时采集**不许漏股票**（`CHG-0216`）。

## 用户报障原话

> 投研分析中，用户输入含有2个及以上的个股或2个及以上的概念板块，
> 且在 **标的** 中输入了1个股票代码，此时采集数据会存在**漏掉一些股票或板块**
> 的信息获取。比如：**当前宏观环境如何，预测下未来一年美国的加息预期下，
> 基于当前板块拥挤度和能源重点项目与新业态投资20万亿的政策，未来半年能否持有
> 高股息的宁波银行和中国神华？** **标的 601088**
> —— 反馈中国神华因缺个股估值与股息数据、宁波银行没有任何可引用的估值，
> 而这个在本地数据库中明显有数据。

## 根因是**三段叠加**（都用用户原句复现过，见 `_probe_multi_stock_gap.py`）

    ① `_PLANNING["full"] = (["CPI","PPI"], …)` —— **一个个股指标都没有**
    ② `resolve_analysis_subject` 的「冲突改判」把输入标的**整个换成**问句里那只
       （`resolve_stock` 只返回**一个**）⇒ 用户填的 601088 **被静默丢弃**
    ③ `augment_plan_by_query_signals` 里 `code` 是**单值** ⇒ 个股指标只能挂一只，
       **另一只必然一个指标都没有** —— 症状就是"该股没有估值/股息数据"

本轮修 ②③（①靠 ③ 的增补补回来）；判据钉的就是"**每一只都排**"。

跑法：
    uv run python -m pytest tests/unit/test_multi_stock_collection.py -q
"""
from __future__ import annotations

import pytest

from src.api.routes import research as research_mod
from src.infrastructure.connectors import security_resolver as sr
from src.orchestration import supervisor as sup

QUERY = ("当前宏观环境如何，预测下未来一年美国的加息预期下，基于当前板块拥挤度和"
         "能源重点项目与新业态投资20万亿的政策，未来半年能否持有高股息的"
         "宁波银行和中国神华？")
TARGET = "601088"

#: 替身名称表：**不打网络**，且名字/代码都是真实口径。
#:
#: ⚠️ **顺序有语义**：`resolve_stock_sync` 的包含匹配用 `len(n) > len(best[1])`
#: （**严格大于**）⇒ **等长**名称取**表里先出现的**那个。用户原句里
#: 「宁波银行」与「中国神华」都是 4 字 ⇒ 把宁波银行放前面才复现真实表的行为
#: （实测真实表 `resolve_stock(QUERY)` 返回宁波银行），**也才会触发冲突改判**。
#: 第一版把中国神华放在前面 ⇒ 解析出的是输入标的本身 ⇒ **没触发冲突** ⇒
#: 新分支根本没被走到，而断言失败信息完全指不到这一点。
_PAIRS = (("002142", "宁波银行"), ("601088", "中国神华"),
          ("601633", "长城汽车"), ("600519", "贵州茅台"))


@pytest.fixture
def fake_names(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sr, "_name_pairs", lambda: list(_PAIRS))
    yield


#: ⚠️ `resolve_analysis_subject` 里是 `await resolve_stock(...)` / `await resolve_stocks(...)`
#: ⇒ 替身**必须是 async**。第一版直接 patch 了 `*_sync`，于是 `await list` 抛 TypeError、
#: 被函数里的 `except Exception`（"名称解析失败不阻断"）吞掉 ⇒ 走了提前返回，
#: 断言看到的是一堆默认值 —— **而失败信息完全指不到真因**。
async def _fake_resolve_stock(text: str):
    return sr.resolve_stock_sync(text)


async def _fake_resolve_stocks(text: str):
    return sr.resolve_stocks_sync(text)


# ===========================================================================
# ① 解析层：问句里能解出**全部**个股（原来只能解出一个）
# ===========================================================================


def test_resolve_stocks_finds_all_in_mention_order(fake_names) -> None:
    """★★ 用户原句要解出**两只**，且按**出现顺序**（宁波银行在前）。"""
    got = sr.resolve_stocks_sync(QUERY)
    assert got == [("002142", "宁波银行"), ("601088", "中国神华")], got


def test_resolve_stocks_handles_multiple_bare_codes(fake_names) -> None:
    assert sr.resolve_stocks_sync("对比 601088 和 002142") == [
        ("601088", "中国神华"), ("002142", "宁波银行")]


def test_resolve_stocks_is_longest_match_not_substring(fake_names) -> None:
    """★ **最长优先且不重叠**：`长城汽车` 不许被再拆出一个 `长城`。

    子串误命中是这类匹配的经典缺陷 —— 它会凭空多出一只不存在的股票。
    """
    assert sr.resolve_stocks_sync("长城汽车怎么样") == [("601633", "长城汽车")]


def test_resolve_stocks_empty_and_unknown(fake_names) -> None:
    assert sr.resolve_stocks_sync("") == []
    assert sr.resolve_stocks_sync("今天天气怎么样") == []


def test_resolve_stocks_dedupes(fake_names) -> None:
    """同一只被提两次只算一次（否则个股指标会重复排两遍）。"""
    assert sr.resolve_stocks_sync("中国神华和中国神华哪个好") == [
        ("601088", "中国神华")]


# ===========================================================================
# ② 标的层：问句点名 ≥2 只时**不许丢掉用户填的标的**
# ===========================================================================


@pytest.mark.asyncio
async def test_two_named_stocks_keep_the_input_target(fake_names,
                                                      monkeypatch) -> None:
    """★★ **输入标的 601088 必须还在**，且两只都进采集覆盖面。

    修复前：`target` 被改成 `002142`、`focus_stock_code` 也是 `002142`
    —— 用户明确填的 **601088 既没进 target 也没进 focus，被静默丢弃**。
    """
    monkeypatch.setattr(research_mod, "resolve_stock", _fake_resolve_stock)
    monkeypatch.setattr(research_mod, "resolve_stocks", _fake_resolve_stocks)

    s = await research_mod.resolve_analysis_subject(TARGET, QUERY, "full")
    assert s.target == TARGET, f"输入标的被改判掉了：{s.target}"
    assert s.focus_stock_code == TARGET
    assert set(s.focus_stock_codes) == {"601088", "002142"}, s.focus_stock_codes
    assert s.focus_stock_codes[0] == TARGET, "主焦点必须排在第一位（恒被采到）"
    assert "全部纳入采集" in s.note, s.note


@pytest.mark.asyncio
async def test_one_named_stock_still_switches(fake_names, monkeypatch) -> None:
    """★ **回归护栏**：问句只点名**一只**时，既有的「冲突改判」行为**逐字不变**。

    那条行为有实测依据（2026-09-29）：输入框残留代码会让整条链路**答错标的**，
    ——"不报错、答错标的"比"缺数据"更危险。不许为了修多只而把它一起改掉。
    """
    monkeypatch.setattr(research_mod, "resolve_stock", _fake_resolve_stock)
    monkeypatch.setattr(research_mod, "resolve_stocks", _fake_resolve_stocks)

    s = await research_mod.resolve_analysis_subject(
        TARGET, "未来半年能否持有高股息的宁波银行？", "full")
    assert s.target == "002142", "单只点名的改判行为被改掉了"
    assert s.focus_stock_code == "002142"
    assert "已按问句分析" in s.note
    assert s.focus_stock_codes == ("002142",)


# ===========================================================================
# ③ 规划层：**每一只**都要排到个股指标（用户报障的核心）
# ===========================================================================


def _per_stock(indicators: list[str]) -> dict[str, set[str]]:
    """把带个股代码的指标按代码分组（`PE(TTM):601088` → `601088`）。"""
    out: dict[str, set[str]] = {}
    for ind in indicators:
        head, _, tail = ind.partition(":")
        if tail.isdigit():
            out.setdefault(tail, set()).add(head)
    return out


def _augment(monkeypatch, codes: tuple[str, ...], *,
             single: str = "") -> list[str]:
    """只保留 `stock` 信号，隔离掉"信号检测"这一层（那是另一个判据的事）。"""
    monkeypatch.setattr(sup, "_query_agent_signals", lambda _t, _g: {"stock"})
    _a, inds, _n = sup.augment_plan_by_query_signals(
        QUERY, TARGET, [], [], resolved_code=single, resolved_codes=codes,
        analysis_type="full")
    return inds


def test_every_named_stock_gets_its_own_indicators(monkeypatch) -> None:
    """★★★ **本文件最重要的一条**：两只都必须拿到估值/股息/财务指标。

    修复前 `code` 是单值 ⇒ 无论传哪一只，**另一只一个指标都没有**
    ⇒ 用户看到"该股没有估值/股息数据"，而本地库里明明有。
    """
    inds = _augment(monkeypatch, ("601088", "002142"))
    by = _per_stock(inds)
    assert set(by) == {"601088", "002142"}, f"只排到了 {set(by)}"
    for code in ("601088", "002142"):
        assert "PE(TTM)" in by[code], f"{code} 没有估值"
        assert "PB" in by[code], f"{code} 没有 PB"
        assert "股息率TTM" in by[code], f"{code} 没有股息（用户问的就是高股息）"
        assert "ROE" in by[code], f"{code} 没有 ROE"


def test_single_code_fallback_is_unchanged(monkeypatch) -> None:
    """★ 只传 `resolved_code`（既有调用点）⇒ 行为与改动前一致：**只有一个代码**。"""
    inds = _augment(monkeypatch, (), single="601088")
    assert set(_per_stock(inds)) == {"601088"}


def test_no_code_still_drops_bare_stock_indicators(monkeypatch) -> None:
    """★ **回归护栏**：一个代码都没有时，裸个股指标**仍然被摘掉**。

    理由（原注释，2026-09-26 实测故障链）：裸名 `PE(TTM)` 没有任何连接器
    supports，A01 会白撞一次网络后必然失败，用户看到"估值数据缺失"
    —— **看起来像数据源坏了，其实是契约不满足**。
    """
    inds = _augment(monkeypatch, ())
    assert _per_stock(inds) == {}, f"没有代码却排了裸个股指标：{inds}"


def test_duplicate_codes_do_not_duplicate_indicators(monkeypatch) -> None:
    """同一只重复传 ⇒ 指标不重复排（去重是采集层的契约）。"""
    inds = _augment(monkeypatch, ("601088", "601088"))
    assert len(inds) == len(set(inds)), inds


def test_non_stock_signals_are_not_multiplied(monkeypatch) -> None:
    """★ 只有 `stock` 信号按股票各排一份；其它信号**逐字保持**单次行为。"""
    monkeypatch.setattr(sup, "_query_agent_signals",
                        lambda _t, _g: {"dividend"})
    _a, inds, _n = sup.augment_plan_by_query_signals(
        "问高股息", "601088", [], [], resolved_codes=("601088", "002142"),
        analysis_type="full")
    # dividend 信号补的是行业截面（`ind:...:all`），不该被乘成两份
    assert len(inds) == len(set(inds)), inds
    assert not [i for i in inds if i.split(":")[-1].isdigit()], (
        f"非个股信号不该产出带个股代码的指标：{inds}")


# ===========================================================================
# ④ 板块侧（`CHG-0217`）—— 与个股侧是**同一个缺陷**的另一半
#
# 用户原话：「…含有2个及以上的个股**或2个及以上的概念板块**…
#            此时采集数据会存在**漏掉一些股票或板块**的信息获取」
# ===========================================================================

#: 带**标的**前缀的真实形状（规划层就是这么拼的：`f"{target} {text}"`）。
BOARD_TEXT = f"{TARGET} {QUERY}"


def test_multi_industry_resolution_is_a_union_not_first_path_wins() -> None:
    """★★ 多值入口必须是**并集**，不是"确定性优先"。

    第一版照搬了单值版的优先级，**实测被它挡掉**：路径①从 `601088` 解出
    `煤炭开采` 就返回 ⇒ **永远走不到路径②**（那里才能从「宁波银行」解出`银行`）
    ⇒ 两个板块只排一个，正是要修的 bug。

    ⇒ 两个入口回答**两个不同的问题**：单值版是**归属判定**（必须唯一），
    多值版是**采集覆盖**（多取一个只是多查一次，漏一个就是"该板块没有数据"）。
    """
    from src.infrastructure.catalog.industry_of import (
        resolve_industries_from_text,
        resolve_industry_from_text,
    )

    many = resolve_industries_from_text(BOARD_TEXT)
    names = [n for n, _how in many]
    assert names[:2] == ["煤炭开采", "银行"], many
    # ★ 回归护栏：单值版**逐字不变**（仍只返回一个，仍确定性优先）
    single = resolve_industry_from_text(BOARD_TEXT)
    assert single[0] == "煤炭开采", single


def test_multi_industry_dedupes_by_name() -> None:
    """按**行业名**去重（两个不同代码可能同属一个行业 ⇒ 不该排两遍）。"""
    from src.infrastructure.catalog.industry_of import (
        resolve_industries_from_text,
    )

    got = resolve_industries_from_text("601088 和 601225 都是煤炭")
    names = [n for n, _how in got]
    assert names.count("煤炭开采") == 1, got


def test_no_industry_still_returns_empty() -> None:
    """★ 回归护栏：三路都不中 ⇒ `[]`（**绝不猜行业**，与单值版同一条纪律）。"""
    from src.infrastructure.catalog.industry_of import (
        resolve_industries_from_text,
    )

    assert resolve_industries_from_text("今天天气怎么样") == []
    assert resolve_industries_from_text("") == []


def test_every_industry_gets_its_own_board_indicators(monkeypatch) -> None:
    """★★★ **板块侧的核心判据**：两个板块都要拿到三族板块指标。

    修复前单值 ⇒ 只给一个补 `行业拥挤度`/`板块资金流`/`行业轮动`，
    **另一个板块一个指标都没有**，用户看到的是"该板块没有数据"。
    """
    monkeypatch.setattr(sup, "_query_agent_signals", lambda _t, _g: set())
    monkeypatch.setattr(sup, "_industry_crowding_available",
                        lambda _name: True)
    _a, inds, _n = sup.augment_plan_by_query_signals(
        BOARD_TEXT, TARGET, [], [], analysis_type="full")

    by_kind: dict[str, set[str]] = {}
    for ind in inds:
        head, _, tail = ind.partition(":")
        if head in ("行业拥挤度", "板块资金流", "行业轮动"):
            by_kind.setdefault(head, set()).add(tail)
    for head in ("行业拥挤度", "板块资金流", "行业轮动"):
        assert by_kind.get(head) == {"煤炭开采", "银行"}, (head, by_kind)


def test_single_industry_output_is_unchanged(monkeypatch) -> None:
    """★ 回归护栏：**只命中一个行业**时，产出的指标与提示**逐字不变**。"""
    monkeypatch.setattr(sup, "_query_agent_signals", lambda _t, _g: set())
    monkeypatch.setattr(sup, "_industry_crowding_available",
                        lambda _name: True)
    _a, inds, notes = sup.augment_plan_by_query_signals(
        "白酒板块现在怎么样", "", [], [], analysis_type="full")
    assert "行业拥挤度:白酒" in inds and "板块资金流:白酒" in inds
    assert not [i for i in inds if i.endswith(":银行")], inds
    assert any("问句命中行业「白酒」" in n for n in notes), notes


def test_news_pipeline_collects_no_board_indicators(monkeypatch) -> None:
    """★ 回归护栏：**`news` 管线不采数据** —— 一个板块指标都不许补。

    实测教训（原注释）：不挡这一下，一条 news 问句（`user_query` 里恰好带行业名）
    会被补上 `行业拥挤度:白酒` ⇒ `_planned_indicators` 非空 ⇒ 采集节点把 A01
    拉起来跑 ⇒ 集成测试 `test_news_graph_info_pipeline` 直接红。
    """
    monkeypatch.setattr(sup, "_query_agent_signals", lambda _t, _g: set())
    _a, inds, _n = sup.augment_plan_by_query_signals(
        BOARD_TEXT, TARGET, [], [], analysis_type="news")
    assert not [i for i in inds
                if i.split(":")[0] in ("行业拥挤度", "板块资金流", "行业轮动")], inds
