"""★★★ `_planned_indicators` 必须是**已声明的 state channel** —— 真实端到端抓到的缺陷。

## 缺陷现场（2026-09-29 真实 LLM 端到端验收）

用户那条复合问：「…未来半年能否持有**高股息的招商银行**？」→ 系统答
「招商银行(600036)**无任何个股财务与股息数据**」「无个股 PE/PB 及行业均值数据」。

而**同一台机器上**实测这些数据全都取得到：

    PE(TTM):600036 = 6.76     PB:600036 = 0.9
    股息率TTM:600036 = 4.9606%（2026-09-28，4915 点）
    ROE / 资产负债率 / 每股净资产 均本地直读命中

## 根因：**未声明的键被 LangGraph 静默丢弃**

`supervisor_node` 做了问句信号增补（`_apply_query_signal_augmentation`
单独测会补 `PE(TTM):600036` / `stock_close:600036` / `ind:社会消费品零售总额同比`
等 13 条），也确实 `return {"_planned_indicators": indicators}` —— 但
`ResearchState` 的 schema 里**没有这个字段**，而 LangGraph 的 channel 只认
schema 声明过的键。于是：

    节点 return 的 `_planned_indicators`  →  丢弃
    `_collect_payload` 读到 None        →  回退 `plan_run()`（**不做增补**）
    → 实际只采基础模板那 17 条

## 为什么它能活到端到端才现形（三件事同时为真）

1. **Agent 补上了**（`plan` 是声明过的）→ A13/A14 真跑了，只是没有本行业指标
   → 结论表现为「采集数据中无科技行业关注指标…**跳过 LLM**」；
2. **指标没了**（本键未声明）→ A10/A11 只见宏观与大盘
   → 结论表现为「**无任何个股数据**」——**听起来像数据源的问题**；
3. **全程无异常**：没有 KeyError、没有 warning、`errors` 为空。

## 本文件的判据

- **① 结构**：`_planned_indicators` 必须在 `ResearchState.__annotations__` 里。
- **② 行为（核心）**：拿**真实** `ResearchState` 编译一个 `StateGraph`，
  节点返回该键 → 输出里必须读得到。**删掉那行声明，这条立刻红** ——
  它复现的就是 LangGraph 的丢弃语义本身，而不是我们的解读。
- **③ 对照**：同一个节点返回一个**故意未声明**的键 → 必须读不到。
  没有这条，②可能因为"其实所有键都会保留"而**空跑通过**（假绿）。
- **④ 耦合**：`supervisor.py` 里必须真的 return 这个键（两端都接上才算通）。
"""

from __future__ import annotations

from pathlib import Path

from langgraph.graph import END, StateGraph

from src.core.state import ResearchState

_ROOT = Path(__file__).resolve().parents[2]
_KEY = "_planned_indicators"
#: 故意**不**声明在 `ResearchState` 里的对照键
_UNDECLARED = "_definitely_not_a_declared_channel"


def _roundtrip(payload: dict) -> dict:
    """把 `payload` 从一个节点里 return 出去，返回图输出。"""
    graph = StateGraph(ResearchState)
    graph.add_node("emit", lambda _s: payload)
    graph.set_entry_point("emit")
    graph.add_edge("emit", END)
    return graph.compile().invoke({
        "task_id": "t", "tenant_id": "x", "user_query": "q",
        "analysis_type": "full", "target": "",
    })


# ============================================================
# ① 结构：schema 里必须有它
# ============================================================


def test_planned_indicators_is_declared_in_state_schema():
    """★ `ResearchState` 必须声明 `_planned_indicators`。

    这是**唯一**能让 supervisor 的规划结果活到采集节点的方式 ——
    LangGraph 不会为未声明的键建 channel。
    """
    assert _KEY in ResearchState.__annotations__, (
        f"`ResearchState` 缺少 `{_KEY}` 声明 ——\n"
        "supervisor 规划出的指标（以及问句信号增补出来的个股/行业指标）"
        "会在节点返回时被 **LangGraph 静默丢弃**，采集回退到基础模板。\n"
        "症状极具误导性：Agent 补上了、指标没了、errors 为空，"
        "结论写成「该股无任何个股数据」——看起来像数据源的问题。"
    )


# ============================================================
# ② 行为（核心）+ ③ 对照：证明"未声明的键会被丢"
# ============================================================


def test_declared_key_survives_a_real_stategraph_roundtrip():
    """★★★ 已声明的键必须穿过真实 `StateGraph(ResearchState)` 存活。

    这是缺陷本体的机器复现：**不是**断言"我们记得声明了"，
    而是让 LangGraph 自己走一遍。
    """
    out = _roundtrip({
        "plan": ["A08_macro"],
        _KEY: ["PE(TTM):600036", "us_cpi_yoy", "ind:社会消费品零售总额同比"],
    })
    assert out.get("plan") == ["A08_macro"], "对照组（plan）都不通，判据本身坏了"
    assert out.get(_KEY) == ["PE(TTM):600036", "us_cpi_yoy",
                             "ind:社会消费品零售总额同比"], (
        f"`{_KEY}` 没有活过 StateGraph —— 要么声明被删了，"
        "要么 LangGraph 的语义变了（两者都必须立刻处理）"
    )


def test_undeclared_key_is_dropped_as_control():
    """★ 对照：**故意未声明**的键必须读不到。

    没有这条，上一条可能因为"其实所有键都保留"而空跑通过 ——
    那它就不再是"声明有用"的证据了（本项目把头号失败模式定为假绿）。
    若这条**失败**，说明 LangGraph 不再丢弃未声明键 ⇒
    上一条的通过不再能证明什么，本文件的口径需要重写（而不是删掉这条）。
    """
    out = _roundtrip({"plan": ["A08_macro"], _UNDECLARED: ["x"]})
    assert _UNDECLARED not in out, (
        f"未声明的键 `{_UNDECLARED}` 竟然活下来了 —— LangGraph 的 channel 语义已变。\n"
        "此时 `test_declared_key_survives_a_real_stategraph_roundtrip` 不再是有效证据，"
        "必须重新确立判据（而不是删掉对照）。"
    )
    assert _UNDECLARED not in ResearchState.__annotations__


# ============================================================
# ④ 耦合：supervisor 必须真的写它
# ============================================================


def test_supervisor_actually_returns_the_key():
    """★ 两端都要接上：schema 有 channel、supervisor 真往里写。

    少了任何一端，"规划结果不生效"都会以另一种形式复发
    （本轮实测的另一半就是：两条规划路径都 return 了它，但 channel 不存在）。
    """
    src = (_ROOT / "src" / "orchestration" / "supervisor.py").read_text(
        encoding="utf-8")
    assert f'"{_KEY}"' in src, (
        f"`supervisor.py` 里没有任何地方写入 `{_KEY}` —— "
        "channel 声明好了却没人写，等于白声明"
    )
    # 采集侧必须读同一个键（写一处、读另一处 = 下一轮同类缺陷）
    assert f'state.get("{_KEY}")' in src, (
        f"采集节点没有读 `{_KEY}` —— 写入与读取必须是同一个键名"
    )
