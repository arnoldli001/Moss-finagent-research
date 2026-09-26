"""主线挖掘：宏观状态与行业敏感度映射（第一层 macro 维度的输入）。

## 为什么单独一个文件

宏观这一维度有两块完全不同的东西，混在 `six_dim.py` 里会让两边都难读：

1. **宏观状态**（时序）：PMI / M1 / M2 / 社融 / Shibor / CPI / PPI 各自当前
   处在历史什么位置 → 一个 z-score。这是**全市场共用**的。
2. **行业敏感度**（横截面）：`configs/mainline_macro_sensitivity.yaml` 里
   31 个申万一级行业对 7 个宏观因子的敏感度（-1 ~ +1）。这是**行业各异**的。

    macro_tilt(行业) = Σ_f 敏感度_f × 方向归一后的 z_f  /  Σ_f |敏感度_f|

## 三个容易搞错的地方

**1. 方向归一必须在算 z 之后做，且只做一次。**
`shibor_3m` 的 `direction: lower`（利率越低越好），所以它的 z 要乘 -1。
敏感度表里填的是"**该因子原始值上行**时行业的暴露方向"（房地产对利率 -0.8
= 利率上行受损），因此**两边都不要再翻一次符号** —— 二次翻号会让所有
利率敏感行业的得分符号整体反过来，而分数看起来仍然"合理"（0-100 之间），
不报任何错。

**2. 宏观是"共同项"，必须限制它的权重占比。**
如果 31 个行业都用同一套宏观 z，那 macro 维度对**横截面排序**没有贡献，
只会把所有板块的分数一起抬高或压低。`common_weight` 就是给这个共同项的
权重：它越高，板块之间的分差越小（分数趋同 → 告警集中在阈值附近抖动）。

**3. 概念板块没有行业敏感度。**
`ths_index` 的 2500 个概念板块不在申万 31 个行业里。此时只能给**共同项**
（全市场宏观状态），并如实标注"该板块宏观分为市场状态代理，不含行业倾斜"。

## 缺失即 None，不补零

某因子接口不可用（如权限不足的 PPI）时，它**不进入**求和的分母，
而不是当 0 敏感度参与 —— 当 0 会让分母虚大、所有行业的 tilt 被系统性压小，
而界面上看不出任何异常。
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from src.core.errors import BRIEF_TIGHT, brief
from src.mainline.config import MainlineConfig, load_config, load_yaml_config
from src.mainline.scoring import mean, to_float, zscore

logger = logging.getLogger(__name__)

#: 方向归一：`higher` = 因子值越高越利好（z 保持原号）；
#: `lower` = 因子值越低越利好（z 取反）。
_DIRECTION_SIGN = {"higher": 1.0, "lower": -1.0}


@dataclass
class MacroFactor:
    """一个宏观因子的定义（来自敏感度表的 `factors` 段）。"""

    key: str
    label: str = ""
    source: str = ""
    direction: str = "higher"

    @property
    def sign(self) -> float:
        return _DIRECTION_SIGN.get(self.direction, 1.0)


@dataclass
class MacroState:
    """一次宏观状态快照（全市场共用）。"""

    as_of: str = ""
    #: `{factor_key: z}`（**已按方向归一**，正数 = 对多数行业偏利好）
    z: dict[str, float] = field(default_factory=dict)
    #: `{factor_key: (period, value)}` 原始值（展示用，保留未归一的方向）
    raw: dict[str, tuple[str, float]] = field(default_factory=dict)
    #: 各因子 z 的均值（共同项：全市场宏观状态）
    common: float = 0.0
    gaps: list[str] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return len(self.z) > 0

    def to_dict(self) -> dict[str, Any]:
        return {"as_of": self.as_of, "common": round(self.common, 4),
                "z": {key: round(value, 4) for key, value in self.z.items()},
                "raw": {key: [period, value]
                        for key, (period, value) in self.raw.items()},
                "gaps": list(self.gaps)}


@dataclass
class MacroSensitivity:
    """行业敏感度表（31 个申万一级行业 × 7 个宏观因子）。"""

    factors: dict[str, MacroFactor] = field(default_factory=dict)
    #: `{行业代码: {因子: 敏感度}}`
    by_code: dict[str, dict[str, float]] = field(default_factory=dict)
    #: `{行业名: 行业代码}`（概念板块按名字匹配行业时用）
    name_to_code: dict[str, str] = field(default_factory=dict)
    loaded: bool = False
    gap: str = ""

    def for_board(self, *, code: str = "", name: str = "") -> dict[str, float]:
        """取某板块的敏感度向量（找不到返回空 dict，调用方走共同项）。"""
        if code and code in self.by_code:
            return self.by_code[code]
        if name:
            key = self.name_to_code.get(name)
            if key and key in self.by_code:
                return self.by_code[key]
        return {}

    def tilt(self, sensitivity: dict[str, float],
             state: MacroState) -> tuple[float | None, list[str]]:
        """把敏感度 × 宏观 z 折成一个行业倾斜值（-1 ~ +1 量级）。

        返回 `(tilt, 参与计算的因子列表)`。`tilt` 为 None 表示无可用因子
        （敏感度为空 or 宏观 z 全缺）—— 调用方据此走共同项而不是拿 0 冒充。
        """
        if not sensitivity or not state.z:
            return None, []
        numerator = 0.0
        denominator = 0.0
        used: list[str] = []
        for key, value in sensitivity.items():
            factor = self.factors.get(key)
            z = state.z.get(key)
            if factor is None or z is None:
                continue
            weight = abs(float(value))
            if weight <= 0:
                continue
            numerator += float(value) * z
            denominator += weight
            used.append(key)
        if denominator <= 0:
            return None, []
        return numerator / denominator, used


#: 敏感度表的默认文件名（与 `MainlineConfig.macro.sensitivity_file` 同源）
DEFAULT_SENSITIVITY_FILE = "mainline_macro_sensitivity.yaml"


def load_sensitivity(config: MainlineConfig | None = None,
                     *, path: str = "") -> MacroSensitivity:
    """加载行业敏感度表；文件缺失/损坏时返回空表并在 `gap` 里写明原因。"""
    config = config or load_config()
    name = path or config.macro.sensitivity_file or DEFAULT_SENSITIVITY_FILE
    raw = load_yaml_config(name)
    if raw is None:
        return MacroSensitivity(
            loaded=False, gap=f"宏观敏感度表不可用：configs/{name}")
    out = MacroSensitivity(loaded=True)
    for item in raw.get("factors") or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        if not key:
            continue
        out.factors[key] = MacroFactor(
            key=key, label=str(item.get("label") or key),
            source=str(item.get("source") or ""),
            direction=str(item.get("direction") or "higher"))
    for item in raw.get("industries") or []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        if not code:
            continue
        vector = {str(k): float(v) for k, v in (item.get("sensitivity") or {}).items()
                  if to_float(v) is not None}
        out.by_code[code] = vector
        for candidate in (code, f"{code}.SI"):
            out.name_to_code[candidate] = code
        industry_name = str(item.get("name") or "").strip()
        if industry_name:
            out.name_to_code[industry_name] = code
    if not out.factors:
        out.gap = f"宏观敏感度表里没有 factors 段：configs/{name}"
    if not out.by_code:
        out.gap = f"宏观敏感度表里没有 industries 段：configs/{name}"
    return out


def build_macro_state(store: Any, config: MainlineConfig | None = None,
                      *, sensitivity: MacroSensitivity | None = None
                      ) -> MacroState:
    """从本地数据仓读宏观序列并算当前 z-score（按方向归一）。

    `store` 是 `MainlineDataStore`（读 `ml_macro`）；测试里可传一个只有
    `macro()` 方法的假对象。窗口取 `config.macro.window_months` 个月。
    """
    config = config or load_config()
    table = sensitivity if sensitivity is not None else load_sensitivity(config)
    state = MacroState()
    if not table.factors:
        state.gaps.append(table.gap or "宏观敏感度表未加载")
        return state
    window = max(12, int(config.macro.window_months or 36))
    zs: dict[str, float] = {}
    latest_period = ""
    for key, factor in table.factors.items():
        try:
            series = store.macro(key, limit=window)
        except Exception as exc:  # noqa: BLE001
            state.gaps.append(f"宏观因子 {key} 读取失败：{brief(exc, BRIEF_TIGHT)}")
            continue
        values = [value for _, value in series]
        if len(values) < 6:
            state.gaps.append(f"宏观因子 {key} 历史不足（{len(values)} 期 < 6）")
            continue
        series_z = zscore(values)
        latest = None
        for item in reversed(series_z):
            if item is not None:
                latest = item
                break
        if latest is None:
            state.gaps.append(f"宏观因子 {key} 无波动（z 无定义）")
            continue
        zs[key] = latest * factor.sign
        period, value = series[-1]
        state.raw[key] = (period, value)
        latest_period = max(latest_period, period)
    state.z = zs
    state.as_of = latest_period
    common = mean(list(zs.values()))
    state.common = float(common) if common is not None else 0.0
    return state


def macro_common_score(common: float, *, scale: float = 2.0) -> float:
    """把共同项 z 映射到 0-100 分（50 = 中性）。

    用 `tanh` 而不是线性：宏观 z 在极端月份能到 ±3，线性映射会一路撞到 0 或
    100 的边界，于是"极度宽松"和"相当宽松"得到同一个分 —— 而这两者对
    风险偏好的含义并不相同。tanh 让极端值平滑收敛到边界。
    """
    value = to_float(common) or 0.0
    return 50.0 + 50.0 * math.tanh(value / max(abs(scale), 1e-6))


def tilt_score(tilt: float, *, scale: float = 2.0) -> float:
    """把行业倾斜值映射到 0-100 分（与 `macro_common_score` 同一口径）。"""
    return macro_common_score(tilt, scale=scale)


def blend_macro(specific: float | None, common: float, *,
                common_weight: float) -> float:
    """行业倾斜分与共同项分的加权（`specific` 为 None 时全用共同项）。"""
    weight = max(0.0, min(1.0, float(common_weight or 0.0)))
    if specific is None:
        return common
    return (1.0 - weight) * specific + weight * common


def explain(state: MacroState, table: MacroSensitivity) -> str:
    """一句话总结当前宏观状态（写进 `gaps` / 面板提示）。"""
    if not state.available:
        return "宏观状态不可用：" + ("；".join(state.gaps[:2]) or "无数据")
    parts: list[str] = []
    for key in sorted(state.z, key=lambda item: -abs(state.z[item]))[:3]:
        factor = table.factors.get(key)
        label = factor.label if factor else key
        direction = "偏强" if state.z[key] > 0 else "偏弱"
        parts.append(f"{label}{direction}")
    return (f"宏观状态（{state.as_of}）：共同项 z={state.common:+.2f}，"
            + "、".join(parts))


def factors_used(state: MacroState) -> Iterable[str]:
    return tuple(state.z)


__all__ = [
    "DEFAULT_SENSITIVITY_FILE",
    "MacroFactor",
    "MacroSensitivity",
    "MacroState",
    "blend_macro",
    "build_macro_state",
    "explain",
    "factors_used",
    "load_sensitivity",
    "macro_common_score",
    "tilt_score",
]
