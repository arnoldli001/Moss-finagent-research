"""下发浮点数的**统一精度口径**（用户 2026-09-30 裁定）。

## 用户原话

第一轮：

> 「检索下项目平台的所有业务、功能板块，所有涉及浮点数传递的都小数点后
>   最多保留 3 位，避免传输数据过大」

第二轮（看到实测后裁定）：

> 「**一律 3 位有效数字**，同时作用于服务端出口和前端缓存写入」

## 为什么这件事值得在**出口集中做一次**

对外试点走公网隧道，实测带宽 **≈51 KB/s**、劣化时段 **~4.6 KB/s**
（见 `web/src/alertsCache.ts` / `docs/OPS_GUIDE.md`）—— 这条链路上
**响应体积直接等于加载时间**。而浮点尾数是**纯体积、零信息**：

    0.00024339722008355307     ← 19 位小数，前端只显示 3 位
    1653132781025.09012345     ← 16 位有效数字，语义上只到"元"

真正让 gzip 失效的是**高熵尾数**：尾数越长，重复模式越少，压缩率越低。
按 3 位有效数字压掉尾数后，gzip 才重新有效。

## 口径：一律 3 位有效数字

    round_for_transport(0.863388)          → 0.863
    round_for_transport(0.00024339722)     → 0.000243     ← 不归零
    round_for_transport(3.4567)            → 3.46
    round_for_transport(1109.28)           → 1110.0
    round_for_transport(402367923.3305)    → 402000000.0
    round_for_transport(1653132781025.09)  → 1650000000000.0

### 实测（拥挤度详情，最坏板块 1480 根日线，生产库）

| 有效数字 | gzip | vs 基线 | 曲线不同取值 | 归零 |
|---|---|---|---|---|
| 基线（现状） | 36,258 B | — | 96 | 0 |
| **3 位** | **22,318 B** | **−38%** | **96** | **0** |
| 4 位 | 24,287 B | −33% | 96 | 0 |
| 5 位 | 26,200 B | −28% | 96 | 0 |
| 6 位 | 28,016 B | −23% | 96 | 0 |

**3 位有效数字同时做到"最省"与"曲线一个点都不丢"** —— 这是选它的核心理由。

### 为什么不是"3 位小数"（第一轮口径被实测否决）

"小数点后 3 位"在**小量级字段上会销毁数据**：

    字段 raw_crowding（板块成交额占比），真实量级 1e-4
    原值 0.00024339722008355307 → 取 3 位小数 = **0.0**
    该字段 96 个不同取值 → 只剩 **1 个**，1416 个曲线点全部归零

占比 / 胜率 / IC / 相关系数 / `net_to_mv`(1e-9~1e-3) / 佣金率(0.00025)
全是这一族。**有效数字口径自适应量级，天然不归零。**

### 代价（诚实登记，已实测）

| 字段 | 原值 | 3 位有效数字后 | 相对误差 |
|---|---|---|---|
| 收盘价 | 1109.28 | 1110.0 | 6.5e-04 |
| 收盘价 | 12.34 | 12.3 | 3.2e-03 |
| 成交额 | 402,367,923.33 | 402,000,000 | 9.1e-04 |
| 净流入 | −12,345,678.9 | −12,300,000 | 3.7e-03 |
| 涨跌幅 | 3.4567 | 3.46 | 9.5e-04 |

**相对误差一律 < 0.4%**，且**量级与主要数字都可读**。
代价是千元以上价格丢掉分位（`1109.28 → 1110`）—— 这是用户已知情的选择。

## 豁免：**非测量值**的浮点

`EXEMPT_FIELDS` 里的字段名**跳过取整**。当前只有时间类：

* `ts` —— epoch 秒（`src/api/routes/research.py:711`），1.7e9 量级，
  3 位有效数字会把精度劣化到 **±12 分钟**；
* `cached_at` / `cache_age` / `age_seconds` / `seconds` / `elapsed` ——
  计时与单调时钟，不是测量值，压它们没有任何体积收益。

⚠️ **豁免表只能收"非测量值"**，不许把"精度不够用的测量值"塞进来 ——
那会让这张表退化成"哪里红灯加哪里"。真正需要更高精度的字段，
应该在**自己的模块里显式取整**（如 `db.SLIM_PRECISION`），
并在 PRD 里写明理由。

## 与"已取整过"的关系

项目里已有 600+ 处 `round(...)`，精度**不统一**（1/2/3/4/6 位都有），
漏掉的那几处正是体积大头（裸 SQLite REAL 直接进响应体）。
本模块在**出口**再兜一次，把"每个路由各自记得取整"变成**一处保证**。
已取整的值再取整是幂等的，不会变坏；3 位有效数字**比多数既有精度更粗**，
所以出口这一层会统一收口。
"""

from __future__ import annotations

import math
from numbers import Integral, Real
from typing import Any

#: 有效数字位数（用户 2026-09-30 裁定：**一律 3 位**）。
SIGNIFICANT_DIGITS = 3

#: 豁免字段（按 **key 名**匹配，任意层级）—— 只收**非测量值**，理由见模块头。
EXEMPT_FIELDS: frozenset[str] = frozenset({
    # epoch 秒：1.7e9 量级，压到 3 位有效数字 = ±12 分钟误差
    "ts",
    # 计时 / 单调时钟：不是测量值，压了也没有体积收益
    "cached_at",
    "cache_age",
    "cache_age_seconds",
    "age_seconds",
    "elapsed",
    "elapsed_seconds",
    "seconds",
    "waited_seconds",
    "age_sec",
    "remaining_s",
})


def round_significant(value: float, *,
                       digits: int = SIGNIFICANT_DIGITS) -> float:
    """把浮点压到 `digits` 位**有效数字**（纯函数，绝不归零）。

    `0.0` / 非有限值原样返回 —— 非有限交给 `_SafeJSONResponse` 转 `null`，
    本函数不越权（两处都管同一件事迟早漂移）。
    """
    if not math.isfinite(value) or value == 0.0:
        return value
    return float(f"{value:.{digits}g}")


def round_payload(value: Any, *,
                  digits: int = SIGNIFICANT_DIGITS,
                  exempt: frozenset[str] = EXEMPT_FIELDS) -> Any:
    """递归地把一份响应体压到下发精度，**顺带兜住非有限浮点**。

    ## 为什么把 NaN 兜底也放这里（合并，而不是两遍递归）

    出口这条路径**每个响应都要跑**，两遍递归就是全站双倍成本。
    而两件事都需要"带着 key 递归"（豁免按字段名判定），
    所以合并成一次遍历。

    ## 规则

    * `bool` **不当作数字**（Python 里 `bool` 是 `int` 子类；漏判会把
      `True` 变成 `1`，前端 `=== true` 直接失效）；
    * `int` 原样返回（成交量/条数/根数本来就是整数，改动会变类型）；
    * `float`：非有限（NaN / ±Inf）→ `None`（= "这个数没有"，与项目里
      `None` 表示缺失的约定一致；**这是 2026-09-27 做T快照 500 的修复**，
      原来由 `_SafeJSONResponse._clean` 做），否则压到 `digits` 位有效数字；
    * key 命中 `exempt` 的值**原样带走**（见模块头"豁免"）；
    * `dict` / `list` / `tuple` 递归；`tuple` 转 `list`（JSON 里本就是数组）。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return round_significant(value, digits=digits)
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key in exempt:
                out[key] = item
            else:
                out[key] = round_payload(item, digits=digits, exempt=exempt)
        return out
    if isinstance(value, (list, tuple)):
        return [round_payload(item, digits=digits, exempt=exempt)
                for item in value]
    if isinstance(value, Real):
        # numpy 标量等 Real 但不是 float/int/bool 的漏网之鱼。
        #
        # ⚠️ **必须在这里也判非有限**（2026-09-30 实测）：`np.float32('nan')`
        # **不是** `float` 的子类（`np.float64` 才是），所以它**走不到上面那个
        # `isinstance(value, float)` 分支**，而 `round_significant` 对非有限值
        # 是"原样返回"（那是它的契约）⇒ nan 一路漏到 `json.dumps` ⇒
        # `ValueError: Out of range float values are not JSON compliant` ⇒ **整响应 500**。
        # 正是 `tests/unit/test_safe_json_response.py` 那条判据抓到的形态。
        as_float = float(value)
        if not math.isfinite(as_float):
            return None
        # 整型族（np.int64/uint32…）**必须保持 int**：成交量/条数/根数变成 7.0
        # 会让前端"整数"契约静默失效（实测：改前一律被压成 float）。
        if isinstance(value, Integral):
            return int(value)
        return round_significant(as_float, digits=digits)
    # numpy 标量家族（`np.float32` / `np.float16` / `np.int64` / `np.bool_` …）：
    # 它们**既不是** `float` 也不都登记在 `numbers` 里（实测 `np.bool_` 既非
    # `bool` 也非 `Real`）⇒ 原样返回会让 `json.dumps` 抛 `TypeError`（又一个 500）。
    #
    # 修法是**归一后重走一遍本函数**（`numpy` 标量的 `.item()` 返回原生标量）——
    # 这样"非有限→None / 整型保持 int / 布尔保持 bool"三条规则**只写一遍**，
    # 不必在这里再抄一次（抄一遍就等于给下一次漂移留位）。
    item = getattr(value, "item", None)
    if callable(item):
        try:
            native = item()
        except Exception:  # noqa: BLE001 归一失败就原样返回（出口护栏不制造新异常）
            return value
        if native is not value and not callable(getattr(native, "item", None)):
            return round_payload(native, digits=digits, exempt=exempt)
    return value


__all__ = [
    "EXEMPT_FIELDS",
    "SIGNIFICANT_DIGITS",
    "round_payload",
    "round_significant",
]
