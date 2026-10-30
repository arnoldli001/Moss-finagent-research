"""`MainlineDataStore._write()` 的**入参形状**守卫（AST 静态扫描）。

## 为什么需要它（2026-10-30 的事故）

`_write(sql, rows)` 内部走的是 `connection.executemany(sql, rows)` —— 也就是
"外层序列 = 一批记录，内层每项 = 一条记录"。所以第二个参数**必须**是
"一行记录的列表"（`[(...)]` / 列表推导），**不能**是裸元组 `(...)`。

裸元组的后果不是"参数不对"这么温和，而是**完全看不出根因的报错**：

    主线数据同步 seat 失败：Incorrect number of bindings supplied.
    The current statement uses 3, and there are 8 supplied.

`8` 是日期字符串 `"20261030"` 的长度 —— `executemany` 把**字符串的第一个元素**
当成了"一条记录"去逐字符绑定。第一次见到这个报错时，几乎不可能联想到
"少写了一对方括号"。

## 它实际造成的损失（必须写下来，否则还会重演）

2026-10-30 用户问"定期自动刷新的脚本是否按预期执行了"，查出来：
`sync_seats` 的空分支用了裸元组 → 只要某天没有龙虎榜记录就抛异常 →
该数据集记 `failed` → `mainline_daily` 连续三次 `failed`（10-08/10-09/10-12）
→ `RunLog.is_paused()` 判真 ⇒ **调度器连续 18 天静默跳过它**，主线评分停在
`20261009`、ETF 份额停在 `20261023`。

⇒ 一个"少一对方括号"的笔误，代价是**三周的数据停更 + 用户来问**。

## 为什么用 AST 而不是跑一遍

跑一遍只能覆盖到"此刻能走到的分支"（空龙虎榜要恰好赶上没数据的交易日）。
AST 扫**所有** `self._write(...)` 调用点，把这一类形状一次性钉住 ——
新写一个 `_write` 时用裸元组，测试立刻红，不用等它触发。
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DATASTORE = REPO / "src" / "mainline" / "datastore.py"


def _write_calls() -> list[tuple[int, ast.expr]]:
    """所有 `self._write(...)` 调用 → (行号, 第二个实参节点)。"""
    tree = ast.parse(DATASTORE.read_text(encoding="utf-8"))
    out: list[tuple[int, ast.expr]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "_write":
            continue
        assert len(node.args) >= 2, (
            f"datastore.py:{node.lineno} `_write()` 的第二个参数（行数据）不见了")
        out.append((node.lineno, node.args[1]))
    return out


def test_write_calls_never_pass_a_bare_tuple() -> None:
    """`_write()` 的第二个参数不许是裸元组（`executemany` 会把它拆错）。"""
    bad = [(line, type(arg).__name__) for line, arg in _write_calls()
           if isinstance(arg, ast.Tuple)]
    assert not bad, (
        "以下 `_write()` 调用把**裸元组**当成了行数据："
        + ", ".join(f"第 {line} 行（{kind}）" for line, kind in bad)
        + "\n  ⇒ 改成 `[(...)]`（一行记录的列表）。`_write` 走的是 "
          "`executemany`，裸元组会被拆成多条记录，报"
          "「uses N, and there are M supplied」这种看不出根因的错。")


def test_write_calls_are_found_at_all() -> None:
    """扫描器自己要能扫到东西 —— 否则"没扫到"会被误读成"没有坏调用"。"""
    calls = _write_calls()
    assert len(calls) >= 10, (
        f"只扫到 {len(calls)} 处 `self._write(` 调用，明显不对劲："
        "扫描器坏了会让上一条守卫变成永远绿")


def test_the_seat_empty_branch_is_a_list() -> None:
    """定点守 2026-10-30 那次事故的具体一行（防止有人"顺手改回去"）。

    不只看形状：要求 `sync_seats` 的空分支里那个 `_write` 的第二个参数是
    `ast.List`。这条与上面的通用守卫**故意重复** —— 通用守卫可能被"改成
    `list((...))` 之类的写法"绕过，而这一条盯的是事故现场。
    """
    src = DATASTORE.read_text(encoding="utf-8")
    tree = ast.parse(src)
    seat = next((node for node in ast.walk(tree)
                 if isinstance(node, ast.FunctionDef) and node.name == "sync_seats"),
                None)
    assert seat is not None, "找不到 `sync_seats()`"
    hits = [node for node in ast.walk(seat)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute) and node.func.attr == "_write"]
    assert hits, "`sync_seats()` 里没有 `_write` 调用（结构变了？）"
    assert any(isinstance(call.args[1], ast.List) for call in hits), (
        "`sync_seats()` 的空分支没把行数据写成 `[(...)]`")
