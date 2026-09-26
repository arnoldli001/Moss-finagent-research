"""面板**按需加载**：算出"这次跑到底需要哪些字段"，别再把 51 个字段全装进内存。

## 为什么需要它

`build_panels` 原先无条件装配**全部字段**（6 个行情 + 15 个估值 + 5 个资金流 +
11 个 bak + 2 个涨跌停 + 财务面板 + 停牌集合 + 指数）。而 35 个因子里
**真正用到的只有十几个**：

- `bak_daily` 的 11 个字段：**一个因子都没用**（`volume_ratio` 走的是
  `daily_basic.volume_ratio`），却要读 950 万行的数据集；
- `stk_limit`（1400 万行）、`suspend_d`：因子筛选根本不用，只有单票回测用；
- `daily_basic` 15 个字段里因子只用 9 个。

后果是内存与耗时都被放大了几倍。实测（2026-09-25）：660 个交易日 × 全市场的
面板峰值 **5088 MB**；按同一条曲线外推到 2015 年起（约 2600 个交易日）约 15 GB，
普通机器直接 OOM —— 明明本地有 2006 年以来的数据，却因为"全字段装配"用不上。

## 怎么算"需要哪些字段"

对因子函数**做静态分析**，而不是手写一张字段清单：

- 手写清单一定会漂移（新增因子忘了登记 → 面板少装字段 → 因子静默变 NaN，
  这是这个项目里最危险的一类错误）；
- 因子函数的取值方式高度规整（`panels.price("close")`、`panels.basic("pb")`、
  `panels.flow(...)`、`panels.bak_field(...)`、`panels.fundamental_field(...)`、
  `panels.index_returns`/`panels.fundamentals`），字面量参数 → 静态可解析。

**必须做闭包**：因子里有"接收 panels 的 helper"（如
`factor_gross_margin_trend` 调 `_year_ago(panels, ...)`，而字段访问藏在
`_year_ago` 里）。只看因子函数本身会漏掉字段。

安全网有两层：
1. `panels.FactorPanels` 在**严格模式**下（显式给了 needs 时）对未加载字段
   **直接抛错**，而不是返回空表 —— 少装字段会立刻失败，不会变成一列 NaN；
2. `tests/unit/test_panel_needs.py` 覆盖全部 35 个因子的字段闭包。
"""
from __future__ import annotations

import ast
import inspect
import logging
from dataclasses import dataclass, field, replace
from typing import Any, Iterable

logger = logging.getLogger(__name__)

#: 访问器 → needs 里的分组名
_ACCESSOR_GROUP = {
    "price": "price",
    "basic": "basic",
    "flow": "flow",
    "bak_field": "bak",
}
#: 取到"整张表/整块面板"的访问器（不按字段名计）
_TABLE_ACCESSORS = {
    "fundamental_field": "fundamentals",
    "fundamental_frame": "fundamentals",
    "is_suspended": "suspended",
}
#: `panels.price("close_raw")` 实际来自 `daily.close`（未复权收盘），
#: 所以拿到它必须同时加载 close —— 否则列不存在。
_PRICE_ALIASES = {"close_raw": "close"}


@dataclass(frozen=True)
class PanelNeeds:
    """这次装配需要哪些面板内容（字段级）。"""

    price: frozenset[str] = frozenset()
    basic: frozenset[str] = frozenset()
    flow: frozenset[str] = frozenset()
    bak: frozenset[str] = frozenset()
    limits: frozenset[str] = frozenset()
    suspended: bool = False
    index: bool = False
    fundamentals: bool = False

    # ---------- 组合 ----------

    def __or__(self, other: PanelNeeds) -> PanelNeeds:
        return PanelNeeds(
            price=self.price | other.price,
            basic=self.basic | other.basic,
            flow=self.flow | other.flow,
            bak=self.bak | other.bak,
            limits=self.limits | other.limits,
            suspended=self.suspended or other.suspended,
            index=self.index or other.index,
            fundamentals=self.fundamentals or other.fundamentals,
        )

    @classmethod
    def everything(cls) -> PanelNeeds:
        """全字段（= `build_panels` 的历史行为，不给 needs 时的默认）。"""
        from src.quant.panels import (
            BAK_FIELDS,
            BASIC_FIELDS,
            FLOW_FIELDS,
            PRICE_SOURCES,
        )

        return cls(
            price=frozenset(PRICE_SOURCES) | {"close_raw"},
            basic=frozenset(BASIC_FIELDS),
            flow=frozenset(FLOW_FIELDS),
            bak=frozenset(BAK_FIELDS),
            limits=frozenset({"up_limit", "down_limit"}),
            suspended=True, index=True, fundamentals=True,
        )

    @classmethod
    def nothing(cls) -> PanelNeeds:
        return cls()

    # ---------- 体检 ----------

    @property
    def field_count(self) -> int:
        """按字段计的规模（表级内容各算 1）。"""
        return (len(self.price) + len(self.basic) + len(self.flow)
                + len(self.bak) + len(self.limits)
                + int(self.suspended) + int(self.index)
                + int(self.fundamentals))

    @property
    def datasets(self) -> list[str]:
        out: list[str] = []
        if self.price:
            out.append("daily")
            if set(self.price) & {"open", "high", "low", "close", "close_raw"}:
                out.append("adj_factor")
        if self.basic:
            out.append("daily_basic")
        if self.flow:
            out.append("moneyflow")
        if self.bak:
            out.append("bak_daily")
        if self.limits:
            out.append("stk_limit")
        if self.suspended:
            out.append("suspend_d")
        if self.index:
            out.append("index_daily")
        if self.fundamentals:
            out.append("fina_indicator_vip")
        return out

    def describe(self) -> str:
        groups = [
            f"行情={len(self.price)}", f"估值={len(self.basic)}",
            f"资金流={len(self.flow)}", f"备用行情={len(self.bak)}",
            f"涨跌停={len(self.limits)}",
        ]
        if self.suspended:
            groups.append("停牌")
        if self.index:
            groups.append("指数")
        if self.fundamentals:
            groups.append("财务")
        return f"{self.field_count} 个字段（" + "、".join(groups) + "）"

    def normalized(self) -> PanelNeeds:
        """补齐派生字段：`close_raw` 需要 `close`（未复权收盘）。"""
        price = set(self.price)
        for alias, source in _PRICE_ALIASES.items():
            if alias in price:
                price.add(source)
        return replace(self, price=frozenset(price))


# ==================================================================
# 静态分析：因子 → 字段
# ==================================================================


@dataclass
class _FuncInfo:
    name: str
    needs: PanelNeeds = field(default_factory=PanelNeeds.nothing)
    calls: set[str] = field(default_factory=set)


class _AccessorVisitor(ast.NodeVisitor):
    """收集一个函数里对 `panels` 的访问（不进入嵌套函数体）。"""

    def __init__(self, panels_names: set[str],
                 known_functions: set[str]) -> None:
        self.panels_names = panels_names
        self.known_functions = known_functions
        self.groups: dict[str, set[str]] = {
            group: set() for group in ("price", "basic", "flow", "bak")}
        self.flags: dict[str, bool] = {
            "suspended": False, "index": False, "fundamentals": False}
        self.calls: set[str] = set()

    # -- 工具 --

    @staticmethod
    def _literal_arg(node: ast.Call) -> str | None:
        if node.args and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str):
            return node.args[0].value
        return None

    def _is_panels(self, node: ast.AST) -> bool:
        return isinstance(node, ast.Name) and node.id in self.panels_names

    # -- 访问 --

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and self._is_panels(func.value):
            attr = func.attr
            literal = self._literal_arg(node)
            if attr in _ACCESSOR_GROUP and literal:
                self.groups[_ACCESSOR_GROUP[attr]].add(literal)
            elif attr in _TABLE_ACCESSORS:
                self.flags[_TABLE_ACCESSORS[attr]] = True
        elif isinstance(func, ast.Name) and func.id in self.known_functions:
            self.calls.add(func.id)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        # `panels.index_returns` / `panels.fundamentals`（属性访问，不是调用）
        if self._is_panels(node.value) and not isinstance(node.ctx, ast.Store):
            if node.attr == "index_returns":
                self.flags["index"] = True
            elif node.attr == "fundamentals":
                self.flags["fundamentals"] = True
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """不进入嵌套函数体（嵌套函数自己会被单独解析成一个条目）。"""

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """同上。"""

    def visit_Lambda(self, node: ast.Lambda) -> None:
        """lambda 里不访问 panels（本项目的用法如此），不深入。"""


def _panels_params(node: ast.FunctionDef) -> set[str]:
    """这个函数的哪个参数是 panels。

    判据是"注解为 `FactorPanels`"或"参数名就叫 panels" —— 故意**不认 `Any`**：
    那样会把 `_simulate(cfg: Any, ...)` 这类非面板参数误判成 panels。
    """
    names: set[str] = set()
    for arg in list(node.args.args) + list(node.args.kwonlyargs):
        annotation = arg.annotation
        if (isinstance(annotation, ast.Name)
                and annotation.id == "FactorPanels") or arg.arg == "panels":
            names.add(arg.arg)
    return names


_MODULE_INDEX_CACHE: dict[str, dict[str, _FuncInfo]] = {}


def _factor_module_index() -> dict[str, _FuncInfo]:
    """解析 `factor_library_v2` 源码 → 每个函数的字段需求 + 模块内调用关系。

    结果按源文件路径缓存（进程内一次）。文件变了（改因子实现）就该重新解析，
    所以缓存键带上 mtime。
    """
    from src.quant import factor_library_v2 as module

    path = inspect.getsourcefile(module) or "factor_library_v2"
    try:
        mtime = str(int(__import__("os").path.getmtime(path)))
    except OSError:
        mtime = "0"
    cache_key = f"{path}:{mtime}"
    cached = _MODULE_INDEX_CACHE.get(cache_key)
    if cached is not None:
        return cached

    tree = ast.parse(inspect.getsource(module))
    functions = {node.name: node for node in ast.walk(tree)
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    known = set(functions)
    index: dict[str, _FuncInfo] = {}
    for name, node in functions.items():
        visitor = _AccessorVisitor(_panels_params(node), known)
        for child in node.body:
            visitor.visit(child)          # 逐个顶层语句：不进嵌套函数体
        index[name] = _FuncInfo(
            name=name,
            needs=PanelNeeds(
                price=frozenset(visitor.groups["price"]),
                basic=frozenset(visitor.groups["basic"]),
                flow=frozenset(visitor.groups["flow"]),
                bak=frozenset(visitor.groups["bak"]),
                suspended=visitor.flags["suspended"],
                index=visitor.flags["index"],
                fundamentals=visitor.flags["fundamentals"]),
            calls=set(visitor.calls))
    _MODULE_INDEX_CACHE.clear()           # 只保留最新一次解析
    _MODULE_INDEX_CACHE[cache_key] = index
    return index


def _closure(name: str, index: dict[str, _FuncInfo]) -> PanelNeeds:
    """函数 + 它调用的模块内函数的字段需求（广度优先，防环）。"""
    seen: set[str] = set()
    queue = [name]
    total = PanelNeeds.nothing()
    while queue:
        current = queue.pop()
        if current in seen:
            continue
        seen.add(current)
        info = index.get(current)
        if info is None:
            continue
        total = total | info.needs
        queue.extend(sorted(info.calls - seen))
    return total


def needs_for_factor(key: str) -> PanelNeeds:
    """单个因子需要哪些字段。"""
    from src.quant.factor_library_v2 import FACTORS

    spec = FACTORS.get(key)
    if spec is None:
        raise KeyError(f"未知因子 {key!r}")
    index = _factor_module_index()
    info = index.get(spec.func.__name__)
    if info is None:
        # 拿不到源码（动态注册/打包成 pyc）→ 保守地全装，宁可慢也不能算错
        logger.warning("因子 %s 的函数源码不可解析，按需求全量装配面板", key)
        return PanelNeeds.everything()
    return _closure(spec.func.__name__, index).normalized()


def needs_for_factors(keys: Iterable[str] | None = None) -> PanelNeeds:
    """一组因子（None = 全部 35 个）的合并需求。"""
    from src.quant.factor_library_v2 import FACTORS

    selected = list(FACTORS) if keys is None else list(keys)
    total = PanelNeeds.nothing()
    for key in selected:
        total = total | needs_for_factor(key)
    return total


def needs_for_screening(keys: Iterable[str] | None = None, *,
                        neutralize_mv: bool = True,
                        exclude_st: bool = False,
                        liquidity: bool = False) -> PanelNeeds:
    """因子筛选的完整需求 = 因子需求 + 流程自身要用的字段。

    - `price: close`：算 IC 的前瞻收益（`screening.forward_returns`）；
    - `basic: total_mv`：市值中性化（开了才要）；
    - `price: amount`：股票池过滤按 20 日均成交额排序（开了才要）。
    """
    needs = needs_for_factors(keys)
    price = {"close"}
    basic: set[str] = set()
    if neutralize_mv:
        basic.add("total_mv")
    if liquidity:
        price.add("amount")
    return needs | PanelNeeds(price=frozenset(price),
                              basic=frozenset(basic)).normalized()
