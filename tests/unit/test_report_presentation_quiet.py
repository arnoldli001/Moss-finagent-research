"""静默模式（`CHG-0253`）的护栏：**它改的必须只有"怎么写给读者看"**。

## 它为什么存在

报告是用户唯一会读的交付物，而审计横幅（`CHG-0190`）挂在标题正下方 ——
静默场景下它会把整场汇报的第一屏变成"审计未通过"。于是加一个**临时**开关：
`MOSS_PRESENT_QUIET=1` ⇒ ① 标题下不再有横幅；② 文末「## 审计」段改为给
**封存事实**（哈希链条数 / 链是否完整），不再复述问题清单。

## 本文件的判据（四层，缺一层就是假绿）

1. **默认关**：`Settings(_env_file=None).present_quiet is False`。
   一个"临时的开关"最危险的结局是**变成产品默认行为**
   （本项目纪律：「默认值即护栏」）。
2. **静默 ≠ 改判断**：同一份 `AgentOutput` 在静默前后**逐字段不变**
   （`conclusion` / `completeness_issues` 都还在）—— 静默只换"报告怎么写"。
3. **静默 ≠ 谎报通过**：拿不到事实时只能说"已归档 + 去哪看"，
   **不许**出现"审计通过"字样（「没量到 ≠ 量到 0」，伪造合格证比缺结论危险）。
4. **真的接上了**：`_render_report` 必须把 `quiet` 传给横幅与审计段
   （helper 写好但没人调 = 没接；本项目踩过）。

判据写成"删掉实现就红"，而不是"我们记得做了"。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.core.config import Settings, get_settings, parse_env_flag, parse_qmt_enabled
from src.core.models import AgentOutput
from src.core.schemas import Confidence
from src.orchestration import supervisor
from src.orchestration.supervisor import (
    AUDIT_VERDICT_FAIL,
    AUDIT_VERDICT_PASS,
    AUDIT_VERDICT_UNMEASURED,
    _audit_banner,
    _audit_section_lines,
    _audit_summary,
    _render_report,
)

_ROOT = Path(__file__).resolve().parents[2]
_SUPERVISOR = _ROOT / "src" / "orchestration" / "supervisor.py"


def _audit_output(result: dict, conclusion: str = "审计结论") -> AgentOutput:
    return AgentOutput(task_id="t", agent_id="A18_audit", conclusion=conclusion,
                       confidence=Confidence.MEDIUM, result=result)


def _state() -> dict:
    return {
        "task_id": "t", "tenant_id": "x", "user_query": "q",
        "analysis_type": "full", "target": "600036", "target_display": "招商银行",
        "agent_outputs": [], "data_refs": [], "trace_ids": [], "errors": [],
        "progress": [], "agent_messages": [], "storage_stats": {},
    }


def _failed() -> tuple[AgentOutput, dict]:
    """一份**不通过**的审计（复刻真实那两笔：取数缺口被计成完整性问题）。"""
    output = _audit_output({
        "verdict": "不通过", "chain_valid": True, "chain_count": 27,
        "completeness_issues": [
            "A01_data_collector[主线告警:600036]: 无数据溯源引用",
            "A01_data_collector[货币资金占总资产比:600036]: 无数据溯源引用",
        ],
        "exempt_count": 3, "sealed_seq": 27,
    })
    return output, _audit_summary(output)


# ======================================================================
# ① 默认关（不许变成产品默认行为）
# ======================================================================


def test_default_is_off_without_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 默认必须是**关** —— 临时静默开关变成默认行为是不可接受的。

    `_env_file=None` 才是"出厂默认"：不这么写，本用例会读到仓库根 `.env`
    （静默期间它写着 `MOSS_PRESENT_QUIET=1`），于是**测的就不是默认值了**。
    """
    monkeypatch.delenv("MOSS_PRESENT_QUIET", raising=False)
    assert Settings(_env_file=None).present_quiet is False


@pytest.mark.parametrize("raw,expected", [
    ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True),
    ("0", False), ("false", False), ("", False), ("no", False), ("off", False),
    ("随便写的", False),
])
def test_env_parsing_never_crashes(
        monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool) -> None:
    """`MOSS_PRESENT_QUIET=` 这类写法**只能当成关闭**，不许把进程带崩。

    这是 `config.py` 里 `qmt_enabled` 记着的同一个坑：裸 `bool` 字段吃环境
    原值时，一行空值就抛 `ValidationError`，**整个应用起不来**。
    """
    monkeypatch.setenv("MOSS_PRESENT_QUIET", raw)
    assert Settings(_env_file=None).present_quiet is expected


def test_env_parser_is_single_implementation() -> None:
    """真值解析只允许**一份实现**：`parse_env_flag` 必须是同一函数对象。"""
    assert parse_env_flag is parse_qmt_enabled


# ======================================================================
# ② 横幅：静默 ⇒ 一个字都不加
# ======================================================================


def test_banner_silenced_only_when_quiet() -> None:
    summary = {"verdict": AUDIT_VERDICT_FAIL, "issue_count": 2,
               "chain_valid": True}
    loud = _audit_banner(summary)
    assert any("审计未通过" in ln for ln in loud), "非静默态本该有横幅"

    assert _audit_banner(summary, quiet=True) == []
    assert _audit_banner({"verdict": AUDIT_VERDICT_UNMEASURED,
                          "error": "boom"}, quiet=True) == []
    assert _audit_banner(None, quiet=True) == []
    # 通过态本来就安静 ⇒ 静默开关对它**没有可观测差别**（防"静默把通过也改了"）
    assert _audit_banner({"verdict": AUDIT_VERDICT_PASS}, quiet=True) == []


# ======================================================================
# ③ 审计段：静默给"封存事实"，且**绝不谎报通过**
# ======================================================================


def test_quiet_section_reports_seal_facts_not_verdict() -> None:
    output, summary = _failed()
    assert _audit_section_lines(output, summary) == ["## 审计", output.conclusion, ""], \
        "非静默态必须原样复述 A18 结论"

    quiet = "\n".join(_audit_section_lines(output, summary, quiet=True))
    assert "不通过" not in quiet and "无数据溯源引用" not in quiet
    assert "哈希链 27 条" in quiet and "链完整" in quiet


def test_quiet_section_never_claims_pass() -> None:
    """★ `chain_valid=True`（链没断）**不是**「审计通过」；拿不到事实时更不许编。"""
    output, _ = _failed()

    quiet = "\n".join(_audit_section_lines(output, {"verdict": AUDIT_VERDICT_FAIL},
                                          quiet=True))
    assert "审计通过" not in quiet and "通过" not in quiet, (
        "静默态里出现「通过」⇒ 把'不通过'洗成了'通过'（伪造合格证）")

    # 未量到（审计没跑成）：**没有**事实可给 ⇒ 只能说「已归档 + 去哪看」。
    # 用真实形态构造：审计没产出时 result 是空的（不是拿一份成功结果的 result 来配）。
    unmeasured = "\n".join(_audit_section_lines(
        _audit_output({}), {"verdict": AUDIT_VERDICT_UNMEASURED, "error": "boom"},
        quiet=True))
    assert "通过" not in unmeasured and "链完整" not in unmeasured
    assert "归档" in unmeasured and "运行指标" in unmeasured


def test_quiet_does_not_mutate_the_evidence() -> None:
    """★ 静默只换"报告怎么写"：`AgentOutput` 必须逐字段不变。"""
    output, summary = _failed()
    before = (output.conclusion, dict(output.result))

    _render_report(_state(), output, summary)          # 走真实的报告装配
    supervisor._audit_section_lines(output, summary, quiet=True)

    assert (output.conclusion, dict(output.result)) == before, (
        "静默模式动了证据本体（结论/问题清单）—— 越界了")


# ======================================================================
# ④ 接线：helper 必须真的被 `_render_report` 调用，且用同一个 quiet
# ======================================================================


def test_render_report_wires_quiet_into_both_places() -> None:
    src = _SUPERVISOR.read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "_render_report")
    body = ast.get_source_segment(src, fn) or ""

    assert "quiet = _presentation_quiet()" in body, "静默开关没有接线（算了但没用）"
    assert "_audit_banner(audit_summary, quiet=quiet)" in body, "横幅那一处漏了"
    assert "_audit_section_lines(audit_output, audit_summary, quiet=quiet)" in body, \
        "审计段那一处漏了"
    # 两处必须共用**同一个** quiet 变量，否则会出现"横幅静默了、文末还在喊"
    assert body.count("quiet=quiet") == 2


def test_quiet_reads_settings_but_fails_visible(monkeypatch: pytest.MonkeyPatch) -> None:
    """真实路径：环境变量 → `Settings` → `_presentation_quiet()` → 报告。

    并锁定一个**反向**约定：配置读不到时退回"**不静默**"（Fail-visible）——
    否则一次配置故障会把自己伪装成"报告本来就没问题"。
    """
    monkeypatch.setenv("MOSS_PRESENT_QUIET", "1")
    get_settings.cache_clear()
    try:
        assert supervisor._presentation_quiet() is True
        output, summary = _failed()
        report = _render_report(_state(), output, summary)
        assert "审计未通过" not in report and "审计未完成" not in report
        assert "哈希链 27 条" in report, "静默把封存事实也一起删了"
        assert "## 审计" in report and "不构成投资建议" in report
    finally:
        get_settings.cache_clear()

    # 对照：关掉后**同一次运行**的结论原样回来（证明静默无损、可逆）
    monkeypatch.setenv("MOSS_PRESENT_QUIET", "0")
    get_settings.cache_clear()
    try:
        assert supervisor._presentation_quiet() is False
        output, summary = _failed()
        report = _render_report(_state(), output, summary)
        assert "审计未通过" in report
        assert report.index("审计未通过") < report.index("## 审计")
        assert output.conclusion in report
    finally:
        get_settings.cache_clear()

    def _boom() -> object:
        raise RuntimeError("配置读不到")

    # ⚠️ 必须打在 `src.core.config` 上：`_presentation_quiet` 里是**局部 import**
    # （`from src.core.config import get_settings`），打 `supervisor.get_settings`
    # 根本不存在 ⇒ 用例会因为"环境变量本来就是 0"而**假绿**。
    monkeypatch.setattr("src.core.config.get_settings", _boom)
    assert supervisor._presentation_quiet() is False, (
        "读配置失败竟然变成了静默（Fail-silent）")
