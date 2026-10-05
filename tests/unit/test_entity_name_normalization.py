"""实体名的**归一与权威来源**：行情前缀 / 字段截断不许污染匹配（2026-10-01）。

## 现场（两个后果都真实发生了）

`data/security_names.json` 是行情侧名称快照，会带**当日前缀**
（`XD`/`XR`/`DR`/`N`/`C`/`ST`/`*ST`；实测 5,572 只里 **229 只**带前缀）：

1. **认不出**（客户可见）：表里是 `*ST三六五`，用户敲的是「三六五网」
   ⇒ `resolve_entity("三六五网")` 返回 **`[]`**（修复前实测）。
2. **派生键被污染**（判据被带偏）：`600028`（中国石化）在除息日快照里是
   **`XD中国石`** —— 2 字前缀 + 4 字名，撞上字段宽度被**截断成 3 字**。
   于是它的首字母从 `zgsh` 变成 `zgs`，**与「中国神华 601088」的撞车消失了**
   ⇒ 拼音唯一性判据判 `zgsh` 唯一 ⇒ 护栏要求收一条
   **按除息日固化的错别名**（下个交易日就翻车）。

修法两条（都不是"在别名表里补一行"）：

* `strip_market_prefix()`：前缀是**当天的行情标记**，不是名字的一部分；
  解析时两种写法都认（`XD中国石` 与 `中国石化` 都能命中）。
* `canonical_name()`：算拼音键这种**会被固化进代码**的派生量时，
  权威名取**静态表里的官方中文名**（不受显示层污染），快照只做兜底
  —— **判据的输入不能依赖被判据检查的那份脏数据**。

## 自证

`test_the_old_polluted_rule_reproduces_both_defects` 把修复前的两条行为
（不剥前缀、以快照为权威）就地重现，断言它们**必然**产出那两个缺陷 ——
证明上面几条判据有鉴别力，不是恒绿。
"""
from __future__ import annotations

import pytest

from src.infrastructure.catalog.synonym_dict import (
    ambiguous_pinyin_aliases,
    canonical_name,
    required_pinyin_aliases,
    resolve_entity,
    snapshot_name_conflicts,
    strip_market_prefix,
)

#: 修复前后都稳定的事实（行情侧代码与名字，不随快照变）
SHIHUA, SHENHUA, SANLIUWU = "600028", "601088", "300295"


# ======================================================================
# 一、前缀剥离：用户敲的是**自然名**，前缀只是当天的行情标记
# ======================================================================

@pytest.mark.parametrize("raw,expected", [
    ("XD中国石", "中国石"),
    ("*ST三六五", "三六五"),
    ("ST海王", "海王"),
    ("XR某股", "某股"),
    # ---- 不许误伤：剥完没中文就保持原样（防止吃掉 TCL 这类真名）----
    ("TCL科技", "TCL科技"),
    ("STAR", "STAR"),
    ("中国神华", "中国神华"),
])
def test_market_prefix_stripping(raw: str, expected: str) -> None:
    assert strip_market_prefix(raw) == expected


def test_natural_name_resolves_even_when_snapshot_has_a_prefix() -> None:
    """★ 客户可见的那条：用户敲「三六五网」，快照里只有 `*ST三六五`。"""
    assert resolve_entity("三六五网")[:1] == [SANLIUWU], (
        "自然名解析不出来 —— 名字表里的 ST 前缀把用户挡在门外")
    # 原样写法也必须继续能认（有人就是会贴行情软件上的写法）
    assert resolve_entity("*ST三六五")[:1] == [SANLIUWU]


# ======================================================================
# 二、权威名：派生量（拼音键）不许被显示层污染
# ======================================================================

def test_official_name_wins_over_the_polluted_snapshot() -> None:
    """★ `XD中国石` 是**截断**的，权威名必须回到「中国石化」。"""
    assert canonical_name(SHIHUA, "XD中国石") == "中国石化"


def test_transient_prefix_cannot_invent_a_unique_pinyin() -> None:
    """★★ 核心不变量：`zgsh` 同时属于中国石化与中国神华 ⇒ **不许**被当成唯一。

    修复前：快照的截断让 `zgsh` 只剩 601088 一个主人 ⇒ 护栏要求把它收进表里
    ⇒ 一条**按除息日固化**的错别名。这条判据钉的是"派生量的输入必须干净"。
    """
    ambiguous = ambiguous_pinyin_aliases()
    assert "zgsh" in ambiguous, (
        "`zgsh` 应当是歧义拼音（中国石化 / 中国神华）—— 它被算成唯一了，"
        "说明权威名又退化回了被污染的快照名")
    assert set(ambiguous["zgsh"]) >= {SHIHUA, SHENHUA}
    assert "zgsh" not in required_pinyin_aliases(), (
        "歧义拼音被当成‘必须收录’ ⇒ 收了就是认错")


def test_two_name_sources_must_align() -> None:
    """两处来源必须能对齐：快照名应当是权威名的**前缀**（或相等）。

    对齐不上说明发生了**更名**或换了口径 —— 那是要人看一眼的事件，
    不是可以静默吸收的差异（因此这里断言"当前无未对齐项"，
    一旦有，测试会把它列出来而不是让它沉下去）。
    """
    conflicts = snapshot_name_conflicts()
    assert not conflicts, (
        "快照名与权威名对不上（更名或截断形态变了），需要人工确认："
        f"{conflicts[:5]}")


# ======================================================================
# 三、自证：修复前的两条行为必须**重现**这两个缺陷
# ======================================================================

def test_the_old_polluted_rule_reproduces_both_defects() -> None:
    """★★ 自证：不剥前缀 + 以快照为权威 ⇒ 两个缺陷都必然出现。

    没有这一条，上面那些断言可能只是"恰好都成立"，而不是"因为修了才对"。
    """
    import json
    import re
    from pathlib import Path

    from pypinyin import Style, lazy_pinyin

    pairs = json.loads(
        Path("data/security_names.json").read_text(encoding="utf-8"))["pairs"]

    # ① 旧行为：名字原样进索引 ⇒ 自然名「三六五网」找不到
    old_index: dict[str, list[str]] = {}
    for code, name in pairs:
        key = re.sub(r"[\s_\-/()（）\[\]【】]", "", str(name)).replace(":", "").lower()
        old_index.setdefault(key, []).append(str(code))
    assert re.sub(r"[\s_\-/()（）\[\]【】]", "", "三六五网").lower() not in old_index, (
        "旧行为竟然也能解析「三六五网」⇒ 这条自证证明不了什么")

    # ② 旧行为：拼音键直接用快照名算 ⇒ `zgsh` 被算成唯一
    def old_initials(name: str) -> str:
        return "".join(lazy_pinyin(str(name), style=Style.FIRST_LETTER,
                                   errors=lambda x: list(x))).lower()

    owners: dict[str, list[str]] = {}
    for code, name in pairs:
        owners.setdefault(old_initials(name), []).append(str(code))
    assert owners.get("zgsh") == [SHENHUA], (
        f"旧行为下 `zgsh` 的主人应当是 [601088]，实际 {owners.get('zgsh')} —— "
        "现场已经变了，请重看这条自证的说明")
    assert len(owners["zgsh"]) == 1, "旧行为下它必须是‘唯一’，否则自证不成立"
