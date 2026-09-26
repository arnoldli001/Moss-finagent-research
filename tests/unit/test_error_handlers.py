"""全局异常处理器契约测试（tests/unit/test_error_handlers.py）。

覆盖三条防线：
1. 未捕获异常 → SYS_5000 + trace_id，**原文不进响应体**；
2. HTTPException：5xx 字符串 detail 被替换为注册表文案，4xx 原文放行；
3. 结构化 detail（{"code","message"}）保持 {"detail": {...}} 包裹形状透传；
4. 422 校验错误只外发 loc/msg/type，不回显 input/ctx。
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from src.api.error_codes import api_error
from src.api.exception_handlers import register_handlers

_SECRET = "D:\\code\\Moss-finagent-research\\src\\secret.py"


def _app() -> FastAPI:
    app = FastAPI()
    register_handlers(app)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError(f"db locked at {_SECRET} token=abc123")

    @app.get("/http500")
    async def http500() -> None:
        raise HTTPException(status_code=500, detail=f"内部路径 {_SECRET}")

    @app.get("/http400")
    async def http400() -> None:
        raise HTTPException(status_code=400, detail="证券代码应为6位数字")

    @app.get("/structured")
    async def structured() -> None:
        raise api_error("TRADE_5022", cause=RuntimeError("上游超时"))

    @app.get("/structured4xx")
    async def structured4xx() -> None:
        raise api_error("REQ_4000", message="自定义文案",
                        cause=ValueError("x=1"))

    class Body(BaseModel):
        code: str

    @app.post("/validated")
    async def validated(body: Body) -> dict:  # noqa: ARG001
        return {"ok": True}

    return app


def test_unhandled_exception_hides_internals() -> None:
    client = TestClient(_app(), raise_server_exceptions=False)
    r = client.get("/boom")
    assert r.status_code == 500
    body = r.json()
    assert body["code"] == "SYS_5000"
    assert body["detail"] == "服务器内部错误，请稍后重试"
    assert body["trace_id"]
    # 原文（路径/内部信息）绝不出现在响应里
    assert _SECRET not in r.text and "token=abc123" not in r.text


def test_http_5xx_string_detail_never_leaks() -> None:
    client = TestClient(_app(), raise_server_exceptions=False)
    r = client.get("/http500")
    assert r.status_code == 500
    body = r.json()
    assert body["code"] == "SYS_5000"
    assert body["detail"] == "服务器内部错误，请稍后重试"
    assert _SECRET not in r.text


def test_http_4xx_string_detail_passthrough() -> None:
    client = TestClient(_app(), raise_server_exceptions=False)
    r = client.get("/http400")
    assert r.status_code == 400
    body = r.json()
    assert body["detail"] == "证券代码应为6位数字"
    assert body["code"] == "REQ_4000"


def test_structured_detail_keeps_wrapped_shape() -> None:
    """结构化 detail 必须保持 {"detail": {...}} 形状（auth/前端 explain 依赖）。"""
    client = TestClient(_app(), raise_server_exceptions=False)
    r = client.get("/structured")
    assert r.status_code == 502
    body = r.json()
    assert body["detail"]["code"] == "TRADE_5022"
    assert body["detail"]["message"] == "回测数据获取失败，请稍后重试"
    assert body["code"] == "TRADE_5022"
    # 5xx 的 cause 不下发（只进日志）
    assert "cause" not in body["detail"]


def test_structured_4xx_carries_redacted_cause() -> None:
    client = TestClient(_app(), raise_server_exceptions=False)
    r = client.get("/structured4xx")
    assert r.status_code == 400
    body = r.json()
    assert body["detail"]["message"] == "自定义文案"
    assert body["detail"]["cause"] == "x=1"


def test_validation_error_strips_input_and_ctx() -> None:
    client = TestClient(_app(), raise_server_exceptions=False)
    r = client.post("/validated", json={"code": 123})
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "REQ_4220"
    assert body["detail"] == "请求参数未通过校验"
    assert body["fields"] and set(body["fields"][0]) == {"loc", "msg", "type"}
    # 用户输入不回显
    assert "123" not in r.text


def test_registry_codes_documented_in_error_codes_md() -> None:
    """漂移守护：注册表里的每个码都必须在 docs/ERROR_CODES.md 总表里有条目。

    没有这条，运维表会在第一次加码时就悄悄过时 —— 表的价值恰恰在于
    「编码--故障--修改建议」三列齐全且与代码一致。
    """
    import re
    from pathlib import Path

    from src.api.error_codes import REGISTRY

    doc = (Path(__file__).resolve().parents[2]
           / "docs" / "ERROR_CODES.md")
    text = doc.read_text(encoding="utf-8")
    missing = sorted(c for c in REGISTRY if not re.search(rf"\b{c}\b", text))
    assert not missing, f"docs/ERROR_CODES.md 缺少报错码条目: {missing}"
