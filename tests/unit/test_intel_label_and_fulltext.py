"""展示名改名（研究笔记 → 券商作文）与"点击看全文"的落点。

## 需求一：`research_note` 的**展示名**改成「券商作文」

用户口径（2026-10-01）：`research_note` 这一类的界面文案要叫「券商作文」。

⚠️ **内部 `kind` 值一个字都不改**：它是 `tone_store` / `tone_job` / 前端筛选 /
告警闸门共用的**键**，改名会让所有已落库的行与所有按 kind 分流的判据集体失配
—— 而那种失配不报错，只表现为"某一类内容凭空消失"。
所以本文件只钉**给人看的那一面**，并把它在**三处**（连接器表 / 聚合层表 /
前端兜底表）钉成同一个值：三处各改各的必然漂移，漂移的表现是
"列表里叫券商作文、筛选项里还叫研究笔记"。

## 需求二：点击看全文 —— 存哪里、经哪条路出去

    `summary`  260 字展示截断（`SUMMARY_MAX_BY_KIND`）—— 契约层不变
    `extract_text`  清洗后的**全文**，只在进程内传递给 `tone_job`
    本存储   `body_store`（JSONL，按 `content_hash`），由定时任务写入
    读取   `GET /api/v1/intel/item/{content_hash}`（**纯读，零模型调用**）

两条反向的纪律各有用例：

    绝不出 `/feed`   全文（`IntelFeed.to_public()` 继续剥 `extract_text`）
    出得去           `/item/{hash}` 的六个键（且**不含**任何渠道身份）
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException

from src.api.routes import intel as INTEL_ROUTE
from src.domain.intel import body_store, tone_store
from src.domain.intel import service as S
from src.infrastructure.connectors.intel_sources import SOURCE_KINDS

#: 仓库根（前端源码断言要用）
_ROOT = Path(__file__).resolve().parents[2]

#: 全文**末段**的独特标记：它落在 260 字展示截断**之外**，
#: 只有真正拿到全文才看得见（"有没有泄漏全文"的判据就靠它）。
_TAIL_MARKER = "碳化硅衬底环节由天岳先进主导，第三代半导体设备国产化提速。"

#: 中间填充：把标记推到展示截断（`SUMMARY_MAX_BY_KIND["research_note"] = 260`）之后。
_FILLER = "就当前时点看，下游需求回暖的持续性仍需观察，但订单能见度已明显改善，"

_NOTE = (
    "【天风电子】半导体设备景气上行，存储芯片价格连续上涨，中芯国际受益于扩产，"
    "机构上调盈利预测；光伏组件价格下滑，隆基绿能承压。"
    + _FILLER * 8
    + _TAIL_MARKER
)


# ======================================================================
# 需求一：展示名
# ======================================================================

def test_backend_label_tables_use_the_new_display_name() -> None:
    """后端两张标签表都要叫「券商作文」，且内部 key 不变。"""
    assert SOURCE_KINDS["research_note"] == "券商作文"
    assert S.KIND_LABELS["research_note"] == "券商作文"
    # ⚠️ 键**必须**还是 `research_note`（改名等于让所有落库的行失配）
    assert "券商作文" not in SOURCE_KINDS and "券商作文" not in S.KIND_LABELS
    # 旧的展示名不许再留在**任何**给人看的表里
    for table in (SOURCE_KINDS, S.KIND_LABELS):
        assert "研究笔记" not in table.values(), table


def test_frontend_fallback_label_map_matches_the_backend() -> None:
    """前端兜底表必须跟后端同值（它是接口没给 `kind_label` 时的兜底）。

    三处（连接器 / 聚合层 / 前端）各写各的必然漂移，而漂移的表现是
    "列表里叫券商作文、筛选或兜底文案里还叫研究笔记"。
    """
    src = (_ROOT / "web" / "src" / "intelApi.ts").read_text(encoding="utf-8")
    block = re.search(r"KIND_FALLBACK_LABELS[^{]*\{(?P<body>[^}]*)\}", src)
    assert block, "找不到前端标签表 KIND_FALLBACK_LABELS"
    body = block.group("body")
    assert 'research_note: "券商作文"' in body, body
    assert "研究笔记" not in body, f"前端兜底表里还有旧展示名：{body}"


def test_first_run_gap_message_uses_the_new_display_name(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """缺口文案是**用户直接看到的字**，必须跟着展示名走。

    ⚠️ 只改 `KIND_LABELS` 不够：这些 message 是硬编码的中文句子
    （见 `service.build_feed` 里知识星球失败那两段），漏改会让提示里
    留下一个界面上再也找不到的旧名字。
    """
    async def _fake_fetch_all(**_kw):
        return [], {}

    monkeypatch.setattr(
        "src.infrastructure.connectors.intel_sources.fetch_all", _fake_fetch_all)
    monkeypatch.setattr(
        "src.infrastructure.connectors.zsxq_incremental.fetch_incremental",
        lambda: (_ for _ in ()).throw(RuntimeError("测试里让该源失败")))
    monkeypatch.setattr(
        "src.infrastructure.connectors.zsxq_incremental.save_watermark",
        lambda *a, **k: None)

    feed = asyncio.run(S.build_feed(limit=10, group_undetermined=False))
    msgs = " ".join(g.get("message", "") for g in feed.gaps)
    assert "券商作文" in msgs, feed.gaps
    assert "研究笔记" not in msgs, feed.gaps


# ======================================================================
# 需求二 · 存储层：`body_store`
# ======================================================================

def _fresh_body_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """把全文存储换到临时目录（**不碰真实存储**）。"""
    monkeypatch.setattr(body_store, "store_path",
                        lambda root=None: tmp_path / "item_bodies.jsonl")
    monkeypatch.setattr(body_store, "_CACHE", {})
    monkeypatch.setattr(body_store, "_LOADED", True)


def _row(h: str, text: str = "正文", **kw) -> dict:
    base = {"content_hash": h, "at": body_store._now(), "title": "标题",
            "text": text, "published_at": "2026-10-01 10:00:00",
            "kind": "research_note"}
    base.update(kw)
    return base


def test_store_read_shape_is_a_whitelist(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """读取侧形状**只有五个键** —— 存储行里多一个字段也出不去。"""
    _fresh_body_store(monkeypatch, tmp_path)
    body_store.save_many([{**_row("h1"), "source_alias": "research-note-zsxq",
                           "platform": "知识星球"}])
    hit = body_store.get("h1")
    assert hit is not None
    view = body_store.view(hit)
    assert set(view) == {"content_hash", "title", "text", "published_at", "kind"}
    assert "source_alias" not in json.dumps(view, ensure_ascii=False)


def test_build_row_prefers_cleaned_full_text(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`extract_text`（清洗后全文）优先；没有它才退回展示摘要。

    退回那一档不是妥协：快讯/研报/政策这些上游**只给到那么长**，
    摘要就是"我们手里有的全部正文"。
    """
    row = body_store.build_row({
        "content_hash": "h1", "title": "t", "summary": "摘要",
        "extract_text": "全文", "published_at": "2026-10-01"})
    assert row is not None and row["text"] == "全文"
    row = body_store.build_row({
        "content_hash": "h2", "title": "t", "summary": "摘要",
        "published_at": "2026-10-01"})
    assert row is not None and row["text"] == "摘要"


def test_build_row_skips_group_and_empty_items() -> None:
    """收容组是服务端合成的条目（没有指纹、没有原文）→ 跳过，不是错误。"""
    assert body_store.build_row({"is_group": True, "summary": "x"}) is None
    assert body_store.build_row({"content_hash": "", "text": "x"}) is None
    assert body_store.build_row({"content_hash": "h", "summary": "  "}) is None


def test_row_text_is_capped_without_rewriting_it(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """单条正文有上限（防上游偶发把整页 HTML 塞进来），且**不加省略号**。

    ⚠️ 加省略号就是改写正文，而 `extraction_text` 的契约是"一个字都不动"
    （`phrases` 要逐字可核对）。
    """
    _fresh_body_store(monkeypatch, tmp_path)
    monkeypatch.setattr(body_store, "MAX_TEXT_CHARS", 5)
    row = body_store.build_row({"content_hash": "h1", "summary": "一二三四五六七"})
    assert row is not None
    assert row["text"] == "一二三四五"
    assert "…" not in row["text"]


def test_save_many_short_circuits_on_the_same_hash(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """★ 同一个 `content_hash` 不重复写。

    ⚠️ 这条链路每 2 小时把同一批条目再送一次：不短路的话，
    append-only 文件一天涨十倍，而内容一个字都没变（prune 的整份重写
    也会跟着变慢）。`content_hash` 是内容指纹 ⇒ 同 hash 必然同内容。
    """
    _fresh_body_store(monkeypatch, tmp_path)
    assert body_store.save_many([_row("h1"), _row("h2")]) == 2
    assert body_store.save_many([_row("h1"), _row("h2"), _row("h3")]) == 1
    assert body_store.get("h3") is not None
    p = body_store.store_path(tmp_path)
    assert len(p.read_text(encoding="utf-8").splitlines()) == 3


def test_prune_drops_expired_rows(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """按 `at` 清过期行（窗口 = 情报流的 3 天）。"""
    _fresh_body_store(monkeypatch, tmp_path)
    old = (datetime.now(timezone.utc).astimezone()
           - timedelta(days=body_store.RETAIN_DAYS + 1)).isoformat(
               timespec="seconds")
    body_store.save_many([_row("fresh"), _row("stale", at=old)])
    assert body_store.prune(retain_days=body_store.RETAIN_DAYS) == 1
    assert body_store.get("stale") is None
    assert body_store.get("fresh") is not None
    # 重写走的是原子替换：临时文件不该留在盘上
    assert not body_store.store_path(tmp_path).with_suffix(".jsonl.tmp").exists()


def test_prune_enforces_hard_caps_oldest_first(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """★ 时间闸门之外还有**条数/字节**两道硬上限（防坏源一天灌几十万条）。

    没有这两道闸门时，prune 的"整份重写"会被一个涨到几百 MB 的文件拖垮，
    而表现是"接口越来越慢"，查不出是哪来的数据。
    """
    _fresh_body_store(monkeypatch, tmp_path)
    base = datetime.now(timezone.utc).astimezone()
    rows = [_row(f"h{i}", text="x" * 20,
                 at=(base - timedelta(minutes=i)).isoformat(timespec="seconds"))
            for i in range(5)]
    body_store.save_many(rows)
    # 条数上限：只留最新的 2 条
    assert body_store.prune(max_rows=2) == 3
    kept = body_store.load(force=True)
    assert set(kept) == {"h0", "h1"}, kept


def test_prune_enforces_byte_cap(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """字节上限同样生效（按最旧优先丢）。"""
    _fresh_body_store(monkeypatch, tmp_path)
    base = datetime.now(timezone.utc).astimezone()
    rows = [_row(f"h{i}", text="y" * 200,
                 at=(base - timedelta(minutes=i)).isoformat(timespec="seconds"))
            for i in range(4)]
    body_store.save_many(rows)
    removed = body_store.prune(max_bytes=900)
    assert removed >= 1
    assert body_store.get("h0") is not None, "最新的那条被误删"


def test_persist_reports_and_prunes(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """调度任务用的入口：**存 + 清一步完成**（只写不清会让磁盘悄悄涨满）。"""
    _fresh_body_store(monkeypatch, tmp_path)
    stats = body_store.persist([
        {"content_hash": "h1", "title": "t", "summary": "正文一二三"},
        {"is_group": True, "summary": "合成条目"},
        {"content_hash": "", "summary": "没有指纹"},
    ])
    assert stats["written"] == 1 and stats["considered"] == 3
    assert stats["pruned"] == 0
    assert body_store.get("h1") is not None


# ======================================================================
# 需求二 · 端点：`GET /intel/item/{content_hash}`
# ======================================================================

def _patch_feature_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_require_feature(request: object, feature: str):
        return ("u-test", "pro")

    monkeypatch.setattr(INTEL_ROUTE, "require_feature", _fake_require_feature)


def _fresh_tone_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(tone_store, "store_path",
                        lambda root=None: tmp_path / "tone_results.jsonl")
    monkeypatch.setattr(tone_store, "_CACHE", {})
    monkeypatch.setattr(tone_store, "_LOADED", True)


def test_item_endpoint_returns_full_text_and_minimum_metadata(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """★ 六个键、全文、以及**按 hash join 来的倾向**。"""
    _fresh_body_store(monkeypatch, tmp_path)
    _fresh_tone_store(monkeypatch, tmp_path)
    _patch_feature_gate(monkeypatch)
    body_store.save_many([_row("h1", text=_NOTE)])
    tone_store.save_many([{"content_hash": "h1", "tone": "偏多",
                           "has_tone": True}], root=tmp_path)

    got = asyncio.run(INTEL_ROUTE.intel_item("h1", None))
    assert set(got) == {"content_hash", "title", "text", "published_at",
                        "kind_label", "tone"}
    assert got["content_hash"] == "h1"
    assert got["text"] == _NOTE
    assert got["kind_label"] == "券商作文", "标签要现算（改名才能作用于老数据）"
    assert got["tone"] == "偏多"


def test_item_endpoint_gives_undetermined_when_not_extracted(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """没抽过倾向时给「未定」，**不编**（与情报流同一份存储、同一个默认值）。"""
    _fresh_body_store(monkeypatch, tmp_path)
    _fresh_tone_store(monkeypatch, tmp_path)
    _patch_feature_gate(monkeypatch)
    body_store.save_many([_row("h1")])
    assert asyncio.run(INTEL_ROUTE.intel_item("h1", None))["tone"] == "未定"


@pytest.mark.parametrize("h", ["不存在的指纹", "", "x" * 200])
def test_item_endpoint_404s_for_unknown_or_expired(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path, h: str) -> None:
    """未知 / 已过期 / 超长垃圾串**都是 404**，但**成因要分开**。

    用户报障（2026-10-01）看到的是「全文读取失败，请稍后重试」，
    而真实情况是"这条超出 3 天留存窗口"—— **重试一万次也不会好**。
    前端要能分开显示，就必须从后端拿到**不同的码**（前端只拿得到 `code`
    与 `message`，见 `errors.ts` 的 `apiErrorFromResponse`）。
    """
    _fresh_body_store(monkeypatch, tmp_path)
    _fresh_tone_store(monkeypatch, tmp_path)
    _patch_feature_gate(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(INTEL_ROUTE.intel_item(h, None))
    assert ei.value.status_code == 404
    # 空串 / 超长垃圾串 = **编号无效**（压根不是一条记录）；
    # 形态合法但查不到 = **没留存**（没采到 / 未落库 / 已过期）。
    # 两者对用户的下一步完全不同，所以码必须不同。
    expected = "item_bad_hash" if (not h or len(h) > 128) else "item_not_retained"
    assert ei.value.detail["code"] == expected


def test_item_endpoint_404_detail_codes_are_distinguishable(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """★ 两个 404 成因给出**不同的错误码**（这是 D 项修复的可断言落点）。

    ⚠️ 与上一条用例分开写，是因为前者的 `empty` 参数被 `_fresh_tone_store`
    的清空掩盖过一次（`h=""` 时"存储里没有"与"编号无效"看起来一样）——
    这里直接比对两个分支的码，不经过任何"猜"。
    """
    _fresh_body_store(monkeypatch, tmp_path)
    _fresh_tone_store(monkeypatch, tmp_path)
    _patch_feature_gate(monkeypatch)
    codes = set()
    for h in ("", "x" * 200, "形态合法但没存过"):
        with pytest.raises(HTTPException) as ei:
            asyncio.run(INTEL_ROUTE.intel_item(h, None))
        codes.add(ei.value.detail["code"])
    assert codes == {"item_bad_hash", "item_not_retained"}, codes
    # 旧码不许再出现：留着它会让前端走进"稍后重试"那条兜底分支
    assert "item_not_found" not in codes


def test_item_endpoint_404s_after_the_retention_prune(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """★ 超出留存窗口后**如实 404**（不是给一份过期的全文）。"""
    _fresh_body_store(monkeypatch, tmp_path)
    _fresh_tone_store(monkeypatch, tmp_path)
    _patch_feature_gate(monkeypatch)
    old = (datetime.now(timezone.utc).astimezone()
           - timedelta(days=body_store.RETAIN_DAYS + 2)).isoformat(
               timespec="seconds")
    body_store.save_many([_row("h1", at=old)])
    body_store.prune()
    with pytest.raises(HTTPException) as ei:
        asyncio.run(INTEL_ROUTE.intel_item("h1", None))
    assert ei.value.status_code == 404


def test_item_endpoint_leaks_no_channel_identity(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """★★ 全文接口**不含任何渠道身份**（构造方式决定的，不是脱敏）。

    威胁模型是"用户按 F12"：响应体直接发给浏览器，日志脱敏对它无效。
    所以响应里**根本不存在** `source_alias` / `platform` / `source_name` /
    URL / 群 ID —— 白名单式构造：将来存储行里多一个字段，它默认不出。
    """
    _fresh_body_store(monkeypatch, tmp_path)
    _fresh_tone_store(monkeypatch, tmp_path)
    _patch_feature_gate(monkeypatch)
    body_store.save_many([{**_row("h1", text=_NOTE),
                           "source_alias": "research-note-zsxq",
                           "source_name": "知识星球-调研纪要",
                           "platform": "知识星球",
                           "extra": {"url": "https://wx.zsxq.com/x"}}])

    blob = json.dumps(asyncio.run(INTEL_ROUTE.intel_item("h1", None)),
                      ensure_ascii=False)
    for leak in ("source_alias", "source_name", "platform", "zsxq", "星球",
                 "调研纪要", "https://", "group_id"):
        assert leak not in blob, f"全文接口里出现 {leak!r}：{blob[:200]}"
    # `source_alias` 的假名形态（`src-…`）同样不该出现：这个端点根本不需要它
    assert not re.search(r"src-[0-9a-f]{6,}", blob)


# ======================================================================
# 需求二 · 全文**绝不能**跟着情报流一起发出去
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


def _patch_zsxq(monkeypatch: pytest.MonkeyPatch, note: _StubTopic) -> None:
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


def test_feed_payload_still_carries_no_full_text(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """★★ 情报流 payload 里**没有全文** —— 只有"点开才取"的那条路有。

    ⚠️ 这是"点击看全文"这个设计能成立的前提：全文跟着一页几十条发出去，
    等于把 `summary` 的 260 字展示截断整个抵消掉（移动端一条占满十屏）。
    所以 `IntelFeed.to_public()` 继续剥 `extract_text`，本存储另走一条路。
    """
    note = _StubTopic("【天风电子】半导体设备", _NOTE, "h-note-1")
    _patch_zsxq(monkeypatch, note)
    _fresh_body_store(monkeypatch, tmp_path)

    feed = asyncio.run(S.build_feed(limit=50, group_undetermined=False))
    rows = [it for it in feed.items if it.get("content_hash") == "h-note-1"]
    assert rows, "笔记没进流水线"
    # 进程内**有**全文（`tone_job` / 存储都要用它）
    assert _TAIL_MARKER in (rows[0].get("extract_text") or "")
    # 前提自检：标记确实在**展示截断之外**，否则这条用例证明不了任何事
    assert _TAIL_MARKER not in rows[0]["summary"], "样本没被截断，用例失去意义"

    blob = json.dumps(feed.to_public(), ensure_ascii=False)
    assert "extract_text" not in blob, "内部全文键泄漏到接口"
    assert _TAIL_MARKER not in blob, "全文内容泄漏到情报流 payload"

    # 落库之后，全文只能从**新端点**那条路取到
    stats = body_store.persist(feed.items)
    assert stats["written"] == 0, "build_feed 已经存过了，这里不该再写一行"
    hit = body_store.get("h-note-1")
    assert hit is not None and _TAIL_MARKER in hit["text"]


# ======================================================================
# 第六轮（2026-10-01）：不依赖定时任务的两条修复
# ======================================================================

def test_build_feed_persists_full_text_without_the_scheduler(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """★★ A 项：`build_feed` **自己**落库全文，不等 `_intel_tone_extract`。

    用户报障「点击看全文 → 全文读取失败」的直接成因是
    `data/intel/item_bodies.jsonl` 根本不存在 —— 而落库当时**只有**
    定时任务一处。实测那条链路每 2 小时一班，而条目是随时采进来的：
    18:20 那一班之后 `intel_zsxq_collect` 又采了 30 条，
    那批条目在两小时内点开就是 404。

    ⚠️ 这条用例**不 mock 任何调度器**：它就是在断言
    "一次普通的 `build_feed` 之后，全文必须已经在盘上"。
    """
    note = _StubTopic("【天风电子】半导体设备", _NOTE, "h-note-9")
    _patch_zsxq(monkeypatch, note)
    _fresh_body_store(monkeypatch, tmp_path)

    feed = asyncio.run(S.build_feed(limit=50, group_undetermined=False))
    assert feed.items, "流水线是空的，用例失去意义"

    # ★ 没有任何定时任务参与，全文已经落库
    hit = body_store.get("h-note-9")
    assert hit is not None, "build_feed 之后全文仍然没落库（A 项没生效）"
    assert _TAIL_MARKER in hit["text"], "落库的是展示摘要而不是清洗后的全文"

    # ★ 幂等：同一批再跑一次**不许**再写一行（否则文件随请求次数线性长大）
    again = asyncio.run(S.build_feed(limit=50, group_undetermined=False))
    assert again.items
    lines = body_store.store_path(tmp_path).read_text(
        encoding="utf-8").splitlines()
    assert len(lines) == 1, f"重复请求把文件写大了：{len(lines)} 行"


def test_build_feed_uses_rule_layer_when_no_verdict_is_stored(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """★★ B 项：没有抽取结果时，方向由**规则层**给，且标明 `source="rules"`。

    用户报障原文：「AI 超级计算机中的芯片数量增加一倍以上」这条明显偏多的
    笔记**没有【多】标记**。成因是抽取任务每 2 小时一班，那批新条目
    在 `tone_store` 里**一行都没有** —— 而方向标记原来完全依赖它。

    ⚠️ 断言 `source`：词表判定**必须**与模型判定分得开。把它标成
    `rules+llm` 会让用户以为模型看过这条（"把请求当保证"的同类错误）。
    """
    note = _StubTopic(
        "AI 超级计算机中的芯片数量增加一倍以上",
        "AI 超级计算机中的芯片数量增加一倍以上，公司在手订单同步增长，"
        "产能持续扩产。",
        "h-note-bull")
    _patch_zsxq(monkeypatch, note)
    _fresh_body_store(monkeypatch, tmp_path)
    _fresh_tone_store(monkeypatch, tmp_path)      # 存储是**空**的（= 抽取没跑过）

    feed = asyncio.run(S.build_feed(limit=50, group_undetermined=False))
    row = next(it for it in feed.items if it.get("content_hash") == "h-note-bull")
    tone = row.get("tone") or {}
    assert tone.get("tone") == "偏多", tone
    assert tone.get("has_tone") is True, tone
    assert tone.get("source") == "rules", "词表判定被伪装成了模型判定"
    assert tone.get("phrases"), "有方向就必须有可核对的依据词"


def test_build_feed_does_not_tag_generic_prose(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """★ B 项的**反向**用例：一个泛用词不足以定方向（防过度标注）。

    `增加` / `增长` 在任何中文文本里都出现。无门槛时这一句
    （纯数据播报、没有方向）会因为"增长"被标成【多】——
    而用户核不出来（字面上确实有"增长"），正是本项目最忌讳的
    "看起来完全合理的错"。

    ⚠️ 样本刻意**同时**覆盖两条可能的误判路径：
      · 只有 1 个弱档词（`增长`）→ 票数不够；
      · 文本里**没有**市场语境词（`消费` / `需求` 那类宏观词
        刻意不收进 `_MARKET_CONTEXT_RE`）→ 门槛不放宽。
    两条中任何一条失效，这条用例都会红。
    """
    note = _StubTopic(
        "8月社会消费品零售总额数据",
        "8月份社会消费品零售总额同比增长3.2%，其中餐饮收入增长2.1%，"
        "网上零售额增加1200万元。数据由国家统计局发布。",
        "h-note-flat")
    _patch_zsxq(monkeypatch, note)
    _fresh_body_store(monkeypatch, tmp_path)
    _fresh_tone_store(monkeypatch, tmp_path)

    feed = asyncio.run(S.build_feed(limit=50, group_undetermined=False))
    row = next(it for it in feed.items if it.get("content_hash") == "h-note-flat")
    assert not (row.get("tone") or {}).get("has_tone"), \
        f"泛用词把中性播报标成了方向：{row.get('tone')}"


def test_summary_is_one_line_and_capped(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """★★ C 项：展示摘要**折叠成单行**且 ≤200 字，并标明是摘录还是摘要。

    用户口径（2026-10-01）：
      · "本地模型提取的信息，要求输出文字不能超过200个"
      · "精简200字以内，用户没时间看全文，要效率"
      · "前端信息不要换行"

    ⚠️ 这段断言的就是用户抱怨的那个形态：一条长原文**不能**原样
    （带换行、超 200 字）走到界面上。
    """
    body = ("【天风电子】半导体设备景气上行。\n\n存储芯片价格连续上涨，"
            "中芯国际受益于扩产，机构上调盈利预测。\r\n"
            "光伏组件价格下滑，隆基绿能承压。\u3000全角空格也要折掉。" + "补" * 200)
    note = _StubTopic("标题", body, "h-note-long")
    _patch_zsxq(monkeypatch, note)
    _fresh_body_store(monkeypatch, tmp_path)
    _fresh_tone_store(monkeypatch, tmp_path)

    feed = asyncio.run(S.build_feed(limit=50, group_undetermined=False))
    row = next(it for it in feed.items if it.get("content_hash") == "h-note-long")
    st = row["summary_text"]
    assert set(st) == {"text", "kind", "truncated"}, st
    assert "\n" not in st["text"] and "\r" not in st["text"], "还有换行"
    assert "\u3000" not in st["text"], "全角空格没被折叠"
    assert "  " not in st["text"], "连续空格没被折叠"
    assert len(st["text"]) <= S.DISPLAY_SUMMARY_MAX_CHARS, len(st["text"])
    # 没有抽取结果 → 这是**摘录**，必须标出来（用户抱怨的正是"被截断的内容"）
    assert st["kind"] == "excerpt" and st["truncated"] is True, st


def test_summary_prefers_the_model_summary_and_marks_it_as_such(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """有抽取结果时用**模型摘要**，并标 `kind="model"`（不是摘录）。"""
    note = _StubTopic("标题", "原文" * 200, "h-note-model")
    _patch_zsxq(monkeypatch, note)
    _fresh_body_store(monkeypatch, tmp_path)
    _fresh_tone_store(monkeypatch, tmp_path)
    tone_store.save_many([{
        "content_hash": "h-note-model", "tone": "偏多", "has_tone": True,
        "neutral": False,
        "summary": "机构上调盈利预测，存储芯片价格连续上涨",
    }], root=tmp_path)

    feed = asyncio.run(S.build_feed(limit=50, group_undetermined=False))
    row = next(it for it in feed.items if it.get("content_hash") == "h-note-model")
    st = row["summary_text"]
    assert st["kind"] == "model", st
    assert st["text"] == "机构上调盈利预测，存储芯片价格连续上涨", st
    # 200 是**上限不是目标**：模型摘要 19 字就显示 19 字，**不许补足**
    assert len(st["text"]) < S.DISPLAY_SUMMARY_MAX_CHARS


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
