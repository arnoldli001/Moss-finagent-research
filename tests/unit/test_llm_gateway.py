"""LLM网关测试（FakeProvider注入，不联网）。"""

import pytest

from src.core.config import Settings
from src.core.exceptions import ConfigError, LLMGatewayError
from src.infrastructure.llm.audit import LLMAuditLog
from src.infrastructure.llm.gateway import LLMGateway
from src.infrastructure.llm.models import LLMResponse


class FakeProvider:
    def __init__(self, error: Exception | None = None, fail_first: int = 0) -> None:
        self._error = error
        self._fail_first = fail_first
        self.calls: list[str] = []
        self.prompts: list[str] = []

    async def chat(self, spec, system, prompt, *, json_mode=False):
        self.calls.append(spec.model_name)
        self.prompts.append(prompt)
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
    assert resp.model_used == "deepseek-flash"  # light层fallback
    assert resp.fallback_used
    assert resp.provider_chain == ["local_light", "deepseek-flash"]


async def test_local_only_never_spends_cloud_tokens(gateway_env):
    """★ `local_only=True` 时**绝不**降级到计费提供商。

    用途：情报抽取那条链路每 2 小时跑几十条，而 `light` 层的 fallback 配的是
    `deepseek-flash`（云端、按 token 计费）。用户口径："本地模型推理不费钱，
    浪费就浪费……只要不用云端tokens就行" —— 本地挂了**宁可失败**（调用方退回
    规则层），也不能悄悄花钱。

    ⚠️ 同时确认它**不改配置里那份链**：别的地方照样能用云端兜底。
    """
    settings, _ = gateway_env
    providers = {"ollama": FakeProvider(error=LLMGatewayError("本地模型挂了")),
                 "deepseek": FakeProvider()}
    gw = LLMGateway(settings=settings, providers=providers, cache=None)

    with pytest.raises(LLMGatewayError, match="全部模型调用失败"):
        await gw.complete("light", "系统", "任务", local_only=True)
    assert providers["deepseek"].calls == [], "local_only 仍然调了云端（会花钱）"

    # 链没有被就地改掉：不带 local_only 时云端兜底照旧
    resp = await gw.complete("light", "系统", "任务")
    assert resp.model_used == "deepseek-flash"
    assert providers["deepseek"].calls == ["deepseek-flash"]


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


async def test_same_prompt_different_tier_is_not_a_cache_hit(gateway_env):
    """**作用域回归**：同 prompt 跨层级不复用缓存。

    `light` 跑本地 1.5B、`decision` 跑云端 pro，system/prompt 可能一字不差；
    若缓存不按层级分区，1.5B 的粗糙结论会被决策层直接拿去当结论。
    """
    settings, _ = gateway_env
    providers = {"ollama": FakeProvider(), "deepseek": FakeProvider()}
    gw = LLMGateway(settings=settings, providers=providers)

    await gw.complete("light", "系统", "同一段文字")
    second = await gw.complete("decision", "系统", "同一段文字")
    assert not second.cache_hit, "跨层级串用了缓存"
    assert len(providers["deepseek"].calls) == 1


@pytest.mark.parametrize("cap", [200, 501, 1000])
async def test_truncation_keeps_head_and_tail(gateway_env, cap):
    """截断必须**保住尾部** —— JSON schema 在 prompt 末尾。

    分析类 prompt 的段序是 `[数据正文] … ## 任务要求(schema) ## 技能指引`。
    一刀切尾部会让模型拿不到输出格式，进而走 repair 重试（成本翻倍）。
    """
    settings, _ = gateway_env
    settings.llm_input_char_hard_cap = cap
    providers = {"ollama": FakeProvider(), "deepseek": FakeProvider()}
    gw = LLMGateway(settings=settings, providers=providers, cache=None)

    prompt = "数据行\n" * 500 + "## 任务要求\n{\"stance\": \"string\"}"
    await gw.complete("light", "系统", prompt)

    sent = providers["ollama"].prompts[-1]
    assert len(sent) <= cap + 40, "截断后仍超出上限"
    assert "## 任务要求" in sent, "尾部指令段被切掉了"
    assert sent.startswith("数据行"), "头部数据段被切掉了"
    assert "已省略" in sent, "中段省略标记缺失，截断不可见"



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


# ---------------------------------------------------------------------------
# 熔断器与"配置类错误"的边界（2026-09-20 实测故障的回归）
#
# 故障现场：后端进程没拿到 DEEPSEEK_API_KEY，连续 3 次调用失败把 deepseek
# 熔断器打开，此后所有请求连 API 都不试、直接返回 circuit_open，日志被这个
# 假象刷满，真正的病因（key 未配置）反而被淹没。
#
# 约定：`LLMGatewayError(count_as_failure=False)` 表示"这是确定性错误，
# 重试不会好，不要计入熔断"。瞬时故障（超时/5xx/429）仍按原阈值熔断。
# ---------------------------------------------------------------------------


async def test_config_error_does_not_trip_circuit_breaker(gateway_env, monkeypatch):
    """配置类错误连打多次也不能把熔断器打开。"""
    from src.infrastructure.llm import circuit_breaker as cbmod

    monkeypatch.setattr(cbmod, "_registry", cbmod.CircuitBreakerRegistry())
    settings, _ = gateway_env
    boom = LLMGatewayError("DEEPSEEK_API_KEY未配置", count_as_failure=False)
    providers = {"deepseek": FakeProvider(error=boom)}
    gw = LLMGateway(settings=settings, providers=providers, cache=None)

    # decision 层主模型是 deepseek；阈值 3 次，打 6 次
    for _ in range(6):
        with pytest.raises(LLMGatewayError):
            await gw.complete("decision", "s", "p")

    snap = cbmod.get_circuit_registry().get_or_create("deepseek").snapshot()
    assert snap["state"] == "CLOSED", "配置类错误不应触发熔断"
    assert snap["failures_in_window"] == 0, "配置类错误不应计入失败窗口"
    assert snap["total_rejected"] == 0, "不该出现被熔断拒绝的请求"


async def test_transient_error_still_trips_circuit_breaker(gateway_env, monkeypatch):
    """瞬时故障仍按阈值熔断 —— 别把熔断器本身削弱了。"""
    from src.infrastructure.llm import circuit_breaker as cbmod

    monkeypatch.setattr(cbmod, "_registry", cbmod.CircuitBreakerRegistry())
    settings, _ = gateway_env
    providers = {"deepseek": FakeProvider(error=LLMGatewayError("连接超时"))}
    gw = LLMGateway(settings=settings, providers=providers, cache=None)

    for _ in range(3):
        with pytest.raises(LLMGatewayError):
            await gw.complete("decision", "s", "p")

    snap = cbmod.get_circuit_registry().get_or_create("deepseek").snapshot()
    assert snap["state"] == "OPEN", "瞬时故障达到阈值必须熔断"
    assert snap["failures_in_window"] >= 3


def test_provider_classifies_http_status():
    """401/402/403 是配置类（不计数），5xx/429/超时仍是瞬时类（计数）。"""
    import httpx

    from src.infrastructure.llm.providers import _gateway_error

    def http_error(code: int) -> httpx.HTTPStatusError:
        req = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
        return httpx.HTTPStatusError("x", request=req,
                                     response=httpx.Response(code, request=req))

    for code in (401, 402, 403):
        assert _gateway_error("DeepSeek", "m", http_error(code)).count_as_failure is False
    for code in (429, 500, 502, 503):
        assert _gateway_error("DeepSeek", "m", http_error(code)).count_as_failure is True
    assert _gateway_error("DeepSeek", "m", httpx.ConnectTimeout("t")).count_as_failure is True
    assert _gateway_error("DeepSeek", "m", httpx.ConnectError("c")).count_as_failure is True


async def test_missing_api_key_is_config_error():
    """缺 key 必须标记为不计熔断，否则 3 次就把熔断器打开。"""
    from src.infrastructure.llm.models import ModelSpec
    from src.infrastructure.llm.providers import DeepSeekProvider

    provider = DeepSeekProvider()
    provider._api_key = ""
    spec = ModelSpec(name="deepseek-flash", provider="deepseek",
                     model_name="deepseek-flash",
                     base_url="https://api.deepseek.com", max_tokens=16,
                     temperature=0.0)
    with pytest.raises(LLMGatewayError) as excinfo:
        await provider.chat(spec, "s", "p")
    assert excinfo.value.count_as_failure is False
    assert "DEEPSEEK_API_KEY" in str(excinfo.value)


def test_routing_fallbacks_are_cross_provider():
    """每一层的 fallback 必须换 provider，否则等于没兜底。

    2026-09-20 实测：decision 层是 deepseek-v4-pro → deepseek-flash，
    两者同属 provider=deepseek，DeepSeek 一熔断主备一起被拒，
    整条链必然失败（报"全部模型调用失败"），而本机 Ollama 是好的却没用上。
    """
    import yaml

    with open("configs/models.yaml", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    models = cfg["models"]
    for tier, route in cfg["routing"].items():
        primary = models[route["primary"]]
        fallback = models[route["fallback"]]
        assert primary["provider"] != fallback["provider"], (
            f"{tier} 层的主备同属 provider={primary['provider']}，"
            "该 provider 整体不可用时没有兜底"
        )


# ==================================================================
# 空响应**不得进缓存**（实测踩过的静默故障）
# ==================================================================


class _EmptyThenOkProvider:
    """第一次返回空 content，之后返回正常内容（模拟 max_tokens 被思维链吃光）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, spec, system, prompt, *, json_mode=False):
        self.calls += 1
        content = "" if self.calls == 1 else f"answer::{self.calls}"
        return LLMResponse(
            content=content, model_used=spec.model_name, provider=spec.provider,
            tokens_in=10, tokens_out=4096 if self.calls == 1 else 20,
            prompt_hash="ph", response_hash=f"rh{self.calls}",
        )


async def test_empty_response_is_not_cached(gateway_env):
    """空响应不能被缓存固化。

    ⚠️ 实测踩过的坑：某次调用因 `max_tokens` 被推理链吃光而返回空 content，
    那条空记录被写进缓存后，**后续每一次重试都命中这条空缓存并立刻返回** ——
    表现为"确定性失败、重试无用"，而且因为命中缓存耗时还变短了，
    看起来完全不像缓存问题。真实的 20% 失败率就是这么被放大的。
    """
    from src.infrastructure.llm.cache import LLMCache

    settings, tmp_dir = gateway_env
    provider = _EmptyThenOkProvider()
    cache = LLMCache(cache_dir=tmp_dir)
    gw = LLMGateway(settings=settings, providers={"deepseek": provider,
                                                  "ollama": provider},
                    cache=cache)

    first = await gw.complete("reasoning", "系统", "问题", json_mode=True)
    assert first.content == "", "第一次确实是空响应（模拟失败）"

    # 关键：第二次必须**真的再去请求一次**，而不是命中第一次的空缓存
    second = await gw.complete("reasoning", "系统", "问题", json_mode=True)
    assert provider.calls == 2, "空响应被缓存了：重试没有真正发起请求"
    assert second.content, "第二次应当拿到正常内容"


async def test_normal_response_is_cached(gateway_env):
    """正常响应仍然要缓存（这是网关的核心价值，别为了修空响应把它关掉）。"""
    from src.infrastructure.llm.cache import LLMCache

    settings, tmp_dir = gateway_env
    provider = FakeProvider()
    gw = LLMGateway(settings=settings, providers={"deepseek": provider,
                                                  "ollama": provider},
                    cache=LLMCache(cache_dir=tmp_dir))

    await gw.complete("reasoning", "系统", "问题", json_mode=True)
    await gw.complete("reasoning", "系统", "问题", json_mode=True)
    assert len(provider.calls) == 1, "正常响应应当命中缓存，不重复请求"
