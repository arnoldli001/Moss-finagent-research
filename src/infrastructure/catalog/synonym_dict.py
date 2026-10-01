"""同义词字典：把**用户/模型说的话**翻译成**库里的列名与实体代码**。

## 解决什么（用户报障："数据在库里却取不到"）

其中一个**具体**原因是名词对不上，而且是双向对不上：

| 用户说 | 库里是 | 现状 |
|---|---|---|
| 「营收」 | `revenue` / 「营业总收入」 | 认不出 |
| 「股息率」 | `dv_ratio` | 认不出 |
| 「招商银行」「招行」 | `600036` | 认不出 |
| 「CATL」「宁德」 | `300750` | 认不出 |
| 「通胀」 | `CPI` | 认不出 |

原先只有零散硬编码（`local_data.py::_METRIC_ALIASES` 内置十几个），
**没有实体别名**，也没有一份可维护、可审计、可被护栏测试读的字典。
本模块就是那一份。

## ★ 匹配强度 = **最长匹配跨度**，不是"别名长度"

判据是**别名在查询串里实际匹配了多长**（完全相等 > 别名是查询的子串 >
查询是别名的子串），实现在 `local_data.py::_alias_match_span` —— 本模块
**直接 import 它，不复制**（"同一判断只允许一份实现"）：

    查询『股息率』    → 『股息率』跨度 3 > 『股息率ttm』跨度 0  → 选 `dv_ratio` ✅
    查询『股息率ttm』 → 『股息率ttm』跨度 6 > 『股息率』跨度 3  → 选 `dv_ttm`   ✅

这不是小事：两个口径**数值不同**（实测 600036 `dv_ratio=5.05` /
`dv_ttm=5.76`），错的那个**看起来完全正常**。所以本模块有两个方向都断言的
回归护栏（`tests/unit/test_synonym_dict.py`）。

## API

    resolve_metric(text) -> list[str]   指标名 → 候选标准列名/指标 id（匹配强度降序）
    resolve_entity(text) -> list[str]   实体名 → 候选 6 位代码（匹配强度降序）
    metric_aliases()     -> dict        指标别名字典（供护栏/审计读）
    entity_aliases()     -> dict        实体别名字典（供护栏/审计读）
    generated_entity_aliases() -> dict  可由数据生成的实体别名（同上，来源可复现）
    dict_stats()         -> dict        规模自述（别名字典不许退化成空壳）

**返回候选列表而不是单值**：解析本来就可能歧义（「平安」既是
中国平安也是平安银行；`zsyh` 既是招商银行也是浙商银行）。
"选一个"不算错，"不说"才是错 —— 所以按强度降序返回全部候选，
由调用方（连接器 / 取数链路）按序试。

**空串 / 纯符号 / 不存在的词 → 返回空列表**，不抛异常、不编造
（**故意不做"原文兜底"**：把查询原样当候选返回，等于让"我没认出这个词"
看起来像"我认出来了"）。

## 可扩展：加一个别名 = 加一行

两个表都是模块级 `dict[str, tuple[str, ...]]`，别名 → 目标元组：

    "股息率": ("dv_ratio", "dividend_yield", "股息率", "dv_ttm"),   # 指标：别名 → 候选列名
    "招行":   ("600036",),                                         # 实体：别名 → 候选代码

两处都有**导入期自检**：任何两个别名在规范化后撞成同一个 key（例如同时写了
`"PE_ttm"` 与 `"pettm"`）会**直接抛 `ValueError`**，而不是后写覆盖先写
（静默覆盖正是本项目反复踩过的那类错误）。

## 已知边界（诚实登记，不假装覆盖）

1. **拼音只收全表唯一的拼写**。见 `ALIAS_SOURCE_NOTE` ③ —— 实测 18 条拼音
   形式因与**字典外**实体同拼写而**故意不收**（`zsyh` 同时是招商银行与
   浙商银行；`ylgf` 同时是伊利股份与另外 11 只）。宁可不认，也不认错。
2. **只覆盖 A 股 6 位代码**，且前缀白名单来自实测的 14 个前缀
   （北交所旧代码 `43x`/`83x`/`87x` 不在名称表内 → 不识别）。港股/美股/ETF
   不在本字典范围。
3. **「十年国债」未收录**：`supervisor.py` 的白名单里有这个词，但仓库里
   查不到对应的 indicator id（只有 `shibor_3m`），不凭记忆编一个。
4. 宏观指标的**中/美同名不同源**：用户说「CPI」既可能指中国 `CPI` 也可能指
   美国 `us_cpi_yoy`，所以按"中国口径优先、美国口径兜底"排候选序，
   **不合并**成一条（合并会让"这是哪个国家的 CPI"彻底不可见）。
"""
from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path

from src.infrastructure.catalog.local_data import _alias_match_span

logger = logging.getLogger(__name__)

__all__ = [
    "ALIAS_SOURCE_NOTE",
    "dict_stats",
    "entity_aliases",
    "extract_code",
    "generated_entity_aliases",
    "metric_aliases",
    "normalize_alias",
    "reset_caches_for_test",
    "resolve_entity",
    "resolve_metric",
]


# ============================================================
# 来源与边界（给审计脚本与下一个人读）
# ============================================================

ALIAS_SOURCE_NOTE: str = """\
本字典的每一条都有可复现来源，分三类 —— **不要往这里加"我记得好像是"的条目**。

① 人工维护（指标侧全部 + 实体侧的中文简称/英文简称）
   · 指标侧目标列名/指标 id **全部**取自仓库既有事实，逐条出处：
       - quant 面板列   `src/quant/panels.py::BASIC_FIELDS` / `PRICE_SOURCES`
                        / `WAREHOUSE_PRICE_FIELDS`（pe/pe_ttm/pb/ps/ps_ttm/
                        dv_ratio/dv_ttm/turnover_rate/volume_ratio/total_mv/
                        circ_mv/close/volume_lot/amount …）
       - 财务规范化字段  `src/quant/fundamental_source.py::YJBB_FIELD_MAP`
                        / `SINA_INDICATOR_FIELD_MAP`（revenue/net_profit/eps/
                        bps/roe/roa_sina/current_ratio/quick_ratio/
                        debt_to_assets/gross_margin …）
       - 连接器指标 id   `src/infrastructure/connectors/akshare_connector.py`
                        （_QUANT_COLUMN_INDICATORS / _FIN_RATIO_INDICATORS /
                        _DERIVED_STOCK_INDICATORS / _NBS_PRICE_SERIES /
                        _MACRO_SERIES / _US_MACRO_SERIES）
       - 宏观/流动性 id  `src/orchestration/supervisor.py`（白名单）、
                        `src/mainline/datastore.py`（cn_pmi/shibor）、
                        `configs/mainline_macro_sensitivity.yaml`
   · 实体侧「招行/工行/茅台/宁德」这类口语简称是**人工判定**的：
     判据是"在中文投研语境里该词**只**指这一家"；有歧义的词一律不收
     （「平安」是唯一的例外，见 `_ENTITY_ALIASES` 里那一行的注释）。
   · 实体侧英文简称（cmb/icbc/ccb/boc/catl/byd/longi）同样是人工判定。

② 由数据生成（可复现，命令见 ④）
   · 实体**代码↔中文名**：来自 `data/security_names.json`
     （`akshare.stock_info_a_code_name` 的落盘缓存；本机快照 5568 只）。
     本文件里 260 条别名的代码**没有一条是手写的** —— 都是这个表查出来的。
   · 实体**拼音**（首字母 + 全拼）：pypinyin 纯本地生成
     （与 `src/quant/stock_directory.py` 同一套 `lazy_pinyin`），
     且**只收全表唯一的拼写**（见 ③）。
   · 实体**全市场中文名**：`generated_entity_aliases()` 在运行期直接读
     `data/security_names.json` 现算（**只读本地磁盘，绝不联网**），
     所以 5568 只标的的中文名都能解析，不只本文件收录的 76 只。

③ 边界：**拼音只收全表唯一的拼写**（实测 18 条被丢弃，名单在 ④ 的输出里）
   理由：拼音首字母在 5568 个名称里**大量撞车**，收进来就是"认错"而不是"认不出"：
       zsyh → 招商银行 600036 / **浙商银行 601916**
       zgyh → 中国银行 601988 / **中国银河 601881**
       htzq → 华泰证券 601688 / **红塔证券 601236**
       wly  → 五粮液 000858 / **万里扬 002434**
       ylgf → 伊利股份 600887 / 另外 **11 只**（000807/002126/002725/002846/
              300174/300230/300956/603308/603969/605303/688190）
   实测：76 只收录实体里，首字母全表唯一的 **59/76**、全拼唯一的 **75/76**
   （唯一被丢的全拼是 `tongweigufen`：通威股份 600438 / 同为股份 002835）。
   → 结果：**134 条拼音别名**收录，18 条丢弃。若某个歧义拼音是高频入口，
   请**人工**加一行显式别名并在本注释登记该取舍；不要放宽这里的自动判据。

④ 复核 / 重新生成（不需要联网，用的是本地缓存快照）
   ```python
   import json, collections
   from pathlib import Path
   from pypinyin import Style, lazy_pinyin
   pairs = json.loads(Path("data/security_names.json").read_text(encoding="utf-8"))["pairs"]
   ini = lambda n: "".join(lazy_pinyin(n, style=Style.FIRST_LETTER, errors=list)).lower()
   idx = collections.defaultdict(list)
   for c, n in pairs:
       idx[ini(n.replace(" ", ""))].append(str(c))
   print([(c, n) for c, n in pairs if len(idx[ini(n.replace(" ", ""))]) > 1])
   ```
   代码前缀白名单（`_A_SHARE_PREFIXES`）也是这条命令同一份数据的产物。

⚠️ 名称表是**会变的**（新股上市 / 更名 / 退市）。本文件是**快照**，
   不随表漂移；漂移由护栏测试发现（`test_entity_names_agree_with_name_table`
   会在名称表里出现同名不同码时变红），不是靠人记得。
"""


# ============================================================
# 规范化（查询串与别名走**同一个**函数，否则匹配会单向失效）
# ============================================================

#: 规范化时**丢弃**的字符：空白 + 下划线 + 连字符 + 斜杠 + 各种括号。
#:
#: 为什么丢：`_alias_match_span` 判的是子串关系，若 `PE(TTM)` / `pe_ttm` /
#: `pettm` 不先归一，就得为同一个口径写三行别名 —— 而"三处写同一个东西"
#: 必然漏改一处。归一后**一行覆盖三种写法**。
#:
#: ⚠️ **不丢** `.`（要留给 `600036.SH`）与 `:`（留给 `fed:effr`，见下）。
_STRIP_CHARS = str.maketrans("", "", " \t\r\n_-/()（）[]【】")


def normalize_alias(text: str) -> str:
    """别名/查询串的**唯一**规范化：去空白与分隔符 + 小写。

    `fed:effr` 里的 `:` 也一并去掉，于是 `fed:effr` / `FED EFFR` / `fed-effr`
    归一后是同一个 key —— 但**返回值仍是原样的目标串**（`fed:effr`），
    所以规范化只影响"认不认得出"，不影响"返回什么"。
    """
    return str(text or "").translate(_STRIP_CHARS).replace(":", "").lower()


# 内部短别名（本文件里反复用）
_norm = normalize_alias


def _ascii_token_aligned(container: str, part: str) -> bool:
    """`part` 是否在 `container` 里以**完整字母数字词**出现（只用于纯 ASCII 串）。

    "完整词"= 左边是串首或非字母数字，**且**右边是串尾或非字母数字。
    调用点已保证两个串都是 ASCII，所以 `str.isalnum()` 不会碰到汉字。
    """
    start = container.find(part)
    while start != -1:
        end = start + len(part)
        left_ok = start == 0 or not container[start - 1].isalnum()
        right_ok = end == len(container) or not container[end].isalnum()
        if left_ok and right_ok:
            return True
        start = container.find(part, start + 1)
    return False


def _match_span(alias_key: str, query_key: str) -> int:
    """本模块的匹配跨度：**跨度本身完全来自** `local_data._alias_match_span`，
    这里只**否决**一类实测有害的匹配 —— **纯 ASCII 的巧合子串**。

    为什么必须否决（护栏实测抓出的跨族误命中）：

        resolve_metric("pe")
          · alias `pe`      → 精确 → 对 ✅
          · alias `pe_ttm`  → "pe" ⊂ "pettm" → 对（同一族，无害）
          · alias `fed:target_upper` → 归一后 `fedtargetupper`，
            而 `"pe"` 恰好是它的子串（…u**pp**er 里的 p+e？见下）→ **错** ❌

    实测结果：`pe` 的候选里混进了 `fed:target_upper` / `fed:policy_range`。
    调用方按候选序试，就会拿"市盈率"的问句去取**美联储利率** ——
    数字能取回来、看起来完全正常，这是本项目最贵的一类错误。

    修法只用一条判据：**纯 ASCII ↔ 纯 ASCII 的匹配必须是完整词**，
    含中文的一侧**完全沿用** `local_data` 的原判据（中文没有词边界，
    跨度的本意就是给中文用的）。于是本函数的结果**恒 ≤** 原函数
    （只减不增，不新增任何匹配），护栏测试断言这条性质。
    """
    span = _alias_match_span(alias_key, query_key)
    if span == 0 or alias_key == query_key:
        return span
    if not (alias_key.isascii() and query_key.isascii()):
        return span                      # 有一侧是中文 → 原判据原样生效
    if alias_key in query_key:           # 别名是查询的一部分
        return span if _ascii_token_aligned(query_key, alias_key) else 0
    if query_key in alias_key:           # 查询是别名的一部分
        return span if _ascii_token_aligned(alias_key, query_key) else 0
    return 0                             # 理论上到不了（span>0 必属上面两类）


def _build_index(table: dict[str, tuple[str, ...]],
                 kind: str) -> dict[str, tuple[str, ...]]:
    """把"可读别名"表编译成"规范化别名"索引，并在**导入期**抓撞车。

    为什么要抓：两个别名规范化后相同（如 `"PE_ttm"` 与 `"pettm"`）在 dict
    字面量里是**两个不同的 key**，但规范化后是一个 —— 后写的那条会**静默
    覆盖**先写的。本项目实测过同类缺陷（同一个 key 写在 3 处只改 1 处 →
    情报 5 个端点对所有人 403），所以这里让它**当场失败**而不是猜。
    """
    index: dict[str, tuple[str, ...]] = {}
    for alias, targets in table.items():
        key = _norm(alias)
        if not key:
            raise ValueError(f"{kind}别名为空：{alias!r}")
        if key in index:
            prev = next(a for a in table if _norm(a) == key and a != alias)
            raise ValueError(
                f"{kind}别名规范化后撞车：{alias!r} 与 {prev!r} 都归一为 {key!r}"
                "（改掉其中一个，或合并成一行）")
        index[key] = tuple(targets)
    return index


# ============================================================
# 指标别名表（别名 → 候选标准列名 / 指标 id，**按强度降序**）
# ============================================================
#
# 顺序契约：**标准列名优先，连接器指标 id 兜底**。
# `LocalDataExecutor` 按列名在库里找列；`SmartFetcher` 按 indicator id 走契约。
# 两边的消费者共用这一张表，所以候选里两种形态都有 —— 前者在前。
_METRIC_ALIASES: dict[str, tuple[str, ...]] = {
    # ---------------- 估值 ----------------
    "市盈率": ("pe_ttm", "pe", "PE(TTM)"),
    "市盈率ttm": ("pe_ttm", "pe", "PE(TTM)"),
    "滚动市盈率": ("pe_ttm", "pe", "PE(TTM)"),
    "pe": ("pe", "pe_ttm", "PE(TTM)"),
    "pe_ttm": ("pe_ttm", "pe", "PE(TTM)"),
    "市净率": ("pb", "PB"),
    "pb": ("pb", "PB"),
    "市销率": ("ps_ttm", "ps", "市销率"),
    "市销率ttm": ("ps_ttm", "ps", "市销率"),
    "ps": ("ps", "ps_ttm", "市销率"),
    "ps_ttm": ("ps_ttm", "ps", "市销率"),
    # ⚠️ 股息率族的两个口径**数值不同**，是本模块最长匹配的头号回归现场：
    #      「股息率」→ dv_ratio（静态，最近一期分红/股价）
    #      「股息率TTM」→ dv_ttm（滚动十二个月）
    #    600036 实测 dv_ratio=5.05 / dv_ttm=5.76。
    "股息率": ("dv_ratio", "dividend_yield", "股息率", "dv_ttm"),
    "股息率ttm": ("dv_ttm", "dv_ratio", "dividend_yield"),
    "滚动股息率": ("dv_ttm", "dv_ratio"),
    "dv_ratio": ("dv_ratio", "dividend_yield"),
    "dv_ttm": ("dv_ttm", "dv_ratio"),
    # 口径提醒：「分红率」严格说是"股利支付率 = 分红/净利润"，与"股息率 =
    # 分红/股价"**不是一回事**。本地没有股利支付率这一列，所以给最接近的
    # 股息率族并在此登记差异 —— 宁可选近似并说明，也不要静默返回空。
    "分红率": ("dv_ratio", "dividend_yield", "股息率", "dv_ttm"),
    "股利支付率": ("dv_ratio", "dividend_yield", "股息率", "dv_ttm"),

    # ---------------- 行情 ----------------
    "收盘价": ("close", "close_basic", "收盘", "stock_close"),
    "收盘": ("close", "close_basic", "stock_close"),
    "close": ("close", "close_basic"),
    "最新价": ("close", "close_basic", "stock_close"),
    "股价": ("close", "close_basic", "stock_close"),
    # ⚠️ 量列名有两套口径，**不是同一套**（`panels.py` 明写）：
    #   仓库 `daily` 面板 = `volume_lot`（**手**，Tushare `daily.vol`）
    #   `quant/price_panel.py` = `volume`（**股**）
    # 所以两个都当候选，由取数链路按实际列名命中。
    "成交量": ("volume", "vol", "volume_lot"),
    "volume": ("volume", "vol", "volume_lot"),
    "volume_lot": ("volume_lot", "volume", "vol"),
    "成交额": ("amount",),
    "成交金额": ("amount",),
    "amount": ("amount",),
    "换手率": ("turnover_rate", "turnover_rate_f", "换手率"),
    "turnover_rate": ("turnover_rate", "turnover_rate_f", "换手率"),
    "自由流通换手率": ("turnover_rate_f", "turnover_rate"),
    "量比": ("volume_ratio", "量比"),
    "volume_ratio": ("volume_ratio", "量比"),
    "开盘价": ("open",),
    "最高价": ("high",),
    "最低价": ("low",),

    # ---------------- 市值 / 股本 ----------------
    "总市值": ("total_mv", "市值", "总市值"),
    "市值": ("total_mv", "circ_mv", "市值"),
    "total_mv": ("total_mv",),
    "流通市值": ("circ_mv", "流通市值"),
    "circ_mv": ("circ_mv",),
    "总股本": ("total_share",),
    "流通股本": ("float_share",),
    "自由流通股本": ("free_share",),

    # ---------------- 财务：偿债 ----------------
    "资产负债率": ("debt_to_assets", "资产负债率(%)", "资产负债率"),
    "负债率": ("debt_to_assets", "资产负债率(%)", "资产负债率"),
    "流动比率": ("current_ratio", "流动比率"),
    "current_ratio": ("current_ratio", "流动比率"),
    "速动比率": ("quick_ratio", "速动比率"),
    "quick_ratio": ("quick_ratio", "速动比率"),
    "产权比率": ("产权比率(%)", "产权比率"),

    # ---------------- 财务：盈利质量 ----------------
    "roe": ("roe", "roe_sina", "净资产收益率(%)", "净资产收益率", "ROE"),
    "净资产收益率": ("roe", "roe_sina", "净资产收益率(%)", "净资产收益率", "ROE"),
    "净资产报酬率": ("roe", "roe_sina", "净资产收益率(%)", "ROE"),
    "加权净资产收益率": ("roe_waa_sina", "加权净资产收益率(%)", "roe", "ROE加权"),
    "加权roe": ("roe_waa_sina", "加权净资产收益率(%)", "ROE加权"),
    "roe加权": ("roe_waa_sina", "加权净资产收益率(%)", "ROE加权"),
    "roa": ("roa_sina", "总资产净利润率(%)", "ROA"),
    "总资产净利润率": ("roa_sina", "总资产净利润率(%)", "ROA"),
    "总资产净利率": ("roa_sina", "总资产净利润率(%)", "ROA"),
    "毛利率": ("gross_margin", "gross_margin_sina", "销售毛利率(%)", "毛利率"),
    "销售毛利率": ("gross_margin", "gross_margin_sina", "销售毛利率(%)", "毛利率"),
    "gross_margin": ("gross_margin", "gross_margin_sina"),
    "净利率": ("net_margin_sina", "销售净利率(%)", "销售净利率"),
    "销售净利率": ("net_margin_sina", "销售净利率(%)", "销售净利率"),

    # ---------------- 财务：规模（利润表） ----------------
    # `revenue` 是 `fundamental_source.py::YJBB_FIELD_MAP` 的规范化字段名，
    # 源列是东财「营业总收入-营业总收入」→ 所以「营收」「营业收入」
    # 「营业总收入」三种说法都归到 `revenue`。
    "营收": ("revenue", "营业总收入", "营业收入"),
    "营业收入": ("revenue", "营业收入", "营业总收入"),
    "营业总收入": ("revenue", "营业总收入", "营业收入"),
    "主营收入": ("revenue", "营业总收入", "营业收入"),
    "revenue": ("revenue", "营业总收入"),
    "营收同比": ("revenue_yoy", "主营业务收入增长率(%)"),
    "营业收入同比": ("revenue_yoy", "主营业务收入增长率(%)"),
    "营收增长率": ("revenue_yoy", "主营业务收入增长率(%)"),
    "净利润": ("net_profit", "净利润"),
    "归母净利润": ("net_profit", "净利润"),
    "net_profit": ("net_profit",),
    "净利润同比": ("net_profit_yoy", "净利润增长率(%)"),
    "净利润增长率": ("net_profit_yoy", "净利润增长率(%)"),

    # ---------------- 财务：每股 ----------------
    "每股收益": ("eps", "摊薄每股收益(元)", "每股收益", "EPS"),
    "eps": ("eps", "摊薄每股收益(元)", "EPS"),
    "基本每股收益": ("eps", "摊薄每股收益(元)", "EPS"),
    "摊薄每股收益": ("eps", "摊薄每股收益(元)", "EPS"),
    "每股净资产": ("bps", "每股净资产_调整前(元)", "每股净资产"),
    "bps": ("bps", "每股净资产_调整前(元)"),
    "每股经营现金流": ("ocfps", "ocfps_sina", "每股经营性现金流(元)", "每股经营现金流"),
    "经营现金流": ("ocfps", "ocfps_sina", "每股经营性现金流(元)", "每股经营现金流"),
    "经营性现金流": ("ocfps", "ocfps_sina", "每股经营性现金流(元)", "每股经营现金流"),
    "ocfps": ("ocfps", "ocfps_sina"),
    "每股未分配利润": ("每股未分配利润(元)",),
    "每股资本公积": ("每股资本公积金(元)",),

    # ---------------- 财务：营运 ----------------
    "应收账款周转率": ("ar_turnover", "应收账款周转率(次)"),
    "存货周转率": ("inv_turnover", "存货周转率(次)"),
    "总资产周转率": ("asset_turnover", "总资产周转率(次)"),

    # ---------------- 宏观（中国） ----------------
    # 中/美同名不同源：中国口径在前，美国口径在后当候选，**不合并**
    # （合并会让"这是哪国 CPI"这件事彻底不可见）。
    "cpi": ("CPI", "us_cpi_yoy"),
    "cpi同比": ("CPI", "us_cpi_yoy"),
    "居民消费价格指数": ("CPI",),
    "通胀": ("CPI", "us_cpi_yoy", "us_core_cpi", "us_pce"),
    "通货膨胀": ("CPI", "us_cpi_yoy", "us_core_cpi", "us_pce"),
    "ppi": ("PPI",),
    "ppi同比": ("PPI",),
    "工业生产者出厂价格指数": ("PPI",),
    "m2": ("M2",),
    "货币供应量": ("M2",),
    "广义货币": ("M2",),
    "gdp": ("GDP",),
    "国内生产总值": ("GDP",),
    "pmi": ("PMI",),
    "制造业pmi": ("PMI",),
    "采购经理指数": ("PMI",),
    "社融": ("社融",),
    "社会融资规模": ("社融",),
    "社融增量": ("社融",),
    "shibor": ("shibor_3m",),
    "shibor_3m": ("shibor_3m",),
    "银行间同业拆借利率": ("shibor_3m",),
    "社会消费品零售总额": ("ind:社会消费品零售总额同比",),
    "动力煤价格": ("ind:动力煤价格(元/吨)",),

    # ---------------- 宏观（美国 / 美联储） ----------------
    # 「加息/降息」不是指标而是**事件**，但用户就是这么问的 ——
    # 实测报障原话：「预测下一年美国的加息、降息节奏」，系统答
    # 「缺少联邦基金利率数据」而库里 `fed:*` / `us_fed_rate` 都有数据。
    # 所以把这两个词也指向利率族，避免同一个问题再问一次还是答不上。
    "联邦基金利率": ("fed:effr", "fed:target_upper", "fed:target_lower",
                     "fed:policy_range", "us_fed_rate"),
    "联邦基金目标利率": ("fed:target_upper", "fed:target_lower",
                         "fed:policy_range", "fed:effr", "us_fed_rate"),
    "政策利率": ("fed:effr", "fed:target_upper", "fed:target_lower", "fed:policy_range"),
    "美联储利率": ("fed:effr", "fed:target_upper", "fed:target_lower", "us_fed_rate"),
    "美国利率": ("fed:effr", "us_fed_rate"),
    "fedfunds": ("fed:effr", "us_fed_rate"),
    "fed:effr": ("fed:effr",),
    "fed:target_upper": ("fed:target_upper", "fed:policy_range"),
    "fed:target_lower": ("fed:target_lower", "fed:policy_range"),
    "fed:policy_range": ("fed:policy_range", "fed:target_upper", "fed:target_lower"),
    "加息": ("fed:effr", "fed:target_upper", "fed:target_lower",
             "fed:policy_range", "us_fed_rate"),
    "降息": ("fed:effr", "fed:target_upper", "fed:target_lower",
             "fed:policy_range", "us_fed_rate"),
    "核心cpi": ("us_core_cpi", "CPI"),
    "核心pce": ("us_pce",),
    "失业率": ("us_unemployment",),
    "美国失业率": ("us_unemployment",),
    "unemployment": ("us_unemployment",),
    "非农": ("us_nonfarm",),
    "非农就业": ("us_nonfarm",),
    "美国非农": ("us_nonfarm",),
    "nonfarm": ("us_nonfarm",),
    "美国cpi": ("us_cpi_yoy", "us_core_cpi", "CPI"),
    # 原样 id 也认（模型/上游有时直接抛 en_id 而不是中文标签）
    "us_cpi_yoy": ("us_cpi_yoy", "CPI"),
    "us_core_cpi": ("us_core_cpi", "us_cpi_yoy"),
    "us_nonfarm": ("us_nonfarm",),
    "us_unemployment": ("us_unemployment",),
    "us_pce": ("us_pce",),
    "us_fed_rate": ("us_fed_rate", "fed:effr"),

    # ---------------- 大盘流动性 / 估值分位 ----------------
    "两融余额": ("mkt:margin_balance", "mkt:margin_balance:hist"),
    "融资融券余额": ("mkt:margin_balance", "mkt:margin_balance:hist"),
    "北向资金": ("mkt:north_flow", "mkt:north_flow:hist"),
    "北向净流入": ("mkt:north_flow", "mkt:north_flow:hist"),
    "两市成交额": ("mkt:turnover:total", "mkt:turnover:hist"),
    "全a换手率": ("mkt:turnover_rate:all_a",),
    "宽基估值": ("idx_val:snapshot:all",),
    "估值分位": ("idx_val:snapshot:all",),
}

#: 规范化后的指标索引（导入期构建，撞车即抛）
_METRIC_INDEX: dict[str, tuple[str, ...]] = _build_index(_METRIC_ALIASES, "指标")


# ============================================================
# 实体别名表（别名 → 候选 6 位代码，**按强度降序**）
# ============================================================
#
# 代码来源：`data/security_names.json`（akshare 名称表落盘缓存，
# 快照 fetched_at=2026-09-23T23:58:00Z / 5568 只）—— **没有一条是手写的**。
# 每一族都能用 ALIAS_SOURCE_NOTE ④ 的命令逐条复核。
#
# 每只实体 4 类别名：① 官方中文名 ② 口语简称 ③ 英文简称 ④ 拼音（首字母+全拼）。
# 拼音**只收全表唯一的拼写**（18 条被丢，理由与名单见 ALIAS_SOURCE_NOTE ③）。
_ENTITY_ALIASES: dict[str, tuple[str, ...]] = {
    # ---- 银行 ----
    "招商银行": ("600036",),
    "招行": ("600036",),
    "cmb": ("600036",),
    "zhaoshangyinhang": ("600036",),
    "工商银行": ("601398",),
    "工行": ("601398",),
    "icbc": ("601398",),
    "gsyh": ("601398",),
    "gongshangyinhang": ("601398",),
    "建设银行": ("601939",),
    "建行": ("601939",),
    "ccb": ("601939",),
    "jiansheyinhang": ("601939",),
    "农业银行": ("601288",),
    "农行": ("601288",),
    "nyyh": ("601288",),
    "nongyeyinhang": ("601288",),
    "中国银行": ("601988",),
    "中行": ("601988",),
    "boc": ("601988",),
    "zhongguoyinhang": ("601988",),
    "交通银行": ("601328",),
    "交行": ("601328",),
    "jtyh": ("601328",),
    "jiaotongyinhang": ("601328",),
    "邮储银行": ("601658",),
    "邮储": ("601658",),
    "ycyh": ("601658",),
    "youchuyinhang": ("601658",),
    "兴业银行": ("601166",),
    "xyyh": ("601166",),
    "xingyeyinhang": ("601166",),
    "浦发银行": ("600000",),
    "浦发": ("600000",),
    "pfyh": ("600000",),
    "pufayinhang": ("600000",),
    "民生银行": ("600016",),
    "msyh": ("600016",),
    "minshengyinhang": ("600016",),
    "中信银行": ("601998",),
    "zxyh": ("601998",),
    "zhongxinyinhang": ("601998",),
    "光大银行": ("601818",),
    "gdyh": ("601818",),
    "guangdayinhang": ("601818",),
    "平安银行": ("000001",),
    "payh": ("000001",),
    "pinganyinhang": ("000001",),
    "华夏银行": ("600015",),
    "hxyh": ("600015",),
    "huaxiayinhang": ("600015",),
    "宁波银行": ("002142",),
    "nbyh": ("002142",),
    "ningboyinhang": ("002142",),
    "江苏银行": ("600919",),
    "jiangsuyinhang": ("600919",),
    # ---- 白酒 ----
    "贵州茅台": ("600519",),
    "茅台": ("600519",),
    "gzmt": ("600519",),
    "guizhoumaotai": ("600519",),
    "五粮液": ("000858",),
    "wuliangye": ("000858",),
    "泸州老窖": ("000568",),
    "老窖": ("000568",),
    "lzlj": ("000568",),
    "luzhoulaojiao": ("000568",),
    "山西汾酒": ("600809",),
    "汾酒": ("600809",),
    "sxfj": ("600809",),
    "shanxifenjiu": ("600809",),
    "洋河股份": ("002304",),
    "洋河": ("002304",),
    "yanghegufen": ("002304",),
    "古井贡酒": ("000596",),
    "古井": ("000596",),
    "gjgj": ("000596",),
    "gujinggongjiu": ("000596",),
    "今世缘": ("603369",),
    "jsy": ("603369",),
    "jinshiyuan": ("603369",),
    "迎驾贡酒": ("603198",),
    "迎驾": ("603198",),
    "yingjiagongjiu": ("603198",),
    "舍得酒业": ("600702",),
    "舍得": ("600702",),
    "sdjy": ("600702",),
    "shedejiuye": ("600702",),
    "水井坊": ("600779",),
    "sjf": ("600779",),
    "shuijingfang": ("600779",),
    # ---- 新能源/电力 ----
    "宁德时代": ("300750",),
    "宁德": ("300750",),
    "catl": ("300750",),
    "ndsd": ("300750",),
    "ningdeshidai": ("300750",),
    "比亚迪": ("002594",),
    "byd": ("002594",),
    "biyadi": ("002594",),
    "隆基绿能": ("601012",),
    "隆基": ("601012",),
    "longi": ("601012",),
    "ljln": ("601012",),
    "longjilvneng": ("601012",),
    "阳光电源": ("300274",),
    "ygdy": ("300274",),
    "yangguangdianyuan": ("300274",),
    "通威股份": ("600438",),
    "通威": ("600438",),
    "亿纬锂能": ("300014",),
    "亿纬": ("300014",),
    "ywln": ("300014",),
    "yiweilineng": ("300014",),
    "赣锋锂业": ("002460",),
    "赣锋": ("002460",),
    "gfly": ("002460",),
    "ganfengliye": ("002460",),
    "天齐锂业": ("002466",),
    "天齐": ("002466",),
    "tqly": ("002466",),
    "tianqiliye": ("002466",),
    "恩捷股份": ("002812",),
    "恩捷": ("002812",),
    "ejgf": ("002812",),
    "enjiegufen": ("002812",),
    "先导智能": ("300450",),
    "先导": ("300450",),
    "xdzn": ("300450",),
    "xiandaozhineng": ("300450",),
    "汇川技术": ("300124",),
    "汇川": ("300124",),
    "hcjs": ("300124",),
    "huichuanjishu": ("300124",),
    "晶澳科技": ("002459",),
    "jakj": ("002459",),
    "jingaokeji": ("002459",),
    "天合光能": ("688599",),
    "thgn": ("688599",),
    "tianheguangneng": ("688599",),
    "德业股份": ("605117",),
    "德业": ("605117",),
    "deyegufen": ("605117",),
    "锦浪科技": ("300763",),
    "锦浪": ("300763",),
    "jinlangkeji": ("300763",),
    "大全能源": ("688303",),
    "dqny": ("688303",),
    "daquannengyuan": ("688303",),
    "长江电力": ("600900",),
    "cjdl": ("600900",),
    "changjiangdianli": ("600900",),
    "中国核电": ("601985",),
    "zghd": ("601985",),
    "zhongguohedian": ("601985",),
    "三峡能源": ("600905",),
    "sanxianengyuan": ("600905",),
    "华能国际": ("600011",),
    "hngj": ("600011",),
    "huanengguoji": ("600011",),
    # ---- 医药/消费 ----
    "恒瑞医药": ("600276",),
    "恒瑞": ("600276",),
    "hengruiyiyao": ("600276",),
    "迈瑞医疗": ("300760",),
    "迈瑞": ("300760",),
    "mryl": ("300760",),
    "mairuiyiliao": ("300760",),
    "药明康德": ("603259",),
    "药明": ("603259",),
    "ymkd": ("603259",),
    "yaomingkangde": ("603259",),
    "伊利股份": ("600887",),
    "伊利": ("600887",),
    "yiligufen": ("600887",),
    "海天味业": ("603288",),
    "htwy": ("603288",),
    "haitianweiye": ("603288",),
    "美的集团": ("000333",),
    "美的": ("000333",),
    "mdjt": ("000333",),
    "meidejituan": ("000333",),
    "格力电器": ("000651",),
    "格力": ("000651",),
    "gldq": ("000651",),
    "gelidianqi": ("000651",),
    "片仔癀": ("600436",),
    "pzh": ("600436",),
    "pianzaihuang": ("600436",),
    "云南白药": ("000538",),
    "ynby": ("000538",),
    "yunnanbaiyao": ("000538",),
    "牧原股份": ("002714",),
    "牧原": ("002714",),
    "mygf": ("002714",),
    "muyuangufen": ("002714",),
    # ---- 金融/资源/交运 ----
    "中国平安": ("601318",),
    # ⚠️ 唯一的多候选中文别名：「平安」在中文语境里既可能指中国平安（保险）
    # 也可能指平安银行，两者都是本字典收录实体 → **两个都给**，中国平安在前
    # （更常见）。要"只给一个"是调用方的决定，不是字典替它猜。
    "平安": ("601318", "000001"),
    "zgpa": ("601318",),
    "zhongguopingan": ("601318",),
    "中信证券": ("600030",),
    "zxzq": ("600030",),
    "zhongxinzhengquan": ("600030",),
    "东方财富": ("300059",),
    "东财": ("300059",),
    "dfcf": ("300059",),
    "dongfangcaifu": ("300059",),
    "招商证券": ("600999",),
    "zhaoshangzhengquan": ("600999",),
    "华泰证券": ("601688",),
    "huataizhengquan": ("601688",),
    "中国石化": ("600028",),
    "中石化": ("600028",),
    "zhongguoshihua": ("600028",),
    "中国石油": ("601857",),
    "中石油": ("601857",),
    "zgsy": ("601857",),
    "zhongguoshiyou": ("601857",),
    "中国神华": ("601088",),
    "神华": ("601088",),
    "zhongguoshenhua": ("601088",),
    "紫金矿业": ("601899",),
    "紫金": ("601899",),
    "zjky": ("601899",),
    "zijinkuangye": ("601899",),
    "京沪高铁": ("601816",),
    "jhgt": ("601816",),
    "jinghugaotie": ("601816",),
    "顺丰控股": ("002352",),
    "顺丰": ("002352",),
    "sfkg": ("002352",),
    "shunfengkonggu": ("002352",),
    "三一重工": ("600031",),
    "syzg": ("600031",),
    "sanyizhonggong": ("600031",),
    "万华化学": ("600309",),
    "万华": ("600309",),
    "whhx": ("600309",),
    "wanhuahuaxue": ("600309",),
    # ---- 科技 ----
    "立讯精密": ("002475",),
    "立讯": ("002475",),
    "lxjm": ("002475",),
    "lixunjingmi": ("002475",),
    "中际旭创": ("300308",),
    "旭创": ("300308",),
    "zjxc": ("300308",),
    "zhongjixuchuang": ("300308",),
    "工业富联": ("601138",),
    "gyfl": ("601138",),
    "gongyefulian": ("601138",),
    "北方华创": ("002371",),
    "bfhc": ("002371",),
    "beifanghuachuang": ("002371",),
    "中芯国际": ("688981",),
    "中芯": ("688981",),
    "zxgj": ("688981",),
    "zhongxinguoji": ("688981",),
    "海光信息": ("688041",),
    "hgxx": ("688041",),
    "haiguangxinxi": ("688041",),
    "寒武纪": ("688256",),
    "hwj": ("688256",),
    "hanwuji": ("688256",),
}

#: 规范化后的实体索引（导入期构建，撞车即抛）
_STATIC_ENTITY_INDEX: dict[str, tuple[str, ...]] = _build_index(
    _ENTITY_ALIASES, "实体")


# ============================================================
# 6 位代码识别（带**实测得来**的交易所前缀白名单）
# ============================================================

#: A 股 6 位代码的前 3 位白名单。
#:
#: **从 `data/security_names.json` 实测得来**，不是凭记忆：该表 5568 只标的
#: 的三位前缀**只有**下面 14 个（复现见 `ALIAS_SOURCE_NOTE` ④）。
#: 已知边界：北交所旧代码段 `43x`/`83x`/`87x` 不在该表内 → 不识别。
_A_SHARE_PREFIXES: frozenset[str] = frozenset({
    "000", "001", "002", "003",
    "300", "301", "302",
    "600", "601", "603", "605", "688", "689",
    "920",
})

#: 6 位数字段。前后**不许紧邻数字**，否则 `6000367` 会被切成 `600036`。
#: 左侧允许紧邻字母：`SH600036` / `sz000858` 要靠这一点才认得出。
_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")


def extract_code(text: str) -> str | None:
    """从文本里取**首个**合法 A 股 6 位代码（`600036.SH` / `SH600036` 都认）。

    返回 `None` 表示"这里没有代码"，而不是"代码是空的" —— 两者必须分开，
    否则调用方无法区分"没写代码"与"解析失败"。
    """
    if not text:
        return None
    for match in _CODE_RE.finditer(str(text)):
        code = match.group(1)
        if code[:3] in _A_SHARE_PREFIXES:
            return code
    return None


# ============================================================
# 由数据生成的实体别名层（只读本地磁盘，**绝不联网**）
# ============================================================

#: 与 `connectors/security_resolver.py::_CACHE_FILE` **同一个文件**
#: （护栏测试断言两者相等，防止复制路径后各指一处）。
_NAME_TABLE_PATH: Path = (
    Path(__file__).resolve().parents[3] / "data" / "security_names.json")

#: ⚠️ 用 `RLock` 而不是 `Lock`，且**不许再嵌套加锁** —— 二者必须同时成立。
#:
#: 实测踩过（本模块第一版）：`_entity_alias_index()` 持锁后调用
#: `generated_entity_aliases()`，而后者**又去拿同一把非重入锁** →
#: `resolve_entity()` **第一次调用即死锁**，进程直接挂住（不是报错，
#: 是"像卡住了"，最难查的那一类）。
#: 修法是结构上合并成**一次**加锁（见 `_ensure_indexes`），`RLock`
#: 只是第二道保险：将来有人再加一层嵌套，退化成"能跑"而不是"挂死"。
_INDEX_LOCK = threading.RLock()
_ENTITY_INDEX: dict[str, tuple[str, ...]] | None = None
_GENERATED_INDEX: dict[str, tuple[str, ...]] | None = None


def _load_name_table() -> tuple[tuple[str, str], ...]:
    """读名称表落盘缓存；缺失/损坏 → 空表（**降级，不是报错**）。

    这里**故意不复用** `security_resolver._name_pairs()`：那个函数在缓存过期时
    会**发起网络请求**（上限 15s）。本模块的契约是"只读本地、绝不联网"，
    所以自己读这个文件。文件不存在时返回空表，由静态字典兜底。
    """
    try:
        payload = json.loads(_NAME_TABLE_PATH.read_text(encoding="utf-8"))
        return tuple((str(code), str(name)) for code, name in payload["pairs"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.info("证券名称表不可用（%s），实体解析退回静态字典",
                    type(exc).__name__)
        return ()


def _build_generated_index() -> dict[str, tuple[str, ...]]:
    """`data/security_names.json` → `{规范化中文名: (代码,)}`。

    同名不同码**合并成候选**而不是后写覆盖先写（名称表实测 0 例重复，
    但表会变，静默覆盖在这里是不可接受的失败模式）。
    """
    index: dict[str, list[str]] = {}
    for code, name in _load_name_table():
        key = _norm(name)
        if not key:
            continue
        codes = index.setdefault(key, [])
        if code not in codes:
            codes.append(code)
    return {k: tuple(v) for k, v in index.items()}


def _ensure_indexes() -> None:
    """**一次加锁**构建两份索引（生成层 + 静态∪生成层）。

    为什么合成一个函数：两个 `if ... is None` 各自加锁、且其中一个调用另一个，
    就是上面注释里那个死锁现场。合成后**锁段内不再调用任何会加锁的函数**，
    正确性由结构保证，不靠"记得别嵌套"。
    """
    global _ENTITY_INDEX, _GENERATED_INDEX
    if _ENTITY_INDEX is not None and _GENERATED_INDEX is not None:
        return
    with _INDEX_LOCK:
        if _GENERATED_INDEX is None:
            _GENERATED_INDEX = _build_generated_index()
        if _ENTITY_INDEX is None:
            merged = dict(_STATIC_ENTITY_INDEX)
            for key, codes in _GENERATED_INDEX.items():
                # 静态优先：人工判定赢，且不一致由护栏测试发现（不是静默覆盖）
                merged.setdefault(key, codes)
            _ENTITY_INDEX = merged


def reset_caches_for_test() -> None:
    """清空惰性缓存（**只给测试用**，用于复现"首次调用"路径）。

    为什么必须有：上面那个死锁**只在冷缓存路径上出现** —— 一旦测过一次，
    缓存命中就再也不走那条路径，"第二次跑是绿的"会把缺陷藏起来。
    与 `catalog/__init__.py::reset_registry_for_test` 同一套约定。
    """
    global _ENTITY_INDEX, _GENERATED_INDEX
    with _INDEX_LOCK:
        _ENTITY_INDEX = None
        _GENERATED_INDEX = None


def generated_entity_aliases() -> dict[str, tuple[str, ...]]:
    """**可由数据生成**的那部分实体别名（全市场中文名 → 代码）。

    与 `entity_aliases()` 分开暴露，是为了让护栏/审计能分别核对：
    静态那份是人工 review 的对象，这份是"名称表快照的产物"。
    名称表缺失时返回 `{}`（不是报错）。
    """
    _ensure_indexes()
    assert _GENERATED_INDEX is not None  # noqa: S101  _ensure_indexes 的契约
    return dict(_GENERATED_INDEX)


def _entity_alias_index() -> dict[str, tuple[str, ...]]:
    """静态字典 ∪ 生成层（**静态优先**）。"""
    _ensure_indexes()
    assert _ENTITY_INDEX is not None  # noqa: S101  _ensure_indexes 的契约
    return _ENTITY_INDEX


# ============================================================
# 解析入口
# ============================================================

def _rank(alias_index: dict[str, tuple[str, ...]],
          key: str) -> list[tuple[str, ...]]:
    """按**最长匹配跨度**给命中的别名排序（判据与实现均来自
    `local_data.py::_alias_match_span`，本模块不重复实现）。

    排序键：`(-跨度, -别名长度, 别名)`。
    第二项只是**同跨度时的确定性平局裁决**（如查询『营业总收入』同时精确
    命中『营业总收入』与……不存在这种情况；但『市值』会同时以"查询是别名
    的子串"命中『总市值』与『流通市值』，此时长的优先）。
    """
    scored: list[tuple[int, int, str, tuple[str, ...]]] = []
    for alias, targets in alias_index.items():
        span = _match_span(alias, key)
        if span > 0:
            scored.append((span, len(alias), alias, targets))
    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return [targets for _span, _len, _alias, targets in scored]


def _dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        lowered = value.lower()
        if lowered not in seen:
            seen.add(lowered)
            out.append(value)
    return out


def resolve_metric(text: str) -> list[str]:
    """用户/模型说的**指标名** → 候选标准列名或指标 id（按匹配强度降序）。

        resolve_metric("股息率TTM")[0] == "dv_ttm"      # 最长匹配跨度 6
        resolve_metric("股息率")[0]    == "dv_ratio"    # 最长匹配跨度 3
        resolve_metric("营收")         ⊇ ["revenue", "营业总收入", ...]
        resolve_metric("")             == []            # 空串/纯符号/未收录词

    **不做原文兜底**：认不出就返回 `[]`。"把查询原样返回"会让
    「我没认出这个词」看起来像「我认出来了」—— 那是本项目最贵的一类假绿。
    """
    key = _norm(text)
    if not key:
        return []
    ranked = _rank(_METRIC_INDEX, key)
    if not ranked:
        return []
    flat: list[str] = []
    for targets in ranked:
        flat.extend(targets)
    return _dedupe(flat)


def resolve_entity(text: str) -> list[str]:
    """用户/模型说的**实体名** → 候选 6 位代码（按匹配强度降序）。

    认这四类写法（顺序即优先级）：

        "600036" / "600036.SH" / "SH600036" / "sz000858"  → 代码直读（最强）
        "招商银行"                                        → 静态字典精确命中
        "招行" / "茅台" / "宁德" / "CATL"                 → 口语与英文简称
        "zsyh" / "zhaoshangyinhang"                       → 拼音（仅全表唯一者）
        其余 5568 只的中文名                              → 名称表现算（只读磁盘）

    空串 / 纯符号 / 认不出的词 → `[]`（不抛异常、不编造）。
    """
    raw = str(text or "").strip()
    if not raw:
        return []

    out: list[str] = []
    # ① 代码直读：代码是**自证**的（前缀白名单 + 6 位），无需查表，优先级最高
    code = extract_code(raw)
    if code:
        out.append(code)
    # ② 别名匹配
    key = _norm(raw)
    if not key:
        return _dedupe(out)
    for targets in _rank(_entity_alias_index(), key):
        out.extend(targets)
    return _dedupe(out)


# ============================================================
# 字典内容（供护栏测试与审计脚本读）
# ============================================================

def metric_aliases() -> dict[str, tuple[str, ...]]:
    """指标别名字典的**副本**（别名 → 候选列名/指标 id）。"""
    return dict(_METRIC_ALIASES)


def entity_aliases() -> dict[str, tuple[str, ...]]:
    """实体别名字典的**副本**（别名 → 候选 6 位代码）。

    只含**人工维护**的那份（260 条，可 review）；全市场中文名那份在
    `generated_entity_aliases()`。
    """
    return dict(_ENTITY_ALIASES)


def dict_stats() -> dict[str, int]:
    """字典规模自述（护栏据此断言"词表没退化成空壳"）。"""
    return {
        "metric_aliases": len(_METRIC_ALIASES),
        "metric_targets": len({t for v in _METRIC_ALIASES.values() for t in v}),
        "entity_aliases": len(_ENTITY_ALIASES),
        "entity_codes": len({c for v in _ENTITY_ALIASES.values() for c in v}),
        "generated_entity_aliases": len(generated_entity_aliases()),
        "a_share_prefixes": len(_A_SHARE_PREFIXES),
    }
