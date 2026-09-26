"""内容触发规则：**不依赖可信度、不依赖模型**的"该不该弹窗"判据。

> 用户口径（2026-09-25 原文）：
>
>   "知识星球是不是弹窗和置信度无关，取决于抓取的信息内容"
>   "如果出现 XX 证券，出现 孙潇雅、赵宇阳、武超则、陈果、刘晨明、洪灏，
>    这些知名券商分析师的名字，就告警弹窗，要求这些名字不能被本地大模型过滤掉。"
>   "另外是 8b 模型分析出是利空或利好时，就弹窗。"
>   "知识星球不看置信度，看内容里有没有'人'和'机构'，还有这条消息被本地模型
>    分析出是有利空或利多偏向的，都要告警。"
>   "个股名比较可靠就按个股名，本身就是个股优先，目的就是找到那些股被唱多，唱空。"

## 这个模块回答的问题与 `alert_bridge` 不同

    alert_rules   "这条内容**本身**有没有我们点名要盯的人 / 机构"（纯文本规则）
    alert_bridge  "把判定结果翻译成告警引擎认识的字段"（闸门 + 字段映射）

分开的理由：内容规则要能被**单独复算**（用户会问"为什么这条没弹"，
答案必须是"里面没有那六个人、模型也没给方向"这种一句话的判据），
而告警闸门要能被单独测试（弹窗是最打扰用户的功能）。

## 为什么必须单独有一条"内容规则"（实测数据）

知识星球的来源分是 **54**（来源名"知识星球-调研纪要"含"纪要"→ 财经自媒体档），
两轴加权混合后实测 15 条真实笔记**全部落在 58 分**（有一条 18 分）。
而 `alert_bridge.MIN_CREDIBILITY = 74` —— 也就是说：

    这个来源在结构上**永远不可能**触发告警，桥写得再完整也不会弹一次。

用户看的就是这个结果："为什么不弹？" 答案不是桥坏了，是**判据选错了**。
用户改判据为"看内容里有没有人（点名要盯的分析师），以及模型有没有给出
利多/利空偏向"，这个模块就是那个判据的落点。

## 触发条件是 **两条 OR**，任何一条成立即弹

    (a) 人     分析师名单命中（`ANALYST_WATCHLIST`）
    (b) 方向   本地模型判出 偏多 / 偏空（= 利多 / 利空）

⚠️ **机构名（「XX证券」与「简称+行业组」两种形态）不是触发条件** ——
用户 2026-10-01 改口径："券商名不一定要告警，但是一定要前端输出信息。"
所以「中泰证券」「天风电子」都只做**展示**（前端"机构：中泰证券"，
走 `institutions()`），一条只提到券商的笔记**不弹窗**（见 `AlertReason.fires`）。

⚠️ 机构名的形态**扩过一次**（用户 2026-10-01 追加："券商名字不一定带券商
两个字，比如可能是天风电子，招商电子"）—— 见 `BROKER_BASES`。
这次扩展**只加召回、不动触发语义**：机构名依旧不弹窗。

⚠️ **OR 不是 AND，这一条必须被测试钉住**：如果写成 AND，表现是
"几乎什么都不弹"，而一个只断言"两条都成立 ⇒ 弹"的测试在两种语义下**都会通过**
（AND 的假阴性测不出来）。所以 `tests/unit/test_intel_alert_content_rule.py` 里
每条各有一个"只满足这一条"的用例，外加"两条都不满足 ⇒ 不弹"、
以及"只有券商名 ⇒ 不弹但机构名仍在导出里"的成对用例。

## 扫哪段文本：触发看全文，展示两段都看

  · 知识星球的券商署名**经常落在正文很后面**。本地真实笔记里
    `国金证券` 在第 **695** 字（全文 830 字）、`国海证券` 在 260 字之后 ——
    而 `research_note` 的展示摘要被截到 **260** 字（`SUMMARY_MAX_BY_KIND`）。
  · 只扫展示文本 → 这些署名**看不见**。

所以触发判据扫**清洗后的全文**（`extract_text`），并记录命中位置
（`offset` / `in_summary`）。理由是两条，缺一条都不成立：

  1. 漏掉是**拿不到信号** —— 一条带署名的真实研报正是用户要找的东西；
  2. 但用户看到的卡片是**截断后**的 260 字。若理由里写一个他在卡片上
     一个字都找不到的名字，他会怀疑系统在编 —— 那是比漏弹更坏的信任损失。

所以 `ContentHit` 带上命中位置，`describe()` 在命中落在摘要之外时**明说**
"正文后段（展示摘要未显示）"。

展示侧（`institutions()` / `analysts()`）则是**全文与展示文本都扫**：
展示多给一个名字没有任何代价，漏掉才是信息缺失。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final

# ======================================================================
# 触发条件 (a)：分析师名单 —— **手工维护，刻意不完整**
# ======================================================================

#: 用户点名的知名券商分析师。
#:
#: ⚠️ **手工维护、刻意不求全**。用户口径（2026-09-25）：
#: "出现 孙潇雅、赵宇阳、武超则、陈果、刘晨明、洪灏，这些知名券商分析师的名字，
#:  就告警弹窗"。名单**不全没关系**（用户明说漏几个可以接受）——
#: 所以这里**不**去凑一份大盘分析师名录、**不**做模糊匹配：
#:
#: ## 为什么不做模糊匹配（三个具体后果）
#:
#:   1. **误报无法解释**。模糊匹配（编辑距离 / 拼音 / 姓氏+名首字）会命中真实存在
#:      的同名陌生人 —— 弹窗说"命中分析师孙潇雅"，用户去原文里找不到这个名字，
#:      他无法判断是匹配错了还是原文真没有。这份名单的**全部价值**就是
#:      "命中即原文逐字有"，模糊一次就作废。
#:   2. **成本不对称**。漏一个名字的代价是"少弹一条"（用户已接受）；
#:      误报一条的代价是"弹窗不可信"（弹窗是最打扰用户的功能，见 `alert_bridge`）。
#:   3. **名单是配置性质的**，将来要扩是从这里加一行，不是改算法。
#:
#: ⚠️ 名字**必须逐字出现在原文里**才算命中（`in` 判断，不做同义/别名映射）。
#: 单独成 `tuple` 而不是散在代码里：这样"有哪些人"是**一处可读的清单**，
#: 而不是埋在三个 if 里的字面量。
ANALYST_WATCHLIST: Final[tuple[str, ...]] = (
    "孙潇雅", "赵宇阳", "武超则", "陈果", "刘晨明", "洪灏",
)

# ======================================================================
# 触发条件 (b)：券商名形态
# ======================================================================

#: 「XX证券」形态。用户口径是"如果出现 XX 证券"。
#:
#: 形态**逐字就是用户给的那条口径**：`[\u4e00-\u9fff]{2,8}证券`
#: （实测覆盖：中泰证券 / 天风证券 / 国金证券 / 国海证券 / 国联民生证券 6 字）。
#:
#: ## ⚠️ 两侧**都不加**边界断言 —— 加哪一侧都有一个实测的静默盲区
#:
#: 本机在真实语料上逐条比对过五个变体（矩阵见 `_probe_broker_ctx.py` 与
#: 本轮提交说明）。结论是"加边界"看着更干净，实际会让**真命中消失**：
#:
#:     加左界 `(?<![\u4e00-\u9fff])`
#:         `内 × 7 + 国金证券` 命中 **0** 次 —— lookbehind 钉住起点、
#:         `{2,8}` 钉住窗口，左侧连续汉字超过 8 个时两个约束的交集为空。
#:         而漏报是**没有任何线索**的失败（这条笔记静默不弹）。
#:     加右界 `(?![\u4e00-\u9fff])`
#:         **真实语料里的原样命中直接归零**：实测 `://国金证券研究服务`
#:         因为"证券"后面紧跟"研"（汉字）被右界否掉 ——
#:         而"券商署名后面紧跟团队/行业名"正是最常见的形态。
#:
#: 裸形态的代价只是"在**没有分隔符**的汉字长串里多带几个前缀字"
#: （如 `内内内内内内国金证券`）。这个代价可以接受，因为：
#:
#:   1. 它**不影响弹窗判定** —— 命中就是命中，该弹还是会弹；
#:   2. 用户看到的名字**仍是原文逐字子串**（`broker_name` 只在命中串内部切），
#:      他拿去原文里一定找得到，且 `_scan` 记录的是**真实起点** `offset`；
#:   3. 真实散文里机构名几乎总被标点/括号/行首界定（实测两处真实命中：
#:      `【国金证券】`、`：印度国家证券`），此时提取出来的就是干净名字。
#:
#: 一句话：宁可偶尔多带两个字，也不要静默漏掉一条。
#:
#: ⚠️ **与 `tone._BROKER_SKIP` 完全是两件事，不要合并**：
#:
#:     tone._BROKER_SKIP   一张**具体团队名**黑名单（"天风电子"/"中泰汽车"…），
#:                         用途是**告诉模型别把券商团队名当股票**（抽取侧拦截）
#:     本模式              一个**形态**正则，用途是**判断这条内容该不该弹窗**
#:
#: 两者判据不同（一个是枚举、一个是形态）、生命周期不同（一个跟着提示词走、
#: 一个跟着用户的告警需求走），合并的结果必然是"改一处、另一处悄悄失效"。
#: `tone._BROKER_SKIP` **一个字都不能动**。
#:
#: ## 为什么不用 `credibility._BROKER_SIGNATURE` 那份实现
#:
#: 那份是 `[\u4e00-\u9fff]{2,6}(?:证券|研究所|研究院)|【…】` —— 它服务的是
#: **可信度提档**（"宁可高估研报的权威性"），所以刻意宽（含研究所/研究院）。
#: 这里的语义是"出现机构名就弹窗"，**研究所≠证券公司**：
#: `XX研究所` 在本项目的语料里大量出现于"研究员/研究所观点"这类泛指，
#: 拿它当"机构出现了"会明显放大弹窗量，而用户点名的形态只有"XX证券"。
#: 所以这里按用户的字面口径收窄成 `证券`。
BROKER_PATTERN: Final = re.compile(r"[\u4e00-\u9fff]{2,8}证券")

#: 券商名里"证券"之前的最多字数（= 上面 `{2,8}` 的上界）。
_MAX_BROKER_STEM: Final = 8

# ======================================================================
# 触发条件 (a')：**署名形态**（简称 + 研究语境后缀）—— 用户 2026-10-01 追加
# ======================================================================

#: 用户口径（2026-10-01 原文）：
#:
#: > "券商名字不一定带券商两个字，比如可能是天风电子，招商电子，
#: >  所以可以搜索国内大型研究机构的名字、研报喜欢用的名字，加进去。"
#:
#: ## 为什么 `[\u4e00-\u9fff]{2,8}证券` 结构性地看不到这些名字
#:
#: 研报署名最常见的形态是**机构简称 + 行业组名**：
#: `【天风电子】`、`【华福电新】`、`【中泰汽车】`、`【招商机械】` ——
#: 它们**一个"证券"字都没有**，所以旧形态一条都命中不了，
#: 界面上表现为"这条没有机构名"（而原文里明明写着）。
#:
#: ## 为什么不能只加一张"裸简称"清单
#:
#: 裸简称**歧义极重**：招商 / 中信 / 东方 / 光大 / 民生 / 兴业 / 平安 / 长城
#: 同时是银行、上市公司与普通词（招商银行、东方财富、平安保险、长城汽车…）。
#: 只写"命中招商"会把一大片无关内容标成机构。
#: 所以判据是 `(简称)(研究语境后缀)` **紧邻无间隔** —— 两个条件同时成立
#: 才认，这是"天风电子"与"长城汽车"之间唯一可用的区分（见下面的上市公司闸门）。
#:
#: ## ⚠️ 手工维护、**刻意不完整**
#:
#: 用户明确接受漏报："不可能完全识别完的，漏掉就漏掉吧"。
#: 这份名单只收**国内大型研究机构**与**研报里高频出现**的简称；
#: 不做拼音/编辑距离之类的模糊匹配（与 `ANALYST_WATCHLIST` 同一条纪律：
#: 命中即"原文逐字有"，模糊一次这条信息就作废）。
#:
#: 种子来自两处（都是**已经存在**的清单，不新造）：
#:   · 参考项目 `moss-finance-assistant` 的 `_ZSXQ_SYSTEM_PROMPT` 券商跳过清单
#:   · 本项目 `tone._BROKER_SKIP`（**只借用名字，不动那个常量** ——
#:     它的用途是"别把券商团队名当成股票"，判据与生命周期都不同，
#:     见它自己的说明）
#: 其余是研报里一眼能见到的头部机构简称。
BROKER_BASES: Final[tuple[str, ...]] = (
    # ── 参考项目的实测清单（`【天风电子】` 这一族）──
    "天风", "华福", "中信", "国金", "中泰", "东吴", "东北", "招商", "信达",
    # ── 头部机构简称 ──
    "国泰海通", "国泰君安", "海通", "华泰", "中金", "广发", "兴业", "民生",
    "长江", "光大", "浙商", "东方", "国信", "申万宏源", "申万", "方正",
    "国投", "平安", "开源", "华创", "德邦", "财通", "西部", "太平洋",
    "首创", "华安", "中银", "国海", "华西", "长城", "东兴", "华宝", "中航",
    "国联民生", "中信建投", "银河", "财信", "华鑫", "华龙", "万联", "国元",
    "华金", "东海", "湘财", "江海", "山西", "红塔", "川财", "中邮", "华林",
    "东亚前海", "甬兴", "华兴", "第一创业", "瑞银", "摩根", "高盛", "野村",
)

#: **研究机构**后缀。命中即"这是一次机构署名"。
#:
#: ⚠️ `银行` / `基金` **刻意不收**（用户口径允许漏、不允许误报）：
#: `招商银行`、`平安银行`、`中银基金` 是**普通上市公司与资管机构**，
#: 而不是"发布研报的研究所"。收进来只会让"机构：招商银行"这种
#: 明显不对的标签上屏，而它恰恰是本项目最忌讳的"看起来完全合理的错"。
#: 少认几个基金公司的署名是可接受的代价（用户："漏掉就漏掉吧"）。
BROKER_ORG_SUFFIXES: Final[tuple[str, ...]] = (
    "证券", "研究所", "研究部", "研究院",
)

#: **行业组**后缀 —— 研报署名里跟在机构简称后面的那一截
#: （`天风` + `电子`、`华福` + `电新`、`中泰` + `汽车`…）。
#:
#: ⚠️ 这一族**必须再过一道"是不是上市公司"的闸门**（见 `_is_listed_company`）：
#: 同一个形态既能是署名（`天风电子`）也能是公司名（`长城汽车` = 长城 + 汽车）。
#: 只靠词形分不开，只能问名录。
BROKER_INDUSTRY_SUFFIXES: Final[tuple[str, ...]] = (
    "电子", "电新", "计算机", "通信", "传媒", "汽车", "机械", "军工",
    "有色", "化工", "家电", "食饮", "医药", "农业", "地产", "建材",
    "建筑", "交运", "钢铁", "煤炭", "石油", "环保", "公用", "轻工",
    "纺服", "社服", "商贸", "零售", "美容", "宏观", "策略", "固收",
    "非银", "中小盘", "海外",
    # ── 参考项目实测署名里出现过、上面那批没覆盖到的组名 ──
    # `国金AI金属` / `东北商业航天` / `信达消费` 都是**整块**后缀：
    # 简称与后缀必须紧邻，所以 `AI金属` 不能拆成 `AI` + `金属`
    # （拆了既匹配不上，又会把 `AI` 变成一个到处乱撞的后缀）。
    "消费", "商业航天", "AI金属",
    # ── 常见行业组（同样是"研报喜欢用的名字"）──
    # ⚠️ 它们与上市公司重名的概率最高（`长江电力` / `国投电力`），
    # 全靠下面 `_is_listed_company` 那道闸门兜住 —— 这也是为什么闸门
    # 必须覆盖**所有**行业组后缀，而不是只给"汽车"开小灶。
    "石化", "电力", "新能源", "半导体", "光伏", "储能", "机器人",
)

#: 全部后缀（建正则用）。**长词优先**：`研究所` 必须先于任何短词被尝试，
#: 否则 `研究`（若将来加进来）会把 `研究所` 切掉半截。
_BROKER_SUFFIXES: Final[tuple[str, ...]] = tuple(sorted(
    set(BROKER_ORG_SUFFIXES) | set(BROKER_INDUSTRY_SUFFIXES),
    key=len, reverse=True))

#: 后缀的匹配形态。**不加边界断言**（与 `BROKER_PATTERN` 同一条理由）：
#: 署名后面紧跟团队/行业名是常态（`天风电子团队`、`招商机械：`）。
_BYLINE_SUFFIX_RE: Final = re.compile(
    "|".join(re.escape(s) for s in _BROKER_SUFFIXES))

#: 简称按**长度降序**（长的先试）。原因：`国联民生证券` 里
#: `民生` 也是一个简称，短匹配会切出一个并不存在的"民生证券"署名。
#:
#: ⚠️ 不做"按长度切片查集合"那类微优化：实测两者在真实文本上都是
#: **~0.36ms/条**（一条 2280 字、含 80 处署名的笔记），成本几乎全在
#: 后缀正则的扫描上，简称查找根本不是瓶颈。这条扫描每次请求最多跑
#: 500 条 × 2 遍（机构 + 分析师），留在"看得懂"的写法上更划算。
_BASES_BY_LEN: Final[tuple[str, ...]] = tuple(
    sorted(BROKER_BASES, key=len, reverse=True))


def _is_listed_company(name: str) -> bool:
    """`name` 是不是**A 股名录里的上市公司**（用于挡掉 `长城汽车` 这类误报）。

    ## 为什么需要这道闸门

    `(简称)(行业组)` 这个形态本身是**歧义**的：

        天风电子   天风(券商) + 电子(行业组)   ← 署名，要认
        长城汽车   长城(券商简称) + 汽车(行业组) ← 上市公司名，**不能**认

    词形上两者一模一样（都是 4 个汉字、都能在原文里逐字找到），
    唯一可靠的判据是"这个名字在不在 A 股名录里"。项目已经有那份名录
    （`vocab` 读 `quant_stock_directory`，进程内缓存），直接用它 ——
    自己再维护一张"像机构的公司名"清单必然漂移。

    ## ⚠️ 只用于**行业组后缀**那一路

    `证券` 后缀**不走**这道闸门：`中信证券`、`东方证券`、`长城证券`
    本身就是上市公司，也正是要认的机构名（用户点名的形态就是"XX证券"）。

    ## 词表读不到时**放行**（不做这道闸门）

    与 `tone._resolve_industry` 同一条取舍：数据仓缺失时"一律丢弃"会让
    用户点名要的机构名**整块消失**且没有任何报错线索；宁可这时偶尔多认
    一个（`长城汽车`），也不要静默清空。缺表本身有日志与 `vocab.stats()['gaps']`。
    """
    if not name:
        return False
    try:
        from src.domain.intel import vocab

        return bool(vocab.stock_code(name))
    except Exception:  # noqa: BLE001 词表读不到不该让机构名整块消失
        return False


def _byline_hits(text: str) -> list[tuple[str, int]]:
    """扫 `(简称)(研究语境后缀)` 形态，返回 `(机构名, 位置)`。

    实现是"先后缀、再往前找简称"，不是把两个清单做笛卡尔积的正则：
    简称 60+ × 后缀 40 = 2400 个分支的交替式，编译与回溯成本都不划算，
    而这里的两次查表是 O(文本长度 + 命中数 × 简称数)，完全可预测。

    ⚠️ 同一处**只认最长的简称**：`国联民生证券` 里 `民生` 也是简称，
    短匹配会多切出一个原文里并不存在的"民生证券"署名。
    区间一旦被接受就**不再重叠**（`taken`），避免同一个字被算成两家机构。

    ⚠️ 名字永远是**原文的切片**（`text[start:end]`）：用户要拿它去原文核对，
    逐字可核是这条信息的全部价值（同 `broker_name` 的说明）。
    """
    if not text:
        return []
    out: list[tuple[str, int]] = []
    taken: list[tuple[int, int]] = []
    for m in _BYLINE_SUFFIX_RE.finditer(text):
        suffix = m.group(0)
        head = text[:m.start()]
        base = ""
        for cand in _BASES_BY_LEN:        # 长的先试，命中即停
            # ⚠️ 用 `head.endswith(cand)` 而不是 `text.startswith(cand, i)`：
            # 实测后者反而**慢一倍**（同一段 2280 字/80 处署名：0.80ms vs 0.37ms，
            # 5 轮取 min/max 无重叠）—— `startswith` 带起点参数走的是另一条实现路径。
            # 记在这里，免得有人"顺手优化"成更慢的写法。
            if head.endswith(cand):
                base = cand
                break
        if not base:
            continue
        start = m.start() - len(base)
        end = m.end()
        if any(start < e and s < end for s, e in taken):
            continue
        name = text[start:end]
        # 行业组后缀要过"是不是上市公司"这道闸门（`长城汽车` / `东方通信`）
        if suffix in BROKER_INDUSTRY_SUFFIXES and _is_listed_company(name):
            continue
        taken.append((start, end))
        out.append((name, start))
    return out


def is_research_house(name: str) -> bool:
    """`name` 是否**已被识别为研究机构名**（两种形态都算）。

    单列成公开函数是为了让"哪些名字是机构"**只有一处判据**：
    抽取侧（`tone._valid_name`）要拿它做硬拦截 —— 用户口径是
    "目的就是找到那些**股**被唱多，唱空"，机构名混进个股列表会直接把
    这个问题的答案污染掉，而它长得完全合理（原文里逐字有）。

    ⚠️ 判据是"**整个名字**恰是一次机构署名"，不是"包含机构名"：
    `天风电子` 是机构，`天风电子产业链` 不是（那是一个短语）。
    所以 `证券` 那一路用 `fullmatch`，署名那一路要求扫出来的区间
    与入参完全相等。

    ⚠️ 上市公司闸门**照常生效**：`长城汽车` 走 `(简称)(行业组)` 形态时
    会被 `_is_listed_company` 挡掉，于是这里返回 `False` —— 它本来就是
    一只股票，抽取侧**不该**拒它（这正是两道闸门要分开写的原因）。
    """
    s = str(name or "").strip()
    if not s:
        return False
    if BROKER_PATTERN.fullmatch(s):
        return True
    return any(hit_name == s for hit_name, _ in _byline_hits(s))


def broker_name(matched: str) -> str:
    """把正则命中规整成机构名（在命中串内部切出**以"证券"结尾**的那一段）。

    ## 为什么必须规整（实测的脏形态）

    裸正则在**没有分隔符**的汉字长串上会多带前缀：

        `内内内内内内内国金证券研究服务` → 命中 `内内内内内内国金证券`

    `内内内内内内` 是正文，不是机构名。所以从命中串里找"证券"、往前最多取
    `_MAX_BROKER_STEM` 个字 —— 得到 `国金证券`。

    ## 为什么这一定安全

    切出来的每一段都是**命中串的子串**，而命中串本身是原文的子串 ⇒
    规整后的名字**必然逐字出现在原文里**，用户拿去核对一定找得到。
    这里**不做**任何词表/模糊匹配（与 `ANALYST_WATCHLIST` 同一条纪律：
    名字的全部价值就是"命中即原文逐字有"，模糊一次就作废）。
    所以最坏情况是"多带前缀"，**不会**是一个原文里不存在的名字。

    ⚠️ 找不到"证券"时返回**空串**而不是原样返回：调用方据此**丢掉**这次命中。
    返回一个不以"证券"结尾的串会让弹窗理由写"命中机构「xxxx」"，
    而那根本不是机构名 —— 宁可漏这一条（模式本身以"证券"结尾，几乎不可能）。
    """
    s = str(matched or "")
    end = s.rfind("证券")
    if end < 0:
        return ""
    return s[max(0, end - _MAX_BROKER_STEM):end + len("证券")]


def _broker_hits(text: str) -> list[tuple[str, int]]:
    """扫出 `(机构名, 位置)` 列表 —— **两条形态都扫**。

    1. `XX证券`（`BROKER_PATTERN`，用户最初的字面口径），经 `broker_name` 规整
    2. `简称 + 研究语境后缀`（`_byline_hits`，用户 2026-10-01 追加：
       "券商名字不一定带券商两个字，比如可能是天风电子，招商电子"）

    两条**相加**而不是替换：`中泰证券` 那种写法今天仍然满屏都是。

    ⚠️ `offset` 取**规整后名字**在原文里的位置，不是命中串的起点 ——
    多带的前缀字数不确定，用命中起点会让 `in_summary`（"用户在卡片上
    看不看得见这个名字"）以一个错的坐标为基准。
    两条形态产出的名字都是**原文切片**，所以这个坐标永远有效。

    ⚠️ 同名只留**最靠前**的那次：两套形态会命中同一个名字
    （`中泰证券` 两条都认），前端只需要一个"最先出现的位置"。

    ⚠️ **署名形态优先于粗匹配**：`内内内内内内内国金证券研究服务` 这种
    没有标点的长串里，`BROKER_PATTERN` 会多带前缀（切成"内内内内内内国金证券"），
    而署名形态从名录里认出 `国金` + `证券`、给出干净的名字。
    两者**区间重叠**时丢掉粗匹配那一条 —— 让用户看到"机构：国金证券"
    而不是"机构：内内内内内内国金证券"。名录之外的机构名照旧走粗匹配
    （那种情况下它仍是唯一来源，粗糙面与原来一样）。
    """
    bylines = _byline_hits(text)
    out: list[tuple[str, int]] = list(bylines)
    byline_spans = [(off, off + len(n)) for n, off in bylines]
    for m in BROKER_PATTERN.finditer(text):
        raw = m.group(0)
        name = broker_name(raw)
        if not name:
            continue
        shift = raw.rfind(name)
        offset = m.start() + (shift if shift >= 0 else 0)
        end = offset + len(name)
        if any(offset < e and s < end for s, e in byline_spans):
            continue          # 区间被署名命中覆盖 → 用那条干净的名字
        out.append((name, offset))

    best: dict[str, int] = {}
    for name, offset in out:
        if name not in best or offset < best[name]:
            best[name] = offset
    return sorted(best.items(), key=lambda kv: kv[1])

# ======================================================================
# 命中结果
# ======================================================================

#: 触发类别（用户口中的"人"/"机构"，以及模型给的"方向"）。
TRIGGER_ANALYST: Final = "analyst"
TRIGGER_BROKER: Final = "broker"
TRIGGER_DIRECTION: Final = "direction"


@dataclass(frozen=True)
class ContentHit:
    """一次内容规则命中。**每一格都要能追到原文**。"""

    #: `analyst` | `broker`
    kind: str
    #: 命中的字面词（"孙潇雅" / "国金证券"）—— 直接可拿去原文核对
    name: str
    #: 命中位置（清洗后全文里的字符下标；在**展示摘要**里扫到时为 -1）
    offset: int
    #: 命中是否落在**展示摘要**范围内（决定理由要不要提示"摘要未显示"）
    in_summary: bool
    #: 扫的是哪一段（`extract_text` / `display`）—— 排障用，不出接口
    source: str

    @property
    def is_analyst(self) -> bool:
        return self.kind == TRIGGER_ANALYST

    def describe(self) -> str:
        """面向用户的一句话理由（**不含来源标识**）。

        ⚠️ 摘要之外的命中必须**明说**。用户看到的卡片只有标题 + 260 字摘要，
        理由里写一个他在卡片上找不到的名字，他会认为系统在编 ——
        "看起来完全合理的错"是本项目最忌讳的一类失败。
        """
        who = "分析师" if self.is_analyst else "机构"
        tail = "" if self.in_summary else "（正文后段，展示摘要未显示）"
        return f"命中{who}「{self.name}」{tail}"


# ======================================================================
# 扫描
# ======================================================================

#: 展示摘要的最大字符数（与 `intel_sources.SUMMARY_MAX_BY_KIND["research_note"]` 同值）。
#:
#: ⚠️ 这里**重复**了那个数字，是刻意的：那个常量在连接器层（构造契约），
#: 这个常量在规则层（判断"用户能不能看见这个词"）。两者语义不同 ——
#: 哪天展示长度调了，这里**应当**跟着调，但必须是有人**看懂后**手动调，
#: 而不是被一次 import 悄悄改掉判断口径（改错的表现是"理由说摘要里有、
#: 用户却看不见"，很难查）。所以加一条测试钉住两者一致。
SUMMARY_CLIP: Final = 260


def _display_text(item: dict[str, Any]) -> str:
    """标题 + 摘要 —— 用户**在卡片上真正看得到**的那段文本。

    它同时是两个问题的答案：
      · 全文不可用时扫哪段（兜底）
      · "这个命中用户能不能看见"（`in_summary`）—— 理由能否被核对的关键
    """
    return f"{item.get('title') or ''} {item.get('summary') or ''}".strip()


def _scan_text(item: dict[str, Any]) -> str:
    """决定**扫哪一段文本**：优先全文（`extract_text`），否则展示文本。

    ## 为什么是 `extract_text`（而不是只扫 `summary`）

    见模块 docstring 的实测：券商署名常落在正文后段（真实笔记里
    `国金证券` 在第 **695** 字），而展示摘要只截到 260 字 —— 只扫摘要
    会**漏掉**那类条目，而"带机构署名的研报"正是用户要找的东西。

    ## ⚠️ 为什么不把"全文 + 展示文本"两段都扫一遍再合并

    试过，是错的：`extract_text` 是**清洗后的全文**，`summary` 是它的
    截断切片 ⇒ 同一个命中会被扫到两次，而两次的 `offset` 落在**不同的
    坐标系**里（全文下标 vs "标题 摘要"的下标），`in_summary` 的比较随之
    失去意义 —— 结果是把一条本来"摘要里就能看见"的命中标成
    "正文后段，展示摘要未显示"，理由反而更不可核对。
    所以这里**只扫一段**，坐标系始终是那一段。

    ⚠️ `extract_text` **只在进程内存在**：`IntelFeed.to_public()` 会剥掉它
    （它是全文，发到浏览器就违反了"摘要截断"的展示契约）。所以这条路只在
    `_intel_signal_alert` 这类**内部消费者**上成立；从接口来的 dict 没有
    这个键，自动退回展示文本 —— 不会 KeyError，也不会静默变成"什么都没扫"。
    """
    raw = item.get("extract_text")
    if isinstance(raw, str) and raw.strip():
        return raw
    return _display_text(item)


def _scan(text: str, *, source: str, display_len: int) -> list[ContentHit]:
    """在一段文本上扫名单与券商形态，返回全部命中（去重、首现顺序）。

    `display_len`（展示文本长度）与 `offset` 的比较是 `in_summary` 的**唯一**
    判据：扫全文时"位置 < 展示长度"即"这段文字用户在卡片上看得见"。
    """
    if not text:
        return []
    positions: list[tuple[str, str, int]] = []
    for name in ANALYST_WATCHLIST:
        # 别名与同义映射一律不做：命中即"原文逐字有这个字面词"。
        i = text.find(name)
        if i >= 0:
            positions.append((TRIGGER_ANALYST, name, i))
    for name, offset in _broker_hits(text):
        positions.append((TRIGGER_BROKER, name, offset))
    out: list[ContentHit] = []
    for kind, name, offset in sorted(positions, key=lambda x: x[2]):
        out.append(ContentHit(
            kind=kind, name=name, offset=offset,
            # 摘要之外的命中**不是"没有位置信息"**，而是"位置在摘要之后"：
            # 两者在理由里说法不同（见 `describe`），不能混成一个布尔。
            in_summary=offset < display_len,
            source=source,
        ))
    return _dedup(out)


def content_hits(item: dict[str, Any]) -> list[ContentHit]:
    """一条情报命中的**人 / 机构**（可能为空）。

    ⚠️ **任何时候都不抛异常**。这是纪律不是风格：告警链路跑在调度任务里，
    一次异常就是"这轮不弹"，而表现只是"今天怎么没弹窗"——
    没有任何报错线索（与 `zsxq_incremental.fresh_floor` 那次
    "内容永久不可见"同属一类事故）。规则层出问题时**退化成"没有命中"**，
    由 (c) 模型方向那条路继续兜底。
    """
    try:
        display = _display_text(item)
        full = item.get("extract_text")
        has_full = isinstance(full, str) and bool(full.strip())
        return _scan(_scan_text(item),
                     source="extract_text" if has_full else "display",
                     display_len=len(display))
    except Exception:  # noqa: BLE001 规则层绝不能让告警链路失败
        return []


def _dedup(hits: list[ContentHit]) -> list[ContentHit]:
    """按 `(类别, 名称)` 去重，保留**位置最靠前**的那次命中。

    为什么保留最靠前的：`in_summary` 由位置决定，而"命中在摘要里"是更强的
    陈述（用户立刻能核对）。同一次命中在前面已经出现过时，后面的那次
    不该把它降级成"摘要未显示"。
    """
    best: dict[tuple[str, str], ContentHit] = {}
    for h in hits:
        key = (h.kind, h.name)
        cur = best.get(key)
        if cur is None or _rank(h) < _rank(cur):
            best[key] = h
    return sorted(best.values(), key=lambda h: (h.offset if h.offset >= 0 else 10**9,
                                                h.name))


def _dedup_names(names: list[str]) -> list[str]:
    """名字列表去重、保序、丢掉空串（展示用；不做任何改名/归并）。"""
    out: list[str] = []
    for n in names:
        s = str(n or "")
        if s and s not in out:
            out.append(s)
    return out


def _rank(h: ContentHit) -> tuple[int, int]:
    return (0 if h.in_summary else 1, h.offset if h.offset >= 0 else 10**9)


# ======================================================================
# 汇总成"给告警用的理由"
# ======================================================================

#: 方向触发在理由里的措辞。与 `alert_bridge` 的 `偏多/偏空` 词汇**刻意不同**：
#: 用户说的是"8b 模型分析出是利空或利好" —— 利多/利空是用户的口径，
#: 偏多/偏空是存储里的值。映射关系写在这里，**只此一处**（见下）。
DIRECTION_BULL_WORD: Final = "利多"
DIRECTION_BEAR_WORD: Final = "利空"

#: 存储里的倾向值 → 用户口径的方向词。
#:
#: ⚠️ **映射确认**（用户 2026-09-25 追加确认）：
#: "8b 模型分析出是利空或利好" / "被本地模型分析出是有利空或利多偏向的"
#: 就是既有的 `tone.has_tone` 且 `tone ∈ {偏多, 偏空}` —— 利多 = 偏多、
#: 利空 = 偏空，**不需要**再要求 `bullish`/`bearish` 桶非空。
#: 也就是说 (c) 这条触发**完全复用**既有的"明确方向"判据，
#: 这里只是把它的措辞翻成用户用的词（少一处自造词汇，就少一处漂移）。
TONE_TO_DIRECTION: Final[dict[str, str]] = {
    "偏多": DIRECTION_BULL_WORD,
    "偏空": DIRECTION_BEAR_WORD,
}


@dataclass(frozen=True)
class AlertReason:
    """一条情报**为什么**该弹窗 —— 两条触发条件各自的落地情况。

    ## ⚠️ 机构命中**不算触发**（用户 2026-10-01 改口径）

    用户原话："券商名不一定要告警，但是一定要前端输出信息。"

    所以本类的语义边界是：

        `hits` / `institutions`   **展示用**：命中了谁、哪家机构（前端要看到）
        `fires`                    **触发用**：只在"人"或"方向"成立时为真
    """

    #: 内容命中（分析师 / 机构），可能为空
    hits: tuple[ContentHit, ...] = ()
    #: 用户口径的方向词（`利多` / `利空`）；空串 = 模型没给出明确方向
    direction: str = ""
    #: 命中的**标的**（个股优先，见 `alert_bridge.stock_entries`）
    stocks: tuple[dict[str, Any], ...] = ()

    @property
    def analysts(self) -> tuple[ContentHit, ...]:
        """命中的**分析师**（触发条件 a）。"""
        return tuple(h for h in self.hits if h.is_analyst)

    @property
    def institutions(self) -> tuple[ContentHit, ...]:
        """命中的**机构**（券商名）。

        ⚠️ **只用于展示**（前端"机构：中泰证券"），**不触发告警** —— 见 `fires`。
        """
        return tuple(h for h in self.hits if not h.is_analyst)

    @property
    def has_analyst(self) -> bool:
        return bool(self.analysts)

    @property
    def has_direction(self) -> bool:
        return bool(self.direction)

    @property
    def fires(self) -> bool:
        """两条触发条件 **OR** —— 任何一条成立就弹。

        ⚠️ **机构名不在这里**（改口径后的关键差异）：
        只有 `命中分析师` 或 `模型给出方向` 才弹；一条只提到"中泰证券"、
        既不点名分析师、模型也没判出方向的笔记 **不弹**，
        但它的机构名仍会出现在前端（`institutions`）。

        这条差异必须靠测试钉住：把 `hits` 直接写进 `fires`（改口径前的写法）
        会让"只有券商名"的条目也弹窗 —— 那是用户明确否掉的行为。
        """
        return self.has_analyst or self.has_direction

    def triggers(self) -> list[str]:
        """触发了哪几条（`analyst` / `direction`）—— 供事件与排障读。

        ⚠️ 不返回 `broker`：它不是触发条件（机构命中会走 `institutions`）。
        """
        out: list[str] = []
        if self.has_analyst:
            out.append(TRIGGER_ANALYST)
        if self.has_direction:
            out.append(TRIGGER_DIRECTION)
        return out

    def describe(self) -> str:
        """**弹窗理由**：只说**触发**的那几条（逐字可核对，不含来源标识）。

        形如：`命中分析师「孙潇雅」；模型判为利多`

        ⚠️ 机构名**不进这里**：一条笔记命中了 5 家券商但只有模型方向触发时，
        理由里堆一串机构名会让人以为"是这些机构让它弹的" ——
        而实际触发的是模型方向。机构名去 `institutions_display()`（前端那一侧）。
        """
        parts: list[str] = []
        if self.analysts:
            parts.append("、".join(h.describe() for h in self.analysts))
        if self.has_direction:
            parts.append(f"模型判为{self.direction}")
        return "；".join(parts)

    def institutions_display(self) -> list[str]:
        """**前端展示**用的机构名列表（用户口径："一定要前端输出信息"）。

        返回的是机构名本身（`["中泰证券"]`），不带"命中机构「」"那种包装 ——
        界面上要显示成"机构：中泰证券"，标签由前端那一层加
        （理由文案与字段值分家，免得前端还要从一句话里抠名字）。
        """
        return _dedup_names([h.name for h in self.institutions])

    def analysts_display(self) -> list[str]:
        """**前端展示**用的分析师名列表（与 `institutions_display` 同一形状）。"""
        return _dedup_names([h.name for h in self.analysts])


def institutions(item: dict[str, Any]) -> list[str]:
    """一条情报里出现的**机构名**（券商名）—— **前端展示用**。

    ## 为什么与 `content_hits` 分开

    用户口径（2026-10-01）："券商名不一定要告警，但是一定要前端输出信息。"

    两条需求落在**两个不同的层**上，所以这里不共用同一个"扫哪里"的判据：

        触发（告警）  扫**清洗后全文**：署名在后段也要能弹（用户要的信号）
        展示（前端）  全文 + 展示文本**都扫**：宁可多给一个名字，
                      也不要让界面上漏掉一条"这条其实有机构"的信息
                      （展示多一个词没有任何代价，漏掉才是信息缺失）

    ⚠️ 返回值是**机构名字符串**（`["中泰证券"]`），不是 `ContentHit` ——
    它要直接进接口给前端渲染，带 offset/source 那些内部坐标没有意义。

    ⚠️ 与"来源平台"**毫无关系**：这里的名字是**内容里写的机构**
    （公开信息，研报本来就署名），不是"这条来自知识星球"那类渠道身份。
    渠道身份一律不出接口（`source_pseudonym` + `IntelItem.to_public` 白名单），
    这条纪律不受本函数影响。
    """
    try:
        item_hits = content_hits(item)
        names = [h.name for h in item_hits if not h.is_analyst]
        # 展示侧再扫一遍纯展示文本：全文不可用时它是唯一来源；
        # 全文可用时它补上"标题里的机构名"（`extract_text` 理论上含标题，
        # 但"包含"不该被当成保证 —— 见 `_scan_text` 的说明）。
        display = _display_text(item)
        full = item.get("extract_text")
        if display and display != (full if isinstance(full, str) else ""):
            names.extend(name for name, _ in _broker_hits(display))
        return _dedup_names(names)
    except Exception:  # noqa: BLE001 展示层失败退化成"没有机构名"，绝不抛
        return []


def analysts(item: dict[str, Any]) -> list[str]:
    """一条情报里出现的**分析师名**（名单内）—— 同样是**前端展示用**。"""
    try:
        return _dedup_names([h.name for h in content_hits(item) if h.is_analyst])
    except Exception:  # noqa: BLE001
        return []


def direction_word(tone: Any) -> str:
    """存储里的倾向 dict → 用户口径方向词（`利多`/`利空`/空串）。

    ⚠️ 判据与 `alert_bridge.direction_of` **完全一致**（`has_tone` 且
    非 `neutral`、值 ∈ {偏多, 偏空}），只是输出换成用户用的词。
    写成两个函数而不是"一个函数两套输出"，是因为两处的**消费方不同**：
    告警引擎要 `偏多/偏空`（既有契约），理由文案要 `利多/利空`（用户口径）。
    同源判据、不同表述 —— 判据漂移会同时错两处，所以这里直接复用
    `alert_bridge.direction_of`（传 `{"tone": …}` 的条目形状），
    不重复实现一遍"什么叫明确方向"。
    """
    if not isinstance(tone, dict):
        return ""
    from src.domain.intel.alert_bridge import direction_of

    return TONE_TO_DIRECTION.get(direction_of({"tone": tone}), "")


__all__ = [
    "ANALYST_WATCHLIST",
    "BROKER_BASES",
    "BROKER_INDUSTRY_SUFFIXES",
    "BROKER_ORG_SUFFIXES",
    "BROKER_PATTERN",
    "DIRECTION_BEAR_WORD",
    "DIRECTION_BULL_WORD",
    "SUMMARY_CLIP",
    "TRIGGER_ANALYST",
    "TRIGGER_BROKER",
    "TRIGGER_DIRECTION",
    "AlertReason",
    "ContentHit",
    "TONE_TO_DIRECTION",
    "analysts",
    "broker_name",
    "content_hits",
    "direction_word",
    "institutions",
    "is_research_house",
]
