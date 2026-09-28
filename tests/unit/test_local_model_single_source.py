"""本地模型的名字**只有一个真值源**：`configs/models.yaml`。

## 为什么需要这条护栏（2026-09-28 清理 `LOCAL_MODEL` 的现场）

`src/domain/intel/llm_policy.py` 曾经定义：

    LOCAL_MODEL: Final = "qwen3:8b"
    LOCAL_MODEL_SMALL: Final = "qwen2.5:1.5b"

三件事叠在一起，让它变成了**过期路标**：

1. **生产代码零引用** —— 全仓库只有"测试函数名里带 LOCAL_MODEL"和文档提过它，
   真正的调用点走的是 `tone_job.EXTRACT_TIER` → 网关 → `configs/models.yaml`。
2. **值本身就过期** —— 配置里真实登记的是 `qwen3:8b-q4_K_M` /
   `qwen2.5:1.5b-instruct-q4_K_M`，而这两个常量写的是不带量化后缀的短名。
3. **本地地板换过代** —— 2026-09-28 从 `qwen3:8b-q4_K_M` 换成 `qwen3.5:4b`，
   常量却还指着 8B。下一个人照着它排障，会去找一个**不在路由链上**的模型。

## 本文件钉住三条

| # | 判据 | 防的是什么 |
|---|---|---|
| 1 | `llm_policy` **不许**再定义模型名常量 | 过期路标长回来 |
| 2 | 配置里必须存在 `local_light` / `local_medium` 两个本地模型键 | 键被改名后"本地模型是谁"查不到 |
| 3 | 本地模型名只能来自配置 | 别处再抄一份（复制必然漂移） |
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _config():
    from src.infrastructure.llm.gateway import _load_model_config

    return _load_model_config("configs/models.yaml")


def test_policy_module_defines_no_model_name() -> None:
    """★ 判据 1：`llm_policy` 不许再出现模型名常量（过期路标不许长回来）。

    ⚠️ 用**语法树**判"有没有定义字符串常量"，不用正则 ——
    注释与文档字符串里提到 `LOCAL_MODEL` 是**历史记录**（本条 docstring 自己
    就在提它），拿正则判会把说明文字当成违规（AGENTS.md：判据要适应文档，
    不要让文档迁就判据）。
    """
    import ast

    src = (ROOT / "src" / "domain" / "intel" / "llm_policy.py").read_text(
        encoding="utf-8")
    tree = ast.parse(src)

    offenders: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        name = node.target.id
        value = node.value
        # 只看"模块级的字符串常量"，且名字像模型名
        if (isinstance(value, ast.Constant) and isinstance(value.value, str)
                and ("MODEL" in name.upper() or "LLM" in name.upper())):
            offenders.append(f"{name} = {value.value!r}")
    assert not offenders, (
        "llm_policy 又定义了模型名常量 —— 本地模型的名字只允许写在 "
        f"`configs/models.yaml`：{offenders}")


def test_local_model_keys_exist_in_config() -> None:
    """★ 判据 2：配置里必须能查到"本地模型是谁"（键在、且 provider 是本机）。"""
    specs, chains, _lo = _config()
    for key in ("local_light", "local_medium"):
        assert key in specs, f"configs/models.yaml 里没有 {key} —— 本地模型查不到了"
        assert specs[key].provider == "ollama", (
            f"{key} 的 provider 是 {specs[key].provider}，不再是本机 —— "
            "本地兜底的地板没了")
        assert specs[key].model_name, f"{key} 没写 model_name"


def test_every_local_hop_in_routing_comes_from_config() -> None:
    """★ 判据 3：路由链上出现的每个本地模型，都必须是配置里登记过的键。

    防的是"某个链上写了个配置里不存在的键" —— 那种情况下网关会抛
    `ConfigError: 模型未在configs/models.yaml定义`，而它**只在被调用的那一刻**
    才暴露（平时测试与启动都不报）。这里提前把它钉住。
    """
    from src.infrastructure.llm.gateway import LOCAL_PROVIDERS

    specs, chains, _lo = _config()
    missing: list[str] = []
    for tier, chain in chains.items():
        for model_key in chain:
            if model_key not in specs:
                missing.append(f"{tier} → {model_key}")
    assert not missing, f"路由链里引用了配置中不存在的模型键：{missing}"

    # 本地跳必须真的是本机 provider（防止把云端写进"本地地板"的位置）
    wrong: list[str] = []
    for tier, chain in chains.items():
        for model_key in chain:
            spec = specs.get(model_key)
            if spec and spec.provider in LOCAL_PROVIDERS and not spec.base_url:
                wrong.append(f"{tier} → {model_key}（本地但没有 base_url）")
    assert not wrong, wrong


def test_removed_constants_are_really_gone() -> None:
    """自证：删掉的东西**真的没了**（不是只从 `__all__` 里摘掉）。"""
    from src.domain.intel import llm_policy

    for name in ("LOCAL_MODEL", "LOCAL_MODEL_SMALL"):
        assert not hasattr(llm_policy, name), (
            f"{name} 还在模块上 —— 删除没生效（可能只摘了 __all__）")
    assert "LOCAL_MODEL" not in getattr(llm_policy, "__all__", [])
    assert "LOCAL_MODEL_SMALL" not in getattr(llm_policy, "__all__", [])


def test_guard_detects_a_reintroduced_constant(tmp_path, monkeypatch) -> None:
    """★ 自证：判据 1 必须能报出"模型名常量被加回来"，不能恒绿。"""
    import ast

    bad_src = (
        "from typing import Final\n"
        "CLOUD_CALLS_PER_DAY: Final = 20\n"
        "LOCAL_MODEL: Final = 'qwen3:8b'\n"
    )
    tree = ast.parse(bad_src)
    found = [n.target.id for n in tree.body
             if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
             and isinstance(n.value, ast.Constant)
             and isinstance(n.value.value, str)
             and ("MODEL" in n.target.id.upper() or "LLM" in n.target.id.upper())]
    assert found == ["LOCAL_MODEL"], (
        "判据 1 认不出被加回来的模型名常量 —— 它是假绿")
