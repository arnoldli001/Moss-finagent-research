"""合规财务比率连接器：给 A12 本地规则的 6 个族补**真实生产者**。

## 为什么需要它（背景，2026-09-29）

`src/domain/agents/analysis/compliance/logic.py` 的爆雷等级**完全由本地规则算**
（`evaluate_compliance` 的返回值是权威值，LLM 不得修改）。规则消费 6 个指标族：

| 族 | 规则出处 | 阈值语义 |
|---|---|---|
| `关联交易` | `_RATIO_RULES` | 占营收 % > 30 → 普通旗标 |
| `商誉` | `_RATIO_RULES` | 占净资产 % > 30 → 普通旗标 |
| `质押` | `_RATIO_RULES` | > 80% → **严重**；> 50% → 普通 |
| `担保` | `_RATIO_RULES` | 占净资产 % > 100 → **严重**；> 50% → 普通 |
| `货币资金` | `_double_high_flag` | 占总资产 % > 40 且「有息负债」也 > 40 → 存贷双高（**严重**） |
| `有息负债` | `_double_high_flag` | 同上 |

这 6 个族在采集侧**原本一个生产者都没有**（`tests/unit/test_contract_consistency.py`
的 `_KNOWN_UNPRODUCED_RULE_FAMILIES` 就是它的机器登记），于是 4 条比率规则
+ 存贷双高**永不触发**，而 `compliance_level_calc` 会稳稳地给出「无」——
看起来像"这家公司没有合规风险"，实际是"这条规则从来没有输入"。

本连接器把其中**能真实取到**的族补上生产者。

## 指标与数据源（每个都**实测过**，见各方法 docstring 的覆盖率数字）

| 指标（规则按**子串**匹配，故名字必须含对应关键词） | 源 | 口径 |
|---|---|---|
| `商誉占净资产比:{code}` | 新浪资产负债表 | 商誉 ÷ 归母净资产 × 100 |
| `货币资金占总资产比:{code}` | 新浪资产负债表 | 货币资金 ÷ 资产总计 × 100 |
| `有息负债占总资产比:{code}` | 新浪资产负债表 | 四项有息负债合计 ÷ 资产总计 |
| `大股东质押比例:{code}` | 东财股东粒度质押明细 | 质押股数 ÷ **该股东持股数** |
| `对外担保占净资产比:{code}` | 巨潮对外担保专题 | 区间累计担保金额 ÷ 归母权益 |

`关联交易占营收比:{code}` **故意不实现、`supports()` 返回假** —— 见下节。

## ★ 纪律一：覆盖率 0% 的字段不接

`关联交易` 族在当前免费源上**取不到比率口径**，实测证据：

* `akshare 1.18.94` 里按函数名与 doc 文本搜 `关联` → **0 个接口**（`stock_cg_*`
  只有 `equity_mortgage` / `guarantee` / `lawsuit` 三个，**没有关联交易**）；
* 东财公告大全（`stock_notice_report`）确实把公告分成 `关联交易` 类型，
  但返回列只有 `代码/名称/公告标题/公告类型/公告日期/网址` —— **没有金额字段**，
  只有事件列表，算不出「占营收比」。

所以本连接器 `supports("关联交易占营收比:600036") is False`。
**接了只会把"没实现"伪装成"接口没给"**（本文件里已有的明文纪律）。

### ⚠️ 同一族还有一个**陷阱**，必须一起挡住

规则用**子串**匹配取值（`logic._find_value`：`if keyword in str(p["indicator"])`）。
所以一个叫 `关联交易公告数:{code}` 的**计数**指标会被 `_find_value(points, "关联交易")`
命中，然后被当成**百分数**去比 `> 30` —— 一年发 31 条关联交易公告的公司会被
判成"关联交易占比 31%，存在利益输送嫌疑"。**假旗标比缺数据更危险。**
因此本连接器对**任何含 `关联交易` 的指标一律不支持**（见
`tests/unit/test_compliance_fin_connector.py::test_related_party_count_trap_is_blocked`）。
要补这一族只能走"事件计数口径"，那需要**同时改规则**，属另一件事（已登记不动手）。

## ★ 纪律二：任何字段缺失 → **不产点**，绝不用 0 填充

「没量到」≠「量到 0」是本项目的硬约束（见 AGENTS.md 与
`tests/unit/test_compliance_input_provenance.py`）。本连接器逐族遵守：

1. **分母缺失 → 不产点**。实测 11 只票（3 只指定 + 8 只普通非金融）
   `资产总计` 与 `归母净资产` **11/11 有值**，所以这条不是借口。
2. **单科目分子（商誉/货币资金）该期为空 → 不产点**。
   源上"无值"与"读数为 0"不可区分，所以取**保守侧**：不产点，规则侧如实显示该族
   「未量到」。实测 `600519 贵州茅台`、`600276 恒瑞医药` 的 `商誉` 列**103 期全空**
   —— 这两家确实是内生增长、无并购商誉（旁证：同花顺模板里 `600519` **根本没有
   商誉列**），但本连接器**仍然不产 0 点**。
3. **`有息负债` 的分项另有一套判据**（为什么可以不同，见下节）。

### ⚠️ 「未量到」的正确解读（这一条决定 A12 的结论怎么念）

上面第 2 条有一个**必须写明的后果**：

> **没有商誉的公司，`商誉` 族会一直是「未量到」。**

实测 `600519 贵州茅台` 的 `商誉占净资产比:600519` 返回**空**。这不是缺陷、
也不是"数据缺了"—— 是**这家公司真的没有这条科目**（新浪列 103 期全空、
同花顺模板里根本没有该列，两个独立源一致）。

所以 A12 的「未量到」**不等于「有问题」**，它的准确含义是
**"这一族这次没有读数"**，可能来自三种完全不同的处境：

| 处境 | 例子 | 该说的话 |
|---|---|---|
| 该实体**没有这个科目** | 茅台的商誉、银行的货币资金 | 口径不适用 / 无此科目 |
| 源**这次没给**这只票 | 不在质押截面、不在担保表内 | 该源覆盖不到 |
| **取数失败** | 上游异常 | 链路故障（会抛错，不静默） |

三者都会让该族进 `compliance_families_unmeasured`，**但只有第三种是故障**。
展示层不许把 `未量到` 渲染成风险信号，也不许渲染成"已检查、无风险"
（后者正是 2026-09-29 修掉的那张伪造体检合格证）。

## ★★ 纪律三："口径不适用"必须与"接口没给"分开

用户报障里点名的 `600036 招商银行`，`货币资金` **取不到不是缺陷**：

* 接口**正常返回了整张表**（银行模板 150 列，返回 102 期）；
* 而是**这张表里根本没有「货币资金」这一行** —— 银行资产负债不划分"货币资金"，
  对应科目是「现金及存放中央银行款项」。

两者在日志里长得一样（值为空），但排查方向完全相反：前者要去改映射表，
后者要去换口径。判据是**列是否存在**（`_bs_column()` 返回 `None` = 模板无此列），
并把它写进 `extra["not_applicable"]` 随数据一起下发。

实测（新浪资产负债表，11 只票）：

| 科目 | 有值 | 全空 | **模板无此列** | 判读 |
|---|---|---|---|---|
| 商誉 | 9 | 2 | 0 | 全空的 2 只是真实的"无商誉"（不产点） |
| 货币资金 | 9 | 0 | **2** | 无列的 2 只 = `600036` / `000001` **银行，口径不适用** |
| 短期借款 | 7 | 4 | 0 | 全空 = 该科目无余额 |
| 长期借款 | 7 | 4 | 0 | 同上 |
| 应付债券 | 5 | 6 | 0 | 同上（只有发债主体有值） |
| 一年内到期的非流动负债 | 9 | 0 | **2** | 无列的 2 只 = 银行，口径不适用 |
| 资产总计 | **11** | 0 | 0 | 分母全可得 |
| 归母净资产 | **11** | 0 | 0 | 分母全可得 |

## `有息负债` 的分项为什么可以"空按 0 计入"

`有息负债` 是**四个分项的求和**，而新浪的三大报表是**固定模板**
（非金融 147 列 / 银行 150 列，**列集合与期数无关**；实测同一只票 102~120 期
列数恒定）。也就是说：

* **某分项的列不存在** → 该实体的报表模板里就没有这个科目 → **口径不适用**，
  不计入求和，记进 `extra["not_applicable"]`；
* **列在、该期为空** → 该科目该期无余额（模板固定 + 源在科目有余额时必给值）
  → 按 0 计入求和，但**逐项登记**进 `extra["components_blank"]`。

并且加一条硬约束：**至少一个分项有值才产点**。四个分项全空 = 一个数都没有，
那就是"没量到"，不许拿 4 个空值凑出一个 0。实测 11/11 满足该条件
（银行靠 `应付债券`；`600519`/`600276` 靠 `一年内到期的非流动负债`）。

### 已知的口径不完整（诚实登记，不修）

银行模板没有「一年内到期的非流动负债」列，所以**银行的有息负债口径不含该分项**。
后果有限：`600036` 的有息负债 ≈ 应付债券 1360 亿 / 总资产 13.79 万亿 ≈ **0.99%**，
而银行真实的计息负债（吸收存款/同业存放）约占资产 90%。
**但存贷双高判据要求 `货币资金` 与 `有息负债` 同时 > 40%，而银行的 `货币资金`
本身口径不适用（不产点）→ 该判据对银行永不触发**，所以这个低估不会制造假绿，
只会让银行这一族的读数偏低。已连同 `extra["basis"]` 一起下发。

## 判据：为什么每族只产**一个**点（窗口内最新一期）

规则侧 `_find_value()` 取的是**首个命中**（`for p in data_points: if keyword in ...: return`）。
如果本连接器返回多期序列，"用哪一期"就取决于上游存储/过滤的排序 ——
一个**不可见、也不可断言**的隐式依赖。所以本连接器：

* 每族只产出**窗口内最新一期**（`start_date <= period <= end_date`）的一个点；
* 于是 `len(points) == 1`，规则读到的必然是那一期，与上游排序无关；
* A12 的 6 条阈值判据全部是**时点状态**判断（"占净资产 >30%"），多期无信息增量；
  需要财务**序列**的消费者（A11 财务排雷等）走已有的
  `资产负债率:` / `ROE:` 财务比率族，不走本连接器。

`tests/unit/test_compliance_fin_connector.py` 用一条断言把这件事钉住
（`test_balance_sheet_family_returns_only_latest_period`）。

## 网络与可用性实测（2026-09-28/29 本机）

| 源 | 接口 | 通/不通 |
|---|---|---|
| 新浪 | `stock_financial_report_sina` | ✅ 通 |
| 东财 | `…pledge_ratio_detail_em`（**采用**：质押） | ✅ 通 |
| 东财 | `…pledge_ratio_em`（市场截面，**未采用**） | ✅ 通 |
| 东财 | `…individual_pledge_ratio_detail_em` | ❌ **不可用** |
| 巨潮 | `stock_cg_guarantee_cninfo`（**采用**：担保） | ✅ 通 |
| 同花顺 | `stock_financial_debt_ths` | ✅ 通但**不用** |
| 东财 | `stock_balance_sheet_by_report_em` | 未采用 |
| 巨潮 | `stock_cg_equity_mortgage_cninfo` | ✅ 通但**不用** |

逐条说明（`…` 前缀均为 `stock_gpzy`）：

* **新浪三大报表**：`600036` 102 期 / `600519` 103 期 / `000001` 120 期。
* **东财股东粒度质押明细**（采用）：实测 126,826 行 / 15 列，
  含 `占所持股份比例`（分母=**该股东持股数**）。
* **东财市场截面质押**（未采用）：周频快照 2210 行；分母是**总股本**，见下节。
* **东财个股过滤质押明细**：实测抛 `TypeError: 'NoneType' object is not
  subscriptable`，**不可用**（所以只能整表拉 + 客户端聚合）。
* **巨潮对外担保专题**（采用）：`webapi.cninfo.com.cn`，实测 3041~4143 行。
* **同花顺资产负债表**（通但不用）：值是 `'13.79万亿'` 这类**中文字符串**、
  空值是 `False`，要自写单位解析；新浪给的是原始数值，优先用新浪。
* **东财资产负债表-按报告期**：列名是 `TOTAL_PARENT_EQUITY` 这类英文枚举；
  新浪已够用，不引入第二种模板。
* **巨潮公告级质押**：只有公告级明细，分母仍是总股本，不用于本族。


## 资源上限（数字写在代码里，不留在注释里）

| 项 | 上限 | 出处 |
|---|---|---|
| 质押明细整表拉取 | **≤ 254 个分页请求 / 实测 75.2s 与 240.5s** | 接口无参数，只能整表拉 |
| 质押明细成功缓存 | **7 天**（`_PLEDGE_DETAIL_TTL`）→ 对齐 `weekly` 采集周期 | 模块常量 |
| 质押明细并发 | **单飞锁** `_pledge_holder_lock` → N 个 code 也只拉一次 | 类属性 |
| 质押明细失败冷却 | **15 分钟**（`_PLEDGE_DETAIL_FAILURE_COOLDOWN`）| 模块常量 |
| 担保截面缓存 | **6 小时**（`_CROSS_SECTION_TTL`），失败即抛不冷却 | 模块常量 |
| 各缓存条数 | 质押明细整表单条；担保截面 **≤ 8** | `_remember()` |
| 担保统计窗口 | **365 天**滚动（`_GUARANTEE_WINDOW_DAYS`） | 模块常量 |

⚠️ **anchor 必须先夹到 today**（`_end_date_or_today`）：调用方很自然地传**年窗口**
`end_date="2026-12-31"`。担保窗口用它算区间，若不夹就会去问未来区间。
（这条同时修掉了质押旧实现的一个实测缺陷：旧版以 anchor 起逐日回退找快照，
anchor 在未来时 31 天全在未来 → 每年 Q4 必然 31 连败。协作者探针实测三只票全败。）
本版质押改用**股东粒度明细**（接口无日期参数），不再有 anchor 问题。

## 两个口径决策（都有实测依据，不拍脑袋）

1. **`质押` 用股东粒度口径，不用市场截面口径** —— 这是本连接器最关键的一次选择。
   规则是 `("质押", ((80.0, …严重…), (50.0, …普通…)))`，50/80 两档是给
   **大股东持股口径**（该股东质押股数 ÷ **该股东持股数**）定的。实测：

   | 来源 | 分母 | 80% 档还开不开火 |
   |---|---|---|
   | `…pledge_ratio_em`（市场截面） | 总股本 | ❌ 全市场 `max=78.74` → **永不触发** |
   | `…pledge_ratio_detail_em` 的 `占所持股份比例` | 该股东持股数 | ✅ 828 条 / **564 只**超 80% |

   用市场截面接进来 = **又接一条永远不会开火的防线**（与刚修掉的"伪造体检合格证"
   同类）。所以选股东粒度，且指标名 `大股东质押比例` 从此**与口径一致**。

   **代价**：整表拉取（≤254 请求 / 75~240s），用 **7 天 TTL** + 单飞锁 + 失败冷却兜住。
   ✅ **已按调用方形状定过 TTL**（2026-09-29）：本指标登记为 `weekly`，而
   `catalog_jobs.plan_jobs()` 按 `frequency` 排预热作业 ⇒ TTL 取 **7 天**（≥ 采集周期），
   每周那次定时采集把缓存养住，**交互路径不会同步阻塞**。
   （原先取 24h：一周里有 6 天任何一次 A17 `query_data` → A01 → 本连接器
   都要阻塞 75~240s —— 与 AGENTS.md「禁止新增串行往返」直接冲突。）

   **两源不一致**（如实登记）：`600036` 在市场截面里有记录（0.34% / 31 笔），
   在股东明细里 **0 行**。两个东财数据集的主体范围不同；本族按规则文字要的口径
   选了股东明细，因而这三只票在本族是**未量到**。

2. **`担保` 是"区间累计"口径，不是"期末余额"口径**。规则文字是
   「对外担保/净资产X%超100%」，而源给的是**公告统计区间内累计担保金额** ÷ 归母权益。
   实测同一家公司换窗口比例就变：`000031 大悦城` 2026 YTD = 264.30%、
   2025 全年 = 366.96%、2025-2026 = 631.27% —— 证明它是**流量累计**而非时点余额。
   本连接器取**最近 12 个月滚动窗口**（该口径下仍是"近一年累计"），
   窗口与口径写进 `extra`。**方向上是"偏高"（更容易命中阈值）**，
   对爆雷判据偏保守，但**会高估严重程度**，属于必须随数据下发的局限。

担保那条不是本连接器能单方面修掉的（改口径 = 改规则语义 = 独立决策），
所以**如实登记、随数据下发**，并在交付说明里点名。
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any

from src.core import symbols
from src.core.exceptions import DataFetchError, NoApplicableData
from src.core.schemas import DataPoint, DataSourceType, FetchMethod
from src.infrastructure.connectors.base import BaseConnector

logger = logging.getLogger(__name__)

#: 指标前缀 → 口径文字（`supports()` 与 `get_capabilities()` **都从这里派生**：
#: 同一判断只允许一份实现，两份就必须能被机器对上）。
_RATIO_INDICATORS: dict[str, str] = {
    "商誉占净资产比": "商誉 ÷ 归属于母公司股东权益 × 100（%）",
    "货币资金占总资产比": "货币资金 ÷ 资产总计 × 100（%）",
    "有息负债占总资产比": (
        "(短期借款 + 长期借款 + 应付债券 + 一年内到期的非流动负债) ÷ 资产总计 × 100（%）"
    ),
    "大股东质押比例": "质押股数 ÷ 总股本 × 100（%，**全股东口径**，非大股东持股口径）",
    "对外担保占净资产比": (
        "公告统计区间内累计担保金额 ÷ 归属于母公司所有者权益 × 100（%，区间累计口径）"
    ),
}

#: 走新浪资产负债表的三个族（同一张表、同一次请求 → 合并成一次取数）。
_BALANCE_SHEET_FAMILIES: tuple[str, ...] = (
    "商誉占净资产比", "货币资金占总资产比", "有息负债占总资产比",
)

#: `supports()` 认的前缀全集（派生）。
_SUPPORTED_PREFIXES: tuple[str, ...] = tuple(f"{p}:" for p in _RATIO_INDICATORS)

#: ★ 明确**不支持**的族 → 理由。`supports()` 对它们返回假，
#: 由 `tests/unit/test_compliance_fin_connector.py` 断言（含"计数伪装成百分数"的陷阱）。
_UNSUPPORTED_FAMILIES: dict[str, str] = {
    "关联交易": (
        "比率口径在免费源上取不到：akshare 1.18.94 无任何关联交易接口（按函数名与 "
        "doc 文本搜 `关联` 命中 0 个）；东财公告大全虽有 `关联交易` 公告类型，"
        "但只有标题/日期/网址，**无金额字段**，算不出「占营收比」。"
        "⚠️ 不许退化成计数口径：规则按子串匹配取值，"
        "`关联交易公告数:{code}` 会被当成百分数去比 `> 30`（31 条公告 → 假旗标）。"
    ),
}

#: 新浪三大报表的**列名候选**（金融/非金融是**两套模板**，同一科目的列名不同）。
#: 实测（2026-09-28）：
#:   · 非金融 `600519`：`归属于母公司股东权益合计` / `货币资金` / `一年内到期的非流动负债`
#:   · 银行   `600036`：`归属于母公司股东的权益` / （无货币资金）/ （无一年内到期的非流动负债）
#: 候选按**优先序**排列，取首个存在的列；一个都不存在 → 该科目对这只票**口径不适用**。
_BS_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "goodwill": ("商誉",),
    "cash": ("货币资金",),
    "short_loan": ("短期借款",),
    "long_loan": ("长期借款",),
    "bond_payable": ("应付债券",),
    "noncur_due_1y": ("一年内到期的非流动负债",),
    "total_assets": ("资产总计", "资产合计"),
    "parent_equity": (
        "归属于母公司股东权益合计", "归属于母公司股东的权益",
        "归属于母公司所有者权益合计",
    ),
}

#: `有息负债` 的四个分项（顺序即 `extra` 里的顺序，便于对账）。
_DEBT_COMPONENTS: tuple[tuple[str, str], ...] = (
    ("short_loan", "短期借款"),
    ("long_loan", "长期借款"),
    ("bond_payable", "应付债券"),
    ("noncur_due_1y", "一年内到期的非流动负债"),
)

#: 截面向量表的进程内缓存 TTL（秒）。取数表是**全市场截面**（2000~4000 行），
#: 逐票各拉一次是纯浪费；但也不能无 TTL 常驻 —— 服务是长驻进程，会静默变陈。
_CROSS_SECTION_TTL = 6 * 3600

#: 质押**股东粒度明细**整表的进程内 TTL（秒）—— **7 天**。
#:
#: ## 为什么这么长（两层理由，第二层是 2026-09-29 补的）
#:
#: ① 该接口**没有日期/个股参数**（个股版实测抛 TypeError，不可用），
#:    只能整表拉：实测 **126,826 行 / 约 254 个分页请求 / 75.2s 与 240.5s**（两次实测）。
#:    配合 `_pledge_holder_lock` 单飞锁，并发 N 个 code 也只拉一次。
#:
#: ② ★ **24h 与"调用方是交互请求"不匹配**。原先取 24h，而
#:    `catalog_jobs.plan_jobs()` 按 `indicators.yaml` 的 **`frequency`** 排批量采集：
#:    本指标登记为 `weekly` ⇒ 预热作业**一周才跑一次**，而缓存 24h 就过期
#:    ⇒ 一周里有 6 天，任何一次交互请求（A17 `query_data` → A01 → 本连接器）
#:    都要**同步阻塞 75~240s**。AGENTS.md 的硬约束是"禁止新增串行往返"，
#:    4 分钟更不可接受。
#:    取 **7 天**后 TTL ≥ 采集周期 ⇒ 每周那次定时采集**恰好把缓存养住**，
#:    交互路径命中的是进程内缓存（或落库后的 DB 短路），不再阻塞。
#:    **代价**：质押明细最多陈 7 天。这是可接受的 —— 源本身就是
#:    "未解押记录"的台账，周级变化，7 天不会改变 50%/80% 这两档的判断。
#:    ⚠️ 若将来把本指标改成 `daily`，这个 TTL 必须同步调回 ≤ 1 天，
#:    否则"日频"就是假的（`test_registry_freshness_matches_frequency_band`
#:    管不到这里，**两者是耦合的，改一处要改两处**）。
_PLEDGE_DETAIL_TTL = 7 * 24 * 3600

#: 质押明细**拉取失败后的冷却**（秒）：15 分钟内不再重打那 254 个分页请求。
#: 为什么必须有：成功路径有 TTL，失败路径若什么都不缓存，每来一次调用就重打一遍。
_PLEDGE_DETAIL_FAILURE_COOLDOWN = 900

#: 对外担保的统计窗口长度（天）—— 最近 12 个月滚动。
_GUARANTEE_WINDOW_DAYS = 365


def _is_nan(value: Any) -> bool:
    return isinstance(value, float) and math.isnan(value)


def _to_float(raw: Any) -> float | None:
    """转 float；`None`/NaN/非数字 → None（**不是 0**）。"""
    if raw is None or _is_nan(raw):
        return None
    try:
        result = float(raw)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(result) else result


def _date_to_iso(raw: Any) -> str | None:
    """`'20260630'` / `datetime(2026,6,30)` / `'2026-06-30'` → `'2026-06-30'`。"""
    if raw is None:
        return None
    if isinstance(raw, (datetime, date)):
        return raw.strftime("%Y-%m-%d")
    text = str(raw).strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return text or None


def _ashare_symbol(code: str) -> str:
    """6 位 A 股代码 → 新浪模板需要的 `sh600036` / `sz000001`。

    北交所（`4xxxxx` / `8xxxxx`）在当前 `symbols` 里**没有映射**，
    而新浪模板也没有对应前缀 —— 如实报"不覆盖"，**不猜一个前缀硬调**。
    """
    if not (len(code) == 6 and code.isdigit()):
        raise DataFetchError(f"合规财务比率需要一个 6 位 A 股代码，收到: {code!r}")
    try:
        return symbols.exchange_symbol(code)
    except Exception as exc:  # noqa: BLE001 北交所等未覆盖市场
        raise DataFetchError(
            f"合规财务比率暂不覆盖 {code}（无法映射到新浪/东财模板的市场前缀）：{exc}"
        ) from exc


class ComplianceFinConnector(BaseConnector):
    """A12 合规规则的比率族生产者：商誉 / 货币资金 / 有息负债 / 质押 / 担保。

    ⚠️ `关联交易` 族**故意不支持**（覆盖率 0% + 计数伪装成百分数的陷阱），
    见模块 docstring 与 `_UNSUPPORTED_FAMILIES`。
    """

    source_name = "AkShare合规财务比率"
    source_url = "https://akshare.akfamily.xyz"

    #: 质押**股东粒度明细**的进程内缓存（整表派生，见 `_pledge_holder_index`）。
    #: `{"at": 时间戳, "as_of": 最新公告日, "by_code": {code: {...}}, "rows": n}`
    _pledge_holder_cache: dict[str, Any] = {}
    #: 质押明细的**失败**缓存（负缓存/熔断）：{"at": 时间戳, "reason": str}
    _pledge_holder_failure: dict[str, Any] = {}
    #: 单飞锁：并发调用只允许一个线程去拉整表（否则 N 个 code = N×254 个分页请求）
    _pledge_holder_lock = threading.Lock()
    #: 对外担保截面缓存：{"at": 时间戳, "window": (start, end), "rows": {code: row_dict}}
    _guarantee_cache: dict[str, Any] = {}

    # ------------------------------------------------------------------ 契约

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "name": self.source_name,
            "source_type": DataSourceType.API.value,
            "indicators": [f"{prefix}:{{code}}" for prefix in _RATIO_INDICATORS],
            "notes": (
                "A12 合规本地规则消费的比率族（百分数口径，名字含规则关键词）。"
                "源：新浪三大报表-资产负债表（商誉/货币资金/有息负债）、"
                "东财股权质押**股东粒度明细**（质押，口径=质押股数÷该股东持股数）、"
                "巨潮公司治理-对外担保（担保）。"
                "**每族只产窗口内最新一期一个点**（规则取首个命中，多期会让"
                "『用哪一期』依赖上游排序）。缺失一律**不产点**，绝不用 0 填充。"
                "`关联交易占营收比:{code}` 故意不支持（免费源取不到比率口径）。"
            ),
        }

    @staticmethod
    def supports(indicator: str) -> bool:
        return indicator.startswith(_SUPPORTED_PREFIXES)

    async def fetch(
        self,
        indicator: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[DataPoint]:
        """按族分派取数。上游异常**一律抛 DataFetchError**，不吞成空、更不填 0。

        返回空列表只有一种含义：**这个源这次没有这只票的读数**
        （如 `600036` 不在质押截面里 —— 它没质押记录，而"不在截面"与
        "质押比例 0%"在源上不可区分，所以不产 0 点）。
        """
        prefix, sep, code = indicator.partition(":")
        if not sep or prefix not in _RATIO_INDICATORS:
            raise DataFetchError(
                f"合规财务比率连接器不支持的指标: {indicator!r}"
                f"（支持：{[f'{p}:{{code}}' for p in _RATIO_INDICATORS]}）"
            )
        code = code.strip()

        # ETF / 基金没有个股财务报表与股东质押/担保主体资格 —— 照
        # `AkshareConnector._fin_ratio_points` 的既有纪律**显式拒绝**，
        # 不许误打个股接口产生误导性数据（那里写的是"ETF无个股财务报表"）。
        if symbols.is_etf_code(code):
            raise DataFetchError(
                f"ETF({code})无个股财务报表，合规比率族对它不适用"
                f"（商誉/货币资金/有息负债/质押/担保都以个股为口径）"
            )

        if prefix in _BALANCE_SHEET_FAMILIES:
            return await asyncio.to_thread(
                self._balance_sheet_points, prefix, code, start_date, end_date
            )
        if prefix == "大股东质押比例":
            return await asyncio.to_thread(self._pledge_points, code, end_date)
        if prefix == "对外担保占净资产比":
            return await asyncio.to_thread(self._guarantee_points, code, end_date)
        raise DataFetchError(f"合规财务比率未实现的分派: {prefix}")  # pragma: no cover

    # ------------------------------------------------------- 族 A：资产负债表

    def _balance_sheet_points(
        self, prefix: str, code: str, start_date: str | None, end_date: str | None,
    ) -> list[DataPoint]:
        """商誉 / 货币资金 / 有息负债 —— 同一张新浪资产负债表，只发一次请求。

        ## 实测覆盖（2026-09-28，11 只票 = 3 只指定 + 8 只普通非金融）

        | 科目 | 有值 | 全空 | 模板无此列 |
        |---|---|---|---|
        | 商誉 | 9 | 2（`600519`/`600276`，真实无商誉） | 0 |
        | 货币资金 | 9 | 0 | **2（`600036`/`000001` = 银行，口径不适用）** |
        | 短期借款 / 长期借款 | 7 | 4 | 0 |
        | 应付债券 | 5 | 6 | 0 |
        | 一年内到期的非流动负债 | 9 | 0 | **2（银行）** |
        | 资产总计 / 归母净资产 | **11** | 0 | 0 |

        ## 取最新一期而不是整个序列

        规则侧 `_find_value()` 取**首个命中**；返回多期会让"用哪一期"取决于上游
        排序。所以这里只取 `[start_date, end_date]` 窗口内**最新一期**。
        """
        symbol = _ashare_symbol(code)
        try:
            import akshare as ak
        except ImportError as exc:  # pragma: no cover - 依赖缺失
            raise DataFetchError(
                "akshare未安装，请执行: uv sync --extra data"
            ) from exc

        try:
            df = ak.stock_financial_report_sina(stock=symbol, symbol="资产负债表")
        except Exception as exc:  # noqa: BLE001 上游异常必须显式化，不许吞成空
            raise DataFetchError(
                f"新浪资产负债表获取失败（{symbol}）: {type(exc).__name__}: {exc}"
            ) from exc
        if df is None or len(df) == 0:
            raise DataFetchError(f"新浪资产负债表返回空表（{symbol}）")

        columns = {str(c) for c in df.columns}
        raw_periods = df[str(df.columns[0])] if str(df.columns[0]) == "报告日" else None
        if raw_periods is None:
            raise DataFetchError(
                f"新浪资产负债表缺少「报告日」列（{symbol}，实际首列="
                f"{str(df.columns[0])!r}）—— 上游模板变了，请先核对再改映射"
            )
        periods = [_date_to_iso(v) for v in raw_periods.tolist()]

        picked = self._pick_latest_row(periods, start_date, end_date)
        if picked is None:
            logger.debug("合规财务比率：%s 在窗口 %s~%s 内没有报告期",
                         symbol, start_date, end_date)
            return []
        idx, period = picked
        row = df.iloc[idx]

        # 逐科目取值 + 记录**为什么**取不到（列不存在 vs 该期为空）
        values: dict[str, float | None] = {}
        columns_hit: dict[str, str] = {}
        not_applicable: list[str] = []
        blank: list[str] = []
        for key, aliases in _BS_COLUMN_ALIASES.items():
            column = next((c for c in aliases if c in columns), None)
            if column is None:
                values[key] = None
                not_applicable.append(key)
                continue
            columns_hit[key] = column
            value = _to_float(row[column])
            values[key] = value
            if value is None:
                blank.append(key)

        common: dict[str, Any] = {
            "unit": "%",
            "frequency": "quarterly",
            "report_period": period,
            "source_columns": columns_hit,
            "not_applicable": sorted(not_applicable),
            "blank_this_period": sorted(blank),
            "periods_available": len(periods),
        }

        if prefix == "商誉占净资产比":
            return self._ratio_point(
                prefix, code, period,
                numerator=("商誉（goodwill）", values.get("goodwill"), "goodwill"),
                denominator=("归母净资产（parent_equity）",
                             values.get("parent_equity"), "parent_equity"),
                extra=common, symbol=symbol,
            )
        if prefix == "货币资金占总资产比":
            return self._ratio_point(
                prefix, code, period,
                numerator=("货币资金（cash）", values.get("cash"), "cash"),
                denominator=("资产总计（total_assets）",
                             values.get("total_assets"), "total_assets"),
                extra=common, symbol=symbol,
            )
        if prefix == "有息负债占总资产比":
            components: dict[str, float | None] = {}
            for key, label in _DEBT_COMPONENTS:
                components[label] = values.get(key)
            present = {k: v for k, v in components.items() if v is not None}
            if not present:
                logger.debug(
                    "合规财务比率：%s 的有息负债四个分项本期全空 → 不产点（不填 0）", symbol)
                return []
            total_debt = sum(float(v) for v in present.values())
            return self._ratio_point(
                prefix, code, period,
                numerator=("有息负债（四分之项合计）", total_debt, None),
                denominator=("资产总计（total_assets）",
                             values.get("total_assets"), "total_assets"),
                extra={
                    **common,
                    "components": {k: v for k, v in components.items()},
                    # 按**定义顺序**（短期借款→长期借款→应付债券→一年内到期）列出，
                    # 不用 sorted：中文按码点排序会把"一年内到期"排到最前，
                    # 人对着 `basis` 里的公式顺序读会对不上。
                    "components_counted": [
                        k for k, v in components.items() if v is not None
                    ],
                    # 列在但本期为空 → 该科目无余额，按 0 计入求和；**逐项登记**。
                    "components_zero_or_blank": [
                        k for k, v in components.items() if v is None
                    ],
                    "components_not_in_template": [
                        label for key, label in _DEBT_COMPONENTS if key in not_applicable
                    ],
                    "basis": (
                        "(短期借款+长期借款+应付债券+一年内到期的非流动负债) ÷ 资产总计；"
                        "模板内为空的分项按 0 计入（新浪三大报表是固定列模板，"
                        "科目有余额时必给值），模板里没有该列的分项口径不适用、不计入"
                    ),
                },
                symbol=symbol,
            )
        raise DataFetchError(f"资产负债表族未实现: {prefix}")  # pragma: no cover

    @staticmethod
    def _pick_latest_row(
        periods: list[str | None], start_date: str | None, end_date: str | None,
    ) -> tuple[int, str] | None:
        """窗口内最新一期 → `(行号, ISO 期间)`；窗口内无期间返回 None。"""
        best: tuple[int, str] | None = None
        for idx, period in enumerate(periods):
            if not period:
                continue
            if start_date and period < start_date:
                continue
            if end_date and period > end_date:
                continue
            if best is None or period > best[1]:
                best = (idx, period)
        return best

    def _ratio_point(
        self,
        prefix: str,
        code: str,
        period: str,
        *,
        numerator: tuple[str, float | None, str | None],
        denominator: tuple[str, float | None, str | None],
        extra: dict[str, Any],
        symbol: str,
    ) -> list[DataPoint]:
        """分子/分母都有值才产点；任一缺失 → **空列表**（不是 0，也不是占位值）。"""
        num_label, num_value, num_key = numerator
        den_label, den_value, den_key = denominator
        if num_value is None:
            logger.debug(
                "合规财务比率：%s 的 %s 本期为空 → 不产点（『无值』与『读数 0』"
                "在源上不可区分，取保守侧）", symbol, num_label)
            return []
        if den_value is None:
            logger.debug("合规财务比率：%s 的 %s 本期为空 → 不产点", symbol, den_label)
            return []
        if float(den_value) == 0.0:
            logger.debug("合规财务比率：%s 的 %s 为 0 → 比率无定义，不产点",
                         symbol, den_label)
            return []
        ratio = round(float(num_value) / float(den_value) * 100.0, 4)
        detail: dict[str, Any] = {
            **extra,
            "numerator": num_label,
            "numerator_value": float(num_value),
            "denominator": den_label,
            "denominator_value": float(den_value),
            "basis": extra.get("basis") or (
                f"{num_label} ÷ {den_label} × 100。"
                "分子或分母缺失时**不产点**，绝不用 0 填充"
            ),
        }
        if num_key and den_key:
            detail["numerator_column"] = (extra.get("source_columns") or {}).get(num_key)
            detail["denominator_column"] = (extra.get("source_columns") or {}).get(den_key)
        return [DataPoint(
            indicator=f"{prefix}:{code}",
            value=ratio,
            unit="%",
            period_date=period,
            extra=detail,
            source_name="新浪财经-财务报表-三大报表（AkShare 封装）",
            source_url=(
                f"https://vip.stock.finance.sina.com.cn/corp/go.php/"
                f"vFD_FinanceSummary/stockid/{code}.phtml?source=fzb"
            ),
            source_type=DataSourceType.API,
            # 新浪三大报表是 HTML 页面抓取（akshare 解析成 DataFrame）
            fetch_method=FetchMethod.WEB_CRAWL,
            confidence=0.85,
            verified=False,
        )]

    # ----------------------------------------------------------- 族 B：质押

    def _pledge_points(self, code: str, end_date: str | None) -> list[DataPoint]:
        """大股东质押比例 —— 东财「股权质押-股东粒度明细」的 **`占所持股份比例`**。

        ## 为什么用**股东粒度**而不是市场截面（口径决定这条防线开不开火）

        `compliance/logic.py` 的规则是：

            ("质押", ((80.0, "大股东股权质押比例{value:g}%超80%…", True),
                      (50.0, "大股东股权质押比例{value:g}%超50%…", False)))

        50/80 这两档是给**大股东持股口径**（该股东质押股数 ÷ **该股东持股数**）定的。

        | 来源 | 字段 | 分母 | 80% 档开不开火 |
        |---|---|---|---|
        | `…pledge_ratio_em`（市场截面） | `质押比例` | **总股本** | ❌ 永不触发 |
        | `…pledge_ratio_detail_em` | `占所持股份比例` | **该股东持股数** | ✅ 564 只超 80% |

        后者正是规则文字要的分母，也是父 Agent 独立复核指出的那个缺陷：
        用全股东口径接进来，等于**又接了一条永远不会开火的防线**
        （与刚修掉的"伪造体检合格证"同类）。

        ## 口径定义（写清楚，不含糊）

        `大股东质押比例:{code}` = **该股所有【状态=未解押】质押记录中，
        按出质股东聚合后，`占所持股份比例` 的最大值**。

        * 分子：该股东未解押质押股份数量合计（同股东多行 → 求和）；
        * 分母：该股东持股数（由源的 `占所持股份比例` 直接给出，**不是我们算的**）；
        * 取 `max` 的口径理由：控制权变更风险来自**质押最重的那一个股东**，
          取均值/合计会把风险抹平。
        * `extra` 里同时给出该股东名称、其质押股数占总股本、以及全部出质股东数，
          所以"这个数是谁贡献的、够不够分量"是**可复核**的。

        ## ★ 实质性（已量化，不藏）

        取 `max` 会纳入微量股东：实测 `>80%` 的 828 条里，按"质押股数占总股本"
        看中位数 **4.98%**、p25 1.88%、p05 0.31%；若额外限定"质押股数 ≥1% 总股本"，
        `>80` 的股票数从 564 降到 520（**44 只 / 7.8% 仅由微量股东贡献**）。

        本连接器**不加这个阈值** —— 那会发明一个"没有数据支撑的阈值"
        （AGENTS.md 明令禁止的假精度）。改为**把实质性随数据下发**
        （`top_holder_pct_of_total` + `materiality_note`），让规则/界面自己判断。

        ## ★ 代价（量化，写进代码）

        `stock_gpzy_pledge_ratio_detail_em()` **没有日期/个股参数**
        （个股版 `stock_gpzy_individual_pledge_ratio_detail_em(symbol=…)` 实测抛
        `TypeError: 'NoneType' object is not subscriptable`，**不可用**），
        只能整表拉 + 客户端聚合：实测 **126,826 行 / 15 列 / 约 254 个分页请求**，
        两次实测耗时 **75.2s** 与 **240.5s**（随网络波动）。

        所以这里做了三道闸门（数字都在模块常量里）：

        1. **7 天 TTL**：`_PLEDGE_DETAIL_TTL` —— 与 `weekly` 采集周期对齐（理由见该常量注释）；
        2. **单飞锁**：`_pledge_holder_lock` —— 并发 N 个 code 也只拉一次
           （否则 N × 254 个请求）；
        3. **失败冷却**：`_PLEDGE_DETAIL_FAILURE_COOLDOWN` —— 拉挂了 15 分钟内不再拉。

        ⚠️ **已知成本，需调用方知情**：本族**首次调用**最坏会阻塞约 75~240s。
        放在**定时采集**路径上没问题；若 A01 采集会被首屏/交互请求同步触发，
        请把本族改为定时任务预热（或调大 `_PLEDGE_DETAIL_TTL` 并预拉一次）。
        这是本次交付**唯一**没能在连接器内部消掉的成本，已如实登记。

        ## 不在表内 → 不产点

        该表只收录**有股东质押记录**的公司（聚合后实测 **2,406 只**有未解押记录，
        ≪ A 股约 5400 只）。实测 `600036 招商银行` / `600519 贵州茅台` /
        `000001 平安银行` 在这张**股东粒度**表里 **0 行**（三家没有需要披露的
        股东质押公告）→ **未量到**，这是正确结论，不是缺陷。

        ⚠️ 顺带登记一个**两源不一致**：同一家的市场截面
        `stock_gpzy_pledge_ratio_em` 里 `600036` **有**记录（0.34% / 31 笔），
        而股东明细里 **0 行**。两个东财数据集的口径/主体范围不同
        （截面含非股东名义持有人等）。本族按**规则文字要的口径**选了股东明细。
        """
        index = self._pledge_holder_index()
        if index is None:
            return []
        entry = index["by_code"].get(code)
        if entry is None:
            logger.info(
                "合规财务比率：%s 不在质押股东明细内（表内 %d 行 / %d 只股票有未解押"
                "记录；该表只收录有股东质押公告的公司，『不在表内』≠『质押比例 0%%』）"
                "→ 不产点", code, index["rows"], len(index["by_code"]))
            # ★ 2026-09-30（`CHG-0135`）：**抛**而不是 `return []`。
            #   原来只写日志 ⇒ 上层拿到的是"空列表"，于是用户面板上这条
            #   与"真的取数失败"长得一模一样（「未获取到 X 数据」+ 置信度低）。
            #   带 `not_covered` 标记后，`supervisor` 会把它分流成
            #   「该专题未收录本主体（非缺陷）」：不进异常面板、不联网硬试。
            raise NoApplicableData(
                f"合规财务比率：{code} 不在质押股东明细内（表内 {index['rows']} 行 / "
                f"{len(index['by_code'])} 只股票有未解押记录；『不在表内』≠『质押比例 0%』）",
                kind="not_covered")
        ratio = _to_float(entry.get("ratio_of_holding"))
        if ratio is None:
            logger.debug("合规财务比率：%s 的最重出质股东 `占所持股份比例` 为空 → 不产点",
                         code)
            return []
        return [DataPoint(
            indicator=f"大股东质押比例:{code}",
            value=float(ratio),
            unit="%",
            period_date=index["as_of"],
            extra={
                "unit": "%",
                "frequency": "event_driven",
                "as_of": index["as_of"],
                # ★ 口径三件套：字段名 / 分母 / 聚合方式，一个都不许省
                "caliber": "pledging_shareholder_ratio_of_own_holding",
                "caliber_source_column": "占所持股份比例",
                "aggregation": "max over 出质股东（仅 状态=未解押）",
                "ratio_denominator": "该股东持股数",
                "top_holder": entry.get("holder"),
                "top_holder_pledged_shares": entry.get("pledged"),
                "top_holder_pct_of_total": entry.get("pct_of_total"),
                "top_holder_latest_announce": entry.get("latest_announce"),
                "holders_pledging": entry.get("holders"),
                "pledge_rows_for_code": entry.get("rows"),
                "universe_rows": index["rows"],
                "universe_codes": len(index["by_code"]),
                "basis": (
                    "该股【未解押】质押记录按出质股东聚合后，`占所持股份比例` 的最大值；"
                    "`占所持股份比例` = 该股东质押股数 ÷ **该股东持股数**（源字段直取，"
                    "正是规则「大股东股权质押比例」要的分母）。"
                    "取 max 而非合计/均值：控制权风险来自质押最重的那一个股东"
                ),
                "materiality_note": (
                    "★ 取 max 会纳入微量股东：实测 >80% 的 828 条中，"
                    "「质押股数占总股本」中位数 4.98% / p25 1.88% / p05 0.31%；"
                    "若额外限定 ≥1% 总股本，>80 的股票数 564→520（44 只 / 7.8% "
                    "仅由微量股东贡献）。**本连接器不设这个阈值**（那会发明一个"
                    "没有数据支撑的阈值），改为把 `top_holder_pct_of_total` 随数据下发，"
                    "由消费方自行判断实质性"
                ),
                "universe_note": (
                    "该表只收录有股东质押记录的公司（实测 2,406 只有未解押记录）；"
                    "不在表内不等于质押比例 0%，故不产点。"
                    "⚠️ 与市场截面 `stock_gpzy_pledge_ratio_em` 的主体范围不同"
                    "（实测 600036 在截面里有 0.34%/31 笔，在股东明细里 0 行）"
                ),
                "cost_note": (
                    "整表拉取实测 126,826 行 / 约 254 个分页请求 / 75.2~240.5s；"
                    f"进程内 {_PLEDGE_DETAIL_TTL // 3600}h TTL + 单飞锁 + "
                    f"{_PLEDGE_DETAIL_FAILURE_COOLDOWN // 60}min 失败冷却"
                ),
            },
            source_name="东方财富-数据中心-股权质押-质押比例明细（股东粒度，AkShare 封装）",
            source_url="https://data.eastmoney.com/gpzy/pledgeRatio.aspx",
            source_type=DataSourceType.API,
            fetch_method=FetchMethod.API_CALL,
            # 口径正确（源字段直取）但聚合是我们做的 + 实质性未过滤 → 不给高置信
            confidence=0.7,
            verified=False,
        )]

    def _pledge_holder_index(self) -> dict[str, Any] | None:
        """整表拉一次 → 派生 {code: 最重出质股东}；7 天 TTL + 单飞 + 失败冷却。

        返回 `None` 只在一种情形：**接口通了，但表里没有未解押记录**。
        上游异常一律抛 `DataFetchError`（不静默变空）。
        """
        cached = self._pledge_holder_cache.get("index")
        if cached and time.time() - cached["at"] < _PLEDGE_DETAIL_TTL:
            return cached

        failure = self._pledge_holder_failure.get("last")
        if failure and time.time() - failure["at"] < _PLEDGE_DETAIL_FAILURE_COOLDOWN:
            remaining = int(
                _PLEDGE_DETAIL_FAILURE_COOLDOWN - (time.time() - failure["at"]))
            raise DataFetchError(
                f"{failure['reason']}（冷却中，还剩 {remaining}s 才重试；"
                f"本族整表拉取一次约 254 个分页请求，不做无谓重打）"
            )

        with self._pledge_holder_lock:
            # 单飞：等锁期间别人可能已经拉好了 —— 进锁后必须**再查一次**
            cached = self._pledge_holder_cache.get("index")
            if cached and time.time() - cached["at"] < _PLEDGE_DETAIL_TTL:
                return cached
            try:
                index = self._pull_pledge_holder_index()
            except DataFetchError as exc:
                self._pledge_holder_failure["last"] = {
                    "at": time.time(), "reason": str(exc)}
                raise
            if index is None:
                return None
            self._pledge_holder_cache["index"] = index
            self._pledge_holder_failure.pop("last", None)
            return index

    @staticmethod
    def _pull_pledge_holder_index() -> dict[str, Any] | None:
        """真实拉取 + 聚合（在单飞锁内执行，同一时刻只有一个线程在跑）。"""
        try:
            import akshare as ak
        except ImportError as exc:  # pragma: no cover - 依赖缺失
            raise DataFetchError(
                "akshare未安装，请执行: uv sync --extra data") from exc

        started = time.time()
        try:
            df = ak.stock_gpzy_pledge_ratio_detail_em()
        except Exception as exc:  # noqa: BLE001 上游异常必须显式化，不许吞成空
            raise DataFetchError(
                f"东财质押股东明细获取失败: {type(exc).__name__}: {exc}"
            ) from exc
        if df is None or len(df) == 0:
            raise DataFetchError("东财质押股东明细返回空表")
        needed = {"股票代码", "股东名称", "质押股份数量", "占所持股份比例",
                  "占总股本比例", "状态", "公告日期"}
        missing = needed - {str(c) for c in df.columns}
        if missing:
            raise DataFetchError(
                f"东财质押股东明细缺少列 {sorted(missing)} —— 上游模板变了，"
                f"请先核对再改聚合（实际列：{[str(c) for c in df.columns]}）"
            )

        as_of: str | None = None
        for value in df["公告日期"].tolist():
            iso = _date_to_iso(value)
            if iso and (as_of is None or iso > as_of):
                as_of = iso

        # 只保留未解押（已解押的历史记录会把"当前质押比例"算高）
        unresolved = df[df["状态"].astype(str).str.contains("未解押")]
        by_code: dict[str, dict[str, Any]] = {}
        holders: dict[tuple[str, str], dict[str, Any]] = {}
        for _, record in unresolved.iterrows():
            code = str(record.get("股票代码", "")).strip().zfill(6)
            holder = str(record.get("股东名称", "")).strip()
            if len(code) != 6 or not code.isdigit() or not holder:
                continue
            ratio = _to_float(record.get("占所持股份比例"))
            if ratio is None:
                continue
            key = (code, holder)
            bucket = holders.setdefault(key, {
                "pledged": 0.0, "ratio_of_holding": None,
                "pct_of_total": None, "latest_announce": None, "rows": 0,
            })
            shares = _to_float(record.get("质押股份数量")) or 0.0
            bucket["pledged"] += shares
            bucket["rows"] += 1
            if bucket["ratio_of_holding"] is None or ratio > bucket["ratio_of_holding"]:
                bucket["ratio_of_holding"] = ratio
            pct_total = _to_float(record.get("占总股本比例"))
            if pct_total is not None and (
                    bucket["pct_of_total"] is None or pct_total > bucket["pct_of_total"]):
                bucket["pct_of_total"] = pct_total
            announced = _date_to_iso(record.get("公告日期"))
            if announced and (bucket["latest_announce"] is None
                              or announced > bucket["latest_announce"]):
                bucket["latest_announce"] = announced

        # 每只票只留**质押最重**的那个股东（控制权风险来自它）。
        # `holders` 计数单独维护：候选在"更重"时替换 entry，但计数必须累加，
        # 否则 extra 里的 `holders_pledging` 会随替换被重置成 1（错）。
        for (code, holder), bucket in holders.items():
            candidate = {**bucket, "holder": holder}
            entry = by_code.get(code)
            if entry is None:
                candidate["holders"] = 1
                by_code[code] = candidate
                continue
            entry["holders"] = int(entry.get("holders", 1)) + 1
            if (candidate["ratio_of_holding"] or -1.0) > (
                    entry["ratio_of_holding"] or -1.0):
                candidate["holders"] = entry["holders"]
                by_code[code] = candidate
        if not by_code:
            logger.info("合规财务比率：质押股东明细里没有未解押记录（%d 行原始）",
                        len(df))
            return None
        logger.info(
            "合规财务比率：质押股东明细聚合完成 —— %d 行原始 / %d 只有未解押记录 / "
            "as_of=%s / 耗时 %.1fs", len(df), len(by_code), as_of, time.time() - started)
        return {"at": time.time(), "as_of": as_of or date.today().strftime("%Y-%m-%d"),
                "by_code": by_code, "rows": len(df)}

    # ----------------------------------------------------------- 族 C：担保

    def _guarantee_points(self, code: str, end_date: str | None) -> list[DataPoint]:
        """对外担保占净资产比 —— 巨潮「公司治理-对外担保」专题统计。

        ## 接口与字段（实测）

        `ak.stock_cg_guarantee_cninfo(symbol="全部", start_date, end_date)` 返回
        `证券代码/证券简称/公告统计区间/担保笔数/担保金额/归属于母公司所有者权益/`
        `担保金融占净资产比例`（最后一列是 akshare 里的错别字，语义是
        **担保金额占净资产比例**）。实测窗口 `20260101~20260928` = 3106 行、
        `20250101~20251231` = 3212 行。

        **比例列自证**：`担保金额 / 归属于母公司所有者权益 × 100` 与源给的比例列
        最大偏差 **0.005**（纯四舍五入），所以它不是另一个口径的估算，就是这两列算的。
        而且分母**正是「归属于母公司所有者权益」** —— 与规则要的「净资产」同源。

        ## ★ 口径差异（必须随数据下发）

        源给的是**公告统计区间内累计担保金额**，**不是期末担保余额**。实测同一家公司
        换窗口比例就变：`000031 大悦城` 2026YTD = **264.30%**、2025 全年 = **366.96%**、
        2025-2026 = **631.27%**。所以本连接器取**最近 12 个月滚动窗口**，
        并把窗口与口径写进 `extra`。方向上偏高（更容易命中 >50%/>100% 阈值），
        对爆雷判据偏保守，但**会高估严重程度** —— 这是已知局限，不藏。

        ## 不在表内 → 不产点

        该表只收录**窗口内有担保公告**的公司（实测 3000+ 行 ≪ A 股约 5400 只）。
        实测 `600036` / `600519` / `000001` 在四个窗口里**都命中 0 行**
        —— 它们没有对外担保公告。"无公告"与"担保为 0"在源上不可区分，不产 0 点。
        """
        anchor = _end_date_or_today(end_date)
        window = self._guarantee_window(anchor)
        table = self._guarantee_snapshot(window)
        if table is None:
            logger.debug("合规财务比率：对外担保表为空（窗口 %s）", window)
            return []
        row = table.get(code)
        if row is None:
            # ★ "空结果"必须是**可解释**的：把窗口与表体量打出来，
            #   这样"这只票不在表内"与"整张表是空的（接口静默失败）"一眼可分。
            logger.info(
                "合规财务比率：%s 不在对外担保表内（窗口 %s~%s，表内 %d 只；"
                "该专题统计只收录窗口内有对外担保公告的公司，"
                "『不在表内』≠『担保为 0%%』）→ 不产点",
                code, window[0], window[1], len(table),
            )
            # ★ 同质押那处（`CHG-0135`）：抛带标记的 `not_covered`，
            #   让上层能把它与"取数失败"分开（用户面板三种情形不再同一句话）。
            raise NoApplicableData(
                f"合规财务比率：{code} 不在对外担保表内（窗口 {window[0]}~{window[1]}，"
                f"表内 {len(table)} 只；该专题只收录窗口内有对外担保公告的公司，"
                "『不在表内』≠『担保为 0%』）",
                kind="not_covered")
        ratio = _to_float(row.get("担保金融占净资产比例"))
        if ratio is None:
            logger.debug("合规财务比率：%s 的担保占净资产比例为空 → 不产点", code)
            return []
        # period_date 必须是 ISO（`DataPoint.period_date` 契约），
        # 而窗口是 `YYYYMMDD` —— 直接把 `window[1]` 塞进去会产出 `20260929`
        # 这种非 ISO 值（实测踩过）。
        window_end_iso = _date_to_iso(window[1])
        return [DataPoint(
            indicator=f"对外担保占净资产比:{code}",
            value=float(ratio),
            unit="%",
            period_date=window_end_iso,
            extra={
                "unit": "%",
                "frequency": "rolling_12m",
                "window_start": _date_to_iso(window[0]),
                "window_end": window_end_iso,
                "announce_window": row.get("公告统计区间"),
                "guarantee_count": _to_float(row.get("担保笔数")),
                "guarantee_amount_wan": _to_float(row.get("担保金额")),
                "parent_equity_wan": _to_float(row.get("归属于母公司所有者权益")),
                "basis": (
                    "公告统计区间内**累计**担保金额 ÷ 归属于母公司所有者权益 × 100"
                    "（源字段直取；自证 = 两列相除，偏差 <0.005）。"
                    "⚠️ 是**区间累计**而非期末担保余额：实测同一家公司 "
                    "2026YTD=264.30% / 2025=366.96% / 2025-2026=631.27%"
                    " → 会偏高，可能高估严重程度"
                ),
                "universe_note": (
                    "该表只收录统计区间内有担保公告的公司；"
                    "不在表内不等于担保为 0%，故不产点"
                ),
            },
            source_name="巨潮资讯-数据中心-专题统计-公司治理-对外担保（AkShare 封装）",
            source_url="https://webapi.cninfo.com.cn/#/thematicStatistics",
            source_type=DataSourceType.API,
            fetch_method=FetchMethod.API_CALL,
            confidence=0.6,
            verified=False,
        )]

    @staticmethod
    def _guarantee_window(anchor: str) -> tuple[str, str]:
        """最近 12 个月滚动窗口 → `(YYYYMMDD, YYYYMMDD)`。"""
        end = datetime.strptime(anchor, "%Y-%m-%d").date()
        start = end - timedelta(days=_GUARANTEE_WINDOW_DAYS)
        return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")

    def _guarantee_snapshot(
        self, window: tuple[str, str],
    ) -> dict[str, dict[str, Any]] | None:
        """窗口 → {code: row}；进程内缓存（键含窗口，不同窗口不许共用）。"""
        key = f"{window[0]}-{window[1]}"
        cached = self._guarantee_cache.get(key)
        if cached and time.time() - cached["at"] < _CROSS_SECTION_TTL:
            return cached["rows"]

        import akshare as ak

        try:
            df = ak.stock_cg_guarantee_cninfo(
                symbol="全部", start_date=window[0], end_date=window[1])
        except Exception as exc:  # noqa: BLE001 上游异常显式化
            raise DataFetchError(
                f"巨潮对外担保获取失败（窗口 {window[0]}~{window[1]}）: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if df is None or len(df) == 0:
            return None
        rows: dict[str, dict[str, Any]] = {}
        for _, record in df.iterrows():
            code = str(record.get("证券代码", "")).strip().zfill(6)
            if len(code) != 6 or not code.isdigit():
                continue
            rows[code] = {
                "担保笔数": record.get("担保笔数"),
                "担保金额": record.get("担保金额"),
                "归属于母公司所有者权益": record.get("归属于母公司所有者权益"),
                "担保金融占净资产比例": record.get("担保金融占净资产比例"),
                "公告统计区间": record.get("公告统计区间"),
            }
        self._guarantee_cache[key] = {"at": time.time(), "rows": rows}
        logger.info("合规财务比率：对外担保表命中 %d 只（窗口 %s~%s）",
                    len(rows), window[0], window[1])
        return rows

    # ------------------------------------------------------------------ 工具

    @classmethod
    def reset_cache(cls) -> None:
        """清空进程内截面缓存（**测试用**：monkeypatch 上游后必须能拿到干净状态）。"""
        cls._pledge_holder_cache.clear()
        cls._pledge_holder_failure.clear()
        cls._guarantee_cache.clear()


def _end_date_or_today(end_date: str | None) -> str:
    """`end_date` → `YYYY-MM-DD`；缺省 today。**并且夹到 today**（不许取未来）。

    ## 为什么要夹（实测缺陷，2026-09-29 修）

    调用方很自然地传**年窗口** `end_date="2026-12-31"`（planner / SmartFetcher /
    协作者探针都这么传）。如果拿它当"最近快照"的搜索起点，回退出来的日期全在
    **未来**，而未来必然没有快照 —— 于是这一族在 Q4 每次调用都必然 31 连败。
    这是设计缺陷，不是数据源问题：**任何"以某日为起点往前找"的逻辑，
    起点都必须先夹到当天**。夹完 Q4 与其它季度行为一致。
    """
    today = date.today().strftime("%Y-%m-%d")
    iso: str | None = None
    if end_date:
        candidate = _date_to_iso(end_date)
        if candidate and len(candidate) == 10:
            iso = candidate
    if iso is None:
        return today
    return min(iso, today)
