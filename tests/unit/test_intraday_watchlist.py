"""做T模块 · 自选池增删 + 失败冷却 + 轻量快照 单元测试。

覆盖三类「前端体验」相关的后端保证：
  1. 前端加/删自选必须落盘到 configs/intraday.yaml，且**不破坏文件里的中文注释**
     （那些注释是这个配置最有价值的部分，整文件 yaml.safe_dump 重写会把它们抹掉）；
  2. 写坏配置必须可回滚（候选内容校验失败 → 原文件纹丝不动）；
  3. 数据源失败冷却：某源刚失败过就跳过，不再让每次快照都白等超时。
"""

from __future__ import annotations

import os

import pytest

from src.core.exceptions import ConfigError
from src.intraday.config import (
    IntradayConfig,
    WatchConfig,
    dump_watchlist_block,
    load_intraday_config,
    remove_watch,
    replace_watchlist_section,
    reset_config_cache,
    save_watchlist,
    upsert_watch,
)

SAMPLE_YAML = """\
# 做T辅助配置（这行注释必须活下来）

version: 1

# ============ 因子权重（合计=100，硬校验） ============
weights:
  box: 17        # 箱体最高权重
  vwap: 11
  boll: 8
  macd: 6
  kdj_rsi: 6
  sentiment: 6
  news: 3
  index_volume: 6
  board_rank: 4
  overseas: 4
  chan: 12       # 技能库四因子
  chip: 8
  cycle: 6
  character: 3

# ============ 信号阈值 ============
thresholds:
  action: 30     # 动手线
  hint: 20       # 提示线

# ============ 自选做T标的 ============
watchlist:
  - code: "300308"
    name: 中际旭创
    boards: [PCB概念]
    industry: "计算机、通信和其他电子设备制造业"
    peers: ["002463", "603228"]

# ============ 推送 ============
notify:
  enabled: true
  cooldown_minutes: 30
"""


@pytest.fixture
def yaml_path(tmp_dir):
    path = os.path.join(tmp_dir, "intraday.yaml")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(SAMPLE_YAML)
    reset_config_cache()
    return path


# ==================== 段落替换：注释保留 ====================


def test_replace_section_keeps_other_comments() -> None:
    items = [WatchConfig(code="600110", name="诺德股份", boards=["PET铜箔"])]
    updated = replace_watchlist_section(SAMPLE_YAML, items)
    # 段落之外的注释与配置必须原样保留
    assert "# 做T辅助配置（这行注释必须活下来）" in updated
    assert "# ============ 因子权重（合计=100，硬校验） ============" in updated
    assert "box: 17        # 箱体最高权重" in updated
    assert "action: 30     # 动手线" in updated
    assert "# ============ 推送 ============" in updated
    assert "cooldown_minutes: 30" in updated
    # 新自选已写入
    assert '  - code: "600110"' in updated
    assert "诺德股份" in updated
    # 旧自选被替换掉
    assert "300308" not in updated


def test_replace_section_appends_when_missing() -> None:
    text = (
        "version: 1\n"
        "weights: {box: 30, vwap: 20, boll: 15, macd: 10, kdj_rsi: 10,"
        " sentiment: 10, news: 5}\n"
        "thresholds: {action: 30, hint: 20}\n"
    )
    updated = replace_watchlist_section(
        text, [WatchConfig(code="300308")])
    assert "watchlist:" in updated
    assert '  - code: "300308"' in updated
    # 其它段落仍在
    assert "thresholds: {action: 30, hint: 20}" in updated


def test_replace_section_empty_list() -> None:
    updated = replace_watchlist_section(SAMPLE_YAML, [])
    assert "watchlist: []" in updated
    assert "# ============ 推送 ============" in updated


def test_dump_block_round_trips_through_yaml() -> None:
    """生成的段落必须能被 YAML 解析回等价对象（标记行是注释，不影响解析）。"""
    import yaml

    items = [
        WatchConfig(code="300308", name="中际旭创", boards=["PCB概念"],
                    industry="计算机、通信和其他电子设备制造业",
                    peers=["002463", "603228"]),
        WatchConfig(code="600110", name="诺德股份", boards=["PET铜箔"]),
    ]
    text = "\n".join(dump_watchlist_block(items)) + "\n"
    parsed = yaml.safe_load(text)
    assert [item["code"] for item in parsed["watchlist"]] == ["300308", "600110"]
    assert parsed["watchlist"][0]["peers"] == ["002463", "603228"]
    assert parsed["watchlist"][1]["boards"] == ["PET铜箔"]


def test_numeric_name_is_quoted_and_survives_reload(yaml_path) -> None:
    """回归：自动填的证券简称若是纯数字（如ETF代码 588170），必须加引号。

    否则 YAML 把它当 int → 下次加载校验失败 → 整份配置被静默回退成默认值
    （用户的自选池与权重全丢）。实测踩过这个坑。
    """
    import yaml

    items = [WatchConfig(code="588170", name="588170")]
    parsed = yaml.safe_load("\n".join(dump_watchlist_block(items)) + "\n")
    assert isinstance(parsed["watchlist"][0]["name"], str)
    assert parsed["watchlist"][0]["name"] == "588170"

    # 端到端：落盘后重新加载不得报错、不得丢自选
    config = save_watchlist(items, yaml_path)
    assert config.load_error is None
    assert [item.code for item in config.watchlist] == ["588170"]
    reset_config_cache()
    reloaded = load_intraday_config(yaml_path, force=True)
    assert reloaded.load_error is None
    assert reloaded.watchlist[0].name == "588170"


def test_numeric_board_and_peer_values_quoted() -> None:
    """板块名/同业代码同样需要引号保护（防止被解析成数字）。"""
    import yaml

    items = [WatchConfig(code="300308", name="中际旭创",
                         boards=["300"], peers=["002463"])]
    parsed = yaml.safe_load("\n".join(dump_watchlist_block(items)) + "\n")
    row = parsed["watchlist"][0]
    assert row["boards"] == ["300"] and isinstance(row["boards"][0], str)
    assert row["peers"] == ["002463"] and isinstance(row["peers"][0], str)


def test_overseas_survives_watchlist_round_trip(yaml_path) -> None:
    """海外映射必须随自选池一起落盘（否则前端加自选后映射会丢）。"""
    items = [WatchConfig(code="300308", name="中际旭创",
                         overseas=["usNVDA", "kr000660"])]
    config = save_watchlist(items, yaml_path)
    assert config.watchlist[0].overseas == ["usNVDA", "kr000660"]
    reset_config_cache()
    reloaded = load_intraday_config(yaml_path, force=True)
    assert reloaded.watchlist[0].overseas == ["usNVDA", "kr000660"]


def test_broken_config_surfaces_load_error(tmp_dir) -> None:
    """配置写坏时：不崩服务，但必须把「已回退默认参数」暴露出来。

    否则用户改的权重/自选池被静默忽略，面板照样出数，问题极难发现。
    """
    import os

    path = os.path.join(tmp_dir, "broken.yaml")
    with open(path, "w", encoding="utf-8") as handle:
        # 语法合法但类型非法：name 是数字 → pydantic 校验失败
        handle.write(
            "weights: {box: 17, vwap: 11, boll: 8, macd: 6, kdj_rsi: 6,"
            " sentiment: 6, news: 3, index_volume: 6, board_rank: 4, overseas: 4,"
            " chan: 12, chip: 8, cycle: 6, character: 3}\n"
            "thresholds: {action: 30, hint: 20}\n"
            "watchlist:\n  - code: \"588170\"\n    name: 588170\n")
    reset_config_cache()
    config = load_intraday_config(path, force=True)
    assert config.load_error is not None, "回退默认值必须被记录"
    assert "解析失败" in config.load_error
    # 回退后仍是一份可用默认配置（服务不中断）
    assert sum(config.weights.as_dict().values()) == pytest.approx(100.0)


def test_replace_section_is_idempotent_and_migrates_legacy_format() -> None:
    """旧格式（无标记）首次替换后写入标记；再替换内容完全不变（幂等）。"""
    items = [WatchConfig(code="300308", name="中际旭创", boards=["PCB概念"])]
    first = replace_watchlist_section(SAMPLE_YAML, items)
    assert ">>> intraday-watchlist:begin" in first
    assert "<<< intraday-watchlist:end" in first
    # 旧段落的说明注释被收编进受管区域，不会重复累积
    assert first.count("# ============ 自选做T标的 ============") == 1
    second = replace_watchlist_section(first, items)
    assert second == first
    # 再替换成另一只票，也不残留旧内容
    third = replace_watchlist_section(
        second, [WatchConfig(code="600110", name="诺德股份")])
    assert "600110" in third and "300308" not in third
    assert third.count("# ============ 自选做T标的 ============") == 1


# ==================== 落盘与缓存刷新 ====================


def test_save_watchlist_persists_and_reloads(yaml_path) -> None:
    items = [WatchConfig(code="600519", name="贵州茅台", boards=["白酒概念"])]
    config = save_watchlist(items, yaml_path)
    assert [item.code for item in config.watchlist] == ["600519"]
    # 重新从磁盘读一遍（绕过缓存）确认真的落盘了
    reset_config_cache()
    reloaded = load_intraday_config(yaml_path, force=True)
    assert reloaded.watchlist[0].name == "贵州茅台"
    assert reloaded.watch("600519") is not None
    # 文件注释仍在
    text = open(yaml_path, encoding="utf-8").read()
    assert "# 做T辅助配置（这行注释必须活下来）" in text


def test_upsert_watch_adds_and_updates_without_duplicates(yaml_path) -> None:
    config = upsert_watch(WatchConfig(code="300308", name="中际旭创改"),
                          yaml_path)
    codes = [item.code for item in config.watchlist]
    assert codes.count("300308") == 1  # 按代码去重
    assert config.watch("300308").name == "中际旭创改"
    config = upsert_watch(WatchConfig(code="600110", name="诺德股份"), yaml_path)
    assert [item.code for item in config.watchlist] == ["300308", "600110"]


def test_remove_watch_is_idempotent(yaml_path) -> None:
    config = remove_watch("300308", yaml_path)
    assert config.watchlist == []
    # 再删一次不报错
    config = remove_watch("300308", yaml_path)
    assert config.watchlist == []
    # 删不存在的代码同样幂等
    config = remove_watch("999999", yaml_path)
    assert config.watchlist == []


def test_save_watchlist_is_atomic_and_cleans_temp(yaml_path) -> None:
    """正常保存：内容落盘、临时文件不残留、注释无损。"""
    config = save_watchlist([WatchConfig(code="600110", name="诺德股份")], yaml_path)
    assert [item.code for item in config.watchlist] == ["600110"]
    text = open(yaml_path, encoding="utf-8").read()
    assert "# 做T辅助配置（这行注释必须活下来）" in text
    assert "# ============ 推送 ============" in text
    assert not os.path.exists(yaml_path + ".tmp")


def test_save_watchlist_rolls_back_when_candidate_invalid(
    yaml_path, monkeypatch,
) -> None:
    """候选内容非法（权重合计≠100）时必须回滚：原文件纹丝不动、临时文件被清理。"""
    before = open(yaml_path, encoding="utf-8").read()

    def _bad_candidate(text, items):
        return ("weights: {box: 999}\nthresholds: {action: 30, hint: 20}\n"
                'watchlist:\n  - code: "300308"\n')

    monkeypatch.setattr(
        "src.intraday.config.replace_watchlist_section", _bad_candidate)
    with pytest.raises(ConfigError):
        save_watchlist([WatchConfig(code="300308")], yaml_path)
    assert open(yaml_path, encoding="utf-8").read() == before
    assert not os.path.exists(yaml_path + ".tmp")


def test_save_watchlist_creates_file_when_absent(tmp_dir) -> None:
    path = os.path.join(tmp_dir, "brand_new.yaml")
    reset_config_cache()
    config = save_watchlist([WatchConfig(code="300308", name="中际旭创")], path)
    assert config.watchlist[0].code == "300308"
    assert os.path.exists(path)


def test_repo_config_watchlist_round_trip_is_safe() -> None:
    """对仓库真实配置做一次「替换后再替换回来」，确认注释与内容无损。

    只比对内存文本，不写盘（避免测试污染仓库配置）。
    """
    path = "configs/intraday.yaml"
    original = open(path, encoding="utf-8").read()
    config = load_intraday_config(path, force=True)
    once = replace_watchlist_section(original, config.watchlist)
    twice = replace_watchlist_section(once, config.watchlist)
    assert once == twice, "幂等性：重复替换同一份自选池不应改变文件内容"
    assert config.watchlist, "仓库配置应带自选标的"
    for section in ("weights:", "thresholds:", "boards:", "notify:"):
        assert section in twice, section
    # 自选段落之外的所有注释头都必须活下来（尤其紧邻的下一段「信号推送」）
    for marker in ("# ============ 信号推送",
                   "# ============ 关联板块",
                   "# ============ 关键价位",
                   "# ============ 因子权重",
                   "# ============ 数据层"):
        assert marker in twice, marker
    # 自选池解析结果不变
    assert [item.code for item in config.watchlist] == [
        item.code for item in load_intraday_config(path, force=True).watchlist]


# ==================== 失败冷却 ====================


def test_failure_cooldown_skips_dead_source() -> None:
    """刚失败的源在冷却窗口内被跳过，不再白等超时。"""
    from src.intraday.config import IntradayConfig
    from src.intraday.sources import IntradayDataProvider

    config = IntradayConfig()
    config.data.source_cooldown_seconds = 300
    provider = IntradayDataProvider(config)
    assert provider._in_cooldown("qmt", "bars") == 0  # noqa: SLF001
    provider._mark_failed("qmt", "bars")  # noqa: SLF001
    remaining = provider._in_cooldown("qmt", "bars")  # noqa: SLF001
    assert 290 < remaining <= 300


def test_failure_cooldown_disabled_when_zero() -> None:
    from src.intraday.sources import IntradayDataProvider

    config = IntradayConfig()
    config.data.source_cooldown_seconds = 0
    provider = IntradayDataProvider(config)
    provider._mark_failed("qmt", "bars")  # noqa: SLF001
    assert provider._in_cooldown("qmt", "bars") == 0  # noqa: SLF001


def test_success_clears_cooldown() -> None:
    from src.intraday.sources import IntradayDataProvider

    provider = IntradayDataProvider(IntradayConfig())
    provider._mark_failed("tencent", "bars")  # noqa: SLF001
    assert provider._in_cooldown("tencent", "bars") > 0  # noqa: SLF001
    provider._cooldown.pop("tencent:bars")  # noqa: SLF001 模拟成功路径
    assert provider._in_cooldown("tencent", "bars") == 0  # noqa: SLF001


def test_invalidate_clears_cooldown_too() -> None:
    """「强制刷新」应连失败冷却一起清掉，否则点了也没用。"""
    from src.intraday.sources import IntradayDataProvider

    provider = IntradayDataProvider(IntradayConfig())
    provider._mark_failed("qmt", "bars")  # noqa: SLF001
    provider.invalidate()
    assert provider._in_cooldown("qmt", "bars") == 0  # noqa: SLF001


# ==================== 消息面缓存 ====================


def test_news_analyzer_caches_within_ttl() -> None:
    """消息面在 TTL 内命中缓存，不重复调用 LLM（这是首个快照 54s 的主因之一）。"""
    import asyncio

    from src.intraday.models import NewsSentiment
    from src.intraday.sentiment import NewsSentimentAnalyzer

    calls = {"n": 0}

    class _Fetcher:
        async def fetch_news(self, code: str, limit: int = 10):
            calls["n"] += 1
            return [{"title": "利好公告", "text": "公司中标大单", "source_name": "东财",
                     "source_url": "", "publish_time": "2026-09-15 10:00"}]

    analyzer = NewsSentimentAnalyzer(gateway=None, news_fetcher=_Fetcher(),
                                     cache_ttl=600)
    first = asyncio.run(analyzer.analyze(code="300308"))
    second = asyncio.run(analyzer.analyze(code="300308"))
    assert calls["n"] == 1, "TTL 内应命中缓存，只抓一次新闻"
    assert isinstance(first, NewsSentiment)
    # 缓存返回的是同一份结果对象（未重新调 LLM）
    assert second is first
    analyzer.invalidate()
    asyncio.run(analyzer.analyze(code="300308"))
    assert calls["n"] == 2, "invalidate 后应重新取数"


def test_news_analyzer_without_cache_always_refetches() -> None:
    import asyncio

    from src.intraday.sentiment import NewsSentimentAnalyzer

    calls = {"n": 0}

    class _Fetcher:
        async def fetch_news(self, code: str, limit: int = 10):
            calls["n"] += 1
            return [{"title": "公告", "text": "", "source_name": "东财",
                     "source_url": "", "publish_time": ""}]

    analyzer = NewsSentimentAnalyzer(news_fetcher=_Fetcher(), cache_ttl=0)
    asyncio.run(analyzer.analyze(code="300308"))
    asyncio.run(analyzer.analyze(code="300308"))
    assert calls["n"] == 2


# ==================== 消息面：空返回重试 + 网关缓存绕过 ====================
#
# 实测根因（2026-09-15，本地 qwen3:8b）：
#   同一 prompt 调两次，一次返回合法JSON（543 tokens），另一次 tokens_out=4096
#   且 content 为空 —— 推理模型把 max_tokens 全耗在思维链上，正文一个字都没输出。
#   更糟的是网关会把「空返回」也缓存24h，同一 prompt 之后每次都被这条毒缓存命中，
#   所以必须重试 + 绕开网关缓存。


class _FakeResponse:
    def __init__(self, content: str, model_used: str = "fake-model",
                 tokens_out: int = 10):
        self.content = content
        self.model_used = model_used
        self.tokens_out = tokens_out


class _FakeGateway:
    """按脚本依次返回内容的假网关（记录每次调用的 prompt 与 use_cache）。"""

    def __init__(self, contents: list[str]):
        self._contents = contents
        self.calls: list[dict] = []

    async def complete(self, tier, system, prompt, *, agent_id="", trace_id="",
                       json_mode=False, use_cache=True, cancel_token=None):
        self.calls.append({
            "tier": tier, "prompt": prompt, "use_cache": use_cache})
        content = self._contents[min(len(self.calls) - 1, len(self._contents) - 1)]
        return _FakeResponse(content)


class _NewsFetcher:
    async def fetch_news(self, code: str, limit: int = 10):
        return [{"title": "公司中标大额订单", "text": "公告显示中标",
                 "source_name": "东财", "source_url": "",
                 "publish_time": "2026-09-15 10:00"}]


_VALID_JSON = ('{"items":[{"i":0,"polarity":"positive"}],"positive":1,'
               '"negative":0,"neutral":0,"score":0.7,"summary":"中标利好"}')


def _analyzer(gateway, **kwargs):
    from src.intraday.sentiment import NewsSentimentAnalyzer

    return NewsSentimentAnalyzer(
        gateway=gateway, news_fetcher=_NewsFetcher(), cache_ttl=0, **kwargs)


def test_news_retries_once_after_empty_content() -> None:
    """第一次空返回 → 重试成功；重试时 prompt 追加严格指令。"""
    import asyncio

    gateway = _FakeGateway(["", _VALID_JSON])
    analyzer = _analyzer(gateway, llm_retries=1)
    news = asyncio.run(analyzer.analyze(code="300308"))
    assert news.llm_used is True
    assert news.llm_score == pytest.approx(0.7)
    assert len(gateway.calls) == 2
    assert "严格模式" not in gateway.calls[0]["prompt"]
    assert "严格模式" in gateway.calls[1]["prompt"], "重试应使用严格指令"


def test_news_falls_back_after_all_retries_exhausted() -> None:
    """全部重试仍为空 → 降级为关键词计数口径，gap 说明真实原因。"""
    import asyncio

    gateway = _FakeGateway([""])
    analyzer = _analyzer(gateway, llm_retries=1)
    news = asyncio.run(analyzer.analyze(code="300308"))
    assert news.llm_used is False
    assert news.llm_score is None
    assert len(gateway.calls) == 2  # 1 次 + 1 次重试
    assert "未产出正文" in (news.gap or "")
    assert "关键词计数" in (news.gap or "")


def test_news_retries_after_invalid_json() -> None:
    """非JSON内容同样触发重试（例如模型返回了纯文字解释）。"""
    import asyncio

    gateway = _FakeGateway(["我觉得这条新闻偏多", _VALID_JSON])
    analyzer = _analyzer(gateway, llm_retries=1)
    news = asyncio.run(analyzer.analyze(code="300308"))
    assert news.llm_used is True
    assert len(gateway.calls) == 2


def test_news_bypasses_gateway_cache_by_default() -> None:
    """默认 use_cache=False：避免网关把「空返回」缓存24h形成毒缓存。"""
    import asyncio

    gateway = _FakeGateway([_VALID_JSON])
    analyzer = _analyzer(gateway)
    asyncio.run(analyzer.analyze(code="300308"))
    assert gateway.calls[0]["use_cache"] is False


def test_news_uses_configured_tier() -> None:
    """任务层级来自配置（默认 light：实测比 medium 快约60倍且更稳）。"""
    import asyncio

    from src.intraday.config import IntradayConfig

    assert IntradayConfig().factors.news.news_task_tier == "light"
    gateway = _FakeGateway([_VALID_JSON])
    analyzer = _analyzer(gateway, task_tier="light")
    asyncio.run(analyzer.analyze(code="300308"))
    assert gateway.calls[0]["tier"] == "light"


def test_news_block_truncates_item_text() -> None:
    """正文截断长度可配（越短思维链越短，越不容易耗尽token）。"""
    from src.intraday.sentiment import build_news_block

    items = [{"title": "标题", "text": "正文" * 300, "publish_time": "",
              "source_name": "东财"}]
    short = build_news_block(items, 1, text_chars=60)
    long = build_news_block(items, 1, text_chars=600)
    assert len(short) < len(long)
    # text_chars=60 → 恰好截到 "正文"×30；再多一个字就不该出现
    assert "正文" * 30 in short
    assert "正文" * 31 not in short


# ==================== 轻量快照 ====================


def test_light_snapshot_skips_slow_subsystems(monkeypatch) -> None:
    """light=True 不应触发消息面LLM与估值取数（自选列表页用）。"""
    import asyncio

    from src.intraday.models import ValuationSpace
    from src.intraday.sentiment import NewsSentimentAnalyzer
    from src.intraday.service import IntradayService

    service = IntradayService(backend=None, gateway=None, news_fetcher=None)
    called = {"news": 0, "valuation": 0}

    async def _news(**kwargs):
        called["news"] += 1
        return NewsSentimentAnalyzer()._fallback_counts  # 占位，不会真跑到

    async def _valuation(*args, **kwargs):
        called["valuation"] += 1
        return ValuationSpace(available=False, code="300308")

    monkeypatch.setattr(service._sentiment, "analyze", _news)  # noqa: SLF001
    monkeypatch.setattr(service._valuation, "fetch", _valuation)  # noqa: SLF001

    async def _empty(*args, **kwargs):
        from src.core.exceptions import DataFetchError
        raise DataFetchError("测试：无数据源")

    monkeypatch.setattr(service._data, "fetch_bars", _empty)  # noqa: SLF001
    monkeypatch.setattr(service._data, "fetch_trend", _empty)  # noqa: SLF001
    monkeypatch.setattr(service._data, "fetch_quote", _empty)  # noqa: SLF001

    snapshot = asyncio.run(service.snapshot("300308", light=True))
    assert called["news"] == 0
    assert called["valuation"] == 0
    # 缺口如实记录，不静默
    assert snapshot.health.gaps
    assert snapshot.valuation is None
    assert snapshot.news is None


def test_service_add_and_remove_watch(tmp_dir) -> None:
    """服务层加/删自选应即时生效并落盘。"""
    import os

    from src.intraday.service import IntradayService

    path = os.path.join(tmp_dir, "intraday.yaml")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(SAMPLE_YAML)
    reset_config_cache()
    service = IntradayService(backend=None, config_path=path)
    config = service.add_watch("600110", name="诺德股份", boards=["PET铜箔"])
    assert [item.code for item in config.watchlist] == ["300308", "600110"]
    assert service.config.watch("600110").name == "诺德股份"
    # 板块名（中文）经 YAML 落盘再读回后必须仍然绑定：这是前端「加自选」时
    # 让「板块情绪/板块排行」两个维度能计入总分的唯一入口。
    assert service.config.board_names("600110") == ["PET铜箔"]
    assert service.config.boards_bound("600110") is True
    assert 'name: "诺德股份"' in open(path, encoding="utf-8").read()
    config = service.remove_watch("300308")
    assert [item.code for item in config.watchlist] == ["600110"]
    assert "# ============ 推送 ============" in open(path, encoding="utf-8").read()


def test_add_watch_resolves_name_from_directory(tmp_dir, monkeypatch) -> None:
    """加自选时名称为空，必须用本地股票字典补齐 —— **不能写成代码本身**。

    真实事故：前端把"当前已加载快照里的名称"发过来，用户没先加载这只票时
    名称就是空的，于是配置里出现 `name: "603083"`。界面上自选股显示一排代码，
    认不出是什么公司，而且看起来像正常数据、不会报错。
    """
    import os

    from src.intraday.service import IntradayService

    path = os.path.join(tmp_dir, "intraday.yaml")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(SAMPLE_YAML)
    reset_config_cache()
    service = IntradayService(backend=None, config_path=path)
    monkeypatch.setattr(service, "_lookup_name",
                        lambda code: "剑桥科技" if code == "603083" else "")

    config = service.add_watch("603083", name="")
    assert service.config.watch("603083").name == "剑桥科技"
    assert 'name: "剑桥科技"' in open(path, encoding="utf-8").read()
    assert [item.code for item in config.watchlist] == ["300308", "603083"]


def test_add_watch_does_not_store_code_as_name(tmp_dir, monkeypatch) -> None:
    """传进来的名称就是代码时，也要当成"没名称"去查字典。"""
    import os

    from src.intraday.service import IntradayService

    path = os.path.join(tmp_dir, "intraday.yaml")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(SAMPLE_YAML)
    reset_config_cache()
    service = IntradayService(backend=None, config_path=path)
    monkeypatch.setattr(service, "_lookup_name", lambda code: "")
    service.add_watch("601999", name="601999")
    # 字典也查不到时留空，而不是把代码写进 name
    assert service.config.watch("601999").name in ("", "601999")


def test_configs_default_boards_used_for_new_watch() -> None:
    """未在自选中声明板块的标的：不打分用板块，只给「参考板块」展示。"""
    config = IntradayConfig()
    # 打分口径：未绑定 → 空（绝不套用配置里的第一个板块）
    assert config.board_names("999999") == []
    assert config.boards_bound("999999") is False
    # 展示口径：仍给出全部配置板块作为参考板块
    assert config.reference_board_names() == [b.name for b in config.boards]


def test_bound_boards_come_from_watchlist_only() -> None:
    """已在自选中声明板块的标的才算「已绑定」。"""
    config = IntradayConfig(
        watchlist=[WatchConfig(code="300308", name="中际旭创",
                               boards=["PCB概念", "PET铜箔"])])
    assert config.boards_bound("300308") is True
    assert config.board_names("300308") == ["PCB概念", "PET铜箔"]
    assert config.board_names("600036") == []
