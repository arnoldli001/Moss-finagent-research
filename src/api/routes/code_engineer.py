"""A19编码实现Agent API：数据缺口自动修复与动态连接器管理。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.infrastructure.connectors.dynamic_loader import get_dynamic_loader

router = APIRouter(prefix="/api/v1/code-engineer", tags=["code-engineer"])


class FixGapRequest(BaseModel):
    gap_description: str = Field(..., description="数据缺口描述，如'未接入A股两市成交额'")
    indicator: str | None = Field(None, description="指标ID提示，如'mkt:turnover:total'")
    error_message: str | None = Field(None, description="原始错误信息（可选）")


@router.post("/fix-gap")
async def fix_gap(req: FixGapRequest) -> dict[str, Any]:
    """触发A19为指定数据缺口生成并注册动态连接器。"""
    from src.api.main import app

    runtime = getattr(app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(status_code=500, detail="运行时未初始化")
    agent = runtime.agents.get("A19_code_engineer")
    if agent is None:
        raise HTTPException(status_code=503, detail="A19编码实现Agent未注册")

    from src.core.models import AgentInput

    try:
        output = await agent.execute(AgentInput(
            task_id=f"ce_{__import__('uuid').uuid4().hex[:8]}",
            tenant_id="tenant_001",
            payload={
                "gap_description": req.gap_description,
                "indicator": req.indicator,
                "error_message": req.error_message,
            },
        ))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {
        "status": "ok",
        "conclusion": output.conclusion,
        "confidence": output.confidence.value,
        "reasoning_steps": [s.model_dump() for s in output.reasoning_steps],
        "result": output.result,
    }


@router.get("/connectors")
async def list_dynamic_connectors() -> dict[str, Any]:
    """列出已加载的动态连接器及其能力。"""
    loader = get_dynamic_loader()
    return {
        "count": len(loader.list_loaded()),
        "connectors": loader.list_loaded(),
    }


@router.post("/reload")
async def reload_dynamic_connectors() -> dict[str, Any]:
    """手动重新扫描并加载动态连接器目录。"""
    loader = get_dynamic_loader()
    routes = loader.reload()
    return {
        "count": len(routes),
        "connectors": loader.list_loaded(),
    }


@router.get("/known-sources")
async def known_sources() -> dict[str, Any]:
    """A19已知的免费数据源清单（供前端展示可自动接入的源）。"""
    from src.domain.agents.engineering.code_engineer.agent import KNOWN_SOURCES

    return {"sources": [{"key": k, "desc": v} for k, v in KNOWN_SOURCES.items()]}
