"""多因子条件组合 DSL：解析 + 截面求值（条件选股 / 条件回测的前置引擎）。

**为什么手写解析器，而不是设计文档 §5.2 的正则方案**

文档给的 `ConditionParser` 把 `AND`→`&`、`OR`→`|`，然后「只要串里出现 & 就整体做 AND」。
它连文档自己的示例 4 都算不对：

    PE < 25 AND ROE > 12 AND (Momentum20 > 5 OR Momentum60 > 10) AND Turnover > 1

按那份实现，括号与 OR 全部失效，等价于「四个条件同时成立」—— 动量条件被要求
同时满足 20 日和 60 日，选出的股票池直接变小且**不报错**。示范：只要其中一边不成立就漏选。
这类"算错但不报错"的缺陷在回测里最贵（净值曲线看起来很合理）。

本模块用 tokenizer + 递归下降解析，语义按标准优先级：

    NOT > AND > OR        （括号可覆盖）
    BETWEEN / IN 作为比较运算符
    数学表达式可出现在比较两侧（如 `Momentum20 - Momentum60 > 2`）

**三值逻辑（重要）**：A 股数据经常缺（停牌、次新股无财报、因子未覆盖）。
`PE != 30` 在 pandas 里对 NaN 会返回 True —— 于是"未知"被当成"满足条件"混进股票池。
这里比较运算统一用 pandas 可空布尔（`boolean` dtype）：
    未知 & False = False、未知 & True = 未知、未知 | True = True、NOT 未知 = 未知
最后 `evaluate()` 把"未知"归为**不入选**（`explain()` 会报出未知只数，不静默）。

**安全**：纯 AST 求值，不用 eval/exec；因子名必须真实存在于输入截面表里，
未知名字报错并给出可用列表（`__import__` 这类输入只会得到"未知因子"错误）。

用法：
    from src.quant.condition_dsl import parse_condition
    cond = parse_condition("PE < 30 AND ROE > 15 AND RANK(Momentum20) > 80")
    mask = cond.evaluate(factor_df)          # pd.Series[bool]，index 与入参对齐
    picked = cond.select(factor_df)
    cond.explain(factor_df)                  # {'total':…, 'selected':…, 'unknown':…}
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

# ==================================================================
# 异常
# ==================================================================


class ConditionError(ValueError):
    """条件表达式错误（语法/未知因子/函数签名）。带出错位置，便于前端高亮。"""

    def __init__(self, message: str, *, position: int | None = None,
                 text: str = "") -> None:
        self.position = position
        self.text = text
        if position is not None and text:
            pointer = " " * position + "^"
            message = f"{message}\n{text}\n{pointer}"
        super().__init__(message)


# ==================================================================
# 词法
# ==================================================================

_KEYWORDS = {"and", "or", "not", "between", "in", "true", "false"}
_COMPARE_OPS = ("<=", ">=", "!=", "==", "<>", "=", "<", ">")
_ARITH_OPS = ("+", "-", "*", "/")

_TOKEN_RE = re.compile(
    r"""
    (?P<space>\s+)
  | (?P<number>\d+\.\d*|\.\d+|\d+)(?P<percent>\s*%)?
  | (?P<string>"[^"]*"|'[^']*')
  | (?P<ident>[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]*)
  | (?P<op><=|>=|!=|==|<>|=|<|>|\+|-|\*|/|\(|\)|,)
    """,
    re.VERBOSE,
)


@dataclass(frozen=True)
class _Token:
    kind: str          # number / string / ident / op / eof
    value: str
    position: int
    percent: bool = False   # 数字后是否带 % 单位标记


def tokenize(text: str) -> list[_Token]:
    """把条件串切成 token（空白忽略；关键字大小写不敏感）。"""
    tokens: list[_Token] = []
    pos = 0
    while pos < len(text):
        match = _TOKEN_RE.match(text, pos)
        if match is None:
            raise ConditionError(f"无法识别的字符 {text[pos]!r}", position=pos,
                                 text=text)
        pos = match.end()
        if match.group("space"):
            continue
        if match.group("number") is not None:
            tokens.append(_Token("number", match.group("number"),
                                 match.start(), bool(match.group("percent"))))
        elif match.group("string") is not None:
            tokens.append(_Token("string", match.group("string")[1:-1],
                                 match.start()))
        elif match.group("ident") is not None:
            word = match.group("ident")
            kind = "keyword" if word.lower() in _KEYWORDS else "ident"
            tokens.append(_Token(kind, word, match.start()))
        else:
            tokens.append(_Token("op", match.group("op"), match.start()))
    tokens.append(_Token("eof", "", len(text)))
    return tokens


# ==================================================================
# 语法树
# ==================================================================


@dataclass(frozen=True)
class Node:
    """AST 基类。`position` 用于报错定位。"""

    position: int = 0


@dataclass(frozen=True)
class Num(Node):
    value: float = 0.0


@dataclass(frozen=True)
class Str(Node):
    value: str = ""


@dataclass(frozen=True)
class Bool(Node):
    value: bool = False


@dataclass(frozen=True)
class FactorRef(Node):
    name: str = ""


@dataclass(frozen=True)
class Unary(Node):
    op: str = "-"
    operand: Node = None  # type: ignore[assignment]


@dataclass(frozen=True)
class Arith(Node):
    op: str = "+"
    left: Node = None   # type: ignore[assignment]
    right: Node = None  # type: ignore[assignment]


@dataclass(frozen=True)
class Compare(Node):
    op: str = "<"
    left: Node = None   # type: ignore[assignment]
    right: Node = None  # type: ignore[assignment]


@dataclass(frozen=True)
class Logical(Node):
    op: str = "AND"     # AND / OR
    left: Node = None   # type: ignore[assignment]
    right: Node = None  # type: ignore[assignment]


@dataclass(frozen=True)
class Not(Node):
    operand: Node = None  # type: ignore[assignment]


@dataclass(frozen=True)
class Between(Node):
    value: Node = None   # type: ignore[assignment]
    low: Node = None     # type: ignore[assignment]
    high: Node = None    # type: ignore[assignment]
    negated: bool = False


@dataclass(frozen=True)
class InSet(Node):
    value: Node = None            # type: ignore[assignment]
    options: tuple[Node, ...] = ()
    negated: bool = False


@dataclass(frozen=True)
class Call(Node):
    func: str = ""
    args: tuple[Node, ...] = ()


# ==================================================================
# 语法分析：递归下降
# ==================================================================


class _Parser:
    """expr := or_expr；优先级 NOT > AND > OR，比较低于算术。"""

    def __init__(self, tokens: list[_Token], text: str) -> None:
        self._tokens = tokens
        self._text = text
        self._index = 0
        self.warnings: list[str] = []

    # ---------- 工具 ----------

    @property
    def _current(self) -> _Token:
        return self._tokens[self._index]

    def _advance(self) -> _Token:
        token = self._tokens[self._index]
        if token.kind != "eof":
            self._index += 1
        return token

    def _error(self, message: str, token: _Token | None = None) -> ConditionError:
        token = token or self._current
        return ConditionError(message, position=token.position, text=self._text)

    def _at_keyword(self, *words: str) -> bool:
        token = self._current
        return (token.kind == "keyword"
                and token.value.lower() in words)

    def _at_op(self, *ops: str) -> bool:
        token = self._current
        return token.kind == "op" and token.value in ops

    def _expect(self, kind: str, value: str | None = None) -> _Token:
        token = self._current
        if token.kind != kind or (value is not None and token.value != value):
            expected = value if value is not None else kind
            raise self._error(f"期望 {expected}，实际是 {token.value or '表达式结束'}")
        return self._advance()

    # ---------- 逻辑层 ----------

    def parse(self) -> Node:
        node = self._or_expr()
        if self._current.kind != "eof":
            raise self._error(f"表达式结尾出现多余内容 {self._current.value!r}")
        return node

    def _or_expr(self) -> Node:
        node = self._and_expr()
        while self._at_keyword("or"):
            token = self._advance()
            node = Logical(op="OR", left=node, right=self._and_expr(),
                           position=token.position)
        return node

    def _and_expr(self) -> Node:
        node = self._not_expr()
        while self._at_keyword("and"):
            token = self._advance()
            node = Logical(op="AND", left=node, right=self._not_expr(),
                           position=token.position)
        return node

    def _not_expr(self) -> Node:
        if self._at_keyword("not"):
            token = self._advance()
            return Not(operand=self._not_expr(), position=token.position)
        return self._comparison()

    # ---------- 比较层 ----------

    def _comparison(self) -> Node:
        left = self._arith()
        token = self._current
        if token.kind == "keyword" and token.value.lower() == "between":
            self._advance()
            negated = False
            low = self._arith()
            if not self._at_keyword("and"):
                raise self._error("BETWEEN 缺少 AND（应写成 x BETWEEN a AND b）")
            self._advance()
            high = self._arith()
            return Between(value=left, low=low, high=high, negated=negated,
                           position=token.position)
        if token.kind == "keyword" and token.value.lower() == "in":
            self._advance()
            self._expect("op", "(")
            options: list[Node] = [self._comparand()]
            while self._at_op(","):
                self._advance()
                options.append(self._comparand())
            self._expect("op", ")")
            return InSet(value=left, options=tuple(options),
                         position=token.position)
        if token.kind == "keyword" and token.value.lower() == "not":
            # NOT IN
            nxt = self._tokens[self._index + 1] if self._index + 1 < len(self._tokens) else None
            if nxt is not None and nxt.kind == "keyword" and nxt.value.lower() == "in":
                self._advance()
                self._advance()
                self._expect("op", "(")
                options2: list[Node] = [self._comparand()]
                while self._at_op(","):
                    self._advance()
                    options2.append(self._comparand())
                self._expect("op", ")")
                return InSet(value=left, options=tuple(options2), negated=True,
                             position=token.position)
        if token.kind == "op" and token.value in _COMPARE_OPS:
            op = self._normalize_compare(self._advance().value)
            return Compare(op=op, left=left, right=self._arith(),
                           position=token.position)
        return left

    @staticmethod
    def _normalize_compare(op: str) -> str:
        if op in ("=", "=="):
            return "=="
        if op == "<>":
            return "!="
        return op

    def _comparand(self) -> Node:
        """IN 列表里的元素：只允许字面量（数字/字符串/负数）。"""
        if self._at_op("-"):
            token = self._advance()
            node = self._atom()
            if not isinstance(node, Num):
                raise self._error("IN 列表只支持字面量", token)
            return Num(value=-node.value, position=token.position)
        node = self._atom()
        if not isinstance(node, (Num, Str)):
            raise self._error("IN 列表只支持字面量（数字或字符串）")
        return node

    # ---------- 算术层 ----------

    def _arith(self) -> Node:
        node = self._term()
        while self._at_op("+", "-"):
            token = self._advance()
            node = Arith(op=token.value, left=node, right=self._term(),
                         position=token.position)
        return node

    def _term(self) -> Node:
        node = self._unary()
        while self._at_op("*", "/"):
            token = self._advance()
            node = Arith(op=token.value, left=node, right=self._unary(),
                         position=token.position)
        return node

    def _unary(self) -> Node:
        if self._at_op("-", "+"):
            token = self._advance()
            operand = self._unary()
            if token.value == "+":
                return operand
            return Unary(op="-", operand=operand, position=token.position)
        return self._atom()

    def _atom(self) -> Node:
        token = self._current
        if token.kind == "number":
            self._advance()
            if token.percent:
                # 百分号只作单位提示：数值原样使用（Tushare/AkShare 的 roe 就是
                # 15.32 表示 15.32%）。若写成 0.15 会退化成"几乎所有股票都满足"，
                # 因此这里显式提示口径，而不是偷偷除以 100。
                self.warnings.append(
                    f"「{token.value}%」按因子的百分数原始单位解释（等价于 "
                    f"{token.value}）；若该因子是小数口径请直接写 {token.value}%"
                    f" 对应的小数。")
            return Num(value=float(token.value), position=token.position)
        if token.kind == "string":
            self._advance()
            return Str(value=token.value, position=token.position)
        if token.kind == "keyword" and token.value.lower() in ("true", "false"):
            self._advance()
            return Bool(value=token.value.lower() == "true",
                        position=token.position)
        if token.kind == "ident":
            self._advance()
            if self._at_op("("):
                self._advance()
                args: list[Node] = []
                if not self._at_op(")"):
                    args.append(self._or_expr())
                    while self._at_op(","):
                        self._advance()
                        args.append(self._or_expr())
                self._expect("op", ")")
                return Call(func=token.value, args=tuple(args),
                            position=token.position)
            return FactorRef(name=token.value, position=token.position)
        if token.kind == "op" and token.value == "(":
            self._advance()
            node = self._or_expr()
            self._expect("op", ")")
            return node
        raise self._error(f"无法解析的内容 {token.value or '表达式结束'}")


# ==================================================================
# 求值
# ==================================================================

# 截面函数：对整列做聚合/排名
_CROSS_SECTION_FUNCS = {
    "rank", "pctl", "avg", "mean", "std", "stdev", "median", "min", "max", "count",
}
# 逐元素函数
_ELEMENT_FUNCS = {"abs", "log", "sqrt", "sign"}
# 时间序列函数（**因果**：只用 t 及之前的数据，窗口长度是最后一个参数）。
#
# 为什么必须有这一族：单股票回测里，"收盘价 > 20 日均线"、"波动率处于过去 60 日
# 低位"这类条件根本没法用截面算子表达 —— 而截面算子在单股票场景下**极其危险**：
# 它的求值对象是"整个序列"，于是 `RANK(PE)` 会拿今天和未来所有日子的 PE 一起排名，
# 是彻头彻尾的未来函数（而且算出来还很像样，不会报错）。
# 所以单股票模式下一律拒绝截面算子，只放行这一族。
_TS_FUNCS = {
    "ma", "mean_ts", "std_ts", "sum_ts", "max_ts", "min_ts",
    "pctl_ts", "ref", "delta", "count_ts", "zscore_ts",
}


def _is_series(value: Any) -> bool:
    return isinstance(value, pd.Series)


def _isna(value: Any) -> Any:
    """标量/序列的统一缺失判断（返回 bool 或 bool Series）。"""
    if _is_series(value):
        return value.isna()
    if value is None:
        return True
    if isinstance(value, float):
        return math.isnan(value)
    return False


def _as_boolean(series: pd.Series) -> pd.Series:
    return series.astype("boolean")


def _compare(left: Any, right: Any, op: str) -> Any:
    """比较运算：缺失一律记为「未知」（pandas 可空布尔），不做 True/False 猜测。"""
    if not _is_series(left) and not _is_series(right):
        if _isna(left) or _isna(right):
            return pd.NA
        return bool(_apply_scalar_compare(left, right, op))
    left_series = left if _is_series(left) else None
    right_series = right if _is_series(right) else None
    if left_series is None:
        left_series = pd.Series(left, index=right_series.index)
    if right_series is None:
        right_series = pd.Series(right, index=left_series.index)
    left_series, right_series = left_series.align(right_series, join="outer")
    unknown = _isna(left_series) | _isna(right_series)
    with np.errstate(invalid="ignore"):
        raw = _apply_scalar_compare(left_series, right_series, op)
    out = _as_boolean(raw if _is_series(raw) else pd.Series(raw, index=left_series.index))
    out[unknown] = pd.NA
    return out


def _apply_scalar_compare(left: Any, right: Any, op: str) -> Any:
    if op == ">":
        return left > right
    if op == ">=":
        return left >= right
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    raise ValueError(f"未知比较运算符 {op}")


def _logical(left: Any, right: Any, op: str) -> Any:
    """Kleene 三值逻辑（pandas boolean dtype 原生支持 NA 传播）。"""
    if not _is_series(left) and not _is_series(right):
        if op == "AND":
            if left is False or right is False:
                return False
            if left is pd.NA or right is pd.NA:
                return pd.NA
            return bool(left and right)
        if left is True or right is True:
            return True
        if left is pd.NA or right is pd.NA:
            return pd.NA
        return bool(left or right)
    if not _is_series(left):
        left = pd.Series(left, index=right.index)
    if not _is_series(right):
        right = pd.Series(right, index=left.index)
    left = left.align(right, join="outer")[0]
    right = right.align(left, join="outer")[0]
    return (_as_boolean(left) & _as_boolean(right) if op == "AND"
            else _as_boolean(left) | _as_boolean(right))


@dataclass
class _EvalContext:
    frame: pd.DataFrame
    factor_names: tuple[str, ...]
    # 单股票（时序）模式：行是 **日期**，列是因子。此时截面算子（RANK/AVG/…）
    # 会被解释成"拿整条序列排名/求均值"，等于把未来数据算进来 —— 必须拒绝。
    ts_mode: bool = False


def _eval(node: Node, ctx: _EvalContext) -> Any:
    if isinstance(node, Num):
        return node.value
    if isinstance(node, Str):
        return node.value
    if isinstance(node, Bool):
        return node.value
    if isinstance(node, FactorRef):
        if node.name not in ctx.frame.columns:
            available = ", ".join(map(str, ctx.factor_names[:40]))
            more = "…" if len(ctx.factor_names) > 40 else ""
            raise ConditionError(
                f"未知因子 {node.name!r}。当前可用因子：{available}{more}",
                position=node.position)
        return ctx.frame[node.name]
    if isinstance(node, Unary):
        operand = _eval(node.operand, ctx)
        return -operand
    if isinstance(node, Arith):
        left = _eval(node.left, ctx)
        right = _eval(node.right, ctx)
        if node.op == "+":
            return left + right
        if node.op == "-":
            return left - right
        if node.op == "*":
            return left * right
        # 除零：pandas 给 inf，转成缺失更符合选股语义
        with np.errstate(divide="ignore", invalid="ignore"):
            result = left / right
        if _is_series(result):
            return result.replace([np.inf, -np.inf], np.nan)
        return np.nan if result in (np.inf, -np.inf) else result
    if isinstance(node, Compare):
        return _compare(_eval(node.left, ctx), _eval(node.right, ctx), node.op)
    if isinstance(node, Logical):
        return _logical(_eval(node.left, ctx), _eval(node.right, ctx), node.op)
    if isinstance(node, Not):
        operand = _eval(node.operand, ctx)
        if _is_series(operand):
            return ~_as_boolean(operand)
        if operand is pd.NA:
            return pd.NA
        return not bool(operand)
    if isinstance(node, Between):
        low = _eval(node.low, ctx)
        high = _eval(node.high, ctx)
        value = _eval(node.value, ctx)
        inside = _logical(_compare(value, low, ">="), _compare(value, high, "<="),
                          "AND")
        if not node.negated:
            return inside
        return ~_as_boolean(inside) if _is_series(inside) else (
            pd.NA if inside is pd.NA else not inside)
    if isinstance(node, InSet):
        value = _eval(node.value, ctx)
        options = [_eval(option, ctx) for option in node.options]
        if not options:
            raise ConditionError("IN 列表不能为空", position=node.position)
        combined: Any = None
        for option in options:
            piece = _compare(value, option, "==")
            combined = piece if combined is None else _logical(combined, piece, "OR")
        if not node.negated:
            return combined
        return ~_as_boolean(combined) if _is_series(combined) else (
            pd.NA if combined is pd.NA else not combined)
    if isinstance(node, Call):
        return _eval_call(node, ctx)
    raise ConditionError(f"不支持的表达式节点 {type(node).__name__}")


def _eval_call(node: Call, ctx: _EvalContext) -> Any:
    func = node.func.lower()
    if func in _TS_FUNCS:
        return _eval_time_series(func, node, ctx)
    if func in _CROSS_SECTION_FUNCS:
        if ctx.ts_mode:
            raise ConditionError(
                f"单股票（时序）模式下不能用截面函数 {node.func}()。"
                f"它的求值对象是整条序列 —— `RANK(x)` 会拿今天和未来所有交易日一起"
                f"排名，是未来函数（算出来还很像样，不会报错）。"
                f"请改用时序函数：MA(x,n) / STD_TS(x,n) / PCTL_TS(x,n) / "
                f"MAX_TS(x,n) / MIN_TS(x,n) / ZSCORE_TS(x,n) / REF(x,n) / "
                f"DELTA(x,n) / COUNT_TS(cond,n)",
                position=node.position)
        return _eval_cross_section(func, node, ctx)
    if func in _ELEMENT_FUNCS:
        if len(node.args) != 1:
            raise ConditionError(f"{node.func}() 需要 1 个参数",
                                 position=node.position)
        value = _eval(node.args[0], ctx)
        if func == "abs":
            return abs(value)
        if func == "sign":
            return np.sign(value)
        with np.errstate(invalid="ignore", divide="ignore"):
            if func == "log":
                return np.log(value.where(value > 0) if _is_series(value)
                              else (value if value > 0 else np.nan))
            return np.sqrt(value.where(value >= 0) if _is_series(value)
                           else (value if value >= 0 else np.nan))
    raise ConditionError(
        f"未知函数 {node.func}()。截面函数：RANK/PCTL/AVG/STD/MEDIAN/MIN/MAX/COUNT；"
        f"逐元素函数：ABS/LOG/SQRT/SIGN；"
        f"时序函数（单股票用）：MA/STD_TS/SUM_TS/MAX_TS/MIN_TS/PCTL_TS/"
        f"ZSCORE_TS/REF/DELTA/COUNT_TS", position=node.position)


def _window_of(node: Call, ctx: _EvalContext, func: str,
               *, default: int | None = None) -> int:
    """取最后一个参数作为窗口长度（必须是正整数常数）。"""
    if len(node.args) < 2 and default is None:
        raise ConditionError(f"{node.func}() 需要 2 个参数：(序列, 窗口天数)",
                             position=node.position)
    raw = _eval(node.args[-1], ctx) if len(node.args) >= 2 else default
    if _is_series(raw):
        raise ConditionError(f"{node.func}() 的窗口必须是常数",
                             position=node.position)
    try:
        window = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ConditionError(f"{node.func}() 的窗口必须是整数",
                             position=node.position) from exc
    if window < 1:
        raise ConditionError(f"{node.func}() 的窗口必须 ≥1", position=node.position)
    if window > 2000:
        raise ConditionError(f"{node.func}() 的窗口过大（>{2000} 天）",
                             position=node.position)
    return window


def _eval_time_series(func: str, node: Call, ctx: _EvalContext) -> Any:
    """时序函数求值：**只用 t 及之前的数据**（因果），窗口滚动。

    实现用 pandas 的 `rolling`/`shift`，天然满足"不含未来"：
    `rolling(n)` 的窗口是 `[t-n+1, t]`，`shift(k)` 是滞后 k 期。
    `min_periods` 设为窗口长度，保证样本不足时返回缺失而不是拿 3 天均值冒充 20 日均线。
    """
    if not ctx.ts_mode:
        raise ConditionError(
            f"{node.func}() 是时序函数，只能在单股票（时序）模式下使用。"
            f"截面模式下每一行是一只股票、没有「历史窗口」可言。",
            position=node.position)
    if func == "count_ts":
        if len(node.args) != 2:
            raise ConditionError("COUNT_TS(条件, 窗口) 需要 2 个参数",
                                 position=node.position)
        window = _window_of(node, ctx, func)
        condition = _eval(node.args[0], ctx)
        series = _as_boolean(condition) if _is_series(condition) else pd.Series(
            bool(condition), index=ctx.frame.index)
        counts = series.astype("float64").fillna(0.0).rolling(
            window, min_periods=window).sum()
        return counts

    if not node.args:
        raise ConditionError(f"{node.func}() 需要参数", position=node.position)
    value = _eval(node.args[0], ctx)
    if not _is_series(value):
        value = pd.Series([value] * ctx.frame.shape[0], index=ctx.frame.index,
                          dtype="float64")

    if func == "ref":
        window = _window_of(node, ctx, func)
        return value.shift(window)
    if func == "delta":
        window = _window_of(node, ctx, func)
        return value - value.shift(window)

    window = _window_of(node, ctx, func)
    rolling = value.rolling(window, min_periods=window)
    if func in ("ma", "mean_ts"):
        return rolling.mean()
    if func == "std_ts":
        return rolling.std(ddof=1)
    if func == "sum_ts":
        return rolling.sum()
    if func == "max_ts":
        return rolling.max()
    if func == "min_ts":
        return rolling.min()
    if func == "zscore_ts":
        mean, std = rolling.mean(), rolling.std(ddof=1)
        with np.errstate(invalid="ignore", divide="ignore"):
            zscore = (value - mean) / std.where(std > 0)
        return zscore.where(np.isfinite(zscore))
    if func == "pctl_ts":
        # 当前值在**过去 window 天**里的百分位（0~100，含当天）。
        # rolling.apply 是 Python 级循环，但只算一列、窗口 ≤2000，实测可接受；
        # 换成 (value >= window_values).mean() 的向量化写法会引入更多中间对象。
        return rolling.apply(
            lambda window_values: float(
                (window_values <= window_values[-1]).mean() * 100.0), raw=True)
    raise ConditionError(f"未知时序函数 {node.func}()", position=node.position)


def _eval_cross_section(func: str, node: Call, ctx: _EvalContext) -> Any:
    if func == "count":
        if node.args:
            raise ConditionError("COUNT() 不接受参数", position=node.position)
        return float(ctx.frame.shape[0])
    if not node.args:
        raise ConditionError(f"{node.func}() 需要参数", position=node.position)
    series = _eval(node.args[0], ctx)
    if not _is_series(series):
        series = pd.Series([series] * ctx.frame.shape[0], index=ctx.frame.index)
    if func == "rank":
        # 百分位排名 0~100（1 表示最小）。缺失保持缺失。
        if len(node.args) != 1:
            raise ConditionError("RANK(x) 只接受 1 个参数", position=node.position)
        return series.rank(pct=True) * 100.0
    if func == "pctl":
        if len(node.args) != 2:
            raise ConditionError("PCTL(x, q) 需要 2 个参数（q 为 0~1 分位）",
                                 position=node.position)
        q = _eval(node.args[1], ctx)
        if _is_series(q):
            raise ConditionError("PCTL 的分位参数必须是常数", position=node.position)
        if not 0.0 <= float(q) <= 1.0:
            raise ConditionError("PCTL 的分位参数应在 0~1 之间",
                                 position=node.position)
        return float(series.quantile(float(q)))
    if func in ("avg", "mean"):
        return float(series.mean())
    if func in ("std", "stdev"):
        return float(series.std(ddof=1))
    if func == "median":
        return float(series.median())
    if func == "min":
        return float(series.min())
    if func == "max":
        return float(series.max())
    raise ConditionError(f"未知截面函数 {node.func}()", position=node.position)


# ==================================================================
# 对外接口
# ==================================================================


@dataclass
class Condition:
    """已解析的条件表达式（可反复在任意截面上求值）。"""

    text: str
    ast: Node
    warnings: list[str] = field(default_factory=list)
    factors: tuple[str, ...] = ()

    # ---------- 求值 ----------

    def evaluate(self, frame: pd.DataFrame, *,
                 keep_unknown: bool = False,
                 ts_mode: bool = False) -> pd.Series:
        """在截面上求值，返回 index 与 frame 一致的布尔序列。

        keep_unknown=False（默认）：数据缺失（三值逻辑里的「未知」）判为**不入选**。
        keep_unknown=True：保留 pd.NA，便于上层自己处理。

        ts_mode=True：把 frame 当作**时间序列**（行=日期，列=因子）来求值 ——
        单股票回测用这个模式，此时只允许时序函数（MA/STD_TS/…），
        截面函数会报错（它们会引入未来数据）。
        """
        ctx = _EvalContext(frame=frame, factor_names=tuple(map(str, frame.columns)),
                           ts_mode=ts_mode)
        result = _eval(self.ast, ctx)
        if not _is_series(result):
            value = pd.NA if result is pd.NA else bool(result)
            result = pd.Series(value, index=frame.index, dtype="boolean")
        elif result.dtype != "boolean" and result.dtype != bool:
            # 数值表达式（如直接写 RANK(PE)）不是条件：直接报错，不猜"非0即真"。
            # 猜的话「PE」这种写法会静默变成"PE 非0"（几乎所有股票满足），
            # 选出的股票池看起来正常但完全不是用户的意思。
            raise ConditionError(
                f"条件必须返回布尔结果，而「{self.text}」返回的是数值。"
                f"请补上比较，例如 RANK(PE) > 50 或 PE > 0",
                position=0, text=self.text)
        mask = _as_boolean(result.reindex(frame.index))
        if keep_unknown:
            return mask
        return mask.fillna(False).astype(bool)

    def select(self, frame: pd.DataFrame) -> pd.DataFrame:
        """返回满足条件的子表（等价于 frame[self.evaluate(frame)]）。"""
        return frame.loc[self.evaluate(frame)]

    def explain(self, frame: pd.DataFrame) -> dict[str, Any]:
        """命中/未命中/未知的统计（前端展示 + 排查数据缺口用）。"""
        mask = self.evaluate(frame, keep_unknown=True)
        unknown = int(mask.isna().sum())
        selected = int((mask == True).sum())  # noqa: E712 可空布尔的显式比较
        return {
            "total": int(frame.shape[0]),
            "selected": selected,
            "rejected": int(frame.shape[0]) - selected - unknown,
            "unknown": unknown,
            "selected_ratio": (selected / frame.shape[0]) if frame.shape[0] else 0.0,
            "factors": list(self.factors),
            "warnings": list(self.warnings),
        }

    # ---------- 序列化 ----------

    def to_dict(self) -> dict[str, Any]:
        """AST → 纯 JSON（供 API 返回给前端做表达式高亮/拆解）。"""
        return {"text": self.text, "factors": list(self.factors),
                "warnings": list(self.warnings), "ast": _node_to_dict(self.ast)}


def _node_to_dict(node: Node) -> dict[str, Any]:
    if isinstance(node, Num):
        return {"type": "number", "value": node.value}
    if isinstance(node, Str):
        return {"type": "string", "value": node.value}
    if isinstance(node, Bool):
        return {"type": "bool", "value": node.value}
    if isinstance(node, FactorRef):
        return {"type": "factor", "name": node.name}
    if isinstance(node, Unary):
        return {"type": "unary", "op": node.op,
                "operand": _node_to_dict(node.operand)}
    if isinstance(node, Arith):
        return {"type": "arith", "op": node.op,
                "left": _node_to_dict(node.left),
                "right": _node_to_dict(node.right)}
    if isinstance(node, Compare):
        return {"type": "compare", "op": node.op,
                "left": _node_to_dict(node.left),
                "right": _node_to_dict(node.right)}
    if isinstance(node, Logical):
        return {"type": "logical", "op": node.op,
                "left": _node_to_dict(node.left),
                "right": _node_to_dict(node.right)}
    if isinstance(node, Not):
        return {"type": "not", "operand": _node_to_dict(node.operand)}
    if isinstance(node, Between):
        return {"type": "between", "negated": node.negated,
                "value": _node_to_dict(node.value),
                "low": _node_to_dict(node.low),
                "high": _node_to_dict(node.high)}
    if isinstance(node, InSet):
        return {"type": "in", "negated": node.negated,
                "value": _node_to_dict(node.value),
                "options": [_node_to_dict(option) for option in node.options]}
    if isinstance(node, Call):
        return {"type": "call", "func": node.func,
                "args": [_node_to_dict(arg) for arg in node.args]}
    return {"type": type(node).__name__}


def _collect_factors(node: Node, out: set[str]) -> None:
    if isinstance(node, FactorRef):
        out.add(node.name)
    elif isinstance(node, Call):
        for arg in node.args:
            _collect_factors(arg, out)
    elif isinstance(node, (Unary, Not)):
        _collect_factors(node.operand, out)
    elif isinstance(node, (Arith, Compare, Logical)):
        _collect_factors(node.left, out)
        _collect_factors(node.right, out)
    elif isinstance(node, Between):
        _collect_factors(node.value, out)
        _collect_factors(node.low, out)
        _collect_factors(node.high, out)
    elif isinstance(node, InSet):
        _collect_factors(node.value, out)
        for option in node.options:
            _collect_factors(option, out)


def _collect_calls(node: Node, out: set[str]) -> None:
    if isinstance(node, Call):
        out.add(node.func)
        for arg in node.args:
            _collect_calls(arg, out)
    elif isinstance(node, (Unary, Not)):
        _collect_calls(node.operand, out)
    elif isinstance(node, (Arith, Compare, Logical)):
        _collect_calls(node.left, out)
        _collect_calls(node.right, out)
    elif isinstance(node, Between):
        _collect_calls(node.value, out)
        _collect_calls(node.low, out)
        _collect_calls(node.high, out)
    elif isinstance(node, InSet):
        _collect_calls(node.value, out)
        for option in node.options:
            _collect_calls(option, out)


def _validate_ts_calls(node: Node) -> None:
    """解析期校验时序函数的参数（窗口必须是 1~2000 的整数字面量）。

    为什么要在解析期就查：窗口写错（`MA(close, 0)`、`MA(close)`、`MA(close, 99999)`）
    如果等到求值期才报，用户得先等面板装配几分钟（实测 20~30 秒起步）才知道自己
    参数写错了。这类错误完全可以静态判定。
    """
    if isinstance(node, Call) and node.func.lower() in _TS_FUNCS:
        func = node.func.lower()
        if len(node.args) != 2:
            raise ConditionError(
                f"{node.func}() 需要 2 个参数：(序列, 窗口天数)"
                + ("；COUNT_TS(条件, 窗口)" if func == "count_ts" else ""),
                position=node.position)
        window_node = node.args[1]
        if not isinstance(window_node, Num) or isinstance(window_node.value, bool):
            raise ConditionError(
                f"{node.func}() 的窗口必须是**常数**（如 20），不能是因子或表达式 —— "
                f"否则窗口会随行情变化，策略无法复现",
                position=node.position)
        window = float(window_node.value)
        if window != int(window) or not 1 <= int(window) <= 2000:
            raise ConditionError(
                f"{node.func}() 的窗口必须是 1~2000 的整数（收到 {window_node.value}）",
                position=node.position)
    for child in _children(node):
        _validate_ts_calls(child)


def _children(node: Node) -> list[Node]:
    """AST 子节点（用于通用遍历）。"""
    if isinstance(node, (Unary, Not)):
        return [node.operand]
    if isinstance(node, Arith):
        return [node.left, node.right]
    if isinstance(node, Compare):
        return [node.left, node.right]
    if isinstance(node, Logical):
        return [node.left, node.right]
    if isinstance(node, Between):
        return [node.value, node.low, node.high]
    if isinstance(node, InSet):
        return [node.value, *node.options]
    if isinstance(node, Call):
        return list(node.args)
    return []


def parse_condition(text: str, *, known_factors: list[str] | None = None,
                    known_columns: list[str] | None = None,
                    ts_mode: bool = False) -> Condition:
    """解析条件表达式。

    known_factors：可选的因子白名单（来自因子库）。给了就**立即**校验因子名，
    让前端在点"开始回测"之前就能报错，而不是等回测跑完才失败。
    known_columns：额外允许的列名（如 行业/代码 这类非因子列）。
    ts_mode=True（单股票时序模式）：把截面函数（RANK/PCTL/AVG/…）在**解析阶段**
    就拒掉。放到解析期而不是求值期报，是为了让错误出现在前端点"开始回测"之前 ——
    否则用户要等面板装配几分钟才知道自己写了个未来函数。
    """
    if text is None or not str(text).strip():
        raise ConditionError("条件表达式为空", position=0, text=str(text or ""))
    source = str(text).strip()
    parser = _Parser(tokenize(source), source)
    ast = parser.parse()

    used: set[str] = set()
    _collect_factors(ast, used)
    calls: set[str] = set()
    _collect_calls(ast, calls)
    known_calls = _CROSS_SECTION_FUNCS | _ELEMENT_FUNCS | _TS_FUNCS
    unknown_calls = sorted(c for c in calls if c.lower() not in known_calls)
    if unknown_calls:
        raise ConditionError(
            f"未知函数 {unknown_calls[0]}()。可用：RANK/PCTL/AVG/STD/MEDIAN/MIN/MAX/"
            f"COUNT/ABS/LOG/SQRT/SIGN（截面）；MA/STD_TS/SUM_TS/MAX_TS/MIN_TS/"
            f"PCTL_TS/ZSCORE_TS/REF/DELTA/COUNT_TS（时序）", position=0, text=source)
    _validate_ts_calls(ast)
    if ts_mode:
        forbidden = sorted(c for c in calls if c.lower() in _CROSS_SECTION_FUNCS)
        if forbidden:
            raise ConditionError(
                f"单股票回测不能用截面函数 {forbidden[0]}()：它的求值对象是整条序列，"
                f"`RANK(x)` 会拿今天和未来所有交易日一起排名 —— 是未来函数，"
                f"回测收益会凭空变好且无法复现。请改用时序版本："
                f"MA(x,n) / STD_TS(x,n) / PCTL_TS(x,n) / MAX_TS(x,n) / "
                f"MIN_TS(x,n) / ZSCORE_TS(x,n) / REF(x,n) / DELTA(x,n) / "
                f"COUNT_TS(cond,n)", position=0, text=source)
    if known_factors is not None:
        allowed = set(known_factors) | set(known_columns or [])
        unknown = sorted(name for name in used if name not in allowed)
        if unknown:
            raise ConditionError(
                f"未知因子 {unknown[0]!r}（共 {len(unknown)} 个未知："
                f"{', '.join(unknown[:5])}）。已知因子见 /factors 接口",
                position=0, text=source)
    warnings = list(parser.warnings)
    if ts_mode and not any(c.lower() in _TS_FUNCS for c in calls):
        warnings.append(
            "条件里没有用到任何时序函数（MA/STD_TS/…）：只比较因子当前值也可以，"
            "但单股票回测的样本量天然很小（一只票的历史只有几千个交易日），"
            "结论的统计置信度远低于全市场截面回测")
    return Condition(text=source, ast=ast, warnings=warnings,
                     factors=tuple(sorted(used)))
