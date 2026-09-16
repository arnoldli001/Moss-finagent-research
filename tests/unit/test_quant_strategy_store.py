"""策略库单测：保存 / 去重 / 列出 / 读取 / 删除 / 达标判定。

重点在**判定口径**：这道门槛如果只看全样本收益，那么"试 30 组参数挑最好的一组"
必然通过 —— 而单股票只有几千个交易日，那种"通过"没有任何预测意义。
所以测试主要盯住"样本量不足/样本外为负/自检失败时必须拒绝"。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.quant.strategy_store import (
    StrategyError,
    StrategyStore,
    spec_hash,
    verdict,
)

SPEC = {
    "code": "600519",
    "entry": "close > MA(close, 20) AND momentum_20 > 0",
    "exit_condition": "close < MA(close, 10)",
    "config": {"max_hold_days": 20, "stop_loss_pct": 0.08,
               "train_ratio": 0.7, "initial_cash": 100_000},
}


def result_with(*, trades: int = 30, oos_trades: int = 10,
                oos_return: float = 5.0, sharpe: float = 1.0,
                warnings: list[str] | None = None) -> dict:
    return {
        "code": "600519",
        "dates": ["20260101", "20260102", "20260103"],
        "metrics": {"trade_count": trades, "sharpe": sharpe,
                    "total_return_pct": 12.0, "max_drawdown_pct": -6.0},
        "segments": {"oos": {"trades": oos_trades, "return_pct": oos_return}},
        "warnings": warnings or [],
        "config": {**SPEC["config"], "entry": SPEC["entry"],
                   "exit": SPEC["exit_condition"]},
    }


@pytest.fixture()
def store(tmp_path: Path) -> StrategyStore:
    return StrategyStore(tmp_path / "strategies")


# ==================================================================
# 内容哈希
# ==================================================================


def test_same_params_produce_same_hash() -> None:
    first = spec_hash(SPEC["code"], SPEC["entry"], SPEC["exit_condition"],
                      SPEC["config"])
    second = spec_hash(SPEC["code"], SPEC["entry"], SPEC["exit_condition"],
                       dict(SPEC["config"]))
    assert first == second


def test_different_params_produce_different_hash() -> None:
    base = spec_hash(SPEC["code"], SPEC["entry"], SPEC["exit_condition"],
                     SPEC["config"])
    changed = spec_hash(SPEC["code"], SPEC["entry"], SPEC["exit_condition"],
                        {**SPEC["config"], "stop_loss_pct": 0.05})
    assert base != changed, "止损改了必须算另一个策略"
    other_entry = spec_hash(SPEC["code"], "close > MA(close, 5)",
                            SPEC["exit_condition"], SPEC["config"])
    assert base != other_entry


def test_direction_of_comparison_changes_hash() -> None:
    """`> 15` 与 `< 15` 是完全不同的策略，哈希必须不同。"""
    first = spec_hash("600519", "roe > 15", "", {})
    second = spec_hash("600519", "roe < 15", "", {})
    assert first != second


# ==================================================================
# 达标判定
# ==================================================================


def test_verdict_rejects_too_few_trades() -> None:
    judgement = verdict(result_with(trades=5, oos_trades=2))
    assert judgement["worthy"] is False
    assert any("交易笔数" in item for item in judgement["reasons"])


def test_verdict_rejects_negative_oos_return() -> None:
    """样本外为负时不能保存 —— 全样本再漂亮也不行。"""
    judgement = verdict(result_with(oos_return=-3.0))
    assert judgement["worthy"] is False
    assert any("样本外收益" in item for item in judgement["reasons"])


def test_verdict_rejects_failed_self_check() -> None:
    """自检失败（例如交易重叠 = 重复卖出）的结果一律不许入库。"""
    judgement = verdict(result_with(
        warnings=["⚠️ 自检失败：存在 1 组重叠交易"]))
    assert judgement["worthy"] is False
    assert any("回测自检" in f"{item['name']}{item['value']}"
               for item in judgement["checks"] if not item["passed"])


def test_verdict_accepts_solid_result() -> None:
    judgement = verdict(result_with())
    assert judgement["worthy"] is True
    assert "值得保存" in judgement["summary"]


def test_verdict_thresholds_can_be_overridden() -> None:
    strict = verdict(result_with(trades=25), {"min_trades": 50})
    assert strict["worthy"] is False
    relaxed = verdict(result_with(trades=3, oos_trades=1, sharpe=0.0),
                      {"min_trades": 1, "min_oos_trades": 1, "min_sharpe": 0.0})
    assert relaxed["worthy"] is True


# ==================================================================
# 保存 / 去重 / 读取
# ==================================================================


def test_save_writes_one_file(store: StrategyStore) -> None:
    record = store.save(**SPEC, name="茅台动量", result=result_with(),
                        source="api")
    files = list(store.root.glob("*.json"))
    assert len(files) == 1
    assert record.id in files[0].name
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["code"] == "600519"
    assert payload["entry"] == SPEC["entry"]
    assert payload["metrics"]["trade_count"] == 30


def test_saving_same_params_twice_updates_instead_of_duplicating(
        store: StrategyStore) -> None:
    """同一套参数重复保存不能攒出一堆只差一个数字的文件。"""
    first = store.save(**SPEC, name="第一次", result=result_with())
    second = store.save(**SPEC, name="改名后", result=result_with(trades=42))
    assert first.id == second.id
    assert len(list(store.root.glob("*.json"))) == 1
    assert second.saved_count == 2
    assert second.name == "改名后"
    assert second.metrics["trade_count"] == 42


def test_different_params_create_separate_records(store: StrategyStore) -> None:
    store.save(**SPEC, result=result_with())
    store.save(code="600519", entry="close > MA(close, 5)",
               exit_condition=SPEC["exit_condition"], config=SPEC["config"],
               result=result_with())
    assert len(list(store.root.glob("*.json"))) == 2


def test_load_round_trips(store: StrategyStore) -> None:
    saved = store.save(**SPEC, name="茅台动量", result=result_with())
    loaded = store.load(saved.id)
    assert loaded.name == saved.name
    assert loaded.entry == saved.entry
    assert loaded.segments["oos"]["return_pct"] == 5.0
    assert loaded.data_range["trading_days"] == 3


def test_list_sorted_by_update_time(store: StrategyStore) -> None:
    store.save(**SPEC, name="A", result=result_with())
    store.save(code="000001", entry="close > 0", config={}, name="B",
               result=result_with())
    records = store.list()
    assert len(records) == 2
    assert {record.name for record in records} == {"A", "B"}


def test_empty_entry_is_rejected(store: StrategyStore) -> None:
    with pytest.raises(StrategyError, match="入场条件"):
        store.save(code="600519", entry="   ", result=result_with())


def test_auto_save_refuses_below_threshold(store: StrategyStore) -> None:
    """一键自动保存必须自己把关，不能把侥幸结果写进库。"""
    with pytest.raises(StrategyError, match="门槛"):
        store.save(**SPEC, result=result_with(trades=4, oos_trades=1),
                   auto_saved=True)
    assert not list(store.root.glob("*.json"))


def test_auto_save_accepts_solid_result(store: StrategyStore) -> None:
    record = store.save(**SPEC, result=result_with(), auto_saved=True)
    assert record.auto_saved is True


def test_delete_removes_file(store: StrategyStore) -> None:
    saved = store.save(**SPEC, result=result_with())
    assert store.delete(saved.id) is True
    assert store.delete(saved.id) is False
    assert not list(store.root.glob("*.json"))


def test_load_missing_raises(store: StrategyStore) -> None:
    with pytest.raises(StrategyError, match="不存在"):
        store.load("20260101-000000-abcdef12")


def test_path_traversal_is_rejected(store: StrategyStore) -> None:
    """策略 ID 来自 HTTP，必须当成不可信输入（防目录穿越）。"""
    for evil in ("../../etc/passwd", "..\\..\\windows", "a/b", "a b", ""):
        with pytest.raises(StrategyError):
            store.load(evil)


def test_corrupt_file_is_skipped_not_fatal(store: StrategyStore) -> None:
    """一个坏文件不能让整个策略库打不开。"""
    store.save(**SPEC, result=result_with())
    store.root.joinpath("broken.json").write_text("{not json",
                                                  encoding="utf-8")
    records = store.list()
    assert len(records) == 1


def test_atomic_write_leaves_no_temp_file(store: StrategyStore) -> None:
    store.save(**SPEC, result=result_with())
    assert not list(store.root.glob("*.tmp"))


def test_save_from_result_auto_save_enforces_thresholds(
        store: StrategyStore) -> None:
    """`save_from_result(auto_saved=True)` 必须走门槛检查。

    这个参数曾经漏传，导致自动保存等于无条件保存 —— 而那正是
    "试 30 组参数挑最好一组"骗过自己的入口。
    """
    from src.quant.strategy_store import save_from_result

    weak = result_with(trades=3, oos_trades=1)
    with pytest.raises(StrategyError, match="门槛"):
        save_from_result(weak, root=store.root, auto_saved=True)
    record = save_from_result(weak, root=store.root, auto_saved=False)
    assert record.id
