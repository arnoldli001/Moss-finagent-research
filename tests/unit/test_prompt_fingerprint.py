"""审计字段 `prompt_hash` 的唯一口径（判据）。

## 这条判据防的是什么

`prompt_hash` 有**两条生产写入路径**，历史上各写一份实现：

| 路径 | 旧实现 | 特征 |
|---|---|---|
| `LLMGateway.complete()` | `cache_key(system, prompt)` | 先 `normalize_text`（小写 + 去标点） |
| `providers._wrap_response()` | `_sha256(system + "\\x00" + prompt)` | 原始字节 |

同一个 prompt 走两条路径得到**两个不同的 hash**，而**没有任何报错** ——
审计里"这两次调用是不是同一个 prompt"这个问题**无法回答**。
这属于本仓库明令的「同一判断两份实现」+「静默失败」。

修法：抽出 `cache.prompt_fingerprint()` 作唯一实现，两条路径都调它。

## 判据怎么做到"删掉实现就红"

- **provider 侧**：直接调生产函数 `_wrap_response()`，断言它产出的
  `prompt_hash` == 规范式 —— 改回旧写法立刻不等（行为判据）。
- **网关侧**：`prompt_hash` 在 `complete()` 内联计算，无法单独调用；
  故用**调用点源码断言**（钉的是"有没有调那个唯一实现"，不是"定义存在与否"）。
  ⚠️ 这条是形状级判据，注释里写明它的强度边界，不冒充行为判据。
"""

from __future__ import annotations

import hashlib
import inspect

from src.infrastructure.llm.cache import cache_key, prompt_fingerprint
from src.infrastructure.llm.models import ModelSpec
from src.infrastructure.llm.providers import _wrap_response

SYS = "你是投研分析师"
PROMPT = "分析 PE(TTM):600036 是否高估"


def _canonical(system: str, prompt: str) -> str:
    """规范式：原始字节 + NUL 分隔。写成字面量，作为判据的锚（不引用被测函数）。"""
    return hashlib.sha256(f"{system}\x00{prompt}".encode()).hexdigest()


# ======================================================================
# 1. 指纹本身的性质
# ======================================================================

def test_fingerprint_is_deterministic_across_calls():
    """同一 (system, prompt) 恒等 —— 换进程、换路径都必须一样。"""
    assert prompt_fingerprint(SYS, PROMPT) == prompt_fingerprint(SYS, PROMPT)


def test_fingerprint_does_not_normalize():
    """★ 关键性质：**不做规范化** —— `PE(TTM)` 与 `pettm` 必须是两个指纹。

    若有人把 `normalize_text` 加进 `prompt_fingerprint()`（"顺手统一一下"），
    本用例立刻变红：两个不同的 prompt 会被记成同一个，审计失去分辨力。
    """
    assert prompt_fingerprint("系统", "PE(TTM):600036") != prompt_fingerprint(
        "系统", "pettm:600036")

    # 自证：本用例真的在测"规范化与否"，不是碰巧不等 ——
    # 缓存键**本来就该**把二者归一（换个写法应命中），这正是它不能当审计指纹的原因。
    assert cache_key("系统", "PE(TTM):600036") == cache_key("系统", "pettm:600036")


def test_fingerprint_separates_system_from_prompt():
    """`"AB"+"C"` 与 `"A"+"BC"` 不许撞 —— 分隔符必须真的起分隔作用。"""
    assert prompt_fingerprint("AB", "C") != prompt_fingerprint("A", "BC")


def test_fingerprint_handles_empty_inputs_without_crashing():
    """空输入是合法输入：返回 64 位 hex，且 `("", "")` 与 `("", "x")` 可区分。"""
    h = prompt_fingerprint("", "")
    assert len(h) == 64
    assert h != prompt_fingerprint("", "x")


# ======================================================================
# 2. ★ 生产实现（provider 侧行为判据）
# ======================================================================

def _spec() -> ModelSpec:
    return ModelSpec(
        name="local_medium", provider="ollama", model_name="qwen2.5:1.5b",
        base_url="http://localhost:11434",
    )


def test_provider_wrap_response_emits_canonical_prompt_hash():
    """★ 调**生产函数** `_wrap_response()`，断言它产出的就是规范式。

    改回 `_sha256(system + "\\x00" + prompt)` 之外的任何写法（或引入规范化），
    本用例立刻红。这一条是行为判据：它跑的是生产代码，不是测试里的复算。
    """
    resp = _wrap_response(_spec(), SYS, PROMPT, "答案", 10, 5, started=0.0)
    assert resp.prompt_hash == _canonical(SYS, PROMPT)


def test_provider_hash_differs_from_cache_key_for_punctuation_variants():
    """★ 反事实：provider 的产出与「缓存键口径」必须**不同**。

    若 provider 侧被改回用缓存键口径，这条会红 —— 它保证上面那条
    "等于规范式"不是因为两种口径碰巧一致。
    """
    resp = _wrap_response(_spec(), SYS, "PE(TTM):600036", "答案", 10, 5, started=0.0)
    assert resp.prompt_hash != cache_key(SYS, "PE(TTM):600036")


# ======================================================================
# 3. 网关侧调用点（形状判据，强度边界写在 docstring 里）
# ======================================================================

def test_gateway_uses_the_single_implementation_at_its_call_sites():
    """★ 钉住网关的**调用点**：不许再用缓存键当审计指纹。

    强度边界（不冒充行为判据）：网关的 `prompt_hash` 在 `complete()` 内联计算，
    没有可单独调用的出口，所以这里只能断言源码里没有旧写法、并调了新写法。
    真正的端到端等价由本轮复算过的"两条路径同函数"保证；
    若将来网关也抽出独立的 `_fingerprint()`，应把本用例升级为行为判据。
    """
    from src.infrastructure.llm import gateway as gateway_mod
    from src.infrastructure.llm import providers as providers_mod

    gw_src = inspect.getsource(gateway_mod)
    pv_src = inspect.getsource(providers_mod)

    assert "prompt_hash=cache_key(" not in gw_src, "网关还在用缓存键当审计指纹"
    assert "prompt_hash=_sha256(" not in pv_src, "provider 又自己拼哈希 ⇒ 两份实现"
    assert "prompt_hash=prompt_fingerprint(" in gw_src, "网关没走唯一实现"
    assert "prompt_hash=prompt_fingerprint(" in pv_src, "provider 没走唯一实现"


def test_cache_module_no_longer_claims_cache_key_serves_audit():
    """`cache_key` 的 docstring 不许再说"给审计 prompt_hash 用"（口径已分家）。"""
    from src.infrastructure.llm import cache as cache_mod

    doc = inspect.getdoc(cache_mod.cache_key) or ""
    assert "审计" in doc and "prompt_fingerprint" in doc, (
        "cache_key 的 docstring 必须显式指向 prompt_fingerprint，"
        "否则下一个人还会拿它当审计指纹"
    )
