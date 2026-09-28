"""LLM 通路贯通点护栏 —— 每一层「真的被调用」且「真的能降级」。

## 为什么需要（2026-09-28 实测，最贵的一次静默降级）

用户问「投研分析的性能优化彻底了吗」。实测发现：

    修之前那几轮：审计日志里**只有 `supervisor_planner` 一条** LLM 调用
                   —— 分析层 A08/A09/A17 **一次都没跑**。
    而 `pytest`：**5275 passed / 0 failed**。

即：**整条分析链静默降级，2575 条测试没有一条抓到。** 端到端还因此显得"很快"
（1.80s），让人误以为优化成功 —— 实际是没干活。

**教训**：现有测试大量是"单元级替身"，它们证明"组件本身对"，但**不能证明
"这条通路被走通了"**。组件全绿 + 通路全断 = 全绿。

## 本文件钉住四件事

| 判据 | 抓什么 |
|---|---|
| ① 每个 Agent 的 `task_tier` 都在 models.yaml 里声明 | 拼错的层名、加了 Agent 忘了配层 |
| ② 每条链引用的模型都存在 | 悬空引用（改名后漏改一处） |
| ③ **每一层都能降级**（主模型失败 → 备源接管） | "配置里写了备源"不等于"备源真的接得住" |
| ④ 分析层 Agent 所属的层**必须有备源** | 关键路径上的单点 |

运行时的"没被降级"检查见 `scripts/audit_llm_paths.py`（静态测试证明不了运行时）。
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from src.core.config import Settings
from src.infrastructure.llm.gateway import LLMGateway, _load_model_config
from src.infrastructure.llm.models import LLMResponse

ROOT = Path(__file__).resolve().parents[2]

#: 分析层（A08–A16）：**关键路径**，失败会静默降级成"没有分析"。
_ANALYSIS_MARK = "/analysis/"

#: ⚠️ 必须扫**所有** `.py`，不能只扫 `agent.py`：
#: 分析层 9 个 Agent 的 `task_tier` **只写在 `analysis/base.py`**（子类继承），
#: 只扫 `agent.py` 会**一个都扫不到** → 判据空转通过（实测踩过）。
_AGENT_GLOB = "src/domain/agents/**/*.py"


def _declared_tiers() -> list[str]:
    _s, chains, _lo = _load_model_config("configs/models.yaml")
    return sorted(chains)


def _agent_task_tiers() -> dict[str, str]:
    """`{相对路径: task_tier}` —— 从**语法树**读类属性，不导入（快且无副作用）。

    用 `ast` 而不是正则：本项目实测正则在注释/字符串/换行上会静默少算。
    """
    found: dict[str, str] = {}
    for path in sorted(ROOT.glob(_AGENT_GLOB)):
        rel = path.relative_to(ROOT).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for stmt in node.body:
                # 形如 `task_tier: TaskTier = "medium"` 或 `task_tier = "medium"`
                targets: list[ast.expr] = []
                value: ast.expr | None = None
                if isinstance(stmt, ast.AnnAssign):
                    targets, value = [stmt.target], stmt.value
                elif isinstance(stmt, ast.Assign):
                    targets, value = stmt.targets, stmt.value
                if not value or not isinstance(value, ast.Constant):
                    continue
                if any(isinstance(t, ast.Name) and t.id == "task_tier"
                       for t in targets):
                    found[rel] = str(value.value)
    return found


# ============================================================
# ① 层名必须已声明
# ============================================================


def test_every_agent_task_tier_is_declared():
    """★ 每个 Agent 声明的 `task_tier` 必须在 models.yaml 里有对应路由。

    拼错层名（`reasonning`）或加了 Agent 忘了配层，都会在**运行时**才炸，
    而且症状是"这一层没有路由"而不是"层名拼错了"。
    """
    declared = set(_declared_tiers())
    tiers = _agent_task_tiers()
    assert tiers, "一个 Agent 的 task_tier 都没扫到 —— 扫描规则坏了（先自证）"

    bad = {p: t for p, t in tiers.items() if t not in declared}
    assert not bad, (
        f"这些 Agent 声明了未在 models.yaml 里配置的层：{bad}；"
        f"已声明的层={sorted(declared)}")


def test_analysis_layer_agents_are_discovered():
    """自证：分析层必须被扫到，否则上面的判据是**空转**（假绿）。"""
    tiers = _agent_task_tiers()
    analysis = {p: t for p, t in tiers.items() if _ANALYSIS_MARK in p}
    assert analysis, (
        f"没有扫到任何分析层 Agent（glob={_AGENT_GLOB}）——判据会空转通过")
    assert all(t for t in analysis.values()), analysis


# ============================================================
# ② 链引用的模型必须存在
# ============================================================


def test_every_chain_entry_exists_in_models():
    """链里不能有悬空引用（改了模型名但漏改 routing）。"""
    specs, chains, _lo = _load_model_config("configs/models.yaml")
    dangling = {tier: [m for m in chain if m not in specs]
                for tier, chain in chains.items()}
    dangling = {k: v for k, v in dangling.items() if v}
    assert not dangling, f"routing 引用了未定义的模型：{dangling}"


def test_every_declared_tier_is_in_tasktier_literal():
    """贯通点：models.yaml 加了层，`TaskTier` 字面量必须跟上。

    ⚠️ 用 `typing.get_args()`，**不要** `str(TaskTier)` ——
    后者是 `typing.Literal['light', ...]`，里面是**单引号**，
    按双引号查会**恒为假**（实测踩过：5 个层全被报成"缺失"）。
    """
    from typing import get_args

    from src.infrastructure.llm.models import TaskTier

    allowed = set(get_args(TaskTier))
    assert allowed, "get_args(TaskTier) 为空 —— 字面量结构变了，判据需重写"
    missing = [t for t in _declared_tiers() if t not in allowed]
    assert not missing, (
        f"这些层在 models.yaml 里但不在 TaskTier 字面量里：{missing}；"
        f"字面量={sorted(allowed)}")


# ============================================================
# ③ 每一层都要**真的能降级**
# ============================================================

class FailByModelName:
    """按**模型名**决定成败的替身。

    ⚠️ 必须按模型名而不是厂商（实测踩过）：`planning` 的链是
    `[deepseek-flash, deepseek-v4-pro]` —— **同厂商两个模型**。
    按 provider 建替身会让两跳一起失败，于是"没有降级"被误报成缺陷。

    替身选择（按断言目标）：断言的是**交互行为**（降级链有没有前进），
    所以用可控失败的 Stub。
    """

    def __init__(self, *, fail_models: set[str]) -> None:
        self.fail_models = fail_models
        self.calls: list[str] = []

    async def chat(self, spec, system, prompt, *, json_mode=False, **kw):
        self.calls.append(spec.model_name)
        if spec.model_name in self.fail_models:
            from src.core.exceptions import LLMGatewayError

            raise LLMGatewayError(f"模拟主模型故障：{spec.model_name}")
        return LLMResponse(content="ok", model_used=spec.model_name,
                           provider=spec.provider, tokens_in=5, tokens_out=2,
                           prompt_hash="ph", response_hash="rh")


@pytest.fixture
def gw_env(tmp_dir):
    return Settings(model_config_path="configs/models.yaml",
                    llm_cache_dir=tmp_dir, llm_audit_dir=tmp_dir,
                    llm_cache_enabled=False)


@pytest.fixture(autouse=True)
def _fresh_circuit_breakers(monkeypatch):
    from src.infrastructure.llm import circuit_breaker as cbmod

    monkeypatch.setattr(cbmod, "_registry", cbmod.CircuitBreakerRegistry())
    yield


def _providers_for(chain: list[str], specs) -> dict[str, FailByModelName]:
    """只让**主模型**失败，同厂商的备源仍可用。"""
    primary = specs[chain[0]].model_name
    provs: dict[str, FailByModelName] = {}
    for name in chain:
        p = specs[name].provider
        if p not in provs:
            provs[p] = FailByModelName(fail_models={primary})
    return provs


@pytest.mark.parametrize("tier", _declared_tiers())
def test_tier_can_degrade_to_its_fallback(tier, gw_env):
    """★ **每一层**都要能降级：主模型失败 → 备源接管并返回结果。

    "配置里写了 fallback" ≠ "fallback 真的接得住"。本项目实测过相反的例子：
    降级链在 `local_only` 钉死时被裁成 1 跳，备源**永远轮不到**。

    `local_only` 的层跳过 —— pin_local 会在**运行时**把链裁到 1 跳，
    它们设计上就没有备源（由 `test_single_hop_tiers_are_intentional` 登记）。
    """
    specs, chains, local_only = _load_model_config("configs/models.yaml")
    if tier in local_only:
        pytest.skip(f"{tier} 是 local_only 层，运行时会被裁成单跳，无备源")
    chain = chains[tier]
    if len(chain) < 2:
        pytest.skip(f"{tier} 是单跳层（{chain}），无备源可测")

    provs = _providers_for(chain, specs)
    gw = LLMGateway(settings=gw_env, providers=provs, cache=None)
    # 显存探测不参与本判据，注入"判不了"→维持原配置，避免真起子进程
    from src.infrastructure.llm.vram import LocalCapacity

    gw._local_capacity = LocalCapacity(  # noqa: SLF001
        free_probe=lambda: None, resident_probe=lambda _u: frozenset(),
        ttl_sec=0.0)

    resp = asyncio.run(gw.complete(tier, "s", "p", agent_id="t"))
    assert resp.content == "ok", (
        f"{tier} 层主模型失败后没有拿到可用结果（链={chain}）")
    assert resp.fallback_used, (
        f"{tier} 层没有走降级（链={chain}）—— 备源形同虚设")


def test_single_hop_tiers_are_intentional():
    """单跳层必须**显式登记**，不能悄悄出现。

    单跳 = 没有备源 = 该层一旦主模型挂掉就必然失败。
    本项目实测过它的代价：`light` 钉死本地 → 规划层挂死 120s 且无处可退。
    所以单跳要么是**有意为之**（下面的白名单 + 理由），要么是缺陷。
    """
    _s, chains, local_only = _load_model_config("configs/models.yaml")
    single = {t: chains[t] for t in chains if len(chains[t]) < 2}
    assert set(single) <= set(local_only), (
        f"这些单跳层**不是** local_only，因此是缺陷（白写了备源又裁掉）："
        f"{ {k: v for k, v in single.items() if k not in local_only} }")


def test_analysis_layer_tier_has_a_fallback():
    """★ 分析层所在层**必须有备源**（关键路径不许是单点）。

    实测背景：分析层静默降级过一次（一次 LLM 都没调），而端到端看起来更快 ——
    没有备源会让这种降级更容易发生且更难察觉。
    """
    specs, chains, _lo = _load_model_config("configs/models.yaml")
    tiers = {t for p, t in _agent_task_tiers().items() if "/analysis/" in p}
    assert tiers, "没扫到分析层 Agent"
    no_fb = [t for t in tiers if len(chains.get(t, [])) < 2]
    assert not no_fb, (
        f"分析层用的这些层没有备源：{no_fb} —— 关键路径单点")
