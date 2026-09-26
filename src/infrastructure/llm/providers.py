"""LLM提供商客户端（Ollama / DeepSeek，httpx异步）。

统一chat契约：输入ModelSpec+system+prompt，输出LLMResponse；
异常一律包装为LLMGatewayError由网关降级链处理，不向上裸抛。
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Protocol

import httpx

from src.core.config import get_settings
from src.core.exceptions import LLMGatewayError
from src.infrastructure.llm.models import LLMResponse, ModelSpec


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


#: 这些状态码属于**配置类**故障，重试不会自愈，因此不计入熔断器
#: （401 未授权 / 402 余额不足 / 403 无权限）
_CONFIG_STATUS = {401, 402, 403}


def _gateway_error(provider: str, model: str, exc: Exception) -> LLMGatewayError:
    """把底层异常包装成网关错误，并判定它该不该计入熔断器。

    为什么要区分：熔断器的语义是"连续瞬时故障就先别打了"。而缺 key / key 无效
    / 余额不足是**确定性**错误 —— 计进去会让 3 次调用就把熔断器打开，
    后续调用连 API 都不试、直接返回 `circuit_open`，日志被这个假象刷满，
    真正的病因（`DEEPSEEK_API_KEY未配置`）反而被淹没。实测就是这么踩的。
    """
    message = f"{provider}调用失败({model}): {exc}"
    status = getattr(getattr(exc, "response", None), "status_code", None)
    # 没有 response 的 httpx 异常（超时/连接失败）走下面的网络分支
    if status is not None and int(status) in _CONFIG_STATUS:
        return LLMGatewayError(
            f"{message}（{status} 属配置类问题：密钥无效/余额不足/无权限，"
            "不计入熔断，请检查 .env 的 DEEPSEEK_API_KEY）",
            count_as_failure=False,
        )
    return LLMGatewayError(message)


class BaseProvider(Protocol):
    """提供商协议，测试可注入Fake实现。"""

    name: str

    async def chat(
        self,
        spec: ModelSpec,
        system: str,
        prompt: str,
        *,
        json_mode: bool = False,
        json_schema: dict[str, Any] | None = None,
    ) -> LLMResponse: ...


def _wrap_response(
    spec: ModelSpec,
    system: str,
    prompt: str,
    content: str,
    tokens_in: int,
    tokens_out: int,
    started: float,
) -> LLMResponse:
    return LLMResponse(
        content=content,
        model_used=spec.model_name,
        provider=spec.provider,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        latency_ms=int((time.perf_counter() - started) * 1000),
        prompt_hash=_sha256(system + "\x00" + prompt),
        response_hash=_sha256(content),
    )


class OllamaProvider:
    """本地Ollama /api/chat（非流式）。

    关键优化：payload 传 keep_alive（默认24h）让 Ollama 把模型常驻内存，
    避免每次调用都重新加载 4-5GB 的 GGUF 文件（冷启动 10-20s → 常驻 < 2s）。
    """

    name = "ollama"
    DEFAULT_KEEP_ALIVE_SEC = 24 * 3600  # 模型常驻内存

    def __init__(self, timeout: float | None = None) -> None:
        settings = get_settings()
        self._timeout = timeout or settings.llm_timeout_seconds
        # HTTP 连接池复用：同 session 复用 TCP 连接，避免每次都握手
        self._client = httpx.AsyncClient(timeout=self._timeout)

    async def chat(
        self,
        spec: ModelSpec,
        system: str,
        prompt: str,
        *,
        json_mode: bool = False,
        json_schema: dict[str, Any] | None = None,
    ) -> LLMResponse:
        started = time.perf_counter()
        payload: dict[str, Any] = {
            "model": spec.model_name,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "keep_alive": self.DEFAULT_KEEP_ALIVE_SEC,
            "options": {"temperature": spec.temperature, "num_predict": spec.max_tokens},
        }
        if json_schema is not None:
            # Ollama 支持把 JSON Schema 直接传给 format 做**语法级约束**（受约束解码）。
            # ⚠️ 小模型只给 format="json" 是不够的：实测 qwen2.5:1.5b 在
            # "提取热门个股" 任务上返回 54 字符的坏 JSON
            # （`{"1. 【CC电新】液冷金帝...": -1.1e6}`），解析必然失败 →
            # 下游拿到 0 条结果，且**看起来像"上游没数据"而不是模型问题**。
            payload["format"] = json_schema
        elif json_mode:
            payload["format"] = "json"
        try:
            resp = await self._client.post(
                f"{spec.base_url}/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise _gateway_error("Ollama", spec.model_name, exc) from exc
        return _wrap_response(
            spec,
            system,
            prompt,
            data["message"]["content"],
            int(data.get("prompt_eval_count") or 0),
            int(data.get("eval_count") or 0),
            started,
        )

    async def close(self) -> None:
        await self._client.aclose()


class DeepSeekProvider:
    """DeepSeek云端 /chat/completions（OpenAI兼容格式）。"""

    name = "deepseek"

    def __init__(self, timeout: float | None = None) -> None:
        settings = get_settings()
        self._timeout = timeout or settings.llm_timeout_seconds
        self._api_key = settings.deepseek_api_key

    async def chat(
        self,
        spec: ModelSpec,
        system: str,
        prompt: str,
        *,
        json_mode: bool = False,
        json_schema: dict[str, Any] | None = None,
    ) -> LLMResponse:
        if not self._api_key:
            # 缺 key 是**配置类**错误，明确标记为不计入熔断：
            # 否则连续 3 次调用就会把 deepseek 熔断器打开，后续全是 circuit_open，
            # 真正的病因被淹没（实测踩过）。
            raise LLMGatewayError(
                "DEEPSEEK_API_KEY未配置，无法调用云端模型"
                "（请写入 .env 或设为环境变量后重启后端）",
                count_as_failure=False,
            )
        started = time.perf_counter()
        payload: dict[str, Any] = {
            "model": spec.model_name,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": spec.max_tokens,
            "temperature": spec.temperature,
        }
        # 思维链强度（DeepSeek 原生，none/low/high/max）。空串不下发→服务端默认 high。
        # 注：思考模式下 temperature 被服务端忽略（官方说明），保留不影响。
        if spec.reasoning_effort:
            payload["reasoning_effort"] = spec.reasoning_effort
        if json_mode or json_schema is not None:
            # DeepSeek 只支持 json_object，**不支持**严格 schema 约束；
            # 传 schema 时仍退化为 json_object（schema 通过 prompt 描述注入）。
            payload["response_format"] = {"type": "json_object"}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{spec.base_url}/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
                resp.raise_for_status()
                data = resp.json()
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise _gateway_error("DeepSeek", spec.model_name, exc) from exc
        usage = data.get("usage") or {}
        return _wrap_response(
            spec,
            system,
            prompt,
            data["choices"][0]["message"]["content"],
            int(usage.get("prompt_tokens") or 0),
            int(usage.get("completion_tokens") or 0),
            started,
        )


def build_providers() -> dict[str, BaseProvider]:
    """按已配置能力实例化提供商表（缺API key时deepseek仍注册，调用时报错触发降级）。"""
    return {"ollama": OllamaProvider(), "deepseek": DeepSeekProvider()}
