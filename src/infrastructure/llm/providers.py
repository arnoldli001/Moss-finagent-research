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


class BaseProvider(Protocol):
    """提供商协议，测试可注入Fake实现。"""

    name: str

    async def chat(
        self, spec: ModelSpec, system: str, prompt: str, *, json_mode: bool = False
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
    """本地Ollama /api/chat（非流式）。"""

    name = "ollama"

    def __init__(self, timeout: float | None = None) -> None:
        settings = get_settings()
        self._timeout = timeout or settings.llm_timeout_seconds

    async def chat(
        self, spec: ModelSpec, system: str, prompt: str, *, json_mode: bool = False
    ) -> LLMResponse:
        started = time.perf_counter()
        payload: dict[str, Any] = {
            "model": spec.model_name,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "options": {"temperature": spec.temperature, "num_predict": spec.max_tokens},
        }
        if json_mode:
            payload["format"] = "json"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(f"{spec.base_url}/api/chat", json=payload)
                resp.raise_for_status()
                data = resp.json()
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise LLMGatewayError(f"Ollama调用失败({spec.model_name}): {exc}") from exc
        return _wrap_response(
            spec,
            system,
            prompt,
            data["message"]["content"],
            int(data.get("prompt_eval_count") or 0),
            int(data.get("eval_count") or 0),
            started,
        )


class DeepSeekProvider:
    """DeepSeek云端 /chat/completions（OpenAI兼容格式）。"""

    name = "deepseek"

    def __init__(self, timeout: float | None = None) -> None:
        settings = get_settings()
        self._timeout = timeout or settings.llm_timeout_seconds
        self._api_key = settings.deepseek_api_key

    async def chat(
        self, spec: ModelSpec, system: str, prompt: str, *, json_mode: bool = False
    ) -> LLMResponse:
        if not self._api_key:
            raise LLMGatewayError("DEEPSEEK_API_KEY未配置，无法调用云端模型")
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
        if json_mode:
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
            raise LLMGatewayError(f"DeepSeek调用失败({spec.model_name}): {exc}") from exc
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
