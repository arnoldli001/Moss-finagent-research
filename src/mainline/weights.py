"""主线挖掘：层内动态权重（滚动 IC / ICIR）。

## 只调层内，不调层间

需求 3.2 / 4.2 要求"每 20 个交易日根据过去 120 日 IC/ICIR 重新计算权重"。
本模块把这条规则**限定在每一层内部**：

    第一层：六维之间按 ICIR 调权重（合计 100）
    第二层：三维之间按 ICIR 调权重（合计 100）
    层间：**静态配置，不参与动态调整**（`synthesis.layer_weights`；
          V2.0 是 50/50，V2.3 起是 100/0）

为什么层间不动：V1.0 让三层一起按 ICIR 加权，结果是"哪层最近涨得好就全押
哪层"。三层的 IC 高度同源（同一批板块、同一段行情），协方差矩阵接近奇异，
最小化跟踪误差的解会剧烈摆动 —— 权重每周翻倍变化，回测曲线好看，
实盘天天换仓。层间固定是**刻意的保守**，不是为了省事。

⚠️ 但"固定"不等于"50/50 是等权"：两层的分数**不在同一尺度**上
（候选池内 sd 4.5 对 13.1）。每一项的典型幅度是 `w·sd`，所以名义 50/50
实际是约 **26/74**（按 `w·sd`）—— 排序由第二层主导；若按合成方差占比看
更极端（约 11/89，尺度差被平方放大）。V2.3 因此改为 100/0。
要真正等权必须先标准化，见 `scripts/layer_weight_sim.py`。

## 为什么不直接用 ICIR 当权重

`ICIR = IC 均值 / IC 标准差`。直接用它会出两个问题：

1. **负 ICIR 会算出负权重**：某维度近期反向，权重变负，合成分失去"分数"
   的含义（可能出现 -8 分的预警分）。因此设 `min_icir` 下限，
   低于它的维度**退出加权**并重新归一化 —— 这是"这个维度近期没用"，
   不是"这个维度要反着用"（反着用需要回测证明，不能靠一次滚动窗口决定）；
2. **单个维度 ICIR 偶然极高会独占权重**：设 `max_single_weight` 上限。

## 回退机制（需求 8.9）

把动态权重的分与**等权**的分都算出来，比它们在同一段验证期上的 IC：
动态更差就回退等权。理由很实在 —— 动态权重在样本内几乎总是更优，
只有样本外对比才能证明它没在拟合噪声。

## 数据不足一律静态

本地评分历史没攒够 `ic_window_days` 天时返回 `mode="static"` 并说明原因。
用 3 天的 IC 算权重，等于把当天行情当成规律。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from src.mainline.config import MainlineConfig
from src.mainline.scoring import icir, mean, spearman, to_float

#: 权重模式（与 `BoardScore.weight_mode` 的取值一致）
MODE_STATIC = "static"      # 用配置里的初始权重
MODE_DYNAMIC = "dynamic"    # 用滚动 ICIR 算出来的权重
MODE_EQUAL = "equal"        # 回退等权

#: 权重下限：低于该值的维度直接归零（避免一堆 0.3% 的"存在感权重"）
MIN_WEIGHT = 1.0


@dataclass
class WeightOutcome:
    """一次权重解析的结果。"""

    weights: dict[str, float] = field(default_factory=dict)
    mode: str = MODE_STATIC
    ic: dict[str, float] = field(default_factory=dict)
    icir: dict[str, float] = field(default_factory=dict)
    samples: int = 0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"weights": {k: round(v, 3) for k, v in self.weights.items()},
                "mode": self.mode, "samples": self.samples,
                "ic": {k: (round(v, 4) if v is not None else None)
                       for k, v in self.ic.items()},
                "icir": {k: (round(v, 4) if v is not None else None)
                         for k, v in self.icir.items()},
                "note": self.note}


def equal_weights(keys: Sequence[str]) -> dict[str, float]:
    """等权（合计 100）。"""
    size = max(len(keys), 1)
    return {key: 100.0 / size for key in keys}


def icir_weights(keys: Sequence[str], ic_by_key: dict[str, list[Any]], *,
                 config: MainlineConfig, fallback: dict[str, float]
                 ) -> WeightOutcome:
    """按 ICIR 算层内权重（含下限、上限与回退）。"""
    synth = config.synthesis
    outcome = WeightOutcome(mode=MODE_DYNAMIC,
                            weights=dict(fallback))
    effective: dict[str, float] = {}
    for key in keys:
        series = [to_float(item) for item in ic_by_key.get(key, [])]
        clean = [item for item in series if item is not None]
        outcome.ic[key] = mean(clean) if clean else None
        value = icir(clean)
        outcome.icir[key] = value
        if value is not None and value > float(synth.min_icir):
            effective[key] = value
    outcome.samples = max((len([1 for item in ic_by_key.get(key, [])
                                if to_float(item) is not None])
                           for key in keys), default=0)
    if not effective:
        outcome.mode = MODE_STATIC
        outcome.note = ("所有维度 ICIR 均低于下限，沿用初始权重"
                        f"（min_icir={synth.min_icir}）")
        return outcome

    total = sum(effective.values())
    raw = {key: value * 100.0 / total for key, value in effective.items()}
    # 缺 ICIR 的维度权重归零而不是补 0 参与 —— 补 0 会把它们的权重摊给其它维度，
    # 让"没数据"变成"别的维度更重要"，这是两种完全不同的结论。
    for key in keys:
        raw.setdefault(key, 0.0)
    capped = _cap(raw, float(synth.max_single_weight))
    cleaned = {key: (value if value >= MIN_WEIGHT else 0.0)
               for key, value in capped.items()}
    if sum(cleaned.values()) <= 0:
        outcome.mode = MODE_STATIC
        outcome.note = "上限裁剪后权重全为 0，沿用初始权重"
        return outcome
    outcome.weights = _rescale(cleaned)
    dropped = [key for key in keys if cleaned.get(key, 0.0) <= 0]
    if dropped:
        outcome.note = "以下维度近期 ICIR 过低，本轮退出加权：" + "、".join(dropped)
    return outcome


def _cap(weights: dict[str, float], limit: float) -> dict[str, float]:
    """把超过上限的权重削到上限，并把多出来的按比例分给未达上限的维度。

    迭代而不是一次分配：一次分配可能把某个维度推到上限之上，
    再削一次又会打破"合计 100"。两三轮即收敛（维度数很少）。
    """
    if limit <= 0 or limit >= 100:
        return dict(weights)
    out = dict(weights)
    for _ in range(8):
        excess = sum(max(0.0, value - limit) for value in out.values())
        if excess <= 1e-9:
            break
        out = {key: min(value, limit) for key, value in out.items()}
        room = {key: max(0.0, limit - value) for key, value in out.items()}
        capacity = sum(room.values())
        if capacity <= 1e-9:
            break
        for key, value in room.items():
            out[key] += excess * value / capacity
    return out


def _rescale(weights: dict[str, float]) -> dict[str, float]:
    total = sum(weights.values())
    if total <= 0:
        return dict(weights)
    return {key: value * 100.0 / total for key, value in weights.items()}


def ic_series(pairs: Sequence[tuple[Any, Any]], *, min_samples: int = 10
              ) -> float | None:
    """一组 `(因子值, 未来收益)` 配对的秩相关（= 该日的 IC）。

    用秩相关而不是皮尔逊：选板块只用到排序，而分数与收益的关系通常单调
    但非线性（80 分与 70 分的收益差未必等于 60 与 50 的差）。
    """
    if not pairs:
        return None
    left = [item[0] for item in pairs]
    right = [item[1] for item in pairs]
    return spearman(left, right, min_samples=min_samples)


def compare_and_maybe_fallback(dynamic: WeightOutcome, *,
                               dynamic_ic: float | None, equal_ic: float | None,
                               config: MainlineConfig) -> WeightOutcome:
    """回退判定：动态权重的验证期 IC 不优于等权时退回等权（需求 8.9）。"""
    if not config.synthesis.fallback_to_equal:
        return dynamic
    dyn = to_float(dynamic_ic)
    eq = to_float(equal_ic)
    if dyn is None or eq is None:
        dynamic.note = (dynamic.note + "；缺少验证期 IC 对照，保留动态权重").strip("；")
        return dynamic
    if dyn < eq:
        outcome = WeightOutcome(
            weights={key: 100.0 / max(len(dynamic.weights), 1)
                     for key in dynamic.weights},
            mode=MODE_EQUAL, ic=dict(dynamic.ic), icir=dict(dynamic.icir),
            samples=dynamic.samples,
            note=(f"动态权重验证期 IC（{dyn:.4f}）低于等权（{eq:.4f}），"
                  "本轮回退等权"))
        return outcome
    return dynamic


__all__ = [
    "MIN_WEIGHT",
    "MODE_DYNAMIC",
    "MODE_EQUAL",
    "MODE_STATIC",
    "WeightOutcome",
    "compare_and_maybe_fallback",
    "equal_weights",
    "ic_series",
    "icir_weights",
]
