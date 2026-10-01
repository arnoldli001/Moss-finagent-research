"""LangGraph共享状态定义（Supervisor编排用）。"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class ResearchState(TypedDict):
    """投研任务的LangGraph共享状态。

    列表字段使用 add reducer，支持无依赖Agent并行执行后自动聚合输出。
    """

    task_id: str
    tenant_id: str
    user_query: str
    analysis_type: str
    target: str
    """解析后的标的标识（个股为6位代码，如300308；宏观/行业为主题词）"""
    target_display: str
    """标的展示名（如中文简称'中际旭创'），报告标题与Agent focus使用；无则回退target"""

    focus_stock_code: str
    """★ 2026-09-28 第二十三轮：**复合问里夹带的个股代码**（6 位，无则空串）。

    与 `target` 的区别（两者语义不同，不能合并）：
      · `target` 表达"这次分析的**主题**"——宏观问的 target 是空的，
        A08 的 prompt 焦点就是它；
      · `focus_stock_code` 表达"问句里**顺带**要看的标的"——
        用户问宏观时夹了一句"未来半年能否持有高股息的招商银行"，
        target 仍是空（主题是宏观），但 A10 需要这只票的代码才能取数。

    合并的后果（实测过）：把"招商银行"写进 `target` 会让 A08 的
    prompt 变成"分析焦点：招商银行"，宏观分析被一只票顶掉。
    """
    focus_stock_name: str
    """`focus_stock_code` 对应的中文简称（展示用，可空）"""

    # Supervisor计划（本run要执行的agent_id列表）
    plan: list[str]

    # ★★★ 2026-09-29：**规划出的指标清单必须在这里声明**（真实端到端验收抓到的缺陷）
    #
    # 报障现场（真实 LLM 端到端，用户那条复合问）：
    #   问「…未来半年能否持有高股息的**招商银行**？」，系统答
    #   「招商银行(600036)**无任何个股财务与股息数据**」「无个股 PE/PB」——
    #   而实测 `PE(TTM):600036=6.76`、`PB:600036=0.9`、`股息率TTM:600036=4.9606%`
    #   **全都取得到**（连接器 supports + 真实路由都通）。
    #
    # 根因：`supervisor_node` 确实做了问句信号增补
    #   （`_apply_query_signal_augmentation` 单独测会补 `PE(TTM):600036`、
    #    `ind:社会消费品零售总额同比`、`us_*` 等 13 条），也确实 return 了
    #   `"_planned_indicators": indicators` —— 但 **LangGraph 只保留在状态
    #   schema 里声明过的 channel**，未声明的键在节点返回时被**静默丢弃**。
    #   于是 `_collect_payload` 拿到 `None` → 回退 `plan_run(...)` →
    #   只采基础模板那 17 条。
    #
    # 为什么这个缺陷特别隐蔽（三件事**同时**为真，所以没人发现）：
    #   ① **Agent 补上了**（`plan` 是声明过的）→ A13/A14 真的跑了，
    #      只是拿不到本行业指标 → 表现为「无本行业关注指标，跳过 LLM」；
    #   ② **指标没了**（本键未声明）→ A10/A11 只见宏观/大盘 →
    #      表现为「招商银行无任何个股数据」，听起来像**数据源的问题**；
    #   ③ **全程无异常**：没有 KeyError、没有 warning，errors 为空。
    #
    # 判据：`tests/unit/test_planned_indicators_channel.py`
    #   —— 含一条**行为判据**：拿真实 `ResearchState` 编译一个 `StateGraph`，
    #   节点返回该键 → 必须能在输出里读到。**删掉这行声明，那条就红。**
    _planned_indicators: list[str]

    # 数据管线流转（collector→cleaner→validator→storage各写一次，add聚合）
    raw_points: Annotated[list[dict[str, Any]], operator.add]
    cleaned_points: Annotated[list[dict[str, Any]], operator.add]
    validated_points: Annotated[list[dict[str, Any]], operator.add]
    validation_report: dict[str, Any]
    storage_stats: dict[str, Any]

    # 信息层管线（A05→A06→A07串行链；info_items为任务创建时直接注入）
    info_items: list[dict[str, Any]]
    verified_items: dict[str, Any]
    extracted_events: dict[str, Any]

    # 本地量化研判参考（流动性周期skill产出，分析层与A17共享）
    analysis_hint: dict[str, Any]

    # 并行Agent输出聚合
    agent_outputs: Annotated[list[dict[str, Any]], operator.add]
    data_refs: Annotated[list[str], operator.add]
    trace_ids: Annotated[list[str], operator.add]
    errors: Annotated[list[str], operator.add]

    # Agent间多轮对话（A2A）：sender→receiver的问答记录，供前端展示协作过程
    agent_messages: Annotated[list[dict[str, Any]], operator.add]

    # 实时进度（running时前端展示，node返回值通过add累积，读取取最后一项）
    progress: Annotated[list[str], operator.add]

    # 取消令牌（注入CancellationToken实例，各节点入口检查）
    cancellation_token: Any

    final_report: str | None
