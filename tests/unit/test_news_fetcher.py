"""新闻抓取器测试：纯函数转换 + Fake akshare + 失败降级空列表。"""

import sys

import pandas as pd

from src.infrastructure.connectors.news_fetcher import (
    AkshareNewsFetcher,
    news_df_to_items,
    topic_df_to_items,
)


def test_news_df_to_items():
    df = pd.DataFrame({
        "关键词": ["601088"],
        "新闻标题": ["中国神华发布中报"],
        "新闻内容": ["净利润287亿元"],
        "发布时间": ["2026-08-29 10:26:54"],
        "文章来源": ["界面新闻"],
        "新闻链接": ["http://example.com/n1"],
    })
    items = news_df_to_items(df, limit=10)
    assert len(items) == 1
    item = items[0]
    assert item["title"] == "中国神华发布中报"
    assert "287亿" in item["text"]
    assert item["source_name"] == "界面新闻"
    assert item["source_url"] == "http://example.com/n1"
    assert item["publish_time"] == "2026-08-29 10:26:54"


def test_news_df_empty():
    assert news_df_to_items(None) == []
    assert news_df_to_items(pd.DataFrame()) == []


class _FakeAk:
    def stock_news_em(self, symbol):
        return pd.DataFrame({
            "关键词": [symbol], "新闻标题": ["t"], "新闻内容": ["c"],
            "发布时间": ["2026-09-01"], "文章来源": ["src"],
            "新闻链接": ["http://x"],
        })


async def test_fetcher_success(monkeypatch):
    monkeypatch.setitem(sys.modules, "akshare", _FakeAk())
    items = await AkshareNewsFetcher().fetch_news("601088", limit=5)
    assert len(items) == 1
    assert items[0]["title"] == "t"


class _BrokenAk:
    def stock_news_em(self, symbol):
        raise ConnectionError("remote closed")


async def test_fetcher_failure_returns_empty(monkeypatch):
    monkeypatch.setitem(sys.modules, "akshare", _BrokenAk())
    items = await AkshareNewsFetcher().fetch_news("600519")
    assert items == []  # 增强链路失败不抛异常


def _topic_df():
    return pd.DataFrame({
        "标题": [
            "美联储9月降息概率升至70%",
            "光伏组件价格继续下探",
            "英伟达发布新一代AI芯片，算力需求爆发",
            "CPO光模块板块持续走强",
        ],
        "摘要": [
            "CME观察显示市场预期年内仍有两次降息",
            "一线企业开工率不足六成",
            "数据中心资本开支上修，液冷、PCB同步受益",
            "海外云厂商800G订单饱满",
        ],
        "发布时间": ["2026-09-10 09:00", "2026-09-10 09:05",
                    "2026-09-10 10:00", "2026-09-10 10:30"],
        "链接": ["http://a", "http://b", "http://c", "http://d"],
    })


def test_topic_df_filter_by_keywords():
    items = topic_df_to_items(_topic_df(), ["美联储", "降息"])
    assert len(items) == 1
    assert items[0]["source_name"] == "东方财富-全球财经"
    assert "美联储" in items[0]["matched_keywords"]
    assert items[0]["source_url"] == "http://a"


def test_topic_df_case_insensitive_and_summary_match():
    # "ai"大小写不敏感命中标题；"液冷"仅出现在摘要
    items = topic_df_to_items(_topic_df(), ["AI", "液冷"])
    assert len(items) == 1
    titles = {i["title"] for i in items}
    assert "英伟达发布新一代AI芯片，算力需求爆发" in titles


def test_topic_df_industry_segment_keywords():
    # 细分环节问法：整组关键词能召回光模块/CPO快讯
    items = topic_df_to_items(_topic_df(), ["AI", "光模块", "CPO", "液冷"])
    assert {i["title"] for i in items} == {
        "英伟达发布新一代AI芯片，算力需求爆发",
        "CPO光模块板块持续走强",
    }


def test_topic_df_empty_inputs_and_limit():
    assert topic_df_to_items(None, ["AI"]) == []
    assert topic_df_to_items(_topic_df(), []) == []
    items = topic_df_to_items(_topic_df(), ["美联储", "降息", "AI", "光模块",
                                            "CPO", "液冷"], limit=1)
    assert len(items) == 1


class _FakeTopicAk:
    def stock_info_global_em(self):
        return _topic_df()


async def test_topic_fetcher_success(monkeypatch):
    monkeypatch.setitem(sys.modules, "akshare", _FakeTopicAk())
    items = await AkshareNewsFetcher().fetch_topic_news(["AI"], limit=5)
    assert items and all(i["source_name"] == "东方财富-全球财经" for i in items)


class _BrokenTopicAk:
    def stock_info_global_em(self):
        raise ConnectionError("remote closed")


async def test_topic_fetcher_failure_returns_empty(monkeypatch):
    monkeypatch.setitem(sys.modules, "akshare", _BrokenTopicAk())
    items = await AkshareNewsFetcher().fetch_topic_news(["美联储"])
    assert items == []
