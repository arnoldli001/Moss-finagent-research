"""分析师名必须**出得去接口**，而且四类内容**不依赖模型**都得在。

> 用户口径（2026-10-01）：
>
>   "建议直接让本地模型输出时，直接输出：相关个股/板块 + 事件一句话摘要
>   （如果有以下内容必须要输出：股票名 板块名 券商 孙潇雅、赵宇阳、武超则、
>    陈果、刘晨明、洪灏），推送到前端展示。"
>   "券商名字不一定带券商两个字，比如可能是天风电子，招商电子。"

## 这个文件守的是"必须"两个字

**提示词不是保证**：把"必须输出"写进 prompt，模型漏一个用户就漏一个 ——
而这次需求的全部目的就是"别漏掉谁在唱多/唱空"。所以那四类的展示保证
长在**确定性规则层**上，本文件把它端到端钉住：

    股票名 + 板块名   `vocab.scan`（词表扫描，与高亮同一份）
    券商              `alert_rules` 的两种署名形态
    分析师名          `alert_rules.ANALYST_WATCHLIST`（用户点名的六人）
    合并位置          `service.build_feed`（清洗后全文再扫一遍、取并集）
                      + 契约层 `IntelItem.to_public()`

## 另一条同样重要的纪律：机构/分析师**不能进个股列表**

用户的原话是"目的就是找到那些股被唱多，唱空"。一个券商名或分析师名
出现在"个股"那一栏里，这个问题的答案就被污染了 —— 而它长得**完全合理**
（原文里逐字有、逐字校验必然放行）。

## 三个"静默少一块"的失败方式，各有一条用例

    `to_public()` 漏字段       → 前端永远看不到（不报错）
    `_group_row()` 漏透传      → 展开收容组后看不到（顶层还正常，像渲染 bug）
    `IntelFeed.to_public()` 误剥 → 与 `extract_text` 那次事故同一类
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.domain.intel import alert_rules as R
from src.domain.intel import service as S
from src.domain.intel import tone, tone_store, vocab
from src.domain.intel.service import IntelFeed
from src.infrastructure.connectors.intel_sources import (
    FORBIDDEN_PUBLIC_KEYS,
    IntelItem,
)

#: 受控词表：**不读**数据仓/行情仓（同 `test_intel_vocab` 的做法）。
_FAKE_ENTRIES = [
    vocab.VocabEntry("存储芯片", vocab.KIND_BOARD, board_code="886042.TI"),
    vocab.VocabEntry("中芯国际", vocab.KIND_STOCK, code="688981"),
    vocab.VocabEntry("隆基绿能", vocab.KIND_STOCK, code="601012"),
]

#: 一条**四类齐全**的真实形态笔记：
#: `国金证券`（券商）+ `孙潇雅`（用户点名的分析师）+ `存储芯片`（板块）
#: + `中芯国际`（个股）。长度 > `MIN_CHARS_FOR_EXTRACTION`，所以它走模型那条路。
_NOTE = (
    "【国金证券】孙潇雅：存储芯片景气上行，中芯国际受益于扩产，"
    "公司订单饱满，机构上调盈利预测。"
    "就当前时点看，下游需求回暖的持续性仍需观察，但订单能见度已明显改善，"
    "我们维持对板块的正面看法，并提示关注后续产能释放节奏与价格传导情况。"
    "综上，推荐关注设备与材料两个环节的龙头公司。"
)

#: 模型**什么都没抽到**的那种回复（实测最常见的失败：输出合法但字段全空）。
_EMPTY_REPLY = json.dumps({
    "summary": "", "events": [], "tone": "未定",
    "phrases": [], "codes": [],
    "bull_industries": [], "bull_stocks": [],
    "bear_industries": [], "bear_stocks": [],
    "brokers": [], "analysts": [],
}, ensure_ascii=False)


@pytest.fixture(autouse=True)
def _fake_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vocab, "_TABLE", vocab.build_table(_FAKE_ENTRIES))


def _item(**kw) -> IntelItem:
    base = dict(
        kind="research_note",
        title="【国金证券】孙潇雅：存储芯片与中芯国际",
        summary=_NOTE,
        published_at="2026-10-01 10:00:00",
        source_alias="research-note-zsxq",
        source_name="知识星球-调研纪要",
        content_hash="h-analysts",
    )
    base.update(kw)
    return IntelItem(**base)


# ======================================================================
# ① 契约层：`analysts` 出得去（与 `institutions` 完全平行）
# ======================================================================

def test_item_export_carries_analysts() -> None:
    """`to_public()` **必须**带 `analysts`（前端"分析师：孙潇雅"的数据源）。"""
    pub = _item().to_public()
    assert pub["analysts"] == ["孙潇雅"]


def test_item_export_analysts_is_empty_list_when_none_found() -> None:
    """没命中时给**空列表**而不是缺键 —— 前端不必写 `?.` 兜底。"""
    pub = _item(title="隔夜美股收跌",
                summary="三大指数收跌，成交量小幅放大" * 8,
                kind="newswire", source_alias="newswire-sina",
                source_name="新浪-7x24快讯").to_public()
    assert pub["analysts"] == []


def test_feed_export_does_not_strip_analysts() -> None:
    """⚠️ `IntelFeed.to_public()` 是**剔除式**的：只有 `internal` 里的键会被摘掉。

    这条用例钉住"`analysts` **不在**那张剔除表里"。一旦有人（很合理地）
    以为它是内部字段而加进去，前端就再也看不到分析师名了 ——
    而那种改动不会让任何东西报错，只表现为"这块功能好像没做"。
    """
    feed = IntelFeed(items=[_item().to_public()], fetched_at="now")
    feed.items[0]["extract_text"] = "全文" * 100
    feed.items[0]["market_terms"] = ["孙潇雅"]
    stripped = feed.to_public()["items"][0]
    assert stripped["analysts"] == ["孙潇雅"]
    # 对照：真正的内部字段确实被剥掉了（证明这条链路是生效的）
    assert "extract_text" not in stripped
    assert "market_terms" not in stripped


def test_group_row_passes_analysts_and_direction_marker_through() -> None:
    """收容组里的子条目是**另一个 dict**（`_group_row` 白名单投影）。

    ⚠️ 不透传的表现与 `extract_text` 那次事故一模一样：组内条目展开后
    看不到分析师名，而**没有任何报错**（顶层还正常，看起来像渲染 bug）。
    方向标记（【多】/【空】）要的最小投影同理。
    """
    row = S._group_row({
        "title": "t", "summary": "s",
        "institutions": ["天风证券"], "analysts": ["赵宇阳"],
        "tone": {"tone": "偏多", "has_tone": True, "source": "rules",
                 "explain": "x" * 200},
        "extract_text": "x",
    })
    assert row["analysts"] == ["赵宇阳"]
    assert row["institutions"] == ["天风证券"]
    # 方向标记只要三个值；整份 tone（含 explain/bullish 这些大字段）**不透传**。
    # `source` 是 2026-10-01 加的：它让 tooltip 能说明"这是词表给的还是模型给的"
    # —— 后端在请求路径上用规则层兜底给方向（用户报障的那条笔记），
    # 而把词表猜测说成模型判定就是"把请求当保证"的同类错误。
    assert row["tone"] == {"tone": "偏多", "has_tone": True, "source": "rules"}
    # 大字段确实没被带出去（这条才是"最小投影"的真正判据）
    assert "explain" not in row["tone"] and "bullish" not in row["tone"]


def test_group_row_keeps_when_there_is_no_tone() -> None:
    """没有倾向字段（未抽取 / 老数据）时给**空字典**，前端不必写兜底。"""
    assert S._group_row({"title": "t", "summary": "s"})["tone"] == {}


# ======================================================================
# ② 端到端：模型**什么都没抽到**，四类照样在（这就是"必须"的落地）
# ======================================================================

class _StubTopic:
    def __init__(self, title: str, text: str, content_hash: str) -> None:
        self.title = title
        self.text = text
        self.created_at = datetime.now(timezone.utc).astimezone().strftime(
            "%Y-%m-%dT%H:%M:%S+0800")
        self.content_hash = content_hash


class _StubIncremental:
    def __init__(self, topics: list[_StubTopic]) -> None:
        self.topics = topics
        self.watermark = "2026-10-01T00:00:00+0800"
        self.new_count = len(topics)
        self.truncated = False
        self.newest = "2026-10-01T23:59:59+0800"


class _FakeGateway:
    """假网关：**只回空字段**（模拟"模型这次什么都没抽到"）。"""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = 0

    async def complete(self, *args: object, **kwargs: object) -> object:
        self.calls += 1
        return type("R", (), {"content": self.reply})()


def _fresh_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """把 `tone_store` 换到临时目录（**不碰真实存储**）。

    ⚠️ 这里 patch 的是 `store_path` 本身，而不是传 `root=`：
    `service.build_feed` 读倾向时调的是 `tone_store.get(hash)`（**没有 root**），
    只有把路径函数换掉，两条链路才落在同一个文件上。
    """
    monkeypatch.setattr(tone_store, "store_path",
                        lambda root=None: tmp_path / "tone_results.jsonl")
    monkeypatch.setattr(tone_store, "_CACHE", {})
    monkeypatch.setattr(tone_store, "_LOADED", True)


def _patch_feeds(monkeypatch: pytest.MonkeyPatch, note: _StubTopic) -> None:
    async def _fake_fetch_all(**_kw):
        return [], {}

    monkeypatch.setattr(
        "src.infrastructure.connectors.intel_sources.fetch_all", _fake_fetch_all)
    monkeypatch.setattr(
        "src.infrastructure.connectors.zsxq_incremental.fetch_incremental",
        lambda: _StubIncremental([note]))
    monkeypatch.setattr(
        "src.infrastructure.connectors.zsxq_incremental.save_watermark",
        lambda *a, **k: None)


def test_all_four_categories_survive_an_empty_model_reply(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """★★ 核心用例：模型字段**全空**时，四类内容仍然一个不少地出现在导出里。

    构造顺序刻意与现实一致：

      1. 抽取任务跑一遍，模型回的是"合法但什么都没有"的 JSON（实测最常见）；
      2. `/feed` 走 `build_feed`，只从存储里读倾向；
      3. 断言四类都在条目的**最终形状**里 —— 它们与模型输出无关。

    这条用例同时防住两类退化：
      · 有人把四类改成"信模型"（提示词一漏，用户就漏）
      · 有人把某个键从 `to_public()` / `_group_row()` / 展示层里删掉
    """
    from src.domain.intel import tone_job

    _fresh_store(monkeypatch, tmp_path)
    note = _StubTopic("【国金证券】孙潇雅：存储芯片与中芯国际", _NOTE, "h-note-1")
    _patch_feeds(monkeypatch, note)

    # ── 1) 抽取：模型什么都没抽到 ──
    gateway = _FakeGateway(_EMPTY_REPLY)
    stats = asyncio.run(tone_job.run_once(
        gateway=gateway, items=[{
            "content_hash": "h-note-1", "title": note.title,
            "summary": _NOTE, "published_at": note.created_at,
            "credibility": {"score": 58},
        }], root=tmp_path))
    assert gateway.calls == 1, "长文本没有走模型（这条用例的前提不成立）"
    assert stats["written"] == 1
    hit = tone_store.get("h-note-1", root=tmp_path)
    assert hit is not None
    # 模型侧的四个实体字段确实是空的 —— 说明下面的四类**不是**模型给的
    assert hit["bullish"]["stocks"] == [] and hit["bullish"]["industries"] == []

    # ── 2) 情报流 ──
    feed = asyncio.run(S.build_feed(limit=50, group_undetermined=False))
    rows = [it for it in feed.items if it.get("content_hash") == "h-note-1"]
    assert rows, f"笔记没进流水线：{[i.get('title') for i in feed.items]}"
    it = rows[0]

    # ── 3) 四类都在 ──
    assert "中芯国际" in it["highlights"], f"个股没进高亮：{it['highlights']}"
    assert "存储芯片" in it["highlights"], f"板块没进高亮：{it['highlights']}"
    assert it["institutions"] == ["国金证券"], it["institutions"]
    assert it["analysts"] == ["孙潇雅"], it["analysts"]

    # 机构 / 分析师**不能**混进个股/板块那一栏（用户要的是"哪些股"）
    assert "国金证券" not in it["highlights"]
    assert "孙潇雅" not in it["highlights"]

    # 而且它们在**导出后**依然在（剥了一层就前功尽弃）
    pub = feed.to_public()["items"][0]
    assert pub["analysts"] == ["孙潇雅"]
    assert pub["institutions"] == ["国金证券"]
    assert "孙潇雅" in pub["highlights"] or "中芯国际" in pub["highlights"]


def test_analysts_do_not_leak_channel_identity() -> None:
    """分析师名是**原文里写的人**，不是"这条来自知识星球"。

        分析师名   公开署名内容 → 可以出接口
        渠道身份   我们用了哪个星球/群 → 一律不出（`source_pseudonym`）
    """
    pub = _item().to_public()
    assert pub["source_alias"] != "research-note-zsxq"
    assert pub["source_alias"].startswith("src-")
    assert pub["platform"] == ""
    for name in pub["analysts"]:
        assert "星球" not in name and "调研" not in name
    assert not (set(pub) & FORBIDDEN_PUBLIC_KEYS)
    assert "source_name" not in pub


def test_contract_layer_and_rule_layer_agree_on_analysts() -> None:
    """契约层（未清洗原文）与规则层（清洗后文本）**用同一份实现**。

    两处各写一遍判据必然漂移，而漂移的表现是"前端显示了名字、
    告警侧却没命中"（或反过来）—— 两边对不上账。
    """
    item = _item()
    pub = item.to_public()
    layer = R.analysts({"title": item.title, "summary": item.summary})
    assert pub["analysts"] == layer == ["孙潇雅"]


# ======================================================================
# ③ 机构名 / 分析师名**绝不能**进个股列表
# ======================================================================

@pytest.mark.parametrize("name", ["国金证券", "天风电子", "招商电子",
                                  "孙潇雅", "赵宇阳"])
def test_research_house_and_analyst_are_never_stocks(name: str) -> None:
    """★★ 用户口径："目的就是**找到那些股**被唱多，唱空。"

    机构名/分析师名混进个股列表会把答案污染掉，而它看起来**完全合理**
    （原文里逐字有、逐字校验必然放行）。所以个股/行业两侧都要硬拦截：
    四类里只有"个股/板块"属于标的，另外两类有它们自己的字段。
    """
    bull, _, rejected = tone.validate_entities(
        {"bull_industries": [name], "bull_stocks": [name]},
        _NOTE + f" {name} 的观点")
    assert bull["stocks"] == [], f"{name} 被当成了个股"
    assert bull["industries"] == [], f"{name} 被当成了板块"
    assert name in rejected["bull_stocks"] and name in rejected["bull_industries"]


def test_real_stock_is_still_accepted() -> None:
    """对照：真正的个股照旧能进来（拦截不是把整个字段清空）。

    ⚠️ 没有这条对照，把 `_valid_name` 改成 `return False` 也能让上面那条通过。
    """
    bull, _, _ = tone.validate_entities({"bull_stocks": ["中芯国际"]}, _NOTE)
    assert bull["stocks"] == [
        {"name": "中芯国际", "code": "688981", "count": 1}]


def test_model_analysts_are_audited_but_not_displayed() -> None:
    """模型给的机构/分析师名：**逐字校验后落库审计**，但不直接上屏。

    ⚠️ 这是刻意的取舍（见 `tone.validate_people`）："逐字在原文里"挡不住
    "这个词在这段文字里，但它不是人名"（模型会把公司名、产品名写进来）。
    名单内的六人由规则层保证上屏，所以这里放宽没有收益、只有风险。
    """
    obj = {"brokers": ["国金证券", "华为"], "analysts": ["孙潇雅", "存储芯片"]}
    brokers, analysts, rejected = tone.validate_people(obj, _NOTE + " 华为")
    assert "国金证券" in brokers
    assert "孙潇雅" in analysts
    # `存储芯片` 是板块名（词表里有），不是人名 → 丢掉并如实记进 rejected
    assert "存储芯片" not in analysts
    assert rejected["analysts"] == ["存储芯片"]


def test_model_analysts_survive_the_store_round_trip(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """★ 审计字段要能**从存储里读回来**（白名单漏一个键 = 静默留在进程里）。

    `tone_store` 有两道白名单（写入 `_ROW_KEYS` + 读取 `view()`），
    漏任何一处都**不会报错**：表现只是"提示词要了、模型给了、排障时查不到"。
    所以这里跑一遍真实的落库 + 读取。
    """
    from src.domain.intel import tone_job

    _fresh_store(monkeypatch, tmp_path)
    reply = json.dumps({
        "summary": "存储芯片景气上行", "events": [],
        "tone": "偏多", "phrases": [], "codes": [],
        "bull_industries": [], "bull_stocks": [],
        "bear_industries": [], "bear_stocks": [],
        "brokers": ["国金证券"], "analysts": ["孙潇雅"],
    }, ensure_ascii=False)
    asyncio.run(tone_job.run_once(
        gateway=_FakeGateway(reply),
        items=[{"content_hash": "h-audit", "title": "【国金证券】孙潇雅",
                "summary": _NOTE, "published_at": "2026-10-01 10:00:00",
                "credibility": {"score": 58}}],
        root=tmp_path))
    hit = tone_store.get("h-audit", root=tmp_path)
    assert hit is not None
    assert hit["brokers"] == ["国金证券"], hit.get("brokers")
    assert hit["analysts"] == ["孙潇雅"], hit.get("analysts")
    # 老行没有这两个键 → `view()` 给空列表（向后兼容）
    view = tone_store.view({"content_hash": "old", "tone": "未定"})
    assert view["brokers"] == [] and view["analysts"] == []


def test_prompt_and_schema_request_brokers_and_analysts() -> None:
    """提示词与 schema **都要**提出这两个字段（少一处模型就给不出来）。

    ⚠️ 但要注意：**提示词不是"必须输出"的保证** —— 保证在
    `build_feed` 的确定性合并那一侧（见本文件第一条端到端用例）。
    这条用例只是防止"要了却没在结构里声明"（受约束解码只输出 schema 里的键，
    表现是"模型总是抽不到这两个字段"）。
    """
    prompt = tone.build_prompt(_NOTE)
    assert "brokers" in prompt and "analysts" in prompt
    assert "孙潇雅" in prompt, "用户点名的分析师要在提示词里出现（给模型示例）"
    schema = tone.extraction_schema()
    assert "brokers" in schema["required"]
    assert "analysts" in schema["required"]


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
