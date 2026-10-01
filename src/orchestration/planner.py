"""LLM驱动的Supervisor规划器：把用户问题分解为执行计划。

PRD要求"Supervisor通过LLM动态决策任务执行顺序"。本模块用LLM理解用户意图，
从18个Agent能力目录中选择参与Agent与采集指标；LLM失败时由supervisor回退
到规则式plan_run()，保证链路不中断。
"""

from __future__ import annotations

import logging
from typing import Any

from src.domain.agents.analysis.base import parse_llm_json
from src.infrastructure.llm import LLMGateway
from src.infrastructure.llm.gateway import _ATTEMPT_BUDGET

logger = logging.getLogger(__name__)

#: 规划层单次调用的墙钟预算（秒）。**取 light 层的既有权威默认值**，
#: 不另造数字 —— `_ATTEMPT_BUDGET["light"]` 的 20s 是 2026-09-26 实测定的
#: （比云端 p90 18.3s 略宽、又远小于本地模型的 45s）。
#:
#: **为什么必须显式给**（2026-09-28 实测）：`light` 层钉了 `local_only: true`
#: （防静默降级到付费云端），`complete()` 在 `pin_local` 时把降级链**裁到 1 跳**
#: —— 而延迟预算的启发式写着"最后一跳不设预算"（怕把"慢但正确"变成"必然失败"）。
#: 两者交叉出的结果：本地模型挂死时，规划层**裸调到 HTTP 120s 超时**。
#:
#:     实测（`scripts/_e2e_timing_probe.py`，astream 逐节点计时）：
#:       supervisor_planner  in=0 out=0  120294ms
#:       端到端 123.45s，其中这一个节点吃掉 120.31s，后面 19 个节点共 3.1s
#:
#: 显式传入后，超时被 `asyncio.wait_for` 在预算处中断 → 落到下面的规则式规划兜底。
#: 实测端到端 **123.45s → 12.22s**（10 倍）。
#:
#: ⚠️ 预算值必须覆盖**冷路径**，否则会把"能成功"变成"必然失败"：
#:     冷加载 qwen2.5:1.5b（显存驱逐后重新载入）   9.5s   ← 实测
#:     规划生成（1102 tokens 输入 → 结构化 plan）  4.73s  ← 实测
#:     ────────────────────────────────────────────────
#:     冷路径合计                                  ≈14.2s
#: 我第一版拍了个 12s（理由是"本地正常 2~5s"—— 那是代码注释里的旧数字，
#: 不是实测），结果冷启动下规划层**必然失败**。20s 覆盖冷路径并留 40% 余量。
#: **待办**：启动时预热该模型可让用户请求不再付这 9.5s（见 main.py 的
#: `_warm_llm_cache_index` 同类做法），届时候选下调本值。
_PLANNER_BUDGET_SEC = _ATTEMPT_BUDGET["planning"]

#: 规划层的**输出**预算（tokens）。
#:
#: 为什么必须显式给（2026-09-28 实测）：`light` 层的层级输出预算是
#: `_TIER_OUTPUT_BUDGET["light"] = 1024`，而规划要输出的是
#: 「analysis_type + 参与 Agent 清单 + 采集指标清单」的结构化 JSON ——
#: **1024 不够**。审计日志抓到的现场：
#:
#:     supervisor_planner | in=2280 out=1024 | 6654ms   ← out 正好 1024
#:     → AgentExecutionError: LLM输出非合法JSON:
#:           Expecting value: line 5 column 2327
#:
#: 即 **JSON 被从中间截断**，报错却长得像"模型不会写 JSON"。
#: 在没有预算的那几轮里，这个截断被 120s 超时盖住，根本看不到。
#:
#: 取 2048（与 medium 层同档）：规划 JSON 实测在 1024~2048 之间，
#: 给一倍余量；这是**上限**，未用满不计费、不拖慢。
_PLANNER_MAX_TOKENS = 2048

# 可用指标目录（与supervisor._PLANNING/INDUSTRY_INDICATORS对齐），
# LLM规划时据此选择需要采集的指标，避免凭空编造不存在的indicator id。
INDICATOR_CATALOG: list[dict[str, str]] = [
    {"id": "CPI", "desc": "中国居民消费价格指数同比（月度）"},
    {"id": "PPI", "desc": "中国工业生产者出厂价格指数同比（月度）"},
    # ★ 2026-09-28 第二十五轮：中国端**货币/信用**两个口径补进目录。
    #
    # 【为什么是缺口】A08_macro 的 system_prompt 白纸黑字写着
    #   「依据给定中国(CPI/PPI/M2/社融)与美国(...)数据点」，
    #   `indicators.yaml` 也登记了 `M2` / `社融`（id 就叫这两个），
    #   `AkshareConnector.supports("M2"|"社融")` 为真、库里各有 119/115 行
    #   （实测 `data/moss_finagent.db`：M2 2016-10..2026-08、社融 2016-10..2026-04）
    #   —— 但 **planner 目录里一条都没有**：
    #   症状是"用户问中国货币/信用环境，planner 选不出这两个指标"
    #   → A08 的 prompt 声称会读，实际拿不到。
    #   这**不是**"顺手扩权"：目录是 prompt 已声称范围的补录。
    #
    # ⚠️ 同族 `PMI` / `GDP` **故意不在这里**（连接器无人 supports()，是真缺口，
    #   登记在 `tests/unit/test_contract_consistency.py::_CLAIMED_BUT_UNPRODUCED`）。
    #   加进来会让 ③→① `test_indicator_catalog_ids_are_connector_supported` 立刻红。
    {"id": "M2", "desc": "中国货币供应量M2同比（月度，AkShare macro_china_money_supply）"},
    {"id": "社融", "desc": "中国社会融资规模增量（月度，亿元，AkShare macro_china_shrzgm）"},
    # ★ 2026-09-29 第二十五轮：`PMI` / `GDP` 补进目录。
    #
    # 【为什么之前"故意不加"】生产者不存在时加进来会让 ③→①
    #   `test_indicator_catalog_ids_are_connector_supported` 立刻红 ——
    #   那是**正确**的红（planner 不该承诺取不到的东西）。
    # 【现在为什么可以加】`macro_extra_connector.MacroExtraConnector` 提供了生产者
    #   （实测 `supports('PMI')` / `supports('GDP')` / `supports('GDP:同比')`
    #   全认），`indicators.yaml` 也已登记 → ③→① 与 ③→② 两侧同时成立。
    #   A08 的 prompt 声称"依据给定中国(...)数据点"，白名单里也一直写着
    #   `"PMI"` / `"GDP"` / `"GDP同比"` —— 目录缺它们 = 声称了却选不出。
    #
    # ⚠️ 口径：`PMI` 是**制造业**口径（50=荣枯线）；`GDP` 是**累计值**（亿元）；
    #   `GDP:同比` 是**累计同比**（%）—— 别把累计值当单季值用。
    {"id": "PMI", "desc": "中国官方制造业PMI（扩散指数，50=荣枯线，月度，东财/统计局）"},
    {"id": "GDP", "desc": "中国GDP累计值（现价，亿元，季度，东财/统计局）"},
    {"id": "GDP:同比", "desc": "中国GDP累计同比（不变价，%，季度）"},
    {"id": "us_cpi_yoy", "desc": "美国CPI同比（月度，AkShare macro_usa_cpi_yoy）"},
    {"id": "us_core_cpi", "desc": "美国核心CPI同比（月度，AkShare macro_usa_core_cpi_monthly）"},
    {"id": "us_nonfarm", "desc": "美国非农就业新增（月度，AkShare macro_usa_non_farm）"},
    {"id": "us_unemployment", "desc": "美国失业率（月度，AkShare macro_usa_unemployment_rate）"},
    {"id": "us_fed_rate", "desc": "美联储利率决议（FOMC，AkShare macro_bank_usa_interest_rate）"},
    {"id": "us_pce", "desc": "美国核心PCE物价指数（月度，AkShare macro_usa_core_pce_price）"},
    # ★ 2026-09-28 第二十三轮：FRED 政策利率三个**单值**序列
    #   （旧混合口径 fed:policy_range 同日有上下限两条，取"最新一条"
    #    会把区间下限当成利率 —— 所以 planner 只暴露单值序列）
    {"id": "fed:effr", "desc": "有效联邦基金利率（日频，FRED DFF，判鹰鸽方向用它）"},
    {"id": "fed:target_upper", "desc": "美联储目标区间上限（FRED DFEDTARU）"},
    {"id": "fed:target_lower", "desc": "美联储目标区间下限（FRED DFEDTARL）"},
    {"id": "stock_close", "desc": "个股日收盘价（需带6位代码后缀，如stock_close:300308）"},
    {"id": "PE(TTM)", "desc": "个股滚动市盈率（需带代码后缀）"},
    {"id": "PB", "desc": "个股市净率（需带代码后缀）"},
    {"id": "资产负债率", "desc": "个股资产负债率（需带代码后缀）"},
    {"id": "流动比率", "desc": "个股流动比率（需带代码后缀）"},
    {"id": "产权比率", "desc": "个股产权比率（负债/股东权益，%）（需带代码后缀）"},
    # ★ 2026-09-29：A12 合规**本地规则直接消费**的比率族（`ComplianceFinConnector`）。
    #   为什么必须进目录：不进目录 → planner 选不出 → 采集侧不取 → A12 的
    #   `evaluate_compliance()` 拿不到输入 → 等级只能是「未量到」
    #   （缺口不再伪装成"无风险"，但**如实报缺口 ≠ 补上缺口**）。
    #   判据：`test_contract_consistency.py` 的 ③→①（目录里的 id 必须有人 supports）
    #   与 ③→②（必须已登记）；两条都过。
    #   ⚠️ `大股东质押比例` 的口径是**出质股东持股**（不是总股本），
    #      且首次取数要整表拉 75~240s —— 已用 7 天 TTL + 定时采集预热兜住。
    {"id": "商誉占净资产比", "desc": "个股商誉/归母净资产（%，A12商誉减值旗标）（需带代码后缀）"},
    {"id": "货币资金占总资产比",
     "desc": "个股货币资金/总资产（%，A12存贷双高，与有息负债同用）（需带代码后缀）"},
    {"id": "有息负债占总资产比",
     "desc": "个股有息负债/总资产（%，A12存贷双高，与货币资金同用）（需带代码后缀）"},
    {"id": "大股东质押比例",
     "desc": "个股出质股东质押比例上限（%=质押股数/该股东持股数，A12严重旗标）（需带代码后缀）"},
    {"id": "对外担保占净资产比",
     "desc": "个股近12个月累计担保金额/归母净资产（%，A12或有负债）（需带代码后缀）"},
    # ★ 2026-09-29：**平台自有后端数据**（`PlatformDataConnector`）。
    #   为什么必须进目录：这 5 族的数据平台早就在算（前端看板/主线挖掘/投资日历），
    #   但采集侧零生产者 ⇒ planner 选不出 ⇒ Agent 侧完全看不到，
    #   用户看到的是"缺数据"（与 `fed:policy_range` 同一形状：数据在库里，Agent 看不见，不报错）。
    #   判据：`test_contract_consistency.py` 的 ③→①（目录 id 必须有人 supports——
    #   靠连接器自己声明的 `X:{code}` 模板根命中）与 ③→②（必须已登记）两条都过。
    #   ⚠️ `估值水位` 的档位/标签**唯一来源**是 `src/intraday/valuation.py`，
    #      连接器只 import 不重写（第二份实现会让界面与 Agent 给出不同结论）。
    {"id": "估值水位",
     "desc": "个股估值空间分[-1,1]+五档（估值透支/合理偏贵/上涨空间充足，"
             "口径唯一来源 src/intraday/valuation.py）（需带代码后缀）"},
    {"id": "概念拥挤度",
     "desc": "个股**相关度最高**概念的板块拥挤度水位（ml_member_corr/ml_stock_theme × "
             "sector_crowding_daily）（需带代码后缀）"},
    {"id": "主线告警",
     "desc": "个股所属概念的中/高强度主线挖掘告警（mainline_alert，默认近90天）"
             "（需带代码后缀）"},
    {"id": "个股告警",
     "desc": "点名该股的事件告警（fact_alerts.affected_stocks_json，含impact/reason）"
             "（需带代码后缀）"},
    {"id": "解禁计划",
     "desc": "个股未来30天限售解禁计划（投资日历 cal:unlock:* 的 extra_json 明细，"
             "value=解禁市值合计）（需带代码后缀）"},
    # ★ 2026-09-29：**行业侧三族**（按**行业名**占位，不是代码后缀）。
    #   用户原话：「② 用户输入内容中含有行业的，要连接到概念板块拥挤度的数据表，
    #   找与之最相近的所属概念板块…也可以接入"板块资金流"…查询该板块近期资金流方向。
    #   ③ 用户问到当下和未来近期行情的，可以接入"行业轮动日报"里的数据。」
    #   判据同上（③→① 由连接器声明的 `X:{行业名}` 模板根命中；③→② 已登记）。
    #   ⚠️ 占位符写 `{行业名}`：`test_contract_consistency.py` 的 `_PLACEHOLDER_SAMPLES`
    #      只认那 6 个已知占位符，新造一个会让那条断言直接红。
    #   ⚠️ id 必须写成**模板形态**（`行业拥挤度:{行业名}`）而不是裸名：
    #      `test_indicator_catalog_ids_are_connector_supported` 的"模板根"那条路
    #      **只认 `_declared_code_prefixes()`（占位符恰为 `code`）**，
    #      行业族的占位符是 `{行业名}`、走的是 `_declared_other_prefixes()`，
    #      写成裸名会被判成"planner 选得出、采集侧取不到"。
    {"id": "行业拥挤度:{行业名}",
     "desc": "行业名→最相近概念板块的拥挤度水位（找得到才产点；找不到不提示）"},
    {"id": "板块资金流:{行业名}",
     "desc": "板块/行业近5日主力净流入合计+方向+榜单排名（FundFlowProvider）"},
    {"id": "行业轮动:{行业名}",
     "desc": "行业轮动日报里该行业那一行+全局研判（含 is_stale 落伍标注）"},
    {"id": "ROE", "desc": "个股净资产收益率（%，ROE）（需带代码后缀）"},
    {"id": "ROE加权", "desc": "个股加权净资产收益率（%）（需带代码后缀）"},
    {"id": "ROA", "desc": "个股总资产净利润率（%，ROA）（需带代码后缀）"},
    {"id": "销售净利率", "desc": "个股销售净利率（%）（需带代码后缀）"},
    {"id": "成本费用利润率", "desc": "个股成本费用利润率（%）（需带代码后缀）"},
    {"id": "存货周转率", "desc": "个股存货周转率（次）（需带代码后缀）"},
    {"id": "应收账款周转率", "desc": "个股应收账款周转率（次）（需带代码后缀）"},
    {"id": "总资产周转率", "desc": "个股总资产周转率（次）（需带代码后缀）"},
    {"id": "净利润增长率", "desc": "个股净利润增长率（%）（需带代码后缀）"},
    {"id": "总资产增长率", "desc": "个股总资产增长率（%）（需带代码后缀）"},
    {"id": "净资产增长率", "desc": "个股净资产增长率（%）（需带代码后缀）"},
    {"id": "营收增长率", "desc": "个股主营业务收入增长率（%）（需带代码后缀）"},
    {"id": "EPS", "desc": "个股摊薄每股收益（元）（需带代码后缀）"},
    {"id": "EPS加权", "desc": "个股加权每股收益（元）（需带代码后缀）"},
    {"id": "每股净资产", "desc": "个股每股净资产（元，BPS）（需带代码后缀）"},
    {"id": "每股经营现金流", "desc": "个股每股经营性现金流（元）（需带代码后缀）"},
    {"id": "每股未分配利润", "desc": "个股每股未分配利润（元）（需带代码后缀）"},
    {"id": "每股资本公积", "desc": "个股每股资本公积金（元）（需带代码后缀）"},
    # ⚠️ **`股息率`（合成口径）已从本目录摘掉**（2026-09-29 实测）：
    #   它走 `最近实施每股派息 ÷ 收盘价`（`_DERIVED_STOCK_INDICATORS`），
    #   数据源 Akshare `stock_history_dividend_detail` **间歇性 RemoteDisconnected**
    #   —— 实测两次真实端到端 + 用户两次报障**每次都失败**：
    #       A01采集失败(股息率:600036): ('Connection aborted.', RemoteDisconnected(…))
    #   而**同源的** `股息率TTM:{code}` 走本地行情仓（`quant_daily_basic.dv_ttm`，
    #   实测 4915 条 / 最新 2026-09-28 = 4.9606%），稳定且更新。
    #   纪律：**计划里放一个必然失败的指标 = 自己制造缺口**（"没量到"会被读成
    #   "没有股息"）。指标本身仍**登记在 `indicators.yaml`、连接器仍 supports**
    #   —— 只在"喂给 LLM 的菜单"里不出现（显式请求与下游合成分仍可用）。
    {"id": "股息率TTM", "desc": "个股股息率TTM（%，行情仓 dv_ttm，滚动十二个月）（需带代码后缀）"},
    # ★ 2026-09-28 第二十五轮：共享行情仓 `quant_daily_basic` 的列族补进目录。
    #
    # 【为什么是缺口】这 6 条是 `akshare_connector._QUANT_COLUMN_INDICATORS`
    #   的完整列族，`supports()` 全认；`indicators.yaml` 本轮补齐了登记；
    #   `_AGENT_DATA_WHITELIST` 的 A10/A11 **也早就写着**它们
    #   （"总市值"/"换手率"/"量比"/"市销率"/"股息率TTM"）——
    #   只有 planner 目录没有 → **白名单写了却永远等不到数据**。
    #
    # ⚠️ 6 条是一个**列族**：只加其中几条会让下一次报障从"另一个列名"重新长出来。
    #   同族模板根（`总市值` 这类裸名字）由 `_obtainable_connectors()` 的
    #   "连接器自己声明了 `X:{code}`" 判据认下 —— 判据是派生的，不是本文件手写。
    {"id": "股息率TTM", "desc": "个股股息率TTM（%，行情仓 dv_ttm，滚动十二个月）（需带代码后缀）"},
    {"id": "总市值", "desc": "个股市值（元，行情仓 total_mv）（需带代码后缀）"},
    {"id": "流通市值", "desc": "个股流通市值（元，行情仓 circ_mv）（需带代码后缀）"},
    {"id": "换手率", "desc": "个股换手率（%，行情仓 turnover_rate）（需带代码后缀）"},
    {"id": "量比", "desc": "个股量比（倍，行情仓 volume_ratio）（需带代码后缀）"},
    {"id": "市销率", "desc": "个股市销率TTM（倍，行情仓 ps_ttm）（需带代码后缀）"},
    {"id": "ind:半导体销售额同比", "desc": "半导体行业销售额同比（科技行业）"},
    {"id": "ind:芯片出货量同比", "desc": "芯片出货量同比（科技行业）"},
    {"id": "ind:科技行业PE(TTM)", "desc": "科技行业PE估值"},
    {"id": "ind:社会消费品零售总额同比", "desc": "社零同比（消费行业）"},
    {"id": "ind:白酒批价(元/瓶)", "desc": "白酒批价（消费行业）"},
    {"id": "ind:消费行业PE(TTM)", "desc": "消费行业PE估值"},
    {"id": "ind:动力煤价格(元/吨)", "desc": "动力煤价格（周期行业）"},
    {"id": "ind:重点电厂煤炭库存(万吨)", "desc": "电厂煤炭库存（周期行业）"},
    {"id": "ind:周期行业PE(TTM)", "desc": "周期行业PE估值"},
    {"id": "ind:创新药IND申报数量(个)", "desc": "创新药IND申报件数（医药行业，CDE受理，月度）"},
    {"id": "ind:医药行业PE(TTM)", "desc": "医药行业PE估值"},
    # 申万行业估值截面（AKShare sw_index_third_info，335个三级行业）
    {"id": "ind:sw_third_pe_ttm:all", "desc": "申万三级行业PE-TTM截面（335个行业，日频）"},
    {"id": "ind:sw_third_pb:all", "desc": "申万三级行业PB截面（335个行业，日频）"},
    {"id": "ind:sw_second_pe_ttm:all", "desc": "申万二级行业PE-TTM截面（131个行业）"},
    {"id": "ind:sw_first_pe_ttm:all", "desc": "申万一级行业PE-TTM截面（31个行业）"},
    {"id": "ind:sw_third_dividend_yield:all", "desc": "申万三级行业股息率截面"},
    # 渗透率数据（多源采集：静态基准+新闻提取+研报搜索）
    {"id": "ind:penetration:新能源汽车", "desc": "新能源汽车渗透率（%）"},
    {"id": "ind:penetration:人形机器人", "desc": "人形机器人渗透率（%）"},
    {"id": "ind:penetration:固态电池", "desc": "固态电池渗透率（%）"},
    {"id": "ind:penetration:AI大模型应用", "desc": "AI大模型应用渗透率（%）"},
    {"id": "ind:penetration:半导体国产替代", "desc": "半导体国产替代渗透率（%）"},
    {"id": "ind:penetration:HBM存储", "desc": "HBM存储渗透率（%）"},
    # A股大盘流动性（免费实时源，行业/个股买卖与持有期研判必选）
    {"id": "mkt:turnover:total",
     "desc": "两市合计成交额（亿元，腾讯/东财实时，盘中快照）"},
    {"id": "mkt:turnover:hist",
     "desc": "两市成交额近60交易日序列（亿元，用于MA5/10/50与量能分层）"},
    {"id": "mkt:turnover_rate:all_a",
     "desc": "全A加权换手率（%）与前5%个股成交集中度（东财clist聚合）"},
    {"id": "mkt:margin_balance",
     "desc": "两市融资融券余额合计（亿元，交易所T+1）"},
    {"id": "mkt:margin_balance:hist",
     "desc": "两融余额近10交易日序列（亿元，杠杆趋势）"},
    {"id": "mkt:north_flow",
     "desc": "北向资金日度净买额（亿元；2024-08起停披，返回停披前最后值）"},
    # 科创板/创业板板块数据（四类数据全主备冗余：腾讯/东财/AKShare/乐咕/新浪）
    {"id": "mkt:cybkcb:turnover:all",
     "desc": "创业板指/科创50/科创综指实时成交额（亿元，腾讯→东财双源；中观三市分项判风格）"},
    {"id": "mkt:cybkcb:turnover_hist",
     "desc": "三指数近60交易日成交额序列（亿元，value=创业板+科创板全板口径）"},
    {"id": "mkt:cybkcb:val:all",
     "desc": "创业板/科创板板块整体PE现值及1/3/5年/全历史分位（乐咕→东财/中证兜底）"},
    {"id": "mkt:cybkcb:spot_summary",
     "desc": "两板个股截面市场宽度：涨跌家数/上涨占比/涨跌幅中位数/成交合计/涨幅前5"},
    {"id": "idx_val:snapshot:all",
     "desc": ("6只核心宽基PE/PB及1/3/5年/全历史分位（沪深300/中证500/中证1000/"
              "上证50/创业板50走乐咕全历史，科创50走中证官网现值PE）")},
    {"id": "fed:rate_prob:next",
     "desc": "CME FedWatch下次FOMC各利率区间概率（外网不可用降级为空）"},
]

SYSTEM_PROMPT = (
    "你是投研系统的总调度Supervisor。根据用户提问，从能力目录中选择需要参与的Agent"
    "与需要采集的数据指标，生成执行计划。要求：\n"
    "1. 只从给定的Agent能力目录中选择，禁止编造未列出的Agent；\n"
    "2. 只从给定的指标目录中选择，个股类指标"
    "(stock_close/PE(TTM)/PB/资产负债率/流动比率/ROE/股息率/每股净资产 等)"
    "必须拼接6位代码后缀（如PE(TTM):300308）；\n"
    "3. 选择必须与用户问题直接相关：\n"
    "   - 问加息/降息/美联储/FOMC/利率/美债/美股宏观等→analysis_type='macro',"
    "选A08_macro + 美国宏观指标(us_cpi_yoy/us_fed_rate/us_nonfarm/us_pce等)；\n"
    "   - 问中国宏观(CPI/PPI/M2/社融)→analysis_type='macro',"
    "选A08_macro + CPI/PPI/M2/社融；\n"
    "   - 问大盘预测/明日A股/后市/复盘今日股市/行情展望→analysis_type='macro',"
    "选A08_macro；\n"
    "   - 问行业/产业链/板块交易策略/板块还能不能追/板块轮动→"
    "analysis_type='industry',选A09_meso + 对应行业Agent(A13-A16) + 行业指标 + "
    "申万行业估值(ind:sw_third_pe_ttm:all等) + 渗透率(ind:penetration:赛道名)；\n"
    "   - 问个股→选A10_micro/A11_fin_risk/A12_compliance + 个股指标(带代码后缀)；\n"
    "   - 凡问大盘预测/复盘/个股或行业的买入卖出/短线操作建议/加仓减仓/止损/"
    "仓位/持有期收益/板块交易策略/当前A股流动性或成交额/两融/北向/换手率/估值分位"
    "→除行业/个股/宏观指标外，必须同时选大盘流动性指标"
    "(mkt:turnover:total/mkt:turnover:hist/mkt:turnover_rate:all_a/"
    "mkt:margin_balance/mkt:margin_balance:hist/mkt:north_flow/"
    "idx_val:snapshot:all)与科创板/创业板中观指标"
    "(mkt:cybkcb:turnover:all/mkt:cybkcb:val:all/mkt:cybkcb:spot_summary)；"
    "问题同时涉及宏观/全球流动性、美联储、降息时加选fed:rate_prob:next；\n"
    "4. 信息层Agent(A05/A06/A07)在有新闻文本时才需要，否则不选；\n"
    "5. 输出必须为合法JSON，不要输出推理过程。"
)

_PLAN_SCHEMA = (
    '输出JSON对象，字段：\n'
    '- "analysis_type": "macro"|"industry"|"stock"|"news"|"full"\n'
    '- "target": 标的标识（个股为6位代码，宏观/行业为主题词，无则空串）\n'
    '- "agents": 参与Agent的agent_id列表（必须来自能力目录，含A17_recommend与A18_audit）\n'
    '- "indicators": 需要采集的指标id列表（必须来自指标目录）\n'
    '- "reasoning": 规划理由（50字内）'
)

#: `_PLAN_SCHEMA` 的**语法级**版本（`json_schema` 受约束解码）。
#:
#: 为什么两者都要（2026-09-28 实测）：上面那段只是**文字描述**，
#: `json_mode=True` 只保证"是 JSON"，不保证"是你要的 JSON"。规划 prompt
#: 有 2280 tokens（能力目录 + 指标目录），本地 qwen2.5:1.5b 在其上**输出会失控**：
#:
#:     同一 prompt 的三次实测：
#:       out=163   1535ms  ✅ 合法 JSON，规划成功
#:       out=1024  6566ms  ❌ 截断（撞 light 层默认输出上限）
#:       out=2048 12892ms  ❌ 截断（撞我们调大后的上限）
#:
#: 调大上限治不了 —— 它只决定"截在哪"。受约束解码从**语法**上禁止
#: 失控输出（枚举锁死 analysis_type、字符串不许逃逸），既治截断也治解析失败。
_PLAN_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "analysis_type": {
            "type": "string",
            "enum": ["macro", "industry", "stock", "news", "full"],
        },
        "target": {"type": "string"},
        "agents": {"type": "array", "items": {"type": "string"}},
        "indicators": {"type": "array", "items": {"type": "string"}},
        "reasoning": {"type": "string"},
    },
    "required": ["analysis_type", "agents"],
}


class LLMSupervisorPlanner:
    """用LLM把用户问题分解为Agent执行计划。"""

    def __init__(self, gateway: LLMGateway, *, max_agents: int = 12) -> None:
        self._gateway = gateway
        self._max_agents = max_agents

    def _build_capabilities_text(self, agents: dict[str, Any]) -> str:
        lines = []
        for aid, agent in agents.items():
            try:
                cap = agent.get_capabilities()
            except Exception:  # noqa: BLE001 规划阶段不阻断
                cap = {"capabilities": []}
            caps = cap.get("capabilities", []) if isinstance(cap, dict) else []
            lines.append(f"- {aid}: {', '.join(str(c) for c in caps) or '未声明'}")
        return "\n".join(lines)

    def _build_indicators_text(self) -> str:
        return "\n".join(f"- {i['id']}: {i['desc']}" for i in INDICATOR_CATALOG)

    async def plan(
        self,
        user_query: str,
        target: str,
        agents: dict[str, Any],
        *,
        trace_id: str = "planner",
    ) -> dict[str, Any] | None:
        """LLM规划，失败返回None（由调用方回退规则路由）。

        `trace_id`：★ 2026-09-28 第二十三轮 —— **由调用方传入任务号**。

        原先这里硬编码 `trace_id="planner"`，后果是**规划层那次调用
        永远不会出现在任何任务的「推理路径与审计」面板里**：
        面板按 `trace_id == task_id` 过滤（`GET /api/v1/trace/{trace_id}`），
        而规划记的是字面量 `"planner"` → 一条都对不上。

        实测现场：某轮 8 条记录里，用户请求**真正发生过的规划调用**
        （`planning` 层 → `qwen-dashscope-flash`）在面板上完全不可见，
        于是面板只显示 deepseek —— 看起来像"只有 deepseek 被调用"，
        而实际上是**规划那一跳没被归档到这条 trace 上**。

        默认值 `"planner"` 保留给无任务上下文的调用方（脚本/探针），
        正常链路必须传 `state["task_id"]`。

        ## ★★ 2026-09-29：**规划这一跳必须绕开缓存（`use_cache=False`）**

        真实端到端实测到的现场（`scripts/_e2e_real_llm.py` 跑两条不同问句）：
        第 2 条是**纯行业问句**「银行板块当下行情怎么样，未来一个月还有上涨空间吗？」
        （不含任何代码，`focus_stock_code` 也是空的），而它的规划结果里
        `target` 竟然是 **600036** —— 于是 A10 的结论写成「**焦点600036**本地估值
        计算为「数据不足」」，一条行业问句被当成了个股问句分析。

        根因：**语义缓存把两条不同问句的规划判成了同一条**。
        规划 prompt 的绝大部分是**静态模板**（Agent 能力目录 + 指标目录 + 任务要求），
        不同问句只占末尾一小段 ⇒ **整 prompt 相似度天然极高**
        （本项目已登记过同款：两条完全不同的资讯整 prompt 相似度 0.93，
        阈值 0.85 ⇒ 必然复用第一条答案；见
        `.trae/skills/measurement-and-attribution-discipline`）。
        而规划的输出**逐字段依赖问句**（`target` / `indicators` / `agents`），
        复用另一条问句的规划不是"省一次调用"，是**把分析焦点换成了另一只票**。

        为什么敢关掉：规划是**每请求一次**的本地/免费档调用（实测 p50 1106ms），
        关缓存省不下什么；而它错了的代价是**整条链路分析错标的**，且**不报错**。
        """
        if not user_query and not target:
            return None
        prompt = (
            f"## 用户提问\n{user_query or '（无）'}\n"
            f"## 用户指定标的/主题\n{target or '（无）'}\n\n"
            f"## Agent能力目录\n{self._build_capabilities_text(agents)}\n\n"
            f"## 可用指标目录\n{self._build_indicators_text()}\n\n"
            f"## 任务要求\n{_PLAN_SCHEMA}"
        )
        try:
            # 规划是从指标目录+Agent目录里挑子集的简单任务，light层(qwen2.5:1.5b)足够，
            # 本地 2-5s vs deepseek-flash 18s，省大头
            response = await self._gateway.complete(
                # ★ `planning` 层（2026-09-28 用户裁定）：
                # 「规划层 优先glm-4.7-flash，其次备选deepseek-flash」
                #
                # 为什么独立成层而**不复用 `medium`**：`medium` 还挂着**高频**的
                # 信息层（A05/A06），而规划是**低频 + 延迟敏感 + 任务简单**。
                # 复用会让"规划走 glm（免费/更快但 17% 429）"这个决定
                # **顺带改掉信息层的次序** —— 用户没要求的连带改动。
                #
                # 为什么规划层可以 glm 优先：暴露面小（1 次/请求），
                # 且失败一跳由 deepseek 接住，换到的是 p50 1106ms + 免费。
                # 本地模型只做第三跳兜底 —— 它显存换入会挂死 120s，绝不能靠前。
                "planning", SYSTEM_PROMPT, prompt,
                agent_id="supervisor_planner", trace_id=trace_id, json_mode=True,
                # 预算仍显式给：免费档实测 17% 429，备源要在预算内接得住
                attempt_budget_sec=_PLANNER_BUDGET_SEC,
                max_tokens=_PLANNER_MAX_TOKENS,
                # ★ 关闭思维链（2026-09-28 实测）：
                # `deepseek-flash` 是**推理模型**，思维链**计入输出预算**
                # （models.yaml 自己那条注释：3072推理+1024输出）。
                # 于是 2048 的输出上限被 CoT 吃光 → 正文截断成空串，
                # 报错却是 `LLM输出非合法JSON: Expecting value: line 1
                # column 1 (char 0)` —— 看起来像"模型不会输出"。
                #     实测：`out=2048 / 8348ms` → 解析失败
                #
                # 规划是"从能力目录+指标目录里挑子集"，**不需要多步推演**；
                # 且 A17 的同类实测显示 effort=none 更快且不丢质量
                # （5451ms vs 9987ms，正文反而更长）。
                reasoning_effort="none",
                # 语法级约束：治长 prompt 上的输出失控
                json_schema=_PLAN_JSON_SCHEMA,
                # ★★ 规划**绝不复用缓存**：语义缓存会把"两条不同问句"判成同一条
                #   （prompt 里静态模板占绝大部分），从而把另一条问句的 `target`
                #   搬过来 ⇒ 行业问句被当成个股问句分析，且**不报错**。
                #   详见本方法 docstring 的现场说明与
                #   `tests/unit/test_planner_cache_scope.py`。
                use_cache=False,
            )
            data = parse_llm_json("supervisor_planner", response.content)
        except Exception as exc:  # noqa: BLE001 LLM规划失败不阻断，回退规则
            # ⚠️ 必须**出声**：这里静默回退过一次 120s 挂死，导致
            # "链路正常但慢了 40 倍"被当成"采集慢"排查了三轮。
            # 判据：回退是允许的，**无声回退**不允许。
            logger.warning(
                "LLM 规划失败，回退规则式规划（agent=supervisor_planner, "
                "预算=%.1fs）：%s: %s",
                _PLANNER_BUDGET_SEC, type(exc).__name__, exc)
            return None
        agents_plan = data.get("agents") or []
        if not isinstance(agents_plan, list) or not agents_plan:
            return None
        # 必须包含决策与审计（否则链路不完整）
        for required in ("A17_recommend", "A18_audit"):
            if required not in agents_plan:
                agents_plan.append(required)
        # 去重保序，限制数量
        seen: list[str] = []
        for a in agents_plan:
            if a not in seen and isinstance(a, str):
                seen.append(a)
        agents_plan = seen[: self._max_agents]
        indicators = data.get("indicators") or []
        if not isinstance(indicators, list):
            indicators = []
        indicators = [str(i) for i in indicators if isinstance(i, str)][:20]
        return {
            "analysis_type": str(data.get("analysis_type", "full")),
            "target": str(data.get("target", target or "")),
            "agents": agents_plan,
            "indicators": indicators,
            "reasoning": str(data.get("reasoning", ""))[:200],
        }
