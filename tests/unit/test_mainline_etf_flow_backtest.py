"""ETF 份额信号回测报告**序列化**（`etf_flow_backtest.to_dict`）单元测试。

## 为什么只测 `to_dict`，不测回测本身

回测要读本地数据仓与全量 ETF 份额，属于数据验证范畴（见 `docs/ETF_FLOW_BACKTEST.md`
与 `test_mainline_etf_flow.py` 的说明）——单测里跑不动也不该跑。

但 `to_dict()` 是纯函数式组装：它的输入是内存里的 `BacktestReport`，输出直接决定
**面板「信号明细」看到什么**。这里钉住的是三个纯逻辑点：

1. **取尾部而不是头部。** records[:300]` 。
2. **输出新的在前。** 前端只渲染前 200 行；若最旧的排在最前，
   那 200 行会截在"最近 300 条里较早的一半"，等于又丢一次近期数据。
3. **`record_count` 是全样本总数**，不是截断后的长度 —— 它是"明细被截断了"。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from src.mainline.etf_flow import FlowSignal
from src.mainline.etf_flow_backtest import BacktestReport, SignalRecord


def _record(day: str) -> SignalRecord:
    return SignalRecord(
        signal=FlowSignal(date=day, code="510300.SH", name="华泰柏瑞沪深300ETF"),
        regime="range", forward={5: 0.01}, forward_t1={5: 0.01},
        max_gain=0.02, max_drawdown=-0.01)


def _dates(count: int) -> list[str]:
    """`count` 个**严格升序**的交易日字符串。

    ⚠️ 用真实日期递推，不要用 `f"20{year:02d}{month:02d}{day:02d}"` 那种算术拼接：
    取模拼出来的月份会回绕（...1231 后面又出现 1201），序列不是单调的，
    于是"明细是否新的在前"这条断言会在夹具上假失败 —— 与被测代码无关。
    """
    base = datetime(2018, 1, 2)
    return [(base + timedelta(days=index)).strftime("%Y%m%d")
            for index in range(count)]


def _report(count: int) -> BacktestReport:
    """造 `count` 条按日期升序的信号 —— 与 `run_backtest` 的真实追加顺序一致。"""
    return BacktestReport(run_id="etfbt-test",
                          records=[_record(day) for day in _dates(count)])


def test_records_keep_the_latest_not_the_earliest() -> None:
    """1252 条里只留最近 300 条 —— 且必须是**尾部**，不是头部。"""
    report = _report(1252)
    payload = report.to_dict(record_limit=300)

    assert len(payload["records"]) == 300
    assert payload["record_count"] == 1252, "总数必须是全样本，不是 300"

    kept = [row["date"] for row in payload["records"]]
    assert max(kept) == report.records[-1].signal.date, "最近的那条必须在"
    assert min(kept) == report.records[-300].signal.date
    # 反证：最早那批（头部 300 条）一条都不该出现
    earliest = {item.signal.date for item in report.records[:300]}
    assert not (earliest & set(kept)), "明细里仍混进了最早那批样本"


def test_records_are_newest_first() -> None:
    """新的在前：前端只渲染前 200 行，倒序才不会把近期样本截掉。"""
    payload = _report(1252).to_dict(record_limit=300)
    dates = [row["date"] for row in payload["records"]]

    assert dates == sorted(dates, reverse=True), "明细顺序必须是新的在前"
    assert dates[0] == _report(1252).records[-1].signal.date


def test_limit_larger_than_sample_keeps_everything() -> None:
    """样本比上限少时全给，仍然新的在前（不因为切片反向而出错）。"""
    payload = _report(7).to_dict(record_limit=300)

    assert len(payload["records"]) == 7
    assert payload["record_count"] == 7
    assert [row["date"] for row in payload["records"]] == sorted(
        [item.signal.date for item in _report(7).records], reverse=True)


def test_record_limit_can_be_disabled() -> None:
    """`record_limit=0` 表示不带明细 —— 但总数照旧如实报。"""
    payload = _report(1252).to_dict(record_limit=0)

    assert payload["records"] == []
    assert payload["record_count"] == 1252


def test_empty_report_is_safe() -> None:
    """没有信号时（回测区间内一条都没触发）序列化不能炸。"""
    payload = BacktestReport(run_id="etfbt-empty").to_dict()

    assert payload["records"] == [] and payload["record_count"] == 0
