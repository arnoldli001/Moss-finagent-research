"""派生指标（公式化）：概念 → 公式 → 输入数据名 → 取数 → **受控**计算。

## 用户口径（2026-09-29 原话）

> 「① 银行息差 未实现，这种未能实现的概念，可以先**联网搜索概念的意义及公式**，
>   根据公式里**设计的数据名称**，再去找**本地数据库和联网数据**，最后**计算**
>   （编码 agent）」

## 四步与各自的单一真值源

| 步 | 做什么 | 真值源 |
|---|---|---|
| ① | 概念 → 公式（**必须带出处 URL**） | `configs/derived_indicators.yaml`：`formula` / `sources` |
| ② | 公式 → 需要的数据名称（每个符号给**候选名序列**） | 同文件的 `inputs` |
| ③ | 数据名 → 取数 | 既有确定性流水线（本地目录 → 连接器 → 联网兜底） |
| ④ | 计算 | 本模块的 `safe_eval`（**AST 白名单算子**） |

## 三条不许越过的线

1. **公式不许凭记忆写**：`sources` 为空 ⇒ 加载时直接报错
   （公式错会产出"看着有据的错误数字"，比缺数据更危险）。
2. **取不到就是缺口，绝不用 0 兜**（AGENTS.md：「没量到」≠「量到 0」）——
   缺哪个输入、试过哪些候选名，都随结果返回。
3. **不执行任意代码**：`safe_eval` 只认数字与 `+ - * / // % ** ( )`，
   名字必须是已解析到值的输入符号。用户说的"编码 agent"落在
   **把新公式/新输入写进注册表**这一步（可评审、可回归），
   而**不是**在运行时执行它生成的代码（那等于把沙箱交给模型）。

## 与既有机制的关系

* 财务比率的**直接取列**（`资产负债率` 这类）不属这里 —— 那是连接器的活；
* 本模块只处理"**库里没有、需要按公式算**"的指标（`_DERIVED_STOCK_INDICATORS`
  里原本硬编码的 `股息率` 就是这一类，见该处的说明）。
"""

from __future__ import annotations

import ast
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from src.core.schemas import DataPoint, DataSourceType, FetchMethod

logger = logging.getLogger(__name__)

#: 注册表路径（相对项目根）。
SPEC_REL = "configs/derived_indicators.yaml"

#: 允许的二元/一元算子（**白名单**，不是黑名单）。
_BIN_OPS: dict[type, Callable[[float, float], float]] = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.Pow: lambda a, b: a ** b,
    ast.Mod: lambda a, b: a % b,
    ast.FloorDiv: lambda a, b: a // b,
}
_UNARY_OPS: dict[type, Callable[[float], float]] = {
    ast.UAdd: lambda a: +a,
    ast.USub: lambda a: -a,
}


class FormulaError(ValueError):
    """公式非法（含未知算子 / 未知符号 / 除零）。"""


def safe_eval(formula: str, values: dict[str, float]) -> float:
    """按公式求值 —— **只认数字、括号与白名单算子**。

    为什么不用 `eval`：公式来自配置文件与（将来）编码 agent 的产出，
    `eval` 等于把任意代码执行权交出去。这里用 `ast` 逐节点校验：
    表达式节点只允许 `BinOp`/`UnaryOp`/`Constant(数字)`/`Name(输入符号)`，
    其它一律 `FormulaError`。
    """
    try:
        tree = ast.parse(formula, mode="eval")
    except SyntaxError as exc:  # 语法错也是公式错
        raise FormulaError(f"公式语法错误：{formula!r}（{exc.msg}）") from exc

    def _eval(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise FormulaError(f"公式里只允许数字常量：{node.value!r}")
            return float(node.value)
        if isinstance(node, ast.Name):
            if node.id not in values:
                raise FormulaError(f"公式引用了未提供的输入：{node.id}")
            raw = values[node.id]
            if raw is None:
                raise FormulaError(f"输入 {node.id} 没有值（缺口不能当 0 用）")
            return float(raw)
        if isinstance(node, ast.BinOp):
            op = _BIN_OPS.get(type(node.op))
            if op is None:
                raise FormulaError(f"不允许的算子：{type(node.op).__name__}")
            left, right = _eval(node.left), _eval(node.right)
            try:
                return op(left, right)
            except ZeroDivisionError as exc:
                # ★ 自己写判据时抓到的：裸 `ZeroDivisionError` 会以**未处理异常**
                #   逃出流水线（看起来像代码 bug），而它其实是**数据问题**
                #   （分母为 0/NULL）。统一成 `FormulaError` 才能被上层当缺口处置。
                raise FormulaError(
                    f"除零：{left!r} {'/' if isinstance(node.op, ast.Div) else 'op'} "
                    f"{right!r} —— 分母为 0 或空，按缺口处理") from exc
        if isinstance(node, ast.UnaryOp):
            op = _UNARY_OPS.get(type(node.op))
            if op is None:
                raise FormulaError(f"不允许的一元算子：{type(node.op).__name__}")
            return op(_eval(node.operand))
        raise FormulaError(f"公式里不允许的语法：{type(node).__name__}")

    result = _eval(tree)
    if isinstance(result, float) and not math.isfinite(result):
        raise FormulaError(f"计算结果不是有限数：{result}（检查分母是否为 0）")
    return result


def formula_symbols(formula: str) -> list[str]:
    """公式里引用的输入符号（保序去重）—— 步②「从公式提取数据名称」的实现。"""
    tree = ast.parse(formula, mode="eval")
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id not in out:
            out.append(node.id)
    return out


@dataclass(frozen=True)
class DerivedSpec:
    """一条派生指标的注册条目。"""

    id: str
    name: str
    formula: str
    inputs: dict[str, tuple[str, ...]]
    unit: str = ""
    frequency: str = ""
    aliases: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    bias: str = ""
    notes: str = ""

    @property
    def prefix(self) -> str:
        """指标前缀（`净息差:{code}` → `净息差`），供 `supports()` 用。"""
        return self.id.split(":", 1)[0]

    @property
    def symbols(self) -> list[str]:
        """公式需要的输入符号（**从 formula 现读**，与 `inputs` 对齐由测试保证）。"""
        return formula_symbols(self.formula)


@dataclass
class InputValue:
    """一个输入符号解析到的值（含它是用哪个候选名拿到的）。"""

    symbol: str
    value: float
    used_name: str
    source: str = ""
    period: str = ""


@dataclass
class DerivedResult:
    """派生结果：**要么有值（带完整口径），要么有缺口（带缺哪个）**。"""

    indicator: str
    value: float | None
    used_inputs: dict[str, InputValue] = field(default_factory=dict)
    #: `symbol → 试过的候选名`（缺口时用来告诉人"下一步该接什么数据"）
    missing: dict[str, list[str]] = field(default_factory=dict)
    reason: str = ""
    spec: DerivedSpec | None = None

    @property
    def ok(self) -> bool:
        return self.value is not None

    def to_point(self) -> Any:
        """★ 派生结果 → `DataPoint`（**落事实表用**，2026-09-30）。

        ## 为什么必须落库（用户原话）

        「2 的 净息差的值，**每次记录**有利于**统计变化趋势**，来衡量银行的
        收益曲线和映射业绩，因为银行主要赚息差。」

        只算不存 ⇒ 趋势永远只有一个"当前值"，历史无法回看；而且维护审计与
        `catalog_*` 批采都看不见它（不在索引里 = 不存在）。

        ## 判据

          * `period_date` 取**输入里最新的那个期间**（不是今天）—— 否则同一份
            财报会被记成"今天的读数"，**趋势线全是假的**；
          * **`value is None` 时不产点**：没算出来绝不当 0 落库；
          * 口径（formula / sources / used_inputs / bias）整体进 `extra`；
          * `confidence` = **0.85**（低于原始读数的 1.0）：派生值多了一跳计算，
            且可能用了退化分母（见 `bias`）—— 不该与原始读数同置信。
            ⚠️ 不取"输入置信度的最小值"：`InputValue` 不带置信度，
            现算一个只会是**假精确**。
        """
        if not self.ok or self.value is None or self.spec is None:
            return None
        periods = [iv.period for iv in self.used_inputs.values() if iv.period]
        period = max(periods) if periods else None
        sources = sorted({iv.source for iv in self.used_inputs.values() if iv.source})
        return DataPoint(
            indicator=self.indicator,
            value=float(self.value),
            unit=self.spec.unit or None,
            period_date=period,
            extra=self.extra(),
            source_name=("派生计算（" + "、".join(sources) + "）") if sources
            else "派生计算",
            source_url=(self.spec.sources[0] if self.spec.sources else ""),
            source_type=DataSourceType.DERIVED,
            fetch_method=FetchMethod.COMPUTED,
            processed_by="derive.py",
            confidence=0.85,
        )

    def extra(self) -> dict[str, Any]:
        """随数据点下发的口径（AGENTS.md：口径与局限随数据一起下发）。"""
        if self.spec is None:
            return {}
        payload: dict[str, Any] = {
            "derived": True,
            "formula": self.spec.formula,
            "unit": self.spec.unit,
            "sources": list(self.spec.sources),
            "used_inputs": {
                sym: {"value": iv.value, "name": iv.used_name,
                      "source": iv.source, "period": iv.period}
                for sym, iv in self.used_inputs.items()
            },
        }
        if self.spec.bias:
            payload["bias"] = " ".join(self.spec.bias.split())
        if self.spec.notes:
            payload["notes"] = " ".join(self.spec.notes.split())
        if not self.ok:
            payload["missing_inputs"] = self.missing
            payload["missing_reason"] = self.reason
        return payload


def load_specs(path: str | Path | None = None) -> list[DerivedSpec]:
    """读注册表。**没有 `sources` 或 `inputs` 覆盖不到公式符号 ⇒ 直接报错**。"""
    target = Path(path) if path else Path(SPEC_REL)
    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    items = raw.get("derived") or []
    specs: list[DerivedSpec] = []
    for item in items:
        spec = DerivedSpec(
            id=str(item["id"]),
            name=str(item.get("name") or item["id"]),
            formula=str(item["formula"]),
            inputs={str(k): tuple(str(x) for x in (v or ()))
                    for k, v in (item.get("inputs") or {}).items()},
            unit=str(item.get("unit") or ""),
            frequency=str(item.get("frequency") or ""),
            aliases=tuple(str(a) for a in (item.get("aliases") or ())),
            sources=tuple(str(s) for s in (item.get("sources") or ())),
            bias=str(item.get("bias") or ""),
            notes=str(item.get("notes") or ""),
        )
        if not spec.sources:
            raise FormulaError(
                f"派生指标 {spec.id} 没有 `sources`（公式出处）—— "
                "公式不许凭记忆写：写错会产出「看着有据的错误数字」")
        missing_spec = [s for s in spec.symbols if s not in spec.inputs]
        if missing_spec:
            raise FormulaError(
                f"派生指标 {spec.id} 的公式引用了 {missing_spec}，"
                "但 `inputs` 里没有对应的候选名序列")
        specs.append(spec)
    return specs


def find_spec(indicator: str, specs: list[DerivedSpec] | None = None) -> DerivedSpec | None:
    """按指标前缀或别名找条目（`净息差:600036` / `银行息差` 都能命中）。"""
    bare = indicator.split(":", 1)[0].strip()
    for spec in specs if specs is not None else load_specs():
        if bare == spec.prefix or bare == spec.name or bare in spec.aliases:
            return spec
    return None


#: 取数回调：`(数据名候选, code) -> (值, 来源, 期间) | None`
Fetcher = Callable[[str, str], "tuple[float, str, str] | None"]


def derive(
    indicator: str, code: str, fetch: Fetcher,
    specs: list[DerivedSpec] | None = None,
) -> DerivedResult:
    """跑完四步：解析输入 → 取数 → 计算（**缺输入就如实报缺口**）。

    `fetch(name, code)` 由调用方注入（本地目录 / 连接器 / 联网兜底都行）——
    本模块**不自己连数据库、不自己联网**，这样它在单测里可以完全离线。
    """
    spec = find_spec(indicator, specs)
    if spec is None:
        return DerivedResult(indicator=indicator, value=None,
                             reason=f"没有登记的派生公式：{indicator}")
    values: dict[str, float] = {}
    used: dict[str, InputValue] = {}
    missing: dict[str, list[str]] = {}
    for symbol in spec.symbols:
        candidates = spec.inputs.get(symbol, ())
        hit: InputValue | None = None
        for name in candidates:
            got = fetch(name, code)
            if got is None:
                continue
            value, source, period = got
            hit = InputValue(symbol=symbol, value=float(value),
                             used_name=name, source=source, period=period)
            break
        if hit is None:
            missing[symbol] = list(candidates)
            continue
        values[symbol] = hit.value
        used[symbol] = hit
    if missing:
        tried = "；".join(f"{s}（试过 {', '.join(names) or '无候选'}）"
                         for s, names in missing.items())
        return DerivedResult(
            indicator=indicator, value=None, used_inputs=used, missing=missing,
            reason=f"公式输入取不到：{tried} —— 已交联网兜底/补采队列",
            spec=spec)
    try:
        value = safe_eval(spec.formula, values)
    except FormulaError as exc:
        return DerivedResult(indicator=indicator, value=None, used_inputs=used,
                             reason=f"计算失败：{exc}", spec=spec)
    return DerivedResult(indicator=indicator, value=value, used_inputs=used,
                         spec=spec)


__all__ = ["SPEC_REL", "FormulaError", "DerivedSpec", "DerivedResult",
           "InputValue", "derive", "find_spec", "formula_symbols", "load_specs",
           "safe_eval"]
