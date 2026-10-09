"""A18 审计结论**必须被消费**的护栏（`CHG-0190` ① / PRD §46）。

## 缺陷本体（本轮实测）

`audit_node` 拿得到 `verdict="不通过"` / `completeness_issues` / 断链位置，但
**没有任何自动化消费方**：

- `grep` 全仓：`completeness_issues` 只命中 `agent.py` 自己 + `tests/` + `docs/`；
  `chain_valid` 只命中 `agent.py`；`不通过`（`src/**/*.py`）只命中 `agent.py:242`；
- `verdict="不通过"` **不是异常** ⇒ 不进 `errors`、不阻断交付、不重试、不告警、不计数；
- 对交付物的唯一影响是 A18 自己的 confidence 从 HIGH 降 MEDIUM。

⇒ **"审计能判不通过"与"不通过会改变交付"是两件事**，中间缺一个能被代码读的字段。

## 本文件的判据（四层，缺一层就是假绿）

1. **三态**：`通过` / `不通过` / **`未量到`** —— 审计异常时**绝不是"通过"**
   （本项目纪律：「没量到」≠「量到 0」；伪造的合格证比缺结论危险）。
2. **交付物可见**：不通过/未量到时报告**标题下方**必须有警告；通过时**一个字都不加**
   （否则警告天天出现，会退化成背景噪音）。
3. **channel 存活**：`audit` 必须声明在 `ResearchState` 且穿过真实 `StateGraph`
   （未声明的键会被 LangGraph 静默丢弃 —— `src/core/state.py:54-55` 记着这条教训），
   并配**对照**：故意未声明的键必须读不到。
4. **有出口**：任务记录与 `GET /research/{task_id}` 必须带 `audit`（否则它只活在内存里）。

判据写成"删掉实现就红"，而不是"我们记得做了"。
"""

from __future__ import annotations

import ast
from pathlib import Path

from langgraph.graph import END, StateGraph

from src.core.models import AgentOutput
from src.core.schemas import Confidence
from src.core.state import ResearchState
from src.orchestration.supervisor import (
    AUDIT_VERDICT_FAIL,
    AUDIT_VERDICT_PASS,
    AUDIT_VERDICT_UNMEASURED,
    _audit_banner,
    _audit_summary,
    _render_report,
)

_ROOT = Path(__file__).resolve().parents[2]
_SUPERVISOR = _ROOT / "src" / "orchestration" / "supervisor.py"
_RESEARCH_ROUTE = _ROOT / "src" / "api" / "routes" / "research.py"
_TASKS = _ROOT / "src" / "api" / "tasks.py"
#: 故意**不**声明在 `ResearchState` 里的对照键
_UNDECLARED = "_definitely_not_a_declared_channel"


def _audit_output(result: dict) -> AgentOutput:
    return AgentOutput(
        task_id="t", agent_id="A18_audit", conclusion="审计结论",
        confidence=Confidence.MEDIUM, result=result)


def _state() -> dict:
    return {
        "task_id": "t", "tenant_id": "x", "user_query": "q",
        "analysis_type": "full", "target": "600519", "target_display": "贵州茅台",
        "agent_outputs": [], "data_refs": [], "trace_ids": [], "errors": [],
        "progress": [], "agent_messages": [], "storage_stats": {},
    }


def _roundtrip(payload: dict) -> dict:
    graph = StateGraph(ResearchState)
    graph.add_node("emit", lambda _s: payload)
    graph.set_entry_point("emit")
    graph.add_edge("emit", END)
    return graph.compile().invoke({
        "task_id": "t", "tenant_id": "x", "user_query": "q",
        "analysis_type": "full", "target": "",
    })


# ======================================================================
# ① 三态口径
# ======================================================================


def test_audit_summary_three_states() -> None:
    """三态必须可分：通过 / 不通过 / **未量到**。"""
    passed = _audit_summary(_audit_output({"verdict": "通过", "chain_valid": True,
                                           "chain_count": 12}))
    assert passed["verdict"] == AUDIT_VERDICT_PASS and passed["failed"] is False

    failed = _audit_summary(_audit_output({
        "verdict": "不通过", "chain_valid": False, "chain_count": 12,
        "chain_broken_at": 7, "completeness_issues": ["A05_verifier: 缺少conclusion"],
    }))
    assert failed["verdict"] == AUDIT_VERDICT_FAIL and failed["failed"] is True
    assert failed["chain_broken_at"] == 7 and failed["issue_count"] == 1


def test_audit_not_run_is_unmeasured_not_pass() -> None:
    """★ 审计**没跑成** ⇒ `未量到`，**绝不许写成"通过"**（伪造合格证）。"""
    unmeasured = _audit_summary(None, error="A18_audit: boom")
    assert unmeasured["verdict"] == AUDIT_VERDICT_UNMEASURED
    assert unmeasured["failed"] is True, "没审过却被判为通过"
    assert unmeasured["verdict"] != AUDIT_VERDICT_PASS


def test_audit_output_without_verdict_is_unmeasured() -> None:
    """A18 返回了结果但**没有 verdict 字段**（契约被改坏）⇒ 也必须落到 `未量到`。"""
    summary = _audit_summary(_audit_output({"chain_valid": True}))
    assert summary["verdict"] == AUDIT_VERDICT_UNMEASURED


# ======================================================================
# ② 交付物可见（报告横幅）
# ======================================================================


def test_banner_on_failure_and_silence_on_pass() -> None:
    """不通过/未量到 ⇒ 报告标题下有警告；通过 ⇒ **一个字都不加**。"""
    assert _audit_banner({"verdict": AUDIT_VERDICT_PASS}) == []
    assert _audit_banner(None) == []

    fail_lines = _audit_banner({"verdict": AUDIT_VERDICT_FAIL, "issue_count": 3,
                                "chain_valid": False, "chain_broken_at": 7})
    assert any("审计未通过" in ln for ln in fail_lines)
    assert any("seq=7" in ln for ln in fail_lines), "断链位置没写出来"

    unmeasured = _audit_banner({"verdict": AUDIT_VERDICT_UNMEASURED,
                                "error": "A18_audit: boom"})
    assert any("未量到" in ln for ln in unmeasured)


def test_report_carries_the_banner() -> None:
    """横幅必须真的进 `final_report`（`_audit_banner` 被写好但没人调 = 没接）。"""
    output = _audit_output({"verdict": "不通过", "chain_valid": False,
                            "chain_broken_at": 7,
                            "completeness_issues": ["缺 conclusion"]})
    summary = _audit_summary(output)
    report = _render_report(_state(), output, summary)
    assert "审计未通过" in report
    # 位置：必须**在正文结论之前**（原先只在文末「## 审计」里，读到那里结论早看完了）
    assert report.index("审计未通过") < report.index("## 审计")

    passed = _audit_summary(_audit_output({"verdict": "通过", "chain_valid": True}))
    clean = _render_report(_state(), _audit_output({"verdict": "通过"}), passed)
    assert "审计未通过" not in clean and "审计未完成" not in clean


# ======================================================================
# ③ channel 存活（含对照）
# ======================================================================


def test_audit_is_declared_in_state_schema() -> None:
    assert "audit" in ResearchState.__annotations__, (
        "`ResearchState` 缺少 `audit` 声明 —— A18 的三态会被 LangGraph **静默丢弃**")


def test_audit_channel_survives_state_graph() -> None:
    out = _roundtrip({"audit": {"verdict": "不通过", "issue_count": 2}})
    assert out.get("audit", {}).get("verdict") == "不通过"


def test_undeclared_key_is_dropped_as_control() -> None:
    """对照：故意未声明的键必须读不到 —— 否则上一条可能因"所有键都保留"而空跑通过。"""
    out = _roundtrip({_UNDECLARED: {"x": 1}})
    assert _UNDECLARED not in out


# ======================================================================
# ④ 有出口：任务记录 + 任务详情接口
# ======================================================================


def test_task_record_declares_audit_and_self_heal_pending() -> None:
    src = _TASKS.read_text(encoding="utf-8")
    tree = ast.parse(src)
    model = next(n for n in ast.walk(tree)
                 if isinstance(n, ast.ClassDef) and n.name == "TaskRecord")
    fields = {t.target.id for t in model.body
              if isinstance(t, ast.AnnAssign) and isinstance(t.target, ast.Name)}
    assert "audit" in fields, "TaskRecord 没有 audit 字段 ⇒ 三态没有落盘出口"
    assert "self_heal_pending" in fields


def test_route_persists_and_exposes_audit() -> None:
    """`research.py` 必须**写**（store.update）并且**读**（任务详情返回体）都带上 audit。"""
    src = _RESEARCH_ROUTE.read_text(encoding="utf-8")
    assert "audit=final.get(\"audit\") or {}" in src, (
        "任务完成时没有把 audit 写进 TaskStore ⇒ 三态只活在内存里")
    assert "\"audit\": getattr(record, \"audit\", {}) or {}" in src, (
        "GET /research/{task_id} 没有返回 audit ⇒ 调用方看不到审计结论")
    # 缓存命中也要带（否则"缓存命中"的任务看起来像没审过）
    assert src.count("audit=final.get(\"audit\") or {}") >= 1
    assert src.count("\"audit\": final.get(\"audit\") or {}") >= 1


def test_audit_failure_is_logged_loudly() -> None:
    """不通过必须**出声**（管理员侧）；通过时不许刷屏。"""
    src = _SUPERVISOR.read_text(encoding="utf-8")
    assert "logger.warning(\n        \"★ A18 审计未通过" in src, (
        "审计未通过没有 warning 日志 ⇒ 链断了也没人知道（原先一行日志都没有）")
    assert "def _log_audit_verdict(" in src
