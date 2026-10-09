"""`PlatformDataConnector`（平台自有后端数据，八族）护栏。

## 本文件钉住的是什么（每条都对应一个**已经发生或极易发生**的失效）

| 判据 | 防的失效 |
|---|---|
| `supports()` 认/不认 | 裸名、别的族的指标（`PE(TTM):600036`）被误认 |
| capabilities 占位符按族给 | 行业三族被声明成 `{code}` → 护栏在**假前提**下变绿 |
| 行业族收 6 位代码**抛错** | 契约违规被静默读成"平台没有这个概念板块" |
| 档位/标签来自 `valuation.py` | 有人在连接器里**另抄一份阈值** → 界面与 Agent 同一天给出不同结论 |
| 分位算法自证（[1..99]+[90] → 90%） | 只断言"有值"的假绿 |
| `extra.basis` 含样本区间与样本数 | 用户把"近三年分位"当成"全历史分位" |
| 库路径指向不存在时**抛错/空** | 用 `0` 填充（"没量到"被读成"量到 0"） |
| `解禁计划` 三态分开 | 把"日历没同步"与"该股无解禁"合并成同一个空列表 |
| 行业族"找不到"不提示 | 用户口径被违反；或反过来把**契约违规**也放行 |
| `has_related_board` 异常时**保守放行** | 把存储层真缺口藏掉 |
| 白名单**精确命中数** | 0 命中走 fallback（返回前 200 条）的假绿 |

真实取数用例都带 `skipif`（本机没有那份数据时不静默变绿）。
"""
from __future__ import annotations

import asyncio
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from src.core.exceptions import DataFetchError
from src.core.schemas import DataPoint
from src.infrastructure.connectors import platform_data_connector as pdc
from src.infrastructure.connectors.platform_data_connector import (
    PlatformDataConnector,
    reset_cache,
)
from src.intraday.valuation import (
    HEADROOM_LABELS,
    compute_headroom_score,
    headroom_bucket,
    valuation_from_series,
)

#: 真实存在的 6 位 A 股代码（招商银行，本机行情仓/概念映射都有它）
CODE = "600036"
#: 个股五族 + 行业三族
STOCK_FAMILIES = ("估值水位", "概念拥挤度", "主线告警", "个股告警", "解禁计划")
INDUSTRY_FAMILIES = ("行业拥挤度", "板块资金流", "行业轮动")


@pytest.fixture(autouse=True)
def _clean_cache():
    """每个用例都从**空缓存**开始 —— 否则用例之间会互相喂缓存（假绿）。"""
    reset_cache()
    yield
    reset_cache()


def _fetch(conn: PlatformDataConnector, indicator: str, **kw: Any) -> list[DataPoint]:
    return asyncio.run(conn.fetch(indicator, **kw))


def _diagnose(conn: PlatformDataConnector, indicator: str, **kw: Any) -> Any:
    return asyncio.run(conn.diagnose(indicator, **kw))


def _stores_ready(*tables: str) -> bool:
    """这些逻辑表在本机能不能解析到（不能就 skip，不静默变绿）。"""
    for table in tables:
        try:
            pdc._table_store(table)          # noqa: SLF001 判据就是"能不能解析"
        except Exception:                    # noqa: BLE001
            return False
    return True


def _crowding_ready() -> bool:
    return _stores_ready("sector_crowding_daily", "ml_member_corr")


def _unlock_ready() -> bool:
    return _stores_ready("fact_data_points")


def _rotation_ready() -> bool:
    """行业轮动族**不读库**（读 `data/sector_rotation/report_*.json`），所以判据是"有没有报告"。

    ⚠️ 第一版写成 `_stores_ready("mainline_cache")` —— 那是个**存储名**不是表名，
    `_table_store` 必然抛错 → 用例被**误 skip**（"判据没跑满"的假绿）。
    """
    try:
        from src.sector_rotation import store as rotation_store

        return rotation_store.latest_date() is not None
    except Exception:                                        # noqa: BLE001
        return False


# ============================================================================
# 一、契约：supports / capabilities / 占位符
# ============================================================================


@pytest.mark.parametrize("family", STOCK_FAMILIES)
def test_supports_accepts_stock_families(family: str) -> None:
    assert PlatformDataConnector.supports(f"{family}:{CODE}") is True


@pytest.mark.parametrize("family", INDUSTRY_FAMILIES)
def test_supports_accepts_industry_families(family: str) -> None:
    assert PlatformDataConnector.supports(f"{family}:银行") is True


@pytest.mark.parametrize("indicator", [
    f"PE(TTM):{CODE}", f"stock_close:{CODE}", f"商誉占净资产比:{CODE}",
    "CPI", "PB", "fed:effr",
])
def test_supports_rejects_other_families(indicator: str) -> None:
    """**别的族的指标不许被认下** —— 认下就等于抢了别人的路由。"""
    assert PlatformDataConnector.supports(indicator) is False


@pytest.mark.parametrize("bare", STOCK_FAMILIES + INDUSTRY_FAMILIES)
def test_supports_rejects_bare_names(bare: str) -> None:
    """裸名（无冒号）**不许"能取"** —— 没有任何连接器 supports 裸名。"""
    assert PlatformDataConnector.supports(bare) is False


def test_capabilities_placeholders_match_family_kind() -> None:
    """★ capabilities 的占位符必须**按族给真实值**（`{code}` vs `{行业名}`）。

    全部写成 `{code}` 会让 `test_contract_consistency` 的
    `_declared_code_prefixes()` 把行业三族当成"个股类模板根"通过 ③→①，
    而 `fetch("行业拥挤度:600036")` 根本不认 6 位代码 —— **护栏在假前提下变绿**。
    """
    caps = PlatformDataConnector().get_capabilities()
    declared = {str(x) for x in caps["indicators"]}
    assert declared == {
        *[f"{f}:{{code}}" for f in STOCK_FAMILIES],
        *[f"{f}:{{行业名}}" for f in INDUSTRY_FAMILIES],
    }
    # 声明与 supports 一致：占位符换成采样值后必须真被 supports 接受
    assert PlatformDataConnector.supports(f"行业拥挤度:{'半导体'}") is True
    assert PlatformDataConnector.supports(f"估值水位:{CODE}") is True


@pytest.mark.parametrize("indicator", [
    f"行业拥挤度:{CODE}", f"板块资金流:{CODE}", f"行业轮动:{CODE}",
    "行业拥挤度:", "行业轮动:ind:sw_third_pe_ttm:all",
])
def test_industry_family_rejects_non_industry_suffix(indicator: str) -> None:
    """★ 行业族拿到 **6 位代码 / 空 / 含冒号** 的后缀要**抛契约错误**。

    静默返回 `[]` 会把"契约违规"读成"平台没有这个概念板块" ——
    前者要修调用方、后者是正常结论（用户口径：不提示）。两件事的下一步动作完全不同。
    """
    with pytest.raises(DataFetchError) as excinfo:
        _fetch(PlatformDataConnector(), indicator)
    message = str(excinfo.value)
    assert "行业名" in message, message
    assert "取值模板" in message, message


# ============================================================================
# 二、估值水位：判据只有一份实现 + 两个口径 + 样本随数据下发
# ============================================================================


def test_percentile_math_is_self_proven() -> None:
    """★ 分位算法**自证**：喂已知答案 —— `[1..99] + [90]` 里 90 的分位 = 90。

    ⚠️ 断言的是**具体数字**，不是"有值"（后者是典型假绿）。
    用的是估值链自己的纯函数 `valuation_from_series`（连接器调的就是它）。

    构造（100 个样本、当前值 90、恰有 90 个 ≤ 90 ⇒ 分位 = 90.0）：
    `[1..89] + [91..100] + [90]` —— 注意**不能**直接写 `[1..99] + [90]`：
    那样 90 会被算两次 → 91.0（本用例第一版就是这么错的，被这条断言抓到）。
    """
    series = ([float(v) for v in range(1, 90)]
              + [float(v) for v in range(91, 101)]
              + [90.0])
    stats = valuation_from_series([_Point(v) for v in series], [])
    assert stats["pe_days"] == 100
    assert stats["pe_percentile"] == pytest.approx(90.0)
    assert stats["pe_current"] == 90.0


class _Point:
    """`valuation_from_series` 只读 `value`（它按 `getattr(p,'value')` 取）。"""

    def __init__(self, value: float) -> None:
        self.value = value


class _StubBackend:
    """替身（管**输入数据**，不验交互）：按需返回指定 PE/PB 序列。"""

    def __init__(self, pe: list[float], pb: list[float]) -> None:
        self._pe = pe
        self._pb = pb
        self.calls: list[str] = []

    async def fetch(self, indicator: str, start_date: str | None = None,
                    end_date: str | None = None, **kw: Any) -> list[DataPoint]:
        self.calls.append(indicator)
        prefix, _, _code = indicator.partition(":")
        values = self._pe if prefix == "PE(TTM)" else self._pb if prefix == "PB" else []
        return [DataPoint(indicator=indicator, value=float(v),
                          period_date=f"2026-01-{i + 1:02d}")
                for i, v in enumerate(values)]


def _series_for_percentile(p: int) -> list[float]:
    """构造一条"当前值恰好在第 p 百分位"的序列（长度 100）。"""
    return [float(v) for v in range(1, 100)] + [float(p)]


@pytest.mark.parametrize(("percentile", "expected"), [
    (20, "ample"), (40, "moderate"), (55, "stretched"), (90, "expensive"),
])
def test_valuation_label_comes_from_the_single_source(
    percentile: int, expected: str,
) -> None:
    """★★ 档位与标签**必须**来自 `src/intraday/valuation.py`（五档行为用例）。

    谁在连接器里另抄一份阈值表，这条会红（四个档位分别落到不同分支）。
    """
    backend = _StubBackend(_series_for_percentile(percentile),
                           _series_for_percentile(percentile))
    conn = PlatformDataConnector(backend=backend)
    points = _fetch(conn, f"估值水位:{CODE}")
    assert len(points) == 1, "平台路径应当产出 1 个点"
    extra = points[0].extra
    # 独立复算（用同一份权威判据）：分位均值 → 合成分 → 档位
    score, _notes = compute_headroom_score(
        pe_percentile=extra["pe_percentile"], pb_percentile=extra["pb_percentile"],
        pe=extra["pe_ttm"], pb=extra["pb"], peer_pe_median=None,
        peer_pb_median=None, industry_pe_median=None)
    assert extra["headroom"] == headroom_bucket(score) == expected
    assert extra["headroom"] in HEADROOM_LABELS
    assert extra["headroom_label"] == HEADROOM_LABELS[extra["headroom"]]
    assert extra["valuation_path"] == "valuation_provider"
    assert points[0].value == pytest.approx(score)
    assert backend.calls == [f"PE(TTM):{CODE}", f"PB:{CODE}"], (
        "估值序列必须走**既有采集链**（ConnectorRouter → PE(TTM)/PB）")


def test_valuation_insufficient_samples_is_labeled_not_zero() -> None:
    """样本不足 → 按平台口径给「数据不足」，**不是** 0 分、也不是"估值透支"。"""
    backend = _StubBackend([1.0, 2.0, 3.0], [1.0, 2.0, 3.0])
    points = _fetch(PlatformDataConnector(backend=backend), f"估值水位:{CODE}")
    assert len(points) == 1
    extra = points[0].extra
    assert extra["headroom"] == "unknown"
    assert extra["headroom_label"] == HEADROOM_LABELS["unknown"] == "数据不足"
    assert points[0].value is None
    assert "insufficient_reason" in extra


@pytest.mark.skipif(
    not Path(pdc._store_path("warehouse")).exists(),   # noqa: SLF001
    reason="本机没有共享行情仓")
def test_valuation_local_path_two_calibers_and_basis() -> None:
    """退化路径（无采集链）：PE/PB 当前值 + **两个口径**的分位 + 样本随 `basis` 下发。"""
    conn = PlatformDataConnector()          # 不注入 backend → 行情仓序列
    points = _fetch(conn, f"估值水位:{CODE}")
    assert len(points) == 1
    extra = points[0].extra
    assert extra["valuation_path"] == "local_warehouse_series"
    assert extra["pe_ttm"] is not None and extra["pb"] is not None
    assert extra["pe_percentile"] is not None and extra["pb_percentile"] is not None
    # ★ 两个口径：近三年（平台口径）与全历史，**都必须带样本数与区间**
    sample, full = extra["sample"], extra["full_history"]
    assert sample["window"] == "近三年"
    assert sample["pe_days"] >= 60 and sample["pe_start"] and sample["pe_end"]
    assert full["pe_days"] > sample["pe_days"]
    assert full["pe_percentile"] is not None
    assert "近三年" in extra["basis"] and str(sample["pe_days"]) in extra["basis"]
    assert str(full["pe_days"]) in extra["basis"], "全历史样本数也要写进 basis"
    # ★ 三件套**必须存在**（本路径恒为"未含对标"，但不许省略）
    assert extra["peer_source"] == "unavailable"
    assert extra["peer_pe_median"] is None
    assert extra["industry_pe_median"] is None


# ============================================================================
# 三、概念拥挤度：按**相关度**排序 + 缺水位的板块不填 0
# ============================================================================


@pytest.mark.skipif(not _crowding_ready(), reason="本机没有拥挤度/相关度表")
def test_concept_crowding_real_data() -> None:
    """真实取数：600036 至少 1 条，带 `water_level` 与最新交易日。"""
    points = _fetch(PlatformDataConnector(), f"概念拥挤度:{CODE}")
    assert len(points) == 1
    point = points[0]
    extra = point.extra
    assert point.indicator == f"概念拥挤度:{CODE}"
    assert 0.0 <= float(point.value) <= 1.0
    assert point.period_date and len(point.period_date) == 10
    assert extra["related_total"] >= 1
    assert extra["value_board"]["board_code"]
    assert extra["value_board"]["corr"] is not None
    assert "ml_member_corr" in extra["basis"]
    first = extra["boards"][0]
    assert first["water_level"] is not None
    assert first["water_level_measured"] is True
    assert "water_level" in extra["value_semantics"]


@pytest.mark.skipif(not _crowding_ready(), reason="本机没有拥挤度/相关度表")
def test_concept_crowding_is_sorted_by_relatedness() -> None:
    """★★ `boards` 必须按 **corr 降序**（用户口径：先相关、再看拥挤）。"""
    extra = _fetch(PlatformDataConnector(), f"概念拥挤度:{CODE}")[0].extra
    corrs = [b["corr"] for b in extra["boards"] if b.get("corr") is not None]
    assert corrs == sorted(corrs, reverse=True), f"未按相关度排序：{corrs}"
    assert extra["value_board"]["corr"] == corrs[0], (
        "`value` 必须取自**相关度最高**的那个板块（且该板块水位已量到）")


@pytest.mark.skipif(not _crowding_ready(), reason="本机没有拥挤度/相关度表")
def test_concept_crowding_keeps_boards_without_crowding() -> None:
    """★ 缺拥挤度的相关板块**不丢弃**（`no_crowding=True` 逐条标出）。

    悄悄丢掉它会吃掉"相关度第一但没有拥挤度"这条信息。
    """
    extra = _fetch(PlatformDataConnector(), f"概念拥挤度:{CODE}")[0].extra
    for board in extra["boards"]:
        if board.get("no_crowding"):
            assert board["no_crowding_reason"], "标了 no_crowding 就必须给原因"
        else:
            assert "water_level" in board


# ============================================================================
# 四、告警两族：空要能解释（空 ≠ 0）
# ============================================================================


def test_mainline_alert_empty_is_explainable() -> None:
    """窗口内没有告警时**不产点**（`[]`），且 `diagnose()` 说得出是"窗口外"还是"没量到"。"""
    conn = PlatformDataConnector()
    if not _crowding_ready() or not _stores_ready("mainline_alert"):
        pytest.skip("本机没有拥挤度/告警表")
    points = _fetch(conn, f"主线告警:{CODE}")
    diag = _diagnose(conn, f"主线告警:{CODE}")
    if points:
        assert diag.reason.startswith("能产点")
        assert all(p.extra["level"] in ("medium", "strong") for p in points)
    else:
        assert diag.reason and "能产点" not in diag.reason
        # 「窗口内确实没有」与「没量到」必须分得开
        assert ("窗口" in diag.reason) or ("相关板块" in diag.reason), diag.reason


def test_alerts_never_return_zero_filled_points() -> None:
    """告警两族**不许**返回 `value=0` 的填充点（0 是有效读数，会被读成"有告警且分数为 0"）。"""
    conn = PlatformDataConnector()
    for family in ("主线告警", "个股告警"):
        for point in _fetch(conn, f"{family}:{CODE}"):
            assert point.value != 0.0 or point.extra.get("state") == "measured_zero"


def test_stock_alert_hit_for_named_code() -> None:
    """`fact_alerts` 里**真被点名**的代码必须取得到（动态取样，不写死）。

    判据是"点名 vs 未点名"的**对照**：同一份数据里被点名的取得到，未被点名的为空。
    """
    if not _stores_ready("fact_alerts"):
        pytest.skip("本机没有 fact_alerts")
    store = pdc._table_store("fact_alerts")       # noqa: SLF001
    with pdc._readonly(store) as conn:            # noqa: SLF001
        rows = conn.execute(
            "SELECT affected_stocks_json FROM fact_alerts "
            "WHERE affected_stocks_json IS NOT NULL AND affected_stocks_json != ''"
        ).fetchall()
    named: list[str] = []
    for row in rows:
        payload = pdc._loads(row["affected_stocks_json"])   # noqa: SLF001
        for entry in payload or []:
            if isinstance(entry, dict) and pdc._digits_code(entry.get("code")):  # noqa: SLF001
                named.append(pdc._digits_code(entry["code"]))                    # noqa: SLF001
    if not named:
        pytest.skip("本机 fact_alerts 没有点名任何个股")

    target = named[0]
    points = _fetch(PlatformDataConnector(), f"个股告警:{target}")
    assert points, f"{target} 被 fact_alerts 点名，却取不到"
    extra = points[0].extra
    assert extra["alert_id"] and extra["value_field"]
    assert extra["impact"] is not None
    assert extra["matches_in_alert"], "必须带上「这条告警里点名的明细」"
    assert extra["matches_in_alert"][0]["code"] == target


# ============================================================================
# 五、解禁计划：三态**分别**断言
# ============================================================================


def test_unlock_three_states_are_distinct() -> None:
    """★ 解禁计划的**四态**分别断言（逐股表口径为主）。

    2026-09-29 口径变更（用户：解禁要落**单独的逐股表**、月频调度、索引点查）：

    * 态 1 命中 → 产点，`value` > 0，`unlock_source == "unlock_plan"`；
    * 态 2 窗口**在覆盖范围内**、表里没有这只票 → 产 `value=0.0`（量到 0）；
    * 态 3 查询窗口**超出**已落库覆盖范围 → `[]`（没量到），reason 明说"超出"；
    * 回退态：表一行都没有时走日历 JSON，且 **state 名不同**
      （`measured_zero_in_truncated_detail`）—— 因为那条路的明细**每天只留 top N**，
      它的"没有"不许当成"无解禁"。
    """
    from src.infrastructure.repositories import unlock_plan_repo

    if not unlock_plan_repo.calendar_ready():
        pytest.skip("本机 app_db::unlock_plan 还没落库（先跑 scripts/unlock_plan.py --ingest）")
    conn = PlatformDataConnector()
    window = unlock_plan_repo.window()
    today = pdc._today()                                          # noqa: SLF001

    # ---- 态 1：从**默认窗口内**挑一个真有解禁的代码 ----
    #   ⚠️ 必须限定在窗口内：表里最靠前的大额解禁可能在一年之后
    #      （第一版只加 `unlock_date>=today`，挑到 2027 年的行 ⇒ 默认窗口取不到 ⇒ 假红）。
    window_end = pdc._shift_days(today, pdc._UNLOCK_WINDOW_DAYS)  # noqa: SLF001
    with unlock_plan_repo.connect() as raw:
        row = raw.execute(
            f"SELECT code, unlock_date FROM {unlock_plan_repo.TABLE} "
            "WHERE unlock_date>=? AND unlock_date<=? "
            "ORDER BY market_cap DESC LIMIT 1",
            (today, window_end)).fetchone()
    if row is None:
        pytest.skip("默认窗口内没有解禁行")
    hit_points = _fetch(conn, f"解禁计划:{row['code']}")
    assert hit_points, f"{row['code']} 在表里有解禁却取不到点"
    assert hit_points[0].value > 0
    assert hit_points[0].extra["state"] == "measured_hit"
    assert hit_points[0].extra["unlock_source"] == "unlock_plan"
    assert hit_points[0].extra["entries"], "命中态必须带明细"
    assert hit_points[0].extra.get("shares_total"), "逐股表口径必须带解禁数量合计"

    # ---- 态 2：窗口在覆盖内、表里没有 600036 → 量到 0 ----
    zero_points = _fetch(conn, f"解禁计划:{CODE}")
    assert len(zero_points) == 1, "「量到 0」必须产点，不许变成空列表"
    assert zero_points[0].value == 0.0
    assert zero_points[0].extra["state"] == "measured_zero"
    assert zero_points[0].extra["covered_window"]["start"] == window["start"]
    assert "量到 0" in zero_points[0].extra["basis"]

    # ---- 态 3：窗口超出已落库覆盖范围 → 没量到 → 空列表 + 说明 ----
    empty = _fetch(conn, f"解禁计划:{CODE}",
                   start_date="2030-01-01", end_date="2030-01-31")
    assert empty == [], "没量到不许产点（更不许产 0）"
    diag = _diagnose(conn, f"解禁计划:{CODE}",
                     start_date="2030-01-01", end_date="2030-01-31")
    assert "没量到" in diag.reason and "超出" in diag.reason
    assert "无解禁" in diag.reason, "必须点明'不是无解禁'（否则会被读成结论）"


# ============================================================================
# 六、库不可用：抛错 / 空，**绝不是 0**
# ============================================================================


#: 走 `_store_path`（本连接器自己的 registry 解析）的族 —— 「库缺失」用例只对它们成立。
#:
#: ⚠️ `板块资金流` 走 `FundFlowProvider`（它自己解析 fundflow 缓存目录 + 行情仓），
#: `行业轮动` 走 `src.sector_rotation.store`（它自己解析 `data/sector_rotation`）——
#: 那两个族的路径**不经过本连接器**，所以"把 `_store_path` 指到不存在的文件"
#: 对它们无效。第一版把这两族也参数化进来，实测拿到的是**真实数据点**（用例错，不是代码错）。
#: ★ 2026-09-29：`解禁计划` 同理 —— 它改走 `app_db::unlock_plan`（逐股表），
#: 那条路经 `unlock_plan_repo` 自己解析存储，`_store_path` 也管不到它。
#: 它的"库缺失"判据见 `test_unlock_table_missing_falls_back_never_zero`。
_REGISTRY_PATH_FAMILIES = tuple(
    f for f in (STOCK_FAMILIES + ("行业拥挤度",)) if f != "解禁计划")


@pytest.mark.parametrize("family", _REGISTRY_PATH_FAMILIES)
def test_missing_store_never_yields_a_zero_filled_point(
    family: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """★★ 把存储路径指到不存在的位置：**要么抛错、要么空列表，绝不许返回 0 点**。

    这是本项目最贵的一类假绿（"没量到"被读成"量到 0"）。
    """
    missing = tmp_path / "nope.db"

    def _broken(_name: str) -> Path:
        return missing

    monkeypatch.setattr(pdc, "_store_path", _broken)
    reset_cache()
    suffix = CODE if family in STOCK_FAMILIES else "银行"
    try:
        points = _fetch(PlatformDataConnector(), f"{family}:{suffix}")
    except DataFetchError as exc:
        assert "不存在" in str(exc), str(exc)
        return
    assert points == [], f"{family} 在库缺失时既不抛错也不为空：{points}"
    assert not any(p.value == 0 for p in points)


def test_table_store_error_is_never_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_table_store` 找不到表时**抛 `DataFetchError`**（不静默回退到别的库）。"""
    monkeypatch.setattr(pdc, "_TABLE_CANDIDATES", {"fact_alerts": ("app_db",)})

    def _broken(_name: str) -> Path:
        return Path("Z:/definitely/not/here.db")

    monkeypatch.setattr(pdc, "_store_path", _broken)
    reset_cache()
    with pytest.raises(DataFetchError) as excinfo:
        pdc._table_store("fact_alerts")              # noqa: SLF001
    assert "都不存在" in str(excinfo.value)


def test_unlock_table_missing_falls_back_never_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """★ `解禁计划`：**两条取数面都不可用**时 —— 要么抛错、要么空，**绝不许返回 0 点**。

    2026-09-29 新增口径：该族首选 `app_db::unlock_plan`（逐股表，索引点查），
    表不可用时回退投资日历 JSON。所以"库缺失"要**同时**打断两条路
    （`unlock_plan_repo._path` + 连接器的 `_store_path`）——
    只打断一条正好会走到另一条的**真实数据**上，看起来像"用例坏了"。
    """
    from src.infrastructure.repositories import unlock_plan_repo

    monkeypatch.setattr(unlock_plan_repo, "_path",
                        lambda: str(tmp_path / "no_unlock.db"))
    monkeypatch.setattr(pdc, "_store_path",
                        lambda _name: tmp_path / "no_points.db")
    reset_cache()
    try:
        points = _fetch(PlatformDataConnector(), f"解禁计划:{CODE}")
    except DataFetchError:
        return                       # 明确报错也是合法归宿
    assert points == [], f"两条取数面都不可用时不许给点（更不许给 0）：{points}"
    assert not any(p.value == 0 for p in points)


def test_readonly_connection_rejects_writes(tmp_path: Path) -> None:
    """连接必须是**只读**的（`PRAGMA query_only=1` + `mode=ro`）—— 写到生产库不可逆。"""
    db = tmp_path / "probe.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (x INT)")
    conn.commit()
    conn.close()

    class _Store:
        name = "probe"

    original = pdc._store_path                          # noqa: SLF001
    pdc._store_path = lambda _name: db                  # type: ignore[assignment]
    try:
        with pdc._readonly("probe") as readonly:        # noqa: SLF001
            with pytest.raises(sqlite3.Error):
                readonly.execute("INSERT INTO t (x) VALUES (1)")
    finally:
        pdc._store_path = original                      # type: ignore[assignment]


# ============================================================================
# 七、行业侧三族
# ============================================================================


@pytest.mark.parametrize(("industry", "expected_tier"), [
    ("银行", "exact"), ("银行(A股)", "exact"), ("白酒", "contains"),
    ("不存在XYZ", "none"), ("", "none"),
])
def test_board_name_matcher_is_deterministic(
    industry: str, expected_tier: str,
) -> None:
    """名称匹配**确定性**（exact → contains → none），且不去括号、不合并同名板块。"""
    candidates = ["银行(A股)", "银行", "股份制银行", "货币金融服务指数", "白酒Ⅱ"]
    matched, tier = pdc._match_board_name(industry, candidates)   # noqa: SLF001
    assert tier == expected_tier
    if tier == "exact":
        assert matched == industry
    if tier == "contains":
        assert matched == "白酒Ⅱ", "互为子串时取命中最长、板块名最短者"
    if tier == "none":
        assert matched is None


@pytest.mark.skipif(not _crowding_ready(), reason="本机没有拥挤度表")
def test_industry_crowding_real_data() -> None:
    """真实取数：行业名 → 最相近板块 → 水位，并把匹配过程写进 extra。"""
    points = _fetch(PlatformDataConnector(), "行业拥挤度:银行")
    assert len(points) == 1
    extra = points[0].extra
    assert extra["industry_name"] == "银行"
    assert extra["matched_board"] and extra["match_tier"] in ("exact", "contains")
    assert 0.0 <= float(points[0].value) <= 1.0
    assert extra["candidate_count"] >= 100
    assert points[0].period_date


@pytest.mark.skipif(not _crowding_ready(), reason="本机没有拥挤度表")
def test_industry_crowding_not_found_is_not_a_gap() -> None:
    """★★ 用户口径：「找不到就不提示未找到数据」。

    判据写成**机器可读**的：不产点 + `reason` 里**不出现**缺口/未找到字样。
    """
    conn = PlatformDataConnector()
    points = _fetch(conn, "行业拥挤度:不存在的行业ZZZ")
    assert points == []
    reason = _diagnose(conn, "行业拥挤度:不存在的行业ZZZ").reason
    assert reason, "不产点也要有理由（给排查用）"
    for banned in ("未找到数据", "未找到", "数据缺失", "缺口"):
        assert banned not in reason, f"reject 措辞泄漏到面向用户的 reason：{reason}"


def test_sector_flow_snapshot_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 序列不可用时**降级为截面当日口径**，并把 `window_days=1` 与原因写进 extra。

    实测（本机）：东财历史接口 `RemoteDisconnected` 时冷调用 **35.6s** 且不可用，
    而 Tushare 板块截面 **0.7s 且可用** —— 所以截面优先、序列最多等 6s。
    """
    class _SnapProvider:
        async def sector_snapshot(self) -> dict[str, dict[str, Any]]:
            return {"银行": {"code": "BK1283.DC", "trade_date": "20260928",
                            "net": -896960544.0, "net_rate": -3.35,
                            "pct_change": -0.21, "rank": 35.0,
                            "content_type": "行业"}}

        async def sector_history(self, _name: str, *, window_days: int = 5) -> Any:
            raise TimeoutError("模拟东财超时")

    monkeypatch.setattr(pdc, "_fundflow_provider", lambda: _SnapProvider())
    points = _fetch(PlatformDataConnector(), "板块资金流:银行")
    assert len(points) == 1
    extra = points[0].extra
    assert extra["window_basis"] == "snapshot_latest_day"
    assert extra["window_days"] == 1
    assert extra["direction"] == "outflow" and extra["direction_zh"] == "净流出"
    assert points[0].value == pytest.approx(-896960544.0)
    assert extra["snapshot_rank"] == 35.0
    assert "等待超过" in str(extra["history_gap"])
    assert "截面当日" in str(extra["history_gap"])


def test_sector_flow_direction_uses_sign_not_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """方向按**合计符号**判（不设阈值 —— 无数据支撑的阈值就是假精度）。"""
    class _Point:
        def __init__(self, net: float, day: str) -> None:
            self.net = net
            self.date = day

        def to_dict(self) -> dict[str, Any]:
            return {"date": self.date, "net": self.net}

    class _Entity:
        data_source = "东财概念资金流历史"
        series = [_Point(1e8, "2026-09-24"), _Point(-3e8, "2026-09-25")]

    class _Provider:
        async def sector_snapshot(self) -> dict[str, dict[str, Any]]:
            return {"银行": {"code": "BK1283.DC", "net": -1.0}}

        async def sector_history(self, _name: str, *, window_days: int = 5) -> Any:
            return _Entity()

    monkeypatch.setattr(pdc, "_fundflow_provider", lambda: _Provider())
    extra = _fetch(PlatformDataConnector(), "板块资金流:银行")[0].extra
    assert extra["window_basis"] == "history_series"
    assert extra["window_days"] == 2
    assert extra["direction"] == "outflow"
    assert "不设阈值" in extra["direction_basis"]


@pytest.mark.skipif(not _rotation_ready(), reason="本机没有行业轮动日报")
def test_rotation_real_data_and_staleness_disclosed() -> None:
    """真实取数：日报那一行 + **落伍要写明**（不许当成"今天的行情"）。"""
    points = _fetch(PlatformDataConnector(), "行业轮动:银行")
    assert len(points) == 1
    extra = points[0].extra
    assert extra["row"]["name"] == "银行"
    assert extra["report_date"]
    assert isinstance(extra["is_stale"], bool)
    if extra["is_stale"]:
        assert extra["stale_note"], "落伍必须有人话说明"
    assert "narrative" in extra


def test_rotation_stale_disclosure_is_computed(monkeypatch: pytest.MonkeyPatch) -> None:
    """落伍天数由 `expected_trade_date` 与报告日期算出（构造输入 → 断言天数）。"""
    class _Store:
        @staticmethod
        def latest_date() -> str:
            return "20260920"

        @staticmethod
        def load(_date: str) -> dict[str, Any]:
            return {"industries": [{"name": "银行", "code": "BK1", "level": 1,
                                    "pct": -0.21, "net_yi": -8.97}],
                    "narrative": {"headline": "测试", "body": "正文", "views": []},
                    "meta": {}}

        @staticmethod
        def history() -> list[str]:
            return ["20260920"]

    class _Service:
        @staticmethod
        def expected_trade_date() -> str:
            return "20260928"

        @staticmethod
        def is_stale() -> bool:
            return True

        @staticmethod
        def pick_heat(boards: list[dict[str, Any]], **_kw: Any) -> list[dict[str, Any]]:
            return boards

    import src.sector_rotation.service as rotation_service
    import src.sector_rotation.store as rotation_store

    monkeypatch.setattr(rotation_store, "latest_date", _Store.latest_date)
    monkeypatch.setattr(rotation_store, "load", _Store.load)
    monkeypatch.setattr(rotation_store, "history", _Store.history)
    monkeypatch.setattr(rotation_service, "expected_trade_date",
                        _Service.expected_trade_date)
    monkeypatch.setattr(rotation_service, "is_stale", _Service.is_stale)
    monkeypatch.setattr(rotation_service, "pick_heat", _Service.pick_heat)
    reset_cache()
    extra = _fetch(PlatformDataConnector(), "行业轮动:银行")[0].extra
    assert extra["is_stale"] is True
    assert extra["stale_days"] == 8
    assert "落伍 8 天" in extra["stale_note"]
    assert extra["narrative"]["body"] == "正文"


# ============================================================================
# 八、`has_related_board`：规划期判定（只读 + 保守放行 + 零新增取数）
# ============================================================================


@pytest.mark.skipif(not _crowding_ready(), reason="本机没有拥挤度/板块池")
def test_has_related_board_true_for_real_industry_false_for_nonsense() -> None:
    conn = PlatformDataConnector()
    assert conn.has_related_board("银行") is True          # 实测命中板块 881155.TI
    assert conn.has_related_board("不存在的行业ZZZ") is False


def test_has_related_board_is_conservative_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ 判定异常 → **True（保守放行）**：例外边界只有"匹配不上"。

    存储层真的坏了（表缺失/库打不开）是真缺口，**不许被这个判定吃掉**。
    """
    def _boom(_code: str) -> Any:
        raise DataFetchError("表 sector_crowding_daily 在候选存储里都不存在")

    monkeypatch.setattr(PlatformDataConnector, "_load_name_index", _boom)
    assert PlatformDataConnector().has_related_board("银行") is True


def test_has_related_board_adds_no_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 规划期**零新增取数**：第二次判定走缓存，SQL 条数不增。"""
    if not _crowding_ready():
        pytest.skip("本机没有拥挤度/板块池")
    counter = {"n": 0}
    original = pdc._readonly                            # noqa: SLF001

    @contextmanager
    def _counting(store: str):
        counter["n"] += 1
        with original(store) as conn:
            yield conn

    monkeypatch.setattr(pdc, "_readonly", _counting)
    conn = PlatformDataConnector()
    assert conn.has_related_board("银行") is True
    first = counter["n"]
    assert first >= 1, "第一次判定应当读一次板块池"
    assert conn.has_related_board("银行") is True
    assert counter["n"] == first, "第二次判定不应再打库（缓存 24h）"


# ============================================================================
# 九、贯通点：白名单**精确命中数** + 规划期增补（含"找不到就不排"）
# ============================================================================


def _passes_whitelist(agent_id: str, indicator: str) -> bool:
    from src.orchestration.supervisor import _AGENT_DATA_WHITELIST

    keywords = _AGENT_DATA_WHITELIST.get(agent_id, ())
    low = indicator.lower()
    return any(str(k).lower() in low for k in keywords)


@pytest.mark.parametrize(("agent_id", "indicator"), [
    # 个股侧五族 → A10；告警两族 + 解禁 → A11（用户/父 agent 的口径）
    ("A10_micro", f"估值水位:{CODE}"),
    ("A10_micro", f"概念拥挤度:{CODE}"),
    ("A10_micro", f"主线告警:{CODE}"),
    ("A10_micro", f"个股告警:{CODE}"),
    ("A10_micro", f"解禁计划:{CODE}"),
    ("A11_fin_risk", f"主线告警:{CODE}"),
    ("A11_fin_risk", f"个股告警:{CODE}"),
    ("A11_fin_risk", f"解禁计划:{CODE}"),
    # 行业侧三族 → A09 / A13-A16 / A20
    ("A09_meso", "行业拥挤度:银行"),
    ("A09_meso", "板块资金流:银行"),
    ("A09_meso", "行业轮动:银行"),
    ("A13_tech", "行业拥挤度:半导体"),
    ("A14_consumer", "板块资金流:白酒"),
    ("A15_cyclical", "行业轮动:煤炭"),
    ("A16_pharma", "行业拥挤度:医药"),
    ("A16_pharma", "行业轮动:医药"),
    ("A20_generic_industry", "行业拥挤度:银行"),
    ("A20_generic_industry", "板块资金流:银行"),
    ("A20_generic_industry", "行业轮动:银行"),
])
def test_whitelist_precise_hits(agent_id: str, indicator: str) -> None:
    """★ 判据写成**精确命中**（不是"有没有出现"）—— 0 命中会走 fallback 返回前 200 条。"""
    assert _passes_whitelist(agent_id, indicator) is True, (
        f"{agent_id} 拿不到 {indicator} —— 数据到了但 Agent 看不见，且不报错")


@pytest.mark.parametrize(("agent_id", "indicator"), [
    # 估值只给个股 Agent（行业 Agent 看行业截面，不看单只票的估值）
    ("A20_generic_industry", f"估值水位:{CODE}"),
    ("A09_meso", f"个股告警:{CODE}"),
    ("A08_macro", "行业拥挤度:银行"),
])
def test_whitelist_does_not_over_reach(agent_id: str, indicator: str) -> None:
    """反向判据：不该看到的**看不到**（白名单不能形同虚设）。"""
    assert _passes_whitelist(agent_id, indicator) is False


def _inject_industry(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """★ 把"注入一个行业"这件事**同时打到两个入口**上（`CHG-0232`）。

    ## 为什么必须两个都打

    `CHG-0217` 把「行业三族」的生产路径从**单值** `resolve_focus_industry`
    改成了**多值** `resolve_focus_industries`。而下面三条用例仍然只 patch 单值入口
    ⇒ 注入**不再生效**，它们转而调用**真实解析器**。

    ★★ **症状比"全红"更危险**：实测三条里**只有一条变红**
    （`test_plan_augmentation_always_plans_crowding` 注入的 `查无此行业` 真实解析不出来），
    另两条**碰巧还是绿的** —— 因为它们注入的 `银行` 在真实文本里**恰好也能被解出来**。
    ⇒ **"注入失效但断言照过"是最坏的一类假绿：它看起来在测注入，其实在测真实解析。**

    ⚠️ 这也解释了为什么"改生产代码走哪个入口"必须回头搜**谁在 patch 旧入口** ——
      patch 目标换了，测试不会报"没打到"，只会**静默测别的东西**。
    """
    from src.orchestration import supervisor as sv

    monkeypatch.setattr(sv, "resolve_focus_industry",
                        lambda _text: (name, "测试注入"))
    monkeypatch.setattr(sv, "resolve_focus_industries",
                        lambda _text: [(name, "测试注入")])


def test_plan_augmentation_adds_industry_families(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """规划期增补：行业名 + 近期意图 → 三族（判据是**指标在计划里**）。"""
    from src.orchestration import supervisor

    _inject_industry(monkeypatch, "银行")
    monkeypatch.setattr(supervisor, "_industry_crowding_available", lambda _i: True)
    _agents, indicators, notes = supervisor.augment_plan_by_query_signals(
        "银行板块近期怎么走", "", [], [])
    assert "行业拥挤度:银行" in indicators
    assert "板块资金流:银行" in indicators
    assert "行业轮动:银行" in indicators, "问句含「近期」→ 必须补轮动日报"
    assert any("行业拥挤度" in n for n in notes)


def test_plan_augmentation_always_plans_crowding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★★ 2026-09-29 口径变更：本地匹配不上**也照排** —— 拿不到就交给联网兜底去找。

    用户原话：「**"找不到就不提示"改成 找不到就去联网搜索找**」。
    所以规划期不再当闸门（旧判据是"匹配不上就不排"，那会让这个族
    **永远不会联网**，因为采集链上当时没有联网兜底）。
    这里钉住新行为：指标**必须**进计划，且 note 里说明"本地无相近板块、
    交给联网兜底"。
    """
    from src.orchestration import supervisor

    _inject_industry(monkeypatch, "查无此行业")
    monkeypatch.setattr(supervisor, "_industry_crowding_available", lambda _i: False)
    _agents, indicators, notes = supervisor.augment_plan_by_query_signals(
        "查无此行业近期怎么走", "", [], [])
    assert "行业拥挤度:查无此行业" in indicators, (
        "本地匹配不上不再是「不排」的理由 —— 必须交给采集去试（联网那跳接手）")
    assert "板块资金流:查无此行业" in indicators
    assert any("联网兜底" in n for n in notes), notes


def test_industry_crowding_probe_exception_is_conservative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ 判定**抛异常时仍然排**（两层都要保守）：真缺口必须如实上报，不许被判定吃掉。

    这是「例外边界只有匹配不上」那条纪律的机器复现 ——
    存储层坏了（表缺失/库打不开）时，指标照旧进计划、缺口照旧出现在 errors 里。
    """
    from src.orchestration import supervisor

    def _boom(_self: Any, _industry: str) -> bool:
        raise RuntimeError("模拟存储层故障（表缺失/库打不开）")

    monkeypatch.setattr(PlatformDataConnector, "has_related_board", _boom)
    assert supervisor._industry_crowding_available("银行") is True   # noqa: SLF001

    _inject_industry(monkeypatch, "银行")
    _agents, indicators, _notes = supervisor.augment_plan_by_query_signals(
        "银行近期怎么走", "", [], [])
    assert "行业拥挤度:银行" in indicators


def test_plan_augmentation_stock_families_carry_code_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ 个股五族进计划时必须带 6 位代码后缀（裸名没人 supports → 必然失败）。"""
    from src.orchestration import supervisor

    _agents, indicators, _notes = supervisor.augment_plan_by_query_signals(
        f"招商银行{CODE}现在能不能持有", "", ["A10_micro"], [], resolved_code=CODE)
    for family in STOCK_FAMILIES:
        assert f"{family}:{CODE}" in indicators, f"{family} 没进计划或没带代码后缀"
    assert not any(i in indicators for i in STOCK_FAMILIES), "不许出现裸名"


def test_registered_ids_templates_resolve_per_code() -> None:
    """登记形态必须是**模板**：`registry.get("估值水位:600036")` 要能命中。

    登记成裸名字的话 `catalog/registry` 的**分段匹配**查不到（key 段数不同）
    ⇒ 判"未登记" ⇒ SmartFetcher 走 daily/24h 兜底、TTL 管不到。
    """
    from src.infrastructure.catalog import get_registry

    registry = get_registry()
    for family in STOCK_FAMILIES:
        meta = registry.get(f"{family}:{CODE}")
        assert meta is not None, f"{family}:{{code}} 未登记成模板，按代码查询查不到"
        assert meta.frequency in ("daily", "intraday", "weekly", "monthly")
    for family in INDUSTRY_FAMILIES:
        meta = registry.get(f"{family}:银行")
        assert meta is not None, f"{family}:{{行业名}} 未登记成模板"
