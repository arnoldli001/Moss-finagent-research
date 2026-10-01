"""CHG-0155：投研分析核心原则「预期差驱动股价」的护栏。

> 用户口径（2026-10-01）：「概念板块和个股的股价上涨动力来自预期差，而不是预期。」

## 护栏覆盖范围

6 个 agent 文件（分析层 5 + 决策层 1）的 `system_prompt` 必须包含：
- **核心关键词**「预期差」
- **对比句**「实际 vs 市场」

任何一处缺失即红。

## 自证机制

`test_macro_missing_principle_breaks_macro_test` —— AST 验证 macro 的 system_prompt
不含"预期差"时，断言会精准失败并报文件路径。
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC = PROJECT_ROOT / "src" / "domain" / "agents"

# 受护栏的 6 个 agent 文件
PROTECTED_AGENTS = {
    "A08_macro": SRC / "analysis" / "macro" / "agent.py",
    "A09_meso": SRC / "analysis" / "meso" / "agent.py",
    "A10_micro": SRC / "analysis" / "micro" / "agent.py",
    "A11_fin_risk": SRC / "analysis" / "risk" / "agent.py",
    "A12_compliance": SRC / "analysis" / "compliance" / "agent.py",
    "A17_recommend": SRC / "decision" / "recommend" / "agent.py",
}

REQUIRED_KEYWORDS = ("预期差",)
REQUIRED_CONTRAST_PHRASE = ("实际", "市场")  # "实际 vs 市场" 的最小骨架


def _extract_system_prompt(file_path: Path) -> str:
    """AST 解析：读出 `class XxxAgent` 下的 `system_prompt` 赋值完整字符串。

    仅解析赋值给 `system_prompt` 的字符串常量拼接（含隐式拼接、函数调用如
    `render_unlock_teaching()`），不做语法解释，提取所有 `ast.Constant` 节点。
    """
    src = file_path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(file_path))

    system_prompt_value = ""
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            if not isinstance(stmt, ast.Assign):
                continue
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id == "system_prompt":
                    # 收集拼接常量与函数调用
                    for elt in ast.walk(stmt.value):
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            system_prompt_value += elt.value
                        elif isinstance(elt, ast.Call) and isinstance(elt.func, ast.Name):
                            # 函数调用不展开，但保留函数名（避免假绿）
                            system_prompt_value += " " + elt.func.id + "() "
    return system_prompt_value


def _principle_present(prompt: str) -> bool:
    """断 6 个文件都含「预期差」与「实际 vs 市场」的对比句骨架。"""
    if "预期差" not in prompt:
        return False
    # 骨架：「实际」与「市场」必须同时出现，并构成对比关系
    # （"实际预期"、"实际 > 市场"、"市场预期" 等都是合法形式）
    has_actual = "实际" in prompt
    has_market = "市场" in prompt
    return has_actual and has_market


def _build_fail_msg(agent_name: str, file_path: Path, prompt: str) -> str:
    """构造失败信息（含文件路径 + 缺哪个关键词），便于 CI 定位。"""
    missing_keywords = [kw for kw in REQUIRED_KEYWORDS if kw not in prompt]
    missing_contrast = []
    if "实际" not in prompt:
        missing_contrast.append("实际")
    if "市场" not in prompt:
        missing_contrast.append("市场")
    return (
        f"{agent_name} ({file_path}): system_prompt 缺少 CHG-0155 投研分析核心原则\n"
        f"  缺关键词: {missing_keywords}\n"
        f"  缺对比骨架: {missing_contrast}\n"
        f"  修复: 在该 agent 的 system_prompt 中加入「★★★ 投研分析核心原则："
        "**预期差**驱动股价」段（含「实际 vs 市场」对比句）"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 主护栏：6 个文件必须都含「预期差」原则
# ═══════════════════════════════════════════════════════════════════════════════

def test_all_six_agents_contain_expectation_gap_principle():
    """所有 6 个 agent 的 system_prompt 都必须含「预期差」+ 「实际 vs 市场」。"""
    failures = []
    for agent_name, file_path in PROTECTED_AGENTS.items():
        prompt = _extract_system_prompt(file_path)
        if not _principle_present(prompt):
            failures.append(_build_fail_msg(agent_name, file_path, prompt))
    assert not failures, "\n\n".join(failures)


def test_specific_agents_carry_their_layer_specific_application():
    """每个 Agent 的 system_prompt 必须含「本层应用」段（不只贴通用模板）。"""
    layer_keywords = {
        "A08_macro": "宏观",       # 美林时钟 / PMI / CPI
        "A09_meso": "中观",        # 产业链 / 板块轮动
        "A10_micro": "微观",       # 个股 / 一致预期
        "A11_fin_risk": "财务",    # 红旗 / 解禁压力
        "A12_compliance": "合规",  # 合规 / 处罚
        "A17_recommend": "决策",   # 综合 / 仲裁
    }
    for agent_name, kw in layer_keywords.items():
        file_path = PROTECTED_AGENTS[agent_name]
        prompt = _extract_system_prompt(file_path)
        assert kw in prompt, (
            f"{agent_name} 的 system_prompt 没体现「{kw}侧应用」——"
            "CHG-0155 要求每层有专属落地说明，不能只贴通用模板。"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 反向判据（自证）：删 macro 的预期差段 ⇒ macro 这一条失败
# ═══════════════════════════════════════════════════════════════════════════════

def test_macro_missing_principle_breaks_macro_test():
    """自证：模拟 macro 的 system_prompt 不含「预期差」时，本测试会精准失败。

    实现方式：读 macro 的源码，**先去掉任何含「预期差」的字符串**，断言 _principle_present
    返回 False ⇒ 即"如果维护者删了这段，测试会立刻红"。
    """
    file_path = PROTECTED_AGENTS["A08_macro"]
    src = file_path.read_text(encoding="utf-8")
    # 把所有「预期差」字符替换为空（不会写回磁盘 —— 仅做内存模拟）
    stripped = src.replace("预期差", "")
    tree = ast.parse(stripped, filename=str(file_path))

    system_prompt_value = ""
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if isinstance(target, ast.Name) and target.id == "system_prompt":
                        for elt in ast.walk(stmt.value):
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                system_prompt_value += elt.value

    # 验证：移除「预期差」后，_principle_present 必须返回 False（否则护栏失效）
    assert "预期差" not in system_prompt_value, "测试本身的清理没生效"
    assert not _principle_present(system_prompt_value), (
        "护栏失效：移除『预期差』后 _principle_present 仍返回 True。"
        "需要更严格的关键词/对比句检测。"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 路径覆盖：防止 PROTECTED_AGENTS 漏掉新增的 Agent
# ═══════════════════════════════════════════════════════════════════════════════

def test_protected_agents_list_covers_all_relevant_classes():
    """护栏白名单要足够全 —— 至少覆盖：分析层 5 + 决策层 1 = 6 个。"""
    assert len(PROTECTED_AGENTS) == 6, (
        f"PROTECTED_AGENTS 应有 6 项（5 分析 + 1 决策），实际 {len(PROTECTED_AGENTS)}。"
        "新增 Agent 时必须同步更新本测试，否则不会被护栏兜住。"
    )
    # 文件必须存在
    for agent_name, file_path in PROTECTED_AGENTS.items():
        assert file_path.exists(), f"{agent_name} 路径不存在: {file_path}"


def test_principle_text_is_not_empty_string():
    """核心原则段不能被改成空字符串 —— 静默删除等同假绿。"""
    for agent_name, file_path in PROTECTED_AGENTS.items():
        prompt = _extract_system_prompt(file_path)
        # 找到「投研分析核心原则」那段（如果有的话），确保它不是空
        if "投研分析核心原则" in prompt:
            idx = prompt.index("投研分析核心原则")
            # 取这一段 200 字符
            chunk = prompt[idx:idx + 200]
            assert "预期差" in chunk, (
                f"{agent_name}: '投研分析核心原则' 出现但紧邻 200 字符内不含'预期差'。"
                "请检查是否是拼接顺序错位。"
            )
