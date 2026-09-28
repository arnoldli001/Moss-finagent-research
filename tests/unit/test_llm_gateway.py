"""LLM网关测试（FakeProvider注入，不联网）。"""

import pytest

from src.core.config import Settings
from src.core.exceptions import ConfigError, LLMGatewayError
from src.infrastructure.llm.audit import LLMAuditLog
from src.infrastructure.llm.gateway import (
    LOCAL_PROVIDERS,
    PAID_PROVIDERS,
    LLMGateway,
)
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


@pytest.fixture
def routing():
    """`configs/models.yaml` **现读**的 `(模型规格表, 层降级链表)`。

    为什么现读而不是硬编码模型名（2026-09-28 第二十轮的真实代价）：
    路由一改，写死模型名与跳数的断言就集体变红 —— 而它们想证明的是
    **「网关照配置执行」**，不是「配置此刻长这样」（后者由
    `tests/unit/test_llm_routing_contract.py` 专门守）。
    """
    from src.infrastructure.llm.gateway import _load_model_config

    specs, chains, _lo = _load_model_config("configs/models.yaml")
    return specs, chains


def _pin_vram(gw: LLMGateway, *, free_mb: int | None = 99999,
              resident: frozenset[str] = frozenset()) -> None:
    """注入假显存探针，让「钉死本地」的用例与本机显卡状态解耦。

    必须注入：显式 `local_only=True` 会走 `_filter_local_by_vram`
    （真起 `nvidia-smi` 子进程 + 问 Ollama，约 2s），而且结果随本机显存
    占用变化 —— 不注入的用例会「在我这台机器上绿、换一台就红」。
    """
    from src.infrastructure.llm.vram import LocalCapacity

    gw._local_capacity = LocalCapacity(  # noqa: SLF001
        free_probe=lambda: free_mb, resident_probe=lambda _u: resident,
        ttl_sec=0.0)


def _local_hops(chain: list[str], specs) -> list[str]:
    """链上的**本机**跳（判据用 `LOCAL_PROVIDERS` 而不是 `PAID_PROVIDERS`）。"""
    return [m for m in chain if specs[m].provider in LOCAL_PROVIDERS]


@pytest.fixture(autouse=True)
def _fresh_circuit_breakers(monkeypatch):
    """每个用例一套**干净的熔断器**。

    熔断器是进程级的：某个用例把 ollama/deepseek 打挂之后，后面的用例会直接
    吃到 `circuit_open`（实测报错 `'circuit_open: ollama' != 'boom'`，
    而且失败信息指向那个无辜的用例，排查方向完全错）。
    """
    from src.infrastructure.llm import circuit_breaker as cbmod

    monkeypatch.setattr(cbmod, "_registry", cbmod.CircuitBreakerRegistry())
    yield


@pytest.fixture(autouse=True)
def _fresh_rate_limit_guard():
    """每个用例一套**干净的限流熔断器**（理由同上，且更隐蔽）。

    `gateway` 现在取的是进程级单例（这样 `/health` 展示的就是**生效中**那一个
    的计数）。若不复位，某个用例锁定的模型会跟着流到后面的用例，
    表现是"某个无辜用例里这一跳被跳过" —— 与熔断器那次踩坑同型。
    """
    from src.infrastructure.llm.rate_limit_guard import reset_guard

    reset_guard()
    yield
    reset_guard()


async def test_routes_light_to_ollama_primary(gateway_env, routing):
    """`light` 层**钉死本地**时，调用落在 ollama 的本机模型上，且是单跳。

    ## ★ 2026-09-28 第二十轮：触发条件从「层级配置」改为「调用点显式声明」

    原判据：**不传任何参数** → 路由到 `local_light`
      （那时 `light` 在 `configs/models.yaml` 里钉着 `local_only: true`，
      运行时链被裁成 1 跳）。

    为什么变：第二十轮把 `light` 首位改成**免费**的 `qwen-siliconflow-7b`
      并移除了 `local_only: true`（配额分散；免费档不花钱 —— 依据见
      `configs/models.yaml` 第 97~99 行）→ 默认调用的主模型变成**云端**，
      `resp.model_used == qwen2.5:1.5b` 不再成立。
      **机制没坏，是载体变了**：要验「路由到本机主模型 + 响应字段填对」，
      就得在调用处显式声明 `local_only=True`。

    ⚠️ 显式钉死本地会走 `_filter_local_by_vram`（真起子进程、结果随本机显存
      变化）→ 注入假探针（`_pin_vram`），与本机显卡状态解耦。
    """
    settings, _ = gateway_env
    specs, chains = routing
    hops = _local_hops(chains["light"], specs)
    assert hops, f"light 链上没有本机模型（{chains['light']}）—— 用例前提不成立"

    providers = {"ollama": FakeProvider(), "deepseek": FakeProvider()}
    gw = LLMGateway(settings=settings, providers=providers, cache=None)
    _pin_vram(gw)

    resp = await gw.complete("light", "系统提示", "清洗这批数据", local_only=True)
    assert resp.model_used == specs[hops[0]].model_name
    assert resp.provider_chain == hops[:1]
    assert not resp.fallback_used
    assert resp.content == f"answer::{specs[hops[0]].model_name}"
    assert resp.tokens_out == 20


async def test_fallback_chain_works_on_reasoning_tier(gateway_env, routing):
    """降级链本身照常工作：主模型失败 → 前进到**下一跳**并返回它的结果。

    ⚠️ 载体选择（2026-09-28 第二十轮更新）：原写「不再用 `light` 验证云端兜底，
    因为那层钉了 `local_only: true`」—— 该钉死已移除（见下一个用例），但
    `light` 仍不适合本条：它的链**三跳全免费**，验不出"主模型挂掉 → 备源接管"
    的语义。改用链**最长**的 `reasoning`：既验"前进一跳即停"，
    也验"没有跳过中间跳"。

    ## ★ 2026-09-28 第二十轮：期望值改为**从配置推导**

    原判据把 `reasoning` 的链写死成 `deepseek-flash → local_medium`
    （"云端主 → 本地备"两跳），断言 `model_used == qwen3:8b-q4_K_M`。
    第二十轮 `reasoning` 改为四跳（`deepseek-flash → qwen-dashscope-flash
    → qwen-siliconflow-7b → local_light`），且 `local_medium` **退出所有链**
    —— 硬编码的模型名与跳数**同时**过期。
    现在按链现读：跳数、配置键名、真实模型名三处都不会再漂移。
    """
    settings, _ = gateway_env
    specs, chains = routing
    chain = chains["reasoning"]
    assert len(chain) >= 2, f"reasoning 链只有一跳（{chain}），没有备源可测"
    # 相邻两跳必须换厂商（由 test_routing_fallbacks_are_cross_provider 守），
    # 所以"按 provider 建替身"不会让主备共用同一个失败替身。
    primary_provider = specs[chain[0]].provider
    providers: dict[str, FakeProvider] = {}
    for name in chain:
        provider = specs[name].provider
        providers.setdefault(provider, FakeProvider(
            error=(LLMGatewayError(f"{provider} 挂了")
                   if provider == primary_provider else None)))
    gw = LLMGateway(settings=settings, providers=providers, cache=None)

    resp = await gw.complete("reasoning", "系统", "任务")
    assert resp.model_used == specs[chain[1]].model_name
    assert resp.fallback_used
    # ⚠️ `provider_chain` 记的是**配置里的名字**，`model_used` 才是真模型名
    assert resp.provider_chain == chain[:2]
    assert providers[primary_provider].calls == [specs[chain[0]].model_name], (
        "主模型没有被尝试 —— 那这条用例证明不了降级")


async def test_light_never_falls_back_to_paid(gateway_env, routing):
    """★★ `light` 层**默认就不许**产生付费调用。

    ## ★ 2026-09-28 第二十轮：保证来源变了（不是放宽断言，是前提变了）

    原判据：`light` 在 `configs/models.yaml` 里钉 `local_only: true`
      → `complete()` 的 `pin_local` 把运行时链裁到只剩 `local_light`
      → 本地一挂即整链失败，**绝不落到付费的 deepseek-flash**。
      实测背景：本地一抖动就悄悄花钱（事件告警阶段一 0.0196 元；
      `_dbg_hot.py` 这类探针更隐蔽）。

    为什么变：第二十轮把 `light` 首位改成**免费**的 `qwen-siliconflow-7b`
      （配额分散：light+medium+planning 共用百炼同一份额度会在 6.4 天耗尽），
      并移除了 `local_only: true` —— **全仓现在没有任何层是 local_only**
      （`_load_model_config` 的 `tiers_local_only` 是空集）。

    新判据（**意图不变，载体变了**）：`light` 的链**不含付费 provider**
      ⇒「默认不花钱」改由**链的组成**保证，而不是运行时钉死保证。
      所以这里把付费 provider 注册成**可用替身**再断言它**一次都没被调用**
      —— 若哪天有人把 deepseek 接回 light 的链，这条立刻红。
      运行期的钉死机制仍在，但改为**显式** `local_only=True` 才生效（见下）。
    """
    settings, _ = gateway_env
    specs, chains = routing
    chain = chains["light"]
    paid = [m for m in chain if specs[m].provider in PAID_PROVIDERS]
    assert not paid, (
        f"light 链上出现付费档 {paid}（链={chain}）—— 本层占 82% 调用量，"
        "静默落到付费档会按调用量放大账单")

    # ① 默认（不传 local_only）：免费链**全挂**也不许落到付费档
    chain_providers = sorted({specs[m].provider for m in chain})
    providers: dict[str, FakeProvider] = {
        p: FakeProvider(error=LLMGatewayError("免费链挂了"))
        for p in chain_providers}
    providers["deepseek"] = FakeProvider()      # 可用但**不在链上**的付费诱饵
    gw = LLMGateway(settings=settings, providers=providers, cache=None)

    with pytest.raises(LLMGatewayError, match="全部模型调用失败"):
        await gw.complete("light", "系统", "任务-light")
    assert providers["deepseek"].calls == [], \
        "light 默认仍然调了云端（会花钱）"

    # ② 显式钉死本地：连**免费**云端也不许碰（运行期护栏仍在，只是默认不打开）
    pinned = {p: FakeProvider() for p in chain_providers if p != "ollama"}
    gw2 = LLMGateway(settings=settings,
                     providers={**pinned, "ollama": FakeProvider()}, cache=None)
    _pin_vram(gw2)
    resp = await gw2.complete("light", "系统", "任务2", local_only=True)
    assert resp.provider_chain == _local_hops(chain, specs)[:1], (
        "local_only=True 没有把链裁到只剩本机模型")
    for provider, fake in pinned.items():
        assert fake.calls == [], f"local_only=True 仍然调了 {provider}（云端）"


async def test_medium_force_local_env_restores_old_behavior(gateway_env, monkeypatch):
    """★★ `MOSS_MEDIUM_FORCE_LOCAL=1` 必须把 `medium` 退回"只用本地"。

    这条守着"延迟 vs 成本"取舍的**可回退性** —— 改配置换来 −75s 的同时，
    必须留一条不改代码就能退回"零云端花费"的路（AGENTS.md：
    「默认值即护栏」+「同一判断只允许一份实现」）。
    """
    settings, _ = gateway_env
    monkeypatch.setenv("MOSS_MEDIUM_FORCE_LOCAL", "1")

    providers = {"ollama": FakeProvider(error=LLMGatewayError("本地模型挂了")),
                 "deepseek": FakeProvider()}
    gw = LLMGateway(settings=settings, providers=providers, cache=None)

    # 本地挂了 + 强制本地 → 必须失败，且**一次都没碰云端**
    with pytest.raises(LLMGatewayError, match="全部模型调用失败"):
        await gw.complete("medium", "系统", "信息层任务")
    assert providers["deepseek"].calls == [], \
        "MOSS_MEDIUM_FORCE_LOCAL=1 时 medium 仍然调了云端"

    # 关掉开关 → 恢复云端 primary（对照组，证明开关真的在起作用）
    monkeypatch.delenv("MOSS_MEDIUM_FORCE_LOCAL", raising=False)
    providers2 = {"ollama": FakeProvider(), "deepseek": FakeProvider()}
    gw2 = LLMGateway(settings=settings, providers=providers2, cache=None)
    resp = await gw2.complete("medium", "系统", "信息层任务")
    assert resp.model_used == "deepseek-flash"


async def test_local_only_never_spends_cloud_tokens(gateway_env, routing):
    """★ `local_only=True` 时**绝不**降级到计费提供商。

    用途：情报抽取那条链路每 2 小时跑几十条（`local_only=True`），绝不能悄悄
    花云端 token。用户口径："本地模型推理不费钱，浪费就浪费……只要不用云端
    tokens就行" —— 本地挂了**宁可失败**（调用方退回规则层），也不能悄悄花钱。

    ## ★ 2026-09-28 第二十轮：换载体层（原判据**空转**了）

    原实现用 `light` 层 + `local_only=True` 断言 `deepseek.calls == []`。
    第二十轮之后 `light` 的链是 `qwen-siliconflow-7b → qwen-dashscope-flash
    → local_light` —— **一个付费跳都没有**：那条断言会**空转**
    （哪怕 `local_only` 完全失效，deepseek 也永远不会被调用）。
    改用**链上确实挂着付费档**的层，并先断言这一点（防再次空转）。
    """
    settings, _ = gateway_env
    specs, chains = routing
    tier = "decision"
    assert any(specs[m].provider in PAID_PROVIDERS for m in chains[tier]), (
        f"{tier} 链上没有付费档（{chains[tier]}）—— 本用例会空转，换个层")

    providers = {"ollama": FakeProvider(error=LLMGatewayError("本地模型挂了")),
                 "deepseek": FakeProvider()}
    gw = LLMGateway(settings=settings, providers=providers, cache=None)
    _pin_vram(gw)

    with pytest.raises(LLMGatewayError, match="全部模型调用失败"):
        await gw.complete(tier, "系统", "任务", local_only=True)
    assert providers["deepseek"].calls == [], "local_only 仍然调了云端（会花钱）"


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


async def test_failure_is_audited(gateway_env, routing):
    """失败的每一跳都要留审计（含模型名）。

    用 `reasoning` 层：这条链上的模型最多，能验出"**每一跳**都留痕"。

    ## ★ 2026-09-28 第二十轮：期望值改为**从配置推导**

    原判据写死了两跳：`errors == ["boom2", "boom"]`、
    `models == ["deepseek-flash", "qwen3:8b-q4_K_M"]`（当时是"云端主 →
    本地备"）。第二十轮 `reasoning` 变成四跳
    （`deepseek-flash → qwen-dashscope-flash → qwen-siliconflow-7b →
    local_light`），`local_medium` 退出所有链 —— 写死的跳数与模型名同时过期。
    现在链、配置键名、真实模型名、每跳的错误文案全部**按链现读**，
    改 routing 不再让这条红（它要证明的是"每跳都留痕"，不是"链长这样"）。

    ⚠️ 必须给链上**每个 provider** 都注入替身：`provider` 未注册时
    `complete()` 只记 `last_error` 并 `continue`，**不写审计**
    （见 `gateway.py` 的 `KeyError` 分支）—— 那样断言会漏跳而看不出来。
    """
    settings, tmp = gateway_env
    specs, chains = routing
    chain = chains["reasoning"]
    assert len(chain) >= 2, f"reasoning 链只有一跳（{chain}），验不出多跳留痕"

    providers: dict[str, FakeProvider] = {}
    for name in chain:
        provider = specs[name].provider
        providers.setdefault(provider, FakeProvider(
            error=LLMGatewayError(f"boom::{provider}")))
    gw = LLMGateway(settings=settings, providers=providers, cache=None)
    with pytest.raises(LLMGatewayError):
        await gw.complete("reasoning", "系统", "任务T")

    entries = LLMAuditLog(tmp).read_all()
    assert [e["model"] for e in entries] == [specs[m].model_name for m in chain]
    assert [e["error"] for e in entries] == [
        f"boom::{specs[m].provider}" for m in chain]


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


# ---------------------------------------------------------------------------
# ★ 2026-09-28 第二十二轮：**免费档限流熔断**（`rate_limit_guard`）的两条
# 接线判据。为什么必须有：
#
#   ① 判据只用**文案**匹配 "429" 是不够的 —— 那句话是 `httpx` 的实现细节，
#      它一改写法，护栏就**静默失效**（`total_429` 永远是 0，没人会收到报错）。
#      所以状态码必须从协议层传上来，且**状态码优先于文案**。
#   ② 状态文件路径写死 `data/run/free_tier_429.json` 是**跨实例污染源**：
#      `data/run/` 是 dev / pilot / 生产**共用**的目录，
#      dev 里调试出的 3 次 429 会把线上这一跳锁 10 分钟（按模型名判断，
#      不看库、不看实例）。故障方向反过来 —— 不报错，只是"线上突然不用免费档了"。
# ---------------------------------------------------------------------------


def test_gateway_error_carries_structured_http_status():
    """`_gateway_error` 必须把状态码**结构化**带上去，不能只留在文案里。"""
    import httpx

    from src.infrastructure.llm.providers import _gateway_error

    req = httpx.Request("POST", "https://api.siliconflow.cn/v1/chat/completions")
    err = httpx.HTTPStatusError(
        "x", request=req, response=httpx.Response(429, request=req))
    gw_err = _gateway_error("siliconflow", "Qwen/Qwen2.5-7B-Instruct", err)
    assert gw_err.http_status == 429, "状态码没传上去 —— 限流判据只能靠文案"

    # 无 response 的异常（超时/连接失败）要如实为 None，不能瞎编一个。
    assert _gateway_error("x", "m", httpx.ConnectTimeout("t")).http_status is None


async def test_rate_limited_locks_the_hop_even_without_429_in_the_text(
        gateway_env, monkeypatch, tmp_path):
    """★ 状态码就能触发锁定 —— 文案里**没有** "429" 也必须锁。

    这一条在修复前是红的：错误消息刻意写成
    `HTTPStatusError: 服务器返回了错误状态`（不含任何限流字样），
    旧判据（只在文案里找 "429"）会**判为不是限流** → 每次调用都白撞一次、
    永远不锁。这正是"护栏装了但从来没生效"的机器复现。
    """
    import httpx

    from src.infrastructure.llm import circuit_breaker as cbmod
    from src.infrastructure.llm.providers import _gateway_error
    from src.infrastructure.llm.rate_limit_guard import LOCK_THRESHOLD

    monkeypatch.setattr(cbmod, "_registry", cbmod.CircuitBreakerRegistry())
    monkeypatch.setenv("MOSS_RATE_GUARD_PATH", str(tmp_path / "guard.json"))

    settings, _ = gateway_env
    req = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    # ⚠️ 文案里**故意没有** 429 / rate limit 字样
    boom = _gateway_error(
        "DeepSeek", "deepseek-flash",
        httpx.HTTPStatusError(
            "服务器返回了错误状态", request=req,
            response=httpx.Response(429, request=req)))
    providers = {"deepseek": FakeProvider(error=boom)}
    gw = LLMGateway(settings=settings, providers=providers, cache=None)
    assert gw._rate_guard.path.endswith("guard.json"), (
        "护栏路径没跟着 MOSS_RATE_GUARD_PATH 走 —— 又回到跨实例共用了")
    fake = providers["deepseek"]

    # 打够阈值次数（每次换 prompt，避免缓存/去重把调用吃掉）
    for i in range(LOCK_THRESHOLD):
        with pytest.raises(LLMGatewayError):
            await gw.complete("decision", "s", f"p{i}")
    assert fake.calls, "provider 一次都没被调用，用例本身没跑到判定路径"
    assert gw._rate_guard.is_locked("deepseek-flash"), (
        "连续 429 之后没有锁定 —— 判据没认出来（文案里没有 '429'）")
    snap = gw._rate_guard.snapshot()["models"]["deepseek-flash"]
    assert snap["total_429"] == LOCK_THRESHOLD, "429 计数没落到状态文件里"
    assert snap["remaining_s"] and snap["remaining_s"] > 0
    calls_before = len(fake.calls)

    # 锁定期内：这一跳必须被**跳过**（不再白撞一次）
    with pytest.raises(LLMGatewayError):
        await gw.complete("decision", "s", "after-lock")
    assert len(fake.calls) == calls_before, (
        f"锁定期内仍在调它（{calls_before} → {len(fake.calls)}）—— 没跳过，"
        "等于每次调用都先白撞一次 429")


def test_rate_guard_state_path_is_isolated_per_instance(monkeypatch, tmp_path):
    """★ dev / pilot / 生产**不能共用**同一个护栏状态文件。"""
    import importlib

    from src.infrastructure.llm import rate_limit_guard as mod

    mod = importlib.reload(mod)          # 保证读到的是当前实现

    monkeypatch.delenv("MOSS_RATE_GUARD_PATH", raising=False)
    monkeypatch.delenv("LLM_AUDIT_DIR", raising=False)
    assert mod.resolve_state_path() == mod.DEFAULT_STATE_PATH

    # dev / pilot 的隔离本来就在设 LLM_AUDIT_DIR —— 护栏跟着它走，
    # 于是 manage.py 里**不需要**再加一处 key（少一个 key 就少一处漏改）
    monkeypatch.setenv("LLM_AUDIT_DIR", str(tmp_path / "dev" / "audit"))
    isolated = mod.resolve_state_path()
    assert isolated != mod.DEFAULT_STATE_PATH
    assert isolated.endswith("/free_tier_429.json")
    assert "\\" not in isolated, "路径分隔符要归一化（否则 Windows 上跨平台判据会脆）"

    monkeypatch.setenv("MOSS_RATE_GUARD_PATH", str(tmp_path / "explicit.json"))
    assert mod.resolve_state_path().endswith("explicit.json"), "显式指定必须最高优先"

    # 单例语义：`get_guard()` 不接受 path 参数，避免"传了 path 就换实例"
    # 从而把内存里已累计的"连续 429"计数丢掉
    mod.reset_guard()
    assert mod.get_guard() is mod.get_guard()
    mod.reset_guard()


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
    """降级链**相邻两跳必须换 provider**，否则等于没兜底。

    2026-09-20 实测：decision 层是 deepseek-v4-pro → deepseek-flash，
    两者同属 provider=deepseek，DeepSeek 一熔断主备一起被拒，
    整条链必然失败（报"全部模型调用失败"），而本机 Ollama 是好的却没用上。

    ⚠️ 2026-09-28 改为按**解析后的完整链**检查，支持三跳（`fallbacks` 列表）。
    原实现读单数 `route["fallback"]`，遇到 `fallbacks` 会取到 `None` ——
    判据会**静默失效**而不是报错。链变长后"相邻不得同厂商"才是正确的不变量
    （厂商级故障不该连杀两跳）。
    """
    from src.infrastructure.llm.gateway import _load_model_config

    specs, chains, _lo = _load_model_config("configs/models.yaml")
    assert chains, "没有解析出任何路由链"
    for tier, chain in chains.items():
        provs = [specs[m].provider for m in chain]
        for i in range(len(provs) - 1):
            assert provs[i] != provs[i + 1], (
                f"{tier} 层第 {i} 跳与第 {i + 1} 跳同属 provider="
                f"{provs[i]}（链={chain}）—— 该 provider 整体不可用时没有兜底"
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
