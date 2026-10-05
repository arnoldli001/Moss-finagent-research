"""指标别名：**没有死胡同**（2026-10-01，`CHG-0155`）。

## 现场（实测，两版口径都错过，留在这里当反面教材）

这一项我量错过两次，先记下来：

* **边对称**（"A→B 有、B→A 无"）—— 表结构天生有向（`canonical → (别名…)`），
  不对称是常态，测出来 241/261/5801 条"不对称"里绝大多数是**设计如此**；
* **目标集合相等** —— 会**打平口径差异**：`股息率`→`dv_ratio`（静态）与
  `股息率TTM`→`dv_ttm`（滚动）**必须**不同（实测 600036 `dv_ratio=5.05` /
  `dv_ttm=5.76`，是两个不同的数）。要求集合相等 = 要求合并两个口径，
  那才是真缺陷。实测 281 组里 181 组"不相等"，但那是**特性**不是缺陷。

**真正该锁的是客户视角的两条**（本文件钉它们）：

1. **没有死胡同**：表里出现过的**任何**名字（键或目标）问下去都必须有结果。
   实测修复前 **215 个名字里有 32 个返回 `[]`** —— 全是**目标名**
   （`dividend_yield` / `close_basic` / `debt_to_assets` / `high` / `low` /
   `open` / `stock_close` / `vol` / `roe_sina` / `idx_val:snapshot:all` …），
   而这些**正是连接器与 planner 会直接吐出的指标 id**。
   "数据在库里、某个名字进不去" —— 与 `CHG-0136` 那次「采了白采」同一形状。
2. **列名自先**：按列名问，它必须排在自己那条的第一位（口径不被别的挤掉）。

修法是**生成**而不是靠人记得：`augment_targets_as_keys()` 在导入期把
"只作为目标出现"的名字补成键（同族 = 所有含它的行的目标并集，自己排第一）。
"""
from __future__ import annotations

from src.infrastructure.catalog import synonym_dict as sd

#: 修复前返回 `[]` 的样本（连接器/planner 会直接吐出的 id）
FORMERLY_DEAD = ("dividend_yield", "close_basic", "debt_to_assets",
                 "stock_close", "roe_sina", "idx_val:snapshot:all")


def _all_names() -> set[str]:
    names: set[str] = set()
    for canonical, aliases in sd.metric_aliases().items():
        names.add(str(canonical))
        names.update(str(a) for a in aliases)
    return names


def test_no_metric_name_is_a_dead_end() -> None:
    """★★ 核心判据：表里出现过的每个名字问下去都要有结果。

    失败信息把**具体名字**列出来（"认不出"必须能定位到是谁认不出）。
    """
    dead = sorted(n for n in _all_names() if not sd.resolve_metric(n))
    assert not dead, (
        f"这些名字在别名表里出现过、却问不出任何目标（死胡同）：{dead}\n"
        "修法不是逐条手工加，而是 `augment_targets_as_keys()` 在导入期生成"
        "（人手写的反方向天生有漏）")


def test_former_dead_ends_now_resolve_with_themselves_first() -> None:
    """★ 列名自先：按列名问，第一条必须就是它自己（口径不能被同族挤掉）。"""
    for name in FORMERLY_DEAD:
        got = sd.resolve_metric(name)
        assert got, f"{name!r} 仍然问不出结果"
        assert sd.normalize_alias(got[0]) == sd.normalize_alias(name), (
            f"{name!r} 的第一条是 {got[0]!r} —— 按列名问必须先拿到它本身")


def test_augmentation_actually_generated_new_keys() -> None:
    """★ 生成必须真的发生（不是"恰好表里都有"）：条数大于 0，且能量到。"""
    assert sd._AUGMENTED_METRIC_KEYS > 0, (   # noqa: SLF001
        "一个键都没补 —— 要么表已经完备（那这条判据可以删），"
        "要么 `augment_targets_as_keys()` 没被调用（那 32 个死胡同会回来）")
    # `dividend_yield` 在手工表里**不是键**，只作为目标出现 ⇒ 它必须是补出来的
    assert sd.normalize_alias("dividend_yield") not in {
        sd.normalize_alias(k) for k in sd.metric_aliases()}


def test_selfproof_without_augmentation_the_dead_ends_come_back() -> None:
    """★★ 自证：把补键关掉，死胡同**必须**回来（证明判据有鉴别力）。

    做法：拿一份手工表的索引（不补键），在同一个名字上比 —— 不依赖模块状态，
    也就不会被"缓存已经建好"掩盖。
    """
    manual_index = sd._build_index(sd.metric_aliases(), "指标")   # noqa: SLF001
    key = sd.normalize_alias("dividend_yield")
    assert key not in manual_index, (
        "手工表里居然已经有这个键 ⇒ 这条自证证明不了生成的必要性")
    assert sd.resolve_metric("dividend_yield"), "生成后的索引应当能解析它"
