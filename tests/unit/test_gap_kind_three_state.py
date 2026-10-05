"""三态豁免（`gap_kind`）+ 采集侧路径计数的判据（本轮新增）。

## 防的是什么（每条判据对应一种会静默变绿/变假的失效）

1. `test_three_gap_kinds_get_three_distinct_verdicts`
   —— 三种情形被判成同一件事（假阳性来源）。**自证 A 的靶子**。
2. `test_legacy_output_without_gap_kind_keeps_the_old_wording`
   —— 字段缺失把**历史数据**静默豁免掉；自造的 `gap_kind` 买到豁免。
3. `test_exemptions_are_visible_and_not_defects`
   —— 豁免静默消失（本项目纪律：不吓人 ≠ 不可见）。
4. `test_gap_kind_vocabulary_is_the_same_object_as_core`
   —— 取值在别处另抄一份 ⇒ 改一边不红（漂移）。
5. `test_collector_classifies_from_real_evidence`
   —— 分类器只认测试构造的输入、真实链路里的标记（类型会丢）认不出来。
6. `test_collection_path_counts_are_real_and_never_default_zero`
   —— 计数是**恒 0 的假字段**。**自证 B 的靶子**。
7. `test_guardrail_fields_separate_armed_from_stopped`
   —— 把「护栏拦下（慢/活太重）」与「源上就没有」混成一句"没取到"。
8. `test_audit_carries_collection_path_stats`
   —— 采集侧算了、审计侧读不到（判据接在没人走的路上）。

## 两处自证（真改源码跑红，见 `scripts/_prove_gap_kind_three_state.py`）

* **自证 A**：把三态分流去掉（回到"只看 `data_refs` 是否为空"）⇒ 判据 1 必红；
* **自证 B**：把路径计数恒置 0 ⇒ 判据 6 必红。

## 纪律

全部用**替身后端**（进程内），不发网络请求、不写生产 `data/`；
台账与路径计数是**进程级**状态 ⇒ 每条判据前 autouse 清空
（不清就会变成"看上一个用例跑了几次"）。
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest

from src.core.exceptions import (
    AgentExecutionError,
    DataFetchError,
    NoApplicableData,
)
from src.core.intel_limits import deadline_reason
from src.core.models import AgentInput
from src.core.schemas import DataPoint
from src.domain.agents.audit.verifier.agent import AuditAgent
from src.domain.agents.data.collector import gap_ledger, path_stats
from src.domain.agents.data.collector.agent import DataCollectorAgent
from src.domain.agents.data.collector.logic import (
    EXEMPT_GAP_KINDS,
    GAP_KIND_NOT_APPLICABLE,
    GAP_KIND_NOT_COVERED,
    format_gap_log,
)

A01 = "A01_data_collector"

# ============================================================
# 测试替身（全部进程内；判据不碰网络、不碰生产 data/）
# ============================================================


def _point(indicator: str, value: float = 1.0) -> DataPoint:
    return DataPoint(indicator=indicator, value=value, unit="%",
                     period_date="2026-09-01",
                     publish_time="2026-09-10T00:00:00Z",
                     source_name="stub")


class _BareBackend:
    """最小的取数替身：**什么都问不出来**（没有 `_matched` / `supports`）。"""

    def __init__(self, *, points=None, exc=None):
        self._points = list(points or [])
        self._exc = exc
        self.calls: list[tuple] = []

    async def fetch(self, indicator, start_date=None, end_date=None, **kwargs):
        self.calls.append((indicator, kwargs))
        if self._exc is not None:
            raise self._exc
        return list(self._points)

    def get_capabilities(self) -> dict:
        return {"name": "bare", "indicators": []}


class _MatchedBackend(_BareBackend):
    """有 `_matched()`（数得出候选路径条数）—— 模拟 `ConnectorRouter`。"""

    def __init__(self, *, paths: int = 1, **kw):
        super().__init__(**kw)
        self._paths = int(paths)

    def _matched(self, indicator):                      # noqa: ANN001, ANN202
        return [object()] * self._paths


class _SupportsOnlyBackend(_BareBackend):
    """只问得出"认不认"（认了也不许把条数写成 1）。"""

    def __init__(self, *, supports: bool = True, **kw):
        super().__init__(**kw)
        self._supports = supports

    def supports(self, indicator) -> bool:              # noqa: ANN001
        return bool(self._supports)


class _NoDeadlineBackend:
    """不认 `deadline_sec` 的后端（签名里没有它 ⇒ A01 走两参数那条路）。"""

    def __init__(self, *, points=None):
        self._points = list(points or [])

    async def fetch(self, indicator, start_date=None, end_date=None):
        return list(self._points)

    def get_capabilities(self) -> dict:
        return {"name": "no-deadline", "indicators": []}


def _ainput(payload: dict, task_id: str = "t_gap_kind") -> AgentInput:
    return AgentInput(task_id=task_id, tenant_id="tenant_001", payload=payload)


def _collect(backend, indicator: str, *, task_id: str = "t_gap_kind", **kw):
    """跑**生产实现** A01；异常按原样抛出（判据自己决定要不要接）。"""
    return asyncio.run(DataCollectorAgent(backend).execute(
        _ainput({"indicator": indicator, **kw}, task_id=task_id)))


def _audit_entry(indicator: str, kind: str | None, *, data_refs=None) -> dict:
    """审计产出的一条样本（形状照 `supervisor._summary`）。"""
    res: dict = {"indicator": indicator, "data_points": []}
    if kind is not None:
        res["gap_kind"] = kind
    return {"agent_id": A01, "agent_name": "数据采集Agent",
            "conclusion": f"未获取到 {indicator} 数据", "confidence": "low",
            "data_refs": list(data_refs or []), "result": res}


def _audit(outputs, tmp_dir: str, *, task_id: str = "t_gap_kind", seal: bool = False):
    return asyncio.run(AuditAgent().execute(_ainput({
        "trace_id": task_id, "agent_outputs": outputs,
        "chain_path": f"{tmp_dir}/chain.jsonl",
        "llm_audit_path": f"{tmp_dir}/none.jsonl",
        "seal_report": seal,
    }, task_id=task_id)))


@pytest.fixture(autouse=True)
def _clean_state():
    """每条判据都从冷启动态开始（计数/台账都是**进程级**的）。"""
    path_stats.reset_for_test()
    gap_ledger.reset_for_test()
    assert path_stats.snapshot()["latest_path"] == path_stats.UNMEASURED
    yield
    path_stats.reset_for_test()
    gap_ledger.reset_for_test()


# ============================================================
# ① 三种情形三种结论（**自证 A 的靶子**）
# ============================================================


def test_three_gap_kinds_get_three_distinct_verdicts(tmp_dir):
    """★ 豁免 / 豁免 / 缺陷 —— 三种含义必须给出三种结论，且**都定位到指标**。

    报障现场：审计面板上「银行没有流动比率」「这家公司不在质押表内」
    「连接器真的挂了」**长得一模一样**（都是 `无数据溯源引用`），
    而处置相反：前两种**不该联网硬试**（硬试只是烧钱），第三种才要去修链。

    自证 A 让这条变红的做法：把三态分流去掉（回到"只看 `data_refs` 是否为空"）
    ⇒ 前两条会掉进 `completeness_issues`、`exemptions` 变空。
    """
    cases = [("流动比率:600036", GAP_KIND_NOT_APPLICABLE),
             ("大股东质押比例:600036", GAP_KIND_NOT_COVERED),
             ("主线告警:600036", None)]

    # 逐条：同一种输入形态，三种不同结论（这是"三分"最直接的说法）
    verdicts = []
    for indicator, kind in cases:
        one = _audit([_audit_entry(indicator, kind)], tmp_dir)
        if one.result["completeness_issues"]:
            assert one.result["exemptions"] == []
            verdicts.append(f"缺陷:{one.result['completeness_issues'][0]}")
        else:
            assert one.result["exempt_count"] == 1, one.result
            verdicts.append(f"豁免:{one.result['exemptions'][0]['gap_kind']}")
    assert verdicts[0].startswith("豁免") and verdicts[1].startswith("豁免")
    assert verdicts[2].startswith("缺陷")
    assert verdicts[0] != verdicts[1], "不适用与未收录必须是两个取值（文案不同）"

    # 三条同时在场：缺陷只有真失败那一条，且文案与定位形状不变
    out = _audit([_audit_entry(i, k) for i, k in cases], tmp_dir)
    assert out.result["completeness_issues"] == [
        f"{A01}[主线告警:600036]: 无数据溯源引用"]
    assert out.result["exempt_count"] == 2
    assert {e["where"] for e in out.result["exemptions"]} == {
        f"{A01}[流动比率:600036]", f"{A01}[大股东质押比例:600036]"}


def test_exempt_only_outputs_still_pass_the_audit(tmp_dir):
    """★ 只有豁免时 `verdict` 必须是**通过** —— 否则豁免等于没生效。"""
    out = _audit([_audit_entry("流动比率:600036", GAP_KIND_NOT_APPLICABLE)], tmp_dir)
    assert out.result["completeness_issues"] == []
    assert out.result["verdict"] == "通过", out.result["completeness_issues"]


# ============================================================
# ② 老产出 / 自造取值：**不许**买到豁免
# ============================================================


def test_legacy_output_without_gap_kind_keeps_the_old_wording(tmp_dir):
    """★ 没有 `gap_kind` 的老产出 ⇒ **逐字**退回既有行为（防"历史数据被静默豁免"）。

    两条断言缺一不可：① 文案与改动前**完全相同**；② 它**不在**豁免清单里。
    只断①会漏掉"偷偷进了 exemptions"；只断②会漏掉"文案被顺手改了"。
    """
    legacy = _audit_entry("us_cpi_yoy:300068", None)
    legacy["result"].pop("gap_kind", None)
    out = _audit([legacy], tmp_dir)

    where = f"{A01}[us_cpi_yoy:300068]"
    assert out.result["completeness_issues"] == [f"{where}: 无数据溯源引用"]
    assert out.result["exemptions"] == [], "老产出被静默豁免了"
    assert out.result["verdict"] == "不通过"


@pytest.mark.parametrize("kind", ["empty", "NOT_APPLICABLE", "not-a-kind", "未收录"])
def test_unknown_gap_kind_never_buys_an_exemption(kind, tmp_dir):
    """★ **fail-closed**：取值不是豁免类（含拼错/近似值）⇒ 仍算缺陷。

    防的是"把判据写宽一点，所有失败就都很健康"—— 那是比假阳性严重的反向错误。
    同时这条把"为什么没豁免"写出来（否则读起来像判据失灵）。
    """
    out = _audit([_audit_entry("主线告警:600036", kind)], tmp_dir)
    issues = out.result["completeness_issues"]
    assert len(issues) == 1 and "无数据溯源引用" in issues[0], issues
    assert out.result["exemptions"] == []
    assert kind in issues[0], f"没被豁免时要把那个值报出来（可见）：{issues[0]}"


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_gap_kind_is_treated_as_missing(blank, tmp_dir):
    """空白 `gap_kind` 与"没有这个字段"同路（**不许**被当成豁免依据）。"""
    out = _audit([_audit_entry("主线告警:600036", blank)], tmp_dir)
    assert out.result["completeness_issues"] == [
        f"{A01}[主线告警:600036]: 无数据溯源引用"]
    assert out.result["exemptions"] == []


@pytest.mark.parametrize("bad", [["not_applicable"], 1, {"kind": "not_covered"}])
def test_broken_gap_kind_type_is_not_an_exemption(bad, tmp_dir):
    """`gap_kind` 不是字符串（列表/数字/嵌套）同样没有豁免依据。"""
    entry = _audit_entry("主线告警:600036", None)
    entry["result"]["gap_kind"] = bad
    out = _audit([entry], tmp_dir)
    assert out.result["exemptions"] == [], bad
    assert len(out.result["completeness_issues"]) == 1


# ============================================================
# ③ 豁免不是静默
# ============================================================


def test_exemptions_are_visible_and_not_defects(tmp_dir):
    """★ 豁免的两条**在返回值里仍然可见**，且**不是** defects。

    三处都要看得见：`result["exemptions"]`（逐条可定位）、
    结论里那一句（人读得到）、以及**封存上链**的记录（事后可核）。
    """
    out = _audit([
        _audit_entry("流动比率:600036", GAP_KIND_NOT_APPLICABLE),
        _audit_entry("对外担保占净资产比:600036", GAP_KIND_NOT_COVERED),
    ], tmp_dir, seal=True)

    assert out.result["exempt_count"] == 2
    assert all(e["where"].startswith(A01) for e in out.result["exemptions"])
    assert all("非缺陷" in e["note"] for e in out.result["exemptions"]), (
        "豁免必须带上『非缺陷』口径 —— 否则读者会以为它是被压下去的告警")
    assert "已豁免" in out.conclusion, out.conclusion
    assert out.result["completeness_issues"] == []

    assert out.result["sealed_seq"] is not None
    lines = [ln for ln in Path(f"{tmp_dir}/chain.jsonl").read_text(
        encoding="utf-8").splitlines() if ln.strip()]
    sealed = json.loads(lines[-1])["entry"]
    assert sealed["completeness_exemptions"], "封存记录里丢了豁免清单（事后核不到）"
    assert sealed["completeness_issues"] == []


# ============================================================
# ④ 取值同源（改一边必须红）
# ============================================================


def test_gap_kind_vocabulary_is_the_same_object_as_core():
    """★★ 取值**不是副本**：`EXEMPT_GAP_KINDS` 就是 `NoApplicableData.KINDS`。

    为什么用 `is` 断言而不是 `==`：`==` 对"抄了一份字面量"同样成立 ——
    而抄一份正是本项目最贵的缺陷形状（同一个 key 写在 3 处，只改一处）。
    `is` 只有在**同一个对象**时才通过：谁把它换成副本，这条当场红。

    另外逐条核对 kind 与**标记**的对应关系（标记是跨层可读的那一份：
    异常经路由器聚合后类型会丢，标记不会）—— 两处一起改才可能同时错。
    """
    assert EXEMPT_GAP_KINDS is NoApplicableData.KINDS
    markers = {kind: str(NoApplicableData("", kind=kind))
               for kind in NoApplicableData.KINDS}
    assert NoApplicableData.MARKER_NOT_APPLICABLE in markers[GAP_KIND_NOT_APPLICABLE]
    assert NoApplicableData.MARKER_NOT_COVERED in markers[GAP_KIND_NOT_COVERED]
    assert NoApplicableData.MARKER_NOT_COVERED not in markers[GAP_KIND_NOT_APPLICABLE]
    assert set(EXEMPT_GAP_KINDS) == {GAP_KIND_NOT_APPLICABLE, GAP_KIND_NOT_COVERED}


def test_gap_log_new_fields_are_optional():
    """日志新字段**不传就一行都不变**（既有 grep 契约与判据都按前缀断言）。"""
    assert (format_gap_log("X:1", "r", kind="error", seconds=1.23)
            == "[采集缺口] indicator=X:1 kind=error reason=r sec=1.2")
    line = format_gap_log(
        "X:1", "r", kind="error", gap_kind=GAP_KIND_NOT_APPLICABLE,
        paths={"paths_queried": 1, "paths_available": 2,
               "hit_path": path_stats.PATH_EXEMPT,
               "guardrail_armed": True, "guardrail_stopped": False})
    assert line.startswith("[采集缺口] indicator=X:1 kind=error reason=r")
    assert f"gap_kind={GAP_KIND_NOT_APPLICABLE}" in line
    assert "paths_available=2" in line and "hit_path=path1_exempt" in line
    assert "guardrail=armed" in line


# ============================================================
# ⑤ 采集侧分类器：真实链路里的证据（含"类型会丢"的那条路）
# ============================================================


def test_collector_classifies_from_real_evidence():
    """★ 三种情形的**证据形态**各不相同，分类器必须都认：

    * 结构化载体：`NoApplicableData(kind=...)` 原样传到 A01；
    * **类型已丢**：`ConnectorRouter` 把各源错误聚合成一条 `DataFetchError`
      （`NoApplicableData` 是它的子类，被 `except DataFetchError` 吃掉后
      只剩**标记文本**）—— 真实链路走的就是这一条；
    * 真失败（列名写错 / 源挂了）：**没有**豁免依据 ⇒ `gap_kind is None`。
    """
    # ① 结构化：not_applicable
    with pytest.raises(AgentExecutionError) as ei:
        _collect(_BareBackend(exc=NoApplicableData("银行没有流动比率",
                                                   kind="not_applicable")),
                 "流动比率:600036")
    assert ei.value.gap_kind == GAP_KIND_NOT_APPLICABLE
    assert ei.value.path_stats["hit_path"] == path_stats.PATH_EXEMPT

    # ② 结构化：not_covered
    with pytest.raises(AgentExecutionError) as ei:
        _collect(_BareBackend(exc=NoApplicableData("表里没有这只票",
                                                   kind="not_covered")),
                 "大股东质押比例:600036")
    assert ei.value.gap_kind == GAP_KIND_NOT_COVERED

    # ③ 类型已丢：路由器聚合后的 DataFetchError（只带标记 + AkShare 的措辞）
    aggregated = DataFetchError(
        "所有数据源获取 流动比率:600036 均失败: [akshare] 600036 的 '流动比率' "
        "在源表中**该实体无值**（已取到 12 期）")
    assert not isinstance(aggregated, NoApplicableData), "这条样本必须**不是**结构化载体"
    with pytest.raises(AgentExecutionError) as ei:
        _collect(_BareBackend(exc=aggregated), "流动比率:600036")
    assert ei.value.gap_kind == GAP_KIND_NOT_APPLICABLE, (
        "类型丢了就认不出来 ⇒ 最典型的那个现场（银行/流动比率）仍然会被报成缺陷")

    covered = DataFetchError(
        "所有数据源获取 对外担保占净资产比:600036 均失败: "
        f"[compliance] 不在担保快照内 —— {NoApplicableData.MARKER_NOT_COVERED}")
    with pytest.raises(AgentExecutionError) as ei:
        _collect(_BareBackend(exc=covered), "对外担保占净资产比:600036")
    assert ei.value.gap_kind == GAP_KIND_NOT_COVERED

    # ④ 真失败：**不许**编一个 kind 出来（换一个指标，别覆盖上面几条的记录）
    with pytest.raises(AgentExecutionError) as ei:
        _collect(_BareBackend(exc=DataFetchError("列名未命中，请改正映射表")),
                 "PE(TTM):600036")
    assert ei.value.gap_kind is None
    assert ei.value.path_stats["hit_path"] == path_stats.PATH_ERROR
    # 真失败也要落台账（审计侧看得到"为什么没取到"），但它**不是**豁免
    gaps = gap_ledger.defects("t_gap_kind", exempt_kinds=EXEMPT_GAP_KINDS)
    assert [g["indicator"] for g in gaps] == ["PE(TTM):600036"], gaps
    assert gaps[0]["gap_kind"] is None, "真失败不许带豁免 kind"
    # 四条缺口都进了台账，但**只有**真失败那条算缺陷（豁免与缺陷不许互相污染）
    assert gap_ledger.snapshot()["records"] == 4
    assert len(gap_ledger.exemptions(
        "t_gap_kind", exempt_kinds=EXEMPT_GAP_KINDS)) == 3


def test_same_indicator_gap_is_overwritten_not_duplicated():
    """同一指标重复取数（重试路径）⇒ 台账只留**最后一次**结论（不许重复计数）。"""
    gap_ledger.record(task_id="t_retry", indicator="CPI",
                      gap_kind=GAP_KIND_NOT_APPLICABLE, source=A01)
    gap_ledger.record(task_id="t_retry", indicator="CPI",
                      gap_kind=None, reason="重试后真失败", source=A01)
    assert gap_ledger.snapshot()["records"] == 1
    # 最后一次是"真失败" ⇒ 它**必须**按缺陷算（不许被上一次的豁免盖住）
    assert gap_ledger.exemptions("t_retry",
                                 exempt_kinds=EXEMPT_GAP_KINDS) == []
    assert [g["gap_kind"] for g in gap_ledger.defects(
        "t_retry", exempt_kinds=EXEMPT_GAP_KINDS)] == [None]


def test_exempt_gaps_are_ledgered_for_the_audit(tmp_dir):
    """★ 豁免类缺口**不产出 AgentOutput**（它是抛异常传达的）⇒ 台账是审计唯一的载体。

    没有这条，三态分流在真实链路上就是死代码（审计的输入里根本没有那两条）。
    """
    bad = NoApplicableData("表里没有这只票", kind="not_covered")
    with pytest.raises(AgentExecutionError):
        _collect(_MatchedBackend(exc=bad, paths=2), "大股东质押比例:600036",
                 task_id="t_ledger")

    out = _audit([], tmp_dir, task_id="t_ledger")     # 产出里**一条都没有**
    assert out.result["completeness_issues"] == []
    assert out.result["exempt_count"] == 1, out.result["exemptions"]
    item = out.result["exemptions"][0]
    assert item["where"] == f"{A01}[大股东质押比例:600036]"
    assert item["gap_kind"] == GAP_KIND_NOT_COVERED
    # ★ 路径计数随豁免一起到审计（B 项的落点）
    assert item["path_stats"]["hit_path"] == path_stats.PATH_EXEMPT
    assert item["path_stats"]["paths_available"] == 2


def test_ledger_is_bounded_and_reports_what_it_dropped():
    """台账有界（长跑进程不许被它撑大），且**丢了多少条必须报出来**。"""
    for i in range(gap_ledger.MAX_PER_TASK + 5):
        gap_ledger.record(task_id="t_big", indicator=f"ind:{i}",
                          gap_kind=None, source=A01)
    snap = gap_ledger.snapshot()
    assert snap["records"] == gap_ledger.MAX_PER_TASK
    assert snap["dropped"] == 5, f"挤掉的条数必须可见（不许静默丢）：{snap}"
    # 丢的是**最旧**的（保留最近的，便于复现最近一次故障）
    kept = gap_ledger.defects("t_big", exempt_kinds=EXEMPT_GAP_KINDS)
    assert kept[-1]["indicator"] == f"ind:{gap_ledger.MAX_PER_TASK + 4}"


# ============================================================
# ⑥ 路径计数是真的（**自证 B 的靶子**）
# ============================================================


def test_collection_path_counts_are_real_and_never_default_zero(caplog):
    """★★ 计数必须**跟着真实取数动**，且候选路径数**问不出来时是「未量到」不是 0**。

    ## 自证 B 让这条变红的做法

    把 `path_stats.record()` 改成"恒返回 0 / 不累加"（一个看起来接好了、
    其实是假字段的实现）⇒ 下面 `paths_available == 2`、
    `counters[PATH_BACKEND] == 1`、`latest_path == PATH_BACKEND` 全部变红。

    ## 为什么 `paths_available` 不许是 0

    0 读起来是"这个指标一条路都没有"（契约/登记缺失，该去补登记）；
    而"问不出来"是**观测缺口**（该去装仪表）。两者下一步动作相反 ——
    这正是本仓库「未量到 ≠ 0」那条硬约束在采集侧的具体落点。
    """
    with caplog.at_level(logging.INFO,
                         logger="src.domain.agents.data.collector.agent"):
        out = _collect(_MatchedBackend(points=[_point("CPI")], paths=2), "CPI",
                       task_id="t_paths")
        ok_lines = [r.message for r in caplog.records if "采集成功" in r.message]

    report = out.result["path_stats"]
    #: 先断**会变**的那个数（候选路径数）：恒 0 的假字段在这里最先露馅
    assert report["paths_available"] == 2, (
        f"候选路径数没跟着后端动（恒 0 的假字段？）：{report}")
    assert report["paths_queried"] == 1, "A01 边界内走过的是后端这一跳"
    assert report["hit_path"] == path_stats.PATH_BACKEND
    assert report["unmeasured"] == path_stats.UNMEASURED
    assert ok_lines, "成功行必须真的打出来（否则 caplog 断言的靶子不存在）"

    snap = path_stats.snapshot()
    assert snap["counters"][path_stats.PATH_BACKEND] == 1, snap
    assert snap["latest_path"] == path_stats.PATH_BACKEND
    assert snap["total"] == 1
    assert set(snap["counters"]) == set(path_stats.PATH_KINDS)

    # 空结果：**没有**豁免依据 ⇒ 记为"没量到"，不是豁免、也不是 0
    empty_report = _collect(_BareBackend(points=[]), "X:1",
                            task_id="t_paths").result["path_stats"]
    assert empty_report["hit_path"] == path_stats.PATH_EMPTY
    assert empty_report["paths_available"] == path_stats.UNMEASURED, (
        "问不出候选路径数时必须是「未量到」——写 0 等于断言『一条路都没有』")

    # 只问得出"认不认"：不认 ⇒ 0 条（这是**可证实**的 0，不是猜的）
    assert _collect(_SupportsOnlyBackend(points=[], supports=False), "X:1",
                    task_id="t_paths").result["path_stats"]["paths_available"] == 0
    # 认了但数不出条数 ⇒ 仍是「未量到」（不许把 ≥1 写成 1）
    assert _collect(_SupportsOnlyBackend(points=[], supports=True), "X:1",
                    task_id="t_paths").result["path_stats"]["paths_available"] \
        == path_stats.UNMEASURED

    # 四种结论各记各的（互斥，不是"都 +1"）
    counts = path_stats.snapshot()["counters"]
    assert counts[path_stats.PATH_BACKEND] == 1
    assert counts[path_stats.PATH_EMPTY] == 3
    assert counts[path_stats.PATH_ERROR] == 0 and counts[path_stats.PATH_EXEMPT] == 0
    assert path_stats.snapshot()["total"] == 4


def test_unknown_path_key_is_rejected_not_silently_ignored():
    """拼错的路径键**必须报错**：静默忽略会让它表现成"这条路永远 0 命中"。"""
    with pytest.raises(KeyError):
        path_stats.record("path1_backedn")          # 拼错一个字母
    assert path_stats.snapshot()["total"] == 0, "报错前不许留下半个计数"


# ============================================================
# ⑦ 护栏（防撞钟）："被拦下"与"源上没有"必须分得开
# ============================================================


def test_guardrail_fields_separate_armed_from_stopped():
    """★ 三种组合各给各的结论（不传 deadline = 定时/预热路径，行为不变）。"""
    # ① 交互路径 + 成功：护栏装上了，但它没拦（成功就是证据）
    out = _collect(_MatchedBackend(points=[_point("CPI")], paths=1), "CPI",
                   deadline_sec=10.0)
    assert out.result["path_stats"]["guardrail_armed"] is True
    assert out.result["path_stats"]["guardrail_stopped"] is False

    # ② 定时/预热路径（不传 deadline）：不受护栏约束
    out = _collect(_MatchedBackend(points=[_point("CPI")], paths=1), "CPI")
    assert out.result["path_stats"]["guardrail_armed"] is False

    # ③ 被防撞钟拦下：异常里带着**本次防撞钟自己的话术**（现算，不抄字面量）
    cut = DataFetchError("所有数据源获取 质押整表:600036 均失败: "
                         + deadline_reason("质押整表:600036", 10.0))
    with pytest.raises(AgentExecutionError) as ei:
        _collect(_MatchedBackend(exc=cut, paths=1), "质押整表:600036",
                 deadline_sec=10.0)
    assert ei.value.path_stats["guardrail_armed"] is True
    assert ei.value.path_stats["guardrail_stopped"] is True, (
        "被护栏拦下（慢/活太重）与源上就没有，下一步动作完全不同，不许混")
    assert ei.value.gap_kind is None, "护栏拦下**不是**豁免类缺口"
    assert ei.value.path_stats["hit_path"] == path_stats.PATH_ERROR

    # ④ 后端认不出 deadline ⇒ 护栏没装上（A01 走两参数那条路，取数行为同旧版）
    silent = _collect(_NoDeadlineBackend(points=[]), "X:1", deadline_sec=10.0)
    assert silent.result["path_stats"]["guardrail_armed"] is False
    assert path_stats.snapshot()["total"] == 4


# ============================================================
# ⑧ 采集侧算了 → 审计侧必须读得到（B 项的端到端落点）
# ============================================================


def test_audit_carries_collection_path_stats(tmp_dir):
    """★ 采集侧记下的路径计数必须**出现在审计返回值里**（不是只在日志里）。

    防的是"判据接在没人走的路上"：采集侧算得再准，审计读不到就等于没有。
    """
    boom = DataFetchError("所有数据源获取 主线告警:600036 均失败: [x] 502")
    with pytest.raises(AgentExecutionError):
        _collect(_MatchedBackend(exc=boom, paths=3), "主线告警:600036",
                 task_id="t_carry")

    out = _audit([], tmp_dir, task_id="t_carry")
    gaps = out.result["collection_gaps"]
    assert out.result["collection_gap_count"] == 1, gaps
    assert gaps[0]["indicator"] == "主线告警:600036"
    stats = gaps[0]["path_stats"]
    assert stats["paths_available"] == 3
    assert stats["hit_path"] == path_stats.PATH_ERROR
    # 真失败**不进**豁免清单（两条载本不许互相污染）
    assert out.result["exemptions"] == []


def test_audit_degrades_when_ledger_is_broken(tmp_dir, monkeypatch):
    """台账坏掉 ⇒ 审计**降级**（清单为空），不许把整次审计打挂。"""
    def _boom(*_a, **_k):
        raise RuntimeError("台账炸了")

    monkeypatch.setattr(gap_ledger, "exemptions", _boom)
    monkeypatch.setattr(gap_ledger, "defects", _boom)
    out = _audit([_audit_entry("CPI", None)], tmp_dir)
    assert out.result["exemptions"] == []
    assert out.result["collection_gaps"] == []
    assert len(out.result["completeness_issues"]) == 1, "降级不许顺手把缺陷也吞掉"
