"""回测载荷的**类型契约**（`src.mainline.storage.normalize_backtest`）单元测试。

## 事故：`"correlation": {}` 让「回测报告」整页白屏

线上报障：主线挖掘面板点「回测报告」→ `ErrorBoundary` 拦到

    TypeError: Cannot read properties of undefined (reading 'length')

浏览器堆栈定位到**一行**代码：

    !correlation || correlation.factors.length === 0
                     ^^^^^^^^^^^^^^^^^^^^ undefined.length 💥

根因不是"某个字段没显示对"，而是**空壳返回里手写了 `"correlation": {}`**：

* 那次 `mainline_backtest` 表是空的（0 行），路由走"还没有回测记录"分支，
  手工拼了 `"correlation": {}`；
* `{}` 在 JavaScript 里是**真值**，所以前端 `report.correlation ?? null`
  的 `??` 兜底**根本不会触发**；
* 紧接着读 `correlation.factors.length` → `undefined.length`。

修法是**两层**（本文件守住后端那一层）：

1. **后端**：两条返回路径（落库回读 / 空壳）共用 `normalize_backtest`，
   有内容 → 补齐全部键；**空内容 → `None`**（前端已有 `null` 分支）。
2. **前端**：`normalizeBacktestReport()` 在 API 边界再兜一次
   （数组键保证是数组、`correlation` 保证完整对象或 `null`）。

之所以强调"空内容一律 `None`"而不是"补一个空的 factors"：
"这次没做相关性验证"与"做了但一个因子都没算出来"是两件事，
前者该显示"无相关性验证结果"的空状态，后者才该显示表格。
"""

from __future__ import annotations

from src.mainline.storage import normalize_backtest


def test_empty_shell_correlation_becomes_none() -> None:
    """**回归测试**：空壳返回的 `correlation: {}` 必须归一成 `None`（不是 `{}`）。"""
    payload = normalize_backtest({
        "error": "", "run_id": "", "gaps": ["还没有回测记录"],
        "metrics": {}, "folds": [], "scenes": [], "signals": [],
        "false_positives": [], "correlation": {}, "markdown": "",
    })
    # `{}` 是真值 → 前端 `?? null` 兜不住它。必须在这里变成 None。
    assert payload["correlation"] is None
    assert payload["gaps"] == ["还没有回测记录"]


def test_missing_keys_are_filled_with_safe_types() -> None:
    """缺键不能让前端读 `.length` / `.map` 时炸掉。"""
    payload = normalize_backtest({"run_id": "bt-1"})
    for key in ("folds", "scenes", "signals", "false_positives", "gaps"):
        assert payload[key] == [], f"{key} 必须是 list"
    assert payload["metrics"] == {}
    assert payload["correlation"] is None
    assert payload["seconds"] is None
    assert payload["markdown"] == ""


def test_bad_types_are_coerced_not_crashed() -> None:
    payload = normalize_backtest({"run_id": "bt-2", "gaps": "还没有回测记录",
                                  "metrics": None, "correlation": "oops",
                                  "folds": {"not": "a list"}, "seconds": "abc"})
    assert payload["gaps"] == ["还没有回测记录"], "单条字符串要包成一项，不能丢"
    assert payload["metrics"] == {}
    assert payload["correlation"] is None
    assert payload["folds"] == []
    assert payload["seconds"] is None


def test_real_correlation_is_padded_to_full_shape() -> None:
    """有内容时补齐全部键（前端不必写"字段不存在"分支）。"""
    payload = normalize_backtest({"correlation": {"factors": ["six.moneyflow"]}})
    correlation = payload["correlation"]
    assert correlation is not None
    assert correlation["factors"] == ["six.moneyflow"]
    assert correlation["matrix"] == {}
    assert correlation["high_pairs"] == []
    assert correlation["passed"] is False
    assert correlation["note"] == ""


def test_correlation_with_only_high_pairs_is_kept() -> None:
    """`high_pairs` 非空也算"有内容"（矩阵可能因为样本不足没算出来）。"""
    payload = normalize_backtest({"correlation": {
        "high_pairs": [{"a": "x", "b": "y", "corr": 0.9}]}})
    assert payload["correlation"] is not None
    assert payload["correlation"]["high_pairs"][0]["corr"] == 0.9


def test_non_dict_input_does_not_crash() -> None:
    for value in (None, [], "text", 0):
        payload = normalize_backtest(value)
        assert payload["correlation"] is None
        assert payload["gaps"] == []


def test_unknown_keys_are_dropped() -> None:
    """契约之外的键不带出去（避免把内部字段漏到前端）。"""
    payload = normalize_backtest({"run_id": "bt-3", "internal_debug": {"a": 1}})
    assert "internal_debug" not in payload
