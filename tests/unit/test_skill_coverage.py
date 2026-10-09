"""技能质量门的**覆盖对账**判据（`CHG-0192`）。

## 这条判据防的是什么

`validate_all()` 只 glob `*/*/SKILL.md`（引擎形态）。而磁盘上**还有**：

| 形态 | 数量 | 例子 |
|---|---|---|
| 引擎形态（两级） | **25** | `skills/policy-analyst/policy-intensity-assessment/SKILL.md` |
| 一级主题型 | **6** | `skills/ai-dev-loop-discipline/SKILL.md` |
| 文件名不是 `SKILL.md` | **1** | `skills/finagent-lessons/踩坑清单与举一反三规则SKILL.md` |

后两类**共 7 个**从不被任何判据看过，而 `validate_all()` 报的是 **`0 problems`** ——
**假绿**：它们可以有语法坏的 frontmatter、可以声明不存在的 references，没人会发现。
这就是"护栏护的是死文件"那一类失效（护了 78%，报告说 100%）。

## 判据强度

- `test_every_skill_md_on_disk_is_either_validated_or_allowlisted` —— **行为判据**：
  磁盘上的 SKILL.md 集合必须**等于** `已校验 ∪ 白名单`。
  往 `skills/` 扔一个新的、没登记的一级技能 ⇒ **立刻红**（盲区不能再静默增长）。
- `test_public_api_reports_the_gap` —— 走 `GET /api/v1/skills/quality` 那条路，
  断言它能把"盲区"报出来（不是只有直接调方法才知道）。
"""

from __future__ import annotations

from pathlib import Path

from src.domain.skills.library import SkillLibrary

ROOT = Path("skills")


def _lib() -> SkillLibrary:
    return SkillLibrary(ROOT)


def _disk_skill_files() -> set[str]:
    """磁盘上所有"看起来是技能"的文件（任意层级、文件名以 `SKILL.md` 结尾）。"""
    return {
        str(p.relative_to(ROOT)).replace("\\", "/")
        for p in ROOT.rglob("*.md")
        if p.name.endswith("SKILL.md")
    }


def test_every_skill_md_on_disk_is_either_validated_or_allowlisted() -> None:
    """★ 核心判据：磁盘上的 SKILL.md **必须** = 已校验 ∪ 白名单，盲区为空。

    往 `skills/` 加一个一级技能（比如 `skills/foo/SKILL.md`）而没登记
    `NON_ENGINE_SKILLS` ⇒ 本用例红。
    """
    if not ROOT.is_dir():                       # pragma: no cover
        return
    report = _lib().coverage_report()
    assert report["unknown"] == [], (
        "以下 SKILL.md **既不在引擎形态、也没登记** —— 没有任何判据看过它"
        "（质量门对它是假绿）：\n  " + "\n  ".join(report["unknown"])
        + "\n两条出路：① 规范成 `skills/<agent>/<skill>/SKILL.md` 让引擎接管；"
          "② 在 `SkillLibrary.NON_ENGINE_SKILLS` 里显式登记（并写清为什么）。"
    )


def test_coverage_is_the_union_of_validated_and_allowlisted() -> None:
    """双向对账：`已校验 ∪ 白名单 == 磁盘集合`（缺一边都不算验过）。"""
    if not ROOT.is_dir():                       # pragma: no cover
        return
    report = _lib().coverage_report()
    covered = set(report["validated"]) | set(report["non_engine"])
    on_disk = _disk_skill_files()
    assert on_disk - covered == set(), f"有文件没被覆盖：{sorted(on_disk - covered)}"
    assert covered - on_disk == set(), (
        f"白名单/校验清单里有磁盘上不存在的条目（护栏护的是死文件）："
        f"{sorted(covered - on_disk)}"
    )


def test_validated_set_is_nonempty_and_really_exists() -> None:
    """反证：不能靠"什么都没扫到"让上面两条恒真（那是最廉价的假绿）。"""
    report = _lib().coverage_report()
    assert len(report["validated"]) >= 20, (
        f"引擎形态技能只有 {len(report['validated'])} 个 —— 扫描面疑似失效"
    )
    for rel in report["validated"]:
        assert (ROOT / rel).is_file(), f"报告里的 {rel} 在磁盘上不存在"


def test_allowlist_entries_carry_a_reason() -> None:
    """白名单每一项都必须**真实存在**（防止它长成历史垃圾场）。

    与 `secret_scan` 的"豁免棘轮"同一思路：压不住东西的豁免就是残留。
    """
    for rel in SkillLibrary.NON_ENGINE_SKILLS:
        assert (ROOT / rel).is_file(), (
            f"白名单里的 {rel} 已不存在 ⇒ 请从 NON_ENGINE_SKILLS 删掉这一行"
        )


def test_public_api_reports_the_gap() -> None:
    """★ 公开 API 必须能报出盲区 —— 否则"看不见"这件事只有测试知道。

    走 `GET /api/v1/skills/quality`（那是运维/CI 唯一能看到质量门的地方）。
    """
    import asyncio

    from src.api.routes import skills as skills_route

    payload = asyncio.run(skills_route.skill_quality())
    assert "coverage" in payload, (
        "质量门端点不返回覆盖率 ⇒ 调用方无法区分"
        "「全都好」与「大部分根本没看」"
    )
    cov = payload["coverage"]
    assert cov["unknown"] == [], f"公开 API 报出盲区：{cov['unknown']}"
    assert cov["validated"] and cov["non_engine"] is not None
