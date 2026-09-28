"""LLM提供商客户端（Ollama / DeepSeek，httpx异步）。

统一chat契约：输入ModelSpec+system+prompt，输出LLMResponse；
异常一律包装为LLMGatewayError由网关降级链处理，不向上裸抛。
"""

from __future__ import annotations

import hashlib
import os
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

    ★ 这里**顺带把 HTTP 状态码带上去**（`http_status`）。为什么：
    限流熔断（`rate_limit_guard`）原先只能从异常文案里找 "429" ——
    而文案是 `httpx` 的实现细节。状态码是协议层的，改不了版。
    """
    message = f"{provider}调用失败({model}): {exc}"
    status = getattr(getattr(exc, "response", None), "status_code", None)
    status_int = int(status) if status is not None else None
    # 没有 response 的 httpx 异常（超时/连接失败）走下面的网络分支
    if status_int is not None and status_int in _CONFIG_STATUS:
        return LLMGatewayError(
            f"{message}（{status_int} 属配置类问题：密钥无效/余额不足/无权限，"
            "不计入熔断，请检查 .env 的 DEEPSEEK_API_KEY）",
            count_as_failure=False,
            http_status=status_int,
        )
    return LLMGatewayError(message, http_status=status_int)


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
    *,
    reasoning_tokens: int = 0,
) -> LLMResponse:
    return LLMResponse(
        content=content,
        model_used=spec.model_name,
        provider=spec.provider,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        reasoning_tokens=reasoning_tokens,
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
        # ★ 应用侧并发闸（见 `local_gate` 的模块说明）：Ollama 只有**一个**
        #   计算槽位，并发请求会在它内部排队；把等待挪到这里，超时就只计
        #   生成时间，而不是"排队排到 120 秒"被记成调用失败。
        from src.infrastructure.llm.local_gate import get_local_gate

        try:
            async with get_local_gate().slot(spec.model_name):
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
            # Ollama 不区分思维链 token（qwen3 的 thinking 混在 eval_count 里，
            # 且 /api/chat 非流式响应不返回细分）→ 显式 0，表示"未量到"。
            # ⚠️ 不要把它当"没有思考"：这是"这个后端不提供该字段"。
            reasoning_tokens=0,
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
        # ★ 2026-09-28：DeepSeek 把思维链 token 单独放在
        # `completion_tokens_details.reasoning_tokens`（若不提供则为 0）。
        # 用于区分"想得久"与"写得多"——见 LLMResponse.reasoning_tokens 的说明。
        details = usage.get("completion_tokens_details") or {}
        try:
            reasoning_tokens = int(details.get("reasoning_tokens") or 0)
        except (TypeError, ValueError):
            reasoning_tokens = 0
        return _wrap_response(
            spec,
            system,
            prompt,
            data["choices"][0]["message"]["content"],
            int(usage.get("prompt_tokens") or 0),
            int(usage.get("completion_tokens") or 0),
            started,
            reasoning_tokens=reasoning_tokens,
        )


class OpenAICompatProvider:
    """通用 OpenAI 兼容 provider（智谱 GLM / 阿里百炼 / 月之暗面 / 硅基流动…）。

    ## 为什么抽这个类（2026-09-28 第十三轮）

    调研结论：`glm-4.7-flash` **免费**且官方明确支持结构化输出，
    是 A17 "换便宜模型"的头号候选（见
    `docs/LLM_MODEL_SELECTION_RESEARCH_20260928.md`）。

    而 GLM / 百炼 / Moonshot / SiliconFlow 的对话接口**都是 OpenAI 兼容格式**
    （`POST {base_url}/chat/completions` + `Authorization: Bearer`）。
    为每个厂商写一遍 provider 会立刻分叉 —— 所以抽一个通用实现，
    厂商差异只在 `name` / `base_url` / `api_key` 三处。

    ## 与 DeepSeekProvider 的关系

    `DeepSeekProvider` 保持不动（它带 DeepSeek 特有的 `reasoning_effort`
    与 `completion_tokens_details.reasoning_tokens` 解析）。
    本类只覆盖"标准 OpenAI 字段"的部分；厂商特有字段按需在子类覆盖
    `_parse_usage()`。

    ## 安全

    **只从环境变量读 key**（`api_key_env` 指定变量名），不接受硬编码 ——
    AGENTS.md：「禁止硬编码敏感信息（API Key、数据库密码）」。
    变量缺失时**明确报配置错误且不计熔断**（与 DeepSeek 同一处理：
    缺 key 是确定性错误，计进熔断会 3 次就把整条链打开）。
    """

    def __init__(
        self, name: str, base_url: str, api_key_env: str,
        timeout: float | None = None, api_key: str = "",
    ) -> None:
        settings = get_settings()
        self.name = name
        self._base_url = base_url.rstrip("/")
        self._api_key_env = api_key_env
        self._timeout = timeout or settings.llm_timeout_seconds
        #: 显式传入的 key（优先）。空则回退到环境变量 —— 两条来源都支持，
        #: 因为 `.env` 只进 Settings、不进 os.environ（见 build_providers 注释）。
        self._explicit_key = (api_key or "").strip()

    @property
    def api_key(self) -> str:
        if self._explicit_key:
            return self._explicit_key
        return os.environ.get(self._api_key_env, "").strip()

    async def chat(
        self,
        spec: ModelSpec,
        system: str,
        prompt: str,
        *,
        json_mode: bool = False,
        json_schema: dict[str, Any] | None = None,
    ) -> LLMResponse:
        if not self.api_key:
            raise LLMGatewayError(
                f"{self._api_key_env}未配置，无法调用 {self.name}"
                f"（请写入 .env 后重启后端）",
                count_as_failure=False,
            )
        started = time.perf_counter()
        payload: dict[str, Any] = {
            "model": spec.model_name,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": spec.temperature,
        }
        if spec.max_tokens:
            payload["max_tokens"] = spec.max_tokens
        if json_mode or json_schema is not None:
            # OpenAI 兼容端点普遍支持 json_object；严格 schema 支持度不一，
            # 所以退化为 json_object（schema 仍由 prompt 描述注入）。
            payload["response_format"] = {"type": "json_object"}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._base_url}/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
                resp.raise_for_status()
                data = resp.json()
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise _gateway_error(self.name, spec.model_name, exc) from exc

        tokens_in, tokens_out, reasoning = self._parse_usage(
            data.get("usage") or {})
        return _wrap_response(
            spec, system, prompt,
            data["choices"][0]["message"]["content"],
            tokens_in, tokens_out, started,
            reasoning_tokens=reasoning,
        )

    @staticmethod
    def _parse_usage(usage: dict[str, Any]) -> tuple[int, int, int]:
        """解析 usage。标准 OpenAI 字段；厂商扩展在子类覆盖。"""
        details = usage.get("completion_tokens_details") or {}
        try:
            reasoning = int(details.get("reasoning_tokens") or 0)
        except (TypeError, ValueError):
            reasoning = 0
        return (
            int(usage.get("prompt_tokens") or 0),
            int(usage.get("completion_tokens") or 0),
            reasoning,
        )


def build_providers() -> dict[str, BaseProvider]:
    """按已配置能力实例化提供商表（缺API key时仍注册，调用时报错触发降级）。

    为什么要注册"缺 key"的 provider：调用时报错可以走**降级链**，
    而"未注册"只会得到 `提供商未注册` 并直接跳过 —— 后者会让
    "忘了配 key"看起来像"这个模型不可用"，排查方向完全错。
    """
    from src.core.config import get_settings as _gs

    settings = _gs()
    providers: dict[str, BaseProvider] = {
        "ollama": OllamaProvider(),
        "deepseek": DeepSeekProvider(),
    }
    # ★ 智谱 GLM（2026-09-28 加入，A17 影子跑用）
    # ⚠️ 从 Settings 读，**不是** os.environ —— pydantic-settings 只把 .env
    #    读进 Settings，不注入进程环境（项目在 eastmoney_direct.py 踩过）。
    # 仅在有 key 时注册：没配就不该出现在路由里（避免误配后静默失败）。
    if (settings.zhipu_api_key or "").strip():
        providers["zhipu"] = OpenAICompatProvider(
            name="zhipu",
            base_url=settings.zhipu_base_url or
            "https://open.bigmodel.cn/api/paas/v4",
            api_key_env="MOSS_ZHIPU_API_KEY",
            api_key=settings.zhipu_api_key,
        )
    # ★ 2026-09-28 第十八轮：高频层（light/medium）的免费替代候选。
    # 三者都是 OpenAI 兼容格式 → 直接复用 `OpenAICompatProvider`，无需新类。
    # **仅注册 provider，不放进 routing** —— 未实测的模型不进路由（见
    # configs/models.yaml 中 glm-4.7-flash 的实测记录）。
    if (settings.dashscope_api_key or "").strip():
        providers["dashscope"] = OpenAICompatProvider(
            name="dashscope",
            base_url=settings.dashscope_base_url or
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key_env="MOSS_DASHSCOPE_API_KEY",
            api_key=settings.dashscope_api_key,
        )
    if (settings.siliconflow_api_key or "").strip():
        providers["siliconflow"] = OpenAICompatProvider(
            name="siliconflow",
            base_url=settings.siliconflow_base_url or
            "https://api.siliconflow.cn/v1",
            api_key_env="MOSS_SILICONFLOW_API_KEY",
            api_key=settings.siliconflow_api_key,
        )
    return providers
