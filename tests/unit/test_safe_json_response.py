"""出口护栏：非有限浮点不得让整个响应 500（2026-09-27 做T快照事故）。

## 事故链条

    Starlette JSONResponse.render()  用 allow_nan=False
      → 响应体里出现 NaN / ±Inf 就抛 ValueError
      → 异常在**响应渲染阶段**，即路由 try/except **之外**
      → 业务侧"取数失败转 502"的兜底拦不住
      → 用户只看到 `SYS_5000 服务器开了点小差，请稍后重试`

真实表现：做T快照偶发失败，且**报的是 500 而不是 502** ——
查了半天才发现是某个因子算出了 `nan`。
"""
from __future__ import annotations

import json

import numpy as np
import pytest
from starlette.responses import JSONResponse


def _render(payload):
    from src.api.main import _SafeJSONResponse

    return _SafeJSONResponse(payload).body


def test_starlette_default_really_rejects_nan() -> None:
    """先钉住**前提**：默认 JSONResponse 确实会炸。

    这条不是为了测 starlette，而是防止哪天它改了默认值、
    让下面几条测试变成"测了个不存在的问题"还一路绿灯。
    """
    with pytest.raises(ValueError, match="Out of range float values"):
        _ = JSONResponse({"x": float("nan")}).body


def test_safe_response_replaces_non_finite_with_null() -> None:
    raw = _render({"a": float("nan"), "b": 1.5,
                   "c": float("inf"), "d": -float("inf")})
    assert json.loads(raw) == {"a": None, "b": 1.5, "c": None, "d": None}


def test_safe_response_walks_nested_containers() -> None:
    """NaN 常藏在深层结构里（`factors -> items -> [ {score: nan} ]`）。"""
    raw = _render({"nested": {"x": [1, float("nan"), {"y": float("inf")}]}})
    assert json.loads(raw) == {"nested": {"x": [1, None, {"y": None}]}}


def test_safe_response_handles_numpy_scalars() -> None:
    """pandas / numpy 的中间结果最常产出 `np.float32('nan')` 这类标量。

    注意 `np.float32` **不是** `float` 的子类（`np.float64` 才是），
    所以判据用 `numbers.Real` 而不是 `isinstance(v, float)`。
    """
    raw = _render({"np32_nan": np.float32("nan"), "np64": np.float64(2.5),
                   "np_int": np.int64(7), "np_inf": np.float32("inf")})
    assert json.loads(raw) == {"np32_nan": None, "np64": 2.5,
                               "np_int": 7, "np_inf": None}


def test_safe_response_preserves_normal_values_and_types() -> None:
    """不能把正常值改坏：`True` 不该变成 `1.0`，整数不该变成浮点。"""
    raw = _render({"ok": 3, "flag": True, "none": None, "txt": "x", "f": 1.25})
    assert json.loads(raw) == {"ok": 3, "flag": True, "none": None,
                               "txt": "x", "f": 1.25}
