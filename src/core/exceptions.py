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


class NoApplicableData(DataFetchError):
    """该口径对该主体**不适用**、或**未被该专题收录** —— **不是取数失败，也不是 0**。

    ## 为什么必须与"取数失败"分开（用户 2026-09-30 报障）

    用户面板上，三种完全不同的情形原来**长得一模一样**（都是
    「未获取到 X 数据」+ 置信度低），而处置相反：

    | 情形 | `kind` | 该怎么办 |
    |---|---|---|
    | 语义上不适用（银行没有"流动比率"） | `not_applicable` | **不用管**，且**不要联网硬试**（烧钱） |
    | 专题表未收录该主体（招行窗口内无质押/担保公告） | `not_covered` | 承认覆盖不到，**别去补**（表里本来就没有它） |
    | 真的取数失败 | （普通 `DataFetchError`） | 去修取数链 |

    ## 为什么文本里带**标准标记**

    `supervisor.NOT_APPLICABLE_MARKERS` 认的就是这些字面标记 —— 那是
    **取数侧自己写下的结论**（机器可读标识），不是拿自然语言猜语义；
    猜语义正是把"不适用"误报成"故障"的原因（该常量处有说明）。
    标记同时是**跨层可读**的：异常经路由器聚合后类型会丢，标记不会。
    """

    #: 与 `supervisor.NOT_APPLICABLE_MARKERS` 逐字一致（判据会核对两边相同）。
    MARKER_NOT_APPLICABLE = "不适用（非缺陷）"
    MARKER_NOT_COVERED = "未收录该主体（非缺陷）"

    KINDS = ("not_applicable", "not_covered")

    def __init__(self, message: str = "", *, kind: str = "not_applicable") -> None:
        if kind not in self.KINDS:
            raise ValueError(f"kind 必须是 {self.KINDS} 之一，实际 {kind!r}")
        marker = (self.MARKER_NOT_COVERED if kind == "not_covered"
                  else self.MARKER_NOT_APPLICABLE)
        super().__init__(f"{message} —— {marker}")
        self.kind = kind


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
