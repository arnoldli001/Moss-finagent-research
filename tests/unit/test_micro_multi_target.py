"""A10_micro 的**多标的**估值判定（`CHG-0219`）。

## 修的是什么（来自 §41.36 的审计，属"**给错答案**"而不是"缺数据"）

用户报障问句里有两票（宁波银行 + 中国神华），而 A10 的本地估值是 **code-blind** 的：

    `_find_value(payload, "PE")`     → 子串匹配**跨代码**，取 `period_date` 最新的一条
                                       ⇒ 拿到的可能是**另一只票**的 PE
    `_segment_series(payload, "PE(TTM)")` → 只看冒号段、**完全无视代码后缀**
                                       ⇒ 两票的 PE 历史被**并成一条序列**再算分位
    `_prepare`                       → 只产出**一个** `valuation_calc`
    `_requirements`                  → 明令"估值结论须与 valuation_calc 一致"

⇒ 用户读到「宁波银行估值合理」，**数字其实来自中国神华**。
★ 这比缺数据更危险：**缺数据用户看得出来，错数看不出来。**

## 本文件守的四件事

1. ★★ **每一票各自算一份**，且**值来自各自的代码**（不是混合出来的）；
2. ★★ **历史分位按代码分开算** —— 这是最隐蔽的一条：分位在两票并集上算，
   那个数**既不是 A 的、也不是 B 的**；
3. ★ **修复前的错法可复现**（把失败模式钉下来，防止悄悄回退）；
4. ★ **回归护栏**：单标的路径与修复前**逐字一致**（连调用方式都没变）。

跑法：
    uv run python -m pytest tests/unit/test_micro_multi_target.py -q
"""
from __future__ import annotations

from src.domain.agents.analysis.micro.agent import (
    MicroAnalysisAgent,
    _code_of,
    _find_value,
    _per_stock_codes,
    _segment_series,
)
from tests.unit.test_analysis_agents import (  # noqa: F401 复用既有替身与构造器
    MACRO_REPLY,
    FakeGateway,
    _dp,
    _make_input,
)

BANK = "002142"   # 宁波银行
COAL = "601088"   # 中国神华


def _series(code: str, segment: str, values: list[float]) -> list[dict]:
    return [_dp(f"{segment}:{code}", v, f"2024-{i + 1:02d}")
            for i, v in enumerate(values)]


async def _run(points: list[dict], **extra):
    gw = FakeGateway(MACRO_REPLY)
    out = await MicroAnalysisAgent(gw).execute(
        _make_input({"data_points": points, **extra}))
    return out, gw


def _by_code(result: dict) -> dict[str, dict]:
    """★ `CHG-0235`：`valuation_calc_by_code` 已统一成 **list**（与 A12 对齐）。

    判据里按代码取行时统一走这个助手 —— 免得以后形状再变时要改十几处断言。
    """
    rows = result.get("valuation_calc_by_code") or []
    assert isinstance(rows, list), f"形状应为 list（与 A12 一致）：{type(rows)}"
    return {str(r.get("code")): r for r in rows}


# ===========================================================================
# ① 取数层：代码后缀要真的被用上
# ===========================================================================


def test_code_of_parses_the_tail() -> None:
    assert _code_of("PE(TTM):601088") == "601088"
    assert _code_of("PB:002142") == "002142"
    assert _code_of("industry_pe") == ""
    assert _code_of("") == ""


def test_find_value_filters_by_code() -> None:
    """★ 给了代码就只在该代码的点里找（`code=""` 才是修复前的跨代码行为）。"""
    payload = type("P", (), {"data_points": [
        _dp("PE(TTM):601088", 12.0, "2026-08"),
        _dp("PE(TTM):002142", 6.0, "2026-09"),      # 期更晚
    ]})()
    assert _find_value(payload, "PE", code=COAL) == 12.0
    assert _find_value(payload, "PE", code=BANK) == 6.0
    # ★ 不传 code = 修复前的行为：跨代码取最新期 ⇒ **拿到的是另一只票的值**
    assert _find_value(payload, "PE") == 6.0, (
        "这条不是在测修复后的行为，而是把**修复前的错法**钉下来")


def test_segment_series_does_not_mix_codes() -> None:
    """★★ 最隐蔽的一条：分位的**分母**不许是两票的并集。"""
    payload = type("P", (), {"data_points": [
        *_series(COAL, "PE(TTM)", [10.0, 11.0, 12.0]),
        *_series(BANK, "PE(TTM)", [5.0, 6.0, 7.0]),
    ]})()
    assert _segment_series(payload, "PE(TTM)", COAL) == [10.0, 11.0, 12.0]
    assert _segment_series(payload, "PE(TTM)", BANK) == [5.0, 6.0, 7.0]
    # ★ 修复前：并成一条（6 个点）⇒ 分位与哪只票都对不上
    assert _segment_series(payload, "PE(TTM)") == [5.0, 6.0, 7.0, 10.0, 11.0, 12.0]


def test_per_stock_codes_preserves_first_seen_order() -> None:
    payload = type("P", (), {"data_points": [
        _dp(f"PE(TTM):{COAL}", 12.0), _dp(f"PB:{BANK}", 1.0),
        _dp(f"PE(TTM):{COAL}", 13.0),               # 重复不重复计
        _dp("industry_pe", 20.0),                   # 行业级不算
    ]})()
    assert _per_stock_codes(payload) == [COAL, BANK]


# ===========================================================================
# ② 判定层：逐票各一份，且**值来自各自的代码**
# ===========================================================================


async def test_each_stock_gets_its_own_valuation() -> None:
    """★★★ 本文件最重要的一条：两票各一份，值不许串。"""
    out, _gw = await _run([
        _dp(f"PE(TTM):{COAL}", 12.0, "2026-08"), _dp(f"PB:{COAL}", 1.2, "2026-08"),
        _dp(f"PE(TTM):{BANK}", 6.0, "2026-09"), _dp(f"PB:{BANK}", 0.7, "2026-09"),
        _dp("industry_pe", 10.0, "2026-09"), _dp("industry_pb", 1.0, "2026-09"),
    ])
    by = _by_code(out.result)
    assert set(by) == {COAL, BANK}, by
    # 中国神华 PE 12 > 行业 10 ⇒ 高估；宁波银行 PE 6 < 10 且 PB 0.7 < 1 ⇒ 低估
    assert by[COAL]["valuation"] == "高估", by[COAL]
    assert by[BANK]["valuation"] == "低估", by[BANK]
    assert "12.0" in by[COAL]["detail"] and "6.0" in by[BANK]["detail"], by


async def test_single_authoritative_value_does_not_pretend_to_be_per_stock() -> None:
    """★ 单值字段**不再冒充一个跨票的权威结论**（形状保留，下游不会崩）。"""
    out, _gw = await _run([
        _dp(f"PE(TTM):{COAL}", 12.0), _dp(f"PB:{COAL}", 1.2),
        _dp(f"PE(TTM):{BANK}", 6.0), _dp(f"PB:{BANK}", 0.7),
    ])
    calc = out.result["valuation_calc"]
    assert calc["basis"] == "per_code", calc
    assert "多标的" in calc["valuation"], calc
    assert COAL in calc["detail"] and BANK in calc["detail"], calc


async def test_percentile_is_computed_per_code_not_on_the_union() -> None:
    """★★ 分位必须按各自序列算。

    构造：两票各 12 期，**并集**看两票的最新值都在中间；但**各自序列**里
    中国神华的最新值是最高的（偏高）、宁波银行的是最低的（低位）。
    若分母是并集，两条结论都会被算成"历史区间内" ⇒ 这条会红。
    """
    coal = _series(COAL, "PE(TTM)", [float(8 + i) for i in range(11)] + [30.0])
    bank = _series(BANK, "PE(TTM)", [float(40 - i) for i in range(11)] + [3.0])
    out, _gw = await _run(coal + bank)
    by = _by_code(out.result)
    assert by[COAL]["basis"] == "自身历史分位", by[COAL]
    assert by[COAL]["pe_percentile"] == 100.0, by[COAL]     # 30 是它自己序列里最高
    assert by[BANK]["pe_percentile"] == 8.3 or by[BANK]["pe_percentile"] <= 10, by[BANK]
    assert by[COAL]["valuation"] == "历史偏高", by[COAL]
    assert by[BANK]["valuation"] == "历史低位", by[BANK]


async def test_requirements_demand_per_stock_when_multi() -> None:
    """★ prompt 必须**明令逐票**，否则模型只会挑一只写（这是"结论出不来"的入口）。"""
    out, gw = await _run([
        _dp(f"PE(TTM):{COAL}", 12.0), _dp(f"PB:{COAL}", 1.2),
        _dp(f"PE(TTM):{BANK}", 6.0), _dp(f"PB:{BANK}", 0.7),
    ])
    prompt = gw.calls[0]["prompt"]
    assert "逐票" in prompt and "多标的" in prompt, prompt[:200]
    assert COAL in prompt and BANK in prompt


# ===========================================================================
# ③ 回归护栏：单标的路径与修复前**逐字一致**
# ===========================================================================


async def test_single_stock_path_is_unchanged() -> None:
    """★ 单只票 ⇒ 走**修复前的原路径**（连调用方式都没变），且不产多值字段。

    这条是结构性保证的护栏：`_prepare` 在 `len(codes) < 2` 时执行的是
    与修复前**逐字相同**的那几行，所以这里的断言是在钉"没有被误改"。
    """
    out, _gw = await _run([
        _dp(f"PE(TTM):{COAL}", 12.0), _dp(f"PB:{COAL}", 1.2),
        _dp("industry_pe", 20.0), _dp("industry_pb", 2.0),
    ])
    assert "valuation_calc_by_code" not in out.result, out.result
    calc = out.result["valuation_calc"]
    assert calc["valuation"] == "低估"          # 12<20 且 1.2<2
    assert calc["basis"] == "行业均值对比"
    assert "12.0" in calc["detail"]


async def test_no_code_falls_back_to_the_single_path() -> None:
    """★ 拿不到任何代码（例如只有行业级指标）⇒ 同样走单值路径，不误判成"多标的"。"""
    out, gw = await _run([_dp("PE", 18.0), _dp("PB", 5.0),
                          _dp("industry_pe", 25.0), _dp("industry_pb", 6.0)])
    assert "valuation_calc_by_code" not in out.result
    assert out.result["valuation_calc"]["valuation"] == "低估"
    assert "逐票" not in gw.calls[0]["prompt"], "单标的 prompt 里出现了多标的措辞"


async def test_single_stock_percentile_history_unchanged() -> None:
    """★ 复刻既有用例的形状（单代码 20 期）⇒ 行为与修复前一致。"""
    pe_points = _series("300308", "PE(TTM)", [float(220 - i * 10)
                                              for i in range(20)])
    pb_points = _series("300308", "PB", [float(11 - i * 0.5) for i in range(20)])
    out, _gw = await _run(pe_points + pb_points, focus="300308",
                          user_query="中际旭创是否价值洼地？")
    calc = out.result["valuation_calc"]
    assert calc["basis"] == "自身历史分位"
    assert calc["valuation"] == "历史低位"
    assert calc["pe_percentile"] is not None and calc["pe_percentile"] <= 10
