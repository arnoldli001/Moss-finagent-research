"""Agent 展示元数据：agent_id → 中文名 / 层级，置信度枚举 → 中文。

单一事实源：configs/agents.yaml（前端展示与最终报告均取此处的 name）。
内置兜底表与运行时 ID 保持一致，yaml 缺失/解析失败时仍可正常展示。

注意：agent_id 是内部路由的稳定标识（ask_agent 工具、LangGraph 节点、
缓存隔离都依赖它），严禁把中文名当作路由 key；本模块仅用于"展示层"转换。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "configs" / "agents.yaml"

# 置信度英文枚举 → 中文展示
CONFIDENCE_ZH: dict[str, str] = {"high": "高", "medium": "中", "low": "低"}

# 内置兜底（与 configs/agents.yaml 的 id/name 保持同步）
_FALLBACK: dict[str, dict[str, str]] = {
    "A01_data_collector": {"name": "数据采集Agent", "layer": "data"},
    "A02_data_cleaner": {"name": "数据清洗Agent", "layer": "data"},
    "A03_data_validator": {"name": "数据校验Agent", "layer": "data"},
    "A04_data_storage": {"name": "数据存储Agent", "layer": "data"},
    "A05_verifier": {"name": "信息核验Agent", "layer": "info"},
    "A06_extractor": {"name": "事件提取Agent", "layer": "info"},
    "A07_sentiment": {"name": "舆情分析Agent", "layer": "info"},
    "A08_macro": {"name": "宏观分析Agent", "layer": "analysis"},
    "A09_meso": {"name": "中观分析Agent", "layer": "analysis"},
    "A10_micro": {"name": "微观分析Agent", "layer": "analysis"},
    "A11_fin_risk": {"name": "财务风险Agent", "layer": "analysis"},
    "A12_compliance": {"name": "合规风险Agent", "layer": "analysis"},
    "A13_tech": {"name": "科技行业Agent", "layer": "industry"},
    "A14_consumer": {"name": "消费行业Agent", "layer": "industry"},
    "A15_cyclical": {"name": "周期行业Agent", "layer": "industry"},
    "A16_pharma": {"name": "医药行业Agent", "layer": "industry"},
    "A17_recommend": {"name": "投研建议Agent", "layer": "decision"},
    "A18_audit": {"name": "逻辑审计Agent", "layer": "audit"},
}


@lru_cache(maxsize=1)
def load_agent_meta() -> dict[str, dict[str, str]]:
    """加载 configs/agents.yaml；失败时降级内置兜底表。"""
    try:
        import yaml

        raw: dict[str, Any] = yaml.safe_load(
            _CONFIG_PATH.read_text(encoding="utf-8")
        ) or {}
        agents = raw.get("agents") or {}
        meta: dict[str, dict[str, str]] = {}
        for agent_id, spec in agents.items():
            if not isinstance(spec, dict) or not spec.get("name"):
                continue
            meta[str(agent_id)] = {
                "name": str(spec["name"]),
                "layer": str(spec.get("layer", "")),
            }
        # yaml 不完整时用兜底表补齐，保证任何运行时 ID 都能展示
        for agent_id, spec in _FALLBACK.items():
            meta.setdefault(agent_id, spec)
        return meta
    except Exception:  # noqa: BLE001 展示层转换不能因配置问题阻断主链路
        return dict(_FALLBACK)


def agent_name(agent_id: str | None) -> str:
    """agent_id → 中文名；未知 ID 原样返回（避免吞掉可排查信息）。"""
    if not agent_id:
        return "未知Agent"
    return load_agent_meta().get(agent_id, {}).get("name", agent_id)


def confidence_zh(level: str | None) -> str:
    """high/medium/low → 高/中/低；未知值原样返回。"""
    if not level:
        return "未知"
    return CONFIDENCE_ZH.get(str(level).lower(), str(level))


def agent_meta_table() -> dict[str, dict[str, str]]:
    """全量元数据表（供 API 下发前端）。"""
    return load_agent_meta()
