"""LLM 层级路由回归测试。

★ 2026-09-28 第十二轮：这些断言是**配置漂移的哨兵**。
AGENTS.md 记载过一次真实代价：9 条关于告警阈值的测试长期红灯被当背景噪音，
两天后同一个漂移以"客户抱怨邮件太多"的形式重现。

这里的每条断言都对应一个**实测过的性能/成本取舍**，改配置必须同步改测试
并在 docstring 里写明新的依据 —— 不允许"悄悄改回去"。
"""

from __future__ import annotations

import pytest

from src.core.config import get_settings
from src.infrastructure.llm.gateway import _load_model_config


@pytest.fixture(scope="module")
def routing():
    settings = get_settings()
    specs, chains, local_only = _load_model_config(settings.model_config_path)
    return specs, chains, local_only


def test_light_has_no_paid_fallback(routing):
    """`light` 层**不许挂付费档**（2026-09-28 第十九轮语义变更）。

    ══════════════════════════════════════════════════════════════════
    ⚠️ 语义变更记录（不是放宽判据，是前提变了）

    原判据：`light` 必须 `local_only: true`。
      理由：它的 fallback 是**付费**的 deepseek-flash —— 不钉死的话
      本地一抖动就悄悄花钱（2026-09-26 实测：事件告警阶段 ¥0.0196）。

    新判据：`light` **不得含付费 provider**（可以不再是 local_only）。
      ~~理由：链路已改为 `qwen-dashscope-flash → local_light →
      qwen-siliconflow-7b`~~（第十九轮原文，**已废止**）
      → **2026-09-28 第二十轮**现行链为
      `qwen-siliconflow-7b → qwen-dashscope-flash → local_light`
      —— **三跳全免费**，原判据要防的"悄悄花钱"在这条链上不存在。
      ⚠️ 次序**不在本文件复述**（复述过一次就漂移过一次）：以
      `configs/models.yaml` 为准，这里只断言"无付费档 + 链上有本地地板"。

    代价与替代护栏（诚实登记）：
      风险从"悄悄花钱"变成"**悄悄限流**"。由 `scripts/probe_free_providers.py`
      的准入判据 + 待补的「连续 429 锁回本地」落盘护栏接管。

    为什么 light 要改成免费档优先（实测支撑）：
      · `light` 占全部调用量 **82%**（1220/1485），日均 114,621 token
      · 本地 1774ms vs dashscope **407ms** → 快 4×
      · 百炼额度 100 万 token → light 单独可在 8.7 天耗尽
        → 所以**本地必须留在链上**（它不消耗配额，是可持续地板）
    ══════════════════════════════════════════════════════════════════
    """
    from src.infrastructure.llm.gateway import PAID_PROVIDERS

    specs, chains, _lo = routing
    chain = chains["light"]
    paid = [m for m in chain if specs[m].provider in PAID_PROVIDERS]
    assert not paid, (
        f"light 层挂了付费档 {paid} —— 本层是最热路径（82% 调用量），"
        "一旦静默落到付费档，账单会按调用量放大")
    assert any(specs[m].provider == "ollama" for m in chain), (
        f"light 层必须在链上保留本地模型（当前 {chain}）—— "
        "它是唯一**不消耗配额**的地板；去掉后免费额度耗尽即整链失败")


def test_medium_primary_is_cloud_not_local(routing):
    """★ `medium` 层 primary 必须是**云端**（2026-09-28 第十九轮修订）。

    ══════════════════════════════════════════════════════════════════
    ⚠️ 修订记录（保留了原判据的**核心意图**：不许回到本地）

    原判据：primary **必须是 `deepseek-flash`**。
      依据（实测 21 次采样）：本地 qwen3:8b 下 A05 38.1s / A06 52.9s，
      Ollama 单槽 → 实际串行 **≈91s**；同环境 deepseek-flash 172 tok/s。
      **这条意图完全保留** —— 本测试仍然禁止 primary 回到本地。

    新判据：primary 只要**不是本地**即可（付费或免费云端都行）。
      依据（2026-09-28 第十九轮实测，`scripts/probe_free_providers.py`）：
        qwen-dashscope-flash   p50 **448ms**（并发 10，n=40，**0/40 限流**）
        deepseek-flash         p50 **973ms**（并发 5，n=30）
      → dashscope 延迟更低且**免费**。

      与 172 tok/s 的关系：那两个数**不矛盾** —— 172 tok/s 是**吞吐**，
      448ms 是**延迟**。所以这里不替原判据"翻案"，而是**收窄它的断言范围**
      到"必须是云端"，把"选哪个云端"交给实测数据。

    保留的硬约束：链上必须有**付费档** —— 免费额度耗尽时降级到付费，
      而不是降级到失败或慢 20 秒的本地。
    ══════════════════════════════════════════════════════════════════
    """
    from src.infrastructure.llm.gateway import PAID_PROVIDERS

    specs, chains, local_only = routing
    chain = chains["medium"]
    assert specs[chain[0]].provider not in ("ollama",), (
        f"medium primary 回到了本地（{chain[0]}）—— "
        "A05+A06 会在 Ollama 单槽上串行 ≈91s"
    )
    assert any(specs[m].provider in PAID_PROVIDERS for m in chain), (
        f"medium 链上没有付费档（{chain}）—— 免费额度耗尽后会降级到"
        "失败或慢 20 秒的本地，而不是可用结果")
    assert "medium" not in local_only, (
        "medium 若保留 local_only: true，云端 primary 永远不会被调用")


def test_decision_primary_is_flash_not_pro(routing):
    """★ `decision` 层 primary 是 flash（2026-09-28 有意变更）。

    依据：v4-pro 实测 72 tok/s（A17 p50 33.7s / out 2431），
    flash 实测 172 tok/s —— 2.4× 生成速度差，且成本仅 1/3.3。
    质量回归时用 `MOSS_DECISION_USE_PRO=1` 临时切回，不要改 yaml。

    ⚠️ 2026-09-28 第十九轮：备源断言由点名 `local_medium` 改为
    **"必须有跨厂商备源 + 链尾有本地地板"** —— 因为 `local_medium`（8B）
    已退出所有链（用户裁定保留模型但不接线）。原判据的**意图**（跨厂商、
    不会一起熔断）完整保留，只是不再绑死具体模型名。
    """
    specs, chains, _lo = routing
    chain = chains["decision"]
    assert chain[0] == "deepseek-flash", (
        f"decision primary 应为 deepseek-flash，当前 {chain[0]}"
    )
    assert len({specs[m].provider for m in chain}) >= 2, (
        f"decision 的备源必须是**另一个 provider**（当前 {chain}）—— "
        "同 provider 的备源会跟着一起熔断，等于没兜底"
    )
    assert specs[chain[-1]].provider == "ollama", (
        f"decision 链尾应是本地模型（当前 {chain[-1]}）—— "
        "云端全挂时它是唯一还能出结论的一跳"
    )


def test_reasoning_uses_cloud_primary(routing):
    """`reasoning` 层 primary 是云端 flash（分析层 5-9 路并发）。

    依据：实测 A08/A09 延迟 8.2s/8.6s、速率 175/181 tok/s；
    换本地 8B 会退到 36-40 tok/s 且撞单槽串行。
    """
    specs, chains, local_only = routing
    assert chains["reasoning"][0] == "deepseek-flash"


def test_all_tiers_have_fallback(routing):
    """每一层都必须有 fallback —— 单点链在 provider 故障时整链失败。"""
    specs, chains, local_only = routing
    for tier, chain in chains.items():
        assert len(chain) >= 2, f"{tier} 层只有一跳（{chain}），没有兜底"
        # 两跳不能同 provider（同 provider 会一起熔断）
        providers = [specs[m].provider for m in chain]
        assert len(set(providers)) >= 2, (
            f"{tier} 层的降级链是同一 provider（{providers}）—— "
            "该 provider 熔断时主备会一起被拒，等于没兜底"
        )


def test_every_routed_model_is_registered(routing):
    """路由引用的每个模型都必须在 models 段登记（否则启动即 ConfigError）。"""
    specs, chains, local_only = routing
    for tier, chain in chains.items():
        for model in chain:
            assert model in specs, f"{tier} 层引用了未登记的模型 {model}"
