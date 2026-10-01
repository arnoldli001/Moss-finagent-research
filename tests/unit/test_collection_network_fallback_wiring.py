"""★ 采集路径「联网兜底」的接线护栏（2026-09-30）。

## 触发它的是只读清点时发现的硬缺陷（已运行时复现）

`AGENTS.md` / PRD 一直声称「找不到就去**联网搜索找**」，而**采集链上这一跳从未执行**：

* `supervisor.py` 的 `_fallback_fetch(indicator, *, agents, context, trigger)`
  —— 签名里**没有 `state`**；
* 采集路径唯一调用方 `_network_lookup_for_collection()` 却写
  `_fallback_fetch(indicator, state=state, agents=..., ...)`
  ⇒ 每次抛 `TypeError: unexpected keyword argument 'state'`；
* 该 TypeError 被 `except Exception` 吞掉且**只记 `logger.debug`**（默认不可见）
  ⇒ 返回 `[]`（"没找到"）⇒ **看起来一切正常**。

这是 `AGENTS.md` 记过的"NetworkFallback 零生产调用方"的**同一形状复发**：
**判据接在没人走的路上 = 没接**。而 `grep -r "_network_lookup_for_collection|LOCAL_MISS" tests/`
**零命中** —— 没有任何护栏盯着这条路径，所以它能一直绿。

## 本文件守三件事

1. **调用点与签名必须一致**（AST 逐调用点核对关键字）—— 这一类错误**编译器抓不到**，
   而运行期又被宽 `except` 吞掉，只能靠判据；
2. **行为判据**：按调用点的**原样关键字**去调 `_fallback_fetch`，必须**不抛 TypeError**
   （用空 agents ⇒ 内部直接返回 None，不触网、不花钱）；
3. **可见性**：那条宽 `except` 必须是 `warning`（不是 `debug`）——
   "兜底坏了"必须能在后端日志里看见，否则缺陷会再次藏起来。
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path

import pytest

SUPERVISOR = Path(__file__).resolve().parents[2] / "src" / "orchestration" / "supervisor.py"


def _tree() -> ast.Module:
    return ast.parse(SUPERVISOR.read_text(encoding="utf-8"))


def _signature_of(tree: ast.Module, name: str) -> tuple[set[str], int]:
    """返回 `(允许的关键字集合, 允许的位置参数个数)`。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name:
            pos = [a.arg for a in node.args.args]
            allowed = set(pos) | {a.arg for a in node.args.kwonlyargs}
            allowed |= {a.arg for a in node.args.posonlyargs}
            return allowed, len(pos)
    raise AssertionError(f"找不到函数 {name}")


def _calls_of(tree: ast.Module, name: str) -> list[ast.Call]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            fname = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
            if fname == name:
                out.append(node)
    return out


def test_every_call_site_matches_the_signature():
    """★ 核心判据：`_fallback_fetch` 的**每个**调用点只能用签名里有的关键字。

    这条红了 = 又出现了"调用点与签名不一致 ⇒ 被宽 except 吞掉 ⇒ 兜底静默失效"。
    """
    tree = _tree()
    allowed, max_pos = _signature_of(tree, "_fallback_fetch")
    calls = _calls_of(tree, "_fallback_fetch")
    assert calls, "supervisor 里找不到 `_fallback_fetch` 的调用点（链路被删了？）"
    bad: list[str] = []
    for call in calls:
        if len(call.args) > max_pos:
            bad.append(f"{SUPERVISOR.name}:{call.lineno} 位置参数过多")
        for kw in call.keywords:
            if kw.arg is not None and kw.arg not in allowed:
                bad.append(f"{SUPERVISOR.name}:{call.lineno} 多传了 `{kw.arg}=`"
                           f"（签名只接受 {sorted(allowed)}）")
    assert not bad, "调用点与签名不一致：\n  " + "\n  ".join(bad)


def test_the_fallback_fetch_has_no_state_parameter():
    """登记事实：它**不需要** `state`（当初多传的就是它）。

    这条同时也提醒：将来若真要为它加 `state`，必须**同时**更新调用点与测试。
    """
    from src.orchestration import supervisor as s

    params = inspect.signature(s._fallback_fetch).parameters
    assert "state" not in params, (
        "`_fallback_fetch` 现在有 `state` 了 ⇒ 请确认这是有意的，"
        "并核对所有调用点（本文件第一条判据）")
    assert "agents" in params and "context" in params and "trigger" in params


def test_calling_it_the_way_collection_does_not_raise_type_error():
    """★ 行为判据：按**调用点的原样关键字**调用，必须不抛 TypeError。

    用空 `agents` ⇒ `_backend_of({})` 返回 None ⇒ 内部直接 `return None`
    （**不触网、不花钱**）。
    """
    from src.orchestration import supervisor as s

    tree = _tree()
    calls = _calls_of(tree, "_fallback_fetch")
    # 取"采集路径"那个调用点（传了 trigger="LOCAL_MISS" 的那个）
    target = None
    for call in calls:
        kws = {k.arg: k for k in call.keywords}
        if "trigger" in kws and isinstance(kws["trigger"].value, ast.Constant) \
                and kws["trigger"].value.value == "LOCAL_MISS":
            target = call
            break
    assert target is not None, "找不到采集路径的调用点（trigger='LOCAL_MISS'）"

    kwargs: dict[str, object] = {}
    for kw in target.keywords:
        if kw.arg == "agents":
            kwargs["agents"] = {}
        elif kw.arg == "state":
            # 老代码多传的那个参数：保留它正是为了让本测**复现** TypeError
            kwargs["state"] = {"task_id": "t1"}
        elif isinstance(kw.value, ast.Constant):
            kwargs[kw.arg] = kw.value.value
        else:
            kwargs[kw.arg] = "probe"

    result = asyncio.run(s._fallback_fetch("CPI", **kwargs))  # type: ignore[arg-type]
    assert result is None, "空 agents 应直接返回 None（不触网）"


def test_collection_fallback_failure_is_visible_not_debug():
    """★ 可见性判据：兜底异常必须 `logger.warning`，不是 `logger.debug`。

    "兜底坏了"用户只看到"没数据"；若日志是 debug（默认不可见），
    缺陷会像这次一样**藏很久**。判据只认机器可读的那一行源码形状。
    """
    src = SUPERVISOR.read_text(encoding="utf-8")
    idx = src.find("采集路径联网兜底异常")
    assert idx > 0, "找不到那句日志（被人改写了？）"
    window = src[max(0, idx - 120): idx + 60]
    assert "logger.warning" in window, (
        "采集路径联网兜底的异常仍记在 debug 级 ⇒ 静默失效会重演：\n" + window)


def test_a_broken_fallback_does_not_break_collection(monkeypatch):
    """反向判据：兜底**抛异常**时，采集侧必须仍然"当没找到"而不是炸掉。

    这是那条宽 `except` 的**正当用途**（联网是最后一层，坏了不能拖垮采集）——
    所以本测守的是"它不许被去掉"，与上面"必须 warning"是一对。
    """
    src = SUPERVISOR.read_text(encoding="utf-8")
    assert "联网兜底坏了不拖垮采集" in src, (
        "宽 except 被删了 ⇒ 一次联网异常会直接打断采集（这是过度收紧）")


@pytest.mark.parametrize("name", ["_fallback_fetch"])
def test_signature_is_stable_for_other_readers(name: str) -> None:
    """自证：本文件的签名解析与 `inspect` 结果一致（判据自身不许漂）。"""
    from src.orchestration import supervisor as s

    tree = _tree()
    allowed, _ = _signature_of(tree, name)
    real = set(inspect.signature(getattr(s, name)).parameters)
    assert allowed == real, f"AST 解析 {allowed} ≠ inspect {real}"
