"""证券代码与交易所归属的**唯一权威实现**。

## 为什么需要这个模块

同一条"代码 → 交易所"规则原先散在 5 处实现，其中 **3 处是已知错误版本**
（只判断 `6/9` 开头，漏了沪市 `5` 段），后果是把 588170（科创 50 ETF）
一类沪市 ETF 判成深市 —— 取数全空，且报错信息完全指不到原因。

放在 `src/core/` 是因为它是**唯一的底层包**（不被任何包反向依赖），
连接器、做T、量化都可以 import 而不产生跨层耦合。

## 归属规则

| 首位 | 市场 | 覆盖 |
|---|---|---|
| `5` | 沪市 | 51/56/58 ETF、500/510 基金 |
| `6` | 沪市 | 60 主板、601/603/605、688 科创板 |
| `9` | 沪市 | 900 B 股 |
| `0` / `2` / `3` | 深市 | 00 主板、002 中小板、30 创业板 |
| `1` | 深市 | 15/16 ETF、12 转债 |
| `4` / `8` / `920` | 北交所 | **暂不支持**（数据源未覆盖） |
"""

from __future__ import annotations

from typing import Final

#: 沪市代码首位
SH_FIRST_DIGITS: Final = ("5", "6", "9")
#: 北交所代码首位 / 首位组合（暂不支持）
BJ_PREFIXES: Final = ("4", "8", "920")
#: 场内 ETF 代码段（沪 51/56/58，深 15/16）
ETF_PREFIXES: Final = ("51", "56", "58", "15", "16")

CODE_LENGTH: Final = 6


class SymbolError(ValueError):
    """代码不合法，或落在当前不支持的市场。"""


def normalize(code: object) -> str:
    """去空白并校验为 6 位数字代码。不合法直接抛，不返回 None。"""
    text = str(code or "").strip()
    if len(text) != CODE_LENGTH or not text.isdigit():
        raise SymbolError(f"证券代码须为 {CODE_LENGTH} 位数字: {code!r}")
    return text


def market_of(code: object) -> str:
    """返回 `"sh"` / `"sz"`。北交所抛 `SymbolError`（数据源未覆盖）。"""
    text = normalize(code)
    if text.startswith(BJ_PREFIXES):
        raise SymbolError(f"北交所标的暂不支持（数据源未覆盖）: {text}")
    return "sh" if text.startswith(SH_FIRST_DIGITS) else "sz"


def exchange_symbol(code: object, *, style: str = "prefix") -> str:
    """带市场标识的符号。

    - `style="prefix"` → `sh600000`（腾讯 / 新浪 / 本项目内部统一格式）
    - `style="qmt"`    → `600000.SH`（迅投 QMT）
    - `style="plain"`  → `600000`（只做校验）
    """
    text = normalize(code)
    market = market_of(text)
    if style == "qmt":
        return f"{text}.{market.upper()}"
    if style == "plain":
        return text
    if style != "prefix":
        raise SymbolError(f"未知 style: {style!r}（可选 prefix / qmt / plain）")
    return f"{market}{text}"


def is_etf_code(code: object) -> bool:
    """是否为场内 ETF。"""
    try:
        return normalize(code).startswith(ETF_PREFIXES)
    except SymbolError:
        return False


__all__ = [
    "BJ_PREFIXES",
    "CODE_LENGTH",
    "ETF_PREFIXES",
    "SH_FIRST_DIGITS",
    "SymbolError",
    "exchange_symbol",
    "is_etf_code",
    "market_of",
    "normalize",
]
