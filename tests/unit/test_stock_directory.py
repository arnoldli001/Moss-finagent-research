"""本地股票字典单测（离线，不打网络）。

守着三类容易出错的地方：

1. **拼音首字母**：错了用户就搜不到（`jqkj` 查不到剑桥科技）；
2. **查不到时必须返回空串，不能退回代码本身** —— 退化成代码正是
   "自选股里显示 603083" 那次事故的直接原因；
3. **联想排序**：用户敲 `payh` 时要第一条就是平安银行，而不是某个代码里
   恰好含 payh 的票。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.quant.stock_directory import (
    DIRECTORY_TABLE,
    StockDirectory,
    StockEntry,
    to_full_pinyin,
    to_initials,
)
from src.quant.warehouse import QuantWarehouse, WarehouseConfig

SAMPLES = [
    ("000001", "平安银行", "PAYH", "银行"),
    ("600519", "贵州茅台", "GZMT", "白酒"),
    ("603083", "剑桥科技", "JQKJ", "通信设备"),
    ("688391", "钜泉科技", "JQKJ", "半导体"),
    ("600150", "中国船舶", "ZGCB", "船舶"),
    ("000002", "万科A", "WKA", "全国地产"),
    ("601318", "中国平安", "ZGPA", "保险"),
    ("588170", "科创半导体ETF华夏", "KCBDTHX", "ETF"),
]


@pytest.fixture()
def directory(tmp_path: Path) -> StockDirectory:
    config = WarehouseConfig(url=f"sqlite:///{(tmp_path / 'd.db').as_posix()}",
                             dialect="sqlite")
    store = StockDirectory(warehouse=QuantWarehouse(config))
    store.upsert([
        StockEntry(code=code, name=name, pinyin_initials=to_initials(name),
                   pinyin_full=to_full_pinyin(name), industry=industry,
                   instrument_type="ETF" if code.startswith("5") else "股票",
                   source="test")
        for code, name, _initials, industry in SAMPLES
    ])
    return store


# ==================================================================
# 拼音生成
# ==================================================================


@pytest.mark.parametrize(("name", "expected"), [
    ("平安银行", "PAYH"),
    ("贵州茅台", "GZMT"),
    ("剑桥科技", "JQKJ"),
    ("中国船舶", "ZGCB"),
    ("中国平安", "ZGPA"),
])
def test_initials_of_common_names(name: str, expected: str) -> None:
    assert to_initials(name) == expected


def test_initials_keep_latin_and_digits() -> None:
    """名称里的字母数字要保留：`万科A` → `WKA`，用户按自己习惯敲 `wka` 也能命中。"""
    assert to_initials("万科A") == "WKA"
    assert to_initials("科创半导体ETF华夏").startswith("KCBDT")


def test_full_pinyin_is_lowercase() -> None:
    assert to_full_pinyin("平安银行") == "pinganyinhang"
    assert to_full_pinyin("") == ""


# ==================================================================
# 联想
# ==================================================================


def test_search_by_code_prefix(directory: StockDirectory) -> None:
    hits = directory.search("6030")
    assert hits and hits[0].code == "603083"


def test_search_by_exact_code_ranks_first(directory: StockDirectory) -> None:
    assert directory.search("600519")[0].name == "贵州茅台"


def test_search_by_pinyin_initials(directory: StockDirectory) -> None:
    hits = directory.search("payh")
    assert hits and hits[0].name == "平安银行"
    assert hits[0].code == "000001"


def test_search_by_initials_prefix_returns_all_matches(
        directory: StockDirectory) -> None:
    """同一个首字母对应多只票时要**都列出来**（JQKJ 有两家）。"""
    names = {item.name for item in directory.search("jqkj")}
    assert names == {"剑桥科技", "钜泉科技"}


def test_search_by_partial_initials(directory: StockDirectory) -> None:
    names = {item.name for item in directory.search("pa")}
    assert "平安银行" in names


def test_search_by_chinese_substring(directory: StockDirectory) -> None:
    names = {item.name for item in directory.search("平安")}
    assert names == {"平安银行", "中国平安"}


def test_search_by_full_pinyin(directory: StockDirectory) -> None:
    assert directory.search("gzmt")[0].name == "贵州茅台"
    assert directory.search("guizhou")[0].name == "贵州茅台"


def test_search_ranking_prefers_exact_code_over_name_match(
        directory: StockDirectory) -> None:
    """排序：代码完全相等 > 代码前缀 > 首字母完全相等 > … > 名称包含。"""
    hits = directory.search("000001")
    assert hits[0].code == "000001"


def test_search_empty_query_returns_nothing(directory: StockDirectory) -> None:
    assert directory.search("") == []
    assert directory.search("   ") == []


def test_search_respects_limit(directory: StockDirectory) -> None:
    assert len(directory.search("6", limit=2)) <= 2


def test_search_can_filter_by_type(directory: StockDirectory) -> None:
    only_etf = directory.search("588170", types=("ETF",))
    assert len(only_etf) == 1
    assert directory.search("588170", types=("股票",)) == []


def test_search_returns_empty_when_directory_missing(tmp_path: Path) -> None:
    """字典不可用时返回空，不抛异常 —— 输入框不能因为字典挂了就用不了。"""
    store = StockDirectory(warehouse=QuantWarehouse(
        WarehouseConfig(url="", dialect="", description="未配置")))
    assert store.count() == 0
    assert store.search("平安") == []
    assert store.get("000001", auto_enrich=False) is None


# ==================================================================
# 名称补全：绝不能退回代码
# ==================================================================


def test_name_of_returns_chinese_name(directory: StockDirectory) -> None:
    assert directory.name_of("603083") == "剑桥科技"
    assert directory.name_of("600150") == "中国船舶"


def test_name_of_returns_empty_for_unknown_not_the_code(
        directory: StockDirectory) -> None:
    """**查不到必须返回空串，不能返回代码本身。**

    返回代码会让"名称就是代码"这种脏数据看起来像正常值 ——
    做T自选股里那批 `603083 / 600150` 就是这么来的，界面上一排代码，
    而且不会有人报错。
    """
    assert directory.name_of("999999") == ""
    assert directory.name_of("000000") == ""


def test_stored_row_whose_name_equals_code_is_treated_as_missing(
        directory: StockDirectory) -> None:
    """配置里已经写成 `name=code` 的历史脏数据也要被当成"没名称"。

    这里只测**关闭自动补录**的确定性路径：脏行必须被识别为"无名称"，
    而不是把代码当成合法名称返回。

    打开自动补录时会去东财查一次并**自我修复**（`601999` 是真实代码），
    这是期望行为 —— 但依赖网络，所以这里用 `auto_enrich=False` 测确定性语义。
    """
    directory.upsert([StockEntry(code="601999", name="601999",
                                 pinyin_initials="", pinyin_full="")])
    assert directory.name_of("601999", auto_enrich=False) == "", \
        "name == code 是脏数据，必须被识别为「无名称」"
    # 关闭补录时原始行仍在（不去网络改写它）
    entry = directory.get("601999", auto_enrich=False)
    assert entry is not None and entry.name == "601999"


def test_code_normalization_accepts_ts_code(directory: StockDirectory) -> None:
    """带交易所后缀的 `000001.SZ` 也要能查（Tushare 口径）。"""
    assert directory.name_of("000001.SZ") == "平安银行"
    assert directory.get("000001.SZ").code == "000001"


def test_upsert_is_idempotent(directory: StockDirectory) -> None:
    before = directory.count()
    directory.upsert([StockEntry(code="000001", name="平安银行",
                                 pinyin_initials="PAYH")])
    assert directory.count() == before


def test_directory_table_name_is_stable() -> None:
    assert DIRECTORY_TABLE == "quant_stock_directory"
