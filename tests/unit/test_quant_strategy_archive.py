"""策略档案（数据库）与价值判定单测。

重点在**两件容易被做错的事**：

1. **档案字段会长大**：`create(checkfirst=True)` 不会给已存在的表补列，
   新增字段（"搜索空间""低样本标记"）会让 INSERT 引用不存在的列而整条失败 ——
   实测踩过，而且失败信息只提列名。所以必须有"自动补列"的测试；
2. **价值判定必须分维度**：单股票回测里"跑赢"至少有四种含义
   （跑赢同票买入持有 / 跑赢指数 / 风险调整后更优 / 样本外仍成立），
   合成一句口号就会掩盖真正发生的事。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.quant.warehouse import (
    STRATEGY_TABLE,
    QuantWarehouse,
    StrategyArchive,
    WarehouseConfig,
)


@pytest.fixture()
def archive(tmp_path: Path) -> StrategyArchive:
    config = WarehouseConfig(url=f"sqlite:///{(tmp_path / 'a.db').as_posix()}",
                             dialect="sqlite", description="测试")
    return StrategyArchive(QuantWarehouse(config))


def record(**overrides) -> dict:
    base = {
        "id": "preset-000001-abc", "name": "趋势跟踪·000001",
        "code": "000001", "preset": "faber_200ma",
        "entry_condition": "close > MA(close, 200)",
        "exit_condition": "close < MA(close, 200)",
        "spec_hash": "abc123",
        "recent_excess_pct": 15.0, "recent_trades": 8,
        "beats_benchmark": 1, "low_sample": 0,
        "search_space": 204, "scan_median_excess_pct": -11.98,
        "scan_beat_ratio": 0.241, "trade_count": 20,
    }
    base.update(overrides)
    return base


def test_save_and_list_round_trip(archive: StrategyArchive) -> None:
    archive.save(record())
    items = archive.list()
    assert len(items) == 1
    assert items[0]["name"] == "趋势跟踪·000001"
    assert items[0]["recent_excess_pct"] == 15.0


def test_same_spec_hash_updates_instead_of_duplicating(
        archive: StrategyArchive) -> None:
    """同一套参数重复归档只更新，不新增 —— 否则搜索会攒出大量近重复记录。"""
    archive.save(record())
    archive.save(record(name="改名了", recent_excess_pct=18.0))
    items = archive.list()
    assert len(items) == 1
    assert items[0]["name"] == "改名了"
    assert items[0]["recent_excess_pct"] == 18.0


def test_missing_columns_are_added_automatically(tmp_path: Path) -> None:
    """档案表必须能给已存在的表补新列。

    `create(checkfirst=True)` 只判断表在不在，**不补列**；档案字段是会长大的
    （"搜索空间""低样本标记"都是后加的）。没有自动补列时，新增字段会让归档
    整条失败 —— 实测出现过"归档 0 条但用户看不到报错"。
    """
    from sqlalchemy import text

    config = WarehouseConfig(url=f"sqlite:///{(tmp_path / 'old.db').as_posix()}",
                             dialect="sqlite")
    warehouse = QuantWarehouse(config)
    archive = StrategyArchive(warehouse)
    archive.ensure_table()
    # 模拟"旧表"：删掉后加的列
    engine = warehouse.engine()
    with engine.begin() as connection:
        for column in ("low_sample", "search_space", "scan_median_excess_pct"):
            connection.execute(text(
                f'ALTER TABLE "{STRATEGY_TABLE}" DROP COLUMN "{column}"'))
    archive.save(record())          # 应自动补列后写入成功
    items = archive.list()
    assert len(items) == 1
    assert items[0]["low_sample"] == 0


def test_stats_counts_winners(archive: StrategyArchive) -> None:
    archive.save(record())
    archive.save(record(id="b", spec_hash="h2", recent_excess_pct=-3.0,
                        beats_benchmark=0))
    stats = archive.stats()
    assert stats["total"] == 2
    assert stats["winners"] == 1
    assert stats["best_recent_excess_pct"] == 15.0


def test_list_filters_by_threshold_and_code(archive: StrategyArchive) -> None:
    archive.save(record())
    archive.save(record(id="b", spec_hash="h2", code="600519",
                        recent_excess_pct=4.0))
    assert len(archive.list(min_excess=10)) == 1
    assert len(archive.list(only_winners=True)) == 2
    assert len(archive.list(code="600519")) == 1


def test_list_orders_by_recent_excess_desc(archive: StrategyArchive) -> None:
    """默认按**近一年超额**降序 —— 这正是"哪些策略跑赢过"的问题。"""
    archive.save(record(recent_excess_pct=5.0))
    archive.save(record(id="b", spec_hash="h2", recent_excess_pct=25.0))
    archive.save(record(id="c", spec_hash="h3", recent_excess_pct=15.0))
    values = [item["recent_excess_pct"] for item in archive.list()]
    assert values == [25.0, 15.0, 5.0]


def test_delete_removes_record(archive: StrategyArchive) -> None:
    archive.save(record())
    assert archive.delete("preset-000001-abc") is True
    assert archive.delete("preset-000001-abc") is False
    assert archive.stats()["total"] == 0


def test_archive_survives_unavailable_database(tmp_path: Path) -> None:
    """数据库不可用时 list 返回空而不是抛异常（前端要能照常打开）。"""
    config = WarehouseConfig(url="", dialect="", description="未配置")
    archive = StrategyArchive(QuantWarehouse(config))
    assert archive.list() == []
    assert archive.stats()["available"] is False


def test_low_sample_and_context_are_persisted(archive: StrategyArchive) -> None:
    """搜索上下文必须跟着档案走。

    一个"跑赢 23.88pp"单独看像发现；配上"从 204 个组合里选出、全体中位超额
    −11.98pp、只有 3 笔交易"才是完整的信息。没有上下文的胜者档案，
    本质上是选择性报告的截图。
    """
    archive.save(record(recent_excess_pct=23.88, recent_trades=3,
                        low_sample=1))
    item = archive.list()[0]
    assert item["low_sample"] == 1
    assert item["search_space"] == 204
    assert item["scan_median_excess_pct"] == pytest.approx(-11.98)
    assert item["scan_beat_ratio"] == pytest.approx(0.241)


def test_payload_keeps_arbitrary_extra_fields(archive: StrategyArchive) -> None:
    """档案要能带任意扩展字段（不同策略类型的指标不一样）。"""
    archive.save(record(extra_metric=42, notes=["自定义说明"]))
    item = archive.list()[0]
    assert item["extra_metric"] == 42
    assert item["notes"] == ["自定义说明"]


def test_strategy_table_name_is_stable() -> None:
    assert STRATEGY_TABLE == "quant_strategy"


def test_scan_json_is_parseable_when_present() -> None:
    """扫描结果 JSON 应能被重新读入归档（--archive-only 依赖这个格式）。"""
    path = Path("data/quant/strategy_scan.json")
    if not path.exists():
        pytest.skip("尚未跑过扫描")
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    assert "presets" in payload and "search_space" in payload
    rows = [row for item in payload["presets"] for row in item.get("rows", [])]
    assert rows, "扫描结果里应当有组合明细"
    assert all("recent_excess_pct" in row for row in rows[:5])
