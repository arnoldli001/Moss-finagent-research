"""`synonym_dict` 护栏：**最长匹配跨度**与**字典不许退化成空壳**的回归现场。

## 为什么这些断言长这样

用户 2026-09-28 报障"数据在库里却取不到"，根因之一是**名词对不上**：
用户说「营收」库里是 `revenue`/「营业总收入」，说「招商银行」实体是 `600036`。
本字典就是那份映射，所以护栏守两件事：

1. **口径不能认错**（比"认不出"更贵）。实测 600036 的 `dv_ratio=5.05` /
   `dv_ttm=5.76` —— 认错的那个**看起来完全正常**。所以「股息率」与
   「股息率TTM」**两个方向都断言**，且断言的是**首选**而不是"候选里有"。
2. **字典不能退化成空壳**。一张只剩两三条的别名表能让所有断言"通过"，
   却解决不了报障 —— 所以逐族断言必查词条都在。

## 与数据源的关系（为什么有几个测试会 skip）

`data/security_names.json` 是 akshare 名称表的落盘缓存，而 `.gitignore:22`
写着 `/data/` —— **它不入库**。所以依赖它的断言在克隆环境里 `pytest.skip`
（**知名降级**，不是静默绿灯）；不依赖它的断言（最长匹配、空输入、家族覆盖、
离线性质、死锁）在任何环境都必须跑。
"""
from __future__ import annotations

import ast
import collections
import json
import re
import threading
from pathlib import Path

import pytest

from src.infrastructure.catalog import local_data, synonym_dict
from src.infrastructure.catalog.synonym_dict import (
    ALIAS_SOURCE_NOTE,
    dict_stats,
    entity_aliases,
    extract_code,
    generated_entity_aliases,
    metric_aliases,
    normalize_alias,
    reset_caches_for_test,
    resolve_entity,
    resolve_metric,
)

#: 名称表落盘缓存（与 `connectors/security_resolver.py::_CACHE_FILE` 同一份）
_NAME_TABLE = Path(__file__).resolve().parents[2] / "data" / "security_names.json"


# ============================================================
# 夹具：名称表（缺失即 skip，且**只读本地、不联网**）
# ============================================================

def _load_name_table() -> dict[str, str]:
    """`规范化中文名 → 代码`（读不到返回空 dict，由调用方 skip）。"""
    try:
        payload = json.loads(_NAME_TABLE.read_text(encoding="utf-8"))
        pairs = [(str(c), str(n)) for c, n in payload["pairs"]]
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    out: dict[str, str] = {}
    for code, name in pairs:
        key = normalize_alias(name)
        if key:
            out.setdefault(key, code)
    return out


@pytest.fixture(scope="module")
def name_table() -> dict[str, str]:
    table = _load_name_table()
    if not table:
        pytest.skip(f"名称表缓存不存在/不可读：{_NAME_TABLE}"
                    "（.gitignore /data/ → 克隆环境没有它；实体代码断言降级跳过）")
    return table


# ============================================================
# ① 最长匹配跨度（本模块的头号回归现场）
# ============================================================

def test_dividend_yield_prefers_static_ratio() -> None:
    """「股息率」→ 首选 `dv_ratio`（**不是** `dv_ttm`）。"""
    got = resolve_metric("股息率")
    assert got, "『股息率』必须解析出候选，解析不出等于报障没修"
    assert got[0] == "dv_ratio", (
        f"『股息率』首选应为 dv_ratio（静态口径），实际 {got!r} —— "
        "最长匹配跨度退化成了别名长度排序")


def test_dividend_yield_ttm_prefers_ttm_column() -> None:
    """「股息率TTM」→ 首选 `dv_ttm`（**不是** `dv_ratio`）。"""
    for query in ("股息率TTM", "股息率ttm", "股息率(TTM)", "股息率_ttm"):
        got = resolve_metric(query)
        assert got and got[0] == "dv_ttm", (
            f"『{query}』首选应为 dv_ttm（滚动十二个月），实际 {got!r}")


def test_longest_match_is_not_alias_length() -> None:
    """★ 判据是**匹配跨度**，不是"别名长度"—— 两个方向一起断言。

    只断言一个方向是不够的：按"别名长度降序"排会同时错另一个方向
    （『股息率』→ `dv_ttm`），而按字典插入顺序排会错『股息率TTM』。
    """
    assert resolve_metric("股息率")[0] == "dv_ratio"
    assert resolve_metric("股息率TTM")[0] == "dv_ttm"
    # 更具体者必须排在更泛者之前：TTM 族的首选绝不能是泛化口径
    assert resolve_metric("股息率TTM")[0] != resolve_metric("股息率")[0]


def test_more_specific_alias_outranks_generic_pe_family() -> None:
    """第二组同型回归（PE 族）：泛化词与 TTM 词的首选必须不同且各归其位。"""
    assert resolve_metric("pe")[0] == "pe"
    assert resolve_metric("pe_ttm")[0] == "pe_ttm"
    assert resolve_metric("PE(TTM)")[0] == "pe_ttm"


def test_query_with_context_still_picks_specific_alias() -> None:
    """带上下文的整句也要认（跨度判据的价值就在这里）。"""
    got = resolve_metric("招商银行股息率TTM")
    assert got and got[0] == "dv_ttm", f"整句中的口径应取更具体者，实际 {got!r}"


# ============================================================
# ② 报障里的具体说法必须能解析
# ============================================================

def test_revenue_aliases_resolve_to_revenue_columns() -> None:
    """「营收」必须能解析到含 `revenue` /「营业总收入」的候选里。"""
    got = resolve_metric("营收")
    assert got, "『营收』解析为空 —— 用户报障的原始说法"
    assert "revenue" in got or "营业总收入" in got, f"候选不含营收列：{got!r}"
    # 三种说法都归到同一个规范化字段
    for query in ("营收", "营业收入", "营业总收入"):
        cands = resolve_metric(query)
        assert "revenue" in cands, f"『{query}』应含 revenue，实际 {cands!r}"


@pytest.mark.parametrize("text", ["招商银行", "招行", "600036", "600036.SH"])
def test_merchants_bank_alias_forms(text: str) -> None:
    """报障原文那只票：中文全称 / 口语简称 / 裸代码 / 带后缀写法。"""
    got = resolve_entity(text)
    assert got, f"『{text}』解析为空"
    assert got[0] == "600036", f"『{text}』首选应为 600036，实际 {got!r}"


@pytest.mark.parametrize(("text", "code"), [
    ("招商银行", "600036"), ("招行", "600036"), ("600036", "600036"),
    ("600036.SH", "600036"), ("SH600036", "600036"), ("sh600036", "600036"),
    ("工商银行", "601398"), ("工行", "601398"),
    ("贵州茅台", "600519"), ("茅台", "600519"),
    ("宁德时代", "300750"), ("宁德", "300750"),
    ("CATL", "300750"), ("catl", "300750"),
    ("五粮液", "000858"),          # ⚠️ 名称表里是 "五 粮 液"（带空格），见 §⑤
    ("比亚迪", "002594"), ("BYD", "002594"),
    ("zhaoshangyinhang", "600036"), ("mairuiyiliao", "300760"),
    ("sz000858", "000858"), ("000858.SZ", "000858"),
])
def test_entity_alias_forms(text: str, code: str) -> None:
    got = resolve_entity(text)
    assert got and got[0] == code, f"『{text}』首选应为 {code}，实际 {got!r}"


# ============================================================
# ③ 认不出就返回空 —— 不抛异常、不编造
# ============================================================

@pytest.mark.parametrize("text", [
    "", "   ", "\t\n", "!!!", "???", "。。。", "---", "-",
    "asdfghjkl", "不存在的指标xyz", "12345678901", "0",
])
def test_unknown_input_returns_empty_not_fabricated(text: str) -> None:
    """**不许"原文兜底"**：把查询原样当候选返回，会让"没认出"看起来像"认出了"。"""
    assert resolve_metric(text) == [], f"resolve_metric({text!r}) 应返回空列表"
    assert resolve_entity(text) == [], f"resolve_entity({text!r}) 应返回空列表"


@pytest.mark.parametrize("text", [None, 0, 3.14])
def test_non_string_input_does_not_raise(text: object) -> None:
    """签名是 str，但上游传 None/数字不许炸链路。"""
    assert resolve_metric(text) == []      # type: ignore[arg-type]
    assert resolve_entity(text) == []      # type: ignore[arg-type]


def test_seven_digit_run_is_not_a_code() -> None:
    """`6000367` 不能被切成 `600036`（前后不许紧邻数字）。"""
    assert extract_code("6000367") is None
    assert extract_code("1600036") is None
    assert extract_code("600036") == "600036"
    assert extract_code("SH600036") == "600036"


# ============================================================
# ④ 字典不许退化成空壳
# ============================================================

def test_metric_aliases_are_non_empty_strings() -> None:
    table = metric_aliases()
    assert table, "指标别名字典为空"
    for alias, targets in table.items():
        assert isinstance(alias, str) and alias.strip(), f"空别名：{alias!r}"
        assert isinstance(targets, tuple) and targets, f"『{alias}』没有目标候选"
        for target in targets:
            assert isinstance(target, str) and target.strip(), \
                f"『{alias}』的目标里有空串：{targets!r}"


def test_entity_aliases_are_non_empty_strings() -> None:
    table = entity_aliases()
    assert table, "实体别名字典为空"
    for alias, codes in table.items():
        assert isinstance(alias, str) and alias.strip(), f"空别名：{alias!r}"
        assert isinstance(codes, tuple) and codes, f"『{alias}』没有候选代码"
        for code in codes:
            assert re.fullmatch(r"\d{6}", code), \
                f"『{alias}』的候选不是 6 位代码：{code!r}"


def test_dict_scale_is_not_degenerate() -> None:
    """规模下限（宽松但有意义）：词表缩成两三条时这里先红。"""
    stats = dict_stats()
    assert stats["metric_aliases"] >= 100, stats
    assert stats["metric_targets"] >= 80, stats
    assert stats["entity_aliases"] >= 200, stats
    assert stats["entity_codes"] >= 60, stats


@pytest.mark.parametrize(("query", "expected_head"), [
    # —— 估值 ——
    ("市盈率", "pe_ttm"), ("PE", "pe"), ("PE(TTM)", "pe_ttm"),
    ("市净率", "pb"), ("PB", "pb"), ("市销率", "ps_ttm"), ("PS", "ps"),
    ("股息率", "dv_ratio"), ("股息率TTM", "dv_ttm"), ("分红率", "dv_ratio"),
    # —— 行情 ——
    ("收盘价", "close"), ("收盘", "close"), ("close", "close"),
    ("成交量", "volume"), ("成交额", "amount"),
    ("换手率", "turnover_rate"), ("量比", "volume_ratio"),
    # —— 市值 ——
    ("总市值", "total_mv"), ("流通市值", "circ_mv"),
    # —— 财务 ——
    ("资产负债率", "debt_to_assets"), ("流动比率", "current_ratio"),
    ("速动比率", "quick_ratio"), ("ROE", "roe"), ("净资产收益率", "roe"),
    ("ROA", "roa_sina"), ("毛利率", "gross_margin"), ("净利率", "net_margin_sina"),
    ("营收", "revenue"), ("营业收入", "revenue"), ("营业总收入", "revenue"),
    ("净利润", "net_profit"), ("每股收益", "eps"), ("EPS", "eps"),
    ("每股净资产", "bps"), ("BPS", "bps"), ("经营现金流", "ocfps"),
    # —— 宏观 ——
    ("CPI", "CPI"), ("居民消费价格指数", "CPI"), ("通胀", "CPI"),
    ("PPI", "PPI"), ("M2", "M2"), ("GDP", "GDP"), ("PMI", "PMI"),
    ("社融", "社融"), ("失业率", "us_unemployment"), ("非农", "us_nonfarm"),
    ("联邦基金利率", "fed:effr"), ("政策利率", "fed:effr"),
    ("FEDFUNDS", "fed:effr"),
])
def test_required_families_are_covered(query: str, expected_head: str) -> None:
    """逐族断言：**必查词条**都在，且首选正确（空壳词表会在这里红）。"""
    got = resolve_metric(query)
    assert got, f"『{query}』解析为空 —— 该词条从字典里掉了"
    assert got[0] == expected_head, f"『{query}』首选应为 {expected_head}，实际 {got!r}"


@pytest.mark.parametrize(("query", "expected_head"), [
    ("招商银行", "600036"), ("工商银行", "601398"),
    ("贵州茅台", "600519"), ("宁德时代", "300750"),
    ("隆基绿能", "601012"), ("迈瑞医疗", "300760"),
    ("中国平安", "601318"), ("东方财富", "300059"),
])
def test_required_entity_families_are_covered(query: str,
                                              expected_head: str) -> None:
    got = resolve_entity(query)
    assert got and got[0] == expected_head, \
        f"『{query}』首选应为 {expected_head}，实际 {got!r}"


@pytest.mark.parametrize("query", ["银行", "白酒", "新能源", "券商", "光伏"])
def test_generic_words_do_not_raise(query: str) -> None:
    """泛词只要求"不炸"，不强断言候选 —— 泛词本来就没有唯一答案。"""
    assert isinstance(resolve_entity(query), list)


# ============================================================
# ⑤ 与名称表一致（缺表即 skip：/data/ 不入库）
# ============================================================

def test_required_codes_agree_with_name_table(name_table: dict[str, str]) -> None:
    """四条硬要求必须有**可复现来源**，不只是"字典里写着"。"""
    for name, code in (("招商银行", "600036"), ("工商银行", "601398"),
                       ("贵州茅台", "600519"), ("宁德时代", "300750")):
        assert name_table.get(normalize_alias(name)) == code, (
            f"名称表与断言不符：{name} → {name_table.get(normalize_alias(name))!r}"
            f"（期望 {code}）—— 要么表变了，要么断言过期")


def test_entity_names_agree_with_name_table(name_table: dict[str, str]) -> None:
    """字典里每个**官方中文名**都必须与名称表同码（静默覆盖会在这里现形）。"""
    checked = 0
    for alias, codes in entity_aliases().items():
        key = normalize_alias(alias)
        if key in name_table:
            checked += 1
            assert name_table[key] in codes, (
                f"『{alias}』字典给 {codes!r}，名称表说 {name_table[key]!r}")
    assert checked >= 60, f"只核对上 {checked} 条官方名，判据可能失效了"


def test_space_padded_names_are_normalized(name_table: dict[str, str]) -> None:
    """★ 名称表里 `000858` 的官方名是 **`五 粮 液`（带空格）**。

    实测证据（本机快照 `2026-09-23`）：直接拿 `"五粮液"` 去名称表**查不到** ——
    这就是"必须有一份中文别名字典"的活证据。这里断言的是**性质**而不是那个
    字符串本身（表刷新后官方名可能被修正，那时断言不该变红）。

    ⚠️ 判据要适应数据，不要让数据迁就判据。
    """
    raw = json.loads(_NAME_TABLE.read_text(encoding="utf-8"))["pairs"]
    spaced = {str(c): str(n) for c, n in raw if re.search(r"\s", str(n))}
    for code, name in spaced.items():
        assert resolve_entity(name)[:1] == [code], \
            f"带空格的官方名 {name!r} 解析不出 {code}"
        assert resolve_entity(re.sub(r"\s+", "", name))[:1] == [code], \
            f"去空格后的 {name!r} 解析不出 {code}"
    # 不管表里怎么写，用户说的"五粮液"都必须认
    assert name_table.get(normalize_alias("五粮液")) == "000858"
    assert resolve_entity("五粮液")[:1] == ["000858"]
    assert resolve_entity("五 粮 液")[:1] == ["000858"]


def test_pinyin_is_registered_exactly_when_globally_unique(
        name_table: dict[str, str]) -> None:
    """★ 拼音收录规则的自更新护栏：**唯一就必须收，歧义就必须不收**。

    为什么需要它：拼音首字母在 5568 个名称里大量撞车（`zsyh` 同时是招商银行
    与浙商银行、`ylgf` 同时是伊利股份与另外 11 只），收进来就是**认错**而不是
    认不出。规则本身写在 `ALIAS_SOURCE_NOTE` ③，这里让规则**可执行**：
    表一变，这条测试就会告诉你"该加一行"或"该删一行"，不靠人记得。

    ## ★ 2026-10-01：本测试原先**自己算了一遍**唯一性（规则的第二份实现）

    实测后果：除息日快照里 `600028` 叫 `XD中国石`，与「中国神华」的 `zgsh`
    本该撞车，不剥前缀就"不撞了" ⇒ 本测试要求收一条**按除息日固化的错别名**。
    根因不是"别名表缺一行"，而是**唯一性规则有两份实现**（本测试一份、
    `synonym_dict` 一份）。现在规则只有一份：`synonym_dict.pinyin_index()`，
    本测试只**核对表与规则是否一致**（`required_*` / `ambiguous_*`）。
    """
    pytest.importorskip("pypinyin", reason="拼音别名由 pypinyin 生成")
    from src.infrastructure.catalog.synonym_dict import (
        ambiguous_pinyin_aliases,
        pinyin_keys,
        required_pinyin_aliases,
    )

    raw = json.loads(_NAME_TABLE.read_text(encoding="utf-8"))["pairs"]
    names = [(str(c), re.sub(r"\s+", "", str(n))) for c, n in raw]
    by_code = dict(names)

    required = required_pinyin_aliases()          # {拼音: (代码,)}
    ambiguous = ambiguous_pinyin_aliases()        # {拼音: (代码, …)}

    table = entity_aliases()
    checked = 0
    for alias, codes in table.items():
        key = normalize_alias(alias)
        if key not in name_table:
            continue          # 不是官方名（口语简称/英文/拼音），跳过
        checked += 1
        name = by_code.get(codes[0], "")
        if not name:
            continue
        ini_key, full_key = pinyin_keys(name)     # 唯一实现（已剥行情前缀）
        for kind, pkey in (("首字母", ini_key), ("全拼", full_key)):
            if pkey in required:
                assert pkey in table, (
                    f"『{name}』的{kind} `{pkey}` 在全表唯一，按规则**必须收录** —— "
                    f"请在 _ENTITY_ALIASES 加一行，并更新 ALIAS_SOURCE_NOTE ③ 的计数。"
                    f"（唯一性由 `synonym_dict.pinyin_index()` 算，已剥行情前缀）")
                assert codes[0] in table[pkey], (
                    f"『{name}』的{kind} `{pkey}` 应为 {codes[0]}，实际 {table[pkey]!r}")
            elif pkey in ambiguous:
                assert pkey not in table, (
                    f"『{name}』的{kind} `{pkey}` 在全表**有 {len(ambiguous[pkey])} 个"
                    f"主人**（{ambiguous[pkey]}），按规则**不许收录** —— 收了就是认错。"
                    "若确实要收，改规则并同步 ALIAS_SOURCE_NOTE ③")
            else:
                # 规则表里没有它（例如名字被剥成了另一个名字）：不猜，直接报出来
                raise AssertionError(
                    f"『{name}』的{kind} `{pkey}` 既不在唯一集也不在歧义集 —— "
                    "规则与名称表不同步，请检查 `pinyin_index()` 的剥前缀规则")
    assert checked >= 60, f"只核对上 {checked} 个官方名，判据可能失效了"


# ============================================================
# ⑥ 结构性护栏（都是"本轮实测踩出来的"那一类）
# ============================================================

def test_shared_span_judgment_has_a_single_implementation() -> None:
    """★ 跨度判据只有一份实现：`local_data._alias_match_span`。

    本模块**不复制**它。复制就会漂移，而漂移的表现是"两个口径各选各的"，
    看起来都正常。（本项目实测过：同一个 key 写在 3 处只改 1 处。）
    """
    assert synonym_dict._alias_match_span is local_data._alias_match_span, \
        "跨度判据被复制成了第二份实现 —— 改回 import"


def test_narrowing_never_invents_spans() -> None:
    """★ 本模块对 ASCII 巧合子串的**否决**只减不增，且不碰中文。

    否则就是"第二个判据"：`_match_span` 必须恒 ≤ 共享函数的返回值；
    任一侧含中文时必须**完全相等**（跨度的本意就是给中文用的）。
    """
    keys = list(synonym_dict._METRIC_INDEX)
    for alias in keys:
        for query in keys:
            shared = synonym_dict._alias_match_span(alias, query)
            mine = synonym_dict._match_span(alias, query)
            assert mine <= shared, f"{alias!r}/{query!r}: {mine} > {shared}"
            if not (alias.isascii() and query.isascii()):
                assert mine == shared, f"{alias!r}/{query!r} 含中文却改了跨度"


@pytest.mark.parametrize(("query", "forbidden"), [
    # 实测：'pe' ⊂ 'fedtargetupper'（fed:target_upper 归一后）→
    # 市盈率的问句会拿到**美联储利率**候选，数字取回来还看着正常。
    ("pe", ("fed:target_upper", "fed:policy_range", "fed:effr")),
    ("PE", ("fed:target_upper", "fed:policy_range", "fed:effr")),
    # 实测：'ps' ⊂ 'eps' / 'bps' / 'ocfps' → 每股收益的问句会拿到**市销率**。
    ("eps", ("ps", "ps_ttm", "市销率")),
    ("EPS", ("ps", "ps_ttm", "市销率")),
    ("bps", ("ps", "ps_ttm", "市销率")),
    ("ocfps", ("ps", "ps_ttm", "市销率")),
    # 实测：'volume' ⊂ 'volumeratio' / 'volumelot' → 成交量会拿到量比族。
    ("volume", ("volume_ratio",)),
])
def test_no_cross_family_pollution(query: str,
                                   forbidden: tuple[str, ...]) -> None:
    """★ ASCII 巧合子串必须被否决（否则候选表里混进**别的族**）。

    判据用**精确成员**而不是子串：`"ps" in "eps"` 恒真，拿子串判会自己误报。
    """
    got = resolve_metric(query)
    assert got, f"『{query}』本身是合法指标，不该解析为空"
    hit = sorted(set(forbidden) & set(got))
    assert not hit, (
        f"『{query}』的候选里混进了别的族：{hit!r}（完整候选 {got!r}）—— "
        "ASCII 巧合子串没被 `_match_span` 否决")


@pytest.mark.parametrize("query", ["type", "asdfgh", "zzz"])
def test_coincidental_latin_words_resolve_to_nothing(query: str) -> None:
    """纯形近/无关的拉丁串不该命中任何指标（`type` 含 `pe`）。"""
    assert resolve_metric(query) == [], f"『{query}』不是指标，不该有候选"


def test_cold_cache_resolution_does_not_deadlock() -> None:
    """★ 冷缓存路径必须在**有界时间**内返回（本模块第一版在这里死锁）。

    实测：`_entity_alias_index()` 持非重入锁后调用 `generated_entity_aliases()`，
    后者又去拿同一把锁 → **第一次 `resolve_entity()` 直接挂住进程**。
    挂住比报错难查得多，所以这条用线程 + join 超时把它变成"快速红灯"；
    并且必须先 `reset_caches_for_test()`，否则缓存命中根本不走那条路径。
    """
    reset_caches_for_test()
    box: dict[str, list[str]] = {}
    worker = threading.Thread(
        target=lambda: box.setdefault("codes", resolve_entity("招商银行")),
        daemon=True)
    worker.start()
    worker.join(timeout=30)
    assert not worker.is_alive(), "resolve_entity 在冷缓存路径上死锁（锁重入？）"
    assert box.get("codes", [])[:1] == ["600036"], box


def test_indexes_have_no_normalization_collisions() -> None:
    """别名规范化后不许撞车（撞车 = 后写静默覆盖先写）。

    ★ 2026-10-01（`CHG-0155`）：指标索引现在会**再补一层生成键**
    （`augment_targets_as_keys()`：把"只作为目标出现"的名字补成键，
    否则 32 个连接器 id 问下去返回 `[]`）。所以"索引大小 == 手工表大小"
    这条**形式**已经不成立 —— 但它要防的东西（手工别名之间规范化撞车）
    必须原样保住，所以改成两段：
      ① 手工表的索引仍然 1:1（撞车仍会当场抛 / 这里仍会红）；
      ② 多出来的键**恰好**是生成的那批，且每一个都不是手工键
         （生成层不可能掩盖一次撞车）。
    """
    manual = synonym_dict._build_index(metric_aliases(), "指标")   # noqa: SLF001
    assert len(manual) == len(metric_aliases()), (
        "手工指标别名规范化后撞车（后写会静默覆盖先写）")
    assert len(synonym_dict._STATIC_ENTITY_INDEX) == len(entity_aliases())
    generated_keys = set(synonym_dict._METRIC_INDEX) - set(manual)   # noqa: SLF001
    assert len(generated_keys) == synonym_dict._AUGMENTED_METRIC_KEYS, (   # noqa: SLF001
        "索引里多出来的键与「生成条数」对不上 —— 要么生成层被改坏，"
        "要么有别的路径偷偷往索引里塞键")
    assert generated_keys, "生成层没生效（那 32 个死胡同会回来）"
    for table, index, kind in (
        (metric_aliases(), synonym_dict._METRIC_INDEX, "指标"),
        (entity_aliases(), synonym_dict._STATIC_ENTITY_INDEX, "实体"),
    ):
        for alias in table:
            assert normalize_alias(alias) in index, f"{kind}别名未进索引：{alias!r}"


def test_static_table_wins_over_generated_layer() -> None:
    """合并顺序：静态（人工判定）优先，生成层只补空位。"""
    reset_caches_for_test()
    generated = generated_entity_aliases()
    static = {normalize_alias(a): c for a, c in entity_aliases().items()}
    merged = synonym_dict._entity_alias_index()
    for key, codes in static.items():
        assert merged[key] == codes, f"生成层覆盖了静态别名 {key!r}"
    borrowed = 0
    for key, codes in generated.items():
        if key in static:
            continue          # 上面已断言静态优先
        assert merged[key] == codes, f"生成层没补上 {key!r}"
        borrowed += 1
    assert borrowed >= 5000, f"生成层只补了 {borrowed} 条，可能没读到名称表"


def test_name_table_path_matches_security_resolver() -> None:
    """★ 名称表路径**只有一处**：本模块与 `security_resolver` 必须指向同一文件。

    复制一份路径常量就会在两处各指一处，而症状是"一边能解析一边不能"。
    """
    from src.infrastructure.connectors import security_resolver

    assert synonym_dict._NAME_TABLE_PATH == security_resolver._CACHE_FILE, (
        f"两处名称表路径不一致：{synonym_dict._NAME_TABLE_PATH} vs "
        f"{security_resolver._CACHE_FILE}")


def test_module_is_offline_by_construction() -> None:
    """★ "只读本地、绝不联网"是**结构**性质：源码里不许出现联网入口。

    为什么值得一条测试：`security_resolver._name_pairs()` 在缓存过期时会
    **发起最长 15s 的网络请求**。本模块故意不复用它，正是为了不把这条
    延迟引进解析路径 —— 一旦有人"顺手改成复用它"，测试立刻红。
    """
    source = Path(synonym_dict.__file__).read_text(encoding="utf-8")
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    forbidden = {"akshare", "requests", "httpx", "urllib", "socket",
                 "urllib.request", "http.client"}
    assert not (modules & forbidden), f"出现了联网入口：{modules & forbidden}"
    assert not any("security_resolver" in m for m in modules), \
        "引了 security_resolver —— 它的名称表加载会联网（上限 15s）"


def test_alias_source_note_documents_both_provenances() -> None:
    """来源与边界必须写清楚"哪些人工维护、哪些可由数据生成"。"""
    assert len(ALIAS_SOURCE_NOTE) > 500, "来源说明太短，等于没写"
    for token in ("人工维护", "由数据生成", "边界", "唯一"):
        assert token in ALIAS_SOURCE_NOTE, f"来源说明缺了「{token}」这一节"


def test_dict_literals_have_no_duplicate_keys() -> None:
    """★ 表字面量里不许有重复 key（Python 会**静默**只留最后一条）。

    用 AST 而不是正则 —— 而且必须同时处理 `Assign` 与 **`AnnAssign`**：
    本模块的表是带类型注解的（`_METRIC_ALIASES: dict[...] = {...}`），
    只匹配 `ast.Assign` 会**一条都找不到**，然后"检查通过"（实测踩过这一个：
    探针报 0 个问题，而它其实根本没看到那两张表）。
    所以这里额外断言"必须找到 2 张表" —— 让"看不到"表现为失败。
    """
    source = Path(synonym_dict.__file__).read_text(encoding="utf-8")
    found: dict[str, list[str]] = {}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name, value = node.target.id, node.value
        elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            name, value = node.targets[0].id, node.value
        else:
            continue
        if name in ("_METRIC_ALIASES", "_ENTITY_ALIASES") and isinstance(value, ast.Dict):
            found[name] = [k.value for k in value.keys if isinstance(k, ast.Constant)]
    assert set(found) == {"_METRIC_ALIASES", "_ENTITY_ALIASES"}, (
        f"AST 只找到 {sorted(found)} —— 判据失效了（是写成 AnnAssign 了吗？）")
    for name, keys in found.items():
        dup = sorted({k for k in keys if keys.count(k) > 1})
        assert not dup, f"{name} 有重复 key（后者静默覆盖前者）：{dup}"
        # 字面量 key 数必须等于运行期字典大小（重复会被 Python 悄悄合并掉）
        assert len(keys) == len(getattr(synonym_dict, name)), \
            f"{name} 字面量 {len(keys)} 条 ≠ 运行期 {len(getattr(synonym_dict, name))} 条"


def test_resolution_is_order_stable() -> None:
    """同一输入重复调用必须逐字节同序（候选顺序会被调用方当优先级用）。"""
    for query in ("股息率TTM", "pe", "营收", "招商银行", "宁德"):
        first = resolve_metric(query), resolve_entity(query)
        for _ in range(3):
            assert (resolve_metric(query), resolve_entity(query)) == first
