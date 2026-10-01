"""★★★ 白名单一致性护栏：**数据在库里 ⇏ Agent 看得见**。

## 这个文件存在的理由（2026-09-28 真实报障）

用户问「预测下一年美国的加息、降息节奏」，前端显示：
    「缺少联邦基金利率数据，无法判定方向」
而后台库里 `fed:policy_range` 有 3 条（2026-09-27 的 3.75/4.00）、
`us_fed_rate` 有 72 条、`us_unemployment` / `us_nonfarm` 各 107 条。

**根因**：`_AGENT_DATA_WHITELIST["A08_macro"]` 写的是**中文标签**
（`"非农"` `"失业率"` `"利率决议"`）和**产品名**（`"FedWatch"`），
而库里存的是 **en_id**（`us_nonfarm` / `us_fed_rate` / `fed:policy_range`）。
匹配是**子串**，`"非农" ⊄ "us_nonfarm"` → **一条都放不过去**。

实测（pilot 库 719 种指标）：A08 白名单 22 词 → **只放行 4 种**。
`us_fed_rate` `fed:policy_range` `us_nonfarm` `us_unemployment` `us_pce`
全部被挡在门外 —— A08 拿着残缺数据报了"缺少联邦基金利率"。

## 为什么既有测试没抓到（本文件要补的那一格）

`tests/unit/test_indicator_prefix_wiring.py` 验的是**正向**：
「我在 `LIVE_PREFIXES` 里声明了 `cal:` → 每个 Agent 都得放行它、都要教 prompt」。
它**永远不会**发现「库里有个指标，谁的白名单都不放行」——
因为没人**声明**它。方向是反的。

本文件补三向：
  ① **登记 → 放行**：`configs/indicators.yaml` 每个指标至少一个 Agent 看得见
  ② **声明 → 放行**：Agent 自己声明的（`watch_keywords` / `system_prompt`
     声称会读的指标族）必须真的能被白名单放行
  ③ **白名单 → 登记**：白名单关键词必须真实存在（无**幻影词**）

## 判据纪律（照 AGENTS.md）

**判据只认机器可读的标识**：指标 id、`watch_keywords` 元组、前缀常量 ——
不认自然语言。所以 ② 只对**显式声明**的族生效（见 `AGENT_REQUIRED_FAMILIES`），
不做"读 prompt 猜语义"这种会漂移的事。
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml

from src.orchestration.supervisor import (
    _AGENT_DATA_WHITELIST,
    _filter_points_for_agent,
)

#: 仓库根（`tests/unit/xxx.py` → parents[2]）
_ROOT = Path(__file__).resolve().parents[2]

#: 行业 Agent 的类路径（用于读它们**自己的** `watch_keywords`）
_INDUSTRY_AGENT_CLASSES: dict[str, str] = {
    "A13_tech": "src.domain.agents.industry.tech.agent:TechIndustryAgent",
    "A14_consumer":
        "src.domain.agents.industry.consumer.agent:ConsumerIndustryAgent",
    "A15_cyclical":
        "src.domain.agents.industry.cyclical.agent:CyclicalIndustryAgent",
    "A16_pharma":
        "src.domain.agents.industry.pharma.agent:PharmaIndustryAgent",
}

#: ★ 每个 Agent **显式声明**必须能读到的指标族（判据词，不是自然语言）。
#:
#: 每一项的来源都必须能在该 Agent 的 `system_prompt` 或
#: `INDICATOR_CATALOG`/`_PLANNING` 里指出来 —— 见每项的注释。
#: 加新项前先问："这个 Agent 真的声称要读它吗？" 没声称就别加（那是扩权，不是修复）。
AGENT_REQUIRED_FAMILIES: dict[str, tuple[str, ...]] = {
    # macro/agent.py 的 prompt 原文：
    #   "美国(CPI/核心CPI/非农/失业率/联邦利率/PCE)数据点"
    "A08_macro": (
        "us_cpi_yoy", "us_core_cpi", "us_nonfarm", "us_unemployment",
        "us_pce", "us_fed_rate",
        # FRED 政策利率族（`fed:policy_range` / `fed:target_upper|lower` / `fed:effr`）
        "fed:policy_range",
        # 中国端（prompt："中国(CPI/PPI/M2/社融)"）
        "CPI", "PPI", "M2", "社融",
    ),
    # meso/agent.py 的 prompt 原文：
    #   "先看两市总量阶段→三市成交额占比判风格→双创PE分位判冷热→再定板块轮动"
    "A09_meso": (
        "mkt:turnover:total", "mkt:cybkcb:turnover:all", "mkt:cybkcb:val:all",
        "idx_val:snapshot:all",
        "ind:sw_third_pe_ttm:all",
    ),
}

#: 允许"关键词在某处无对应登记指标"的白名单条目（**显式登记，带理由**）。
#:
#: ## 为什么需要它，以及为什么**故意做得很难变长**
#:
#: 白名单里有两类"匹配不到任何登记指标"的词，性质**完全不同**：
#:
#:   A. **缺陷**（会导致静默丢数据）：数据源已登记（如 `us_fed_rate`），
#:      而白名单只写了中文标签（`"非农"`）→ 一条都放不过去。
#:      这类由 `test_agent_declared_families_are_passable` 和
#:      `test_registered_ids_in_agent_domain_stay_passable` **盯死**，不进本表。
#:
#:   B. **意图声明**（数据源本机还没有，登记了也不过是空跑）：如 A12 的
#:      `"诉讼"` / `"问询函"` —— 库里根本没有这类指标可匹配，
#:      但保留它们表达了"这类数据该进 A12"的设计意图。**这类是本表的内容。**
#:
#: ⚠️ 本项目实测过的教训：**豁免表一旦没有过期检查，就退化成永久豁免**。
#: 所以本表只登记 B 类，并且配一条**规模上限**断言
#: （`test_phantom_keyword_count_is_bounded`）——
#: 想往这张表里加词，就必须先回答"为什么它不是 A 类缺陷"。
_ALLOWED_PHANTOM_PREFIXES: tuple[str, ...] = (
    # A08：数据源本机未接线（indicators.yaml 未登记 us_* 以外的这些宏观项）
    #
    # ★ 2026-09-29：`"A08_macro:GDP"` / `"A08_macro:PMI"` **已按本文件自己的过期
    #   检查删除** —— `macro_extra_connector.MacroExtraConnector` 提供了生产者
    #   （实测 `supports('PMI')` / `supports('GDP')` 全认），`indicators.yaml`
    #   随之补登记 → 这两个关键词不再"匹配不到任何登记指标"，豁免到期。
    #   `A08_macro:GDP同比` **也已删除**（2026-09-29 当日）：它的存在本身就是
    #   **口径漂移的证据**——白名单写不带冒号的 `"GDP同比"`，而登记的 id 是
    #   `GDP:同比`（带冒号），子串匹配不到 ⇒ 一条都放不过去。
    #   修法是**改白名单**（把 `"GDP同比"` 改成 `"GDP:同比"`），不是留着豁免
    #   —— 留着它等于把"我放行了 GDP 同比"这句假话登记成合法状态。
    #   ⚠️ 教训：**同一指标两种写法 = 幻影关键词**，肉眼看不出来，
    #   只有"关键词必须能在 indicators.yaml 里匹配到"这条判据能抓。
    "A08_macro:新增贷款",
    "A08_macro:十年国债", "A08_macro:美元指数", "A08_macro:美债",
    "A08_macro:美元兑人民币",
    # A08：中文别名/产品名（en_id 才是命中判据，见同文件的族断言）
    "A08_macro:FedWatch", "A08_macro:非农", "A08_macro:失业率",
    "A08_macro:利率决议", "A08_macro:通胀", "A08_macro:货币政策",
    "A08_macro:CPI同比", "A08_macro:PPI同比",
    "A08_macro:宏观",
    # A09：行业级指标的**备用命名**（本机当前用 ind:sw_* 那套）
    "A09_meso:ind_", "A09_meso:bk_", "A09_meso:industry_",
    "A09_meso:industry_pe", "A09_meso:industry_amount",
    "A09_meso:industry_pct", "A09_meso:估值分位",
    # A10：行情类字段（库里有 stock_close:* 等，这些是**冗余表达**）
    #
    # ★ 2026-09-28 第二十五轮：删掉 4 条**已过期**的条目 ——
    #   `A10_micro:总市值` / `:流通市值` / `:换手率` / `:市值`。
    #   它们由本文件 `test_allowlisted_phantoms_have_no_registered_hit` 的
    #   过期检查**主动报出**（`indicators.yaml` 本轮补登记了
    #   `总市值:{code}` / `流通市值:{code}` / `换手率:{code}`，
    #   而 `市值` 作为子串同时命中了前两条）—— 这正是那张表的设计意图：
    #   一旦能匹配到登记指标，豁免就必须删掉，不许退化成永久豁免。
    "A10_micro:PCF", "A10_micro:stock_turnover:",
    "A10_micro:涨跌幅", "A10_micro:成交额",
    # A11：财务比率 —— ★ 2026-09-28 第二十四轮：`ROE`/`ROA`/`应收账款`/`净利率`
    #   四条豁免**已被本文件的过期检查逼出并删除**：连接器补了真实取数、
    #   indicators.yaml 补了登记，它们不再匹配不到任何指标。
    #   保留的下面这些是**本机确实还没有数据源**的口径。
    "A11_fin_risk:净负债率", "A11_fin_risk:扣非净利润",
    "A11_fin_risk:经营性现金流",
    "A11_fin_risk:财务费用", "A11_fin_risk:杠杆",
    "A11_fin_risk:debt_ratio",
    # ↑ ★ 2026-09-29：删掉 `A11_fin_risk:商誉` 与 `A11_fin_risk:有息负债` ——
    #   本文件的过期检查**主动报出**它们已能匹配到登记指标
    #   （`商誉占净资产比:{code}` / `有息负债占总资产比:{code}`，
    #    由 `ComplianceFinConnector` 生产）。这正是那张表的设计意图：
    #   一旦能匹配到登记指标，豁免必须删掉，不许退化成永久豁免。
    # A12：合规事件类 —— 走 `extracted_events`（信息层 A06）而不是 data_points
    "A12_compliance:公告", "A12_compliance:处罚", "A12_compliance:诉讼",
    "A12_compliance:关联交易", "A12_compliance:减持", "A12_compliance:增持",
    "A12_compliance:回购", "A12_compliance:冻结",
    "A12_compliance:违规", "A12_compliance:监管",
    "A12_compliance:问询函", "A12_compliance:关注函",
    "A12_compliance:defense", "A12_compliance:compliance",
    # ↑ ★ 2026-09-29：同样被过期检查逼出并删掉 `A12_compliance:担保` /
    #   `A12_compliance:质押` —— `对外担保占净资产比:{code}` 与
    #   `大股东质押比例:{code}` 已登记，两个词不再是幻影。
    #   保留的上面这些是**事件类**：它们走 `extracted_events` 而不是 `data_points`，
    #   本机没有对应的时序指标前缀，属"意图声明"而非缺口。
    # A13-A16：申万**行业名**级指标（本机用 ind:sw_* 截面替代，尚未逐行业采）
    "A13_tech:sw_tech", "A13_tech:sw_电子", "A13_tech:sw_计算机",
    "A13_tech:sw_通信", "A13_tech:算力",
    "A14_consumer:sw_consumer", "A14_consumer:sw_食品",
    "A14_consumer:sw_纺织", "A14_consumer:sw_商业", "A14_consumer:家电",
    "A15_cyclical:sw_cyclical", "A15_cyclical:sw_煤炭",
    "A15_cyclical:sw_有色", "A15_cyclical:sw_钢铁", "A15_cyclical:sw_化工",
    "A15_cyclical:有色",
    "A16_pharma:sw_pharma", "A16_pharma:sw_医药", "A16_pharma:sw_生物",
    "A16_pharma:器械",
    # 四个行业 Agent 共用的通用词
    #
    # ★ 2026-09-29：`A13_tech:板块` / `A14_consumer:板块` / `A15_cyclical:板块` /
    #   `A16_pharma:板块` 四条豁免**已被本文件自己的过期检查逼出并删除** ——
    #   本轮登记了 `板块资金流:{行业名}`，于是 `"板块"` 不再是幻影词
    #   （它现在能匹配到真实登记指标）。这正是这张表的设计意图：
    #   一旦能匹配到登记指标，豁免就必须删掉，不许退化成永久豁免。
)

#: 幻影关键词**总数上限**。加词之前先问：它是 A 类缺陷吗？
#: 是 → 去修白名单；不是 → 才允许进表并抬高这个数字（一次一行，强制留痕）。
#:
#: ★ 2026-09-29：`90 → 85`。本轮登记 `板块资金流:{行业名}` 后，
#:   `板块` 不再是幻影词 → 5 条 `*:板块` 豁免被过期检查逼出并删除
#:   （删了 5 条，预算跟着收紧；实测现行幻影 **68 条**，85 仍然宽松）。
#:   ⚠️ 预算**只许往下调**，不许为了让红灯变绿而抬回来。
_PHANTOM_BUDGET: int = 85

#: Agent 语义域 → **候选**指标族前缀（可多个 Agent 共享一个域）。
#:
#: 判据是**前缀**（机器可读），不是自然语言。想给某个 Agent 加一族，
#: 必须同时能说出"它的 prompt 里哪句话要读这一族"。
#:
#: ⚠️ 这里是**候选**而不是**独占**：行业 Agent 之间可以互看
#: （A13 拿得到煤炭指标不构成错误 —— 多一个维度比少一个维度安全）。
#: 所以断言写在"这一族**至少有一个**候选 Agent 放行"，见
#: `test_registered_ids_in_agent_domain_stay_passable`。
_AGENT_DOMAIN_PREFIXES: dict[str, tuple[str, ...]] = {
    # 宏观：中国端短名 + 美国 en_id + FRED 政策利率 + 日历
    #
    # ★ 2026-09-29：补 `"PMI"` / `"GDP"`。依据不是"顺手加"，是**白名单字面量**：
    #   `_AGENT_DATA_WHITELIST["A08_macro"]` 一直写着 `"PMI"` / `"GDP"` / `"GDP同比"`
    #   （旧注释还写「通胀/利率/汇率/GDP/货币政策」），而本表原先漏登记 →
    #   `test_every_registered_indicator_has_a_candidate_owner` 会判"域外无人认领"。
    #   ⚠️ 与上面几条同一个纪律：**只登记 `passers` 里真实出现过的 Agent**。
    #   实测（复刻 `_filter_points_for_agent` 子串语义）：`'PMI'` / `'GDP'` 的
    #   passers 都是 `['A08_macro']`。
    "A08_macro": ("CPI", "PPI", "M2", "社融", "PMI", "GDP",
                  "us_", "fed:", "cal:",
                  # ★ 2026-09-29：**`fred:` 通用序列前缀**（`FredConnector`）。
                  #   实测 `passers`：'fred:UNRATE' / 'fred:DGS10' → ['A08_macro']。
                  #   与 `fed:` **不是同一个**（后者是政策利率三条单值序列）。
                  "fred:"),
    # 中观：大盘/板块流动性 + 申万与行业估值截面
    #
    # ★ 2026-09-29：补**行业侧三族**（平台自有后端数据）。
    #   实测 `passers`（复刻 `_filter_points_for_agent` 子串语义）：
    #     '行业拥挤度:银行' / '板块资金流:银行' / '行业轮动:银行'
    #       → passers=['A09_meso','A13_tech','A14_consumer','A15_cyclical',
    #                  'A16_pharma','A20_generic_industry']
    #   依据是用户原话（「用户输入内容中含有行业的，要连接到概念板块拥挤度的数据表…
    #   也可以接入板块资金流…问到当下和未来近期行情的，可以接入行业轮动日报」），
    #   而 A09 是中观风格/轮动 Agent、A13-A16/A20 是行业 Agent —— 三族都归它们。
    "A09_meso": ("mkt:", "idx_val:", "ind:sw_", "ind:", "cal:",
                 "行业拥挤度", "板块资金流", "行业轮动"),
    # 微观个股：行情与估值（模板族 `PE(TTM):{code}` 由 _CODE_SUFFIX_INDICATORS 展开）
    #
    # ★ 2026-09-28 第二十五轮：补 3 条**共享行情仓列族**（`quant_daily_basic`）。
    #   本表原先漏登记，而 `supervisor._AGENT_DATA_WHITELIST["A10_micro"]`
    #   **早就写着它们** —— 即"登记表过期"，不是判据在拦。
    #   实测（复刻 `_filter_points_for_agent` 的真实子串语义）：
    #     '总市值:600036'   passers=['A10_micro','A11_fin_risk']
    #     '流通市值:600036' passers=['A10_micro','A11_fin_risk']
    #     '换手率:600036'   passers=['A10_micro','A11_fin_risk']
    #   ⚠️ 只写 `passers` 里**真实出现**的 Agent。想往里加一条就必须先量
    #      `passers` —— 给一个"没有 Agent 真的看得见"的前缀在这里登记，
    #      等于把 `indicators.yaml` 的僵尸登记合法化（本项目在 `FEATURES`
    #      上踩过同款：把 `metrics` 移出后连管理员都看不到）。
    "A10_micro": ("stock_", "PE(TTM)", "PB", "cal:",
                  "总市值", "流通市值", "换手率",
                  # ★ 2026-09-29：平台自有后端数据的个股五族（`PlatformDataConnector`）。
                  #   实测 `passers`：
                  #     '估值水位:600036' / '概念拥挤度:600036' / '解禁计划:600036'
                  #       → ['A10_micro']（A11 按用户/父 agent 的口径只拿告警两族）
                  #     '主线告警:600036' / '个股告警:600036'
                  #       → ['A10_micro','A11_fin_risk']
                  "估值水位", "概念拥挤度", "主线告警", "个股告警", "解禁计划"),
    # 财务风险：**已登记**的财务比率族
    # ⚠️ 只列 indicators.yaml 里真实存在的那些。
    # ★ 2026-09-28 第二十四轮：补入连接器已实现取数的 20 个财入口径
    #   （此前只登记了「资产负债率/流动比率/速动比率/存货周转」四个，
    #    而连接器的 `_FIN_RATIO_INDICATORS` 有 22 个 —— **登记与实现脱节**，
    #    于是 ROE/股息率 这类用户直接问的口径"数据源可达却取不到"）。
    #    本测试在这一轮**主动报错**逼出了这次补齐，这就是它的价值。
    "A11_fin_risk": (
        "资产负债率", "流动比率", "速动比率", "存货周转", "cal:",
        # ★ 2026-09-29：**银行报表口径**（用户点名的「银行息差」的公式输入）。
        #   实测 `passers`（复刻 `_filter_points_for_agent` 的子串语义）：
        #     '利息净收入:600036' / '利息收入:600036' / '利息支出:600036' /
        #     '总资产:600036' / '净息差:600036' → passers=['A11_fin_risk']
        #   归 A11 的依据：净息差 = 利息净收入 ÷ 生息资产平均余额，是**盈利能力/
        #   财务风险**的读法（A10 看估值、A12 看合规事件）。
        #   生产者：`BankStatementConnector`（新浪财务分析报表模板）。
        "利息净收入", "利息收入", "利息支出", "总资产", "净息差",
        # —— 第二十四轮补入（与 `akshare_connector._FIN_RATIO_INDICATORS` 对齐）——
        "产权比率", "ROE", "ROE加权", "ROA", "销售净利率", "成本费用利润率",
        "应收账款周转率", "总资产周转率", "净利润增长率", "总资产增长率",
        "净资产增长率", "营收增长率", "EPS", "EPS加权", "每股净资产",
        "每股经营现金流", "每股未分配利润", "每股资本公积",
        # 合成口径（分红 ÷ 收盘价，见 `_DERIVED_STOCK_INDICATORS`）
        "股息率",
        # ★ 第二十五轮：共享行情仓列族（`akshare_connector._QUANT_COLUMN_INDICATORS`）。
        #   `股息率TTM:{code}` 已被上面的 `股息率` 覆盖（前缀匹配）；
        #   下面 5 条实测 `passers`：
        #     '总市值:600036' / '流通市值:600036' / '换手率:600036'
        #         → passers=['A10_micro','A11_fin_risk']
        #     '量比:600036' / '市销率:600036'
        #         → passers=['A11_fin_risk']   ← **只有 A11**，别顺手加到 A10
        "总市值", "流通市值", "换手率", "量比", "市销率",
        # ★ 2026-09-29：平台自有数据里与**财务风险**直接相关的两族
        #   （`解禁计划:{code}` 不含 `cal:` 前缀，必须单独放行；
        #    实测 passers：'解禁计划:600036' → ['A11_fin_risk']）。
        #   ⚠️ `主线告警` / `个股告警` 由 A10 与 A11 **共同**放行，
        #      所以它们登记在 A10_micro 那一项里（本表是**候选**域，可共享）。
        "解禁计划",
    ),
    # 合规：**本地规则直接消费的比率族**（`compliance/logic.py`）
    #
    # ★ 2026-09-29：新增本域。生产者 `ComplianceFinConnector` 落地后，
    #   `商誉占净资产比` / `货币资金占总资产比` / `有息负债占总资产比` /
    #   `大股东质押比例` / `对外担保占净资产比` 五条登记进 `indicators.yaml`，
    #   本表原先**根本没有 A12 这一项** → 它们"落在所有 Agent 的语义域之外"。
    #
    # ⚠️ 两条纪律都守住了：
    #   ① 前缀取自 `indicators.yaml` 里**真实登记的 id 前缀**（不是概念词）——
    #      `大股东质押` / `对外担保` 是完整前缀，不能简写成 `质押`/`担保`：
    #      本表的认领判据是 `id.startswith(prefix)`，简写会**一条都认领不到**
    #      （而视觉上"看起来覆盖了"，是本项目最防的那种假绿）。
    #   ② 与之配对的 `_AGENT_DATA_WHITELIST["A12_compliance"]` 已同步补
    #      `商誉`/`货币资金`/`有息负债`（`担保`/`质押` 原本就在）——
    #      `test_registered_ids_in_agent_domain_stay_passable` 会逐个验证
    #      "我域内的登记指标我真的拿得到"，所以两边必须一起改。
    "A12_compliance": (
        "商誉", "货币资金", "有息负债", "大股东质押", "对外担保",
    ),
    # 行业层：申万截面 + 渗透率 + 本行业产业指标
    #
    # ★ 2026-09-29：补**行业侧三族**（平台自有后端数据，按行业名取数）。
    #   实测 `passers`：三族的 passers 都是这 5 个行业 Agent + A09
    #   （白名单里按族名前缀放行，所以每个行业 Agent 都拿得到自己行业的行）。
    #   ⚠️ 与 A20 的差别：A20 是**兜底**行业 Agent，域前缀必须同样含这三族，
    #      否则 `test_every_registered_indicator_has_a_candidate_owner`
    #      会因为"兜底 Agent 的域覆盖不到"而红。
    "A13_tech": ("ind:", "cal:", "行业拥挤度", "板块资金流", "行业轮动"),
    "A14_consumer": ("ind:", "cal:", "行业拥挤度", "板块资金流", "行业轮动"),
    "A15_cyclical": ("ind:", "cal:", "行业拥挤度", "板块资金流", "行业轮动"),
    "A16_pharma": ("ind:", "cal:", "行业拥挤度", "板块资金流", "行业轮动"),
    "A20_generic_industry": ("ind:", "cal:",
                             "行业拥挤度", "板块资金流", "行业轮动"),
}

#: **跨域例外**：这些指标虽落在某 Agent 的前缀域内，但**故意不给它**。
#: 显式登记，避免"静默不覆盖"。
_CROSS_DOMAIN_EXEMPT: frozenset[str] = frozenset()


# ============================================================
# 工具
# ============================================================


def _registered_ids() -> list[str]:
    """`configs/indicators.yaml` 里登记的全部指标 id（现读，不硬编码）。"""
    path = _ROOT / "configs" / "indicators.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    out: list[str] = []

    def walk(node) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            if isinstance(node.get("id"), str):
                out.append(node["id"])
            for value in node.values():
                walk(value)

    walk(raw)
    return sorted(set(out))


def _passes(agent_id: str, indicator: str) -> bool:
    """复刻 `_filter_points_for_agent` 的匹配语义（子串、大小写宽松）。"""
    kws = _AGENT_DATA_WHITELIST.get(agent_id, ())
    kws_lower = tuple(str(k).lower() for k in kws)
    low = indicator.lower()
    return any(k in low or k in indicator for k in kws_lower)


def _any_agent_passes(indicator: str) -> bool:
    return any(_passes(aid, indicator) for aid in _AGENT_DATA_WHITELIST)


# ============================================================
# ① 登记 → 放行：库里有，就得有人看得见
# ============================================================


def test_every_registered_indicator_is_visible_to_some_agent():
    """★ 每个登记指标至少被一个 Agent 白名单放行。

    红了说明：数据在跑、落库了、索引登记了，**但没有任何 Agent 看得见** ——
    这正是 2026-09-28 报障的形状（`fed:policy_range` / `us_fed_rate` 就这样
    在库里躺了几天没人发现）。

    修法二选一（**必须显式选择，不许沉默**）：
      · 该指标确实有消费者 → 把它的 id/前缀加进对应 Agent 的白名单；
      · 该指标确实没有消费者 → 从 `configs/indicators.yaml` 摘掉登记
        （登记了却没人用，只会让下一次排查多一个假线索）。
    """
    ids = _registered_ids()
    assert ids, "indicators.yaml 一个指标都没读到 —— 路径或结构变了"

    orphans = [i for i in ids if not _any_agent_passes(i)]
    assert not orphans, (
        "以下登记指标**没有任何 Agent 的白名单放行**"
        "（数据到了但 Agent 看不见）：\n"
        + "\n".join(f"  · {i}" for i in orphans)
        + "\n→ 要么把它的 id/前缀加进对应 Agent 的白名单，"
        "要么从 indicators.yaml 摘掉登记（并在 PRD/台账登记该决定）。"
    )


@pytest.mark.parametrize("agent_id", sorted(AGENT_REQUIRED_FAMILIES))
def test_agent_declared_families_are_passable(agent_id: str):
    """★★ Agent **自己声明**要读的指标族，白名单必须真的放行。

    这是本文件最核心的一条：A08 的 prompt 白纸黑字写着会读"非农/失业率/
    联邦利率/PCE"，而白名单一条都放不过去 —— **声明与实现脱节**，
    且故障方向是"Agent 拿着残缺数据给出自信结论"，**不报错**。
    """
    missing = [ind for ind in AGENT_REQUIRED_FAMILIES[agent_id]
               if not _passes(agent_id, ind)]
    assert not missing, (
        f"{agent_id} 声明会读以下指标，但 `_AGENT_DATA_WHITELIST` 一条都不放行：\n"
        + "\n".join(f"  · {ind}" for ind in missing)
        + f"\n当前白名单：{_AGENT_DATA_WHITELIST.get(agent_id, ())}\n"
        "→ 补白名单。⚠️ 优先补 **en_id**（库里实际存的名字），"
        "中文标签匹配不上 en_id。"
    )


@pytest.mark.parametrize("agent_id", sorted(_INDUSTRY_AGENT_CLASSES))
def test_industry_watch_keywords_survive_whitelist(agent_id: str):
    """★ 行业 Agent 的 `watch_keywords` 必须能穿过白名单（否则等于自废武功）。

    ## 为什么这条必须有（实测过的静默故障）

    行业 Agent 有**两层**过滤：
      ① supervisor 的 `_AGENT_DATA_WHITELIST`（**先执行**）
      ② Agent 自己的 `_watched_points()` 按 `watch_keywords`

    白名单在 ① 就把数据删了，② 永远看不到 → `watched_indicator_count == 0`
    → `base.py::_skip_reason()` 判"无本行业关注指标" → **跳过 LLM**、
    `skipped=True`、置信度 low。

    **界面上看起来像"这个行业没什么可分析的"**，而不是"数据被挡了"。
    实测：A13 声明 `芯片/出货量`，而 `ind:芯片出货量同比` 被白名单挡掉。

    判据：任一 `watch_keyword` 若能匹配到**登记指标**，则该 Agent 必须至少
    放行其中一个（不能让"声明了、库里有、却一条都进不来"发生）。
    """
    mp, cls = _INDUSTRY_AGENT_CLASSES[agent_id].split(":")
    watch = tuple(getattr(importlib.import_module(mp), cls).watch_keywords)
    assert watch, f"{agent_id} 没有 watch_keywords，测试失去意义"

    ids = _registered_ids()
    dead: list[str] = []
    for kw in watch:
        matched = [i for i in ids if str(kw).lower() in i.lower() or kw in i]
        if matched and not any(_passes(agent_id, i) for i in matched):
            dead.append(f"{kw}（匹配到 {matched}，但白名单一条都不放行）")

    assert not dead, (
        f"{agent_id} 的 watch_keywords 里有词**完全无法生效**：\n"
        + "\n".join(f"  · {d}" for d in dead)
        + f"\n当前白名单：{_AGENT_DATA_WHITELIST.get(agent_id, ())}\n"
        "→ 补白名单（`_derive_whitelist_gaps.py` 可自动推导这份清单）。\n"
        "⚠️ 不补的后果不是报错，是行业 Agent 静默 skipped。"
    )


# ============================================================
# ② 白名单 → 登记：域内已登记的指标，必须真的可达
# ============================================================


@pytest.mark.parametrize("agent_id", sorted(_AGENT_DOMAIN_PREFIXES))
def test_registered_ids_in_agent_domain_stay_passable(agent_id: str):
    """★★★ 落在某 Agent **语义域**内的登记指标，必须被它放行。

    ## 这是本文件里真正防复发的那一条

    A08 报障的机制是：`configs/indicators.yaml` 登记了 `us_fed_rate` /
    `fed:policy_range`（数据在采、在落库、在更新），而 A08 的白名单里
    **只有中文标签**（`"非农"` `"利率决议"`）→ 一条都放不过去。

    这个测试不问"白名单写了什么"，而是问
    **"登记表里落在这个 Agent 域内的指标，它到底能不能拿到？"** ——
    方向是反的，所以它能抓到正向测试永远抓不到的那类漏配
    （`test_indicator_prefix_wiring.py` 只验"我声明的前缀有没有贯通"，
    对"没人声明过的指标"完全无感）。

    ## 判据

    对每个 Agent 的域前缀（如 A08 的 `us_` / `fed:`）：
    `indicators.yaml` 里以它开头的指标，必须 `_passes(agent_id, id) == True`。
    `_CROSS_DOMAIN_EXEMPT` 里显式登记的例外不算。
    """
    ids = _registered_ids()
    prefixes = _AGENT_DOMAIN_PREFIXES[agent_id]
    in_domain = [
        i for i in ids
        if any(i.lower().startswith(p.lower()) for p in prefixes)
        and i not in _CROSS_DOMAIN_EXEMPT
    ]
    if not in_domain:
        pytest.skip(f"{agent_id} 的域前缀 {prefixes} 在本机没有登记指标")

    unreachable = [i for i in in_domain if not _passes(agent_id, i)]
    assert not unreachable, (
        f"{agent_id} 的语义域（前缀 {prefixes}）里有 **{len(unreachable)} 个"
        f"已登记指标拿不到**：\n"
        + "\n".join(f"  · {i}" for i in unreachable)
        + f"\n当前白名单：{_AGENT_DATA_WHITELIST.get(agent_id, ())}\n"
        "→ 数据在采、在落库，但 Agent 看不见，且**不报错**。\n"
        "→ 两条路：① 补白名单前缀；② 若确实不该给它，"
        "加进 `_CROSS_DOMAIN_EXEMPT` 并说明由谁消费。"
    )


def test_every_registered_indicator_has_a_candidate_owner():
    """★ 每个登记指标都必须落在**至少一个** Agent 的候选域里，且该候选真的放行它。

    ## 与上一条的分工

    · 上一条 `test_registered_ids_in_agent_domain_stay_passable`
      逐个 Agent 检查"我的域内指标我放行了吗"。
    · 这一条查**反向**：有没有指标**谁的域都不在**（= 新采一族数据没人认领，
      它会进库、进索引，然后在分析层**无声消失**）。

    判据用**候选域并集**而不是"唯一归属" —— 行业 Agent 之间刻意互看
    （A13 拿到煤炭指标不是错误），所以不做独占断言，只查"有没有人管"。
    """
    ids = _registered_ids()
    all_prefixes = tuple(
        p for ps in _AGENT_DOMAIN_PREFIXES.values() for p in ps)
    unclaimed = [
        i for i in ids
        if not any(i.lower().startswith(p.lower()) for p in all_prefixes)
        and i not in _CROSS_DOMAIN_EXEMPT
    ]
    assert not unclaimed, (
        "以下登记指标**落在所有 Agent 的语义域之外**（没人认领）：\n"
        + "\n".join(f"  · {i}" for i in unclaimed)
        + "\n→ 在 `_AGENT_DOMAIN_PREFIXES` 里认领给某个 Agent"
        "（并确认它的 prompt 真的会读这一族），或从 indicators.yaml 摘掉登记。\n"
        "⚠️ `_CROSS_DOMAIN_EXEMPT` 只用于『故意不给』的显式例外，不许当垃圾桶。"
    )


def test_domain_prefixes_are_not_phantom():
    """域前缀本身必须能在 `indicators.yaml` 里匹配到东西（否则是空域）。"""
    ids = _registered_ids()
    dead: list[str] = []
    for aid, prefixes in _AGENT_DOMAIN_PREFIXES.items():
        for p in prefixes:
            if not any(i.lower().startswith(p.lower()) for i in ids):
                dead.append(f"{aid}:{p}")
    assert not dead, (
        "以下语义域前缀在 indicators.yaml 里匹配不到任何指标"
        "（域是空的，等于没认领）：\n"
        + "\n".join(f"  · {d}" for d in dead)
    )


def test_phantom_keyword_count_is_bounded():
    """★ 幻影关键词（匹配不到任何登记指标）的**规模上限**。

    ## 为什么用"总数上限"而不是"逐条理由"

    实测本仓库有 **86 条**这类词，绝大多数是**意图声明**（本机还没有那个
    数据源，如 A12 的 `"诉讼"`）而不是缺陷 —— 给 86 条各写一句理由是
    噪音，不是护栏。

    真正会致命的那一类（**数据源已登记、白名单只写中文标签**）
    已被 `test_registered_ids_in_agent_domain_stay_passable` 单独盯死。

    所以这里只守"**别让这张表继续长**"：数量超预算就必须停下来
    回答"新加的这个是不是 A 类缺陷"。要加词 → 同时抬高
    `_PHANTOM_BUDGET`，**一次一行，强制留痕**。
    """
    ids = _registered_ids()
    allowed = set(_ALLOWED_PHANTOM_PREFIXES)
    phantoms = []
    for aid, kws in _AGENT_DATA_WHITELIST.items():
        for kw in kws:
            if any(str(kw).lower() in i.lower() or kw in i for i in ids):
                continue
            phantoms.append(f"{aid}:{kw}")

    unexpected = sorted(set(phantoms) - allowed)
    assert len(phantoms) <= _PHANTOM_BUDGET, (
        f"幻影关键词 {len(phantoms)} 条 > 预算 {_PHANTOM_BUDGET} 条。\n"
        "先判断新加的是不是「数据源已登记、白名单却匹配不上」那类**缺陷**：\n"
        "  · 是 → 修白名单（补 en_id/前缀），**不要**加进豁免表\n"
        "  · 否 → 加进 `_ALLOWED_PHANTOM_PREFIXES` 并抬高 `_PHANTOM_BUDGET`\n"
        f"预算外的条目：\n" + "\n".join(f"  · {p}" for p in unexpected[:20])
    )


def test_allowlisted_phantoms_have_no_registered_hit():
    """★ 豁免必须带**过期检查**：某条一旦能匹配到登记指标，就必须删掉。

    没有这条，"已知在飞项"会退化成**永久豁免** ——
    即"数据源补齐了，但白名单的豁免还留着，没人去把中文标签换成 en_id"。
    """
    ids = _registered_ids()
    stale: list[str] = []
    for key in _ALLOWED_PHANTOM_PREFIXES:
        aid, _, kw = key.partition(":")
        if aid not in _AGENT_DATA_WHITELIST:
            stale.append(f"{key}（Agent 已不存在于白名单）")
            continue
        if kw not in _AGENT_DATA_WHITELIST[aid]:
            stale.append(f"{key}（该关键词已从白名单移除）")
            continue
        hit = [i for i in ids if kw.lower() in i.lower() or kw in i]
        if hit:
            stale.append(f"{key}（现在能匹配到 {hit}）")
    assert not stale, (
        "以下豁免条目**已过期**，请从 `_ALLOWED_PHANTOM_PREFIXES` 删除"
        "（并确认白名单已补上真正的 en_id）：\n"
        + "\n".join(f"  · {s}" for s in stale)
    )


# ============================================================
# ③ 行为层：报障现场的原样复现（回归测试）
# ============================================================


def test_regression_fed_rate_reaches_a08_macro():
    """★★★ 报障原始现场回归：联邦基金利率/目标区间必须能进 A08。

    2026-09-28 用户问「预测下一年美国的加息、降息节奏」，
    A08 输出「缺少联邦基金利率数据，无法判定方向」——
    而库里明明有 `fed:policy_range`（FRED）与 `us_fed_rate`（AkShare）。

    这条测试直接构造那次的 payload 形状（含大量噪声点），断言：
    `fed:policy_range` / `us_fed_rate` / `us_unemployment` / `us_nonfarm`
    **全部**穿过白名单。修好之前它是红的。
    """
    points = [
        {"indicator": "fed:policy_range", "value": 3.75, "period_date": "2026-09-27"},
        {"indicator": "fed:policy_range", "value": 4.00, "period_date": "2026-09-27"},
        {"indicator": "us_fed_rate", "value": 4.5, "period_date": "2025-07-31"},
        {"indicator": "us_unemployment", "value": 4.2, "period_date": "2025-08-01"},
        {"indicator": "us_nonfarm", "value": 7.3, "period_date": "2025-08-01"},
        {"indicator": "us_pce", "value": 2.6, "period_date": "2025-08-29"},
        {"indicator": "us_cpi_yoy", "value": 3.4, "period_date": "2026-08-01"},
        {"indicator": "CPI", "value": 0.8, "period_date": "2026-09-01"},
        {"indicator": "PPI", "value": 3.8, "period_date": "2026-09-01"},
        # 噪声：真实 payload 里绝大多数是个股指标
        *[{"indicator": f"PE(TTM):{i:06d}", "value": 20.0,
           "period_date": "2026-09-27"} for i in range(300)],
    ]
    kept = _filter_points_for_agent("A08_macro", points)
    kept_inds = {str(p["indicator"]) for p in kept}

    required = {"fed:policy_range", "us_fed_rate", "us_unemployment",
                "us_nonfarm", "us_pce", "us_cpi_yoy", "CPI", "PPI"}
    missing = required - kept_inds
    assert not missing, (
        f"A08 仍然拿不到这些指标：{sorted(missing)}\n"
        "用户问『美国加息降息节奏』时，A08 会继续报『缺少联邦基金利率数据』。"
    )
    # 顺带断言：个股噪声**不该**进 A08（白名单不能形同虚设）
    assert not any(i.startswith("PE(TTM):") for i in kept_inds), (
        "个股估值指标漏进了 A08 的 payload —— 白名单过滤失效（token 会爆炸）"
    )


def test_regression_market_liquidity_reaches_a09_meso():
    """A09 的 prompt 声称要判"三市成交额占比/双创PE分位" → 必须真的拿得到。

    `plan_run` 对 industry 类型**无条件**追加这些指标，白名单却挡掉 →
    A09 的核心风格判断输入归零，且**不报错**。
    """
    points = [
        {"indicator": "mkt:turnover:total", "value": 21000.0,
         "period_date": "2026-09-28"},
        {"indicator": "mkt:cybkcb:turnover:all", "value": 3200.0,
         "period_date": "2026-09-28"},
        {"indicator": "mkt:cybkcb:val:all", "value": 45.0,
         "period_date": "2026-09-28"},
        {"indicator": "idx_val:snapshot:all", "value": 60.0,
         "period_date": "2026-09-28"},
        {"indicator": "ind:sw_third_pe_ttm:all", "value": 28.0,
         "period_date": "2026-09-28"},
        *[{"indicator": f"stock_close:{i:06d}", "value": 10.0,
           "period_date": "2026-09-28"} for i in range(200)],
    ]
    kept = _filter_points_for_agent("A09_meso", points)
    kept_inds = {str(p["indicator"]) for p in kept}
    required = {"mkt:turnover:total", "mkt:cybkcb:turnover:all",
                "mkt:cybkcb:val:all", "idx_val:snapshot:all",
                "ind:sw_third_pe_ttm:all"}
    missing = required - kept_inds
    assert not missing, f"A09 拿不到这些指标：{sorted(missing)}"


def test_regression_industry_agent_gets_its_own_industry_data():
    """★ 行业 Agent 报障形状回归：A13 必须拿得到半导体/渗透率数据。

    修好之前：`ind:芯片出货量同比` / `ind:sw_*` / `ind:penetration:*`
    全被白名单挡掉 → A13 的 `_skip_reason()` 判"无本行业关注指标" →
    **静默 skipped**，用户看到"科技行业没什么可分析的"。
    """
    points = [
        {"indicator": "ind:芯片出货量同比", "value": 12.0,
         "period_date": "2026-07-01"},
        {"indicator": "ind:半导体销售额同比", "value": 18.0,
         "period_date": "2026-07-01"},
        {"indicator": "ind:sw_third_pe_ttm:all", "value": 55.0,
         "period_date": "2026-09-28"},
        {"indicator": "ind:penetration:AI大模型应用", "value": 32.0,
         "period_date": "2026-06-30"},
        *[{"indicator": f"PE(TTM):{i:06d}", "value": 20.0,
           "period_date": "2026-09-27"} for i in range(200)],
    ]
    kept = _filter_points_for_agent("A13_tech", points)
    kept_inds = {str(p["indicator"]) for p in kept}
    required = {"ind:芯片出货量同比", "ind:半导体销售额同比",
                "ind:sw_third_pe_ttm:all", "ind:penetration:AI大模型应用"}
    missing = required - kept_inds
    assert not missing, (
        f"A13 拿不到这些指标：{sorted(missing)}\n"
        "→ `industry/base.py::_skip_reason()` 会判『无本行业关注指标』"
        "并**跳过 LLM**，界面显示为『没什么可分析的』。"
    )


def test_whitelist_still_filters_noise():
    """反向测试：修完白名单不能变成"谁都拿全部数据"（token 护栏仍在）。

    这是 `test_no_agent_silently_gets_everything` 的行为版补充 ——
    那一条只查关键词长度，这一条查**真实过滤效果**。
    """
    noise = [{"indicator": f"totally_unrelated_{i}", "value": 1.0,
              "period_date": "2026-09-28"} for i in range(400)]
    for aid in _AGENT_DATA_WHITELIST:
        kept = _filter_points_for_agent(aid, noise)
        # 兜底分支会返回前 200 条（已知设计），但**不能**是"全量"
        assert len(kept) < len(noise), (
            f"{aid} 把 {len(noise)} 条无关指标全放行了 —— 白名单形同虚设"
        )
