"""主线「冻结概念池」的读取与生效（2026-09-27）。

## 背景

用户口径：按 20 日胜率判据筛出的概念板块**写死在**主线池里，后续不再变动。
出处：`configs/mainline_theme_exclusions.yaml` 头部
    「本次：池内 138 个 → 剔除 16 个、留 122 个」

落地：`configs/mainline_frozen_pool.yaml`（122 条）+ `import_crowding_pool`
以它为**唯一权威**。

⚠️ **冻结值 122 → 124**（`CHG-0121`，2026-09-30）：用户裁定「只放过这两个题材」
—— `886015.TI 创新药` / `885927.TI CRO概念` 由人工放回池内（恢复前判据：
胜率 23.8% / 39.1%，已走满 21 / 23 个窗口）。判据见
`test_restored_themes_are_in_the_pool`。

⚠️ 这些测试的**关键不变量**是「读不到就退回动态口径」——
冻结文件坏掉/被删时，绝不能把主线池变成空的（那会让整条主线静默停摆）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

CONFIGS = Path(__file__).resolve().parents[2] / "configs"
REAL = "mainline_frozen_pool.yaml"
THEME_EXCLUSIONS = "mainline_theme_exclusions.yaml"

#: 2026-09-30 人工恢复的 2 个题材（用户裁定「只放过这两个题材」）。
#: ⚠️ 它们必须同时在**两处**被放过：冻结名单（决定能不能入池）与
#: 池级剔除清单（`boards()` 出口还会再挡一道）—— 只改一处等于没改，且不报错。
RESTORED_THEMES = {"886015.TI": "创新药", "885927.TI": "CRO概念"}


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


def test_shipped_frozen_pool_is_124_and_all_concept_prefixes() -> None:
    """随仓库发布的冻结名单：124 条，且全是 885/886 概念指数。

    `885/886` 是同花顺概念指数段；出现别的段（`881` 行业 / `700` 宽基 /
    `871` GICS）说明名单里混进了非概念板块 —— 那正是这次要清掉的东西。

    ⚠️ 124 是**冻结值**（122 + 2026-09-30 人工恢复的 2 个，`CHG-0121`）：
    它变了说明有人在动这条口径，必须是有意的。
    """
    from src.mainline.datastore import load_frozen_pool

    codes = load_frozen_pool(REAL)
    if codes is None:
        pytest.skip(f"{REAL} 不存在（未启用冻结口径）")
    assert len(codes) == 124, f"冻结名单应为 124 条，实际 {len(codes)}"
    bad = sorted(c for c in codes if not c.startswith(("885", "886")))
    assert not bad, f"名单里混进了非概念段：{bad[:5]}"


def test_restored_themes_are_in_the_pool() -> None:
    """**回归测试**：2026-09-30 人工恢复的 2 个题材必须两处都在。

    ## 它防的是哪一种故障

    「放过一个题材」要改**两处**才产生可观察行为：

      ① `configs/mainline_frozen_pool.yaml` —— 决定它能不能**入池**
         （`import_crowding_pool` 只保留名单内的）；
      ② `configs/mainline_theme_exclusions.yaml` 的 `codes:` —— `boards()`
         出口**再挡一道**（池级剔除：不打分 / 不告警 / 不再同步行情）。

    只改① → 板块进了 `ml_board`、却仍被 `boards()` 过滤掉：
    **库里有、接口里没有**，而全程零报错（面板上只表现为"改了名单但还是看不到"）。
    只改② → 它压根不入池，同样看不到。

    ⚠️ 第②处是**生成脚本的产物**（`scripts/build_theme_exclusions.py`），
    重新生成时若忘了带 `--keep 886015.TI --keep 885927.TI`，这两行会被写回去
    —— 这个测试就是那次"静默回退"的哨兵。
    """
    from src.mainline.config import load_theme_exclusions
    from src.mainline.datastore import load_frozen_pool

    frozen = load_frozen_pool(REAL)
    if frozen is None:
        pytest.skip(f"{REAL} 不存在（未启用冻结口径）")
    missing = sorted(set(RESTORED_THEMES) - frozen)
    assert not missing, (
        f"人工恢复的题材不在冻结名单里：{missing} —— "
        "它们会被重新挡在池外（见 mainline_frozen_pool.yaml 头部说明）")

    gated = sorted(set(RESTORED_THEMES) & load_theme_exclusions(THEME_EXCLUSIONS))
    assert not gated, (
        f"人工恢复的题材又出现在池级剔除清单里：{gated} —— "
        "重新生成清单时必须带 `--keep`，否则冻结名单改了也看不到")


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
