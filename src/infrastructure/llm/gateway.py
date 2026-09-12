"""LLM统一网关：路由 → 语义缓存 → 降级链 → 哈希审计。

架构红线：所有LLM调用必须经过本网关（禁止Agent直连模型API）。
调用流程：缓存查询（exact/semantic）→ 主模型 → 失败降级备模型 →
全程审计落JSONL（prompt_hash/response_hash/token/延迟/降级链）。
"""

from __future__ import annotations

import time
from typing import Any

import yaml

from src.core.config import Settings, get_settings
from src.core.exceptions import ConfigError, LLMGatewayError
from src.infrastructure.llm.audit import LLMAuditLog
from src.infrastructure.llm.cache import LLMCache, cache_key
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
    """供所有Agent注入使用的统一LLM入口。"""

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
    ) -> LLMResponse:
        """执行一次补全：缓存→主模型→备模型，全程审计。"""
        if task_tier not in self._routing:
            raise ConfigError(f"未知任务层级: {task_tier}")
        chain = self._routing[task_tier]

        if self._cache and use_cache:
            hit = self._cache.get(system, prompt)
            if hit is not None:
                hit.trace_id = trace_id
                self._audit.record(
                    trace_id=trace_id, agent_id=agent_id, task_tier=task_tier,
                    response=hit, cached=True,
                )
                return hit

        tried: list[str] = []
        last_error: Exception | None = None
        for index, model_name in enumerate(chain):
            spec = self._spec(model_name)
            tried.append(model_name)
            started = time.perf_counter()
            try:
                provider = self._providers[spec.provider]
            except KeyError:
                last_error = LLMGatewayError(f"提供商未注册: {spec.provider}")
                continue
            try:
                resp = await provider.chat(spec, system, prompt, json_mode=json_mode)
            except LLMGatewayError as exc:
                last_error = exc
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
            if self._cache and use_cache:
                self._cache.put(system, prompt, resp)
            self._audit.record(
                trace_id=trace_id, agent_id=agent_id, task_tier=task_tier,
                response=resp, cached=False,
            )
            return resp

        raise LLMGatewayError(
            f"全部模型调用失败（链: {'→'.join(tried)}）: {last_error}"
        )
