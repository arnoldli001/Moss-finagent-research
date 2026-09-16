"""LLM统一网关：路由 → 语义缓存 → 降级链 → 哈希审计。

架构红线：所有LLM调用必须经过本网关（禁止Agent直连模型API）。
调用流程：缓存查询（exact/semantic）→ 主模型 → 失败降级备模型 →
全程审计落JSONL（prompt_hash/response_hash/token/延迟/降级链）。
"""

from __future__ import annotations

import time
from typing import Any

import yaml

from src.core.cancel import CancellationToken
from src.core.config import Settings, get_settings
from src.core.exceptions import ConfigError, LLMGatewayError
from src.infrastructure.llm.audit import LLMAuditLog
from src.infrastructure.llm.cache import LLMCache, cache_key
from src.infrastructure.llm.circuit_breaker import get_circuit_registry
from src.infrastructure.llm.models import LLMResponse, ModelSpec, TaskTier
from src.infrastructure.llm.providers import BaseProvider, build_providers


def _load_model_config(path: str) -> tuple[dict[str, ModelSpec], dict[str, list[str]]]:
    """解析configs/models.yaml → (模型规格表, 层级降级链表)。"""
    try:
        with open(path, encoding="utf-8") as fh:
            raw: dict[str, Any] = yaml.safe_load(fh)
    except OSError as exc:
        raise ConfigError(f"模型配置读取失败: {path}: {exc}") from exc

    specs: dict[str, ModelSpec] = {}
    for name, item in (raw.get("models") or {}).items():
        specs[name] = ModelSpec(name=name, **{
            k: item[k] for k in ("provider", "model_name", "base_url")
            if k in item
        } | {
            "max_tokens": item.get("max_tokens", 2048),
            "temperature": item.get("temperature", 0.1),
        })

    routing: dict[str, list[str]] = {}
    for tier, item in (raw.get("routing") or {}).items():
        chain = [item["primary"]]
        if item.get("fallback"):
            chain.append(item["fallback"])
        routing[tier] = chain

    if not specs or not routing:
        raise ConfigError(f"模型配置不完整: {path}")
    return specs, routing


class LLMGateway:
    """供所有Agent注入使用的统一LLM入口。

    Token预算保护：按trace_id累计DeepSeek调用token，超限拒绝后续调用。
    """

    def __init__(
        self,
        settings: Settings | None = None,
        providers: dict[str, BaseProvider] | None = None,
        cache: LLMCache | None = None,
        audit: LLMAuditLog | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._specs, self._routing = _load_model_config(self._settings.model_config_path)
        self._providers = providers if providers is not None else build_providers()
        self._cache = cache if cache is not None else (
            LLMCache(
                cache_dir=self._settings.llm_cache_dir,
                ttl_hours=self._settings.llm_cache_ttl_hours,
                semantic_threshold=self._settings.llm_semantic_threshold,
            )
            if self._settings.llm_cache_enabled
            else None
        )
        self._audit = audit or LLMAuditLog(self._settings.llm_audit_dir)
        # Token预算：trace_id → 累计token数
        self._token_usage: dict[str, int] = {}
        self._token_budget = self._settings.llm_token_budget_per_task

    @property
    def audit_log(self) -> LLMAuditLog:
        return self._audit

    def _spec(self, model_name: str) -> ModelSpec:
        try:
            return self._specs[model_name]
        except KeyError as exc:
            raise ConfigError(f"模型未在configs/models.yaml定义: {model_name}") from exc

    async def complete(
        self,
        task_tier: TaskTier,
        system: str,
        prompt: str,
        *,
        agent_id: str = "",
        trace_id: str = "",
        json_mode: bool = False,
        use_cache: bool = True,
        cancel_token: CancellationToken | None = None,
    ) -> LLMResponse:
        """执行一次补全：缓存→主模型→备模型，全程审计。

        cancel_token非None且被取消时，直接跳过API调用抛TaskCancelledError。
        输入prompt超过llm_input_char_hard_cap时尾部截断，防数据淹没致token超支。
        """
        if task_tier not in self._routing:
            raise ConfigError(f"未知任务层级: {task_tier}")
        # 取消检查：在调用provider前拦截，避免浪费token
        if cancel_token is not None:
            cancel_token.check()
        # 输入截断：超长prompt尾部截断（保留头部用户提问与数据，丢弃尾部冗余）
        cap = self._settings.llm_input_char_hard_cap
        if len(prompt) > cap:
            prompt = prompt[:cap] + "\n…（已截断）"
        chain = self._routing[task_tier]

        if self._cache and use_cache:
            hit = self._cache.get(system, prompt, agent_id)
            if hit is not None:
                hit.trace_id = trace_id
                self._audit.record(
                    trace_id=trace_id, agent_id=agent_id, task_tier=task_tier,
                    response=hit, cached=True,
                )
                return hit

        # Token预算检查：单任务DeepSeek调用累计超限则拒绝
        uses_paid = any(self._specs[m].provider == "deepseek" for m in chain)
        if trace_id and uses_paid:
            used = self._token_usage.get(trace_id, 0)
            if used >= self._token_budget:
                raise LLMGatewayError(
                    f"任务{trace_id}的token预算已耗尽"
                    f"（已用{used}/{self._token_budget}），"
                    "请缩小分析范围或调整配置llm_token_budget_per_task"
                )

        tried: list[str] = []
        last_error: Exception | None = None
        for index, model_name in enumerate(chain):
            # 降级链中再次检查取消（fallback时也拦截）
            if cancel_token is not None:
                cancel_token.check()
            spec = self._spec(model_name)
            tried.append(model_name)
            started = time.perf_counter()
            try:
                provider = self._providers[spec.provider]
            except KeyError:
                last_error = LLMGatewayError(f"提供商未注册: {spec.provider}")
                continue
            # 熔断器准入检查：熔断中直接跳过该provider，降级到备模型
            cb = get_circuit_registry().get_or_create(spec.provider)
            if not cb.allow_request():
                last_error = LLMGatewayError(
                    f"提供商{spec.provider}熔断中({cb.snapshot()['state']})，"
                    "自动降级到备模型"
                )
                self._audit.record(
                    trace_id=trace_id, agent_id=agent_id, task_tier=task_tier,
                    response=LLMResponse(
                        content="", model_used=spec.model_name, provider=spec.provider,
                        prompt_hash=cache_key(system, prompt),
                        latency_ms=0, provider_chain=tried,
                    ),
                    cached=False, error=f"circuit_open: {spec.provider}",
                )
                continue
            # 硬截断max_tokens（防单次超支）
            if spec.max_tokens > self._settings.llm_max_tokens_hard_cap:
                spec = ModelSpec(
                    name=spec.name, provider=spec.provider,
                    model_name=spec.model_name, base_url=spec.base_url,
                    max_tokens=self._settings.llm_max_tokens_hard_cap,
                    temperature=spec.temperature,
                )
            try:
                resp = await provider.chat(spec, system, prompt, json_mode=json_mode)
            except LLMGatewayError as exc:
                last_error = exc
                cb.record_failure()
                self._audit.record(
                    trace_id=trace_id, agent_id=agent_id, task_tier=task_tier,
                    response=LLMResponse(
                        content="", model_used=spec.model_name, provider=spec.provider,
                        prompt_hash=cache_key(system, prompt),
                        latency_ms=int((time.perf_counter() - started) * 1000),
                        provider_chain=tried,
                    ),
                    cached=False,
                    error=str(exc),
                )
                continue

            resp.provider_chain = list(tried)
            resp.fallback_used = index > 0
            resp.trace_id = trace_id
            cb.record_success()  # 熔断器成功计数
            # 累计token预算（仅DeepSeek）
            if spec.provider == "deepseek" and trace_id:
                self._token_usage[trace_id] = (
                    self._token_usage.get(trace_id, 0)
                    + resp.tokens_in + resp.tokens_out
                )
            if self._cache and use_cache:
                self._cache.put(system, prompt, resp, agent_id)
            self._audit.record(
                trace_id=trace_id, agent_id=agent_id, task_tier=task_tier,
                response=resp, cached=False,
            )
            return resp

        raise LLMGatewayError(
            f"全部模型调用失败（链: {'→'.join(tried)}）: {last_error}"
        )
