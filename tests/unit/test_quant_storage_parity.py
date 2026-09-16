"""存储路径一致性：**同一份数据走 CSV 与走数据库，PIT 结果必须逐值相同**。

为什么必须有这条测试：财务数据供给 11 个因子，而"换个存储后端结果就变了"
是最难查的一类不一致 —— 回测照样跑得出来，只是数字不一样，而且没人会想到
去怀疑存储层。实测（2026-09-16 全量入库后）确实出现过行数不一致：

    CSV 原始     606,745 行
    库里          331,747 行   ← 少了 45%
    PitPanel      331,746 行   ← 比库还少 1 行

查清后的结论（不是 bug，是分层）：
  1. **274,998 行是完全相同的整行副本**（36 列逐列比对，无一处不同）——
     财务去重键 (code, report_period, ann_date) 把它们合并掉，CSV 路径的
     `PitPanel._prepare` 本来也会做同样的合并，两边数量一致；
  2. **库里多出的那 1 行**是"公告日早于报告期"的错位记录，
     `PitPanel` 按硬规则主动丢弃（未来信息），仓库作为存储层忠实保留 ——
     这正是期望的分层：**存储层不替上层做语义判断**。

本测试用合成数据把这两个性质都钉住。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.quant.dataset_store import DatasetStore
from src.quant.pit import PitPanel
from src.quant.warehouse import QuantWarehouse, WarehouseConfig

KEY = ["code", "report_period", "ann_date"]
METRICS = ["roe", "roa", "grossprofit_margin", "netprofit_margin"]


def _fina_rows() -> pd.DataFrame:
    """三行正常记录 + 一行完全重复 + 一行"公告日早于报告期"的错位记录。"""
    base = {
        "code": ["000001", "000001", "600519", "000001", "603400"],
        "report_period": ["20260331", "20251231", "20260331", "20260331",
                          "20260630"],
        "ann_date": ["20260425", "20260320", "20260428", "20260425",
                     "20260422"],
        "roe": [11.5, 10.2, 8.8, 11.5, 9.9],
        "roa": [0.9, 0.85, 0.7, 0.9, 0.6],
        "grossprofit_margin": [30.0, 29.5, 90.0, 30.0, 25.0],
        "netprofit_margin": [20.0, 19.0, 50.0, 20.0, 15.0],
    }
    return pd.DataFrame(base)


def _warehouse(tmp_path: Path) -> QuantWarehouse:
    return QuantWarehouse(WarehouseConfig(
        url=f"sqlite:///{(tmp_path / 'pit.db').as_posix()}", dialect="sqlite"))


def test_warehouse_keeps_raw_rows_but_pit_drops_impossible(
        tmp_path: Path) -> None:
    """仓库保留原始行（含错位行），PIT 层负责丢弃 —— 分层不能颠倒。"""
    rows = _fina_rows()
    warehouse = _warehouse(tmp_path)
    warehouse.upsert("fina_indicator_vip", rows)

    stored = warehouse.load("fina_indicator_vip")
    # 5 行原始数据里，1 行是整行副本 → 入库去重后 4 行（含错位行）
    assert len(stored) == 4, "仓库应保留 4 个不同键（含错位行）"
    assert ("603400", "20260630", "20260422") in set(
        map(tuple, stored[KEY].astype(str).to_numpy())), \
        "仓库作为存储层不应替上层做语义判断"

    panel = PitPanel(rows)
    assert len(panel) == 3, "PIT 面板应丢掉整行副本与错位行"
    assert panel.dropped_impossible == 1
    warehouse.close()


def test_csv_and_db_paths_agree_on_values(tmp_path: Path) -> None:
    """两条路径在**共有键**上的取值必须逐列相同。"""
    rows = _fina_rows()

    csv_store = DatasetStore("fina_indicator_vip", root=tmp_path / "csv")
    csv_store.write("20260331", rows)
    csv_panel = PitPanel(csv_store.read("20260331"))

    warehouse = _warehouse(tmp_path)
    warehouse.upsert("fina_indicator_vip", rows)
    db_frame = warehouse.load("fina_indicator_vip")
    warehouse.close()

    left = csv_panel.records.copy()
    right = db_frame.copy()
    for frame in (left, right):
        frame["code"] = frame["code"].astype(str).str.zfill(6)

    left_keys = set(map(tuple, left[KEY].astype(str).to_numpy()))
    right_keys = set(map(tuple, right[KEY].astype(str).to_numpy()))
    shared = left_keys & right_keys
    assert shared, "应当有共有键可比"

    index = pd.MultiIndex.from_tuples(sorted(shared), names=KEY)
    left_indexed = left.set_index(KEY).reindex(index)
    right_indexed = right.set_index(KEY).reindex(index)
    for column in METRICS:
        a = pd.to_numeric(left_indexed[column], errors="coerce")
        b = pd.to_numeric(right_indexed[column], errors="coerce")
        assert (a.fillna(-999) - b.fillna(-999)).abs().max() < 1e-9, \
            f"列 {column} 在两条存储路径上不一致"


def test_pit_panel_does_not_depend_on_storage_backend(tmp_path: Path) -> None:
    """端到端：同一份数据经 CSV 与经数据库构造出的 PitPanel，快照必须相同。

    列顺序允许不同（库表按建表/反射顺序返回，实际是字母序；CSV 保持原始列序）——
    因子与面板代码一律**按列名**取值，顺序不影响结果。
    这里按列名排序后再比对，让断言盯住"取值"而不是"顺序"。
    """
    rows = _fina_rows()
    csv_store = DatasetStore("fina_indicator_vip", root=tmp_path / "csv2")
    csv_store.write("20260331", rows)
    from_csv = PitPanel(csv_store.load(csv_store.keys()))

    warehouse = _warehouse(tmp_path)
    warehouse.upsert("fina_indicator_vip", rows)
    from_db = PitPanel(warehouse.load("fina_indicator_vip"))
    warehouse.close()

    date = "20260430"
    snapshot_csv = from_csv.as_of(date).sort_index().sort_index(axis=1)
    snapshot_db = from_db.as_of(date).sort_index().sort_index(axis=1)
    pd.testing.assert_frame_equal(snapshot_csv, snapshot_db)
