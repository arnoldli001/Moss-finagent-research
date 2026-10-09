"""`ask_agent` 工具描述与执行逻辑的**契约一致**（判据）。

## 这条判据防的是什么（真实缺陷）

工具描述**会进 LLM 的 system prompt**（`react.py` 的 `describe_all()`），
所以描述里的数字是**运行时契约**，不是文档修辞。历史上：

| 位置 | 值 |
|---|---|
| `recommend_node` 里的执行逻辑常量 | `MAX_ASKS_PER_AGENT = 1` |
| 手写在 `tools.register(..., "…最多追问2次…")` 的描述 | `2` |

后果：模型按描述去试第 2 次 → 被 `_ask_agent` 拒绝 → 它不知道为什么，
于是**换个说法继续试同一件事**（追问是真金白银的 LLM 调用）。
同一件事两个数，且没有任何报错 —— 典型的静默漂移。

修法：描述抽成模块级工厂 `ask_agent_tool_description()`，数字由常量插值。
本文件的判据**直接调那个工厂**（行为判据，不是读源码的形状判据）。
"""

from __future__ import annotations

import re

from src.orchestration.supervisor import (
    MAX_ASKS_PER_AGENT,
    ask_agent_tool_description,
)

AGENTS = ["A08_macro", "A09_meso", "A17_recommend"]


def test_description_uses_the_constant_not_a_handwritten_number():
    """★ 描述里的"最多追问 N 次"必须 == 常量值。

    把工厂里的 `{MAX_ASKS_PER_AGENT}` 改回手写数字（比如 2）⇒ 立刻红。
    """
    desc = ask_agent_tool_description(AGENTS)
    found = re.search(r"最多追问(\d+)次", desc)
    assert found, f"描述里找不到追问上限（形状变了？）：{desc!r}"
    assert int(found.group(1)) == MAX_ASKS_PER_AGENT, (
        f"描述说 {found.group(1)} 次，代码常量是 {MAX_ASKS_PER_AGENT} 次 —— "
        "同一件事两个数（LLM 会按描述去试，然后被拒且不知原因）"
    )


def test_description_does_not_contain_any_other_stray_hint_number():
    """描述里不许再出现第二个"次数"口径（防止有人补一句"可追问 2 轮"）。"""
    desc = ask_agent_tool_description(AGENTS)
    numbers = {int(n) for n in re.findall(r"追问(\d+)", desc)}
    assert numbers == {MAX_ASKS_PER_AGENT}, f"追问口径不止一处：{numbers}"


def test_description_list_depends_on_the_argument():
    """可用 Agent 清单必须由**入参**决定（行为判据：换入参就换清单）。

    这比"扫描文案里有没有某个 agent_id"强 —— 它抓的是"清单是不是真的从参数来"。
    """
    a = ask_agent_tool_description(["A08_macro"])
    b = ask_agent_tool_description(["A09_meso", "A10_micro"])
    assert "A08_macro" in a and "A09_meso" not in a
    assert "A09_meso" in b and "A10_micro" in b and "A08_macro" not in b


def test_description_keeps_data_layer_carve_out():
    """数据层不可提问这条护栏必须在描述里（否则模型会去问 A01-A04）。"""
    desc = ask_agent_tool_description(AGENTS)
    assert "A01-A04" in desc and "query_data" in desc, (
        "描述丢了「数据层不可提问、指标数值用 query_data」这条分流规则"
    )


def test_production_register_call_passes_the_factory():
    """★ 钉住生产调用点：`tools.register("ask_agent", …)` 的第三个实参必须是
    `ask_agent_tool_description(...)` 调用，**不能是字符串字面量**。

    ## 为什么用 AST 而不是字符串匹配

    第一版用 `"最多追问2次" not in src` —— 结果被**修复注释本身**触发
    （注释里引用了历史缺陷原文）。字符串判据分不清"代码"与"注释"，
    这类假红会逼着人把注释删掉，是本仓库明令要避免的"判据形态错位"。
    AST 天然只解析代码，注释不参与。
    """
    import ast
    import inspect

    from src.orchestration import supervisor as sup

    tree = ast.parse(inspect.getsource(sup))
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # 形如 tools.register("ask_agent", _ask_agent, <desc>)
        if not (isinstance(func, ast.Attribute) and func.attr == "register"):
            continue
        if len(node.args) < 3:
            continue
        first = node.args[0]
        if not (isinstance(first, ast.Constant) and first.value == "ask_agent"):
            continue
        hits.append(node.args[2])

    assert hits, "找不到 tools.register(\"ask_agent\", …) 的注册点（形态变了？）"
    for desc_arg in hits:
        assert isinstance(desc_arg, ast.Call), (
            "ask_agent 的描述是字面量字符串 ⇒ 又可与常量漂移"
        )
        assert isinstance(desc_arg.func, ast.Name)
        assert desc_arg.func.id == "ask_agent_tool_description", (
            f"描述由 {desc_arg.func.id} 生成，不是唯一构造点"
        )
