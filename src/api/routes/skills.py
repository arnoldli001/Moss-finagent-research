"""技能目录REST路由（PTD三级加载的查询侧）。

- GET /api/skills/quality          质量门扫描结果
- GET /api/skills                  全部agent的L0技能索引
- GET /api/skills/{agent_id}       指定agent的L0技能索引
- GET /api/skills/{agent_id}/{skill_name}  L1技能正文
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from src.core.errors import brief
from src.domain.skills.library import SkillLibrary

router = APIRouter(prefix="/api/v1", tags=["skills"])

_library: SkillLibrary | None = None


def _lib() -> SkillLibrary:
    global _library  # noqa: PLW0603
    if _library is None:
        _library = SkillLibrary("skills")
    return _library


@router.get("/skills/quality")
async def skill_quality() -> dict[str, object]:
    """技能库质量门：frontmatter完整性/token预算/references存在性 **+ 覆盖对账**。

    ## 为什么要带 `coverage`（`CHG-0192`）

    `problems == []` 单独看**区分不了**两件事：
      · 所有技能都合格；
      · 大部分技能**根本没被扫到**（引擎只认 `*/*/SKILL.md` 这一种形态）。
    实测：磁盘上 32 个 SKILL.md，质量门只看 25 个 —— 而它报"0 problems"。
    这是"护栏护的是死文件"那一类假绿：**保护了 78%，报告说 100%**。

    `coverage.unknown` 非空 = 有技能谁都没看过；`coverage.non_engine` 是
    **已显式登记**（`SkillLibrary.NON_ENGINE_SKILLS`）暂不校验的那批。
    """
    lib = _lib()
    problems = lib.validate_all()
    return {
        "ok": not problems,
        "problems": problems,
        "total": len(problems),
        "coverage": lib.coverage_report(),
    }


@router.get("/skills")
async def all_skill_indexes() -> dict[str, object]:
    """全部agent的L0技能索引（agent_id → 技能摘要列表）。"""
    return {agent_id: _lib().list_skills(agent_id) for agent_id in _lib().known_agents()}


@router.get("/skills/{agent_id}")
async def skill_indexes(agent_id: str) -> dict[str, object]:
    """指定agent的L0技能索引（frontmatter摘要，不含正文）。"""
    indexes = _lib().list_skills(agent_id)
    if not indexes:
        raise HTTPException(status_code=404, detail=f"agent {agent_id} 无可用技能")
    return {"agent_id": agent_id, "skills": indexes}


@router.get("/skills/{agent_id}/{skill_name}")
async def skill_detail(agent_id: str, skill_name: str) -> dict[str, object]:
    """L1技能正文（SKILL.md全文）。"""
    try:
        content = _lib().load_skill(agent_id, skill_name)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=brief(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=brief(exc)) from exc
    return {"agent_id": agent_id, "skill_name": skill_name, "content": content}
