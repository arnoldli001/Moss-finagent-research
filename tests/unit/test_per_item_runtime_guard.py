"""`CHG-0236`：多标的「必须返回等长逐只结果」的**运行时**校验判据。

## 这个文件在防什么

`CHG-0230`（A17 `per_subject`）/`CHG-0231`（A12 `compliance_by_code`）把
「每只票各一条」写进了 prompt —— 那是**契约层**。契约层只是**请求**：
模型完全可以只写一只、把两只合并成一条、写一个上一轮的代码、或者干脆
不写这个键，而 `parse_llm_json()` **只保证"是合法 JSON 对象"**
⇒ 这四种情况**全都静默通过**，用户看到"已逐只分析"的外观 + **少一只**的内容。

所以本文件的判据**不是**"契约里有没有那句话"（那已经有判据了），
而是「**模型真的少给一只时，输出里能看出来吗**」。

## 判据的分层（三层，缺一层就有洞）

  ① **助手层**（`enforce_per_item_rows`）：等长、保序、占位、留痕；
  ② **A17/A12 调用点**：真跑一次 Agent，断言输出里逐只结果等长；
  ③ **接线层**：`ast` 判据钉住"第二个调用点"（supervisor 的 ReAct 分支）——
     因为 **ReAct 才是多标的的生产路径**，它绕过 `A17.execute()`。

跑法：
    uv run python -m pytest tests/unit/test_per_item_runtime_guard.py -q
"""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

from src.core.models import AgentInput
from src.domain.agents.analysis.base import (
    PER_ITEM_MISSING,
    enforce_per_item_rows,
)
from src.domain.agents.decision.recommend.agent import RecommendationAgent

BANK = "002142"    # 宁波银行
COAL = "601088"    # 中国神华
_REPO = Path(__file__).resolve().parents[2]


def _ph() -> dict:
    return {"stance": PER_ITEM_MISSING, "position_advice": None}


def _run(data: dict, codes: list[str], **kw) -> dict:
    return enforce_per_item_rows(
        data, "per_subject", codes,
        where="test", label="标的",
        placeholder_fields=kw.get("placeholder_fields", _ph()),
        warning_prefix=kw.get("warning_prefix", "A17 未按标的逐只返回结论"),
    )


# ===========================================================================
# ① 助手层：等长、保序、占位、留痕
# ===========================================================================


def test_missing_row_is_padded_and_order_follows_the_question() -> None:
    """★ 少给一只 ⇒ **补空位**（不是丢掉、也不是拿另一只顶替）。

    为什么必须是"补"而不是"丢"：条数相等是下游与前端的前提 ——
    少一条时"第 i 条 = 第 i 只"这个约定**悄悄失效**，用户会按位置去读。
    """
    data = _run({"conclusion": "综合结论", "per_subject": [{"code": COAL, "stance": "看多"}]},
                [BANK, COAL])

    rows = data["per_subject"]
    assert [r["code"] for r in rows] == [BANK, COAL], "顺序必须跟用户提问顺序，而不是模型给的顺序"
    assert rows[0]["stance"] == PER_ITEM_MISSING and rows[0]["_missing"] is True, rows[0]
    assert rows[1]["stance"] == "看多", rows[1]
    assert data["per_subject_missing"] == [BANK], data
    assert data["per_subject_expected"] == 2 and data["per_subject_returned"] == 1, data
    # ★ 三处同时可见：占位行 + `*_missing` + 结论尾部的告警
    assert BANK in data["conclusion"] and PER_ITEM_MISSING in data["conclusion"], data["conclusion"]
    assert data["confidence"] == "low", "少给一只却不降置信度 = 让用户以为结论完整"


def test_equal_length_is_checked_by_code_not_by_count() -> None:
    """★★ **长度对得上但代码不对**必须判红 —— 这是"等长"这个说法的真正含义。

    反事实：如果判据只写 `len(rows) == len(codes)`，下面这个例子会**全绿**：
    模型给了 2 条，但其中一条是上一轮的代码（`000001`）—— 长度 2 == 2。
    于是"宁波银行"这条**根本没被回答**，而输出看起来是齐的。
    """
    data = _run({"per_subject": [{"code": COAL, "stance": "看多"},
                                 {"code": "000001", "stance": "看空"}]},
                [BANK, COAL])

    assert [r["code"] for r in data["per_subject"]] == [BANK, COAL], data["per_subject"]
    assert data["per_subject_missing"] == [BANK], data
    assert data["per_subject_unexpected"] == ["000001"], data
    assert data["per_subject"][0]["_missing"] is True, "宁波银行那一格必须是占位"


def test_key_absent_entirely_is_not_silently_ok() -> None:
    """模型压根不写这个键 ⇒ 全部占位，并且**留痕**（不是"看起来没有异常"）。"""
    data = _run({"conclusion": "我逐只给了结论"}, [BANK, COAL])

    assert data["per_subject_missing"] == [BANK, COAL], data
    assert data["per_subject_shape"] == "missing", data
    assert all(r["_missing"] for r in data["per_subject"]), data["per_subject"]
    assert data["conclusion"].startswith("我逐只给了结论"), "告警是**追加**，不许覆盖模型原文"


def test_happy_path_also_leaves_trace_that_the_guard_ran() -> None:
    """★ 全对时也要留痕：`*_checked`。

    没有它，"校验通过"与"压根没跑校验"在结果里**长得一模一样** ——
    这正是本项目 2026-09-28 踩过的"规则式路径不写审计"同款。
    """
    data = _run({"per_subject": [{"code": BANK, "stance": "看多"},
                                 {"code": COAL, "stance": "看空"}]},
                [BANK, COAL])

    assert data["per_subject_checked"] == "test", data
    assert data["per_subject_expected"] == data["per_subject_returned"] == 2, data
    assert "per_subject_missing" not in data and "per_subject_shape" not in data, data
    assert "confidence" not in data, "全对时不许动 confidence"


def test_single_target_is_byte_identical() -> None:
    """门槛是 **2**：单标的时**一个键都不加**（"单标的路径逐字不变"的纪律）。"""
    before = {"conclusion": "单标的结论", "stance": "中性"}
    data = _run(dict(before), [COAL])
    assert data == before, data


def test_dict_shape_is_accepted_but_recorded() -> None:
    """模型偶尔给 `{"601088": {...}}`：**接受但留痕**。

    不接受的话，一份内容其实齐全的输出会被整体判成"一只都没给"——
    那才是真正的误报（护栏本身制造假红）。
    """
    data = _run({"per_subject": {COAL: {"stance": "看多"}, BANK: {"stance": "看空"}}},
                [BANK, COAL])

    assert data["per_subject_shape"] == "dict", data
    assert [r["code"] for r in data["per_subject"]] == [BANK, COAL], data["per_subject"]
    assert "per_subject_missing" not in data, data
    assert data["per_subject"][1]["stance"] == "看多", data["per_subject"]


def test_duplicate_code_is_not_counted_twice() -> None:
    """同一只写两遍 ⇒ 第二条进 `_unexpected`，不许顶替另一只的空位。"""
    data = _run({"per_subject": [{"code": COAL, "stance": "看多"},
                                 {"code": COAL, "stance": "看空"}]},
                [BANK, COAL])

    assert data["per_subject_returned"] == 1, data
    assert data["per_subject_missing"] == [BANK], data
    assert data["per_subject_unexpected"] == [COAL], data
    assert data["per_subject"][1]["stance"] == "看多", "第一次出现的才是真值"


def test_malformed_row_counts_as_missing_and_is_recorded() -> None:
    """条目不是对象（模型返回字符串数组）⇒ 记 `_malformed`，该代码仍按缺失占位。"""
    data = _run({"per_subject": ["宁波银行看多", "中国神华看空"]}, [BANK, COAL])

    assert data["per_subject_malformed"] == 2, data
    assert data["per_subject_missing"] == [BANK, COAL], data


def test_market_suffix_is_normalized_before_matching() -> None:
    """`600036.SH` / `sh600036` 与 `600036` 是同一只票 —— 不归一化会**两头都错**
    （判成"该给的没给 + 给了没要的"）。"""
    data = _run({"per_subject": [{"code": f"{COAL}.SH", "stance": "看多"},
                                 {"stock_code": f"sh{BANK}", "stance": "看空"}]},
                [BANK, COAL])

    assert "per_subject_missing" not in data, data
    # ★ 归一化：带后缀/别名字段的写法在**输出**里也必须变成 6 位代码 ——
    #   否则下游按代码索引会**索引不到**（A17 的 cache anchor 就是这么索引的）。
    assert [r["code"] for r in data["per_subject"]] == [BANK, COAL], data["per_subject"]


def test_expected_codes_are_deduped_keeping_order() -> None:
    """期望里同一个代码出现两次（用户重复问）⇒ 期望也只有一条，不许补出两条。"""
    data = _run({"per_subject": [{"code": COAL, "stance": "看多"},
                                 {"code": BANK, "stance": "看空"}]},
                [BANK, COAL, BANK])

    assert data["per_subject_expected"] == 2, data
    assert len(data["per_subject"]) == 2, data["per_subject"]


# ===========================================================================
# ② A17 调用点：真跑 execute，断言输出
# ===========================================================================


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content
        self.model_used = "fake"
        self.cache_kind = "none"
        self.fallback_used = False


class _Gw:
    """最小替身：只回一次（不做 JSON 修复重试）。"""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls = 0

    async def complete(self, *_a, **_kw):  # noqa: ANN002, ANN003
        self.calls += 1
        return _Resp(json.dumps(self.payload, ensure_ascii=False))


def _a17_input(subjects: list[dict]) -> AgentInput:
    return AgentInput(
        task_id="t-guard", tenant_id="tenant_001", agent_id="A17_recommend",
        payload={
            "analyses": [{"agent_id": "A10_micro", "conclusion": "估值合理",
                          "confidence": "medium", "result": {}}],
            "focus": "中国神华",
            "user_query": "能否持有宁波银行和中国神华？",
            "hint": {},
            "subjects": subjects,
        },
    )


def _a17_run(payload: dict, subjects: list[dict]):
    gw = _Gw(payload)
    out = asyncio.run(RecommendationAgent(gw).execute(_a17_input(subjects)))
    return out, gw


def test_a17_multi_target_without_per_subject_gets_padded() -> None:
    """★★ 端到端：模型只回一个整体结论（**没有 per_subject**）时的实际输出。

    修复前：`result` 里没有 `per_subject` 这个键，`conclusion` 里可能还写着
    "两只票都值得关注" ⇒ 用户无从知道"逐只表态"这件事**没有发生**。
    """
    out, gw = _a17_run(
        {"conclusion": "两只票均可关注", "confidence": "medium", "stance": "中性"},
        [{"code": BANK, "name": "宁波银行"}, {"code": COAL, "name": "中国神华"}])

    assert gw.calls == 1, gw.calls
    assert [r["code"] for r in out.result["per_subject"]] == [BANK, COAL], out.result
    assert out.result["per_subject_missing"] == [BANK, COAL], out.result
    assert out.result["per_subject_checked"] == "A17_recommend/execute", out.result
    assert out.confidence.value == "low", out.confidence
    assert "逐只" in out.conclusion, out.conclusion


def test_a17_multi_target_with_one_subject_missing_flags_only_that_one() -> None:
    """给了一只、缺一只 ⇒ 告警**点名**缺的那只（不写成"逐只结果不完整"就完事）。"""
    out, _ = _a17_run(
        {"conclusion": "综合结论", "confidence": "high", "stance": "中性",
         "per_subject": [{"code": COAL, "name": "中国神华", "stance": "看多"}]},
        [{"code": BANK, "name": "宁波银行"}, {"code": COAL, "name": "中国神华"}])

    assert out.result["per_subject_missing"] == [BANK], out.result
    assert out.result["per_subject_returned"] == 1, out.result
    assert BANK in out.conclusion and COAL not in out.conclusion.split("⚠️")[-1], out.conclusion
    assert out.result["per_subject"][1]["stance"] == "看多", out.result


def test_a17_single_target_has_no_guard_keys() -> None:
    """★ 单标的回归护栏：**一个校验键都不许出现**（契约层也没加那段要求）。

    反事实：门槛若写成 `>= 1`，这条会红 —— 而它是"单标的逐字不变"的判据。
    """
    out, _ = _a17_run(
        {"conclusion": "单标的结论", "confidence": "high", "stance": "看多"},
        [{"code": COAL, "name": "中国神华"}])

    assert not [k for k in out.result if k.startswith("per_subject")], out.result


# ===========================================================================
# ②b A12 调用点：真跑 execute，断言逐只合规结论等长
# ===========================================================================


def _a12_run(reply: dict, *, codes: list[str]):
    from src.domain.agents.analysis import ComplianceAnalysisAgent
    from tests.unit.test_compliance_agent import FakeGateway, _dp, _make_input

    points = [_dp(f"大股东质押比例:{c}", 85.0) for c in codes]
    gw = FakeGateway(reply)
    out = asyncio.run(ComplianceAnalysisAgent(gw).execute(_make_input({
        "focus": "、".join(codes), "data_points": points, "events": [],
    })))
    return out, gw


def test_a12_multi_target_without_per_code_rows_gets_padded() -> None:
    """★★ 与 A17 同款：模型只给一个整体合规等级、**没有逐只结论**时。

    修复前：`compliance_by_code` 这个键**根本不存在**，而扁平字段
    `compliance_level` 会被读成"这两家公司"的等级 —— 但它是**汇总（取最坏）**。
    """
    out, gw = _a12_run({"conclusion": "两家公司合规风险可控", "confidence": "high",
                        "compliance_level": "无", "burst_risk": "未见明确爆雷路径",
                        "red_flags": [], "key_points": []},
                       codes=[BANK, COAL])

    assert len(gw.calls) == 1, "多标的必须调 LLM（_should_skip_llm 已拦）"
    rows = out.result["compliance_by_code"]
    assert [r["code"] for r in rows] == [BANK, COAL], rows
    assert out.result["compliance_by_code_missing"] == [BANK, COAL], out.result
    assert out.result["compliance_by_code_checked"] == "A12_compliance/_enrich_result", out.result
    assert all(r["level"] == "未量到" for r in rows), rows
    assert all("未返回" in r["burst_risk"] for r in rows), rows
    assert out.confidence.value == "low", out.confidence


def test_a12_partial_rows_are_completed_in_code_order() -> None:
    """只给了一只 ⇒ 另一只补占位、已给的原样保留（含它自己的旗标）。"""
    out, _ = _a12_run({"conclusion": "综合", "confidence": "medium",
                       "compliance_level": "高", "burst_risk": "质押平仓",
                       "red_flags": [], "key_points": [],
                       "compliance_by_code": [
                           {"code": COAL, "level": "无", "burst_risk": "未见",
                            "red_flags": []}]},
                      codes=[BANK, COAL])

    rows = out.result["compliance_by_code"]
    assert [r["code"] for r in rows] == [BANK, COAL], rows
    assert rows[0]["_missing"] is True and rows[0]["level"] == "未量到", rows[0]
    assert rows[1]["level"] == "无" and "_missing" not in rows[1], rows[1]
    assert out.result["compliance_by_code_missing"] == [BANK], out.result


def test_a12_rule_only_path_is_explicitly_skipped() -> None:
    """★★ 纯规则路径**不许**被要求给 `compliance_by_code`。

    `multi_target_guard` 那条分支**压根没调 LLM** —— 要求它给逐只 LLM 结论是
    **无中生有**；补出来的占位行会把"规则已逐只判定过"误标成"模型没返回"。
    （反事实：把 `not data.get("_rule_only")` 那个条件删掉，这条必红。）
    """
    from src.domain.agents.analysis import ComplianceAnalysisAgent
    from tests.unit.test_compliance_agent import FakeGateway, _dp, _make_input

    points = [_dp(f"大股东质押比例:{c}", 85.0) for c in (BANK, COAL)]
    agent = ComplianceAnalysisAgent(FakeGateway({}))
    payload = agent._parse_payload(  # noqa: SLF001
        _make_input({"focus": f"{BANK}、{COAL}", "data_points": points,
                     "events": []}).payload)
    agent._prepare(payload)  # noqa: SLF001
    data = agent._enrich_result(payload, {"_rule_only": True, "conclusion": "规则结论"})  # noqa: SLF001

    assert "compliance_by_code_checked" not in data, data
    assert data["compliance_per_code"], "逐只结果本身仍要下发（那是规则算的）"
    assert data["conclusion"] == "规则结论", data


def test_a12_single_target_has_no_guard_keys() -> None:
    """★ 单标的回归护栏：一个校验键都不许出现（`compliance_per_code` 仍有 1 条）。"""
    out, _ = _a12_run({"conclusion": "单标的结论", "confidence": "high",
                       "compliance_level": "无", "burst_risk": "未见明确爆雷路径",
                       "red_flags": [], "key_points": []},
                      codes=[COAL])

    assert not [k for k in out.result if k.startswith("compliance_by_code")], out.result
    assert len(out.result["compliance_per_code"]) == 1, out.result["compliance_per_code"]


# ===========================================================================
# ③ 接线层：ReAct 分支（**多标的真正的生产路径**）必须也接上
# ===========================================================================


def _supervisor_tree() -> ast.Module:
    return ast.parse((_REPO / "src" / "orchestration" / "supervisor.py").read_text("utf-8"))


def _find_nested(node: ast.AST, name: str) -> ast.AST | None:
    for child in ast.walk(node):
        if isinstance(child, ast.AsyncFunctionDef | ast.FunctionDef) and child.name == name:
            return child
    return None


def _calls(node: ast.AST, func_name: str) -> list[ast.Call]:
    out = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and (
            getattr(child.func, "id", None) == func_name
            or getattr(child.func, "attr", None) == func_name
        ):
            out.append(child)
    return out


def test_react_branch_calls_the_same_guard() -> None:
    """★★★ `recommend_node` 的 **ReAct 分支**必须调用同一个校验函数。

    ## 为什么这条判据必须有（而不是"多写一条注释"）

    `recommend_node` 有两条出口：
      · 薄分析单次直答 → 走 `A17.execute()` ⇒ 校验在 `execute()` 里；
      · **完整 ReAct**（多分析 + 可能追问上游）→ `react.run()` 直接返回
        `final_answer`，**绕过 `execute()`**。
    而用户那句「2 只票 + 板块」的请求，分析条数多、**走的就是 ReAct**。
    ⇒ 只在 `execute()` 里接，等于"改了但没生效"（本项目高频翻车点）。

    A17 的 ReAct 提示词把 `per_subject` 要求放在 **final_answer 内**，
    所以返回的 `data` 顶层就带 `per_subject` ⇒ 这里能校验。
    """
    tree = _supervisor_tree()
    node = _find_nested(tree, "recommend_node")
    assert node is not None, "supervisor 里找不到 recommend_node（改名了？判据要跟着改）"

    calls = _calls(node, "enforce_per_item_rows")
    assert calls, ("ReAct 分支没有调用 enforce_per_item_rows —— 多标的走这条路时"
                   "逐只等长校验**不会发生**")
    wheres = {
        kw.value.value for call in calls for kw in call.keywords
        if kw.arg == "where" and isinstance(kw.value, ast.Constant)
    }
    assert "A17_recommend/supervisor-react" in wheres, wheres
    assert any(
        isinstance(kw.value, ast.Constant) and kw.value.value == "per_subject"
        for call in calls for kw in call.keywords if kw.arg is None
    ) or any(
        call.args and isinstance(call.args[1], ast.Constant)
        and call.args[1].value == "per_subject" for call in calls
    ), "校验的键不是 per_subject"


def test_guard_has_exactly_one_implementation() -> None:
    """★ 「同一判断只允许一份实现」：全仓只有一处 `def enforce_per_item_rows`。

    它是 A17 与 A12 共用的判据。若哪天有人各写一份，形状会再次漂移
    （本项目刚修过同款：A10 的 dict 与 A12 的 list 形状不一致）。
    """
    hits = [
        path for path in (_REPO / "src").rglob("*.py")
        if "def enforce_per_item_rows" in path.read_text("utf-8")
    ]
    assert [p.relative_to(_REPO).as_posix() for p in hits] == [
        "src/domain/agents/analysis/base.py"], hits


def test_both_call_sites_exist() -> None:
    """A17（`execute`）与 A12（`_enrich_result`）两个调用点都必须真的在。"""
    a17 = (_REPO / "src/domain/agents/decision/recommend/agent.py").read_text("utf-8")
    a12 = (_REPO / "src/domain/agents/analysis/compliance/agent.py").read_text("utf-8")
    assert "enforce_per_item_rows(" in a17, "A17 没接运行时校验"
    assert "enforce_per_item_rows(" in a12, "A12 没接运行时校验"
    assert "per_subject" in a17 and "compliance_by_code" in a12
