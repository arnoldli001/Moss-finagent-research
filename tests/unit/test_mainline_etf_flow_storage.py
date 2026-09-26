"""ETF 份额监控落库（`MainlineRepository` 的 ETF 相关方法）单元测试。

只测存储层本身：用合成信号，不依赖本地仓库与全量数据。
覆盖的重点是**筛选口径**与**幂等性** ——
`alerts_only` 若写成"只判 gated"就会把观察列表里的弱信号也算成告警，
面板上会凭空多出一批"建议"，而这在界面上看起来完全正常。
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from src.mainline.etf_flow import (
    KIND_INDUSTRY_REVERSAL,
    KIND_OPPORTUNITY,
    KIND_RISK,
    LEVEL_MEDIUM,
    LEVEL_STRONG,
    LEVEL_WEAK,
    FlowSignal,
)
from src.mainline.storage import ETF_BACKTEST_TABLE, ETF_SIGNAL_TABLE, MainlineRepository


@pytest.fixture
def repo(tmp_path) -> MainlineRepository:
    return MainlineRepository(path=str(tmp_path / "mainline.db"))


def _signal(date: str, code: str, *, kind: str = KIND_OPPORTUNITY,
            level: str = LEVEL_MEDIUM, gated: bool = False,
            regime: str = "bear", name: str = "", group: str = "hs300",
            resonance: bool = False) -> FlowSignal:
    return FlowSignal(date=date, code=code, name=name or code, group=group,
                      group_label="沪深300系列", kind=kind, level=level,
                      index_code="000300.SH", index_name="沪深300",
                      index_percentile=0.12, change_1d=0.045, change_5d=0.08,
                      resonance=resonance, resonance_count=3, resonance_total=4,
                      regime=regime, gated=gated, reasons=["测试理由"])


# ==================================================================
# 建表
# ==================================================================


async def test_schema_creates_etf_tables(repo: MainlineRepository) -> None:
    await repo.ensure_schema()
    connection = sqlite3.connect(repo.path)
    try:
        names = {str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        connection.close()
    assert ETF_SIGNAL_TABLE in names
    assert ETF_BACKTEST_TABLE in names


async def test_ensure_schema_is_idempotent(repo: MainlineRepository) -> None:
    await repo.ensure_schema()
    await repo.ensure_schema()
    assert await repo.latest_etf_trade_date() == ""


# ==================================================================
# 信号读写
# ==================================================================


async def test_save_and_load_round_trip(repo: MainlineRepository) -> None:
    written = await repo.save_etf_signals([
        _signal("20260115", "510300.SH", name="华泰柏瑞沪深300ETF")])
    assert written == 1
    rows = await repo.load_etf_signals()
    assert len(rows) == 1
    row = rows[0]
    assert row["trade_date"] == "20260115"
    assert row["code"] == "510300.SH"
    assert row["name"] == "华泰柏瑞沪深300ETF"
    assert row["kind"] == KIND_OPPORTUNITY
    assert row["level"] == LEVEL_MEDIUM
    assert row["regime"] == "bear"
    assert row["gated"] is False
    assert row["payload"]["change_1d"] == pytest.approx(0.045)


async def test_repeated_save_updates_instead_of_duplicating(
        repo: MainlineRepository) -> None:
    """同一天同一只 ETF 重复写入只有一行（复盘重算会反复写同一天）。"""
    await repo.save_etf_signals([_signal("20260115", "510300.SH")])
    await repo.save_etf_signals([_signal("20260115", "510300.SH",
                                         level=LEVEL_STRONG, resonance=True)])
    rows = await repo.load_etf_signals()
    assert len(rows) == 1
    assert rows[0]["level"] == LEVEL_STRONG
    assert rows[0]["resonance"] is True


async def test_signal_without_id_is_skipped(repo: MainlineRepository) -> None:
    """没有 alert_id 的对象无法定位，跳过而不是写一行空主键。"""
    broken = _signal("20260115", "510300.SH")
    broken.date = ""
    broken.code = ""
    assert await repo.save_etf_signals([broken]) == 0


async def test_empty_input_writes_nothing(repo: MainlineRepository) -> None:
    assert await repo.save_etf_signals([]) == 0


async def test_latest_trade_date(repo: MainlineRepository) -> None:
    await repo.save_etf_signals([_signal("20260115", "510300.SH"),
                                 _signal("20260320", "510310.SH")])
    assert await repo.latest_etf_trade_date() == "20260320"


async def test_latest_trade_date_empty_when_no_rows(
        repo: MainlineRepository) -> None:
    assert await repo.latest_etf_trade_date() == ""


# ==================================================================
# 筛选口径（最容易写错的地方）
# ==================================================================


async def test_alerts_only_excludes_weak_observations(
        repo: MainlineRepository) -> None:
    """`alerts_only` 必须排除弱信号。

    弱信号虽然 `gated=False`，但它只是观察项、从不告警；
    若只判 gated，面板会把观察列表当成告警列表。
    """
    await repo.save_etf_signals([
        _signal("20260115", "510300.SH", level=LEVEL_MEDIUM),
        _signal("20260115", "510310.SH", level=LEVEL_WEAK),
        _signal("20260115", "510330.SH", level=LEVEL_STRONG),
    ])
    alerts = await repo.load_etf_signals(alerts_only=True)
    assert {row["code"] for row in alerts} == {"510300.SH", "510330.SH"}
    everything = await repo.load_etf_signals()
    assert len(everything) == 3


async def test_alerts_only_excludes_gated(repo: MainlineRepository) -> None:
    """被环境门控降级的信号即使等级没降干净也不能算告警。"""
    await repo.save_etf_signals([
        _signal("20260115", "510300.SH", level=LEVEL_STRONG, gated=True),
        _signal("20260115", "510310.SH", level=LEVEL_MEDIUM, gated=False),
    ])
    alerts = await repo.load_etf_signals(alerts_only=True)
    assert {row["code"] for row in alerts} == {"510310.SH"}


async def test_gated_flag_filter_is_tri_state(repo: MainlineRepository) -> None:
    """`gated=None`（默认）不过滤；True/False 各自只取一侧。"""
    await repo.save_etf_signals([
        _signal("20260115", "510300.SH", gated=True),
        _signal("20260115", "510310.SH", gated=False),
    ])
    assert len(await repo.load_etf_signals()) == 2
    assert len(await repo.load_etf_signals(gated=True)) == 1
    assert (await repo.load_etf_signals(gated=True))[0]["code"] == "510300.SH"
    assert (await repo.load_etf_signals(gated=False))[0]["code"] == "510310.SH"


async def test_kind_filter_accepts_comma_list(repo: MainlineRepository) -> None:
    await repo.save_etf_signals([
        _signal("20260115", "510300.SH", kind=KIND_OPPORTUNITY),
        _signal("20260115", "510310.SH", kind=KIND_RISK),
        _signal("20260115", "159516.SZ", kind=KIND_INDUSTRY_REVERSAL),
    ])
    rows = await repo.load_etf_signals(kind=f"{KIND_OPPORTUNITY},{KIND_RISK}")
    assert {row["kind"] for row in rows} == {KIND_OPPORTUNITY, KIND_RISK}


async def test_date_range_is_inclusive_start_exclusive_end(
        repo: MainlineRepository) -> None:
    await repo.save_etf_signals([
        _signal("20260101", "510300.SH"), _signal("20260115", "510310.SH"),
        _signal("20260201", "510330.SH")])
    rows = await repo.load_etf_signals(start="20260101", end="20260201")
    assert {row["trade_date"] for row in rows} == {"20260101", "20260115"}


async def test_code_and_group_filters(repo: MainlineRepository) -> None:
    await repo.save_etf_signals([
        _signal("20260115", "510300.SH", group="hs300"),
        _signal("20260115", "510050.SH", group="sse50")])
    assert len(await repo.load_etf_signals(code="510050.SH")) == 1
    assert len(await repo.load_etf_signals(group="sse50")) == 1


async def test_limit_is_respected(repo: MainlineRepository) -> None:
    await repo.save_etf_signals([
        _signal(f"202601{i:02d}", f"51030{i}.SH") for i in range(1, 6)])
    assert len(await repo.load_etf_signals(limit=2)) == 2


async def test_ordering_is_newest_first(repo: MainlineRepository) -> None:
    await repo.save_etf_signals([_signal("20260101", "510300.SH"),
                                 _signal("20260301", "510310.SH")])
    rows = await repo.load_etf_signals()
    assert rows[0]["trade_date"] == "20260301"


async def test_same_day_ordering_is_by_severity_not_alphabet(
        repo: MainlineRepository) -> None:
    """同日信号按严重度排（strong → medium → weak）。

    按字符串排会得到 medium < strong < weak，把"中信号"顶到"强信号"前面 ——
    面板上最该先看到的那条反而排在后面。
    """
    await repo.save_etf_signals([
        _signal("20260115", "510300.SH", level=LEVEL_WEAK),
        _signal("20260115", "510310.SH", level=LEVEL_STRONG),
        _signal("20260115", "510330.SH", level=LEVEL_MEDIUM)])
    rows = await repo.load_etf_signals()
    assert [row["level"] for row in rows] == [LEVEL_STRONG, LEVEL_MEDIUM, LEVEL_WEAK]


# ==================================================================
# 容错
# ==================================================================


async def test_corrupt_payload_row_is_skipped_not_fatal(
        repo: MainlineRepository) -> None:
    """一行坏 JSON 只牺牲自己，不能把整页读成空。"""
    await repo.save_etf_signals([_signal("20260115", "510300.SH")])
    await repo.save_etf_signals([_signal("20260116", "510310.SH")])
    connection = sqlite3.connect(repo.path)
    try:
        connection.execute(
            f"UPDATE {ETF_SIGNAL_TABLE} SET payload = 'not json'"
            " WHERE code = '510300.SH'")
        connection.commit()
    finally:
        connection.close()
    rows = await repo.load_etf_signals()
    assert len(rows) == 1
    assert rows[0]["code"] == "510310.SH"


async def test_read_degrades_to_empty_when_db_missing(tmp_path) -> None:
    """库路径指向不存在的目录时读降级成空，而不是抛错（面板要能开）。"""
    repo = MainlineRepository(path=str(tmp_path / "nope" / "x.db"))
    assert await repo.load_etf_signals() == []
    assert await repo.latest_etf_trade_date() == ""


# ==================================================================
# 回测读写
# ==================================================================


class _FakeReport:
    """鸭子类型的回测报告（存储层只需要 `to_dict()` 和几个属性）。

    ⚠️ `to_dict` 的签名必须和真身 `BacktestReport.to_dict` 一致（含
    `collapse_runs`）：存储层落库时会显式传 `collapse_runs=True`（明细里
    "同一事件连报多日"要合并掉），替身少一个入参就会让整组测试炸在
    TypeError 上 —— 那是**契约不同步**，不是被测代码的问题。
    """

    def __init__(self, run_id: str = "") -> None:
        self.run_id = run_id
        self.range_start, self.range_end = "20180102", "20260918"
        self.started_at = "2026-09-20T19:00:00"
        self.finished_at = "2026-09-20T19:00:03"
        self.seconds = 2.91
        self.days = 2048
        self.records = [1, 2, 3]
        self.markdown = "# 报告正文"
        self.gaps = ["一个缺口"]
        self.error = ""

    def to_dict(self, *, record_limit: int = 300,
                collapse_runs: bool = False) -> dict:  # noqa: ARG002 签名对齐真身
        return {"by_kind": {"opportunity_live": {"34": {"samples": 66}}},
                "by_regime": {"bear": {"34": {"samples": 77}}},
                "record_count": len(self.records), "records": [],
                "collapsed": 0}


async def test_backtest_round_trip(repo: MainlineRepository) -> None:
    run_id = await repo.save_etf_backtest(_FakeReport(run_id="etfbt-1"))
    assert run_id == "etfbt-1"
    payload = await repo.load_etf_backtest("etfbt-1")
    assert payload["range_start"] == "20180102"
    assert payload["signals"] == 3
    assert payload["days"] == 2048
    assert payload["gaps"] == ["一个缺口"]
    assert payload["result"]["by_kind"]["opportunity_live"]["34"]["samples"] == 66
    assert payload["markdown"] == "# 报告正文"


async def test_load_backtest_defaults_to_latest(repo: MainlineRepository) -> None:
    await repo.save_etf_backtest(_FakeReport(run_id="etfbt-old"))
    newer = _FakeReport(run_id="etfbt-new")
    newer.finished_at = "2026-09-21T10:00:00"
    await repo.save_etf_backtest(newer)
    assert (await repo.load_etf_backtest())["run_id"] == "etfbt-new"


async def test_load_backtest_without_rows_returns_empty(
        repo: MainlineRepository) -> None:
    assert await repo.load_etf_backtest() == {}


async def test_list_backtests_omits_markdown(repo: MainlineRepository) -> None:
    """列表接口不能拖正文（单页几十 KB）。"""
    await repo.save_etf_backtest(_FakeReport(run_id="etfbt-1"))
    runs = await repo.list_etf_backtests()
    assert len(runs) == 1
    assert "markdown" not in runs[0]
    assert "result" not in runs[0]
    assert runs[0]["signals"] == 3


async def test_backtest_without_run_id_gets_one(repo: MainlineRepository) -> None:
    report = _FakeReport()
    run_id = await repo.save_etf_backtest(report)
    assert run_id
    assert report.run_id == run_id
    assert (await repo.load_etf_backtest(run_id))["run_id"] == run_id


async def test_backtest_result_is_valid_json_in_db(repo: MainlineRepository) -> None:
    await repo.save_etf_backtest(_FakeReport(run_id="etfbt-1"))
    connection = sqlite3.connect(repo.path)
    try:
        row = connection.execute(
            f"SELECT result FROM {ETF_BACKTEST_TABLE}").fetchone()
    finally:
        connection.close()
    assert isinstance(json.loads(row[0]), dict)


# ==================================================================
# 快照 JSON → FlowSignal 反构造的保真度
# ==================================================================


class TestSignalRebuildFidelity:
    """`_rebuild_flow_signals` 用字段白名单反构造对象。

    白名单漏字段**不会报错**：该字段静默变成默认值再被写回库里，
    在界面上表现为"这条信号没有理由"，而原因完全看不出来 ——
    这里就曾经漏掉 `reasons`，导致落库的理由全是空数组。
    所以用"和 `to_dict()` 的键集合逐一比对"来守住它，
    以后 `FlowSignal` 加字段时这个测试会直接失败。
    """

    def _payload(self) -> dict:
        source = FlowSignal(
            date="20180703", code="510300.SH", name="华泰柏瑞沪深300ETF",
            group="hs300", group_label="沪深300系列", kind=KIND_OPPORTUNITY,
            level=LEVEL_STRONG, index_code="000300.SH", index_name="沪深300",
            index_percentile=0.0588, change_1d=0.0378, change_5d=0.0611,
            resonance=True, resonance_count=3, resonance_total=4,
            regime="bear", gated=False,
            reasons=["指数分位 5.9% ≤ 30%（低位）", "共振确认：3/4 只同向变化"],
            forward={"34": 0.0918})
        return {"signals": [source.to_dict()]}

    def test_all_dict_keys_survive_rebuild(self) -> None:
        from src.api.routes.mainline import _rebuild_flow_signals

        payload = self._payload()
        rebuilt = _rebuild_flow_signals(payload, None)
        assert len(rebuilt) == 1
        assert set(rebuilt[0].to_dict()) == set(payload["signals"][0])

    def test_reasons_are_preserved(self) -> None:
        from src.api.routes.mainline import _rebuild_flow_signals

        rebuilt = _rebuild_flow_signals(self._payload(), None)[0]
        assert rebuilt.reasons == ["指数分位 5.9% ≤ 30%（低位）",
                                   "共振确认：3/4 只同向变化"]
        assert rebuilt.to_dict()["reasons"] == rebuilt.reasons

    def test_forward_is_preserved(self) -> None:
        from src.api.routes.mainline import _rebuild_flow_signals

        rebuilt = _rebuild_flow_signals(self._payload(), None)[0]
        assert rebuilt.forward == {"34": 0.0918}

    def test_alert_id_is_regenerated_consistently(self) -> None:
        from src.api.routes.mainline import _rebuild_flow_signals

        payload = self._payload()
        rebuilt = _rebuild_flow_signals(payload, None)[0]
        assert rebuilt.alert_id == payload["signals"][0]["alert_id"]

    def test_non_dict_entries_are_skipped(self) -> None:
        from src.api.routes.mainline import _rebuild_flow_signals

        assert _rebuild_flow_signals({"signals": [None, "x", 3]}, None) == []
