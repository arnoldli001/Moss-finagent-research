"""显存感知路由：拉不起来的本地模型让位给云端。

## 用户的原话（2026-09-28）

> 「本地显卡只有8G，你选择模型，**没有考虑硬件上限**，在 supervisor 规划使用的
>   模型在本地没了可用显存，无法加载，那就改用降级去用云端模型。两个各自正确的
>   护栏导致的交叉问题，**要去掉一个护栏**，规划层模型缺少显存无法拉起就改云端，
>   可以检测下当前显存剩余大小来判断是否走云端」

## 这条指令纠正了我上一版的修复方向

我上一版做的是「显式预算 → 快速失败 → **回退规则式规划**」：
**接受**本地模型跑不起来，只是别等 120s。
用户指出这是错的 —— 正确做法是**别让它跑不起来**：显存不够就换云端，
拿一个**真规划**回来。

## 交叉问题的两条护栏与取舍

| 护栏 | 处置 |
|---|---|
| `local_only: true`（防静默花钱） | **让它有条件**：显存拉不起来时不钉死 |
| `attempt_budget_sec`（防"成功但极慢"） | 保留（真挂死时仍需要它兜底） |

即「去掉一个护栏」去的是**钉死的无条件性**，不是去掉省钱意图。
改道会花钱，所以每次都要写审计、出日志 —— **静默改道等于偷偷花钱**。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.core.config import Settings
from src.infrastructure.llm.gateway import LOCAL_PROVIDERS, LLMGateway
from src.infrastructure.llm.models import LLMResponse
from src.infrastructure.llm.vram import (
    DEFAULT_MODEL_VRAM_MB,
    LocalCapacity,
    decide_local_usable,
)


# ============================================================
# ① 纯判据
# ============================================================

def test_resident_model_needs_no_new_vram():
    """已驻留 → 不需要新显存 → 用本地（最省也最快）。"""
    ok, why = decide_local_usable(
        model_name="qwen2.5:1.5b", required_mb=1000, free_mb=176,
        resident=frozenset({"qwen2.5:1.5b"}))
    assert ok, why
    assert "已驻留" in why


def test_insufficient_vram_rejects_local():
    """★ 核心：空闲显存不够且未驻留 → 必须判不可用（这正是 120s 挂死的起点）。

    实测数字：本机 RTX 4060 两个模型驻留后仅剩 **176MB** 空闲。
    """
    ok, why = decide_local_usable(
        model_name="qwen2.5:1.5b", required_mb=1000, free_mb=176,
        resident=frozenset())
    assert not ok, why
    assert "拉不起来" in why
    assert "176" in why and "1512" in why   # 1000 权重 + 512 余量


def test_sufficient_vram_keeps_local():
    ok, why = decide_local_usable(
        model_name="qwen2.5:1.5b", required_mb=1000, free_mb=7099,
        resident=frozenset())
    assert ok, why


def test_unjudgeable_vram_stays_local_not_cloud():
    """**判不了 ≠ 不够**。判不了时维持原配置（不花钱的一侧是安全侧）。

    如果这里判成"不可用"，非 N 卡机器 / 沙箱环境会**每一跳都去花钱**。
    """
    ok, why = decide_local_usable(
        model_name="qwen2.5:1.5b", required_mb=1000, free_mb=None,
        resident=frozenset())
    assert ok, why
    assert "判不了" in why


def test_margin_is_applied():
    """余量必须计入：刚好等于权重时**不算够**（还要 KV cache / compute buffer）。"""
    ok, _ = decide_local_usable(model_name="m", required_mb=1000,
                                free_mb=1000, resident=frozenset(),
                                margin_mb=512)
    assert not ok, "1000MB 空闲装 1000MB 权重 —— 余量没生效"


# ============================================================
# ② 网关行为：钉死本地时也要看显存
# ============================================================

class RecordingProvider:
    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.calls: list[str] = []

    async def chat(self, spec, system, prompt, *, json_mode=False):
        self.calls.append(spec.model_name)
        return LLMResponse(content=f"answer::{self.tag}",
                           model_used=spec.model_name, provider=spec.provider,
                           tokens_in=10, tokens_out=5,
                           prompt_hash="ph", response_hash="rh")


@pytest.fixture
def gw_env(tmp_dir):
    return Settings(model_config_path="configs/models.yaml",
                    llm_cache_dir=tmp_dir, llm_audit_dir=tmp_dir,
                    llm_cache_enabled=False)


@pytest.fixture
def routing():
    """`configs/models.yaml` **现读**的 `(模型规格表, 层降级链表)`。

    为什么现读：本文件的期望值（"改道落到哪一跳"）必须**从链推导**，
    写死模型名会在每次改 routing 时变红（2026-09-28 第二十轮的真实代价）。
    """
    from src.infrastructure.llm.gateway import _load_model_config

    specs, chains, _lo = _load_model_config("configs/models.yaml")
    return specs, chains


def _local_hop(chain: list[str], specs) -> str:
    hops = [m for m in chain if specs[m].provider in LOCAL_PROVIDERS]
    assert hops, f"链上没有本机模型（{chain}）—— 本文件的用例前提不成立"
    return hops[0]


def _cloud_providers(chain: list[str], specs) -> list[str]:
    """链上的云端 provider（去重、按链序）—— 改道只可能落到它们身上。"""
    out: list[str] = []
    for name in chain:
        provider = specs[name].provider
        if provider not in LOCAL_PROVIDERS and provider not in out:
            out.append(provider)
    return out


def _gw_recording(gw_env, chain: list[str], specs):
    """建一个"本地 / 云端各一个替身"的网关，返回 `(gw, local, cloud)`。

    云端替身按**链上真实存在的 provider** 注册：不注册的 provider 会被
    `complete()` 当成"提供商未注册"跳过（且**不写审计**，见 gateway.py），
    那样用例会**静默少测一跳**。
    """
    local = RecordingProvider("local")
    cloud = RecordingProvider("cloud")
    gw = LLMGateway(
        settings=gw_env,
        providers={**{p: cloud for p in _cloud_providers(chain, specs)},
                   "ollama": local},
        cache=None)
    return gw, local, cloud


@pytest.fixture(autouse=True)
def _fresh_circuit_breakers(monkeypatch):
    from src.infrastructure.llm import circuit_breaker as cbmod

    monkeypatch.setattr(cbmod, "_registry", cbmod.CircuitBreakerRegistry())
    yield


def _pin(gw: LLMGateway, *, free_mb: int | None,
         resident: frozenset[str] = frozenset()) -> None:
    """把网关的显存能力换成受控假探针（不碰真显卡）。"""
    gw._local_capacity = LocalCapacity(  # noqa: SLF001
        free_probe=lambda: free_mb,
        resident_probe=lambda _url: resident,
        ttl_sec=0.0,
    )


async def test_vram_shortage_reroutes_to_cloud(gw_env, routing):
    """★ 用户要的核心行为：显存不够 → 不钉死本地 → 落到云端备源。

    这就是「去掉一个护栏」的机器复现。修之前：本地拉不起来 → 干等到超时 →
    静默回退规则式规划。修之后：**拿到一个真结果**（来自云端）。

    ## ★ 2026-09-28 第二十轮：**必须显式 `local_only=True`**

    原实现依赖 `light` 在配置里被钉死本地 —— 那时它是全仓**唯一**的
    local_only 层，`complete()` 的 `pin_local` 分支才是显存改道的触发条件。
    第二十轮该钉死被移除（`light` 首位改为免费的 `qwen-siliconflow-7b`，
    依据见 `configs/models.yaml` 第 97~99 行），**全仓已无 local_only 层**。
    改道机制本身没坏，但它的触发条件从此只能来自**调用点显式声明** ——
    所以条件写在调用处，与 `models.yaml` 的配置解耦。

    改道的目标跳也**从配置推导**（链上第一个云端模型），不写死模型名。
    """
    specs, chains = routing
    chain = chains["light"]
    cloud_providers = _cloud_providers(chain, specs)
    assert cloud_providers, f"light 链上没有云端备源（{chain}）—— 改道无路可走"
    cloud_hop = next(m for m in chain if specs[m].provider == cloud_providers[0])

    gw, local, cloud = _gw_recording(gw_env, chain, specs)
    _pin(gw, free_mb=100)          # 显存严重不足，且什么都没驻留

    resp = await gw.complete("light", "s", "p", agent_id="planner",
                             local_only=True)
    assert resp.content == "answer::cloud", (
        f"显存不足时应改走云端，实际用了 {resp.model_used}")
    assert resp.model_used == specs[cloud_hop].model_name, (
        f"改道应落在链上第一个云端跳 {cloud_hop}，实际 {resp.model_used}")
    assert not local.calls, "本地模型不该被尝试调用（它拉不起来）"
    assert cloud.calls == [specs[cloud_hop].model_name], (
        f"云端应被调用一次（{cloud_hop}），实际 {cloud.calls}")


async def test_resident_model_does_not_spend_cloud_money(gw_env, routing):
    """反向护栏：模型已驻留时必须**继续用本地**，不许无谓地花钱。

    ⚠️ 2026-09-28 第二十轮：与上面那条配对的**反向护栏必须加同一个显式
    `local_only=True`** —— 否则它测的就不是显存判据：`light` 已不再被配置
    钉死，默认调用会直接落到**免费的**云端首位，"没有改道"于是退化成
    "根本没需要改道"，断言**空转通过**（这正是本项目登记过的"假绿"）。
    """
    specs, chains = routing
    chain = chains["light"]
    local_hop = _local_hop(chain, specs)

    gw, _local, cloud = _gw_recording(gw_env, chain, specs)
    _pin(gw, free_mb=100, resident=frozenset({specs[local_hop].model_name}))

    resp = await gw.complete("light", "s", "p", agent_id="planner",
                             local_only=True)
    assert resp.content == "answer::local", (
        f"模型已驻留却改走了云端（白花钱）：{resp.model_used}")
    assert not cloud.calls


async def test_ample_vram_keeps_local(gw_env, routing):
    """显存充足 → 照常本地，不花云端钱。

    ⚠️ 2026-09-28 第二十轮：加显式 `local_only=True`（同上的"防空转"理由）。
    """
    specs, chains = routing
    chain = chains["light"]

    gw, _local, cloud = _gw_recording(gw_env, chain, specs)
    _pin(gw, free_mb=7099)

    resp = await gw.complete("light", "s", "p", agent_id="planner",
                             local_only=True)
    assert resp.content == "answer::local", resp.model_used
    assert not cloud.calls


async def test_unjudgeable_vram_keeps_local(gw_env, routing):
    """判不了显存（无 N 卡 / 沙箱）→ 维持原配置，不花钱。

    ⚠️ 2026-09-28 第二十轮：加显式 `local_only=True`（同上的"防空转"理由）。
    """
    specs, chains = routing
    chain = chains["light"]

    gw, _local, _cloud = _gw_recording(gw_env, chain, specs)
    _pin(gw, free_mb=None)

    resp = await gw.complete("light", "s", "p", agent_id="planner",
                             local_only=True)
    assert resp.content == "answer::local", resp.model_used


async def test_reroute_is_audited_not_silent(gw_env, tmp_dir, routing):
    """★ 改道**必须留痕**：静默改道等于偷偷花钱。

    ⚠️ 2026-09-28 第二十轮：加显式 `local_only=True` —— 改道（`vram_reroute`）
    只在"调用方要求只用本地"时才会发生；`light` 的层级钉死已被移除，
    不显式传参就**没有改道可审计**，这条会退化成"审计里恰好没有它"。
    """
    specs, chains = routing
    chain = chains["light"]

    gw, _local, _cloud = _gw_recording(gw_env, chain, specs)
    _pin(gw, free_mb=100)
    await gw.complete("light", "s", "p", agent_id="planner", local_only=True)

    audit_file = Path(tmp_dir) / "llm_audit.jsonl"
    assert audit_file.exists(), "没有写审计"
    text = audit_file.read_text(encoding="utf-8")
    assert "vram_reroute" in text, (
        "改走云端没有登记 —— 花钱必须可见。审计内容：" + text[:300])


# ============================================================
# ③ 配置贯通点
# ============================================================

def test_local_models_declare_vram_requirement():
    """本地模型必须在 models.yaml 声明 `vram_mb`，否则只能靠兜底估算。

    实测教训：选模型时没写显存需求，网关就无法判断"拉不拉得起来"。
    """
    from src.infrastructure.llm.gateway import _load_model_config

    specs, _chains, _local_only = _load_model_config("configs/models.yaml")
    missing = [n for n, s in specs.items()
               if s.provider == "ollama" and not s.vram_mb]
    assert not missing, (
        f"这些本地模型没声明 vram_mb：{missing}（会退化成 "
        f"{DEFAULT_MODEL_VRAM_MB}MB 的兜底估算）")


def test_declared_vram_is_plausible():
    """声明的显存需求要与参数规模量级相符（防手滑写错一位数）。"""
    from src.infrastructure.llm.gateway import _load_model_config

    specs, _c, _l = _load_model_config("configs/models.yaml")
    for name, s in specs.items():
        if s.provider != "ollama":
            continue
        assert 200 <= s.vram_mb <= 8000, (
            f"{name} 的 vram_mb={s.vram_mb} 不合理（本机总共只有 8187MB）")
