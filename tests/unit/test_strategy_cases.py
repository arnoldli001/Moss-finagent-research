"""开源策略案例库单测（离线，不打网络）。

抓取逻辑本身靠 `scripts/quant_cases.py fetch` 在真实网络下验证；
这里守着的是**离线可判定的部分**，尤其是三类容易被做错的事：

1. **主题/类型分类**：分类错了，"回测方法论"这类最有价值的线索会被埋进"其它"；
2. **增量去重**：按来源链接哈希去重，每周抓一次不能把表撑爆成重复行；
3. **必须标未验证**：抓来的内容是网络说法，`verified` 必须恒为 0 ——
   一旦有人把它改成 1，界面上就会出现"已验证"的假象；
4. **配置驱动**：源清单能读 YAML、坏 YAML 不能让抓取整体崩掉。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.quant.strategy_cases import (
    DEFAULT_SOURCES,
    StrategyCase,
    case_id,
    classify_kind,
    classify_theme,
    load_sources,
)
from src.quant.warehouse import QuantWarehouse, StrategyCaseStore, WarehouseConfig


@pytest.fixture()
def store(tmp_path: Path) -> StrategyCaseStore:
    config = WarehouseConfig(url=f"sqlite:///{(tmp_path / 'c.db').as_posix()}",
                             dialect="sqlite")
    return StrategyCaseStore(QuantWarehouse(config), disclaimer="测试用免责声明")


def case(url: str, **overrides) -> StrategyCase:
    base = {
        "id": case_id(url), "theme": "因子研究", "title": "多因子选股回测",
        "summary": "用 IC 加权合成因子", "source_name": "GitHub",
        "source_url": url, "published_at": "2026-09-01",
    }
    base.update(overrides)
    return StrategyCase(**base)


# ==================================================================
# 分类
# ==================================================================


@pytest.mark.parametrize(("text", "expected"), [
    ("Cross-sectional momentum in Chinese equities", "动量/趋势"),
    ("低波动异象与风险平价配置", "波动/风险"),
    ("Deep learning for stock ranking", "机器学习"),
    ("Pairs trading with cointegration", "统计套利"),
    ("Multi-factor model and Barra risk", "因子研究"),
    ("红利低估值策略回测", "价值/基本面"),
    ("涨停板打板情绪择时", "打板/情绪"),
])
def test_theme_classification(text: str, expected: str) -> None:
    assert classify_theme(text) == expected


def test_lookahead_articles_get_their_own_theme() -> None:
    """未来函数/过拟合类内容必须单独成类。

    实测：这些文章原本全落进「其它」，而这个项目整个量化模块的设计重心
    恰恰就是防未来函数与过拟合 —— 归到「其它」等于把最有价值的线索埋掉。
    """
    for text in ("量化概念：未来函数（回测结果最常见的失真来源）",
                 "如何避免回测中的过拟合与数据泄露",
                 "Survivorship bias in backtesting",
                 "Walk-forward 样本外检验怎么做"):
        assert classify_theme(text) == "回测方法论", text


def test_kind_separates_frameworks_from_strategies() -> None:
    """回测框架不是策略案例 —— 混在表里会让人以为抓取坏了。

    实测：GitHub 搜索"量化 回测"的前 18 条里，backtrader/vnpy 这类框架
    占了三分之二。
    """
    assert classify_kind("a backtrader-based backtest framework") == "框架/工具"
    assert classify_kind("量化交易入门教程") == "教程/笔记"
    assert classify_kind("We propose a new factor model") == "学术论文"
    assert classify_kind("Short-Term Reversal Strategy in A-shares") == "策略案例"


def test_case_id_is_stable_and_url_based() -> None:
    url = "https://github.com/example/strategy"
    assert case_id(url) == case_id(url)
    assert case_id(url) != case_id(url + "1")
    assert len(case_id(url)) == 16


# ==================================================================
# 存储与去重
# ==================================================================


def test_upsert_and_dedupe_by_url(store: StrategyCaseStore) -> None:
    """同一链接重复抓取只更新，不新增 —— 每周抓一次不能把表撑爆。"""
    first = store.upsert([case("https://a.example/1"),
                          case("https://a.example/2")])
    assert first["inserted"] == 2
    assert store.count() == 2
    store.upsert([case("https://a.example/1", title="改过的标题")])
    assert store.count() == 2
    items = store.list()
    assert any(item["title"] == "改过的标题" for item in items)


def test_cases_are_always_marked_unverified(store: StrategyCaseStore) -> None:
    """`verified` 必须恒为 0：抓来的是网络说法，不是我们验证过的结论。"""
    store.upsert([case("https://a.example/3")])
    item = store.list()[0]
    assert item["verified"] == 0


def test_list_filters_by_theme_kind_and_excludes_tools(
        store: StrategyCaseStore) -> None:
    store.upsert([
        case("https://a.example/s1", theme="动量/趋势", kind="策略案例"),
        case("https://a.example/s2", theme="动量/趋势", kind="框架/工具"),
        case("https://a.example/s3", theme="因子研究", kind="策略案例"),
    ])
    assert len(store.list(theme="动量/趋势")) == 2
    assert len(store.list(kind="策略案例")) == 2
    # 默认排除框架：只留下真正的策略案例
    assert len(store.list(exclude_kinds=("框架/工具",))) == 2


def test_themes_and_kinds_stats(store: StrategyCaseStore) -> None:
    store.upsert([
        case("https://a.example/t1", theme="动量/趋势", kind="策略案例"),
        case("https://a.example/t2", theme="动量/趋势", kind="框架/工具"),
    ])
    assert {"theme": "动量/趋势", "count": 2} in store.themes()
    assert {"kind": "策略案例", "count": 1} in store.kind_stats()


def test_reclassify_updates_historical_rows(store: StrategyCaseStore) -> None:
    """改了关键词表之后，历史记录也要重分类。

    抓取只更新"本批返回的条目"，旧记录会保留旧分类 ——
    实测就是这样：新增"回测方法论"后表里仍有 40 多条停在"其它"。
    """
    store.upsert([case("https://a.example/r1", theme="其它", kind="其它",
                       title="量化回测中的未来函数陷阱", summary="如何避免虚假收益")])
    updated = store.reclassify()
    assert updated == 1
    item = store.list()[0]
    assert item["theme"] == "回测方法论"


def test_store_survives_unavailable_database(tmp_path: Path) -> None:
    store = StrategyCaseStore(QuantWarehouse(
        WarehouseConfig(url="", dialect="", description="未配置")))
    assert store.list() == []
    assert store.count() == 0
    assert store.upsert([case("https://a.example/x")])["total"] == 0


# ==================================================================
# 源清单配置
# ==================================================================


def test_default_sources_exclude_unscrapable() -> None:
    """实测抓不到内容的源不能在默认清单里启用。"""
    enabled = {spec.name for spec in DEFAULT_SOURCES if spec.enabled}
    assert "雪球" not in enabled, "雪球需 JS 过风控，抓取必然失败"
    assert {"GitHub", "arXiv q-fin", "CSDN"} <= enabled


def test_load_sources_from_yaml(tmp_path: Path) -> None:
    config = tmp_path / "sources.yaml"
    config.write_text(
        "sources:\n"
        "  - name: 测试源\n"
        "    kind: rss\n"
        "    enabled: true\n"
        "    limit: 3\n"
        "    endpoint: https://example.com/feed.xml\n"
        "    tags: [自定义]\n", encoding="utf-8")
    specs = load_sources(config)
    assert len(specs) == 1
    assert specs[0].name == "测试源"
    assert specs[0].kind == "rss"
    assert specs[0].limit == 3


def test_broken_yaml_falls_back_to_defaults(tmp_path: Path) -> None:
    """源清单写坏了不该让抓取整体失效。"""
    config = tmp_path / "broken.yaml"
    config.write_text("sources: [ this is not: valid: yaml", encoding="utf-8")
    specs = load_sources(config)
    assert len(specs) == len(DEFAULT_SOURCES)


def test_missing_config_uses_defaults(tmp_path: Path) -> None:
    specs = load_sources(tmp_path / "nope.yaml")
    assert [spec.name for spec in specs] == [spec.name for spec in DEFAULT_SOURCES]
