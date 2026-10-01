"""宏观补充连接器（官方 PMI / GDP）测试 —— **全离线**：注入假 akshare 帧 + 打桩网络。

## 为什么这个文件值得存在

用户报障「要确保数据都能找到，不存在数据缺少问题」时，实测的缺口是：
`PMI` / `GDP` 在采集侧**没有任何连接器实现**（19 个连接器 `supports()` 全为 False），
而 A08 白名单、`fetch_depth`、`synonym_dict` 三处独立契约早把它们当真实指标。
补上取数之后，真正容易**静默**出错的地方有四处，逐条钉在这里：

  1. **降序连接器**：东财帧是新→旧，原样透出会与"命中本地 DB"的升序结果不一致
     （`series_to_points` 的 docstring 记录过同一个坑）→ `test_points_are_ascending`；
  2. **季度标签塌陷**：`akshare_connector.period_to_iso` 把 `2026年第1-2季度` 与
     `2026年第1季度` 都解析成 `2026-01`（实测）→
     `test_quarter_labels_do_not_collapse`；
  3. **列名漂移抓错列**：`制造业-指数` 是 `非制造业-指数` 的子串；同族的
     `制造业-同比增长` 量级是 ±4 → `test_missing_column_raises_instead_of_sibling`
     与 `test_out_of_range_rows_are_not_returned`；
  4. **没量到 ≠ 量到 0**：NaN / 空帧 / 接口抛错都必须是"没有数据"，
     绝不能变成 0 或占位值 → `test_*_raises_*` 若干条。

帧片段取自 2026-09-28 的真实接口输出（列名、量级、排序方向照抄），
但**不联网**：`macro_extra_connector._import_akshare` 被替换成假模块。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import yaml

from src.core.exceptions import DataFetchError
from src.core.schemas import DataSourceType, FetchMethod
from src.infrastructure.connectors import macro_extra_connector as mod
from src.infrastructure.connectors.macro_extra_connector import (
    MacroExtraConnector,
)

# ============================================================
# 真实帧片段（2026-09-28 实测输出，新→旧，列名照抄）
# ============================================================

#: `ak.macro_china_pmi()`：224 行、5 列；此处取最近 3 期 + 干扰列（同比）
_PMI_FRAME = pd.DataFrame({
    "月份": ["2026年08月份", "2026年07月份", "2026年06月份"],
    "制造业-指数": [49.8, 49.2, 50.3],
    "制造业-同比增长": [0.809717, -0.202840, 1.207243],
    "非制造业-指数": [49.0, 49.0, 50.2],
    "非制造业-同比增长": [-2.584493, -2.195609, -0.594059],
})

#: `ak.macro_china_gdp()`：82 行、9 列；"第1-2季度" 是**年内累计**口径
_GDP_FRAME = pd.DataFrame({
    "季度": ["2026年第1-2季度", "2026年第1季度", "2025年第1-4季度",
             "2025年第1-3季度"],
    "国内生产总值-绝对值": [695704.0, 334192.9, 1401879.2, 1013967.9],
    "国内生产总值-同比增长": [4.7, 5.0, 5.0, 5.2],
    "第一产业-绝对值": [31521.8, 11940.8, 93346.8, 58187.1],
    "第一产业-同比增长": [3.7, 3.8, 3.9, 4.1],
})

#: `ak.macro_china_nbs_nation(...)`：**宽表**（index=指标行名、columns=期别），新→旧
_NBS_PMI_FRAME = pd.DataFrame(
    {"2026年8月": [49.8, 50.4], "2026年7月": [49.2, 49.9]},
    index=["制造业采购经理指数(%)", "生产指数(%)"],
)
_NBS_GDP_FRAME = pd.DataFrame(
    {"2026年第二季度": [695704.0], "2026年第一季度": [334192.9]},
    index=["国内生产总值_累计值(亿元)", "第一产业增加值_累计值(亿元)"],
)

_IDS = ("PMI", "PMI:制造业", "PMI:非制造业", "GDP", "GDP:同比")


class _FakeAkshare:
    """假 akshare 模块：只提供被测分支用到的接口（未提供的属性不存在 → AttributeError）。"""

    def __init__(self, **funcs: Any) -> None:
        self.__dict__.update(funcs)


@pytest.fixture(autouse=True)
def _no_backup_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """退避睡眠归零：否则每条"主源失败"的用例都要白睡 2+4+6=12 秒。

    只改**节奏**，不改重试**次数**与分支逻辑（次数由 `_NBS_RETRIES` 断言盯住）。
    """
    monkeypatch.setattr(mod, "_NBS_BACKOFF_SEC", 0.0)


def _patch_ak(monkeypatch: pytest.MonkeyPatch, **funcs: Any) -> None:
    fake = _FakeAkshare(**funcs)
    monkeypatch.setattr(mod, "_import_akshare", lambda: fake)


def _eastmoney_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """主源（东财口径）可用：PMI/GDP 各返回真实形状的帧。"""
    _patch_ak(
        monkeypatch,
        macro_china_pmi=lambda: _PMI_FRAME,
        macro_china_gdp=lambda: _GDP_FRAME,
        macro_china_nbs_nation=lambda **_: _NBS_PMI_FRAME,
    )


# ============================================================
# ① supports / capabilities
# ============================================================


def test_supports_accepts_declared_ids():
    for indicator in _IDS:
        assert MacroExtraConnector.supports(indicator) is True, indicator


@pytest.mark.parametrize("indicator", [
    "CPI", "PPI", "M2", "社融",                    # 同族宏观，已由 AkshareConnector 负责
    "stock_close:600036", "index_close:000300",    # 行情
    "PE(TTM):601088",
    "GDP同比", "pmi", "gdp", "PMI:制造业:同比", "PMI:综合",
    "ind:社会消费品零售总额同比", "", "PMI:",
])
def test_supports_rejects_unrelated_ids(indicator: str):
    """不认无关 id（尤其 `PMI:` 这种裸前缀 —— 认了就会吞掉未来所有 `PMI:xxx`）。"""
    assert MacroExtraConnector.supports(indicator) is False


def test_capabilities_and_supports_agree():
    """①↔①：能力清单里声明的 id 必须真的被 `supports()` 接受（否则描述是谎言）。"""
    caps = MacroExtraConnector().get_capabilities()
    assert caps["simulated"] is False
    assert set(caps["indicators"]) == set(_IDS)
    for indicator in caps["indicators"]:
        assert MacroExtraConnector.supports(indicator) is True
        # 口径必须写进 notes（人话），供运维判断"这个数字是什么"
    assert "累计" in caps["notes"] and "NBS" in caps["notes"]


def test_capabilities_use_exact_ids_not_template_prefixes():
    """能力清单里**不放** `{...}` 模板前缀。

    带占位符的条目会被 `test_contract_consistency` 当成"连接器声明的模板前缀"，
    进而要求 `indicators.yaml` 登记（那是登记侧的职责，不是本连接器的）。
    这里把该约束钉住：本连接器只声明精确 id。
    """
    caps = MacroExtraConnector().get_capabilities()
    assert not [i for i in caps["indicators"] if "{" in str(i)]


# ============================================================
# ② 数据点形状（真实契约：schema.DataPoint + 溯源 12 字段）
# ============================================================


async def test_points_are_ascending(monkeypatch: pytest.MonkeyPatch):
    """★ 升序：东财帧是**新→旧**，原样透出就是"降序连接器"（实测踩过的坑）。

    走网络降序、命中本地 DB 短路升序 → 任何依赖顺序的消费方时好时坏。
    """
    _eastmoney_ok(monkeypatch)
    points = await MacroExtraConnector().fetch("PMI")
    periods = [p.period_date for p in points]
    assert periods == sorted(periods), f"返回顺序不是升序: {periods}"
    assert periods == ["2026-06-30", "2026-07-31", "2026-08-31"]
    assert [p.value for p in points] == [50.3, 49.2, 49.8]


async def test_point_shape_is_traceable(monkeypatch: pytest.MonkeyPatch):
    """每个点都要有：时间、数值、单位、口径、可追溯来源。"""
    _eastmoney_ok(monkeypatch)
    points = await MacroExtraConnector().fetch("PMI")
    assert points, "未取到任何数据点"
    for point in points:
        assert point.indicator == "PMI"
        assert point.value is not None and point.value > 0   # 不是 0 / 不是占位值
        # 期末日：YYYY-MM-DD（月末日 = 官方发布日；月首会多算 30 天数据年龄）
        assert point.period_date and len(point.period_date) == 10
        assert point.period_date.endswith(("28", "29", "30", "31"))
        assert point.unit == "指数"
        assert point.source_type is DataSourceType.API
        assert point.fetch_method is FetchMethod.API_CALL
        assert 0 < point.confidence <= 1
        assert point.data_id and point.raw_content_hash and point.fetch_time
        assert point.processed_by == "A01_data_collector"
        # 来源可追溯：URL 非空 + 口径人话在 source_name 上
        # （Agent 的 `_format_with_fresh` 只拼 source_name，不展开 extra）
        assert point.source_url.startswith("https://")
        assert "制造业PMI" in point.source_name
        assert point.extra["unit"] == "指数"
        assert point.extra["frequency"] == "monthly"
        assert point.extra["origin"] == "eastmoney"
        assert point.extra["simulated"] is False
        assert "50=荣枯线" in point.extra["basis"]


async def test_gdp_points_carry_cumulative_basis(monkeypatch: pytest.MonkeyPatch):
    """GDP 是**累计**口径 —— 单位与累计标记必须随数据一起下发。"""
    _eastmoney_ok(monkeypatch)

    total = await MacroExtraConnector().fetch("GDP")
    yoy = await MacroExtraConnector().fetch("GDP:同比")
    assert [p.value for p in total] == [1013967.9, 1401879.2, 334192.9, 695704.0]
    assert all(p.unit == "亿元" and p.extra["cumulative"] is True for p in total)
    assert all(p.unit == "%" and p.extra["frequency"] == "quarterly" for p in yoy)
    assert [p.period_date for p in yoy] == [
        "2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30"]
    assert yoy[-1].value == 4.7
    # GDP 发布在**次月中旬**（比期末日晚约两周）—— 必须随数据披露，不许当成"可获取时点"
    assert "次月中旬" in yoy[-1].extra["period_basis"]


async def test_non_manufacturing_uses_its_own_column(monkeypatch: pytest.MonkeyPatch):
    """`PMI:非制造业` 取非制造业列（49.0/49.0/50.2），不能串到制造业列。"""
    _eastmoney_ok(monkeypatch)
    points = await MacroExtraConnector().fetch("PMI:非制造业")
    assert [p.value for p in points] == [50.2, 49.0, 49.0]
    assert "非制造业" in points[0].source_name
    manu = await MacroExtraConnector().fetch("PMI:制造业")
    assert [p.value for p in manu] == [50.3, 49.2, 49.8]
    assert manu[-1].value != points[-1].value  # 两个口径不能同值（49.8 vs 49.0）


# ============================================================
# ③ 季度标签：不许塌陷（`period_to_iso` 的实测坑）
# ============================================================


def test_quarter_labels_do_not_collapse():
    """★ `2026年第1-2季度` 与 `2026年第1季度` 必须映射到**不同**期别。

    为什么不复用 `akshare_connector.period_to_iso`：它的正则
    `(\\d{4})\\D{0,2}(\\d{1,2})` 会把两者都解析成 `2026-01`（实测复现），
    四个季度塌成一个期别、互相覆盖 —— 而下游"取最新一条"会拿到错的那条。
    """
    got = {
        "2026年第1-2季度": mod._quarter_to_period("2026年第1-2季度"),
        "2026年第1季度": mod._quarter_to_period("2026年第1季度"),
        "2025年第1-3季度": mod._quarter_to_period("2025年第1-3季度"),
        "2025年第1-4季度": mod._quarter_to_period("2025年第1-4季度"),
    }
    assert got == {
        "2026年第1-2季度": "2026-06-30",
        "2026年第1季度": "2026-03-31",
        "2025年第1-3季度": "2025-09-30",
        "2025年第1-4季度": "2025-12-31",
    }
    # 塌陷的判据：四个期别必须互不相同
    assert len(set(got.values())) == 4
    assert not any(v.startswith("2026-01") for v in got.values())


def test_period_is_month_end_not_month_start():
    """★ 期末日标注：`2026年08月份` → `2026-08-31`（月末 = 官方PMI发布日）。

    为什么不是月首（实测账，today=2026-09-29）：
      月首 `2026-08-01` → 59 天 → conf 0.374 → **stale（权重 0.3）**；
      期末 `2026-08-31` → 29 天 → conf 0.617 → lagging（权重 0.7）。
    月首还**早于**发布日（月末披露）—— 等于让回测偷看 30 天。
    """
    assert mod._month_to_period("2026年08月份") == "2026-08-31"
    assert mod._month_to_period("2026年2月") == "2026-02-28"
    assert mod._month_to_period("2024年2月") == "2024-02-29"      # 闰年（标准库算）
    assert mod._month_to_period("2026年4月") == "2026-04-30"
    assert mod._quarter_to_period("2025年第1-4季度") == "2025-12-31"


def test_quarter_parser_handles_nbs_chinese_numerals_and_garbage():
    """NBS 季度标签用中文数字（`2026年第二季度`）；解析不出必须返回 None。"""
    assert mod._quarter_to_period("2026年第二季度") == "2026-06-30"
    assert mod._quarter_to_period("2026年第四季度") == "2026-12-31"
    assert mod._quarter_to_period("2026年8月") is None
    assert mod._quarter_to_period("") is None
    assert mod._month_to_period("2026年08月份") == "2026-08-31"
    assert mod._month_to_period("2026年13月") is None
    assert mod._month_to_period("2026年0月") is None


async def test_all_gdp_rows_map_to_distinct_periods(monkeypatch: pytest.MonkeyPatch):
    """真实 82 行必须映射成 82 个互不冲突的期别（塌陷就会少）。"""
    _eastmoney_ok(monkeypatch)
    points = await MacroExtraConnector().fetch("GDP")
    periods = [p.period_date for p in points]
    assert len(periods) == len(set(periods)) == len(_GDP_FRAME)


# ============================================================
# ④ 列名漂移 / 越界值：宁缺勿错
# ============================================================


async def test_missing_column_raises_instead_of_sibling(monkeypatch: pytest.MonkeyPatch):
    """缺 `制造业-指数` 时报错，**不许**退到子串命中的 `非制造业-指数`。

    `制造业-指数` 是 `非制造业-指数` 的子串 —— 用子串匹配就会静默返回另一个口径
    （同 `coal_inventory_connector` 记录的"锚点缩短即抓错列"）。
    """
    drift = pd.DataFrame({
        "月份": ["2026年08月份"],
        "制造业PMI指数": [49.8],          # 改名了
        "非制造业-指数": [49.0],
    })
    _patch_ak(monkeypatch, macro_china_pmi=lambda: drift)
    with pytest.raises(DataFetchError) as excinfo:
        await MacroExtraConnector().fetch("PMI")
    assert "结构异常" in str(excinfo.value)
    assert "非制造业-指数" in str(excinfo.value)   # 报错要带实际列名，便于定位


async def test_missing_date_column_raises(monkeypatch: pytest.MonkeyPatch):
    _patch_ak(monkeypatch, macro_china_pmi=lambda: pd.DataFrame(
        {"报告期": ["2026年08月份"], "制造业-指数": [49.8]}))
    with pytest.raises(DataFetchError) as excinfo:
        await MacroExtraConnector().fetch("PMI")
    assert "月份" in str(excinfo.value)


async def test_out_of_range_rows_are_not_returned(monkeypatch: pytest.MonkeyPatch):
    """整列量级不对（= 抓到了"同比"列，±4）→ 全部弃用 → 抛错，而不是当成 PMI 值。"""
    wrong = pd.DataFrame({
        "月份": ["2026年08月份", "2026年07月份"],
        "制造业-指数": [0.809717, -0.202840],   # 这是"制造业-同比增长"
    })
    _patch_ak(monkeypatch, macro_china_pmi=lambda: wrong)
    with pytest.raises(DataFetchError) as excinfo:
        await MacroExtraConnector().fetch("PMI")
    assert "未产出可用行" in str(excinfo.value)


async def test_nan_rows_are_dropped_not_zeroed(monkeypatch: pytest.MonkeyPatch):
    """NaN 行必须**消失**，绝不能变成 0（"没量到" ≠ "量到 0"）。"""
    with_nan = pd.DataFrame({
        "月份": ["2026年08月份", "2026年07月份"],
        "制造业-指数": [49.8, None],
    })
    _patch_ak(monkeypatch, macro_china_pmi=lambda: with_nan)
    points = await MacroExtraConnector().fetch("PMI")
    assert [p.period_date for p in points] == ["2026-08-31"]
    assert all(p.value != 0 for p in points)


async def test_empty_frame_raises(monkeypatch: pytest.MonkeyPatch):
    _patch_ak(monkeypatch, macro_china_pmi=lambda: pd.DataFrame())
    with pytest.raises(DataFetchError) as excinfo:
        await MacroExtraConnector().fetch("PMI")
    assert "空帧" in str(excinfo.value)


# ============================================================
# ⑤ 失败不许伪造数据（本文件最硬的一条）
# ============================================================


async def test_primary_and_backup_failure_raises_and_returns_no_points(
    monkeypatch: pytest.MonkeyPatch,
):
    """主源与备源**都**失败 → 抛 `DataFetchError`，绝不返回空壳点 / 0 / 占位值。

    这是 AGENTS.md「『没量到』≠『量到 0』」的机器复现：把 fetch 失败
    monkeypatch 成抛异常后，唯一合法的行为是**报错**（由路由层记录缺口），
    而不是给下游一个 0。
    """
    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("network down")

    nbs_calls: list[int] = []

    def nbs_boom(**_kwargs: Any) -> Any:
        nbs_calls.append(1)
        raise RuntimeError("nbs blocked")

    _patch_ak(monkeypatch, macro_china_pmi=boom, macro_china_nbs_nation=nbs_boom)

    with pytest.raises(DataFetchError) as excinfo:
        await MacroExtraConnector().fetch("PMI")
    message = str(excinfo.value)
    assert "NBS" in message and "主源失败原因" in message
    # 备源**确实被尝试过**（不是直接放弃），且重试了 _NBS_RETRIES 次
    assert len(nbs_calls) == mod._NBS_RETRIES


async def test_primary_failure_uses_nbs_backup_with_disclosure(
    monkeypatch: pytest.MonkeyPatch,
):
    """主源挂 → 走 NBS 备源，并**在点上披露**这次是备源、主源错在哪。"""
    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("eastmoney blocked")

    calls: list[str] = []

    def nbs(**kwargs: Any) -> Any:
        calls.append(str(kwargs.get("path")))
        return _NBS_PMI_FRAME

    _patch_ak(monkeypatch, macro_china_pmi=boom, macro_china_nbs_nation=nbs)

    points = await MacroExtraConnector().fetch("PMI")
    assert calls == ["采购经理指数 > 制造业采购经理指数"]
    assert [p.period_date for p in points] == ["2026-07-31", "2026-08-31"]   # 备源也升序
    assert [p.value for p in points] == [49.2, 49.8]
    point = points[-1]
    assert point.extra["origin"] == "nbs"
    assert point.extra["fallback_source"] == "国家统计局NBS(AkShare)"
    assert "eastmoney blocked" in point.extra["primary_error"]
    assert point.source_name.endswith("(国家统计局NBS/AkShare)")
    assert point.source_url.startswith("https://data.stats.gov.cn")
    assert point.confidence == 0.8      # 备源置信度低于主源（0.85）


async def test_backup_wide_frame_is_parsed_by_row_name(monkeypatch: pytest.MonkeyPatch):
    """NBS 帧是**宽表**：行名选错就会拿到另一个指标（如"生产指数"）。"""
    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("eastmoney blocked")

    _patch_ak(monkeypatch, macro_china_pmi=boom,
              macro_china_nbs_nation=lambda **_: _NBS_PMI_FRAME)
    points = await MacroExtraConnector().fetch("PMI")
    assert [p.value for p in points] == [49.2, 49.8]     # 不是生产指数 49.9/50.4


async def test_eastmoney_ok_never_touches_the_backup(monkeypatch: pytest.MonkeyPatch):
    """主源可用时**不许**多打一次备源（NBS 有 WAF 限流，白打会把自己打封）。"""
    def nbs_must_not_run(**_kwargs: Any) -> Any:
        raise AssertionError("主源可用时不应调用 NBS 备源")

    _patch_ak(monkeypatch, macro_china_pmi=lambda: _PMI_FRAME,
              macro_china_nbs_nation=nbs_must_not_run)
    points = await MacroExtraConnector().fetch("PMI")
    assert points and all(p.extra["origin"] == "eastmoney" for p in points)


async def test_gdp_yoy_has_no_cross_basis_fallback(monkeypatch: pytest.MonkeyPatch):
    """★ `GDP:同比` 主源失败时**必须报错**，不许接"单季同比"来凑。

    实测：金十 `macro_china_gdp_yearly` 是**单季同比**（2024-12 = 5.4，而累计同比
    5.0），与东财的**累计同比**是两个口径。混进同一条序列 = 数字看着有据、
    语义是错的 —— 比"缺数据"更危险（AGENTS.md 的 `fed:policy_range` 教训）。
    """
    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("eastmoney blocked")

    def nbs_must_not_run(**_kwargs: Any) -> Any:
        raise AssertionError("GDP:同比 没有同口径备源，不该调用 NBS")

    _patch_ak(monkeypatch, macro_china_gdp=boom, macro_china_nbs_nation=nbs_must_not_run)
    with pytest.raises(DataFetchError) as excinfo:
        await MacroExtraConnector().fetch("GDP:同比")
    assert "没有同口径备源" in str(excinfo.value)


async def test_unsupported_indicator_raises(monkeypatch: pytest.MonkeyPatch):
    """不支持的指标直接报错（不静默返回空列表 —— 那会被当成"取到了 0 条"）。"""
    _eastmoney_ok(monkeypatch)
    for indicator in ("CPI", "stock_close:600036", "PMI:"):
        with pytest.raises(DataFetchError):
            await MacroExtraConnector().fetch(indicator)


async def test_backup_call_shape_error_fails_fast_without_retrying(
    monkeypatch: pytest.MonkeyPatch,
):
    """★ 备源的**调用形状**错误（akshare 改名/形参变了）不许退避重试。

    重试只会白睡 2+4+6=12 秒，然后报**同一个**错 —— 取数路径上的每一秒都是
    用户的等待。只有"WAF 抖动/网络"这类**可能自愈**的错误才值得重试。
    """
    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("eastmoney blocked")

    calls: list[int] = []

    def nbs_type_error(**_kwargs: Any) -> Any:
        calls.append(1)
        raise TypeError("macro_china_nbs_nation() got an unexpected keyword 'period'")

    _patch_ak(monkeypatch, macro_china_pmi=boom, macro_china_nbs_nation=nbs_type_error)
    with pytest.raises(DataFetchError) as excinfo:
        await MacroExtraConnector().fetch("PMI")
    assert calls == [1], "形状错误被重试了（会白等 12 秒）"
    assert "调用方式失效" in str(excinfo.value)



# ============================================================
# ⑥ 区间过滤
# ============================================================


async def test_range_filter_is_inclusive_and_never_raises_on_empty_window(
    monkeypatch: pytest.MonkeyPatch,
):
    """区间内确实没有数据 → 返回**空列表**（这是合法结果，不是源故障）。

    与"源本身取不到数"必须区分：后者抛错（上面的用例），前者返回空。
    """
    _eastmoney_ok(monkeypatch)
    conn = MacroExtraConnector()
    got = await conn.fetch("PMI", start_date="2026-06-01", end_date="2026-07-31")
    assert [p.period_date for p in got] == ["2026-06-30", "2026-07-31"]
    got = await conn.fetch("PMI", start_date="20260701")
    assert [p.period_date for p in got] == ["2026-07-31", "2026-08-31"]
    assert await conn.fetch("PMI", start_date="1990-01-01",
                            end_date="1990-12-31") == []


def test_in_range_month_granularity():
    """期别是**期末日**，但区间判定按**整月**包含。

    按日比较会让"查到 2026-08-15 为止"这种请求把 2026-08-31 那期漏掉 ——
    用户看到的是"最新一期没了"。
    """
    assert mod._in_range("2026-08-31", "2026-08-31", None) is True
    assert mod._in_range("2026-08-31", None, "2026-08-01") is True   # 整月包含
    assert mod._in_range("2026-08-31", "2026-08-01", "2026-08-15") is True
    assert mod._in_range("2026-07-31", "2026-08-01", None) is False
    assert mod._in_range("2026-09-30", None, "2026-08-31") is False
    assert mod._in_range("2026-08-31", None, None) is True


# ============================================================
# ⑦ 支持面自证：连接器要能在路由里被枚举到
# ============================================================


def test_connector_is_a_base_connector_with_stable_identity():
    conn = MacroExtraConnector()
    assert conn.source_name and conn.source_url.startswith("https://")
    # 路由的失败冷却键是 f"{source_name}:{indicator}" —— 名字必须非空且稳定
    assert isinstance(conn.source_name, str)


def test_module_exposes_no_second_connector_class():
    """一个模块一个连接器（枚举面按 `obj.__module__ == 模块名` 判定，多开会互相污染）。"""
    from src.infrastructure.connectors.base import BaseConnector

    found = [
        name for name, obj in vars(mod).items()
        if isinstance(obj, type) and issubclass(obj, BaseConnector)
        and obj is not BaseConnector and obj.__module__ == mod.__name__
    ]
    assert found == ["MacroExtraConnector"]


@pytest.mark.parametrize("indicator", _IDS)
async def test_every_supported_id_is_actually_fetchable(
    monkeypatch: pytest.MonkeyPatch, indicator: str,
):
    """★ `supports()` 认了就必须真的取得到（否则就是"能力描述是谎言"）。

    这是本连接器存在的理由：报障的形态正是"白名单/目录/别名三处都认它，
    采集侧 `supports()` 却是 False"。
    """
    _eastmoney_ok(monkeypatch)
    points = await MacroExtraConnector().fetch(indicator)
    assert points, f"{indicator} supports() 为真但取不到数据"
    assert all(p.indicator == indicator for p in points)


def test_patch_helper_really_replaces_akshare(monkeypatch: pytest.MonkeyPatch):
    """自证：`_patch_ak` 必须真的把 `_import_akshare` 换掉。

    没有这条，打桩哪天退化（比如函数改名），下面所有用例会去打**真实网络**
    —— 在 CI 上变成网络敏感用例，而且**依然通过**（假绿）。
    """
    _eastmoney_ok(monkeypatch)
    fake = mod._import_akshare()
    assert fake.macro_china_pmi() is _PMI_FRAME
    assert fake.macro_china_gdp() is _GDP_FRAME


# ============================================================
# ⑧ 贯通点：登记频率 ↔ 新鲜度档位（同一判断不许有两份实现）
# ============================================================

_ROOT = Path(__file__).resolve().parents[2]
#: 登记表的 `frequency` → `DataFreshnessEvaluator` 的发布周期（天）
_CYCLE_BY_FREQUENCY = {"daily": 1, "weekly": 7, "monthly": 30, "quarterly": 90,
                       "yearly": 365}


def _registered_frequencies() -> dict[str, str]:
    """从 `configs/indicators.yaml` 现读 `id → frequency`（不硬编码）。"""
    raw = yaml.safe_load(
        (_ROOT / "configs" / "indicators.yaml").read_text(encoding="utf-8"))
    out: dict[str, str] = {}

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            if isinstance(node.get("id"), str) and "frequency" in node:
                out[str(node["id"])] = str(node["frequency"])
            for value in node.values():
                walk(value)

    walk(raw)
    return out


def _freshness_cycle(indicator: str) -> int:
    """评估器给这个指标算的发布周期（天）—— 现读，不硬编码。"""
    from src.core.data_freshness import DataFreshnessEvaluator

    return DataFreshnessEvaluator().evaluate(
        indicator, "2026-01-31").publish_cycle_days


@pytest.mark.parametrize("indicator", _IDS)
def test_freshness_tier_matches_registered_frequency(indicator: str):
    """★ 登记表说 monthly/quarterly，`DataFreshnessEvaluator` 就必须按同一个周期算。

    ## 为什么这条必须有（本轮实测出来的真缺口）

    `GDP` 按 **quarterly** 登记，但 `data_freshness._FREQ_BY_PREFIX` 里没有 `GDP`
    → 落到 `_DEFAULT_FREQ`(30, 90, 365) 被当成**月频**。实测后果（today=2026-09-29、
    期别 2026-06-30）：conf 0.219 → `router._is_db_fresh`（≥0.4）判**不新鲜 →
    每次查询都穿透去联网**，而库里那份 H1 数据正是当季最新的。

    这正是 AGENTS.md 说的"同一个 key 写在 N 处，必有一处被漏改" ——
    登记侧的 `frequency` 与新鲜度侧的档位是**同一个判断的两份实现**，
    所以这里用"现读登记表 + 现读评估器"把它们钉在一起（不是硬编码 90）。
    """
    registered = _registered_frequencies()
    assert indicator in registered, (
        f"{indicator} 未登记进 configs/indicators.yaml —— 取到了也无处引用"
        "（SmartFetcher 判未登记 → 每次联网、保留策略管不到）")
    expected = _CYCLE_BY_FREQUENCY[registered[indicator]]
    assert _freshness_cycle(indicator) == expected, (
        f"{indicator} 登记为 {registered[indicator]}（周期 {expected} 天），"
        f"而新鲜度评估器按 {_freshness_cycle(indicator)} 天算 —— "
        "两处口径不一致会让数据被误判成 stale（并让路由每次白跑网络）")
