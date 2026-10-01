"""合规财务比率连接器测试（**全离线**：假 akshare + 真规则联动）。

## 这份测试要钉住的三件事（不是"把代码跑一遍"）

1. **`supports()` 的边界**：认 5 个给 A12 规则喂数的百分数口径指标；
   **不认**无关 id；并且**明确不认 `关联交易` 族**（附一条"为什么必须不认"的
   机器复现 —— 计数型指标会被规则当成百分数）。
2. **"缺失 → 不产点"是结构，不是承诺**：分子/分母/分项缺失、上游抛异常
   四条路径逐个断言**不产出 0、不产出占位值**（异常要么显式抛 DataFetchError，
   要么返回空列表，绝不用 0 糊过去）。
3. **★ 端到端**：连接器**真实输出形状**的点喂进 `evaluate_compliance()` 后，
   该族必须真的从「未量到」翻成「量到」，并且阈值真的触发旗标 ——
   `supports() is True` 只证明"路由会命中"，不证明"规则算得出来"。

假 akshare 用 `monkeypatch.setitem(sys.modules, "akshare", fake)`（本仓库
`tests/unit/test_akshare_connector.py` 的既有做法），不联网、不依赖 akshare 安装。
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date, timedelta

import pandas as pd
import pytest

from src.core.exceptions import DataFetchError, NoApplicableData
from src.core.schemas import DataSourceType, FetchMethod
from src.domain.agents.analysis.compliance.logic import evaluate_compliance
from src.infrastructure.connectors.compliance_fin_connector import (
    _RATIO_INDICATORS,
    _UNSUPPORTED_FAMILIES,
    ComplianceFinConnector,
)

#: 本连接器应当支持的 5 个族（**面向规则的百分数口径**）。
_SUPPORTED_PROBES: tuple[str, ...] = (
    "商誉占净资产比:600036",
    "货币资金占总资产比:600036",
    "有息负债占总资产比:600036",
    "大股东质押比例:600036",
    "对外担保占净资产比:600036",
)

#: 与合规规则无关的指标（**必须不认**，否则会把别人的取数劫走）。
_UNRELATED_PROBES: tuple[str, ...] = (
    "PE(TTM):600036",
    "stock_close:600036",
    "资产负债率:600036",
    "ROE:600036",
    "CPI",
    "商誉占净资产比",       # 缺 `:{code}` 后缀
    "商誉:600036",          # 旧口径名（本连接器不生产，避免与财务比率族抢路由）
)


# ============================================================================
# 假 akshare：三个接口的最小真实形状（列名照 2026-09-28 实测输出抄）
# ============================================================================


def _sina_bs(periods: list[str], **columns: list) -> pd.DataFrame:
    """构造 `stock_financial_report_sina` 形状的帧（首列必须是「报告日」）。"""
    data: dict[str, list] = {"报告日": periods}
    data.update(columns)
    return pd.DataFrame(data)


def _pledge_detail_frame(rows: list[dict]) -> pd.DataFrame:
    """`stock_gpzy_pledge_ratio_detail_em` 形状（列名照实测 15 列取用到的子集）。"""
    return pd.DataFrame(rows)


def _guarantee_frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


class _FakeAk:
    """假 akshare：三个接口可分别注入"抛异常"或"返回固定帧"。

    * `pledge` → `stock_gpzy_pledge_ratio_detail_em()`，**股东粒度整表**
      （实测 126,826 行 / 约 254 个分页请求 —— 所以 `calls` 里它的次数必须恒为 1）。
    """

    def __init__(
        self,
        *,
        bs: pd.DataFrame | None = None,
        bs_error: Exception | None = None,
        pledge: pd.DataFrame | None = None,
        pledge_error: Exception | None = None,
        guarantee: pd.DataFrame | None = None,
        guarantee_error: Exception | None = None,
    ) -> None:
        self.bs = bs
        self.bs_error = bs_error
        self.pledge = pledge
        self.pledge_error = pledge_error
        self.guarantee = guarantee
        self.guarantee_error = guarantee_error
        self.calls: list[tuple[str, dict]] = []

    def stock_financial_report_sina(self, stock: str, symbol: str) -> pd.DataFrame:
        self.calls.append(("sina_bs", {"stock": stock, "symbol": symbol}))
        if self.bs_error is not None:
            raise self.bs_error
        return self.bs

    def stock_gpzy_pledge_ratio_detail_em(self) -> pd.DataFrame:
        self.calls.append(("pledge", {}))
        if self.pledge_error is not None:
            raise self.pledge_error
        return self.pledge

    def stock_cg_guarantee_cninfo(
        self, symbol: str, start_date: str, end_date: str,
    ) -> pd.DataFrame:
        self.calls.append(
            ("guarantee", {"symbol": symbol, "start": start_date, "end": end_date}))
        if self.guarantee_error is not None:
            raise self.guarantee_error
        return self.guarantee


@pytest.fixture(autouse=True)
def _clean_cache():
    """连接器的截面缓存挂在类上 → 每个用例前后都必须清，否则用例之间互相污染。"""
    ComplianceFinConnector.reset_cache()
    yield
    ComplianceFinConnector.reset_cache()


def _fetch(indicator: str, end_date: str | None = None) -> list:
    conn = ComplianceFinConnector()
    return asyncio.run(conn.fetch(indicator, end_date=end_date))


def _install(monkeypatch, fake: _FakeAk) -> _FakeAk:
    monkeypatch.setitem(sys.modules, "akshare", fake)
    return fake


#: 一只"普通非金融公司"的资产负债表：三族都能量到。
_BS_PERIODS = ["20260630", "20260331", "20251231"]
_BS_COLUMNS = {
    "商誉": [4_000_000_000.0, 4_000_000_000.0, 3_900_000_000.0],
    "货币资金": [6_000_000_000.0, 5_500_000_000.0, 5_200_000_000.0],
    "短期借款": [2_000_000_000.0, 1_900_000_000.0, 1_800_000_000.0],
    "长期借款": [2_000_000_000.0, 2_000_000_000.0, 2_000_000_000.0],
    "应付债券": [float("nan"), float("nan"), float("nan")],
    "一年内到期的非流动负债": [1_000_000_000.0, 900_000_000.0, 800_000_000.0],
    "资产总计": [10_000_000_000.0, 9_800_000_000.0, 9_500_000_000.0],
    "归属于母公司股东权益合计": [10_000_000_000.0, 9_900_000_000.0, 9_600_000_000.0],
}


def _worst_case_bs() -> pd.DataFrame:
    """商誉 40% / 货币资金 60% / 有息负债 50% —— 四类阈值全部踩中。"""
    return _sina_bs(_BS_PERIODS, **_BS_COLUMNS)


# ============================================================================
# 一、supports() 的边界
# ============================================================================


@pytest.mark.parametrize("probe", _SUPPORTED_PROBES)
def test_supports_accepts_every_rule_family(probe: str):
    assert ComplianceFinConnector.supports(probe) is True


@pytest.mark.parametrize("probe", _UNRELATED_PROBES)
def test_supports_rejects_unrelated_indicators(probe: str):
    """不认无关 id —— 否则会把别的连接器的取数劫走（路由是首个命中者处理）。"""
    assert ComplianceFinConnector.supports(probe) is False


def test_capabilities_and_supports_are_one_implementation():
    """`get_capabilities()` 与 `supports()` 必须同源（本项目"同一判断只允许一份实现"）。

    两条机器判据：
      · capabilities 声明的每个 `XXX:{code}` 都能被 `supports()` 接受；
      · capabilities 声明的族集合 == 映射表的键集合（不许一边加了一边没加）。
    """
    caps = ComplianceFinConnector().get_capabilities()
    declared = [str(x) for x in caps["indicators"]]
    assert declared, "capabilities 不许为空"
    for template in declared:
        probe = template.replace("{code}", "600036")
        assert ComplianceFinConnector.supports(probe) is True, template
    assert {t.split(":")[0] for t in declared} == set(_RATIO_INDICATORS)
    # 声明里必须写明"每族只产最新一期"与"缺失不产点"这两条口径
    notes = caps["notes"]
    assert "最新一期" in notes
    assert "不产点" in notes


# ============================================================================
# 二、`关联交易` 族：为什么**必须**不支持（含陷阱的机器复现）
# ============================================================================


def test_related_party_family_is_refused_with_a_reason():
    """覆盖率 0% 的族不许接（`akshare_connector.py` 的既有纪律）。"""
    assert ComplianceFinConnector.supports("关联交易占营收比:600036") is False
    reason = _UNSUPPORTED_FAMILIES["关联交易"]
    # 理由必须写清"哪个源、缺什么"，而不是一句"暂不支持"
    assert "无金额字段" in reason
    assert "0 个" in reason


@pytest.mark.parametrize("probe", [
    "关联交易占营收比:600036",
    "关联交易金额:600036",
    "关联交易公告数:600036",
    "关联交易:600036",
])
def test_related_party_count_trap_is_blocked(probe: str):
    """★ 任何含 `关联交易` 的指标一律不支持。

    规则用**子串**匹配取值（`logic._find_value`）。如果哪天有人把
    `关联交易公告数:{code}` 这种**计数**口径接进来，它会被当成**百分数**
    去比 `> 30` —— 见下面那条自证测试。所以这里从路由层就挡住。
    """
    assert ComplianceFinConnector.supports(probe) is False


def test_rule_really_misreads_a_related_party_count_as_percent():
    """★ 自证：上面那条护栏不是多余的 —— 规则**确实**会把计数读成百分数。

    这是"如果按计数口径补这一族会发生什么"的机器复现：
    31 条关联交易公告 → 规则报「关联交易占比31%超30%，存在利益输送嫌疑」。
    **假旗标比缺数据更危险**，所以计数口径不能接（要接必须同时改规则，属另一件事）。
    """
    out = evaluate_compliance([{"indicator": "关联交易公告数:600036", "value": 31.0}])
    assert out["compliance_families_measured"] == ["关联交易"]
    assert any("关联交易占比31%" in flag for flag in out["compliance_flags"]), (
        f"陷阱没有复现（规则可能已改），请重新评估这条护栏：{out['compliance_flags']}"
    )


# ============================================================================
# 三、数据点形状（照 DataPoint 的真实契约断言，不猜）
# ============================================================================


def test_balance_sheet_point_shape_is_contract_compliant(monkeypatch):
    _install(monkeypatch, _FakeAk(bs=_worst_case_bs()))

    points = _fetch("商誉占净资产比:600519")

    assert len(points) == 1, "每族只产窗口内最新一期一个点"
    point = points[0]
    # —— 业务字段 ——
    assert point.indicator == "商誉占净资产比:600519"
    assert point.value == pytest.approx(40.0)          # 40亿 / 100亿 × 100
    assert point.unit == "%"
    assert point.period_date == "2026-06-30"           # ISO，不是 20260630
    # —— 溯源元数据（DATA_CONTRACT.md 强制 12 字段）——
    assert point.source_name and "新浪" in point.source_name
    assert point.source_url.startswith("https://")
    assert point.source_type is DataSourceType.API
    assert point.fetch_method is FetchMethod.WEB_CRAWL
    assert point.fetch_time is not None
    assert point.process_time is not None
    assert point.raw_content_hash, "raw_content_hash 不许为空"
    assert point.data_id
    assert point.processed_by == "A01_data_collector"
    assert 0.0 < point.confidence <= 1.0
    assert point.verified is False, "未做运行时交叉验证就不许标 verified=True"
    # —— 口径随数据一起下发（"这个数字准不准"必须可见）——
    assert "商誉" in point.extra["numerator"]
    assert "归母净资产" in point.extra["denominator"]
    assert point.extra["report_period"] == "2026-06-30"
    assert point.extra["source_columns"]["goodwill"] == "商誉"


def test_balance_sheet_family_returns_only_latest_period(monkeypatch):
    """★ 规则侧 `_find_value()` 取**首个命中** → 多期序列会让"用哪一期"取决于
    上游排序（不可见、不可断言）。本连接器用"只产一个点"把这件事变成结构保证。
    """
    _install(monkeypatch, _FakeAk(bs=_worst_case_bs()))

    points = _fetch("商誉占净资产比:600519")

    assert len(points) == 1
    assert points[0].period_date == max(
        f"{p[:4]}-{p[4:6]}-{p[6:]}" for p in _BS_PERIODS)


def test_window_selects_latest_period_inside_the_window(monkeypatch):
    """给了 end_date 就必须取**窗口内**最新一期，不许越窗偷看未来。"""
    _install(monkeypatch, _FakeAk(bs=_worst_case_bs()))

    points = _fetch("商誉占净资产比:600519", end_date="2026-03-31")

    assert len(points) == 1
    assert points[0].period_date == "2026-03-31"


# ============================================================================
# 四、缺失 → 不产点（绝不用 0 填充）
# ============================================================================


def test_missing_numerator_yields_no_point_not_zero(monkeypatch):
    """分子为空 → **空列表**。这是"没量到"，不许报成"量到 0%"。

    实测原型：`600519 贵州茅台` / `600276 恒瑞医药` 的 `商誉` 列 103 期全空
    （两家确实无并购商誉），但源上"无值"与"读数为 0"不可区分 → 保守侧不产点。
    """
    columns = dict(_BS_COLUMNS)
    columns["商誉"] = [float("nan")] * len(_BS_PERIODS)
    _install(monkeypatch, _FakeAk(bs=_sina_bs(_BS_PERIODS, **columns)))

    assert _fetch("商誉占净资产比:600519") == []


def test_missing_denominator_yields_no_point(monkeypatch):
    columns = dict(_BS_COLUMNS)
    columns["归属于母公司股东权益合计"] = [float("nan")] * len(_BS_PERIODS)
    _install(monkeypatch, _FakeAk(bs=_sina_bs(_BS_PERIODS, **columns)))

    assert _fetch("商誉占净资产比:600519") == []


def test_zero_denominator_yields_no_point(monkeypatch):
    """分母为 0 → 比率无定义 → 不产点（不许 ZeroDivisionError，更不许填 0）。"""
    columns = dict(_BS_COLUMNS)
    columns["归属于母公司股东权益合计"] = [0.0] * len(_BS_PERIODS)
    _install(monkeypatch, _FakeAk(bs=_sina_bs(_BS_PERIODS, **columns)))

    assert _fetch("商誉占净资产比:600519") == []


def test_column_absent_from_template_means_not_applicable_not_zero(monkeypatch):
    """★ 「口径不适用」与「接口没给」必须分开。

    实测原型：`600036 招商银行` 的资产负债表里**根本没有「货币资金」这一行**
    （银行对应科目是「现金及存放中央银行款项」）。这不是缺陷 —— 接口正常返回了
    整张 150 列的表，是这张表里没有该科目。所以：不产点，且**不许填 0**。
    """
    bank_columns = {
        "商誉": [9_954_000_000.0, 9_954_000_000.0, 9_954_000_000.0],
        "短期借款": [float("nan")] * 3,
        "长期借款": [float("nan")] * 3,
        "应付债券": [136_037_000_000.0, 142_484_000_000.0, 143_487_000_000.0],
        "资产总计": [13_785_280_000_000.0] * 3,
        "归属于母公司股东的权益": [1_345_033_000_000.0] * 3,
    }
    _install(monkeypatch, _FakeAk(bs=_sina_bs(_BS_PERIODS, **bank_columns)))

    assert _fetch("货币资金占总资产比:600036") == []
    assert _fetch("商誉占净资产比:600036") != [], "其余两族对同一张表仍然要能算"
    assert _fetch("有息负债占总资产比:600036") != []


def test_all_debt_components_blank_yields_no_point(monkeypatch):
    """四个分项全空 = 一个数都没有 → **不产点**，不许拿 4 个空值凑出一个 0。"""
    columns = dict(_BS_COLUMNS)
    for key in ("短期借款", "长期借款", "应付债券", "一年内到期的非流动负债"):
        columns[key] = [float("nan")] * len(_BS_PERIODS)
    _install(monkeypatch, _FakeAk(bs=_sina_bs(_BS_PERIODS, **columns)))

    assert _fetch("有息负债占总资产比:600519") == []


def test_debt_components_basis_is_disclosed(monkeypatch):
    """分项按 0 计入求和时，**逐项**登记进 extra（口径必须随数据下发）。"""
    _install(monkeypatch, _FakeAk(bs=_worst_case_bs()))

    point = _fetch("有息负债占总资产比:600519")[0]

    # 短期借款 20亿 + 长期借款 20亿 + 应付债券(空→0) + 一年内到期 10亿 = 50亿 / 100亿
    assert point.value == pytest.approx(50.0)
    assert point.extra["components_counted"] == ["短期借款", "长期借款",
                                                "一年内到期的非流动负债"]
    assert point.extra["components_zero_or_blank"] == ["应付债券"]
    assert "模板" in point.extra["basis"]


# ============================================================================
# 五、上游异常 → 抛 DataFetchError，**不是** 0、也不静默变成空
# ============================================================================


def test_balance_sheet_upstream_error_raises(monkeypatch):
    _install(monkeypatch, _FakeAk(bs_error=RuntimeError("sina down")))

    with pytest.raises(DataFetchError) as excinfo:
        _fetch("商誉占净资产比:600519")
    assert "sina down" in str(excinfo.value)
    assert "RuntimeError" in str(excinfo.value)


def test_balance_sheet_empty_frame_raises(monkeypatch):
    """空表 = 上游异常（不是"这只票没数据"），必须显式化。"""
    _install(monkeypatch, _FakeAk(bs=pd.DataFrame()))

    with pytest.raises(DataFetchError):
        _fetch("商誉占净资产比:600519")


def test_pledge_upstream_outage_raises_instead_of_silent_empty(monkeypatch):
    """★ 上游挂了 → 抛 DataFetchError，**不许**静默返回空。

    "这只票没有质押记录"与"整表没拉到"在结果上都是"没有点"，
    但前者是结论、后者是故障。混成一样，排查方向会完全相反。
    """
    _install(monkeypatch, _FakeAk(pledge_error=ConnectionError("network down")))

    with pytest.raises(DataFetchError) as excinfo:
        _fetch("大股东质押比例:600036")
    assert "东财质押股东明细获取失败" in str(excinfo.value)
    assert "network down" in str(excinfo.value)


def test_pledge_table_missing_columns_raises(monkeypatch):
    """上游模板变了（少列）→ 显式报错，不许把 KeyError 吞成空。"""
    broken = pd.DataFrame({"股票代码": ["600036"], "股东名称": ["某股东"]})
    _install(monkeypatch, _FakeAk(pledge=broken))

    with pytest.raises(DataFetchError) as excinfo:
        _fetch("大股东质押比例:600036")
    assert "缺少列" in str(excinfo.value)


def test_pledge_code_absent_from_table_yields_no_point_not_zero(monkeypatch):
    """★ 不在表里 ≠ 质押比例 0%。

    实测：股东粒度表只收录**有股东质押公告**的公司（2,406 只有未解押记录
    ≪ A 股约 5400 只），`600036 招商银行` / `600519 贵州茅台` / `000001 平安银行`
    在这张表里 **0 行** → 这三家在本族是**未量到**（正确结论，不是缺陷）。

    ## ★ 2026-09-30 口径变更（`CHG-0135`）：**空列表 → 抛带标记的异常**

    原来这里断言 `== []`。问题不在"不产点"（那是本族铁律），而在
    **"返回空列表"让上层无法区分**「这只票不在这张专题表里」与「真的取数失败」
    —— 实测用户面板上两者长得一模一样（都是「未获取到 X 数据」+ 置信度低），
    而处置相反（前者不用管、后者要去修链路）。现在抛
    `NoApplicableData(kind="not_covered")`：仍然**不产点、绝不填 0**，
    但原因**随异常上传**，`supervisor` 据此分流成
    「该专题未收录本主体（≠ 取值为 0，非缺陷）」—— 不进异常面板、不联网硬试。
    """
    frame = _pledge_detail_frame([{
        "股票代码": "600998", "股东名称": "北京点金投资有限公司",
        "质押股份数量": 88_000_000, "占所持股份比例": 31.82,
        "占总股本比例": 1.75, "状态": "未解押", "公告日期": date(2026, 9, 29),
    }])
    _install(monkeypatch, _FakeAk(pledge=frame))

    with pytest.raises(NoApplicableData) as ei:
        _fetch("大股东质押比例:600036")
    assert ei.value.kind == "not_covered"
    assert "不在质押股东明细内" in str(ei.value)
    assert "0%" in str(ei.value), "必须点明『不在表内』≠『质押比例为 0』"


def test_pledge_uses_holder_caliber_and_picks_the_heaviest_holder(monkeypatch):
    """★ 本族最关键的一条：口径是**股东粒度** `占所持股份比例`（分母=该股东持股数），
    且多股东时取**质押最重**的那个（控制权风险来自它）。

    为什么不能用市场截面 `质押比例`（分母=总股本）：实测全市场 `max=78.74`
    → 规则的 80% 严重档**永不触发**。这条测试把"用的是哪个字段"钉死。
    """
    frame = _pledge_detail_frame([
        # 同一股东多行 → 必须求和股数、并保留其最大比例
        {"股票代码": "603657", "股东名称": "陈弘旋", "质押股份数量": 1_500_000,
         "占所持股份比例": 35.71, "占总股本比例": 1.11, "状态": "未解押",
         "公告日期": date(2026, 9, 29)},
        {"股票代码": "603657", "股东名称": "陈弘旋", "质押股份数量": 2_700_000,
         "占所持股份比例": 64.29, "占总股本比例": 2.00, "状态": "未解押",
         "公告日期": date(2026, 9, 29)},
        # 更重的另一个股东 → 必须被选中
        {"股票代码": "603657", "股东名称": "陈凯", "质押股份数量": 4_300_000,
         "占所持股份比例": 88.00, "占总股本比例": 3.18, "状态": "未解押",
         "公告日期": date(2026, 9, 25)},
        # 已解押的**更重**记录 → 必须被排除（否则把历史当成当下）
        {"股票代码": "603657", "股东名称": "陈凯", "质押股份数量": 9_900_000,
         "占所持股份比例": 99.00, "占总股本比例": 7.00, "状态": "已解押",
         "公告日期": date(2025, 10, 10)},
    ])
    _install(monkeypatch, _FakeAk(pledge=frame))

    point = _fetch("大股东质押比例:603657")[0]

    assert point.indicator == "大股东质押比例:603657"
    # 88.00（陈凯，未解押）而不是 99.00（已解押）、也不是 64.29（陈弘旋）
    assert point.value == pytest.approx(88.00)
    assert point.unit == "%"
    assert point.period_date == "2026-09-29"        # ISO
    assert point.extra["caliber"] == (
        "pledging_shareholder_ratio_of_own_holding")
    assert point.extra["caliber_source_column"] == "占所持股份比例"
    assert point.extra["ratio_denominator"] == "该股东持股数"
    assert point.extra["top_holder"] == "陈凯"
    assert point.extra["top_holder_pct_of_total"] == pytest.approx(3.18)
    assert point.extra["holders_pledging"] == 2      # 陈弘旋 + 陈凯
    assert "max" in point.extra["aggregation"]
    # 实质性必须随数据下发（本连接器**不设**这个阈值）
    assert "materiality_note" in point.extra
    assert point.extra["materiality_note"].count("44") == 1
    assert "东方财富" in point.source_name


def test_pledge_aggregation_skips_rows_without_holder_ratio(monkeypatch):
    """`占所持股份比例` 为空的行不许当成 0 计入（源实测非空率 99.8%，有空值）。"""
    frame = _pledge_detail_frame([
        {"股票代码": "000672", "股东名称": "浙江上峰控股集团有限公司",
         "质押股份数量": 6_000_000, "占所持股份比例": float("nan"),
         "占总股本比例": 0.62, "状态": "未解押", "公告日期": date(2026, 9, 24)},
    ])
    _install(monkeypatch, _FakeAk(pledge=frame))

    assert _fetch("大股东质押比例:000672") == []


def test_pledge_all_repaid_yields_no_point(monkeypatch):
    """全部已解押 → 没有"当前"质押 → 不产点（也不许回退用历史值）。"""
    frame = _pledge_detail_frame([
        {"股票代码": "600998", "股东名称": "楚昌投资集团有限公司",
         "质押股份数量": 88_000_000, "占所持股份比例": 19.15,
         "占总股本比例": 1.75, "状态": "已解押", "公告日期": date(2026, 9, 29)},
    ])
    _install(monkeypatch, _FakeAk(pledge=frame))

    assert _fetch("大股东质押比例:600998") == []


def test_pledge_pull_is_cached_and_single_flight(monkeypatch):
    """★ 代价护栏：整表 254 个分页请求只能拉一次。

    两次连续取数（不同 code）→ 上游只被调用 **1 次**（24h TTL 命中）。
    没有这条，N 个 code 就是 N × 254 个请求。
    """
    frame = _pledge_detail_frame([
        {"股票代码": "600998", "股东名称": "北京点金投资有限公司",
         "质押股份数量": 88_000_000, "占所持股份比例": 31.82,
         "占总股本比例": 1.75, "状态": "未解押", "公告日期": date(2026, 9, 29)},
        {"股票代码": "603657", "股东名称": "陈弘旋", "质押股份数量": 2_700_000,
         "占所持股份比例": 64.29, "占总股本比例": 2.00, "状态": "未解押",
         "公告日期": date(2026, 9, 29)},
    ])
    fake = _install(monkeypatch, _FakeAk(pledge=frame))

    assert _fetch("大股东质押比例:600998")[0].value == pytest.approx(31.82)
    assert _fetch("大股东质押比例:603657")[0].value == pytest.approx(64.29)

    assert [c[0] for c in fake.calls].count("pledge") == 1, (
        "整表只许拉一次（24h TTL）；实际拉了 "
        f"{[c[0] for c in fake.calls].count('pledge')} 次")


def test_pledge_failure_is_circuit_broken_not_re_hammered(monkeypatch):
    """★ 代价护栏：失败路径也必须有冷却，不许每次调用都把 254 个请求重打一遍。

    修前：成功路径有缓存、**失败路径什么都不缓存** → 每来一次调用重打一遍
    （协作者复核实测：三只票 = 93 个必然失败的请求，下次调用还要再来一遍）。
    """
    fake = _install(monkeypatch, _FakeAk(pledge_error=ConnectionError("down")))

    with pytest.raises(DataFetchError) as first:
        _fetch("大股东质押比例:600036")
    calls_after_first = len(fake.calls)
    assert calls_after_first == 1, "整表拉一次（内部分页由 akshare 负责）"

    with pytest.raises(DataFetchError) as second:
        _fetch("大股东质押比例:600036")
    assert len(fake.calls) == calls_after_first, "冷却期内不许再打网络"
    assert "冷却中" in str(second.value)
    assert "还剩" in str(second.value)
    assert str(second.value).split("（")[0] == str(first.value).split("（")[0], (
        "两次的根因描述必须一致（否则排查会被第二次的另一句话带偏）")


def test_guarantee_upstream_error_raises(monkeypatch):
    _install(monkeypatch, _FakeAk(guarantee_error=RuntimeError("cninfo down")))

    with pytest.raises(DataFetchError) as excinfo:
        _fetch("对外担保占净资产比:000031", end_date="2026-09-24")
    assert "cninfo down" in str(excinfo.value)


# ============================================================================
# 五之二、★ 复核抓到的缺陷的回归（2026-09-29 协作者独立复核）
# ============================================================================


def test_future_end_date_is_clamped_to_today(monkeypatch):
    """★★ 缺陷回归：`end_date` 是**未来**时不许去问未来区间。

    实测现场：调用方（planner / SmartFetcher / 协作者探针）传年窗口
    `end_date="2026-12-31"`，而当天是 2026-09-28。担保窗口 = `end-365d ~ end`，
    若不夹就会去问一个**未来区间**（旧版质押实现更严重：以 anchor 逐日回退，
    31 天全在未来 → 每年 Q4 必然 31 连败，协作者探针实测三只票全败）。
    """
    today = date.today()
    frame = _guarantee_frame([{
        "证券代码": "000031", "证券简称": "大悦城",
        "公告统计区间": "x", "担保笔数": 9, "担保金额": 2605870.0,
        "归属于母公司所有者权益": 985934.52, "担保金融占净资产比例": 264.3,
    }])
    _install(monkeypatch, _FakeAk(guarantee=frame))

    point = _fetch("对外担保占净资产比:000031", end_date="2099-12-31")[0]

    assert point.extra["window_end"] == today.strftime("%Y-%m-%d")
    assert point.extra["window_end"] <= today.strftime("%Y-%m-%d"), "绝不许问未来区间"
    assert point.extra["window_start"] == (
        today - timedelta(days=365)).strftime("%Y-%m-%d")


def test_guarantee_empty_result_is_explainable(monkeypatch, caplog):
    """★ 复核要求："空"必须**可解释** —— 日志里要能看到窗口与表体量，
    这样"这只票不在表内"与"整张表是空的（接口静默失败）"一眼可分。

    `CHG-0135` 起**双通道**：日志给人看，异常给机器看（`kind="not_covered"`）。
    """
    frame = _guarantee_frame([{
        "证券代码": "000031", "证券简称": "大悦城",
        "公告统计区间": "2025-09-24---2026-09-24", "担保笔数": 9,
        "担保金额": 2605870.0, "归属于母公司所有者权益": 985934.52,
        "担保金融占净资产比例": 264.3,
    }])
    _install(monkeypatch, _FakeAk(guarantee=frame))

    with caplog.at_level("INFO", logger="src.infrastructure.connectors.compliance_fin_connector"):
        with pytest.raises(NoApplicableData) as ei:
            _fetch("对外担保占净资产比:600036", end_date="2026-09-24")

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "600036" in text
    assert "20250924" in text and "20260924" in text, "窗口必须打出来"
    assert "表内 1 只" in text, "表体量必须打出来（空表 vs 缺这只票 靠它区分）"
    assert "不产点" in text
    assert ei.value.kind == "not_covered", "异常里也要带机器可读原因"
    assert "不在对外担保表内" in str(ei.value)


def test_guarantee_code_absent_from_table_yields_no_point(monkeypatch):
    """★ 实测原型：`600036`/`600519`/`000001` 在四个窗口里都命中 0 行
    （没有对外担保公告）。"无公告"与"担保为 0"在源上不可区分 → 不产 0 点。

    `CHG-0135`：改为抛 `NoApplicableData(kind="not_covered")`（原因随异常上传），
    **仍然不产点、绝不填 0**。
    """
    frame = _guarantee_frame([{
        "证券代码": "000031", "证券简称": "大悦城",
        "公告统计区间": "2025-09-24---2026-09-24", "担保笔数": 9,
        "担保金额": 2605870.0, "归属于母公司所有者权益": 985934.52,
        "担保金融占净资产比例": 264.3,
    }])
    _install(monkeypatch, _FakeAk(guarantee=frame))

    with pytest.raises(NoApplicableData) as ei:
        _fetch("对外担保占净资产比:600036", end_date="2026-09-24")
    assert ei.value.kind == "not_covered"


def test_guarantee_point_shape_period_is_iso(monkeypatch):
    """★ 回归：窗口是 `YYYYMMDD`，`period_date` 必须是 ISO。

    实测踩过：直接把 `window[1]` 塞进 `period_date` → 产出 `20260929`
    这种非 ISO 值（`DataPoint.period_date` 契约要求 ISO）。
    """
    frame = _guarantee_frame([{
        "证券代码": "000031", "证券简称": "大悦城",
        "公告统计区间": "2025-09-24---2026-09-24", "担保笔数": 9,
        "担保金额": 2605870.0, "归属于母公司所有者权益": 985934.52,
        "担保金融占净资产比例": 264.3,
    }])
    _install(monkeypatch, _FakeAk(guarantee=frame))

    point = _fetch("对外担保占净资产比:000031", end_date="2026-09-24")[0]

    assert point.period_date == "2026-09-24"
    assert len(point.period_date) == 10 and point.period_date[4] == "-"
    assert point.value == pytest.approx(264.3)
    assert point.extra["window_start"] == "2025-09-24"
    assert point.extra["window_end"] == "2026-09-24"
    assert point.extra["guarantee_count"] == 9
    assert "巨潮" in point.source_name
    # ★ 口径差异必须随数据下发（源给的是区间累计，不是期末余额）
    assert "累计" in point.extra["basis"]
    assert point.confidence <= 0.7, "口径有差异的读数不许给高置信"


# ============================================================================
# 六、★ 端到端：连接器输出 → 规则**真的**从「未量到」翻成「量到」并触发旗标
# ============================================================================


def _payload(points) -> list[dict]:
    """把连接器输出转成 A12 拿到的形状（规则只读 indicator/value）。"""
    return [{"indicator": p.indicator, "value": p.value, "period_date": p.period_date}
            for p in points]


def test_rule_was_unmeasured_before_and_is_measured_after(monkeypatch):
    """★★★ 本文件的核心断言：**端到端**的"没量到 → 量到了"。

    只用 `supports() is True` 证明不了任何事 —— 它只说明路由会命中，
    不说明规则算得出来。这里走的是真实路径：
    `fetch()`（假 akshare，形状照实测输出）→ `evaluate_compliance()`。

    对照组：`evaluate_compliance([])` 必须是「未量到」且 6 族全未量到
    （这就是补生产者之前的真实状态）。
    """
    before = evaluate_compliance([])
    assert before["compliance_families_measured"] == []
    assert before["compliance_level_calc"] == "未量到"
    assert len(before["compliance_families_unmeasured"]) == 6

    _install(monkeypatch, _FakeAk(
        bs=_worst_case_bs(),
        pledge=_pledge_detail_frame([{
            "股票代码": "600519", "股东名称": "中国贵州茅台酒厂(集团)有限责任公司",
            "质押股份数量": 100.0, "占所持股份比例": 85.0,
            "占总股本比例": 4.0, "状态": "未解押", "公告日期": date(2026, 9, 24),
        }]),
        guarantee=_guarantee_frame([{
            "证券代码": "600519", "证券简称": "贵州茅台",
            "公告统计区间": "2025-09-24---2026-09-24", "担保笔数": 5,
            "担保金额": 1200000.0, "归属于母公司所有者权益": 1000000.0,
            "担保金融占净资产比例": 120.0,
        }]),
    ))

    points = []
    for prefix in ("商誉占净资产比", "货币资金占总资产比", "有息负债占总资产比",
                   "对外担保占净资产比"):
        points += _fetch(f"{prefix}:600519", end_date="2026-09-24")
    points += _fetch("大股东质押比例:600519")

    assert len(points) == 5, "五族都必须真的取到点（取不到就不叫端到端）"

    after = evaluate_compliance(_payload(points))

    for family in ("商誉", "货币资金", "有息负债", "质押", "担保"):
        assert family in after["compliance_families_measured"], (
            f"{family} 族仍未量到：{after}")
    assert after["compliance_families_unmeasured"] == ["关联交易"]
    assert after["compliance_measured"] is True

    # ★ 阈值必须真的触发（不是"量到了但一条旗标都没有"）
    flags = " | ".join(after["compliance_flags"])
    assert "商誉/净资产40%超30%" in flags
    assert "大股东股权质押比例85%超80%" in flags
    assert "对外担保/净资产120%超100%" in flags
    assert "存贷双高" in flags, "货币资金60% + 有息负债50% 应触发存贷双高"
    assert after["compliance_level_calc"] == "高"
    assert after["severe_flag_count"] == 3   # 质押 + 担保 + 存贷双高
    assert "未见明显合规风险信号" not in after["compliance_flags"]


def test_measured_and_clean_is_not_unmeasured(monkeypatch):
    """反面对照：量到了、都没超阈值 → 「无」（**不是**「未量到」）。

    这两者在旧的假绿实现里共用同一个返回值，是 2026-09-29 修掉的那张
    「伪造体检合格证」。本连接器的存在让"量到了"这一侧第一次真的可达。
    """
    _install(monkeypatch, _FakeAk(
        bs=_sina_bs(_BS_PERIODS, **{
            "商誉": [100_000_000.0] * 3,          # 1% → 不触发
            "货币资金": [1_000_000_000.0] * 3,     # 10% → 不触发
            "短期借款": [200_000_000.0] * 3,
            "长期借款": [float("nan")] * 3,
            "应付债券": [float("nan")] * 3,
            "一年内到期的非流动负债": [float("nan")] * 3,
            "资产总计": [10_000_000_000.0] * 3,
            "归属于母公司股东权益合计": [10_000_000_000.0] * 3,
        }),
        pledge=_pledge_detail_frame([{
            # ★ 质押 0.0% 是**有效读数**（真的没质押）→ 必须算「量到」
            "股票代码": "600519", "股东名称": "某股东",
            "质押股份数量": 0.0, "占所持股份比例": 0.0,
            "占总股本比例": 0.0, "状态": "未解押", "公告日期": date(2026, 9, 24),
        }]),
    ))

    points = []
    for prefix in ("货币资金占总资产比", "有息负债占总资产比", "商誉占净资产比"):
        points += _fetch(f"{prefix}:600519", end_date="2026-09-24")
    points += _fetch("大股东质押比例:600519")

    out = evaluate_compliance(_payload(points))

    assert set(out["compliance_families_measured"]) == {"商誉", "货币资金", "有息负债", "质押"}
    assert out["compliance_level_calc"] == "无", "量到了且都合格 → 无（与「未量到」不同）"
    assert out["compliance_flags"] == ["未见明显合规风险信号"]
    # ★ 质押 0% 是**有效读数**（真的没质押），必须算"量到"，不许被当成缺数据
    assert "质押" in out["compliance_families_measured"]


def test_unsupported_indicator_raises_before_any_network_call(monkeypatch):
    """不支持的指标必须在**发请求之前**抛错（不许先打网络再报不支持）。"""
    fake = _install(monkeypatch, _FakeAk(bs=_worst_case_bs()))

    with pytest.raises(DataFetchError):
        _fetch("关联交易占营收比:600519")
    assert fake.calls == []


def test_bad_code_is_rejected(monkeypatch):
    fake = _install(monkeypatch, _FakeAk(bs=_worst_case_bs()))

    with pytest.raises(DataFetchError):
        _fetch("商誉占净资产比:ABCDEF")
    assert fake.calls == []


@pytest.mark.parametrize("prefix", [
    "商誉占净资产比", "货币资金占总资产比", "有息负债占总资产比",
    "大股东质押比例", "对外担保占净资产比",
])
def test_etf_codes_are_refused_for_every_family(monkeypatch, prefix: str):
    """★ ETF/基金没有个股财务报表，也当不了质押/担保主体 → 五族一律显式拒绝。

    照 `AkshareConnector._fin_ratio_points` 的既有纪律："ETF无个股财务报表，
    禁止误打个股接口产生误导性数据"。这里对它更严格：**全部五族**都拒，
    而不是只拒资产负债表族 —— 截面表里查不到 ETF 会返回空列表，
    那会把"口径不适用"伪装成"这只票没数据"。
    """
    fake = _install(monkeypatch, _FakeAk(bs=_worst_case_bs()))

    with pytest.raises(DataFetchError) as excinfo:
        _fetch(f"{prefix}:510300")
    assert "ETF" in str(excinfo.value)
    assert fake.calls == [], "必须在发请求之前拒绝"
