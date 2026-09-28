"""规划层延迟预算护栏 —— 防「120s 挂死」复发。

## 为什么需要（2026-09-28 实测，端到端证据）

用户问「投研分析的性能优化彻底了吗」。真实端到端实测（`astream` 逐节点计时）：

    supervisor   t=120.31s   ← 一个节点吃掉全部时长，且期间零进度
    collect      t=123.24s
    ...其余 18 个节点共 3.1s
    端到端       123.45s

LLM 审计日志给出了那 120s 的身份：

    supervisor_planner | in=0 out=0 | 120294ms

**`in=0 out=0` + 120294ms = 一次硬超时，不是"慢"。** 根因是两条护栏交叉：

| 护栏 | 单独正确 | 交叉结果 |
|---|---|---|
| `light.local_only: true`（防静默花钱） | 禁止降级云端 | 降级链**只剩 1 跳** |
| `_ATTEMPT_BUDGET`（防"成功但极慢"） | 只对有退路的跳生效 | `has_next=False` → **预算被跳过** |

于是本地模型挂死时，规划层**裸调到 HTTP 120s**，然后
`except Exception: return None` **无声回退**规则式规划 ——
**结论正确、耗时 40 倍**，而症状表现得像"数据采集慢"。

本文件钉住三件事：
1. 显式传入的预算**在最后一跳也生效**（本次修复）
2. 层级默认预算**仍然不在最后一跳生效**（原有启发式不许被顺手破坏）
3. 规划层**确实传了**预算（防"改了 gateway 但没人用"）
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from src.core.config import Settings
from src.core.exceptions import LLMGatewayError
from src.infrastructure.llm.gateway import LLMGateway
from src.infrastructure.llm.models import LLMResponse

ROOT = Path(__file__).resolve().parents[2]


class SlowProvider:
    """每次调用都睡 `delay` 秒 —— 模拟"挂死的本地模型"。"""

    def __init__(self, delay: float = 5.0) -> None:
        self.delay = delay
        self.calls: list[str] = []

    async def chat(self, spec, system, prompt, *, json_mode=False):
        self.calls.append(spec.model_name)
        await asyncio.sleep(self.delay)
        return LLMResponse(
            content=f"answer::{spec.model_name}", model_used=spec.model_name,
            provider=spec.provider, tokens_in=10, tokens_out=5,
            prompt_hash="ph", response_hash="rh",
        )


@pytest.fixture
def gw_env(tmp_dir):
    return Settings(
        model_config_path="configs/models.yaml",
        llm_cache_dir=tmp_dir, llm_audit_dir=tmp_dir, llm_cache_enabled=False,
    )


@pytest.fixture(autouse=True)
def _fresh_circuit_breakers(monkeypatch):
    """熔断器是进程级的，不隔离会污染后续用例。"""
    from src.infrastructure.llm import circuit_breaker as cbmod

    monkeypatch.setattr(cbmod, "_registry", cbmod.CircuitBreakerRegistry())
    yield


@pytest.fixture
def routing():
    """`configs/models.yaml` **现读**的 `(模型规格表, 层降级链表)`。

    期望值从配置推导，而不是写死模型名 —— 写死的期望值会在**每次改 routing**
    时变红，而它想证明的是"网关按配置执行"（配置本身该长什么样由
    `tests/unit/test_llm_routing_contract.py` 守）。
    """
    from src.infrastructure.llm.gateway import _load_model_config

    specs, chains, _lo = _load_model_config("configs/models.yaml")
    return specs, chains


class TimedProvider:
    """按**层链上的配置键**决定睡多久（`delays[spec.name]`，缺省 0 秒）。

    为什么按模型名而不是按厂商：链上"哪一跳慢"是本次用例的自变量，
    而"哪一跳属于哪个厂商"是 routing 的事 —— 按厂商建替身会把两者绑死
    （实测踩过：`planning` 的链曾是同厂商两跳）。
    """

    def __init__(self, delays: dict[str, float]) -> None:
        self.delays = delays
        self.calls: list[str] = []

    async def chat(self, spec, system, prompt, *, json_mode=False):
        self.calls.append(spec.model_name)
        await asyncio.sleep(self.delays.get(spec.name, 0.0))
        return LLMResponse(
            content=f"answer::{spec.model_name}", model_used=spec.model_name,
            provider=spec.provider, tokens_in=10, tokens_out=5,
            prompt_hash="ph", response_hash="rh",
        )


async def test_light_tier_is_pinned_local_at_runtime(gw_env):
    """前提自证：**显式** `local_only=True` 时，`light` 的运行时链是单跳本地。

    ## ★ 2026-09-28 第二十轮：前提变了，判据随之改写（不是放宽断言）

    原判据：`assert "light" in gw._tiers_local_only`
      —— 依据是 2026-09-26 给 `light` 钉的 `local_only: true`（防"本地一抖动
      就悄悄花钱"）。该钉死在第二十轮**被移除**：`light` 首位改为免费的
      `qwen-siliconflow-7b`（配额分散，理由见 `configs/models.yaml`
      第 97~99 行）—— 免费档不花钱，风险从"悄悄花钱"变成"悄悄限流"。
      因此 `_load_model_config()` 现在返回的 `tiers_local_only` 是**空集**。

    新判据（保住原判据真正要证明的事：**运行时链 ≠ 配置态链**）：
      · 配置态：`light` 是三跳链（看着有云端兜底）
      · 运行时：显式 `local_only=True` → 只剩本机一跳
    这正是本文件其余用例关心的"最后一跳"现场 —— 只是现场现在由**调用点
    显式声明**产生，而不是由层级配置默认产生。

    ⚠️ 若哪天有人重新给某层钉 `local_only: true`，本条会红 —— 那正是提醒：
    重新核对 `_ATTEMPT_BUDGET` 的"最后一跳不设预算"启发式对这些层还成不成立。
    """
    from src.infrastructure.llm.gateway import _load_model_config

    specs, chains, local_only = _load_model_config("configs/models.yaml")
    assert "light" not in local_only, (
        "light 又被钉死本地了 —— 本文件的前提自证与结论都要重新核对")
    assert len(chains["light"]) >= 2, (
        f"light 变成单跳链（{chains['light']}）—— 前提自证失去意义")

    local = SlowProvider(delay=0.0)
    cloud = SlowProvider(delay=0.0)
    providers: dict[str, SlowProvider] = {"ollama": local}
    for name in chains["light"]:
        if specs[name].provider != "ollama":
            providers.setdefault(specs[name].provider, cloud)
    gw = LLMGateway(settings=gw_env, providers=providers, cache=None)
    _fake_capacity(gw)          # 显存充足 → 钉死生效（不注入会真起 nvidia-smi）

    resp = await gw.complete("light", "s", "p", local_only=True)
    hop = [m for m in chains["light"] if specs[m].provider == "ollama"][0]
    assert resp.provider_chain == [hop], (
        f"local_only=True 的运行时链不是单跳本地：{resp.provider_chain}")
    assert not resp.fallback_used, "被裁成单跳后不该有降级"
    assert cloud.calls == [], "local_only=True 仍然调了云端"


def _fake_capacity(gw, *, free_mb: int | None = 99999,
                   resident: frozenset[str] = frozenset()) -> None:
    """注入假显存探针。

    必须注入：真实探测要起 `nvidia-smi` 子进程 + 问 Ollama（约 2s），
    会把"预算是否正确生效"的断言淹没在探测耗时里（实测 2.40s > 2.0s 阈值）。
    """
    from src.infrastructure.llm.vram import LocalCapacity

    gw._local_capacity = LocalCapacity(  # noqa: SLF001
        free_probe=lambda: free_mb, resident_probe=lambda _u: resident,
        ttl_sec=0.0)


async def test_explicit_budget_applies_on_last_hop(gw_env, monkeypatch):
    """★ 核心：显式预算在**最后一跳**也必须生效。

    这就是 120s 挂死的机器复现。用可判别的时序对：
    同一个慢 provider，`attempt_budget_sec=0.3` 必须让它**快速失败**。

    ⚠️ 2026-09-28 第二十轮：`local_only=True` 显式声明"最后一跳"的现场。
    原实现靠 `light` 的层级钉死把运行时链裁成单跳；该钉死已移除，
    不显式传参的话，本用例会退化成依赖"链上的云端 provider 恰好没注册"
    ——**场景是意外产生的，不是声明出来的**（见同文件的前提自证用例）。
    """
    prov = SlowProvider(delay=5.0)
    gw = LLMGateway(settings=gw_env, providers={"ollama": prov}, cache=None)
    _fake_capacity(gw)          # 显存充足 → 钉死本地生效（运行时单跳）

    t0 = time.perf_counter()
    with pytest.raises(LLMGatewayError):
        await gw.complete("light", "s", "p", agent_id="t",
                          attempt_budget_sec=0.3, local_only=True)
    elapsed = time.perf_counter() - t0
    assert elapsed < 2.0, (
        f"显式预算 0.3s 没有在最后一跳生效，实际等了 {elapsed:.2f}s —— "
        "「120s 挂死」缺陷复发")
    assert prov.calls, "provider 应被调用过一次（说明确实走到了超时）"


async def test_tier_default_budget_still_skipped_on_last_hop(
        gw_env, monkeypatch):
    """反向护栏：**层级默认**预算仍不许在最后一跳生效。

    原始意图：没有退路时把"慢但正确"变成"必然失败"是净损失。
    修复不能顺手破坏它。

    可判别设计：**把默认预算压到 0.3s**（否则 20s 的默认值无法与
    "根本没设预算"区分开 —— 睡 1s 两种情况下都会成功）。

    ⚠️ 2026-09-28 第二十轮：`local_only=True` 显式声明"最后一跳"的现场
    （理由同上一个用例 —— 层级钉死已移除，场景必须由调用点声明）。
    """
    from src.infrastructure.llm import gateway as gwmod

    monkeypatch.setitem(gwmod._ATTEMPT_BUDGET, "light", 0.3)
    prov = SlowProvider(delay=1.0)
    gw = LLMGateway(settings=gw_env, providers={"ollama": prov}, cache=None)
    _fake_capacity(gw)

    # 默认预算 0.3s < 实际 1.0s。若预算生效会超时失败；
    # 而"最后一跳不设预算"意味着它必须**成功**。
    resp = await gw.complete("light", "s", "p", agent_id="t", local_only=True)
    assert resp.content.startswith("answer::"), (
        "层级默认预算在最后一跳生效了 —— 原有启发式被破坏")


async def test_explicit_budget_still_advances_to_fallback(gw_env, routing):
    """显式预算 + 有备源时，仍应正常降级（新分支不能吃掉老行为）。

    ## ★ 2026-09-28 第二十轮：期望值改为**从配置推导**

    原实现写死"慢的本地 → 快的 deepseek-flash"，并靠 `local_only=False`
    把链恢复成两跳。第二十轮 `light` 的链变成
    `qwen-siliconflow-7b → qwen-dashscope-flash → local_light` ——
    **链上没有 deepseek-flash**，写死的期望值过期。
    现在改为"链上第 1 跳慢、第 2 跳快"，断言"超时后确实前进到下一跳并成功"：
    判据与链的组成解耦，而它要证明的东西（显式预算不吞掉降级）没变。

    `local_only=False` 保留：本用例要的是**完整链**（light 当前是否被钉死
    由 `test_light_tier_is_pinned_local_at_runtime` 守）。
    """
    specs, chains = routing
    chain = chains["light"]
    assert len(chain) >= 2, f"light 链只有一跳（{chain}），无备源可测"

    delays = {chain[0]: 5.0, **{m: 0.0 for m in chain[1:]}}
    providers: dict[str, TimedProvider] = {}
    for name in chain:
        providers.setdefault(specs[name].provider, TimedProvider(delays))
    gw = LLMGateway(settings=gw_env, providers=providers, cache=None)

    t0 = time.perf_counter()
    resp = await gw.complete("light", "s", "p", agent_id="t",
                             attempt_budget_sec=0.3, local_only=False)
    elapsed = time.perf_counter() - t0
    assert elapsed < 2.0, f"没有及时切到备源，等了 {elapsed:.2f}s"
    assert resp.model_used == specs[chain[1]].model_name, (
        f"应降级到第 2 跳 {chain[1]}，实际 {resp.model_used}")
    assert resp.fallback_used, "降级标记没置上"
    assert resp.provider_chain == chain[:2], (
        f"尝试序列应为前两跳，实际 {resp.provider_chain}")


def test_planner_actually_passes_a_budget():
    """★ 贯通点：改了 gateway 但规划层不传预算 = 没修。

    判据写成"源码里有这个 kwarg"，因为这是**跨模块接线**，
    单测 gateway 永远发现不了漏接。
    """
    src = (ROOT / "src" / "orchestration" / "planner.py").read_text(
        encoding="utf-8")
    assert "attempt_budget_sec=" in src, (
        "planner 没有传 attempt_budget_sec —— gateway 改了也不会生效")
    from src.orchestration.planner import _PLANNER_BUDGET_SEC

    assert 0 < _PLANNER_BUDGET_SEC <= 30, (
        f"规划层预算 {_PLANNER_BUDGET_SEC}s 不合理")


def test_planner_budget_reuses_tier_default_not_an_invented_number():
    """★ 预算必须来自 `_ATTEMPT_BUDGET["light"]`，**不许另造数字**。

    实测教训：我第一版拍了个 12s，理由是引用代码注释里的"本地 2~5s"
    （旧数字，非本次实测）—— 但冷路径实测 ≈14.2s（冷加载 9.5s + 生成 4.73s），
    于是规划层在冷启动下**必然失败**。项目已有的 20s 才是实测产物。

    这条断言把"单一权威默认值"钉住：以后要调，调 `_ATTEMPT_BUDGET`，
    而不是在 planner 里悄悄写第三个数字。
    """
    from src.infrastructure.llm.gateway import _ATTEMPT_BUDGET
    from src.orchestration.planner import _PLANNER_BUDGET_SEC

    assert _PLANNER_BUDGET_SEC == _ATTEMPT_BUDGET["medium"], (
        f"规划层预算 {_PLANNER_BUDGET_SEC} != medium 层权威默认值 "
        f"{_ATTEMPT_BUDGET['medium']} —— 出现了第二个数字来源")
    assert _PLANNER_BUDGET_SEC >= 15, (
        f"预算 {_PLANNER_BUDGET_SEC}s 低于实测冷路径 ≈14.2s，"
        "会把'能成功'变成'必然失败'")


def test_planner_failure_is_logged_not_silent():
    """静默回退不算合规：失败必须出声（否则又是"看起来像采集慢"）。"""
    src = (ROOT / "src" / "orchestration" / "planner.py").read_text(
        encoding="utf-8")
    assert "logger.warning" in src, "规划失败必须记日志，不能静默 return None"


# ============================================================
# 规划层离开本地（2026-09-28）
# ============================================================


def _planner_tier_literal() -> str | None:
    """用语法树读 `plan()` 里 `gateway.complete(<第一个实参>, ...)` 的字面量。

    ⚠️ 不要用字符串切片判断（实测踩过）：planner.py 的注释里大量提到 `light`，
    切片法会误报。判断"代码里有什么"要用 `ast`。
    """
    import ast

    tree = ast.parse((ROOT / "src" / "orchestration" / "planner.py")
                     .read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr == "complete"):
            continue
        if node.args and isinstance(node.args[0], ast.Constant):
            return str(node.args[0].value)
    return None


def test_planner_uses_a_cloud_tier_not_local():
    """★ 规划层必须用**云端主模型**的层，不许再用 `light`（本地）。

    用户原话：「规划层直接用一个云端免费的快速又不丢质量的模型，
    不就省去了本地模型各种判断？」

    证据：规划层曾挂 `light`（本地 qwen2.5:1.5b），代价是显存换入挂死
    （120294ms）、输出失控撞上限截断、以及一整套显存/驻留判断。

    ⚠️ 落地用的是既有 `medium` 层，**没有**自造 `planning` 层 ——
    自造层被两条既有护栏挡下（跨厂商备源 / 免费档不进 routing），
    详见 `docs/ADR_PLANNING_TIER.md`。
    """
    from src.infrastructure.llm.gateway import _load_model_config

    tier = _planner_tier_literal()
    assert tier, "没读到 planner 的 complete() 层名字面量 —— 结构变了"
    assert tier != "light", "planner 又回到 light（本地）层了"
    specs, chains, _lo = _load_model_config("configs/models.yaml")
    assert tier in chains, f"planner 用了未声明的层 {tier}"
    assert specs[chains[tier][0]].provider != "ollama", (
        f"planner 用的 {tier} 层主模型是本地 {chains[tier][0]} —— 显存问题会回来")


def test_planner_tier_primary_is_cloud_and_fallback_is_cross_provider():
    """规划层所在层：**云端主模型** + **跨厂商备源**。

    为什么备源必须跨厂商：熔断器**按 provider 生效**，同厂商的主备会一起被拒，
    等于没有兜底（本项目已有护栏 `test_routing_fallbacks_are_cross_provider`
    在管全局；这里钉住规划层用的那一层的具体形状）。
    """
    from src.infrastructure.llm.gateway import _load_model_config

    specs, chains, local_only = _load_model_config("configs/models.yaml")
    tier = "medium"
    chain = chains[tier]
    assert specs[chain[0]].provider != "ollama", (
        f"{tier} 层主模型是本地 {chain[0]} —— 显存问题会回来")
    providers = [specs[m].provider for m in chain]
    assert len(set(providers)) >= 2, (
        f"{tier} 层主备同厂商（{providers}）—— 熔断时一起被拒")
    assert tier not in local_only, f"{tier} 层不该被钉死本地"
