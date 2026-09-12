"""core层：BaseAgent、Message、异常、配置、共享Schema。

core层不包含业务逻辑；除Pydantic生态（pydantic/pydantic-settings）外不依赖外部库。
"""

from src.core.base_agent import BaseAgent
from src.core.config import Settings, get_settings
from src.core.exceptions import FinAgentError
from src.core.message import Message, MessageMetadata, StandardMessageType, build_message
from src.core.models import AgentInput, AgentOutput
from src.core.schemas import (
    Confidence,
    DataPoint,
    DataSourceRef,
    DataSourceType,
    FetchMethod,
    TraceStep,
    hash_content,
)
from src.core.state import ResearchState

__all__ = [
    "AgentInput",
    "AgentOutput",
    "BaseAgent",
    "Confidence",
    "DataPoint",
    "DataSourceRef",
    "DataSourceType",
    "FetchMethod",
    "FinAgentError",
    "Message",
    "MessageMetadata",
    "ResearchState",
    "Settings",
    "StandardMessageType",
    "TraceStep",
    "build_message",
    "get_settings",
    "hash_content",
]
