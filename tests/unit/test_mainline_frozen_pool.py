"""主线「冻结概念池」的读取与生效（2026-09-27）。

## 背景

用户口径：按 20 日胜率判据筛出的概念板块**写死在**主线池里，后续不再变动。
出处：`configs/mainline_theme_exclusions.yaml` 头部
    「本次：池内 138 个 → 剔除 16 个、留 122 个」

落地：`configs/mainline_frozen_pool.yaml`（122 条）+ `import_crowding_pool`
以它为**唯一权威**。

⚠️ 这些测试的**关键不变量**是「读不到就退回动态口径」——
冻结文件坏掉/被删时，绝不能把主线池变成空的（那会让整条主线静默停摆）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

CONFIGS = Path(__file__).resolve().parents[2] / "configs"
REAL = "mainline_frozen_pool.yaml"


def test_missing_file_returns_none() -> None:
    """文件不存在 → None（= 退回动态口径），**不是空集**。"""
    from src.mainline.datastore import load_frozen_pool

    assert load_frozen_pool("__no_such_file__.yaml") is None


def test_empty_boards_returns_none() -> None:
    """文件在但 `boards:` 为空 → 也返回 None。

    空集会被下游理解成"主线不该看任何板块" —— 那是灾难性的。
    写坏了更可能是这种情况，所以按"不可用"处理。
    """
    from src.mainline.datastore import load_frozen_pool

    path = CONFIGS / "_tmp_empty_frozen.yaml"
    path.write_text("version: 1\ncount: 0\nboards:\n", encoding="utf-8")
    try:
        assert load_frozen_pool(path.name) is None
    finally:
        path.unlink(missing_ok=True)


def test_malformed_yaml_returns_none_without_raising() -> None:
    """YAML 语法坏掉 → 返回 None 且**不抛**（坏清单不该让整条链挂掉）。"""
    from src.mainline.datastore import load_frozen_pool

    path = CONFIGS / "_tmp_bad_frozen.yaml"
    path.write_text("boards: [ this is not: valid: yaml\n", encoding="utf-8")
    try:
        assert load_frozen_pool(path.name) is None
    finally:
        path.unlink(missing_ok=True)


def test_entries_without_code_are_ignored() -> None:
    """缺 `code` 的条目跳过；全是废条目时结果等同于"不可用"。"""
    from src.mainline.datastore import load_frozen_pool

    path = CONFIGS / "_tmp_partial_frozen.yaml"
    path.write_text(
        "boards:\n"
        '  - { name: "没有代码" }\n'
        '  - { code: "885530.TI", name: "黄金概念" }\n',
        encoding="utf-8")
    try:
        got = load_frozen_pool(path.name)
        assert got == {"885530.TI"}
    finally:
        path.unlink(missing_ok=True)


def test_shipped_frozen_pool_is_122_and_all_concept_prefixes() -> None:
    """随仓库发布的冻结名单：122 条，且全是 885/886 概念指数。

    `885/886` 是同花顺概念指数段；出现别的段（`881` 行业 / `700` 宽基 /
    `871` GICS）说明名单里混进了非概念板块 —— 那正是这次要清掉的东西。

    ⚠️ 122 是**冻结值**：它变了说明有人在动这条口径，必须是有意的。
    """
    from src.mainline.datastore import load_frozen_pool

    codes = load_frozen_pool(REAL)
    if codes is None:
        pytest.skip(f"{REAL} 不存在（未启用冻结口径）")
    assert len(codes) == 122, f"冻结名单应为 122 条，实际 {len(codes)}"
    bad = sorted(c for c in codes if not c.startswith(("885", "886")))
    assert not bad, f"名单里混进了非概念段：{bad[:5]}"


def test_pool_is_subset_of_frozen_list() -> None:
    """真库集成：`ml_board` 必须**恰好**等于冻结名单（不是子集就说明有别的规则在插手）。"""

    from src.mainline.config import load_config
    from src.mainline.datastore import MainlineDataStore, load_frozen_pool

    frozen = load_frozen_pool(REAL)
    if frozen is None:
        pytest.skip("未启用冻结口径")
    store = MainlineDataStore(config=load_config())
    try:
        boards = {item.code for item in store.boards()}
    except Exception as exc:  # noqa: BLE001 无库/无表时跳过
        pytest.skip(f"板块池不可读：{type(exc).__name__}")
    if not boards:
        pytest.skip("板块池为空（尚未导入）")
    assert not (boards - frozen), (
        f"池里有冻结名单外的板块：{sorted(boards - frozen)[:5]}")
    assert not (frozen - boards), (
        f"冻结名单里有未入池的板块：{sorted(frozen - boards)[:5]}")
