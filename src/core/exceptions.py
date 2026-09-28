"""项目异常层次。

所有自定义异常继承 FinAgentError，便于上层统一捕获；
禁止裸except，调用方应捕获具体异常类型。
"""


class FinAgentError(Exception):
    """项目异常基类。"""


class ConfigError(FinAgentError):
    """配置缺失或非法。"""


class MessageFormatError(FinAgentError):
    """Agent间消息格式不合法。"""


class AgentExecutionError(FinAgentError):
    """Agent执行失败。"""


class DataFetchError(FinAgentError):
    """数据源获取失败。"""


class DataValidationError(FinAgentError):
    """数据校验不通过。"""


class LLMGatewayError(FinAgentError):
    """模型网关调用失败。

    `count_as_failure` 表示这次失败**该不该计入熔断器**：

    - 瞬时故障（超时、连接失败、5xx、429 限流）→ True。它们会自愈，连续出现
      正是熔断器要拦的情况；
    - 配置类故障（key 未配置/401/402/403）→ False。它们**重试多少次都不会好**，
      计进去只会让一个纯配置问题迅速把熔断器打开，之后所有调用被立刻拒绝，
      日志里堆满 `circuit_open`，把真正的病因（缺 key）淹没 —— 实测踩过。

    `http_status` 是**结构化**的 HTTP 状态码（拿不到时为 None）。

    为什么必须带上它（2026-09-28 实测）：限流熔断的判据原先只做**文案**匹配
    （在异常消息里找 "429"），而那句话来自 `httpx.HTTPStatusError` 的默认
    文案 —— 用 httpx 自己的实现细节当判据，它一改写法判据就**静默失效**，
    表现为"护栏装了但从来不锁"（`total_429` 永远是 0），且没有任何报错。
    状态码是协议层的，不会因为文案改版而变。
    """

    def __init__(self, message: str, *, count_as_failure: bool = True,
                 http_status: int | None = None) -> None:
        super().__init__(message)
        self.count_as_failure = count_as_failure
        self.http_status = http_status


class AuditTrailError(FinAgentError):
    """审计链写入或校验失败。"""


class PermissionDeniedError(FinAgentError):
    """数据分级权限不足。"""
