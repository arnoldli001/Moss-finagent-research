"""测试选中器本身的护栏 —— 防「高危清单腐烂」。

## 为什么需要它

`scripts/affected_tests.py` 让"只跑相关测试"这条规则立得住（否则下一个人
因为怕漏会继续跑全量）。但它有一个**会腐烂的部分**：`HIGH_FANOUT_HINT`
那份"改到就必须跑全量"的名单。

名单腐烂的方式很安静：新加一个被 200 个测试引用的共享模块，名单没更新，
于是某天有人只跑"相关测试"、全绿、推送 —— 而那 200 个里挂了一个。

所以本文件**实测扇出**，而不是信任那份名单。

## 自证要求（AGENTS.md）

选中器必须**先自证**：喂已知答案，确认它能报对。这里用两条
互补的已知答案 —— 正例（必中）与负例（必空）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: ⚠️ `scripts/` 默认不入库（`.gitignore` 政策）。本测试会入库、脚本可能不会，
#: 所以必须显式跳过而不是假装通过（同 test_preflight_check.py 的处理）。
_SCRIPT = ROOT / "scripts" / "affected_tests.py"
if not _SCRIPT.exists():
    pytest.skip(
        "scripts/affected_tests.py 未随仓库发布（scripts/ 默认不入库）；"
        "该护栏只在开发机生效。发布方式见 tests/unit/test_shipped_deps.py",
        allow_module_level=True)

from scripts import affected_tests as at  # noqa: E402


# ============================================================
# ① 自证：正例必中、负例必空
# ============================================================


def test_selector_finds_a_known_dependency():
    """正例：改 `planner.py` **必须**选中 `test_planner_budget.py`。

    这是已知答案 —— 该测试文件顶部就写着"规划层延迟预算护栏"，
    它 import 了 planner。选不中说明选中器坏了。
    """
    picked = at.affected(["src/orchestration/planner.py"])
    assert "tests/unit/test_planner_budget.py" in picked, (
        f"改 planner.py 没选中 test_planner_budget.py；实际选中 "
        f"{len(picked)} 个：{picked[:10]}")


def test_selector_returns_nothing_for_pure_docs():
    """负例：纯文档改动不该选中任何测试（否则规则失去意义）。"""
    assert at.affected(["docs/ADR_PLANNING_TIER.md"]) == []


def test_selector_reduces_scope_materially():
    """选中器必须**真的缩小范围**，否则规则立不住（大家还是会跑全量）。"""
    picked = at.affected(["src/orchestration/planner.py"])
    total = len(list(ROOT.glob("tests/**/*.py")))
    assert total > 0
    assert len(picked) < total / 2, (
        f"改一个文件选中了 {len(picked)}/{total} 个测试文件 —— "
        "缩得不明显，规则没有实际收益")


# ============================================================
# ② 高危清单不许腐烂（**实测扇出**）
# ============================================================


#: 扇出阈值：选中测试文件数 ≥ 此值即视为"高扇出"，必须在名单里。
_FANOUT_THRESHOLD = 20

#: 至少要覆盖的候选（已知被大量测试引用）。
_MUST_WATCH = (
    "src/infrastructure/llm/models.py",    # TaskTier 字面量
    "src/orchestration/supervisor.py",     # 白名单 / 贯通点清单
    "configs/models.yaml",                 # 每层路由
)


def test_high_fanout_candidates_are_actually_high_fanout():
    """★ 实测：本仓库确实存在"改一个文件波及几十个测试"的文件。

    如果这条红了（没人高扇出），说明仓库结构变了 —— 那时可以放宽规则，
    但必须是**基于实测**的决定，不是把它忘掉。
    """
    fanouts = {f: len(at.affected([f])) for f in _MUST_WATCH}
    worst = max(fanouts.values())
    assert worst >= _FANOUT_THRESHOLD, (
        f"最高扇出只有 {worst}（候选 {fanouts}）—— 阈值 {_FANOUT_THRESHOLD} "
        "已不适用，请按实测重定，而不是删掉这条判据")


def test_every_high_fanout_file_is_in_the_hint_list():
    """★ 核心护栏：**实测扇出大的文件都必须在 `HIGH_FANOUT_HINT` 里**。

    这条防的是名单腐烂：新加一个共享模块（如又一个被全仓库 import 的
    常量文件），名单没更新 → 某天只跑"相关测试"、全绿、推送，而漏掉的那个挂了。
    """
    hint = set(at.HIGH_FANOUT_HINT)
    missing: dict[str, int] = {}
    for f in _MUST_WATCH:
        if f in hint:
            continue
        n = len(at.affected([f]))
        if n >= _FANOUT_THRESHOLD:
            missing[f] = n
    assert not missing, (
        f"这些文件实测高扇出却不在 HIGH_FANOUT_HINT 里：{missing}"
        "（改到它们时选中器不会提示跑全量）")


def test_hint_list_has_no_stale_entries():
    """名单也不许留着已不存在的文件（悬空条目 = 永远不会触发）。"""
    stale = [f for f in at.HIGH_FANOUT_HINT if not (ROOT / f).exists()]
    assert not stale, f"HIGH_FANOUT_HINT 里的文件已不存在：{stale}"


# ============================================================
# ③ 判据本身可信（别把"选中 0 个"当成功）
# ============================================================


def test_import_scan_actually_reads_tests():
    """自证：索引必须真的读到测试，否则全部判据空转。"""
    mods, paths = at._build_index()  # noqa: SLF001
    assert len(mods) > 50, f"只索引到 {len(mods)} 个测试文件 —— 扫描范围坏了"
    assert any("src." in m for deps in mods.values() for m in deps), (
        "没有任何测试被解析出 src.* 引用 —— ast 扫描没生效")
