"""LLM网关测试（FakeProvider注入，不联网）。"""

import pytest

from src.core.config import Settings
from src.core.exceptions import ConfigError, LLMGatewayError
from src.infrastructure.llm.audit import LLMAuditLog
from src.infrastructure.llm.cache import LLMCache
from src.infrastructure.llm.gateway import LLMGateway
from src.infrastructure.llm.models import LLMResponse


class FakeProvider:
    def __init__(self, error: Exception | None = None, fail_first: int = 0) -> None:
        self._error = error
        self._fail_first = fail_first
        self.calls: list[str] = []

    async def chat(self, spec, system, prompt, *, json_mode=False):
        self.calls.append(spec.model_name)
        if self._fail_first > 0:
            self._fail_first -= 1
            raise LLMGatewayError("模拟主模型故障")
        if self._error:
            raise self._error
        return LLMResponse(
            content=f"answer::{spec.model_name}", model_used=spec.model_name,
            provider=spec.provider, tokens_in=100, tokens_out=20,
            prompt_hash="ph", response_hash="rh",
        )


@pytest.fixture
def gateway_env(tmp_dir):
    settings = Settings(
        model_config_path="configs/models.yaml",
        llm_cache_dir=tmp_dir,
        llm_audit_dir=tmp_dir,
        llm_cache_enabled=True,
    )
    return settings, tmp_dir


async def test_routes_light_to_ollama_primary(gateway_env):
    settings, _ = gateway_env
    providers = {"ollama": FakeProvider(), "deepseek": FakeProvider()}
    gw = LLMGateway(settings=settings, providers=providers, cache=None)

    resp = await gw.complete("light", "系统提示", "清洗这批数据")
    assert resp.model_used == "qwen2.5:1.5b-instruct-q4_K_M"
    assert resp.provider_chain == ["local_light"]
    assert not resp.fallback_used
    assert resp.content == "answer::qwen2.5:1.5b-instruct-q4_K_M"
    assert resp.tokens_out == 20


async def test_fallback_chain_on_primary_failure(gateway_env):
    settings, _ = gateway_env
    providers = {"ollama": FakeProvider(error=LLMGatewayError("本地模型挂了")),
                 "deepseek": FakeProvider()}
    gw = LLMGateway(settings=settings, providers=providers, cache=None)

    resp = await gw.complete("light", "系统", "任务")
    assert resp.model_used == "deepseek-v4-flash"  # light层fallback
    assert resp.fallback_used
    assert resp.provider_chain == ["local_light", "deepseek-v4-flash"]


async def test_cache_hit_skips_provider(gateway_env):
    settings, _ = gateway_env
    providers = {"ollama": FakeProvider(), "deepseek": FakeProvider()}
    gw = LLMGateway(settings=settings, providers=providers)

    first = await gw.complete("light", "系统", "相同问题")
    second = await gw.complete("light", "系统", "相同问题")
    assert not first.cache_hit
    assert second.cache_hit and second.cache_kind == "exact"
    assert second.content == first.content
    assert len(providers["ollama"].calls) == 1  # 第二次未打模型


async def test_all_providers_fail_raises(gateway_env):
    settings, _ = gateway_env
    providers = {"ollama": FakeProvider(error=LLMGatewayError("x")),
                 "deepseek": FakeProvider(error=LLMGatewayError("y"))}
    gw = LLMGateway(settings=settings, providers=providers, cache=None)

    with pytest.raises(LLMGatewayError, match="全部模型调用失败"):
        await gw.complete("light", "系统", "任务")


async def test_unknown_tier_raises(gateway_env):
    settings, _ = gateway_env
    gw = LLMGateway(settings=settings, providers={}, cache=None)
    with pytest.raises(ConfigError, match="未知任务层级"):
        await gw.complete("unknown_tier", "s", "p")  # type: ignore[arg-type]


async def test_audit_records_all_calls(gateway_env):
    settings, tmp = gateway_env
    providers = {"ollama": FakeProvider(), "deepseek": FakeProvider()}
    gw = LLMGateway(settings=settings, providers=providers)

    await gw.complete("light", "系统", "问题A", agent_id="A08_macro", trace_id="tr_1")
    await gw.complete("light", "系统", "问题A", agent_id="A08_macro", trace_id="tr_1")

    entries = LLMAuditLog(tmp).read_all()
    assert len(entries) == 2
    assert entries[0]["cache_hit"] is False
    assert entries[1]["cache_hit"] is True and entries[1]["cache_kind"] == "exact"
    assert entries[0]["prompt_hash"] and entries[0]["agent_id"] == "A08_macro"


async def test_failure_is_audited(gateway_env):
    settings, tmp = gateway_env
    providers = {"ollama": FakeProvider(error=LLMGatewayError("boom")),
                 "deepseek": FakeProvider(error=LLMGatewayError("boom2"))}
    gw = LLMGateway(settings=settings, providers=providers, cache=None)
    with pytest.raises(LLMGatewayError):
        await gw.complete("light", "系统", "任务T")

    entries = LLMAuditLog(tmp).read_all()
    assert [e["error"] for e in entries] == ["boom", "boom2"]
    assert entries[0]["model"] == "qwen2.5:1.5b-instruct-q4_K_M"
