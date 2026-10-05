"""`X:all` 截面：**能力面不许超出登记面**（2026-10-01，把一条取舍变成判据）。

## 结论先说：这不是缺陷，是一条**刻意的成本决策**

`ind:sw_*:all` 是**全市场截面**聚合请求。`sw_industry_valuation_connector`
的 `get_capabilities()` 里写着（@86-89）：

> ⚠️ 因此这里**故意不声明** 7 个 `:all` 截面
> （`ind:sw_{first,second}_pb:all`、`ind:sw_{first,second,third}_pe_static:all`、
> `ind:sw_{first,second}_dividend_yield:all`）—— 连接器**确实支持**，
> 但**没登记**；声明了它就会进"能力面 vs 登记面"的比对并被周期采集。

也就是说"7 个未声明的 `:all` 段"的真相是：**声明 = 承诺周期采集 = 花钱**。
所以正确处置不是"补齐 7 条"，而是：

1. **判据**：声明出来的每一条都必须在 `configs/indicators.yaml` 里登记过
   （否则会被周期采集却算不出新鲜度 ⇒ 每次判 stale ⇒ **每次联网**，`registry.py` @61-72）；
2. **决策留痕**：刻意不声明的那几条必须**在源码里写明白**（否则下一个人会"顺手补上"，
   悄悄开始花钱）。

## 自证

第 3 条判据断言"抽出来的集合非空且都含 `:all`" ——
如果抽取逻辑哪天失效（返回空集），前两条会变成**恒绿**，这条会先红。
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
CONNECTOR = ROOT / "src/infrastructure/connectors/sw_industry_valuation_connector.py"
YAML = ROOT / "configs/indicators.yaml"


def _registered_ids() -> set[str]:
    text = YAML.read_text(encoding="utf-8")
    return {m.group(1).strip() for m in
            re.finditer(r"^\s*-\s*id:\s*(\S+)", text, re.M)}


def _declared_all_forms() -> list[str]:
    """AST 抽出 `get_capabilities()` 里 `all_forms` 那个元组字面量。

    用 AST 而不是 import：连接器类需要实例化才拿得到 capabilities，
    而这里要钉的正是"**源码里声明了什么**"（与实例状态无关）。
    """
    tree = ast.parse(CONNECTOR.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "all_forms" for t in node.targets):
            if isinstance(node.value, ast.Tuple):
                return [str(ast.literal_eval(e)) for e in node.value.elts]
    raise AssertionError("在 sw_industry_valuation_connector.py 里找不到 all_forms 元组")


def test_extraction_is_not_vacuous() -> None:
    """★ 自证：抽出来的集合必须非空、且**确实都是 `:all` 截面**。

    没有这一条，抽取逻辑失效（返回空集）会让下面两条**恒绿**。
    """
    forms = _declared_all_forms()
    assert len(forms) >= 5, f"只抽到 {len(forms)} 条，抽取逻辑可能已失效"
    assert all(":all" in f for f in forms), f"抽到的不是 `:all` 截面：{forms}"


def test_declared_all_forms_are_registered() -> None:
    """★ 能力面 ⊆ 登记面：声明了却没登记 ⇒ 索引 row_count 恒 0 ⇒ **每次联网**。

    这条是不对称的：**登记了不声明**是允许的（登记面是"我们打算采"的超集），
    但**声明了没登记**一定出问题 —— 因为 `:all` 是聚合请求，名字本身
    永远不会写进事实表（`registry.py` @61-72）。
    """
    reg = _registered_ids()
    missing = [f for f in _declared_all_forms() if f not in reg]
    assert not missing, (
        "这些 `:all` 截面被声明为能力、却没在 configs/indicators.yaml 登记："
        f"{missing} —— 它们会被判 stale 并**每次联网**")


def test_deliberately_undeclared_sections_are_documented() -> None:
    """★ 刻意不声明的取舍必须**写在源码里**（否则会被"顺手补上"、悄悄开始花钱）。

    判据形式：模块 docstring/capabilities 注释里必须同时出现
    ①「故意不声明」的表述 ②被排除的那几个前缀（`pe_static` / `dividend_yield` / `_pb:all`）。
    """
    src = CONNECTOR.read_text(encoding="utf-8")
    assert "故意不声明" in src or "故意不声明" in src.replace("**", ""), (
        "连接器里找不到『故意不声明』的说明 —— 这条取舍会随时间消失")
    for needle in ("pe_static", "dividend_yield", "_pb:all"):
        assert needle in src, (
            f"被刻意排除的截面 `{needle}` 没有在源码里点名 —— "
            "下一个人会以为它们是漏登记的，然后顺手补上（= 开始周期采集 = 花钱）")
