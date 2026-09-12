# Agent接口规范

## BaseAgent抽象接口

所有Agent必须实现以下接口：

```python
from abc import ABC, abstractmethod
from pydantic import BaseModel

class AgentInput(BaseModel):
    task_id: str
    tenant_id: str
    user_context: dict
    payload: dict

class AgentOutput(BaseModel):
    task_id: str
    agent_id: str
    conclusion: str
    confidence: str  # high/medium/low
    data_refs: list[str]
    trace_id: str
    reasoning_steps: list[dict]

class BaseAgent(ABC):
    @abstractmethod
    async def execute(self, input: AgentInput) -> AgentOutput:
        """执行Agent任务，返回结构化输出"""
        pass

    @abstractmethod
    def get_capabilities(self) -> dict:
        """返回Agent能力描述，用于Supervisor路由"""
        pass

    @abstractmethod
    def health_check(self) -> bool:
        """健康检查"""
        pass
```

## 消息格式

所有Agent间消息必须遵循以下格式：

```json
{
  "message_id": "msg_{uuid}",
  "sender": "A08_macro",
  "receiver": "A17_advisory",
  "message_type": "macro.cycle_report",
  "timestamp": "ISO8601",
  "payload": {
    "conclusion": "...",
    "confidence": "high",
    "data_snapshot": {},
    "reasoning_path": []
  },
  "metadata": {
    "audit_id": "audit_{uuid}",
    "data_sources": ["data.stats.gov.cn"],
    "version": "1.0"
  }
}
```
