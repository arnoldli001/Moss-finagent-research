"""下发浮点精度口径的判据（`CHG-0138`）。

## 用户要求（两轮，后一轮是裁定）

> 第一轮：「检索下项目平台的所有业务、功能板块，所有涉及浮点数传递的
>   都小数点后最多保留 3 位，避免传输数据过大」
>
> 第二轮（看到实测后）：「**一律 3 位有效数字**，同时作用于服务端出口和
>   前端缓存写入」

## 这组判据守什么

1. **3 位有效数字**（用户裁定本身）；
2. ★ **小量级不许被压成 0** —— 这是**正确性**判据，也正是"3 位小数"
   被否决的原因：实测 `raw_crowding`（板块成交额占比，量级 1e-4）
   取 3 位小数后 **96 个不同取值只剩 1 个**，1416 个曲线点全部归零；
3. ★ **非测量值豁免** —— epoch 秒（`ts`）压到 3 位有效数字会劣化到
   **±12 分钟**；计时字段压了也没有体积收益；
4. **类型不许被改坏** —— `bool` 不能被当成数字（Python 里 `bool` 是
   `int` 子类），`True` 变 `1` 会让前端 `=== true` 失效；`int` 不许变成 float；
5. ★ **出口必须真的接上** —— `_SafeJSONResponse.render` 必须调
   `round_payload`（语法树判据）。"加了函数但没人调用"是本项目
   登记过的失效形状；
6. ★ **NaN 兜底不许被这次改动弄丢** —— 那是 2026-09-27 做T快照 500 的修复，
   与精度压缩共用同一次遍历，很容易在重构时掉一个。
"""

from __future__ import annotations

import ast
import json
import math
from pathlib import Path
from typing import Any

import pytest

from src.core.float_precision import (
    EXEMPT_FIELDS,
    SIGNIFICANT_DIGITS,
    round_payload,
    round_significant,
)

ROOT = Path(__file__).resolve().parents[2]
MAIN_PY = ROOT / "src" / "api" / "main.py"
FRONTEND_TS = ROOT / "web" / "src" / "floatPrecision.ts"
CACHE_TS = ROOT / "web" / "src" / "crowdingDetailCache.ts"


# ------------------------------------------------------- ① 3 位有效数字

@pytest.mark.parametrize("raw, expect", [
    (0.86338812345678, 0.863),
    (3.4567, 3.46),
    (12.3456, 12.3),
    (1109.28, 1110.0),
    (-9.87654, -9.88),
    (0.00024339722008355307, 0.000243),
    (402367923.3305, 402000000.0),
    (1653132781025.09, 1650000000000.0),
    (7.34, 7.34),
    (0.5, 0.5),
])
def test_three_significant_digits(raw: float, expect: float) -> None:
    """用户裁定的口径：一律 3 位有效数字。"""
    assert round_significant(raw) == expect


def test_relative_error_stays_small() -> None:
    """★ 取整的**相对误差必须可忽略**（实测全部 < 0.4%）。

    这条是"精度代价"的守门人：如果哪天有人把位数调低，误差会先红。
    """
    for raw in (0.863388, 3.4567, 12.3456, 1109.28, 402367923.3305,
                1653132781025.09, -12345678.9, 0.00024339722008):
        got = round_significant(raw)
        relative = abs(got - raw) / abs(raw)
        assert relative < 0.01, (
            f"{raw!r} → {got!r} 相对误差 {relative:.1%} 过大")


def test_small_magnitude_is_not_crushed_to_zero() -> None:
    """★ **正确性判据**：小量级不许被压成 0。

    实测现场：`raw_crowding` 真实值 0.00024339722008355307，
    **纯 3 位小数会得到 0.0**，该字段 96 个不同取值只剩 1 个。
    占比 / 胜率 / IC / 相关系数 / `net_to_mv`(1e-9~1e-3) / 佣金率(0.00025)
    全是这一族 —— 有效数字口径天然不归零。
    """
    for raw in (0.00024339722008355307, 0.0002372813604450766,
                2.5473e-09, 0.00025, 0.00001, 6e-06):
        got = round_significant(raw)
        assert got != 0.0, (
            f"{raw!r} 被压成了 0.0 —— 小量级字段会变成一条直线")


def test_small_magnitude_keeps_order_of_magnitude() -> None:
    """小量级取整后**量级必须不变**。"""
    for raw in (0.00024339722008355307, 2.5473e-09, 6e-06, 0.0123):
        got = round_significant(raw)
        assert math.floor(math.log10(abs(got))) == math.floor(
            math.log10(abs(raw))), f"{raw!r} → {got!r} 量级变了"


def test_distinct_values_stay_distinguishable() -> None:
    """★ 曲线的**可分辨性**必须保住（归零真正的危害）。

    造一列同量级、彼此接近的占比值 —— 取整后不许坍缩成一个值。
    实测：`raw_crowding` 的 96 个不同取值在 3 位有效数字下**全部保留**。
    """
    series = [0.00024339722008, 0.00023728136044, 0.000244, 0.000251,
              0.000230, 0.000199, 0.0002601]
    got = {round_significant(v) for v in series}
    assert len(got) >= 5, (
        f"7 个不同取值取整后只剩 {len(got)} 个 —— 曲线被压平了：{sorted(got)}")


# ------------------------------------------------------- ② 时间类豁免

def test_epoch_seconds_is_exempt() -> None:
    """★ epoch 秒**不许**被压 —— 3 位有效数字会劣化到 ±12 分钟。

    `src/api/routes/research.py:711` 的存活探针 `ts` 就是 1.7e9 量级。
    """
    epoch = 1790749917.019103
    out = round_payload({"ts": epoch})
    assert out["ts"] == epoch, (
        f"ts 被压成 {out['ts']!r} —— epoch 秒会劣化到 ±12 分钟")
    # 反向：证明这个量级**真的**会被压坏（判据不是恒绿）
    assert round_significant(epoch) != epoch, (
        "3 位有效数字居然没改变 epoch 秒 —— 豁免判据可能取错了对象")


def test_timing_fields_are_exempt() -> None:
    """计时字段（非测量值）豁免 —— 压它们没有体积收益。"""
    for key in ("cached_at", "cache_age", "age_seconds", "seconds",
                "elapsed", "elapsed_seconds"):
        assert key in EXEMPT_FIELDS, f"{key} 不在豁免表里"
        out = round_payload({key: 1234.5678})
        assert out[key] == 1234.5678, f"{key} 被压了"


def test_exempt_only_holds_non_measurements() -> None:
    """★ 豁免表**只许收非测量值**。

    这是防"豁免表退化成『哪里红灯加哪里』"的元判据：测量类字段名
    一旦出现在这里，说明有人为了让测试变绿而豁免了它 ——
    正确做法是在该字段自己的模块里显式取整并写明理由。
    """
    forbidden = {
        "water_level", "raw_crowding", "ma5_crowding", "sector_amount",
        "market_amount", "net_inflow", "circ_mv_base", "close", "price",
        "entry_close", "net_to_mv", "score", "confidence",
    }
    overlap = forbidden & EXEMPT_FIELDS
    assert not overlap, (
        f"豁免表里出现了测量字段 {sorted(overlap)} —— "
        f"精度不够就应该改口径，不是在出口豁免它")


# ------------------------------------------------------- ③ 类型不许改坏

def test_bool_is_not_treated_as_number() -> None:
    """★ `bool` 不许被当成数字（Python 里 `bool` 是 `int` 子类）。"""
    assert round_payload({"ok": True, "no": False}) == {"ok": True,
                                                        "no": False}
    assert round_payload([True, False]) == [True, False]


def test_int_stays_int() -> None:
    """整数不许变成浮点（成交量/条数/根数都是 int）。"""
    out = round_payload({"bars": 1480, "n": 7})
    assert out == {"bars": 1480, "n": 7}
    assert isinstance(out["bars"], int) and not isinstance(out["bars"], bool)


def test_nan_becomes_null_but_infinity_too() -> None:
    """★ NaN/±Inf → `None`（**2026-09-27 做T快照 500 的修复，不许丢**）。

    这次把 NaN 兜底与精度压缩合并进同一次遍历，最容易掉的就是这条。
    """
    assert round_payload({"a": float("nan")})["a"] is None
    assert round_payload({"a": float("inf")})["a"] is None
    assert round_payload({"a": float("-inf")})["a"] is None


def test_none_stays_none() -> None:
    """缺失值原样保留（`None` 是"数据不足"的语义）。"""
    assert round_payload({"water_level": None}) == {"water_level": None}


def test_recurses_into_nested_structures() -> None:
    payload: Any = {"a": [{"b": 0.123456}], "c": (1.98765,)}
    out = round_payload(payload)
    assert out["a"][0]["b"] == 0.123
    assert out["c"] == [1.99], "tuple 应转成 list（JSON 里本就是数组）"


def test_does_not_mutate_input() -> None:
    """**不许就地改入参** —— 路由可能还在用同一个对象。"""
    original = {"v": 0.123456789}
    snapshot = dict(original)
    round_payload(original)
    assert original == snapshot, "round_payload 改了入参"


def test_idempotent() -> None:
    """重复取整不许继续变（出口可能与路由内已有的 `round` 叠加）。"""
    once = round_payload({"v": 0.123456789})
    twice = round_payload(once)
    assert once == twice == {"v": 0.123}


# ------------------------------------------------------- ④ 出口真的接上了

def test_outlet_calls_the_rounder() -> None:
    """★ **接入判据**（语法树）：`_SafeJSONResponse.render` 必须调 `round_payload`。

    "写了函数但没人调用"是本项目登记过的失效形状。行为判据测不到它 ——
    因为模块单测会直接调函数、全绿。
    """
    tree = ast.parse(MAIN_PY.read_text(encoding="utf-8"))
    target = next((node for node in ast.walk(tree)
                   if isinstance(node, ast.ClassDef)
                   and node.name == "_SafeJSONResponse"), None)
    assert target is not None, "找不到 _SafeJSONResponse"

    called = {node.func.id for node in ast.walk(target)
              if isinstance(node, ast.Call)
              and isinstance(node.func, ast.Name)}
    assert "round_payload" in called, (
        "_SafeJSONResponse 没有调用 round_payload —— "
        "统一精度口径没有生效（函数写了不等于接上了）")


def test_outlet_is_the_default_response_class() -> None:
    """`_SafeJSONResponse` 必须仍是全局 default_response_class。

    否则它只对显式声明了该类的路由生效 —— 而本项目的路由**基本都不声明**，
    等于整条口径静默失效。
    """
    src = MAIN_PY.read_text(encoding="utf-8")
    assert "default_response_class=_SafeJSONResponse" in src, (
        "FastAPI(...) 的 default_response_class 不再是 _SafeJSONResponse —— "
        "全平台浮点精度口径会静默失效")


def test_outlet_end_to_end() -> None:
    """端到端：经出口渲染后，尾数消失、豁免保精度、NaN 转 null。"""
    from src.api.main import _SafeJSONResponse

    payload = {
        "water_level": 0.00024339722008355307,
        "sector_amount": 1653132781025.09012345,
        "ts": 1790749917.019103,
        "nan": float("nan"),
        "flag": True,
        "bars": 1480,
    }
    body = _SafeJSONResponse(payload).render(payload).decode()
    got = json.loads(body)
    assert got["water_level"] == 0.000243
    assert got["sector_amount"] == 1650000000000.0
    assert got["ts"] == 1790749917.019103, "ts 豁免没生效"
    assert got["nan"] is None
    assert got["flag"] is True and got["bars"] == 1480
    assert "0.00024339722008355307" not in body


# ------------------------------------------------------- ⑤ 前端同一口径

def test_frontend_rule_matches_backend_digits() -> None:
    """★ 前端必须用**同一个位数**（两端漂移会让缓存与下发精度不一致）。"""
    src = FRONTEND_TS.read_text(encoding="utf-8")
    assert f"SIGNIFICANT_DIGITS = {SIGNIFICANT_DIGITS}" in src, (
        f"前端位数与后端 SIGNIFICANT_DIGITS={SIGNIFICANT_DIGITS} 不一致 —— "
        f"会出现「缓存命中一种精度、联网核对另一种精度」的抖动")


def test_frontend_exempt_list_matches_backend() -> None:
    """★ 前端豁免清单必须与后端**逐字一致**。

    两端都按字段名豁免；清单漂移会让同一个字段在服务端保精度、
    在缓存里被压掉（或反过来），而**没有任何报错**。
    """
    src = FRONTEND_TS.read_text(encoding="utf-8")
    missing = [name for name in sorted(EXEMPT_FIELDS) if f'"{name}"' not in src]
    assert not missing, (
        f"前端豁免清单缺 {missing} —— 与 src/core/float_precision.py 漂移了")


def test_cache_write_applies_the_rounding() -> None:
    """★ 缓存**写入**必须走取整（用户明确要求"作用于前端缓存写入"）。

    这条防的是"只在服务端做了" —— 那缓存里仍会是 19 位小数，
    约 258 KB/份，撑爆 localStorage 会连累其它模块的缓存。
    """
    src = CACHE_TS.read_text(encoding="utf-8")
    # 去掉注释再断言 —— 注释里写着 roundPayload 会被误判为已接入
    stripped = "\n".join(line.split("//")[0] for line in src.splitlines())
    assert "roundPayload(" in stripped, (
        "crowdingDetailCache 的写入路径没有调 roundPayload —— "
        "缓存里仍会是完整精度（用户要求同时作用于缓存写入）")
