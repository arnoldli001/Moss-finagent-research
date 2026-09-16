"""技能目录REST路由（PTD三级加载的查询侧）。

- GET /api/skills/quality          质量门扫描结果
- GET /api/skills                  全部agent的L0技能索引
- GET /api/skills/{agent_id}       指定agent的L0技能索引
- GET /api/skills/{agent_id}/{skill_name}  L1技能正文
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

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
    """技能库质量门：frontmatter完整性/token预算/references存在性。"""
    problems = _lib().validate_all()
    return {"ok": not problems, "problems": problems, "total": len(problems)}


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
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"agent_id": agent_id, "skill_name": skill_name, "content": content}
