"""判据：`not_applicable` 必须在**真实抛点**上有生产者（不能只有文本标记）。

## 防的是什么（每条判据对应一种会静默变假的失效）

审计的完整性判据原来只看 `data_refs` 是否为空，把三种含义完全不同的情形判成
一件事；上一轮修好了审计侧（`gap_kind` 三态豁免 + fail-closed + 缺口台账），
但留了一个**确切的缺口**：`NoApplicableData.KINDS` 里的 `not_applicable`
在结构化载体上**零生产者** ——

* `compliance_fin_connector` 两处抛的都是 `kind="not_covered"`；
* 最典型的那条「银行没有流动比率」由 `akshare_connector._stock_fundamental`
  抛**普通 `DataFetchError`**，只带散文。

后果：`not_applicable` 只能靠 `supervisor.NOT_APPLICABLE_MARKERS` 做**文本匹配**
兜底 —— 而文本匹配正是上一轮明确要退役的路径（拿自然语言猜语义）。

| # | 判据 | 防的是 |
|---|---|---|
| 1 | `test_fin_ratio_entity_absent_is_structured_not_applicable` | 生产者不存在（回到"只有标记、没有 `kind`"） |
| 2 | `test_fetch_sync_entry_also_produces_the_kind` | 只改了内层方法、真实入口那一跳没走它 |
| 3 | `test_fin_ratio_real_failures_are_not_exempt` | **一刀切**：把"区间没覆盖到 / 源返回空 / 列名写错"也豁免掉 |
| 4 | `test_etf_statement_caliber_split_is_evidence_based` | ETF 那两档被**互换**（口径不存在 ↔ 表不收录） |
| 5 | `test_router_interception_and_marker_survive` | 既有 `except DataFetchError` 拦截面被改坏（漏接 ⇒ 路由不再换源） |
| 6 | `test_kind_is_a_real_carrier_not_the_text` | 只用文本承载结论：抹掉标记就认不出来（"结构化"是装饰） |

## 自证（B5）

把生产点的 `kind="not_applicable"` 去掉（改回普通 `DataFetchError`）⇒ 判据 1 必红。
实测输出见交付说明（真改源码跑一次红灯再还原）。

## 纪律

全部**进程内**：替身 akshare 只实现被调用的那几个方法，不发网络请求、
不写 `data/`；`kind` 的取值一律**现算**（不抄字面量），与 `NoApplicableData.KINDS`
同源。
"""
from __future__ import annotations

import sys
from datetime import date, datetime
from typing import Any

import pandas as pd
import pytest

from src.core.exceptions import DataFetchError, NoApplicableData
from src.core.schemas import DataPoint
from src.infrastructure.connectors.akshare_connector import AkshareConnector

# ============================================================
# 替身（只实现被调到的方法；记录调用以便证明"走的是真实那条路径"）
# ============================================================


class _FakeAk:
    """最小 akshare 替身：只提供个股财务指标表接口。"""

    def __init__(self, frame: Any) -> None:
        self._frame = frame
        self.calls: list[tuple[str, str]] = []

    def stock_financial_analysis_indicator(self, symbol: str, start_year: str) -> Any:
        self.calls.append((symbol, start_year))
        return self._frame


def _bank_like_frame() -> pd.DataFrame:
    """银行形状的财务指标表：列**在**，但这只票的该列**整列无值**。

    新浪源用 NaN 或 `"--"` 表示"没披露/不适用"，两者都必须被认成无值 ——
    只认 NaN 会把 `"--"` 那部分现场漏掉（实测该接口两种都出现过）。
    """
    return pd.DataFrame({
        "日期": [date(2026, 3, 31), date(2025, 12, 31), date(2025, 9, 30)],
        "流动比率": [float("nan"), "--", None],
    })


def _assert_real_failure(exc: BaseException) -> None:
    """真失败的三条断言：不是结构化豁免、没有 `kind`、分类器**也不许**豁免它。

    最后一条最关键：豁免靠 `kind` **或**文本标记，只要文案里混进了标记，
    真缺口就会被说成"本来就没有"。所以这里必须问**真实的分类器**，而不是
    只看异常类型（那正是上一版的漏洞形状）。
    """
    from src.domain.agents.data.collector.logic import classify_gap_kind

    assert not isinstance(exc, NoApplicableData), (
        f"真失败被标成了「语义不适用」：{exc}")
    assert getattr(exc, "kind", None) is None
    assert classify_gap_kind(exc) is None, (
        f"真失败被审计侧豁免了：{exc} —— 文本里混进了豁免标记（多豁免比多报假阳性危险）")


# ============================================================
# ① 生产者存在（结构化载体）
# ============================================================


def test_fin_ratio_entity_absent_is_structured_not_applicable() -> None:
    """★ 「该实体无值」必须抛 `NoApplicableData(kind="not_applicable")`。

    三条同时成立才算合格：
      ① 结构化 `kind` 与 `NoApplicableData.KINDS` 同源（不是自造取值）；
      ② 仍是 `DataFetchError` 子类 ⇒ 既有 `except DataFetchError` 拦截面不漏接；
      ③ 原散文**逐字**保留（`supervisor` 的标记核对与客户文案都依赖它）。
    """
    from src.domain.agents.data.collector.logic import (
        GAP_KIND_NOT_APPLICABLE,
        classify_gap_kind,
    )

    ak = _FakeAk(_bank_like_frame())
    with pytest.raises(NoApplicableData) as ei:
        AkshareConnector()._stock_fundamental(ak, "流动比率:600036", None, None)
    exc = ei.value

    # 证明走的是真实那条路径（真的去取了这张表），而不是替身自己抛的
    assert ak.calls == [("600036", str(datetime.now().year - 3))], ak.calls

    assert isinstance(exc, DataFetchError), "它仍必须是取数错误的一种"
    assert exc.kind == "not_applicable"
    assert exc.kind in NoApplicableData.KINDS
    assert classify_gap_kind(exc) == GAP_KIND_NOT_APPLICABLE

    # ③ 文案逐字保留（既有判据/文案映射靠它）
    assert "该实体无值" in str(exc)

    # ① kind ↔ 标记**同源**：现算每个 kind 的标记，不抄字面量
    markers = {kind: str(NoApplicableData("", kind=kind))
               for kind in NoApplicableData.KINDS}
    assert NoApplicableData.MARKER_NOT_APPLICABLE in markers[exc.kind]
    assert NoApplicableData.MARKER_NOT_APPLICABLE in str(exc)
    assert NoApplicableData.MARKER_NOT_COVERED not in str(exc), "不许串到「未收录」那一档"


def test_fetch_sync_entry_also_produces_the_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 真实入口 `_fetch_sync`（经 `_load_extra_points`）那一跳也要抛结构化异常。

    防的是"只改了内层方法、外层却换了一条路"—— 本仓库记录过这类失效：
    判据接在**没人走**的路上。这里注入替身 akshare 模块，走真实入口。
    """
    fake = _FakeAk(_bank_like_frame())
    monkeypatch.setitem(sys.modules, "akshare", fake)

    with pytest.raises(NoApplicableData) as ei:
        AkshareConnector()._fetch_sync("流动比率:600036", None, None)

    assert ei.value.kind == "not_applicable"
    assert fake.calls, "入口那一跳没有真的去取这张表 —— 判据可能接在别的分支上"


# ============================================================
# ② 语义不混：同一路径上的"真失败"**不许**被豁免
# ============================================================


def test_fin_ratio_real_failures_are_not_exempt() -> None:
    """★★ 防一刀切：同一条抛点上的四种"真失败"形状，一条都不许变豁免。

    | 形状 | 证据 | 下一步 |
    |---|---|---|
    | 列名不在源表列中 | `df.columns` 有，但没有这一列 | **我们的契约写错了**（改映射表） |
    | 列在、有值，但不在请求区间内 | 全表可转数值 > 0 | 放宽区间 / 更新源 |
    | 帧为空（源什么都没返回） | 0 期 | 查源可用性 |
    | 帧是 None（源返回空对象） | 无法读列 | 查源可用性（原实现会 `AttributeError`） |

    第三条尤其要防：空帧带着"列存在、一个值都没有"的形状掉进豁免分支，
    就会**把源故障说成"该口径不适用"** —— 那是最危险的方向。
    """
    conn = AkshareConnector()

    # (a) 列名不在源表列中 → 契约错误
    wrong_col = pd.DataFrame({"日期": [date(2026, 3, 31)], "资产负债率(%)": [75.0]})
    with pytest.raises(DataFetchError) as ei:
        conn._stock_fundamental(_FakeAk(wrong_col), "流动比率:600036", None, None)
    _assert_real_failure(ei.value)
    assert "列未命中" in str(ei.value)

    # (b) 列在、有值，但都不在请求区间内 → 区间/时效问题
    has_value = pd.DataFrame({"日期": [date(2026, 3, 31)], "流动比率": [1.71]})
    with pytest.raises(DataFetchError) as ei:
        conn._stock_fundamental(_FakeAk(has_value), "流动比率:600036",
                                "2020-01-01", "2020-12-31")
    _assert_real_failure(ei.value)
    assert "区间" in str(ei.value)

    # (c) 帧为空（源什么都没返回）
    with pytest.raises(DataFetchError) as ei:
        conn._stock_fundamental(_FakeAk(pd.DataFrame()), "流动比率:600036", None, None)
    _assert_real_failure(ei.value)
    assert "源表为空" in str(ei.value)

    # (d) 帧是 None：原实现会在 `df.columns` 上抛 AttributeError（非取数错误）
    with pytest.raises(DataFetchError) as ei:
        conn._stock_fundamental(_FakeAk(None), "流动比率:600036", None, None)
    _assert_real_failure(ei.value)


# ============================================================
# ③ 第二处同类抛点：按证据分档，不许多豁免
# ============================================================


def test_etf_statement_caliber_split_is_evidence_based() -> None:
    """★ ETF 请求个股财务口径时，**按语义分两档**（两个闸门、两个 `kind`，不许互换）：

    * `流动比率/资产负债率…`（个股**财务报表**口径）：ETF 没有资产负债表，
      该口径对 ETF 根本不存在（与"银行没有流动比率"同形）⇒ `not_applicable`。
      证据是**代码本身是不是 ETF**，与源可用性无关 ⇒ 结构化、不是猜语义；
    * `总市值/股息率/股息率TTM/换手率`：这些口径对 ETF **客观存在**（ETF 也有市值、
      也可能分红），而我们的表（全A股截面 `quant_daily_basic`）与股息率合成链
      **只收个股** ⇒ 缺的是"**表/链不收录这个主体**" ⇒ **覆盖问题** ⇒ `not_covered`。

    ## ⚠️ 这条判据的断言在 2026-10-02 变过（用户裁定 2，原话「算**覆盖问题**」）

    用户裁定之前，这里断言 `总市值/股息率` 抛**普通 `DataFetchError`**（fail-closed），
    理由是"两个 `kind` 都不对：`not_applicable` 是**假的**（口径明明存在）＝多豁免，
    而覆盖问题当时还没有落到这个判点上"。用户 2026-10-02 明确裁定这一档
    「算**覆盖问题**」（`docs/PRD.md` §33.5 第 2 条问的就是 `总市值:588170`
    这个"先命中 ETF 检查、永远走不到新探针"的判点）⇒ 它应当是
    `NoApplicableData(kind="not_covered")`，普通失败那一版断言随之**过期**。

    这是**裁定变了**，不是放松护栏：`not_covered` 的含义是"承认覆盖不到、**别去补**"，
    与普通失败（"取数链故障、去修"）处置相反 —— 换错档会把排查方向带偏。

    ⚠️ 两档**不许互换**（下面两组互斥断言就是钉这个）。
    """
    from src.domain.agents.data.collector.logic import (
        GAP_KIND_NOT_APPLICABLE,
        GAP_KIND_NOT_COVERED,
        classify_gap_kind,
    )

    ak = _FakeAk(pd.DataFrame({"日期": [date(2026, 3, 31)], "流动比率": [1.71]}))
    conn = AkshareConnector()

    # 档一：口径对 ETF **不存在** ⇒ not_applicable
    with pytest.raises(NoApplicableData) as ei:
        conn._stock_fundamental(ak, "流动比率:588170", None, None)
    na = ei.value
    assert na.kind == "not_applicable"
    assert classify_gap_kind(na) == GAP_KIND_NOT_APPLICABLE
    assert NoApplicableData.MARKER_NOT_APPLICABLE in str(na)
    assert NoApplicableData.MARKER_NOT_COVERED not in str(na), (
        "口径「不存在」被说成了「我们没收录」—— 两档互换")

    # 档二：口径对 ETF **存在**、是我们的表/链不收录它 ⇒ not_covered（覆盖问题）
    for indicator in ("总市值:588170", "股息率:588170", "股息率TTM:588170",
                      "换手率:588170"):
        with pytest.raises(NoApplicableData) as ei:
            conn._stock_fundamental(ak, indicator, None, None)
        exc = ei.value
        assert isinstance(exc, DataFetchError), "必须仍是取数错误（拦截面不许变）"
        assert exc.kind == "not_covered", f"{indicator} 的档位变了：{exc}"
        assert classify_gap_kind(exc) == GAP_KIND_NOT_COVERED, (
            f"{indicator}：真实分类器不认它是「未收录」：{exc}")
        assert NoApplicableData.MARKER_NOT_COVERED in str(exc)
        assert NoApplicableData.MARKER_NOT_APPLICABLE not in str(exc), (
            "覆盖问题 ≠ 口径不存在（两档不许互换）")
        assert "无个股" in str(exc), "原文案必须逐字保留（构造函数据它拼标记）"

    assert not ak.calls, "ETF 被拒时**一次都不许**打个股财务接口（防误导性数据）"


# ============================================================
# ④ 既有拦截面不破 + 标记跨层可读
# ============================================================


class _ProducerConnector:
    """把**真实连接器**当源接进路由器：异常由真实抛点产出，不是手写样本。"""

    source_name = "akshare"
    source_url = "test://akshare"

    def __init__(self, frame: Any) -> None:
        self._frame = frame

    async def fetch(self, indicator: str, start_date: str | None = None,
                    end_date: str | None = None) -> list[DataPoint]:
        return AkshareConnector()._stock_fundamental(
            _FakeAk(self._frame), indicator, start_date, end_date)

    def get_capabilities(self) -> dict[str, Any]:
        return {"name": self.source_name, "indicators": ["流动比率"]}


class _SuccessConnector:
    """链上的下一个源：它必须**真的被走到**（证明故障转移没坏）。"""

    source_name = "stub-ok"
    source_url = "test://stub-ok"

    def __init__(self, points: list[DataPoint]) -> None:
        self._points = list(points)
        self.calls = 0

    async def fetch(self, indicator: str, start_date: str | None = None,
                    end_date: str | None = None) -> list[DataPoint]:
        self.calls += 1
        return list(self._points)

    def get_capabilities(self) -> dict[str, Any]:
        return {"name": self.source_name, "indicators": ["流动比率"]}


async def test_router_interception_and_marker_survive() -> None:
    """★ 两件事一起守：

    ① `NoApplicableData` 是 `DataFetchError` 子类 ⇒ 路由器**照样换源**
       （既有 `except DataFetchError` 拦截点行为不变，不许"漏接"）；
    ② 所有源都是"不适用"时，路由器把错误聚合成一条新 `DataFetchError`
       —— **类型会丢，标记不许丢**：`classify_gap_kind` 仍要认出 `not_applicable`。
       少了②，最典型的那个现场（银行/流动比率）又会回到缺陷清单。
    """
    from src.domain.agents.data.collector.logic import (
        GAP_KIND_NOT_APPLICABLE,
        classify_gap_kind,
    )
    from src.infrastructure.connectors.router import ConnectorRouter

    ok = _SuccessConnector([DataPoint(indicator="流动比率:600036", value=1.71,
                                      period_date="2026-03-31", source_name="stub")])

    # ① 故障转移照旧
    router = ConnectorRouter([
        (_ProducerConnector(_bank_like_frame()), lambda i: True),
        (ok, lambda i: True),
    ])
    assert await router.fetch("流动比率:600036")
    assert ok.calls == 1, "结构化异常被漏接了 ⇒ 路由不再换源"

    # ② 聚合后：类型丢、标记留下
    router_all_fail = ConnectorRouter([
        (_ProducerConnector(_bank_like_frame()), lambda i: True),
    ])
    with pytest.raises(DataFetchError) as ei:
        await router_all_fail.fetch("流动比率:600036")
    aggregated = ei.value
    assert not isinstance(aggregated, NoApplicableData), "聚合后类型本就该丢（既有行为）"
    assert NoApplicableData.MARKER_NOT_APPLICABLE in str(aggregated)
    assert classify_gap_kind(aggregated) == GAP_KIND_NOT_APPLICABLE


# ============================================================
# ⑤ 结构化必须**真的**在承载结论（不是装饰）
# ============================================================


def test_kind_is_a_real_carrier_not_the_text() -> None:
    """★ 把**所有**豁免标记文本抹掉，结论不许变 —— 这才叫"标记不再是唯一载体"。

    做法可执行、可复现：拿**真实抛点**产出的异常，把消息里分类器认得的
    **每一个**标记都替换掉，再问真实分类器。若结论随文本一起消失，
    说明 `kind` 只是装饰，"退役文本匹配"就是一句空话。

    ⚠️ 标记必须**现读** `gap_marker_tables()`（分类器的事实源），不许只抹
    `MARKER_NOT_APPLICABLE` 一个：本判据第一版就是这么写的，结果对照组仍被判成
    `not_applicable` —— 因为这句散文里**还带着** `supervisor` 那一族标记
    （「该实体无值」）。只抹一个标记等于**没抹干净**，判据会自己骗自己。
    """
    from src.domain.agents.data.collector.logic import (
        GAP_KIND_NOT_APPLICABLE,
        classify_gap_kind,
        gap_marker_tables,
    )

    with pytest.raises(NoApplicableData) as ei:
        AkshareConnector()._stock_fundamental(
            _FakeAk(_bank_like_frame()), "流动比率:600036", None, None)
    exc = ei.value
    assert getattr(exc, "kind", None) in NoApplicableData.KINDS

    not_applicable_markers = gap_marker_tables()[0]
    assert not_applicable_markers, "取不到标记表 —— 判据失去目标，会静默恒绿"
    stripped = str(exc)
    for marker in not_applicable_markers:
        stripped = stripped.replace(marker, "（标记已抹掉）")
    assert not any(m in stripped for m in not_applicable_markers), stripped

    # ① 结构化版：文本标记全没了，结论**仍在**（结论来自 kind）
    exc.args = (stripped,)
    assert classify_gap_kind(exc) == GAP_KIND_NOT_APPLICABLE

    # ② 纯文本版（同样文本、同样抹掉全部标记）⇒ 认不出来
    #    这一条是对照组：它证明①里的结论确实由 kind 承载，而不是文本碰巧还在
    assert classify_gap_kind(DataFetchError(stripped)) is None
