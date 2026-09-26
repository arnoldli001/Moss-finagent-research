"""券商名**不一定带"证券"两个字** —— 署名形态（简称 + 研究语境后缀）。

> 用户口径（2026-10-01）：
>
>   "券商名字不一定带券商两个字，比如可能是天风电子，招商电子，
>    所以可以搜索国内大型研究机构的名字、研报喜欢用的名字，加进去。"

## 旧形态结构性看不到这些名字

`BROKER_PATTERN = [\\u4e00-\\u9fff]{2,8}证券` 只认"XX证券"，而研报署名最常见的
形态是**机构简称 + 行业组名**：`【天风电子】`、`【招商电子】`、`【华福电新】`
—— 一个"证券"字都没有。表现是"这条没有机构名"，而原文里明明写着。

## 为什么是 `(简称)(后缀)` 而不是一张裸简称清单

裸简称**歧义极重**：招商 / 中信 / 东方 / 光大 / 民生 / 兴业 / 平安 / 长城
同时是银行、上市公司与普通词（招商银行、东方财富、平安保险、长城汽车…）。
所以两个条件必须**同时**成立：简称在名单里 + 紧跟着一个研究语境后缀。

## 两道闸门的分工（本文件逐个钉住）

    后缀名单    `银行` / `基金` **不收** —— 它们是上市公司与资管机构，
                不是"发布研报的研究所"（"机构：招商银行"是明显错的标签）
    上市公司闸门 行业组后缀（`汽车`/`通信`…）要问 A 股名录：
                `天风电子` 不是上市公司 → 认；`长城汽车` 是 → 不认

精度优先于召回：用户明确说过漏掉可以接受（"不可能完全识别完的，漏掉就漏掉吧"），
而误报是"弹窗/展示不可信"那一类损失。
"""

from __future__ import annotations

import pytest

from src.domain.intel import alert_bridge as B
from src.domain.intel import alert_rules as R
from src.domain.intel import vocab
from src.infrastructure.connectors.intel_sources import IntelItem

#: 受控词表：**只放"看起来像机构简称 + 行业组"的上市公司**。
#: 有了它，`长城汽车` / `东方通信` 那道闸门才是被真正验证的
#: （而不是"恰好词表为空所以蒙对了"）。
_FAKE_ENTRIES = [
    vocab.VocabEntry("长城汽车", vocab.KIND_STOCK, code="601633"),
    vocab.VocabEntry("东方通信", vocab.KIND_STOCK, code="600776"),
    vocab.VocabEntry("招商银行", vocab.KIND_STOCK, code="600036"),
    vocab.VocabEntry("东方财富", vocab.KIND_STOCK, code="300059"),
    # "简称 + 行业组"里**真正危险的**一类：机构名与上市公司名逐字相同
    # （长江电力 600900 / 国投电力 600886）—— 只靠词形分不开，只能问名录
    vocab.VocabEntry("长江电力", vocab.KIND_STOCK, code="600900"),
    vocab.VocabEntry("国投电力", vocab.KIND_STOCK, code="600886"),
    # 中信证券**是**上市公司，也正是要认的机构（`证券` 后缀那一路不走闸门）
    vocab.VocabEntry("中信证券", vocab.KIND_STOCK, code="600030"),
]


@pytest.fixture(autouse=True)
def _fake_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vocab, "_TABLE", vocab.build_table(_FAKE_ENTRIES))


def _item(*, title: str = "", summary: str = "") -> dict:
    """一条最小情报（形状与 `IntelItem.to_public()` 一致）。"""
    return {
        "kind": "research_note",
        "title": title,
        "summary": summary,
        "credibility": {"score": 58},
        "tone": {"tone": "未定", "has_tone": False, "neutral": False},
    }


def _names(text: str) -> list[str]:
    return [h.name for h in R.content_hits(_item(summary=text))]


# ======================================================================
# ① 署名形态：召回到了
# ======================================================================

@pytest.mark.parametrize("text,expected", [
    # 用户点名的两个例子
    ("【天风电子】半导体设备板块景气上行", "天风电子"),
    ("【招商电子】消费电子需求回暖", "招商电子"),
    # 参考项目实测清单里的其余几个（同一种形态）
    ("【华福电新】风电装机超预期", "华福电新"),
    ("【中泰汽车】销量数据点评", "中泰汽车"),
    ("【招商机械】工程机械出口强劲", "招商机械"),
    ("【国金AI金属】有色金属周报", "国金AI金属"),
    ("【东北商业航天】卫星互联网进展", "东北商业航天"),
    ("【信达消费】白酒动销跟踪", "信达消费"),
])
def test_byline_form_is_detected(text: str, expected: str) -> None:
    """`(简称)(行业组)` 必须被认出来（旧形态一条都认不出）。"""
    assert _names(text) == [expected]


@pytest.mark.parametrize("text,expected", [
    # 用户最初的字面口径仍然有效（两条形态是**相加**，不是替换）
    ("【中泰证券】维持买入评级", "中泰证券"),
    ("【国联民生证券】发布深度报告", "国联民生证券"),
    ("某公司公告：印度国家证券存管有限公司", "印度国家证券"),
    # 研究所/研究部/研究院也是机构署名后缀（简称必须在名单里）
    ("天风研究所：下半年策略", "天风研究所"),
])
def test_classic_form_still_works(text: str, expected: str) -> None:
    """`XX证券` 那一路**一个字都没改**（它今天仍然满屏都是）。"""
    assert _names(text) == [expected]


def test_broker_pattern_verbatim_is_unchanged() -> None:
    """旧形态的正则**逐字未变** —— 新形态是加在它旁边，不是改它。

    改它的后果是同时改掉召回与误报（那一侧有实测语料撑着的边界讨论，
    见 `BROKER_PATTERN` 的说明），所以扩召回只能**新增**一条形态。
    """
    assert R.BROKER_PATTERN.pattern == r"[\u4e00-\u9fff]{2,8}证券"


def test_detected_name_is_a_verbatim_slice_with_correct_offset() -> None:
    """★ 名字必须是**原文切片**，`offset` 指向它在原文里的真实起点。

    用户要拿"机构：天风电子"去原文里核对；位置错了会让
    `in_summary`（"这个名字用户在卡片上看得见吗"）以一个错的坐标判断，
    进而把摘要里能看见的命中标成"正文后段未显示"。
    """
    text = "……早盘点评：【天风电子】团队认为景气度上行……"
    hits = R.content_hits(_item(summary=text))
    assert [h.name for h in hits] == ["天风电子"]
    h = hits[0]
    assert text[h.offset:h.offset + len(h.name)] == h.name
    assert h.kind == R.TRIGGER_BROKER
    assert h.in_summary is True


# ======================================================================
# ② 误报：普通公司名**不能**被当成机构
# ======================================================================

@pytest.mark.parametrize("text", [
    "招商银行发布三季度业绩快报",          # 银行：后缀名单刻意不收
    "东方财富证券营业部数据",              # 东方 + 财富（财富不是后缀）
    "平安保险公告年度分红方案",            # 保险不是后缀
    "长城汽车公布月度产销数据",            # 长城 + 汽车，**是上市公司** → 闸门挡
    "东方通信中标运营商集采",              # 东方 + 通信，**是上市公司** → 闸门挡
    "长江电力三季度发电量公告",            # 长江 + 电力，**是上市公司** → 闸门挡
    "国投电力获准发行可转债",              # 国投 + 电力，**是上市公司** → 闸门挡
])
def test_ordinary_company_names_are_not_institutions(text: str) -> None:
    """★ 精度优先：这些名字**一个都不能**被认成研究机构。

    ⚠️ 注意 `东方财富证券营业部` 里有"证券"两字，旧形态会切出一个
    "东方财富证券" —— 那**不是**机构署名，而是券商营业部的名字。
    这里如实记录：`证券` 那一路的边界由 `BROKER_PATTERN` 的既有讨论负责，
    本用例只保证**新形态**不会额外制造误报（断言里不出现 `东方财富`）。
    """
    got = _names(text)
    for bad in ("招商银行", "东方财富", "平安保险", "长城汽车", "东方通信",
                "长江电力", "国投电力"):
        assert bad not in got, f"{bad} 被误认成机构：{got}"


def test_listed_company_gate_is_the_only_thing_saving_changcheng(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """长城汽车靠的是**名录闸门**，不是运气 —— 把词表清空后它会（如实地）被认错。

    这条用例的意义是让"退化路径"可见：数据仓缺失时 `_is_listed_company`
    返回 False（放行），于是 `长城汽车` 会被算成机构。取舍与
    `tone._resolve_industry` 一致：宁可偶尔多一个名字，也不要因为读不到
    名录就让**所有**机构名整块消失（那种缺失没有任何报错线索）。
    """
    monkeypatch.setattr(vocab, "_TABLE", vocab.build_table([]))
    assert "长城汽车" in _names("长城汽车公布月度产销数据")


def test_bank_and_fund_are_not_suffixes() -> None:
    """`银行` / `基金` **刻意不收**（决策写进测试，免得后人"顺手补上"）。

    收进来的直接后果是"机构：招商银行"这种明显不对的标签上屏 ——
    而它与真正的研报署名长得一模一样，用户核不出问题。
    少认几个基金公司的署名是可接受的代价（用户："漏掉就漏掉吧"）。
    """
    assert "银行" not in R.BROKER_ORG_SUFFIXES
    assert "基金" not in R.BROKER_ORG_SUFFIXES
    assert "银行" not in R.BROKER_INDUSTRY_SUFFIXES
    assert "基金" not in R.BROKER_INDUSTRY_SUFFIXES
    # 研究语境后缀与行业组后缀都要有内容（空表会让新形态静默失效）
    assert "证券" in R.BROKER_ORG_SUFFIXES
    assert "电子" in R.BROKER_INDUSTRY_SUFFIXES
    assert R.BROKER_BASES, "简称名单为空 = 这条形态永远不会命中"


def test_one_text_can_carry_both_forms() -> None:
    """同一段文字里两种形态并存时都要出来，且顺序按位置。"""
    text = "【招商机械】早报：工程机械出口强劲；【中泰证券】维持买入评级"
    assert _names(text) == ["招商机械", "中泰证券"]


def test_broker_name_is_never_a_stock() -> None:
    """`is_research_house` 是抽取侧"绝不能当个股"那道闸门的判据。

    ⚠️ 它认的是**整个名字恰是一次机构署名**：`天风电子` 是机构，
    `长城汽车` **不是**（它本来就是一只股票，抽取侧不该拒它）。
    """
    assert R.is_research_house("天风电子") is True
    assert R.is_research_house("招商机械") is True
    assert R.is_research_house("中泰证券") is True
    assert R.is_research_house("长城汽车") is False      # 上市公司（名录闸门放行）
    assert R.is_research_house("华为") is False
    assert R.is_research_house("天风电子产业链") is False  # 短语，不是署名
    assert R.is_research_house("") is False


# ======================================================================
# ③ 触发语义**没变**：机构名只展示，不告警
# ======================================================================

def test_byline_broker_is_display_only_not_a_trigger() -> None:
    """用户口径："券商名不一定要告警，但**一定要前端输出信息**。"

    扩召回**不许**动摇这条：只有"点名分析师"与"模型给出方向"才弹窗，
    而机构名要出现在导出里（前端渲染"机构：天风电子"）。
    """
    item = _item(summary="【天风电子】半导体设备板块景气上行，订单饱满")
    reason = B.reason_of(item)
    assert reason.fires is False, "只有机构名就弹窗了（用户明确否掉的行为）"
    assert reason.triggers() == [], reason.triggers()
    assert reason.institutions_display() == ["天风电子"]

    # 同一份判据在契约层也要出得去（前端真正读的是这个字段）
    pub = IntelItem(
        kind="research_note",
        title="【天风电子】半导体设备",
        summary="【天风电子】半导体设备板块景气上行，订单饱满，机构上调盈利预测。" * 3,
        published_at="2026-10-01 10:00:00",
        source_alias="research-note-zsxq",
        content_hash="h-byline",
    ).to_public()
    assert pub["institutions"] == ["天风电子"]


def test_analyst_still_fires_without_broker_help() -> None:
    """对照：真正该弹的那一条（点名分析师）照旧弹 —— 扩召回没把触发条件改坏。"""
    item = _item(summary="【天风电子】孙潇雅认为景气度上行，订单能见度改善")
    reason = B.reason_of(item)
    assert reason.fires is True
    assert reason.triggers() == [R.TRIGGER_ANALYST]
    assert reason.institutions_display() == ["天风电子"]


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
