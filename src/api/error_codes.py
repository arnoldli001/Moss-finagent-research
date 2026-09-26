"""HTTP 报错码**分层注册表** —— 前后端统一错误契约的唯一权威来源（后端侧）。

## 为什么需要它

2026-09-25 复盘发现全项目没有统一错误契约：

- 24 处路由/中间件把 `str(exc)` 原样塞进 `detail`（异常消息可能带 URL、
  文件路径、内部配置，直接泄漏到公网前端）；
- 前端把后端响应体原文截 200 字显示，运维指令（`manage.py start ...`）
  也被写进用户可见的报错里；
- 未捕获异常走 Starlette 默认 500，没有任何 trace_id 可关联日志。

正确做法：**报错码 + 分层文案**。后端只往外发「码 + 用户可读文案」，
内部细节（异常原文、堆栈）只进日志；前端按码映射展示文案，
公网环境绝不显示运维指令。

## 分层（码前缀 = 层）

| 前缀 | 层 | 覆盖 |
|---|---|---|
| `SYS_` | 系统层 | 未捕获异常、服务不可用、运行时未就绪 |
| `AUTH_` | 认证与权限层 | 未登录、会话失效、权限不足、限流 |
| `REQ_` | 请求与参数层 | 参数非法、资源不存在、状态冲突、校验失败 |
| `DATA_` | 数据源层 | 上游行情/宏观数据源失败、超时 |
| `ANA_` | 分析链路层 | 多Agent分析任务、编码Agent执行失败 |
| `TRADE_` | 交易功能层 | 做T快照、回测、资金流、量化选股 |
| `EVT_` | 事件告警层 | 事件扫描、告警推送 |
| `NET_` | 网络层 | **仅前端本地生成**（后端不可达/连接中断），后端不会下发 |

## 响应契约（向后兼容）

```json
{"detail": "用户可读文案", "code": "DATA_5020", "trace_id": "可选"}
```

- `detail` 保留为**字符串**，老的 `resp.json()["detail"]` 断言不受影响；
- detail 本来就是 dict 的（auth 的 `{"code","message"}`）**原样透传**，
  由前端 `explain()` 读取；
- **5xx 的 detail 一律替换为注册表通用文案**（原始异常只进日志），
  4xx 的字符串 detail 视为路由作者写的用户文案，原样放行。

新增报错码时：在对应层的号段里追加 `ErrorSpec`，前端
`web/src/errors.ts` 的同前缀号段里补展示文案（缺省时前端回退到
后端下发的 message，不会裸奔）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Final

from fastapi import HTTPException

from src.core.errors import brief

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ErrorSpec:
    """一条报错码定义。

    - `code`: 稳定标识，`层前缀_四位数字`，对外契约的一部分，**不许改名**；
    - `http_status`: 默认 HTTP 状态码（路由显式指定时以路由为准）；
    - `message`: 用户可读文案 —— 绝不包含路径/URL/异常原文/运维指令；
    - `retryable`: 前端是否值得提示"重试"（瞬时故障 True，配置/权限类 False）。
    """

    code: str
    http_status: int
    message: str
    retryable: bool = False


# ======================================================================
# 注册表（按层分区；号段：层内唯一即可，不强制连续）
# ======================================================================

_SPECS: Final[tuple[ErrorSpec, ...]] = (
    # ---- SYS_ 系统层 ----
    ErrorSpec("SYS_5000", 500, "服务器内部错误，请稍后重试", retryable=True),
    ErrorSpec("SYS_5030", 503, "服务暂不可用，请稍后重试", retryable=True),
    ErrorSpec("SYS_5100", 500, "运行时未初始化"),
    # ---- AUTH_ 认证与权限层 ----
    ErrorSpec("AUTH_4010", 401, "未登录或登录状态已失效"),
    ErrorSpec("AUTH_4030", 403, "权限不足"),
    ErrorSpec("AUTH_4290", 429, "请求过于频繁，请稍后再试", retryable=True),
    # ---- REQ_ 请求与参数层 ----
    ErrorSpec("REQ_4000", 400, "请求参数不合法"),
    ErrorSpec("REQ_4040", 404, "请求的资源不存在"),
    ErrorSpec("REQ_4090", 409, "请求与当前状态冲突"),
    ErrorSpec("REQ_4220", 422, "请求参数未通过校验"),
    # ---- DATA_ 数据源层 ----
    ErrorSpec("DATA_5020", 502, "数据获取失败，请稍后重试", retryable=True),
    ErrorSpec("DATA_5040", 504, "数据获取超时，请稍后重试", retryable=True),
    # ---- ANA_ 分析链路层 ----
    ErrorSpec("ANA_5001", 500, "分析任务执行失败", retryable=True),
    ErrorSpec("ANA_5031", 503, "分析链路暂不可用", retryable=True),
    ErrorSpec("ANA_5401", 500, "编码Agent执行失败", retryable=True),
    # ---- TRADE_ 交易功能层 ----
    ErrorSpec("TRADE_5021", 502, "行情快照获取失败，请稍后重试", retryable=True),
    ErrorSpec("TRADE_5022", 502, "回测数据获取失败，请稍后重试", retryable=True),
    ErrorSpec("TRADE_5023", 502, "资金流数据获取失败，请稍后重试", retryable=True),
    # ---- EVT_ 事件告警层 ----
    ErrorSpec("EVT_5001", 500, "事件扫描执行失败", retryable=True),
    ErrorSpec("EVT_5031", 503, "事件告警子系统不可用"),
)

REGISTRY: Final[dict[str, ErrorSpec]] = {s.code: s for s in _SPECS}

#: HTTP 状态码 → 兜底报错码（路由没给码时按状态归类，保证**任何**错误响应都有码）
_STATUS_FALLBACK: Final[dict[int, str]] = {
    400: "REQ_4000",
    401: "AUTH_4010",
    403: "AUTH_4030",
    404: "REQ_4040",
    405: "REQ_4000",
    409: "REQ_4090",
    422: "REQ_4220",
    429: "AUTH_4290",
    500: "SYS_5000",
    502: "DATA_5020",
    503: "SYS_5030",
    504: "DATA_5040",
}


def spec(code: str) -> ErrorSpec:
    """按码取定义；未注册的码是编程错误，直接抛 KeyError（开发期就该发现）。"""
    return REGISTRY[code]


def spec_for_status(status: int) -> ErrorSpec:
    """状态码 → 兜底定义（>=500 一律 SYS_5000，未收录的 4xx 一律 REQ_4000）。"""
    code = _STATUS_FALLBACK.get(status)
    if code is not None:
        return REGISTRY[code]
    return REGISTRY["SYS_5000" if status >= 500 else "REQ_4000"]


def api_error(code: str, *, cause: BaseException | None = None,
              message: str | None = None,
              status_code: int | None = None) -> HTTPException:
    """路由侧标准抛错：`raise api_error("TRADE_5022", cause=exc)`。

    - `cause` 经 `brief()` 截断+脱敏：
      **4xx 放进 detail dict 的 `cause` 键随响应下发**（用户输入问题需要原因）；
      **5xx 只进日志不下发**（异常原文可能含路径/内部配置），前端只见码+文案；
    - `message` 覆盖注册表默认文案（仅当默认文案确实不适用时）；
    - 返回的 HTTPException.detail 是 dict，全局处理器识别后原样透传。
    """
    sp = spec(code)
    status = status_code or sp.http_status
    detail: dict[str, Any] = {"code": sp.code,
                              "message": message or sp.message}
    if cause is not None:
        if status >= 500:
            logger.warning("%s 内部原因（仅日志）: %s", sp.code, brief(cause))
        else:
            detail["cause"] = brief(cause)
    return HTTPException(status_code=status, detail=detail)


def is_structured_detail(detail: Any) -> bool:
    """detail 是否已是结构化错误体（dict 且带 code）—— 全局处理器的透传判据。"""
    return isinstance(detail, dict) and isinstance(detail.get("code"), str)


def public_detail(status: int, detail: Any) -> str:
    """字符串 detail 的对外口径。

    - 5xx：**永不外发**，替换为兜底文案（原始异常可能含路径/URL/内部配置）；
    - 4xx：视为路由作者写的用户文案，原样放行（路由迁移已把 `str(exc)`
      换成 `brief()` 脱敏版，见各路由改动点）。
    """
    if status >= 500:
        return spec_for_status(status).message
    return detail if isinstance(detail, str) and detail else \
        spec_for_status(status).message


__all__ = [
    "REGISTRY",
    "ErrorSpec",
    "api_error",
    "is_structured_detail",
    "public_detail",
    "spec",
    "spec_for_status",
]
