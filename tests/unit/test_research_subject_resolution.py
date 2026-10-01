"""★★ 分析标的解析：**问句点名的股票优先于输入框里的代码**（用户报障驱动）。

## 报障现场（2026-09-29 原话）

> 「当前宏观环境如何，预测下未来一年美国的加息、降息节奏，对A股的影响，
>   以及AI应用加速失业率增加对消费的影响节奏时间节点分析，
>   未来半年能否持有高股息的**招商银行**？」

`POST /api/v1/research/analyze` 的 `target` 是 **300068**（输入框残留/误填），
于是 `candidate = target or query` 解析出 300068(**ST南都**)：

* A01 去取 `股息率:300068` / `商誉占净资产比:300068` / `大股东质押比例:300068`…
* A10/A11/A12 全按 300068 算；
* 用户读到的是「**招商银行(600036) PE/PB/行业均值全部缺失**，valuation_calc=数据不足」。

**不报错、答错标的** —— 比"缺数据"更危险。判据：输入标的是**裸 6 位代码**
**且** 问句能解析出一只**不同的**股票 ⇒ 以问句为准（`target` 一起改，
因为它是规划 prompt 的「用户指定标的」与补后缀依据）。

## 为什么这条判据必须存在（而不是"让用户别填错"）

`target` 会被写进规划 prompt 与 `sanitize_indicators` 的补后缀依据 ——
**只改 `focus_stock_code` 不改 `target` 等于没改**（LLM 仍按旧代码生成指标）。
所以"以问句为准"必须是**一处判定**，见 `resolve_analysis_subject()`。
"""

from __future__ import annotations

import pytest

from src.api.routes.research import resolve_analysis_subject

#: 用户原话（逐字），只把结尾的问号保留。
_QUERY = ("当前宏观环境如何，预测下未来一年美国的加息、降息节奏，对A股的影响，"
          "以及AI应用加速失业率增加对消费的影响节奏时间节点分析，"
          "未来半年能否持有高股息的招商银行？")


@pytest.mark.asyncio
async def test_query_named_stock_wins_over_a_stale_code_target() -> None:
    """★ 报障现场：`target=300068` + 问句点名招商银行 ⇒ 分析 600036。"""
    subject = await resolve_analysis_subject("300068", _QUERY, "full")
    assert subject.target == "600036", "规划 prompt 的标的必须换成问句点名的那只"
    assert subject.focus_stock_code == "600036"
    assert subject.focus_stock_name == "招商银行"
    assert subject.target_display == "招商银行"
    assert subject.note, "改判必须留下人话说明（否则用户不知道系统换了标的）"
    assert "300068" in subject.note and "600036" in subject.note


@pytest.mark.asyncio
async def test_stock_task_also_prefers_the_query_name() -> None:
    """个股任务同样适用（用户可以点了某只票、却在问句里问了另一只）。"""
    subject = await resolve_analysis_subject("300068", _QUERY, "stock")
    assert (subject.target, subject.focus_stock_code) == ("600036", "600036")


@pytest.mark.asyncio
async def test_matching_target_is_left_alone() -> None:
    """输入与问句**一致**时不动，也不产生说明（保持既有行为）。"""
    subject = await resolve_analysis_subject("600036", _QUERY, "full")
    assert subject.target == "600036"
    assert subject.focus_stock_code == "600036"
    assert subject.note == ""


@pytest.mark.asyncio
async def test_macro_query_without_a_named_stock_keeps_target_empty() -> None:
    """★ 宏观问的 `target` **必须是空的** —— 填上个股会把 A08 的焦点顶掉。

    （这是 2026-09-28 第二十三轮定下的口径：解析出的代码只进
    `focus_stock_code`，不写进 `target`。）
    """
    subject = await resolve_analysis_subject(
        "", "当前宏观环境如何，预测下未来一年美国的加息、降息节奏", "macro")
    assert subject.target == ""
    assert subject.target_display == ""
    assert subject.focus_stock_code == ""


@pytest.mark.asyncio
async def test_a_code_in_the_query_is_still_resolved_for_a_macro_question() -> None:
    """问句里**直接给了代码**时，宏观问也要把它记成 focus（既有行为）。"""
    subject = await resolve_analysis_subject(
        "", "分析600036未来半年的持有价值", "macro")
    assert subject.focus_stock_code == "600036"
    assert subject.target == "", "宏观问的 target 不许被个股顶掉"


@pytest.mark.asyncio
async def test_query_without_a_stock_name_keeps_the_input_target() -> None:
    """问句里没有股票名时**不许改**（用户点了一只票再问通用问题，是合法用法）。"""
    subject = await resolve_analysis_subject(
        "300068", "未来半年能持有吗？", "full")
    assert subject.target == "300068"
    assert subject.focus_stock_code == "300068"
    assert subject.note == ""


@pytest.mark.asyncio
async def test_stock_type_target_becomes_the_code() -> None:
    """个股任务：中文名必须落成 6 位代码（否则会拼出 `PE(TTM):招商银行`）。"""
    subject = await resolve_analysis_subject("", "招商银行", "stock")
    assert subject.target == "600036"
    assert subject.target_display == "招商银行"
