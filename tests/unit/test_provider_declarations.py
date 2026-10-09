"""付费/本地判据必须来自**提供商自声明**，而不是网关里的硬编码集合（判据）。

## 这条判据防的是什么（`CHG-0192` 修的真实缺陷）

网关原先把两个集合写死在自己文件里：

```python
PAID_PROVIDERS  = frozenset({"deepseek"})
LOCAL_PROVIDERS = frozenset({"ollama"})
```

于是**新增一个付费提供商时，没有任何机制提醒你去登记它** ⇒
`uses_paid` 对它恒为 `False` ⇒ **它花掉的钱不进 token 预算**。
预算检查、付费只做链首、降级链裁剪这几条护栏**全部静默失效**：
没有任何报错，只是钱不再被挡住。

反方向也踩过（2026-09-28 实测）：免费云端（dashscope/siliconflow/zhipu）
不在 `PAID_PROVIDERS` 里 ⇒ **被误判成本地模型** ⇒ 一接进来就被显存检查拦掉，
报错还是"qwen-flash 拉不起来"（而它根本不在本机）。

## 判据强度

`test_declared_paid_provider_enters_the_budget` —— **反事实判据**：
同一个 chain，**声明为付费**时必须进入预算分支、**未声明**时不进。
把 `_provider_is_paid` 改回读硬编码集合 ⇒ 前一半立刻红。

`test_free_cloud_provider_is_not_local` —— 免费云端**不是本地**（那次的教训）。
"""

from __future__ import annotations

from typing import Any

from src.infrastructure.llm.gateway import (
    PAID_PROVIDERS,
    _provider_is_local,
    _provider_is_paid,
)
from src.infrastructure.llm.providers import (
    PROVIDER_DECLARATIONS,
    build_providers,
    declare_provider,
)


class _FakeProvider:
    """可注入的假提供商（与 `BaseProvider` 同形，但**不带自声明**）。"""

    def __init__(self, name: str, *, paid: bool | None = None,
                 local: bool | None = None) -> None:
        self.name = name
        if paid is not None:
            self.paid = paid
        if local is not None:
            self.local = local

    async def chat(self, spec, system, prompt, **kw):  # pragma: no cover
        raise NotImplementedError


# ======================================================================
# 1. 声明的语义
# ======================================================================

def test_builtin_declarations_cover_every_registered_provider():
    """★ 内置提供商**每个都必须有自声明** —— 新增供应商忘了登记就在这里红。

    这是"新增即注册"的机器判据：`build_providers()` 注册了什么，
    `PROVIDER_DECLARATIONS` 就必须声明什么。
    """
    registered = set(build_providers())
    declared = set(PROVIDER_DECLARATIONS)
    missing = registered - declared
    assert not missing, (
        f"这些提供商已注册但**没有自声明** ⇒ 它们的付费/本地判据会落到硬编码回退："
        f"{sorted(missing)}\n修法：在 providers.PROVIDER_DECLARATIONS 里加一行"
    )


def test_every_registered_provider_carries_the_declaration():
    """自声明必须真的**打到实例上**（不是只写在表里）。"""
    for name, provider in build_providers().items():
        assert isinstance(getattr(provider, "paid", None), bool), (
            f"{name} 实例上没有 paid 声明 ⇒ 网关只能回退硬编码集合"
        )
        assert isinstance(getattr(provider, "local", None), bool), (
            f"{name} 实例上没有 local 声明"
        )


def test_local_and_paid_are_independent_predicates():
    """★ 两个谓词各司其职：本地**不等于**免费，免费云端**不是**本地。

    2026-09-28 的教训就是有人拿 `PAID_PROVIDERS` 当"是否本地"用。
    """
    provs = {
        "ollama": _FakeProvider("ollama", paid=False, local=True),
        "dashscope": _FakeProvider("dashscope", paid=False, local=False),
        "deepseek": _FakeProvider("deepseek", paid=True, local=False),
    }
    assert _provider_is_local("ollama", provs) is True
    assert _provider_is_paid("ollama", provs) is False
    # 免费云端：既不付费、**也不在本机**
    assert _provider_is_paid("dashscope", provs) is False
    assert _provider_is_local("dashscope", provs) is False, (
        "免费云端被当成本地 ⇒ 会被显存检查拦掉（那次实测的错法）"
    )
    assert _provider_is_paid("deepseek", provs) is True


# ======================================================================
# 2. ★ 反事实：声明生效 vs 回退
# ======================================================================

def test_declared_paid_provider_enters_the_budget():
    """★ 核心反事实：**声明为付费**的提供商必须被判为付费，哪怕它不在硬编码集合里。

    · 若 `_provider_is_paid` 读自声明 ⇒ `True`（本用例过）；
    · 若改回"只读 `PAID_PROVIDERS`" ⇒ `False` ⇒ **本用例红**。
      那正是缺陷：新提供商的调用不进 token 预算。
    """
    newbie = _FakeProvider("brand-new-paid-llm", paid=True, local=False)
    assert "brand-new-paid-llm" not in PAID_PROVIDERS, "前提：它不在硬编码集合里"
    assert _provider_is_paid("brand-new-paid-llm", {"brand-new-paid-llm": newbie}) is True, (
        "★ 声明为付费却没被判为付费 ⇒ 它的调用不进 token 预算（静默失效）"
    )


def test_undeclared_provider_falls_back_to_the_legacy_sets():
    """未声明的第三方实现必须**回退**到旧集合（向后兼容，不打破既有契约）。

    注入 Fake provider 的测试、以及直接实现 `BaseProvider` 的第三方
    都不带自声明 —— 它们的行为必须与改动前一致。
    """
    bare = _FakeProvider("deepseek")            # 不带任何声明
    assert _provider_is_paid("deepseek", {"deepseek": bare}) is True, (
        "未声明时没能回退到 PAID_PROVIDERS ⇒ 打破了既有契约"
    )
    ollama_bare = _FakeProvider("ollama")
    assert _provider_is_local("ollama", {"ollama": ollama_bare}) is True


def test_declaration_wins_over_the_legacy_set():
    """自声明**优先**于硬编码集合（否则"改表"这件事没有意义）。"""
    # deepseek 在旧集合里是付费；这里声明成免费 ⇒ 必须听声明的
    free_deepseek = _FakeProvider("deepseek", paid=False, local=True)
    assert _provider_is_paid("deepseek", {"deepseek": free_deepseek}) is False
    assert _provider_is_local("deepseek", {"deepseek": free_deepseek}) is True


def test_declare_provider_tags_from_the_table():
    """`declare_provider()` 是唯一的打标入口（表是唯一事实源）。"""
    p = _FakeProvider("ollama")
    assert declare_provider(p) is p, "必须返回同一个对象（便于链式构造）"
    assert (p.paid, p.local) == PROVIDER_DECLARATIONS["ollama"]


def test_unknown_provider_defaults_to_not_paid_not_local():
    """表里没有的 provider 打标为"不付费、不在本机" —— 安全的一侧。"""
    p = _FakeProvider("totally-unknown")
    declare_provider(p)
    assert (p.paid, p.local) == (False, False), (
        "未知 provider 默认成付费会拦掉调用、默认成本地会被显存检查拦 —— "
        "两者都是「更容易出错」的一侧"
    )


# ======================================================================
# 3. 报错文案不许与事实不符
# ======================================================================

def test_error_message_lists_actually_registered_paid_providers():
    """`local_only` 报错里的"计费提供商"必须现算，不许打印常量。

    否则新增付费商后，报错会告诉用户"计费提供商=[deepseek]"，
    而链上其实还有别人 —— **文案与事实不符**。
    """
    import inspect

    from src.infrastructure.llm import gateway as gw

    src = inspect.getsource(gw)
    assert "计费提供商={sorted(PAID_PROVIDERS)}" not in src, (
        "报错文案还在打印硬编码常量（新增付费商后会与事实不符）"
    )
    assert "_paid_provider_names()" in src


def test_paid_provider_names_reads_the_live_registry():
    """`_paid_provider_names()` 从**活注册表**现算。"""
    from src.infrastructure.llm.gateway import LLMGateway

    gw_obj = LLMGateway.__new__(LLMGateway)      # 不跑 __init__（要起配置）
    gw_obj._providers = {
        "a": _FakeProvider("a", paid=True, local=False),
        "b": _FakeProvider("b", paid=False, local=False),
        "c": _FakeProvider("c", paid=True, local=True),
    }
    assert gw_obj._paid_provider_names() == ["a", "c"]
