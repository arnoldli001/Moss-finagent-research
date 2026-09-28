"""免费 provider 候选的接入护栏 —— 钉住「**未实测的模型不进路由**」。

## 用户裁定（2026-09-28）

> 「接免费 provider（B+C，目标高频层）」

## 为什么这么接

- **目标放高频层**：`light`（清洗/格式化）与 `medium`（信息层 A05/A06）
  是每次请求都走的层；而 A08/A11 单次成本仅约 ¥0.02。省高频层的钱收益是
  分析层的几十倍。
- **只接代码，不动 routing**：本项目铁律 —— 未实测的模型不进路由。
  `glm-4.7-flash` 是活证据：官方定价页写"免费"，实测 **429×3 次**
  + 一次 **74.2 秒**。

## 本文件钉住三条

1. 候选模型**已登记**（能建 provider、能被探针按名字取到）
2. 候选模型**不在任何 routing 链里**（防"顺手接上"）
3. 每个新 provider 的注册都是**条件式**（缺 key 不注册）—— 否则误配后
   会静默失败，而"未注册"会被误读成"这个模型不可用"
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: 已完成实测、**允许进 routing** 的候选（含实测证据）。
#:
#: ⚠️ 语义变更（2026-09-28 第十九轮）：原判据是"候选一律不许进 routing"，
#: 那是在**尚未实测**时的临时闸门。现在它们已实测，闸门该换成
#: **"进 routing 的免费模型必须有实测记录"** —— 否则这条护栏会一直红，
#: 而"一直红的护栏"正是本项目登记过的最贵失败模式（会被当背景噪音无视）。
#:
#: 实测证据（`scripts/probe_free_providers.py` + `probe_analysis_models.py`）：
#:   qwen-dashscope-flash  轻任务 p50 448ms · 并发10 n=40 **0/40 限流**
#:                         A08 2848ms/116字 · A11 2470ms/135字
#:   qwen-siliconflow-7b   轻任务 p50 578ms · 并发10 n=40 **0/40 限流**
#:                         A08 4993ms/ 86字 · A11 2978ms/ 69字
MEASURED_FREE_MODELS: dict[str, str] = {
    "qwen-dashscope-flash": "并发10 n=40 零限流；A08/A11 质量已比（约 deepseek 的 70%）",
    "qwen-siliconflow-7b": "并发10 n=40 零限流；A08/A11 质量已比（约 deepseek 的 35%）",
}

#: **未实测**的候选：一律不许进 routing。
UNMEASURED_CANDIDATES = ("glm-4.7-flash",)


def _cfg():
    from src.infrastructure.llm.gateway import _load_model_config

    return _load_model_config("configs/models.yaml")


def test_candidates_are_registered():
    """候选必须已登记，否则探针按名字取不到、也就无法实测。"""
    specs, _chains, _lo = _cfg()
    missing = [c for c in (*MEASURED_FREE_MODELS, *UNMEASURED_CANDIDATES)
               if c not in specs]
    assert not missing, f"这些候选没登记进 models.yaml：{missing}"


def test_unmeasured_models_are_not_in_any_routing_chain():
    """★ 核心：**未实测**的模型不许进任何 routing 链。

    `glm-4.7-flash` 是这条判据的活证据：官方定价页写"免费"、支持结构化输出，
    看着完美 —— 实测 **429×3 次 + 一次 74.2 秒**。
    """
    _specs, chains, _lo = _cfg()
    bad = {tier: [m for m in chain if m in UNMEASURED_CANDIDATES]
           for tier, chain in chains.items()}
    bad = {k: v for k, v in bad.items() if v}
    assert not bad, (
        f"未实测的模型被接进了路由：{bad} —— "
        "先跑 scripts/probe_free_providers.py 拿到准入数据再改")


def test_routed_free_models_all_have_measurement_records():
    """★ 反向判据：进了 routing 的免费档，必须在 `MEASURED_FREE_MODELS` 里有记录。

    防的是"接了个新免费档但没实测就上线" —— 那条路径本会话已经走过一次
    （glm 的 429×3）。**判据从"白名单"改成"必须有实测记录"**，
    这样它不会因为"实测完了"而永远红着（永远红的护栏 = 背景噪音）。
    """
    _specs, chains, _lo = _cfg()
    routed_free = set()
    for chain in chains.values():
        for m in chain:
            if m.startswith("qwen-"):
                routed_free.add(m)
    undocumented = routed_free - set(MEASURED_FREE_MODELS)
    assert not undocumented, (
        f"这些免费档进了 routing 但没有实测记录：{undocumented} —— "
        "补实测并登记进 MEASURED_FREE_MODELS（含依据数字）")


def test_candidates_declare_zero_cost():
    """候选都声称免费 —— 若哪天要收费，这条会红，提醒同步改文档与归因。"""
    import yaml

    raw = yaml.safe_load((ROOT / "configs" / "models.yaml").read_text(
        encoding="utf-8"))
    for c in (*MEASURED_FREE_MODELS, *UNMEASURED_CANDIDATES):
        cost = (raw["models"].get(c, {}).get("cost") or {})
        assert cost.get("output", 1) == 0, (
            f"{c} 的 output 单价不是 0（{cost.get('output')}）—— "
            "若已收费，必须同步改这里的断言 + docs 的调研报告")


def test_new_providers_are_conditionally_registered():
    """★ 新 provider 必须**缺 key 就不注册**（照 zhipu 的既有写法）。

    为什么：注册了但没 key，调用时才报错 → 走降级链，能工作；
    但"未注册"只会得到 `提供商未注册` 并被跳过，让**"忘了配 key"看起来像
    "这个模型不可用"**，排查方向完全错（`build_providers` 的 docstring 原文）。
    """
    src = (ROOT / "src" / "infrastructure" / "llm" / "providers.py"
           ).read_text(encoding="utf-8")
    for provider in ("dashscope", "siliconflow"):
        assert f'providers["{provider}"]' in src, f"{provider} 没注册"
        # 注册语句必须包在"key 非空"的判断里
        idx = src.index(f'providers["{provider}"]')
        window = src[max(0, idx - 400):idx]
        assert "api_key" in window and "strip()" in window, (
            f"{provider} 的注册没有做「缺 key 不注册」判断 —— "
            "会把'忘了配 key'伪装成'模型不可用'")


def test_providers_module_imports_cleanly():
    """接线不能引入导入错误（这是最便宜的一道门）。"""
    import importlib

    m = importlib.import_module("src.infrastructure.llm.providers")
    provs = m.build_providers()
    # 没配 key 时只有既有的三个；配了才多出来
    assert "deepseek" in provs and "ollama" in provs
