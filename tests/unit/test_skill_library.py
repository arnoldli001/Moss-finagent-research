"""SkillLibrary 单测：PTD三级加载、边界控制、质量门。

目录结构为两级：skills/{agent_dir}/{skill_name}/SKILL.md。
"""

from pathlib import Path

import pytest

from src.domain.skills.library import SkillLibrary


def make_skill(
    root: Path, rel_dir: str, *,
    name: str | None = None, description: str = "当需要估值时使用，触发短语：估值、DCF。",
    refs: list[str] | None = None, body: str = "# 正文\n执行步骤。",
    ref_contents: dict[str, str] | None = None,
) -> None:
    """在root下构造一个最小SKILL.md（rel_dir形如 "agent-dir/skill-name"）。"""
    lines = ["---"]
    if name is not None:
        lines.append(f"name: {name}")
    if description:
        lines.append(f"description: {description}")
    if refs is not None:
        lines.append("references:")
        lines.extend(f"  - {r}" for r in refs)
    lines.append("---")
    skill_dir = root / rel_dir
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text("\n".join(lines) + "\n" + body, encoding="utf-8")
    for ref_name, content in (ref_contents or {}).items():
        (skill_dir / ref_name).parent.mkdir(parents=True, exist_ok=True)
        (skill_dir / ref_name).write_text(content, encoding="utf-8")


@pytest.fixture()
def lib_root(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    root.mkdir()
    make_skill(root, "valuation-agent/multi-method-valuation",
               name="multi-method-valuation", refs=["references/methods.md"],
               ref_contents={"references/methods.md": "# DCF方法详解"})
    make_skill(root, "risk-assessor/risk-scan", name="risk-scan")
    return root


def _lib(root: Path) -> SkillLibrary:
    return SkillLibrary(root, {"A10_micro": ["valuation-agent"], "A11_fin_risk": ["risk-assessor"]})


class TestPTDLoading:
    def test_l0_index_only_contains_frontmatter(self, lib_root: Path) -> None:
        lib = _lib(lib_root)
        idx = lib.list_skills("A10_micro")
        assert len(idx) == 1
        assert idx[0]["name"] == "multi-method-valuation"
        assert "description" in idx[0] and "tags" in idx[0]
        assert "正文" not in str(idx[0])  # L0不泄露正文

    def test_l1_load_full_skill(self, lib_root: Path) -> None:
        lib = _lib(lib_root)
        text = lib.load_skill("A10_micro", "multi-method-valuation")
        assert "执行步骤" in text  # 正文可见
        assert text.startswith("---")  # frontmatter保留

    def test_l1_load_by_directory_name(self, lib_root: Path) -> None:
        lib = _lib(lib_root)
        assert "执行步骤" in lib.load_skill("A10_micro", "multi-method-valuation")

    def test_l2_reference_whitelisted(self, lib_root: Path) -> None:
        lib = _lib(lib_root)
        content = lib.load_reference("A10_micro", "multi-method-valuation",
                                     "references/methods.md")
        assert "DCF" in content

    def test_l2_reference_not_declared_rejected(self, lib_root: Path) -> None:
        lib = _lib(lib_root)
        with pytest.raises(PermissionError):
            lib.load_reference("A10_micro", "multi-method-valuation", "references/other.md")

    def test_l2_path_traversal_rejected(self, tmp_path: Path) -> None:
        make_skill(tmp_path, "evil-agent/evil-skill", refs=["../../secret.md"],
                   ref_contents={"../../secret.md": "top secret"})
        evil_lib = SkillLibrary(tmp_path, {"A10_micro": ["evil-agent"]})
        with pytest.raises(PermissionError):
            evil_lib.load_reference("A10_micro", "evil-skill", "../../secret.md")

    def test_missing_directory_skipped_in_l0(self, lib_root: Path) -> None:
        lib = SkillLibrary(lib_root, {"A08_macro": ["policy-analyst", "calendar-agent"]})
        assert lib.list_skills("A08_macro") == []  # 未落盘目录静默跳过


class TestBoundary:
    def test_agent_cannot_access_others_skill(self, lib_root: Path) -> None:
        lib = _lib(lib_root)
        # 越界访问与不存在不可区分（LookupError），避免泄露他agent技能存在性
        with pytest.raises(LookupError):
            lib.load_skill("A10_micro", "risk-scan")
        with pytest.raises(LookupError):
            lib.load_skill("A10_micro", "unknown-skill")

    def test_unknown_agent_gets_nothing(self, lib_root: Path) -> None:
        lib = _lib(lib_root)
        assert lib.list_skills("A99_unknown") == []
        with pytest.raises(LookupError):
            lib.load_skill("A99_unknown", "multi-method-valuation")


class TestQualityGate:
    def test_validate_clean_library(self, lib_root: Path) -> None:
        assert _lib(lib_root).validate_all() == []

    def test_missing_name_and_description_flagged(self, lib_root: Path) -> None:
        make_skill(lib_root, "bad-agent/bad-skill", name=None,
                   description="这是一个长度足够的描述说明。")
        make_skill(lib_root, "no-desc-agent/x-skill", name="x-skill", description="")
        lib = SkillLibrary(lib_root, {})
        problems = "\n".join(lib.validate_all())
        assert "bad-agent/bad-skill" in problems and "name必填" in problems
        assert "no-desc-agent/x-skill" in problems and "description必填" in problems

    def test_oversized_body_flagged(self, lib_root: Path) -> None:
        make_skill(lib_root, "fat-agent/fat-skill", body="长" * 12000)
        problems = SkillLibrary(lib_root, {}).validate_all()
        assert any("fat-agent" in p and "超预算" in p for p in problems)

    def test_missing_reference_file_flagged(self, lib_root: Path) -> None:
        make_skill(lib_root, "dangling-agent/dangling-skill", refs=["references/ghost.md"])
        problems = SkillLibrary(lib_root, {}).validate_all()
        assert any("ghost.md" in p for p in problems)

    def test_short_description_flagged(self, lib_root: Path) -> None:
        make_skill(lib_root, "terse-agent/terse-skill", description="太短")
        problems = SkillLibrary(lib_root, {}).validate_all()
        assert any("terse-agent" in p and "过短" in p for p in problems)


def test_real_skills_dir_smoke() -> None:
    """真实skills目录冒烟：已落盘的技能必须全部通过质量门。"""
    root = Path("skills")
    if not list(root.glob("*/*/SKILL.md")):
        pytest.skip("技能文件尚未生成")
    problems = SkillLibrary(root).validate_all()
    assert problems == [], "\n".join(problems)
