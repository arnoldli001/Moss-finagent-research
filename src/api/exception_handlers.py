"""全局异常处理器 —— 「不泄漏内部细节」的最后一道防线。

从 `main.py` 抽出的独立模块：处理器是**纯函数式注册**（`register_handlers(app)`），
测试可以用一个最小 FastAPI 实例验证契约，不必拉起整个 Runtime。

契约见 `src/api/error_codes.py`：出错的响应一律是
  {"detail": "用户可读文案", "code": "层前缀_数字", "trace_id": 可选}
- detail 是 dict（auth 的 {"code","message"} / api_error() 的结构体）→ 原样透传；
- 5xx 的字符串 detail **永不外发**（可能含路径/URL/异常原文），替换为注册表文案；
- 未捕获异常 → SYS_5000 + trace_id，堆栈只进日志（按 trace_id 可回查）。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from src.api.error_codes import (
    is_structured_detail,
    public_detail,
    spec_for_status,
)

logger = logging.getLogger(__name__)


async def http_exception_handler(request: Request,
                                 exc: HTTPException) -> JSONResponse:
    detail = exc.detail
    if is_structured_detail(detail):
        # 保持 FastAPI 默认的 {"detail": {...}} 包裹形状（auth 测试与前端
        # explain() 都按 `json()["detail"]["code"]` 读取），顶层再补一份 code
        # 让新前端不必拆嵌套。
        return JSONResponse({"detail": detail, "code": detail["code"]},
                            status_code=exc.status_code,
                            headers=getattr(exc, "headers", None))
    sp = spec_for_status(exc.status_code)
    body: dict[str, Any] = {"detail": public_detail(exc.status_code, detail),
                            "code": sp.code}
    return JSONResponse(body, status_code=exc.status_code,
                        headers=getattr(exc, "headers", None))


async def validation_exception_handler(request: Request,
                                       exc: RequestValidationError
                                       ) -> JSONResponse:
    """422 参数校验：只外发 字段位置+错误类型，**不回显用户输入与内部 ctx**。

    pydantic 默认错误体带 `input`/`ctx` —— `ctx` 里可能有内部约束对象，
    `input` 会把请求原文整体回吐（公网日志/抓包角度都是噪音面）。
    """
    fields = [
        {"loc": [str(p) for p in err.get("loc", [])],
         "msg": str(err.get("msg", "")),
         "type": str(err.get("type", ""))}
        for err in exc.errors()[:10]
    ]
    sp = spec_for_status(422)
    return JSONResponse(
        {"detail": sp.message, "code": sp.code, "fields": fields},
        status_code=422)


async def unhandled_exception_handler(request: Request,
                                      exc: Exception) -> JSONResponse:
    """未捕获异常：对外只有 SYS_5000 + trace_id；堆栈按 trace_id 进日志。"""
    trace_id = uuid.uuid4().hex[:12]
    logger.exception("未捕获异常 trace_id=%s %s %s",
                     trace_id, request.method, request.url.path)
    sp = spec_for_status(500)
    return JSONResponse(
        {"detail": sp.message, "code": sp.code, "trace_id": trace_id},
        status_code=500)


def register_handlers(app: FastAPI) -> None:
    """把三个处理器挂到 app 上（main.py 启动时装配一次）。"""
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError,
                              validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)


__all__ = [
    "http_exception_handler",
    "register_handlers",
    "unhandled_exception_handler",
    "validation_exception_handler",
]
