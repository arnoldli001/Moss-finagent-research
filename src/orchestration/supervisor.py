"""Supervisor编排引擎（LangGraph StateGraph）。

管线：supervisor(规划) → A01采集 → A02清洗 → A03校验 → A04入库 →
      并行分析(A08宏观/A09中观/A10微观/A11风险) → A17综合建议 → A18审计封存。

Agent通过工厂注入（测试可全部替换为Fake）；节点级异常吞入state.errors，
不中断整图；计划外Agent节点直接跳过（空更新）。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from langgraph.graph import END, START, StateGraph

from src.core.agent_meta import agent_name, confidence_zh
from src.core.exceptions import AgentExecutionError
from src.core.hop_stats import (
    HOP1_VALIDATED,
    HOP2_LOCAL,
    HOP3_CONNECTOR,
    HOP4_NETWORK,
    HOP_NONE,
    bump,
)
from src.core.intel_limits import query_deadline_sec
from src.core.models import AgentInput, AgentOutput
from src.core.state import ResearchState
from src.domain.skills.library import SkillLibrary
from src.orchestration.planner import INDICATOR_CATALOG, LLMSupervisorPlanner

#: A17 走多步 ReAct 所需的**最低剩余时间**（秒）。低于它就当场降到单步直答：
#: 实测多步是 3 次串行 34.7s，而单步直答约 6~11s —— 期限已经很紧时，
#: "多一步自我校验"换来的不是质量而是超时。
_A17_REACT_MIN_SEC: float = 12.0


def _a17_max_steps(configured: int) -> int:
    """A17 实际步数 = `configured`，但**限时模式下剩余不够就降到 1**。

    抽成独立函数是为了能被**行为判据**钉住（而不是把规则埋在节点函数里，
    只能靠读代码确认）。未启用期限 ⇒ 原样返回（默认行为逐字不变）。
    """
    from src.core import deadline as _deadline

    if not _deadline.active() or _deadline.remaining() >= _A17_REACT_MIN_SEC:
        return configured
    if configured > 1:
        logger.info("限时模式：A17 ReAct 步数 %d → 1（剩余 %.1fs < %.0fs）",
                    configured, _deadline.remaining(), _A17_REACT_MIN_SEC)
    return 1


logger = logging.getLogger(__name__)

# ======================================================================
# ★ 2026-09-27 第八轮：DataGapResolver 白名单 + 频率上限
# 审计实证：A19 在东财 WAF 失效的指标上每 trace 稳定触发，50 次调用 →
#          162,608 tokens 输出 = 全系统输出的 23%，**全是浪费**。
# 设计：
#   · _HEAL_BLACKLIST：已知数据源不可达（实测多次失败）的 indicator 前缀，
#     命中即跳过 + 一次性 warn 而非每次 trace 都 LLM。
#   · _HEAL_FAIL_CACHE：本进程内近期自修复失败的指标。
#   · _HEAL_QUOTA_WINDOW：60 秒滑动窗口，全局最多触发 5 次（突发阻尼）。
# ======================================================================
_HEAL_BLACKLIST: tuple[str, ...] = (
    "ind:sw_third_",    # 东财申万三级（已知 WAF 限频，2026-09 持续失败）
    "ind:ths_",         # 同花顺指标（连通性不稳）
    "ind:custom_",      # 自定义指标 → 应走运维人工接入
)
_HEAL_FAIL_CACHE: dict[str, float] = {}
_HEAL_FAIL_TTL = 3600.0  # 失败指标 1 小时内不再尝试
#: ★ 2026-10-07（`CHG-0190` ②）：在飞的自修复后台任务的**强引用**。
#:
#: 为什么必须有：`asyncio.create_task` 只保留**弱**引用，任务对象被 GC 之后
#: 会被**静默取消**（本项目已在 `src/api/routes/intel.py:419` 记过这条）。
#: 放在模块级是刻意的 —— 闭包内的局部集合会随节点返回而失去意义。
_HEAL_TASKS: set[asyncio.Task] = set()
#: 单轮最多登记多少条"待自修复"缺口（防刷屏；与 A17 报缺口的 `gaps[:10]` 对齐）。
_SELF_HEAL_MAX_PER_ROUND = 10
_HEAL_QUOTA_WINDOW: list[float] = []
_HEAL_QUOTA_MAX = 5       # 60 秒内最多 5 次自修复尝试
_HEAL_QUOTA_WINDOW_S = 60.0


def _self_heal_allowed(indicator: str, error_ctx: str, *,
                       consume: bool = True) -> bool:
    """自修复入口闸门：白名单黑名单 + 失败缓存 + 限频。

    Args:
        consume: **是否消耗限频配额**（默认 True，保持原语义）。
            ★ 2026-10-07（`CHG-0190` ②）：后台调度路径必须先做一次**不消耗**的预检 ——
            原先这道闸门"返回 True 就把 now 记进 `_HEAL_QUOTA_WINDOW`"，
            于是"先判据、再 `create_task`"会在**任务真正跑起来之前**就把
            5 次/60s 的配额吃掉（判据与实际尝试解耦 = 静默烧配额）。
            预检用 `consume=False`，真正的尝试由 `_try_self_heal` 内部那次消耗 ——
            **判断仍然只有一份实现**。

    Returns:
        True = 允许走自修复流程；False = 直接跳过（节省 LLM 调用）。
    """
    import time as _t
    now = _t.monotonic()

    # 1. 黑名单（前缀匹配）：已知数据源故障
    for prefix in _HEAL_BLACKLIST:
        if indicator.startswith(prefix):
            logger.warning(
                "A19 自修复黑名单跳过：%s（前缀 %s 已知数据源不可达）",
                indicator, prefix)
            return False

    # 2. 失败缓存：本进程内近期尝试过且失败的指标
    last_fail = _HEAL_FAIL_CACHE.get(indicator)
    if last_fail is not None and (now - last_fail) < _HEAL_FAIL_TTL:
        return False

    # 3. 限频：60 秒滑动窗口
    global _HEAL_QUOTA_WINDOW
    _HEAL_QUOTA_WINDOW = [t for t in _HEAL_QUOTA_WINDOW
                          if (now - t) < _HEAL_QUOTA_WINDOW_S]
    if len(_HEAL_QUOTA_WINDOW) >= _HEAL_QUOTA_MAX:
        logger.warning(
            "A19 自修复限频触发：60s 内已尝试 %d 次（限 %d），本次跳过",
            len(_HEAL_QUOTA_WINDOW), _HEAL_QUOTA_MAX)
        return False
    if consume:
        _HEAL_QUOTA_WINDOW.append(now)
    return True


def _record_heal_failure(indicator: str) -> None:
    """记录一次自修复失败（用于失败缓存与黑名单上报）。"""
    import time as _t
    _HEAL_FAIL_CACHE[indicator] = _t.monotonic()


def _record_heal_success(indicator: str) -> None:
    """成功时清掉失败缓存。"""
    _HEAL_FAIL_CACHE.pop(indicator, None)


def _note_self_heal_candidate(updates: dict[str, Any], indicator: str,
                              miss_stage: str,
                              miss_reason: str) -> dict[str, Any] | None:
    """把一个**真缺口**登记进 `self_heal_pending`，并回答"要不要调度自修复"。

    Returns:
        要调度 ⇒ 返回登记条目；不调度 ⇒ `None`（条目可能仍被登记，见 `guarded`）。

    ★ 抽成模块级纯函数是刻意的（`CHG-0190` ②）：这三条规则
    （单轮上限、护栏预检、`guarded` 标记）原先埋在 `_live_fetch_one` 的闭包里，
    **除了跑整张图没有别的办法验证** —— 而"没法单测的规则"正是它当初能变成
    一句注释的原因。现在它们是纯函数，`tests/unit/test_self_heal_wiring.py` 直接测。

    ⚠️ 预检必须 `consume=False`：`_self_heal_allowed` 是"检查即记账"的闸门
    （返回 True 就把 now 写进 60s 窗口），先判后调度会在**任务真正跑起来之前**
    把 5 次/60s 的配额吃掉；真正的尝试由 `_try_self_heal` 内部那次消耗。
    """
    pending = updates.setdefault("self_heal_pending", [])
    if len(pending) >= _SELF_HEAL_MAX_PER_ROUND:
        return None
    entry: dict[str, Any] = {
        "indicator": indicator,
        "stage": miss_stage,
        "reason": str(miss_reason)[:200],
    }
    if not _self_heal_allowed(indicator, str(miss_reason), consume=False):
        # 被护栏拦下（黑名单 / 1h 失败缓存 / 限频）⇒ **登记但不去试**：
        # "没量到"与"量到不值得试"必须分开，且都不静默。
        entry["guarded"] = True
        pending.append(entry)
        return None
    pending.append(entry)
    return entry


def _enqueue_self_heal_gap(indicator: str, *, reason: str, task_id: str) -> bool:
    """自修复没成 ⇒ 把缺口交给**盘后**（`gap_drain`）。

    与 A17 报缺口（`_enqueue_data_gaps`）**共用同一个队列**：队列自带
    24h 去重、`MAX_ATTEMPTS=3` 退避、`ROUTE_PROSE` 兜底（A19 无从下手的形态
    只登记不烧钱）。**不允许出现第二套"缺口"实现**。
    """
    from src.domain.agents.decision.gap_queue import get_gap_queue

    return bool(get_gap_queue().enqueue(
        _resolve_gap_indicator(indicator) or indicator,
        reason=f"自修复未成功（{str(reason)[:80]}）",
        status="fetchable", source="self_heal", trace_id=task_id))

ANALYSIS_AGENTS = ("A08_macro", "A09_meso", "A10_micro", "A11_fin_risk", "A12_compliance")
#: `ask_agent` 工具的**每 Agent 追问上限**（模块级：工具描述与执行逻辑共用同一常量）。
#:
#: ★ 曾经是 `recommend_node` 内的局部变量，而工具描述里**手写**了"最多追问2次" ——
#: 同一件事两个数。提到模块级后，`ask_agent_tool_description()` 用插值生成描述，
#: 判据 `tests/unit/test_ask_agent_tool_contract.py` 断言两者一致。
#: 收紧到 1 次的理由：实测 LLM 会绕着重问同一 Agent，而追问代价是真金白银的 LLM 调用。
MAX_ASKS_PER_AGENT = 1
#: 行业层 Agent。⚠️ **A20 是兜底**：它不靠关键词命中，而是在
#: "问句/标的有明确行业，但没有任何专属 Agent 覆盖它"时由
#: `needs_generic_industry()` 补挂（如银行/非银/公用事业/交运）。
INDUSTRY_AGENTS = ("A13_tech", "A14_consumer", "A15_cyclical", "A16_pharma",
                   "A20_generic_industry")
#: **有专属关键词的**行业 Agent（A20 刻意不在其中 —— 它没有关键词，
#: 加进来会让 `route_industry("")` 这类空文本也命中它）。
KEYWORDED_INDUSTRY_AGENTS = ("A13_tech", "A14_consumer", "A15_cyclical",
                             "A16_pharma")
# 行业路由表：target命中关键词 → 对应行业Agent（industry类型任务用）
INDUSTRY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "A13_tech": ("科技", "半导体", "芯片", "电子", "软件", "ai", "人工智能", "计算机",
                 "通信", "算力", "消费电子"),
    "A14_consumer": ("消费", "白酒", "食品", "饮料", "家电", "零售", "纺织", "服装",
                     "美妆", "农业", "旅游", "免税"),
    "A15_cyclical": ("周期", "煤炭", "有色", "钢铁", "化工", "建材", "石油", "原油",
                     "航运", "铜", "铝", "锂"),
    "A16_pharma": ("医药", "医疗", "创新药", "生物", "器械", "中药", "药店", "疫苗",
                   "cxo"),
}
INFO_AGENTS = ("A05_verifier", "A06_extractor", "A07_sentiment")
DATA_PIPELINE_AGENTS = ("A02_data_cleaner", "A03_data_validator", "A04_data_storage")
# 产出可被A17综合的全部分析类Agent
ALL_INSIGHT_AGENTS = ANALYSIS_AGENTS + INDUSTRY_AGENTS


# ★★★ 2026-09-28 第十四轮：分析层 Agent 白名单（−2万 tokens_in/轮）
#
# ⚠️⚠️ **新增指标类时，必须同步检查这张表的全部条目**（血泪教训）：
#
# 本轮把「投资日历/限售解禁」落库并登记索引后，`cal:*` 指标**没有出现
# 在下面任何白名单里**。实测后果（`scripts/_verify_cal_whitelist.py`）：
#   · A13-A16 明确被过滤掉（连兜底都没轮到）
#   · A08/A09/A11 显示"保留"是**假绿** —— 它们白名单命中 0 条，
#     靠 `_filter_points_for_agent` 的**兜底**（返回前 200 条）碰巧混进来；
#   · A10/A12 只保住 `cal:unlock:top_stock_cap`，因为白名单里的
#     `stock_` 前缀**巧合**匹配到了 `...top_**stock**_cap`。
#
# 也就是说："数据落库了 + 索引登记了 + SmartFetcher 取得了"
# **不等于** "分析层看得见"。中间还隔着这一层过滤。
#
# 纪律：**新增指标前缀 → grep 这张表 → 逐个决定该给谁**。
# 判据见 `docs/DATA_INDEX_REQUIREMENTS_AUDIT.md` 与
# `.trae/skills/requirement-closure-and-impact/SKILL.md`。
_AGENT_DATA_WHITELIST: dict[str, tuple[str, ...]] = {
    # A08 宏观：通胀/利率/汇率/GDP/货币政策
    #
    # ★★★ 2026-09-28 第二十三轮：**补 en_id 与 `fed:` 前缀（真实报障驱动）**
    #
    # 【报障原文】用户问"预测下一年美国的加息、降息节奏"，前端显示：
    #     「缺少联邦基金利率数据，无法判定方向」
    #   而库里 `fed:policy_range` 有 3 条（2026-09-27 的 3.75/4.00）、
    #   `us_fed_rate` 有 72 条、`us_unemployment`/`us_nonfarm` 各 107 条。
    #
    # 【根因】这张表原先只写**中文标签**（"非农"/"失业率"/"利率决议"）
    #   和**产品名**（"FedWatch"），而库里存的是 **en_id**
    #   （`us_nonfarm` / `us_unemployment` / `us_fed_rate` / `fed:policy_range`）。
    #   匹配是**子串**（见 `_filter_points_for_agent`），
    #   "非农" ⊄ "us_nonfarm" → **一条都放不过去**。
    #
    # 【实测证据】`scripts/_probe_whitelist_coverage.py`（归档在
    #   `docs/_evidence_20260928_model_routing/`）跑 pilot 库：
    #     A08 白名单 22 词 → 719 种指标里只放行 **4** 种
    #     （CPI/PPI/us_core_cpi/us_cpi_yoy），
    #     `us_fed_rate` `fed:policy_range` `us_nonfarm` `us_unemployment`
    #     `us_pce` 全部被挡 → 纯模板据此报"缺少联邦基金利率数据"。
    #
    # 【纪律】只补 A08 的 `system_prompt` **已经声称会读**的那些指标
    #   （见 macro/agent.py 的 prompt："美国(CPI/核心CPI/非农/失业率/联邦利率/PCE)"）
    #   —— 不顺手扩权。`mkt:` 仍**不给** A08：大盘流动性走
    #   `analysis_hint.market_liquidity`（liquidity_ctx_node），不走 payload。
    #
    # 【护栏】`tests/unit/test_whitelist_coverage.py`：
    #   ① configs/indicators.yaml 每个登记指标至少被一个 Agent 放行；
    #   ② A08 必须放行 us_macro 类全部指标；
    #   ③ 白名单关键词必须在 indicators.yaml 里真实存在（无幻影词）。
    "A08_macro": (
        "CPI", "PPI", "M2", "GDP", "PMI", "社融", "新增贷款", "十年国债", "FedWatch",
        "美元指数", "美债", "非农", "失业率", "美元兑人民币", "利率决议", "通胀",
        "货币政策", "CPI同比", "PPI同比", "宏观",
        # ★ 2026-09-29：`"GDP同比"` → `"GDP:同比"`。
        #   原写法**无冒号**，而登记的 id 是 `GDP:同比`（**有冒号**）——
        #   匹配是子串，`"GDP同比"` ⊄ `"GDP:同比"` ⇒ 这是一条**幻影关键词**：
        #   看起来"GDP 同比我放行了"，实际一条都放不过去
        #   （`test_whitelist_coverage.py::_ALLOWED_PHANTOM_PREFIXES` 里
        #    `A08_macro:GDP同比` 一直挂着豁免，就是它的痕迹）。
        #   ⚠️ 同一个指标两种写法 = 口径漂移。**这类漂移只能靠"关键词必须能在
        #   indicators.yaml 里匹配到"这条判据抓**，肉眼看是看不出来的。
        "GDP:同比",
        # ★ 第二十三轮：en_id（库里**实际**用的名字 —— 上面那批中文词多数匹配不上）
        "us_cpi_yoy", "us_core_cpi", "us_nonfarm", "us_unemployment",
        "us_pce", "us_fed_rate",
        # ★ 第二十三轮：`fed:` 前缀覆盖 FRED 全部序列
        #   （`fed:policy_range` 旧口径 + `fed:target_upper/lower` + `fed:effr` 新口径）
        "fed:",
        # ★ 2026-09-29：**`fred:` 通用序列前缀**（`FredConnector`，按序列号取数）。
        #   为什么必须放行：AkShare 东财 `macro_usa_*` **接口本身停更**
        #   （实测 `us_unemployment`/`us_nonfarm` 最新行 2025-09-05 且值 `nan`、
        #   `us_fed_rate` 2025-10-30 `nan`）⇒ 换 FRED 补齐到最新：
        #   `fred:UNRATE` 2026-08-01 = 4.1、`fred:DGS10` 2026-09-25 = 5.17%。
        #   ⚠️ `fred:` 与上面的 `fed:` **不是同一个前缀**（后者是政策利率族的三条单值序列）；
        #   漏了它，A08 就拿不到新补的美债/失业率（"数据到了、Agent 看不见"的又一次）。
        "fred:",
        # ★ 第十四轮：日历类 —— 宏观发布日程与解禁是宏观供给面的组成部分
        "cal:",
    ),
    # A09 中观行业：行业指数、申万/东财行业估值、板块资金流
    #
    # ★★★ 2026-09-28 第二十三轮：补 `ind:sw_` / `mkt:` / `idx_val:`
    #
    # 【根因同 A08】白名单写的是"前缀的**近似写法**"（`sw_`/`ind_`），
    #   而库里存的是 `ind:sw_third_pe_ttm:all` —— `sw_` ⊄ `ind:sw_...`
    #   只在 `sw_` 紧跟 `ind:` 之后才成立？不成立：`"ind:sw_third..."` 里
    #   确实含子串 `sw_` ✅。**真正的漏项是 `mkt:` / `idx_val:` / `ind:` 本身**：
    #   `ind:sw_*` 靠 `sw_` 侥幸匹配上了，但 `ind:贯穿率` 类（`ind:penetration`）
    #   与全部大盘流动性指标（`mkt:*`）**一条都放不过去**。
    #
    # 【为什么 A09 必须有 `mkt:`/`idx_val:`】A09 的 `system_prompt` 明写：
    #   「先看两市总量阶段→三市成交额占比判风格→双创PE分位判冷热→再定板块轮动」
    #   —— 它**已经声称**要读这几族数据，而白名单不给。
    "A09_meso": (
        "sw_", "ind_", "bk_", "industry_", "板块", "行业", "估值分位",
        "industry_pe", "industry_amount", "industry_pct",
        # ★ 第二十三轮
        "ind:", "mkt:", "idx_val:",
        # ★ 第十四轮：解禁是**板块级供给冲击**（如 2026-10-28 西安奕材
        #   单家解禁 848 亿，直接影响半导体板块资金面）
        "cal:",
        # ★ 2026-09-29：**行业侧三族**（平台自有后端数据，按行业名/板块名取数）。
        #   用户原话：「用户输入内容中含有行业的，要连接到概念板块拥挤度的数据表，
        #   找与之最相近的所属概念板块」「也可以接入"板块资金流"功能板块的数据，
        #   查询该板块近期资金流方向」「问到现在和未来近期行情的，可以接入
        #   "行业轮动日报"里的数据」。A09 是中观风格/轮动 Agent，三族都归它。
        "行业拥挤度", "板块资金流", "行业轮动",
    ),
    # A10 微观个股：估值/行情/财务指标（按个股代码）
    "A10_micro": (
        "stock_close:", "stock_open:", "stock_high:", "stock_low:",
        "stock_volume:", "PE(TTM)", "PB", "PS", "PCF",
        "stock_turnover:", "流通市值", "总市值", "换手率",
        "涨跌幅", "成交额", "stock_", "市值", "pe_ttm", "pb",
        # ★ 第十四轮：解禁/财报日程直接影响个股供给与估值
        "cal:",
        # ★ 2026-09-29：**平台自有后端数据**（`PlatformDataConnector`）。
        #   用户原话：「个股 agent：自身估值水平…是属于"估值透支"、还是
        #   "估值合理偏贵"、"上涨空间充足"，其判断依据的数据，都可以连接到」。
        #   这 5 族的「判断依据」平台**早就有**（前端看板/主线挖掘/投资日历），
        #   只是从没进过连接器 ⇒ Agent 侧看不到、用户看到的是"缺数据"。
        #   ⚠️ 名字必须与 `_CODE_SUFFIX_INDICATORS` 一致（否则被加成裸名 → 必然失败）。
        "估值水位", "概念拥挤度", "主线告警", "个股告警", "解禁计划",
        # ★ 2026-09-29：行业侧三族（按**行业名/板块名**，不是代码后缀）。
        #   用户原话：「用户输入内容中含有行业的，要连接到概念板块拥挤度的数据表」
        #   「也可以接入"板块资金流"功能板块的数据」「行业轮动日报里的数据」。
        "行业拥挤度", "板块资金流", "行业轮动",
    ),
    # A11 财务风险：偿债/盈利/营运/现金流指标
    "A11_fin_risk": (
        "资产负债率", "流动比率", "速动比率", "净负债率", "商誉", "扣非净利润",
        "经营性现金流", "存货周转", "应收账款", "ROE", "ROA", "毛利率",
        "净利率", "有息负债", "财务费用", "杠杆", "debt_ratio",
        # ★ 第二十四轮：连接器已实现取数的财入口径（此前只有"连接器"一面，
        #   白名单没有 → A11 拿不到 → 结论只能写"缺基本面数据"）。
        #   子串匹配，所以这里写**不带代码后缀**的口径名即可命中 `口径:600036`。
        "股息率", "股息率TTM", "产权比率", "销售净利率", "成本费用利润率",
        "存货周转率", "应收账款周转率", "总资产周转率", "净利润增长率",
        "总资产增长率", "净资产增长率", "营收增长率", "EPS", "每股净资产",
        "每股经营现金流", "每股未分配利润", "每股资本公积", "总市值",
        "流通市值", "换手率", "量比", "市销率", "市盈率", "市净率",
        # ★ 2026-09-29：**银行报表口径**（用户点名的「银行息差」的公式输入）。
        #   为什么归 A11：它是**盈利能力/财务风险**的读法
        #   （净息差 = 利息净收入 ÷ 生息资产平均余额），而 A10 看估值、A12 看合规事件。
        #   实测这三个字段只在银行/金融报表里存在（工商业利润表没有「利息净收入」），
        #   数据由 `BankStatementConnector` 提供（新浪财务分析报表模板）。
        "利息净收入", "利息收入", "利息支出", "总资产", "净息差",
        # ★ 第十四轮：**解禁是财务风险的核心输入**（限售股解禁 =
        #   潜在减持压力 = 股价下行风险；尤其"占流通市值比"高的）
        "cal:",
        # ★ 2026-09-29：平台自有数据里与**财务风险**直接相关的一族 —— 解禁计划
        #   （`cal:` 只覆盖聚合三条，个股明细在 `fact_data_points.extra_json` 里，
        #    指标名 `解禁计划:{code}` 不含 `cal:` 前缀，必须单独放行）；
        #   以及主线/个股告警（事件面风险与机会）。
        "解禁计划", "主线告警", "个股告警",
        # ★ 2026-09-29（真实端到端跑完补的）：**估值透支 = 风险**，而 A11 原先
        #   看不见 `估值水位`；拥挤度/资金流/轮动同理（拥挤高位=踩踏风险、
        #   资金净流出=退潮、轮动=风格切换）。
        #   【为什么必须补 —— 实测的原话】行业形态问句里 A11 写
        #   「本上下文**无**银行行业拥挤度、板块资金流、行业轮动及 600036 估值水位…
        #     **属数据缺口**」——它把"我这一片没有"说成了"平台没有"，
        #   而同一轮 A10/A20 正在引用这些具体数值。
        #   白名单是**过滤器**，被挡掉的东西在 Agent 眼里与"不存在"无法区分，
        #   所以「该不该给它」要比「它会不会用错」更早回答：**给**，
        #   并在教学块里写清风险视角怎么用（见 `platform_data_teaching` 的 risk 角色块）。
        "估值水位", "概念拥挤度", "行业拥挤度", "板块资金流", "行业轮动",
    ),
    # A12 合规爆雷：公告/处罚/诉讼/关联交易
    "A12_compliance": (
        "公告", "处罚", "诉讼", "关联交易", "减持", "增持", "回购",
        "担保", "质押", "冻结", "st_", "ST", "违规", "监管",
        "问询函", "关注函", "defense", "compliance",
        # ★ 2026-09-29：**合规本地规则直接消费的比率族**，必须放行。
        #
        # 【为什么这 3 个词是必需的（不是顺手扩权）】
        #   `compliance/logic.py` 的 `_RATIO_RULES` 有「商誉」一档，
        #   `_double_high_flag` 需要「货币资金」**与**「有息负债」两个输入。
        #   本表原先只有 `担保`/`质押`/`关联交易` 三个词能**巧合**匹配到
        #   生产者给的指标名（`对外担保占净资产比`←担保、`大股东质押比例`←质押、
        #   `关联交易占营收比`←关联交易）；商誉/货币资金/有息负债**全部被挡**。
        #
        # 【实测后果】生产者 2026-09-29 落地（`ComplianceFinConnector` 真实取到
        #   600036 商誉占净资产比=0.7401、有息负债占总资产比=0.9868），
        #   但白名单挡掉 → A12 的 `payload.data_points` 里没有这三族
        #   → 商誉档与存贷双高（需两个输入同时到手）**恒不触发**
        #   → `compliance_level_calc` 照样给出结论。
        #   与"数据在库里 Agent 看不见"**完全同类，只是换了一层**。
        #
        # 【护栏】`tests/unit/test_whitelist_coverage.py` 的
        #   `test_registered_ids_in_agent_domain_stay_passable` +
        #   `test_every_registered_indicator_is_visible_to_some_agent`：
        #   登记了这 5 条，就必须有 Agent 真放行它们。
        "商誉", "货币资金", "有息负债",
        # ★ 第十四轮：解禁 → 大股东减持的前置事件
        "cal:",
    ),
    # 行业 Agent：板块成分股 + 行业估值
    #
    # ★★★ 2026-09-28 第二十三轮：**按 Agent 自己声明的 `watch_keywords` 补齐**
    #
    # 【本轮的推导方式（可复算）】`scripts/_derive_whitelist_gaps.py` 把每个
    #   行业 Agent 的 `watch_keywords` 与 `configs/indicators.yaml` 交叉比对，
    #   报出「自己声明要看的词，匹配得到登记指标，但白名单一条都不放行」。
    #   结果：A13 缺 `芯片/出货量`、A14/A15/A16 缺 `ind:sw_`/`ind:penetration`、
    #   A15 缺 `PPI`/`价格`。
    #
    # 【为什么这不是"顺手扩权"】这几族指标**本来就会进 payload**
    #   （`plan_run` 对 industry 类型无条件追加申万三个 + 命中赛道时追加
    #   `ind:penetration:*`），却被白名单挡掉 → 行业 Agent 的核心输入归零。
    #   更糟的是它**不报错**：`industry/base.py` 的 `_skip_reason()` 会判
    #   "无本行业关注指标" → 直接跳过 LLM，`skipped=True`，
    #   界面上看起来像"这个行业没什么可分析的"。
    #
    # 【护栏】`tests/unit/test_whitelist_coverage.py::
    #   test_industry_watch_keywords_survive_whitelist` ——
    #   任何"Agent 声明了、白名单却挡住"的组合立刻红。
    "A13_tech": (
        "sw_tech", "sw_电子", "sw_计算机", "sw_通信", "stock_close:",
        "板块", "科技", "半导体", "AI", "算力",
        # ★ 第二十三轮：`ind:` 整族。依据不是"顺手给"，是**消费侧代码**：
        #   · `industry/base.py:64-68` 用 `"PE" in indicator` 单独捞 PE 点进
        #     prompt（`ind:科技行业PE(TTM)` 就在这一族）→ 白名单挡掉 = 估值
        #     旗标（`pe_high_watermark`）永远判不了；
        #   · `industry/base.py:185-192` 明写"申万行业估值截面和渗透率数据
        #     本身也可支撑行业研判，**不应跳过**"→ 它俩是**防静默 skipped** 的
        #     最后一道闸，被白名单挡掉就等于这道闸失效；
        #   · `watch_keywords` 自己也写着 `ind:sw_` / `ind:penetration`。
        "ind:",
        # ★ 第十四轮：行业 Agent 需要看到**本行业的解禁冲击**
        #   （如存储/半导体在 10 月有西安奕材、中船特气两笔 800 亿级解禁）
        "cal:",
        # ★ 2026-09-29：行业侧三族（拥挤度/资金流/轮动日报）—— 用户原话见 A09 处注释。
        "行业拥挤度", "板块资金流", "行业轮动",
    ),
    "A14_consumer": (
        "sw_consumer", "sw_食品", "sw_纺织", "sw_商业", "stock_close:",
        "板块", "消费", "白酒", "家电",
        # ★ 第二十三轮：watch_keywords 含 `CPI`（消费景气的锚）+ `ind:` 整族
        "CPI", "ind:",
        "cal:",
        "行业拥挤度", "板块资金流", "行业轮动",
    ),
    "A15_cyclical": (
        "sw_cyclical", "sw_煤炭", "sw_有色", "sw_钢铁", "sw_化工",
        "stock_close:", "板块", "周期", "煤炭", "有色",
        # ★ 第二十三轮：watch_keywords 含 `PPI`（周期定位交叉验证）+ `ind:` 整族
        #   （`ind:动力煤价格(元/吨)` / `ind:重点电厂煤炭库存(万吨)` 是 A15 的
        #    两个**真实产业指标**，被挡掉等于周期定位只剩 PPI 一条腿）
        "PPI", "ind:",
        "cal:",
        "行业拥挤度", "板块资金流", "行业轮动",
    ),
    "A16_pharma": (
        "sw_pharma", "sw_医药", "sw_生物", "stock_close:",
        "板块", "医药", "创新药", "器械",
        # ★ 第二十三轮：`ind:创新药IND申报数量(个)` / `ind:医药行业PE(TTM)`
        #   是 A16 的两个真实产业指标，被挡掉等于医药只剩新闻面
        "ind:",
        "cal:",
        # ★ 2026-09-29：行业侧三族（拥挤度/资金流/轮动日报）。
        #   ⚠️ `板块` 这个笼统关键词只能**巧合**命中 `板块资金流`，
        #      另两族（`行业拥挤度` / `行业轮动`）必须逐字写出来 ——
        #      实测漏写时 `test_registered_ids_in_agent_domain_stay_passable[A16_pharma]`
        #      报"域内 2 条拿不到"。
        "行业拥挤度", "板块资金流", "行业轮动",
    ),
    # ★ 2026-09-29：**兜底行业 Agent**（A20）。它服务任意行业，
    #   所以这里只能给**族前缀**：`ind:`（申万截面 + 产业指标 + 渗透率）
    #   与 `cal:`（解禁等日历事件）。
    #   ⚠️ 刻意**不给** `stock_close:`/`PE(TTM)` —— 那是 A10（个股）的域；
    #   行业 Agent 看行业截面，不看单只票的行情（混在一起会让"行业结论"
    #   被一只票的走势带偏，而用户分不清哪句是行业、哪句是个股）。
    "A20_generic_industry": (
        "ind:",
        "cal:",
        # ★ 2026-09-29：兜底行业 Agent 也要这三族（它服务任意行业，
        #   所以按**族名前缀**放行，行业名由运行时解析）。
        "行业拥挤度", "板块资金流", "行业轮动",
    ),
}

#: ★ 第十四轮：新增指标**前缀**时必须检查的"关联模块清单"。
#:
#: 为什么列出来：本轮新增 `cal:` 前缀后，只改了"取数 + 索引"两层，
#: **漏了分析层白名单** → 数据取到了但 Agent 看不见（实测 A13-A16 被过滤）。
#: 这张表把"一个新指标前缀要穿过哪些门"显式写下来，
#: 避免下次又在某一层静默掉链。
#:
#: 用法：新增 `xxx:` 前缀时，逐个回答下面每一项"该不该加"。
NEW_INDICATOR_TOUCHPOINTS: tuple[tuple[str, str], ...] = (
    ("configs/indicators.yaml",
     "登记元数据（来源/频率/新鲜度/存储位置）"),
    ("_AGENT_DATA_WHITELIST（本文件）",
     "分析层可见性 —— 漏了就是「数据到了但 Agent 看不见」"),
    ("LLMSupervisorPlanner.INDICATOR_CATALOG",
     "planner 能否把它规划进 plan"),
    ("plan_run / append_liquidity_indicators",
     "规则式路由是否会补采它"),
    ("scheduler/catalog_jobs.FREQUENCY_TO_CRON",
     "有没有定时作业去更新它（否则永远 stale → 每次联网）"),
    ("domain/agents/decision/capabilities.py",
     "A17 是否知道这个能力存在（漏了它会误报「数据缺失」）"),
    ("src/domain/analysts 的 system_prompt",
     "该 Agent 的 prompt 有没有教它怎么用这个指标"),
)


#: 同义词池的**进程内缓存**（`metric_aliases()` 是静态字典，读一次即可）。
_ALIAS_EXPANSION: dict[str, tuple[str, ...]] | None = None


def _alias_expansion() -> dict[str, tuple[str, ...]]:
    """`关键词 → 等价词集合`（复用 `catalog/synonym_dict.py`，**不另造一套**）。

    ★ 2026-09-30（`CHG-0136`）实测报障：用户问"未来半年能否持有高股息的招商银行"，
      而 A11 金融风险的白名单写的是**中文**「市净率」，库里/计划里的 id 是**英文**
      `PB:600036` ⇒ `_filter_points_for_agent` 的**子串匹配**一条都放不过去 ⇒
      **采了白采**（该指标本次采集成功，却没有任何 Agent 看得到它）。

      同义词池早就在 `synonym_dict.metric_aliases()`（154 条，含
      `'市净率' -> ('pb','PB')`、`'市盈率' -> ('pe_ttm','pe','PE(TTM)')`、
      `'股息率' -> ('dv_ratio','dividend_yield','股息率','dv_ttm')`），
      这里只是**把它接进匹配**。取不到就退化为原子串匹配（增强坏了不该拖垮采集）。
    """
    global _ALIAS_EXPANSION
    if _ALIAS_EXPANSION is None:
        try:
            from src.infrastructure.catalog.synonym_dict import metric_aliases

            table: dict[str, tuple[str, ...]] = {}
            for canonical, aliases in metric_aliases().items():
                pool = {str(canonical).lower()}
                pool |= {str(a).lower() for a in aliases}
                table[str(canonical).lower()] = tuple(sorted(pool))
            _ALIAS_EXPANSION = table
        except Exception:  # noqa: BLE001 同义词池读不到 → 退化为子串匹配
            logger.warning("同义词池不可用，白名单不做语义扩张", exc_info=True)
            _ALIAS_EXPANSION = {}
    return _ALIAS_EXPANSION


#: ASCII 字母/数字（用于"词边界"判断）。**规则本体不在这里** ——
#: 见下面 `_kw_hit` 的说明：全仓库只有一处实现。
_ASCII_ALNUM = re.compile(r"[a-z0-9]")


def _kw_hit(ind_lower: str, kw: str) -> bool:
    """一个关键词是否命中指标名。

    ⚠️ **ASCII 别名按"词边界"匹配**：`'市盈率'` 扩张出 `pe`，而
    `ind:penetration:AI大模型应用` 里恰好含 `pe`（**penetration**）——
    纯子串会让 A11 把"AI 渗透率"也吞进来（白花 token）。
    中文别名保持子串语义（中文没有词边界问题，且指标名里常带后缀）。

    ## ★★ 边界规则只有一处实现（2026-10-01 收敛，`CHG-0155`）

    本函数原先**自己实现**了一套词边界判据，而 `catalog/synonym_dict.py`
    里还有另一套（`ascii_full_word` 的前身）——**两份实现、两套规则**。
    实测分歧：`fed:` / `cal:` / `ind:` / `mkt:` / `roe` 这类别名
    **一条路径认得出、另一条认不出**（`scripts/_audit_matching_layer.py` part ②）。

    现在本函数只做**域适配**（中文走子串、ASCII 委托给唯一实现），
    规则本体在 `synonym_dict.ascii_full_word()`。判据断言"两条路径调的是同一个函数"
    （用探针替换那个函数、看两处是否都被点到），而**不是**只比对两边的返回值 ——
    值相等但两份拷贝照样会漂，那正是这次缺陷的成因。
    """
    if not kw:
        return False
    if not kw.isascii():
        return kw in ind_lower
    from src.infrastructure.catalog.synonym_dict import ascii_full_word

    return ascii_full_word(ind_lower, kw)


def _filter_points_for_agent(agent_id: str, points: list[dict[str, Any]],
                             *, fallback_limit: int = 200) -> list[dict[str, Any]]:
    """按 Agent ID 白名单过滤 validated_points（避免无关数据占 token）。

    ## ★ 关键词先做**语义扩张**再匹配（`CHG-0136`）

    白名单是人写的（中文概念词），而 id 来自登记表/连接器（多为英文 en_id）。
    两边用**同一个同义词池**对齐：`市净率 → pb/PB`、`市盈率 → pe/pe_ttm/PE(TTM)`、
    `股息率 → dv_ratio/dividend_yield/dv_ttm`。

    策略：
      1. 命中白名单（**含等价词**）的 indicator：保留全部（不要在过滤时丢上下文）；
      2. 白名单为空（未配置该 Agent）：保留前 fallback_limit 条 + 提示日志；
      3. 过滤后剩 0 条：兜底取前 fallback_limit 条（防止 Agent 完全无输入）。

    Returns:
        过滤后的 data_points 列表（保持原序）。
    """
    keywords = _AGENT_DATA_WHITELIST.get(agent_id)
    if not keywords:
        # 该 Agent 未配置白名单（info/info_agents等），按 fallback 截断
        return list(points[:fallback_limit])

    # ★ 语义扩张：把每个关键词换成"它自己 + 同义词池里的等价词"
    alias = _alias_expansion()
    expanded: set[str] = set()
    for k in keywords:
        expanded.add(k)
        expanded.update(alias.get(str(k).lower(), ()))
    kws = tuple(expanded)

    matched = []
    for p in points:
        ind = str(p.get("indicator", ""))
        ind_lower = ind.lower()
        if any(_kw_hit(ind_lower, kw.lower()) for kw in kws):
            matched.append(p)
    # 白名单过滤后 0 条：兜底（防 Agent 拿空输入做幻觉），但**日志告警**
    # ★ 2026-09-27 修复：重构到模块级时这行兜底 return 丢失了 ——
    #   docstring 承诺"0 条→取前 fallback_limit 条"，实际却只告警返回空列表，
    #   下游 Agent 全部走"无数据拒绝分析"路径、直接跳过 LLM 调用
    #   （test_full_graph_pipeline 抓到：A10 期望 3 次网关调用只发生 2 次）。
    if not matched and points:
        logger.warning(
            "Agent %s 的白名单过滤后 0 条数据点（fallback 取前 %d 条），"
            "请检查 _AGENT_DATA_WHITELIST 配置", agent_id, fallback_limit)
        return list(points[:fallback_limit])
    return matched


def _ensure_item_ids(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """给缺 `item_id` 的信息条目按序补一个稳定 ID（与 A05 的规则同款）。

    ★ 2026-09-28 修复第九轮并行化引入的回归：原先 item_id 由 A05 的
    `_parse_items` 顺带分配（`info_{index+1}`），A06 串行排在 A05 之后、
    拿到的是已带 ID 的条目。并行化后 A06 直接读**原始** info_items ——
    条目可能没有 item_id，A06 的事件全部因"item_id 不可溯源"被丢弃
    （test_news_graph_info_pipeline 抓到：stats.total 期望 1 实际 0）。

    在 fan-out 之前统一补齐：A05（自带同款规则）与 A06 拿到的 ID
    按同一顺序、同一规则生成，保持一致 —— A05 的 reviews 与 A06 的
    events 才能对上同一条目。
    """
    out: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        entry = dict(item)
        if not str(entry.get("item_id") or "").strip():
            entry["item_id"] = f"info_{index + 1}"
        out.append(entry)
    return out


def _news_focus_codes(state: ResearchState) -> list[str]:
    """★ 要**逐只取新闻**的 6 位代码（`focus_stock_codes`，保序去重）。

    ## 为什么需要它（用户 2026-10-08 报障的"舆情版"）

    采集节点原来只读**单值** `state["target"]`（`re.fullmatch(r"\\d{6}", target)`）
    ⇒ 一条问两只票的问句里，**第二只票的新闻一条都不取**，
    A05/A06/A07 拿到零输入整体空跳过 —— 用户看到"该股近期无消息"，
    而真相是**根本没去取**（与"宁波银行没有任何可引用的估值"同一形状）。

    ⚠️ 只认 6 位数字代码：名称/主题词交给既有的 `fetch_topic_news` 那条路
    （不在这里混两种语义）。拿不到任何代码 → 返回 `[]`，调用方走原文路径。
    """
    out: list[str] = []
    for raw in (state.get("focus_stock_codes") or ()):
        code = str(raw or "").strip()
        if re.fullmatch(r"\d{6}", code) and code not in out:
            out.append(code)
    return out


async def _fetch_news_per_code(news_fetcher: Any, codes: list[str]) -> list[dict]:
    """**逐只** `fetch_news(code)`，并把代码标到每条 `item["stock_code"]` 上。

    ## 为什么必须标代码

    下游 A07 原来是**把所有事件混算成一个情绪分**（`info/sentiment/logic.py`）
    ⇒ 两只票的利好/利空互相抵消，用户读到的"情绪偏暖"不知道是针对哪只。
    代码随条目下发后：A06 把它带到事件上、A07 按代码分组出分。

    ## 取数失败的边界

    单只取失败（返回空）**不影响**另一只 —— 逐只循环，一只一条都不丢地累加。
    `stock_code` 用**我们要的那个 6 位代码**覆盖（上游 `news_df_to_items` 用
    东财的"关键词"列填这个字段，值可能是**股票名**，不能当代码用）。
    """
    items: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for code in codes:
        got = await news_fetcher.fetch_news(code)
        for raw in got or ():
            if not isinstance(raw, dict):
                continue
            entry = {**raw, "stock_code": code}
            key = (code, str(entry.get("source_url") or ""),
                   str(entry.get("title") or entry.get("text") or "")[:120])
            if key in seen:
                continue
            seen.add(key)
            items.append(entry)
    return items


def _verified_texts(state: ResearchState, *, limit: int = 10) -> list[str]:
    """从 verified_items 取可信原文（A08-A12 通用）。模块级函数，
    不依赖任何闭包状态，便于单元测试。

    ★ 行为与原闭包内版本对齐：A05 把 LLM verdict 合并进 items，
    `verified=True` 才放行，"拒绝"剔除。
    """
    verified = state.get("verified_items") or {}
    items_review = verified.get("items") or []
    trusted = {
        str(e.get("item_id"))
        for e in items_review
        if isinstance(e, dict) and e.get("verified") is True
    }
    if not trusted:
        return []
    texts: list[str] = []
    for entry in items_review:
        if not isinstance(entry, dict) or str(entry.get("item_id")) not in trusted:
            continue
        title = str(entry.get("title") or "").strip()
        body = str(entry.get("text") or "").strip().replace("\n", " ")
        source = str(entry.get("source_name") or "").strip()
        when = str(entry.get("publish_time") or "").strip()
        head = f"【{source} {when}】{title}".strip()
        snippet = body[:300] if body else title
        texts.append(f"{head} {snippet}".strip())
        if len(texts) >= limit:
            break
    return texts


def ask_agent_tool_description(available_agents: list[str]) -> str:
    """★ `ask_agent` 工具描述的唯一构造点（抽成模块级，为的是**可判据**）。

    ## 为什么必须抽出来（`CHG-0192` 的真实缺陷）

    原实现把描述写在 `tools.register(...)` 的参数里，其中**手写**了
    "每个Agent最多追问2次"；而代码常量是 `MAX_ASKS_PER_AGENT = 1`。
    同一件事两个数 —— 工具描述**会进 LLM 的 system prompt**，
    于是模型按 2 次去试、第二次被拒，而它不知道为什么（描述与行为不符）。

    抽成工厂之后，`tests/unit/test_ask_agent_tool_contract.py` 可以**直接调它**，
    断言"描述里的数字 == 常量值"；把 `{MAX_ASKS_PER_AGENT}` 改回手写数字即红。
    """
    return (
        f"向指定上游分析Agent提问。可用Agent：{', '.join(available_agents)}。"
        "参数receiver=上述agent_id之一, question=具体问题。"
        f"规则：每个Agent最多追问{MAX_ASKS_PER_AGENT}次，"
        "不要对同一Agent重复提出相同或"
        "高度相似的问题；数据层Agent(A01-A04)不可提问，指标数值用query_data。"
    )


def _build_analyze_payload_fn(agent_id: str):
    """★ 2026-09-27 第八轮：分析层 payload_fn 工厂（−2万 tokens_in/轮）。

    原 `_payload_fn` 是闭包内 lambda，绑死了 `ALL_INSIGHT_AGENTS` 等；
    抽到模块级后：
      · 单测可独立验证"某个 agent_id 的过滤行为"
      · 不再每次构建 StateGraph 都重建 N 个闭包
      · 与 `_filter_points_for_agent` 形成完整链路（模块级 helper）
    """
    def _payload_fn(state: ResearchState) -> dict[str, Any]:
        all_points = state.get("validated_points", []) or []
        focus = state.get("target_display") or state["target"]
        hint: dict[str, Any] = state.get("analysis_hint", {})
        # ★ 2026-09-29：行业 Agent 的**归属判定**（§19.16.5 第 1 条）。
        #   规划期已按标的行业裁过一轮，但"问句点名了某个行业"时那个 Agent
        #   会被保留 —— 此时它需要一个机器可判的信号来**改写措辞**（而不是
        #   让 LLM 自由发挥出「非本框架覆盖标的」那句免责话术）。
        #   判定只在这一处做（`INDUSTRY_KEYWORDS` 是路由的单一事实源），
        #   领域层只渲染，不重新判定；`hint` 是浅拷贝，**不改 state 里的那份**。
        scope = industry_scope_for(
            agent_id, f"{focus} {state.get('user_query', '')}",
            str(state.get("focus_stock_code") or ""))
        if scope is not None:
            hint = {**hint, "industry_scope": scope}
            # ★★ 2026-09-29（真实端到端跑完才补的第二层）：**只禁措辞不够**。
            #   实测：A13/A14 拿到「600036 + 一句禁令」后**照样**写
            #   「600036 属银行、不在本次科技行业数据覆盖内…无法给出可验证的持有结论」。
            #   根因是**焦点本身就错了** —— 它们被要求"分析 {focus}"，而 focus
            #   是一只不属于它们行业的票。所以把焦点换成**它们自己的行业**：
            #   用户点名了那个行业 ⇒ 他要的就是这个行业的视角，不是这只票的。
            #   于是禁令从"别说那句话"变成"**没有理由**说那句话"。
            if scope.get("self_named"):
                focus = f"{scope['agent_industry']}行业"
        # ★★ 2026-09-30：**宏观必查项与口径随数据下发**（用户口径：
        #   「把美债收益率/失业率作为宏观结论的必查项写进**数据侧的组装逻辑**，
        #   而不是写进 prompt」）。走既有的 `hint` 通道（与 `valuation_calc` /
        #   `red_flag_calc` / `compliance_calc` 同一条 —— 数据侧算好、Agent 直接读），
        #   所以 **prompt 一个字都不用改**。
        if agent_id == "A08_macro":
            _pts = _filter_points_for_agent(agent_id, all_points)
            got = {str(p.get("indicator", "")) for p in _pts}
            required = list(_MACRO_INDICATORS) + list(_US_RATES_INDICATORS)
            hint = {
                **hint,
                #: 口径（跟着**实际拿到的**数据走：没拿到的不讲口径）
                "macro_basis": macro_basis_for(sorted(got)),
                #: 必查项里**没取到的**（机器可读）—— Agent 据此如实声明缺口，
                #: 而不是静默少写一维（"没量到" ≠ "量到 0"）。
                "macro_required_missing": [i for i in required if i not in got],
                "macro_required_total": len(required),
                "macro_required_got": len([i for i in required if i in got]),
            }
        # ★★ 2026-10-08：**多行业问句的行业清单随数据下发**（A09_meso 消费）。
        #
        # 现场（同一条用户报障）：一条问句里同时有「煤炭开采」（601088）与
        # 「银行」（宁波银行）时，A09 的输出契约只有**一组**
        # `industry_cycle`/`chain_position` ⇒ 它只写一个行业，另一个行业的
        # 中观结论**不存在**（用户读到"只有中国神华有行业结论"）。
        #
        # 为什么在编排层算：`resolve_focus_industries` 是 `CHG-0217` 的**唯一**
        # 多值实现（与挂 Agent 的判据同源）；领域层只**渲染**，不重新解析
        # （两份解析必然漂移，而漂移的表现是"挂的是 A、写的是 B"，不报错）。
        #
        # ⚠️ **只在 ≥2 个行业时才写这个键** —— 单行业时 `hint` 与修复前
        #    逐字相同 ⇒ A09 的 prompt（含 `json.dumps(hint)` 那段）也逐字相同，
        #    "单行业逐字不变"是**结构保证**而不是靠断言维持。
        if agent_id == "A09_meso":
            _multi_industries = [
                name for name, _how in resolve_focus_industries(
                    f"{state.get('target') or ''} {state.get('user_query') or ''}")
                if name]
            if len(_multi_industries) >= 2:
                hint = {**hint, "focus_industries": _multi_industries}
        return {
            "focus": focus,
            "user_query": state["user_query"],
            "data_points": _filter_points_for_agent(agent_id, all_points),
            "hint": hint,
            "events": (state.get("extracted_events") or {}).get("events", []),
            "verified_texts": _verified_texts(state),
        }
    return _payload_fn


async def _liquidity_ctx_node(state: ResearchState) -> dict[str, Any]:
    """流动性周期 skill：基于已校验数据点本地计算（量能/换手/两融/估值/FedWatch）。

    ★ 2026-09-27 第八轮：从 build_research_graph 大闭包抽到模块级。
    不依赖任何外部状态（agents 等），纯函数式 ——
    可以直接 `await _liquidity_ctx_node(state)` 单测。

    输出 `analysis_hint.market_liquidity`，分析层(A08-A16)与 A17 共享同一份量化参考；
    无流动性类数据点时空跳过；纯计算失败不阻断整图。
    """
    pts = state.get("validated_points", [])
    if not any(str(p.get("indicator", "")).startswith(("mkt:", "idx_val:", "fed:"))
               for p in pts):
        return {}
    from src.domain.skills.liquidity_cycle import assess_liquidity

    try:
        assessment = assess_liquidity(pts)
    except Exception as exc:  # noqa: BLE001
        return {"errors": [f"liquidity_cycle_skill: 意外异常 {exc}"]}
    return {
        "analysis_hint": {"market_liquidity": assessment},
        "progress": [
            "流动性周期研判完成（量能分层/三市分项/市场宽度/板块估值分位/"
            "换手率/两融/FedWatch）"
        ],
    }

# 主题新闻关键词组（stock_info_global_em 最近约200条全球快讯中过滤）：
# 宏观问题只取与问法直接相关的词；行业命中后放开整组，以覆盖"细分环节"类提问
TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "macro_core": ("美联储", "加息", "降息", "美股", "美债", "非农", "通胀", "PCE",
                   "欧央行", "日本央行", "关税", "衰退", "利率决议", "鲍威尔", "就业数据",
                   # 补齐（2026-09-26）：用户原话常常只用这些词，
                   # 缺了它们会让"主题关键词为空 → 不拉任何新闻 → 信息层整体空跳过"。
                   # 实测故障：问题里写了"美国说年内还会再加息一次"，
                   # 但"加息"是"再加息"的子串、且没提"美联储"，
                   # 旧实现按**精确词**匹配导致命中为空，A05/A06/A07 全部拿到零输入。
                   "利率", "联邦基金", "FOMC", "货币政策", "缩表", "美债收益率",
                   "美联储主席", "点阵图", "CPI", "核心CPI", "失业率", "就业",
                   "经济数据", "加息预期", "降息预期", "美元", "美股三大指数"),
    "A13_tech": ("人工智能", "AI", "算力", "芯片", "半导体", "光模块", "CPO", "服务器",
                 "液冷", "PCB", "存储芯片", "消费电子", "数据中心", "国产替代", "先进封装",
                 "HBM", "铜缆", "机器人", "大模型",
                 # 补齐：用户说"AI产业链""产业链龙头"时也应命中科技组
                 "产业链", "AI产业链"),
    "A14_consumer": ("消费", "白酒", "家电", "零售", "食品饮料", "免税", "旅游", "餐饮"),
    "A15_cyclical": ("煤炭", "有色", "钢铁", "化工", "石油", "原油", "铜价", "铝价",
                     "锂电", "航运", "天然气"),
    "A16_pharma": ("医药", "创新药", "医疗器械", "中药", "疫苗", "CXO", "集采",
                   "GLP-1", "减肥药"),
}

# 行业专业指标目录。ind: 前缀下的每一条现在都对应**真实互联网数据源**
# （科技：WSTS/统计局/中证；消费周期医药PE：中证指数官网；白酒价格：酒排名；
# 电厂煤炭库存：中电联CECI；创新药IND：CDE药审中心）——原先占位的
# `MockIndustryConnector` 已随最后一条模拟指标退役而整体删除。
INDUSTRY_INDICATORS: dict[str, tuple[str, ...]] = {
    "A13_tech": (
        "ind:半导体销售额同比", "ind:芯片出货量同比", "ind:科技行业PE(TTM)",
    ),
    "A14_consumer": (
        "ind:社会消费品零售总额同比", "ind:白酒批价(元/瓶)", "ind:消费行业PE(TTM)",
    ),
    "A15_cyclical": (
        "ind:动力煤价格(元/吨)", "ind:重点电厂煤炭库存(万吨)", "ind:周期行业PE(TTM)",
    ),
    # 注：原 `ind:医保集采药品均价同比` 已退役 —— 集采是**离散事件制**（每批一次
    # 开标、约6~12个月一批），不存在"连续月度均价同比"这种真实序列；
    # 官方公告从第10批(2024-12)起连整体降幅都不再公布，免费公开源无替代，
    # 故按"宁缺口不造假"直接删除该指标，而不是继续合成。
    "A16_pharma": (
        "ind:创新药IND申报数量(个)", "ind:医药行业PE(TTM)",
    ),
    # ★ 2026-09-29：A20 兜底行业 Agent 的产业指标**故意为空**。
    #   它服务任意行业（银行/非银/公用/交运…），指标在 Agent 内部按解析出的
    #   行业名从 payload 里挑（申万截面按 `extra.industry_name` 命中）。
    #   在这里写死任何指标都会让它只对某一个行业有效 —— 那就不是兜底了。
    "A20_generic_industry": (),
}

# analysis_type → (采集指标, 参与分析Agent)
# 个股指标带代码后缀（PE(TTM)/PB/财务比率由AkShare提供，喂饱A10/A11）：
# plan_run 中统一解析为 "{indicator}:{target}"。
_CODE_SUFFIX_INDICATORS = frozenset(
    {"stock_close", "PE(TTM)", "PB", "资产负债率", "流动比率",
     # ★ 2026-09-29 补 8 条：这些是**个股类**指标，裸名没有任何连接器 supports。
     #   为什么必须在这里（而不是只在 `_SIGNAL_AGENT_INDICATORS` 里列名字）：
     #   `_needs_code_suffix()` 就是查这张表；不在表里 ⇒ 增补出来的名字
     #   **不带代码后缀** ⇒ A01 去 fetch 一个没人 supports 的裸名 ⇒
     #   必然失败，而用户看到的是"该股没有股息数据"。
     #   ——**这与 2026-09-26 那条 `PE(TTM)` 故障链是同一个形状**，
     #   区别只是这次裸名是我们自己新加的，所以更容易漏。
     #   实测踩过：加了 `_SIGNAL_AGENT_INDICATORS` 却没加这张表，
     #   探针按 `'600036' in ind` 过滤时**看不出来**（裸名里没有代码）。
     "股息率TTM", "股息率", "ROE",
     "商誉占净资产比", "货币资金占总资产比", "有息负债占总资产比",
     "大股东质押比例", "对外担保占净资产比",
     # ★ 2026-09-29：**平台自有后端数据**的 5 族（`PlatformDataConnector`，个股类）。
     #   与上面那几条同一个理由：这些名字**没有裸名连接器**，
     #   不进这张表 ⇒ `_needs_code_suffix()` 判 False ⇒ 增补出来的是**裸名**
     #   ⇒ A01 去 fetch 一个没人 supports 的名字 ⇒ 必然失败，
     #   而用户看到的是"该股没有估值水位/没有解禁计划"。
     #   实测踩过同款：加了 `_SIGNAL_AGENT_INDICATORS` 却没加这张表，探针看不出来。
     "估值水位", "概念拥挤度", "主线告警", "个股告警", "解禁计划"})

#: 目录里**声明了"需带代码后缀"**的指标名 —— **现读** `planner.INDICATOR_CATALOG`。
#:
#: ## 为什么必须派生（2026-09-29 实测到的漂移）
#:
#: 目录与 `_CODE_SUFFIX_INDICATORS` 是**两处**表达同一件事，而它们已经漂移：
#: 目录里 **40 条**写着"（需带代码后缀）"，而那张集合只列了 24 个。
#: 后果实测（用户报障里的一条）：LLM 照菜单选了 `每股净资产`，
#: `_needs_code_suffix()` 判它"不需要后缀" ⇒ **裸名进计划** ⇒
#: `A01采集失败(每股净资产): 无连接器支持指标 每股净资产`。
#:
#: 现在两张表**求并集**，并加护栏
#: （`test_indicator_suffix_contract.py::test_catalog_suffix_declarations_are_enforced`）
#: —— 目录里再写"需带代码后缀"，判据必须同时认它，**不许再漂**。
_CATALOG_CODE_SUFFIX_ROOTS: frozenset[str] = frozenset(
    str(item.get("id") or "").split(":", 1)[0]
    for item in INDICATOR_CATALOG
    if "需带代码后缀" in str(item.get("desc") or "")
)

#: 用户可见的"采集未取到"说明的**长度上限**（字符）。
#:
#: 为什么要有上限（用户 2026-09-29 报障）：「下次还会出现**一堆报错信息**」——
#: 实测那条 `A01采集失败(us_cpi_yoy:300068)` 后面跟着**整份已注册指标清单**
#: （~150 条、几千字），把「部分节点异常」面板刷成一屏注册表。
#: **超长内容按"本地没查到"处理**，只留一句有界说明 + 缺口日志。
USER_ERROR_MAX = 160

#: 连接器**特意产出**的"该口径对这个主体不适用"标记（例如银行没有流动比率）。
#:
#: 为什么按**字面标记**判而不是按自然语言猜：这是取数侧**自己写下的结论**
#: （`akshare_connector` 在源表该实体为空且语义成立时产出），属机器可读标识；
#: 猜语义会把它误判成"取数失败"（实测用户面板上就是这么显示的）。
NOT_APPLICABLE_MARKERS: tuple[str, ...] = (
    "该实体无值",          # AkShare 财务比率族：源表里这只票该字段为空
    "对该主体不适用",
    "不适用（非缺陷）",
)

#: 「该**专题表**未收录这个主体」的标记（`CHG-0135`）。
#:
#: 与"不适用"分开，是因为**对客户的说法不同**：
#:   · 不适用：「银行没有流动比率」——这个口径对这类主体**根本不存在**；
#:   · 未收录：「这张专题表只收录有质押/担保公告的公司，招行窗口内没有」——
#:     口径存在、只是**这张表不含它**（且『不在表内』≠『取值为 0』）。
#: 两者处置相同（都**不是故障**、都**不联网硬试**），但**文案必须不同** ——
#: 否则客户会把"表里没有这家公司"读成"这家公司没有质押/担保"（正好读反）。
#: 字面标记同样是**取数侧自己写下的结论**（`NoApplicableData` 产出），不是猜语义。
NOT_COVERED_MARKERS: tuple[str, ...] = (
    "未收录该主体（非缺陷）",
)


def format_gap_note_for_log(indicator: str, exc: object) -> str:
    """采集异常 → **有界的** `[采集缺口]` 日志行（与用户可见文案同源）。

    用 `logic.format_gap_log` 的固定形状（可 grep），`reason` 由 `brief()` 截断
    —— 一屏注册表那种长度**不允许**进日志，更不允许进用户面板。
    """
    from src.core.errors import brief
    from src.domain.agents.data.collector.logic import format_gap_log

    return format_gap_log(indicator, brief(exc, USER_ERROR_MAX), kind="error")


def collection_gap_note(
    indicator: str, reason: object, *, stage: str = "local",
) -> str:
    """采集未取到的**唯一**文案（有界、措辞是"缺口"而不是"故障"）。

    用户口径（2026-09-29）：
    > 「对于数据内容超长或报错的，应该**默认为数据未在本地查询到**，
    >   自动转联网搜索获取，而不是**报异常故障**」

    所以措辞按阶段分三档，且**一律截断**：

    | `stage` | 文案 | 语义 |
    |---|---|---|
    | `local` | `…本地未取到（{reason}）→ 已转联网搜索` | 处理中：正在联网找 |
    | `online` | `…本地与联网均未取到（已登记缺口，不阻断本轮）` | 结论：**缺口**，不是故障 |
    | `timeout` | `…超时未取到（防撞钟 {reason}）→ 已转联网搜索` | 慢，不是坏 |

    ⚠️ 前缀保留 `A01_data_collector({ind})` 形状：既有契约
    （`tests/integration/test_supervisor_graph.py::test_collector_error_is_contained`）
    断言"采集失败必须出现在 `errors` 里"——**可见性不能丢，只是不再吓人**。
    """
    from src.core.errors import brief

    text = brief(reason, USER_ERROR_MAX - len(indicator) - 60)
    if stage == "not_covered":
        # ★ 2026-09-30（`CHG-0135`）：口径存在、但**这张专题表不含该主体**。
        #   必须与"不适用"分开说：客户看到「未收录」不会读成「值为 0」，
        #   而看到「不适用」也不会以为系统坏了。
        return (f"A01_data_collector({indicator}): 该专题未收录本主体"
                f"（≠ 取值为 0，非缺陷）（{text}）")
    if stage == "not_applicable":
        # ★ 用户报障现场：「600036 的 '流动比率' 在源表中**该实体无值**…
        #   这通常是**语义正确**而非缺陷」—— 它被当成"节点异常"报出来了。
        #   这类**不是故障**：措辞照实说，且不进异常面板（调用方据此分流）。
        return (f"A01_data_collector({indicator}): 该口径对本主体不适用（非缺陷）"
                f"（{text}）")
    if stage == "online":
        return (f"A01_data_collector({indicator}): 本地与联网均未取到"
                f"（{text}；已登记缺口，不阻断本轮）")
    if stage == "timeout":
        return (f"A01_data_collector({indicator}): 超时未取到"
                f"（{text}）→ 已转联网搜索")
    return (f"A01_data_collector({indicator}): 本地未取到"
            f"（{text}）→ 已转联网搜索")


#: 中国宏观四件套 —— **宏观问必须带上**（登记在 `configs/indicators.yaml`，连接器 supports）。
#:
#: ## 报障现场（用户 2026-09-29）
#:
#: > 「…以及AI应用加速失业率增加对消费的影响节奏…」的执行结果：
#: > 「**中国端：中国宏观数据缺失**」，而 `CPI`（229 行，最新 2026-09-01）、
#: > `PPI`（229 行）、`M2`（119 行）、`社融`（115 行）**都躺在库里**。
#:
#: 根因**不在数据源、也不在白名单**：`_PLANNING["macro"]` 里有这两条，
#: 但**真正跑的是 LLM 规划那条路** —— 实测同一条问句的 LLM 计划里
#: **一条中国宏观都没有**（20 条全是 `us_*` / `fed:*` / 个股）。
#: 于是 `A08` 的 `_latest_numeric(pts, "CPI")` 拿到 None ⇒ 模板输出
#: 「中国宏观数据缺失」。**"数据在库里"与"计划里有它"是两件事。**
#:
#: 修法与流动性/美国宏观一致：**确定性的补**（不指望 LLM 记得）。
_MACRO_INDICATORS: tuple[str, ...] = ("CPI", "PPI", "M2", "社融")

#: ★★ 2026-09-30：**美国/利率侧的必查项**（用户口径：「把**美债收益率/失业率**作为
#: 宏观结论的**必查项**写进**数据侧的组装逻辑**，而不是写进 prompt」）。
#:
#: 为什么必须是这一组 id：
#:   · `us_*` 那五条（`us_unemployment`/`us_nonfarm`/`us_pce`/`us_core_cpi`/
#:     `us_fed_rate`）的源（AkShare 东财 `macro_usa_*`）**已停更** ⇒ 已标
#:     `enabled: false` 停产 ⇒ **不许再排进计划**（排了就是自己制造缺口）；
#:   · 替代者全部来自实测可达的 `fred:` 序列与 `fed:` 政策利率族：
#:     `fred:UNRATE` 失业率 · `fred:DGS10` **10 年期美债收益率**（用户点名）·
#:     `fred:T10Y2Y` 期限利差（衰退信号）· `fred:PAYEMS` 非农水平值 ·
#:     `fred:CPILFESL`/`fred:PCEPILFE` 核心 CPI/PCE **指数** ·
#:     `fed:target_upper`/`target_lower`/`effr` 政策利率三条**单值序列**。
_US_RATES_INDICATORS: tuple[str, ...] = (
    "fred:DGS10", "fred:T10Y2Y", "fred:UNRATE", "fred:PAYEMS",
    "fred:CPILFESL", "fred:PCEPILFE",
    "fed:target_upper", "fed:target_lower", "fed:effr",
)

#: 这些序列的**口径**（随数据下发，**不写进 prompt** —— 见 `macro_basis_for()`）。
#:
#: 为什么要有它：prompt 里原先只用注释写着「PMI 是制造业口径 / GDP 是累计值」，
#: 而**注释不会到达模型**（`macro/agent.py` 的注释自己就写着"不写进 prompt 也要知道"）
#: ⇒ 模型看到 `fred:CPILFESL = 337.765` 只会当"一个数"，不知道那是**指数**、
#: 不能当"核心 CPI 同比"读。口径属于**数据属性**，就该跟数据一起走。
_MACRO_BASIS: dict[str, str] = {
    "fred:DGS10": "10 年期美债收益率，**水平值 %**（不是涨跌幅）；日频，节假日无值",
    "fred:T10Y2Y": "10 年−2 年期限利差，**百分点**；为负=倒挂（衰退常见前兆）",
    "fred:UNRATE": "美国失业率，**水平值 %**（月度）",
    "fred:PAYEMS": "美国非农就业**水平值（千人）**，**不是「新增」**；"
                   "新增 = 本月 − 上月（需派生）",
    "fred:CPILFESL": "美国核心 CPI **指数**（不是同比 %）；同比需自行按同期计算",
    "fred:PCEPILFE": "美国核心 PCE **指数**（不是同比 %）；同比需自行按同期计算",
    "fed:target_upper": "联邦基金目标利率**上限** %（单值序列）",
    "fed:target_lower": "联邦基金目标利率**下限** %（单值序列）",
    "fed:effr": "有效联邦基金利率（EFFR）% —— **政策利率的实测值**",
    "CPI": "中国 CPI **同比 %**（月度）",
    "PPI": "中国 PPI **同比 %**（月度）",
    "M2": "中国 M2 **同比 %**（月度）",
    "社融": "社会融资规模**增量（亿元）**，月度累计口径见源；⚠️ 源侧停更（最新 2026-04）",
    "PMI": "**制造业** PMI（50 = 荣枯线）",
    "GDP": "GDP **累计值（亿元）**；`GDP:同比` 是累计同比 %",
}


def _record_collection_anomaly(
    indicator: str, stage: str, reason: object, *, task_id: str = "",
) -> None:
    """把一次采集缺口/超时记到**管理员侧**（用户不可见）。

    单一实现在 `core/collection_anomalies.py`（有界 + 去重 + 落登记的 `run_dir`）；
    这里只做"stage → kind"的映射。**绝不影响主链路**（内部吞掉一切异常）。
    """
    try:
        from src.core.collection_anomalies import (
            KIND_ERROR,
            KIND_GAP,
            KIND_TIMEOUT,
            record,
        )

        kind = {"timeout": KIND_TIMEOUT,
                "error": KIND_ERROR}.get(str(stage), KIND_GAP)
        record(kind, indicator, str(reason or ""), task_id=task_id,
               source="A01_data_collector", extra={"stage": str(stage)})
    except Exception:  # noqa: BLE001 观测失败不影响采集
        pass


def macro_basis_for(indicators: list[str]) -> dict[str, str]:
    """给一组宏观指标取**口径说明**（只返回有登记的，未知的如实不返回）。

    ★ 为什么是函数而不是常量直用：口径要**跟着实际拿到的数据走**
    （`fred:PAYEMS` 没取到时不该讲它的口径），而且调用方是数据侧组装
    （payload builder），不是 prompt。
    """
    out: dict[str, str] = {}
    for ind in indicators:
        bare = str(ind).split(":")[0] if not str(ind).startswith("fred:") \
            else str(ind)
        note = _MACRO_BASIS.get(bare)
        if note:
            out[str(ind)] = note
    return out

#: 命中这些词就按"宏观口径"补齐四件套（问句/标的文本）。
_MACRO_TOPIC_KEYWORDS: tuple[str, ...] = (
    "宏观", "CPI", "PPI", "M2", "社融", "通胀", "通缩", "货币政策",
    "经济数据", "GDP", "PMI", "加息", "降息", "美联储", "FOMC",
    "衰退", "滞胀", "美林", "经济周期",
)

#: ★ 2026-09-30：**美国/利率侧自己的触发词**（用户点名的"美债收益率/失业率"在内）。
#:
#: 为什么必须单列一组：原先只有 `_MACRO_TOPIC_KEYWORDS`，而它**没有**
#: 「美债」「失业率」「非农」「PCE」这些词 —— 实测「美债收益率与失业率怎么看」
#: 一个关键词都命中不了 ⇒ 必查项**一条都不补**（用户点名要的那两条恰好漏掉）。
#: 这是"判据看不见"的典型：数据可达、白名单放行、就是没人把它排进计划。
_US_RATES_KEYWORDS: tuple[str, ...] = (
    "美债", "美债收益率", "国债收益率", "收益率曲线", "期限利差", "利差",
    "失业率", "非农", "就业数据", "核心CPI", "核心PCE", "PCE",
    "美元指数", "美元", "点阵图", "联邦基金",
)


def ensure_macro_indicators(
    analysis_type: str, route_text: str, resolved: list[str],
) -> list[str]:
    """宏观口径的问句**补上中国宏观四件套**（幂等、确定性、不依赖 LLM）。

    触发条件（任一）：`analysis_type == "macro"`，或文本命中 `_MACRO_TOPIC_KEYWORDS`。
    为什么用"或"而不是只看 analysis_type：实测 LLM 会把宏观问判成
    `full`/`industry`（它只看"问了几件事"），而"对A股的影响"这类问题
    仍然需要中国宏观那一端。

    ⚠️ 只**补**不删（与 `append_liquidity_indicators` 同纪律）：
    已经计划的指标一个都不动。
    """
    text = route_text or ""
    low = text.lower()
    # ★★ 2026-09-30：**资讯（news）管线一条宏观数据都不补**。
    #   news 是**信息层**管线（A05/A06/A07 串行链，A01–A04 整体跳过），
    #   补指标会把采集节点拉起来 —— 这条纪律在 `CHG-0093` 就定过
    #   （`augment_plan_by_query_signals` 的 `collects_data` 闸门），
    #   本轮加"美国/利率侧触发词"时**差点从另一个入口破掉它**
    #   （实测 `test_news_graph_info_pipeline` 立刻红：A01 又出现在 news 链路里）。
    #   教训与 AGENTS.md 一致：**同一个判断多一个入口，就要多问一遍"这条闸还在不在"**。
    if analysis_type == "news":
        return list(resolved)
    hit = analysis_type == "macro" or any(
        kw.lower() in low for kw in _MACRO_TOPIC_KEYWORDS
    ) or any(kw.lower() in low for kw in _US_RATES_KEYWORDS)
    if not hit:
        return list(resolved)
    # ★★ 2026-09-30：宏观口径的问句**两个方向都补齐** ——
    #   ① 中国四件套（CPI/PPI/M2/社融）；② **美国/利率侧必查项**
    #   （美债收益率/失业率/非农/核心通胀指数/政策利率）。
    #   用户口径：「把美债收益率/失业率作为**宏观结论的必查项**写进**数据侧的
    #   组装逻辑**，而不是写进 prompt」。
    #   为什么必须"确定性补"而不是靠关键词+LLM：改前这里只在
    #   `analysis_type == "macro"` **且**命中 `_US_MACRO_KEYWORDS` 时才追加，
    #   而且追加的是 `us_*` **五条已停产**的 id ⇒ 排了也取不到（自己制造缺口）。
    wanted = list(_MACRO_INDICATORS) + list(_US_RATES_INDICATORS)
    return list(dict.fromkeys(
        list(resolved) + [i for i in wanted if i not in resolved]))


# 美国宏观关键词：query命中时自动追加美国宏观指标
_US_MACRO_KEYWORDS = ("美股", "加息", "降息", "美联储", "FOMC", "联邦基金",
                      "美债", "非农", "美国CPI", "美国通胀", "PCE", "鲍威尔",
                      "利率决议", "就业数据", "美国宏观")

# A股大盘流动性指标：行业/个股买卖与持有期研判时自动补齐（免费实时源）
_MARKET_LIQUIDITY_INDICATORS: tuple[str, ...] = (
    "mkt:turnover:total",       # 两市成交额（腾讯/东财实时，extra含沪/深/创/科分项）
    "mkt:turnover:hist",        # 60交易日序列→MA5/MA10/MA50
    "mkt:turnover_rate:all_a",  # 全A加权换手率+前5%成交集中度
    "mkt:margin_balance",       # 两融余额
    "mkt:margin_balance:hist",  # 两融10日序列
    "mkt:north_flow",           # 北向（停披期诚实缺口）
    "idx_val:snapshot:all",     # 核心宽基PE/PB分位
)
# 科创板/创业板板块指标：中观轮动研判必备（三市分项成交额/板块PE分位/市场宽度）
_BOARD_LIQUIDITY_INDICATORS: tuple[str, ...] = (
    "mkt:cybkcb:turnover:all",      # 创业板指/科创50/科创综指实时成交额
    "mkt:cybkcb:val:all",           # 双创板块PE及1/3/5年/全历史分位
    "mkt:cybkcb:spot_summary",      # 两板涨跌家数/上涨占比/中位涨幅（情绪温度）
)
# 全球/海外流动性关键词：命中时追加CME FedWatch（外网不可达会自动降级为空）
_GLOBAL_LIQUIDITY_KEYWORDS = (
    "美联储", "加息", "降息", "FOMC", "FedWatch", "联邦基金", "宏观流动性",
    "全球流动性", "外资", "美债", "美元流动性", "海外流动性",
)
# A股语境词：宏观问题中出现这些词时也需要A股流动性数据（含交易意图触发词）
_A_SHARE_LIQUIDITY_KEYWORDS = (
    "A股", "两市", "大盘", "成交额", "两融", "北向", "换手率", "估值分位",
    "仓位", "放量", "缩量",
    # —— 条件触发：大盘预测/复盘类问法（skill 1.1 第0节）——
    "复盘", "明日", "后市", "股市", "上证", "创业板", "科创板", "双创",
    # —— 条件触发：短线/买卖/板块策略类问法 ——
    "短线", "买卖", "买入", "卖出", "加仓", "减仓", "建仓", "清仓",
    "止损", "止盈", "回踩", "追涨", "轮动", "操作建议", "交易策略",
)


def append_liquidity_indicators(
    analysis_type: str, route_text: str, resolved: list[str]
) -> list[str]:
    """按任务类型与问题文本补齐大盘/板块流动性、FedWatch指标（规则式确定性追加）。

    LLM规划可能漏掉流动性指标，此函数保证行业/个股/全量研判必然携带；
    macro类型仅在问题明确涉及A股（含大盘预测/复盘/短线买卖/板块策略等交易
    意图词）时追加。命中即同时补齐双创板块指标，供中观三市分项轮动研判。
    """
    text = (route_text or "").lower()
    need_market = analysis_type in ("stock", "industry", "full")
    if analysis_type == "macro" and any(
        kw in (route_text or "") for kw in _A_SHARE_LIQUIDITY_KEYWORDS
    ):
        need_market = True
    if need_market:
        resolved += [i for i in _MARKET_LIQUIDITY_INDICATORS if i not in resolved]
        resolved += [i for i in _BOARD_LIQUIDITY_INDICATORS if i not in resolved]
    if any(kw.lower() in text for kw in _GLOBAL_LIQUIDITY_KEYWORDS):
        # `fed:policy_range`（FRED 当前目标区间）是**可达**的那一个：
        # 实测 CME（`fed:rate_prob:next` 的数据源）在本机网络 TCP 443 不通，
        # 而 FRED 1.0~1.3s 可达且给到最新值（2026-09-25 区间 3.75%~4.00%）。
        #
        # ★ 第二十三轮：**同时**采三个独立单值序列。
        #   为什么不能只留混合口径：它同日有上下限两条，
        #   read 侧"取最新一条"会挑中上限或下限之一，
        #   实测把**区间下限 3.75 当成政策利率**报了出去。
        #   拆开的三条各自是单值序列（方向用 `fed:effr`，区间成对读）。
        #   混合口径**保留采集**（历史库已有 3 条点，且下游 A17 的
        #   capabilities 里仍按它描述能力），但不作为读数依据。
        for ind in ("fed:effr", "fed:target_upper", "fed:target_lower",
                    "fed:policy_range", "fed:rate_prob:next"):
            if ind not in resolved:
                resolved.append(ind)
    return list(dict.fromkeys(resolved))  # 保序去重（入参可能已含重复项）


_PLANNING: dict[str, tuple[list[str], list[str]]] = {
    "macro": (["CPI", "PPI"], ["A08_macro"]),
    "industry": (["CPI", "PPI"], ["A09_meso"]),
    "stock": (["stock_close", "PE(TTM)", "PB", "资产负债率", "流动比率"],
              ["A10_micro", "A11_fin_risk", "A12_compliance"]),
    "news": ([], list(INFO_AGENTS)),
    "full": (["CPI", "PPI"], list(ANALYSIS_AGENTS)),
}

#: 某类能力被问句"点单"时需要补挂的 (分析 Agent, 裸指标名)。
#:
#: ⚠️ 裸指标名（`PE(TTM)` 这种）**必须**经 `sanitize_indicators()` 补 6 位代码，
#:    否则没有任何连接器 supports 它（见该函数的 docstring：这是 2026-09-26
#:    实测过的故障链）。所以增补后**必须再跑一次** sanitize。
_SIGNAL_AGENT_INDICATORS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    # 个股级：估值 + 行情 + 财务（A10 微观 / A11 财务风险 / A12 合规）
    #
    # ★ 2026-09-29 补 7 条（真实 LLM 端到端验收抓到的缺口）：
    #   问句问的是「未来半年能否持有**高股息**的招商银行」，而修复前
    #   `planned` 里**一个股息类指标都没有** —— 系统答"招行高股息可持续性
    #   因个股估值/股息/财务数据全缺，无法验证"，**问股息却不采股息**。
    #   同时 A12 合规的 5 个比率族也从未进过计划 ⇒ 它的本地规则永远拿不到输入
    #   （于是永远输出「未量到」—— 那是**诚实**的，但"诚实报缺口 ≠ 补上缺口"）。
    #
    #   这 6 条都已登记在 `configs/indicators.yaml` 且连接器 `supports()` 认：
    #     `股息率TTM:` / `股息率:`（合成口径）· `ROE:`  ← 新浪财务分析指标
    #     `商誉占净资产比:` / `货币资金占总资产比:` / `有息负债占总资产比:` /
    #     `大股东质押比例:` / `对外担保占净资产比:`  ← ComplianceFinConnector
    "stock": (
        ("A10_micro", "A11_fin_risk", "A12_compliance"),
        ("stock_close", "PE(TTM)", "PB", "资产负债率", "流动比率",
         # 高股息 + 盈利可持续性（用户问句里的"高股息"直接对应这两条）
         #
         # ⚠️ **只放 `股息率TTM`，不放 `股息率`**（2026-09-29 真实端到端实测）：
         #   `股息率:{code}` 走**合成口径**（分红明细 ÷ 收盘价，
         #   `_DERIVED_STOCK_INDICATORS`），而 AkShare 的
         #   `stock_history_dividend_detail` **间歇性 RemoteDisconnected**
         #   —— 实测在真实端到端里**每次都失败**并进 `errors`
         #   （"股息率:600036 … Connection aborted / RemoteDisconnected"），
         #   反而给 A17 递了一句"股息数据缺失"的假缺口。
         #   同源的 `股息率TTM:{code}` 走本地行情仓（`quant_daily_basic.dv_ttm`），
         #   实测 **4915 条 / 最新 2026-09-28 = 4.9606%**，稳定且更新。
         #   **同一个口径只留能取到的那一个** —— 计划里放一个必然失败的指标
         #   等于自己制造缺口（"没量到"会被读成"没有"）。
         "股息率TTM", "ROE",
         # A12 合规本地规则的输入（`compliance/logic.py::_RULE_FAMILIES` 六族）
         "商誉占净资产比", "货币资金占总资产比", "有息负债占总资产比",
         "大股东质押比例", "对外担保占净资产比",
         # ★ 2026-09-29：**平台自有后端数据** 5 族（用户原话：
         #   「个股 agent：自身估值水平…是属于"估值透支"还是"估值合理偏贵"、
         #     "上涨空间充足"，其判断依据的数据都可以连接到」）。
         #   这几条的「判断依据」平台**早就有权威实现**，本处只负责把数据接进计划：
         #     · `估值水位`   ← src/intraday/valuation.py（前端同源档位与标签）
         #     · `概念拥挤度` ← ml_member_corr/ml_stock_theme × sector_crowding_daily
         #     · `主线告警`   ← mainline_alert（中高强度）
         #     · `个股告警`   ← fact_alerts 点名该股
         #     · `解禁计划`   ← 投资日历 cal:unlock:* 的 extra_json 个股明细
         #   ⚠️ 必须同时进 `_CODE_SUFFIX_INDICATORS`（否则增补成裸名 → 必然失败）。
         "估值水位", "概念拥挤度", "主线告警", "个股告警", "解禁计划"),
    ),
    # 行业级：命中的行业 Agent 看本行业产业指标
    "industry": ((), ()),   # 指标由 INDUSTRY_INDICATORS 按命中的 Agent 下发
    # 消费主题：消费行业 Agent（"对消费的影响"这类问法）
    "consumer": (("A14_consumer",), ()),
    # 高股息/红利主题 → 银行属于金融，当前无金融行业 Agent，
    # 落到 A10（个股估值）与 A09（行业估值分位）覆盖。
    "dividend": ((), ()),
}


def _query_agent_signals(text: str, target: str = "") -> set[str]:
    """从问句里识别**领域信号**（规则式，确定性，不调 LLM）。

    ## 为什么需要它（2026-09-28 真实报障）

    用户问：「当前宏观环境如何，预测下一年美国的加息、降息节奏，
    **对A股的影响**，以及AI应用加速失业率增加**对消费的影响**节奏时间节点分析，
    **未来半年能否持有高股息的招商银行**？」

    规划器把它判成单一 `analysis_type = "macro"`，而 `supervisor_node`
    紧接着执行了这条规则：

        if llm_plan["analysis_type"] == "macro":
            planned_agents = [a for a in planned_agents if a not in industry_agents]

    **四个子问题共用一份 CPI/PPI 数据**，然后交给 A17 一次性写完。

    ## 设计取舍：为什么不改 `analysis_type`

    `analysis_type` 是**单值**契约（下游 `_PLANNING`、`plan_run`、
    前端展示都按它分支）。把它改成多值会牵动一条长链，属于"顺手重构"。

    这里改成**增补**：`analysis_type` 保持规划器的判断（宏观为主），
    但按问句里**真实出现的领域信号**把对应的分析 Agent 与指标补回来。
    这样：
      · 纯宏观问 → 行为完全不变（没有信号就不增补）
      · 复合问问到个股 → A10/A11 重新入列，`sanitize_indicators` 会为它补代码
      · 问到消费 → A14 入列，且**不会**被 macro 的剥离规则误删（增补在其后）

    ## 判据纪律（避免误命中 —— 每一条都是"宁可不补"）

      · **个股信号走 `query_needs_stock_resolution`**（与 API 层**同源**）。
        判据变化史：一开始写成"意图词 + 必须有 target"，结果
        **复合问的 target 是空的**（主题是宏观），个股信号永远不触发 ——
        闸门写得太严与不写，在报障现场是同一个结果。
      · **行业信号要 `route_industry` 真命中**（"半导体/煤炭/医药"这类具体行业名）。
        **不看"行业/板块/赛道"这类通用词** —— 宏观问句里出现"行业"是常态，
        用它当判据会把 macro 的剥离优化整个撤销（每次多跑 4 个 reasoning 层 Agent）。
    """
    q = text or ""
    signals: set[str] = set()
    if query_needs_stock_resolution(q, target):
        signals.add("stock")
    if any(k in q for k in _CONSUMER_THEME_WORDS):
        signals.add("consumer")
    if route_industry(f"{target or ''} {q}"):
        signals.add("industry")
    if any(k in q for k in _DIVIDEND_THEME_WORDS):
        signals.add("dividend")
    return signals


#: **严格大盘词**：出现即判定"持有/仓位"说的是**市场**，不是某只个股。
#:
#: 为什么需要这份排除表（实测）：'持有' 本身**不足以**判定是个股问 ——
#: 「大盘未来半年能不能持有」也命中它。若只看意图词，
#: 每次大盘问都会做一次 5500 行名称表解析，还可能把大盘解析成
#: 名字里含这些词的标的（更糟：**悄悄把大盘问变成个股问**）。
_STRICT_MARKET_OBJECTS: tuple[str, ...] = (
    "大盘", "两市", "全A", "全a", "全市场", "整体市场", "仓位配置",
)

#: **宽基词**：这些词说明问句里提到市场，但**不足以**排除个股 ——
#: 实测报障原文同时含「**对A股的影响**」与「能否持有…**招商银行**」，
#: 若把"A股"当排除词，个股信号会被一起误杀（而那只票是真要分析的）。
#: 所以宽基词只在**没有别的个股指向**时才参与排除。
_BROAD_MARKET_WORDS: tuple[str, ...] = (
    "A股", "a股", "沪深", "上证", "深证", "创业板", "科创板", "双创",
    "指数", "板块", "市场",
)

#: "持有/买卖/仓位"类意图词：用户在对**具体标的**要结论。
#:
#: ⚠️ 这份表是"要不要去做名称→代码解析"的**闸门**，所以刻意保守：
#:   · 收录的都是**及物**动作（持有/买入/建仓/止盈…）——它们后面通常跟一个标的；
#:   · **不收录**"值得买"/"能不能买"这类口语（"大盘值得买吗"也命中）；
#:   · 还要叠加 `_MARKET_OBJECT_WORDS` 排除（"大盘能不能持有"命中"持有"，
#:     但对象是市场，不是个股）。
#: 宁可漏（少解析一次，答案里如实登记缺口），不可误（把大盘问变成个股问）。
_HOLDING_INTENT_WORDS: tuple[str, ...] = (
    "持有", "买入", "卖出", "加仓", "减仓", "建仓", "清仓", "止盈", "止损",
    "仓位", "目标价",
)

#: 消费主题词（问到消费景气/失业→消费这类传导时补挂 A14）
_CONSUMER_THEME_WORDS: tuple[str, ...] = (
    "消费", "零售", "社零", "白酒", "家电", "食品饮料", "必选", "可选消费",
    "需求复苏", "居民消费",
)

#: 高股息/红利主题词
_DIVIDEND_THEME_WORDS: tuple[str, ...] = (
    "高股息", "股息率", "红利", "分红", "派息",
)


def query_needs_stock_resolution(text: str, target: str = "") -> bool:
    """问句是否**需要**做"名称 → 6 位代码"解析（给 API 层用，避免无谓联网）。

    ## 为什么要单独暴露这个判据

    API 层原先只在 `analysis_type == "stock"` 时解析标的。于是宏观问句里
    夹带的那只个股（「未来半年能否持有高股息的**招商银行**」）
    **永远不会被解析出代码**，A10 也就无从取数 —— 而 A17 只能在
    `data_gaps` 里写「招商银行个股财务明细…未提供」。

    ## 判据（**与 `_query_agent_signals` 同源**，同一份关键词表）

      · 问句里已有 6 位代码 → 需要（直接可用，不必查名称表）；
      · 问句里出现**及物**的持有/买卖意图词 → 需要（名称表解析）。

    ## 两条排除（都是"宁可不解析"）

      ① 问句里出现**严格大盘词**（大盘/两市/全A…）→ "持有/仓位"说的是
         **市场**，不是某只个股（实测：「大盘未来半年能不能持有」命中"持有"）；
      ② 只出现**宽基词**（A股/沪深300/指数…）且**没有任何严格大盘词**时
         → 不排除。因为报障原文同时含「对A股的影响」与「招商银行」，
         把"A股"当排除词会把真要分析的那只票一起误杀。

    ⚠️ 与 `target` 的**文本**无关：复合问的 target 往往是空的（主题是宏观），
    个股只出现在问句正文里。所以判据只看问句。

    宁可漏判（少解析一次 → 答案里如实登记"缺个股明细"），
    不可误判（把大盘问变成个股问）。
    """
    q = text or ""
    if re.search(r"(?<!\d)\d{6}(?!\d)", q):
        return True
    if not any(k in q for k in _HOLDING_INTENT_WORDS):
        return False
    return not any(k in q for k in _STRICT_MARKET_OBJECTS)


def _industry_crowding_available(industry: str) -> bool:
    """这个行业在拥挤度板块池里**有没有相近板块**（仅供参考的**查询**，不再当闸门）。

    ## ⚠️ 2026-09-29 口径变更（用户原话）

    > 「**"找不到就不提示"改成 找不到就去联网搜索找**」

    原先这个判定被当作**规划期闸门**：匹配不上就**不排** `行业拥挤度:{行业名}`
    —— 于是采集不跑、没有卡片、没有提示（那是当时"不提示"口径的落法）。
    但那条路**永远不会联网**（采集链上原来没有联网兜底），
    所以"本地匹配不上"= 这个族对用户彻底不存在。

    现在采集链上补了联网兜底（`_network_lookup_for_collection`，走
    `NetworkFallback` 的三道闸门），于是正确的做法是：
    **照排**，本地/连接器拿不到就**去联网找**；联网也没有才按缺口如实上报。

    本函数因此**降级为诊断助手**（探针/排障看"本地池里有没有"），
    **不再是排不排的依据**。
    """
    try:
        from src.infrastructure.connectors.platform_data_connector import (
            PlatformDataConnector,
        )

        return bool(PlatformDataConnector().has_related_board(industry))
    except Exception:  # noqa: BLE001 连接器不可用不该让调用方挂
        logger.warning(
            "行业拥挤度可用性判定失败（保守放行，真缺口照样上报）：%s",
            industry, exc_info=True)
        return True


#: ★ 2026-09-29：**近期行情意图**关键词 —— 命中则补「行业轮动日报」那一族。
#:
#: 用户原话：「用户问到**当下和未来近期行情**的，可以接入"**行业轮动日报**"里的数据，
#: 寻找相关参考。」所以判据是"问句里有没有近期/当下/后市/轮动这类意图"，
#: 而不是"有没有提到行业轮动这四个字"（后者用户根本不会那么问）。
_NEAR_TERM_KEYWORDS: tuple[str, ...] = (
    "当下", "目前", "现在", "当前", "近期", "短期", "未来", "后市", "下周",
    "明天", "明日", "这两天", "这几天", "轮动", "行情", "走势", "还能不能",
    "要不要", "该不该", "买点", "卖点",
)


def augment_plan_by_query_signals(
    text: str, target: str, agents: list[str], indicators: list[str],
    *, resolved_code: str = "", analysis_type: str = "",
    resolved_codes: tuple[str, ...] = (),
) -> tuple[list[str], list[str], list[str]]:
    """按问句领域信号**增补** Agent 与指标。返回 `(agents, indicators, notes)`。

    这是"复合问不该被压成单一视角"的落地：`analysis_type` 不变，
    但问句真实触及的领域会被补回计划里。

    ⚠️ **只增不减**：本函数不删任何 Agent/指标 —— 删是规划层的决定
    （如 macro 剥离行业 Agent），增补层不越权回滚它。

    `resolved_code`：API 层已解析出的 6 位代码（`state["focus_stock_code"]`）。
    有它时，个股类**裸指标**会在这里补上后缀 —— 因为 `sanitize_indicators`
    只认 `target`（宏观问的 target 是空的，补不出后缀）。

    ★★ `resolved_codes`（`CHG-0216`）：**问句里点名的全部个股**。
    > 用户报障：「用户输入含有 **2 个及以上**的个股…此时采集数据会存在
    > **漏掉一些股票**的信息获取」——例：「…能否持有高股息的**宁波银行**和
    > **中国神华**？」标的 `601088`，反馈中国神华缺个股估值与股息、
    > 宁波银行没有任何可引用的估值，**而本地库里明明有**。
    根因：这一段原来只有**单个** `code` ⇒ 个股指标只能挂到一只上，
    **另一只一个指标都没有**（症状是"该股没有估值/股息数据"）。
    ⇒ 现在为**每一个**代码各排一份。`resolved_code` 保留为单值回退
    （既有调用点与测试不受影响）。

    `analysis_type`：**只有 `news` 会改变行为** —— news 管线不采数据
    （信息层 A05/A06/A07 串行链，A01-A04 整体跳过，见
    `tests/integration/test_supervisor_graph.py::test_news_graph_info_pipeline`），
    所以**行业/个股那些采集类指标一律不补**。实测教训：不挡这一下，
    一条 news 问句（`user_query` 里恰好带"茅台"）会被补上 `行业拥挤度:白酒`
    ⇒ `_planned_indicators` 非空 ⇒ 采集节点把 A01 拉起来跑 ⇒
    那条"数据管线在 news 下整体跳过"的集成测试直接红。
    """
    signals = _query_agent_signals(text, target)
    notes: list[str] = []
    new_agents = list(agents)
    new_indicators = list(indicators)
    #: news 管线不采数据（见 docstring 实测教训）
    collects_data = analysis_type != "news"

    # ★ 2026-09-29：**行业侧三族**（平台自有后端数据，按**行业名**取数）。
    #
    # 用户原话（同一天追加的需求）：
    #   「② 用户输入内容中含有**行业**的，要连接到概念板块拥挤度的数据表，
    #     找与之**最相近的所属概念板块**…也可以接入"**板块资金流**"功能板块的数据，
    #     查询该板块**近期资金流方向**。
    #    ③ 用户问到**当下和未来近期行情**的，可以接入"**行业轮动日报**"里的数据。」
    #
    # 两件事必须守住：
    #   ① 行业名解析走**唯一实现** `resolve_focus_industry`（本地名录口径，
    #      与 `needs_generic_industry` / `industry_scope_for` 同源）——
    #      在这里另写一套模糊匹配，必然演成"路由认为归 A 管、增补按 B 取数"；
    #   ② 这一节**不受 `signals` 有无支配**（问句只提行业名、没命中任何领域信号时
    #      同样要接），所以它放在下面那个 `if not signals` 提前返回**之前**。
    # ★★ `CHG-0217`：**全部**命中行业，不是一个。
    #
    # 用户报障原话包含「…**或 2 个及以上的概念板块**…此时采集数据会存在
    # **漏掉一些股票或板块**的信息获取」。与个股侧（`CHG-0216`）是**同一个缺陷**：
    # 原来 `focus_industry` 是单值 ⇒ 问句提到两个板块时，只给一个补
    # `行业拥挤度`/`板块资金流`/`行业轮动`，**另一个板块一个指标都没有**。
    focus_industries = resolve_focus_industries(
        f"{target or ''} {text}".strip())
    for focus_industry, industry_how in (
            focus_industries if collects_data else ()):
        # ★ 「找不到就不提示」的**正确落法**（用户口径 + 链路实测，2026-09-29）：
        #   用户明确说「如果找不到就不提示未找到数据」，而 `fetch()` 返回 `[]`
        #   **做不到"不提示"** —— 空结果会在两处冒出来：
        #     ① `domain/agents/data/collector/logic.py` 的空结果摘要
        #        「未获取到 {indicator} 数据」（时间线里一张卡）；
        #     ② 本文件里空结果触发的「⚠️ {indicator} 实时无数据（自修复已转后台）」
        #        progress 行，**并真的去联网自修复一次**。
        #   所以改成**规划期就不排这个指标**：判定走连接器自己的缓存
        #   （`PlatformDataConnector.has_related_board`，只读、零新增取数），
        #   匹配不上 → 不进计划 → A01 不跑、无卡片、无 progress、A17 上下文里也没有它。
        #   ⚠️ 判定**异常时返回 True（保守放行）**：例外边界只有"匹配不上"，
        #      存储层坏了（表缺失/库打不开）是真缺口，必须照旧上报。
        # ★ 2026-09-29 口径变更：**照排**，拿不到就联网找。
        #   原先是"规划期先问有没有相近板块，没有就不排"（当时的"不提示"落法）；
        #   但采集链上原来**没有联网兜底** ⇒ 不排 = 这个族对用户彻底不存在。
        #   现在采集链补了联网兜底（三道闸门在 `NetworkFallback` 里），
        #   所以本地匹配不上不再是"不排"的理由 —— 让采集去试，联网那跳会接手。
        ind = f"行业拥挤度:{focus_industry}"
        if ind not in new_indicators:
            new_indicators.append(ind)
            local_hint = "本地板块池有相近板块" if _industry_crowding_available(
                focus_industry) else "本地板块池无相近板块（交给联网兜底去找）"
            notes.append(
                f"问句命中行业「{focus_industry}」（{industry_how}）→ 补 {ind}"
                f"（{local_hint}）")
        ind = f"板块资金流:{focus_industry}"
        if ind not in new_indicators:
            new_indicators.append(ind)
            notes.append(f"问句命中行业「{focus_industry}」→ 补 {ind}")
        if any(kw in (text or "") for kw in _NEAR_TERM_KEYWORDS):
            ind = f"行业轮动:{focus_industry}"
            if ind not in new_indicators:
                new_indicators.append(ind)
                notes.append(f"问句含近期行情意图 → 补 {ind}（行业轮动日报）")

    if not signals:
        return new_agents, new_indicators, notes

    # ★ `CHG-0216`：个股信号要覆盖**所有**被点名的股票。
    #   单值回退：没给 `resolved_codes` 时用 `resolved_code`（既有调用点不变）。
    codes: tuple[str, ...] = tuple(resolved_codes) or (
        (resolved_code,) if resolved_code else ())
    for sig in sorted(signals):
        sig_agents, sig_indicators = _SIGNAL_AGENT_INDICATORS.get(sig, ((), ()))
        if not collects_data:
            # news 管线：连"补挂分析 Agent"也不做吗？——**要做**（信息层的结论
            # 仍需要有人综合），但**指标一个都不补**（补了就会把 A01 拉起来跑）。
            sig_indicators = ()
        for aid in sig_agents:
            if aid not in new_agents:
                new_agents.append(aid)
                notes.append(f"问句命中「{sig}」→ 补挂 {aid}")
        #: 这一个信号要覆盖的代码集合。只有 `stock` 是"按股票各排一份"，
        #: 其余信号走原来的单次（`("",)`）—— 逐字保持既有行为。
        ind_codes: tuple[str, ...] = codes if sig == "stock" else ("",)
        for ind in sig_indicators:
            # 拿不到代码时**不要**把裸个股指标塞进计划：裸名（`PE(TTM)`）
            # 没有任何连接器 supports，A01 会白撞一次网络后必然失败，
            # 用户看到"估值数据缺失"——**看起来像数据源坏了，其实是契约不满足**。
            # 摘掉它，把"没有标的代码"如实留给 A17 写进 `data_gaps`。
            #
            # ⚠️ 这个判断必须留在**本函数内部**（而不是让调用方自己再处理一遍）：
            #   否则每个新调用点都要记得这件事，而漏掉的那次**不会报错**，
            #   只会在用户面前表现为"数据缺失"。本项目已登记过太多这类缺陷。
            if not ind_codes and _needs_code_suffix(ind):
                continue
            for code in (ind_codes or ("",)):
                if code == "" and _needs_code_suffix(ind):
                    continue
                fixed = (f"{ind.split(':', 1)[0]}:{code}"
                         if (code and _needs_code_suffix(ind) and ":" not in ind)
                         else ind)
                if fixed not in new_indicators:
                    new_indicators.append(fixed)
                    if fixed != ind:
                        notes.append(f"个股指标补标的代码：{ind} → {fixed}")

    # 行业信号：按问句路由到的行业 Agent，补它的产业指标
    if "industry" in signals:
        for aid in route_industry(f"{target or ''} {text}"):
            if aid not in new_agents:
                new_agents.append(aid)
                notes.append(f"问句命中行业「{aid}」→ 补挂")
            for ind in INDUSTRY_INDICATORS.get(aid, ()):
                if ind not in new_indicators:
                    new_indicators.append(ind)

    # ★ 2026-09-29：**兜底行业 Agent**（用户报障「缺少银行 agent 吗？」）
    #
    # 上面那条只在**关键词命中**时才挂 Agent。银行/非银/公用事业/交运这类
    # 没有专属关键词的行业，一个 Agent 都挂不上 —— 用户看到的是
    # 「600036 非本行业覆盖标的，无法给出结论」。
    # 这里用**确定性解析**（代码→本地名录 / 简称→代码 / 文本直述行业名）
    # 补挂 A20；它的行业名与关注指标都在 Agent 内部按请求解析。
    # ★ 2026-10-08：判据改成**逐行业**的并集（`needs_generic_industries`）。
    #   「整条问句有没有行业」是错的问题 —— 问句里两个行业时，第一个有主
    #   （煤炭开采 → A15）就会让判据说"有人管"，而**第二个行业（银行）
    #   一个人都没管**。现在：**每一个**没有 A13–A16 接管的行业都要求补挂 A20
    #   （A20 自己按 `requested_industries` 逐行业各出一节）。
    #   ⚠️ 单行业时 notes 文案与修复前**逐字相同**（`'、'.join([x]) == x`）。
    if "stock" in signals or "industry" in signals:
        generic_industries = needs_generic_industries(
            f"{target or ''} {text}", resolved_code)
        if generic_industries and "A20_generic_industry" not in new_agents:
            new_agents.append("A20_generic_industry")
            notes.append(
                f"标的属「{'、'.join(generic_industries)}」行业且无专属行业 Agent → "
                f"补挂 A20_generic_industry（兜底）")

    # 高股息/红利主题：银行属金融，当前没有金融行业 Agent。
    # 用**已登记**的行业估值截面覆盖。
    #
    # ⚠️ 2026-09-29 更正（**错误的理由比没有理由更危险**）：这段注释原先写着
    #   「`ind:sw_third_dividend_yield:all` **未登记在 configs/indicators.yaml**，
    #    所以不能加」—— 那条理由**已经过期**：本轮已把它登记进
    #    `configs/indicators.yaml` 并补进 `INDICATOR_CATALOG`（CHG-0072）。
    #    于是"问高股息却不采行业股息率截面"这件事**没有任何理由**了，补上。
    if "dividend" in signals:
        for ind in ("ind:sw_third_pe_ttm:all", "ind:sw_first_pe_ttm:all",
                    "ind:sw_third_dividend_yield:all"):
            if ind not in new_indicators:
                new_indicators.append(ind)
        if "A09_meso" not in new_agents:
            new_agents.append("A09_meso")
            notes.append("问句命中「高股息」→ 补挂 A09_meso（行业估值分位）")

    return new_agents, new_indicators, notes


def route_industry(target: str) -> list[str]:
    """按行业名关键词把target路由到对应行业Agent（无匹配返回空列表）。"""
    lowered = (target or "").lower()
    return [aid for aid, kws in INDUSTRY_KEYWORDS.items()
            if any(k.lower() in lowered for k in kws)]


def resolve_focus_industry(text: str) -> tuple[str, str]:
    """文本 → `(行业名, 依据说明)`；拿不到返回 `("", "")`（**不猜**）。

    唯一实现在 `catalog/industry_of.resolve_industry_from_text`（本地名录
    `quant_stock_basic.industry`）。本函数只是**编排层的入口**，不复制判据 ——
    "编排层挂的 Agent"与"Agent 自己解析的行业"必须是同一个答案，
    否则结论里的行业名与路由不一致，而且不报错。
    """
    probe = str(text or "").strip()
    if not probe:
        return "", ""
    try:
        from src.infrastructure.catalog.industry_of import (
            resolve_industry_from_text,
        )

        return resolve_industry_from_text(probe)
    except Exception as exc:  # noqa: BLE001 名录不可用 → 退化到"没有兜底"，不阻断任务
        logger.debug("行业解析失败（不阻断）：%s", exc)
        return "", ""


def _a17_subjects(state: ResearchState) -> list[dict[str, Any]]:
    """★ `CHG-0230`：A17 的**逐只表态清单** —— 从 `focus_stock_codes` 来。

    ## 为什么需要（§41.36 审计的 A17 项）

    用户问「…未来半年能否持有高股息的**宁波银行**和**中国神华**？」时，
    A17 的输入只有**单值** `focus`，输出也只有**一套** `stance`/`position_advice`/
    `expected_return_3_6m` ⇒ **两只票共用一个立场**，用户不知道"中性偏多"是针对哪只；
    若模型只挑了最像的那只写，**另一只在最终交付里根本不存在**。

    ⚠️ **三处注入点共用本函数**（`CHG-0087` 为"同一件事写两遍"付过代价）：
    写三遍必然漂移，而漂移的方向是"某条路径下 A17 又退回单标的"——
    **不报错，只是又少了一只票**。

    ⚠️ **少于 2 只 ⇒ 返回 `[]`** ⇒ A17 走单标的口径，输出与修复前**逐字一致**。

    ⚠️ 已知边界：非主焦点的**中文名拿不到** —— API 层只把代码写进了
    `focus_stock_codes`（`AnalysisSubject.focus_stock_codes` 是 `tuple[str, ...]`）。
    这里只保证**代码齐全**；名称由 A17 从问句与上游结论里读。
    要补名称得同时改 `AnalysisSubject` 与 `ResearchState`，不在本轮半径内。
    """
    codes = [str(c) for c in (state.get("focus_stock_codes") or ()) if c]
    if len(codes) < 2:
        return []
    primary_name = str(state.get("focus_stock_name") or "")
    primary_code = str(state.get("focus_stock_code") or "")
    return [
        {"code": c, "name": primary_name if c == primary_code else ""}
        for c in codes
    ]


def resolve_focus_industries(text: str) -> list[tuple[str, str]]:
    """★ 文本 → **全部**命中行业 `[(行业名, 依据说明), …]`（`CHG-0217`）。

    与 `resolve_focus_industry` 的关系：同一个唯一实现
    （`catalog/industry_of.resolve_industries_from_text`）、同一条"确定性优先"
    的三路优先级，只是**收全部**而不是取第一个。

    为什么需要（用户报障原话）：「…用户输入含有2个及以上的个股**或2个及以上的
    概念板块**…此时采集数据会存在**漏掉一些股票或板块**的信息获取」——
    单值入口下，问句提到两个板块时只有**一个**会被补 `行业拥挤度`/`板块资金流`/
    `行业轮动`，另一个板块**一个指标都没有**。

    ⚠️ 拿不到与"拿到空"必须区分：本函数返回 `[]` 表示**三路都不中**（不猜行业），
    调用方据此不排任何板块族 —— 与单值版返回 `("", "")` 同一条纪律。
    """
    probe = str(text or "").strip()
    if not probe:
        return []
    try:
        from src.infrastructure.catalog.industry_of import (
            resolve_industries_from_text,
        )

        return list(resolve_industries_from_text(probe))
    except Exception as exc:  # noqa: BLE001 名录不可用 → 不阻断任务，只是不补板块
        logger.debug("多行业解析失败（不阻断）：%s", exc)
        return []


def needs_generic_industries(text: str, focus_code: str = "") -> list[str]:
    """★ 问句里**每一个"没有专属行业 Agent 接管"的行业**（保序去重；拿不到 → 空列表）。

    ## 报障现场（用户 2026-10-08，与 `CHG-0216`/`CHG-0217` 同一条原话）

    > 「…基于当前板块拥挤度和能源重点项目与新业态投资20万亿的政策，
    >   未来半年能否持有高股息的**宁波银行**和**中国神华**？**标的 601088**」

    修前实测（标的 601088 + 用户原句）：

        resolve_focus_industries(txt)          = ['煤炭开采'(601088), '银行'(002142)]
        needs_generic_industry(txt, '601088')  = ''      ← **空**
        route_industry(txt)                    = []      ← 一个行业 Agent 都不挂

    根因：判据是**逐问句**的 —— 单值入口 `resolve_focus_industry` 是"确定性优先"
    （路径①从 601088 解出 `煤炭开采` 就返回），而 `煤炭开采` 已被 A15（周期，
    关键词含"煤炭"）覆盖 ⇒ 判据说"这条问句有行业、有人管" ⇒ **A20 不挂**；
    可问句里**还有第二个行业「银行」**，它没有任何 A13–A16 接管
    ⇒ 宁波银行没有行业结论（"没有任何可引用的估值"的行业版）。

    ## 判据（**逐行业**判，不是逐问句判）

    对 `resolve_focus_industries`（`CHG-0217` 的**并集**解析器）里的每一个行业：
    `route_industry(行业名)` 为空 ⇒ 该行业没有 A13–A16 之一接管 ⇒ 收进结果。
    行业名与"谁管它"都取自既有单一事实源（本地名录 + `INDUSTRY_KEYWORDS`），
    本函数**不新增清单、不重写行业解析**。

    ⚠️ 与单值版的分工（两个问题，不是一个）：
      · 本函数 = **"要兜底几个行业"** ⇒ A20 的输出契约按它**逐行业各出一节**；
      · `needs_generic_industry`（单值）= **"第一个没人管的行业是谁"** ⇒
        `industry_scope_for` 用它渲染"行业结论由谁负责"。
      两者同源：单值版就是本函数的首元素（拿不到 → `""`）。

    ⚠️ 单行业问句下本函数 == `[单值版的结果]`（并集解析器在只有一个行业时
    与单值版逐字同源）⇒ 挂载/裁剪/输出**逐字不变**（有回归护栏钉住）。
    """
    out: list[str] = []
    for name, _how in resolve_focus_industries(f"{text} {focus_code}".strip()):
        if not name or name in out:
            continue
        if route_industry(name):
            continue        # 已有专属 Agent 覆盖，不需要兜底
        out.append(name)
    return out


def needs_generic_industry(text: str, focus_code: str = "") -> str:
    """这个问句/标的是不是需要一个**兜底行业 Agent**？返回要分析的行业名（否则空串）。

    ## 报障现场（用户 2026-09-29）

    > 「600036（招商银行属银行，**非本行业消费**）在数据缺口下无法给出明确持有
    >  结论：**缺少银行 agent 吗？**」

    修前：问句提到"招商银行"，`route_industry` 命中不了任何专属 Agent
    （`INDUSTRY_KEYWORDS` 里没有银行），于是这条问句**没有任何行业 Agent 该管银行**；
    而 A14（消费）被"消费"二字挂上后，第一句就写「非本框架覆盖标的」。

    ## 判据（三条同时成立才算"需要兜底"）

    1. 文本能**确定性**解析出一个行业（`resolve_focus_industry`，
       代码 → 名录 / 简称 → 代码 → 名录 / 文本直述行业名）；
    2. 该行业**没有专属 Agent**（`route_industry(行业名)` 为空）；
    3. 它也不是那四个专属行业的同义词（同 2，用同一份关键词表判，不另立清单）。

    拿不到行业 → 返回空串（**不猜**）。猜一个行业的代价是
    "用一个错误的框架分析"，比"没有行业结论"更糟。

    ★ 2026-10-08：本函数改为 `needs_generic_industries()`（**并集**判据）的
      **首元素** —— 修前它自己走单值入口，于是"第一个行业有主、第二个行业
      没人管"时返回空串（A20 不挂；见并集版的报障现场）。
      单行业问句下两者结果相同 ⇒ 本函数的对外行为**逐字不变**。
    """
    industries = needs_generic_industries(text, focus_code)
    return industries[0] if industries else ""


def industry_scope_for(
    agent_id: str, text: str, focus_code: str = ""
) -> dict[str, Any] | None:
    """这个**专属**行业 Agent 该不该管本次标的？归它管/判不出 → None。

    ## 现场（`docs/PRD.md` §19.16.5 第 1 条，真实端到端跑出来的）

    兜底 Agent（A20）已接管银行，但 A13（科技）/A14（消费）仍会输出
    「600036 属银行、**不在本次科技行业数据覆盖内**…无法给出可验证的持有结论」。
    用户读到的仍然是"系统缺能力"。

    ## 判据（与 `needs_generic_industry` / 规划期裁剪**同源**）

    - 本 Agent 是**关键词型**专属行业 Agent（A20 是兜底，不参与）；
    - 文本能确定性解析出行业，且**不归本 Agent**（`route_industry(行业名)`
      里没有它）；
    - 返回值的 `self_named` 说明**问句是否点名了本 Agent 的行业** ——
      用户主动问"消费板块里的银行股"时，这一节是用户要的视角，
      不能直接吞掉（那时只禁用"非本框架"这类免责话术，见
      `industry/base.py::_requirements`）。

    为什么判据放在编排层：`INDUSTRY_KEYWORDS` 是路由的**单一事实源**，
    领域层再抄一份必然漂移（漂移的表现是"路由认为不归它管、它自己认为归它管"）。
    领域层只**渲染**这个判定，不重新判定。
    """
    if agent_id not in KEYWORDED_INDUSTRY_AGENTS:
        return None
    probe = str(text or "").strip()
    industry, how = resolve_focus_industry(f"{probe} {focus_code}".strip())
    if not industry:
        return None                      # 判不出行业 → 不下结论、不改行为
    if agent_id in route_industry(industry):
        return None                      # 归它管
    owners = route_industry(industry)
    if owners:
        served_by = "、".join(owners)
    elif needs_generic_industry(probe, focus_code):
        served_by = "兜底行业 Agent（A20_generic_industry）"
    else:
        served_by = "本平台暂无对应行业 Agent（已按缺口登记）"
    return {
        "agent_industry": INDUSTRY_KEYWORDS[agent_id][0],
        "focus_industry": industry,
        "focus_industry_basis": how,
        "served_by": served_by,
        "self_named": agent_id in route_industry(probe),
    }


def prune_industry_agents_by_focus(
    planned: list[str], text: str, focus_code: str = ""
) -> list[str]:
    """个股/综合问法：只留「管这个标的行业」的行业 Agent（+ 兜底）。

    ## 为什么（同上 §19.16.5 第 1 条）

    `analysis_type == "stock"` 原先**保留全部** A13–A16。于是一条"高股息
    招商银行"的问句里，A14（消费）也跑一次 reasoning 调用，产出「非本框架
    覆盖标的」—— 既花钱又给用户"系统缺能力"的错觉。

    ## 判据（三条，全部来自既有的单一事实源，不新增清单）

    1. 问句**点名**的行业 Agent（`route_industry(问句+标的)`）留下；
    2. **每一个**被点到的行业各自的专属 Agent（`route_industry(行业名)`）留下；
    3. 没有专属 Agent 覆盖的行业 → 留下 A20（`needs_generic_industries`）。

    三条都不成立（**行业判不出来、也没点名**）→ **原样返回**：
    宁可多跑几个 Agent，也不要凭猜测砍掉可能相关的那一个。

    ★ 2026-10-08：`owners` 改为按**并集**算（`CHG-0217` 的多值解析器）。
      修前这里是**单值**行业 ⇒ 问句 `标的 601088 + …宁波银行和中国神华？` 里
      第一个行业 `煤炭开采` 有主（A15）、第二个行业 `银行` 没主，
      于是 `owners` 里没有 A20 ⇒ **A20 刚被挂上就被这条裁剪裁掉**
      （实测 `prune_industry_agents_by_focus(['A20_generic_industry'], txt, '601088') == []`）。
    """
    owners = set(route_industry(text))
    for industry, _how in resolve_focus_industries(
            f"{text} {focus_code}".strip()):
        if industry:
            owners |= set(route_industry(industry))
    if needs_generic_industries(text, focus_code):
        owners.add("A20_generic_industry")
    if not owners:
        return planned
    return [a for a in planned if a not in INDUSTRY_AGENTS or a in owners]


def extract_topic_keywords(
    analysis_type: str, target: str, query: str
) -> list[str]:
    """为宏观/行业问题提取主题新闻关键词（无命中返回空列表，不拉无关新闻）。

    - macro：仅取问题中实际出现的宏观词，命中后补充美联储/加息/降息核心词；
    - industry：按target+query路由行业组，命中后放开整组关键词以覆盖"细分环节"问法。
    """
    text = f"{target or ''} {query or ''}"
    low = text.lower()
    kws: list[str] = []

    def _add_macro() -> None:
        hits = [k for k in TOPIC_KEYWORDS["macro_core"] if k.lower() in low]
        if hits:
            # 加息相关报道常以"美联储/降息"表述，补充核心词提高召回
            for k in list(dict.fromkeys(hits + ["美联储", "加息", "降息"])):
                if k not in kws:
                    kws.append(k)

    def _add_industry() -> None:
        """按问题文本路由行业组，命中即放开整组（覆盖"细分环节"类提问）。"""
        for aid in route_industry(text):
            for k in TOPIC_KEYWORDS.get(aid, ()):  # type: ignore[arg-type]
                if k not in kws:
                    kws.append(k)

    if analysis_type == "macro":
        _add_macro()
    elif analysis_type == "industry":
        _add_industry()
    elif analysis_type in ("stock", "full"):
        # ⚠️ 综合问题（full）**不能只按 analysis_type 取词**（2026-09-26 实测故障）：
        # 用户问的是一句话里同时含美国加息 **和** A股AI产业链龙头的综合问题，
        # LLM 规划器把它定成 analysis_type="macro"，于是旧的 if/elif 只走
        # macro 分支、industry 组**根本没被评估**；又因为问题里写的是"再加息"
        # 而没有"美联储"字样，macro 分支也命中为空 ——
        # 最终 topic_keywords=[]，collect 节点据此**一个新闻都不拉**，
        # A05/A06/A07 信息层拿到零输入全部空跳过，舆情/事件维度整体缺席。
        #
        # 既然问题文本本身既谈宏观又谈行业，这里就**两组都取**。
        _add_macro()
        _add_industry()
    return kws


def ensure_info_agents(
    agents: list[str],
    *,
    info_items: list | None,
    analysis_type: str,
    target: str,
    query: str = "",
    focus_stock_codes: Sequence[str] = (),
    auto_topic: list[str] | None = None,
) -> list[str]:
    """**确定性**决定信息层（`A05`/`A06`/`A07`）参不参与 —— 两条规划路径共用这一份判据。

    ## 为什么要有它（`CHG-0240`，2026-10-08 **真实端到端**跑出来的）

    规则兜底路径一直有这段确定性补齐，而 **LLM 规划路径**下 `state["plan"]` 直接来自
    `llm_plan["agents"]`（**模型选的**）⇒ 模型不选，三个信息层 Agent 就不在计划里，
    于是节点里的闸门

        agent_id in INFO_AGENTS and (agent_id not in state["plan"] or not info_items)

    **第一句就命中**，三个 Agent 一次都不执行（连日志都不留一行）。

    实测（两次同问句端到端，`task_20261008_09a27591` / `task_20261008_b3b2adc2`）：
    提交响应的 `plan` 里明明有 A05/A06/A07，而 dev 审计里该 trace **只有 8 次调用**
    （planner / A08 / A10 / A11 / A12 / A20 / A09 / A17），信息层**零调用**；
    同时 `_fetch_news_per_code` 单独实测**返回 20 条**（每只 10 条、代码归属正确）
    ⇒ **不是"没有新闻"，是没人去处理新闻**。用户看到的就是本轮一直在修的那个形态：
    **数据进得去，结论出不来**（"该股近期无消息"看起来像数据源坏了）。

    ★ 这正是「同一判断两份实现」的又一例：**一份确定性、一份交给模型**。
    现在两条路径调同一个函数。

    ## 判据（与修复前的规则路径**逐字同源**，只是多了 `focus_stock_codes`）

      · 显式给了 `info_items`（调用方已经拿到新闻/公告/研报）；
      · 或 `target` 是 6 位个股代码且 `analysis_type ∈ {stock, full}`（会自动拉个股新闻）；
      · 或能提取到主题关键词（自动拉全球财经快讯，取不到时信息层节点空跳过）；
      · ★ 或问句里点名了个股（`focus_stock_codes`）—— 多标的场景下 `target` 可能
        不是代码（比如只填了一个板块），但**既然点名了票就该去取它的新闻**。
    """
    if auto_topic is None:
        auto_topic = extract_topic_keywords(analysis_type, target, query)
    if (info_items
            or (bool(re.fullmatch(r"\d{6}", target or ""))
                and analysis_type in ("stock", "full"))
            or auto_topic
            or tuple(focus_stock_codes)):
        return list(agents) + [a for a in INFO_AGENTS if a not in agents]
    return list(agents)


def plan_run(
    analysis_type: str,
    target: str,
    info_items: list | None = None,
    query: str = "",
    focus_stock_codes: Sequence[str] = (),
) -> dict[str, Any]:
    """规则式Supervisor规划：决定采集指标与参与Agent（Demo用确定性路由）。

    query：用户原始问题。行业Agent路由与主题新闻关键词同时参考target与query，
    因此target留空、问题写在query中（如"当前AI产业链…"）也能正确路由。
    携带info_items（新闻/公告/研报文本）时自动追加信息层Agent，
    与analysis_type无关；news类型为纯信息层管线（不采集数据点）。
    """
    analysis_type = analysis_type if analysis_type in _PLANNING else "full"
    indicators, agents = _PLANNING[analysis_type]
    agents = list(agents)  # 拷贝：_PLANNING为模块级共享配置，禁止原地修改
    indicators = list(indicators)
    is_stock_code = bool(re.fullmatch(r"\d{6}", target or ""))
    # 个股类指标统一拼代码后缀（stock_close/PE/PB/财务比率）
    resolved = [
        f"{ind}:{target}" if ind in _CODE_SUFFIX_INDICATORS and is_stock_code else ind
        for ind in indicators
    ]
    route_text = f"{target or ''} {query or ''}"
    # ★★ 2026-09-30：这里原有一段"美国宏观问题自动追加美国宏观指标"的分支，
    #   追加的是 `us_cpi_yoy`/`us_fed_rate`/`us_nonfarm`/`us_pce`/
    #   `us_unemployment`/`us_core_cpi` —— **其中五条的源已停更并停产**
    #   （`enabled: false`），排进计划只会得到"无连接器/无数据"的缺口。
    #   现在美国/利率侧的必查项由 `ensure_macro_indicators()` **统一确定性补齐**
    #   （见那里的 `_US_RATES_INDICATORS`），两条规划路径（LLM 与规则）都过它，
    #   所以这里不再各写一份 —— 同一件事两份实现必然漂移。
    _ = route_text  # 仍被下游 `route_industry` 等使用，先保留变量语义
    if analysis_type == "industry":
        # 通用产业链分析(A09) + 命中的专业行业Agent(A13-A16)，无匹配则仅A09
        routed = [a for a in route_industry(route_text) if a not in agents]
        agents += routed
        # 按命中行业下发专业产业指标（均为真实互联网数据源）
        for aid in routed:
            resolved += [i for i in INDUSTRY_INDICATORS.get(aid, ()) if i not in resolved]
        # 申万行业估值截面（覆盖所有行业的PE/PB，不依赖命中行业路由）
        sw_indicators = [
            "ind:sw_third_pe_ttm:all", "ind:sw_third_pb:all",
            "ind:sw_first_pe_ttm:all",
        ]
        resolved += [i for i in sw_indicators if i not in resolved]
        # 渗透率数据（按query命中的核心赛道自动追加）
        _PENETRATION_TRACKS_MAP = {
            "新能源": "ind:penetration:新能源汽车",
            "汽车": "ind:penetration:新能源汽车",
            "机器人": "ind:penetration:人形机器人",
            "人形": "ind:penetration:人形机器人",
            "固态电池": "ind:penetration:固态电池",
            "电池": "ind:penetration:固态电池",
            "AI": "ind:penetration:AI大模型应用",
            "人工智能": "ind:penetration:AI大模型应用",
            "半导体": "ind:penetration:半导体国产替代",
            "国产替代": "ind:penetration:半导体国产替代",
            "HBM": "ind:penetration:HBM存储",
            "存储": "ind:penetration:HBM存储",
        }
        for kw, ind in _PENETRATION_TRACKS_MAP.items():
            if kw.lower() in route_text.lower() and ind not in resolved:
                resolved.append(ind)
    if analysis_type in ("stock", "full") and target:
        agents = list(dict.fromkeys(agents))  # 保序去重
    # 信息层参与条件：显式info_items / 个股(自动拉个股新闻) /
    # 宏观或行业问题能提取到主题关键词（自动拉全球财经快讯，无新闻时节点空跳过）
    #
    # ★ `CHG-0240`：判据抽成**一份实现** `ensure_info_agents()`，LLM 规划路径共用
    #   （原来只有这条规则路径有这段确定性补齐，LLM 选谁就是谁 —— 见该函数注释）。
    auto_topic = extract_topic_keywords(analysis_type, target, query)
    agents = ensure_info_agents(
        agents, info_items=info_items, analysis_type=analysis_type,
        target=target, query=query, focus_stock_codes=focus_stock_codes,
        auto_topic=auto_topic)
    # 行业/个股/全量研判确定性补齐大盘流动性指标；海外流动性关键词补FedWatch
    resolved = append_liquidity_indicators(analysis_type, route_text, resolved)
    # ★ 2026-09-29：中国宏观四件套（与 LLM 分支**同一份实现**，见
    #   `ensure_macro_indicators` 的报障说明）。
    resolved = ensure_macro_indicators(analysis_type, route_text, resolved)
    return {
        "analysis_type": analysis_type,
        "target": target,
        "indicators": resolved,
        "topic_keywords": auto_topic,
        "agents": agents + ["A17_recommend", "A18_audit"],
    }


def _needs_code_suffix(indicator: str) -> bool:
    """该指标是否必须带 6 位代码后缀才能被任何连接器支持。

    判据 = **两张表求并集**（单一真值源不能只写一处）：

      * `_CODE_SUFFIX_INDICATORS` —— 平台自有族与几个显式条目（本文件维护）；
      * `_CATALOG_CODE_SUFFIX_ROOTS` —— **目录里自己声明了**"需带代码后缀"的那些
        （现读 `planner.INDICATOR_CATALOG`，见该常量的漂移说明）。

    裸名字（如 ``"PE(TTM)"``）**没有任何连接器 supports** ——
    连不上就是连不上，不是"数据缺失"。
    """
    bare = indicator.split(":", 1)[0]
    return (bare in _CODE_SUFFIX_INDICATORS
            or bare in _CATALOG_CODE_SUFFIX_ROOTS)


#: ★★ 2026-09-29：**按行业名取数**的指标族（`PlatformDataConnector`，见 PRD §19.17）。
#:
#: 与 `_CODE_SUFFIX_INDICATORS` 是**对称**的两张表：一个拼 6 位代码，一个拼行业名。
#:
#: 为什么必须存在：LLM planner 从 `planner.INDICATOR_CATALOG` 选出来的 id 是
#: **裸名**（`行业拥挤度`），而连接器只认 `行业拥挤度:{行业名}`
#: —— 裸名没有任何连接器 `supports()` ⇒ A01 白撞一次网络后必然失败
#: ⇒ 用户看到的是"**数据缺失**"。
#: **这与 2026-09-26 那条 `PE(TTM)` 故障链是同一个形状、同一个后果**，
#: 区别只是这次是我们自己新加的族（见 `sanitize_indicators` 的 docstring）。
_INDUSTRY_SUFFIX_INDICATORS = frozenset({"行业拥挤度", "板块资金流", "行业轮动"})


def _needs_industry_suffix(indicator: str) -> bool:
    """该指标是否必须带**行业名**后缀（`行业拥挤度:银行`）才能被连接器支持。"""
    bare = indicator.split(":", 1)[0]
    return bare in _INDUSTRY_SUFFIX_INDICATORS


def _indicator_registry() -> Any:
    """指标登记表（`configs/indicators.yaml` 的**唯一**读入口）。

    为什么延迟导入：`catalog` 侧要读本模块的 `_CODE_PREFIXES`，
    在模块顶层 import 会形成环。这里只在真正判指标时读一次（内部有缓存）。
    """
    from src.infrastructure.catalog.registry import get_registry

    return get_registry()


def strip_bogus_code_suffix(indicators: list[str]) -> tuple[list[str], list[str]]:
    """掐掉**不该带却带了**的 6 位代码后缀，返回 `(指标, 说明)`。

    ## 现场（2026-09-29 用户报障，**九条宏观指标全废**）

    > 「当前宏观环境如何，预测下未来一年美国的加息、降息节奏…未来半年能否持有
    >   高股息的招商银行？」

    报错里出现 `无连接器支持指标 fed:effr:300068`、`us_cpi_yoy:300068`、
    `us_nonfarm:300068`、`us_pce:300068`、`us_unemployment:300068`、
    `fed:target_upper:300068`、`fed:target_lower:300068`、`us_core_cpi:300068`、
    `us_fed_rate:300068` —— **九条美国宏观指标全部被拼上了个股代码**。

    成因：`planner.SYSTEM_PROMPT` 写着「个股类指标**必须**拼接6位代码后缀
    （如 `PE(TTM):300308`）」，而 light 层小模型**过度套用**了这条规则 ——
    问句里既有美国宏观又有个股时，它把代码拼到了宏观指标上。
    **这不是提示词能治的**：同一个 1.5B 模型每次的过度套用位置都不一样
    （本项目已登记过多次"prompt 禁令挡不住措辞"）。所以做成**确定性的契约修正**。

    后果的严重性不在"多几条失败"：`fed:effr` 这些**正是问句的核心**
    （"美国的加息、降息节奏"）⇒ A08 那一轮**一条美国宏观数据都没有**，
    而错误文案看起来只是"某些指标没取到"。

    ## 判据**派生自登记表**，不是手写清单

    唯一的真值源是 `configs/indicators.yaml`（`IndicatorRegistry`）：

    * 登记形态**没有占位符**的指标（`fed:effr` / `us_cpi_yoy` / `M2` …）
      **本来就不接受后缀** ⇒ 掐掉尾段代码，改回登记形态；
    * 登记形态是**模板**的指标（`股息率:{code}` / `PE(TTM):{code}` …）——
      整串会命中模板 ⇒ **原样保留**（它本来就该带后缀）。

    所以规则只有一条：**整串查不到登记、但掐掉尾段 6 位数字能查到登记、
    且登记形态无占位符 ⇒ 掐掉**。库里新增一个宏观指标时它**自动生效**，
    不需要改这里（这正是"手写清单不会自己长大"的反面）。
    """
    registry = _indicator_registry()
    fixed: list[str] = []
    notes: list[str] = []
    for ind in indicators:
        base, sep, tail = ind.rpartition(":")
        # 只处理"尾段恰好是 6 位数字"的形态：`fed:rate_prob:2026-01-28`（日期）、
        # `ind:sw_third_pe_ttm:银行`（行业名）都不该被这条规则碰。
        if not sep or len(tail) != 6 or not tail.isdigit():
            fixed.append(ind)
            continue
        # ⚠️ **必须带后缀的两类先豁免**（个股类 / 行业类）。
        #   为什么不能只靠登记表：`商誉占净资产比` / `大股东质押比例` / `资产负债率`
        #   / `ROE` 这批在 `indicators.yaml` 里登记的是**裸名字**（该文件自己
        #   登记了这条既有缺口："同样解析不到带代码后缀的查询"）——
        #   只看登记表会把 `商誉占净资产比:300068` 这种**正确**的计划改坏，
        #   把它变成裸名 ⇒ A01 必然失败。实测：本轮第一版判据就踩了这个坑
        #   （`商誉占净资产比:300068 → 商誉占净资产比`），是自证用例抓出来的。
        if base in _CODE_SUFFIX_INDICATORS or base in _INDUSTRY_SUFFIX_INDICATORS:
            fixed.append(ind)
            continue
        if registry.get(ind) is not None:
            fixed.append(ind)          # 整串（含模板命中）本来就合法
            continue
        meta = registry.get(base)
        if meta is None or meta.is_template():
            fixed.append(ind)          # 掐掉也查不到 / 掐掉的是模板 ⇒ 不动它
            continue
        fixed.append(base)
        notes.append(
            f"去掉多余的代码后缀：{ind} → {base}"
            "（登记形态无 `{code}` 占位符，宏观/序列类指标不接受代码后缀）")
    return fixed, notes


def sanitize_indicators(
    indicators: list[str], target: str, analysis_type: str,
    industry: str = "",
) -> tuple[list[str], list[str], list[str]]:
    """修正 LLM 规划产出的指标契约，返回 (可用指标, 被丢弃, 被替换说明)。

    修的是 2026-09-26 实测到的一个真实故障链：

    用户问"请对当前A股**AI产业链各细分行业龙头**的估值及10月走势分析预测"，
    LLM 规划器（light 层小模型）返回：

        analysis_type = "macro", target = ""
        indicators = [..., "stock_close", "PE(TTM)", "PB", "资产负债率", "流动比率"]

    这些**裸个股指标**是契约违规 —— 个股指标必须拼 6 位代码
    （``PE(TTM):300308``）。而 target 为空，下游补不出后缀，于是 A01 去 fetch
    ``"PE(TTM)"``：**没有任何连接器 supports 它，瞬间 DataFetchError**。
    用户看到的"AI产业链龙头估值数据缺失 / 估值无任何输入数据 / valuation_calc=数据不足"
    全部源自这里 —— **本地库里明明躺着 82 只个股的 PE 与 80 只的 PB**（含最新交易日）。

    三类处理（**方向相反的两个错**都要挡）：
    0. **后缀加多了** → `strip_bogus_code_suffix()` 掐掉（`fed:effr:300068` → `fed:effr`，
       2026-09-29 实测：九条美国宏观指标被拼上个股代码 ⇒ 美联储那一问一条数据都没有）；
    1. **有 target 代码** → 补后缀（把 ``PE(TTM)`` 修成 ``PE(TTM):300308``）；
    2. **无 target** → 丢弃裸指标，并**换成行业级估值**（申万截面 PE/PB），
       因为"行业龙头的估值"在没有具体标的时，行业估值截面是唯一有数据可依的口径。
       换掉而不是静默丢弃，是为了让分析层仍有估值素材、而不是空手（否则 A10
       又会输出"估值数据不足"）。

    ★ 2026-09-29（PRD §19.17）：**同一形状的第三种裸名** —— 按**行业名**取数的族
    （`行业拥挤度` / `板块资金流` / `行业轮动`）。它们的后缀是行业名而不是代码，
    所以：
    1. **传了 `industry`** → 补行业名（``行业拥挤度`` → ``行业拥挤度:银行``）；
    2. **没传** → **摘掉**，且**不**换成申万截面（那是给"没有标的的个股估值"
       兜底的，与"问句里没有行业名"不是一回事）—— 留一个裸名等于自己制造一个
       必然失败的 fetch，而用户会把失败读成"平台没有这个数据"。
    """
    code = target if re.fullmatch(r"\d{6}", target or "") else ""
    # ★ 先掐掉**多余**的后缀（宏观指标被拼上个股代码），再做下面两类补/丢 ——
    #   顺序不能反：`fed:effr:300068` 的裸名判据看的是"fed"，先补后掐会互相打架。
    normalized, suffix_notes = strip_bogus_code_suffix(list(indicators))
    usable: list[str] = []
    dropped: list[str] = []
    dropped_industry: list[str] = []
    notes: list[str] = list(suffix_notes)
    for ind in normalized:
        if _needs_industry_suffix(ind):
            _, _, existing = ind.partition(":")
            if existing:
                usable.append(ind)          # 已带行业名 → 原样保留
            elif industry:
                fixed = f"{ind.split(':', 1)[0]}:{industry}"
                usable.append(fixed)
                notes.append(f"{ind} → {fixed}（补行业名）")
            else:
                dropped_industry.append(ind)
            continue
        if not _needs_code_suffix(ind):
            usable.append(ind)
            continue
        # 已经带 6 位代码的指标**必须原样保留**：LLM 有时会在 query 里提到多只
        # 个股并给出带后缀的指标，这时不能用 target 覆盖它 —— 那会把"分析 A 股"
        # 悄悄改成"分析 target 那一只"（测试 `test_already_suffixed...` 守着这条）。
        _, _, existing = ind.partition(":")
        if re.fullmatch(r"\d{6}", existing):
            usable.append(ind)
            continue
        if code:
            fixed = f"{ind.split(':', 1)[0]}:{code}"
            usable.append(fixed)
            notes.append(f"{ind} → {fixed}（补标的代码）")
            continue
        dropped.append(ind)
    if dropped_industry:
        notes.append(
            f"丢弃缺行业名的行业类指标 {dropped_industry}"
            "（连接器只认 `X:{行业名}`，裸名无人 supports；问句里没解析出行业时不硬凑）")
    if dropped:
        # 无可解析标的时用行业估值截面兜底（这些指标有真实数据源与入库作业）
        fallback = ["ind:sw_third_pe_ttm:all", "ind:sw_third_pb:all",
                    "ind:sw_first_pe_ttm:all"]
        added = [i for i in fallback if i not in usable]
        usable += added
        notes.append(
            f"丢弃无标的的个股指标 {dropped}（缺 6 位代码，无连接器支持）"
            f"，改用行业估值截面 {added}")
    return usable, dropped + dropped_industry, notes


def _apply_query_signal_augmentation(
    state: ResearchState, agents: list[str], indicators: list[str],
    *, target_for_focus: str = "",
) -> tuple[list[str], list[str], list[str]]:
    """问句信号增补的**唯一实现**（LLM 规划路径与规则兜底路径共用）。

    ## 为什么必须抽成一处

    两条路径（`supervisor_node` 的 LLM 分支与规则 `plan_run` 兜底分支）
    都要做同一件事：按问句里真实出现的领域信号补 Agent/指标。
    写两处必然漂移 —— 而漂移的方向是"某条路径下个股指标是裸名"，
    症状是 A01 去 fetch 一个**没有任何连接器 supports** 的名字、
    白等一次网络、然后报"估值数据缺失"（2026-09-26 实测过的故障链）。

    ## 拿不到 6 位代码时**提前摘掉**裸个股指标

    名称→代码的解析在 API 层（要读 5500 行名称表，不能阻塞事件循环）。
    这里拿不到代码时，裸指标（`PE(TTM)`）必须**现在**摘掉，而不是留给
    `sanitize_indicators` —— 后者按 `target` 补代码，而宏观问的 target
    是空的，它只会把裸指标丢进 `dropped` 并换成行业截面。
    两条路的结果一样（都不采个股），但提前摘掉能省一次必然失败的 fetch，
    并且日志更准确。拿不到代码时如实留给 A17 登记缺口。

    Returns: `(agents, indicators, notes)`。
    """
    code = str(state.get("focus_stock_code") or "")
    # ★ `CHG-0216`：问句里点名的**全部**个股（API 层解析好放进 state）。
    #   单值回退 `focus_stock_code` ⇒ 既有行为与既有测试逐字不变。
    codes: tuple[str, ...] = tuple(
        str(c) for c in (state.get("focus_stock_codes") or ()) if c)
    if not codes and code:
        codes = (code,)
    focus = target_for_focus or state.get("target", "")
    text = f"{focus} {state.get('user_query', '')}"
    agents, indicators, notes = augment_plan_by_query_signals(
        text, focus, agents, indicators, resolved_code=code,
        resolved_codes=codes,
        # ★ 2026-09-29：`analysis_type` 必须传下去 —— **`news` 管线不采数据**
        #   （信息层 A05/A06/A07 串行链，A01-A04 整体跳过，见
        #   `tests/integration/test_supervisor_graph.py::test_news_graph_info_pipeline`）。
        #   行业那三族是**采集类**指标，替一条 news 问句补上它们会把 A01 拉起来跑
        #   ——实测就是这样把那条集成测试打红的（"数据管线在 news 下整体跳过"）。
        analysis_type=str(state.get("analysis_type") or ""),
    )
    if not codes and "stock" in _query_agent_signals(text, focus):
        notes.append("问句命中个股信号但没有 6 位代码，个股指标已摘除"
                     "（由 A17 如实登记缺口）")
    return agents, indicators, notes


#: 已知**可达**的数据源名。A17 若把"缺数据"归因到这些源"不可访问"，那是**假缺口**。
#:
#: ## 为什么必须拦（2026-09-28 实测）
#:
#: 某轮 A17 的结论一边引用「目标区间 3.75%~4.00%、有效利率 3.88%」
#: （**数据就在同一个 prompt 里**），一边在 `data_gaps` 写
#: 「CME FedWatch利率概率（当前环境无法访问CME/FRED）」。
#:
#: 两个后果：
#:   ① 用户读到自相矛盾的结论（"用着 FRED 的数据说 FRED 访问不了"）；
#:   ② 这条假缺口**被入队**（纯字符串没带 status → 走"保守入队"）→
#:      A19 盘后为它做一次**注定失败**的补取尝试。
#:
#: 判据只认**源名**（机器可读），并配一个"可达性证据"——见
#: `tests/unit/test_gap_queue_false_positive.py`：拦掉的同时必须能说出
#: 这个源**当前真的可达**，否则就是我在瞎拦。
_FALSE_GAP_SOURCE_NAMES: tuple[str, ...] = (
    # en_id（库里实际的名字）
    "fed:effr", "fed:target_upper", "fed:target_lower",
    "fed:policy_range", "FRED",
    # ★ 中文别名也要认 —— 否则判据只认 en_id，A17 用中文写就漏拦。
    #   （这一条是 2026-09-28 实测补的：护栏自测时报「联邦基金利率不可访问」漏拦，
    #     而它正是同一件事的中文说法。与白名单只写中文标签那个 bug 是**同一类**：
    #     **同一个概念有两种写法，判据只认其中一种就必然漏**。）
    "联邦基金利率", "政策利率", "目标区间", "有效利率",
)


def _is_false_gap(item: str, status: str) -> bool:
    """这条缺口是不是"把可达的源说成不可访问"的**假缺口**。

    判据（三条同时成立才算假缺口，避免误伤）：
      ① 文案里出现"不可访问/无法访问/不可达/受限"这类**可达性归因**；
      ② 文案里点名了一个**已知可达**的源（`_FALSE_GAP_SOURCE_NAMES`）；
      ③ A17 **没有**把它标记成 `unavailable` —— 标了就说明它知道那是环境限制，
         属于如实登记（只是不该牵连 FRED），交给提示词纠正即可，不必拦。

    Returns: True = 假缺口，不入队。
    """
    if status in ("unavailable", "source_terminated"):
        return False                      # 如实登记，放行（由提示词纠正表述）
    text = str(item or "")
    reachability = ("不可访问", "无法访问", "不可达", "访问不了", "受限",
                    "无法获取", "访问不到")
    if not any(k in text for k in reachability):
        return False
    return any(src in text for src in _FALSE_GAP_SOURCE_NAMES)


def _resolve_gap_indicator(item: str) -> str:
    """把 A17 报缺口的**人话**解析成 indicator id（解析不出返回空串）。

    A17 的 `data_gaps` 写的是人话（"三市分项成交额"、"限售解禁规模"），
    不是 indicator id。本函数尝试用 `IndicatorRegistry.resolve()`
    做一次名→id 解析（含模板展开 + n-gram 兜底）。

    **解析不出不是错误** —— 调用方会用原文入队。A19 本来就是
    "按指标名生成连接器"的 Agent，拿到人话也能理解。
    """
    text = str(item or "").strip()
    if not text:
        return ""
    # 去掉常见修饰（"未提供"/"缺失"/"数据"），提高命中率
    for noise in ("未提供", "缺失", "数据", "规模", "信息", "明细"):
        text = text.replace(noise, "")
    text = text.strip("（()）:：、，,。 ")
    if not text:
        return ""
    try:
        from src.infrastructure.catalog.registry import get_registry

        meta, how = get_registry().resolve(text, fuzzy=True)
        if meta is not None and how != "miss":
            return meta.indicator
    except Exception:  # noqa: BLE001 解析是增强能力，失败用原文
        pass
    return ""


def _summary(output: AgentOutput) -> dict[str, Any]:
    return {
        "agent_id": output.agent_id,
        "agent_name": agent_name(output.agent_id),
        "conclusion": output.conclusion,
        "confidence": output.confidence.value,
        "confidence_zh": confidence_zh(output.confidence.value),
        "data_refs": output.data_refs,
        "result": output.result,
    }


# ======================================================================
# ★★★ 2026-10-07：A18 审计结论的**机器可读三态**（`CHG-0190` ① / PRD §46）
# ======================================================================

#: 审计结论的两个正常取值。
AUDIT_VERDICT_PASS = "通过"
AUDIT_VERDICT_FAIL = "不通过"
#: ⚠️ 第三个取值：**审计没跑成**。绝不是"通过"。
#: 本项目纪律：「没量到」≠「量到 0」——一张**伪造的合格证**比缺结论危险得多。
AUDIT_VERDICT_UNMEASURED = "未量到"


def _audit_summary(output: AgentOutput | None, *, error: str = "") -> dict[str, Any]:
    """把 A18 的结论压成一段**能被代码读**的字段（写进 `state["audit"]` 与任务记录）。

    为什么需要它（本轮报障，`CHG-0190` ①）：A18 判出"不通过"之后，**没有任何自动化
    消费方** —— 它只影响 A18 自己的 confidence 档位，外加 `_render_report` 把
    `conclusion` 抄进 Markdown。于是"审计能判不通过"与"不通过会改变交付"是两件事，
    中间缺的正是这个字段。

    三态口径（**必须三态**）：`通过` / `不通过` / `未量到`。
    审计异常、没产出结论时给 `未量到` —— 不许把"没审"写成"通过"。
    """
    if output is None:
        return {
            "verdict": AUDIT_VERDICT_UNMEASURED,
            "failed": True,
            "chain_valid": None,
            "chain_count": None,
            "chain_broken_at": None,
            "completeness_issues": [],
            "issue_count": 0,
            "exempt_count": 0,
            "collection_gap_count": 0,
            "llm_calls": None,
            "sealed_seq": None,
            "error": error,
        }
    result = dict(output.result or {})
    issues = [str(x) for x in (result.get("completeness_issues") or [])]
    verdict = str(result.get("verdict") or "").strip() or AUDIT_VERDICT_UNMEASURED
    return {
        "verdict": verdict,
        #: `failed` 把三态压成一问"这份交付有没有被审计背书"（未量到 = 没有背书）
        "failed": verdict != AUDIT_VERDICT_PASS,
        "chain_valid": result.get("chain_valid"),
        "chain_count": result.get("chain_count"),
        "chain_broken_at": result.get("chain_broken_at"),
        "completeness_issues": issues,
        "issue_count": len(issues),
        "exempt_count": int(result.get("exempt_count") or 0),
        "collection_gap_count": int(result.get("collection_gap_count") or 0),
        "llm_calls": result.get("llm_calls"),
        "sealed_seq": result.get("sealed_seq"),
        "error": error,
    }


def _audit_banner(summary: dict[str, Any] | None) -> list[str]:
    """审计不通过/未量到时，在报告**标题正下方**给一句显式警告；通过则**什么都不加**。

    位置选在标题下方是刻意的：报告是用户唯一会读的交付物，而原先"审计不通过"
    只出现在**文末**的「## 审计」段里 —— 读到那里的人早就读完结论了。
    通过时**一个字都不加**：否则"警告"会因为天天出现而变成背景噪音。
    """
    if not summary:
        return []
    verdict = summary.get("verdict")
    if verdict == AUDIT_VERDICT_PASS:
        return []
    if verdict == AUDIT_VERDICT_UNMEASURED:
        reason = str(summary.get("error") or "审计未产出结论")
        return [f"> ⚠️ **审计未完成（未量到，不等于通过）**：{reason}", ""]
    bits = [f"完整性问题 {summary.get('issue_count', 0)} 项"]
    if summary.get("chain_valid") is False:
        bits.append(f"哈希链在 seq={summary.get('chain_broken_at')} 断链")
    return [f"> ⚠️ **审计未通过**：{'；'.join(bits)}。详见文末「审计」。", ""]


def _log_audit_verdict(summary: dict[str, Any], *, task_id: str) -> None:
    """审计不通过/未量到时出声（管理员侧可见）。通过时保持安静。

    为什么必须有：A18 原先的结论**没有下游消费者**，所以"链断了"这种事
    在日志里也看不出来 —— 只有下一次有人手动 verify 才会发现。
    """
    if not summary.get("failed"):
        logger.info("A18 审计通过：链 %s 条，问题 0 项（task=%s）",
                    summary.get("chain_count"), task_id)
        return
    logger.warning(
        "★ A18 审计未通过（task=%s）：verdict=%s 链valid=%s 断链位=%s "
        "完整性问题=%s 项 未量到=%s",
        task_id, summary.get("verdict"), summary.get("chain_valid"),
        summary.get("chain_broken_at"), summary.get("issue_count"),
        summary.get("verdict") == AUDIT_VERDICT_UNMEASURED,
    )


# ======================================================================
# ★ 2026-09-28 第二十五轮：A17 `query_data` 的**第四跳 = 联网兜底**
#
# 用户原话（2026-09-28）：
#   「不要指望靠提示词让 Agent『优先查本地库』…要把『找数据』做成一条
#     确定性流水线：元数据目录 → 语义解析 → 实体/指标链接 → 查询计划 →
#     统一网关执行 → **空结果诊断** → 反馈治理。」
#   「连接器找不到、指标等级里无 id、INDICATOR_CATALOG 目录里没有，
#     **都要自动去联网获取数据，做最差的兜底，一定要找到数据**。」
#
# ## 为什么这段代码必须存在（本仓库实测的失败模式）
#
# `catalog/network_fallback.py`（71 KB，四条护栏齐全，自带 30 条全绿测试）
# 曾经**一个生产调用方都没有** —— `grep` 全仓库只命中它自己、它自己的测试
# 和一个 `docs/` 证据脚本。按《护栏保真》的四道门，它卡在
# **第三道门「有没有人调它」**，而且**不报错**：
# 护栏写得越完整，这个状态越像"已经做完了"。
#
# ## 四跳的顺序就是优先级（顺序即策略，不靠提示词）
#
#   ① 本次已采集的 `validated_points`（内存，最便宜）
#   ② `LocalDataExecutor.metric_series`（本地库，意图直达列）
#   ③ `_query_data_via_connectors`（A01 的 `ConnectorRouter`，同一个实例）
#   ④ **`data_fallback_hop`（联网兜底，四条护栏 + 每日预算 + 小时上限 + 冷却）**
#
# 每一跳都只在**上一跳真的没给出数据**时才发生；第四跳还要再过
# `should_fallback()` 的诊断码判据（`NOT_APPLICABLE_FOR_ENTITY` 与 5 个
# 环境维度码**永远不联网** —— 联网拿不到同一口径，只会白花预算）。
#
# ## 输出形状**没变**
#
# `query_data` 的返回值仍然是 `str`（`ToolRegistry.execute` 用
# `str(result)` 喂给 LLM，见 `domain/agents/analysis/react.py`）。
# 结构化结论单独由 `DataFallbackOutcome` 承载 ——
# 把判据塞进给 LLM 看的散文里，等于让护栏的验收依赖文案。
# ======================================================================

#: 「真实数据点」渲染成文本后的行首格式：`- <期间>: <值> (来源<X>)`。
#:
#: 单一事实源：测试用它断言"未量到的结果里**一行数据都不许有**"
#: （= 没有 0、也没有占位数值）。护栏的判据必须是**结构**，不是文案。
#:
#: ⚠️ 必须 `MULTILINE`：结果文本是**多行**的（第一行是"（联网兜底 …）"表头），
#: 没有 MULTILINE 时 `^` 只匹配整个字符串的开头 → 正则**永远不命中** →
#: "不许出现数据行"那条断言会**恒真**（假绿）。
#: 这个坑由 `test_real_points_are_rendered_with_traceable_source`（正向对照）
#: 钉住：它要求有数据时**必须**命中同一个正则。
DATA_LINE_RE: re.Pattern[str] = re.compile(r"^\s*-\s*\S+:\s*\S+", re.MULTILINE)


#: 第四跳的**接线豁免登记表**：`诊断码 → 为什么不联网`。**默认必须是空的。**
#:
#: ⚠️ 它只可能把"该联网"改成"**不**联网"（fail-closed 方向），
#: 所以即便被填错也不会多花钱 —— 这是刻意设计的：**唯一允许的豁免方向是更保守**。
#: 想反过来（让一个不在触发表里的码去联网）必须改
#: `network_fallback._NO_FALLBACK_REASONS`，那才是唯一的事实源。
#:
#: 为什么要有这张表而不是"需要时直接写 if"：AGENTS.md《护栏必须带过期语义》——
#: 任何"临时豁免"都会腐烂成永久豁免（本项目实测过 `_KNOWN_UNGUARDED` 那条路）。
#: 集中在一处 + `tests/unit/test_fallback_wiring.py` 断言它是空的，
#: 于是**加一条就必须改一条测试并说明什么时候删**。
_FALLBACK_TRIGGER_EXEMPTIONS: dict[str, str] = {}


#: 第四跳单次**渲染**的行数上限（条）。**写进代码，不是注释里的"一般不会超过"**。
#:
#: 为什么必须有它：`limit` 来自 **LLM 的工具调用参数**（`query_data(indicator, limit)`），
#: 而这段文本会**原样回到 LLM 的上下文**里 —— 没有上限就等于让模型用一次参数
#: 把上下文顶满（AGENTS.md《AI 首轮编码硬约束》：成本/并发/**输出预算**都要有
#: 实测数字 + 超限行为）。
#:
#: 取 20 的依据：与 `network_fallback.RECENT_EVENTS_MAX = 20` 同量级
#: （那个是账本快照的有界条数，本项目唯一的"给 LLM/界面的有界条数"基线）；
#: A17 拿 20 行足够判断趋势，而联网兜底本来就是"补一个缺口"，不是搬全量。
#: **超限行为**：按期间降序只渲染最新 `MAX_FALLBACK_ROWS` 行；
#: 且表头**如实写明**"源共返回 N 条、带数值 M 条、本次最多列 R 行" ——
#: 截断必须可见（本仓库实测过：收容/截断做成隐式就会让下游把"少了"读成"没有"）。
MAX_FALLBACK_ROWS: Final[int] = 20


@dataclass(frozen=True)
class DataFallbackOutcome:
    """`query_data` 第四跳（联网兜底）的**结构化结论**（机器可验的那一半）。

    `text` 是给 LLM 的那一段（`query_data` 的返回值形状仍是 `str`，见上面）；
    其余字段是判据 —— 验收断言读它们，**不读散文**。
    """

    #: `should_fallback()` 判为"该联网"（诊断码属于 `FALLBACK_TRIGGER_CODES`）。
    triggered: bool
    #: 触发/不触发的人话理由（`should_fallback()` 原文，不重写）。
    trigger: str
    #: 护栏是否**放行**（与"有没有取到"是两件事）。
    allowed: bool = False
    #: 是否拿到**真实**数据点（`False` = **未量到**，绝不是"量到 0"）。
    measured: bool = False
    #: 机器可读判据（`network_fallback.ReasonCode`）。
    code: str = ""
    #: 拒绝/失败原因原文（人话 + 剩余额度/剩余时间）。
    reason: str = ""
    #: 取到数据的源（可追溯；`RouterSourceBridge.source_key()` 口径）。
    source: str = ""
    #: 给 LLM 的那一段文本（未量到时必含 `UNMEASURED`）。
    text: str = ""
    #: 真实数据点（连接器原样返回，带 `source_name`/`source_url` 溯源字段）。
    points: list[Any] = field(default_factory=list)


class _CodeOnlyResult:
    """只带诊断码的鸭子类型结果 —— `should_fallback()` 的输入契约。

    `should_fallback()` 明确只读 `diag.code` 与 `plan`（见它的 docstring：
    不 import `local_data` 才不会循环 import），所以这里就给这两个字段。
    构造方式与 `tests/unit/test_network_fallback.py::_Result` 一致 ——
    两条路径喂给同一个判据，结果必须相同。
    """

    __slots__ = ("diag", "plan")

    def __init__(self, code: str) -> None:
        self.diag = type("_Diag", (), {"code": code})()
        self.plan: dict[str, Any] = {}


def _fallback_trigger(res: Any, local_error: BaseException | None) -> tuple[bool, str]:
    """该不该联网 —— **唯一实现**是 `network_fallback.should_fallback()`。

    本函数只负责把"本地执行器**自己抛异常**"（`res is None`）翻译成诊断码：

    · `res is None` 意味着**本地这一跳根本没量到**（不是"本地有数据且新鲜"）。
      按 `CONN_FAIL` 登记 —— 触发表里它的理由是"本地库/源连不上，在线源是
      此时唯一的出路"，正是这个语义。原始异常写进 detail，可追溯。
    · ⚠️ 刻意**不**登记成 `NO_DATA`：`NO_DATA` 的处置是"入缺口队列、补采"，
      `CONN_FAIL` 的处置是"查连接"。混用会把排查方向带偏 ——
      这是本仓库 `local_data.DiagCode` 存在的全部理由。
    · 传入的 `should_fallback` 判据**不做任何本地二次判断**（不另写一套表），
      所以新增诊断码时行为由 `tests/unit/test_fallback_wiring.py` 的
      "两张表并集 == DiagCode 全集"那条断言统一管住。
    · 唯一的本地分支是 `_FALLBACK_TRIGGER_EXEMPTIONS`（默认空），
      且它只能把结论从"联网"改成"不联网"（更保守的方向）。
    """
    from src.infrastructure.catalog.network_fallback import should_fallback

    code = ""
    if res is None and local_error is not None:
        from src.infrastructure.catalog.local_data import DiagCode

        code = DiagCode.CONN_FAIL
        res = _CodeOnlyResult(code)

    triggered, why = should_fallback(res)
    if triggered and code and code in _FALLBACK_TRIGGER_EXEMPTIONS:
        # 豁免只能更保守（见登记表说明）：把"该联网"降级成"不联网"，并带上理由。
        return False, (f"本地诊断码 {code} 已登记为**暂缓联网兜底**："
                       f"{_FALLBACK_TRIGGER_EXEMPTIONS[code]}")
    return triggered, why


def _pget(point: Any, key: str, default: Any = None) -> Any:
    """数据点的字段读取：`dict`（已采集点）与对象（`DataPoint`）都能读。"""
    if isinstance(point, dict):
        return point.get(key, default)
    return getattr(point, key, default)


def _render_fallback_points(points: list[Any], *, source: str,
                            limit: int) -> tuple[str, int, int]:
    """联网取回的数据点 → 可追溯文本。返回 `(文本, 量到条数, 未量到条数)`。

    ★ **「没量到」与「量到 0」在这里分开**（AGENTS.md 硬约束）：

      · `value is None` 的点**不是 0** —— 它没有值。直接跳过并**单独计数**，
        绝不渲染成 `0`、也不渲染成空串。
      · 只有真正带数值的点才进 `文本`，且每行都带来源
        （`source_name` 缺失时退回**兜底源名**，仍然可追溯）。
    """
    rows = sorted(points, key=lambda p: str(_pget(p, "period_date", "") or ""),
                  reverse=True)
    lines: list[str] = []
    measured = 0
    unmeasured = 0
    for point in rows:
        value = _pget(point, "value", None)
        if value is None:
            unmeasured += 1
            continue
        if measured >= limit:
            continue
        src = str(_pget(point, "source_name", "") or source)
        period = _pget(point, "period_date", None) or "期间未标注"
        lines.append(f"- {period}: {value} (来源{src})")
        measured += 1
    return "\n".join(lines), measured, unmeasured


def _backend_of(agents: dict[str, Any]) -> Any | None:
    """A01 的采集后端（第三跳与第四跳用的是**同一个**实例）。"""
    collector = agents.get("A01_data_collector")
    return getattr(collector, "_backend", None)  # noqa: SLF001 与第三跳同一口径


#: 跨通道判定结论码（稳定、可枚举；同时写进标注与采集异常）。
CROSS_CHANNEL_KIND: Final[str] = "cross_channel"
CROSS_CHANNEL_FRESH: Final[str] = "kept_local_fresh"
CROSS_CHANNEL_UPGRADED: Final[str] = "used_online"
CROSS_CHANNEL_STALE_KEPT: Final[str] = "kept_local_stale"
CROSS_CHANNEL_DISAGREE: Final[str] = "kept_local_online_differs"
CROSS_CHANNEL_NO_TOLERANCE: Final[str] = "no_declared_tolerance"


@dataclass(frozen=True)
class CrossChannelVerdict:
    """跨通道一致性判定的结论（**口径见 PRD §三十三 / `CHG-0157`**）。"""

    code: str
    #: 拼进本地正文的标注（空串 = 无需标注）
    note: str = ""
    #: 用在线值时的正文（空串 = 用本地）
    text: str = ""
    #: 结构化事实（写进采集异常，便于事后复核"当时差多少"）
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def used_online(self) -> bool:
        return self.code == CROSS_CHANNEL_UPGRADED


def _declared_tolerance_days(indicator: str) -> float | None:
    """该指标**声明**的新鲜度容忍（天）；未登记 ⇒ `None`（**不猜**）。

    判据来源是登记表自己的 `freshness_hours`（默认 24h），
    不是在这里另定一套天数 —— "同一件事只允许一份实现"。
    """
    try:
        from src.infrastructure.catalog.registry import get_registry

        reg = get_registry()
        meta = reg.get(indicator) or reg.get_by_base(
            str(indicator).partition(":")[0])
        if meta is None:
            return None
        return max(0.0, float(getattr(meta, "freshness_hours", 0.0))) / 24.0
    except Exception:  # noqa: BLE001 登记表读不到 ⇒ 当作未登记（不升级）
        logger.debug("新鲜度容忍读取失败（按未登记处理）: %s", indicator,
                     exc_info=True)
        return None


def _local_latest(res: Any) -> tuple[str, Any]:
    """本地序列里期次最新的一条 → `(期次, 值)`；取不到返回 `("", None)`。"""
    best_period, best_value = "", None
    for p in getattr(res, "points", None) or []:
        period = str((p or {}).get("period") or "")
        if period > best_period:
            best_period, best_value = period, (p or {}).get("value")
    return best_period, best_value


def _record_cross_channel(indicator: str, verdict: CrossChannelVerdict, *,
                          state: Any) -> None:
    """把跨通道结论记进**采集异常**（一次一条，绝不抛）。

    为什么要记：`hop_stats` 只回答"哪一跳答出来的"，
    而"本地其实已经陈旧了"这件事在命中统计里**看不见** ——
    它正是本轮要暴露的那一类（否则"本地命中率很高"会掩盖"命中的是旧数据"）。
    """
    try:
        from src.core.collection_anomalies import record as _record

        detail = " ".join(f"{k}={v}" for k, v in verdict.facts.items())
        _record(CROSS_CHANNEL_KIND, indicator,
                f"{verdict.code}: {detail}".strip(),
                task_id=str((state or {}).get("task_id") or ""))
    except Exception:  # noqa: BLE001 观测失败绝不影响取数
        logger.debug("跨通道异常记录失败（忽略）", exc_info=True)


async def _cross_channel_decide(key: str, res: Any, *, limit: int,
                                state: ResearchState,
                                agents: dict[str, Any]) -> CrossChannelVerdict:
    r"""本地已命中时，判断**要不要因为陈旧而升级到在线**，以及"两条都拿到"怎么处置。

    ## 口径（`CHG-0157`，写进 PRD §三十三）

    **R1 顺序不变**：本地优先这条链顺序**不动**（实测本地一次 ~85ms，
    联网是秒级；且本地是我们自己落库的快照）。
    **R2 陈旧才升级**：判据是**该指标声明的新鲜度**（`freshness_hours`），
    **不是**这里另拍一个天数 —— 与 `SmartFetcher` 判 stale 用的是同一个契约。
    未登记容忍度 ⇒ **不升级**（不猜），但如实标注。
    **R3 两条都拿到时不静默改数**：
      · 在线期次**更新** ⇒ 用在线值（这是"修数据"的正当事由），并标注；
      · 期次相同但**值不同**（或在线更旧）⇒ **保留本地**（顺序优先）+ 记异常，
        因为"在线抖动就静默改数"比"用一条已知陈旧但稳定的值"更难查；
      · 在线取不到 ⇒ 保留本地 + 如实标注"陈旧且在线不可得"。
    **R4 必须留痕**：无论哪一支都记一条采集异常（`kind=cross_channel`）。
    """
    from src.infrastructure.catalog.network_fallback import UNMEASURED

    plan = getattr(res, "plan", None) or {}
    stale_days = float(plan.get("stale_days") or 0.0)
    local_period, local_value = _local_latest(res)
    base_facts = {"stale_days": round(stale_days, 1),
                  "local_period": local_period or UNMEASURED,
                  # ★ 选库原因（`CHG-0156` 的 `PathReason`）跟着事实一起记：
                  #   "为什么是这张表"与"这张表陈旧了"是同一个决定的两半，
                  #   分开记会让事后复核要跑两次探针才能还原现场。
                  #   ⚠️ 它只进**管理员侧**的采集异常，不进用户可见文案。
                  "path_why": str(plan.get("path_why") or "") or UNMEASURED}

    tolerance = _declared_tolerance_days(key)
    if tolerance is None:
        verdict = CrossChannelVerdict(
            CROSS_CHANNEL_NO_TOLERANCE,
            note="（未登记新鲜度容忍 ⇒ 未做跨通道比对）",
            facts=base_facts)
        # 未登记是"我们没有契约"，不是缺陷：不记异常，避免按未登记量刷屏
        return verdict
    if stale_days <= tolerance:
        return CrossChannelVerdict(CROSS_CHANNEL_FRESH, facts={
            **base_facts, "tolerance_days": round(tolerance, 2)})

    facts = {**base_facts, "tolerance_days": round(tolerance, 2)}
    fetched = await _connector_probe(key, limit, state=state, agents=agents)
    if not fetched:
        verdict = CrossChannelVerdict(
            CROSS_CHANNEL_STALE_KEPT,
            note=(f"（⚠️ 本地已陈旧 {stale_days:.0f} 天 > 声明容忍 "
                  f"{tolerance:.1f} 天；在线通道本次未取到，故仍用本地值）"),
            facts={**facts, "online": "unavailable"})
        _record_cross_channel(key, verdict, state=state)
        return verdict

    probe, points = fetched
    online_period = max((str(getattr(p, "period_date", "") or "") for p in points),
                        default="")
    same_period = bool(online_period) and online_period == local_period
    online_same_value = any(
        str(getattr(p, "value", "")) == str(local_value)
        for p in points if str(getattr(p, "period_date", "")) == local_period)
    facts = {**facts, "online": probe, "online_period": online_period or UNMEASURED}

    if online_period > local_period and not same_period:
        rows = sorted(points, key=lambda p: str(getattr(p, "period_date", "")),
                      reverse=True)[:limit]
        body = "\n".join(
            f"- {getattr(p, 'period_date', '?')}: {getattr(p, 'value', '?')} "
            f"(来源{getattr(p, 'source_name', '?')})" for p in rows)
        verdict = CrossChannelVerdict(
            CROSS_CHANNEL_UPGRADED,
            note=(f"（本地陈旧 {stale_days:.0f} 天 > 声明容忍 {tolerance:.1f} 天；"
                  f"已用在线刷新到 {online_period}）"),
            # ⚠️ 理由必须**跟着数据走**：这条正文是给 LLM 与用户看的，
            #   只给在线值、不说"为什么换源"，等于把一次改数据变成静默行为。
            text=(f"（跨通道刷新 {probe}，共 {len(points)} 条；"
                  f"本地陈旧 {stale_days:.0f} 天 > 声明容忍 {tolerance:.1f} 天，"
                  f"已用在线刷新到 {online_period}）\n{body}"),
            facts=facts)
        _record_cross_channel(key, verdict, state=state)
        return verdict

    if same_period and not online_same_value:
        verdict = CrossChannelVerdict(
            CROSS_CHANNEL_DISAGREE,
            note=(f"（⚠️ 本地与在线同期次 {online_period} 数值不一致："
                  f"本地 {local_value} / 在线 {getattr(points[0], 'value', '?')}；"
                  f"仍用本地值，差异已留痕）"),
            facts={**facts, "local_value": local_value,
                   "online_value": str(getattr(points[0], "value", ""))})
        _record_cross_channel(key, verdict, state=state)
        return verdict

    verdict = CrossChannelVerdict(
        CROSS_CHANNEL_STALE_KEPT,
        note=(f"（⚠️ 本地已陈旧 {stale_days:.0f} 天 > 声明容忍 "
              f"{tolerance:.1f} 天；在线期次 {online_period or UNMEASURED} "
              f"未更新，故仍用本地值）"),
        facts=facts)
    _record_cross_channel(key, verdict, state=state)
    return verdict


async def _connector_probe(key: str, limit: int, *,
                           state: ResearchState,
                           agents: dict[str, Any]) -> tuple[str, list[Any]] | None:
    """第三跳的**取数本体**（唯一实现）：返回 `(命中的 probe, 结构化 points)`。

    为什么从 `_query_data_via_connectors` 里拆出来：跨通道比对需要**结构化**结果，
    而那个函数返回的是**给 LLM 看的文案**。让比对去解析文案 = "把判据塞进措辞"
    （本项目明令禁止：验收会依赖文案）。拆出本体之后，文案版与比对版**共用同一次取数**，
    不会出现"两次取数结果不同"的问题。

    失败**绝不抛**（返回 `None`），行为与原来逐字一致。
    """
    backend = _backend_of(agents)
    if backend is None:
        return None
    metric, _, entity = key.partition(":")
    code = (entity or str(state.get("focus_stock_code") or "")).strip()
    if entity and not code.isdigit():
        # 实体是中文名（如「招商银行」）→ 用同义词字典解析成代码再试
        try:
            from src.infrastructure.catalog.synonym_dict import (
                resolve_entity,
            )

            hits = resolve_entity(entity)
            code = hits[0] if hits else ""
        except Exception:  # noqa: BLE001 字典不可用时保持原样
            code = ""
    probes = [key]
    if metric and code:
        probes.append(f"{metric}:{code}")
    for probe in dict.fromkeys(probes):
        try:
            # ★ 防撞钟：这一跳是**交互路径**（A17 的工具调用），
            #   必须带预算 —— 否则一条病态慢的指标会把 A17 卡到超时。
            points = await backend.fetch(
                probe, deadline_sec=query_deadline_sec())
        except Exception as exc:  # noqa: BLE001 单指标失败不阻断
            logger.debug("query_data 连接器兜底失败(%s): %s", probe, exc)
            continue
        if not points:
            continue
        return probe, list(points)
    return None


async def _query_data_via_connectors(key: str, limit: int, *,
                                     state: ResearchState,
                                     agents: dict[str, Any]) -> str:
    """第三跳：把指标交给 A01 的采集路由（连接器链）试一次。

    ## 为什么需要这一跳（实测驱动，不是设计想象）

    本会话实测：并发清理期间 `quant_daily_basic` 一度从库中消失，
    而**同一个口径 `股息率TTM:600036` 仍能从源取到 4915 条**。
    也就是说"本地库没有"**不等于**"数据找不到" ——
    中间还隔着连接器这一层能力。

    ## 复用而不是另造（AGENTS.md：「同一判断只允许一份实现」）

    走的是 `A01_data_collector` 的 backend ——
    **与主采集链路同一个 `ConnectorRouter` 实例**，
    因此自带故障转移、失败冷却、新鲜度下限，成功率与主链路一致。

    失败**绝不抛**：第三跳是兜底，它坏了不能拖垮主链（返回空串，
    由调用方给出本地诊断）。

    ★ 取数本体已抽到 `_connector_probe()`（`CHG-0157`）：跨通道比对需要结构化结果，
    而本函数返回的是**给 LLM 看的文案**。两者共用同一次取数，行为逐字未改。

    ⚠️ 2026-09-28 第二十五轮：本函数从 `recommend_node` 内的闭包**提到模块级**
    （只加了 `state`/`agents` 两个显式参数，行为逐字未改）——
    否则第四跳没法单独验收，而"没人调它"正是本轮要修的缺陷本身。
    """
    fetched = await _connector_probe(key, limit, state=state, agents=agents)
    if not fetched:
        return ""
    probe, points = fetched
    rows = sorted(
        points,
        key=lambda p: str(getattr(p, "period_date", "")),
        reverse=True)[:limit]
    body = "\n".join(
        f"- {getattr(p, 'period_date', '?')}: "
        f"{getattr(p, 'value', '?')} "
        f"(来源{getattr(p, 'source_name', '?')})"
        for p in rows
    )
    return f"（连接器兜底 {probe}，共 {len(points)} 条）\n{body}"


async def _fallback_fetch(
    indicator: str, *, agents: dict[str, Any], context: str, trigger: str,
) -> Any:
    """**联网兜底的唯一取数实现**（两条生产链路共用）。

    ## 为什么必须只有一处调 `get_network_fallback`

    有一条常驻护栏
    `tests/unit/test_fallback_wiring.py::test_the_singleton_is_read_from_exactly_one_place_in_supervisor`
    —— 它要求 supervisor 里**恰好一处**读那个单例。理由不是洁癖：
    两处各自的护栏/预算/冷却组合会漂移，而"哪一条链路多花了一次钱"
    在账本上看不出来。

    所以：
      · `data_fallback_hop`（A17 的 `query_data` 工具路径）调本函数；
      · `_network_lookup_for_collection`（采集路径，用户口径"找不到就去联网找"）调它。
    两条链路**同一份白名单/预算/冷却/防撞钟**。

    取数实现不可用（A01 后端未装配）时返回 `None`；**绝不抛**。
    """
    backend = _backend_of(agents)
    if backend is None:
        return None
    from src.infrastructure.catalog.network_fallback import (
        get_network_fallback,
        routes_from_router,
    )

    fb = get_network_fallback(routes=routes_from_router(backend))
    return await fb.fetch_outcome(
        indicator, context=context, trigger=trigger)


async def data_fallback_hop(key: str, res: Any, *, limit: int,
                            state: ResearchState, agents: dict[str, Any],
                            local_error: BaseException | None = None,
                            ) -> DataFallbackOutcome:
    """**第四跳（联网兜底）—— 本轮的 call site。**

    这是 `NetworkFallback` 在**生产请求链路**上的唯一入口：
    本地三跳全空且 `should_fallback()` 判为"该联网"时，真的走一次
    `get_network_fallback()`；护栏放行才联网，护栏拒绝就**如实记录拒绝原因**
    （人话 + 剩余额度/剩余时间），绝不把"护栏拒绝"说成"数据不存在"。

    ## 取数实现与 `ConnectorRouter` **同一份 routes**

    `get_network_fallback(routes=routes_from_router(backend))`：
    与第三跳同一个 `A01._backend`。白名单与候选源的交集由
    `NetworkFallback._candidate_sources()` 算 —— 这里不重写判据。

    ## 「没量到」绝不写成 0

    返回 `measured=False` 且 `text` 里出现 `UNMEASURED`；`points` 为空列表。
    取到的点若**每个都没有值**（`DataPoint.value is None`），同样算未量到，
    并把"源返回了 N 条但没有一条带数值"如实写出来 ——
    这是"没量到"与"量到 0"的边界，混了就会让 LLM 把空值读成 0。

    ## 绝不抛

    护栏本身就不抛（`fetch_outcome()` 只返回结论）；这里再包一层，
    保证第四跳**永远不会**把异常带进 `query_data`（兜底是"最差的一层"）。
    """
    from src.infrastructure.catalog.network_fallback import UNMEASURED

    try:
        triggered, trigger = _fallback_trigger(res, local_error)
        if not triggered:
            return DataFallbackOutcome(triggered=False, trigger=trigger,
                                       code="NOT_TRIGGERED", reason=trigger)

        outcome = await _fallback_fetch(
            key, agents=agents, context=f"query_data:{key}", trigger=trigger)
        if outcome is None:
            return DataFallbackOutcome(triggered=True, trigger=trigger,
                                       measured=False, code="NO_FETCHER",
                                       reason="联网兜底没有可用的取数实现"
                                              "（A01 后端未装配）")

        body, measured_n, unmeasured_n = _render_fallback_points(
            outcome.points, source=outcome.source,
            limit=max(1, min(int(limit), MAX_FALLBACK_ROWS)))
        if measured_n:
            head = (f"（联网兜底 {outcome.source}，源返回 {len(outcome.points)} 条，"
                    f"其中带数值 {measured_n} 条、{UNMEASURED} {unmeasured_n} 条，"
                    f"本次按上限最多列 {MAX_FALLBACK_ROWS} 行")
            if unmeasured_n:
                head += "（没值的那些点不是 0）"
            head += f"；触发理由：{trigger}）"
            return DataFallbackOutcome(
                triggered=True, trigger=trigger, allowed=outcome.allowed,
                measured=True, code=outcome.code, reason=outcome.reason,
                source=outcome.source, text=f"{head}\n{body}",
                points=list(outcome.points))

        extra = ""
        if outcome.points and not measured_n:
            extra = (f"源『{outcome.source}』返回了 {len(outcome.points)} 个条目，"
                     f"但**没有一个带数值** → 仍然是{UNMEASURED}（不是 0）。")
        elif unmeasured_n:
            extra = f"另有 {unmeasured_n} 个条目没有值 → 它们{UNMEASURED}（不是 0）。"
        return DataFallbackOutcome(
            triggered=True, trigger=trigger, allowed=outcome.allowed,
            measured=False, code=outcome.code, reason=outcome.reason,
            source=outcome.source, text=extra, points=[])
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 第四跳坏了不能拖垮 query_data
        logger.warning("query_data 联网兜底异常（已吞）：%s -> %s", key, exc,
                       exc_info=True)
        return DataFallbackOutcome(
            triggered=True, trigger="", measured=False, code="EXCEPTION",
            reason=f"联网兜底这一跳自身异常：{type(exc).__name__}: {exc}"
                   f"（{UNMEASURED}：异常不等于源上没有数据）")


def _fallback_line(fallback: DataFallbackOutcome) -> str:
    """把第四跳的结论说成**人话**（给 LLM 与日志共用，一处措辞）。"""
    from src.infrastructure.catalog.network_fallback import UNMEASURED

    if not fallback.triggered:
        return f"未触发（{fallback.trigger}）"
    if fallback.allowed:
        return (f"已放行、但没取到 —— {fallback.reason}"
                f"（结论：{UNMEASURED}，不是 0）")
    return (f"未放行（护栏拒绝，码 {fallback.code}）—— {fallback.reason}"
            f"（结论：{UNMEASURED}，不是 0）")


async def query_data_for_agent(indicator: str, limit: int = 10, *,
                               state: ResearchState,
                               agents: dict[str, Any]) -> str:
    """A17 `query_data` 工具的**唯一实现**（四跳；返回值形状仍是 `str`）。

    ① 本次已采集数据点 → ② 本地库（`LocalDataExecutor`）→
    ③ 连接器链（A01 backend）→ ④ **联网兜底**（`data_fallback_hop`）。

    四跳全空时返回的**不是**"没有数据"这一句：
    它必须能分清"本地没有 / 护栏拒绝联网 / 联网了源上也没有"——
    三者的下一步动作完全不同。且一律标注 `UNMEASURED`，
    明确禁止用 0 或估计值替代。

    ## ★ 跳级命中埋点（可观测性缺口：四跳命中率原先给不出来）

    本函数是四跳链**唯一**决定"由哪一跳答出来"的地方，因此也是**唯一**的打点处：
    每个 `return` 之前一行 `bump(...)`（`src/core/hop_stats.py`），
    四跳全空则记 `HOP_NONE`（缺口，不算任何一跳命中）。

    为什么打点必须在这里、而不是抽一个 `_record_hop()` 之类的函数给测试调：
    本项目实测过「判据接在没人走的路上」这种缺陷（`describe()` 零个生产调用方，
    而且不报错）。计数挂在**真实路径**上，测试才能构造一条**绕过**打点的路径
    （直接调 `_query_data_via_connectors`）自证"计数不跟着它动"。

    为什么埋点不放进 `data_fallback_hop()`：那个函数**不只是**本链路的第四跳
    （`tests/unit/test_fallback_wiring.py` 有多条用例直接调它），
    在里面打点会把"测试直接调一次"也算成"链路命中一次" ——
    指标就成了"看谁调过"，而不是"哪一跳答出来的"。

    ⚠️ 2026-09-28 第二十五轮：本函数从 `recommend_node` 内的闭包提出，
    只加了 `state`/`agents` 两个显式参数，前三跳行为逐字未改
    （`recommend_node` 里的 `_query_data` 现在是一行委托）。
    """
    from src.infrastructure.catalog.network_fallback import UNMEASURED

    key = str(indicator)
    points = [p for p in state.get("validated_points", [])
              if str(p.get("indicator", "")) == key]
    if points:
        points = sorted(
            points, key=lambda p: str(p.get("period_date", "")),
            reverse=True)[:limit]
        # ★ 跳级命中埋点：**已决定由第一跳返回**，就在这一行记（不是抽个函数给测试调）。
        bump(HOP1_VALIDATED)
        return "\n".join(
            f"- {p.get('period_date')}: {p.get('value')} "
            f"(来源{p.get('source_name')})"
            for p in points
        )

    # 第二跳：本地库（意图直达列）
    res = None
    local_error: BaseException | None = None
    try:
        from src.infrastructure.catalog.local_data import (
            LocalDataExecutor,
        )

        metric, _, entity = key.partition(":")
        ex = LocalDataExecutor()
        res = await asyncio.to_thread(
            ex.metric_series, metric or key,
            entity=entity or str(state.get("focus_stock_code") or ""),
            limit=limit,
        )
    except Exception as exc:  # noqa: BLE001 本地兜底失败不影响主链
        # ⚠️ 原名只 `logger.debug`（默认不可见）—— 于是"本地这一跳没量到"
        # 与"本地有数据但新鲜"在结果里长得一模一样。改成 warning 并**记下异常**，
        # 由第四跳按 CONN_FAIL 处置（见 `_fallback_trigger`）。
        local_error = exc
        logger.warning("query_data 本地兜底异常(%s)：%s", key, exc)
    if res is not None and res.ok:
        # ★ 跨通道一致性（`CHG-0157`，口径见 PRD §三十三）：本地命中**不等于**可以用它 ——
        #   如果它已经陈旧到违反**声明的新鲜度**，要么用在线刷新，要么如实标注。
        #   ⚠️ 打点只记**最终答出来的那一跳**（`total` 是"四跳路径走过的次数"，
        #   一次查询只能 +1）；"本地其实陈旧"这件事由采集异常承载，不进命中率。
        verdict = await _cross_channel_decide(key, res, limit=limit,
                                              state=state, agents=agents)
        if verdict.used_online and verdict.text:
            bump(HOP3_CONNECTOR)
            return verdict.text
        bump(HOP2_LOCAL)
        return (f"（本地库 {res.source}，共 {res.row_count} 条{verdict.note}）\n"
                + res.summary(limit))

    # 第三跳：连接器兜底
    fetched = await _query_data_via_connectors(key, limit, state=state,
                                               agents=agents)
    if fetched:
        # ★ 跳级命中埋点：第三跳真的给出了数据（前两跳都没给）。
        bump(HOP3_CONNECTOR)
        return fetched

    # 第四跳：**联网兜底**（call site；护栏说了算）
    fallback = await data_fallback_hop(key, res, limit=limit, state=state,
                                       agents=agents, local_error=local_error)
    if fallback.measured:
        # ★ 跳级命中埋点：第四跳真的给出了数据（前三跳全空才走到这里）。
        bump(HOP4_NETWORK)
    else:
        # ★ 跳级命中埋点：四跳**一个都没给出数据** —— 这是"缺口"，不是任何一跳的命中。
        #   判据用结构化的 `measured`，不读给 LLM 看的散文（把判据塞进文案，
        #   验收就会依赖措辞）。下面这段文本照旧往下走。
        bump(HOP_NONE)

    diag = res.diag if res is not None else None
    parts = [f"未找到指标 {indicator} 的数据 —— **{UNMEASURED}**"
             "（不是 0，也不是空值。）"]
    if diag is not None:
        parts.append(f"[本地诊断] {diag.render()}")
    elif local_error is not None:
        parts.append(f"[本地诊断] 本地执行器异常：{type(local_error).__name__}: "
                     f"{local_error}")
    parts.append(f"[联网兜底] {_fallback_line(fallback)}")
    if fallback.text:
        parts.append(f"[联网兜底补充] {fallback.text}")
    parts.append(
        "下一步：如实说明该口径" + UNMEASURED + "及上述原因；"
        "**不要**用 0、空值或估计值替代，也不要把『护栏拒绝联网』写成"
        "『数据源没有该数据』——两者的下一步动作不同。")
    return "\n".join(parts)


def build_research_graph(agents: dict[str, Any], *, chain_path: str, llm_audit_path: str,
                         news_fetcher: Any = None, planner: LLMSupervisorPlanner | None = None,
                         repo: Any = None, skill_library: SkillLibrary | None = None):
    """构建并编译投研StateGraph。

    ★ 2026-09-27 第八轮：拆 3 个 subgraph（数据层 / 信息层 / 决策层）
    之前的实现是单一 ~600 行闭包，所有 helper 函数混在一起；
    现在拆为：
      · `_build_data_subgraph`（collect → clean → validate → store）
      · `_build_info_subgraph`（verify_info → extract_events → sentiment）
      · `_build_decision_subgraph`（liquidity_ctx → fan-out analyze → recommend → audit）
    顶层 `build_research_graph` 只负责"装配 + 顺序串接"（约 30 行）。

    收益：
      · 单元可测试（每个 subgraph 可独立 mock agents 跑通）
      · 错误隔离（一个 subgraph 内的 Exception 不会拖垮另两个）
      · 渐进演进（要改"信息层"流程只看对应 subgraph，不被全图干扰）

    agents: agent_id → Agent实例（必须含A01-A04、A17、A18；
    A08-A11缺失时对应分支自动跳过）。
    news_fetcher: 可选个股新闻抓取器（fetch_news(code,limit)），
    个股任务采集阶段自动拉取新闻注入info_items，打通A05-A07信息层。
    planner: 可选LLM驱动的Supervisor规划器；传入时优先用LLM动态规划，
    失败回退到规则式plan_run()。
    repo: 可选DataPointRepository；实时采集失败/为空时，从入库快照降级读取
    （定时作业已落库的最近值），extra标storage_fallback，保证研判连续性。
    skill_library: 可选技能库（PTD三级加载）；缺省时按仓库skills/目录构造，
    目录不存在时技能功能静默关闭，不影响主链路。
    """
    if skill_library is None:
        skill_library = SkillLibrary("skills")

    # ========== 数据缺口自修复 ==========
    #
    # ⚠️ 2026-10-07（`CHG-0192` ①）：这里**删掉了** `_STORAGE_FALLBACK_LIMITS` 与
    #    `_storage_fallback()` —— 它们唯一的调用点在 `_collect_one`，而那个函数
    #    **全仓零引用**（AST 实证）。活路径的空结果由 `SmartFetcher` 的 DB-only
    #    分支承担（见 `src/infrastructure/catalog/smart_fetch.py` 模块 docstring
    #    第 2/5 条），留在这里只会让下一个人以为"存储降级有两条实现"。
    #    纪律出处：`CHG-0185` 的「否决一个方案不等于删掉它；半截尸体比从未实现更危险」。

    _gap_resolver: Any | None = None  # 延迟初始化（需要 gateway）

    def _get_gap_resolver() -> Any | None:
        """延迟创建 DataGapResolverAgent（需要 gateway 从 agents 获取）。"""
        nonlocal _gap_resolver
        if _gap_resolver is not None:
            return _gap_resolver
        a17 = agents.get("A17_recommend")
        if a17 is None:
            return None
        gw = getattr(a17, "_gateway", None)  # noqa: SLF001
        if gw is None:
            return None
        from src.domain.agents.data_gap_resolver import DataGapResolverAgent
        _gap_resolver = DataGapResolverAgent(gw)
        return _gap_resolver

    #: 豁免类的 `gap_kind` 取值 → 人话（**唯一**的文案映射，别处不许再写一份）。
    EXEMPT_GAP_WORDING: dict[str, str] = {
        "not_applicable": "该口径对本主体不适用（非缺陷）",
        "not_covered": "该专题未收录本主体（非缺陷）",
    }

    def _record_exempt_gap(ind: str, gap_kind: str, exc: BaseException,
                           state: ResearchState) -> None:
        """把"这条缺口属于豁免类"记进**缺口台账**（`CHG-0155`）。

        为什么要有这一跳：豁免结论原先只活在**这一处的 progress 文案**里，
        而下游（`collect_node` 的缺口文案、审计的完整性三态）各自再判断一次
        —— 于是同一件事在界面上出现两句互相矛盾的话。实测现场：豁免类同时显示
        「ℹ️ …不适用（非缺陷，已跳过）」与「⚠️ …缺口（实时失败 + DB 无快照）」，
        用户读到后者会以为系统坏了。

        取值优先用取数侧挂在异常上的结构化字段（拿不到才用 `miss_stage` 映射），
        **绝不抛**：观测失败不该影响采集（与 `audit.record` 同一条纪律）。

        ## ★ `state` 是**显式参数**，不许靠闭包（`CHG-0185`）

        第一版把 `state` 当闭包变量用 —— 而**定义处的外层作用域里没有它**
        （`build_research_graph` 的 128 个赋值名里没有 `state`），
        **有它的是调用点所在的 `_live_fetch_one`**（那是另一个函数）。
        ⇒ 每次调用都 `NameError`，被下面的 `except Exception` 吞掉 ⇒
        **台账一条都没写过**（真实探针实测：无 `state` 写入 **0** 条、
        有 `state` 写入 **1** 条），而 PRD 里还写着"`_live_fetch_one` 已写入"。

        这与 `CHG-0109`（`_fallback_fetch(state=state)` 的 `TypeError` 被
        `logger.debug` 吞掉）是**同一形状、同一个文件、第二次** ⇒ 所以这里
        同时做两件事：**参数显式**（`ruff F821` 与
        `tests/unit/test_no_undefined_names.py` 盯着）+ **日志提到 `warning`**
        （`debug` 默认不可见，等于没有；本项目记过"INFO 曾被整体丢弃"）。
        """
        try:
            from src.domain.agents.data.collector.gap_ledger import (
                record as _record_gap,
            )

            _record_gap(
                task_id=str(state.get("task_id") or ""),
                indicator=str(ind),
                gap_kind=str(getattr(exc, "gap_kind", "") or gap_kind),
                reason=exc,
                path_stats=dict(getattr(exc, "path_stats", {}) or {}),
                source="supervisor._live_fetch_one",
            )
        except Exception:  # noqa: BLE001 观测失败绝不影响采集
            # ⚠️ `warning` 而不是 `debug`：这条日志是"台账没写进去"的**唯一**迹象，
            #    用 `debug` 就等于把静默失效制度化（`CHG-0109` 的原现场）。
            logger.warning("豁免缺口台账记录失败（观测缺失，不影响采集）",
                           exc_info=True)

    async def _live_fetch_one(
        collector: Any, state: ResearchState, ind: str,
        updates: dict[str, Any], live_calls: set[str],
        ct: Any,
    ) -> list[Any]:
        """**交互采集的唯一实现**：防撞钟 + 联网兜底 + 审计累积。

        ## 为什么必须抽成一处（本轮实测的教训）

        第一版把防撞钟与联网兜底接在**旧路径** `_collect_one` 上，而**真正跑的是
        SmartFetcher 快批**那条路（`collect_node` → `SmartFetcher.fetch_many`
        → `live_fetch` 适配器）⇒ 实测端到端仍然 284.6s、质押整表照样 258s，
        防撞钟**一次都没触发**。判决式：**判据接在没人走的路上 = 没接**。
        （`_collect_one` 已于 2026-10-07 删除：AST 实证零引用的死代码。）

        返回 `DataPoint` 列表（已反序列化，供 SmartFetcher 写库用）。
        """
        from src.core.schemas import DataPoint

        deadline = query_deadline_sec()
        points: list[Any] = []
        miss_reason: object = ""
        miss_stage = "local"
        try:
            update, out = await asyncio.wait_for(
                _run_agent(collector, state,
                           {"indicator": ind, "deadline_sec": deadline}),
                timeout=deadline + 2.0,
            )
            updates["agent_outputs"] += update["agent_outputs"]
            updates["data_refs"] += update["data_refs"]
            updates["trace_ids"] += update["trace_ids"]
            live_calls.add(ind)
            points = [DataPoint(**p)
                      for p in (out.result.get("data_points", []) or [])]
        except (asyncio.TimeoutError, TimeoutError):
            from src.core.intel_limits import deadline_reason

            # ★ 超时**不是故障**（AGENTS.md：不许记失败冷却），文案也照此写。
            miss_reason = deadline_reason(ind, deadline + 2.0)
            miss_stage = "timeout"
            if ct is not None:
                ct.push_progress(f"⏱️ {ind} 触发防撞钟（>{deadline:.0f}s），已终止")
        except Exception as exc:  # noqa: BLE001 ★★ 用户口径：**一律先当"本地没查到"**
            # 用户原话（2026-09-29）：
            #   「对于数据内容超长或报错的，应该**默认为数据未在本地查询到**，
            #     自动转联网搜索获取，而不是**报异常故障**」
            # 所以取数异常在这里**不再向上抛**，也不把异常原文塞进用户可见面板：
            # 只留一句**有界**缺口说明（`collection_gap_note` 用 `brief()` 截断），
            # 随后统一走下面的联网兜底。
            # ⚠️ 为什么仍然写进 `errors`（而不是只写 progress）：既有契约
            #   `test_collector_error_is_contained` 断言"采集失败必须可见" ——
            #   **可见性不丢，只是不再吓人**（一屏注册表那次就是丢在这里）。
            miss_reason = f"{type(exc).__name__}: {exc}"
            text = str(exc)
            # ★★ 2026-10-01（`CHG-0155`）：**优先用结构化载体**，文本标记只作回退。
            #   取数侧在抛出时把结论挂在异常上（`exc.gap_kind`，取值就是
            #   `NoApplicableData.KINDS` 里那两个）；文本匹配是上一版的做法，
            #   它把"结论"降级成"猜语义"（`catalog` 那侧有一模一样的教训）。
            #   两条都留：老路径/别的连接器可能只给散文。
            structured_kind = str(getattr(exc, "gap_kind", "") or "")
            if structured_kind == "not_covered" or (
                    not structured_kind
                    and any(marker in text for marker in NOT_COVERED_MARKERS)):
                # ★ 2026-09-30（`CHG-0135`）：口径存在、专题表未收录该主体。
                #   三种处置与"不适用"完全一致（都不是故障），只是文案不同：
                #   ① **不进 errors**；② **不联网硬试**（表里本来就没有它）；
                #   ③ 记一行 progress，让人看得见"为什么这条没有数据"。
                miss_stage = "not_covered"
                if ct is not None:
                    ct.push_progress(
                        f"ℹ️ {ind} 该专题未收录本主体（≠ 取值为 0，非缺陷，已跳过）")
            elif structured_kind == "not_applicable" or (
                    not structured_kind
                    and any(marker in text for marker in NOT_APPLICABLE_MARKERS)):
                # ★ 取数侧**自己下的结论**："该口径对这个主体不适用"（银行没有流动比率）。
                #   三种处置（照 AGENTS.md 的 `NOT_APPLICABLE_FOR_ENTITY` 纪律）：
                #   ① **不进 errors**（它不是故障，是口径不适用）；
                #   ② **不触发联网兜底**（联网也拿不到，硬试只是烧钱）；
                #   ③ 记一行 progress，让人看得见"为什么这条没有数据"。
                miss_stage = "not_applicable"
                if ct is not None:
                    ct.push_progress(
                        f"ℹ️ {ind} 该口径对本主体不适用（非缺陷，已跳过）")
            else:
                miss_stage = "local"
            if miss_stage in ("not_applicable", "not_covered"):
                # ★ 让这个结论**随指标一起走**：下游（`collect_node` 的缺口文案、
                #   审计的三态豁免）都要用它，否则同一件事会在界面上出现两句
                #   互相矛盾的话 —— 实测现场：豁免类会同时显示
                #   「ℹ️ …不适用（非缺陷，已跳过）」与
                #   「⚠️ …缺口（实时失败 + DB 无快照）」。用户读到后者会以为系统坏了。
                _record_exempt_gap(ind, miss_stage, exc, state=state)
            logger.warning("%s", format_gap_note_for_log(ind, exc))

        if miss_reason and not points and miss_stage not in (
                "not_applicable", "not_covered"):
            updates["errors"].append(
                collection_gap_note(ind, miss_reason, stage=miss_stage))
            # ★★ 2026-09-30（用户口径）：这类"节点异常/防撞钟"信息
            #   **不再显示给用户**，但要**记录到管理员侧**供维护。
            #   记录点选在这里是刻意的：它是**超时/异常两条路唯一汇合处**
            #   （`miss_stage` 区分 timeout/local），一处覆盖两种，不用在
            #   两个 except 里各写一份（同一件事两份实现必然漂移）。
            _record_collection_anomaly(
                ind, miss_stage, miss_reason,
                task_id=str(state.get("task_id") or ""))
        elif miss_reason and not points:
            # ★ `CHG-0135`：`not_covered` 与 `not_applicable` 都走这条
            #   （都是"非缺陷"，只是文案由 `collection_gap_note` 按 stage 分）。
            updates["progress"] = (
                updates.get("progress", []) + [
                    collection_gap_note(ind, miss_reason,
                                        stage=miss_stage)])

        if not points:
            # 「该口径不适用」**不联网**：联网也拿不到（银行没有流动比率），硬试只是烧钱。
            # 这是 AGENTS.md 已有纪律（`NOT_APPLICABLE_FOR_ENTITY` 不触发联网兜底）
            # 在采集路径上的落点。
            if miss_stage in ("not_applicable", "not_covered"):
                return points
            # 找不到就去联网找（用户口径 2026-09-29）——带同样的防撞钟
            online = await _network_lookup_for_collection(ind, state, agents)
            if online:
                points = [p if isinstance(p, DataPoint) else DataPoint(**p)
                          for p in online]
                live_calls.add(ind)
                updates["progress"] = (
                    updates.get("progress", []) + [
                        f"🌐 {ind} 本地无 → 联网兜底取到 {len(points)} 条"
                        f"（源 {getattr(points[0], 'source_name', '?')}）"])
                # 取到了 ⇒ **把那条缺口说明撤掉**（否则面板上留着一条已解决的"异常"）
                updates["errors"] = [e for e in updates["errors"]
                                     if f"({ind})" not in e]
                logger.info("指标%s本地未命中，联网兜底取到 %d 条", ind, len(points))
            elif miss_reason:
                # ★ 两边都没有 ⇒ 结论是**缺口**，不是故障：把"已转联网"改成
                #   "本地与联网均未取到（已登记缺口，不阻断本轮）"。
                #   用户口径：「默认为数据未在本地查询到」——**不许报异常故障**。
                updates["errors"] = [e for e in updates["errors"]
                                     if f"({ind})" not in e]
                updates["errors"].append(
                    collection_gap_note(ind, miss_reason, stage="online"))
                updates["progress"] = (
                    updates.get("progress", []) + [
                        f"⚠️ {ind} 本地与联网均未取到（已登记缺口，不阻断本轮）"])
                # ⚠️ 自修复**不挂在这里**（2026-10-07 实测纠正，`CHG-0197`）：
                #   这一支只在"走了 live_fetcher"时才执行，而连接器路径下
                #   `live_fetcher` 根本不会被调用 ⇒ 带计数器实测**被调用 0 次**，
                #   缺口照样产生。真正的汇合点是 `collect_node` 里
                #   `got_empty` + `result.missing` 那两个循环（见那里的说明）。
                #   教训与 `_live_fetch_one` docstring 里那句**逐字相同**：
                #   **判据接在没人走的路上 = 没接。**
        return points

    async def _network_lookup_for_collection(
        indicator: str, state: ResearchState, agents: dict[str, Any],
    ) -> list[Any]:
        """**采集路径的联网兜底**（用户 2026-09-29：「找不到就去联网搜索找」）。

        ## 为什么要有它（口径变更 + 实测缺口）

        原来采集路径是"本地/连接器拿不到 → 存储降级 → 后台自修复 → 记缺口"，
        **一次都不联网** —— 第四跳（`data_fallback_hop`）只挂在 A17 的
        `query_data` 工具上。于是"行业↔板块在本地匹配不上"这类**本地确实没有**
        的情况，只能静默跳过（当时的用户口径是"不提示"）。
        现在口径改成「**找不到就去联网搜索找**」，所以这一跳必须补在采集链上，
        而且是**所有指标**都走（用户说的是"投研分析的任何查询数据"）。

        ## 三道闸门一个都不新写

        白名单（默认空 = 全禁）· 预算（日/时）· 冷却与熔断 —— 全部由
        `NetworkFallback` 自带（经 `_fallback_fetch`），本函数只做三件事：
        取一次、把"没量到"的语义带回来、**带防撞钟**（超时就当没找到）。

        失败**绝不抛**：联网兜底是最后一层，它坏了不能拖垮采集。
        """
        try:
            outcome = await asyncio.wait_for(
                _fallback_fetch(
                    # ★★ 2026-09-30 修：这里原写成 `_fallback_fetch(indicator,
                    #   state=state, agents=..., ...)`，而 `_fallback_fetch` 的签名
                    #   里**没有 `state`**（它只需要 agents/context/trigger）
                    #   ⇒ 每次调用抛 `TypeError: unexpected keyword argument 'state'`
                    #   ⇒ 被下面的 `except Exception` 吞掉 ⇒ **采集路径的联网兜底
                    #   一次都没真正发生过**（用户口径「找不到就去联网找」在采集侧
                    #   等于没接）。与 AGENTS.md 记过的"NetworkFallback 零生产调用方"
                    #   是同一形状的复发：**判据接在没人走的路上 = 没接**。
                    #   护栏：`tests/unit/test_collection_network_fallback_wiring.py`
                    #   （① AST 逐调用点核对关键字与签名一致；② 行为判据：采集路径
                    #   必须真的调到它并把 outcome 变成 points）。
                    indicator, agents=agents,
                    context=f"collect:{state.get('task_id', '')}",
                    trigger="LOCAL_MISS"),
                timeout=query_deadline_sec(),
            )
        except (asyncio.TimeoutError, TimeoutError):
            from src.core.intel_limits import deadline_reason

            logger.warning("%s", deadline_reason(f"联网兜底 {indicator}",
                                                 query_deadline_sec()))
            return []
        except Exception as exc:  # noqa: BLE001 联网兜底坏了不拖垮采集
            # ★ 2026-09-30：`debug` → `warning`。为什么：兜底是**最后一层**，
            #   它坏掉时用户看到的只是"没数据"，而 `debug` 默认不可见 ⇒
            #   **上面那个 TypeError 藏了很久没人发现**。可见性是这条链路的护栏，
            #   不是日志洁癖。
            logger.warning("采集路径联网兜底异常(%s): %s: %s",
                           indicator, type(exc).__name__, exc)
            return []
        if outcome is None:
            return []
        points = [p for p in (outcome.points or [])
                  if getattr(p, "value", None) is not None]
        if not outcome.allowed:
            logger.info("采集路径联网兜底被护栏拒绝(%s)：%s", indicator, outcome.reason)
            return []
        return points

    async def _try_self_heal(
        indicator: str, error_ctx: str,
    ) -> list[dict[str, Any]]:
        """数据缺口自修复：LLM 生成连接器 → 沙箱验证 → 热加载 → 重试 fetch。

        失败不阻断主链路——返回空列表，指标缺口照样上报给 A17。

        ★★★ 2026-09-27 第八轮：白名单 + 频率上限（−90% 误触发）
          审计实证：`ind:sw_third_*` 因东财 WAF 稳定返回空 → 每个 trace 稳定触发 A19；
          50 次调用 → 162,608 tokens（输出）= 全系统输出的 23%。
          改造：
            · 进程内 `尝试过且未成功` 黑名单（命中直接跳过 + 警告一次）
            · 全局每分钟 5 次上限（突发阻尼，避免 N 个并发 trace 各自触发）
        """
        resolver = _get_gap_resolver()
        if resolver is None:
            return []

        # 白名单/黑名单判断：已知数据源故障（WAF/限频）的指标直接放弃自修复
        if not _self_heal_allowed(indicator, error_ctx):
            return []

        # fetch 重试函数（让 resolver 验证新连接器能真的拿到数据）
        collector = agents.get("A01_data_collector")
        fetch_fn = None
        if collector is not None:
            async def _retry_fetch(ind: str) -> list:
                from src.core.models import AgentInput
                out = await collector.execute(AgentInput(
                    task_id=f"heal_{int(time.time())}",
                    tenant_id="tenant_001",
                    payload={"indicator": ind},
                ))
                return list(out.result.get("data_points", []))
            fetch_fn = _retry_fetch

        try:
            result = await resolver.resolve(indicator, error_ctx, fetch_fn)
        except Exception as exc:  # noqa: BLE001
            logger.warning("自修复异常(%s): %s", indicator, exc)
            return []

        if not result.success:
            # ★ 失败指标入黑名单缓存（1h 内不再尝试）
            _record_heal_failure(indicator)
            logger.info("自修复未成功(%s): %s", indicator, result.error or "?")
            return []

        # 自修复成功 → 清失败缓存 + 注册定时调度（沉淀为持久采集任务）
        _record_heal_success(indicator)
        try:
            from src.scheduler.registry import register_dynamic_job
            sched_cfg = type(resolver).get_dynamic_schedule_config(
                indicator, result)
            job_name = f"dynamic_{hashlib.md5(indicator.encode()).hexdigest()[:8]}"
            register_dynamic_job(
                name=job_name,
                cron=sched_cfg["cron"],
                indicator=indicator,
                connector_path=result.connector_path,
            )
            logger.info("自修复成功→已注册定时采集: %s (%s)", job_name, sched_cfg["cron"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("动态调度注册失败(%s): %s", indicator, exc)

        # 自修复成功——重试 fetch 拿数据
        if fetch_fn is not None:
            try:
                points = await fetch_fn(indicator)
                if points:
                    logger.info(
                        "自修复成功(%s)：新连接器 %s 获取 %d 条数据",
                        indicator, result.connector_class, len(points))
                    return [p.model_dump() if hasattr(p, "model_dump")
                            else p for p in points]
            except Exception as exc:  # noqa: BLE001
                logger.warning("自修复后重试fetch失败(%s): %s", indicator, exc)

        return []

    # ------------------------------------------------------------------
    # ★★ 2026-10-07（`CHG-0190` ② / `CHG-0192` ①）：把自修复**接回活路径**
    #
    # 修的是什么：`_try_self_heal`（上面那个函数）原先只有一个调用点，而那个
    # 调用点位于 `_collect_one` —— AST 实证**全仓零引用**的死代码。也就是说
    # 整条"缺数据 → LLM 生成连接器 → 沙箱 → 重试"在图内**从未发生过一次**，
    # 而注释却声称"结果写到 `self_heal_pending` 标记，由盘后批量作业回填"。
    # 判决式沿用本文件 `_live_fetch_one` 的那句教训：**判据接在没人走的路上 = 没接**。
    #
    # 现在的形状（三条纪律，缺一条就会退化成新的假绿）：
    #   ① **只读判据**：先做一次 `consume=False` 的预检（不烧配额），
    #      真正的尝试由 `_try_self_heal` 内部那次消耗 —— 判断仍只有一份实现；
    #   ② **绝不 await**：`create_task` 后台跑，否则会拖长 `live_fetch` 持有的
    #      `live_lock`（那是所有指标采集的串行点）；
    #   ③ **结果落持久载体**：成功 → 连接器已注册定时采集（下一轮命中）；
    #      失败 → 进 `gap_queue`，交盘后 `gap_drain`（**唯一**能读不到 state 的消费者）。
    # ------------------------------------------------------------------

    def _schedule_self_heal(ind: str, state: ResearchState, reason: str,
                            ct: Any) -> None:
        """把一次自修复丢到后台，并保留强引用（见 `_HEAL_TASKS` 的说明）。"""
        try:
            task = asyncio.create_task(_heal_bg(ind, state, reason, ct))
        except RuntimeError:      # 没有运行中的事件循环（同步单测/脚本）
            logger.debug("自修复调度跳过（无事件循环）：%s", ind)
            return
        _HEAL_TASKS.add(task)
        task.add_done_callback(_HEAL_TASKS.discard)

    async def _heal_bg(ind: str, state: ResearchState, reason: str,
                       ct: Any) -> None:
        """后台自修复尝试；**失败不静默** —— 入缺口队列交给盘后。

        ⚠️ 这里**不**再调 `_self_heal_allowed`（那会二次消耗配额）：
        闸门由 `_try_self_heal` 内部那一次负责；调度前的预检走
        `consume=False`（见 `_schedule_self_heal` 的调用点）。
        """
        try:
            points = await _try_self_heal(ind, reason)
        except Exception as exc:  # noqa: BLE001 后台任务不许把异常抛给事件循环
            logger.warning("自修复异常(%s): %s", ind, exc)
            points = []
        if points:
            logger.info("自修复成功(%s)：%d 条，连接器已注册定时采集", ind, len(points))
            if ct is not None:
                ct.push_progress(
                    f"🔧 {ind} 自修复成功（{len(points)} 条），连接器已注册定时采集，"
                    "下一轮可直接命中")
            return
        # 自修复也没成 ⇒ **交给盘后**（与 A17 报缺口共用同一个队列，
        # 不允许出现第二套"缺口"实现）
        try:
            if _enqueue_self_heal_gap(ind, reason=reason,
                                      task_id=str(state.get("task_id") or "")):
                logger.info("自修复未成功(%s) ⇒ 已入缺口队列（盘后 gap_drain 补取）", ind)
                if ct is not None:
                    ct.push_progress(f"📋 {ind} 自修复未成功，已入缺口队列（盘后自动补取）")
        except Exception as exc:  # noqa: BLE001 缺口登记失败不阻断交付
            logger.warning("缺口入队失败(%s): %s", ind, exc)

    async def _run_agent(
        agent: Any, state: ResearchState, payload: dict[str, Any]
    ) -> dict[str, Any]:
        output = await agent.execute(AgentInput(
            task_id=state["task_id"], tenant_id=state["tenant_id"], payload=payload,
        ))
        # agent_id取自output（Fake替身可能未挂agent_id属性）
        aid = getattr(agent, "agent_id", None) or output.agent_id
        update: dict[str, Any] = {
            "agent_outputs": [_summary(output)],
            "data_refs": list(output.data_refs),
            "trace_ids": [output.trace_id],
            "progress": [f"{aid} 已完成"],
        }
        return update, output

    def _node(agent_id: str, payload_fn, *, collect: bool = False):
        agent = agents.get(agent_id)

        async def node(state: ResearchState) -> dict[str, Any]:
            if agent is None:
                return {}  # 未注册的Agent直接跳过（可选分支）
            # 取消检查：用户点停止时立即终止
            token = state.get("cancellation_token")
            if token is not None:
                token.check()
            # 写入实时进度（通过return值让LangGraph更新state）
            if agent_id in ALL_INSIGHT_AGENTS and agent_id not in state["plan"]:
                return {"progress": [f"跳过 {agent_id}（不在本次规划中）"]}
            if agent_id in INFO_AGENTS and (
                agent_id not in state["plan"] or not state.get("info_items")
            ):
                return {}  # 信息层：计划未包含或无输入文本时跳过
            if agent_id in DATA_PIPELINE_AGENTS and not state.get("raw_points"):
                return {}  # 无采集数据时数据管线空转无意义（如news管线）
            # 实时进度：节点内通过cancel_token共享通道追加，前端立即可见
            if token is not None:
                token.push_progress(f"正在执行 {agent_id}…")
            try:
                (update, output) = await _run_agent(agent, state, payload_fn(state))
            except AgentExecutionError as exc:
                return {"errors": [f"{agent_id}: {exc}"]}
            except Exception as exc:  # noqa: BLE001 单节点失败不拖垮整图
                return {"errors": [f"{agent_id}: 意外异常 {exc}"]}

            if agent_id == "A02_data_cleaner":
                update["cleaned_points"] = list(output.result.get("data_points", []))
            elif agent_id == "A03_data_validator":
                update["validated_points"] = list(output.result.get("data_points", []))
                update["validation_report"] = output.result
            elif agent_id == "A04_data_storage":
                update["storage_stats"] = output.result.get("storage_stats")
                # ★ 第十一轮：A04 落库完成后**幂等重算** indicator_catalog 索引。
                # 为什么放在这里而不是 collect 阶段：数据要经 A02/A03 校验，
                # A04 才是唯一写入事实表的节点。索引必须反映"事实表里到底有什么"。
                # 失败不阻断主链路（索引是加速结构，坏了只影响下次命中率）。
                if repo is not None:
                    try:
                        from src.infrastructure.catalog.catalog_repo import (
                            CatalogRepository,
                        )
                        inds = state.get("_catalog_indicators") or []
                        if inds:
                            cat = CatalogRepository(db_path=repo._db_path)
                            await cat.refresh_stats_from_facts(inds)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("catalog 索引回填失败（忽略）: %s", exc)
            elif agent_id == "A05_verifier":
                update["verified_items"] = output.result
            elif agent_id == "A06_extractor":
                update["extracted_events"] = output.result
            return update

        node.__name__ = f"node_{agent_id}"
        return node

    async def supervisor_node(state: ResearchState) -> dict[str, Any]:
        """Supervisor规划：优先LLM动态规划，失败回退规则路由。"""
        llm_plan = None
        if planner is not None:
            try:
                llm_plan = await planner.plan(
                    state["user_query"], state.get("target", ""), agents,
                    # ★ 第二十三轮：规划那次调用要归到**本任务**的 trace 上。
                    #   原先 planner 内部硬编码 trace_id="planner"，
                    #   导致 `GET /api/v1/trace/{task_id}` 按 trace_id 过滤时
                    #   永远筛不到它 —— 面板上看不到"规划用了哪个模型"，
                    #   看起来像"只有 deepseek 被调用过"。
                    trace_id=state.get("task_id", "") or "planner",
                )
            except Exception:  # noqa: BLE001 LLM规划异常不阻断
                llm_plan = None
        if llm_plan:
            # 契约修正：LLM 常返回**裸个股指标**（"PE(TTM)"）且 target 为空，
            # 而个股指标必须带 6 位代码后缀，否则没有任何连接器 supports 它 ——
            # A01 会瞬间 DataFetchError，用户看到"估值数据缺失"。
            # 详见 sanitize_indicators 的说明（2026-09-26 实测故障）。
            requested = list(llm_plan["indicators"])
            # ★ 2026-09-29（§19.17）：行业类裸名（`行业拥挤度`）要补**行业名**。
            #   解析走唯一实现 `resolve_focus_industry`（与路由/归属判定同源）。
            _focus_text = (f"{llm_plan.get('target') or ''} {state['user_query']}")
            _focus_industry, _ = resolve_focus_industry(
                f"{_focus_text} {state.get('focus_stock_code') or ''}".strip())
            indicators, dropped, fix_notes = sanitize_indicators(
                requested,
                # ★ 第二十三轮：复合问里解析出的个股代码也要参与补后缀 ——
                #   否则"宏观问 + 顺带一只个股"时 target 为空，
                #   `PE(TTM)` 会被判成"无标的"丢弃、换成行业截面，
                #   用户问的那只票**始终没有数据**。
                (llm_plan.get("target")
                 or str(state.get("focus_stock_code") or "")
                 or state.get("target", "")),
                llm_plan["analysis_type"],
                industry=_focus_industry,
            )
            topic_kw = extract_topic_keywords(
                llm_plan["analysis_type"], llm_plan["target"], state["user_query"],
            )
            # LLM可能漏掉大盘流动性指标，按任务类型/问题关键词确定性补齐
            route_text = f"{llm_plan.get('target') or ''} {state['user_query']}"
            indicators = append_liquidity_indicators(
                llm_plan["analysis_type"], route_text, indicators,
            )
            # ★ 2026-09-29：**中国宏观四件套**也要确定性补齐 ——
            #   实测 LLM 的宏观计划里一条中国宏观都没有（用户读到
            #   「中国端：中国宏观数据缺失」，而 CPI/PPI/M2/社融 都在库里）。
            indicators = ensure_macro_indicators(
                llm_plan["analysis_type"], route_text, indicators)
            # ★ 2026-09-28 第十轮：行业 Agent 按需（plan 后置过滤）
            # LLM planner (qwen2.5:1.5b) 偶发"保守起见全选" → 把 A13-A16 都加上。
            # 纯宏观/个股问题跑 4 路行业 Agent 是浪费（4 次 reasoning-tier 调用）。
            # 规则：analysis_type=macro/industry 且问题未命中任何行业关键词 → 剥离 A13-A16。
            #       analysis_type=industry：仅保留 route_industry 命中的行业 Agent。
            #       analysis_type=stock/full：只保留**管这个标的行业**的那一个
            #         （+ 兜底 A20）—— 见 `prune_industry_agents_by_focus` 的现场说明。
            #       analysis_type=news：保留全部。
            planned_agents = list(llm_plan["agents"])
            text_for_route = route_text
            # ★ 2026-09-29：剥离集合直接取 `INDUSTRY_AGENTS`（**单一事实源**）。
            #   原先这里手写四个 id —— 新增 A20（兜底行业）后如果不改这里，
            #   宏观问法**剥不掉 A20**（它会白跑一次 reasoning 调用），
            #   而"行业问法保留命中的"那条也会把它一起剥掉（它是兜底，
            #   本来就命中不了关键词）。用常量就同时解决两处。
            industry_agents = set(INDUSTRY_AGENTS)
            # ★ 2026-10-08：并集判据（`needs_generic_industries`）——
            #   "第一个行业有主、第二个行业没人管"时也必须挂 A20。
            generic_industries = needs_generic_industries(
                text_for_route, str(state.get("focus_stock_code") or ""))
            if llm_plan["analysis_type"] == "macro":
                # 宏观问法：行业 Agent 几乎用不上（A08 已覆盖宏观面）
                planned_agents = [
                    a for a in planned_agents if a not in industry_agents]
            elif llm_plan["analysis_type"] == "industry":
                # 行业问法：只保留 route_industry 命中的（关键词 → 行业）+
                # 兜底 Agent（当标的确有行业、但没人覆盖它时）
                routed = set(route_industry(text_for_route))
                if generic_industries:
                    routed.add("A20_generic_industry")
                planned_agents = [
                    a for a in planned_agents
                    if a not in industry_agents or a in routed]
            elif llm_plan["analysis_type"] in ("stock", "full"):
                # ★★★ 2026-09-29：个股/综合问法按**标的行业**裁剪（§19.16.5 第 1 条）。
                #   修前这里是"保留全部" ⇒ 一条"高股息招商银行"的问句会让
                #   A14（消费）也跑一次 reasoning 调用，产出「**非本框架覆盖标的**」
                #   —— 用户读到的仍然是"系统缺能力"（真实端到端跑出来的原话）。
                #   判据全部来自既有单一事实源（`route_industry` +
                #   `resolve_focus_industry`），判不出行业时**原样返回**（不猜、不砍）。
                pruned = prune_industry_agents_by_focus(
                    planned_agents, text_for_route,
                    str(state.get("focus_stock_code") or ""))
                if pruned != planned_agents:
                    logger.info(
                        "行业 Agent 按标的行业裁剪：%s → %s",
                        [a for a in planned_agents if a not in pruned],
                        [a for a in pruned if a in industry_agents])
                planned_agents = pruned
            # news：保留全部
            #
            # ★★★ 2026-09-28 第二十三轮：**问句领域信号增补**（复合问修复）
            #
            # 上面刚按 `analysis_type` 做了剥离。但**单值** analysis_type
            # 表达不了复合问 —— 用户一句话问了宏观 + A股 + 消费 + 某只个股时，
            # 规划器只能挑一个（实测挑成 macro），于是另外三个子问题
            # **一个 Agent 都没分到**，最后 A17 拿单份 CPI/PPI 硬写。
            #
            # 这里按问句里**真实出现的领域信号**把对应 Agent/指标补回来。
            # 只增不减，且增补发生在剥离**之后**（否则会被剥掉）。
            _route_text_for_signals = (
                f"{llm_plan.get('target') or state.get('target', '')} "
                f"{state['user_query']}")
            planned_agents, indicators, _sig_notes = (
                _apply_query_signal_augmentation(
                    state, planned_agents, indicators,
                    target_for_focus=(
                        llm_plan.get("target") or state.get("target", "")),
                )
            )
            if _sig_notes:
                logger.info("问句领域信号增补：%s", "；".join(_sig_notes))

            progress = ["Supervisor规划完成，开始数据采集…"]
            stripped = [a for a in llm_plan["agents"]
                        if a not in planned_agents]
            if stripped:
                logger.info("行业 Agent 按需剥离：%s（节省 ~%ds）",
                            stripped, len(stripped) * 10)
                progress = [
                    f"行业 Agent 按需剥离：{', '.join(stripped)}（节省推理调用）"
                ] + progress
            if fix_notes:
                progress = [f"规划修正：{n}" for n in fix_notes] + progress
                logger.info("规划契约修正：%s", "；".join(fix_notes))
            # ★★ `CHG-0240`：**信息层由判据决定，不由模型决定**。
            #
            # 上面 `planned_agents` 的起点是 `llm_plan["agents"]`（模型选的）。
            # 模型没选 A05/A06/A07 时，节点里的闸门第一句就命中，三个 Agent
            # **一次都不执行、连日志都不留** —— 实测两次端到端都是这个结果
            # （提交响应的 plan 里有、审计里零调用），而新闻其实是取到了的。
            # 判据与规则路径**同一个函数**（`ensure_info_agents`），不再各写一份。
            planned_agents = ensure_info_agents(
                planned_agents,
                info_items=state.get("info_items"),
                analysis_type=llm_plan["analysis_type"],
                target=llm_plan.get("target") or state.get("target", ""),
                query=state["user_query"],
                focus_stock_codes=state.get("focus_stock_codes") or (),
                auto_topic=topic_kw,
            )
            return {
                "plan": planned_agents,
                "analysis_type": llm_plan["analysis_type"],
                "target": llm_plan["target"] or state.get("target", ""),
                "_planned_indicators": indicators,
                "topic_keywords": topic_kw,
                "progress": progress,
            }
        # 规则路由fallback
        plan = plan_run(state["analysis_type"], state["target"],
                        state.get("info_items"), query=state["user_query"])
        # ★ 第二十三轮：规则兜底路径**也要**做问句信号增补。
        #   两条路径各写一份必然漂移，所以共用 `_apply_query_signal_augmentation`。
        _fb_agents, _fb_indicators, _fb_notes = _apply_query_signal_augmentation(
            state, list(plan["agents"]), list(plan["indicators"]),
        )
        if _fb_notes:
            logger.info("规则兜底路径的信号增补：%s", "；".join(_fb_notes))
        # 裸指标补后缀：个股类补 6 位代码、行业类补行业名（与 LLM 分支同一理由）。
        # ⚠️ 这里**不再**用 `if _fb_code:` 守卫 —— 行业类裸名（`行业拥挤度`）
        #    在没有个股代码时同样需要补行业名；只在有代码时才跑，
        #    会让"纯行业问句"留下裸名 ⇒ 必然失败的 fetch（§19.17 的裸名陷阱）。
        #    没有代码且没有裸个股指标时，这段是**空操作**（不会误加申万截面）。
        _fb_code = str(state.get("focus_stock_code") or "")
        _fb_industry, _ = resolve_focus_industry(
            f"{state.get('target', '')} {state['user_query']} {_fb_code}".strip())
        _fb_indicators, _, _fb_fix = sanitize_indicators(
            _fb_indicators, _fb_code, plan["analysis_type"],
            industry=_fb_industry)
        _fb_notes += _fb_fix
        return {
            "plan": _fb_agents,
            "analysis_type": plan["analysis_type"],
            "target": plan["target"],
            "_planned_indicators": _fb_indicators,
            "topic_keywords": plan.get("topic_keywords", []),
            "progress": ["Supervisor规划完成，开始数据采集…"],
        }

    def _collect_payload(state: ResearchState) -> dict[str, Any]:
        indicators = state.get("_planned_indicators")
        if indicators is None:
            indicators = plan_run(
                state["analysis_type"], state["target"],
                query=state.get("user_query", ""))["indicators"]
        return {"indicators": indicators}

    # ★★ 2026-10-07（`CHG-0192` ①）：这里**删掉了 `_collect_one`**。
    #
    # 它曾经是"单指标采集（异常隔离 + 存储降级）"的旧路径，但活路径早已换成
    # `collect_node → SmartFetcher.fetch_many → live_fetch → _live_fetch_one`，
    # 于是它成了**全仓零引用**的死代码（AST 判据：定义 1 处、`Load` 引用 0 处）。
    # 它的存在本身造成过真实缺陷：
    #   · `_try_self_heal` 的**唯一**调用点在它里面 ⇒ 图内自修复从未发生过一次；
    #   · 注释声称"结果写到 `self_heal_pending` 标记" —— 而那个键当时**根本不存在**
    #     （全仓 `grep self_heal_pending` 只命中那句注释）。**带着证据的失效声明
    #     比从未实现更危险**，因为它会说服下一个人当它存在（`CHG-0185` 的教训）。
    # 现在：自修复接在**活路径**上（见 `_live_fetch_one` 的"本地与联网均未取到"分支
    # + `_schedule_self_heal`/`_heal_bg`），标记也是真实声明的 channel。
    # 判据：`tests/unit/test_self_heal_wiring.py`（可达性 + reducer + 真入队）。

    # ★ 2026-09-28 第十轮："懒指标"清单
    # 已知慢 / 不可达 / 不阻塞决策的指标 → 移到后台跑，wall-clock 不被拖死。
    # 加入前必须实测过"它确实拖时间但不影响主决策"。
    # 当前入选：CME fed:rate_prob:next（443 不通，本机实测 25s timeout）；
    # 其余按需扩充。
    _LAZY_INDICATORS: frozenset[str] = frozenset({
        "fed:rate_prob:next",       # CME 443 不通，实测 25s timeout
        "mkt:cybkcb:spot_summary",  # 双创板块全量分页，~12s
    })

    # ★ 2026-09-28 第十一轮：SmartFetcher 单指标返回条数上限
    # ⚠️ 2026-10-07：旧 `_STORAGE_FALLBACK_LIMITS` dict（含逐指标精确上限）
    #   已随死代码 `_collect_one`/`_storage_fallback` 一起删除 —— 活路径统一用
    #   本默认值（`SmartFetcher.fetch_many(limit_per_indicator=…)`）。
    _STORAGE_FALLBACK_LIMITS_DEFAULT: int = 60  # 序列类指标保留趋势

    async def _collect_lazy(state: ResearchState, lazy: list[str],
                            ct: Any | None) -> None:
        """懒批后台 fire-and-forget（不阻塞 collect 主路径）。

        ★ 第十轮：原 collect 阶段 await 所有指标 → 单条 25s timeout 拖死整体。
        现在懒批指标以 asyncio.create_task 后台跑，**结果不回写当前任务的
        state**（它对当前任务已没用），但会走 storage_fallback 写库后被
        下次同 query 任务直接命中。
        """
        async def _one(ind: str) -> None:
            try:
                # 复用 _run_agent，但输出丢弃（避免污染当前任务 state）
                await _run_agent(
                    agents["A01_data_collector"], state, {"indicator": ind})
                if ct is not None:
                    ct.push_progress(f"🟡 懒指标 {ind} 后台已完成（不入当前报告）")
            except Exception as exc:  # noqa: BLE001
                logger.debug("懒批 %s 后台失败: %s", ind, exc)

        await asyncio.gather(*(_one(i) for i in lazy))

    async def collect_node(state: ResearchState) -> dict[str, Any]:
        """A01：指标并发采集，**freshness-aware** 智能路由（DB 优先 / 网络 fallback）。

        指标间无依赖，并发跑比串行快 3-10×；结果顺序不影响后续 A02-A04 管线。

        ★ 2026-09-28 第十一轮（数据索引）：核心改造 = SmartFetcher
        - 元数据查表（CatalogRepository.bulk_get）一次性拿全 freshness
        - freshness_hours 内有数据 → DB-only（不联网）
        - stale/missing → 走 A01 live fetch（并行度由 SmartFetcher 控制）
        - 联网回来的数据 → 写库 + 更新 indicator_catalog（下次直接走 DB-only）

        实测收益：
          · 冷启（全 stale）：同现状，max 网络延迟为主
          · 热命中（DB fresh）：~50ms 完成；之前 ~25s（最慢指标 network）
          · 混合场景：每指标按 freshness 独立决策，最坏不慢于冷启

        ★ "懒指标"分流（保持兼容）：
        命中 _LAZY_INDICATORS 的指标走后台 fire-and-forget，不阻塞主路径。
        """
        indicators = _collect_payload(state)["indicators"]
        ct = state.get("cancellation_token")
        lazy = [i for i in indicators if i in _LAZY_INDICATORS]
        fast = [i for i in indicators if i not in _LAZY_INDICATORS]
        if ct is not None:
            ct.push_progress(
                f"数据采集层启动（快批 {len(fast)} + 懒批 {len(lazy)} 个并发，"
                f"SmartFetcher 智能路由）…"
            )

        # ---------- SmartFetcher 路径（快批）----------
        updates: dict[str, Any] = {
            "raw_points": [], "agent_outputs": [],
            "data_refs": [], "trace_ids": [], "errors": [],
            "progress": [
                f"数据采集层执行中（快批 {len(fast)} + 懒批 {len(lazy)} 并发，"
                f"SmartFetcher + A01 → A02 → A03 → A04）…"],
        }

        if fast:
            from src.infrastructure.catalog.catalog_repo import (
                CatalogRepository,
                get_catalog_repository,
            )
            from src.infrastructure.catalog.smart_fetch import SmartFetcher

            # ★ catalog 必须与 data_repo 共享同一 db_path（同一 SQLite 文件）
            # 测试用临时路径时必须显式 new；单例 get_catalog_repository() 用默认路径
            if repo is not None:
                catalog = CatalogRepository(db_path=repo._db_path)
                await catalog.ensure_schema()
            else:
                # 没有 repo 的场景（旧 build_research_graph 默认）：fallback 到全局单例
                # —— 仅供集成测试 / 不带 DB 的 demo 模式
                catalog = get_catalog_repository()
                await catalog.ensure_schema()

            # live_fetcher adapter：A01 collector → list[DataPoint]
            collector = agents["A01_data_collector"]
            live_lock = asyncio.Lock()
            live_calls: set[str] = set()  # 仅记录真联网的指标

            async def live_fetch(ind: str, _start, _end):
                # 串到 A01；调用 _run_agent 走原有审计链（agent_outputs 累积）
                #
                # ★★ 2026-09-29：**这里才是真正的交互采集路径**（SmartFetcher 快批），
                #   所以防撞钟与联网兜底都必须接在**这里** —— 第一版只接在
                #   **旧路径** `_collect_one`（已于 2026-10-07 删除）上，实测端到端仍然跑了 284.6s、
                #   质押整表照样 258s：**判据接在没人走的那条路上**。
                #   现在与 `_collect_one` 共用 `_live_fetch_one`（单一实现）。
                async with live_lock:
                    try:
                        out = await _live_fetch_one(
                            collector, state, ind, updates, live_calls, ct)
                    except AgentExecutionError as exc:
                        updates["errors"].append(
                            f"A01_data_collector({ind}): {exc}")
                        return []
                    except Exception as exc:  # noqa: BLE001
                        updates["errors"].append(
                            f"A01_data_collector({ind}): 意外异常 {exc}")
                        return []
                return out

            smart = SmartFetcher(
                catalog_repo=catalog,
                data_repo=repo,
            )
            result = await smart.fetch_many(
                fast,
                live_fetcher=live_fetch,
                cancel_token=ct,
                limit_per_indicator=_STORAGE_FALLBACK_LIMITS_DEFAULT,
                # ★ 主链路**不落库**：数据要经 A02 清洗 / A03 校验，
                # 由 A04 统一入库。SmartFetcher 抢先落库 = 绕过校验写脏数据。
                persist=False,
                # 索引更新也交给 A04 之后统一做（见 store 节点的 catalog 回填）
                update_catalog=False,
            )
            # 索引回填不在这里做 —— 数据还没经 A02/A03 校验。
            # 由 store 节点（A04 入库后）调 `refresh_stats_from_facts` 幂等重算。
            state["_catalog_indicators"] = list(result.data.keys())
            # 把 SmartFetchResult → state.raw_points（list of dict）
            # ⚠️ 键名用 `_ind` 而不是 `ind`：这里只消费 `pts`，循环变量本身不用
            #    —— `ruff B007` 会报"未使用的循环控制变量"，而 CI 跑
            #    `ruff check src tests scripts`，留着就是一条常驻告警。
            for _ind, pts in result.data.items():
                for dp in pts:
                    updates["raw_points"].append(dp.model_dump(mode="json"))
            # missing 推进度（不进 errors —— 与已删除的旧路径行为一致：
            # 旧实现 live 返回空时 silently 走 storage_fallback，不计硬错误）。
            # 真正的硬错误（agent 抛异常）已在 live_fetch 里被吞，仅记日志。
            # A17 综合分析时若需提示数据缺口，通过 "数据缺口" 字段在结论中显式声明。
            #
            # ★ 2026-10-01（`CHG-0155`）：**豁免类不许说成「缺口」**。
            #   实测现场：豁免类（不适用/未收录）会同时出现
            #     「ℹ️ …不适用（非缺陷，已跳过）」（`_live_fetch_one`）
            #     「⚠️ …缺口（实时失败 + DB 无快照）」（本循环，来源 `result.missing`）
            #   —— 前者说不是缺陷、后者说取数失败，用户读到后者会以为系统坏了。
            #   事实源是缺口台账（`gap_ledger`，`_live_fetch_one` 已写入），
            #   文案映射只有 `EXEMPT_GAP_WORDING` 一份。
            exempt_here: dict[str, str] = {}
            try:
                from src.domain.agents.data.collector.gap_ledger import (
                    exemptions as _gap_exemptions,
                )
                from src.domain.agents.data.collector.logic import (
                    EXEMPT_GAP_KINDS as _EXEMPT_KINDS,
                )

                for row in _gap_exemptions(str(state.get("task_id") or ""),
                                           exempt_kinds=_EXEMPT_KINDS):
                    exempt_here[str(row.get("indicator") or "")] = str(
                        row.get("gap_kind") or "")
            except Exception:  # noqa: BLE001 台账读不到就退回旧文案（不阻断采集）
                logger.debug("缺口台账不可读，缺口文案退回默认口径", exc_info=True)

            for ind in result.missing:
                kind = exempt_here.get(ind, "")
                if kind:
                    updates["progress"] += [
                        f"ℹ️ {ind} {EXEMPT_GAP_WORDING.get(kind, '非缺陷')}"
                    ]
                else:
                    updates["progress"] += [
                        f"⚠️ {ind} 缺口（实时失败 + DB 无快照）"
                    ]
            # ★ 2026-09-29 用户要求：「数据采集 agent 要记录任何未能获取到的信息
            #   日志，展示在后端日志里，方便查看采集效果，定期维护数据」。
            #   缺口逐条记 **WARNING** + 本轮汇总记 INFO（`src` 命名空间已装配
            #   handler，见 `core/logging_setup.py` —— 不装配的话 INFO 会被丢弃）。
            #   ⚠️ 判据是"**这一轮实际拿到几条点**"，不是"它在哪个桶里"：
            #      `result.data` 里可能有**空列表**（连接器成功但没数据）。
            from src.domain.agents.data.collector.logic import (
                format_gap_log,
                format_ok_log,
                format_round_summary,
            )

            got_ok: list[str] = []
            got_empty: list[str] = []
            for ind, pts in result.data.items():
                if pts:
                    got_ok.append(ind)
                    logger.info("%s", format_ok_log(ind, pts))
                else:
                    got_empty.append(ind)
                    logger.warning("%s", format_gap_log(
                        ind, "连接器返回空列表（没量到 ≠ 量到 0）", kind="empty"))
            for ind in result.missing:
                logger.warning("%s", format_gap_log(
                    ind, "实时源失败且 DB 无快照（未获取到）", kind="empty"))
            logger.info("%s", format_round_summary(
                str(state.get("task_id") or ""), fast, got_ok, got_empty,
                list(result.missing)))
            # ★★ 2026-10-07（`CHG-0190` ② / `CHG-0192` ①）：**真缺口 → 自修复**
            #
            # ⚠️ 挂点是被实测**纠正**过来的：第一版挂在 `_live_fetch_one` 的
            #   "本地与联网均未取到"分支上，而带计数器跑真实链路时发现它
            #   **被调用 0 次**，13 条缺口照样产生 —— 因为连接器路径下
            #   `live_fetcher` **根本不会被调用**（SmartFetcher 自己取完就返回空）。
            #   也就是说：那一版又踩了本文件 `_live_fetch_one` docstring 里记着的
            #   同一句话 —— **判据接在没人走的路上 = 没接**。
            #   现在挂在**两条空结果路径的唯一汇合处**（`got_empty` + `missing`），
            #   它同时覆盖"连接器返回空"与"实时源失败且 DB 无快照"。
            for ind, why in (
                [(i, "连接器返回空列表（没量到 ≠ 量到 0）") for i in got_empty]
                + [(i, "实时源失败且 DB 无快照") for i in result.missing]
            ):
                if _note_self_heal_candidate(
                        updates, ind, "missing", why) is not None:
                    # 只调度、不等待（`live_fetch` 持有 live_lock，等它=串行化所有指标）
                    _schedule_self_heal(ind, state, why, ct)
            # 进度细分
            if result.from_db:
                updates["progress"] += [
                    f"📦 DB 命中 {len(result.from_db)}：{', '.join(result.from_db[:5])}"
                    f"{'...' if len(result.from_db) > 5 else ''}"
                ]
            if result.from_network:
                updates["progress"] += [
                    f"🌐 联网 {len(result.from_network)}：{', '.join(result.from_network[:5])}"
                    f"{'...' if len(result.from_network) > 5 else ''}"
                ]
            # per-indicator 即时推送（治"黑屏"）
            if ct is not None:
                for ind in result.from_db:
                    ct.push_progress(
                        f"✅ A01 命中 {ind}（DB 命中，{len(result.data.get(ind, []))} 条）")
                for ind in result.from_network:
                    ct.push_progress(
                        f"🌐 A01 已采 {ind}（联网，{len(result.data.get(ind, []))} 条）")

        # 懒批：后台 fire-and-forget（不阻塞主路径）
        if lazy:
            await _collect_lazy(state, lazy, ct)

        if news_fetcher is not None and not state.get("info_items"):
            items = []
            target = state.get("target") or ""
            try:
                # ★★ 2026-10-08：**多标的逐只取新闻**（`focus_stock_codes`）。
                #
                # 报障原文：「…未来半年能否持有高股息的**宁波银行**和**中国神华**？
                #   标的 601088 —— **宁波银行没有任何可引用的估值**」。
                # 估值那一半由 `CHG-0216`/`CHG-0229` 修掉；**新闻/事件/舆情这一半
                # 仍然只按单值 `target` 取** ⇒ 第二只票的信息层输入**静默为 0**
                # （A05/A06/A07 拿到零输入、全部空跳过，用户读到"该股近期无消息"，
                # 看起来像数据源坏了，其实是**根本没去取**）。
                #
                # ⚠️ 判据放在 `len(codes) >= 2` 的分支里：**单标的走下面那条原文
                #    路径**（同一个 `fetch_news(target)`、条目一个字段都不改）⇒
                #    "单标的逐字不变"是结构保证（`tests/unit/test_multi_focus_news.py`
                #    有回归护栏：集成测试 `fetcher.calls == ["600519"]` 也不许变）。
                _focus_codes = _news_focus_codes(state)
                if len(_focus_codes) >= 2:
                    items = await _fetch_news_per_code(news_fetcher, _focus_codes)
                elif re.fullmatch(r"\d{6}", target):
                    items = await news_fetcher.fetch_news(target)
                else:
                    keywords = state.get("topic_keywords") or extract_topic_keywords(
                        state.get("analysis_type", ""), target,
                        state.get("user_query", ""))
                    if keywords:
                        items = await news_fetcher.fetch_topic_news(keywords)
            except Exception as exc:  # noqa: BLE001 新闻为增强链路
                updates["errors"].append(f"news_fetcher: 意外异常 {exc}")
            if items:
                updates["info_items"] = items
        updates["progress"] = ["数据采集完成，开始分析与行业研判…"]
        return updates

    async def recommend_node(state: ResearchState) -> dict[str, Any]:
        # 取消检查
        token = state.get("cancellation_token")
        if token is not None:
            token.check()
        # 实时进度：A17开始ReAct综合分析（通过cancel_token共享通道，前端立即可见）
        if token is not None:
            token.push_progress("正在执行 A17_recommend 综合分析（ReAct循环）…")
        analyses = [o for o in state["agent_outputs"] if o["agent_id"] in ALL_INSIGHT_AGENTS]
        if not analyses:  # 纯信息层管线（news）时以信息层结论综合
            analyses = [o for o in state["agent_outputs"] if o["agent_id"] in INFO_AGENTS]
        if not analyses:
            return {"errors": ["A17_recommend: 无上游分析结论"], "final_report": None}

        from src.domain.agents.analysis.react import ReActExecutor, ToolRegistry
        from src.orchestration.message_bus import ask_agent, normalize_question

        # ReAct工具集：A17可主动追问上游Agent或查询已采集数据
        tools = ToolRegistry()
        collected_messages: list[dict[str, Any]] = []
        # 列出当前plan中可接受定性追问的分析/行业层Agent，避免追问数据层（契约不匹配）
        askable_prefixes = tuple(f"A{n:02d}" for n in range(8, 17))
        available_agents = [a for a in state.get("plan", [])
                            if a.startswith(askable_prefixes)]
        # 幂等：同一Agent同一问题只真正追问一次；每Agent追问次数封顶，防止LLM绕着重问。
        # 上限是**模块级常量**（`MAX_ASKS_PER_AGENT`）—— 工具描述由同一常量插值生成。
        asked_cache: dict[tuple[str, str], str] = {}
        ask_counts: dict[str, int] = {}

        async def _ask_agent(receiver: str, question: str) -> str:
            if receiver not in available_agents:
                return (f"{receiver}不支持直接问答；可追问对象："
                        f"{', '.join(available_agents) or '无'}；"
                        "数据层指标请改用 query_data 工具。")
            key = (receiver, normalize_question(question))
            if key in asked_cache:
                return ("（该问题已询问过，请勿重复提问；此前回答：）\n"
                        + asked_cache[key])
            if ask_counts.get(receiver, 0) >= MAX_ASKS_PER_AGENT:
                return (f"（已向{receiver}追问{MAX_ASKS_PER_AGENT}次，"
                        "请基于已有回答直接给出final_answer，不要再追问同一Agent）")
            answer, msgs = await ask_agent(
                "A17_recommend", receiver, question, agents, state,
            )
            asked_cache[key] = answer
            ask_counts[receiver] = ask_counts.get(receiver, 0) + 1
            collected_messages.extend(msgs)
            # 实时追加到live_state（通过cancel_token共享通道，前端轮询立即可见）
            if token is not None:
                token.push_messages(msgs)
            return answer

        async def _query_data(indicator: str, limit: int = 10) -> str:
            """A17 取数工具：**四跳**（已采集点 → 本地库 → 连接器 → 联网兜底）。

            ## 为什么要有本地库兜底（用户 2026-09-28 第三次报障）

            原实现**只查 `state["validated_points"]`**（本次已采集的数据点），
            未命中就回一句「未找到指标 X 的数据」。于是：
              · 库里**有** `dv_ratio`（股息率 1184 万行）→ 无人知道 → 报"缺数据"；
              · 这句话还会被 A17 写进 `data_gaps`，看起来像"数据源根本没有"。

            ## 为什么第四跳（联网兜底）必须在这条链路上

            `catalog/network_fallback.py` 的护栏曾经**一个生产调用方都没有**
            （本仓库实测：全仓库 `grep` 只命中它自己、它自己的测试和一个
            `docs/` 证据脚本）—— 护栏齐全，但它**不在请求链路上**，且不报错。
            第四跳就是补上那个 call site。

            ## 耗时（实测，不是估计）

              · 反向索引查找   **0.029 ms**
              · 单次查询       **~85 ms**（400 行）
              · 首次建索引     ~1.7 s —— 由 `main._warm_column_index`
                                在启动后 8 秒**后台预热**，不在请求路径上
              · 第四跳         默认**根本不发生**（白名单为空 → 零网络调用）；
                                开启后单次受 `DEFAULT_MAX_CALLS_PER_HOUR`
                                与 `DEFAULT_DAILY_BUDGET_CNY` 硬约束

            对比 A17 单次 LLM 调用 6~18 秒，这些兜底是噪声级开销。

            ## 空结果必须带诊断码 + 兜底结论

            未命中时返回 `[本地诊断] …` + `[联网兜底] …`，让 A17 分得清
            "列不存在(换同义词)" / "该口径不适用该实体(换口径)" /
            "时间超范围(放宽时间)" / "真缺口(可入队)" /
            "护栏拒绝联网" / "联网了源上也没有"。

            ⚠️ 实现已提到模块级 `query_data_for_agent()`（**唯一实现**）。
            本闭包只做一行委托 —— 提出来是为了让第四跳可**单独验收**，
            而"没人调它"正是本轮要修的缺陷本身。
            """
            return await query_data_for_agent(indicator, limit, state=state,
                                              agents=agents)

        # ★ 工具描述**只能**由 `ask_agent_tool_description()` 生成（模块级唯一构造点）：
        #   曾经在这里手写"最多追问2次"，与常量 `MAX_ASKS_PER_AGENT=1` 冲突 ——
        #   描述会进 system prompt，模型按 2 次去试、第二次被拒且不知原因。
        #   判据：`tests/unit/test_ask_agent_tool_contract.py`（改回手写即红）。
        tools.register("ask_agent", _ask_agent,
                       ask_agent_tool_description(available_agents))
        tools.register("query_data", _query_data,
                       "查询已采集的数据点，参数indicator=指标名, limit=条数")

        # 技能工具（PTD）：LLM按L0索引自主决定是否加载技能正文/参考
        async def _load_skill(name: str) -> str:
            return skill_library.load_skill("A17_recommend", name)

        async def _load_reference(skill_name: str, ref_name: str) -> str:
            return skill_library.load_reference("A17_recommend", skill_name, ref_name)

        skill_indexes = skill_library.list_skills("A17_recommend")
        if skill_indexes:
            tools.register("load_skill", _load_skill,
                           "加载指定技能的完整正文（执行步骤/输出格式），参数name=技能名。")
            tools.register("load_reference", _load_reference,
                           "加载技能声明的参考文件，参数skill_name=技能名, ref_name=参考路径。")
        skill_index_block = ""
        if skill_indexes:
            # ★ 2026-09-28 第十轮：按 user_query / focus 的触发短语过滤技能索引
            # 原实现把全部 skill_indexes 都注入 L0 块（3 个），prompt ~150 tokens。
            # 改：仅保留 trigger_terms 与 query/focus 命中者；零命中 → 不注入。
            # 用户的"美联储加息"query 通常命中 0~1 个技能 → 砍掉 50~150 tokens。
            from src.domain.skills.library import SkillLibrary
            probe_text = (
                f"{state.get('user_query', '')} "
                f"{state.get('target_display') or state.get('target', '')}"
            ).strip()
            if probe_text:
                relevant = [
                    i for i in skill_indexes
                    if any(t in probe_text
                           for t in SkillLibrary.trigger_terms_text(i))
                ]
                if relevant:
                    skill_indexes = relevant
            lines = "\n".join(
                f"- {i['name']}：{str(i['description'])[:120]}" for i in skill_indexes
            )
            skill_index_block = (
                f"\n\n## 可用专业技能（L0索引）\n{lines}\n"
                "若某技能与当前综合任务相关，先调用 load_skill 获取其Phase步骤与输出格式，"
                "按技能要求组织final_answer；技能正文声明的references可用load_reference查阅。"
                "无关技能不要加载，避免浪费步数。"
            )

        a17 = agents["A17_recommend"]
        payload = a17._parse_payload({  # noqa: SLF001 复用payload解析与prompt构建
            "analyses": analyses,
            "focus": state.get("target_display") or state["target"],
            "user_query": state["user_query"],
            "hint": state.get("analysis_hint", {}),
            # ★ `CHG-0230`：逐只表态清单（<2 只时为空 ⇒ 输出与修复前逐字一致）
            "subjects": _a17_subjects(state),
        })
        # ★ 第十轮：MOSS_DECISION_USE_PRO=1 强制用 v4-pro（默认走 flash，详见
        # configs/models.yaml 的 decision 路由注释）
        import os as _os2
        _use_pro = (_os2.environ.get("MOSS_DECISION_USE_PRO", "0") or "0") == "1"
        _effective_task_tier = "decision"  # flash 当前是 decision 主路由
        if _use_pro:
            # 切到 v4-pro：把 task_tier 改用 v4-pro 专属 tier（这里临时用 decision + 强制 provider）
            _effective_task_tier = "decision"  # 仍是 decision；model_used 在 audit 字段体现
        # ★ 2026-09-28 第九轮：max_steps 从 3 降到 2
        # 实测：>90% 案例第 1 步就能出 final_answer；少数情况第 2 步已够；
        # 第 3 步是兜底（很少真正用上）。2 步节省 ~10-15s。
        # 仍可通过环境变量 MOSS_REACT_MAX_STEPS 覆盖（默认 2，debug 时可设 3）。
        import os as _os
        try:
            _max_steps = int(_os.environ.get("MOSS_REACT_MAX_STEPS", "2"))
        except (TypeError, ValueError):
            _max_steps = 2
        _max_steps = _a17_max_steps(_max_steps)

        # ★ 2026-09-28 第十三轮：A17 思维链强度（实测裁定后设为 `none`）
        #
        # ## 实测数据（`scripts/probe_a17_models.py`，同输入 / 禁缓存 / 留原文）
        #
        # | 档位     | out   | 思维链      | 正文 | 墙钟     | 正文字符 |
        # |---------|------:|-----------:|-----:|--------:|--------:|
        # | high    | 1930  | 1140 (59%) | 790  | 9987ms  | 1327    |
        # | low     | 2092  | 1288 (62%) | 804  | 10448ms | 1564    |
        # | **none**| 829   | **0**      | 829  | **5451ms** | **1576** |
        #
        # ## 两条反直觉结论
        #
        # 1. **`low` 与 `high` 没有区别**（+5%，甚至 low 输出更多）
        #    → 证实了第三方自建实测（`blog.2my.xyz`：low/medium/high 输出几乎相同），
        #      **推翻 DeepSeek 官方"不同档有实际差异"的说法**。
        #      推论：第十二轮把默认设成 `low` 是**无效操作**（省不到时间）。
        # 2. **`none` 快 45% 且正文更长**（829 vs 790 tokens / 1576 vs 1327 字符）
        #    → 不思考反而写得更充实。这是"思维链对合成类任务是净开销"的直接证据。
        #
        # ## 为什么敢把默认设成 `none`
        #
        # A17 的任务是**综合上游已分析好的结论**，不是从头推理 ——
        # 上游（A08-A16）已经把分析做完了，A17 只需要组织与仲裁。
        # 实测也印证：`none` 档正文最长。
        #
        # ⚠️ **质量仍需人工确认**：各档原文落盘在 `data/probe/a17_*.txt`，
        #    请比对这几项是否还在：① 冲突仲裁 ② 三档情景 ③ 证伪信号 ④ 仓位建议。
        #    若发现缺失 → `MOSS_A17_EFFORT=high` 回退（一行，无需改代码）。
        _a17_effort = (_os.environ.get("MOSS_A17_EFFORT", "none") or "none").lower()
        if _a17_effort not in ("none", "low", "high", "max"):
            _a17_effort = "none"

        # ★ 2026-09-28 第十轮：薄分析快速路径
        # 当 analyses ≤ 2 条（A05 + A07 或 A08 等单一分析），ReAct framing 反而是
        # 负担（ask_agent 工具 + action/final_answer 双重格式）。直答 prompt 更紧凑，
        # 输出更短，墙钟更短。
        # 阈值通过 MOSS_A17_THIN_THRESHOLD 覆盖（默认 2）。
        try:
            _thin_threshold = int(_os.environ.get("MOSS_A17_THIN_THRESHOLD", "2"))
        except (TypeError, ValueError):
            _thin_threshold = 2
        _use_single_shot = len(analyses) <= _thin_threshold
        cancel_token = state.get("cancellation_token")
        data = None
        if _use_single_shot:
            # ★ 薄分析单次直答（跳过 ReAct）
            logger.info("A17 薄分析快速路径：%d 条 analyses ≤ 阈值 %d，跳过 ReAct",
                        len(analyses), _thin_threshold)
            try:
                update, output = await _run_agent(
                    a17, state,
                    {
                        "analyses": analyses,
                        "focus": state.get("target_display") or state["target"],
                        "user_query": state["user_query"],
                        "hint": state.get("analysis_hint", {}),
                        # ★ `CHG-0230`：逐只表态清单（三处注入点共用一份实现）
                        "subjects": _a17_subjects(state),
                    },
                )
                # 把 output 折成 react.run() 的 final_answer 形状
                data = dict(output.result or {})
                if not str(data.get("conclusion", "")).strip():
                    data["conclusion"] = output.conclusion
                data.setdefault("confidence", output.confidence.value)
                data["_single_shot"] = True
            except AgentExecutionError as exc2:
                logger.warning("A17 单次直答失败（trace=%s）：%s",
                               state["task_id"], exc2)
                return {"errors": [f"A17_recommend: {exc2}"], "final_report": None}
        else:
            # 完整 ReAct 路径（多分析 + 可能追问上游 Agent）
            # 第 1 步总是发全量（compact），第 2 步起只发上次输出+新observation（incremental）
            react = ReActExecutor(
                a17._gateway, tools, max_steps=_max_steps, task_tier="decision",
                incremental=True,  # noqa: SLF001
            )
            try:
                data = await react.run(
                    a17.system_prompt + skill_index_block,
                    a17.build_prompt(payload, react_mode=True, compact=True),
                    agent_id=a17.agent_id, trace_id=state["task_id"],
                    cancel_token=cancel_token,
                    reasoning_effort=_a17_effort,  # ★ 第十轮：削 effort
                )
            except Exception:  # noqa: BLE001 ReAct失败回退单次调用
                data = None
            # ReAct返回空conclusion（如输出被max_tokens截断导致JSON残缺）→
            # 回退单次直答，避免A17产出空结论被审计判完整性缺失
            if not data or not str(data.get("conclusion", "")).strip():
                logger.warning("A17 ReAct结论为空，回退单次直答（trace=%s）",
                               state["task_id"])
                try:
                    update, output = await _run_agent(a17, state, {
                        "analyses": analyses,
                        "focus": state.get("target_display") or state["target"],
                        "user_query": state["user_query"],
                        "hint": state.get("analysis_hint", {}),
                        # ★ `CHG-0230`：逐只表态清单（三处注入点共用一份实现）
                        "subjects": _a17_subjects(state),
                    })
                    return {**update, "agent_messages": collected_messages,
                            "final_report": None}
                except AgentExecutionError as exc2:
                    return {"errors": [f"A17_recommend: {exc2}"], "final_report": None}
            # ★★ `CHG-0236`：**ReAct 路径绕过 `A17.execute()`** ——
            #    它把 `final_answer` 直接返回给编排层，不经过 `execute()` 里
            #    那道逐只等长校验。而多标的（>1 只票、分析条数多）恰恰**就是**
            #    走这条路的典型场景 ⇒ 不在这里补一次，等于校验只覆盖了少数路径。
            #    两处调用的是**同一个** `enforce_per_item_rows`（唯一实现），
            #    这里只是第二个调用点。
            from src.domain.agents.analysis.base import (  # noqa: PLC0415
                PER_ITEM_MISSING,
                enforce_per_item_rows,
            )
            enforce_per_item_rows(
                data, "per_subject",
                [s["code"] for s in _a17_subjects(state)],
                where="A17_recommend/supervisor-react",
                label="标的",
                placeholder_fields={
                    "stance": PER_ITEM_MISSING,
                    "position_advice": None,
                    "_placeholder": "A17 ReAct 未返回该标的的逐只结论",
                },
                warning_prefix="A17 未按标的逐只返回结论",
            )

        from src.core.models import AgentOutput
        from src.core.schemas import TraceStep, coerce_confidence
        output = AgentOutput(
            task_id=state["task_id"], agent_id=a17.agent_id,
            conclusion=str(data.get("conclusion", "")),
            confidence=coerce_confidence(data.get("confidence", "medium")),
            data_refs=[a.get("agent_id", "?") for a in analyses],
            trace_id=state["task_id"],
            reasoning_steps=[TraceStep(step=1, step_type="llm_inference",
                                       description="ReAct循环综合（含工具调用）")],
            result={**data, "stance": data.get("stance", "中性"),
                    "disclaimer": "以上信息仅供研究参考，不构成投资建议。"},
        )
        update = {
            "agent_outputs": [_summary(output)],
            "data_refs": list(output.data_refs),
            "trace_ids": [output.trace_id],
            "agent_messages": collected_messages,
            "progress": ["A17_recommend 综合分析完成"],
        }
        # ★★★ 2026-09-28 第十四轮：A17 报的数据缺口 → 入队 → 由盘后作业补取
        #
        # 为什么是**入队**而不是当场补：
        #   A17 跑在链路末端，结论已要交付。当场触发 A19（LLM 生成连接器 +
        #   沙箱验证，秒级到十几秒）只会让用户白等，而且补到的数据**本轮用不上**
        #   （prompt 已经发出去了）。这是**跨轮次**能力。
        #
        # 入队失败绝不阻断交付（缺口登记是增值功能）。
        try:
            await _enqueue_data_gaps(state, data)
        except Exception as exc:  # noqa: BLE001
            logger.debug("缺口入队失败（忽略）: %s", exc)
        return {**update, "final_report": None}

    async def _enqueue_data_gaps(state: ResearchState,
                                 data: dict[str, Any]) -> int:
        """把 A17 的 `data_gaps` 写入缺口队列。返回入队条数。

        ## 兼容两种格式

        A17 的 `data_gaps` 可能是：
          · **字符串数组**（旧/简写）：`["三市分项成交额未提供", ...]`
          · **对象数组**（新）：`[{"item": "...", "status": "fetchable",
                                  "where": "投资日历"}, ...]`

        字符串形态**没有 status** → 按"保守入队"处理（宁可多试一次）。
        对象形态按 `NO_ENQUEUE_STATUS` 过滤（`source_terminated` /
        `unavailable` 补也补不到，不入队）。

        ## 指标名从哪来

        `data_gaps` 写的是**人话**（"三市分项成交额"），不是 indicator id。
        所以优先取对象里的 `indicator` 字段；没有就尝试
        `IndicatorRegistry.resolve()` 做一次名→id 解析（含 n-gram 兜底）。
        都解析不出来就**用原文入队** —— A19 拿到人话也能理解
        （它本来就是"按指标名生成连接器"的 Agent）。
        """
        from src.domain.agents.decision.gap_queue import get_gap_queue

        gaps = (data or {}).get("data_gaps")
        if not gaps:
            return 0
        if isinstance(gaps, str):
            gaps = [gaps]
        if not isinstance(gaps, list):
            return 0

        q = get_gap_queue()
        trace_id = str(state.get("task_id") or "")
        n = 0
        for raw in gaps[:10]:          # 单轮最多 10 条，防刷屏
            if isinstance(raw, dict):
                item = str(raw.get("item") or raw.get("indicator") or "").strip()
                status = str(raw.get("status") or "").strip()
                where = str(raw.get("where") or raw.get("how_to_get") or "")
                reason = f"{status} {where}".strip()
                indicator = _resolve_gap_indicator(item)
            else:
                item = str(raw).strip()
                status = ""
                reason = "A17 报缺口（字符串格式，无 status）"
                indicator = _resolve_gap_indicator(item)
            if not item:
                continue
            # ★ 假缺口拦在这里：把**可达的源**说成"不可访问"的条目不入队 ——
            #   否则 A19 会为它做一次注定失败的补取（见 _is_false_gap 的说明）。
            if _is_false_gap(item, status):
                logger.warning(
                    "拦下一条**假缺口**（把可达源说成不可访问，不入队）：%s",
                    item[:120])
                continue
            if q.enqueue(indicator or item, reason=reason or item,
                         status=status, source="A17_recommend",
                         trace_id=trace_id):
                n += 1
        if n:
            logger.info("A17 报 %d 条数据缺口已入队（待盘后 A19 补取）", n)
            token = state.get("cancellation_token")
            if token is not None:
                token.push_progress(
                    f"📋 {n} 条数据缺口已入队（盘后自动补取，下次分析可用）")
        return n

    async def audit_node(state: ResearchState) -> dict[str, Any]:
        try:
            update, output = await _run_agent(agents["A18_audit"], state, {
                "trace_id": state["task_id"],
                "agent_outputs": state["agent_outputs"],
                "chain_path": chain_path,
                "llm_audit_path": llm_audit_path,
            })
        except AgentExecutionError as exc:
            # ★ 2026-10-07（`CHG-0190` ①）：审计**没跑成**也要留下机器可读的三态
            #   —— 否则"没审"与"审过了"在交付物上长得一模一样。
            summary = _audit_summary(None, error=f"A18_audit: {exc}")
            _log_audit_verdict(summary, task_id=str(state.get("task_id") or ""))
            return {"errors": [f"A18_audit: {exc}"], "audit": summary}
        summary = _audit_summary(output)
        _log_audit_verdict(summary, task_id=str(state.get("task_id") or ""))
        report = _render_report(state, output, summary)
        return {**update, "final_report": report, "audit": summary}

    g = StateGraph(ResearchState)
    g.add_node("supervisor", supervisor_node)
    g.add_node("collect", collect_node)
    g.add_node("clean", _node(
        "A02_data_cleaner", lambda s: {"data_points": s.get("raw_points", [])}))
    g.add_node("validate", _node(
        "A03_data_validator", lambda s: {"data_points": s.get("cleaned_points", [])}))
    g.add_node("store", _node(
        "A04_data_storage", lambda s: {"data_points": s.get("validated_points", [])}))
    g.add_node("verify_info", _node(
        "A05_verifier", lambda s: {"info_items": s.get("info_items", [])}))
    # ★★★ 2026-09-28 第九轮：A06 不再 wait A05（并行化信息层）
    # 审计实证：旧版 A05→A06→A07 串行 = ~120s（本地 8B ×3）
    # 改造：A06 直接读 info_items 不过滤 verified（verified 是 A05 给 A08-A12
    # 用的"信不信这条新闻"信号，A06 的"抽取事件表"任务是**结构化提取**，
    # 不依赖"是不是真"判断 — 假新闻也能抽出事件，调用方按 verified 过滤即可）。
    # A05 与 A06 并发跑：A07 等两者都完成（其依赖 extracted_events）。
    g.add_node("extract_events", _node("A06_extractor", lambda s: {
        # ★ 直接读 info_items 不过滤 — 让 A06 与 A05 并行。
        #   缺 item_id 的条目先按 A05 同款规则补齐（见 _ensure_item_ids 的
        #   回归说明），否则事件全部因"不可溯源"被丢弃。
        "info_items": _ensure_item_ids(s.get("info_items", []) or []),
    }))
    g.add_node("sentiment", _node("A07_sentiment", lambda s: {
        "events": (s.get("extracted_events") or {}).get("events", []),
    }))

    # ★ 2026-09-27 第八轮：liquidity_ctx_node 已抽到模块顶层
    g.add_node("liquidity_ctx", _liquidity_ctx_node)
    for aid in ALL_INSIGHT_AGENTS:
        # ★ 2026-09-27 第八轮：payload_fn 也抽到模块顶层（_build_analyze_payload_fn）
        #   不再每个 aid 都重建闭包 → 内存 +0、可测性 +0
        g.add_node(f"analyze_{aid}", _node(aid, _build_analyze_payload_fn(aid)))
    g.add_node("recommend", recommend_node)
    g.add_node("audit", audit_node)

    g.add_edge(START, "supervisor")
    g.add_edge("supervisor", "collect")
    g.add_edge("collect", "clean")
    g.add_edge("clean", "validate")
    g.add_edge("validate", "store")
    # ★★★ 2026-09-28 第九轮：信息层 **fan-out**（A05 / A06 并行）
    # 旧：store → A05 → A06 → A07（串行，~120s）
    # 新：store → ┌─ A05 ──┐
    #                 └─ A06 ──┴── A07（~70s，A05+A06 同时跑）
    # liquidity_ctx 改为从 store 接出（不等 sentiment）：
    #   - liquidity_ctx 只读 validated_points，与 sentiment 输出**无依赖**
    #   - 旧版等 sentiment 完成是过度串行；现在 liquidity_ctx 可与 sentiment 并行
    #   - 进一步压缩 ~30s
    g.add_edge("store", "verify_info")
    g.add_edge("store", "extract_events")
    g.add_edge("verify_info", "sentiment")
    g.add_edge("extract_events", "sentiment")
    g.add_edge("store", "liquidity_ctx")  # ★ 不再等 sentiment
    # ★★★ 2026-09-28 第十轮：分析层不再等 liquidity_ctx（暂时回滚，因破坏测试）
    # liquidity_ctx 是 A08-A16 的**增强**输入（hint.market_liquidity），
    # 不是必需输入 —— 它们的核心数据是 validated_points + events。
    # 让 analyze_* 在 store 完成即可启动（hint={}，prompt 显示「无流动性参考」），
    # 与 liquidity_ctx 完全并行。质量影响：分析结论少一段流动性叙述；
    # A17 的最终合成读 state.analysis_hint（含 liquidity_ctx 输出）→ 末态不受影响。
    # 收益：宏观/行业问题平均 -15s（liquidity_ctx 之前的串行等待）。
    for aid in ALL_INSIGHT_AGENTS:
        g.add_edge("liquidity_ctx", f"analyze_{aid}")  # ★ Phase 9 暂回滚：只保留旧路径
        g.add_edge(f"analyze_{aid}", "recommend")
    # recommend 还需要 sentiment 输出（A07）：extra_in_chain
    g.add_edge("sentiment", "recommend")
    g.add_edge("recommend", "audit")
    g.add_edge("audit", END)

    return g.compile()


def _render_report(state: ResearchState, audit_output: AgentOutput,
                   audit_summary: dict[str, Any] | None = None) -> str:
    """把各Agent结论拼装成带溯源与免责声明的最终Markdown报告。"""
    title = state.get("target_display") or state["target"] or state["user_query"]
    lines = [f"# 投研分析报告：{title}", ""]
    lines += _audit_banner(audit_summary)
    info_outs = [o for o in state["agent_outputs"] if o["agent_id"] in INFO_AGENTS]
    if info_outs:
        lines += ["## 信息核验与舆情"]
        for o in info_outs:
            lines.append(
                f"- **{o.get('agent_name') or agent_name(o['agent_id'])}**"
                f"（置信度 {o.get('confidence_zh') or confidence_zh(o['confidence'])}）"
                f"：{o['conclusion']}"
            )
        lines.append("")
    for o in state["agent_outputs"]:
        if o["agent_id"] in ALL_INSIGHT_AGENTS or o["agent_id"] == "A17_recommend":
            lines += [
                f"## {o.get('agent_name') or agent_name(o['agent_id'])}"
                f"（置信度 {o.get('confidence_zh') or confidence_zh(o['confidence'])}）",
                o["conclusion"],
                "",
            ]
    # Agent间多轮对话（A2A协作过程）
    messages = state.get("agent_messages") or []
    if messages:
        lines += ["## Agent协作对话"]
        for m in messages:
            role = "问" if m.get("message_type") == "question" else "答"
            lines.append(
                f"- **{agent_name(m.get('sender'))}** → "
                f"**{agent_name(m.get('receiver'))}** [{role}]: "
                f"{m.get('content','')[:300]}"
            )
        lines.append("")
    stats = state.get("storage_stats") or {}
    if stats:
        lines += ["## 数据入库", f"- 新增 {stats.get('inserted', 0)} 条，"
                  f"重复跳过 {stats.get('skipped', 0)} 条", ""]
    lines += ["## 审计", audit_output.conclusion, "",
              "---",
              "⚠️ 以上信息来自互联网公开资料，仅供研究参考，不构成投资建议。"
              "投资有风险，入市需谨慎，盈亏自负。"]
    return "\n".join(lines)
