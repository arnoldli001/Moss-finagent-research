"""★★ `_SIGNAL_AGENT_INDICATORS["stock"]` ↔ `_CODE_SUFFIX_INDICATORS` 的耦合护栏。

## 缺陷现场（2026-09-29 真实 LLM 端到端验收）

问句问的是「未来半年能否持有**高股息**的招商银行」，而修复前计划里
**一个股息类指标都没有** —— 系统答「招行高股息可持续性因个股估值/股息/财务
数据全缺，无法验证」。**问股息却不采股息。**

补指标时踩到的第二层坑（本文件守的就是它）：

    `_SIGNAL_AGENT_INDICATORS["stock"]` 里加了 `股息率TTM` / `ROE` /
    `商誉占净资产比` … 8 条，**但没加进 `_CODE_SUFFIX_INDICATORS`**。
    而 `_needs_code_suffix()` 只查后者 ⇒ 增补出来的名字**不带 6 位代码后缀**
    ⇒ A01 去 fetch 一个**没有任何连接器 supports** 的裸名 ⇒ 必然失败，
    用户看到的却是「该股没有股息数据」。

**这与 2026-09-26 那条 `PE(TTM)` 故障链是同一个形状**（裸个股指标 →
必然失败的 fetch → 伪装成"数据缺失"），区别只是这次裸名是我们自己新加的。

## 为什么本判据必须是**派生**的

手写"记得两边一起改"的清单**不会自己长大** —— 下一个新指标照漏。
所以这里不写清单，直接读代码里的两张表做交叉断言。

## 判据

1. `_SIGNAL_AGENT_INDICATORS["stock"]` 的**每个**指标都必须
   `_needs_code_suffix() == True`：它全是**个股类**，不带后缀就是必然失败；
2. 反向：`_CODE_SUFFIX_INDICATORS` 的每个裸名都必须在
   `configs/indicators.yaml` 里**能匹配到登记条目**（否则这条后缀规则指向
   一个不存在的指标 —— 与"登记了却取不到"是同一类静默缺口）；
3. 非空自证：两张表都不能是空集，否则上面两条会**空跑通过**（假绿）。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from src.orchestration.supervisor import (
    _CODE_SUFFIX_INDICATORS,
    _SIGNAL_AGENT_INDICATORS,
    _needs_code_suffix,
)

_ROOT = Path(__file__).resolve().parents[2]


def _registered_ids() -> list[str]:
    raw = yaml.safe_load(
        (_ROOT / "configs" / "indicators.yaml").read_text(encoding="utf-8"))
    out: list[str] = []

    def walk(node) -> None:
        if isinstance(node, list):
            for x in node:
                walk(x)
        elif isinstance(node, dict):
            if isinstance(node.get("id"), str):
                out.append(node["id"])
            for v in node.values():
                walk(v)

    walk(raw)
    return sorted(set(out))


def _stock_signal_indicators() -> tuple[str, ...]:
    """`_SIGNAL_AGENT_INDICATORS["stock"]` 里声明的**指标**（第 2 个元素）。"""
    _agents, indicators = _SIGNAL_AGENT_INDICATORS["stock"]
    return tuple(indicators)


# ============================================================
# ③ 非空自证（放在最前：没有它，下面两条都会空跑通过）
# ============================================================


def test_both_tables_are_non_trivial():
    """★ 自证：两张表都非空。任一为空 ⇒ 下面两条断言**空跑通过**（假绿）。"""
    assert _stock_signal_indicators(), (
        "`_SIGNAL_AGENT_INDICATORS['stock']` 的指标列表为空 —— "
        "本文件的判据会全部空跑通过"
    )
    assert _CODE_SUFFIX_INDICATORS, "`_CODE_SUFFIX_INDICATORS` 为空"


# ============================================================
# ① 正向：个股信号声明的每个指标都必须"需要代码后缀"
# ============================================================


def test_every_stock_signal_indicator_needs_a_code_suffix():
    """★★★ 个股信号里的指标**必须**都在 `_CODE_SUFFIX_INDICATORS` 里。

    否则它会被规划成**裸名**，而裸名没有任何连接器 `supports()` ——
    失败信息会伪装成"该股没有这项数据"。
    """
    missing = [i for i in _stock_signal_indicators()
               if not _needs_code_suffix(i)]
    assert not missing, (
        "以下指标由「stock」信号补进计划，却**不在** `_CODE_SUFFIX_INDICATORS` 里：\n"
        + "\n".join(f"  · {m}" for m in missing)
        + "\n→ 后果：它们会被加成**裸名**（如 `股息率TTM`），"
        "A01 去 fetch 一个没人 supports 的名字 → 必然失败，"
        "而结论写成「该股没有股息数据」。\n"
        "→ 修法：把这几个裸名加进 `_CODE_SUFFIX_INDICATORS`"
        "（**不是**加进别的清单 —— `_needs_code_suffix()` 只查那一张表）。"
    )


# ============================================================
# ② 反向：后缀规则指向的指标必须真实登记
# ============================================================


def test_code_suffix_indicators_are_registered():
    """★★ 反向：`_CODE_SUFFIX_INDICATORS` 的每个裸名都要能在登记表里匹配到。

    它同时守住另一件事：**这套"哪些指标是个股类"的知识必须与登记表一致**。
    指向一个不存在（或已改名）的指标 = 一条永远不生效的后缀规则。
    """
    registered = _registered_ids()
    assert registered, "indicators.yaml 一条都没读到 —— 路径或结构变了"
    unregistered = [
        bare for bare in sorted(_CODE_SUFFIX_INDICATORS)
        if not any(r == bare or r.startswith(f"{bare}:") for r in registered)
    ]
    assert not unregistered, (
        "以下裸名在 `_CODE_SUFFIX_INDICATORS` 里，却**在 `indicators.yaml` 里"
        "匹配不到登记条目**（后缀规则指向一个不存在的指标）：\n"
        + "\n".join(f"  · {u}" for u in unregistered)
        + "\n→ 要么补登记，要么从后缀规则里摘掉（两者选一个，别都不动）。"
    )


def test_a12_is_reachable_from_the_stock_signal():
    """★ 合规 Agent 必须由个股信号补挂 —— 否则它的本地规则永远拿不到输入。

    `compliance/logic.py` 的规则直接消费 `payload.data_points`；
    若 A12 不在计划里（或其指标没进计划），`evaluate_compliance()` 只能
    输出「未量到」。**"诚实报缺口"是对的，但缺口本身仍要补上。**
    """
    agents, _indicators = _SIGNAL_AGENT_INDICATORS["stock"]
    assert "A12_compliance" in agents, (
        "个股信号没有补挂 A12_compliance —— 合规的 5 个比率族不会进计划，"
        "A12 将永远输出「未量到」"
    )
