"""A18 审计Agent（哈希链校验 + 完整性检查）。

## 完整性检查为什么是**三分**而不是"有/没有"

`无数据溯源引用` 这一条原先**只看 `data_refs` 是否为空**，于是三种含义完全
不同的情形被判成同一件事，而它们的**处置相反**：

| 情形 | 采集侧怎么标 | 审计侧怎么处置 |
|---|---|---|
| 语义上不适用（银行没有"流动比率"） | `gap_kind=not_applicable` | **不算缺陷**，进豁免清单 |
| 专题表未收录该主体（窗口内没有质押公告） | `gap_kind=not_covered` | 同上 |
| 真的取数失败 | 无 `gap_kind`（或值不是豁免类） | **仍报** `无数据溯源引用` |

三条纪律：

1. **老产出（没有 `gap_kind`）退回既有行为**：字段缺失**永不**买来豁免
   —— 否则历史数据会被静默豁免掉（那比多报几条假阳性严重）；
2. **豁免不是静默**：豁免的两条仍然出现在返回值里（`exemptions`），
   并且**必须不是** `completeness_issues` 的成员（本项目纪律：不吓人 ≠ 不可见）；
3. **取值同源**：豁免类取值来自 `EXEMPT_GAP_KINDS`（**就是**
   `NoApplicableData.KINDS` 本身，不是副本），拼错/自造的 `gap_kind`
   **拿不到豁免**（fail-closed：写宽一点就能让所有失败"看起来很健康"是反向错误）。

## 两条载本（缺一条，三态分流就有一条路没人走）

* **产出里**的 `result["gap_kind"]` —— 上游（`supervisor._live_fetch_one`）
  以后让失败也留产出时立刻生效；
* **采集侧台账**（`gap_ledger`）—— 豁免类缺口今天**不产出 AgentOutput**
  （它是抛异常传达的），没有这条载本，审计的输入里**根本没有那两条**，
  豁免清单永远是空的 ⇒ 分流成了死代码。两条载本按指标合并去重。
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from src.core.base_agent import BaseAgent
from src.core.exceptions import AgentExecutionError
from src.core.models import AgentInput, AgentOutput
from src.core.schemas import Confidence, TraceStep
from src.domain.agents.data.collector import gap_ledger
from src.domain.agents.data.collector.logic import EXEMPT_GAP_KINDS, gap_note
from src.infrastructure.repositories.audit_chain import AuditChainWriter, ChainVerifier

logger = logging.getLogger(__name__)

#: 审计值里豁免清单最多列多少条（多了只报计数 —— 面板不该被一屏清单刷掉）。
EXEMPTIONS_MAX = 50


def _audit_file(name: str) -> str:
    """审计文件路径 —— 从 registry 的 `llm_audit` 派生（`CHG-0071`）。

    原先这里是两条写死的相对路径（`data/audit/llm_audit.jsonl` /
    `data/audit/audit_chain.jsonl`）—— 主实例与隔离档本来该落在**不同**目录，
    写死等于让隔离档的审计混进主实例的哈希链。
    """
    from src.infrastructure.catalog.data_stores import store_rel

    return store_rel("llm_audit") + "/" + name


class AuditPayload(BaseModel):
    """A18输入：本次投研任务的产出摘要。"""

    trace_id: str = ""
    agent_outputs: list[dict[str, Any]] = Field(default_factory=list)
    # 两条默认路径都从 registry 取（CHG-0071）。用 `default_factory` 而不是
    # 字面量：默认值必须在**实例化时**求值，否则隔离档会静默用主实例布局。
    llm_audit_path: str = Field(
        default_factory=lambda: _audit_file("llm_audit.jsonl"))
    chain_path: str = Field(
        default_factory=lambda: _audit_file("audit_chain.jsonl"))
    seal_report: bool = True
    """是否把本次审计结论追加进哈希链（封存）"""


class AuditAgent(BaseAgent):
    """审计校验（PRD A18，P0）：链完整性 + 产出完整性，结论封存上链。"""

    def __init__(self, agent_id: str = "A18_audit") -> None:
        super().__init__(agent_id)

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "capabilities": ["hash_chain_verify", "completeness_check", "seal_on_chain"],
        }

    def health_check(self) -> bool:
        return True

    def _parse_payload(self, payload: dict[str, Any]) -> AuditPayload:
        try:
            return AuditPayload.model_validate(payload)
        except ValidationError as exc:
            raise AgentExecutionError(f"{self.agent_id}输入不合法: {exc}") from exc

    # 信息层以上游文本为输入，合法产出可能为空（如0个事件），不强制数据溯源引用
    _REF_EXEMPT_AGENTS = {"A05_verifier", "A06_extractor", "A07_sentiment"}

    @staticmethod
    def _entry_gap_kind(entry: dict[str, Any]) -> str | None:
        """产出里**机器可读**的 `gap_kind`；没有/不是字符串 ⇒ `None`。

        `None` 的含义是"**没有豁免依据**"（老产出、真失败、值被写坏都一样）
        —— 调用方据此退回既有行为，不许把 `None` 读成"某种豁免"。
        """
        res = entry.get("result")
        if not isinstance(res, dict):
            return None
        kind = res.get("gap_kind")
        if isinstance(kind, str) and kind.strip():
            return kind.strip()
        return None

    def _split_completeness(
        self, outputs: list[dict[str, Any]], *, task_id: str = "",
    ) -> tuple[list[str], list[dict[str, Any]]]:
        """完整性检查（`CHG-0136` 的可定位性 + 本轮的三态分流）。

        返回 `(缺陷, 豁免)`。**唯一**实现 —— `_check_completeness()` 是它的
        兼容薄壳（只取缺陷那一半），既有调用点与判据不用改。

        ⚠️ 报障现场（`CHG-0136`）：审计面板显示 4 条一模一样的
        `A01_data_collector: 无数据溯源引用` —— **看不出是哪 4 条**
        （A01 的产出里本来就有 `result["indicator"]`，只是这条消息没用它）。
        审计的价值在于"能指向具体对象"：**每一条问题、每一条豁免都带指标名**。
        """
        issues: list[str] = []
        exemptions: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        for a in outputs:
            aid = a.get("agent_id", "?")
            #: 能定位就带上指标名（A01 的 result 里有）
            ind = ""
            res = a.get("result")
            if isinstance(res, dict):
                ind = str(res.get("indicator") or "")
            where = f"{aid}[{ind}]" if ind else aid
            if not a.get("conclusion"):
                issues.append(f"{where}: 缺少conclusion")
            if not a.get("confidence"):
                issues.append(f"{where}: 缺少confidence")
            if aid not in self._REF_EXEMPT_AGENTS and not a.get("data_refs"):
                kind = self._entry_gap_kind(a)
                if kind in EXEMPT_GAP_KINDS:
                    # ★ 豁免：不进 issues，但**必须留痕**（可见、可定位）。
                    exemptions.append({
                        "where": where, "agent_id": aid, "indicator": ind,
                        "gap_kind": kind, "note": gap_note(kind),
                        "path_stats": (res or {}).get("path_stats") or {},
                    })
                    seen.add((str(aid), ind))
                elif kind:
                    # ★ 值不是豁免类（写坏/自造）⇒ **拿不到豁免**（fail-closed），
                    #   并把"为什么没豁免"写出来（否则这条读起来像判据失灵）。
                    issues.append(
                        f"{where}: 无数据溯源引用"
                        f"（gap_kind={kind!r} 不是豁免类，仍算缺陷）")
                else:
                    # ★ 老产出/真失败：**逐字**退回既有行为。
                    issues.append(f"{where}: 无数据溯源引用")

        # ★ 第二条载本：采集侧台账里"豁免但不产出"的那两条（见模块 docstring）。
        if task_id:
            for rec in self._ledger_exemptions(task_id):
                key = (str(rec.get("source") or ""), str(rec.get("indicator") or ""))
                if key in seen:
                    continue          # 产出里已经报过 ⇒ 不重复列
                seen.add(key)
                ind = str(rec.get("indicator") or "")
                src = str(rec.get("source") or "") or "A01_data_collector"
                exemptions.append({
                    "where": f"{src}[{ind}]" if ind else src,
                    "agent_id": src, "indicator": ind,
                    "gap_kind": rec.get("gap_kind"), "note": gap_note(
                        str(rec.get("gap_kind") or "")),
                    "path_stats": rec.get("path_stats") or {},
                })
        return issues, exemptions

    def _check_completeness(self, outputs: list[dict[str, Any]]) -> list[str]:
        """只取缺陷那一半（**既有调用点/判据的兼容口**，判据仍只有一份）。"""
        return self._split_completeness(outputs)[0]

    @staticmethod
    def _ledger_exemptions(task_id: str) -> list[dict[str, Any]]:
        """采集侧台账里的豁免条目（读不到就按空处理，**不影响审计主链**）。"""
        try:
            return gap_ledger.exemptions(task_id, exempt_kinds=EXEMPT_GAP_KINDS)
        except Exception as exc:  # noqa: BLE001 观测坏了不该把审计打挂
            logger.debug("采集缺口台账（豁免）读取失败: %s", exc)
            return []

    def _collection_gaps(self, task_id: str) -> list[dict[str, Any]]:
        """采集侧台账里**不豁免**的那些缺口（真失败/没有依据）。

        它们不进 `completeness_issues`（那条判据的输入是产出，语义不变），
        但**如实列在审计结果里**：每条带 `path_stats`（查了几条路径、命中哪一条、
        护栏拦没拦）—— "为什么慢/为什么没取到"从此有数可查，不用靠猜。
        """
        try:
            return gap_ledger.defects(task_id, exempt_kinds=EXEMPT_GAP_KINDS)
        except Exception as exc:  # noqa: BLE001 台账读不到 ⇒ 不影响审计主链
            logger.debug("采集缺口台账读取失败: %s", exc)
            return []

    def _count_llm_calls(self, path: str, trace_id: str) -> int:
        from pathlib import Path

        p = Path(path)
        if not trace_id or not p.exists():
            return 0
        count = 0
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip() and f'"trace_id": "{trace_id}"' in line:
                count += 1
        return count

    async def execute(self, input: AgentInput) -> AgentOutput:
        payload = self._parse_payload(input.payload)

        chain = ChainVerifier(payload.chain_path)
        chain_result = chain.verify()
        # ★ 两条载本的载体键必须与采集侧**同一个** task_id（`supervisor` 里
        #   两者都取自 `state["task_id"]`）。取不到 trace_id 时退回 `input.task_id`。
        task_key = payload.trace_id or input.task_id
        issues, exemptions = self._split_completeness(
            payload.agent_outputs, task_id=task_key)
        collection_gaps = self._collection_gaps(task_key)
        llm_calls = self._count_llm_calls(payload.llm_audit_path, payload.trace_id)

        chain_note = (
            "完整" if chain_result["valid"]
            else f"于seq={chain_result['broken_at']}断链"
        )
        verdict = "通过" if chain_result["valid"] and not issues else "不通过"
        #: 豁免一项都不许静默：结论里**必须**报出条数（但**不**改变既有句子
        #: —— 没有豁免时这句话与加这个功能之前逐字一致）。
        exempt_note = (
            f"另有{len(exemptions)}项非缺陷缺口已豁免"
            f"（不适用/未收录，详见exemptions）。"
            if exemptions else ""
        )
        conclusion = (
            f"审计{verdict}：哈希链{chain_result['count']}条记录"
            f"{chain_note}，"
            f"产出完整性问题{len(issues)}项，LLM调用{llm_calls}次。"
            f"{exempt_note}"
        )

        sealed = None
        if payload.seal_report:
            sealed = AuditChainWriter(payload.chain_path).append({
                "kind": "research_audit",
                "trace_id": payload.trace_id,
                "task_id": input.task_id,
                "verdict": verdict,
                "chain_valid": chain_result["valid"],
                "completeness_issues": issues,
                "completeness_exemptions": exemptions,
                "llm_calls": llm_calls,
                "agent_ids": [a.get("agent_id", "?") for a in payload.agent_outputs],
            })

        return AgentOutput(
            task_id=input.task_id, agent_id=self.agent_id,
            conclusion=conclusion,
            confidence=Confidence.HIGH if verdict == "通过" else Confidence.MEDIUM,
            data_refs=[a.get("agent_id", "?") for a in payload.agent_outputs],
            trace_id=input.task_id,
            reasoning_steps=[
                TraceStep(step=1, step_type="cross_validation",
                          description=(
                              f"哈希链校验 {chain_result['count']} 条, "
                              f"断链位={chain_result['broken_at']}"
                          )),
                TraceStep(step=2, step_type="final_conclusion",
                          description=f"封存seq={sealed['seq']} head={sealed['record_hash'][:16]}"
                                      if sealed else "未封存（seal_report=False）"),
            ],
            result={
                "verdict": verdict,
                "chain_valid": chain_result["valid"],
                "chain_count": chain_result["count"],
                "chain_head": sealed["record_hash"] if sealed else chain_result["head"],
                "completeness_issues": issues,
                #: ★ 豁免清单：**不是** defects，但**必须在返回值里看得见**
                #: （每条带 `where` = `A01_data_collector[指标名]`，可定位）。
                "exemptions": exemptions[:EXEMPTIONS_MAX],
                "exempt_count": len(exemptions),
                #: ★ 采集侧如实登记的缺口（真失败/无依据），**每条带路径计数**：
                #: 查了几条候选路径、命中哪一条、护栏拦没拦。
                "collection_gaps": collection_gaps[:EXEMPTIONS_MAX],
                "collection_gap_count": len(collection_gaps),
                "llm_calls": llm_calls,
                "sealed_seq": sealed["seq"] if sealed else None,
                "sealed_hash": sealed["record_hash"] if sealed else None,
            },
        )
