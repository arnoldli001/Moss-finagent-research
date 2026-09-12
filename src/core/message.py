"""Agent间标准消息格式（ai-dev-rules 1.1 强制规范）。

消息必须包含 message_id、sender、receiver、message_type、timestamp、
payload、metadata；metadata 必须包含 audit_id 和 data_sources。
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from src.core.exceptions import MessageFormatError


class StandardMessageType(str, Enum):
    """系统级标准消息类型；业务类型遵循 {domain}.{action} 命名。"""

    TASK_DISPATCH = "system.task_dispatch"
    TASK_RESULT = "system.task_result"
    ERROR = "system.error"
    HEARTBEAT = "system.heartbeat"


class MessageMetadata(BaseModel):
    """消息元数据（审计必填字段）。"""

    audit_id: str = Field(default_factory=lambda: f"audit_{uuid4().hex}")
    data_sources: list[str] = Field(default_factory=list)
    version: str = "1.0"


class Message(BaseModel):
    """Agent间标准JSON消息。"""

    message_id: str = Field(default_factory=lambda: f"msg_{uuid4().hex}")
    sender: str
    receiver: str
    message_type: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: MessageMetadata = Field(default_factory=MessageMetadata)

    @field_validator("message_type")
    @classmethod
    def _check_message_type(cls, value: str) -> str:
        """校验消息类型：标准类型或 {domain}.{action} 格式。"""
        if value in {item.value for item in StandardMessageType}:
            return value
        parts = value.split(".")
        if len(parts) != 2 or not all(parts):
            raise ValueError(
                f"消息类型必须为 {{domain}}.{{action}} 格式或标准类型，收到: {value!r}"
            )
        return value


def build_message(
    sender: str,
    receiver: str,
    message_type: str,
    payload: dict[str, Any],
    data_sources: list[str] | None = None,
) -> Message:
    """构造标准消息；data_sources 写入 metadata 以满足审计要求。"""
    try:
        return Message(
            sender=sender,
            receiver=receiver,
            message_type=message_type,
            payload=payload,
            metadata=MessageMetadata(data_sources=data_sources or []),
        )
    except ValueError as exc:
        raise MessageFormatError(str(exc)) from exc
