"""LLM提供商客户端（Ollama / DeepSeek，httpx异步）。

统一chat契约：输入ModelSpec+system+prompt，输出LLMResponse；
异常一律包装为LLMGatewayError由网关降级链处理，不向上裸抛。
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from typing import Any, Final, Protocol

import httpx

from src.core.config import get_settings
from src.core.exceptions import LLMGatewayError
from src.infrastructure.llm.cache import prompt_fingerprint
from src.infrastructure.llm.local_budget import LOCAL_MODEL_TIMEOUT_SEC
from src.infrastructure.llm.models import LLMResponse, ModelSpec

logger = logging.getLogger(__name__)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


#: 本地（Ollama）思维链开关的环境变量。**默认关**（理由见 `_resolve_local_think`）。
ENV_THINK = "MOSS_LOCAL_THINK"


def _warn_if_prompt_truncated(spec: ModelSpec, system: str, prompt: str,
                              prompt_eval_count: int) -> None:
    """★ 提示词被后端**静默截断**时出声（Ollama 实测行为，2026-09-28）。

    ## 为什么需要它

    Ollama 在 prompt 超过它的上下文上限时**不报错**，而是处理"装得下的那部分"，
    并把**实际处理的 token 数**放在响应的 `prompt_eval_count` 里 ——
    生产链路上**没有任何地方读这个字段**，所以截断是完全不可见的：
    表现只是"这条怎么没抽全"。

    实测（`qwen3.5:4b`，`num_ctx=4096`）：

    | 提交的正文 | 实际 prompt | `prompt_eval_count` |
    |---|---|---|
    | 600 字（生产单段上限） | 1379 字符 | **795**（未截断） |
    | 6000 字 | 6761 字符 | 3027（未截断） |
    | 12000 字 | 12761 字符 | **2050 ← 封顶** |
    | 20000 字 | 20761 字符 | **2050 ← 封顶** |

    注意封顶值**不是常量**：换一个 `num_ctx=16384` 的临时模型后是 8194~9152。

    ## 判据

    中文正文的实测密度约 **0.64 token/字**（600 字 → 795 token，含 404 token 骨架）。
    取 0.3 作**保守下限**（按英文字符更密的情况留足余量）：若
    `prompt_eval_count < 0.3 × (system+prompt 字符数)`，说明后端只处理了一部分。
    只**告警**不改行为 —— 这一层不该替调用方决定"截断了要不要重试"。
    """
    total_chars = len(system or "") + len(prompt or "")
    if total_chars <= 0 or prompt_eval_count <= 0:
        return
    # 骨架本身也要算：它再短也有几十 token，所以只在"比例明显偏低"时告警。
    if prompt_eval_count < 0.3 * total_chars:
        logger.warning(
            "Ollama 可能截断了提示词：提交 %d 字符，后端只处理了 %d token"
            "（模型 %s）。截断是**静默**的 —— 表现只是'没抽全'。"
            "若这是抽取链路，请检查单段字数上限（tone.MAX_TEXT_CHARS）"
            "与 num_ctx 的关系。",
            total_chars, prompt_eval_count, spec.model_name)


def _resolve_local_think(*, spec: ModelSpec) -> bool:
    """该不该给这次**本地**调用下 `think`，下什么值。

    ## 为什么默认**关**思维链（2026-09-28 实测：这就是"掷硬币"的根因）

    `qwen3` / `qwen3.5` 是**思考型**模型，思考 token **计入** `num_predict`
    （Ollama 把它单独放在响应的 `thinking` 字段里，但预算是一起算的）。
    实测（真实 prompt = `tone.build_prompt` + `tone.extraction_schema()`，
    `num_predict=2048`，各 3 次）：

    | 模型 | 默认（开思考） | `think=false` |
    |---|---|---|
    | `qwen3:8b` | p50 **18.3s** · 输出 729 tok · 键齐全 100% | p50 **9.1s** · 321 tok · 键齐全 100% |
    | `qwen3.5:4b` | p50 31.4s · **空正文 100%**（2048 全被思考吃掉） | p50 **4.8s** · 184 tok · 键齐全 100% |

    - 对 8B，关思考是**纯收益**：延迟减半、输出 token 减 57%、抽取能力不变；
    - 对 4B，开思考是**必然空返回**（不是偶发）—— 上层看到"模型返回空内容"，
      与随机故障长得一模一样。这正是被长期登记为"**8GB 显存不足导致掷硬币**"
      的那个现象。

    **显存不是这个病**：4B 权重只占 ~3.4GB（比 8B 的 5.6GB 宽裕得多），
    却比 8B 更容易空返回。病在**输出预算被思维链吃光**。

    ## 判据

    - `spec.think` 显式给了 → 听调用方的（供 A/B 与个别任务回退）
    - 否则读 `MOSS_LOCAL_THINK`：`1/true/on/yes` → True；
      `0/false/off/no` → False；**未设 → False（默认关）**
    - 设了别的值 → 打 warning 并按默认关（**不静默**）
    """
    if spec.think is not None:
        return bool(spec.think)
    raw = (os.environ.get(ENV_THINK) or "").strip().lower()
    if not raw:
        return False
    if raw in {"1", "true", "on", "yes"}:
        return True
    if raw in {"0", "false", "off", "no"}:
        return False
    logger.warning("%s=%r 无法识别（用 1/0），按默认「关思考」处理", ENV_THINK, raw)
    return False


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
    wait_ms: int | None = None,
) -> LLMResponse:
    """把 provider 的原始返回包成统一响应。

    ## `wait_ms` / `model_ms`：排队与模型侧必须分开（`CHG-0177`）

    `latency_ms` 是**墙钟**，它把"在闸里排队"和"模型在干活"算在一起。
    实测本地调用 max **210,136ms**，而那个数 = `local_gate` 的 90s 排队上限
    + HTTP 的 120s 超时 —— 合并计量时**无法归因**（见 `LLMResponse.wait_ms`）。

    ⚠️ 字段叫 `model_ms` 而**不是** `gen_ms`：它含**模型加载/换入**。
    实测冷调用 2,383ms 只出了 3 个 token —— 那是加载，不是生成。

    `wait_ms` 由**走闸的** provider 传入（目前只有 Ollama）。
    **不传 = 该 provider 没有闸 = 不适用**，此时 `model_ms` 也必须是 `None`：
    填 `latency_ms` 会把"不适用"伪装成"零排队"，正是本项目明令禁止的
    「用 0 糊过去」。
    """
    latency_ms = int((time.perf_counter() - started) * 1000)
    model_ms = None if wait_ms is None else max(0, latency_ms - wait_ms)
    return LLMResponse(
        content=content,
        model_used=spec.model_name,
        provider=spec.provider,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        reasoning_tokens=reasoning_tokens,
        latency_ms=latency_ms,
        wait_ms=wait_ms,
        model_ms=model_ms,
        prompt_hash=prompt_fingerprint(system, prompt),
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
        # ★ 本地跳的 HTTP 超时**从 ceiling 派生**，不用全局 `llm_timeout_seconds`
        #   （`CHG-0179`）。
        #
        #   原先这里取 `settings.llm_timeout_seconds = 120.0`，而排队上限是
        #   `local_gate` 里的另一处 `90.0` —— **两者相加 = 210.1 秒**，
        #   正是实测到的本地调用最大值，而**没有任何地方声明过这个数**。
        #   现在两者同源于 `local_budget.LOCAL_HOP_CEILING_SEC`。
        #
        #   ⚠️ 这意味着 `llm_timeout_seconds` **不再作用于本地跳**。
        #      要缩短本地预算请改 `local_budget.LOCAL_HOP_CEILING_SEC`
        #      （它会把排队与模型侧**一起**缩短，保持自洽）。
        self._timeout = timeout or LOCAL_MODEL_TIMEOUT_SEC
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
        # ★ 思维链开关（顶层字段，**不在 options 里**）。见 `_resolve_local_think`
        # 的实测表：开思考会让 4B 100% 空返回、8B 白花一倍延迟而能力不变。
        payload["think"] = _resolve_local_think(spec=spec)
        # ★ 应用侧并发闸（见 `local_gate` 的模块说明）：Ollama 只有**一个**
        #   计算槽位，并发请求会在它内部排队；把等待挪到这里，超时就只计
        #   生成时间，而不是"排队排到 120 秒"被记成调用失败。
        from src.infrastructure.llm.local_gate import get_local_gate

        #: ★ 本跳的**纯排队**时长（毫秒）。只在走闸时有意义 ⇒ 初值 `None`
        #: 而不是 0（"没量到" ≠ "量到 0"）。
        wait_ms: int | None = None
        try:
            # `slot()` 产出**等待时长（秒）**—— 取到它的那一刻还没发 HTTP，
            # 所以它正好是"排队"与"生成"的分界线（`CHG-0177`）。
            async with get_local_gate().slot(spec.model_name) as waited:
                wait_ms = int(waited * 1000)
                resp = await self._client.post(
                    f"{spec.base_url}/api/chat", json=payload)
                resp.raise_for_status()
                data = resp.json()
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise _gateway_error("Ollama", spec.model_name, exc) from exc
        tokens_in = int(data.get("prompt_eval_count") or 0)
        _warn_if_prompt_truncated(spec, system, prompt, tokens_in)
        return _wrap_response(
            spec,
            system,
            prompt,
            data["message"]["content"],
            tokens_in,
            int(data.get("eval_count") or 0),
            started,
            # Ollama 不区分思维链 token（qwen3 的 thinking 混在 eval_count 里，
            # 且 /api/chat 非流式响应不返回细分）→ 显式 0，表示"未量到"。
            # ⚠️ 不要把它当"没有思考"：这是"这个后端不提供该字段"。
            reasoning_tokens=0,
            wait_ms=wait_ms,
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


#: ★ 提供商自声明表（`CHG-0192` 修）：`name → (是否计费, 是否跑在本机)`。
#:
#: ## 为什么必须由**提供商自己**声明，而不是网关里的两个硬编码集合
#:
#: 网关原先把 `PAID_PROVIDERS = {"deepseek"}` / `LOCAL_PROVIDERS = {"ollama"}`
#: 写死在自己文件里。后果是：**新增一个付费提供商时没有任何机制提醒你去登记它**
#: ⇒ `uses_paid` 对它恒为 `False` ⇒ **它花掉的钱不进 token 预算**，
#: 而预算检查、降级链裁剪、付费只做链首这几条护栏**全部静默失效**。
#: 这与本仓库"白名单要单一事实源 + 新增即注册"的纪律冲突。
#:
#: ## 两个谓词各司其职（别合并）
#:
#: * `paid` —— **成本**：决定 token 预算是否累积、付费是否只做链首；
#: * `local` —— **位置**：决定是否受显存检查、是否算"跑在本机"。
#:
#: 2026-09-28 实测过把它们混用的代价：免费云端（dashscope/siliconflow/zhipu）
#: 不在 `PAID_PROVIDERS` 里 ⇒ **被误判成本地模型** ⇒ 一接进来就被显存检查拦掉
#: （报错是"qwen-flash 拉不起来"，而它根本不在本机）。
#:
#: ⚠️ 网关对**未声明**的提供商回退到既有的两个硬编码集合（向后兼容）：
#: 注入 Fake provider 的测试、以及第三方直接实现 `BaseProvider` 的场景不受影响。
PROVIDER_DECLARATIONS: Final[dict[str, tuple[bool, bool]]] = {
    # name: (paid, 本机)
    "ollama": (False, True),
    "deepseek": (True, False),
    # 下面三个是**免费云端**：既不付费、也不在本机。
    # ⚠️ 必须显式声明（不能靠"不在 PAID_PROVIDERS 里"来推断本地）——
    #    2026-09-28 实测过那个推断的代价：免费云端被当成"跑在本机"，
    #    一接进来就被显存检查拦掉（报错还写着"拉不起来"，而它不在本机）。
    "zhipu": (False, False),
    "dashscope": (False, False),
    "siliconflow": (False, False),
}


def declare_provider(provider: Any) -> Any:
    """把自声明打到 provider 实例上（`paid` / `local`），返回同一个对象。

    为什么落在**实例属性**而不是加进 `BaseProvider` 协议：
    Protocol 加类属性会让所有既有实现（含测试里的 Fake）都不再满足协议 ——
    那是"为了新能力打破旧契约"。实例属性是纯增量：没声明的走网关回退。
    """
    paid, local = PROVIDER_DECLARATIONS.get(
        getattr(provider, "name", ""), (False, False))
    provider.paid = paid
    provider.local = local
    return provider


def build_providers() -> dict[str, BaseProvider]:
    """按已配置能力实例化提供商表（缺API key时仍注册，调用时报错触发降级）。

    为什么要注册"缺 key"的 provider：调用时报错可以走**降级链**，
    而"未注册"只会得到 `提供商未注册` 并直接跳过 —— 后者会让
    "忘了配 key"看起来像"这个模型不可用"，排查方向完全错。

    ★ 每个实例都会带上**自声明**（`paid` / `local`，见 `PROVIDER_DECLARATIONS`）——
    新增提供商时**只改这一张表**，网关侧不需要再改任何集合。
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
    # ★ 统一打自声明（新增提供商只需在 `PROVIDER_DECLARATIONS` 里加一行）
    for provider in providers.values():
        declare_provider(provider)
    return providers
