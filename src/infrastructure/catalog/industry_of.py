"""个股 → 所属行业：**本地行情仓的唯一只读入口**。

## 为什么需要它（用户 2026-09-29 报障）

> 「600036（招商银行属银行，非本行业消费）在数据缺口下无法给出明确持有结论：
>   **缺少银行 agent 吗？那就生成一个负责其他产业的 agent。做个兜底行业的 agent，
>   支持根据输入内容，动态注册对应行业 agent**」

兜底行业 Agent 要做的第一件事是：**确定性地说出"这只票属于哪个行业"**。
不能靠 LLM 猜，也不该靠"公司名里有没有行业字样"（`贵州茅台` 里没有"白酒"）。

本地行情仓的 `quant_stock_basic.industry` 就是权威答案（Tushare 名录）：
实测 `600036 → 银行`、`000001 → 银行`、`600519 → 白酒`、`300308 → 通信设备`，
全表 100+ 个行业名，5562 只 A 股全覆盖。

## 纪律

- **只读**：走 `data_stores.resolve_store("warehouse")`（registry 单一事实源）
  + `mode=ro`，**不写任何库、不硬编码路径**（本项目实测过：路径字面量会让
  "把库隔离到 tmp" 的测试仍写到真实仓库）。
- **拿不到就说拿不到**：返回 `""`，**绝不猜一个行业**（猜错会让兜底 Agent
  用错误的框架分析，比"没有结论"更糟）。
- **进程内缓存**：单次查询 ~0.3ms，但行业映射一天只变几次；加 TTL 避免
  每只票每轮都打一次库。
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time

logger = logging.getLogger(__name__)

#: 缓存 TTL（秒）—— 名录一天变不了几次，10 分钟足够，且失效代价只是再查一次。
_CACHE_TTL = 600

#: `{code: (industry, 写入时刻)}`；同时缓存"查过但没有"（空串），
#: 否则每次遇到不存在的代码都会重新打库（负缓存与正缓存同样重要）。
_CACHE: dict[str, tuple[str, float]] = {}

#: `(全部行业名集合, 写入时刻)` —— 给"按行业名反查"用（兜底 Agent 的最后一路）。
_VOCAB: tuple[frozenset[str], float] | None = None


def reset_cache() -> None:
    """清空进程内缓存（测试用；生产无需调用）。"""
    global _VOCAB
    _CACHE.clear()
    _VOCAB = None


def _connect() -> sqlite3.Connection | None:
    """打开行情仓（只读）。取不到就返回 None —— **不抛**，调用方按"拿不到"处理。"""
    from src.infrastructure.catalog import data_stores

    try:
        path = data_stores.resolve_store("warehouse")
    except Exception as exc:  # noqa: BLE001 registry 缺失/名字变更
        logger.debug("行业查询：registry 里取不到 warehouse 仓（%s）", exc)
        return None
    if not path.exists():
        logger.debug("行业查询：行情仓不存在 %s", path)
        return None
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        # 双保险：即便 DSN 被改，也不允许写。
        con.execute("PRAGMA query_only=1")
        return con
    except sqlite3.Error as exc:
        logger.debug("行业查询：行情仓打开失败 %s", exc)
        return None


def industry_of(code: str) -> str:
    """该 6 位代码所属行业；拿不到返回 `""`（**不猜**）。"""
    code = str(code or "").strip()
    if not (len(code) == 6 and code.isdigit()):
        return ""
    now = time.time()
    hit = _CACHE.get(code)
    if hit is not None and now - hit[1] < _CACHE_TTL:
        return hit[0]

    value = ""
    con = _connect()
    if con is not None:
        try:
            row = con.execute(
                "SELECT industry FROM quant_stock_basic WHERE code = ? LIMIT 1",
                (code,)).fetchone()
            if row is not None:
                value = str(row["industry"] or "").strip()
        except sqlite3.Error as exc:
            # 表不存在/列改名 → 如实记日志并当"拿不到"，**不猜**
            logger.debug("行业查询：读 quant_stock_basic 失败 %s", exc)
        finally:
            con.close()
    _CACHE[code] = (value, now)
    return value


def industry_vocabulary() -> frozenset[str]:
    """本地名录里出现过的**全部行业名**（给"文本里直接出现行业名"这一路用）。"""
    global _VOCAB
    now = time.time()
    if _VOCAB is not None and now - _VOCAB[1] < _CACHE_TTL:
        return _VOCAB[0]

    names: set[str] = set()
    con = _connect()
    if con is not None:
        try:
            rows = con.execute(
                "SELECT DISTINCT industry FROM quant_stock_basic "
                "WHERE industry IS NOT NULL AND industry != ''").fetchall()
            names = {str(r["industry"]).strip() for r in rows}
        except sqlite3.Error as exc:
            logger.debug("行业查询：读行业词表失败 %s", exc)
        finally:
            con.close()
    vocab = frozenset(n for n in names if n)
    _VOCAB = (vocab, now)
    return vocab


#: 6 位 A 股代码（裸码 / 600036.SH / sh600036 都能抠出来）
_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")


def resolve_industry_from_text(text: str) -> tuple[str, str]:
    """从**任意文本**（问句 + 标的）解析行业，返回 `(行业名, 依据说明)`。

    ## 这是**唯一实现**（编排层与兜底 Agent 共用）

    两处各写一套必然漂移，而漂移的表现是"编排层认为该挂兜底 Agent、
    Agent 自己却解析出另一个行业"—— 结论里的行业名与路由不一致，很难查。

    ## 三路，**确定性优先**（实测依据）

    1. 文本里的 **6 位代码** → `industry_of()`。最确定（Tushare 名录）。
    2. **股票简称** → `synonym_dict.resolve_entity()` → 代码 → 同上。
       为什么必须有这一路：`贵州茅台` 里**没有**"白酒"二字，
       只靠第 3 路会解析失败或误判。
    3. 文本里**直接出现行业名** → 拿本地 110 个行业名做**最长匹配**
       （避免 `银行` ⊂ `银行保险` 这类包含关系取错）。

    三路都不中 → `("", "")`。**绝不猜行业** —— 猜错会让下游用错误的框架分析，
    比"没有结论"更糟。
    """
    text = str(text or "")

    for code in _CODE_RE.findall(text):
        name = industry_of(code)
        if name:
            return name, f"个股代码 {code} → 本地名录（Tushare stock_basic）"

    try:
        from src.infrastructure.catalog.synonym_dict import resolve_entity

        hits = resolve_entity(text) if text else []
    except Exception as exc:  # noqa: BLE001 字典不可用不该拖垮行业解析
        logger.debug("行业解析：简称字典不可用 %s", exc)
        hits = []
    for code in hits[:3]:
        name = industry_of(code)
        if name:
            return name, f"简称解析 {code} → 本地名录"

    vocab = industry_vocabulary()
    matched = sorted((v for v in vocab if v and v in text), key=len, reverse=True)
    if matched:
        return matched[0], f"文本直述行业名「{matched[0]}」"

    return "", ""


__all__ = ["industry_of", "industry_vocabulary", "resolve_industry_from_text",
           "reset_cache"]
