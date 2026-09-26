"""原文倾向抽取（P1 第二步）—— 对象是**第三方原文的语气**，不是平台判断。

对应设计：`docs/INTEL_CENTER_REDESIGN.md` §0.3（约束 3）、§5.1。

## ⚠️ 这个模块的输出**不是**我们的观点

字段名 `tone` 的含义严格限定为：**这条第三方原文自己是什么语气**。
界面上必须显示为「原文倾向」，且**永远**与一个可核对的依据（原文词组）
一起出现 —— 用户能自己判断这个归类对不对。

## 用户口径（2026-09-25）

> "原则上有幻觉风险的可以不显示数值，可信度低的也不做倾向分析。"

两条都落在这里：

  1. **可信度低的（< `MIN_CREDIBILITY_FOR_TONE`）不做倾向分析** ——
     抽错了也没人会发现，而用户会把它当成平台的判断
  2. **有幻觉风险的项不显示数值** —— `confidence` 只在
     **规则层与模型层一致**时给出；不一致时 `tone="未定"`，
     界面上不显示倾向标签

## 实测的幻觉（所以才有拦截）

本地 `qwen2.5:1.5b` 在 5 条样本上出了 4 类错，全部实测：

| 幻觉 | 实例 |
|---|---|
| 把 JSON 模板当答案抄回 | `codes: ["6位代码"]` |
| 空串占位 | `codes: [""]` |
| 标点被改写（不再逐字） | `phrases: ["据传,未经证实"]`（原文是全角逗号） |
| **判定相反** | 「中标12.5亿元订单，机构上调盈利预测」被判 `中性`（规则层 4 个偏多词） |

最后一条最危险：**不是抽取失败，是判断错了**。所以判据不能只看模型 ——
必须与规则层交叉验证。

## 拦截策略

    tone 层  取模型与规则的**交集**；不一致 → "未定"（不显示倾向）
    phrases  逐字子串校验，**丢弃所有**非逐字的（不做模糊匹配 ——
             模糊匹配会把"改写"也放进来，那就失去了"可核对"的意义）
    codes    必须是**原文中出现的 6 位数字**。这是合规要求（设计稿"抽取的
             股票代码必须能在原文找到依据"），顺带挡掉 `["6位代码"]`
    confidence  只在两层一致时给值，且取两者较小值（保守）

## 扩展（2026-09-25 第二轮）：一句话摘要 + 利好/利空的行业与个股 + 关键事件

知识星球的调研笔记 1000~3000+ 字，用户要的不是"这条什么语气"，而是
**"利好了哪个行业、哪只票，因为什么事"**。所以在同一次模型调用里多要四样：

    summary     ≤ `MAX_SUMMARY_CHARS` 的核心摘要（供信息密度参考）
    bullish     利好行业 / 利好个股
    bearish     利空行业 / 利空个股
    events      对应的关键事件（短句）

### ⚠️ 为什么是"同一次调用"而不是加一次调用

本地模型单条实测 ~770ms。情报流一页 60 条 —— **在请求路径里调模型
就是 +46 秒**（`tone_store` 模块 docstring 记的正是这个失败）。所以：

  · 抽取只能在 `tone_job`（2 小时一次）里做，结果落 `tone_store`，
    接口按 `content_hash` O(1) 读。**这条不能被"顺手优化"掉**。
  · 一次调用同时出全部字段：两者读的是同一段文本、走同一次前向，
    分成两次只是把延迟翻倍。

### ⚠️ 新字段同样是**原文逐字**的（比参考实现更严）

参考实现（`moss-finance-assistant`）只做"整段研报里 find 到就算"，
本项目**不接受**这种宽松度。理由与 `phrases` 完全一样：
界面上要能拿这个词组去原文里核对，核不到的等于给了用户一条假证据。

所以逐个字段都要过子串校验，**过不了就丢那个字段/那一条**，
绝不"补全"、绝不模糊匹配：

    industries   必须逐字出现在原文里
    stocks[].name  必须逐字出现在原文里（`stocks[].code` 另需原文里真有）
    stocks[].code  必须在原文里真出现（复用 `_CODE_RE`，不是随便 6 位数字）
    summary/events 必须与原文有**最长公共子串 ≥ `MIN_COMMON_CHARS`** ——
                 这类字段允许模型压缩措辞（"同比+30%" → "增长三成"），
                 所以不能要求整串逐字；但"原文里一个字都没有"的句子
                 同样是编的，一样丢

### ⚠️ 券商名/团队名必须跳过（实测最高频的一类错）

模型极易把**发布研报的券商团队**当成股票：`【天风电子】` 看起来
完全像一只票。参考实现在提示词里列了名单，这里照抄并**同时做成硬拦截**——
提示词是"请求"，拦截才是"保证"。详见 `_BROKER_SKIP`。

### 这里的 `tone` **仍然是** `偏多|偏空|中性`

不是 `利好|利空`。利好/利空的语义活在 `bullish` / `bearish` 两个桶里，
`tone` 是"原文语气"、被既有契约与情报流消费，改词表会让既有读取方
全部失配。`to_public()` 是白名单，新字段必须显式加进去才出接口。

## 扩展（2026-09-25 第三轮）：实体**查本地词表**，短文本**不走模型**

第二轮是"让模型抽实体 + 逐字校验"。实测下来**逐字校验不够**：

    · 模型会给出原文里根本没有的 6 位代码（一条连数字都没有的笔记，
      它输出了 `"codes":["603919"]`）—— 逐字校验能挡住代码，
      但挡不住"名字逐字在原文里、代码是它配上去的"那种错
    · 行业名会飘（"新能源车"、"光模块龙头"这种"听起来就该在这段里"的词）

所以第三轮把**实体解析**从"信模型 + 校验"改成"**查项目自己的名录**"：

    概念板块  只认 `ml_board` 里 `kind='concept'` 的 138 个板块名，
              且必须**逐字出现在原文里**（`vocab.boards_in_text`）
    个股      名字必须逐字在原文里，**代码只从词表取**，模型给的代码
              一律不采用（`vocab.stock_code`）

这样情报流的"概念板块"与**主线挖掘模块跟踪的板块**是同一份名字，
两边能对上 —— 用户口径："概念板块要和主线挖掘跟踪的板块对齐。"

同一轮还加了一条**成本/风险**规则：`≤ MIN_CHARS_FOR_EXTRACTION` 的
短文本**不调模型**（原文本身就是摘要），倾向走 `rule_tone`、
实体走词表扫描（都是零模型成本的规则层）。

## 扩展（2026-09-25 第四轮）：长文**分段多次调用** + 确定性合并

第三轮之后，"两头各 300 字"（`_EXTRACT_GAP` 那套）成了实体召回率的**硬天花板**。
实测（本地 8B，qwen3:8b-q4_K_M）一条 3452 字的《碳化硅材料专题会议》：

    两头窗口 591 字 → 实体字段**全空**
    而 `天岳先进`（688234）与 `第三代半导体` 就在被切掉的中间 ~2500 字里

模型没有失败，是**它根本没看见那段原文** —— 而"找出利好了哪个行业、哪只票"
恰恰是这次抽取存在的唯一目的。所以第四轮把单次调用改成
**按句边界切段 + 每段一次调用 + 确定性合并**：

    ≤ EXTRACT_MAX_CHARS        一段（就是第三轮的行为，一次调用）
    更长的原文                 按句边界切成 ≤600 字的连续段
    段数上限 MAX_EXTRACT_SEGMENTS   超过则取**前 N-1 段 + 最后一段**（6 段，
                              实测定的：最近 30 条样本里 6 段覆盖 100%）

### ⚠️ 为什么"每段用**它自己那段原文**校验"是正确性要求，不是优化

`原文逐字` 校验（phrases / 行业 / 个股 / 事件 / 摘要）必须拿**模型真正看过的那段
文本**来跑。若拿整篇原文去校验第 2 段的输出，第 3 段里的词就能"通过"第 2 段 ——
那等于接受了一句**模型不可能读到**的引文，而界面上它会显示成"原文逐字可核对"。
本模块存在的全部意义就是让用户能拿它去原文里核对；放宽这一条即等于取消这条纪律。

### ⚠️ 合并规则是**确定性**的（不引入任何"权重"发明）

    events / phrases    并集，按**逐字相等**去重，首现顺序，各取既有上限
    行业 / 个股          并集，按 (名字, 代码) 去重；代码仍然**只从词表取**
    tone                逐段投票，多数取胜；**平票 → 未定**（不发明加权方案）
    confidence          **取最小值**（保守：段数与不确定性只会增加，不会减少）
    summary             逐段摘要用 `；` 连接；超过 MAX_SUMMARY_CHARS 就**只留
                        第 1 段**（它是导语）；**绝不无界拼接** —— 摘要的全部
                        价值就在于"短到能一眼扫完"

`≤ MIN_CHARS_FOR_EXTRACTION`（100 字）的短文本**一次模型调用都不发生**，
这条第三轮的规则原样保留。

调用仍然**只能**发生在 `tone_job`（见那个模块的 docstring）：
本地推理免费但慢，抽完落 `tone_store`，接口按 `content_hash` O(1) 读。

## 扩展（2026-10-01 第五轮）：券商名 / 分析师名，以及"机构名不能当股票"

用户口径：

> "建议直接让本地模型输出时，直接输出：相关个股/板块 + 事件一句话摘要
>  （如果有以下内容必须要输出：股票名 板块名 券商 孙潇雅、赵宇阳、武超则、
>   陈果、刘晨明、洪灏），推送到前端展示。"
> "券商名字不一定带券商两个字，比如可能是天风电子，招商电子。"

两件事必须分开看，否则会把"请求"当成"保证"：

    **展示保证**（模型漏了也不会漏显示）
        股票名 + 板块名   `vocab.scan`（词表扫描，与高亮同一份）
        券商              `alert_rules` 的两种署名形态
                          （`XX证券` / `简称+研究语境后缀`，用户追加的那一条）
        分析师名          `alert_rules.ANALYST_WATCHLIST`（用户点名的六人）
    合并与补齐在 `service.build_feed`（清洗后全文再扫一遍、取并集）

    **模型职责**（本轮新加的两个字段）
        一句话事件摘要 + 方向 + 这些标的属于哪一侧（既有）
        `brokers` / `analysts`（新）：逐字校验后落 `tone_store`，**只作审计**

⚠️ 所以本轮在校验层新增的是"把机构名/分析师名**挡在个股之外**"
（`_is_research_house`），而不是"靠模型给全" —— 后者做不到，
而做不到的表现恰恰是这次要修的"漏掉谁在唱多/唱空"。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Final

from src.domain.intel.credibility import MIN_CREDIBILITY_FOR_TONE

logger = logging.getLogger(__name__)

#: 倾向取值。`未定` 是**一等公民**而不是"失败" ——
#: 两层不一致时它就是正确答案：我们确实不知道原文什么语气。
TONE_BULL: Final = "偏多"
TONE_BEAR: Final = "偏空"
TONE_NEUTRAL: Final = "中性"
TONE_UNKNOWN: Final = "未定"

VALID_TONES: Final[frozenset[str]] = frozenset(
    {TONE_BULL, TONE_BEAR, TONE_NEUTRAL})

#: 依据词组的长度下限（太短的词组在任何文本里都能找到，没有核对价值）
MIN_PHRASE_CHARS: Final = 3

#: 单条文本送模型的最大字符数（防 prompt 膨胀；摘要已在契约层截断过）
MAX_TEXT_CHARS: Final = 600

#: **走模型抽取**的最小字符数。**≤ 这个长度的原文不调模型**。
#:
#: 用户口径（2026-09-25）：
#:
#: > "若这个内容长度本身就100字，长度不长久不需要走模型抽取了，
#: >  文字多的才需要抽取，然后输出到前端。"
#:
#: 理由（三件事同时成立）：
#:
#:   1. **100 字的原文不需要压缩** —— 它本身就是摘要，模型能做的只有改写；
#:   2. 模型是这条链路上**最不可靠的一环**（实测会把 JSON 模板抄回来、
#:      会把代码编出来、会判反语气）。对"已经足够短"的内容，
#:      它带来的风险大于收益；
#:   3. 短内容在情报流里占比不小，跳过它们等于把每轮的模型调用
#:      （实测 ~770ms/条、单轮上限 40 条）留给真正需要抽取的长文。
#:
#: ⚠️ 短文本**不调模型 ≠ 不落库**：`tone_job` 照常写一行
#: （摘要 = 原文本身、倾向 = `rule_tone`、实体 = 词表解析），
#: 否则前端看到的就是"这条没有数据"（那正是被明令禁止的静默消失）。
MIN_CHARS_FOR_EXTRACTION: Final = 100

#: 核心摘要的长度上限（字符）。用户口径"一句话、能扫"——
#: 卡片列表里一行放得下，且比 `phrases` 更直接地回答"这条讲了什么"。
MAX_SUMMARY_CHARS: Final = 40

#: 摘要的长度下限。低于它说明模型没压出东西（或原文本来就没有摘要价值）。
MIN_SUMMARY_CHARS: Final = 6

#: 关键事件条数上限 / 单条长度上限。
#: 上限是为了**契约稳定**：`events` 会进接口与界面，不能由模型决定长度。
MAX_EVENTS: Final = 8
MAX_EVENT_CHARS: Final = 60

#: 单个方向的行业/个股条数上限（同上，防模型输出无界膨胀）
MAX_INDUSTRIES: Final = 6
MAX_STOCKS: Final = 8

#: 券商名 / 分析师名的条数上限（同样是契约稳定要求）。
#:
#: ⚠️ 这两个字段**只作审计**（见 `validate_people`）：界面上"机构：X / 分析师：X"
#: 由**确定性规则层**（`alert_rules`）负责，模型漏了不会导致漏显示。
MAX_BROKERS: Final = 6
MAX_ANALYSTS: Final = 8

#: 事件/摘要与原文的**最长公共子串**下限。
#:
#: 为什么不是"整串逐字"：这两类字段允许模型压缩措辞
#: （"同比增长30%" → "增长三成"），要求整串逐字会把合法输出全杀掉。
#: 但"原文里一个字都没有"的句子就是编的 —— 参考实现里模型会凭空补
#: 一句"公司基本面良好"，那种句子在原文里连 4 个字都对不上。
MIN_COMMON_CHARS: Final = 4

#: **A 股**股票代码（6 位，且以真实存在的板块前缀开头）
#:
#: ⚠️ 不能只匹配 `\d{6}`。实测踩到：别国事件里的金额串会被当股票代码 ——
#: "金砖国家新开发银行与南非签署**2亿美元**项目贷款" 里抠出了 `222500`、
#: `256500` 这种根本不存在的代码，还带着 `codes` 上了界面。
#: 放开匹配等于给用户看了假的标的。
#:
#: 现行有效前缀（2026）：
#:   沪市 600/601/603/605（主板）、688（科创板）
#:   深市 000/001/002/003（主板+中小）、300/301（创业板）
#: ⚠️ 各分支的**位数必须各自配平到 6 位** —— 第一版把 `8[3-9]`（2 位）
#: 和 `60[0135]`（4 位）塞进同一个非捕获组再补 `\d{3}`，
#: 于是 2 位前缀的只匹配到 5 位、永远配不上北交所代码（实测 `830799` 漏掉）。
_CODE_RE: Final = re.compile(
    r"(?<!\d)("
    r"60[0135]\d{3}"      # 沪主板
    r"|688\d{3}"          # 科创板
    r"|00[0123]\d{3}"     # 深主板/中小
    r"|30[01]\d{3}"       # 创业板
    r"|43\d{4}"           # 北交所（430xxx）
    r"|8[3-9]\d{4}"       # 北交所（83/87/88 开头）
    r")(?!\d)"
)

#: 情绪/方向词表 —— 与 `llm_policy._BULL/_BEAR` 同源，
#: 但这里**不 import 它**：那个模块管"该走哪一层"，本模块管"语气是什么"，
#: 复用会让改一处的词表意外影响另一处的路由决策。
#:
#: ## ⚠️ 第六轮（2026-10-01 用户报障）：词表分两档，判据带门槛
#:
#: 用户口径：
#:
#: > "签订了订单，增加了……这些是能区分出是利好的。"
#:
#: 原来的词表只有 20 个词，`增加` / `签订` / `订单` / `增长一倍` / `创新高`
#: 这一整类**一个都不在里面** —— 于是一条明显偏多的笔记
#: （"AI 超级计算机中的芯片数量增加一倍以上"）拿不到【多】标记。
#:
#: 但直接把 `增加` 这类泛用词塞进去会**反向出错**：`增加` 在任何中文文本里
#: 都出现（"注册资本增加"、"营业成本增加"、"降雨量增加"），
#: 一条中性快讯会因为一个词被标成【多】—— 那是本项目最忌讳的
#: "看起来完全合理的错"（用户核不出来）。所以词表分两档（见 `rule_tone`）：
#:
#:     强档  出现即可支撑方向（事件性强、几乎只出现在方向性语境里）
#:     弱档  泛用词，**单独出现不足以定方向**，要两条以上或与强档同现
#:
#: ⚠️ 分档不是"加权发明"：门槛判据是**确定性**的（见 `rule_tone` 的
#: `MIN_SIGNAL_*`），任何一条文本都能手工复算，与既有"不发明权重"的纪律一致。
#:
#: ## ★ 分档的判据：**这个词本身是不是一个"事件"**
#:
#: 第一版把 `订单` / `上调` / `突破` / `受益` 都放进了强档，实测**立刻过度标注**
#: 了一条真实的中性快讯（形态与 `tests/unit/test_intel_vocab.py` 的
#: `_AI_NOTE` / `_OVERSEAS_NOTE` 同类）：
#:
#:     "英伟达盘后发布最新指引，市场关注算力需求变化，多家云厂商上调资本开支
#:      计划，产业链订单能见度有所提升。"
#:
#: 它命中了 `上调`（强）+ `订单`（强）→ 两个强档 → 判成偏多。而这句话是
#: **行业观察**，不是"谁签了单"。根因是：`订单` / `上调` / `提升` 是
#: **名词与泛用动词**，它们描述的是"能见度/计划/预期"这类**状态**；
#: 真正的事件是 `签订` / `中标` / `获得订单`（有主体、有动作、有结果）。
#: 所以强档只留**事件性与比较级**的词，其余一律进弱档 —— 这才是"看起来
#: 完全合理的错"与"方向明确"之间的那条线。
#:
#: ## ⚠️ 分档**解决到什么程度**：实测边界，不要把它读成"过度标注已经消失"
#:
#: 分档真正消灭的是**一个泛用词单独定方向**。实测（2026-10-01，`rule_tone`）：
#:
#:     "公司营业成本增加，毛利率承压。"     → 未定（弱多 1：增加）
#:     "该公司注册资本增加至 5000 万元。"   → 未定（弱多 1）
#:     "入汛以来全省降雨量增加明显…"        → 未定（弱多 1）
#:     "8月份社会消费品零售总额同比增长3.2%" → 未定（弱多 1 + 无市场语境）
#:
#: ⚠️ **上面那条"英伟达…订单能见度提升"的样本现在仍然会被判成偏多**：
#: 它落在"有市场语境（`产业链` / `厂商`）→ 弱档 ≥2"这一支上，而它同时命中
#: `订单` + `上调` + `提升` 三个弱档词。这不是遗漏，是**这条规则的分辨率上限**：
#: 要把它与"业绩下滑 + 减持"（同样 2 个弱档，但确实该判偏空）分开，需要读懂
#: "计划/能见度"与"经营事实"的区别 —— 那是语义，不是词表。而请求路径**不许**
#: 调模型（硬约束），所以这个残余必须靠另外两件事兜住：
#:
#:   ① `source="rules"` 原样下发（`rule_tone_verdict`）：界面据此说明
#:      "这是词表给的，模型没看过"，用户能自己打折；
#:   ② `phrases` 逐字给命中的词：用户一眼就能看见"它是因为订单/上调/提升
#:      才被标偏多的"，核得出来 —— 这是本项目对"看起来完全合理的错"的唯一解药。
#:
#: 实测残余比例（2026-10-01，272 条真实落库行）：**24 条被判方向（8.8%）**，
#: 其中绝大多数是券商作文里本就明写的多空表述；上面那条是已知的出错形态。
_BULL_STRONG: Final[tuple[str, ...]] = (
    # 事件性：有主体、有动作、有结果（一条公告就是一次方向明确的事件）
    "签订", "中标", "签订订单", "获得订单", "新增订单", "斩获",
    "扭亏", "扭亏为盈", "获批", "量产",
    # 比较级 / 阈值型（用户点名的那条："增加一倍以上"）
    "增长一倍", "增加一倍", "翻倍", "翻番", "创新高", "创历史新高",
)
_BULL_WEAK: Final[tuple[str, ...]] = (
    # 泛用词与**状态词**：必须凑够门槛才认方向（见 `rule_tone`）。
    # ⚠️ `订单` / `上调` / `突破` / `超预期` / `扩产` / `放量` / `涨价` /
    #    `提价` / `受益` / `改善` / `回暖` / `提升` 全在这一档：
    #    它们**方向性成立但特异性不足** —— "上调资本开支"、"订单能见度提升"、
    #    "突破关键技术"都是中性语境，单独一个不足以定方向。
    "增加", "增长", "订单", "上调", "超预期", "突破", "新高", "扩产",
    "放量", "涨价", "提价", "受益", "改善", "回暖", "提升", "增持", "回购",
    "涨停", "上升", "上涨", "向好", "复苏", "提速", "加快", "景气", "看好",
    "推荐", "布局", "落地", "开工", "投产",
)
_BEAR_STRONG: Final[tuple[str, ...]] = (
    # 事件性（同上：一次公告就是一次明确的事件）
    "退单", "取消订单", "解约", "爆雷", "暴雷", "违约", "被诉", "立案",
    "退市", "停产", "冻结", "商誉减值", "跌停",
)
_BEAR_WEAK: Final[tuple[str, ...]] = (
    "下滑", "下降", "减少", "下调", "低于预期", "亏损", "减持", "质押",
    "违规", "处罚", "跌价", "承压", "延期", "推迟", "萎缩", "走弱", "疲软",
    "放缓", "遇冷", "堪忧",
)

#: 旧名（`_BULL_WORDS` / `_BEAR_WORDS`）—— **两档的并集**，保留给既有引用点。
#: ⚠️ 不要在别处按它做判据：它丢掉了"强/弱"这个区分，拿它计数会退回
#: "一个泛用词就定方向"的老问题（见上面分档说明）。
_BULL_WORDS: Final[tuple[str, ...]] = _BULL_STRONG + _BULL_WEAK
_BEAR_WORDS: Final[tuple[str, ...]] = _BEAR_STRONG + _BEAR_WEAK

#: 定方向的门槛（**可复算、可解释**，不是拍出来的权重）。
#:
#: ## 判据（`rule_tone` 里的 `_ok`，逐字可复算）
#:
#: 命中的词先按强/弱分档（见 `_BULL_STRONG` 的说明 —— 强档只留
#: **事件性与比较级**的词）。然后**看文本里有没有市场语境**：
#:
#:     有市场语境（`_MARKET_CONTEXT_RE` 命中）
#:         强 ≥ 1  → 认这个方向
#:         弱 ≥ 2  → 认这个方向
#:     没有市场语境
#:         强 ≥ 2  → 认这个方向
#:         强 = 1 且 弱 ≥ 1 → 认这个方向
#:         弱 ≥ 3  → 认这个方向
#:     其余 → 这一侧**不成立**（票不够）
#:
#: ## 为什么"市场语境"这一层是必要的（两次实测的过度标注）
#:
#: 纯计数（无论阈值取几）都过不了这两条真实样本 —— 阈值调高会漏掉
#: 用户点名的短句，调低会把中性播报标成方向：
#:
#:     "8月份社会消费品零售总额**增长**3.2%"              → 1 个弱档
#:     "**业绩下滑**，遭大额**减持**"                      → 2 个弱档（该判偏空）
#:
#: 两句都是"两个以内的词"，但前者是**统计播报**、后者是**公司经营事实**。
#: 区别不在词数上，而在"**这些词讲的是不是市场/公司经营**"。
#: 所以判据从"数词"改成"数词 + 看语境"：
#:
#:     `增长` 单独出现                   → 不认（宏观统计里满地都是）
#:     `业绩下滑` + `减持`               → 认偏空（`业绩` 是市场语境）
#:     `社会消费品零售总额同比增长3.2%` → 不认（无市场语境词）
#:
#: ⚠️ 语境词表是**朴素名词**（业绩/股价/订单/产能…），不是"行业白名单"：
#: 它只回答"这段文字在讲市场/公司经营吗"，不回答"讲的是哪个行业"。
MIN_SIGNAL_STRONG: Final = 2
MIN_SIGNAL_WEAK: Final = 3

#: **市场/公司经营语境**词（正则，`re.search`）。
#:
#: 用途见 `MIN_SIGNAL_STRONG` 的说明：它决定"泛用词的票数门槛"要不要放宽一档。
#:
#: ## 收集原则（比词表本身更重要）
#:
#:   · 只放**金融/经营语境才出现**的词 —— 放进来就等于自己给自己投票，
#:     所以 `消费` / `需求` 这类宏观与通用词**一律不收**：
#:     "社会消费品零售总额增长" 必须留在"无语境"那一档（票不够 → 不判方向）。
#:   · 收的是**名词/领域词**，不收任何方向性形容词（"下滑"不是语境，"业绩"才是）。
#:
#: ⚠️ 第一版收了 `消费` / `需求`，实测把一条宏观播报（"社会消费品零售总额
#: 同比增长3.2%"）误判成"有市场语境" —— 门槛被放宽一档，于是它拿到了
#: 它不该拿到的方向候选资格。语境词表的**松**会直接变成过度标注。
_MARKET_CONTEXT_RE: Final = re.compile(
    r"业绩|营收|净利|毛利|股价|估值|市值|板块|行业|产业链|厂商|"
    r"订单|合同|产能|出货|销量|产量|库存|市占|装机|"
    r"价格|报价|涨价|跌价|评级|目标价|盈利|亏损|融资|投产|扩产"
)

# ======================================================================
# 券商名/团队名黑名单（提示词 + 硬拦截，两处都要有）
# ======================================================================

#: 券商/团队名清单 —— **必须跳过，它们不是股票**。
#:
#: 来源：参考实现 `moss-finance-assistant` 的 `_ZSXQ_SYSTEM_PROMPT` 第 3 条
#: （实测名单）。`【天风电子】` 这种"券商+行业"的写法看起来完全像一只票，
#: 是这一类任务里**最高频**的错。
#:
#: ⚠️ 只写进提示词是不够的：提示词是"请求"，模型可以不听。
#: `validate_entities` 里用同一份名单做**硬拦截** —— 名单只有一份，
#: 提示词与拦截共用，避免两边改漏一处后"提示了但没拦住"。
_BROKER_SKIP: Final[tuple[str, ...]] = (
    "天风电子", "华福电新", "中信电子", "国金AI金属", "中泰汽车",
    "东吴计算机", "东北商业航天", "招商机械", "信达消费",
)

#: 方向桶的归一化词（**contains 匹配**）。
#:
#: 为什么用 contains 而不是等值：实测模型会输出 `"利好（推荐）"`、
#: `"偏利好"`、`"短期利空"` 这类带修饰的值。等值匹配会把它们全判成无效、
#: 整条丢掉；contains 能把它们收进正确的桶。
#: ⚠️ **顺序有意义**：`利空` 必须先判 —— `"利空出尽"` 里同时含
#: "利空"和（"利好"不含，但）语义上它属于利空侧，先判利空更保守。
_SENT_BEAR: Final[tuple[str, ...]] = ("利空", "看空", "偏空", "负面", "下调")
_SENT_BULL: Final[tuple[str, ...]] = ("利好", "看多", "偏多", "正面", "上调")
_SENT_NEUTRAL: Final[tuple[str, ...]] = ("中性", "中立")

#: 模型可以给的**别名键** —— 提示词用的是英文扁平键，但小模型经常
#: 自作主张换名字（`bull_stocks` / `bullish_stocks` / `利好个股`…）。
#: 全部认下来，认不出就当这个字段没有（不猜）。
_ALIAS_BULL_IND: Final[tuple[str, ...]] = (
    "bull_industries", "bullish_industries", "利好行业", "bull_sectors",
    "bullish_sectors", "利好板块")
_ALIAS_BEAR_IND: Final[tuple[str, ...]] = (
    "bear_industries", "bearish_industries", "利空行业", "bear_sectors",
    "bearish_sectors", "利空板块")
_ALIAS_BULL_STK: Final[tuple[str, ...]] = (
    "bull_stocks", "bullish_stocks", "利好个股", "利好股票", "bull_stock",
    "利好标的")
_ALIAS_BEAR_STK: Final[tuple[str, ...]] = (
    "bear_stocks", "bearish_stocks", "利空个股", "利空股票", "bear_stock",
    "利空标的")
_ALIAS_SUMMARY: Final[tuple[str, ...]] = ("summary", "摘要", "核心摘要", "一句话摘要")
_ALIAS_EVENTS: Final[tuple[str, ...]] = ("events", "关键事件", "事件", "key_events")
_ALIAS_BROKERS: Final[tuple[str, ...]] = (
    "brokers", "broker", "券商", "券商名", "机构", "institutions")
_ALIAS_ANALYSTS: Final[tuple[str, ...]] = (
    "analysts", "analyst", "分析师", "分析师姓名")


def _pick(obj: dict[str, Any], aliases: tuple[str, ...]) -> Any:
    """按别名顺序取第一个**存在且非空**的值（取不到返回 `None`）。"""
    for k in aliases:
        v = obj.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def _dedup(seq: list[str]) -> list[str]:
    out: list[str] = []
    for s in seq:
        if s and s not in out:
            out.append(s)
    return out


def _dedup_limited(seq: list[str], limit: int) -> list[str]:
    """去重并截到 `limit`（`limit <= 0` 视为不截断）。

    分段合并要按**逐字相等**去重并保留首现顺序，而 `_dedup` 没有上限 ——
    合并 4 段的结果可能超过契约上限（`MAX_EVENTS` / `MAX_INDUSTRIES` / …），
    超出部分会一路走到接口与界面，而"长度由模型决定"正是这些常量要防的事。
    """
    return _dedup(seq)[:limit] if limit > 0 else _dedup(seq)


#: 模型"没内容"时**实际会写出来的占位值**。
#:
#: 实测两批，形态不同、本质一样：
#:
#:     第一批  `"bull_industries": ["无"]`（写了个汉字"无"）
#:     第二批  `"codes":[""], "bull_industries":[""], "bull_stocks":[""]`
#:             （一个**装着空串的列表**，而不是空数组）
#:
#: 两种都必须当成"没有数据"。**空串不处理会更糟**：`[""]` 过一遍
#: `strip()` 之后仍然是一个"存在的条目"，它会在 rejected 里刷一行噪音，
#: 而 `"无"` 更危险 —— 只要原文里刚好有"无"字，逐字校验就会放行，
#: 于是界面上出现一条叫"无"的行业/个股，看起来像我们的系统坏了。
_NO_DATA: Final[frozenset[str]] = frozenset({
    "无", "無", "暂无", "没有", "none", "null", "n/a", "na", "-", "—", "/",
})


def _clean_item(raw: Any) -> str:
    """列表项归一化：空串 / 纯空白 / 占位值（`无`、`N/A`…）→ `""`（= 没有数据）。

    所有列表字段（`codes` / `phrases` / 两个方向的行业与个股 / `events`）
    都必须先过这里 —— 判断"这项有没有内容"的地方**只能有一处**，
    否则每加一个字段就要记得写一次 `strip()`，漏一处就是存储里多一条垃圾。
    """
    s = str(raw or "").strip()
    return "" if s.casefold() in _NO_DATA else s


def _normalize_sentiment(raw: Any) -> str:
    """方向值归一化 → `利好` / `利空` / `中性`，**认不出返回空串（丢弃）**。

    与参考实现同口径（contains 匹配）。为什么认不出要丢而不是默认中性：
    默认中性等于替模型补了一个它没说过的判断，那是**编造**。
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    for w in _SENT_BEAR:
        if w in s:
            return "利空"
    for w in _SENT_BULL:
        if w in s:
            return "利好"
    for w in _SENT_NEUTRAL:
        if w in s:
            return "中性"
    return ""


def _has_common_run(part: str, text: str, *, min_len: int = MIN_COMMON_CHARS) -> bool:
    """`part` 与 `text` 是否存在长度 ≥ `min_len` 的公共子串。

    用途：给"允许压缩"的字段（摘要/事件）做防编造校验。
    实现是滚动集合而不是 DP 最长公共子串 —— 只要判断"有没有"，
    O(len(part) × min_len) 就够，且 part 已被截到 60 字。
    """
    if not part or not text or min_len <= 0:
        return False
    if part in text:
        return True
    if len(part) < min_len:
        # 太短的部分串在任何长文本里都能碰上，判它有公共串等于没校验。
        # 直接按"不通过"处理，由调用方连同长度下限一起丢掉。
        return part in text
    grams = {part[i:i + min_len] for i in range(len(part) - min_len + 1)}
    return any(g in text for g in grams)


def _full_text_units(text: str) -> str:
    """把原文的空白去掉，供 count 使用（原文里名字可能被换行拆开）。"""
    return re.sub(r"\s+", "", text or "")


def a_share_codes(text: str) -> list[str]:
    """原文里出现的 A 股代码（去重，保持出现顺序）。

    单列成公开函数是为了让"什么算 A 股代码"**只有一处定义**：
    `summarize._CODE`、实体词表（`vocab.highlights` 要标绿代码）
    都得用同一个判据，各写一份正则必然漂移 —— `_CODE_RE` 上面那段注释
    记的正是"位数没配平导致北交所代码永远匹配不上"那一次事故。
    """
    out: list[str] = []
    for code in _CODE_RE.findall(text or ""):
        if code not in out:
            out.append(code)
    return out


# ======================================================================
# 规则层
# ======================================================================

@dataclass
class RuleTone:
    tone: str = TONE_UNKNOWN
    bull_hits: list[str] = field(default_factory=list)
    bear_hits: list[str] = field(default_factory=list)
    confidence: float = 0.0
    #: 命中的**强档**词（用户点名的那一类：`签订` / `订单` / `增长一倍`…）。
    #:
    #: 单列出来是为了让门槛判据**可解释**：界面上（与排障时）要能回答
    #: "这条为什么被认成偏多" —— 只报总数会把"2 个强档"与"3 个泛用词"
    #: 混成同一个数字，而两者的可信度完全不同。
    strong_hits: list[str] = field(default_factory=list)
    #: 命中的**弱档**（泛用）词。它们的数量必须凑够门槛才算数（见 `rule_tone`）。
    weak_hits: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        """`多X/空Y` 的可读概括（强档票数在括号里）—— 只给 `explain` 用。"""
        return (f"多{len(self.bull_hits)}/空{len(self.bear_hits)}"
                f"（强档{len(self.strong_hits)}）")


def rule_tone(text: str) -> RuleTone:
    """纯词表计数。**可复算、零成本、零幻觉** —— 所以它是交叉验证的基准。

    ## 门槛判据（第六轮加，2026-10-01）

    命中之后**不是**简单地比个数：先看文本里有没有**市场语境**
    （`_MARKET_CONTEXT_RE`），再按强/弱档的票数判（完整表格见
    `MIN_SIGNAL_STRONG` 的说明）：

        有市场语境    强 ≥ 1  或  弱 ≥ 2
        无市场语境    强 ≥ 2  或  (强 = 1 且 弱 ≥ 1)  或  弱 ≥ 3

    两侧都不成立 → `未定`（不是"中性"：我们并没有判定它中性，
    只是词表没给出方向）。两侧都成立 → 比强档票数，再比总票数，
    仍然相等才是 `中性`。

    ## ⚠️ 强弱怎么分：看这个词**本身是不是一个事件**

    判据与实例见 `_BULL_STRONG` 的说明。一句话概括：
    `签订` / `中标` / `增加一倍` 是事件与阈值（方向明确），
    而 `订单` / `上调` / `提升` 是名词与状态词（中性语境里满地都是，
    所以只算一票、还要凑门槛）。
    """
    s = text or ""
    bull_strong = [w for w in _BULL_STRONG if w in s]
    bull_weak = [w for w in _BULL_WEAK if w in s]
    bear_strong = [w for w in _BEAR_STRONG if w in s]
    bear_weak = [w for w in _BEAR_WEAK if w in s]
    # 语境只看一次（两个方向共用同一个判据 —— 各判一次必然漂移）
    market = bool(_MARKET_CONTEXT_RE.search(s))

    def _ok(strong: list[str], weak: list[str]) -> bool:
        """这一侧的证据够不够成方向（判据见函数 docstring）。"""
        if market:
            # 市场/经营语境里，方向词的可信度高一档：一个事件词就够，
            # 泛用词也只要两个（"业绩下滑 + 减持"这种典型组合）
            return bool(strong) or len(weak) >= 2
        if len(strong) >= MIN_SIGNAL_STRONG:
            return True
        if strong and weak:
            return True
        return len(weak) >= MIN_SIGNAL_WEAK

    bull_ok, bear_ok = _ok(bull_strong, bull_weak), _ok(bear_strong, bear_weak)
    bull = bull_strong + bull_weak
    bear = bear_strong + bear_weak

    if not bull_ok and not bear_ok:
        # 两侧票都不够 → 未定。**不是中性**：中性是"确定了没有倾向"，
        # 而这里是"词表没给出方向"（与 `rule_tone` 旧行为一致：
        # 一个词都没命中时也是未定）。
        return RuleTone(bull_hits=bull, bear_hits=bear,
                        strong_hits=bull_strong + bear_strong,
                        weak_hits=bull_weak + bear_weak)
    if bull_ok and not bear_ok:
        tone = TONE_BULL
    elif bear_ok and not bull_ok:
        tone = TONE_BEAR
    elif len(bull_strong) != len(bear_strong):
        tone = TONE_BULL if len(bull_strong) > len(bear_strong) else TONE_BEAR
    elif len(bull) != len(bear):
        tone = TONE_BULL if len(bull) > len(bear) else TONE_BEAR
    else:
        # 两侧**票数完全相等** → 中性（可以展示的结论：这条原文多空相抵）。
        # ⚠️ 不要在这里改判未定：它与 `extract_tone` 的"两层不一致 → 未定"
        # 是两件事 —— 那里是"两个来源打架"，这里是"一个来源说多空相抵"。
        tone = TONE_NEUTRAL

    # 差距越大越有把握；上限 0.9（词表法不该自称完全确定）
    gap = abs(len(bull) - len(bear))
    conf = 0.5 + 0.1 * gap if gap else 0.4
    return RuleTone(tone=tone, bull_hits=bull, bear_hits=bear,
                    confidence=min(round(conf, 2), 0.9),
                    strong_hits=bull_strong + bear_strong,
                    weak_hits=bull_weak + bear_weak)


def rule_tone_verdict(text: str) -> dict[str, Any]:
    """规则层倾向 → **读取侧那一份最小形状**（`build_feed` 的兜底用）。

    ## 为什么需要它（用户报障 2026-10-01）

    用户报障：「AI 超级计算机中的芯片数量增加一倍以上」这条明显偏多的笔记
    **没有【多】标记**。实测成因是**抽取任务的班次间隔**：`_intel_tone_extract`
    注册为 `20 */2 * * *`（每 2 小时一班，见 `registry.py`），而条目是**随时**
    被采进来的 —— 实测 18:20 那一班之后 `intel_zsxq_collect` 又采了 30 条
    （游标 18:20 → 21:20），那批条目在 `tone_store` 里**一行都没有**，
    于是最长 2 小时内它们一律没有方向标记。

    ⚠️ 一条**已被推翻**的旧判断留在这里，免得有人按它去"修"错的地方：
    早先以为试点根本没有调度器（因为 `manage.py` 里写着
    `MOSS_SCHEDULER_ENABLED=0`），实测**那句话是错的** ——
    那个变量**全仓库没有任何一处读它**，`main.py` 的 lifespan 无条件
    `CronScheduler(...).start()`。试点其实一直在跑全部 24 个作业。
    所以这里要解决的**不是**"作业没跑"，而是"**作业跑得比采集慢**"。

    方向因此必须有第二条、**不依赖班次**的来源。这条路本来就是免费的
    （`rule_tone` 是几个 `in` 判断），放在请求路径里不违反
    "请求路径不调模型"这条硬约束。

    ## ⚠️ `source` 必须是 `rules`，不能伪造成模型判定

    界面与存储靠 `source` 区分"模型给的"与"词表给的"
    （`tone_job` 那边同样是 `rules` / `rules+llm`）。把词表猜测标成
    `rules+llm` 会让用户以为模型看过这条 —— 那正是"把请求当保证"的同类错误。

    ## ⚠️ 返回 `None` 的两种情形，含义完全不同

      · `未定`（票不够）→ 返回 `None`：**不给方向字段**，前端照旧显示
        "没有判定"，条目留在"给不出方向"那一侧（可被收容组收纳）。
      · `中性`（多空票数相等）→ **照样返回**：那是一个判定，
        读取侧按"已判定中性"处理（与模型给定的中性同一条路径）。

    返回 `None` 而不是抛错：规则层读不到不该让整条情报流挂掉
    （与 `tone_store` 的"文件坏了按空处理"同一条纪律）。
    """
    try:
        rule = rule_tone(text or "")
    except Exception:  # noqa: BLE001 规则层失败退化成"没有倾向"，绝不抛
        logger.warning("规则层倾向计算失败（按无倾向处理）")
        return None
    if rule.tone == TONE_UNKNOWN:
        return None
    return {
        "tone": rule.tone,
        "has_tone": rule.tone in VALID_TONES,
        "neutral": rule.tone == TONE_NEUTRAL,
        # 依据：命中的词本身就是原文子串，**逐字可核对**
        # （与 `extract_tone` 的 phrases 同一条纪律：标签必须配得上证据）。
        "phrases": list(rule.bull_hits if rule.tone == TONE_BULL
                        else rule.bear_hits if rule.tone == TONE_BEAR else []),
        "confidence": rule.confidence,
        # ★ 审计字段：让"这是词表给的，不是模型给的"在存储/界面/排障里都看得见
        "source": "rules",
        "explain": (f"规则层词表计数（多{len(rule.bull_hits)}/"
                    f"空{len(rule.bear_hits)}，"
                    f"强档{len(rule.strong_hits)}），未经语义抽取"),
    }


# ======================================================================
# 抽取结果
# ======================================================================

@dataclass
class ToneResult:
    """一条情报的原文倾向 + **可核对的依据**。"""

    tone: str = TONE_UNKNOWN
    #: 原文词组（**逐字来自原文**，已过子串校验）
    phrases: list[str] = field(default_factory=list)
    #: 原文中出现的 6 位代码（已过"必须在原文里"校验）
    codes: list[str] = field(default_factory=list)
    #: 0~1。**只在两层一致时给值**；不一致时为 `None`（不显示数值）
    confidence: float | None = None
    #: 判定来源：`rules` | `rules+llm` | `skipped`
    source: str = "rules"
    #: 面向用户的一句话（**不含来源标识**）
    explain: str = ""
    #: 被拦截掉的内容（仅供日志与审计，**不出接口**）
    rejected: dict[str, Any] = field(default_factory=dict)

    # ── 扩展字段（第二轮）。都不是"平台判断"，是原文里明写的标的与事件 ──
    #: ≤ `MAX_SUMMARY_CHARS` 的核心摘要。空串 = 这次没生成/校验不过
    summary: str = ""
    #: 关键事件（短句，与原文有公共子串才保留）
    events: list[str] = field(default_factory=list)
    #: 利好侧。`{"industries": [...], "stocks": [{"name","code","count"}],
    #: "boards": [{"name","code"}]}`
    bullish: dict[str, Any] = field(default_factory=dict)
    #: 利空侧（结构同上）
    bearish: dict[str, Any] = field(default_factory=dict)

    # ── 第五轮：券商名 / 分析师名（用户 2026-10-01："如果有…必须要输出"）──
    #
    # ⚠️ 这两个字段只作**审计**（落 `tone_store`，供排错与扩名单时回看），
    # **不直接上屏** —— 界面上的"机构：X / 分析师：X"由确定性规则层保证
    # （见 `validate_people` 里那段取舍说明）。
    #: 模型给的券商名（逐字校验过；可能是我们名单之外的机构）
    brokers: list[str] = field(default_factory=list)
    #: 模型给的分析师名（逐字校验过；只做粗筛，不保证真是分析师）
    analysts: list[str] = field(default_factory=list)

    # ── 第四轮：分段抽取的**来源说明**（供落库与排障，不是判断）──
    #
    # ⚠️ 这三项**刻意不进 `to_public()`**：它们回答的是"这个结果是**怎么**来的"
    # （内部过程），不是"原文在讲什么"（对外事实）。白名单纪律见 `to_public`：
    # 该进接口的字段必须显式加，**不该进的也不该顺手加**。
    #: 本条实际**处理**的段数（= 送模型的段数；短文本为 1）
    segments: int = 1
    #: 本条实际发起的**模型调用次数**。正常情况下等于 `segments`；
    #: 与 `segments` 分开记是为了让"短文本 0 次调用"这件事在存储里看得见
    calls: int = 0
    #: 因 `MAX_EXTRACT_SEGMENTS` 上限**跳过**的段区间说明（`"2~3 段 / 1820 字"`）。
    #: 空串 = 没有跳过。⚠️ 有跳过就必须有它 —— 静默丢中间一段，
    #: 表现只是"这段怎么没抽到"，日志里查不出任何原因。
    skipped_segments: str = ""

    @property
    def has_tone(self) -> bool:
        return self.tone in VALID_TONES

    def to_public(self) -> dict[str, Any]:
        """**白名单构造** —— 新字段默认不出接口。"""
        out: dict[str, Any] = {
            "tone": self.tone,
            # 界面靠它决定"显示标签还是显示未定"
            "has_tone": self.has_tone,
            "phrases": list(self.phrases),
            "codes": list(self.codes),
            "confidence": self.confidence,
            "source": self.source,
            "explain": self.explain,
        }
        # ⚠️ 扩展字段**必须显式加**：白名单式构造下，漏了就是"模型抽到了
        #   但接口永远看不到"（不会有任何报错，只会表现为功能没做）。
        out["summary"] = self.summary
        out["events"] = list(self.events)
        out["bullish"] = _public_side(self.bullish)
        out["bearish"] = _public_side(self.bearish)
        # 券商名 / 分析师名（审计字段，见 `ToneResult` 的说明）。同样是
        # **白名单**：不加这两行，模型抽到的名字会静默留在进程里 ——
        # 表现为"提示词要了、模型给了、存储里没有"，没有任何报错。
        out["brokers"] = list(self.brokers)
        out["analysts"] = list(self.analysts)
        return out


def _public_side(side: dict[str, Any]) -> dict[str, Any]:
    """方向桶的**白名单投影**（只放行已知键，逐个拷贝防外部改内存）。"""
    return {
        "industries": list((side or {}).get("industries") or []),
        "stocks": [
            {
                "name": str(s.get("name") or ""),
                "code": str(s.get("code") or ""),
                "count": int(s.get("count") or 0),
            }
            for s in ((side or {}).get("stocks") or [])
        ],
        "count": int((side or {}).get("count") or 0),
        # 第三轮：与 `industries` **同一批板块**，多了主线挖掘的板块代码。
        # 只放行已知键，理由同上面几个 —— 白名单漏一个键的表现是
        # "抽到了但接口永远看不到"，不会有任何报错。
        "boards": [
            {
                "name": str(b.get("name") or ""),
                "code": str(b.get("code") or ""),
            }
            for b in ((side or {}).get("boards") or [])
            if isinstance(b, dict)
        ],
    }


def _last_safe_cut(text: str) -> int:
    """返回 `text` 里**最后一个"未落在字符串内部"的 token 边界**。

    边界 = 逗号 / 左右括号之后（这些字符不可能出现在 JSON 字符串里，
    所以扫到时一定在结构层）。返回 `0` 表示找不到任何边界。

    ## 为什么不能用"最后一个 `}`"（参考实现的做法）

    参考实现是 `text[:text.rfind('}') + 1]` 再补括号。**在字符串被截断时
    它是错的** —— 模型输出被 token 上限切断，切的往往是**字符串中间**：

        … {"name":"隆基绿          ← 最后那个 `}` 是**外层对象**的开括号
                                     配对的 `}` 根本没输出

    实测本地 1.5B 长这样：`{"tone":"偏多","bull_stocks":["中芯`,
    最后一个 `}` 在开头，`rfind` 返回 0，`text[:1]` = `"{"`，补成 `"{}"`
    —— 修出来一个**空对象**，然后所有字段都"没了"，看起来像模型没说话。

    正确做法：切在最后一个**结构层字符**之后，把没写完的那个 token
    整段丢掉。丢掉的是残缺值，**保留的全是完整值**。
    """
    in_str = False
    esc = False
    last = 0
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in ",{}[]":
            last = i + 1
    return last


def _repair_truncated_json(raw: str) -> str | None:
    """修复被 token 上限截断的 JSON。

    ## 为什么必须有这一步

    输出预算被截断时，模型给的是 `{"tone":"偏多","bull_stocks":["中芯国际` ——
    `json.loads` 必然失败，于是**整条走规则层兜底**，
    而模型其实已经把大部分答案说完了。

    ## 策略：切在最后一个"未落在字符串内部"的边界，再补齐括号

    ⚠️ 补的只有 `]` 和 `}`，**不补任何字段值** —— 补值就是替模型编造。
    截断丢掉的只有最后那个（不完整的）条目，前面的原样保留。

    ⚠️ 切点**不能**只看最后一个 `}`（见 `_last_safe_cut` 的实测反例）：
    字符串被切断时最后那个 `}` 往往是外层对象的开括号，
    照它切会修出一个空对象 —— 比不修更糟（"没内容"会被当成"模型没抽到"）。
    """
    text = (raw or "").strip()
    if not text:
        return None
    cut = _last_safe_cut(text)
    if cut <= 1:
        # 一个 token 边界都没有（或只有开头那个 `{`）：没有任何完整值，
        # 修出来只能是 `{}` —— 那是一个**空的成功解析**，
        # 比"解析失败"更糟（会让调用方以为模型什么都没抽到）。
        return None
    body = text[:cut].rstrip().rstrip(",").rstrip()
    if not body.startswith("{"):
        return None
    # 从**外层**的开括号起算括号平衡（不能整串数：被丢掉的尾段里
    # 可能还有没配平的引号/括号，会把配平数算错）
    open_arr = body.count("[") - body.count("]")
    open_obj = body.count("{") - body.count("}")
    repaired = body + ("]" * max(open_arr, 0)) + ("}" * max(open_obj, 0))
    try:
        json.loads(repaired)
    except (ValueError, TypeError):
        return None      # 修完仍然不是 JSON → 放弃（调用方走规则层）
    return repaired


def parse_json(raw: str) -> dict[str, Any] | None:
    """从模型输出里取 JSON。回退链（每层都只做**结构**修补，不补字段）：

    1. 直接 `json.loads`
    2. 剥 ``` 围栏（`json_mode=True` 时模型仍偶尔加）
    3. 正则抓第一个 `{...}`（模型在前面写了一句"好的，结果是："）
    4. 修截断（见 `_repair_truncated_json`）
    5. 放弃 → `None`

    取不到就返回 `None`，由 `extract_tone(llm_obj=None)` 走规则层 ——
    **绝不猜模型想说什么**（补全等于替模型编造它没说过的内容）。
    """
    s = (raw or "").strip()
    if not s:
        return None
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        obj = json.loads(s)
    except (ValueError, TypeError):
        obj = None
    if obj is None:
        m = re.search(r"\{[\s\S]*\}", s)
        if m:
            try:
                obj = json.loads(m.group(0))
            except (ValueError, TypeError):
                obj = None
    if obj is None:
        repaired = _repair_truncated_json(s)
        if repaired:
            try:
                obj = json.loads(repaired)
            except (ValueError, TypeError):
                obj = None
    return obj if isinstance(obj, dict) else None


def validate_extraction(obj: dict[str, Any], text: str) -> tuple[
        str, list[str], list[str], dict[str, Any]]:
    """把模型输出过一遍拦截，返回 `(tone, phrases, codes, rejected)`。

    ## 三条拦截，每一条都对应实测出现过的一次幻觉

      · `tone` 必须是合法枚举 —— 实测模型会把模板文字抄回来
      · `phrases` 必须**逐字**出现在原文 —— 实测被改写过标点
      · `codes` 必须是原文里真有的 6 位数字 —— 实测出现过
        `["6位代码"]`（模板文字）与 `[""]`（空串）

    ⚠️ 返回签名是**四个值**，扩展字段不在里面（走 `validate_entities` /
    `validate_events` / `validate_summary`）—— 既有测试是按四元组解包的，
    把新字段塞进这个元组会让所有既有调用点静默错位。
    """
    rejected: dict[str, Any] = {}

    raw_tone = str(obj.get("tone") or "").strip()
    tone = raw_tone if raw_tone in VALID_TONES else TONE_UNKNOWN
    if raw_tone and tone == TONE_UNKNOWN:
        rejected["tone"] = raw_tone

    phrases: list[str] = []
    bad_phrases: list[str] = []
    for p in (obj.get("phrases") or []):
        s = _clean_item(p)
        if not s:
            continue
        if len(s) < MIN_PHRASE_CHARS or s not in text:
            bad_phrases.append(s)
            continue
        if s not in phrases:
            phrases.append(s)
    if bad_phrases:
        rejected["phrases"] = bad_phrases

    src_codes = set(_CODE_RE.findall(text))
    codes: list[str] = []
    bad_codes: list[str] = []
    for c in (obj.get("codes") or []):
        s = _clean_item(c)
        if not s:
            continue
        if s in src_codes and s not in codes:
            codes.append(s)
        else:
            bad_codes.append(s)
    if bad_codes:
        rejected["codes"] = bad_codes
    # 兜底：模型没给代码但原文里有 —— 用规则抓（原文里真有，不算幻觉）
    if not codes and src_codes:
        codes = sorted(src_codes)[:3]

    return tone, phrases, codes, rejected


# ======================================================================
# 扩展字段的校验（原文逐字 / 防编造）
# ======================================================================

def _is_research_house(name: str) -> bool:
    """`name` 是**研究机构署名**或**点名分析师**吗（⇒ 绝不能当个股/板块）。

    ## 为什么必须有一道硬拦截（用户口径 2026-09-25）

    > "个股名比较可靠就按个股名，本身就是个股优先，目的就是找到那些股被
    >  唱多，唱空。"

    这条链路要回答的是"**哪些股**在被打"。一旦 `国金证券`（券商）或
    `孙潇雅`（分析师）出现在个股列表里，这个答案就被污染了 ——
    而它长得**完全合理**：原文里逐字有这个名字、逐字校验放行、
    界面上显示成一只"标的"，用户核不出任何问题。

    ## 三类来源，缺一不可

      · `_BROKER_SKIP`        具体团队名黑名单（`天风电子`、`中泰汽车`…）
      · `alert_rules.is_research_house`  `XX证券` + `简称+行业组` 两种署名形态
        —— 用户 2026-10-01 追加："券商名字不一定带券商两个字，比如可能是
        天风电子，招商电子"。**形态扩了，这道拦截也一起扩**，否则新认出来的
        机构名会立刻从"机构"那一栏漏进"个股"那一栏。
      · `ANALYST_WATCHLIST`   用户点名的六位分析师（`孙潇雅`…）

    ⚠️ 后两类**复用 `alert_rules` 那一份实现**，不在这里另抄清单：
    名单是手工维护的，抄一份必然漂移，而漂移的表现是"告警侧认得出、
    抽取侧却把它当成了股票"（或反过来）—— 两边对不上账，没人看得出为什么。
    """
    if not name:
        return False
    if name in _BROKER_SKIP:
        return True
    try:
        from src.domain.intel import alert_rules

        if name in alert_rules.ANALYST_WATCHLIST:
            return True
        return alert_rules.is_research_house(name)
    except Exception:  # noqa: BLE001 规则层读不到不该让整条校验失败
        return False


def _valid_name(name: str, text: str) -> bool:
    """名称是否**逐字**出现在原文里，且不在券商/分析师名单上。

    ⚠️ 只做 `in` 判断，不做任何模糊匹配、不做同义词映射 ——
    模糊匹配会把"改写"放进来，那就失去了"用户能拿它去原文核对"的意义
    （与 `phrases` 同一条理由）。
    """
    if len(name) < 2:
        # 单字名字在任何中文文本里都能撞上（"中"/"大"），没有核对价值
        return False
    if _is_research_house(name):
        return False
    return name in text


def _valid_industry(ind: str, text: str) -> bool:
    """行业名同样要逐字，且不能是券商/团队名。

    券商团队名长得就像行业名（`华福电新`、`中泰汽车`），所以这里
    复用同一份黑名单 —— 只在个股那侧拦是不够的。
    分析师名同理：`孙潇雅` 出现在"行业"那一栏里是纯粹的脏数据。
    """
    if len(ind) < 2:
        return False
    if _is_research_house(ind):
        return False
    return ind in text


def _resolve_industry(ind: str, text: str) -> str:
    """行业名 → **概念板块规范名**；不在项目跟踪的板块里就返回空串（丢弃）。

    ## 为什么要在逐字校验之外再加一道词表闸门

    逐字校验只能保证"这个词在原文里"，保证不了"这是一个板块"。
    实测模型会把"新能源车""光模块龙头"这类**听起来就该在这段里**的词
    写进行业 —— 它们在原文里确实逐字存在，但本项目并没有跟踪这个板块，
    放进情报流就是一条用户对不上的分类。

    用户口径（2026-09-25）："情报流的概念板块要和主线挖掘跟踪的板块对齐。"
    所以这里只放行 `vocab` 里那份**主线挖掘自己的**板块目录。

    ## ⚠️ 词表读不到时退回旧的"逐字即可"判据

    改成"一律丢弃"会让界面上的行业**整块消失**且没有任何报错线索 ——
    那是本项目最忌讳的静默功能消失（与 `zsxq_incremental.fresh_floor`
    那次事故同类）。宁可这时宽松一点，也不要静默清空。
    """
    from src.domain.intel import vocab

    if not _valid_industry(ind, text):
        return ""
    if vocab.board_code(ind):
        return ind
    return "" if vocab.boards_available() else ind


def _text_pair_code(name: str, text: str) -> str:
    """原文里**紧跟在名字后面**的 6 位代码（`中芯国际（688981）`）。

    这是"原文里明写的配对"，不是模型给的配对 —— 所以可以用。
    允许名字与代码之间夹 ≤4 个非数字字符（括号、空格、冒号都见过）。
    """
    if len(name) < 2 or not text:
        return ""
    # 只认**原文里真实存在的 A 股代码**（`_CODE_RE` 已含前缀与数字边界判据）
    src_codes = set(_CODE_RE.findall(text))
    if not src_codes:
        return ""
    for m in re.finditer(re.escape(name) + r"[^\d]{0,4}(\d{6})", text):
        code = m.group(1)
        if code in src_codes:
            return code
    return ""


def _stock_code_for(name: str, text: str) -> str:
    """个股名 → 代码：**词表优先，其次原文里明写的配对**。

    ⚠️ 两条来源都不是"模型给的代码"。实测模型会给一个"配得上这个名字"的
    代码（原文里一个数字都没有，它照样能编出 6 位数字），所以那个值
    **一律不采用**（见 `validate_entities`）。
    """
    from src.domain.intel import vocab

    code = vocab.stock_code(name)
    if code:
        return code
    return _text_pair_code(name, text)


def _split_name_code(raw: Any) -> tuple[str, str]:
    """把模型给的一项拆成 `(名称, 代码)`。认不出返回 `("", "")`。

    模型给"股票"的形态实测有四种：`"中芯国际"`、`"688981"`、
    `"中芯国际(688981)"`、`{"name":..., "code":...}`。前三种都要认，
    否则合法输出会被当成无效项丢掉。
    """
    if isinstance(raw, dict):
        return (str(raw.get("name") or raw.get("股票") or "").strip(),
                str(raw.get("code") or raw.get("代码") or "").strip())
    s = str(raw or "").strip()
    if not s:
        return "", ""
    m = re.match(r"^(.*?)[（(]\s*(\d{6})\s*[)）]\s*$", s)
    if m and m.group(1).strip():
        return m.group(1).strip(), m.group(2)
    if re.fullmatch(r"\d{6}", s):
        return "", s
    return s, ""


def _count_of(raw: Any, name: str, flat: str) -> int:
    """提及次数：`max(模型给的, 原文实际出现次数)`，名字长度 ≥2 才算。

    ## 为什么取 max 而不是信模型

    实测模型**系统性少报**（一条笔记里"中芯国际"出现 5 次，模型写 1）——
    它只数了记住的那几处。原文的 `count` 是**可复算**的，所以两者取大：
    模型多报时原文计数可能真的少（名字被换行拆开），此时信模型；
    模型少报时用原文计数补上。

    ⚠️ `len(name) >= 2` 是必须的：单字名（"中"）在原文里出现几十次，
    计数会变成一个毫无意义的数（参考实现同样是 `len(name) >= 2`）。
    """
    try:
        model_count = int((raw or {}).get("count") if isinstance(raw, dict) else 0)
    except (TypeError, ValueError):
        model_count = 0
    actual = flat.count(name) if len(name) >= 2 else 0
    return max(model_count, actual, 1)


def validate_entities(obj: dict[str, Any], text: str) -> tuple[
        dict[str, Any], dict[str, Any], dict[str, Any]]:
    """校验利好/利空两侧的行业与个股，返回 `(bullish, bearish, rejected)`。

    ## 逐条判据（全部是"过不了就丢这一条"，不是丢整个字段）

      · **名称必须逐字在原文里**（`_valid_name`）—— 这是硬要求
      · **行业必须逐字在原文里，且在概念板块词表里**（`_resolve_industry`）：
        逐字只能保证"这个词在原文里"，保证不了"它是本项目跟踪的板块"，
        实测模型会写"新能源车""光模块龙头"这类**听起来就该在这段里**的分类
      · **券商/团队名一律丢**（`_BROKER_SKIP`，提示词里也写了）
      · **个股代码只从词表取**（`_stock_code_for`）：模型给的代码
        **一律不采用** —— 实测一条连数字都没有的笔记，它输出了
        `"codes":["603919"]`。代码来自词表/原文配对，两者都可核对
      · 只给代码（没给名字）时，代码必须在原文里**真出现**（复用 `_CODE_RE`，
        挡掉金额串与模型编的号），名字从词表反查，查不到就留空
      · 没名字也没代码 → 丢（无标的可核对）

    ⚠️ 逐字校验（`_valid_name`）仍然**不做**模糊匹配、不做同义词映射 ——
    模糊匹配会把"改写"放进来，那就失去了"用户能拿它去原文核对"的意义。
    词表只用来**取代码**，不用来放宽名字（名字的合法性判据只有"原文里有"）。
    """
    rejected: dict[str, Any] = {}
    flat = _full_text_units(text)
    src_codes = set(_CODE_RE.findall(text))
    # 延迟导入：`vocab` 会读数据仓/行情仓，模块顶部导入会把这两条
    # 重依赖挂到所有引用 `tone` 的地方（包括接口进程）
    from src.domain.intel import vocab

    def _side(side_name: str, stk_keys: tuple[str, ...],
              ind_keys: tuple[str, ...]
              ) -> tuple[dict[str, Any], list[str], list[str], list[str]]:
        raw_inds = _pick(obj, ind_keys) or []
        raw_stks = _pick(obj, stk_keys) or []
        if isinstance(raw_inds, str):
            raw_inds = [raw_inds]
        if isinstance(raw_stks, str):
            raw_stks = [raw_stks]
        # 兼容嵌套形态：`{"bullish": {"industries": [...], "stocks": [...]}}`
        nested = obj.get(side_name)
        if isinstance(nested, dict):
            raw_inds = raw_inds or (nested.get("industries")
                                    or nested.get("sectors") or [])
            raw_stks = raw_stks or (nested.get("stocks") or [])

        good_inds: list[str] = []
        bad_inds: list[str] = []
        for x in raw_inds or []:
            raw_name = (x or {}).get("name") if isinstance(x, dict) else x or ""
            s = _clean_item(raw_name)
            if not s:
                # 占位值（`""` / `"无"` / `"N/A"`）在这里就被吃掉：
                # 不进结果、也**不进 rejected**（那不是幻觉，是"没有内容"）
                continue
            canon = _resolve_industry(s, text)
            if canon and canon not in good_inds:
                good_inds.append(canon)
            else:
                bad_inds.append(s)

        good_stks: list[dict[str, Any]] = []
        bad_stks: list[str] = []
        ignored_codes: list[str] = []
        seen_codes: set[str] = set()
        seen_names: set[str] = set()
        for x in raw_stks or []:
            name, model_code = _split_name_code(x)
            # 占位值（`{"name": ""}` / `"无"` / `"N/A"`）→ 没有数据
            name, model_code = _clean_item(name), _clean_item(model_code)
            label = name or model_code
            if not name and not model_code:
                continue
            if name:
                if not _valid_name(name, text):
                    bad_stks.append(name)
                    continue
                # ★ 代码**只从词表/原文配对来**。模型给的那个即使看起来合理
                #   也不用 —— 它会在原文一个数字都没有时照样编出一个代码。
                code = _stock_code_for(name, text)
                if model_code and model_code != code:
                    ignored_codes.append(f"{name}:{model_code}")
            else:
                # 只给了代码：必须在原文里真出现（挡掉模型编的号）
                if model_code not in src_codes:
                    bad_stks.append(f"{label}:代码不在原文({model_code})")
                    continue
                code = model_code
                # 名字只能来自词表（原文里没写名字）。查不到就留空 —— 不编名字
                name = vocab.stock_name(code)
            if not name and not code:
                continue
            if code and code in seen_codes:
                continue
            key = name or code
            if key in seen_names:
                continue
            seen_codes.add(code)
            seen_names.add(key)
            good_stks.append({
                "name": name,
                "code": code,
                "count": _count_of(x, name or code, flat),
            })
            if len(good_stks) >= MAX_STOCKS:
                break

        side = {
            "industries": good_inds[:MAX_INDUSTRIES],
            "stocks": good_stks,
            "count": len(good_inds[:MAX_INDUSTRIES]) + len(good_stks),
            # ── 第三轮新增：板块代码 ──
            #
            # `industries` 是**名字**（界面直接显示、用户拿去原文核对），
            # 这里给同一批板块的**主线挖掘板块代码**（`885xxx.TI`）——
            # 有了它，"情报流说的板块"与"主线挖掘跟踪的板块"才是**可连接**的，
            # 而不只是看起来同名。
            "boards": [
                {"name": n, "code": vocab.board_code(n)} for n in good_inds
            ][:MAX_INDUSTRIES],
        }
        return side, bad_inds, bad_stks, ignored_codes

    bullish, bad_bull_ind, bad_bull_stk, ign_bull = _side(
        "bullish", _ALIAS_BULL_STK, _ALIAS_BULL_IND)
    bearish, bad_bear_ind, bad_bear_stk, ign_bear = _side(
        "bearish", _ALIAS_BEAR_STK, _ALIAS_BEAR_IND)

    if bad_bull_ind:
        rejected["bull_industries"] = bad_bull_ind
    if bad_bear_ind:
        rejected["bear_industries"] = bad_bear_ind
    if bad_bull_stk:
        rejected["bull_stocks"] = bad_bull_stk
    if bad_bear_stk:
        rejected["bear_stocks"] = bad_bear_stk
    # 模型给的代码被忽略（**仅供日志与审计**，不出接口、不落库）——
    # 这是"模型在编代码"这个缺陷唯一的观测点，没有它就只能靠翻原文才发现
    if ign_bull:
        rejected["bull_stocks_model_code"] = ign_bull
    if ign_bear:
        rejected["bear_stocks_model_code"] = ign_bear
    return bullish, bearish, rejected


def validate_events(obj: dict[str, Any], text: str) -> tuple[list[str], list[str]]:
    """校验关键事件，返回 `(events, rejected)`。

    判据：非空、长度 ≥ `MIN_COMMON_CHARS`、与原文有长度 ≥
    `MIN_COMMON_CHARS` 的公共子串（`_has_common_run`）。

    为什么事件不做"整串逐字"：事件本来就是**概括**（"公司中标12.5亿元订单"
    → "中标大额订单"），要求整串逐字等于要求模型不许概括，那这个字段
    就没有存在意义。改成"必须有公共子串"：允许压缩措辞，但
    "原文里一个字都找不到"的句子（实测模型会补"公司基本面良好"）
    照样丢掉。
    """
    raw = _pick(obj, _ALIAS_EVENTS) or []
    if isinstance(raw, str):
        raw = [raw]
    good: list[str] = []
    bad: list[str] = []
    for x in raw or []:
        raw_ev = (x or {}).get("event") if isinstance(x, dict) else x or ""
        s = _clean_item(raw_ev)
        s = re.sub(r"\s+", " ", s)[:MAX_EVENT_CHARS]
        if not s:
            continue
        if len(s) < MIN_COMMON_CHARS or not _has_common_run(s, text):
            bad.append(s)
            continue
        if s not in good:
            good.append(s)
        if len(good) >= MAX_EVENTS:
            break
    return good, bad


def validate_summary(obj: dict[str, Any], text: str) -> tuple[str, str]:
    """校验核心摘要，返回 `(summary, reason)`。`reason` 非空 = 被丢掉了。

    与 `summarize.validate_summary` **不是**重复实现，分工不同：
      · `summarize` 那份管"字数、数字不能编、不能出现买卖建议"
      · 这份管"这句话在原文里有没有根"（`_has_common_run`），
        且只作为**兜底**使用 —— `tone_job` 优先用 `summarize` 的结果。
    """
    raw = _pick(obj, _ALIAS_SUMMARY)
    s = re.sub(r"^(?:摘要|总结|一句话|要点)[：:]\s*", "",
               _clean_item(raw))
    s = re.sub(r"\s+", " ", s)
    if not s:
        return "", ""
    if len(s) < MIN_SUMMARY_CHARS or not _has_common_run(s, text):
        return "", "no_root_in_source"
    return s[:MAX_SUMMARY_CHARS], ""


# ======================================================================
# 券商名 / 分析师名的校验（用户 2026-10-01："如果有…必须要输出"）
# ======================================================================

#: "像机构名"的后缀词（**比展示侧的判据宽一档**，理由见 `validate_people`）。
_ORG_WORDS: Final[tuple[str, ...]] = (
    "证券", "券商", "银行", "基金", "资管", "研究所", "研究部", "研究院",
    "投资", "资本", "公司", "集团",
)

#: 中文人名的长度区间（汉字个数）。`陈果` 两字、`武超则` 三字。
_PERSON_MIN: Final = 2
_PERSON_MAX: Final = 4


def _is_cjk(s: str) -> bool:
    """整串都是汉字（用来判"像不像中文人名"）。"""
    return bool(s) and all("\u4e00" <= ch <= "\u9fff" for ch in s)


def _looks_like_person(name: str, text: str) -> bool:
    """`name` 像不像一个**原文里的人物名**（只用于"审计字段"的粗筛）。

    ⚠️ 这个判据**不保证**它是分析师 —— 中文人名与公司简称在词形上无法区分
    （`华为` 也是两个字）。所以它只服务审计字段，不上屏。

    ⚠️ 机构名要排除（`天风电子` 也是 4 个汉字），但**用户点名的分析师
    不能被排除** —— 他们正是这个字段最该有的值（用
    `alert_rules.is_research_house`（只认机构）而不是 `_is_research_house`
    （机构 + 分析师，"绝不能当个股"那道闸门），两者判据不同：
    后者会把 `孙潇雅` 一起挡掉，于是审计字段永远是空的）。
    """
    if not (_PERSON_MIN <= len(name) <= _PERSON_MAX) or not _is_cjk(name):
        return False
    if name in _BROKER_SKIP:
        return False
    try:
        from src.domain.intel import alert_rules

        if alert_rules.is_research_house(name):
            return False
        # 个股名 / 板块名一律不是人名（`中芯国际` 4 个字、`存储芯片` 4 个字）
        from src.domain.intel import vocab

        if vocab.stock_code(name) or vocab.board_code(name):
            return False
    except Exception:  # noqa: BLE001 词表/规则读不到不该让整条校验失败
        pass
    return name in text


def _looks_like_org(name: str, text: str) -> bool:
    """`name` 像不像一个**机构名**（同样只用于审计字段的粗筛）。"""
    if not (2 <= len(name) <= 12):
        return False
    if not any(w in name for w in _ORG_WORDS) and not _is_research_house(name):
        return False
    return name in text


def validate_people(obj: dict[str, Any], text: str) -> tuple[
        list[str], list[str], dict[str, Any]]:
    """校验模型给的**券商名 / 分析师名**，返回 `(brokers, analysts, rejected)`。

    ## 为什么向模型要这两个字段（用户口径 2026-10-01）

    > "建议直接让本地模型输出时，直接输出：相关个股/板块 + 事件一句话摘要
    >  （如果有以下内容必须要输出：股票名 板块名 券商 孙潇雅、赵宇阳、
    >   武超则、陈果、刘晨明、洪灏），推送到前端展示。"

    ## ⚠️ 提示词**不是**"必须输出"的保证（这一段比上面那句重要）

    模型漏一个名字，用户就漏一个 —— 而这次需求的全部目的就是"别漏掉
    谁在唱多/唱空"。所以那四类的**展示**保证长在**确定性规则层**上：

        股票名 + 板块名   `vocab.scan`（词表扫描，与高亮同一份）
        券商              `alert_rules` 的两种署名形态（`XX证券` / `简称+行业组`）
        分析师名          `alert_rules.ANALYST_WATCHLIST`（用户点名的六人）
    合并 / 补齐发生在 `service.build_feed`（清洗后的**全文**再扫一遍、取并集）。
    也就是说"模型这次什么都没抽到"**不会**让任何一类从界面上消失。

    本函数抽出来的两个字段是**补充与审计**：模型可能读到我们名单之外的
    机构或分析师。它们过逐字校验后落进 `tone_store`，
    供排错与"将来扩大名单"时回看；**不直接进界面**。

    ## ⚠️ 为什么不直接上屏（一道刻意的取舍，写下来免得后人误会）

    "逐字在原文里"挡不住"这个词确实在这段文字里，但它不是机构/人名"：
    模型会把公司名（`华为`）、产品名、泛称写进这两个字段。界面上出现
    "分析师：华为"正是本项目最忌讳的"看起来完全合理的错"（用户核不出来），
    而名单内的名字由规则层**保证**上屏，放宽这一步的收益远小于风险。
    要让它上屏，得先有比"逐字"更强的判据（例如分析师语境窗口），
    那是另一件事，不在本次改动里顺手做。

    ## 判据（逐条都是"过不了就丢这一条，不丢整个字段"）

      · 逐字出现在**这段**原文里（`in`，不做模糊匹配）
      · 券商名要"像机构"（`_looks_like_org`：含机构后缀词，或已是规则层
        认得出的署名形态）
      · 分析师名要"像人名"（`_looks_like_person`：2~4 个汉字，
        且不是机构名 / 个股名 / 板块名）
      · 占位值（`""` / `"无"` / `"N/A"`）在这里就被吃掉（`_clean_item`），
        既不进结果也不进 rejected —— 那不是幻觉，是"没有内容"
    """
    rejected: dict[str, Any] = {}

    def _collect(aliases: tuple[str, ...], limit: int,
                 validator: Any, key: str) -> list[str]:
        raw = _pick(obj, aliases) or []
        if isinstance(raw, str):
            raw = [raw]
        good: list[str] = []
        bad: list[str] = []
        for x in raw or []:
            raw_name = (x or {}).get("name") if isinstance(x, dict) else x or ""
            n = _clean_item(raw_name)
            if not n:
                continue
            if not validator(n):
                bad.append(n)
                continue
            if n not in good:
                good.append(n)
            if len(good) >= limit:
                break
        if bad:
            rejected[key] = bad
        return good

    brokers = _collect(_ALIAS_BROKERS, MAX_BROKERS,
                       lambda n: _looks_like_org(n, text), "brokers")
    analysts = _collect(_ALIAS_ANALYSTS, MAX_ANALYSTS,
                        lambda n: _looks_like_person(n, text), "analysts")
    return brokers, analysts, rejected


def _rule_people(text: str) -> tuple[list[str], list[str]]:
    """**规则层**的机构名 / 分析师名（零模型成本）—— 短文本那条路用。

    `tone_job` 对 ≤ `MIN_CHARS_FOR_EXTRACTION` 的原文**一次模型调用都不发起**
    （用户口径），但那一段仍然要落库一行 —— 否则"短文本没有机构/分析师"
    与"这一行根本没写"在存储里长得一样。所以这里用与展示侧**同一份实现**
    （`alert_rules`）把名字扫出来，`source` 会如实标成 `rules`。
    """
    try:
        from src.domain.intel import alert_rules

        return (alert_rules.institutions({"title": "", "summary": text}),
                alert_rules.analysts({"title": "", "summary": text}))
    except Exception:  # noqa: BLE001 规则层失败退化成"没有名字"，绝不抛
        return [], []


# ======================================================================
# 编排
# ======================================================================

def _rule_entities(text: str, rule: RuleTone) -> tuple[
        dict[str, Any], dict[str, Any], dict[str, Any]]:
    """规则层的实体解析（**纯词表扫描，零模型成本**），返回 `(多, 空, rejected)`。

    ## 只对**短文本**做（`_rule_entities` 里还有一道长度保险）

    `≤ MIN_CHARS_FOR_EXTRACTION` 的原文只讲一件事 —— 整条的语气就是那件事的
    语气，所以"整条偏多 ⇒ 原文提到的板块/个股在利好侧"成立。

    长文本不成立：一条笔记里"光伏组件价格下滑"与"半导体设备景气上行"常常
    同时出现，整条语气是两者**相抵**的结果（往往还是未定）。按整条语气给
    每个标的安方向，会把其中一只放到错的一侧 —— 那正是本项目最忌讳的
    "看起来完全合理的错"（用户核不出来）。所以长文本的实体只由模型给，
    并逐条过词表校验。

    ## 没有方向依据时**两侧都不放**（宁可少显示，不猜）

    契约里只有 `bullish` / `bearish` 两个桶，没有"中性"桶。规则层给不出方向
    （未定 / 中性）时，把命中的标的塞进"利好"或"利空"，等于替用户编了一个
    原文里没有的判断。此时两侧留空，只把命中的名字记进 `rejected` ——
    它**只进日志、不落库、不出接口**，用途是让"这条为什么没有实体"可排查。
    """
    empty: dict[str, Any] = {}
    s = text or ""
    if len(s) > MIN_CHARS_FOR_EXTRACTION:
        return empty, empty, {}
    # 延迟导入（同 `validate_entities`：词表要读数据仓/行情仓）
    from src.domain.intel import vocab

    boards = vocab.boards_in_text(s, limit=MAX_INDUSTRIES)
    stocks = vocab.stocks_in_text(s, limit=MAX_STOCKS)
    if not boards and not stocks:
        return empty, empty, {}
    if rule.tone not in (TONE_BULL, TONE_BEAR):
        return empty, empty, {"entities_no_direction": [
            str(b["name"]) for b in boards] + [str(t["name"]) for t in stocks]}
    side = {
        "industries": [str(b["name"]) for b in boards],
        "stocks": stocks,
        "count": len(boards) + len(stocks),
        "boards": [{"name": str(b["name"]), "code": str(b["code"])}
                   for b in boards],
    }
    return (side, empty, {}) if rule.tone == TONE_BULL else (empty, side, {})


def extract_tone(*, text: str, credibility_score: int,
                 llm_obj: dict[str, Any] | None = None,
                 rule_entities: bool = False) -> ToneResult:
    """合成一条最终倾向。

    `llm_obj` 为 `None` 表示**没有调模型**（低可信 / 模型不可用），
    此时只用规则层。这样"模型挂了"不会让功能消失，只是精度下降 ——
    但界面能看出来（`source` 字段）。

    ## `rule_entities`：要不要用规则层去**扫实体**

    默认 `False`。只有"这条**本来就该由规则层负责**"时才传 `True`
    —— 也就是 `tone_job` 判定的短文本（≤ `MIN_CHARS_FOR_EXTRACTION`，
    用户口径：短内容不走模型）。理由见 `_rule_entities`：

      · 短文本只讲一件事，整条语气就是那件事的语气，方向可以照搬；
      · 长文本往往多主题并存，用整条语气给每个标的安方向会**张冠李戴**，
        而那种错"看起来完全合理"，用户核不出来。

    做成显式参数而不是"按长度自动决定"：`extract_tone(llm_obj=None)` 的
    另一个来路是**模型挂了**（长文本），那条路上绝不该按整条语气派方向。
    把意图写在调用点上，读代码的人不用猜。

    ## 交叉验证：不一致就不给倾向

    实测最危险的一类错不是"抽取失败"，而是**判断相反**：
    「中标12.5亿元订单，机构上调盈利预测」被模型判成 `中性`，
    而规则层命中 4 个偏多词。这时：
      · 信模型 → 一条明显的偏多原文被标成中性（漏报）
      · 信规则 → 词表法也会被"利空出尽"这类反讽骗到
    所以**两个都不信**，给 `未定`。用户看到的就是"这条我们不做倾向归类"，
    那比一个可能错的标签诚实得多。

    ## 扩展字段与 `tone` 的关系（故意解耦）

    `bullish` / `bearish` / `events` / `summary` **不参与**交叉验证，
    也不因 `tone=未定` 而被清空。理由：它们不是"判断"，是
    **原文里明写的标的与事件**（已过逐字校验）——
    "模型与词表对语气意见不一致"不能推出"这段原文没提到中芯国际"。
    清空它们等于因为一个分歧丢掉全部可核对的事实。

    ⚠️ 但 `phrases` 仍然清空（它是 `tone` 的**依据**，留着等于变相给倾向）。
    """
    rule = rule_tone(text)

    # 低可信：不做倾向分析（用户口径）
    if credibility_score < MIN_CREDIBILITY_FOR_TONE:
        return ToneResult(
            tone=TONE_UNKNOWN,
            phrases=[],
            codes=sorted(set(_CODE_RE.findall(text or "")))[:3],
            confidence=None,
            source="skipped",
            explain=(f"可信度 {credibility_score} 低于 {MIN_CREDIBILITY_FOR_TONE}，"
                     f"不做原文倾向分析"))

    # 没调模型：只用规则
    #
    # ⚠️ 实体要不要用规则层扫，由 `rule_entities` 决定（见上面 docstring）：
    #   · 短文本（`tone_job` 传 True）：本该由规则层负责，词表扫描补上实体
    #   · 长文本（模型挂了 / 输出解析失败）：**不扫** —— 整条语气给每个标的
    #     安方向会张冠李戴
    if llm_obj is None:
        bull_side: dict[str, Any] = {}
        bear_side: dict[str, Any] = {}
        ent_rejected: dict[str, Any] = {}
        if rule_entities:
            bull_side, bear_side, ent_rejected = _rule_entities(text or "", rule)
        # 机构名 / 分析师名：**规则层也要给**（见 `_rule_people`）。
        # ⚠️ 不看 `rule_entities`：那一项管的是"要不要按整条语气给标的安方向"
        # （长文本会张冠李戴），而"这段文字里出现了哪家机构/哪位分析师"
        # 与语气无关，长短文本都成立。
        people_brokers, people_analysts = _rule_people(text or "")
        if rule.tone == TONE_UNKNOWN:
            return ToneResult(
                tone=TONE_UNKNOWN, source="rules",
                codes=sorted(set(_CODE_RE.findall(text or "")))[:3],
                explain="规则层无倾向词，且未做语义抽取",
                bullish=bull_side, bearish=bear_side,
                brokers=people_brokers, analysts=people_analysts,
                rejected=ent_rejected)
        return ToneResult(
            tone=rule.tone,
            phrases=(rule.bull_hits if rule.tone == TONE_BULL
                     else rule.bear_hits if rule.tone == TONE_BEAR else []),
            codes=sorted(set(_CODE_RE.findall(text or "")))[:3],
            confidence=rule.confidence,
            source="rules",
            explain=f"词表计数（多{len(rule.bull_hits)}/空{len(rule.bear_hits)}）",
            bullish=bull_side, bearish=bear_side,
            brokers=people_brokers, analysts=people_analysts,
            rejected=ent_rejected)

    tone, phrases, codes, rejected = validate_extraction(llm_obj, text or "")

    # ── 扩展字段：**独立校验**，一个字段不过不牵连其它字段 ──
    #
    # ⚠️ 这里刻意不放进 `validate_extraction`：那个函数返回四元组，
    # 既有调用点（含单测）按四元组解包，加返回值会让它们静默错位。
    bullish, bearish, ent_rejected = validate_entities(llm_obj, text or "")
    # ★ 单段也要去跨侧同名：模型可以在**同一次回答**里把一个板块同时写进
    #   `bull_industries` 与 `bear_industries`（实测"储能"就是这样）。
    #   用户口径："同名两侧都不放，在触发依据里不用写任何内容。"
    bullish, bearish = drop_cross_side(bullish, bearish)
    events, bad_events = validate_events(llm_obj, text or "")
    summary, summary_reason = validate_summary(llm_obj, text or "")
    # 机构名 / 分析师名：**独立校验**（同上面几个字段，一个不过不牵连其它）。
    # 展示仍由规则层保证，这两个字段只作审计 —— 见 `validate_people`。
    brokers, analysts, people_rejected = validate_people(llm_obj, text or "")
    rejected.update(ent_rejected)
    rejected.update(people_rejected)
    if bad_events:
        rejected["events"] = bad_events
    if summary_reason:
        rejected["summary"] = summary_reason

    # ── 交叉验证 ──
    if rule.tone == TONE_UNKNOWN:
        # 规则层没意见：模型说了算，但**不给数值置信度** ——
        # 只有一个来源的判断，没资格自称有把握
        final = tone
        conf = None
        why = "规则层无倾向词，采用语义抽取" if tone != TONE_UNKNOWN else "无依据"
    elif tone == TONE_UNKNOWN:
        final = rule.tone
        conf = rule.confidence
        why = f"语义抽取无结论，采用词表计数（多{len(rule.bull_hits)}/空{len(rule.bear_hits)}）"
    elif tone == rule.tone:
        final = tone
        conf = min(rule.confidence, 0.85)   # 一致但仍保守
        why = "词表计数与语义抽取一致"
    else:
        # ★ 不一致 → 不猜
        final = TONE_UNKNOWN
        conf = None
        why = (f"词表计数（{rule.tone}）与语义抽取（{tone}）不一致，"
               f"不给出倾向")

    # ★ 依据词组的**兜底**：模型给的词组可能全被拦掉（实测：标点被改写
    # 导致"逐字"校验失败），此时 final 有倾向但 phrases 为空 ——
    # 界面上就会显示"偏多"却**没有依据**。对一个必须能被核对的字段，
    # 那比不显示更糟（用户无法判断归类对不对）。
    #
    # 兜底用**规则层命中的词**：它们按定义就是原文子串，逐字可核。
    if final != TONE_UNKNOWN and not phrases:
        phrases = list(rule.bull_hits if final == TONE_BULL
                       else rule.bear_hits if final == TONE_BEAR else [])

    # 标 `未定` 时把依据也清掉 —— 否则界面上会出现
    # "未定" + 一串看起来像证据的词组，等于变相给了倾向
    if final == TONE_UNKNOWN:
        phrases = []
        codes = codes or sorted(set(_CODE_RE.findall(text or "")))[:3]

    res = ToneResult(
        tone=final, phrases=phrases, codes=codes, confidence=conf,
        source="rules+llm", explain=why, rejected=rejected,
        summary=summary, events=events, bullish=bullish, bearish=bearish,
        # 模型给的机构/分析师名（审计字段）。
        # ⚠️ `final == 未定` 时**照样保留**：它们不是"判断"，
        # 是"原文里出现的名字"（已过逐字校验），与上面 bullish/bearish
        # 同一条纪律 —— 语气上的分歧推不出"这段原文没提到孙潇雅"。
        brokers=brokers, analysts=analysts)
    if rejected:
        # 只记数量，不记内容 —— 内容可能夹带上游文本
        logger.info("倾向抽取拦截：%s", {k: len(v) for k, v in rejected.items()})
    return res


# ======================================================================
# 分段抽取的**确定性合并**（第四轮）
# ======================================================================

def _side_merge(picked: list[dict[str, Any]]) -> dict[str, Any]:
    """合并各段同一方向的行业/个股（**纯函数，便于单测**）。

    规则（全部是"并集 + 确定性去重"，没有一处需要猜）：

        行业   按**逐字相等**去重，保留首现顺序，截到 `MAX_INDUSTRIES`
        个股   按 `(名字, 代码)` 去重（**不能只按名字**：同一个名字在不同段里
               可能一个带代码一个不带，只按名字去重会把带代码的那条丢掉），
               截到 `MAX_STOCKS`
        板块   跟 `industries` 走（名字相同就同码，`vocab.board_code` 是纯函数）
        count  合并后**重算**，不累加各段的 count —— 累加会把同一只票
               因出现在两段里而被数成两只，这个数会进界面

    ⚠️ 代码**只从词表/原文配对取**（`validate_entities` 已经保证），
    合并这一步不做任何代码推断：它只搬已经过校验的条目。
    """
    inds: list[str] = []
    stocks: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for side in picked:
        for name in (side.get("industries") or []):
            s = str(name)
            if s and s not in inds:
                inds.append(s)
        for stk in (side.get("stocks") or []):
            if not isinstance(stk, dict):
                continue
            name = str(stk.get("name") or "")
            code = str(stk.get("code") or "")
            if not name and not code:
                continue
            key = (name, code)
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            stocks.append({"name": name, "code": code,
                           "count": int(stk.get("count") or 0)})
    inds = inds[:MAX_INDUSTRIES]
    stocks = stocks[:MAX_STOCKS]
    return {
        "industries": inds,
        "stocks": stocks,
        # 合并后重算（见 docstring：累加会把重复出现的票数成两只）
        "count": len(inds) + len(stocks),
        "boards": [{"name": n, "code": _board_code(n)} for n in inds],
    }


def _stock_key(stk: dict[str, Any]) -> str:
    """个股在"跨侧去重"里用的键：**有名字按名字**，没名字才按代码。

    ⚠️ 不能一律用 `(名字, 代码)`：同一个板块里的票在不同段里可能一个带代码、
    一个不带（模型只给了名字），按二元组判会让它**两侧都留着** ——
    而用户看到的都是"中芯国际"这一个名字，界面就成了
    「利好：中芯国际 / 利空：中芯国际」。
    """
    name = str(stk.get("name") or "").strip()
    if name:
        return f"n:{name}"
    return f"c:{str(stk.get('code') or '').strip()}"


def drop_cross_side(bullish: dict[str, Any],
                     bearish: dict[str, Any],
                     ) -> tuple[dict[str, Any], dict[str, Any]]:
    """同一个名字同时出现在**两侧**时，**两侧都删掉**。

    > 用户口径（2026-09-26）：
    >
    >   "同一个板块会同时出现在「利好」和「利空」两侧，同名两侧都不放，
    >    在触发依据里不用写任何内容。"

    ## 为什么两侧都不放，而不是"留证据多的那一侧"

    我们**没有带方向的计数**：`count` 是"这条原文里提到它几次"，不分多空。
    按它选一侧，等于用一个与方向无关的数替用户决定方向 —— 正是本项目
    最忌讳的"看起来完全合理的错"（用户拿去原文核不出来）。
    而两侧都留的表现更糟：界面上「利好：储能 / 利空：储能」看起来像功能坏了。

    ## 为什么**不**写进触发依据（`explain`）

    用户明确要求"不用写任何内容"。而且这一栏的职责是"方向是靠哪几个词判的"
    （逐字可核对），塞一句"原文对它有分歧"会把**可核对的事实**与
    **我们的解释**混在一起；删掉的名字本来也不该在界面上留痕。

    ## 判据

        行业 / 板块   按**名字**逐字相等
        个股          按名字（没名字才按代码），见 `_stock_key`

    `count` 与 `boards` 按删完的结果**重算** —— 留一个悬空计数会让界面
    显示"利好（3）"而底下只列两条。

    ⚠️ 两侧都没有交集时**原样返回**（不重建 dict）：这条函数在抽取热路径上
    （每条情报每段一次），而绝大多数条目根本没有交集。
    """
    bull_ind = [str(x) for x in (bullish.get("industries") or [])]
    bear_ind = [str(x) for x in (bearish.get("industries") or [])]
    both_ind = {x for x in bull_ind if x in set(bear_ind)} - {""}

    bull_stk = [s for s in (bullish.get("stocks") or []) if isinstance(s, dict)]
    bear_stk = [s for s in (bearish.get("stocks") or []) if isinstance(s, dict)]
    bull_keys = {_stock_key(s) for s in bull_stk}
    bear_keys = {_stock_key(s) for s in bear_stk}
    both_keys = {k for k in bull_keys if k in bear_keys} - {"n:", "c:"}

    if not both_ind and not both_keys:
        return bullish, bearish

    def _clean(side: dict[str, Any]) -> dict[str, Any]:
        inds = [str(n) for n in (side.get("industries") or [])
                if str(n) not in both_ind]
        stks = [s for s in (side.get("stocks") or [])
                if isinstance(s, dict) and _stock_key(s) not in both_keys]
        out = dict(side)
        out["industries"] = inds
        out["stocks"] = stks
        out["count"] = len(inds) + len(stks)
        # `boards` 与 `industries` 是同一批（三个产出方都是这么建的），
        # 所以按删完的 `industries` 重建，不留悬空板块。
        out["boards"] = [{"name": n, "code": _board_code(n)} for n in inds]
        return out

    return _clean(bullish), _clean(bearish)


def _board_code(name: str) -> str:
    """板块名 → 主线挖掘板块代码（词表读不到时给空串，调用方照常工作）。"""
    from src.domain.intel import vocab

    try:
        return vocab.board_code(name)
    except Exception:  # noqa: BLE001 词表读不到不该让整条结果作废
        return ""


def _tone_vote(parts: list[ToneResult]) -> tuple[str, str]:
    """逐段语气**投票**：多数取胜，**平票 → 未定**。返回 `(tone, 说明)`。

    ## 为什么不发明"加权方案"

    段与段之间没有可靠的权重依据：段长、位置、"哪段更像结论"都是我们编出来的
    先验，而它们会直接影响界面上那个标签。用户口径是"有幻觉风险的可以不显示"，
    不是"给个大概准的标签"。所以只做**可复算**的计数：
    多数取胜、平票给 `未定`（`未定` 在这里是一等公民，不是失败）。

    ⚠️ `未定` 段**不参与计票**：它是"这一段没判出语气"，不是"这一段判了未定"。
    把它当一票会让三段的 `偏多/未定/偏空` 算成平票 → 丢掉一个真实结论。
    """
    votes: dict[str, int] = {}
    for p in parts:
        if p.tone in (TONE_BULL, TONE_BEAR, TONE_NEUTRAL):
            votes[p.tone] = votes.get(p.tone, 0) + 1
    if not votes:
        return TONE_UNKNOWN, "各段均未判出语气"
    top = max(votes.values())
    winners = [t for t, n in votes.items() if n == top]
    detail = "、".join(f"{t}{n}" for t, n in
                       sorted(votes.items(), key=lambda kv: -kv[1]))
    if len(winners) == 1:
        return winners[0], f"多数段一致（{detail}）"
    # 平票（含"两段判了不同的语气各一票"）：**不猜**
    return TONE_UNKNOWN, f"各段语气票数相等（{detail}）"


def _merge_summary(summaries: list[str]) -> str:
    """逐段摘要 → 一条摘要：用 `；` 连接；**超过上限就只留第 1 段**。

    ## 为什么是"只留第 1 段"而不是再截一刀

    第 1 段是**导语**（研报开头就是结论），它单独出现仍然读得通。
    反过来"截前 40 字"会把第 2 段的半句话接在第 1 段后面 ——
    读起来像一句话，实际是两件事拼的，那正是本项目最忌讳的
    "看起来完全合理的错"。

    ## ⚠️ 绝不无界拼接

    摘要存在的全部意义就是"短到能一眼扫完"（列表卡片里一行）。
    四段摘要拼起来最多 160 字，那已经不是摘要，而界面上的位置没变 ——
    结果是"卡片被撑开"或"看起来像没生成"。所以这里必须收敛：

        ≤ MAX_SUMMARY_CHARS   连接（信息更多，仍然短）
        > MAX_SUMMARY_CHARS   只留第 1 段（导语，必然读得通）
    """
    joined = "；".join(s for s in summaries if s)
    if not joined:
        return ""
    if len(joined) <= MAX_SUMMARY_CHARS:
        return joined
    return summaries[0][:MAX_SUMMARY_CHARS]


def merge_tone_results(parts: list[ToneResult], *,
                       segments: int = 0, calls: int = 0,
                       skipped: str = "") -> ToneResult:
    """把各段的 `ToneResult` 合成**一条**（确定性规则，逐条可复算）。

    ## 为什么必须先逐段校验、再在这里合并

    各段的 `phrases` / 行业 / 个股早已由 `validate_*` 拿**它自己那段原文**校验过。
    合并只做"并集 + 去重"，**不再做任何校验**。若把校验挪到合并之后
    （拿整篇原文校验合并结果），第 3 段里的词就能"通过"第 2 段的条目 ——
    那等于接受一句模型不可能读到的引文，而界面上它显示为"原文逐字可核对"。

    ## 合并规则（每一条的理由）

        events / phrases    并集 → 逐字去重 → 首现顺序 → 截到既有上限
                            （上限是**契约稳定**的要求：`events` 会进接口与界面，
                              不能因为段数变多就变长）
        行业 / 个股          并集 → 按 (名字, 代码) 去重 → 截到既有上限
        tone                见 `_tone_vote`：多数取胜，平票 → 未定
        confidence          **取各段的最小值**。段数只增加不确定性，
                            取最大/平均都会让"多切了几段"看起来更有把握；
                            `None`（该段未给数值）**不参与**取最小值 ——
                            `None` 是"不显示数值"，不是"置信度 0"
        summary             见 `_merge_summary`：连接后超长就只留第 1 段
        source              `.source` 交给调用方按"有没有段调过模型"决定
        skipped_segments    原样带上；**空串也要带**（"没跳过"是明确信息）

    ## 空输入

    `parts` 为空（所有段都失败 / 调用方没给）时返回一个保守的默认结果：
    `未定` + 空实体。**不抛异常** —— 这条链路的纪律是"精度下降，不是功能消失"。
    """
    if not parts:
        return ToneResult(tone=TONE_UNKNOWN, source="rules",
                          explain="各段抽取均未产生结果，按规则层处理",
                          segments=segments or 0, calls=calls,
                          skipped_segments=skipped)
    picked = [p for p in parts if isinstance(p, ToneResult)]

    # ── events / phrases：并集、逐字去重、首现顺序、既有上限 ──
    events = _dedup_limited([e for p in picked for e in p.events], MAX_EVENTS)
    phrases = _dedup_limited([x for p in picked for x in p.phrases],
                             MAX_EVENTS)
    codes = _dedup_limited([c for p in picked for c in p.codes], 3)

    # ── 语气：投票 ──
    final, vote_why = _tone_vote(picked)

    # ── confidence：取最小值（保守）。`None` 不参与 ──
    confs = [p.confidence for p in picked if p.confidence is not None]
    conf = min(confs) if confs else None

    # ── summary：连接，超长只留第 1 段（见 `_merge_summary`）──
    summaries = [p.summary for p in picked if p.summary]

    # ── 标的：两侧分别并集 ──
    bullish = _side_merge([p.bullish for p in picked])
    bearish = _side_merge([p.bearish for p in picked])
    # ★ 合并会**制造**跨侧同名：第 1 段把"储能"放利好、第 3 段把它放利空 ——
    #   单段各自都合法，合起来就是界面上"利好：储能 / 利空：储能"。
    #   所以这一步必须在合并**之后**（`extract_tone` 里那一次只管单段）。
    bullish, bearish = drop_cross_side(bullish, bearish)

    # ── 机构名 / 分析师名：并集（与 events/phrases 同一条规则）──
    #
    # ⚠️ 必须在这里合并：分段抽取是**常态**（长笔记实测平均 1.9~2.0 段/条），
    # 而合并会新建一个 `ToneResult` —— 不显式带上这两个字段的表现是
    # "短文本有、长文本没有"，看起来像模型只在短文本里认出了分析师。
    brokers = _dedup_limited([b for p in picked for b in p.brokers], MAX_BROKERS)
    analysts = _dedup_limited([a for p in picked for a in p.analysts],
                              MAX_ANALYSTS)

    # ── 依据短语：`未定` 就不留依据（与 `extract_tone` 同一条纪律）──
    #
    # ⚠️ `phrases` 是 `tone` 的**依据**。合并后若"有倾向却没有依据"，
    # 界面会显示一个用户无从核对的标签 —— 那比不显示更糟。
    # `extract_tone` 那条路用规则层命中的词兜底；分段这条路**故意不做**：
    # 规则层的命中词是针对**某一段**算出来的，拿它去给合并后的整条语气作依据，
    # 等于用第 1 段的词证明合并结论 —— 与"逐段校验"同一条纪律相冲突。
    # 所以只在"未定"这半边收敛，有倾向没依据时如实留空。
    if final == TONE_UNKNOWN:
        phrases = []

    rejected: dict[str, Any] = {}
    for p in picked:
        for k, v in (p.rejected or {}).items():
            rejected.setdefault(k, [])
            if isinstance(v, list):
                rejected[k].extend(v)
            else:
                rejected[k] = v

    return ToneResult(
        tone=final,
        phrases=phrases,
        codes=codes,
        confidence=conf,
        # `source` 由调用方按"有没有段成功调过模型"覆盖 —— 这里给保守值
        source="rules",
        explain=vote_why,
        rejected=rejected,
        summary=_merge_summary(summaries),
        events=events,
        bullish=bullish,
        bearish=bearish,
        brokers=brokers,
        analysts=analysts,
        segments=segments or len(picked),
        calls=calls,
        skipped_segments=skipped,
    )


# ======================================================================
# 提示词
# ======================================================================

#: 送模型的系统提示。措辞很讲究：
#:   · **"只输出 JSON"** —— 实测不加这句模型会写一段解释
#:   · **"逐字"** —— 实测不加这句 phrases 会被改写（标点被换掉）
#:   · **"不得补充"** —— 防止模型把常识补进来
#:   · 明确"只判原文语气、不做预测" —— 合规边界写进提示词
#:   · **券商名/团队名跳过清单** —— 移植自参考实现（实测最高频的一类错）：
#:     `【天风电子】` 这种"券商+行业"的写法在模型眼里完全像一只票
#:   · **机构名与分析师名要输出到自己的字段**（用户口径 2026-10-01：
#:     "如果有以下内容必须要输出：股票名 板块名 券商 孙潇雅、赵宇阳、
#:      武超则、陈果、刘晨明、洪灏"）。
#:     ⚠️ 提示词只是**请求**：漏了也不会漏显示 —— 那四类的展示由
#:     `alert_rules` + `vocab` 的确定性扫描保证（见 `validate_people`）。
#:     这里把"不要提取分析师姓名"改成"填进 analysts 字段"，是因为旧措辞
#:     会让模型**主动丢掉**用户点名要的名字；改的是**去向**，不是放松约束：
#:     这两个字段仍然是原文逐字，且**绝不能进 stocks**。
#:   · **"每只股票只输出一次并合并重复"** —— 否则同一只票会占满输出预算，
#:     把后面真正的新信息挤掉（本地模型输出上限只有几百 token）
SYSTEM_PROMPT: Final = (
    "你是信息抽取器，只做一件事：判断**这段第三方原文自己**的语气，"
    "并抽取原文提到的利好/利空行业与个股、对应关键事件。"
    "不是你的观点，不是预测，不要给任何投资建议。"
    "只输出 JSON，不要解释。"
    "phrases 必须**逐字**来自输入文本，不得改写标点、不得补充常识。"
    "codes 只填文本中**真实出现**的 6 位股票代码，没有就填空数组。"
    "行业名与股票名也必须是输入文本里**逐字出现过**的，"
    "文本里没有的一律不要写。"
    "绝对不要把券商名称、团队名或分析师姓名写进股票或行业！"
    "券商是发布研报的机构，分析师是写报告的人，都不是股票。"
    "例如以下都是券商名，绝不能进 stocks：天风电子、华福电新、中信电子、"
    "国金AI金属、中泰汽车、东吴计算机、东北商业航天、招商机械、信达消费。"
    "原文里出现的券商名（如中泰证券、天风电子）填进 brokers 字段；"
    "出现的分析师姓名（如孙潇雅、赵宇阳、武超则、陈果、刘晨明、洪灏）"
    "填进 analysts 字段；两个字段都必须是原文里逐字出现的词，没有就填空数组。"
    "不要提取政府机构、行业概念、产品代号。"
    "每只股票只输出一次并合并重复。"
)

#: 送模型的**输出结构**（Ollama 走受约束解码 = 语法级保证）。
#:
#: 为什么必须给 schema：`json_mode=True` 只保证"是 JSON"，不保证
#: "是你要的 JSON"。实测本地 `qwen2.5:1.5b` 在 json_mode 下仍会返回
#: 字段名自创的 JSON（`{"1. 【CC电新】液冷金帝...": -1.1e6}`），
#: 解析失败后**整条静默退回规则层** —— 看起来像"模型没抽到"，
#: 实际是"抽到了但结构不对"。有了 schema，结构由采样器保证。
_EXTRACTION_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "events": {"type": "array", "items": {"type": "string"}},
        "tone": {"type": "string", "enum": ["偏多", "偏空", "中性"]},
        "phrases": {"type": "array", "items": {"type": "string"}},
        "codes": {"type": "array", "items": {"type": "string"}},
        "bull_industries": {"type": "array", "items": {"type": "string"}},
        "bear_industries": {"type": "array", "items": {"type": "string"}},
        "bull_stocks": {"type": "array", "items": {"type": "string"}},
        "bear_stocks": {"type": "array", "items": {"type": "string"}},
        # ── 第五轮：券商名 / 分析师名（用户 2026-10-01）──
        #
        # ⚠️ 加进 `required` 而不是可选：受约束解码只会输出 schema 里列出的键，
        # 不列 = 模型永远给不出这个字段（"提示词要了但结构里没有"，表现是
        # "模型总是抽不到"）。空数组是合法值，所以要求它必给没有代价。
        "brokers": {"type": "array", "items": {"type": "string"}},
        "analysts": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "events", "tone", "phrases", "codes",
                 "bull_industries", "bear_industries",
                 "bull_stocks", "bear_stocks",
                 "brokers", "analysts"],
}


def extraction_schema() -> dict[str, Any]:
    """给 `gateway.complete(json_schema=...)` 用的输出结构（**拷贝**）。

    返回拷贝而不是常量本身：调用方（网关）会把它序列化进缓存指纹，
    万一有谁就地改它，污染的是所有后续调用的约束。
    """
    return json.loads(json.dumps(_EXTRACTION_SCHEMA, ensure_ascii=False))


def build_prompt(text: str) -> str:
    """单条抽取的 prompt（**一次调用出全部字段**，见模块 docstring）。

    ⚠️ 示例 JSON 里的值是**占位**，不是候选答案。实测模型会把模板文字
    原样抄回来（`codes: ["6位代码"]`），所以示例里刻意用中括号标出
    "这里填原文里的东西"，而不是给一个看起来像答案的样例。
    """
    body = (text or "")[:MAX_TEXT_CHARS]
    return (
        "从下面的文本里抽取信息，只输出 JSON，不要解释。\n"
        "输出结构（键必须齐全，没有内容就给空数组/空串）：\n"
        '{"summary":"核心摘要","events":["关键事件"],'
        '"tone":"偏多|偏空|中性",'
        '"phrases":["原文逐字词组"],"codes":["6位代码"],'
        '"bull_industries":["利好行业"],"bull_stocks":["利好股票"],'
        '"bear_industries":["利空行业"],"bear_stocks":["利空股票"],'
        '"brokers":["原文逐字券商名"],"analysts":["原文逐字分析师姓名"]}\n'
        "\n"
        f"summary：不超过 {MAX_SUMMARY_CHARS} 字的核心摘要"
        "（讲清这条说了什么）。\n"
        "events：对应的关键事件，每条一句话；摘要与事件都必须基于原文，"
        "不得编造原文没有的数字、公司、事件。\n"
        "tone：**这段第三方原文自己**的语气（偏多/偏空/中性），"
        "不是你的观点、不预测涨跌。\n"
        "phrases：必须逐字来自原文，不得改写标点。\n"
        "codes：只填原文中真实出现的 6 位 A 股代码。\n"
        "bull_industries / bull_stocks：原文**明确看好或利好**的行业与个股。\n"
        "bear_industries / bear_stocks：原文**明确看空或利空**的行业与个股。\n"
        "行业名与股票名必须是原文里**逐字出现过**的词；"
        "券商名、团队名与分析师姓名**绝对不能**写进行业或股票；"
        "每只股票只写一次（重复的合并）。\n"
        # ⚠️ 这一段是**请求**，不是保证：模型漏了也不会漏显示 ——
        # 展示侧由 `alert_rules`（券商形态 + 分析师名单）与 `vocab`
        # 确定性扫出来，见 `validate_people`。写在提示词里是为了让模型
        # 有机会给出**名单之外**的机构/分析师（那部分只作审计）。
        "brokers：原文里出现的券商/研究机构名（如 中泰证券、天风电子、"
        "招商机械），必须逐字来自原文，没有就填空数组。\n"
        "analysts：原文里出现的分析师姓名（如 孙潇雅、赵宇阳、武超则、"
        "陈果、刘晨明、洪灏），必须逐字来自原文，没有就填空数组。\n"
        "原文：\n" + body
    )


#: 抽取入参里**单段**（= 单次模型调用）的长度上限。
#:
#: 用户口径（2026-09-25）："如果超过600，抽取内容可以取两头各300字。"
#:
#: ⚠️ 第四轮起它的含义变了：不再是"单条笔记的处理上限"，而是**单段上限** ——
#: 更长的原文由 `segment_text` 切成多段、每段一次调用。原因是实测的硬天花板：
#: 一条 3452 字的笔记取两头（591 字）后实体字段**全空**，
#: 而 `天岳先进` / `第三代半导体` 就在被切掉的中间 ~2500 字里 ——
#: 模型没失败，是**它没看见**。取两头等于用"看不见"换"便宜"。
EXTRACT_MAX_CHARS: Final = 600

#: 取两头时代的"每头字数"。**已不再是分段参数**（分段按句边界累积到
#: `EXTRACT_MAX_CHARS`）。保留它是因为 `extraction_text` 的旧契约与既有测试
#: 都在引用这个名字 —— 删掉只会让引用点集体失配，而它的语义并没有错。
EXTRACT_HALF_CHARS: Final = 300

#: 单条笔记**最多切几段**（= 最多几次模型调用）。
#:
#: 上限的作用是**把成本钉住**；取 6 是**实测定的**，不是拍的：
#:
#:   样本   最近 30 条知识星球笔记（10 条 ≤100 字免模型、20 条需抽取）
#:   冷跑   8B 实测 **19.7~31.1 秒/次调用**（均值 26.5s，`use_cache=False`）
#:   分布   cap=4 → 1 段×10、2 段×6、4 段×4，**2 条被裁**
#:          cap=5 → **1 条被裁**      cap=6 → **0 条被裁**（与不设上限等价）
#:   代价   cap 4→6 只多 3 次调用（38→41，+8%），40 条一批 34→36 分钟
#:
#: ⚠️ **4 太小了，会把这次要修的东西又切掉**：《碳化硅材料专题会议》（3341 字）
#: 不设上限是 6 段，`第三代半导体` 在段 0、`天岳先进` 在**段 3/4**；
#: cap=4 取"前 3 段 + 末段"→ `天岳先进` 又一次被跳过（实测最后一轮复现），
#: 而它正是这次改动的起因。6 段 = 前 5 段 + 末段，两条都进得来。
#:
#: ⚠️ 超过上限时取**前 N-1 段 + 最后一段**，绝不是"只取前 N 段"：
#: 研报结构固定为"开头给结论、**结尾给标的与盈利预测**"
#: （"综上，推荐 XX，目标价…"），砍掉尾段等于砍掉这次抽取最想要的标的。
#: 被跳过的那段**必须记进 `SegmentPlan.skipped`** —— 静默丢中间一段，
#: 表现只是"这段怎么没抽到"，日志里查不出任何原因。
MAX_EXTRACT_SEGMENTS: Final = 6

#: 句末标点。中英文都要认：研报里 `PER/PE 30x.` 这种英文句号很常见。
SENTENCE_ENDS: Final = "。！？!?；;."

#: 旧名（`_SENTENCE_ENDS`）—— 同一份字符串，保留给既有引用点。
_SENTENCE_ENDS: Final = SENTENCE_ENDS

#: 找不到任何句末标点时的缺口说明（口语流水账整段没有标点）。
#: ⚠️ 硬切**必须**留下这句话：硬切会把一句话断成两半，
#: 而模型拿到半句话会**顺手补全它** —— 这类"补出来的半句"是编造内容最主要的
#: 来源，且读起来毫无破绽。留下说明至少让排障的人知道"这段是被切断的"。
_HARD_CUT_NOTE: Final = "第{index}段无句末标点，按长度硬切"

#: 段数超上限时的缺口说明模板（段号是**原始**段号，不是处理顺序）。
_SKIP_NOTE: Final = "{start}~{end} 段 / {chars} 字"

#: 两头拼接处的省略标记。
#: **必须有**：直接首尾相接，模型会把"开头最后一句"与"结尾第一句"读成相邻两句，
#: 进而脑补出两者之间的因果 —— 那是最隐蔽的一类幻觉（语言完全通顺，
#: 但原文里这两件事毫无关系）。标记还顺带告诉模型"中间有内容没给你"。
_EXTRACT_GAP: Final = "\n\n……（原文中间部分省略）……\n\n"


@dataclass
class SegmentPlan:
    """一条笔记的**分段计划**（纯数据，可单测）。

    `segments` 是**实际要送模型**的段（已按 `MAX_EXTRACT_SEGMENTS` 裁剪）；
    `total` 是不设上限时切出的段数。两者可能不等 —— 差的那部分在 `skipped` 里，
    调用方必须把它落库：静默丢一段与"这段没抽到"在存储里长得一模一样。
    """

    #: 实际处理的段（每段 ≤ `EXTRACT_MAX_CHARS`，按句边界切）
    segments: list[str] = field(default_factory=list)
    #: 不设上限时切出的段数（> `len(segments)` 即发生了裁剪）
    total: int = 0
    #: 被跳过区间的说明（空串 = 全处理了）
    skipped: str = ""
    #: 分段过程的说明（硬切等；空串 = 全部按句边界切）
    note: str = ""

    @property
    def count(self) -> int:
        """实际段数。`tone_job` 的模型调用次数就是它（逐段一次调用）。"""
        return len(self.segments)

    @property
    def single(self) -> bool:
        """只有一段 = **就是第三轮的单次调用行为**（≤600 字那条路）。

        单列成属性是为了让调用方一眼看出"这次没走分段"，
        而不是每处自己写 `len(plan.segments) == 1`。
        """
        return len(self.segments) <= 1


def segment_text(text: str, *,
                 max_chars: int = EXTRACT_MAX_CHARS,
                 max_segments: int | None = None) -> SegmentPlan:
    """把原文切成**连续、按句边界、每段 ≤ `max_chars`** 的段（供逐段调用）。

    ## 为什么按句边界切（与旧的两头取文同一条理由）

    硬截会把句子断在中间，模型拿到半句话会**顺手补全它** —— 这类"补出来的半句"
    是编造内容最主要的来源，读起来毫无破绽。所以段只在句末标点**之后**断开，
    每段都是完整句子。整段找不到任何句末标点时才硬切（口语流水账），
    并在 `note` 里如实记下。

    ## 段与段必须**连续且不重叠**

    这是"实体召回变好"的前提：段之间不能有洞（有洞就是又漏掉一截原文），
    也不能重复（重复会让同一件事被算两次）。超上限时 `skipped` 把洞**说出来** ——
    那是我们主动做的取舍，不是无声的丢失。

    ## ⚠️ 每段 ≤ `max_chars` 是上界，不是目标

    段长在 (0, 600] 之间浮动；不为了"凑满 600"去跨句拼接 ——
    那会把两个话题塞进同一次调用，而模型的注意力正是被切段这件事省下来的。

    ## ⚠️ `max_segments` 的默认值是**调用时**读模块常量，不是 `def` 时绑定

    写成 `max_segments: int = MAX_EXTRACT_SEGMENTS` 会在**导入时**把值钉死：
    此后改 `tone.MAX_EXTRACT_SEGMENTS`（调参、单测、端到端脚本）对它**毫无影响**，
    而表现是"上限改了但段数没变、跳过区间也没变"—— 没有任何报错，
    实测就是靠这一条才发现"上限 4 改成 5 完全没生效"。
    """
    cap = MAX_EXTRACT_SEGMENTS if max_segments is None else max_segments
    raw = (text or "").strip()
    if not raw:
        return SegmentPlan(segments=[], total=0)
    limit = max(1, max_chars)

    # ── 切成句子（**标点保留在句尾**，切点之后才是下一句的开头）──
    sentences: list[str] = []
    start = 0
    for i, ch in enumerate(raw):
        if ch in SENTENCE_ENDS:
            piece = raw[start:i + 1].strip()
            if piece:
                sentences.append(piece)
            start = i + 1
    rest = raw[start:].strip()
    if rest:
        # 末尾没有句末标点的半句：**照样送出去**（它可能就是"综上，推荐XX"）。
        # 不补标点、不改写 —— 补出来的标点会改变原文，而 phrases 要逐字可核对。
        sentences.append(rest)

    # ── 按句累积成段 ──
    segments: list[str] = []
    buf = ""
    notes: list[str] = []
    for sent in sentences:
        if len(sent) > limit:
            # 单句本身就超长（整段没有句末标点的流水账）：只能硬切，
            # 但**要留下痕迹**（见 `_HARD_CUT_NOTE`），别让半句话看起来像原文。
            if buf:
                segments.append(buf)
                buf = ""
            for k in range(0, len(sent), limit):
                part = sent[k:k + limit]
                if len(part) == limit:
                    segments.append(part)
                    notes.append(_HARD_CUT_NOTE.format(index=len(segments)))
                else:
                    buf = part
            continue
        if buf and len(buf) + len(sent) > limit:
            segments.append(buf)
            buf = ""
        buf += sent
    if buf:
        segments.append(buf)
    if not segments:
        return SegmentPlan(segments=[], total=0)

    # ── 段数上限：取**前 N-1 段 + 最后一段**（保住结尾的标的/目标价）──
    total = len(segments)
    skipped = ""
    if total > max(1, cap):
        keep = max(1, cap)
        head = segments[: keep - 1]
        mid = segments[keep - 1: total - 1]      # 被跳过的中间段
        skipped = _SKIP_NOTE.format(
            start=keep, end=total - 1, chars=sum(len(s) for s in mid))
        segments = head + [segments[-1]]

    return SegmentPlan(segments=segments, total=total,
                       skipped=skipped,
                       note="；".join(notes))


def extraction_text(text: str) -> str:
    """**清洗后**的抽取全文（第四轮：不再在这里压缩）。

    ## 为什么改掉了"取两头"

    第一~三轮它是"超过 600 字就取两头各 300 字"。那个做法有一个**实测的硬天花板**：
    一条 3452 字的《碳化硅材料专题会议》取两头得到 591 字，
    而 `天岳先进`（688234）与 `第三代半导体` 落在被切掉的中间 ~2500 字里 ——
    于是模型给出的实体字段**全空**。它没失败，它没看见。

    压缩的目的（"别把整篇笔记塞进一次 prompt"）现在由 `segment_text` 完成，
    而且做得更对：切成多段、**每段都送进模型**，而不是丢掉中间只留两头。

    ## ⚠️ 这个函数不能返回空、也不能改写正文

    调用方（`service.extraction_input` → `tone_job`）拿它当"清洗后的全文"：
      · 去空白？**不做** —— `phrases` 要逐字可核对，动一个空格就会让
        合法输出在校验里被丢掉（实测模型抄的就是带换行的原文）
      · 截断？**不做** —— 这里截一刀，`tone_job` 就再也看不到中间那段原文，
        而这次改动存在的全部理由就是让中间那段被看见

    压缩只发生在两个地方：`segment_text`（切段）与 `build_prompt`
    （`MAX_TEXT_CHARS` 兜底截断，防 prompt 膨胀）。
    """
    return (text or "").strip()


__all__ = [
    "drop_cross_side",
    "EXTRACT_HALF_CHARS",
    "EXTRACT_MAX_CHARS",
    "MAX_ANALYSTS",
    "MAX_BROKERS",
    "MAX_EVENTS",
    "MAX_EVENT_CHARS",
    "MAX_EXTRACT_SEGMENTS",
    "MAX_INDUSTRIES",
    "MAX_STOCKS",
    "MAX_SUMMARY_CHARS",
    "MAX_TEXT_CHARS",
    "MIN_CHARS_FOR_EXTRACTION",
    "MIN_COMMON_CHARS",
    "MIN_PHRASE_CHARS",
    "MIN_SUMMARY_CHARS",
    "SENTENCE_ENDS",
    "SYSTEM_PROMPT",
    "TONE_BEAR",
    "TONE_BULL",
    "TONE_NEUTRAL",
    "TONE_UNKNOWN",
    "VALID_TONES",
    "RuleTone",
    "SegmentPlan",
    "ToneResult",
    "a_share_codes",
    "build_prompt",
    "extract_tone",
    "extraction_schema",
    "extraction_text",
    "merge_tone_results",
    "parse_json",
    "rule_tone",
    "rule_tone_verdict",
    "segment_text",
    "validate_entities",
    "validate_events",
    "validate_extraction",
    "validate_people",
    "validate_summary",
]
