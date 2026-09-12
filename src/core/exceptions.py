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
    """模型网关调用失败。"""


class AuditTrailError(FinAgentError):
    """审计链写入或校验失败。"""


class PermissionDeniedError(FinAgentError):
    """数据分级权限不足。"""
